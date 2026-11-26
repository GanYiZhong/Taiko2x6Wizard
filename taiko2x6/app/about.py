"""Help > About and Help > Check for updates.

* ``build_info`` -- version label / commit / date of the running build. The
  release workflow writes ``taiko2x6/_build_info.py`` before packaging; from
  source it falls back to ``git rev-parse`` and finally to "source".
* ``check_latest`` -- one GET to the GitHub "latest release" API, only when the
  user clicks Check for updates. Nothing is downloaded or installed, and no
  user data is sent (a generic User-Agent only).
"""
from __future__ import annotations

import json
import platform
import re
import subprocess
import threading
import urllib.error
import urllib.request
from dataclasses import dataclass
from pathlib import Path

from PySide6.QtCore import Qt, QUrl, Signal
from PySide6.QtGui import QDesktopServices
from PySide6.QtWidgets import (QApplication, QDialog, QHBoxLayout, QLabel,
                               QPushButton, QVBoxLayout)

from taiko2x6.app import jobs
from taiko2x6.app.dialog_polish import polish
from taiko2x6.app.i18n import tr

REPO = "GanYiZhong/Taiko2x6Wizard"
REPO_URL = "https://github.com/" + REPO
ISSUES_URL = REPO_URL + "/issues"
RELEASES_URL = REPO_URL + "/releases"
API_LATEST = "https://api.github.com/repos/%s/releases/latest" % REPO
LICENSE_NAME = "MIT"
TIMEOUT = 8.0


# -- build info -------------------------------------------------------------
@dataclass
class BuildInfo:
    version: str          # "v1.2.2", "rolling" or "source"
    commit: str           # full or short hash, "" when unknown
    date: str             # "YYYY-MM-DD" or ""


def build_info() -> BuildInfo:
    try:
        from taiko2x6 import _build_info as b
        return BuildInfo(str(getattr(b, "VERSION", "") or "source"),
                         str(getattr(b, "COMMIT", "") or ""),
                         str(getattr(b, "DATE", "") or ""))
    except Exception:
        pass
    commit = ""
    try:
        root = Path(__file__).resolve().parents[2]
        r = subprocess.run(["git", "rev-parse", "HEAD"], cwd=root, timeout=3,
                           capture_output=True, text=True)
        if r.returncode == 0 and re.fullmatch(r"[0-9a-f]{7,40}", r.stdout.strip()):
            commit = r.stdout.strip()
    except Exception:
        pass
    return BuildInfo("source", commit, "")


def build_label(b: BuildInfo | None = None) -> str:
    b = b or build_info()
    s = b.version
    if b.commit:
        s += " (%s)" % b.commit[:7]
    if b.date:
        s += " " + b.date
    return s


def qt_versions() -> str:
    try:
        import PySide6
        from PySide6 import QtCore
        return "%s / Qt %s" % (PySide6.__version__, QtCore.qVersion())
    except Exception:
        return "?"


def info_text(b: BuildInfo | None = None) -> str:
    """Plain text for bug reports (kept in English on purpose)."""
    return "\n".join((
        "Taiko2x6Wizard %s" % build_label(b),
        "Python %s" % platform.python_version(),
        "PySide6 %s" % qt_versions(),
        "OS: %s" % platform.platform(),
        "Log folder: %s" % jobs.log_dir(),
    ))


# -- update check -----------------------------------------------------------
@dataclass
class UpdateResult:
    state: str            # "current" | "newer" | "unknown" | "error"
    name: str = ""
    date: str = ""
    url: str = RELEASES_URL
    error: str = ""       # "offline" | "rate" | "bad" when state == "error"


def fetch_latest(urlopen=None) -> dict:
    """GET the latest-release JSON. Raises urllib errors / ValueError."""
    urlopen = urlopen or urllib.request.urlopen
    req = urllib.request.Request(API_LATEST, headers={
        "User-Agent": "Taiko2x6Wizard", "Accept": "application/vnd.github+json"})
    with urlopen(req, timeout=TIMEOUT) as r:
        data = json.loads(r.read().decode("utf-8"))
    if not isinstance(data, dict) or "tag_name" not in data:
        raise ValueError("unexpected reply")
    return data


def _release_commit(rel: dict) -> str:
    c = str(rel.get("target_commitish") or "")
    if re.fullmatch(r"[0-9a-f]{7,40}", c):
        return c
    m = re.search(r"\(([0-9a-f]{7,40})\)", str(rel.get("name") or ""))
    return m.group(1) if m else ""


def compare(rel: dict, b: BuildInfo) -> UpdateResult:
    tag = str(rel.get("tag_name") or "")
    name = str(rel.get("name") or tag)
    date = str(rel.get("published_at") or "")[:10]
    url = str(rel.get("html_url") or RELEASES_URL)
    if not url.startswith("https://"):
        url = RELEASES_URL

    def mk(state):
        return UpdateResult(state, name, date, url)

    if b.version == "source":
        return mk("unknown")
    if tag == "rolling":
        rc = _release_commit(rel)
        if b.version == "rolling":
            if not (b.commit and rc):
                return mk("unknown")
            n = min(len(rc), len(b.commit))
            return mk("current" if rc[:n] == b.commit[:n] else "newer")
        return mk("newer")
    return mk("current" if tag == b.version else "newer")


