import os
import time
import json
import re
import asyncio
import subprocess
import urllib.request
import urllib.error
import urllib.parse
from typing import Dict, Any, List
from fastapi import FastAPI, Request
from fastapi.responses import JSONResponse, StreamingResponse
import yt_dlp

app = FastAPI(title="iPod CC API", version="4.0")

URL_CACHE: Dict[str, Dict[str, Any]] = {}
CACHE_TTL = 2700
YOUTUBE_ID_REGEX = re.compile(r"^[a-zA-Z0-9_-]{11}$")

# ============================================================
# CONFIGURAÇÃO DE COOKIES E YT-DLP BLINDADO
# ============================================================
COOKIE_FILE = "cookies.txt"

if os.path.exists(COOKIE_FILE):
    print(">>> SUCESSO: Arquivo cookies.txt encontrado na raiz do projeto!")
else:
    print(">>> AVISO: cookies.txt NÃO foi encontrado na raiz.")
    if os.environ.get("YT_COOKIES"):
        raw_cookies = os.environ.get("YT_COOKIES").replace('\\n', '\n').replace('\r', '')
        with open(COOKIE_FILE, "w", encoding="utf-8") as f:
            f.write(raw_cookies)
        print(">>> Cookie gerado através da variável de ambiente.")

COMMON_YTDL_OPTS = {
    'quiet': True,
    'no_warnings': True,
    'socket_timeout': 15,
    'format': 'all',
    'http_headers': {
        'User-Agent': 'Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/125.0.0.0 Safari/537.36',
        'Accept-Language': 'en-US,en;q=0.9',
    },
    'extractor_args': {
        'youtube': {
            'player_client': ['web', 'android'],
            'player_skip': ['webpage']
        }
    }
}

if os.path.exists(COOKIE_FILE):
    COMMON_YTDL_OPTS['cookiefile'] = COOKIE_FILE

def validate_cookies() -> bool:
    if not os.path.exists(COOKIE_FILE):
        return False
    try:
        test_opts = {**COMMON_YTDL_OPTS, 'extract_flat': True, 'skip_download': True}
        with yt_dlp.YoutubeDL(test_opts) as ydl:
            info = ydl.extract_info("https://www.youtube.com/watch?v=dQw4w9WgXcQ", download=False)
            return info is not None and info.get('id') is not None
    except Exception as e:
        print(f">>> COOKIES INVÁLIDOS OU EXPIRADOS: {e}")
        return False

COOKIES_VALID = validate_cookies()
if COOKIES_VALID:
    print(">>> SUCESSO: Cookies do YouTube validados e funcionais!")
else:
    print(">>> AVISO: Os cookies presentes falharam na validação ativa ou não existem.")

# ============================================================
# LISTAS DE PROXIES (Piped e Invidious)
# ============================================================
PIPED_INSTANCES = [
    "https://api.piped.video",
    "https://pipedapi.kavin.rocks",
    "https://pipedapi.tokhmi.xyz",
    "https://pipedapi.adminforge.de"
]

INVIDIOUS_INSTANCES = [
    "https://inv.tux.pizza",
    "https://invidious.drgns.space",
    "https://vid.puffyan.us",
    "https://invidious.slipfox.xyz"
]

BROWSER_HEADERS = {
    'User-Agent': 'Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36',
    'Accept': 'application/json'
}

def fetch_via_piped(video_id: str) -> str:
    for instance in PIPED_INSTANCES:
        try:
            url = f"{instance}/streams/{video_id}"
            req = urllib.request.Request(url, headers=BROWSER_HEADERS)
            with urllib.request.urlopen(req, timeout=4) as resp:
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
            req = urllib.request.Request(url, headers=BROWSER_HEADERS)
            with urllib.request.urlopen(req, timeout=4) as resp:
                data = json.loads(resp.read().decode())
                for fmt in data.get("adaptiveFormats", []):
                    if "audio" in fmt.get("type", ""):
                        return fmt["url"]
        except Exception:
            continue
    return None

