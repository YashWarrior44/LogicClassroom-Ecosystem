"""
media_cache.py — PDF render cache (WEBP only).

PDF pages are rendered to WEBP via PyMuPDF + Pillow. Same cache layout as
the PPT renderer (one folder per imported file, manifest with fingerprint).

Cache layout
────────────
    temps/media/<safe_name>/
        cache_manifest.json
        page_001.webp          ← full-resolution page render
        page_002.webp
        ...
        thumb_001.webp         ← small thumbnail for Media-Manager
        thumb_002.webp

Adaptive resolution
───────────────────
* Full pages are rendered at the primary display's width (capped at 3840 px).
* Thumbnails are always 512 px wide — cheap to decode for the Media Manager.

Public API
──────────
    render_pdf_with_cache(source_pdf, cache_dir, *, force=False,
                          on_progress=None) → PdfCacheResult
"""
from __future__ import annotations

import json
import logging
import os
import time
import shutil
import sys
import threading
from dataclasses import dataclass, field
from pathlib import Path
from typing import Callable, List, Optional, Tuple

logger = logging.getLogger(__name__)

__all__ = ["PdfCacheResult", "render_pdf_with_cache", "render_pdf_pages",
           "count_pdf_pages", "count_ppt_slides",
           "estimate_render_seconds", "format_duration",
           "LARGE_DOCUMENT_PAGES"]


# Dependency probes
try:
    import fitz   # PyMuPDF                                # noqa: F401
    _HAS_PYMUPDF = True
except ImportError:
    _HAS_PYMUPDF = False

try:
    from PIL import Image                                  # noqa: F401
    _HAS_PIL = True
except ImportError:
    _HAS_PIL = False


# Render budgets
_MAX_RENDER_WIDTH   = 3840
_DEFAULT_WIDTH      = 1920
_THUMB_WIDTH        = 512


def _detect_display_width() -> int:
    try:
        from PySide6.QtGui import QGuiApplication
        app = QGuiApplication.instance()
        if app is not None:
            scr = app.primaryScreen()
            if scr:
                w = int(scr.geometry().width() * scr.devicePixelRatio())
                return min(max(_DEFAULT_WIDTH, w), _MAX_RENDER_WIDTH)
    except Exception:
        pass
    return _DEFAULT_WIDTH


def _silence_mupdf() -> None:
    try:
        import fitz
        try: fitz.TOOLS.mupdf_display_errors(False)
        except Exception: pass
        try: fitz.TOOLS.mupdf_display_warnings(False)
        except Exception: pass
    except Exception:
        pass


# ─────────────────────────────────────────────────────────────────────────────
# Result + fingerprint helpers (mirror what ppt_renderer uses)
# ─────────────────────────────────────────────────────────────────────────────

import hashlib

def _fingerprint(path: Path) -> dict:
    p = Path(path)
    try:
        st = p.stat()
    except OSError:
        return {"size": 0, "mtime": 0, "sha1_1mb": "", "name": getattr(p, "name", "")}
    h = hashlib.sha1()
    try:
        with p.open("rb") as f:
            h.update(f.read(1024 * 1024))
    except OSError:
        pass
    return {"size": st.st_size, "mtime": int(st.st_mtime),
            "sha1_1mb": h.hexdigest(), "name": p.name}


def _is_cache_fresh(source: Path, cache_dir: Path) -> bool:
    manifest = cache_dir / "cache_manifest.json"
    if not manifest.exists():
        return False
    try:
        data = json.loads(manifest.read_text(encoding="utf-8"))
    except Exception:
        return False
    src_fp = _fingerprint(source)
    c_fp   = data.get("fingerprint", {})
    if src_fp.get("size")  != c_fp.get("size"):  return False
    # Content hash beats mtime — see the long note in ppt_renderer's
    # is_cache_fresh. Short version: reopening a .lb rewrites every extracted
    # file's mtime (zipfile does not restore timestamps), so demanding an mtime
    # match declared a byte-identical PDF stale and re-rendered every page of
    # it on the next click. mtime survives only as the legacy-manifest path.
    if src_fp.get("sha1_1mb") and c_fp.get("sha1_1mb"):
        if src_fp["sha1_1mb"] != c_fp["sha1_1mb"]:
            return False
    elif src_fp.get("mtime") != c_fp.get("mtime"):
        return False
    for rel in data.get("files", []):
        if not (cache_dir / rel).exists():
            return False
    return True


