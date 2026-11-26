"""zh-TW wording rules: the glossary at the top of taiko2x6/app/i18n.py, enforced.

Only the translations are checked here; the English keys are the source strings
(test_i18n.py makes sure every tr() literal has an entry).
"""
import ast
import re
import string
from collections import Counter
from pathlib import Path

import pytest

from taiko2x6.app import i18n

ZH = i18n.ZH_TW
I18N_SRC = Path(i18n.__file__).read_text(encoding="utf-8")
CJK = "一-鿿"
FULL_PUNCT = "，。、；：？！（）「」"

# Values that legitimately equal their English key (names, tokens, layout strings).
SAME_AS_KEY = {"ID", "TJA…", "vgmstream", "pfsshell", "#", "A", "B", "ISO (*.iso)",
               "BPM         : {b}", "+{a} -{r} ~{c} ={u}", "[{i}/{n}] {sid} — {title}"}

# (pattern, what to use instead). Patterns are searched in every zh-TW value.
BANNED = [
    (r"封包|封存(?!檔)", "封存檔 (archive)"),
    (r"映像(?!檔)", "映像檔 (disk image)"),
    (r"譜(?!面)", "譜面 (chart)"),
    (r"[Pp]atch", "修補 (patch)"),
    (r"製作(?!人員)", "建置 (build) / 建立 (create)"),
    (r"取代", "替換 (replace)"),
    (r"覆蓋", "覆寫 (overwrite)"),
    (r"還原(記錄|日誌)", "復原日誌 (undo journal)"),
    (r"保存", "儲存 (save)"),
    (r"(?<![封遊戲暫])存檔(?!案)", "儲存 (save); 封存檔 is the archive"),
    (r"紀錄", "記錄 (record / log)"),
    (r"選曲", "選歌 (song select)"),
    (r"防拷", "加密狗 (dongle)"),
    (r"街機|筐體", "機台 (arcade machine)"),
    (r"執行檔|執行檔案", "主程式 (the taiko executable)"),
    (r"材質|紋理", "貼圖 (texture)"),
    (r"歌名圖|歌曲名貼圖", "曲名圖 (song-title texture)"),
    (r"歌曲數量", "歌曲數"),
    (r"歌曲限制|曲數限制", "歌曲上限 (song limit)"),
    (r"硬盤|磁盤", "硬碟 / 磁碟"),
    (r"軟件|程序(?!員)", "軟體 / 程式"),
    (r"默認|缺省", "預設 (default)"),
    (r"設置", "設定 (settings)"),
    (r"信息", "資訊 / 訊息"),
    (r"文件(?!夾)", "檔案 (file)"),
    (r"文件夾|檔案夾|(?<!根)目錄", "資料夾 (folder); 根目錄 is the only 目錄"),
    (r"音頻|音檔", "音訊 (audio)"),
    (r"窗口", "視窗 (window)"),
    (r"屏幕", "螢幕 (screen)"),
    (r"您", "你 (second person)"),
    (r"[“”‘’\"]", "「」 for quoted names (no curly or straight double quotes)"),
]


def _fields(s):
    return sorted(f for _, f, _, _ in string.Formatter().parse(s) if f is not None)


def _protected(s):
    """Drop backslash escapes so \\n never looks like a Latin 'n'."""
    return re.sub(chr(92)*2+".|;;", chr(0), s)


def _bad(pred):
    return sorted(f"{k!r} => {v!r}" for k, v in ZH.items() if pred(k, v))


def test_glossary_banned_variants():
    problems = []
    for pat, use in BANNED:
        hits = [k for k, v in ZH.items() if re.search(pat, v)]
        if hits:
            problems.append(f"{pat!r} -> use {use}: {hits[:5]}")
    assert not problems, "\n".join(problems)


def test_no_empty_or_untranslated_values():
    bad = [k for k, v in ZH.items() if not v.strip() or (k == v and k not in SAME_AS_KEY)]
    assert not bad, bad


