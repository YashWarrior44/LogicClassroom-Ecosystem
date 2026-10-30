"""
media_manager.py — Media Manager dialog (Library | Preview | Actions).

A Windows 11–style 3-pane media management window:

    ┌──────────────┬───────────────────┬──────────────┐
    │ Library      │ Preview           │ Actions      │
    │ (cards)      │ (image / grid)    │ (set bg, …)  │
    └──────────────┴───────────────────┴──────────────┘

The window is fully resizable (QSplitter). Cards highlight selection,
the preview pane shows the original image or a clickable grid of
PDF/PPT page thumbnails, and the right-hand actions panel scopes
``Set As Background`` / ``Add To Board`` to the picked page when one
is selected (else "entire pdf/ppt").

Data comes from :class:`ui.util.media_library.MediaLibrary`.
"""
from __future__ import annotations

import logging
from pathlib import Path
from typing import Optional, Callable

from PySide6.QtWidgets import (
    QDialog, QVBoxLayout, QHBoxLayout, QLabel, QPushButton, QFrame, QSplitter,
    QScrollArea, QWidget, QGridLayout, QSizePolicy, QMessageBox, QSpacerItem,
    QApplication,
)
from ui.panels.aero_alert import AeroAlert
from PySide6.QtCore  import Qt, Signal, QSize, QEvent, QPointF
from PySide6.QtGui   import (
    QPixmap, QIcon, QPainter, QColor, QFontMetrics, QPen, QPolygonF,
)
try:
    from PySide6.QtSvg import QSvgRenderer
    _HAS_SVG = True
except Exception:
    _HAS_SVG = False

from ui.util.scaling   import UiScale
from ui.util.icon_loader import ICONS_DIR
from ui.util.aero   import prepare_aero_widget, apply_aero, enable_shadow
from ui.util.animations import fade_in
from ui.util.touch_scroll import setup_scroll
from ui.util.icon_loader import tool_icon
from ui.util.audio     import click_sound
from ui.util.media_library import MediaLibrary, MediaItem, human_size
from ui.theme.theme_manager import theme_mgr

__all__ = ["MediaManagerDialog"]

logger = logging.getLogger(__name__)


# ─────────────────────────────────────────────────────────────────────────────
# SVG icon loader (assets/icons/<type>.svg) with graceful text fallback.
# ─────────────────────────────────────────────────────────────────────────────
def _project_root() -> Path:
    # ui/panels/media_manager.py → repo root
    return Path(__file__).resolve().parents[2]


def _load_svg_renderer(svg_path: Path):
    """Load an SVG into a QSvgRenderer, stripping elements QtSvg can't render.

    QtSvg has NO support for SVG <filter> effects (feDropShadow, blur, etc.).
    When a path references filter="url(#...)", QtSvg fails to render it and
    leaves a black box. We strip the filter references (and the now-unused
    <filter> defs) in-memory before rendering, so the gradients/paths still
    draw correctly. The asset file on disk is left untouched.
    """
    if not _HAS_SVG:
        return None
    try:
        import re
        from PySide6.QtCore import QByteArray
        text = svg_path.read_text(encoding="utf-8")
        # Drop  filter="url(#...)"  attributes and the <filter>…</filter> defs.
        text = re.sub(r'\s*filter\s*=\s*"url\(#[^"]*\)"', '', text)
        text = re.sub(r'<filter\b.*?</filter>', '', text, flags=re.DOTALL)
        renderer = QSvgRenderer(QByteArray(text.encode("utf-8")))
        return renderer if renderer.isValid() else None
    except Exception as e:
        logger.debug("SVG sanitize/load failed for %s: %s", svg_path, e)
        return None


def load_scaled_pixmap(path, box_w: int, box_h: int) -> Optional[QPixmap]:
    """Decode an image DIRECTLY at (roughly) the size we need.

    ``QPixmap(path)`` decodes the file at full resolution and only then
    scales, so previewing a 6000x4000 photo materialised ~96 MB of pixels
    to show a 300 px card — the reason the Media Manager crawled on big
    files. ``QImageReader`` reports the size from the header without
    decoding, and ``setScaledSize`` makes the decoder produce the reduced
    image directly (for JPEG that's a DCT-scaled decode, several times
    faster and a fraction of the memory).

    Never upscales — a small source stays its own size and is centred by
    the caller. Returns None if the file can't be read.
    """
    try:
        from PySide6.QtGui import QImageReader
        reader = QImageReader(str(path))
        reader.setAutoTransform(True)          # honour EXIF orientation
        src = reader.size()                    # header only, no pixels yet
        if src.isValid() and src.width() > 0 and src.height() > 0:
            scale = min(box_w / src.width(), box_h / src.height(), 1.0)
            reader.setScaledSize(QSize(max(1, int(src.width() * scale)),
                                        max(1, int(src.height() * scale))))
        img = reader.read()
        if img.isNull():
            return None
        return QPixmap.fromImage(img)
    except Exception as e:
        logger.debug("scaled load failed (%s): %s", path, e)
        return None


def media_type_icon(kind: str, size: int = 64) -> QIcon:
    """Return a QIcon for the media type. Uses ``assets/icons/<kind>.svg``
    when present; otherwise paints a colored rounded-rect fallback."""
    from PySide6.QtCore import QRectF as _QRectF
    from ui.util.icon_loader import blank_icon_pixmap, icon_target_dpr
    svg_path = ICONS_DIR / f"{kind}.svg"
    # ``size`` is LOGICAL, so back the pixmap at the device ratio — otherwise
    # Qt upscales the bitmap at paint time and the icon goes soft on a Hi-DPI
    # board, the same defect the toolbar icons had.
    pm = blank_icon_pixmap(size, icon_target_dpr())

    if svg_path.exists():
        renderer = _load_svg_renderer(svg_path)
        if renderer is not None:
            try:
                painter = QPainter(pm)
                painter.setRenderHint(QPainter.Antialiasing, True)
                # Explicit LOGICAL rect: the painter already scales by the
                # ratio, so the default device-sized viewport double-scales.
                renderer.render(painter, _QRectF(0, 0, size, size))
                painter.end()
                return QIcon(pm)
            except Exception as e:
                logger.debug("SVG render failed for %s: %s", svg_path, e)

    # Fallback — clean modern gradient badge (used when the SVG is missing or
    # QtSvg can't render its gradients). Single rounded rect with a vertical
    # gradient + centered label; no overlapping second rectangle.
    from PySide6.QtGui import QLinearGradient, QFont
    from PySide6.QtCore import QRectF
    palette = {
        "image": (QColor(66, 165, 245),  QColor(21, 101, 192),  "IMG"),
        "pdf":   (QColor(239, 83,  80),  QColor(198, 40,  40),  "PDF"),
        "ppt":   (QColor(255, 154, 85),  QColor(216, 67,  21),  "PPT"),
    }
    top, bottom, label = palette.get(kind, (QColor(120, 120, 140),
                                            QColor(80,  80,  100), "?"))
    painter = QPainter(pm)
    painter.setRenderHint(QPainter.Antialiasing, True)
    grad = QLinearGradient(0, 0, 0, size)
    grad.setColorAt(0.0, top)
    grad.setColorAt(1.0, bottom)
    painter.setBrush(grad)
    painter.setPen(Qt.NoPen)
    r = size * 0.18
    painter.drawRoundedRect(QRectF(size * 0.08, size * 0.08,
                                   size * 0.84, size * 0.84), r, r)
    painter.setPen(QColor(255, 255, 255))
    f = QFont(); f.setBold(True); f.setPixelSize(int(size * 0.24))
    painter.setFont(f)
    painter.drawText(pm.rect(), Qt.AlignCenter, label)
    painter.end()
    return QIcon(pm)


# ─────────────────────────────────────────────────────────────────────────────
# ElidedLabel — a QLabel that truncates its text with "…" to fit its width,
# and never demands horizontal space (so it can't widen its parent card).
# ─────────────────────────────────────────────────────────────────────────────
class ElidedLabel(QLabel):
    def __init__(self, text: str = "", parent=None):
        super().__init__(parent)
        self._full_text = text
        # Ignored horizontal policy => the label reports no preferred width, so
        # a long file name can't push the card wider. It elides to fit instead.
        self.setSizePolicy(QSizePolicy.Ignored, QSizePolicy.Preferred)
        self.setText(text)

    def setText(self, text: str) -> None:
        self._full_text = text or ""
        self.setToolTip(self._full_text)  # full name on hover
        self._update_elide()

    def text(self) -> str:
        return self._full_text

    def resizeEvent(self, event):
        super().resizeEvent(event)
        self._update_elide()

    def _update_elide(self):
        from PySide6.QtGui import QFontMetrics
        fm = QFontMetrics(self.font())
        w = max(0, self.width())
        elided = fm.elidedText(self._full_text, Qt.ElideRight, w) if w else self._full_text
        super().setText(elided)


