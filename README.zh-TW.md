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
- **Omnimix 合併**：把 Taiko 8～14 多款遊戲的歌曲合併進同一個映像檔，
  自動去除重複歌曲、解除歌曲數量上限，必要時自動擴大分割區。
- **譜面轉換**：TJA → SHT，以及加密的**虹版（Gen3）** fumen → SHT
  （保留分歧譜面、正確處理變速）。
- **歌曲資料表編輯器**：`musicinfo`、`tuning`、`streaminfo`、`rank`、`lamp`、`fname`、
  `hdbdinfo`、`enso_parts`，Explorer 會自動找到並開啟對應的編輯器。
- **音訊**：VAG（PS2 ADPCM）編碼／解碼、匯入 OGG/WAV、播放 `hd/bd` 音效庫。
- **圖像**：TIM2 圖檔讀寫、用勘亭流字型產生曲名貼圖、編輯 SWG 精靈動畫、播放逐格動畫。
- **PS2 儲存裝置**：自行實作的 APA 分割區 + PFS 檔案系統讀寫、記憶卡讀寫、ISO 打包、
  多款遊戲合併到同一顆硬碟、映像檔瘦身。
- **執行檔修補**：解密並修補 T14 以後的 `taiko` ELF，例如提高歌曲數量上限、
  調整選歌倒數、跳過開機的 Notice 畫面。

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

- **Python 3.10 以上**。
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
python -m taiko2x6.exe.taiko_exe                   # T14 執行檔修補工具（GUI）
python -m taiko2x6.bineditors.bineditor_musicinfo  # 直接編輯 musicinfo.bin（GUI）
python -m taiko2x6.charts.gen3_convert --help      # 虹版譜面 → SHT（命令列）
```

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
- **合併多款遊戲（Omnimix）**：Tools → *Omnimix Maker*（`songs/omnimix_maker`）。
  214 首以內已驗證可正常遊玩；超過 214 首時資料能寫入，但實機能否全部顯示與遊玩尚未測試，
  請先在備份上試。

> ⚠️ **安全須知**：寫入 PS2 硬碟 `.img` 前，**請先關閉 PCSX2**，它會鎖住映像檔不讓寫入
> （只讀取的話不用關）。請一律在**副本**上操作並保留備份，有些操作無法復原。

---

## 專案結構

```
Taiko2x6Wizard/
├── README.md                  英文說明
├── README.zh-TW.md            本檔案
├── LICENSE                    MIT（僅涵蓋工具程式，不含遊戲內容）
├── requirements.txt           Python 套件需求
├── .gitignore                 排除遊戲資料與需自備的檔案
├── config.ini.example         路徑記憶的範本
├── main.py                    啟動程式，開啟 Explorer 主介面
├── Taiko2x6Wizard.spec        PyInstaller 打包設定（單一執行檔）
├── taiko2x6/                  工具主體套件
│   ├── app/                    Explorer 主介面、設定與資源路徑
│   ├── archive/                DATA.000 + list.bin 讀取與重建
│   ├── songs/                  歌曲新增／替換、資料庫管理、Omnimix
│   ├── bineditors/             各 .bin 資料表編輯器（自動偵測）
│   ├── charts/                 TJA／虹版 → SHT、SHT 檢查工具
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
