SCORE: 85

# Round 2 review — bineditor_*.py (8 modules)

Scope: `enso_parts, lamp, musicinfo, rank, tuning, fname, hdbdinfo, streaminfo`.
Method: static read + an executable harness that round-trips each real sample bin
and exercises length-changing / in-place string edits and reparse stability.
(Reviewed read-only; the harness imports the modules as libraries, no edits made.)

## Verdict

7 of 8 modules are clean and would individually score 90+. **One real
correctness bug in `musicinfo` (data loss on any string edit)** caps the SET at
85: the "no correctness bugs" bar for 90 is not met. The bug does not affect the
unedited `serialize(parse(d)) == d` guarantee — only the edit path — so it is
contained, hence high-80s rather than lower.

## Harness results (real sample bins)

Round-trip `serialize(parse(d)) == d`: **PASS for all 8.**
Interface (`FILENAME`/`parse`/`serialize`/`Editor` + `.result_bytes`): **PASS for all 8.**
tuning: `structured=True`, 180 blocks discovered (marker scan + validation OK).

Edit-path checks:
- enso_parts: length-growing pooled-string edit 44435->44442, reparse-stable,
  all 1244 pool parts preserved (unreferenced strings NOT dropped). CORRECT.
- fname: shorter UTF-16 name edit — size preserved, edit isolated to its slot,
  no leak into neighbours. CORRECT.
- hdbdinfo: in-place shorten round-trips; grow-past-capacity raises. CORRECT.
  (An earlier "fail" was a test artifact — the harness mutated the reference
  string before comparing; re-tested with a snapshot: PASS.)
- streaminfo: in-place shorten round-trips, reparse-stable; capacity guard OK.
  CORRECT. (Same test-artifact caveat as hdbdinfo.)
- lamp / rank / tuning: round-trip + edit paths clean.

## CRITICAL — musicinfo: pool rebuild silently drops unreferenced strings

`bineditor_musicinfo.py` `serialize()` (the `dirty_strings` branch, ~L255-311).

On ANY string edit the pool is rebuilt exclusively from the `StrRef` list, which
covers only the pool-relative pointers in **SEC3 / SEC4 / SEC5**. The sample
pool is 3800 bytes; only ~1004 bytes are reachable through those pointers. The
remaining ~2796 bytes — **180 song-title strings** ("POP STAR", "GLAMOROUS SKY",
"yuzu2", "nana", ...) at pool offsets >=66 — are not enumerated by any StrRef and
are discarded.

Proof (forced rebuild with UNCHANGED text, i.e. a pure no-op edit):
`dirty_strings=True` then serialize => **12284 -> 9488 bytes, 2796 bytes lost.**
A real one-char edit gives 12284 -> 9494. The reparse of the shrunken file is
self-consistent (so the harness's naive "len_edit_stable" check passes), which
makes the loss silent — the file simply comes back smaller and missing titles.

Why it matters: those titles are almost certainly indexed by SEC0 (the 90-row
per-song main table) or other offset fields; even if some are dead, dropping 74%
of the pool on a single unrelated gallery-name edit is data corruption. The
module's own docstring promises "Unreferenced strings are preserved" — this
contract is violated.

Fix direction: the pool must be rebuilt from the full set of pool strings (walk
the original pool, keep every string, and only relocate/append the edited ones),
mirroring how `enso_parts.apply_string_edits` preserves all `pool_parts` and how
`lamp` explicitly preserves trailing unreferenced pool bytes. As written, only
`musicinfo` throws pool content away. Note `enso_parts` solves the exact same
problem correctly and passes the 1244-parts-preserved check — use it as the
reference implementation.

## Non-blocking notes

- musicinfo: SEC0 columns are edited as raw i32 with no pool-offset awareness; if
  any SEC0 field is in fact a pool pointer, it is neither surfaced nor fixed up on
  a pool rebuild (compounds the bug above). Worth confirming SEC0 has no pool refs.
- musicinfo dedup on shared pool offsets (offsets 0/8/16/36/... referenced 5-7x)
  is handled correctly — verified reparse-stable — so string sharing is fine; the
  defect is strictly the dropped unreferenced tail.
- tuning: `_scan_blocks` returns `[]` on the no-marker path but `(starts, bounds)`
  otherwise; `parse` guards with `if scan:` so the shape mismatch is safe, but the
  heterogeneous return type is a latent footgun. Cosmetic.
- rank: `count`/`stride` header fields are (correctly) read-only since the body is
  a fixed 11-row grid; reserved words remain editable. Consistent and safe.
- fname: documented trailing-U+3000 limitation is acknowledged and benign; unedited
  slots re-emit raw bytes so it never corrupts on round-trip. Good.
- Shared interface, latin1 lossless decode, malformed/truncated fallbacks
  (raw_fallback / structured=False / clamped headers), and u32/int32 range
  validation are consistent and solid across the set.

## To reach >=90

Fix the musicinfo pool rebuild to preserve all unreferenced pool strings on edit
(model it on enso_parts). With that single fix the SET has no correctness bugs,
reasonable robustness, and a consistent interface — a clear 90+.
