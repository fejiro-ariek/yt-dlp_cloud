import os
import uuid
import subprocess
from pathlib import Path

from fastapi import FastAPI, HTTPException, Query, UploadFile, File
from fastapi.responses import StreamingResponse
import yt_dlp

app = FastAPI(title="YT-DLP Downloader API", version="1.0.0")

DOWNLOAD_DIR = Path("/tmp/downloads")
DOWNLOAD_DIR.mkdir(parents=True, exist_ok=True)

COOKIES_FILE = "/app/cookies.txt"
PROXY_URL = os.environ.get("PROXY_URL", "")


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
        "postprocessors": [{
            "key": "FFmpegVideoConvertor",
            "preferedformat": "mp4",
        }],
        **cookie_opts(),
        **proxy_opts(),
    }


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
        filename = f"{safe_title[:80]}.mp4"
        headers = {
            "Content-Disposition": f'attachment; filename="{filename}"',
            "Content-Length": str(file_size),
            "X-Video-Title": safe_title,
        }
        return StreamingResponse(iterfile(), media_type="video/mp4", headers=headers)

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
    job_id = uuid.uuid4().hex
    output_path = str(DOWNLOAD_DIR / f"{job_id}.%(ext)s")

    ydl_opts = {
        "format": "bestaudio/best/worstaudio",
        "outtmpl": output_path,
        "quiet": True,
        "no_warnings": True,
        "extractor_args": {"youtube": {"player_client": ["ios", "android", "web"]}},
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
        filename = f"{safe_title[:80]}.mp3"
        headers = {
            "Content-Disposition": f'attachment; filename="{filename}"',
            "Content-Length": str(file_size),
        }
        return StreamingResponse(iterfile(), media_type="audio/mpeg", headers=headers)

    except yt_dlp.utils.DownloadError as e:
        raise HTTPException(status_code=400, detail=f"Download failed: {str(e)}")
    except HTTPException:
        raise
    except Exception as e:
        raise HTTPException(status_code=500, detail=f"Unexpected error: {str(e)}")


@app.post("/merge")
async def merge_audio_video(
    video_url: str = Query(..., description="YouTube video URL"),
    audio: UploadFile = File(..., description="Italian dubbed audio file (mp3/wav)"),
):
    """
    Download a YouTube video, strip its original audio,
    and replace it with the uploaded Italian dubbed audio.
    Returns the final dubbed video as mp4.
    """
    job_id = uuid.uuid4().hex
    video_path = DOWNLOAD_DIR / f"{job_id}_video.%(ext)s"
    audio_path = DOWNLOAD_DIR / f"{job_id}_audio{Path(audio.filename).suffix or '.mp3'}"
    output_path = DOWNLOAD_DIR / f"{job_id}_dubbed.mp4"

    # Save uploaded audio to disk
    try:
        audio_bytes = await audio.read()
        with open(audio_path, "wb") as f:
            f.write(audio_bytes)
    except Exception as e:
        raise HTTPException(status_code=400, detail=f"Failed to save audio: {str(e)}")

    # Download video from YouTube (video stream only, no audio)
    ydl_opts = {
        "format": "bestvideo+bestaudio/best",
        "outtmpl": str(video_path),
        "quiet": True,
        "no_warnings": True,
        "extractor_args": {"youtube": {"player_client": ["ios", "android", "web"]}},
        **cookie_opts(),
        **proxy_opts(),
    }

    try:
        with yt_dlp.YoutubeDL(ydl_opts) as ydl:
            ydl.extract_info(video_url, download=True)

        downloaded_videos = list(DOWNLOAD_DIR.glob(f"{job_id}_video.*"))
        if not downloaded_videos:
            raise HTTPException(status_code=500, detail="Video download failed")

        actual_video_path = downloaded_videos[0]

        # Use ffmpeg to merge video + new audio
        # -an removes original audio, -shortest cuts to shortest stream
        result = subprocess.run([
            "ffmpeg", "-y",
            "-i", str(actual_video_path),   # input video
            "-i", str(audio_path),           # input dubbed audio
            "-c:v", "copy",                  # copy video stream (no re-encode, fast)
            "-c:a", "aac",                   # encode audio as aac
            "-map", "0:v:0",                 # take video from first input
            "-map", "1:a:0",                 # take audio from second input
            "-shortest",                     # end when shortest stream ends
            str(output_path)
        ], capture_output=True, text=True)

        if result.returncode != 0:
            raise HTTPException(status_code=500, detail=f"FFmpeg error: {result.stderr}")

        file_size = output_path.stat().st_size

        def iterfile():
            try:
                with open(output_path, "rb") as f:
                    while chunk := f.read(1024 * 1024):
                        yield chunk
            finally:
                # Clean up all temp files
                for p in [actual_video_path, audio_path, output_path]:
                    try:
                        p.unlink()
                    except Exception:
                        pass

        headers = {
            "Content-Disposition": 'attachment; filename="dubbed_italian.mp4"',
            "Content-Length": str(file_size),
        }
        return StreamingResponse(iterfile(), media_type="video/mp4", headers=headers)

    except yt_dlp.utils.DownloadError as e:
        raise HTTPException(status_code=400, detail=f"Video download failed: {str(e)}")
    except HTTPException:
        raise
    except Exception as e:
        raise HTTPException(status_code=500, detail=f"Unexpected error: {str(e)}")
