"""
pages_panel.py — Side panel showing page thumbnails.

Opens as a right-side strip from top to bottom of the screen.
Each card shows a small preview thumbnail + page number + delete button.
Click a card to jump to that page. Scroll starts at the current page.

RAM optimised: thumbnails are rendered at a fixed small resolution (240×135)
and only for visible cards (via QScrollArea viewport checks).
"""
from __future__ import annotations

import logging
import time
from typing import Optional, Callable, TYPE_CHECKING

from PySide6.QtWidgets import (
    QDialog, QVBoxLayout, QHBoxLayout, QLabel, QPushButton,
    QScrollArea, QWidget, QFrame, QSizePolicy, QApplication,
    QGraphicsScene, QGraphicsBlurEffect,
)
from PySide6.QtGui import (
    QPainter, QPen, QBrush, QColor, QPixmap, QPainterPath,
    QFont, QTransform, QImage,
)
from PySide6.QtCore import (QEvent, Qt, QPoint, QPointF, QRect, QRectF, QSize,
                            QTimer)

from ui.util.scaling import UiScale
from ui.util.aero import prepare_aero_widget, apply_aero, enable_shadow
from ui.util.animations import fade_in
from ui.util.shadows import _preset_for
from ui.util.audio import click_sound
from ui.theme.theme_manager import theme_mgr

if TYPE_CHECKING:
    from ui.canvas.canvas_widget import CanvasWidget

try:
    import logicboard_engine as lbe
    HAS_ENGINE = True
except ImportError:
    lbe = None
    HAS_ENGINE = False

__all__ = ["PagesPanel"]

# Thumbnail size (px before DPI scaling)
THUMB_W = 240
THUMB_H = 135


# ── shadows, baked once ─────────────────────────────────────────────────────
#
# The cards used to wear QGraphicsDropShadowEffect — one on every card and
# one on every thumbnail frame. That effect re-renders its widget offscreen
# and re-blurs it on EVERY repaint, and while pages slide every card in view
# repaints every frame. Measured in a real window: 14 ms a frame median, 19
# at worst, against 16.7 for 60 fps — the stutter in reordering. The same
# shadow drawn from a cached, pre-blurred picture costs a blit: 7.6 ms.
#
# Same Fluent presets (ui.theme tokens elev_*), so it looks the same.

_SHADOWS: dict = {}


def _baked_shadow(w: int, h: int, radius: float, depth: str,
                  dpr: float) -> tuple:
    """(pixmap, pad, dx, dy): the shadow of a ``w`` x ``h`` rounded rect,
    blurred once and cached. Draw it at (x - pad + dx, y - pad + dy)."""
    blur, dx, dy, alpha = _preset_for(depth)
    key = (w, h, round(radius, 1), blur, dx, dy, alpha, round(dpr, 2))
    hit = _SHADOWS.get(key)
    if hit is not None:
        return hit
    pad = int(blur) + 2
    W, H = w + 2 * pad, h + 2 * pad
    img = QImage(max(1, int(round(W * dpr))), max(1, int(round(H * dpr))),
                 QImage.Format_ARGB32_Premultiplied)
    img.fill(Qt.transparent)
    img.setDevicePixelRatio(dpr)
    scene = QGraphicsScene(0, 0, W, H)
    path = QPainterPath()
    path.addRoundedRect(QRectF(pad, pad, w, h), radius, radius)
    item = scene.addPath(path, QPen(Qt.NoPen), QBrush(QColor(0, 0, 0, alpha)))
    fx = QGraphicsBlurEffect()
    fx.setBlurRadius(blur)
    fx.setBlurHints(QGraphicsBlurEffect.QualityHint)
    item.setGraphicsEffect(fx)
    p = QPainter(img)
    p.setRenderHint(QPainter.Antialiasing)
    scene.render(p, QRectF(0, 0, W, H), QRectF(0, 0, W, H))
    p.end()
    if len(_SHADOWS) > 64:
        _SHADOWS.clear()
    hit = (QPixmap.fromImage(img), pad, dx, dy)
    _SHADOWS[key] = hit
    return hit


class _Ghost(QWidget):
    """The lifted page: the card's picture over its own float shadow, baked
    into ONE pixmap at grab time and drawn scaled. A label wearing a live
    drop-shadow effect re-blurred a 40 px shadow every frame it grew or
    moved."""

    def __init__(self, parent, card: "PageCard"):
        super().__init__(parent)
        self.setAttribute(Qt.WA_TransparentForMouseEvents, True)
        dpr = float(card.devicePixelRatioF() or 1.0)
        shot = card.grab()
        sh, pad, dx, dy = _baked_shadow(card.width(), card.height(),
                                        card.corner_radius(), "float", dpr)
        self.pad = pad
        img = QPixmap(int(round((card.width() + 2 * pad) * dpr)),
                      int(round((card.height() + 2 * pad) * dpr)))
        img.setDevicePixelRatio(dpr)
        img.fill(Qt.transparent)
        p = QPainter(img)
        p.setRenderHint(QPainter.Antialiasing)
        p.drawPixmap(dx, dy, sh)
        # grab() fills the corners outside the card's curve with solid
        # background, which gave the lifted page square dark corners. Only
        # the rounded card itself is kept.
        clip = QPainterPath()
        clip.addRoundedRect(QRectF(pad, pad, card.width(), card.height()),
                            card.corner_radius(), card.corner_radius())
        p.setClipPath(clip)
        p.drawPixmap(pad, pad, shot)
        p.end()
        self._pm = img

    def paintEvent(self, _e):                           # noqa: N802
        p = QPainter(self)
        p.setRenderHint(QPainter.SmoothPixmapTransform)
        p.drawPixmap(self.rect(), self._pm)
        p.end()


class _ReorderHandle(QWidget):
    """The grip on a page card: press it and drag to move the page.

    Reordering used to have exactly one entry point — hold a card still for
    380 ms — which nothing on screen announced. A grip is the control every
    touch UI uses for "this can be moved", and pressing it picks the card up
    AT ONCE: there is no long press to wait out, because a press on the grip
    cannot mean anything else.

    It drives the very same drag as the long press (PageCard._start_drag),
    so the lift, the sliding cards, the edge auto-scroll and the drop glide
    are one implementation, not two.

    Six dots in two columns, drawn rather than typed: the braille "grip"
    glyph changes size and weight with whichever font the machine has, and
    on the board it fell back to a box.
    """

    def __init__(self, card: "PageCard"):
        super().__init__(card)
        self._card = card
        self._hover = False
        self._held = False
        sc = UiScale.px
        self.setFixedSize(sc(30), sc(30))
        self.setCursor(Qt.OpenHandCursor)
        self.setToolTip("Drag to reorder")
        self.setAttribute(Qt.WA_Hover, True)

    def enterEvent(self, e):                            # noqa: N802
        self._hover = True
        self.update()
        super().enterEvent(e)

    def leaveEvent(self, e):                            # noqa: N802
        self._hover = False
        self.update()
        super().leaveEvent(e)

    def paintEvent(self, _e):                           # noqa: N802
        t = theme_mgr.tokens
        sc = UiScale.px
        p = QPainter(self)
        p.setRenderHint(QPainter.Antialiasing)
        r = QRectF(self.rect()).adjusted(0.5, 0.5, -0.5, -0.5)
        if self._held or self._hover:
            p.setPen(Qt.NoPen)
            p.setBrush(theme_mgr.qcolor(t.accent_muted if self._held
                                        else t.fill_hover))
            p.drawRoundedRect(r, sc(8), sc(8))
        dot = theme_mgr.qcolor(t.accent if self._held
                               else (t.text_primary if self._hover
                                     else t.text_secondary))
        p.setPen(Qt.NoPen)
        p.setBrush(dot)
        rad = max(1.4, sc(2) * 0.9)
        cx, cy = self.width() / 2.0, self.height() / 2.0
        dx, dy = sc(3.5), sc(5.5)
        for ix in (-1, 1):
            for iy in (-1, 0, 1):
                p.drawEllipse(QPointF(cx + ix * dx, cy + iy * dy), rad, rad)
        p.end()

    def _set_held(self, on: bool) -> None:
        self._held = on
        self.setCursor(Qt.ClosedHandCursor if on else Qt.OpenHandCursor)
        self.update()

    def mousePressEvent(self, e):                       # noqa: N802
        if e.button() == Qt.LeftButton:
            self._set_held(True)
            self._card._start_drag(e.globalPosition().toPoint())
        e.accept()

    def mouseMoveEvent(self, e):                        # noqa: N802
        # This widget holds the implicit grab for the whole gesture, so the
        # moves arrive HERE and are handed to the card's drag.
        self._card._drag_move_to(e.globalPosition().toPoint())
        e.accept()

    def mouseReleaseEvent(self, e):                     # noqa: N802
        self._set_held(False)
        self._card._drag_release(e.globalPosition().toPoint())
        e.accept()


