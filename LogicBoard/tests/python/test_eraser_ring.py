"""
test_eraser_ring.py — the eraser ring (the circle that follows the rub).

Runs under pytest, and also standalone (``python tests/python/test_eraser_ring.py``)
because the repo's pytest install is currently broken on Python 3.11.

What is worth protecting here
-----------------------------
The ring is one circle drawn from one point, so every bug it has ever had has
been about WHO writes that point and WHO repaints the pixels it leaves. A
classroom board makes that hard in a way a desk never does: the same physical
contact arrives as touch events AND, a few samples later, as synthesised mouse
events. Two writers, each scrubbing its own idea of where the ring used to be,
is the whole family of symptoms — a circle that tears in half at speed, an arc
left behind at every sample of a two-finger sweep, a ring stuck where the
finger came off.

So the tests below pin the two invariants that make those impossible:

  * WHATEVER changes on screen, the code asked for those pixels back. This is
    the trail test, and it is checked by diffing full renders against the
    union of the update rectangles the code requested — the same technique
    test_pixel_eraser.py uses for the erase itself.
  * The ring is on screen exactly while something is holding it there. No
    contact and no hovering pointer means parked, including when the touch
    frame never sends the release.
"""
import math
import os
import sys

os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")
sys.path.insert(0, os.path.abspath(os.path.join(os.path.dirname(__file__),
                                                "..", "..")))

from PySide6.QtWidgets import QApplication
from PySide6.QtCore import QPoint, QPointF, QRect, QEvent, Qt
from PySide6.QtGui import QMouseEvent, QRegion

_app = QApplication.instance() or QApplication([])

import logicboard_engine as lbe
from ui.canvas.canvas_widget import CanvasWidget
from ui.canvas.input_handler import InputHandler


# ── scaffolding ─────────────────────────────────────────────────────────────

class _FakePoint:
    """Duck-typed QEventPoint — PySide6 won't let a test set a point's state."""

    def __init__(self, pid, pos, state, contact=18.0):
        self._id, self._pos, self._state, self._c = pid, pos, state, contact

    def id(self):        return self._id
    def position(self):  return self._pos
    def ellipseDiameters(self):
        from PySide6.QtCore import QSizeF
        return QSizeF(self._c, self._c)

    def state(self):
        from types import SimpleNamespace
        return SimpleNamespace(name=self._state)


class _FakeTouch:
    def __init__(self, etype, pts): self._t, self._p = etype, pts
    def type(self):   return self._t
    def points(self): return self._p
    def accept(self): pass


def _board():
    """Canvas with the pixel eraser selected and some ink to rub at."""
    c = CanvasWidget()
    c.resize(900, 700)
    ih = InputHandler(c)
    c._input_handler = ih
    c.set_tool("eraser")
    c.set_eraser_mode("pixel")
    c.set_eraser_size(60.0)
    c._touch_mode = "palm_rejection"        # a fingertip is just a fingertip
    s = c.current_scene()
    for k in range(6):
        o = lbe.FreehandStroke()
        o.set_points([(80.0 + i * 12, 120.0 + k * 70) for i in range(50)])
        o.stroke_width = 5.0
        o.stroke_color = lbe.RGBA(255, 0, 0, 255)
        s.add_object(o)
    c.invalidate_cache()
    return c, ih


def _mouse(c, kind, x, y, *, buttons=Qt.NoButton):
    """Deliver a mouse event the way Windows delivers the synthesised ones."""
    ev = QMouseEvent(kind, QPointF(x, y), Qt.LeftButton, buttons, Qt.NoModifier)
    {QEvent.MouseMove:        c.mouseMoveEvent,
     QEvent.MouseButtonPress: c.mousePressEvent,
     QEvent.MouseButtonRelease: c.mouseReleaseEvent}[kind](ev)


class _UpdateSpy:
    """Record every rectangle the widget asks to have repainted.

    A ``None`` entry means a full-widget repaint was requested, which trivially
    covers everything — the tests treat that as "no claim to check"."""

    def __init__(self):
        self.rects, self.full = [], False
        self._real = CanvasWidget.update

    def __enter__(self):
        spy = self

        def update(self, *a):
            if len(a) == 4:
                spy.rects.append(QRect(a[0], a[1], a[2], a[3]))
            elif len(a) == 1 and isinstance(a[0], QRect):
                spy.rects.append(QRect(a[0]))
            else:
                spy.full = True
            return spy._real(self, *a)

        CanvasWidget.update = update
        return self

    def __exit__(self, *exc):
        CanvasWidget.update = self._real
        return False

    def clear(self):
        self.rects.clear(); self.full = False

    def region(self):
        reg = QRegion()
        for r in self.rects:
            reg = reg.united(QRegion(r.adjusted(-1, -1, 1, 1)))
        return reg


def _stale_pixels(before, after, region):
    """Pixels that changed outside the region the code asked to repaint.

    Those are the trail: something moved on screen and nothing invalidated
    where it moved FROM, so the old pixels are still lit."""
    bad = 0
    for y in range(0, after.height(), 2):
        for x in range(0, after.width(), 2):
            if (after.pixel(x, y) != before.pixel(x, y)
                    and not region.contains(QPoint(x, y))):
                bad += 1
    return bad