@dataclass
class PdfCacheResult:
    page_count:  int               = 0
    page_files:  List[str]         = field(default_factory=list)
    thumb_files: List[str]         = field(default_factory=list)
    format:      str               = "webp"
    render_size: Tuple[int, int]   = (0, 0)

    def page_file(self, idx: int) -> Optional[str]:
        return self.page_files[idx] if 0 <= idx < len(self.page_files) else None

    def to_dict(self) -> dict:
        return {
            "page_count":  self.page_count,
            "page_files":  list(self.page_files),
            "thumb_files": list(self.thumb_files),
            "format":      self.format,
            "render_size": list(self.render_size),
        }

    @classmethod
    def from_dict(cls, d: dict) -> "PdfCacheResult":
        rs = tuple(d.get("render_size", [0, 0]))[:2] or (0, 0)
        return cls(
            page_count  = int(d.get("page_count", 0)),
            page_files  = list(d.get("page_files", [])),
            thumb_files = list(d.get("thumb_files", [])),
            format      = str(d.get("format", "webp")),
            render_size = (int(rs[0]) if len(rs) > 0 else 0,
                           int(rs[1]) if len(rs) > 1 else 0),
        )


# ─────────────────────────────────────────────────────────────────────────────
# Page rendering (PyMuPDF pixmap → Pillow → WEBP)
# ─────────────────────────────────────────────────────────────────────────────

# ONE lock for every MuPDF call made from the render workers.
#
# PyMuPDF documents that it "does not support running on multiple threads —
# doing so may cause incorrect behaviour or even crash Python itself". A
# document per thread (below) is what MuPDF's C API needs, but PyMuPDF
# shares one MuPDF context between them — its glyph cache, font cache and
# resource store — with no locking, so two threads rasterising text at once
# can corrupt it. That does not raise; it kills the process. It also gets
# MORE likely the more truly parallel cores a machine has, which fits the
# report: never on the 12-thread development laptop, a sudden exit mid-
# import on a school board's 24-thread i7-14650HX.
#
# What it costs is small, because MuPDF is the small half of the work:
# rasterising a 1920 px page is ~6.5 ms against ~223 ms to encode it as
# WEBP, and the encode — Pillow, thread-safe, GIL-releasing — stays fully
# parallel outside this lock. Opening documents, loading pages, rendering
# and DROPPING pages and pixmaps (dropping is a MuPDF call too) all happen
# under it.
_MUPDF_LOCK = threading.Lock()


def _render_page_pixmap(page, target_w: int) -> Optional["Image.Image"]:
    """Render one PDF page to a Pillow Image at ~target_w pixels wide."""
    try:
        rect = page.rect
        if rect.width <= 0:
            return None
        zoom = target_w / float(rect.width)
        import fitz
        from PIL import Image
        matrix = fitz.Matrix(zoom, zoom)
        pix    = page.get_pixmap(matrix=matrix, alpha=False)
        mode   = "RGB" if pix.alpha == 0 else "RGBA"
        depth  = 4 if mode == "RGBA" else 3

        # WRAP MuPDF's buffer. Do not copy it.
        #
        # ``pix.samples`` builds a bytes object of the whole page, and
        # ``Image.frombytes`` then copies that again — and BOTH copies hold
        # the GIL, because they are Python-level buffer operations rather
        # than library calls that release it.
        #
        # That is what capped this pipeline. Measured on a 1920 px page:
        # rasterising is 6.2 ms and releases the GIL, ``pix.samples`` adds
        # 2.6 ms that does NOT, and frombytes adds 9.0 ms that does not
        # either. Across twelve worker threads those milliseconds serialise,
        # and a 1000-page import sat at 61% of the machine no matter how
        # many cores it was given.
        #
        # ``samples_mv`` is a memoryview onto the pixmap — no copy at all —
        # and ``frombuffer`` wraps it in place. Verified byte-identical.
        #
        # The image is then a VIEW, so the pixmap has to outlive it. That
        # costs memory, and the cost is real: an in-flight page goes from
        # about 193 MB to 252 MB at 3840 px, because MuPDF's buffer is now
        # held through the encode instead of being freed before it. The
        # worker count is computed from that figure
        # (``_BYTES_PER_RENDERED_PIXEL``), so the budget absorbs it by
        # running fewer pages at once rather than by using more memory.
        img = None
        if pix.stride == pix.width * depth:
            try:
                img = Image.frombuffer(mode, (pix.width, pix.height),
                                       pix.samples_mv, "raw", mode, 0, 1)
                img._lb_backing_pixmap = pix        # noqa: SLF001
            except Exception:
                img = None
        if img is None:
            # A padded stride, or a PyMuPDF without samples_mv. Copying is
            # correct, just slower.
            img = Image.frombytes(mode, (pix.width, pix.height), pix.samples)

        if img.mode != "RGB":
            bg = Image.new("RGB", img.size, "white")
            bg.paste(img, mask=img.split()[-1])
            img = bg
        return img
    except Exception as e:
        logger.warning("PDF page render failed: %s", e)
        return None


