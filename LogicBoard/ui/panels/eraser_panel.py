"""
eraser_panel.py — the Eraser popover: Simple, Object and Lasso.

    ┌──────────────────────────────────────────────────┐
    │                      Eraser                      │
    │  ┌───────┐  ERASER SIZE                  50 px   │
    │  │Simple │  ━━━━━━━●━━━━━━━━━━━━━━━━━━━━━━━━━━   │  (lasso: a note)
    │  ├───────┤  PREVIEW                              │
    │  │Object │  ┌────────────────────────────────┐   │
    │  │       │  │   the eraser at its TRUE size  │   │
    │  ├───────┤  │     over a sample stroke       │   │
    │  │ Lasso │  └────────────────────────────────┘   │
    │  └───────┘  what this mode does                  │
    └──────────────────────────────────────────────────┘

The same family as the Drawing Tool panel (pen_panel.py): the modes stacked
on the left, everything about the chosen mode on the right, the right column
``_col_w()`` wide, and the two columns ending level — the tile side is worked
out from the right column's real height.

THE PREVIEW IS TRUE SIZE. The ring is drawn at the diameter it will have on
the board at 100 % zoom, and the preview is tall enough for the largest size
the slider allows, so 200 px looks like 200 px rather than a capped circle
that stopped growing halfway along the slider.

ONE SIZE IN EVERY MODE. Lasso has no size — the loop is drawn — so its
slider row is swapped for a note in a slot of the same height, and the
panel never moves under the stylus.
"""
from __future__ import annotations

from typing import Callable, Optional

from PySide6.QtCore import QPoint, QPointF, QSize, Qt
from PySide6.QtGui import QBrush, QColor, QPainter, QPainterPath, QPen, QPixmap
from PySide6.QtWidgets import (QDialog, QHBoxLayout, QLabel, QStackedWidget,
                               QToolButton, QVBoxLayout, QWidget)

from ui.panels.pen_panel import _col_w, _tile_w
from ui.theme.theme_manager import theme_mgr
from ui.util.aero import apply_aero, enable_shadow, prepare_aero_widget
from ui.util.audio import click_sound
from ui.util.circle_slider import CircleHandleSlider
from ui.util.icon_loader import tool_icon
from ui.util.scaling import UiScale

__all__ = ["EraserPanel"]

_TILE_GAP = 8
_COL_GAP = 16         # between the tile column and the right column
_SIZE_MIN = 5
_SIZE_MAX = 200       # the preview is built tall enough for this, unscaled
_PREVIEW_PAD = 14     # clear space round the biggest ring

_MODES = ("pixel", "object", "lasso")
_TILES = (("pixel", "Simple", "eraser"),
          ("object", "Object", "eraser_object"),
          ("lasso", "Lasso", "eraser_lasso"))
_DESCRIPTIONS = {
    "pixel":  "Rubs out exactly where it touches, splitting strokes along "
              "its path. Imported media is never touched.",
    "object": "Removes a whole stroke or shape the moment it touches it. "
              "Imported media is protected unless Settings says otherwise.",
    "lasso":  "Draw a loop around what you want gone — everything inside "
              "it is erased in one go. Undo brings it all back.",
}

# The on-canvas rings — keep in step with CanvasWidget's eraser ring.
_RING_PIXEL = QColor(154, 208, 255)
_RING_OBJECT = QColor(255, 80, 80)
_LASSO_RED = QColor(255, 90, 95)


