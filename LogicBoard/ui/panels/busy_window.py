"""
busy_window.py — the small square "working on it" window.

WHY IT EXISTS
=============
Some operations are simply long. Laying a thousand-page PDF across a board
touches a thousand pages; opening a big board unpacks an archive. None of
them can be made instant, and while they run the app paints nothing — at
which point Windows helpfully retitles it "LogicBoard (Not Responding)" and
the teacher reasonably concludes it has crashed.

A window that says otherwise is the whole fix. It needs to be visibly
ALIVE, which is what the animation is for.

A PERCENTAGE, WHEN THERE HONESTLY IS ONE
========================================
This used to refuse a percentage outright, on the grounds that a bar
sitting at 30% for ten seconds is a worse lie than no bar. That reasoning
still holds for jobs that cannot count — unpacking an archive knows how
many bytes it has read and nothing about how long the rest will take — and
those still get the plain sweeping ring.

But some of these jobs count perfectly well. Laying a thousand-page PDF
across a board is a loop over a thousand known pages, and it was already
reporting "Laying out page 250 of 1,000…" as text. Text is the part that
did not work: the window is a fixed 156 px square and the label is drawn
on ONE line into a 136 px strip, so that message lost its last third.
Measured, EVERY message this window is used with overflowed — even
"Opening board…" at 154 px.

So a caller that knows its progress calls ``set_progress`` and gets the
number drawn large in the middle of the ring, where four characters fit
with room to spare and the arc becomes a real gauge. A caller that does
not know says nothing, and nothing is what is shown.

Labels are elided either way. A window that silently cuts words in half
is worse than one that admits it with an ellipsis.

WHAT IT IS NOT FOR
==================
* **Media rendering.** ``ImportProgressDialog`` already covers PDF and PPT
  imports, and it has a real percentage, a real page count and a Cancel
  button, because those jobs genuinely know how far along they are.
* **The splash screen.** That is startup, it has its own identity, and it
  is the one moment where a logo is the right thing to show.

Everything else that makes the app go quiet for more than a moment.

THE ONE THING TO BE CAREFUL ABOUT
=================================
This window can only paint if the event loop is being turned over. A
long operation that never yields will leave it frozen on screen, which is
worse than not showing it at all — a spinner that has stopped reads as a
hung app more strongly than a blank one does.

So ``run_with_busy`` pumps the loop while the work runs, and any operation
that wants the window is expected to call ``tick()`` periodically or to be
broken into steps. There is no way around that: Qt paints on one thread.
"""
from __future__ import annotations

import logging
import math
import time
from typing import Optional

from PySide6.QtCore import Qt, QTimer, QRectF, QPointF
from PySide6.QtGui import QPainter, QColor, QPen, QFont, QFontMetrics
from PySide6.QtWidgets import QDialog, QApplication, QWidget, QVBoxLayout

from ui.theme.theme_manager import theme_mgr
from ui.util.scaling import UiScale

try:
    from ui.util.aero import (apply_aero, prepare_aero_widget,
                              apply_rounded_corners)
except Exception:                                   # pragma: no cover
    def apply_aero(*a, **k): ...
    def prepare_aero_widget(*a, **k): ...
    def apply_rounded_corners(*a, **k): ...

log = logging.getLogger(__name__)

__all__ = ["BusyWindow", "run_with_busy"]


