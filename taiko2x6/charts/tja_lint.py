#!/usr/bin/env python3
"""TJA check: report common mistakes in a TJA before it is converted or built.

    issues = lint_tja(text, base_dir=folder_of_the_tja, raw=file_bytes)
    for i in issues: print(format_issue(i))

Nothing here changes how a TJA converts: the check only reads. Some issues
carry an automatic fix (``Issue.edits``) that is unambiguous; ``apply_fixes``
returns the fixed TEXT and ``write_fixed_copy`` saves it next to the original
as ``<name>.fixed.tja`` -- the user's file is never overwritten.

Rules (``Issue.rule``):
  no_course, unknown_course, no_level, bad_level   COURSE / LEVEL headers
  balloon_short, balloon_long, balloon_missing     BALLOON list vs balloon notes
  outside_chart, no_end, stray_end                 notes outside #START/#END
  unterminated_measure                             last measure lacks its comma
  roll_open, roll_nested                           roll / balloon not closed by 8
  branch_missing_route, branch_length, branch_open #BRANCHSTART structure
  odd_chars                                        characters the converter blanks
  offset_sign                                      first note before audio start
  wave_missing, no_wave                            WAVE file next to the TJA
  encoding_sjis, encoding_unknown, mojibake        text encoding
"""
from __future__ import annotations

import re
import unicodedata
from dataclasses import dataclass, field
from pathlib import Path

from taiko2x6.app.i18n import tr
from taiko2x6.charts import tja2sht

WARNING, INFO = "warning", "info"

_NOTE_CHARS = set("0123456789ABab,")
_NOTE_LIKE = re.compile(r"^[0-9ABab,\s]+$")
_ROUTES = {"N": 0, "E": 1, "M": 2}
_IGNORED_COURSES = ("dan", "tower")        # converter drops these on purpose
_MOJIBAKE_ENCODINGS = ("cp932", "latin-1", "cp1252")
DEFAULT_PAD = 5


@dataclass
class Issue:
    line: int                     # 1-based, 0 = whole file
    rule: str
    severity: str
    message: str
    suggestion: str = ""
    # automatic fix: ("sub", idx, fn(str)->str) / ("ins", idx, text) /
    # ("recode", fn(str)->str); idx is the 0-based original line index
    edits: list = field(default_factory=list)
    fix_text: str = ""            # what the fix does, in words

    @property
    def fixable(self) -> bool:
        return bool(self.edits)


def format_issue(i: Issue) -> str:
    head = tr("line {n}: ").format(n=i.line) if i.line else ""
    return head + i.message + (" — " + i.suggestion if i.suggestion else "")


# ---------------------------------------------------------------------------
# reading / writing
# ---------------------------------------------------------------------------
def decode_guess(raw: bytes):
    """(text, encoding): UTF-16 by BOM, UTF-8, else Shift-JIS (cp932); a file
    that is none of them is decoded lossily as UTF-8 with encoding None."""
    if raw[:2] in (b"\xff\xfe", b"\xfe\xff"):
        try:
            return raw.decode("utf-16"), "utf-16"
        except UnicodeDecodeError:
            return raw.decode("utf-16", errors="replace"), None
    try:
        return raw.decode("utf-8-sig"), "utf-8"
    except UnicodeDecodeError:
        pass
    try:
        return raw.decode("cp932"), "cp932"
    except UnicodeDecodeError:
        return raw.decode("utf-8", errors="replace"), None


def read_tja(path):
    """(text, raw, encoding) of a .tja file."""
    raw = Path(path).read_bytes()
    text, enc = decode_guess(raw)
    return text, raw, enc


def fixed_copy_path(path) -> Path:
    """<name>.fixed.tja next to the TJA; never an existing file (a second
    copy becomes <name>.fixed2.tja ...)."""
    p = Path(path)
    cand = p.with_name(p.stem + ".fixed.tja")
    n = 2
    while cand.exists():
        cand = p.with_name("%s.fixed%d.tja" % (p.stem, n))
        n += 1
    return cand


def write_fixed_copy(path, text: str, encoding=None) -> Path:
    """Save `text` as a new file beside `path` (never over it)."""
    dst = fixed_copy_path(path)
    enc = {"cp932": "cp932", "utf-16": "utf-16"}.get(encoding, "utf-8")
    try:
        data = text.encode(enc)
    except UnicodeEncodeError:
        data = text.encode("utf-8")
    dst.write_bytes(data)
    return dst