class PageCard(QFrame):
    """Single page thumbnail card — Windows 11 / Fluent floating card.

    Layout matches the user's mockup:

      ╔═══════════════════════════════════╗  ← Outer card, solid,
      ║ Page 3                       🗑   ║      curved edges, soft shadow
      ║  ┌─────────────────────────────┐  ║
      ║  │                             │  ║  ← Inner thumbnail frame
      ║  │     (page thumbnail)        │  ║      with its own border + radius
      ║  │                             │  ║
      ║  └─────────────────────────────┘  ║
      ╚═══════════════════════════════════╝

    The outer card is a solid surface one step above the panel, lit from
    above (a soft top-to-bottom gradient); the thumbnail sits in an inset
    rounded frame inside it.
    """

    #: How long a finger has to rest on a card before it becomes draggable.
    #:
    #: Long enough that scrolling never turns into a reorder by accident —
    #: a flick is moving well before this — and short enough that somebody
    #: deliberately holding a page does not think nothing is happening.
    LONG_PRESS_MS = 380

    def __init__(self, index: int, total: int, is_current: bool,
                 on_click: Callable, on_delete: Callable, parent=None,
                 on_drag_start: Optional[Callable] = None,
                 on_drag_move: Optional[Callable] = None,
                 on_drag_end: Optional[Callable] = None):
        super().__init__(parent)

        self._index = index
        self._on_click = on_click
        self._on_delete = on_delete
        self._is_current = is_current
        self._total = total
        # Tap-vs-scroll state — see mousePressEvent and friends below.
        self._press_pos: Optional[QPoint] = None
        self._press_live: bool = False
        # Long-press-to-drag.
        self._on_drag_start = on_drag_start
        self._on_drag_move = on_drag_move
        self._on_drag_end = on_drag_end
        self._dragging = False
        self._hold_timer = QTimer(self)
        self._hold_timer.setSingleShot(True)
        self._hold_timer.setTimerType(Qt.PreciseTimer)
        self._hold_timer.timeout.connect(self._hold_elapsed)

        sc = UiScale.px
        t  = theme_mgr.tokens

        self.setObjectName("PageCard")
        self.setFrameShape(QFrame.NoFrame)
        self.setCursor(Qt.PointingHandCursor)

        # ── Outer card surface ─────────────────────────────────────────
        # Solid, lit from above. The current page gets a 2-px accent stroke.
        #
        # THE CARD HAS TO BE ALLOWED TO PAINT ITS OWN BACKGROUND: if
        # WA_TranslucentBackground ever reaches it, a stylesheet fill is
        # silently not drawn at all.
        self.setAttribute(Qt.WA_TranslucentBackground, False)
        self.setAttribute(Qt.WA_NoSystemBackground, False)
        self.setAttribute(Qt.WA_StyledBackground, True)

        self._apply_surface_style()
        # Its drop shadow is painted by the stack under it (see
        # _CardStack.paintEvent) — the current card sits slightly higher.

        # Card size:
        #   width  = thumbnail + horizontal padding × 2
        #   height = header row + thumbnail + spacings
        pad      = sc(14)
        header_h = sc(28)
        thumb_w  = sc(THUMB_W)
        thumb_h  = sc(THUMB_H)
        self.setFixedSize(thumb_w + 2 * pad, header_h + thumb_h + 3 * pad)

        # ── Outer layout (vertical: header on top, thumb below) ────────
        outer = QVBoxLayout(self)
        outer.setContentsMargins(pad, pad, pad, pad)
        outer.setSpacing(sc(8))

        # ── Header row ─────────────────────────────────────────────────
        header = QHBoxLayout()
        header.setContentsMargins(0, 0, 0, 0)
        header.setSpacing(sc(6))

        # Page name label — accent-coloured when current to act as a
        # subtle "you are here" indicator.
        name_lbl = QLabel(f"Page {index + 1}")
        self._name_lbl = name_lbl
        name_lbl.setAttribute(Qt.WA_TransparentForMouseEvents, True)
        self._apply_name_style()
        header.addWidget(name_lbl, 0, Qt.AlignVCenter)
        header.addStretch(1)

        # Reorder grip — shown only when there is something to reorder
        # against, and only when the panel can actually move pages.
        #
        # Both controls are ALWAYS built and merely shown or hidden (see
        # _sync_controls): cards are recycled, and one built while the board
        # had a single page used to stay without a bin for ever after — and
        # one built earlier kept its bin when the board got down to one page.
        self._grip = None
        if on_drag_start is not None:
            self._grip = _ReorderHandle(self)
            header.addWidget(self._grip, 0, Qt.AlignVCenter)

        # Delete button (TOP-RIGHT) — shown only when there's more than one
        # page, so the user can't accidentally delete their only page.
        if True:
            del_btn = QPushButton("🗑")
            self._del_btn = del_btn
            # NEVER takes focus. A pressed button keeps the keyboard focus;
            # the delete then hides its card in the rebuild, Qt hands the
            # focus to the NEXT card's bin, and the scroll area scrolls to
            # show that one — which is hidden in turn, and so on. Every
            # delete flung the list four cards down, and each of those
            # scrolls rebuilt the visible window while the rebuild that
            # caused them was still half done, so cards ended up bound to
            # the wrong pages or to none: the "broken" pages panel.
            del_btn.setFocusPolicy(Qt.NoFocus)
            del_sz = sc(26)
            del_btn.setFixedSize(del_sz, del_sz)
            del_btn.setCursor(Qt.PointingHandCursor)
            del_btn.setStyleSheet(f"""
                QPushButton {{
                    background-color: transparent;
                    color: {t.text_secondary};
                    border: 1px solid transparent;
                    border-radius: {del_sz // 2}px;
                    font-size: {sc(13)}px;
                    padding: 0px;
                }}
                QPushButton:hover {{
                    background-color: {t.danger_bg_hover};
                    color: {t.danger_text};
                    border-color: {t.danger_border_btn};
                }}
                QPushButton:pressed {{
                    background-color: {t.danger_bg};
                }}
            """)
            # self._index, NOT the index captured at build time: a
            # recycled card stands for a different page than the one it was
            # created for, and a bound index would delete the old one.
            del_btn.clicked.connect(
                lambda _checked=False: self._on_delete(self._index)
            )
            header.addWidget(del_btn, 0, Qt.AlignVCenter)
        self._sync_controls()

        outer.addLayout(header)

        # ── Thumbnail frame (inset, with its own border) ───────────────
        # This is the bordered rectangle in the user's mockup. The
        # actual rendered preview is painted inside via setPixmap().
        thumb_frame = QFrame()
        thumb_frame.setObjectName("PageThumbFrame")
        thumb_frame.setFixedSize(thumb_w, thumb_h)
        thumb_frame.setStyleSheet(f"""
            QFrame#PageThumbFrame {{
                background-color: {t.canvas_bg_color};
                border: 1px solid {t.stroke_default};
                border-radius: {t.radius_md}px;
            }}
        """)
        # The thumbnail label sits inside the frame, slightly inset so
        # the frame's border remains visible around the image.
        thumb_layout = QVBoxLayout(thumb_frame)
        thumb_layout.setContentsMargins(2, 2, 2, 2)
        thumb_layout.setSpacing(0)

        self._thumb = QLabel()
        self._thumb.setAlignment(Qt.AlignCenter)
        self._thumb.setAttribute(Qt.WA_TransparentForMouseEvents, True)
        self._thumb.setStyleSheet(
            "QLabel { background: transparent; border: none; }"
        )
        thumb_layout.addWidget(self._thumb)

        # Soft shadow on the thumbnail frame too, for a sense of depth —
        # painted by the card itself (paintEvent), not a live effect.
        self._thumb_frame = thumb_frame

        outer.addWidget(thumb_frame, 0, Qt.AlignHCenter)

    # ── shadows ────────────────────────────────────────────────────────
    def corner_radius(self) -> float:
        return float(UiScale.px(theme_mgr.tokens.radius_2xl))

    def shadow(self) -> tuple:
        """This card's drop shadow, baked: see _baked_shadow."""
        return _baked_shadow(self.width(), self.height(), self.corner_radius(),
                             "hover" if self._is_current else "rest",
                             float(self.devicePixelRatioF() or 1.0))

    def _shadow_reach(self) -> int:
        _pm, pad, dx, dy = self.shadow()
        return pad + max(abs(dx), abs(dy))

    def _repaint_shadow(self, rect: QRect) -> None:
        """The stack paints this card's shadow, and Qt only repaints the
        card's own rectangle when it moves — so the shadow's fringe has to
        be asked for, or it would smear behind a sliding card."""
        par = self.parentWidget()
        if par is not None:
            m = self._shadow_reach()
            par.update(rect.adjusted(-m, -m, m, m))

    def moveEvent(self, e):                             # noqa: N802
        super().moveEvent(e)
        if self.isVisible():
            self._repaint_shadow(QRect(e.oldPos(), self.size()))
            self._repaint_shadow(self.geometry())

    def resizeEvent(self, e):                           # noqa: N802
        super().resizeEvent(e)
        if self.isVisible():
            self._repaint_shadow(QRect(self.pos(), e.oldSize()))
            self._repaint_shadow(self.geometry())

    def showEvent(self, e):                             # noqa: N802
        super().showEvent(e)
        self._repaint_shadow(self.geometry())

    def hideEvent(self, e):                             # noqa: N802
        super().hideEvent(e)
        self._repaint_shadow(self.geometry())

    def paintEvent(self, e):                            # noqa: N802
        super().paintEvent(e)                # the styled card surface
        f = getattr(self, "_thumb_frame", None)
        if f is None:
            return
        sh, pad, dx, dy = _baked_shadow(
            f.width(), f.height(), float(theme_mgr.tokens.radius_md), "rest",
            float(self.devicePixelRatioF() or 1.0))
        p = QPainter(self)
        p.drawPixmap(f.x() - pad + dx, f.y() - pad + dy, sh)
        p.end()

    def _apply_surface_style(self) -> None:
        t = theme_mgr.tokens
        sc = UiScale.px
        border_w = "2px" if self._is_current else "1px"
        border_col = t.accent if self._is_current else t.stroke_default
        top_col = t.page_card_bg_hover
        self.setStyleSheet(f"""
            QFrame#PageCard {{
                background: qlineargradient(x1:0, y1:0, x2:0, y2:1,
                    stop:0 {top_col}, stop:1 {t.page_card_bg});
                border: {border_w} solid {border_col};
                border-radius: {sc(t.radius_2xl)}px;
            }}
            QFrame#PageCard:hover {{
                background: {t.page_card_bg_hover};
                border-color: {t.accent if self._is_current else t.stroke_strong};
            }}
        """)

    def _apply_name_style(self) -> None:
        t = theme_mgr.tokens
        sc = UiScale.px
        self._name_lbl.setStyleSheet(f"""
            QLabel {{
                color: {t.accent if self._is_current else t.text_primary};
                font-size: {sc(13)}px;
                font-weight: {'700' if self._is_current else '600'};
                background: transparent;
                border: none;
            }}
        """)

    def _sync_controls(self) -> None:
        """Bin and grip only when the board has more than one page."""
        many = self._total > 1
        self._del_btn.setVisible(many)
        if self._grip is not None:
            self._grip.setVisible(many)

    def rebind(self, index: int, total: int, is_current: bool) -> None:
        """Point this card at a different page.

        Recycling exists because a far jump on a long board otherwise
        replaces the whole visible window: measured at 1000 pages, throwing
        away sixteen cards and building sixteen more cost 54 ms to reach the
        far end and 92 ms to jump to an arbitrary page — both well past a
        frame. A small scroll was always fine (4 ms), because most of the
        cards survive it. Re-labelling a card is a few property writes
        against 1.35 ms to construct one.

        Everything that varies per page is reset here. The thumbnail
        especially: it belongs to the page the card USED to be, and leaving
        it would show the wrong preview under the right number.
        """
        if index == self._index and total == self._total \
                and is_current == self._is_current:
            return
        was_current = self._is_current
        self._index = index
        self._total = total
        self._is_current = is_current
        self._name_lbl.setText(f"Page {index + 1}")
        self._sync_controls()
        if was_current != is_current:
            self._apply_name_style()
            self._apply_surface_style()
            if self.isVisible():
                self._repaint_shadow(self.geometry())
        self.clear_thumbnail()

    def clear_thumbnail(self) -> None:
        """Drop the preview, so a recycled card cannot show the old page."""
        try:
            self._thumb.clear()
        except Exception:
            pass

    def adopt_thumbnail(self, pixmap: QPixmap) -> None:
        """Take a preview that is ALREADY the right size — one carried over
        from the card that showed this page before a rebuild."""
        if pixmap is not None and not pixmap.isNull():
            self._thumb.setPixmap(pixmap)

    def has_thumbnail(self) -> bool:
        """True once a thumbnail has been set. Lets the panel skip cards it
        has already filled instead of re-fetching them on every scroll."""
        pm = self._thumb.pixmap()
        return pm is not None and not pm.isNull()

    def set_thumbnail(self, pixmap: QPixmap):
        """Fit the page preview into the card, at the screen's real resolution.

        THE BLUR THIS FIXES: the target size here is LOGICAL pixels, and a
        pixmap scaled to a logical size carries a device-pixel-ratio of 1.
        Qt then has to stretch it to the panel's physical pixels at paint
        time — so on any Hi-DPI board every page preview was being resampled
        a second time, upwards, by the compositor. The first downscale was
        already smooth and careful; the second one is what softened the ink.

        The fix is the same one the toolbar icons use (see
        ``icon_loader._blank_pixmap``): rasterise at ``logical * dpr``
        PHYSICAL pixels and tag the result, after which Qt blits it 1:1 and
        the second resample does not happen at all.

        Nothing here costs more than it did. The source pixmap the manager
        hands over is far larger than the card either way, so this changes
        only which size it is reduced TO — one downscale, as before, just to
        the right number of pixels.
        """
        if not pixmap or pixmap.isNull():
            return
        # Account for the 2-px inset of the thumb_layout margins.
        target = self._thumb.size()
        if target.width() <= 1 or target.height() <= 1:
            target = QSize(UiScale.px(THUMB_W) - 4,
                           UiScale.px(THUMB_H) - 4)
        # The ratio the LABEL will be painted at, not the primary screen's:
        # a board plugged into a second monitor of a different scale must
        # take the one it is actually showing on.
        dpr = float(self._thumb.devicePixelRatioF() or 1.0)
        phys = QSize(max(1, int(round(target.width() * dpr))),
                     max(1, int(round(target.height() * dpr))))
        scaled = pixmap.scaled(
            phys,
            Qt.KeepAspectRatio,
            Qt.SmoothTransformation,
        )
        # Without this the pixmap is simply a bigger picture and the label
        # draws it at double size — the tag is what says "these are physical
        # pixels; lay them down one for one".
        scaled.setDevicePixelRatio(dpr)
        self._thumb.setPixmap(scaled)

    # ── Tap vs scroll ────────────────────────────────────────────────────
    # A card is nearly the full width and a good chunk of the height of the
    # panel, so a finger that wants to scroll the list has nowhere to land
    # except on one. Committing the jump on PRESS meant every scroll attempt
    # was read as "go to this page": the panel jumped to whatever page the
    # finger touched and closed itself, before the finger had moved a
    # millimetre. The list was effectively unscrollable by touch.
    #
    # A tap is now only committed on RELEASE, and only if the finger stayed
    # put and the kinetic scroller never took over. Three things make that
    # reliable on a touch board:
    #
    #   * Qt cancels a child's press when QScroller claims the gesture, by
    #     sending it a release at an off-widget position — which fails the
    #     distance check below by a mile, so no tap is emitted.
    #   * The slop is measured in MILLIMETRES, like QScroller's own drag
    #     threshold, so "held still" means the same thing on a laptop and on
    #     a 4K board.
    #   * A press landing on a coasting flick stops the flick instead of
    #     navigating — what every touch UI does, and the reason the scroller
    #     state is consulted as well as the distance.
    #
    # Release-based tapping is already proven on this hardware: every
    # QPushButton in the app (including this card's own delete button) fires
    # on release and works fine by touch.

    def _tap_slop(self) -> float:
        """How far (px) the finger may travel and still count as a tap.

        Deliberately a little LARGER than QScroller's 5 mm drag threshold
        (see ui/util/touch_scroll.py): a wobble too small to start a scroll
        must still register as a tap, otherwise there's a dead band where
        neither scrolling nor tapping happens and the card feels broken.
        """
        try:
            from PySide6.QtGui import QGuiApplication
            screen = QGuiApplication.primaryScreen()
            if screen is not None:
                dpi = float(screen.physicalDotsPerInch())
                if dpi > 1:
                    return max(float(UiScale.px(12)), (dpi / 25.4) * 6.0)
        except Exception:
            pass
        return float(UiScale.px(18))

    def _scroller_busy(self) -> bool:
        """True while the list around us is ACTUALLY MOVING — being dragged,
        or still coasting from a flick.

        The distinction from "not Inactive" is the whole of drag-to-reorder.
        QScroller has four states, and a press puts it straight into
        ``Pressed`` — which does not mean the list is scrolling, it means the
        scroller has seen a finger go down and has not yet decided what it
        is. A long press to pick up a card lives entirely inside that state.

        Treating ``Pressed`` as busy is why the gesture could never start.
        ``mousePressEvent`` armed fine, because the card gets the press
        while the scroller is still Inactive — which is exactly why TAPS
        went on working and hid the problem — but 380 ms later
        ``_hold_elapsed`` asked again, found ``Pressed``, and returned. The
        card was never picked up, on any board, ever.

        Dragging and Scrolling are the states that really mean "hands off":
        one is the user moving the list, the other is it coasting, and a
        press during either is a "stop the scroll" gesture rather than the
        start of anything.
        """
        try:
            from PySide6.QtWidgets import QAbstractScrollArea, QScroller
            w = self.parent()
            while w is not None and not isinstance(w, QAbstractScrollArea):
                w = w.parent()
            if w is None:
                return False
            vp = w.viewport()
            if vp is None or not QScroller.hasScroller(vp):
                return False
            state = QScroller.scroller(vp).state()
            S = QScroller.State
            return state in (S.Dragging, S.Scrolling)
        except Exception:
            return False          # never let this block a tap

    @staticmethod
    def _travel(event, origin) -> float:
        if origin is None:
            return 0.0
        return float((event.globalPosition().toPoint()
                      - origin).manhattanLength())

    # ── long press -> drag ──────────────────────────────────────────
    #
    # The card already has to tell a tap from a scroll. A third gesture goes
    # on the same state machine rather than beside it, because the three are
    # mutually exclusive and deciding between them in one place is the only
    # way they stay consistent:
    #
    #   moved beyond the slop   -> the list is scrolling, never a tap
    #   held still past 380 ms  -> a drag, and never a tap afterwards
    #   released before either  -> a tap
    #
    # The hold timer is cancelled by ANY of: movement, release, or the drag
    # actually starting. A timer that outlives its gesture is how a card
    # ends up picked up after the finger has gone.

    def _hold_elapsed(self) -> None:
        """The finger stayed put long enough. Pick the card up."""
        if self._press_pos is None or not self._press_live:
            return
        if self._scroller_busy():
            return
        self._start_drag(self._press_pos)

    # The ONE way a drag starts, moves and ends — the long press above and
    # the grip both come through here.
    def _start_drag(self, global_pos) -> None:
        if self._on_drag_start is None or self._dragging:
            return
        self._cancel_hold()
        self._press_live = False        # this can no longer become a tap
        self._dragging = True
        try:
            click_sound.play_click()
        except Exception:
            pass
        self._on_drag_start(self._index, global_pos)

    def _drag_move_to(self, global_pos) -> None:
        if self._dragging and self._on_drag_move is not None:
            self._on_drag_move(global_pos)

    def _drag_release(self, global_pos) -> None:
        was = self._dragging
        self._dragging = False
        self._press_live, self._press_pos = False, None
        if was and self._on_drag_end is not None:
            self._on_drag_end(global_pos)

    def _cancel_hold(self) -> None:
        self._hold_timer.stop()

    def mousePressEvent(self, event):
        if event.button() == Qt.LeftButton:
            self._press_pos = event.globalPosition().toPoint()
            # Landing on a moving list is a "stop the scroll" gesture, so it
            # can never become a page jump however still the finger then is.
            self._press_live = not self._scroller_busy()
            self._dragging = False
            if self._press_live and self._on_drag_start is not None:
                self._hold_timer.start(self.LONG_PRESS_MS)
        event.accept()

    def mouseMoveEvent(self, event):
        if self._dragging:
            if self._on_drag_move is not None:
                self._on_drag_move(event.globalPosition().toPoint())
            event.accept()
            return
        if (self._press_live
                and self._travel(event, self._press_pos) > self._tap_slop()):
            self._press_live = False          # the finger is scrolling
            self._cancel_hold()
        event.accept()

    def mouseReleaseEvent(self, event):
        # Read the press state, then clear it BEFORE any early return, so a
        # cancelled gesture can't leave the card armed for the next event.
        self._cancel_hold()
        live, pos = self._press_live, self._press_pos
        was_dragging = self._dragging
        self._press_live, self._press_pos = False, None
        self._dragging = False
        if was_dragging:
            if self._on_drag_end is not None:
                self._on_drag_end(event.globalPosition().toPoint())
            event.accept()
            return
        if (event.button() == Qt.LeftButton and live and pos is not None
                and self._travel(event, pos) <= self._tap_slop()
                and not self._scroller_busy()):
            click_sound.play_click()
            self._on_click(self._index)
        event.accept()


