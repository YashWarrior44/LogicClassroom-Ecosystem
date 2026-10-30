"""
animations.py — Fluent motion primitives.

Every helper uses durations + easing curves sourced from the active
ThemeTokens, matching Microsoft's motion specification:

    duration_fast    : 120 ms  — micro-interactions (hover-in, focus ring)
    duration_default : 200 ms  — standard (button press, panel pop)
    duration_slow    : 320 ms  — major (dialog open, theme switch)

Public API
──────────
    fade_in(widget, ms=None)           — opacity 0 → 1
    scale_in(widget, ms=None)          — pop-in with subtle overshoot
    hover_lift(widget, shadow_fx)      — animates shadow blur on hover
    press_squish(widget)               — micro-scale on press
"""
from __future__ import annotations

from typing import Optional

from PySide6.QtCore    import (Qt, QObject, QEvent, QPoint, QPointF, QRect,
                                QEasingCurve, QPropertyAnimation,
                                QAbstractAnimation, QParallelAnimationGroup)
from PySide6.QtGui     import QColor
from PySide6.QtWidgets import (QWidget, QGraphicsOpacityEffect,
                                QGraphicsDropShadowEffect)

from ui.theme.theme_manager import theme_mgr

__all__ = [
    "fade_in", "scale_in", "hover_lift", "press_squish",
    "FADE_IN_MS", "FADE_IN_EASING",
]

# Legacy aliases (existing callsites reference these).
FADE_IN_MS:     int = 180
FADE_IN_EASING: QEasingCurve.Type = QEasingCurve.Type.OutCubic


# ─────────────────────────────────────────────────────────────────────────────
# Helpers
# ─────────────────────────────────────────────────────────────────────────────

def _ms(custom: Optional[int], default_attr: str = "duration_default") -> int:
    if custom is not None:
        return max(1, custom)
    return getattr(theme_mgr.tokens, default_attr, 200)


def _easing(curve: QEasingCurve.Type = QEasingCurve.Type.OutCubic) -> QEasingCurve:
    return QEasingCurve(curve)


# ─────────────────────────────────────────────────────────────────────────────
# fade_in
# ─────────────────────────────────────────────────────────────────────────────

def fade_in(widget: QWidget, duration_ms: Optional[int] = None) -> None:
    """Animate `widget` from opacity 0 → 1.

    * Top-level windows: animates ``windowOpacity`` (DWM-friendly).
    * Child widgets: applies a transient ``QGraphicsOpacityEffect``.
    """
    if widget is None:
        return
    ms = _ms(duration_ms, "duration_default")

    is_top_level = widget.isWindow()
    if is_top_level:
        anim = QPropertyAnimation(widget, b"windowOpacity", widget)
        anim.setDuration(ms)
        anim.setStartValue(0.0)
        anim.setEndValue(1.0)
        anim.setEasingCurve(_easing(QEasingCurve.Type.OutCubic))
        anim.start(QAbstractAnimation.DeleteWhenStopped)
        return

    fx = widget.graphicsEffect()
    if not isinstance(fx, QGraphicsOpacityEffect):
        fx = QGraphicsOpacityEffect(widget)
        widget.setGraphicsEffect(fx)
    fx.setOpacity(0.0)
    anim = QPropertyAnimation(fx, b"opacity", widget)
    anim.setDuration(ms)
    anim.setStartValue(0.0)
    anim.setEndValue(1.0)
    anim.setEasingCurve(_easing(QEasingCurve.Type.OutCubic))
    anim.start(QAbstractAnimation.DeleteWhenStopped)


# ─────────────────────────────────────────────────────────────────────────────
# scale_in — Fluent "pop-in" used for panels, dialogs, context menus
# ─────────────────────────────────────────────────────────────────────────────

