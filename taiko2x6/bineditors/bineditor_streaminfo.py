#!/usr/bin/env python3
"""
streaminfo.bin editor — Taiko no Tatsujin SYSTEM256/PS2 streaming-audio table.

`streaminfo.bin` maps each streaming-audio slot (per-song background music and
UI streams) to an ASCII name plus a small set of numeric parameters. The file is
a little-endian count-prefixed record array followed by a packed string pool.

Reverse-engineered layout (offsets from file start), confirmed byte-exact:

  0x00  u32   count            number of records (105 in the sample)
  0x04  u32   record_size      stride note constant 12 (0x0C) in sample header
  0x08  u32   pool_offset      absolute offset of the string pool (0x840)
  0x0C  count * 20-byte records
            +0x00 u32  name_offset   offset into the string pool (rel pool start)
            +0x04 u32  field1        constant 16 in sample (group / type id)
            +0x08 u32  field2        varies 26..60 (likely volume / gain)
            +0x0C u32  field3        constant 55 in sample
            +0x10 u32  field4        constant 0  in sample
  pool  packed null-terminated ASCII names (e.g. "STR_BARCODE_BGM", "music_zelda")

The name pool is kept as raw bytes and edited in place within each name's byte
slot (capacity = distance to the next name's offset, or pool end). This makes
re-serialisation byte-exact: untouched files reproduce the original bytes, and
a name edit only rewrites its own slot (must fit, padded with NULs). Records are
plain integers; unchanged values reproduce verbatim.

Public interface:
    FILENAME
    parse(data) -> Model
    serialize(model) -> bytes      # serialize(parse(d)) == d
    Editor(QDialog)                # .result_bytes set on Save, else None
"""
from __future__ import annotations

import struct
from dataclasses import dataclass, field

FILENAME = "streaminfo.bin"

HEADER_FMT = "<3I"
HEADER_SIZE = 12
RECORD_FMT = "<5I"
RECORD_SIZE = 20
NUM_FIELDS = 5  # name_offset + 4 numeric fields


# --------------------------------------------------------------------------- #
# model
# --------------------------------------------------------------------------- #
@dataclass
class Record:
    name_offset: int          # offset into the pool (preserved verbatim)
    fields: list              # the 4 trailing u32 fields
    name: str                 # decoded ASCII name (its own pool slot)
    capacity: int             # writable bytes of this name's slot, capped at the
                              # real NUL terminator (name bytes + that one NUL).
                              # Excludes trailing pool padding so an edit cannot
                              # grow into / overwrite alignment bytes.


@dataclass
class Model:
    count: int
    record_size: int          # header field [1] (constant; preserved)
    pool_offset: int          # header field [2]
    records: list = field(default_factory=list)     # list[Record]
    raw_pool: bytearray = field(default_factory=bytearray)   # original pool bytes
    header_extra: bytes = b""  # any bytes between header end and first record
    pool_gap: bytes = b""      # any bytes between last record and pool_offset
    tail: bytes = b""          # any bytes after the pool (none in sample)