class EraserPanel(QDialog):
    def __init__(self, parent=None, *, eraser_size: int = 50,
                 eraser_mode: str = "pixel",
                 on_size_change: Optional[Callable[[int], None]] = None,
                 on_mode_change: Optional[Callable[[str], None]] = None):
        super().__init__(
            parent, Qt.Tool | Qt.FramelessWindowHint | Qt.WindowStaysOnTopHint)
        self.setModal(False)
        prepare_aero_widget(self)

        # Our own outside-tap dismissal as well as main_window's (see
        # pen_panel). The eraser button is ignored so it can toggle us shut.
        try:
            from ui.util.dismiss_on_outside import install_outside_dismiss
            tb = parent._toolbar if hasattr(parent, "_toolbar") else None
            ignore = [w for w in (getattr(tb, n, None) for n in
                                  ("eraser_btn", "left", "center", "right"))
                      if w is not None] if tb is not None else []
            install_outside_dismiss(self, ignore_widgets=ignore)
        except Exception:
            pass

        self._size = int(eraser_size)
        self._mode = eraser_mode if eraser_mode in _MODES else "pixel"
        self._on_size = on_size_change
        self._on_mode = on_mode_change

        self._build()
        self._refresh_mode()
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

        self.setStyleSheet(
            f"QDialog {{ background: transparent; border: none; "
            f"border-radius: {t.radius_xl}px; }}")
        container = QWidget(self)
        container.setStyleSheet(
            f"QWidget {{ background: transparent; color: {t.text_primary}; }}")
        lay = QVBoxLayout(container)
        m = sc(18)
        lay.setContentsMargins(m, sc(16), m, m)
        lay.setSpacing(0)

        title = QLabel("Eraser")
        title.setAlignment(Qt.AlignCenter)
        title.setStyleSheet(
            f"color: {t.text_primary}; font-size: {sc(17)}px; "
            f"font-weight: 800; background: transparent;")
        lay.addWidget(title)
        lay.addSpacing(sc(14))

        body = QHBoxLayout()
        body.setContentsMargins(0, 0, 0, 0)
        body.setSpacing(sc(_COL_GAP))

        # ── LEFT: the three modes ────────────────────────────────────────
        left = QVBoxLayout()
        left.setContentsMargins(0, 0, 0, 0)
        left.setSpacing(gap)
        self._mode_btns: dict[str, QToolButton] = {}
        for mode, label, icon in _TILES:
            b = QToolButton()
            b.setText(label)
            b.setToolButtonStyle(Qt.ToolButtonTextUnderIcon)
            b.setIcon(tool_icon(icon, sc(36), t.text_primary))
            b.setIconSize(QSize(sc(36), sc(36)))
            b.setCursor(Qt.PointingHandCursor)
            b.clicked.connect(lambda _c=False, mm=mode: self._set_mode(mm))
            self._mode_btns[mode] = b
            left.addWidget(b)
        # Kept under their old names for callers and tests.
        self._pixel_btn = self._mode_btns["pixel"]
        self._object_btn = self._mode_btns["object"]
        self._lasso_btn = self._mode_btns["lasso"]
        body.addLayout(left)

        # ── RIGHT: size, preview, what the mode does ─────────────────────
        right_box = QWidget()
        right_box.setStyleSheet("background: transparent;")
        right = QVBoxLayout(right_box)
        right.setContentsMargins(0, 0, 0, 0)
        right.setSpacing(0)

        # Size, or (lasso) a note in the same slot.
        self._size_slot = QStackedWidget()
        self._size_slot.setStyleSheet("background: transparent;")
        size_page = QWidget()
        sp = QVBoxLayout(size_page)
        sp.setContentsMargins(0, 0, 0, 0)
        sp.setSpacing(sc(2))
        head = QHBoxLayout()
        head.setContentsMargins(0, 0, 0, 0)
        head.addWidget(self._heading("ERASER SIZE"))
        head.addStretch(1)
        self._val = QLabel()
        self._val.setAlignment(Qt.AlignRight | Qt.AlignVCenter)
        self._val.setStyleSheet(
            f"color: {t.accent}; font-size: {sc(14)}px; font-weight: 800; "
            f"background: transparent;")
        head.addWidget(self._val)
        sp.addLayout(head)
        self._slider = CircleHandleSlider()
        self._slider.setFixedWidth(col_w)
        self._slider.setRange(_SIZE_MIN, _SIZE_MAX)
        self._slider.setValue(self._size)
        self._slider.valueChanged.connect(self._on_slider)
        sp.addWidget(self._slider)
        self._size_slot.addWidget(size_page)

        lasso_page = QWidget()
        lp = QVBoxLayout(lasso_page)
        lp.setContentsMargins(0, 0, 0, 0)
        lp.setSpacing(sc(4))
        lp.addWidget(self._heading("LASSO"))
        self._lasso_note = QLabel("No size to set — just draw a loop around "
                                  "what you want to erase.")
        self._lasso_note.setWordWrap(True)
        self._lasso_note.setStyleSheet(
            f"color: {t.text_primary}; font-size: {sc(12)}px; "
            f"font-weight: 600; background: transparent;")
        lp.addWidget(self._lasso_note)
        lp.addStretch(1)
        self._size_slot.addWidget(lasso_page)
        # Both pages share one height, so switching to Lasso cannot resize
        # the panel.
        self._size_slot.setFixedSize(col_w, size_page.sizeHint().height())
        right.addWidget(self._size_slot)
        right.addSpacing(sc(10))

        right.addWidget(self._heading("PREVIEW"))
        right.addSpacing(sc(6))
        self._preview = QLabel()
        self._preview.setAlignment(Qt.AlignCenter)
        self._preview_min_h = _SIZE_MAX + 2 * sc(_PREVIEW_PAD)
        self._preview.setFixedSize(col_w, self._preview_min_h)
        self._preview.setStyleSheet(
            f"QLabel {{ background: {t.card_grad}; border: 1px solid "
            f"{t.border}; border-radius: {sc(14)}px; }}")
        right.addWidget(self._preview)
        right.addSpacing(sc(10))

        self._mode_desc = QLabel()
        self._mode_desc.setWordWrap(True)
        self._mode_desc.setAlignment(Qt.AlignLeft | Qt.AlignTop)
        self._mode_desc.setFixedWidth(col_w)
        self._mode_desc.setStyleSheet(
            f"color: {t.text_secondary}; font-size: {sc(11)}px; "
            f"background: transparent;")
        # Room for the longest description, so the text never resizes it.
        fm = self._mode_desc.fontMetrics()
        longest = max(_DESCRIPTIONS.values(), key=len)
        need = fm.boundingRect(0, 0, col_w, 1000, Qt.TextWordWrap, longest)
        self._mode_desc.setFixedHeight(need.height() + sc(2))
        right.addWidget(self._mode_desc)
        body.addWidget(right_box, 0, Qt.AlignTop)
        lay.addLayout(body)

        # ── level the two columns ────────────────────────────────────────
        # The pen panel's tile width; the height from the right column, with
        # the preview taking the rounding remainder so 3 tiles + 2 gaps ==
        # the right column exactly.
        right_box.adjustSize()
        right_h = right_box.sizeHint().height()
        th = (right_h - 2 * gap) // 3
        left_h = 3 * th + 2 * gap
        self._preview.setFixedHeight(self._preview_min_h + (left_h - right_h))
        for b in self._mode_btns.values():
            b.setFixedSize(_tile_w(), th)
        right_box.setFixedHeight(left_h)

        outer = QVBoxLayout(self)
        outer.setContentsMargins(0, 0, 0, 0)
        outer.addWidget(container)

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

    def _set_mode(self, m: str) -> None:
        if m not in _MODES:
            return
        click_sound.play_click()
        self._mode = m
        self._refresh_mode()
        if self._on_mode:
            self._on_mode(m)

    def _refresh_mode(self) -> None:
        sc = UiScale.px
        t = theme_mgr.tokens
        for mode, b in self._mode_btns.items():
            on = mode == self._mode
            b.setStyleSheet(f"""
                QToolButton {{
                    background: {t.accent_selected_bg if on else t.tile_grad};
                    color: {t.text_primary};
                    border: {2 if on else 1}px solid {t.accent if on else t.border};
                    border-radius: {sc(16)}px;
                    font-size: {sc(11.5)}px; font-weight: 700;
                    padding: {sc(8)}px {sc(4)}px {sc(6)}px {sc(4)}px;
                }}
                QToolButton:hover {{
                    background: {t.bg_tile_hover}; border: 2px solid {t.accent};
                }}
            """)
        self._size_slot.setCurrentIndex(1 if self._mode == "lasso" else 0)
        self._val.setText(f"{self._size} px")
        self._mode_desc.setText(_DESCRIPTIONS[self._mode])
        self._draw_preview()

    def mode(self) -> str:
        return self._mode

    # ================================================================
    # Size
    # ================================================================

    def _on_slider(self, v: int) -> None:
        self._size = int(v)
        self._val.setText(f"{v} px")
        if self._on_size:
            self._on_size(v)
        self._draw_preview()

    def set_size_silent(self, v: int) -> None:
        """Show a size set elsewhere (a mode switch restoring that mode's
        own size) WITHOUT firing on_size_change, which would overwrite the
        new mode's saved value with the old one's."""
        self._size = int(v)
        self._val.setText(f"{self._size} px")
        self._slider.blockSignals(True)
        try:
            self._slider.setValue(self._size)
        finally:
            self._slider.blockSignals(False)
        self._draw_preview()

    # ================================================================
    # Preview
    # ================================================================

    def _draw_preview(self) -> None:
        """A sample stroke with the eraser on it, as it will look on the
        board: the ring at its TRUE size (the preview is built tall enough
        for the largest) for Simple and Object, a dashed loop around the
        stroke for Lasso."""
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
            t = theme_mgr.tokens
            ink = theme_mgr.qcolor(t.accent)
            cy = ph / 2.0

            def squiggle(x0, x1, amp):
                path = QPainterPath(QPointF(x0, cy))
                n = 6
                for i in range(1, n + 1):
                    xa = x0 + (x1 - x0) * (i - 0.5) / n
                    xb = x0 + (x1 - x0) * i / n
                    path.quadTo(QPointF(xa, cy + (amp if i % 2 else -amp)),
                                QPointF(xb, cy))
                return path

            if self._mode == "lasso":
                p.setPen(QPen(ink, 5, Qt.SolidLine, Qt.RoundCap))
                p.setBrush(Qt.NoBrush)
                p.drawPath(squiggle(pw * 0.30, pw * 0.70, ph * 0.08))
                loop = QPainterPath()
                loop.addEllipse(QPointF(pw / 2, cy), pw * 0.30, ph * 0.26)
                fill = QColor(_LASSO_RED)
                fill.setAlpha(34)
                p.setPen(Qt.NoPen)
                p.setBrush(QBrush(fill))
                p.drawPath(loop)
                pen = QPen(_LASSO_RED, 2.4, Qt.DashLine)
                pen.setDashPattern([5, 4])
                p.setPen(pen)
                p.setBrush(Qt.NoBrush)
                p.drawPath(loop)
            else:
                # The stroke, with a gap where the eraser has been through.
                r = max(2.5, self._size / 2.0)
                ex = pw / 2.0
                p.setPen(QPen(ink, 5, Qt.SolidLine, Qt.RoundCap))
                p.setBrush(Qt.NoBrush)
                p.save()
                clip = QPainterPath()
                clip.addRect(0, 0, pw, ph)
                hole = QPainterPath()
                hole.addEllipse(QPointF(ex, cy), r, r)
                if self._mode == "pixel":
                    p.setClipPath(clip.subtracted(hole))
                    p.drawPath(squiggle(pw * 0.06, pw * 0.94, ph * 0.08))
                else:
                    # Object: the whole stroke under the ring is gone; show
                    # its ghost so it reads as "this one goes".
                    ghost = QColor(ink)
                    ghost.setAlpha(60)
                    p.setPen(QPen(ghost, 5, Qt.DashLine, Qt.RoundCap))
                    p.drawPath(squiggle(pw * 0.06, pw * 0.94, ph * 0.08))
                p.restore()
                ring = _RING_PIXEL if self._mode == "pixel" else _RING_OBJECT
                p.setPen(QPen(ring, 2.5))
                p.setBrush(Qt.NoBrush)
                p.drawEllipse(QPointF(ex, cy), r, r)
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

    def show_anchored(self, anchor, offset=QPoint(0, 0)):
        size = self.size()
        x = -(size.width() // 2) + (anchor.width() // 2)
        y = -size.height() - UiScale.px(25)
        self.move(anchor.mapToGlobal(QPoint(x, y)) + offset)
        self.show()