class _BusyCanvas(QWidget):
    """The animation. Fills the window; draws nothing else.

    An arc sweeping round a ring, rather than a row of pulsing dots or a
    bar: a ring has no beginning or end, so it makes no implicit promise
    about how far along the work is. Its only claim is "still going", which
    is the only claim these operations can actually support.
    """

    def __init__(self, parent=None):
        super().__init__(parent)
        self.setAttribute(Qt.WA_TranslucentBackground, True)
        self._t0 = time.perf_counter()
        self._label = ""
        self._pct = None            # None => this job cannot count

    def set_label(self, text: str) -> None:
        if text != self._label:
            self._label = text or ""
            self.update()

    def label_for_width(self, font, width: int) -> str:
        """The label as it will actually be PAINTED at ``width``.

        A method rather than an inline call so the result can be asserted
        on. The bug this exists for is invisible to a pixel test: an
        overflowing drawText is clipped by the window, so the ink stops at
        the same column either way and only the shape of the last letter
        differs. What distinguishes correct from broken is the STRING, so
        the string is what is exposed.
        """
        try:
            return QFontMetrics(font).elidedText(
                self._label, Qt.ElideRight, max(0, int(width)))
        except Exception:
            return self._label

    def set_percent(self, pct) -> None:
        """0-100, or None to go back to the indeterminate sweep."""
        if pct is None:
            new = None
        else:
            try:
                new = max(0.0, min(100.0, float(pct)))
            except (TypeError, ValueError):
                new = None
        # Repaint on a whole percent, not on every call. This is driven from
        # inside the work loop, and a thousand-page job would otherwise ask
        # for a thousand repaints to show a hundred different numbers.
        if new is None or self._pct is None:
            changed = new is not self._pct
        else:
            changed = int(new) != int(self._pct)
        self._pct = new
        if changed:
            self.update()

    def paintEvent(self, _event):
        t = theme_mgr.tokens
        sc = UiScale.px
        p = QPainter(self)
        try:
            p.setRenderHint(QPainter.Antialiasing, True)
            w, h = self.width(), self.height()

            # The ring is sized to the window rather than to a constant, so
            # the animation "covers the whole box" at any UI scale.
            side = min(w, h)
            radius = side * 0.30
            centre = QPointF(w / 2.0, h / 2.0 - (sc(14) if self._label else 0))
            box = QRectF(centre.x() - radius, centre.y() - radius,
                         radius * 2, radius * 2)
            thickness = max(2.0, side * 0.055)

            elapsed = time.perf_counter() - self._t0

            # Track: the full circle, barely there. Without it the sweeping
            # arc reads as a comet with nowhere to go.
            track = QPen(theme_mgr.qcolor(t.fill_hover), thickness,
                         Qt.SolidLine, Qt.RoundCap)
            p.setPen(track)
            p.drawEllipse(box)

            arc = QPen(theme_mgr.qcolor(t.accent), thickness,
                       Qt.SolidLine, Qt.RoundCap)
            p.setPen(arc)
            # Qt angles are in 1/16ths of a degree, anticlockwise from 3
            # o'clock.
            if self._pct is None:
                # Sweep: rotates steadily while its length breathes, so the
                # motion never looks like it has stalled at a constant angle
                # — which a fixed-length arc at a fixed speed genuinely can,
                # on a display whose refresh happens to align with it.
                span = 90.0 + 60.0 * math.sin(elapsed * 2.2)
                start = (-elapsed * 260.0) % 360.0
                p.drawArc(box, int(start * 16), int(span * 16))
            else:
                # A gauge: from twelve o'clock, clockwise, as far as the
                # work has got. Clockwise is negative in Qt's convention.
                p.drawArc(box, 90 * 16, int(-3.6 * self._pct * 16))

            # The number, inside the ring. Four characters at most, in a
            # box the ring's own diameter — it cannot overflow, which is
            # the entire reason it lives here and not under the ring with
            # the label that did.
            if self._pct is not None:
                p.setPen(theme_mgr.qcolor(t.text_primary))
                fp = QFont(p.font())
                fp.setPixelSize(int(radius * 0.62))
                fp.setWeight(QFont.DemiBold)
                p.setFont(fp)
                p.drawText(box, Qt.AlignCenter, f"{int(self._pct)}%")

            if self._label:
                p.setPen(theme_mgr.qcolor(t.text_secondary))
                f = QFont(p.font())
                f.setPixelSize(sc(t.font_body))
                p.setFont(f)
                strip = QRectF(sc(t.space_md), centre.y() + radius + sc(16),
                               w - sc(t.space_md) * 2, sc(40))
                # ELIDED — see label_for_width. drawText on one line runs
                # past the strip and Qt then clips it at the window edge, so
                # the failure is a word sliced in half with nothing to say
                # so. Measured, every message this window was ever given
                # overflowed, "Opening board…" included.
                p.drawText(strip, Qt.AlignHCenter | Qt.AlignTop,
                           self.label_for_width(f, int(strip.width())))
        finally:
            p.end()


