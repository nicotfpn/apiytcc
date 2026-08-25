import os
import time
import json
import re
import asyncio
import subprocess
import urllib.request
import urllib.error
import urllib.parse
from concurrent.futures import ThreadPoolExecutor, as_completed
from typing import Dict, Any, List
from fastapi import FastAPI, Request
from fastapi.responses import JSONResponse, StreamingResponse
import yt_dlp

app = FastAPI(title="iPod CC API", version="4.2")

URL_CACHE: Dict[str, Dict[str, Any]] = {}
CACHE_TTL = 600
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

# Opções mais "leves" só para busca (ytsearch). O 'player_skip': ['webpage']
# e o player_client restrito a web/android são pensados pra extração de áudio
# de um vídeo específico - na busca eles não ajudam em nada e são mais uma
# forma de a extração quebrar silenciosamente quando o YouTube muda algo.
SEARCH_YTDL_OPTS = {
    'quiet': True,
    'no_warnings': True,
    'socket_timeout': 10,
    'extract_flat': True,
    'skip_download': True,
    'http_headers': COMMON_YTDL_OPTS['http_headers'],
}
if os.path.exists(COOKIE_FILE):
    SEARCH_YTDL_OPTS['cookiefile'] = COOKIE_FILE

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

# ============================================================
# LISTAS DE PROXIES (Piped e Invidious)
# ============================================================
PIPED_INSTANCES = [
    "https://pipedapi.kavin.rocks",
    "https://pipedapi.leptons.xyz",
    "https://pipedapi.adminforge.de",
    "https://api.piped.yt",
    "https://pipedapi.drgns.space",
    "https://pipedapi.owo.si",
    "https://pipedapi-libre.kavin.rocks",
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

def fetch_via_piped(video_id: str):
    for instance in PIPED_INSTANCES:
        try:
            url = f"{instance}/streams/{video_id}"
            req = urllib.request.Request(url, headers=BROWSER_HEADERS)
            with urllib.request.urlopen(req, timeout=4) as resp:
                data = json.loads(resp.read().decode())
                audio_streams = data.get("audioStreams", [])
                valid = [a for a in audio_streams if a.get("url")]
                if valid:
                    valid.sort(key=lambda a: (a.get("bitrate") or a.get("bitrate") or 0), reverse=True)
                    best = valid[0]
                    return {
                        "url": best.get("url"),
                        "headers": best.get("httpHeaders") or {},
                        "source": "piped",
                    }
        except Exception as e:
            print(f">>> [PIPED AUDIO FALHOU em {instance}] {e}")
            continue
    return None


def _piped_search_one(instance: str, query: str, limit: int) -> List[dict]:
    try:
        url = f"{instance}/search?q={urllib.parse.quote(query)}&filter=videos"
        req = urllib.request.Request(url, headers=BROWSER_HEADERS)
        with urllib.request.urlopen(req, timeout=4) as resp:
            data = json.loads(resp.read().decode())
        results = []
        for item in data[:limit]:
            vid = item.get("url", "")
            if vid.startswith("/watch?v="):
                vid = vid.split("v=", 1)[1].split("&", 1)[0]
            elif "watch?v=" in vid:
                vid = vid.split("watch?v=", 1)[1].split("&", 1)[0]
            if vid:
                results.append({
                    "type": "video",
                    "id": vid,
                    "name": item.get("title", "Sem título"),
                    "artist": item.get("uploaderName") or item.get("uploader") or "YouTube",
                })
        return results
    except Exception as e:
        print(f">>> [PIPED SEARCH FALHOU em {instance}] {e}")
        return []


def search_via_piped(query: str, limit: int = 5) -> List[dict]:
    with ThreadPoolExecutor(max_workers=len(PIPED_INSTANCES)) as pool:
        futures = [pool.submit(_piped_search_one, instance, query, limit) for instance in PIPED_INSTANCES]
        for fut in as_completed(futures):
            try:
                result = fut.result()
                if result:
                    return result
            except Exception:
                pass
    return []


def _invidious_search_one(instance: str, query: str, limit: int) -> List[dict]:
    try:
        url = f"{instance}/api/v1/search?q={urllib.parse.quote(query)}&type=video"
        req = urllib.request.Request(url, headers=BROWSER_HEADERS)
        with urllib.request.urlopen(req, timeout=4) as resp:
            data = json.loads(resp.read().decode())
        results = []
        for item in data[:limit]:
            vid = item.get("videoId")
            if vid:
                results.append({
                    "type": "video",
                    "id": vid,
                    "name": item.get("title", "Sem título"),
                    "artist": item.get("author") or "YouTube",
                })
        return results
    except Exception as e:
        print(f">>> [INVIDIOUS SEARCH FALHOU em {instance}] {e}")
        return []


def search_via_invidious(query: str, limit: int = 5) -> List[dict]:
    with ThreadPoolExecutor(max_workers=len(INVIDIOUS_INSTANCES)) as pool:
        futures = [pool.submit(_invidious_search_one, instance, query, limit) for instance in INVIDIOUS_INSTANCES]
        for fut in as_completed(futures):
            try:
                result = fut.result()
                if result:
                    return result
            except Exception:
                pass
    return []


def fetch_via_invidious(video_id: str):
    for instance in INVIDIOUS_INSTANCES:
        try:
            url = f"{instance}/api/v1/videos/{video_id}"
            req = urllib.request.Request(url, headers=BROWSER_HEADERS)
            with urllib.request.urlopen(req, timeout=4) as resp:
                data = json.loads(resp.read().decode())
            formats = data.get("adaptiveFormats", []) + data.get("formatStreams", [])
            audio = [f for f in formats if f.get("url") and "audio" in (f.get("type") or "")]
            if audio:
                audio.sort(key=lambda f: f.get("bitrate") or 0, reverse=True)
                best = audio[0]
                return {"url": best.get("url"), "headers": {}, "source": "invidious"}
        except Exception as e:
            print(f">>> [INVIDIOUS AUDIO FALHOU em {instance}] {e}")
            continue
    return None


def pick_best_audio_source(info: dict):
    formats = info.get('formats', []) if info else []
    def has_audio(fmt):
        return fmt.get('url') and fmt.get('acodec') not in (None, 'none')

    audio_only = [f for f in formats if has_audio(f) and f.get('vcodec') in (None, 'none')]
    if audio_only:
        audio_only.sort(key=lambda f: f.get('abr') or 0, reverse=True)
        best = audio_only[0]
    else:
        with_audio = [f for f in formats if has_audio(f)]
        if not with_audio:
            return None
        with_audio.sort(key=lambda f: f.get('abr') or 0, reverse=True)
        best = with_audio[0]

    return {
        "url": best.get('url'),
        "headers": best.get('http_headers') or info.get('http_headers') or {},
        "source": "yt-dlp",
    }


def fetch_via_ytdl_manual(video_id: str):
    opts = {
        **COMMON_YTDL_OPTS,
        'skip_download': True,
        'format': 'bestaudio/best',
    }
    with yt_dlp.YoutubeDL(opts) as ydl:
        info = ydl.extract_info(f"https://www.youtube.com/watch?v={video_id}", download=False)
        return pick_best_audio_source(info)


async def get_direct_audio_source(video_id: str):
    # Primeiro tenta yt-dlp: normalmente fornece a URL mais compatível + os
    # headers que o servidor do YouTube espera.
    for label, use_cookies in (("cookies", COOKIES_VALID), ("sem_cookies", False)):
        if label == "cookies" and not use_cookies:
            continue
        try:
            source = await asyncio.wait_for(
                asyncio.to_thread(fetch_via_ytdl_manual, video_id), timeout=12
            )
            if source and source.get("url"):
                print(f">>> [AUDIO] yt-dlp ({label}) encontrou áudio para {video_id}")
                return source
        except Exception as e:
            print(f">>> [AUDIO] yt-dlp ({label}) falhou para {video_id}: {e}")

    # Fallbacks em paralelo.
    tasks = [
        asyncio.create_task(asyncio.to_thread(fetch_via_piped, video_id)),
        asyncio.create_task(asyncio.to_thread(fetch_via_invidious, video_id)),
    ]
    try:
        for coro in asyncio.as_completed(tasks, timeout=8):
            try:
                result = await coro
                if result and result.get("url"):
                    print(f">>> [AUDIO] fallback {result.get('source')} encontrou áudio para {video_id}")
                    return result
            except Exception:
                continue
    except asyncio.TimeoutError:
        pass
    finally:
        for t in tasks:
            if not t.done():
                t.cancel()

    print(f">>> [ERRO CRÍTICO] Nenhuma fonte de áudio funcionou para: {video_id}")
    return None

# ============================================================
# ROTAS DA API
# ============================================================

@app.api_route("/", methods=["GET", "HEAD"])
async def handle_request(request: Request, search: str = None, id: str = None, v: str = "2.1"):
    if request.method == "HEAD" or (not search and not id):
        return JSONResponse(content={"status": "online", "version": v})

    if search:
        try:
            is_url = search.startswith("http://") or search.startswith("https://")
            target = search if is_url else f"ytsearch5:{search}"

            info = None
            search_failed = False
            # Timeout rigido para a busca nao travar o servidor
            try:
                info = await asyncio.wait_for(
                    asyncio.to_thread(lambda: yt_dlp.YoutubeDL(SEARCH_YTDL_OPTS).extract_info(target, download=False)),
                    timeout=8
                )
            except asyncio.TimeoutError:
                print(f">>> [TIMEOUT] A busca por '{search}' demorou muito para responder.")
                search_failed = True
            except Exception as e:
                import traceback
                print(f">>> [YT-DLP FALHOU NA BUSCA] '{search}': {e}")
                traceback.print_exc()
                search_failed = True
                search_failed = True

            # Se o yt-dlp falhou (ou não achou nada) e não é uma URL direta,
            # tenta o Piped como plano B — ele não depende de cookies nem do
            # extrator do YouTube, então continua funcionando mesmo quando o
            # yt-dlp quebra por causa de alguma mudança no YouTube.
            if not is_url and (search_failed or not info or not (info.get('entries') or [])):
                piped_results = await asyncio.to_thread(search_via_piped, search)
                if piped_results:
                    return JSONResponse(content=[{"status": "ok"}] + piped_results)

                invidious_results = await asyncio.to_thread(search_via_invidious, search)
                if invidious_results:
                    return JSONResponse(content=[{"status": "ok"}] + invidious_results)

                if search_failed:
                    return JSONResponse(status_code=504, content=[{"status": "error"}, {"message": "Não foi possível buscar (yt-dlp, Piped e Invidious falharam)"}])

            if search_failed and is_url:
                return JSONResponse(status_code=502, content=[{"status": "error"}, {"message": "Não foi possível processar essa URL/playlist"}])

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
            print(f">>> [ERRO NA BUSCA] {e}")
            return JSONResponse(status_code=500, content=[{"status": "error"}, {"message": str(e)}])

    elif id:
        if not YOUTUBE_ID_REGEX.match(id):
            return JSONResponse(status_code=400, content={"status": "error", "message": "ID de vídeo inválido"})

        now = time.time()
        audio_source = None

        if id in URL_CACHE and (now - URL_CACHE[id]['timestamp']) < CACHE_TTL:
            audio_source = URL_CACHE[id]['source']
        else:
            audio_source = await get_direct_audio_source(id)
            if audio_source and audio_source.get('url'):
                URL_CACHE[id] = {'source': audio_source, 'timestamp': now}

        if not audio_source or not audio_source.get('url'):
            return JSONResponse(
                status_code=502,
                content={"status": "error", "message": f"Não foi possível obter áudio válido para {id}"}
            )

        async def stream_bytes():
            proc = None
            try:
                ffmpeg_cmd = [
                    'ffmpeg',
                    '-hide_banner',
                    '-loglevel', 'error',
                    '-reconnect', '1',
                    '-reconnect_streamed', '1',
                    '-reconnect_at_eof', '1',
                    '-reconnect_on_network_error', '1',
                    '-reconnect_delay_max', '10',
                    '-rw_timeout', '15000000',
                ]

                input_headers = audio_source.get('headers') or {}
                if input_headers:
                    header_lines = []
                    for hk, hv in input_headers.items():
                        if hv is not None:
                            header_lines.append(f"{hk}: {hv}")
                    if header_lines:
                        ffmpeg_cmd += ['-headers', '\r\n'.join(header_lines) + '\r\n']

                ffmpeg_cmd += [
                    '-i', audio_source['url'],
                    '-map', '0:a:0?',
                    '-vn',
                    '-sn',
                    '-dn',
                    '-f', 'dfpwm',
                    '-ar', '48000',
                    '-ac', '1',
                    'pipe:1'
                ]
                
                proc = subprocess.Popen(
                    ffmpeg_cmd,
                    stdout=subprocess.PIPE,
                    stderr=subprocess.DEVNULL
                )

                while True:
                    chunk = await asyncio.to_thread(proc.stdout.read, 4096)
                    if not chunk:
                        if proc.poll() is not None:
                            print(f">>> [FFMPEG] encerrou com código {proc.returncode} para {id}")
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