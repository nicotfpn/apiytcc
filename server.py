import os
import time
import json
import re
import asyncio
import subprocess
import sys
import tempfile
from pathlib import Path
import urllib.request
import urllib.parse
from concurrent.futures import ThreadPoolExecutor, as_completed
from typing import Dict, Any, List, Optional

from fastapi import FastAPI, Request
from fastapi.responses import JSONResponse, StreamingResponse
import yt_dlp


app = FastAPI(title="iPod CC API", version="4.8")

CLIENT_CACHE: Dict[str, Dict[str, Any]] = {}
CLIENT_CACHE_TTL = 1800

# Cache local do audio JA CONVERTIDO em DFPWM.
# Isso evita bater novamente no YouTube quando o mesmo video e pedido
# outra vez durante a vida do container da Railway.
DFPWM_CACHE_DIR = Path(
    os.environ.get(
        "DFPWM_CACHE_DIR",
        "/tmp/apiytcc_dfpwm_cache",
    )
)
DFPWM_CACHE_DIR.mkdir(parents=True, exist_ok=True)

DFPWM_CACHE_TTL = int(
    os.environ.get(
        "DFPWM_CACHE_TTL",
        str(12 * 60 * 60),
    )
)

DFPWM_CACHE_MAX_FILES = int(
    os.environ.get(
        "DFPWM_CACHE_MAX_FILES",
        "40",
    )
)

DFPWM_CACHE_MIN_BYTES = 4096


def dfpwm_cache_path(video_id: str) -> Path:
    return DFPWM_CACHE_DIR / f"{video_id}.dfpwm"


def dfpwm_part_path(video_id: str) -> Path:
    return DFPWM_CACHE_DIR / f"{video_id}.dfpwm.part"


def valid_dfpwm_cache(video_id: str) -> Optional[Path]:
    path = dfpwm_cache_path(video_id)

    try:
        if not path.is_file():
            return None

        stat = path.stat()

        if stat.st_size < DFPWM_CACHE_MIN_BYTES:
            path.unlink(missing_ok=True)
            return None

        if time.time() - stat.st_mtime > DFPWM_CACHE_TTL:
            path.unlink(missing_ok=True)
            return None

        return path

    except Exception:
        return None


def prune_dfpwm_cache() -> None:
    try:
        files = [
            path
            for path in DFPWM_CACHE_DIR.glob("*.dfpwm")
            if path.is_file()
        ]

        now = time.time()

        for path in files:
            try:
                if now - path.stat().st_mtime > DFPWM_CACHE_TTL:
                    path.unlink(missing_ok=True)
            except Exception:
                pass

        files = [
            path
            for path in DFPWM_CACHE_DIR.glob("*.dfpwm")
            if path.is_file()
        ]

        files.sort(
            key=lambda path: path.stat().st_mtime,
            reverse=True,
        )

        for path in files[DFPWM_CACHE_MAX_FILES:]:
            try:
                path.unlink(missing_ok=True)
            except Exception:
                pass

    except Exception:
        pass


async def stream_cached_dfpwm(path: Path):
    handle = None

    try:
        handle = open(path, "rb")

        while True:
            chunk = await asyncio.to_thread(
                handle.read,
                4096,
            )

            if not chunk:
                break

            yield chunk

    finally:
        if handle:
            handle.close()


prune_dfpwm_cache()

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


YT_PROXY = os.environ.get("YT_PROXY", "").strip()

if YT_PROXY:
    print(">>> YT_PROXY ativo.")
else:
    print(">>> YT_PROXY nao configurado; usando egress direto.")


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
    import yt_dlp_ejs  # noqa: F401
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
    opts: Dict[str, Any] = {
        "quiet": True,
        "no_warnings": True,
        "socket_timeout": timeout,
        "skip_download": True,
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
        # Aceita audio-only ou muxed com audio.
        opts["format"] = "bestaudio*/best*"

    if use_cookies and HAS_COOKIES:
        opts["cookiefile"] = COOKIE_FILE

    if YT_PROXY:
        opts["proxy"] = YT_PROXY

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

if HAS_COOKIES:
    SEARCH_YTDL_OPTS["cookiefile"] = COOKIE_FILE

if YT_PROXY:
    SEARCH_YTDL_OPTS["proxy"] = YT_PROXY


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
                    "label": "piped",
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
                    "label": "invidious",
                }

        except Exception:
            continue

    return None


