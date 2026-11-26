"""Activity history: a small persistent log of every write operation.

One JSON object per line in ``activity.jsonl`` (next to ``taiko.log``), capped
at ``CAP`` entries. An entry holds only paths and a one-line summary -- never
file contents::

    {"time": "2026-11-26 14:03:11", "tool": "Archive", "targets": ["D:/x/DATA.000"],
     "summary": "Saved 3 groups", "ok": true, "undo": "D:/x/DATA.000.bak",
     "error": ""}

Central write points (``safeio.write_pair``, ``ps2hdd.write_session``) open an
:func:`operation`; a tool that knows a better summary opens an outer one and
the inner writes merge their targets / undo path into it, so one user action is
one entry. Logging must never break a write: every failure here is swallowed.
No Qt in this module.
"""
from __future__ import annotations

import json
import os
import threading
import time
from contextlib import contextmanager
from pathlib import Path

NAME = "activity.jsonl"
CAP = 2000
_lock = threading.Lock()
_local = threading.local()


def history_path() -> Path:
    """%LOCALAPPDATA%\\Taiko2x6Wizard\\logs\\activity.jsonl (same folder as the
    log); tests redirect this function."""
    base = os.environ.get("LOCALAPPDATA")
    cands = []
    if base:
        cands.append(Path(base) / "Taiko2x6Wizard" / "logs")
    from taiko2x6.app import apppaths
    cands.append(apppaths.resource_dir() / "logs")
    for d in cands:
        try:
            d.mkdir(parents=True, exist_ok=True)
            return d / NAME
        except OSError:
            continue
    return cands[-1] / NAME


def record(tool: str, targets, summary: str, ok: bool = True,
           undo=None, error: str = "") -> None:
    """Append one entry (never raises)."""
    try:
        e = {"time": time.strftime("%Y-%m-%d %H:%M:%S"), "tool": str(tool),
             "targets": [str(t) for t in (targets or [])],
             "summary": str(summary), "ok": bool(ok),
             "undo": str(undo) if undo else "", "error": str(error)[:300]}
        line = json.dumps(e, ensure_ascii=False)
        with _lock:
            p = history_path()
            with open(p, "a", encoding="utf-8") as f:
                f.write(line + "\n")
            _trim(p)
    except Exception:
        pass


def _trim(p: Path) -> None:
    """Keep the newest CAP lines (rewrites only when over the cap)."""
    try:
        with open(p, "rb") as f:
            n = sum(1 for _ in f)
        if n <= CAP:
            return
        lines = p.read_text(encoding="utf-8", errors="replace").splitlines()
        tmp = p.with_name(p.name + ".tmp")
        tmp.write_text("\n".join(lines[-CAP:]) + "\n", encoding="utf-8")
        os.replace(tmp, p)
    except OSError:
        pass


def load(path=None) -> list[dict]:
    """All entries, newest first; unreadable lines are skipped."""
    p = Path(path) if path else history_path()
    out = []
    try:
        text = p.read_text(encoding="utf-8", errors="replace")
    except OSError:
        return out
    for ln in text.splitlines():
        try:
            e = json.loads(ln)
        except ValueError:
            continue
        if isinstance(e, dict) and "time" in e:
            out.append(e)
    out.reverse()
    return out


class _Child:
    """What a nested scope gets: it can add targets / undo to the outermost
    operation but not change its summary or suppress it."""

    def __init__(self, outer):
        self._outer = outer
        self.summary = ""
        self.skip = False

    def add(self, targets=(), undo=None):
        self._outer.add(targets, undo)


class Op:
    """The open operation of the current thread."""

    def __init__(self, tool, summary):
        self.tool, self.summary = tool, summary
        self.targets: list[str] = []
        self.undo = ""
        self.skip = False

    def add(self, targets=(), undo=None):
        for t in targets:
            t = str(t)
            if t not in self.targets:
                self.targets.append(t)
        if undo:
            self.undo = str(undo)


@contextmanager
def operation(tool: str, summary: str = "", targets=()):
    """Scope of one write operation. Nested scopes merge into the outermost
    one (its tool / summary are kept); only the outermost records. The yielded
    Op may be updated: ``op.summary = ...``, ``op.add(targets, undo=...)``,
    ``op.skip = True`` (nothing was written)."""
    outer = getattr(_local, "op", None)
    if outer is not None:
        outer.add(targets)
        yield _Child(outer)
        return
    op = Op(tool, summary)
    op.add(targets)
    _local.op = op
    try:
        yield op
    except BaseException as exc:
        _local.op = None
        record(op.tool, op.targets, op.summary, ok=False,
               error=f"{type(exc).__name__}: {exc}")
        raise
    else:
        _local.op = None
        if not op.skip:
            record(op.tool, op.targets, op.summary, ok=True, undo=op.undo)
