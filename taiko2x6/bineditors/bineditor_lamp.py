#!/usr/bin/env python3
"""
Binary editor for `lamp.bin` — a Taiko no Tatsujin (SYSTEM256 / PS2-era arcade)
clear-lamp / game-state configuration table.

Reverse-engineered structure (all little-endian), verified byte-exact:

  off 0x000  u32   num_strings   (= 6)
  off 0x004  u32   num_units     (= 102)
  off 0x008        GROUP TABLE   : array of (start_unit:u32, count:u32) pairs.
                                   The array length is implicit — pairs are read
                                   until the running sum of `count` reaches
                                   num_units. For lamp.bin this is 36 pairs.
  ....             UNIT REGION   : num_units * 8 bytes. Each unit is two u32
                                   (a/b). Groups (above) carve this region into
                                   contiguous spans. Values look like
                                   (flag, parameter) tuples, often 0xffffffff
                                   sentinels (e.g. (1, 3000), (1, 1200, ...)).
  ....             STRING DESCS  : num_strings * (pool_offset:u32, value:u32).
                                   pool_offset indexes the string pool; value is
                                   a per-state small integer / lamp id.
  ....             STRING POOL    : null-terminated ASCII, runs to EOF.

For lamp.bin the 6 strings are Taiko game-state / lamp names:
  ATORAKUTO (attract), DENGENTOUNYUUJI (power-on), ENSOUGE_MU (play game),
  ENSOUGE_MUNORUMAKURIA (play-game normal clear), ENTORI_MACHI (entry wait),
  GE_MU (game).

Parsing is non-destructive: every byte lives in a structured field, and
serialize() reassembles them in original order, so serialize(parse(d)) == d
byte-for-byte. If anything fails to match the expected shape, parse() falls back
to keeping the whole file as a single raw blob (still round-trips exactly).
"""
from __future__ import annotations

import struct
from dataclasses import dataclass, field

FILENAME = "lamp.bin"


# --------------------------------------------------------------------------- #
#  Model
# --------------------------------------------------------------------------- #
@dataclass
class LampModel:
    # When `raw_fallback` is not None the file did not match the expected shape;
    # the whole file is stored verbatim and re-emitted unchanged.
    raw_fallback: bytes | None = None

    num_strings: int = 0
    num_units: int = 0
    groups: list = field(default_factory=list)   # list[(start_unit, count)]
    units: list = field(default_factory=list)     # list[(a, b)]  each 8 bytes
    descs: list = field(default_factory=list)     # list[(pool_off, value)]
    pool: bytes = b""                              # raw string-pool bytes

    # convenience: decoded string for each descriptor
    def string_at(self, pool_off: int) -> str:
        end = self.pool.find(b"\x00", pool_off)
        if end < 0:
            end = len(self.pool)
        return self.pool[pool_off:end].decode("latin1", "replace")


# --------------------------------------------------------------------------- #
#  parse / serialize
# --------------------------------------------------------------------------- #
def parse(data: bytes) -> LampModel:
    try:
        return _parse_strict(data)
    except Exception:
        return LampModel(raw_fallback=bytes(data))


def _parse_strict(data: bytes) -> LampModel:
    n = len(data)
    if n < 8:
        raise ValueError("too small")

    num_strings, num_units = struct.unpack_from("<II", data, 0)
    if num_strings > 4096 or num_units > 1_000_000:
        raise ValueError("implausible header")

    # --- group table: read (start, count) pairs until sum(count) == num_units
    groups = []
    off = 8
    total = 0
    while total < num_units:
        if off + 8 > n:
            raise ValueError("group table overruns file")
        start, count = struct.unpack_from("<II", data, off)
        groups.append((start, count))
        total += count
        off += 8
    if total != num_units:
        raise ValueError("group counts do not sum to num_units")

    # --- unit region: num_units * 8 bytes
    units = []
    for _ in range(num_units):
        if off + 8 > n:
            raise ValueError("unit region overruns file")
        a, b = struct.unpack_from("<II", data, off)
        units.append((a, b))
        off += 8

    # --- string descriptor table: num_strings * (pool_off, value)
    descs = []
    for _ in range(num_strings):
        if off + 8 > n:
            raise ValueError("desc table overruns file")
        poff, val = struct.unpack_from("<II", data, off)
        descs.append((poff, val))
        off += 8

    # --- string pool: remainder of file
    pool = data[off:]

    model = LampModel(
        raw_fallback=None,
        num_strings=num_strings,
        num_units=num_units,
        groups=groups,
        units=units,
        descs=descs,
        pool=pool,
    )

    # self-check: must reassemble identically
    if serialize(model) != data:
        raise ValueError("round-trip self-check failed during parse")
    return model


