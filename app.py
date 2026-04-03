import os
import uuid
import subprocess
import threading
from pathlib import Path

from fastapi import FastAPI, HTTPException, Query, UploadFile, File
from fastapi.responses import StreamingResponse
import yt_dlp

app = FastAPI(title="YT-DLP Downloader API", version="1.0.0")

DOWNLOAD_DIR = Path("/tmp/downloads")
DOWNLOAD_DIR.mkdir(parents=True, exist_ok=True)

COOKIES_FILE = "/app/cookies.txt"
PROXY_URL = os.environ.get("PROXY_URL", "")

# In-memory job store
jobs: dict = {}


def cookie_opts() -> dict:
    if os.path.exists(COOKIES_FILE):
        return {"cookiefile": COOKIES_FILE}
    return {}


def proxy_opts() -> dict:
    if PROXY_URL:
        return {"proxy": PROXY_URL}
    return {}


def get_ydl_opts(quality: str, output_path: str) -> dict:
    format_map = {
        "high":   "bestvideo+bestaudio/best",
        "medium": "bestvideo[height<=720]+bestaudio/best[height<=720]/best",
        "low":    "bestvideo[height<=480]+bestaudio/best[height<=480]/best",
    }
    return {
        "format": format_map.get(quality, format_map["high"]),
        "outtmpl": output_path,
        "merge_output_format": "mp4",
        "quiet": True,
        "no_warnings": True,
        "extractor_args": {"youtube": {"player_client": ["ios", "android", "web"]}},
        "retries": 10,
        "fragment_retries": 10,
        "file_access_retries": 5,
        "http_chunk_size": 1024 * 1024,
        "postprocessors": [{
            "key": "FFmpegVideoConvertor",
            "preferedformat": "mp4",
        }],
        **cookie_opts(),
        **proxy_opts(),
    }


# ── Background worker (merge) ──────────────────────────────────────────────────

def run_merge_job(job_id: str, video_url: str, audio_bytes: bytes, audio_ext: str):
    try:
        jobs[job_id]["status"] = "processing"

        video_tmpl = str(DOWNLOAD_DIR / f"{job_id}_video.%(ext)s")
        audio_path = DOWNLOAD_DIR / f"{job_id}_audio{audio_ext}"
        output_path = DOWNLOAD_DIR / f"{job_id}_dubbed.mp4"

        with open(audio_path, "wb") as f:
            f.write(audio_bytes)

        ydl_opts = {
            "format": "bestvideo+bestaudio/best",
            "outtmpl": video_tmpl,
            "quiet": True,
            "no_warnings": True,
            "extractor_args": {"youtube": {"player_client": ["ios", "android", "web"]}},
            "retries": 10,
            "fragment_retries": 10,
            "file_access_retries": 5,
            "http_chunk_size": 1024 * 1024,
            **cookie_opts(),
            **proxy_opts(),
        }
        with yt_dlp.YoutubeDL(ydl_opts) as ydl:
            ydl.extract_info(video_url, download=True)

        video_files = list(DOWNLOAD_DIR.glob(f"{job_id}_video.*"))
        if not video_files:
            raise Exception("Video download failed — no file found")

        actual_video = video_files[0]

        result = subprocess.run([
            "ffmpeg", "-y",
            "-i", str(actual_video),
            "-i", str(audio_path),
            "-c:v", "copy",
            "-c:a", "aac",
            "-map", "0:v:0",
            "-map", "1:a:0",
            "-shortest",
            str(output_path)
        ], capture_output=True, text=True)

        for p in [actual_video, audio_path]:
            try:
                p.unlink()
            except Exception:
                pass

        if result.returncode != 0:
            raise Exception(f"FFmpeg error: {result.stderr}")

        jobs[job_id]["status"] = "done"
        jobs[job_id]["file"] = str(output_path)

    except Exception as e:
        jobs[job_id]["status"] = "error"
        jobs[job_id]["error"] = str(e)


# ── Background worker (subtitle) ───────────────────────────────────────────────

