#!/usr/bin/env python3
"""
hdbdinfo.bin editor for Taiko no Tatsujin (SYSTEM256 / PS2-era arcade).

`hdbdinfo.bin` is the sound-bank info table ("HD/BD" = header/body sound data).
It maps named sound cues (COM_DON_C, GAME_BALOON_C, VO_TK8_915, ...) to playback
parameters (sound id, bank, volume, pan, ...).

Reverse-engineered layout (all little-endian):

  0x00  u32   record_count_a   (9)   -- number of CATEGORY names
  0x04  u32   record_count_b   (236) -- number of sound RECORDS
  0x08  u32   cat_table_off    (20)  -- file offset of the category pointer table
  0x0C  u32   records_off      (56)  -- file offset of the records array
  0x10  u32   pool_off         (15160) -- file offset of the ASCII string pool
  0x14  ...    category table   : record_count_a x u32 (pool-relative offsets)
  records_off  records array    : record_count_b x 64-byte records
  pool_off     string pool      : null-terminated ASCII names (categories + records)

Category table = 9 u32, each a pool-relative offset to a category name
(ATTRACT, COM, GAME, GAMEOVER, NAME, RESULT, RULE, SELECT, TOTAL).

Record (64 bytes), as 16 u32:
  [0] seq         sequential index (0..count-1)
  [1] name_ptr    pool-relative offset to this cue's name
  [2] sound_id    sound sample id
  [3] bank        bank / group id
  [4] volume      volume (0..~127)
  [5] pan         pan (0=left, 64=center, 127=right)
  [6] p6          (usually 60)
  [7] p7          (usually 100)
  [8..15] flags   mostly zero; occasional 1

Round-trip strategy: the file is kept as raw bytes. Only edited fields are
patched back in place, so serialize(parse(data)) == data byte-exact unless a
value is deliberately changed. Strings are edited in place within their pool
slot (must fit the original byte capacity, terminator included).
"""
from __future__ import annotations

import struct
from dataclasses import dataclass, field

FILENAME = "hdbdinfo.bin"

HEADER_FMT = "<5I"
HEADER_SIZE = 20
RECORD_SIZE = 64
RECORD_U32 = 16

# editable record field names (index into the 16 u32 of a record)
REC_FIELDS = [
    "seq", "name_ptr", "sound_id", "bank", "volume", "pan",
    "p6", "p7", "f8", "f9", "f10", "f11", "f12", "f13", "f14", "f15",
]
# fields the GUI exposes as plain editable integers (name_ptr handled via string col)
EDITABLE_INT_FIELDS = [2, 3, 4, 5, 6, 7, 8, 9, 10, 11, 12, 13, 14, 15]


@dataclass
class Record:
    offset: int            # absolute file offset of this 64-byte record
    values: list           # 16 ints (u32 each)


@dataclass
class PoolString:
    offset: int            # absolute file offset of the string bytes
    rel: int               # pool-relative offset
    text: str
    capacity: int          # writable bytes for this slot: name + terminator,
                           # plus a small bounded reserve of immediately-
                           # following NULs (never the whole padding run, so an
                           # edit cannot grow across an empty region into later
                           # structure).
    refcount: int = 1      # how many records' name_ptr resolve to this slot


@dataclass
class HdbdInfo:
    raw: bytearray
    count_a: int           # category count
    count_b: int           # record count
    cat_table_off: int
    records_off: int
    pool_off: int
    categories: list = field(default_factory=list)   # list[int] pool-rel offsets
    records: list = field(default_factory=list)       # list[Record]
    strings: list = field(default_factory=list)       # list[PoolString]

    # -- helpers ----------------------------------------------------------
    def name_at(self, rel: int) -> str:
        """Decode the pool string at a pool-relative offset."""
        i = self.pool_off + rel
        n = len(self.raw)
        j = i
        while j < n and self.raw[j] != 0:
            j += 1
        return self.raw[i:j].decode("latin1")

    def category_names(self) -> list:
        return [self.name_at(r) for r in self.categories]