def pick_best_audio_url(formats: List[dict]) -> str:
    def has_real_audio(fmt):
        acodec = fmt.get('acodec')
        return fmt.get('url') and acodec not in (None, 'none', '')

    # 1. Prioridade: áudio puro (sem vídeo) de alta qualidade
    audio_only = [f for f in formats if has_real_audio(f) and f.get('vcodec') in (None, 'none')]
    if audio_only:
        audio_only.sort(key=lambda f: f.get('abr') or 0, reverse=True)
        return audio_only[0]['url']

    # 2. Fallback: formato misto que contenha áudio real válido
    with_audio = [f for f in formats if has_real_audio(f)]
    if with_audio:
        with_audio.sort(key=lambda f: f.get('abr') or 0, reverse=True)
        return with_audio[0]['url']

    # 3. Nenhuma url com áudio real identificada
    return None

def fetch_via_ytdl_manual(video_id: str) -> str:
    opts = {**COMMON_YTDL_OPTS, 'skip_download': True}
    with yt_dlp.YoutubeDL(opts) as ydl:
        info = ydl.extract_info(f"https://www.youtube.com/watch?v={video_id}", download=False)
        if not info:
            return None
        return pick_best_audio_url(info.get('formats', []))

async def get_direct_audio_url(video_id: str) -> str:
    # 1. Tenta yt-dlp com cookies primeiro
    if COOKIES_VALID:
        try:
            url = await asyncio.wait_for(
                asyncio.to_thread(fetch_via_ytdl_manual, video_id), timeout=8
            )
            if url:
                return url
        except Exception as e:
            print(f"yt-dlp com cookies falhou para {video_id}: {e}")

    # 2. Se falhar, dispara Piped e Invidious em paralelo com gerenciamento de tasks órfãs
    tasks = [
        asyncio.create_task(asyncio.to_thread(fetch_via_piped, video_id)),
        asyncio.create_task(asyncio.to_thread(fetch_via_invidious, video_id)),
    ]
    
    try:
        for coro in asyncio.as_completed(tasks, timeout=6):
            try:
                result = await coro
                if result:
                    for t in tasks:
                        if not t.done():
                            t.cancel()
                    return result
            except Exception:
                continue
    except asyncio.TimeoutError:
        pass
    finally:
        for t in tasks:
            if not t.done():
                t.cancel()

    # 3. Fallback final com yt-dlp puro sem cookies
    try:
        return await asyncio.wait_for(
            asyncio.to_thread(fetch_via_ytdl_manual, video_id), timeout=10
        )
    except Exception as e:
        print(f"Erro fatal no yt-dlp para {video_id}: {e}")
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
                            for entry in entries if entry and entry.get("id")
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
                            if entry and entry.get("id"):
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
        if not YOUTUBE_ID_REGEX.match(id):
            return JSONResponse(status_code=400, content={"status": "error", "message": "ID de vídeo inválido"})

        now = time.time()
        audio_url = None

        # Resolução limpa ANTES de iniciar o StreamingResponse (evita HTTP 200 falso)
        if id in URL_CACHE and (now - URL_CACHE[id]['timestamp']) < CACHE_TTL:
            audio_url = URL_CACHE[id]['url']
        else:
            audio_url = await get_direct_audio_url(id)
            if audio_url:
                URL_CACHE[id] = {'url': audio_url, 'timestamp': now}

        if not audio_url:
            return JSONResponse(
                status_code=502,
                content={"status": "error", "message": f"Não foi possível obter áudio válido para {id}"}
            )

        async def stream_bytes():
            proc = None
            try:
                ffmpeg_cmd = [
                    'ffmpeg',
                    '-reconnect', '1',
                    '-reconnect_streamed', '1',
                    '-reconnect_delay_max', '10',
                    '-rw_timeout', '15000000',
                    '-i', audio_url,
                    '-f', 'dfpwm',
                    '-ar', '48000',
                    '-ac', '1',
                    'pipe:1'
                ]
                
                proc = subprocess.Popen(ffmpeg_cmd, stdout=subprocess.PIPE, stderr=subprocess.PIPE)

                while True:
                    chunk = await asyncio.to_thread(proc.stdout.read, 4096)
                    if not chunk:
                        if proc.poll() is not None:
                            err_output = proc.stderr.read().decode('utf-8', errors='ignore')
                            if err_output:
                                print(f"Erro interno do FFmpeg: {err_output[-300:]}")
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
                if proc and proc.stderr:
                    proc.stderr.close()

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