class _InsertGap(QPushButton):
    """The "+ Page" row in the gap between two page cards.

    Inserting a page between two others used to be a right-click menu,
    which a touch board does not have — it meant a long, fiddly press with
    no hint that there was anything to find. Now every gap says what it
    does: a hairline either side of a small "+ Page" pill. The WHOLE gap is
    the touch target (a finger does not have to find the pill), but only
    the pill is drawn, so the column stays quiet.

    A QPushButton underneath, on purpose: it commits on RELEASE, and when
    the kinetic scroller claims a flick that started here, Qt sends the
    button a release outside itself — so scrolling through the column never
    inserts a page by accident. Same rule the page cards follow.
    """

    def __init__(self, parent, on_insert: Callable):
        super().__init__(parent)
        self._at = 0
        self._on_insert = on_insert
        self.setCursor(Qt.PointingHandCursor)
        self.setFocusPolicy(Qt.NoFocus)
        self.setFlat(True)
        self.setAttribute(Qt.WA_Hover, True)
        self.setStyleSheet("QPushButton { background: transparent; border: none; }")
        self.clicked.connect(self._fire)

    def bind(self, at: int, total: int) -> None:
        """This gap inserts a page so that it becomes page ``at`` (0-based)
        — i.e. between page ``at`` and page ``at + 1`` as the user counts."""
        self._at = int(at)
        self.setToolTip("Insert a page between page %d and page %d"
                        % (at, at + 1))
        self.setAccessibleName("Insert a page here")

    @property
    def insert_at(self) -> int:
        return self._at

    def _fire(self) -> None:
        self._on_insert(self._at)

    def paintEvent(self, _event):
        t = theme_mgr.tokens
        q = theme_mgr.qcolor
        sc = UiScale.px
        p = QPainter(self)
        p.setRenderHint(QPainter.Antialiasing)
        hot = self.underMouse() or self.isDown()

        f = QFont(self.font())
        f.setPixelSize(max(9, sc(12)))
        f.setWeight(QFont.Weight.DemiBold)
        p.setFont(f)
        label = "+  Page"
        from PySide6.QtGui import QFontMetrics
        fm = QFontMetrics(f)
        pill_w = fm.horizontalAdvance(label) + sc(24)
        pill_h = min(self.height() - 2, sc(26))
        cx, cy = self.width() / 2.0, self.height() / 2.0
        pill = QRectF(cx - pill_w / 2.0, cy - pill_h / 2.0, pill_w, pill_h)

        # Hairlines either side, fading toward the edges.
        from PySide6.QtGui import QLinearGradient
        line = q(t.accent if hot else t.stroke_strong)
        for x0, x1 in ((sc(14), pill.left() - sc(8)),
                       (pill.right() + sc(8), self.width() - sc(14))):
            if x1 <= x0:
                continue
            g = QLinearGradient(x0, 0, x1, 0)
            far = QColor(line); far.setAlpha(0)
            near = QColor(line); near.setAlpha(150 if hot else 110)
            if x0 < cx:
                g.setColorAt(0, far); g.setColorAt(1, near)
            else:
                g.setColorAt(0, near); g.setColorAt(1, far)
            p.setPen(QPen(QBrush(g), 1.0))
            p.drawLine(QPointF(x0, cy), QPointF(x1, cy))

        if self.isDown():
            fill = q(t.accent_pressed)
        elif hot:
            fill = q(t.accent)
        else:
            fill = q(t.bg_card)
        p.setPen(QPen(q(t.accent if hot else t.stroke_default), 1.0))
        p.setBrush(fill)
        p.drawRoundedRect(pill, pill_h / 2.0, pill_h / 2.0)
        p.setPen(q(t.text_on_accent if hot else t.accent))
        p.drawText(pill, Qt.AlignCenter, label)


