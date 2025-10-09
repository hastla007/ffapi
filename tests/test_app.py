import asyncio
import importlib
import io
import json
import os
import shutil
import sys
import time
from http.cookies import SimpleCookie
from itertools import count
from pathlib import Path
from typing import List, Tuple, Optional
from urllib.parse import urlencode, urlparse, parse_qs
from unittest.mock import Mock

import pytest
from fastapi import HTTPException, UploadFile
from starlette.datastructures import Headers
from pydantic import ValidationError


class StubHeadResponse:
    def __init__(self, *, length: int, status_code: int = 200):
        self.headers = {"content-length": str(length)}
        self.status_code = status_code

    def raise_for_status(self):
        if self.status_code >= 400:
            raise RuntimeError(f"status {self.status_code}")


class StubStreamResponse:
    def __init__(
        self,
        *,
        status_code: int,
        chunks: List[bytes],
        fail_after_first_chunk: bool = False,
        headers: dict | None = None,
    ) -> None:
        self.status_code = status_code
        self.headers = headers or {"content-length": str(sum(len(c) for c in chunks))}
        self._chunks = list(chunks)
        self._fail_after_first = fail_after_first_chunk

    async def __aenter__(self):
        return self

    async def __aexit__(self, exc_type, exc, tb):
        return False

    def raise_for_status(self):
        if self.status_code >= 400:
            raise RuntimeError(f"status {self.status_code}")

    async def aiter_bytes(self, chunk_size: int):
        for index, chunk in enumerate(self._chunks):
            yield chunk
            if self._fail_after_first and index == 0:
                raise RuntimeError("connection dropped")


class StubAsyncClient:
    def __init__(self, head_responses: List[StubHeadResponse], stream_responses: List[StubStreamResponse]):
        self.head_responses = list(head_responses)
        self.stream_responses = list(stream_responses)
        self.head_calls: List[dict] = []
        self.stream_calls: List[dict] = []
        self.head_timeouts: List[float] = []
        self.stream_timeouts: List[float] = []

    async def head(self, url: str, headers=None, timeout=None):
        self.head_calls.append(dict(headers or {}))
        self.head_timeouts.append(timeout)
        return self.head_responses.pop(0)

    def stream(self, method: str, url: str, headers=None, timeout=None):
        self.stream_calls.append(dict(headers or {}))
        self.stream_timeouts.append(timeout)
        return self.stream_responses.pop(0)

    async def aclose(self):
        return None


class DummyCompletedProcess:
    def __init__(self, returncode: int = 0, stdout=None, stderr=None):
        self.returncode = returncode
        self.stdout = stdout
        self.stderr = stderr


async def _call_app(app, method: str, path: str, *, headers=None, body: bytes = b"", query: str = "") -> Tuple[int, dict, bytes]:
    headers = headers or []
    scope = {
        "type": "http",
        "asgi": {"version": "3.0"},
        "method": method,
        "path": path,
        "raw_path": path.encode("ascii"),
        "query_string": query.encode("ascii"),
        "headers": [(k.lower().encode("latin-1"), v.encode("latin-1")) for k, v in headers],
        "client": ("testclient", 123),
        "server": ("testserver", 80),
        "scheme": "http",
    }

    request_body = body
    request_complete = False
    response_status = None
    response_headers = []
    response_body = bytearray()

    async def receive():
        nonlocal request_body, request_complete
        if request_complete:
            return {"type": "http.disconnect"}
        request_complete = True
        return {"type": "http.request", "body": request_body, "more_body": False}

    async def send(message):
        nonlocal response_status, response_headers, response_body
        if message["type"] == "http.response.start":
            response_status = message["status"]
            response_headers = message.get("headers", [])
        elif message["type"] == "http.response.body":
            response_body.extend(message.get("body", b""))

    await app(scope, receive, send)

    headers_dict: dict[str, str] = {}
    set_cookies: List[str] = []
    for key_bytes, value_bytes in response_headers:
        key = key_bytes.decode("latin-1")
        value = value_bytes.decode("latin-1")
        if key.lower() == "set-cookie":
            set_cookies.append(value)
        else:
            headers_dict[key] = value
    if set_cookies:
        headers_dict["set-cookie"] = "\n".join(set_cookies)
    return response_status or 500, headers_dict, bytes(response_body)


def call_app(app, method: str, path: str, *, headers=None, body: bytes = b"", query: str = "") -> Tuple[int, dict, bytes]:
    return asyncio.run(_call_app(app, method, path, headers=headers, body=body, query=query))


def extract_csrf_token(html: str) -> str:
    marker = 'name="csrf_token" value="'
    start = html.find(marker)
    if start == -1:
        raise AssertionError("CSRF token not found in HTML response")
    start += len(marker)
    end = html.find('"', start)
    if end == -1:
        raise AssertionError("Malformed CSRF token field")
    return html[start:end]


def update_cookie_jar(jar: dict[str, str], headers: dict) -> None:
    set_cookie = headers.get("set-cookie")
    if not set_cookie:
        return
    cookie = SimpleCookie()
    if isinstance(set_cookie, str):
        parts = [p for p in set_cookie.split("\n") if p]
    else:
        parts = []
    for part in parts:
        cookie.load(part)
    for key, morsel in cookie.items():
        jar[key] = morsel.value


def build_cookie_header(jar: dict[str, str]) -> str:
    return "; ".join(f"{k}={v}" for k, v in jar.items()) if jar else ""


def fetch_csrf_token(app, jar: dict[str, str], path: str = "/settings", query: str = "") -> str:
    headers = []
    cookie_header = build_cookie_header(jar)
    if cookie_header:
        headers.append(("Cookie", cookie_header))
    status, response_headers, body = call_app(app, "GET", path, headers=headers, query=query)
    assert status == 200
    update_cookie_jar(jar, response_headers)
    return extract_csrf_token(body.decode("utf-8"))


def post_form(app, path: str, data: dict[str, str], jar: dict[str, str]):
    headers = [("Content-Type", "application/x-www-form-urlencoded")]
    cookie_header = build_cookie_header(jar)
    if cookie_header:
        headers.append(("Cookie", cookie_header))
    status, response_headers, body = call_app(
        app,
        "POST",
        path,
        headers=headers,
        body=urlencode(data).encode("utf-8"),
    )
    update_cookie_jar(jar, response_headers)
    return status, response_headers, body


def get_with_cookies(app, path: str, jar: dict[str, str], query: str = ""):
    headers = []
    cookie_header = build_cookie_header(jar)
    if cookie_header:
        headers.append(("Cookie", cookie_header))
    status, response_headers, body = call_app(app, "GET", path, headers=headers, query=query)
    update_cookie_jar(jar, response_headers)
    return status, response_headers, body


def test_security_headers_and_generated_request_id(app_module):
    app_module.RATE_LIMITER.reset()
    status, headers, body = call_app(app_module.app, "GET", "/health")
    assert status == 200
    assert (
        headers["content-security-policy"]
        == "default-src 'self'; style-src 'self' 'unsafe-inline'; script-src 'self' 'unsafe-inline'; img-src 'self' data:"
    )
    assert headers["x-content-type-options"] == "nosniff"
    assert headers["x-frame-options"] == "DENY"
    assert "x-request-id" in headers
    payload = json.loads(body.decode("utf-8"))
    assert isinstance(payload["ok"], bool)


def test_request_id_preserved_from_header(app_module):
    app_module.RATE_LIMITER.reset()
    provided = "req-123"
    status, headers, _ = call_app(
        app_module.app,
        "GET",
        "/health",
        headers=[("X-Request-ID", provided)],
    )
    assert status == 200
    assert headers["x-request-id"] == provided


def test_publish_file_rejects_invalid_extension(app_module, tmp_path):
    src = tmp_path / "source.bin"
    src.write_bytes(b"data")

    with pytest.raises(ValueError):
        app_module.publish_file(src, "bad")


@pytest.fixture(scope="session")
def app_module(tmp_path_factory: pytest.TempPathFactory):
    base = tmp_path_factory.mktemp("data")
    env = {
        "PUBLIC_DIR": str(base / "public"),
        "WORK_DIR": str(base / "work"),
        "LOGS_DIR": str(base / "logs"),
        "PUBLIC_BASE_URL": "http://example.com",
        "RETENTION_DAYS": "7",
        "MIN_FREE_SPACE_MB": "0",
        "MAX_FILE_SIZE_MB": "1",
        "PUBLIC_CLEANUP_INTERVAL_SECONDS": "0",
    }
    for key, value in env.items():
        os.environ[key] = value

    repo_root = Path(__file__).resolve().parents[1]
    if str(repo_root) not in sys.path:
        sys.path.insert(0, str(repo_root))

    module_name = "app.main"
    if module_name in sys.modules:
        del sys.modules[module_name]
    module = importlib.import_module(module_name)
    module._FFMPEG_VERSION_CACHE = None
    module.UI_AUTH.reset()
    module.PROBE_CACHE.clear()
    module.csrf_protect.validate_csrf = lambda submitted, cookie: None
    return module


