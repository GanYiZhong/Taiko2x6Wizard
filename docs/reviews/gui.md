# Code Review — Taiko SYSTEM256 Explorer (PySide6)

**Files reviewed (read-only):**
- `E:\Taiko No Tatsujin 8\taiko256_explorer_gui6.py`
- `E:\Taiko No Tatsujin 8\hdd_browser.py`
- `E:\Taiko No Tatsujin 8\hdd_song_wizard.py`

**SCORE: 76 / 100**

The core read paths (mmap + byte-accounted LRU cache) and the extract threading are solid, and destructive operations are honestly warned. The score is held below 90 by non-atomic in-place writes over a single stale backup, an HDD desync-on-partial-write, and close-during-worker races — these are correctness / data-loss class bugs.

---

## CRITICAL

### C1 — Non-atomic multi-GB write over the only backup
**File:** `taiko256_explorer_gui6.py:1230-1236` (also `:1169-1173` in `_write_full_archive`, and the writes at `:185-186`, `:310-311`)
**Problem:** The overwrite path closes the mmap, copies originals to `.bak` **only `if not bak.exists()`**, then performs `out_data.write_bytes(bytes(full))` — a non-atomic, multi-GB write directly over the live file. A crash/exception mid-write leaves a truncated, corrupt DATA.000. On a *second* edit+save, no fresh backup is taken (the `.bak` still holds the long-gone original), so the only known-good copy is the one being overwritten.
**Exact fix:** Write to a temp file in the same directory and atomically swap:
```python
import tempfile, os
def _atomic_write(path: Path, data: bytes):
    d = path.parent
    fd, tmp = tempfile.mkstemp(dir=d, prefix=path.name + ".", suffix=".tmp")
    try:
        with os.fdopen(fd, "wb") as f:
            f.write(data); f.flush(); os.fsync(f.fileno())
        os.replace(tmp, path)   # atomic on same volume
    except BaseException:
        try: os.unlink(tmp)
        except OSError: pass
        raise
```
Use `_atomic_write` for `out_data`/`out_list` in `Archive.save` (`:185-186`), `Archive.rebuild` (`:310-311`), and `_write_full_archive` (`:1173-1174`). Replace the `if not bak.exists()` backup logic with a versioned backup (`.bak`, `.bak.1`, …) or always refresh it before each overwrite.

### C2 — `self.hdd` mutated on the worker thread, raced by GUI-thread `close()`
**File:** `hdd_browser.py:179-186` (mutation) vs `:219-222` (`closeEvent`)
**Problem:** `_do_replace` runs on the `_Worker` thread and does `self.hdd.close()` → reopen writable → `pfs_write` → reopen read-only, reassigning `self.hdd` from the worker thread. Meanwhile `closeEvent` (GUI thread) calls `self.hdd.close()` with no lock. If the dialog is closed during an in-flight 80 GB in-place PFS write, two threads touch/free the same handle → image corruption.
**Exact fix:** Keep all `self.hdd` access on one thread. In `_do_replace`, operate on a local handle and don't reassign `self.hdd` from the worker; do the reopen on the GUI thread in `_after_replace`. And gate `closeEvent` on the worker (see H2).

---

## HIGH

### H1 — HDD write order desyncs the partition on partial failure
**File:** `hdd_song_wizard.py:206-216`
**Problem:** The worker writes `pfs_write(part, "/list.bin", lb)` **before** `pfs_write(part, "/DATA.000", data)`. If the big DATA.000 write fails after list.bin succeeded, the partition has a NEW list.bin pointing at OLD data → desynced, unbootable archive, with no rollback. `self.archive` is also already `None` (closed at `:211`).
**Exact fix:** Write DATA.000 first, then list.bin, so a failure leaves the old (self-consistent) list pointing at old data:
```python
h.pfs_write(part, "/DATA.000", data)
h.pfs_write(part, "/list.bin", lb)
```
Better: stage both, verify both fit, and only commit after both succeed (or snapshot both files first for rollback).

### H2 — `closeEvent` does not wait for a running worker; wizard deletes in-use temp files
**File:** `hdd_browser.py:219-222`; `hdd_song_wizard.py:264-274`
**Problem:** Neither dialog's `closeEvent` waits for `self._worker`. The wizard additionally `unlink()`s the temp `list.bin`/`DATA.000` (`:268-271`) and closes `self.archive` (`:266`) — files/handles a running `add_new_song` worker may still be reading via mmap. The main window does this correctly (`taiko256_explorer_gui6.py:1361`: `self.worker.wait(2000)`).
**Exact fix:** In both `closeEvent`s, before closing handles / deleting files:
```python
if getattr(self, "_worker", None) and self._worker.isRunning():
    self._worker.wait()
```

### H3 — Reload failure leaves `self.archive` pointing at a closed mmap, actions still enabled
**File:** `taiko256_explorer_gui6.py:1262-1269`
**Problem:** `_reload_after_save` calls `self.archive = Archive(...)`; if construction raises, it shows a messagebox and `return`s, leaving `self.archive` referencing the old, already-`close()`d archive (mmap closed). Save/Extract stay enabled (`_update_actions` not called on this branch) and the next `read_file`/`decode_group` raises `mmap closed`.
**Exact fix:**
```python
def _reload_after_save(self, list_path, data_path):
    try:
        self.archive = Archive(list_path, data_path, fmt=2)
    except Exception as exc:
        self.archive = None
        QMessageBox.critical(self, "Reload failed", str(exc))
        self._populate_tree()      # clears the now-stale tree
        self._update_actions()
        return
    self._populate_tree()
    self._update_actions()
```

---

## MEDIUM

