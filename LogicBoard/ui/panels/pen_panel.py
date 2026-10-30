"""
pen_panel.py — the Drawing Tool popover: Pen, Highlighter, Brush, Dual Pen.

    ┌────────────────────────────────────────────────────┐
    │                    Drawing Tool                    │
    │  ┌──────┐  PEN WIDTH                        4 px   │
    │  │ Pen  │  ━━━━━━━●━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━  │
    │  ├──────┤  PREVIEW                    (BACK TIP)   │
    │  │ High │  [ stroke preview ] ⇄ [ 5 back tips ]    │
    │  ├──────┤  COLOUR                                  │
    │  │Brush │  ● ● ● ● ● ● ● ●                         │
    │  ├──────┤  ● ● ● ● ● ● ● ●                         │
    │  │ Dual │  ● ● ● ● ● ● ● +                         │
    │  └──────┘                                          │
    └────────────────────────────────────────────────────┘

Two columns: the four tools stacked on the left, everything about the
chosen tool on the right. THE COLUMNS END LEVEL. The tile size is not a
constant; it is worked out from the right column's real height, and the
preview strip takes up any remainder, so the last tile and the last row of
colours share a bottom edge on every display scale — no empty band under
either column.

Every row of the right column is exactly ``_col_w()`` wide: slider, strip
and colour grid share both edges.

THE PANEL IS ONE SIZE IN EVERY MODE. Dual Pen's back-tip picker and the
other modes' stroke preview share one strip (a QStackedWidget), so
switching mode cannot resize the panel under the stylus, and Dual Pen keeps
the whole colour grid.

Colours come from ui/theme/ink_palette.py, shared with the shapes panel.
"""

from __future__ import annotations

import math
from typing import Callable, Optional

from PySide6.QtCore import QEvent, QPoint, QPointF, QSize, Qt
from PySide6.QtGui import QBrush, QColor, QPainter, QPainterPath, QPen, QPixmap
from PySide6.QtWidgets import (QColorDialog, QDialog, QGridLayout, QHBoxLayout,
                               QLabel, QPushButton, QStackedWidget, QToolButton,
                               QVBoxLayout, QWidget)

from ui.theme.ink_palette import (GRID_COLS, GRID_ROWS, INK_COLORS,
                                  INK_NAMES)
from ui.theme.theme_manager import theme_mgr
from ui.util.aero import apply_aero, enable_shadow, prepare_aero_widget
from ui.util.audio import click_sound
from ui.util.circle_slider import CircleHandleSlider
from ui.util.icon_loader import tool_icon
from ui.util.scaling import UiScale

__all__ = ["PenPanel"]

# Kept under their old names: tests and callers import them from here.
_SWATCH_COLS = GRID_COLS
_SWATCH_ROWS = GRID_ROWS
_PRESET_COLORS = INK_COLORS

# Reference sizes (design px, scaled by UiScale).
_TILE_W = 96          # tool tile width — shared with the eraser panel
_TILE_MIN_H = 78      # shortest pen tile; the real height is derived
_TILE_GAP = 8
_COL_GAP = 16         # between the tile column and the right column
_SWATCH = 34
_SWATCH_GAP = 8
_STRIP_H = 60         # preview / back-tip strip, before levelling
_BACK_GAP = 6         # 5 back-tip tiles across the column

# Stroke sizes, in board px at 100 % zoom (NOT scaled by UiScale: the
# preview draws them at the size they will have on the board).
_PEN_RANGE = (1, 40)
_BRUSH_RANGE = (6, 60)
_HL_RANGE = (10, 60)      # keep in step with HIGHLIGHTER_MAX_HEIGHT
# The preview strip is never shorter than the biggest stroke plus this much
# clear space above and below, so every size is shown whole.
_PREVIEW_PAD = 8

