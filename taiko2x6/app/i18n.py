"""UI language: English source strings, Traditional Chinese translations.

    from taiko2x6.app.i18n import tr
    QPushButton(tr("Open…"))
    tr("{n} songs").format(n=3)

The English text is the key. A string with no translation shows in English,
so untranslated dialogs keep working. The language comes from config.ini
[ui] lang ("en" or "zh-TW"); when unset it follows the system locale. A change
takes effect on the next start (widgets read tr() when they are built).
"""
from __future__ import annotations

from taiko2x6.app import appconfig

LANGUAGES = {"en": "English", "zh-TW": "繁體中文"}
_lang: str | None = None


def _system_lang() -> str:
    name = ""
    try:
        from PySide6.QtCore import QLocale
        name = QLocale.system().name()          # e.g. "zh_TW"
    except Exception:
        import locale
        name = (locale.getlocale()[0] or "")
    return "zh-TW" if name.lower().startswith(("zh", "chinese")) else "en"


def lang() -> str:
    global _lang
    if _lang is None:
        saved = appconfig.get("ui", "lang")
        _lang = saved if saved in LANGUAGES else _system_lang()
    return _lang


def set_lang(code: str) -> None:
    """Remember `code` for the next start (the running UI keeps its language)."""
    if code not in LANGUAGES:
        raise ValueError(code)
    appconfig.set_value("ui", "lang", code)


def tr_in(code: str, text: str) -> str:
    return ZH_TW.get(text, text) if code == "zh-TW" else text


def tr(text: str) -> str:
    return tr_in(lang(), text)


