<img src="https://github.com/hastla007/ffapi/blob/main/logo-ffapi.png?raw=true" alt="ffapi Logo" width="20%" height="20%" >

# FFAPI Ultimate

### Key features
- `publish_file()` uses `shutil.move()` (works across devices / Docker volumes / Windows).
- All `TemporaryDirectory(...)` calls use `dir=$WORK_DIR` (default `/data/work`) so temps are on the same volume.
- **Fixed retention**: Files are deleted based on actual file age (modification time), not folder name. Files persist through restarts until they are truly RETENTION_DAYS old, and cleanup resumes automatically on startup and via a periodic background task.
- **Runtime tuning**: Administrators can adjust retention windows, rate limits, FFmpeg timeouts, and upload limits directly from the `/settings` dashboard without restarting the service.
- **Optional API authentication**: Toggle API key enforcement at runtime and issue/revoke keys from the `/api-keys` dashboard.
- **API IP whitelist**: When API authentication is enabled, restrict access to approved IPv4/IPv6 addresses or CIDR ranges from the `/settings` page.
- **Optional dashboard MFA**: Enable TOTP-based two-factor authentication with QR setup, backup codes, and recovery enforcement for the settings and dashboard pages when UI authentication is required. (Install `qrcode[pil]` to render setup QR codes—otherwise a placeholder image is shown.)
- **Rich browsing experience**: The `/downloads` page now supports search, pagination, and inline video thumbnails while `/logs` offers paginated browsing for large histories.
- **Job telemetry**: Asynchronous compose jobs expose granular progress updates, status messages, and a capped history log via the `/jobs/{job_id}` endpoint.
- **Webhook-ready background jobs**: Async URL compositions can notify external systems on completion or failure and expose a browsable `/jobs` history dashboard.
- **Persistent FFmpeg logs**: All FFmpeg operations save logs to `/data/logs` for debugging.
- **Application logging**: Container logs (stdout/stderr) are saved to `/data/logs/application.log` and viewable at `/ffmpeg` endpoint.

### Endpoints
- `/health` - Health check
- `/downloads` - Browse generated files (HTML)
- `/logs` - Browse FFmpeg logs (HTML)
- `/logs/view?path=...` - View individual log file
- `/ffmpeg` - Display FFmpeg version and container logs (HTML)
- `/documentation` - Complete API documentation (HTML)
- `/api-keys` - Manage API key authentication and generate keys
- `/metrics` - Operational dashboard with per-endpoint metrics
- `/metrics/prometheus` - Prometheus text-format metrics export
- `/settings` - Settings page for UI auth, runtime tuning, and storage overview
- `/files/*` - Static file serving
- `/image/to-mp4-loop` - Convert image to looping video
- `/compose/from-binaries` - Compose video from uploaded files
- `/compose/from-urls` - Compose video from URLs
- `/compose/from-urls/async` - Queue URL composition job
- `/jobs` - Review recent async job history (HTML dashboard)
- `/jobs/{job_id}` - Check asynchronous job status
- `/compose/from-tracks` - Compose from track definitions
- `/video/concat-from-urls` - Concatenate videos
- `/video/concat` - Concat alias (accepts `clips` or `urls`)
- `/v1/audio/tempo` - Adjust audio playback speed and track jobs
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
- `/downloads` - Browse, search, and paginate generated video files with inline thumbnails
- `/logs` - View FFmpeg operation logs
- `/ffmpeg` - Monitor container with live logs and FFmpeg version info (supports auto-refresh)
- `/documentation` - Complete API reference with all endpoints and parameters
- `/api-keys` - Enable API authentication and create/revoke access keys (honors IP whitelist state)
- `/settings` - Configure dashboard access, retention, performance, API authentication (including the optional IP whitelist), and review storage usage

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
- `/compose/from-urls/async` - Start background job and poll `/jobs/{job_id}` for results
- `/compose/from-tracks` - Compose from track definitions

**Video Concatenation:**
- `/video/concat-from-urls` - Concatenate clips
- `/video/concat` - Concat alias

**Audio Processing:**
- `POST /v1/audio/tempo` - Download an audio file, manipulate playback speed, and publish the result. Body:
  ```json
  {
    "input_url": "http://10.120.2.5:4321/audio/speech/long/abcd1234/download",
    "output_name": "slowed.mp3",
    "tempo": 0.85
  }
  ```
  Successful responses include the generated `job_id`, formatted `input_size`, `output_file` path, and a signed `download_url`.
  n8n Execute Command example (assumes `FFAPI_BASE_URL` is an environment variable inside n8n):
  ```bash
  curl -X POST "$FFAPI_BASE_URL/v1/audio/tempo" \
    -H "Content-Type: application/json" \
    -d '{
      "input_url": "{{ $json["input_url"] }}",
      "output_name": "{{ $json["output_name"] || "tempo-output.mp3" }}",
      "tempo": {{ $json["tempo"] || 0.85 }}
    }'
  ```
- `GET /v1/audio/tempo` - Return the in-memory history of recent tempo jobs, including status, timestamps, and file metadata.
- `GET /v1/audio/tempo/{job_id}/status` - Inspect an individual tempo job for success, failure details, or in-progress updates.

**Custom Commands:**
- `/v1/run-ffmpeg-command` - Run custom FFmpeg command
- `/jobs/{job_id}` - Fetch asynchronous job status

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
- `MAX_FILE_SIZE_MB` - Maximum upload size enforced during streaming (default: 2048)
- `FFMPEG_TIMEOUT_SECONDS` - Maximum FFmpeg runtime before timeout (default: 7200)
- `MIN_FREE_SPACE_MB` - Minimum free disk space required before processing (default: 1000)
- `PUBLIC_CLEANUP_INTERVAL_SECONDS` - Interval between retention sweeps (default: 3600)
- `REQUIRE_DURATION_LIMIT` - Enforce `-t` or `-frames` on custom FFmpeg commands (default: false)
- `RATE_LIMIT_REQUESTS_PER_MINUTE` - Requests per minute per client before 429 responses (default: 60)

### Retention Logic
Files are automatically cleaned up based on their **actual modification time**, not the folder date. This means:
- Files survive machine restarts but are still removed once their modification time exceeds RETENTION_DAYS, even after container restarts
- Only truly old files are deleted
- RETENTION_DAYS works as expected
