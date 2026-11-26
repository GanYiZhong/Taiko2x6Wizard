"""Render the README screenshots from made-up demo data (no game content).

usage: python docs/screenshots/make_screenshots.py en docs/screenshots/en
       python docs/screenshots/make_screenshots.py zh-TW docs/screenshots/zh-TW

Settings, recent files and history go to a temp folder, so nothing of the
machine it runs on shows up in the pictures."""
import sys, tempfile
from pathlib import Path

LANG, OUT = sys.argv[1], Path(sys.argv[2])
OUT.mkdir(parents=True, exist_ok=True)
REPO = Path(__file__).resolve().parents[2]
sys.path[:0] = [str(REPO), str(REPO / "tests")]
TMP = Path(tempfile.mkdtemp())

from taiko2x6.app import appconfig, i18n
appconfig.INI_PATH = TMP / "config.ini"
i18n._lang = LANG
from taiko2x6 import history
history.history_path = lambda: TMP / "activity.jsonl"

from PySide6.QtCore import QSettings, Qt
from PySide6.QtWidgets import QApplication
_ini = str(TMP / "qs.ini")
def _fake(*a, **k):
    return QSettings(_ini, QSettings.IniFormat)
app = QApplication.instance() or QApplication([])

from taiko2x6.app import taiko256_explorer_gui6 as g
for name, mod in list(sys.modules.items()):
    if name.startswith("taiko2x6") and getattr(mod, "QSettings", None) is QSettings:
        mod.QSettings = _fake


def snap(w, name, size=None):
    if size:
        w.resize(*size)
    w.show()
    for _ in range(20):
        app.processEvents()
    w.grab().save(str(OUT / f"{name}.png"))
    print("saved", name, w.size().width(), w.size().height())


# 1. start page
w = g.MainWindow()
w.setup_box.hide(); w.btn_setup.hide()
snap(w, "start", (1200, 580))

# 2. explorer on a synthetic archive
from conftest import build_archive
from taiko2x6.charts import tja2sht
TJA = """TITLE:demo
BPM:160
OFFSET:0
COURSE:Oni
LEVEL:8
BALLOON:8

#START
1011201010112010,
1110222011102220,
3000100010201120,
1010200011102000,
#GOGOSTART
1120112011201120,
2210221022102210,
5000000000000008,
1212121211112222,
#GOGOEND
1021102110211021,
4000300040003000,
#END
"""
SHT = tja2sht.convert_tja(TJA, "oni")
groups = []
for i, sid in enumerate(["demo1", "demo2", "demo3", "demo4", "demo5", "demo6"]):
    groups.append((f"song.{sid}", [(f"{sid}_e.bin", bytes([1]) * 900),
                                   (f"{sid}_n.bin", bytes([2]) * 1200),
                                   (f"{sid}_h.bin", bytes([3]) * 1600),
                                   (f"{sid}_m.sht", SHT),
                                   (f"{sid}.vag", bytes(64) * 40)]))
groups.append(("gamedata", [("musicinfo.bin", bytes(4096)), ("tuning.bin", bytes(2048)),
                            ("streaminfo.bin", bytes(1024))]))
lb, db = build_archive(groups)
(TMP / "list.bin").write_bytes(lb)
(TMP / "DATA.000").write_bytes(db)
w._load(TMP / "list.bin", TMP / "DATA.000")
w.tree.expandAll()
w.tree.setColumnWidth(0, 230)
it = w._find_file_item(0, 3)
if it is not None:
    ix = w.proxy.mapFromSource(it.index())
    w.tree.setCurrentIndex(ix)
    w._on_tree_clicked(ix)
w.resize(1200, 760); w.show(); app.processEvents()
w.preview.setCurrentWidget(w.preview.chart_view) if hasattr(w, "preview") else None
snap(w, "explorer", (1200, 760))

# 3. dialogs
from taiko2x6.songs import omnimix_gui
d = omnimix_gui.OmnimixDialog(w, default_img="")
d.ed_timg.setText(r"D:\Taiko\TK14_omnimix.img")
for n in ("TK13.img", "TK12.img", "TK11.iso", "TK10.iso"):
    d.lst.addItem("D:\\Taiko\\" + n)
for k in ("skip_notice", "skip_copyright", "freeze_select_timer", "disable_diff_timer"):
    d.ck_boot[k].setChecked(True)
snap(d, "omnimix")
d.close()

from taiko2x6.charts import chart_convert
d = chart_convert.ChartConverterDialog(w)
for n in ("my_song.tja", "song_demo_e.bin", "song_demo_n.bin", "song_demo_h.bin", "song_demo_m.bin", "song_demo.nus3bank"):
    d.lst.addItem("D:\\charts\\" + n)
snap(d, "chart_converter")
d.close()

from taiko2x6.exe import taiko_exe
d = taiko_exe.TaikoBootDialog(w, default_img="")
d.ed_img.setText(r"D:\Taiko\TK14_omnimix.img")
for k, cb in d.cbs.items():
    cb.setEnabled(True)
    cb.setChecked(k in ("skip_notice", "skip_copyright", "freeze_select_timer", "category_folders"))
snap(d, "boot_patches")
d.close()

if w.archive:
    w.archive.close()
w.close()
