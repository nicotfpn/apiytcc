if search:
        try:
            is_url = search.startswith("http://") or search.startswith("https://")
            target = search if is_url else f"ytsearch5:{search}"
            
            opts = {**COMMON_YTDL_OPTS, 'extract_flat': True, 'skip_download': True}
            
            # Adicionado asyncio.wait_for para evitar travamento infinito se o YouTube bloquear a busca
            try:
                info = await asyncio.wait_for(
                    asyncio.to_thread(lambda: yt_dlp.YoutubeDL(opts).extract_info(target, download=False)),
                    timeout=8
                )
            except asyncio.TimeoutError:
                print(f">>> [TIMEOUT] A busca por '{search}' demorou muito para responder.")
                return JSONResponse(status_code=504, content=[{"status": "error"}, {"message": "Tempo limite de busca excedido"}])

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