class _DropSlot(QWidget):
    """The place the dragged page will land: a dashed outline the size of a
    card, gliding from slot to slot as the drag moves. Without it the gap
    the other cards opened was just empty panel, and it was not obvious
    that letting go would put the page THERE."""

    def __init__(self, parent):
        super().__init__(parent)
        self.setAttribute(Qt.WA_TransparentForMouseEvents, True)
        self.hide()

    def paintEvent(self, _e):                           # noqa: N802
        t = theme_mgr.tokens
        sc = UiScale.px
        p = QPainter(self)
        p.setRenderHint(QPainter.Antialiasing)
        r = QRectF(self.rect()).adjusted(1.5, 1.5, -1.5, -1.5)
        fill = QColor(theme_mgr.qcolor(t.accent))
        fill.setAlpha(26)
        pen = QPen(theme_mgr.qcolor(t.accent), max(1.5, sc(2)))
        pen.setStyle(Qt.DashLine)
        pen.setDashPattern([4, 3])
        p.setPen(pen)
        p.setBrush(fill)
        rad = sc(16)
        p.drawRoundedRect(r, rad, rad)
        p.end()


class _CardStack(QWidget):
    """The scrolling content widget. Owns no layout — see PagesPanel.

    Exists only so a width change can be noticed: cards are positioned and
    sized by hand, so when the panel gets wider something has to tell them.
    A layout would have done this, and a layout is what could not carry a
    thousand pages.
    """

    def __init__(self, panel):
        super().__init__()
        self._panel = panel

    def resizeEvent(self, event):
        super().resizeEvent(event)
        try:
            self._panel._relayout_cards()
        except Exception:
            logging.debug("card relayout failed", exc_info=True)

    def paintEvent(self, event):                        # noqa: N802
        """Every visible card's drop shadow, from the baked cache — a blit
        per card, where a live effect re-blurred each one per frame."""
        clip = event.rect()
        p = None
        for card in self._panel._cards:
            if card is None or not card.isVisible() or card.x() < 0:
                continue                 # parked during a drag: no shadow
            sh, pad, dx, dy = card.shadow()
            r = QRect(card.x() - pad + dx, card.y() - pad + dy,
                      card.width() + 2 * pad, card.height() + 2 * pad)
            if not r.intersects(clip):
                continue
            if p is None:
                p = QPainter(self)
            p.drawPixmap(r.topLeft(), sh)
        if p is not None:
            p.end()