def scale_in(widget: QWidget,
             duration_ms: Optional[int] = None,
             *,
             start_scale: float = 0.96) -> None:
    """Open a panel with a subtle scale + fade. Mimics Fluent's panel pop.

    Implemented as a geometry animation (resize + reposition around the
    widget's centre) because Qt 6 doesn't expose a proper layer-scale
    transform for stylesheeted widgets. The motion is small (4 %), so the
    geometry interpolation is imperceptible visually but reads as 'pop'.
    """
    if widget is None:
        return
    ms = _ms(duration_ms, "duration_default")

    # Cache the final geometry; we'll animate FROM a slightly-smaller box.
    final = widget.geometry()
    cx, cy = final.center().x(), final.center().y()
    w, h   = final.width(), final.height()

    # A widget cannot be made smaller than its layout's minimum. Panels here
    # are sized by adjustSize(), which lands them AT (or very near) that
    # minimum — so the naive 0.96x start box is illegal, Qt refuses every
    # frame, and the result is no animation plus a
    #   "QWindowsWindow::setGeometry: Unable to set geometry ..."
    # warning per frame in the log.
    #
    # We do NOT try to predict the minimum. minimumSizeHint() is unreliable
    # for any layout containing a word-wrapped label: its height depends on
    # its width, so the reported minimum drifts from the one actually
    # enforced (the pen panel reported 659 and enforced 674). Instead, ASK:
    # set the start box, read back what Qt granted, and believe the answer.
    # If the size came back different the widget is already at its floor —
    # restore it and fade instead. A fade that plays beats a pop that doesn't.
    sw, sh = int(w * start_scale), int(h * start_scale)
    initial = QRect(cx - sw // 2, cy - sh // 2, sw, sh)

    widget.setGeometry(initial)
    if widget.size() != initial.size():
        widget.setGeometry(final)
        fade_in(widget, ms)
        return
    # The start box was accepted, so every frame from here to `final` is at
    # least as large and therefore also legal. OutBack only overshoots ABOVE
    # the end value, never below the start, so no frame can dip under it.

    geom = QPropertyAnimation(widget, b"geometry", widget)
    geom.setDuration(ms)
    geom.setStartValue(initial)
    geom.setEndValue(final)
    geom.setEasingCurve(_easing(QEasingCurve.Type.OutBack))   # subtle overshoot

    # Parallel fade
    if widget.isWindow():
        widget.setWindowOpacity(0.0)
        opac = QPropertyAnimation(widget, b"windowOpacity", widget)
        opac.setStartValue(0.0); opac.setEndValue(1.0)
    else:
        fx = widget.graphicsEffect()
        if not isinstance(fx, QGraphicsOpacityEffect):
            fx = QGraphicsOpacityEffect(widget)
            widget.setGraphicsEffect(fx)
        fx.setOpacity(0.0)
        opac = QPropertyAnimation(fx, b"opacity", widget)
        opac.setStartValue(0.0); opac.setEndValue(1.0)
    opac.setDuration(ms)
    opac.setEasingCurve(_easing(QEasingCurve.Type.OutCubic))

    group = QParallelAnimationGroup(widget)
    group.addAnimation(geom)
    group.addAnimation(opac)
    group.start(QAbstractAnimation.DeleteWhenStopped)


# ─────────────────────────────────────────────────────────────────────────────
# hover_lift — animate a shadow on hover (use after apply_shadow)
# ─────────────────────────────────────────────────────────────────────────────

class _HoverLiftFilter(QObject):
    """Event filter that animates a card's drop shadow blur/offset on hover."""

    def __init__(self, widget: QWidget, fx: QGraphicsDropShadowEffect):
        super().__init__(widget)
        self._w  = widget
        self._fx = fx
        t = theme_mgr.tokens
        self._rest_blur, _, self._rest_dy, self._rest_alpha = t.elev_rest
        self._hov_blur,  _, self._hov_dy,  self._hov_alpha  = t.elev_hover

    def eventFilter(self, obj, event):
        if obj is not self._w:
            return False
        et = event.type()
        if et == QEvent.Enter:
            self._animate_to(self._hov_blur, self._hov_dy, self._hov_alpha)
        elif et == QEvent.Leave:
            self._animate_to(self._rest_blur, self._rest_dy, self._rest_alpha)
        return False

    def _animate_to(self, blur: int, dy: int, alpha: int):
        ms = _ms(None, "duration_fast")
        a1 = QPropertyAnimation(self._fx, b"blurRadius", self._w)
        a1.setDuration(ms)
        a1.setStartValue(self._fx.blurRadius())
        a1.setEndValue(blur)
        a1.setEasingCurve(_easing(QEasingCurve.Type.OutCubic))

        a2 = QPropertyAnimation(self._fx, b"yOffset", self._w)
        a2.setDuration(ms)
        a2.setStartValue(self._fx.yOffset())
        a2.setEndValue(dy)
        a2.setEasingCurve(_easing(QEasingCurve.Type.OutCubic))

        # Colour alpha
        old = self._fx.color()
        new = QColor(old.red(), old.green(), old.blue(), alpha)
        a3  = QPropertyAnimation(self._fx, b"color", self._w)
        a3.setDuration(ms)
        a3.setStartValue(old)
        a3.setEndValue(new)
        a3.setEasingCurve(_easing(QEasingCurve.Type.OutCubic))

        for a in (a1, a2, a3):
            a.start(QAbstractAnimation.DeleteWhenStopped)


def hover_lift(widget: QWidget, fx: QGraphicsDropShadowEffect) -> None:
    """Install a hover handler that animates ``fx``'s shadow on enter/leave.

    Usage:
        from ui.util.shadows import apply_shadow
        from ui.util.animations import hover_lift
        fx = apply_shadow(my_card, depth="rest")
        hover_lift(my_card, fx)
    """
    if widget is None or fx is None:
        return
    flt = _HoverLiftFilter(widget, fx)
    widget.installEventFilter(flt)
    # Keep a reference on the widget so GC doesn't drop it.
    widget._hover_lift_filter = flt


# ─────────────────────────────────────────────────────────────────────────────
# press_squish — micro-scale on mouse press for tactile feel
# ─────────────────────────────────────────────────────────────────────────────

class _PressSquishFilter(QObject):
    """Triggers a tiny scale-down animation on press, scale-up on release."""

    def __init__(self, widget: QWidget):
        super().__init__(widget)
        self._w = widget
        self._original_geometry: Optional[QRect] = None

    def eventFilter(self, obj, event):
        if obj is not self._w:
            return False
        if event.type() == QEvent.MouseButtonPress:
            self._squish()
        elif event.type() in (QEvent.MouseButtonRelease, QEvent.Leave):
            self._restore()
        return False

    def _squish(self):
        g = self._w.geometry()
        if self._original_geometry is None:
            self._original_geometry = g
        cx, cy = g.center().x(), g.center().y()
        sw, sh = int(g.width() * 0.97), int(g.height() * 0.97)
        target = QRect(cx - sw // 2, cy - sh // 2, sw, sh)
        a = QPropertyAnimation(self._w, b"geometry", self._w)
        a.setDuration(_ms(None, "duration_fast"))
        a.setEndValue(target)
        a.setEasingCurve(_easing(QEasingCurve.Type.OutCubic))
        a.start(QAbstractAnimation.DeleteWhenStopped)

    def _restore(self):
        if self._original_geometry is None:
            return
        a = QPropertyAnimation(self._w, b"geometry", self._w)
        a.setDuration(_ms(None, "duration_fast"))
        a.setEndValue(self._original_geometry)
        a.setEasingCurve(_easing(QEasingCurve.Type.OutBack))
        a.start(QAbstractAnimation.DeleteWhenStopped)


def press_squish(widget: QWidget) -> None:
    """Add a tactile press-squish micro-animation to a widget."""
    if widget is None:
        return
    flt = _PressSquishFilter(widget)
    widget.installEventFilter(flt)
    widget._press_squish_filter = flt