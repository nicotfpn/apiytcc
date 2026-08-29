FROM node:22-bookworm-slim

ENV PYTHONDONTWRITEBYTECODE=1 \
    PYTHONUNBUFFERED=1 \
    PATH="/opt/venv/bin:$PATH" \
    NO_PROXY="127.0.0.1,localhost,::1" \
    no_proxy="127.0.0.1,localhost,::1"

ARG BGUTIL_VERSION=1.3.2
ARG WGCF_VERSION=2.2.32
ARG WIREPROXY_VERSION=1.1.3

RUN apt-get update && apt-get install -y --no-install-recommends \
    python3 python3-venv python3-pip ffmpeg git ca-certificates curl tar \
    && rm -rf /var/lib/apt/lists/*

RUN git clone --depth 1 --branch "${BGUTIL_VERSION}" \
    https://github.com/Brainicism/bgutil-ytdlp-pot-provider.git /opt/bgutil \
    && cd /opt/bgutil/server \
    && npm ci \
    && npx tsc \
    && npm prune --omit=dev

RUN curl -fsSL \
    "https://github.com/ViRb3/wgcf/releases/download/v${WGCF_VERSION}/wgcf_${WGCF_VERSION}_linux_amd64" \
    -o /usr/local/bin/wgcf \
    && chmod +x /usr/local/bin/wgcf \
    && curl -fsSL \
    "https://github.com/windtf/wireproxy/releases/download/v${WIREPROXY_VERSION}/wireproxy_linux_amd64.tar.gz" \
    -o /tmp/wireproxy.tar.gz \
    && tar -xzf /tmp/wireproxy.tar.gz -C /tmp \
    && find /tmp -maxdepth 2 -type f -name wireproxy -exec cp {} /usr/local/bin/wireproxy \; \
    && chmod +x /usr/local/bin/wireproxy \
    && rm -f /tmp/wireproxy.tar.gz

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
