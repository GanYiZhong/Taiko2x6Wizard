#!/usr/bin/env python3
"""
Song-name texture generator for Taiko SYSTEM256 archives.

Each song has a `music_texture.kenri_song_<id>/nut` texture: a 640x160, 4-bit
indexed (16-colour, 32-bit RGBA palette) TIM2 holding the credits plate —
song title, 作詞 (lyricist), 作曲 (composer) and a © copyright line, rendered as
white text on transparency.

This module renders those four lines with a Japanese gothic font and encodes
them into a real song-name nut by splicing the pixels + palette into an existing
nut used as a template (so the header / GS registers stay game-valid). A small
Qt dialog provides live preview + fields.
"""
from __future__ import annotations

from pathlib import Path

import numpy as np
from PIL import Image, ImageDraw, ImageFont

import tim2

WIDTH, HEIGHT = 640, 160

# Japanese gothic fonts, best-match first (arcade plate uses an MS-Gothic-like face)
_JP_FONTS = [
    str(Path(__file__).resolve().parent / "Font.ttf"),  # authentic Taiko 勘亭流
    "C:/Windows/Fonts/msgothic.ttc",
    "C:/Windows/Fonts/YuGothB.ttc",
    "C:/Windows/Fonts/meiryo.ttc",
]


def _font(size: int, path: str | None = None) -> ImageFont.FreeTypeFont:
    size = max(6, int(size))
    for p in ([path] if path else []) + _JP_FONTS:
        if p and Path(p).exists():
            try:
                return ImageFont.truetype(p, size)
            except Exception:
                continue
    # No CJK-capable TrueType font found: the bitmap default renders Japanese
    # as blank tofu. Fail loudly so the caller knows the plate would be empty
    # rather than silently producing an unreadable texture.
    raise RuntimeError(
        "no CJK-capable font found (looked for Font.ttf and common Windows "
        "Japanese fonts); cannot render song-name text")


def _fit_font_to_width(text: str, base_size: int, max_w: int,
                       font_path: str | None = None,
                       min_size: int = 8) -> ImageFont.FreeTypeFont:
    """Largest size <= base_size whose rendered width fits max_w (>= min_size)."""
    probe = ImageDraw.Draw(Image.new("L", (8, 8)))
    size = max(min_size, int(base_size))
    while size > min_size:
        f = _font(size, font_path)
        l, _, r, _ = probe.textbbox((0, 0), text, font=f)
        if (r - l) <= max_w:
            return f
        size -= 1
    return _font(min_size, font_path)


def render_kenri_rgba(title: str, lyricist: str = "", composer: str = "",
                      copyright_text: str = "", font_path: str | None = None,
                      title_size: int = 30, sub_size: int = 23,
                      copyright: str | None = None) -> np.ndarray:
    """Render the 4-line credits plate to a 640x160 RGBA array (white on clear).

    Each line auto-shrinks to fit the plate width, and the line gap is reduced
    if the four lines would otherwise overflow the 160 px height, so no line is
    clipped. `copyright` is accepted as a legacy alias for `copyright_text`.
    """
    if copyright:
        copyright_text = copyright
    img = Image.new("RGBA", (WIDTH, HEIGHT), (0, 0, 0, 0))
    d = ImageDraw.Draw(img)

    x = 6
    avail_w = WIDTH - 2 * x
    specs = [(title, title_size)]
    if lyricist:
        specs.append((f"作詞：{lyricist}", sub_size))
    if composer:
        specs.append((f"作曲：{composer}", sub_size))
    if copyright_text:
        specs.append((copyright_text, sub_size))

    lines = [(text, _fit_font_to_width(text, size, avail_w, font_path))
             for text, size in specs if text]

    # Total height with a 4 px gap; shrink the gap (down to 0) if it overflows.
    line_h = [sum(f.getmetrics()) for _, f in lines]
    gap = 4
    while gap > 0 and 4 + sum(line_h) + gap * len(line_h) > HEIGHT:
        gap -= 1

    y = 4
    for (text, font), lh in zip(lines, line_h):
        if text:
            d.text((x, y), text, font=font, fill=(255, 255, 255, 255))
        y += lh + gap
    return np.asarray(img, dtype=np.uint8)


def rgba_to_indexed4_white(rgba: np.ndarray):
    """Map white-on-transparent RGBA to 16-level alpha ramp indices + palette.

    Index 0 = fully transparent; 1..15 = white at increasing alpha. Palette alpha
    is stored in PS2 range (0..128). Returns (indices HxW uint8, palette 16x4).
    """
    alpha = rgba[:, :, 3].astype(np.float32)
    idx = np.clip(np.round(alpha / 255.0 * 15.0), 0, 15).astype(np.uint8)
    pal = np.zeros((16, 4), np.uint8)
    for i in range(16):
        a8 = round(i / 15 * 255)
        ps2a = round(a8 / 255 * 128)            # PS2 alpha 0..128
        pal[i] = (255, 255, 255, ps2a)
    return idx, pal


def make_kenri_nut(template_nut: bytes, title: str, lyricist: str = "",
                   composer: str = "", copyright: str = "",
                   font_path: str | None = None) -> bytes:
    """Render the credits plate and encode it into a song-name .nut (TIM2)."""
    rgba = render_kenri_rgba(title, lyricist, composer, copyright, font_path)
    idx, pal = rgba_to_indexed4_white(rgba)
    return tim2.encode_indexed4_into_template(template_nut, idx, pal)


