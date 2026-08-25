-- startup
sleep(2)

local api_base_url = "https://affair-cartoon-immerse.ngrok-free.dev"
local version = "2.1"

local width, height = term.getSize()
local tab = 1

local waiting_for_input = false
local last_search = nil
local last_search_url = nil
local search_results = nil
local search_error = false
local in_search_result = false
local clicked_result = nil

-- ============================================================
-- UI AESTHETIC
-- ============================================================
local UI = {
    bg = colors.black,
    panel = colors.gray,
    panel2 = colors.lightGray,
    text = colors.white,
    dim = colors.lightGray,
    muted = colors.gray,
    accent = colors.lime,
    accent2 = colors.green,
    warning = colors.yellow,
    error = colors.red,
}

local uiBounds = {
    tabs = {},
    play = nil,
    skip = nil,
    loop = nil,
    volume = nil,
    search = nil,
    results = {},
    detail = {},
}

local function clipText(s, max)
    s = tostring(s or "")
    if max <= 0 then return "" end
    if #s <= max then return s end
    if max <= 3 then return string.sub(s, 1, max) end
    return string.sub(s, 1, max - 3) .. "..."
end

local function centerX(text, x1, x2)
    return x1 + math.floor(((x2 - x1 + 1) - #text) / 2)
end

local function uiWrite(x, y, text, fg, bg)
    term.setCursorPos(x, y)
    term.setTextColor(fg or UI.text)
    term.setBackgroundColor(bg or UI.bg)
    term.write(text)
end

local function uiCenter(y, text, fg, bg, x1, x2)
    x1 = x1 or 1
    x2 = x2 or width
    uiWrite(centerX(text, x1, x2), y, text, fg, bg)
end

local function uiFill(x1, y1, x2, y2, bg)
    if x2 < x1 or y2 < y1 then return end
    term.setBackgroundColor(bg)
    for y = y1, y2 do
        term.setCursorPos(x1, y)
        term.write(string.rep(" ", x2 - x1 + 1))
    end
end

local function uiLine(y, x1, x2, char, fg)
    char = char or "-"
    uiWrite(x1, y, string.rep(char, math.max(0, x2 - x1 + 1)), fg or UI.muted, UI.bg)
end

local function uiButton(x, y, label, active, enabled)
    enabled = enabled ~= false
    local textColor = not enabled and UI.muted or (active and UI.bg or UI.text)
    local bg = not enabled and UI.bg or (active and UI.accent or UI.panel)
    local left = "[" .. label .. "]"
    uiWrite(x, y, left, textColor, bg)
    return {x1=x, y1=y, x2=x+#left-1, y2=y}
end

local function coverPattern(name, x, y, size, base)
    uiFill(x, y, x + size - 1, y + size - 1, base)
    local seed = 0
    name = tostring(name or "")
    for i = 1, #name do seed = (seed + string.byte(name, i) * i) % 997 end

    local alt = (base == colors.green) and colors.lime or colors.green
    local light = colors.white
    local r1 = (seed % (size - 5)) + 3
    local r2 = ((seed * 3) % (size - 6)) + 2

    for row = 1, size do
        if row == r1 or row == r1 + 1 or row == r2 then
            uiFill(x + 2, y + row - 1, x + size - 3, y + row - 1, alt)
        end
    end

    for col = 1, size do
        if col % 4 == (seed % 4) then
            uiWrite(x + col - 1, y + 2, " ", light, light)
            if size >= 9 then
                uiWrite(x + col - 1, y + size - 3, " ", light, light)
            end
        end
    end

    local c = math.floor(size / 2)
    uiFill(x + c - 2, y + c - 1, x + c + 2, y + c + 1, colors.black)
    uiFill(x + c - 1, y + c - 2, x + c + 1, y + c + 2, colors.black)
    uiFill(x + c - 1, y + c - 1, x + c + 1, y + c + 1, base)
end

local playing = false
local queue = {}
local now_playing = nil
local looping = 0
local volume = 1.5

local playing_id = nil
local last_download_url = nil
local playing_status = 0
local is_loading = false
local is_error = false

local state_file = "ipod_state.json"

local function loadStateAndResume()
    if fs.exists(state_file) then
        local f = fs.open(state_file, "r")
        if f then
            local data = f.readAll()
            f.close()
            fs.delete(state_file)
            
            local state = textutils.unserialiseJSON(data)
            if state and type(state) == "table" then
                now_playing = state.now_playing
                queue = state.queue or {}
                looping = state.looping or 0
                volume = state.volume or 1.5
                playing = true
                is_error = false
                playing_id = nil
            end
        end
    end
end

loadStateAndResume()

local player_handle = nil
local pending_chat_searches = {}
local chat_request_counter = 0
local start = nil
local pcm = nil
local size = nil
local decoder = require "cc.audio.dfpwm".make_decoder()
local needs_next_chunk = 0
local buffer

local speakers = { peripheral.find("speaker") }
if #speakers == 0 then
    error("No speakers attached. You need to connect a speaker to this computer.", 0)
end

-- ============================================================
-- MULTI-MONITOR SUPPORT (Wired Modems)
-- ============================================================
local monitors = {}

local function refreshMonitors()
    monitors = {}
    for _, name in ipairs(peripheral.getNames()) do
        if peripheral.getType(name) == "monitor" then
            local m = peripheral.wrap(name)
            if m then 
                m.setTextScale(0.5)
                table.insert(monitors, m) 
            end
        end
    end
end

refreshMonitors()

local chat_box = peripheral.find("chat_box")

local ART_PALETTE = {
    colors.green, colors.lime, colors.cyan, colors.blue, colors.purple,
    colors.pink, colors.orange, colors.red, colors.lightBlue, colors.yellow,
}

local function artColorFor(name)
    if not name then return colors.green end
    local sum = 0
    for i = 1, #name do
        sum = (sum + string.byte(name, i) * i) % 997
    end
    return ART_PALETTE[(sum % #ART_PALETTE) + 1]
end

local function monitorClip(text, max)
    text = tostring(text or "")
    if max <= 0 then return "" end
    if #text <= max then return text end
    if max <= 3 then return text:sub(1, max) end
    return text:sub(1, max - 3) .. "..."
end

function redrawScreen()
    if waiting_for_input then return end
    term.setCursorBlink(false)
    term.setBackgroundColor(UI.bg)
    term.clear()
    uiBounds.tabs = {}
    uiWrite(2, 1, "MUSIC", UI.accent, UI.bg)
    local tabs = {"TOCANDO", "BUSCAR"}
    local gap = 4
    local total = #tabs[1] + #tabs[2] + gap
    local tabStart = math.max(2, width - total - 1)
    for i, label in ipairs(tabs) do
        local x = (i == 1) and tabStart or (tabStart + #tabs[1] + gap)
        local selected = (tab == i)
        uiWrite(x, 1, label, selected and UI.text or UI.muted, UI.bg)
        if selected then
            uiWrite(x, 2, string.rep("-", #label), UI.accent, UI.bg)
        end
        uiBounds.tabs[i] = {x1=x, y1=1, x2=x+#label-1, y2=2}
    end
    uiLine(3, 2, width - 1, "-", UI.panel)
    if tab == 1 then drawNowPlaying() else drawSearch() end
end

function drawNowPlaying()
    local top = 5
    local bottom = height
    uiBounds.play, uiBounds.skip, uiBounds.loop, uiBounds.volume = nil, nil, nil, nil
    if now_playing then
        local coverSize = math.min(9, math.max(7, math.floor((bottom - top) * 0.36)))
        if width < coverSize + 28 then coverSize = math.min(7, math.max(5, width - 24)) end
        local coverX = math.max(2, math.floor((width - coverSize) / 2))
        local coverY = top
        local coverBase = (artColorFor and artColorFor(now_playing.name)) or colors.green
        if coverBase == colors.black or coverBase == colors.gray then coverBase = colors.green end
        coverPattern(now_playing.name, coverX, coverY, coverSize, coverBase)
        local infoY = coverY + coverSize + 2
        local maxTitle = width - 4
        uiCenter(infoY, clipText(now_playing.name, maxTitle), UI.text)
        uiCenter(infoY + 1, clipText(now_playing.artist, maxTitle), UI.dim)
        if is_loading then uiCenter(infoY + 3, "CARREGANDO", UI.warning)
        elseif is_error then uiCenter(infoY + 3, "ERRO DE REDE", UI.error)
        elseif playing then uiCenter(infoY + 3, "TOCANDO", UI.accent)
        else uiCenter(infoY + 3, "PAUSADO", UI.muted) end
    else
        uiCenter(top + 4, "NADA TOCANDO", UI.muted)
    end
    local controlY = math.max(9, bottom - 7)
    local controls = {
        {label = playing and "PAUSAR" or "TOCAR", enabled = (now_playing ~= nil or #queue > 0)},
        {label = "PULAR", enabled = (now_playing ~= nil or #queue > 0)},
        {label = (looping == 0 and "LOOP" or looping == 1 and "FILA" or "MUSICA"), enabled = true},
    }
    local widths = {}
    local total = 0
    for i, c in ipairs(controls) do
        widths[i] = #c.label + 2
        total = total + widths[i]
        if i < #controls then total = total + 2 end
    end
    local x = math.max(2, math.floor((width - total) / 2) + 1)
    uiBounds.play = uiButton(x, controlY, controls[1].label, playing, controls[1].enabled)
    x = x + widths[1] + 2
    uiBounds.skip = uiButton(x, controlY, controls[2].label, false, controls[2].enabled)
    x = x + widths[2] + 2
    uiBounds.loop = uiButton(x, controlY, controls[3].label, looping ~= 0, true)
    local volY = controlY + 2
    local volLabel = "VOLUME"
    local barX = 2
    local barW = math.max(10, width - #volLabel - 9)
    uiWrite(barX, volY, volLabel, UI.muted, UI.bg)
    barX = barX + #volLabel + 2
    local filled = math.floor((volume / 3) * barW + 0.5)
    uiWrite(barX, volY, string.rep("#", filled), UI.accent, UI.bg)
    uiWrite(barX + filled, volY, string.rep("-", barW - filled), UI.panel, UI.bg)
    uiWrite(barX + barW + 1, volY, string.format("%d%%", math.floor(volume / 3 * 100 + 0.5)), UI.text, UI.bg)
    uiBounds.volume = {x1=barX, y1=volY, x2=barX + barW - 1, y2=volY}
    local queueY = volY + 2
    if #queue > 0 and queueY <= height then
        uiWrite(2, queueY, "PROXIMA:", UI.muted, UI.bg)
        local label = clipText(queue[1].name or "", width - 12)
        uiWrite(11, queueY, label, UI.dim, UI.bg)
    end
end

function drawSearch()
    uiBounds.search = nil
    uiBounds.results = {}
    uiBounds.detail = {}
    if in_search_result and search_results and search_results[clicked_result] then
        local track = search_results[clicked_result]
        local title = clipText(track.name, width - 4)
        local artist = clipText(track.artist, width - 4)
        uiCenter(5, "DETALHES", UI.muted)
        uiCenter(7, title, UI.text)
        uiCenter(8, artist, UI.dim)
        local by = 11
        local bx = math.max(2, math.floor((width - 22) / 2) + 1)
        uiBounds.detail.now = uiButton(bx, by, "TOCAR AGORA", true, true)
        uiBounds.detail.next = uiButton(bx, by + 2, "TOCAR DEPOIS", false, true)
        uiBounds.detail.queue = uiButton(bx, by + 4, "ADICIONAR A FILA", false, true)
        uiBounds.detail.cancel = uiButton(bx, by + 7, "VOLTAR", false, true)
        return
    end
    local boxY = 5
    local boxX1 = 2
    local boxX2 = width - 1
    uiFill(boxX1, boxY, boxX2, boxY + 2, UI.panel)
    uiWrite(boxX1 + 2, boxY + 1, clipText(last_search or "TOQUE PARA BUSCAR", width - 7),
        last_search and UI.text or UI.muted, UI.panel)
    uiBounds.search = {x1=boxX1, y1=boxY, x2=boxX2, y2=boxY+2}
    local startY = 9
    if search_results then
        local maxRows = math.floor((height - startY - 1) / 2)
        for i = 1, math.min(#search_results, maxRows) do
            local y = startY + (i - 1) * 2
            local title = clipText(search_results[i].name or "", width - 9)
            local artist = clipText(search_results[i].artist or "", width - 9)
            uiWrite(3, y, title, UI.text, UI.bg)
            uiWrite(3, y + 1, artist, UI.dim, UI.bg)
            uiWrite(2, y + 1, ">", UI.accent, UI.bg)
            uiBounds.results[i] = {x1=2, y1=y, x2=width-1, y2=y+1}
        end
        if #search_results == 0 then uiCenter(startY, "NENHUM RESULTADO", UI.muted) end
    else
        if search_error then uiCenter(startY, "ERRO AO BUSCAR", UI.error)
        elseif last_search_url ~= nil then uiCenter(startY, "BUSCANDO...", UI.muted)
        else uiCenter(startY, "PESQUISE POR MUSICA OU COLE UM LINK", UI.muted) end
    end
end

function uiLoop()
    redrawScreen()
    while true do
        if waiting_for_input then
            parallel.waitForAny(
                function()
                    term.setCursorBlink(true)
                    local input = read()
                    if #input > 0 then
                        last_search = input
                        last_search_url = api_base_url .. "?v=" .. version .. "&search=" .. textutils.urlEncode(input)
                        http.request({url = last_search_url, binary = false, method = "GET"})
                        search_results = nil
                        search_error = false
                    else
                        last_search = nil
                        last_search_url = nil
                        search_results = nil
                        search_error = false
                    end
                    waiting_for_input = false
                    term.setCursorBlink(false)
                    os.queueEvent("redraw_screen")
                end,
                function()
                    while waiting_for_input do
                        local _, button, x, y = os.pullEvent("mouse_click")
                        if not inBounds(uiBounds.search, x, y) then
                            waiting_for_input = false
                            term.setCursorBlink(false)
                            os.queueEvent("redraw_screen")
                            break
                        end
                    end
                end
            )
        else
            parallel.waitForAny(
                function()
                    local _, button, x, y = os.pullEvent("mouse_click")
                    if button ~= 1 then return end
                    if not in_search_result then
                        for i, b in ipairs(uiBounds.tabs) do
                            if inBounds(b, x, y) then
                                tab = i
                                redrawScreen()
                                return
                            end
                        end
                    end
                    if tab == 2 and not in_search_result then
                        if inBounds(uiBounds.search, x, y) then
                            waiting_for_input = true
                            redrawScreen()
                            return
                        end
                        for i, b in ipairs(uiBounds.results) do
                            if inBounds(b, x, y) then
                                in_search_result = true
                                clicked_result = i
                                redrawScreen()
                                return
                            end
                        end
                    elseif tab == 2 and in_search_result then
                        local d = uiBounds.detail
                        if inBounds(d.now, x, y) then
                            in_search_result = false
                            for _, speaker in ipairs(speakers) do speaker.stop() end
                            os.queueEvent("playback_stopped")
                            playing = true
                            is_error = false
                            playing_id = nil
                            if search_results[clicked_result].type == "playlist" then
                                now_playing = search_results[clicked_result].playlist_items[1]
                                queue = {}
                                for i = 2, #search_results[clicked_result].playlist_items do
                                    table.insert(queue, search_results[clicked_result].playlist_items[i])
                                end
                            else
                                now_playing = search_results[clicked_result]
                            end
                            os.queueEvent("audio_update")
                        elseif inBounds(d.next, x, y) then
                            in_search_result = false
                            if search_results[clicked_result].type == "playlist" then
                                for i = #search_results[clicked_result].playlist_items, 1, -1 do
                                    table.insert(queue, 1, search_results[clicked_result].playlist_items[i])
                                end
                            else
                                table.insert(queue, 1, search_results[clicked_result])
                            end
                            os.queueEvent("audio_update")
                        elseif inBounds(d.queue, x, y) then
                            in_search_result = false
                            if search_results[clicked_result].type == "playlist" then
                                for i = 1, #search_results[clicked_result].playlist_items do
                                    table.insert(queue, search_results[clicked_result].playlist_items[i])
                                end
                            else
                                table.insert(queue, search_results[clicked_result])
                            end
                            os.queueEvent("audio_update")
                        elseif inBounds(d.cancel, x, y) then
                            in_search_result = false
                        end
                        redrawScreen()
                    elseif tab == 1 and not in_search_result then
                        if inBounds(uiBounds.play, x, y) then
                            if playing then
                                playing = false
                                for _, speaker in ipairs(speakers) do speaker.stop() end
                                os.queueEvent("playback_stopped")
                                playing_id = nil
                                is_loading = false
                                is_error = false
                                os.queueEvent("audio_update")
                            elseif now_playing ~= nil then
                                playing_id = nil
                                playing = true
                                is_error = false
                                os.queueEvent("audio_update")
                            elseif #queue > 0 then
                                now_playing = queue[1]
                                table.remove(queue, 1)
                                playing_id = nil
                                playing = true
                                is_error = false
                                os.queueEvent("audio_update")
                            end
                        elseif inBounds(uiBounds.skip, x, y) then
                            if now_playing ~= nil or #queue > 0 then
                                is_error = false
                                if playing then
                                    for _, speaker in ipairs(speakers) do speaker.stop() end
                                    os.queueEvent("playback_stopped")
                                end
                                if #queue > 0 then
                                    if looping == 1 then table.insert(queue, now_playing) end
                                    now_playing = queue[1]
                                    table.remove(queue, 1)
                                    playing_id = nil
                                else
                                    now_playing = nil
                                    playing = false
                                    is_loading = false
                                    is_error = false
                                    playing_id = nil
                                end
                                os.queueEvent("audio_update")
                            end
                        elseif inBounds(uiBounds.loop, x, y) then
                            if looping == 0 then looping = 1
                            elseif looping == 1 then looping = 2
                            else looping = 0 end
                        elseif inBounds(uiBounds.volume, x, y) then
                            volume = math.max(0, math.min(3,
                                (x - uiBounds.volume.x1) / (uiBounds.volume.x2 - uiBounds.volume.x1 + 1) * 3))
                        end
                        redrawScreen()
                    end
                end,
                function()
                    local _, button, x, y = os.pullEvent("mouse_drag")
                    if button == 1 and tab == 1 and not in_search_result and inBounds(uiBounds.volume, x, y) then
                        volume = math.max(0, math.min(3,
                            (x - uiBounds.volume.x1) / (uiBounds.volume.x2 - uiBounds.volume.x1 + 1) * 3))
                        redrawScreen()
                    end
                end,
                function()
                    os.pullEvent("redraw_screen")
                    redrawScreen()
                end
            )
        end
    end
end

function audioLoop()
    while true do
        if playing and now_playing then
            local thisnowplayingid = now_playing.id
            if playing_id ~= thisnowplayingid then
                playing_id = thisnowplayingid
                last_download_url = api_base_url .. "?v=" .. version .. "&id=" .. textutils.urlEncode(playing_id)
                playing_status = 0
                needs_next_chunk = 1
                if player_handle then pcall(player_handle.close) player_handle = nil end
                http.request({url = last_download_url, binary = true, method = "GET"})
                is_loading = true
                os.queueEvent("redraw_screen")
                os.queueEvent("audio_update")
            elseif playing_status == 1 and needs_next_chunk == 1 then
                while true do
                    local chunk = player_handle.read(size)
                    if not chunk then
                        if looping == 2 or (looping == 1 and #queue == 0) then
                            playing_id = nil
                        elseif looping == 1 and #queue > 0 then
                            table.insert(queue, now_playing)
                            now_playing = queue[1]
                            table.remove(queue, 1)
                            playing_id = nil
                        else
                            if #queue > 0 then
                                now_playing = queue[1]
                                table.remove(queue, 1)
                                playing_id = nil
                            else
                                now_playing = nil
                                playing = false
                                playing_id = nil
                                is_loading = false
                                is_error = false
                            end
                        end
                        os.queueEvent("redraw_screen")
                        player_handle.close()
                        player_handle = nil
                        needs_next_chunk = 0
                        break
                    else
                        if start then
                            chunk, start = start .. chunk, nil
                            size = size + 4
                        end
                        buffer = decoder(chunk)
                        local fn = {}
                        for i, speaker in ipairs(speakers) do 
                            fn[i] = function()
                                local name = peripheral.getName(speaker)
                                while not speaker.playAudio(buffer, volume) do
                                    parallel.waitForAny(
                                        function() repeat until select(2, os.pullEvent("speaker_audio_empty")) == name end,
                                        function() os.pullEvent("playback_stopped") end
                                    )
                                    if not playing or playing_id ~= thisnowplayingid then return end
                                end
                            end
                        end
                        local ok, err = pcall(parallel.waitForAll, table.unpack(fn))
                        if not ok then
                            needs_next_chunk = 2
                            is_error = true
                            break
                        end
                        if not playing or playing_id ~= thisnowplayingid then break end
                    end
                end
                os.queueEvent("audio_update")
            end
        end
        os.pullEvent("audio_update")
    end
end

function httpLoop()
    while true do
        parallel.waitForAny(
            function()
                local event, url, handle = os.pullEvent("http_success")

                local chat_req = pending_chat_searches[url]
                if chat_req then
                    pending_chat_searches[url] = nil
                    local data = handle.readAll()
                    handle.close()
                    local results = nil
                    if data and data ~= "" then
                        results = textutils.unserialiseJSON(data)
                    end

                    if type(results) ~= "table" or #results == 0 then
                        chatReplyPrivate(chat_req.user, "Nenhum resultado para: " .. chat_req.query)
                    else
                        if results[1] and results[1].status then
                            table.remove(results, 1)
                        end

                        if #results == 0 then
                            chatReplyPrivate(chat_req.user, "Nenhum resultado para: " .. chat_req.query)
                        else
                            local track = results[1]
                            local tracks = {}
                            if track.type == "playlist" then
                                tracks = track.playlist_items or {}
                            else
                                tracks = { track }
                            end

                            if #tracks == 0 or not tracks[1] or not tracks[1].id then
                                chatReplyPrivate(chat_req.user, "A API não retornou uma música válida para: " .. chat_req.query)
                            elseif chat_req.mode == "now" then
                                for _, speaker in ipairs(speakers) do pcall(speaker.stop) end
                                os.queueEvent("playback_stopped")
                                playing = true
                                is_error = false
                                playing_id = nil
                                now_playing = tracks[1]
                                queue = {}
                                for i = 2, #tracks do table.insert(queue, tracks[i]) end
                                chatReplyPrivate(chat_req.user, "Tocando agora: " .. (tracks[1].name or "?") .. " - " .. (tracks[1].artist or "YouTube"))
                            elseif chat_req.mode == "next" then
                                for i = #tracks, 1, -1 do table.insert(queue, 1, tracks[i]) end
                                chatReplyPrivate(chat_req.user, "Adicionado como proximo: " .. (tracks[1].name or "?"))
                            elseif chat_req.mode == "queue" then
                                for i = 1, #tracks do table.insert(queue, tracks[i]) end
                                chatReplyPrivate(chat_req.user, "Adicionado na fila: " .. (tracks[1].name or "?"))
                            end

                            os.queueEvent("audio_update")
                            os.queueEvent("redraw_screen")
                        end
                    end
                end

                if url == last_search_url then
                    local data = handle.readAll()
                    handle.close()
                    if data and data ~= "" then
                        local raw_results = textutils.unserialiseJSON(data)
                        if type(raw_results) == "table" then
                            if #raw_results > 1 then table.remove(raw_results, 1) end
                            search_results = raw_results
                        else
                            search_results = nil
                            search_error = true
                        end
                    else
                        search_results = nil
                        search_error = true
                    end
                    os.queueEvent("redraw_screen")
                end
                if url == last_download_url then
                    is_loading = false
                    player_handle = handle
                    start = handle.read(4)
                    size = 16 * 1024 - 4
                    playing_status = 1
                    os.queueEvent("redraw_screen")
                    os.queueEvent("audio_update")
                end
            end,
            function()
                local event, url = os.pullEvent("http_failure")

                local chat_req = pending_chat_searches[url]
                if chat_req then
                    pending_chat_searches[url] = nil
                    chatReplyPrivate(chat_req.user, "Erro de rede ao buscar: " .. chat_req.query)
                end

                if url == last_search_url then
                    search_error = true
                    os.queueEvent("redraw_screen")
                end
                if url == last_download_url then
                    is_loading = false
                    is_error = true
                    playing = false
                    playing_id = nil
                    os.queueEvent("redraw_screen")
                    os.queueEvent("audio_update")
                end
            end
        )
    end
end

-- ============================================================
-- MONITOR LOOP (Múltiplos Monitores)
-- ============================================================
function monitorLoop()
    if #monitors == 0 then return end

    local function drawMonitor(monitor)
        local mw, mh = monitor.getSize()
        if mw < 2 or mh < 2 then return end -- Pula monitores pequenos demais pra não bugar

        local BG = colors.black
        local WHITE = colors.white
        local LIGHT = colors.lightGray
        local DIM = colors.gray
        local GREEN = colors.green
        local LIME = colors.lime
        local YELLOW = colors.yellow
        local RED = colors.red

        local function mFill(x1, y1, x2, y2, bg)
            if x2 < x1 or y2 < y1 then return end
            monitor.setBackgroundColor(bg)
            local row = string.rep(" ", x2 - x1 + 1)
            for y = y1, y2 do
                monitor.setCursorPos(x1, y)
                monitor.write(row)
            end
        end

        local function mWrite(x, y, text, fg, bg)
            monitor.setCursorPos(x, y)
            monitor.setTextColor(fg or WHITE)
            monitor.setBackgroundColor(bg or BG)
            monitor.write(text or "")
        end

        local function mCenter(y, text, fg, bg, x1, x2)
            x1 = x1 or 1
            x2 = x2 or mw
            text = tostring(text or "")
            local x = x1 + math.floor(((x2 - x1 + 1) - #text) / 2)
            if x < x1 then x = x1 end
            mWrite(x, y, text, fg or WHITE, bg or BG)
        end

        local function mLine(y, x1, x2, color)
            mWrite(x1, y, string.rep("-", math.max(0, x2 - x1 + 1)), color or DIM, BG)
        end

        local function drawAlbumArt(track, x, y, size)
            local base = artColorFor(track and track.name or "MUSIC")
            local alt = (base == colors.green) and colors.lime or colors.white
            mFill(x, y, x + size - 1, y + size - 1, base)
            local seed = 0
            local name = tostring(track and track.name or "MUSIC")
            for i = 1, #name do
                seed = (seed + string.byte(name, i) * i) % 997
            end
            local band1 = 2 + (seed % math.max(1, size - 6))
            local band2 = 3 + ((seed * 3) % math.max(1, size - 7))
            if band1 > size - 2 then band1 = size - 2 end
            if band2 > size - 2 then band2 = size - 2 end
            mFill(x + 2, y + band1, x + size - 3, y + band1 + 1, alt)
            mFill(x + 2, y + band2, x + size - 3, y + band2, colors.black)
            local cx = x + math.floor(size / 2)
            local cy = y + math.floor(size / 2)
            mFill(cx - 3, cy - 3, cx + 3, cy + 3, colors.black)
            mFill(cx - 2, cy - 2, cx + 2, cy + 2, base)
            mFill(cx - 1, cy - 1, cx + 1, cy + 1, alt)
        end

        -- Inicia o Desenho
        monitor.setBackgroundColor(BG)
        monitor.clear()

        local pad = math.max(2, math.floor(mw * 0.05))
        local contentLeft = pad
        local contentRight = mw - pad
        local contentW = contentRight - contentLeft + 1

        mCenter(2, "SPOTIFY", LIME)
        mCenter(3, "MUSIC PLAYER", DIM)
        mLine(5, contentLeft, contentRight, GREEN)

        if now_playing then
            local title = monitorClip(now_playing.name or "Sem titulo", contentW)
            local artist = monitorClip(now_playing.artist or "Artista desconhecido", contentW)
            local artSize = math.min(28, math.max(16, math.floor(math.min(contentW * 0.30, mh * 0.28))))
            local artX = math.floor((mw - artSize) / 2) + 1
            local artY = 8
            drawAlbumArt(now_playing, artX, artY, artSize)

            local infoY = artY + artSize + 3
            mCenter(infoY, title, WHITE)
            mCenter(infoY + 2, artist, LIGHT)

            if is_loading then mCenter(infoY + 5, "CARREGANDO", YELLOW)
            elseif is_error then mCenter(infoY + 5, "ERRO DE REDE", RED)
            elseif playing then mCenter(infoY + 5, "TOCANDO", LIME)
            else mCenter(infoY + 5, "PAUSADO", DIM) end
        else
            mCenter(14, "NADA TOCANDO", DIM)
            mCenter(16, "Use !tocar <musica> no chat", LIGHT)
        end

        local queueDivider = math.floor(mh * 0.68)
        mLine(queueDivider, contentLeft, contentRight, DIM)
        mWrite(contentLeft, queueDivider + 2, "A SEGUIR", GREEN, BG)

        if #queue == 0 then
            mWrite(contentLeft, queueDivider + 4, "Fila vazia", DIM, BG)
        else
            local rowY = queueDivider + 4
            local maxItems = math.max(1, math.floor((mh - rowY - 3) / 2))
            for i = 1, math.min(#queue, maxItems) do
                local track = queue[i]
                local title = monitorClip(track.name or "Sem titulo", contentW - 8)
                local artist = monitorClip(track.artist or "", contentW - 8)
                mWrite(contentLeft, rowY, string.format("%02d", i), DIM, BG)
                mWrite(contentLeft + 4, rowY, title, WHITE, BG)
                mWrite(contentLeft + 4, rowY + 1, artist, DIM, BG)
                rowY = rowY + 2
            end
            if #queue > maxItems then
                mWrite(contentRight - 14, queueDivider + 2, "+" .. tostring(#queue - maxItems) .. " mais", DIM, BG)
            end
        end

        mLine(mh - 3, contentLeft, contentRight, DIM)
        local vol = math.floor(volume / 3 * 100 + 0.5)
        local loopText = looping == 0 and "LOOP OFF" or looping == 1 and "LOOP FILA" or "LOOP MUSICA"
        mWrite(contentLeft, mh - 1, loopText, looping ~= 0 and LIME or DIM, BG)
        local volText = "VOLUME " .. tostring(vol) .. "%"
        mWrite(contentRight - #volText + 1, mh - 1, volText, LIGHT, BG)
    end

    while true do
        for _, m in ipairs(monitors) do
            drawMonitor(m)
        end
        
        local ev = os.pullEvent()
        if ev == "peripheral" or ev == "peripheral_detach" then
            refreshMonitors()
        end
    end
end

function chatLoop()
    -- NÃO pode dar 'return' aqui se chat_box faltar: parallel.waitForAny
    -- encerra TUDO (UI, áudio, http) assim que qualquer função passada
    -- termina. Em vez disso, fica tentando achar o peripheral de novo a
    -- cada poucos segundos (ele pode demorar a aparecer no boot).
    while not chat_box do
        sleep(2)
        chat_box = peripheral.find("chat_box")
    end

    function chatReplyPrivate(recipient, msg)
        if chat_box.tell then
            chat_box.tell(recipient, "[iPod] " .. msg)
        elseif chat_box.sendToastToPlayer then
            chat_box.sendToastToPlayer("[iPod]", msg, recipient)
        else
            chat_box.sendMessage("[iPod] " .. msg, recipient)
        end
    end

    local function searchAndPlay(query, mode, user)
        chat_request_counter = chat_request_counter + 1
        local token = tostring(os.clock()):gsub("%D", "") .. "_" .. tostring(chat_request_counter)
        local url = api_base_url .. "?v=" .. version .. "&search=" .. textutils.urlEncode(query) .. "&chat_request=" .. token

        pending_chat_searches[url] = {
            user = user,
            mode = mode,
            query = query,
        }

        if mode == "now" and player_handle then
            pcall(player_handle.close)
            player_handle = nil
        end

        local ok, err = pcall(function()
            http.request({url = url, binary = false, method = "GET"})
        end)

        if not ok then
            pending_chat_searches[url] = nil
            chatReplyPrivate(user, "Erro ao iniciar busca: " .. tostring(err))
            return
        end

        -- A resposta será tratada por httpLoop(), sem bloquear o chatLoop.
    end

    while true do
        local event, user, msg, uuid = os.pullEvent("chat")

        if string.sub(msg, 1, 1) == "!" then
            local cmd, args = string.match(msg, "^!(%S+)%s*(.*)")
            cmd = string.lower(cmd or "")

            if cmd == "tocar" then
                if args and #args > 0 then
                    chatReplyPrivate(user, "Buscando: " .. args .. "...")
                    searchAndPlay(args, "now", user)
                else
                    if not playing and (now_playing ~= nil or #queue > 0) then
                        if now_playing == nil then
                            now_playing = queue[1]
                            table.remove(queue, 1)
                        end
                        playing_id = nil
                        playing = true
                        is_error = false
                        os.queueEvent("audio_update")
                        os.queueEvent("redraw_screen")
                        chatReplyPrivate(user, "Continuando: " .. (now_playing.name or "?"))
                    else
                        chatReplyPrivate(user, "Uso: !tocar <nome ou link>")
                    end
                end

            elseif cmd == "proximo" then
                if args and #args > 0 then
                    chatReplyPrivate(user, "Buscando: " .. args .. "...")
                    searchAndPlay(args, "next", user)
                else
                    chatReplyPrivate(user, "Uso: !proximo <musica>")
                end

            elseif cmd == "fila" then
                if args and #args > 0 then
                    chatReplyPrivate(user, "Buscando: " .. args .. "...")
                    searchAndPlay(args, "queue", user)
                else
                    if #queue == 0 then
                        chatReplyPrivate(user, "Fila vazia.")
                    else
                        local lista = "Fila (" .. #queue .. "): "
                        for i = 1, math.min(#queue, 5) do
                            lista = lista .. i .. ". " .. queue[i].name
                            if i < math.min(#queue, 5) then lista = lista .. " | " end
                        end
                        if #queue > 5 then lista = lista .. " ... e mais " .. (#queue - 5) end
                        chatReplyPrivate(user, lista)
                    end
                end

            elseif cmd == "pausar" or cmd == "parar" then
                if playing then
                    playing = false
                    for _, speaker in ipairs(speakers) do speaker.stop() end
                    os.queueEvent("playback_stopped")
                    playing_id = nil
                    is_loading = false
                    is_error = false
                    os.queueEvent("audio_update")
                    os.queueEvent("redraw_screen")
                    chatReplyPrivate(user, "Pausado.")
                else
                    chatReplyPrivate(user, "Nada tocando.")
                end

            elseif cmd == "continuar" then
                if not playing and now_playing ~= nil then
                    playing_id = nil
                    playing = true
                    is_error = false
                    os.queueEvent("audio_update")
                    os.queueEvent("redraw_screen")
                    chatReplyPrivate(user, "Continuando: " .. now_playing.name)
                elseif not playing and #queue > 0 then
                    now_playing = queue[1]
                    table.remove(queue, 1)
                    playing_id = nil
                    playing = true
                    is_error = false
                    os.queueEvent("audio_update")
                    os.queueEvent("redraw_screen")
                    chatReplyPrivate(user, "Continuando: " .. now_playing.name)
                else
                    chatReplyPrivate(user, "Ja esta tocando.")
                end

            elseif cmd == "pular" then
                if now_playing ~= nil or #queue > 0 then
                    is_error = false
                    if playing then
                        for _, speaker in ipairs(speakers) do speaker.stop() end
                        os.queueEvent("playback_stopped")
                    end
                    if #queue > 0 then
                        if looping == 1 then table.insert(queue, now_playing) end
                        now_playing = queue[1]
                        table.remove(queue, 1)
                        playing_id = nil
                        chatReplyPrivate(user, "Pulando para: " .. now_playing.name)
                    else
                        now_playing = nil
                        playing = false
                        is_loading = false
                        playing_id = nil
                        chatReplyPrivate(user, "Fila acabou.")
                    end
                    os.queueEvent("audio_update")
                    os.queueEvent("redraw_screen")
                else
                    chatReplyPrivate(user, "Nada na fila para pular.")
                end

            elseif cmd == "volume" then
                local vol = tonumber(args)
                if vol and vol >= 0 and vol <= 100 then
                    volume = vol / 100 * 3
                    os.queueEvent("redraw_screen")
                    chatReplyPrivate(user, "Volume: " .. vol .. "%")
                else
                    chatReplyPrivate(user, "Uso: !volume <0-100> | Atual: " .. math.floor(volume / 3 * 100) .. "%")
                end

            elseif cmd == "loop" then
                if looping == 0 then looping = 1 chatReplyPrivate(user, "Loop: Fila ativado.")
                elseif looping == 1 then looping = 2 chatReplyPrivate(user, "Loop: Musica ativado.")
                else looping = 0 chatReplyPrivate(user, "Loop: Desativado.") end
                os.queueEvent("redraw_screen")

            elseif cmd == "info" then
                if now_playing then
                    local status = playing and "Tocando" or "Pausado"
                    local vol = math.floor(volume / 3 * 100)
                    chatReplyPrivate(user, status .. ": " .. now_playing.name .. " | Vol: " .. vol .. "% | Fila: " .. #queue)
                else
                    chatReplyPrivate(user, "Nada tocando. Fila: " .. #queue)
                end

            elseif cmd == "limpar" then
                if #queue > 0 then
                    local removidas = #queue
                    queue = {}
                    os.queueEvent("redraw_screen")
                    chatReplyPrivate(user, "Fila limpa! " .. removidas .. " musica(s) removida(s).")
                else
                    chatReplyPrivate(user, "A fila já está vazia.")
                end

            elseif cmd == "remover" then
                local index = tonumber(args)
                if index and index >= 1 and index <= #queue then
                    local removed = table.remove(queue, index)
                    os.queueEvent("redraw_screen")
                    chatReplyPrivate(user, "Removido da fila: " .. removed.name)
                else
                    chatReplyPrivate(user, "Uso: !remover <numero> | A fila atual tem " .. #queue .. " itens.")
                end

            elseif cmd == "embaralhar" then
                if #queue > 1 then
                    for i = #queue, 2, -1 do
                        local j = math.random(1, i)
                        queue[i], queue[j] = queue[j], queue[i]
                    end
                    os.queueEvent("redraw_screen")
                    chatReplyPrivate(user, "Fila embaralhada!")
                else
                    chatReplyPrivate(user, "Musicas insuficientes na fila para embaralhar.")
                end

            elseif cmd == "link" then
                if now_playing and now_playing.id then
                    local link = "https://www.youtube.com/watch?v=" .. now_playing.id
                    chatReplyPrivate(user, "Link da musica atual: " .. link)
                else
                    chatReplyPrivate(user, "Nada tocando no momento.")
                end

            elseif cmd == "reiniciar" then
                chatReplyPrivate(user, "Reiniciando o sistema e retomando a musica...")
                local f = fs.open(state_file, "w")
                if f then
                    local state = {
                        now_playing = now_playing,
                        queue = queue,
                        looping = looping,
                        volume = volume
                    }
                    f.write(textutils.serialiseJSON(state))
                    f.close()
                end
                for _, speaker in ipairs(speakers) do speaker.stop() end
                sleep(1)
                os.reboot()
            end
        end
    end
end

parallel.waitForAny(uiLoop, audioLoop, httpLoop, monitorLoop, chatLoop)