# ── the trail: every pixel that changes must have been asked for ────────────

def test_a_fast_rub_asks_for_every_pixel_it_changes():
    """The ring moves in big jumps at speed, so consecutive dirty boxes stop
    overlapping — exactly when a missing "repaint where it came from" starts
    showing as half a circle left behind next to the new one."""
    c, ih = _board()
    pts = [(120.0 + i * 47, 140.0 + 90 * math.sin(i / 2.0)) for i in range(14)]
    with _UpdateSpy() as spy:
        ih.touch_event(_FakeTouch(QEvent.TouchBegin,
                                  [_FakePoint(1, QPointF(*pts[0]), "Pressed")]))
        prev = c.grab().toImage()
        stale = 0
        for p in pts[1:]:
            spy.clear()
            ih.touch_event(_FakeTouch(QEvent.TouchUpdate,
                                      [_FakePoint(1, QPointF(*p), "Updated")]))
            cur = c.grab().toImage()
            if not spy.full:
                stale += _stale_pixels(prev, cur, spy.region())
            prev = cur
    assert stale == 0, f"{stale} pixels changed outside the repainted region"


def test_the_synthesised_mouse_stream_cannot_move_the_ring_mid_rub():
    """Windows replays a touch contact as mouse moves a few samples behind it.

    Both used to write the ring position, so it alternated between the finger
    and a point trailing it — the circle "distorting" when the rub gets fast.
    The touch stream owns the ring; the mouse events must not touch it."""
    c, ih = _board()
    ih.touch_event(_FakeTouch(QEvent.TouchBegin,
                              [_FakePoint(1, QPointF(300.0, 200.0), "Pressed")]))
    ih.touch_event(_FakeTouch(QEvent.TouchUpdate,
                              [_FakePoint(1, QPointF(500.0, 300.0), "Updated")]))
    at_finger = QPoint(c._cursor_pos)
    assert at_finger.x() == 500 and at_finger.y() == 300
    # …and here comes the same contact again, as the mouse, lagging badly.
    _mouse(c, QEvent.MouseMove, 320.0, 210.0, buttons=Qt.LeftButton)
    assert c._cursor_pos == at_finger, \
        f"the synthesised mouse dragged the ring to {c._cursor_pos}"


def test_a_two_finger_sweep_leaves_no_ring_behind():
    """Two contacts route into the gesture path, which erases nothing. The
    ring has to go — and the pixels have to be asked for, or it is only
    logically gone and an arc stays lit at every sample of the sweep."""
    c, ih = _board()
    with _UpdateSpy() as spy:
        ih.touch_event(_FakeTouch(QEvent.TouchBegin,
                                  [_FakePoint(1, QPointF(200.0, 200.0), "Pressed")]))
        # A real sweep lasts longer than the 350 ms window the drawing path
        # armed on the way in, so by the time the second finger lands that
        # window has lapsed and the mouse stream is live again. Model it, or
        # the test passes for the wrong reason — the mouse events would be
        # suppressed by a window the gesture path never renewed.
        c._ignore_mouse_until = 0.0
        prev = c.grab().toImage()
        stale = 0
        for i in range(1, 10):
            x, y = 200.0 + i * 40, 200.0 + i * 18
            spy.clear()
            ih.touch_event(_FakeTouch(QEvent.TouchUpdate, [
                _FakePoint(1, QPointF(x, y), "Updated"),
                _FakePoint(2, QPointF(x + 90, y + 30),
                           "Pressed" if i == 1 else "Updated")]))
            # The mouse stream keeps coming during a gesture too.
            _mouse(c, QEvent.MouseMove, x + 20, y + 8, buttons=Qt.LeftButton)
            cur = c.grab().toImage()
            if not spy.full:
                stale += _stale_pixels(prev, cur, spy.region())
            prev = cur
    assert stale == 0, f"{stale} pixels of ring trail left by the sweep"
    assert c._cursor_pos.x() < 0, "the ring stayed live through a gesture"


# ── the ring is on screen only while something holds it there ───────────────

def test_the_ring_is_parked_when_the_last_contact_lifts():
    c, ih = _board()
    ih.touch_event(_FakeTouch(QEvent.TouchBegin,
                              [_FakePoint(1, QPointF(300.0, 200.0), "Pressed")]))
    assert c._cursor_pos.x() >= 0, "the ring never appeared under the contact"
    ih.touch_event(_FakeTouch(QEvent.TouchEnd,
                              [_FakePoint(1, QPointF(300.0, 240.0), "Released")]))
    assert c._cursor_pos.x() < 0, "the ring stayed where the finger came off"
    c.grab()                                   # let the parking repaint run
    assert c._ring_last_painted is None


