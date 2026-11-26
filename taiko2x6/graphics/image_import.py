"""Replace a texture (.nut / TIM2) with a PNG / JPG picked from disk.

``fit_rgba`` resizes a picture to the texture's size (stretch / fit with
transparent padding / crop); ``texture_target`` says what the template can
take; ``ImageReplaceDialog`` previews original | converted result side by
side. The dialog only produces ``result_bytes``; the caller stages them.
"""
from __future__ import annotations

import numpy as np

from taiko2x6.graphics import tim2
from taiko2x6.app.i18n import tr

FIT_MODES = ("stretch", "fit", "crop")
IMAGE_SUFFIXES = (".png", ".jpg", ".jpeg", ".bmp", ".gif", ".webp")


def load_rgba(path) -> np.ndarray:
    """Read any Pillow-readable picture as an HxWx4 uint8 array."""
    from PIL import Image, ImageOps
    with Image.open(path) as im:
        im = ImageOps.exif_transpose(im)
        return np.asarray(im.convert("RGBA"), dtype=np.uint8).copy()


def fit_rgba(rgba, width: int, height: int, mode: str = "stretch") -> np.ndarray:
    """Resize to exactly width x height.

    stretch: ignore the aspect ratio. fit: keep it, centre on a transparent
    canvas. crop: keep it, fill the canvas and cut the overflow (centred)."""
    from PIL import Image
    if mode not in FIT_MODES:
        raise ValueError(f"unknown fit mode {mode!r}")
    im = Image.fromarray(np.asarray(rgba, dtype=np.uint8), "RGBA")
    sw, sh = im.size
    if (sw, sh) == (width, height):
        return np.asarray(im, dtype=np.uint8).copy()
    if mode == "stretch":
        out = im.resize((width, height), Image.LANCZOS)
    else:
        scale = (min if mode == "fit" else max)(width / sw, height / sh)
        nw, nh = max(1, round(sw * scale)), max(1, round(sh * scale))
        scaled = im.resize((nw, nh), Image.LANCZOS)
        out = Image.new("RGBA", (width, height), (0, 0, 0, 0))
        out.paste(scaled, ((width - nw) // 2, (height - nh) // 2))
    return np.asarray(out, dtype=np.uint8).copy()


def texture_target(template: bytes) -> dict:
    """What the template needs: ``{"ok", "width", "height", "colors", "bits",
    "reason"}``. ``ok`` is False (with a ``reason``) for formats the encoder
    cannot write yet."""
    try:
        lay = tim2.first_picture_layout(template)
    except Exception as exc:
        return {"ok": False, "width": 0, "height": 0, "colors": 0, "bits": 0,
                "reason": tr("Not a readable TIM2 texture ({e}).").format(e=exc)}
    t = {"ok": True, "width": lay["width"], "height": lay["height"],
         "colors": 0, "bits": 0, "reason": ""}
    itype, w, h = lay["image_type"], lay["width"], lay["height"]
    direct = {1: (16, 32768), 2: (24, 1 << 24), 3: (32, 1 << 24)}
    if itype in (4, 5):
        t["bits"] = 4 if itype == 4 else 8
        bpp = tim2._clut_bpp(lay["clut_type"], lay["clut_size"], lay["clut_colors"])
        t["colors"] = min(16 if itype == 4 else 256, lay["clut_colors"])
        if lay["clut_colors"] <= 0 or lay["clut_size"] < lay["clut_colors"] * bpp:
            t["ok"] = False
            t["reason"] = tr("This texture's palette is incomplete; it cannot be re-encoded.")
    elif itype in direct:
        t["bits"], t["colors"] = direct[itype]
    else:
        t["ok"] = False
        t["reason"] = tr("This texture format (type {n}) is not supported; "
                         "only 4/8-bit indexed and 16/24/32-bit textures can be "
                         "replaced.").format(n=itype)
    if t["ok"]:
        need = {1: w * h * 2, 2: w * h * 3, 3: w * h * 4,
                4: (w * h + 1) // 2, 5: w * h}[itype]
        if lay["image_size"] < need or lay["img_start"] + lay["image_size"] > len(template):
            t["ok"] = False
            t["reason"] = tr("This texture's pixel data does not match its size; "
                             "it cannot be re-encoded.")
    return t


def convert(template: bytes, source_rgba, mode: str, dither: bool) -> tuple[bytes, np.ndarray]:
    """-> (new nut bytes, what the game will show as RGBA)."""
    t = texture_target(template)
    if not t["ok"]:
        raise ValueError(t["reason"])
    fitted = fit_rgba(source_rgba, t["width"], t["height"], mode)
    data = tim2.encode_rgba_into_template(template, fitted, dither=dither)
    return data, tim2.decode_tim2(data)[0][2]


try:
    from PySide6.QtGui import QImage, QPixmap
    from PySide6.QtWidgets import (QCheckBox, QComboBox, QDialog, QFormLayout,
                                   QHBoxLayout, QLabel, QLineEdit, QPushButton,
                                   QVBoxLayout)

    from taiko2x6.app import appconfig
    from taiko2x6.app.dialog_polish import polish
    from taiko2x6.graphics import imageview

    def rgba_to_pixmap(rgba) -> QPixmap:
        a = np.ascontiguousarray(rgba, dtype=np.uint8)
        h, w = a.shape[:2]
        return QPixmap.fromImage(QImage(a.tobytes(), w, h, w * 4,
                                        QImage.Format_RGBA8888).copy())

    class ImageReplaceDialog(QDialog):
        """Pick a picture, preview the converted texture, then OK.

        ``result_bytes`` holds the new .nut after accept()."""

        def __init__(self, template: bytes, name: str = "", parent=None,
                     path: str | None = None):
            super().__init__(parent)
            self.setWindowTitle(tr("Replace image from file"))
            self.setAcceptDrops(True)
            self.resize(900, 560)
            self._template = template
            self._source: np.ndarray | None = None
            self.result_bytes: bytes | None = None
            self._target = texture_target(template)
            self._build_ui(name)
            if path:
                self.set_path(path)
            else:
                self._refresh()
            polish(self, primary=self.b_ok)

        def _build_ui(self, name):
            t = self._target
            lay = QVBoxLayout(self)
            if t["ok"]:
                msg = tr("Target: {name} - {w} x {h} px, {bits}-bit, up to {c} colours "
                         "(PS2 alpha).").format(name=name, w=t["width"], h=t["height"],
                                                bits=t["bits"], c=t["colors"])
            else:
                msg = t["reason"]
            self.lbl_target = QLabel(msg)
            self.lbl_target.setWordWrap(True)
            lay.addWidget(self.lbl_target)

            form = QFormLayout()
            row = QHBoxLayout()
            self.ed_path = QLineEdit()
            self.ed_path.setPlaceholderText(tr("PNG / JPG file (or drop one here)"))
            self.ed_path.editingFinished.connect(
                lambda: self.set_path(self.ed_path.text().strip()))
            self.b_browse = QPushButton(tr("Browse…"))
            self.b_browse.clicked.connect(self._browse)
            row.addWidget(self.ed_path, 1)
            row.addWidget(self.b_browse)
            form.addRow(tr("Image file:"), row)
            self.cb_fit = QComboBox()
            self.cb_fit.addItem(tr("Stretch to the texture size"), "stretch")
            self.cb_fit.addItem(tr("Fit, pad with transparency"), "fit")
            self.cb_fit.addItem(tr("Crop to fill"), "crop")
            self.cb_fit.currentIndexChanged.connect(self._refresh)
            form.addRow(tr("Fit mode:"), self.cb_fit)
            self.chk_dither = QCheckBox(tr("Dither"))
            self.chk_dither.setToolTip(tr("Scatter the colour error; helps gradients and photos."))
            self.chk_dither.toggled.connect(self._refresh)
            form.addRow("", self.chk_dither)
            lay.addLayout(form)

            views = QHBoxLayout()
            self.view_src = imageview.ImageViewer(show_info=False)
            self.view_dst = imageview.ImageViewer(show_info=False)
            for title, v in ((tr("Original"), self.view_src),
                             (tr("Converted (what the game shows)"), self.view_dst)):
                col = QVBoxLayout()
                col.addWidget(QLabel(title))
                col.addWidget(v, 1)
                views.addLayout(col, 1)
            self.view_src.set_message(tr("(no image)"))
            self.view_dst.set_message(tr("(no image)"))
            lay.addLayout(views, 1)

            self.lbl_status = QLabel("")
            self.lbl_status.setWordWrap(True)
            lay.addWidget(self.lbl_status)

            btns = QHBoxLayout()
            btns.addStretch(1)
            self.b_ok = QPushButton(tr("Replace"))
            self.b_ok.clicked.connect(self.accept)
            b_cancel = QPushButton(tr("Cancel"))
            b_cancel.clicked.connect(self.reject)
            btns.addWidget(self.b_ok)
            btns.addWidget(b_cancel)
            lay.addLayout(btns)

        # -- input ---------------------------------------------------------
        def _browse(self):
            src = appconfig.pick_open(
                self, "image_import", tr("Choose an image"),
                "Images (*.png *.jpg *.jpeg *.bmp *.gif *.webp);;All files (*)")
            if src:
                self.set_path(src)

        def set_path(self, path: str):
            self.ed_path.setText(path)
            self._source = None
            if path:
                try:
                    self._source = load_rgba(path)
                except Exception as exc:
                    self._refresh(error=tr("Cannot read this image: {e}").format(e=exc))
                    return
            self._refresh()

        def dragEnterEvent(self, ev):
            if self._dropped(ev):
                ev.acceptProposedAction()

        dragMoveEvent = dragEnterEvent

        def dropEvent(self, ev):
            p = self._dropped(ev)
            if p:
                ev.acceptProposedAction()
                self.set_path(p)

        @staticmethod
        def _dropped(ev) -> str:
            for u in ev.mimeData().urls():
                if u.isLocalFile() and u.toLocalFile().lower().endswith(IMAGE_SUFFIXES):
                    return u.toLocalFile()
            return ""

        # -- conversion ----------------------------------------------------
        def _refresh(self, *_, error: str = ""):
            self.result_bytes = None
            ok = False
            if self._source is None:
                self.view_src.set_message(tr("(no image)"))
                self.view_dst.set_message(tr("(no image)"))
                self.lbl_status.setText(error or (
                    "" if self._target["ok"] else self._target["reason"]))
            else:
                self.view_src.set_pixmap(rgba_to_pixmap(self._source))
                if not self._target["ok"]:
                    self.view_dst.set_message(self._target["reason"])
                    self.lbl_status.setText(self._target["reason"])
                else:
                    try:
                        data, shown = convert(self._template, self._source,
                                              self.cb_fit.currentData(),
                                              self.chk_dither.isChecked())
                    except Exception as exc:
                        self.view_dst.set_message(str(exc))
                        self.lbl_status.setText(tr("Conversion failed: {e}").format(e=exc))
                    else:
                        self.result_bytes = data
                        ok = True
                        self.view_dst.set_pixmap(rgba_to_pixmap(shown))
                        px = shown.reshape(-1, 4)
                        n = len({tuple(p) for p in px[px[:, 3] > 0].tolist()})
                        self.lbl_status.setText(tr(
                            "Result: {w} x {h} px, {n} of {c} colours used, {b:,} bytes.").format(
                                w=shown.shape[1], h=shown.shape[0], n=n,
                                c=self._target["colors"], b=len(data)))
            self.b_ok.setEnabled(ok)

except ImportError:                                  # pragma: no cover
    ImageReplaceDialog = None  # type: ignore
