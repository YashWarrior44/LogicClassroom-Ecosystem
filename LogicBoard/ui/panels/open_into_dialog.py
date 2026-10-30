"""
open_into_dialog.py — "where should this PDF go?"

Shown when a PDF or PowerPoint file is opened WITH LogicBoard (double-click
once LogicBoard is the default, or Explorer's "Open with"). The file is
headed for a board, and the only real question is which one:

    ┌──────────────────────────────────────────────────┐
    │ [PDF]  Chapter 4 — Optics.pdf                    │
    │        PDF · 12 pages                            │
    │                                                  │
    │ ┌──────────────────────────────────────────────┐ │
    │ │ ＋  New board                                │ │  ← selected first
    │ │     Every page becomes a page you can write on│ │
    │ └──────────────────────────────────────────────┘ │
    │ OR ADD IT TO A SAVED BOARD                       │
    │ [ Search boards                              ]   │
    │ ┌──────────────────────────────────────────────┐ │
    │ │ [logo] Physics 11-A          OPEN NOW        │ │
    │ │        2 days ago · 3.1 MB                   │ │
    │ │ [logo] Chemistry revision                    │ │
    │ └──────────────────────────────────────────────┘ │
    │                     [ Cancel ] [ Open in new board ]
    └──────────────────────────────────────────────────┘

A new board is the default because it is the choice that cannot go wrong:
nothing existing is touched. Picking a saved board ADDS the pages after its
last page — never over the page the teacher happened to be on.

The primary button names the outcome ("Open in new board", "Add to Physics
11-A") rather than saying "OK": on a board in front of a class, the button
is read once, from a distance, and has to say what it is about to do.

Touch first, like the Files panel: every row is a full-width target at
least 56 px tall, and a double-tap on a row is the same as tapping it and
then the primary button.
"""
from __future__ import annotations

import logging
from pathlib import Path
from typing import Optional

from PySide6.QtCore import Qt, QTimer
from PySide6.QtGui import QPixmap, QPainter, QIcon
from PySide6.QtWidgets import (
    QDialog, QFrame, QHBoxLayout, QLabel, QLineEdit, QPushButton,
    QScrollArea, QVBoxLayout, QWidget,
)

from ui.panels.file_panel import (_ElidingLabel, _human_size, _lb_card_pixmap,
                                  _when, saved_boards)
from ui.theme.theme_manager import theme_mgr
from ui.util.aero import apply_aero, enable_shadow, prepare_aero_widget
from ui.util.animations import fade_in
from ui.util.audio import click_sound
from ui.util.scaling import UiScale

__all__ = ["OpenIntoDialog", "NEW_BOARD", "document_kind"]

log = logging.getLogger(__name__)

#: The answer for "a new board". A saved board is answered with its Path.
NEW_BOARD = "new"

_ROW_H = 60
_LIST_ROWS = 4          # visible before the list scrolls
_ICON = 44


def document_kind(path: Path) -> str:
    """"PDF" / "PowerPoint" — what the header calls this file."""
    return "PDF" if Path(path).suffix.lower() == ".pdf" else "PowerPoint"


def _page_count(path: Path) -> Optional[int]:
    """Pages or slides, read from the file's own index — cheap, and None
    rather than a guess when it cannot be read."""
    try:
        from ui.util.media_cache import count_pdf_pages, count_ppt_slides
        n = (count_pdf_pages(path) if path.suffix.lower() == ".pdf"
             else count_ppt_slides(path))
        return int(n) if n else None
    except Exception:
        return None


def _type_icon(path: Path, side: int) -> QPixmap:
    """The same PDF / PowerPoint artwork Windows now shows for these files."""
    from ui.util.paths import icons_dir
    name = "pdf.svg" if path.suffix.lower() == ".pdf" else "ppt.svg"
    dpr = 2.0
    pm = QPixmap(int(side * dpr), int(side * dpr))
    pm.fill(Qt.transparent)
    try:
        from PySide6.QtSvg import QSvgRenderer
        r = QSvgRenderer(str(icons_dir() / name))
        p = QPainter(pm)
        p.setRenderHint(QPainter.Antialiasing)
        r.render(p)
        p.end()
    except Exception:
        log.debug("type icon render failed", exc_info=True)
    pm.setDevicePixelRatio(dpr)
    return pm