class PagesPanel(QDialog):
    """Right-side pages panel stretching from top to bottom."""

    def __init__(self, canvas: CanvasWidget, parent=None, *,
                 on_goto: Optional[Callable] = None,
                 on_delete: Optional[Callable] = None,
                 on_add: Optional[Callable] = None,
                 on_move: Optional[Callable] = None,
                 on_insert: Optional[Callable] = None,
                 thumb_manager=None):
        super().__init__(parent, Qt.Tool | Qt.FramelessWindowHint | Qt.NoDropShadowWindowHint)
        self.setModal(False)
        # The panel's solid, rounded body (see ui.util.aero.prepare_aero_widget)
        # — set up BEFORE the first show.
        prepare_aero_widget(self)
        # Outside-tap dismissal now uses the shared ``dismiss_on_outside``
        # helper, which handles more event types (TouchBegin, TabletPress,
        # release fallback) than the previous inline QApplication filter.
        # Toolbar buttons (in particular the Pages button) are ignored so
        # tapping them can toggle-close us in one action rather than
        # reopening us immediately after our own outside-dismiss ran.
        try:
            from ui.util.dismiss_on_outside import install_outside_dismiss
            tb = parent._toolbar if hasattr(parent, "_toolbar") else None
            ignore = []
            if tb is not None:
                for name in ("pages_btn", "left", "center", "right"):
                    w = getattr(tb, name, None)
                    if w is not None:
                        ignore.append(w)
            install_outside_dismiss(self, ignore_widgets=ignore)
        except Exception:
            pass
        self._canvas = canvas
        self._on_goto = on_goto
        self._on_delete = on_delete
        self._on_add = on_add
        self._on_move = on_move
        self._on_insert = on_insert

        # ── drag-to-reorder state ────────────────────────────────────────
        # Everything about a drag lives in one place so that abandoning one
        # is a single reset rather than five flags to remember.
        self._drag_src: Optional[int] = None      # index picked up
        self._drag_to: Optional[int] = None       # slot it would land in
        self._drag_ghost = None                   # the floating copy
        self._drag_grab_dy: int = 0               # cursor offset inside it
        self._drag_slot_h: int = 0                # one card + one gap
        self._drag_top: int = 0                   # y of slot 0 in content
        self._drag_park_x: Optional[int] = None   # where the lifted card sat
        # The ghost is placed by its CENTRE so its size can animate: it
        # grows as the page is lifted and shrinks back as it lands.
        self._ghost_pm = None                     # the card, grabbed once
        self._ghost_cw: int = 0                   # card size at scale 1
        self._ghost_ch: int = 0
        self._ghost_cx: float = 0.0               # centre, panel coords
        self._ghost_cy: float = 0.0
        self._ghost_scale: float = 1.0
        self._ghost_scale_to: float = 1.0
        # After the drop: the ghost glides into the landed card's slot and
        # the card is revealed under it once it gets there.
        self._drop_ghost = None
        self._drop_land: Optional[int] = None
        self._drop_slot: Optional[_DropSlot] = None
        self._reveal_after: set = set()           # cards shown once settled
        self._gaps: dict = {}                     # gap index -> _InsertGap
        self._gap_pool: list = []                 # spare "+ Page" rows
        self._card_pool: list = []                # spare cards, ready to rebind
        self._pool_cap: int = 0                   # biggest window seen so far
        self._scroller_suspended: bool = False    # we borrowed the gesture
        self._slide = QTimer(self)
        self._slide.setInterval(16)               # ~60 Hz
        self._slide.setTimerType(Qt.PreciseTimer)
        self._slide.timeout.connect(self._slide_step)
        self._edge_scroll = QTimer(self)
        self._edge_scroll.setInterval(16)
        self._edge_scroll.timeout.connect(self._edge_scroll_step)
        self._edge_dir = 0
        self._thumb_manager = thumb_manager
        self._cards: list[PageCard] = []
        # Incremental thumbnail fill — see _pump_thumbnails. The generation
        # counter cancels a fill that a rebuild has made obsolete. Declared
        # here (not next to the timer further down) so _rebuild is safe to
        # call at any point during construction.
        self._fill_queue: list[int] = []
        self._fill_gen: int = 0

        sc = UiScale.px
        t = theme_mgr.tokens

        panel_w = sc(THUMB_W + 52)

        self.setStyleSheet(f"""
            QDialog {{
                background: transparent;
                border-left: 1px solid {t.border};
            }}
        """)

        outer = QVBoxLayout(self)
        outer.setContentsMargins(0, 0, 0, 0)
        outer.setSpacing(0)

        # ---- Header ----
        header = QWidget()
        header.setFixedHeight(sc(52))
        header.setStyleSheet(f"""
            QWidget {{
                background: transparent;
                border-bottom: 1px solid {t.border};
            }}
        """)
        h_lay = QHBoxLayout(header)
        h_lay.setContentsMargins(sc(14), 0, sc(14), 0)
        title = QLabel("Pages")
        title.setStyleSheet(f"""
            QLabel {{
                color: {t.text_primary};
                font-size: {sc(18)}px;
                font-weight: bold;
                background: transparent;
            }}
        """)
        h_lay.addWidget(title)
        h_lay.addStretch()

        add_btn = QPushButton("+ Add")
        add_btn.setCursor(Qt.PointingHandCursor)
        add_btn.setFixedHeight(sc(34))
        add_btn.setStyleSheet(f"""
            QPushButton {{
                background: {t.accent_grad};
                color: #ffffff;
                border: none;
                border-radius: {sc(8)}px;
                font-size: {sc(13)}px;
                font-weight: bold;
                padding: 0 {sc(14)}px;
            }}
            QPushButton:hover {{
                background: {t.accent_grad_hover};
            }}
        """)
        add_btn.clicked.connect(self._on_add_clicked)
        h_lay.addWidget(add_btn)

        outer.addWidget(header)

        # ---- Scroll area ----
        self._scroll = QScrollArea()
        self._scroll.setWidgetResizable(True)
        self._scroll.setHorizontalScrollBarPolicy(Qt.ScrollBarAlwaysOff)
        self._scroll.setVerticalScrollBarPolicy(Qt.ScrollBarAsNeeded)
        from ui.util.touch_scroll import setup_scroll
        setup_scroll(self._scroll)   # modern scrollbar + touch flick

        # ── The card stack ────────────────────────────────────────────
        #
        # Positioned by hand, with no layout, because the panel only builds
        # the cards you can actually see.
        #
        # A QVBoxLayout holding one card per page does not survive a long
        # board. Measured: 25 pages rebuilt in 114 ms, 100 in 555 ms, 400 in
        # 3.8 s and 1000 in 18.8 s — 4.5 ms per card climbing to 18.8, which
        # is not a widget being expensive but a list being redone for each
        # new member. Constructing a PageCard on its own is flat at 1.35 ms
        # however many already exist, so the cost is entirely the scroll
        # area and the layout managing N children.
        #
        # Cards are all one size in one column, so which pages are on screen
        # is arithmetic, and arithmetic does not care how long the board is.
        # ``_realise_window`` keeps roughly a viewport's worth either side of
        # what is visible and throws the rest away; ``_cards`` stays a list
        # of page_count entries with None where a card is not currently
        # built, so every index and bounds check in this file still reads
        # the way it did.
        self._pad = sc(10)
        # Tall enough to hold a "+ Page" row a finger can hit, between every
        # pair of cards. It was 12 px when the gaps were just spacing.
        self._gap = sc(46)
        self._card_h = 0            # measured from the first card built
        self._content = _CardStack(self)
        self._content.setStyleSheet("background: transparent;")

        self._scroll.setWidget(self._content)
        outer.addWidget(self._scroll)

        # Thumbnails are fetched for visible cards only (see
        # _render_thumbnails); scrolling brings more into view, so refill
        # then — debounced, because a touch flick emits a continuous stream
        # of valueChanged and each fetch can cost a page rasterise.
        self._thumb_timer = QTimer(self)
        self._thumb_timer.setSingleShot(True)
        self._thumb_timer.timeout.connect(self._render_thumbnails)
        self._scroll.verticalScrollBar().valueChanged.connect(self._on_scrolled)

        self.setFixedWidth(panel_w)

    def _on_add_clicked(self):
        click_sound.play_click()
        if self._on_add:
            self._on_add()
        self._rebuild()

    def _on_card_click(self, index: int):
        if self._on_goto:
            self._on_goto(index)
        self.close()

    def _on_card_delete(self, index: int):
        click_sound.play_click()
        thumbs = self._snapshot_thumbs()
        before = self._canvas.page_count
        if self._on_delete:
            self._on_delete(index)
        if self._canvas.page_count >= before:
            return                          # nothing was deleted
        # CRITICAL: after a page deletion every page-index from `index+1`
        # onward shifts down by one. The ThumbnailManager has its RAM and
        # disk caches keyed by index — without invalidating them, the
        # surviving cards display stale thumbnails (page 3's thumbnail
        # showing under what's now page 2, etc).
        # Clearing BOTH layers is the only reliable fix: the on-tick
        # renderer + lazy fetch will regenerate them on the next access.
        #
        # Only the pages from the deleted one on have moved; the ones before
        # it keep their previews. Throwing the whole cache away re-rendered
        # every preview on the board.
        if self._thumb_manager is not None:
            try:
                self._thumb_manager.invalidate(range(index, before))
            except Exception as e:
                logging.debug("thumb cache invalidate failed: %s", e)
        self._rebuild()
        # Carry the previews still on screen to the pages they now are, so
        # the column does not flash blank after every delete.
        self._restore_thumbs(
            thumbs, lambda j: None if j == index else (j - 1 if j > index else j))
        # The pages after it start where they were and slide up into the
        # space, rather than the column jumping — the reverse of an insert.
        for i, card in enumerate(self._cards):
            if card is not None and i >= index:
                card.move(card.x(), self._slot_y_abs(i + 1))
        self._layout_gaps()
        self._animate_cards()


    # ════════════════════════════════════════════════════════════════════
    # Drag to reorder
    # ════════════════════════════════════════════════════════════════════
    #
    # HOW IT WORKS, and why not the obvious way.
    #
    # The obvious way is to re-insert the card into the QVBoxLayout every
    # time the pointer crosses a boundary and let Qt re-lay it out. That is
    # a handful of lines and it looks terrible: the layout snaps, so pages
    # teleport past each other with no sense of being pushed aside, and on a
    # long board the re-layout cost is paid on every pointer sample.
    #
    # So for the duration of a drag the layout is switched OFF and the cards
    # are positioned by hand. Every card is the same height, so a slot index
    # is just arithmetic, and a single 60 Hz timer eases each card toward the
    # slot it currently belongs in. One timer for the whole list rather than
    # an animation per card: a fifty-page board would otherwise start fifty
    # QPropertyAnimations every time the pointer crossed a boundary, which is
    # exactly the lag this is meant to avoid.
    #
    # The card being dragged is not moved at all — a GHOST follows the
    # cursor instead. The ghost is a child of the panel rather than of the
    # scrolling content, so it floats above everything and does not get
    # clipped by the viewport when the drag reaches the edge.

    _SLIDE_EASE = 0.35          # fraction of the remaining gap per frame
    _LIFT = 1.04                # how much bigger the picked-up card is drawn
    _EDGE_ZONE_PX = 48          # how near the edge auto-scroll kicks in
    _EDGE_SPEED = 14            # pixels per frame at the very edge

    def _slot_geometry(self):
        """(top_y, slot_height) for the card stack, in content coords."""
        if not self._cards:
            return 0, 0
        return self._pad, self._slot_h()

    def _begin_drag(self, index: int, global_pos) -> None:
        if self._drag_src is not None or not (0 <= index < len(self._cards)):
            return
        if len(self._cards) < 2:
            return                      # nothing to reorder
        card = self._cards[index]
        if card is None:
            return          # not built, so not on screen, so never grabbed

        self._drag_src = index
        self._drag_to = index
        self._drag_top, self._drag_slot_h = self._slot_geometry()
        self._slide.stop()
        # The "+ Page" rows step aside while pages are moving: they would sit
        # in the very gaps the cards are sliding through.
        self._layout_gaps()

        for i, c in enumerate(self._cards):
            if c is not None:
                c.move(c.x(), self._slot_y(i))

        # The ghost: a picture of the card that is LIFTED off the stack —
        # it grows to _LIFT over a few frames, with a heavier shadow, so it
        # reads as picked up rather than as a copy sliding about. It starts
        # at exactly the card's size and place, so there is no jump.
        self._discard_drop_ghost()
        ghost = _Ghost(self, card)
        self._ghost_cw, self._ghost_ch = card.width(), card.height()
        top_left = card.mapTo(self, QPoint(0, 0))
        local = self.mapFromGlobal(global_pos)
        self._drag_grab_dy = local.y() - top_left.y()
        self._ghost_cx = top_left.x() + card.width() / 2.0
        self._ghost_cy = top_left.y() + card.height() / 2.0
        self._ghost_scale = 1.0
        self._ghost_scale_to = self._LIFT
        self._drag_ghost = ghost
        self._place_ghost(ghost)
        ghost.show()
        ghost.raise_()

        # The drop slot starts under the card it was lifted from.
        if self._drop_slot is None:
            self._drop_slot = _DropSlot(self._content)
        ds = self._drop_slot
        ds.setGeometry(card.x(), self._slot_y(index), card.width(), card.height())
        ds.lower()
        ds.show()
        self._animate_cards()

        # Take the gesture off the kinetic scroller for the duration.
        # Without this the drag dies on its first millimetre — QScroller is
        # watching the same viewport and claims the gesture as soon as the
        # pointer moves. See suspend_touch_scroll.
        from ui.util.touch_scroll import suspend_touch_scroll
        self._scroller_suspended = suspend_touch_scroll(self._scroll)

        # The card itself stays where it is but goes invisible, so the slot
        # it used to occupy is free for the others to slide into.
        #
        # Moved out of the way rather than hidden: hiding a widget drops the
        # implicit mouse grab it earned by being pressed, and the drag needs
        # to keep receiving moves and the release. Children are clipped to
        # their parent, so a card parked off to the left is just as invisible
        # and still very much alive.
        self._drag_park_x = card.x()
        card.move(-10 * max(1, card.width()), card.y())

    def _place_ghost(self, ghost) -> None:
        """Size and place a ghost from its centre and its scale. The
        ghost carries its shadow round the card, ``pad`` each side."""
        pad = getattr(ghost, "pad", 0)
        w = max(1, int(round((self._ghost_cw + 2 * pad) * self._ghost_scale)))
        h = max(1, int(round((self._ghost_ch + 2 * pad) * self._ghost_scale)))
        ghost.setGeometry(int(round(self._ghost_cx - w / 2.0)),
                          int(round(self._ghost_cy - h / 2.0)), w, h)

    def _ghost_card_top(self) -> int:
        """The dragged CARD's top edge (not the bigger ghost's), in content
        coords — what every slot calculation is measured from."""
        return self._content.mapFrom(
            self, QPoint(0, int(round(self._ghost_cy - self._ghost_ch / 2.0)))).y()

    def _slot_y(self, slot: int) -> int:
        return int(self._drag_top + slot * self._drag_slot_h)

    def _target_slot_for(self, card_index: int) -> int:
        """Where card ``card_index`` should sit while the drag is in
        progress — i.e. with the dragged page removed and re-inserted at
        ``_drag_to``."""
        src, dst = self._drag_src, self._drag_to
        if src is None or dst is None or card_index == src:
            return card_index
        if src < dst:
            return card_index - 1 if src < card_index <= dst else card_index
        return card_index + 1 if dst <= card_index < src else card_index

    def _target_y(self, card_index: int) -> int:
        """Where card ``card_index`` belongs RIGHT NOW: its drag position
        while a drag is running, its own slot otherwise.

        One answer for every code path that places a card. The relayout
        that runs on every scroll used to put each card back in its OWN
        slot, drag or no drag — so auto-scrolling during a drag snapped
        every card back over the gap the dragged page was meant to drop
        into, and nothing slid them out again.
        """
        if self._drag_src is not None:
            return self._slot_y(self._target_slot_for(card_index))
        return self._slot_y_abs(card_index)

    def _animate_cards(self) -> None:
        if not self._slide.isActive():
            self._last_step = None
            self._slide.start()

    def _frame_ease(self) -> float:
        """This frame's share of the remaining distance, from the time that
        has REALLY passed. A fixed share per tick made the motion only as
        smooth as the timer: a frame that ran late left the cards behind, and
        a busy moment turned a glide into a crawl and then a jump. Measured
        in time it covers the same ground per second however the frames
        fall. A tick that comes too soon still counts as one frame, so
        stepping it by hand (the tests do) moves at the design speed."""
        now = time.perf_counter()
        last = getattr(self, "_last_step", None)
        self._last_step = now
        frame = 1.0 / 60.0
        dt = frame if last is None else min(max(now - last, frame), 4 * frame)
        return 1.0 - (1.0 - self._SLIDE_EASE) ** (dt / frame)

    def _slide_step(self) -> None:
        """Ease every card toward where it belongs. One timer, whole list.

        Drives three motions with one rule: cards making room during a
        drag, the dropped page gliding into its slot, and the pages after an
        insert moving down to make space for the new one.
        """
        moving = False
        f = self._frame_ease()
        self._f = f
        for i, c in enumerate(self._cards):
            if c is None or i == self._drag_src:
                continue
            want = self._target_y(i)
            now = c.y()
            if now == want:
                continue
            step = (want - now) * f
            # Always close at least a pixel, or the last fraction of the
            # gap takes forever and the row never quite settles.
            if -1 < step < 1:
                step = 1 if want > now else -1
            nxt = now + int(step)
            if (step > 0 and nxt > want) or (step < 0 and nxt < want):
                nxt = want
            c.move(c.x(), nxt)
            moving = True
        moving |= self._step_drop_slot()
        moving |= self._step_ghost()
        # Stop as soon as everything has settled; a new target restarts it.
        if not moving:
            self._slide.stop()
            if self._drag_src is None:
                self._settled()

    def _ease(self, now: float, want: float, snap: float) -> float:
        """One frame of the same ease the cards use, landing exactly."""
        if abs(want - now) <= snap:
            return want
        return now + (want - now) * getattr(self, "_f", self._SLIDE_EASE)

    def _step_drop_slot(self) -> bool:
        ds = self._drop_slot
        if ds is None or not ds.isVisible() or self._drag_to is None:
            return False
        want = self._slot_y(self._drag_to)
        if ds.y() == want:
            return False
        ds.move(ds.x(), int(round(self._ease(ds.y(), want, 1.0))))
        return True

    def _step_ghost(self) -> bool:
        """Lift the dragging ghost to _LIFT; glide a dropped one home."""
        ghost = self._drag_ghost or self._drop_ghost
        if ghost is None:
            return False
        moving = False
        if self._ghost_scale != self._ghost_scale_to:
            self._ghost_scale = self._ease(self._ghost_scale,
                                           self._ghost_scale_to, 0.003)
            moving = True
        if self._drop_ghost is not None:
            home = self._drop_home()
            if home is None:
                self._discard_drop_ghost()
                return moving
            cx, cy = home
            if (cx, cy) != (self._ghost_cx, self._ghost_cy):
                self._ghost_cx = self._ease(self._ghost_cx, cx, 0.5)
                self._ghost_cy = self._ease(self._ghost_cy, cy, 0.5)
                moving = True
        if moving:
            self._place_ghost(ghost)
        return moving

    def _drop_home(self):
        """Centre of the slot the dropped page landed in, panel coords."""
        land = self._drop_land
        if land is None or not (0 <= land < len(self._cards)):
            return None
        card = self._cards[land]
        if card is None:
            return None
        tl = self._content.mapTo(self, QPoint(card.x(), self._slot_y_abs(land)))
        return (tl.x() + card.width() / 2.0, tl.y() + card.height() / 2.0)

    def _discard_drop_ghost(self) -> None:
        if self._drop_ghost is not None:
            self._drop_ghost.hide()
            self._drop_ghost.deleteLater()
            self._drop_ghost = None
        self._drop_land = None

    def _animating(self) -> bool:
        return self._slide.isActive() or self._drag_src is not None

    def _settled(self) -> None:
        """Everything is where it belongs: show what was held back."""
        self._discard_drop_ghost()
        QTimer.singleShot(0, self._render_thumbnails)
        for i in sorted(self._reveal_after):
            if 0 <= i < len(self._cards) and self._cards[i] is not None:
                self._cards[i].show()
        self._reveal_after = set()
        self._layout_gaps()

    def _retarget_from_ghost(self) -> None:
        """Recompute which slot the dragged card is over, and start the slide.

        Measured from the CARD'S TOP EDGE, so the answer is symmetric: the
        card moves to the next slot when it has travelled half a slot, up or
        down. It used to be measured from the card's centre and rounded —
        but the centre already sits ~half a card below the slot's top, so a
        7 px nudge DOWN jumped a slot while going UP took more than half a
        card. The cards lurched one way and dragged their feet the other.

        Called for two reasons: the pointer moves, or the list scrolls
        underneath a pointer holding still at the edge.
        """
        if self._drag_src is None or self._drag_ghost is None:
            return
        top_in_content = self._ghost_card_top()
        slot = int(round((top_in_content - self._drag_top)
                         / max(1, self._drag_slot_h)))
        slot = max(0, min(slot, len(self._cards) - 1))
        if slot != self._drag_to:
            self._drag_to = slot
            self._animate_cards()

    def _drag_moved(self, global_pos) -> None:
        if self._drag_src is None or self._drag_ghost is None:
            return
        local = self.mapFromGlobal(global_pos)
        self._ghost_cy = local.y() - self._drag_grab_dy + self._ghost_ch / 2.0
        self._place_ghost(self._drag_ghost)
        self._retarget_from_ghost()

        # Auto-scroll when the ghost is held near an edge, so a page can be
        # dragged further than one screenful.
        vp = self._scroll.viewport()
        p_in_vp = vp.mapFrom(self, local).y()
        if p_in_vp < self._EDGE_ZONE_PX:
            self._edge_dir = -1
        elif p_in_vp > vp.height() - self._EDGE_ZONE_PX:
            self._edge_dir = 1
        else:
            self._edge_dir = 0
        if self._edge_dir and not self._edge_scroll.isActive():
            self._edge_scroll.start()
        elif not self._edge_dir and self._edge_scroll.isActive():
            self._edge_scroll.stop()

    def _edge_scroll_step(self) -> None:
        if self._drag_src is None or not self._edge_dir:
            self._edge_scroll.stop()
            return
        bar = self._scroll.verticalScrollBar()
        before = bar.value()
        bar.setValue(before + self._edge_dir * self._EDGE_SPEED)
        if bar.value() == before:
            self._edge_scroll.stop()
            return
        # The cards just moved under a ghost that did not. Ask again which
        # slot it is over, or the drop target stays where the scroll started.
        self._retarget_from_ghost()

    def _end_drag(self, global_pos) -> None:
        """Commit the move AT ONCE — the board is reordered the moment the
        page is let go — and then animate the landing: the ghost shrinks
        back to card size while gliding into the slot the page now has, and
        the real card appears under it when it gets there."""
        src, dst = self._drag_src, self._drag_to
        ghost = self._drag_ghost
        # Keep the ghost: _finish_drag_visuals would throw it away.
        self._drag_ghost = None
        thumbs = self._snapshot_thumbs()
        self._finish_drag_visuals()
        moved = (src is not None and dst is not None and src != dst
                 and self._on_move is not None)
        if moved:
            self._on_move(src, dst)
        self._rebuild()

        def new_index(j):
            if not moved:
                return j
            if j == src:
                return dst
            if src < dst and src < j <= dst:
                return j - 1
            if dst < src and dst <= j < src:
                return j + 1
            return j
        self._restore_thumbs(thumbs, new_index)

        land = dst if moved else src
        if (ghost is not None and land is not None
                and 0 <= land < len(self._cards)
                and self._cards[land] is not None):
            self._drop_ghost = ghost
            self._drop_land = land
            self._ghost_scale_to = 1.0
            ghost.raise_()
            card = self._cards[land]
            card.hide()
            self._reveal_after.add(land)
            self._layout_gaps()
            self._animate_cards()
        elif ghost is not None:
            ghost.hide()
            ghost.deleteLater()

    def _finish_drag_visuals(self) -> None:
        """Put the panel back to rest.

        Separate from _end_drag because it must also run when a drag is
        abandoned — the panel closing, a rebuild arriving from elsewhere.
        """
        self._slide.stop()
        self._edge_scroll.stop()
        self._edge_dir = 0
        if self._drag_ghost is not None:
            self._drag_ghost.hide()
            self._drag_ghost.deleteLater()
            self._drag_ghost = None
        if self._drop_slot is not None:
            self._drop_slot.hide()
        if self._drag_src is not None and 0 <= self._drag_src < len(self._cards):
            card = self._cards[self._drag_src]
            if card is not None:
                card.setVisible(True)
                if self._drag_park_x is not None:
                    card.move(self._drag_park_x, card.y())
        self._drag_park_x = None
        if self._scroller_suspended:
            from ui.util.touch_scroll import resume_touch_scroll
            resume_touch_scroll(self._scroll)
            self._scroller_suspended = False
        self._drag_src = None
        self._drag_to = None
        # Put the stack back the way the scroll position says it should be:
        # the drag may have scrolled a long way, so cards that were dropped
        # while it ran need building before anything is repositioned.
        self._realise_window()

    # ── carrying previews across a change ──────────────────────────────
    #
    # A move, an insert or a delete rebuilds the column, and a rebuilt card
    # starts with no picture. The previews then refilled one by one — so
    # every drop made the whole column flash blank, which is a large part of
    # why reordering looked broken. The pictures still on screen are carried
    # across to the pages they now belong to; the thumbnail manager then
    # refreshes them in the background as it always did.

    def _snapshot_thumbs(self) -> dict:
        snap = {}
        for i, card in enumerate(self._cards):
            if card is not None and card.has_thumbnail():
                try:
                    snap[i] = card._thumb.pixmap()
                except Exception:
                    pass
        return snap

    def _restore_thumbs(self, snap: dict, new_index_of) -> None:
        for old, pm in snap.items():
            ni = new_index_of(old)
            if ni is None or not (0 <= ni < len(self._cards)):
                continue
            card = self._cards[ni]
            if card is not None and not card.has_thumbnail():
                card.adopt_thumbnail(pm)

    # ════════════════════════════════════════════════════════════════════
    # Insert a page between two others
    # ════════════════════════════════════════════════════════════════════

    def _insert_index_at(self, content_y: int) -> int:
        """Which page index a click at ``content_y`` would insert BEFORE.

        Measured against card midpoints rather than against the gaps, so
        the answer is defined everywhere in the column — a right-click that
        lands a pixel inside a card still means something sensible instead
        of nothing at all.
        """
        # Arithmetic, not a walk over the cards: most of them do not exist.
        # A click below the last card means "at the end", which is what the
        # clamp at the bottom gives.
        n = len(self._cards)
        if n == 0:
            return 0
        slot = self._slot_h()
        rel = content_y - self._pad + (slot - self._card_h // 2)
        return max(0, min(n, int(rel // slot)))

    def _page_index_at(self, content_y: int) -> int:
        """Which PAGE the point ``content_y`` is over, 0-based.

        Distinct from ``_insert_index_at``, which answers "between which two
        pages". This one answers "which card did the user hit", and the menu
        needs that one: every action it offers is expressed relative to the
        page that was actually pointed at, not to the current selection
        and not to the nearest gap.

        A click in the gap between two cards belongs to the card above it,
        so the answer is defined everywhere in the column rather than only
        on the cards themselves.
        """
        n = len(self._cards)
        if n == 0:
            return 0
        i = (int(content_y) - self._pad) // self._slot_h()
        return max(0, min(n - 1, int(i)))

    def _on_gap_insert(self, at: int) -> None:
        """A "+ Page" row was tapped."""
        click_sound.play_click()
        self._insert_and_refresh(at)

    def _insert_and_refresh(self, at: int) -> None:
        """Insert through the app's own page machinery, then re-read.

        ``_on_insert`` is MainWindow._insert_page, which grows the board,
        throws away the previews from the insertion point on, marks the
        board dirty and updates the page counter. Nothing about creating a
        page is reimplemented here.

        Then, so it behaves the way inserting a page is expected to:

        * the new page is OPENED — you insert a page to write on it — and
          the panel stays up, with the new card marked current, so you can
          see exactly where it landed;
        * the pages after it slide down to make room, and the new card
          appears in the space they leave, instead of the column jumping;
        * the previews stay on their pages (carried across, not refetched).
        """
        at = int(at)
        before = self._canvas.page_count
        thumbs = self._snapshot_thumbs()
        self._on_insert(at)
        if self._canvas.page_count <= before:
            self._rebuild()                 # nothing was inserted
            return
        at = max(0, min(at, self._canvas.page_count - 1))
        if self._on_goto is not None:
            try:
                self._on_goto(at)
            except Exception:
                logging.exception("could not open the inserted page")
        self._rebuild()
        self._restore_thumbs(thumbs, lambda j: j if j < at else j + 1)
        for i, card in enumerate(self._cards):
            if card is None:
                continue
            if i > at:
                card.move(card.x(), self._slot_y_abs(i - 1))
            elif i == at:
                card.hide()
                self._reveal_after.add(i)
        self._layout_gaps()
        self._animate_cards()

    # No right-click menu. Everything it offered is on the cards now — the
    # "+ Page" rows insert, the grip reorders — and on a touch board a
    # press-and-hold IS a right-click, so the menu kept popping up over the
    # very long press that picks a card up.

    def _rebuild(self):
        """Rebuild all page cards."""
        # A rebuild arriving mid-drag (a background finishing, a page
        # deleted elsewhere) has to put the layout back first, or the list
        # stays frozen with the layout disabled.
        if self._drag_src is not None:
            self._finish_drag_visuals()
        # Nothing in the stack keeps the keyboard focus through a rebuild:
        # hiding a focused card makes Qt hand the focus on, and the scroll
        # area then scrolls to wherever it went. See PageCard's bin.
        fw = QApplication.focusWidget()
        if fw is not None and self._content.isAncestorOf(fw):
            fw.clearFocus()
        self._rebuilding = True
        try:
            self._rebuild_cards()
        finally:
            self._rebuilding = False
        self._realise_window()

        # Generate thumbnails lazily after layout settles
        QTimer.singleShot(50, self._render_thumbnails)

    def _rebuild_cards(self) -> None:
        """The part of a rebuild that must not be interrupted: a scroll
        landing in the middle of it would realise the window against a
        half-cleared card list."""
        # Invalidate any in-flight incremental fill — its queued indices point
        # at cards that are about to be deleted.
        self._fill_gen += 1
        self._fill_queue = []
        # Clear old cards
        # Old cards become spares rather than rubbish — a rebuild after an
        # insert re-uses almost all of them.
        for card in self._cards:
            if card is not None:
                card.hide()
                self._card_pool.append(card)
        self._cards.clear()

        canvas = self._canvas
        total = canvas.page_count
        current = canvas.current_page

        # One slot per page, all empty. Cards get built as they come into
        # view; see _realise_window.
        self._cards = [None] * total
        self._reveal_after = set()
        self._current_index = current
        self._resize_content()

    # ── virtualisation ──────────────────────────────────────────────
    #
    # Everything below assumes the stack is a single column of identical
    # cards, which it is. That makes "where does card i sit" and "which
    # cards are on screen" pure arithmetic, answerable without building
    # anything — which is the whole point.

    def _slot_h(self) -> int:
        """Height of one card plus the gap under it."""
        return max(1, self._card_h + self._gap)

    def _measure_card(self) -> None:
        """Learn how tall a card is, by building one and asking.

        Done once per panel. The alternative is a constant, and a constant
        would be wrong the moment the card's contents or the UI scale
        change — which is exactly the kind of number that goes stale
        silently and puts every card a few pixels out.
        """
        if self._card_h:
            return
        # ``total`` matters: the card shows "3 / 800", so a probe built with
        # total=1 is narrower text and a different height — measured, 159 px
        # against the 170 a real card wants. Getting that wrong puts every
        # card in the stack 11 px out and moves the drop slot a drag lands
        # in, which is exactly how it was first caught.
        total = max(1, len(self._cards) or self._canvas.page_count)
        probe = PageCard(index=0, total=total, is_current=False,
                         on_click=lambda *_: None,
                         on_delete=lambda *_: None, parent=self._content)
        # minimumHeight() too: the card is setFixedSize, which a layout
        # hint does not report — the hint said 170 while every card was
        # really 175, so each card overlapped the gap below it by 5 px and
        # every slot in a drag was 5 px further out than the last.
        self._card_h = max(1, probe.sizeHint().height(),
                           probe.minimumSizeHint().height(),
                           probe.minimumHeight())
        probe.setParent(None)
        probe.deleteLater()

    def _resize_content(self) -> None:
        """Give the stack the height the WHOLE board would need.

        The scrollbar's range comes from this, so it has to account for
        every page whether or not that page has a card right now —
        otherwise the list would appear to be only as long as the part of
        it that happens to be built.
        """
        self._measure_card()
        n = len(self._cards)
        h = self._pad * 2 + max(0, n * self._card_h + max(0, n - 1) * self._gap)
        self._content.setFixedHeight(int(h))

    def _visible_range(self) -> tuple:
        """(first, last) page index worth having a card for.

        A viewport's worth of margin either side, so a flick lands on cards
        that are already there rather than on holes.
        """
        n = len(self._cards)
        if n == 0:
            return 0, -1
        slot = self._slot_h()
        top = self._scroll.verticalScrollBar().value()
        vph = max(1, self._scroll.viewport().height())
        first = max(0, int((top - vph - self._pad) // slot))
        last = min(n - 1, int((top + 2 * vph - self._pad) // slot))
        return first, last

    def _realise_window(self) -> None:
        """Build the cards inside the visible window, drop the rest."""
        if not self._cards:
            return
        first, last = self._visible_range()
        total = len(self._cards)
        current = getattr(self, "_current_index", -1)

        # Never destroy the card being dragged: the gesture holds it, and
        # scrolling to the edge is exactly when it would fall outside.
        keep = self._drag_src

        self._content.setUpdatesEnabled(False)
        try:
            # Cards that fall out of the window go to a POOL rather than to
            # deleteLater, and the pages that just came in take them over.
            #
            # A far jump replaces the entire window at once, so without this
            # it destroys a screenful of widgets and constructs another —
            # measured at 1000 pages, 54 ms to reach the far end and 92 ms
            # to reach an arbitrary page, both well past a frame. A small
            # scroll was always fine because most of the cards survive it.
            # Re-labelling a card is a few property writes against 1.35 ms
            # to build one.
            #
            # The pool persists BETWEEN calls, which matters more than it
            # looks: the window is smaller at the ends of the board, where
            # there is no margin past page 1 or the last page, so a pool
            # that was emptied each time threw away the surplus on arriving
            # at an end and had to build it again on the way back.
            pool = self._card_pool
            for i, card in enumerate(self._cards):
                if card is None or first <= i <= last or i == keep:
                    continue
                pool.append(card)
                self._cards[i] = None

            for i in range(first, last + 1):
                if self._cards[i] is not None:
                    continue
                if pool:
                    card = pool.pop()
                    card.rebind(i, total, i == current)
                else:
                    card = PageCard(
                        index=i,
                        total=total,
                        is_current=(i == current),
                        on_click=self._on_card_click,
                        on_delete=self._on_card_delete,
                        parent=self._content,
                        on_drag_start=self._begin_drag,
                        on_drag_move=self._drag_moved,
                        on_drag_end=self._end_drag,
                    )
                self._cards[i] = card
                if i not in self._reveal_after:
                    card.show()

            # Keep spares up to the BIGGEST window this panel has needed,
            # not the current one. The window is smaller at the ends of the
            # board — no margin past page 1 or the last page — so trimming
            # to the current span discarded precisely the cards the next
            # jump back to the middle would ask for: measured, ten
            # constructions across four far jumps that need none once the
            # cap stops shrinking. The cap is a window's worth of widgets,
            # about sixteen, so it is bounded however long the board is.
            span = max(1, last - first + 1)
            self._pool_cap = max(getattr(self, "_pool_cap", 0), span)
            # Hidden: a pooled card is still parented to the stack and would
            # otherwise paint wherever it was last positioned.
            for card in pool:
                card.hide()
            while len(pool) > self._pool_cap:
                card = pool.pop()
                card.setParent(None)
                card.deleteLater()
            self._realise_gaps(first, last)
            self._relayout_cards()
        finally:
            self._content.setUpdatesEnabled(True)

    def _relayout_cards(self) -> None:
        """Put every built card where it belongs.

        "Where it belongs" is ``_target_y``: its drag position during a
        drag, its own slot otherwise. While cards are mid-slide only their
        width and x are fixed here — snapping their y would cut every
        animation short each time the list scrolled.
        """
        if not self._cards or not self._card_h:
            return
        w = max(1, self._content.width() - self._pad * 2)
        animating = self._slide.isActive()
        far = 2 * self._slot_h()
        for i, card in enumerate(self._cards):
            if card is None:
                continue
            # The dragged card is parked out of the stack on purpose.
            if self._drag_src is not None and i == self._drag_src:
                card.resize(w, self._card_h)
                continue
            want = self._target_y(i)
            y = card.y()
            # A card a long way off is a recycled one carrying its old
            # position, not one mid-slide: put it straight where it goes.
            if not animating or abs(y - want) > far:
                y = want
            card.setGeometry(self._pad, y, w, self._card_h)
        self._layout_gaps(w)

    # ── the "+ Page" rows ──────────────────────────────────────────────

    def _realise_gaps(self, first: int, last: int) -> None:
        """Build the "+ Page" rows for the gaps beside the built cards —
        virtualised and pooled exactly like the cards themselves."""
        n = len(self._cards)
        if self._on_insert is None or n < 2:
            want = set()
        else:
            want = set(range(max(0, first), min(last, n - 2) + 1))
        for i in list(self._gaps):
            if i not in want:
                g = self._gaps.pop(i)
                g.hide()
                self._gap_pool.append(g)
        for i in sorted(want):
            g = self._gaps.get(i)
            if g is None:
                g = self._gap_pool.pop() if self._gap_pool else \
                    _InsertGap(self._content, self._on_gap_insert)
                self._gaps[i] = g
            g.bind(i + 1, n)
        cap = max(4, getattr(self, "_pool_cap", 0))
        while len(self._gap_pool) > cap:
            g = self._gap_pool.pop()
            g.setParent(None)
            g.deleteLater()

    def _gaps_wanted(self) -> bool:
        """The rows show only when the column is at rest: during a drag or
        a slide they would sit in the very gaps the cards move through."""
        return (self._on_insert is not None and self._drag_src is None
                and not self._slide.isActive())

    def _layout_gaps(self, w: Optional[int] = None) -> None:
        if w is None:
            w = max(1, self._content.width() - self._pad * 2)
        show = self._gaps_wanted()
        for i, g in self._gaps.items():
            g.setGeometry(self._pad, self._slot_y_abs(i) + self._card_h, w, self._gap)
            g.setVisible(show)
            if show:
                g.raise_()

    def _slot_y_abs(self, i: int) -> int:
        return int(self._pad + i * self._slot_h())

    def _render_thumbnails(self):
        """Populate thumbnails for the cards that are ACTUALLY ON SCREEN.

        ``ThumbnailManager.get_thumbnail`` is RAM -> disk -> render-on-demand,
        and it only keeps the current page +/-5 in RAM. This used to loop over
        every card, so opening the panel on a 100-page board fired ~90
        render-on-demand rasterises back to back on the UI thread — which is
        exactly what "Set Whole Media As Bg" now produces from a 100-page PDF.
        The module docstring always claimed this was viewport-limited; now it
        actually is.

        A one-screen margin above and below keeps a flick from landing on
        blank cards.
        """
        canvas = self._canvas
        if not HAS_ENGINE or canvas.board is None:
            return
        if self._thumb_manager is None:
            return

        if not self._cards:
            return
        # A preview can take tens of ms to rasterise, on this thread. Done in
        # the middle of a slide it froze the cards for that long, every
        # time. _settled() asks again the moment everything has stopped.
        if self._animating():
            return

        # The same window the cards themselves are built for — asking for a
        # thumbnail on a page with no card would render a page nobody can
        # see. It is arithmetic rather than a read of each card's y(): this
        # runs right after a rebuild, before the scroll area has resized the
        # stack, when a pixel test would say every card is on screen.
        first, last = self._visible_range()

        queue: list[int] = []
        for i in range(int(first), int(last) + 1):
            card = self._cards[i]
            if card is None:
                continue
            # "Already filled" is not the same as "still correct". A card
            # keeps whatever it was first given, so without the dirty check a
            # preview the manager KNOWS is stale (media deleted off that page,
            # background changed while you were elsewhere) would sit there
            # unchanged for as long as the panel stays open.
            if card.has_thumbnail() and not self._is_stale(i):
                continue
            queue.append(i)
        self._fill_queue = queue
        self._pump_thumbnails()

    def _is_stale(self, page_index: int) -> bool:
        mgr = self._thumb_manager
        if mgr is None or not hasattr(mgr, "is_dirty"):
            return False
        try:
            return bool(mgr.is_dirty(page_index))
        except Exception:
            return False

    def _pump_thumbnails(self):
        """Fill ONE queued card, then hand control back to the event loop.

        Each fetch can be a full page rasterise (tens of ms, more when it has
        to decode a 4K slide background), so doing the whole viewport in one
        synchronous loop froze the panel for as long as it took — exactly the
        moment the user is flicking through pages. One per turn means the
        cards fill in visibly, scrolling and taps stay responsive throughout,
        and the total work is identical.
        """
        if not self._fill_queue:
            return
        if self._animating():
            return              # resumed by _settled(), see _render_thumbnails
        gen = self._fill_gen
        i = self._fill_queue.pop(0)
        if 0 <= i < len(self._cards) and self._thumb_manager is not None:
            try:
                card = self._cards[i]
                pm = self._thumb_manager.get_thumbnail(i) \
                    if card is not None else None
                if pm is not None and not pm.isNull():
                    card.set_thumbnail(pm)
            except Exception as e:
                logging.debug(f"Thumbnail fetch failed for page {i}: {e}")
        # A _rebuild() during the pump replaces every card, so anything still
        # queued refers to widgets that no longer exist. The generation stamp
        # cancels this chain; the rebuild starts its own.
        if self._fill_queue and gen == self._fill_gen and self.isVisible():
            QTimer.singleShot(0, self._pump_thumbnails)

    def refresh_previews(self, pages=None) -> None:
        """Re-pull previews for cards on screen — for when something OUTSIDE
        this panel changed the pages (e.g. media deleted in the Media
        Manager). Cards the manager considers stale are re-fetched; the rest
        are left alone, so this is nearly free when nothing actually changed.
        """
        if pages is not None and self._thumb_manager is not None:
            try:
                self._thumb_manager.invalidate(pages)
            except Exception as e:
                logging.debug("thumb invalidate failed: %s", e)
        self._render_thumbnails()

    def _on_scrolled(self, _value=None):
        """Two jobs at two different cadences.

        Building the cards that just came into view happens NOW: they are a
        millisecond each and skipping a frame of them means scrolling into
        blank space.

        Fetching their thumbnails is coalesced, because each one can be a
        full page rasterise and a touch flick emits a continuous stream of
        valueChanged — one fetch per pixel of travel would stall the flick
        it is trying to illustrate.
        """
        if getattr(self, "_rebuilding", False):
            return          # _rebuild realises the window itself when done
        self._realise_window()
        self._thumb_timer.start(80)

    def showEvent(self, event):
        super().showEvent(event)
        apply_aero(self)
        fade_in(self)
        enable_shadow(self)

    def show_right_side(self):
        """Show the panel anchored to the right edge of the parent, full height."""
        self._rebuild()

        parent = self.parent()
        if parent:
            pg = parent.geometry()
            screen_h = pg.height()
            pw = self.width()
            # Position at the right edge of the parent
            gp = parent.mapToGlobal(QPoint(pg.width() - pw, 0))
            self.setFixedHeight(screen_h)
            self.move(gp)
        else:
            screen = QApplication.primaryScreen()
            if screen:
                sg = screen.geometry()
                self.setFixedHeight(sg.height())
                self.move(sg.width() - self.width(), 0)

        self.show()

        # Scroll to current page card
        current = self._canvas.current_page
        # Deferred until the layout has settled — but skipped if the list
        # was scrolled in the meantime: a finger that started flicking the
        # moment the panel opened must not be yanked back to this page.
        if 0 <= current < len(self._cards):
            bar = self._scroll.verticalScrollBar()
            opened_at = bar.value()
            # ``self`` as the context: the panel is deleted on close, and a
            # bare lambda would still fire into it.
            QTimer.singleShot(100, self, lambda: (
                bar.value() == opened_at and self._scroll_to_card(current)))

    def _scroll_to_card(self, index: int):
        """Bring page ``index`` into view.

        By the slot's position rather than by handing the scroll area a
        widget: the whole point of virtualising is that a page far down the
        board has no widget yet, and ensureWidgetVisible cannot scroll to
        something that does not exist. Arithmetic works for every page,
        built or not, and building follows from the scroll.
        """
        if not (0 <= index < len(self._cards)):
            return
        y = self._slot_y_abs(index)
        vph = max(1, self._scroll.viewport().height())
        bar = self._scroll.verticalScrollBar()
        # A little above centre, matching the 50 px margin the old call asked
        # for: the page you jumped to should not sit against the top edge.
        bar.setValue(max(0, min(bar.maximum(),
                                y - max(50, (vph - self._card_h) // 3))))
        self._realise_window()
        self._render_thumbnails()