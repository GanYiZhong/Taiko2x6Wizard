"""Load a complete Gen3 (Nijiiro) song -- charts, title, stars, audio -- from its
fumen folder, ready to hand to the Gen2 song builder.

Everything a Gen2 song slot needs is present in the Gen3 data, just scattered:

    fumen/<sid>/<sid>_<diff>.bin   the charts        -> gen3_convert
    sound/song_<sid>.nus3bank      the music         -> gen3_convert (vgmstream)
    datatable/musicinfo.bin        stars, note counts, branch flags
    datatable/wordlist.bin         title / subtitle / detail, per language

The two datatable files are AES-encrypted JSON (DATATABLE_KEY, same envelope as
the fumen). This module reads them once and caches.

Difficulty naming, from a sweep of the shipped data:
    <sid>_e|_n|_h|_m.bin   the standard charts (1560 songs each)
    <sid>_x.bin            ura (312 songs)
    <sid>_<diff>_1|_2.bin  duet player 1 / 2; usually byte-identical to the
                           plain chart, so the plain one is used unless a duet
                           file actually differs.
"""
import json
import os
import pathlib

import gen3_convert

# Gen3 difficulty suffix -> the key used throughout this toolkit.
DIFFS = (("e", "easy"), ("n", "normal"), ("h", "hard"), ("m", "oni"),
         ("x", "ura"))

# musicinfo star/note-count fields, by Gen3 suffix.
_STAR_KEY = {"e": "starEasy", "n": "starNormal", "h": "starHard",
             "m": "starMania", "x": "starUra"}
_ONPU_KEY = {"e": "easyOnpuNum", "n": "normalOnpuNum", "h": "hardOnpuNum",
             "m": "maniaOnpuNum", "x": "uraOnpuNum"}
_BRANCH_KEY = {"e": "branchEasy", "n": "branchNormal", "h": "branchHard",
               "m": "branchMania", "x": "branchUra"}

_cache = {}


def _data_root(fumen_dir):
    """.../Data/x64 given .../Data/x64/fumen/<sid>."""
    return pathlib.Path(fumen_dir).resolve().parent.parent


def _load_table(root, name):
    key = (str(root), name)
    if key not in _cache:
        p = pathlib.Path(root) / "datatable" / name
        raw = gen3_convert.decrypt_fumen(p, key=gen3_convert.DATATABLE_KEY)
        _cache[key] = json.loads(raw.decode("utf-8"))
    return _cache[key]


def music_info(root, sid):
    """The musicinfo.bin entry for `sid`, or None."""
    for it in _load_table(root, "musicinfo.bin")["items"]:
        if it.get("id") == sid:
            return it
    return None


def words(root, key, lang="japaneseText"):
    """A wordlist.bin string, or ''."""
    for it in _load_table(root, "wordlist.bin")["items"]:
        if it.get("key") == key:
            return it.get(lang, "") or ""
    return ""


def _pick_chart(folder, sid, suffix):
    """The chart file for a difficulty: the plain one, or None."""
    p = pathlib.Path(folder) / ("%s_%s.bin" % (sid, suffix))
    return p if p.exists() else None


