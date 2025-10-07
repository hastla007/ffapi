import os, io, shlex, json, subprocess, random, string, shutil, time
from datetime import datetime, timedelta
from pathlib import Path
from tempfile import TemporaryDirectory
from typing import Dict, List, Optional, Literal

import requests
from fastapi import FastAPI, UploadFile, File, HTTPException, Query
from fastapi.responses import FileResponse, HTMLResponse, JSONResponse, RedirectResponse, PlainTextResponse
from fastapi.staticfiles import StaticFiles
from pydantic import BaseModel, HttpUrl

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

# Mount static /files
app.mount("/files", StaticFiles(directory=str(PUBLIC_DIR)), name="files")


# Setup logging to file
import logging
import sys

# Force unbuffered output
sys.stdout.reconfigure(line_buffering=True)
sys.stderr.reconfigure(line_buffering=True)

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
    
    # Check if logo exists
    logo_path = Path(__file__).parent / "logo-ffapi.png"
    if logo_path.exists():
        logger.info(f"✓ Logo found at: {logo_path}")
    else:
        logger.warning(f"✗ Logo NOT found at: {logo_path}")
        logger.warning("Logo will not be displayed on pages. To fix:")
        logger.warning("1. Place logo-ffapi.png in project root (same directory as Dockerfile)")
        logger.warning("2. Rebuild: docker compose build --no-cache")
    
    _flush_logs()


def _rand(n=8):
    import random, string
    return "".join(random.choices(string.digits, k=n))


def _flush_logs():
    """Force flush all log handlers."""
    for handler in logging.getLogger().handlers:
        handler.flush()


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
                    except Exception:
                        pass
            
            # Delete old files
            for fp in files_to_delete:
                try:
                    fp.unlink()
                    deleted_count += 1
                except Exception:
                    pass
            
            # Remove directory if empty
            try:
                if not any(child.iterdir()):
                    child.rmdir()
            except Exception:
                pass
    
    if deleted_count > 0:
        logger.info(f"Cleanup: deleted {deleted_count} files older than {days} days")
        _flush_logs()


def publish_file(src: Path, ext: str) -> Dict[str, str]:
    """Move a finished file into PUBLIC_DIR/YYYYMMDD/ and return URLs/paths.
       Uses shutil.move to be cross-device safe (works across Docker volumes/Windows)."""
    cleanup_old_public()
    day = datetime.utcnow().strftime("%Y%m%d")
    folder = PUBLIC_DIR / day
    folder.mkdir(parents=True, exist_ok=True)
    name = datetime.utcnow().strftime("%Y%m%d_%H%M%S_") + _rand() + ext
    dst = folder / name

    # Cross-device safe move
    shutil.move(str(src), str(dst))
    
    logger.info(f"Published file: {name} ({dst.stat().st_size / (1024*1024):.2f} MB)")
    _flush_logs()

    rel = f"/files/{day}/{name}"
    url = f"{PUBLIC_BASE_URL.rstrip('/')}{rel}" if PUBLIC_BASE_URL else rel
    return {"dst": str(dst), "url": url, "rel": rel}


