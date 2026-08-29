#!/bin/sh
set -u

export NO_PROXY="127.0.0.1,localhost,::1"
export no_proxy="$NO_PROXY"

WARP_PROXY="http://127.0.0.1:40000"
WARP_OK=0

echo ">>> Iniciando Cloudflare WARP OFICIAL..."

mkdir -p /run/dbus /var/lib/cloudflare-warp
rm -f /run/dbus/pid 2>/dev/null || true

# O WARP funciona sem D-Bus para o proxy, mas isso evita erros ruidosos.
dbus-daemon --system --fork >/tmp/dbus.log 2>&1 || true

warp-svc --accept-tos >/tmp/warp-svc.log 2>&1 &
WARP_SVC_PID=$!

# Espera o daemon responder.
i=1
while [ "$i" -le 20 ]; do
    if warp-cli --accept-tos status >/dev/null 2>&1; then
        break
    fi

    if ! kill -0 "$WARP_SVC_PID" 2>/dev/null; then
        echo ">>> AVISO: warp-svc encerrou."
        break
    fi

    i=$((i + 1))
    sleep 1
done

# Registro pelo cliente OFICIAL. Nao usa mais wgcf.
if kill -0 "$WARP_SVC_PID" 2>/dev/null; then
    if [ ! -s /var/lib/cloudflare-warp/reg.json ]; then
        n=1
        while [ "$n" -le 5 ]; do
            echo ">>> WARP oficial: registro tentativa $n/5"

            if warp-cli --accept-tos registration new >/tmp/warp-register.log 2>&1; then
                break
            fi

            # Algumas versoes podem persistir o registro mesmo se o CLI
            # retornar erro depois da criacao.
            if [ -s /var/lib/cloudflare-warp/reg.json ]; then
                break
            fi

            n=$((n + 1))
            sleep 3
        done
    else
        echo ">>> WARP oficial: registro existente."
    fi
fi

if [ -s /var/lib/cloudflare-warp/reg.json ]; then
    # Proxy mode atual usa MASQUE.
    warp-cli --accept-tos tunnel protocol set MASQUE >/tmp/warp-protocol.log 2>&1 || true
    warp-cli --accept-tos mode proxy >/tmp/warp-mode.log 2>&1 || true
    warp-cli --accept-tos proxy port 40000 >/tmp/warp-port.log 2>&1 || true
    warp-cli --accept-tos connect >/tmp/warp-connect.log 2>&1 || true

    # Espera o proxy local ficar utilizavel.
    i=1
    while [ "$i" -le 25 ]; do
        TRACE="$(
            curl -fsS \
                --connect-timeout 3 \
                --max-time 8 \
                --proxy "$WARP_PROXY" \
                https://www.cloudflare.com/cdn-cgi/trace \
                2>/dev/null || true
        )"

        if printf '%s\n' "$TRACE" | grep -qE '^warp=(on|plus)$'; then
            WARP_OK=1
            break
        fi

        i=$((i + 1))
        sleep 1
    done
fi

if [ "$WARP_OK" -eq 1 ]; then
    export YT_PROXY="$WARP_PROXY"
    echo ">>> Cloudflare WARP OFICIAL: OK"
    echo ">>> Proxy local: 127.0.0.1:40000"
    echo ">>> YouTube/yt-dlp usarao WARP."
else
    unset YT_PROXY
    echo ">>> AVISO: WARP oficial nao conectou; usando Railway direto."

    # Mostra apenas um resumo util do motivo, sem despejar logs enormes.
    if [ -s /tmp/warp-register.log ]; then
        echo ">>> WARP registro: $(tail -n 1 /tmp/warp-register.log | cut -c1-240)"
    fi
    if [ -s /tmp/warp-connect.log ]; then
        echo ">>> WARP connect: $(tail -n 1 /tmp/warp-connect.log | cut -c1-240)"
    fi
fi

# O provider de PO Token deve enxergar o mesmo egress do yt-dlp.
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
