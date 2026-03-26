# YT-DLP API on Render

A FastAPI service that uses **yt-dlp** + **ffmpeg** to download YouTube videos and audio, deployable on [Render](https://render.com).

---

## Endpoints

| Method | Path | Description |
|--------|------|-------------|
| GET | `/` | Health check |
| GET | `/metadata?video_url=...` | Get video metadata (no download) |
| GET | `/download?video_url=...&quality=high` | Download video (mp4) |
| GET | `/audio?video_url=...` | Download audio only (mp3) |

### Quality options for `/download`
- `high` – best available resolution (default)
- `medium` – up to 720p
- `low` – up to 480p

---

## Deploy on Render

### Option A — Blueprint (recommended)

1. Push this repo to GitHub
2. Go to [render.com](https://render.com) → **New** → **Blueprint**
3. Connect your repo — Render will detect `render.yaml` automatically
4. Click **Apply** — done ✅

### Option B — Manual via Dashboard

1. Push this repo to GitHub
2. Go to Render → **New** → **Web Service**
3. Connect your repo
4. Set **Runtime** to **Docker**
5. Leave everything else as default
6. Click **Create Web Service**

---

## Run Locally

```bash
# Install dependencies (requires ffmpeg installed on your system)
pip install -r requirements.txt

# Run
uvicorn app:app --reload
```

Or with Docker:

```bash
docker build -t yt-dlp-api .
docker run -p 8080:8080 yt-dlp-api
```

---

## Important Notes

- **ffmpeg is required** — it's installed inside the Docker image automatically. Without it, yt-dlp can't merge separate video and audio streams.
- **Render's free tier has a 30-second request timeout** — large/long videos may time out. Upgrade to the Starter or Standard plan for longer timeouts and more RAM.
- Files are written to `/tmp` and deleted immediately after streaming — nothing is stored permanently.
- yt-dlp can break when YouTube changes its internals. Keep `yt-dlp` updated in `requirements.txt`.

---

## Keeping yt-dlp Updated

YouTube frequently changes how it serves videos. If downloads suddenly stop working, update the version pin in `requirements.txt`:

```
yt-dlp==<latest version from https://github.com/yt-dlp/yt-dlp/releases>
```

Then redeploy.
