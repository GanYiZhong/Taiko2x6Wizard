"""Shared plumbing for "something went wrong" and "this takes a while".

* ``setup_file_logging`` -- keep a rotating log file the user can send us.
* ``install_excepthook`` -- an uncaught exception (any thread) is logged and
  shown in a friendly dialog (what failed, where the log is, Copy details)
  instead of the app vanishing silently.
* ``open_log_folder`` -- Help > Open log folder.
* ``fmt_elapsed`` / ``ElapsedLabel`` / ``attach_elapsed`` -- elapsed time for
  long jobs.
"""
from __future__ import annotations

import logging
import logging.handlers
import os
import platform
import sys
import threading
import time
import traceback
from pathlib import Path

from taiko2x6.app import apppaths
from taiko2x6.app.i18n import tr
from taiko2x6.app.dialog_polish import polish

LOG_NAME = "taiko.log"


# -- log file ---------------------------------------------------------------
def log_dir() -> Path:
    """Folder that holds the log: %LOCALAPPDATA%\\Taiko2x6Wizard\\logs, falling
    back to <resource dir>/logs when that cannot be created."""
    base = os.environ.get("LOCALAPPDATA")
    candidates = []
    if base:
        candidates.append(Path(base) / "Taiko2x6Wizard" / "logs")
    candidates.append(apppaths.resource_dir() / "logs")
    for d in candidates:
        try:
            d.mkdir(parents=True, exist_ok=True)
            return d
        except OSError:
            continue
    return candidates[-1]


def log_file() -> Path:
    return log_dir() / LOG_NAME


def setup_file_logging(logger: logging.Logger, level=logging.DEBUG) -> Path | None:
    """Attach a rotating file handler to `logger` (idempotent). Returns the
    log path, or None when no file could be opened (logging stays on console)."""
    path = log_file()
    for h in logger.handlers:
        if getattr(h, "_taiko_file", False):
            return path
    try:
        h = logging.handlers.RotatingFileHandler(
            path, maxBytes=1_000_000, backupCount=3, encoding="utf-8")
    except OSError:
        return None
    h._taiko_file = True                                    # type: ignore[attr-defined]
    h.setLevel(level)
    h.setFormatter(logging.Formatter(
        "%(asctime)s %(levelname)-7s %(name)s: %(message)s"))
    logger.addHandler(h)
    return path


def open_log_folder() -> bool:
    """Show the log folder in the file manager."""
    from PySide6.QtCore import QUrl
    from PySide6.QtGui import QDesktopServices
    return QDesktopServices.openUrl(QUrl.fromLocalFile(str(log_dir())))


# -- crash reporting --------------------------------------------------------
def format_report(exc_type, exc, tb, where: str = "") -> str:
    """Plain-text details for Copy details / the log."""
    try:
        from taiko2x6.app import appconfig  # noqa: F401
        head = "Taiko2x6Wizard\nPython %s, %s\n" % (
            platform.python_version(), platform.platform())
    except Exception:
        head = ""
    if where:
        head += "Thread: %s\n" % where
    head += "Log file: %s\n\n" % log_file()
    return head + "".join(traceback.format_exception(exc_type, exc, tb))


def _dialog_class():
    from PySide6.QtCore import Qt
    from PySide6.QtWidgets import (QApplication, QDialog, QHBoxLayout, QLabel,
                                   QPlainTextEdit, QPushButton, QVBoxLayout)

    class CrashDialog(QDialog):
        def __init__(self, summary: str, details: str, parent=None):
            super().__init__(parent)
            self.setWindowTitle(tr("Something went wrong"))
            self.resize(640, 420)
            self.details = details
            lay = QVBoxLayout(self)
            head = QLabel(tr(
                "An unexpected error stopped what you were doing. You can usually "
                "keep using the program, but if you were saving something, "
                "check that file before relying on it. If this keeps "
                "happening, please send the log."))
            head.setWordWrap(True)
            lay.addWidget(head)
            what = QLabel(tr("What failed: {err}").format(err=summary))
            what.setWordWrap(True)
            what.setTextInteractionFlags(Qt.TextSelectableByMouse)
            lay.addWidget(what)
            where = QLabel(tr("Log file: {path}").format(path=log_file()))
            where.setWordWrap(True)
            where.setStyleSheet("color:palette(mid);")
            lay.addWidget(where)
            self.view = QPlainTextEdit(details)
            self.view.setReadOnly(True)
            lay.addWidget(self.view, 1)
            row = QHBoxLayout()
            self.b_log = QPushButton(tr("Open log folder"))
            self.b_log.setAutoDefault(False)
            self.b_log.clicked.connect(open_log_folder)
            self.b_copy = QPushButton(tr("Copy details"))
            self.b_copy.setAutoDefault(False)
            self.b_copy.clicked.connect(self._copy)
            b_close = QPushButton(tr("Close"))
            b_close.setDefault(True)
            b_close.clicked.connect(self.accept)
            row.addWidget(self.b_log)
            row.addWidget(self.b_copy)
            row.addStretch(1)
            row.addWidget(b_close)
            lay.addLayout(row)
            polish(self)

        def _copy(self):
            QApplication.clipboard().setText(self.details)
            self.b_copy.setText(tr("Copied"))

    return CrashDialog


