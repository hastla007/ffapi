import os, io, shlex, json, subprocess, random, string, shutil, time, asyncio, html, types
from collections import Counter, defaultdict, deque
from datetime import datetime, timedelta
from dataclasses import dataclass
from pathlib import Path
from tempfile import TemporaryDirectory
from typing import Dict, List, Optional, Literal
from uuid import uuid4

import threading

try:
    import httpx
except ModuleNotFoundError:  # pragma: no cover - fallback for minimal environments
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

        async def aclose(self) -> None:
            await asyncio.to_thread(self._session.close)

    httpx = types.SimpleNamespace(AsyncClient=_AsyncClient)
from fastapi import FastAPI, UploadFile, File, HTTPException, Query
from fastapi.responses import FileResponse, HTMLResponse, JSONResponse, RedirectResponse, PlainTextResponse
from fastapi.staticfiles import StaticFiles
from pydantic import BaseModel, HttpUrl, field_validator

app = FastAPI()


class RateLimiter:
    def __init__(self, requests_per_minute: int = 60) -> None:
        self._limits: defaultdict[str, List[datetime]] = defaultdict(list)
        self._rpm = max(requests_per_minute, 1)
        self._lock = threading.Lock()

    def check(self, identifier: str) -> bool:
        now = datetime.utcnow()
        cutoff = now - timedelta(minutes=1)
        with self._lock:
            timestamps = [t for t in self._limits[identifier] if t > cutoff]
            if len(timestamps) >= self._rpm:
                self._limits[identifier] = timestamps
                return False
            timestamps.append(now)
            self._limits[identifier] = timestamps
            return True

    def reset(self) -> None:
        with self._lock:
            self._limits.clear()


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

JOBS: Dict[str, Dict[str, object]] = {}
JOBS_LOCK = threading.Lock()

# Mount static /files
app.mount("/files", StaticFiles(directory=str(PUBLIC_DIR)), name="files")


@app.middleware("http")
async def metrics_middleware(request, call_next):
    path = request.url.path
    arrival = time.perf_counter()
    METRICS.request_started()

    identifier = "unknown"
    if request.client:
        identifier = request.client.host or "unknown"

    if not RATE_LIMITER.check(identifier):
        METRICS.request_finished(path, 429, 0.0, 0.0, "rate_limited")
        METRICS.request_completed()
        return PlainTextResponse("Too Many Requests", status_code=429)

    processing_start = time.perf_counter()
    wait_time = processing_start - arrival
    try:
        response = await call_next(request)
        duration = time.perf_counter() - processing_start
        METRICS.request_finished(path, response.status_code, duration, wait_time)
        return response
    except HTTPException as exc:
        duration = time.perf_counter() - processing_start
        detail = exc.detail if isinstance(exc.detail, str) else exc.__class__.__name__
        METRICS.request_finished(path, exc.status_code, duration, wait_time, detail)
        raise
    except Exception as exc:
        duration = time.perf_counter() - processing_start
        METRICS.request_finished(path, 500, duration, wait_time, exc.__class__.__name__)
        raise
    finally:
        METRICS.request_completed()


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
file_handler = logging.StreamHandler(file_stream)
file_handler.setLevel(logging.INFO)
file_handler.setFormatter(logging.Formatter('%(asctime)s - %(name)s - %(levelname)s - %(message)s'))

# Create console handler
console_handler = logging.StreamHandler(sys.stdout)
console_handler.setLevel(logging.INFO)
console_handler.setFormatter(logging.Formatter('%(asctime)s - %(name)s - %(levelname)s - %(message)s'))

# Configure root logger to catch everything (including uvicorn)
logging.basicConfig(
    level=logging.INFO,
    handlers=[file_handler, console_handler]
)

logger = logging.getLogger("ffapi")

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

# Force flush
file_handler.flush()

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
    return {"available": available, "version": version_line, "error": error}


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