ZH_TW: dict[str, str] = {
    # --- main window: menus, toolbar ---
    "&File": "檔案(&F)",
    "&Tools": "工具(&T)",
    "&Settings": "設定(&S)",
    "Language": "語言",
    "Restart Taiko2x6Wizard to use the new language.": "重新啟動 Taiko2x6Wizard 後會套用新的語言。",
    "Main": "主要",
    "Open…": "開啟…",
    "Extract All…": "全部解出…",
    "Extract Selected…": "解出選取項目…",
    "Replace File…": "替換檔案…",
    "Save": "儲存",
    "Discard Edits": "捨棄修改",
    "Songs": "歌曲",
    "Charts && audio": "譜面與音訊",
    "PS2 HDD && memory card": "PS2 硬碟與記憶卡",
    "taiko executable patches": "taiko 主程式 patch",
    "Song Manager…": "歌曲管理…",
    "Edit/add/remove songs across musicinfo + tuning + streaminfo":
        "同時編輯 musicinfo、tuning、streaminfo，新增、修改或刪除歌曲",
    "Custom Song Builder…": "自製歌曲建置…",
    "Replace a song slot from a TJA + audio: textures, charts, VAG, stars":
        "用 TJA 和音訊替換一個歌曲欄位：歌名圖、譜面、VAG、星數",
    "Song Replacer…": "歌曲替換…",
    "Overwrite an existing song slot with a custom song (boot-safe: counts stay identical). Stages + writes DATA.000 in one step.":
        "用自製歌曲覆蓋既有的歌曲欄位（歌曲數量不變，可安全開機）。暫存並一次寫入 DATA.000。",
    "Pack to ISO…": "打包成 ISO…",
    "Inject the current DATA.000 + LIST.BIN into a new game ISO":
        "把目前的 DATA.000 + LIST.BIN 放進新的遊戲 ISO",
    "Close any emulator holding the image first.": "請先關閉佔用映像檔的模擬器。",
    "Open PS2 Memory Card (Arcade/COH)…": "開啟 PS2 記憶卡（Arcade/COH）…",
    "Browse a PS2 / MagicGate-Arcade memory-card image (what mymc does): extract / replace T14GAME, boot.bin, IRXARC.BIN… ECC is regenerated on write.":
        "瀏覽 PS2 / MagicGate-Arcade 記憶卡映像（和 mymc 一樣）：匯出或替換 T14GAME、boot.bin、IRXARC.BIN…，寫入時會重新產生 ECC。",
    "Open PS2 HDD via pfsshell…": "用 pfsshell 開啟 PS2 硬碟…",
    "Browse a PS2 HDD image with the native pfsshell.exe backend (extract / replace DATA.000, list.bin).":
        "用 pfsshell.exe 瀏覽 PS2 硬碟映像（匯出或替換 DATA.000、list.bin）。",
    "Patch taiko song limit (T14+)…": "taiko 歌曲上限 patch（T14+）…",
    "Raise the 210-song ceiling in `taiko` (the exe T14+ actually runs, on the HDD — not the dongle's T14GAME).":
        "提高 `taiko` 的 210 首上限（T14+ 實際執行的是硬碟上的這支主程式，不是加密狗裡的 T14GAME）。",
    "Song-select timer (T14+)…": "選歌倒數時間（T14+）…",
    "Change the song-select countdown in `taiko` (stock 120 s) to any value from 1 to 999 s.":
        "把 `taiko` 的選歌倒數（原版 120 秒）改成 1 到 999 秒。",
    "Boot & select patches (T14+)…": "開機與選歌 patch（T14+）…",
    "Checkboxes for `taiko`: skip the boot Notice screen, skip the attract credits (協力) screen, freeze the song-select countdown, disable the difficulty-select timeout. Unticking restores the stock code.":
        "`taiko` 的勾選項：跳過開機 Notice、跳過待機輪播的「協力」畫面、凍結選歌倒數、關閉難度選擇倒數。取消勾選會還原成原版程式碼。",
    "USB keyboard patch (T14)…": "USB 鍵盤 patch（T14）…",
    "Play Taiko 14 with a USB keyboard and no JVS I/O board (KBD2EE.irx). Installs into the HDD game partition (card untouched) or onto the dongle card image.":
        "不用 JVS IO 板，用 USB 鍵盤玩太鼓 14（KBD2EE.irx）。可安裝到硬碟的遊戲分割區（不動記憶卡），或安裝到加密狗記憶卡映像。",
    "Slim PS2 HDD image…": "縮小 PS2 硬碟映像…",
    "Reclaim disk space from a .img. Punches holes over regions that already read as zero, so the emulator still reads identical bytes.":
        "回收 .img 佔用的磁碟空間：把本來就是 0 的區域改成稀疏，模擬器讀到的內容完全相同。",
    "Merge HDD images (multi-game disk)…": "合併硬碟映像（多遊戲硬碟）…",
    "Combine several game HDD images into one APA disk. Each game's memory card looks up its own partition by name, so the card alone selects which game boots. Sources are never modified.":
        "把多個遊戲的硬碟映像合成一顆 APA 硬碟。每款遊戲的記憶卡會用名稱找自己的分割區，所以插哪張卡就開哪款遊戲。來源映像不會被修改。",
    "Omnimix Maker (fuse many games)…": "Omnimix 製作（合併多代歌曲）…",
    "Harvest every song from several Taiko images (7…14) and merge the ones the target lacks into one image — charts, textures, audio and stars — then lift the exe's song ceiling.":
        "從多個太鼓映像（7…14 代）收集歌曲，把目標缺少的歌合併進去（譜面、圖、音訊、星數），再提高主程式的歌曲上限。",
    "Browse…": "瀏覽…",
    "Output folder:": "輸出資料夾：",
    "Output folder": "輸出資料夾",
    "Pick an output folder.": "請選擇輸出資料夾。",
    "Chart converter (TJA / sht / Gen3 .bin)…": "譜面轉換器（TJA / sht / Gen3 .bin）…",
    "Chart converter (TJA / sht / Gen3 .bin)": "譜面轉換器（TJA / sht / Gen3 .bin）",
    "Convert charts between TJA, PS2-arcade .sht and Nijiiro (Gen3) fumen .bin in any direction, branches kept; a Gen3 song_<id>.nus3bank becomes a game VAG.":
        "在 TJA、PS2 機台 .sht 與新筐體（Gen3）fumen .bin 之間任意互轉，保留分歧；Gen3 的 song_<id>.nus3bank 會轉成遊戲 VAG。",
    "Add TJA, .sht or Gen3 (Nijiiro) fumen .bin files — or drop them here — and pick what to write. Files of the same song (same id) are converted together, so a song's difficulties become one TJA. A Gen3 song_<id>.nus3bank becomes a game VAG (needs vgmstream). Branches are kept in every direction.":
        "加入 TJA、.sht 或 Gen3（新筐體）fumen .bin（也可以直接拖進來），再勾選要輸出的格式。同一首歌（同 ID）的檔案會一起轉換，所以各難度會合成一個 TJA。Gen3 的 song_<id>.nus3bank 會轉成遊戲 VAG（需要 vgmstream）。各方向都會保留分歧。",
    "Add files…": "加入檔案…",
    "Clear": "清空",
    "Write": "輸出",
    ".sht (PS2 arcade)": ".sht（PS2 機台）",
    "Gen3 fumen .bin (Nijiiro)": "Gen3 fumen .bin（新筐體）",
    "VAG audio (from nus3bank)": "VAG 音訊（來自 nus3bank）",
    ".sht players:": ".sht 玩家：",
    "Song ID:": "歌曲 ID：",
    "from the file name": "取自檔名",
    "Output name when converting one song, e.g. the game's id (10tai). Left empty, each song keeps its own id.":
        "只轉一首歌時的輸出名稱，例如遊戲裡的 ID（10tai）。留空則每首歌沿用自己的 ID。",
    "Add charts": "加入譜面",
    "Charts and audio (*.tja *.sht *.bin sht *.nus3bank);;All files (*)":
        "譜面與音訊 (*.tja *.sht *.bin sht *.nus3bank);;所有檔案 (*)",
    "Add at least one file.": "請至少加入一個檔案。",
    "Tick at least one format to write.": "請至少勾選一種輸出格式。",
    "Convert": "轉換",
    "done: {s} songs, {f} files written, {e} problems": "完成：{s} 首歌，寫入 {f} 個檔案，{e} 個問題",
    "Still converting; close the window when it is done.": "還在轉換中，完成後再關閉視窗。",
    "VAG Generator (audio→vag)…": "VAG 產生器（音訊→vag）…",
    "Convert wav/ogg to the game's VAG stream, exported to a folder": "把 wav/ogg 轉成遊戲的 VAG 音訊，輸出到資料夾",
    "SHT Validator (check format)…": "SHT 檢查器（檢查格式）…",
    "Validate a .sht chart against the official on-disc format": "依官方格式檢查 .sht 譜面",

    # --- main window: layout ---
    "Name": "名稱",
    "Size": "大小",
    "Type": "類型",
    "Group offset": "群組內位移",
    "Filter by name…": "依名稱篩選…",
    "(no image)": "（沒有圖片）",
    "(no audio)": "（沒有音訊）",
    "Info": "資訊",
    "Hex": "十六進位",
    "Text": "文字",
    "Image": "圖片",
    "Audio": "音訊",
    "Extract / save options": "解出與儲存選項",
    "Extract options": "解出選項",
    "threads:": "執行緒：",
    "Number of worker threads used by Extract.": "解出時使用的執行緒數。",
    "Detected CPU cores: {n}": "偵測到的 CPU 核心：{n}",
    "write manifest.json (Extract All only)": "寫出 manifest.json（僅限全部解出）",
    "Write the same manifest.json index the CLI tool produces, so the extracted tree can be fed back to the CLI patch/repack commands.":
        "寫出和命令列工具相同的 manifest.json 索引，解出的檔案可以再交給命令列工具 patch 或重新打包。",
    "convert .nut (TIM2) → PNG": "把 .nut（TIM2）轉成 PNG",
    "For every .nut texture, also write a decoded .png next to it. The raw .nut is still extracted, so patching keeps working.":
        "每個 .nut 貼圖旁邊另外寫出解碼後的 .png。原始 .nut 照樣解出，不影響 patch。",
    "Save options": "儲存選項",
    "full rebuild (allow LARGER files; layout-preserving)": "完整重建（允許變大的檔案，保留原本配置）",
    "Always on. Repacks DATA.000 so a replacement can be any size while KEEPING every unchanged group at its original sector (boot-safe on real SYSTEM256 hardware); only edited groups are recompressed and appended.":
        "固定開啟。重新打包 DATA.000，替換的檔案大小不受限，沒改的群組都留在原本的磁區（實機可安全開機）；只有修改過的群組會重新壓縮並附加在後面。",
    "allow relayout (repack group internally)": "允許重新配置（在群組內重新打包）",
    "In-place mode only: lets an edited file grow into spare room within its own group, as long as the compressed group still fits its original slot.":
        "僅限原地模式：只要壓縮後的群組還放得進原本的位置，修改的檔案可以使用群組內的剩餘空間變大。",
    "overwrite originals (.bak backup)": "覆蓋原檔（保留 .bak 備份）",
    "Open an archive folder (File ▸ Open) to begin.": "開啟封包資料夾（檔案 ▸ 開啟）開始使用。",
    "Pick where to start. Everything here is also in the File and Tools menus.":
        "選擇要從哪裡開始。這些功能也都在「檔案」和「工具」選單裡。",
    "Open archive folder…": "開啟封包資料夾…",
    "A folder with DATA.000 + list.bin: browse, preview, replace files.":
        "含 DATA.000 + list.bin 的資料夾：瀏覽、預覽、替換檔案。",
    "Open PS2 HDD image…": "開啟 PS2 硬碟映像…",
    "Browse a Taiko HDD .img and extract or replace DATA.000 / list.bin.":
        "瀏覽太鼓的硬碟 .img，匯出或替換 DATA.000 / list.bin。",
    "Omnimix Maker…": "Omnimix 製作…",
    "Merge the songs of several Taiko images into one.": "把多個太鼓映像的歌合併成一個。",
    "Boot & select patches…": "開機與選歌 patch…",
    "Skip the Notice / credits screens, freeze the select timers.": "跳過 Notice／協力畫面、凍結選歌倒數。",

    # --- main window: messages ---
    "Choose folder containing LIST.BIN + DATA.000": "選擇含有 LIST.BIN + DATA.000 的資料夾",
    "Not found": "找不到",
    "Could not find both LIST.BIN and DATA.000 in:\n{folder}": "這個資料夾裡沒有同時找到 LIST.BIN 和 DATA.000：\n{folder}",
    "Open failed": "開啟失敗",
    "{groups} groups · {files} files · DATA {mb:.1f} MB": "{groups} 個群組 · {files} 個檔案 · DATA {mb:.1f} MB",
    "Read failed": "讀取失敗",
    "Editor failed": "編輯器開啟失敗",
    "Song texture": "歌名圖",
    "This nut is not a 640×160 4-bit song-name slot.": "這個 nut 不是 640×160 4-bit 的歌名圖欄位。",
    "Frame Player": "畫格播放",
    "No .nut frames found here.": "這裡沒有 .nut 畫格。",
    "Sound bank": "音色庫",
    "This group has no hd/bd pair.": "這個群組沒有 hd/bd 檔案。",
    "Sound bank failed": "音色庫開啟失敗",
    "Export file": "匯出檔案",
    "Export failed": "匯出失敗",
    "Replace": "替換",
    "Select a single file to replace.": "請選擇一個要替換的檔案。",
    "Discard": "捨棄",
    "Discard all staged replacements?": "要捨棄所有暫存的替換嗎？",
    "Song Manager": "歌曲管理",
    "Song Manager failed": "歌曲管理開啟失敗",
    "Patch taiko": "taiko patch",
    "Song-select timer": "選歌倒數",
    "Boot & select patches": "開機與選歌 patch",
    "Omnimix Maker": "Omnimix 製作",
    "PySide6 is required.": "需要 PySide6。",
    "Slim image": "縮小映像",
    "Merge HDD images": "合併硬碟映像",
    "Song Replacer": "歌曲替換",
    "Open an archive first.": "請先開啟封包。",
    "Write DATA.000": "寫入 DATA.000",
    "Write new archive": "寫入新封包",
    "Write failed": "寫入失敗",
    "New song added": "已新增歌曲",
    "Save failed": "儲存失敗",
    "Saved": "已儲存",
    "Wrote {groups} group(s), {files} file(s) in {took}.": "已寫入 {groups} 個群組、{files} 個檔案，耗時 {took}。",
    "Reload failed": "重新載入失敗",
    "Extract": "解出",
    "Extract to folder": "解出到資料夾",
    "Nothing selected to extract.": "沒有選取要解出的項目。",
    "Extract failed": "解出失敗",
    "Extract complete": "解出完成",
    "Extracted {g} groups, {f} files": "已解出 {g} 個群組、{f} 個檔案",
    "Unsaved edits": "有未儲存的修改",
    "You have unsaved replacements. Quit anyway?": "還有未儲存的替換，確定要離開嗎？",

    # --- Omnimix dialog ---
    "Omnimix Maker — fuse songs from many images into one": "Omnimix 製作：把多個映像的歌合併成一個",
    "Fuses the songs of several Taiko images into one. Only songs the TARGET does not already have are added (dedup by id; the target is the base). Charts, textures, audio and stars are copied as-is. ⚠ Close PCSX2 first, and work on a COPY — the .img is written in place.":
        "把多個太鼓映像的歌合併成一個。只會加入「目標」還沒有的歌（依歌曲 ID 去重，以目標為基礎）。譜面、圖、音訊、星數原樣複製。⚠ 請先關閉 PCSX2，並在複本上操作：.img 會被直接改寫。",
    "Target (merge INTO this — the base)": "目標（合併到這裡，作為基礎）",
    "target type:": "目標類型：",
    "HDD .img (Taiko 14+)": "硬碟 .img（太鼓 14+）",
    "Folder (DATA.000 + list.bin)": "資料夾（DATA.000 + list.bin）",
    "target path:": "目標路徑：",
    "(auto — chosen on Preview)": "（自動：按「預覽計畫」時選擇）",
    "partition:": "分割區：",
    "Sources (harvest FROM these — first one wins on ties)": "來源（從這些收集歌曲，重複時排前面的優先）",
    "Add .img…": "加入 .img…",
    "Add ISO…": "加入 ISO…",
    "Add folder…": "加入資料夾…",
    "Up": "上移",
    "Down": "下移",
    "Remove": "移除",
    "Options": "選項",
    "when songs exceed 214:": "歌曲超過 214 首時：",
    "Fit everything, patch the ceiling (up to 644)": "全部放入並提高上限（最多 644）",
    "Cap at 214 songs": "限制在 214 首",
    "song audio:": "歌曲音訊：",
    "keep original (44.1 kHz)": "保留原始（44.1 kHz）",
    "re-encode all songs at 30 kHz (fits ~600 songs)": "全部重新編碼為 30 kHz（約可放 600 首）",
    "re-encode all songs at 24 kHz": "全部重新編碼為 24 kHz",
    "DATA.000 must stay under 2 GiB or the game hangs on LOADING. At 44.1 kHz that is about 430 songs. Lower rates keep speed and pitch (the game reads the rate from each song), only less treble.":
        "DATA.000 必須小於 2 GiB，否則遊戲會卡在 LOADING。44.1 kHz 大約只能放 430 首。降低取樣率不影響速度和音高（遊戲會讀每首歌的取樣率），只是高音少一些。",
    "back up the target .img before writing": "寫入前備份目標 .img",
    "patch the song-count ceiling in the exe (.img target)": "修改主程式的歌曲數上限（.img 目標）",
    "Game patches applied after the build (.img target)": "建置完成後套用的遊戲 patch（.img 目標）",
    "Unticked ones are left as they are. Change them later under Tools ▸ taiko executable patches.":
        "沒勾的項目維持原狀。之後可以在「工具 ▸ taiko 主程式 patch」修改。",
    "idle": "待命",
    "log:": "記錄：",
    "Preview plan": "預覽計畫",
    "Build Omnimix": "建置 Omnimix",
    "Close": "關閉",
    "Open .img failed": "開啟 .img 失敗",
    "No partitions": "沒有分割區",
    "No PFS partitions found.": "找不到 PFS 分割區。",
    "Choose partition": "選擇分割區",
    "Target PS2 HDD image": "目標 PS2 硬碟映像",
    "Target folder (DATA.000 + list.bin)": "目標資料夾（DATA.000 + list.bin）",
    "Source PS2 HDD image": "來源 PS2 硬碟映像",
    "Source game ISO (DATA.000 + LIST.BIN)": "來源遊戲 ISO（DATA.000 + LIST.BIN）",
    "Source folder (DATA.000 + list.bin)": "來源資料夾（DATA.000 + list.bin）",
    "Label": "名稱",
    "Name this source (e.g. {ex}):": "替這個來源命名（例如 {ex}）：",
    "Not a Taiko game ISO": "不是太鼓的遊戲 ISO",
    "That ISO has no DATA.000 + LIST.BIN in its root.": "這個 ISO 的根目錄沒有 DATA.000 + LIST.BIN。",
    "Open ISO failed": "開啟 ISO 失敗",
    "Not a game folder": "不是遊戲資料夾",
    "That folder has no DATA.000 + list.bin.": "這個資料夾沒有 DATA.000 + list.bin。",
    "Choose a target.": "請選擇目標。",
    "Add at least one source.": "請至少加入一個來源。",
    "Extracting images (large, ~1 min each)…": "正在解出映像（檔案很大，每個約 1 分鐘）…",
    "Extracting images…": "正在解出映像…",
    "Plan failed": "計畫失敗",
    "plan ready": "計畫完成",
    "Plan ready": "計畫完成",
    "Songs to add by folder:": "各資料夾要加入的歌曲數：",
    "Click “Build Omnimix” to assemble.": "按「建置 Omnimix」開始組合。",
    "Run Preview plan first.": "請先執行「預覽計畫」。",
    "Too many songs": "歌曲太多",
    "Above 214 songs": "超過 214 首",
    "Write?": "要寫入嗎？",
    "Add {n} songs → {total} total, then write into:\n{where}\n\nProceed?":
        "加入 {n} 首，合計 {total} 首，然後寫入：\n{where}\n\n要繼續嗎？",
    "Building — this rewrites the whole archive and (for .img) writes it back. Please wait…":
        "建置中：會重寫整個封包（.img 目標還會寫回映像）。請稍候…",
    "Building…": "建置中…",
    "Done — added {added} songs, total {total}.": "完成：加入 {added} 首，合計 {total} 首。",
    "Exe song ceiling patched to {n}.": "主程式歌曲上限已改為 {n}。",
    "Game patches on:": "已開啟的遊戲 patch：",
    "Added by folder:": "各資料夾加入的歌曲數：",
    "{n} warnings (see log).": "{n} 個警告（請看記錄）。",
    "Omnimix built": "Omnimix 建置完成",
    "Omnimix failed": "Omnimix 失敗",
    "done": "完成",
    "failed": "失敗",
    "genre {g}": "類別 {g}",

    # --- taiko exe dialogs ---
    "Patch taiko song limit (T14+)": "taiko 歌曲上限 patch（T14+）",
    "Song-select timer (T14+)": "選歌倒數時間（T14+）",
    "Boot & select patches (T14+)": "開機與選歌 patch（T14+）",
    "HDD .img…": "硬碟 .img…",
    "Song ceiling:": "歌曲上限：",
    "Seconds:": "秒數：",
    "Check": "檢查",
    "Read current": "讀取目前狀態",
    "Back up taiko…": "備份 taiko…",
    "Patch": "套用 patch",
    "Apply": "套用",
    "Restore…": "還原…",
    "Open PS2 HDD image": "開啟 PS2 硬碟映像",
    "Pick an HDD image first.": "請先選擇硬碟映像。",
    "Reading + decrypting…": "讀取並解密中…",
    "Back up taiko to": "把 taiko 備份到",
    "Extracting /taiko…": "正在取出 /taiko…",
    "Restore taiko from (your backup)": "從備份還原 taiko",
    "Restoring /taiko…": "正在還原 /taiko…",
    "Applying…": "套用中…",
    "applied ✓": "已套用 ✓",
    "restored ✓": "已還原 ✓",
    "failed — image untouched": "失敗，映像未被修改",
    "matches the image": "和映像目前狀態相同",
    "{n} change(s) to apply": "有 {n} 項變更待套用",
    "Patches (ticked = on)": "Patch（勾選 = 開啟）",
    "Pick an image; its current patches are read in. Tick what you want and Apply. Unticking a patch restores the stock code. Close PCSX2 first — it locks the .img.":
        "選擇映像後會讀取目前已開啟的 patch。勾選想要的項目再按「套用」；取消勾選會還原成原版程式碼。請先關閉 PCSX2，它會鎖住 .img。",
    "Stock is {n} s. Sets all three select-flow countdowns. Close PCSX2 first — it locks the .img. Back up `taiko` before your first patch.":
        "原版是 {n} 秒，會同時修改選歌流程的三個倒數。請先關閉 PCSX2（它會鎖住 .img），第一次 patch 前請先備份 `taiko`。",
    "The image is modified in place; an undo journal is saved first. Make sure PCSX2 is closed. Continue?":
        "映像會被直接修改，修改前會先存一份還原記錄。請確認 PCSX2 已關閉。要繼續嗎？",
    "Raise the song ceiling to {n} in {name}?": "要把 {name} 的歌曲上限提高到 {n} 嗎？",
    "Set the song-select timer to {n} s in {name}?": "要把 {name} 的選歌倒數設為 {n} 秒嗎？",
    "Change these in {name}?": "要在 {name} 套用以下變更嗎？",
    "ON ": "開啟",
    "OFF": "關閉",
    "Skip the boot Notice screen": "跳過開機 Notice 畫面",
    "Skip the attract credits (協力) screen": "跳過待機輪播的「協力」畫面",
    "Freeze the song-select countdown": "凍結選歌倒數",
    "Disable the difficulty-select 30 s timeout": "關閉難度選擇的 30 秒倒數",
    "Boot straight past the 10 s NOTICE / copyright screen.": "開機直接略過 10 秒的 NOTICE／版權畫面。",
    "The attract loop moves on instead of showing the 協力 license list.": "待機輪播不顯示「協力」授權名單，直接進入下一個畫面。",
    "The song-select countdown stays on screen but never runs out.": "選歌倒數仍會顯示，但不會歸零。",
    "The difficulty (course) select never auto-starts after 30 s.": "難度選擇不會在 30 秒後自動開始。",
    "Repair existing Omnimix": "修復現有的 Omnimix",
    "Brings an Omnimix built with an older version up to date without rebuilding it: trims the select title textures (the 2nd song of a credit no longer hangs) and, with the same sources as the build (stock TK14 first), fixes gauge values and preview starts.": "不必重建，就把舊版工具做的 Omnimix 更新到最新：裁切選歌曲名貼圖（連玩第 2 首不再卡住）；若加入與建置時相同的來源（原版 TK14 放第一個），也會修正魂量表數值和試聽起點。",
    "Repair works on a HDD .img target. Choose one.": "修復只能用在 HDD .img 目標，請先選擇一個。",
    "Sources: {n} (gauge values and preview starts are fixed too).": "來源：{n} 個（也會修正魂量表數值和試聽起點）。",
    "No sources added: only the title textures are trimmed.": "沒有加入來源：只會裁切曲名貼圖。",
    "Repair partition {part} of\n{img}\nin place?": "要直接修復\n{img}\n的分割區 {part} 嗎？",
    "Repairing…": "修復中…",
    "Repaired and written back.": "已修復並寫回。",
    "Nothing to repair: this Omnimix is up to date.": "沒有需要修復的地方：這份 Omnimix 已是最新。",
    "Song select as category folders": "選歌改成類別資料夾",
    "Keep songs already played this credit in the list": "同一次投幣已玩過的歌不從清單隱藏",
    "A song played earlier in the same credit is not hidden from the song select.": "同一次投幣中玩過的歌，選歌畫面仍會顯示，可以再選。",
    "Song select shows only the category cards; don on a card opens that folder, both ka close it.": "選歌只顯示類別卡，在卡上按咚打開資料夾，同時按左右咔關閉。",
}
