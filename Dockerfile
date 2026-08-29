FROM node:22-bookworm-slim

ENV PYTHONDONTWRITEBYTECODE=1 \
    PYTHONUNBUFFERED=1 \
    PATH="/opt/venv/bin:$PATH" \
    NO_PROXY="127.0.0.1,localhost,::1" \
    no_proxy="127.0.0.1,localhost,::1" \
    DEBIAN_FRONTEND=noninteractive

ARG BGUTIL_VERSION=1.3.2

RUN apt-get update && apt-get install -y --no-install-recommends \
    python3 \
    python3-venv \
    python3-pip \
    ffmpeg \
    git \
    ca-certificates \
    curl \
    gnupg \
    dbus \
    && rm -rf /var/lib/apt/lists/*

# Cloudflare WARP OFICIAL para Debian 12 (bookworm).
RUN curl -fsSL https://pkg.cloudflareclient.com/pubkey.gpg \
    | gpg --yes --dearmor \
      --output /usr/share/keyrings/cloudflare-warp-archive-keyring.gpg \
    && echo "deb [arch=amd64 signed-by=/usr/share/keyrings/cloudflare-warp-archive-keyring.gpg] https://pkg.cloudflareclient.com/ bookworm main" \
      > /etc/apt/sources.list.d/cloudflare-client.list \
    && apt-get update \
    && apt-get install -y --no-install-recommends cloudflare-warp \
    && rm -rf /var/lib/apt/lists/*

# BgUtil PO Token Provider.
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
COPY start.sh .

RUN chmod +x /app/start.sh

EXPOSE 10000

CMD ["/app/start.sh"]
