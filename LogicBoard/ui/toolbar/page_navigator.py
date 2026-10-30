"""
page_navigator.py — [Prev]  [N / Total]  [Next].

    ┌──────┐             ┌──────┐
    │  <   │ ┌─────────┐ │  >   │
    │      │ │ 01 / 01 │ │      │     the counter, centred on the row
    │ Prev │ └─────────┘ │ Next │
    └──────┘             └──────┘

The counter is a tile of its own between the two buttons: centred on the
row's middle line, with the buttons' corner radius and ``tool_btn_bg``
gradient, hover and pressed fills. It carries no caption — the number says
what it is — so it can be sized for the number alone: about two thirds of a
button's height, which holds the digits with even room above and below
rather than a band of empty tile or digits pressed against its edges.

THE NUMBER NEVER CLIPS. Its width is measured from the font the label
actually draws with (the stylesheet is applied first, so the metrics are
the real ones, letter-spacing included) and every digit is measured as the
widest digit, so no page number is wider than the tile was sized for. A
board with so many pages that the counter would outgrow ``_MAX_W`` gets a
smaller font instead of a toolbar that keeps widening; if even the smallest
font is too wide, the tile grows — it is never narrower than its text.

Theme-aware: ``_apply_style`` is re-run on every theme_changed signal via
``apply_theme()`` from the parent toolbar.
"""

from __future__ import annotations

import re

from PySide6.QtWidgets import QWidget, QHBoxLayout, QVBoxLayout, QLabel, QFrame
from PySide6.QtCore    import Qt, Signal

from ui.toolbar.tool_button   import ToolButton
from ui.theme.theme_manager   import theme_mgr

__all__ = ["PageNavigator"]

# Design px, scaled by the toolbar's scale.
_TILE_H = 42          # of a 64 px button: room for the digits, no empty band
_VALUE_PX = 18        # the number's font
_VALUE_MIN_PX = 11    # smallest it shrinks to for a huge page count
_PAD_X = 14           # clear space either side of the number
_MIN_W = 92           # an ordinary board's counter is a steady width
_MAX_W = 168          # past this the font shrinks rather than the tile grow


class _CounterTile(QFrame):
    """The middle tile. A frame rather than a QPushButton so it can hold a
    label and still look like a ToolButton; it emits ``clicked`` itself on
    a release inside it."""

    clicked = Signal()

    def __init__(self, parent=None) -> None:
        super().__init__(parent)
        self._pressed = False

    def mousePressEvent(self, e):
        if e.button() == Qt.LeftButton:
            self._pressed = True
            self.setProperty("down", True)
            self.style().unpolish(self); self.style().polish(self)
        super().mousePressEvent(e)

    def mouseReleaseEvent(self, e):
        was = self._pressed
        self._pressed = False
        self.setProperty("down", False)
        self.style().unpolish(self); self.style().polish(self)
        super().mouseReleaseEvent(e)
        if was and e.button() == Qt.LeftButton and self.rect().contains(
                e.position().toPoint()):
            self.clicked.emit()


