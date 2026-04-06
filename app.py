import os
import uuid
import subprocess
import math
from pathlib import Path

from fastapi import FastAPI, HTTPException, Query, UploadFile, File, Form
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
        "postprocessors": [{"key": "FFmpegVideoConvertor", "preferedformat": "mp4"}],
        **cookie_opts(),
        **proxy_opts(),
    }


def make_srt(text: str, duration_seconds: float, job_id: str, segments: list = None) -> str:
    """
    Write an SRT file from either:
    - segments: list of {"text": str, "start": float, "duration": float} from Groq
    - text + duration: evenly split fallback
    """
    srt_path = str(DOWNLOAD_DIR / f"{job_id}.srt")

    def fmt_time(seconds):
        h = int(seconds // 3600)
        m = int((seconds % 3600) // 60)
        s = int(seconds % 60)
        ms = int((seconds - int(seconds)) * 1000)
        return f"{h:02}:{m:02}:{s:02},{ms:03}"

    lines = []

    if segments:
        # Use Groq timestamps — group into chunks of ~5 words
        # Groq returns "end" not "duration"
        words_per_chunk = 5
        all_words = []
        for seg in segments:
            seg_words = seg["text"].strip().split()
            seg_start = seg.get("start", 0)
            seg_end = seg.get("end", seg_start + seg.get("duration", 2))
            seg_dur = seg_end - seg_start
            word_dur = seg_dur / max(len(seg_words), 1)
            for wi, w in enumerate(seg_words):
                all_words.append({
                    "word": w,
                    "start": seg_start + wi * word_dur,
                    "end": seg_start + (wi + 1) * word_dur,
                })

        chunks = []
        for i in range(0, len(all_words), words_per_chunk):
            chunk_words = all_words[i:i+words_per_chunk]
            chunks.append({
                "text": " ".join(w["word"] for w in chunk_words),
                "start": chunk_words[0]["start"],
                "end": chunk_words[-1]["end"],
            })

        for i, chunk in enumerate(chunks):
            lines += [str(i+1), f"{fmt_time(chunk['start'])} --> {fmt_time(chunk['end'])}", chunk["text"], ""]
    else:
        # Fallback: even split
        words = text.split()
        words_per_chunk = 5
        chunks = [" ".join(words[i:i+words_per_chunk]) for i in range(0, len(words), words_per_chunk)]
        if not chunks:
            chunks = [text]
        time_per_chunk = duration_seconds / len(chunks)
        for i, chunk in enumerate(chunks):
            start = i * time_per_chunk
            end = start + time_per_chunk
            lines += [str(i+1), f"{fmt_time(start)} --> {fmt_time(end)}", chunk, ""]

    with open(srt_path, "w", encoding="utf-8") as f:
        f.write("\n".join(lines))

    return srt_path


@app.get("/")
def root():
    return {"status": "ok", "message": "YT-DLP API is running"}


@app.get("/metadata")
def get_metadata(video_url: str = Query(...)):
    ydl_opts = {
        "quiet": True, "no_warnings": True, "skip_download": True,
        "extractor_args": {"youtube": {"player_client": ["ios", "android", "web"]}},
        **cookie_opts(), **proxy_opts(),
    }
    try:
        with yt_dlp.YoutubeDL(ydl_opts) as ydl:
            info = ydl.extract_info(video_url, download=False)
            return {
                "title": info.get("title"),
                "duration_seconds": info.get("duration"),
                "thumbnail": info.get("thumbnail"),
                "uploader": info.get("uploader"),
            }
    except Exception as e:
        raise HTTPException(status_code=400, detail=str(e))


@app.get("/download")
def download_video(
    video_url: str = Query(...),
    quality: str = Query("high"),
):
    if quality not in ("high", "medium", "low"):
        raise HTTPException(status_code=400, detail="quality must be high, medium, or low")

    job_id = uuid.uuid4().hex
    output_path = str(DOWNLOAD_DIR / f"{job_id}.%(ext)s")

    try:
        with yt_dlp.YoutubeDL(get_ydl_opts(quality, output_path)) as ydl:
            info = ydl.extract_info(video_url, download=True)
            title = info.get("title", "video")

        downloaded_files = list(DOWNLOAD_DIR.glob(f"{job_id}.*"))
        if not downloaded_files:
            raise HTTPException(status_code=500, detail="File not found after download")

        file_path = downloaded_files[0]

        def iterfile():
            try:
                with open(file_path, "rb") as f:
                    while chunk := f.read(1024 * 1024):
                        yield chunk
            finally:
                try: file_path.unlink()
                except: pass

        safe_title = "".join(c for c in title if c.isalnum() or c in " -_").strip()
        return StreamingResponse(iterfile(), media_type="video/mp4", headers={
            "Content-Disposition": f'attachment; filename="{safe_title[:80]}.mp4"',
            "Content-Length": str(file_path.stat().st_size),
        })
    except yt_dlp.utils.DownloadError as e:
        raise HTTPException(status_code=400, detail=f"Download failed: {str(e)}")
    except HTTPException:
        raise
    except Exception as e:
        raise HTTPException(status_code=500, detail=str(e))


@app.get("/audio")
def download_audio(video_url: str = Query(...)):
    job_id = uuid.uuid4().hex
    output_path = str(DOWNLOAD_DIR / f"{job_id}.%(ext)s")

    ydl_opts = {
        "format": "bestaudio/best/worstaudio",
        "outtmpl": output_path,
        "quiet": True, "no_warnings": True,
        "extractor_args": {"youtube": {"player_client": ["ios", "android", "web"]}},
        "postprocessors": [{"key": "FFmpegExtractAudio", "preferredcodec": "mp3", "preferredquality": "192"}],
        **cookie_opts(), **proxy_opts(),
    }

    try:
        with yt_dlp.YoutubeDL(ydl_opts) as ydl:
            info = ydl.extract_info(video_url, download=True)
            title = info.get("title", "audio")

        downloaded_files = list(DOWNLOAD_DIR.glob(f"{job_id}.*"))
        if not downloaded_files:
            raise HTTPException(status_code=500, detail="File not found after download")

        file_path = downloaded_files[0]

        def iterfile():
            try:
                with open(file_path, "rb") as f:
                    while chunk := f.read(1024 * 1024):
                        yield chunk
            finally:
                try: file_path.unlink()
                except: pass

        safe_title = "".join(c for c in title if c.isalnum() or c in " -_").strip()
        return StreamingResponse(iterfile(), media_type="audio/mpeg", headers={
            "Content-Disposition": f'attachment; filename="{safe_title[:80]}.mp3"',
            "Content-Length": str(file_path.stat().st_size),
        })
    except yt_dlp.utils.DownloadError as e:
        raise HTTPException(status_code=400, detail=f"Download failed: {str(e)}")
    except HTTPException:
        raise
    except Exception as e:
        raise HTTPException(status_code=500, detail=str(e))


@app.post("/merge")
async def merge_audio_video(
    video_url: str = Query(..., description="YouTube video URL"),
    italian_text: str = Form(..., description="Italian translated text for subtitles"),
    audio: UploadFile = File(..., description="Italian dubbed audio file"),
):
    """
    1. Download YouTube video
    2. Replace audio with Italian dubbed audio
    3. Draw black rectangle over French subtitle area
    4. Burn Italian subtitles on top of the rectangle
    Returns final dubbed + subtitled video.
    """
    job_id = uuid.uuid4().hex
    video_path_template = DOWNLOAD_DIR / f"{job_id}_video.%(ext)s"
    audio_path = DOWNLOAD_DIR / f"{job_id}_audio.mp3"
    output_path = DOWNLOAD_DIR / f"{job_id}_final.mp4"

    # Save uploaded audio
    try:
        audio_bytes = await audio.read()
        with open(audio_path, "wb") as f:
            f.write(audio_bytes)
    except Exception as e:
        raise HTTPException(status_code=400, detail=f"Failed to save audio: {str(e)}")

    # Download video
    ydl_opts = {
        "format": "bestvideo+bestaudio/best",
        "outtmpl": str(video_path_template),
        "quiet": True, "no_warnings": True,
        "extractor_args": {"youtube": {"player_client": ["ios", "android", "web"]}},
        "retries": 5,
        "fragment_retries": 5,
        **cookie_opts(), **proxy_opts(),
    }

    try:
        with yt_dlp.YoutubeDL(ydl_opts) as ydl:
            info = ydl.extract_info(video_url, download=True)
            duration = info.get("duration", 60)

        downloaded_videos = list(DOWNLOAD_DIR.glob(f"{job_id}_video.*"))
        if not downloaded_videos:
            raise HTTPException(status_code=500, detail="Video download failed")

        actual_video_path = downloaded_videos[0]

        # Parse Groq segments if provided for accurate subtitle timing
        segments = None
        if segments_json:
            import json
            try:
                segments = json.loads(segments_json)
            except Exception:
                segments = None
        srt_path = make_srt(italian_text, duration, job_id, segments=segments)

        # Get video dimensions to position rectangle correctly
        probe = subprocess.run([
            "ffprobe", "-v", "error",
            "-select_streams", "v:0",
            "-show_entries", "stream=width,height",
            "-of", "csv=p=0",
            str(actual_video_path)
        ], capture_output=True, text=True)

        dims = probe.stdout.strip().split(",")
        width = int(dims[0]) if len(dims) >= 2 else 1080
        height = int(dims[1]) if len(dims) >= 2 else 1920

        # Rectangle covers ~75-85% down the video (where French sub is)
        rect_y = int(height * 0.72)
        rect_h = int(height * 0.10)
        sub_y = rect_y + int(rect_h * 0.5)  # Italian text sits in middle of rectangle

        # ffmpeg filter:
        # 1. drawbox  — black rectangle over French subtitle
        # 2. subtitles — Italian SRT burned on top
        # 3. map audio from dubbed file
        margin_bottom = int(height * 0.06)
        vf_filter = (
            f"subtitles={srt_path}:force_style='"
            f"FontSize=11,PrimaryColour=&HFFFFFF,Bold=1,OutlineColour=&H000000,"
            f"Outline=2,Shadow=1,MarginV={margin_bottom},Alignment=2'"
        )

        result = subprocess.run([
            "ffmpeg", "-y",
            "-i", str(actual_video_path),
            "-i", str(audio_path),
            "-vf", vf_filter,
            "-c:v", "libx264",
            "-c:a", "aac",
            "-map", "0:v:0",
            "-map", "1:a:0",
            "-shortest",
            str(output_path)
        ], capture_output=True, text=True)

        if result.returncode != 0:
            raise HTTPException(status_code=500, detail=f"FFmpeg error: {result.stderr[-500:]}")

        file_size = output_path.stat().st_size

        def iterfile():
            try:
                with open(output_path, "rb") as f:
                    while chunk := f.read(1024 * 1024):
                        yield chunk
            finally:
                for p in [actual_video_path, audio_path, output_path, Path(srt_path)]:
                    try: p.unlink()
                    except: pass

        return StreamingResponse(iterfile(), media_type="video/mp4", headers={
            "Content-Disposition": 'attachment; filename="dubbed_italian.mp4"',
            "Content-Length": str(file_size),
        })

    except yt_dlp.utils.DownloadError as e:
        raise HTTPException(status_code=400, detail=f"Video download failed: {str(e)}")
    except HTTPException:
        raise
    except Exception as e:
        raise HTTPException(status_code=500, detail=f"Unexpected error: {str(e)}")


@app.post("/merge-files")
async def merge_files(
    italian_text: str = Form(..., description="Italian translated text for subtitles"),
    video: UploadFile = File(..., description="Video file"),
    audio: UploadFile = File(..., description="Italian dubbed audio file"),
    segments_json: str = Form(None, description="Optional Groq segments JSON for accurate timing"),
):
    """
    Accept video + audio as uploaded files (no YouTube download).
    1. Draw black rectangle over French subtitle area
    2. Burn Italian subtitles on top
    3. Replace audio with Italian dubbed audio
    Returns final dubbed + subtitled video.
    """
    job_id = uuid.uuid4().hex
    video_path = DOWNLOAD_DIR / f"{job_id}_video.mp4"
    audio_path = DOWNLOAD_DIR / f"{job_id}_audio.mp3"
    output_path = DOWNLOAD_DIR / f"{job_id}_final.mp4"

    # Save uploaded files
    try:
        with open(video_path, "wb") as f:
            f.write(await video.read())
        with open(audio_path, "wb") as f:
            f.write(await audio.read())
    except Exception as e:
        raise HTTPException(status_code=400, detail=f"Failed to save files: {str(e)}")

    try:
        # Get video dimensions
        probe = subprocess.run([
            "ffprobe", "-v", "error",
            "-select_streams", "v:0",
            "-show_entries", "stream=width,height",
            "-of", "csv=p=0",
            str(video_path)
        ], capture_output=True, text=True)

        dims = probe.stdout.strip().split(",")
        width = int(dims[0]) if len(dims) >= 2 else 1080
        height = int(dims[1]) if len(dims) >= 2 else 1920

        # Get audio duration for subtitle timing
        probe_audio = subprocess.run([
            "ffprobe", "-v", "error",
            "-show_entries", "format=duration",
            "-of", "csv=p=0",
            str(audio_path)
        ], capture_output=True, text=True)
        duration = float(probe_audio.stdout.strip() or 60)

        # Generate SRT
        srt_path = make_srt(italian_text, duration, job_id)

        # Rectangle covers French subtitle area
        rect_y = int(height * 0.72)
        rect_h = int(height * 0.10)
        sub_y = rect_y + int(rect_h * 0.5)

        # Italian subtitles at the bottom with padding, thin outline
        margin_bottom = int(height * 0.09)  # 9% padding from bottom
        vf_filter = (
            f"subtitles={srt_path}:force_style='"
            f"FontSize=10,PrimaryColour=&HFFFFFF,Bold=1,OutlineColour=&H000000,"
            f"Outline=1,Shadow=0,MarginV={margin_bottom},Alignment=2'"
        )

        # Get video duration
        probe_video = subprocess.run([
            "ffprobe", "-v", "error",
            "-show_entries", "format=duration",
            "-of", "csv=p=0",
            str(video_path)
        ], capture_output=True, text=True)
        video_duration = float(probe_video.stdout.strip() or 0)

        # Calculate atempo to stretch/compress Italian audio to match video duration
        # atempo range is 0.5-2.0, chain filters if needed
        af_filter = None
        if video_duration > 0 and duration > 0:
            ratio = duration / video_duration
            ratio = max(0.5, min(2.0, ratio))  # clamp to valid range
            af_filter = f"atempo={ratio:.4f}"

        cmd = [
            "ffmpeg", "-y",
            "-i", str(video_path),
            "-i", str(audio_path),
            "-vf", vf_filter,
        ]
        if af_filter:
            cmd += ["-af", af_filter]
        cmd += [
            "-c:v", "libx264",
            "-preset", "ultrafast",
            "-crf", "28",
            "-c:a", "aac",
            "-b:a", "128k",
            "-map", "0:v:0",
            "-map", "1:a:0",
            "-shortest",
            "-threads", "1",
            str(output_path)
        ]

        result = subprocess.run(cmd, capture_output=True, text=True, timeout=120)

        if result.returncode != 0:
            raise HTTPException(status_code=500, detail=f"FFmpeg error: {result.stderr[-500:]}")

        file_size = output_path.stat().st_size

        def iterfile():
            try:
                with open(output_path, "rb") as f:
                    while chunk := f.read(1024 * 1024):
                        yield chunk
            finally:
                for p in [video_path, audio_path, output_path, Path(srt_path)]:
                    try: p.unlink()
                    except: pass

        return StreamingResponse(iterfile(), media_type="video/mp4", headers={
            "Content-Disposition": 'attachment; filename="dubbed_italian.mp4"',
            "Content-Length": str(file_size),
        })

    except HTTPException:
        raise
    except Exception as e:
        raise HTTPException(status_code=500, detail=f"Unexpected error: {str(e)}")
