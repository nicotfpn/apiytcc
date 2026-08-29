#!/bin/sh
set -u

export NO_PROXY="127.0.0.1,localhost,::1"
export no_proxy="$NO_PROXY"

WARP_DIR="/tmp/apiytcc-warp"
WARP_HTTP="http://127.0.0.1:25345"
mkdir -p "$WARP_DIR"
cd "$WARP_DIR"

echo ">>> Tentando ativar Cloudflare WARP GRATIS..."

if [ ! -s wgcf-account.toml ]; then
    n=1
    while [ "$n" -le 5 ]; do
        echo ">>> WARP registro tentativa $n/5"
        wgcf register --accept-tos >/tmp/wgcf-register.log 2>&1 || true

        if [ -s wgcf-account.toml ]; then
            break
        fi

        n=$((n + 1))
        sleep 4
    done
fi

WARP_READY=0

if [ -s wgcf-account.toml ]; then
    wgcf generate >/tmp/wgcf-generate.log 2>&1 || true

    if [ -s wgcf-profile.conf ]; then
        cat > wireproxy.conf <<EOF
WGConfig = $WARP_DIR/wgcf-profile.conf

[http]
BindAddress = 127.0.0.1:25345

[Resolve]
ResolveStrategy = ipv4
EOF

        if wireproxy -n -c "$WARP_DIR/wireproxy.conf" >/tmp/wireproxy-check.log 2>&1; then
            wireproxy -s -c "$WARP_DIR/wireproxy.conf" &
            WIREPROXY_PID=$!

            i=1
            while [ "$i" -le 12 ]; do
                if curl -fsS --connect-timeout 3 --max-time 8 \
                    -x "$WARP_HTTP" \
                    https://www.cloudflare.com/cdn-cgi/trace \
                    2>/dev/null | grep -q '^warp=on$'; then
                    WARP_READY=1
                    break
                fi

                kill -0 "$WIREPROXY_PID" 2>/dev/null || break
                i=$((i + 1))
                sleep 1
            done
        fi
    fi
fi

if [ "$WARP_READY" -eq 1 ]; then
    export YT_PROXY="$WARP_HTTP"
    echo ">>> Cloudflare WARP GRATIS: OK"
    echo ">>> YouTube vai sair por WARP."
else
    unset YT_PROXY
    echo ">>> AVISO: WARP nao iniciou; usando Railway direto."
fi

if [ -n "${YT_PROXY:-}" ]; then
    echo ">>> POT provider usando o mesmo WARP."
    HTTP_PROXY="$YT_PROXY" \
    HTTPS_PROXY="$YT_PROXY" \
    ALL_PROXY="$YT_PROXY" \
    NO_PROXY="$NO_PROXY" \
    node /opt/bgutil/server/build/main.js --port 4416 &
else
    echo ">>> POT provider usando egress direto."
    node /opt/bgutil/server/build/main.js --port 4416 &
fi

sleep 2
cd /app
exec python server.py