# ─────────────────────────────────────────────────────────────────────────────
# MediaCard — one row in the library list
# ─────────────────────────────────────────────────────────────────────────────
class MediaCard(QPushButton):
    """One row in the library list. The WHOLE row is the click target.

    This is a QPushButton rather than a QFrame with a hand-rolled
    mousePressEvent, and that is deliberate. A frame only ever sees a click
    when the press lands on the frame itself or bubbles up from a child that
    declined it — which made the hit area depend on exactly which child widget
    happened to be under the finger, and on a touch board most of the card
    simply didn't respond. A button is the shape Qt (and QScroller, which runs
    the kinetic scrolling on the list around us) already knows how to deliver
    taps to — the preview tiles in the middle pane have always been buttons and
    have never had the problem. Children are additionally made transparent to
    mouse events below, so hit-testing can only ever resolve to the card.
    """

    card_clicked = Signal(str)   # emits item.id  (QPushButton owns `clicked`)

    def __init__(self, item: MediaItem, selected: bool = False, parent=None,
                 library: "MediaLibrary | None" = None):
        super().__init__(parent)
        self._item = item
        self._selected = selected
        self._library = library

        sc = UiScale.px

        self.setObjectName("MediaCard")
        self.setText("")
        # Inside a QDialog an ordinary button volunteers as the default button
        # and would then swallow Enter. These cards are list rows, not actions.
        self.setAutoDefault(False)
        self.setDefault(False)
        self.setFocusPolicy(Qt.NoFocus)
        self.clicked.connect(self._on_clicked)
        self.setCursor(Qt.PointingHandCursor)
        # Comfortable, modern card height — roomy without being oversized.
        self.setMinimumHeight(sc(86))
        self.setMaximumHeight(sc(88))
        self.setSizePolicy(QSizePolicy.Expanding, QSizePolicy.Fixed)

        layout = QHBoxLayout(self)
        layout.setContentsMargins(sc(14), sc(10), sc(18), sc(10))
        layout.setSpacing(sc(16))

        # ── LEFT: large file-type icon (or image thumbnail) ──────────────
        # The icon is the visual anchor of the card, so it's sized generously
        # and fills its column. The SVGs are padded internally (content sits
        # at viewBox 12..52 / 8..58) so they never clip against the box edge.
        icon_sz = sc(66)
        icon_label = QLabel()
        icon_label.setFixedSize(icon_sz, icon_sz)
        icon_label.setAlignment(Qt.AlignCenter)
        # _build_card_pixmap already returns a correctly sized, DPI-aware
        # pixmap (logical icon_sz, physical icon_sz*dpr). Do NOT .scaled() it —
        # that operates on raw pixels, ignores devicePixelRatio, and re-clips.
        pm = self._build_card_pixmap(icon_sz)
        icon_label.setPixmap(pm)
        icon_label.setStyleSheet("background: transparent;")
        layout.addWidget(icon_label, 0, Qt.AlignVCenter)

        # ── CENTER: file name (prominent) + metadata (secondary) ─────────
        t = theme_mgr.tokens
        text_box = QVBoxLayout()
        text_box.setSpacing(sc(4))
        text_box.setContentsMargins(0, 0, 0, 0)

        name_label = ElidedLabel(item.name)
        name_label.setStyleSheet(
            f"color: {t.text_primary}; font-size: {sc(16)}px; "
            f"font-weight: 700; background: transparent;"
        )
        if item.type != "image" and item.preview_count == 0:
            why = ("needs PowerPoint"
                   if getattr(item, "render_error", "") == "no_powerpoint"
                   else "not rendered")
            sub_text = f"{item.type.upper()}  ·  {human_size(item.size_bytes)}  ·  {why}"
        elif item.type == "ppt":
            sub_text = (f"{item.type.upper()}  ·  {human_size(item.size_bytes)}  ·  "
                        f"{item.preview_count} slide"
                        f"{'s' if item.preview_count != 1 else ''}")
        elif item.type == "pdf":
            sub_text = (f"{item.type.upper()}  ·  {human_size(item.size_bytes)}  ·  "
                        f"{item.preview_count} page"
                        f"{'s' if item.preview_count != 1 else ''}")
        else:
            sub_text = f"{item.type.upper()}  ·  {human_size(item.size_bytes)}"
        sub_label = ElidedLabel(sub_text)
        sub_label.setStyleSheet(
            f"color: {t.text_secondary}; font-size: {sc(12)}px; "
            f"font-weight: 500; background: transparent;"
        )
        # Vertically center name + metadata as a tight pair.
        text_box.addStretch()
        text_box.addWidget(name_label)
        text_box.addWidget(sub_label)
        text_box.addStretch()
        layout.addLayout(text_box, 1)

        # Nothing inside the card may absorb a press: with this set, Qt's
        # hit-testing skips the children entirely and resolves straight to the
        # card, so every pixel of the row behaves identically. (This is why the
        # tooltip moves to the card — a mouse-transparent label never sees the
        # hover that would raise its own.)
        self.setToolTip(item.name)
        for child in (icon_label, name_label, sub_label):
            child.setAttribute(Qt.WA_TransparentForMouseEvents, True)

        self._refresh_style()

    def _on_clicked(self) -> None:
        click_sound.play_click()
        self.card_clicked.emit(self._item.id)

    def _build_card_pixmap(self, side: int) -> QPixmap:
        """Build the LEFT-side pixmap for the card.

        ALL media types (image, PPT, PDF) show their clean file-type SVG icon —
        we never load the real image/render here. Loading full-resolution
        images for every card was the main RAM hog; the actual pixels are only
        loaded lazily in the Preview pane when the user clicks a card.
        """
        return self._render_type_icon(self._item.type, side)

    @staticmethod
    def _render_thumb(path: Path, w: int, h: int) -> Optional[QPixmap]:
        """Load a thumbnail from disk into a (w × h) pixmap, centered, with the
        source's aspect preserved. Handles SVG and raster.

        Renders into a *device-pixel* sized backing store and sets
        ``devicePixelRatio`` so the result stays sharp on Hi-DPI / 4 K
        displays (previously the SVG/raster was rendered at logical-pixel
        resolution and ended up blurred → the "corrupted" icon).
        """
        from PySide6.QtGui import QGuiApplication
        screen = QGuiApplication.primaryScreen()
        dpr    = screen.devicePixelRatio() if screen else 1.0
        phys_w = max(1, int(w * dpr))
        phys_h = max(1, int(h * dpr))

        target = QPixmap(phys_w, phys_h)
        target.setDevicePixelRatio(dpr)
        target.fill(Qt.transparent)
        try:
            low = path.suffix.lower()
            if low == ".svg" and _HAS_SVG:
                renderer = QSvgRenderer(str(path))
                if not renderer.isValid():
                    return None
                src = renderer.defaultSize()
                sw, sh = max(1, src.width()), max(1, src.height())
                # Render rect in LOGICAL units (w, h) — the painter scales by
                # dpr itself. Using phys here double-scaled and clipped on
                # Hi-DPI displays.
                scale = min(w / sw, h / sh)
                dw, dh = sw * scale, sh * scale
                ox, oy = (w - dw) / 2.0, (h - dh) / 2.0
                from PySide6.QtCore import QRectF as _QRectF
                painter = QPainter(target)
                painter.setRenderHint(QPainter.Antialiasing, True)
                painter.setRenderHint(QPainter.SmoothPixmapTransform, True)
                renderer.render(painter, _QRectF(ox, oy, dw, dh))
                painter.end()
                return target
            # Raster (WEBP / PNG / JPEG / etc.)
            # Decoded straight to card size — see load_scaled_pixmap. A card
            # thumbnail must never pull a full-resolution photo into memory.
            scaled = load_scaled_pixmap(path, phys_w, phys_h)
            if scaled is None or scaled.isNull():
                return None
            # Tag the scaled source with dpr so drawPixmap maps it 1:1 onto the
            # dpr target (otherwise the painter scales it AGAIN by dpr and the
            # bottom/right edges clip on Hi-DPI displays).
            scaled.setDevicePixelRatio(dpr)
            painter = QPainter(target)
            try:
                from PySide6.QtCore import QPointF as _QPointF
                painter.setRenderHint(QPainter.SmoothPixmapTransform, True)
                ox = (w - scaled.width() / dpr) / 2.0
                oy = (h - scaled.height() / dpr) / 2.0
                painter.drawPixmap(_QPointF(ox, oy), scaled)
            finally:
                painter.end()
            return target
        except Exception as e:
            logger.debug("Card thumb render failed (%s): %s", path, e)
            return None

    @staticmethod
    def _render_type_icon(kind: str, side: int) -> QPixmap:
        """Generic type icon, rendered aspect-preserving and DPI-aware.

        The previous implementation rendered into a logical-pixel sized
        QPixmap which made the SVG paths look chunky on 4 K monitors. We now
        size the backing store in device pixels and tag the pixmap with the
        correct ``devicePixelRatio``.
        """
        from PySide6.QtGui import QGuiApplication
        screen   = QGuiApplication.primaryScreen()
        dpr      = screen.devicePixelRatio() if screen else 1.0
        # Backing store is sized in PHYSICAL pixels for crisp 4K rendering...
        phys     = max(1, round(side * dpr))
        target   = QPixmap(phys, phys)
        target.setDevicePixelRatio(dpr)
        target.fill(Qt.transparent)

        svg_path = ICONS_DIR / f"{kind}.svg"
        if svg_path.exists():
            renderer = _load_svg_renderer(svg_path)
            if renderer is not None:
                try:
                    # ...but a QPainter on a dpr-tagged pixmap multiplies all
                    # coordinates by dpr internally. So the render rect MUST be
                    # in LOGICAL units (`side`), NOT physical (`phys`). Passing
                    # physical-sized rects made the painter draw at phys*dpr,
                    # overflowing the pixmap and clipping the bottom/right edges
                    # on every display with dpr > 1 (i.e. the 4K smartboard).
                    src = renderer.defaultSize()
                    sw, sh = max(1, src.width()), max(1, src.height())
                    inset  = max(2.0, side * 0.06)   # logical-unit margin
                    avail  = side - 2 * inset
                    scale  = min(avail / sw, avail / sh)
                    dw, dh = sw * scale, sh * scale
                    ox, oy = (side - dw) / 2.0, (side - dh) / 2.0
                    from PySide6.QtCore import QRectF as _QRectF
                    painter = QPainter(target)
                    painter.setRenderHint(QPainter.Antialiasing, True)
                    painter.setRenderHint(QPainter.SmoothPixmapTransform, True)
                    renderer.render(painter, _QRectF(ox, oy, dw, dh))
                    painter.end()
                    return target
                except Exception as e:
                    logger.debug("Type-icon render failed: %s", e)
        # Last resort fallback (handled by media_type_icon helper)
        return media_type_icon(kind, side * 2).pixmap(QSize(side, side))

    @property
    def item_id(self) -> str: return self._item.id

    def setSelected(self, sel: bool) -> None:
        if sel != self._selected:
            self._selected = sel
            self._refresh_style()

    def _refresh_style(self) -> None:
        sc = UiScale.px; t = theme_mgr.tokens
        # Fully transparent by default so aero shows through; only a faint
        # fill on hover and an accent tint when selected give click feedback.
        if self._selected:
            bg = t.accent_selected_bg; bdr = t.accent;  bw = "2px"
        else:
            bg = "transparent";        bdr = t.border; bw = "1px"
        self.setStyleSheet(f"""
            QPushButton#MediaCard {{
                background-color: {bg};
                border: {bw} solid {bdr};
                border-radius: {sc(12)}px;
                text-align: left;
                padding: 0px;
            }}
            QPushButton#MediaCard:hover {{
                background-color: {t.fill_subtle};
            }}
        """)


# ─────────────────────────────────────────────────────────────────────────────
# MediaPreviewView — middle pane (image OR vertically-stacked, lazy-loaded
# slide / page list).
#
# Design goals:
#   • PPT / PDF slides arranged **vertically**, one per row (user scrolls).
#   • Each tile takes the available preview-pane width (minus padding) so
#     the user gets a large, readable thumbnail instead of a tiny grid cell.
#   • Image previews are **bounded** — capped at a reasonable max instead of
#     expanding to fill the whole pane.
#   • **Lazy rendering**: thumbnails are only rasterised when their row
#     enters (or is about to enter) the viewport. Off-screen rows are kept
#     as cheap placeholders → constant RAM regardless of slide count.
#   • **Pixmap LRU cache** caps decoded pixmaps to ~30 entries so even with
#     a 200-slide deck the memory footprint stays modest.
# ─────────────────────────────────────────────────────────────────────────────

# Maximum pixmaps kept decoded in RAM (LRU). 30 × ~500 KB = ~15 MB ceiling.
_PIXMAP_LRU_CAP = 30
# Pixels of look-ahead/behind around the viewport when deciding "visible".
_VISIBILITY_MARGIN_PX = 320


class _LazyTile(QPushButton):
    """One vertical thumbnail slot. Knows how to render itself on demand
    and how to release the rendered pixmap when scrolled far off-screen."""

    def __init__(self, idx: int, path: Path, what: str,
                 width: int, height: int, parent=None, anim_count: int = 0):
        super().__init__(parent)
        self._idx        = idx
        self._path       = path
        self._target_w   = width
        self._target_h   = height
        self._loaded     = False
        self._selected   = False

        # Header bar at the very top of the tile ("Slide 1" / "Page 2"), with
        # the thumbnail filling the area beneath it. We DON'T use the button's
        # built-in text (which sits beside the icon) — instead a child QLabel
        # is pinned to the top and the icon is sized to fit the space below.
        # NOTE ON STYLING: nothing in this class calls setStyleSheet(). Every
        # rule lives in ONE sheet on the scroll-area content widget (see
        # MediaPreviewView._apply_tile_qss) and reaches these widgets by object
        # name. That matters at scale: Qt re-parses a stylesheet string every
        # time it is assigned, so the old per-tile/per-label styling made a
        # 100-page document parse ~300 sheets just to open, and another 100 on
        # every single page click. Selection is a dynamic property instead, so
        # switching pages repolishes exactly two tiles.
        from PySide6.QtWidgets import QLabel as _QLabel
        sc = UiScale.px
        self.setText("")
        self.setObjectName("previewTile")
        self.setProperty("selected", False)
        self._header = _QLabel(f"{what.capitalize()} {idx + 1}", self)
        self._header.setObjectName("tileHeader")
        # Same reason as MediaCard: a child that hit-tests first can absorb the
        # press, leaving dead spots on a tile that looks entirely clickable.
        self._header.setAttribute(Qt.WA_TransparentForMouseEvents, True)
        self._header.setAlignment(Qt.AlignLeft | Qt.AlignVCenter)
        self._header.setFixedWidth(width - sc(24))
        self._header.move(sc(12), sc(6))

        # Animation count badge (PPT slides only) — pinned to the FAR RIGHT of
        # the same header row, e.g. "5 anim". Hidden when the slide has none.
        if anim_count > 0:
            bw = sc(96)
            self._anim_badge = _QLabel(
                f"{anim_count} anim" + ("s" if anim_count != 1 else ""), self)
            self._anim_badge.setObjectName("tileBadge")
            self._anim_badge.setAttribute(Qt.WA_TransparentForMouseEvents, True)
            self._anim_badge.setAlignment(Qt.AlignRight | Qt.AlignVCenter)
            self._anim_badge.setFixedWidth(bw)
            self._anim_badge.move(width - sc(12) - bw, sc(6))
            # Don't let a long slide label run under the badge.
            self._header.setFixedWidth(width - sc(24) - bw)

        self.setCursor(Qt.PointingHandCursor)
        self.setFixedSize(width, height)
        # Icon occupies the area BELOW the header bar.
        self.setIconSize(QSize(width - sc(20), height - sc(40)))

        # The loading placeholder is built ON DEMAND (see _ensure_spinner).
        # Only a handful of tiles are ever in view, so creating one QLabel per
        # tile up front was ~N wasted widgets on open for no visible benefit.
        self._spinner = None
        self._load_pending = False

    @property
    def idx(self)  -> int: return self._idx
    @property
    def path(self) -> Path: return self._path

    def is_loaded(self) -> bool:
        return self._loaded

    def _ensure_spinner(self):
        """Create the 'loading' placeholder the first time this tile is asked
        to render. Styled by the shared sheet via its object name."""
        if self._spinner is None:
            from PySide6.QtWidgets import QLabel as _QLabel
            sc = UiScale.px
            self._spinner = _QLabel("Getting things ready… 😅", self)
            self._spinner.setObjectName("tileSpinner")
            self._spinner.setAttribute(Qt.WA_TransparentForMouseEvents, True)
            self._spinner.setAlignment(Qt.AlignCenter)
            self._spinner.setGeometry(sc(10), sc(30),
                                      self._target_w - sc(20),
                                      self._target_h - sc(40))
        return self._spinner

    def ensure_loaded(self, get_pixmap) -> None:
        """Show a loading cue, then decode the (downscaled) thumbnail on the
        next event-loop tick so the placeholder actually paints first. The
        decode is bounded in size by `get_pixmap` → never blows up RAM."""
        if self._loaded or self._load_pending:
            return
        self._load_pending = True
        self._ensure_spinner().show()
        from PySide6.QtCore import QTimer
        # ``self`` as the context, so closing the manager mid-load (it is
        # deleted on close) cancels this instead of firing into a dead tile.
        QTimer.singleShot(0, self, lambda: self._do_load(get_pixmap))

    def _do_load(self, get_pixmap) -> None:
        if not self._load_pending:
            return
        self._load_pending = False
        pm = get_pixmap(self._path, self._target_w - UiScale.px(20),
                                     self._target_h - UiScale.px(36))
        if self._spinner is not None:
            self._spinner.hide()
        if pm is not None and not pm.isNull():
            self.setIcon(QIcon(pm))
        self._loaded = True

    def unload(self) -> None:
        """Free the icon to shrink RAM (caller decides when off-screen
        enough to evict)."""
        self._load_pending = False
        if self._spinner is not None:
            self._spinner.hide()
        if not self._loaded:
            return
        self.setIcon(QIcon())
        self._loaded = False

    def set_selected(self, sel: bool) -> None:
        """Flip the dynamic property the shared sheet keys off, then repolish
        just this widget. O(1) — the whole point of not using setStyleSheet."""
        if bool(self.property("selected")) == bool(sel):
            return
        self.setProperty("selected", bool(sel))
        st = self.style()
        st.unpolish(self)
        st.polish(self)
        self.update()