def parse(data: bytes) -> HdbdInfo:
    raw = bytearray(data)
    if len(raw) < HEADER_SIZE:
        raise ValueError("file too small to be hdbdinfo.bin")
    count_a, count_b, cat_off, rec_off, pool_off = struct.unpack_from(HEADER_FMT, raw, 0)

    if not (0 < pool_off <= len(raw)):
        raise ValueError(f"implausible pool offset {pool_off}")
    if rec_off + count_b * RECORD_SIZE > pool_off:
        raise ValueError("records overrun the string pool")

    model = HdbdInfo(
        raw=raw, count_a=count_a, count_b=count_b,
        cat_table_off=cat_off, records_off=rec_off, pool_off=pool_off,
    )

    # category pointer table
    model.categories = list(struct.unpack_from("<%dI" % count_a, raw, cat_off))

    # records
    recs = []
    for r in range(count_b):
        off = rec_off + r * RECORD_SIZE
        vals = list(struct.unpack_from("<%dI" % RECORD_U32, raw, off))
        recs.append(Record(offset=off, values=vals))
    model.records = recs

    # string pool (tracked for in-place editing, with capacity)
    # A large run of NULs is padding/empty space, NOT free capacity for one
    # name; we only let an edit reuse a small bounded reserve of the NULs that
    # immediately follow this string's terminator.
    CAP_RESERVE = 4   # bytes of trailing NUL we treat as reusable per slot
    strings = []
    n = len(raw)
    i = pool_off
    while i < n:
        if raw[i] == 0:
            i += 1
            continue
        j = i
        while j < n and raw[j] != 0:
            j += 1
        text = raw[i:j].decode("latin1")
        # full NUL run after the string (to the next non-null or pool end)
        k = j
        while k < n and raw[k] == 0:
            k += 1
        full_run = k - j                  # number of trailing NULs
        # writable capacity: name bytes + terminator + bounded reserve, never
        # exceeding the bytes that physically exist before the next string.
        reserve = min(CAP_RESERVE, full_run)
        if reserve < 1:
            reserve = 1                   # always room for the terminator
        cap = (j - i) + reserve
        strings.append(PoolString(offset=i, rel=i - pool_off,
                                   text=text, capacity=cap))
        i = j
    model.strings = strings

    # count how many records share each pool slot via name_ptr, so the editor
    # can refuse per-record renames that would silently alias other records.
    rel_count: dict[int, int] = {}
    for rec in model.records:
        rel = rec.values[1]
        rel_count[rel] = rel_count.get(rel, 0) + 1
    for s in model.strings:
        s.refcount = rel_count.get(s.rel, 0)

    return model


def serialize(model: HdbdInfo) -> bytes:
    """Patch any tracked edits into the raw buffer and return bytes.

    Untouched models round-trip byte-exact because `raw` is the original buffer.
    """
    raw = model.raw
    # rewrite header
    struct.pack_into(HEADER_FMT, raw, 0,
                     model.count_a, model.count_b,
                     model.cat_table_off, model.records_off, model.pool_off)
    # category table
    struct.pack_into("<%dI" % model.count_a, raw, model.cat_table_off,
                     *model.categories)
    # records
    for rec in model.records:
        struct.pack_into("<%dI" % RECORD_U32, raw, rec.offset, *rec.values)
    return bytes(raw)


def set_record_field(model: HdbdInfo, rec_index: int, field_index: int, value: int):
    rec = model.records[rec_index]
    rec.values[field_index] = value & 0xFFFFFFFF


def set_string(model: HdbdInfo, str_offset: int, text: str):
    """Overwrite a pool string in place (must fit its byte capacity)."""
    s = next((x for x in model.strings if x.offset == str_offset), None)
    if s is None:
        raise ValueError(f"no string tracked at 0x{str_offset:X}")
    try:
        b = text.encode("latin1")
    except UnicodeEncodeError as exc:
        raise ValueError(
            f"'{text}' contains a character that cannot be stored in this "
            f"latin1 pool: {exc}") from exc
    if len(b) + 1 > s.capacity:
        raise ValueError(
            f"'{text}' needs {len(b)+1} bytes but slot holds {s.capacity}")
    model.raw[str_offset:str_offset + s.capacity] = b + b"\0" * (s.capacity - len(b))
    s.text = text


# ---------------------------------------------------------------------------
# GUI
# ---------------------------------------------------------------------------
try:
    from PySide6.QtCore import Qt
    from PySide6.QtWidgets import (
        QDialog, QVBoxLayout, QHBoxLayout, QTableWidget, QTableWidgetItem,
        QPushButton, QLabel, QTabWidget, QHeaderView, QMessageBox, QLineEdit,
    )
    _HAVE_QT = True
except Exception:  # pragma: no cover - Qt optional for headless import
    _HAVE_QT = False
    QDialog = object  # type: ignore


