#!/usr/bin/env python3
"""
Binary editor for `fname.bin` — the Taiko no Tatsujin (SYSTEM256 / PS2-era)
default NAME-ENTRY name pool.

Format (reverse-engineered empirically; little-endian throughout)
----------------------------------------------------------------
  0x00  u32   const  (observed 4)
  0x04  u32   const  (observed 4)
  0x08  u32   field  (observed 326 — NOT the slot count; kept raw)
  0x0C  ...    body: an array of fixed-width name slots

Each name slot is 10 bytes = 5 × u16 (UTF-16LE):
    [c0][c1][c2][c3][0x0000]
i.e. up to 4 UTF-16LE characters followed by a u16 0x0000 terminator.
Names shorter than 4 characters are right-padded with U+3000 (the
ideographic/full-width space) up to 4 characters before the terminator.

    e.g.  "たいこ"  -> 305f 3044 3053 3000 0000   (3 chars + 1 pad + null)
          "わだどん" -> 308f 3060 3069 3093 0000   (4 chars, no pad)

The 3112-byte sample contains a 12-byte header + 3100 body bytes = 310 slots
(3100 / 10), e.g. たいこ / なむこ / まつり / わだどん ...

Round-trip strategy
-------------------
`parse()` keeps the raw header bytes and, per slot, both the raw 10 bytes and
the decoded display name (decoded up to the 0x0000 terminator, then trailing
U+3000 padding stripped). `serialize()` re-emits the original raw bytes for any
slot whose display name is unchanged, so an untouched model is byte-identical to
the input. Edited slots are rebuilt (<=4 chars, U+3000-padded, 0x0000-
terminated). Any trailing bytes that do not form a complete 10-byte slot are
preserved verbatim as a raw tail.

Known limitation: a name that *intentionally* ends in U+3000 (full-width space)
displays without it because trailing U+3000 is indistinguishable from padding.
Such a slot round-trips byte-exact while unedited (serialize re-emits the raw
bytes), but re-typing it in the editor would drop the trailing space. This is
benign for the real name pool, which never ends a name in padding.
"""
from __future__ import annotations

import struct
from dataclasses import dataclass, field

FILENAME = "fname.bin"

HEADER_SIZE = 12          # three u32
SLOT_SIZE = 10            # 5 * u16
SLOT_CHARS = 4            # max name characters per slot
PAD_CHAR = "　"       # ideographic space used as padding


@dataclass
class NameSlot:
    raw: bytes            # original 10 bytes of this slot
    name: str             # decoded display name (trailing U+3000 padding stripped)


@dataclass
class FnameModel:
    header: bytes                       # raw 12-byte header
    slots: list = field(default_factory=list)   # list[NameSlot]
    tail: bytes = b""                   # any trailing bytes not forming a full slot


def _decode_slot(chunk: bytes) -> str:
    """Decode a 10-byte slot to a display name.

    The slot is up to ``SLOT_CHARS`` UTF-16LE code units followed by a 0x0000
    terminator, with trailing U+3000 padding before the terminator. We first
    truncate at the first 0x0000 code unit (so an embedded NUL in a malformed
    slot can never leak past the terminator and scramble re-encoding), then
    strip trailing U+3000 padding.
    """
    u = struct.unpack("<5H", chunk)
    chars = []
    for c in u[:SLOT_CHARS]:
        if c == 0x0000:           # terminator within the char region: stop
            break
        chars.append(chr(c))
    name = "".join(chars)
    # Strip trailing ideographic-space padding only (preserve any internal one).
    return name.rstrip(PAD_CHAR)


def _encode_slot(name: str) -> bytes:
    """Encode a display name into a 10-byte slot.

    Up to 4 chars, right-padded with U+3000 to 4 chars, then a 0x0000 u16.
    """
    chars = list(name)
    if len(chars) > SLOT_CHARS:
        raise ValueError(
            f"name {name!r} has {len(chars)} chars; max is {SLOT_CHARS}")
    code_units: list[int] = []
    for ch in chars:
        o = ord(ch)
        if o > 0xFFFF:
            raise ValueError(
                f"character {ch!r} (U+{o:04X}) is outside the BMP and cannot be "
                "stored in a single UTF-16LE code unit")
        code_units.append(o)
    while len(code_units) < SLOT_CHARS:
        code_units.append(ord(PAD_CHAR))
    code_units.append(0x0000)
    return struct.pack("<5H", *code_units)


def parse(data: bytes) -> FnameModel:
    if len(data) < HEADER_SIZE:
        raise ValueError("file too small to contain header")
    header = bytes(data[:HEADER_SIZE])
    body = data[HEADER_SIZE:]
    n_full = len(body) // SLOT_SIZE
    slots = []
    for k in range(n_full):
        chunk = bytes(body[k * SLOT_SIZE:(k + 1) * SLOT_SIZE])
        slots.append(NameSlot(raw=chunk, name=_decode_slot(chunk)))
    tail = bytes(body[n_full * SLOT_SIZE:])
    return FnameModel(header=header, slots=slots, tail=tail)


