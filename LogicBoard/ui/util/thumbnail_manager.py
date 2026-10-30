"""
thumbnail_manager.py — RAM/CPU-optimised page thumbnail system.

Design goals (per spec):
  * Each page's thumbnail is stored as a PNG in <board>/temps/.
  * Thumbnails are NOT regenerated on every change; a timer refreshes the
    CURRENTLY-OPEN page only, every few seconds, and only if it's dirty.
  * Thumbnails are rendered at 1/4 the display pixel count (half width AND half
    height), so they're cheap to make and store.
  * At most 11 QPixmaps are kept in RAM (the current page +/- 5). Others are
    evicted from memory but remain on disk and are reloaded on demand.
  * Rendering reuses the canvas's own object renderers, so the look matches.

This manager is owned by MainWindow and shared with the PagesPanel: the panel
asks the manager for a page's pixmap (RAM -> disk -> render fallback), and the
manager's timer keeps the open page's image reasonably fresh.
"""
from __future__ import annotations

import logging
from collections import OrderedDict
from pathlib import Path
from typing import Optional, TYPE_CHECKING

from PySide6.QtGui import QPixmap, QPainter, QColor
from PySide6.QtCore import Qt, QTimer, QObject

if TYPE_CHECKING:
    from ui.canvas.canvas_widget import CanvasWidget

logger = logging.getLogger(__name__)

# How many thumbnails to hold in RAM (current +/- 5).
RAM_WINDOW = 11
RAM_HALF = RAM_WINDOW // 2  # 5

# How often (ms) the refresh loop runs.
REFRESH_INTERVAL_MS = 4000

TEMPS_DIRNAME    = "temps"
NAV_PAGES_SUBDIR = "nav_pages"
# WEBP for the size, but not at any quality: these previews are mostly INK —
# thin, high-contrast strokes and text, which is the exact content lossy
# compression treats worst. At 80 the ringing around a pen stroke is visible
# once the preview is shown at real resolution, and it showed up as "the
# thumbnails look soft" alongside the device-pixel-ratio bug in
# PageCard.set_thumbnail.
#
# 92 is still far smaller than PNG for a photo-backed page and keeps strokes
# clean. Worth spending, because a page that came from disk and the same page
# still in RAM should not look different from each other.
THUMB_FORMAT  = "WEBP"
THUMB_EXT     = ".webp"
THUMB_QUALITY = 92