def pick_best_audio_source(
    info: dict,
    *,
    prefer_hls: bool = False,
):
    formats = info.get("formats", []) if info else []

    def has_audio(fmt):
        return (
            fmt.get("url")
            and fmt.get("acodec") not in (None, "none")
        )

    def protocol(fmt):
        return str(fmt.get("protocol") or "").casefold()

    audio_only = [
        fmt
        for fmt in formats
        if has_audio(fmt)
        and fmt.get("vcodec") in (None, "none")
    ]

    with_audio = [
        fmt
        for fmt in formats
        if has_audio(fmt)
    ]

    if prefer_hls:
        preferred = [
            fmt
            for fmt in audio_only
            if protocol(fmt).startswith("m3u8")
        ]

        if not preferred:
            preferred = [
                fmt
                for fmt in with_audio
                if protocol(fmt).startswith("m3u8")
            ]
    else:
        preferred = [
            fmt
            for fmt in audio_only
            if protocol(fmt) in ("http", "https")
        ]

        if not preferred:
            preferred = audio_only

    if not preferred:
        preferred = with_audio

    if not preferred:
        return None

    preferred.sort(
        key=lambda fmt: (
            fmt.get("abr") or 0,
            fmt.get("asr") or 0,
        ),
        reverse=True,
    )

    best = preferred[0]

    return {
        "url": best.get("url"),
        "headers": (
            best.get("http_headers")
            or info.get("http_headers")
            or {}
        ),
        "source": "yt-dlp",
        "format_id": best.get("format_id"),
        "protocol": best.get("protocol"),
        "ext": best.get("ext"),
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

    source = pick_best_audio_source(
        info,
        prefer_hls=(player_client == "web_safari"),
    )

    if source:
        source["client"] = player_client

    return source



def stream_attempts() -> List[Dict[str, Any]]:
    """
    Ordem conservadora.

    web_embedded:
        Nao precisa de GVS PO Token, quando o video permite embed.

    mweb:
        Usa o BgUtil PO Token Provider que ja esta rodando no container.

    android_vr:
        Cliente sem GVS PO Token para a maioria dos videos comuns.

    web_safari:
        Preferimos HLS, que atualmente e a parte mais util desse cliente.
    """
    attempts: List[Dict[str, Any]] = [
        {
            "label": "web_embedded",
            "client": "web_embedded",
            "cookies": False,
            "format": "bestaudio*/best*",
        },
        {
            "label": "mweb_pot",
            "client": "mweb",
            "cookies": False,
            "format": "bestaudio*/best*",
        },
        {
            "label": "android_vr",
            "client": "android_vr",
            "cookies": False,
            "format": "bestaudio*/best*",
        },
        {
            "label": "web_safari_hls",
            "client": "web_safari",
            "cookies": False,
            "format": (
                "bestaudio[protocol^=m3u8]/"
                "best[protocol^=m3u8]/"
                "bestaudio*/best*"
            ),
        },
    ]

    if HAS_COOKIES:
        attempts.extend(
            [
                {
                    "label": "mweb_pot_cookies",
                    "client": "mweb",
                    "cookies": True,
                    "format": "bestaudio*/best*",
                },
                {
                    "label": "web_safari_hls_cookies",
                    "client": "web_safari",
                    "cookies": True,
                    "format": (
                        "bestaudio[protocol^=m3u8]/"
                        "best[protocol^=m3u8]/"
                        "bestaudio*/best*"
                    ),
                },
            ]
        )

    return attempts


def ordered_stream_attempts(video_id: str) -> List[Dict[str, Any]]:
    attempts = stream_attempts()

    cached = CLIENT_CACHE.get(video_id)

    if (
        cached
        and time.time() - cached.get("timestamp", 0) < CLIENT_CACHE_TTL
    ):
        cached_label = cached.get("label")

        attempts.sort(
            key=lambda item: 0 if item["label"] == cached_label else 1
        )

    return attempts


def build_ytdlp_pipe_command(
    video_id: str,
    attempt: Dict[str, Any],
) -> List[str]:
    """
    IMPORTANTE:
    yt-dlp faz a requisicao real do media e escreve os bytes em stdout.

    Nao entregamos mais uma URL GoogleVideo para o FFmpeg abrir sozinho.
    Assim retries, fragments, cookies, PO Token e challenge handling
    continuam sob controle do proprio yt-dlp.
    """
    cmd = [
        sys.executable,
        "-m",
        "yt_dlp",
        "--quiet",
        "--no-warnings",
        "--no-progress",
        "--no-playlist",
        "--js-runtimes",
        "node",
        "--extractor-args",
        f"youtube:player_client={attempt['client']}",
        "--format",
        attempt["format"],
        "--output",
        "-",
        "--retries",
        "6",
        "--fragment-retries",
        "6",
        "--extractor-retries",
        "3",
        "--file-access-retries",
        "3",
        "--retry-sleep",
        "1",
        "--socket-timeout",
        "15",
    ]

    if YT_PROXY:
        cmd += [
            "--proxy",
            YT_PROXY,
        ]

    if attempt.get("cookies") and HAS_COOKIES:
        cmd += [
            "--cookies",
            COOKIE_FILE,
        ]

    cmd.append(
        f"https://www.youtube.com/watch?v={video_id}"
    )

    return cmd


def build_ffmpeg_pipe_command() -> List[str]:
    return [
        "ffmpeg",
        "-hide_banner",
        "-loglevel",
        "error",
        "-i",
        "pipe:0",
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


def read_error_tail(handle, limit: int = 1200) -> str:
    try:
        handle.flush()
        handle.seek(0, 2)
        size = handle.tell()
        handle.seek(max(0, size - limit))
        raw = handle.read()

        if isinstance(raw, bytes):
            raw = raw.decode("utf-8", errors="replace")

        lines = [
            line.strip()
            for line in str(raw).splitlines()
            if line.strip()
        ]

        if not lines:
            return ""

        return lines[-1][:300]

    except Exception:
        return ""


def stop_pipeline(pipeline: Optional[Dict[str, Any]]) -> None:
    if not pipeline:
        return

    ffmpeg = pipeline.get("ffmpeg")
    ytdlp = pipeline.get("ytdlp")

    for proc in (ffmpeg, ytdlp):
        if proc and proc.poll() is None:
            try:
                proc.terminate()
            except Exception:
                pass

    for proc in (ffmpeg, ytdlp):
        if proc and proc.poll() is None:
            try:
                proc.wait(timeout=1)
            except Exception:
                try:
                    proc.kill()
                except Exception:
                    pass

    if ffmpeg and ffmpeg.stdout:
        try:
            ffmpeg.stdout.close()
        except Exception:
            pass

    for key in ("ytdlp_err", "ffmpeg_err"):
        handle = pipeline.get(key)
        if handle:
            try:
                handle.close()
            except Exception:
                pass


def start_ytdlp_ffmpeg_pipeline(
    video_id: str,
    attempt: Dict[str, Any],
) -> Dict[str, Any]:
    ytdlp_err = tempfile.TemporaryFile()
    ffmpeg_err = tempfile.TemporaryFile()

    ytdlp = subprocess.Popen(
        build_ytdlp_pipe_command(video_id, attempt),
        stdout=subprocess.PIPE,
        stderr=ytdlp_err,
        bufsize=0,
    )

    if ytdlp.stdout is None:
        ytdlp.terminate()
        ytdlp_err.close()
        ffmpeg_err.close()
        raise RuntimeError("yt-dlp sem stdout")

    ffmpeg = subprocess.Popen(
        build_ffmpeg_pipe_command(),
        stdin=ytdlp.stdout,
        stdout=subprocess.PIPE,
        stderr=ffmpeg_err,
        bufsize=0,
    )

    # O FFmpeg agora possui o descritor de leitura.
    # Fechar a copia do processo pai e importante para EOF/SIGPIPE.
    ytdlp.stdout.close()

    return {
        "label": attempt["label"],
        "attempt": attempt,
        "ytdlp": ytdlp,
        "ffmpeg": ffmpeg,
        "ytdlp_err": ytdlp_err,
        "ffmpeg_err": ffmpeg_err,
        "first_chunk": b"",
    }


async def read_pipeline_chunk(
    pipeline: Dict[str, Any],
    size: int,
    timeout: float,
) -> bytes:
    ffmpeg = pipeline["ffmpeg"]

    if ffmpeg.stdout is None:
        return b""

    reader = getattr(ffmpeg.stdout, "read1", None)

    if reader is None:
        reader = ffmpeg.stdout.read

    try:
        return await asyncio.wait_for(
            asyncio.to_thread(reader, size),
            timeout=timeout,
        )

    except asyncio.TimeoutError:
        return b""


async def open_stream_pipeline(video_id: str):
    """
    So devolve sucesso depois que DFPWM real saiu do FFmpeg.

    Isso significa que o HTTP 200 nao e enviado apenas porque o yt-dlp
    encontrou uma URL. Primeiro precisamos receber bytes reproduziveis.
    """
    attempts = ordered_stream_attempts(video_id)

    # Duas passadas. A segunda gera uma extracao/token/sessao nova.
    # Isso cobre falhas intermitentes sem ficar em loop infinito.
    for round_number in (1, 2):
        for attempt in attempts:
            pipeline = None

            try:
                pipeline = await asyncio.to_thread(
                    start_ytdlp_ffmpeg_pipeline,
                    video_id,
                    attempt,
                )

                first_chunk = await read_pipeline_chunk(
                    pipeline,
                    size=2048,
                    timeout=22,
                )

                if first_chunk:
                    pipeline["first_chunk"] = first_chunk

                    CLIENT_CACHE[video_id] = {
                        "label": attempt["label"],
                        "timestamp": time.time(),
                    }

                    print(
                        f">>> [AUDIO] {attempt['label']} OK "
                        f"para {video_id}"
                    )

                    return pipeline

                ytdlp_reason = read_error_tail(
                    pipeline["ytdlp_err"]
                )
                ffmpeg_reason = read_error_tail(
                    pipeline["ffmpeg_err"]
                )

                reason = ytdlp_reason or ffmpeg_reason

                if reason:
                    print(
                        f">>> [AUDIO] {attempt['label']} falhou: "
                        f"{reason}"
                    )
                else:
                    print(
                        f">>> [AUDIO] {attempt['label']} falhou "
                        f"antes de gerar audio"
                    )

            except Exception as exc:
                print(
                    f">>> [AUDIO] {attempt['label']} falhou: "
                    f"{str(exc)[:300]}"
                )

            finally:
                if pipeline and not pipeline.get("first_chunk"):
                    stop_pipeline(pipeline)

        if round_number == 1:
            print(
                f">>> [AUDIO] nova tentativa com extracao fresca "
                f"para {video_id}"
            )
            await asyncio.sleep(1.0)

    # Ultima chance: Piped/Invidious antigos.
    fallback_sources = await asyncio.gather(
        asyncio.to_thread(fetch_via_piped, video_id),
        asyncio.to_thread(fetch_via_invidious, video_id),
        return_exceptions=True,
    )

    for source in fallback_sources:
        if isinstance(source, Exception):
            continue

        if not source or not source.get("url"):
            continue

        direct_err = tempfile.TemporaryFile()

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
        ]

        headers = source.get("headers") or {}

        if headers:
            header_lines = [
                f"{key}: {value}"
                for key, value in headers.items()
                if value is not None
            ]

            if header_lines:
                ffmpeg_cmd += [
                    "-headers",
                    "\r\n".join(header_lines) + "\r\n",
                ]

        ffmpeg_cmd += [
            "-i",
            source["url"],
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

        ffmpeg = subprocess.Popen(
            ffmpeg_cmd,
            stdout=subprocess.PIPE,
            stderr=direct_err,
            bufsize=0,
        )

        pipeline = {
            "label": source.get("source") or "fallback",
            "attempt": None,
            "ytdlp": None,
            "ffmpeg": ffmpeg,
            "ytdlp_err": None,
            "ffmpeg_err": direct_err,
            "first_chunk": b"",
        }

        first_chunk = await read_pipeline_chunk(
            pipeline,
            size=2048,
            timeout=12,
        )

        if first_chunk:
            pipeline["first_chunk"] = first_chunk

            print(
                f">>> [AUDIO] fallback "
                f"{pipeline['label']} OK para {video_id}"
            )

            return pipeline

        stop_pipeline(pipeline)

    print(
        f">>> [ERRO] Nenhuma fonte conseguiu gerar audio "
        f"para {video_id}"
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
                "backend": "4.8-warp-free-pipe-cache",
                "proxy": "warp" if YT_PROXY == "http://127.0.0.1:25345" else ("external" if YT_PROXY else "direct"),
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

        # Primeiro tenta o DFPWM local.
        # Se essa musica ja tocou ate o fim neste container, nao toca
        # no YouTube, yt-dlp, PO Token ou FFmpeg novamente.
        cached_audio = valid_dfpwm_cache(id)

        if cached_audio:
            print(
                f">>> [CACHE] DFPWM HIT para {id}"
            )

            return StreamingResponse(
                stream_cached_dfpwm(cached_audio),
                media_type="application/octet-stream",
                headers={
                    "Cache-Control": "no-cache",
                },
            )

        # O pipeline e aberto ANTES do StreamingResponse.
        # Portanto o cliente so recebe HTTP 200 depois que DFPWM real
        # saiu do FFmpeg.
        pipeline = await open_stream_pipeline(id)

        if not pipeline:
            return JSONResponse(
                status_code=502,
                content={
                    "status": "error",
                    "message": (
                        "Nao foi possivel gerar audio "
                        f"para {id}"
                    ),
                },
            )

        async def stream_bytes():
            cache_handle = None
            cache_part = dfpwm_part_path(id)
            cache_final = dfpwm_cache_path(id)
            normal_eof = False

            try:
                # Nunca reaproveita um .part antigo.
                cache_part.unlink(missing_ok=True)

                cache_handle = open(
                    cache_part,
                    "wb",
                )

                first_chunk = pipeline.get("first_chunk")

                if first_chunk:
                    cache_handle.write(first_chunk)
                    yield first_chunk
                    pipeline["first_chunk"] = b""

                while True:
                    chunk = await read_pipeline_chunk(
                        pipeline,
                        size=4096,
                        timeout=45,
                    )

                    if not chunk:
                        normal_eof = True
                        break

                    cache_handle.write(chunk)
                    yield chunk

            except asyncio.CancelledError:
                # Cliente saiu antes do fim. Nao salva cache parcial.
                pass

            except Exception as exc:
                print(
                    f">>> [STREAM] erro para {id}: "
                    f"{str(exc)[:300]}"
                )

            finally:
                if cache_handle:
                    try:
                        cache_handle.flush()
                    except Exception:
                        pass

                    try:
                        cache_handle.close()
                    except Exception:
                        pass

                ffmpeg = pipeline.get("ffmpeg")
                ytdlp = pipeline.get("ytdlp")
                label = pipeline.get("label")

                # Aguarda rapidamente os processos terminarem para saber
                # se o arquivo recebido esta realmente completo.
                if normal_eof:
                    for proc in (ffmpeg, ytdlp):
                        if proc and proc.poll() is None:
                            try:
                                await asyncio.to_thread(
                                    proc.wait,
                                    2,
                                )
                            except Exception:
                                pass

                ffmpeg_code = (
                    ffmpeg.poll()
                    if ffmpeg
                    else 0
                )

                ytdlp_code = (
                    ytdlp.poll()
                    if ytdlp
                    else 0
                )

                completed_ok = (
                    normal_eof
                    and ffmpeg_code == 0
                    and ytdlp_code == 0
                )

                stop_pipeline(pipeline)

                try:
                    size = (
                        cache_part.stat().st_size
                        if cache_part.exists()
                        else 0
                    )
                except Exception:
                    size = 0

                if (
                    completed_ok
                    and size >= DFPWM_CACHE_MIN_BYTES
                ):
                    try:
                        os.replace(
                            cache_part,
                            cache_final,
                        )
                        prune_dfpwm_cache()

                        print(
                            f">>> [CACHE] DFPWM salvo para {id}"
                        )
                    except Exception:
                        cache_part.unlink(
                            missing_ok=True
                        )
                else:
                    cache_part.unlink(
                        missing_ok=True
                    )

                if completed_ok:
                    print(
                        f">>> [STREAM] finalizado "
                        f"({label}) para {id}"
                    )
                else:
                    print(
                        f">>> [STREAM] encerrado "
                        f"({label}) para {id}"
                    )

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
