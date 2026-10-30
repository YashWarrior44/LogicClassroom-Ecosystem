"""
ui/panels/aero_alert.py — a reusable, macOS-style alert / warning window.

Frameless, aero-backed, rounded, no title bar — built on LogicBoard's own
utilities (prepare_aero_widget / apply_aero / enable_shadow / scale_in /
UiScale / theme_mgr) so every alert in the app shares one glassy look.

macOS layout: a big symbol up top (⚠️ / 💀 / ℹ️ / ✅ / ❓) in a soft tinted
halo, a bold centered title, a centered message, and buttons across the bottom
(the default/primary button highlighted, on the right).

Drop-in for QMessageBox — the static helpers mirror its API:

    AeroAlert.warning(self, "PDF Import", "No pages could be imported.")
    AeroAlert.critical(self, "Crash", "Something went very wrong.")     # 💀
    AeroAlert.information(self, "Done", "Export finished.")
    AeroAlert.success(self, "Saved", "Your board was saved.")
    if AeroAlert.question(self, "Discard?", "Lose unsaved changes?"):
        ...

`question`/`ask` return a bool; the others return the clicked button index.
"""
from __future__ import annotations

from PySide6.QtCore import Qt
from PySide6.QtWidgets import (
    QDialog, QWidget, QVBoxLayout, QHBoxLayout, QLabel, QPushButton,
)

from ui.util.scaling     import UiScale
from ui.util.aero     import apply_aero, enable_shadow, prepare_aero_widget
from ui.util.animations  import fade_in
from ui.theme.theme_manager import theme_mgr

__all__ = ["AeroAlert"]


# kind → (emoji, accent-color token name)
_KIND = {
    "warning":     ("💀", "warning"),
    "danger":      ("💀", "danger"),
    "critical":    ("💀", "danger"),
    "info":        ("ℹ️", "accent"),
    "information": ("ℹ️", "accent"),
    "success":     ("😎✅", "success"),
    "question":    ("💀❓", "accent"),
}


