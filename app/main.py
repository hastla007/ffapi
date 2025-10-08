import os, io, shlex, json, subprocess, random, string, shutil, time, asyncio
from datetime import datetime, timedelta
from pathlib import Path
from tempfile import TemporaryDirectory
from typing import Dict, List, Optional, Literal

import requests
from fastapi import FastAPI, UploadFile, File, HTTPException, Query
from fastapi.responses import FileResponse, HTMLResponse, JSONResponse, RedirectResponse, PlainTextResponse
from fastapi.staticfiles import StaticFiles
from pydantic import BaseModel, HttpUrl, field_validator

app = FastAPI()

# --------- config ---------
PUBLIC_DIR = Path(os.getenv("PUBLIC_DIR", "/data/public")).resolve()
PUBLIC_DIR.mkdir(parents=True, exist_ok=True)

# Optional dedicated work directory to keep temp files on the same volume
WORK_DIR = Path(os.getenv("WORK_DIR", "/data/work")).resolve()
WORK_DIR.mkdir(parents=True, exist_ok=True)

# Logs directory for persistent ffmpeg logs
LOGS_DIR = Path(os.getenv("LOGS_DIR", "/data/logs")).resolve()
LOGS_DIR.mkdir(parents=True, exist_ok=True)

# Application log file (captures stdout/stderr)
APP_LOG_FILE = LOGS_DIR / "application.log"

RETENTION_DAYS = int(os.getenv("RETENTION_DAYS", "7"))
PUBLIC_BASE_URL = os.getenv("PUBLIC_BASE_URL")  # e.g. "http://10.120.2.5:3000"

MAX_FILE_SIZE_MB = int(os.getenv("MAX_FILE_SIZE_MB", "2048"))
MAX_FILE_SIZE_BYTES = MAX_FILE_SIZE_MB * 1024 * 1024
FFMPEG_TIMEOUT_SECONDS = int(os.getenv("FFMPEG_TIMEOUT_SECONDS", str(2 * 60 * 60)))
MIN_FREE_SPACE_MB = int(os.getenv("MIN_FREE_SPACE_MB", "1000"))
UPLOAD_CHUNK_SIZE = 1024 * 1024
PUBLIC_CLEANUP_INTERVAL_SECONDS = int(os.getenv("PUBLIC_CLEANUP_INTERVAL_SECONDS", "3600"))

# Mount static /files
app.mount("/files", StaticFiles(directory=str(PUBLIC_DIR)), name="files")


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

# Create file handler with immediate flush
file_handler = logging.FileHandler(str(APP_LOG_FILE))
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
                lines = handle.readlines()
            return "".join(lines[-num_lines:]) if lines else ""
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
            rows.append(f"<tr><td>{day.name}</td><td><a href='{rel}'>{f.name}</a></td><td>{size_mb:.2f} MB</td></tr>")
    html = f"""
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
    return HTMLResponse(html)


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
            rows.append(f"<tr><td>{day.name}</td><td>{f.name}</td><td>{operation}</td><td>{size_kb:.1f} KB</td><td><a href='/logs/view?path={day.name}/{f.name}' target='_blank'>View</a></td></tr>")
    html = f"""
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
    return HTMLResponse(html)


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
    
    html = f"""
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
        <a href="/documentation">API Docs</a>
      </nav>
      <h2>FFmpeg & Container Information</h2>
      
      <div class="section">
        <h3>FFmpeg Version & Build</h3>
        <pre>{version_output}</pre>
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
          Page loaded: {current_time} | {log_info}
        </div>
        <pre class="logs" id="logContainer">{app_logs}</pre>
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
    return HTMLResponse(html)


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
  <div class="desc">Health check endpoint</div>
  <div class="response">Returns: {"ok": true}</div>
  <div class="example">Example:<br>curl http://localhost:3000/health</div>
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
    
    html = f"""
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
    return HTMLResponse(html)


@app.get("/health")
def health():
    logger.info("Health check requested")
    return {"ok": True}


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

