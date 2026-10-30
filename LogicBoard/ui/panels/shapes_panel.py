"""
shapes_panel.py — the Shapes popover: a 2D / 3D switch over a grid of shapes.

    ┌───┬──────────────────────────────────────────┐
    │ ● │ Shapes                     [ 2D | 3D ]   │
    │ ● │ ┌────┐┌────┐┌────┐┌────┐┌────┐           │
    │ ● │ │ ╱  ││ ╱┄ ││ ╱· ││ ↗  ││ ⤢  │  ...      │
    │ … │ └────┘└────┘└────┘└────┘└────┘           │
    │ + │                                          │
    └───┴──────────────────────────────────────────┘

The column on the left is the SHAPES' OWN colour. Shapes used to draw in
the pen's ink, so recolouring a diagram recoloured the handwriting next to
it. It runs the full height of the panel, its first swatch level with the
top edge and the custom cell level with the bottom.

Every icon is the CANVAS drawing of that shape, scaled down — single white
lines, dashed where the canvas dashes — from ui/canvas/shape_geometry, the
same code that draws the placed shape. The previous icons were separate
artwork with fills and gradients the canvas never produced, which promised
one thing and drew another.

ONE SIZE ON BOTH TABS. 2D is 20 shapes in a 5 x 4 grid, 3D is 12 in a
4 x 3 grid of larger tiles, and the two grids cover exactly the same area.
The panel is anchored above the toolbar button by its BOTTOM edge, so a
panel that changed height between tabs would move the 2D / 3D switch out
from under the finger that just pressed it.

Click a shape to make it the active tool; the toolbar's Shapes button then
shows that shape until another is chosen.
"""
from __future__ import annotations

from typing import Callable, Optional

from PySide6.QtCore import QPoint, Qt
from PySide6.QtGui import QColor
from PySide6.QtWidgets import (QColorDialog, QDialog, QFrame, QGridLayout,
                               QHBoxLayout, QLabel, QPushButton,
                               QStackedWidget, QVBoxLayout, QWidget)

from ui.canvas import shape_geometry as sg
from ui.theme.ink_palette import DEFAULT_SHAPE_COLOR, SHAPE_COLORS
from ui.theme.theme_manager import theme_mgr
from ui.util.aero import apply_aero, enable_shadow, prepare_aero_widget
from ui.util.animations import fade_in
from ui.util.audio import click_sound
from ui.util.scaling import UiScale

__all__ = ["ShapesPanel", "SHAPE_DEFS"]

#: Every shape, 2D then 3D: (tool id, label). Kept under its old name for
#: callers that only need the list.
SHAPE_DEFS = sg.SHAPES_2D + sg.SHAPES_3D

_TILE_2D = 88          # reference px; the 3D tiles are derived from it
_DOT = 30              # a colour-column swatch
_GAP = 8
_ROWS_2D = 4
_ROWS_3D = 3


def _grid_size():
    sc = UiScale.px
    w = sg.GRID_COLS_2D * sc(_TILE_2D) + (sg.GRID_COLS_2D - 1) * sc(_GAP)
    h = _ROWS_2D * sc(_TILE_2D) + (_ROWS_2D - 1) * sc(_GAP)
    return w, h


