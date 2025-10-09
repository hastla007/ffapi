import os, io, shlex, json, subprocess, random, string, shutil, time, asyncio, html, types, atexit, hmac, hashlib, secrets, base64, struct, math
from collections import Counter, defaultdict, deque
from contextvars import ContextVar
from datetime import datetime, timedelta, timezone
from dataclasses import dataclass
from pathlib import Path
from tempfile import TemporaryDirectory
from typing import Any, Dict, List, Optional, Literal, Tuple, Callable
from urllib.parse import urlencode, quote, parse_qs
from uuid import uuid4

import re
import threading

try:  # pragma: no cover - structlog may be unavailable in minimal environments
    import structlog
    from structlog.contextvars import bind_contextvars, clear_contextvars
except ModuleNotFoundError:  # pragma: no cover - fallback when structlog isn't installed
    structlog = None  # type: ignore

    def bind_contextvars(**_: Any) -> None:
        return None

    def clear_contextvars() -> None:
        return None

import importlib.util

try:  # pragma: no cover - optional dependency
    import bcrypt
except ModuleNotFoundError:  # pragma: no cover - test fallback
    bcrypt = None

if importlib.util.find_spec("httpx") is not None:
    import httpx  # type: ignore
    HAS_HTTPX = True
else:  # pragma: no cover - minimal environments without httpx
    HAS_HTTPX = False
    import requests

    class _HeadResponse:
        def __init__(self, response: requests.Response):
            self._response = response
            self.status_code = response.status_code
            self.headers = response.headers

        def raise_for_status(self) -> None:
            self._response.raise_for_status()


    class _StreamResponse(_HeadResponse):
        async def __aenter__(self):
            return self

        async def __aexit__(self, exc_type, exc, tb):
            self._response.close()
            return False

        async def aiter_bytes(self, chunk_size: int):
            iterator = self._response.iter_content(chunk_size)
            while True:
                try:
                    chunk = await asyncio.to_thread(next, iterator)
                except StopIteration:
                    break
                yield chunk


    class _AsyncClient:
        def __init__(self, follow_redirects: bool = True):
            self._session = requests.Session()
            self._session.trust_env = False
            self._follow = follow_redirects

        async def head(self, url: str, headers=None, timeout=None):
            response = await asyncio.to_thread(
                self._session.head,
                url,
                headers=headers,
                timeout=timeout,
                allow_redirects=self._follow,
            )
            return _HeadResponse(response)

        async def stream(self, method: str, url: str, headers=None, timeout=None):
            if method.upper() != "GET":
                raise ValueError("Only GET is supported by the fallback AsyncClient")
            response = await asyncio.to_thread(
                self._session.get,
                url,
                headers=headers,
                timeout=timeout,
                allow_redirects=self._follow,
                stream=True,
            )
            return _StreamResponse(response)

        async def post(self, url: str, json=None, headers=None, timeout=None):
            response = await asyncio.to_thread(
                self._session.post,
                url,
                headers=headers,
                json=json,
                timeout=timeout,
            )
            return _HeadResponse(response)

        async def aclose(self) -> None:
            await asyncio.to_thread(self._session.close)

    httpx = types.SimpleNamespace(AsyncClient=_AsyncClient)
from fastapi import FastAPI, UploadFile, File, HTTPException, Query, Request
from fastapi.responses import FileResponse, HTMLResponse, JSONResponse, RedirectResponse, PlainTextResponse
from fastapi.staticfiles import StaticFiles
from fastapi.openapi.utils import get_openapi
from pydantic import BaseModel, HttpUrl, field_validator

from fastapi_csrf_protect import CsrfProtect, CsrfProtectException
try:  # pragma: no cover - optional dependency for QR generation
    import qrcode  # type: ignore
except ModuleNotFoundError:  # pragma: no cover - environments without qrcode
    qrcode = None  # type: ignore[assignment]

app = FastAPI()


class RateLimiter:
    def __init__(self, requests_per_minute: int = 60) -> None:
        self._limits: defaultdict[str, List[datetime]] = defaultdict(list)
        self._rpm = max(requests_per_minute, 1)
        self._lock = threading.Lock()

    def check(self, identifier: str) -> bool:
        now = datetime.now(timezone.utc)
        cutoff = now - timedelta(minutes=1)
        with self._lock:
            timestamps = [t for t in self._limits[identifier] if t > cutoff]
            if len(timestamps) >= self._rpm:
                self._limits[identifier] = timestamps
                return False
            timestamps.append(now)
            self._limits[identifier] = timestamps
            return True

    def cleanup_old_identifiers(self, max_age_minutes: int = 60) -> None:
        cutoff = datetime.now(timezone.utc) - timedelta(minutes=max_age_minutes)
        with self._lock:
            stale: List[str] = []
            for identifier, timestamps in self._limits.items():
                if not timestamps or all(t < cutoff for t in timestamps):
                    stale.append(identifier)
            for identifier in stale:
                self._limits.pop(identifier, None)

    def reset(self) -> None:
        with self._lock:
            self._limits.clear()

    def update_limit(self, requests_per_minute: int) -> None:
        with self._lock:
            self._rpm = max(requests_per_minute, 1)
            self._limits.clear()

    def current_limit(self) -> int:
        with self._lock:
            return self._rpm


@dataclass
class Settings:
    PUBLIC_DIR: Path
    WORK_DIR: Path
    LOGS_DIR: Path
    PUBLIC_BASE_URL: Optional[str]
    RETENTION_DAYS: int
    MAX_FILE_SIZE_MB: int
    FFMPEG_TIMEOUT_SECONDS: int
    MIN_FREE_SPACE_MB: int
    UPLOAD_CHUNK_SIZE: int
    PUBLIC_CLEANUP_INTERVAL_SECONDS: int
    REQUIRE_DURATION_LIMIT: bool
    RATE_LIMIT_REQUESTS_PER_MINUTE: int

    @classmethod
    def load(cls) -> "Settings":
        def env_path(name: str, default: str) -> Path:
            return Path(os.getenv(name, default))

        def env_int(name: str, default: int) -> int:
            return int(os.getenv(name, str(default)))

        def env_bool(name: str, default: bool) -> bool:
            return os.getenv(name, str(default).lower()).lower() == "true"

        retention = env_int("RETENTION_DAYS", 7)
        if not (1 <= retention <= 365):
            raise ValueError("RETENTION_DAYS must be 1-365")

        rpm = env_int("RATE_LIMIT_REQUESTS_PER_MINUTE", 60)
        if rpm < 1:
            raise ValueError("RATE_LIMIT_REQUESTS_PER_MINUTE must be >= 1")

        return cls(
            PUBLIC_DIR=env_path("PUBLIC_DIR", "/data/public"),
            WORK_DIR=env_path("WORK_DIR", "/data/work"),
            LOGS_DIR=env_path("LOGS_DIR", "/data/logs"),
            PUBLIC_BASE_URL=os.getenv("PUBLIC_BASE_URL"),
            RETENTION_DAYS=retention,
            MAX_FILE_SIZE_MB=env_int("MAX_FILE_SIZE_MB", 2048),
            FFMPEG_TIMEOUT_SECONDS=env_int("FFMPEG_TIMEOUT_SECONDS", 2 * 60 * 60),
            MIN_FREE_SPACE_MB=env_int("MIN_FREE_SPACE_MB", 1000),
            UPLOAD_CHUNK_SIZE=env_int("UPLOAD_CHUNK_SIZE", 1024 * 1024),
            PUBLIC_CLEANUP_INTERVAL_SECONDS=env_int("PUBLIC_CLEANUP_INTERVAL_SECONDS", 3600),
            REQUIRE_DURATION_LIMIT=env_bool("REQUIRE_DURATION_LIMIT", False),
            RATE_LIMIT_REQUESTS_PER_MINUTE=rpm,
        )


settings = Settings.load()

# --------- config ---------
PUBLIC_DIR = settings.PUBLIC_DIR.resolve()
PUBLIC_DIR.mkdir(parents=True, exist_ok=True)

# Optional dedicated work directory to keep temp files on the same volume
WORK_DIR = settings.WORK_DIR.resolve()
WORK_DIR.mkdir(parents=True, exist_ok=True)

# Logs directory for persistent ffmpeg logs
LOGS_DIR = settings.LOGS_DIR.resolve()
LOGS_DIR.mkdir(parents=True, exist_ok=True)

# Application log file (captures stdout/stderr)
APP_LOG_FILE = LOGS_DIR / "application.log"

RETENTION_DAYS = settings.RETENTION_DAYS
PUBLIC_BASE_URL = settings.PUBLIC_BASE_URL  # e.g. "http://10.120.2.5:3000"

MAX_FILE_SIZE_MB = settings.MAX_FILE_SIZE_MB
MAX_FILE_SIZE_BYTES = MAX_FILE_SIZE_MB * 1024 * 1024
FFMPEG_TIMEOUT_SECONDS = settings.FFMPEG_TIMEOUT_SECONDS
MIN_FREE_SPACE_MB = settings.MIN_FREE_SPACE_MB
UPLOAD_CHUNK_SIZE = settings.UPLOAD_CHUNK_SIZE
PUBLIC_CLEANUP_INTERVAL_SECONDS = settings.PUBLIC_CLEANUP_INTERVAL_SECONDS
REQUIRE_DURATION_LIMIT = settings.REQUIRE_DURATION_LIMIT
RATE_LIMITER = RateLimiter(settings.RATE_LIMIT_REQUESTS_PER_MINUTE)

_FFMPEG_VERSION_CACHE: Optional[Dict[str, Optional[str]]] = None

JOBS: Dict[str, Dict[str, object]] = {}
JOBS_LOCK = threading.Lock()
SETTINGS_LOCK = threading.Lock()


SESSION_COOKIE_NAME = "ffapi_session"
SESSION_TTL_SECONDS = 3600

TOTP_STEP_SECONDS = 30
TOTP_DIGITS = 6
TOTP_RATE_LIMIT = 5
TOTP_WINDOW = 1
BACKUP_CODE_LENGTH = 10
BACKUP_CODE_GROUP_SIZE = 5
BACKUP_CODE_COUNT = 10
VIDEO_THUMBNAIL_EXTENSIONS = {".mp4", ".mov", ".mkv", ".webm", ".avi"}
THUMBNAIL_DIR_NAME = "thumbnails"
JOB_HISTORY_LIMIT = 20
PROBE_CACHE_TTL = 3600
PROBE_CACHE: Dict[str, Tuple[dict, float]] = {}
PROBE_CACHE_LOCK = threading.Lock()


def _generate_totp_secret(length: int = 20) -> str:
    raw = secrets.token_bytes(length)
    secret = base64.b32encode(raw).decode("utf-8").rstrip("=")
    return secret.upper()


def _normalize_base32(secret: str) -> bytes:
    padding = "=" * ((8 - len(secret) % 8) % 8)
    return base64.b32decode((secret + padding).upper(), casefold=True)


def _totp_at(secret_bytes: bytes, counter: int, digits: int = TOTP_DIGITS) -> str:
    message = struct.pack(">Q", counter)
    digest = hmac.new(secret_bytes, message, hashlib.sha1).digest()
    offset = digest[-1] & 0x0F
    truncated = struct.unpack(">I", digest[offset : offset + 4])[0] & 0x7FFFFFFF
    return f"{truncated % (10 ** digits):0{digits}d}"


