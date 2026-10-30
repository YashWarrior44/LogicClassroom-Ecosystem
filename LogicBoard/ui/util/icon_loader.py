"""
icon_loader.py — Unified icon resolver for LogicBoard.

Resolution order for every icon name:
  1. ``assets/icons/<name>.svg``  — project SVGs (gradient-coloured, sharp at any DPI)
  2. Segoe Fluent Icons / MDL2 Assets font (Windows 10/11 only)
  3. Emoji fallback (all platforms)

SVG files use ``currentColor`` for theme-aware strokes/fills *and/or* hardcoded
vibrant gradients for decorative icons. The caller can pass ``color`` to override
``currentColor`` (used for thin-stroke icon variants that want to match the UI
text colour).
"""
from __future__ import annotations

import logging
from pathlib import Path
from typing import Optional

from PySide6.QtCore  import QByteArray, QRectF, QSize, Qt
from PySide6.QtGui   import (QColor, QFont, QGuiApplication, QIcon, QPainter,
                             QPixmap)

try:
    from PySide6.QtSvg import QSvgRenderer
    _HAS_SVG = True
except ImportError:
    _HAS_SVG = False

__all__ = ["tool_icon", "ICONS_DIR", "icon_target_dpr", "blank_icon_pixmap",
           "app_logo_pixmap"]

logger = logging.getLogger(__name__)

# ── paths ─────────────────────────────────────────────────────────────────
# icon_loader.py lives at  ui/util/icon_loader.py
# assets/icons/           lives at  <repo_root>/assets/icons/
ICONS_DIR: Path = Path(__file__).resolve().parents[2] / "assets" / "icons"

# Sanity ceiling for the device-pixel ratio. Nothing real exceeds 4x, and a
# bogus value would otherwise allocate an enormous pixmap per icon.
_MAX_DPR = 4.0


# ── Hi-DPI ────────────────────────────────────────────────────────────────
def _target_dpr() -> float:
    """Physical pixels per logical pixel to rasterise icons at.

    THE fix for blurry toolbar icons. Sizes here arrive in LOGICAL pixels —
    ``size_manager`` returns resolution-scaled logical units and leaves the
    DPI factor to Qt, exactly as its module docstring says. So a pixmap
    allocated at ``size × size`` with no dpr is a bitmap of logical extent
    being stretched to physical extent at paint time: at 125 % Windows
    scaling every icon is blown up 28 → 35 px, and at a 4K board's 150 % it
    is 28 → 42. That upscale IS the blur, and it hits nothing else in the
    app because every other icon surface already rasterises at the device
    ratio (see selection_menu, media_manager, pen_panel).

    The MAXIMUM across screens, not the primary one: a teacher's laptop
    driving a board is two screens at two different ratios, and an icon
    built for the laptop is the one that goes soft when the window moves to
    the board. Over-rendering costs a few KB and downscales cleanly;
    under-rendering is the defect being fixed.
    """
    try:
        app = QGuiApplication.instance()
        if app is None:
            return 1.0
        ratios = [float(s.devicePixelRatio()) for s in app.screens()]
        return max(1.0, min(_MAX_DPR, max(ratios))) if ratios else 1.0
    except Exception:
        return 1.0


def _blank_pixmap(size: int, dpr: float) -> QPixmap:
    """Transparent pixmap of ``size`` LOGICAL px, backed at ``dpr``."""
    phys = max(1, int(round(size * dpr)))
    pm = QPixmap(phys, phys)
    pm.setDevicePixelRatio(dpr)
    pm.fill(Qt.transparent)
    return pm


# Public aliases — the toolbar rasterises its own glyph icons and must use
# the same ratio and the same pixmap backing, or it reintroduces the blur one
# module over.
icon_target_dpr = _target_dpr
blank_icon_pixmap = _blank_pixmap


# ── the app logo ─────────────────────────────────────────────────────────────

_logo_cache: dict = {}


def _edge_margin(pm) -> int:
    """Narrowest transparent border on the left or right of ``pm``, in px.

    Used to decide whether the logo needs insetting at a given size. Cheap
    enough because ``app_logo_pixmap`` caches its result, so this runs once
    per (size, ratio) the app actually asks for — a handful over a session.

    Scans columns rather than every pixel: the question is only how far in
    the first ink is from each side.
    """
    try:
        img = pm.toImage()
        w, h = img.width(), img.height()
        if w <= 0 or h <= 0:
            return 0

        def first_inked_column(columns):
            for x in columns:
                for y in range(h):
                    if img.pixelColor(x, y).alpha() > 12:
                        return x
            return w          # nothing at all — treat as fully margined

        left = first_inked_column(range(w))
        right = w - 1 - first_inked_column(range(w - 1, -1, -1))
        return min(left, right)
    except Exception:
        return 1              # unknown: assume it is fine, change nothing