class AeroAlert(QDialog):
    # --- QMessageBox-compatible constants (so call sites can swap names) ---
    Ok          = 0x0001
    Cancel      = 0x0002
    Yes         = 0x0004
    No          = 0x0008
    # icon-kind aliases
    Warning     = "warning"
    Information = "info"
    Critical    = "danger"
    Question    = "question"
    # button roles (compat only)
    AcceptRole      = 0
    RejectRole      = 1
    DestructiveRole = 2

    def __init__(self, parent=None, *, kind="warning", title="", message="",
                 buttons=None, default=-1):
        super().__init__(parent, Qt.WindowType.FramelessWindowHint
                         | Qt.WindowType.Dialog
                         | Qt.WindowType.WindowStaysOnTopHint)
        self.setModal(True)
        prepare_aero_widget(self)
        self._result_index = -1

        buttons = list(buttons) if buttons else ["OK"]
        if default < 0:
            default = len(buttons) - 1   # default = rightmost (macOS convention)

        emoji, color_key = _KIND.get(kind, _KIND["warning"])
        t = theme_mgr.tokens
        accent = getattr(t, color_key, t.accent)
        sc = UiScale.px

        # ─── Width: minimum 380, maximum sensible reading width ─────────
        # The old code did ``setFixedWidth(380)`` unconditionally. That's
        # too narrow for the multi-line messages the Media Manager sends
        # (they easily run 400+ px worth of text after word-wrap), and the
        # 380px cap combined with word-wrap's stale-sizeHint problem was
        # what caused the reported "text clips off the bottom" bug —
        # QLabel with setWordWrap(True) returns a sizeHint() computed at
        # its "natural" (unwrapped) width by default, so adjustSize() ends
        # up laying the dialog out too short.
        #
        # Fix: pick a comfortable base width, use setMinimumWidth so the
        # dialog can grow if a message really needs it, and use
        # setMaximumWidth to keep it from stretching wider than a
        # readable line length (~72ch @ 13px ≈ 520px).
        self.setMinimumWidth(sc(400))
        self.setMaximumWidth(sc(520))
        self.setStyleSheet(f"""
            QDialog {{
                background-color: {t.dialog_bg};
                border: 1px solid {t.stroke_default};
                border-radius: {t.radius_xl}px;
            }}
            QLabel {{ color: {t.text_primary}; background: transparent; border: none; }}
            QPushButton#secondary {{
                background-color: {t.fill_default}; color: {t.text_primary};
                border: 1px solid {t.stroke_default}; border-radius: {t.radius_sm}px;
                padding: {sc(9)}px {sc(16)}px; font-size: {sc(13)}px; font-weight: 600;
            }}
            QPushButton#secondary:hover {{ background: {t.bg_card_hover}; }}
            QPushButton#primary {{
                background-color: {accent}; color: {t.text_on_accent};
                border: 1px solid {accent}; border-radius: {t.radius_sm}px;
                padding: {sc(9)}px {sc(16)}px; font-size: {sc(13)}px; font-weight: 700;
            }}
            QPushButton#primary:hover {{ background: {accent}; }}
        """)

        root = QVBoxLayout(self)
        root.setContentsMargins(sc(28), sc(26), sc(28), sc(22))
        root.setSpacing(sc(12))

        # symbol — a big centered emoji, no circle behind it. A QLabel renders
        # the glyph in its native colour and centres it perfectly (the old
        # hand-painted halo left the emoji slightly off-centre).
        sym = QLabel(emoji)
        sym.setAlignment(Qt.AlignmentFlag.AlignCenter)
        sym.setStyleSheet(
            f"font-size: {sc(46)}px; background: transparent; border: none;")
        root.addWidget(sym, alignment=Qt.AlignmentFlag.AlignHCenter)

        # We track the wrapping-label widgets so we can size them
        # correctly after the dialog's width is finalised (see the
        # heightForWidth block after ``adjustSize``).
        self._wrap_labels: list[QLabel] = []

        if title:
            tl = QLabel(title)
            tl.setAlignment(Qt.AlignmentFlag.AlignCenter); tl.setWordWrap(True)
            tl.setStyleSheet(f"font-size: {sc(16)}px; font-weight: 800; color: {t.text_primary};")
            root.addWidget(tl)
            self._wrap_labels.append(tl)

        if message:
            ml = QLabel(message)
            ml.setAlignment(Qt.AlignmentFlag.AlignCenter); ml.setWordWrap(True)
            ml.setStyleSheet(f"font-size: {sc(13)}px; color: {t.text_secondary}; line-height: 1.4;")
            root.addWidget(ml)
            self._wrap_labels.append(ml)

        root.addSpacing(sc(6))

        # buttons row
        brow = QHBoxLayout(); brow.setSpacing(sc(10))
        brow.addStretch()
        self._btns = []
        for i, label in enumerate(buttons):
            b = QPushButton(label)
            b.setObjectName("primary" if i == default else "secondary")
            b.setCursor(Qt.CursorShape.PointingHandCursor)
            b.setMinimumWidth(sc(96))
            b.clicked.connect(lambda _=False, idx=i: self._choose(idx))
            if i == default:
                b.setDefault(True); b.setAutoDefault(True)
            self._btns.append(b)
            brow.addWidget(b)
        brow.addStretch()
        root.addLayout(brow)

        # ─── Two-pass sizing to defeat QLabel wordwrap's stale sizeHint ─
        # BUG FIX (aero alert clipping): QLabel with setWordWrap(True)
        # reports a sizeHint() based on its "natural" (unwrapped) width by
        # default, so the initial adjustSize() reads a stale height and
        # the message text clips off the bottom of the dialog.
        #
        # Fix (standard Qt idiom for this class of bug):
        #   1. Do a first adjustSize() so the dialog picks a width
        #      between minimumWidth and maximumWidth.
        #   2. Now that we KNOW the final width, ask each wrapping label
        #      for its heightForWidth(final_width) — this returns the
        #      TRUE wrapped height — and setMinimumHeight() it to that
        #      value so the layout can't undercount it.
        #   3. adjustSize() again so the dialog grows to accommodate the
        #      corrected label heights.
        self.adjustSize()
        avail_width = self.width() - sc(28) * 2   # minus contentsMargins L+R
        for lbl in self._wrap_labels:
            try:
                needed = lbl.sizeHint().height()
                # heightForWidth returns -1 if the widget doesn't support
                # it; fall back to sizeHint in that case.
                hfw = lbl.heightForWidth(avail_width)
                if hfw > 0:
                    needed = hfw
                lbl.setMinimumHeight(needed)
            except Exception:
                pass
        self.adjustSize()
        self._center_on_screen()

    def _choose(self, idx: int):
        self._result_index = idx
        self.accept()

    def chosen(self) -> int:
        return self._result_index

    def _center_on_screen(self):
        try:
            geo = self.screen().geometry()
            self.move(geo.center() - self.rect().center())
        except Exception:
            pass

    def keyPressEvent(self, e):
        if e.key() in (Qt.Key.Key_Escape,):
            self._result_index = -1
            self.reject(); return
        super().keyPressEvent(e)

    def showEvent(self, event):
        super().showEvent(event)
        self._center_on_screen()
        apply_aero(self); enable_shadow(self)
        # opacity fade (NOT geometry scale) — avoids setGeometry warnings on
        # fixed-size frameless windows under display scaling.
        fade_in(self)
        # make sure we sit above any frameless panel that opened us
        self.raise_()
        self.activateWindow()

    # ---------------------------------------------------------------- helpers
    @classmethod
    def show_alert(cls, parent, kind, title, message,
                   buttons=("OK",), default=-1) -> int:
        dlg = cls(parent, kind=kind, title=title, message=message,
                  buttons=list(buttons), default=default)
        dlg.exec()
        return dlg.chosen()

    # QMessageBox-style drop-ins ------------------------------------------
    @classmethod
    def warning(cls, parent, title, message, buttons=("OK",)) -> int:
        return cls.show_alert(parent, "warning", title, message, buttons)

    @classmethod
    def critical(cls, parent, title, message, buttons=("OK",)) -> int:
        return cls.show_alert(parent, "danger", title, message, buttons)

    # alias
    danger = critical

    @classmethod
    def information(cls, parent, title, message, buttons=("OK",)) -> int:
        return cls.show_alert(parent, "info", title, message, buttons)

    @classmethod
    def success(cls, parent, title, message, buttons=("OK",)) -> int:
        return cls.show_alert(parent, "success", title, message, buttons)

    @classmethod
    def question(cls, parent, title, message, buttons=None, default=None) -> int:
        """QMessageBox-compatible. `buttons` is an OR of Yes/No/Ok/Cancel;
        returns the chosen flag (AeroAlert.Yes / .No / .Ok / .Cancel)."""
        if buttons is None:
            buttons = cls.Yes | cls.No
        # left→right display order (negatives first, affirmatives last)
        order = [(cls.Cancel, "Cancel"), (cls.No, "No"), (cls.Ok, "OK"), (cls.Yes, "Yes")]
        present = [(flag, label) for flag, label in order if buttons & flag]
        if not present:
            present = [(cls.Ok, "OK")]
        flags  = [f for f, _ in present]
        labels = [l for _, l in present]
        default_idx = flags.index(default) if (default in flags) else len(labels) - 1
        idx = cls.show_alert(parent, "question", title, message,
                             tuple(labels), default=default_idx)
        if idx < 0:                       # Esc/closed → the negative option
            for neg in (cls.Cancel, cls.No):
                if neg in flags:
                    return neg
            return flags[0]
        return flags[idx]

    @classmethod
    def confirm(cls, parent, title, message, *,
                yes="Yes", no="Cancel", danger=False) -> bool:
        """Friendly two-button confirm. Returns True if the affirmative was
        chosen (affirmative is the right-hand, highlighted button)."""
        kind = "danger" if danger else "question"
        idx = cls.show_alert(parent, kind, title, message,
                             buttons=(no, yes), default=1)
        return idx == 1

    # readable alias
    ask = confirm