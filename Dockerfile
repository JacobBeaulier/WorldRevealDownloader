FROM python:3.12-slim

# ffmpeg/ffprobe needed both by yt-dlp (merging video+audio) and by our own
# H.264 transcode pass in worldreveal.youtube_client.
RUN apt-get update && apt-get install -y --no-install-recommends \
        ffmpeg \
        ca-certificates \
    && rm -rf /var/lib/apt/lists/*

WORKDIR /app

# Install deps first for better Docker layer caching.
COPY requirements.txt pyproject.toml README.md ./
RUN pip install --no-cache-dir -r requirements.txt \
    && pip install --no-cache-dir --upgrade yt-dlp
# ^ yt-dlp is force-upgraded to the latest release on every build. YouTube
# breaks yt-dlp every few weeks and fixes land in new versions; rebuild the
# image (`docker compose build --no-cache`) if you start seeing bot-challenge
# or "Requested format is not available" errors.

# Copy source and install the package.
COPY worldreveal ./worldreveal
RUN pip install --no-cache-dir --no-deps -e .

# Runtime dirs (also mount points — see docker-compose.yml).
RUN mkdir -p /app/credentials /app/logs /app/downloads

# Non-root user for safety. UID/GID 1000 matches the typical host user so
# bind-mounted files stay writable when the container is stopped.
RUN useradd -m -u 1000 worldreveal && chown -R worldreveal:worldreveal /app
USER worldreveal

ENV PYTHONUNBUFFERED=1 \
    PYTHONDONTWRITEBYTECODE=1

# Monitor loop, not a one-shot.
CMD ["worldreveal"]