if _HAVE_QT:

    class Editor(QDialog):
        """Modal editor. After exec(), .result_bytes holds new bytes if saved."""

        # record columns shown in the grid (label, u32 field index)
        REC_COLUMNS = [
            ("idx", None),         # row index (read-only)
            ("name", 1),           # name string (special: edits pool string)
            ("category", None),    # derived from name prefix (read-only hint)
            ("sound_id", 2),
            ("bank", 3),
            ("volume", 4),
            ("pan", 5),
            ("p6", 6),
            ("p7", 7),
            ("f8", 8),
            ("f9", 9),
            ("f10", 10),
            ("f11", 11),
            ("f12", 12),
            ("f13", 13),
            ("f14", 14),
            ("f15", 15),
        ]

        def __init__(self, data: bytes, title: str = "", parent=None):
            super().__init__(parent)
            self.setWindowTitle(f"hdbdinfo.bin Editor — {title}" if title
                                else "hdbdinfo.bin Editor")
            self.resize(1100, 680)
            self.model = parse(data)
            self.result_bytes: bytes | None = None
            # map record -> its PoolString (by name_ptr) for editing names
            self._str_by_rel = {s.rel: s for s in self.model.strings}
            self._build_ui()

        def _build_ui(self):
            lay = QVBoxLayout(self)
            m = self.model
            cats = ", ".join(m.category_names())
            lay.addWidget(QLabel(
                f"records: {m.count_b}   strings: {len(m.strings)}   "
                f"pool@0x{m.pool_off:X}   categories ({m.count_a}): {cats}"))

            tabs = QTabWidget()

            # ---- records tab ----
            self.tbl = QTableWidget(len(m.records), len(self.REC_COLUMNS))
            self.tbl.setHorizontalHeaderLabels([c[0] for c in self.REC_COLUMNS])
            self.tbl.horizontalHeader().setSectionResizeMode(QHeaderView.ResizeToContents)
            self.tbl.horizontalHeader().setSectionResizeMode(1, QHeaderView.Stretch)
            for r, rec in enumerate(m.records):
                name = m.name_at(rec.values[1])
                cat = name.split("_", 1)[0] if "_" in name else name
                for c, (label, fidx) in enumerate(self.REC_COLUMNS):
                    if label == "idx":
                        it = QTableWidgetItem(str(r))
                        it.setFlags(it.flags() & ~Qt.ItemIsEditable)
                    elif label == "name":
                        it = QTableWidgetItem(name)
                    elif label == "category":
                        it = QTableWidgetItem(cat)
                        it.setFlags(it.flags() & ~Qt.ItemIsEditable)
                    else:
                        it = QTableWidgetItem(str(rec.values[fidx]))
                    self.tbl.setItem(r, c, it)
            tabs.addTab(self.tbl, f"Records ({len(m.records)})")

            # ---- raw string pool tab ----
            self.tbl_str = QTableWidget(len(m.strings), 3)
            self.tbl_str.setHorizontalHeaderLabels(["offset", "slot", "text (editable)"])
            self.tbl_str.horizontalHeader().setSectionResizeMode(2, QHeaderView.Stretch)
            for r, s in enumerate(m.strings):
                o = QTableWidgetItem(f"0x{s.offset:X}")
                o.setFlags(o.flags() & ~Qt.ItemIsEditable)
                cap = QTableWidgetItem(str(s.capacity))
                cap.setFlags(cap.flags() & ~Qt.ItemIsEditable)
                self.tbl_str.setItem(r, 0, o)
                self.tbl_str.setItem(r, 1, cap)
                self.tbl_str.setItem(r, 2, QTableWidgetItem(s.text))
            tabs.addTab(self.tbl_str, f"String pool ({len(m.strings)})")
            lay.addWidget(tabs, 1)

            # filter box for the records table
            flt = QHBoxLayout()
            flt.addWidget(QLabel("filter:"))
            self.ed_filter = QLineEdit()
            self.ed_filter.textChanged.connect(self._apply_filter)
            flt.addWidget(self.ed_filter)
            lay.addLayout(flt)

            btns = QHBoxLayout()
            b_save = QPushButton("Save"); b_save.clicked.connect(self._save)
            b_cancel = QPushButton("Cancel"); b_cancel.clicked.connect(self.reject)
            btns.addStretch(1); btns.addWidget(b_save); btns.addWidget(b_cancel)
            lay.addLayout(btns)

        def _apply_filter(self, text: str):
            text = text.lower()
            for r in range(self.tbl.rowCount()):
                name_item = self.tbl.item(r, 1)
                name = name_item.text().lower() if name_item else ""
                self.tbl.setRowHidden(r, bool(text) and text not in name)

        def _save(self):
            m = self.model
            try:
                # Resolve all name (pool-string) edits up front with a single
                # source of truth per slot. Precedence: a record-tab name edit
                # wins over the string-pool tab for the same slot (and the pool
                # tab is then synced). A rename of a slot that >1 record points
                # at is refused, since it would silently alias every sharer.
                #
                # slot offset -> (new_text, origin_label)
                pending: dict[int, tuple] = {}

                # (a) string-pool tab edits
                for r, s in enumerate(list(m.strings)):
                    new = self.tbl_str.item(r, 2).text()
                    if new != s.text:
                        pending[s.offset] = (new, f"string row {r}")

                # (b) record-tab name edits (override; enforce no shared rename)
                for r, rec in enumerate(m.records):
                    name_item = self.tbl.item(r, 1)
                    if name_item is None:
                        continue
                    new_name = name_item.text()
                    cur_name = m.name_at(rec.values[1])
                    if new_name == cur_name:
                        continue
                    s = self._str_by_rel.get(rec.values[1])
                    if s is None:
                        raise ValueError(
                            f"row {r}: cannot edit unknown name slot")
                    if s.refcount > 1:
                        raise ValueError(
                            f"row {r}: name slot 0x{s.offset:X} is shared by "
                            f"{s.refcount} records; renaming it here would "
                            f"rename all of them. Edit it in the String pool "
                            f"tab if that is intended.")
                    pending[s.offset] = (new_name, f"record row {r}")

                # (c) apply the reconciled name edits and sync the pool tab
                for off, (text, _origin) in pending.items():
                    set_string(m, off, text)
                    for sr, ss in enumerate(m.strings):
                        if ss.offset == off:
                            self.tbl_str.item(sr, 2).setText(text)
                            break

                # (d) integer record fields
                for r, rec in enumerate(m.records):
                    for c, (label, fidx) in enumerate(self.REC_COLUMNS):
                        if fidx is None or label == "name":
                            continue
                        cell = self.tbl.item(r, c)
                        if cell is None:
                            continue
                        txt = cell.text().strip()
                        try:
                            val = int(txt, 0)
                        except ValueError:
                            raise ValueError(
                                f"row {r} col '{label}': '{txt}' is not an integer")
                        if val != rec.values[fidx]:
                            set_record_field(m, r, fidx, val)
            except (ValueError, struct.error) as exc:
                QMessageBox.critical(self, "Edit error", str(exc))
                return
            self.result_bytes = serialize(m)
            self.accept()

