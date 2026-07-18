#!/usr/bin/env python3
"""
Binary editor for ``rank.bin`` — a Taiko no Tatsujin (SYSTEM256 / PS2-era
arcade) rank / soul-gauge threshold table.

Empirically reverse-engineered layout (all little-endian u32), 208 bytes:

    offset  words  meaning
    0x00    [0]    count            = 10   (record / rank count field)
    0x04    [1]    stride/fields    = 4    (values per row)
    0x08    [2..7] reserved         = 0 0 0 0 0 0   (6 zero words)
    0x20    body   11 rows x 4 u32  = 44 words (176 bytes)

The body is a grid of 4-wide u32 rows. Values look like ascending integer
thresholds (50000, 100000, ...) interleaved with small per-row markers
(1, 2, 3, 4), consistent with rank / gauge threshold rows. The first two
header words (10, 4) describe the table shape but the on-disk body actually
holds 11 rows, so the model stores the header verbatim and the body as a
generic u32 grid. This keeps re-serialisation byte-exact regardless of the
exact semantic meaning of each cell.

Round-trip guarantee:  serialize(parse(data)) == data  (byte-exact), and any
trailing / odd-sized bytes are preserved in ``model.tail``.

Public interface:
    FILENAME : str
    parse(data: bytes) -> RankModel
    serialize(model: RankModel) -> bytes
    Editor(QDialog)  with  .result_bytes: bytes | None
"""
from __future__ import annotations

import struct
from dataclasses import dataclass, field

FILENAME = "rank.bin"

# ---- layout constants -------------------------------------------------------
HEADER_WORDS = 8          # 0x00..0x1F : count, stride, then 6 reserved zeros
ROW_WIDTH = 4             # u32 values per body row
WORD = 4


@dataclass
class RankModel:
    """Decoded rank.bin: header words + a grid of u32 rows + any leftover tail."""
    header: list           # list[int] of HEADER_WORDS u32 values
    rows: list             # list[list[int]] each of length ROW_WIDTH
    tail: bytes = b""      # any bytes that don't fit whole rows (normally empty)

    @property
    def count(self) -> int:
        return self.header[0] if self.header else 0

    @property
    def stride(self) -> int:
        return self.header[1] if len(self.header) > 1 else ROW_WIDTH


# ---- parse / serialize ------------------------------------------------------
def parse(data: bytes) -> RankModel:
    data = bytes(data)
    n = len(data)

    # header: up to HEADER_WORDS u32 (clamp if file is unexpectedly short)
    avail_words = n // WORD
    hwords = min(HEADER_WORDS, avail_words)
    header = list(struct.unpack_from("<%dI" % hwords, data, 0))

    body_off = hwords * WORD
    body = data[body_off:]

    # body as ROW_WIDTH-wide u32 rows; keep any remainder as tail
    row_bytes = ROW_WIDTH * WORD
    full_rows = len(body) // row_bytes
    rows = []
    for r in range(full_rows):
        off = body_off + r * row_bytes
        rows.append(list(struct.unpack_from("<%dI" % ROW_WIDTH, data, off)))
    tail = body[full_rows * row_bytes:]

    return RankModel(header=header, rows=rows, tail=tail)


def serialize(model: RankModel) -> bytes:
    out = bytearray()
    out += struct.pack("<%dI" % len(model.header), *model.header)
    for row in model.rows:
        out += struct.pack("<%dI" % len(row), *row)
    out += model.tail
    return bytes(out)


# ---- editor dialog ----------------------------------------------------------
try:
    from PySide6.QtCore import Qt
    from PySide6.QtWidgets import (
        QDialog, QVBoxLayout, QHBoxLayout, QFormLayout, QGroupBox, QLabel,
        QTableWidget, QTableWidgetItem, QPushButton, QHeaderView, QMessageBox,
    )
    _HAVE_QT = True
except Exception:  # pragma: no cover - Qt optional for headless import
    _HAVE_QT = False
    QDialog = object  # type: ignore


