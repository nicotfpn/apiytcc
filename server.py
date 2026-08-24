import os
import time
import json
import asyncio
import subprocess
import urllib.request
from typing import Dict, Any
from fastapi import FastAPI, Request
from fastapi.responses import JSONResponse, StreamingResponse
import yt_dlp

app = FastAPI(title="iPod CC API", version="3.0")

URL_CACHE: Dict[str, Dict[str, Any]] = {}
CACHE_TTL = 3600

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

def fetch_via_piped(video_id: str) -> str:
    """Extrai URL do áudio via Piped API (Bypassa o IP ban do Render)."""
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
    """Extrai URL do áudio via Invidious API (Segunda ponte de backup)."""
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
    """Tenta obter a URL do áudio usando múltiplos proxies."""
    # 1. Tenta Piped API
    url = await asyncio.to_thread(fetch_via_piped, video_id)
    if url:
        return url

    # 2. Tenta Invidious API
    url = await asyncio.to_thread(fetch_via_invidious, video_id)
    if url:
        return url

    # 3. Fallback final: yt-dlp local
    def _ytdl_fallback():
        opts = {'quiet': True, 'skip_download': True, 'socket_timeout': 5}
        with yt_dlp.YoutubeDL(opts) as ydl:
            info = ydl.extract_info(f"https://www.youtube.com/watch?v={video_id}", download=False)
            return info.get('url') if info else None

    try:
        return await asyncio.to_thread(_ytdl_fallback)
    except Exception:
        return None


@app.api_route("/", methods=["GET", "HEAD"])
async def handle_request(request: Request, search: str = None, id: str = None, v: str = "2.1"):
    if request.method == "HEAD" or (not search and not id):
        return JSONResponse(content={"status": "online", "version": v})

    # ============================================================
    # MODO BUSCA / PLAYLIST (JSON)
    # ============================================================
    if search:
        try:
            is_url = search.startswith("http://") or search.startswith("https://")
            target = search if is_url else f"ytsearch5:{search}"
            
            opts = {'quiet': True, 'extract_flat': True, 'skip_download': True}
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

    # ============================================================
    # MODO STREAMING CONTINUO (DFPWM Binário)
    # ============================================================
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

                    # Envia silêncio DFPWM para segurar a conexão do ComputerCraft
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