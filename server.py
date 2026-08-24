import os
import subprocess
from fastapi import FastAPI
from fastapi.responses import JSONResponse, StreamingResponse
import yt_dlp

app = FastAPI()

@app.get("/")
def handle_request(search: str = None, id: str = None, v: str = "2.1"):
    if search:
        ydl_opts = {
            'extract_flat': 'in_playlist',
            'quiet': True,
            'no_warnings': True,
        }
        results = [{"status": "ok"}]
        
        with yt_dlp.YoutubeDL(ydl_opts) as ydl:
            target = search if search.startswith("http") else f"ytsearch5:{search}"
            info = ydl.extract_info(target, download=False)
            
            if 'entries' in info:
                if info.get('_type') == 'playlist' and ('list=' in search or 'playlist' in search):
                    playlist_items = []
                    for entry in info['entries']:
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
                    for entry in info['entries']:
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

    elif id:
        ydl_opts = {'format': 'bestaudio/best', 'quiet': True}
        with yt_dlp.YoutubeDL(ydl_opts) as ydl:
            info = ydl.extract_info(f"https://www.youtube.com/watch?v={id}", download=False)
            audio_url = info['url']

        ffmpeg_cmd = [
            'ffmpeg', '-i', audio_url,
            '-f', 'dfpwm', '-ar', '48000', '-ac', '1', 'pipe:1'
        ]
        proc = subprocess.Popen(ffmpeg_cmd, stdout=subprocess.PIPE, stderr=subprocess.DEVNULL)
        
        def stream_bytes():
            while True:
                chunk = proc.stdout.read(4096)
                if not chunk:
                    break
                yield chunk
            proc.stdout.close()

        return StreamingResponse(stream_bytes(), media_type="application/octet-stream")

    return JSONResponse(content={"status": "error", "message": "Parâmetros inválidos"})

if __name__ == "__main__":
    import uvicorn
    port = int(os.environ.get("PORT", 10000))
    uvicorn.run(app, host="0.0.0.0", port=port)