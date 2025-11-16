import os, io, shlex, json, subprocess, random, string, shutil, time, asyncio, html, types, atexit, hmac, hashlib, secrets, base64, struct, math, ipaddress
from collections import Counter, defaultdict, deque, OrderedDict
from contextlib import asynccontextmanager
from contextvars import ContextVar
from datetime import datetime, timedelta, timezone
from dataclasses import dataclass
from pathlib import Path
from tempfile import TemporaryDirectory
from typing import Any, Callable, Dict, List, Literal, Optional, Tuple, Deque, Type, Union
from urllib.parse import urlencode, quote, parse_qs, urlparse
from uuid import uuid4

import html
import re
import signal
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
from fastapi import FastAPI, UploadFile, File, HTTPException, Query, Request, Response
from fastapi.responses import FileResponse, HTMLResponse, JSONResponse, RedirectResponse, PlainTextResponse
from fastapi.templating import Jinja2Templates
from fastapi.staticfiles import StaticFiles
from fastapi.openapi.utils import get_openapi
from pydantic import BaseModel, Field, HttpUrl, field_validator, model_validator
from starlette.datastructures import FormData

from fastapi_csrf_protect import CsrfProtect, CsrfProtectException
try:  # pragma: no cover - optional dependency for QR generation
    import qrcode  # type: ignore
except ModuleNotFoundError:  # pragma: no cover - environments without qrcode
    qrcode = None  # type: ignore[assignment]


@asynccontextmanager
async def lifespan(app):
    """Lifespan event handler for FastAPI application startup and shutdown."""
    # Startup
    logger.info("FastAPI server is ready to accept requests")
    log_gpu_status()
    try:
        cleanup_old_public()
    except Exception as exc:
        logger.warning("Initial public cleanup failed: %s", exc)
    if PUBLIC_CLEANUP_INTERVAL_SECONDS > 0:
        asyncio.create_task(_periodic_public_cleanup())
        asyncio.create_task(_periodic_jobs_cleanup())
    asyncio.create_task(_periodic_session_cleanup())
    asyncio.create_task(_periodic_rate_limiter_cleanup())
    asyncio.create_task(_periodic_stuck_job_cleanup())
    if PROBE_CACHE_TTL > 0:
        asyncio.create_task(_periodic_cache_cleanup())
    _flush_logs()

    yield

    # Shutdown (cleanup if needed)
    logger.info("FastAPI server is shutting down")


app = FastAPI(lifespan=lifespan)


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

TEMPLATES_DIR = Path(__file__).resolve().parent / "templates"
try:
    templates: Optional[Jinja2Templates] = Jinja2Templates(directory=str(TEMPLATES_DIR))
except AssertionError:  # pragma: no cover - environments without Jinja2
    templates = None

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
LOGIN_RATE_LIMITER = RateLimiter(5)

_FFMPEG_VERSION_CACHE: Optional[Dict[str, Optional[str]]] = None

JOBS: Dict[str, Dict[str, object]] = {}
JOBS_LOCK = threading.Lock()
JOB_PROCESSES: Dict[str, Any] = {}
JOB_PROCESSES_LOCK = threading.Lock()
JOB_PROGRESS: Dict[str, Dict[str, Any]] = {}
JOB_PROGRESS_LOCK = threading.Lock()
SETTINGS_LOCK = threading.Lock()
TEMPO_HISTORY: Deque[Dict[str, Any]] = deque(maxlen=200)
TEMPO_HISTORY_LOCK = threading.Lock()


@asynccontextmanager
async def safe_lock(lock: threading.Lock, *, timeout: float = 5.0, operation: str = "lock"):
    """Acquire a threading lock with timeout protection."""
    acquired = False
    try:
        acquired = await asyncio.wait_for(asyncio.to_thread(lock.acquire), timeout=timeout)
        if not acquired:
            raise TimeoutError(f"Failed to acquire {operation} within {timeout}s")
        yield
    except asyncio.TimeoutError as exc:
        logger.error("Lock timeout on %s", operation)
        raise HTTPException(status_code=500, detail=f"Lock timeout: {operation}") from exc
    finally:
        if acquired:
            try:
                lock.release()
            except Exception as exc:
                logger.error("Error releasing %s: %s", operation, exc)


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
PROBE_CACHE_MAX_SIZE = 1000
PROBE_CACHE_LOCK = threading.Lock()


class JobProcessHandle:
    __slots__ = ("process",)

    def __init__(self) -> None:
        self.process: Optional[Any] = None


class JobTransaction:
    """Context manager for atomic job updates."""

    def __init__(self, job_id: str):
        self.job_id = job_id
        self.updates: Dict[str, Any] = {}

    def __enter__(self) -> "JobTransaction":
        return self

    def __exit__(
        self,
        exc_type: Optional[Type[BaseException]],
        exc_val: Optional[BaseException],
        exc_tb: Optional[Any],
    ) -> None:
        if exc_type is None and self.updates:
            with JOBS_LOCK:
                existing = JOBS.get(self.job_id, {})
                JOBS[self.job_id] = {**existing, **self.updates}

    def set(self, **kwargs: Any) -> None:
        self.updates.update(kwargs)


class LRUCache:
    def __init__(self, max_size: int = 1000, ttl: int = 3600) -> None:
        self._cache: "OrderedDict[str, Tuple[dict, float]]" = OrderedDict()
        self._max_size = max(0, max_size)
        self._ttl = max(0, ttl)

    def get(self, key: str) -> Optional[Tuple[dict, float]]:
        entry = self._cache.get(key)
        if entry is None:
            return None
        value, timestamp = entry
        if self._ttl and time.time() - timestamp >= self._ttl:
            self._cache.pop(key, None)
            return None
        self._cache.move_to_end(key)
        return value, timestamp

    def set(self, key: str, value: dict) -> None:
        self._cache[key] = (value, time.time())
        self._cache.move_to_end(key)
        if self._max_size and len(self._cache) > self._max_size:
            self._cache.popitem(last=False)

    def cleanup_expired(self) -> int:
        if not self._ttl:
            return 0
        cutoff = time.time() - self._ttl
        removed = 0
        for cache_key, (_, timestamp) in list(self._cache.items()):
            if timestamp < cutoff:
                self._cache.pop(cache_key, None)
                removed += 1
        return removed

    def clear(self) -> None:
        self._cache.clear()


PROBE_CACHE = LRUCache(max_size=PROBE_CACHE_MAX_SIZE, ttl=PROBE_CACHE_TTL)


# ========== GPU Acceleration Helpers ==========


class GPUState:
    """Thread-safe GPU runtime state tracking."""

    def __init__(self) -> None:
        self._lock = threading.Lock()
        self._runtime_disabled = False
        self._filter_fallback_warned_at: Optional[float] = None

    def disable_runtime(self) -> None:
        with self._lock:
            self._runtime_disabled = True

    def is_runtime_disabled(self) -> bool:
        with self._lock:
            return self._runtime_disabled

    def should_warn_filter_fallback(self) -> bool:
        with self._lock:
            now = time.time()
            if self._filter_fallback_warned_at is None:
                self._filter_fallback_warned_at = now
                return True
            if now - self._filter_fallback_warned_at >= 600:
                self._filter_fallback_warned_at = now
                return True
            return False


GPU_STATE = GPUState()


def get_gpu_config() -> Dict[str, Any]:
    """Get GPU configuration from environment variables."""
    enabled = os.getenv("ENABLE_GPU", "false").lower() == "true"
    if GPU_STATE.is_runtime_disabled():
        enabled = False
    return {
        "enabled": enabled,
        "encoder": os.getenv("GPU_ENCODER", "h264_nvenc"),
        "decoder": os.getenv("GPU_DECODER", "h264_cuvid"),
        "device": os.getenv("GPU_DEVICE", "0"),
    }


def _nvenc_supports_modern_presets() -> bool:
    """Return True if the installed FFmpeg uses the modern NVENC preset names."""

    version_line = ffmpeg_snapshot().get("version") or ""
    match = re.search(r"ffmpeg version\s+(?:n)?(\d+)(?:\.(\d+))?", version_line)
    if not match:
        return False
    major = int(match.group(1))
    minor = int(match.group(2) or 0)
    if major > 5:
        return True
    if major == 5 and minor >= 1:
        return True
    return False


def get_video_encoder(*, force_cpu: bool = False) -> Tuple[str, List[str]]:
    """
    Get appropriate video encoder and preset args based on GPU availability.

    Args:
        force_cpu: If True, always return CPU encoder regardless of GPU config

    Returns:
        Tuple of (codec_name, extra_args)
    """

    # Force CPU encoding if requested
    if force_cpu:
        return ("libx264", ["-preset", "medium"])

    gpu_config = get_gpu_config()

    if not gpu_config["enabled"]:
        return ("libx264", ["-preset", "medium"])

    encoder = gpu_config["encoder"]

    if "nvenc" in encoder:
        preset_args = (
            ["-preset", "p1", "-tune", "hq"]
            if _nvenc_supports_modern_presets()
            else ["-preset", "medium"]
        )
        return (
            encoder,
            preset_args
            + ["-rc", "vbr", "-cq", "28", "-b:v", "5M", "-maxrate", "10M", "-bufsize", "10M"],
        )
    elif "amf" in encoder:
        return (
            encoder,
            ["-quality", "balanced", "-rc", "vbr_latency", "-qp_i", "23", "-qp_p", "23"],
        )
    elif "qsv" in encoder:
        return (encoder, ["-preset", "medium", "-global_quality", "23"])

    logger.warning(f"Unknown GPU encoder {encoder}, falling back to CPU")
    return ("libx264", ["-preset", "medium"])


def get_video_decoder(input_path: Optional[Path] = None) -> List[str]:
    """
    Get appropriate video decoder args based on GPU availability.

    Args:
        input_path: Optional path to input file for format detection

    Returns:
        List of FFmpeg args for hardware decoding
    """

    gpu_config = get_gpu_config()

    if not gpu_config["enabled"]:
        return []

    decoder = gpu_config["decoder"]

    # input_path is accepted for future format-specific handling
    _ = input_path

    if "cuvid" in decoder:
        return [
            "-hwaccel",
            "cuda",
            "-hwaccel_device",
            gpu_config["device"],
        ]
    elif "amf" in decoder or decoder == "h264":
        return ["-hwaccel", "auto"]
    elif "qsv" in decoder:
        return ["-hwaccel", "qsv", "-hwaccel_device", f"/dev/dri/renderD{128 + int(gpu_config['device'])}"]

    return []


def _decoder_args_use_hw_frames(decoder_args: List[str]) -> bool:
    """Return True if the decoder args enable hardware frames transfer."""

    try:
        idx = decoder_args.index("-hwaccel")
    except ValueError:
        return False

    if idx + 1 >= len(decoder_args):
        return False

    accel = decoder_args[idx + 1]
    return accel in {"cuda", "qsv"}


def get_scaling_filter(width: int, height: int, codec: str, use_gpu_filters: bool) -> str:
    """Get appropriate scaling filter based on GPU availability."""

    if not use_gpu_filters:
        return f"scale={width}:{height}"

    if "nvenc" in codec:
        return f"scale_cuda={width}:{height}"
    if "amf" in codec:
        return f"scale={width}:{height},format=nv12"
    if "qsv" in codec:
        return f"scale_qsv={width}:{height}"

    return f"scale={width}:{height}"


def build_encode_args(
    width: int,
    height: int,
    fps: int,
    *,
    decoder_args: Optional[List[str]] = None,
    prefer_gpu_filters: bool = True,
    force_cpu_encoder: bool = False,
) -> List[str]:
    """Build complete encoding arguments including filters and codec settings."""

    codec, codec_args = get_video_encoder(force_cpu=force_cpu_encoder)
    decoder_args = decoder_args or []

    # Check environment variable for GPU filters
    gpu_filters_env = os.getenv("ENABLE_GPU_FILTERS", "false").lower() == "true"

    # Enable GPU filters if:
    # 1. Environment variable is true
    # 2. Caller prefers GPU filters
    # 3. Hardware decoding is active (decoder_args present)
    use_gpu_filters = (
        gpu_filters_env
        and prefer_gpu_filters
        and bool(decoder_args)
        and _decoder_args_use_hw_frames(decoder_args)
    )

    if gpu_filters_env and not use_gpu_filters:
        if not decoder_args:
            logger.debug("GPU filters disabled: no hardware decoder active")
        elif not _decoder_args_use_hw_frames(decoder_args):
            logger.debug("GPU filters disabled: decoder doesn't use hardware frames")

    scale_filter = get_scaling_filter(width, height, codec, use_gpu_filters)

    filter_chain = f"{scale_filter},fps={fps}"

    using_gpu_encoder = codec in [
        "h264_nvenc",
        "hevc_nvenc",
        "av1_nvenc",
        "h264_amf",
        "hevc_amf",
        "h264_qsv",
        "hevc_qsv",
    ]
    cpu_decode = not decoder_args or not _decoder_args_use_hw_frames(decoder_args)

    if using_gpu_encoder and cpu_decode and not use_gpu_filters:
        if "nvenc" in codec:
            filter_chain += ",format=nv12,hwupload_cuda"
            logger.debug("Added hwupload_cuda for CPU->NVENC pipeline")
        elif "qsv" in codec:
            filter_chain += ",format=nv12,hwupload=extra_hw_frames=64"
            logger.debug("Added hwupload for CPU->QSV pipeline")
        elif "amf" in codec:
            filter_chain += ",format=nv12"
            logger.debug("Added format=nv12 for CPU->AMF pipeline")

    args = [
        "-vf",
        filter_chain,
        "-c:v",
        codec,
    ] + codec_args + [
        "-pix_fmt",
        "yuv420p",
        "-movflags",
        "+faststart",
        "-threads",
        "0",  # auto-detect cores
    ]

    return args


def log_gpu_status() -> None:
    """Log current GPU configuration at startup."""

    gpu_config = get_gpu_config()
    requested_enabled = os.getenv("ENABLE_GPU", "false").lower() == "true"

    logger.info("=" * 60)
    if gpu_config["enabled"]:
        logger.info("GPU Acceleration ENABLED")
        logger.info(f"  Encoder: {gpu_config['encoder']}")
        logger.info(f"  Decoder: {gpu_config['decoder']}")
        logger.info(f"  Device: {gpu_config['device']}")

        try:
            result = subprocess.run(
                ["nvidia-smi", "--query-gpu=driver_version,name", "--format=csv,noheader"],
                capture_output=True,
                text=True,
                timeout=5,
            )
            if result.returncode == 0 and result.stdout.strip():
                logger.info(f"  GPU Info: {result.stdout.strip()}")
        except Exception:
            logger.warning("  Could not query GPU information (nvidia-smi not available)")

        try:
            test_cmd = ["ffmpeg", "-hide_banner", "-encoders"]
            result = subprocess.run(test_cmd, capture_output=True, text=True, timeout=5)
            if gpu_config["encoder"] in result.stdout:
                logger.info(f"✓ GPU encoder '{gpu_config['encoder']}' is available")
            else:
                logger.warning(
                    f"⚠ GPU encoder '{gpu_config['encoder']}' not found in FFmpeg; disabling GPU acceleration"
                )
                GPU_STATE.disable_runtime()
                os.environ["ENABLE_GPU"] = "false"
                logger.info("GPU acceleration disabled for this process due to missing encoder")
        except Exception as exc:
            logger.warning(f"Could not verify GPU encoder: {exc}")

        if GPU_STATE.is_runtime_disabled():
            logger.info("GPU Acceleration DISABLED - using CPU encoding")
    else:
        if requested_enabled and GPU_STATE.is_runtime_disabled():
            logger.info("GPU Acceleration DISABLED - runtime fallback to CPU encoding")
        else:
            logger.info("GPU Acceleration DISABLED - using CPU encoding")

    logger.info("=" * 60)


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
            is_valid = entry is not None and not entry.get("used", True)

            if is_valid:
                entry["used"] = True
                self._backup_codes[hashed] = entry
                self._totp_attempts.pop(self.username, None)

            return is_valid

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


def _split_whitelist_entries(text: str) -> List[str]:
    entries: List[str] = []
    if not text:
        return entries
    for raw_line in text.replace(",", "\n").splitlines():
        entry = raw_line.strip()
        if entry:
            entries.append(entry)
    return entries


class APIWhitelist:
    def __init__(self) -> None:
        self._lock = threading.Lock()
        self._enabled = False
        self._entries: List[str] = []
        self._networks: List[ipaddress._BaseNetwork] = []

    def reset(self) -> None:
        with self._lock:
            self._enabled = False
            self._entries.clear()
            self._networks.clear()

    def is_enabled(self) -> bool:
        with self._lock:
            return self._enabled

    def get_entries(self) -> List[str]:
        with self._lock:
            return list(self._entries)

    def configure(self, enabled: bool, entries: List[str]) -> None:
        normalized: List[str] = []
        networks: List[ipaddress._BaseNetwork] = []
        for raw_entry in entries:
            entry = raw_entry.strip()
            if not entry:
                continue
            try:
                if "/" in entry:
                    network = ipaddress.ip_network(entry, strict=False)
                    display = str(network)
                else:
                    address = ipaddress.ip_address(entry)
                    display = str(address)
                    network = ipaddress.ip_network(
                        f"{address}/{address.max_prefixlen}", strict=False
                    )
            except ValueError as exc:
                raise ValueError(f"Invalid IP address or network: {entry}") from exc
            if display in normalized:
                continue
            normalized.append(display)
            networks.append(network)

        if enabled and not normalized:
            raise ValueError(
                "Add at least one IP address or network before enabling the whitelist"
            )

        with self._lock:
            self._enabled = enabled
            self._entries = normalized
            self._networks = networks

    def is_allowed(self, host: Optional[str]) -> bool:
        with self._lock:
            enabled = self._enabled
            networks = tuple(self._networks)
        if not enabled:
            return True
        if not host:
            return False
        try:
            ip = ipaddress.ip_address(host)
        except ValueError:
            return False
        return any(ip in network for network in networks)


API_WHITELIST = APIWhitelist()


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


def nav_context(active: Optional[str] = None) -> Dict[str, Any]:
    return {
        "nav_links": [
            {"href": href, "label": label} for href, label in NAV_LINKS
        ],
        "active_path": active,
    }


def fallback_top_bar_html(active: Optional[str] = None, *, indent: str = "      ") -> str:
    lines = ['<div class="top-bar">']
    lines.append('  <a class="brand" href="/" aria-label="FFAPI home"><span class="ff">ff</span><span class="api">api</span></a>')
    lines.append('  <nav class="main-nav">')
    for href, label in NAV_LINKS:
        class_attr = ' class="active"' if active == href else ""
        lines.append(f'    <a href="{href}"{class_attr}>{label}</a>')
    lines.append('  </nav>')
    lines.append('</div>')
    return "\n".join(f"{indent}{line}" for line in lines)