# --------------------------------------------------------------------------- #
#  Qt generator dialog
# --------------------------------------------------------------------------- #
try:
    from PySide6.QtCore import Qt, QRectF
    from PySide6.QtGui import QImage, QPixmap, QPainter, QColor
    from PySide6.QtWidgets import (
        QDialog, QVBoxLayout, QHBoxLayout, QFormLayout, QLineEdit, QSpinBox,
        QPushButton, QWidget, QLabel, QMessageBox,
    )

    class _Preview(QWidget):
        def __init__(self):
            super().__init__()
            self.setMinimumSize(WIDTH // 2, HEIGHT // 2 + 8)
            self._pix = None

        def set_nut(self, nut: bytes):
            w, h, rgba = tim2.decode_tim2(nut)[0]
            buf = np.ascontiguousarray(rgba, np.uint8).tobytes()
            self._pix = QPixmap.fromImage(QImage(buf, w, h, w * 4, QImage.Format_RGBA8888).copy())
            self.update()

        def paintEvent(self, _):
            p = QPainter(self)
            cb = 10
            for yy in range(self.height() // cb + 1):
                for xx in range(self.width() // cb + 1):
                    p.fillRect(xx * cb, yy * cb, cb, cb,
                               QColor(60, 60, 66) if (xx + yy) & 1 else QColor(48, 48, 54))
            if self._pix:
                s = min(self.width() / self._pix.width(), self.height() / self._pix.height())
                dw, dh = self._pix.width() * s, self._pix.height() * s
                p.drawPixmap(QRectF((self.width() - dw) / 2, (self.height() - dh) / 2, dw, dh),
                             self._pix, QRectF(self._pix.rect()))
            p.end()

    class SongTexDialog(QDialog):
        """Generate a song-name (kenri) texture. result_bytes set on Save."""

        def __init__(self, template_nut: bytes, title: str = "", lyricist: str = "",
                     composer: str = "", copyright: str = "", parent=None):
            super().__init__(parent)
            self.setWindowTitle("Song-name texture generator")
            self.resize(700, 420)
            self._template = template_nut
            self.result_bytes: bytes | None = None
            self._build_ui(title, lyricist, composer, copyright)
            self._refresh()

        def _build_ui(self, title, lyricist, composer, copyright):
            lay = QVBoxLayout(self)
            form = QFormLayout()
            self.ed_title = QLineEdit(title)
            self.ed_lyr = QLineEdit(lyricist)
            self.ed_comp = QLineEdit(composer)
            self.ed_copy = QLineEdit(copyright or "© 20XX")
            self.sp_tsize = QSpinBox(); self.sp_tsize.setRange(8, 64); self.sp_tsize.setValue(30)
            self.sp_ssize = QSpinBox(); self.sp_ssize.setRange(8, 64); self.sp_ssize.setValue(23)
            for w in (self.ed_title, self.ed_lyr, self.ed_comp, self.ed_copy):
                w.textChanged.connect(self._refresh)
            self.sp_tsize.valueChanged.connect(self._refresh)
            self.sp_ssize.valueChanged.connect(self._refresh)
            form.addRow("title 曲名:", self.ed_title)
            form.addRow("作詞 lyricist:", self.ed_lyr)
            form.addRow("作曲 composer:", self.ed_comp)
            form.addRow("© copyright:", self.ed_copy)
            sizes = QWidget(); sl = QHBoxLayout(sizes); sl.setContentsMargins(0, 0, 0, 0)
            sl.addWidget(QLabel("title pt")); sl.addWidget(self.sp_tsize)
            sl.addWidget(QLabel("sub pt")); sl.addWidget(self.sp_ssize)
            form.addRow("font size:", sizes)
            lay.addLayout(form)

            lay.addWidget(QLabel("preview (640×160, transparent):"))
            self.preview = _Preview()
            lay.addWidget(self.preview, 1)

            btns = QHBoxLayout(); btns.addStretch(1)
            b_save = QPushButton("Use this texture"); b_save.clicked.connect(self._save)
            b_cancel = QPushButton("Cancel"); b_cancel.clicked.connect(self.reject)
            btns.addWidget(b_save); btns.addWidget(b_cancel)
            lay.addLayout(btns)

        def _gen(self) -> bytes:
            rgba = render_kenri_rgba(
                self.ed_title.text(), self.ed_lyr.text(), self.ed_comp.text(),
                self.ed_copy.text(), title_size=self.sp_tsize.value(),
                sub_size=self.sp_ssize.value())
            idx, pal = rgba_to_indexed4_white(rgba)
            return tim2.encode_indexed4_into_template(self._template, idx, pal)

        def _refresh(self, *_):
            try:
                self.preview.set_nut(self._gen())
            except Exception:
                pass

        def _save(self):
            try:
                self.result_bytes = self._gen()
            except Exception as exc:
                QMessageBox.critical(self, "Generate failed", str(exc))
                return
            self.accept()

except ImportError:
    SongTexDialog = None  # type: ignore


if __name__ == "__main__":
    import sys
    # self-test: round-trip a real kenri nut as template, then render new text
    tpl_path = Path(r"C:\Users\User\AppData\Local\Temp\claude\D--\ee6b327c-e139-4924-9604-a691103f5eaa\scratchpad\template_kenri.nut")
    if tpl_path.exists():
        tpl = tpl_path.read_bytes()
        nut = make_kenri_nut(tpl, "テスト曲", "作曲者テスト", "作詞者テスト", "© 2024 TEST")
        lay = tim2.first_picture_layout(nut)
        ok = tim2.is_tim2(nut) and lay["width"] == 640 and lay["height"] == 160 and len(nut) == len(tpl)
        print(f"generated nut: {len(nut)}B same-size-as-template={len(nut)==len(tpl)} valid={ok} PASS" if ok else "FAIL")
        sys.exit(0 if ok else 1)
    print("no template available for self-test")
