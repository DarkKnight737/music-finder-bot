FROM python:3.12-slim

# ffmpeg is required by yt-dlp for MP3 conversion
RUN apt-get update \
    && apt-get install -y --no-install-recommends ffmpeg \
    && rm -rf /var/lib/apt/lists/*

WORKDIR /app
COPY requirements.txt .
RUN pip install --no-cache-dir -r requirements.txt

COPY bot.py music_service.py downloader.py config.py ./

RUN mkdir -p /tmp/_downloads

# Render injects PORT + RENDER_EXTERNAL_URL -> bot.py runs in webhook mode.
# Locally (no such env vars) bot.py falls back to long polling.
CMD ["python", "bot.py"]
