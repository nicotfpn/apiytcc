import os
import time
import asyncio
import subprocess
from typing import Dict, Any
from fastapi import FastAPI, Request
from fastapi.responses import JSONResponse, StreamingResponse
import yt_dlp

app = FastAPI(title="iPod CC API", version="2.6")

URL_CACHE: Dict[str, Dict[str, Any]] = {}
CACHE_TTL = 3600

COOKIE_FILE = "cookies.txt"
if os.environ.get("YT_COOKIES"):
    with open(COOKIE_FILE, "w", encoding="utf-8") as f:
        f.write(os.environ.get("YT_COOKIES"))

COMMON_YTDL_OPTS = {
    'quiet': True,
    'no_warnings': True,
    'socket_timeout': 10,
    'extractor_args': {
        'youtube': {
            'player_client': ['android', 'ios', 'mweb', 'web']
        }
    }
}

if os.path.exists(COOKIE_FILE):
    COMMON_YTDL_OPTS['cookiefile'] = COOKIE_FILE

YTDL_SEARCH_OPTS = {
    **COMMON_YTDL_OPTS,
    'extract_flat': True,
    'skip_download': True,
}

YTDL_STREAM_OPTS = {
    **COMMON_YTDL_OPTS,
    'skip_download': True,
    'youtube_include_dash_manifest': False,
    'youtube_include_hls_manifest': False,
}

async def fetch_yt_stream_url(video_url: str) -> str:
    """Extrai a URL direta do stream sem quebrar por falta de formato específico."""
    def _extract():
        with yt_dlp.YoutubeDL(YTDL_STREAM_OPTS) as ydl:
            info = ydl.extract_info(video_url, download=False)
            if not info:
                return None
            
            # 1. Retorna a URL principal se selecionada pelo yt-dlp
            if info.get('url'):
                return info['url']
            
            # 2. Fallback: varre a lista de formatos e pega a primeira URL de stream válida
            formats = info.get('formats', [])
            for fmt in reversed(formats):
                if fmt.get('url'):
                    return fmt['url']
            return None

    return await asyncio.to_thread(_extract)

async def fetch_yt_search(target: str) -> dict:
    def _extract():
        with yt_dlp.YoutubeDL(YTDL_SEARCH_OPTS) as ydl:
            return ydl.extract_info(target, download=False)
    return await asyncio.to_thread(_extract)


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
            
            info = await fetch_yt_search(target)
            results = [{"status": "ok"}]

            if info:
                if info.get('_type') == 'playlist' or 'entries' in info:
                    entries = list(info.get('entries', []))
                    if is_url and ('list=' in search or 'playlist' in search):
                        playlist_items = []
                        for entry in entries:
                            if entry:
                                playlist_items.append({
                                    "id": entry.get("id"),
                                    "name": entry.get("title", "Sem título"),
                                    "artist": entry.get("uploader") or entry.get("channel") or "YouTube"
                                })
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
                        "artist": entry.get("uploader") or entry.get("channel") or "Desconhecido"
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
                    yt_task = asyncio.create_task(
                        fetch_yt_stream_url(f"https://www.youtube.com/watch?v={id}")
                    )

                    # Transmite silêncio DFPWM contínuo para segurar a conexão com o Minecraft
                    while not yt_task.done():
                        yield b'\x55' * 1500
                        await asyncio.sleep(0.25)

                    try:
                        audio_url = await yt_task
                    except Exception as yt_err:
                        print(f"Erro na extração do YT: {yt_err}")
                        return

                    if not audio_url:
                        print("Nenhuma URL de stream encontrada no YouTube.")
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