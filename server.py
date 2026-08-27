import os
import time
import json
import re
import asyncio
import subprocess
import urllib.request
import urllib.parse
from concurrent.futures import ThreadPoolExecutor, as_completed
from typing import Dict, Any, List, Optional

from fastapi import FastAPI, Request
from fastapi.responses import JSONResponse, StreamingResponse
import yt_dlp


app = FastAPI(title="iPod CC API", version="4.3")

URL_CACHE: Dict[str, Dict[str, Any]] = {}
CACHE_TTL = 600

YOUTUBE_ID_REGEX = re.compile(r"^[a-zA-Z0-9_-]{11}$")

COOKIE_FILE = "cookies.txt"

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
    "https://invidious.slipfox.xyz",
]

BROWSER_HEADERS = {
    "User-Agent": (
        "Mozilla/5.0 (Windows NT 10.0; Win64; x64) "
        "AppleWebKit/537.36 (KHTML, like Gecko) "
        "Chrome/125.0.0.0 Safari/537.36"
    ),
    "Accept-Language": "en-US,en;q=0.9",
}

API_HEADERS = {
    "User-Agent": BROWSER_HEADERS["User-Agent"],
    "Accept": "application/json",
}


def prepare_cookie_file() -> bool:
    raw = os.environ.get("YT_COOKIES", "")

    if raw:
        raw = raw.replace("\\n", "\n").replace("\r", "")
        with open(COOKIE_FILE, "w", encoding="utf-8") as handle:
            handle.write(raw)
        print(">>> YT_COOKIES carregado.")

    exists = os.path.isfile(COOKIE_FILE)

    if exists:
        print(">>> Cookies disponiveis.")
    else:
        print(">>> Sem cookies. Videos publicos usam PO Token primeiro.")

    return exists


HAS_COOKIES = prepare_cookie_file()

try:
    import yt_dlp_ejs
    print(">>> yt-dlp EJS disponivel.")
except Exception:
    print(">>> AVISO: yt-dlp EJS nao encontrado.")

print(">>> Runtime JS configurado: Node 22")


def make_ytdl_opts(
    *,
    use_cookies: bool,
    player_client: str = "mweb",
    flat: bool = False,
    timeout: int = 15,
) -> Dict[str, Any]:
    """
    O provider bgutil e descoberto automaticamente pelo yt-dlp plugin.

    Fluxo principal:
      mweb + PO Token provider

    Cookies sao opcionais e usados apenas como fallback para conteudo
    que realmente precise de sessao.
    """
    opts: Dict[str, Any] = {
        "quiet": True,
        "no_warnings": True,
        "socket_timeout": timeout,
        "skip_download": True,

        # YouTube 2026: yt-dlp precisa de um runtime JS + EJS
        # para resolver os challenges e liberar todos os formatos.
        # O Docker ja usa Node 22, mas o yt-dlp nao habilita Node
        # automaticamente; sem isso alguns formatos simplesmente somem.
        "js_runtimes": {
            "node": {},
        },

        "http_headers": dict(BROWSER_HEADERS),
        "extractor_args": {
            "youtube": {
                "player_client": [player_client],
            }
        },
    }

    if flat:
        opts["extract_flat"] = True
    else:
        opts["format"] = "bestaudio*/best*"

    if use_cookies and HAS_COOKIES:
        opts["cookiefile"] = COOKIE_FILE

    return opts


SEARCH_YTDL_OPTS: Dict[str, Any] = {
    "quiet": True,
    "no_warnings": True,
    "socket_timeout": 10,
    "extract_flat": True,
    "skip_download": True,
    "js_runtimes": {
        "node": {},
    },
    "http_headers": dict(BROWSER_HEADERS),
}

# Para busca, cookies so sao usados se existirem.
# A busca flat costuma ser mais leve que extrair o player inteiro.
if HAS_COOKIES:
    SEARCH_YTDL_OPTS["cookiefile"] = COOKIE_FILE


def is_bot_check_error(exc: Exception) -> bool:
    text = str(exc).casefold()
    needles = (
        "sign in to confirm",
        "not a bot",
        "bot check",
        "po token",
        "http error 403",
    )
    return any(needle in text for needle in needles)


def fetch_via_piped(video_id: str):
    for instance in PIPED_INSTANCES:
        try:
            url = f"{instance}/streams/{video_id}"
            req = urllib.request.Request(url, headers=API_HEADERS)

            with urllib.request.urlopen(req, timeout=4) as resp:
                data = json.loads(resp.read().decode())

            audio_streams = data.get("audioStreams", [])
            valid = [item for item in audio_streams if item.get("url")]

            if valid:
                valid.sort(
                    key=lambda item: item.get("bitrate") or 0,
                    reverse=True,
                )

                best = valid[0]

                return {
                    "url": best.get("url"),
                    "headers": best.get("httpHeaders") or {},
                    "source": "piped",
                }

        except Exception:
            continue

    return None