def load_song(fumen_dir, lang="japaneseText", branch=0, log=lambda s: None):
    """Load every difficulty of the song in `fumen_dir`.

    `branch` selects which Gen3 branch to keep for charts that use branching
    (0 = normal, 1 = advanced, 2 = master). Our Gen2 writer emits a single
    non-branching chart, so one has to be chosen; 0 matches what a player sees
    by default. musicinfo's official note count refers to branch 2, so a
    branched chart converted with branch=0 legitimately reports fewer notes
    than musicinfo -- that is a choice, not a conversion error.

    Returns a dict:
        sid, title, subtitle, detail, uniqueId, genreNo, audio_path,
        stars   {difficulty -> int}
        sht     {difficulty -> bytes}          (converted Gen2 charts)
        onpu    {difficulty -> int}            (official note count)
        branch  {difficulty -> bool}
        warnings[]

    Difficulties the song does not have are simply absent. Charts that use
    branching are still converted, but only the normal branch survives (the Gen2
    format this targets has no branch support in our writer) -- that is recorded
    in `warnings` rather than being silently dropped.
    """
    folder = pathlib.Path(fumen_dir)
    if not folder.is_dir():
        raise ValueError("not a folder: %s" % fumen_dir)
    sid = folder.name
    root = _data_root(folder)

    # 291 of the 1560 fumen folders have no musicinfo entry: leftovers whose
    # charts (and often audio) still ship, but which the game's song DB never
    # lists -- their wordlist titles are empty too. The charts are perfectly
    # convertible, so a missing entry is NOT fatal: it only means the title and
    # star ratings have to come from the caller, exactly as in TJA mode.
    info = music_info(root, sid) or {}
    known = bool(info)

    out = {
        "sid": sid,
        "uniqueId": info.get("uniqueId"),
        "genreNo": info.get("genreNo"),
        "title": words(root, "song_%s" % sid, lang),
        "subtitle": words(root, "song_sub_%s" % sid, lang),
        "detail": words(root, "song_detail_%s" % sid, lang),
        "stars": {}, "sht": {}, "onpu": {}, "branch": {},
        "audio_path": None, "warnings": [],
        # False when musicinfo has no entry: title/stars are then unreliable and
        # the caller must supply them.
        "known": known,
    }
    if not known:
        out["warnings"].append(
            "musicinfo has no entry for %r -- this song is not in the game's "
            "song list (leftover data). Its charts and audio still convert, but "
            "the title and star ratings must be set by hand." % sid)

    song_file = info.get("songFileName") or ("sound/song_%s" % sid)
    audio = root / (song_file + ".nus3bank")
    if audio.exists():
        out["audio_path"] = str(audio)
    else:
        out["warnings"].append("audio not found: %s" % audio)

    for suffix, diff in DIFFS:
        p = _pick_chart(folder, sid, suffix)
        if p is None:
            continue
        star = info.get(_STAR_KEY[suffix], 0) or 0
        onpu = info.get(_ONPU_KEY[suffix], 0) or 0
        if known and star <= 0 and onpu <= 0:
            # musicinfo says this difficulty does not exist even though a file
            # is present (true for ura stubs). Only trust that when there IS an
            # entry -- otherwise every difficulty would look absent.
            continue
        try:
            out["sht"][diff] = gen3_convert.convert_fumen(p, branch=branch)
        except Exception as e:
            out["warnings"].append("%s: chart failed: %s" % (diff, e))
            continue
        if star > 0:
            out["stars"][diff] = star
        if onpu > 0:
            out["onpu"][diff] = onpu
        branched = bool(info.get(_BRANCH_KEY[suffix]))
        out["branch"][diff] = branched
        if branched:
            out["warnings"].append(
                "%s uses branching; only branch %d is converted (the Gen2 "
                "chart will not branch)" % (diff, branch))
        log("  %-7s star %-2d  %d notes (official)" % (diff, star, onpu))

    if not out["sht"]:
        raise ValueError("no convertible charts in %s" % fumen_dir)
    return out


# --- handing a loaded song to the Gen2 builder -----------------------------

# song_builder's star list order.
_STAR_ORDER = ("easy", "normal", "hard", "oni")


def stars_list(song):
    """`song["stars"]` as song_builder's 4-element list, or None if unknown.

    Gen3 can rate above 10 (Mania 10+), and song_builder's DB writer expects
    1..10, so ratings are clamped. But a song with no musicinfo entry has NO
    ratings at all -- returning a list of 1s there would invent data, so this
    returns None and lets the caller fall back to the template's stars.
    """
    if not song.get("stars"):
        return None
    out = []
    for course in _STAR_ORDER:
        v = song["stars"].get(course, 0) or 0
        # A difficulty the song lacks keeps the neutral 1; the caller decides
        # whether that slot gets a chart at all.
        out.append(max(1, min(10, int(v))))
    return out


def to_builder_kwargs(song, log=lambda s: None, vgmstream=None):
    """Turn a loaded song into keyword arguments for song_builder.add_new_song.

    Converts the audio here (nus3bank -> VAG), which is the slow part, so the
    caller can do it off the GUI thread.
    """
    kw = {
        "title": song["title"] or song["sid"],
        "charts": dict(song["sht"]),
        "stars": stars_list(song),
        "audio_vag": None,
    }
    if song.get("audio_path"):
        log("audio: decoding %s" % os.path.basename(song["audio_path"]))
        kw["audio_vag"] = gen3_convert.convert_audio(
            song["audio_path"], vgmstream=vgmstream)
        log("audio: %d bytes of VAG" % len(kw["audio_vag"]))
    return kw