class _Row(QFrame):
    """One choice: the New-board card, or a saved board."""

    def __init__(self, key, title: str, subtitle: str, icon: QPixmap,
                 on_pick, on_go, *, badge: str = "", big: bool = False):
        super().__init__()
        self.key = key
        self._on_pick = on_pick
        self._on_go = on_go
        self._big = big
        self.setObjectName("OpenRow")
        self.setCursor(Qt.PointingHandCursor)
        sc = UiScale.px
        t = theme_mgr.tokens
        self.setFixedHeight(sc(76 if big else _ROW_H))

        h = QHBoxLayout(self)
        h.setContentsMargins(sc(12), sc(8), sc(14), sc(8))
        h.setSpacing(sc(12))

        ic = QLabel()
        ic.setFixedSize(sc(_ICON), sc(_ICON))
        ic.setAlignment(Qt.AlignCenter)
        ic.setPixmap(icon)
        ic.setStyleSheet("background: transparent; border: none;")
        h.addWidget(ic)

        txt = QVBoxLayout()
        txt.setSpacing(sc(2))
        self._title = _ElidingLabel(title)
        self._title.setStyleSheet(
            f"font-size: {sc(15 if big else 14)}px; font-weight: "
            f"{700 if big else 600}; color: {t.text_primary}; "
            f"background: transparent; border: none;")
        txt.addWidget(self._title)
        if subtitle:
            sub = _ElidingLabel(subtitle)
            sub.setStyleSheet(
                f"font-size: {sc(12)}px; color: {t.text_secondary}; "
                f"background: transparent; border: none;")
            txt.addWidget(sub)
        h.addLayout(txt, 1)

        if badge:
            chip = QLabel(badge)
            chip.setStyleSheet(
                f"font-size: {sc(9)}px; font-weight: 800; letter-spacing: 0.5px;"
                f"color: {t.accent}; background: {t.accent_muted};"
                f"border: none; border-radius: {sc(4)}px;"
                f"padding: {sc(3)}px {sc(7)}px;")
            h.addWidget(chip, 0, Qt.AlignVCenter)

        self.set_selected(False)

    def set_selected(self, on: bool) -> None:
        t = theme_mgr.tokens
        sc = UiScale.px
        bg = t.accent_muted if on else (t.bg_card if self._big else "transparent")
        bc = t.accent if on else (t.border_subtle if self._big else "transparent")
        self.setStyleSheet(f"""
            QFrame#OpenRow {{
                background: {bg};
                border: 1px solid {bc};
                border-radius: {sc(12)}px;
            }}
            QFrame#OpenRow:hover {{
                background: {t.accent_muted if on else t.fill_hover};
            }}
        """)

    def mousePressEvent(self, e):                       # noqa: N802
        if e.button() == Qt.LeftButton:
            click_sound.play_click()
            self._on_pick(self.key)
        e.accept()

    def mouseDoubleClickEvent(self, e):                 # noqa: N802
        if e.button() == Qt.LeftButton:
            self._on_pick(self.key)
            self._on_go()
        e.accept()