# ---------------------------------------------------------------------------
# fixes
# ---------------------------------------------------------------------------
def apply_fixes(text: str, issues, only=None):
    """Apply the automatic fixes of `issues` (or just those in `only`) to
    `text`. Returns (fixed_text, [issues applied])."""
    nl = _newline(text)
    lines = text.split(nl)
    chosen = [i for i in issues if i.fixable and (only is None or i in only)]
    recode = []
    ins = []
    for i in chosen:
        for e in i.edits:
            if e[0] == "sub" and e[1] < len(lines):
                lines[e[1]] = e[2](lines[e[1]])
            elif e[0] == "ins":
                ins.append((e[1], e[2]))
            elif e[0] == "recode":
                recode.append(e[1])
    for idx, t in sorted(ins, key=lambda x: -x[0]):
        lines.insert(idx, t)
    out = nl.join(lines)
    for fn in recode:
        out = fn(out)
    return out, chosen


def _newline(text):
    if "\r\n" in text:
        return "\r\n"
    if "\n" not in text and "\r" in text:
        return "\r"
    return "\n"


def _add_comma(s):
    k = s.find("//")
    if k < 0:
        return s.rstrip() + ","
    return s[:k].rstrip() + ", " + s[k:]


def _replace_balloon(vals):
    text = "BALLOON:" + ",".join(str(v) for v in vals)
    return lambda s: text


def _normalize_note_line(s):
    k = s.find("//")
    head, tail = (s, "") if k < 0 else (s[:k], s[k:])
    head = "".join(
        c if c in _NOTE_CHARS or c.isspace() else
        (unicodedata.normalize("NFKC", c)
         if unicodedata.normalize("NFKC", c) in _NOTE_CHARS else c)
        for c in head)
    return head + tail


def _demojibake(s):
    """s re-read as UTF-8 if it is UTF-8 text mis-decoded as Shift-JIS /
    Latin-1, else None."""
    for enc in _MOJIBAKE_ENCODINGS:
        try:
            out = s.encode(enc).decode("utf-8")
        except (UnicodeEncodeError, UnicodeDecodeError):
            continue
        if out != s and any(ord(c) > 0x2E80 for c in out):
            return out
    return None


# ---------------------------------------------------------------------------
# the check
# ---------------------------------------------------------------------------
def _parse_balloon(val):
    return [int(tja2sht._to_float(x)) for x in val.replace(" ", "").split(",")
            if x != ""]


class _Course:
    def __init__(self, name, line, idx):
        self.name = name              # raw COURSE value ("" = none given)
        self.line = line              # 1-based line of COURSE: (0 = none)
        self.idx = idx
        self.headers = {}             # KEY -> (value, 1-based line)
        self.balloon = None           # (list, 1-based line) own BALLOON line
        self.balloons = 0             # 7/9 notes seen in the chart
        self.start_idx = None         # 0-based line of #START
        self.chart_seen = False