def run_ffmpeg_with_timeout(cmd: List[str], log_handle) -> int:
    try:
        proc = subprocess.Popen(cmd, stdout=log_handle, stderr=log_handle)
    except Exception as exc:
        logger.error("Failed to launch ffmpeg command %s: %s", cmd, exc)
        _flush_logs()
        raise HTTPException(status_code=500, detail="Failed to start ffmpeg") from exc
    try:
        return proc.wait(timeout=FFMPEG_TIMEOUT_SECONDS)
    except subprocess.TimeoutExpired:
        proc.terminate()
        try:
            proc.wait(timeout=5)
        except subprocess.TimeoutExpired:
            proc.kill()
        logger.warning("FFmpeg timed out after %s seconds: %s", FFMPEG_TIMEOUT_SECONDS, cmd)
        _flush_logs()
        raise HTTPException(status_code=504, detail="Processing timeout")

def cleanup_old_public(days: int = RETENTION_DAYS):
    """Delete files older than specified days based on actual file modification time."""
    if days <= 0:
        return
    cutoff_timestamp = time.time() - (days * 86400)  # days * seconds_per_day
    
    deleted_count = 0
    for child in PUBLIC_DIR.iterdir():
        if child.is_dir():
            # Check all files in this directory
            files_to_delete = []
            for file_path in child.iterdir():
                if file_path.is_file():
                    try:
                        file_mtime = file_path.stat().st_mtime
                        if file_mtime < cutoff_timestamp:
                            files_to_delete.append(file_path)
                    except Exception as exc:
                        logger.warning("Failed to inspect file %s: %s", file_path, exc)
            
            # Delete old files
            for fp in files_to_delete:
                try:
                    fp.unlink()
                    deleted_count += 1
                except Exception as exc:
                    logger.warning("Failed to delete expired file %s: %s", fp, exc)
            
            # Remove directory if empty
            try:
                if not any(child.iterdir()):
                    child.rmdir()
            except Exception as exc:
                logger.warning("Failed to remove empty directory %s: %s", child, exc)
    
    if deleted_count > 0:
        logger.info(f"Cleanup: deleted {deleted_count} files older than {days} days")


async def _periodic_public_cleanup():
    """Periodically clean up expired files from the public directory."""
    try:
        # Stagger the first run so startup work can finish
        await asyncio.sleep(PUBLIC_CLEANUP_INTERVAL_SECONDS)
    except asyncio.CancelledError:
        raise
    while True:
        try:
            cleanup_old_public()
        except asyncio.CancelledError:
            raise
        except Exception as exc:
            logger.warning("Periodic public cleanup failed: %s", exc)
        try:
            await asyncio.sleep(PUBLIC_CLEANUP_INTERVAL_SECONDS)
        except asyncio.CancelledError:
            raise


def publish_file(src: Path, ext: str) -> Dict[str, str]:
    """Move a finished file into PUBLIC_DIR/YYYYMMDD/ and return URLs/paths.
       Uses shutil.move to be cross-device safe (works across Docker volumes/Windows)."""
    check_disk_space(PUBLIC_DIR)
    day = datetime.utcnow().strftime("%Y%m%d")
    folder = PUBLIC_DIR / day
    folder.mkdir(parents=True, exist_ok=True)
    check_disk_space(folder)
    name = datetime.utcnow().strftime("%Y%m%d_%H%M%S_") + _rand() + ext
    dst = folder / name

    # Cross-device safe move
    shutil.move(str(src), str(dst))

    logger.info(f"Published file: {name} ({dst.stat().st_size / (1024*1024):.2f} MB)")

    rel = f"/files/{day}/{name}"
    url = f"{PUBLIC_BASE_URL.rstrip('/')}{rel}" if PUBLIC_BASE_URL else rel
    return {"dst": str(dst), "url": url, "rel": rel}