def app_logo_pixmap(size: int, dpr: Optional[float] = None):
    """The LogicBoard logo at ``size`` LOGICAL pixels. Cached. Never raises.

    WHY QIcon AND NOT QPixmap
    =========================
    ``logo.ico`` is a multi-resolution icon — ten frames, 16x16 up to
    256x256. ``QPixmap(path)`` on a file like that does not pick the frame
    nearest what you want, or even the largest: it loads the FIRST directory
    entry, which here is the **16x16**. Every caller then scaled that
    16-pixel image UP to whatever it needed — 3.5x for a 56-pixel file card.

    That upscale is the pixelated logo: on the .lb cards in the file panel,
    in About, in Settings, and in the setup wizard. All four had already been
    "fixed" for device pixel ratio, correctly, which is why it was easy to
    miss — the ratio was never the problem, the source frame was.

    ``QIcon`` reads the whole directory and picks the closest frame at or
    above the requested size, so asking for 56 gets the 64 (or the 256
    master) and downscales it, with pixels to spare at every scale factor.

    Returns ``None`` when there is no icon to load, so callers can fall back
    to text or an emoji rather than showing an empty box.
    """
    if dpr is None:
        dpr = _target_dpr()
    dpr = max(1.0, float(dpr))
    key = (int(size), round(dpr, 3))
    cached = _logo_cache.get(key)
    if cached is not None:
        return cached
    try:
        from ui.util.paths import find_app_icon_path
        icon_path = find_app_icon_path()
        if not icon_path:
            return None
        phys = max(1, int(round(size * dpr)))
        icon = QIcon(str(icon_path))

        # ── Where the pixels come from ───────────────────────────────────
        #
        # logo.ico holds ten authored frames: 16, 20, 24, 32, 40, 48, 64,
        # 96, 128, 256. Two cases, and they want opposite things.
        #
        # EXACT FRAME — take it untouched. Those frames are authored, not
        # derived, and nothing beats them at their own size. Measured:
        # scaling the 256 master down to 24/32/40/48 instead was 17-21%
        # SOFTER.
        #
        # ANY OTHER SIZE — scale the 256 master down, once, smoothly. And
        # this is the case that actually happens: UiScale multiplies every
        # dimension by the display's factor, so a card asking for sc(42)
        # gets 36, and 36 is not a frame. ``QIcon.pixmap(36)`` takes the 40
        # and resamples it, which is a 10% reduction from an already-small
        # image. Measured against the master downscale at the sizes this
        # app actually requests — 27, 31, 36, 39, 44 — QIcon's own path was
        # 12-20% softer at every one of them. That was the fuzzy logo.
        #
        # Note what is NOT done here: supersampling by handing back more
        # pixels than asked for and letting devicePixelRatio carry the
        # difference. QLabel.setPixmap resamples to the LABEL's ratio and
        # discards the pixmap's own — measured, a 40x40 at dpr 1.111 comes
        # back out of the label as a 36x36 at dpr 1.0 — so on a 1x screen
        # that just reintroduces the resample it was meant to avoid.
        # ── The best available art at this exact size ────────────────────
        frames = sorted(f.width() for f in icon.availableSizes())

        # QIcon.pixmap() MULTIPLIES the requested size by the application's
        # device-pixel ratio and hands back a pixmap tagged with it. So
        # ``icon.pixmap(QSize(64, 64))`` on a 125 % display is 80x80 real
        # pixels that reports a logical extent of 64 — measured, exactly
        # that.
        #
        # Everything below this line works in REAL pixels: ``phys`` is a
        # real-pixel count and the canvas is allocated at it. Mixing the
        # two units is what shaved the logo: the centring offset was
        # ``(64 - art.width()) // 2`` = ``(64 - 80) // 2`` = -8, while
        # drawPixmap placed the art at its LOGICAL 64 — so the artwork sat
        # 8 px up and left of the canvas with its top-left corner cut off
        # and 9 px of dead space down the right. Invisible at 100 %
        # scaling, which is why it shipped.
        #
        # So ask for the logical size that lands on ``target`` real pixels,
        # then drop the ratio tag. From there ``.width()`` IS the drawn
        # width and the arithmetic is in one unit.
        qt_dpr = 1.0
        try:
            _app = QGuiApplication.instance()
            if _app is not None:
                qt_dpr = max(1.0, float(_app.devicePixelRatio()))
        except Exception:
            qt_dpr = 1.0

        def _raw(target: int):
            """``target`` REAL pixels of the best frame, untagged."""
            req = max(1, int(round(target / qt_dpr)))
            out = icon.pixmap(QSize(req, req))
            if not out.isNull():
                out.setDevicePixelRatio(1.0)
            return out

        def _art(target):
            """The sharpest ``target``-pixel rendition available.

            An exact frame is authored rather than derived and nothing
            beats it at its own size — measured, scaling the 256 master
            down to 24/32/40/48 instead was 17-21% softer.

            Every other size takes the master, scaled once, smoothly. That
            is the case that actually happens: UiScale multiplies every
            dimension by the display factor, so a card asking for sc(42)
            gets 36, and 36 is not a frame. ``QIcon.pixmap(36)`` resamples
            the 40 — a 10% reduction of an already-small image — and
            measured 12-20% softer than the master downscale at every size
            this app requests (27, 31, 36, 39, 44). That was the fuzzy
            logo.
            """
            if target in frames:
                exact = _raw(target)
                if not exact.isNull() and exact.width() == target:
                    return exact
            master = frames[-1] if frames else target
            src = _raw(master)
            if src.isNull():
                return src
            # KeepAspectRatio because the artwork is NOT square — 244 x 208
            # of ink inside the 256 frame — and stretching it to fill a
            # square is its own kind of wrong.
            return src.scaled(target, target, Qt.KeepAspectRatio,
                              Qt.SmoothTransformation)

        art = _art(phys)
        if art.isNull():
            return None
        # Rounding in ``_raw`` can land a pixel over. A canvas is not a
        # crop: anything wider than ``phys`` would lose that pixel off an
        # edge rather than shrink, so bring it down instead.
        if art.width() > phys or art.height() > phys:
            art = art.scaled(phys, phys, Qt.KeepAspectRatio,
                             Qt.SmoothTransformation)
            art.setDevicePixelRatio(1.0)

        # ── A margin, but only where one is actually missing ─────────────
        #
        # The logo's own margin is about 2% of the frame: six pixels at
        # 256, and rounding to ZERO by the time the frame is 32. Measured,
        # the ink runs x 0..N-1 at 16, 24 and 32 — flush to both edges — so
        # the antialiased border lands on the boundary pixel and the left
        # side reads as shaved off. That is the reported clipping.
        #
        # Insetting is not free: drawing the art at 94% costs about 6% of
        # edge sharpness, and applying it everywhere made the authored
        # frames 13-18% SOFTER than doing nothing at all.
        #
        # So it is applied only to sizes that are not authored frames.
        # Those are derived either way, so there is nothing to lose and a
        # margin to gain — measured +0.7% to +15.4% against the old path at
        # 27, 36, 39 and 44, all of them with a pixel of margin they did
        # not have. An exact frame is what somebody DREW for that size, and
        # second-guessing it is both presumptuous and, at 16 or 24 px,
        # visibly worse: a pixel of margin there is 6% of the whole icon.
        if phys not in frames and _edge_margin(art) < 1:
            inner = max(1, int(round(phys * 0.94)))
            inset = _art(inner)
            if not inset.isNull():
                if inset.width() > inner or inset.height() > inner:
                    inset = inset.scaled(inner, inner, Qt.KeepAspectRatio,
                                         Qt.SmoothTransformation)
                    inset.setDevicePixelRatio(1.0)
                art = inset

        pm = QPixmap(phys, phys)
        pm.fill(Qt.transparent)
        painter = QPainter(pm)
        painter.setRenderHint(QPainter.Antialiasing, True)
        painter.setRenderHint(QPainter.SmoothPixmapTransform, True)
        painter.drawPixmap((phys - art.width()) // 2,
                           (phys - art.height()) // 2, art)
        painter.end()

        # Tag the ratio so the label lays it out at ``size`` logical pixels
        # instead of drawing ``phys`` of them and overflowing its box.
        pm.setDevicePixelRatio(dpr)
        _logo_cache[key] = pm
        return pm
    except Exception:
        # Corrupt file, permissions, a fresh checkout with no assets — a
        # plainer card beats a crash while rendering one.
        return None


# ── SVG renderer ─────────────────────────────────────────────────────────
def _render_svg(svg_path: Path, size: int,
                color: Optional[str] = None) -> Optional[QPixmap]:
    """Read SVG file, optionally substitute ``currentColor`` ➜ ``color``,
    render to a QPixmap ``size × size`` LOGICAL px, rasterised at the
    display's device-pixel ratio so it stays sharp on a Hi-DPI board.

    QtSvg has NO support for SVG <filter> effects (feDropShadow, blur, …); any
    element carrying filter="url(#…)" renders as a BLACK BOX. We strip those
    filter references and the now-unused <filter> defs in-memory before
    rendering, so gradients/paths still draw. The file on disk is untouched.
    """
    if not _HAS_SVG or not svg_path.exists():
        return None
    try:
        import re
        text = svg_path.read_text(encoding="utf-8")
        if color:
            text = text.replace("currentColor", color)
        # Remove filter usages + <filter> defs that QtSvg can't render.
        text = re.sub(r'\s*filter\s*=\s*"url\(#[^"]*\)"', '', text)
        text = re.sub(r'<filter\b.*?</filter>', '', text, flags=re.DOTALL)
        text = re.sub(r'\s*<feDropShadow\b[^>]*/?>', '', text)
        renderer = QSvgRenderer(QByteArray(text.encode("utf-8")))
        if not renderer.isValid():
            return None
        dpr = _target_dpr()
        pm = _blank_pixmap(size, dpr)
        painter = QPainter(pm)
        painter.setRenderHint(QPainter.Antialiasing, True)
        # An explicit LOGICAL rect, not the default viewport. A QPainter on a
        # dpr-tagged pixmap already scales coordinates by dpr, so letting the
        # renderer default to the device-sized viewport scales it twice and
        # the icon overflows its box. Same reasoning as the note in
        # selection_menu._render_svg_icon.
        renderer.render(painter, QRectF(0, 0, size, size))
        painter.end()
        return pm
    except Exception as e:
        logger.debug("SVG render failed (%s): %s", svg_path.name, e)
        return None


# ── text / glyph renderer ─────────────────────────────────────────────────
def _render_char(char: str, size: int, color: str,
                 font_family: str = "") -> QPixmap:
    """Render a single glyph / emoji to a transparent QPixmap, rasterised at
    the display's device-pixel ratio."""
    dpr = _target_dpr()
    pm = _blank_pixmap(size, dpr)
    painter = QPainter(pm)
    painter.setRenderHint(QPainter.Antialiasing, True)
    painter.setRenderHint(QPainter.TextAntialiasing, True)
    font = QFont()
    if font_family:
        font.setFamily(font_family)
    font.setPixelSize(max(8, int(size * 0.64)))
    painter.setFont(font)
    painter.setPen(QColor(color))
    # LOGICAL rect. ``pm.rect()`` is in DEVICE pixels, and the painter is
    # already scaling by dpr — centring inside it would push the glyph off
    # towards the bottom-right by a factor of dpr.
    painter.drawText(QRectF(0, 0, size, size), Qt.AlignCenter, char)
    painter.end()
    return pm


# ── public API ────────────────────────────────────────────────────────────
def tool_icon(name: str, size: int = 32,
              color: str | None = None) -> QIcon:
    """Return a QIcon for ``name`` at ``size × size`` pixels.

    ``name`` must be an icon identifier such as ``"pen"``, ``"eraser"``,
    ``"rect"``… (matches the SVG filename without extension).

    ``color`` is substituted for ``currentColor`` in the SVG and also used
    as the glyph colour in the text-fallback path. Leave it None to get the
    theme's ``icon_default``, which is what keeps a toolbar glyph the same
    colour as the caption printed under it.
    """
    if color is None:
        from ui.theme.theme_manager import theme_mgr
        color = theme_mgr.tokens.icon_default
    # 0. Shapes are drawn, not looked up: the icon IS the canvas drawing of
    #    the shape (shape_geometry), so the toolbar can never show a shape
    #    differently from how it lands on the board.
    from ui.canvas.shape_geometry import SHAPE_TOOLS, shape_icon
    if name in SHAPE_TOOLS:
        return QIcon(shape_icon(name, size, color))
    # 1. SVG — best quality
    svg_path = ICONS_DIR / f"{name}.svg"
    if svg_path.exists():
        pm = _render_svg(svg_path, size, color)
        if pm is not None and not pm.isNull():
            return QIcon(pm)

    # 2. Fluent / MDL2 font glyph (Windows only)
    try:
        from ui.util.fluent_icons import icon_char, icon_font_family, is_fluent_active
        char = icon_char(name)
        if is_fluent_active() and char and ord(char[0]) >= 0xE000:
            pm = _render_char(char, size, color, icon_font_family())
            return QIcon(pm)
        # 3. Emoji
        if char:
            pm = _render_char(char, size, color)
            return QIcon(pm)
    except Exception as e:
        logger.debug("Fallback icon failed for '%s': %s", name, e)

    # 4. Empty (should never happen)
    return QIcon(_blank_pixmap(size, _target_dpr()))