class MediaPreviewView(QScrollArea):
    page_selected = Signal(int)   # 0-based index; -1 = "no specific selection"

    def __init__(self, library: MediaLibrary, parent=None):
        super().__init__(parent)
        self._library = library
        self._item: Optional[MediaItem] = None
        self._selected_index: int = -1
        self._tiles: list[_LazyTile] = []
        # Incremental tile build state (see _build_chunk).
        self._pending: list = []
        self._pending_what: str = "page"
        self._tile_w = self._tile_h = 0
        self._reserve: Optional[QSpacerItem] = None
        self._build_gen: int = 0
        # Row pitch + y of the first tile — see _update_visibility.
        self._row_h: int = 0
        self._origin_y: int = 0

        # Pixmap LRU cache — keyed by path string. We use an OrderedDict for
        # cheap O(1) move-to-end semantics on access.
        from collections import OrderedDict
        self._pm_cache: "OrderedDict[str, QPixmap]" = OrderedDict()

        sc = UiScale.px; t = theme_mgr.tokens

        self.setWidgetResizable(True)
        self.setHorizontalScrollBarPolicy(Qt.ScrollBarAlwaysOff)
        setup_scroll(self)   # modern scrollbar + touch flick
        self.setFrameShape(QFrame.NoFrame)

        self._content = QWidget()
        self._content.setObjectName("previewContent")
        # Scoped to #previewContent so it can't leak onto the tiles below —
        # the tile rules are appended to this same sheet by _apply_tile_qss().
        self._content.setStyleSheet("QWidget#previewContent { background: transparent; }")
        self._tile_qss_applied = False
        self._layout = QVBoxLayout(self._content)
        self._layout.setContentsMargins(sc(12), sc(12), sc(12), sc(12))
        self._layout.setSpacing(sc(10))
        self.setWidget(self._content)

        # Recompute which tiles are visible whenever the user scrolls.
        self.verticalScrollBar().valueChanged.connect(self._update_visibility)

        self._show_empty()

    @property
    def selected_index(self) -> int: return self._selected_index

    # ── lifecycle ───────────────────────────────────────────────────────

    def set_item(self, item: Optional[MediaItem]) -> None:
        self._item            = item
        self._selected_index  = -1
        self._clear()
        if item is None:
            self._show_empty()
            return
        if item.type == "image":
            self._show_image(item)
        else:
            self._show_pages_vertical(item)
        # Defer the first visibility pass to after layout settles.
        from PySide6.QtCore import QTimer
        QTimer.singleShot(0, self._update_visibility)

    def resizeEvent(self, e):
        super().resizeEvent(e)
        # Width changed → re-evaluate which tiles are in view, but DO NOT
        # rebuild — tiles keep their pre-decided sizes (avoids re-rendering
        # all SVGs every time the user resizes the window).
        self._update_visibility()

    # ── internals ───────────────────────────────────────────────────────

    def _clear(self) -> None:
        # Retire any in-flight incremental build before tearing the layout
        # down — otherwise its next timer would insert tiles into the layout
        # of the item the user just navigated away from.
        self._build_gen += 1
        self._pending = []
        self._reserve = None
        while self._layout.count():
            it = self._layout.takeAt(0)
            w  = it.widget()
            if w:
                # Hidden now, deleted later: deleteLater alone leaves it on
                # screen until the next event-loop pass, drawn underneath
                # whatever replaces it.
                w.hide()
                w.deleteLater()
        self._tiles  = []
        self._pm_cache.clear()

    def _show_empty(self) -> None:
        sc = UiScale.px; t = theme_mgr.tokens
        msg = QLabel("Pick something to preview ✨")
        msg.setAlignment(Qt.AlignCenter)
        msg.setStyleSheet(
            f"color: {t.text_secondary}; font-size: {sc(13)}px; "
            f"padding: {sc(40)}px; background: transparent;"
        )
        self._layout.addWidget(msg)
        self._layout.addStretch()

    # -------- IMAGE PREVIEW (bounded — never fills the whole pane) ------

    def _show_image(self, item: MediaItem) -> None:
        sc = UiScale.px; t = theme_mgr.tokens
        path = self._library.absolute_path(item.original_relpath)

        viewer = QLabel()
        viewer.setAlignment(Qt.AlignCenter)
        viewer.setStyleSheet(
            f"background: {t.card_grad}; border: 1px solid {t.border}; "
            f"border-radius: {sc(10)}px; padding: {sc(8)}px;"
        )

        if path is not None and path.exists():
            # CAP the preview at a sensible size so it doesn't blow up to
            # fill the entire pane on a 4 K display, and decode at that cap
            # rather than decoding full-res and shrinking afterwards.
            max_w = min(sc(720), max(sc(320),
                                      self.viewport().width() - sc(60)))
            max_h = sc(540)
            scaled = load_scaled_pixmap(path, max_w, max_h)
            if scaled is not None and not scaled.isNull():
                viewer.setPixmap(scaled)
                viewer.setFixedSize(scaled.width() + sc(16),
                                     scaled.height() + sc(16))
            else:
                viewer.setText("(image unavailable)")
                viewer.setMinimumHeight(sc(200))
        else:
            viewer.setText("(file missing)")
            viewer.setMinimumHeight(sc(200))

        # Wrap in a horizontally-centered row so the bounded viewer is
        # centered inside the (possibly wider) preview pane.
        row = QHBoxLayout()
        row.addStretch()
        row.addWidget(viewer)
        row.addStretch()
        wrap = QWidget()
        wrap.setStyleSheet("background: transparent;")
        wrap.setLayout(row)
        self._layout.addWidget(wrap)
        self._layout.addStretch()

    # -------- VERTICAL PAGE LIST (lazy-loaded tiles) --------------------

    def _show_pages_vertical(self, item: MediaItem) -> None:
        sc = UiScale.px; t = theme_mgr.tokens
        preview_paths = self._library.preview_paths(item)
        if not preview_paths:
            msg = QLabel("Not rendered yet — press Render on the right")
            msg.setAlignment(Qt.AlignCenter)
            msg.setStyleSheet(
                f"color: {t.text_muted}; padding: {sc(40)}px; "
                f"background: transparent;"
            )
            self._layout.addWidget(msg)
            self._layout.addStretch()
            return

        # Header showing total count.
        what   = "slide" if item.type == "ppt" else "page"
        header = QLabel(
            f"{len(preview_paths)} {what}{'s' if len(preview_paths) != 1 else ''}"
        )
        header.setStyleSheet(
            f"color: {t.text_secondary}; font-size: {sc(11)}px; "
            f"background: transparent;"
        )
        self._layout.addWidget(header)

        # Each tile gets the full viewport width (minus padding). We pick a
        # 16:9 height so PPT / 4:3 PDF previews both look good.
        avail_w = max(sc(280), self.viewport().width() - sc(48))
        # Cap so we don't end up with absurdly huge tiles on a 4 K board.
        tile_w  = min(sc(640), avail_w)
        tile_h  = int(tile_w * 9 / 16)

        # One stylesheet parse for the whole list (see _LazyTile's styling note).
        self._apply_tile_qss()

        # ---- incremental build -------------------------------------------
        # Creating every tile up front is what made a big document hang on
        # open: the cost is linear in page count and it all lands in one
        # blocking burst before the first frame (~0.5 s for a 200-page PDF on
        # a dev box, far worse on classroom hardware). Instead build one
        # viewport's worth now and stream the rest in small chunks between
        # frames, so the pane appears immediately and stays interactive.
        #
        # A spacer reserves the full height of the not-yet-built tiles, so the
        # scrollbar is correctly sized from the very first frame and does not
        # grow under the user's finger while the rest arrive.
        self._pending = list(enumerate(preview_paths))
        self._pending_what = what
        self._tile_w, self._tile_h = tile_w, tile_h
        self._build_gen += 1
        row_h = tile_h + self._layout.spacing()
        # Tile positions are computed arithmetically rather than read back off
        # the widgets — see _tile_top(). Every row is the same fixed height, so
        # the layout can be predicted exactly, and the prediction is available
        # the instant a tile is created instead of one relayout later.
        self._row_h = row_h
        self._origin_y = (self._layout.contentsMargins().top()
                          + header.sizeHint().height() + self._layout.spacing())
        self._reserve = QSpacerItem(1, row_h * len(self._pending),
                                    QSizePolicy.Minimum, QSizePolicy.Fixed)
        self._layout.addItem(self._reserve)
        self._layout.addStretch()
        self._build_chunk(self._build_gen, first=True)

    # Tiles added per chunk. Small enough that a chunk fits comfortably in one
    # frame even on slow hardware, large enough that a long document finishes
    # streaming quickly.
    _BUILD_CHUNK = 8

    def _build_chunk(self, gen: int, first: bool = False) -> None:
        """Append the next batch of tiles, then reschedule until done.

        ``gen`` guards against a stale timer: if the user picks another item
        mid-build, _clear() bumps the generation and this run retires."""
        if gen != self._build_gen or not self._pending:
            return
        sc = UiScale.px
        anim = (self._item.anim_counts
                if self._item is not None and self._item.type == "ppt" else ())
        # Enough to cover the viewport on the first pass so nothing looks empty.
        row_h = self._tile_h + self._layout.spacing()
        n = (max(self._BUILD_CHUNK, self.viewport().height() // max(1, row_h) + 2)
             if first else self._BUILD_CHUNK)
        batch, self._pending = self._pending[:n], self._pending[n:]

        # Insert ahead of the reserve spacer + trailing stretch.
        insert_at = self._layout.count() - 2
        for i, p in batch:
            anim_count = int(anim[i]) if i < len(anim) else 0
            tile = _LazyTile(i, p, self._pending_what,
                             self._tile_w, self._tile_h, anim_count=anim_count)
            tile.clicked.connect(lambda _, idx=i: self._on_tile_clicked(idx))
            # Centered directly in the column. The old code wrapped every tile
            # in its own QWidget + QHBoxLayout purely to center it, which
            # doubled the widget count for zero visual difference — on a
            # 100-page PDF that alone was 200 extra objects to build, lay out
            # and destroy.
            self._layout.insertWidget(insert_at, tile, 0, Qt.AlignHCenter)
            insert_at += 1
            self._tiles.append(tile)
            if i == self._selected_index:
                tile.set_selected(True)

        # Give back exactly the height the new tiles now occupy, so the total
        # content height — and therefore the scroll range — never moves.
        self._reserve.changeSize(1, row_h * len(self._pending),
                                 QSizePolicy.Minimum, QSizePolicy.Fixed)
        self._layout.invalidate()

        self._update_visibility()
        if self._pending:
            from PySide6.QtCore import QTimer
            # Context ``self``: a close between chunks deletes the view.
            QTimer.singleShot(0, self, lambda: self._build_chunk(gen))

    def _apply_tile_qss(self) -> None:
        """Install the single sheet that styles every tile and its labels.

        Re-applied per document so a theme switch between openings is picked
        up, but never per tile and never per click."""
        sc = UiScale.px; t = theme_mgr.tokens
        self._content.setStyleSheet(
            f"QWidget#previewContent {{ background: transparent; }}"
            f"QPushButton#previewTile {{ background-color: {t.card_grad}; "
            f"color: {t.text_primary}; border: 1px solid {t.border}; "
            f"border-radius: {sc(10)}px; font-size: {sc(11)}px; "
            f"padding-top: {sc(26)}px; padding-bottom: {sc(6)}px; }}"
            f"QPushButton#previewTile:hover {{ background-color: {t.bg_tile_hover}; }}"
            f'QPushButton#previewTile[selected="true"] {{ '
            f"background-color: {t.accent_selected_bg}; "
            f"border: 2px solid {t.accent}; font-weight: 700; }}"
            f"QLabel#tileHeader {{ color: {t.text_primary}; "
            f"font-size: {sc(11)}px; font-weight: 700; background: transparent; }}"
            f"QLabel#tileBadge {{ color: {t.accent}; "
            f"font-size: {sc(11)}px; font-weight: 700; background: transparent; }}"
            f"QLabel#tileSpinner {{ color: {t.text_muted}; "
            f"font-size: {sc(12)}px; font-weight: 600; background: transparent; }}"
        )
        self._tile_qss_applied = True

    # ── lazy-load mechanics ─────────────────────────────────────────────

    def _update_visibility(self, _value: int = 0) -> None:
        """Render tiles inside / near the viewport; unload tiles far away.

        Tile positions come from the fixed row pitch, NOT from ``tile.y()``.
        Inside a widgetResizable QScrollArea a just-inserted widget reports
        y() == 0 until the area has resized its content and the layout has
        run, so during an incremental build every new tile looked like it sat
        at the top of the viewport and got queued for decoding — silently
        defeating the lazy loading and decoding all N pages on open, the exact
        cost this class exists to avoid. Every row is the same fixed height,
        so the true position is simple arithmetic and is correct the instant a
        tile is created."""
        if not self._tiles or self._row_h <= 0:
            return
        sb       = self.verticalScrollBar()
        view_top = sb.value()
        view_bot = view_top + self.viewport().height()
        # Expand the "visible" band by VISIBILITY_MARGIN_PX above and below
        # the viewport so scrolling feels instant — the next tile is already
        # rendered by the time it slides in.
        visible_top = view_top - _VISIBILITY_MARGIN_PX
        visible_bot = view_bot + _VISIBILITY_MARGIN_PX

        # Solve for the visible index band directly instead of testing all N
        # tiles on every scroll tick — this now costs the same on a 500-page
        # PDF as on a 5-page one.
        span = visible_top - self._origin_y - self._tile_h
        first = max(0, -(-span // self._row_h) if span > 0 else 0)
        last  = min(len(self._tiles) - 1,
                    (visible_bot - self._origin_y) // self._row_h)
        for i in range(int(first), int(last) + 1):
            self._tiles[i].ensure_loaded(self._get_pm)
            # We deliberately do NOT unload tiles when they leave the view
            # band — the LRU cache below already caps total decoded
            # pixmaps. Setting/unsetting the icon repeatedly while
            # scrolling causes flicker.

    def _get_pm(self, path: Path, w: int, h: int) -> Optional[QPixmap]:
        """LRU-cached pixmap fetch. Hot path during scrolling."""
        key = str(path)
        cached = self._pm_cache.get(key)
        if cached is not None:
            self._pm_cache.move_to_end(key)
            return cached

        pm = _load_preview_pixmap(path, w, h)
        if pm is None:
            return None

        self._pm_cache[key] = pm
        # Evict oldest entries to keep cache bounded.
        while len(self._pm_cache) > _PIXMAP_LRU_CAP:
            old_key, old_pm = self._pm_cache.popitem(last=False)
            # Find the tile pointing at that path and clear its icon so
            # the QPixmap can actually be garbage-collected.
            for t in self._tiles:
                if str(t.path) == old_key and t.is_loaded():
                    t.unload()
                    break
        return pm

    # ── tile interaction ────────────────────────────────────────────────

    def _on_tile_clicked(self, idx: int) -> None:
        click_sound.play_click()
        prev = self._selected_index
        self._selected_index = idx
        # Touch only the two tiles whose appearance actually changes. This used
        # to re-assign a freshly built stylesheet to EVERY tile, so picking a
        # page in a 100-page PDF cost ~100 stylesheet parses — measurably worse
        # than opening the document in the first place.
        if 0 <= prev < len(self._tiles):
            self._tiles[prev].set_selected(False)
        if 0 <= idx < len(self._tiles):
            self._tiles[idx].set_selected(True)
        self.page_selected.emit(idx)


# ─────────────────────────────────────────────────────────────────────────────
# Module-level helper: preview-pixmap loader (SVG-aware, DPI-aware)
# Used by _LazyTile via the MediaPreviewView LRU cache.
# ─────────────────────────────────────────────────────────────────────────────

def _load_preview_pixmap(path: Path, target_w: int,
                          target_h: int) -> Optional[QPixmap]:
    """Load `path` into a (target_w × target_h) pixmap with aspect preserved.
    Handles SVG via QSvgRenderer, raster via QPixmap. Returns None on failure.

    Renders at device-pixel resolution and tags devicePixelRatio for crisp
    output on Hi-DPI screens.
    """
    if target_w <= 0 or target_h <= 0:
        return None
    from PySide6.QtGui import QGuiApplication
    screen = QGuiApplication.primaryScreen()
    dpr    = screen.devicePixelRatio() if screen else 1.0
    phys_w = max(1, int(target_w * dpr))
    phys_h = max(1, int(target_h * dpr))

    low = path.suffix.lower()
    if low == ".svg" and _HAS_SVG:
        try:
            renderer = QSvgRenderer(str(path))
            if not renderer.isValid():
                return None
            from PySide6.QtCore import QRectF as _QRectF
            pm = QPixmap(phys_w, phys_h)
            pm.setDevicePixelRatio(dpr)
            pm.fill(Qt.transparent)
            p  = QPainter(pm)
            p.setRenderHint(QPainter.Antialiasing, True)
            p.setRenderHint(QPainter.SmoothPixmapTransform, True)
            sz       = renderer.defaultSize()
            sw, sh   = max(1, sz.width()), max(1, sz.height())
            scale    = min(phys_w / sw, phys_h / sh)
            dw, dh   = sw * scale, sh * scale
            ox, oy   = (phys_w - dw) / 2.0, (phys_h - dh) / 2.0
            renderer.render(p, _QRectF(ox, oy, dw, dh))
            p.end()
            return pm
        except Exception as e:
            logger.debug("SVG preview render failed (%s): %s", path, e)
            return None

    # Raster: decode DIRECTLY at the downscaled size via QImageReader so we
    # never materialise the full-resolution bitmap in RAM. A 6000×4000 photo
    # would otherwise spike ~100 MB per decode; this keeps each load bounded to
    # roughly the tile size, which is what keeps total RAM well under budget.
    from PySide6.QtGui import QImageReader
    reader = QImageReader(str(path))
    reader.setAutoTransform(True)              # honour EXIF orientation
    native = reader.size()
    if native.isValid() and native.width() > 0 and native.height() > 0:
        scale = min(phys_w / native.width(), phys_h / native.height(), 1.0)
        tw = max(1, int(native.width()  * scale))
        th = max(1, int(native.height() * scale))
        reader.setScaledSize(QSize(tw, th))    # decoder downscales as it reads
    img = reader.read()
    if img.isNull():
        return None
    pm = QPixmap.fromImage(img)
    if pm.isNull():
        return None
    pm.setDevicePixelRatio(dpr)
    return pm


# ─────────────────────────────────────────────────────────────────────────────
# Tiny painted glyphs (chevron / tick) for the split-button dropdown.
#
# Painted rather than loaded from assets/icons because there is no chevron or
# tick SVG there, and painting keeps them exactly on the theme's text colour in
# both light and dark. Drawn at 2× and tagged with a device pixel ratio so they
# stay crisp on HiDPI, same trick as the preview pixmaps above.
# ─────────────────────────────────────────────────────────────────────────────
def _glyph_icon(kind: str, size: int, color: str) -> QIcon:
    """``kind`` is "chevron_down" | "chevron_up" | "check" | "blank"."""
    dpr = 2.0
    pm = QPixmap(max(2, int(size * dpr)), max(2, int(size * dpr)))
    pm.setDevicePixelRatio(dpr)
    pm.fill(Qt.transparent)
    # "blank" is a fully transparent icon of the SAME size as the tick. It is
    # what the unchecked rows carry, so their text starts on the same column as
    # the checked one instead of sliding left when the tick moves away.
    if kind == "blank":
        return QIcon(pm)
    try:
        p = QPainter(pm)
        p.setRenderHint(QPainter.Antialiasing, True)
        pen = QPen(theme_mgr.qcolor(color))
        pen.setWidthF(max(1.3, size * 0.11))
        pen.setCapStyle(Qt.RoundCap)
        pen.setJoinStyle(Qt.RoundJoin)
        p.setPen(pen)
        s = float(size)
        if kind == "check":
            pts = [QPointF(s * 0.20, s * 0.54), QPointF(s * 0.41, s * 0.74),
                   QPointF(s * 0.80, s * 0.28)]
        else:
            y0, y1 = ((s * 0.62, s * 0.40) if kind == "chevron_up"
                      else (s * 0.40, s * 0.62))
            pts = [QPointF(s * 0.26, y0), QPointF(s * 0.50, y1),
                   QPointF(s * 0.74, y0)]
        p.drawPolyline(QPolygonF(pts))
        p.end()
    except Exception as e:                          # never let a glyph crash UI
        logger.debug("glyph paint failed (%s): %s", kind, e)
    return QIcon(pm)


# ─────────────────────────────────────────────────────────────────────────────
# MediaActionsPanel — right pane (Set As Background / Add / Delete / Info)
# ─────────────────────────────────────────────────────────────────────────────
class MediaActionsPanel(QFrame):
    # The Layout last picked in THIS RUN of LogicBoard. The Media Manager is
    # rebuilt every time it opens, and the choice used to go with it, so a
    # teacher setting a deck of slides as backgrounds re-picked "Stretch"
    # for every one. It lives on the class, not in settings: it lasts until
    # it is changed or LogicBoard is restarted, then it is "fit" again.
    _session_layout_mode: str = "fit"

    # (item.id, index_or_-1, fit mode). The mode rides along rather than
    # getting its own signal per button: all three do the same thing to the
    # same page and only differ in how the picture is laid on it.
    set_background_requested = Signal(str, int, str)
    # (item.id, fit mode) — every slide/page → its own page. The mode is the
    # one ticked in the split button's dropdown and is applied to EVERY page
    # the action creates.
    set_whole_background_requested = Signal(str, str)
    add_object_requested     = Signal(str, int)
    # Take this media OFF the board — every placed copy and every page
    # background using it — while leaving the library entry, the original
    # file and the render cache exactly where they are. Deliberately a
    # separate signal from ``delete_requested``: the two differ in what
    # survives, which is the one thing a user must never have to guess.
    remove_from_board_requested = Signal(str)
    delete_requested         = Signal(str)
    # Render media that has nothing rendered, from the original already on
    # the board — no second import.
    render_requested         = Signal(str)
    reset_background_requested = Signal()         # reset page to default bg
    background_step_requested  = Signal(str, int, int)  # (item_id, slide, step)

    def __init__(self, parent=None):
        super().__init__(parent)
        self._item: Optional[MediaItem] = None
        self._selected_index: int = -1

        sc = UiScale.px; t = theme_mgr.tokens
        self.setObjectName("MediaActionsPanel")
        self.setStyleSheet(f"""
            QFrame#MediaActionsPanel {{
                background-color: transparent;
                border-radius: {sc(12)}px;
                border: 1px solid {t.border};
            }}
        """)

        # Hard bound on the pane. Eliding the name is the real fix, but a
        # QSplitter sizes children from their minimumSizeHint, so ANY future
        # long unbreakable string in here could grab the dialog again. This
        # keeps the Actions pane a side panel by construction.
        self.setMinimumWidth(sc(220))
        self.setMaximumWidth(sc(420))

        # The pane's content is taller than the pane on short screens (a 53 MB
        # deck's info block alone runs four lines), and a QVBoxLayout that runs
        # out of room does not clip politely — it squeezes widgets past their
        # minimums until the buttons overlap the text. So the content lives in
        # a scroll area with the app's standard finger-flick + slim scrollbar
        # treatment: it lays out at its natural size and the user drags for the
        # rest. The frame, its border and the width bounds stay on the panel.
        outer = QVBoxLayout(self)
        outer.setContentsMargins(sc(4), sc(4), sc(4), sc(4))
        outer.setSpacing(0)

        self._scroll = QScrollArea()
        self._scroll.setWidgetResizable(True)
        self._scroll.setHorizontalScrollBarPolicy(Qt.ScrollBarAlwaysOff)
        setup_scroll(self._scroll)      # same scrollbar + kinetic flick as
                                        # the library pane next door
        outer.addWidget(self._scroll)

        content = QWidget()
        content.setStyleSheet("background: transparent;")
        self._scroll.setWidget(content)

        layout = QVBoxLayout(content)
        layout.setContentsMargins(sc(10), sc(10), sc(10), sc(10))
        layout.setSpacing(sc(10))

        # An open dropdown is anchored to where the split button WAS; scrolling
        # moves the button out from under it, so it closes — exactly what a
        # native menu does when its anchor scrolls away.
        vbar = self._scroll.verticalScrollBar()
        if vbar is not None:
            vbar.valueChanged.connect(lambda _v: self._close_whole_bg_menu())

        title = QLabel("Actions")
        title.setStyleSheet(
            f"color: {t.text_primary}; font-size: {sc(15)}px; "
            f"font-weight: 700; background: transparent;"
        )
        layout.addWidget(title)

        self._info = QLabel("Pick a media item to see its options ✨")
        self._info.setStyleSheet(
            f"color: {t.text_secondary}; font-size: {sc(11)}px; "
            f"background: transparent;"
        )
        self._info.setWordWrap(True)
        self._info.setMinimumHeight(sc(80))
        self._info.setAlignment(Qt.AlignTop | Qt.AlignLeft)
        layout.addWidget(self._info)
        # Raw pieces for _apply_info; the name is elided to fit the panel.
        self._info_name: str = ""
        self._info_tail: list = []
        self._info_scope: str = ""

        self._buttons: list[QPushButton] = []
        # ``_whole_bg_btn`` is tracked separately so ``set_item`` can show
        # it only for multi-page media (PPT / PDF with >1 frame). It's not
        # useful for a single image, so it stays hidden there to avoid
        # implying "spread this one image across many pages".
        self._whole_bg_btn: Optional[QPushButton] = None
        self._whole_bg_arrow: Optional[QPushButton] = None
        self._whole_bg_row: Optional[QWidget] = None
        self._whole_bg_hint: Optional[QLabel] = None
        self._whole_bg_menu: Optional[QFrame] = None
        # The shared layout choice. ``_whole_bg_opts`` stays for the
        # dropdown machinery that is no longer built (see
        # _build_whole_bg_row); ``_layout_opts`` is the live one.
        #
        # Defaults to "fit": the whole slide or page visible, centred, nothing
        # cropped off. "auto" (fill) used to be the default, and it trimmed
        # the edge of every slide whose shape did not match the board — which
        # on a lesson is usually where the slide's footer or a label sits.
        self._layout_mode: str = MediaActionsPanel._session_layout_mode
        self._layout_opts: dict = {}
        self._whole_bg_opts: dict = {}
        # "split" marks the row that is NOT a plain button: the whole-media
        # action carries a dropdown arrow, so it is built by
        # _build_whole_bg_row() at exactly this position in the stack.
        # The standalone "Fit To Canvas" / "Stretch To Canvas" buttons used to
        # sit here. They are gone: three near-identical full-width buttons for
        # one action ate the pane's height, and the layout they each hard-coded
        # is now a pick in the split button's dropdown. "Set As Background"
        # keeps the original auto behaviour — see CanvasWidget.BG_FIT_MODES —
        # and _emit_set_bg still takes a mode, so nothing downstream changed.
        # ORDER MATTERS, and it is this: the plain placement actions first,
        # then the background block last.
        #
        # The background group is three controls that only make sense read
        # together — two buttons and the Layout setting they share — so it
        # sits at the FOOT of the section as one unit rather than being split
        # up by "Add To Board" and friends. Keeping Layout adjacent to the two
        # buttons it governs is what stops it reading as a global setting.
        # ── Render (only for media with nothing rendered) ──────────────
        self._render_btn = QPushButton("Render")
        self._render_btn.setMinimumHeight(sc(42))
        self._render_btn.setCursor(Qt.PointingHandCursor)
        self._render_btn.setStyleSheet(f"""
            QPushButton {{
                background-color: {t.accent_grad}; color: {t.text_on_accent};
                border: none; border-radius: {sc(8)}px;
                font-size: {sc(13)}px; font-weight: 700;
                padding: 0 {sc(12)}px;
            }}
            QPushButton:hover {{ background-color: {t.accent_grad_hover}; }}
        """)
        self._render_btn.clicked.connect(self._on_render)
        self._render_btn.hide()
        layout.addWidget(self._render_btn)
        self._render_note = QLabel("")
        self._render_note.setWordWrap(True)
        self._render_note.setStyleSheet(
            f"color: {t.warning_text}; font-size: {sc(11)}px; "
            f"background: transparent;")
        self._render_note.hide()
        layout.addWidget(self._render_note)
        layout.addSpacing(sc(4))
        self._btn_by_label: dict = {}

        for label, slot, danger in [
            ("Add To Board",                   self._on_add,          False),
            # Board-only removal, immediately above the permanent one so the
            # gentler option is the one found first — and so the pair reads
            # as a choice rather than Delete being the only way to get media
            # off a page.
            # Yellow: it takes the media off the board but keeps it — a
            # caution, not the red of the permanent delete below it.
            ("Remove From Board",              self._on_remove_from_board, "warning"),
            ("Delete Media",                   self._on_delete,       True),
            ("Set As Background",              self._on_set_bg,       False),
            # "Media" rather than "Presentation": this button is shown for
            # multi-page PDFs as well as PPT decks (see set_item), and
            # calling a PDF handout a "presentation" reads as a bug.
            ("Set Whole Media As Bg",          "split",               False),
            # Layout closes the group, directly under the two controls it
            # governs and below everything it does not.
            ("",                               "layout",              False),
        ]:
            if label == "Set As Background":
                layout.addSpacing(sc(10))
            if slot == "split":
                self._build_whole_bg_row(layout, label)
                continue
            if slot == "layout":
                self._build_layout_section(layout)
                layout.addSpacing(sc(6))
                continue
            b = QPushButton(label)
            b.setMinimumHeight(sc(42))
            b.setCursor(Qt.PointingHandCursor)
            b.setStyleSheet(self._action_qss(danger))
            b.clicked.connect(slot)
            b.setEnabled(False)
            layout.addWidget(b)
            self._buttons.append(b)
            self._btn_by_label[label] = b

        # ── Background Information section ───────────────────────────────
        # Separator with breathing room above/below, matching UI styling.
        sep = QFrame()
        sep.setFrameShape(QFrame.HLine)
        sep.setFixedHeight(1)
        sep.setStyleSheet(f"background: {t.border}; border: none;")
        layout.addSpacing(sc(8))
        layout.addWidget(sep)
        layout.addSpacing(sc(8))

        bg_title = QLabel("Background Information")
        bg_title.setStyleSheet(
            f"color: {t.text_primary}; font-size: {sc(13)}px; "
            f"font-weight: 700; background: transparent;"
        )
        layout.addWidget(bg_title)

        # Current background label (always shows something).
        self._bg_title_label = QLabel("Default Background")
        self._bg_title_label.setStyleSheet(
            f"color: {t.text_primary}; font-size: {sc(12)}px; "
            f"font-weight: 600; background: transparent;"
        )
        self._bg_title_label.setWordWrap(True)
        layout.addWidget(self._bg_title_label)

        self._bg_detail_label = QLabel("")
        self._bg_detail_label.setStyleSheet(
            f"color: {t.text_secondary}; font-size: {sc(11)}px; "
            f"background: transparent;"
        )
        self._bg_detail_label.setWordWrap(True)
        # Reserve height so toggling detail text never shifts the layout.
        self._bg_detail_label.setMinimumHeight(sc(16))
        layout.addWidget(self._bg_detail_label)

        # ── Animation step controls (PPT-slide backgrounds with build-steps) ──
        self._bg_anim = None   # current {item_id, slide, step, total_steps}
        self._bg_anim_row = QWidget()
        self._bg_anim_row.setStyleSheet("background: transparent;")
        _ar = QHBoxLayout(self._bg_anim_row)
        _ar.setContentsMargins(0, sc(8), 0, 0); _ar.setSpacing(sc(8))

        def _icon_btn(name):
            b = QPushButton()
            b.setFixedSize(sc(36), sc(34))
            b.setCursor(Qt.PointingHandCursor)
            b.setIcon(tool_icon(name, sc(18), color=t.text_primary))
            b.setIconSize(QSize(sc(18), sc(18)))
            b.setStyleSheet(
                f"QPushButton {{ background-color: {t.card_grad}; "
                f"border: 1px solid {t.border}; border-radius: {sc(8)}px; }}"
                f"QPushButton:hover {{ background-color: {t.bg_tile_hover}; "
                f"border-color: {t.accent}; }}"
                f"QPushButton:disabled {{ background-color: {t.card_grad}; }}")
            return b

        self._bg_prev_btn = _icon_btn("prev")
        self._bg_prev_btn.clicked.connect(self._on_bg_prev)
        self._bg_next_btn = _icon_btn("next")
        self._bg_next_btn.clicked.connect(self._on_bg_next)
        self._bg_anim_label = QLabel("")
        self._bg_anim_label.setAlignment(Qt.AlignCenter)
        self._bg_anim_label.setStyleSheet(
            f"color: {t.text_secondary}; font-size: {sc(12)}px; "
            f"font-weight: 700; background: transparent;")
        _ar.addWidget(self._bg_prev_btn)
        _ar.addWidget(self._bg_anim_label, 1)
        _ar.addWidget(self._bg_next_btn)
        self._bg_anim_row.hide()
        layout.addWidget(self._bg_anim_row)

        self._reset_bg_btn = QPushButton("Reset To Default Background")
        self._reset_bg_btn.setMinimumHeight(sc(38))
        self._reset_bg_btn.setCursor(Qt.PointingHandCursor)
        self._reset_bg_btn.setStyleSheet(
            f"QPushButton {{ background-color: {t.card_grad}; "
            f"color: {t.text_primary}; border: 1px solid {t.border}; "
            f"border-radius: {sc(8)}px; font-size: {sc(12)}px; "
            f"text-align: center; padding: 0 {sc(12)}px; font-weight: 600; }}"
            f"QPushButton:hover {{ background-color: {t.bg_tile_hover}; "
            f"border-color: {t.accent}; }}"
            f"QPushButton:disabled {{ color: {t.text_muted}; "
            f"background-color: {t.card_grad}; border-color: {t.border}; }}"
        )
        self._reset_bg_btn.clicked.connect(self._on_reset_bg)
        layout.addSpacing(sc(6))
        layout.addWidget(self._reset_bg_btn)

        layout.addStretch()

        # A QScrollArea reports a tiny minimumSizeHint of its own, so putting
        # the content inside one ALSO threw away the width its buttons need —
        # the pane could then be dragged narrower than "Reset To Default
        # Background" and clip it, with no horizontal bar to scroll it back.
        # Hand that demand back, capped by the pane's own maximum so a long
        # future label still can't grow the Actions pane without bound.
        want = content.minimumSizeHint().width() + sc(16)   # + scrollbar lane
        self._scroll.setMinimumWidth(min(want, self.maximumWidth() - sc(12)))

    # ── shared button styling ────────────────────────────────────────────
    def _action_qss(self, danger: bool = False, corners: str = "all") -> str:
        """Stylesheet for the action buttons.

        ``corners`` is "all" for a normal button, or "left"/"right" for the two
        halves of the split button — the seam between them is drawn once, by
        the arrow half's left border, so the pair reads as a single control
        rather than two buttons that happen to touch.
        """
        sc = UiScale.px; t = theme_mgr.tokens
        r = sc(8)
        if corners == "left":
            radius = (f"border-top-left-radius: {r}px; "
                      f"border-bottom-left-radius: {r}px; "
                      f"border-top-right-radius: 0px; "
                      f"border-bottom-right-radius: 0px; border-right: none;")
        elif corners == "right":
            radius = (f"border-top-right-radius: {r}px; "
                      f"border-bottom-right-radius: {r}px; "
                      f"border-top-left-radius: 0px; "
                      f"border-bottom-left-radius: 0px;")
        else:
            radius = f"border-radius: {r}px;"

        if danger == "warning":
            return (
                f"QPushButton {{ background-color: {t.warning_bg}; "
                f"color: {t.warning_text}; border: 1px solid {t.warning_border}; "
                f"{radius} font-size: {sc(13)}px; "
                f"text-align: center; padding: 0 {sc(12)}px; font-weight: 600; }}"
                f"QPushButton:hover {{ background-color: {t.warning_bg_hover}; "
                f"border-color: {t.warning}; }}"
                f"QPushButton:disabled {{ color: {t.text_muted}; "
                f"background-color: {t.card_grad}; border-color: {t.border}; }}"
            )
        if danger:
            return (
                f"QPushButton {{ background-color: {t.danger_bg}; "
                f"color: {t.danger_text}; border: 1px solid {t.danger_border_btn}; "
                f"{radius} font-size: {sc(13)}px; "
                f"text-align: center; padding: 0 {sc(12)}px; font-weight: 600; }}"
                f"QPushButton:hover {{ background-color: {t.danger_bg_hover}; }}"
                f"QPushButton:disabled {{ color: {t.text_muted}; "
                f"background-color: {t.card_grad}; border-color: {t.border}; }}"
            )
        return (
            f"QPushButton {{ background-color: {t.card_grad}; "
            f"color: {t.text_primary}; border: 1px solid {t.border}; "
            f"{radius} font-size: {sc(13)}px; "
            f"text-align: center; padding: 0 {sc(12)}px; font-weight: 600; }}"
            f"QPushButton:hover {{ background-color: {t.bg_tile_hover}; "
            f"border-color: {t.accent}; }}"
            f"QPushButton:checked {{ background-color: {t.selected_grad}; "
            f"border-color: {t.accent}; }}"
            f"QPushButton:disabled {{ color: {t.text_muted}; }}"
        )

    # ── "Set Whole Media As Bg" split button + fit-mode dropdown ─────────
    # (key, label, hint). ``key`` is a CanvasWidget.BG_FIT_MODES value, so the
    # dropdown teaches the canvas and the save file nothing new — it just picks
    # which of the three existing layouts every generated page gets.
    # ONE layout setting, used by "Set As Background" and "Set Whole Media
    # As Bg" alike. It used to live inside the whole-media button's dropdown,
    # which meant the plain "Set As Background" had no way to say how the
    # picture should sit on the page and always used "auto".
    LAYOUT_MODES = (
        ("auto",    "Fill The Whole Canvas",
         "Covers the page edge to edge; the overflowing side is cropped."),
        ("fit",     "Fit The Canvas",
         "Shows the whole frame, centred, with bars on the short side."),
        ("stretch", "Stretch To Fit The Canvas",
         "Fills the page exactly, ignoring the frame's aspect ratio."),
    )

    def _build_whole_bg_row(self, layout, label: str) -> None:
        """Build the whole-media button.

        It used to be a split button (action | ▾) whose dropdown held the
        three layout choices. The dropdown is gone: those choices are now the
        Layout section built right below this row, because they were never
        specific to this action — "Set As Background" lays a picture on a page
        by exactly the same rules and had no way to say how. A setting that
        governs two buttons does not belong inside one of them.

        The dropdown MACHINERY is left in place and simply never built
        (``_whole_bg_menu`` stays None, and every method that touches it
        returns early on None). That keeps this change to the part that
        matters instead of unpicking the menu's positioning, its event filter
        and its close-on-scroll wiring at the same time.
        """
        sc = UiScale.px

        row = QWidget()
        row.setStyleSheet("background: transparent;")
        rl = QHBoxLayout(row)
        rl.setContentsMargins(0, 0, 0, 0)
        rl.setSpacing(0)

        btn = QPushButton(label)
        btn.setMinimumHeight(sc(42))
        btn.setCursor(Qt.PointingHandCursor)
        btn.setStyleSheet(self._action_qss(False))
        btn.clicked.connect(self._on_set_whole_bg)
        btn.setEnabled(False)
        btn.setToolTip("Give every slide/page its own board page, starting at "
                       "the page you are on.")
        # The button's own hint would otherwise pin the pane wider than the
        # splitter's minimum — same reasoning as _apply_info's eliding.
        btn.setSizePolicy(QSizePolicy.Ignored, QSizePolicy.Preferred)
        rl.addWidget(btn, 1)

        layout.addWidget(row)
        self._buttons.append(btn)
        self._whole_bg_btn = btn
        self._whole_bg_row = row

    def _build_layout_section(self, layout) -> None:
        """The Layout section — ONE setting, shared by both background actions.

        Sits directly under the two background buttons because that is what it
        governs, and reads as a list of three rather than a dropdown because
        there are only three and the current one should be visible without a
        click. Exactly one is ticked at any time.

        There is deliberately no second copy of this state anywhere: both
        ``_on_set_bg`` and ``_on_set_whole_bg`` read ``_layout_mode``, so the
        two actions cannot drift into laying the same picture out differently.
        """
        sc = UiScale.px; t = theme_mgr.tokens

        cap = QLabel("Layout")
        cap.setStyleSheet(
            f"color: {t.text_primary}; font-size: {sc(12)}px; "
            f"font-weight: 700; background: transparent;")
        layout.addSpacing(sc(4))
        layout.addWidget(cap)

        sub = QLabel("Used by both background actions above.")
        sub.setWordWrap(True)
        sub.setStyleSheet(
            f"color: {t.text_secondary}; font-size: {sc(11)}px; "
            f"background: transparent;")
        sub.setSizePolicy(QSizePolicy.Ignored, QSizePolicy.Preferred)
        layout.addWidget(sub)

        opt_qss = (
            f"QPushButton {{ background: transparent; color: {t.text_primary}; "
            f"border: 1px solid transparent; border-radius: {sc(8)}px; "
            f"text-align: left; padding: 0 {sc(8)}px; font-size: {sc(12)}px; "
            f"font-weight: 600; }}"
            f"QPushButton:hover {{ background-color: {t.bg_tile_hover}; }}"
            f"QPushButton:checked {{ background-color: {t.selected_grad}; "
            f"border-color: {t.accent}; }}"
        )
        for key, text, hint in self.LAYOUT_MODES:
            o = QPushButton(text)
            o.setCheckable(True)
            # Exclusivity is enforced in _apply_layout_mode so the tick icon
            # stays in step with the checked state — Qt's autoExclusive only
            # moves the check.
            o.setAutoExclusive(False)
            o.setMinimumHeight(sc(36))
            o.setCursor(Qt.PointingHandCursor)
            o.setStyleSheet(opt_qss)
            o.setIconSize(QSize(sc(14), sc(14)))
            o.setToolTip(hint)
            o.setSizePolicy(QSizePolicy.Ignored, QSizePolicy.Fixed)
            o.clicked.connect(lambda _c=False, k=key: self._on_layout_mode_picked(k))
            layout.addWidget(o)
            self._layout_opts[key] = o
        self._apply_layout_mode(self._layout_mode)

    # -- shared layout state ----------------------------------------------
    def _apply_layout_mode(self, mode: str) -> None:
        """Tick exactly one row, and keep the tick icon with it."""
        sc = UiScale.px; t = theme_mgr.tokens
        if mode not in self._layout_opts:
            mode = "fit"              # the default — see __init__
        self._layout_mode = mode
        tick  = _glyph_icon("check", sc(14), t.accent)
        blank = _glyph_icon("blank", sc(14), t.text_primary)
        for key, text, hint in self.LAYOUT_MODES:
            o = self._layout_opts.get(key)
            if o is None:
                continue
            on = (key == mode)
            o.setChecked(on)
            o.setIcon(tick if on else blank)

    def _on_layout_mode_picked(self, key: str) -> None:
        click_sound.play_click()
        self._apply_layout_mode(key)
        MediaActionsPanel._session_layout_mode = self._layout_mode

    @property
    def layout_mode(self) -> str:
        """The layout both background actions will use."""
        return self._layout_mode

    def _on_whole_bg_arrow(self) -> None:
        click_sound.play_click()
        if self._whole_bg_menu is not None and self._whole_bg_menu.isVisible():
            self._close_whole_bg_menu()
        else:
            self._open_whole_bg_menu()

    def _open_whole_bg_menu(self) -> None:
        if (self._whole_bg_menu is None or self._whole_bg_row is None
                or not self._whole_bg_row.isVisible()):
            return
        sc = UiScale.px; t = theme_mgr.tokens
        self._position_whole_bg_menu()
        self._whole_bg_menu.show()
        self._whole_bg_menu.raise_()
        if self._whole_bg_arrow is not None:
            self._whole_bg_arrow.setIcon(
                _glyph_icon("chevron_up", sc(14), t.text_primary))
        # Watch for a click anywhere else so the dropdown dismisses like a menu
        # instead of sitting open until the arrow is pressed again.
        app = QApplication.instance()
        if app is not None:
            app.installEventFilter(self)

    def _close_whole_bg_menu(self) -> None:
        if self._whole_bg_menu is None:
            return
        sc = UiScale.px; t = theme_mgr.tokens
        was_open = self._whole_bg_menu.isVisible()
        self._whole_bg_menu.hide()
        if self._whole_bg_arrow is not None:
            self._whole_bg_arrow.setIcon(
                _glyph_icon("chevron_down", sc(14), t.text_primary))
        if was_open:
            app = QApplication.instance()
            if app is not None:
                app.removeEventFilter(self)

    def _position_whole_bg_menu(self) -> None:
        """Park the dropdown under the split button, flipping above it when
        there isn't room — it is clipped to this panel, so an unflipped menu
        near the bottom would simply be cut off."""
        menu, row = self._whole_bg_menu, self._whole_bg_row
        if menu is None or row is None:
            return
        sc = UiScale.px
        origin = row.mapTo(self, row.rect().topLeft())
        # Clamped to the panel: the row lives in a scroll area whose content
        # can be wider than the visible pane, and a menu that hangs off the
        # right edge would be half-clipped.
        w = max(sc(160), min(row.width(), self.width() - origin.x() - sc(4)))
        menu.setFixedWidth(w)
        self._elide_whole_bg_opts(w)
        h = menu.sizeHint().height()
        below = origin.y() + row.height() + sc(6)
        if below + h > self.height() - sc(8):
            below = max(sc(8), origin.y() - h - sc(6))
        menu.setGeometry(origin.x(), below, w, h)

    def _elide_whole_bg_opts(self, width: int) -> None:
        """Elide the Layout rows to the pane width.

        They carry an Ignored size policy so a long mode name cannot widen the
        pane (see _apply_info for the trap that guards against), and the cost
        of that is a label clipped mid-word instead. Eliding puts the ellipsis
        where a reader expects it.

        Still named for the dropdown it was written for, because the callers
        are the panel's resize path and nothing about WHEN it runs changed —
        only which rows it has to measure now that the options are a section
        rather than a menu.
        """
        sc = UiScale.px
        avail = max(40, width - sc(50))     # margins + tick column + padding
        fulls = {k: text for k, text, _hint in self.LAYOUT_MODES}
        for key, o in self._layout_opts.items():
            full = fulls.get(key, "")
            if not full:
                continue
            fm = QFontMetrics(o.font())
            o.setText(fm.elidedText(full, Qt.ElideRight, avail))

    def eventFilter(self, obj, event):
        """Dismiss the dropdown on a click outside it (the arrow excepted — its
        own handler toggles, and closing here first would make it reopen)."""
        try:
            if (event.type() == QEvent.MouseButtonPress
                    and self._whole_bg_menu is not None
                    and self._whole_bg_menu.isVisible()):
                gp = event.globalPosition().toPoint()
                in_menu = self._whole_bg_menu.rect().contains(
                    self._whole_bg_menu.mapFromGlobal(gp))
                in_arrow = (self._whole_bg_arrow is not None
                            and self._whole_bg_arrow.rect().contains(
                                self._whole_bg_arrow.mapFromGlobal(gp)))
                if not in_menu and not in_arrow:
                    self._close_whole_bg_menu()
        except Exception:
            logger.debug("whole-bg menu event filter", exc_info=True)
        return super().eventFilter(obj, event)

    def event(self, e):
        # Dialog closed / panel hidden — don't leave a floating menu (and an
        # application-wide event filter) behind. HideToParent matters as much
        # as Hide: closing the dialog hides the DIALOG, and Qt sends the child
        # panel HideToParent rather than its own hideEvent, so watching only
        # for Hide would leak the filter for the life of the panel.
        if e.type() in (QEvent.Hide, QEvent.HideToParent):
            self._close_whole_bg_menu()
        return super().event(e)

    def set_item(self, item: Optional[MediaItem], selected_index: int = -1) -> None:
        self._item = item
        self._selected_index = selected_index

        # A different item (or none) is being shown — an open dropdown belongs
        # to the row that is about to change, so it goes away with it.
        self._close_whole_bg_menu()

        if item is None:
            self._info.setText("Pick a media item to see its options ✨")
            for b in self._buttons: b.setEnabled(False)
            self._show_whole_bg_row(False)
            self._render_btn.hide(); self._render_note.hide()
            return

        scope = ""
        if item.type == "image":
            scope = ""
        elif item.preview_count == 0:
            scope = "\n\n(not rendered yet)"
        elif selected_index >= 0:
            what = "Slide" if item.type == "ppt" else "Page"
            scope = f"\n\nScope: {what} {selected_index+1} of {item.preview_count}"
        else:
            scope = (f"\n\nScope: entire {item.type} "
                     f"({item.preview_count} "
                     f"{'slides' if item.type == 'ppt' else 'pages'})")

        # Keep the raw pieces — the name is elided to the CURRENT width and
        # has to be re-elided whenever the panel is resized.
        self._info_name = item.name
        self._info_tail = [
            f"Type: {item.type.upper()}",
            f"Size: {human_size(item.size_bytes)}",
        ]
        self._info_scope = scope
        self._apply_info()
        for b in self._buttons: b.setEnabled(True)

        # Nothing rendered: offer Render, and switch off what needs pages.
        # Remove and Delete stay — they work on the media itself.
        unrendered = item.type in ("pdf", "ppt") and item.preview_count == 0
        self._render_btn.setVisible(unrendered)
        if unrendered:
            no_ppt = getattr(item, "render_error", "") == "no_powerpoint"
            self._render_note.setText(
                "LogicBoard needs Microsoft PowerPoint to render presentations, "
                "and it isn't installed on this computer — so this one couldn't "
                "be rendered." if no_ppt else
                "This media hasn't been rendered. Render it here — no need to "
                "import it again.")
            self._render_note.show()
            for label in ("Add To Board", "Set As Background"):
                b = self._btn_by_label.get(label)
                if b is not None:
                    b.setEnabled(False)
        else:
            self._render_note.hide()

        # "Set Whole Media As Bg" only makes sense for multi-frame
        # media (a PPT deck or a multi-page PDF). Hide it entirely for
        # images and single-page docs so it doesn't imply a nonsensical
        # "spread one frame across many pages" action.
        self._show_whole_bg_row(item.type in ("ppt", "pdf")
                                and item.preview_count > 1)

    def _show_whole_bg_row(self, on: bool) -> None:
        """Show/hide the whole-media split button as one unit — button, arrow
        and caption together. Hiding only the button (what used to happen) left
        the arrow and the caption behind for single images."""
        for w in (self._whole_bg_row, self._whole_bg_hint):
            if w is not None:
                w.setVisible(on)
        for b in (self._whole_bg_btn, self._whole_bg_arrow):
            if b is not None:
                b.setEnabled(on)
        if not on:
            self._close_whole_bg_menu()

    def _apply_info(self) -> None:
        """Compose the info block, eliding the file name to the panel width.

        A QLabel with word-wrap still can't break a single unbroken token,
        and a file name has no spaces — so ``handout_for_class_11A_...pdf``
        set the label's minimum width to the full string, and the splitter
        obeys a child's minimum, so the Actions pane grew until it swallowed
        the whole dialog. Eliding removes the demand at source.
        """
        if not self._info_name:
            return
        avail = self._info.width()
        if avail <= 1:                     # before the first layout pass
            avail = self.width() - UiScale.px(32)
        if avail <= 1:
            avail = UiScale.px(240)
        fm = QFontMetrics(self._info.font())
        name = fm.elidedText(self._info_name, Qt.ElideRight, max(48, avail - 4))
        self._info.setText("\n".join([f"Name: {name}"] + self._info_tail)
                           + self._info_scope)
        # Full name still reachable on hover, since the label now hides it.
        self._info.setToolTip(self._info_name)

    def resizeEvent(self, event):
        """Re-elide on resize — the available width just changed."""
        super().resizeEvent(event)
        self._apply_info()
        # The Layout rows are elided to the pane, and the pane just changed
        # width. This used to be driven by the dropdown opening; the rows are
        # always on screen now, so the resize is what has to drive it.
        self._elide_whole_bg_opts(self.width())
        # The dropdown is positioned by hand, so it has to follow the splitter.
        if self._whole_bg_menu is not None and self._whole_bg_menu.isVisible():
            self._position_whole_bg_menu()

    def _on_set_bg(self):
        # The shared Layout choice, not a hard-coded "auto". This button used
        # to have no say in how the picture was laid on the page at all.
        self._emit_set_bg(self._layout_mode)

    def _emit_set_bg(self, mode: str):
        if self._item is None: return
        self._close_whole_bg_menu()
        click_sound.play_click()
        self.set_background_requested.emit(self._item.id, self._selected_index, mode)

    def _on_set_whole_bg(self):
        if self._item is None: return
        self._close_whole_bg_menu()
        click_sound.play_click()
        # The same shared Layout choice "Set As Background" uses, so the two
        # actions cannot lay the same picture out differently.
        self.set_whole_background_requested.emit(self._item.id,
                                                 self._layout_mode)

    def _on_render(self):
        if self._item is None: return
        click_sound.play_click()
        self.render_requested.emit(self._item.id)

    def _on_remove_from_board(self):
        if self._item is None: return
        click_sound.play_click()
        self.remove_from_board_requested.emit(self._item.id)

    def _on_reset_bg(self):
        self._close_whole_bg_menu()
        click_sound.play_click()
        self.reset_background_requested.emit()

    def set_background_info(self, title: str, detail: str = "",
                            has_bg: bool = False, anim: dict = None) -> None:
        """Update the Background Information section.

        title  — e.g. "PPT Slide 3", "Default Background".
        detail — optional source file name (second line).
        has_bg — whether a non-default background is currently set (enables
                 the reset button).
        anim   — for PPT-slide backgrounds with build-steps:
                 {item_id, slide, step, total_steps}. None hides the controls.
        """
        self._bg_title_label.setText(title or "Default Background")
        self._bg_detail_label.setText(detail or "")
        self._reset_bg_btn.setEnabled(bool(has_bg))

        self._bg_anim = anim
        if anim and anim.get("total_steps", 1) > 1:
            cur = anim.get("step", 0); total = anim.get("total_steps", 1)
            self._bg_anim_label.setText(f"Animation Step: {cur + 1} / {total}")
            self._bg_prev_btn.setEnabled(cur > 0)
            self._bg_next_btn.setEnabled(cur < total - 1)
            self._bg_anim_row.show()
        else:
            self._bg_anim_row.hide()

    def _on_bg_prev(self):
        if not self._bg_anim:
            return
        click_sound.play_click()
        self.background_step_requested.emit(
            self._bg_anim["item_id"], self._bg_anim["slide"],
            max(0, self._bg_anim["step"] - 1))

    def _on_bg_next(self):
        if not self._bg_anim:
            return
        click_sound.play_click()
        self.background_step_requested.emit(
            self._bg_anim["item_id"], self._bg_anim["slide"],
            self._bg_anim["step"] + 1)

    def _on_add(self):
        if self._item is None: return
        click_sound.play_click()
        self.add_object_requested.emit(self._item.id, self._selected_index)

    def _on_delete(self):
        if self._item is None: return
        click_sound.play_click()
        self.delete_requested.emit(self._item.id)


# ─────────────────────────────────────────────────────────────────────────────
# MediaManagerDialog — the 3-pane window
# ─────────────────────────────────────────────────────────────────────────────
class MediaManagerDialog(QDialog):
    """Three-pane Media Manager.

    Callbacks
    ---------
    on_browse_import    : called when user clicks 'Import Media'. Should open
                          a file dialog, import selected file(s) via the
                          library, and return the new item.id (or None).
    on_set_background   : (item_id, selected_index_or_minus_one, fit_mode)
    on_set_whole_background : (item_id, fit_mode)
    on_add_object       : (item_id, selected_index_or_minus_one)
    """

    def __init__(self, library: MediaLibrary, parent=None, *,
                 on_set_background: Optional[Callable[[str, int, str], None]] = None,
                 on_set_whole_background: Optional[Callable[[str, str], None]] = None,
                 on_add_object:     Optional[Callable[[str, int], None]] = None,
                 on_browse_import:  Optional[Callable[[], Optional[str]]] = None,
                 on_reset_background: Optional[Callable[[], None]] = None,
                 get_background_info: Optional[Callable[[], dict]] = None,
                 on_background_step: Optional[Callable[[str, int, int], None]] = None,
                 on_media_deleted: Optional[Callable[[str], None]] = None,
                 on_remove_from_board: Optional[Callable[[str], None]] = None,
                 on_render: Optional[Callable[[str], None]] = None):
        # Frameless modal dialog. No OS title bar → the user can't drag it
        # (you asked for this) and there's no chrome breaking the aero look.
        # A custom close button is added in the header instead. Still modal so
        # the canvas behind stays locked while Media is open.
        super().__init__(parent, Qt.Dialog | Qt.FramelessWindowHint)
        self.setModal(True)
        # MUST set WA_TranslucentBackground BEFORE the first show so that the
        # Win32 aero / pywinstyles mica backdrop has a transparent Qt
        # surface to render through.
        prepare_aero_widget(self)

        self._library = library
        self._on_set_background = on_set_background
        self._on_set_whole_background = on_set_whole_background
        self._on_add_object     = on_add_object
        self._on_browse_import  = on_browse_import
        self._on_reset_background = on_reset_background
        self._get_background_info = get_background_info
        self._on_background_step = on_background_step
        self._on_media_deleted   = on_media_deleted
        self._on_remove_from_board = on_remove_from_board
        self._on_render = on_render
        self._selected_id: Optional[str] = None
        self._cards: list[MediaCard] = []

        sc = UiScale.px; t = theme_mgr.tokens

        self.setWindowTitle("Media")
        # Size relative to the screen instead of hardcoded pixels: ~64% of the
        # available width and ~72% of the height, clamped to sane bounds, so it
        # adapts from small laptops to 4K displays.
        from PySide6.QtGui import QGuiApplication
        screen = QGuiApplication.primaryScreen()
        avail = screen.availableGeometry() if screen else None
        if avail is not None:
            w = int(avail.width()  * 0.64)
            h = int(avail.height() * 0.72)
            self.resize(w, h)
            self.setMinimumSize(int(avail.width() * 0.42),
                                int(avail.height() * 0.46))
        else:
            self.resize(sc(1100), sc(680))
            self.setMinimumSize(sc(800), sc(480))
        # Fully transparent surface so the aero blur shows through the whole
        # dialog (the tiles/cards provide their own translucent fills).
        self.setStyleSheet("QDialog { background: transparent; }")

        root = QVBoxLayout(self)
        root.setContentsMargins(sc(14), sc(14), sc(14), sc(14))
        root.setSpacing(sc(10))

        # ── Header (icon + title + Import button) ──────────────────
        header = QHBoxLayout()
        header.setSpacing(sc(9))
        icon = QLabel()
        icon.setFixedSize(sc(26), sc(26))
        icon.setPixmap(tool_icon("media", sc(26)).pixmap(sc(26), sc(26)))
        icon.setStyleSheet("background: transparent; border: none;")
        header.addWidget(icon, 0, Qt.AlignVCenter)
        title = QLabel("Media")
        title.setStyleSheet(
            f"color: {t.text_primary}; font-size: {sc(22)}px; "
            f"font-weight: 700; background: transparent;"
        )
        header.addWidget(title)
        header.addStretch()

        self._import_btn = QPushButton("Import Media")
        self._import_btn.setMinimumHeight(sc(38))
        self._import_btn.setMinimumWidth(sc(140))
        self._import_btn.setCursor(Qt.PointingHandCursor)
        self._import_btn.setStyleSheet(f"""
            QPushButton {{
                background-color: {t.accent_grad}; color: {t.text_on_accent};
                border: none; border-radius: {sc(8)}px;
                font-size: {sc(13)}px; font-weight: 700;
                padding: 0 {sc(18)}px;
            }}
            QPushButton:hover {{ background-color: {t.accent_grad_hover}; }}
        """)
        self._import_btn.clicked.connect(self._on_import_clicked)
        header.addWidget(self._import_btn)

        # Custom close button (replaces the removed OS title-bar close).
        self._close_btn = QPushButton("✕")
        self._close_btn.setFixedSize(sc(38), sc(38))
        self._close_btn.setCursor(Qt.PointingHandCursor)
        self._close_btn.setToolTip("Close")
        self._close_btn.setStyleSheet(f"""
            QPushButton {{
                background-color: {t.card_grad}; color: {t.text_primary};
                border: 1px solid {t.border}; border-radius: {sc(8)}px;
                font-size: {sc(15)}px; font-weight: 700;
            }}
            QPushButton:hover {{
                background-color: {t.danger}; color: white;
                border-color: {t.danger};
            }}
        """)
        self._close_btn.clicked.connect(self.close)
        header.addWidget(self._close_btn)
        root.addLayout(header)

        # ── 3-pane splitter ───────────────────────────────────────
        splitter = QSplitter(Qt.Horizontal)
        splitter.setHandleWidth(sc(6))
        splitter.setStyleSheet(f"""
            QSplitter::handle {{
                background: transparent;
            }}
            QSplitter::handle:hover {{
                background: {t.border};
                border-radius: {sc(2)}px;
            }}
        """)

        # LEFT: scrollable library card list
        self._library_scroll = QScrollArea()
        self._library_scroll.setWidgetResizable(True)
        self._library_scroll.setHorizontalScrollBarPolicy(Qt.ScrollBarAlwaysOff)
        setup_scroll(self._library_scroll)   # modern scrollbar + touch flick
        self._library_content = QWidget()
        self._library_content.setStyleSheet("background: transparent;")
        self._library_layout = QVBoxLayout(self._library_content)
        self._library_layout.setContentsMargins(sc(4), sc(4), sc(4), sc(4))
        self._library_layout.setSpacing(sc(8))
        self._library_layout.addStretch()
        self._library_scroll.setWidget(self._library_content)
        splitter.addWidget(self._library_scroll)

        # MIDDLE: preview
        self._preview = MediaPreviewView(library)
        self._preview.page_selected.connect(self._on_page_selected)
        splitter.addWidget(self._preview)

        # RIGHT: actions
        self._actions = MediaActionsPanel()
        self._actions.set_background_requested.connect(self._on_set_bg)
        self._actions.set_whole_background_requested.connect(self._on_set_whole_bg)
        self._actions.add_object_requested.connect(self._on_add_obj)
        self._actions.remove_from_board_requested.connect(
            self._on_remove_from_board_requested)
        self._actions.delete_requested.connect(self._on_delete_media)
        self._actions.render_requested.connect(self._on_render_requested)
        self._actions.reset_background_requested.connect(self._on_reset_bg)
        self._actions.background_step_requested.connect(self._on_bg_step)
        splitter.addWidget(self._actions)

        splitter.setSizes([sc(320), sc(520), sc(280)])
        splitter.setStretchFactor(0, 0)
        splitter.setStretchFactor(1, 1)
        splitter.setStretchFactor(2, 0)
        root.addWidget(splitter, 1)

        self._reload_library()

    # ── show: Aero + fade ──────────────────────────────────────
    def showEvent(self, event):
        super().showEvent(event)
        # Frameless dialog can't be dragged, so center it on the screen the
        # first time it's shown.
        if not getattr(self, "_centered_once", False):
            from PySide6.QtGui import QGuiApplication
            screen = QGuiApplication.primaryScreen()
            if screen is not None:
                geo = screen.availableGeometry()
                fg = self.frameGeometry()
                fg.moveCenter(geo.center())
                self.move(fg.topLeft())
            self._centered_once = True
        # prepare_aero_widget was already done in __init__; here we just
        # trigger the DWM / pywinstyles backdrop now that the HWND exists.
        apply_aero(self)
        enable_shadow(self)
        fade_in(self)
        # Reflect the page's current background in the info section every time
        # the dialog is shown (covers page changes that happened while closed).
        self._refresh_bg_info()

    # ── library list management ───────────────────────────────────
    def _reload_library(self) -> None:
        # Clear existing cards
        while self._library_layout.count():
            it = self._library_layout.takeAt(0)
            w = it.widget()
            if w: w.deleteLater()
        self._cards = []

        items = self._library.items()
        if not items:
            sc = UiScale.px; t = theme_mgr.tokens
            empty = QLabel(
                "Nothing here yet! 📚\n\n"
                "Tap 'Import Media' to bring in images, PDFs, or PowerPoint "
                "slides and let's make this board awesome. 😎"
            )
            empty.setAlignment(Qt.AlignCenter)
            empty.setWordWrap(True)
            empty.setStyleSheet(
                f"color: {t.text_secondary}; font-size: {sc(12)}px; "
                f"padding: {sc(40)}px; background: transparent;"
            )
            self._library_layout.addWidget(empty)
            self._library_layout.addStretch()
            self._preview.set_item(None)
            self._actions.set_item(None)
            return

        for item in items:
            card = MediaCard(item, selected=(item.id == self._selected_id),
                              library=self._library)
            card.card_clicked.connect(self._on_card_clicked)
            self._library_layout.addWidget(card)
            self._cards.append(card)
        self._library_layout.addStretch()

        # Restore preview / actions for the previously selected item if any
        if self._selected_id:
            sel = self._library.get(self._selected_id)
            self._preview.set_item(sel)
            self._actions.set_item(sel, self._preview.selected_index)

    def _on_card_clicked(self, item_id: str) -> None:
        self._selected_id = item_id
        for c in self._cards:
            c.setSelected(c.item_id == item_id)
        item = self._library.get(item_id)
        self._preview.set_item(item)
        self._actions.set_item(item, -1)

    def _on_page_selected(self, index: int) -> None:
        if self._selected_id:
            item = self._library.get(self._selected_id)
            self._actions.set_item(item, index)

    # ── action callbacks ──────────────────────────────────────────
    def _on_import_clicked(self) -> None:
        click_sound.play_click()
        if self._on_browse_import is None:
            return
        try:
            new_id = self._on_browse_import()
        except Exception as e:
            logger.exception("Import callback failed: %s", e)
            new_id = None
        if new_id:
            self._selected_id = new_id
        self._reload_library()

    def _on_set_bg(self, item_id: str, idx: int, mode: str = "auto") -> None:
        if self._on_set_background:
            self._on_set_background(item_id, idx, mode)
        self.close()

    def _on_set_whole_bg(self, item_id: str, mode: str = "auto") -> None:
        """Turn the whole presentation into pages (one page per slide/page,
        each with that frame as its background, laid out per ``mode``). Closes
        the dialog so the user lands on the freshly-generated first page."""
        if self._on_set_whole_background:
            self._on_set_whole_background(item_id, mode)
        self.close()

    def _on_reset_bg(self) -> None:
        """Reset the page to its default background. Stays open and refreshes
        the Background Information section immediately."""
        if self._on_reset_background:
            try:
                self._on_reset_background()
            except Exception as e:
                logger.exception("Reset background failed: %s", e)
        self._refresh_bg_info()

    def _on_bg_step(self, item_id: str, slide: int, step: int) -> None:
        if self._on_background_step:
            self._on_background_step(item_id, slide, step)
        # Re-pull the (now-updated) background description so the counter and
        # button states reflect the new step.
        self._refresh_bg_info()

    def _refresh_bg_info(self) -> None:
        """Pull the current page background description from the host and push
        it into the Actions panel's Background Information section."""
        if self._get_background_info is None:
            return
        try:
            info = self._get_background_info() or {}
        except Exception as e:
            logger.exception("get_background_info failed: %s", e)
            info = {}
        self._actions.set_background_info(
            info.get("title", "Default Background"),
            info.get("detail", ""),
            bool(info.get("has_bg", False)),
            info.get("anim"),
        )

    def _on_add_obj(self, item_id: str, idx: int) -> None:
        if self._on_add_object:
            self._on_add_object(item_id, idx)
        self.close()

    def _on_render_requested(self, item_id: str) -> None:
        """Render unrendered media in place, then show the result."""
        if self._on_render is None:
            return
        try:
            self._on_render(item_id)
        except Exception as e:
            logger.exception("Render failed: %s", e)
        self._selected_id = item_id
        self._reload_library()

    def _on_remove_from_board_requested(self, item_id: str) -> None:
        """Take this media off the board, keeping the media itself.

        The confirmation is the point of this handler. "Remove" and "Delete"
        sit next to each other and a user is entitled to be unsure which one
        keeps their file, so the dialog says outright what survives — the
        library entry, the original file and the cached renders — rather than
        only what goes.
        """
        item = self._library.get(item_id)
        if item is None:
            return
        name = item.name

        # Same counter the delete flow uses, so the warning names what will
        # actually change instead of describing the operation in the abstract.
        obj_count = bg_count = 0
        host = self.parent()
        if host is not None and hasattr(host, "_count_media_usages"):
            try:
                obj_count, bg_count = host._count_media_usages(item)
            except Exception:
                logging.exception("_count_media_usages failed for %s", name)
                obj_count = bg_count = 0

        if obj_count == 0 and bg_count == 0:
            AeroAlert.information(
                self, "Not on the board",
                f"'{name}' isn't placed on any page, so there is nothing to "
                f"remove.\n\nIt stays in the Media Manager, ready to use.")
            return

        parts = []
        if obj_count > 0:
            noun = "place" if obj_count == 1 else "places"
            parts.append(f"• Removed from {obj_count} {noun} on the canvas")
        if bg_count > 0:
            noun = "page" if bg_count == 1 else "pages"
            parts.append(f"• Cleared from the background of {bg_count} {noun}")
        details = "\n".join(parts)

        ans = AeroAlert.question(
            self, "Remove media from board",
            f"Remove '{name}' from this board?\n\n"
            f"{details}\n\n"
            f"THE MEDIA ITSELF IS NOT DELETED. It stays in the Media Manager, "
            f"the original file stays on disk, and nothing is removed from the "
            f"cache — so you can place it again at any time.",
            AeroAlert.Yes | AeroAlert.No, AeroAlert.No
        )
        if ans != AeroAlert.Yes:
            return

        if self._on_remove_from_board is not None:
            try:
                self._on_remove_from_board(item_id)
            except Exception:
                logging.exception("on_remove_from_board callback failed")
        # The library is untouched, so there is no reload to do — but the
        # page background may have just been cleared, and that section is on
        # screen right now.
        self._refresh_bg_info()

    def _on_delete_media(self, item_id: str) -> None:
        # Confirm — delete is permanent. The warning is now honest about
        # what will happen: any placed copies AND any page backgrounds
        # that point at this item are scrubbed across the whole board.
        item = self._library.get(item_id)
        if item is None:
            return
        name = item.name

        # Ask the host (MainWindow) to count usages across every page.
        # Falls back to 0/0 if the host doesn't expose the helper (older
        # MainWindow versions) — in that case the warning is a generic
        # "may break" string, same as the legacy behaviour.
        #
        # IMPORTANT: the count is ONLY used to build the confirmation
        # message. It does NOT gate whether the purge runs — see the
        # extended comment on _purge_media_references below for why.
        obj_count = 0
        bg_count = 0
        host = self.parent()
        if host is not None and hasattr(host, "_count_media_usages"):
            try:
                obj_count, bg_count = host._count_media_usages(item)
            except Exception:
                # Log this — silently swallowing was the bug that let
                # stale references linger on pages. We keep the counts at
                # 0 so the warning message is generic, but the purge below
                # will still walk every page and remove anything matching.
                logging.exception("_count_media_usages failed for %s", name)
                obj_count, bg_count = 0, 0

        if obj_count == 0 and bg_count == 0:
            body = (f"Permanently delete '{name}' from this board?\n\n"
                    "It isn't currently placed on any page, so nothing "
                    "else will change.")
        else:
            # Build a clear, specific summary of what will disappear.
            parts = []
            if obj_count > 0:
                noun = "place" if obj_count == 1 else "places"
                parts.append(f"• Removed from {obj_count} {noun} on the canvas")
            if bg_count > 0:
                noun = "page" if bg_count == 1 else "pages"
                parts.append(f"• Cleared from the background of {bg_count} {noun}")
            details = "\n".join(parts)
            body = (
                f"Permanently delete '{name}'?\n\n"
                f"This media is used on this board. Deleting it will:\n"
                f"{details}\n\n"
                f"This cannot be undone from the Media Manager. "
                f"(Use the Undo button on the canvas to restore individual "
                f"placements after deletion if needed.)"
            )

        ans = AeroAlert.question(
            self, "Delete media",
            body,
            AeroAlert.Yes | AeroAlert.No, AeroAlert.No
        )
        if ans != AeroAlert.Yes:
            return

        # ─── ALWAYS PURGE ───────────────────────────────────────────────────
        # BUG FIX: the previous code gated the purge on
        #   ``obj_count > 0 or bg_count > 0``
        # which was WRONG because _count_media_usages is wrapped in a
        # try/except that resets both to 0 on any error. So if the count
        # method failed silently (a bad relpath encoding, a stale page
        # reference, a race with an autosave — anything), the counts
        # would say "0 usages", the purge would be SKIPPED, and the media
        # would be deleted from the library while all its objects +
        # backgrounds continued to reference the now-vanished cache.
        #
        # Fix: always run the purge if the helper is available. It's O(n)
        # over pages+objects (fast), idempotent (finds and removes what
        # matches, ignores everything else), and safe (each removal goes
        # through the page's undo stack). No downside to running it when
        # there's nothing to purge — it just returns (0, 0).
        if host is not None and hasattr(host, "_purge_media_references"):
            try:
                purged = host._purge_media_references(item)
                logging.info(
                    "Purge %s: %d objects removed, %d backgrounds cleared",
                    name, purged[0] if purged else 0,
                    purged[1] if purged else 0,
                )
            except Exception:
                logging.exception("_purge_media_references failed")

        self._library.delete(item_id)

        # Tell the host the board's pages may look different now. The purge
        # above rips this media out of backgrounds and objects across every
        # page, and the page-preview thumbnails are cached per page — without
        # this event they keep showing the deleted media until something else
        # happens to re-render them.
        if self._on_media_deleted is not None:
            try:
                self._on_media_deleted(item_id)
            except Exception:
                logging.exception("on_media_deleted callback failed")

        # PREVIEW BUG FIX: if the item we just deleted was the selected
        # one, clear the preview + actions pane EXPLICITLY. The old code
        # cleared `_selected_id` but never called `_preview.set_item(None)`,
        # so the middle pane kept rendering the deleted item's thumbnail /
        # slide grid until the user clicked on something else. Same issue
        # for the right-hand actions panel — buttons for a nonexistent
        # item stayed live and could crash if clicked. Clear both here so
        # the panel state stays consistent with reality regardless of
        # what _reload_library does next.
        if self._selected_id == item_id:
            self._selected_id = None
            try:
                self._preview.set_item(None)
            except Exception:
                logging.exception("preview.set_item(None) failed")
            try:
                self._actions.set_item(None)
            except Exception:
                logging.exception("actions.set_item(None) failed")
        self._reload_library()