class Editor(QDialog):
    """Modal grid editor. After exec(), ``result_bytes`` holds new bytes if saved."""

    def __init__(self, data: bytes, title: str = "", parent=None):
        super().__init__(parent)
        self.setWindowTitle(f"rank.bin Editor — {title}" if title else "rank.bin Editor")
        self.resize(680, 560)
        self._orig = bytes(data)
        self.model = parse(data)
        self.result_bytes: bytes | None = None
        self._build_ui()

    def _build_ui(self):
        lay = QVBoxLayout(self)

        # --- header section ---
        gen = QGroupBox("Header")
        gf = QFormLayout(gen)
        self.tbl_hdr = QTableWidget(1, len(self.model.header))
        self.tbl_hdr.setHorizontalHeaderLabels(
            ["count", "stride"] + [f"rsvd{i}" for i in range(len(self.model.header) - 2)]
        )
        self.tbl_hdr.setVerticalHeaderLabels(["u32"])
        for c, v in enumerate(self.model.header):
            it = QTableWidgetItem(str(v))
            # count (col 0) and stride (col 1) are descriptive only: they do NOT
            # reshape the on-disk body (which holds 11 rows regardless). Mark
            # them read-only so editing them cannot mislead the user into
            # thinking the grid resizes. Reserved words stay editable.
            if c in (0, 1):
                it.setFlags(it.flags() & ~Qt.ItemIsEditable)
            self.tbl_hdr.setItem(0, c, it)
        self.tbl_hdr.horizontalHeader().setSectionResizeMode(QHeaderView.Stretch)
        self.tbl_hdr.setFixedHeight(72)
        gf.addRow(self.tbl_hdr)
        gf.addRow(QLabel(f"size: {len(self._orig)} bytes  |  "
                         f"body rows: {len(self.model.rows)} x {ROW_WIDTH} u32"
                         + (f"  |  tail: {len(self.model.tail)} bytes" if self.model.tail else "")))
        gf.addRow(QLabel("Note: 'count'/'stride' are descriptive header fields "
                         "(read-only); they do not reshape the body grid."))
        lay.addWidget(gen)

        # --- body grid ---
        body = QGroupBox(f"Body rows ({len(self.model.rows)} x {ROW_WIDTH} u32, little-endian)")
        bl = QVBoxLayout(body)
        self.tbl = QTableWidget(len(self.model.rows), ROW_WIDTH)
        self.tbl.setHorizontalHeaderLabels([f"col {c}" for c in range(ROW_WIDTH)])
        self.tbl.setVerticalHeaderLabels([f"row {r}" for r in range(len(self.model.rows))])
        for r, row in enumerate(self.model.rows):
            for c, v in enumerate(row):
                self.tbl.setItem(r, c, QTableWidgetItem(str(v)))
        self.tbl.horizontalHeader().setSectionResizeMode(QHeaderView.Stretch)
        bl.addWidget(self.tbl)
        lay.addWidget(body, 1)

        # --- buttons ---
        btns = QHBoxLayout()
        b_save = QPushButton("Save")
        b_save.clicked.connect(self._save)
        b_cancel = QPushButton("Cancel")
        b_cancel.clicked.connect(self.reject)
        btns.addStretch(1)
        btns.addWidget(b_save)
        btns.addWidget(b_cancel)
        lay.addLayout(btns)

    @staticmethod
    def _u32(text: str) -> int:
        v = int(str(text).strip(), 0)  # accept decimal or 0x-hex
        if not (0 <= v <= 0xFFFFFFFF):
            raise ValueError(f"value {v} out of u32 range")
        return v

    def _save(self):
        try:
            header = [self._u32(self.tbl_hdr.item(0, c).text())
                      for c in range(self.tbl_hdr.columnCount())]
            rows = []
            for r in range(self.tbl.rowCount()):
                rows.append([self._u32(self.tbl.item(r, c).text())
                             for c in range(self.tbl.columnCount())])
        except Exception as exc:
            QMessageBox.critical(self, "Edit error", str(exc))
            return
        new_model = RankModel(header=header, rows=rows, tail=self.model.tail)
        self.result_bytes = serialize(new_model)
        self.accept()


# ---- self-test --------------------------------------------------------------
if __name__ == "__main__":
    import os

    candidates = [
        os.path.join(os.path.dirname(os.path.abspath(__file__)), FILENAME),
        r"C:\Users\User\AppData\Local\Temp\claude\D--"
        r"\ee6b327c-e139-4924-9604-a691103f5eaa\scratchpad\bins\rank.bin",
    ]
    path = next((p for p in candidates if os.path.exists(p)), None)
    if path is None:
        # synthesize a representative blob if the real file isn't present
        import random
        random.seed(0)
        data = struct.pack("<2I", 10, 4) + b"\0" * 24
        data += b"".join(struct.pack("<4I", *[random.randint(0, 9_999_999) for _ in range(4)])
                          for _ in range(11))
        src = "synthetic"
    else:
        data = open(path, "rb").read()
        src = path

    model = parse(data)
    rt = serialize(model)
    ok = rt == data
    summary = (f"rank.bin [{os.path.basename(src) if path else src}] "
               f"size={len(data)}B header={model.header[:2]} "
               f"rows={len(model.rows)}x{ROW_WIDTH} tail={len(model.tail)}B")
    print(summary)
    print("PASS" if ok else "FAIL")
    assert ok, "round-trip mismatch: serialize(parse(data)) != data"
