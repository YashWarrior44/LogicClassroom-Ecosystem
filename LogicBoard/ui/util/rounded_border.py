"""
rounded_border.py — one-pass rounded borders for translucent surfaces.

Why this exists
---------------
Qt's stylesheet border painter cannot draw a SEMI-TRANSPARENT rounded border
correctly. With ``border-radius`` set it renders the four straight edges and
the four corner arcs as separate paint operations; wherever they meet, the
translucent pixels are composited twice and the alpha compounds. Measured on
PySide6 6.11.1, a plain QFrame, alpha sampled from the rendered image:

    3px  rgba(255,255,255,90)  -> straight edge  90, corner 148   (+58)
    1px  rgba(255,255,255,90)  -> straight edge   0, corner 118  (+118)
    3px  opaque white          -> straight edge 255, corner 255    (0)

So the corners come out visibly darker than the edges, and at 1 px the two
disagree completely. Opaque borders hide the bug — 255 over 255 is still 255
— which is why the artifact only appears the moment you make a border
translucent. It is not a rounding or antialiasing issue and no amount of
tweaking the QSS changes it.

The fix is to stop asking QSS for the border at all: set ``border: none`` and
stroke ONE ``QPainterPath`` instead. A single stroke composites once, so the
alpha is uniform all the way round by construction.

Usage
-----
    from ui.util.rounded_border import install_rounded_border

    self.setStyleSheet("QDialog { background: none; border: none;"
                       "           border-radius: 14px; }")
    install_rounded_border(self, radius=14, color=QColor(255, 255, 255, 38))

``border-radius`` should still be set in the QSS — it clips the background
fill. Only the BORDER moves here.

Why an overlay child rather than a paintEvent override
------------------------------------------------------
The panels are a dozen unrelated QDialog subclasses and most don't override
paintEvent at all. An overlay child is opt-in per widget, needs no subclass,
and paints strictly on top of the panel's own content — which is what a
border has to do.
"""
from __future__ import annotations

from typing import Optional

from PySide6.QtCore import QEvent, QObject, QRectF, Qt
from PySide6.QtGui import QColor, QPainter, QPainterPath, QPen
from PySide6.QtWidgets import QWidget

__all__ = ["install_rounded_border", "RoundedBorder"]


class RoundedBorder(QWidget):
    """Transparent child that strokes a rounded rect over its parent."""

    def __init__(self, parent: QWidget, *, radius: float,
                 color: QColor, width: float = 1.0) -> None:
        super().__init__(parent)
        self._radius = float(radius)
        self._color = QColor(color)
        self._width = float(width)
        # Must never eat input — it covers the entire panel.
        self.setAttribute(Qt.WA_TransparentForMouseEvents, True)
        self.setAttribute(Qt.WA_NoSystemBackground, True)
        self.setAttribute(Qt.WA_TranslucentBackground, True)
        self.setFocusPolicy(Qt.NoFocus)
        self.setGeometry(parent.rect())
        self.raise_()

    def set_border(self, *, radius: Optional[float] = None,
                   color: Optional[QColor] = None,
                   width: Optional[float] = None) -> None:
        if radius is not None: self._radius = float(radius)
        if color is not None:  self._color = QColor(color)
        if width is not None:  self._width = float(width)
        self.update()

    def paintEvent(self, _e) -> None:
        if self._width <= 0 or self._color.alpha() == 0:
            return
        p = QPainter(self)
        try:
            p.setRenderHint(QPainter.Antialiasing, True)
            # Inset by half the pen width so the stroke lands INSIDE the
            # widget instead of straddling the edge and getting clipped —
            # a clipped stroke is half as bright, which would reintroduce
            # the very unevenness this module exists to remove.
            half = self._width / 2.0
            r = QRectF(self.rect()).adjusted(half, half, -half, -half)
            if r.width() <= 0 or r.height() <= 0:
                return
            # Shrink the radius by the same inset so the stroked curve stays
            # concentric with the background's border-radius.
            rad = max(0.0, self._radius - half)
            path = QPainterPath()
            path.addRoundedRect(r, rad, rad)
            p.setPen(QPen(self._color, self._width))
            p.setBrush(Qt.NoBrush)
            p.drawPath(path)          # ONE stroke — uniform alpha everywhere
        finally:
            p.end()


class _BorderKeeper(QObject):
    """Keeps the overlay full-size and on top of later-added siblings."""

    def __init__(self, host: QWidget, overlay: RoundedBorder) -> None:
        super().__init__(host)
        self._overlay = overlay
        host.installEventFilter(self)

    def eventFilter(self, obj, ev):
        t = ev.type()
        if t == QEvent.Resize:
            self._overlay.setGeometry(obj.rect())
            self._overlay.raise_()
        elif t in (QEvent.ChildAdded, QEvent.Show):
            # A child added after us would otherwise paint over the border.
            self._overlay.raise_()
        return False


def install_rounded_border(widget: QWidget, *, radius: float,
                           color: QColor, width: float = 1.0) -> RoundedBorder:
    """Give ``widget`` a single-stroke rounded border.

    Set ``border: none`` in the widget's own stylesheet first — otherwise the
    QSS border paints too and you get both, which defeats the point. Keep
    ``border-radius`` there so the background fill stays clipped to the same
    curve. Idempotent: calling twice reconfigures the existing overlay rather
    than stacking a second one.
    """
    existing = widget.findChild(RoundedBorder)
    if existing is not None and existing.parent() is widget:
        existing.set_border(radius=radius, color=color, width=width)
        existing.setGeometry(widget.rect())
        existing.raise_()
        return existing
    overlay = RoundedBorder(widget, radius=radius, color=color, width=width)
    _BorderKeeper(widget, overlay)
    overlay.show()
    return overlay
