FROM python:3.11-slim

RUN apt-get update && apt-get install -y ffmpeg && rm -rf /var/lib/apt/lists/*

WORKDIR /app

COPY requirements.txt .
RUN pip install --no-cache-dir -r requirements.txt

# Always install latest yt-dlp — cache busted by build arg
ARG CACHEBUST=1
RUN pip install --no-cache-dir --upgrade yt-dlp

COPY app.py .

ENV PORT=8080
EXPOSE 8080

CMD ["sh", "-c", "uvicorn app:app --host 0.0.0.0 --port ${PORT}"]