def lint_tja(text: str, base_dir=None, raw: bytes | None = None) -> list:
    """All issues of a TJA, sorted by line. `base_dir` (the TJA's folder)
    enables the WAVE checks; `raw` (the file bytes) the encoding guess."""
    issues: list = []

    def add(line, rule, sev, msg, sug="", edits=None, fix=""):
        issues.append(Issue(line, rule, sev, msg, sug, edits or [], fix))

    nl = _newline(text)
    lines = text.split(nl)

    gheaders = {}                     # global header -> (value, line)
    gballoon = None                   # (list, line)
    cur = None                        # current _Course
    courses = []
    in_block = skip = False
    outside_run = []                  # consecutive stray note lines

    # per-chart state
    st = None

    def flush_outside():
        if outside_run:
            first = outside_run[0]
            n = len(outside_run)
            add(first + 1, "outside_chart", WARNING,
                tr("Note data outside #START / #END ({n} line(s)); the "
                   "converter ignores it").format(n=n),
                tr("Move it between #START and #END, or delete it."))
            outside_run.clear()

    def new_st():
        return {"buf": 0, "last_note": None, "roll": None, "route": None,
                "branch": None, "start": None, "measures": 0}

    def end_roll(reason_line=None):
        r = st["roll"]
        if r is not None:
            add(r + 1, "roll_open", WARNING,
                tr("Roll or balloon started here is never closed with 8"),
                tr("Add an 8 where the roll / balloon ends."))
            st["roll"] = None

    def check_unterminated():
        if st["buf"] and st["last_note"] is not None:
            ln = st["last_note"]
            add(ln + 1, "unterminated_measure", WARNING,
                tr("Last measure has no closing comma"),
                tr("End the measure with a comma."),
                [("sub", ln, _add_comma)],
                tr("Add the missing comma at the end of this line."))
            st["buf"] = 0

    def finish_branch():
        b = st["branch"]
        if b is None:
            return
        missing = [k for k in "NEM" if _ROUTES[k] not in b["counts"]]
        if missing:
            add(b["line"] + 1, "branch_missing_route", WARNING,
                tr("#BRANCHSTART lacks {routes}").format(
                    routes=", ".join("#" + k for k in missing)),
                tr("A branch needs all of #N, #E and #M."))
        else:
            cs = [b["counts"][_ROUTES[k]] for k in "NEM"]
            if len(set(cs)) > 1:
                add(b["line"] + 1, "branch_length", WARNING,
                    tr("Branch routes differ in length: #N {n}, #E {e}, "
                       "#M {m} measures").format(n=cs[0], e=cs[1], m=cs[2]),
                    tr("Give every route the same number of measures; "
                       "otherwise the converter keeps only one path."))
        st["branch"] = None
        st["route"] = None

    def finish_block(end_line):
        nonlocal in_block
        if st is not None:
            check_unterminated()
            end_roll()
            if st["branch"] is not None:
                add(st["branch"]["line"] + 1, "branch_open", WARNING,
                    tr("#BRANCHSTART is never closed with #BRANCHEND"),
                    tr("Add #BRANCHEND after the last route."))
                finish_branch()
        in_block = False

    for i, rawline in enumerate(lines):
        content = rawline.split("//", 1)[0].strip()
        if not content:
            continue
        if content.startswith("#"):
            flush_outside()
            parts = content[1:].split(None, 1)
            cmd = parts[0].upper() if parts else ""
            arg = parts[1].strip() if len(parts) > 1 else ""
            if cmd == "START":
                if in_block:
                    if not skip:
                        add(i + 1, "no_end", WARNING,
                            tr("#START again before the previous chart's #END"),
                            tr("Close the previous chart with #END."))
                        finish_block(i)
                if cur is None:
                    cur = _Course("", 0, tja2sht._COURSE_ALIASES["oni"])
                    courses.append(cur)
                    add(i + 1, "no_course", WARNING,
                        tr("No COURSE: before #START; the chart is taken as Oni"),
                        tr("Add a COURSE: line (Easy / Normal / Hard / Oni / Edit)."))
                in_block = True
                skip = (arg.upper().replace(" ", "") == "P2"
                        or cur.chart_seen)
                if skip:
                    st = None
                else:
                    cur.chart_seen = True
                    cur.start_idx = i
                    st = new_st()
                    st["start"] = i
                continue
            if cmd == "END":
                if not in_block:
                    add(i + 1, "stray_end", WARNING,
                        tr("#END without a #START"),
                        tr("Remove it or add the missing #START."))
                else:
                    if st is not None:
                        # the unterminated measure belongs to the line before
                        finish_block(i)
                    else:
                        in_block = False
                continue
            if st is None or not in_block:
                continue
            if cmd == "BRANCHSTART":
                if st["branch"] is not None:
                    add(st["branch"]["line"] + 1, "branch_open", WARNING,
                        tr("#BRANCHSTART is never closed with #BRANCHEND"),
                        tr("Add #BRANCHEND after the last route."))
                    finish_branch()
                check_unterminated()
                end_roll()
                st["branch"] = {"line": i, "counts": {}}
            elif cmd in _ROUTES:
                check_unterminated()
                end_roll()
                if st["branch"] is not None:
                    st["route"] = _ROUTES[cmd]
                    st["branch"]["counts"].setdefault(st["route"], 0)
            elif cmd == "BRANCHEND":
                check_unterminated()
                end_roll()
                finish_branch()
            continue

        if ":" in content and not in_block:
            flush_outside()
            key, val = content.split(":", 1)
            key = key.strip().upper()
            val = val.strip()
            if key == "COURSE":
                low = val.lower()
                idx = tja2sht._COURSE_ALIASES.get(low)
                cur = _Course(val, i + 1, idx)
                courses.append(cur)
                if idx is None and low not in _IGNORED_COURSES:
                    add(i + 1, "unknown_course", WARNING,
                        tr("Unknown COURSE '{v}'; the converter ignores this "
                           "chart").format(v=val),
                        tr("Use Easy, Normal, Hard, Oni or Edit."))
            elif key == "BALLOON":
                lst = _parse_balloon(val)
                if cur is not None:
                    cur.balloon = (lst, i + 1)
                else:
                    gballoon = (lst, i + 1)
            elif cur is not None:
                cur.headers[key] = (val, i + 1)
            else:
                gheaders[key] = (val, i + 1)
            continue

        if not in_block:
            if ":" not in content and _NOTE_LIKE.match(content):
                if outside_run and outside_run[-1] != i - 1:
                    flush_outside()
                outside_run.append(i)
            continue
        if st is None or ":" in content:
            continue

        # ---- note data inside a chart --------------------------------
        odd = sorted({c for c in content if not c.isspace()
                      and c not in _NOTE_CHARS})
        if odd:
            fixed = _normalize_note_line(rawline)
            fixable = all(c in _NOTE_CHARS or c.isspace() for c in
                          fixed.split("//", 1)[0])
            add(i + 1, "odd_chars", WARNING,
                tr("Odd character(s) in note line: {chars}; the converter "
                   "turns them into 0").format(chars=" ".join(odd)),
                tr("Notes are 0-9, A, B and the comma."),
                [("sub", i, _normalize_note_line)] if fixable else [],
                tr("Replace full-width digits / commas with plain ones.")
                if fixable else "")
        for ch in content.upper():
            if ch == ",":
                st["buf"] = 0
                st["measures"] += 1
                if st["branch"] is not None and st["route"] is not None:
                    c = st["branch"]["counts"]
                    c[st["route"]] = c.get(st["route"], 0) + 1
                continue
            if ch not in _NOTE_CHARS:
                if ch.isspace():
                    continue
                ch = "0"
            st["buf"] += 1
            st["last_note"] = i
            if ch in "79":
                cur.balloons += 1
            if ch in "5679":
                if st["roll"] is not None:
                    add(i + 1, "roll_nested", WARNING,
                        tr("A roll starts before the previous one was "
                           "closed with 8"),
                        tr("Close the earlier roll / balloon with 8 first."))
                    # report the first only
                    st["roll"] = i
                else:
                    st["roll"] = i
            elif ch == "8":
                st["roll"] = None
    flush_outside()
    if in_block and not skip and st is not None:
        add(len(lines), "no_end", WARNING,
            tr("Chart has no #END"), tr("Add #END after the last measure."))
        finish_block(len(lines))

    # ---- per course: LEVEL / BALLOON ----------------------------------
    for c in courses:
        if not c.chart_seen or c.idx is None:
            continue
        lv = c.headers.get("LEVEL") or gheaders.get("LEVEL")
        label = c.name or tr("(none)")
        if lv is None:
            add(c.line or (c.start_idx or 0) + 1, "no_level", WARNING,
                tr("Course {c} has no LEVEL").format(c=label),
                tr("Add a LEVEL: line with the star count (1-10)."))
        else:
            try:
                float(lv[0])
            except ValueError:
                add(lv[1], "bad_level", WARNING,
                    tr("LEVEL '{v}' is not a number").format(v=lv[0]),
                    tr("Use a whole number such as LEVEL:8."))
        has_branch_lists = any(k in c.headers or k in gheaders for k in
                               ("BALLOONNOR", "BALLOONEXP", "BALLOONMAS"))
        own = c.balloon
        eff = own or gballoon
        have = len(eff[0]) if eff else 0
        if has_branch_lists:
            continue
        pad_to = c.balloons
        if c.balloons > have:
            vals = list(eff[0]) if eff else []
            fill = vals[-1] if vals else DEFAULT_PAD
            vals += [fill] * (pad_to - have)
            if own:
                edits = [("sub", own[1] - 1, _replace_balloon(vals))]
            else:
                edits = [("ins", c.start_idx, "BALLOON:" + ",".join(
                    str(v) for v in vals))]
            ln = own[1] if own else (eff[1] if eff else
                                     (c.start_idx or 0) + 1)
            add(ln, "balloon_short" if eff else "balloon_missing", WARNING,
                tr("Course {c}: BALLOON has {have} value(s) but the chart has "
                   "{want} balloon note(s)").format(c=label, have=have,
                                                    want=c.balloons),
                tr("Give one hit count per balloon note, in chart order."),
                edits,
                tr("Pad BALLOON with {v} up to {n} value(s) (the last value, "
                   "or {d} when there is none).").format(
                       v=fill, n=pad_to, d=DEFAULT_PAD))
        elif own and have > c.balloons:
            add(own[1], "balloon_long", INFO,
                tr("Course {c}: BALLOON has {have} value(s) but the chart has "
                   "only {want} balloon note(s)").format(c=label, have=have,
                                                         want=c.balloons),
                tr("Remove the extra values (they are unused)."))

    # ---- OFFSET sign ----------------------------------------------------
    try:
        _check_offset(text, add, gheaders, courses)
    except Exception:
        pass

    # ---- WAVE ---------------------------------------------------------
    if base_dir is not None:
        wave = gheaders.get("WAVE")
        if wave is None:
            for c in courses:
                if "WAVE" in c.headers:
                    wave = c.headers["WAVE"]
                    break
        if wave is None:
            add(0, "no_wave", INFO, tr("No WAVE: header"),
                tr("Add WAVE:<audio file> so players find the music."))
        elif wave[0] and not (Path(base_dir) / wave[0]).is_file():
            add(wave[1], "wave_missing", WARNING,
                tr("WAVE file '{f}' is not next to the TJA").format(f=wave[0]),
                tr("Copy the audio beside the TJA or fix the WAVE: name."))

    # ---- encoding ---------------------------------------------------------
    if raw is not None:
        _, enc = decode_guess(raw)
        if enc == "cp932":
            add(0, "encoding_sjis", INFO,
                tr("File is Shift-JIS (not UTF-8)"),
                tr("Fine for the converter; keep it unless titles look broken."))
        elif enc is None:
            add(0, "encoding_unknown", WARNING,
                tr("File is neither UTF-8 nor Shift-JIS"),
                tr("Re-save it as UTF-8 or Shift-JIS."))
    for key in ("TITLE", "SUBTITLE"):
        v = gheaders.get(key)
        if v is None:
            continue
        fixed = _demojibake(v[0])
        if fixed is not None:
            whole = _demojibake(text)
            add(v[1], "mojibake", WARNING,
                tr("{k} looks garbled (UTF-8 read with the wrong encoding): "
                   "{v}").format(k=key, v=v[0]),
                tr("Probably \"{good}\"; re-save the file as UTF-8.").format(
                    good=fixed),
                [("recode", _demojibake_all)] if whole is not None else [],
                tr("Re-read the whole file as UTF-8 text.")
                if whole is not None else "")
            break
        if "�" in v[0]:
            add(v[1], "mojibake", WARNING,
                tr("{k} contains broken characters: {v}").format(k=key, v=v[0]),
                tr("Re-save the file as UTF-8 or Shift-JIS."))
            break

    issues.sort(key=lambda x: (x.line, x.rule))
    return issues