def _piped_search_one(
    instance: str,
    query: str,
    limit: int,
) -> List[dict]:
    try:
        url = (
            f"{instance}/search?"
            f"q={urllib.parse.quote(query)}&filter=videos"
        )

        req = urllib.request.Request(url, headers=API_HEADERS)

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
                results.append(
                    {
                        "type": "video",
                        "id": vid,
                        "name": item.get("title", "Sem titulo"),
                        "artist": (
                            item.get("uploaderName")
                            or item.get("uploader")
                            or "YouTube"
                        ),
                    }
                )

        return results

    except Exception:
        return []


def search_via_piped(
    query: str,
    limit: int = 5,
) -> List[dict]:
    with ThreadPoolExecutor(
        max_workers=len(PIPED_INSTANCES)
    ) as pool:
        futures = [
            pool.submit(
                _piped_search_one,
                instance,
                query,
                limit,
            )
            for instance in PIPED_INSTANCES
        ]

        for future in as_completed(futures):
            try:
                result = future.result()
                if result:
                    return result
            except Exception:
                pass

    return []


def _invidious_search_one(
    instance: str,
    query: str,
    limit: int,
) -> List[dict]:
    try:
        url = (
            f"{instance}/api/v1/search?"
            f"q={urllib.parse.quote(query)}&type=video"
        )

        req = urllib.request.Request(url, headers=API_HEADERS)

        with urllib.request.urlopen(req, timeout=4) as resp:
            data = json.loads(resp.read().decode())

        results = []

        for item in data[:limit]:
            vid = item.get("videoId")

            if vid:
                results.append(
                    {
                        "type": "video",
                        "id": vid,
                        "name": item.get("title", "Sem titulo"),
                        "artist": item.get("author") or "YouTube",
                    }
                )

        return results

    except Exception:
        return []


def search_via_invidious(
    query: str,
    limit: int = 5,
) -> List[dict]:
    with ThreadPoolExecutor(
        max_workers=len(INVIDIOUS_INSTANCES)
    ) as pool:
        futures = [
            pool.submit(
                _invidious_search_one,
                instance,
                query,
                limit,
            )
            for instance in INVIDIOUS_INSTANCES
        ]

        for future in as_completed(futures):
            try:
                result = future.result()
                if result:
                    return result
            except Exception:
                pass

    return []


def fetch_via_invidious(video_id: str):
    for instance in INVIDIOUS_INSTANCES:
        try:
            url = f"{instance}/api/v1/videos/{video_id}"
            req = urllib.request.Request(url, headers=API_HEADERS)

            with urllib.request.urlopen(req, timeout=4) as resp:
                data = json.loads(resp.read().decode())

            formats = (
                data.get("adaptiveFormats", [])
                + data.get("formatStreams", [])
            )

            audio = [
                item
                for item in formats
                if item.get("url")
                and "audio" in (item.get("type") or "")
            ]

            if audio:
                audio.sort(
                    key=lambda item: item.get("bitrate") or 0,
                    reverse=True,
                )

                best = audio[0]

                return {
                    "url": best.get("url"),
                    "headers": {},
                    "source": "invidious",
                }

        except Exception:
            continue

    return None


def pick_best_audio_source(info: dict):
    formats = info.get("formats", []) if info else []

    def has_audio(fmt):
        return (
            fmt.get("url")
            and fmt.get("acodec") not in (None, "none")
        )

    audio_only = [
        fmt
        for fmt in formats
        if has_audio(fmt)
        and fmt.get("vcodec") in (None, "none")
    ]

    if audio_only:
        audio_only.sort(
            key=lambda fmt: fmt.get("abr") or 0,
            reverse=True,
        )
        best = audio_only[0]
    else:
        with_audio = [
            fmt
            for fmt in formats
            if has_audio(fmt)
        ]

        if not with_audio:
            return None

        with_audio.sort(
            key=lambda fmt: fmt.get("abr") or 0,
            reverse=True,
        )
        best = with_audio[0]

    return {
        "url": best.get("url"),
        "headers": (
            best.get("http_headers")
            or info.get("http_headers")
            or {}
        ),
        "source": "yt-dlp",
    }


def fetch_via_ytdl(
    video_id: str,
    *,
    use_cookies: bool,
    player_client: str,
):
    opts = make_ytdl_opts(
        use_cookies=use_cookies,
        player_client=player_client,
        flat=False,
        timeout=15,
    )

    with yt_dlp.YoutubeDL(opts) as ydl:
        info = ydl.extract_info(
            f"https://www.youtube.com/watch?v={video_id}",
            download=False,
        )

    return pick_best_audio_source(info)


