"""Help > About and the update check (urlopen is always mocked)."""
import io
import json
import os
import urllib.error

import pytest

os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")
pytest.importorskip("PySide6")

from PySide6.QtWidgets import QApplication  # noqa: E402

from taiko2x6.app import about  # noqa: E402

SHA = "a" * 40


def _opener(payload=None, exc=None, seen=None):
    def op(req, timeout=None):
        if seen is not None:
            seen.append((req, timeout))
        if exc:
            raise exc
        return io.BytesIO(json.dumps(payload).encode())
    return op


def _rolling(sha):
    return {"tag_name": "rolling", "name": "Rolling build (%s)" % sha[:7],
            "published_at": "2026-05-01T10:00:00Z", "target_commitish": sha,
            "html_url": "https://github.com/x/y/releases/tag/rolling"}


def test_newer_and_same_rolling():
    seen = []
    b = about.BuildInfo("rolling", "b" * 40, "2026-04-01")
    r = about.check_latest(_opener(_rolling(SHA), seen=seen), b)
    assert r.state == "newer" and r.date == "2026-05-01" and "aaaaaaa" in r.name
    req, timeout = seen[0]
    assert timeout and timeout <= 10
    assert {k.lower() for k in req.headers} <= {"user-agent", "accept"}
    assert req.get_header("User-agent") == "Taiko2x6Wizard"
    assert about.check_latest(_opener(_rolling(SHA)),
                              about.BuildInfo("rolling", SHA[:7], "")).state == "current"


def test_version_tags_and_source():
    rel = {"tag_name": "v1.2.3", "name": "T v1.2.3", "html_url": "http://evil/x"}
    assert about.check_latest(_opener(rel), about.BuildInfo("v1.2.2", "", "")).state == "newer"
    r = about.check_latest(_opener(rel), about.BuildInfo("v1.2.3", "", ""))
    assert r.state == "current" and r.url == about.RELEASES_URL     # non-https url ignored
    assert about.check_latest(_opener(rel), about.BuildInfo("source", "", "")).state == "unknown"


@pytest.mark.parametrize("exc,err", [
    (urllib.error.URLError("no net"), "offline"),
    (TimeoutError(), "offline"),
    (urllib.error.HTTPError("u", 403, "rate", {}, None), "rate"),
    (urllib.error.HTTPError("u", 500, "boom", {}, None), "offline"),
])
def test_errors(exc, err):
    r = about.check_latest(_opener(exc=exc), about.BuildInfo("rolling", SHA, ""))
    assert r.state == "error" and r.error == err


def test_bad_json():
    r = about.check_latest(_opener(payload=["x"]), about.BuildInfo("rolling", SHA, ""))
    assert r.state == "error" and r.error == "bad"


def test_build_info_fallback():
    assert about.build_info().version
    assert "Python" in about.info_text() and "PySide6" in about.info_text()


def test_about_dialog_shows_versions():
    import platform
    QApplication.instance() or QApplication([])
    d = about.AboutDialog()
    text = d.lbl_info.text()
    assert platform.python_version() in text
    assert "Qt " in text
    d.b_copy.click()
    assert "Python" in QApplication.clipboard().text()


def test_update_dialog_shows_result():
    QApplication.instance() or QApplication([])
    d = about.UpdateDialog(autostart=False)
    d._show(about.UpdateResult("newer", "Rolling build (aaaaaaa)", "2026-05-01"))
    assert "Rolling build" in d.lbl.text() and d.b_open.isEnabled()
    d._show(about.UpdateResult("error", error="offline"))
    assert not d.b_open.isEnabled()