@pytest.fixture()
def patched_app(app_module, monkeypatch, tmp_path):
    publish_root = tmp_path / "published"
    publish_root.mkdir(parents=True, exist_ok=True)
    counter = count()

    def fake_publish_file(src: Path, ext: str, *, duration_ms: Optional[int] = None):
        dst = publish_root / f"file_{next(counter)}{ext}"
        src_path = Path(src)
        if src_path.exists():
            dst.write_bytes(src_path.read_bytes())
        else:
            dst.write_bytes(b"")
        fake_publish_file.last_duration = duration_ms
        return {
            "dst": str(dst),
            "url": f"http://example.com/{dst.name}",
            "rel": f"/files/{dst.name}",
            "thumbnail": None,
        }
    fake_publish_file.last_duration = None

    async def fake_download_to(url: str, dest: Path, headers=None, max_retries: int = 3, chunk_size: int = 1024 * 1024, client=None):
        dest_path = Path(dest)
        dest_path.parent.mkdir(parents=True, exist_ok=True)
        dest_path.write_bytes(f"downloaded from {url}".encode("utf-8"))

    def fake_save_log(*args, **kwargs):
        return None

    def fake_run(cmd, *args, **kwargs):
        text_mode = kwargs.get("text", False)
        capture_output = kwargs.get("capture_output", False)
        stdout_target = kwargs.get("stdout")
        stderr_target = kwargs.get("stderr")

        if isinstance(cmd, list):
            command = cmd
        else:
            command = cmd.split()

        def write_stream(target, data: bytes | str):
            if target is None or not hasattr(target, "write"):
                return
            try:
                target.write(data)
            except TypeError:
                if isinstance(data, str):
                    target.write(data.encode("utf-8"))
                else:
                    target.write(data.decode("utf-8"))

        if command and command[0] == "ffmpeg" and len(command) > 1 and command[1] == "-version":
            stdout = "ffmpeg version n4.0" if text_mode else b"ffmpeg version n4.0"
            return DummyCompletedProcess(returncode=0, stdout=stdout, stderr="" if text_mode else b"")

        if command and command[0] == "ffprobe":
            payload = json.dumps({"format": {"format_name": "fake"}, "streams": []}).encode("utf-8")
            return DummyCompletedProcess(returncode=0, stdout=payload, stderr=b"")

        output_path = None
        for token in reversed(command):
            if token.startswith("-"):
                break
            output_path = Path(token)
            break
        if output_path is not None:
            output_path.parent.mkdir(parents=True, exist_ok=True)
            output_path.write_bytes(b"fake output")

        data_bytes = b"run"
        data_text = "run"
        if stdout_target is not None:
            write_stream(stdout_target, data_text if text_mode else data_bytes)
        if stderr_target is not None:
            write_stream(stderr_target, data_text if text_mode else data_bytes)

        stdout_value = "" if text_mode else b""
        if capture_output:
            stdout_value = "" if text_mode else b""
        return DummyCompletedProcess(returncode=0, stdout=stdout_value, stderr="" if text_mode else b"")

    class DummyPopen:
        def __init__(self, cmd, stdout=None, stderr=None, **kwargs):
            self.cmd = cmd
            self.stdout = stdout if hasattr(stdout, "write") else io.StringIO()
            self.stderr = stderr if hasattr(stderr, "write") else io.StringIO()
            self.returncode = 0
            payload = b"run"
            output_path = None
            for token in reversed(cmd):
                if isinstance(token, str) and token.startswith("-"):
                    continue
                output_path = Path(token)
                break
            if output_path is not None:
                output_path.parent.mkdir(parents=True, exist_ok=True)
                output_path.write_bytes(b"fake output")
            payload_text = payload.decode("utf-8")
            try:
                self.stdout.write(payload_text)
            except Exception:
                pass
            if self.stderr is not self.stdout:
                try:
                    self.stderr.write(payload_text)
                except Exception:
                    pass

        def wait(self, timeout=None):
            return self.returncode

        def kill(self):
            self.returncode = -9

    monkeypatch.setattr(app_module, "publish_file", fake_publish_file)
    monkeypatch.setattr(app_module, "_download_to", fake_download_to)
    monkeypatch.setattr(app_module, "save_log", fake_save_log)
    monkeypatch.setattr(app_module.subprocess, "run", fake_run)
    monkeypatch.setattr(app_module.subprocess, "Popen", DummyPopen)
    app_module._FFMPEG_VERSION_CACHE = None

    for directory in (app_module.PUBLIC_DIR, app_module.LOGS_DIR, app_module.WORK_DIR):
        if directory.exists():
            shutil.rmtree(directory)
        directory.mkdir(parents=True, exist_ok=True)

    app_module.METRICS.reset()
    app_module.RATE_LIMITER.reset()
    app_module.UI_AUTH.reset()
    app_module.API_KEYS.reset()

    return app_module


@pytest.fixture(autouse=True)
def reset_ui_auth_state(app_module):
    app_module.UI_AUTH.reset()
    app_module.API_KEYS.reset()
    yield
    app_module.UI_AUTH.reset()
    app_module.API_KEYS.reset()


def test_root_redirects_to_downloads(patched_app):
    status, headers, _ = call_app(patched_app.app, "GET", "/")
    assert status in {301, 302, 307, 308}
    assert headers["location"] == "/downloads"


def test_downloads_page_lists_existing_files(patched_app):
    day_dir = patched_app.PUBLIC_DIR / "20240101"
    day_dir.mkdir(parents=True, exist_ok=True)
    (day_dir / "example.txt").write_text("hello", encoding="utf-8")

    status, _, body = call_app(patched_app.app, "GET", "/downloads")
    assert status == 200
    html = body.decode()
    assert "Search by filename or date" in html
    assert "example.txt" in html
    assert "Showing 1-1 of 1 file(s)" in html
    assert "thumb-placeholder" in html


def test_downloads_page_escapes_special_characters(patched_app):
    day_dir = patched_app.PUBLIC_DIR / "20240104"
    day_dir.mkdir(parents=True, exist_ok=True)
    filename = "<img src=x onerror=alert('xss')>.mp4"
    (day_dir / filename).write_text("attack", encoding="utf-8")

    status, _, body = call_app(patched_app.app, "GET", "/downloads")
    assert status == 200
    html = body.decode()
    assert "&lt;img src=x onerror=alert(&#x27;xss&#x27;)&gt;.mp4" in html
    assert "onerror" in html
    assert "<img" not in html.split("Generated Files", 1)[1]


def test_downloads_search_filters_results(patched_app):
    day_one = patched_app.PUBLIC_DIR / "20240110"
    day_two = patched_app.PUBLIC_DIR / "20240111"
    day_one.mkdir(parents=True, exist_ok=True)
    day_two.mkdir(parents=True, exist_ok=True)
    (day_one / "report-a.txt").write_text("a", encoding="utf-8")
    (day_two / "holiday.mp4").write_text("b", encoding="utf-8")

    status, _, body = call_app(
        patched_app.app,
        "GET",
        "/downloads",
        query=urlencode({"q": "holiday"}),
    )
    assert status == 200
    html = body.decode()
    assert "holiday.mp4" in html
    assert "report-a.txt" not in html
    assert "No files match your filters" not in html


def test_downloads_supports_pagination(patched_app):
    day_dir = patched_app.PUBLIC_DIR / "20240112"
    day_dir.mkdir(parents=True, exist_ok=True)
    for index in range(30):
        (day_dir / f"video_{index:02d}.mp4").write_text(str(index), encoding="utf-8")

    status, _, body = call_app(
        patched_app.app,
        "GET",
        "/downloads",
        query=urlencode({"page": 2}),
    )
    assert status == 200
    html = body.decode()
    assert "Page 2 of 2" in html
    assert "video_00.mp4" in html
    assert "video_29.mp4" not in html


def test_logs_page_supports_pagination(patched_app):
    day_dir = patched_app.LOGS_DIR / "20240113"
    day_dir.mkdir(parents=True, exist_ok=True)
    for index in range(40):
        (day_dir / f"log_{index:02d}_compose.log").write_text("entry", encoding="utf-8")

    status, _, body = call_app(
        patched_app.app,
        "GET",
        "/logs",
        query=urlencode({"page": 2, "page_size": 20}),
    )
    assert status == 200
    html = body.decode()
    assert "Page 2 of 2" in html
    assert "log_39_compose.log" not in html
    assert "log_00_compose.log" in html


def test_settings_page_shows_auth_retention_and_storage_sections(patched_app):
    status, _, body = call_app(patched_app.app, "GET", "/settings")
    assert status == 200
    html = body.decode()
    assert "UI Authentication" in html
    assert "Require login for dashboard pages" in html
    assert "auth-row" in html
    assert "credentials-block is-disabled" in html
    assert "button type=\"submit\" disabled" in html
    assert "Two-factor authentication" in html
    assert "twofactor-card is-disabled" in html
    assert "Generate backup codes" in html
    assert "Download as TXT" in html
    assert "Backup codes" in html
    assert "Enable dashboard login to configure two-factor authentication." in html
    assert "API Authentication" in html
    assert "Require API key for API requests" in html
    assert "Save API authentication" in html
    assert "Retention Settings" in html
    assert "Default retention (hours)" in html
    assert "name=\"retention_hours\"" in html
    assert "storage-table" in html
    assert "Performance Settings" in html
    assert "name=\"rate_limit_rpm\"" in html
    assert "Storage Management" in html
    assert "Total storage used" in html
    assert html.index("Update retention") < html.index("Storage Management")


def test_dashboard_requires_login_when_enabled(patched_app):
    jar: dict[str, str] = {}
    csrf_token = fetch_csrf_token(patched_app.app, jar)
    status, headers, _ = post_form(
        patched_app.app,
        "/settings/ui-auth",
        {"require_login": "true", "csrf_token": csrf_token},
        jar,
    )
    assert status == 303
    assert headers["location"].startswith("/settings")

    status, headers, _ = get_with_cookies(patched_app.app, "/downloads", jar)
    assert status == 303
    assert headers["location"].startswith("/settings?next=%2Fdownloads")

    login_csrf = fetch_csrf_token(
        patched_app.app, jar, path="/settings", query="next=%2Fdownloads"
    )
    status, _, body = post_form(
        patched_app.app,
        "/settings/login",
        {
            "username": "admin",
            "password": "wrongpass",
            "next": "/downloads",
            "csrf_token": login_csrf,
        },
        jar,
    )
    assert status == 401
    body_text = body.decode()
    assert "Invalid username or password" in body_text
    login_csrf = extract_csrf_token(body_text)

    status, headers, _ = post_form(
        patched_app.app,
        "/settings/login",
        {
            "username": "admin",
            "password": "admin123",
            "next": "/downloads",
            "csrf_token": login_csrf,
        },
        jar,
    )
    assert status == 303
    assert headers["location"] == "/downloads"

    status, _, _ = get_with_cookies(patched_app.app, "/downloads", jar)
    assert status == 200


