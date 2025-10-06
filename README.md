
# FFAPI Ultimate (fixed: cross-device move + WORK_DIR)

### Key changes
- `publish_file()` uses `shutil.move()` (works across devices / Docker volumes / Windows).
- All `TemporaryDirectory(...)` calls use `dir=$WORK_DIR` (default `/data/work`) so temps are on the same volume.

### Endpoints
Same as before: /health, /downloads, /files, /image/to-mp4-loop, /compose/*, /video/concat*, /v1/run-ffmpeg-command, /probe/*

### Run with Docker
```bash
docker compose up -d
# http://localhost:3000/downloads
```
Outputs persist in `./public` (mounted to `/data/public`). Temporary work files live in `./work` (`/data/work`).

### Env
- PUBLIC_BASE_URL  (e.g., http://10.120.2.5:3000)
- PUBLIC_DIR       (/data/public)
- WORK_DIR         (/data/work)
- RETENTION_DAYS   (default 7)
