"""User-facing error text shared by the image tools."""
from __future__ import annotations


def is_locked_error(exc: BaseException) -> bool:
    """True when `exc` looks like the image being held by another process
    (PCSX2 keeps the .img open while it runs) or being read-only."""
    if isinstance(exc, PermissionError):
        return True
    if isinstance(exc, OSError) and getattr(exc, "winerror", None) in (5, 32, 33):
        return True
    low = str(exc).lower()
    return ("being used by another process" in low or "permission denied" in low
            or "locked by another process" in low)


def friendly_error(exc: BaseException) -> str:
    """Message for a failed image job: a locked / read-only image gets a plain
    explanation (close PCSX2) ahead of the raw OS error."""
    from taiko2x6.app.i18n import tr
    msg = str(exc)
    if is_locked_error(exc):
        return tr("The image is locked or read-only. Close PCSX2 (and any "
                  "other program using the .img) and try again; the image "
                  "was not changed.") + "\n\n" + msg
    return msg
