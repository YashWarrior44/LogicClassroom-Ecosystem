"""
pixmap_cache.py — a bounded, least-recently-used cache of decoded pixmaps.

WHY THIS EXISTS
===============
The canvas used a plain dict, keyed by source path, that nothing ever
removed anything from. Every image, every rendered PDF page and every
PowerPoint slide the board had drawn stayed decoded in memory for the life
of the session — the only thing that ever cleared it was opening a different
board.

Measured, on A4 pages rendered at 150 dpi: 8.3 MB per page, and the
process's working set tracked it almost exactly — 75 MB with the board open,
275 MB after twenty-four pages. PDFs are imported at up to 3840 px wide
(``media_cache._MAX_RENDER_WIDTH``), where an A4 page decodes to about
83 MB, so a scanned chapter of thirty pages is 2.5 GB in this one dict.

A teacher does not need page 3 decoded while they are teaching page 27, and
re-decoding a page costs a few milliseconds against hundreds of megabytes of
resident memory.

WHAT IT GUARANTEES
==================
* Total decoded pixels stay under a byte budget.
* The most recently drawn pages are the ones kept, which is exactly the
  working set of somebody moving through a document.
* A single image larger than the whole budget is still returned — a page
  that cannot be cached must still be drawable — it simply does not stay.

WHAT IT DOES NOT DO
===================
Free memory Qt is still using. Evicting drops THIS cache's reference; if a
paint is mid-flight and holds its own, the pixmap lives until that finishes.
That is correct and is why eviction is safe to do at any moment.
"""
from __future__ import annotations

import logging
from collections import OrderedDict
from typing import Optional

logger = logging.getLogger(__name__)

__all__ = ["PixmapCache", "default_budget_bytes"]

_MB = 1024 * 1024


def _physical_ram_bytes() -> Optional[int]:
    """Total physical RAM, or None if it cannot be determined."""
    try:
        import ctypes
        from ctypes import wintypes

        class _MEMORYSTATUSEX(ctypes.Structure):
            _fields_ = [("dwLength", wintypes.DWORD),
                        ("dwMemoryLoad", wintypes.DWORD),
                        ("ullTotalPhys", ctypes.c_ulonglong),
                        ("ullAvailPhys", ctypes.c_ulonglong),
                        ("ullTotalPageFile", ctypes.c_ulonglong),
                        ("ullAvailPageFile", ctypes.c_ulonglong),
                        ("ullTotalVirtual", ctypes.c_ulonglong),
                        ("ullAvailVirtual", ctypes.c_ulonglong),
                        ("ullAvailExtendedVirtual", ctypes.c_ulonglong)]

        st = _MEMORYSTATUSEX()
        st.dwLength = ctypes.sizeof(_MEMORYSTATUSEX)
        if ctypes.windll.kernel32.GlobalMemoryStatusEx(ctypes.byref(st)):
            return int(st.ullTotalPhys)
    except Exception:
        pass
    try:
        import os
        return os.sysconf("SC_PAGE_SIZE") * os.sysconf("SC_PHYS_PAGES")
    except Exception:
        return None


def default_budget_bytes() -> int:
    """How much decoded imagery this machine should hold.

    A fixed number is wrong in both directions: 512 MB is most of a 2 GB
    classroom laptop and a rounding error on a 32 GB desk machine. So it is
    a share of what is actually installed, with a floor that keeps a couple
    of pages workable and a ceiling that keeps the process well inside the
    2.5 GB it is meant to stay under.

    The ceiling is the number that matters. LogicBoard's own baseline is
    about 75 MB with a board open, the scene cache is a handful more, and
    the engine holds the strokes; 640 MB of decoded imagery on top of that
    leaves well over a gigabyte of headroom for PDF rendering spikes and
    for Qt itself.
    """
    total = _physical_ram_bytes()
    if not total:
        return 384 * _MB
    share = int(total * 0.18)
    return max(192 * _MB, min(640 * _MB, share))