def test_two_factor_toggle_requires_valid_code(patched_app):
    jar: dict[str, str] = {}
    csrf_token = fetch_csrf_token(patched_app.app, jar)
    status, _, _ = post_form(
        patched_app.app,
        "/settings/ui-auth",
        {"require_login": "true", "csrf_token": csrf_token},
        jar,
    )
    assert status == 303

    login_csrf = fetch_csrf_token(patched_app.app, jar)
    status, _, _ = post_form(
        patched_app.app,
        "/settings/login",
        {"username": "admin", "password": "admin123", "csrf_token": login_csrf},
        jar,
    )
    assert status == 303

    settings_csrf = fetch_csrf_token(patched_app.app, jar)
    status, headers, _ = post_form(
        patched_app.app,
        "/settings/two-factor",
        {"action": "enable", "code": "000000", "csrf_token": settings_csrf},
        jar,
    )
    assert status == 303
    assert "error=Invalid+authentication+code" in headers["location"]
    assert not patched_app.UI_AUTH.is_two_factor_enabled()

    secret = patched_app.UI_AUTH.get_two_factor_secret()
    code = patched_app.generate_totp_code(secret)
    settings_csrf = fetch_csrf_token(patched_app.app, jar)
    status, headers, _ = post_form(
        patched_app.app,
        "/settings/two-factor",
        {"action": "enable", "code": code, "csrf_token": settings_csrf},
        jar,
    )
    assert status == 303
    assert (
        "message=Two-factor+authentication+enabled.+Backup+codes+generated"
        in headers["location"]
    )
    assert patched_app.UI_AUTH.is_two_factor_enabled()
    statuses = patched_app.UI_AUTH.get_backup_code_status()
    assert len(statuses) == patched_app.BACKUP_CODE_COUNT
    assert all(not item["used"] for item in statuses)


def test_login_requires_totp_when_two_factor_enabled(patched_app):
    jar: dict[str, str] = {}
    csrf_token = fetch_csrf_token(patched_app.app, jar)
    status, _, _ = post_form(
        patched_app.app,
        "/settings/ui-auth",
        {"require_login": "true", "csrf_token": csrf_token},
        jar,
    )
    assert status == 303

    login_csrf = fetch_csrf_token(patched_app.app, jar)
    status, _, _ = post_form(
        patched_app.app,
        "/settings/login",
        {"username": "admin", "password": "admin123", "csrf_token": login_csrf},
        jar,
    )
    assert status == 303

    secret = patched_app.UI_AUTH.get_two_factor_secret()
    code = patched_app.generate_totp_code(secret)
    settings_csrf = fetch_csrf_token(patched_app.app, jar)
    status, _, _ = post_form(
        patched_app.app,
        "/settings/two-factor",
        {"action": "enable", "code": code, "csrf_token": settings_csrf},
        jar,
    )
    assert status == 303
    assert patched_app.UI_AUTH.is_two_factor_enabled()
    backup_codes = patched_app.UI_AUTH.pop_pending_backup_codes()
    assert len(backup_codes) == patched_app.BACKUP_CODE_COUNT

    logout_csrf = fetch_csrf_token(patched_app.app, jar)
    status, _, _ = post_form(
        patched_app.app,
        "/settings/logout",
        {"csrf_token": logout_csrf},
        jar,
    )
    assert status == 303

    login_csrf = fetch_csrf_token(
        patched_app.app, jar, path="/settings", query="next=%2Fdownloads"
    )
    status, _, body = post_form(
        patched_app.app,
        "/settings/login",
        {
            "username": "admin",
            "password": "admin123",
            "next": "/downloads",
            "csrf_token": login_csrf,
        },
        jar,
    )
    assert status == 401
    html = body.decode()
    assert "Authentication or backup code required" in html
    assert "name=\"totp\"" in html
    assert "name=\"backup_code\"" in html
    login_csrf = extract_csrf_token(html)

    status, _, body = post_form(
        patched_app.app,
        "/settings/login",
        {
            "username": "admin",
            "password": "admin123",
            "totp": "000000",
            "next": "/downloads",
            "csrf_token": login_csrf,
        },
        jar,
    )
    assert status == 401
    html = body.decode()
    assert "Invalid authentication or backup code" in html
    login_csrf = extract_csrf_token(html)

    status, headers, _ = post_form(
        patched_app.app,
        "/settings/login",
        {
            "username": "admin",
            "password": "admin123",
            "backup_code": backup_codes[0],
            "next": "/downloads",
            "csrf_token": login_csrf,
        },
        jar,
    )
    assert status == 303
    assert headers["location"] == "/downloads"
    statuses_after = patched_app.UI_AUTH.get_backup_code_status()
    assert any(item["used"] for item in statuses_after)


def test_settings_shows_backup_codes_alert(patched_app):
    jar: dict[str, str] = {}
    csrf_token = fetch_csrf_token(patched_app.app, jar)
    status, _, _ = post_form(
        patched_app.app,
        "/settings/ui-auth",
        {"require_login": "true", "csrf_token": csrf_token},
        jar,
    )
    assert status == 303

    login_csrf = fetch_csrf_token(patched_app.app, jar)
    status, _, _ = post_form(
        patched_app.app,
        "/settings/login",
        {"username": "admin", "password": "admin123", "csrf_token": login_csrf},
        jar,
    )
    assert status == 303

    secret = patched_app.UI_AUTH.get_two_factor_secret()
    code = patched_app.generate_totp_code(secret)
    settings_csrf = fetch_csrf_token(patched_app.app, jar)
    status, headers, _ = post_form(
        patched_app.app,
        "/settings/two-factor",
        {"action": "enable", "code": code, "csrf_token": settings_csrf},
        jar,
    )
    assert status == 303
    target = headers["location"].split("/settings", 1)[1]
    page_status, _, page_body = call_app(
        patched_app.app,
        "GET",
        "/settings",
        headers=[("Cookie", build_cookie_header(jar))],
        query=target.lstrip("?"),
    )
    assert page_status == 200
    html = page_body.decode()
    assert "alert info" in html
    assert "backup-list" in html
    assert "Generate backup codes" in html


def test_settings_page_enables_credentials_when_authentication_required(patched_app):
    patched_app.UI_AUTH.set_require_login(True)
    token = patched_app.UI_AUTH.create_session()
    status, _, body = call_app(
        patched_app.app,
        "GET",
        "/settings",
        headers=[("Cookie", f"{patched_app.SESSION_COOKIE_NAME}={token}")],
    )
    assert status == 200
    html = body.decode()
    assert "credentials-block is-disabled" not in html
    assert "button type=\"submit\" disabled" not in html


def test_settings_rejects_short_password(patched_app):
    body = urlencode({"username": "admin", "password": "short"}).encode()
    status, _, response_body = call_app(
        patched_app.app,
        "POST",
        "/settings/credentials",
        headers=[("Content-Type", "application/x-www-form-urlencoded")],
        body=body,
    )
    assert status == 400
    assert "Password must be at least 6 characters long" in response_body.decode()


def test_retention_settings_update_changes_value(patched_app):
    original_days = patched_app.RETENTION_DAYS
    try:
        body = urlencode({"retention_hours": "48"}).encode()
        status, headers, _ = call_app(
            patched_app.app,
            "POST",
            "/settings/retention",
            headers=[("Content-Type", "application/x-www-form-urlencoded")],
            body=body,
        )
        assert status == 303
        assert headers["location"].startswith("/settings")
        assert pytest.approx(patched_app.RETENTION_DAYS, rel=1e-6) == pytest.approx(2.0, rel=1e-6)
        assert pytest.approx(patched_app.settings.RETENTION_DAYS, rel=1e-6) == pytest.approx(2.0, rel=1e-6)
    finally:
        patched_app.RETENTION_DAYS = original_days
        patched_app.settings.RETENTION_DAYS = original_days


def test_retention_settings_rejects_invalid_hours(patched_app):
    original_days = patched_app.RETENTION_DAYS
    try:
        body = urlencode({"retention_hours": "0"}).encode()
        status, _, response_body = call_app(
            patched_app.app,
            "POST",
            "/settings/retention",
            headers=[("Content-Type", "application/x-www-form-urlencoded")],
            body=body,
        )
        assert status == 400
        html = response_body.decode()
        assert "Retention must be between 1 hour" in html
        assert pytest.approx(patched_app.RETENTION_DAYS, rel=1e-6) == pytest.approx(original_days, rel=1e-6)
        assert pytest.approx(patched_app.settings.RETENTION_DAYS, rel=1e-6) == pytest.approx(original_days, rel=1e-6)
    finally:
        patched_app.RETENTION_DAYS = original_days
        patched_app.settings.RETENTION_DAYS = original_days


def test_performance_settings_update_changes_globals(patched_app):
    original_rpm = patched_app.RATE_LIMITER.current_limit()
    original_timeout = patched_app.FFMPEG_TIMEOUT_SECONDS
    original_chunk = patched_app.UPLOAD_CHUNK_SIZE
    original_file_mb = patched_app.MAX_FILE_SIZE_MB
    try:
        body = urlencode(
            {
                "rate_limit_rpm": "120",
                "ffmpeg_timeout_minutes": "15",
                "upload_chunk_mb": "2.5",
                "max_file_size_mb": "5",
            }
        ).encode()
        status, headers, _ = call_app(
            patched_app.app,
            "POST",
            "/settings/performance",
            headers=[("Content-Type", "application/x-www-form-urlencoded")],
            body=body,
        )
        assert status == 303
        assert headers["location"].startswith("/settings")
        assert patched_app.RATE_LIMITER.current_limit() == 120
        assert patched_app.settings.RATE_LIMIT_REQUESTS_PER_MINUTE == 120
        assert patched_app.FFMPEG_TIMEOUT_SECONDS == 900
        assert patched_app.settings.FFMPEG_TIMEOUT_SECONDS == 900
        expected_chunk = int(2.5 * 1024 * 1024)
        assert patched_app.UPLOAD_CHUNK_SIZE == expected_chunk
        assert patched_app.settings.UPLOAD_CHUNK_SIZE == patched_app.UPLOAD_CHUNK_SIZE
        assert patched_app.MAX_FILE_SIZE_MB == 5
        assert patched_app.MAX_FILE_SIZE_BYTES == 5 * 1024 * 1024
        assert patched_app.settings.MAX_FILE_SIZE_MB == 5
    finally:
        patched_app.RATE_LIMITER.update_limit(original_rpm)
        patched_app.settings.RATE_LIMIT_REQUESTS_PER_MINUTE = original_rpm
        patched_app.FFMPEG_TIMEOUT_SECONDS = original_timeout
        patched_app.settings.FFMPEG_TIMEOUT_SECONDS = original_timeout
        patched_app.UPLOAD_CHUNK_SIZE = original_chunk
        patched_app.settings.UPLOAD_CHUNK_SIZE = original_chunk
        patched_app.MAX_FILE_SIZE_MB = original_file_mb
        patched_app.MAX_FILE_SIZE_BYTES = original_file_mb * 1024 * 1024
        patched_app.settings.MAX_FILE_SIZE_MB = original_file_mb