else:  # pragma: no cover

    class Editor(QDialog):  # type: ignore
        def __init__(self, *a, **k):
            raise RuntimeError("PySide6 is required for the Editor GUI")


# ---------------------------------------------------------------------------
# self-test
# ---------------------------------------------------------------------------
if __name__ == "__main__":
    import os
    import sys

    candidates = [
        os.environ.get("HDBDINFO_BIN"),
        r"C:\Users\User\AppData\Local\Temp\claude\D--\ee6b327c-e139-4924-9604-a691103f5eaa\scratchpad\bins\hdbdinfo.bin",
        os.path.join(os.path.dirname(__file__), FILENAME),
        FILENAME,
    ]
    path = next((p for p in candidates if p and os.path.exists(p)), None)
    if path is None:
        print("FAIL: could not locate hdbdinfo.bin for self-test")
        sys.exit(1)

    data = open(path, "rb").read()
    model = parse(data)
    out = serialize(model)
    ok = out == data

    # extra check: a no-op string rewrite must also round-trip
    model2 = parse(data)
    if model2.strings:
        s0 = model2.strings[0]
        set_string(model2, s0.offset, s0.text)
    ok2 = serialize(model2) == data

    cats = ",".join(model.category_names())
    print(f"hdbdinfo.bin: {len(data)} bytes, {model.count_b} records "
          f"({RECORD_SIZE}B each), {len(model.strings)} pool strings, "
          f"{model.count_a} categories [{cats}], pool@0x{model.pool_off:X} "
          f"-- round-trip {'PASS' if ok and ok2 else 'FAIL'}")
    sys.exit(0 if (ok and ok2) else 1)