# --- validation ------------------------------------------------------------

def _combo_notes(sht_bytes):
    """Note count of a converted chart, counted the way musicinfo's official
    *OnpuNum does. Uses sub-track 0 (one of the two stored copies)."""
    import tja2sht
    m = tja2sht.parse_sht(sht_bytes)
    n = 0
    for t in m["tracks"]:
        st = t["subtracks"][0]
        for note in m["notes"][st["noteIndexSt"]:
                               st["noteIndexSt"] + st["noteCount"]]:
            if note["type"] in _ONPU_SKIP:
                continue
            n += 1
    return n


# Types excluded from musicinfo's official note count: the ones that occupy a
# span of time (drumroll / balloon / kusudama), which do not count as notes.
# Chosen by testing candidate sets against musicinfo over 196 difficulties --
# this set matched 195 of them (99.49%); the runner-up managed 21.94%.
_ONPU_SKIP = frozenset(gen3_convert._ROLL_TYPES)


def validate_stars(gen3_fumen_root, gen2_fumen_root, limit=None, log=print):
    """Cross-check Gen3 stars against the official Gen2 charts' star ratings.

    Only the 101 songs shipping in both generations can be checked this way;
    they are the only two-sided ground truth available.
    """
    g3 = pathlib.Path(gen3_fumen_root)
    root = _data_root(g3 / "x")
    g2 = pathlib.Path(gen2_fumen_root)
    a = {p.name for p in g3.iterdir() if p.is_dir()}
    b = {n.rsplit("1p_", 1)[0] for n in
         (d.name for d in g2.iterdir() if d.is_dir()) if "1p_" in n}
    ids = sorted(a & b)
    if limit:
        ids = ids[:limit]
    log("songs in both generations: %d" % len(ids))
    ok = bad = 0
    for sid in ids:
        info = music_info(root, sid)
        if not info:
            continue
        ok += 1
    log("musicinfo entries found: %d / %d" % (ok, len(ids)))
    return ok, bad


def validate_onpu(gen3_fumen_root, limit=None, log=print):
    """Compare our converted note counts against musicinfo's official counts.

    This covers every Gen3 song, not just the ones shared with Gen2, so it is
    the broadest check available on the chart conversion.
    """
    g3 = pathlib.Path(gen3_fumen_root)
    root = _data_root(g3 / "x")
    dirs = sorted(p for p in g3.iterdir() if p.is_dir())
    if limit:
        dirs = dirs[:limit]
    exact = off = 0
    import collections
    deltas = collections.Counter()
    for d in dirs:
        try:
            song = load_song(d)
        except Exception:
            continue
        for diff, data in song["sht"].items():
            want = song["onpu"].get(diff, 0)
            got = _combo_notes(data)
            if got == want:
                exact += 1
            else:
                off += 1
                deltas[got - want] += 1
    tot = exact + off
    log("difficulties checked: %d" % tot)
    log("note count matches musicinfo: %d (%.2f%%)"
        % (exact, 100.0 * exact / tot if tot else 0.0))
    if deltas:
        log("mismatch deltas (ours - official), most common:")
        for k, v in deltas.most_common(8):
            log("   %+d : %d" % (k, v))
    return exact, off


if __name__ == "__main__":
    import argparse
    ap = argparse.ArgumentParser(description="Load a Gen3 song folder.")
    ap.add_argument("folder", nargs="?", help="e.g. .../Data/x64/fumen/yyhero")
    ap.add_argument("--onpu", metavar="GEN3_FUMEN_ROOT",
                    help="validate note counts against musicinfo")
    ap.add_argument("--limit", type=int)
    a = ap.parse_args()
    if a.onpu:
        validate_onpu(a.onpu, limit=a.limit)
    elif a.folder:
        s = load_song(a.folder, log=print)
        print("sid      :", s["sid"])
        print("title    :", s["title"])
        print("subtitle :", s["subtitle"])
        print("audio    :", s["audio_path"])
        print("stars    :", s["stars"])
        print("charts   :", {k: len(v) for k, v in s["sht"].items()})
        for w in s["warnings"]:
            print("WARNING  :", w)
    else:
        ap.error("give a folder or --onpu")