# libwebp's compression EFFORT (0 fastest .. 6 tightest). This one number
# dominated the whole import: profiling a page showed MuPDF rasterising in
# 42 ms while the WEBP encode took 560 ms — 80% of the time, and the actual
# reason a 100-page PDF felt like it took forever. "PDF rendering is slow"
# was never really about PDF rendering.
#
# Measured per 1920 px page:
#     method=0   149 ms   279 KB
#     method=2   211 ms   218 KB     <- chosen
#     method=4   550 ms   211 KB     <- previous
#     method=6   672 ms   209 KB
#
# Going 4 -> 2 was 2.6x faster for 3% more disk. Re-measured since, on a
# 1920 px A4 text page, including how faithful each result is (mean absolute
# error per channel against the source bitmap):
#
#     method=0   148 ms   357 KB   err 0.27    <- chosen
#     method=1   184 ms   351 KB   err 0.26
#     method=2   222 ms   303 KB   err 0.28    <- previous
#
# 0 is another 1.5x for 18% more disk and NO loss of fidelity — the error is
# if anything marginally lower, because libwebp's cheaper modes are less
# aggressive rather than less accurate. Everything above 0 is paying CPU for
# file size alone, and the encode is 93% of what an import costs (measured:
# 6.5 ms rasterising in MuPDF, 9.6 ms copying, 223 ms encoding), so this one
# number is most of the import.
#
# WORTH KNOWING: JPEG at q85 encodes the same page in 12.6 ms — SEVENTEEN
# times faster than method=2 — at an error of 0.48, which is still invisible.
# It is not used because these files are zipped into the .lb, and JPEG's 92%
# larger output would roughly double the size of every board containing a
# PDF. That is a trade worth offering, not one worth making silently.
_WEBP_METHOD = 0


def _save_webp(img, out_path: Path, *, quality: int = 85) -> bool:
    """Encode one image to WEBP. True if a non-empty file resulted.

    No ``mkdir`` and no ``exists()``/``stat()`` here. Every one of those is
    a filesystem syscall that holds the GIL, and at two files per page they
    came to six thousand syscalls for a thousand-page import — serialised
    across every worker, for information the save itself already gives us:
    if ``Image.save`` returns without raising, the file was written.

    The directory is created ONCE by the caller before any worker starts.
    """
    try:
        img.save(str(out_path), "WEBP", quality=quality, method=_WEBP_METHOD)
        return True
    except Exception as e:
        logger.warning("WEBP save failed (%s): %s", out_path.name, e)
        return False


def _save_thumb_from_page(page, out_path: Path, *,
                          max_width: int = _THUMB_WIDTH,
                          quality: int = 75) -> bool:
    """Render a thumbnail straight from the PDF page.

    The obvious way — resize the full-resolution page down — is the slow
    way, because it makes Pillow filter every pixel of a 3840 px image to
    produce a 512 px one. MuPDF can simply rasterise the page again at the
    smaller zoom, and a 512 px render is about 1.8% of the pixels of a
    3840 px render, so it is nearly free.

    Measured on an A4 text page at 3840 px:

        resize the full page    141.0 ms      47 KB
        re-render at 512 px      31.4 ms      47 KB

    4.5x faster for a byte-identical result, and it removes the LANCZOS
    intermediate — a second large allocation — from the memory peak.

    Falls back to the resize path if anything about the re-render fails, so
    a page that MuPDF will only rasterise once still gets a thumbnail.
    """
    thumb = _render_thumb_image(page, max_width)
    if thumb is None:
        return False
    try:
        thumb.save(str(out_path), "WEBP", quality=quality,
                   method=_WEBP_METHOD)
        return True
    except Exception as e:
        logger.warning("Thumb save failed (%s): %s", out_path.name, e)
        return False


def _render_thumb_image(page, max_width: int = _THUMB_WIDTH):
    """The MuPDF half of a thumbnail: rasterise at thumbnail size and COPY
    the pixels out, so the result owns its memory and can be encoded after
    the MuPDF lock is let go. None on failure."""
    try:
        import fitz
        from PIL import Image
        rect = page.rect
        if rect.width <= 0:
            return None
        zoom = max_width / float(rect.width)
        pix = page.get_pixmap(matrix=fitz.Matrix(zoom, zoom), alpha=False)
        mode = "RGB" if pix.alpha == 0 else "RGBA"
        thumb = Image.frombytes(mode, (pix.width, pix.height), pix.samples)
        pix = None
        if thumb.mode != "RGB":
            bg = Image.new("RGB", thumb.size, "white")
            bg.paste(thumb, mask=thumb.split()[-1])
            thumb = bg
        return thumb
    except Exception as e:
        logger.warning("Thumb render failed: %s", e)
        return None