def test_performance_settings_rejects_bad_values(patched_app):
    original_rpm = patched_app.RATE_LIMITER.current_limit()
    body = urlencode(
        {
            "rate_limit_rpm": "0",
            "ffmpeg_timeout_minutes": "15",
            "upload_chunk_mb": "2",
            "max_file_size_mb": "5",
        }
    ).encode()
    status, _, response_body = call_app(
        patched_app.app,
        "POST",
        "/settings/performance",
        headers=[("Content-Type", "application/x-www-form-urlencoded")],
        body=body,
    )
    assert status == 400
    html = response_body.decode()
    assert "Rate Limit Rpm must be between 1 and 100000" in html
    assert patched_app.RATE_LIMITER.current_limit() == original_rpm
    assert patched_app.settings.RATE_LIMIT_REQUESTS_PER_MINUTE == original_rpm


def test_performance_settings_rejects_tiny_upload_chunk(patched_app):
    original_chunk = patched_app.UPLOAD_CHUNK_SIZE
    body = urlencode(
        {
            "rate_limit_rpm": "60",
            "ffmpeg_timeout_minutes": "15",
            "upload_chunk_mb": "0.0005",
            "max_file_size_mb": "5",
        }
    ).encode()
    status, _, response_body = call_app(
        patched_app.app,
        "POST",
        "/settings/performance",
        headers=[("Content-Type", "application/x-www-form-urlencoded")],
        body=body,
    )
    assert status == 400
    html = response_body.decode()
    assert "Upload chunk size must be at least 0.001 MB" in html
    assert patched_app.UPLOAD_CHUNK_SIZE == original_chunk


def test_api_keys_page_shows_controls(patched_app):
    status, _, body = call_app(patched_app.app, "GET", "/api-keys")
    assert status == 200
    html = body.decode()
    assert "API Keys" in html
    assert "Authentication status" in html
    assert "Enable API authentication in <a href=\"/settings\">Settings</a>" in html
    assert "Generate new key" in html
    assert "No API keys yet" in html
    assert "keys-card is-disabled" in html


def test_api_keys_toggle_requires_login_when_ui_auth_enabled(patched_app):
    jar: dict[str, str] = {}
    csrf_token = fetch_csrf_token(patched_app.app, jar)
    status, _, _ = post_form(
        patched_app.app,
        "/settings/ui-auth",
        {"require_login": "true", "csrf_token": csrf_token},
        jar,
    )
    assert status == 303

    status, headers, _ = get_with_cookies(patched_app.app, "/api-keys", jar)
    assert status == 303
    assert headers["location"].startswith("/settings?next=%2Fapi-keys")

    login_csrf = fetch_csrf_token(patched_app.app, jar)
    status, headers, _ = post_form(
        patched_app.app,
        "/settings/api-auth",
        {"require_api_key": "true", "csrf_token": login_csrf},
        jar,
    )
    assert status == 303
    assert headers["location"].startswith("/settings?error=")
    assert patched_app.API_KEYS.is_required() is False

    login_csrf = fetch_csrf_token(patched_app.app, jar)
    status, _, _ = post_form(
        patched_app.app,
        "/settings/login",
        {"username": "admin", "password": "admin123", "csrf_token": login_csrf},
        jar,
    )
    assert status == 303

    settings_csrf = fetch_csrf_token(patched_app.app, jar)
    status, headers, _ = post_form(
        patched_app.app,
        "/settings/api-auth",
        {"require_api_key": "true", "csrf_token": settings_csrf},
        jar,
    )
    assert status == 303
    assert headers["location"].startswith("/settings?message=")
    assert patched_app.API_KEYS.is_required() is True


def test_api_key_generation_and_listing(patched_app):
    toggle_body = urlencode({"require_api_key": "true"}).encode()
    status, headers, _ = call_app(
        patched_app.app,
        "POST",
        "/settings/api-auth",
        headers=[("Content-Type", "application/x-www-form-urlencoded")],
        body=toggle_body,
    )
    assert status == 303
    assert patched_app.API_KEYS.is_required() is True

    status, headers, _ = call_app(
        patched_app.app,
        "POST",
        "/api-keys/generate",
        headers=[("Content-Type", "application/x-www-form-urlencoded")],
        body=b"",
    )
    assert status == 303
    location = headers["location"]
    parsed = urlparse(location)
    params = parse_qs(parsed.query)
    new_key_id = params["new_key_id"][0]

    status, _, body = call_app(
        patched_app.app,
        "GET",
        parsed.path,
        query=parsed.query,
    )
    assert status == 200
    html = body.decode()
    assert "New API key generated" in html
    assert "keys-card is-disabled" not in html
    snippet = html.split("New API key generated", 1)[1]
    start = snippet.index("<code>") + len("<code>")
    end = snippet.index("</code>", start)
    key_value = snippet[start:end]
    assert key_value
    listing = patched_app.API_KEYS.list_keys()
    assert len(listing) == 1
    assert key_value.startswith(listing[0]["prefix"])
    assert listing[0]["id"] == new_key_id
    assert patched_app.API_KEYS.verify(key_value) is True
    table_section = html.split("<tbody>", 1)[1]
    assert key_value[:8] in table_section


def test_api_requests_require_key_when_enabled(patched_app):
    payload = {
        "video_url": "https://example.com/video.mp4",
        "duration_ms": 1000,
        "width": 640,
        "height": 360,
        "fps": 24,
    }

    status, _, body = call_app(
        patched_app.app,
        "POST",
        "/compose/from-urls",
        headers=[("content-type", "application/json")],
        body=json.dumps(payload).encode("utf-8"),
        query="as_json=true",
    )
    assert status == 200
    result = json.loads(body.decode())
    assert result["ok"] is True

    toggle_body = urlencode({"require_api_key": "true"}).encode()
    status, headers, _ = call_app(
        patched_app.app,
        "POST",
        "/settings/api-auth",
        headers=[("Content-Type", "application/x-www-form-urlencoded")],
        body=toggle_body,
    )
    assert status == 303
    assert headers["location"].startswith("/settings?message=")

    status, _, body = call_app(
        patched_app.app,
        "POST",
        "/compose/from-urls",
        headers=[("content-type", "application/json")],
        body=json.dumps(payload).encode("utf-8"),
        query="as_json=true",
    )
    assert status == 401
    error_payload = json.loads(body.decode())
    assert error_payload["error"] == "api_key_required"

    status, headers, _ = call_app(
        patched_app.app,
        "POST",
        "/api-keys/generate",
        headers=[("Content-Type", "application/x-www-form-urlencoded")],
        body=b"",
    )
    location = headers["location"]
    parsed_location = urlparse(location)
    status, _, body = call_app(
        patched_app.app,
        "GET",
        parsed_location.path,
        query=parsed_location.query,
    )
    html = body.decode()
    snippet = html.split("New API key generated", 1)[1]
    start = snippet.index("<code>") + len("<code>")
    end = snippet.index("</code>", start)
    key_value = snippet[start:end]

    status, _, body = call_app(
        patched_app.app,
        "POST",
        "/compose/from-urls",
        headers=[
            ("content-type", "application/json"),
            ("X-API-Key", key_value),
        ],
        body=json.dumps(payload).encode("utf-8"),
        query="as_json=true",
    )
    assert status == 200
    data = json.loads(body.decode())
    assert data["ok"] is True
    listing = patched_app.API_KEYS.list_keys()
    assert listing and listing[0]["last_used"] is not None


def test_concurrent_requests_handle_independent_state(patched_app):
    day_dir = patched_app.PUBLIC_DIR / "20240201"
    day_dir.mkdir(parents=True, exist_ok=True)
    (day_dir / "file.txt").write_text("data", encoding="utf-8")

    log_dir = patched_app.LOGS_DIR / "20240201"
    log_dir.mkdir(parents=True, exist_ok=True)
    (log_dir / "event.log").write_text("entry", encoding="utf-8")

    async def run_requests():
        downloads_task = _call_app(patched_app.app, "GET", "/downloads")
        logs_task = _call_app(patched_app.app, "GET", "/logs")
        health_task = _call_app(patched_app.app, "GET", "/health")
        return await asyncio.gather(downloads_task, logs_task, health_task)

    responses = asyncio.run(run_requests())

    assert [status for status, _, _ in responses] == [200, 200, 200]
    downloads_html = responses[0][2].decode()
    assert "file.txt" in downloads_html
    logs_html = responses[1][2].decode()
    assert "event.log" in logs_html


