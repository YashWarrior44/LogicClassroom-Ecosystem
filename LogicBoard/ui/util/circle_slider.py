"""circle_slider.py — horizontal QSlider with a big CIRCULAR handle.

On the native Windows style, a QSS ``border-radius`` on ``QSlider::handle``
renders as a rounded *rectangle*, never a true circle (the style ignores the
handle's square geometry). This subclass paints the groove, the filled portion
and a perfectly round handle itself, and maps mouse input to the same geometry
so click / drag stays pixel-accurate.

Colours come from the theme tokens, which are CSS strings like
``rgba(255,255,255,0.10)``. ``QColor(str)`` CANNOT parse that functional
notation (it returns an invalid -> opaque-black colour), so every colour is
resolved through ``theme_mgr.qcolor`` which understands rgb()/rgba()/#hex.
"""
from __future__ import annotations

from PySide6.QtWidgets import QSlider
from PySide6.QtGui import QPainter, QPen, QColor
from PySide6.QtCore import Qt, QRectF, QPointF

from ui.util.scaling import UiScale          # tunnels to SizeManager.scale_size
from ui.theme.theme_manager import theme_mgr


def _qc(token, alpha: float | None = None) -> QColor:
    """Token string -> valid QColor (handles rgb()/rgba()/#hex). Optional
    alpha override in 0..1."""
    c = theme_mgr.qcolor(token)
    if alpha is not None:
        c = QColor(c)
        c.setAlpha(max(0, min(255, int(round(alpha * 255)))))
    return c


class CircleHandleSlider(QSlider):
    """Horizontal slider drawn entirely in :meth:`paintEvent` so the handle is
    a guaranteed circle at any DPI, with a translucent groove, an accent fill
    and a white accent-ringed knob (matching the Fluent QSS slider it replaced)."""

    def __init__(self, parent=None):
        super().__init__(Qt.Horizontal, parent)
        sc = UiScale.px
        self._groove_h = max(4, sc(6))
        self._handle_d = max(16, sc(28))      # big round handle
        self._border   = max(2, sc(3))
        self.setFixedHeight(self._handle_d + sc(12))
        self.setCursor(Qt.PointingHandCursor)
        # NOTE: deliberately NOT using WA_TranslucentBackground here — on a
        # child widget that flag makes Windows fill the surface black. A normal
        # child composites onto the parent's backing store, so the unpainted
        # area around the groove already shows the panel behind it.

    # ---- geometry ----
    def _track(self):
        """Return (x0, track_width): the handle centre travels x0 … x0+width."""
        r = self._handle_d / 2.0
        x0 = r
        x1 = self.width() - r
        return x0, max(1.0, x1 - x0)

    def _handle_x(self) -> float:
        x0, tw = self._track()
        rng = self.maximum() - self.minimum()
        frac = 0.0 if rng <= 0 else (self.value() - self.minimum()) / rng
        return x0 + frac * tw

    def _value_from_x(self, x: float) -> int:
        x0, tw = self._track()
        frac = min(1.0, max(0.0, (x - x0) / tw))
        rng = self.maximum() - self.minimum()
        return round(self.minimum() + frac * rng)

    # ---- interaction (map clicks to our geometry) ----
    def mousePressEvent(self, ev):
        if ev.button() == Qt.LeftButton:
            self.setSliderDown(True)
            self.setValue(self._value_from_x(ev.position().x()))
            self.update()
            ev.accept()
            return
        super().mousePressEvent(ev)

    def mouseMoveEvent(self, ev):
        if ev.buttons() & Qt.LeftButton:
            self.setValue(self._value_from_x(ev.position().x()))
            ev.accept()
            return
        super().mouseMoveEvent(ev)

    def mouseReleaseEvent(self, ev):
        if ev.button() == Qt.LeftButton:
            self.setSliderDown(False)
            self.update()
            ev.accept()
            return
        super().mouseReleaseEvent(ev)

    # ---- paint ----
    def paintEvent(self, _ev):
        t = theme_mgr.tokens
        sc = UiScale.px
        p = QPainter(self)
        try:
            p.setRenderHint(QPainter.Antialiasing, True)
            cy = self.height() / 2.0
            gh = float(self._groove_h)
            x0, tw = self._track()
            hx = self._handle_x()

            p.setPen(Qt.NoPen)
            # groove (full track) — translucent
            p.setBrush(_qc(t.slider_groove))
            p.drawRoundedRect(QRectF(x0, cy - gh / 2, tw, gh), gh / 2, gh / 2)
            # filled portion up to the handle — accent colour
            fill_w = max(0.0, hx - x0)
            if fill_w > 0:
                p.setBrush(_qc(t.slider_sub_page))
                p.drawRoundedRect(QRectF(x0, cy - gh / 2, fill_w, gh),
                                  gh / 2, gh / 2)

            r = self._handle_d / 2.0 - self._border / 2.0

            # soft drop shadow under the knob for depth
            p.setBrush(_qc("rgba(0,0,0,0.28)"))
            p.drawEllipse(QPointF(hx, cy + sc(1)), r + sc(1), r + sc(1))

            # accent glow ring while dragging
            if self.isSliderDown():
                p.setBrush(_qc(t.slider_sub_page, alpha=0.28))
                p.drawEllipse(QPointF(hx, cy), r + sc(5), r + sc(5))

            # knob: white fill + accent ring
            pen = QPen(_qc(t.accent))
            pen.setWidthF(self._border)
            p.setPen(pen)
            p.setBrush(_qc(t.text_on_accent))
            p.drawEllipse(QPointF(hx, cy), r, r)
        finally:
            p.end()