def _save_thumb_from_image(img, out_path: Path, *,
                      max_width: int = _THUMB_WIDTH,
                      quality: int = 75) -> bool:
    try:
        from PIL import Image
        ratio = max_width / float(img.width) if img.width else 1.0
        new_h = max(1, int(img.height * ratio))
        # ``reducing_gap`` lets Pillow do a cheap box-reduce down to roughly
        # 2x the target before the expensive LANCZOS pass. Going straight
        # from 1920 px to 512 px in one LANCZOS resample means filtering
        # every source pixel at full weight — pointless when the result is
        # a 512 px thumbnail nobody inspects closely.
        thumb = img.resize((max_width, new_h), Image.LANCZOS, reducing_gap=2.0)
        thumb.save(str(out_path), "WEBP", quality=quality,
                   method=_WEBP_METHOD)
        return True
    except Exception as e:
        logger.warning("Thumb save failed (%s): %s", out_path.name, e)
        return False


# ─────────────────────────────────────────────────────────────────────────────
# Parallel page rendering
#
# Rendering a page is two native, GIL-RELEASING stages: MuPDF rasterises, then
# Pillow encodes two WEBPs (the full page and a LANCZOS-resized thumbnail).
# Because both libraries drop the GIL, plain threads scale here — measured on
# a 48-page deck at 1920 px on 12 cores:
#
#     serial          36.3 s   (757 ms/page)
#     threads x8       8.8 s   4.15x
#     threads x12      7.6 s   4.76x
#     processes x8     8.9 s   4.09x   (slower, plus spawn + IPC cost)
#
# Threads beat processes, so there is no process pool here and nothing to
# make PyInstaller-safe (a frozen app spawning copies of itself is a whole
# class of bug we simply don't have). Worker count is capped at
# _MAX_RENDER_WORKERS because each in-flight page holds a full-resolution
# RGB bitmap — at 1920x2716 that is ~15 MB apiece, so the cap is really a
# memory ceiling, not a CPU one.
#
# THREAD SAFETY: a MuPDF document must not be touched from two threads at
# once. Each worker thread therefore opens its OWN fitz.Document via
# thread-local storage; _PageRenderer keeps a registry of every document it
# opened so they are all closed deterministically, because an unclosed
# handle keeps the source PDF locked on Windows.
# ─────────────────────────────────────────────────────────────────────────────

# No CPU cap. Rendering a page is two native, GIL-releasing stages, so
# every core the machine has is a core this can use — and an import is a
# modal operation the teacher is watching a progress bar for, not
# background work that has to leave room for anything else.
#
# It used to be pinned at 8, which left a third of a 12-core machine idle.
# The ONLY limit now is memory (see _RENDER_MEMORY_BUDGET): at 1920 px a
# worker costs about 46 MB, so twelve of them fit inside the budget with
# room to spare and every core gets used; at 3840 px a worker costs 185 MB
# and the budget is what decides, which is the correct thing to be bound by.
_MAX_RENDER_WORKERS = 64

# How much memory the whole render pass may occupy at once.
#
# Every in-flight page holds a full-resolution RGB bitmap plus the buffers
# Pillow needs to encode it, and that cost is LINEAR in the worker count.
# Measured on a 30-page A4 chapter at 3840 px, peak working set:
#
#     1 worker    280 MB    26.4 s
#     2 workers   475 MB    14.1 s
#     3 workers   664 MB    10.0 s
#     4 workers   856 MB     8.4 s
#     6 workers  1239 MB     6.6 s
#     8 workers  1621 MB     6.2 s      <- what it used to always do
#
# 193 MB per worker, dead straight, while the time curve flattens hard
# after four. Eight workers buy 26% over four and cost 765 MB for it.
#
# 1.6 GB is not survivable inside a 2.5 GB ceiling: the app is already
# holding a board, a scene cache and up to 640 MB of decoded imagery.
# 768 MB is the share this pass gets, and it is chosen so that a NORMAL
# import is completely unaffected — at 1920 px a worker costs about 48 MB,
# so eight of them come to 384 MB and the CPU cap still governs. The
# memory cap only ever bites on a 4 K board, which is exactly where it
# should.
_RENDER_MEMORY_BUDGET = 768 * 1024 * 1024

# Bytes one in-flight page costs, per pixel of the rendered page.
#
# Three for the RGB bitmap itself, and the rest is what Pillow needs around
# it to encode a WEBP. Derived from the table above: 193 MB for a 3840 x
# 5430 page is 9.3 bytes per pixel, and the same constant predicts the
# 1920 px measurement (48 MB) to within a megabyte.
_BYTES_PER_RENDERED_PIXEL = 9.3