def save_log(log_path: Path, operation: str) -> None:
    """Save ffmpeg log to persistent logs directory."""
    if not log_path.exists():
        return
    day = datetime.utcnow().strftime("%Y%m%d")
    folder = LOGS_DIR / day
    folder.mkdir(parents=True, exist_ok=True)
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
        .logo {{ display: block; width: 20%; height: 20%; margin-bottom: 20px; }}
        table {{ border-collapse: collapse; width: 100%; }}
        th, td {{ border-bottom: 1px solid #eee; padding: 8px 10px; font-size: 14px; }}
        th {{ text-align: left; background: #fafafa; }}
        a {{ text-decoration: none; }}
        nav {{ margin-bottom: 20px; }}
        nav a {{ margin-right: 15px; color: #0066cc; }}
      </style>
    </head>
    <body>
      <img src="/logo.png" alt="FFAPI Logo" class="logo" onerror="this.style.display='none'">
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
        .logo {{ display: block; width: 20%; height: 20%; margin-bottom: 20px; }}
        table {{ border-collapse: collapse; width: 100%; }}
        th, td {{ border-bottom: 1px solid #eee; padding: 8px 10px; font-size: 14px; }}
        th {{ text-align: left; background: #fafafa; }}
        a {{ text-decoration: none; color: #0066cc; }}
        nav {{ margin-bottom: 20px; }}
        nav a {{ margin-right: 15px; }}
      </style>
    </head>
    <body>
      <img src="/logo.png" alt="FFAPI Logo" class="logo" onerror="this.style.display='none'">
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
    target = (LOGS_DIR / path).resolve()
    if LOGS_DIR not in target.parents and target != LOGS_DIR:
        raise HTTPException(status_code=400, detail="Invalid path")
    if not target.exists():
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
            
            with APP_LOG_FILE.open("r", encoding="utf-8", errors="ignore") as f:
                lines = f.readlines()
                # Get last 1000 lines
                app_logs = "".join(lines[-1000:]) if lines else "No logs yet"
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
        .logo {{ display: block; width: 20%; height: 20%; margin-bottom: 20px; }}
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
      <img src="/logo.png" alt="FFAPI Logo" class="logo" onerror="this.style.display='none'">
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
</div>

<div class="endpoint">
  <div class="method get">GET</div>
  <div class="path">/downloads</div>
  <div class="desc">Browse generated files (HTML page)</div>
</div>

<div class="endpoint">
  <div class="method get">GET</div>
  <div class="path">/logs</div>
  <div class="desc">Browse FFmpeg operation logs (HTML page)</div>
</div>

<div class="endpoint">
  <div class="method get">GET</div>
  <div class="path">/logs/view?path={date}/{filename}</div>
  <div class="desc">View individual log file content</div>
  <div class="params">
    <span class="param">path</span> - Relative path to log file (e.g., 20241008/20241008_143022_12345678_compose-urls.log)
  </div>
</div>

<div class="endpoint">
  <div class="method get">GET</div>
  <div class="path">/ffmpeg?auto_refresh={seconds}</div>
  <div class="desc">FFmpeg info and container logs with optional auto-refresh</div>
  <div class="params">
    <span class="param">auto_refresh</span> - Auto-refresh interval in seconds (0-60, default: 0)
  </div>
</div>

<div class="endpoint">
  <div class="method get">GET</div>
  <div class="path">/documentation</div>
  <div class="desc">This page - Complete API documentation</div>
</div>

<div class="endpoint">
  <div class="method get">GET</div>
  <div class="path">/health</div>
  <div class="desc">Health check endpoint</div>
  <div class="response">Returns: {"ok": true}</div>
</div>

<div class="endpoint">
  <div class="method get">GET</div>
  <div class="path">/files/{date}/{filename}</div>
  <div class="desc">Static file serving - download generated files</div>
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
  <div class="example">
Example:<br>
{<br>
  "input_files": {"video": "https://example.com/vid.mp4"},<br>
  "output_files": {"out": "result.mp4"},<br>
  "ffmpeg_command": "-i {{video}} -vf scale=1280:720 {{out}}"<br>
}
  </div>
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
        .logo {{ display: block; width: 20%; height: 20%; margin-bottom: 20px; }}
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
      <img src="/logo.png" alt="FFAPI Logo" class="logo" onerror="this.style.display='none'">
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
    _flush_logs()
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


# ---------- helpers ----------
ALLOWED_FORWARD_HEADERS_LOWER = {
    "cookie", "authorization", "x-n8n-api-key", "ngrok-skip-browser-warning"
}

def _download_to(url: str, dest: Path, headers: Optional[Dict[str, str]] = None):
    hdr = {}
    if headers:
        for k, v in headers.items():
            if k.lower() in ALLOWED_FORWARD_HEADERS_LOWER:
                hdr[k] = v
    with requests.get(url, headers=hdr, stream=True, timeout=300) as r:
        r.raise_for_status()
        with dest.open("wb") as f:
            for ch in r.iter_content(8192):
                if ch:
                    f.write(ch)


# ---------- routes ----------
@app.post("/image/to-mp4-loop")
async def image_to_mp4_loop(file: UploadFile = File(...), duration: int = 30, as_json: bool = False):
    logger.info(f"Starting image-to-mp4-loop: {file.filename}, duration={duration}s")
    _flush_logs()
    if file.content_type not in {"image/png", "image/jpeg"}:
        raise HTTPException(status_code=400, detail="Only PNG/JPEG are supported.")
    if not (1 <= duration <= 3600):
        raise HTTPException(status_code=400, detail="duration must be 1..3600 seconds")
    with TemporaryDirectory(prefix="loop_", dir=str(WORK_DIR)) as workdir:
        work = Path(workdir)
        in_path = work / ("input.png" if file.content_type == "image/png" else "input.jpg")
        out_path = work / "output.mp4"
        in_path.write_bytes(await file.read())
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
            code = subprocess.run(cmd, stdout=lf, stderr=lf).returncode
        save_log(log, "image-to-mp4")
        if code != 0 or not out_path.exists():
            logger.error(f"image-to-mp4-loop failed for {file.filename}")
            return JSONResponse(status_code=500, content={"error": "ffmpeg_failed", "log": log.read_text()})
        pub = publish_file(out_path, ".mp4")
        logger.info(f"image-to-mp4-loop completed: {pub['rel']}")
        _flush_logs()
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
    width: int = 1920,
    height: int = 1080,
    fps: int = 30,
    bgm_volume: float = 0.3,
    as_json: bool = False,
):
    logger.info(f"Starting compose-from-binaries: video={video.filename}, audio={audio.filename if audio else None}, bgm={bgm.filename if bgm else None}")
    _flush_logs()
    with TemporaryDirectory(prefix="compose_", dir=str(WORK_DIR)) as workdir:
        work = Path(workdir)
        v_path = work / "video.mp4"
        a_path = work / "audio"
        b_path = work / "bgm"
        out_path = work / "output.mp4"

        v_path.write_bytes(await video.read())
        has_audio = False
        has_bgm = False
        if audio:
            a_path.write_bytes(await audio.read())
            has_audio = True
        if bgm:
            b_path.write_bytes(await bgm.read())
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
            code = subprocess.run(cmd, stdout=lf, stderr=lf).returncode
        save_log(log, "compose-binaries")
        if code != 0 or not out_path.exists():
            logger.error("compose-from-binaries failed")
            return JSONResponse(status_code=500, content={"error": "ffmpeg_failed", "cmd": cmd, "log": log.read_text()})

        pub = publish_file(out_path, ".mp4")
        logger.info(f"compose-from-binaries completed: {pub['rel']}")
        _flush_logs()
        if as_json:
            return {"ok": True, "file_url": pub["url"], "path": pub["dst"]}
        resp = FileResponse(pub["dst"], media_type="video/mp4", filename=os.path.basename(pub["dst"]))
        resp.headers["X-File-URL"] = pub["url"]
        return resp


@app.post("/video/concat-from-urls")
def video_concat_from_urls(job: ConcatJob, as_json: bool = False):
    logger.info(f"Starting concat: {len(job.clips)} clips, {job.width}x{job.height}@{job.fps}fps")
    _flush_logs()
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
                code = subprocess.run(cmd, stdout=lf, stderr=lf).returncode
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
            code2 = subprocess.run(cmd2, stdout=lf, stderr=lf).returncode
        save_log(log2, "concat-final")
        if code2 != 0 or not out_path.exists():
            logger.error("concat failed at final stage")
            return JSONResponse(status_code=500, content={"error": "ffmpeg_failed_concat", "log": log2.read_text()})
        pub = publish_file(out_path, ".mp4")
        logger.info(f"concat completed: {len(job.clips)} clips -> {pub['rel']}")
        _flush_logs()
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
            code = subprocess.run(cmd, stdout=logf, stderr=logf).returncode
        save_log(log_path, "compose-urls")
        if code != 0 or not out_path.exists():
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
            code = subprocess.run(cmd, stdout=lf, stderr=lf).returncode
        save_log(log, "compose-tracks")
        out_path = work / "output.mp4"
        if code != 0 or not out_path.exists():
            return JSONResponse(status_code=500, content={"error": "ffmpeg_failed", "log": log.read_text()})

        pub = publish_file(out_path, ".mp4")
        if as_json:
            return {"ok": True, "file_url": pub["url"], "path": pub["dst"]}
        resp = FileResponse(pub["dst"], media_type="video/mp4", filename=os.path.basename(pub["dst"]))
        resp.headers["X-File-URL"] = pub["url"]
        return resp


@app.post("/v1/run-ffmpeg-command")
def run_rendi(job: RendiJob):
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
        except Exception:
            args = ["bash", "-lc", cmd_text]

        log = work / "ffmpeg.log"
        with log.open("wb") as lf:
            run_args = ["ffmpeg"] + args if args and args[0] != "ffmpeg" else args
            code = subprocess.run(run_args, stdout=lf, stderr=lf).returncode
        save_log(log, "rendi-command")
        if code != 0:
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

ALLOWED_FORWARD_HEADERS_LOWER = {"cookie","authorization","x-n8n-api-key","ngrok-skip-browser-warning"}

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
    with TemporaryDirectory(prefix="probe_", dir=str(WORK_DIR)) as workdir:
        p = Path(workdir) / (file.filename or "input.bin")
        p.write_bytes(await file.read())
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
    target = (PUBLIC_DIR / rel).resolve()
    if PUBLIC_DIR not in target.parents and target != PUBLIC_DIR:
        raise HTTPException(status_code=400, detail="rel must be within PUBLIC_DIR")
    if not target.exists():
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
