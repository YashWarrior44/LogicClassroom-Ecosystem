"""
paste_chip.py — the "Paste" chip that appears after Copy.

A small card pinned to the main window's top-right corner while LogicBoard's
clipboard holds something:

    ┌───────────────────────────────────────────┐
    │ [copy]  2 items copied   [ Paste ]   ✕    │
    └───────────────────────────────────────────┘

Why a chip and not a menu item: a smart board has no right-click and usually
no keyboard, so "copy here, paste there" needs a Paste button that is still
on screen after the teacher has moved to another page — the selection menu
that held Copy is gone by then, because nothing is selected. The chip stays
until the clipboard is dismissed (✕) or the board changes, and Paste can be
pressed any number of times.

Top-RIGHT because the toast owns the top-left, and the toolbar the bottom.

Unlike the toast it takes input, so it is NOT transparent to the mouse — but
it is small and sits at the edge of the board, out of the writing area.
"""
from __future__ import annotations

from typing import Callable, Optional

from PySide6.QtCore import Qt, QPropertyAnimation, QEasingCurve
from PySide6.QtWidgets import (QGraphicsOpacityEffect, QHBoxLayout, QLabel,
                               QPushButton, QWidget)

from ui.theme.theme_manager import theme_mgr
from ui.util.audio import click_sound
from ui.util.scaling import UiScale

__all__ = ["PasteChip"]

_MARGIN = 18


class PasteChip(QWidget):
    def __init__(self, parent: QWidget, *, on_paste: Callable[[], None],
                 on_dismiss: Callable[[], None]) -> None:
        super().__init__(parent)
        self.setAttribute(Qt.WA_StyledBackground, True)
        self.setAttribute(Qt.WA_AcceptTouchEvents, True)
        self.setObjectName("PasteChip")
        self._on_paste = on_paste
        self._on_dismiss = on_dismiss
        sc = UiScale.px
        t = theme_mgr.tokens

        lay = QHBoxLayout(self)
        lay.setContentsMargins(sc(14), sc(8), sc(8), sc(8))
        lay.setSpacing(sc(10))

        from ui.panels.selection_menu import _render_svg_icon
        icon = QLabel()
        icon.setPixmap(_render_svg_icon(
            "copy.svg", sc(20), color=theme_mgr.qcolor(t.text_secondary)))
        icon.setFixedSize(sc(22), sc(22))
        lay.addWidget(icon)

        self._label = QLabel("")
        lay.addWidget(self._label)

        self._paste = QPushButton("Paste")
        self._paste.setObjectName("PastePrimary")
        self._paste.setCursor(Qt.PointingHandCursor)
        self._paste.setFixedHeight(sc(38))
        self._paste.setMinimumWidth(sc(84))
        self._paste.clicked.connect(self._fire_paste)
        lay.addWidget(self._paste)

        close = QPushButton("✕")
        close.setObjectName("PasteClose")
        close.setToolTip("Clear the clipboard")
        close.setCursor(Qt.PointingHandCursor)
        close.setFixedSize(sc(34), sc(34))
        close.clicked.connect(self._fire_dismiss)
        lay.addWidget(close)

        self.setStyleSheet(f"""
            QWidget#PasteChip {{
                background: {t.card_grad};
                border: 1px solid {t.stroke_strong};
                border-radius: {sc(t.radius_lg)}px;
            }}
            QLabel {{
                color: {t.text_primary}; background: transparent; border: none;
                font-size: {sc(13)}px; font-weight: 600;
            }}
            QPushButton#PastePrimary {{
                background: {t.accent_grad}; color: {t.text_on_accent};
                border: none; border-radius: {sc(t.radius_sm)}px;
                font-size: {sc(13)}px; font-weight: 700;
                padding: 0 {sc(16)}px;
            }}
            QPushButton#PastePrimary:hover   {{ background: {t.accent_grad_hover}; }}
            QPushButton#PastePrimary:pressed {{ background: {t.accent_grad_pressed}; }}
            QPushButton#PasteClose {{
                background: transparent; color: {t.text_secondary};
                border: none; border-radius: {sc(17)}px;
                font-size: {sc(13)}px;
            }}
            QPushButton#PasteClose:hover {{
                background: {t.fill_hover}; color: {t.text_primary};
            }}
        """)

        self._fx = QGraphicsOpacityEffect(self)
        self._fx.setOpacity(1.0)
        self.setGraphicsEffect(self._fx)
        self._anim: Optional[QPropertyAnimation] = None
        self.hide()

    # ── public ──────────────────────────────────────────────────────────
    def present(self, count: int) -> None:
        """Show (or refresh) the chip for ``count`` copied items."""
        self._label.setText(
            f"{count} item{'s' if count != 1 else ''} copied")
        self.reposition()
        was_visible = self.isVisible()
        self.show()
        self.raise_()
        if not was_visible:
            self._fade(0.0, 1.0)

    def reposition(self) -> None:
        """Top-right of the parent. Called on show and on every parent
        resize, so it follows the window between screens."""
        p = self.parentWidget()
        if p is None:
            return
        self.adjustSize()
        m = UiScale.px(_MARGIN)
        self.move(p.width() - self.width() - m, m)

    # ── internals ───────────────────────────────────────────────────────
    def _fire_paste(self) -> None:
        click_sound.play_click()
        self._on_paste()

    def _fire_dismiss(self) -> None:
        click_sound.play_click()
        self.hide()
        self._on_dismiss()

    def _fade(self, a: float, b: float) -> None:
        if self._anim is not None:
            self._anim.stop()
        anim = QPropertyAnimation(self._fx, b"opacity", self)
        anim.setDuration(160)
        anim.setStartValue(a)
        anim.setEndValue(b)
        anim.setEasingCurve(QEasingCurve.OutCubic)
        anim.start()
        self._anim = anim