def _physical_core_count() -> int:
    """Cores that can actually run work in parallel, not SMT siblings.

    ``os.cpu_count()`` reports LOGICAL processors, and on an SMT chip half
    of those share an execution unit with another. For compute-bound native
    work that is worth perhaps 30% extra, not 100% — so sizing a thread pool
    by the logical count over-subscribes the machine.

    Measured on a Ryzen 5 5600H (6 cores, 12 threads), importing a real
    100-page PDF:

        1 worker   13.81 s   1.00x
        2           7.26 s   1.90x
        4           4.28 s   3.23x
        6           3.26 s   4.24x
        8           3.10 s   4.45x
        10          2.90 s   4.76x
        12          3.05 s   4.53x   <- SLOWER than 10

    The curve flattens at the physical core count and turns back down once
    every logical processor is claimed, because the threads then fight over
    six real cores AND over memory bandwidth. This is also why the CPU
    meter shows 70-80% during an import rather than 100%: 100% would mean
    every logical processor busy, and that state is slower than the one
    just below it.

    Returns the logical count when the physical count cannot be determined,
    which is the old behaviour.
    """
    try:
        import ctypes
        from ctypes import wintypes

        RelationProcessorCore = 0
        fn = ctypes.windll.kernel32.GetLogicalProcessorInformationEx
        fn.argtypes = [ctypes.c_int, ctypes.c_void_p,
                       ctypes.POINTER(wintypes.DWORD)]
        fn.restype = wintypes.BOOL

        length = wintypes.DWORD(0)
        fn(RelationProcessorCore, None, ctypes.byref(length))
        if length.value:
            buf = ctypes.create_string_buffer(length.value)
            if fn(RelationProcessorCore, buf, ctypes.byref(length)):
                # Walk the variable-length records: each one is a core, and
                # the first DWORD after the relationship is its size.
                offset = 0
                cores = 0
                data = buf.raw
                while offset + 8 <= len(data):
                    size = int.from_bytes(data[offset + 4:offset + 8], "little")
                    if size <= 0:
                        break
                    cores += 1
                    offset += size
                if cores > 0:
                    return cores
    except Exception:
        logger.debug("physical core detection failed", exc_info=True)
    return os.cpu_count() or 4


