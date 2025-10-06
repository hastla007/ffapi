
<img src="https://github.com/hastla007/ffapi/blob/main/logo-ffapi.png?raw=true" alt="ffapi Logo" >


# FFAPI Ultimate

### Key features
- `publish_file()` uses `shutil.move()` (works across devices / Docker volumes / Windows).
- All `TemporaryDirectory(...)` calls use `dir=$WORK_DIR` (default `/data/work`) so temps are on the same volume.
- **Fixed retention**: Files are deleted based on actual file age (modification time), not folder name. Files persist through restarts until truly RETENTION_DAYS old.
- **Persistent FFmpeg logs**: All FFmpeg operations save logs to `/data/logs` for debugging.
- **Application logging**: Container logs (stdout/stderr) are saved to `/data/logs/application.log` and viewable at `/ffmpeg` endpoint.

### Endpoints
- `/health` - Health check
- `/downloads` - Browse generated files (HTML)
- `/logs` - Browse FFmpeg logs (HTML)
- `/logs/view?path=...` - View individual log file
- `/ffmpeg` - Display FFmpeg version and container logs (HTML)
- `/documentation` - Complete API documentation (HTML)
- `/files/*` - Static file serving
- `/image/to-mp4-loop` - Convert image to looping video
- `/compose/from-binaries` - Compose video from uploaded files
- `/compose/from-urls` - Compose video from URLs
- `/compose/from-tracks` - Compose from track definitions
- `/video/concat-from-urls` - Concatenate videos
- `/video/concat` - Concat alias (accepts `clips` or `urls`)
- `/v1/run-ffmpeg-command` - Run custom FFmpeg command
- `/probe/from-urls` - FFprobe on URL
- `/probe/from-binary` - FFprobe on uploaded file
- `/probe/public` - FFprobe on public file

### Run with Docker
```bash
docker compose up -d
# Browse files: http://localhost:3000/downloads
# Browse logs: http://localhost:3000/logs
# FFmpeg & container info: http://localhost:3000/ffmpeg
# API documentation: http://localhost:3000/documentation
```

**Page descriptions:**
- `/downloads` - Browse and download generated video files
- `/logs` - View FFmpeg operation logs
- `/ffmpeg` - Monitor container with live logs and FFmpeg version info (supports auto-refresh)
- `/documentation` - Complete API reference with all endpoints and parameters

Outputs persist in `./public` (mounted to `/data/public`).  
Temporary work files live in `./work` (`/data/work`).  
FFmpeg logs persist in `./logs` (`/data/logs`).

### API Operations

For complete API documentation with all parameters and examples, visit `/documentation` endpoint.

**Image Processing:**
- `/image/to-mp4-loop` - Convert image to looping video

**Video Composition:**
- `/compose/from-binaries` - Compose from uploaded files
- `/compose/from-urls` - Compose from URLs  
- `/compose/from-tracks` - Compose from track definitions

**Video Concatenation:**
- `/video/concat-from-urls` - Concatenate clips
- `/video/concat` - Concat alias

**Custom Commands:**
- `/v1/run-ffmpeg-command` - Run custom FFmpeg command

**Media Inspection:**
- `/probe/from-urls` - Inspect media from URL
- `/probe/from-binary` - Inspect uploaded file
- `/probe/public` - Inspect public file

### Environment Variables
- `PUBLIC_BASE_URL` - Base URL for file links (e.g., http://10.120.2.5:3000)
- `PUBLIC_DIR` - Directory for output files (default: /data/public)
- `WORK_DIR` - Directory for temporary work files (default: /data/work)
- `LOGS_DIR` - Directory for FFmpeg logs (default: /data/logs)
- `RETENTION_DAYS` - Days to keep files before deletion (default: 7)

### Retention Logic
Files are automatically cleaned up based on their **actual modification time**, not the folder date. This means:
- Files survive machine restarts
- Only truly old files are deleted
- RETENTION_DAYS works as expected
