FROM node:22-bookworm-slim

ENV PYTHONDONTWRITEBYTECODE=1 \
    PYTHONUNBUFFERED=1 \
    PATH="/opt/venv/bin:$PATH"

RUN apt-get update && apt-get install -y --no-install-recommends \
    python3 \
    python3-venv \
    python3-pip \
    ffmpeg \
    git \
    ca-certificates \
    && rm -rf /var/lib/apt/lists/*

# PO Token provider. Keep this version aligned with requirements.txt.
ARG BGUTIL_VERSION=1.3.2

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

COPY server.py .

EXPOSE 10000

# The bgutil provider listens on the default port expected by the plugin.
CMD ["sh", "-c", "node /opt/bgutil/server/build/main.js --port 4416 & sleep 2; exec python server.py"]
