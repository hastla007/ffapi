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
- `/ffmpeg` - Display FFmpeg version and capabilities (HTML)
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
```

The `/ffmpeg` page shows:
- Application logs (last 1000 lines - same as `docker logs`)
  - **Auto-refresh**: Click "Auto 5s", "Auto 10s", or "Auto 30s" for automatic updates
  - Shows file size and last modified time to confirm logs are updating
  - Manual refresh button available
- FFmpeg version and build information
- Available formats, codecs, and encoders

### Viewing Logs

**Via Browser:**
```
http://localhost:3000/ffmpeg?auto_refresh=10
```
This will auto-refresh every 10 seconds to show live updates.

**Via Docker CLI:**
```bash
docker logs ffapi -f  # Follow logs in real-time
```

**Via Log File:**
```bash
cat ./logs/application.log  # All logs are also saved here
```

Outputs persist in `./public` (mounted to `/data/public`).  
Temporary work files live in `./work` (`/data/work`).  
FFmpeg logs persist in `./logs` (`/data/logs`).

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