class ThumbnailManager(QObject):
    def __init__(self, canvas: "CanvasWidget", parent=None):
        super().__init__(parent)
        self._canvas = canvas
        # RAM cache: page_index -> QPixmap, LRU-ordered (most-recent at end).
        self._ram: "OrderedDict[int, QPixmap]" = OrderedDict()
        # Pages whose on-disk thumbnail is stale and should be re-rendered.
        self._dirty: set[int] = set()
        self._temps_dir: Optional[Path] = None

        # Thumbnail pixel size = 1/4 of the display (half W, half H).
        self._thumb_w, self._thumb_h = self._compute_thumb_size()

        # Periodic refresh timer (refreshes the CURRENT page only).
        self._timer = QTimer(self)
        self._timer.setInterval(REFRESH_INTERVAL_MS)
        self._timer.timeout.connect(self._on_tick)

    # ---- configuration --------------------------------------------------
    def _compute_thumb_size(self):
        screen = None
        try:
            from PySide6.QtWidgets import QApplication
            screen = QApplication.primaryScreen()
        except Exception:
            pass
        if screen is not None:
            geo = screen.geometry()
            w, h = geo.width(), geo.height()
        else:
            w, h = 1920, 1080
        # Half on each axis => 1/4 of the pixels.
        return max(160, w // 2), max(90, h // 2)

    def set_board_dir(self, board_dir: Optional[Path]):
        """Point the manager at a board folder (creates ``temps/nav_pages/``)."""
        self.clear_ram()
        if board_dir is None:
            self._temps_dir = None
            return
        # New layout: <board>/temps/nav_pages/ with WEBP files.
        self._temps_dir = Path(board_dir) / TEMPS_DIRNAME / NAV_PAGES_SUBDIR
        try:
            self._temps_dir.mkdir(parents=True, exist_ok=True)
        except OSError as e:
            logger.debug("Could not create nav_pages dir: %s", e)
            self._temps_dir = None
            return

        # Best-effort cleanup of the legacy <board>/temps/page_*.png files
        # so they don't take up space forever. We don't migrate the bytes —
        # they regenerate cheaply on the next tick.
        legacy_root = Path(board_dir) / TEMPS_DIRNAME
        if legacy_root.exists():
            try:
                for fp in legacy_root.glob("page_*.png"):
                    try:
                        fp.unlink()
                    except OSError:
                        pass
            except OSError:
                pass

    # ---- lifecycle ------------------------------------------------------
    def start(self):
        if not self._timer.isActive():
            self._timer.start()

    # ---- immediate refresh (debounced) ---------------------------------
    # Called from content_changed signal on every committed scene change.
    # Coalesces rapid strokes into a single render so we don't repaint per
    # mouse move — but renders within ~150 ms of the user pausing, giving a
    # near-real-time current-page preview without harming performance.
    _CURRENT_REFRESH_DEBOUNCE_MS = 150

    def refresh_current_soon(self):
        """Schedule a fast refresh of the CURRENT page's thumbnail only.
        Resets the debounce window on every call so a burst of edits becomes
        one render. Independent from the 5-second periodic tick which still
        handles any other pages that need refreshing."""
        if not hasattr(self, "_debounce_timer"):
            self._debounce_timer = QTimer(self)
            self._debounce_timer.setSingleShot(True)
            self._debounce_timer.timeout.connect(self._do_refresh_current_now)
        self._debounce_timer.start(self._CURRENT_REFRESH_DEBOUNCE_MS)

    def _do_refresh_current_now(self):
        """Render the current page thumbnail right now (no debounce). Only
        affects the open page — other pages remain on the periodic tick."""
        cur = self._canvas.current_page
        self._dirty.discard(cur)         # we're rendering it now anyway
        self._render_and_store(cur)

    def stop(self):
        self._timer.stop()

    def clear_ram(self):
        self._ram.clear()

    def clear_disk(self):
        """Delete all cached thumbnail files (used when page indices shift, e.g.
        after a page delete/reorder, so stale images don't map to wrong pages)."""
        if self._temps_dir is None or not self._temps_dir.exists():
            return
        try:
            for pattern in ("page_*.webp", "page_*.png"):
                for fp in self._temps_dir.glob(pattern):
                    try:
                        fp.unlink()
                    except OSError:
                        pass
        except OSError:
            pass
        self._dirty.clear()

    def mark_dirty(self, page_index: int):
        """Flag a page's thumbnail as out of date.

        For the CURRENT page the periodic tick picks this up. For every other
        page the flag is honoured by ``get_thumbnail`` — see ``is_dirty``.
        """
        self._dirty.add(int(page_index))

    def mark_current_dirty(self):
        self.mark_dirty(self._canvas.current_page)

    def is_dirty(self, page_index: int) -> bool:
        """Whether this page's cached thumbnail no longer matches the page.

        The PagesPanel asks before reusing a card it has already filled — a
        card that is already showing something is otherwise never refreshed
        while the panel is open.
        """
        return int(page_index) in self._dirty

    def invalidate(self, pages=None) -> None:
        """Throw away cached thumbnails for ``pages`` (default: all of them).

        Marking dirty alone is not enough when the change must SURVIVE a
        restart: ``_dirty`` lives in RAM, so a stale WEBP left on disk comes
        straight back the next time the board is opened. Deleting the file is
        what makes "this preview is wrong" permanent. Nothing is re-rendered
        here — the next fetch does that, and only for pages actually on
        screen, so invalidating a 100-page board costs 100 unlinks and no
        rasterising at all.
        """
        if pages is None:
            try:
                pages = range(max(0, self._canvas.page_count))
            except Exception:
                pages = ()
        for i in pages:
            i = int(i)
            self._dirty.add(i)
            self._ram.pop(i, None)
            path = self._disk_path(i)
            if path is not None:
                try:
                    path.unlink()
                except OSError:
                    pass          # missing is the desired state anyway

    # ---- timer tick: refresh ONLY the current page ----------------------
    def _on_tick(self):
        cur = self._canvas.current_page
        # Only ever re-render the page the user is actually on, and only if it
        # has been marked dirty since the last render (avoids constant work).
        if cur in self._dirty:
            self._render_and_store(cur)
            self._dirty.discard(cur)

    # ---- thumbnail access (panel calls this) ----------------------------
    def get_thumbnail(self, page_index: int) -> Optional[QPixmap]:
        """Return a thumbnail for a page: RAM -> disk -> render-on-demand.
        Maintains the 11-item RAM window centred on the current page."""
        # 0. Known stale? Re-render before serving anything.
        #
        # This is what makes ``mark_dirty`` mean something for pages other
        # than the open one. It used to be a dead flag for them: ``_on_tick``
        # only ever looks at the current page, and this method went straight
        # to the RAM/disk copy without consulting ``_dirty`` — so a page whose
        # content changed while you were somewhere else (a whole-deck
        # background, a media purge clearing backgrounds board-wide) kept
        # serving the picture of how it used to look, indefinitely.
        #
        # Doing it HERE rather than on a sweep is what keeps it cheap: the
        # panel only fetches thumbnails for cards in the viewport, so only
        # what the user can actually see is ever rasterised.
        if int(page_index) in self._dirty:
            fresh = self._render_and_store(page_index)
            if fresh is not None:
                self._dirty.discard(int(page_index))
                return fresh
            # Render failed (page gone mid-flight?) — fall through and serve
            # whatever we still have rather than showing an empty card.
        # 1. RAM hit.
        pm = self._ram.get(page_index)
        if pm is not None:
            self._ram.move_to_end(page_index)
            self._enforce_window()
            return pm
        # 2. Disk hit (try WEBP first, then legacy PNG).
        path = self._disk_path(page_index)
        if path is not None and path.exists():
            pm = QPixmap(str(path))
            if not pm.isNull():
                self._store_ram(page_index, pm)
                return pm
        legacy = self._legacy_png_path(page_index)
        if legacy is not None and legacy.exists():
            pm = QPixmap(str(legacy))
            if not pm.isNull():
                # Re-save as WEBP and drop the legacy file so next time the
                # WEBP path is used.
                if path is not None:
                    try:
                        pm.save(str(path), THUMB_FORMAT, THUMB_QUALITY)
                        try: legacy.unlink()
                        except OSError: pass
                    except Exception as e:
                        logger.debug("Migrate legacy thumb p%d: %s", page_index, e)
                self._store_ram(page_index, pm)
                return pm
        # 3. Render fresh.
        return self._render_and_store(page_index)

    # ---- internal -------------------------------------------------------
    def _disk_path(self, page_index: int) -> Optional[Path]:
        if self._temps_dir is None:
            return None
        return self._temps_dir / f"page_{page_index:04d}{THUMB_EXT}"

    def _legacy_png_path(self, page_index: int) -> Optional[Path]:
        """Path the old (v1) PNG thumbnail would have been at — used during a
        one-shot migration when a board last saved by an older app version
        is opened in this one."""
        if self._temps_dir is None:
            return None
        # Old layout: <board>/temps/page_NNNN.png (no nav_pages/ subdir)
        return self._temps_dir.parent / f"page_{page_index:04d}.png"

    def _store_ram(self, page_index: int, pm: QPixmap):
        self._ram[page_index] = pm
        self._ram.move_to_end(page_index)
        self._enforce_window()

    def _enforce_window(self):
        """Keep only the current +/- 5 pages (max 11) in RAM. Pages outside the
        window are dropped from memory (still on disk)."""
        cur = self._canvas.current_page
        lo, hi = cur - RAM_HALF, cur + RAM_HALF
        # Drop anything clearly outside the window first.
        for idx in list(self._ram.keys()):
            if idx < lo or idx > hi:
                del self._ram[idx]
        # If still over budget (e.g. rapid jumps), evict LRU.
        while len(self._ram) > RAM_WINDOW:
            self._ram.popitem(last=False)

    def _render_and_store(self, page_index: int) -> Optional[QPixmap]:
        scene = self._canvas.scene_at(page_index)
        if scene is None:
            return None
        pm = self._render_scene(scene)
        if pm is None:
            return None
        # Save to disk (best-effort, WEBP).
        path = self._disk_path(page_index)
        if path is not None:
            try:
                pm.save(str(path), THUMB_FORMAT, THUMB_QUALITY)
            except Exception as e:
                logger.debug("Thumb save failed p%d: %s", page_index, e)
        self._store_ram(page_index, pm)
        return pm

    def _render_scene(self, scene) -> Optional[QPixmap]:
        tw, th = self._thumb_w, self._thumb_h
        cw = scene.canvas_width if scene.canvas_width > 0 else 1920
        ch = scene.canvas_height if scene.canvas_height > 0 else 1080
        # Fit canvas into the thumbnail, preserving aspect.
        s = min(tw / cw, th / ch)
        out_w, out_h = max(1, int(cw * s)), max(1, int(ch * s))

        pm = QPixmap(out_w, out_h)
        bg = scene.background_color
        pm.fill(QColor(bg.r, bg.g, bg.b, bg.a))
        p = QPainter(pm)
        try:
            p.setRenderHint(QPainter.Antialiasing, True)
            p.setRenderHint(QPainter.SmoothPixmapTransform, True)
            p.scale(s, s)
            canvas = self._canvas
            # Background image first (if any).
            try:
                canvas._render_bg_image(p, scene)
            except Exception:
                pass
            for j in range(scene.object_count):
                obj = scene.object_at(j)
                if obj and obj.visible:
                    canvas._render_object(p, obj)
        except Exception as e:
            logger.debug("Thumb render failed: %s", e)
        finally:
            p.end()
        return pm