def test_a_stray_mouse_move_after_a_lift_does_not_bring_the_ring_back():
    """The lift is followed by one buttonless move — Windows settling the
    cursor at the contact point. On a board with no hovering pointer that
    means nothing, and acting on it pins the ring there forever: nothing will
    ever move it again, which is what "the circle stays where I lifted off"
    was."""
    c, ih = _board()
    ih.touch_event(_FakeTouch(QEvent.TouchBegin,
                              [_FakePoint(1, QPointF(300.0, 200.0), "Pressed")]))
    ih.touch_event(_FakeTouch(QEvent.TouchEnd,
                              [_FakePoint(1, QPointF(300.0, 240.0), "Released")]))
    c._ignore_mouse_until = 0.0                # the 350 ms window has expired
    _mouse(c, QEvent.MouseMove, 300.0, 240.0)
    assert c._cursor_pos.x() < 0, "a stray mouse move resurrected the ring"


def test_a_real_mouse_still_gets_a_ring_that_follows_it():
    """The flip side: a machine that DOES hover must keep the ring under the
    pointer. Suppressing the ring on buttonless moves outright would fix the
    board by breaking every desk."""
    c, ih = _board()
    for i in range(6):                         # a real mouse hovers, and keeps
        _mouse(c, QEvent.MouseMove, 400.0 + i, 300.0 + i)
    assert c._hover_seen, "a stream of buttonless moves is a hovering pointer"
    assert c._cursor_pos.x() >= 0, "the ring never followed the mouse"


def test_a_dropped_release_still_parks_the_ring():
    """An IR frame that loses the release — lift fast and it happens — leaves
    nothing to run the parking code. The idle watchdog is the backstop."""
    c, ih = _board()
    ih.touch_event(_FakeTouch(QEvent.TouchBegin,
                              [_FakePoint(1, QPointF(300.0, 200.0), "Pressed")]))
    assert c._ring_watchdog.isActive(), "no watchdog armed for a touch ring"
    ih._active_touches.clear()                 # the frame simply stops talking
    c._on_ring_idle()
    assert c._cursor_pos.x() < 0, "the ring outlived the contact holding it"


def test_a_resting_contact_does_not_trip_the_watchdog():
    """A finger held still is not idle input. Parking there would blank the
    ring in the middle of an erase."""
    c, ih = _board()
    ih.touch_event(_FakeTouch(QEvent.TouchBegin,
                              [_FakePoint(1, QPointF(300.0, 200.0), "Pressed")]))
    c._on_ring_idle()
    assert c._cursor_pos.x() >= 0, "the watchdog parked a ring still in use"
    assert c._ring_watchdog.isActive(), "the watchdog stopped watching"


# ── Dual Pen: the ring has to follow the TIP, not the tool menu ─────────────

class _TipClassifier:
    """Stand-in for InputCalibration's classifier: every contact is the back
    tip of the stylus (that IS what the eraser profile means)."""

    def classify(self, features):
        return "eraser", 0.95


def test_dual_pen_back_tip_gets_a_ring_when_it_arrives_as_touch():
    """The back tip comes in as a plain touch contact on an IR board. Erasing
    always worked — the ring didn't, because nothing recomputed its visibility
    for the tip that had just landed, so you were rubbing blind."""
    c, ih = _board()
    c._touch_classifier = _TipClassifier()
    ih.set_dual_pen_assignment("pen", "eraser")
    c.set_tool("dual_pen")
    assert not c._show_eraser_cursor, "front tip is the pen — no ring yet"
    ih.touch_event(_FakeTouch(QEvent.TouchBegin,
                              [_FakePoint(1, QPointF(300.0, 200.0), "Pressed")]))
    assert ih._current_tool() == "eraser", "the back tip should be erasing"
    assert c._show_eraser_cursor, "the back tip erased with no ring"
    assert c._cursor_pos.x() >= 0, "the ring never moved to the contact"


def test_the_ring_agrees_with_the_tool_the_stroke_is_running():
    """Mid-stroke the ring must reflect the tool frozen at press, not whatever
    the tip says now — otherwise a flipped pen shows a ring over a stroke that
    is still, correctly, drawing ink."""
    c, ih = _board()
    c._touch_classifier = _TipClassifier()
    ih.set_dual_pen_assignment("pen", "eraser")
    c.set_tool("dual_pen")
    ih._active_tip = "front"
    ih._touch_drawing = True
    ih._touch_tool = "pen"                     # a pen stroke is in flight
    ih._active_tip = "back"                    # …and the tip flips mid-stroke
    c._sync_eraser_cursor()
    assert not c._show_eraser_cursor, \
        "the ring appeared over a stroke that is still drawing ink"


if __name__ == "__main__":
    failures = []
    for name, fn in sorted(globals().items()):
        if not name.startswith("test_") or not callable(fn):
            continue
        try:
            fn()
            print(f"  ok    {name}")
        except AssertionError as exc:
            failures.append(name)
            print(f"  FAIL  {name}: {exc}")
        except Exception as exc:                       # noqa: BLE001
            failures.append(name)
            print(f"  ERROR {name}: {type(exc).__name__}: {exc}")
    print(f"\n{len(failures)} failed" if failures else "\nall passed")
    sys.exit(1 if failures else 0)