def serialize(model: FnameModel) -> bytes:
    out = bytearray(model.header)
    for slot in model.slots:
        if _decode_slot(slot.raw) == slot.name:
            # Unchanged: emit original bytes verbatim (byte-exact round-trip).
            out += slot.raw
        else:
            out += _encode_slot(slot.name)
    out += model.tail
    return bytes(out)


# ---------------------------------------------------------------------------
# GUI
# ---------------------------------------------------------------------------
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


class Editor(QDialog):
    """Modal editor for fname.bin. After exec(), `result_bytes` holds new bytes
    if the user saved, else None."""

    def __init__(self, data: bytes, title: str = "", parent=None):
        super().__init__(parent)
        self._original = bytes(data)
        self.model = parse(data)
        self.result_bytes: bytes | None = None
        self.setWindowTitle(f"fname.bin — name pool editor {('— ' + title) if title else ''}".strip())
        self.resize(560, 680)
        self._build_ui()

    def _build_ui(self):
        lay = QVBoxLayout(self)

        info = QLabel(
            f"{len(self.model.slots)} names · 4 chars max each "
            f"(padding {PAD_CHAR!r}=U+3000 added automatically)")
        info.setWordWrap(True)
        lay.addWidget(info)

        self.tbl = QTableWidget(len(self.model.slots), 2)
        self.tbl.setHorizontalHeaderLabels(["#", "name (editable, max 4 chars)"])
        self.tbl.horizontalHeader().setSectionResizeMode(0, QHeaderView.ResizeToContents)
        self.tbl.horizontalHeader().setSectionResizeMode(1, QHeaderView.Stretch)
        self.tbl.verticalHeader().setVisible(False)
        self.tbl.setSelectionBehavior(QAbstractItemView.SelectItems)
        for r, slot in enumerate(self.model.slots):
            idx = QTableWidgetItem(str(r))
            idx.setFlags(idx.flags() & ~Qt.ItemIsEditable)
            idx.setTextAlignment(Qt.AlignCenter)
            self.tbl.setItem(r, 0, idx)
            self.tbl.setItem(r, 1, QTableWidgetItem(slot.name))
        lay.addWidget(self.tbl, 1)

        btns = QHBoxLayout()
        b_save = QPushButton("Save")
        b_save.clicked.connect(self._save)
        b_cancel = QPushButton("Cancel")
        b_cancel.clicked.connect(self.reject)
        btns.addStretch(1)
        btns.addWidget(b_save)
        btns.addWidget(b_cancel)
        lay.addLayout(btns)

    def _save(self):
        # Validate and apply edits onto the model.
        try:
            for r, slot in enumerate(self.model.slots):
                item = self.tbl.item(r, 1)
                new = item.text() if item is not None else slot.name
                if len(new) > SLOT_CHARS:
                    raise ValueError(
                        f"row {r}: {new!r} has {len(new)} chars (max {SLOT_CHARS})")
                # Validate BMP-only (single UTF-16 code unit) by encoding.
                if new != slot.name:
                    _encode_slot(new)  # raises on invalid chars
                slot.name = new
            out = serialize(self.model)
        except (ValueError, struct.error) as exc:
            QMessageBox.critical(self, "Save error", str(exc))
            return
        self.result_bytes = out
        self.accept()


# ---------------------------------------------------------------------------
# Self-test
# ---------------------------------------------------------------------------
if __name__ == "__main__":
    import os

    path = os.environ.get(
        "FNAME_BIN",
        r"C:\Users\User\AppData\Local\Temp\claude\D--"
        r"\ee6b327c-e139-4924-9604-a691103f5eaa\scratchpad\bins\fname.bin",
    )
    with open(path, "rb") as fh:
        data = fh.read()

    model = parse(data)
    rt = serialize(model)
    ok = rt == data

    names = [s.name for s in model.slots]
    sample = " / ".join(names[:4])
    print(f"{len(names)} names, header={struct.unpack('<3I', model.header)}, "
          f"tail={len(model.tail)}B | e.g. {sample}")

    # Extra check: a length-changing edit still serializes to a valid slot and
    # leaves all other bytes untouched.
    if model.slots:
        m2 = parse(data)
        m2.slots[0].name = "ど"          # shorter than original
        out2 = serialize(m2)
        assert len(out2) == len(data), "length-changing edit altered file size"
        assert out2[:HEADER_SIZE] == data[:HEADER_SIZE], "header changed"
        assert out2[HEADER_SIZE + SLOT_SIZE:] == data[HEADER_SIZE + SLOT_SIZE:], \
            "edit leaked into other slots"
        assert parse(out2).slots[0].name == "ど", "edited name did not round-trip"

    print("PASS" if ok else "FAIL", "— serialize(parse(data)) == data")
    if not ok:
        raise SystemExit(1)
