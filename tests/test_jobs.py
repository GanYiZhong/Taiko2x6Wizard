"""Crash dialog, log file, elapsed time and cancellable chart conversion."""
import logging
import sys

import pytest

pytest.importorskip("PySide6")

from taiko2x6.app import jobs  # noqa: E402


@pytest.fixture
def app():
    from PySide6.QtWidgets import QApplication
    return QApplication.instance() or QApplication([])


def test_fmt_elapsed():
    assert jobs.fmt_elapsed(7) == "0:07"
    assert jobs.fmt_elapsed(750) == "12:30"
    assert jobs.fmt_elapsed(3723) == "1:02:03"
    assert jobs.fmt_elapsed(-5) == "0:00"


def test_file_logging_writes_and_is_idempotent(tmp_path, monkeypatch):
    monkeypatch.setenv("LOCALAPPDATA", str(tmp_path))
    lg = logging.getLogger("taiko.test_jobs")
    p = jobs.setup_file_logging(lg)
    assert p == tmp_path / "Taiko2x6Wizard" / "logs" / "taiko.log"
    assert jobs.setup_file_logging(lg) == p
    assert len([h for h in lg.handlers if getattr(h, "_taiko_file", False)]) == 1
    lg.setLevel(logging.INFO)
    lg.info("hello log")
    for h in list(lg.handlers):
        h.flush()
        h.close()
        lg.removeHandler(h)
    assert "hello log" in p.read_text(encoding="utf-8")


def test_crash_dialog_has_buttons_and_copies(app):
    from PySide6.QtWidgets import QApplication
    d = jobs._dialog_class()("ValueError: boom", "Traceback...\nValueError: boom")
    try:
        assert d.b_log.text() and d.b_copy.text()
        d.b_copy.click()
        assert QApplication.clipboard().text().endswith("ValueError: boom")
    finally:
        d.close()
        d.deleteLater()


def test_excepthook_logs_and_shows_dialog(app, tmp_path, monkeypatch):
    monkeypatch.setenv("LOCALAPPDATA", str(tmp_path))
    shown = []
    monkeypatch.setattr(jobs, "show_crash_dialog",
                        lambda summary, details, parent=None: shown.append((summary, details)))
    old_hook, old_thook = sys.excepthook, __import__("threading").excepthook
    try:
        jobs.install_excepthook()
        try:
            raise RuntimeError("kaboom")
        except RuntimeError:
            sys.excepthook(*sys.exc_info())
        assert shown and "kaboom" in shown[0][0]
        assert "Traceback" in shown[0][1]
    finally:
        sys.excepthook = old_hook
        __import__("threading").excepthook = old_thook


def test_elapsed_label(app):
    lab = jobs.ElapsedLabel()
    try:
        lab.start()
        assert "0:0" in lab.text()
        lab.stop()
    finally:
        lab.deleteLater()


def test_convert_cancel_stops_before_first_song(tmp_path):
    from taiko2x6.charts import chart_convert
    lines = []
    songs, written, errors = chart_convert.convert(
        [str(tmp_path / "missing.tja")], str(tmp_path / "out"), {"tja"},
        log=lines.append, cancel=lambda: True)
    assert (songs, written) == ([], [])
    assert "cancelled" in lines
    assert not (tmp_path / "out").exists()


def test_help_menu_has_open_log(tmp_path, monkeypatch, app):
    from taiko2x6.app import appconfig
    monkeypatch.setattr(appconfig, "INI_PATH", tmp_path / "config.ini")
    from taiko2x6.app import taiko256_explorer_gui6 as g
    w = g.MainWindow()
    try:
        assert w.act_open_log.text()
    finally:
        w.close()
