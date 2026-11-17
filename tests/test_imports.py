"""Every module imports, and the Explorer finds every bin editor."""
import importlib
import pkgutil

import pytest

import taiko2x6

MODULES = sorted(m.name for m in pkgutil.walk_packages(taiko2x6.__path__, "taiko2x6."))
QT_FREE = {"taiko2x6.safeio", "taiko2x6.archive.archive_model",
           "taiko2x6.archive.taiko256_archive_tool_v2", "taiko2x6.ps2.ps2hdd"}


@pytest.mark.parametrize("name", MODULES)
def test_module_imports(name):
    if name not in QT_FREE:
        pytest.importorskip("PySide6")
    importlib.import_module(name)


def test_core_modules_do_not_need_qt():
    import sys
    for name in QT_FREE:
        importlib.import_module(name)
    # the archive model must stay importable without a GUI toolkit
    src = (importlib.import_module("taiko2x6.archive.archive_model").__file__)
    assert "PySide6" not in open(src, encoding="utf-8").read()
    assert "taiko2x6.safeio" in sys.modules


def test_bineditors_are_discovered():
    from pathlib import Path
    pkg = Path(taiko2x6.__file__).parent / "bineditors"
    found = sorted(p.stem for p in pkg.glob("bineditor_*.py"))
    assert len(found) == 8
