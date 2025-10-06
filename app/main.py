
import os, io, shlex, json, subprocess, random, string, shutil
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

RETENTION_DAYS = int(os.getenv("RETENTION_DAYS", "7"))
PUBLIC_BASE_URL = os.getenv("PUBLIC_BASE_URL")  # e.g. "http://10.120.2.5:3000"

# Mount static /files
app.mount("/files", StaticFiles(directory=str(PUBLIC_DIR)), name="files")


def _rand(n=8):
    import random, string
    return "".join(random.choices(string.digits, k=n))


def cleanup_old_public(days: int = RETENTION_DAYS):
    if days <= 0:
        return
    cutoff = datetime.utcnow().date() - timedelta(days=days)
    for child in PUBLIC_DIR.iterdir():
        if child.is_dir():
            try:
                d = datetime.strptime(child.name, "%Y%m%d").date()
                if d < cutoff:
                    shutil.rmtree(child, ignore_errors=True)
            except Exception:
                pass


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

    rel = f"/files/{day}/{name}"
    url = f"{PUBLIC_BASE_URL.rstrip('/')}{rel}" if PUBLIC_BASE_URL else rel
    return {"dst": str(dst), "url": url, "rel": rel}


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
        body {{ font-family: system-ui, sans-serif; padding: 24px; }}
        table {{ border-collapse: collapse; width: 100%; }}
        th, td {{ border-bottom: 1px solid #eee; padding: 8px 10px; font-size: 14px; }}
        th {{ text-align: left; background: #fafafa; }}
        a {{ text-decoration: none; }}
      </style>
    </head>
    <body>
      <h2>Generated Files</h2>
      <table>
        <thead><tr><th>Date</th><th>File</th><th>Size</th></tr></thead>
        <tbody>{"".join(rows) if rows else "<tr><td colspan='3'>No files yet</td></tr>"} </tbody>
      </table>
    </body>
    </html>
    """
    return HTMLResponse(html)


@app.get("/health")
def health():
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
        if code != 0 or not out_path.exists():
            return JSONResponse(status_code=500, content={"error": "ffmpeg_failed", "log": log.read_text()})
        pub = publish_file(out_path, ".mp4")
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
        if code != 0 or not out_path.exists():
            return JSONResponse(status_code=500, content={"error": "ffmpeg_failed", "cmd": cmd, "log": log.read_text()})

        pub = publish_file(out_path, ".mp4")
        if as_json:
            return {"ok": True, "file_url": pub["url"], "path": pub["dst"]}
        resp = FileResponse(pub["dst"], media_type="video/mp4", filename=os.path.basename(pub["dst"]))
        resp.headers["X-File-URL"] = pub["url"]
        return resp


@app.post("/video/concat-from-urls")
def video_concat_from_urls(job: ConcatJob, as_json: bool = False):
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
        if code2 != 0 or not out_path.exists():
            return JSONResponse(status_code=500, content={"error": "ffmpeg_failed_concat", "log": log2.read_text()})
        pub = publish_file(out_path, ".mp4")
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
