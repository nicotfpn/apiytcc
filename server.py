import os
import time
import json
import asyncio
import subprocess
import urllib.request
import urllib.parse
from typing import Dict, Any
from fastapi import FastAPI, Request
from fastapi.responses import JSONResponse, StreamingResponse
import yt_dlp

app = FastAPI(title="iPod CC API", version="3.1")

URL_CACHE: Dict[str, Dict[str, Any]] = {}
CACHE_TTL = 3600

# Garante a criação dos cookies caso a variável YT_COOKIES esteja configurada no Render
COOKIE_FILE = "cookies.txt"
if os.environ.get("YT_COOKIES"):
    with open(COOKIE_FILE, "w", encoding="utf-8") as f:
        f.write(os.environ.get("YT_COOKIES"))

# Opções globais do yt-dlp com Bypass e Cookies
COMMON_YTDL_OPTS = {
    'quiet': True,
    'no_warnings': True,
    'socket_timeout': 10,
    'extractor_args': {
        'youtube': {
            'player_client': ['tv_embedded', 'android_vr', 'ios', 'mweb']
        }
    }
}

if os.path.exists(COOKIE_FILE):
    COMMON_YTDL_OPTS['cookiefile'] = COOKIE_FILE

PIPED_INSTANCES = [
    "https://api.piped.video",
    "https://pipedapi.kavin.rocks",
    "https://pipedapi.tokhmi.xyz",
    "https://pipedapi.adminforge.de",
]

INVIDIOUS_INSTANCES = [
    "https://inv.tux.pizza",
    "https://invidious.drgns.space",
    "https://vid.puffyan.us",
]

# ============================================================
# FUNÇÕES DE EXTRAÇÃO (PIPED / INVIDIOUS / YT-DLP FALLBACK)
# ============================================================

def search_via_piped(query: str) -> list:
    """Realiza busca de vídeos sem bater diretamente no YouTube."""
    for instance in PIPED_INSTANCES:
        try:
            url = f"{instance}/search?q={urllib.parse.quote(query)}&filter=videos"
            req = urllib.request.Request(url, headers={'User-Agent': 'Mozilla/5.0'})
            with urllib.request.urlopen(req, timeout=4) as resp:
                data = json.loads(resp.read().decode())
                items = data.get("items", [])
                results = [{"status": "ok"}]
                for item in items[:5]:
                    if item.get("type") == "stream":
                        video_id = item.get("url", "").replace("/watch?v=", "")
                        results.append({
                            "type": "video",
                            "id": video_id,
                            "name": item.get("title", "Sem título"),
                            "artist": item.get("uploaderName", "YouTube")
                        })
                if len(results) > 1:
                    return results
        except Exception:
            continue
    return None

def fetch_via_piped(video_id: str) -> str:
    """Obtém URL do áudio via Piped."""
    for instance in PIPED_INSTANCES:
        try:
            url = f"{instance}/streams/{video_id}"
            req = urllib.request.Request(url, headers={'User-Agent': 'Mozilla/5.0'})
            with urllib.request.urlopen(req, timeout=4) as resp:
                data = json.loads(resp.read().decode())
                audio_streams = data.get("audioStreams", [])
                if audio_streams:
                    return audio_streams[0]["url"]
        except Exception:
            continue
    return None

def fetch_via_invidious(video_id: str) -> str:
    """Obtém URL do áudio via Invidious."""
    for instance in INVIDIOUS_INSTANCES:
        try:
            url = f"{instance}/api/v1/videos/{video_id}"
            req = urllib.request.Request(url, headers={'User-Agent': 'Mozilla/5.0'})
            with urllib.request.urlopen(req, timeout=4) as resp:
                data = json.loads(resp.read().decode())
                for fmt in data.get("adaptiveFormats", []):
                    if "audio" in fmt.get("type", ""):
                        return fmt["url"]
        except Exception:
            continue
    return None

async def get_direct_audio_url(video_id: str) -> str:
    # 1. Piped API
    url = await asyncio.to_thread(fetch_via_piped, video_id)
    if url:
        return url

    # 2. Invidious API
    url = await asyncio.to_thread(fetch_via_invidious, video_id)
    if url:
        return url

    # 3. Fallback com yt-dlp + Cookies + Mobile Bypass
    def _ytdl_fallback():
        opts = {**COMMON_YTDL_OPTS, 'skip_download': True}
        with yt_dlp.YoutubeDL(opts) as ydl:
            info = ydl.extract_info(f"https://www.youtube.com/watch?v={video_id}", download=False)
            return info.get('url') if info else None

    try:
        return await asyncio.to_thread(_ytdl_fallback)
    except Exception as e:
        print(f"Erro no fallback do yt-dlp: {e}")
        return None

