"""
test_dual_pen_back_tip.py — the Dual Pen back tip runs the tool it was given.

Runs under pytest, and also standalone (``python tests/python/test_dual_pen_back_tip.py``)
because the repo's pytest install is currently broken on Python 3.11.

What is worth protecting here
-----------------------------
The back tip is a big blunt contact. So is a palm. On a frame that only
measures size those are the same observation, and every bug below is some
version of the app resolving that ambiguity against the user: the tip they
deliberately assigned a tool to gets judged an accident, and the tool never
runs. Set the back tip to Pen and it erases, or does nothing, or previews as
one tool and commits as another.

The rules pinned here:

  * The contact that OPENS a Dual Pen stroke is a stylus tip, never a palm.
    Later contacts are still palms — that is what palm handling is for.
  * A contact admitted as a tip stays a tip. Pressing harder mid-word grows
    the footprint; it must not turn the stroke into an erase halfway along.
  * Dual Pen is only offered when the back tip can actually be RECOGNISED.
    A profile flagged calibrated but carrying no contact area (the mouse
    route produces exactly that, and so does a frame that reports no ellipse)
    leaves the classifier unable to ever say "back tip".
  * A tablet event's pointerType outranks a setting about what a mouse click
    means, so a back-tip press can't be re-labelled as the front tip.
"""
import math
import os
import pathlib
import sys
import tempfile

os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")
sys.path.insert(0, os.path.abspath(os.path.join(os.path.dirname(__file__),
                                                "..", "..")))

from PySide6.QtWidgets import QApplication
from PySide6.QtCore import QPointF, QEvent, Qt, QSizeF
from PySide6.QtGui import QColor, QPointingDevice

_app = QApplication.instance() or QApplication([])

import logicboard_engine as lbe
from ui.canvas.canvas_widget import CanvasWidget
from ui.canvas.input_handler import InputHandler
from ui.canvas.touch_classifier import TouchClassifier
from ui.util.input_calibration import InputCalibration

PEN_RGB = (0, 200, 0)
INK_RGB = (255, 0, 0)


# ── scaffolding ─────────────────────────────────────────────────────────────

class _Point:
    """Duck-typed QEventPoint with a settable contact size."""

    def __init__(self, pid, pos, state, diameter):
        self._i, self._p, self._s, self._d = pid, pos, state, diameter

    def id(self):       return self._i
    def position(self): return self._p
    def ellipseDiameters(self): return QSizeF(self._d, self._d)

    def state(self):
        from types import SimpleNamespace
        return SimpleNamespace(name=self._s)


class _Touch:
    def __init__(self, t, pts): self._t, self._p = t, pts
    def type(self):   return self._t
    def points(self): return self._p
    def accept(self): pass


class _Tablet:
    def __init__(self, t, pos, ptype):
        self._t, self._pos, self._pt = t, pos, ptype
    def type(self):        return self._t
    def position(self):    return self._pos
    def pressure(self):    return 0.5
    def modifiers(self):   return Qt.NoModifier
    def pointerType(self): return self._pt
    def accept(self):      pass


def _calibration(*, back_area, palm_area=None):
    cal = InputCalibration(pathlib.Path(tempfile.mkdtemp()) / "cal.json")
    # The school board: the front tip arrives as a plain left click, so there
    # is no contact to measure and mouse_is_stylus_front supplies the fact.
    cal.stylus_profile.calibrated = True
    cal.stylus_profile.source = "mouse"
    cal.mouse_is_stylus_front = True
    cal.eraser_profile.calibrated = True
    cal.eraser_profile.median_area = back_area
    cal.eraser_profile.source = "touch"
    if palm_area is not None:
        cal.palm_profile.calibrated = True
        cal.palm_profile.median_area = palm_area
    return cal