def _demojibake_all(s):
    out = _demojibake(s)
    return s if out is None else out


def _check_offset(text, add, gheaders, courses):
    """First note before audio start: audio time = chart time - OFFSET, so a
    positive OFFSET larger than the first note's chart time is a wrong sign."""
    parsed = tja2sht.parse_tja(text)
    for cidx in sorted(parsed["courses"]):
        c = parsed["courses"][cidx]
        meta = c["meta"]
        off = tja2sht._to_float(meta.get("OFFSET", parsed["meta"].get("OFFSET", "0")))
        if off <= 0:
            continue
        t = 0.0
        t_note = None
        for m in c["measures"]:
            if m.get("branch", tja2sht.BRANCH_COMMON) not in (
                    tja2sht.BRANCH_COMMON, tja2sht.BRANCH_NORMAL):
                continue
            t += m.get("delay_ms", 0.0) / 1000.0
            bpm = m["bpm"] if m["bpm"] > 0 else 120.0
            beats = m.get("beats")
            if beats is None:
                beats = 4.0 * m["measure_num"] / float(m["measure_den"] or 4)
            sec = beats * 60.0 / bpm
            ns = m["notes"]
            for k, ch in enumerate(ns):
                if ch in "12345679AB":
                    t_note = t + sec * k / float(len(ns))
                    break
            if t_note is not None:
                break
            t += sec
        if t_note is None or t_note - off >= -0.0005:
            continue
        line = 0
        for cc in courses:
            if cc.idx == cidx and "OFFSET" in cc.headers:
                line = cc.headers["OFFSET"][1]
        if not line and "OFFSET" in gheaders:
            line = gheaders["OFFSET"][1]
        add(line, "offset_sign", WARNING,
            tr("First note is {s:.2f} s before the music starts (OFFSET "
               "{o:g}); the sign of OFFSET may be wrong").format(
                   s=off - t_note, o=off),
            tr("If the music starts later than the chart, OFFSET is "
               "negative: try OFFSET:{v:g}").format(v=-off))
        return