class BusyWindow(QDialog):
    """A small square frameless window that says the app is still working.

    Modal, so the board cannot be edited into an inconsistent state while
    something is rewriting it underneath, and always-on-top so it cannot be
    lost behind the window it is speaking for.
    """

    #: How often the ring is repainted. 40 ms is 25 fps, which is smooth
    #: enough for a rotating arc and cheap enough that it never competes
    #: with the work it is reporting on.
    FRAME_MS = 40

    def __init__(self, parent=None, message: str = ""):
        super().__init__(parent,
                         Qt.FramelessWindowHint | Qt.Dialog
                         | Qt.WindowStaysOnTopHint)
        sc = UiScale.px
        self.setAttribute(Qt.WA_TranslucentBackground, True)
        self.setModal(True)
        self.setStyleSheet("background: transparent;")
        prepare_aero_widget(self)

        side = sc(184)
        self.setFixedSize(side, side)

        lay = QVBoxLayout(self)
        lay.setContentsMargins(0, 0, 0, 0)
        self._canvas = _BusyCanvas(self)
        lay.addWidget(self._canvas)
        self._canvas.set_label(message)

        self._timer = QTimer(self)
        self._timer.setInterval(self.FRAME_MS)
        self._timer.timeout.connect(self._canvas.update)

    # ── the glass ───────────────────────────────────────────────────────
    def showEvent(self, event):
        super().showEvent(event)
        try:
            apply_aero(self)
            # Square window, rounded by the compositor exactly like every
            # other floating surface in the app — so it belongs to
            # LogicBoard rather than looking like a system dialog.
            apply_rounded_corners(self, mode="round")
        except Exception:
            log.debug("busy window glass failed", exc_info=True)
        self._centre_on_parent()
        self._timer.start()

    def hideEvent(self, event):
        self._timer.stop()
        super().hideEvent(event)

    def _centre_on_parent(self) -> None:
        try:
            par = self.parentWidget()
            if par is not None and par.isVisible():
                geo = par.frameGeometry()
            else:
                screen = QApplication.primaryScreen()
                geo = screen.availableGeometry() if screen else None
            if geo is not None:
                self.move(geo.center().x() - self.width() // 2,
                          geo.center().y() - self.height() // 2)
        except Exception:
            pass

    # ── public API ──────────────────────────────────────────────────────
    def set_message(self, text: str) -> None:
        self._canvas.set_label(text)

    def set_progress(self, done, total=None) -> None:
        """Report how far along the work is, as a percentage.

        ``set_progress(3, 10)`` or ``set_progress(30.0)``. Pass ``None`` to
        drop back to the indeterminate ring — a job that stops being able
        to count should stop claiming it can.

        Only for work that genuinely knows. This window is used for jobs
        that do not, and inventing a number for those is what the module
        docstring is about.
        """
        if done is None:
            self._canvas.set_percent(None)
            return
        # ``total is None`` is the signal for "done is already a percent".
        # Testing truthiness instead meant an explicit total=0 fell into
        # that branch, so set_progress(5, 0) — nothing to count — reported
        # a confident 5%.
        if total is None:
            pct = done
        else:
            try:
                t = float(total)
                pct = (100.0 * float(done) / t) if t > 0 else None
            except (TypeError, ValueError):
                pct = None
        self._canvas.set_percent(pct)

    def tick(self) -> None:
        """Give the window a chance to paint.

        Call this from inside a long loop. Without it the window appears and
        then freezes, which reads as a hung app more strongly than showing
        nothing would have.
        """
        QApplication.processEvents()

    # Escape must not dismiss it: the work carries on regardless, and a
    # board being rewritten with no indication of it is the worst of both.
    def keyPressEvent(self, event):
        if event.key() == Qt.Key_Escape:
            event.ignore()
            return
        super().keyPressEvent(event)

    def closeEvent(self, event):
        # Only close() from code, never from the window manager.
        if getattr(self, "_allow_close", False):
            super().closeEvent(event)
        else:
            event.ignore()

    def finish(self) -> None:
        self._allow_close = True
        self._timer.stop()
        self.close()
        self.deleteLater()


def run_with_busy(parent, message: str, work, *, min_ms: int = 0):
    """Run ``work(busy)`` behind a busy window and return its result.

    ``work`` is handed the window so it can update the message and call
    ``tick()``; a callable taking no arguments works too.

    ``min_ms`` keeps the window up for at least that long. Without it a job
    that turns out to be fast produces a flash of a window, which is more
    distracting than the pause it was covering.

    The work runs on THIS thread. That is deliberate: everything this is
    used for touches the board or the widget tree, and moving that off the
    GUI thread would trade a visible pause for a class of crash. The window
    stays alive because the work yields, not because it is asynchronous.
    """
    busy = BusyWindow(parent, message)
    busy.show()
    QApplication.processEvents()
    started = time.perf_counter()
    try:
        try:
            return work(busy)
        except TypeError:
            return work()
    finally:
        if min_ms > 0:
            remaining = min_ms / 1000.0 - (time.perf_counter() - started)
            deadline = time.perf_counter() + max(0.0, remaining)
            while time.perf_counter() < deadline:
                QApplication.processEvents()
        busy.finish()