# ============================================================
# ROTAS DA API
# ============================================================

@app.api_route("/", methods=["GET", "HEAD"])
async def handle_request(request: Request, search: str = None, id: str = None, v: str = "2.1"):
    if request.method == "HEAD" or (not search and not id):
        return JSONResponse(content={"status": "online", "version": v})

    # MODO BUSCA / PLAYLIST (JSON)
    if search:
        try:
            is_url = search.startswith("http://") or search.startswith("https://")
            
            # Tenta buscar pelo Piped primeiro se for termo simples
            if not is_url:
                piped_res = await asyncio.to_thread(search_via_piped, search)
                if piped_res:
                    return JSONResponse(content=piped_res)

            # Fallback para yt-dlp usando cookies e parâmetros corretos
            target = search if is_url else f"ytsearch5:{search}"
            opts = {**COMMON_YTDL_OPTS, 'extract_flat': True, 'skip_download': True}
            
            info = await asyncio.to_thread(lambda: yt_dlp.YoutubeDL(opts).extract_info(target, download=False))
            results = [{"status": "ok"}]

            if info:
                if info.get('_type') == 'playlist' or 'entries' in info:
                    entries = list(info.get('entries', []))
                    if is_url and ('list=' in search or 'playlist' in search):
                        playlist_items = [
                            {
                                "id": entry.get("id"),
                                "name": entry.get("title", "Sem título"),
                                "artist": entry.get("uploader") or entry.get("channel") or "YouTube"
                            }
                            for entry in entries if entry
                        ]
                        results.append({
                            "type": "playlist",
                            "id": info.get("id", ""),
                            "name": info.get("title", "Playlist"),
                            "artist": info.get("uploader") or "YouTube",
                            "playlist_items": playlist_items
                        })
                    else:
                        for entry in entries:
                            if entry:
                                results.append({
                                    "type": "video",
                                    "id": entry.get("id"),
                                    "name": entry.get("title", "Sem título"),
                                    "artist": entry.get("uploader") or entry.get("channel") or "Desconhecido"
                                })
                else:
                    results.append({
                        "type": "video",
                        "id": info.get("id"),
                        "name": info.get("title", "Sem título"),
                        "artist": info.get("uploader") or info.get("channel") or "Desconhecido"
                    })

            return JSONResponse(content=results)
        except Exception as e:
            return JSONResponse(status_code=500, content=[{"status": "error"}, {"message": str(e)}])

    # MODO STREAMING CONTINUO (DFPWM Binário)
    elif id:
        async def stream_bytes():
            proc = None
            try:
                now = time.time()
                audio_url = None

                if id in URL_CACHE and (now - URL_CACHE[id]['timestamp']) < CACHE_TTL:
                    audio_url = URL_CACHE[id]['url']
                else:
                    yt_task = asyncio.create_task(get_direct_audio_url(id))

                    while not yt_task.done():
                        yield b'\x55' * 1500
                        await asyncio.sleep(0.25)

                    audio_url = await yt_task

                    if not audio_url:
                        print(f"Erro: Não foi possível obter o áudio para a ID {id}")
                        return

                    URL_CACHE[id] = {'url': audio_url, 'timestamp': now}

                ffmpeg_cmd = [
                    'ffmpeg',
                    '-reconnect', '1',
                    '-reconnect_streamed', '1',
                    '-reconnect_delay_max', '5',
                    '-i', audio_url,
                    '-f', 'dfpwm',
                    '-ar', '48000',
                    '-ac', '1',
                    'pipe:1'
                ]
                
                proc = subprocess.Popen(ffmpeg_cmd, stdout=subprocess.PIPE, stderr=subprocess.DEVNULL)

                while True:
                    chunk = await asyncio.to_thread(proc.stdout.read, 4096)
                    if not chunk:
                        break
                    yield chunk

            except asyncio.CancelledError:
                pass
            except Exception as e:
                print(f"Erro no stream: {e}")
            finally:
                if proc and proc.poll() is None:
                    proc.terminate()
                    try:
                        proc.wait(timeout=1)
                    except subprocess.TimeoutExpired:
                        proc.kill()
                if proc and proc.stdout:
                    proc.stdout.close()

        return StreamingResponse(
            stream_bytes(), 
            media_type="application/octet-stream",
            headers={"Cache-Control": "no-cache"}
        )

    return JSONResponse(content={"status": "error", "message": "Parâmetros inválidos"})


if __name__ == "__main__":
    import uvicorn
    port = int(os.environ.get("PORT", 10000))
    uvicorn.run(app, host="0.0.0.0", port=port)