class _ShapeTile(QPushButton):
    """Icon over a caption that may wrap to two lines ("Triangular Prism")."""

    def __init__(self, kind: str, label: str, w: int, h: int, icon_px: int,
                 selected: bool):
        super().__init__()
        self.kind = kind
        self.setFixedSize(w, h)
        self.setCursor(Qt.PointingHandCursor)
        self.setToolTip(label)
        sc = UiScale.px
        t = theme_mgr.tokens
        v = QVBoxLayout(self)
        v.setContentsMargins(sc(4), sc(8), sc(4), sc(6))
        v.setSpacing(sc(4))
        icon = QLabel()
        icon.setAlignment(Qt.AlignCenter)
        icon.setAttribute(Qt.WA_TransparentForMouseEvents, True)
        icon.setPixmap(sg.shape_icon(kind, icon_px, t.text_primary))
        icon.setStyleSheet("background: transparent; border: none;")
        v.addWidget(icon, 1)
        cap = QLabel(label)
        cap.setAlignment(Qt.AlignHCenter | Qt.AlignTop)
        cap.setWordWrap(True)
        cap.setAttribute(Qt.WA_TransparentForMouseEvents, True)
        cap.setStyleSheet(
            f"color: {t.text_primary if selected else t.text_secondary};"
            f"font-size: {sc(11)}px; font-weight: {700 if selected else 500};"
            f"background: transparent; border: none;")
        v.addWidget(cap)
        self.setStyleSheet(f"""
            QPushButton {{
                background: {t.accent_selected_bg if selected else t.tile_grad};
                border: {2 if selected else 1}px solid
                        {t.accent if selected else t.border};
                border-radius: {sc(14)}px;
            }}
            QPushButton:hover {{
                border: 2px solid {t.accent};
                background: {t.bg_tile_hover};
            }}
            QPushButton:pressed {{ background: {t.fill_pressed}; }}
        """)