class OpenIntoDialog(QDialog):
    """Ask where an opened PDF / deck should go. ``choice()`` afterwards is
    ``NEW_BOARD``, a saved board's ``Path``, or None for Cancel."""

    def __init__(self, file_path: Path, parent=None, *,
                 current_board: Optional[Path] = None,
                 boards: Optional[list[Path]] = None):
        super().__init__(parent, Qt.WindowType.FramelessWindowHint
                         | Qt.WindowType.Dialog
                         | Qt.WindowType.WindowStaysOnTopHint)
        self.setModal(True)
        prepare_aero_widget(self)
        self._file = Path(file_path)
        self._current = Path(current_board) if current_board else None
        self._boards = [b for b in (saved_boards() if boards is None else boards)
                        if b.is_file()]          # .lb files, not legacy folders
        self._selected = NEW_BOARD
        self._choice = None
        self._rows: list[_Row] = []

        t = theme_mgr.tokens
        sc = UiScale.px
        self.setFixedWidth(sc(560))
        self.setStyleSheet(f"""
            QDialog {{
                background-color: {t.dialog_bg};
                border: 1px solid {t.stroke_default};
                border-radius: {t.radius_xl}px;
            }}
            QLabel {{ color: {t.text_primary}; background: transparent; border: none; }}
            QPushButton#secondary {{
                background: {t.fill_default}; color: {t.text_primary};
                border: 1px solid {t.stroke_default}; border-radius: {t.radius_sm}px;
                padding: 0 {sc(18)}px; font-size: {sc(13)}px; font-weight: 600;
            }}
            QPushButton#secondary:hover {{ background: {t.bg_card_hover}; }}
            QPushButton#primary {{
                background: {t.accent_grad}; color: {t.text_on_accent};
                border: none; border-radius: {t.radius_sm}px;
                padding: 0 {sc(20)}px; font-size: {sc(13)}px; font-weight: 700;
            }}
            QPushButton#primary:hover   {{ background: {t.accent_grad_hover}; }}
            QPushButton#primary:pressed {{ background: {t.accent_grad_pressed}; }}
        """)

        root = QVBoxLayout(self)
        root.setContentsMargins(sc(24), sc(22), sc(24), sc(20))
        root.setSpacing(sc(12))

        root.addLayout(self._build_header())
        self._new_row = _Row(
            NEW_BOARD, "New board",
            "Every page becomes a page you can write on",
            self._plus_icon(sc(_ICON)), self._pick, self._go, big=True)
        self._rows.append(self._new_row)
        root.addWidget(self._new_row)

        if self._boards:
            cap = QLabel("OR ADD IT TO A SAVED BOARD")
            cap.setStyleSheet(
                f"font-size: {sc(11)}px; font-weight: 700; letter-spacing: 0.6px;"
                f"color: {t.text_secondary};")
            root.addSpacing(sc(2))
            root.addWidget(cap)
            if len(self._boards) > _LIST_ROWS:
                root.addWidget(self._build_search())
            root.addWidget(self._build_list())

        root.addSpacing(sc(4))
        root.addLayout(self._build_footer())
        self._refresh()
        self.adjustSize()

    # ── pieces ──────────────────────────────────────────────────────────
    def _build_header(self) -> QHBoxLayout:
        t = theme_mgr.tokens
        sc = UiScale.px
        row = QHBoxLayout()
        row.setSpacing(sc(14))
        ic = QLabel()
        ic.setFixedSize(sc(52), sc(52))
        ic.setPixmap(_type_icon(self._file, sc(52)))
        row.addWidget(ic, 0, Qt.AlignTop)

        col = QVBoxLayout()
        col.setSpacing(sc(3))
        name = _ElidingLabel(self._file.name)
        name.setStyleSheet(
            f"font-size: {sc(17)}px; font-weight: 800; color: {t.text_primary};")
        col.addWidget(name)
        n = _page_count(self._file)
        unit = "page" if self._file.suffix.lower() == ".pdf" else "slide"
        bits = [document_kind(self._file)]
        if n:
            bits.append(f"{n} {unit}{'s' if n != 1 else ''}")
        meta = QLabel("  ·  ".join(bits))
        meta.setStyleSheet(f"font-size: {sc(12)}px; color: {t.text_secondary};")
        col.addWidget(meta)
        ask = QLabel("Where should it go?")
        ask.setStyleSheet(
            f"font-size: {sc(13)}px; font-weight: 600; color: {t.text_primary};"
            f"padding-top: {sc(6)}px;")
        col.addWidget(ask)
        row.addLayout(col, 1)
        return row

    def _build_search(self) -> QLineEdit:
        t = theme_mgr.tokens
        sc = UiScale.px
        box = QLineEdit()
        box.setPlaceholderText("Search boards")
        box.setClearButtonEnabled(True)
        box.setFixedHeight(sc(38))
        box.setStyleSheet(f"""
            QLineEdit {{
                background: {t.bg_input}; color: {t.text_primary};
                border: 1px solid {t.border}; border-radius: {sc(8)}px;
                padding: 0 {sc(12)}px; font-size: {sc(13)}px;
            }}
            QLineEdit:focus {{ border: 1px solid {t.accent}; }}
        """)
        box.textChanged.connect(self._filter)
        self._search = box
        return box

    def _build_list(self) -> QScrollArea:
        t = theme_mgr.tokens
        sc = UiScale.px
        scroll = QScrollArea()
        scroll.setWidgetResizable(True)
        scroll.setFrameShape(QFrame.NoFrame)
        shown = min(len(self._boards), _LIST_ROWS)
        scroll.setFixedHeight(shown * sc(_ROW_H) + (shown - 1) * sc(4))
        try:
            from ui.util.touch_scroll import setup_scroll
            setup_scroll(scroll)
        except Exception:
            pass
        inner = QWidget()
        inner.setStyleSheet("background: transparent;")
        v = QVBoxLayout(inner)
        v.setContentsMargins(0, 0, sc(6) if len(self._boards) > _LIST_ROWS else 0, 0)
        v.setSpacing(sc(4))
        logo = _lb_card_pixmap(sc(36)) or QPixmap()
        for b in self._boards:
            bits = []
            try:
                st = b.stat()
                bits = [w for w in (_when(st.st_mtime), _human_size(st.st_size)) if w]
            except OSError:
                pass
            is_open = self._current is not None and _same(b, self._current)
            row = _Row(b, b.stem, "  ·  ".join(bits), logo, self._pick, self._go,
                       badge="OPEN NOW" if is_open else "")
            self._rows.append(row)
            v.addWidget(row)
        v.addStretch(1)
        scroll.setWidget(inner)
        self._list_scroll = scroll
        return scroll

    def _build_footer(self) -> QHBoxLayout:
        sc = UiScale.px
        row = QHBoxLayout()
        row.setSpacing(sc(10))
        row.addStretch(1)
        cancel = QPushButton("Cancel")
        cancel.setObjectName("secondary")
        cancel.setCursor(Qt.PointingHandCursor)
        cancel.setFixedHeight(sc(42))
        cancel.clicked.connect(self.reject)
        row.addWidget(cancel)
        self._go_btn = QPushButton("")
        self._go_btn.setObjectName("primary")
        self._go_btn.setCursor(Qt.PointingHandCursor)
        self._go_btn.setFixedHeight(sc(42))
        self._go_btn.setDefault(True)
        self._go_btn.clicked.connect(self._go)
        row.addWidget(self._go_btn)
        return row

    @staticmethod
    def _plus_icon(side: int) -> QPixmap:
        """A rounded accent tile with a plus — drawn, not an emoji, so it
        is the same on every machine."""
        from PySide6.QtGui import QColor, QPen, QLinearGradient
        t = theme_mgr.tokens
        dpr = 2.0
        pm = QPixmap(int(side * dpr), int(side * dpr))
        pm.setDevicePixelRatio(dpr)
        pm.fill(Qt.transparent)
        p = QPainter(pm)
        p.setRenderHint(QPainter.Antialiasing)
        acc = theme_mgr.qcolor(t.accent)
        g = QLinearGradient(0, 0, 0, side)
        g.setColorAt(0, acc.lighter(118))
        g.setColorAt(1, acc.darker(112))
        p.setPen(Qt.NoPen)
        p.setBrush(g)
        p.drawRoundedRect(0, 0, side, side, side * 0.26, side * 0.26)
        pen = QPen(QColor(255, 255, 255))
        pen.setWidthF(side * 0.085)
        pen.setCapStyle(Qt.RoundCap)
        p.setPen(pen)
        c, r = side / 2, side * 0.2
        p.drawLine(int(c - r), int(c), int(c + r), int(c))
        p.drawLine(int(c), int(c - r), int(c), int(c + r))
        p.end()
        return pm

    # ── behaviour ───────────────────────────────────────────────────────
    def _pick(self, key) -> None:
        self._selected = key
        self._refresh()

    def _go(self) -> None:
        click_sound.play_click()
        self._choice = self._selected
        self.accept()

    def _filter(self, text: str) -> None:
        needle = (text or "").strip().lower()
        for r in self._rows:
            if r.key == NEW_BOARD:
                continue
            r.setVisible(not needle or needle in Path(r.key).stem.lower())

    def _refresh(self) -> None:
        for r in self._rows:
            r.set_selected(_same_key(r.key, self._selected))
        if self._selected == NEW_BOARD:
            self._go_btn.setText("Open in new board")
        else:
            name = Path(self._selected).stem
            if len(name) > 22:
                name = name[:21] + "…"
            self._go_btn.setText(f"Add to {name}")

    def choice(self):
        """``NEW_BOARD``, a board ``Path``, or None (cancelled)."""
        return self._choice

    @classmethod
    def ask(cls, file_path: Path, parent=None, *,
            current_board: Optional[Path] = None):
        dlg = cls(file_path, parent, current_board=current_board)
        dlg.exec()
        return dlg.choice()

    # ── window plumbing (same as AeroAlert) ─────────────────────────────
    def keyPressEvent(self, e):                          # noqa: N802
        if e.key() == Qt.Key_Escape:
            self.reject()
            return
        if e.key() in (Qt.Key_Return, Qt.Key_Enter):
            self._go()
            return
        super().keyPressEvent(e)

    def showEvent(self, event):                          # noqa: N802
        super().showEvent(event)
        try:
            geo = (self.parent().screen() if self.parent() else self.screen()).geometry()
            self.move(geo.center() - self.rect().center())
        except Exception:
            pass
        apply_aero(self)
        enable_shadow(self)
        fade_in(self)
        self.raise_()
        self.activateWindow()
        # Focus on the answer, not the search box: a focused field on a
        # touch board summons the on-screen keyboard over the very list
        # the teacher is trying to read.
        self._go_btn.setFocus(Qt.OtherFocusReason)


def _same(a: Path, b: Path) -> bool:
    try:
        return Path(a).resolve() == Path(b).resolve()
    except OSError:
        return str(a).lower() == str(b).lower()


def _same_key(a, b) -> bool:
    if isinstance(a, str) or isinstance(b, str):
        return a == b
    return _same(a, b)
