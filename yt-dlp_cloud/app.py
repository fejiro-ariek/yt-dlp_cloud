import os
import uuid
import asyncio
import subprocess
from pathlib import Path

from fastapi import FastAPI, HTTPException, Query
from fastapi.responses import StreamingResponse, JSONResponse
import yt_dlp

app = FastAPI(title="YT-DLP Downloader API", version="1.0.0")

DOWNLOAD_DIR = Path("/tmp/downloads")
DOWNLOAD_DIR.mkdir(parents=True, exist_ok=True)


def get_ydl_opts(quality: str, output_path: str) -> dict:
    """Build yt-dlp options based on quality preference."""
    format_map = {
        "high":   "bestvideo[ext=mp4]+bestaudio[ext=m4a]/best[ext=mp4]/best",
        "medium": "bestvideo[height<=720][ext=mp4]+bestaudio[ext=m4a]/best[height<=720][ext=mp4]/best",
        "low":    "bestvideo[height<=480][ext=mp4]+bestaudio[ext=m4a]/best[height<=480][ext=mp4]/best",
    }
    return {
        "format": format_map.get(quality, format_map["high"]),
        "outtmpl": output_path,
        "merge_output_format": "mp4",
        "quiet": True,
        "no_warnings": True,
        "postprocessors": [{
            "key": "FFmpegVideoConvertor",
            "preferedformat": "mp4",
        }],
    }


@app.get("/")
def root():
    return {"status": "ok", "message": "YT-DLP API is running"}


@app.get("/metadata")
def get_metadata(video_url: str = Query(..., description="YouTube video URL")):
    """Return metadata for a YouTube video without downloading it."""
    ydl_opts = {
        "quiet": True,
        "no_warnings": True,
        "skip_download": True,
    }
    try:
        with yt_dlp.YoutubeDL(ydl_opts) as ydl:
            info = ydl.extract_info(video_url, download=False)
            return {
                "title": info.get("title"),
                "description": info.get("description", "")[:500],  # truncate
                "duration_seconds": info.get("duration"),
                "thumbnail": info.get("thumbnail"),
                "uploader": info.get("uploader"),
                "view_count": info.get("view_count"),
                "upload_date": info.get("upload_date"),
                "formats": [
                    {
                        "format_id": f.get("format_id"),
                        "ext": f.get("ext"),
                        "resolution": f.get("resolution") or f.get("height"),
                        "filesize": f.get("filesize"),
                    }
                    for f in info.get("formats", [])
                    if f.get("ext") in ("mp4", "webm")
                ][-10:],  # last 10 formats (typically best quality)
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
    """Download a YouTube video and stream it back to the client."""
    if quality not in ("high", "medium", "low"):
        raise HTTPException(status_code=400, detail="quality must be 'high', 'medium', or 'low'")

    # Unique filename per request to avoid collisions
    job_id = uuid.uuid4().hex
    output_path = str(DOWNLOAD_DIR / f"{job_id}.%(ext)s")

    ydl_opts = get_ydl_opts(quality, output_path)

    try:
        with yt_dlp.YoutubeDL(ydl_opts) as ydl:
            info = ydl.extract_info(video_url, download=True)
            title = info.get("title", "video")

        # Find the downloaded file
        downloaded_files = list(DOWNLOAD_DIR.glob(f"{job_id}.*"))
        if not downloaded_files:
            raise HTTPException(status_code=500, detail="Download completed but file not found")

        file_path = downloaded_files[0]
        file_size = file_path.stat().st_size

        def iterfile():
            try:
                with open(file_path, "rb") as f:
                    while chunk := f.read(1024 * 1024):  # 1MB chunks
                        yield chunk
            finally:
                # Clean up after streaming
                try:
                    file_path.unlink()
                except Exception:
                    pass

        safe_title = "".join(c for c in title if c.isalnum() or c in " -_").strip()
        filename = f"{safe_title[:80]}.mp4"

        headers = {
            "Content-Disposition": f'attachment; filename="{filename}"',
            "Content-Length": str(file_size),
            "X-Video-Title": safe_title,
        }

        return StreamingResponse(
            iterfile(),
            media_type="video/mp4",
            headers=headers,
        )

    except yt_dlp.utils.DownloadError as e:
        raise HTTPException(status_code=400, detail=f"Download failed: {str(e)}")
    except HTTPException:
        raise
    except Exception as e:
        raise HTTPException(status_code=500, detail=f"Unexpected error: {str(e)}")


@app.get("/audio")
def download_audio(
    video_url: str = Query(..., description="YouTube video URL"),
):
    """Download audio only as MP3."""
    job_id = uuid.uuid4().hex
    output_path = str(DOWNLOAD_DIR / f"{job_id}.%(ext)s")

    ydl_opts = {
        "format": "bestaudio/best",
        "outtmpl": output_path,
        "quiet": True,
        "no_warnings": True,
        "postprocessors": [{
            "key": "FFmpegExtractAudio",
            "preferredcodec": "mp3",
            "preferredquality": "192",
        }],
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
        filename = f"{safe_title[:80]}.mp3"

        headers = {
            "Content-Disposition": f'attachment; filename="{filename}"',
            "Content-Length": str(file_size),
        }

        return StreamingResponse(
            iterfile(),
            media_type="audio/mpeg",
            headers=headers,
        )

    except yt_dlp.utils.DownloadError as e:
        raise HTTPException(status_code=400, detail=f"Download failed: {str(e)}")
    except HTTPException:
        raise
    except Exception as e:
        raise HTTPException(status_code=500, detail=f"Unexpected error: {str(e)}")