def serialize(model: LampModel) -> bytes:
    if model.raw_fallback is not None:
        return bytes(model.raw_fallback)

    out = bytearray()
    out += struct.pack("<II", model.num_strings, model.num_units)
    for start, count in model.groups:
        out += struct.pack("<II", start, count)
    for a, b in model.units:
        out += struct.pack("<II", a, b)
    for poff, val in model.descs:
        out += struct.pack("<II", poff, val)
    out += model.pool
    return bytes(out)


# --------------------------------------------------------------------------- #
#  Editor (PySide6)
# --------------------------------------------------------------------------- #
try:
    from PySide6.QtCore import Qt
    from PySide6.QtWidgets import (
        QDialog, QVBoxLayout, QHBoxLayout, QTabWidget, QTableWidget,
        QTableWidgetItem, QPushButton, QLabel, QHeaderView, QMessageBox, QWidget,
    )
    _HAVE_QT = True
except Exception:  # pragma: no cover - Qt optional for headless import
    _HAVE_QT = False
    QDialog = object  # type: ignore


def _u32(text: str) -> int:
    """Parse an int that may be decimal or 0x-hex; raise on out-of-range."""
    t = text.strip()
    v = int(t, 16) if t.lower().startswith("0x") else int(t)
    if not (0 <= v <= 0xFFFFFFFF):
        raise ValueError(f"value out of u32 range: {text}")
    return v


