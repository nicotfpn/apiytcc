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

app = FastAPI(title="iPod CC API", version="3.3")

URL_CACHE: Dict[str, Dict[str, Any]] = {}
CACHE_TTL = 3600

COOKIE_FILE = "cookies.txt"
if os.environ.get("YT_COOKIES"):
    with open(COOKIE_FILE, "w", encoding="utf-8") as f:
        f.write(os.environ.get("YT_COOKIES"))

# 'format': 'all' impede que o yt-dlp lance o erro de formato inexistente
COMMON_YTDL_OPTS = {
    'quiet': True,
    'no_warnings': True,
    'socket_timeout': 10,
    'format': 'all',
    'extractor_args': {
        'youtube': {
            'player_client': ['android', 'ios', 'mweb', 'tv_embedded']
        }
    }
}

if os.path.exists(COOKIE_FILE):
    COMMON_YTDL_OPTS['cookiefile'] = COOKIE_FILE

PIPED_INSTANCES = [
    "https://api.piped.video",
    "https://pipedapi.kavin.rocks",
    "https://pipedapi.tokhmi.xyz"
]

INVIDIOUS_INSTANCES = [
    "https://inv.tux.pizza",
    "https://invidious.drgns.space",
    "https://vid.puffyan.us"
]

COBALT_INSTANCES = [
    "https://api.cobalt.tools"
]

# ============================================================
# EXTRAÇÃO DE ÁUDIO VIA APIs E FALLBACKS
# ============================================================

def fetch_via_cobalt(video_id: str) -> str:
    yt_url = f"https://www.youtube.com/watch?v={video_id}"
    payload = json.dumps({"url": yt_url, "downloadMode": "audio"}).encode('utf-8')
    for instance in COBALT_INSTANCES:
        try:
            req = urllib.request.Request(
                f"{instance}/",
                data=payload,
                headers={
                    'Accept': 'application/json',
                    'Content-Type': 'application/json',
                    'User-Agent': 'Mozilla/5.0'
                },
                method='POST'
            )
            with urllib.request.urlopen(req, timeout=5) as resp:
                data = json.loads(resp.read().decode())
                if data.get("status") in ["stream", "redirect", "success"] and data.get("url"):
                    return data["url"]
        except Exception:
            continue
    return None

def fetch_via_piped(video_id: str) -> str:
    for instance in PIPED_INSTANCES:
        try:
            url = f"{instance}/streams/{video_id}"
            req = urllib.request.Request(url, headers={'User-Agent': 'Mozilla/5.0'})
            with urllib.request.urlopen(req, timeout=5) as resp:
                data = json.loads(resp.read().decode())
                audio_streams = data.get("audioStreams", [])
                if audio_streams:
                    return audio_streams[0]["url"]
        except Exception:
            continue
    return None

def fetch_via_invidious(video_id: str) -> str:
    for instance in INVIDIOUS_INSTANCES:
        try:
            url = f"{instance}/api/v1/videos/{video_id}"
            req = urllib.request.Request(url, headers={'User-Agent': 'Mozilla/5.0'})
            with urllib.request.urlopen(req, timeout=5) as resp:
                data = json.loads(resp.read().decode())
                for fmt in data.get("adaptiveFormats", []):
                    if "audio" in fmt.get("type", ""):
                        return fmt["url"]
        except Exception:
            continue
    return None

def fetch_via_ytdl_manual(video_id: str) -> str:
    opts = {**COMMON_YTDL_OPTS, 'skip_download': True}
    with yt_dlp.YoutubeDL(opts) as ydl:
        info = ydl.extract_info(f"https://www.youtube.com/watch?v={video_id}", download=False)
        if not info:
            return None
        
        # Procura qualquer coisa que sirva como áudio direto na lista do YT
        formats = info.get('formats', [])
        
        # 1. Tenta áudio puro
        for fmt in reversed(formats):
            if fmt.get('url') and fmt.get('vcodec') == 'none' and fmt.get('acodec') != 'none':
                return fmt['url']
        
        # 2. Tenta áudio com vídeo (o FFmpeg consegue converter sem problemas)
        for fmt in reversed(formats):
            if fmt.get('url'):
                return fmt['url']
                
        return info.get('url')

async def get_direct_audio_url(video_id: str) -> str:
    # Ordem de prioridade de bypass
    url = await asyncio.to_thread(fetch_via_cobalt, video_id)
    if url: return url

    url = await asyncio.to_thread(fetch_via_piped, video_id)
    if url: return url

    url = await asyncio.to_thread(fetch_via_invidious, video_id)
    if url: return url

    try:
        return await asyncio.to_thread(fetch_via_ytdl_manual, video_id)
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

    # MODO BUSCA (JSON)
    if search:
        try:
            is_url = search.startswith("http://") or search.startswith("https://")
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

    # MODO STREAMING (DFPWM)
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
                        print(f"Erro: Não foi possível obter o link direto para a ID {id}")
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

            except asyncio.CancelledError:  # <-- ERRO DE SINTAXE CORRIGIDO AQUI
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