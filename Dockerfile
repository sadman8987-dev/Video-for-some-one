# Video Downloader Bot — production image for Railway / Docker
FROM python:3.12-slim-bookworm

ENV PYTHONDONTWRITEBYTECODE=1 \
    PYTHONUNBUFFERED=1 \
    PIP_NO_CACHE_DIR=1 \
    PIP_DISABLE_PIP_VERSION_CHECK=1 \
    XDG_CACHE_HOME=/tmp/.cache \
    HOME=/tmp

# System deps: ffmpeg (merge/extract), aria2 (optional faster downloads),
# git (pip install from git), ca-certificates, curl
RUN apt-get update \
    && apt-get install -y --no-install-recommends \
        ffmpeg \
        aria2 \
        ca-certificates \
        curl \
        git \
        unzip \
    && rm -rf /var/lib/apt/lists/*

# Deno is required by modern yt-dlp for some YouTube JS challenges
RUN curl -fsSL https://deno.land/install.sh | DENO_INSTALL=/usr/local sh \
    && deno --version

WORKDIR /app

# Install Python deps first (better layer cache)
COPY requirements.txt .
RUN python -m pip install --upgrade pip setuptools wheel \
    && pip install --no-cache-dir -r requirements.txt

# App source
COPY main.py youtube_handler.py smart_download_engine.py ./

# Non-root user (Railway-friendly)
RUN useradd --create-home --uid 10001 --shell /usr/sbin/nologin botuser \
    && mkdir -p /tmp/.cache \
    && chown -R botuser:botuser /app /tmp/.cache

USER 10001:10001

# Long-polling Telegram bot — no HTTP port required
CMD ["python", "-u", "main.py"]