def test_download_to_clears_range_header_when_resume_not_supported(app_module, tmp_path):
    dest = tmp_path / "asset.bin"
    dest.write_bytes(b"abcd")

    client = StubAsyncClient(
        head_responses=[StubHeadResponse(length=6), StubHeadResponse(length=6)],
        stream_responses=[
            StubStreamResponse(status_code=200, chunks=[b"one", b"two"], fail_after_first_chunk=True),
            StubStreamResponse(status_code=200, chunks=[b"onetwo"], fail_after_first_chunk=False),
        ],
    )

    asyncio.run(
        app_module._download_to(
            "https://example.com/file",
            dest,
            headers={"Authorization": "token"},
            max_retries=2,
            chunk_size=3,
            client=client,
        )
    )

    assert len(client.stream_calls) == 2
    assert "Range" in client.stream_calls[0]
    assert "Range" not in client.stream_calls[1]
    assert len(client.head_calls) == 2
    assert "Range" not in client.head_calls[0]
    assert dest.read_bytes() == b"onetwo"


def test_download_to_scales_timeout_and_disk_check(app_module, monkeypatch, tmp_path):
    dest = tmp_path / "large.bin"

    total_bytes = 70 * 1024 * 1024
    disk_checks = []

    def fake_check_disk_space(path: Path, required_mb: int = 0):
        path.mkdir(parents=True, exist_ok=True)
        disk_checks.append(required_mb)

    monkeypatch.setattr(app_module, "check_disk_space", fake_check_disk_space)
    client = StubAsyncClient(
        head_responses=[StubHeadResponse(length=total_bytes)],
        stream_responses=[
            StubStreamResponse(
                status_code=200,
                chunks=[b"x" * (1024 * 1024)] * 70,
                fail_after_first_chunk=False,
                headers={"content-length": str(total_bytes)},
            )
        ],
    )

    asyncio.run(
        app_module._download_to(
            "https://example.com/large.bin",
            dest,
            chunk_size=1024 * 1024,
            client=client,
        )
    )

    assert client.stream_timeouts == [700]
    assert disk_checks[-1] == 70


def test_download_to_raises_when_partial_cannot_reset(app_module, monkeypatch, tmp_path):
    dest = tmp_path / "asset.bin"
    dest.write_bytes(b"abcd")

    original_unlink = Path.unlink

    def fake_unlink(self, missing_ok=False):
        if self == dest:
            raise PermissionError("cannot remove")
        return original_unlink(self, missing_ok=missing_ok)

    monkeypatch.setattr(Path, "unlink", fake_unlink)

    client = StubAsyncClient(
        head_responses=[StubHeadResponse(length=8)],
        stream_responses=[
            StubStreamResponse(status_code=200, chunks=[b"efgh"], fail_after_first_chunk=False),
        ],
    )

    with pytest.raises(app_module.HTTPException) as exc:
        asyncio.run(
            app_module._download_to(
                "https://example.com/file",
                dest,
                headers=None,
                max_retries=1,
                chunk_size=4,
                client=client,
            )
        )

    assert exc.value.status_code == 500


def test_download_to_appends_when_resume_supported(app_module, tmp_path):
    dest = tmp_path / "resume.bin"
    dest.write_bytes(b"old")

    client = StubAsyncClient(
        head_responses=[StubHeadResponse(length=6)],
        stream_responses=[
            StubStreamResponse(
                status_code=206,
                chunks=[b"new"],
                fail_after_first_chunk=False,
                headers={"content-length": "3"},
            )
        ],
    )

    asyncio.run(
        app_module._download_to(
            "https://example.com/resume.bin",
            dest,
            chunk_size=1024,
            client=client,
        )
    )

    assert client.stream_calls[0]["Range"] == "bytes=3-"
    assert dest.read_bytes() == b"oldnew"


def test_download_to_removes_partial_before_full_restart(app_module, monkeypatch, tmp_path):
    dest = tmp_path / "resume.bin"
    dest.write_bytes(b"stale-data")

    unlink_calls: list[Path] = []
    original_unlink = app_module.Path.unlink

    def spy_unlink(self, *args, **kwargs):
        if self == dest:
            unlink_calls.append(self)
        return original_unlink(self, *args, **kwargs)

    monkeypatch.setattr(app_module.Path, "unlink", spy_unlink, raising=False)

    client = StubAsyncClient(
        head_responses=[StubHeadResponse(length=6)],
        stream_responses=[
            StubStreamResponse(
                status_code=200,
                chunks=[b"result"],
                headers={"content-length": "6"},
            )
        ],
    )

    asyncio.run(
        app_module._download_to(
            "https://example.com/resume.bin",
            dest,
            chunk_size=1024,
            client=client,
        )
    )

    assert client.stream_calls[0]["Range"] == f"bytes={len('stale-data')}-"
    assert unlink_calls == [dest]
    assert dest.read_bytes() == b"result"


def test_download_to_fails_on_unexpected_resume_status(app_module, tmp_path):
    dest = tmp_path / "resume.bin"
    dest.write_bytes(b"old")

    client = StubAsyncClient(
        head_responses=[StubHeadResponse(length=6)],
        stream_responses=[
            StubStreamResponse(
                status_code=204,
                chunks=[],
                headers={"content-length": "0"},
            )
        ],
    )

    with pytest.raises(HTTPException) as exc:
        asyncio.run(
            app_module._download_to(
                "https://example.com/resume.bin",
                dest,
                chunk_size=1024,
                client=client,
            )
        )

    assert exc.value.status_code == 502
    assert dest.read_bytes() == b"old"


def test_download_to_removes_partial_file_on_final_failure(app_module, tmp_path):
    dest = tmp_path / "broken.bin"
    dest.write_bytes(b"partial")

    client = StubAsyncClient(
        head_responses=[StubHeadResponse(length=0)],
        stream_responses=[
            StubStreamResponse(
                status_code=200,
                chunks=[b"partial"],
                fail_after_first_chunk=True,
                headers={"content-length": "7"},
            )
        ],
    )

    with pytest.raises(RuntimeError):
        asyncio.run(
            app_module._download_to(
                "https://example.com/broken.bin",
                dest,
                max_retries=1,
                client=client,
            )
        )

    assert not dest.exists()


def test_cleanup_old_public_handles_missing_file_race(app_module, monkeypatch):
    day_dir = app_module.PUBLIC_DIR / "20200101"
    day_dir.mkdir(parents=True, exist_ok=True)
    old_file = day_dir / "stale.mp4"
    old_file.write_text("data", encoding="utf-8")

    cutoff = time.time() - ((app_module.RETENTION_DAYS + 1) * 86400)
    os.utime(old_file, (cutoff, cutoff))

    original_unlink = app_module.os.unlink
    triggered = {"count": 0}

    def flaky_unlink(path, *args, **kwargs):
        if Path(path) == old_file and not triggered["count"]:
            triggered["count"] = 1
            if old_file.exists():
                original_unlink(path)
            raise FileNotFoundError("already removed")
        return original_unlink(path, *args, **kwargs)

    monkeypatch.setattr(app_module.os, "unlink", flaky_unlink)

    app_module.cleanup_old_public(days=app_module.RETENTION_DAYS)

    assert triggered["count"] == 1
    assert not old_file.exists()


def test_logs_page_lists_entries(patched_app):
    day_dir = patched_app.LOGS_DIR / "20240102"
    day_dir.mkdir(parents=True, exist_ok=True)
    (day_dir / "20240102_log.log").write_text("log line", encoding="utf-8")

    status, _, body = call_app(patched_app.app, "GET", "/logs")
    assert status == 200
    html = body.decode()
    assert "20240102" in html
    assert "View" in html


def test_logs_page_escapes_malicious_names(patched_app):
    day_dir = patched_app.LOGS_DIR / "20240106"
    day_dir.mkdir(parents=True, exist_ok=True)
    filename = "<<script>>.log"
    (day_dir / filename).write_text("entry", encoding="utf-8")

    status, _, body = call_app(patched_app.app, "GET", "/logs")
    assert status == 200
    html = body.decode()
    assert "&lt;&lt;script&gt;&gt;.log" in html
    assert "<<script" not in html


def test_view_log_endpoint(patched_app):
    day_dir = patched_app.LOGS_DIR / "20240103"
    day_dir.mkdir(parents=True, exist_ok=True)
    (day_dir / "operation.log").write_text("details", encoding="utf-8")

    status, _, body = call_app(
        patched_app.app,
        "GET",
        "/logs/view",
        query=urlencode({"path": "20240103/operation.log"}),
    )
    assert status == 200
    assert body.decode() == "details"

    status, _, _ = call_app(
        patched_app.app,
        "GET",
        "/logs/view",
        query=urlencode({"path": "../outside.log"}),
    )
    assert status == 403


def test_view_log_rejects_large_files(patched_app):
    day_dir = patched_app.LOGS_DIR / "20240107"
    day_dir.mkdir(parents=True, exist_ok=True)
    large = day_dir / "big.log"
    large.write_bytes(b"x" * (11 * 1024 * 1024))

    status, _, _ = call_app(
        patched_app.app,
        "GET",
        "/logs/view",
        query=urlencode({"path": "20240107/big.log"}),
    )
    assert status == 413


def test_ffmpeg_page_includes_version_and_auto_refresh(patched_app):
    patched_app.APP_LOG_FILE.write_text("<script>alert(1)</script>\nsecond line", encoding="utf-8")

    status, _, body = call_app(
        patched_app.app,
        "GET",
        "/ffmpeg",
        query=urlencode({"auto_refresh": 120}),
    )
    assert status == 200
    html = body.decode()
    assert "ffmpeg version" in html
    assert "Auto-refreshing every 60 seconds" in html
    assert "&lt;script&gt;alert(1)&lt;/script&gt;" in html


def test_documentation_page_loads(patched_app):
    status, _, body = call_app(patched_app.app, "GET", "/documentation")
    assert status == 200
    assert "FFAPI Ultimate - API Documentation" in body.decode()


def test_openapi_schema_includes_server(patched_app):
    schema = patched_app.app.openapi()
    assert schema["info"]["title"] == "FFAPI Ultimate"
    assert schema["servers"][0]["url"] == "http://example.com"