# --------------------------------------------------------------------------- #
# parse / serialize
# --------------------------------------------------------------------------- #
def parse(data: bytes) -> Model:
    if len(data) < HEADER_SIZE:
        raise ValueError("streaminfo.bin too small for header")
    count, record_size, pool_offset = struct.unpack_from(HEADER_FMT, data, 0)

    recs_start = HEADER_SIZE
    recs_end = recs_start + count * RECORD_SIZE
    if pool_offset < recs_end or pool_offset > len(data):
        raise ValueError(
            f"implausible pool_offset {pool_offset} "
            f"(records end {recs_end}, size {len(data)})"
        )

    # bytes between header and first record (none expected, kept for safety)
    header_extra = b""  # records immediately follow the 12-byte header

    raw_pool = bytearray(data[pool_offset:])

    # gather raw name offsets first so we can derive slot capacities
    raw = []
    for i in range(count):
        base = recs_start + i * RECORD_SIZE
        vals = struct.unpack_from(RECORD_FMT, data, base)
        raw.append(vals)

    # bytes between last record and pool start
    pool_gap = bytes(data[recs_end:pool_offset])

    records = []
    n_pool = len(raw_pool)
    # Highest end (terminator inclusive) actually used by any slot, so trailing
    # pool padding past the last real string can be preserved as a raw span and
    # excluded from any slot's writable capacity.
    max_used_end = 0
    for i, vals in enumerate(raw):
        name_off = vals[0]
        fields = list(vals[1:])
        if name_off < 0 or name_off > n_pool:
            raise ValueError(f"record {i}: bad name offset {name_off}")
        # The slot region runs until the next *strictly greater* record offset,
        # or the pool end. Within that region the writable capacity is capped at
        # the real NUL terminator (name bytes + that single NUL). This stops an
        # edit on the final name from overwriting trailing pool alignment.
        higher = [r[0] for r in raw if r[0] > name_off]
        region_end = min(higher) if higher else n_pool
        region = raw_pool[name_off:region_end]
        term = region.find(b"\x00")
        if term < 0:
            # No terminator before the next record / pool end: the whole region
            # is this slot's writable span (no trailing alignment to protect).
            cap = region_end - name_off
            name = region.decode("ascii", "replace")
        else:
            cap = term + 1               # name bytes + the terminating NUL
            name = region[:term].decode("ascii", "replace")
        records.append(Record(name_offset=name_off, fields=fields,
                              name=name, capacity=cap))
        max_used_end = max(max_used_end, name_off + cap)

    # Any bytes after the last used slot are pool alignment / padding; keep them
    # verbatim in the raw_pool (serialize re-emits raw_pool whole), so they are
    # preserved but never offered as editable capacity.
    return Model(count=count, record_size=record_size, pool_offset=pool_offset,
                 records=records, raw_pool=raw_pool,
                 header_extra=header_extra, pool_gap=pool_gap, tail=b"")


def _apply_name(model: Model, rec: Record, new_name: str) -> None:
    """Write a new name into its pool slot (in place, NUL-padded to capacity)."""
    try:
        b = new_name.encode("ascii")
    except UnicodeEncodeError as exc:
        raise ValueError(
            f"'{new_name}' contains non-ASCII characters not allowed in this "
            f"pool: {exc}") from exc
    if len(b) + 1 > rec.capacity:
        raise ValueError(
            f"'{new_name}' needs {len(b) + 1} bytes but slot holds {rec.capacity}"
        )
    o = rec.name_offset
    model.raw_pool[o:o + rec.capacity] = b + b"\x00" * (rec.capacity - len(b))
    rec.name = new_name


def serialize(model: Model) -> bytes:
    out = bytearray()
    out += struct.pack(HEADER_FMT, model.count, model.record_size,
                       model.pool_offset)
    out += model.header_extra
    for rec in model.records:
        out += struct.pack(RECORD_FMT, rec.name_offset, *rec.fields)
    out += model.pool_gap
    out += bytes(model.raw_pool)
    out += model.tail
    return bytes(out)


# --------------------------------------------------------------------------- #
# editor
# --------------------------------------------------------------------------- #
try:
    from PySide6.QtCore import Qt
    from PySide6.QtWidgets import (
        QDialog, QVBoxLayout, QHBoxLayout, QTableWidget, QTableWidgetItem,
        QPushButton, QLabel, QHeaderView, QMessageBox, QAbstractItemView,
    )
    _HAVE_QT = True
except Exception:  # pragma: no cover - Qt optional for headless import
    _HAVE_QT = False
    QDialog = object  # type: ignore

_FIELD_HEADERS = ["#", "name (editable)", "slot", "field1", "field2", "field3", "field4"]