# The pen slider is not linear. Teachers write at 1-10 px almost all the
# time, and on a linear 1-40 slider those ten sizes were crammed into the
# first quarter, a few px of travel apart. Here the first THIRD of the
# slider is 1-10 px and the other two thirds are 11 px to the maximum.
_PEN_FINE_TOP = 10
_PEN_STEPS = 600          # slider positions; a multiple of 3


def _pen_pos_to_width(pos: int) -> int:
    """Slider position (0.._PEN_STEPS) -> pen width in px."""
    lo, hi = _PEN_RANGE
    third = _PEN_STEPS // 3
    pos = max(0, min(_PEN_STEPS, int(pos)))
    if pos <= third:
        return int(round(lo + pos * (_PEN_FINE_TOP - lo) / third))
    w = _PEN_FINE_TOP + (pos - third) * (hi - _PEN_FINE_TOP) / (_PEN_STEPS - third)
    return max(_PEN_FINE_TOP + 1, min(hi, int(round(w))))


def _pen_width_to_pos(width: int) -> int:
    """Pen width in px -> the slider position that shows it."""
    lo, hi = _PEN_RANGE
    third = _PEN_STEPS // 3
    w = max(lo, min(hi, int(width)))
    if w <= _PEN_FINE_TOP:
        return int(round((w - lo) * third / (_PEN_FINE_TOP - lo)))
    return int(round(third + (w - _PEN_FINE_TOP) * (_PEN_STEPS - third)
                     / (hi - _PEN_FINE_TOP)))

_MODES = ("pen", "highlighter", "brush", "dual_pen")

_SIZE_HEADING = {
    "pen":         "PEN WIDTH",
    "highlighter": "HIGHLIGHTER HEIGHT",
    "brush":       "BRUSH NIB",
    "dual_pen":    "FRONT TIP WIDTH",
}

# The back tip can be any tool that draws or picks; brush included.
_BACK_TIPS = (
    ("pen",         "Pen",         "pen"),
    ("highlighter", "Highlighter", "highlighter"),
    ("brush",       "Brush",       "brush"),
    ("eraser",      "Eraser",      "eraser"),
    ("select",      "Select",      "select"),
)


def _tile_w() -> int:
    """The left column's width. Both popovers use it, so the Drawing Tool
    and Eraser panels are the same width and their tiles the same width."""
    return UiScale.px(_TILE_W)


def _col_w() -> int:
    """The right column's width: eight colour swatches and their gaps.
    The slider, the strip and the grid are all exactly this wide."""
    sc = UiScale.px
    return 8 * sc(_SWATCH) + 7 * sc(_SWATCH_GAP)


def _luma(c: QColor) -> float:
    return 0.299 * c.red() + 0.587 * c.green() + 0.114 * c.blue()