def _download_to(url: str, dest: Path, headers: Optional[Dict[str, str]] = None, max_retries: int = 3, chunk_size: int = 1024 * 1024):
    """Download file with retry, resume, and progress tracking.

    Args:
        url: URL to download from
        dest: Destination file path
        headers: Optional HTTP headers to forward
        max_retries: Number of retry attempts (default: 3)
        chunk_size: Download chunk size in bytes (default: 1MB)
    """
    base_headers: Dict[str, str] = {}
    if headers:
        for k, v in headers.items():
            if k.lower() in ALLOWED_FORWARD_HEADERS_LOWER:
                base_headers[k] = v

    dest.parent.mkdir(parents=True, exist_ok=True)
    check_disk_space(dest.parent)

    resume_supported = True

    for attempt in range(max_retries):
        try:
            req_hdr = dict(base_headers)

            if not resume_supported and dest.exists():
                try:
                    dest.unlink()
                except Exception as cleanup_exc:
                    logger.warning("Failed to reset partial download %s: %s", dest, cleanup_exc)
            existing_size = dest.stat().st_size if dest.exists() else 0

            use_resume = resume_supported and existing_size > 0

            if use_resume:
                req_hdr['Range'] = f'bytes={existing_size}-'
                logger.info(f"Resuming download from {existing_size/1024/1024:.1f}MB: {url}")

            # Dynamic timeout: 10 minutes base
            timeout = 600

            with requests.get(url, headers=req_hdr, stream=True, timeout=timeout) as r:
                r.raise_for_status()

                # Check if server supports Range requests
                if use_resume:
                    if r.status_code == 206:
                        # Server supports Range, we got partial content
                        logger.info(f"✓ Server supports resume for: {url}")
                    elif r.status_code == 200:
                        # Server doesn't support Range, gave us full file
                        logger.warning(f"⚠ Server doesn't support resume, downloading from start: {url}")
                        mode = 'wb'  # Switch to overwrite mode
                        existing_size = 0
                        resume_supported = False
                    else:
                        mode = 'ab'
                else:
                    mode = 'wb'

                # Get total size
                if 'content-length' in r.headers:
                    content_length = int(r.headers['content-length'])
                    if r.status_code == 206:
                        # Partial content - add existing size
                        total_size = existing_size + content_length
                    else:
                        # Full content
                        total_size = content_length
                else:
                    total_size = 0
                
                # Download with progress tracking
                downloaded = existing_size
                last_log = downloaded

                with dest.open(mode) as f:
                    for chunk in r.iter_content(chunk_size):
                        if chunk:
                            f.write(chunk)
                            downloaded += len(chunk)
                            
                            # Log progress every 50MB
                            if downloaded - last_log >= 50 * 1024 * 1024:
                                if total_size:
                                    percent = (downloaded / total_size) * 100
                                    logger.info(f"Download progress: {percent:.0f}% ({downloaded/1024/1024:.0f}MB/{total_size/1024/1024:.0f}MB)")
                                else:
                                    logger.info(f"Downloaded: {downloaded/1024/1024:.0f}MB")
                                last_log = downloaded

                # Verify size if we know what to expect
                if total_size and downloaded != total_size:
                    raise Exception(f"Download incomplete: got {downloaded} bytes, expected {total_size}")

                logger.info(f"Download complete: {downloaded/1024/1024:.1f}MB - {url}")
                return  # Success!

        except Exception as e:
            if attempt < max_retries - 1:
                wait_time = 2 ** attempt  # Exponential backoff: 1s, 2s, 4s
                logger.warning(f"Download failed (attempt {attempt + 1}/{max_retries}): {e}")
                logger.warning(f"Retrying in {wait_time} seconds...")
                time.sleep(wait_time)
            else:
                # Final attempt failed
                logger.error(f"Download failed after {max_retries} attempts: {e}")
                _flush_logs()
                # Clean up partial file on final failure
                if dest.exists():
                    try:
                        dest.unlink()
                    except Exception as cleanup_exc:
                        logger.warning("Failed to remove partial download %s: %s", dest, cleanup_exc)
                        _flush_logs()
                raise


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
def video_concat_from_urls(job: ConcatJob, as_json: bool = False):
    logger.info(f"Starting concat: {len(job.clips)} clips, {job.width}x{job.height}@{job.fps}fps")
    check_disk_space(WORK_DIR)
    with TemporaryDirectory(prefix="concat_", dir=str(WORK_DIR)) as workdir:
        work = Path(workdir)
        norm = []
        for i, url in enumerate(job.clips):
            raw = work / f"in_{i:03d}.bin"
            _download_to(str(url), raw, None)
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
def video_concat_alias(job: ConcatAliasJob, as_json: bool = False):
    clip_list = job.clips or job.urls
    if not clip_list:
        raise HTTPException(status_code=422, detail="Provide 'clips' (preferred) or 'urls' array of video URLs")
    cj = ConcatJob(clips=clip_list, width=job.width, height=job.height, fps=job.fps)
    return video_concat_from_urls(cj, as_json=as_json)


