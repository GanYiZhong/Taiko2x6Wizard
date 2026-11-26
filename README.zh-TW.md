# Taiko2x6Wizard

[English](README.md) | **繁體中文**

一套以 Python + Qt（PySide6）開發的工具，用來**逆向分析與改造大型機台版《太鼓之達人》**，
對象是以 PS2 為基礎的 SYSTEM256 / SYSTEM2x6 世代（約 Taiko 8～14 代）。
你可以在同一個 Explorer 介面裡瀏覽遊戲封存檔、新增或替換歌曲（譜面、音訊、貼圖、資料庫）、
編輯各首歌的 `.bin` 資料表、把 TJA 與虹版（Nijiiro）譜面轉成機台格式，
以及讀寫 PS2 硬碟映像檔和記憶卡。

> **獨立的同好專案，不含任何遊戲內容。** 本 repo 只提供原創的工具程式，
> 不包含遊戲程式碼、音訊、貼圖、字型或資料。你必須自行準備合法取得的檔案
> （見[需要自備的檔案](#需要自備的檔案)）。本專案與 Bandai Namco 無關，也未獲其認可。

---

## 功能

- **封存檔瀏覽器（Explorer）**：開啟遊戲的 `DATA.000` + `list.bin`，瀏覽所有群組與檔案，
  預覽貼圖、播放音訊，並直接編輯。
- **新增／替換歌曲**：一次處理曲名貼圖、譜面（fumen）、音訊（VAG）與所有資料庫欄位，
  再重建封存檔。
- **Omnimix 合併**：把 Taiko 7～14 多款遊戲的歌曲合併進同一個映像檔，
  自動去除重複歌曲、解除歌曲數量上限，必要時自動擴大分割區。
- **譜面轉換**：單一轉換器（工具 → 譜面與音訊），TJA、SHT 與加密的**虹版（Gen3）**
  fumen `.bin` 之間任意互轉並保留分歧；虹版 `song_<id>.nus3bank` 會轉成遊戲 VAG
  （保留分歧譜面、正確處理變速）。
- **歌曲資料表編輯器**：`musicinfo`、`tuning`、`streaminfo`、`rank`、`lamp`、`fname`、
  `hdbdinfo`、`enso_parts`，Explorer 會自動找到並開啟對應的編輯器。
- **音訊**：VAG（PS2 ADPCM）編碼／解碼、匯入 OGG/WAV、播放 `hd/bd` 音效庫。
- **圖像**：TIM2 圖檔讀寫、用勘亭流字型產生曲名貼圖、編輯 SWG 精靈動畫、播放逐格動畫。
- **PS2 儲存裝置**：自行實作的 APA 分割區 + PFS 檔案系統讀寫、記憶卡讀寫、ISO 打包、
  多款遊戲合併到同一顆硬碟、映像檔瘦身。
- **執行檔修補**：解密並修補 T14 以後的 `taiko` ELF，例如提高歌曲數量上限、
  調整選歌倒數、跳過開機的 Notice 畫面。
- **USB 鍵盤 patch（Taiko 14）**：用一般 USB 鍵盤遊玩，不需要 JVS I/O 板
  （[KBD2EE](https://github.com/GanYiZhong/Taiko14-USB-Keyboard-Patch)）；
  可以裝進硬碟映像（不動加密狗卡），或裝到加密狗卡映像。

模組說明見 [`docs/ARCHITECTURE.md`](docs/ARCHITECTURE.md)，
逆向出來的檔案格式見 [`docs/FILE_FORMATS.md`](docs/FILE_FORMATS.md)（皆為英文）。

## 下載（不需要安裝 Python）

到 [Releases 頁面](https://github.com/GanYiZhong/Taiko2x6Wizard/releases)下載最新的
**`Taiko2x6Wizard.exe`**。這是單一 Windows 執行檔，已內含 Python 與所有函式庫，
直接執行即可，不用安裝。

有四個檔案**沒有**包在裡面（商用字型、第三方工具、你自己的遊戲資料）。
要用到相關功能時，把它們放在 `.exe` 旁邊：`Font.ttf`、`vgmstream-win64/`、
`pfsshell.exe`、`T14LOAD.bin`。詳見[需要自備的檔案](#需要自備的檔案)。
想從原始碼執行的開發者請看下面的說明。

---

## 系統需求

- **Python 3.12 以上**（PySide6 6.12 在 Python 3.10/3.11 上，`QStandardItem.setData`
  每呼叫一次就少算一次 `None` 的引用，開幾次大型封存檔就會讓程式崩潰；
  Python 3.12 起 `None` 不會被釋放）。發佈版內建 Python 3.13。
- 主要在 Windows 上開發與測試（PS2 相關工具和外部執行檔以 Windows 為主），
  純 Python 的解析器可以跨平台使用。

```bash
pip install -r requirements.txt
```

核心套件：`PySide6`、`numpy`、`Pillow`、`pycryptodome`。匯入音訊還需要 `soundfile`
（`librosa` 為選用）。詳見 [`requirements.txt`](requirements.txt)。

## 需要自備的檔案

以下檔案屬於第三方或來自遊戲本身，所以**不在** repo 裡（已被 git 忽略）。
請放在專案根目錄（`main.py` 旁邊）：

| 檔案／資料夾 | 用途 | 取得方式 |
|---|---|---|
| `Font.ttf` | 勘亭流毛筆字型（DFPKanTeiRyu-XB），產生曲名貼圖用 | 自備合法授權的字型，改名為 `Font.ttf` |
| `T14LOAD.bin` | 用來推導 T14 `taiko` 執行檔金鑰的載入器資料 | 從你自己的遊戲或加密狗取出 |
| `pfsshell.exe` | PFS 參考工具（`ps2hdd` 的替代方案，選用） | [uyjulian/pfsshell](https://github.com/uyjulian/pfsshell) 的 Releases |
| `vgmstream-win64/` | vgmstream 命令列工具，解碼虹版 `.nus3bank` 音訊用 | [vgmstream.org](https://vgmstream.org/)，把 win64 版解壓到這裡 |

遊戲資料本身（`DATA.000`、`list.bin`、`.img`、`.iso`、`.ps2`、譜面、音訊）
需要你自己提供，這些都已被 git 忽略，不會被上傳到 repo。

---

## 快速開始

所有指令都在專案根目錄執行：

```bash
cd Taiko2x6Wizard
python main.py                       # 開啟 Explorer 主介面
# 也可以直接指定放有 DATA.000 + list.bin 的資料夾：
python main.py "D:/Taiko/extract"
```

Explorer 是整個工具的中心，`.bin` 編輯器和大部分工具都從它的選單開啟。
部分模組也能用 `python -m` 單獨執行，例如：

```bash
python -m taiko2x6.exe.taiko_exe TK14.img -n 214 --backup taiko.bak  # 提高歌曲上限（命令列）
python -m taiko2x6.exe.taiko_exe TK14.img --select-timer 999          # 選歌倒數秒數（命令列）
python -m taiko2x6.exe.taiko_exe --help                               # 所有執行檔修補選項
python -m taiko2x6.exe.usb_keyboard install TK14.img --card NM00057.ps2  # USB 鍵盤 patch
python -m taiko2x6.charts.gen3_convert --help      # 虹版譜面 → SHT（命令列）
python -m taiko2x6.charts.chart_convert --help     # TJA／SHT／虹版譜面任意互轉（命令列）
```

（執行檔修補在 Explorer 的 Tools 選單裡也有圖形介面。）

在自己的腳本裡使用時，從套件 import（在專案根目錄執行，或把根目錄加入 `PYTHONPATH`）：

```python
from taiko2x6.ps2 import ps2hdd
from taiko2x6.songs.song_manager import SongManager
```

`config.ini`（已被 git 忽略）只用來記住你上次用過的路徑。可以把
[`config.ini.example`](config.ini.example) 複製成 `config.ini` 預先設定，
或是讓介面在你瀏覽時自動填入。

---

## 常見流程

- **在 T14 以後的硬碟映像檔加歌**：用 `ps2/ps2hdd` 從遊戲分割區取出 `list.bin` / `DATA.000`，
  用 `songs/song_builder` 加入歌曲（貼圖 + 譜面 + 音訊 + 資料庫），
  用 `archive/archive_builder` 重建，再用 `ps2/ps2hdd` 寫回去。
  如果歌曲數量超過上限，先用 `exe/taiko_exe` 修補執行檔。
- **匯入 TJA 譜面**：`charts/tja2sht` 轉換譜面，`audio/vagtool` 把 OGG/WAV 轉成 VAG，
  `graphics/songtex_all` 產生曲名貼圖。
- **匯入虹版歌曲**：`charts/gen3_song` / `charts/gen3_convert` 負責解密、轉換譜面，
  並透過 `vgmstream-win64/` 解碼音訊。
- **用 USB 鍵盤玩 Taiko 14**：Tools → *USB keyboard patch (T14)*（`exe/usb_keyboard`）。
  按鍵：1P <kbd>D</kbd> <kbd>F</kbd> <kbd>J</kbd> <kbd>K</kbd>、
  2P <kbd>Z</kbd> <kbd>X</kbd> <kbd>C</kbd> <kbd>V</kbd>、投幣 <kbd>5</kbd>、
  SERVICE <kbd>9</kbd>、TEST <kbd>F1</kbd>、選單 <kbd>↑</kbd> <kbd>↓</kbd> <kbd>Enter</kbd>；
  開機後要把鍵盤拔掉再插一次。
  - *硬碟映像*（預設）：`taiko` 會先讀自己分區上的 `pfs0:IRXARC.BIN;1`，找不到才讀卡片的
    `mc0:IRXARC.BIN`，所以工具會把「卡片原版 IRXARC + KBD2EE」寫進 `t14jp1400.0001`。
    第一次安裝需要提供一次原版 IRXARC（加密狗卡映像或取出的 `IRXARC.BIN`），卡片本身不會被修改。
    **尚未在實機上測試。** 按 *Remove* 會刪掉這些檔案，遊戲就會改回讀卡片。
  - *卡片映像*：原本在實機測試過的做法，修改卡片上的 `IRXARC.BIN` 和 `T14LOAD`
    （會先備份一份 `.bak`）。
  - *建置版本*：**鍵盤 + 假 JVS**（預設，實機測試過）不接 IO 板就能玩。
    **只有鍵盤**（`--kbdonly`）給仍接著原版 IO 板的機台：IO 板照常運作，鍵盤可以一起用
    （同一份原始碼拿掉假 JVS 的 hook 編譯而成；尚未在實機上測試）。
  - 安裝前會先解密映像裡的 `taiko` 並檢查：KBD2EE 會覆寫 7 段固定位址的程式碼、
    使用一塊固定的記憶體，如果 `taiko` 版本不同就會蓋壞遊戲程式，所以不相符時會拒絕安裝。
    解密用的金鑰取自卡片映像、`--t14load` 或 `T14LOAD.bin`；卡片模式請用 `--img`
    指定要檢查的硬碟映像。歌曲上限和 Omnimix 的修補不會改到這些位置。
    只想檢查可以執行 `python -m taiko2x6.exe.usb_keyboard check TK14.img --card NM00057.ps2`。
- **合併多款遊戲（Omnimix）**：Tools → *Omnimix Maker*（`songs/omnimix_maker`）。
  214 首以內沿用原本已驗證的修補。超過 214 首（最多 644 首）時會一併加大每首歌的紀錄表
  （`exe/song_tables`）：遊戲把成績、皇冠、遊玩次數和人氣排行存在固定 210 格的表裡，
  以前第 210 首以後的歌都會寫到表外。現在表格容得下所有歌；前 210 首的紀錄照舊存進基板 SRAM，
  210 首以後的紀錄在關機前都有效，但不會保存（32 KB 的 SRAM 已經滿了）。
  已在 PCSX2x6 用 331 首的映像實測：第 210、214 首可以遊玩，紀錄和排行都會更新，前 210 首的紀錄重開機後仍在。
  Taiko 7 當來源時會自動轉換：T7 的譜面是舊格式（時間與 BPM 是整數、沒有捲動速度），
  T8 以後的遊戲會讀錯，所以會轉成 Gen2 格式（音符不變；T7 全部 645 份譜面都通過譜面檢查），
  難度星數也改用 T7 自己的 `tuning.bin` 格式讀取。尚未在遊戲中實際遊玩測試。
  選歌畫面的曲名貼圖會裁掉底部全透明的列，省下約 3 MB 遊戲記憶體；不裁的話，約 600 首的
  Omnimix 在同一局第 2 首會卡住。同一個對話框的「修復現有的 Omnimix」可以把這項修正，以及
  （加入建置時的來源後）魂量表數值和試聽起點的修正，套到舊版工具做的 Omnimix 上，不必重建。

> ⚠️ **安全須知**：寫入 PS2 硬碟 `.img` 前，**請先關閉 PCSX2**，它會鎖住映像檔不讓寫入
> （只讀取的話不用關）。在**副本**上操作仍是最保險的做法。

### 寫入保護

所有會修改遊戲檔案的工具都經過 `taiko2x6/safeio.py`：

| 寫入對象 | 保護方式 |
|---|---|
| `DATA.000` + `list.bin`（Explorer 存檔、Song Builder、封存 CLI、Omnimix 輸出到資料夾） | 兩個檔案一起成功或一起失敗。覆寫時會保留 `.bak`、`.bak.1`…。如果存檔中途程式掛掉，下次開啟封存時會自動完成或還原。 |
| PS2 硬碟 `.img`（HDD Browser、HDD Song Wizard、Omnimix Maker、`taiko_exe`、USB 鍵盤 patch） | 覆寫任何磁區前，先把舊內容存進 **undo 日誌** `<img>.<日期-時間>.undo`（大小約等於寫入的資料量，不是整個映像檔）。寫入失敗會當場自動還原；寫入成功時日誌會保留，當作這次寫入的備份。 |
| 記憶卡 `.ps2` | 以寫入模式開啟前先複製一份 `.bak`。 |
| ISO 的 UDF 修復 | 舊的檔頭先存進 undo 日誌。 |

事後想還原映像檔（或寫到一半當機、斷電）時：

```bash
python -m taiko2x6.safeio info     TK14.img.20261118-101500.undo
python -m taiko2x6.safeio rollback TK14.img.20261118-101500.undo
```

有多個日誌時，從最新的開始還原；不需要時可以自行刪除。

---

## 疑難排解

| 狀況 | 解法 |
|---|---|
| 出現 `T14LOAD.bin not found` | 把 `T14LOAD.bin` 放在專案根目錄（或 `.exe` 旁邊），或用環境變數 `T14LOAD_BIN` 指定路徑。 |
| 出現 `vgmstream-cli not found` | 解壓 vgmstream 的 win64 版，讓 `vgmstream-win64/vgmstream-cli.exe` 存在，或加上 `--vgmstream` 參數。 |
| 出現 `pfsshell.exe not found` | 只有走 pfsshell 路線才需要；內建的 `ps2/ps2hdd` 不需要它。 |
| 加歌時出現 `Font.ttf` 錯誤 | 準備勘亭流字型並命名為 `Font.ttf`（見上方）。 |
| 無法寫入 `.img`／權限錯誤 | 關閉 PCSX2，它會鎖住映像檔。 |
| 選歌時卡住（Taiko 8） | 群組的 `unknown2` 雜湊錯誤，請用 `archive_builder` 重建（見 `docs/unknown2_report.md`）。 |
| 歌加進去了但看不到（T14） | musicinfo 的自我索引欄位必須指向新的那一列，請用 Song Manager／Song Builder 加歌，不要直接複製原始資料列。 |
| 寫入後映像檔壞了／想回到舊版 | `python -m taiko2x6.safeio rollback <img>.<日期-時間>.undo`（從最新的日誌開始）。 |
| `DATA.000` 旁出現 `.txn`／`.txn-old` 檔 | 存檔被中斷留下的；重新開啟封存就會自動清掉。 |
| 需要更多訊息 | 用 `python main.py --debug` 啟動（或設定 `TAIKO_LOG=DEBUG`）。 |

## 測試

```bash
pip install -r requirements-dev.txt
python -m pytest
```

測試會自己產生迷你的封存檔和 PFS 映像，不需要遊戲檔案；GitHub Actions 會在
Windows 和 Linux 上執行。想用自己的遊戲檔案測試，把 `TAIKO_GAME_LIBRARY`
指向放遊戲資料夾的目錄即可（原檔只會以唯讀開啟，寫入都在 pytest 暫存資料夾的副本上做）。
加上 `TAIKO_HEAVY=1` 會多跑一個複製數 GB 映像檔的測試：

```bash
TAIKO_GAME_LIBRARY=E:/games/system2x6 TAIKO_HEAVY=1 python -m pytest --basetemp=E:/pytest-tmp
```

各模組的自我測試（`python -m taiko2x6.<子資料夾>.<模組>`）會到 `samples/`
（已被 git 忽略）或環境變數 `TAIKO_SAMPLES` 指定的資料夾找範例檔。

---

## 專案結構

```
Taiko2x6Wizard/
├── README.md                  英文說明
├── README.zh-TW.md            本檔案
├── LICENSE                    MIT（僅涵蓋工具程式，不含遊戲內容）
├── requirements.txt           Python 套件需求
├── requirements-dev.txt       測試用套件（pytest）
├── pytest.ini                 測試設定
├── .gitignore                 排除遊戲資料與需自備的檔案
├── config.ini.example         路徑記憶的範本
├── main.py                    啟動程式，開啟 Explorer 主介面
├── Taiko2x6Wizard.spec        PyInstaller 打包設定（單一執行檔）
├── tests/                     pytest 測試（合成資料；實機資料測試需手動開啟）
├── .github/workflows/         CI（在 Windows + Linux 跑 pytest）
├── taiko2x6/                  工具主體套件
│   ├── safeio.py               安全寫入：原子寫入、DATA+LIST 交易、undo 日誌
│   ├── app/                    Explorer 主介面、設定與資源路徑
│   ├── archive/                DATA.000 + list.bin 模型、讀取／存檔／重建
│   ├── songs/                  歌曲新增／替換、資料庫管理、Omnimix
│   ├── bineditors/             各 .bin 資料表編輯器（自動偵測）
│   ├── charts/                 TJA／SHT／虹版譜面互轉、SHT 檢查工具
│   ├── audio/                  VAG、hd/bd 音效庫、播放器
│   ├── graphics/               TIM2、曲名貼圖、SWG 精靈、逐格動畫
│   ├── ps2/                    PS2 硬碟（APA/PFS）、記憶卡、ISO、映像檔工具
│   └── exe/                    T14 以後的 taiko 執行檔修補
├── release/                   版本說明
└── docs/
    ├── ARCHITECTURE.md         模組對照與架構
    ├── FILE_FORMATS.md         逆向出的檔案格式
    ├── notecolor_report.md     逆向筆記：音符顏色編碼
    ├── unknown2_report.md      逆向筆記：LIST.BIN 群組雜湊
    ├── PFSSHELL_README.md      pfsshell 上游說明
    └── reviews/                內部設計與程式碼審查筆記
```

> `Font.ttf`、`T14LOAD.bin`、`pfsshell.exe`、`vgmstream-win64/` 和 `config.ini`
> 都由 `taiko2x6/app/apppaths.py` 統一在專案根目錄尋找（打包成 `.exe` 時則找 `.exe` 旁邊）。
> 把新的 `bineditor_*.py` 放進 `taiko2x6/bineditors/`，Explorer 就會自動載入。

---

## 授權

MIT，見 [`LICENSE`](LICENSE)。MIT 授權**只涵蓋本工具程式**，
不授予任何遊戲內容的權利，本工具也不附帶任何遊戲內容。
