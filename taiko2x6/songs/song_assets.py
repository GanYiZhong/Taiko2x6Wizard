"""A song's assets in DATA.000, read-only: its charts, audio stream and name
textures, with Preview / "Show in explorer" for each, and the reverse lookup
(archive file -> song id) used by the explorer's context menu.

The naming rules are NOT repeated here: charts come from
song_builder.find_charts, textures from song_builder.find_textures and the
audio stream / group-name parsing from omnimix_maker (HarvestIndex and its
prefixes)."""
from __future__ import annotations

import re
from dataclasses import dataclass

from PySide6.QtCore import Qt
from PySide6.QtWidgets import (QAbstractItemView, QDialog, QGroupBox, QHBoxLayout,
                               QHeaderView, QLabel, QPushButton, QTableWidget,
                               QTableWidgetItem, QVBoxLayout)

from taiko2x6.app.dialog_polish import polish
from taiko2x6.app.i18n import tr
from taiko2x6.songs import omnimix_maker as OM
from taiko2x6.songs import song_builder as SB

_DIFFS = ("e", "n", "h", "m")
_DIFF_NAMES = {"e": "Easy", "n": "Normal", "h": "Hard", "m": "Oni"}
_SELECT_ENTRY_RE = re.compile(r"^select_(?:full|short|non)_(?P<id>.+)$")


@dataclass
class Asset:
    kind: str          # "chart" | "audio" | "texture"
    label: str
    detail: str
    gi: int            # group index (the explorer tree's ROLE_GROUP)
    fi: int            # file index (ROLE_FILE)


# --------------------------------------------------------------------------- #
#  reverse lookup
# --------------------------------------------------------------------------- #
def song_id_of(group_name: str, entry_name: str = "") -> str | None:
    """The song id an archive file belongs to, or None. Uses omnimix_maker's
    own group-name rules; select_* plates live in shared music_select groups
    and are told apart by their entry name."""
    m = OM._FUMEN_RE.match(group_name)
    if m:
        return m.group("id")
    if group_name.startswith(OM._STREAM_PREFIX):
        return group_name[len(OM._STREAM_PREFIX):] or None
    for pre in OM._TEX_PREFIXES:
        if group_name.startswith(pre):
            return group_name[len(pre):] or None
    if group_name == OM._SELECT_GROUP or group_name.startswith(OM._SELECT_GROUP + "_"):
        m = _SELECT_ENTRY_RE.match(entry_name)
        if m:
            return m.group("id")
    return None


# --------------------------------------------------------------------------- #
#  locating
# --------------------------------------------------------------------------- #
def chart_hits(data: bytes) -> int | None:
    """Hit-note count of a .sht's first route, None if unreadable."""
    try:
        from taiko2x6.charts import chart_preview, sht_export, tja2sht
        model = tja2sht.parse_sht(data)
        _br, per_track = sht_export.routes(model)
        return sum(1 for t in per_track for n in t[0]
                   if n["type"] in chart_preview.HIT_TYPES)
    except Exception:
        return None


def locate(archive, sid: str, stars=None, index=None) -> list:
    """The song's Asset rows: charts (1P then 2P, easy..oni), audio, textures."""
    out = []
    order = {(p, d): i for i, (p, d) in
             enumerate((p, d) for p in ("1p", "2p") for d in _DIFFS)}
    charts = sorted(SB.find_charts(archive, sid), key=lambda c: order[(c[0], c[1])])
    for player, letter, grp, e in charts:
        di = _DIFFS.index(letter)
        hits = None
        try:
            hits = chart_hits(archive.read_file(grp, e))
        except Exception:
            pass
        bits = [tr("{n} notes").format(n=hits) if hits is not None
                else tr("(chart not readable)")]
        st = stars[di] if stars and di < len(stars) else None
        if st:
            bits.append(tr("{n} stars").format(n=st))
        out.append(Asset("chart", f"{player.upper()} {tr(_DIFF_NAMES[letter])}",
                         ", ".join(bits), grp["index"], e["index"]))
    index = index if index is not None else OM.HarvestIndex(archive)
    for grp in index.per_song.get(sid, ()):
        if grp["name"].startswith(OM._STREAM_PREFIX):
            for e in archive.layout.files_for_group(grp):
                out.append(Asset("audio", tr("Audio stream"),
                                 f"{grp['name']}/{e['name']} ({e['size']:,} B)",
                                 grp["index"], e["index"]))
    for tname, grp, e, _data in SB.find_textures(archive, sid):
        out.append(Asset("texture", tr("Texture: {t}").format(t=tname),
                         f"{grp['name']}/{e['name']}", grp["index"], e["index"]))
    return out


