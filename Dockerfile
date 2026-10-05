FROM python:3.10-slim-bookworm

# ── System dependencies ───────────────────────────────────────────────────────
# libva2, libva-drm2, libdrm2 are required by jellyfin-ffmpeg for VAAPI.
RUN apt-get update && apt-get install -y --no-install-recommends \
    curl \
    ca-certificates \
    libva2 \
    libva-drm2 \
    libdrm2 \
    && rm -rf /var/lib/apt/lists/*

# ── Jellyfin FFmpeg ───────────────────────────────────────────────────────────
# Full VAAPI decode + encode support, unlike standard apt ffmpeg.
# Pinned to bookworm build to match base image.
RUN curl -fsSL \
    https://github.com/jellyfin/jellyfin-ffmpeg/releases/download/v7.1.3-5/jellyfin-ffmpeg7_7.1.3-5-bookworm_amd64.deb \
    -o /tmp/jellyfin-ffmpeg.deb \
    && apt-get update \
    && apt-get install -y --no-install-recommends /tmp/jellyfin-ffmpeg.deb \
    && rm /tmp/jellyfin-ffmpeg.deb \
    && rm -rf /var/lib/apt/lists/*

# Symlink so existing code calls ffmpeg/ffprobe normally
RUN ln -sf /usr/lib/jellyfin-ffmpeg/ffmpeg  /usr/local/bin/ffmpeg \
 && ln -sf /usr/lib/jellyfin-ffmpeg/ffprobe /usr/local/bin/ffprobe

# ── Python application ────────────────────────────────────────────────────────
WORKDIR /app

COPY requirements.txt .
RUN pip install --no-cache-dir -r requirements.txt

COPY . .

# Everything you keep (database, thumbnails, users, settings) lives here: mount a volume.
VOLUME /app/data
EXPOSE 8008

HEALTHCHECK --interval=30s --timeout=5s --start-period=60s --retries=3 \
    CMD curl -fsS http://127.0.0.1:8008/api/health || exit 1

CMD ["python", "web_server.py"]