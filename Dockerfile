FROM python:3.12-slim

# ffmpeg (MP3 conversion), nodejs + git (bgutil PO-token provider for YouTube)
RUN apt-get update \
    && apt-get install -y --no-install-recommends ffmpeg ca-certificates curl gnupg git \
    && rm -rf /var/lib/apt/lists/*

# bgutil's PO-token server requires Node >= 22 (Debian ships an older one)
RUN curl -fsSL https://deb.nodesource.com/setup_22.x | bash - \
    && apt-get install -y --no-install-recommends nodejs \
    && rm -rf /var/lib/apt/lists/*

# yt-dlp plugin that generates PO tokens for datacenter IPs
RUN pip install --no-cache-dir bgutil-ytdlp-pot-provider

# POT provider server (Node.js, listens on 127.0.0.1:4416 inside the container).
# Must be version-matched with the pip plugin above.
RUN git clone --depth 1 https://github.com/Brainicism/bgutil-ytdlp-pot-provider.git /opt/bgutil \
    && cd /opt/bgutil/server && npm ci && npx tsc

WORKDIR /app
COPY requirements.txt .
RUN pip install --no-cache-dir -r requirements.txt

COPY bot.py music_service.py downloader.py saavn_source.py config.py ./
COPY static ./static
RUN mkdir -p /tmp/_downloads

COPY entrypoint.sh /entrypoint.sh
RUN chmod +x /entrypoint.sh

# Render injects PORT + RENDER_EXTERNAL_URL -> webhook mode with Mini App.
# Locally (no such env vars) bot.py falls back to long polling.
ENTRYPOINT ["/entrypoint.sh"]