def save_log(log_path: Path, operation: str) -> None:
    """Save ffmpeg log to persistent logs directory."""
    if not log_path.exists():
        return
    check_disk_space(LOGS_DIR)
    day = datetime.utcnow().strftime("%Y%m%d")
    folder = LOGS_DIR / day
    folder.mkdir(parents=True, exist_ok=True)
    check_disk_space(folder)
    name = datetime.utcnow().strftime("%Y%m%d_%H%M%S_") + _rand() + f"_{operation}.log"
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
def downloads():
    rows = []
    for day in sorted(PUBLIC_DIR.iterdir(), reverse=True):
        if not day.is_dir():
            continue
        for f in sorted(day.iterdir(), reverse=True):
            if not f.is_file():
                continue
            rel = f"/files/{day.name}/{f.name}"
            size_mb = f.stat().st_size / (1024*1024)
            rows.append(
                "<tr><td>{day}</td><td><a href='{href}'>{name}</a></td><td>{size:.2f} MB</td></tr>".format(
                    day=html.escape(day.name),
                    href=html.escape(rel, quote=True),
                    name=html.escape(f.name),
                    size=size_mb,
                )
            )
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
        table {{ border-collapse: collapse; width: 100%; }}
        th, td {{ border-bottom: 1px solid #eee; padding: 8px 10px; font-size: 14px; }}
        th {{ text-align: left; background: #fafafa; }}
        a {{ text-decoration: none; }}
        nav {{ margin-bottom: 20px; }}
        nav a {{ margin-right: 15px; color: #0066cc; }}
      </style>
    </head>
    <body>
      <div class="brand"><span class="ff">ff</span><span class="api">api</span></div>
      <nav>
        <a href="/downloads">Downloads</a>
        <a href="/logs">Logs</a>
        <a href="/ffmpeg">FFmpeg Info</a>
        <a href="/metrics">Metrics</a>
        <a href="/documentation">API Docs</a>
      </nav>
      <h2>Generated Files</h2>
      <table>
        <thead><tr><th>Date</th><th>File</th><th>Size</th></tr></thead>
        <tbody>{"".join(rows) if rows else "<tr><td colspan='3'>No files yet</td></tr>"} </tbody>
      </table>
    </body>
    </html>
    """
    return HTMLResponse(html_content)


@app.get("/logs", response_class=HTMLResponse)
def logs():
    rows = []
    for day in sorted(LOGS_DIR.iterdir(), reverse=True):
        if not day.is_dir():
            continue
        for f in sorted(day.iterdir(), reverse=True):
            if not f.is_file():
                continue
            size_kb = f.stat().st_size / 1024
            # Extract operation name from filename
            parts = f.name.split("_")
            operation = parts[-1].replace(".log", "") if len(parts) > 3 else "unknown"
            rows.append(
                "<tr><td>{day}</td><td>{name}</td><td>{op}</td><td>{size:.1f} KB</td><td><a href='/logs/view?path={path}' target='_blank'>View</a></td></tr>".format(
                    day=html.escape(day.name),
                    name=html.escape(f.name),
                    op=html.escape(operation),
                    size=size_kb,
                    path=html.escape(f"{day.name}/{f.name}", quote=True),
                )
            )
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
        table {{ border-collapse: collapse; width: 100%; }}
        th, td {{ border-bottom: 1px solid #eee; padding: 8px 10px; font-size: 14px; }}
        th {{ text-align: left; background: #fafafa; }}
        a {{ text-decoration: none; color: #0066cc; }}
        nav {{ margin-bottom: 20px; }}
        nav a {{ margin-right: 15px; }}
      </style>
    </head>
    <body>
      <div class="brand"><span class="ff">ff</span><span class="api">api</span></div>
      <nav>
        <a href="/downloads">Downloads</a>
        <a href="/logs">Logs</a>
        <a href="/ffmpeg">FFmpeg Info</a>
        <a href="/metrics">Metrics</a>
        <a href="/documentation">API Docs</a>
      </nav>
      <h2>FFmpeg Logs</h2>
      <table>
        <thead><tr><th>Date</th><th>Filename</th><th>Operation</th><th>Size</th><th>Action</th></tr></thead>
        <tbody>{"".join(rows) if rows else "<tr><td colspan='5'>No logs yet</td></tr>"} </tbody>
      </table>
    </body>
    </html>
    """
    return HTMLResponse(html_content)


@app.get("/logs/view", response_class=PlainTextResponse)
def view_log(path: str):
    """View individual log file content."""
    target = safe_path_check(LOGS_DIR, path)
    if not target.exists() or not target.is_file():
        raise HTTPException(status_code=404, detail="Log not found")
    return target.read_text(encoding="utf-8", errors="ignore")


@app.get("/ffmpeg", response_class=HTMLResponse)
def ffmpeg_info(auto_refresh: int = 0):
    """Display FFmpeg version and container logs.
    
    Args:
        auto_refresh: Auto-refresh interval in seconds (0 = disabled, max 60)
    """
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
            mtime = datetime.fromtimestamp(stat.st_mtime).strftime("%Y-%m-%d %H:%M:%S")
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
    current_time = datetime.utcnow().strftime("%Y-%m-%d %H:%M:%S UTC")

    version_output_safe = html.escape(version_output)
    app_logs_safe = html.escape(app_logs)
    log_info_safe = html.escape(log_info)

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
      <nav>
        <a href="/downloads">Downloads</a>
        <a href="/logs">Logs</a>
        <a href="/ffmpeg">FFmpeg Info</a>
        <a href="/metrics">Metrics</a>
        <a href="/documentation">API Docs</a>
      </nav>
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
def documentation():
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
    <span class="param">duration_ms</span> - Duration in milliseconds (1-3600000, default: 30000)<br>
    <span class="param">width</span> - Output width in pixels (default: 1920)<br>
    <span class="param">height</span> - Output height in pixels (default: 1080)<br>
    <span class="param">fps</span> - Output frames per second (default: 30)<br>
    <span class="param">bgm_volume</span> - BGM volume multiplier (default: 0.3)<br>
    <span class="param">headers</span> - HTTP headers for authenticated requests (optional)<br>
    <span class="param">as_json</span> - Return JSON instead of file (default: false)
  </div>
  <div class="response">Returns: MP4 file or {"ok": true, "file_url": "...", "path": "..."}</div>
  <div class="example">Example:<br>curl -X POST http://localhost:3000/compose/from-urls \<br>  -H "Content-Type: application/json" \<br>  -d '{<br>    "video_url": "https://example.com/video.mp4",<br>    "audio_url": "https://example.com/audio.mp3",<br>    "bgm_url": "https://example.com/music.mp3",<br>    "duration_ms": 20000,<br>    "width": 1280,<br>    "height": 720,<br>    "fps": 30,<br>    "bgm_volume": 0.3,<br>    "as_json": true<br>  }'</div>
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
      <nav>
        <a href="/downloads">Downloads</a>
        <a href="/logs">Logs</a>
        <a href="/ffmpeg">FFmpeg Info</a>
        <a href="/metrics">Metrics</a>
        <a href="/documentation">API Docs</a>
      </nav>
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
def metrics_dashboard():
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
      <nav>
        <a href="/downloads">Downloads</a>
        <a href="/logs">Logs</a>
        <a href="/ffmpeg">FFmpeg Info</a>
        <a href="/metrics">Metrics</a>
        <a href="/documentation">API Docs</a>
      </nav>

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


# ---------- models ----------
class RendiJob(BaseModel):
    input_files: Dict[str, HttpUrl]
    output_files: Dict[str, str]
    ffmpeg_command: str


class ConcatJob(BaseModel):
    clips: List[HttpUrl]
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


class ComposeFromUrlsJob(BaseModel):
    video_url: HttpUrl
    audio_url: Optional[HttpUrl] = None
    bgm_url: Optional[HttpUrl] = None
    duration_ms: int = 30000
    width: int = 1920
    height: int = 1080
    fps: int = 30
    bgm_volume: float = 0.3
    headers: Optional[Dict[str, str]] = None  # forwarded header subset

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


class Keyframe(BaseModel):
    url: Optional[HttpUrl] = None
    timestamp: int = 0
    duration: int


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
                                    logger.warning(
                                        "Failed to remove stale partial %s: %s", dest, cleanup_exc
                                    )
                                    _flush_logs()
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
        with log.open("wb") as lf:
            code = run_ffmpeg_with_timeout(cmd, lf)
        save_log(log, "image-to-mp4")
        if code != 0 or not out_path.exists():
            logger.error(f"image-to-mp4-loop failed for {file.filename}")
            _flush_logs()
            return JSONResponse(status_code=500, content={"error": "ffmpeg_failed", "log": log.read_text()})
        pub = publish_file(out_path, ".mp4")
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
        with log.open("wb") as lf:
            code = run_ffmpeg_with_timeout(cmd, lf)
        save_log(log, "compose-binaries")
        if code != 0 or not out_path.exists():
            logger.error("compose-from-binaries failed")
            _flush_logs()
            return JSONResponse(status_code=500, content={"error": "ffmpeg_failed", "cmd": cmd, "log": log.read_text()})

        pub = publish_file(out_path, ".mp4")
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
            with log.open("wb") as lf:
                code = run_ffmpeg_with_timeout(cmd, lf)
            save_log(log, f"concat-norm-{i}")
            if code != 0 or not out.exists():
                return JSONResponse(status_code=500, content={"error": "ffmpeg_failed_on_clip", "clip": str(url), "log": log.read_text()})
            norm.append(out)
        listfile = work / "list.txt"
        with listfile.open("w", encoding="utf-8") as f:
            for p in norm:
                f.write(f"file '{p.as_posix()}'\n")
        out_path = work / "output.mp4"
        cmd2 = ["ffmpeg", "-y", "-f", "concat", "-safe", "0", "-i", str(listfile), "-c", "copy", "-movflags", "+faststart", str(out_path)]
        log2 = work / "concat.log"
        with log2.open("wb") as lf:
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


async def _compose_from_urls_impl(job: ComposeFromUrlsJob) -> Dict[str, str]:
    if job.duration_ms <= 0 or job.duration_ms > 3600000:
        raise HTTPException(status_code=400, detail="invalid duration_ms (1..3600000)")

    check_disk_space(WORK_DIR)

    with TemporaryDirectory(prefix="cfu_", dir=str(WORK_DIR)) as workdir:
        work = Path(workdir)
        v_path = work / "video_in.mp4"
        a_path = work / "audio_in"
        b_path = work / "bgm_in"
        out_path = work / "output.mp4"
        log_path = work / "ffmpeg.log"

        await _download_to(str(job.video_url), v_path, job.headers)
        has_audio = False
        has_bgm = False
        if job.audio_url:
            await _download_to(str(job.audio_url), a_path, job.headers)
            has_audio = True
        if job.bgm_url:
            await _download_to(str(job.bgm_url), b_path, job.headers)
            has_bgm = True

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
        with log_path.open("wb") as logf:
            code = run_ffmpeg_with_timeout(cmd, logf)
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

        pub = publish_file(out_path, ".mp4")
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


async def _process_compose_from_urls_job(job_id: str, job: ComposeFromUrlsJob) -> None:
    with JOBS_LOCK:
        JOBS[job_id]["status"] = "processing"
        JOBS[job_id]["progress"] = 10
        JOBS[job_id]["started"] = time.time()

    start = time.perf_counter()
    try:
        pub = await _compose_from_urls_impl(job)
    except HTTPException as exc:
        detail = exc.detail if isinstance(exc.detail, (str, dict)) else str(exc.detail)
        with JOBS_LOCK:
            JOBS[job_id] = {
                "status": "failed",
                "progress": 100,
                "status_code": exc.status_code,
                "error": detail,
            }
        return
    except Exception as exc:
        logger.exception("Async compose job %s failed", job_id)
        with JOBS_LOCK:
            JOBS[job_id] = {
                "status": "failed",
                "progress": 100,
                "status_code": 500,
                "error": str(exc),
            }
        return

    duration_ms = (time.perf_counter() - start) * 1000.0
    with JOBS_LOCK:
        JOBS[job_id] = {
            "status": "finished",
            "progress": 100,
            "result": {
                "file_url": pub["url"],
                "path": pub["dst"],
                "rel": pub["rel"],
            },
            "duration_ms": duration_ms,
        }


@app.post("/compose/from-urls/async")
async def compose_from_urls_async(job: ComposeFromUrlsJob):
    job_id = str(uuid4())
    with JOBS_LOCK:
        JOBS[job_id] = {"status": "queued", "progress": 0, "created": time.time()}
    job_copy = job.model_copy(deep=True)
    asyncio.create_task(_process_compose_from_urls_job(job_id, job_copy))
    return {"job_id": job_id, "status_url": f"/jobs/{job_id}"}


@app.get("/jobs/{job_id}")
async def job_status(job_id: str):
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
    primary_video_url: Optional[str] = None
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
                        if primary_video_url is None:
                            primary_video_url = str(keyframe.url)
                elif track.type == "audio":
                    audio_urls.append(str(keyframe.url))
    if not video_urls:
        raise HTTPException(status_code=400, detail="No video URLs found in tracks")
    if primary_video_url is None:
        raise HTTPException(status_code=400, detail="No valid video keyframes with URLs found in tracks")
    if max_dur <= 0:
        raise HTTPException(status_code=400, detail="At least one keyframe must include a duration")

    check_disk_space(WORK_DIR)
    with TemporaryDirectory(prefix="tracks_", dir=str(WORK_DIR)) as workdir:
        work = Path(workdir)
        v_in = work / "video.mp4"
        await _download_to(primary_video_url, v_in, None)
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
        with log.open("wb") as lf:
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
        with log.open("wb") as lf:
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

def _headers_kv_list(h: Optional[Dict[str, str]]) -> list:
    if not h: return []
    out = []
    for k, v in h.items():
        if k.lower() in ALLOWED_FORWARD_HEADERS_LOWER:
            out += ["-headers", f"{k}: {v}"]
    return out

@app.post("/probe/from-urls")
def probe_from_urls(job: ProbeUrlJob):
    cmd = _ffprobe_cmd_base(job.show_format, job.show_streams, job.show_chapters, job.show_programs, job.show_packets,
                            job.count_frames, job.count_packets, job.probe_size, job.analyze_duration, job.select_streams)
    cmd += _headers_kv_list(job.headers)
    cmd += [str(job.url)]
    proc = subprocess.run(cmd, stdout=subprocess.PIPE, stderr=subprocess.PIPE)
    if proc.returncode != 0:
        raise HTTPException(status_code=500, detail={"error": "ffprobe_failed", "stderr": proc.stderr.decode("utf-8", "ignore"), "cmd": cmd})
    try:
        return json.loads(proc.stdout.decode("utf-8", "ignore"))
    except Exception as e:
        raise HTTPException(status_code=500, detail={"error": "parse_failed", "msg": str(e)})

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
        proc = subprocess.run(cmd, stdout=subprocess.PIPE, stderr=subprocess.PIPE)
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
    proc = subprocess.run(cmd, stdout=subprocess.PIPE, stderr=subprocess.PIPE)
    if proc.returncode != 0:
        raise HTTPException(status_code=500, detail={"error": "ffprobe_failed", "stderr": proc.stderr.decode("utf-8", "ignore"), "cmd": cmd})
    try:
        return json.loads(proc.stdout.decode("utf-8", "ignore"))
    except Exception as e:
        raise HTTPException(status_code=500, detail={"error": "parse_failed", "msg": str(e)})
