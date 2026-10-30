"""
toast.py — a small transient message in the corner of the board.

For telling the user something happened WITHOUT taking the board away from
them. Autosave is the case that motivated it: a modal in the middle of the
screen with a spinner is the right shape for "you asked me to save and I am
doing it now", and exactly the wrong shape for "by the way, I saved" — the
user didn't ask, isn't waiting, and on a classroom board is very likely
mid-sentence with a pen on the glass.

Design notes
------------
* PLAIN CHILD WIDGET, not a Qt.Tool window — same reasoning as SelectionMenu.
  A second HWND brings its own activation state, and on a touch board that
  costs the first tap. A child is just part of the main window's surface.
  The cost is the DWM aero blur (per-HWND, so a child cannot have one); the
  fill below is a real translucent colour composited over the canvas.
* TRANSPARENT TO INPUT. The whole promise is "does not interrupt", and a
  panel that swallows pen strokes in the corner it happens to occupy would
  break that promise in the most annoying way possible — the user would
  find a rectangle of the board that silently refuses ink for three seconds.
  WA_TransparentForMouseEvents (plus no touch attribute) means every press
  goes straight through to the canvas.
* Every dimension goes through ``size_mgr`` so the message keeps its
  proportions from a 1080p laptop to a 4K smart board, at any Windows
  display-scaling setting.
"""
from __future__ import annotations

from typing import Optional

from PySide6.QtCore import Qt, QTimer, QPropertyAnimation, QEasingCurve
from PySide6.QtGui import QFont
from PySide6.QtWidgets import QWidget, QHBoxLayout, QLabel, QGraphicsOpacityEffect

from ui.util.size_manager import size_mgr as S
from ui.theme.theme_manager import theme_mgr

__all__ = ["Toast"]

# Gap from the window's top-left corner, at the 1920x1080 reference.
_MARGIN_REF = 22

_FADE_IN_MS = 160
_FADE_OUT_MS = 260


class Toast(QWidget):
    """A short message that appears in the top-left corner and fades away.

    Use :meth:`show_message`; it reuses the parent's existing toast instead of
    stacking a new widget per call, which matters because the thing most
    likely to post one is a timer.
    """

    def __init__(self, parent: QWidget, text: str = "") -> None:
        super().__init__(parent)
        # Without WA_StyledBackground a plain QWidget subclass ignores its
        # stylesheet background entirely — Qt only paints one automatically
        # for the built-in widget classes. The toast would be a floating
        # label with no card behind it.
        self.setAttribute(Qt.WA_StyledBackground, True)
        # See the module docstring: the board must stay drawable underneath.
        self.setAttribute(Qt.WA_TransparentForMouseEvents, True)
        self.setFocusPolicy(Qt.NoFocus)

        lay = QHBoxLayout(self)
        lay.setContentsMargins(S.scale_margin(18), S.scale_margin(11),
                               S.scale_margin(18), S.scale_margin(11))
        lay.setSpacing(S.scale_spacing(10))

        self._label = QLabel(text, self)
        f = QFont()
        f.setPixelSize(S.scale_font(13))
        f.setBold(True)
        self._label.setFont(f)
        lay.addWidget(self._label)

        # One effect instance for the life of the toast. Rebuilding it per
        # show would drop the animation that is mid-flight when a second
        # message arrives before the first has faded.
        self._opacity = QGraphicsOpacityEffect(self)
        self._opacity.setOpacity(0.0)
        self.setGraphicsEffect(self._opacity)
        self._anim: Optional[QPropertyAnimation] = None

        self._hold = QTimer(self)
        self._hold.setSingleShot(True)
        self._hold.timeout.connect(self._fade_out)

        self._apply_theme()
        self.hide()

    # ── appearance ───────────────────────────────────────────────────────
    def _apply_theme(self) -> None:
        """Re-read the tokens. Called on every ``pop`` rather than wired to
        ``theme_changed``: the toast is transient, so reading the tokens as it
        appears is always current, and it avoids leaving a lambda connected to
        the theme singleton that would fire into a deleted widget after the
        window closes. (Today the app is dark-only and that signal never
        fires at all — one more reason not to depend on it.)"""
        t = theme_mgr.tokens
        self.setStyleSheet(f"""
            Toast {{
                background: {t.card_grad};
                border: 1px solid {t.stroke_subtle};
                border-radius: {S.scale_radius(t.radius_md)}px;
            }}
            QLabel {{
                color: {t.text_primary};
                background: transparent;
            }}
        """)

    def set_text(self, text: str) -> None:
        self._label.setText(text)
        self.adjustSize()

    # ── placement ────────────────────────────────────────────────────────
    def reposition(self) -> None:
        """Park in the parent's top-left corner.

        Called on every show rather than once at construction: the window is
        resizable and moves between screens, and a toast measured against a
        stale geometry would sit half off the board.
        """
        m = S.scale_margin(_MARGIN_REF)
        self.adjustSize()
        self.move(m, m)

    # ── lifecycle ────────────────────────────────────────────────────────
    def _animate_opacity(self, to: float, ms: int, on_done=None) -> None:
        if self._anim is not None:
            self._anim.stop()
        anim = QPropertyAnimation(self._opacity, b"opacity", self)
        anim.setDuration(ms)
        anim.setStartValue(self._opacity.opacity())
        anim.setEndValue(to)
        anim.setEasingCurve(QEasingCurve.OutCubic)
        if on_done is not None:
            anim.finished.connect(on_done)
        self._anim = anim
        anim.start()

    def pop(self, text: str, duration_ms: int = 3000) -> None:
        """Show ``text`` for ``duration_ms``, then fade out.

        Restarting an already-visible toast simply retargets it: new text,
        full opacity, timer back to the top. A save that lands while the
        previous message is still up should read as one message that stayed,
        not two that fought over the corner.
        """
        self._apply_theme()
        self.set_text(text)
        self.reposition()
        self.show()
        self.raise_()
        self._hold.stop()
        self._animate_opacity(1.0, _FADE_IN_MS)
        self._hold.start(max(0, int(duration_ms)))

    def _fade_out(self) -> None:
        self._animate_opacity(0.0, _FADE_OUT_MS, on_done=self.hide)

    # ── convenience ──────────────────────────────────────────────────────
    @classmethod
    def show_message(cls, parent: QWidget, text: str,
                     duration_ms: int = 3000) -> "Toast":
        """Post ``text`` on ``parent``'s toast, creating it on first use."""
        toast = getattr(parent, "_toast", None)
        if toast is None or not isinstance(toast, cls):
            toast = cls(parent)
            parent._toast = toast
        toast.pop(text, duration_ms)
        return toast