class ShapesPanel(QDialog):
    def __init__(self, parent=None, *,
                 current_shape: str = "rect",
                 current_label: str = "",
                 on_select: Optional[Callable[[str, str, str], None]] = None,
                 current_color: Optional[QColor] = None,
                 on_color: Optional[Callable[[QColor], None]] = None):
        # Qt.Tool (NOT Qt.Popup) — popups bypass the DWM compositor.
        super().__init__(
            parent, Qt.Tool | Qt.FramelessWindowHint | Qt.WindowStaysOnTopHint)
        self.setModal(False)
        prepare_aero_widget(self)

        try:
            from ui.util.dismiss_on_outside import install_outside_dismiss
            tb = parent._toolbar if hasattr(parent, "_toolbar") else None
            ignore = []
            if tb is not None:
                for name in ("shapes_btn", "left", "center", "right"):
                    w = getattr(tb, name, None)
                    if w is not None:
                        ignore.append(w)
            install_outside_dismiss(self, ignore_widgets=ignore)
        except Exception:
            pass

        self._on_select = on_select
        self._on_color = on_color
        self._color = QColor(current_color if current_color is not None
                             else DEFAULT_SHAPE_COLOR)
        self._current = current_shape
        self._current_label = current_label

        sc = UiScale.px
        t = theme_mgr.tokens
        self.setStyleSheet(
            f"QDialog {{ background: transparent; border: none; "
            f"border-radius: {sc(14)}px; }}")

        container = QWidget(self)
        container.setStyleSheet("QWidget { background: transparent; }")
        body = QHBoxLayout(container)
        body.setContentsMargins(sc(16), sc(14), sc(16), sc(16))
        body.setSpacing(sc(14))
        body.addWidget(self._build_colours())
        rule = QFrame()
        rule.setFixedWidth(1)
        rule.setStyleSheet(f"background: {t.border}; border: none;")
        body.addWidget(rule)
        outer = QVBoxLayout()
        outer.setContentsMargins(0, 0, 0, 0)
        outer.setSpacing(sc(12))
        body.addLayout(outer)

        head = QHBoxLayout()
        title = QLabel("Shapes")
        title.setStyleSheet(
            f"color: {t.text_primary}; font-size: {sc(16)}px; "
            f"font-weight: bold; background: transparent;")
        head.addWidget(title)
        head.addStretch(1)
        head.addWidget(self._build_switch())
        outer.addLayout(head)

        self._stack = QStackedWidget()
        self._stack.setStyleSheet("background: transparent;")
        gw, gh = _grid_size()
        self._stack.setFixedSize(gw, gh)
        self._stack.addWidget(self._build_grid(sg.SHAPES_2D, sg.GRID_COLS_2D,
                                               _ROWS_2D))
        self._stack.addWidget(self._build_grid(sg.SHAPES_3D, sg.GRID_COLS_3D,
                                               _ROWS_3D))
        outer.addWidget(self._stack)

        wrapper = QVBoxLayout(self)
        wrapper.setContentsMargins(0, 0, 0, 0)
        wrapper.addWidget(container)

        self._set_tab("3D" if current_shape in sg.IS_3D else "2D", sound=False)
        self.adjustSize()
        self.setFixedSize(self.sizeHint())

    # ── pieces ──────────────────────────────────────────────────────────
    def _build_colours(self) -> QWidget:
        """One column of swatches, spread down the panel's full height.

        Equal stretches between the dots, none outside them: the first dot
        sits on the top edge, the custom cell on the bottom edge, and the
        gaps are identical whatever height the shape grid beside it takes."""
        sc = UiScale.px
        col = QWidget()
        col.setStyleSheet("background: transparent;")
        v = QVBoxLayout(col)
        v.setContentsMargins(0, 0, 0, 0)
        v.setSpacing(0)
        d = sc(_DOT)
        col.setFixedWidth(d)
        self._color_btns = []
        for i, (name, c) in enumerate(SHAPE_COLORS):
            if i:
                v.addStretch(1)
            b = QPushButton()
            b.setFixedSize(d, d)
            b.setCursor(Qt.PointingHandCursor)
            b.setToolTip(name)
            b.clicked.connect(lambda _c=False, cc=QColor(c): self._pick_color(cc))
            self._color_btns.append((b, QColor(c)))
            v.addWidget(b, 0, Qt.AlignHCenter)
        v.addStretch(1)
        self._custom_btn = QPushButton("+")
        self._custom_btn.setFixedSize(d, d)
        self._custom_btn.setCursor(Qt.PointingHandCursor)
        self._custom_btn.setToolTip("Custom colour…")
        self._custom_btn.clicked.connect(self._pick_custom_color)
        v.addWidget(self._custom_btn, 0, Qt.AlignHCenter)
        self._refresh_colours()
        return col

    def _refresh_colours(self) -> None:
        sc = UiScale.px
        t = theme_mgr.tokens
        r = sc(_DOT) // 2
        rgb = (self._color.red(), self._color.green(), self._color.blue())
        matched = False
        for b, c in self._color_btns:
            on = (c.red(), c.green(), c.blue()) == rgb
            matched |= on
            luma = 0.299 * c.red() + 0.587 * c.green() + 0.114 * c.blue()
            b.setText("✓" if on else "")
            b.setStyleSheet(f"""
                QPushButton {{
                    background: rgb({c.red()}, {c.green()}, {c.blue()});
                    border-radius: {r}px;
                    border: {sc(3) if on else 1}px solid
                            {t.accent if on else t.swatch_ring};
                    color: {t.swatch_tick_dark if luma > 150 else t.swatch_tick_light};
                    font-size: {sc(13)}px; font-weight: 900;
                }}
                QPushButton:hover {{ border: {sc(3)}px solid {t.accent}; }}
            """)
        self._custom_btn.setStyleSheet(f"""
            QPushButton {{
                background: {t.swatch_custom_grad};
                border-radius: {r}px;
                border: {sc(3) if not matched else 1}px solid
                        {t.accent if not matched else t.swatch_ring};
                color: {t.swatch_tick_light}; font-size: {sc(16)}px; font-weight: 900;
            }}
            QPushButton:hover {{ border: {sc(3)}px solid {t.accent}; }}
        """)

    def _pick_color(self, c: QColor) -> None:
        """Recolour, and stay open: the next tap is usually the shape."""
        click_sound.play_click()
        self._color = QColor(c)
        self._refresh_colours()
        if self._on_color:
            self._on_color(QColor(c))

    def _pick_custom_color(self) -> None:
        chosen = QColorDialog.getColor(self._color, self, "Choose Shape Colour")
        if chosen.isValid():
            self._pick_color(chosen)

    def current_color(self) -> QColor:
        return QColor(self._color)

    def _build_switch(self) -> QWidget:
        sc = UiScale.px
        t = theme_mgr.tokens
        box = QWidget()
        box.setObjectName("ShapeSwitch")
        box.setAttribute(Qt.WA_StyledBackground, True)
        box.setStyleSheet(
            f"QWidget#ShapeSwitch {{ background: {t.fill_subtle}; "
            f"border: 1px solid {t.border}; border-radius: {sc(10)}px; }}")
        h = QHBoxLayout(box)
        pad = sc(3)
        h.setContentsMargins(pad, pad, pad, pad)
        h.setSpacing(pad)
        self._tab_btns = {}
        for key in ("2D", "3D"):
            b = QPushButton(key)
            b.setCursor(Qt.PointingHandCursor)
            b.setFixedSize(sc(64), sc(34))
            b.clicked.connect(lambda _c=False, k=key: self._set_tab(k))
            self._tab_btns[key] = b
            h.addWidget(b)
        return box

    def _build_grid(self, items, cols: int, rows: int) -> QWidget:
        sc = UiScale.px
        gw, gh = _grid_size()
        tw = (gw - (cols - 1) * sc(_GAP)) // cols
        th = (gh - (rows - 1) * sc(_GAP)) // rows
        # Solids carry hidden-edge dashes that need room to read; they get
        # a larger share of their (larger) tile than the flat shapes do.
        icon_px = int(min(tw, th) * (0.64 if cols == sg.GRID_COLS_3D else 0.52))
        page = QWidget()
        page.setStyleSheet("background: transparent;")
        g = QGridLayout(page)
        g.setContentsMargins(0, 0, 0, 0)
        g.setHorizontalSpacing(sc(_GAP))
        g.setVerticalSpacing(sc(_GAP))
        g.setAlignment(Qt.AlignTop | Qt.AlignLeft)
        self._tiles = getattr(self, "_tiles", {})
        for i, (kind, label) in enumerate(items):
            tile = _ShapeTile(kind, label, tw, th, icon_px,
                              selected=(kind == self._current))
            tile.clicked.connect(lambda _c=False, k=kind, l=label: self._fire(k, l))
            self._tiles[kind] = tile
            r, c = divmod(i, cols)
            g.addWidget(tile, r, c)
        return page

    # ── behaviour ───────────────────────────────────────────────────────
    def _set_tab(self, key: str, sound: bool = True) -> None:
        if sound:
            click_sound.play_click()
        self._tab = key
        self._stack.setCurrentIndex(0 if key == "2D" else 1)
        sc = UiScale.px
        t = theme_mgr.tokens
        for k, b in self._tab_btns.items():
            on = (k == key)
            b.setStyleSheet(
                f"QPushButton {{ background: {t.accent_grad if on else 'transparent'};"
                f" color: {t.text_on_accent if on else t.text_secondary};"
                f" border: none; border-radius: {sc(8)}px;"
                f" font-size: {sc(13)}px; font-weight: 700; }}"
                + ("" if on else
                   f" QPushButton:hover {{ background: {t.fill_hover};"
                   f" color: {t.text_primary}; }}"))

    def current_tab(self) -> str:
        return self._tab

    def _fire(self, kind: str, label: str) -> None:
        click_sound.play_click()
        self.close()
        if self._on_select:
            self._on_select(kind, None, label)

    def showEvent(self, event):                     # noqa: N802
        super().showEvent(event)
        apply_aero(self)
        fade_in(self)
        enable_shadow(self)

    def show_anchored(self, anchor, offset=QPoint(0, 0)):
        self.adjustSize()
        x = -(self.sizeHint().width() // 2) + (anchor.width() // 2)
        y = -self.sizeHint().height() - UiScale.px(25)
        self.move(anchor.mapToGlobal(QPoint(x, y)) + offset)
        self.show()
