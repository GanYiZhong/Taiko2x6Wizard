"""In-app help: the Help > Quick guide window and the small "?" help buttons.

The guide is one short page per workflow (what you need, the steps, what can go
wrong, where the backup/undo is). A page can carry buttons that open the tool it
talks about; each button names a ``MainWindow`` QAction (``act_*``), listed in
``page_actions()`` so a test can check they all exist.

``help_button`` makes the "?" button put next to a dialog option that needs more
than a tooltip; the same text is also set as the option's "What's this?" text.
"""
from __future__ import annotations

from html import escape

from PySide6.QtCore import Qt
from PySide6.QtWidgets import (QDialog, QHBoxLayout, QListWidget, QMessageBox,
                               QPushButton, QSplitter, QTextBrowser, QToolButton,
                               QVBoxLayout, QWidget)

from taiko2x6.app.dialog_polish import polish
from taiko2x6.app.i18n import tr


def help_button(parent: QWidget, title: str, text: str, *targets: QWidget) -> QToolButton:
    """A small "?" button that shows ``text`` in a message box.

    ``title``/``text`` must already be translated. The text is also set as the
    What's-this text of the button and of every widget in ``targets``."""
    b = QToolButton(parent)
    b.setText("?")
    b.setAutoRaise(True)
    b.setToolTip(tr("What does this do?"))
    b.setAccessibleName(tr("Help"))
    b.setWhatsThis(text)
    for w in targets:
        w.setWhatsThis(text)
    b.clicked.connect(lambda: QMessageBox.information(parent.window(), title, text))
    return b