def login_template_response(
    request: Request,
    next_path: str,
    csrf_token: str,
    *,
    error: Optional[str] = None,
    status_code: int = 200,
):
    if templates is None:
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
        html_content = f"""
    <!doctype html>
    <html>
    <head>
      <meta charset=\"utf-8\" />
      <title>Settings Login</title>
      <style>
        body {{ font-family: system-ui, sans-serif; padding: 24px; max-width: 400px; margin: 0 auto; }}
        .brand {{ font-size: 32px; font-weight: bold; margin-bottom: 20px; text-align: center; text-decoration: none; display: inline-flex; gap: 2px; align-items: center; justify-content: center; color: #000; }}
        .brand:focus, .brand:hover {{ text-decoration: none; color: #000; }}
        .brand .ff {{ color: #28a745; }}
        .brand .api {{ color: #000; }}
        form {{ display: flex; flex-direction: column; gap: 12px; }}
        label {{ font-weight: 600; }}
        input {{ padding: 10px; font-size: 14px; border: 1px solid #ccc; border-radius: 4px; }}
        button {{ padding: 10px; background: #28a745; color: #fff; border: none; border-radius: 4px; font-size: 14px; cursor: pointer; }}
        button:hover {{ background: #1f7a34; }}
        .alert {{ padding: 10px; border-radius: 4px; font-size: 14px; margin-bottom: 16px; }}
        .alert.error {{ background: #fdecea; color: #b3261e; border: 1px solid #f7c6c4; }}
        .help {{ font-size: 13px; color: #555; text-align: center; margin-top: 12px; }}
        .help-text {{ font-size: 13px; color: #4b5563; margin-top: -4px; }}
      </style>
    </head>
    <body>
      <a class=\"brand\" href=\"/\" aria-label=\"FFAPI home\"><span class=\"ff\">ff</span><span class=\"api\">api</span></a>
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
        return HTMLResponse(html_content, status_code=status_code)

    context = {
        "request": request,
        "title": "Settings Login",
        "body_class": "login-page",
        "nav_links": [],
        "active_path": None,
        "csrf_field": csrf_hidden_input(csrf_token),
        "next_path": next_path,
        "error": error,
        "require_code": UI_AUTH.is_two_factor_enabled(),
        "default_username": DEFAULT_UI_USERNAME,
        "default_password": DEFAULT_UI_PASSWORD,
    }
    return templates.TemplateResponse(request, "login.html", context, status_code=status_code)


def settings_template_response(
    request: Request,
    storage: Dict[str, Any],
    message: Optional[str] = None,
    error: Optional[str] = None,
    *,
    authenticated: bool,
    csrf_token: str,
    backup_codes: Optional[List[str]] = None,
    backup_status: Optional[List[Dict[str, Any]]] = None,
    whitelist_text: Optional[str] = None,
    status_code: int = 200,
) -> Response:
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
        alert_blocks.append(
            """
            <div class="alert warning">
              <strong>⚠️ Save these codes now!</strong>
              They will be hidden if you refresh this page.
            </div>
            """
        )
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
    status_class = "enabled" if require_login else "disabled"
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
    api_status_class = "enabled" if api_require_key else "disabled"
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
    whitelist_entries_current = API_WHITELIST.get_entries()
    whitelist_enabled = API_WHITELIST.is_enabled()
    whitelist_entries_text = (
        whitelist_text if whitelist_text is not None else "\n".join(whitelist_entries_current)
    )
    if not api_require_key:
        if whitelist_enabled:
            whitelist_note = (
                "Enable API authentication to enforce the saved whitelist entries."
            )
        else:
            whitelist_note = "Enable API authentication to restrict API access by IP address."
    elif whitelist_enabled:
        whitelist_note = "Only the IP addresses below may call API endpoints."
    else:
        whitelist_note = "All client IPs can call API endpoints until a whitelist is enabled."
    whitelist_help = "Enter one IPv4/IPv6 address or CIDR range per line."
    whitelist_checkbox_state = "checked" if whitelist_enabled else ""
    whitelist_card_class = " is-disabled" if not api_require_key else ""
    whitelist_disabled_attr = " disabled" if not api_require_key else ""
    whitelist_status_text = "Enabled" if whitelist_enabled else "Disabled"
    whitelist_status_html = html.escape(whitelist_status_text)
    whitelist_status_class = "enabled" if whitelist_enabled else "disabled"
    whitelist_note_text = html.escape(whitelist_note)
    whitelist_help_text = html.escape(whitelist_help)
    whitelist_entries_html = html.escape(whitelist_entries_text)
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
    two_factor_status_class = "enabled" if two_factor_enabled else "disabled"
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

    top_bar_html = fallback_top_bar_html("/settings")
    html_content = f"""
    <!doctype html>
    <html>
    <head>
      <meta charset=\"utf-8\" />
      <title>Settings</title>
      <style>
        body {{ font-family: system-ui, sans-serif; padding: 24px; max-width: 1400px; margin: 0 auto; }}
        .top-bar {{ display: flex; align-items: center; justify-content: space-between; gap: 16px; flex-wrap: wrap; margin-bottom: 24px; }}
        .brand {{ font-size: 32px; font-weight: bold; margin: 0; text-decoration: none; display: inline-flex; align-items: center; gap: 2px; color: #000; }}
        .brand:focus, .brand:hover {{ text-decoration: none; color: #000; }}
        .brand .ff {{ color: #28a745; }}
        .brand .api {{ color: #000; }}
        .main-nav {{ margin: 0; margin-left: auto; display: flex; gap: 12px; flex-wrap: wrap; justify-content: flex-end; }}
        .main-nav a {{ padding: 8px 14px; border-radius: 6px; background: #e2e8f0; color: #6b7280; text-decoration: none; font-weight: 600; transition: background 0.15s ease, color 0.15s ease; }}
        .main-nav a:hover {{ background: #d1d5db; color: #000; }}
        .main-nav a.active {{ background: #28a745; color: #fff; }}
        h2 {{ margin-top: 32px; }}
        section {{ background: #f7f9fc; padding: 20px; border-radius: 8px; box-shadow: 0 1px 2px rgba(0,0,0,0.08); margin-bottom: 24px; }}
        form {{ margin-top: 12px; }}
        label {{ display: block; font-weight: 600; margin-bottom: 6px; }}
        input {{ padding: 10px; font-size: 14px; border: 1px solid #ccd5e0; border-radius: 4px; width: 100%; box-sizing: border-box; }}
        button {{ padding: 10px 16px; background: #28a745; color: #fff; border: none; border-radius: 4px; font-size: 14px; cursor: pointer; }}
        button:hover {{ background: #1f7a34; }}
        button.secondary {{ background: #9aa5b1; }}
        button.secondary:hover {{ background: #7c8794; }}
        .half-width {{ width: 50%; }}
        .api-auth-form button {{ margin-top: 20px; }}
        .checkbox-row {{ display: flex; align-items: center; gap: 10px; font-weight: 600; }}
        .checkbox-row input {{ width: auto; margin: 0; transform: scale(1.2); }}
        .settings-grid {{ display: grid; grid-template-columns: repeat(auto-fit, minmax(220px, 1fr)); gap: 16px; margin-top: 16px; }}
        .settings-row {{ display: grid; grid-template-columns: repeat(auto-fit, minmax(280px, 1fr)); gap: 24px; margin-bottom: 24px; }}
        .settings-row section {{ margin-bottom: 0; }}
        .alert {{ padding: 12px; border-radius: 4px; margin-bottom: 16px; font-size: 14px; }}
        .alert.success {{ background: #e8f5e9; color: #256029; border: 1px solid #c8e6c9; }}
        .alert.error {{ background: #fdecea; color: #b3261e; border: 1px solid #f7c6c4; }}
        .alert.warning {{ background: #fff7ed; color: #9a3412; border: 1px solid #fed7aa; }}
        .alert.info {{ background: #eff6ff; color: #1f7a34; border: 1px solid #bfdbfe; }}
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
        .whitelist-card {{ margin-top: 20px; background: #fff; border-radius: 8px; box-shadow: inset 0 0 0 1px #e1e7ef; padding: 16px; display: flex; flex-direction: column; gap: 12px; }}
        .whitelist-card.is-disabled {{ opacity: 0.55; }}
        .whitelist-card textarea {{ min-height: 120px; font-family: 'Courier New', monospace; resize: vertical; }}
        .whitelist-card button {{ width: fit-content; }}
        .whitelist-header {{ display: flex; align-items: center; justify-content: space-between; gap: 12px; flex-wrap: wrap; }}
        .credentials-grid label {{ margin-bottom: 0; }}
        .twofactor-card .status-pill.enabled {{ background: #e8f5e9; color: #256029; }}
        .secret-box {{ background: #f1f5f9; border-radius: 6px; padding: 12px; display: flex; flex-direction: column; gap: 4px; }}
        .secret-box span {{ font-size: 12px; color: #526079; text-transform: uppercase; letter-spacing: 0.1em; }}
        .secret-box code {{ font-size: 18px; letter-spacing: 2px; font-weight: 600; }}
        .twofactor-actions {{ display: flex; flex-direction: column; gap: 12px; margin-top: 4px; align-items: flex-start; }}
        .twofactor-actions form {{ margin: 0; display: flex; flex-direction: column; gap: 8px; align-items: flex-start; }}
        .twofactor-actions button {{ width: fit-content; }}
        .status-pill {{ display: inline-flex; align-items: center; gap: 6px; padding: 6px 10px; background: #e2e8f0; color: #1f2937; border-radius: 999px; font-weight: 600; width: fit-content; }}
        .status-pill.enabled {{ background: #e8f5e9; color: #256029; }}
        .status-pill.disabled {{ background: #fdecea; color: #b3261e; }}
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
        .backup-table .status {{ display: inline-flex; align-items: center; gap: 6px; padding: 4px 8px; border-radius: 999px; font-weight: 600; background: #e0f2fe; color: #1f7a34; }}
        .backup-table .status.used {{ background: #f8f0f0; color: #b91c1c; }}
        .form-grid {{ display: grid; grid-template-columns: repeat(auto-fit, minmax(200px, 1fr)); gap: 16px; }}
        .field-card {{ background: #f9fbff; padding: 12px; border-radius: 8px; box-shadow: inset 0 0 0 1px #d6e2f1; display: grid; gap: 8px; }}
        .retention-form button, .performance-form button {{ margin-top: 16px; }}
        .field-card label {{ margin-bottom: 0; display: flex; align-items: center; gap: 6px; }}
        .tooltip {{ position: relative; display: inline-flex; align-items: center; justify-content: center; width: 18px; height: 18px; border-radius: 50%; background: #e2e8f0; color: #1e293b; font-size: 12px; font-weight: 600; cursor: help; }}
        .retention-actions {{ display: flex; align-items: center; flex-wrap: wrap; gap: 12px; margin-top: 16px; }}
        .retention-actions .help-text {{ margin: 0; }}
        .tooltip:focus-visible {{ outline: 2px solid #2563eb; outline-offset: 2px; }}
        .tooltip .tooltiptext {{ visibility: hidden; opacity: 0; width: 220px; background: #1e293b; color: #f8fafc; text-align: left; border-radius: 6px; padding: 8px 10px; position: absolute; z-index: 10; bottom: 125%; left: 50%; transform: translateX(-50%); transition: opacity 0.2s ease-in-out; box-shadow: 0 8px 16px rgba(15, 23, 42, 0.25); }}
        .tooltip:hover .tooltiptext, .tooltip:focus .tooltiptext {{ visibility: visible; opacity: 1; }}
        #processingIndicator {{ display: none; position: fixed; inset: 0; background: rgba(15, 23, 42, 0.35); color: #f8fafc; font-size: 16px; font-weight: 600; align-items: center; justify-content: center; z-index: 9999; gap: 8px; }}
        #processingIndicator .spinner {{ width: 18px; height: 18px; border-radius: 50%; border: 2px solid rgba(248, 250, 252, 0.45); border-top-color: #f8fafc; animation: spin 0.8s linear infinite; }}
        @keyframes spin {{ to {{ transform: rotate(360deg); }} }}
      </style>
    </head>
    <body>
{top_bar_html}
      {alerts}

      <section>
        <h2>UI Authentication</h2>
        <div class="auth-row">
          <div class="auth-card toggle-card">
            <h3>Access control</h3>
        <span class="status-pill {status_class}">{status_text}</span>
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
                  <label for="password_confirm">Confirm new password</label>
                  <input id="password_confirm" name="password_confirm" type="password" placeholder="Re-enter the new password" required{disabled_attr} />
                </div>
                <button type="submit"{disabled_attr}>Update credentials</button>
              </form>
            </div>
          </div>
          <div class="auth-card twofactor-card{two_factor_card_class}">
            <h3>Two-factor authentication</h3>
            <span class="status-pill {two_factor_status_class}">{two_factor_status_text}</span>
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
        <span class="status-pill {api_status_class}">{api_status_html}</span>
        <p>{api_note_text}</p>
        <form method="post" action="/settings/api-auth" class="api-auth-form">
          {csrf_field}
          <label class="checkbox-row" for="require_api_key_settings">
            <input type="checkbox" id="require_api_key_settings" name="require_api_key" value="true" {api_checkbox_state} />
            <span>Require API key for API requests</span>
          </label>
          <button type="submit">Save API authentication</button>
          <p class="help-text">{api_help_text}</p>
        </form>
        <div class="whitelist-card{whitelist_card_class}">
          <div class="whitelist-header">
            <h3>API IP whitelist</h3>
            <span class="status-pill {whitelist_status_class}">{whitelist_status_html}</span>
          </div>
          <p>{whitelist_note_text}</p>
          <form method="post" action="/settings/api-whitelist" class="whitelist-form">
            {csrf_field}
            <label class="checkbox-row" for="enable_api_whitelist">
              <input type="checkbox" id="enable_api_whitelist" name="enable_whitelist" value="true" {whitelist_checkbox_state}{whitelist_disabled_attr} />
              <span>Restrict API access to specific IPs</span>
            </label>
            <label for="whitelist_entries">Allowed IPs or CIDR ranges</label>
            <textarea id="whitelist_entries" name="whitelist_entries" rows="5"{whitelist_disabled_attr}>{whitelist_entries_html}</textarea>
            <span class="help-text">{whitelist_help_text}</span>
            <button type="submit"{whitelist_disabled_attr}>Save API whitelist</button>
          </form>
        </div>
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
              class="half-width"
            />
            <div class="retention-actions">
              <button type="submit">Update retention</button>
              <span class="help-text">Equivalent to {retention_days_text} day(s).</span>
            </div>
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
                  class="half-width"
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
                  class="half-width"
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
                  class="half-width"
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
                  class="half-width"
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

    if templates is None:
        return HTMLResponse(html_content, status_code=status_code)

    alert_items: List[Dict[str, Any]] = []
    if message:
        alert_items.append({"type": "success", "text": message})
    if error:
        alert_items.append({"type": "error", "text": error})
    if backup_codes:
        alert_items.append({"type": "info", "backup_codes": backup_codes})

    backup_rows_context = []
    for idx, item in enumerate(backup_status, start=1):
        last_four = item.get("last_four", "????")
        masked = f"\u2022\u2022\u2022\u2022-{last_four}"
        backup_rows_context.append(
            {
                "index": idx,
                "masked": masked,
                "status_text": "Used" if item.get("used") else "Unused",
                "status_class": "used" if item.get("used") else "unused",
            }
        )

    context = {
        "request": request,
        "title": "Settings",
        "body_class": "settings-page",
        "max_width": "1400px",
        **nav_context("/settings"),
        "alerts": alert_items,
        "csrf_field": csrf_field,
        "require_login": require_login,
        "status_text": status_text,
        "status_class": status_class,
        "auth_note": auth_note,
        "checkbox_checked": require_login,
        "logout_allowed": bool(require_login and authenticated),
        "ui_username": UI_AUTH.username,
        "ui_disabled": not require_login,
        "two_factor": {
            "enabled": two_factor_enabled,
            "status_text": two_factor_status_text,
            "status_class": "enabled" if two_factor_enabled else "disabled",
            "note": two_factor_note,
            "secret": grouped_secret,
            "otpauth_uri": otpauth_uri_value,
            "qr_data_uri": qr_data_uri,
            "disabled": not require_login,
            "backup_card_disabled": not two_factor_enabled,
            "download_enabled": bool(require_login and backup_status),
            "generate_enabled": bool(require_login and two_factor_enabled),
            "backup_rows": backup_rows_context,
        },
        "storage": {
            "total_used_text": f"{format_number(total_used_mb)} MB",
            "active_files_text": str(active_files),
            "expired_files_text": str(expired_files),
            "quota_text": quota_display,
            "retention_days_text": retention_days_display,
        },
        "retention": {
            "hours_value": retention_hours_display,
            "days_text": retention_days_display,
        },
        "performance": {
            "rate_limit": rate_limit_rpm,
            "ffmpeg_timeout": ffmpeg_timeout_display,
            "upload_chunk": upload_chunk_display,
            "max_file_size": MAX_FILE_SIZE_MB,
        },
        "api_auth": {
            "enabled": api_require_key,
            "status_text": api_status_text,
            "status_class": api_status_class,
            "note": api_note,
            "help_text": "Clients must provide X-API-Key header or api_key query when enabled.",
        },
        "api_whitelist": {
            "enabled": whitelist_enabled,
            "status_text": whitelist_status_text,
            "status_class": whitelist_status_class,
            "note": whitelist_note,
            "help_text": whitelist_help,
            "entries_text": whitelist_entries_text,
            "disabled": not api_require_key,
        },
    }

    return templates.TemplateResponse(request, "settings.html", context, status_code=status_code)


def api_keys_template_response(
    request: Request,
    *,
    keys: List[Dict[str, Any]],
    require_key: bool,
    csrf_token: str,
    message: Optional[str] = None,
    error: Optional[str] = None,
    new_key: Optional[str] = None,
    status_code: int = 200,
) -> Response:
    csrf_field = csrf_hidden_input(csrf_token)

    def format_timestamp(epoch: Optional[float]) -> str:
        if not epoch:
            return "Never"
        dt = datetime.fromtimestamp(epoch, tz=timezone.utc)
        return dt.strftime("%Y-%m-%d %H:%M:%S UTC")

    key_rows: List[Dict[str, str]] = []
    for item in keys:
        key_rows.append(
            {
                "prefix": str(item.get("prefix", "")),
                "created": format_timestamp(item.get("created")),
                "last_used": format_timestamp(item.get("last_used")),
                "id": str(item.get("id", "")),
            }
        )

    status_text = "Enabled" if require_key else "Disabled"
    status_class = "enabled" if require_key else "disabled"
    note_text = (
        "API endpoints currently require a valid key in the X-API-Key header or api_key parameter."
        if require_key
        else "API endpoints are open; enable authentication to restrict access."
    )

    if templates is None:
        alert_blocks: List[str] = []
        if message:
            alert_blocks.append(
                f"<div class=\"alert success\">{html.escape(message)}</div>"
            )
        if error:
            alert_blocks.append(
                f"<div class=\"alert error\">{html.escape(error)}</div>"
            )
        if new_key:
            alert_blocks.append(
                """
                <div class="alert success">
                  <strong>New API key generated</strong>
                  <p>Copy this value now — it will not be shown again.</p>
                  <code>{key}</code>
                </div>
                """.format(key=html.escape(new_key))
            )
        alerts_html = "".join(alert_blocks)

        disabled_class = "" if require_key else " is-disabled"
        disabled_attr = "" if require_key else " disabled"

        rows_html: List[str] = []
        for row in key_rows:
            rows_html.append(
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
                    display=html.escape(row["prefix"]),
                    created=html.escape(row["created"]),
                    last_used=html.escape(row["last_used"]),
                    key_id=html.escape(row["id"], quote=True),
                    disabled=disabled_attr,
                    csrf_field=csrf_field,
                )
            )

        if not rows_html:
            rows_html.append(
                """
                <tr>
                  <td colspan="4">No API keys yet</td>
                </tr>
                """
            )

        settings_notice = ""
        if not require_key:
            settings_notice = (
                "<p class=\"help-text\">Enable API authentication in <a href=\"/settings\">Settings</a> before generating keys.</p>"
            )

        top_bar_html = fallback_top_bar_html("/api-keys")
        html_content = f"""
    <!doctype html>
    <html>
    <head>
      <meta charset=\"utf-8\" />
      <title>API Keys</title>
      <style>
        body {{ font-family: system-ui, sans-serif; padding: 24px; max-width: 1400px; margin: 0 auto; }}
        .top-bar {{ display: flex; align-items: center; justify-content: space-between; gap: 16px; flex-wrap: wrap; margin-bottom: 24px; }}
        .brand {{ font-size: 32px; font-weight: bold; margin: 0; text-decoration: none; display: inline-flex; align-items: center; gap: 2px; color: #000; }}
        .brand:focus, .brand:hover {{ text-decoration: none; color: #000; }}
        .brand .ff {{ color: #28a745; }}
        .brand .api {{ color: #000; }}
        .main-nav {{ margin: 0; margin-left: auto; display: flex; gap: 12px; flex-wrap: wrap; justify-content: flex-end; }}
        .main-nav a {{ padding: 8px 14px; border-radius: 6px; background: #e2e8f0; color: #6b7280; text-decoration: none; font-weight: 600; transition: background 0.15s ease, color 0.15s ease; }}
        .main-nav a:hover {{ background: #d1d5db; color: #000; }}
        .main-nav a.active {{ background: #28a745; color: #fff; }}
        h2 {{ margin-top: 32px; }}
        section {{ background: #f7f9fc; padding: 20px; border-radius: 8px; box-shadow: 0 1px 2px rgba(0,0,0,0.08); margin-bottom: 24px; }}
        form {{ margin-top: 12px; }}
        label {{ display: block; font-weight: 600; margin-bottom: 6px; }}
        input {{ padding: 10px; font-size: 14px; border: 1px solid #ccd5e0; border-radius: 4px; width: 100%; box-sizing: border-box; }}
        button {{ padding: 10px 16px; background: #28a745; color: #fff; border: none; border-radius: 4px; font-size: 14px; cursor: pointer; }}
        button:hover {{ background: #1f7a34; }}
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
        .status-pill {{ display: inline-flex; align-items: center; gap: 6px; padding: 6px 10px; background: #e2e8f0; color: #1f2937; border-radius: 999px; font-weight: 600; width: fit-content; }}
        .status-pill.enabled {{ background: #e8f5e9; color: #256029; }}
        .status-pill.disabled {{ background: #fdecea; color: #b3261e; }}
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
{top_bar_html}
      {alerts_html}

      <section>
        <h2>API Keys</h2>
        <div class="api-card keys-card{disabled_class}">
          <div class="api-card-header">
            <h3>Authentication status</h3>
            <span class="status-pill {status_class}">{html.escape(status_text)}</span>
          </div>
          <p>{html.escape(note_text)}</p>
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
              {''.join(rows_html)}
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
        return HTMLResponse(html_content, status_code=status_code)

    context = {
        "request": request,
        "title": "API Keys",
        "body_class": "api-keys-page",
        **nav_context("/api-keys"),
        "message": message,
        "error": error,
        "new_key": new_key,
        "require_key": require_key,
        "status_text": status_text,
        "status_class": status_class,
        "note_text": note_text,
        "keys": key_rows,
        "csrf_field": csrf_field,
    }
    return templates.TemplateResponse(
        request, "api_keys.html", context, status_code=status_code
    )


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
            if status in {"finished", "failed", "killed"} and created < cutoff:
                to_remove.append(job_id)
            elif status == "processing" and created < (cutoff - 86400):
                logger.warning("Removing ancient stuck job %s", job_id)
                to_remove.append(job_id)
        for job_id in to_remove:
            JOBS.pop(job_id, None)
            with JOB_PROGRESS_LOCK:
                JOB_PROGRESS.pop(job_id, None)

        if max_total_jobs > 0 and len(JOBS) > max_total_jobs:
            finished_jobs = sorted(
                (
                    (job_id, data.get("created", 0.0))
                    for job_id, data in JOBS.items()
                    if data.get("status") in {"finished", "failed", "killed"}
                ),
                key=lambda item: item[1] or 0.0,
            )
            excess = len(JOBS) - max_total_jobs
            for job_id, _ in finished_jobs:
                if excess <= 0:
                    break
                JOBS.pop(job_id, None)
                with JOB_PROGRESS_LOCK:
                    JOB_PROGRESS.pop(job_id, None)
                excess -= 1


def _get_progress_snapshot(job_id: str) -> Optional[Dict[str, Any]]:
    with JOB_PROGRESS_LOCK:
        snapshot = JOB_PROGRESS.get(job_id)
        if snapshot is None:
            return None
        history = list(snapshot.get("history", []))
        return {**snapshot, "history": history}


def _drain_job_progress(job_id: str) -> Optional[Dict[str, Any]]:
    with JOB_PROGRESS_LOCK:
        snapshot = JOB_PROGRESS.pop(job_id, None)
        if snapshot is None:
            return None
        history = list(snapshot.get("history", []))
        return {**snapshot, "history": history}


def _merge_job_progress(job_id: str, data: Dict[str, Any]) -> Dict[str, Any]:
    """Best-effort merge of cached progress data into a job record."""

    with JOB_PROGRESS_LOCK:
        snapshot = JOB_PROGRESS.get(job_id)
        if snapshot is None:
            return dict(data)

        merged = dict(data)
        progress = snapshot.get("progress")
        message = snapshot.get("message")
        detail = snapshot.get("detail")
        ffmpeg_stats = snapshot.get("ffmpeg_stats")
        updated_ts = snapshot.get("updated")
        history = list(merged.get("history", []))
        history.extend(snapshot.get("history", []))
        if len(history) > JOB_HISTORY_LIMIT:
            history = history[-JOB_HISTORY_LIMIT:]

        if progress is not None:
            merged["progress"] = progress
        if message is not None:
            merged["message"] = message
        if detail is not None:
            merged["detail"] = detail
        if ffmpeg_stats is not None:
            merged["ffmpeg_stats"] = ffmpeg_stats
        if updated_ts is not None:
            merged["updated"] = updated_ts
        merged["history"] = history
        return merged


def _finalize_sync_job_failure(
    job_id: str,
    start_time: float,
    error: Any,
    log_path: Optional[str],
    *,
    failure_message: str,
) -> None:
    duration_ms = (time.time() - start_time) * 1000
    progress_snapshot = _drain_job_progress(job_id)
    history_updates: List[Dict[str, Any]] = []
    if progress_snapshot is not None:
        history_updates.extend(progress_snapshot.get("history", []))

    now_ts = time.time()
    history_updates.append(
        {
            "timestamp": now_ts,
            "progress": 100,
            "message": failure_message,
        }
    )

    with JOBS_LOCK:
        existing = JOBS.get(job_id, {})
        history = list(existing.get("history", []))

    history.extend(history_updates)
    if len(history) > JOB_HISTORY_LIMIT:
        history = history[-JOB_HISTORY_LIMIT:]

    update_payload: Dict[str, Any] = {
        "status": "failed",
        "progress": 100,
        "message": failure_message,
        "error": error,
        "updated": now_ts,
        "duration_ms": duration_ms,
        "history": history,
        "result": None,
    }
    if log_path is not None:
        update_payload["log_path"] = log_path

    with JobTransaction(job_id) as tx:
        tx.set(**update_payload)


class JobProgressReporter:
    def __init__(self, job_id: str) -> None:
        self.job_id = job_id

    def update(
        self,
        percent: float,
        message: str,
        detail: Optional[str] = None,
        ffmpeg_stats: Optional[dict] = None,
    ) -> None:
        clamped = max(0, min(100, int(percent)))
        now = time.time()
        with JOBS_LOCK:
            status = JOBS.get(self.job_id, {}).get("status")
        if status not in {"queued", "processing"}:
            return

        history_entry: Dict[str, Any] = {
            "timestamp": now,
            "progress": clamped,
            "message": message,
        }
        if ffmpeg_stats:
            history_entry["ffmpeg"] = ffmpeg_stats

        with JOB_PROGRESS_LOCK:
            snapshot = dict(JOB_PROGRESS.get(self.job_id, {}))
            history = list(snapshot.get("history", []))
            history.append(history_entry)
            if len(history) > JOB_HISTORY_LIMIT:
                history = history[-JOB_HISTORY_LIMIT:]

            updated = dict(snapshot)
            updated["progress"] = clamped
            updated["message"] = message
            updated["updated"] = now
            updated["history"] = history
            if detail is not None:
                updated["detail"] = detail
            if ffmpeg_stats is not None:
                updated["ffmpeg_stats"] = ffmpeg_stats

            JOB_PROGRESS[self.job_id] = updated


class FFmpegProgressParser:
    _TIME_PATTERN = re.compile(r"time=(\d+):(\d+):(\d+(?:\.\d+)?)")
    _FPS_PATTERN = re.compile(r"fps=\s*(\d+\.?\d*)")
    _SPEED_PATTERN = re.compile(r"speed=\s*(\d+\.?\d*)x")
    _BITRATE_PATTERN = re.compile(r"bitrate=\s*(\d+\.?\d*)\s*(\w+/?\w*)")
    _FRAME_PATTERN = re.compile(r"frame=\s*(\d+)")
    _SIZE_PATTERN = re.compile(r"size=\s*(\d+\w+)")

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

        metrics = self._parse_metrics(line, elapsed)

        detail_parts = [f"time={elapsed:.1f}s"]
        if metrics.get("fps"):
            detail_parts.append(f"fps={metrics['fps']}")
        if metrics.get("speed"):
            detail_parts.append(f"speed={metrics['speed']}x")
        if metrics.get("frame"):
            detail_parts.append(f"frame={metrics['frame']}")

        detail = " | ".join(detail_parts)

        self._reporter.update(
            percent,
            self._stage_message,
            detail=detail,
            ffmpeg_stats=metrics,
        )

    def _parse_metrics(self, line: str, elapsed: float) -> dict:
        """Extract all available FFmpeg metrics from output line"""
        metrics = {"time": f"{elapsed:.1f}"}

        fps_match = self._FPS_PATTERN.search(line)
        if fps_match:
            metrics["fps"] = fps_match.group(1)

        speed_match = self._SPEED_PATTERN.search(line)
        if speed_match:
            metrics["speed"] = speed_match.group(1)

        bitrate_match = self._BITRATE_PATTERN.search(line)
        if bitrate_match:
            metrics["bitrate"] = f"{bitrate_match.group(1)}{bitrate_match.group(2)}"

        frame_match = self._FRAME_PATTERN.search(line)
        if frame_match:
            metrics["frame"] = frame_match.group(1)

        size_match = self._SIZE_PATTERN.search(line)
        if size_match:
            metrics["size"] = size_match.group(1)

        return metrics


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
        client_host = request.client.host if request.client else None
        if not API_WHITELIST.is_allowed(client_host):
            METRICS.request_finished(path, 403, 0.0, 0.0, "ip_not_whitelisted")
            response = JSONResponse(
                {
                    "error": "ip_not_whitelisted",
                    "detail": "Client IP address is not permitted",
                },
                status_code=403,
            )
            response.headers["X-Request-ID"] = request_id
            return response
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
        if parsed:
            form_items = [(key, value) for key, values in parsed.items() for value in values]
            request._form = FormData(form_items)
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
    # Resolve base path first to handle symlinks
    base = base.resolve()
    try:
        target = (base / rel).resolve()
    except Exception as exc:
        logger.warning("Invalid path provided for %s: %s", base, rel)
        _flush_logs()
        raise HTTPException(status_code=400, detail="Invalid path") from exc

    # Use relative_to() which raises ValueError if target is not under base
    # This is more secure than checking parents as it handles all edge cases
    try:
        target.relative_to(base)
    except ValueError:
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
    temp_dest = dest.with_name(dest.name + ".partial")
    space_check_interval = 64 * 1024 * 1024
    last_space_check = 0
    try:
        with temp_dest.open("wb") as buffer:
            while True:
                chunk = await upload.read(UPLOAD_CHUNK_SIZE)
                if not chunk:
                    break
                chunk_len = len(chunk)
                if total + chunk_len > MAX_FILE_SIZE_BYTES:
                    logger.warning("Upload exceeded max size: %s", upload.filename)
                    _flush_logs()
                    raise HTTPException(status_code=413, detail="File too large")
                buffer.write(chunk)
                total += chunk_len
                if total - last_space_check >= space_check_interval:
                    try:
                        check_disk_space(temp_dest.parent)
                    except HTTPException:
                        raise
                    last_space_check = total
    except HTTPException:
        if temp_dest.exists():
            try:
                temp_dest.unlink()
            except Exception as exc:
                logger.warning("Failed cleaning partial upload %s: %s", temp_dest, exc)
        raise
    except Exception as exc:
        if temp_dest.exists():
            try:
                temp_dest.unlink()
            except Exception as cleanup_exc:
                logger.warning("Failed removing incomplete upload %s: %s", temp_dest, cleanup_exc)
        logger.error("Failed to persist upload %s: %s", upload.filename, exc)
        _flush_logs()
        raise HTTPException(status_code=500, detail="Failed to save upload") from exc
    else:
        try:
            if dest.exists():
                dest.unlink()
            temp_dest.replace(dest)
        except Exception as exc:
            if temp_dest.exists():
                try:
                    temp_dest.unlink()
                except Exception:
                    pass
            logger.error("Failed to finalize upload %s: %s", upload.filename, exc)
            _flush_logs()
            raise HTTPException(status_code=500, detail="Failed to finalize upload") from exc
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


def format_file_size(num_bytes: int) -> str:
    units = ["B", "KB", "MB", "GB", "TB"]
    value = float(num_bytes)
    for unit in units:
        if value < 1024.0 or unit == units[-1]:
            return f"{value:.2f} {unit}"
        value /= 1024.0
    return f"{num_bytes} B"


def format_duration(seconds: float) -> str:
    return f"{seconds:.2f}s"


GPU_FILTER_ERROR_PATTERNS = [
    "Impossible to convert between the formats",
    "hwupload",
    "scale_cuda",
    "scale_qsv",
    "Cannot initialize the decoder",
]


async def run_ffmpeg_with_timeout(
    cmd: List[str],
    log_handle,
    *,
    progress_parser: Optional[Callable[[str], None]] = None,
    allow_gpu_fallback: bool = True,
    process_handle: Optional[JobProcessHandle] = None,
) -> int:
    """Run FFmpeg asynchronously without blocking the event loop."""

    def _read_log_text() -> str:
        """Best-effort attempt to read the collected log output."""

        if not allow_gpu_fallback:
            return ""

        try:
            if hasattr(log_handle, "flush"):
                log_handle.flush()
        except Exception:
            pass

        if hasattr(log_handle, "getvalue"):
            try:
                return str(log_handle.getvalue())
            except Exception:
                return ""

        if hasattr(log_handle, "read"):
            try:
                position = log_handle.tell()
                try:
                    log_handle.seek(0)
                except Exception:
                    return ""
                data = log_handle.read()
                try:
                    log_handle.seek(position)
                except Exception:
                    pass
                return data if isinstance(data, str) else str(data)
            except Exception:
                pass

        log_path = getattr(log_handle, "name", None)
        if log_path:
            try:
                path_obj = Path(log_path)
            except Exception:
                path_obj = None
            if path_obj and path_obj.exists():
                try:
                    return path_obj.read_text(encoding="utf-8", errors="ignore")
                except Exception:
                    return ""

        return ""

    def _interpret_return_code(return_code: int) -> int:
        if not allow_gpu_fallback or return_code == 0:
            return return_code

        try:
            log_text = _read_log_text()
        except Exception:
            log_text = ""

        if return_code != 0 and not log_text.strip():
            try:
                gpu_enabled = bool(get_gpu_config().get("enabled"))
            except Exception:
                gpu_enabled = False
            if gpu_enabled:
                logger.warning(
                    "FFmpeg failed with no output (possible GPU crash), will retry with CPU"
                )
                return -999

        if log_text and any(pattern in log_text for pattern in GPU_FILTER_ERROR_PATTERNS):
            logger.warning("GPU filter error detected, will retry with CPU filters")
            return -999

        return return_code

    async def _pump_stream(stream: Optional[asyncio.StreamReader], *, parse_progress: bool) -> None:
        if stream is None:
            return
        try:
            buffer = ""
            while True:
                # Read small chunks to capture FFmpeg's \r-terminated stats
                chunk = await stream.read(1024)
                if not chunk:
                    break

                text = chunk.decode("utf-8", errors="ignore")
                buffer += text

                # Split on both \r and \n to capture FFmpeg stats
                lines = buffer.split('\r')
                buffer = lines[-1]  # Keep incomplete line in buffer

                for line in lines[:-1]:
                    # Further split on \n for regular output
                    sublines = line.split('\n')
                    for subline in sublines:
                        if not subline:
                            continue

                        full_line = subline + '\n'

                        try:
                            if hasattr(log_handle, "write"):
                                log_handle.write(full_line)
                                if hasattr(log_handle, "flush"):
                                    log_handle.flush()
                        except Exception:
                            pass

                        if parse_progress and progress_parser is not None:
                            try:
                                progress_parser(subline)
                            except Exception as exc:
                                logger.debug(
                                    "Progress parser failed on line: %s - %s",
                                    subline[:100],
                                    exc,
                                )

            # Process any remaining buffer
            if buffer and parse_progress and progress_parser is not None:
                try:
                    progress_parser(buffer)
                except Exception:
                    pass

        except asyncio.CancelledError:
            raise
        except Exception:
            pass

    async def _run_with_asyncio_process() -> int:
        stdout_task = asyncio.create_task(_pump_stream(proc.stdout, parse_progress=False))
        stderr_task = asyncio.create_task(_pump_stream(proc.stderr, parse_progress=True))
        pump_tasks = [t for t in (stdout_task, stderr_task) if t is not None]

        try:
            return_code = await asyncio.wait_for(proc.wait(), timeout=FFMPEG_TIMEOUT_SECONDS)
            if pump_tasks:
                try:
                    await asyncio.wait_for(asyncio.gather(*pump_tasks), timeout=5)
                except asyncio.TimeoutError:
                    for task in pump_tasks:
                        task.cancel()
                    for task in pump_tasks:
                        try:
                            await task
                        except (asyncio.CancelledError, Exception):
                            pass
            return _interpret_return_code(return_code)
        except asyncio.TimeoutError:
            logger.error(
                "FFmpeg process timed out after %d seconds for command: %s",
                FFMPEG_TIMEOUT_SECONDS,
                " ".join(cmd[:10]),
            )
            proc.terminate()
            try:
                await asyncio.wait_for(proc.wait(), timeout=5)
            except asyncio.TimeoutError:
                proc.kill()
                try:
                    await asyncio.wait_for(proc.wait(), timeout=1)
                except asyncio.TimeoutError:
                    pass
            for task in pump_tasks:
                task.cancel()
            for task in pump_tasks:
                try:
                    await task
                except (asyncio.CancelledError, Exception):
                    pass
            logger.warning("FFmpeg timed out after %s seconds: %s", FFMPEG_TIMEOUT_SECONDS, cmd)
            _flush_logs()
            raise HTTPException(status_code=504, detail="Processing timeout")
        except Exception:
            for task in pump_tasks:
                task.cancel()
            for task in pump_tasks:
                try:
                    await task
                except (asyncio.CancelledError, Exception):
                    pass
            raise

    async def _run_with_subprocess_fallback(
        launch_error: Optional[Exception] = None,
    ) -> int:
        try:
            popen_kwargs = dict(
                stdin=subprocess.DEVNULL,
                stdout=log_handle if hasattr(log_handle, "write") else subprocess.PIPE,
                stderr=subprocess.PIPE,
                text=True,
                encoding="utf-8",
                errors="ignore",
                bufsize=1,
            )
            if os.name != "nt":
                popen_kwargs["preexec_fn"] = _child_setup

            proc_sync = await asyncio.to_thread(
                subprocess.Popen,
                cmd,
                **popen_kwargs,
            )
            if process_handle is not None:
                process_handle.process = proc_sync
        except Exception as exc:  # pragma: no cover - defensive fallback
            logger.error("Failed to launch ffmpeg command %s: %s", cmd, exc)
            _flush_logs()
            if launch_error is not None:
                raise HTTPException(status_code=500, detail="Failed to start ffmpeg") from launch_error
            raise HTTPException(status_code=500, detail="Failed to start ffmpeg") from exc

        try:
            reader_thread: Optional[threading.Thread] = None
            if proc_sync.stderr is not None:

                def _reader() -> None:
                    try:
                        for line in proc_sync.stderr:
                            try:
                                if hasattr(log_handle, "write"):
                                    log_handle.write(line)
                                    if hasattr(log_handle, "flush"):
                                        log_handle.flush()
                            except Exception:
                                pass
                            if progress_parser is not None:
                                try:
                                    progress_parser(line)
                                except Exception as exc:
                                    logger.debug(
                                        "Progress parser failed on line: %s - %s",
                                        line[:100],
                                        exc,
                                    )
                    finally:
                        try:
                            proc_sync.stderr.close()
                        except Exception:
                            pass

                reader_thread = threading.Thread(target=_reader, daemon=True)
                reader_thread.start()

            loop = asyncio.get_running_loop()

            try:
                return_code = await loop.run_in_executor(
                    None,
                    lambda: proc_sync.wait(timeout=FFMPEG_TIMEOUT_SECONDS),
                )
                return _interpret_return_code(return_code)
            except subprocess.TimeoutExpired:
                logger.error(
                    "FFmpeg process timed out after %d seconds for command: %s",
                    FFMPEG_TIMEOUT_SECONDS,
                    " ".join(cmd[:10]),
                )
                proc_sync.terminate()
                try:
                    await asyncio.wait_for(
                        loop.run_in_executor(None, lambda: proc_sync.wait(timeout=5)),
                        timeout=5,
                    )
                except (asyncio.TimeoutError, subprocess.TimeoutExpired):
                    proc_sync.kill()
                    try:
                        await asyncio.wait_for(
                            loop.run_in_executor(None, lambda: proc_sync.wait(timeout=1)),
                            timeout=1,
                        )
                    except (asyncio.TimeoutError, subprocess.TimeoutExpired):
                        pass
                logger.warning("FFmpeg timed out after %s seconds: %s", FFMPEG_TIMEOUT_SECONDS, cmd)
                _flush_logs()
                raise HTTPException(status_code=504, detail="Processing timeout")
            finally:
                if reader_thread is not None:
                    reader_thread.join(timeout=1)
        finally:
            if process_handle is not None:
                process_handle.process = None

        return _interpret_return_code(0)

    def _child_setup() -> None:
        signal.signal(signal.SIGTERM, signal.SIG_DFL)

    subprocess_kwargs: Dict[str, Any] = {
        "stdin": asyncio.subprocess.DEVNULL,
        "stdout": asyncio.subprocess.PIPE,
        "stderr": asyncio.subprocess.PIPE,
    }
    if os.name != "nt":
        subprocess_kwargs["preexec_fn"] = _child_setup

    try:
        proc = await asyncio.create_subprocess_exec(
            *cmd,
            **subprocess_kwargs,
        )
    except AttributeError as attr_error:
        return _interpret_return_code(await _run_with_subprocess_fallback(attr_error))
    except Exception as exc:
        logger.error("Failed to launch ffmpeg command %s: %s", cmd, exc)
        _flush_logs()
        raise HTTPException(status_code=500, detail="Failed to start ffmpeg") from exc
    else:
        if process_handle is not None:
            process_handle.process = proc
        try:
            return await _run_with_asyncio_process()
        finally:
            if process_handle is not None:
                process_handle.process = None

    return await _run_with_asyncio_process()

async def generate_video_thumbnail(video_path: Path) -> Optional[Path]:
    suffix = video_path.suffix.lower()
    if suffix not in VIDEO_THUMBNAIL_EXTENSIONS:
        return None

    thumb_dir = video_path.parent / THUMBNAIL_DIR_NAME
    try:
        thumb_dir.mkdir(parents=True, exist_ok=True)
    except FileNotFoundError as exc:
        logger.debug("Parent directory missing for thumbnail %s: %s", video_path, exc)
        return None
    except Exception as exc:
        logger.debug("Unable to create thumbnail directory %s: %s", thumb_dir, exc)
        return None

    thumb_path = thumb_dir / f"{video_path.stem}.jpg"
    temp_log = WORK_DIR / f"thumb_{video_path.stem}.log"

    logger.info("Starting thumbnail generation for %s", video_path.name)

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

    code: Optional[int] = None
    try:
        with temp_log.open("w", encoding="utf-8", errors="ignore") as log_handle:
            code = await run_ffmpeg_with_timeout(
                cmd,
                log_handle,
                allow_gpu_fallback=False,
            )
        logger.info("Thumbnail generation completed with code %d", code)
    except asyncio.TimeoutError:
        logger.warning("Thumbnail generation timed out for %s", video_path)
        if thumb_path.exists():
            try:
                thumb_path.unlink()
            except Exception:
                pass
        return None
    except HTTPException:
        logger.warning("Thumbnail generation failed for %s", video_path)
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

    logger.info("Thumbnail saved to %s", thumb_path)
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


async def _periodic_stuck_job_cleanup() -> None:
    """Kill jobs stuck in processing for too long."""

    while True:
        await asyncio.sleep(3600)
        now = time.time()
        cutoff = now - (2 * FFMPEG_TIMEOUT_SECONDS)
        candidates: List[Tuple[str, Dict[str, Any]]] = []

        with JOBS_LOCK:
            for job_id, data in JOBS.items():
                if data.get("status") != "processing":
                    continue
                started = data.get("started")
                if started is None:
                    started = data.get("created", now)
                if started is None or started >= cutoff:
                    continue
                candidates.append((job_id, dict(data)))

        if not candidates:
            continue

        struct_logger.error(
            "stuck_jobs_cleanup_detected",
            count=len(candidates),
            job_ids=[job_id for job_id, _ in candidates],
        )

        for job_id, data in candidates:
            with JOB_PROCESSES_LOCK:
                handle = JOB_PROCESSES.get(job_id)
            process = getattr(handle, "process", None) if handle is not None else None

            if process is not None:
                try:
                    if isinstance(process, asyncio.subprocess.Process):
                        if process.returncode is None:
                            process.terminate()
                            try:
                                await asyncio.wait_for(process.wait(), timeout=5)
                            except asyncio.TimeoutError:
                                process.kill()
                                try:
                                    await asyncio.wait_for(process.wait(), timeout=1)
                                except asyncio.TimeoutError:
                                    pass
                    else:
                        if getattr(process, "poll", lambda: None)() is None:
                            process.terminate()
                            try:
                                await asyncio.wait_for(
                                    asyncio.to_thread(process.wait, timeout=5),
                                    timeout=5,
                                )
                            except (asyncio.TimeoutError, subprocess.TimeoutExpired):
                                process.kill()
                except Exception as exc:  # pragma: no cover - defensive logging
                    logger.warning("Failed to terminate stuck job %s: %s", job_id, exc)

            with JOB_PROCESSES_LOCK:
                JOB_PROCESSES.pop(job_id, None)

            progress_snapshot = _drain_job_progress(job_id)
            history = list(data.get("history", []))
            if progress_snapshot is not None:
                history.extend(progress_snapshot.get("history", []))

            now_mark = time.time()
            history.append(
                {
                    "timestamp": now_mark,
                    "progress": data.get("progress", 0),
                    "message": "Job exceeded maximum processing time",
                }
            )
            if len(history) > JOB_HISTORY_LIMIT:
                history = history[-JOB_HISTORY_LIMIT:]

            updates: Dict[str, Any] = {
                "status": "failed",
                "message": "Job exceeded maximum processing time",
                "error": "Job exceeded maximum processing time",
                "updated": now_mark,
                "history": history,
            }
            if progress_snapshot is not None:
                if "progress" in progress_snapshot:
                    updates.setdefault("progress", progress_snapshot["progress"])
                if "detail" in progress_snapshot:
                    updates["detail"] = progress_snapshot["detail"]
                if "ffmpeg_stats" in progress_snapshot:
                    updates["ffmpeg_stats"] = progress_snapshot["ffmpeg_stats"]

            with JobTransaction(job_id) as txn:
                txn.set(**updates)


async def _periodic_cache_cleanup():
    try:
        await asyncio.sleep(1800)
    except asyncio.CancelledError:
        raise
    while True:
        try:
            with PROBE_CACHE_LOCK:
                removed = PROBE_CACHE.cleanup_expired()
            if removed:
                logger.info("Cleaned %d expired probe cache entries", removed)
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
            LOGIN_RATE_LIMITER.cleanup_old_identifiers()
        except asyncio.CancelledError:
            raise
        except Exception as exc:
            logger.warning("Rate limiter cleanup failed: %s", exc)
        try:
            await asyncio.sleep(300)
        except asyncio.CancelledError:
            raise


async def publish_file(src: Path, ext: str, *, duration_ms: Optional[int] = None) -> Dict[str, str]:
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
    try:
        await asyncio.wait_for(
            asyncio.to_thread(shutil.move, str(src), str(dst)),
            timeout=120,
        )
    except asyncio.TimeoutError as exc:
        logger.error("File move operation timed out for %s", name)
        raise HTTPException(status_code=500, detail="File publishing timeout - move operation") from exc
    except Exception as exc:
        logger.error("File move failed for %s: %s", name, exc)
        raise HTTPException(status_code=500, detail=f"File move failed: {str(exc)}") from exc

    size_mb = dst.stat().st_size / (1024 * 1024)
    logger.info(f"Published file: {name} ({size_mb:.2f} MB)")
    struct_logger.info(
        "file_published",
        filename=name,
        size_mb=round(size_mb, 4),
        duration_ms=duration_ms,
    )

    logger.info("File published: %s, starting thumbnail generation", name)
    thumbnail_rel: Optional[str] = None
    try:
        thumb_path = await asyncio.wait_for(
            generate_video_thumbnail(dst),
            timeout=60,
        )
        logger.info("Thumbnail generation finished for %s", name)
        if thumb_path:
            thumbnail_rel = f"/files/{day}/{THUMBNAIL_DIR_NAME}/{thumb_path.name}"
    except asyncio.TimeoutError:
        logger.warning("Thumbnail generation timed out for %s (non-critical)", name)
    except Exception as exc:  # pragma: no cover - defensive logging
        logger.debug("Thumbnail generation failed for %s: %s", dst, exc)

    rel = f"/files/{day}/{name}"
    url = f"{PUBLIC_BASE_URL.rstrip('/')}{rel}" if PUBLIC_BASE_URL else rel
    return {"dst": str(dst), "url": url, "rel": rel, "thumbnail": thumbnail_rel}


def save_log(log_path: Path, operation: str) -> Optional[Path]:
    """Save ffmpeg log to persistent logs directory and return the saved path."""
    if not log_path.exists():
        return None
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
        return dst
    except Exception:
        return None


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
                    "size_display": f"{size_mb:.2f}",
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

    pagination = {
        "page": page,
        "total_pages": total_pages,
        "has_prev": page > 1,
        "has_next": page < total_pages,
        "prev_url": build_page_url(page - 1) if page > 1 else None,
        "next_url": build_page_url(page + 1) if page < total_pages else None,
    }

    clear_url = f"/downloads?page_size={page_size}" if search else None

    if templates is not None:
        context = {
            "request": request,
            "title": "Downloads",
            "max_width": "1400px",
            **nav_context("/downloads"),
            "search": search,
            "page_size": page_size,
            "summary_text": summary_text,
            "entries": current_entries,
            "total_items": total_items,
            "pagination": pagination,
            "clear_url": clear_url,
        }
        return templates.TemplateResponse(request, "downloads.html", context)

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
                    <div class="file-size">{size} MB</div>
                  </div>
                </div>
              </td>
              <td class="date-cell">{day}</td>
            </tr>
            """.format(
                day=html.escape(item["day"]),
                href=html.escape(item["rel"], quote=True),
                name=html.escape(item["name"]),
                size=item["size_display"],
                thumb=thumb_html,
            )
        )

    if not rows:
        if total_items:
            rows.append("<tr><td colspan='3'>No files match your filters.</td></tr>")
        else:
            rows.append("<tr><td colspan='3'>No files yet</td></tr>")

    pagination_links = []
    if pagination["has_prev"] and pagination["prev_url"]:
        pagination_links.append(
            f"<a href=\"{html.escape(pagination['prev_url'], quote=True)}\" class=\"pager-link\">&laquo; Prev</a>"
        )
    if pagination["has_next"] and pagination["next_url"]:
        pagination_links.append(
            f"<a href=\"{html.escape(pagination['next_url'], quote=True)}\" class=\"pager-link\">Next &raquo;</a>"
        )
    pagination_html = (
        "<div class=\"pagination\">" + "".join(pagination_links) + f"<span>Page {page} of {total_pages}</span></div>"
        if total_items
        else ""
    )

    search_value = html.escape(search, quote=True)
    clear_link = (
        f"<a class=\"clear-link\" href=\"{html.escape(clear_url, quote=True)}\">Clear</a>" if clear_url else ""
    )

    top_bar_html = fallback_top_bar_html("/downloads")
    html_content = f"""
    <!doctype html>
    <html>
    <head>
      <meta charset="utf-8" />
      <title>Downloads</title>
      <style>
        body {{ font-family: system-ui, sans-serif; padding: 24px; max-width: 1400px; margin: 0 auto; }}
        .top-bar {{ display: flex; align-items: center; justify-content: space-between; gap: 16px; flex-wrap: wrap; margin-bottom: 24px; }}
        .brand {{ font-size: 32px; font-weight: bold; margin: 0; text-decoration: none; display: inline-flex; align-items: center; gap: 2px; color: #000; }}
        .brand:focus, .brand:hover {{ text-decoration: none; color: #000; }}
        .brand .ff {{ color: #28a745; }}
        .brand .api {{ color: #000; }}
        .main-nav {{ margin: 0; margin-left: auto; display: flex; gap: 12px; flex-wrap: wrap; justify-content: flex-end; }}
        .main-nav a {{ padding: 8px 14px; border-radius: 6px; background: #e2e8f0; color: #6b7280; text-decoration: none; font-weight: 600; transition: background 0.15s ease, color 0.15s ease; }}
        .main-nav a:hover {{ background: #d1d5db; color: #000; }}
        .main-nav a.active {{ background: #28a745; color: #fff; }}
        table {{ border-collapse: collapse; width: 100%; background: #fff; box-shadow: 0 1px 2px rgba(15,23,42,0.08); }}
        th, td {{ border-bottom: 1px solid #e2e8f0; padding: 12px 14px; font-size: 14px; vertical-align: top; }}
        th {{ text-align: left; background: #f8fafc; color: #1f2937; }}
        a {{ text-decoration: none; color: #0f172a; }}
        .filters {{ display: flex; align-items: center; gap: 12px; margin-bottom: 16px; }}
        .filters input[type="text"] {{ flex: 1; padding: 10px; border: 1px solid #cbd5f5; border-radius: 6px; }}
        .filters button {{ padding: 10px 16px; background: #28a745; color: #fff; border: none; border-radius: 6px; cursor: pointer; }}
        .filters button:hover {{ background: #1f7a34; }}
        .summary {{ margin-bottom: 12px; font-size: 14px; color: #475569; }}
        .pagination {{ display: flex; gap: 12px; align-items: center; margin-top: 16px; font-size: 14px; color: #475569; }}
        .pager-link {{ color: #1f7a34; }}
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
{top_bar_html}
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

    def build_page_url(target_page: int) -> str:
        params: Dict[str, Any] = {"page": target_page}
        if page_size != 25:
            params["page_size"] = page_size
        query = urlencode(params)
        return f"/logs?{query}" if query else "/logs"

    pagination = {
        "page": page,
        "total_pages": total_pages,
        "has_prev": page > 1,
        "has_next": page < total_pages,
        "prev_url": build_page_url(page - 1) if page > 1 else None,
        "next_url": build_page_url(page + 1) if page < total_pages else None,
    }

    display_entries = [
        {
            "day": item["day"],
            "name": item["name"],
            "operation": item["operation"],
            "size_display": f"{item['size']:.1f}",
            "path": item["path"],
        }
        for item in current_entries
    ]

    if templates is not None:
        context = {
            "request": request,
            "title": "FFmpeg Logs",
            "max_width": "1400px",
            **nav_context("/logs"),
            "entries": display_entries,
            "total_items": total_items,
            "pagination": pagination,
        }
        return templates.TemplateResponse(request, "logs.html", context)

    rows: List[str] = []
    for item in display_entries:
        rows.append(
            """
            <tr>
              <td>{day}</td>
              <td>{name}</td>
              <td>{operation}</td>
              <td>{size} KB</td>
              <td><a href="/logs/view?path={path}" target="_blank">View</a></td>
            </tr>
            """.format(
                day=html.escape(item["day"]),
                name=html.escape(item["name"]),
                operation=html.escape(item["operation"]),
                size=item["size_display"],
                path=html.escape(item["path"], quote=True),
            )
        )

    if not rows:
        rows.append("<tr><td colspan='5'>No logs yet</td></tr>")

    pagination_links = []
    if pagination["has_prev"] and pagination["prev_url"]:
        pagination_links.append(
            f"<a href=\"{html.escape(pagination['prev_url'], quote=True)}\" class=\"pager-link\">&laquo; Prev</a>"
        )
    if pagination["has_next"] and pagination["next_url"]:
        pagination_links.append(
            f"<a href=\"{html.escape(pagination['next_url'], quote=True)}\" class=\"pager-link\">Next &raquo;</a>"
        )
    pagination_html = (
        "<div class=\"pagination\">" + "".join(pagination_links) + f"<span>Page {page} of {total_pages}</span></div>"
        if total_items
        else ""
    )

    top_bar_html = fallback_top_bar_html("/logs")
    html_content = f"""
    <!doctype html>
    <html>
    <head>
      <meta charset="utf-8" />
      <title>FFmpeg Logs</title>
      <style>
        body {{ font-family: system-ui, sans-serif; padding: 24px; max-width: 1400px; margin: 0 auto; }}
        .top-bar {{ display: flex; align-items: center; justify-content: space-between; gap: 16px; flex-wrap: wrap; margin-bottom: 24px; }}
        .brand {{ font-size: 32px; font-weight: bold; margin: 0; text-decoration: none; display: inline-flex; align-items: center; gap: 2px; color: #000; }}
        .brand:focus, .brand:hover {{ text-decoration: none; color: #000; }}
        .brand .ff {{ color: #28a745; }}
        .brand .api {{ color: #000; }}
        .main-nav {{ margin: 0; margin-left: auto; display: flex; gap: 12px; flex-wrap: wrap; justify-content: flex-end; }}
        .main-nav a {{ padding: 8px 14px; border-radius: 6px; background: #e2e8f0; color: #6b7280; text-decoration: none; font-weight: 600; transition: background 0.15s ease, color 0.15s ease; }}
        .main-nav a:hover {{ background: #d1d5db; color: #000; }}
        .main-nav a.active {{ background: #28a745; color: #fff; }}
        table {{ border-collapse: collapse; width: 100%; background: #fff; box-shadow: 0 1px 2px rgba(15,23,42,0.08); }}
        th, td {{ border-bottom: 1px solid #e2e8f0; padding: 12px 14px; font-size: 14px; }}
        th {{ text-align: left; background: #f8fafc; color: #1f2937; }}
        a {{ text-decoration: none; color: #0f172a; }}
        .pagination {{ display: flex; gap: 12px; align-items: center; margin-top: 16px; font-size: 14px; color: #475569; }}
        .pager-link {{ color: #1f7a34; }}
      </style>
    </head>
    <body>
{top_bar_html}
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

    if templates is not None:
        context = {
            "request": request,
            "title": "FFmpeg Info",
            "max_width": "1400px",
            **nav_context("/ffmpeg"),
            "version_output": version_output,
            "app_logs": app_logs,
            "log_info": log_info,
            "auto_refresh": auto_refresh,
            "refresh_status": refresh_status,
            "current_time": current_time,
        }
        return templates.TemplateResponse(request, "ffmpeg.html", context)

    version_output_safe = html.escape(version_output)
    app_logs_safe = html.escape(app_logs)
    log_info_safe = html.escape(log_info)

    top_bar_html = fallback_top_bar_html("/ffmpeg")
    html_content = f"""
    <!doctype html>
    <html>
    <head>
      <meta charset="utf-8" />
      <title>FFmpeg Info</title>
      {refresh_meta}
      <style>
        body {{ font-family: system-ui, sans-serif; padding: 24px; max-width: 1400px; margin: 0 auto; }}
        .top-bar {{ display: flex; align-items: center; justify-content: space-between; gap: 16px; flex-wrap: wrap; margin-bottom: 24px; }}
        .brand {{ font-size: 32px; font-weight: bold; margin: 0; text-decoration: none; display: inline-flex; align-items: center; gap: 2px; color: #000; }}
        .brand:focus, .brand:hover {{ text-decoration: none; color: #000; }}
        .brand .ff {{ color: #28a745; }}
        .brand .api {{ color: #000; }}
        pre {{ background: #f5f5f5; padding: 15px; border-radius: 4px; overflow-x: auto; font-size: 12px; line-height: 1.4; }}
        h3 {{ margin-top: 30px; margin-bottom: 10px; }}
        .main-nav {{ margin: 0; margin-left: auto; display: flex; gap: 12px; flex-wrap: wrap; justify-content: flex-end; }}
        .main-nav a {{ padding: 8px 14px; border-radius: 6px; background: #e2e8f0; color: #6b7280; text-decoration: none; font-weight: 600; transition: background 0.15s ease, color 0.15s ease; }}
        .main-nav a:hover {{ background: #d1d5db; color: #000; }}
        .main-nav a.active {{ background: #28a745; color: #fff; }}
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
          background: #28a745;
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
{top_bar_html}
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
    api_docs = r"""
<h4>UI & Info</h4>
<div class="endpoint">
  <div class="method get">GET</div>
  <div class="path">/</div>
  <div class="desc">Redirects to /downloads</div>
  <div class="responses">
    <div class="response-title">Possible Responses</div>
    <ul>
      <li><code>302</code> Redirect to <code>/downloads</code></li>
      <li><code>303</code> Redirect to <code>/settings</code> when UI login is required</li>
    </ul>
  </div>
  <div class="example">Example:<br>curl http://localhost:3000/</div>
</div>

<div class="endpoint">
  <div class="method get">GET</div>
  <div class="path">/downloads</div>
  <div class="desc">Browse generated files (HTML page)</div>
  <div class="responses">
    <div class="response-title">Possible Responses</div>
    <ul>
      <li><code>200</code> HTML page listing generated files</li>
      <li><code>303</code> Redirect to <code>/settings</code> when UI login is required</li>
    </ul>
  </div>
  <div class="example">Example:<br>curl http://localhost:3000/downloads</div>
</div>

<div class="endpoint">
  <div class="method get">GET</div>
  <div class="path">/logs</div>
  <div class="desc">Browse FFmpeg operation logs (HTML page)</div>
  <div class="responses">
    <div class="response-title">Possible Responses</div>
    <ul>
      <li><code>200</code> HTML log browser</li>
      <li><code>303</code> Redirect to <code>/settings</code> when UI login is required</li>
    </ul>
  </div>
  <div class="example">Example:<br>curl http://localhost:3000/logs</div>
</div>

<div class="endpoint">
  <div class="method get">GET</div>
  <div class="path">/logs/view?path={date}/{filename}</div>
  <div class="desc">View individual log file content</div>
  <div class="params">
    <span class="param">path</span> - Relative path to log file (e.g., 20241008/20241008_143022_12345678_compose-urls.log)
  </div>
  <div class="responses">
    <div class="response-title">Possible Responses</div>
    <ul>
      <li><code>200</code> Plain-text log contents</li>
      <li><code>303</code> Redirect to <code>/settings</code> when UI login is required</li>
      <li><code>404</code> JSON error when the specified log file is missing</li>
      <li><code>413</code> JSON error when the requested log exceeds the size limit</li>
    </ul>
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
  <div class="responses">
    <div class="response-title">Possible Responses</div>
    <ul>
      <li><code>200</code> HTML diagnostics dashboard</li>
      <li><code>303</code> Redirect to <code>/settings</code> when UI login is required</li>
    </ul>
  </div>
  <div class="example">Example:<br>curl http://localhost:3000/ffmpeg?auto_refresh=10</div>
</div>

<div class="endpoint">
  <div class="method get">GET</div>
  <div class="path">/documentation</div>
  <div class="desc">This page - Complete API documentation</div>
  <div class="responses">
    <div class="response-title">Possible Responses</div>
    <ul>
      <li><code>200</code> HTML API reference</li>
      <li><code>303</code> Redirect to <code>/settings</code> when UI login is required</li>
    </ul>
  </div>
  <div class="example">Example:<br>curl http://localhost:3000/documentation</div>
</div>

<div class="endpoint">
  <div class="method get">GET</div>
  <div class="path">/settings</div>
  <div class="desc">Configuration page for UI authentication and storage overview</div>
  <div class="responses">
    <div class="response-title">Possible Responses</div>
    <ul>
      <li><code>200</code> HTML settings dashboard</li>
      <li><code>303</code> Redirect to login when UI authentication is enabled and unauthenticated</li>
    </ul>
  </div>
  <div class="example">Example:<br>curl http://localhost:3000/settings</div>
</div>

<div class="endpoint">
  <div class="method get">GET</div>
  <div class="path">/api-keys</div>
  <div class="desc">Manage API authentication, generate keys, and review usage history</div>
  <div class="responses">
    <div class="response-title">Possible Responses</div>
    <ul>
      <li><code>200</code> HTML page for key management</li>
      <li><code>303</code> Redirect to login when UI authentication is enabled and unauthenticated</li>
    </ul>
  </div>
  <div class="example">Example:<br>curl http://localhost:3000/api-keys</div>
</div>

<div class="endpoint">
  <div class="method post">POST</div>
  <div class="path">/api-keys/(require|generate|revoke)</div>
  <div class="desc">Toggle API key enforcement, create new keys, or revoke existing ones</div>
  <div class="params">Submit form-encoded fields such as <span class="param">require_api_key</span> or <span class="param">key_id</span>.</div>
  <div class="responses">
    <div class="response-title">Possible Responses</div>
    <ul>
      <li><code>303</code> Redirect back to <code>/api-keys</code> with success or error feedback in the query string</li>
      <li><code>303</code> Redirect to <code>/settings</code> when UI login is required</li>
    </ul>
  </div>
</div>

<div class="endpoint">
  <div class="method get">GET</div>
  <div class="path">/health</div>
  <div class="desc">Health check endpoint with disk, memory, FFmpeg, and operation metrics</div>
  <div class="responses">
    <div class="response-title">Possible Responses</div>
    <ul>
      <li><code>200</code> JSON body with <code>ok</code>, <code>disk</code>, <code>memory</code>, <code>ffmpeg</code>, and <code>operations</code> sections</li>
      <li><code>401</code> / <code>403</code> JSON errors when API key enforcement or IP allow-listing blocks the request</li>
      <li><code>429</code> Plain-text error when the caller exceeds the global rate limit</li>
      <li><code>500</code> JSON error when health information cannot be collected</li>
    </ul>
  </div>
  <div class="example">Example:<br>curl http://localhost:3000/health</div>
</div>

<div class="endpoint">
  <div class="method get">GET</div>
  <div class="path">/metrics</div>
  <div class="desc">Operational metrics dashboard (HTML page)</div>
  <div class="responses">
    <div class="response-title">Possible Responses</div>
    <ul>
      <li><code>200</code> HTML dashboard with aggregated metrics</li>
      <li><code>303</code> Redirect to <code>/settings</code> when UI login is required</li>
    </ul>
  </div>
  <div class="example">Example:<br>curl http://localhost:3000/metrics</div>
</div>

<div class="endpoint">
  <div class="method get">GET</div>
  <div class="path">/files/{date}/{filename}</div>
  <div class="desc">Static file serving - download generated files</div>
  <div class="responses">
    <div class="response-title">Possible Responses</div>
    <ul>
      <li><code>200</code> Binary file download</li>
      <li><code>404</code> Plain-text response when the requested file does not exist</li>
    </ul>
  </div>
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
  <div class="responses">
    <div class="response-title">Possible Responses</div>
    <ul>
      <li><code>200</code> MP4 file stream or JSON payload with <code>ok</code>, <code>file_url</code>, and <code>path</code></li>
      <li><code>401</code> / <code>403</code> JSON errors when API key enforcement or IP allow-listing blocks the request</li>
      <li><code>413</code> JSON error when the uploaded file exceeds configured limits</li>
      <li><code>422</code> JSON validation error for malformed parameters</li>
      <li><code>429</code> Plain-text error when the caller exceeds the global rate limit</li>
      <li><code>500</code> JSON error when FFmpeg processing fails</li>
    </ul>
  </div>
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
  <div class="responses">
    <div class="response-title">Possible Responses</div>
    <ul>
      <li><code>200</code> MP4 file stream or JSON payload with <code>ok</code>, <code>file_url</code>, and <code>path</code></li>
      <li><code>401</code> / <code>403</code> JSON errors when API key enforcement or IP allow-listing blocks the request</li>
      <li><code>413</code> JSON error when uploaded binaries exceed configured limits</li>
      <li><code>422</code> JSON validation error for malformed parameters</li>
      <li><code>429</code> Plain-text error when the caller exceeds the global rate limit</li>
      <li><code>500</code> JSON error when FFmpeg composition fails</li>
    </ul>
  </div>
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
    <span class="param">duration_ms</span> - Duration in milliseconds (1-3600000, optional)<br>
    <span class="param">width</span> - Output width in pixels (default: 1920)<br>
    <span class="param">height</span> - Output height in pixels (default: 1080)<br>
    <span class="param">fps</span> - Output frames per second (default: 30)<br>
    <span class="param">bgm_volume</span> - BGM volume multiplier (default: 0.3)<br>
    <span class="param">loop_bgm</span> - Loop background music to fill duration (default: false)<br>
    <span class="param">headers</span> - HTTP headers for authenticated requests (optional)<br>
    <span class="param">as_json</span> - Return JSON instead of file (default: false)
  </div>
  <div class="responses">
    <div class="response-title">Possible Responses</div>
    <ul>
      <li><code>200</code> MP4 file stream or JSON payload with <code>ok</code>, <code>file_url</code>, and <code>path</code></li>
      <li><code>401</code> / <code>403</code> JSON errors when API key enforcement or IP allow-listing blocks the request</li>
      <li><code>404</code> JSON error when referenced media URLs cannot be retrieved</li>
      <li><code>422</code> JSON validation error for malformed parameters</li>
      <li><code>429</code> Plain-text error when the caller exceeds the global rate limit</li>
      <li><code>500</code> JSON error when FFmpeg composition fails</li>
    </ul>
  </div>
  <div class="example">Example:<br>curl -X POST http://localhost:3000/compose/from-urls \<br>  -H "Content-Type: application/json" \<br>  -d '{<br>    "video_url": "https://example.com/video.mp4",<br>    "audio_url": "https://example.com/audio.mp3",<br>    "bgm_url": "https://example.com/music.mp3",<br>    "duration": 20.0,<br>    "width": 1280,<br>    "height": 720,<br>    "fps": 30,<br>    "bgm_volume": 0.3,<br>    "as_json": true<br>  }'</div>
</div>

<div class="endpoint">
  <div class="method post">POST</div>
  <div class="path">/compose/from-urls/async</div>
  <div class="desc">Queue video composition as a background job</div>
  <div class="params">
    Same parameters as <code>/compose/from-urls</code>
  </div>
  <div class="responses">
    <div class="response-title">Possible Responses</div>
    <ul>
      <li><code>200</code> JSON payload with <code>job_id</code> and <code>status_url</code></li>
      <li><code>401</code> / <code>403</code> JSON errors when API key enforcement or IP allow-listing blocks the request</li>
      <li><code>422</code> JSON validation error for malformed parameters</li>
      <li><code>429</code> Plain-text error when the caller exceeds the global rate limit</li>
      <li><code>500</code> JSON error when the job cannot be enqueued</li>
    </ul>
  </div>
  <div class="example">Example:<br>curl -X POST http://localhost:3000/compose/from-urls/async \<br>  -H "Content-Type: application/json" \<br>  -d '{<br>    "video_url": "https://example.com/video.mp4",<br>    "duration_ms": 10000<br>  }'</div>
</div>

<div class="endpoint">
  <div class="method get">GET</div>
  <div class="path">/jobs/{job_id}</div>
  <div class="desc">Fetch status details for an asynchronous job</div>
  <div class="params">
    <span class="param">job_id</span> - Identifier returned from the async compose endpoint
  </div>
  <div class="responses">
    <div class="response-title">Possible Responses</div>
    <ul>
      <li><code>200</code> JSON payload including <code>status</code>, progress metrics, and any result URLs</li>
      <li><code>401</code> / <code>403</code> JSON errors when API key enforcement or IP allow-listing blocks the request</li>
      <li><code>404</code> JSON error when the job cannot be located</li>
      <li><code>429</code> Plain-text error when the caller exceeds the global rate limit</li>
    </ul>
  </div>
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
  <div class="responses">
    <div class="response-title">Possible Responses</div>
    <ul>
      <li><code>200</code> MP4 file stream or JSON payload with <code>ok</code>, <code>file_url</code>, and <code>path</code></li>
      <li><code>401</code> / <code>403</code> JSON errors when API key enforcement or IP allow-listing blocks the request</li>
      <li><code>422</code> JSON validation error for malformed track definitions</li>
      <li><code>429</code> Plain-text error when the caller exceeds the global rate limit</li>
      <li><code>500</code> JSON error when FFmpeg composition fails</li>
    </ul>
  </div>
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
  <div class="responses">
    <div class="response-title">Possible Responses</div>
    <ul>
      <li><code>200</code> MP4 file stream or JSON payload with <code>ok</code>, <code>file_url</code>, and <code>path</code></li>
      <li><code>401</code> / <code>403</code> JSON errors when API key enforcement or IP allow-listing blocks the request</li>
      <li><code>404</code> JSON error when referenced clips cannot be retrieved</li>
      <li><code>422</code> JSON validation error for malformed parameters</li>
      <li><code>429</code> Plain-text error when the caller exceeds the global rate limit</li>
      <li><code>500</code> JSON error when FFmpeg concatenation fails</li>
    </ul>
  </div>
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
  <div class="responses">
    <div class="response-title">Possible Responses</div>
    <ul>
      <li><code>200</code> MP4 file stream or JSON payload with <code>ok</code>, <code>file_url</code>, and <code>path</code></li>
      <li><code>401</code> / <code>403</code> JSON errors when API key enforcement or IP allow-listing blocks the request</li>
      <li><code>404</code> JSON error when referenced clips cannot be retrieved</li>
      <li><code>422</code> JSON validation error for malformed parameters</li>
      <li><code>429</code> Plain-text error when the caller exceeds the global rate limit</li>
      <li><code>500</code> JSON error when FFmpeg concatenation fails</li>
    </ul>
  </div>
  <div class="example">Example (using 'clips'):<br>curl -X POST http://localhost:3000/video/concat \<br>  -H "Content-Type: application/json" \<br>  -d '{<br>    "clips": [<br>      "https://example.com/video1.mp4",<br>      "https://example.com/video2.mp4"<br>    ],<br>    "width": 1280,<br>    "height": 720,<br>    "as_json": true<br>  }'</div>
</div>

<h4>Audio Processing</h4>
<div class="endpoint">
  <div class="method post">POST</div>
  <div class="path">/v1/audio/tempo</div>
  <div class="desc">Download an audio source, adjust playback speed with FFmpeg, and publish the converted file. Accepts <code>Content-Type: application/json</code>.</div>
  <div class="params">
    <span class="param">input_url</span> - HTTP(S) URL for the source audio<br>
    <span class="param">output_name</span> - Desired filename for the processed output (default: output.mp3)<br>
    <span class="param">tempo</span> - Playback rate multiplier (0.5 - 2.0, default: 1.0)
  </div>
  <div class="responses">
    <div class="response-title">Possible Responses</div>
    <ul>
      <li><code>200</code> JSON payload with <code>status</code>, <code>job_id</code>, <code>tempo</code>, <code>input_size</code>, <code>output_file</code>, <code>download_url</code>, and <code>processing_time</code></li>
      <li><code>401</code> / <code>403</code> JSON errors when API key enforcement or IP allow-listing blocks the request</li>
      <li><code>422</code> JSON validation error for malformed parameters</li>
      <li><code>429</code> Plain-text error when the caller exceeds the global rate limit</li>
      <li><code>500</code> JSON error when FFmpeg processing fails</li>
      <li><code>504</code> JSON error when tempo conversion exceeds the processing timeout</li>
    </ul>
  </div>
  <div class="example">JSON Example:<br>curl -X POST http://localhost:3000/v1/audio/tempo \<br>  -H "Content-Type: application/json" \<br>  -d '{<br>    "input_url": "http://10.120.2.5:4321/audio/speech/long/abcd1234/download",<br>    "output_name": "slowed.mp3",<br>    "tempo": 0.85<br>  }'</div>
  <div class="example">n8n curl Example (Execute Command node):<br>curl -X POST "$FFAPI_BASE_URL/v1/audio/tempo" \<br>  -H "Content-Type: application/json" \<br>  -d '{<br>    "input_url": "&#123;&#123; $json[\"input_url\"] &#125;&#125;",<br>    "output_name": "&#123;&#123; $json[\"output_name\"] || \"tempo-output.mp3\" &#125;&#125;",<br>    "tempo": &#123;&#123; $json[\"tempo\"] || 0.85 &#125;&#125;<br>  }'</div>
</div>

<div class="endpoint">
  <div class="method get">GET</div>
  <div class="path">/v1/audio/tempo</div>
  <div class="desc">List the most recent tempo jobs recorded in memory</div>
  <div class="responses">
    <div class="response-title">Possible Responses</div>
    <ul>
      <li><code>200</code> JSON array of job summaries with <code>id</code>, filenames, <code>tempo</code>, timestamps, and status</li>
      <li><code>401</code> / <code>403</code> JSON errors when API key enforcement or IP allow-listing blocks the request</li>
      <li><code>429</code> Plain-text error when the caller exceeds the global rate limit</li>
    </ul>
  </div>
  <div class="example">Example:<br>curl http://localhost:3000/v1/audio/tempo</div>
</div>

<div class="endpoint">
  <div class="method get">GET</div>
  <div class="path">/v1/audio/tempo/{job_id}/status</div>
  <div class="desc">Fetch live status for a previously created tempo job</div>
  <div class="params">
    <span class="param">job_id</span> - Identifier returned from the POST /v1/audio/tempo response
  </div>
  <div class="responses">
    <div class="response-title">Possible Responses</div>
    <ul>
      <li><code>200</code> JSON payload including <code>status</code>, <code>tempo</code>, <code>processing_time</code>, and download metadata</li>
      <li><code>401</code> / <code>403</code> JSON errors when API key enforcement or IP allow-listing blocks the request</li>
      <li><code>404</code> JSON error when the job cannot be located</li>
      <li><code>429</code> Plain-text error when the caller exceeds the global rate limit</li>
    </ul>
  </div>
  <div class="example">Example:<br>curl http://localhost:3000/v1/audio/tempo/b9f32f/status</div>
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
  <div class="responses">
    <div class="response-title">Possible Responses</div>
    <ul>
      <li><code>200</code> JSON payload with <code>ok</code> and an <code>outputs</code> mapping of generated URLs</li>
      <li><code>400</code> JSON error when the command is invalid or violates safety checks</li>
      <li><code>401</code> / <code>403</code> JSON errors when API key enforcement or IP allow-listing blocks the request</li>
      <li><code>429</code> Plain-text error when the caller exceeds the global rate limit</li>
      <li><code>500</code> JSON error when FFmpeg execution fails</li>
    </ul>
  </div>
  <div class="example">Example (scale video to 720p):<br>curl -X POST http://localhost:3000/v1/run-ffmpeg-command \<br>  -H "Content-Type: application/json" \<br>  -d '{<br>    "input_files": {<br>      "video": "https://example.com/input.mp4"<br>    },<br>    "output_files": {<br>      "out": "result.mp4"<br>    },<br>    "ffmpeg_command": "-i {{video}} -vf scale=1280:720 -c:a copy {{out}}"<br>  }'</div>
</div>

<h4>Advanced FFmpeg Jobs</h4>
<div class="endpoint">
  <div class="method post">POST</div>
  <div class="path">/v2/run-ffmpeg-job</div>
  <div class="desc">Execute FFmpeg with structured inputs and filter_complex</div>
  <div class="params">
    <span class="param">task.inputs</span> - Array of input file URLs with optional per-input options<br>
    <span class="param">task.filter_complex</span> - FFmpeg filter_complex string<br>
    <span class="param">task.outputs</span> - Array of output specifications with options<br>
    <span class="param">as_json</span> - Return JSON instead of file (default: false)
  </div>
  <div class="responses">
    <div class="response-title">Possible Responses</div>
    <ul>
      <li><code>200</code> File response or JSON with output URLs</li>
      <li><code>422</code> Validation error for malformed parameters</li>
      <li><code>500</code> FFmpeg execution failed</li>
    </ul>
  </div>
  <div class="example">Example:<br>curl -X POST http://localhost:3000/v2/run-ffmpeg-job \<br>  -H "Content-Type: application/json" \<br>  -d '{<br>    "task": {<br>      "inputs": [<br>        {"file_path": "https://example.com/image.jpg"},<br>        {"file_path": "https://example.com/song1.mp3"}<br>      ],<br>      "filter_complex": "[1:a]anull[aout];[0:v]loop=999:size=1:start=0,scale=1280:720[vout]",<br>      "outputs": [<br>        {<br>          "file": "output.mp4",<br>          "options": ["-map", "[vout]", "-map", "[aout]", "-c:v", "libx264", "-c:a", "aac"]<br>        }<br>      ]<br>    },<br>    "as_json": true<br>  }'</div>
</div>

<div class="endpoint">
  <div class="method post">POST</div>
  <div class="path">/v2/run-ffmpeg-job/async</div>
  <div class="desc">Queue an FFmpeg job and poll /jobs/{job_id} for status</div>
  <div class="params">
    <span class="param">task.inputs</span> - Array of input file URLs with optional per-input options<br>
    <span class="param">task.filter_complex</span> - FFmpeg filter_complex string<br>
    <span class="param">task.outputs</span> - Array of output specifications with options<br>
    <span class="param">headers</span> - Optional headers forwarded to source downloads
  </div>
  <div class="responses">
    <div class="response-title">Possible Responses</div>
    <ul>
      <li><code>200</code> JSON payload with <code>job_id</code> and <code>status_url</code></li>
      <li><code>422</code> Validation error for malformed parameters</li>
      <li><code>500</code> Job queueing failed</li>
    </ul>
  </div>
  <div class="example">Example:<br>curl -X POST http://localhost:3000/v2/run-ffmpeg-job/async \<br>  -H "Content-Type: application/json" \<br>  -d '{<br>    "task": {<br>      "inputs": [<br>        {"file_path": "https://example.com/image.jpg"},<br>        {"file_path": "https://example.com/song1.mp3"}<br>      ],<br>      "filter_complex": "[1:a]anull[aout];[0:v]loop=999:size=1:start=0,scale=1280:720[vout]",<br>      "outputs": [<br>        {<br>          "file": "output.mp4",<br>          "options": ["-map", "[vout]", "-map", "[aout]", "-c:v", "libx264", "-c:a", "aac"]<br>        }<br>      ]<br>    }<br>  }'</div>
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
  <div class="responses">
    <div class="response-title">Possible Responses</div>
    <ul>
      <li><code>200</code> JSON payload mirroring the ffprobe output</li>
      <li><code>400</code> JSON error when the supplied URL or options are invalid</li>
      <li><code>401</code> / <code>403</code> JSON errors when API key enforcement or IP allow-listing blocks the request</li>
      <li><code>404</code> JSON error when the remote media cannot be downloaded</li>
      <li><code>422</code> JSON validation error for malformed parameters</li>
      <li><code>429</code> Plain-text error when the caller exceeds the global rate limit</li>
      <li><code>500</code> JSON error when ffprobe execution fails</li>
    </ul>
  </div>
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
  <div class="responses">
    <div class="response-title">Possible Responses</div>
    <ul>
      <li><code>200</code> JSON payload mirroring the ffprobe output</li>
      <li><code>401</code> / <code>403</code> JSON errors when API key enforcement or IP allow-listing blocks the request</li>
      <li><code>413</code> JSON error when the uploaded file exceeds configured limits</li>
      <li><code>422</code> JSON validation error for malformed parameters</li>
      <li><code>429</code> Plain-text error when the caller exceeds the global rate limit</li>
      <li><code>500</code> JSON error when ffprobe execution fails</li>
    </ul>
  </div>
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
  <div class="responses">
    <div class="response-title">Possible Responses</div>
    <ul>
      <li><code>200</code> JSON payload mirroring the ffprobe output</li>
      <li><code>400</code> JSON error when the relative path is invalid</li>
      <li><code>401</code> / <code>403</code> JSON errors when API key enforcement, IP allow-listing, or directory safety checks block the request</li>
      <li><code>404</code> JSON error when the referenced file does not exist</li>
      <li><code>429</code> Plain-text error when the caller exceeds the global rate limit</li>
      <li><code>500</code> JSON error when ffprobe execution fails</li>
    </ul>
  </div>
  <div class="example">Example:<br>curl "http://localhost:3000/probe/public?rel=20241008/20241008_143022_12345678.mp4&show_format=true&show_streams=true"</div>
</div>
"""

    base_url = PUBLIC_BASE_URL or "http://localhost:3000"
    retention_days = str(RETENTION_DAYS)
    if templates is None:
        top_bar_html = fallback_top_bar_html("/documentation")
        html_content = f"""
    <!doctype html>
    <html>
    <head>
      <meta charset=\"utf-8\" />
      <title>API Documentation</title>
      <style>
        body {{ font-family: system-ui, sans-serif; padding: 24px; max-width: 1400px; margin: 0 auto; }}
        .top-bar {{ display: flex; align-items: center; justify-content: space-between; gap: 16px; flex-wrap: wrap; margin-bottom: 24px; }}
        .brand {{ font-size: 32px; font-weight: bold; margin: 0; text-decoration: none; display: inline-flex; align-items: center; gap: 2px; color: #000; }}
        .brand:focus, .brand:hover {{ text-decoration: none; color: #000; }}
        .brand .ff {{ color: #28a745; }}
        .brand .api {{ color: #000; }}
        h2 {{ margin-bottom: 10px; }}
        h4 {{ margin-top: 30px; margin-bottom: 15px; color: #333; border-bottom: 2px solid #28a745; padding-bottom: 5px; }}
        .main-nav {{ margin: 0; margin-left: auto; display: flex; gap: 12px; flex-wrap: wrap; justify-content: flex-end; }}
        .main-nav a {{ padding: 8px 14px; border-radius: 6px; background: #e2e8f0; color: #6b7280; text-decoration: none; font-weight: 600; transition: background 0.15s ease, color 0.15s ease; }}
        .main-nav a:hover {{ background: #d1d5db; color: #000; }}
        .main-nav a.active {{ background: #28a745; color: #fff; }}
        .intro {{
          background: #f0f7ff;
          padding: 20px;
          border-radius: 8px;
          margin-bottom: 30px;
          border-left: 4px solid #28a745;
        }}
        .intro p {{ margin: 8px 0; line-height: 1.6; }}

        .endpoint {{
          background: #f9f9f9;
          border-left: 4px solid #28a745;
          padding: 12px 15px;
          margin-bottom: 12px;
          border-radius: 4px;
        }}
        .method {{
          display: inline-block;
          background: #28a745;
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
        .responses {{
          margin-top: 10px;
          font-size: 13px;
          color: #374151;
        }}
        .response-title {{
          font-weight: 600;
          margin-bottom: 4px;
        }}
        .responses ul {{
          margin: 4px 0 0 18px;
          padding: 0;
        }}
        .responses li {{
          margin: 2px 0;
          list-style: disc;
        }}
        .responses code {{
          background: #e2e8f0;
          padding: 1px 4px;
          border-radius: 4px;
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
          color: #28a745;
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
{top_bar_html}
      <h2>FFAPI Ultimate - API Documentation</h2>

      <div class=\"intro\">
        <p><strong>Base URL:</strong> {base_url}</p>
        <p><strong>Response Formats:</strong> Most endpoints support both file responses and JSON (via <code>as_json=true</code> parameter)</p>
        <p><strong>File Retention:</strong> Generated files are kept for {retention_days} days</p>
        <p><strong>Output Location:</strong> All generated files are available at <code>/files/{{date}}/{{filename}}</code></p>
      </div>

      {api_docs}
    </body>
    </html>
    """
        return HTMLResponse(html_content)

    context = {
        "request": request,
        "title": "API Documentation",
        "max_width": "1400px",
        **nav_context("/documentation"),
        "base_url": base_url,
        "retention_days": retention_days,
        "api_docs": api_docs,
    }
    return templates.TemplateResponse(request, "documentation.html", context)


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
        return login_template_response(
            request,
            next_target,
            csrf_token,
            error=error or None,
        )

    storage = storage_management_snapshot()
    csrf_token = ensure_csrf_token(request)
    backup_codes = UI_AUTH.pop_pending_backup_codes()
    backup_status = UI_AUTH.get_backup_code_status()
    return settings_template_response(
        request,
        storage,
        message or None,
        error or None,
        authenticated=authed,
        csrf_token=csrf_token,
        backup_codes=backup_codes,
        backup_status=backup_status,
    )


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

    client_ip = request.client.host if request.client else "unknown"
    if not LOGIN_RATE_LIMITER.check(client_ip):
        csrf_token = ensure_csrf_token(request)
        return login_template_response(
            request,
            next_path,
            csrf_token,
            error="Too many login attempts. Try again later.",
            status_code=429,
        )

    if UI_AUTH.verify(username, password):
        if UI_AUTH.is_two_factor_enabled():
            if not totp_code and not backup_code:
                csrf_token = ensure_csrf_token(request)
                return login_template_response(
                    request,
                    next_path,
                    csrf_token,
                    error="Authentication or backup code required",
                    status_code=401,
                )
            second_factor_ok = False
            if totp_code:
                second_factor_ok = UI_AUTH.verify_totp(totp_code)
            if not second_factor_ok:
                if backup_code:
                    second_factor_ok = UI_AUTH.verify_backup_code(backup_code)
            if not second_factor_ok:
                csrf_token = ensure_csrf_token(request)
                return login_template_response(
                    request,
                    next_path,
                    csrf_token,
                    error="Invalid authentication or backup code",
                    status_code=401,
                )
        token = UI_AUTH.create_session()
        response = RedirectResponse(url=next_path, status_code=303)
        cookie_is_secure = request.url.scheme == "https"
        samesite_policy = "strict" if cookie_is_secure else "lax"
        response.set_cookie(
            SESSION_COOKIE_NAME,
            token,
            max_age=SESSION_TTL_SECONDS,
            httponly=True,
            samesite=samesite_policy,
            secure=cookie_is_secure,
        )
        return response

    csrf_token = ensure_csrf_token(request)
    return login_template_response(
        request,
        next_path,
        csrf_token,
        error="Invalid username or password",
        status_code=401,
    )


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


@app.post("/settings/api-whitelist")
async def settings_update_api_whitelist(request: Request):
    authed = is_authenticated(request)
    if UI_AUTH.require_login and not authed:
        response = RedirectResponse(
            url=f"/settings?{urlencode({'error': 'Authentication required to change API whitelist'})}",
            status_code=303,
        )
        response.delete_cookie(SESSION_COOKIE_NAME)
        return response

    if not API_KEYS.is_required():
        query = urlencode(
            {"error": "Enable API authentication before configuring the API whitelist"}
        )
        return RedirectResponse(url=f"/settings?{query}", status_code=303)

    form = await request.form()
    raw_enabled = form.get("enable_whitelist")
    enable = False
    if raw_enabled is not None:
        enable = str(raw_enabled).lower() in {"true", "1", "on", "yes"}
    entries_text = str(form.get("whitelist_entries", ""))
    entries = _split_whitelist_entries(entries_text)

    try:
        API_WHITELIST.configure(enable, entries)
    except ValueError as exc:
        storage = storage_management_snapshot()
        csrf_token = ensure_csrf_token(request)
        return settings_template_response(
            request,
            storage,
            error=str(exc),
            authenticated=authed,
            csrf_token=csrf_token,
            whitelist_text=entries_text,
            status_code=400,
        )

    message = "API whitelist enabled" if enable else "API whitelist disabled"
    query = urlencode({"message": message})
    return RedirectResponse(url=f"/settings?{query}", status_code=303)


@app.post("/settings/ui-auth")
async def settings_toggle_auth(request: Request):
    form = await request.form()
    raw_value = form.get("require_login")
    authed = is_authenticated(request)
    # Only require auth if it's currently enabled (allows enabling auth when disabled)
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
    password_confirm = str(form.get("password_confirm", ""))
    authed = is_authenticated(request)
    if UI_AUTH.require_login and not authed:
        response = RedirectResponse(
            url=f"/settings?{urlencode({'error': 'Authentication required to change credentials'})}",
            status_code=303,
        )
        response.delete_cookie(SESSION_COOKIE_NAME)
        return response

    if password != password_confirm:
        storage = storage_management_snapshot()
        csrf_token = ensure_csrf_token(request)
        return settings_template_response(
            request,
            storage,
            error="Passwords do not match",
            authenticated=authed,
            csrf_token=csrf_token,
            status_code=400,
        )

    try:
        UI_AUTH.set_credentials(username, password)
    except ValueError as exc:
        storage = storage_management_snapshot()
        csrf_token = ensure_csrf_token(request)
        return settings_template_response(
            request,
            storage,
            error=str(exc),
            authenticated=authed,
            csrf_token=csrf_token,
            status_code=400,
        )

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
        return settings_template_response(
            request,
            storage,
            error="Retention hours are required",
            authenticated=authed,
            csrf_token=csrf_token,
            status_code=400,
        )

    try:
        hours = float(raw_hours)
    except ValueError:
        storage = storage_management_snapshot()
        csrf_token = ensure_csrf_token(request)
        return settings_template_response(
            request,
            storage,
            error="Retention hours must be a number",
            authenticated=authed,
            csrf_token=csrf_token,
            status_code=400,
        )

    if hours < 1 or hours > 24 * 365:
        storage = storage_management_snapshot()
        csrf_token = ensure_csrf_token(request)
        return settings_template_response(
            request,
            storage,
            error="Retention must be between 1 hour and 8760 hours (365 days)",
            authenticated=authed,
            csrf_token=csrf_token,
            status_code=400,
        )

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
        return settings_template_response(
            request,
            storage,
            error="; ".join(errors) if errors else "Invalid performance settings",
            authenticated=authed,
            csrf_token=csrf_token,
            status_code=400,
        )

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
    return api_keys_template_response(
        request,
        keys=API_KEYS.list_keys(),
        require_key=API_KEYS.is_required(),
        message=message or None,
        error=error or None,
        new_key=new_key_value,
        csrf_token=csrf_token,
    )

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

    requested_gpu = os.getenv("ENABLE_GPU", "false").lower() == "true"
    runtime_disabled = GPU_STATE.is_runtime_disabled()
    gpu_config = get_gpu_config()
    gpu_status: Dict[str, Any] = {
        "requested": requested_gpu,
        "enabled": gpu_config["enabled"],
        "runtime_disabled": runtime_disabled,
        "encoder": os.getenv("GPU_ENCODER", "h264_nvenc"),
        "decoder": os.getenv("GPU_DECODER", "h264_cuvid"),
        "device": os.getenv("GPU_DEVICE", "0"),
    }

    gpu_ok = True
    if requested_gpu:
        if not gpu_config["enabled"] or runtime_disabled:
            gpu_status["available"] = False
            gpu_status["issue"] = "runtime_disabled"
            gpu_ok = False
        else:
            try:
                result = subprocess.run(
                    ["nvidia-smi", "-L"], capture_output=True, text=True, timeout=2
                )
                available = result.returncode == 0
                gpu_status["available"] = available
                if not available:
                    stderr = (result.stderr or "").strip()
                    if stderr:
                        gpu_status["nvidia_smi_error"] = stderr
                    gpu_ok = False
            except Exception as exc:
                gpu_status["available"] = False
                gpu_status["nvidia_smi_error"] = str(exc)
                gpu_ok = False
    else:
        gpu_status["available"] = None

    overall_ok = ffmpeg_info.get("available", False) and disk_ok and gpu_ok

    return {
        "ok": overall_ok,
        "disk": disk,
        "memory": memory,
        "ffmpeg": ffmpeg_info,
        "gpu": gpu_status,
        "operations": {
            "recent_success_rate": metrics_data["recent"],
            "queue": metrics_data["queue"],
            "error_counts": metrics_data["errors"],
        },
    }


@app.get("/gpu/test")
async def test_gpu_encoding(request: Request):
    """Run a lightweight GPU encoding smoke test."""

    redirect = ensure_dashboard_access(request)
    if redirect:
        return redirect

    gpu_config = get_gpu_config()
    runtime_disabled = GPU_STATE.is_runtime_disabled()

    if not gpu_config["enabled"] or runtime_disabled:
        status = "runtime_disabled" if runtime_disabled else "disabled"
        return {
            "status": status,
            "message": "GPU acceleration is not enabled",
            "encoder": os.getenv("GPU_ENCODER", "h264_nvenc"),
        }

    try:
        with TemporaryDirectory(prefix="gpu_test_", dir=str(WORK_DIR)) as workdir:
            work = Path(workdir)
            test_video = work / "test.mp4"
            cmd = [
                "ffmpeg",
                "-y",
                "-f",
                "lavfi",
                "-i",
                "color=c=blue:s=320x240:d=1",
                "-c:v",
                gpu_config["encoder"],
                "-t",
                "1",
                str(test_video),
            ]
            result = subprocess.run(cmd, capture_output=True, text=True, timeout=10)
            if result.returncode == 0 and test_video.exists():
                return {
                    "status": "success",
                    "encoder": gpu_config["encoder"],
                    "message": "GPU encoding test passed",
                }
            return {
                "status": "failed",
                "encoder": gpu_config["encoder"],
                "stderr": (result.stderr or "").strip(),
            }
    except Exception as exc:
        return {"status": "error", "message": str(exc)}


@app.get("/metrics", response_class=HTMLResponse)
def metrics_dashboard(request: Request):
    redirect = ensure_dashboard_access(request)
    if redirect:
        return redirect
    snapshot = METRICS.snapshot()
    endpoint_rows = []
    for path, stats in sorted(snapshot["per_endpoint"].items()):
        endpoint_rows.append(
            {
                "path": path,
                "success": int(stats.get("success", 0)),
                "failure": int(stats.get("failure", 0)),
                "avg_duration": stats.get("avg_duration_ms", 0.0),
                "success_percent": stats.get("success_rate", 0.0) * 100.0,
            }
        )

    if not endpoint_rows:
        endpoint_rows.append(
            {
                "path": "",
                "success": None,
                "failure": None,
                "avg_duration": None,
                "success_percent": None,
            }
        )

    error_rows = [
        {"name": name, "count": int(count)}
        for name, count in sorted(snapshot["errors"].items(), key=lambda item: item[0])
    ]
    if not error_rows:
        error_rows.append({"name": "No errors recorded", "count": None})

    queue = snapshot["queue"]
    recent = snapshot["recent"]
    recent_rate = recent.get("success_rate")
    recent_summary = (
        f"{recent.get('successes', 0)} / {recent.get('window', 0)}"
        if recent_rate is not None
        else "No recent activity"
    )
    recent_percent = f"{recent_rate * 100.0:.1f}%" if recent_rate is not None else "-"

    if templates is not None:
        context = {
            "request": request,
            "title": "Operational Metrics",
            "max_width": "1400px",
            **nav_context("/metrics"),
            "endpoint_rows": endpoint_rows,
            "error_rows": error_rows,
            "queue": queue,
            "recent": recent,
            "recent_summary": recent_summary,
            "recent_percent": recent_percent,
        }
        return templates.TemplateResponse(request, "metrics.html", context)

    endpoints_html = []
    for row in endpoint_rows:
        if row["success"] is None:
            endpoints_html.append("<tr><td colspan='5'>No requests recorded yet</td></tr>")
            continue
        endpoints_html.append(
            "<tr><td>{path}</td><td>{success}</td><td>{failure}</td><td>{avg:.2f} ms</td><td>{rate:.1f}%</td></tr>".format(
                path=html.escape(row["path"]),
                success=row["success"],
                failure=row["failure"],
                avg=row["avg_duration"],
                rate=row["success_percent"],
            )
        )

    errors_html = []
    for row in error_rows:
        if row["count"] is None:
            errors_html.append("<tr><td colspan='2'>No errors recorded</td></tr>")
        else:
            errors_html.append(
                "<tr><td>{name}</td><td>{count}</td></tr>".format(
                    name=html.escape(row["name"]), count=row["count"]
                )
            )

    top_bar_html = fallback_top_bar_html("/metrics")
    html_content = f"""
    <!doctype html>
    <html>
    <head>
      <meta charset="utf-8" />
      <title>Operational Metrics</title>
      <style>
        body {{ font-family: system-ui, sans-serif; padding: 24px; max-width: 1400px; margin: 0 auto; }}
        .top-bar {{ display: flex; align-items: center; justify-content: space-between; gap: 16px; flex-wrap: wrap; margin-bottom: 24px; }}
        .brand {{ font-size: 32px; font-weight: bold; margin: 0; text-decoration: none; display: inline-flex; align-items: center; gap: 2px; color: #000; }}
        .brand:focus, .brand:hover {{ text-decoration: none; color: #000; }}
        .brand .ff {{ color: #28a745; }}
        .brand .api {{ color: #000; }}
        table {{ border-collapse: collapse; width: 100%; margin-bottom: 30px; }}
        th, td {{ border-bottom: 1px solid #eee; padding: 8px 10px; font-size: 14px; }}
        th {{ text-align: left; background: #fafafa; }}
        .main-nav {{ margin: 0; margin-left: auto; display: flex; gap: 12px; flex-wrap: wrap; justify-content: flex-end; }}
        .main-nav a {{ padding: 8px 14px; border-radius: 6px; background: #e2e8f0; color: #6b7280; text-decoration: none; font-weight: 600; transition: background 0.15s ease, color 0.15s ease; }}
        .main-nav a:hover {{ background: #d1d5db; color: #000; }}
        .main-nav a.active {{ background: #28a745; color: #fff; }}
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
{top_bar_html}

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


class TempoRequest(BaseModel):
    input_url: HttpUrl
    output_name: str = "output.mp3"
    tempo: float = Field(1.0, ge=0.5, le=2.0)

    @field_validator("output_name")
    @classmethod
    def validate_output_name(cls, value: str) -> str:
        cleaned = value.strip()
        if not cleaned:
            raise ValueError("output_name cannot be empty")
        if any(sep in cleaned for sep in ("/", "\\")):
            raise ValueError("output_name must be a file name without directories")
        if not re.search(r"\.[A-Za-z0-9]+$", cleaned):
            raise ValueError("output_name must include a file extension")
        return cleaned


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


class FFmpegInput(BaseModel):
    file_path: HttpUrl
    options: List[str] = Field(default_factory=list)

    @field_validator("file_path", mode="before")
    @classmethod
    def validate_file_path(cls, value):
        if value is None:
            raise ValueError("file_path is required")
        value_str = str(value)
        if not value_str.startswith(("http://", "https://")):
            raise ValueError("Only http:// and https:// URLs are supported")
        return value


class FFmpegOutput(BaseModel):
    file: str
    options: List[str] = Field(default_factory=list)
    maps: List[str] = Field(default_factory=list)

    @field_validator("file")
    @classmethod
    def validate_file_name(cls, value: str) -> str:
        cleaned = value.strip()
        if not cleaned:
            raise ValueError("output file name cannot be empty")
        if any(sep in cleaned for sep in ("/", "\\")):
            raise ValueError("file name must not include directories")
        if not re.search(r"\.[A-Za-z0-9]+$", cleaned):
            raise ValueError("file name must include a file extension")
        return cleaned


class FFmpegTask(BaseModel):
    inputs: List[FFmpegInput]
    filter_complex: str
    outputs: List[FFmpegOutput]

    @field_validator("inputs")
    @classmethod
    def validate_inputs(cls, value):
        if not value or len(value) == 0:
            raise ValueError("At least one input is required")
        if len(value) > 50:
            raise ValueError("Too many inputs (max 50)")
        return value

    @field_validator("outputs")
    @classmethod
    def validate_outputs(cls, value):
        if not value or len(value) == 0:
            raise ValueError("At least one output is required")
        if len(value) > 10:
            raise ValueError("Too many outputs (max 10)")
        return value


class FFmpegJobRequest(BaseModel):
    task: FFmpegTask
    headers: Optional[Dict[str, str]] = None


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
    loop_bgm: bool = False
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

    @field_validator("video_url", "audio_url", "bgm_url", mode="before")
    @classmethod
    def validate_url_scheme(cls, value):
        if value is None:
            return value
        value_str = str(value)
        if not value_str.startswith(("http://", "https://")):
            raise ValueError("Only http:// and https:// URLs are supported")
        return value

    @field_validator("webhook_url", mode="before")
    @classmethod
    def validate_webhook_url_ssrf(cls, value):
        """Validate webhook URL to prevent SSRF attacks."""
        if value is None:
            return value

        value_str = str(value)

        # Check scheme first
        if not value_str.startswith(("http://", "https://")):
            raise ValueError("Webhook URL must use http:// or https://")

        # Parse URL to check components
        try:
            parsed = urlparse(value_str.lower())
        except Exception:
            raise ValueError("Invalid webhook URL format")

        # Extract hostname/IP
        hostname = parsed.hostname
        if not hostname:
            raise ValueError("Webhook URL must have a valid hostname")

        # Block private/internal IP addresses to prevent SSRF
        try:
            # Try to parse as IP address
            ip = ipaddress.ip_address(hostname)

            # Block private, loopback, link-local, and multicast addresses
            if ip.is_private or ip.is_loopback or ip.is_link_local or ip.is_multicast:
                raise ValueError(
                    "Webhook URL cannot target private, loopback, or internal IP addresses"
                )

            # Specifically block cloud metadata service
            if str(ip) == "169.254.169.254":
                raise ValueError("Webhook URL cannot target cloud metadata services")

        except ValueError as ve:
            # If it's our security error, re-raise it
            if "cannot target" in str(ve):
                raise
            # Otherwise, it's not an IP address (it's a hostname), which is OK
            # Note: Hostnames could still resolve to private IPs, but preventing
            # that requires DNS lookups which are slow and unreliable
            pass

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
        # Don't set a default - let the video play its full length
        if self.duration is not None and self.duration_ms is not None:
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
# NOTE: Only forward safe, non-sensitive headers to external URLs
# DO NOT include authentication headers (cookie, authorization, API keys)
# as this would leak credentials to third-party services (SECURITY RISK)
ALLOWED_FORWARD_HEADERS_LOWER = {
    "ngrok-skip-browser-warning",  # Safe: Just skips browser warning page
    "user-agent",  # Safe: Client identification
    "accept",  # Safe: Content negotiation
    "accept-language",  # Safe: Language preference
}


def _escape_ffmpeg_concat_path(path: Path) -> str:
    """Escape path for FFmpeg concat demuxer file directive.

    The concat demuxer parses lines like: file 'path'
    We need to escape:
    - Single quotes (used to delimit the path)
    - Newlines (could inject new directives)
    - Other special chars that could break parsing
    """
    # Convert to POSIX path
    path_str = path.as_posix()

    # Check for dangerous characters that could inject directives
    if '\n' in path_str or '\r' in path_str:
        raise ValueError(f"Path contains newline characters: {path_str}")

    # FFmpeg concat format uses single quotes, so escape them
    # The correct escaping is to replace ' with '\''
    # But we also need to ensure no injection is possible
    escaped = path_str.replace("'", "'\\''")

    return escaped


def _probe_cache_key(url: str, params: Dict[str, Any]) -> str:
    cache_data = f"{url}:{json.dumps(params, sort_keys=True)}"
    return hashlib.sha256(cache_data.encode("utf-8")).hexdigest()[:16]


def _make_async_client() -> httpx.AsyncClient:
    return httpx.AsyncClient(follow_redirects=True)


# ---------------------------------------------------------------------------
# Media URL helpers
# ---------------------------------------------------------------------------


def _normalize_media_url(url: str) -> str:
    """Normalize media URLs to avoid malformed duplicates from the UI proxy."""

    normalized = url.strip()
    if not normalized:
        return normalized

    proxy_base = "http://10.120.2.5:3000"
    localhost_base = "http://localhost:3000"
    double_prefix = f"{proxy_base}/{localhost_base}"
    double_proxy = f"{proxy_base}/{proxy_base}"

    def _bounded_replace(value: str, prefix: str, replacement: str, label: str) -> str:
        if replacement.startswith(prefix):
            raise ValueError(
                f"Replacement '{replacement}' cannot start with prefix '{prefix}'"
            )
        max_iterations = 10
        iterations = 0
        while value.startswith(prefix):
            value = replacement + value[len(prefix) :]
            iterations += 1
            if iterations >= max_iterations:
                logger.warning("URL normalization hit max iterations (%s): %s", label, url)
                break
        return value

    normalized = _bounded_replace(normalized, double_proxy, proxy_base, "double-proxy")

    normalized = _bounded_replace(normalized, double_prefix, proxy_base, "proxy-prefix")

    if normalized.startswith(localhost_base):
        normalized = proxy_base + normalized[len(localhost_base) :]

    proxy_http_prefix = f"{proxy_base}/http://"
    normalized = _bounded_replace(
        normalized,
        proxy_http_prefix,
        "http://",
        "proxy-http",
    )

    if normalized.startswith("http://"):
        rest = normalized[len("http://") :]
        max_repeats = 10
        repeats = 0
        while rest.startswith("http://"):
            rest = rest[len("http://") :]
            repeats += 1
            if repeats >= max_repeats:
                logger.warning("URL normalization trimmed repeated scheme %s times: %s", repeats, url)
                break
        normalized = "http://" + rest

    parsed = urlparse(normalized)
    if not parsed.scheme:
        normalized = f"{proxy_base}/{normalized.lstrip('/')}"

    return normalized


async def _download_to(
    url: str,
    dest: Path,
    headers: Optional[Dict[str, str]] = None,
    max_retries: int = 3,
    chunk_size: int = 1024 * 1024,
    client: Optional[httpx.AsyncClient] = None,
) -> None:
    """Download file with retry, resume, and progress tracking."""

    normalized_url = _normalize_media_url(url)
    if normalized_url != url:
        logger.debug("Normalized download URL from %s to %s", url, normalized_url)
    url = normalized_url

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
                existing_size = 0
                had_existing_file = False
                try:
                    stat_result = dest.stat()
                except FileNotFoundError:
                    pass
                else:
                    existing_size = stat_result.st_size
                    had_existing_file = True

                if not can_resume and had_existing_file:
                    try:
                        dest.unlink()
                    except FileNotFoundError:
                        pass
                    except Exception as cleanup_exc:
                        logger.warning("Failed to reset download %s: %s", dest, cleanup_exc)
                        _flush_logs()
                    existing_size = 0

                downloaded = existing_size
                wrote_data = False
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
                            downloaded = 0
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

                    last_log = downloaded
                    space_check_interval = 64 * 1024 * 1024
                    last_space_check = downloaded
                    periodic_required_mb = required_mb

                    with dest.open(mode) as f:
                        async for chunk in r.aiter_bytes(chunk_size):
                            if chunk:
                                chunk_len = len(chunk)
                                f.write(chunk)
                                downloaded += chunk_len
                                wrote_data = True

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

                                if downloaded - last_space_check >= space_check_interval:
                                    if total_size:
                                        remaining = total_size - downloaded
                                        if remaining < 0:
                                            remaining = 0
                                        remaining_mb = (remaining + 1024 * 1024 - 1) // (
                                            1024 * 1024
                                        )
                                        new_required = max(
                                            MIN_FREE_SPACE_MB,
                                            MIN_FREE_SPACE_MB + remaining_mb,
                                        )
                                        periodic_required_mb = max(
                                            periodic_required_mb,
                                            new_required,
                                        )
                                    else:
                                        periodic_required_mb = MIN_FREE_SPACE_MB
                                    try:
                                        check_disk_space(
                                            dest.parent, required_mb=periodic_required_mb
                                        )
                                    except HTTPException:
                                        raise
                                    last_space_check = downloaded

                    if total_size and downloaded != total_size:
                        raise Exception(
                            f"Download incomplete: got {downloaded} bytes, expected {total_size}"
                        )

                    logger.info(
                        "Download complete: %.1fMB - %s", downloaded / 1024 / 1024, url
                    )
                    return

            except HTTPException:
                remove_partial = not use_resume or wrote_data
                if remove_partial and dest.exists():
                    try:
                        dest.unlink()
                    except Exception as cleanup_exc:
                        logger.warning(
                            "Failed removing incomplete download %s: %s",
                            dest,
                            cleanup_exc,
                        )
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

        codec, codec_args = get_video_encoder()

        cmd = [
            "ffmpeg",
            "-y",
            "-loop",
            "1",
            "-t",
            str(duration),
            "-i",
            str(in_path),
            "-c:v",
            codec,
        ] + codec_args

        if codec == "libx264":
            cmd += ["-tune", "stillimage"]

        cmd += [
            "-pix_fmt",
            "yuv420p",
            "-movflags",
            "+faststart",
            str(out_path),
        ]
        log = work / "ffmpeg.log"
        with log.open("w", encoding="utf-8", errors="ignore") as lf:
            code = await run_ffmpeg_with_timeout(cmd, lf)
        save_log(log, "image-to-mp4")
        if code != 0 or not out_path.exists():
            logger.error(f"image-to-mp4-loop failed for {file.filename}")
            _flush_logs()
            return JSONResponse(status_code=500, content={"error": "ffmpeg_failed", "log": log.read_text()})
        pub = await publish_file(out_path, ".mp4", duration_ms=duration * 1000)
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
    duration_ms: Optional[int] = Query(None, ge=1, le=3600000),
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

        inputs = ["-i", str(v_path)]
        if has_audio:
            inputs += ["-i", str(a_path)]
        if has_bgm:
            inputs += ["-i", str(b_path)]

        maps = ["-map", "0:v:0"]
        decoder_args = get_video_decoder(v_path)
        cmd = ["ffmpeg", "-y"] + decoder_args + inputs

        if duration_ms is not None:
            dur_s = f"{duration_ms/1000:.3f}"
            cmd += ["-t", dur_s]

        encode_args = build_encode_args(width, height, fps, decoder_args=decoder_args)
        cmd += encode_args
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
            code = await run_ffmpeg_with_timeout(cmd, lf)
        save_log(log, "compose-binaries")
        if code != 0 or not out_path.exists():
            logger.error("compose-from-binaries failed")
            _flush_logs()
            return JSONResponse(status_code=500, content={"error": "ffmpeg_failed", "cmd": cmd, "log": log.read_text()})

        pub = await publish_file(out_path, ".mp4", duration_ms=duration_ms)
        logger.info(f"compose-from-binaries completed: {pub['rel']}")
        if as_json:
            return {"ok": True, "file_url": pub["url"], "path": pub["dst"]}
        resp = FileResponse(pub["dst"], media_type="video/mp4", filename=os.path.basename(pub["dst"]))
        resp.headers["X-File-URL"] = pub["url"]
        return resp


@app.post("/video/concat-from-urls")
async def video_concat_from_urls(job: ConcatJob, as_json: bool = False):
    if not job.clips:
        raise HTTPException(status_code=422, detail="At least one video clip is required")

    logger.info(
        "Starting concat: %d clips, %dx%d@%dfps",
        len(job.clips),
        job.width,
        job.height,
        job.fps,
    )

    cleanup_old_jobs()
    job_id = str(uuid4())
    start_time = time.time()

    with JOBS_LOCK:
        JOBS[job_id] = {
            "status": "processing",
            "progress": 0,
            "message": "Starting concat",
            "history": [
                {
                    "timestamp": start_time,
                    "progress": 0,
                    "message": "Starting concat",
                }
            ],
            "created": start_time,
            "updated": start_time,
            "started": start_time,
        }

    reporter = JobProgressReporter(job_id)
    process_handle = JobProcessHandle()
    with JOB_PROCESSES_LOCK:
        JOB_PROCESSES[job_id] = process_handle

    log_rel_path: Optional[str] = None

    try:
        reporter.update(5, "Preparing concat job")
        check_disk_space(WORK_DIR)

        max_attempts = 2
        last_error: Optional[HTTPException] = None
        result_response: Optional[Union[FileResponse, Dict[str, Any]]] = None
        result_payload: Optional[Dict[str, Any]] = None

        for attempt in range(max_attempts):
            use_gpu = attempt == 0
            try:
                (
                    result_response,
                    result_payload,
                    log_rel_path,
                ) = await _concat_impl_with_tracking(
                    job,
                    as_json,
                    force_cpu=not use_gpu,
                    reporter=reporter,
                    job_id=job_id,
                    process_handle=process_handle,
                )
                break
            except HTTPException as exc:
                last_error = exc
                detail = exc.detail if isinstance(exc.detail, dict) else {}
                if log_rel_path is None and isinstance(detail, dict):
                    maybe_path = detail.get("log_path")
                    if isinstance(maybe_path, str):
                        log_rel_path = maybe_path
                if use_gpu:
                    log_text = ""
                    if isinstance(detail, dict):
                        log_text = str(detail.get("log", ""))
                    if log_text and any(err in log_text for err in GPU_FILTER_ERROR_PATTERNS):
                        reporter.update(45, "GPU filters failed - retrying on CPU")
                        logger.warning("GPU filter failed, retrying with CPU filters")
                        continue
                raise

        if result_response is None:
            if last_error is not None:
                raise last_error
            raise HTTPException(status_code=500, detail="Unknown concat failure")

        duration_ms = (time.time() - start_time) * 1000
        now_ts = time.time()
        progress_snapshot = _drain_job_progress(job_id)
        history_updates: List[Dict[str, Any]] = []
        if progress_snapshot is not None:
            history_updates.extend(progress_snapshot.get("history", []))
        history_updates.append(
            {
                "timestamp": now_ts,
                "progress": 100,
                "message": "Completed",
            }
        )

        with JOBS_LOCK:
            existing = JOBS.get(job_id, {})
            history = list(existing.get("history", []))
        history.extend(history_updates)
        if len(history) > JOB_HISTORY_LIMIT:
            history = history[-JOB_HISTORY_LIMIT:]

        with JobTransaction(job_id) as tx:
            tx.set(
                status="finished",
                progress=100,
                message="Completed",
                updated=now_ts,
                duration_ms=duration_ms,
                history=history,
                result=result_payload,
                log_path=log_rel_path,
            )

        logger.info("Concat job %s completed in %.1fs", job_id, duration_ms / 1000)

        if isinstance(result_response, FileResponse):
            result_response.headers["X-Job-ID"] = job_id
            result_response.headers["X-Job-Status-URL"] = f"/jobs/{job_id}"
        elif isinstance(result_response, dict):
            enriched = dict(result_response)
            enriched["job_id"] = job_id
            enriched["job_status_url"] = f"/jobs/{job_id}"
            result_response = enriched

        return result_response

    except HTTPException as exc:
        detail = exc.detail if isinstance(exc.detail, (dict, str)) else str(exc)
        if isinstance(exc.detail, dict):
            maybe_path = exc.detail.get("log_path")
            if isinstance(maybe_path, str):
                log_rel_path = log_rel_path or maybe_path
        _finalize_sync_job_failure(
            job_id,
            start_time,
            detail,
            log_rel_path,
            failure_message="Failed",
        )
        raise
    except Exception as exc:
        _finalize_sync_job_failure(
            job_id,
            start_time,
            str(exc),
            log_rel_path,
            failure_message="Failed",
        )
        raise
    finally:
        with JOB_PROCESSES_LOCK:
            JOB_PROCESSES.pop(job_id, None)


@app.post("/video/concat-from-urls/async")
async def video_concat_from_urls_async(job: ConcatJob):
    """Queue video concatenation as a background job."""

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
    asyncio.create_task(_process_concat_job_async(job_id, job_copy))

    return {"job_id": job_id, "status_url": f"/jobs/{job_id}"}


async def _process_concat_job_async(job_id: str, job: ConcatJob):
    """Background worker for async concat jobs."""

    async with safe_lock(JOBS_LOCK, timeout=5.0, operation="jobs_start"):
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

    with JOB_PROGRESS_LOCK:
        JOB_PROGRESS.pop(job_id, None)

    reporter = JobProgressReporter(job_id)
    start_wall = time.time()
    start_perf = time.perf_counter()
    struct_logger.info("job_started", job_id=job_id, job_type="concat")

    process_handle = JobProcessHandle()
    with JOB_PROCESSES_LOCK:
        JOB_PROCESSES[job_id] = process_handle

    try:
        _result_response, result_payload, log_rel_path = await _concat_impl_with_tracking(
            job,
            as_json=True,
            force_cpu=False,
            reporter=reporter,
            job_id=job_id,
            process_handle=process_handle,
        )

        duration_ms = (time.perf_counter() - start_perf) * 1000
        now_ts = time.time()
        progress_snapshot = _drain_job_progress(job_id)

        history_updates: List[Dict[str, Any]] = []
        if progress_snapshot is not None:
            history_updates.extend(progress_snapshot.get("history", []))
        history_updates.append(
            {
                "timestamp": now_ts,
                "progress": 100,
                "message": "Completed",
            }
        )

        with JOBS_LOCK:
            existing = JOBS.get(job_id, {})
            history = list(existing.get("history", []))

        history.extend(history_updates)
        if len(history) > JOB_HISTORY_LIMIT:
            history = history[-JOB_HISTORY_LIMIT:]

        with JobTransaction(job_id) as tx:
            tx.set(
                status="finished",
                progress=100,
                message="Completed",
                updated=now_ts,
                duration_ms=duration_ms,
                history=history,
                result=result_payload,
                log_path=log_rel_path,
            )

        struct_logger.info("job_completed", job_id=job_id, job_type="concat", duration_ms=duration_ms)

    except Exception as exc:
        logger.exception("Async concat job %s failed", job_id)
        _finalize_sync_job_failure(
            job_id,
            start_wall,
            str(exc),
            None,
            failure_message="Failed",
        )
        struct_logger.error("job_failed", job_id=job_id, job_type="concat", error=str(exc))
    finally:
        with JOB_PROCESSES_LOCK:
            JOB_PROCESSES.pop(job_id, None)


async def _process_rendi_job_async(job_id: str, job: RendiJob) -> None:
    """Background worker for async rendi jobs."""

    async with safe_lock(JOBS_LOCK, timeout=5.0, operation="jobs_start"):
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

    with JOB_PROGRESS_LOCK:
        JOB_PROGRESS.pop(job_id, None)

    reporter = JobProgressReporter(job_id)
    start_wall = time.time()
    start_perf = time.perf_counter()
    struct_logger.info("job_started", job_id=job_id, job_type="rendi_command")

    process_handle = JobProcessHandle()
    with JOB_PROCESSES_LOCK:
        JOB_PROCESSES[job_id] = process_handle

    log_rel_path: Optional[str] = None
    published: Dict[str, str] = {}
    result_payload: Optional[Dict[str, Any]] = None

    def _record_log_path(saved: Optional[Path]) -> None:
        nonlocal log_rel_path
        if saved is None:
            return
        try:
            log_rel_path = str(saved.relative_to(LOGS_DIR))
        except Exception:
            log_rel_path = str(saved)

    try:
        check_disk_space(WORK_DIR)
        reporter.update(10, "Preparing workspace")

        with TemporaryDirectory(prefix="rendi_", dir=str(WORK_DIR)) as workdir:
            work = Path(workdir)
            resolved: Dict[str, str] = {}

            reporter.update(20, "Downloading input files")
            total_inputs = max(len(job.input_files), 1)
            for idx, (key, url) in enumerate(job.input_files.items()):
                progress = 20 + int(((idx + 1) / total_inputs) * 30)
                reporter.update(progress, f"Downloading input {idx + 1}/{total_inputs}")
                p = work / f"{key}"
                await _download_to(str(url), p, None)
                resolved[key] = str(p)

            out_paths: Dict[str, Path] = {key: work / name for key, name in job.output_files.items()}

            reporter.update(50, "Building FFmpeg command")
            cmd_text = job.ffmpeg_command
            for k, p in resolved.items():
                cmd_text = cmd_text.replace("{{" + k + "}}", p)
            for k, p in out_paths.items():
                cmd_text = cmd_text.replace("{{" + k + "}}", str(p))

            cmd_lower = cmd_text.lower()
            dangerous_patterns = ["-f lavfi", "-i /dev/", "-i file:", "-i concat:"]
            for pattern in dangerous_patterns:
                if pattern in cmd_lower:
                    raise HTTPException(
                        status_code=400,
                        detail={"error": "forbidden_pattern", "pattern": pattern},
                    )

            if REQUIRE_DURATION_LIMIT and "-t" not in cmd_lower and "-frames" not in cmd_lower:
                raise HTTPException(
                    status_code=400,
                    detail={"error": "missing_limit", "detail": "Command must include -t or -frames"},
                )

            try:
                args = shlex.split(cmd_text)
            except Exception as exc:
                raise HTTPException(
                    status_code=400,
                    detail={"error": "invalid_command", "msg": str(exc)},
                ) from exc

            log = work / "ffmpeg.log"
            run_args = ["ffmpeg"] + args if args and args[0] != "ffmpeg" else args

            reporter.update(60, "Executing FFmpeg")
            with log.open("w", encoding="utf-8", errors="ignore") as lf:
                code = await run_ffmpeg_with_timeout(
                    run_args,
                    lf,
                    process_handle=process_handle,
                )

            _record_log_path(save_log(log, "rendi-command-async"))

            if code != 0:
                logger.error("run-ffmpeg-command async failed")
                _flush_logs()
                log_text = ""
                if log.exists():
                    try:
                        log_text = log.read_text(encoding="utf-8", errors="ignore")
                    except Exception:
                        log_text = ""
                detail: Dict[str, Any] = {"error": "ffmpeg_failed", "cmd": run_args, "log": log_text}
                if log_rel_path:
                    detail["log_path"] = log_rel_path
                raise HTTPException(status_code=500, detail=detail)

            reporter.update(80, "Publishing outputs")
            for key, path_obj in out_paths.items():
                if path_obj.exists():
                    pub = await publish_file(path_obj, Path(path_obj).suffix or ".bin")
                    published[key] = pub["url"]

            if not published:
                log_text = ""
                if log.exists():
                    try:
                        log_text = log.read_text(encoding="utf-8", errors="ignore")
                    except Exception:
                        log_text = ""
                detail = {"error": "no_outputs_found", "log": log_text}
                if log_rel_path:
                    detail["log_path"] = log_rel_path
                raise HTTPException(status_code=500, detail=detail)

            reporter.update(95, "Complete")
            result_payload = {"outputs": dict(published)}

            duration_ms = (time.perf_counter() - start_perf) * 1000
            now_ts = time.time()
            progress_snapshot = _drain_job_progress(job_id)

            history_updates: List[Dict[str, Any]] = []
            if progress_snapshot is not None:
                history_updates.extend(progress_snapshot.get("history", []))
            history_updates.append(
                {
                    "timestamp": now_ts,
                    "progress": 100,
                    "message": "Completed",
                }
            )

            with JOBS_LOCK:
                existing = JOBS.get(job_id, {})
                history = list(existing.get("history", []))

            history.extend(history_updates)
            if len(history) > JOB_HISTORY_LIMIT:
                history = history[-JOB_HISTORY_LIMIT:]

            update_payload: Dict[str, Any] = {
                "status": "finished",
                "progress": 100,
                "message": "Completed",
                "updated": now_ts,
                "duration_ms": duration_ms,
                "history": history,
                "error": None,
                "result": result_payload,
            }
            if log_rel_path is not None:
                update_payload["log_path"] = log_rel_path

            with JobTransaction(job_id) as tx:
                tx.set(**update_payload)

            struct_logger.info(
                "job_completed",
                job_id=job_id,
                job_type="rendi_command",
                duration_ms=duration_ms,
            )

    except Exception as exc:
        logger.exception("Async rendi job %s failed", job_id)
        _finalize_sync_job_failure(
            job_id,
            start_wall,
            str(exc),
            log_rel_path,
            failure_message="Failed",
        )
        struct_logger.error(
            "job_failed",
            job_id=job_id,
            job_type="rendi_command",
            error=str(exc),
        )
    finally:
        with JOB_PROCESSES_LOCK:
            JOB_PROCESSES.pop(job_id, None)

async def _concat_impl_with_tracking(
    job: ConcatJob,
    as_json: bool,
    force_cpu: bool = False,
    *,
    reporter: Optional[JobProgressReporter] = None,
    job_id: Optional[str] = None,
    process_handle: Optional[JobProcessHandle] = None,
) -> Tuple[Union[FileResponse, Dict[str, Any]], Dict[str, Any], Optional[str]]:
    if force_cpu:
        logger.info("CPU fallback enabled for concat normalization")

    log_rel_path: Optional[str] = None

    def _record_log_path(saved: Optional[Path]) -> None:
        nonlocal log_rel_path
        if saved is None:
            return
        try:
            log_rel_path = str(saved.relative_to(LOGS_DIR))
        except Exception:
            log_rel_path = str(saved)

    if reporter:
        reporter.update(10, "Downloading clips")

    with TemporaryDirectory(prefix="concat_", dir=str(WORK_DIR)) as workdir:
        work = Path(workdir)

        # Step 1: Download all clips
        logger.info("Downloading %d clips...", len(job.clips))
        downloaded = []
        total_clips = max(len(job.clips), 1)
        for i, url in enumerate(job.clips):
            raw = work / f"in_{i:03d}.mp4"
            await _download_to(str(url), raw, None)
            downloaded.append(raw)
            logger.info("Downloaded clip %d/%d", i + 1, len(job.clips))
            if reporter:
                progress = 10 + int(((i + 1) / total_clips) * 25)
                reporter.update(progress, f"Downloading clip {i + 1}/{total_clips}")

        # Step 2: Probe clips to check compatibility
        logger.info("Probing clips for compatibility...")
        if reporter:
            reporter.update(35, "Probing clips for compatibility")

        def probe_clip(clip_path: Path) -> Optional[Dict[str, Any]]:
            """Probe a video clip and return its stream info."""
            try:
                probe_cmd = [
                    "ffprobe", "-v", "error",
                    "-select_streams", "v:0,a:0",
                    "-show_entries", "stream=codec_name,width,height,avg_frame_rate,pix_fmt,codec_type",
                    "-of", "json",
                    str(clip_path)
                ]
                result = subprocess.run(probe_cmd, capture_output=True, text=True, timeout=10)
                if result.returncode == 0:
                    data = json.loads(result.stdout)
                    streams = data.get("streams", [])
                    video = next((s for s in streams if s.get("codec_type") == "video"), None)
                    audio = next((s for s in streams if s.get("codec_type") == "audio"), None)
                    return {"video": video, "audio": audio}
            except Exception as exc:
                logger.warning("Failed to probe %s: %s", clip_path, exc)
            return None

        def parse_fps(fps_str: Optional[str]) -> Optional[float]:
            if not fps_str or fps_str == "0/0":
                return None
            try:
                num, den = fps_str.split("/")
                return float(num) / float(den)
            except Exception:
                return None

        # Probe all clips
        clip_info = [probe_clip(p) for p in downloaded]

        # Check if all clips are compatible for stream copy
        all_compatible = True
        baseline = clip_info[0]

        if not baseline:
            logger.warning("Could not probe first clip, will re-encode all")
            all_compatible = False
        else:
            baseline_video = baseline.get("video")
            baseline_audio = baseline.get("audio")

            if not baseline_video:
                logger.warning("No video stream info in first clip, will re-encode all")
                all_compatible = False
            else:
                baseline_codec = baseline_video.get("codec_name")
                baseline_width = baseline_video.get("width")
                baseline_height = baseline_video.get("height")
                baseline_pix_fmt = baseline_video.get("pix_fmt")
                baseline_fps = parse_fps(baseline_video.get("avg_frame_rate"))
                baseline_audio_codec = baseline_audio.get("codec_name") if baseline_audio else None

                # Check if requested output matches baseline
                if (
                    baseline_width != job.width
                    or baseline_height != job.height
                    or (baseline_fps is not None and abs(baseline_fps - job.fps) > 0.1)
                ):
                    logger.info("Output parameters differ from source, will re-encode")
                    all_compatible = False
                else:
                    # Check all clips against baseline
                    for i, info in enumerate(clip_info[1:], 1):
                        if not info:
                            logger.warning("Could not probe clip %d, will re-encode all", i)
                            all_compatible = False
                            break

                        video = info.get("video") if info else None
                        audio = info.get("audio") if info else None

                        if not video:
                            logger.info("Clip %d missing video stream info, will re-encode all", i)
                            all_compatible = False
                            break

                        clip_fps = parse_fps(video.get("avg_frame_rate"))

                        if (
                            video.get("codec_name") != baseline_codec
                            or video.get("width") != baseline_width
                            or video.get("height") != baseline_height
                            or video.get("pix_fmt") != baseline_pix_fmt
                            or abs((clip_fps or 0) - (baseline_fps or 0)) > 0.1
                        ):
                            logger.info(
                                "Clip %d video incompatible, will re-encode all (codec=%s vs %s, size=%dx%d vs %dx%d)",
                                i,
                                video.get("codec_name"),
                                baseline_codec,
                                video.get("width"),
                                video.get("height"),
                                baseline_width,
                                baseline_height,
                            )
                            all_compatible = False
                            break

                        if baseline_audio_codec and audio:
                            if audio.get("codec_name") != baseline_audio_codec:
                                logger.info(
                                    "Clip %d audio codec differs (%s vs %s), will re-encode all",
                                    i,
                                    audio.get("codec_name"),
                                    baseline_audio_codec,
                                )
                                all_compatible = False
                                break
                        elif bool(baseline_audio) != bool(audio):
                            logger.info("Clip %d audio stream mismatch, will re-encode all", i)
                            all_compatible = False
                            break

        out_path = work / "output.mp4"

        # Step 3: Choose processing path
        if all_compatible:
            # ⚡ FAST PATH: Stream copy (no re-encoding!)
            logger.info("✓ All clips compatible - using FAST stream copy mode")
            logger.info("GPU acceleration not needed (no encoding performed)")
            if reporter:
                reporter.update(40, "Using fast stream copy mode")

            listfile = work / "list.txt"
            with listfile.open("w", encoding="utf-8") as f:
                for p in downloaded:
                    f.write(f"file '{_escape_ffmpeg_concat_path(p)}'\n")

            cmd = [
                "ffmpeg", "-y",
                "-f", "concat",
                "-safe", "0",
                "-i", str(listfile),
                "-c", "copy",  # ← Stream copy - instant!
                "-movflags", "+faststart",
                str(out_path)
            ]

            log = work / "concat_fast.log"
            with log.open("w", encoding="utf-8", errors="ignore") as lf:
                code = await run_ffmpeg_with_timeout(
                    cmd,
                    lf,
                    process_handle=process_handle,
                )
            _record_log_path(save_log(log, "concat-fast"))

            if code != 0 or not out_path.exists():
                logger.error("Fast concat failed, falling back to re-encode")
                all_compatible = False  # Fall through to slow path
                if out_path.exists():
                    try:
                        out_path.unlink()
                    except Exception as exc:
                        logger.warning("Failed to remove incomplete fast path output: %s", exc)
            else:
                if reporter:
                    reporter.update(80, "Finalizing output")

        if not all_compatible:
            # 🐌 SLOW PATH: Normalize and re-encode incompatible clips
            logger.info("⚠ Clips incompatible or fast path failed - using SLOW re-encode mode")
            if reporter:
                reporter.update(40, "Normalizing incompatible clips")

            norm = []
            total_norm = max(len(downloaded), 1)
            for i, clip_path in enumerate(downloaded):
                logger.info("Normalizing clip %d/%d...", i + 1, len(downloaded))
                if reporter:
                    progress = 40 + int((i / total_norm) * 40)
                    reporter.update(progress, f"Normalizing clip {i + 1}/{total_norm}")

                if not clip_path.exists():
                    raise HTTPException(
                        status_code=500,
                        detail=f"Clip {i} disappeared: {clip_path}",
                    )

                file_size = clip_path.stat().st_size
                if file_size == 0:
                    raise HTTPException(
                        status_code=500,
                        detail=f"Clip {i} is empty (0 bytes): {clip_path}",
                    )

                logger.info("Clip %d size: %.1f MB", i, file_size / (1024 * 1024))

                out = work / f"norm_{i:03d}.mp4"

                if force_cpu:
                    decoder_args: List[str] = []
                else:
                    decoder_args = get_video_decoder(clip_path)

                if decoder_args and not force_cpu:
                    test_cmd = ["ffprobe"] + decoder_args + ["-i", str(clip_path), "-v", "error"]
                    try:
                        test_result = subprocess.run(
                            test_cmd,
                            capture_output=True,
                            timeout=10,
                            check=False,
                        )
                        if test_result.returncode != 0:
                            logger.warning("GPU decoder test failed for clip %d, using CPU", i)
                            decoder_args = []
                    except Exception as probe_exc:
                        logger.warning(
                            "GPU decoder test exception for clip %d: %s, using CPU",
                            i,
                            probe_exc,
                        )
                        decoder_args = []

                encode_args = build_encode_args(
                    job.width,
                    job.height,
                    job.fps,
                    decoder_args=decoder_args,
                    prefer_gpu_filters=not force_cpu,
                    force_cpu_encoder=force_cpu,
                )

                cmd = [
                    "ffmpeg", "-y",
                ] + decoder_args + [
                    "-i", str(clip_path),
                ] + encode_args + [
                    "-c:a", "aac",
                    "-b:a", "128k",
                    "-ar", "48000",
                    "-movflags", "+faststart",
                    str(out),
                ]

                log = work / f"norm_{i:03d}.log"

                logger.info("Normalizing clip %d: %s", i, clip_path.name)
                logger.info("Output: %s", out.name)
                command_preview = " ".join(cmd[:10]) + "..." if len(cmd) > 10 else " ".join(cmd)
                logger.info("Command: %s", command_preview)
                logger.info("GPU decoder args: %s", decoder_args if decoder_args else "None (CPU)")

                with log.open("w", encoding="utf-8", errors="ignore") as lf:
                    lf.write(f"# FFmpeg command:\n# {' '.join(cmd)}\n\n")
                    lf.flush()
                    code = await run_ffmpeg_with_timeout(
                        cmd,
                        lf,
                        allow_gpu_fallback=not force_cpu,
                        process_handle=process_handle,
                    )

                if code == -999:
                    logger.info("Retrying clip %d with CPU filters", i)
                    encode_args_cpu = build_encode_args(
                        job.width,
                        job.height,
                        job.fps,
                        decoder_args=[],
                        prefer_gpu_filters=False,
                        force_cpu_encoder=True,
                    )
                    cmd = [
                        "ffmpeg", "-y",
                        "-i", str(clip_path),
                    ] + encode_args_cpu + [
                        "-c:a", "aac",
                        "-b:a", "128k",
                        "-ar", "48000",
                        "-movflags", "+faststart",
                        str(out),
                    ]

                    logger.info("Retry command (CPU): %s", " ".join(cmd[:10]) + "..." if len(cmd) > 10 else " ".join(cmd))

                    with log.open("w", encoding="utf-8", errors="ignore") as lf:
                        lf.write(f"# FFmpeg command:\n# {' '.join(cmd)}\n\n")
                        lf.flush()
                        code = await run_ffmpeg_with_timeout(
                            cmd,
                            lf,
                            allow_gpu_fallback=False,
                            process_handle=process_handle,
                        )

                _record_log_path(save_log(log, f"concat-norm-{i}"))

                if code != 0 or not out.exists():
                    logger.error("Failed to normalize clip %d", i)

                    log_content = "Log file not found"
                    if log.exists():
                        try:
                            log_content = log.read_text(encoding="utf-8", errors="ignore")
                            if not log_content.strip():
                                log_content = "Log file is empty - FFmpeg may have crashed immediately"
                        except Exception as read_exc:
                            log_content = f"Failed to read log: {read_exc}"

                    logger.error("Normalization failed for clip %d/%d", i, len(downloaded))
                    logger.error("FFmpeg command was: %s", " ".join(cmd))
                    logger.error("FFmpeg log content:\n%s", log_content[:1000])

                    for partial in norm:
                        try:
                            partial.unlink()
                        except Exception:
                            pass
                    if out.exists():
                        try:
                            out.unlink()
                        except Exception:
                            pass
                    _flush_logs()
                    detail = {
                        "error": "ffmpeg_failed_on_clip",
                        "clip": str(job.clips[i]),
                        "clip_index": i,
                        "log": log_content,
                        "command": " ".join(cmd),
                        "exit_code": code,
                    }
                    if log_rel_path:
                        detail["log_path"] = log_rel_path
                    raise HTTPException(status_code=500, detail=detail)
                norm.append(out)
                if reporter:
                    progress = 40 + int(((i + 1) / total_norm) * 40)
                    reporter.update(progress, f"Normalized clip {i + 1}/{total_norm}")

            # Concatenate normalized clips
            logger.info("Concatenating %d normalized clips...", len(norm))
            if reporter:
                reporter.update(85, "Concatenating normalized clips")
            listfile = work / "list.txt"
            with listfile.open("w", encoding="utf-8") as f:
                for p in norm:
                    f.write(f"file '{_escape_ffmpeg_concat_path(p)}'\n")

            cmd2 = [
                "ffmpeg", "-y",
                "-f", "concat",
                "-safe", "0",
                "-i", str(listfile),
                "-c", "copy",
                "-movflags", "+faststart",
                str(out_path)
            ]

            log2 = work / "concat.log"
            with log2.open("w", encoding="utf-8", errors="ignore") as lf:
                code2 = await run_ffmpeg_with_timeout(
                    cmd2,
                    lf,
                    process_handle=process_handle,
                )
            _record_log_path(save_log(log2, "concat-final"))

            if code2 != 0 or not out_path.exists():
                logger.error("concat failed at final stage")
                _flush_logs()
                detail = {
                    "error": "ffmpeg_failed_concat",
                    "log": log2.read_text() if log2.exists() else "",
                }
                if log_rel_path:
                    detail["log_path"] = log_rel_path
                raise HTTPException(status_code=500, detail=detail)

        # Step 4: Publish result
        if reporter:
            reporter.update(90, "Publishing result")
        pub = await publish_file(out_path, ".mp4")
        logger.info(f"concat completed: {len(job.clips)} clips -> {pub['rel']}")
        if reporter:
            reporter.update(95, "Complete")

        result_payload = {"file_url": pub["url"], "path": pub["dst"]}

        if as_json:
            response_payload: Dict[str, Any] = {"ok": True, **result_payload}
            return response_payload, result_payload, log_rel_path

        resp = FileResponse(
            pub["dst"],
            media_type="video/mp4",
            filename=os.path.basename(pub["dst"])
        )
        resp.headers["X-File-URL"] = pub["url"]
        return resp, result_payload, log_rel_path


class ConcatAliasJob(ConcatJob):
    clips: Optional[List[HttpUrl]] = None
    urls: Optional[List[HttpUrl]] = None

    @model_validator(mode="before")
    @classmethod
    def apply_url_alias(cls, values: Any) -> Any:
        if isinstance(values, dict):
            clips = values.get("clips")
            urls = values.get("urls")
            if not clips and urls:
                values = dict(values)
                values["clips"] = urls
        return values

    @model_validator(mode="after")
    def ensure_urls_reflect_clips(self) -> "ConcatAliasJob":
        if self.clips and self.urls is None:
            self.urls = self.clips
        return self

@app.post("/video/concat")
async def video_concat_alias(job: ConcatAliasJob, as_json: bool = False):
    if not job.clips:
        raise HTTPException(status_code=422, detail="Provide 'clips' (preferred) or 'urls' array of video URLs")
    return await video_concat_from_urls(job, as_json=as_json)


async def _compose_from_urls_impl(
    job: ComposeFromUrlsJob,
    progress: Optional[JobProgressReporter] = None,
    *,
    process_handle: Optional[JobProcessHandle] = None,
    job_id: Optional[str] = None,
) -> Tuple[Dict[str, str], Optional[str]]:
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

        video_url = _normalize_media_url(str(job.video_url))
        if video_url != str(job.video_url):
            logger.debug(
                "Normalized job video URL from %s to %s", job.video_url, video_url
            )
        await _download_to(video_url, v_path, job.headers)
        if progress:
            progress.update(35, "Downloaded primary video")
        has_audio = False
        has_bgm = False
        if job.audio_url:
            if progress:
                progress.update(45, "Downloading audio track")
            audio_url = _normalize_media_url(str(job.audio_url))
            if audio_url != str(job.audio_url):
                logger.debug(
                    "Normalized job audio URL from %s to %s", job.audio_url, audio_url
                )
            await _download_to(audio_url, a_path, job.headers)
            has_audio = True
        else:
            if progress:
                progress.update(45, "Audio track skipped")
        if job.bgm_url:
            if progress:
                progress.update(55, "Downloading background music")
            bgm_url = _normalize_media_url(str(job.bgm_url))
            if bgm_url != str(job.bgm_url):
                logger.debug(
                    "Normalized job bgm URL from %s to %s", job.bgm_url, bgm_url
                )
            await _download_to(bgm_url, b_path, job.headers)
            has_bgm = True
        else:
            if progress:
                progress.update(55, "Background music skipped")

        inputs = ["-i", str(v_path)]
        if has_audio:
            inputs += ["-i", str(a_path)]
        if has_bgm:
            inputs += ["-i", str(b_path)]

        maps = ["-map", "0:v:0"]
        decoder_args = get_video_decoder(v_path)
        cmd = ["ffmpeg", "-y"] + decoder_args + inputs

        # Only add duration limit if user explicitly provided it
        if job.duration_ms is not None:
            dur_s = f"{job.duration_ms/1000:.3f}"
            cmd += ["-t", dur_s]

        encode_args = build_encode_args(
            job.width, job.height, job.fps, decoder_args=decoder_args
        )
        cmd += encode_args
        if has_audio and has_bgm:
            # Mix voice audio with background music
            if job.loop_bgm:
                # Loop the BGM indefinitely, then mix
                af = (
                    f"[1:a]anull[a1];"
                    f"[2:a]aloop=loop=-1:size=2e+09,volume={job.bgm_volume}[a2];"
                    "[a1][a2]amix=inputs=2:normalize=0:duration=first[aout]"
                )
            else:
                # Standard mix - BGM stops when it ends
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
            # Only BGM, with optional looping
            if job.loop_bgm:
                cmd += [
                    "-filter_complex",
                    f"[1:a]aloop=loop=-1:size=2e+09,volume={job.bgm_volume}[aout]",
                ]
                maps += ["-map", "[aout]"]
            else:
                cmd += ["-filter_complex", f"[1:a]volume={job.bgm_volume}[aout]"]
                maps += ["-map", "[aout]"]
            cmd += ["-c:a", "aac", "-b:a", "128k", "-ar", "48000"]

        cmd += maps + [str(out_path)]
        parser: Optional[FFmpegProgressParser] = None
        if progress and job.duration_ms is not None:
            progress.update(70, "Rendering composition")
            parser = FFmpegProgressParser(
                job.duration_ms / 1000.0, progress, "Rendering composition"
            )
        elif progress:
            progress.update(70, "Rendering composition (unknown duration)")
        if job_id and _job_marked_killed(job_id):
            raise RuntimeError("job_killed")

        with log_path.open("w", encoding="utf-8", errors="ignore") as logf:
            code = await run_ffmpeg_with_timeout(
                cmd,
                logf,
                progress_parser=parser,
                process_handle=process_handle,
            )
        saved_log_path = save_log(log_path, "compose-urls")
        log_rel_path: Optional[str] = None
        if saved_log_path is not None:
            try:
                log_rel_path = str(saved_log_path.relative_to(LOGS_DIR))
            except ValueError:
                log_rel_path = str(saved_log_path)
            except Exception:
                log_rel_path = str(saved_log_path)
            if job_id is not None:
                _update_ffmpeg_async_job(job_id, log_path=log_rel_path)
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
        pub = await publish_file(out_path, ".mp4", duration_ms=job.duration_ms)
        if progress:
            progress.update(95, "Publishing file")
        logger.info("compose-from-urls completed: %s", pub["rel"])
        return pub, log_rel_path


@app.post("/compose/from-urls")
async def compose_from_urls(job: ComposeFromUrlsJob, as_json: bool = False):
    pub, _ = await _compose_from_urls_impl(job)
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
    async with safe_lock(JOBS_LOCK, timeout=5.0, operation="jobs_start"):
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

    with JOB_PROGRESS_LOCK:
        JOB_PROGRESS.pop(job_id, None)

    reporter = JobProgressReporter(job_id)
    start = time.perf_counter()
    struct_logger.info("job_started", job_id=job_id, job_type="compose_from_urls")
    process_handle = JobProcessHandle()
    with JOB_PROCESSES_LOCK:
        JOB_PROCESSES[job_id] = process_handle

    final_status = "processing"
    final_message: Optional[str] = "Job started"
    final_progress: Optional[int] = 10
    final_result: Optional[Dict[str, Any]] = None
    final_error: Optional[Any] = None
    final_status_code: Optional[int] = None
    final_duration_ms: Optional[float] = None
    notify_webhook = False
    webhook_status: Optional[str] = None
    webhook_result: Optional[Dict[str, Any]] = None
    webhook_error: Optional[Any] = None

    compose_log_path: Optional[str] = None

    try:
        pub, compose_log_path = await _compose_from_urls_impl(
            job,
            reporter,
            process_handle=process_handle,
            job_id=job_id,
        )

        if _job_marked_killed(job_id):
            logger.info("Compose job %s completed but was killed", job_id)
            final_status = "killed"
            final_message = "Killed (processing completed)"
            final_progress = 100
            final_result = None
            final_error = None
            final_status_code = None
            return

        reporter.update(100, "Completed")
        duration_ms = (time.perf_counter() - start) * 1000.0
        final_status = "finished"
        final_message = "Completed"
        final_progress = 100
        final_result = {
            "file_url": pub["url"],
            "path": pub["dst"],
            "rel": pub["rel"],
            "thumbnail": pub.get("thumbnail"),
        }
        final_duration_ms = duration_ms
        notify_webhook = True
        webhook_status = "finished"
        webhook_result = final_result
        webhook_error = None
        struct_logger.info(
            "job_completed",
            job_id=job_id,
            job_type="compose_from_urls",
            duration_ms=duration_ms,
            output_rel=pub["rel"],
        )
        return

    except HTTPException as exc:
        if _job_marked_killed(job_id):
            logger.info("Compose job %s failed after being killed", job_id)
            final_status = "killed"
            final_message = "Killed"
            final_progress = 100
            final_result = None
            final_error = None
            final_status_code = None
            return

        reporter.update(100, "Composition failed")
        detail = exc.detail if isinstance(exc.detail, (str, dict)) else str(exc.detail)
        struct_logger.error(
            "job_failed",
            job_id=job_id,
            job_type="compose_from_urls",
            status_code=exc.status_code,
            error=detail,
        )
        final_status = "failed"
        final_message = "Failed"
        final_progress = 100
        final_result = None
        final_error = detail
        final_status_code = exc.status_code
        notify_webhook = True
        webhook_status = "failed"
        webhook_error = detail
        webhook_result = None
        return

    except Exception as exc:
        if _job_marked_killed(job_id):
            logger.info("Compose job %s encountered exception after being killed", job_id)
            final_status = "killed"
            final_message = "Killed"
            final_progress = 100
            final_result = None
            final_error = None
            final_status_code = None
            return

        reporter.update(100, "Unexpected failure")
        logger.exception("Async compose job %s failed", job_id)
        struct_logger.error(
            "job_failed",
            job_id=job_id,
            job_type="compose_from_urls",
            status_code=500,
            error=str(exc),
        )
        final_status = "failed"
        final_message = "Failed"
        final_progress = 100
        final_result = None
        final_error = str(exc)
        final_status_code = 500
        notify_webhook = True
        webhook_status = "failed"
        webhook_error = str(exc)
        webhook_result = None
        return

    finally:
        logger.info("[%s] CLEANUP: Removing from JOB_PROCESSES", job_id)
        with JOB_PROCESSES_LOCK:
            JOB_PROCESSES.pop(job_id, None)

        progress_snapshot = _drain_job_progress(job_id)
        if final_progress is None and progress_snapshot is not None:
            final_progress = progress_snapshot.get("progress")

        message_to_store = final_message
        if message_to_store is None and progress_snapshot is not None:
            message_to_store = progress_snapshot.get("message")

        history_updates: List[Dict[str, Any]] = []
        if progress_snapshot is not None:
            history_updates.extend(progress_snapshot.get("history", []))

        now_ts = time.time()
        if final_progress is not None and message_to_store is not None:
            history_updates.append(
                {
                    "timestamp": now_ts,
                    "progress": final_progress,
                    "message": message_to_store,
                }
            )

        max_retries = 3
        for attempt in range(max_retries):
            try:
                async with safe_lock(JOBS_LOCK, timeout=30.0, operation="jobs_final"):
                    existing = JOBS.get(job_id, {})
                    created = existing.get("created", now_ts)
                    history = list(existing.get("history", []))
                    history.extend(history_updates)
                    if len(history) > JOB_HISTORY_LIMIT:
                        history = history[-JOB_HISTORY_LIMIT:]

                    progress_value = final_progress
                    if progress_value is None:
                        progress_value = existing.get("progress", 0)

                    message_value = message_to_store or existing.get("message")

                    updated = {
                        **existing,
                        "status": final_status,
                        "progress": progress_value,
                        "message": message_value,
                        "created": created,
                        "updated": now_ts,
                        "history": history,
                    }

                    if progress_snapshot is not None:
                        if "detail" in progress_snapshot:
                            updated["detail"] = progress_snapshot["detail"]
                        if "ffmpeg_stats" in progress_snapshot:
                            updated["ffmpeg_stats"] = progress_snapshot["ffmpeg_stats"]

                    if final_duration_ms is not None:
                        updated["duration_ms"] = final_duration_ms
                    if final_result is not None:
                        updated["result"] = final_result
                    elif final_status != "finished":
                        updated.pop("result", None)

                    if final_error is not None:
                        updated["error"] = final_error
                    elif final_status == "finished":
                        updated.pop("error", None)

                    if final_status_code is not None:
                        updated["status_code"] = final_status_code
                    elif final_status == "finished":
                        updated.pop("status_code", None)

                    if compose_log_path is not None:
                        updated["log_path"] = compose_log_path

                    JOBS[job_id] = updated
                break
            except Exception as lock_exc:  # pragma: no cover - defensive logging
                if attempt == max_retries - 1:
                    logger.error(
                        "Failed to update final status for job %s after %d attempts: %s",
                        job_id,
                        max_retries,
                        lock_exc,
                    )
                else:
                    await asyncio.sleep(0.5 * (attempt + 1))

        if notify_webhook:
            await _notify_webhook(
                str(job.webhook_url) if job.webhook_url else None,
                job_id=job_id,
                status=webhook_status or final_status,
                result=webhook_result,
                error=webhook_error,
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


@app.get("/jobs/{job_id}/log")
async def job_log(request: Request, job_id: str, format: str = "text"):
    """Get FFmpeg log for a job with live updates."""

    cleanup_old_jobs()

    with JOBS_LOCK:
        data = JOBS.get(job_id)

    if data is None:
        raise HTTPException(status_code=404, detail="Job not found")

    log_rel_path = data.get("log_path")
    if not log_rel_path:
        raise HTTPException(status_code=404, detail="No log available for this job")

    log_file = LOGS_DIR / log_rel_path
    if not log_file.exists():
        raise HTTPException(status_code=404, detail="Log file not found")

    if format == "html":
        redirect = ensure_dashboard_access(request)
        if redirect:
            return redirect

        # Get current job status for auto-refresh decision
        job_status = data.get("status", "unknown")
        is_active = job_status in ["queued", "processing"]

        log_content = log_file.read_text(encoding="utf-8", errors="ignore")

        html_content = f"""
        <!doctype html>
        <html>
        <head>
            <meta charset=\"utf-8\" />
            <title>Job {job_id} - FFmpeg Log</title>
            <style>
                body {{ 
                    font-family: system-ui, sans-serif; 
                    padding: 24px; 
                    max-width: 1400px; 
                    margin: 0 auto; 
                    background: #f8fafc;
                }}
                .header {{
                    display: flex;
                    align-items: center;
                    justify-content: space-between;
                    margin-bottom: 20px;
                    flex-wrap: wrap;
                    gap: 12px;
                }}
                .back-link {{ 
                    display: inline-block; 
                    color: #28a745; 
                    text-decoration: none; 
                    font-weight: 600; 
                }}
                .back-link:hover {{ 
                    text-decoration: underline; 
                }}
                h2 {{
                    margin: 0;
                    color: #1f2937;
                }}
                .controls {{
                    display: flex;
                    gap: 8px;
                    align-items: center;
                    flex-wrap: wrap;
                }}
                .btn {{
                    padding: 8px 16px;
                    border: none;
                    border-radius: 6px;
                    cursor: pointer;
                    font-size: 14px;
                    font-weight: 600;
                    transition: all 0.2s;
                }}
                .btn-primary {{
                    background: #28a745;
                    color: white;
                }}
                .btn-primary:hover {{
                    background: #1f7a34;
                }}
                .btn-secondary {{
                    background: #6c757d;
                    color: white;
                }}
                .btn-secondary:hover {{
                    background: #5a6268;
                }}
                .btn-danger {{
                    background: #dc3545;
                    color: white;
                }}
                .btn-danger:hover {{
                    background: #c82333;
                }}
                .status-badge {{
                    display: inline-flex;
                    align-items: center;
                    gap: 6px;
                    padding: 6px 12px;
                    border-radius: 999px;
                    font-size: 13px;
                    font-weight: 600;
                }}
                .status-badge.active {{
                    background: #d1fae5;
                    color: #065f46;
                }}
                .status-badge.active::before {{
                    content: '●';
                    animation: pulse 2s cubic-bezier(0.4, 0, 0.6, 1) infinite;
                }}
                .status-badge.inactive {{
                    background: #e5e7eb;
                    color: #6b7280;
                }}
                @keyframes pulse {{
                    0%, 100% {{ opacity: 1; }}
                    50% {{ opacity: 0.5; }}
                }}
                .log-info {{
                    background: white;
                    padding: 12px 16px;
                    border-radius: 8px;
                    margin-bottom: 16px;
                    display: flex;
                    justify-content: space-between;
                    align-items: center;
                    box-shadow: 0 1px 3px rgba(0,0,0,0.1);
                    flex-wrap: wrap;
                    gap: 12px;
                }}
                .log-stats {{
                    display: flex;
                    gap: 24px;
                    font-size: 14px;
                    color: #6b7280;
                }}
                .log-stats span {{
                    display: flex;
                    align-items: center;
                    gap: 6px;
                }}
                .log-stats strong {{
                    color: #1f2937;
                }}
                .log-container {{ 
                    background: #1e1e1e; 
                    color: #d4d4d4; 
                    padding: 20px; 
                    border-radius: 8px; 
                    overflow-x: auto;
                    box-shadow: 0 4px 6px rgba(0,0,0,0.1);
                    max-height: 70vh;
                    overflow-y: auto;
                    position: relative;
                }}
                .log-container.auto-scroll {{
                    scroll-behavior: smooth;
                }}
                pre {{ 
                    margin: 0; 
                    font-family: 'Courier New', 'Monaco', monospace; 
                    font-size: 12px; 
                    line-height: 1.6; 
                    white-space: pre-wrap; 
                    word-wrap: break-word; 
                }}
                .log-line {{
                    padding: 2px 0;
                }}
                .log-line:hover {{
                    background: rgba(255, 255, 255, 0.05);
                }}
                .log-line:has(.keyword) {{
                    color: #4ec9b0;
                }}
                .keyword {{
                    color: #569cd6;
                    font-weight: 600;
                }}
                .value {{
                    color: #ce9178;
                }}
                .error {{
                    color: #f48771;
                    font-weight: 600;
                }}
                .warning {{
                    color: #dcdcaa;
                }}
                .scroll-bottom-btn {{
                    position: fixed;
                    bottom: 30px;
                    right: 30px;
                    background: #28a745;
                    color: white;
                    border: none;
                    border-radius: 50%;
                    width: 48px;
                    height: 48px;
                    font-size: 20px;
                    cursor: pointer;
                    box-shadow: 0 4px 12px rgba(0,0,0,0.3);
                    display: none;
                    z-index: 1000;
                    transition: all 0.3s;
                }}
                .scroll-bottom-btn:hover {{
                    background: #1f7a34;
                    transform: scale(1.1);
                }}
                .scroll-bottom-btn.show {{
                    display: block;
                }}
                #updateIndicator {{
                    position: fixed;
                    top: 20px;
                    right: 20px;
                    background: #28a745;
                    color: white;
                    padding: 8px 16px;
                    border-radius: 6px;
                    font-size: 13px;
                    font-weight: 600;
                    box-shadow: 0 4px 12px rgba(0,0,0,0.2);
                    display: none;
                    z-index: 1000;
                    animation: slideIn 0.3s ease;
                }}
                @keyframes slideIn {{
                    from {{ transform: translateY(-100%); opacity: 0; }}
                    to {{ transform: translateY(0); opacity: 1; }}
                }}
            </style>
        </head>
        <body>
            <div class="header">
                <div>
                    <a href="/jobs/{job_id}?format=html" class="back-link">&larr; Back to Job</a>
                    <h2>FFmpeg Log - Job {job_id}</h2>
                </div>
                <div class="controls">
                    <span class="status-badge {'active' if is_active else 'inactive'}" id="autoRefreshBadge">
                        {'Live Updates' if is_active else 'Job Completed'}
                    </span>
                    <button class="btn btn-secondary" id="toggleAutoScroll" onclick="toggleAutoScroll()">
                        Auto-scroll: <span id="autoScrollStatus">ON</span>
                    </button>
                    <button class="btn btn-secondary" onclick="copyLog()">📋 Copy</button>
                    <button class="btn btn-primary" onclick="downloadLog()">⬇️ Download</button>
                    {'<button class="btn btn-danger" onclick="clearLog()">🗑️ Clear View</button>' if not is_active else ''}
                </div>
            </div>

            <div class="log-info">
                <div class="log-stats">
                    <span><strong>Lines:</strong> <span id="lineCount">0</span></span>
                    <span><strong>Size:</strong> <span id="fileSize">0 KB</span></span>
                    <span><strong>Last updated:</strong> <span id="lastUpdate">Just now</span></span>
                </div>
            </div>

            <div class="log-container" id="logContainer">
                <pre id="logContent">{html.escape(log_content)}</pre>
            </div>

            <button class="scroll-bottom-btn" id="scrollBottomBtn" onclick="scrollToBottom(true)">
                ↓
            </button>

            <div id="updateIndicator">Log updated</div>

            <script>
                let autoScroll = true;
                let autoRefresh = {str(is_active).lower()};
                let refreshInterval = null;
                let lastLogContent = '';
                let isUserScrolling = false;
                let scrollTimeout = null;

                const logContainer = document.getElementById('logContainer');
                const logContent = document.getElementById('logContent');
                const scrollBtn = document.getElementById('scrollBottomBtn');
                const updateIndicator = document.getElementById('updateIndicator');

                // Initialize
                updateStats();
                if (autoRefresh) {{
                    startAutoRefresh();
                }}
                scrollToBottom(false);

                // Monitor user scrolling
                logContainer.addEventListener('scroll', function() {{
                    const isAtBottom = logContainer.scrollHeight - logContainer.scrollTop <= logContainer.clientHeight + 100;
                    
                    if (!isAtBottom) {{
                        scrollBtn.classList.add('show');
                        if (autoScroll) {{
                            autoScroll = false;
                            updateAutoScrollButton();
                        }}
                    }} else {{
                        scrollBtn.classList.remove('show');
                    }}
                    
                    isUserScrolling = true;
                    clearTimeout(scrollTimeout);
                    scrollTimeout = setTimeout(() => {{
                        isUserScrolling = false;
                    }}, 1000);
                }});

                function startAutoRefresh() {{
                    refreshInterval = setInterval(function() {{
                        fetch('/jobs/{job_id}/log?format=text')
                            .then(response => response.text())
                            .then(data => {{
                                if (data !== lastLogContent) {{
                                    lastLogContent = data;
                                    logContent.textContent = data;
                                    updateStats();
                                    
                                    // Show update indicator
                                    updateIndicator.style.display = 'block';
                                    setTimeout(() => {{
                                        updateIndicator.style.display = 'none';
                                    }}, 1500);
                                    
                                    // Auto-scroll if enabled and user isn't actively scrolling
                                    if (autoScroll && !isUserScrolling) {{
                                        scrollToBottom(false);
                                    }}
                                }}
                            }})
                            .catch(err => console.warn('Failed to refresh log:', err));
                        
                        // Check if job is still active
                        fetch('/jobs/{job_id}?format=json')
                            .then(response => response.json())
                            .then(data => {{
                                const isActive = data.status === 'processing' || data.status === 'queued';
                                if (!isActive && autoRefresh) {{
                                    stopAutoRefresh();
                                    document.getElementById('autoRefreshBadge').textContent = 'Job Completed';
                                    document.getElementById('autoRefreshBadge').classList.remove('active');
                                    document.getElementById('autoRefreshBadge').classList.add('inactive');
                                }}
                            }})
                            .catch(err => console.warn('Failed to check job status:', err));
                    }}, 2000);
                }}

                function stopAutoRefresh() {{
                    if (refreshInterval) {{
                        clearInterval(refreshInterval);
                        refreshInterval = null;
                    }}
                    autoRefresh = false;
                }}

                function toggleAutoScroll() {{
                    autoScroll = !autoScroll;
                    updateAutoScrollButton();
                    if (autoScroll) {{
                        scrollToBottom(true);
                    }}
                }}

                function updateAutoScrollButton() {{
                    document.getElementById('autoScrollStatus').textContent = autoScroll ? 'ON' : 'OFF';
                }}

                function scrollToBottom(smooth) {{
                    if (smooth) {{
                        logContainer.scrollTo({{
                            top: logContainer.scrollHeight,
                            behavior: 'smooth'
                        }});
                    }} else {{
                        logContainer.scrollTop = logContainer.scrollHeight;
                    }}
                }}

                function updateStats() {{
                    const content = logContent.textContent;
                    const lines = content.split('\n').length;
                    const sizeKB = (new Blob([content]).size / 1024).toFixed(2);
                    
                    document.getElementById('lineCount').textContent = lines.toLocaleString();
                    document.getElementById('fileSize').textContent = sizeKB + ' KB';
                    document.getElementById('lastUpdate').textContent = new Date().toLocaleTimeString();
                }}

                function copyLog() {{
                    navigator.clipboard.writeText(logContent.textContent)
                        .then(() => {{
                            const btn = event.target;
                            const originalText = btn.textContent;
                            btn.textContent = '✓ Copied!';
                            btn.style.background = '#28a745';
                            setTimeout(() => {{
                                btn.textContent = originalText;
                                btn.style.background = '';
                            }}, 2000);
                        }})
                        .catch(err => alert('Failed to copy: ' + err));
                }}

                function downloadLog() {{
                    const blob = new Blob([logContent.textContent], {{ type: 'text/plain' }});
                    const url = URL.createObjectURL(blob);
                    const a = document.createElement('a');
                    a.href = url;
                    a.download = 'job_{job_id}_ffmpeg.log';
                    document.body.appendChild(a);
                    a.click();
                    document.body.removeChild(a);
                    URL.revokeObjectURL(url);
                }}

                function clearLog() {{
                    if (confirm('This will only clear the display, not the actual log file. Continue?')) {{
                        logContent.textContent = '';
                        updateStats();
                    }}
                }}
            </script>
        </body>
        </html>
        """
        return HTMLResponse(html_content)

    return FileResponse(
        log_file,
        media_type="text/plain",
        filename=f"job_{job_id}.log",
    )


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

    all_jobs = [_merge_job_progress(job["job_id"], job) for job in all_jobs]

    all_jobs.sort(key=lambda item: item.get("created", 0), reverse=True)
    if status:
        all_jobs = [job for job in all_jobs if job.get("status") == status]

    total_items = len(all_jobs)
    total_pages = max(1, math.ceil(total_items / page_size))
    page = min(max(page, 1), total_pages)
    start = (page - 1) * page_size
    end = start + page_size
    jobs_slice = all_jobs[start:end]

    status_classes = {
        "finished": "success",
        "failed": "error",
        "processing": "warning",
        "queued": "info",
    }
    job_rows = []
    for job in jobs_slice:
        job_id = job.get("job_id", "-")
        status_val = job.get("status", "unknown")
        created_ts = job.get("created") or 0
        created = datetime.fromtimestamp(created_ts, tz=timezone.utc)
        duration_ms = job.get("duration_ms")
        job_rows.append(
            {
                "job_id": str(job_id),
                "status": str(status_val),
                "status_class": status_classes.get(status_val, ""),
                "created": created.strftime("%Y-%m-%d %H:%M:%S UTC"),
                "duration": f"{(duration_ms or 0) / 1000:.1f}s" if duration_ms else "—",
            }
        )

    filters_data = []
    for filter_status in [None, "finished", "failed", "processing", "queued"]:
        label = filter_status or "All"
        params = {"page": 1}
        if filter_status:
            params["status"] = filter_status
        query = urlencode(params)
        filters_data.append(
            {
                "label": label,
                "url": f"/jobs?{query}",
                "active": filter_status == status,
            }
        )

    pagination_text = f"Page {page} of {total_pages}"

    if templates is not None:
        context = {
            "request": request,
            "title": "Job History",
            "max_width": "1400px",
            **nav_context("/jobs"),
            "jobs": job_rows,
            "filters": filters_data,
            "pagination_text": pagination_text,
        }
        return templates.TemplateResponse(request, "jobs.html", context)

    rows: List[str] = []
    for job in job_rows:
        rows.append(
            """
            <tr>
                <td><code>{job_id}</code></td>
                <td><span class="status {status_class}">{status}</span></td>
                <td>{created}</td>
                <td>{duration}</td>
                <td><a href="/jobs/{job_id}?format=html" target="_blank">View</a></td>
            </tr>
            """.format(
                job_id=html.escape(job["job_id"]),
                status_class=job["status_class"],
                status=html.escape(job["status"]),
                created=job["created"],
                duration=job["duration"],
            )
        )

    if not rows:
        rows.append("<tr><td colspan='5'>No jobs found</td></tr>")

    filter_links = []
    for item in filters_data:
        css_class = "active" if item["active"] else ""
        filter_links.append(
            f"<a href='{html.escape(item['url'], quote=True)}' class='filter-link {css_class}'>{html.escape(item['label'])}</a>"
        )

    top_bar_html = fallback_top_bar_html("/jobs", indent="        ")
    html_content = f"""
    <!doctype html>
    <html>
    <head>
        <meta charset="utf-8" />
        <title>Job History</title>
        <style>
            body {{ font-family: system-ui, sans-serif; padding: 24px; max-width: 1400px; margin: 0 auto; }}
            .top-bar {{ display: flex; align-items: center; justify-content: space-between; gap: 16px; flex-wrap: wrap; margin-bottom: 24px; }}
            .brand {{ font-size: 32px; font-weight: bold; margin: 0; }}
            .brand .ff {{ color: #28a745; }}
            .brand .api {{ color: #000; }}
            .main-nav {{ margin: 0; margin-left: auto; display: flex; gap: 12px; flex-wrap: wrap; justify-content: flex-end; }}
            .main-nav a {{ color: #28a745; text-decoration: none; font-weight: 500; }}
            .main-nav a:hover {{ text-decoration: underline; }}
            .filters {{ margin-bottom: 16px; display: flex; gap: 12px; flex-wrap: wrap; }}
            .filter-link {{ padding: 6px 12px; border-radius: 4px; background: #f0f0f0; text-decoration: none; color: #333; }}
            .filter-link.active {{ background: #28a745; color: white; }}
            table {{ border-collapse: collapse; width: 100%; background: #fff; box-shadow: 0 1px 2px rgba(0,0,0,0.08); }}
            th, td {{ border-bottom: 1px solid #e2e8f0; padding: 12px 14px; font-size: 14px; text-align: left; }}
            th {{ background: #f8fafc; color: #1f2937; }}
            code {{ background: #f3f4f6; padding: 2px 6px; border-radius: 3px; }}
            .status {{ padding: 4px 8px; border-radius: 4px; font-weight: 600; text-transform: capitalize; }}
            .status.success {{ background: #e8f5e9; color: #256029; }}
            .status.error {{ background: #fdecea; color: #b3261e; }}
            .status.warning {{ background: #fff3cd; color: #856404; }}
            .status.info {{ background: #e0f2fe; color: #1f7a34; }}
            .pagination {{ margin-top: 16px; color: #666; }}
        </style>
    </head>
    <body>
{top_bar_html}
        <h2>Job History</h2>
        <div class="filters">{''.join(filter_links)}</div>
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
        <p class="pagination">{pagination_text}</p>
    </body>
    </html>
    """
    return HTMLResponse(html_content)


@app.get("/jobs/{job_id}")
async def job_status(request: Request, job_id: str, format: str = "json"):
    """Get job status - supports both JSON API and HTML view"""
    cleanup_old_jobs()
    with JOBS_LOCK:
        data = JOBS.get(job_id)
    if data is None:
        if format == "html":
            redirect = ensure_dashboard_access(request)
            if redirect:
                return redirect
            raise HTTPException(status_code=404, detail="Job not found")
        raise HTTPException(status_code=404, detail="job_not_found")

    data = _merge_job_progress(job_id, data)

    # Calculate ETA if job is processing
    eta_seconds: Optional[float] = None
    elapsed_seconds: Optional[float] = None
    if data.get("status") == "processing":
        started = data.get("started")
        progress = data.get("progress", 0)
        if started and progress > 0:
            elapsed_seconds = time.time() - started
            estimated_total = (elapsed_seconds / progress) * 100
            eta_seconds = max(0.0, estimated_total - elapsed_seconds)

    if format == "html":
        redirect = ensure_dashboard_access(request)
        if redirect:
            return redirect

        csrf_token = ensure_csrf_token(request)

        created_ts = data.get("created") or 0
        created_dt = datetime.fromtimestamp(created_ts, tz=timezone.utc)
        created_str = created_dt.strftime("%Y-%m-%d %H:%M:%S UTC")

        started_ts = data.get("started")
        started_str = None
        if started_ts:
            started_dt = datetime.fromtimestamp(started_ts, tz=timezone.utc)
            started_str = started_dt.strftime("%Y-%m-%d %H:%M:%S UTC")

        history_items = []
        for item in data.get("history", [])[-20:]:
            ts = item.get("timestamp", 0)
            dt = datetime.fromtimestamp(ts, tz=timezone.utc)
            history_items.append(
                {
                    "timestamp": dt.strftime("%H:%M:%S"),
                    "progress": item.get("progress", 0),
                    "message": item.get("message", ""),
                }
            )

        context = {
            "request": request,
            "title": f"Job {job_id}",
            "max_width": "1000px",
            **nav_context("/jobs"),
            "job_id": job_id,
            "status": data.get("status", "unknown"),
            "progress": data.get("progress", 0),
            "message": data.get("message", ""),
            "detail": data.get("detail"),
            "created": created_str,
            "started": started_str,
            "elapsed_seconds": elapsed_seconds,
            "eta_seconds": eta_seconds,
            "duration_ms": data.get("duration_ms"),
            "result": data.get("result"),
            "error": data.get("error"),
            "history": history_items,
            "ffmpeg_stats": data.get("ffmpeg_stats"),
            "log_path": data.get("log_path"),
            "total_frames": data.get("total_frames"),
            "csrf_token": csrf_token,
        }

        if templates is not None:
            return templates.TemplateResponse(request, "job_detail.html", context)

        return HTMLResponse(_job_detail_fallback_html(context))

    payload: Dict[str, Any] = {"job_id": job_id}
    payload.update(data)
    if eta_seconds is not None:
        payload["eta_seconds"] = int(eta_seconds)
    if elapsed_seconds is not None:
        payload["elapsed_seconds"] = int(elapsed_seconds)
    return payload


def _job_marked_killed(job_id: str) -> bool:
    with JOBS_LOCK:
        return JOBS.get(job_id, {}).get("status") == "killed"


@app.post("/jobs/{job_id}/kill")
async def kill_job(request: Request, job_id: str):
    """Kill a running job and mark it as killed."""

    accept_header = request.headers.get("accept", "")
    if accept_header.startswith("text/html"):
        redirect = ensure_dashboard_access(request)
        if redirect:
            return redirect

    cleanup_old_jobs()

    with JOBS_LOCK:
        job_data = JOBS.get(job_id)

    if job_data is None:
        raise HTTPException(status_code=404, detail="job_not_found")

    status = job_data.get("status")
    if status not in {"queued", "processing"}:
        raise HTTPException(
            status_code=400,
            detail=f"Cannot kill job with status '{status}'. Only queued/processing jobs can be killed.",
        )

    with JOB_PROCESSES_LOCK:
        process_handle = JOB_PROCESSES.get(job_id)

    process = None
    if process_handle is not None:
        process = getattr(process_handle, "process", process_handle)
    killed = False

    if process is not None:
        try:
            if isinstance(process, asyncio.subprocess.Process):
                if process.returncode is None:
                    process.terminate()
                    try:
                        await asyncio.wait_for(process.wait(), timeout=5)
                        killed = True
                    except asyncio.TimeoutError:
                        process.kill()
                        try:
                            await asyncio.wait_for(process.wait(), timeout=1)
                            killed = True
                        except asyncio.TimeoutError:
                            pass
            else:
                if getattr(process, "poll", lambda: None)() is None:
                    process.terminate()
                    try:
                        process.wait(timeout=5)
                        killed = True
                    except subprocess.TimeoutExpired:
                        process.kill()
                        try:
                            process.wait(timeout=1)
                            killed = True
                        except subprocess.TimeoutExpired:
                            pass
        except Exception as exc:
            logger.warning("Failed to kill process for job %s: %s", job_id, exc)

    with JOBS_LOCK:
        existing = JOBS.get(job_id, {})
        history = list(existing.get("history", []))
        history.append(
            {
                "timestamp": time.time(),
                "progress": existing.get("progress", 0),
                "message": "Killed by user",
            }
        )
        if len(history) > JOB_HISTORY_LIMIT:
            history = history[-JOB_HISTORY_LIMIT:]

        JOBS[job_id] = {
            **existing,
            "status": "killed",
            "message": "Killed by user",
            "updated": time.time(),
            "history": history,
        }

    with JOB_PROGRESS_LOCK:
        JOB_PROGRESS.pop(job_id, None)

    with JOB_PROCESSES_LOCK:
        JOB_PROCESSES.pop(job_id, None)

    logger.info("Job %s killed by user (process killed: %s)", job_id, killed)

    if accept_header.startswith("text/html"):
        return RedirectResponse(url="/jobs", status_code=303)

    return {
        "ok": True,
        "job_id": job_id,
        "status": "killed",
        "process_killed": killed,
    }


def _job_detail_fallback_html(context: Dict[str, Any]) -> str:
    """Fallback HTML for job detail page when templates unavailable"""
    job_id = context["job_id"]
    status = context["status"]
    progress = context["progress"]
    message = context.get("message", "")
    detail = context.get("detail")
    created = context["created"]
    started = context.get("started", "Not started") or "Not started"
    elapsed = context.get("elapsed_seconds")
    eta = context.get("eta_seconds")
    duration_ms = context.get("duration_ms")
    result = context.get("result")
    error = context.get("error")
    history = context.get("history", [])
    ffmpeg_stats = context.get("ffmpeg_stats") or {}
    log_path = context.get("log_path")

    status_class = {
        "finished": "success",
        "failed": "error",
        "processing": "warning",
        "queued": "info",
    }.get(status, "")

    eta_display = ""
    if eta is not None and status == "processing":
        minutes = int(eta // 60)
        seconds = int(eta % 60)
        eta_display = f"<p><strong>Estimated time remaining:</strong> {minutes}m {seconds}s</p>"

    elapsed_display = ""
    if elapsed is not None:
        minutes = int(elapsed // 60)
        seconds = int(elapsed % 60)
        elapsed_display = f"<p><strong>Elapsed:</strong> {minutes}m {seconds}s</p>"
    elif duration_ms is not None:
        seconds_total = duration_ms / 1000
        minutes = int(seconds_total // 60)
        seconds = int(seconds_total % 60)
        elapsed_display = f"<p><strong>Duration:</strong> {minutes}m {seconds}s</p>"

    result_html = ""
    if result:
        if isinstance(result, dict):
            if "file_url" in result:
                path_html = ""
                if result.get("path"):
                    path_html = (
                        f"<p><strong>Path:</strong> <code>{html.escape(result.get('path', ''), quote=True)}</code></p>"
                    )
                result_html = f"""
                <div class=\"result-box\">
                    <h3>Result</h3>
                    <p><a href=\"{html.escape(result['file_url'], quote=True)}\" target=\"_blank\" class=\"download-link\">Download file</a></p>
                    {path_html}
                </div>
                """
            elif "outputs" in result:
                outputs_list = "".join(
                    f"<li><a href=\"{html.escape(url, quote=True)}\" target=\"_blank\">{html.escape(name)}</a></li>"
                    for name, url in result["outputs"].items()
                )
                result_html = f"""
                <div class=\"result-box\">
                    <h3>Result Files</h3>
                    <ul>{outputs_list}</ul>
                </div>
                """

    error_html = ""
    if error:
        error_text = error if isinstance(error, str) else str(error)
        error_html = f"""
        <div class=\"error-box\">
            <h3>Error</h3>
            <pre>{html.escape(error_text)}</pre>
        </div>
        """

    history_rows = ""
    for item in history:
        history_rows += f"""
        <tr>
            <td>{html.escape(item['timestamp'])}</td>
            <td>{item['progress']}%</td>
            <td>{html.escape(item['message'])}</td>
        </tr>
        """

    if not history_rows:
        history_rows = "<tr><td colspan=\"3\">No history yet</td></tr>"

    stats_html = ""
    if ffmpeg_stats:
        stats_items = "".join(
            f"<li><strong>{html.escape(key.capitalize())}:</strong> {html.escape(str(value))}</li>"
            for key, value in ffmpeg_stats.items()
        )
        stats_html = f"""
        <div class=\"ffmpeg-stats\">
            <h3>FFmpeg Live Statistics</h3>
            <ul>{stats_items}</ul>
        </div>
        """

    log_button_html = ""
    if log_path:
        log_button_html = (
            f"<a href=\"/jobs/{html.escape(job_id)}/log?format=html\" target=\"_blank\" "
            "class=\"view-log-btn\">View FFmpeg Log</a>"
        )

    auto_refresh = ""
    if status in {"queued", "processing"}:
        auto_refresh = '<meta http-equiv="refresh" content="2">'

    top_bar_html = fallback_top_bar_html("/jobs")

    return f"""
    <!doctype html>
    <html>
    <head>
        <meta charset=\"utf-8\" />
        <title>Job {html.escape(job_id)}</title>
        {auto_refresh}
        <style>
            body {{ font-family: system-ui, sans-serif; padding: 24px; max-width: 1000px; margin: 0 auto; }}
            .top-bar {{ display: flex; align-items: center; justify-content: space-between; gap: 16px; flex-wrap: wrap; margin-bottom: 24px; }}
            .brand {{ font-size: 32px; font-weight: bold; margin: 0; text-decoration: none; display: inline-flex; align-items: center; gap: 2px; color: #000; }}
            .brand .ff {{ color: #28a745; }}
            .brand .api {{ color: #000; }}
            .main-nav {{ margin: 0; margin-left: auto; display: flex; gap: 12px; flex-wrap: wrap; }}
            .main-nav a {{ padding: 8px 14px; border-radius: 6px; background: #e2e8f0; color: #6b7280; text-decoration: none; font-weight: 600; }}
            .main-nav a:hover {{ background: #d1d5db; color: #000; }}
            .main-nav a.active {{ background: #28a745; color: #fff; }}

            .job-header {{ background: #f8fafc; padding: 20px; border-radius: 8px; margin-bottom: 24px; }}
            .job-header-top {{ display: flex; align-items: flex-start; justify-content: space-between; gap: 16px; margin-bottom: 12px; flex-wrap: wrap; }}
            .job-id {{ font-family: monospace; font-size: 18px; color: #1f2937; }}
            .status {{ display: inline-block; padding: 6px 12px; border-radius: 4px; font-weight: 600; text-transform: capitalize; }}
            .status.success {{ background: #e8f5e9; color: #256029; }}
            .status.error {{ background: #fdecea; color: #b3261e; }}
            .status.warning {{ background: #fff3cd; color: #856404; }}
            .status.info {{ background: #e0f2fe; color: #1f7a34; }}
            .job-header-actions {{ display: flex; gap: 12px; align-items: center; }}
            .view-log-btn {{ padding: 10px 20px; border-radius: 6px; background: #6c757d; color: white; font-weight: 600; text-decoration: none; display: inline-block; }}
            .view-log-btn:hover {{ background: #5a6268; }}

            .progress-section {{ background: #fff; padding: 20px; border-radius: 8px; margin-bottom: 24px; box-shadow: 0 1px 3px rgba(0,0,0,0.1); }}
            .progress-bar-container {{ background: #e2e8f0; border-radius: 8px; height: 32px; overflow: hidden; margin: 16px 0; }}
            .progress-bar {{ background: linear-gradient(90deg, #28a745, #20c997); height: 100%; transition: width 0.3s ease; display: flex; align-items: center; justify-content: center; color: white; font-weight: 600; }}
            .status-detail {{ font-family: "Courier New", monospace; font-size: 12px; color: #6b7280; background: #f9fafb; padding: 8px 12px; border-radius: 4px; margin-top: 8px; }}
            .ffmpeg-stats {{ background: #fff; padding: 16px; border-radius: 8px; margin-bottom: 24px; box-shadow: 0 1px 3px rgba(0,0,0,0.1); }}
            .ffmpeg-stats ul {{ list-style: none; padding: 0; margin: 0; display: grid; grid-template-columns: repeat(auto-fit, minmax(160px, 1fr)); gap: 12px; }}
            .ffmpeg-stats li {{ background: #f8fafc; padding: 12px; border-radius: 6px; border: 1px solid #e5e7eb; font-size: 13px; }}

            .info-grid {{ display: grid; grid-template-columns: repeat(auto-fit, minmax(200px, 1fr)); gap: 16px; }}
            .info-box {{ background: #f8fafc; padding: 12px; border-radius: 6px; }}
            .info-box strong {{ display: block; color: #6b7280; font-size: 12px; margin-bottom: 4px; }}

            .result-box, .error-box {{ background: #fff; padding: 20px; border-radius: 8px; margin-bottom: 24px; box-shadow: 0 1px 3px rgba(0,0,0,0.1); }}
            .result-box h3, .error-box h3 {{ margin-top: 0; }}
            .download-link {{ display: inline-block; padding: 10px 20px; background: #28a745; color: white; text-decoration: none; border-radius: 6px; font-weight: 600; }}
            .download-link:hover {{ background: #1f7a34; }}
            .error-box {{ background: #fdecea; border-left: 4px solid #b3261e; }}
            .error-box pre {{ background: #fff; padding: 12px; border-radius: 4px; overflow-x: auto; font-size: 12px; }}

            table {{ width: 100%; border-collapse: collapse; background: #fff; margin-top: 16px; }}
            th, td {{ padding: 10px 12px; border-bottom: 1px solid #e2e8f0; font-size: 14px; text-align: left; }}
            th {{ background: #f8fafc; color: #1f2937; font-weight: 600; }}

            .back-link {{ display: inline-block; margin-bottom: 20px; color: #28a745; text-decoration: none; font-weight: 600; }}
            .back-link:hover {{ text-decoration: underline; }}
        </style>
    </head>
    <body>
{top_bar_html}
        <a href="/jobs" class="back-link">&larr; Back to Jobs</a>

        <div class="job-header">
            <div class="job-header-top">
                <div>
                    <h1>Job Details</h1>
                    <p class="job-id">{html.escape(job_id)}</p>
                </div>
                <div class="job-header-actions">{log_button_html}</div>
            </div>
            <span class="status {status_class}">{html.escape(status)}</span>
        </div>

        <div class="progress-section">
            <h2>Progress: {progress}%</h2>
            <div class="progress-bar-container">
                <div class="progress-bar" style="width: {progress}%">
                    {progress}%
                </div>
            </div>
            <p><strong>Current status:</strong> {html.escape(message)}</p>
            {f'<pre class="status-detail">{html.escape(detail or "")}</pre>' if detail else ''}
            {eta_display}
            {elapsed_display}
        </div>
        {stats_html}

        <div class="info-grid">
            <div class="info-box">
                <strong>Created</strong>
                {html.escape(created)}
            </div>
            <div class="info-box">
                <strong>Started</strong>
                {html.escape(started)}
            </div>
        </div>

        {result_html}
        {error_html}

        <h3>Progress History</h3>
        <table>
            <thead>
                <tr><th>Time</th><th>Progress</th><th>Message</th></tr>
            </thead>
            <tbody>
                {history_rows}
            </tbody>
        </table>
    </body>
    </html>
    """


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
                concat_code = await run_ffmpeg_with_timeout(concat_cmd, lf)
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
            code = await run_ffmpeg_with_timeout(cmd, lf)
        save_log(log, "compose-tracks")
        out_path = work / "output.mp4"
        if code != 0 or not out_path.exists():
            logger.error("compose-from-tracks failed")
            _flush_logs()
            return JSONResponse(status_code=500, content={"error": "ffmpeg_failed", "log": log.read_text()})

        pub = await publish_file(out_path, ".mp4")
        if as_json:
            return {"ok": True, "file_url": pub["url"], "path": pub["dst"]}
        resp = FileResponse(pub["dst"], media_type="video/mp4", filename=os.path.basename(pub["dst"]))
        resp.headers["X-File-URL"] = pub["url"]
        return resp


@app.post("/v1/run-ffmpeg-command")
async def run_rendi(job: RendiJob):
    cleanup_old_jobs()
    job_id = str(uuid4())
    start_time = time.time()

    with JOBS_LOCK:
        JOBS[job_id] = {
            "status": "processing",
            "progress": 0,
            "message": "Starting FFmpeg command",
            "history": [
                {
                    "timestamp": start_time,
                    "progress": 0,
                    "message": "Starting",
                }
            ],
            "created": start_time,
            "updated": start_time,
            "started": start_time,
        }

    reporter = JobProgressReporter(job_id)
    process_handle = JobProcessHandle()
    with JOB_PROCESSES_LOCK:
        JOB_PROCESSES[job_id] = process_handle

    log_rel_path: Optional[str] = None
    published: Dict[str, str] = {}
    result_payload: Optional[Dict[str, Any]] = None

    def _record_log_path(saved: Optional[Path]) -> None:
        nonlocal log_rel_path
        if saved is None:
            return
        try:
            log_rel_path = str(saved.relative_to(LOGS_DIR))
        except Exception:
            log_rel_path = str(saved)

    try:
        check_disk_space(WORK_DIR)
        reporter.update(10, "Preparing workspace")

        with TemporaryDirectory(prefix="rendi_", dir=str(WORK_DIR)) as workdir:
            work = Path(workdir)
            resolved: Dict[str, str] = {}

            reporter.update(20, "Downloading input files")
            total_inputs = max(len(job.input_files), 1)
            for idx, (key, url) in enumerate(job.input_files.items()):
                progress = 20 + int(((idx + 1) / total_inputs) * 30)
                reporter.update(progress, f"Downloading input {idx + 1}/{total_inputs}")
                p = work / f"{key}"
                await _download_to(str(url), p, None)
                resolved[key] = str(p)

            out_paths: Dict[str, Path] = {key: work / name for key, name in job.output_files.items()}

            reporter.update(50, "Building FFmpeg command")
            cmd_text = job.ffmpeg_command
            for k, p in resolved.items():
                cmd_text = cmd_text.replace("{{" + k + "}}", p)
            for k, p in out_paths.items():
                cmd_text = cmd_text.replace("{{" + k + "}}", str(p))

            # Parse command first to get actual arguments
            try:
                args = shlex.split(cmd_text)
            except Exception as exc:
                raise HTTPException(status_code=400, detail={"error": "invalid_command", "msg": str(exc)}) from exc

            # Validate arguments - check for dangerous patterns in actual parsed args
            # This is more secure than string matching on the full command
            for i, arg in enumerate(args):
                arg_lower = arg.lower()

                # Block dangerous input formats
                if arg_lower == "-f" and i + 1 < len(args):
                    next_arg_lower = args[i + 1].lower()
                    if next_arg_lower in ("lavfi", "libavfilter"):
                        raise HTTPException(
                            status_code=400,
                            detail={"error": "forbidden_pattern", "pattern": f"-f {args[i + 1]}"},
                        )

                # Block dangerous input sources
                if arg_lower == "-i" and i + 1 < len(args):
                    input_value = args[i + 1].lower()
                    dangerous_inputs = ["/dev/", "file:", "concat:", "pipe:", "tcp:", "udp:", "rtp:", "rtsp:"]
                    for dangerous in dangerous_inputs:
                        if input_value.startswith(dangerous):
                            raise HTTPException(
                                status_code=400,
                                detail={"error": "forbidden_pattern", "pattern": f"-i {dangerous}*"},
                            )

                # Block filter_complex from untrusted sources (can be used for arbitrary file access)
                if arg_lower in ("-filter_complex", "-filter_complex_script"):
                    # This is allowed but we log it for monitoring
                    logger.warning("Custom filter_complex in FFmpeg command: %s", arg)

            # Check for duration limit if required
            has_limit = False
            for i, arg in enumerate(args):
                if arg.lower() in ("-t", "-frames", "-to"):
                    has_limit = True
                    break

            if REQUIRE_DURATION_LIMIT and not has_limit:
                raise HTTPException(
                    status_code=400,
                    detail={"error": "missing_limit", "detail": "Command must include -t, -frames, or -to"},
                )

            log = work / "ffmpeg.log"
            run_args = ["ffmpeg"] + args if args and args[0] != "ffmpeg" else args

            reporter.update(60, "Executing FFmpeg")
            with log.open("w", encoding="utf-8", errors="ignore") as lf:
                code = await run_ffmpeg_with_timeout(
                    run_args,
                    lf,
                    process_handle=process_handle,
                )

            _record_log_path(save_log(log, "rendi-command"))

            if code != 0:
                logger.error("run-ffmpeg-command failed")
                _flush_logs()
                log_text = ""
                if log.exists():
                    try:
                        log_text = log.read_text(encoding="utf-8", errors="ignore")
                    except Exception:
                        log_text = ""
                detail = {"error": "ffmpeg_failed", "cmd": run_args, "log": log_text}
                if log_rel_path:
                    detail["log_path"] = log_rel_path
                raise HTTPException(status_code=500, detail=detail)

            reporter.update(80, "Publishing outputs")
            for key, path_obj in out_paths.items():
                if path_obj.exists():
                    pub = await publish_file(path_obj, Path(path_obj).suffix or ".bin")
                    published[key] = pub["url"]

            if not published:
                log_text = ""
                if log.exists():
                    try:
                        log_text = log.read_text(encoding="utf-8", errors="ignore")
                    except Exception:
                        log_text = ""
                detail = {"error": "no_outputs_found", "log": log_text}
                if log_rel_path:
                    detail["log_path"] = log_rel_path
                raise HTTPException(status_code=500, detail=detail)

            reporter.update(95, "Complete")
            result_payload = {"outputs": dict(published)}

    except HTTPException as exc:
        detail = exc.detail if isinstance(exc.detail, (dict, str)) else str(exc)
        if isinstance(exc.detail, dict):
            maybe_path = exc.detail.get("log_path")
            if isinstance(maybe_path, str):
                log_rel_path = log_rel_path or maybe_path
        _finalize_sync_job_failure(
            job_id,
            start_time,
            detail,
            log_rel_path,
            failure_message="Failed",
        )
        raise
    except Exception as exc:
        _finalize_sync_job_failure(
            job_id,
            start_time,
            str(exc),
            log_rel_path,
            failure_message="Failed",
        )
        raise
    else:
        duration_ms = (time.time() - start_time) * 1000
        now_ts = time.time()
        progress_snapshot = _drain_job_progress(job_id)
        history_updates: List[Dict[str, Any]] = []
        if progress_snapshot is not None:
            history_updates.extend(progress_snapshot.get("history", []))
        history_updates.append(
            {
                "timestamp": now_ts,
                "progress": 100,
                "message": "Completed",
            }
        )

        with JOBS_LOCK:
            existing = JOBS.get(job_id, {})
            history = list(existing.get("history", []))

        history.extend(history_updates)
        if len(history) > JOB_HISTORY_LIMIT:
            history = history[-JOB_HISTORY_LIMIT:]

        update_payload: Dict[str, Any] = {
            "status": "finished",
            "progress": 100,
            "message": "Completed",
            "updated": now_ts,
            "duration_ms": duration_ms,
            "history": history,
            "error": None,
            "result": result_payload,
        }
        if log_rel_path is not None:
            update_payload["log_path"] = log_rel_path

        with JobTransaction(job_id) as tx:
            tx.set(**update_payload)

        response_payload = {
            "ok": True,
            "outputs": dict(published),
            "job_id": job_id,
            "job_status_url": f"/jobs/{job_id}",
        }
        return response_payload
    finally:
        with JOB_PROCESSES_LOCK:
            JOB_PROCESSES.pop(job_id, None)


@app.post("/v1/run-ffmpeg-command/async")
async def run_rendi_async(job: RendiJob):
    """Queue FFmpeg command for background processing."""

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
    asyncio.create_task(_process_rendi_job_async(job_id, job_copy))

    return {"job_id": job_id, "status_url": f"/jobs/{job_id}"}


@app.post("/v2/run-ffmpeg-job")
async def run_ffmpeg_job(job: FFmpegJobRequest, as_json: bool = False):
    """
    Execute FFmpeg with structured inputs, filter_complex, and outputs.

    Example:
    {
      "task": {
        "inputs": [
          {"file_path": "https://example.com/image.jpg"},
          {"file_path": "https://example.com/song1.mp3"},
          {"file_path": "https://example.com/song2.mp3"}
        ],
        "filter_complex": "[1:a][2:a]concat=n=2:v=0:a=1[aout];[0:v]loop=999:size=1:start=0,scale=1280:720[vout]",
        "outputs": [
          {
            "file": "final-video.mp4",
            "options": ["-map", "[vout]", "-map", "[aout]", "-c:v", "libx264", "-crf", "23", "-c:a", "aac"]
          }
        ]
      }
    }
    """
    logger.info(
        "Starting ffmpeg job with %d inputs and %d outputs",
        len(job.task.inputs),
        len(job.task.outputs),
    )
    check_disk_space(WORK_DIR)

    with TemporaryDirectory(prefix="ffmpeg_job_", dir=str(WORK_DIR)) as workdir:
        work = Path(workdir)

        # Download all input files
        input_paths: List[Path] = []
        for i, input_spec in enumerate(job.task.inputs):
            input_url = _normalize_media_url(str(input_spec.file_path))
            ext = Path(urlparse(input_url).path).suffix or ".bin"
            input_path = work / f"input_{i:03d}{ext}"

            logger.info(
                "Downloading input %d/%d: %s",
                i + 1,
                len(job.task.inputs),
                input_url,
            )
            await _download_to(input_url, input_path, job.headers)
            input_paths.append(input_path)

        # Build FFmpeg command
        cmd = ["ffmpeg", "-y"]

        # Add all inputs with per-input options
        for index, input_spec in enumerate(job.task.inputs):
            input_path = input_paths[index]
            if input_spec.options:
                cmd += input_spec.options
            cmd += ["-i", str(input_path)]

        # Add filter_complex
        cmd += ["-filter_complex", job.task.filter_complex]

        # Process outputs
        output_paths: List[Path] = []
        for output_spec in job.task.outputs:
            output_path = work / output_spec.file
            output_paths.append(output_path)

            # Add output options (which should include -map directives)
            for map_arg in output_spec.maps:
                cmd += ["-map", map_arg]
            cmd += output_spec.options

            # Add output file path
            cmd += [str(output_path)]

        # Execute FFmpeg
        log_path = work / "ffmpeg.log"
        logger.info("Executing FFmpeg command: %s", " ".join(cmd))

        with log_path.open("w", encoding="utf-8", errors="ignore") as lf:
            code = await run_ffmpeg_with_timeout(cmd, lf)

        save_log(log_path, "ffmpeg-job")

        if code != 0:
            logger.error("FFmpeg job failed with exit code %d", code)
            _flush_logs()
            return JSONResponse(
                status_code=500,
                content={
                    "error": "ffmpeg_failed",
                    "exit_code": code,
                    "cmd": cmd,
                    "log": log_path.read_text(encoding="utf-8", errors="ignore"),
                },
            )

        # Check that all outputs were created
        missing_outputs = [p for p in output_paths if not p.exists()]
        if missing_outputs:
            logger.error("FFmpeg succeeded but outputs are missing: %s", missing_outputs)
            _flush_logs()
            return JSONResponse(
                status_code=500,
                content={
                    "error": "missing_outputs",
                    "missing": [str(p.name) for p in missing_outputs],
                    "log": log_path.read_text(encoding="utf-8", errors="ignore"),
                },
            )

        # Publish all outputs
        published_urls: Dict[str, str] = {}
        published_details: Dict[str, Dict[str, str]] = {}
        for output_path in output_paths:
            pub = await publish_file(output_path, output_path.suffix or ".bin")
            published_urls[output_path.name] = pub["url"]
            published_details[output_path.name] = pub
            logger.info("Published output: %s -> %s", output_path.name, pub["rel"])

        if as_json:
            return {
                "ok": True,
                "outputs": published_urls,
            }

        # Return the first output as a file response
        first_output_name = output_paths[0].name
        first_pub = published_details[first_output_name]
        first_path = Path(first_pub["dst"])

        resp = FileResponse(
            first_pub["dst"],
            media_type="video/mp4" if first_path.suffix == ".mp4" else "application/octet-stream",
            filename=first_output_name,
        )
        resp.headers["X-File-URL"] = first_pub["url"]

        # Include other outputs in headers
        if len(published_urls) > 1:
            resp.headers["X-Additional-Outputs"] = json.dumps(
                {k: v for k, v in published_urls.items() if k != first_output_name}
            )

        return resp


def _update_ffmpeg_async_job(
    job_id: str,
    *,
    progress: Optional[int] = None,
    message: Optional[str] = None,
    status: Optional[str] = None,
    **extra: Any,
) -> None:
    with JOBS_LOCK:
        existing = dict(JOBS.get(job_id, {}))

    timestamp = time.time()
    created = existing.get("created", timestamp)
    history = list(existing.get("history", []))
    if progress is not None or message is not None:
        history.append(
            {
                "timestamp": timestamp,
                "progress": progress if progress is not None else existing.get("progress", 0),
                "message": message if message is not None else existing.get("message", ""),
            }
        )
        if len(history) > JOB_HISTORY_LIMIT:
            history = history[-JOB_HISTORY_LIMIT:]

    updated: Dict[str, Any] = {
        **existing,
        "created": created,
        "updated": timestamp,
        "history": history,
    }
    if progress is not None:
        updated["progress"] = progress
    if message is not None:
        updated["message"] = message
    if status is not None:
        updated["status"] = status
    updated.update(extra)

    with JOBS_LOCK:
        JOBS[job_id] = updated


@app.post("/v2/run-ffmpeg-job/async")
async def run_ffmpeg_job_async(job: FFmpegJobRequest):
    """Queue FFmpeg job for background processing."""
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
    asyncio.create_task(_process_ffmpeg_job_async(job_id, job_copy))

    return {"job_id": job_id, "status_url": f"/jobs/{job_id}"}


async def _process_ffmpeg_job_async(job_id: str, job: FFmpegJobRequest) -> None:
    """Background worker for async FFmpeg jobs."""
    now = time.time()
    _update_ffmpeg_async_job(
        job_id,
        progress=10,
        message="Job started",
        status="processing",
        started=now,
    )

    start = time.time()
    struct_logger.info("job_started", job_id=job_id, job_type="ffmpeg_job")

    process_handle = JobProcessHandle()
    with JOB_PROCESSES_LOCK:
        JOB_PROCESSES[job_id] = process_handle

    log_rel_path: Optional[str] = None

    def mark_killed(message: str = "Killed", *, progress: Optional[int] = None) -> None:
        current_progress = progress if progress is not None else 100
        extra: Dict[str, Any] = {
            "progress": current_progress,
            "message": message,
            "status": "killed",
        }
        if log_rel_path is not None:
            extra["log_path"] = log_rel_path
        _update_ffmpeg_async_job(job_id, **extra)

    try:
        check_disk_space(WORK_DIR)

        with TemporaryDirectory(prefix="ffmpeg_job_", dir=str(WORK_DIR)) as workdir:
            work = Path(workdir)

            check_disk_space(LOGS_DIR)

            now_dt = datetime.now(timezone.utc)
            day = now_dt.strftime("%Y%m%d")
            log_folder = LOGS_DIR / day
            log_folder.mkdir(parents=True, exist_ok=True)
            check_disk_space(log_folder)
            log_name = (
                now_dt.strftime("%Y%m%d_%H%M%S_") + _rand() + "_ffmpeg-job-async.log"
            )
            persistent_log_path = log_folder / log_name
            try:
                log_rel_path = str(persistent_log_path.relative_to(LOGS_DIR))
            except ValueError:
                log_rel_path = str(persistent_log_path)
            except Exception:
                log_rel_path = str(persistent_log_path)
            _update_ffmpeg_async_job(job_id, log_path=log_rel_path)

            input_paths: List[Path] = []
            total_inputs = max(len(job.task.inputs), 1)
            logger.info("[%s] STEP 1: Starting input downloads", job_id)
            for index, input_spec in enumerate(job.task.inputs):
                progress = 10 + int((index / total_inputs) * 40)
                _update_ffmpeg_async_job(
                    job_id,
                    progress=progress,
                    message=f"Downloading input {index + 1}/{len(job.task.inputs)}",
                )

                input_url = _normalize_media_url(str(input_spec.file_path))
                ext = Path(urlparse(input_url).path).suffix or ".bin"
                input_path = work / f"input_{index:03d}{ext}"
                logger.info(
                    "[%s] Downloading input %d/%d: %s",
                    job_id,
                    index + 1,
                    len(job.task.inputs),
                    input_url,
                )
                await _download_to(input_url, input_path, job.headers)
                input_paths.append(input_path)
                logger.info(
                    "[%s] Downloaded input %d/%d", job_id, index + 1, len(job.task.inputs)
                )

            _update_ffmpeg_async_job(
                job_id,
                progress=55,
                message="Inputs downloaded",
            )
            logger.info("[%s] STEP 2: All inputs downloaded", job_id)

            if input_paths:
                try:
                    probe_cmd = [
                        "ffprobe",
                        "-v",
                        "error",
                        "-select_streams",
                        "v:0",
                        "-count_frames",
                        "-show_entries",
                        "stream=nb_read_frames,duration,r_frame_rate",
                        "-of",
                        "json",
                        str(input_paths[0]),
                    ]
                    probe_result = await asyncio.to_thread(
                        subprocess.run,
                        probe_cmd,
                        capture_output=True,
                        text=True,
                        timeout=30,
                    )
                    if probe_result.returncode == 0 and probe_result.stdout:
                        try:
                            probe_data = json.loads(probe_result.stdout)
                        except json.JSONDecodeError:
                            probe_data = {}
                        stream_info = (probe_data.get("streams") or [{}])[0]
                        nb_frames_raw = stream_info.get("nb_read_frames")
                        frame_count: Optional[int] = None
                        if isinstance(nb_frames_raw, str) and nb_frames_raw.isdigit():
                            frame_count = int(nb_frames_raw)
                        elif isinstance(nb_frames_raw, (int, float)):
                            frame_count = int(nb_frames_raw)
                        else:
                            duration_raw = stream_info.get("duration")
                            rate_raw = stream_info.get("r_frame_rate")
                            if duration_raw and rate_raw and rate_raw != "0/0":
                                try:
                                    num, den = rate_raw.split("/")
                                    fps = float(num) / float(den or 1)
                                    frame_count = int(float(duration_raw) * fps)
                                except (ValueError, ZeroDivisionError):
                                    frame_count = None
                        if frame_count and frame_count > 0:
                            _update_ffmpeg_async_job(job_id, total_frames=frame_count)
                except Exception as probe_exc:
                    logger.debug(
                        "[%s] Unable to determine total frames via ffprobe: %s",
                        job_id,
                        probe_exc,
                    )

            logger.info("[%s] STEP 3: Building FFmpeg command", job_id)
            cmd = ["ffmpeg", "-y", "-stats", "-stats_period", "1"]
            for index, input_spec in enumerate(job.task.inputs):
                input_path = input_paths[index]
                if input_spec.options:
                    cmd += input_spec.options
                cmd += ["-i", str(input_path)]
            cmd += ["-filter_complex", job.task.filter_complex]

            output_paths: List[Path] = []
            for output_spec in job.task.outputs:
                output_path = work / output_spec.file
                output_paths.append(output_path)
                for map_arg in output_spec.maps:
                    cmd += ["-map", map_arg]
                cmd += output_spec.options
                cmd += [str(output_path)]

            _update_ffmpeg_async_job(
                job_id,
                progress=60,
                message="Processing with FFmpeg",
            )

            last_update_time = time.time()
            current_progress = 60
            last_logged_time = time.time()

            def smart_progress_updater(line: str) -> None:
                """Update progress based on time elapsed and FFmpeg metrics."""

                logger.debug("[%s] FFmpeg line: %s", job_id, line[:100])
                nonlocal last_update_time, current_progress, last_logged_time
                now = time.time()

                if "time=" in line or "frame=" in line:
                    logger.debug("[%s] Progress line: %s", job_id, line.strip())

                if "frame=" in line or "time=" in line or "fps=" in line:
                    logger.info("[%s] FFmpeg progress: %s", job_id, line.strip())

                if now - last_update_time >= 3:
                    if current_progress < 95:
                        increment = 2 if current_progress < 80 else 1
                        current_progress = min(95, current_progress + increment)

                    metrics: Dict[str, str] = {}
                    detail_parts: List[str] = []

                    time_match = re.search(r"time=(\d+):(\d+):(\d+(?:\.\d+)?)", line)
                    if time_match:
                        hours, minutes, seconds = time_match.groups()
                        total_seconds = int(hours) * 3600 + int(minutes) * 60 + float(seconds)
                        metrics["time"] = f"{total_seconds:.1f}"
                        detail_parts.append(f"time={total_seconds:.1f}s")

                    fps_match = re.search(r"fps=\s*(\d+\.?\d*)", line)
                    if fps_match:
                        metrics["fps"] = fps_match.group(1)
                        detail_parts.append(f"fps={fps_match.group(1)}")

                    speed_match = re.search(r"speed=\s*(\d+\.?\d*)x", line)
                    if speed_match:
                        metrics["speed"] = speed_match.group(1)
                        detail_parts.append(f"speed={speed_match.group(1)}x")

                    frame_match = re.search(r"frame=\s*(\d+)", line)
                    if frame_match:
                        metrics["frame"] = frame_match.group(1)

                    detail = " | ".join(detail_parts) if detail_parts else "Active"

                    logger.info("[%s] Extracted metrics: %s", job_id, metrics)

                    _update_ffmpeg_async_job(
                        job_id,
                        progress=current_progress,
                        message="Processing with FFmpeg",
                        detail=detail,
                        ffmpeg_stats=metrics if metrics else None,
                    )
                    last_update_time = now

                if now - last_logged_time >= 30:
                    logger.info(
                        "[%s] Still processing (progress: %d%%, elapsed: %.0fs)",
                        job_id,
                        current_progress,
                        now - start,
                    )
                    last_logged_time = now

            async def enhanced_watchdog() -> None:
                """Monitor job health, enforce timeout, and handle kill signals."""
                check_interval = 10  # Check every 10 seconds
                last_log_elapsed = 0.0  # Track last logged elapsed time

                try:
                    while True:
                        await asyncio.sleep(check_interval)
                        elapsed = time.time() - start

                        # Check if job was marked as killed
                        if _job_marked_killed(job_id):
                            logger.warning(
                                "[%s] Watchdog detected kill signal after %.0fs - terminating FFmpeg",
                                job_id,
                                elapsed,
                            )
                            # Try to terminate the process
                            with JOB_PROCESSES_LOCK:
                                handle = JOB_PROCESSES.get(job_id)
                            if handle:
                                process = getattr(handle, "process", None)
                                if process:
                                    try:
                                        if isinstance(process, asyncio.subprocess.Process):
                                            if process.returncode is None:
                                                process.terminate()
                                                logger.info("[%s] Watchdog sent SIGTERM to FFmpeg", job_id)
                                        else:
                                            if getattr(process, "poll", lambda: None)() is None:
                                                process.terminate()
                                                logger.info("[%s] Watchdog sent SIGTERM to FFmpeg", job_id)
                                    except Exception as term_exc:
                                        logger.error(
                                            "[%s] Watchdog failed to terminate process: %s",
                                            job_id,
                                            term_exc,
                                        )
                            return  # Exit watchdog - let main flow handle cleanup

                        # Check for timeout
                        if elapsed >= FFMPEG_TIMEOUT_SECONDS:
                            logger.error(
                                "[%s] Watchdog detected timeout (%.0fs >= %ds) - killing FFmpeg",
                                job_id,
                                elapsed,
                                FFMPEG_TIMEOUT_SECONDS,
                            )
                            # Force kill the process
                            with JOB_PROCESSES_LOCK:
                                handle = JOB_PROCESSES.get(job_id)
                            if handle:
                                process = getattr(handle, "process", None)
                                if process:
                                    try:
                                        if isinstance(process, asyncio.subprocess.Process):
                                            if process.returncode is None:
                                                process.kill()
                                                logger.warning("[%s] Watchdog sent SIGKILL to FFmpeg", job_id)
                                        else:
                                            if getattr(process, "poll", lambda: None)() is None:
                                                process.kill()
                                                logger.warning("[%s] Watchdog sent SIGKILL to FFmpeg", job_id)
                                    except Exception as kill_exc:
                                        logger.error(
                                            "[%s] Watchdog failed to kill process: %s",
                                            job_id,
                                            kill_exc,
                                        )
                            return  # Exit watchdog - timeout will be caught by main flow

                        # Periodic progress logging (every 30 seconds)
                        if elapsed - last_log_elapsed >= 30:
                            logger.info(
                                "[%s] Watchdog: %.0fs elapsed, progress: %d%%, still healthy",
                                job_id,
                                elapsed,
                                current_progress,
                            )
                            last_log_elapsed = elapsed

                except asyncio.CancelledError:
                    logger.debug("[%s] Watchdog cancelled normally", job_id)
                    raise

            watchdog_task = asyncio.create_task(enhanced_watchdog())

            logger.info(
                "[%s] STEP 4: Starting FFmpeg (command: %s)",
                job_id,
                " ".join(cmd[:15]) + ("..." if len(cmd) > 15 else ""),
            )

            if _job_marked_killed(job_id):
                logger.info("[%s] Job killed before FFmpeg launch", job_id)
                mark_killed("Killed before FFmpeg start", progress=current_progress)
                watchdog_task.cancel()
                try:
                    await watchdog_task
                except asyncio.CancelledError:
                    pass
                return

            try:
                with persistent_log_path.open(
                    "w",
                    encoding="utf-8",
                    errors="ignore",
                    buffering=1,
                ) as lf:
                    code = await run_ffmpeg_with_timeout(
                        cmd,
                        lf,
                        progress_parser=smart_progress_updater,
                        process_handle=process_handle,
                    )
            finally:
                watchdog_task.cancel()
                try:
                    await watchdog_task
                except asyncio.CancelledError:
                    pass

            logger.info(
                "[%s] STEP 5: FFmpeg completed with exit code %d (took %.1fs)",
                job_id,
                code,
                time.time() - start,
            )

            if code != 0:
                if _job_marked_killed(job_id):
                    logger.info(
                        "[%s] Job killed after FFmpeg exit code %d",
                        job_id,
                        code,
                    )
                    mark_killed("Killed after FFmpeg", progress=current_progress)
                    return
                error_text = persistent_log_path.read_text(
                    encoding="utf-8", errors="ignore"
                )
                update_kwargs: Dict[str, Any] = {
                    "progress": 100,
                    "message": "FFmpeg failed",
                    "status": "failed",
                    "error": error_text,
                }
                if log_rel_path is not None:
                    update_kwargs["log_path"] = log_rel_path
                _update_ffmpeg_async_job(job_id, **update_kwargs)
                struct_logger.error(
                    "job_failed",
                    job_id=job_id,
                    job_type="ffmpeg_job",
                    exit_code=code,
                )
                return

            logger.info(f"[{job_id}] STEP 6: Checking output files")
            missing_outputs = [p for p in output_paths if not p.exists()]
            if missing_outputs:
                if _job_marked_killed(job_id):
                    logger.info(
                        "[%s] Job killed while checking outputs",
                        job_id,
                    )
                    mark_killed("Killed while checking outputs")
                    return
                missing_kwargs: Dict[str, Any] = {
                    "progress": 100,
                    "message": "Missing outputs",
                    "status": "failed",
                    "error": {"missing": [p.name for p in missing_outputs]},
                }
                if log_rel_path is not None:
                    missing_kwargs["log_path"] = log_rel_path
                _update_ffmpeg_async_job(job_id, **missing_kwargs)
                struct_logger.error(
                    "job_failed",
                    job_id=job_id,
                    job_type="ffmpeg_job",
                    error="missing_outputs",
                    missing=[p.name for p in missing_outputs],
                )
                return

            if _job_marked_killed(job_id):
                logger.info("[%s] Job killed before publishing outputs", job_id)
                mark_killed("Killed before publishing outputs", progress=current_progress)
                return

            _update_ffmpeg_async_job(
                job_id,
                progress=90,
                message="Publishing outputs",
            )

            published_outputs: Dict[str, str] = {}
            logger.info("[%s] STEP 7: Publishing outputs", job_id)
            for idx, output_path in enumerate(output_paths):
                logger.info(
                    "[%s] Publishing output %d/%d: %s",
                    job_id,
                    idx + 1,
                    len(output_paths),
                    output_path.name,
                )
                try:
                    pub = await asyncio.wait_for(
                        publish_file(output_path, output_path.suffix or ".bin"),
                        timeout=180,
                    )
                except asyncio.TimeoutError:
                    logger.error(
                        "[%s] TIMEOUT publishing output %s after 3 minutes",
                        job_id,
                        output_path.name,
                    )
                    raise HTTPException(
                        status_code=500,
                        detail=f"Timeout publishing {output_path.name}",
                    )
                published_outputs[output_path.name] = pub["url"]
                logger.info(
                    "[%s] Published output %d/%d: %s -> %s",
                    job_id,
                    idx + 1,
                    len(output_paths),
                    output_path.name,
                    pub["rel"],
                )

            logger.info("[%s] STEP 8: All outputs published successfully", job_id)
            duration_ms = (time.time() - start) * 1000.0
            if _job_marked_killed(job_id):
                logger.info("[%s] Job completed but was killed", job_id)
                mark_killed("Killed after publishing outputs")
                return
            logger.info("[%s] STEP 9: Updating job to finished status", job_id)
            success_kwargs: Dict[str, Any] = {
                "progress": 100,
                "message": "Completed",
                "status": "finished",
                "result": {"outputs": published_outputs},
                "duration_ms": duration_ms,
            }
            if log_rel_path is not None:
                success_kwargs["log_path"] = log_rel_path
            _update_ffmpeg_async_job(job_id, **success_kwargs)
            logger.info("[%s] STEP 10: Job marked as finished", job_id)
            struct_logger.info(
                "job_completed",
                job_id=job_id,
                job_type="ffmpeg_job",
                duration_ms=duration_ms,
            )

    except HTTPException as exc:
        if _job_marked_killed(job_id):
            logger.info("[%s] Job failed after kill: %s", job_id, exc)
            mark_killed("Killed")
            return

        detail = exc.detail if isinstance(exc.detail, (str, dict)) else str(exc.detail)
        status_code = exc.status_code if hasattr(exc, "status_code") else 500
        logger.error("[%s] Async FFmpeg job failed with HTTP error: %s", job_id, detail)
        http_error_kwargs: Dict[str, Any] = {
            "progress": 100,
            "message": "Failed",
            "status": "failed",
            "error": detail,
            "status_code": status_code,
        }
        if log_rel_path is not None:
            http_error_kwargs["log_path"] = log_rel_path
        _update_ffmpeg_async_job(job_id, **http_error_kwargs)
        struct_logger.error(
            "job_failed",
            job_id=job_id,
            job_type="ffmpeg_job",
            status_code=status_code,
            error=detail,
        )
        return

    except Exception as exc:
        if _job_marked_killed(job_id):
            logger.info("[%s] Job stopped after kill: %s", job_id, exc)
            mark_killed("Killed")
            return

        logger.exception("[%s] Async FFmpeg job failed", job_id)
        exception_kwargs: Dict[str, Any] = {
            "progress": 100,
            "message": "Failed",
            "status": "failed",
            "error": str(exc),
        }
        if log_rel_path is not None:
            exception_kwargs["log_path"] = log_rel_path
        _update_ffmpeg_async_job(job_id, **exception_kwargs)
        struct_logger.error(
            "job_failed",
            job_id=job_id,
            job_type="ffmpeg_job",
            error=str(exc),
        )
        return
    finally:
        logger.info(f"[{job_id}] CLEANUP: Removing from JOB_PROCESSES")
        with JOB_PROCESSES_LOCK:
            JOB_PROCESSES.pop(job_id, None)
        logger.info(f"[{job_id}] CLEANUP: Complete")

        with JOBS_LOCK:
            current_status = dict(JOBS.get(job_id, {}))
        if current_status and current_status.get("status") == "processing":
            logger.error("[%s] Job left in processing state after worker exit", job_id)
            _update_ffmpeg_async_job(
                job_id,
                progress=current_status.get("progress", 0),
                message="Failed",
                status="failed",
                error="Job ended without final status update",
            )

# ---------- audio endpoints ----------


@app.post("/v1/audio/tempo")
async def change_audio_tempo(request: TempoRequest):
    job_id = uuid4().hex[:8]
    start = time.perf_counter()
    record: Dict[str, Any] = {
        "id": job_id,
        "input": None,
        "output": request.output_name,
        "tempo": request.tempo,
        "created_at": datetime.now(timezone.utc).isoformat(),
        "status": "processing",
        "processing_time": None,
        "input_url": str(request.input_url),
    }
    with TEMPO_HISTORY_LOCK:
        TEMPO_HISTORY.appendleft(record)

    struct_logger.info(
        "audio_tempo_started",
        job_id=job_id,
        tempo=request.tempo,
        input_url=str(request.input_url),
        output_name=request.output_name,
    )

    try:
        check_disk_space(WORK_DIR)
        with TemporaryDirectory(prefix="tempo_", dir=str(WORK_DIR)) as workdir:
            work = Path(workdir)
            parsed = urlparse(str(request.input_url))
            candidate_name = Path(parsed.path).name or "input.audio"
            input_name = candidate_name or "input.audio"
            if not Path(input_name).suffix:
                input_name = f"{input_name}.bin"
            input_path = work / input_name

            await _download_to(str(request.input_url), input_path, None)
            input_size_bytes = input_path.stat().st_size
            record["input"] = input_name
            record["input_size"] = format_file_size(input_size_bytes)

            output_path = work / request.output_name
            output_ext = output_path.suffix.lower().lstrip(".")
            if output_ext == "wav":
                codec = "pcm_s16le"
                extra_args: List[str] = []
            elif output_ext in {"ogg", "oga"}:
                codec = "libvorbis"
                extra_args = ["-b:a", "192k"]
            elif output_ext == "flac":
                codec = "flac"
                extra_args = []
            else:
                codec = "libmp3lame"
                extra_args = ["-b:a", "192k"]

            cmd = [
                "ffmpeg",
                "-y",
                "-i",
                str(input_path),
                "-filter:a",
                f"atempo={request.tempo}",
                "-ar",
                "44100",
                "-ac",
                "2",
                "-c:a",
                codec,
                *extra_args,
                str(output_path),
            ]

            try:
                result = subprocess.run(
                    cmd,
                    capture_output=True,
                    text=True,
                    timeout=FFMPEG_TIMEOUT_SECONDS,
                )
            except subprocess.TimeoutExpired as exc:
                record["status"] = "failed"
                record["error"] = "processing_timeout"
                _flush_logs()
                struct_logger.error(
                    "audio_tempo_failed",
                    job_id=job_id,
                    tempo=request.tempo,
                    error="processing_timeout",
                )
                raise HTTPException(
                    status_code=504,
                    detail={
                        "error": "processing_timeout",
                        "cmd": cmd,
                        "stderr": (exc.stderr.decode("utf-8", "ignore") if isinstance(exc.stderr, bytes) else exc.stderr),
                    },
                ) from exc

            log_path = work / "ffmpeg.log"
            try:
                log_path.write_text(result.stderr or "", encoding="utf-8", errors="ignore")
            except Exception:
                pass
            save_log(log_path, "audio-tempo")

            if result.returncode != 0 or not output_path.exists():
                record["status"] = "failed"
                record["error"] = "ffmpeg_failed"
                _flush_logs()
                struct_logger.error(
                    "audio_tempo_failed",
                    job_id=job_id,
                    tempo=request.tempo,
                    error="ffmpeg_failed",
                )
                raise HTTPException(
                    status_code=500,
                    detail={
                        "error": "ffmpeg_failed",
                        "stderr": result.stderr,
                        "stdout": result.stdout,
                        "cmd": cmd,
                    },
                )

            published = await publish_file(output_path, Path(output_path).suffix or ".bin")
            elapsed = time.perf_counter() - start
            duration_text = format_duration(elapsed)
            record["status"] = "done"
            record["processing_time"] = duration_text
            record["download_url"] = published["url"]
            record["output_file"] = published["rel"]

            struct_logger.info(
                "audio_tempo_completed",
                job_id=job_id,
                tempo=request.tempo,
                input_size_bytes=input_size_bytes,
                output_rel=published["rel"],
                duration_seconds=round(elapsed, 4),
            )

            return {
                "status": "success",
                "job_id": job_id,
                "tempo": request.tempo,
                "input_size": format_file_size(input_size_bytes),
                "output_file": published["rel"],
                "download_url": published["url"],
                "processing_time": duration_text,
            }
    except HTTPException as exc:
        record["status"] = "failed"
        if "processing_time" not in record or record["processing_time"] is None:
            record["processing_time"] = format_duration(time.perf_counter() - start)
        if "error" not in record:
            detail = exc.detail
            if isinstance(detail, str):
                record["error"] = detail
            else:
                try:
                    record["error"] = json.dumps(detail)
                except Exception:
                    record["error"] = "unknown_error"
        error_key = record.get("error", "http_error")
        if error_key not in {"processing_timeout", "ffmpeg_failed"}:
            struct_logger.error(
                "audio_tempo_failed",
                job_id=job_id,
                tempo=request.tempo,
                error=error_key,
            )
        _flush_logs()
        raise
    except Exception as exc:
        record["status"] = "failed"
        record["error"] = str(exc)
        record["processing_time"] = format_duration(time.perf_counter() - start)
        struct_logger.error(
            "audio_tempo_failed",
            job_id=job_id,
            tempo=request.tempo,
            error=str(exc),
        )
        _flush_logs()
        raise


@app.get("/v1/audio/tempo")
def list_tempo_jobs() -> List[Dict[str, Any]]:
    with TEMPO_HISTORY_LOCK:
        return [dict(item) for item in TEMPO_HISTORY]


@app.get("/v1/audio/tempo/{job_id}/status")
def get_tempo_job_status(job_id: str) -> Dict[str, Any]:
    with TEMPO_HISTORY_LOCK:
        for item in TEMPO_HISTORY:
            if item.get("id") == job_id:
                return dict(item)
    raise HTTPException(status_code=404, detail="job_not_found")


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
    if PROBE_CACHE_TTL > 0:
        with PROBE_CACHE_LOCK:
            cached = PROBE_CACHE.get(cache_key)
    if cached is not None:
        result, _timestamp = cached
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
            PROBE_CACHE.set(cache_key, result)
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