def run_subtitle_job(job_id: str, video_url: str, subtitle_text: str):
    try:
        jobs[job_id]["status"] = "processing"

        video_tmpl = str(DOWNLOAD_DIR / f"{job_id}_video.%(ext)s")
        srt_path = DOWNLOAD_DIR / f"{job_id}.srt"
        output_path = DOWNLOAD_DIR / f"{job_id}_subtitled.mp4"

        ydl_opts = {
            "format": "bestvideo+bestaudio/best",
            "outtmpl": video_tmpl,
            "quiet": True,
            "no_warnings": True,
            "extractor_args": {"youtube": {"player_client": ["ios", "android", "web"]}},
            "retries": 10,
            "fragment_retries": 10,
            "file_access_retries": 5,
            "http_chunk_size": 1024 * 1024,
            **cookie_opts(),
            **proxy_opts(),
        }
        with yt_dlp.YoutubeDL(ydl_opts) as ydl:
            ydl.extract_info(video_url, download=True)

        video_files = list(DOWNLOAD_DIR.glob(f"{job_id}_video.*"))
        if not video_files:
            raise Exception("Video download failed — no file found")

        actual_video = video_files[0]

        # Get video duration
        probe = subprocess.run([
            "ffprobe", "-v", "error",
            "-show_entries", "format=duration",
            "-of", "default=noprint_wrappers=1:nokey=1",
            str(actual_video)
        ], capture_output=True, text=True)
        duration = float(probe.stdout.strip() or "60")

        # Split text into lines of ~80 chars
        words = subtitle_text.split()
        lines = []
        current_line = ""
        for word in words:
            if len(current_line) + len(word) + 1 <= 80:
                current_line += (" " if current_line else "") + word
            else:
                lines.append(current_line)
                current_line = word
        if current_line:
            lines.append(current_line)

        # Group into 2-line blocks spread across video duration
        blocks = [lines[i:i+2] for i in range(0, len(lines), 2)]
        num_blocks = len(blocks)
        block_duration = duration / max(num_blocks, 1)

        def fmt_time(s):
            h = int(s // 3600)
            m = int((s % 3600) // 60)
            sec = int(s % 60)
            ms = int((s - int(s)) * 1000)
            return f"{h:02}:{m:02}:{sec:02},{ms:03}"

        srt_content = ""
        for i, block in enumerate(blocks):
            start_sec = i * block_duration
            end_sec = (i + 1) * block_duration
            srt_content += f"{i+1}\n{fmt_time(start_sec)} --> {fmt_time(end_sec)}\n"
            srt_content += "\n".join(block) + "\n\n"

        with open(srt_path, "w", encoding="utf-8") as f:
            f.write(srt_content)

        # Burn subtitles into video
        result = subprocess.run([
            "ffmpeg", "-y",
            "-i", str(actual_video),
            "-vf", f"subtitles={srt_path}:force_style='FontSize=18,PrimaryColour=&HFFFFFF,OutlineColour=&H000000,Outline=2,Bold=1,Alignment=2'",
            "-c:a", "copy",
            str(output_path)
        ], capture_output=True, text=True)

        for p in [actual_video, srt_path]:
            try:
                p.unlink()
            except Exception:
                pass

        if result.returncode != 0:
            raise Exception(f"FFmpeg subtitle error: {result.stderr}")

        jobs[job_id]["status"] = "done"
        jobs[job_id]["file"] = str(output_path)

    except Exception as e:
        jobs[job_id]["status"] = "error"
        jobs[job_id]["error"] = str(e)


# ── Routes ─────────────────────────────────────────────────────────────────────

@app.get("/")
def root():
    return {"status": "ok", "message": "YT-DLP API is running"}


@app.get("/metadata")
def get_metadata(video_url: str = Query(..., description="YouTube video URL")):
    ydl_opts = {
        "quiet": True,
        "no_warnings": True,
        "skip_download": True,
        "extractor_args": {"youtube": {"player_client": ["ios", "android", "web"]}},
        **cookie_opts(),
        **proxy_opts(),
    }
    try:
        with yt_dlp.YoutubeDL(ydl_opts) as ydl:
            info = ydl.extract_info(video_url, download=False)
            return {
                "title": info.get("title"),
                "description": info.get("description", "")[:500],
                "duration_seconds": info.get("duration"),
                "thumbnail": info.get("thumbnail"),
                "uploader": info.get("uploader"),
                "view_count": info.get("view_count"),
                "upload_date": info.get("upload_date"),
            }
    except yt_dlp.utils.DownloadError as e:
        raise HTTPException(status_code=400, detail=f"Could not fetch video info: {str(e)}")
    except Exception as e:
        raise HTTPException(status_code=500, detail=f"Unexpected error: {str(e)}")


@app.get("/download")
def download_video(
    video_url: str = Query(..., description="YouTube video URL"),
    quality: str = Query("high", description="Quality: high | medium | low"),
):
    if quality not in ("high", "medium", "low"):
        raise HTTPException(status_code=400, detail="quality must be 'high', 'medium', or 'low'")

    job_id = uuid.uuid4().hex
    output_path = str(DOWNLOAD_DIR / f"{job_id}.%(ext)s")
    ydl_opts = get_ydl_opts(quality, output_path)

    try:
        with yt_dlp.YoutubeDL(ydl_opts) as ydl:
            info = ydl.extract_info(video_url, download=True)
            title = info.get("title", "video")

        downloaded_files = list(DOWNLOAD_DIR.glob(f"{job_id}.*"))
        if not downloaded_files:
            raise HTTPException(status_code=500, detail="Download completed but file not found")

        file_path = downloaded_files[0]
        file_size = file_path.stat().st_size

        def iterfile():
            try:
                with open(file_path, "rb") as f:
                    while chunk := f.read(1024 * 1024):
                        yield chunk
            finally:
                try:
                    file_path.unlink()
                except Exception:
                    pass

        safe_title = "".join(c for c in title if c.isalnum() or c in " -_").strip()
        return StreamingResponse(iterfile(), media_type="video/mp4", headers={
            "Content-Disposition": f'attachment; filename="{safe_title[:80]}.mp4"',
            "Content-Length": str(file_size),
            "X-Video-Title": safe_title,
        })

    except yt_dlp.utils.DownloadError as e:
        raise HTTPException(status_code=400, detail=f"Download failed: {str(e)}")
    except HTTPException:
        raise
    except Exception as e:
        raise HTTPException(status_code=500, detail=f"Unexpected error: {str(e)}")


@app.get("/audio")
def download_audio(video_url: str = Query(..., description="YouTube video URL")):
    job_id = uuid.uuid4().hex
    output_path = str(DOWNLOAD_DIR / f"{job_id}.%(ext)s")

    ydl_opts = {
        "format": "bestaudio/best/worstaudio",
        "outtmpl": output_path,
        "quiet": True,
        "no_warnings": True,
        "extractor_args": {"youtube": {"player_client": ["ios", "android", "web"]}},
        "retries": 10,
        "fragment_retries": 10,
        "file_access_retries": 5,
        "http_chunk_size": 1024 * 1024,
        "postprocessors": [{
            "key": "FFmpegExtractAudio",
            "preferredcodec": "mp3",
            "preferredquality": "192",
        }],
        **cookie_opts(),
        **proxy_opts(),
    }

    try:
        with yt_dlp.YoutubeDL(ydl_opts) as ydl:
            info = ydl.extract_info(video_url, download=True)
            title = info.get("title", "audio")

        downloaded_files = list(DOWNLOAD_DIR.glob(f"{job_id}.*"))
        if not downloaded_files:
            raise HTTPException(status_code=500, detail="Download completed but file not found")

        file_path = downloaded_files[0]
        file_size = file_path.stat().st_size

        def iterfile():
            try:
                with open(file_path, "rb") as f:
                    while chunk := f.read(1024 * 1024):
                        yield chunk
            finally:
                try:
                    file_path.unlink()
                except Exception:
                    pass

        safe_title = "".join(c for c in title if c.isalnum() or c in " -_").strip()
        return StreamingResponse(iterfile(), media_type="audio/mpeg", headers={
            "Content-Disposition": f'attachment; filename="{safe_title[:80]}.mp3"',
            "Content-Length": str(file_size),
        })

    except yt_dlp.utils.DownloadError as e:
        raise HTTPException(status_code=400, detail=f"Download failed: {str(e)}")
    except HTTPException:
        raise
    except Exception as e:
        raise HTTPException(status_code=500, detail=f"Unexpected error: {str(e)}")


# ── Async Merge Endpoints ──────────────────────────────────────────────────────

@app.post("/merge/start")
async def merge_start(
    video_url: str = Query(..., description="YouTube video URL"),
    audio: UploadFile = File(..., description="Dubbed audio file"),
):
    job_id = uuid.uuid4().hex
    audio_bytes = await audio.read()
    audio_ext = Path(audio.filename).suffix or ".mp3"

    jobs[job_id] = {"status": "pending", "file": None, "error": None}

    thread = threading.Thread(
        target=run_merge_job,
        args=(job_id, video_url, audio_bytes, audio_ext),
        daemon=True,
    )
    thread.start()

    return {"job_id": job_id, "status": "pending"}


@app.get("/merge/status")
def merge_status(job_id: str = Query(...)):
    if job_id not in jobs:
        raise HTTPException(status_code=404, detail="Job not found")
    job = jobs[job_id]
    return {"job_id": job_id, "status": job["status"], "error": job.get("error")}


@app.get("/merge/result")
def merge_result(job_id: str = Query(...)):
    if job_id not in jobs:
        raise HTTPException(status_code=404, detail="Job not found")

    job = jobs[job_id]
    if job["status"] != "done":
        raise HTTPException(status_code=400, detail=f"Job not ready. Status: {job['status']}")

    file_path = Path(job["file"])
    if not file_path.exists():
        raise HTTPException(status_code=500, detail="Output file missing")

    file_size = file_path.stat().st_size

    def iterfile():
        try:
            with open(file_path, "rb") as f:
                while chunk := f.read(1024 * 1024):
                    yield chunk
        finally:
            try:
                file_path.unlink()
            except Exception:
                pass
            jobs.pop(job_id, None)

    return StreamingResponse(iterfile(), media_type="video/mp4", headers={
        "Content-Disposition": 'attachment; filename="dubbed_italian.mp4"',
        "Content-Length": str(file_size),
    })


# ── Async Subtitle Endpoints ───────────────────────────────────────────────────

@app.post("/subtitle/start")
async def subtitle_start(
    video_url: str = Query(..., description="YouTube video URL"),
    text: str = Query(..., description="Italian subtitle text to burn into video"),
):
    job_id = uuid.uuid4().hex
    jobs[job_id] = {"status": "pending", "file": None, "error": None}

    thread = threading.Thread(
        target=run_subtitle_job,
        args=(job_id, video_url, text),
        daemon=True,
    )
    thread.start()

    return {"job_id": job_id, "status": "pending"}


@app.get("/subtitle/status")
def subtitle_status(job_id: str = Query(...)):
    if job_id not in jobs:
        raise HTTPException(status_code=404, detail="Job not found")
    job = jobs[job_id]
    return {"job_id": job_id, "status": job["status"], "error": job.get("error")}


@app.get("/subtitle/result")
def subtitle_result(job_id: str = Query(...)):
    if job_id not in jobs:
        raise HTTPException(status_code=404, detail="Job not found")

    job = jobs[job_id]
    if job["status"] != "done":
        raise HTTPException(status_code=400, detail=f"Job not ready. Status: {job['status']}")

    file_path = Path(job["file"])
    if not file_path.exists():
        raise HTTPException(status_code=500, detail="Output file missing")

    file_size = file_path.stat().st_size

    def iterfile():
        try:
            with open(file_path, "rb") as f:
                while chunk := f.read(1024 * 1024):
                    yield chunk
        finally:
            try:
                file_path.unlink()
            except Exception:
                pass
            jobs.pop(job_id, None)

    return StreamingResponse(iterfile(), media_type="video/mp4", headers={
        "Content-Disposition": 'attachment; filename="subtitled_italian.mp4"',
        "Content-Length": str(file_size),
    })