# ------------------------------------------------------------------ the pages
def _pages() -> list[dict]:
    """Page data, translated for the current language. Keys: title, intro,
    needs, steps, wrong, undo (lists of str) and actions [(act_name, label)]."""
    return [
        {
            "title": tr("Open an archive and edit files"),
            "intro": tr("Browse the game's DATA.000 + list.bin, preview files and "
                        "replace them."),
            "needs": [tr("A folder holding DATA.000 and list.bin (extracted from "
                         "your own game image).")],
            "steps": [
                tr("File > Open, then pick that folder (recent folders are under "
                   "Open Recent)."),
                tr("Pick a file in the tree to preview it (textures, audio); the "
                   "per-song .bin tables have their own editors."),
                tr("Replace File… (or drop a file onto its row in the tree) stages "
                   "an edit. Staged edits are listed under Pending edits; Discard "
                   "selected edit(s) drops one of them."),
                tr("Save (Ctrl+S) writes DATA.000 and list.bin together."),
            ],
            "wrong": [
                tr("Save options: full rebuild is always on, so a replacement may "
                   "be any size and unchanged groups keep their sectors."),
                tr("Untick “overwrite originals” to write .new files next to the "
                   "originals instead."),
            ],
            "undo": [
                tr("Before Save: Discard Edits (Ctrl+Shift+Z) drops every staged edit."),
                tr("After Save: overwriting keeps a rolling .bak, .bak.1, … of both "
                   "files."),
                tr("If the app dies mid-save, the next open of the archive finishes "
                   "or undoes the save."),
            ],
            "actions": [("act_open", tr("Open archive…"))],
        },
        {
            "title": tr("Add or replace a song"),
            "intro": tr("Build a song from a TJA chart and a wav/ogg, or from a Gen3 "
                        "(Nijiiro) song folder."),
            "needs": [
                tr("An open archive."),
                tr("A .tja and a wav/ogg file, or a Nijiiro …/fumen/<id> folder."),
                tr("Font.ttf next to the program (the title texture is drawn with "
                   "it); a Gen3 song's audio also needs vgmstream-win64/."),
            ],
            "steps": [
                tr("Tools > Songs > Custom Song Builder. Tick “Add as NEW song” and "
                   "type a song id to create a new slot; leave it off to overwrite "
                   "the selected slot."),
                tr("Choose the source, fill in the title and credits, then Build → "
                   "stage. The files are only staged."),
                tr("Save in the main window writes them. Song Replacer overwrites an "
                   "existing slot and offers to write straight away."),
                tr("Song Manager edits the per-song database rows (names, stars, "
                   "ids) in one searchable table."),
            ],
            "wrong": [
                tr("A new song raises the song count. If it passes the game's "
                   "ceiling, raise it first with “Patch taiko song limit”."),
                tr("Leave “sync nudge” at 0 and feed the original .tja/.ogg; the "
                   "sync is computed from the TJA."),
            ],
            "undo": [
                tr("Nothing is written until you Save (Discard Edits drops the "
                   "staging); a new-song build asks before overwriting and keeps "
                   "a .bak."),
            ],
            "actions": [("act_songbuild", tr("Custom Song Builder…")),
                        ("act_songreplace", tr("Song Replacer…")),
                        ("act_songmgr", tr("Song Manager…"))],
        },
        {
            "title": tr("Convert charts"),
            "intro": tr("Convert between TJA, PS2-arcade .sht and Nijiiro (Gen3) "
                        "fumen .bin, in any direction."),
            "needs": [tr("The chart files to convert; nothing needs to be open."),
                      tr("For a Gen3 song_<id>.nus3bank: vgmstream-win64/.")],
            "steps": [
                tr("Tools > Charts & audio > Chart converter."),
                tr("Add files…, tick the output format(s) under Write, choose the Output folder, then Convert."),
                tr("Check a result with SHT Validator; VAG Generator turns a "
                   "wav/ogg into the game's VAG audio."),
            ],
            "wrong": [tr("Branches are kept, but read the converter's log for "
                         "warnings before using the output.")],
            "undo": [tr("Converters write new files to the folder you choose and "
                        "do not touch your game files.")],
            "actions": [("act_chartconv", tr("Chart converter…")),
                        ("act_shtval", tr("SHT Validator…")),
                        ("act_vaggen", tr("VAG Generator…"))],
        },
        {
            "title": tr("Build an Omnimix"),
            "intro": tr("Fuse the songs of several Taiko images (7 to 14) into one "
                        "target image."),
            "needs": [
                tr("A target PS2 HDD .img (Taiko 14+) or a DATA.000 + list.bin "
                   "folder, and at least one source (.img, ISO or folder)."),
                tr("Close PCSX2 first and work on a copy: the .img is written in "
                   "place."),
            ],
            "steps": [
                tr("Tools > Songs > Omnimix Maker. Set the target, then add "
                   "sources; order them with Up / Down (first wins on ties)."),
                tr("Pick the options, then Preview plan to see what would be added."),
                tr("Build Omnimix; confirm the write. It rewrites the whole archive "
                   "and can take a while."),
                tr("Repair existing Omnimix fixes an Omnimix made by an older "
                   "version without rebuilding it."),
            ],
            "wrong": [
                tr("DATA.000 must stay under 2 GiB or the game hangs on LOADING: "
                   "use the 30 kHz or 24 kHz audio option for ~430+ songs."),
                tr("Above 214 songs the song tables are grown; records of songs "
                   "past 210 are not kept across a reboot."),
            ],
            "undo": [
                tr("“back up the target .img” copies it to <img>.omnimix.bak first."),
                tr("With it off, the write is journaled instead: <img>.<date-time>."
                   "undo (python -m taiko2x6.safeio rollback <file>)."),
            ],
            "actions": [("act_omnimix", tr("Omnimix Maker…"))],
        },
        {
            "title": tr("Patch the game executable"),
            "intro": tr("Edit the `taiko` program on a Taiko 14+ HDD image: song "
                        "limit, select timer, boot patches, category folders."),
            "needs": [tr("A PS2 HDD .img of Taiko 14 or later."),
                      tr("T14LOAD.bin next to the program (it holds the key to "
                         "decrypt `taiko`)."),
                      tr("PCSX2 closed: it locks the .img.")],
            "steps": [
                tr("Tools > taiko executable patches > Patch taiko song limit: set "
                   "the ceiling (stock 210; up to 214 is the proven path), Check, "
                   "then Patch."),
                tr("Song-select timer sets the countdown (stock 120 s) to 1-999 s."),
                tr("Boot & select patches: tick the switches you want, Apply. "
                   "Unticking restores the stock code. “Song select as category "
                   "folders” shows only category cards; don opens one."),
            ],
            "wrong": [
                tr("Omnimix Maker patches the ceiling for you; patch by hand only "
                   "for songs added another way."),
                tr("A big ceiling can glitch the song wheel: test on a copy."),
            ],
            "undo": [
                tr("Back up taiko… saves the original program; Restore… puts it "
                   "back."),
                tr("Every HDD write also keeps an undo journal <img>.<date-time>.undo."),
            ],
            "actions": [("act_taikoexe", tr("Patch taiko song limit…")),
                        ("act_taikotimer", tr("Song-select timer…")),
                        ("act_taikoboot", tr("Boot && select patches…"))],
        },
        {
            "title": tr("Play Taiko 14 with a USB keyboard"),
            "intro": tr("Install KBD2EE so a plain USB keyboard works without a JVS "
                        "I/O board."),
            "needs": [tr("A Taiko 14 HDD image, plus the stock IRXARC.BIN from the "
                         "dongle card image (needed once)."),
                      tr("T14LOAD.bin next to the program.")],
            "steps": [
                tr("Tools > taiko executable patches > USB keyboard patch."),
                tr("Pick the HDD image (and the card image if asked), choose the "
                   "build, then Install. The image's `taiko` is checked first."),
                tr("Keys: 1P D F J K, 2P Z X C V, COIN 5, SERVICE 9, TEST F1, "
                   "SELECT Up / Down / Enter. Unplug and replug the keyboard once "
                   "after boot."),
            ],
            "wrong": [
                tr("A `taiko` that does not match is refused, so live code is "
                   "never overwritten."),
                tr("Keyboard + dummy JVS plays with no I/O board; keyboard-only is "
                   "for a machine that keeps its real board."),
            ],
            "undo": [
                tr("Remove deletes the files and the game uses the card again."),
                tr("HDD installs keep an undo journal; a card image gets a .bak first."),
            ],
            "actions": [("act_usbkbd", tr("USB keyboard patch…"))],
        },
        {
            "title": tr("PS2 HDD and memory card tools"),
            "intro": tr("Browse images, shrink them, merge several games onto one "
                        "disk, or edit a memory card."),
            "needs": [tr("The .img / card image to work on."),
                      tr("PCSX2 closed before writing an .img."),
                      tr("pfsshell.exe next to the program for the pfsshell "
                         "browser.")],
            "steps": [
                tr("Open PS2 HDD via pfsshell: browse a partition, extract or "
                   "replace DATA.000 and list.bin."),
                tr("Slim PS2 HDD image reclaims space; the emulator reads the "
                   "same bytes. Merge HDD images combines games into one disk."),
                tr("Open PS2 Memory Card: extract or replace T14GAME, boot.bin, "
                   "IRXARC.BIN…; ECC is regenerated."),
                tr("Pack to ISO injects the open DATA.000 + LIST.BIN into a new "
                   "game ISO."),
            ],
            "wrong": [tr("“Can't write the .img” almost always means an emulator "
                         "still holds it.")],
            "undo": [
                tr("HDD writes keep <img>.<date-time>.undo; roll back newest "
                   "first with python -m taiko2x6.safeio rollback <file>."),
                tr("A memory card gets a .bak copy before it is opened for "
                   "writing; merge sources are never modified."),
            ],
            "actions": [("act_pfsshell", tr("Open PS2 HDD via pfsshell…")),
                        ("act_imgslim", tr("Slim PS2 HDD image…")),
                        ("act_apamerge", tr("Merge HDD images…")),
                        ("act_ps2mc", tr("Open PS2 Memory Card…")),
                        ("act_packiso", tr("Pack to ISO…"))],
        },
    ]