def check_latest(urlopen=None, b: BuildInfo | None = None) -> UpdateResult:
    b = b or build_info()
    try:
        return compare(fetch_latest(urlopen), b)
    except urllib.error.HTTPError as e:
        return UpdateResult("error", error="rate" if e.code in (403, 429) else "offline")
    except (urllib.error.URLError, OSError, TimeoutError):
        return UpdateResult("error", error="offline")
    except Exception:
        return UpdateResult("error", error="bad")


def _open(url: str) -> None:
    QDesktopServices.openUrl(QUrl(url))


# -- dialogs ----------------------------------------------------------------
class AboutDialog(QDialog):
    def __init__(self, parent=None):
        super().__init__(parent)
        self.setWindowTitle(tr("About Taiko2x6Wizard"))
        b = build_info()
        lay = QVBoxLayout(self)
        title = QLabel("Taiko2x6Wizard")
        f = title.font()
        f.setBold(True)
        f.setPointSizeF(f.pointSizeF() * 1.5)
        title.setFont(f)
        lay.addWidget(title)
        lay.addWidget(QLabel(tr("Toolkit for Taiko no Tatsujin SYSTEM256 data.")))
        rows = (
            (tr("Version"), build_label(b)),
            ("Python", platform.python_version()),
            ("PySide6", qt_versions()),
            (tr("License"), LICENSE_NAME),
        )
        self.lbl_info = QLabel("<br>".join("<b>%s</b>: %s" % kv for kv in rows))
        self.lbl_info.setTextInteractionFlags(Qt.TextSelectableByMouse)
        lay.addWidget(self.lbl_info)
        self.lbl_links = QLabel(
            '<a href="%s">%s</a> | <a href="%s">%s</a> | <a href="%s">%s</a>' % (
                REPO_URL, tr("Repository"), ISSUES_URL, tr("Report an issue"),
                RELEASES_URL, tr("Releases")))
        self.lbl_links.setOpenExternalLinks(True)
        lay.addWidget(self.lbl_links)
        row = QHBoxLayout()
        self.b_copy = QPushButton(tr("Copy info"))
        self.b_copy.clicked.connect(self._copy)
        self.b_log = QPushButton(tr("Open log folder"))
        self.b_log.clicked.connect(jobs.open_log_folder)
        self.b_close = QPushButton(tr("Close"))
        self.b_close.clicked.connect(self.accept)
        row.addWidget(self.b_copy)
        row.addWidget(self.b_log)
        row.addStretch(1)
        row.addWidget(self.b_close)
        lay.addLayout(row)
        polish(self, self.b_close)

    def _copy(self):
        QApplication.clipboard().setText(info_text())
        self.b_copy.setText(tr("Copied"))


class UpdateDialog(QDialog):
    """Runs the check on a worker thread so the window never freezes."""
    done = Signal(object)

    def __init__(self, parent=None, urlopen=None, autostart=True):
        super().__init__(parent)
        self.setWindowTitle(tr("Check for updates"))
        self._urlopen = urlopen
        self.result: UpdateResult | None = None
        lay = QVBoxLayout(self)
        self.lbl = QLabel(tr("Checking for updates..."))
        self.lbl.setWordWrap(True)
        self.lbl.setTextInteractionFlags(Qt.TextSelectableByMouse)
        lay.addWidget(self.lbl)
        row = QHBoxLayout()
        self.b_open = QPushButton(tr("Open release page"))
        self.b_open.setEnabled(False)
        self.b_open.clicked.connect(
            lambda: _open(self.result.url if self.result else RELEASES_URL))
        self.b_close = QPushButton(tr("Close"))
        self.b_close.clicked.connect(self.accept)
        row.addWidget(self.b_open)
        row.addStretch(1)
        row.addWidget(self.b_close)
        lay.addLayout(row)
        polish(self, self.b_close)
        self.done.connect(self._show)
        if autostart:
            self.start()

    def start(self):
        threading.Thread(target=lambda: self.done.emit(check_latest(self._urlopen)),
                         daemon=True).start()

    def _show(self, r: UpdateResult):
        self.result = r
        what = "%s (%s)" % (r.name, r.date) if r.date else r.name
        if r.state == "current":
            msg = tr("You are up to date.") + "\n" + what
        elif r.state == "newer":
            msg = tr("A new build is available: {what}").format(what=what)
        elif r.state == "unknown":
            msg = tr("Latest release: {what}").format(what=what) + "\n" + tr(
                "This build cannot be compared with it.")
        else:
            msg = {"offline": tr("Could not reach GitHub. Check your connection."),
                   "rate": tr("GitHub is limiting requests right now. Try again later."),
                   }.get(r.error, tr("Unexpected reply from GitHub."))
        self.lbl.setText(msg)
        self.b_open.setEnabled(r.state != "error")