class PageNavigator(QWidget):
    """Compact page-navigation cluster: Prev, the page counter, Next."""

    prev_clicked   = Signal()
    next_clicked   = Signal()
    center_clicked = Signal()

    def __init__(self, parent=None) -> None:
        super().__init__(parent)
        self.setObjectName("PageNavigator")
        self._scale = 1.0
        self._value_px = _VALUE_PX
        self._fitted: tuple | None = None

        layout = QHBoxLayout(self)
        layout.setContentsMargins(0, 0, 0, 0)
        layout.setSpacing(0)

        self.prev_btn = ToolButton("⬅️", "Prev", icon_name="prev")
        self.next_btn = ToolButton("➡️", "Next", icon_name="next")

        self._center_frame = _CounterTile()
        self._center_frame.setObjectName("PageNavIndicator")
        self._center_frame.setCursor(Qt.PointingHandCursor)
        self._center_frame.setToolTip("Pages")
        cl = QVBoxLayout(self._center_frame)
        cl.setContentsMargins(0, 0, 0, 0)
        cl.setSpacing(0)
        self._value = QLabel("01 / 01")
        self._value.setObjectName("PageNavValue")
        self._value.setAlignment(Qt.AlignCenter)
        self._value.setAttribute(Qt.WA_TransparentForMouseEvents, True)
        cl.addWidget(self._value)

        self.prev_btn.clicked.connect(self.prev_clicked.emit)
        self.next_btn.clicked.connect(self.next_clicked.emit)
        self._center_frame.clicked.connect(self.center_clicked.emit)

        align = Qt.AlignVCenter
        layout.addWidget(self.prev_btn,      0, align)
        layout.addWidget(self._center_frame, 0, align)
        layout.addWidget(self.next_btn,      0, align)

        self._apply_style()
        self._apply_geometry()

    # ── public ──────────────────────────────────────────────────────────
    def set_page_text(self, text: str) -> None:
        self._value.setText(text)
        self._fit_to_text(text)

    def page_text(self) -> str:
        return self._value.text()

    def apply_scale(self, scale: float) -> None:
        self._scale = scale
        self.prev_btn.apply_scale(scale)
        self.next_btn.apply_scale(scale)
        self._fitted = None
        self._apply_style()
        self._apply_geometry()

    def apply_theme(self) -> None:
        self.prev_btn.apply_theme()
        self.next_btn.apply_theme()
        self._apply_style()

    # ── sizing ──────────────────────────────────────────────────────────
    def _s(self, v: float) -> int:
        return int(round(v * self._scale))

    def _probe(self, text: str) -> str:
        """The widest string this board's counter can show: both halves at
        the longer half's length, every digit the font's widest digit. Paging
        from 9 to 10 therefore does not shuffle the buttons either side; the
        width only changes when the board gains a digit."""
        fm = self._value.fontMetrics()
        widest = max("0123456789", key=fm.horizontalAdvance)
        parts = [q.strip() for q in text.split("/")] or [text]
        n = max(len(q) for q in parts)
        half = re.sub(r"\S", widest, "0" * n)
        return f"{half} / {half}" if "/" in text else half

    def _need(self, text: str) -> int:
        """Tile width that holds ``text`` at the label's current font."""
        self._value.ensurePolished()          # the stylesheet font, for real
        probe = self._probe(text)
        fm = self._value.fontMetrics()
        # +1 px per glyph covers letter-spacing on every platform's metrics.
        return (fm.horizontalAdvance(probe) + len(probe)
                + 2 * self._s(_PAD_X) + 2 * max(1, self._s(2)))

    def _fit_to_text(self, text: str) -> None:
        key = (self._scale, len(text), text.count("/"))
        if self._fitted == key:
            return
        cap = self._s(_MAX_W)
        px = self._s(_VALUE_PX)
        floor = max(8, self._s(_VALUE_MIN_PX))
        self._set_value_px(px)
        need = self._need(text)
        while need > cap and px > floor:
            px -= 1
            self._set_value_px(px)
            need = self._need(text)
        self._center_frame.setFixedWidth(max(self._s(_MIN_W), need))
        self._center_frame.updateGeometry()
        self._fitted = key

    def _apply_geometry(self) -> None:
        self.layout().setSpacing(self._s(8))
        self._center_frame.setFixedHeight(self._s(_TILE_H))
        self._apply_style()                     # radius follows the height
        self._fit_to_text(self._value.text())

    # ── style ───────────────────────────────────────────────────────────
    def _set_value_px(self, px: int) -> None:
        t = theme_mgr.tokens
        self._value_px = px
        self._value.setStyleSheet(f"""
            QLabel#PageNavValue {{
                background: transparent; border: none;
                color: {t.accent};
                font-size: {px}px; font-weight: 800;
                letter-spacing: 0.5px;
            }}
        """)

    def _apply_style(self) -> None:
        """Every value from the tokens the ToolButtons use, so the counter
        cannot drift from its neighbours on a theme or scale change."""
        t = theme_mgr.tokens
        s = self._s
        border_w = max(1, s(2))
        # The buttons' radius, but never more than half the tile's height:
        # past that, Qt's stylesheet painter gives up on the rounding and
        # draws SQUARE corners.
        radius = min(s(13), self._s(_TILE_H) // 2)

        self.setStyleSheet(
            "QWidget#PageNavigator { background: transparent; border: none; }")
        self._center_frame.setStyleSheet(f"""
            QFrame#PageNavIndicator {{
                background: {t.tool_btn_bg};
                border: {border_w}px solid transparent;
                border-radius: {radius}px;
            }}
            QFrame#PageNavIndicator:hover {{
                background: {t.tool_btn_hover};
                border: 1px solid {t.stroke_default};
            }}
            QFrame#PageNavIndicator[down="true"] {{
                background: {t.fill_pressed};
                border: 1px solid {t.stroke_strong};
            }}
        """)
        self._set_value_px(self._value_px)