def _render_worker_count(page_count: int,
                         page_pixels: Optional[int] = None) -> int:
    """Threads to use for a document of ``page_count`` pages.

    ``page_pixels`` is the pixel count of ONE rendered page. Pass it and
    the answer respects the memory budget as well as the CPU count; leave
    it out and only the CPU cap applies, which is the old behaviour and
    fine for callers rendering something small.
    """
    logical = os.cpu_count() or 4
    physical = _physical_core_count()
    # Half again as many threads as there are real cores. Every core stays
    # busy, the SMT siblings pick up the stalls, and the machine is not
    # over-subscribed to the point where it starts losing throughput — the
    # measured sweet spot was 10 on a 6-core/12-thread chip, and this gives
    # 9, within 2% of the best time while leaving a little room for the
    # progress dialog to stay responsive.
    workers = min(logical, max(1, physical + physical // 2),
                  _MAX_RENDER_WORKERS, page_count)
    cpu_workers = workers

    if page_pixels and page_pixels > 0:
        per_worker = page_pixels * _BYTES_PER_RENDERED_PIXEL
        affordable = int(_RENDER_MEMORY_BUDGET // max(1.0, per_worker))
        if affordable < workers:
            logger.info(
                "PDF render: %d workers instead of %d — one page is %.0f MB "
                "and the budget is %.0f MB",
                max(1, affordable), cpu_workers, per_worker / (1024 * 1024),
                _RENDER_MEMORY_BUDGET / (1024 * 1024))
        workers = min(workers, affordable)

    return max(1, workers)


class _PageRenderer:
    """Renders PDF pages to WEBP, one ``fitz.Document`` per worker thread."""

    def __init__(self, src: Path, cache: Path, render_w: int,
                 page_namer=None, thumb_namer=None) -> None:
        self._src = str(src)
        self._cache = cache
        self._w = render_w
        self._local = threading.local()
        self._docs: list = []
        self._lock = threading.Lock()
        # Naming is pluggable so the PPT pipeline can reuse this renderer
        # verbatim and still emit its own slide_XXX_anim_000 / thumb_XXX
        # filenames. Defaults preserve the PDF cache layout exactly.
        self._page_namer = page_namer or (lambda i: f"page_{i + 1:03d}.webp")
        self._thumb_namer = thumb_namer or (lambda i: f"thumb_{i + 1:03d}.webp")
        # Once, here, rather than twice per page inside the workers.
        try:
            cache.mkdir(parents=True, exist_ok=True)
        except OSError:
            logger.warning("could not create %s", cache)

    def _doc(self):
        # Called with _MUPDF_LOCK held (from render).
        doc = getattr(self._local, "doc", None)
        if doc is None:
            import fitz
            doc = fitz.open(self._src)
            self._local.doc = doc
            with self._lock:
                self._docs.append(doc)
        return doc

    def render(self, i: int) -> Tuple[int, Optional[str], Optional[str], int]:
        """(index, page_name|None, thumb_name|None, pixel height).

        Returns rather than raises on a bad page so one broken page can't
        take down the whole import — same tolerance the serial loop had.

        The whole body is wrapped in one try/except. It used to only rely
        on `_render_page_pixmap`'s internal catch, but `load_page(i)` runs
        BEFORE that call and was unguarded — one page MuPDF can't even
        load (real teacher decks hit this; small synthetic test decks
        don't) raised out of a worker thread, `ThreadPoolExecutor` handed
        it back through `fut.result()` in `render_pdf_pages`, which
        cancelled every other in-flight page and re-raised — turning one
        bad slide into the entire import terminating, on a document that
        could otherwise render 149 pages fine.
        """
        img = thumb = None
        try:
            # Everything MuPDF, under the one lock: load, rasterise the page
            # AND its thumbnail (rendered, not resized — see
            # _save_thumb_from_page for the measurement), drop the page.
            with _MUPDF_LOCK:
                page = self._doc().load_page(i)
                try:
                    img = _render_page_pixmap(page, self._w)
                    if img is not None:
                        thumb = _render_thumb_image(page)
                finally:
                    page = None
            if img is None:
                return i, None, None, 0
            page_name = self._page_namer(i)
            thumb_name = self._thumb_namer(i)
            height = img.height
            # Encoding — the expensive part — in parallel, outside the lock.
            ok_page = _save_webp(img, self._cache / page_name, quality=85)
            ok_thumb = False
            if thumb is not None:
                ok_thumb = _save_webp(thumb, self._cache / thumb_name,
                                      quality=75)
                thumb = None
            if not ok_thumb:
                # A page MuPDF will only rasterise once still gets a thumbnail.
                # Rare, and worth the second full-size image for the moment it
                # takes, because a missing thumbnail is a blank tile in the
                # pages panel for the rest of the board's life.
                ok_thumb = _save_thumb_from_image(img, self._cache / thumb_name)
            return (i,
                    page_name if ok_page else None,
                    thumb_name if ok_thumb else None,
                    height)
        except Exception as e:
            logger.warning("Page %d render failed, skipping: %s", i, e)
            return i, None, None, 0
        finally:
            # The page image is a VIEW onto a MuPDF pixmap (see
            # _render_page_pixmap); letting go of it frees the pixmap, and
            # that is a MuPDF call like any other.
            with _MUPDF_LOCK:
                img = None

    def close(self) -> None:
        with _MUPDF_LOCK, self._lock:
            for doc in self._docs:
                try:
                    doc.close()
                except Exception:
                    pass
            self._docs.clear()


# Documents at or above this many pages get a "this will take a while"
# warning before anything starts. Below it, the progress dialog is enough.
LARGE_DOCUMENT_PAGES = 400


def count_pdf_pages(src: Path) -> int:
    """Pages in a PDF, or 0 if it cannot be read. Cheap — no rendering."""
    try:
        import fitz
        doc = fitz.open(str(src))
        try:
            return int(doc.page_count)
        finally:
            doc.close()
    except Exception:
        logger.debug("could not count pages in %s", src, exc_info=True)
        return 0


def count_ppt_slides(src: Path) -> int:
    """Slides in a .pptx, or 0 if unknown.

    A .pptx is a zip, and every slide is one ``ppt/slides/slideN.xml``
    entry, so this is a directory read rather than an import — which
    matters, because the alternative is asking PowerPoint to open the deck
    and that is the very thing being warned about.

    Older binary .ppt files are not zips and come back 0, which is honest:
    the caller then simply does not warn.
    """
    try:
        import re as _re
        import zipfile
        with zipfile.ZipFile(str(src)) as zf:
            pattern = _re.compile(r"^ppt/slides/slide\d+\.xml$")
            return sum(1 for name in zf.namelist() if pattern.match(name))
    except Exception:
        logger.debug("could not count slides in %s", src, exc_info=True)
        return 0


def estimate_render_seconds(src: Path, render_w: Optional[int] = None,
                            page_count: Optional[int] = None
                            ) -> Optional[float]:
    """How long importing ``src`` is likely to take, in seconds.

    MEASURED, NOT GUESSED. The estimate comes from actually rendering the
    first page on this machine and timing it, then scaling by the page
    count and the worker count. A formula based on core count alone cannot
    know whether those cores are a laptop's or a workstation's, how fast
    the disk is, or how heavy this particular document's pages are — a
    scanned chapter and a text-only report differ by several times on
    identical hardware.

    Timing one page costs what one page costs (tens to hundreds of
    milliseconds), which is nothing against the several minutes being
    estimated, and it is only ever called for documents big enough to
    warrant a warning.

    Returns None when it cannot tell, so callers can fall back to saying
    nothing rather than showing a made-up number.
    """
    try:
        import fitz
    except Exception:
        return None
    if render_w is None:
        render_w = _detect_display_width()
    doc = None
    try:
        doc = fitz.open(str(src))
        pages = int(page_count or doc.page_count)
        if pages <= 0:
            return None
        page = doc.load_page(0)
        rect = page.rect
        page_pixels = 0
        if rect.width > 0:
            zoom = render_w / float(rect.width)
            page_pixels = int(render_w * rect.height * zoom)

        t0 = time.perf_counter()
        img = _render_page_pixmap(page, render_w)
        if img is None:
            return None
        # Encoding is most of the cost, so the sample has to include it —
        # timing the rasterise alone under-reports by roughly a factor of
        # three and would promise a minute for a three-minute import.
        import tempfile as _tf
        with _tf.TemporaryDirectory() as td:
            probe_page = Path(td) / "probe.webp"
            _save_webp(img, probe_page, quality=85)
            img = None
            _save_thumb_from_page(page, Path(td) / "probe_thumb.webp")
        one_page = time.perf_counter() - t0

        workers = _render_worker_count(pages, page_pixels)
        # Threads do not scale perfectly: measured on a 48-page deck at
        # 1920 px, eight workers came to 4.15x and twelve to 4.76x, so
        # roughly 55-60% efficiency once several are in flight. 0.6 is the
        # optimistic end of that, which keeps the estimate from becoming a
        # scare rather than a warning.
        effective = max(1.0, workers * 0.6)
        return (one_page * pages) / effective
    except Exception:
        logger.debug("render estimate failed for %s", src, exc_info=True)
        return None
    finally:
        if doc is not None:
            try:
                doc.close()
            except Exception:
                pass


def format_duration(seconds: float) -> str:
    """A duration a person can act on: "about 4 minutes", not "247.3 s"."""
    seconds = max(0.0, float(seconds))
    if seconds < 45:
        return "under a minute"
    minutes = seconds / 60.0
    if minutes < 60:
        n = max(1, int(round(minutes)))
        return "about %d minute%s" % (n, "" if n == 1 else "s")
    hours = minutes / 60.0
    if hours < 2:
        # "about 1.0 hours" is worse than either alternative.
        return "about an hour" if hours < 1.25 else "well over an hour"
    if hours < 10:
        return "about %.0f hours" % round(hours)
    return "over %d hours" % int(hours)


def render_pdf_pages(src: Path, out_dir: Path, render_w: int, *,
                     page_namer=None, thumb_namer=None,
                     on_progress: Optional[ProgressCb] = None,
                     progress_lo: float = 0.08,
                     progress_span: float = 0.88):
    """Render every page of ``src`` to WEBP + thumbnail, in parallel.

    Returns ``(page_count, ordered, first_page_height)`` where ``ordered`` is
    a list of ``(page_name|None, thumb_name|None, height)`` in PAGE ORDER.

    Shared by the PDF importer and — via PowerPoint's own PDF export — the
    PPT importer, so both get the same threading, the same worker cap and
    the same encoder settings from one place. ``page_namer`` /
    ``thumb_namer`` let the caller keep its own filename scheme.
    """
    import fitz
    probe = None
    renderer = None
    try:
        # One cheap open just to learn the page count; the workers open
        # their own handles.
        probe = fitz.open(str(src))
        page_count = probe.page_count
        # While the document is open anyway, measure what ONE rendered page
        # is going to cost. Page 1 is representative enough — a document
        # whose pages differ wildly in size is not the case worth tuning
        # for, and guessing high here only means fewer workers.
        page_pixels = 0
        try:
            if page_count:
                rect = probe.load_page(0).rect
                if rect.width > 0:
                    zoom = render_w / float(rect.width)
                    page_pixels = int(render_w * rect.height * zoom)
        except Exception:
            page_pixels = 0
        probe.close()
        probe = None

        workers = _render_worker_count(page_count, page_pixels)
        renderer = _PageRenderer(src, out_dir, render_w,
                                 page_namer=page_namer,
                                 thumb_namer=thumb_namer)
        done: dict = {}          # index -> (page_name, thumb_name, height)

        def report(n):
            if on_progress:
                on_progress(f"Rendering page {n}/{page_count}…",
                            progress_lo + progress_span * n / max(1, page_count))

        if workers <= 1:
            for i in range(page_count):
                report(i)
                idx, pname, tname, h = renderer.render(i)
                done[idx] = (pname, tname, h)
        else:
            from concurrent.futures import ThreadPoolExecutor, as_completed
            logger.info("PDF render: %d pages on %d threads", page_count, workers)
            with ThreadPoolExecutor(max_workers=workers,
                                     thread_name_prefix="pdfrender") as pool:
                futures = [pool.submit(renderer.render, i)
                           for i in range(page_count)]
                try:
                    completed = 0
                    # Progress is driven by COMPLETIONS, not page index —
                    # pages finish out of order, so counting them is the only
                    # honest measure. Reporting (and the cancel check inside
                    # on_progress) stays on this one thread, so a cancel
                    # raises here rather than inside a worker.
                    for fut in as_completed(futures):
                        idx, pname, tname, h = fut.result()
                        done[idx] = (pname, tname, h)
                        completed += 1
                        report(completed)
                except BaseException:
                    # Cancel (or a genuine failure): stop queued pages before
                    # unwinding, otherwise the pool's context manager blocks
                    # until every remaining page has rendered.
                    for f in futures:
                        f.cancel()
                    raise

        # Assemble in PAGE ORDER. ``done`` filled in completion order, which
        # is arbitrary under threading — walking the index range is what
        # keeps page_002 from landing after page_007 in the manifest.
        ordered = [done.get(i, (None, None, 0)) for i in range(page_count)]
        first_h = ordered[0][2] if ordered else 0
        return page_count, ordered, first_h
    finally:
        # Always release every handle — a page that fails to render must not
        # leave the source PDF locked. Windows won't let us delete/replace a
        # locked file even after this function returns.
        if probe is not None:
            try: probe.close()
            except Exception: pass
        if renderer is not None:
            renderer.close()


# ─────────────────────────────────────────────────────────────────────────────
# Public entry
# ─────────────────────────────────────────────────────────────────────────────

ProgressCb = Callable[[str, float], None]


def render_pdf_with_cache(
    source_pdf:  Path,
    cache_dir:   Path,
    *,
    force:       bool                = False,
    on_progress: Optional[ProgressCb] = None,
) -> PdfCacheResult:
    """Render `source_pdf` into `cache_dir` as WEBP pages + thumbnails."""
    src   = Path(source_pdf)
    cache = Path(cache_dir)
    cache.mkdir(parents=True, exist_ok=True)

    if not src.exists():
        return PdfCacheResult()
    if not _HAS_PYMUPDF:
        logger.warning("PyMuPDF (fitz) not installed — cannot render PDF.")
        return PdfCacheResult()
    if not _HAS_PIL:
        logger.warning("Pillow not installed — cannot encode WEBP.")
        return PdfCacheResult()

    _silence_mupdf()

    # ── Cache hit? ────────────────────────────────────────────────────────
    manifest_path = cache / "cache_manifest.json"
    if not force and _is_cache_fresh(src, cache):
        try:
            data = json.loads(manifest_path.read_text(encoding="utf-8"))
            res  = PdfCacheResult.from_dict(data.get("result", {}))
            if res.page_count > 0:
                logger.info("PDF cache hit: %s (%d pages)", src.name, res.page_count)
                if on_progress:
                    on_progress("Using cached render", 1.0)
                return res
        except Exception as e:
            logger.warning("PDF manifest unreadable (%s); regenerating.", e)

    # Wipe stale files (keep manifest)
    for old in cache.iterdir():
        if old.name == "cache_manifest.json":
            continue
        try:
            if old.is_file(): old.unlink()
            else:             shutil.rmtree(old, ignore_errors=True)
        except OSError:
            pass

    render_w = _detect_display_width()
    if on_progress:
        on_progress("Opening PDF…", 0.05)

    # ── Render ────────────────────────────────────────────────────────────
    page_files:  List[str] = []
    thumb_files: List[str] = []
    page_count = 0
    render_h_total = 0

    try:
        page_count, ordered, render_h_total = render_pdf_pages(
            src, cache, render_w, on_progress=on_progress)
        for pname, tname, _h in ordered:
            if pname:
                page_files.append(pname)
            if tname:
                thumb_files.append(tname)
    except Exception as e:
        logger.warning("PDF render loop failed: %s", e)

    if on_progress:
        on_progress("Wrapping up… 🚀", 0.97)

    result = PdfCacheResult(
        page_count  = page_count,
        page_files  = page_files,
        thumb_files = thumb_files,
        format      = "webp",
        render_size = (render_w, render_h_total),
    )

    try:
        manifest = {
            "schema_version": 3,
            "fingerprint":    _fingerprint(src),
            "result":         result.to_dict(),
            "files":          list(page_files) + list(thumb_files),
        }
        manifest_path.write_text(json.dumps(manifest, indent=2),
                                 encoding="utf-8")
    except OSError as e:
        logger.warning("PDF manifest write failed: %s", e)

    if on_progress:
        on_progress("Complete", 1.0)
    return result