async def get_direct_audio_source(video_id: str):
    """
    Ordem:
      1) mweb + PO Token, sem cookies
      2) mweb + PO Token, com cookies (se houver)
      3) web_safari sem cookies
      4) Piped/Invidious

    O passo 1 evita depender da conta para videos publicos.
    """
    attempts = [
        ("mweb_pot", False, "mweb"),
    ]

    if HAS_COOKIES:
        attempts.append(
            ("mweb_pot_cookies", True, "mweb")
        )

    # web_embedded atualmente nao depende de GVS PO Token
    # e pode salvar videos em que o mweb nao entrega formatos uteis.
    attempts.append(
        ("web_embedded", False, "web_embedded")
    )

    attempts.append(
        ("web_safari", False, "web_safari")
    )

    last_error: Optional[Exception] = None

    for label, use_cookies, client in attempts:
        try:
            source = await asyncio.wait_for(
                asyncio.to_thread(
                    fetch_via_ytdl,
                    video_id,
                    use_cookies=use_cookies,
                    player_client=client,
                ),
                timeout=15,
            )

            if source and source.get("url"):
                print(
                    f">>> [AUDIO] yt-dlp {label} OK "
                    f"para {video_id}"
                )
                return source

        except Exception as exc:
            last_error = exc

            if is_bot_check_error(exc):
                print(
                    f">>> [YOUTUBE] bloqueio/PO token em "
                    f"{label} para {video_id}"
                )
            else:
                print(
                    f">>> [AUDIO] yt-dlp {label} falhou "
                    f"para {video_id}: {exc}"
                )

            # Evita martelar o YouTube em sequencia.
            await asyncio.sleep(0.75)

    tasks = [
        asyncio.create_task(
            asyncio.to_thread(fetch_via_piped, video_id)
        ),
        asyncio.create_task(
            asyncio.to_thread(fetch_via_invidious, video_id)
        ),
    ]

    try:
        for coro in asyncio.as_completed(
            tasks,
            timeout=7,
        ):
            try:
                result = await coro

                if result and result.get("url"):
                    print(
                        f">>> [AUDIO] fallback "
                        f"{result.get('source')} OK "
                        f"para {video_id}"
                    )
                    return result

            except Exception:
                continue

    except asyncio.TimeoutError:
        pass

    finally:
        for task in tasks:
            if not task.done():
                task.cancel()

    if last_error and is_bot_check_error(last_error):
        print(
            f">>> [ERRO] YouTube recusou todas as "
            f"tentativas para {video_id}"
        )
    else:
        print(
            f">>> [ERRO] Nenhuma fonte de audio "
            f"funcionou para {video_id}"
        )

    return None


def build_search_results(
    info: dict,
    is_url: bool,
    original_search: str,
) -> List[dict]:
    results: List[dict] = [{"status": "ok"}]

    if not info:
        return results

    if info.get("_type") == "playlist" or "entries" in info:
        entries = list(info.get("entries", []) or [])

        if is_url and (
            "list=" in original_search
            or "playlist" in original_search
        ):
            playlist_items = [
                {
                    "id": entry.get("id"),
                    "name": entry.get(
                        "title",
                        "Sem titulo",
                    ),
                    "artist": (
                        entry.get("uploader")
                        or entry.get("channel")
                        or "YouTube"
                    ),
                }
                for entry in entries
                if entry and entry.get("id")
            ]

            results.append(
                {
                    "type": "playlist",
                    "id": info.get("id", ""),
                    "name": info.get(
                        "title",
                        "Playlist",
                    ),
                    "artist": (
                        info.get("uploader")
                        or "YouTube"
                    ),
                    "playlist_items": playlist_items,
                }
            )
        else:
            for entry in entries:
                if entry and entry.get("id"):
                    results.append(
                        {
                            "type": "video",
                            "id": entry.get("id"),
                            "name": entry.get(
                                "title",
                                "Sem titulo",
                            ),
                            "artist": (
                                entry.get("uploader")
                                or entry.get("channel")
                                or "Desconhecido"
                            ),
                        }
                    )
    else:
        results.append(
            {
                "type": "video",
                "id": info.get("id"),
                "name": info.get(
                    "title",
                    "Sem titulo",
                ),
                "artist": (
                    info.get("uploader")
                    or info.get("channel")
                    or "Desconhecido"
                ),
            }
        )

    return results


