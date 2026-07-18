"""Gen3 (Nijiiro) -> Gen2 (PS2 arcade) song converter: fumen .bin -> sht, and
song_<id>.nus3bank -> game VAG.

Chart side
----------
The Gen3 fumen and the Gen2 sht turn out to share far more than expected, which
is what makes this conversion trustworthy rather than a guess:

  * The note TYPE enum is IDENTICAL. Verified on the 101 songs that ship an
    official chart in BOTH generations: 12 distinct types, ~40k notes, a 100%
    diagonal confusion matrix (4 stray notes total, from charts rebalanced
    between generations).
  * The TIME AXIS is the same: gen2 track.time == gen3 measure.offset.
  * Gen2 stores a note's position as 1/48 of its measure; Gen3 stores absolute
    milliseconds within the measure. round(pos / measure_len * 48) reproduces
    the official gen2 value for 95.6% of notes, the residual being +/-1 rounding.

That last point is the only lossy step, and it is a limit of the Gen2 format
itself (a 1/48 grid cannot express every millisecond), not of this converter.
Run ``--validate`` to measure it against the official charts directly.

Audio side
----------
nus3bank is a NUS3 chunked container (BANK/TOC/PROP/BINF/GRP/DTON/TONE/PACK)
whose audio is BNSF -- Namco's wrapper around ITU G.719 (Siren 22). That codec
is not reimplemented here; vgmstream decodes it. Point VGMSTREAM_DIR at a
vgmstream build, or pass --vgmstream. If it is missing we say so and stop,
rather than silently producing nothing.
"""
import binascii
import gzip
import os
import pathlib
import struct
import subprocess
import sys
import tempfile

import tja2sht

# --- Gen3 fumen decryption -------------------------------------------------

FUMEN_KEY = binascii.unhexlify(
    "4434423946383537303842433443383030333843444132343339373531353830")
DATATABLE_KEY = binascii.unhexlify(
    "3530304242323633353537423431384139353134383346433246464231354534")

# Types whose note occupies a span of time (drumroll / balloon / kusudama).
# Established against the official Gen2 charts: these are exactly the notes
# where Gen2 sets longNoteLength to something other than -1, and the Gen3 `dur`
# field reproduces that value. Type 9 is confirmed separately: 328/328 of them
# carry a length in Gen2, and `dur` matches it 312/328.
#
# Same set that musicinfo's official *OnpuNum excludes from a chart's note
# count (99.5% of difficulties) -- rolls and balloons do not count as notes.
_ROLL_TYPES = {6, 9, 10, 12}

# Types that are balloons: Gen2 gives them a balloonHitCount (others get -1).
# The count lives in the u16 at +0x10 -- the SAME slot that carries scoreInit on
# an ordinary note, i.e. that slot is a union, not a score field.  Verified
# 103/103 against the official charts.
_BALLOON_TYPES = {10, 12}

# Gen2 uses -1, not 0, to mean "this note has no balloon count / no length".
_NONE = -1

_MEASURE_HDR = 40
_NOTE_SIZE = 24
_POS_DIV = 48          # gen2 in-measure grid


def decrypt_fumen(path, key=FUMEN_KEY):
    """AES-256-CBC (IV = first 16 bytes), strip PKCS7, gunzip."""
    raw = pathlib.Path(path).read_bytes()
    if len(raw) < 32:
        raise ValueError("%s: too small to be a Gen3 fumen (%d bytes)"
                         % (path, len(raw)))
    iv, body = raw[:16], raw[16:]
    try:
        from Crypto.Cipher import AES
        dec = AES.new(key, AES.MODE_CBC, iv).decrypt(body)
    except ImportError:
        from cryptography.hazmat.primitives.ciphers import (
            Cipher, algorithms, modes)
        d = Cipher(algorithms.AES(key), modes.CBC(iv)).decryptor()
        dec = d.update(body) + d.finalize()
    pad = dec[-1] if dec else 0
    if 1 <= pad <= 16 and dec[-pad:] == bytes([pad]) * pad:
        dec = dec[:-pad]
    return gzip.decompress(dec)