class Editor(QDialog):
    """
    Modal editor for lamp.bin. After exec(), `result_bytes` holds the new bytes
    if the user saved, else None. Unchanged edits round-trip to identical bytes.
    """

    def __init__(self, data: bytes, title: str = "", parent=None):
        super().__init__(parent)
        self.setWindowTitle(f"lamp.bin Editor — {title}" if title else "lamp.bin Editor")
        self.resize(820, 600)
        self._orig = bytes(data)
        self.model = parse(data)
        self.result_bytes: bytes | None = None

        self._build_ui()

    # -- ui ----------------------------------------------------------------- #
    def _build_ui(self):
        lay = QVBoxLayout(self)

        m = self.model
        if m.raw_fallback is not None:
            lay.addWidget(QLabel(
                "lamp.bin did not match the known structure; showing a raw hex "
                "view. Saving re-writes the original bytes unchanged."))
            hexv = QTableWidget(0, 1)
            hexv.setHorizontalHeaderLabels(["raw bytes (read-only)"])
            hexv.horizontalHeader().setSectionResizeMode(QHeaderView.Stretch)
            rows = [m.raw_fallback[i:i + 16].hex(" ")
                    for i in range(0, len(m.raw_fallback), 16)]
            hexv.setRowCount(len(rows))
            for r, h in enumerate(rows):
                it = QTableWidgetItem(h)
                it.setFlags(it.flags() & ~Qt.ItemIsEditable)
                hexv.setItem(r, 0, it)
            lay.addWidget(hexv)
            self._build_buttons(lay)
            return

        lay.addWidget(QLabel(
            f"strings: {m.num_strings}   units: {m.num_units}   "
            f"groups: {len(m.groups)}"))

        tabs = QTabWidget()

        # --- Strings tab (the meaningful editable content) ---
        self.tbl_str = QTableWidget(len(m.descs), 3)
        self.tbl_str.setHorizontalHeaderLabels(["pool offset", "value", "name (editable)"])
        self.tbl_str.horizontalHeader().setSectionResizeMode(2, QHeaderView.Stretch)
        for r, (poff, val) in enumerate(m.descs):
            io = QTableWidgetItem(str(poff))
            io.setFlags(io.flags() & ~Qt.ItemIsEditable)
            self.tbl_str.setItem(r, 0, io)
            self.tbl_str.setItem(r, 1, QTableWidgetItem(str(val)))
            self.tbl_str.setItem(r, 2, QTableWidgetItem(m.string_at(poff)))
        tabs.addTab(self.tbl_str, f"Strings ({len(m.descs)})")

        # --- Groups tab ---
        self.tbl_grp = QTableWidget(len(m.groups), 2)
        self.tbl_grp.setHorizontalHeaderLabels(["start unit", "count"])
        self.tbl_grp.horizontalHeader().setSectionResizeMode(QHeaderView.Stretch)
        for r, (s, c) in enumerate(m.groups):
            self.tbl_grp.setItem(r, 0, QTableWidgetItem(str(s)))
            self.tbl_grp.setItem(r, 1, QTableWidgetItem(str(c)))
        tabs.addTab(self.tbl_grp, f"Groups ({len(m.groups)})")

        # --- Units tab ---
        self.tbl_unit = QTableWidget(len(m.units), 2)
        self.tbl_unit.setHorizontalHeaderLabels(["a (u32)", "b (u32)"])
        self.tbl_unit.horizontalHeader().setSectionResizeMode(QHeaderView.Stretch)
        for r, (a, b) in enumerate(m.units):
            self.tbl_unit.setItem(r, 0, QTableWidgetItem(str(a)))
            self.tbl_unit.setItem(r, 1, QTableWidgetItem(str(b)))
        tabs.addTab(self.tbl_unit, f"Units ({len(m.units)})")

        lay.addWidget(tabs, 1)
        self._build_buttons(lay)

    def _build_buttons(self, lay):
        row = QWidget()
        btns = QHBoxLayout(row)
        btns.addStretch(1)
        b_save = QPushButton("Save")
        b_save.clicked.connect(self._save)
        b_cancel = QPushButton("Cancel")
        b_cancel.clicked.connect(self.reject)
        btns.addWidget(b_save)
        btns.addWidget(b_cancel)
        lay.addWidget(row)

    # -- save --------------------------------------------------------------- #
    def _save(self):
        m = self.model
        if m.raw_fallback is not None:
            self.result_bytes = self._orig
            self.accept()
            return

        try:
            # Rebuild descriptor values + string pool from the table.
            new_descs = []
            names = []
            for r in range(self.tbl_str.rowCount()):
                val = _u32(self.tbl_str.item(r, 1).text())
                name = self.tbl_str.item(r, 2).text()
                names.append(name.encode("latin1"))
                new_descs.append([None, val])  # pool offset filled below

            # Recompute the string pool. If the names are unchanged, this
            # reproduces the original pool byte-for-byte (offsets preserved).
            orig_names = [m.string_at(poff) for (poff, _v) in m.descs]
            unchanged = [n.decode("latin1") for n in names] == orig_names

            if unchanged:
                new_pool = m.pool
                for i, (poff, _v) in enumerate(m.descs):
                    new_descs[i][0] = poff
            else:
                # Determine any trailing pool bytes beyond the last referenced
                # string (alignment padding / unreferenced data). Preserve them
                # so a name edit cannot silently shrink the file.
                last_end = 0
                for (poff, _v) in m.descs:
                    end = m.pool.find(b"\x00", poff)
                    end = (len(m.pool) if end < 0 else end) + 1
                    if end > last_end:
                        last_end = end
                trailing = bytes(m.pool[last_end:])
                pool = bytearray()
                for i, nb in enumerate(names):
                    new_descs[i][0] = len(pool)
                    pool += nb + b"\x00"
                pool += trailing
                new_pool = bytes(pool)

            # Groups
            new_groups = []
            for r in range(self.tbl_grp.rowCount()):
                s = _u32(self.tbl_grp.item(r, 0).text())
                c = _u32(self.tbl_grp.item(r, 1).text())
                new_groups.append((s, c))

            # Units
            new_units = []
            for r in range(self.tbl_unit.rowCount()):
                a = _u32(self.tbl_unit.item(r, 0).text())
                b = _u32(self.tbl_unit.item(r, 1).text())
                new_units.append((a, b))

            new = LampModel(
                raw_fallback=None,
                num_strings=m.num_strings,
                num_units=m.num_units,
                groups=new_groups,
                units=new_units,
                descs=[(o, v) for (o, v) in new_descs],
                pool=new_pool,
            )
            self.result_bytes = serialize(new)
        except (ValueError, struct.error) as exc:
            QMessageBox.critical(self, "Edit error", str(exc))
            return

        self.accept()


# --------------------------------------------------------------------------- #
#  Self-test
# --------------------------------------------------------------------------- #
if __name__ == "__main__":
    import os

    path = os.environ.get("LAMP_BIN", os.path.join(os.path.dirname(__file__), "lamp.bin"))
    if not os.path.exists(path):
        path = (r"C:\Users\User\AppData\Local\Temp\claude\D--"
                r"\ee6b327c-e139-4924-9604-a691103f5eaa\scratchpad\bins\lamp.bin")

    with open(path, "rb") as f:
        data = f.read()

    model = parse(data)
    out = serialize(model)
    ok = out == data

    if model.raw_fallback is not None:
        shape = "RAW FALLBACK"
    else:
        names = [model.string_at(o) for (o, _v) in model.descs]
        shape = (f"strings={model.num_strings} units={model.num_units} "
                 f"groups={len(model.groups)} names={names}")

    print(f"lamp.bin ({len(data)} bytes): {shape}")
    print(f"round-trip serialize(parse(d))==d : {'PASS' if ok else 'FAIL'}")
    assert ok, "ROUND-TRIP FAILED"