def _board(*, mode="palm_eraser", back_tool="pen", front_tool="pen",
           back_area=900.0, palm_area=None):
    c = CanvasWidget(); c.resize(900, 700)
    ih = InputHandler(c); c._input_handler = ih
    cal = _calibration(back_area=back_area, palm_area=palm_area)
    c._touch_classifier = TouchClassifier(cal)
    c._touch_mode = mode
    ih.set_mouse_is_stylus_front(True)
    ih.set_dual_pen_assignment(front=front_tool, back=back_tool)
    c.set_tool("dual_pen")
    # BOTH, and they are genuinely two settings now. The Pen tool and the
    # dual pen keep separate colours, so a teacher can leave the barrel on
    # red without the front tip changing underneath them. This fixture used
    # to set only ``set_pen_color`` and then assert the back tip drew in it,
    # which quietly stopped being true the day they were split — every test
    # here failed with "committed in rgb(0,122,255)", the dual pen's own
    # untouched default.
    c.set_pen_color(QColor(*PEN_RGB))
    c.set_dual_pen_color(QColor(*PEN_RGB))
    c.set_pen_width(6.0)
    c.set_highlighter_color(QColor(255, 170, 0))
    c.set_eraser_size(60.0)
    s = c.current_scene()
    for k in range(3):                       # ink to lose, if it erases
        o = lbe.FreehandStroke()
        o.set_points([(120.0 + i * 10, 195.0 + k * 6) for i in range(20)])
        o.stroke_width = 4.0
        o.stroke_color = lbe.RGBA(*INK_RGB, 255)
        s.add_object(o)
    c.invalidate_cache()
    return c, ih, cal


def _touch_stroke(ih, diameter, grow=0.0):
    ih.touch_event(_Touch(QEvent.TouchBegin,
                          [_Point(1, QPointF(150.0, 200.0), "Pressed", diameter)]))
    for i in range(1, 8):
        ih.touch_event(_Touch(QEvent.TouchUpdate,
                              [_Point(1, QPointF(150.0 + i * 20, 200.0),
                                      "Updated", diameter + grow * i)]))
    ih.touch_event(_Touch(QEvent.TouchEnd,
                          [_Point(1, QPointF(300.0, 200.0), "Released", diameter)]))


def _pen_strokes(c):
    """Objects painted in the PEN colour — what the back tip should leave."""
    s = c.current_scene()
    out = []
    for o in s.hit_test_all(lbe.Vec2f(220.0, 200.0), 600.0):
        col = o.stroke_color
        if (col.r, col.g, col.b) == PEN_RGB:
            out.append(o)
    return out


def _diameter_for(area):
    return 2.0 * math.sqrt(area / math.pi)


# A back tip comfortably inside its own band, and one pressed hard enough to
# cross a calibrated palm threshold.
BACK_TIP = _diameter_for(1200.0)
BACK_TIP_HARD = _diameter_for(6000.0)


# ── the back tip runs its assigned tool ─────────────────────────────────────

def test_back_tip_set_to_pen_draws_in_the_pen_colour():
    c, ih, _ = _board(palm_area=4000.0)
    before = c.current_scene().object_count
    _touch_stroke(ih, BACK_TIP)
    strokes = _pen_strokes(c)
    assert strokes, "the back tip drew nothing in the pen colour"
    assert strokes[0].type_name == "freehand", \
        f"back tip set to Pen committed a {strokes[0].type_name}"
    assert c.current_scene().object_count > before


def test_a_back_tip_pressed_hard_still_draws_rather_than_erasing():
    """The footprint grows with pressure and crosses the calibrated palm
    threshold. Judging it a palm there throws away the assigned tool — with
    palm_eraser that means the Pen rubs out the board instead of writing."""
    c, ih, _ = _board(mode="palm_eraser", palm_area=4000.0)
    before = c.current_scene().object_count
    _touch_stroke(ih, BACK_TIP_HARD)
    assert _pen_strokes(c), "a firmly-pressed back tip drew nothing"
    assert c.current_scene().object_count >= before, \
        "the back tip erased existing ink instead of drawing"


def test_a_back_tip_pressed_hard_is_not_swallowed_by_palm_rejection():
    c, ih, _ = _board(mode="palm_rejection", palm_area=4000.0)
    _touch_stroke(ih, BACK_TIP_HARD)
    assert _pen_strokes(c), "palm rejection swallowed the stylus back tip"