def test_health_endpoint(patched_app):
    # Prime metrics with a request so the tracker has data
    call_app(patched_app.app, "GET", "/downloads")

    status, _, body = call_app(patched_app.app, "GET", "/health")
    assert status == 200
    payload = json.loads(body.decode())
    assert payload["ok"] is True
    assert "disk" in payload and "public" in payload["disk"]
    assert payload["ffmpeg"]["available"] is True
    assert "recent_success_rate" in payload["operations"]
    assert payload["operations"]["recent_success_rate"]["successes"] >= 0


def test_metrics_dashboard_reports_activity(patched_app):
    call_app(patched_app.app, "GET", "/downloads")
    call_app(patched_app.app, "GET", "/logs")

    status, _, body = call_app(patched_app.app, "GET", "/metrics")
    assert status == 200
    html = body.decode()
    assert "Operational Metrics" in html
    assert "/downloads" in html
    assert "Recent Success Rate" in html


def test_prometheus_metrics_export(patched_app):
    call_app(patched_app.app, "GET", "/downloads")
    status, headers, body = call_app(patched_app.app, "GET", "/metrics/prometheus")
    assert status == 200
    assert headers["content-type"].startswith("text/plain")
    text = body.decode()
    assert "ffapi_requests_total" in text
    assert "ffapi_queue_current_requests" in text


def test_rate_limiter_blocks_excess_requests(patched_app, monkeypatch):
    monkeypatch.setattr(patched_app.RATE_LIMITER, "_rpm", 1)
    patched_app.RATE_LIMITER.reset()

    first_status, _, _ = call_app(patched_app.app, "GET", "/health")
    assert first_status == 200

    second_status, _, body = call_app(patched_app.app, "GET", "/health")
    assert second_status == 429
    assert b"Too Many Requests" in body

    patched_app.RATE_LIMITER.reset()


def test_rate_limited_requests_complete_once(patched_app, monkeypatch):
    patched_app.METRICS.reset()
    patched_app.RATE_LIMITER.reset()

    original_completed = patched_app.METRICS.request_completed
    mock_completed = Mock(side_effect=lambda: original_completed())

    monkeypatch.setattr(patched_app.RATE_LIMITER, "check", lambda identifier: False)
    monkeypatch.setattr(patched_app.METRICS, "request_completed", mock_completed)

    status, _, body = call_app(patched_app.app, "GET", "/health")
    assert status == 429
    assert b"Too Many Requests" in body
    assert mock_completed.call_count == 1

    snapshot = patched_app.METRICS.snapshot()
    assert snapshot["queue"]["current"] == 0

    patched_app.RATE_LIMITER.reset()


def test_async_compose_job_lifecycle(patched_app):
    payload = {
        "video_url": "https://example.com/video.mp4",
        "audio_url": "https://example.com/audio.mp3",
        "duration_ms": 5000,
        "width": 640,
        "height": 360,
        "fps": 24,
    }

    with patched_app.JOBS_LOCK:
        patched_app.JOBS.clear()

    status, _, body = call_app(
        patched_app.app,
        "POST",
        "/compose/from-urls/async",
        headers=[("content-type", "application/json")],
        body=json.dumps(payload).encode("utf-8"),
    )
    assert status == 200
    data = json.loads(body.decode())
    job_id = data["job_id"]

    job = patched_app.ComposeFromUrlsJob(**payload)
    asyncio.run(patched_app._process_compose_from_urls_job(job_id, job))

    status, _, body = call_app(patched_app.app, "GET", f"/jobs/{job_id}")
    assert status == 200
    job_info = json.loads(body.decode())
    assert job_info["status"] == "finished"
    assert job_info["result"]["file_url"].startswith("http://example.com/")
    assert job_info["message"] == "Completed"
    assert "thumbnail" in job_info["result"]
    assert job_info["result"]["thumbnail"] is None
    assert isinstance(job_info.get("history"), list)
    assert any(entry["message"] == "Completed" for entry in job_info["history"])
    assert any(entry["message"] == "Rendering composition" for entry in job_info["history"])
    assert patched_app.publish_file.last_duration == 5000


def test_async_compose_job_sends_webhook(monkeypatch, patched_app):
    calls = []

    async def fake_notify(webhook_url, **kwargs):
        calls.append({"webhook_url": webhook_url, **kwargs})

    monkeypatch.setattr(patched_app, "_notify_webhook", fake_notify)

    job = patched_app.ComposeFromUrlsJob(
        video_url="https://example.com/video.mp4",
        duration_ms=1000,
        webhook_url="https://example.com/webhook",
        webhook_headers={"X-Key": "value"},
    )

    asyncio.run(patched_app._process_compose_from_urls_job("job-webhook", job))

    assert len(calls) == 1
    payload = calls[0]
    assert payload["webhook_url"] == "https://example.com/webhook"
    assert payload["status"] == "finished"
    assert payload["headers"] == {"X-Key": "value"}
    assert payload["result"]["file_url"].startswith("http://example.com/")


def test_async_compose_job_failed_webhook(monkeypatch, patched_app):
    calls = []

    async def fake_notify(webhook_url, **kwargs):
        calls.append(kwargs["status"])

    async def failing_impl(job, reporter):
        raise patched_app.HTTPException(status_code=422, detail="bad input")

    monkeypatch.setattr(patched_app, "_notify_webhook", fake_notify)
    monkeypatch.setattr(patched_app, "_compose_from_urls_impl", failing_impl)

    job = patched_app.ComposeFromUrlsJob(
        video_url="https://example.com/video.mp4",
        duration_ms=1000,
        webhook_url="https://example.com/webhook",
    )

    asyncio.run(patched_app._process_compose_from_urls_job("job-fail", job))

    assert calls == ["failed"]


def test_notify_webhook_retries(monkeypatch, patched_app):
    attempts: List[int] = []
    sleep_delays: List[float] = []

    class FakeResponse:
        def __init__(self, status_code: int) -> None:
            self.status_code = status_code

        def raise_for_status(self) -> None:
            if self.status_code >= 400:
                raise RuntimeError("boom")

    class FakeClient:
        async def post(self, url, json=None, headers=None, timeout=None):
            attempts.append(len(attempts) + 1)
            if len(attempts) < 3:
                raise RuntimeError("fail")
            return FakeResponse(200)

        async def aclose(self) -> None:
            return None

    async def fake_sleep(delay: float) -> None:
        sleep_delays.append(delay)

    monkeypatch.setattr(patched_app, "_make_async_client", lambda: FakeClient())
    monkeypatch.setattr(patched_app.asyncio, "sleep", fake_sleep)

    asyncio.run(
        patched_app._notify_webhook(
            "https://example.com/webhook",
            job_id="job-retry",
            status="finished",
            result={"file_url": "http://example.com/file.mp4"},
        )
    )

    assert attempts == [1, 2, 3]
    assert sleep_delays == [1.0, 2.0]


def test_jobs_history_page_lists_jobs(patched_app):
    with patched_app.JOBS_LOCK:
        patched_app.JOBS.clear()
        patched_app.JOBS["job123"] = {
            "status": "finished",
            "created": time.time(),
            "duration_ms": 2500,
            "message": "Completed",
            "history": [],
        }

    status, _, body = call_app(patched_app.app, "GET", "/jobs")
    assert status == 200
    text = body.decode()
    assert "Job History" in text
    assert "job123" in text
    assert "Page 1 of 1" in text


def test_static_file_serving(patched_app):
    day_dir = patched_app.PUBLIC_DIR / "20240104"
    day_dir.mkdir(parents=True, exist_ok=True)
    file_path = day_dir / "asset.txt"
    file_path.write_text("asset", encoding="utf-8")

    status, _, body = call_app(
        patched_app.app,
        "GET",
        f"/files/{day_dir.name}/{file_path.name}",
    )
    assert status == 200
    assert body.decode() == "asset"


def test_image_to_mp4_loop_as_json(patched_app):
    upload = UploadFile(
        file=io.BytesIO(b"\x89PNG\r\n\x1a\n"),
        filename="image.png",
        headers=Headers({"content-type": "image/png"}),
    )
    result = asyncio.run(patched_app.image_to_mp4_loop(upload, duration=5, as_json=True))
    assert result["ok"] is True
    assert result["file_url"].startswith("http://example.com/")


def test_image_upload_rejects_oversized_file(patched_app):
    big_payload = b"\x89PNG\r\n\x1a\n" + b"0" * (patched_app.MAX_FILE_SIZE_BYTES + 1)
    upload = UploadFile(
        file=io.BytesIO(big_payload),
        filename="too-big.png",
        headers=Headers({"content-type": "image/png"}),
    )
    with pytest.raises(patched_app.HTTPException) as exc:
        asyncio.run(patched_app.image_to_mp4_loop(upload, duration=5, as_json=True))
    assert exc.value.status_code == 413


def test_stream_upload_rejects_declared_length(patched_app, tmp_path):
    too_large = str(patched_app.MAX_FILE_SIZE_BYTES + 1024)
    upload = UploadFile(
        file=io.BytesIO(b"123"),
        filename="clip.mp4",
        headers=Headers({"content-type": "video/mp4", "content-length": too_large}),
    )
    destination = tmp_path / "clip.mp4"
    with pytest.raises(patched_app.HTTPException) as exc:
        asyncio.run(patched_app.stream_upload_to_path(upload, destination))
    assert exc.value.status_code == 413
    assert not destination.exists()


def test_compose_from_binaries_with_all_inputs(patched_app):
    video = UploadFile(file=io.BytesIO(b"video"), filename="video.mp4", headers=Headers({"content-type": "video/mp4"}))
    audio = UploadFile(file=io.BytesIO(b"audio"), filename="audio.mp3", headers=Headers({"content-type": "audio/mpeg"}))
    bgm = UploadFile(file=io.BytesIO(b"music"), filename="music.mp3", headers=Headers({"content-type": "audio/mpeg"}))
    result = asyncio.run(
        patched_app.compose_from_binaries(
            video=video,
            audio=audio,
            bgm=bgm,
            duration_ms=15000,
            width=1280,
            height=720,
            fps=24,
            bgm_volume=0.5,
            as_json=True,
        )
    )
    assert result["ok"] is True