class Editor(QDialog):
    """Modal editor for streaminfo.bin.

    After exec(), .result_bytes holds the new bytes if the user saved,
    otherwise None. Unchanged data round-trips byte-exact.
    """

    def __init__(self, data: bytes, title: str = "", parent=None):
        super().__init__(parent)
        self.setWindowTitle(f"streaminfo.bin — {title}" if title else "streaminfo.bin editor")
        self.resize(760, 620)
        self._orig = bytes(data)
        self.model = parse(data)
        self.result_bytes: bytes | None = None
        self._build_ui()

    def _build_ui(self):
        lay = QVBoxLayout(self)
        m = self.model
        lay.addWidget(QLabel(
            f"{m.count} records · pool @ 0x{m.pool_offset:X} · "
            f"record stride {RECORD_SIZE} bytes"
        ))

        self.tbl = QTableWidget(len(m.records), len(_FIELD_HEADERS))
        self.tbl.setHorizontalHeaderLabels(_FIELD_HEADERS)
        self.tbl.verticalHeader().setVisible(False)
        self.tbl.setEditTriggers(
            QAbstractItemView.DoubleClicked | QAbstractItemView.SelectedClicked
        )
        hh = self.tbl.horizontalHeader()
        hh.setSectionResizeMode(1, QHeaderView.Stretch)

        for r, rec in enumerate(m.records):
            # index (read-only)
            it = QTableWidgetItem(str(r))
            it.setFlags(it.flags() & ~Qt.ItemIsEditable)
            self.tbl.setItem(r, 0, it)
            # name (editable)
            self.tbl.setItem(r, 1, QTableWidgetItem(rec.name))
            # slot capacity (read-only, informative)
            cap = QTableWidgetItem(str(rec.capacity))
            cap.setFlags(cap.flags() & ~Qt.ItemIsEditable)
            self.tbl.setItem(r, 2, cap)
            # 4 numeric fields (editable)
            for c, val in enumerate(rec.fields):
                self.tbl.setItem(r, 3 + c, QTableWidgetItem(str(val)))

        lay.addWidget(self.tbl, 1)

        btns = QHBoxLayout()
        btns.addStretch(1)
        b_save = QPushButton("Save")
        b_save.clicked.connect(self._save)
        b_cancel = QPushButton("Cancel")
        b_cancel.clicked.connect(self.reject)
        btns.addWidget(b_save)
        btns.addWidget(b_cancel)
        lay.addLayout(btns)

    def _save(self):
        try:
            for r, rec in enumerate(self.model.records):
                # numeric fields
                new_fields = []
                for c in range(4):
                    txt = self.tbl.item(r, 3 + c).text().strip()
                    new_fields.append(int(txt, 0))
                rec.fields = new_fields
                # name (in-place into its slot)
                new_name = self.tbl.item(r, 1).text()
                if new_name != rec.name:
                    _apply_name(self.model, rec, new_name)
        except (ValueError, struct.error) as exc:
            QMessageBox.critical(self, "Edit error", str(exc))
            return
        self.result_bytes = serialize(self.model)
        self.accept()


# --------------------------------------------------------------------------- #
# self-test
# --------------------------------------------------------------------------- #
if __name__ == "__main__":
    import os
    import sys

    candidates = [
        os.path.join(os.path.dirname(__file__), FILENAME),
        r"C:\Users\User\AppData\Local\Temp\claude\D--"
        r"\ee6b327c-e139-4924-9604-a691103f5eaa\scratchpad\bins\streaminfo.bin",
    ]
    path = next((p for p in candidates if os.path.exists(p)), None)
    if path is None:
        print("streaminfo.bin not found; skipping self-test")
        sys.exit(0)

    data = open(path, "rb").read()
    model = parse(data)
    out = serialize(model)
    ok = out == data

    # exercise an in-place name edit of the same length to confirm slot logic
    edit_ok = True
    try:
        m2 = parse(data)
        if m2.records and len(m2.records[0].name) >= 1:
            old = m2.records[0].name
            _apply_name(m2, m2.records[0], old)  # same name, no-op rewrite
            edit_ok = serialize(m2) == data
    except Exception as exc:
        edit_ok = False
        print("edit-test error:", exc)

    names = [r.name for r in model.records]
    print(
        f"streaminfo.bin: {model.count} records, "
        f"pool@0x{model.pool_offset:X}, "
        f"e.g. {names[0]!r}..{names[-1]!r}; "
        f"roundtrip={'OK' if ok else 'FAIL'}, "
        f"editslot={'OK' if edit_ok else 'FAIL'} "
        f"=> {'PASS' if (ok and edit_ok) else 'FAIL'}"
    )
    sys.exit(0 if (ok and edit_ok) else 1)
