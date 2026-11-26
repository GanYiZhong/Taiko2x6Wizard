"""Drag and drop routing: detection on synthetic files, and the main window
sending each kind to the right (mocked) tool slot."""
import pytest

pytest.importorskip("PySide6")

from taiko2x6.app import drop_router  # noqa: E402


def _mk(path, size=0, head=b"", at=0):
    buf = bytearray(max(size, at + len(head)))
    buf[at:at + len(head)] = head
    path.write_bytes(bytes(buf))
    return path


def test_classify_archive_folder_and_files(tmp_path):
    d = tmp_path / "arc"
    d.mkdir()
    _mk(d / "DATA.000", 16)
    _mk(d / "list.bin", 16)
    assert drop_router.classify(d) == "archive"
    assert drop_router.classify(d / "DATA.000") == "archive"
    assert drop_router.classify(d / "list.bin") == "archive"


def test_classify_images_and_cards(tmp_path):
    assert drop_router.classify(_mk(tmp_path / "h.img", 2 << 20, b"APA\x00", 4)) == "hdd"
    assert drop_router.classify(_mk(tmp_path / "g.iso", 0x9000, b"CD001", 0x8001)) == "iso"
    assert drop_router.classify(
        _mk(tmp_path / "c.ps2", 8650752, b"Sony PS2 Memory Card Format ")) == "card"
    assert drop_router.classify(_mk(tmp_path / "c2.bin", 8388608)) == "card"
    assert drop_router.classify(_mk(tmp_path / "T14LOAD.bin", 64)) == "t14load"


def test_classify_charts_and_unknown(tmp_path):
    assert drop_router.classify(_mk(tmp_path / "a.tja", 4)) == "chart"
    assert drop_router.classify(_mk(tmp_path / "a.sht", 4)) == "chart"
    songs = tmp_path / "song"
    songs.mkdir()
    _mk(songs / "x.tja", 4)
    assert drop_router.classify(songs) == "chart"
    assert drop_router.classify(_mk(tmp_path / "x.txt", 4)) == "unknown"
    assert drop_router.classify(_mk(tmp_path / "junk.bin", 100)) == "unknown"
    empty = tmp_path / "empty"
    empty.mkdir()
    assert drop_router.classify(empty) == "unknown"
    assert drop_router.classify(tmp_path / "missing") == "unknown"


def test_candidates():
    keys = lambda k, a=False: [c[0] for c in drop_router.candidates(k, a)]  # noqa: E731
    assert keys("hdd")[0] == "hdd_browser" and "hdd_wizard" in keys("hdd")
    assert keys("iso") == ["omnimix_source"]
    assert keys("iso", True) == ["omnimix_source", "pack_iso"]
    assert keys("chart") == ["chart_convert"]
    assert keys("chart", True) == ["chart_convert", "song_builder"]
    assert keys("card") == ["ps2mc", "usb_keyboard_card"]
    assert keys("unknown") == []


def test_classify_all_charts_combine(tmp_path):
    a, b = _mk(tmp_path / "a.tja", 4), _mk(tmp_path / "b.tja", 4)
    assert drop_router.classify_all([a, b])[0] == "chart"
    assert len(drop_router.classify_all([a, b])[1]) == 2
    g = _mk(tmp_path / "g.iso", 0x9000, b"CD001", 0x8001)
    kind, paths = drop_router.classify_all([g, a])
    assert kind == "iso" and len(paths) == 1


@pytest.fixture
def win(tmp_path, monkeypatch):
    from PySide6.QtWidgets import QApplication
    from taiko2x6.app import appconfig
    monkeypatch.setattr(appconfig, "INI_PATH", tmp_path / "config.ini")
    QApplication.instance() or QApplication([])
    from taiko2x6.app import taiko256_explorer_gui6 as g
    w = g.MainWindow()
    yield w
    w.close()
    w.deleteLater()


def _spy(win, monkeypatch, name):
    calls = []
    monkeypatch.setattr(win, name, lambda paths: calls.append(list(paths)))
    return calls


def test_window_accepts_drops(win):
    assert win.acceptDrops()


def test_route_archive_and_t14load(win, tmp_path, monkeypatch):
    d = tmp_path / "arc"
    d.mkdir()
    _mk(d / "DATA.000", 16)
    _mk(d / "list.bin", 16)
    got = _spy(win, monkeypatch, "_drop_archive")
    win.route_drop([str(d)])
    assert got == [[str(d)]]
    t = _mk(tmp_path / "T14LOAD.bin", 32)
    win.route_drop([str(t)])
    from taiko2x6.app import appconfig
    assert appconfig.last_existing("t14load") == str(t)


def test_route_unknown_shows_message(win, tmp_path, monkeypatch):
    from PySide6.QtWidgets import QMessageBox
    shown = []
    monkeypatch.setattr(QMessageBox, "information",
                        lambda *a, **k: shown.append(a[2]))
    win.route_drop([str(_mk(tmp_path / "x.txt", 4))])
    assert shown and "x.txt" in shown[0]


def test_route_chart_uses_chooser_with_archive(win, tmp_path, monkeypatch):
    f = _mk(tmp_path / "a.tja", 4)
    win.archive = object()          # an archive is open: builder is offered
    seen = {}

    class Fake:
        chosen = "song_builder"

        def __init__(self, label, opts, parent=None):
            seen["keys"] = [o[0] for o in opts]

        def exec(self):
            return 1

        def deleteLater(self):
            pass

    monkeypatch.setattr(drop_router, "OpenWithDialog", Fake)
    got = _spy(win, monkeypatch, "_drop_song_builder")
    win.route_drop([str(f)])
    assert seen["keys"] == ["chart_convert", "song_builder"]
    assert got == [[str(f)]]
    win.archive = None


def test_route_chart_direct_to_converter(win, tmp_path, monkeypatch):
    f = _mk(tmp_path / "a.tja", 4)
    got = _spy(win, monkeypatch, "_drop_chart_convert")
    win.route_drop([str(f)])
    assert got == [[str(f)]]


def test_route_hdd_cancel_and_choice(win, tmp_path, monkeypatch):
    img = _mk(tmp_path / "h.img", 2 << 20, b"APA\x00", 4)
    state = {"ok": 0, "key": ""}

    class Fake:
        def __init__(self, *a, **k):
            pass

        @property
        def chosen(self):
            return state["key"]

        def exec(self):
            return state["ok"]

        def deleteLater(self):
            pass

    monkeypatch.setattr(drop_router, "OpenWithDialog", Fake)
    got = _spy(win, monkeypatch, "_drop_hdd_wizard")
    win.route_drop([str(img)])
    assert got == []                              # cancelled
    state.update(ok=1, key="hdd_wizard")
    win.route_drop([str(img)])
    assert got == [[str(img)]]


def test_hdd_drop_sets_image_for_tool(win, tmp_path, monkeypatch):
    img = _mk(tmp_path / "h.img", 2 << 20, b"APA\x00", 4)
    seen = []
    monkeypatch.setattr(win, "open_hdd_browser",
                        lambda: seen.append(win._recent_hdd_img()))
    win._drop_hdd_browser([str(img)])
    assert seen == [str(img)]
    assert win._img_override == ""


def test_open_with_dialog(win):
    from PySide6.QtWidgets import QDialog
    d = drop_router.OpenWithDialog("a.img", drop_router.candidates("hdd"))
    d.listing.setCurrentRow(1)
    d.accept()
    assert d.chosen == "hdd_wizard" and d.result() == QDialog.Accepted
    d.close()
    d.deleteLater()