class PenPanel(QDialog):
    """Floating pen / highlighter / brush / dual-pen settings popover."""

    def __init__(
        self,
        parent=None,
        *,
        mode: str = "pen",
        pen_width: int = 3,
        highlighter_height: int = 35,
        brush_height: int = 12,
        pen_color: QColor = QColor(92, 162, 255),
        highlighter_color: QColor = QColor(255, 201, 60),
        brush_color: QColor = QColor(92, 162, 255),
        dual_color: QColor = QColor(92, 162, 255),
        on_mode_change: Optional[Callable[[str], None]] = None,
        on_width_change: Optional[Callable[[int], None]] = None,
        on_color_change: Optional[Callable[[QColor], None]] = None,
        dual_front: str = "pen",
        dual_back: str = "select",
        on_dual_back_change: Optional[Callable[[str], None]] = None,
        # Dual Pen tells the front tip from the back tip, so it needs BOTH
        # calibrated. False by default: a gate whose default is "open" is
        # not a gate (main_window always passes the real values).
        stylus_calibrated: bool = False,
        eraser_calibrated: bool = False,
    ) -> None:
        super().__init__(
            parent, Qt.Tool | Qt.FramelessWindowHint | Qt.WindowStaysOnTopHint)
        self.setModal(False)
        # Before the first show: makes the Qt surface translucent so the
        # Win32 blur applied in showEvent reads through it.
        prepare_aero_widget(self)

        # Our own outside-tap dismissal as well as main_window's: on some
        # touch stacks the central filter misses. The toolbar is ignored so
        # its pen button can toggle the panel shut in one tap.
        try:
            from ui.util.dismiss_on_outside import install_outside_dismiss
            tb = parent._toolbar if hasattr(parent, "_toolbar") else None
            ignore = [w for w in (getattr(tb, n, None) for n in
                                  ("pen_btn", "left", "center", "right"))
                      if w is not None] if tb is not None else []
            install_outside_dismiss(self, ignore_widgets=ignore)
        except Exception:
            pass

        self._stylus_calibrated = bool(stylus_calibrated)
        self._eraser_calibrated = bool(eraser_calibrated)
        self._dual_ready = self._stylus_calibrated and self._eraser_calibrated
        if not self._stylus_calibrated and not self._eraser_calibrated:
            self._lock_msg = ("Calibrate the front and back tips to unlock "
                              "Dual Pen.")
        elif not self._stylus_calibrated:
            self._lock_msg = "Calibrate the FRONT tip to unlock Dual Pen."
        else:
            self._lock_msg = "Calibrate the BACK tip to unlock Dual Pen."
        # Opened in a mode it cannot honour (a hand-edited settings file):
        # fall back to the plain pen rather than show an unusable mode.
        if mode == "dual_pen" and not self._dual_ready:
            mode = "pen"
        if mode not in _MODES:
            mode = "pen"

        self._mode = mode
        self._pen_width = int(pen_width)
        # Clamped here as well as on the canvas: a size saved before the
        # limits changed (highlighters went up to 110) must not show as a
        # value the slider cannot reach.
        self._pen_width = max(_PEN_RANGE[0], min(_PEN_RANGE[1], self._pen_width))
        self._hl_height = max(_HL_RANGE[0], min(_HL_RANGE[1],
                                                int(highlighter_height)))
        self._brush_height = max(_BRUSH_RANGE[0], min(_BRUSH_RANGE[1],
                                                      int(brush_height)))
        self._pen_color = QColor(pen_color)
        self._hl_color = QColor(highlighter_color)
        self._brush_color = QColor(brush_color)
        self._dual_color = QColor(dual_color)
        self._on_mode = on_mode_change
        self._on_width = on_width_change
        self._on_color = on_color_change
        self._dual_front = dual_front
        self._dual_back = dual_back
        self._on_dual_back_change = on_dual_back_change
        self._loading = False

        self._build()
        self._reload()
        # Every row has a fixed size, so this is the size of every mode.
        self.adjustSize()
        self.setFixedSize(self.sizeHint())

    # ================================================================
    # Construction
    # ================================================================

    def _build(self) -> None:
        sc = UiScale.px
        t = theme_mgr.tokens
        col_w = _col_w()
        gap = sc(_TILE_GAP)

        # No painted outline: DWM rounds and shadows the real window, and a
        # stroke drawn inside the client area only ever disagreed with it.
        self.setStyleSheet(
            f"QDialog {{ background: transparent; border: none; "
            f"border-radius: {t.radius_xl}px; }}")

        container = QWidget(self)
        self._container = container
        container.setStyleSheet(
            f"QWidget {{ background: transparent; color: {t.text_primary}; }}")
        lay = QVBoxLayout(container)
        m = sc(18)
        lay.setContentsMargins(m, sc(16), m, m)
        lay.setSpacing(0)

        title = QLabel("Drawing Tool")
        title.setAlignment(Qt.AlignCenter)
        title.setStyleSheet(
            f"color: {t.text_primary}; font-size: {sc(17)}px; "
            f"font-weight: 800; background: transparent;")
        lay.addWidget(title)
        lay.addSpacing(sc(14))

        body = QHBoxLayout()
        body.setContentsMargins(0, 0, 0, 0)
        body.setSpacing(sc(_COL_GAP))

        # ── LEFT: the four tools ─────────────────────────────────────────
        left = QVBoxLayout()
        left.setContentsMargins(0, 0, 0, 0)
        left.setSpacing(gap)
        self._pen_btn = self._tool_tile("Pen", "pen")
        self._hl_btn = self._tool_tile("Highlighter", "highlighter")
        self._brush_btn = self._tool_tile("Brush", "brush")
        self._dual_btn = self._tool_tile("Dual Pen", "dual_pen")
        tiles = (self._pen_btn, self._hl_btn, self._brush_btn, self._dual_btn)
        for b, m_ in zip(tiles, ("pen", "highlighter", "brush", "dual_pen")):
            b.clicked.connect(lambda _c=False, mm=m_: self._select_mode(mm))
            left.addWidget(b)
        body.addLayout(left)

        # ── RIGHT: everything about the chosen tool ──────────────────────
        right_box = QWidget()
        right_box.setStyleSheet("background: transparent;")
        right = QVBoxLayout(right_box)
        right.setContentsMargins(0, 0, 0, 0)
        right.setSpacing(0)

        size_head = QHBoxLayout()
        size_head.setContentsMargins(0, 0, 0, 0)
        self._size_heading = self._heading("PEN WIDTH")
        size_head.addWidget(self._size_heading)
        size_head.addStretch(1)
        self._value_label = QLabel()
        self._value_label.setAlignment(Qt.AlignRight | Qt.AlignVCenter)
        self._value_label.setStyleSheet(
            f"color: {t.accent}; font-size: {sc(14)}px; font-weight: 800; "
            f"background: transparent;")
        size_head.addWidget(self._value_label)
        right.addLayout(size_head)
        right.addSpacing(sc(2))

        self._slider = CircleHandleSlider()
        self._slider.setFixedWidth(col_w)
        self._slider.valueChanged.connect(self._on_slider)
        right.addWidget(self._slider)
        right.addSpacing(sc(10))

        self._strip_heading = self._heading("PREVIEW")
        right.addWidget(self._strip_heading)
        right.addSpacing(sc(6))
        self._strip = QStackedWidget()
        self._strip.setStyleSheet("background: transparent;")
        self._preview = QLabel()
        self._preview.setAlignment(Qt.AlignCenter)
        self._preview.setStyleSheet(
            f"QLabel {{ background: {t.card_grad}; border: 1px solid "
            f"{t.border}; border-radius: {sc(14)}px; }}")
        self._preview.installEventFilter(self)
        self._strip.addWidget(self._preview)

        self._dual_section = QWidget()
        row = QHBoxLayout(self._dual_section)
        row.setContentsMargins(0, 0, 0, 0)
        row.setSpacing(sc(_BACK_GAP))
        n = len(_BACK_TIPS)
        bw = (col_w - (n - 1) * sc(_BACK_GAP)) // n
        self._bt_btns: dict[str, QToolButton] = {}
        for tool_id, label, icon in _BACK_TIPS:
            b = QToolButton()
            b.setText(label)
            b.setToolTip(f"Back tip: {label}")
            b.setToolButtonStyle(Qt.ToolButtonTextUnderIcon)
            b.setIcon(tool_icon(icon, sc(22), t.text_primary))
            b.setIconSize(QSize(sc(22), sc(22)))
            b.setCursor(Qt.PointingHandCursor)
            b.clicked.connect(lambda _c=False, tid=tool_id:
                              self._pick_back_tip(tid))
            b.setFixedWidth(bw)
            self._bt_btns[tool_id] = b
            row.addWidget(b)
        self._strip.addWidget(self._dual_section)
        right.addWidget(self._strip)
        right.addSpacing(sc(12))

        right.addWidget(self._heading("COLOUR"))
        right.addSpacing(sc(6))
        grid = QGridLayout()
        grid.setContentsMargins(0, 0, 0, 0)
        sgap = sc(_SWATCH_GAP)
        grid.setHorizontalSpacing(sgap)
        grid.setVerticalSpacing(sgap)
        sw = (col_w - (GRID_COLS - 1) * sgap) // GRID_COLS
        self._swatch_px = sw
        self._swatch_btns: list[tuple[QPushButton, QColor]] = []
        for idx, (c, name) in enumerate(zip(INK_COLORS, INK_NAMES)):
            btn = QPushButton()
            btn.setFixedSize(sw, sw)
            btn.setCursor(Qt.PointingHandCursor)
            btn.setToolTip(name)
            btn.clicked.connect(lambda _c=False, col=QColor(c):
                                self._pick_color(col))
            self._swatch_btns.append((btn, QColor(c)))
            r, cc = divmod(idx, GRID_COLS)
            grid.addWidget(btn, r, cc)
        self._custom_btn = QPushButton("+")
        self._custom_btn.setFixedSize(sw, sw)
        self._custom_btn.setCursor(Qt.PointingHandCursor)
        self._custom_btn.setToolTip("Custom colour…")
        self._custom_btn.clicked.connect(self._pick_custom_color)
        grid.addWidget(self._custom_btn, GRID_ROWS - 1, GRID_COLS - 1)
        grid_box = QWidget()
        grid_box.setFixedWidth(col_w)
        grid_box.setLayout(grid)
        grid_box.setStyleSheet("background: transparent;")
        right.addWidget(grid_box)
        body.addWidget(right_box, 0, Qt.AlignTop)
        lay.addLayout(body)

        # ── level the two columns ────────────────────────────────────────
        # Measure the right column with the strip at its starting height,
        # derive the tile height from it, then give the strip whatever is
        # left so 4 tiles + 3 gaps == the right column, to the pixel.
        strip0 = max(sc(_STRIP_H),
                     max(_HL_RANGE[1], _BRUSH_RANGE[1]) + 2 * _PREVIEW_PAD)
        self._set_strip_h(strip0, col_w)
        right_box.adjustSize()
        right_h = right_box.sizeHint().height()
        th = max(sc(_TILE_MIN_H), (right_h - 3 * gap) // 4)
        left_h = 4 * th + 3 * gap
        self._set_strip_h(strip0 + (left_h - right_h), col_w)
        for b in tiles:
            b.setFixedSize(_tile_w(), th)
        right_box.setFixedHeight(left_h)

        outer = QVBoxLayout(self)
        outer.setContentsMargins(0, 0, 0, 0)
        outer.addWidget(container)

    def _set_strip_h(self, h: int, col_w: int) -> None:
        """One height for the strip and both of its pages."""
        self._strip.setFixedSize(col_w, h)
        self._preview.setFixedSize(col_w, h)
        self._dual_section.setFixedSize(col_w, h)
        for b in getattr(self, "_bt_btns", {}).values():
            b.setFixedHeight(h)

    def _tool_tile(self, label: str, icon: str) -> QToolButton:
        sc = UiScale.px
        t = theme_mgr.tokens
        b = QToolButton()
        b.setText(label)
        b.setToolButtonStyle(Qt.ToolButtonTextUnderIcon)
        b.setIcon(tool_icon(icon, sc(32), t.text_primary))
        b.setIconSize(QSize(sc(32), sc(32)))
        b.setCursor(Qt.PointingHandCursor)
        return b

    def _heading(self, text: str) -> QLabel:
        sc = UiScale.px
        t = theme_mgr.tokens
        h = QLabel(text)
        h.setStyleSheet(
            f"color: {t.text_secondary}; font-size: {sc(11)}px; "
            f"font-weight: 800; letter-spacing: 1px; background: transparent;")
        return h

    # ================================================================
    # Mode
    # ================================================================

    def _select_mode(self, mode: str) -> None:
        # The locked tile is also refused here, so no other path (keyboard,
        # an external caller) can slip past it.
        if mode == "dual_pen" and not self._dual_ready:
            return
        if mode not in _MODES:
            return
        click_sound.play_click()
        self._mode = mode
        if self._on_mode:
            self._on_mode(mode)
        self._reload()

    def _pick_back_tip(self, tool_id: str) -> None:
        click_sound.play_click()
        self._dual_back = tool_id
        if self._on_dual_back_change:
            self._on_dual_back_change(tool_id)
        self._reload()

    # ================================================================
    # State -> widgets
    # ================================================================

    def _reload(self) -> None:
        sc = UiScale.px
        t = theme_mgr.tokens
        mode = self._mode

        def tile_qss(selected: bool) -> str:
            return f"""
                QToolButton {{
                    background: {t.accent_selected_bg if selected else t.tile_grad};
                    color: {t.text_primary};
                    border: {2 if selected else 1}px solid
                            {t.accent if selected else t.border};
                    border-radius: {sc(16)}px;
                    font-size: {sc(10.5)}px; font-weight: 700;
                    padding: {sc(6)}px {sc(2)}px {sc(5)}px {sc(2)}px;
                }}
                QToolButton:hover {{
                    background: {t.bg_tile_hover}; border: 2px solid {t.accent};
                }}
            """

        for b, m in ((self._pen_btn, "pen"), (self._hl_btn, "highlighter"),
                     (self._brush_btn, "brush")):
            b.setStyleSheet(tile_qss(mode == m))

        if self._dual_ready:
            self._dual_btn.setStyleSheet(tile_qss(mode == "dual_pen"))
            self._dual_btn.setCursor(Qt.PointingHandCursor)
            self._dual_btn.setText("Dual Pen")
            self._dual_btn.setToolTip("")
        else:
            # AMBER, not grey: the tool is available, it needs one thing
            # first, and amber is how the app says that. No hover, and an
            # arrow cursor, so the tile does not pretend to be clickable.
            self._dual_btn.setStyleSheet(f"""
                QToolButton {{
                    background: {t.warning_bg}; color: {t.warning_text};
                    border: 1px solid {t.warning_border};
                    border-radius: {sc(16)}px;
                    font-size: {sc(10.5)}px; font-weight: 700;
                    padding: {sc(6)}px {sc(2)}px {sc(5)}px {sc(2)}px;
                }}
            """)
            self._dual_btn.setCursor(Qt.ArrowCursor)
            self._dual_btn.setText("🔒 Dual Pen")
            self._dual_btn.setToolTip(self._lock_msg)

        is_dual = mode == "dual_pen"
        self._size_heading.setText(_SIZE_HEADING[mode])
        self._strip_heading.setText("BACK TIP" if is_dual else "PREVIEW")
        self._strip.setCurrentWidget(self._dual_section if is_dual
                                     else self._preview)
        if is_dual:
            for tid, b in self._bt_btns.items():
                on = tid == self._dual_back
                b.setStyleSheet(f"""
                    QToolButton {{
                        background: {t.accent_selected_bg if on else t.tile_grad};
                        color: {t.text_primary if on else t.text_secondary};
                        border: {2 if on else 1}px solid
                                {t.accent if on else t.border};
                        border-radius: {sc(12)}px;
                        font-size: {sc(10)}px; font-weight: 700;
                        padding: {sc(5)}px {sc(2)}px {sc(4)}px {sc(2)}px;
                    }}
                    QToolButton:hover {{
                        background: {t.bg_tile_hover};
                        border: 2px solid {t.accent};
                    }}
                """)

        self._loading = True
        if mode in ("pen", "dual_pen"):
            # Positions, not px: see _pen_pos_to_width.
            self._slider.setRange(0, _PEN_STEPS)
            self._slider.setSingleStep(_PEN_STEPS // 60)
            self._slider.setPageStep(_PEN_STEPS // 6)
            self._slider.setValue(_pen_width_to_pos(self._pen_width))
        elif mode == "brush":
            # A writing tool: a 110 px nib is a paint roller.
            self._slider.setRange(*_BRUSH_RANGE)
            self._slider.setSingleStep(1)
            self._slider.setPageStep(5)
            self._slider.setValue(self._brush_height)
        else:
            self._slider.setRange(*_HL_RANGE)
            self._slider.setSingleStep(1)
            self._slider.setPageStep(5)
            self._slider.setValue(self._hl_height)
        self._loading = False
        self._update_value_label()

        self._refresh_swatches()
        self._draw_preview()

    def _current_size(self) -> int:
        if self._mode == "brush":
            return self._brush_height
        if self._mode == "highlighter":
            return self._hl_height
        return self._pen_width

    def _update_value_label(self) -> None:
        unit = " px nib" if self._mode == "brush" else " px"
        self._value_label.setText(f"{self._current_size()}{unit}")

    # ================================================================
    # Size
    # ================================================================

    def _on_slider(self, value: int) -> None:
        if self._loading:
            return
        if self._mode in ("pen", "dual_pen"):
            value = _pen_pos_to_width(value)
            if value == self._pen_width:
                return          # dragging within one px's stretch of slider
            self._pen_width = value
        elif self._mode == "brush":
            self._brush_height = value
        else:
            self._hl_height = value
        self._update_value_label()
        if self._on_width:
            self._on_width(value)
        self._draw_preview()

    # ================================================================
    # Colour
    # ================================================================

    def _active_color(self) -> QColor:
        # Four modes, four inks — a colour picked in one never changes another.
        if self._mode == "brush":
            return QColor(self._brush_color)
        if self._mode == "dual_pen":
            return QColor(self._dual_color)
        if self._mode == "highlighter":
            return QColor(self._hl_color)
        return QColor(self._pen_color)

    def _set_active_color(self, c: QColor) -> None:
        if self._mode == "brush":
            self._brush_color = QColor(c)
        elif self._mode == "dual_pen":
            self._dual_color = QColor(c)
        elif self._mode == "highlighter":
            self._hl_color = QColor(c)
        else:
            self._pen_color = QColor(c)

    def _pick_color(self, c: QColor) -> None:
        click_sound.play_click()
        self._set_active_color(c)
        if self._on_color:
            self._on_color(QColor(c))
        self._refresh_swatches()
        self._draw_preview()

    def _pick_custom_color(self) -> None:
        chosen = QColorDialog.getColor(self._active_color(), self,
                                       "Choose Ink Colour")
        if chosen.isValid():
            self._pick_color(chosen)

    def _refresh_swatches(self) -> None:
        sc = UiScale.px
        t = theme_mgr.tokens
        active = self._active_color()
        sw = self._swatch_px
        r = sw // 2
        rgb = (active.red(), active.green(), active.blue())
        matched = False
        for btn, c in self._swatch_btns:
            on = (c.red(), c.green(), c.blue()) == rgb
            matched |= on
            # Fixed-size buttons: a thicker border on the selected one goes
            # INWARD, so it never grows out of line with its neighbours.
            tick = t.swatch_tick_dark if _luma(c) > 150 else t.swatch_tick_light
            btn.setText("✓" if on else "")
            btn.setStyleSheet(f"""
                QPushButton {{
                    background: rgb({c.red()}, {c.green()}, {c.blue()});
                    border-radius: {r}px;
                    border: {sc(3) if on else 1}px solid
                            {t.accent if on else t.swatch_ring};
                    color: {tick}; font-size: {sc(16)}px; font-weight: 900;
                }}
                QPushButton:hover {{ border: {sc(3)}px solid {t.accent}; }}
            """)
        # The custom cell is a colour wheel, and wears the selection ring
        # when the active ink is one the grid does not have.
        self._custom_btn.setStyleSheet(f"""
            QPushButton {{
                background: {t.swatch_custom_grad};
                border-radius: {r}px;
                border: {sc(3) if not matched else 1}px solid
                        {t.accent if not matched else t.swatch_ring};
                color: {t.swatch_tick_light}; font-size: {sc(20)}px; font-weight: 900;
            }}
            QPushButton:hover {{ border: {sc(3)}px solid {t.accent}; }}
        """)

    # ================================================================
    # Preview
    # ================================================================

    def eventFilter(self, obj, event):
        if obj is getattr(self, "_preview", None) and \
                event.type() == QEvent.Resize and hasattr(self, "_swatch_btns"):
            self._draw_preview()
        return super().eventFilter(obj, event)

    def _draw_preview(self) -> None:
        """The stroke at its real width. The strip is built tall enough
        for the largest size every slider allows, so nothing is capped.

        A gentle wave rather than a straight bar: it shows the pen's round
        joins, the highlighter's flat band, and — the one that matters — the
        brush's thick-and-thin, which only appears when the stroke turns."""
        pw, ph = self._preview.width(), self._preview.height()
        if pw <= 0 or ph <= 0:
            return
        dpr = 3
        pix = QPixmap(pw * dpr, ph * dpr)
        pix.setDevicePixelRatio(dpr)
        pix.fill(Qt.transparent)
        p = QPainter(pix)
        try:
            p.setRenderHint(QPainter.Antialiasing, True)
            cx, cy = pw / 2.0, ph / 2.0
            room = ph - 2 * _PREVIEW_PAD           # px of height to draw in
            half = pw * 0.36

            def wave(amp):
                pts = []
                for i in range(49):
                    u = i / 48.0
                    pts.append(QPointF(cx - half + u * 2 * half,
                                       cy + math.sin(u * math.pi * 2) * amp))
                return pts

            if self._mode == "brush":
                from ui.canvas.brush_ink import BRUSH_NIB_ANGLE_DEG
                from ui.canvas.canvas_widget import CanvasWidget
                # The nib is slanted, so the stroke stands only
                # h * cos(angle) tall; the wave gets what is left over.
                h = float(self._brush_height)
                rise = h * abs(math.cos(math.radians(BRUSH_NIB_ANGLE_DEG)))
                pts = wave(max(2.0, (room - rise) / 2.0))
                p.setPen(Qt.NoPen)
                p.setBrush(QBrush(QColor(self._brush_color)))
                p.drawPath(CanvasWidget._chisel_path(
                    pts, h, angle_deg=BRUSH_NIB_ANGLE_DEG))
            elif self._mode == "highlighter":
                c = QColor(self._hl_color)
                c.setAlpha(75)
                h = float(min(self._hl_height, room))  # fits by build
                p.setPen(Qt.NoPen)
                p.setBrush(QBrush(c))
                p.drawRoundedRect(QPointF(cx - half, cy - h / 2).x(),
                                  cy - h / 2, 2 * half, h,
                                  min(8.0, h / 2), min(8.0, h / 2))
            else:
                w = float(min(self._pen_width, room))
                path = QPainterPath()
                pts = wave(max(2.0, (room - w) / 2.0))
                path.moveTo(pts[0])
                for q in pts[1:]:
                    path.lineTo(q)
                p.setPen(QPen(self._active_color(), w, Qt.SolidLine,
                              Qt.RoundCap, Qt.RoundJoin))
                p.setBrush(Qt.NoBrush)
                p.drawPath(path)
        finally:
            p.end()
        self._preview.setPixmap(pix)

    # ================================================================
    # Window
    # ================================================================

    def showEvent(self, event):
        super().showEvent(event)
        apply_aero(self)
        enable_shadow(self)
        from ui.util.animations import scale_in
        scale_in(self)

    def show_anchored(self, anchor: QWidget, offset: QPoint = QPoint(0, 0)) -> None:
        # From the size the panel actually has (it is fixed), so it sits
        # exactly above the button that opened it.
        size = self.size()
        x = -(size.width() // 2) + (anchor.width() // 2)
        y = -size.height() - UiScale.px(25)
        self.move(anchor.mapToGlobal(QPoint(x, y)) + offset)
        self.show()