# Note types carrying 8 extra bytes beyond the standard 24. Established by
# walking every Gen3 fumen that ships with the game: 19656/19656 charts consume
# exactly measureCount measures and land exactly on EOF under this rule, and
# none needs a different one -- it is a constant of the format, not per-chart.
#
# Types 10 and 12 also carry a duration but are 24 bytes: they are balloons. Do
# NOT infer "has a duration" => "is long".
_LONG_NOTE_TYPES = frozenset({6, 9})


def parse_gen3(data, long_types=_LONG_NOTE_TYPES):
    """Parse a decrypted Gen3 fumen into a list of measure dicts.

    Raises ValueError unless the walk consumes exactly measureCount measures and
    ends exactly on EOF. That assertion is the whole safety net, so it is left
    to fail loudly: a chart this rule does not fit is a genuinely new variant
    worth looking at, and guessing an alternative rule until one happens to land
    on EOF would silently mis-parse it instead.
    """
    if len(data) < 0x208:
        raise ValueError("decrypted fumen too small (%d bytes)" % len(data))
    n_meas = struct.unpack_from("<I", data, 0x200)[0]
    pos = 0x208
    meas = []
    for i in range(n_meas):
        if pos + _MEASURE_HDR > len(data):
            raise ValueError("fumen truncated at measure %d (offset 0x%X)"
                             % (i, pos))
        bpm, offset = struct.unpack_from("<ff", data, pos)
        # +9 is the bar-line flag, not a "hidden" flag: its value distribution
        # is identical to Gen2's trackLine on every shared chart.
        gogo, barline = data[pos + 8], data[pos + 9]
        branch_info = list(struct.unpack_from("<6i", data, pos + 12))
        pos += _MEASURE_HDR
        branches = []
        for _b in range(3):
            cnt, unk, speed = struct.unpack_from("<HHf", data, pos)
            pos += 8
            notes = []
            for _k in range(cnt):
                t, p, item, pad, u10, u12, dur = struct.unpack_from(
                    "<ifiiHHf", data, pos)
                pos += 32 if t in long_types else _NOTE_SIZE
                # u10 is a union: balloon hit count on _BALLOON_TYPES,
                # scoreInit otherwise.
                notes.append({"type": t, "pos": p, "item": item, "pad": pad,
                              "u10": u10, "u12": u12, "dur": dur})
            branches.append({"count": cnt, "unk": unk, "speed": speed,
                             "notes": notes})
        meas.append({"bpm": bpm, "offset": offset, "gogo": gogo,
                     "barline": barline, "branchInfo": branch_info,
                     "branches": branches})
    if len(meas) != n_meas or pos != len(data):
        raise ValueError(
            "Gen3 layout check failed: parsed %d/%d measures, ended at 0x%X "
            "of 0x%X" % (len(meas), n_meas, pos, len(data)))
    return meas


def measure_length(meas, k):
    """Length of measure k in ms. Gen3 stores absolute offsets, so a measure's
    length is simply the gap to the next one; the last falls back to its BPM."""
    if k + 1 < len(meas):
        return meas[k + 1]["offset"] - meas[k]["offset"]
    bpm = meas[k]["bpm"]
    return 240000.0 / bpm if bpm > 0 else 0.0


# --- Gen3 -> Gen2 sht ------------------------------------------------------

