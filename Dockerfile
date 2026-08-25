FROM node:22-bookworm-slim

ENV PYTHONDONTWRITEBYTECODE=1 \
    PYTHONUNBUFFERED=1 \
    PATH="/opt/venv/bin:$PATH" \
    POT_PROVIDER_URL="http://127.0.0.1:4416"

RUN apt-get update && apt-get install -y --no-install-recommends \
    python3 \
    python3-venv \
    python3-pip \
    ffmpeg \
    git \
    ca-certificates \
    && rm -rf /var/lib/apt/lists/*

# BgUtils PO Token provider. Keep this version aligned with the Python plugin.
ARG BGUTIL_VERSION=1.3.1
RUN git clone --depth 1 --branch "${BGUTIL_VERSION}" \
    https://github.com/Brainicism/bgutil-ytdlp-pot-provider.git /opt/bgutil \
    && cd /opt/bgutil/server \
    && npm ci \
    && npx tsc \
    && npm prune --omit=dev

WORKDIR /app

RUN python3 -m venv /opt/venv
COPY requirements.txt .
RUN pip install --no-cache-dir -U pip \
    && pip install --no-cache-dir -r requirements.txt

# Copy only what the API needs. Cookies are injected with YT_COOKIES.
COPY server.py .

EXPOSE 10000

# Provider and API run in the same DockHosting container.
CMD ["sh", "-c", "node /opt/bgutil/server/build/main.js --port 4416 & sleep 2; exec python server.py"]