def test_a_stroke_that_grows_mid_word_does_not_turn_into_an_erase():
    """Press harder halfway through a word and the contact crosses the palm
    threshold. The press-time decision is the one that counts."""
    c, ih, _ = _board(mode="palm_eraser", palm_area=4000.0)
    # Driven by hand, because the tell is only visible WHILE the stroke is in
    # flight: the end of the sequence banks any erase and clears the mask, so
    # a check afterwards sees a tidy scene either way — and the pen stroke
    # commits regardless, which is what makes this one easy to miss.
    ih.touch_event(_Touch(QEvent.TouchBegin,
                          [_Point(1, QPointF(150.0, 200.0), "Pressed", BACK_TIP)]))
    assert ih._active_touches[1]["tool"] == "normal", "the press was not a tip"
    for i in range(1, 8):
        ih.touch_event(_Touch(QEvent.TouchUpdate,
                              [_Point(1, QPointF(150.0 + i * 20, 200.0),
                                      "Updated", BACK_TIP + 9.0 * i)]))
        assert ih._active_touches[1]["tool"] == "normal", \
            f"the tip was re-judged a palm at sample {i}"
        assert not ih._touch_erasing, \
            f"an erase started under the stroke at sample {i}"
    ih.touch_event(_Touch(QEvent.TouchEnd,
                          [_Point(1, QPointF(300.0, 200.0), "Released", BACK_TIP)]))
    assert _pen_strokes(c), "the stroke never committed"


def test_the_back_tip_still_honours_a_non_pen_assignment():
    """The fix must not pin the back tip to one tool — it removes the palm
    override, it doesn't replace the assignment."""
    c, ih, _ = _board(back_tool="highlighter", palm_area=4000.0)
    _touch_stroke(ih, BACK_TIP)
    s = c.current_scene()
    hl = [o for o in s.hit_test_all(lbe.Vec2f(220.0, 200.0), 600.0)
          if o.type_name == "highlighter"]
    assert hl, "back tip set to Highlighter did not commit a highlighter"


# ── palm handling is still palm handling ────────────────────────────────────

def test_a_palm_is_still_a_palm_outside_dual_pen():
    """The exemption is scoped to Dual Pen. With an ordinary tool selected a
    big contact is what it has always been."""
    c, ih, _ = _board(mode="palm_eraser", palm_area=4000.0)
    c.set_tool("pen")
    _touch_stroke(ih, BACK_TIP_HARD)
    assert not _pen_strokes(c), "a palm drew ink with the pen tool selected"
    assert ih._is_dual_pen_tip_contact("Pressed") is False, \
        "the tip exemption leaked outside Dual Pen"


def test_only_the_opening_contact_gets_the_tip_exemption():
    """A hand landing while a tip is already drawing is exactly what palm
    handling is for, so it must still go through it."""
    c, ih, _ = _board(palm_area=4000.0)
    assert ih._is_dual_pen_tip_contact("Pressed"), \
        "the opening contact should be treated as a tip"
    ih._touch_drawing = True
    assert not ih._is_dual_pen_tip_contact("Pressed"), \
        "a contact landing mid-stroke must not be treated as the tip"
    ih._touch_drawing = False
    ih._active_touches[7] = {"pos": QPointF(), "tool": "normal"}
    assert not ih._is_dual_pen_tip_contact("Pressed"), \
        "a second live contact must not be treated as the tip"
    assert not ih._is_dual_pen_tip_contact("Updated"), \
        "only a press opens an interaction"


# ── the Dual Pen gate ───────────────────────────────────────────────────────

def test_the_back_tip_works_on_a_frame_that_cannot_measure_it():
    """``calibrated=True`` with ``median_area == 0`` is a real state, not a
    corrupt one: the mouse route produces it by construction and so does an IR
    frame that reports no contact ellipse. The classifier can never answer
    "eraser" there, so the back tip fell through to the legacy size heuristic
    and was called a palm — assigned Pen, it erased.

    The board is still perfectly usable: in Dual Pen the contact that opens a
    stroke is a tip, and on this hardware the front tip arrives as a click, so
    a touch contact IS the back tip. Withdrawing the mode here would have
    punished exactly the boards it exists for.
    """
    cal = _calibration(back_area=0.0)
    assert cal.classification_thresholds["back_tip"] is None, \
        "precondition: the classifier has nothing to match the back tip on"
    assert cal.dual_pen_available, "Dual Pen should still be offered"

    c, ih, _ = _board(mode="palm_eraser", back_area=0.0)
    _touch_stroke(ih, BACK_TIP_HARD)
    assert _pen_strokes(c), \
        "the back tip drew nothing on a frame with no contact geometry"