def page_actions() -> list[str]:
    """Every MainWindow action name a guide page can open."""
    return sorted({a for p in _pages() for a, _ in p["actions"]})


def _html(p: dict) -> str:
    def block(head: str, items: list[str], ordered: bool = False) -> str:
        if not items:
            return ""
        tag = "ol" if ordered else "ul"
        li = "".join(f"<li>{escape(i)}</li>" for i in items)
        return f"<h4>{escape(head)}</h4><{tag}>{li}</{tag}>"
    return (f"<h2>{escape(p['title'])}</h2><p>{escape(p['intro'])}</p>"
            + block(tr("What you need"), p["needs"])
            + block(tr("Steps"), p["steps"], ordered=True)
            + block(tr("What can go wrong"), p["wrong"])
            + block(tr("Backup and undo"), p["undo"]))


class GuideDialog(QDialog):
    """Help > Quick guide: workflow pages on the left list, text on the right,
    and buttons that open the tool of the page (actions of ``parent``)."""

    def __init__(self, parent=None):
        super().__init__(parent)
        self.setWindowTitle(tr("Quick guide"))
        self.resize(860, 600)
        self._pages = _pages()
        self._buttons: list[tuple[QPushButton, object]] = []

        self.listing = QListWidget()
        self.listing.addItems([p["title"] for p in self._pages])
        self.listing.setMinimumWidth(190)
        self.text = QTextBrowser()
        self.text.setOpenExternalLinks(False)
        split = QSplitter(Qt.Horizontal)
        split.addWidget(self.listing)
        split.addWidget(self.text)
        split.setStretchFactor(1, 1)
        split.setSizes([230, 630])

        self.tools = QHBoxLayout()
        self.b_close = QPushButton(tr("Close"))
        self.b_close.clicked.connect(self.accept)
        bottom = QHBoxLayout()
        bottom.addLayout(self.tools, 1)
        bottom.addWidget(self.b_close)

        lay = QVBoxLayout(self)
        lay.addWidget(split, 1)
        lay.addLayout(bottom)
        for name in page_actions():
            act = getattr(parent, name, None)
            if act is not None:
                act.changed.connect(self._sync_buttons)
        self.listing.currentRowChanged.connect(self._show)
        self.listing.setCurrentRow(0)
        polish(self, primary=self.b_close)

    def select(self, row: int) -> None:
        self.listing.setCurrentRow(row)

    def _show(self, row: int) -> None:
        if not 0 <= row < len(self._pages):
            return
        page = self._pages[row]
        self.text.setHtml(_html(page))
        while self.tools.count():
            item = self.tools.takeAt(0)
            if item.widget() is not None:
                item.widget().deleteLater()
        self._buttons = []
        for name, label in page["actions"]:
            act = getattr(self.parent(), name, None)
            b = QPushButton(label)
            b.setAutoDefault(False)
            if act is None:
                b.setEnabled(False)
            else:
                b.clicked.connect(lambda _=False, a=act: a.trigger())
                b.setToolTip(act.toolTip() if act.isEnabled() else
                             tr("Not available right now (for example, no archive is open)."))
            self.tools.addWidget(b)
            self._buttons.append((b, act))
        self.tools.addStretch(1)
        self._sync_buttons()

    def _sync_buttons(self) -> None:
        for b, act in self._buttons:
            try:
                b.setEnabled(act is not None and act.isEnabled())
            except RuntimeError:        # button already deleted by a page change
                pass