def gen3_to_sht(meas, branch=0):
    """Build Gen2 sht bytes from parsed Gen3 measures.

    The sub-track / note-table layout below is not invented here: it mirrors
    what tja2sht.convert_tja emits, which in turn matches every corpus chart
    (each measure's notes stored twice, sub-track 0 and 3 pointing at the two
    copies, empty slots kept contiguous, pointGain a remaining-note countdown).
    Deviating from it hangs the game at chart load.
    """
    notes = []
    tracks = []

    per_measure = []
    for k, m in enumerate(meas):
        L = measure_length(meas, k)
        br = m["branches"][branch]
        recs = []
        for n in br["notes"]:
            p = int(round(n["pos"] / L * _POS_DIV)) if L > 0 else 0
            p = max(0, min(_POS_DIV - 1, p))
            t = n["type"]
            recs.append({
                "type": t,
                "measure": p,
                "balloonHitCount": n["u10"] if t in _BALLOON_TYPES else _NONE,
                "longNoteLength": (int(round(n["dur"])) if t in _ROLL_TYPES
                                   else _NONE),
            })
        per_measure.append((m, br, recs))

    total = sum(len(r) for (_m, _b, r) in per_measure)
    remaining = total
    for (m, br, recs) in per_measure:
        copy0 = len(notes)
        notes.extend(dict(r) for r in recs)
        copy1 = len(notes)
        notes.extend(dict(r) for r in recs)
        end = len(notes)
        cnt = len(recs)
        remaining -= cnt

        subs = []
        for s in range(6):
            if s == 0:
                subs.append({"noteIndexSt": copy0, "noteCount": cnt,
                             "pointGain": remaining})
            elif s == 3:
                subs.append({"noteIndexSt": copy1, "noteCount": cnt,
                             "pointGain": remaining})
            elif s in (1, 2):
                subs.append({"noteIndexSt": copy1, "noteCount": 0,
                             "pointGain": 0})
            else:
                subs.append({"noteIndexSt": end, "noteCount": 0,
                             "pointGain": 0})

        tracks.append({
            "time": m["offset"],
            "bpm": m["bpm"],
            "trackLine": int(m["barline"]),
            "gogoFlag": 1 if m["gogo"] else 0,
            "_unk": 0,
            "bunkis": [-1] * 6,
            "scrollSpeeds": [float(br["speed"])] * 6,
            "subtracks": subs,
        })

    if not tracks:
        raise ValueError("Gen3 fumen has no measures")

    return tja2sht.serialize_sht({
        "noteOffset": 16 + len(tracks) * tja2sht.TRACK_NEW_SIZE,
        "padding": 0,
        "isOld": False,
        "tracks": tracks,
        "notes": notes,
        "_gap": b"",
        "_tail": b"",
    })


def convert_fumen(path, branch=0):
    """Gen3 fumen .bin path -> Gen2 sht bytes."""
    return gen3_to_sht(parse_gen3(decrypt_fumen(path)), branch=branch)


# --- Audio: nus3bank -> VAG ------------------------------------------------

import apppaths
VGMSTREAM_DIR = str(apppaths.resource_dir() / "vgmstream-win64")


def find_vgmstream(explicit=None):
    """Locate vgmstream-cli. Returns the path, or None."""
    cands = []
    if explicit:
        cands += [explicit, os.path.join(explicit, "vgmstream-cli.exe"),
                  os.path.join(explicit, "vgmstream-cli")]
    cands += [os.path.join(VGMSTREAM_DIR, "vgmstream-cli.exe"),
              os.path.join(VGMSTREAM_DIR, "vgmstream-cli")]
    for c in cands:
        if c and os.path.isfile(c):
            return c
    from shutil import which
    return which("vgmstream-cli")


class VgmstreamMissing(RuntimeError):
    pass


def nus3bank_to_wav(src, dst, vgmstream=None):
    """Decode a nus3bank to a wav via vgmstream. Raises if it is unavailable."""
    exe = find_vgmstream(vgmstream)
    if not exe:
        raise VgmstreamMissing(
            "vgmstream-cli not found. nus3bank audio is BNSF (ITU G.719 / "
            "Siren 22) and needs vgmstream to decode. Looked in %s and on "
            "PATH." % VGMSTREAM_DIR)
    r = subprocess.run([exe, "-o", str(dst), str(src)],
                       capture_output=True, text=True)
    if r.returncode != 0 or not os.path.isfile(dst):
        raise RuntimeError("vgmstream failed on %s:\n%s\n%s"
                           % (src, r.stdout.strip(), r.stderr.strip()))
    return dst