### M1 — TJA read forces UTF-8 and corrupts Shift-JIS charts
**File:** `hdd_song_wizard.py:183-184`
**Problem:** `Path(...).read_text(encoding="utf-8", errors="replace")`. Taiko TJA files are frequently Shift-JIS; `errors="replace"` silently turns Japanese title/lyricist/composer into `�` with no warning.
**Exact fix:** Read bytes and decode with fallback (matching the preview pane at `taiko256_explorer_gui6.py:457`):
```python
raw = Path(p).read_bytes()
for enc in ("utf-8-sig", "utf-8", "shift_jis", "cp932"):
    try:
        tja = raw.decode(enc); break
    except UnicodeDecodeError:
        tja = None
```

### M2 — Hard-coded developer machine path shipped in code
**File:** `taiko256_explorer_gui6.py:1107` and `:1115`
**Problem:** Both HDD entry points seed the default with a literal `E:\NM00057 T14100-1-NA-HDD0-A [Ver.B02] (HDD).img`. Guarded by `.exists()` so harmless functionally, but it leaks dev-environment state.
**Exact fix:** Persist a recent-files list (e.g. `QSettings`) and use the last opened image as the guess; drop the literal.

### M3 — Extract progress: cancel vs `QProgressDialog` auto-reset race
**File:** `taiko256_explorer_gui6.py:1315-1347`
**Problem:** `QProgressDialog` auto-closes when `setValue(total)` is reached while `prog.canceled.connect(self.worker.cancel)` is also wired. A natural finish and a user cancel can both fire. Benign because `ExtractWorker.cancel()` is idempotent (`Event.set`), but fragile.
**Exact fix:** `prog.setAutoClose(False); prog.setAutoReset(False)` and close it explicitly only in `on_done`.

### M4 — Silent blanket exception swallowing in decode/convert loops
**File:** `taiko256_explorer_gui6.py:365-370` (PNG convert), `:874-880` (SWG textures), `:970-975` (frame player)
**Problem:** `except Exception: pass` hides systematic decoder breakage — a fully broken TIM2/PNG path produces zero output with no diagnostic.
**Exact fix:** Count skipped items and surface a summary (e.g. include `skipped=N` in the status/message), or log to stderr like `_bin_editor_module` does (`:1387`).

### M5 — HDD in-place replace has no rollback on partial write
**File:** `hdd_browser.py:179-186`
**Problem:** reopen-writable → `pfs_write` → reopen-RO with no atomicity. The dialog *warns* "no automatic backup" (honest UX), but a mid-write failure still corrupts the 80 GB image irrecoverably.
**Exact fix:** Offer an opt-in backup/snapshot of the affected file's zones before writing, or at minimum document non-atomicity in the warning and disable the action while another write is pending.

---

## LOW

### L1 — Dead no-op statement
**File:** `taiko256_explorer_gui6.py:562`
**Problem:** `self.tree.selectionModel  # placeholder` accesses a bound method without calling it; does nothing.
**Exact fix:** Delete the line.

### L2 — Temp-dir cleanup leaks on any extra scratch file
**File:** `hdd_song_wizard.py:264-274`
**Problem:** Cleanup only `unlink()`s `list.bin` and `DATA.000`, then `self._tmpdir.rmdir()`. If `song_builder` wrote any other scratch file into `_tmpdir`, `rmdir()` raises `OSError` (caught/ignored) and the temp dir leaks.
**Exact fix:** Replace the unlink+rmdir block with `shutil.rmtree(self._tmpdir, ignore_errors=True)`.

### L3 — Peak-memory doubling during save/rebuild
**File:** `taiko256_explorer_gui6.py:150` (`save`) and `:249` (`rebuild`)
**Problem:** `self.data_path.read_bytes()` loads a full copy into a `bytearray` while `self._mm` still maps the same file, plus `new_data`. For large DATA this is ~2–3× transient RAM.
**Exact fix:** Document the requirement, or close `self._mm` before the read in the overwrite path and stream-write groups instead of building one giant `bytearray`.

### L4 — Confusing tautological writability predicate
**File:** `hdd_browser.py` calls into `ps2hdd.py:836`
**Problem:** `not (overlay or (writable and not overlay))` reduces to `not (overlay or writable)`. Correct result, redundant term. (In ps2hdd.py — out of scope to edit here, noted because the call path was reviewed.)
**Exact fix:** Simplify to `if not (self.dev.overlay or self.dev.writable):`.

---

## Positives (no change needed)
- LRU eviction is correct: byte-accounted, holds `_cache_lock`, never evicts below 1 entry (`taiko256_explorer_gui6.py:113`).
- Extract workers deliberately bypass the shared LRU cache and only read the read-only mmap — no off-thread QWidget creation (`:347`).
- Unsaved-edits guard on `closeEvent` (`:1350-1358`).
- "file no longer fits" raises a readable `ValueError` with an actionable hint to enable full rebuild (`:1244-1246`).
- In-place 80 GB writes are clearly warned in both HDD dialogs with explicit "no automatic backup" language (`hdd_browser.py:167-174`, `hdd_song_wizard.py:185-191`).

---

## To reach 90+
1. Atomic temp-file + `os.replace()` writes and reliable/versioned backups (C1).
2. Order or stage HDD writes so a partial failure never desyncs list.bin/DATA.000 (H1); add rollback to in-place HDD replace (M5).
3. `wait()` for running workers in both HDD `closeEvent`s before closing handles / deleting temp files; serialize `self.hdd` access (C2, H2).
4. Null `self.archive` + `_update_actions()` on reload failure (H3).
5. Shift-JIS-aware TJA reading (M1).

**SCORE: 76**