# ── the tablet path ─────────────────────────────────────────────────────────

def test_a_tablet_back_tip_press_is_not_relabelled_as_the_front_tip():
    """pointerType() is the digitizer stating which end is down. A setting
    about what a MOUSE click means must not override it — that made the press
    run the front tool while the moves and release ran the back one, so the
    stroke previewed in one tool's colour and committed in another's."""
    c, ih, _ = _board(front_tool="highlighter", back_tool="pen")
    eraser_tip = QPointingDevice.PointerType.Eraser
    ih.tablet_event(_Tablet(QEvent.TabletPress, QPointF(150.0, 600.0), eraser_tip))
    # The press must have opened a PEN preview, not a highlighter one.
    assert ih._active_tip == "back", "the back tip was relabelled at press"
    assert not c._live_is_highlighter, \
        "back tip set to Pen previewed as a highlighter"
    for x in (170.0, 190.0, 210.0):
        ih.tablet_event(_Tablet(QEvent.TabletMove, QPointF(x, 600.0), eraser_tip))
    ih.tablet_event(_Tablet(QEvent.TabletRelease, QPointF(210.0, 600.0), eraser_tip))
    hits = [o for o in c.current_scene().hit_test_all(lbe.Vec2f(180.0, 600.0), 200.0)]
    assert hits and hits[-1].type_name == "freehand", \
        "the back tip did not commit a pen stroke"
    col = hits[-1].stroke_color
    assert (col.r, col.g, col.b) == PEN_RGB, \
        f"committed in rgb({col.r},{col.g},{col.b}), expected the pen colour"


def test_a_real_mouse_press_still_selects_the_front_tip():
    """The override exists for boards that report the front tip as a click.
    Exempting the tablet path must not disable it for actual mouse input."""
    from PySide6.QtGui import QMouseEvent
    c, ih, _ = _board(front_tool="pen", back_tool="eraser")
    ih._active_tip = "back"
    ev = QMouseEvent(QEvent.MouseButtonPress, QPointF(400.0, 400.0),
                     Qt.LeftButton, Qt.LeftButton, Qt.NoModifier)
    ih.mouse_press(ev)
    assert ih._active_tip == "front", \
        "mouse_is_stylus_front no longer resolves a click to the front tip"



def test_the_pen_and_the_dual_pen_keep_separate_colours():
    """The feature the fixture above had to learn about.

    Two tools, two colours. Setting one must not move the other, and the
    dual pen must commit in ITS colour — that is the whole point of a
    stylus whose barrel is a different pen from its tip.
    """
    c, ih, _ = _board(palm_area=4000.0)
    c.set_pen_color(QColor(10, 200, 10))
    c.set_dual_pen_color(QColor(*PEN_RGB))
    assert (c._pen_color.red(), c._pen_color.green(), c._pen_color.blue())         == (10, 200, 10), "setting the dual pen's colour moved the pen's"
    dp = c.dual_pen_color          # a property, not a getter
    assert (dp.red(), dp.green(), dp.blue()) == PEN_RGB

    _touch_stroke(ih, BACK_TIP)
    assert _pen_strokes(c), "the dual pen did not commit in its own colour"


def test_neither_colour_is_still_the_factory_blue_by_accident():
    """A test that passes because both tools happen to sit on the same
    default proves nothing. Move them apart first."""
    c, _ih, _ = _board()
    c.set_pen_color(QColor(200, 30, 30))
    c.set_dual_pen_color(QColor(30, 30, 200))
    assert c._pen_color.rgb() != c._dual_pen_color.rgb(),         "the two colours are aliased to one value again"

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