def convert_audio(src, sample_rate=44100, lead_silence_ms=0.0,
                  trim_start_ms=0.0, vgmstream=None, progress=None,
                  cancel=None):
    """song_<id>.nus3bank -> game VAG bytes (via wav)."""
    import vagtool
    tmp = tempfile.mkdtemp(prefix="gen3conv_")
    try:
        wav = os.path.join(tmp, "decoded.wav")
        nus3bank_to_wav(src, wav, vgmstream=vgmstream)
        return vagtool.convert_audio_file(
            wav, sample_rate=sample_rate, progress=progress, cancel=cancel,
            lead_silence_ms=lead_silence_ms, trim_start_ms=trim_start_ms)
    finally:
        import shutil
        shutil.rmtree(tmp, ignore_errors=True)


# --- Validation against the official Gen2 charts ---------------------------

def validate(gen3_root, gen2_root, limit=None, log=print):
    """Convert songs that have BOTH official charts and diff against Gen2.

    This is the honest measure of conversion quality: both sides are shipped
    game data, so any disagreement is ours (or a chart rebalanced between
    generations, which is why note-count mismatches are reported separately
    rather than silently dropped).
    """
    g3 = pathlib.Path(gen3_root)
    g2 = pathlib.Path(gen2_root)
    a = {p.name for p in g3.iterdir() if p.is_dir()}
    b = {n.rsplit("1p_", 1)[0] for n in
         (d.name for d in g2.iterdir() if d.is_dir()) if "1p_" in n}
    ids = sorted(a & b)
    if limit:
        ids = ids[:limit]
    log("songs with both official charts: %d" % len(ids))

    pairs = skipped = 0
    drifts = []
    stats = {"type": [0, 0], "measure": [0, 0], "balloon": [0, 0],
             "long": [0, 0], "time": [0, 0], "bpm": [0, 0], "gogo": [0, 0],
             "trackLine": [0, 0], "scroll": [0, 0]}

    def bump(key, good):
        stats[key][0 if good else 1] += 1

    for sid in ids:
        for diff in ("e", "n", "h", "m"):
            p3 = g3 / sid / ("%s_%s.bin" % (sid, diff))
            p2 = g2 / ("%s1p_%s" % (sid, diff)) / "sht"
            if not (p3.exists() and p2.exists()):
                continue
            try:
                meas = parse_gen3(decrypt_fumen(p3))
                ours = tja2sht.parse_sht(gen3_to_sht(meas))
                theirs = tja2sht.parse_sht(p2.read_bytes())
            except Exception as e:
                log("  %s_%s: %s" % (sid, diff, e))
                skipped += 1
                continue
            if len(ours["tracks"]) != len(theirs["tracks"]):
                skipped += 1
                continue
            pairs += 1
            # Compare times RELATIVE to the first measure. The two generations
            # ship different audio masters, so their absolute offsets differ by
            # a constant (~24 ms here, 1.5 s for other songs); only the shape of
            # the timeline is meaningful across generations.
            b1 = ours["tracks"][0]["time"]
            b2 = theirs["tracks"][0]["time"]
            for t1, t2 in zip(ours["tracks"], theirs["tracks"]):
                d = abs((t1["time"] - b1) - (t2["time"] - b2))
                drifts.append(d)
                bump("time", d < 2.0)
                bump("bpm", abs(t1["bpm"] - t2["bpm"]) < 0.05)
                bump("gogo", t1["gogoFlag"] == t2["gogoFlag"])
                bump("trackLine", t1["trackLine"] == t2["trackLine"])
                bump("scroll", abs(t1["scrollSpeeds"][0]
                                   - t2["scrollSpeeds"][0]) < 0.01)
                s1, s2 = t1["subtracks"][0], t2["subtracks"][0]
                if s1["noteCount"] != s2["noteCount"]:
                    continue
                n1 = ours["notes"][s1["noteIndexSt"]:
                                   s1["noteIndexSt"] + s1["noteCount"]]
                n2 = theirs["notes"][s2["noteIndexSt"]:
                                     s2["noteIndexSt"] + s2["noteCount"]]
                for x, y in zip(n1, n2):
                    bump("type", x["type"] == y["type"])
                    bump("measure", x["measure"] == y["measure"])
                    bump("balloon", x["balloonHitCount"] == y["balloonHitCount"])
                    bump("long", x["longNoteLength"] == y["longNoteLength"])

    log("")
    log("chart pairs compared: %d   (skipped: %d)" % (pairs, skipped))
    log("%-12s %10s %10s %8s" % ("field", "match", "differ", "pct"))
    for k, (g, bad) in stats.items():
        tot = g + bad
        log("%-12s %10d %10d %7.2f%%"
            % (k, g, bad, 100.0 * g / tot if tot else 0.0))
    if drifts:
        drifts.sort()
        log("")
        log("measure-time drift vs the official Gen2 chart, after removing the "
            "constant\noffset between the two generations' audio masters "
            "(informational -- our times\ncome from Gen3 and ship with Gen3 "
            "audio, so this is how much the two\ngenerations' charts differ, "
            "not our error):")
        log("  median %.2f ms   p95 %.2f ms   max %.2f ms"
            % (drifts[len(drifts) // 2], drifts[int(len(drifts) * .95)],
               drifts[-1]))
    return stats


# --- GUI -------------------------------------------------------------------

try:
    from PySide6.QtWidgets import (
        QDialog, QVBoxLayout, QHBoxLayout, QLabel, QLineEdit, QPushButton,
        QPlainTextEdit, QFileDialog, QMessageBox, QComboBox, QDoubleSpinBox)
except ImportError:                                            # headless / CLI
    QDialog = None


if QDialog is not None:

    class Gen3ConvertDialog(QDialog):
        """Convert a Gen3 song's chart and audio into Gen2 game assets."""

        _DIFFS = [("Easy", "e"), ("Normal", "n"), ("Hard", "h"), ("Oni", "m")]

        def __init__(self, parent=None, fumen_dir="", sound_dir=""):
            super().__init__(parent)
            self.setWindowTitle("Gen3 → Gen2 converter")
            self.resize(720, 460)
            v = QVBoxLayout(self)

            v.addWidget(QLabel(
                "Converts a Nijiiro (Gen3) song to PS2-arcade (Gen2) assets: the\n"
                "fumen .bin becomes an .sht chart, and song_<id>.nus3bank becomes\n"
                "a game VAG. Note types are identical between generations; note\n"
                "positions are re-gridded to Gen2's 1/48 of a measure, which is\n"
                "the one lossy step and is a limit of the Gen2 format itself."))

            for label, attr, browse in (
                    ("Gen3 fumen .bin:", "ed_fumen", "file"),
                    ("Gen3 nus3bank:", "ed_audio", "file"),
                    ("Output folder:", "ed_out", "dir")):
                row = QHBoxLayout()
                row.addWidget(QLabel(label))
                ed = QLineEdit()
                setattr(self, attr, ed)
                row.addWidget(ed, 1)
                b = QPushButton("Browse…")
                b.clicked.connect(
                    lambda _=False, e=ed, k=browse: self._browse(e, k))
                row.addWidget(b)
                v.addLayout(row)

            row = QHBoxLayout()
            row.addWidget(QLabel("Audio lead silence (ms):"))
            self.sp_lead = QDoubleSpinBox()
            self.sp_lead.setRange(0, 60000)
            self.sp_lead.setDecimals(1)
            self.sp_lead.setToolTip(
                "The game starts the chart and the music together at t=0, so a "
                "song whose music should start N ms in needs N ms of silence "
                "baked into the VAG. Gen3 charts already carry their own "
                "measure-0 offset, so this is usually 0.")
            row.addWidget(self.sp_lead)
            row.addStretch(1)
            v.addLayout(row)

            self.log = QPlainTextEdit()
            self.log.setReadOnly(True)
            v.addWidget(self.log, 1)

            row = QHBoxLayout()
            row.addStretch(1)
            self.btn_go = QPushButton("Convert")
            self.btn_go.clicked.connect(self._run)
            row.addWidget(self.btn_go)
            close = QPushButton("Close")
            close.clicked.connect(self.reject)
            row.addWidget(close)
            v.addLayout(row)

            self._fumen_dir = fumen_dir
            self._sound_dir = sound_dir

        def _browse(self, ed, kind):
            if kind == "dir":
                p = QFileDialog.getExistingDirectory(self, "Output folder",
                                                     ed.text())
            else:
                start = (self._fumen_dir if ed is self.ed_fumen
                         else self._sound_dir) or ed.text()
                filt = ("Gen3 fumen (*.bin)" if ed is self.ed_fumen
                        else "nus3bank (*.nus3bank)")
                p, _ = QFileDialog.getOpenFileName(self, "Select", start, filt)
            if p:
                ed.setText(p)

        def _say(self, s):
            self.log.appendPlainText(str(s))
            self.log.repaint()

        def _run(self):
            out = self.ed_out.text().strip()
            if not out or not os.path.isdir(out):
                QMessageBox.warning(self, "Convert", "Pick an output folder.")
                return
            self.btn_go.setEnabled(False)
            try:
                fumen = self.ed_fumen.text().strip()
                if fumen:
                    self._say("chart: %s" % fumen)
                    try:
                        data = convert_fumen(fumen)
                        dst = os.path.join(
                            out, pathlib.Path(fumen).stem + ".sht")
                        pathlib.Path(dst).write_bytes(data)
                        self._say("  wrote %s (%d bytes)" % (dst, len(data)))
                    except Exception as e:
                        self._say("  FAILED: %s" % e)
                audio = self.ed_audio.text().strip()
                if audio:
                    self._say("audio: %s" % audio)
                    try:
                        data = convert_audio(
                            audio, lead_silence_ms=self.sp_lead.value())
                        dst = os.path.join(
                            out, pathlib.Path(audio).stem + ".vag")
                        pathlib.Path(dst).write_bytes(data)
                        self._say("  wrote %s (%d bytes)" % (dst, len(data)))
                    except VgmstreamMissing as e:
                        self._say("  FAILED: %s" % e)
                    except Exception as e:
                        self._say("  FAILED: %s" % e)
                if not fumen and not audio:
                    self._say("nothing selected.")
                self._say("done.")
            finally:
                self.btn_go.setEnabled(True)


# --- CLI -------------------------------------------------------------------

def main(argv=None):
    import argparse
    ap = argparse.ArgumentParser(
        description="Convert Gen3 (Nijiiro) fumen/audio to Gen2 (PS2 arcade).")
    ap.add_argument("input", nargs="?",
                    help="Gen3 fumen .bin, or song_<id>.nus3bank")
    ap.add_argument("-o", "--out", help="output file (.sht / .vag)")
    ap.add_argument("--branch", type=int, default=0,
                    help="Gen3 branch to take (0=normal, default 0)")
    ap.add_argument("--lead-ms", type=float, default=0.0,
                    help="silence to prepend to audio")
    ap.add_argument("--trim-ms", type=float, default=0.0,
                    help="milliseconds to cut off the front of the audio")
    ap.add_argument("--rate", type=int, default=44100)
    ap.add_argument("--vgmstream", help="path to vgmstream-cli or its folder")
    ap.add_argument("--validate", nargs=2, metavar=("GEN3_FUMEN", "GEN2_FUMEN"),
                    help="diff converted charts against the official Gen2 ones")
    ap.add_argument("--limit", type=int, help="validate: only the first N songs")
    a = ap.parse_args(argv)

    if a.validate:
        validate(a.validate[0], a.validate[1], limit=a.limit)
        return 0

    if not a.input:
        ap.error("input is required (or use --validate)")
    src = pathlib.Path(a.input)
    if src.suffix.lower() == ".nus3bank":
        data = convert_audio(src, sample_rate=a.rate,
                             lead_silence_ms=a.lead_ms,
                             trim_start_ms=a.trim_ms, vgmstream=a.vgmstream)
        out = a.out or str(src.with_suffix(".vag"))
    else:
        data = convert_fumen(src, branch=a.branch)
        out = a.out or str(src.with_suffix(".sht"))
    pathlib.Path(out).write_bytes(data)
    print("wrote %s (%d bytes)" % (out, len(data)))
    return 0


if __name__ == "__main__":
    sys.exit(main())