def test_compose_from_binaries_rejects_non_video_upload(patched_app):
    bad_video = UploadFile(file=io.BytesIO(b"video"), filename="video.txt", headers=Headers({"content-type": "text/plain"}))
    with pytest.raises(patched_app.HTTPException) as exc:
        asyncio.run(
            patched_app.compose_from_binaries(
                video=bad_video,
                audio=None,
                bgm=None,
                duration_ms=15000,
                width=1280,
                height=720,
                fps=24,
                bgm_volume=0.5,
                as_json=True,
            )
        )
    assert exc.value.status_code == 400


def test_compose_from_binaries_rejects_oversized_audio(patched_app):
    video = UploadFile(
        file=io.BytesIO(b"video"),
        filename="video.mp4",
        headers=Headers({"content-type": "video/mp4"}),
    )
    big_audio_payload = b"audio" + b"0" * (patched_app.MAX_FILE_SIZE_BYTES + 5)
    audio = UploadFile(
        file=io.BytesIO(big_audio_payload),
        filename="audio.mp3",
        headers=Headers({"content-type": "audio/mpeg"}),
    )

    with pytest.raises(patched_app.HTTPException) as exc:
        asyncio.run(
            patched_app.compose_from_binaries(
                video=video,
                audio=audio,
                bgm=None,
                duration_ms=15000,
                width=1280,
                height=720,
                fps=24,
                bgm_volume=0.5,
                as_json=True,
            )
        )

    assert exc.value.status_code == 413


def test_compose_from_binaries_rejects_invalid_audio_type(patched_app):
    video = UploadFile(file=io.BytesIO(b"video"), filename="video.mp4", headers=Headers({"content-type": "video/mp4"}))
    bad_audio = UploadFile(file=io.BytesIO(b"audio"), filename="audio.txt", headers=Headers({"content-type": "text/plain"}))
    with pytest.raises(patched_app.HTTPException) as exc:
        asyncio.run(
            patched_app.compose_from_binaries(
                video=video,
                audio=bad_audio,
                bgm=None,
                duration_ms=15000,
                width=1280,
                height=720,
                fps=24,
                bgm_volume=0.5,
                as_json=True,
            )
        )
    assert exc.value.status_code == 400


def test_compose_from_binaries_rejects_invalid_bgm_type(patched_app):
    video = UploadFile(file=io.BytesIO(b"video"), filename="video.mp4", headers=Headers({"content-type": "video/mp4"}))
    bgm = UploadFile(file=io.BytesIO(b"music"), filename="music.txt", headers=Headers({"content-type": "application/octet-stream"}))
    with pytest.raises(patched_app.HTTPException) as exc:
        asyncio.run(
            patched_app.compose_from_binaries(
                video=video,
                audio=None,
                bgm=bgm,
                duration_ms=15000,
                width=1280,
                height=720,
                fps=24,
                bgm_volume=0.5,
                as_json=True,
            )
        )
    assert exc.value.status_code == 400


def test_compose_from_urls_as_json(patched_app):
    job = patched_app.ComposeFromUrlsJob(
        video_url="https://example.com/video.mp4",
        audio_url="https://example.com/audio.mp3",
        bgm_url="https://example.com/music.mp3",
        duration_ms=10000,
        width=640,
        height=360,
        fps=25,
        bgm_volume=0.2,
    )
    result = asyncio.run(patched_app.compose_from_urls(job, as_json=True))
    assert result["ok"] is True


def test_compose_from_urls_accepts_duration_seconds(patched_app):
    job = patched_app.ComposeFromUrlsJob(
        video_url="https://example.com/video.mp4",
        duration=12.5,
    )
    assert job.duration_ms == 12500


def test_compose_from_urls_parses_string_duration(patched_app):
    job = patched_app.ComposeFromUrlsJob(
        video_url="https://example.com/video.mp4",
        duration="2.75",
    )
    assert job.duration_ms == 2750


def test_compose_from_urls_rejects_both_duration_fields(patched_app):
    with pytest.raises(ValidationError):
        patched_app.ComposeFromUrlsJob(
            video_url="https://example.com/video.mp4",
            duration=5.0,
            duration_ms=5000,
        )


def test_compose_from_urls_rejects_non_http_urls(patched_app):
    with pytest.raises(ValidationError):
        patched_app.ComposeFromUrlsJob(video_url="ftp://example.com/video.mp4")


def test_compose_from_urls_rejects_invalid_duration(patched_app):
    with pytest.raises(ValidationError):
        patched_app.ComposeFromUrlsJob(
            video_url="https://example.com/video.mp4",
            duration_ms=0,
        )


def test_compose_from_urls_webhook_configuration(patched_app):
    job = patched_app.ComposeFromUrlsJob(
        video_url="https://example.com/video.mp4",
        webhook_url="https://example.com/hook",
        webhook_headers={"X-Test": 1, "  Extra  ": "value"},
    )
    assert str(job.webhook_url) == "https://example.com/hook"
    assert job.webhook_headers == {"X-Test": "1", "Extra": "value"}


def test_compose_from_urls_rejects_empty_webhook_header(patched_app):
    with pytest.raises(ValidationError):
        patched_app.ComposeFromUrlsJob(
            video_url="https://example.com/video.mp4",
            webhook_url="https://example.com/hook",
            webhook_headers={" ": "value"},
        )


def test_compose_from_tracks_as_json(patched_app):
    job = patched_app.TracksComposeJob(
        tracks=[
            patched_app.Track(
                id="video1",
                type="video",
                keyframes=[patched_app.Keyframe(url="https://example.com/video.mp4", timestamp=0, duration=5000)],
            ),
            patched_app.Track(
                id="audio1",
                type="audio",
                keyframes=[patched_app.Keyframe(url="https://example.com/audio.mp3", timestamp=0, duration=5000)],
            ),
        ],
        width=1280,
        height=720,
        fps=30,
    )
    result = asyncio.run(patched_app.compose_from_tracks(job, as_json=True))
    assert result["ok"] is True


def test_compose_from_tracks_requires_valid_video_keyframe(patched_app):
    job = patched_app.TracksComposeJob(
        tracks=[
            patched_app.Track(
                id="video1",
                type="video",
                keyframes=[patched_app.Keyframe(url=None, timestamp=0, duration=5000)],
            )
        ],
        width=640,
        height=360,
        fps=24,
    )
    with pytest.raises(patched_app.HTTPException) as exc:
        asyncio.run(patched_app.compose_from_tracks(job, as_json=True))
    assert exc.value.status_code == 400


def test_keyframe_rejects_non_http_url(patched_app):
    with pytest.raises(ValidationError):
        patched_app.Keyframe(url="ftp://example.com/video.mp4", timestamp=0, duration=1000)


def test_compose_from_tracks_rejects_non_positive_duration(patched_app):
    job = patched_app.TracksComposeJob(
        tracks=[
            patched_app.Track(
                id="video1",
                type="video",
                keyframes=[patched_app.Keyframe(url="https://example.com/video.mp4", timestamp=0, duration=0)],
            )
        ],
        width=640,
        height=360,
        fps=24,
    )
    with pytest.raises(patched_app.HTTPException) as exc:
        asyncio.run(patched_app.compose_from_tracks(job, as_json=True))
    assert exc.value.status_code == 400


def test_compose_from_tracks_concats_multiple_videos(patched_app, monkeypatch):
    download_calls: List[str] = []

    async def tracking_download(url: str, dest: Path, headers=None, max_retries: int = 3, chunk_size: int = 1024 * 1024, client=None):
        download_calls.append(url)
        dest.parent.mkdir(parents=True, exist_ok=True)
        dest.write_bytes(f"content from {url}".encode("utf-8"))

    commands: List[List[str]] = []

    def fake_run_ffmpeg_with_timeout(
        cmd: List[str],
        log_handle,
        *,
        progress_parser=None,
    ) -> int:
        commands.append(list(cmd))
        output_path = None
        for token in reversed(cmd):
            if isinstance(token, str) and not token.startswith("-"):
                output_path = Path(token)
                break
        if output_path is not None:
            output_path.parent.mkdir(parents=True, exist_ok=True)
            output_path.write_bytes(b"video")
        log_handle.write("log\n")
        if progress_parser is not None:
            progress_parser("frame= 120 fps=30.0 q=28.0 size=1024kB time=00:00:04.00")
        return 0

    monkeypatch.setattr(patched_app, "_download_to", tracking_download)
    monkeypatch.setattr(patched_app, "run_ffmpeg_with_timeout", fake_run_ffmpeg_with_timeout)

    job = patched_app.TracksComposeJob(
        tracks=[
            patched_app.Track(
                id="video1",
                type="video",
                keyframes=[
                    patched_app.Keyframe(url="https://example.com/video1.mp4", timestamp=0, duration=4000),
                    patched_app.Keyframe(url="https://example.com/video2.mp4", timestamp=4000, duration=4000),
                ],
            ),
            patched_app.Track(
                id="audio1",
                type="audio",
                keyframes=[patched_app.Keyframe(url="https://example.com/audio.mp3", timestamp=0, duration=8000)],
            ),
        ],
        width=640,
        height=360,
        fps=24,
    )

    result = asyncio.run(patched_app.compose_from_tracks(job, as_json=True))

    assert result["ok"] is True
    assert download_calls[:2] == [
        "https://example.com/video1.mp4",
        "https://example.com/video2.mp4",
    ]
    assert any("concat" in cmd for cmd in commands)


def test_escape_ffmpeg_concat_path_handles_quotes(patched_app):
    sample = Path("/tmp/weird name with 'quote.mp4")
    escaped = patched_app._escape_ffmpeg_concat_path(sample)
    assert escaped == "/tmp/weird name with '\\''quote.mp4"