def test_placeholders_preserved():
    bad = [k for k, v in ZH.items() if _fields(k) != _fields(v)]
    assert not bad, bad
    # escapes must match too: a lost \n changes a dialog's layout
    bad = [k for k, v in ZH.items() if k.count("\n") != v.count("\n")
           and k not in SAME_AS_KEY]
    assert not bad, bad


def test_ellipsis_style():
    assert not _bad(lambda k, v: "..." in v or "⋯" in v), "use the single character …"
    # key ends with … <=> value ends with …  (trailing "..." in a key counts as …)
    ends = lambda s: s.rstrip().endswith(("…", "..."))
    assert not _bad(lambda k, v: ends(k) != ends(v)), "…: key and value must agree"
    # the English source never ends in three dots (the glyph is …)
    assert not [k for k in ZH if k.rstrip().endswith("...")], "English keys end with …"
    assert not _bad(lambda k, v: re.search(r"\s…\s*$", v)), "no space before the final …"
    assert not _bad(lambda k, v: v.rstrip().endswith("。…") or "…。" in v)


def test_sentence_final_punctuation_follows_key():
    for a, b in ((".", "。"), (":", "："), ("?", "？"), ("!", "！")):
        bad = _bad(lambda k, v: re.search(r"[%s]" % CJK, v)
                   and k.rstrip().endswith(a) != v.rstrip().endswith(b)
                   and not k.rstrip().endswith("..."))
        assert not bad, bad


def test_fullwidth_punctuation_in_chinese_text():
    p = lambda v: _protected(v)
    # ASCII , ; : ? ! touching a CJK character
    bad = _bad(lambda k, v: re.search(r"[%s]\s?[,;:?!]|(?<![A-Za-z0-9 ])[,;:?!]\s?[%s]" % (CJK, CJK), p(v)))
    assert not bad, bad
    # ASCII parentheses around Chinese (accelerator "(&X)" and Qt filters "(*.x)" are fine)
    bad = _bad(lambda k, v: re.search(r"\((?![&*])[^()]*[%s][^()]*\)" % CJK, p(v))
               or re.search(r"[%s] ?\((?![&*])" % CJK, p(v)))
    assert not bad, bad
    # a sentence ending in ASCII '.' after Chinese
    bad = _bad(lambda k, v: re.search(r"[%s]\.\s*$" % CJK, p(v)))
    assert not bad, bad
    assert not _bad(lambda k, v: " — " in v or " -> " in v or "▸" in v), "use ——, →, >"


def test_spaces_around_cjk_and_latin():
    p = lambda v: _protected(v)
    # a space between Chinese and a Latin word / number: 第 3 首, PS2 硬碟 (not 第3首)
    bad = _bad(lambda k, v: re.search(r"[%s][A-Za-z0-9`]|[A-Za-z0-9`][%s]" % (CJK, CJK), p(v)))
    assert not bad, bad
    # no space against full-width punctuation
    bad = _bad(lambda k, v: re.search(r"\s[，。、；？！）」]|[（「]\s|[%s]\s[（「]" % CJK, p(v)))
    assert not bad, bad
    assert not _bad(lambda k, v: v != v.strip() and k == k.strip()), "stray outer space"
    assert not _bad(lambda k, v: "  " in v and "  " not in k), "double space"


def test_keys_and_overrides_are_unambiguous():
    """A key written twice (or in two tables with different text) hides a translation."""
    tree = ast.parse(I18N_SRC)
    dup = []
    for n in ast.walk(tree):
        if isinstance(n, ast.Dict):
            keys = [k.value for k in n.keys if isinstance(k, ast.Constant) and isinstance(k.value, str)]
            dup += [k for k, c in Counter(keys).items() if c > 1]
    assert not dup, dup


def test_accelerator_clash_free_values():
    """tr() text with an '&' mnemonic keeps exactly one zh-TW '(&X)' and no bare '&X'."""
    for k, v in ZH.items():
        if re.search(r"(?<!&)&[A-Za-z]", k) and re.search(r"[%s]" % CJK, v):
            assert re.search(r"\(&[A-Za-z]\)", v) or not re.search(r"(?<!&)&[A-Za-z]", v), (k, v)