@app.post("/compose/from-urls")
def compose_from_urls(job: ComposeFromUrlsJob, as_json: bool = False):
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

        _download_to(str(job.video_url), v_path, job.headers)
        has_audio = False
        has_bgm = False
        if job.audio_url:
            _download_to(str(job.audio_url), a_path, job.headers)
            has_audio = True
        if job.bgm_url:
            _download_to(str(job.bgm_url), b_path, job.headers)
            has_bgm = True

        dur_s = f"{job.duration_ms/1000:.3f}"
        inputs = ["-i", str(v_path)]
        if has_audio: inputs += ["-i", str(a_path)]
        if has_bgm:   inputs += ["-i", str(b_path)]

        maps = ["-map", "0:v:0"]
        cmd = ["ffmpeg", "-y"] + inputs + [
            "-t", dur_s,
            "-vf", f"scale={job.width}:{job.height},fps={job.fps}",
            "-c:v", "libx264", "-preset", "medium", "-pix_fmt", "yuv420p",
            "-movflags", "+faststart",
        ]
        if has_audio and has_bgm:
            af = f"[1:a]anull[a1];[2:a]volume={job.bgm_volume}[a2];[a1][a2]amix=inputs=2:normalize=0:duration=shortest[aout]"
            cmd += ["-filter_complex", af, "-c:a", "aac", "-b:a", "128k", "-ar", "48000"]
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
            return JSONResponse(status_code=500, content={"error": "ffmpeg_failed", "cmd": cmd, "log": log_path.read_text()})

        pub = publish_file(out_path, ".mp4")
        if as_json:
            return {"ok": True, "file_url": pub["url"], "path": pub["dst"]}
        resp = FileResponse(pub["dst"], media_type="video/mp4", filename=os.path.basename(pub["dst"]))
        resp.headers["X-File-URL"] = pub["url"]
        return resp


@app.post("/compose/from-tracks")
def compose_from_tracks(job: TracksComposeJob, as_json: bool = False):
    video_urls: List[str] = []
    audio_urls: List[str] = []
    max_dur = 0
    for t in job.tracks:
        for k in t.keyframes:
            if k.duration is not None:
                max_dur = max(max_dur, int(k.duration))
            if k.url:
                if t.type == "video":
                    video_urls.append(str(k.url))
                elif t.type == "audio":
                    audio_urls.append(str(k.url))
    if not video_urls:
        raise HTTPException(status_code=400, detail="No video URL found in tracks")
    if max_dur <= 0:
        max_dur = 30000

    check_disk_space(WORK_DIR)
    with TemporaryDirectory(prefix="tracks_", dir=str(WORK_DIR)) as workdir:
        work = Path(workdir)
        v_in = work / "video.mp4"
        _download_to(video_urls[0], v_in, None)
        a_ins: List[Path] = []
        for i, url in enumerate(audio_urls):
            p = work / f"aud_{i:03d}.bin"
            _download_to(url, p, None)
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
def run_rendi(job: RendiJob):
    check_disk_space(WORK_DIR)
    with TemporaryDirectory(prefix="rendi_", dir=str(WORK_DIR)) as workdir:
        work = Path(workdir)
        resolved = {}
        for key, url in job.input_files.items():
            p = work / f"{key}"
            _download_to(str(url), p, None)
            resolved[key] = str(p)

        out_paths: Dict[str, Path] = {}
        for key, name in job.output_files.items():
            out_paths[key] = work / name

        cmd_text = job.ffmpeg_command
        for k, p in resolved.items():
            cmd_text = cmd_text.replace("{{" + k + "}}", p)
        for k, p in out_paths.items():
            cmd_text = cmd_text.replace("{{" + k + "}}", str(p))

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