def test_cleanup_old_jobs_removes_finished_entries(patched_app):
    with patched_app.JOBS_LOCK:
        patched_app.JOBS.clear()
        patched_app.JOBS["expired"] = {
            "status": "finished",
            "created": time.time() - 7200,
        }
        patched_app.JOBS["active"] = {
            "status": "processing",
            "created": time.time() - 7200,
        }
        patched_app.JOBS["fresh"] = {
            "status": "finished",
            "created": time.time(),
        }
        patched_app.JOBS["malformed"] = {"status": "finished"}

    patched_app.cleanup_old_jobs(max_age_seconds=3600)

    with patched_app.JOBS_LOCK:
        assert "expired" not in patched_app.JOBS
        assert "fresh" in patched_app.JOBS
        assert "active" in patched_app.JOBS
        assert "malformed" not in patched_app.JOBS


def test_cleanup_old_jobs_enforces_max_total(patched_app):
    with patched_app.JOBS_LOCK:
        patched_app.JOBS.clear()
        now = time.time()
        for index in range(8):
            patched_app.JOBS[f"finished-{index}"] = {
                "status": "finished",
                "created": now - index,
            }
        patched_app.JOBS["processing"] = {"status": "processing", "created": now}

    patched_app.cleanup_old_jobs(max_age_seconds=10_000, max_total_jobs=5)

    with patched_app.JOBS_LOCK:
        assert "processing" in patched_app.JOBS
        assert len(patched_app.JOBS) <= 5
        remaining_finished = [
            key for key, data in patched_app.JOBS.items() if data.get("status") == "finished"
        ]
        assert set(remaining_finished).issubset({"finished-0", "finished-1", "finished-2", "finished-3"})


def test_rate_limiter_cleanup_removes_stale_identifiers(patched_app):
    limiter = patched_app.RateLimiter(10)
    now = patched_app.datetime.now(patched_app.timezone.utc)
    stale = now - patched_app.timedelta(minutes=120)

    with limiter._lock:
        limiter._limits["old"] = [stale]
        limiter._limits["fresh"] = [now]

    limiter.cleanup_old_identifiers(max_age_minutes=60)

    with limiter._lock:
        assert "old" not in limiter._limits
        assert "fresh" in limiter._limits


def test_video_concat_from_urls_as_json(patched_app):
    job = patched_app.ConcatJob(
        clips=[
            "https://example.com/a.mp4",
            "https://example.com/b.mp4",
        ],
        width=1920,
        height=1080,
        fps=30,
    )
    result = asyncio.run(patched_app.video_concat_from_urls(job, as_json=True))
    assert result["ok"] is True


def test_video_concat_job_rejects_empty_list(patched_app):
    with pytest.raises(ValidationError):
        patched_app.ConcatJob(clips=[], width=1920, height=1080, fps=30)


def test_video_concat_alias_requires_clips(patched_app):
    job = patched_app.ConcatAliasJob(width=640, height=360, fps=30)
    with pytest.raises(patched_app.HTTPException) as exc:
        asyncio.run(patched_app.video_concat_alias(job))
    assert exc.value.status_code == 422


def test_video_concat_alias_with_clips(patched_app):
    job = patched_app.ConcatAliasJob(clips=["https://example.com/1.mp4", "https://example.com/2.mp4"], width=854, height=480, fps=30)
    result = asyncio.run(patched_app.video_concat_alias(job, as_json=True))
    assert result["ok"] is True


def test_run_ffmpeg_command_returns_outputs(patched_app):
    job = patched_app.RendiJob(
        input_files={"video": "https://example.com/input.mp4"},
        output_files={"out": "result.mp4"},
        ffmpeg_command="-i {{video}} -t 1 {{out}}",
    )
    result = asyncio.run(patched_app.run_rendi(job))
    assert result["ok"] is True
    assert "out" in result["outputs"]


def test_rendi_job_rejects_non_http_inputs(patched_app):
    with pytest.raises(ValidationError):
        patched_app.RendiJob(
            input_files={"video": "file:///tmp/input.mp4"},
            output_files={"out": "result.mp4"},
            ffmpeg_command="-i {{video}} -t 1 {{out}}",
        )


def test_run_ffmpeg_command_rejects_dangerous_patterns(patched_app):
    job = patched_app.RendiJob(
        input_files={"video": "https://example.com/input.mp4"},
        output_files={"out": "result.mp4"},
        ffmpeg_command="-i {{video}} -f lavfi -t 1 {{out}}",
    )
    with pytest.raises(patched_app.HTTPException) as exc:
        asyncio.run(patched_app.run_rendi(job))
    assert exc.value.status_code == 400
    assert exc.value.detail["error"] == "forbidden_pattern"


def test_run_ffmpeg_command_requires_duration_limit(patched_app, monkeypatch):
    monkeypatch.setattr(patched_app, "REQUIRE_DURATION_LIMIT", True)
    job = patched_app.RendiJob(
        input_files={"video": "https://example.com/input.mp4"},
        output_files={"out": "result.mp4"},
        ffmpeg_command="-i {{video}} {{out}}",
    )
    with pytest.raises(patched_app.HTTPException) as exc:
        asyncio.run(patched_app.run_rendi(job))
    assert exc.value.status_code == 400
    assert exc.value.detail["error"] == "missing_limit"


def test_run_ffmpeg_with_timeout_handles_spawn_failure(app_module, monkeypatch):
    def boom_popen(cmd, stdout=None, stderr=None, **kwargs):
        raise RuntimeError("spawn failed")

    monkeypatch.setattr(app_module.subprocess, "Popen", boom_popen)

    with pytest.raises(app_module.HTTPException) as exc:
        app_module.run_ffmpeg_with_timeout(["ffmpeg", "-i", "in.mp4", "out.mp4"], io.StringIO())

    assert exc.value.status_code == 500


def test_run_ffmpeg_with_timeout_handles_timeout(app_module, monkeypatch):
    wait_calls = []
    terminated = False
    killed = False

    class SlowProc:
        def __init__(self, cmd, stdout=None, stderr=None, **kwargs):
            self.cmd = cmd
            self._wait_count = 0
            self.stderr = io.StringIO("")

        def wait(self, timeout=None):
            self._wait_count += 1
            wait_calls.append(timeout)
            raise app_module.subprocess.TimeoutExpired(cmd=self.cmd, timeout=timeout)

        def terminate(self):
            nonlocal terminated
            terminated = True

        def kill(self):
            nonlocal killed
            killed = True

    def slow_popen(cmd, stdout=None, stderr=None, **kwargs):
        return SlowProc(cmd, stdout=stdout, stderr=stderr, **kwargs)

    monkeypatch.setattr(app_module.subprocess, "Popen", slow_popen)

    with pytest.raises(app_module.HTTPException) as exc:
        app_module.run_ffmpeg_with_timeout(["ffmpeg", "-i", "in.mp4", "out.mp4"], io.StringIO())

    assert exc.value.status_code == 504
    assert terminated is True
    assert killed is True
    assert wait_calls == [app_module.FFMPEG_TIMEOUT_SECONDS, 5, 1]


def test_probe_from_urls_returns_json(patched_app):
    job = patched_app.ProbeUrlJob(url="https://example.com/video.mp4", show_streams=True, count_frames=True)
    result = patched_app.probe_from_urls(job)
    assert result["format"]["format_name"] == "fake"


def test_probe_from_urls_uses_cache(monkeypatch, patched_app):
    calls = []

    def fake_run(cmd, *args, **kwargs):
        calls.append(list(cmd))
        payload = json.dumps({"format": {"format_name": "fake"}, "streams": []}).encode("utf-8")
        return DummyCompletedProcess(returncode=0, stdout=payload, stderr=b"")

    monkeypatch.setattr(patched_app.subprocess, "run", fake_run)
    patched_app.PROBE_CACHE.clear()

    job = patched_app.ProbeUrlJob(url="https://example.com/video.mp4", show_streams=True)
    first = patched_app.probe_from_urls(job)
    second = patched_app.probe_from_urls(job)

    assert first == second
    assert len(calls) == 1
    patched_app.PROBE_CACHE.clear()


def test_probe_from_binary_returns_json(patched_app):
    upload = UploadFile(file=io.BytesIO(b"binary"), filename="upload.mp4", headers=Headers({"content-type": "video/mp4"}))
    result = asyncio.run(patched_app.probe_from_binary(upload, show_format=True, show_streams=True))
    assert result["format"]["format_name"] == "fake"


def test_probe_public_returns_json(patched_app):
    day_dir = patched_app.PUBLIC_DIR / "20240105"
    day_dir.mkdir(parents=True, exist_ok=True)
    media = day_dir / "video.mp4"
    media.write_bytes(b"fake")

    result = patched_app.probe_public(
        rel=f"{day_dir.name}/{media.name}",
        show_format=True,
        show_streams=True,
    )
    assert result["format"]["format_name"] == "fake"


def test_probe_endpoints_use_timeout(patched_app, monkeypatch):
    captured: List[int | None] = []

    def fake_run(cmd, *args, **kwargs):
        captured.append(kwargs.get("timeout"))
        payload = json.dumps({"format": {"format_name": "fake"}, "streams": []}).encode("utf-8")
        return DummyCompletedProcess(returncode=0, stdout=payload, stderr=b"")

    monkeypatch.setattr(patched_app.subprocess, "run", fake_run)

    job = patched_app.ProbeUrlJob(url="https://example.com/video.mp4")
    patched_app.probe_from_urls(job)

    upload = UploadFile(file=io.BytesIO(b"binary"), filename="upload.mp4", headers=Headers({"content-type": "video/mp4"}))
    asyncio.run(patched_app.probe_from_binary(upload, show_format=True, show_streams=True))

    day_dir = patched_app.PUBLIC_DIR / "20240108"
    day_dir.mkdir(parents=True, exist_ok=True)
    media = day_dir / "clip.mp4"
    media.write_bytes(b"data")
    patched_app.probe_public(rel=f"{day_dir.name}/{media.name}", show_format=True, show_streams=True)

    assert captured == [60, 60, 60]
