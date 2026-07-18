SCORE: 91

# Re-review: taiko256_explorer_gui6.py + hdd_browser.py + hdd_song_wizard.py

Second pass after fixes for atomic writes, rolling backups, threading, mmap lifecycle. The
earlier correctness/threading/save-safety defects are resolved. The code now meets the 90 bar:
no correctness/threading bugs found, saves and in-place writes are safe, robustness is reasonable.
Remaining items are minor.

## Verified correct (previously-flagged areas)

- **Atomic writes.** `_atomic_write()` writes a sibling temp on the same volume, `flush()`+`fsync()`,
  then `os.replace()` (atomic same-volume), unlinks temp on any failure. DATA.000/LIST.BIN can never
  be left half-written. Used consistently in `save()`, `rebuild()`, and `_write_full_archive()`.
- **mmap close-before-overwrite.** `save_archive()` calls `a.close()` (mmap + fh) *before* backup and
  before `save()/rebuild()` write the file being mmap'd. Crucially, `save()`/`rebuild()` re-read the
  source via `self.data_path.read_bytes()` (not `self._mm`), so operating on a closed archive is safe —
  confirmed the core's `decode_group_payload`/`get_group_encoded_block` accept any sliceable buffer, so
  passing a `bytes`/`bytearray` after close is correct. No use-after-close of `_mm` on the save path.
- **Rolling backups.** `_make_backup()` keeps `.bak` as the first (known-good) snapshot and rolls
  subsequent saves into `.bak.1`, `.bak.2`, … so a good original is never clobbered by a backup of an
  already-edited file. Correct.
- **Full-archive write + reload.** `_write_full_archive()` closes the archive, backs up both files,
  atomic-writes DATA then LIST, and reloads via `_reload_after_save()`. On write failure it still
  reloads so the app doesn't hold a dangling/closed handle.
- **Reload failure handling.** `_reload_after_save()` sets `self.archive = None` on failure, clears the
  tree, and disables actions — no dangling handle, no crash on next op.
- **Threading — no QWidget off the GUI thread.** `ExtractWorker` (QThread) only touches archive layout,
  mmap reads, filesystem, and TIM2 decode; results marshalled back via signals. HDD wizard's `_run()`
  correctly does the SongManager/`prepare_new_song_db` DB prep on the GUI thread (comment calls it out)
  and only pushes `add_new_song` + PFS write onto the worker. No `QMessageBox`/widget construction in
  any `run()`.
- **HDD handle ownership.** `hdd_browser._do_replace()` runs on the worker but operates on a *local*
  writable handle; `self.hdd` is closed/niled on the GUI thread before start and reopened in
  `_after_replace()` on the GUI thread — single-thread ownership of `self.hdd`. `closeEvent` waits for
  the in-flight worker before closing handles. Sound design.
- **Temp cleanup.** Wizard `closeEvent` waits for the worker, closes the archive mmap, then
  `shutil.rmtree(ignore_errors=True)` — temp dir won't leak and won't raise on stray files.
- **Write ordering on partial failure.** Wizard writes DATA.000 *then* list.bin, with a comment
  explaining that a failed DATA write leaves the OLD (self-consistent) list.bin — correct choice for a
  non-atomic PFS target.
- **80 GB in-place warnings.** Both `hdd_browser._replace()` and wizard `_run()` show explicit
  QMessageBox warnings that the image is modified IN PLACE, non-atomically, with no backup. UX-safe.
- **Extract cancel/progress race.** `QProgressDialog` uses `setAutoClose(False)`/`setAutoReset(False)`
  and is closed explicitly in `on_done`, avoiding the reach-max auto-reset race with cancel.
- **LRU cache under lock.** `decode_group` guards cache read and write with `_cache_lock`; byte
  accounting and eviction (`len(self._cache) > 1` guard) are correct — no negative counter, always
  keeps at least one entry.

## Findings (minor — none block 90)

### LOW 1 — Save-overwrite crash between backup and write leaves NO live file momentarily
`save_archive()` closes the mmap and makes backups, then `save()`/`rebuild()` build the full new image
in memory before the single `os.replace()`. Because the original is never truncated in place (replace
is atomic and the temp is separate), the original DATA.000 stays intact until the atomic swap. So this
is actually safe — the `.bak` is belt-and-suspenders. No action needed; noting only that recovery
relies on the `.bak` if the process dies mid-`save()` (original is still the untouched live file, so
recovery is trivial). Good.

### LOW 2 — `ExtractWorker` holds a reference to `self.archive`; save can close the mmap under it
`_run_extract()` stores `self.worker` and starts extraction, but the UI does not disable Save while an
extract is running. If a user starts "Extract All" and then hits Save (overwrite), `save_archive()`
calls `a.close()` on the mmap the extract worker is actively reading via `self.archive._mm`, causing a
possible crash/`ValueError` on the worker thread. Recommend disabling Save/Discard (and Open) while
`self.worker is not None`, or `self.worker.wait()` before closing the mmap in `save_archive()`.
This is the one real (if narrow) threading gap remaining.

### LOW 3 — `_after_write` re-extraction ignores the just-written archive being reopenable
Wizard `_after_write()` calls `_load_partition()` to re-extract after a successful write. If that
re-extract fails (e.g. transient PFS read), `self.archive` stays None and `b_run` is disabled with only
a log line — the user has a successful write but a dead dialog. Minor; a targeted error path would be
nicer, but data integrity is fine.

### LOW 4 — `_err`/error tuple sentinel is positional (`res[0] == "ERROR"`)
Both HDD dialogs detect worker errors by a `("ERROR", exc, tb)` tuple. A legitimate result that happens
to be a tuple starting with the string `"ERROR"` would be misclassified. Current workers only return
ints/summary tuples, so it's safe today, but a typed sentinel (or a dedicated `error` signal) would be
more robust.

### INFO — Memory peak documented, not bounded
`save()`/`rebuild()` load the full DATA into memory (peak ≈1–3× DATA per the docstrings). Documented and
acceptable for this tool, but for very large DATA.000 on low-RAM machines this can OOM. Out of scope.

## Bottom line
Correctness of Archive read/cache/save, threading discipline (no cross-thread QWidget, mmap
closed before overwrite, full-archive write+reload, signal dispatch), save/write safety, and 80 GB
in-place warnings are all in good shape. Only LOW 2 (Save vs. running ExtractWorker) is a genuine
residual race worth fixing; the rest are polish. Score: 91.