def show_crash_dialog(summary: str, details: str, parent=None) -> None:
    dlg = _dialog_class()(summary, details, parent)
    try:
        dlg.exec()
    finally:
        dlg.deleteLater()


_showing = False
_bridge = None


def _handle(exc_type, exc, tb, where: str = "") -> None:
    logging.getLogger("taiko").critical(
        "Unhandled exception (%s)", where or "main",
        exc_info=(exc_type, exc, tb))
    _bridge.report.emit(
        "%s: %s" % (exc_type.__name__, exc), format_report(exc_type, exc, tb, where))


def install_excepthook() -> None:
    """Route uncaught exceptions (main thread, Python threads and QThread
    run() bodies) to the log and a friendly dialog. Needs a QApplication."""
    global _bridge
    from PySide6.QtCore import QObject, Signal

    class _Bridge(QObject):
        report = Signal(str, str)

    _bridge = _Bridge()          # lives in the GUI thread: emits from other
    #                              threads are queued onto it.

    def show(summary, details):
        global _showing
        if _showing:             # a failure while the dialog is up: log only
            return
        _showing = True
        try:
            show_crash_dialog(summary, details)
        finally:
            _showing = False

    _bridge.report.connect(show)
    old = sys.excepthook

    def hook(exc_type, exc, tb):
        if issubclass(exc_type, KeyboardInterrupt):
            old(exc_type, exc, tb)
            return
        try:
            _handle(exc_type, exc, tb, threading.current_thread().name)
        except Exception:
            old(exc_type, exc, tb)

    def thread_hook(args):
        if args.exc_type is SystemExit:
            return
        hook(args.exc_type, args.exc_value, args.exc_traceback)

    sys.excepthook = hook
    threading.excepthook = thread_hook


# -- elapsed time -----------------------------------------------------------
def fmt_elapsed(seconds: float) -> str:
    """0:07, 12:30, 1:02:03."""
    s = max(0, int(seconds))
    h, rem = divmod(s, 3600)
    m, s = divmod(rem, 60)
    return "%d:%02d:%02d" % (h, m, s) if h else "%d:%02d" % (m, s)


def _label_class():
    from PySide6.QtCore import QTimer
    from PySide6.QtWidgets import QLabel

    class ElapsedLabel(QLabel):
        """"Elapsed 0:12" that ticks once a second between start() and stop()."""

        def __init__(self, parent=None):
            super().__init__(parent)
            self._t0 = None
            self._timer = QTimer(self)
            self._timer.setInterval(1000)
            self._timer.timeout.connect(self._tick)

        def start(self):
            self._t0 = time.monotonic()
            self._tick()
            self._timer.start()

        def stop(self):
            self._timer.stop()
            if self._t0 is not None:
                self._tick()

        def seconds(self) -> float:
            return 0.0 if self._t0 is None else time.monotonic() - self._t0

        def _tick(self):
            self.setText(tr("Elapsed {t}").format(t=fmt_elapsed(self.seconds())))

    return ElapsedLabel


def ElapsedLabel(parent=None):                              # noqa: N802
    return _label_class()(parent)


def attach_elapsed(progress, text: str):
    """Make a QProgressDialog's label read "<text>  (0:12)", updating each
    second. Returns the QTimer (it is parented to the dialog, which stops it
    when closed)."""
    from PySide6.QtCore import QTimer
    t0 = time.monotonic()
    timer = QTimer(progress)
    timer.setInterval(1000)

    def tick():
        progress.setLabelText("%s  (%s)" % (text, fmt_elapsed(time.monotonic() - t0)))

    timer.timeout.connect(tick)
    timer.start()
    return timer