def generate_totp_code(secret: str, for_time: Optional[int] = None) -> str:
    if for_time is None:
        for_time = int(time.time())
    counter = int(for_time // TOTP_STEP_SECONDS)
    secret_bytes = _normalize_base32(secret)
    return _totp_at(secret_bytes, counter)


def verify_totp_code(secret: str, code: str, window: int = TOTP_WINDOW) -> bool:
    code = code.strip()
    if not code.isdigit() or len(code) != TOTP_DIGITS:
        return False
    try:
        secret_bytes = _normalize_base32(secret)
    except Exception:
        return False
    now_counter = int(time.time() // TOTP_STEP_SECONDS)
    for offset in range(-window, window + 1):
        expected = _totp_at(secret_bytes, now_counter + offset)
        if secrets.compare_digest(expected, code):
            return True
    return False


_QR_PLACEHOLDER_PNG = (
    "iVBORw0KGgoAAAANSUhEUgAAAAEAAAABCAQAAAC1HAwCAAAAC0lEQVR4nGMAAQAABQABDQottAAAAABJRU5ErkJggg=="
)


def build_totp_qr_data_uri(otpauth_uri: str) -> Optional[str]:
    if not otpauth_uri:
        return None
    if qrcode is None:
        return f"data:image/png;base64,{_QR_PLACEHOLDER_PNG}"
    try:
        qr = qrcode.QRCode(version=1, box_size=4, border=2)
        qr.add_data(otpauth_uri)
        qr.make(fit=True)
        img = qr.make_image(fill_color="black", back_color="white")
        buffer = io.BytesIO()
        img.save(buffer, format="PNG")
        encoded = base64.b64encode(buffer.getvalue()).decode("ascii")
        return f"data:image/png;base64,{encoded}"
    except Exception:
        return f"data:image/png;base64,{_QR_PLACEHOLDER_PNG}"


class CsrfSettings(BaseModel):
    secret_key: str = os.getenv("CSRF_SECRET_KEY", secrets.token_urlsafe(32))
    cookie_name: str = os.getenv("CSRF_COOKIE_NAME", "csrftoken")
    header_name: str = "X-CSRF-Token"
    cookie_secure: bool = os.getenv("CSRF_COOKIE_SECURE", "false").lower() == "true"
    cookie_samesite: str = os.getenv("CSRF_COOKIE_SAMESITE", "Lax")
    time_limit: int = int(os.getenv("CSRF_TIME_LIMIT_SECONDS", "3600"))


@CsrfProtect.load_config
def _load_csrf_config() -> CsrfSettings:
    return CsrfSettings()


csrf_protect = CsrfProtect()
DEFAULT_UI_USERNAME = "admin"
DEFAULT_UI_PASSWORD = "admin123"


class UIAuthManager:
    def __init__(self) -> None:
        self._lock = threading.Lock()
        self.require_login = False
        self.username = DEFAULT_UI_USERNAME
        self._password_hash = self._hash(DEFAULT_UI_PASSWORD)
        self._sessions: Dict[str, float] = {}
        self.two_factor_enabled = False
        self._totp_secret = _generate_totp_secret()
        self._totp_attempts: Dict[str, List[float]] = {}
        self._backup_codes: Dict[str, Dict[str, Any]] = {}
        self._pending_backup_codes: List[str] = []

    def _hash(self, password: str) -> str:
        if bcrypt is not None:
            salt = bcrypt.gensalt()
            hashed = bcrypt.hashpw(password.encode("utf-8"), salt)
            return f"bcrypt${hashed.decode('utf-8')}"
        digest = hashlib.sha256(password.encode("utf-8")).hexdigest()
        return f"sha256${digest}"

    def _clear_backup_codes_locked(self) -> None:
        self._backup_codes.clear()
        self._pending_backup_codes = []

    def reset(self) -> None:
        with self._lock:
            self.require_login = False
            self.username = DEFAULT_UI_USERNAME
            self._password_hash = self._hash(DEFAULT_UI_PASSWORD)
            self._sessions.clear()
            self.two_factor_enabled = False
            self._totp_secret = _generate_totp_secret()
            self._totp_attempts.clear()
            self._clear_backup_codes_locked()

    def set_require_login(self, enabled: bool) -> None:
        with self._lock:
            self.require_login = enabled
            if not enabled:
                self._sessions.clear()
                self._totp_attempts.clear()
                self._clear_backup_codes_locked()

    def set_credentials(self, username: str, password: str) -> None:
        username = username.strip()
        if not username:
            raise ValueError("Username is required")
        if not password:
            raise ValueError("Password is required")
        if len(password) < 6:
            raise ValueError("Password must be at least 6 characters long")
        with self._lock:
            self.username = username
            self._password_hash = self._hash(password)
            self._sessions.clear()
            # Enabling new credentials invalidates existing second-factor sessions
            self.two_factor_enabled = False
            self._totp_secret = _generate_totp_secret()
            self._totp_attempts.clear()
            self._clear_backup_codes_locked()

    def verify(self, username: str, password: str) -> bool:
        with self._lock:
            expected_user = self.username
            expected_hash = self._password_hash
        if not secrets.compare_digest(expected_user, username):
            return False
        if expected_hash.startswith("bcrypt$"):
            if bcrypt is None:
                return False
            stored_hash = expected_hash.split("$", 1)[1].encode("utf-8")
            try:
                return bcrypt.checkpw(password.encode("utf-8"), stored_hash)
            except ValueError:
                return False
        if expected_hash.startswith("sha256$"):
            digest = hashlib.sha256(password.encode("utf-8")).hexdigest()
            return secrets.compare_digest(expected_hash.split("$", 1)[1], digest)
        if "$" not in expected_hash:
            digest = hashlib.sha256(password.encode("utf-8")).hexdigest()
            return secrets.compare_digest(expected_hash, digest)
        return False

    def create_session(self) -> str:
        token = secrets.token_hex(16)
        expiry = time.time() + SESSION_TTL_SECONDS
        with self._lock:
            self._sessions[token] = expiry
        return token

    def authenticate_session(self, token: Optional[str]) -> bool:
        if not token:
            return False
        now = time.time()
        with self._lock:
            expiry = self._sessions.get(token)
            if expiry is None:
                return False
            if expiry < now:
                self._sessions.pop(token, None)
                return False
            self._sessions[token] = now + SESSION_TTL_SECONDS
            return True

    def clear_session(self, token: str) -> None:
        with self._lock:
            self._sessions.pop(token, None)

    def get_two_factor_secret(self) -> str:
        with self._lock:
            return self._totp_secret

    def get_otpauth_uri(self) -> str:
        secret = self.get_two_factor_secret()
        username = self.get_username()
        label = quote(f"FFAPI:{username}")
        issuer = quote("FFAPI")
        return f"otpauth://totp/{label}?secret={secret}&issuer={issuer}"

    def get_username(self) -> str:
        with self._lock:
            return self.username

    def set_two_factor_enabled(self, enabled: bool) -> None:
        with self._lock:
            self.two_factor_enabled = enabled
            if not enabled:
                self._sessions.clear()
                self._totp_attempts.clear()
                self._clear_backup_codes_locked()

    def regenerate_two_factor_secret(self) -> str:
        secret = _generate_totp_secret()
        with self._lock:
            self._totp_secret = secret
            self.two_factor_enabled = False
            self._sessions.clear()
            self._totp_attempts.clear()
            self._clear_backup_codes_locked()
        return secret

    def is_two_factor_enabled(self) -> bool:
        with self._lock:
            return self.two_factor_enabled

    def _normalize_backup_code(self, code: str) -> Optional[str]:
        cleaned = "".join(ch for ch in code.upper() if ch.isalnum())
        if len(cleaned) != BACKUP_CODE_LENGTH:
            return None
        return cleaned

    def _hash_backup_code(self, normalized: str) -> str:
        return hashlib.sha256(normalized.encode("utf-8")).hexdigest()

    def generate_backup_codes(self, count: int = BACKUP_CODE_COUNT) -> List[str]:
        alphabet = string.ascii_uppercase + string.digits
        new_codes: Dict[str, Dict[str, Any]] = {}
        plain_codes: List[str] = []
        for _ in range(count):
            normalized = "".join(secrets.choice(alphabet) for _ in range(BACKUP_CODE_LENGTH))
            chunks = [
                normalized[i : i + BACKUP_CODE_GROUP_SIZE]
                for i in range(0, BACKUP_CODE_LENGTH, BACKUP_CODE_GROUP_SIZE)
            ]
            display = "-".join(chunks)
            hashed = self._hash_backup_code(normalized)
            new_codes[hashed] = {"used": False, "last_four": normalized[-4:]}
            plain_codes.append(display)
        with self._lock:
            self._backup_codes = new_codes
            self._pending_backup_codes = list(plain_codes)
        return plain_codes

    def verify_backup_code(self, code: str) -> bool:
        normalized = self._normalize_backup_code(code)
        if not normalized:
            return False
        hashed = self._hash_backup_code(normalized)
        with self._lock:
            entry = self._backup_codes.get(hashed)
            if not entry or entry["used"]:
                return False
            entry["used"] = True
            self._backup_codes[hashed] = entry
            self._totp_attempts.pop(self.username, None)
        return True

    def pop_pending_backup_codes(self) -> List[str]:
        with self._lock:
            pending = list(self._pending_backup_codes)
            self._pending_backup_codes = []
        return pending

    def get_backup_code_status(self) -> List[Dict[str, Any]]:
        with self._lock:
            return [
                {"last_four": entry["last_four"], "used": entry["used"]}
                for entry in self._backup_codes.values()
            ]

    def verify_second_factor(self, totp_code: str = "", backup_code: str = "") -> bool:
        totp_code = totp_code.strip()
        backup_code = backup_code.strip()
        if totp_code and self.verify_totp(totp_code):
            return True
        if backup_code and self.verify_backup_code(backup_code):
            return True
        if totp_code and not backup_code:
            return self.verify_backup_code(totp_code)
        return False

    def verify_totp(self, code: str) -> bool:
        username = self.get_username()
        now = time.time()
        with self._lock:
            attempts = [t for t in self._totp_attempts.get(username, []) if t > now - 60]
            if len(attempts) >= TOTP_RATE_LIMIT:
                self._totp_attempts[username] = attempts
                return False
            attempts.append(now)
            self._totp_attempts[username] = attempts
            secret = self._totp_secret
        code = code.strip()
        if not code.isdigit() or len(code) != TOTP_DIGITS:
            return False
        try:
            secret_bytes = _normalize_base32(secret)
        except Exception:
            return False
        now_counter = int(time.time() // TOTP_STEP_SECONDS)
        for offset in range(-TOTP_WINDOW, TOTP_WINDOW + 1):
            expected = _totp_at(secret_bytes, now_counter + offset)
            if secrets.compare_digest(expected, code):
                with self._lock:
                    self._totp_attempts.pop(username, None)
                return True
        return False

    def cleanup_expired_sessions(self) -> None:
        cutoff = time.time()
        with self._lock:
            expired_sessions = [token for token, expiry in self._sessions.items() if expiry < cutoff]
            for token in expired_sessions:
                self._sessions.pop(token, None)
            # Trim stale attempt counters to avoid unbounded growth
            recent_cutoff = cutoff - 60
            for key in list(self._totp_attempts.keys()):
                recent = [ts for ts in self._totp_attempts[key] if ts > recent_cutoff]
                if recent:
                    self._totp_attempts[key] = recent
                else:
                    self._totp_attempts.pop(key, None)


UI_AUTH = UIAuthManager()


def ensure_csrf_token(request: Request) -> str:
    existing = request.cookies.get(csrf_protect.cookie_name)
    if existing:
        try:
            csrf_protect.validate_token(existing)
            return existing
        except CsrfProtectException:
            pass
    token = csrf_protect.generate_csrf()
    request.state._csrf_token_to_set = token
    return token


def csrf_hidden_input(token: str) -> str:
    return f'<input type="hidden" name="csrf_token" value="{html.escape(token, quote=True)}" />'


class APIKeyManager:
    def __init__(self) -> None:
        self._lock = threading.Lock()
        self.require_key = False
        self._keys: Dict[str, Dict[str, Any]] = {}
        self._plaintext: Dict[str, str] = {}

    def reset(self) -> None:
        with self._lock:
            self.require_key = False
            self._keys.clear()
            self._plaintext.clear()

    def is_required(self) -> bool:
        with self._lock:
            return self.require_key

    def set_require_key(self, enabled: bool) -> None:
        with self._lock:
            self.require_key = enabled

    def generate_key(self) -> tuple[str, str]:
        token = secrets.token_urlsafe(32)
        digest = hashlib.sha256(token.encode("utf-8")).hexdigest()
        entry = {
            "hash": digest,
            "created": time.time(),
            "prefix": token[:8],
            "last_used": None,
        }
        key_id = uuid4().hex
        with self._lock:
            self._keys[key_id] = entry
            self._plaintext[key_id] = token
        return key_id, token

    def consume_plaintext(self, key_id: str) -> Optional[str]:
        with self._lock:
            return self._plaintext.pop(key_id, None)

    def list_keys(self) -> List[Dict[str, Any]]:
        with self._lock:
            items = [
                {
                    "id": key_id,
                    "prefix": data["prefix"],
                    "created": data["created"],
                    "last_used": data.get("last_used"),
                }
                for key_id, data in self._keys.items()
            ]
        items.sort(key=lambda item: item["created"], reverse=True)
        return items

    def remove_key(self, key_id: str) -> None:
        with self._lock:
            self._keys.pop(key_id, None)
            self._plaintext.pop(key_id, None)

    def verify(self, token: Optional[str]) -> bool:
        if not token:
            return False
        digest = hashlib.sha256(token.encode("utf-8")).hexdigest()
        now = time.time()
        with self._lock:
            for data in self._keys.values():
                if secrets.compare_digest(data["hash"], digest):
                    data["last_used"] = now
                    return True
        return False


API_KEYS = APIKeyManager()


_DASHBOARD_EXACT_PATHS = {"/", "/health"}
_DASHBOARD_PREFIXES = (
    "/downloads",
    "/logs",
    "/ffmpeg",
    "/metrics",
    "/documentation",
    "/settings",
    "/api-keys",
    "/files",
)


def is_dashboard_path(path: str) -> bool:
    if path in _DASHBOARD_EXACT_PATHS:
        return True
    return any(path.startswith(prefix) for prefix in _DASHBOARD_PREFIXES)


def requires_api_key(request: Request) -> bool:
    if not API_KEYS.is_required():
        return False
    path = request.url.path
    if is_dashboard_path(path):
        return False
    return True


def is_authenticated(request: Request) -> bool:
    token = request.cookies.get(SESSION_COOKIE_NAME)
    return UI_AUTH.authenticate_session(token)


def ensure_dashboard_access(request: Request) -> Optional[RedirectResponse]:
    if not UI_AUTH.require_login:
        return None
    if is_authenticated(request):
        return None
    next_path = request.url.path
    if request.url.query:
        next_path = f"{next_path}?{request.url.query}"
    response = RedirectResponse(
        url=f"/settings?{urlencode({'next': next_path})}", status_code=303
    )
    response.delete_cookie(SESSION_COOKIE_NAME)
    return response


def storage_management_snapshot() -> Dict[str, Any]:
    total_bytes = 0
    active_files = 0
    expired_files = 0
    cutoff: Optional[float]
    if RETENTION_DAYS > 0:
        cutoff = time.time() - (RETENTION_DAYS * 86400)
    else:
        cutoff = None

    for root, _, files in os.walk(PUBLIC_DIR):
        for name in files:
            path = Path(root) / name
            try:
                stat = path.stat()
            except Exception:
                continue
            total_bytes += stat.st_size
            if cutoff is None:
                active_files += 1
            elif stat.st_mtime < cutoff:
                expired_files += 1
            else:
                active_files += 1

    try:
        quota_total = shutil.disk_usage(PUBLIC_DIR).total
        quota_mb = quota_total / (1024 * 1024)
    except Exception:
        quota_mb = None

    return {
        "total_bytes": total_bytes,
        "active_files": active_files,
        "expired_files": expired_files,
        "quota_mb": quota_mb,
    }


NAV_LINKS: Tuple[Tuple[str, str], ...] = (
    ("/downloads", "Downloads"),
    ("/logs", "Logs"),
    ("/ffmpeg", "FFmpeg Info"),
    ("/metrics", "Metrics"),
    ("/jobs", "Jobs"),
    ("/documentation", "API Docs"),
    ("/api-keys", "API Keys"),
    ("/settings", "Settings"),
)


def render_nav(active: Optional[str] = None, *, indent: str = "      ") -> str:
    lines = ["<nav>"]
    for href, label in NAV_LINKS:
        class_attr = ' class="active"' if active == href else ""
        lines.append(f'  <a href="{href}"{class_attr}>{label}</a>')
    lines.append("</nav>")
    return "\n".join(f"{indent}{line}" for line in lines)


def render_login_page(next_path: str, csrf_token: str, error: Optional[str] = None) -> str:
    alert = ""
    if error:
        alert = f"<div class=\"alert error\">{html.escape(error)}</div>"
    require_code = UI_AUTH.is_two_factor_enabled()
    code_field = ""
    if require_code:
        code_field = """
        <label for=\"totp\">Authentication code</label>
        <input id=\"totp\" name=\"totp\" type=\"text\" inputmode=\"numeric\" pattern=\"\\d*\" autocomplete=\"one-time-code\" />
        <label for=\"backup_code\">Backup code (optional)</label>
        <input id=\"backup_code\" name=\"backup_code\" type=\"text\" autocomplete=\"one-time-code\" placeholder=\"ABCDE-12345\" />
        <p class=\"help-text\">Enter a 6-digit authenticator code or one of your backup codes.</p>
        """
    csrf_field = csrf_hidden_input(csrf_token)
    return f"""
    <!doctype html>
    <html>
    <head>
      <meta charset=\"utf-8\" />
      <title>Settings Login</title>
      <style>
        body {{ font-family: system-ui, sans-serif; padding: 24px; max-width: 400px; margin: 0 auto; }}
        .brand {{ font-size: 32px; font-weight: bold; margin-bottom: 20px; text-align: center; }}
        .brand .ff {{ color: #28a745; }}
        .brand .api {{ color: #000; }}
        form {{ display: flex; flex-direction: column; gap: 12px; }}
        label {{ font-weight: 600; }}
        input {{ padding: 10px; font-size: 14px; border: 1px solid #ccc; border-radius: 4px; }}
        button {{ padding: 10px; background: #0066cc; color: #fff; border: none; border-radius: 4px; font-size: 14px; cursor: pointer; }}
        button:hover {{ background: #0053a3; }}
        .alert {{ padding: 10px; border-radius: 4px; font-size: 14px; margin-bottom: 16px; }}
        .alert.error {{ background: #fdecea; color: #b3261e; border: 1px solid #f7c6c4; }}
        .help {{ font-size: 13px; color: #555; text-align: center; margin-top: 12px; }}
        .help-text {{ font-size: 13px; color: #4b5563; margin-top: -4px; }}
      </style>
    </head>
    <body>
      <div class=\"brand\"><span class=\"ff\">ff</span><span class=\"api\">api</span></div>
      {alert}
      <form method=\"post\" action=\"/settings/login\">
        {csrf_field}
        <input type=\"hidden\" name=\"next\" value=\"{html.escape(next_path, quote=True)}\" />
        <label for=\"username\">Username</label>
        <input id=\"username\" name=\"username\" type=\"text\" autocomplete=\"username\" required />
        <label for=\"password\">Password</label>
        <input id=\"password\" name=\"password\" type=\"password\" autocomplete=\"current-password\" required />
        {code_field}
        <button type=\"submit\">Sign in</button>
      </form>
      <div class=\"help\">Default credentials: {html.escape(DEFAULT_UI_USERNAME)} / {html.escape(DEFAULT_UI_PASSWORD)}</div>
    </body>
    </html>
    """


def render_settings_page(
    storage: Dict[str, Any],
    message: Optional[str] = None,
    error: Optional[str] = None,
    *,
    authenticated: bool,
    csrf_token: str,
    backup_codes: Optional[List[str]] = None,
    backup_status: Optional[List[Dict[str, Any]]] = None,
) -> str:
    total_used_mb = storage["total_bytes"] / (1024 * 1024) if storage["total_bytes"] else 0.0
    active_files = storage["active_files"]
    expired_files = storage["expired_files"]
    quota_mb = storage["quota_mb"]
    quota_display = f"{quota_mb:.1f} MB" if quota_mb is not None else "Unknown"
    alert_blocks = []
    if message:
        alert_blocks.append(f"<div class=\"alert success\">{html.escape(message)}</div>")
    if error:
        alert_blocks.append(f"<div class=\"alert error\">{html.escape(error)}</div>")
    backup_codes = backup_codes or []
    backup_status = backup_status or []
    if backup_codes:
        code_items = "".join(
            f"<li><code>{html.escape(code)}</code></li>" for code in backup_codes
        )
        alert_blocks.append(
            """
            <div class="alert info">
              <strong>Backup codes generated.</strong>
              <p>Store these codes in a safe place. Each code can be used once if you lose access to your authenticator app.</p>
              <ul class="backup-list">{items}</ul>
            </div>
            """.format(items=code_items)
        )
    alerts = "".join(alert_blocks)
    require_login = UI_AUTH.require_login
    status_text = "Enabled" if require_login else "Disabled"
    auth_note = (
        "Dashboard pages currently require sign-in."
        if require_login
        else "Dashboard pages are open without authentication."
    )
    checkbox_state = "checked" if require_login else ""
    disabled_class = "" if require_login else " is-disabled"
    disabled_attr = "" if require_login else " disabled"
    csrf_field = csrf_hidden_input(csrf_token)

    logout_button = ""
    if require_login and authenticated:
        logout_button = f"""
        <form method=\"post\" action=\"/settings/logout\">
          {csrf_field}
          <button type=\"submit\" class=\"secondary\">Log out</button>
        </form>
        """

    def format_number(value: float, digits: int = 2) -> str:
        text = f"{value:.{digits}f}"
        if "." in text:
            text = text.rstrip("0").rstrip(".")
        return text

    retention_hours = RETENTION_DAYS * 24
    retention_hours_display = format_number(retention_hours)
    retention_days_display = format_number(RETENTION_DAYS)
    rate_limit_rpm = RATE_LIMITER.current_limit()
    ffmpeg_timeout_minutes = FFMPEG_TIMEOUT_SECONDS / 60
    ffmpeg_timeout_display = format_number(ffmpeg_timeout_minutes)
    upload_chunk_mb = UPLOAD_CHUNK_SIZE / (1024 * 1024)
    upload_chunk_display = format_number(upload_chunk_mb)
    retention_hours_value = html.escape(retention_hours_display)
    retention_days_text = html.escape(retention_days_display)
    rate_limit_value = html.escape(str(rate_limit_rpm))
    ffmpeg_timeout_value = html.escape(ffmpeg_timeout_display)
    upload_chunk_value = html.escape(upload_chunk_display)
    max_file_size_value = html.escape(str(MAX_FILE_SIZE_MB))
    total_used_text = html.escape(f"{format_number(total_used_mb)} MB")
    active_files_text = html.escape(str(active_files))
    expired_files_text = html.escape(str(expired_files))
    quota_text = html.escape(quota_display)
    api_require_key = API_KEYS.is_required()
    api_status_text = "Enabled" if api_require_key else "Disabled"
    api_note = (
        "API requests currently require a valid key."
        if api_require_key
        else "Requests are open without a key. Enable protection to restrict access."
    )
    api_note_text = html.escape(api_note)
    api_status_html = html.escape(api_status_text)
    api_checkbox_state = "checked" if api_require_key else ""
    api_help_text = html.escape(
        "Clients must provide X-API-Key header or api_key query when enabled."
    )
    two_factor_enabled = UI_AUTH.is_two_factor_enabled()
    two_factor_status_text = "Enabled" if two_factor_enabled else "Disabled"
    if two_factor_enabled:
        two_factor_note = "One-time codes are required when signing in."
    elif not require_login:
        two_factor_note = "Enable dashboard login to configure two-factor authentication."
    else:
        two_factor_note = "Protect the dashboard with an authenticator app."
    two_factor_note_text = html.escape(two_factor_note)
    secret_raw = UI_AUTH.get_two_factor_secret()
    grouped_secret = " ".join(
        secret_raw[i : i + 4] for i in range(0, len(secret_raw), 4)
    )
    secret_display = html.escape(grouped_secret)
    otpauth_uri_value = UI_AUTH.get_otpauth_uri()
    otpauth_uri = html.escape(otpauth_uri_value, quote=True)
    qr_data_uri = build_totp_qr_data_uri(otpauth_uri_value)
    qr_image_html = ""
    if qr_data_uri:
        qr_image_html = (
            "<div class=\"qr-wrapper\">"
            f"<img src=\"{html.escape(qr_data_uri, quote=True)}\" alt=\"Authenticator QR code\" class=\"qr-image\" />"
            "</div>"
        )
    two_factor_disabled_attr = disabled_attr
    two_factor_card_class = disabled_class
    two_factor_status_class = "status-pill enabled" if two_factor_enabled else "status-pill"
    backup_generation_disabled = two_factor_disabled_attr or ("" if two_factor_enabled else " disabled")
    backup_download_disabled = two_factor_disabled_attr or (
        " disabled" if not backup_status else ""
    )
    backup_card_class = "" if two_factor_enabled else " is-disabled"
    backup_rows: List[str] = []
    for idx, item in enumerate(backup_status, start=1):
        last_four = html.escape(item.get("last_four", "????"))
        masked = f"\u2022\u2022\u2022\u2022-{last_four}"
        status_text = "Used" if item.get("used") else "Unused"
        status_class = "status used" if item.get("used") else "status unused"
        backup_rows.append(
            """
            <tr>
              <td>{index}</td>
              <td><code>{masked}</code></td>
              <td><span class="{cls}">{status}</span></td>
            </tr>
            """.format(index=idx, masked=masked, cls=status_class, status=html.escape(status_text))
        )
    if not backup_rows:
        backup_rows.append(
            """
            <tr>
              <td colspan="3">Generate backup codes to view their status.</td>
            </tr>
            """
        )
    if two_factor_enabled:
        two_factor_actions = f"""
            <div class=\"twofactor-actions\">
              <form method=\"post\" action=\"/settings/two-factor\">
                {csrf_field}
                <input type=\"hidden\" name=\"action\" value=\"disable\" />
                <button type=\"submit\" class=\"secondary\" onclick=\"return confirm('Disable two-factor authentication?')\"{two_factor_disabled_attr}>Disable two-factor</button>
              </form>
              <form method=\"post\" action=\"/settings/two-factor\">
                {csrf_field}
                <input type=\"hidden\" name=\"action\" value=\"regenerate\" />
                <button type=\"submit\" class=\"secondary\"{two_factor_disabled_attr}>Generate new secret</button>
              </form>
            </div>
        """
    else:
        two_factor_actions = f"""
            <div class=\"twofactor-actions\">
              <form method=\"post\" action=\"/settings/two-factor\">
                {csrf_field}
                <input type=\"hidden\" name=\"action\" value=\"enable\" />
                <label for=\"totp_code\">Authentication code</label>
                <input id=\"totp_code\" name=\"code\" type=\"text\" inputmode=\"numeric\" pattern=\"\\d*\" placeholder=\"123456\"{two_factor_disabled_attr} />
                <span class=\"help-text\">Enter a code from your authenticator app.</span>
                <button type=\"submit\"{two_factor_disabled_attr}>Enable two-factor</button>
              </form>
              <form method=\"post\" action=\"/settings/two-factor\">
                {csrf_field}
                <input type=\"hidden\" name=\"action\" value=\"regenerate\" />
                <button type=\"submit\" class=\"secondary\"{two_factor_disabled_attr}>Generate new secret</button>
              </form>
            </div>
        """

    backup_section = f"""
            <div class=\"backup-codes{backup_card_class}\">
              <div class=\"backup-header\">
                <h4>Backup codes</h4>
                <div class=\"backup-actions\">
                  <button type=\"button\" class=\"secondary\"{backup_download_disabled} onclick=\"downloadBackupCodes()\">Download as TXT</button>
                  <form method=\"post\" action=\"/settings/two-factor\">
                    {csrf_field}
                    <input type=\"hidden\" name=\"action\" value=\"generate_codes\" />
                    <button type=\"submit\" class=\"secondary\"{backup_generation_disabled}>Generate backup codes</button>
                  </form>
                </div>
              </div>
              <p class=\"help-text\">Each backup code may be used once when your authenticator is unavailable.</p>
              <table class=\"backup-table\">
                <thead><tr><th>#</th><th>Code</th><th>Status</th></tr></thead>
                <tbody>{"".join(backup_rows)}</tbody>
              </table>
            </div>
    """

    nav_html = render_nav("/settings")
    return f"""
    <!doctype html>
    <html>
    <head>
      <meta charset=\"utf-8\" />
      <title>Settings</title>
      <style>
        body {{ font-family: system-ui, sans-serif; padding: 24px; max-width: 1400px; margin: 0 auto; }}
        .brand {{ font-size: 32px; font-weight: bold; margin-bottom: 20px; }}
        .brand .ff {{ color: #28a745; }}
        .brand .api {{ color: #000; }}
        nav {{ margin-bottom: 20px; }}
        nav a {{ margin-right: 15px; color: #0066cc; text-decoration: none; }}
        h2 {{ margin-top: 32px; }}
        section {{ background: #f7f9fc; padding: 20px; border-radius: 8px; box-shadow: 0 1px 2px rgba(0,0,0,0.08); margin-bottom: 24px; }}
        form {{ margin-top: 12px; }}
        label {{ display: block; font-weight: 600; margin-bottom: 6px; }}
        input {{ padding: 10px; font-size: 14px; border: 1px solid #ccd5e0; border-radius: 4px; width: 100%; box-sizing: border-box; }}
        button {{ padding: 10px 16px; background: #0066cc; color: #fff; border: none; border-radius: 4px; font-size: 14px; cursor: pointer; }}
        button:hover {{ background: #0053a3; }}
        button.secondary {{ background: #9aa5b1; }}
        button.secondary:hover {{ background: #7c8794; }}
        .checkbox-row {{ display: flex; align-items: center; gap: 10px; font-weight: 600; }}
        .checkbox-row input {{ width: auto; margin: 0; transform: scale(1.2); }}
        .settings-grid {{ display: grid; grid-template-columns: repeat(auto-fit, minmax(220px, 1fr)); gap: 16px; margin-top: 16px; }}
        .settings-row {{ display: grid; grid-template-columns: repeat(auto-fit, minmax(280px, 1fr)); gap: 24px; margin-bottom: 24px; }}
        .settings-row section {{ margin-bottom: 0; }}
        .alert {{ padding: 12px; border-radius: 4px; margin-bottom: 16px; font-size: 14px; }}
        .alert.success {{ background: #e8f5e9; color: #256029; border: 1px solid #c8e6c9; }}
        .alert.error {{ background: #fdecea; color: #b3261e; border: 1px solid #f7c6c4; }}
        .alert.info {{ background: #eff6ff; color: #1e40af; border: 1px solid #bfdbfe; }}
        .alert.info ul {{ margin: 8px 0 0; padding-left: 20px; }}
        .alert.info li {{ font-family: 'Courier New', monospace; margin-bottom: 4px; }}
        .storage-table {{ margin-top: 20px; background: #fff; border-radius: 8px; box-shadow: inset 0 0 0 1px #e1e7ef; overflow: hidden; }}
        .storage-table h3 {{ margin: 0; padding: 16px; font-size: 16px; color: #1f2937; border-bottom: 1px solid #d6e2f1; }}
        .storage-table table {{ width: 100%; border-collapse: collapse; }}
        .storage-table th {{ width: 220px; font-weight: 600; color: #2f3b52; }}
        .storage-table td {{ color: #334155; }}
        .storage-table th, .storage-table td {{ padding: 12px 16px; font-size: 14px; vertical-align: top; }}
        .storage-table tr:not(:last-child) td, .storage-table tr:not(:last-child) th {{ border-bottom: 1px solid #edf2f7; }}
        .auth-row {{ display: grid; grid-template-columns: repeat(auto-fit, minmax(220px, 1fr)); gap: 16px; align-items: stretch; margin-top: 16px; }}
        .auth-card {{ background: #fff; padding: 16px; border-radius: 8px; box-shadow: inset 0 0 0 1px #e1e7ef; display: flex; flex-direction: column; gap: 12px; }}
        .auth-card.is-disabled {{ opacity: 0.55; }}
        .auth-card h3 {{ margin: 0; font-size: 16px; color: #1f2937; }}
        .auth-card p {{ margin: 0; font-size: 14px; color: #334155; }}
        .auth-card form {{ margin-top: 0; display: flex; flex-direction: column; gap: 12px; align-items: flex-start; }}
        .auth-card button {{ width: fit-content; padding: 8px 14px; }}
        .credentials-block {{ margin-top: 20px; padding-top: 16px; border-top: 1px solid #e1e7ef; display: flex; flex-direction: column; gap: 12px; }}
        .credentials-block.is-disabled {{ opacity: 0.5; }}
        .credentials-grid {{ display: grid; grid-template-columns: 1fr; gap: 12px; }}
        .credentials-grid label {{ margin-bottom: 0; }}
        .twofactor-card .status-pill {{ background: #e0f2fe; color: #0369a1; }}
        .twofactor-card .status-pill.enabled {{ background: #e8f5e9; color: #256029; }}
        .secret-box {{ background: #f1f5f9; border-radius: 6px; padding: 12px; display: flex; flex-direction: column; gap: 4px; }}
        .secret-box span {{ font-size: 12px; color: #526079; text-transform: uppercase; letter-spacing: 0.1em; }}
        .secret-box code {{ font-size: 18px; letter-spacing: 2px; font-weight: 600; }}
        .twofactor-actions {{ display: flex; flex-direction: column; gap: 12px; margin-top: 4px; align-items: flex-start; }}
        .twofactor-actions form {{ margin: 0; display: flex; flex-direction: column; gap: 8px; align-items: flex-start; }}
        .twofactor-actions button {{ width: fit-content; }}
        .status-pill {{ display: inline-flex; align-items: center; gap: 6px; padding: 6px 10px; background: #e8f5e9; color: #256029; border-radius: 999px; font-weight: 600; width: fit-content; }}
        .help-text {{ font-size: 13px; color: #526079; }}
        .qr-wrapper {{ margin: 12px 0; }}
        .qr-image {{ width: 140px; height: 140px; image-rendering: pixelated; border: 1px solid #d1d9e6; border-radius: 8px; padding: 8px; background: #fff; }}
        .backup-codes {{ margin-top: 16px; background: #fff; border-radius: 8px; box-shadow: inset 0 0 0 1px #e1e7ef; padding: 16px; display: flex; flex-direction: column; gap: 12px; }}
        .backup-codes.is-disabled {{ opacity: 0.55; }}
        .backup-header {{ display: flex; align-items: center; justify-content: space-between; gap: 12px; }}
        .backup-header h4 {{ margin: 0; font-size: 15px; color: #1f2937; }}
        .backup-header form {{ margin: 0; }}
        .backup-actions {{ display: flex; align-items: center; gap: 10px; flex-wrap: wrap; justify-content: flex-end; }}
        .backup-table {{ width: 100%; border-collapse: collapse; }}
        .backup-table th, .backup-table td {{ padding: 8px 10px; font-size: 13px; border-bottom: 1px solid #e2e8f0; text-align: left; }}
        .backup-table td code {{ font-size: 13px; }}
        .backup-table .status {{ display: inline-flex; align-items: center; gap: 6px; padding: 4px 8px; border-radius: 999px; font-weight: 600; background: #e0f2fe; color: #0369a1; }}
        .backup-table .status.used {{ background: #f8f0f0; color: #b91c1c; }}
        .form-grid {{ display: grid; grid-template-columns: repeat(auto-fit, minmax(200px, 1fr)); gap: 16px; }}
        .field-card {{ background: #f9fbff; padding: 12px; border-radius: 8px; box-shadow: inset 0 0 0 1px #d6e2f1; display: grid; gap: 8px; }}
        .retention-form button, .performance-form button {{ margin-top: 16px; }}
        .field-card label {{ margin-bottom: 0; display: flex; align-items: center; gap: 6px; }}
        .tooltip {{ position: relative; display: inline-flex; align-items: center; justify-content: center; width: 18px; height: 18px; border-radius: 50%; background: #e2e8f0; color: #1e293b; font-size: 12px; font-weight: 600; cursor: help; }}
        .tooltip:focus-visible {{ outline: 2px solid #2563eb; outline-offset: 2px; }}
        .tooltip .tooltiptext {{ visibility: hidden; opacity: 0; width: 220px; background: #1e293b; color: #f8fafc; text-align: left; border-radius: 6px; padding: 8px 10px; position: absolute; z-index: 10; bottom: 125%; left: 50%; transform: translateX(-50%); transition: opacity 0.2s ease-in-out; box-shadow: 0 8px 16px rgba(15, 23, 42, 0.25); }}
        .tooltip:hover .tooltiptext, .tooltip:focus .tooltiptext {{ visibility: visible; opacity: 1; }}
        #processingIndicator {{ display: none; position: fixed; inset: 0; background: rgba(15, 23, 42, 0.35); color: #f8fafc; font-size: 16px; font-weight: 600; align-items: center; justify-content: center; z-index: 9999; gap: 8px; }}
        #processingIndicator .spinner {{ width: 18px; height: 18px; border-radius: 50%; border: 2px solid rgba(248, 250, 252, 0.45); border-top-color: #f8fafc; animation: spin 0.8s linear infinite; }}
        @keyframes spin {{ to {{ transform: rotate(360deg); }} }}
      </style>
    </head>
    <body>
      <div class=\"brand\"><span class=\"ff\">ff</span><span class=\"api\">api</span></div>
{nav_html}
      {alerts}

      <section>
        <h2>UI Authentication</h2>
        <div class="auth-row">
          <div class="auth-card toggle-card">
            <h3>Access control</h3>
            <span class="status-pill">{status_text}</span>
            <p>{auth_note}</p>
            <form method="post" action="/settings/ui-auth">
              {csrf_field}
              <label class="checkbox-row" for="require_login">
                <input type="checkbox" id="require_login" name="require_login" value="true" {checkbox_state} />
                <span>Require login for dashboard pages</span>
              </label>
              <button type="submit">Save preference</button>
            </form>
            {logout_button}
            <div class="credentials-block{disabled_class}">
              <h4>Update credentials</h4>
              <form method="post" action="/settings/credentials">
                {csrf_field}
                <div class="credentials-grid">
                  <label for="username">Username</label>
                  <input id="username" name="username" type="text" value="{html.escape(UI_AUTH.username, quote=True)}" required{disabled_attr} />
                  <label for="password">New password</label>
                  <input id="password" name="password" type="password" placeholder="Enter a new password" required{disabled_attr} />
                </div>
                <button type="submit"{disabled_attr}>Update credentials</button>
              </form>
            </div>
          </div>
          <div class="auth-card twofactor-card{two_factor_card_class}">
            <h3>Two-factor authentication</h3>
            <span class="{two_factor_status_class}">{two_factor_status_text}</span>
            <p>{two_factor_note_text}</p>
            <div class="secret-box">
              <span>Secret</span>
              <code>{secret_display}</code>
            </div>
            {qr_image_html}
            <p class="help-text">Scan or add the secret manually, then use<br><a href="{otpauth_uri}">{otpauth_uri}</a> in your authenticator.</p>
            {two_factor_actions}
            {backup_section}
          </div>
        </div>
      </section>

      <div class="settings-row">
      <section>
        <h2>API Authentication</h2>
        <span class="status-pill">{api_status_html}</span>
        <p>{api_note_text}</p>
        <form method="post" action="/settings/api-auth">
          {csrf_field}
          <label class="checkbox-row" for="require_api_key_settings">
            <input type="checkbox" id="require_api_key_settings" name="require_api_key" value="true" {api_checkbox_state} />
            <span>Require API key for API requests</span>
          </label>
          <button type="submit">Save API authentication</button>
          <p class="help-text">{api_help_text}</p>
        </form>
      </section>

      <section>
        <h2>Retention Settings</h2>
        <form method="post" action="/settings/retention" class="retention-form">
          {csrf_field}
          <label for="retention_hours">Default retention (hours)
            <span class="tooltip" tabindex="0">?
              <span class="tooltiptext">Hours files remain available before cleanup runs.</span>
            </span>
          </label>
          <input
              id="retention_hours"
              name="retention_hours"
              type="number"
              min="1"
              max="8760"
              step="0.5"
              value="{retention_hours_value}"
              required
            />
            <span class="help-text">Equivalent to {retention_days_text} day(s).</span>
            <button type="submit">Update retention</button>
        </form>
        <div class="storage-table">
          <h3>Storage Management</h3>
          <table>
            <tbody>
              <tr>
                <th>Total storage used</th>
                <td>{total_used_text}</td>
                <td>Across all published files</td>
              </tr>
              <tr>
                <th>Active files</th>
                <td>{active_files_text}</td>
                <td>Within retention window</td>
              </tr>
              <tr>
                <th>Expired files pending cleanup</th>
                <td>{expired_files_text}</td>
                <td>Older than {retention_days_text} day(s)</td>
              </tr>
              <tr>
                <th>Storage quota limit</th>
                <td>{quota_text}</td>
                <td>Based on current volume size</td>
              </tr>
            </tbody>
          </table>
        </div>
      </section>

      <section>
        <h2>Performance Settings</h2>
          <form method="post" action="/settings/performance" class="performance-form">
            {csrf_field}
            <div class="form-grid">
              <div class="field-card">
                <label for="rate_limit_rpm">Rate limit (requests/minute)
                  <span class="tooltip" tabindex="0">?
                    <span class="tooltiptext">Maximum requests per client IP per minute.</span>
                  </span>
                </label>
                <input
                  id="rate_limit_rpm"
                  name="rate_limit_rpm"
                  type="number"
                  min="1"
                  max="100000"
                  step="1"
                  value="{rate_limit_value}"
                  required
                />
                <span class="help-text">Requests allowed per minute.</span>
              </div>
              <div class="field-card">
                <label for="ffmpeg_timeout_minutes">FFmpeg timeout (minutes)
                  <span class="tooltip" tabindex="0">?
                    <span class="tooltiptext">Terminate jobs exceeding this processing time.</span>
                  </span>
                </label>
                <input
                  id="ffmpeg_timeout_minutes"
                  name="ffmpeg_timeout_minutes"
                  type="number"
                  min="1"
                  max="720"
                  step="1"
                  value="{ffmpeg_timeout_value}"
                  required
                />
                <span class="help-text">Maximum processing duration.</span>
              </div>
              <div class="field-card">
                <label for="upload_chunk_mb">Upload chunk size (MB)
                  <span class="tooltip" tabindex="0">?
                    <span class="tooltiptext">Size of each streamed upload chunk.</span>
                  </span>
                </label>
                <input
                  id="upload_chunk_mb"
                  name="upload_chunk_mb"
                  type="number"
                  min="0.1"
                  max="512"
                  step="0.1"
                  value="{upload_chunk_value}"
                  required
                />
                <span class="help-text">Per-stream read buffer.</span>
              </div>
              <div class="field-card">
                <label for="max_file_size_mb">File size limit (MB)
                  <span class="tooltip" tabindex="0">?
                    <span class="tooltiptext">Largest upload accepted by the server.</span>
                  </span>
                </label>
                <input
                  id="max_file_size_mb"
                  name="max_file_size_mb"
                  type="number"
                  min="1"
                  max="8192"
                  step="1"
                  value="{max_file_size_value}"
                  required
                />
                <span class="help-text">Maximum upload size.</span>
              </div>
            </div>
            <button type="submit">Update performance settings</button>
          </form>
        </section>
      </div>
      <div id="processingIndicator">Processing... <span class="spinner"></span></div>
      <script>
        window.downloadBackupCodes = function downloadBackupCodes() {{
          const codes = Array.from(document.querySelectorAll('.backup-list code')).map(function (el) {{
            return el.textContent;
          }});
          if (!codes.length) {{
            alert('No backup codes available. Generate new codes first.');
            return;
          }}
          const blob = new Blob([codes.join('\n')], {{ type: 'text/plain' }});
          const url = URL.createObjectURL(blob);
          const link = document.createElement('a');
          link.href = url;
          link.download = 'ffapi-backup-codes.txt';
          document.body.appendChild(link);
          link.click();
          document.body.removeChild(link);
          URL.revokeObjectURL(url);
        }};

        document.addEventListener('DOMContentLoaded', function () {{
          const indicator = document.getElementById('processingIndicator');
          if (indicator) {{
            document.querySelectorAll('form').forEach(function (form) {{
              form.addEventListener('submit', function () {{
                indicator.style.display = 'flex';
              }});
            }});
          }}

          document.querySelectorAll('input[type="file"]').forEach(function (input) {{
            input.addEventListener('change', function (event) {{
              const file = event.target.files && event.target.files[0];
              if (!file) {{
                return;
              }}
              const maxSize = 1024 * 1024 * 1024; // 1GB
              if (file.size > maxSize) {{
                alert('File too large');
                event.target.value = '';
              }}
            }});
          }});
        }});
      </script>
    </body>
    </html>
    """


def render_api_keys_page(
    *,
    keys: List[Dict[str, Any]],
    require_key: bool,
    message: Optional[str] = None,
    error: Optional[str] = None,
    new_key: Optional[str] = None,
    csrf_token: str,
) -> str:
    alert_blocks: List[str] = []
    if message:
        alert_blocks.append(f"<div class=\"alert success\">{html.escape(message)}</div>")
    if error:
        alert_blocks.append(f"<div class=\"alert error\">{html.escape(error)}</div>")
    if new_key:
        alert_blocks.append(
            """
            <div class="alert success">
              <strong>New API key generated</strong>
              <p>Copy this value now — it will not be shown again.</p>
              <code>{key}</code>
            </div>
            """.format(key=html.escape(new_key)),
        )
    alerts = "".join(alert_blocks)

    disabled_class = "" if require_key else " is-disabled"
    disabled_attr = "" if require_key else " disabled"
    csrf_field = csrf_hidden_input(csrf_token)

    def format_timestamp(epoch: Optional[float]) -> str:
        if not epoch:
            return "Never"
        dt = datetime.fromtimestamp(epoch, tz=timezone.utc)
        return dt.strftime("%Y-%m-%d %H:%M:%S UTC")

    rows = []
    for item in keys:
        prefix = html.escape(item["prefix"])
        created = format_timestamp(item.get("created"))
        last_used = format_timestamp(item.get("last_used"))
        rows.append(
            """
            <tr>
              <td>{display}&hellip;</td>
              <td>{created}</td>
              <td>{last_used}</td>
              <td>
                <form method="post" action="/api-keys/revoke">
                  {csrf_field}
                  <input type="hidden" name="key_id" value="{key_id}" />
                  <button type="submit" class="secondary" onclick="return confirm('Are you sure you want to revoke this API key?')"{disabled}>Revoke</button>
                </form>
              </td>
            </tr>
            """.format(
                display=prefix,
                created=html.escape(created),
                last_used=html.escape(last_used),
                key_id=html.escape(str(item["id"]), quote=True),
                disabled=disabled_attr,
                csrf_field=csrf_field,
            )
        )

    if not rows:
        rows.append(
            """
            <tr>
              <td colspan="4">No API keys yet</td>
            </tr>
            """
        )

    status_text = "Enabled" if require_key else "Disabled"
    note_text = (
        "API endpoints currently require a valid key in the X-API-Key header or api_key parameter."
        if require_key
        else "API endpoints are open; enable authentication to restrict access."
    )
    status_html = html.escape(status_text)
    note_html = html.escape(note_text)
    settings_notice = ""
    if not require_key:
        settings_notice = (
            "<p class=\"help-text\">Enable API authentication in <a href=\"/settings\">Settings</a> before generating keys.</p>"
        )

    nav_html = render_nav("/api-keys")
    return f"""
    <!doctype html>
    <html>
    <head>
      <meta charset=\"utf-8\" />
      <title>API Keys</title>
      <style>
        body {{ font-family: system-ui, sans-serif; padding: 24px; max-width: 1400px; margin: 0 auto; }}
        .brand {{ font-size: 32px; font-weight: bold; margin-bottom: 20px; }}
        .brand .ff {{ color: #28a745; }}
        .brand .api {{ color: #000; }}
        nav {{ margin-bottom: 20px; }}
        nav a {{ margin-right: 15px; color: #0066cc; text-decoration: none; }}
        h2 {{ margin-top: 32px; }}
        section {{ background: #f7f9fc; padding: 20px; border-radius: 8px; box-shadow: 0 1px 2px rgba(0,0,0,0.08); margin-bottom: 24px; }}
        form {{ margin-top: 12px; }}
        label {{ display: block; font-weight: 600; margin-bottom: 6px; }}
        input {{ padding: 10px; font-size: 14px; border: 1px solid #ccd5e0; border-radius: 4px; width: 100%; box-sizing: border-box; }}
        button {{ padding: 10px 16px; background: #0066cc; color: #fff; border: none; border-radius: 4px; font-size: 14px; cursor: pointer; }}
        button:hover {{ background: #0053a3; }}
        button.secondary {{ background: #9aa5b1; }}
        button.secondary:hover {{ background: #7c8794; }}
        .alert {{ padding: 12px; border-radius: 4px; margin-bottom: 16px; font-size: 14px; }}
        .alert.success {{ background: #e8f5e9; color: #256029; border: 1px solid #c8e6c9; }}
        .alert.error {{ background: #fdecea; color: #b3261e; border: 1px solid #f7c6c4; }}
        .api-card {{ background: #fff; padding: 16px; border-radius: 8px; box-shadow: inset 0 0 0 1px #e1e7ef; display: flex; flex-direction: column; gap: 12px; }}
        .api-card h3 {{ margin: 0; font-size: 16px; color: #1f2937; }}
        .api-card-header {{ display: flex; align-items: center; justify-content: space-between; }}
        .api-card p {{ margin: 0; font-size: 14px; color: #334155; }}
        .api-card form {{ margin-top: 0; display: flex; flex-direction: column; gap: 12px; }}
        .api-card button {{ width: fit-content; }}
        .api-card.is-disabled {{ opacity: 0.5; }}
        .status-pill {{ display: inline-flex; align-items: center; gap: 6px; padding: 6px 10px; background: #e8f5e9; color: #256029; border-radius: 999px; font-weight: 600; width: fit-content; }}
        .help-text {{ font-size: 13px; color: #526079; }}
        table {{ width: 100%; border-collapse: collapse; }}
        th, td {{ padding: 10px 12px; border-bottom: 1px solid #e5edf6; font-size: 14px; text-align: left; }}
        th {{ color: #1f2937; }}
        code {{ background: #0f172a; color: #e2e8f0; padding: 6px 10px; border-radius: 4px; display: inline-block; font-size: 13px; }}
        #processingIndicator {{ display: none; position: fixed; inset: 0; background: rgba(15, 23, 42, 0.35); color: #f8fafc; font-size: 16px; font-weight: 600; align-items: center; justify-content: center; z-index: 9999; gap: 8px; }}
        #processingIndicator .spinner {{ width: 18px; height: 18px; border-radius: 50%; border: 2px solid rgba(248, 250, 252, 0.45); border-top-color: #f8fafc; animation: spin 0.8s linear infinite; }}
        @keyframes spin {{ to {{ transform: rotate(360deg); }} }}
      </style>
    </head>
    <body>
      <div class=\"brand\"><span class=\"ff\">ff</span><span class=\"api\">api</span></div>
{nav_html}
      {alerts}

      <section>
        <h2>API Keys</h2>
        <div class="api-card keys-card{disabled_class}">
          <div class="api-card-header">
            <h3>Authentication status</h3>
            <span class="status-pill">{status_html}</span>
          </div>
          <p>{note_html}</p>
          {settings_notice}
          <form method="post" action="/api-keys/generate">
            {csrf_field}
            <button type="submit"{disabled_attr}>Generate new key</button>
          </form>
          <table>
            <thead>
              <tr><th>Key</th><th>Created</th><th>Last used</th><th></th></tr>
            </thead>
            <tbody>
              {''.join(rows)}
            </tbody>
          </table>
        </div>
      </section>
      <div id="processingIndicator">Processing... <span class="spinner"></span></div>
      <script>
        document.addEventListener('DOMContentLoaded', function () {{
          const indicator = document.getElementById('processingIndicator');
          if (indicator) {{
            document.querySelectorAll('form').forEach(function (form) {{
              form.addEventListener('submit', function () {{
                indicator.style.display = 'flex';
              }});
            }});
          }}

          document.querySelectorAll('input[type="file"]').forEach(function (input) {{
            input.addEventListener('change', function (event) {{
              const file = event.target.files && event.target.files[0];
              if (!file) {{
                return;
              }}
              const maxSize = 1024 * 1024 * 1024; // 1GB
              if (file.size > maxSize) {{
                alert('File too large');
                event.target.value = '';
              }}
            }});
          }});
        }});
      </script>
    </body>
    </html>
    """


REQUEST_ID_CTX: ContextVar[Optional[str]] = ContextVar("request_id", default=None)


def cleanup_old_jobs(max_age_seconds: int = 3600, max_total_jobs: int = 1000) -> None:
    cutoff = time.time() - max_age_seconds
    with JOBS_LOCK:
        to_remove: List[str] = []
        for job_id, data in list(JOBS.items()):
            created = data.get("created")
            status = data.get("status")
            if created is None:
                to_remove.append(job_id)
                continue
            if status in {"finished", "failed"} and created < cutoff:
                to_remove.append(job_id)
        for job_id in to_remove:
            JOBS.pop(job_id, None)

        if max_total_jobs > 0 and len(JOBS) > max_total_jobs:
            finished_jobs = sorted(
                (
                    (job_id, data.get("created", 0.0))
                    for job_id, data in JOBS.items()
                    if data.get("status") in {"finished", "failed"}
                ),
                key=lambda item: item[1] or 0.0,
            )
            excess = len(JOBS) - max_total_jobs
            for job_id, _ in finished_jobs:
                if excess <= 0:
                    break
                JOBS.pop(job_id, None)
                excess -= 1


class JobProgressReporter:
    def __init__(self, job_id: str) -> None:
        self.job_id = job_id

    def update(self, percent: float, message: str, detail: Optional[str] = None) -> None:
        clamped = max(0, min(100, int(percent)))
        now = time.time()
        with JOBS_LOCK:
            data = JOBS.get(self.job_id)
            if not data or data.get("status") not in {"queued", "processing"}:
                return
            updated = dict(data)
            updated["progress"] = clamped
            updated["message"] = message
            if detail is not None:
                updated["detail"] = detail
            history = list(updated.get("history", []))
            history.append({"timestamp": now, "progress": clamped, "message": message})
            if len(history) > JOB_HISTORY_LIMIT:
                history = history[-JOB_HISTORY_LIMIT:]
            updated["history"] = history
            updated["updated"] = now
            JOBS[self.job_id] = updated


class FFmpegProgressParser:
    _TIME_PATTERN = re.compile(r"time=(\d+):(\d+):(\d+(?:\.\d+)?)")

    def __init__(
        self,
        total_seconds: float,
        reporter: JobProgressReporter,
        stage_message: str,
    ) -> None:
        self._total = max(total_seconds, 0.001)
        self._reporter = reporter
        self._stage_message = stage_message
        self._last_percent = -1.0

    def __call__(self, line: str) -> None:
        match = self._TIME_PATTERN.search(line)
        if not match:
            return
        hours, minutes, seconds = match.groups()
        try:
            elapsed = (
                int(hours) * 3600
                + int(minutes) * 60
                + float(seconds)
            )
        except ValueError:
            return
        percent = min(99.0, (elapsed / self._total) * 100.0)
        if percent <= self._last_percent + 0.5:
            return
        self._last_percent = percent
        self._reporter.update(percent, self._stage_message, detail=line.strip())


class MetricsTracker:
    def __init__(self) -> None:
        self._lock = threading.Lock()
        self._per_endpoint: Dict[str, Dict[str, float]] = defaultdict(
            lambda: {"success": 0, "failure": 0, "total_duration": 0.0}
        )
        self._error_counts: Counter[str] = Counter()
        self._recent_outcomes: deque[bool] = deque(maxlen=100)
        self._operation_history: deque[Dict[str, float]] = deque(maxlen=200)
        self._current_requests = 0
        self._max_queue_depth = 0
        self._total_wait_time = 0.0
        self._wait_samples = 0

    def reset(self) -> None:
        with self._lock:
            self._per_endpoint.clear()
            self._error_counts.clear()
            self._recent_outcomes.clear()
            self._operation_history.clear()
            self._current_requests = 0
            self._max_queue_depth = 0
            self._total_wait_time = 0.0
            self._wait_samples = 0

    def request_started(self) -> None:
        with self._lock:
            self._current_requests += 1
            if self._current_requests > self._max_queue_depth:
                self._max_queue_depth = self._current_requests

    def request_finished(
        self,
        path: str,
        status_code: int,
        duration: float,
        wait_time: float,
        error_key: Optional[str] = None,
    ) -> None:
        success = 200 <= status_code < 400
        with self._lock:
            stats = self._per_endpoint[path]
            if success:
                stats["success"] += 1
            else:
                stats["failure"] += 1
            stats["total_duration"] += duration
            self._recent_outcomes.append(success)
            self._operation_history.append(
                {
                    "timestamp": time.time(),
                    "path": path,
                    "status": status_code,
                    "duration": duration,
                }
            )
            self._total_wait_time += max(wait_time, 0.0)
            self._wait_samples += 1
            if not success:
                key = error_key or str(status_code)
                self._error_counts[key] += 1

    def request_completed(self) -> None:
        with self._lock:
            if self._current_requests > 0:
                self._current_requests -= 1

    def snapshot(self) -> Dict[str, object]:
        with self._lock:
            per_endpoint: Dict[str, Dict[str, object]] = {}
            for path, stats in self._per_endpoint.items():
                total_calls = stats["success"] + stats["failure"]
                avg_duration = (
                    stats["total_duration"] / total_calls if total_calls else 0.0
                )
                success_rate = (
                    stats["success"] / total_calls if total_calls else 0.0
                )
                per_endpoint[path] = {
                    "success": int(stats["success"]),
                    "failure": int(stats["failure"]),
                    "total": int(total_calls),
                    "avg_duration_ms": avg_duration * 1000.0,
                    "success_rate": success_rate,
                }

            total_recent = len(self._recent_outcomes)
            recent_successes = sum(1 for outcome in self._recent_outcomes if outcome)
            queue_avg_wait = (
                (self._total_wait_time / self._wait_samples)
                if self._wait_samples
                else 0.0
            )

            return {
                "per_endpoint": per_endpoint,
                "errors": dict(self._error_counts),
                "queue": {
                    "current": self._current_requests,
                    "max": self._max_queue_depth,
                    "avg_wait_ms": queue_avg_wait * 1000.0,
                },
                "recent": {
                    "window": total_recent,
                    "successes": recent_successes,
                    "failures": total_recent - recent_successes,
                    "success_rate": (
                        recent_successes / total_recent if total_recent else None
                    ),
                },
                "history": list(self._operation_history),
            }


METRICS = MetricsTracker()

# Mount static /files
app.mount("/files", StaticFiles(directory=str(PUBLIC_DIR)), name="files")


@app.middleware("http")
async def metrics_middleware(request, call_next):
    path = request.url.path
    arrival = time.perf_counter()
    request_id = request.headers.get("X-Request-ID") or uuid4().hex
    token = REQUEST_ID_CTX.set(request_id)
    bind_contextvars(request_id=request_id)
    METRICS.request_started()

    identifier = "unknown"
    if request.client:
        identifier = request.client.host or "unknown"

    if requires_api_key(request):
        provided_key = request.headers.get("X-API-Key") or request.query_params.get("api_key")
        if not provided_key:
            METRICS.request_finished(path, 401, 0.0, 0.0, "missing_api_key")
            response = JSONResponse(
                {"error": "api_key_required", "detail": "Valid API key is required"},
                status_code=401,
            )
            response.headers["X-Request-ID"] = request_id
            return response
        if not API_KEYS.verify(provided_key):
            METRICS.request_finished(path, 401, 0.0, 0.0, "invalid_api_key")
            response = JSONResponse(
                {"error": "api_key_invalid", "detail": "Supplied API key is not recognized"},
                status_code=401,
            )
            response.headers["X-Request-ID"] = request_id
            return response

    wait_time = 0.0
    processing_start: Optional[float] = None
    try:
        if not RATE_LIMITER.check(identifier):
            METRICS.request_finished(path, 429, 0.0, wait_time, "rate_limited")
            response = PlainTextResponse("Too Many Requests", status_code=429)
            response.headers["X-Request-ID"] = request_id
            return response

        processing_start = time.perf_counter()
        wait_time = processing_start - arrival
        response = await call_next(request)
        duration = time.perf_counter() - processing_start
        METRICS.request_finished(path, response.status_code, duration, wait_time)
        response.headers.setdefault("X-Request-ID", request_id)
        return response
    except HTTPException as exc:
        now = time.perf_counter()
        duration = now - processing_start if processing_start is not None else 0.0
        detail = exc.detail if isinstance(exc.detail, str) else exc.__class__.__name__
        headers = dict(exc.headers or {})
        headers.setdefault("X-Request-ID", request_id)
        exc.headers = headers
        METRICS.request_finished(path, exc.status_code, duration, wait_time, detail)
        raise
    except Exception as exc:
        now = time.perf_counter()
        duration = now - processing_start if processing_start is not None else 0.0
        METRICS.request_finished(path, 500, duration, wait_time, exc.__class__.__name__)
        raise
    finally:
        METRICS.request_completed()
        clear_contextvars()
        REQUEST_ID_CTX.reset(token)


@app.middleware("http")
async def csrf_middleware(request: Request, call_next):
    content_type = request.headers.get("content-type", "")
    content_type = content_type.split(";", 1)[0].strip().lower()
    should_validate = (
        request.method in {"POST", "PUT", "PATCH", "DELETE"}
        and content_type == "application/x-www-form-urlencoded"
    )
    if should_validate:
        body_bytes = await request.body()
        parsed = parse_qs(body_bytes.decode("utf-8")) if body_bytes else {}
        token_list = parsed.get("csrf_token")
        token = token_list[0] if token_list else None
        cookie_token = request.cookies.get(csrf_protect.cookie_name)
        try:
            csrf_protect.validate_csrf(token, cookie_token)
        except CsrfProtectException as exc:
            return JSONResponse({"detail": exc.message}, status_code=exc.status_code)

        async def receive():
            return {"type": "http.request", "body": body_bytes, "more_body": False}

        request._receive = receive
    response = await call_next(request)
    token_to_set = getattr(request.state, "_csrf_token_to_set", None)
    if token_to_set:
        csrf_protect.set_csrf_cookie(response, token_to_set)
    elif not request.cookies.get(csrf_protect.cookie_name):
        csrf_protect.set_csrf_cookie(response, csrf_protect.generate_csrf())
    return response


@app.middleware("http")
async def security_headers_middleware(request, call_next):
    response = await call_next(request)
    response.headers.setdefault(
        "Content-Security-Policy",
        "default-src 'self'; style-src 'self' 'unsafe-inline'; script-src 'self' 'unsafe-inline'; img-src 'self' data:",
    )
    response.headers.setdefault("X-Content-Type-Options", "nosniff")
    response.headers.setdefault("X-Frame-Options", "DENY")
    return response


# Setup logging to file
import logging
import sys

# Force unbuffered output when supported
try:
    sys.stdout.reconfigure(line_buffering=True)
    sys.stderr.reconfigure(line_buffering=True)
except (AttributeError, io.UnsupportedOperation):
    # Some deployment targets (e.g. WSGI, Windows) don't expose reconfigure
    pass

# Create file handler with line buffering to avoid manual flush storms
file_stream = open(APP_LOG_FILE, "a", encoding="utf-8", buffering=1)
atexit.register(file_stream.close)


class RequestIdFilter(logging.Filter):
    def filter(self, record: logging.LogRecord) -> bool:  # pragma: no cover - logging infrastructure
        record.request_id = REQUEST_ID_CTX.get(None) or "-"
        return True


log_format = '%(asctime)s - %(name)s - %(levelname)s - [%(request_id)s] %(message)s'
request_id_filter = RequestIdFilter()

file_handler = logging.StreamHandler(file_stream)
file_handler.setLevel(logging.INFO)
file_handler.addFilter(request_id_filter)
file_handler.setFormatter(logging.Formatter(log_format))

# Create console handler
console_handler = logging.StreamHandler(sys.stdout)
console_handler.setLevel(logging.INFO)
console_handler.addFilter(request_id_filter)
console_handler.setFormatter(logging.Formatter(log_format))

# Configure root logger to catch everything (including uvicorn)
logging.basicConfig(
    level=logging.INFO,
    handlers=[file_handler, console_handler]
)

logger = logging.getLogger("ffapi")

if structlog is not None:  # pragma: no branch - configuration depends on availability
    structlog.configure(
        processors=[
            structlog.contextvars.merge_contextvars,
            structlog.processors.add_log_level,
            structlog.processors.StackInfoRenderer(),
            structlog.processors.format_exc_info,
            structlog.processors.TimeStamper(fmt="iso", utc=True),
            structlog.processors.JSONRenderer(),
        ],
        wrapper_class=structlog.make_filtering_bound_logger(logging.INFO),
        context_class=dict,
        logger_factory=structlog.PrintLoggerFactory(),
        cache_logger_on_first_use=False,
    )
    struct_logger = structlog.get_logger("ffapi")
else:
    class _FallbackStructLogger:
        def __init__(self, base_logger: logging.Logger) -> None:
            self._logger = base_logger

        def info(self, event: str, **context: Any) -> None:
            self._logger.info("%s %s", event, context)

        def error(self, event: str, **context: Any) -> None:
            self._logger.error("%s %s", event, context)

    struct_logger = _FallbackStructLogger(logger)

# Also capture uvicorn logs
uvicorn_logger = logging.getLogger("uvicorn")
uvicorn_logger.addHandler(file_handler)
uvicorn_access = logging.getLogger("uvicorn.access")
uvicorn_access.addHandler(file_handler)

# Log startup
logger.info("="*60)
logger.info("FFAPI Ultimate starting...")
logger.info(f"PUBLIC_DIR: {PUBLIC_DIR}")
logger.info(f"WORK_DIR: {WORK_DIR}")
logger.info(f"LOGS_DIR: {LOGS_DIR}")
logger.info(f"RETENTION_DAYS: {RETENTION_DAYS}")
logger.info(f"PUBLIC_BASE_URL: {PUBLIC_BASE_URL or 'Not set'}")
logger.info("="*60)

# Startup event to log when server is ready
@app.on_event("startup")
async def startup_event():
    logger.info("FastAPI server is ready to accept requests")
    try:
        cleanup_old_public()
    except Exception as exc:
        logger.warning("Initial public cleanup failed: %s", exc)
    if PUBLIC_CLEANUP_INTERVAL_SECONDS > 0:
        asyncio.create_task(_periodic_public_cleanup())
        asyncio.create_task(_periodic_jobs_cleanup())
    asyncio.create_task(_periodic_session_cleanup())
    asyncio.create_task(_periodic_rate_limiter_cleanup())
    if PROBE_CACHE_TTL > 0:
        asyncio.create_task(_periodic_cache_cleanup())
    _flush_logs()


def _rand(n=8):
    import random, string
    return "".join(random.choices(string.digits, k=n))


def _flush_logs():
    """Force flush all log handlers."""
    for handler in logging.getLogger().handlers:
        handler.flush()


def tail_file(filepath: Path, num_lines: int = 1000) -> str:
    try:
        size = filepath.stat().st_size
    except FileNotFoundError:
        return ""
    except Exception as exc:
        logger.warning("Failed to stat log file %s: %s", filepath, exc)
        return "Error reading log file"

    if size <= 10 * 1024 * 1024:  # 10MB
        try:
            with filepath.open("r", encoding="utf-8", errors="ignore") as handle:
                limited = deque(handle, maxlen=num_lines)
            return "".join(limited) if limited else ""
        except Exception as exc:
            logger.warning("Failed to read log file %s: %s", filepath, exc)
            return "Error reading log file"

    # Large file: read from the end in blocks
    block_size = 8192
    blocks: List[bytes] = []
    bytes_to_read = size
    lines_found = 0
    try:
        with filepath.open("rb") as handle:
            while bytes_to_read > 0 and lines_found <= num_lines:
                read_size = block_size if bytes_to_read > block_size else bytes_to_read
                handle.seek(bytes_to_read - read_size)
                data = handle.read(read_size)
                if not data:
                    break
                blocks.append(data)
                bytes_to_read -= read_size
                lines_found += data.count(b"\n")
    except Exception as exc:
        logger.warning("Failed to stream log file %s: %s", filepath, exc)
        return "Error reading log file"

    text = b"".join(reversed(blocks)).decode("utf-8", errors="ignore")
    return "\n".join(text.splitlines()[-num_lines:])


def check_disk_space(path: Path, required_mb: int = MIN_FREE_SPACE_MB) -> None:
    target = path if path.exists() else path.parent
    target.mkdir(parents=True, exist_ok=True)
    stat = shutil.disk_usage(target)
    available_mb = stat.free / (1024 * 1024)
    if available_mb < required_mb:
        logger.warning(
            "Insufficient disk space at %s: %.1f MB available, %d MB required",
            target,
            available_mb,
            required_mb,
        )
        _flush_logs()
        raise HTTPException(status_code=507, detail="Insufficient disk space")


def disk_snapshot() -> Dict[str, Dict[str, float]]:
    snapshot: Dict[str, Dict[str, float]] = {}
    targets = {
        "public": PUBLIC_DIR,
        "work": WORK_DIR,
        "logs": LOGS_DIR,
    }
    for name, target in targets.items():
        try:
            usage = shutil.disk_usage(target)
            snapshot[name] = {
                "total_mb": usage.total / (1024 * 1024),
                "used_mb": usage.used / (1024 * 1024),
                "available_mb": usage.free / (1024 * 1024),
            }
        except FileNotFoundError:
            snapshot[name] = {"error": "not_found"}
        except Exception as exc:
            snapshot[name] = {"error": str(exc)}
    return snapshot


def memory_snapshot() -> Dict[str, Optional[float]]:
    rss_bytes: Optional[float] = None
    try:
        import resource

        usage = resource.getrusage(resource.RUSAGE_SELF)
        rss_bytes = float(usage.ru_maxrss)
        if sys.platform != "darwin":
            rss_bytes *= 1024.0
    except Exception:
        rss_bytes = None

    total_bytes: Optional[float] = None
    available_bytes: Optional[float] = None
    try:
        with open("/proc/meminfo", "r", encoding="utf-8") as handle:
            values = {}
            for line in handle:
                if ":" not in line:
                    continue
                key, rest = line.split(":", 1)
                values[key.strip()] = rest.strip()
        if "MemTotal" in values:
            total_bytes = float(values["MemTotal"].split()[0]) * 1024.0
        if "MemAvailable" in values:
            available_bytes = float(values["MemAvailable"].split()[0]) * 1024.0
        elif "MemFree" in values:
            available_bytes = float(values["MemFree"].split()[0]) * 1024.0
    except Exception:
        pass

    def to_mb(value: Optional[float]) -> Optional[float]:
        return (value / (1024.0 * 1024.0)) if value is not None else None

    return {
        "rss_mb": to_mb(rss_bytes),
        "total_mb": to_mb(total_bytes),
        "available_mb": to_mb(available_bytes),
    }


def ffmpeg_snapshot() -> Dict[str, Optional[str]]:
    global _FFMPEG_VERSION_CACHE
    if _FFMPEG_VERSION_CACHE is not None:
        return dict(_FFMPEG_VERSION_CACHE)
    try:
        result = subprocess.run(
            ["ffmpeg", "-version"], capture_output=True, text=True, timeout=5
        )
        available = result.returncode == 0
        version_line = (result.stdout or "").splitlines()[0] if available else ""
        error = None if available else (result.stderr or "Unknown failure")
    except Exception as exc:
        available = False
        version_line = ""
        error = str(exc)
    snapshot = {"available": available, "version": version_line, "error": error}
    _FFMPEG_VERSION_CACHE = dict(snapshot)
    return snapshot


def safe_path_check(base: Path, rel: str) -> Path:
    try:
        target = (base / rel).resolve()
    except Exception as exc:
        logger.warning("Invalid path provided for %s: %s", base, rel)
        _flush_logs()
        raise HTTPException(status_code=400, detail="Invalid path") from exc
    if target == base:
        return target
    if base not in target.parents:
        logger.warning("Blocked path traversal attempt: %s -> %s", rel, target)
        _flush_logs()
        raise HTTPException(status_code=403, detail="Access denied")
    return target


async def stream_upload_to_path(upload: UploadFile, dest: Path) -> int:
    await upload.seek(0)
    total = 0
    try:
        header_value = upload.headers.get("content-length") if upload.headers else None
    except AttributeError:
        header_value = None
    if header_value:
        try:
            declared_length = int(header_value)
        except (TypeError, ValueError):
            logger.warning("Invalid content-length header on upload %s: %s", upload.filename, header_value)
        else:
            if declared_length > MAX_FILE_SIZE_BYTES:
                logger.warning(
                    "Upload %s declared size %s exceeds max bytes %s",
                    upload.filename,
                    declared_length,
                    MAX_FILE_SIZE_BYTES,
                )
                raise HTTPException(status_code=413, detail="File too large")
    try:
        with dest.open("wb") as buffer:
            while chunk := await upload.read(UPLOAD_CHUNK_SIZE):
                total += len(chunk)
                if total > MAX_FILE_SIZE_BYTES:
                    logger.warning("Upload exceeded max size: %s", upload.filename)
                    _flush_logs()
                    raise HTTPException(status_code=413, detail="File too large")
                buffer.write(chunk)
    except HTTPException:
        if dest.exists():
            try:
                dest.unlink()
            except Exception as exc:
                logger.warning("Failed cleaning partial upload %s: %s", dest, exc)
        raise
    except Exception as exc:
        if dest.exists():
            try:
                dest.unlink()
            except Exception as cleanup_exc:
                logger.warning("Failed removing incomplete upload %s: %s", dest, cleanup_exc)
        logger.error("Failed to persist upload %s: %s", upload.filename, exc)
        _flush_logs()
        raise HTTPException(status_code=500, detail="Failed to save upload") from exc
    return total


def ensure_upload_type(upload: UploadFile, expected_prefix: str, field: str) -> None:
    content_type = (upload.content_type or "").lower()
    if not content_type.startswith(expected_prefix):
        logger.warning(
            "%s upload rejected due to invalid content-type: %s",
            field,
            content_type or "unknown",
        )
        raise HTTPException(
            status_code=400,
            detail=f"{field} must be {expected_prefix}*, got {content_type or 'unknown'}",
        )


def run_ffmpeg_with_timeout(
    cmd: List[str],
    log_handle,
    *,
    progress_parser: Optional[Callable[[str], None]] = None,
) -> int:
    reader_thread: Optional[threading.Thread] = None
    reader_error: Optional[BaseException] = None

    try:
        proc = subprocess.Popen(
            cmd,
            stdout=log_handle,
            stderr=subprocess.PIPE,
            text=True,
            encoding="utf-8",
            errors="ignore",
            bufsize=1,
        )
    except Exception as exc:
        logger.error("Failed to launch ffmpeg command %s: %s", cmd, exc)
        _flush_logs()
        raise HTTPException(status_code=500, detail="Failed to start ffmpeg") from exc

    if proc.stderr is not None:
        def _reader() -> None:
            nonlocal reader_error
            try:
                for line in proc.stderr:  # pragma: no branch - sequential iteration
                    try:
                        log_handle.write(line)
                        if hasattr(log_handle, "flush"):
                            log_handle.flush()
                    except Exception as log_exc:  # pragma: no cover - defensive logging
                        reader_error = log_exc
                    if progress_parser is not None:
                        try:
                            progress_parser(line)
                        except Exception:
                            logger.debug("Progress parser failed for line: %s", line.strip())
            finally:
                try:
                    proc.stderr.close()
                except Exception:
                    pass

        reader_thread = threading.Thread(target=_reader, daemon=True)
        reader_thread.start()

    try:
        return proc.wait(timeout=FFMPEG_TIMEOUT_SECONDS)
    except subprocess.TimeoutExpired:
        proc.terminate()
        try:
            proc.wait(timeout=5)
        except subprocess.TimeoutExpired:
            proc.kill()
            try:
                proc.wait(timeout=1)
            except subprocess.TimeoutExpired:
                pass
        logger.warning("FFmpeg timed out after %s seconds: %s", FFMPEG_TIMEOUT_SECONDS, cmd)
        _flush_logs()
        raise HTTPException(status_code=504, detail="Processing timeout")
    finally:
        if reader_thread is not None:
            reader_thread.join(timeout=1)
        if reader_error is not None:
            logger.debug("FFmpeg log writer encountered error: %s", reader_error)


def generate_video_thumbnail(video_path: Path) -> Optional[Path]:
    suffix = video_path.suffix.lower()
    if suffix not in VIDEO_THUMBNAIL_EXTENSIONS:
        return None
    thumb_dir = video_path.parent / THUMBNAIL_DIR_NAME
    try:
        thumb_dir.mkdir(parents=True, exist_ok=True)
    except Exception as exc:
        logger.debug("Unable to create thumbnail directory %s: %s", thumb_dir, exc)
        return None
    thumb_path = thumb_dir / f"{video_path.stem}.jpg"
    temp_log = WORK_DIR / f"thumb_{video_path.stem}.log"
    cmd = [
        "ffmpeg",
        "-y",
        "-i",
        str(video_path),
        "-ss",
        "00:00:01",
        "-frames:v",
        "1",
        "-vf",
        "scale=320:-1",
        str(thumb_path),
    ]
    try:
        with temp_log.open("w", encoding="utf-8", errors="ignore") as log_handle:
            code = run_ffmpeg_with_timeout(cmd, log_handle)
    except HTTPException:
        if thumb_path.exists():
            try:
                thumb_path.unlink()
            except Exception as exc:
                logger.warning("Failed to remove thumbnail %s: %s", thumb_path, exc)
        return None
    finally:
        if temp_log.exists():
            try:
                temp_log.unlink()
            except Exception:
                pass
    if code != 0 or not thumb_path.exists():
        if thumb_path.exists():
            try:
                thumb_path.unlink()
            except Exception as exc:
                logger.warning("Failed to remove thumbnail %s: %s", thumb_path, exc)
        return None
    return thumb_path

def cleanup_old_public(days: Optional[float] = None, *, batch_size: Optional[int] = None) -> None:
    """Delete files older than specified days based on actual file modification time."""
    retention_days = RETENTION_DAYS if days is None else days
    if retention_days <= 0:
        return
    cutoff_timestamp = time.time() - (retention_days * 86400)

    remaining = None if batch_size is None or batch_size <= 0 else int(batch_size)
    deleted_count = 0
    for child in PUBLIC_DIR.iterdir():
        if remaining is not None and remaining <= 0:
            break
        if not child.is_dir():
            continue

        files_to_delete: List[Path] = []
        try:
            for file_path in child.iterdir():
                if remaining is not None and remaining <= 0:
                    break
                if not file_path.is_file():
                    continue
                try:
                    file_mtime = file_path.stat().st_mtime
                    if file_mtime < cutoff_timestamp:
                        files_to_delete.append(file_path)
                except Exception as exc:
                    logger.warning("Failed to inspect file %s: %s", file_path, exc)
        except Exception as exc:
            logger.warning("Failed to scan directory %s: %s", child, exc)
            continue

        for fp in files_to_delete:
            try:
                fp.unlink()
                deleted_count += 1
                if remaining is not None:
                    remaining -= 1
                    if remaining <= 0:
                        break
            except Exception as exc:
                logger.warning("Failed to delete expired file %s: %s", fp, exc)
        if remaining is not None and remaining <= 0:
            break

        try:
            if not any(child.iterdir()):
                child.rmdir()
        except Exception as exc:
            logger.warning("Failed to remove empty directory %s: %s", child, exc)
    
    if deleted_count > 0:
        logger.info(
            "Cleanup: deleted %s files older than %s days", deleted_count, retention_days
        )


async def _periodic_public_cleanup():
    """Periodically clean up expired files from the public directory."""
    try:
        # Stagger the first run so startup work can finish
        await asyncio.sleep(PUBLIC_CLEANUP_INTERVAL_SECONDS)
    except asyncio.CancelledError:
        raise
    while True:
        try:
            cleanup_old_public(batch_size=100)
        except asyncio.CancelledError:
            raise
        except Exception as exc:
            logger.warning("Periodic public cleanup failed: %s", exc)
        try:
            await asyncio.sleep(PUBLIC_CLEANUP_INTERVAL_SECONDS)
        except asyncio.CancelledError:
            raise


async def _periodic_jobs_cleanup():
    """Periodically purge completed jobs from the in-memory registry."""
    try:
        await asyncio.sleep(3600)
    except asyncio.CancelledError:
        raise
    while True:
        try:
            cleanup_old_jobs()
        except asyncio.CancelledError:
            raise
        except Exception as exc:
            logger.warning("Periodic jobs cleanup failed: %s", exc)
        try:
            await asyncio.sleep(3600)
        except asyncio.CancelledError:
            raise


async def _periodic_cache_cleanup():
    try:
        await asyncio.sleep(1800)
    except asyncio.CancelledError:
        raise
    while True:
        try:
            cutoff = time.time() - PROBE_CACHE_TTL
            expired: List[str] = []
            with PROBE_CACHE_LOCK:
                for key, (_, timestamp) in list(PROBE_CACHE.items()):
                    if timestamp < cutoff:
                        expired.append(key)
                for key in expired:
                    PROBE_CACHE.pop(key, None)
            if expired:
                logger.info("Cleaned %d expired probe cache entries", len(expired))
        except asyncio.CancelledError:
            raise
        except Exception as exc:
            logger.warning("Cache cleanup failed: %s", exc)
        try:
            await asyncio.sleep(1800)
        except asyncio.CancelledError:
            raise


async def _periodic_session_cleanup():
    """Periodically reap expired UI authentication sessions."""
    try:
        await asyncio.sleep(1800)
    except asyncio.CancelledError:
        raise
    while True:
        try:
            UI_AUTH.cleanup_expired_sessions()
        except asyncio.CancelledError:
            raise
        except Exception as exc:
            logger.warning("Session cleanup failed: %s", exc)
        try:
            await asyncio.sleep(1800)
        except asyncio.CancelledError:
            raise


async def _periodic_rate_limiter_cleanup():
    """Periodically purge stale identifiers from the rate limiter cache."""
    try:
        await asyncio.sleep(300)
    except asyncio.CancelledError:
        raise
    while True:
        try:
            RATE_LIMITER.cleanup_old_identifiers()
        except asyncio.CancelledError:
            raise
        except Exception as exc:
            logger.warning("Rate limiter cleanup failed: %s", exc)
        try:
            await asyncio.sleep(300)
        except asyncio.CancelledError:
            raise


def publish_file(src: Path, ext: str, *, duration_ms: Optional[int] = None) -> Dict[str, str]:
    """Move a finished file into PUBLIC_DIR/YYYYMMDD/ and return URLs/paths.
       Uses shutil.move to be cross-device safe (works across Docker volumes/Windows)."""
    if not ext or not re.fullmatch(r"\.[A-Za-z0-9]+", ext):
        raise ValueError(f"Invalid file extension: {ext}")
    check_disk_space(PUBLIC_DIR)
    now = datetime.now(timezone.utc)
    day = now.strftime("%Y%m%d")
    folder = PUBLIC_DIR / day
    folder.mkdir(parents=True, exist_ok=True)
    check_disk_space(folder)
    name = now.strftime("%Y%m%d_%H%M%S_") + _rand() + ext
    dst = folder / name

    # Cross-device safe move
    shutil.move(str(src), str(dst))

    size_mb = dst.stat().st_size / (1024 * 1024)
    logger.info(f"Published file: {name} ({size_mb:.2f} MB)")
    struct_logger.info(
        "file_published",
        filename=name,
        size_mb=round(size_mb, 4),
        duration_ms=duration_ms,
    )

    thumbnail_rel: Optional[str] = None
    try:
        thumb_path = generate_video_thumbnail(dst)
    except Exception as exc:  # pragma: no cover - defensive logging
        logger.debug("Thumbnail generation failed for %s: %s", dst, exc)
    else:
        if thumb_path:
            thumbnail_rel = f"/files/{day}/{THUMBNAIL_DIR_NAME}/{thumb_path.name}"

    rel = f"/files/{day}/{name}"
    url = f"{PUBLIC_BASE_URL.rstrip('/')}{rel}" if PUBLIC_BASE_URL else rel
    return {"dst": str(dst), "url": url, "rel": rel, "thumbnail": thumbnail_rel}


def save_log(log_path: Path, operation: str) -> None:
    """Save ffmpeg log to persistent logs directory."""
    if not log_path.exists():
        return
    check_disk_space(LOGS_DIR)
    now = datetime.now(timezone.utc)
    day = now.strftime("%Y%m%d")
    folder = LOGS_DIR / day
    folder.mkdir(parents=True, exist_ok=True)
    check_disk_space(folder)
    name = now.strftime("%Y%m%d_%H%M%S_") + _rand() + f"_{operation}.log"
    dst = folder / name
    try:
        shutil.copy2(str(log_path), str(dst))
    except Exception:
        pass


# ---------- pages ----------
@app.get("/", include_in_schema=False)
def root():
    return RedirectResponse(url="/downloads", status_code=302)


@app.get("/downloads", response_class=HTMLResponse)
def downloads(
    request: Request,
    page: int = Query(1, ge=1),
    page_size: int = Query(25, ge=1, le=100),
    q: str = "",
):
    redirect = ensure_dashboard_access(request)
    if redirect:
        return redirect

    search = q.strip()
    search_lower = search.lower()
    entries: List[Dict[str, Any]] = []
    for day in sorted(PUBLIC_DIR.iterdir(), reverse=True):
        if not day.is_dir():
            continue
        for file_path in sorted(day.iterdir(), reverse=True):
            if not file_path.is_file():
                continue
            name = file_path.name
            if search and search_lower not in name.lower() and search_lower not in day.name.lower():
                continue
            rel = f"/files/{day.name}/{name}"
            stat = file_path.stat()
            size_mb = stat.st_size / (1024 * 1024)
            thumb_path = file_path.parent / THUMBNAIL_DIR_NAME / f"{file_path.stem}.jpg"
            thumb_rel = None
            if thumb_path.exists():
                thumb_rel = f"/files/{day.name}/{THUMBNAIL_DIR_NAME}/{thumb_path.name}"
            entries.append(
                {
                    "day": day.name,
                    "name": name,
                    "rel": rel,
                    "size": size_mb,
                    "thumb": thumb_rel,
                    "mtime": stat.st_mtime,
                }
            )

    entries.sort(key=lambda item: item["mtime"], reverse=True)
    total_items = len(entries)
    total_pages = max(1, math.ceil(total_items / page_size)) if total_items else 1
    page = min(max(page, 1), total_pages)
    start = (page - 1) * page_size
    end = start + page_size
    current_entries = entries[start:end]

    rows: List[str] = []
    for item in current_entries:
        thumb_html = (
            f"<img src=\"{html.escape(item['thumb'], quote=True)}\" alt=\"Thumbnail\" class=\"thumb-image\" />"
            if item["thumb"]
            else "<span class=\"thumb-placeholder\">—</span>"
        )
        rows.append(
            """
            <tr>
              <td>
                <div class="file-entry">
                  {thumb}
                  <div class="file-meta">
                    <a href="{href}">{name}</a>
                    <div class="file-size">{size:.2f} MB</div>
                  </div>
                </div>
              </td>
              <td class="date-cell">{day}</td>
            </tr>
            """.format(
                day=html.escape(item["day"]),
                href=html.escape(item["rel"], quote=True),
                name=html.escape(item["name"]),
                size=item["size"],
                thumb=thumb_html,
            )
        )

    if not rows:
        if total_items:
            rows.append("<tr><td colspan='3'>No files match your filters.</td></tr>")
        else:
            rows.append("<tr><td colspan='3'>No files yet</td></tr>")

    def build_page_url(target_page: int) -> str:
        params: Dict[str, Any] = {"page": target_page}
        if page_size != 25:
            params["page_size"] = page_size
        if search:
            params["q"] = search
        query = urlencode(params)
        return f"/downloads?{query}" if query else "/downloads"

    summary_text = ""
    if total_items:
        display_start = start + 1
        display_end = min(end, total_items)
        summary_text = f"Showing {display_start}-{display_end} of {total_items} file(s)"
    elif search:
        summary_text = "No files match your filters"

    pagination_links = []
    if page > 1:
        pagination_links.append(
            f"<a href=\"{html.escape(build_page_url(page - 1), quote=True)}\" class=\"pager-link\">&laquo; Prev</a>"
        )
    if page < total_pages:
        pagination_links.append(
            f"<a href=\"{html.escape(build_page_url(page + 1), quote=True)}\" class=\"pager-link\">Next &raquo;</a>"
        )
    pagination_html = (
        "<div class=\"pagination\">" + "".join(pagination_links) + f"<span>Page {page} of {total_pages}</span></div>"
        if total_items
        else ""
    )

    clear_link = (
        f"<a class=\"clear-link\" href=\"/downloads?page_size={page_size}\">Clear</a>" if search else ""
    )
    search_value = html.escape(search, quote=True)

    nav_html = render_nav("/downloads")
    html_content = f"""
    <!doctype html>
    <html>
    <head>
      <meta charset="utf-8" />
      <title>Downloads</title>
      <style>
        body {{ font-family: system-ui, sans-serif; padding: 24px; max-width: 1400px; margin: 0 auto; }}
        .brand {{ font-size: 32px; font-weight: bold; margin-bottom: 20px; }}
        .brand .ff {{ color: #28a745; }}
        .brand .api {{ color: #000; }}
        nav {{ margin-bottom: 20px; }}
        nav a {{ margin-right: 15px; color: #0066cc; text-decoration: none; }}
        table {{ border-collapse: collapse; width: 100%; background: #fff; box-shadow: 0 1px 2px rgba(15,23,42,0.08); }}
        th, td {{ border-bottom: 1px solid #e2e8f0; padding: 12px 14px; font-size: 14px; vertical-align: top; }}
        th {{ text-align: left; background: #f8fafc; color: #1f2937; }}
        a {{ text-decoration: none; color: #0f172a; }}
        .filters {{ display: flex; align-items: center; gap: 12px; margin-bottom: 16px; }}
        .filters input[type="text"] {{ flex: 1; padding: 10px; border: 1px solid #cbd5f5; border-radius: 6px; }}
        .filters button {{ padding: 10px 16px; background: #0066cc; color: #fff; border: none; border-radius: 6px; cursor: pointer; }}
        .filters button:hover {{ background: #0053a3; }}
        .summary {{ margin-bottom: 12px; font-size: 14px; color: #475569; }}
        .pagination {{ display: flex; gap: 12px; align-items: center; margin-top: 16px; font-size: 14px; color: #475569; }}
        .pager-link {{ color: #0369a1; }}
        .file-entry {{ display: flex; gap: 12px; align-items: center; }}
        .thumb-image {{ width: 96px; height: 54px; object-fit: cover; border-radius: 4px; border: 1px solid #cbd5f5; background: #f8fafc; }}
        .thumb-placeholder {{ display: inline-flex; width: 96px; height: 54px; align-items: center; justify-content: center; border: 1px dashed #cbd5f5; border-radius: 4px; color: #94a3b8; font-size: 12px; }}
        .file-meta {{ display: flex; flex-direction: column; gap: 4px; }}
        .file-size {{ font-size: 13px; color: #64748b; }}
        .date-cell {{ width: 140px; color: #475569; font-size: 13px; }}
        .clear-link {{ font-size: 13px; color: #64748b; }}
      </style>
    </head>
    <body>
      <div class="brand"><span class="ff">ff</span><span class="api">api</span></div>
{nav_html}
      <h2>Generated Files</h2>
      <form method="get" class="filters">
        <input type="text" name="q" value="{search_value}" placeholder="Search by filename or date" />
        <input type="hidden" name="page_size" value="{page_size}" />
        <button type="submit">Search</button>
        {clear_link}
      </form>
      <div class="summary">{html.escape(summary_text) if summary_text else ''}</div>
      <table>
        <thead><tr><th>File</th><th>Date</th></tr></thead>
        <tbody>{"".join(rows)}</tbody>
      </table>
      {pagination_html}
    </body>
    </html>
    """
    return HTMLResponse(html_content)


@app.get("/logs", response_class=HTMLResponse)
def logs(request: Request, page: int = Query(1, ge=1), page_size: int = Query(25, ge=1, le=200)):
    redirect = ensure_dashboard_access(request)
    if redirect:
        return redirect

    entries: List[Dict[str, Any]] = []
    for day in sorted(LOGS_DIR.iterdir(), reverse=True):
        if not day.is_dir():
            continue
        for file_path in sorted(day.iterdir(), reverse=True):
            if not file_path.is_file():
                continue
            stat = file_path.stat()
            size_kb = stat.st_size / 1024
            parts = file_path.name.split("_")
            operation = parts[-1].replace(".log", "") if len(parts) > 3 else "unknown"
            entries.append(
                {
                    "day": day.name,
                    "name": file_path.name,
                    "operation": operation,
                    "size": size_kb,
                    "path": f"{day.name}/{file_path.name}",
                    "mtime": stat.st_mtime,
                }
            )

    entries.sort(key=lambda item: item["mtime"], reverse=True)
    total_items = len(entries)
    total_pages = max(1, math.ceil(total_items / page_size)) if total_items else 1
    page = min(max(page, 1), total_pages)
    start = (page - 1) * page_size
    end = start + page_size
    current_entries = entries[start:end]

    rows: List[str] = []
    for item in current_entries:
        rows.append(
            """
            <tr>
              <td>{day}</td>
              <td>{name}</td>
              <td>{operation}</td>
              <td>{size:.1f} KB</td>
              <td><a href="/logs/view?path={path}" target="_blank">View</a></td>
            </tr>
            """.format(
                day=html.escape(item["day"]),
                name=html.escape(item["name"]),
                operation=html.escape(item["operation"]),
                size=item["size"],
                path=html.escape(item["path"], quote=True),
            )
        )

    if not rows:
        rows.append("<tr><td colspan='5'>No logs yet</td></tr>")

    def build_page_url(target_page: int) -> str:
        params = {"page": target_page, "page_size": page_size}
        query = urlencode(params)
        return f"/logs?{query}" if query else "/logs"

    pagination_links = []
    if page > 1:
        pagination_links.append(
            f"<a href=\"{html.escape(build_page_url(page - 1), quote=True)}\" class=\"pager-link\">&laquo; Prev</a>"
        )
    if page < total_pages:
        pagination_links.append(
            f"<a href=\"{html.escape(build_page_url(page + 1), quote=True)}\" class=\"pager-link\">Next &raquo;</a>"
        )
    pagination_html = (
        "<div class=\"pagination\">" + "".join(pagination_links) + f"<span>Page {page} of {total_pages}</span></div>"
        if total_items
        else ""
    )

    nav_html = render_nav("/logs")
    html_content = f"""
    <!doctype html>
    <html>
    <head>
      <meta charset="utf-8" />
      <title>FFmpeg Logs</title>
      <style>
        body {{ font-family: system-ui, sans-serif; padding: 24px; max-width: 1400px; margin: 0 auto; }}
        .brand {{ font-size: 32px; font-weight: bold; margin-bottom: 20px; }}
        .brand .ff {{ color: #28a745; }}
        .brand .api {{ color: #000; }}
        nav {{ margin-bottom: 20px; }}
        nav a {{ margin-right: 15px; color: #0066cc; text-decoration: none; }}
        table {{ border-collapse: collapse; width: 100%; background: #fff; box-shadow: 0 1px 2px rgba(15,23,42,0.08); }}
        th, td {{ border-bottom: 1px solid #e2e8f0; padding: 12px 14px; font-size: 14px; }}
        th {{ text-align: left; background: #f8fafc; color: #1f2937; }}
        a {{ text-decoration: none; color: #0f172a; }}
        .pagination {{ display: flex; gap: 12px; align-items: center; margin-top: 16px; font-size: 14px; color: #475569; }}
        .pager-link {{ color: #0369a1; }}
      </style>
    </head>
    <body>
      <div class="brand"><span class="ff">ff</span><span class="api">api</span></div>
{nav_html}
      <h2>FFmpeg Logs</h2>
      <table>
        <thead><tr><th>Date</th><th>Filename</th><th>Operation</th><th>Size</th><th>Action</th></tr></thead>
        <tbody>{"".join(rows)}</tbody>
      </table>
      {pagination_html}
    </body>
    </html>
    """
    return HTMLResponse(html_content)


@app.get("/logs/view", response_class=PlainTextResponse)
def view_log(request: Request, path: str, max_size_mb: int = 10):
    """View individual log file content."""
    redirect = ensure_dashboard_access(request)
    if redirect:
        return redirect
    target = safe_path_check(LOGS_DIR, path)
    if not target.exists() or not target.is_file():
        raise HTTPException(status_code=404, detail="Log not found")

    size_mb = target.stat().st_size / (1024 * 1024)
    if size_mb > max_size_mb:
        raise HTTPException(
            status_code=413,
            detail=f"Log file too large ({size_mb:.1f}MB). Maximum {max_size_mb}MB.",
        )

    return target.read_text(encoding="utf-8", errors="ignore")


@app.get("/ffmpeg", response_class=HTMLResponse)
def ffmpeg_info(request: Request, auto_refresh: int = 0):
    """Display FFmpeg version and container logs.

    Args:
        auto_refresh: Auto-refresh interval in seconds (0 = disabled, max 60)
    """
    redirect = ensure_dashboard_access(request)
    if redirect:
        return redirect
    # Get version
    version_proc = subprocess.run(["ffmpeg", "-version"], capture_output=True, text=True)
    version_output = version_proc.stdout if version_proc.returncode == 0 else "Error getting version"
    
    # Force flush all handlers before reading
    for handler in logging.getLogger().handlers:
        handler.flush()
    
    # Get application logs (last 1000 lines)
    app_logs = ""
    log_info = ""
    if APP_LOG_FILE.exists():
        try:
            stat = APP_LOG_FILE.stat()
            size_kb = stat.st_size / 1024
            mtime = datetime.fromtimestamp(stat.st_mtime, tz=timezone.utc).strftime("%Y-%m-%d %H:%M:%S UTC")
            log_info = f"Log file: {size_kb:.1f} KB, Last modified: {mtime}"
            app_logs = tail_file(APP_LOG_FILE, 1000) or "No logs yet"
        except Exception as e:
            app_logs = f"Error reading logs: {e}"
            log_info = "Error reading log file"
    else:
        app_logs = "Log file not created yet"
        log_info = "Waiting for first log entry..."
    
    # Auto-refresh setup
    auto_refresh = max(0, min(auto_refresh, 60))  # Clamp between 0-60 seconds
    refresh_meta = f'<meta http-equiv="refresh" content="{auto_refresh}">' if auto_refresh > 0 else ''
    refresh_status = f"Auto-refreshing every {auto_refresh} seconds" if auto_refresh > 0 else "Auto-refresh: OFF"
    
    # Current time
    current_time = datetime.now(timezone.utc).strftime("%Y-%m-%d %H:%M:%S UTC")

    version_output_safe = html.escape(version_output)
    app_logs_safe = html.escape(app_logs)
    log_info_safe = html.escape(log_info)

    nav_html = render_nav("/ffmpeg")
    html_content = f"""
    <!doctype html>
    <html>
    <head>
      <meta charset="utf-8" />
      <title>FFmpeg Info</title>
      {refresh_meta}
      <style>
        body {{ font-family: system-ui, sans-serif; padding: 24px; max-width: 1400px; margin: 0 auto; }}
        .brand {{ font-size: 32px; font-weight: bold; margin-bottom: 20px; }}
        .brand .ff {{ color: #28a745; }}
        .brand .api {{ color: #000; }}
        pre {{ background: #f5f5f5; padding: 15px; border-radius: 4px; overflow-x: auto; font-size: 12px; line-height: 1.4; }}
        h3 {{ margin-top: 30px; margin-bottom: 10px; }}
        nav {{ margin-bottom: 20px; }}
        nav a {{ margin-right: 15px; color: #0066cc; text-decoration: none; }}
        nav a:hover {{ text-decoration: underline; }}
        .section {{ margin-bottom: 40px; }}
        .logs {{ 
          height: 600px; 
          overflow-y: scroll; 
          background: #1e1e1e; 
          color: #d4d4d4; 
          overflow-x: auto;
        }}
        .controls {{ margin-bottom: 15px; }}
        .btn {{ 
          background: #0066cc; 
          color: white; 
          padding: 8px 16px; 
          border: none; 
          border-radius: 4px; 
          cursor: pointer;
          margin-right: 10px;
          text-decoration: none;
          display: inline-block;
        }}
        .btn:hover {{ background: #0052a3; }}
        .btn.secondary {{ background: #666; }}
        .btn.secondary:hover {{ background: #555; }}
        .info {{ color: #666; font-size: 13px; margin-bottom: 10px; }}
        .status {{ 
          padding: 6px 12px; 
          background: #e8f4f8; 
          border-radius: 4px; 
          display: inline-block;
          margin-left: 10px;
          font-size: 13px;
        }}
        .status.active {{ background: #d4edda; color: #155724; }}
      </style>
    </head>
    <body>
      <div class="brand"><span class="ff">ff</span><span class="api">api</span></div>
{nav_html}
      <h2>FFmpeg & Container Information</h2>
      
      <div class="section">
        <h3>FFmpeg Version & Build</h3>
        <pre>{version_output_safe}</pre>
      </div>
      
      <div class="section">
        <h3>Application Logs (Docker Container Output - Last 1000 lines)</h3>
        <div class="controls">
          <button class="btn" onclick="location.reload()">Manual Refresh</button>
          <a href="/ffmpeg?auto_refresh=5" class="btn secondary">Auto 5s</a>
          <a href="/ffmpeg?auto_refresh=10" class="btn secondary">Auto 10s</a>
          <a href="/ffmpeg?auto_refresh=30" class="btn secondary">Auto 30s</a>
          <a href="/ffmpeg" class="btn secondary">Stop Auto</a>
          <span class="status {'active' if auto_refresh > 0 else ''}">{refresh_status}</span>
        </div>
        <div class="info">
          Page loaded: {current_time} | {log_info_safe}
        </div>
        <pre class="logs" id="logContainer">{app_logs_safe}</pre>
      </div>
    </body>
    <script>
      // Auto-scroll logs to bottom on page load
      window.addEventListener('DOMContentLoaded', function() {{
        var logContainer = document.getElementById('logContainer');
        if (logContainer) {{
          logContainer.scrollTop = logContainer.scrollHeight;
        }}
      }});
    </script>
    </html>
    """
    return HTMLResponse(html_content)


@app.get("/documentation", response_class=HTMLResponse)
def documentation(request: Request):
    redirect = ensure_dashboard_access(request)
    if redirect:
        return redirect
    """Display API documentation."""
    
    # API Endpoints Documentation
    api_docs = """
<h4>UI & Info</h4>
<div class="endpoint">
  <div class="method get">GET</div>
  <div class="path">/</div>
  <div class="desc">Redirects to /downloads</div>
  <div class="example">Example:<br>curl http://localhost:3000/</div>
</div>

<div class="endpoint">
  <div class="method get">GET</div>
  <div class="path">/downloads</div>
  <div class="desc">Browse generated files (HTML page)</div>
  <div class="example">Example:<br>curl http://localhost:3000/downloads</div>
</div>

<div class="endpoint">
  <div class="method get">GET</div>
  <div class="path">/logs</div>
  <div class="desc">Browse FFmpeg operation logs (HTML page)</div>
  <div class="example">Example:<br>curl http://localhost:3000/logs</div>
</div>

<div class="endpoint">
  <div class="method get">GET</div>
  <div class="path">/logs/view?path={date}/{filename}</div>
  <div class="desc">View individual log file content</div>
  <div class="params">
    <span class="param">path</span> - Relative path to log file (e.g., 20241008/20241008_143022_12345678_compose-urls.log)
  </div>
  <div class="example">Example:<br>curl http://localhost:3000/logs/view?path=20241008/20241008_143022_12345678_compose-urls.log</div>
</div>

<div class="endpoint">
  <div class="method get">GET</div>
  <div class="path">/ffmpeg?auto_refresh={seconds}</div>
  <div class="desc">FFmpeg info and container logs with optional auto-refresh</div>
  <div class="params">
    <span class="param">auto_refresh</span> - Auto-refresh interval in seconds (0-60, default: 0)
  </div>
  <div class="example">Example:<br>curl http://localhost:3000/ffmpeg?auto_refresh=10</div>
</div>

<div class="endpoint">
  <div class="method get">GET</div>
  <div class="path">/documentation</div>
  <div class="desc">This page - Complete API documentation</div>
  <div class="example">Example:<br>curl http://localhost:3000/documentation</div>
</div>

<div class="endpoint">
  <div class="method get">GET</div>
  <div class="path">/settings</div>
  <div class="desc">Configuration page for UI authentication and storage overview</div>
  <div class="example">Example:<br>curl http://localhost:3000/settings</div>
</div>

<div class="endpoint">
  <div class="method get">GET</div>
  <div class="path">/api-keys</div>
  <div class="desc">Manage API authentication, generate keys, and review usage history</div>
  <div class="example">Example:<br>curl http://localhost:3000/api-keys</div>
</div>

<div class="endpoint">
  <div class="method post">POST</div>
  <div class="path">/api-keys/(require|generate|revoke)</div>
  <div class="desc">Toggle API key enforcement, create new keys, or revoke existing ones</div>
  <div class="params">Submit form-encoded fields such as <span class="param">require_api_key</span> or <span class="param">key_id</span>.</div>
</div>

<div class="endpoint">
  <div class="method get">GET</div>
  <div class="path">/health</div>
  <div class="desc">Health check endpoint with disk, memory, FFmpeg, and operation metrics</div>
  <div class="response">Returns: {"ok": true, "disk": {...}, "memory": {...}, "ffmpeg": {...}, "operations": {...}}</div>
  <div class="example">Example:<br>curl http://localhost:3000/health</div>
</div>

<div class="endpoint">
  <div class="method get">GET</div>
  <div class="path">/metrics</div>
  <div class="desc">Operational metrics dashboard (HTML page)</div>
  <div class="example">Example:<br>curl http://localhost:3000/metrics</div>
</div>

<div class="endpoint">
  <div class="method get">GET</div>
  <div class="path">/files/{date}/{filename}</div>
  <div class="desc">Static file serving - download generated files</div>
  <div class="example">Example:<br>curl -O http://localhost:3000/files/20241008/20241008_143022_12345678.mp4</div>
</div>

<h4>Image Processing</h4>
<div class="endpoint">
  <div class="method post">POST</div>
  <div class="path">/image/to-mp4-loop</div>
  <div class="desc">Convert static image to looping video</div>
  <div class="params">
    <span class="param">file</span> - Image file (PNG/JPEG) [multipart/form-data]<br>
    <span class="param">duration</span> - Duration in seconds (1-3600, default: 30)<br>
    <span class="param">as_json</span> - Return JSON instead of file (default: false)
  </div>
  <div class="response">Returns: MP4 file or {"ok": true, "file_url": "...", "path": "..."}</div>
  <div class="example">Example:<br>curl -X POST http://localhost:3000/image/to-mp4-loop \<br>  -F "file=@image.jpg" \<br>  -F "duration=10" \<br>  -F "as_json=true"</div>
</div>

<h4>Video Composition</h4>
<div class="endpoint">
  <div class="method post">POST</div>
  <div class="path">/compose/from-binaries</div>
  <div class="desc">Compose video from uploaded files</div>
  <div class="params">
    <span class="param">video</span> - Video file [multipart/form-data]<br>
    <span class="param">audio</span> - Audio file (optional) [multipart/form-data]<br>
    <span class="param">bgm</span> - Background music file (optional) [multipart/form-data]<br>
    <span class="param">duration_ms</span> - Duration in milliseconds (1-3600000, default: 30000)<br>
    <span class="param">width</span> - Output width in pixels (default: 1920)<br>
    <span class="param">height</span> - Output height in pixels (default: 1080)<br>
    <span class="param">fps</span> - Output frames per second (default: 30)<br>
    <span class="param">bgm_volume</span> - BGM volume multiplier (default: 0.3)<br>
    <span class="param">as_json</span> - Return JSON instead of file (default: false)
  </div>
  <div class="response">Returns: MP4 file or {"ok": true, "file_url": "...", "path": "..."}</div>
  <div class="example">Example:<br>curl -X POST http://localhost:3000/compose/from-binaries \<br>  -F "video=@video.mp4" \<br>  -F "audio=@audio.mp3" \<br>  -F "bgm=@music.mp3" \<br>  -F "duration_ms=15000" \<br>  -F "width=1280" \<br>  -F "height=720" \<br>  -F "bgm_volume=0.2" \<br>  -F "as_json=true"</div>
</div>

<div class="endpoint">
  <div class="method post">POST</div>
  <div class="path">/compose/from-urls</div>
  <div class="desc">Compose video from URLs</div>
  <div class="params">
    <span class="param">video_url</span> - Video URL (required)<br>
    <span class="param">audio_url</span> - Audio URL (optional)<br>
    <span class="param">bgm_url</span> - Background music URL (optional)<br>
    <span class="param">duration</span> - Duration in seconds (0.001-3600, optional)<br>
    <span class="param">duration_ms</span> - Duration in milliseconds (1-3600000, default: 30000)<br>
    <span class="param">width</span> - Output width in pixels (default: 1920)<br>
    <span class="param">height</span> - Output height in pixels (default: 1080)<br>
    <span class="param">fps</span> - Output frames per second (default: 30)<br>
    <span class="param">bgm_volume</span> - BGM volume multiplier (default: 0.3)<br>
    <span class="param">headers</span> - HTTP headers for authenticated requests (optional)<br>
    <span class="param">as_json</span> - Return JSON instead of file (default: false)
  </div>
  <div class="response">Returns: MP4 file or {"ok": true, "file_url": "...", "path": "..."}</div>
  <div class="example">Example:<br>curl -X POST http://localhost:3000/compose/from-urls \<br>  -H "Content-Type: application/json" \<br>  -d '{<br>    "video_url": "https://example.com/video.mp4",<br>    "audio_url": "https://example.com/audio.mp3",<br>    "bgm_url": "https://example.com/music.mp3",<br>    "duration": 20.0,<br>    "width": 1280,<br>    "height": 720,<br>    "fps": 30,<br>    "bgm_volume": 0.3,<br>    "as_json": true<br>  }'</div>
</div>

<div class="endpoint">
  <div class="method post">POST</div>
  <div class="path">/compose/from-urls/async</div>
  <div class="desc">Queue video composition as a background job</div>
  <div class="params">
    Same parameters as <code>/compose/from-urls</code>
  </div>
  <div class="response">Returns: {"job_id": "...", "status_url": "/jobs/{job_id}"}</div>
  <div class="example">Example:<br>curl -X POST http://localhost:3000/compose/from-urls/async \<br>  -H "Content-Type: application/json" \<br>  -d '{<br>    "video_url": "https://example.com/video.mp4",<br>    "duration_ms": 10000<br>  }'</div>
</div>

<div class="endpoint">
  <div class="method get">GET</div>
  <div class="path">/jobs/{job_id}</div>
  <div class="desc">Fetch status details for an asynchronous job</div>
  <div class="params">
    <span class="param">job_id</span> - Identifier returned from the async compose endpoint
  </div>
  <div class="response">Returns: {"status": "queued|processing|finished|failed", ...}</div>
  <div class="example">Example:<br>curl http://localhost:3000/jobs/123e4567-e89b-12d3-a456-426614174000</div>
</div>

<div class="endpoint">
  <div class="method post">POST</div>
  <div class="path">/compose/from-tracks</div>
  <div class="desc">Compose video from track definitions</div>
  <div class="params">
    <span class="param">tracks</span> - Array of track objects with keyframes<br>
    <span class="param">width</span> - Output width in pixels (default: 1920)<br>
    <span class="param">height</span> - Output height in pixels (default: 1080)<br>
    <span class="param">fps</span> - Output frames per second (default: 30)<br>
    <span class="param">as_json</span> - Return JSON instead of file (default: false)
  </div>
  <div class="response">Returns: MP4 file or {"ok": true, "file_url": "...", "path": "..."}</div>
  <div class="example">Example:<br>curl -X POST http://localhost:3000/compose/from-tracks \<br>  -H "Content-Type: application/json" \<br>  -d '{<br>    "tracks": [<br>      {<br>        "id": "video1",<br>        "type": "video",<br>        "keyframes": [<br>          {<br>            "url": "https://example.com/clip.mp4",<br>            "timestamp": 0,<br>            "duration": 10000<br>          }<br>        ]<br>      },<br>      {<br>        "id": "audio1",<br>        "type": "audio",<br>        "keyframes": [<br>          {<br>            "url": "https://example.com/audio.mp3",<br>            "timestamp": 0,<br>            "duration": 10000<br>          }<br>        ]<br>      }<br>    ],<br>    "width": 1920,<br>    "height": 1080,<br>    "fps": 30,<br>    "as_json": true<br>  }'</div>
</div>

<h4>Video Concatenation</h4>
<div class="endpoint">
  <div class="method post">POST</div>
  <div class="path">/video/concat-from-urls</div>
  <div class="desc">Concatenate multiple video clips from URLs</div>
  <div class="params">
    <span class="param">clips</span> - Array of video URLs<br>
    <span class="param">width</span> - Output width in pixels (default: 1920)<br>
    <span class="param">height</span> - Output height in pixels (default: 1080)<br>
    <span class="param">fps</span> - Output frames per second (default: 30)<br>
    <span class="param">as_json</span> - Return JSON instead of file (default: false)
  </div>
  <div class="response">Returns: MP4 file or {"ok": true, "file_url": "...", "path": "..."}</div>
  <div class="example">Example:<br>curl -X POST http://localhost:3000/video/concat-from-urls \<br>  -H "Content-Type: application/json" \<br>  -d '{<br>    "clips": [<br>      "https://example.com/clip1.mp4",<br>      "https://example.com/clip2.mp4",<br>      "https://example.com/clip3.mp4"<br>    ],<br>    "width": 1920,<br>    "height": 1080,<br>    "fps": 30,<br>    "as_json": true<br>  }'</div>
</div>

<div class="endpoint">
  <div class="method post">POST</div>
  <div class="path">/video/concat</div>
  <div class="desc">Concatenate videos (alias endpoint, accepts 'clips' or 'urls')</div>
  <div class="params">
    <span class="param">clips</span> - Array of video URLs (preferred)<br>
    <span class="param">urls</span> - Array of video URLs (alternative)<br>
    <span class="param">width</span> - Output width in pixels (default: 1920)<br>
    <span class="param">height</span> - Output height in pixels (default: 1080)<br>
    <span class="param">fps</span> - Output frames per second (default: 30)<br>
    <span class="param">as_json</span> - Return JSON instead of file (default: false)
  </div>
  <div class="response">Returns: MP4 file or {"ok": true, "file_url": "...", "path": "..."}</div>
  <div class="example">Example (using 'clips'):<br>curl -X POST http://localhost:3000/video/concat \<br>  -H "Content-Type: application/json" \<br>  -d '{<br>    "clips": [<br>      "https://example.com/video1.mp4",<br>      "https://example.com/video2.mp4"<br>    ],<br>    "width": 1280,<br>    "height": 720,<br>    "as_json": true<br>  }'</div>
</div>

<h4>Custom FFmpeg Commands</h4>
<div class="endpoint">
  <div class="method post">POST</div>
  <div class="path">/v1/run-ffmpeg-command</div>
  <div class="desc">Run custom FFmpeg command with template variables</div>
  <div class="params">
    <span class="param">input_files</span> - Object mapping variable names to URLs<br>
    <span class="param">output_files</span> - Object mapping variable names to filenames<br>
    <span class="param">ffmpeg_command</span> - FFmpeg command with {{variable}} placeholders
  </div>
  <div class="response">Returns: {"ok": true, "outputs": {"key": "url", ...}}</div>
  <div class="example">Example (scale video to 720p):<br>curl -X POST http://localhost:3000/v1/run-ffmpeg-command \<br>  -H "Content-Type: application/json" \<br>  -d '{<br>    "input_files": {<br>      "video": "https://example.com/input.mp4"<br>    },<br>    "output_files": {<br>      "out": "result.mp4"<br>    },<br>    "ffmpeg_command": "-i {{video}} -vf scale=1280:720 -c:a copy {{out}}"<br>  }'</div>
</div>

<h4>FFprobe (Media Inspection)</h4>
<div class="endpoint">
  <div class="method post">POST</div>
  <div class="path">/probe/from-urls</div>
  <div class="desc">Inspect media file from URL using ffprobe</div>
  <div class="params">
    <span class="param">url</span> - Media file URL<br>
    <span class="param">headers</span> - HTTP headers for authenticated requests (optional)<br>
    <span class="param">show_format</span> - Show format info (default: true)<br>
    <span class="param">show_streams</span> - Show stream info (default: true)<br>
    <span class="param">show_chapters</span> - Show chapters (default: false)<br>
    <span class="param">show_programs</span> - Show programs (default: false)<br>
    <span class="param">show_packets</span> - Show packets (default: false)<br>
    <span class="param">count_frames</span> - Count frames (default: false)<br>
    <span class="param">count_packets</span> - Count packets (default: false)<br>
    <span class="param">probe_size</span> - Probe size (optional)<br>
    <span class="param">analyze_duration</span> - Analyze duration (optional)<br>
    <span class="param">select_streams</span> - Select specific streams (optional)
  </div>
  <div class="response">Returns: JSON with media information</div>
  <div class="example">Example:<br>curl -X POST http://localhost:3000/probe/from-urls \<br>  -H "Content-Type: application/json" \<br>  -d '{<br>    "url": "https://example.com/video.mp4",<br>    "show_format": true,<br>    "show_streams": true,<br>    "count_frames": true<br>  }'</div>
</div>

<div class="endpoint">
  <div class="method post">POST</div>
  <div class="path">/probe/from-binary</div>
  <div class="desc">Inspect uploaded media file using ffprobe</div>
  <div class="params">
    <span class="param">file</span> - Media file [multipart/form-data]<br>
    <span class="param">(same options as /probe/from-urls)</span>
  </div>
  <div class="response">Returns: JSON with media information</div>
  <div class="example">Example:<br>curl -X POST http://localhost:3000/probe/from-binary \<br>  -F "file=@video.mp4" \<br>  -F "show_format=true" \<br>  -F "show_streams=true" \<br>  -F "count_frames=true"</div>
</div>

<div class="endpoint">
  <div class="method get">GET</div>
  <div class="path">/probe/public?rel={path}</div>
  <div class="desc">Inspect file from public directory using ffprobe</div>
  <div class="params">
    <span class="param">rel</span> - Relative path within PUBLIC_DIR<br>
    <span class="param">(same options as /probe/from-urls)</span>
  </div>
  <div class="response">Returns: JSON with media information</div>
  <div class="example">Example:<br>curl "http://localhost:3000/probe/public?rel=20241008/20241008_143022_12345678.mp4&show_format=true&show_streams=true"</div>
</div>
"""

    nav_html = render_nav("/documentation")
    html_content = f"""
    <!doctype html>
    <html>
    <head>
      <meta charset="utf-8" />
      <title>API Documentation</title>
      <style>
        body {{ font-family: system-ui, sans-serif; padding: 24px; max-width: 1400px; margin: 0 auto; }}
        .brand {{ font-size: 32px; font-weight: bold; margin-bottom: 20px; }}
        .brand .ff {{ color: #28a745; }}
        .brand .api {{ color: #000; }}
        h2 {{ margin-bottom: 10px; }}
        h4 {{ margin-top: 30px; margin-bottom: 15px; color: #333; border-bottom: 2px solid #0066cc; padding-bottom: 5px; }}
        nav {{ margin-bottom: 20px; }}
        nav a {{ margin-right: 15px; color: #0066cc; text-decoration: none; }}
        nav a:hover {{ text-decoration: underline; }}
        .intro {{ 
          background: #f0f7ff; 
          padding: 20px; 
          border-radius: 8px; 
          margin-bottom: 30px; 
          border-left: 4px solid #0066cc;
        }}
        .intro p {{ margin: 8px 0; line-height: 1.6; }}
        
        .endpoint {{
          background: #f9f9f9;
          border-left: 4px solid #0066cc;
          padding: 12px 15px;
          margin-bottom: 12px;
          border-radius: 4px;
        }}
        .method {{
          display: inline-block;
          background: #0066cc;
          color: white;
          padding: 3px 10px;
          border-radius: 3px;
          font-weight: bold;
          font-size: 12px;
          margin-right: 10px;
        }}
        .method.get {{ background: #28a745; }}
        .method.post {{ background: #007bff; }}
        .path {{
          display: inline-block;
          font-family: 'Courier New', monospace;
          font-size: 14px;
          font-weight: bold;
          color: #333;
        }}
        .desc {{
          margin-top: 8px;
          color: #555;
          font-size: 14px;
        }}
        .params {{
          margin-top: 8px;
          padding: 10px;
          background: #fff;
          border-radius: 3px;
          font-size: 13px;
          line-height: 1.6;
        }}
        .param {{
          font-family: 'Courier New', monospace;
          color: #0066cc;
          font-weight: bold;
        }}
        .response {{
          margin-top: 8px;
          padding: 8px;
          background: #e8f5e9;
          border-radius: 3px;
          font-size: 13px;
          font-family: 'Courier New', monospace;
        }}
        .example {{
          margin-top: 8px;
          padding: 10px;
          background: #fff3cd;
          border-radius: 3px;
          font-size: 12px;
          font-family: 'Courier New', monospace;
          white-space: pre;
        }}
      </style>
    </head>
    <body>
      <div class="brand"><span class="ff">ff</span><span class="api">api</span></div>
{nav_html}
      <h2>FFAPI Ultimate - API Documentation</h2>
      
      <div class="intro">
        <p><strong>Base URL:</strong> {os.getenv("PUBLIC_BASE_URL") or "http://localhost:3000"}</p>
        <p><strong>Response Formats:</strong> Most endpoints support both file responses and JSON (via <code>as_json=true</code> parameter)</p>
        <p><strong>File Retention:</strong> Generated files are kept for {os.getenv("RETENTION_DAYS", "7")} days</p>
        <p><strong>Output Location:</strong> All generated files are available at <code>/files/{{date}}/{{filename}}</code></p>
      </div>
      
      {api_docs}
    </body>
    </html>
    """
    return HTMLResponse(html_content)


@app.get("/settings", response_class=HTMLResponse)
def settings_page(
    request: Request,
    next: str = "",
    message: str = "",
    error: str = "",
):
    authed = is_authenticated(request)
    next_target = next or "/settings"
    if UI_AUTH.require_login and not authed:
        csrf_token = ensure_csrf_token(request)
        html_content = render_login_page(next_target, csrf_token, error or None)
        return HTMLResponse(html_content)

    storage = storage_management_snapshot()
    csrf_token = ensure_csrf_token(request)
    backup_codes = UI_AUTH.pop_pending_backup_codes()
    backup_status = UI_AUTH.get_backup_code_status()
    html_content = render_settings_page(
        storage,
        message or None,
        error or None,
        authenticated=authed,
        csrf_token=csrf_token,
        backup_codes=backup_codes,
        backup_status=backup_status,
    )
    return HTMLResponse(html_content)


@app.post("/settings/login")
async def settings_login(request: Request):
    form = await request.form()
    username = str(form.get("username", ""))
    password = str(form.get("password", ""))
    totp_code = str(form.get("totp", "")).strip()
    backup_code = str(form.get("backup_code", "")).strip()
    next_path = str(form.get("next", "/settings")) or "/settings"
    if not next_path.startswith("/"):
        next_path = "/settings"

    if UI_AUTH.verify(username, password):
        if UI_AUTH.is_two_factor_enabled():
            if not totp_code and not backup_code:
                csrf_token = ensure_csrf_token(request)
                html_content = render_login_page(next_path, csrf_token, "Authentication or backup code required")
                return HTMLResponse(html_content, status_code=401)
            second_factor_ok = False
            if totp_code:
                second_factor_ok = UI_AUTH.verify_totp(totp_code)
            if not second_factor_ok:
                if backup_code:
                    second_factor_ok = UI_AUTH.verify_backup_code(backup_code)
                elif totp_code:
                    second_factor_ok = UI_AUTH.verify_backup_code(totp_code)
            if not second_factor_ok:
                csrf_token = ensure_csrf_token(request)
                html_content = render_login_page(next_path, csrf_token, "Invalid authentication or backup code")
                return HTMLResponse(html_content, status_code=401)
        token = UI_AUTH.create_session()
        response = RedirectResponse(url=next_path, status_code=303)
        response.set_cookie(
            SESSION_COOKIE_NAME,
            token,
            max_age=SESSION_TTL_SECONDS,
            httponly=True,
            samesite="lax",
        )
        return response

    csrf_token = ensure_csrf_token(request)
    html_content = render_login_page(next_path, csrf_token, "Invalid username or password")
    return HTMLResponse(html_content, status_code=401)


@app.post("/settings/logout")
async def settings_logout(request: Request):
    token = request.cookies.get(SESSION_COOKIE_NAME)
    if token:
        UI_AUTH.clear_session(token)
    response = RedirectResponse(url="/settings", status_code=303)
    response.delete_cookie(SESSION_COOKIE_NAME)
    return response


@app.post("/settings/api-auth")
async def settings_update_api_auth(request: Request):
    if UI_AUTH.require_login and not is_authenticated(request):
        query = urlencode({"error": "Authentication required to change API authentication"})
        return RedirectResponse(url=f"/settings?{query}", status_code=303)

    form = await request.form()
    raw_value = form.get("require_api_key")
    enable = False
    if raw_value is not None:
        enable = str(raw_value).lower() in {"true", "1", "on", "yes"}
    API_KEYS.set_require_key(enable)
    message = "API authentication enabled" if enable else "API authentication disabled"
    query = urlencode({"message": message})
    return RedirectResponse(url=f"/settings?{query}", status_code=303)


@app.post("/settings/ui-auth")
async def settings_toggle_auth(request: Request):
    form = await request.form()
    raw_value = form.get("require_login")
    authed = is_authenticated(request)
    if UI_AUTH.require_login and not authed:
        response = RedirectResponse(
            url=f"/settings?{urlencode({'error': 'Authentication required to change settings'})}",
            status_code=303,
        )
        response.delete_cookie(SESSION_COOKIE_NAME)
        return response

    enable = False
    if raw_value is not None:
        enable = str(raw_value).lower() in {"true", "1", "on", "yes"}
    UI_AUTH.set_require_login(enable)
    message = (
        "Dashboard login requirement enabled"
        if enable
        else "Dashboard login requirement disabled"
    )
    query = urlencode({"message": message})
    response = RedirectResponse(url=f"/settings?{query}", status_code=303)
    if not enable:
        response.delete_cookie(SESSION_COOKIE_NAME)
    return response


@app.post("/settings/credentials")
async def settings_update_credentials(request: Request):
    form = await request.form()
    username = str(form.get("username", ""))
    password = str(form.get("password", ""))
    authed = is_authenticated(request)
    if UI_AUTH.require_login and not authed:
        response = RedirectResponse(
            url=f"/settings?{urlencode({'error': 'Authentication required to change credentials'})}",
            status_code=303,
        )
        response.delete_cookie(SESSION_COOKIE_NAME)
        return response

    try:
        UI_AUTH.set_credentials(username, password)
    except ValueError as exc:
        storage = storage_management_snapshot()
        csrf_token = ensure_csrf_token(request)
        html_content = render_settings_page(
            storage,
            error=str(exc),
            authenticated=authed,
            csrf_token=csrf_token,
        )
        return HTMLResponse(html_content, status_code=400)

    query = urlencode({"message": "Credentials updated"})
    response = RedirectResponse(url=f"/settings?{query}", status_code=303)
    response.delete_cookie(SESSION_COOKIE_NAME)
    return response





@app.post("/settings/two-factor")
async def settings_update_two_factor(request: Request):
    form = await request.form()
    action = str(form.get("action", "")).strip().lower()
    authed = is_authenticated(request)
    if UI_AUTH.require_login and not authed:
        response = RedirectResponse(
            url=f"/settings?{urlencode({'error': 'Authentication required to change security settings'})}",
            status_code=303,
        )
        response.delete_cookie(SESSION_COOKIE_NAME)
        return response

    message: Optional[str] = None
    error: Optional[str] = None

    if action == "enable":
        code = str(form.get("code", "")).strip()
        if not code:
            error = "Authentication code is required to enable two-factor"
        elif not UI_AUTH.verify_totp(code):
            error = "Invalid authentication code"
        else:
            UI_AUTH.set_two_factor_enabled(True)
            UI_AUTH.generate_backup_codes()
            message = "Two-factor authentication enabled. Backup codes generated"
    elif action == "disable":
        UI_AUTH.set_two_factor_enabled(False)
        message = "Two-factor authentication disabled"
    elif action == "regenerate":
        UI_AUTH.regenerate_two_factor_secret()
        message = "Generated a new authentication secret. Two-factor authentication has been disabled."
    elif action == "generate_codes":
        if not UI_AUTH.is_two_factor_enabled():
            error = "Enable two-factor authentication before generating backup codes"
        else:
            UI_AUTH.generate_backup_codes()
            message = "New backup codes generated"
    else:
        error = "Unknown two-factor action"

    query_parts = {}
    if message:
        query_parts["message"] = message
    if error:
        query_parts["error"] = error
    query = urlencode(query_parts) if query_parts else ""
    target = "/settings" if not query else f"/settings?{query}"
    return RedirectResponse(url=target, status_code=303)
@app.post("/settings/retention")
async def settings_update_retention(request: Request):
    form = await request.form()
    authed = is_authenticated(request)
    if UI_AUTH.require_login and not authed:
        response = RedirectResponse(
            url=f"/settings?{urlencode({'error': 'Authentication required to change retention'})}",
            status_code=303,
        )
        response.delete_cookie(SESSION_COOKIE_NAME)
        return response

    raw_hours = str(form.get("retention_hours", "")).strip()
    if not raw_hours:
        storage = storage_management_snapshot()
        csrf_token = ensure_csrf_token(request)
        html_content = render_settings_page(
            storage,
            error="Retention hours are required",
            authenticated=authed,
            csrf_token=csrf_token,
        )
        return HTMLResponse(html_content, status_code=400)

    try:
        hours = float(raw_hours)
    except ValueError:
        storage = storage_management_snapshot()
        csrf_token = ensure_csrf_token(request)
        html_content = render_settings_page(
            storage,
            error="Retention hours must be a number",
            authenticated=authed,
            csrf_token=csrf_token,
        )
        return HTMLResponse(html_content, status_code=400)

    if hours < 1 or hours > 24 * 365:
        storage = storage_management_snapshot()
        csrf_token = ensure_csrf_token(request)
        html_content = render_settings_page(
            storage,
            error="Retention must be between 1 hour and 8760 hours (365 days)",
            authenticated=authed,
            csrf_token=csrf_token,
        )
        return HTMLResponse(html_content, status_code=400)

    days = hours / 24
    global RETENTION_DAYS
    with SETTINGS_LOCK:
        RETENTION_DAYS = days
        settings.RETENTION_DAYS = days
    cleanup_old_public()
    formatted_hours = f"{hours:.1f}".rstrip("0").rstrip(".")
    query = urlencode({"message": f"Retention updated to {formatted_hours} hour(s)"})
    return RedirectResponse(url=f"/settings?{query}", status_code=303)


@app.post("/settings/performance")
async def settings_update_performance(request: Request):
    form = await request.form()
    authed = is_authenticated(request)
    if UI_AUTH.require_login and not authed:
        response = RedirectResponse(
            url=f"/settings?{urlencode({'error': 'Authentication required to change performance settings'})}",
            status_code=303,
        )
        response.delete_cookie(SESSION_COOKIE_NAME)
        return response

    errors: List[str] = []

    def parse_int(name: str, minimum: int, maximum: int) -> Optional[int]:
        raw_value = str(form.get(name, "")).strip()
        if not raw_value:
            errors.append(f"{name.replace('_', ' ').title()} is required")
            return None
        try:
            value = int(raw_value)
        except ValueError:
            try:
                float_value = float(raw_value)
            except ValueError:
                errors.append(f"{name.replace('_', ' ').title()} must be a number")
                return None
            if not float_value.is_integer():
                errors.append(f"{name.replace('_', ' ').title()} must be a whole number")
                return None
            value = int(float_value)
        if value < minimum or value > maximum:
            errors.append(
                f"{name.replace('_', ' ').title()} must be between {minimum} and {maximum}"
            )
            return None
        return value

    def parse_float(name: str, minimum: float, maximum: float) -> Optional[float]:
        raw_value = str(form.get(name, "")).strip()
        if not raw_value:
            errors.append(f"{name.replace('_', ' ').title()} is required")
            return None
        try:
            value = float(raw_value)
        except ValueError:
            errors.append(f"{name.replace('_', ' ').title()} must be a number")
            return None
        if value < minimum or value > maximum:
            errors.append(
                f"{name.replace('_', ' ').title()} must be between {minimum} and {maximum}"
            )
            return None
        return value

    rpm = parse_int("rate_limit_rpm", 1, 100000)
    timeout_minutes = parse_float("ffmpeg_timeout_minutes", 1, 720)
    chunk_mb = parse_float("upload_chunk_mb", 0.0, 512)
    max_file_mb = parse_int("max_file_size_mb", 1, 8192)

    if chunk_mb is not None and chunk_mb < 0.001:
        errors.append("Upload chunk size must be at least 0.001 MB")
        chunk_mb = None

    if errors or rpm is None or timeout_minutes is None or chunk_mb is None or max_file_mb is None:
        storage = storage_management_snapshot()
        csrf_token = ensure_csrf_token(request)
        html_content = render_settings_page(
            storage,
            error="; ".join(errors) if errors else "Invalid performance settings",
            authenticated=authed,
            csrf_token=csrf_token,
        )
        return HTMLResponse(html_content, status_code=400)

    global FFMPEG_TIMEOUT_SECONDS, UPLOAD_CHUNK_SIZE, MAX_FILE_SIZE_MB, MAX_FILE_SIZE_BYTES

    with SETTINGS_LOCK:
        RATE_LIMITER.update_limit(rpm)
        settings.RATE_LIMIT_REQUESTS_PER_MINUTE = rpm

        FFMPEG_TIMEOUT_SECONDS = int(timeout_minutes * 60)
        settings.FFMPEG_TIMEOUT_SECONDS = FFMPEG_TIMEOUT_SECONDS

        upload_chunk_bytes = max(1, int(chunk_mb * 1024 * 1024))
        UPLOAD_CHUNK_SIZE = upload_chunk_bytes
        settings.UPLOAD_CHUNK_SIZE = upload_chunk_bytes

        MAX_FILE_SIZE_MB = max_file_mb
        MAX_FILE_SIZE_BYTES = MAX_FILE_SIZE_MB * 1024 * 1024
        settings.MAX_FILE_SIZE_MB = MAX_FILE_SIZE_MB

    message = "Performance settings updated"
    query = urlencode({"message": message})
    return RedirectResponse(url=f"/settings?{query}", status_code=303)


@app.get("/api-keys", response_class=HTMLResponse)
def api_keys_page(
    request: Request,
    message: str = "",
    error: str = "",
    new_key_id: str = "",
):
    redirect = ensure_dashboard_access(request)
    if redirect:
        return redirect

    new_key_value = API_KEYS.consume_plaintext(new_key_id) if new_key_id else None
    csrf_token = ensure_csrf_token(request)
    html_content = render_api_keys_page(
        keys=API_KEYS.list_keys(),
        require_key=API_KEYS.is_required(),
        message=message or None,
        error=error or None,
        new_key=new_key_value,
        csrf_token=csrf_token,
    )
    return HTMLResponse(html_content)

@app.post("/api-keys/generate")
async def api_keys_generate(request: Request):
    redirect = ensure_dashboard_access(request)
    if redirect:
        return redirect

    if not API_KEYS.is_required():
        query = urlencode({"error": "Enable API authentication before generating keys"})
        return RedirectResponse(url=f"/api-keys?{query}", status_code=303)

    key_id, _ = API_KEYS.generate_key()
    query = urlencode({"message": "Generated new API key", "new_key_id": key_id})
    response = RedirectResponse(url=f"/api-keys?{query}", status_code=303)
    response.headers["Cache-Control"] = "no-store"
    return response


@app.post("/api-keys/revoke")
async def api_keys_revoke(request: Request):
    redirect = ensure_dashboard_access(request)
    if redirect:
        return redirect

    form = await request.form()
    key_id = str(form.get("key_id", "")).strip()
    if key_id:
        API_KEYS.remove_key(key_id)
        query = urlencode({"message": "API key revoked"})
    else:
        query = urlencode({"error": "Key identifier is required"})
    return RedirectResponse(url=f"/api-keys?{query}", status_code=303)


@app.get("/health")
def health():
    logger.info("Health check requested")
    disk = disk_snapshot()
    memory = memory_snapshot()
    ffmpeg_info = ffmpeg_snapshot()
    metrics_data = METRICS.snapshot()

    disk_ok = True
    for info in disk.values():
        if "error" in info:
            disk_ok = False
            break
        if info.get("available_mb", 0) < MIN_FREE_SPACE_MB:
            disk_ok = False
            break

    overall_ok = ffmpeg_info.get("available", False) and disk_ok

    return {
        "ok": overall_ok,
        "disk": disk,
        "memory": memory,
        "ffmpeg": ffmpeg_info,
        "operations": {
            "recent_success_rate": metrics_data["recent"],
            "queue": metrics_data["queue"],
            "error_counts": metrics_data["errors"],
        },
    }


@app.get("/metrics", response_class=HTMLResponse)
def metrics_dashboard(request: Request):
    redirect = ensure_dashboard_access(request)
    if redirect:
        return redirect
    snapshot = METRICS.snapshot()
    endpoints_html = []
    for path, stats in sorted(snapshot["per_endpoint"].items()):
        success_rate = stats.get("success_rate", 0.0) * 100.0
        endpoints_html.append(
            "<tr><td>{path}</td><td>{success}</td><td>{failure}</td><td>{avg:.2f} ms</td><td>{rate:.1f}%</td></tr>".format(
                path=html.escape(path),
                success=int(stats.get("success", 0)),
                failure=int(stats.get("failure", 0)),
                avg=stats.get("avg_duration_ms", 0.0),
                rate=success_rate,
            )
        )

    if not endpoints_html:
        endpoints_html.append(
            "<tr><td colspan='5'>No requests recorded yet</td></tr>"
        )

    errors_html = []
    for name, count in sorted(snapshot["errors"].items(), key=lambda item: item[0]):
        errors_html.append(
            "<tr><td>{name}</td><td>{count}</td></tr>".format(
                name=html.escape(name), count=int(count)
            )
        )
    if not errors_html:
        errors_html.append("<tr><td colspan='2'>No errors recorded</td></tr>")

    queue = snapshot["queue"]
    recent = snapshot["recent"]
    recent_rate = recent.get("success_rate")
    recent_summary = (
        f"{recent.get('successes', 0)} / {recent.get('window', 0)}"
        if recent_rate is not None
        else "No recent activity"
    )
    recent_percent = f"{recent_rate * 100.0:.1f}%" if recent_rate is not None else "-"

    nav_html = render_nav("/metrics")
    html_content = f"""
    <!doctype html>
    <html>
    <head>
      <meta charset="utf-8" />
      <title>Operational Metrics</title>
      <style>
        body {{ font-family: system-ui, sans-serif; padding: 24px; max-width: 1400px; margin: 0 auto; }}
        .brand {{ font-size: 32px; font-weight: bold; margin-bottom: 20px; }}
        .brand .ff {{ color: #28a745; }}
        .brand .api {{ color: #000; }}
        table {{ border-collapse: collapse; width: 100%; margin-bottom: 30px; }}
        th, td {{ border-bottom: 1px solid #eee; padding: 8px 10px; font-size: 14px; }}
        th {{ text-align: left; background: #fafafa; }}
        nav {{ margin-bottom: 20px; }}
        nav a {{ margin-right: 15px; color: #0066cc; text-decoration: none; }}
        .cards {{ display: flex; flex-wrap: wrap; gap: 20px; margin-bottom: 30px; }}
        .card {{
          flex: 1 1 240px;
          background: #f7f9fc;
          border-radius: 8px;
          padding: 16px;
          box-shadow: 0 1px 2px rgba(0,0,0,0.08);
        }}
        .card h3 {{ margin-top: 0; font-size: 16px; color: #333; }}
        .card p {{ margin: 6px 0; color: #555; font-size: 14px; }}
      </style>
    </head>
    <body>
      <div class="brand"><span class="ff">ff</span><span class="api">api</span></div>
{nav_html}

      <div class="cards">
        <div class="card">
          <h3>Queue Depth</h3>
          <p><strong>Current:</strong> {queue.get('current', 0)}</p>
          <p><strong>Max Observed:</strong> {queue.get('max', 0)}</p>
          <p><strong>Avg Wait:</strong> {queue.get('avg_wait_ms', 0.0):.2f} ms</p>
        </div>
        <div class="card">
          <h3>Recent Success Rate</h3>
          <p><strong>Window:</strong> {recent.get('window', 0)} operations</p>
          <p><strong>Successful:</strong> {recent_summary}</p>
          <p><strong>Rate:</strong> {recent_percent}</p>
        </div>
      </div>

      <h2>Endpoint Performance</h2>
      <table>
        <thead>
          <tr><th>Endpoint</th><th>Success</th><th>Failure</th><th>Avg Duration</th><th>Success %</th></tr>
        </thead>
        <tbody>{''.join(endpoints_html)}</tbody>
      </table>

      <h2>Error Frequency</h2>
      <table>
        <thead><tr><th>Error</th><th>Count</th></tr></thead>
        <tbody>{''.join(errors_html)}</tbody>
      </table>
    </body>
    </html>
    """
    return HTMLResponse(html_content)


def _prometheus_escape(value: str) -> str:
    return (
        value.replace("\\", "\\\\")
        .replace("\n", "\\n")
        .replace("\"", "\\\"")
    )


@app.get("/metrics/prometheus", response_class=PlainTextResponse, include_in_schema=False)
def metrics_prometheus() -> PlainTextResponse:
    snapshot = METRICS.snapshot()
    lines: List[str] = []

    lines.append("# HELP ffapi_requests_total Total requests processed by endpoint and result")
    lines.append("# TYPE ffapi_requests_total counter")
    for path, stats in sorted(snapshot.get("per_endpoint", {}).items()):
        label = _prometheus_escape(path or "unknown")
        lines.append(f'ffapi_requests_total{{path="{label}",result="success"}} {stats.get("success", 0)}')
        lines.append(f'ffapi_requests_total{{path="{label}",result="failure"}} {stats.get("failure", 0)}')

    lines.append("# HELP ffapi_request_duration_average_ms Average request duration per endpoint")
    lines.append("# TYPE ffapi_request_duration_average_ms gauge")
    for path, stats in sorted(snapshot.get("per_endpoint", {}).items()):
        label = _prometheus_escape(path or "unknown")
        lines.append(f'ffapi_request_duration_average_ms{{path="{label}"}} {stats.get("avg_duration_ms", 0.0)}')

    lines.append("# HELP ffapi_request_success_rate_ratio Success ratio per endpoint")
    lines.append("# TYPE ffapi_request_success_rate_ratio gauge")
    for path, stats in sorted(snapshot.get("per_endpoint", {}).items()):
        label = _prometheus_escape(path or "unknown")
        lines.append(f'ffapi_request_success_rate_ratio{{path="{label}"}} {stats.get("success_rate", 0.0)}')

    errors = snapshot.get("errors", {})
    lines.append("# HELP ffapi_request_errors_total Total request errors by key")
    lines.append("# TYPE ffapi_request_errors_total counter")
    if errors:
        for key, count in sorted(errors.items()):
            label = _prometheus_escape(str(key))
            lines.append(f'ffapi_request_errors_total{{error="{label}"}} {count}')
    else:
        lines.append('ffapi_request_errors_total{error="none"} 0')

    queue = snapshot.get("queue", {})
    lines.append("# HELP ffapi_queue_current_requests Current in-flight requests")
    lines.append("# TYPE ffapi_queue_current_requests gauge")
    lines.append(f"ffapi_queue_current_requests {queue.get('current', 0)}")
    lines.append("# HELP ffapi_queue_max_depth Maximum concurrent requests observed")
    lines.append("# TYPE ffapi_queue_max_depth gauge")
    lines.append(f"ffapi_queue_max_depth {queue.get('max', 0)}")
    lines.append("# HELP ffapi_queue_avg_wait_ms Average request wait time in milliseconds")
    lines.append("# TYPE ffapi_queue_avg_wait_ms gauge")
    lines.append(f"ffapi_queue_avg_wait_ms {queue.get('avg_wait_ms', 0.0)}")

    recent = snapshot.get("recent", {})
    lines.append("# HELP ffapi_recent_success_rate_ratio Success ratio across the recent window")
    lines.append("# TYPE ffapi_recent_success_rate_ratio gauge")
    success_rate = recent.get("success_rate")
    lines.append(f"ffapi_recent_success_rate_ratio {success_rate if success_rate is not None else 0.0}")
    lines.append("# HELP ffapi_recent_window_total Total recent requests considered in success ratio")
    lines.append("# TYPE ffapi_recent_window_total gauge")
    lines.append(f"ffapi_recent_window_total {recent.get('window', 0)}")

    body = "\n".join(lines) + "\n"
    return PlainTextResponse(body, media_type="text/plain; version=0.0.4")


# ---------- models ----------
class RendiJob(BaseModel):
    input_files: Dict[str, HttpUrl]
    output_files: Dict[str, str]
    ffmpeg_command: str

    @field_validator("input_files", mode="before")
    @classmethod
    def validate_input_urls(cls, value):
        if value is None:
            raise ValueError("input_files is required")
        for key, item in value.items():
            item_str = str(item)
            if not item_str.startswith(("http://", "https://")):
                raise ValueError(f"input file '{key}' must use http:// or https://")
        return value


class ConcatJob(BaseModel):
    clips: List[HttpUrl]
    width: int = 1920
    height: int = 1080
    fps: int = 30

    @field_validator("clips", mode="before")
    @classmethod
    def validate_clips(cls, value):
        if not value:
            raise ValueError("At least one clip is required")
        if isinstance(value, (str, bytes)):
            raise ValueError("Clips must be provided as a list of URLs")
        if len(value) > 100:
            raise ValueError("Too many clips (max 100)")
        for clip in value:
            clip_str = str(clip)
            if not clip_str.startswith(("http://", "https://")):
                raise ValueError("Only http:// and https:// clip URLs are supported")
        return value

    @field_validator("width", "height")
    @classmethod
    def validate_dimensions(cls, value: int) -> int:
        if not (1 <= value <= 7680):
            raise ValueError(f"Dimension must be 1-7680, got {value}")
        return value

    @field_validator("fps")
    @classmethod
    def validate_fps(cls, value: int) -> int:
        if not (1 <= value <= 240):
            raise ValueError(f"FPS must be 1-240, got {value}")
        return value


class ComposeFromUrlsJob(BaseModel):
    video_url: HttpUrl
    audio_url: Optional[HttpUrl] = None
    bgm_url: Optional[HttpUrl] = None
    duration: Optional[float] = None
    duration_ms: Optional[int] = None
    width: int = 1920
    height: int = 1080
    fps: int = 30
    bgm_volume: float = 0.3
    headers: Optional[Dict[str, str]] = None  # forwarded header subset
    webhook_url: Optional[HttpUrl] = None
    webhook_headers: Optional[Dict[str, str]] = None

    @field_validator("duration", mode="before")
    @classmethod
    def parse_duration(cls, value):
        if value is None:
            return None
        if isinstance(value, str):
            try:
                return float(value)
            except ValueError as exc:
                raise ValueError(f"duration must be a valid number, got '{value}'") from exc
        return float(value)

    @field_validator("duration")
    @classmethod
    def validate_duration_seconds(cls, value: Optional[float]) -> Optional[float]:
        if value is not None:
            if not (0.001 <= value <= 3600.0):
                raise ValueError("duration must be 0.001-3600 seconds")
        return value

    @field_validator("width", "height")
    @classmethod
    def validate_dimensions(cls, value: int) -> int:
        if not (1 <= value <= 7680):
            raise ValueError(f"Dimension must be 1-7680, got {value}")
        return value

    @field_validator("fps")
    @classmethod
    def validate_fps(cls, value: int) -> int:
        if not (1 <= value <= 240):
            raise ValueError(f"FPS must be 1-240, got {value}")
        return value

    @field_validator("bgm_volume")
    @classmethod
    def validate_volume(cls, value: float) -> float:
        if not (0.0 <= value <= 5.0):
            raise ValueError(f"bgm_volume must be between 0 and 5, got {value}")
        return value

    @field_validator("duration_ms")
    @classmethod
    def validate_duration_ms(cls, value: Optional[int]) -> Optional[int]:
        if value is not None:
            if not (1 <= value <= 3_600_000):
                raise ValueError("duration_ms must be 1-3600000")
        return value

    @field_validator("video_url", "audio_url", "bgm_url", "webhook_url", mode="before")
    @classmethod
    def validate_url_scheme(cls, value):
        if value is None:
            return value
        value_str = str(value)
        if not value_str.startswith(("http://", "https://")):
            raise ValueError("Only http:// and https:// URLs are supported")
        return value

    @field_validator("webhook_headers", mode="before")
    @classmethod
    def normalize_webhook_headers(cls, value):
        if value is None:
            return None
        if isinstance(value, dict):
            normalized: Dict[str, str] = {}
            for raw_key, raw_value in value.items():
                key = str(raw_key).strip()
                if not key:
                    raise ValueError("Webhook header names cannot be empty")
                normalized[key] = str(raw_value)
            return normalized
        raise ValueError("webhook_headers must be a mapping of header names to values")

    def model_post_init(self, __context: Any) -> None:
        if self.duration is None and self.duration_ms is None:
            self.duration_ms = 30000
        elif self.duration is not None and self.duration_ms is not None:
            raise ValueError("Provide either 'duration' or 'duration_ms', not both")
        elif self.duration is not None:
            self.duration_ms = int(self.duration * 1000)


class Keyframe(BaseModel):
    url: Optional[HttpUrl] = None
    timestamp: int = 0
    duration: int

    @field_validator("url", mode="before")
    @classmethod
    def validate_keyframe_url(cls, value):
        if value is None:
            return value
        value_str = str(value)
        if not value_str.startswith(("http://", "https://")):
            raise ValueError("Keyframe URLs must use http:// or https://")
        return value


class Track(BaseModel):
    id: str
    type: Literal["video", "audio"]
    keyframes: List[Keyframe]


class TracksComposeJob(BaseModel):
    tracks: List[Track]
    width: int = 1920
    height: int = 1080
    fps: int = 30

    @field_validator("width", "height")
    @classmethod
    def validate_dimensions(cls, value: int) -> int:
        if not (1 <= value <= 7680):
            raise ValueError(f"Dimension must be 1-7680, got {value}")
        return value

    @field_validator("fps")
    @classmethod
    def validate_fps(cls, value: int) -> int:
        if not (1 <= value <= 240):
            raise ValueError(f"FPS must be 1-240, got {value}")
        return value


# ---------- helpers ----------
ALLOWED_FORWARD_HEADERS_LOWER = {
    "cookie", "authorization", "x-n8n-api-key", "ngrok-skip-browser-warning"
}


def _escape_ffmpeg_concat_path(path: Path) -> str:
    return path.as_posix().replace("'", "'\\''")


def _probe_cache_key(url: str, params: Dict[str, Any]) -> str:
    cache_data = f"{url}:{json.dumps(params, sort_keys=True)}"
    return hashlib.sha256(cache_data.encode("utf-8")).hexdigest()[:16]


def _make_async_client() -> httpx.AsyncClient:
    return httpx.AsyncClient(follow_redirects=True)


async def _download_to(
    url: str,
    dest: Path,
    headers: Optional[Dict[str, str]] = None,
    max_retries: int = 3,
    chunk_size: int = 1024 * 1024,
    client: Optional[httpx.AsyncClient] = None,
) -> None:
    """Download file with retry, resume, and progress tracking."""

    base_headers: Dict[str, str] = {}
    if headers:
        for k, v in headers.items():
            if k.lower() in ALLOWED_FORWARD_HEADERS_LOWER:
                base_headers[k] = v

    dest.parent.mkdir(parents=True, exist_ok=True)
    check_disk_space(dest.parent)

    can_resume = True
    own_client = False
    session = client
    if session is None:
        session = _make_async_client()
        own_client = True

    try:
        for attempt in range(max_retries):
            try:
                req_hdr = dict(base_headers)
                existing_size = dest.stat().st_size if dest.exists() else 0

                if not can_resume and dest.exists():
                    try:
                        dest.unlink()
                    except Exception as cleanup_exc:
                        logger.warning("Failed to reset download %s: %s", dest, cleanup_exc)
                        _flush_logs()
                    existing_size = 0

                use_resume = can_resume and existing_size > 0

                if use_resume:
                    req_hdr["Range"] = f"bytes={existing_size}-"
                    logger.info(
                        "Resuming download from %.1fMB: %s",
                        existing_size / 1024 / 1024,
                        url,
                    )

                timeout = 600
                size_hint = None
                try:
                    head_headers = dict(req_hdr)
                    head_headers.pop("Range", None)
                    head_resp = await session.head(url, headers=head_headers, timeout=30)
                    head_resp.raise_for_status()
                    if "content-length" in head_resp.headers:
                        size_hint = int(head_resp.headers["content-length"])
                except Exception:
                    size_hint = None

                if size_hint is not None:
                    size_mb = size_hint / (1024 * 1024)
                    timeout = max(600, int(size_mb * 10))
                else:
                    timeout = 3600

                async with session.stream("GET", url, headers=req_hdr, timeout=timeout) as r:
                    r.raise_for_status()

                    if use_resume:
                        if r.status_code == 206:
                            logger.info("✓ Server supports resume for: %s", url)
                            mode = "ab"
                        elif r.status_code == 200:
                            logger.warning(
                                "⚠ Server doesn't support resume, downloading from start: %s",
                                url,
                            )
                            if dest.exists():
                                try:
                                    dest.unlink()
                                except Exception as cleanup_exc:
                                    logger.error(
                                        "Failed to remove stale partial %s: %s", dest, cleanup_exc
                                    )
                                    _flush_logs()
                                    raise HTTPException(
                                        status_code=500,
                                        detail="Failed to reset partial download",
                                    ) from cleanup_exc
                            mode = "wb"
                            existing_size = 0
                            can_resume = False
                        else:
                            logger.error(
                                "Unexpected status %s while resuming download %s",
                                r.status_code,
                                url,
                            )
                            _flush_logs()
                            raise HTTPException(
                                status_code=502,
                                detail=f"Unexpected status {r.status_code} when requesting range",
                            )
                    else:
                        mode = "wb"

                    if "content-length" in r.headers:
                        content_length = int(r.headers["content-length"])
                        if r.status_code == 206:
                            total_size = existing_size + content_length
                        else:
                            total_size = content_length
                    else:
                        total_size = 0

                    required_mb = MIN_FREE_SPACE_MB
                    if total_size:
                        remaining = total_size - existing_size
                        if remaining < 0:
                            remaining = 0
                        remaining_mb = (remaining + 1024 * 1024 - 1) // (1024 * 1024)
                        required_mb = max(
                            MIN_FREE_SPACE_MB, MIN_FREE_SPACE_MB + remaining_mb
                        )
                    check_disk_space(dest.parent, required_mb=required_mb)

                    downloaded = existing_size
                    last_log = downloaded

                    with dest.open(mode) as f:
                        async for chunk in r.aiter_bytes(chunk_size):
                            if chunk:
                                f.write(chunk)
                                downloaded += len(chunk)

                                if downloaded - last_log >= 50 * 1024 * 1024:
                                    if total_size:
                                        percent = (downloaded / total_size) * 100
                                        logger.info(
                                            "Download progress: %d%% (%.0fMB/%.0fMB)",
                                            int(percent),
                                            downloaded / 1024 / 1024,
                                            total_size / 1024 / 1024,
                                        )
                                    else:
                                        logger.info("Downloaded: %.0fMB", downloaded / 1024 / 1024)
                                    last_log = downloaded

                    if total_size and downloaded != total_size:
                        raise Exception(
                            f"Download incomplete: got {downloaded} bytes, expected {total_size}"
                        )

                    logger.info(
                        "Download complete: %.1fMB - %s", downloaded / 1024 / 1024, url
                    )
                    return

            except HTTPException:
                raise
            except Exception as e:
                if attempt < max_retries - 1:
                    wait_time = 2**attempt
                    logger.warning(
                        "Download failed (attempt %d/%d): %s",
                        attempt + 1,
                        max_retries,
                        e,
                    )
                    logger.warning("Retrying in %s seconds...", wait_time)
                    await asyncio.sleep(wait_time)
                else:
                    logger.error("Download failed after %d attempts: %s", max_retries, e)
                    _flush_logs()
                    if dest.exists():
                        try:
                            dest.unlink()
                        except Exception as cleanup_exc:
                            logger.warning("Failed to remove partial download %s: %s", dest, cleanup_exc)
                            _flush_logs()
                    raise
    finally:
        if own_client and session is not None:
            await session.aclose()


# ---------- routes ----------
@app.post("/image/to-mp4-loop")
async def image_to_mp4_loop(file: UploadFile = File(...), duration: int = 30, as_json: bool = False):
    logger.info(f"Starting image-to-mp4-loop: {file.filename}, duration={duration}s")
    if file.content_type not in {"image/png", "image/jpeg"}:
        raise HTTPException(status_code=400, detail="Only PNG/JPEG are supported.")
    if not (1 <= duration <= 3600):
        raise HTTPException(status_code=400, detail="duration must be 1..3600 seconds")
    check_disk_space(WORK_DIR)
    with TemporaryDirectory(prefix="loop_", dir=str(WORK_DIR)) as workdir:
        work = Path(workdir)
        in_path = work / ("input.png" if file.content_type == "image/png" else "input.jpg")
        out_path = work / "output.mp4"

        await stream_upload_to_path(file, in_path)

        cmd = [
            "ffmpeg", "-y",
            "-loop", "1",
            "-t", str(duration),
            "-i", str(in_path),
            "-c:v", "libx264", "-preset", "medium",
            "-tune", "stillimage",
            "-pix_fmt", "yuv420p",
            "-movflags", "+faststart",
            str(out_path),
        ]
        log = work / "ffmpeg.log"
        with log.open("w", encoding="utf-8", errors="ignore") as lf:
            code = run_ffmpeg_with_timeout(cmd, lf)
        save_log(log, "image-to-mp4")
        if code != 0 or not out_path.exists():
            logger.error(f"image-to-mp4-loop failed for {file.filename}")
            _flush_logs()
            return JSONResponse(status_code=500, content={"error": "ffmpeg_failed", "log": log.read_text()})
        pub = publish_file(out_path, ".mp4", duration_ms=duration * 1000)
        logger.info(f"image-to-mp4-loop completed: {pub['rel']}")
        if as_json:
            return {"ok": True, "file_url": pub["url"], "path": pub["dst"]}
        resp = FileResponse(pub["dst"], media_type="video/mp4", filename=os.path.basename(pub["dst"]))
        resp.headers["X-File-URL"] = pub["url"]
        return resp


@app.post("/compose/from-binaries")
async def compose_from_binaries(
    video: UploadFile = File(...),
    audio: Optional[UploadFile] = File(None),
    bgm: Optional[UploadFile] = File(None),
    duration_ms: int = Query(30000, ge=1, le=3600000),
    width: int = Query(1920, ge=1, le=7680),
    height: int = Query(1080, ge=1, le=7680),
    fps: int = Query(30, ge=1, le=240),
    bgm_volume: float = Query(0.3, ge=0.0, le=5.0),
    as_json: bool = False,
):
    logger.info(f"Starting compose-from-binaries: video={video.filename}, audio={audio.filename if audio else None}, bgm={bgm.filename if bgm else None}")
    ensure_upload_type(video, "video/", "video")
    if audio:
        ensure_upload_type(audio, "audio/", "audio")
    if bgm:
        ensure_upload_type(bgm, "audio/", "bgm")
    check_disk_space(WORK_DIR)
    with TemporaryDirectory(prefix="compose_", dir=str(WORK_DIR)) as workdir:
        work = Path(workdir)
        v_path = work / "video.mp4"
        a_path = work / "audio"
        b_path = work / "bgm"
        out_path = work / "output.mp4"

        await stream_upload_to_path(video, v_path)

        has_audio = False
        has_bgm = False
        if audio:
            await stream_upload_to_path(audio, a_path)
            has_audio = True
        if bgm:
            await stream_upload_to_path(bgm, b_path)
            has_bgm = True

        dur_s = f"{duration_ms/1000:.3f}"
        inputs = ["-i", str(v_path)]
        if has_audio: inputs += ["-i", str(a_path)]
        if has_bgm:   inputs += ["-i", str(b_path)]

        maps = ["-map", "0:v:0"]
        cmd = ["ffmpeg", "-y"] + inputs + [
            "-t", dur_s,
            "-vf", f"scale={width}:{height},fps={fps}",
            "-c:v", "libx264", "-preset", "medium", "-pix_fmt", "yuv420p",
            "-movflags", "+faststart",
        ]
        if has_audio and has_bgm:
            af = f"[1:a]anull[a1];[2:a]volume={bgm_volume}[a2];[a1][a2]amix=inputs=2:normalize=0:duration=shortest[aout]"
            cmd += ["-filter_complex", af, "-c:a", "aac", "-b:a", "128k", "-ar", "48000"]
            maps += ["-map", "[aout]"]
        elif has_audio:
            maps += ["-map", "1:a:0"]
            cmd += ["-c:a", "aac", "-b:a", "128k", "-ar", "48000"]
        elif has_bgm:
            cmd += ["-filter_complex", f"[1:a]volume={bgm_volume}[aout]"]
            maps += ["-map", "[aout]"]
            cmd += ["-c:a", "aac", "-b:a", "128k", "-ar", "48000"]
        cmd += maps + [str(out_path)]

        log = work / "ffmpeg.log"
        with log.open("w", encoding="utf-8", errors="ignore") as lf:
            code = run_ffmpeg_with_timeout(cmd, lf)
        save_log(log, "compose-binaries")
        if code != 0 or not out_path.exists():
            logger.error("compose-from-binaries failed")
            _flush_logs()
            return JSONResponse(status_code=500, content={"error": "ffmpeg_failed", "cmd": cmd, "log": log.read_text()})

        pub = publish_file(out_path, ".mp4", duration_ms=duration_ms)
        logger.info(f"compose-from-binaries completed: {pub['rel']}")
        if as_json:
            return {"ok": True, "file_url": pub["url"], "path": pub["dst"]}
        resp = FileResponse(pub["dst"], media_type="video/mp4", filename=os.path.basename(pub["dst"]))
        resp.headers["X-File-URL"] = pub["url"]
        return resp


@app.post("/video/concat-from-urls")
async def video_concat_from_urls(job: ConcatJob, as_json: bool = False):
    logger.info(f"Starting concat: {len(job.clips)} clips, {job.width}x{job.height}@{job.fps}fps")
    check_disk_space(WORK_DIR)
    with TemporaryDirectory(prefix="concat_", dir=str(WORK_DIR)) as workdir:
        work = Path(workdir)
        norm = []
        for i, url in enumerate(job.clips):
            raw = work / f"in_{i:03d}.bin"
            await _download_to(str(url), raw, None)
            out = work / f"norm_{i:03d}.mp4"
            cmd = [
                "ffmpeg", "-y",
                "-i", str(raw),
                "-vf", f"scale={job.width}:{job.height},fps={job.fps}",
                "-c:v", "libx264", "-preset", "medium", "-pix_fmt", "yuv420p",
                "-c:a", "aac", "-b:a", "128k", "-ar", "48000",
                "-movflags", "+faststart",
                str(out),
            ]
            log = work / f"norm_{i:03d}.log"
            with log.open("w", encoding="utf-8", errors="ignore") as lf:
                code = run_ffmpeg_with_timeout(cmd, lf)
            save_log(log, f"concat-norm-{i}")
            if code != 0 or not out.exists():
                return JSONResponse(status_code=500, content={"error": "ffmpeg_failed_on_clip", "clip": str(url), "log": log.read_text()})
            norm.append(out)
        listfile = work / "list.txt"
        with listfile.open("w", encoding="utf-8") as f:
            for p in norm:
                f.write(f"file '{_escape_ffmpeg_concat_path(p)}'\n")
        out_path = work / "output.mp4"
        cmd2 = ["ffmpeg", "-y", "-f", "concat", "-safe", "0", "-i", str(listfile), "-c", "copy", "-movflags", "+faststart", str(out_path)]
        log2 = work / "concat.log"
        with log2.open("w", encoding="utf-8", errors="ignore") as lf:
            code2 = run_ffmpeg_with_timeout(cmd2, lf)
        save_log(log2, "concat-final")
        if code2 != 0 or not out_path.exists():
            logger.error("concat failed at final stage")
            _flush_logs()
            return JSONResponse(status_code=500, content={"error": "ffmpeg_failed_concat", "log": log2.read_text()})
        pub = publish_file(out_path, ".mp4")
        logger.info(f"concat completed: {len(job.clips)} clips -> {pub['rel']}")
        if as_json:
            return {"ok": True, "file_url": pub["url"], "path": pub["dst"]}
        resp = FileResponse(pub["dst"], media_type="video/mp4", filename=os.path.basename(pub["dst"]))
        resp.headers["X-File-URL"] = pub["url"]
        return resp


class ConcatAliasJob(BaseModel):
    clips: Optional[List[HttpUrl]] = None
    urls: Optional[List[HttpUrl]] = None
    width: int = 1920
    height: int = 1080
    fps: int = 30

    @field_validator("clips", "urls", mode="before")
    @classmethod
    def validate_optional_clips(cls, value):
        if value is None:
            return value
        if not value:
            raise ValueError("Clip list cannot be empty")
        if isinstance(value, (str, bytes)):
            raise ValueError("Clips must be provided as a list of URLs")
        if len(value) > 100:
            raise ValueError("Too many clips (max 100)")
        for clip in value:
            clip_str = str(clip)
            if not clip_str.startswith(("http://", "https://")):
                raise ValueError("Only http:// and https:// clip URLs are supported")
        return value

    @field_validator("width", "height")
    @classmethod
    def validate_dimensions(cls, value: int) -> int:
        if not (1 <= value <= 7680):
            raise ValueError(f"Dimension must be 1-7680, got {value}")
        return value

    @field_validator("fps")
    @classmethod
    def validate_fps(cls, value: int) -> int:
        if not (1 <= value <= 240):
            raise ValueError(f"FPS must be 1-240, got {value}")
        return value

@app.post("/video/concat")
async def video_concat_alias(job: ConcatAliasJob, as_json: bool = False):
    clip_list = job.clips or job.urls
    if not clip_list:
        raise HTTPException(status_code=422, detail="Provide 'clips' (preferred) or 'urls' array of video URLs")
    cj = ConcatJob(clips=clip_list, width=job.width, height=job.height, fps=job.fps)
    return await video_concat_from_urls(cj, as_json=as_json)


async def _compose_from_urls_impl(
    job: ComposeFromUrlsJob, progress: Optional[JobProgressReporter] = None
) -> Dict[str, str]:
    check_disk_space(WORK_DIR)

    with TemporaryDirectory(prefix="cfu_", dir=str(WORK_DIR)) as workdir:
        work = Path(workdir)
        v_path = work / "video_in.mp4"
        a_path = work / "audio_in"
        b_path = work / "bgm_in"
        out_path = work / "output.mp4"
        log_path = work / "ffmpeg.log"

        if progress:
            progress.update(15, "Preparing workspace")
        await _download_to(str(job.video_url), v_path, job.headers)
        if progress:
            progress.update(35, "Downloaded primary video")
        has_audio = False
        has_bgm = False
        if job.audio_url:
            if progress:
                progress.update(45, "Downloading audio track")
            await _download_to(str(job.audio_url), a_path, job.headers)
            has_audio = True
        else:
            if progress:
                progress.update(45, "Audio track skipped")
        if job.bgm_url:
            if progress:
                progress.update(55, "Downloading background music")
            await _download_to(str(job.bgm_url), b_path, job.headers)
            has_bgm = True
        else:
            if progress:
                progress.update(55, "Background music skipped")

        dur_s = f"{job.duration_ms/1000:.3f}"
        inputs = ["-i", str(v_path)]
        if has_audio:
            inputs += ["-i", str(a_path)]
        if has_bgm:
            inputs += ["-i", str(b_path)]

        maps = ["-map", "0:v:0"]
        cmd = ["ffmpeg", "-y"] + inputs + [
            "-t", dur_s,
            "-vf", f"scale={job.width}:{job.height},fps={job.fps}",
            "-c:v", "libx264", "-preset", "medium", "-pix_fmt", "yuv420p",
            "-movflags", "+faststart",
        ]
        if has_audio and has_bgm:
            af = (
                f"[1:a]anull[a1];[2:a]volume={job.bgm_volume}[a2];"
                "[a1][a2]amix=inputs=2:normalize=0:duration=shortest[aout]"
            )
            cmd += [
                "-filter_complex",
                af,
                "-c:a",
                "aac",
                "-b:a",
                "128k",
                "-ar",
                "48000",
            ]
            maps += ["-map", "[aout]"]
        elif has_audio:
            maps += ["-map", "1:a:0"]
            cmd += ["-c:a", "aac", "-b:a", "128k", "-ar", "48000"]
        elif has_bgm:
            cmd += ["-filter_complex", f"[1:a]volume={job.bgm_volume}[aout]"]
            maps += ["-map", "[aout]"]
            cmd += ["-c:a", "aac", "-b:a", "128k", "-ar", "48000"]

        cmd += maps + [str(out_path)]
        parser: Optional[FFmpegProgressParser] = None
        if progress:
            progress.update(70, "Rendering composition")
            parser = FFmpegProgressParser(job.duration_ms / 1000.0, progress, "Rendering composition")
        with log_path.open("w", encoding="utf-8", errors="ignore") as logf:
            code = run_ffmpeg_with_timeout(cmd, logf, progress_parser=parser)
        save_log(log_path, "compose-urls")
        if code != 0 or not out_path.exists():
            logger.error("compose-from-urls failed")
            _flush_logs()
            raise HTTPException(
                status_code=500,
                detail={
                    "error": "ffmpeg_failed",
                    "cmd": cmd,
                    "log": log_path.read_text(),
                },
            )

        if progress:
            progress.update(85, "Saving rendered output")
        pub = publish_file(out_path, ".mp4", duration_ms=job.duration_ms)
        if progress:
            progress.update(95, "Publishing file")
        logger.info("compose-from-urls completed: %s", pub["rel"])
        return pub


@app.post("/compose/from-urls")
async def compose_from_urls(job: ComposeFromUrlsJob, as_json: bool = False):
    pub = await _compose_from_urls_impl(job)
    if as_json:
        return {"ok": True, "file_url": pub["url"], "path": pub["dst"]}
    resp = FileResponse(pub["dst"], media_type="video/mp4", filename=os.path.basename(pub["dst"]))
    resp.headers["X-File-URL"] = pub["url"]
    return resp


async def _notify_webhook_with_retry(
    client,
    webhook_url: str,
    payload: Dict[str, Any],
    headers: Dict[str, str],
    *,
    job_id: str,
    status: str,
    max_retries: int = 3,
    base_delay: float = 1.0,
) -> None:
    delay = base_delay
    last_error: Optional[Exception] = None
    for attempt in range(1, max_retries + 1):
        try:
            response = await client.post(
                webhook_url,
                json=payload,
                headers=headers,
                timeout=10,
            )
            status_code = getattr(response, "status_code", None)
            response.raise_for_status()
            struct_logger.info(
                "webhook_notified",
                job_id=job_id,
                status=status,
                status_code=status_code,
                attempt=attempt,
            )
            return
        except Exception as exc:
            last_error = exc
            logger.warning(
                "Webhook notification attempt %s failed for job %s: %s",
                attempt,
                job_id,
                exc,
            )
            if attempt == max_retries:
                break
            await asyncio.sleep(delay)
            delay *= 2

    if last_error is not None:
        struct_logger.error(
            "webhook_failed",
            job_id=job_id,
            status=status,
            attempts=max_retries,
            error=str(last_error),
        )


async def _notify_webhook(
    webhook_url: Optional[str],
    *,
    job_id: str,
    status: str,
    result: Optional[Dict[str, Any]] = None,
    error: Optional[str] = None,
    headers: Optional[Dict[str, str]] = None,
) -> None:
    if not webhook_url:
        return

    payload: Dict[str, Any] = {
        "job_id": job_id,
        "status": status,
        "timestamp": datetime.now(timezone.utc).isoformat(),
    }
    if result is not None:
        payload["result"] = result
    if error is not None:
        payload["error"] = error

    request_headers = {str(k): str(v) for k, v in (headers or {}).items()}
    client = _make_async_client()
    try:
        await _notify_webhook_with_retry(
            client,
            webhook_url,
            payload,
            request_headers,
            job_id=job_id,
            status=status,
        )
    finally:
        try:
            await client.aclose()
        except Exception:
            pass


async def _process_compose_from_urls_job(job_id: str, job: ComposeFromUrlsJob) -> None:
    with JOBS_LOCK:
        existing = JOBS.get(job_id, {})
        created = existing.get("created", time.time())
        now = time.time()
        history = list(existing.get("history", []))
        history.append({"timestamp": now, "progress": 10, "message": "Job started"})
        if len(history) > JOB_HISTORY_LIMIT:
            history = history[-JOB_HISTORY_LIMIT:]
        JOBS[job_id] = {
            **existing,
            "status": "processing",
            "progress": 10,
            "message": "Job started",
            "started": now,
            "created": created,
            "updated": now,
            "history": history,
        }

    reporter = JobProgressReporter(job_id)
    start = time.perf_counter()
    struct_logger.info("job_started", job_id=job_id, job_type="compose_from_urls")
    try:
        pub = await _compose_from_urls_impl(job, reporter)
    except HTTPException as exc:
        reporter.update(100, "Composition failed")
        detail = exc.detail if isinstance(exc.detail, (str, dict)) else str(exc.detail)
        struct_logger.error(
            "job_failed",
            job_id=job_id,
            job_type="compose_from_urls",
            status_code=exc.status_code,
            error=detail,
        )
        with JOBS_LOCK:
            existing = JOBS.get(job_id, {})
            created = existing.get("created", time.time())
            history = list(existing.get("history", []))
            history.append({"timestamp": time.time(), "progress": 100, "message": "Failed"})
            if len(history) > JOB_HISTORY_LIMIT:
                history = history[-JOB_HISTORY_LIMIT:]
            JOBS[job_id] = {
                **existing,
                "status": "failed",
                "progress": 100,
                "message": "Failed",
                "status_code": exc.status_code,
                "error": detail,
                "created": created,
                "updated": time.time(),
                "history": history,
            }
        await _notify_webhook(
            str(job.webhook_url) if job.webhook_url else None,
            job_id=job_id,
            status="failed",
            error=detail,
            headers=job.webhook_headers,
        )
        return
    except Exception as exc:
        reporter.update(100, "Unexpected failure")
        logger.exception("Async compose job %s failed", job_id)
        struct_logger.error(
            "job_failed",
            job_id=job_id,
            job_type="compose_from_urls",
            status_code=500,
            error=str(exc),
        )
        with JOBS_LOCK:
            existing = JOBS.get(job_id, {})
            created = existing.get("created", time.time())
            history = list(existing.get("history", []))
            history.append({"timestamp": time.time(), "progress": 100, "message": "Failed"})
            if len(history) > JOB_HISTORY_LIMIT:
                history = history[-JOB_HISTORY_LIMIT:]
            JOBS[job_id] = {
                **existing,
                "status": "failed",
                "progress": 100,
                "message": "Failed",
                "status_code": 500,
                "error": str(exc),
                "created": created,
                "updated": time.time(),
                "history": history,
            }
        await _notify_webhook(
            str(job.webhook_url) if job.webhook_url else None,
            job_id=job_id,
            status="failed",
            error=str(exc),
            headers=job.webhook_headers,
        )
        return

    reporter.update(100, "Completed")
    duration_ms = (time.perf_counter() - start) * 1000.0
    with JOBS_LOCK:
        existing = JOBS.get(job_id, {})
        created = existing.get("created", time.time())
        history = list(existing.get("history", []))
        history.append({"timestamp": time.time(), "progress": 100, "message": "Completed"})
        if len(history) > JOB_HISTORY_LIMIT:
            history = history[-JOB_HISTORY_LIMIT:]
        JOBS[job_id] = {
            **existing,
            "status": "finished",
            "progress": 100,
            "message": "Completed",
            "result": {
                "file_url": pub["url"],
                "path": pub["dst"],
                "rel": pub["rel"],
                "thumbnail": pub.get("thumbnail"),
            },
            "duration_ms": duration_ms,
            "created": created,
            "updated": time.time(),
            "history": history,
        }
    struct_logger.info(
        "job_completed",
        job_id=job_id,
        job_type="compose_from_urls",
        duration_ms=duration_ms,
        output_rel=pub["rel"],
    )
    await _notify_webhook(
        str(job.webhook_url) if job.webhook_url else None,
        job_id=job_id,
        status="finished",
        result={
            "file_url": pub["url"],
            "path": pub["dst"],
            "rel": pub["rel"],
            "thumbnail": pub.get("thumbnail"),
        },
        headers=job.webhook_headers,
    )


@app.post("/compose/from-urls/async")
async def compose_from_urls_async(job: ComposeFromUrlsJob):
    cleanup_old_jobs()
    job_id = str(uuid4())
    with JOBS_LOCK:
        now = time.time()
        JOBS[job_id] = {
            "status": "queued",
            "progress": 0,
            "message": "Queued",
            "history": [{"timestamp": now, "progress": 0, "message": "Queued"}],
            "created": now,
            "updated": now,
        }
    job_copy = job.model_copy(deep=True)
    asyncio.create_task(_process_compose_from_urls_job(job_id, job_copy))
    return {"job_id": job_id, "status_url": f"/jobs/{job_id}"}


@app.get("/jobs", response_class=HTMLResponse)
def jobs_history(
    request: Request,
    status: Optional[str] = None,
    page: int = Query(1, ge=1),
    page_size: int = Query(25, ge=1, le=100),
):
    redirect = ensure_dashboard_access(request)
    if redirect:
        return redirect

    with JOBS_LOCK:
        all_jobs = [
            {**data, "job_id": job_id}
            for job_id, data in JOBS.items()
        ]

    all_jobs.sort(key=lambda item: item.get("created", 0), reverse=True)
    if status:
        all_jobs = [job for job in all_jobs if job.get("status") == status]

    total_items = len(all_jobs)
    total_pages = max(1, math.ceil(total_items / page_size))
    page = min(max(page, 1), total_pages)
    start = (page - 1) * page_size
    end = start + page_size
    jobs_slice = all_jobs[start:end]

    rows: List[str] = []
    for job in jobs_slice:
        job_id = job.get("job_id", "-")
        status_val = job.get("status", "unknown")
        created_ts = job.get("created") or 0
        created = datetime.fromtimestamp(created_ts, tz=timezone.utc)
        duration_ms = job.get("duration_ms")
        duration_text = f"{(duration_ms or 0) / 1000:.1f}s" if duration_ms else "—"
        status_class = {
            "finished": "success",
            "failed": "error",
            "processing": "warning",
            "queued": "info",
        }.get(status_val, "")
        rows.append(
            """
            <tr>
                <td><code>{job_id}</code></td>
                <td><span class="status {status_class}">{status_val}</span></td>
                <td>{created}</td>
                <td>{duration}</td>
                <td><a href="/jobs/{job_id}" target="_blank">View</a></td>
            </tr>
            """.format(
                job_id=html.escape(str(job_id)),
                status_class=status_class,
                status_val=html.escape(str(status_val)),
                created=created.strftime("%Y-%m-%d %H:%M:%S UTC"),
                duration=duration_text,
            )
        )

    if not rows:
        rows.append("<tr><td colspan='5'>No jobs found</td></tr>")

    filters: List[str] = []
    for filter_status in [None, "finished", "failed", "processing", "queued"]:
        label = filter_status or "All"
        params = {"page": 1}
        if filter_status:
            params["status"] = filter_status
        query = urlencode(params)
        css_class = "active" if filter_status == status else ""
        filters.append(
            f"<a href='/jobs?{query}' class='filter-link {css_class}'>{html.escape(label)}</a>"
        )

    pagination = f"Page {page} of {total_pages}"

    nav_html = render_nav("/jobs", indent="        ")
    html_content = f"""
    <!doctype html>
    <html>
    <head>
        <meta charset="utf-8" />
        <title>Job History</title>
        <style>
            body {{ font-family: system-ui, sans-serif; padding: 24px; max-width: 1400px; margin: 0 auto; }}
            .brand {{ font-size: 32px; font-weight: bold; margin-bottom: 20px; }}
            .brand .ff {{ color: #28a745; }}
            .brand .api {{ color: #000; }}
            nav {{ margin-bottom: 20px; display: flex; gap: 12px; flex-wrap: wrap; }}
            nav a {{ color: #0066cc; text-decoration: none; font-weight: 500; }}
            nav a:hover {{ text-decoration: underline; }}
            .filters {{ margin-bottom: 16px; display: flex; gap: 12px; flex-wrap: wrap; }}
            .filter-link {{ padding: 6px 12px; border-radius: 4px; background: #f0f0f0; text-decoration: none; color: #333; }}
            .filter-link.active {{ background: #0066cc; color: white; }}
            table {{ border-collapse: collapse; width: 100%; background: #fff; box-shadow: 0 1px 2px rgba(0,0,0,0.08); }}
            th, td {{ border-bottom: 1px solid #e2e8f0; padding: 12px 14px; font-size: 14px; text-align: left; }}
            th {{ background: #f8fafc; color: #1f2937; }}
            code {{ background: #f3f4f6; padding: 2px 6px; border-radius: 3px; }}
            .status {{ padding: 4px 8px; border-radius: 4px; font-weight: 600; text-transform: capitalize; }}
            .status.success {{ background: #e8f5e9; color: #256029; }}
            .status.error {{ background: #fdecea; color: #b3261e; }}
            .status.warning {{ background: #fff3cd; color: #856404; }}
            .status.info {{ background: #e0f2fe; color: #0369a1; }}
            .pagination {{ margin-top: 16px; color: #666; }}
        </style>
    </head>
    <body>
        <div class="brand"><span class="ff">ff</span><span class="api">api</span></div>
{nav_html}
        <h2>Job History</h2>
        <div class="filters">{''.join(filters)}</div>
        <table>
            <thead>
                <tr>
                    <th>Job ID</th>
                    <th>Status</th>
                    <th>Created</th>
                    <th>Duration</th>
                    <th>Action</th>
                </tr>
            </thead>
            <tbody>{''.join(rows)}</tbody>
        </table>
        <p class="pagination">{pagination}</p>
    </body>
    </html>
    """
    return HTMLResponse(html_content)


@app.get("/jobs/{job_id}")
async def job_status(job_id: str):
    cleanup_old_jobs()
    with JOBS_LOCK:
        data = JOBS.get(job_id)
    if data is None:
        raise HTTPException(status_code=404, detail="job_not_found")
    payload = {"job_id": job_id}
    payload.update(data)
    return payload


@app.post("/compose/from-tracks")
async def compose_from_tracks(job: TracksComposeJob, as_json: bool = False):
    video_urls: List[str] = []
    audio_urls: List[str] = []
    has_valid_video_keyframe = False
    max_dur = 0
    for track in job.tracks:
        for keyframe in track.keyframes:
            duration_value: Optional[int] = None
            if keyframe.duration is not None:
                duration_value = int(keyframe.duration)
                if duration_value <= 0:
                    raise HTTPException(status_code=400, detail="Keyframe duration must be positive")
                max_dur = max(max_dur, duration_value)
            if keyframe.url:
                if track.type == "video":
                    video_urls.append(str(keyframe.url))
                    if duration_value and duration_value > 0:
                        has_valid_video_keyframe = True
                elif track.type == "audio":
                    audio_urls.append(str(keyframe.url))
    if not video_urls:
        raise HTTPException(status_code=400, detail="No video URLs found in tracks")
    if not has_valid_video_keyframe:
        raise HTTPException(status_code=400, detail="No valid video keyframes with URLs found in tracks")
    if max_dur <= 0:
        raise HTTPException(status_code=400, detail="At least one keyframe must include a duration")

    check_disk_space(WORK_DIR)
    with TemporaryDirectory(prefix="tracks_", dir=str(WORK_DIR)) as workdir:
        work = Path(workdir)
        video_paths: List[Path] = []
        for index, url in enumerate(video_urls):
            v_path = work / f"video_{index:03d}.mp4"
            await _download_to(url, v_path, None)
            video_paths.append(v_path)

        if len(video_paths) == 1:
            v_in = video_paths[0]
        else:
            concat_list = work / "videos.txt"
            with concat_list.open("w", encoding="utf-8") as handle:
                for path in video_paths:
                    handle.write(f"file '{_escape_ffmpeg_concat_path(path)}'\n")
            v_in = work / "video_concat.mp4"
            concat_log = work / "ffmpeg_concat.log"
            concat_cmd = [
                "ffmpeg",
                "-y",
                "-f",
                "concat",
                "-safe",
                "0",
                "-i",
                str(concat_list),
                "-c:v",
                "libx264",
                "-preset",
                "medium",
                "-pix_fmt",
                "yuv420p",
                "-an",
                str(v_in),
            ]
            with concat_log.open("w", encoding="utf-8", errors="ignore") as lf:
                concat_code = run_ffmpeg_with_timeout(concat_cmd, lf)
            save_log(concat_log, "compose-tracks-concat")
            if concat_code != 0 or not v_in.exists():
                logger.error("compose-from-tracks concat failed")
                _flush_logs()
                raise HTTPException(status_code=500, detail="Failed to combine video tracks")
        a_ins: List[Path] = []
        for i, url in enumerate(audio_urls):
            p = work / f"aud_{i:03d}.bin"
            await _download_to(url, p, None)
            a_ins.append(p)

        dur_s = f"{max_dur/1000:.3f}"
        inputs = ["-i", str(v_in)]
        for p in a_ins:
            inputs += ["-i", str(p)]
        maps = ["-map", "0:v:0"]
        cmd = ["ffmpeg", "-y"] + inputs + [
            "-t", dur_s,
            "-vf", f"scale={job.width}:{job.height},fps={job.fps}",
            "-c:v", "libx264", "-preset", "medium", "-pix_fmt", "yuv420p",
            "-movflags", "+faststart",
        ]
        if a_ins:
            labels = "".join([f"[{i+1}:a]" for i in range(len(a_ins))])
            af = f"{labels}amix=inputs={len(a_ins)}:normalize=0:duration=shortest[aout]"
            cmd += ["-filter_complex", af, "-c:a", "aac", "-b:a", "128k", "-ar", "48000"]
            maps += ["-map", "[aout]"]
        cmd += maps + [str(work / "output.mp4")]

        log = work / "ffmpeg.log"
        with log.open("w", encoding="utf-8", errors="ignore") as lf:
            code = run_ffmpeg_with_timeout(cmd, lf)
        save_log(log, "compose-tracks")
        out_path = work / "output.mp4"
        if code != 0 or not out_path.exists():
            logger.error("compose-from-tracks failed")
            _flush_logs()
            return JSONResponse(status_code=500, content={"error": "ffmpeg_failed", "log": log.read_text()})

        pub = publish_file(out_path, ".mp4")
        if as_json:
            return {"ok": True, "file_url": pub["url"], "path": pub["dst"]}
        resp = FileResponse(pub["dst"], media_type="video/mp4", filename=os.path.basename(pub["dst"]))
        resp.headers["X-File-URL"] = pub["url"]
        return resp


@app.post("/v1/run-ffmpeg-command")
async def run_rendi(job: RendiJob):
    check_disk_space(WORK_DIR)
    with TemporaryDirectory(prefix="rendi_", dir=str(WORK_DIR)) as workdir:
        work = Path(workdir)
        resolved = {}
        for key, url in job.input_files.items():
            p = work / f"{key}"
            await _download_to(str(url), p, None)
            resolved[key] = str(p)

        out_paths: Dict[str, Path] = {}
        for key, name in job.output_files.items():
            out_paths[key] = work / name

        cmd_text = job.ffmpeg_command
        for k, p in resolved.items():
            cmd_text = cmd_text.replace("{{" + k + "}}", p)
        for k, p in out_paths.items():
            cmd_text = cmd_text.replace("{{" + k + "}}", str(p))

        cmd_lower = cmd_text.lower()
        dangerous_patterns = ["-f lavfi", "-i /dev/", "-i file:", "-i concat:"]
        for pattern in dangerous_patterns:
            if pattern in cmd_lower:
                raise HTTPException(status_code=400, detail={"error": "forbidden_pattern", "pattern": pattern})
        if REQUIRE_DURATION_LIMIT and "-t" not in cmd_lower and "-frames" not in cmd_lower:
            raise HTTPException(
                status_code=400,
                detail={"error": "missing_limit", "detail": "Command must include -t or -frames"},
            )

        try:
            args = shlex.split(cmd_text)
        except Exception as exc:
            raise HTTPException(status_code=400, detail={"error": "invalid_command", "msg": str(exc)}) from exc

        log = work / "ffmpeg.log"
        run_args = ["ffmpeg"] + args if args and args[0] != "ffmpeg" else args
        with log.open("w", encoding="utf-8", errors="ignore") as lf:
            code = run_ffmpeg_with_timeout(run_args, lf)
        save_log(log, "rendi-command")
        if code != 0:
            logger.error("run-ffmpeg-command failed")
            _flush_logs()
            return JSONResponse(status_code=500, content={"error": "ffmpeg_failed", "cmd": run_args, "log": log.read_text()})

        published = {}
        for key, p in out_paths.items():
            if p.exists():
                pub = publish_file(p, Path(p).suffix or ".bin")
                published[key] = pub["url"]
        if not published:
            return JSONResponse(status_code=500, content={"error": "no_outputs_found", "log": log.read_text()})
        return {"ok": True, "outputs": published}


# ---------- ffprobe endpoints ----------
class ProbeUrlJob(BaseModel):
    url: HttpUrl
    headers: Optional[Dict[str, str]] = None
    show_format: bool = True
    show_streams: bool = True
    show_chapters: bool = False
    show_programs: bool = False
    show_packets: bool = False
    count_frames: bool = False
    count_packets: bool = False
    probe_size: Optional[str] = None
    analyze_duration: Optional[str] = None
    select_streams: Optional[str] = None

    @field_validator("url", mode="before")
    @classmethod
    def validate_probe_url(cls, value):
        if value is None:
            raise ValueError("url is required")
        value_str = str(value)
        if not value_str.startswith(("http://", "https://")):
            raise ValueError("Only http:// and https:// URLs are supported")
        return value

def _ffprobe_cmd_base(
    show_format: bool,
    show_streams: bool,
    show_chapters: bool,
    show_programs: bool,
    show_packets: bool,
    count_frames: bool,
    count_packets: bool,
    probe_size: Optional[str],
    analyze_duration: Optional[str],
    select_streams: Optional[str],
):
    cmd = ["ffprobe", "-v", "error", "-print_format", "json"]
    if show_format: cmd += ["-show_format"]
    if show_streams: cmd += ["-show_streams"]
    if show_chapters: cmd += ["-show_chapters"]
    if show_programs: cmd += ["-show_programs"]
    if show_packets: cmd += ["-show_packets"]
    if count_frames: cmd += ["-count_frames", "1"]
    if count_packets: cmd += ["-count_packets", "1"]
    if probe_size: cmd += ["-probesize", probe_size]
    if analyze_duration: cmd += ["-analyzeduration", analyze_duration]
    if select_streams: cmd += ["-select_streams", select_streams]
    return cmd

def _headers_kv_list(h: Optional[Dict[str, str]]) -> List[str]:
    if not h: return []
    out = []
    for k, v in h.items():
        if k.lower() in ALLOWED_FORWARD_HEADERS_LOWER:
            out += ["-headers", f"{k}: {v}"]
    return out

@app.post("/probe/from-urls")
def probe_from_urls(job: ProbeUrlJob):
    cache_params = {
        "show_format": job.show_format,
        "show_streams": job.show_streams,
        "show_chapters": job.show_chapters,
        "show_programs": job.show_programs,
        "show_packets": job.show_packets,
        "count_frames": job.count_frames,
        "count_packets": job.count_packets,
        "probe_size": job.probe_size,
        "analyze_duration": job.analyze_duration,
        "select_streams": job.select_streams,
        "headers": sorted((k.lower(), v) for k, v in (job.headers or {}).items()),
    }
    cache_key = _probe_cache_key(str(job.url), cache_params)
    cached: Optional[Tuple[dict, float]] = None
    now = time.time()
    if PROBE_CACHE_TTL > 0:
        with PROBE_CACHE_LOCK:
            cached = PROBE_CACHE.get(cache_key)
    if cached is not None:
        result, timestamp = cached
        if now - timestamp < PROBE_CACHE_TTL:
            logger.info("Probe cache hit for %s", job.url)
            return result

    cmd = _ffprobe_cmd_base(
        job.show_format,
        job.show_streams,
        job.show_chapters,
        job.show_programs,
        job.show_packets,
        job.count_frames,
        job.count_packets,
        job.probe_size,
        job.analyze_duration,
        job.select_streams,
    )
    cmd += _headers_kv_list(job.headers)
    cmd += [str(job.url)]
    proc = subprocess.run(
        cmd,
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
        timeout=60,
    )
    if proc.returncode != 0:
        raise HTTPException(
            status_code=500,
            detail={
                "error": "ffprobe_failed",
                "stderr": proc.stderr.decode("utf-8", "ignore"),
                "cmd": cmd,
            },
        )
    try:
        result = json.loads(proc.stdout.decode("utf-8", "ignore"))
    except Exception as exc:
        raise HTTPException(status_code=500, detail={"error": "parse_failed", "msg": str(exc)}) from exc

    if PROBE_CACHE_TTL > 0:
        with PROBE_CACHE_LOCK:
            PROBE_CACHE[cache_key] = (result, time.time())
        logger.info("Probe result cached for %s", job.url)
    return result

@app.post("/probe/from-binary")
async def probe_from_binary(
    file: UploadFile = File(...),
    show_format: bool = True,
    show_streams: bool = True,
    show_chapters: bool = False,
    show_programs: bool = False,
    show_packets: bool = False,
    count_frames: bool = False,
    count_packets: bool = False,
    probe_size: Optional[str] = None,
    analyze_duration: Optional[str] = None,
    select_streams: Optional[str] = None,
):
    check_disk_space(WORK_DIR)
    with TemporaryDirectory(prefix="probe_", dir=str(WORK_DIR)) as workdir:
        p = Path(workdir) / (file.filename or "input.bin")

        await stream_upload_to_path(file, p)

        cmd = _ffprobe_cmd_base(show_format, show_streams, show_chapters, show_programs, show_packets,
                                count_frames, count_packets, probe_size, analyze_duration, select_streams) + [str(p)]
        proc = subprocess.run(
            cmd,
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            timeout=60,
        )
        if proc.returncode != 0:
            raise HTTPException(status_code=500, detail={"error": "ffprobe_failed", "stderr": proc.stderr.decode("utf-8", "ignore"), "cmd": cmd})
        try:
            return json.loads(proc.stdout.decode("utf-8", "ignore"))
        except Exception as e:
            raise HTTPException(status_code=500, detail={"error": "parse_failed", "msg": str(e)})

@app.get("/probe/public")
def probe_public(
    rel: str,
    show_format: bool = True,
    show_streams: bool = True,
    show_chapters: bool = False,
    show_programs: bool = False,
    show_packets: bool = False,
    count_frames: bool = False,
    count_packets: bool = False,
    probe_size: Optional[str] = None,
    analyze_duration: Optional[str] = None,
    select_streams: Optional[str] = None,
):
    target = safe_path_check(PUBLIC_DIR, rel)
    if not target.exists() or not target.is_file():
        raise HTTPException(status_code=404, detail="file not found")
    cmd = _ffprobe_cmd_base(show_format, show_streams, show_chapters, show_programs, show_packets,
                            count_frames, count_packets, probe_size, analyze_duration, select_streams) + [str(target)]
    proc = subprocess.run(
        cmd,
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
        timeout=60,
    )
    if proc.returncode != 0:
        raise HTTPException(status_code=500, detail={"error": "ffprobe_failed", "stderr": proc.stderr.decode("utf-8", "ignore"), "cmd": cmd})
    try:
        return json.loads(proc.stdout.decode("utf-8", "ignore"))
    except Exception as e:
        raise HTTPException(status_code=500, detail={"error": "parse_failed", "msg": str(e)})


def custom_openapi():
    """Enhanced OpenAPI schema with descriptive metadata and server info."""
    if app.openapi_schema:
        return app.openapi_schema

    openapi_schema = get_openapi(
        title="FFAPI Ultimate",
        version="2.0.0",
        description=(
            "## FFmpeg-powered Media Processing API\n\n"
            "Complete video/audio processing toolkit with:\n"
            "- 🎬 Video composition and editing\n"
            "- 🔗 URL concatenation\n"
            "- 🖼️ Image to video conversion\n"
            "- 🔍 Media inspection (FFprobe)\n"
            "- ⚡ Async job processing\n"
            "- 🔐 Optional authentication\n\n"
            "### Quick Start\n"
            "1. Enable API authentication in `/settings` (optional)\n"
            "2. Generate API key in `/api-keys` (optional)\n"
            "3. Use endpoints with `X-API-Key` header or `api_key` param\n"
            "4. Poll `/jobs/{job_id}` for async operations\n"
        ),
        routes=app.routes,
    )
    openapi_schema["servers"] = [
        {
            "url": PUBLIC_BASE_URL or "http://localhost:3000",
            "description": "Current server",
        }
    ]
    app.openapi_schema = openapi_schema
    return app.openapi_schema


app.openapi = custom_openapi