# --------------------------------------------------------------------------- #
#  preview window (reuses the explorer's PreviewPane)
# --------------------------------------------------------------------------- #
class AssetPreviewDialog(QDialog):
    NO_DEFAULT_REASON = "read-only viewer: Close only closes it"

    def __init__(self, archive, asset: Asset, parent=None):
        super().__init__(parent)
        from taiko2x6.app.taiko256_explorer_gui6 import PreviewPane
        self.setWindowTitle(tr("Preview: {n}").format(n=asset.label))
        self.setAttribute(Qt.WA_DeleteOnClose, True)
        self.resize(760, 520)
        lay = QVBoxLayout(self)
        self.pane = PreviewPane()
        lay.addWidget(self.pane, 1)
        btn = QPushButton(tr("Close"))
        btn.clicked.connect(self.close)
        row = QHBoxLayout()
        row.addStretch(1)
        row.addWidget(btn)
        lay.addLayout(row)
        grp = archive.layout.groups[asset.gi]
        entry = archive.layout.files[asset.fi]
        data = archive.read_file(grp, entry)
        self.pane.show_file(entry["name"], grp["name"], entry, data,
                            (asset.gi, asset.fi) in archive.replacements)
        tab = {"chart": self.pane.chart_view, "texture": self.pane._image_tab,
               "audio": self.pane._audio_holder}.get(asset.kind)
        if tab is not None:
            self.pane.setCurrentWidget(tab)
        polish(self)

    def done(self, code):
        self.pane._audio_stop()
        for w in list(self.pane._vag_workers):      # never destroy a running QThread
            w.wait()
        super().done(code)


# --------------------------------------------------------------------------- #
#  the panel
# --------------------------------------------------------------------------- #
class SongAssetsPanel(QGroupBox):
    """Lists the selected song's charts / audio / textures. ``host`` (the
    explorer window) provides ``reveal_file(gi, fi) -> bool``; without it the
    "Show in explorer" buttons are disabled."""

    def __init__(self, archive, host=None, parent=None):
        super().__init__(tr("Song assets"), parent)
        self.archive = archive
        self.host = host
        self.assets: list = []
        self._index = None
        self._previews: list = []
        lay = QVBoxLayout(self)
        self.lbl = QLabel(tr("Select a song to see its charts, audio and textures."))
        self.lbl.setWordWrap(True)
        lay.addWidget(self.lbl)
        self.tbl = QTableWidget(0, 4)
        self.tbl.setHorizontalHeaderLabels(
            [tr("Asset"), tr("Details"), "", ""])
        self.tbl.verticalHeader().setVisible(False)
        self.tbl.setEditTriggers(QAbstractItemView.NoEditTriggers)
        self.tbl.setSelectionMode(QAbstractItemView.NoSelection)
        self.tbl.horizontalHeader().setSectionResizeMode(1, QHeaderView.Stretch)
        self.tbl.setMaximumHeight(170)
        lay.addWidget(self.tbl)

    def set_song(self, sid, stars=None):
        self.assets = []
        if not sid:
            self.lbl.setText(tr("Select a song to see its charts, audio and textures."))
        else:
            if self._index is None:
                self._index = OM.HarvestIndex(self.archive)
            self.assets = locate(self.archive, sid, stars, self._index)
            self.lbl.setText(tr("Assets of {id}: {n} found").format(id=sid, n=len(self.assets)))
        self.tbl.setRowCount(len(self.assets))
        for r, a in enumerate(self.assets):
            self.tbl.setItem(r, 0, QTableWidgetItem(a.label))
            self.tbl.setItem(r, 1, QTableWidgetItem(a.detail))
            b1 = QPushButton(tr("Preview"))
            b1.clicked.connect(lambda _=False, r=r: self.preview_row(r))
            b2 = QPushButton(tr("Show in explorer"))
            b2.setEnabled(self.host is not None)
            b2.clicked.connect(lambda _=False, r=r: self.show_row(r))
            self.tbl.setCellWidget(r, 2, b1)
            self.tbl.setCellWidget(r, 3, b2)
        self.tbl.resizeColumnToContents(0)
        self.tbl.resizeColumnToContents(2)
        self.tbl.resizeColumnToContents(3)

    def preview_row(self, r: int):
        dlg = AssetPreviewDialog(self.archive, self.assets[r], self.window())
        self._previews.append(dlg)
        dlg.destroyed.connect(lambda *_, d=dlg: self._previews.remove(d)
                              if d in self._previews else None)
        dlg.show()
        return dlg

    def show_row(self, r: int) -> bool:
        a = self.assets[r]
        ok = bool(self.host and self.host.reveal_file(a.gi, a.fi))
        self.lbl.setText(tr("Selected in the explorer: {n}").format(n=a.detail) if ok
                         else tr("Could not find that file in the explorer."))
        return ok

    def close_previews(self):
        for d in list(self._previews):
            d.close()