class PixmapCache:
    """Least-recently-used cache of QPixmaps, bounded by total bytes.

    Keys are whatever the caller uses to identify a source — the canvas
    uses the media-relative path.
    """

    def __init__(self, budget_bytes: Optional[int] = None):
        self._items: "OrderedDict[str, object]" = OrderedDict()
        self._bytes: dict = {}
        self._total = 0
        self._budget = int(budget_bytes if budget_bytes is not None
                           else default_budget_bytes())
        self.hits = 0
        self.misses = 0
        self.evictions = 0

    # ── measurement ─────────────────────────────────────────────────────
    @staticmethod
    def size_of(pixmap) -> int:
        """Decoded bytes of a pixmap.

        ``depth()`` rather than a hardcoded 4: an 8-bit mask and a 32-bit
        ARGB image differ by 4x, and guessing high on the small ones would
        evict things that cost nothing to keep.
        """
        try:
            if pixmap is None or pixmap.isNull():
                return 0
            depth = max(1, pixmap.depth()) // 8 or 1
            return pixmap.width() * pixmap.height() * depth
        except Exception:
            return 0

    # ── the dict interface the canvas already used ──────────────────────
    def __contains__(self, key) -> bool:
        return key in self._items

    def __len__(self) -> int:
        return len(self._items)

    def __getitem__(self, key):
        value = self._items[key]
        self._items.move_to_end(key)       # most recently used
        self.hits += 1
        return value

    def get(self, key, default=None):
        if key in self._items:
            return self[key]
        self.misses += 1
        return default

    def __setitem__(self, key, pixmap) -> None:
        if key in self._items:
            self._total -= self._bytes.pop(key, 0)
            del self._items[key]
        size = self.size_of(pixmap)
        self._items[key] = pixmap
        self._bytes[key] = size
        self._total += size
        self._evict_to_budget(protect=key)

    def values(self):
        return self._items.values()

    def keys(self):
        return self._items.keys()

    def items(self):
        return self._items.items()

    def clear(self) -> None:
        self._items.clear()
        self._bytes.clear()
        self._total = 0

    def pop(self, key, default=None):
        if key not in self._items:
            return default
        self._total -= self._bytes.pop(key, 0)
        return self._items.pop(key)

    # ── the bound ───────────────────────────────────────────────────────
    @property
    def total_bytes(self) -> int:
        return self._total

    @property
    def budget_bytes(self) -> int:
        return self._budget

    def set_budget(self, budget_bytes: int) -> None:
        self._budget = max(1, int(budget_bytes))
        self._evict_to_budget()

    def _evict_to_budget(self, protect=None) -> None:
        """Drop least-recently-used entries until back inside the budget.

        ``protect`` is never evicted. It is the entry that was just
        inserted, and it is about to be drawn — evicting it would mean
        decoding it again on the very next line, forever, for any image
        larger than the budget. Such an image is kept for this one paint
        and falls out as soon as anything else is cached.
        """
        while self._total > self._budget and len(self._items) > 1:
            key, _value = next(iter(self._items.items()))
            if key == protect:
                if len(self._items) < 2:
                    break
                # Look past the protected entry rather than stopping at it.
                key = next(k for k in self._items if k != protect)
            self._total -= self._bytes.pop(key, 0)
            del self._items[key]
            self.evictions += 1
        if self._total > self._budget:
            logger.debug("pixmap cache holds %.0f MB against a %.0f MB budget "
                         "— a single image is larger than the whole budget",
                         self._total / _MB, self._budget / _MB)

    def stats(self) -> dict:
        return {
            "entries": len(self._items),
            "megabytes": round(self._total / _MB, 1),
            "budget_mb": round(self._budget / _MB, 1),
            "hits": self.hits,
            "misses": self.misses,
            "evictions": self.evictions,
        }