async def search_youtube(search: str):
    is_url = (
        search.startswith("http://")
        or search.startswith("https://")
    )

    target = (
        search
        if is_url
        else f"ytsearch5:{search}"
    )

    try:
        info = await asyncio.wait_for(
            asyncio.to_thread(
                lambda: yt_dlp.YoutubeDL(
                    SEARCH_YTDL_OPTS
                ).extract_info(
                    target,
                    download=False,
                )
            ),
            timeout=10,
        )

        if info:
            return build_search_results(
                info,
                is_url,
                search,
            )

    except Exception as exc:
        print(
            f">>> [BUSCA] yt-dlp falhou para "
            f"'{search}': {exc}"
        )

    if not is_url:
        piped_results = await asyncio.to_thread(
            search_via_piped,
            search,
        )

        if piped_results:
            return [{"status": "ok"}] + piped_results

        invidious_results = await asyncio.to_thread(
            search_via_invidious,
            search,
        )

        if invidious_results:
            return [{"status": "ok"}] + invidious_results

        return JSONResponse(
            status_code=504,
            content=[
                {"status": "error"},
                {
                    "message": (
                        "Nao foi possivel buscar "
                        "(yt-dlp, Piped e Invidious falharam)"
                    )
                },
            ],
        )

    return JSONResponse(
        status_code=502,
        content=[
            {"status": "error"},
            {
                "message": (
                    "Nao foi possivel processar "
                    "essa URL/playlist"
                )
            },
        ],
    )


@app.api_route("/", methods=["GET", "HEAD"])
async def handle_request(
    request: Request,
    search: str = None,
    id: str = None,
    v: str = "2.1",
):
    if request.method == "HEAD" or (
        not search and not id
    ):
        return JSONResponse(
            content={
                "status": "online",
                "version": v,
                "backend": "4.3-pot",
            }
        )

    if search:
        try:
            return await search_youtube(search)

        except Exception as exc:
            print(f">>> [ERRO NA BUSCA] {exc}")

            return JSONResponse(
                status_code=500,
                content=[
                    {"status": "error"},
                    {"message": str(exc)},
                ],
            )

    if id:
        if not YOUTUBE_ID_REGEX.match(id):
            return JSONResponse(
                status_code=400,
                content={
                    "status": "error",
                    "message": "ID de video invalido",
                },
            )

        now = time.time()
        audio_source = None

        cached = URL_CACHE.get(id)

        if cached and (
            now - cached["timestamp"]
        ) < CACHE_TTL:
            audio_source = cached["source"]
        else:
            audio_source = await get_direct_audio_source(id)

            if audio_source and audio_source.get("url"):
                URL_CACHE[id] = {
                    "source": audio_source,
                    "timestamp": now,
                }

        if (
            not audio_source
            or not audio_source.get("url")
        ):
            return JSONResponse(
                status_code=502,
                content={
                    "status": "error",
                    "message": (
                        "Nao foi possivel obter audio "
                        f"valido para {id}"
                    ),
                },
            )

        async def stream_bytes():
            proc = None

            try:
                ffmpeg_cmd = [
                    "ffmpeg",
                    "-hide_banner",
                    "-loglevel",
                    "error",
                    "-reconnect",
                    "1",
                    "-reconnect_streamed",
                    "1",
                    "-reconnect_at_eof",
                    "1",
                    "-reconnect_on_network_error",
                    "1",
                    "-reconnect_delay_max",
                    "10",
                    "-rw_timeout",
                    "15000000",
                ]

                input_headers = (
                    audio_source.get("headers")
                    or {}
                )

                if input_headers:
                    header_lines = [
                        f"{key}: {value}"
                        for key, value
                        in input_headers.items()
                        if value is not None
                    ]

                    if header_lines:
                        ffmpeg_cmd += [
                            "-headers",
                            "\r\n".join(
                                header_lines
                            ) + "\r\n",
                        ]

                ffmpeg_cmd += [
                    "-i",
                    audio_source["url"],
                    "-map",
                    "0:a:0?",
                    "-vn",
                    "-sn",
                    "-dn",
                    "-f",
                    "dfpwm",
                    "-ar",
                    "48000",
                    "-ac",
                    "1",
                    "pipe:1",
                ]

                proc = subprocess.Popen(
                    ffmpeg_cmd,
                    stdout=subprocess.PIPE,
                    stderr=subprocess.DEVNULL,
                )

                while True:
                    chunk = await asyncio.to_thread(
                        proc.stdout.read,
                        4096,
                    )

                    if not chunk:
                        if proc.poll() is not None:
                            print(
                                f">>> [FFMPEG] codigo "
                                f"{proc.returncode} para {id}"
                            )
                        break

                    yield chunk

            except asyncio.CancelledError:
                pass

            except Exception as exc:
                print(
                    f">>> [STREAM] erro para {id}: {exc}"
                )

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
            headers={
                "Cache-Control": "no-cache",
            },
        )

    return JSONResponse(
        content={
            "status": "error",
            "message": "Parametros invalidos",
        }
    )


if __name__ == "__main__":
    import uvicorn

    port = int(
        os.environ.get("PORT", 10000)
    )

    uvicorn.run(
        app,
        host="0.0.0.0",
        port=port,
    )
