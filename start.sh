#!/bin/sh
set -u

export NO_PROXY="127.0.0.1,localhost,::1"
export no_proxy="$NO_PROXY"

WARP_PORT="40000"
WARP_HTTP="http://127.0.0.1:${WARP_PORT}"
WARP_SOCKS="socks5h://127.0.0.1:${WARP_PORT}"
WARP_OK=0
YT_PROXY_SELECTED=""

echo ">>> Iniciando Cloudflare WARP OFICIAL..."

mkdir -p /run/dbus /var/lib/cloudflare-warp
rm -f /run/dbus/pid 2>/dev/null || true

dbus-daemon --system --fork >/tmp/dbus.log 2>&1 || true
warp-svc --accept-tos >/tmp/warp-svc.log 2>&1 &
WARP_SVC_PID=$!

i=1
while [ "$i" -le 20 ]; do
    if warp-cli --accept-tos status >/dev/null 2>&1; then break; fi
    if ! kill -0 "$WARP_SVC_PID" 2>/dev/null; then echo ">>> AVISO: warp-svc encerrou."; break; fi
    i=$((i + 1)); sleep 1
done

if kill -0 "$WARP_SVC_PID" 2>/dev/null; then
    if [ ! -s /var/lib/cloudflare-warp/reg.json ]; then
        n=1
        while [ "$n" -le 5 ]; do
            echo ">>> WARP oficial: registro tentativa $n/5"
            warp-cli --accept-tos registration new >/tmp/warp-register.log 2>&1 || true
            if [ -s /var/lib/cloudflare-warp/reg.json ]; then break; fi
            n=$((n + 1)); sleep 3
        done
    else
        echo ">>> WARP oficial: registro existente."
    fi
fi

if [ -s /var/lib/cloudflare-warp/reg.json ]; then
    warp-cli --accept-tos tunnel protocol set MASQUE >/tmp/warp-protocol.log 2>&1 || true
    warp-cli --accept-tos mode proxy >/tmp/warp-mode.log 2>&1 || true
    warp-cli --accept-tos proxy port "$WARP_PORT" >/tmp/warp-port.log 2>&1 || true
    warp-cli --accept-tos connect >/tmp/warp-connect.log 2>&1 || true

    i=1
    while [ "$i" -le 25 ]; do
        if curl -fsS --connect-timeout 3 --max-time 8 --proxy "$WARP_HTTP" \
            https://www.cloudflare.com/cdn-cgi/trace 2>/dev/null | grep -qE '^warp=(on|plus)$'; then
            WARP_OK=1; break
        fi
        i=$((i + 1)); sleep 1
    done
fi

if [ "$WARP_OK" -eq 1 ]; then
    echo ">>> Cloudflare WARP OFICIAL: conectado."
    echo ">>> Testando o egress contra o YouTube..."

    if curl -fsS -o /dev/null --connect-timeout 4 --max-time 10 \
        --proxy "$WARP_SOCKS" https://www.youtube.com/generate_204 \
        2>/tmp/warp-socks-youtube.log; then
        YT_PROXY_SELECTED="$WARP_SOCKS"
        echo ">>> WARP YouTube: SOCKS5 OK"
    elif curl -fsS -o /dev/null --connect-timeout 4 --max-time 10 \
        --proxy "$WARP_HTTP" https://www.youtube.com/generate_204 \
        2>/tmp/warp-http-youtube.log; then
        YT_PROXY_SELECTED="$WARP_HTTP"
        echo ">>> WARP YouTube: HTTP CONNECT OK"
    else
        echo ">>> AVISO: WARP conectou, mas o proxy local nao alcancou YouTube."
        echo ">>> SOCKS: $(tail -n 1 /tmp/warp-socks-youtube.log 2>/dev/null | cut -c1-220)"
        echo ">>> HTTP: $(tail -n 1 /tmp/warp-http-youtube.log 2>/dev/null | cut -c1-220)"
    fi
fi

if [ -n "$YT_PROXY_SELECTED" ]; then
    export YT_PROXY="$YT_PROXY_SELECTED"
    case "$YT_PROXY_SELECTED" in
        socks5h://*) echo ">>> YT_PROXY selecionado: SOCKS5 local :${WARP_PORT}" ;;
        http://*) echo ">>> YT_PROXY selecionado: HTTP CONNECT local :${WARP_PORT}" ;;
    esac
else
    unset YT_PROXY
    echo ">>> AVISO: usando egress direto da Railway."
fi

# Nao setamos HTTP_PROXY/HTTPS_PROXY no processo do BgUtil.
# O plugin do yt-dlp envia o campo proxy em cada POST /get_pot, e o
# provider usa esse mesmo proxy para gerar o token.
echo ">>> Iniciando POT provider..."
node /opt/bgutil/server/build/main.js --port 4416 &

sleep 2
cd /app
exec python server.py
