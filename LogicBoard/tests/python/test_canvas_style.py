"""
test_canvas_style.py — Scrollable List Style, on the existing page model.

Runs under pytest, and also standalone (``python tests/python/test_canvas_style.py``)
because the repo's pytest install is currently broken on Python 3.11.

What is worth protecting here
-----------------------------
The list is a second ARRANGEMENT of the board's pages, not a second set of
pages. That distinction is the whole feature: if the list ever grew its own
page objects, then editing, undo, imports and page add/delete would each need
teaching about two models, and they would drift apart on the first one that
wasn't updated.

So the tests below check the arrangement against ``board.page_count`` and the
real scenes, and — the important one — that a stroke drawn on the third sheet
lands in the third page's scene and nowhere else. Everything downstream
(tools, erasers, selection, per-page undo stacks) reaches the right page
through ``current_scene`` / ``current_stack``, so proving the current page
follows the pointer is proving all of them.

The other half is that One Page Style is untouched: same coordinates, same
zoom, and completely deaf to Zoom Sensitivity.
"""
import math
import os
import sys
import tempfile
import time
from pathlib import Path

os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")
sys.path.insert(0, os.path.abspath(os.path.join(os.path.dirname(__file__),
                                                "..", "..")))

from PySide6.QtWidgets import QApplication
from PySide6.QtCore import QPointF, QEvent, Qt, QSizeF
from PySide6.QtGui import QColor

_app = QApplication.instance() or QApplication([])

import logicboard_engine as lbe
from ui.canvas.canvas_widget import CanvasWidget
from ui.canvas.input_handler import InputHandler


# ── scaffolding ─────────────────────────────────────────────────────────────

class _Point:
    def __init__(self, pid, pos, state="Updated", diameter=16.0):
        self._i, self._p, self._s, self._d = pid, pos, state, diameter
    def id(self):        return self._i
    def position(self):  return self._p
    def ellipseDiameters(self): return QSizeF(self._d, self._d)
    def state(self):
        from types import SimpleNamespace
        return SimpleNamespace(name=self._s)


class _Touch:
    def __init__(self, t, pts): self._t, self._p = t, pts
    def type(self):   return self._t
    def points(self): return self._p
    def accept(self): pass


def _board(pages=4):
    c = CanvasWidget(); c.resize(900, 700)
    ih = InputHandler(c); c._input_handler = ih
    c.set_tool("pen"); c.set_pen_color(QColor(255, 0, 0)); c.set_pen_width(4.0)
    c._touch_mode = "palm_off"
    for _ in range(pages - 1):
        c.add_page()
    for i in range(c.page_count):
        s = c.scene_at(i)
        o = lbe.FreehandStroke()
        o.set_points([(100.0 + k * 20, 150.0 + i * 30) for k in range(20)])
        o.stroke_width = 6.0
        o.stroke_color = lbe.RGBA(min(255, 30 + i * 40), 90, 200, 255)
        s.add_object(o)
    c.invalidate_cache()
    return c, ih


def _counts(c):
    return [c.scene_at(i).object_count for i in range(c.page_count)]


def _stroke_on(ih, c, start, n=8):
    ih.touch_event(_Touch(QEvent.TouchBegin, [_Point(1, start, "Pressed")]))
    for k in range(1, n):
        ih.touch_event(_Touch(QEvent.TouchUpdate,
                              [_Point(1, QPointF(start.x() + k * 10,
                                                 start.y() + k * 3))]))
    ih.touch_event(_Touch(QEvent.TouchEnd,
                          [_Point(1, QPointF(start.x() + n * 10,
                                             start.y() + n * 3), "Released")]))


def _fingers(ih, cy, spread, n, cx=450.0):
    """``n`` contacts evenly around a circle of radius ``spread`` at ``cy``.

    Laid out on a circle so the gesture's own measure — the mean distance
    from the centroid — comes out as exactly ``spread`` for ANY n. That is
    what lets the two- and five-contact tests share their numbers.
    """
    pts = []
    for k in range(n):
        a = 2 * math.pi * k / n
        pts.append(_Point(100 + k, QPointF(cx + math.cos(a) * spread,
                                           cy + math.sin(a) * spread)))
    ih.touch_event(_Touch(QEvent.TouchUpdate, pts))


def _two(ih, cy, spread, cx=450.0):
    _fingers(ih, cy, spread, 2, cx)


def _five(ih, cy, spread, cx=450.0):
    _fingers(ih, cy, spread, 5, cx)


def _reset_gesture(ih):
    ih._gesture_active = False
    ih._active_touches.clear()
    # Clears the tracking flag AND the locked gesture kind. Resetting only the
    # flag leaves the lock behind, and the next gesture skips its own
    # classification and inherits the last one.
    ih._end_gesture()


# ── One Page Style is untouched ─────────────────────────────────────────────

def test_one_page_is_the_default_and_maps_coordinates_as_before():
    c, _ = _board()
    assert c.canvas_style == "one_page"
    assert c.screen_to_scene(QPointF(100, 100)) == QPointF(100, 100)
    assert c.scene_to_screen(QPointF(100, 100)) == QPointF(100, 100)


def test_zoom_sensitivity_has_no_effect_in_one_page_style():
    """The requirement stated most sharply: the setting must not reach the
    one-page view at all. It has its own scale field for exactly this."""
    c, ih = _board()
    c.set_canvas_style("one_page")
    c.set_zoom_sensitivity(3.0)
    before = c.view_scale
    _reset_gesture(ih)
    _five(ih, 300.0, 60.0)
    _five(ih, 300.0, 240.0)          # a huge spread change
    assert c.view_scale == before, \
        f"one-page zoom moved from {before} to {c.view_scale}"


def test_drawing_in_one_page_style_still_targets_the_current_page():
    c, ih = _board()
    c.goto_page(1)
    before = _counts(c)
    _stroke_on(ih, c, QPointF(200.0, 300.0))
    after = _counts(c)
    assert after[1] == before[1] + 1
    assert [after[i] for i in (0, 2, 3)] == [before[i] for i in (0, 2, 3)]


# ── the list is an arrangement of the SAME pages ────────────────────────────

def test_the_list_shows_every_page_of_the_board_in_order():
    c, _ = _board(pages=5)
    c.set_canvas_style("scroll_list")
    layout = c.page_layout()
    assert [i for i, _r in layout] == list(range(c.page_count)), \
        "the list is not the board's pages, in the board's order"
    assert len(layout) == c.board.page_count, \
        "the list has a different number of pages than the document model"
    tops = [r.top() for _i, r in layout]
    assert tops == sorted(tops), "pages are not stacked top to bottom"
    for (i, a), (j, b) in zip(layout, layout[1:]):
        assert b.top() > a.bottom(), f"page {j} overlaps page {i} — no spacing"


def test_adding_and_deleting_pages_updates_the_list():
    c, _ = _board(pages=3)
    c.set_canvas_style("scroll_list")
    assert len(c.page_layout()) == 3
    c.add_page()
    assert len(c.page_layout()) == 4, "a new page never reached the list"
    c.delete_page(1)
    assert len(c.page_layout()) == 3, "a deleted page is still in the list"
    assert len(c.page_layout()) == c.board.page_count


def test_a_new_board_works_in_list_style():
    c, _ = _board(pages=1)
    c.set_canvas_style("scroll_list")
    layout = c.page_layout()
    assert len(layout) == 1
    assert layout[0][1].height() > 0


def test_switching_styles_does_not_disturb_the_pages():
    c, _ = _board(pages=4)
    before_counts = _counts(c)
    c.goto_page(2)
    for style in ("scroll_list", "one_page", "scroll_list", "one_page"):
        c.set_canvas_style(style)
    assert c.page_count == 4
    assert _counts(c) == before_counts, "page contents changed with the view"
    assert c.current_page == 2, "the current page moved when the style did"


# ── input routes to the page under the pointer ──────────────────────────────

def test_a_point_resolves_to_the_sheet_under_it():
    c, _ = _board()
    c.set_canvas_style("scroll_list")
    c.set_list_scale(0.30)
    for i in range(c.page_count):
        r = c.page_rect_on_screen(i)
        if r is None or r.bottom() < 0 or r.top() > c.height():
            continue
        assert c.page_at_screen(r.center()) == i, f"page {i} not hit at its centre"
    # The gaps between sheets belong to no page.
    r0 = c.page_rect_on_screen(0)
    assert c.page_at_screen(QPointF(r0.center().x(), r0.bottom() + 6)) is None


def test_drawing_on_the_third_sheet_lands_in_the_third_page():
    """The heart of it. Every tool reaches its page through current_scene(),
    so a stroke arriving in the right scene is the proof that erasing,
    selection and per-page undo do too."""
    c, ih = _board()
    c.set_canvas_style("scroll_list")
    c.set_list_scale(0.30)
    r = c.page_rect_on_screen(2)
    assert r is not None and r.top() < c.height(), "page 2 isn't on screen to test"
    before = _counts(c)
    _stroke_on(ih, c, QPointF(r.x() + r.width() * 0.25,
                              r.y() + r.height() * 0.3))
    after = _counts(c)
    assert c.current_page == 2, "the touched sheet did not become current"
    assert after[2] == before[2] + 1, "the stroke did not reach page 2"
    assert [after[i] for i in (0, 1, 3)] == [before[i] for i in (0, 1, 3)], \
        "the stroke also landed on another page"


def test_coordinates_are_page_local_inside_the_sheet():
    """A press at a sheet's top-left is scene (0,0) for THAT page — which is
    what makes every existing tool work unchanged."""
    c, _ = _board()
    c.set_canvas_style("scroll_list")
    c.set_list_scale(0.30)
    c.goto_page(1)
    r = c.page_rect_on_screen(1)
    p = c.screen_to_scene(QPointF(r.x(), r.y()))
    assert abs(p.x()) < 1.0 and abs(p.y()) < 1.0, f"top-left mapped to {p}"
    # …and the mapping round-trips.
    mid = QPointF(r.x() + r.width() / 2, r.y() + r.height() / 2)
    back = c.scene_to_screen(c.screen_to_scene(mid))
    assert abs(back.x() - mid.x()) < 1.0 and abs(back.y() - mid.y()) < 1.0


# ── the scroll/zoom gesture ─────────────────────────────────────────────────
#
# TWO contacts is the whole gesture. It used to take five, which nobody
# discovers on their own — the tests below therefore lead with two, and the
# five-contact ones that follow are there to prove a whole hand on the board
# still navigates rather than falling through to some other path.

def test_two_fingers_scroll_the_list():
    c, ih = _board(pages=6)
    c.set_canvas_style("scroll_list")
    c.set_list_scale(0.30)
    _reset_gesture(ih)
    assert c._max_scroll() > 0, "six pages should not fit in one viewport"
    _two(ih, 500.0, 80.0)
    _two(ih, 300.0, 80.0)            # fingers move up -> content scrolls down
    assert c._scroll_y > 0, "two fingers did not scroll the list"


def test_two_fingers_zoom_the_list():
    c, ih = _board()
    c.set_canvas_style("scroll_list")
    c.set_list_scale(0.30)
    c.set_zoom_sensitivity(1.0)
    _reset_gesture(ih)
    before = c.list_scale
    _two(ih, 300.0, 80.0)
    _two(ih, 300.0, 140.0)           # pinch open
    assert c.list_scale > before, "a two-finger pinch open did not zoom in"
    zoomed = c.list_scale
    _two(ih, 300.0, 70.0)            # pinch closed
    assert c.list_scale < zoomed, "a two-finger pinch closed did not zoom out"


def test_two_fingers_never_draw():
    """The failure this guards is specific: two contacts used to be SWALLOWED
    rather than acted on, and the one thing worse than a gesture that does
    nothing is a gesture that leaves ink. The first finger starts a stroke
    before the second lands, exactly as a real hand does."""
    c, ih = _board()
    c.set_canvas_style("scroll_list")
    c.set_list_scale(0.30)
    before = _counts(c)
    r = c.page_rect_on_screen(0)
    ih.touch_event(_Touch(QEvent.TouchBegin,
                          [_Point(1, QPointF(r.center()), "Pressed")]))
    for _ in range(3):
        _two(ih, r.center().y(), 90.0)
    ih.touch_event(_Touch(QEvent.TouchEnd,
                          [_Point(k, QPointF(400.0, 400.0), "Released")
                           for k in (1, 100, 101)]))
    assert _counts(c) == before, "a two-finger gesture left ink on the board"
    assert not c._live_active, "a live stroke survived the gesture"


def test_zoom_sensitivity_has_no_effect_on_one_page_with_two_fingers():
    """The sensitivity setting must not reach the one-page view by ANY
    route, and two contacts is now a route that reaches the zoom code."""
    c, ih = _board()
    c.set_canvas_style("one_page")
    c.set_zoom_sensitivity(3.0)
    before = c.view_scale
    _reset_gesture(ih)
    _two(ih, 300.0, 60.0)
    _two(ih, 300.0, 240.0)
    assert c.view_scale == before, (
        f"one-page zoom moved from {before} to {c.view_scale}")


def test_five_fingers_zoom_the_list():
    c, ih = _board()
    c.set_canvas_style("scroll_list")
    c.set_list_scale(0.30)
    c.set_zoom_sensitivity(1.0)
    _reset_gesture(ih)
    before = c.list_scale
    _five(ih, 300.0, 80.0)
    _five(ih, 300.0, 140.0)          # hand opens
    assert c.list_scale > before, "opening the hand did not zoom in"
    zoomed = c.list_scale
    _five(ih, 300.0, 70.0)           # hand closes
    assert c.list_scale < zoomed, "closing the hand did not zoom out"


def test_zoom_sensitivity_controls_how_hard_it_zooms():
    def zoom_with(sens):
        c, ih = _board()
        c.set_canvas_style("scroll_list")
        c.set_list_scale(0.30)
        c.set_zoom_sensitivity(sens)
        _reset_gesture(ih)
        _five(ih, 300.0, 80.0)
        _five(ih, 300.0, 120.0)
        return c.list_scale

    low, high = zoom_with(0.5), zoom_with(2.5)
    assert high > low, f"higher sensitivity zoomed less ({high} vs {low})"


def test_five_fingers_scroll_the_list():
    c, ih = _board(pages=6)
    c.set_canvas_style("scroll_list")
    c.set_list_scale(0.30)
    _reset_gesture(ih)
    assert c._max_scroll() > 0, "six pages should not fit in one viewport"
    _five(ih, 500.0, 80.0)
    _five(ih, 300.0, 80.0)           # hand moves up -> content scrolls down
    assert c._scroll_y > 0, "five fingers did not scroll the list"


def test_five_fingers_never_draw():
    """A five-finger gesture must be navigation, not five pens. The gesture
    latch also has to abandon anything the first finger started before its
    friends landed."""
    c, ih = _board()
    c.set_canvas_style("scroll_list")
    c.set_list_scale(0.30)
    before = _counts(c)
    # One finger lands and starts to draw, then four more join.
    r = c.page_rect_on_screen(0)
    ih.touch_event(_Touch(QEvent.TouchBegin,
                          [_Point(1, QPointF(r.center()), "Pressed")]))
    for _ in range(3):
        _five(ih, r.center().y(), 90.0)
    ih.touch_event(_Touch(QEvent.TouchEnd,
                          [_Point(k, QPointF(400.0, 400.0), "Released")
                           for k in (1, 100, 101, 102, 103, 104)]))
    assert _counts(c) == before, "a five-finger gesture left ink on the board"
    assert not c._live_active, "a live stroke survived the gesture"


# ── the live stroke must be drawn where it will be committed ───────────────

def _red_ink_bbox(c):
    """Bounding box of the red pen ink on screen, or None.

    Every pixel, and "red dominates" rather than "pure red": at list scale a
    6-unit pen is under 2 px wide and antialiased, so a coarse scan with a
    strict threshold finds nothing at all — which reads as "the stroke
    vanished" when the stroke is right there.
    """
    from PySide6.QtCore import QRectF
    img = c.grab().toImage()
    xs, ys = [], []
    for y in range(img.height()):
        for x in range(img.width()):
            px = img.pixel(x, y)
            r, g, b = (px >> 16) & 255, (px >> 8) & 255, px & 255
            if r > 110 and r > g + 60 and r > b + 60:
                xs.append(x); ys.append(y)
    if not xs:
        return None
    return QRectF(min(xs), min(ys), max(xs) - min(xs), max(ys) - min(ys))


def test_the_live_stroke_is_drawn_inside_its_sheet():
    """The reported bug: ink sprawled across the whole window while drawing
    and only snapped into the page on release.

    Every live overlay mapped scene points with the ONE-PAGE transform. In
    list style those points are page-local, so at view scale 1.0 with no page
    offset they landed relative to the widget origin at full size. On release
    the committed object was drawn by the (correct) list renderer, which is
    why it 'jumped' into place.
    """
    c, ih = _board()
    c.set_canvas_style("scroll_list")
    c.set_list_scale(0.30)
    r = c.page_rect_on_screen(1)
    assert r is not None
    start = QPointF(r.x() + r.width() * 0.2, r.y() + r.height() * 0.3)
    ih.touch_event(_Touch(QEvent.TouchBegin, [_Point(1, start, "Pressed")]))
    for k in range(1, 10):
        ih.touch_event(_Touch(QEvent.TouchUpdate,
                              [_Point(1, QPointF(start.x() + k * 8,
                                                 start.y() + k * 5))]))
    _app.processEvents()
    bbox = _red_ink_bbox(c)          # MID-stroke, before release
    assert bbox is not None, "no live ink was rendered at all"
    assert r.adjusted(-4, -4, 4, 4).contains(bbox), (
        f"live ink at {bbox} is outside its sheet {r} — it is being drawn "
        f"with the one-page transform")
    ih.touch_event(_Touch(QEvent.TouchEnd,
                          [_Point(1, QPointF(start.x() + 80, start.y() + 50),
                                  "Released")]))


def test_the_committed_stroke_lands_where_the_preview_was():
    """Preview and commit must agree — that agreement is the bug's real
    symptom, and comparing the two renders is the only way to see it."""
    c, ih = _board()
    c.set_canvas_style("scroll_list")
    c.set_list_scale(0.30)
    r = c.page_rect_on_screen(1)
    start = QPointF(r.x() + r.width() * 0.25, r.y() + r.height() * 0.35)
    ih.touch_event(_Touch(QEvent.TouchBegin, [_Point(1, start, "Pressed")]))
    for k in range(1, 10):
        ih.touch_event(_Touch(QEvent.TouchUpdate,
                              [_Point(1, QPointF(start.x() + k * 8, start.y()))]))
    _app.processEvents()
    live = _red_ink_bbox(c)
    ih.touch_event(_Touch(QEvent.TouchEnd,
                          [_Point(1, QPointF(start.x() + 80, start.y()), "Released")]))
    _app.processEvents()
    committed = _red_ink_bbox(c)
    assert live is not None and committed is not None
    assert abs(live.x() - committed.x()) < 12 and abs(live.y() - committed.y()) < 12,         f"preview at {live} but committed at {committed} — the stroke jumped"


# ── trackpad zoom ───────────────────────────────────────────────────────────

class _Pinch:
    """Duck-typed NativeGesture. The real QNativeGestureEvent constructor
    needs a live QPointingDevice and segfaults with None on PySide6 6.11,
    and the handler only ever asks for these three."""
    def __init__(self, delta, y=300.0): self._d, self._y = delta, y
    def gestureType(self): return Qt.ZoomNativeGesture
    def value(self): return self._d
    def position(self): return QPointF(450.0, self._y)


def _ctrl_wheel(c, notches, y=300.0):
    from PySide6.QtCore import QPoint
    from PySide6.QtGui import QWheelEvent
    c.wheelEvent(QWheelEvent(QPointF(450, y), QPointF(450, y), QPoint(0, 0),
                             QPoint(0, int(120 * notches)), Qt.NoButton,
                             Qt.ControlModifier, Qt.NoScrollPhase, False))


def test_a_trackpad_pinch_zooms_the_list():
    """A laptop trackpad never produces five contacts, so without this there
    is no way to zoom the list on one."""
    c, _ = _board(pages=6)
    c.set_canvas_style("scroll_list")
    c.set_list_scale(0.30)
    c.set_zoom_sensitivity(1.0)
    assert c._handle_native_gesture(_Pinch(0.25)) is True
    assert c.list_scale > 0.30, "pinch open did not zoom in"
    up = c.list_scale
    c._handle_native_gesture(_Pinch(-0.25))
    assert c.list_scale < up, "pinch closed did not zoom out"


def test_ctrl_wheel_zooms_and_plain_wheel_still_scrolls():
    c, _ = _board(pages=6)
    c.set_canvas_style("scroll_list")
    c.set_list_scale(0.30)
    _ctrl_wheel(c, 3)
    assert c.list_scale > 0.30, "ctrl+wheel did not zoom in"
    zoomed = c.list_scale
    _ctrl_wheel(c, -3)
    assert c.list_scale < zoomed, "ctrl+wheel did not zoom out"
    # Plain wheel must keep meaning SCROLL.
    from PySide6.QtCore import QPoint
    from PySide6.QtGui import QWheelEvent
    scale, scroll = c.list_scale, c._scroll_y
    c.wheelEvent(QWheelEvent(QPointF(450, 300), QPointF(450, 300), QPoint(0, 0),
                             QPoint(0, -360), Qt.NoButton, Qt.NoModifier,
                             Qt.NoScrollPhase, False))
    assert c.list_scale == scale, "a plain wheel zoomed instead of scrolling"
    assert c._scroll_y > scroll, "a plain wheel stopped scrolling"


def _wheel(c, dx=0, dy=0, mods=Qt.NoModifier, at=(450.0, 300.0)):
    from PySide6.QtCore import QPoint
    from PySide6.QtGui import QWheelEvent
    p = QPointF(*at)
    c.wheelEvent(QWheelEvent(p, p, QPoint(0, 0), QPoint(int(dx), int(dy)),
                             Qt.NoButton, mods, Qt.NoScrollPhase, False))


# ── sideways scrolling ──────────────────────────────────────────────────────
#
# Zoom the list past the window width and a sheet genuinely runs off both
# edges. Before this there was no way to reach either one: the gesture read
# only the vertical component of the centroid and threw the rest away, and the
# bar on the right wall moves the view up and down.

def _wide(pages=4, scale=2.0):
    """A board zoomed until the sheets are wider than the viewport."""
    c, ih = _board(pages=pages)
    c.set_canvas_style("scroll_list")
    c.set_list_scale(scale)
    assert c._max_scroll_x() > 0, "the sheet should not fit in the window"
    return c, ih


def test_two_fingers_scroll_sideways():
    c, ih = _wide()
    _reset_gesture(ih)
    _two(ih, 300.0, 80.0, cx=600.0)
    _two(ih, 300.0, 80.0, cx=400.0)      # fingers sweep left
    assert c._scroll_x > 0, "two fingers did not scroll sideways"
    right = c._scroll_x
    _two(ih, 300.0, 80.0, cx=700.0)      # and back the other way
    assert c._scroll_x < right, "the sideways scroll only went one way"


def test_the_sideways_offset_is_clamped_to_the_content():
    """The same guarantee the vertical offset has. An unclamped offset parks
    the view in blank space beside the stack, which renders as a dead board."""
    c, _ = _wide()
    c.scroll_by_x(99_999)
    assert abs(c._scroll_x - c._max_scroll_x()) < 0.5, "scrolled past the edge"
    c.scroll_by_x(-99_999)
    assert c._scroll_x == 0.0, "scrolled past the near edge"


def test_zooming_out_pulls_the_sideways_offset_back():
    """Zooming out shrinks the content under a live offset — the horizontal
    half of the bug clamp_scroll already fixed for the vertical one."""
    c, _ = _wide()
    c.scroll_by_x(c._max_scroll_x())
    assert c._scroll_x > 0
    c.set_list_scale(0.10)               # everything fits now
    assert c._max_scroll_x() == 0.0
    assert c._scroll_x == 0.0, "the view stayed parked beside the stack"


def test_hit_testing_follows_the_sideways_scroll():
    """The point of routing input through page_rect_on_screen: scroll sideways
    and a press still lands on the sheet it looks like it is on, at the scene
    coordinates that sheet uses."""
    c, _ = _wide()
    c.scroll_by_x(200.0)
    r = c.page_rect_on_screen(0)
    centre = QPointF(r.center().x(), r.center().y())
    assert c.page_at_screen(centre) == 0, "the press missed the sheet"
    back = c.scene_to_screen(c.screen_to_scene(centre))
    assert abs(back.x() - centre.x()) < 1.0 and abs(back.y() - centre.y()) < 1.0


def test_sideways_scrolling_does_nothing_in_one_page_style():
    """One page is fitted to the viewport by definition — there is nothing off
    to the side, and a sideways gesture must not slide the page out from under
    the user."""
    c, ih = _board()
    c.set_canvas_style("one_page")
    assert c._max_scroll_x() == 0.0
    assert c.scroll_by_x(250.0) is False
    _reset_gesture(ih)
    _two(ih, 300.0, 80.0, cx=600.0)
    _two(ih, 300.0, 80.0, cx=300.0)
    assert c._scroll_x == 0.0, "one-page style scrolled sideways"


def test_shift_wheel_scrolls_sideways():
    """A mouse has no two-finger gesture and the bar only goes up and down, so
    without this a mouse-only machine cannot reach the far side of a zoomed-in
    sheet at all."""
    c, _ = _wide()
    _wheel(c, dy=-360, mods=Qt.ShiftModifier)
    assert c._scroll_x > 0, "shift+wheel did not scroll sideways"
    out = c._scroll_x
    _wheel(c, dy=360, mods=Qt.ShiftModifier)
    assert c._scroll_x < out, "shift+wheel only scrolled one way"


def test_a_plain_wheel_still_means_vertical():
    c, _ = _wide()
    _wheel(c, dy=-360)
    assert c._scroll_y > 0, "a plain wheel stopped scrolling the list"
    assert c._scroll_x == 0.0, "a plain wheel scrolled sideways"


def test_the_page_label_stays_in_view_when_scrolled_sideways():
    """A strictly sheet-aligned label slides off the left edge on a sheet
    wider than the window — losing the page number exactly when the user is
    navigating. It is clamped into view instead."""
    c, _ = _wide(pages=4, scale=2.0)
    c.scroll_by_x(c._max_scroll_x())      # hard right
    c.invalidate_cache()
    img = c.grab().toImage()
    r = c.page_rect_on_screen(1)
    assert r.left() < 0, "the sheet should be scrolled off the left edge"
    bg = img.pixel(img.width() // 2, 2)
    ink = 0
    for y in range(max(0, int(r.top() - c._LIST_GAP)), max(1, int(r.top()) - 1)):
        for x in range(0, 120):
            if img.pixel(x, y) != bg:
                ink += 1
    assert ink > 0, "the page label scrolled out of the window with the sheet"



# ── gesture arbitration ─────────────────────────────────────────────────────
#
# ONE action per touch sequence, decided from the opening movement and then
# locked. Every sample used to be read on its own terms, so the same swipe
# scrolled, drifted sideways AND rescaled at once, each from its own component
# of one movement — and the small movement no hand can avoid read as the board
# twitching. These tests are that mechanism, from both ends: it has to commit,
# and it has to refuse to change its mind.


def _pair(ih, cx, cy, spread=80.0):
    """Two contacts either side of (cx, cy), ``spread`` apart from centre."""
    ih.touch_event(_Touch(QEvent.TouchUpdate,
                          [_Point(100, QPointF(cx + spread, cy)),
                           _Point(101, QPointF(cx - spread, cy))]))


def _list_board(pages=8, scale=0.30):
    c, ih = _board(pages=pages)
    c.set_canvas_style("scroll_list")
    c.set_list_scale(scale)
    _reset_gesture(ih)
    return c, ih


def test_a_gesture_inside_the_dead_zone_does_nothing():
    """A hand on a board is never perfectly still. Without a dead zone that
    noise is a scroll of a pixel or two in a direction nobody chose."""
    c, ih = _list_board()
    _pair(ih, 450, 300)
    for k in range(8):                       # a few px of wobble, both ways
        _pair(ih, 450 + (k % 2) * 3, 300 + (k % 2) * 3)
    assert ih._gesture_kind == "pending", "classified on jitter alone"
    assert c._scroll_y == 0.0 and c._scroll_x == 0.0, "jitter scrolled the board"
    assert abs(c.list_scale - 0.30) < 1e-9, "jitter zoomed the board"


def test_a_vertical_swipe_that_drifts_sideways_stays_vertical():
    """Nobody swipes in a straight line. The sideways component of a swipe
    that is plainly vertical has to be dropped, not acted on."""
    c, ih = _list_board()
    _pair(ih, 450, 500)
    for k in range(1, 11):                   # 200 px up, 60 px of drift
        _pair(ih, 450 + k * 6, 500 - k * 20)
    assert ih._gesture_kind == "vertical"
    assert c._scroll_y > 0, "the swipe did not scroll"
    assert c._scroll_x == 0.0, "the drift leaked into a sideways scroll"


def test_a_swipe_whose_fingers_splay_does_not_zoom():
    """Two fingers never travel perfectly in parallel, so a swipe always
    changes the spread a little. That stray component must not out-argue the
    swipe itself — the board zooming when the user scrolled is the complaint
    this whole mechanism exists for."""
    c, ih = _list_board()
    _pair(ih, 450, 500, spread=80)
    for k in range(1, 11):                   # swipe up while opening the hand
        _pair(ih, 450, 500 - k * 20, spread=80 + k * 4)
    assert ih._gesture_kind == "vertical"
    assert abs(c.list_scale - 0.30) < 1e-9, "a scroll zoomed the list"


def _spot_under(c, screen_y):
    """"This spot on this sheet" — (page index, fraction down that page).

    The honest measure of where the fingers are, and therefore of what an
    anchored zoom has to preserve. The obvious alternative, dividing a list
    coordinate by ``list_scale``, is NOT conserved: sheet heights scale with
    the zoom but the gaps between them are a constant number of screen
    pixels, so the two do not move together and the leftover gap error grows
    with the page index. Measuring against the one rectangle that is actually
    being scaled has no such error.
    """
    ly = screen_y + c._scroll_y
    for i, r in c.page_layout():
        if r.top() <= ly <= r.bottom():
            return i, (ly - r.top()) / r.height()
    return None, None


def _page_under(c, screen_y):
    return _spot_under(c, screen_y)[0]


def _pinch(ih, cy, spreads, cx=450.0):
    for sp in spreads:
        ih.touch_event(_Touch(QEvent.TouchUpdate,
                              [_Point(100, QPointF(cx + sp, cy)),
                               _Point(101, QPointF(cx - sp, cy))]))


def test_a_pinch_is_still_a_pinch():
    """Arbitration must not have made zoom unreachable."""
    c, ih = _list_board()
    _pinch(ih, 300.0, [60 + k * 20 for k in range(8)])
    assert ih._gesture_kind == "zoom"
    assert c.list_scale > 0.30, "a deliberate pinch did not zoom"


def test_a_pinch_zooms_about_the_fingers():
    """The paper under the fingers stays under the fingers.

    ``set_list_scale`` on its own changes the scale and NOTHING else, so the
    list grows from the top of page one. Pinch half way down a long board and
    the view marches off towards the earlier pages, carrying away the very
    sheet being pinched — the "it zooms to the top and takes me to the upper
    pages" report. Anchoring is the whole fix, so this is the test with teeth:
    not that the scale changed, but that the anchor held.
    """
    c, ih = _board(pages=12)
    c.set_canvas_style("scroll_list")
    c.set_list_scale(0.30)
    _reset_gesture(ih)
    c.scroll_by(c._max_scroll() * 0.45)      # well clear of both ends
    CY = 560.0                               # fingers low in the window
    before = _spot_under(c, CY)
    assert before[0] is not None, "the test point is not on a sheet"
    _pinch(ih, CY, [60 + k * 18 for k in range(9)])
    assert c.list_scale > 0.30, "the pinch did not zoom"
    after = _spot_under(c, CY)
    assert after[0] == before[0], (
        f"the fingers were on page {before[0]} and ended on {after[0]}")
    assert abs(after[1] - before[1]) < 0.01, (
        f"the fingers slid from {before[1]:.3f} to {after[1]:.3f} down page "
        f"{before[0]}")


def test_a_pinch_closed_anchors_at_the_fingers_too():
    """Zooming OUT has the same duty, and it is the direction where an
    unanchored scale is least obvious — everything shrinks towards the top,
    which reads as the board scrolling on its own."""
    c, ih = _board(pages=12)
    c.set_canvas_style("scroll_list")
    c.set_list_scale(0.90)
    _reset_gesture(ih)
    c.scroll_by(c._max_scroll() * 0.45)
    CY = 400.0
    before = _spot_under(c, CY)
    assert before[0] is not None, "the test point is not on a sheet"
    _pinch(ih, CY, [200 - k * 18 for k in range(9)])
    assert c.list_scale < 0.90, "the pinch did not zoom out"
    after = _spot_under(c, CY)
    assert after[0] == before[0] and abs(after[1] - before[1]) < 0.01, (
        f"the fingers slid from {before} to {after} while zooming out")


def test_where_the_fingers_are_is_what_decides_the_anchor():
    """Two identical pinches at different heights must land on different
    parts of the board. A zoom that ignores its anchor passes every
    scale-only check ever written, so this is the one that catches it."""
    def zoom_at(screen_y):
        c, ih = _board(pages=12)
        c.set_canvas_style("scroll_list")
        c.set_list_scale(0.30)
        _reset_gesture(ih)
        c.scroll_by(c._max_scroll() * 0.45)
        _pinch(ih, screen_y, [60 + k * 18 for k in range(9)])
        return c._scroll_y

    high, low = zoom_at(120.0), zoom_at(620.0)
    assert abs(high - low) > 50.0, (
        "pinching at the top and at the bottom of the window ended in the "
        "same place — the anchor is being ignored")


def test_the_trackpad_pinch_shares_the_anchored_entry():
    """Touch and trackpad both go through ``zoom_list_at``. They used to
    disagree: the trackpad anchored and touch did not, so the same gesture
    behaved differently depending on the hardware it arrived from."""
    c, _ = _board(pages=12)
    c.set_canvas_style("scroll_list")
    c.set_list_scale(0.30)
    c.scroll_by(c._max_scroll() * 0.45)
    CY = 560.0
    before = _spot_under(c, CY)
    assert before[0] is not None, "the test point is not on a sheet"

    class _PinchAt:
        def gestureType(self): return Qt.ZoomNativeGesture
        def value(self): return 0.30
        def position(self): return QPointF(450.0, CY)

    assert c._handle_native_gesture(_PinchAt()) is True
    assert c.list_scale > 0.30
    after = _spot_under(c, CY)
    assert after[0] == before[0] and abs(after[1] - before[1]) < 0.01, (
        f"the trackpad pinch stopped anchoring: {before} -> {after}")




def test_a_locked_gesture_never_reclassifies():
    """Once it is a vertical scroll it stays one until the hand comes off,
    however hard the movement turns. A classifier that can change its mind
    mid-gesture is one that changes it every few frames."""
    c, ih = _list_board()
    _pair(ih, 450, 500)
    for k in range(1, 6):
        _pair(ih, 450, 500 - k * 20)         # lock it vertical
    assert ih._gesture_kind == "vertical"
    for k in range(1, 12):
        _pair(ih, 450 + k * 40, 400)         # now yank it sideways, hard
    assert ih._gesture_kind == "vertical", "the lock broke"
    assert c._scroll_x == 0.0, "the locked gesture scrolled sideways anyway"


def test_there_is_no_jump_when_the_gesture_is_classified():
    """The movement spent inside the dead zone is DISCARDED, not banked. Carry
    it forward and the board lurches by the whole dead-zone distance the
    instant it makes up its mind."""
    c, ih = _list_board()
    slop = ih._GESTURE_SLOP
    _pair(ih, 450, 500)
    _pair(ih, 450, 500 - (slop - 1))         # inside the dead zone
    assert c._scroll_y == 0.0, "acted before classifying"
    _pair(ih, 450, 500 - (slop + 2))         # clears it: 3 px of movement
    assert 1.0 < c._scroll_y < 6.0, (
        f"scrolled {c._scroll_y} px on a 3 px move — the dead zone was banked")


def test_the_gesture_state_is_cleared_when_the_touch_ends():
    """A kind left locked would make the NEXT swipe skip its own
    classification and inherit this one."""
    c, ih = _list_board()
    _pair(ih, 450, 500)
    for k in range(1, 6):
        _pair(ih, 450, 500 - k * 20)
    ih.touch_event(_Touch(QEvent.TouchEnd,
                          [_Point(100, QPointF(450, 400), "Released"),
                           _Point(101, QPointF(450, 400), "Released")]))
    assert ih._gesture_kind == ""
    assert ih._gesture_origin is None
    assert ih._one_finger_scroll is False
    assert ih._gesture_active is False
    assert not ih._active_touches


# ── one finger, off the sheets ──────────────────────────────────────────────


def _off_sheet_point(c, y=300.0):
    """A point in the margin beside the sheets, where nothing can be drawn."""
    r = c.page_rect_on_screen(0)
    p = QPointF(max(2.0, r.left() / 2.0), y)
    assert c.page_at_screen(p) is None, "the test point is on a sheet"
    return p


def _one_finger_swipe(ih, start, n=12, step=-20.0):
    ih.touch_event(_Touch(QEvent.TouchBegin, [_Point(1, start, "Pressed")]))
    for k in range(1, n):
        ih.touch_event(_Touch(QEvent.TouchUpdate,
                              [_Point(1, QPointF(start.x(),
                                                 start.y() + k * step))]))


def test_one_finger_off_the_sheets_scrolls():
    """That space was already dead — a contact in the margin or in a gap is
    parked as 'ignored' so it cannot draw outside a page — so scrolling with
    it takes nothing away from any existing tool."""
    c, ih = _list_board()
    _one_finger_swipe(ih, _off_sheet_point(c))
    assert ih._one_finger_scroll is True
    assert ih._gesture_kind == "vertical"
    assert c._scroll_y > 0, "one finger off the sheets did not scroll"


def test_one_finger_on_a_sheet_still_draws():
    """The rule is off-sheet ONLY. On the paper a single finger is the pen,
    exactly as it has always been."""
    c, ih = _list_board()
    before = _counts(c)
    r = c.page_rect_on_screen(0)
    start = QPointF(r.center().x(), r.center().y())
    ih.touch_event(_Touch(QEvent.TouchBegin, [_Point(1, start, "Pressed")]))
    for k in range(1, 9):
        ih.touch_event(_Touch(QEvent.TouchUpdate,
                              [_Point(1, QPointF(start.x() + k * 8,
                                                 start.y() + k * 4))]))
    ih.touch_event(_Touch(QEvent.TouchEnd,
                          [_Point(1, QPointF(start.x() + 80, start.y() + 40),
                                  "Released")]))
    assert ih._one_finger_scroll is False, "a finger on the paper became a scroll"
    assert _counts(c)[0] == before[0] + 1, "one finger on a sheet stopped drawing"
    assert c._scroll_y == 0.0, "drawing scrolled the board"


def test_a_scroll_that_wanders_onto_a_sheet_does_not_draw():
    """Latched at touch-DOWN. Where the finger landed is what the user chose;
    where it has been dragged to since is not — and a gesture allowed to
    change its mind is a scroll that starts inking the moment it crosses onto
    a page."""
    c, ih = _list_board()
    before = _counts(c)
    start = _off_sheet_point(c)
    r = c.page_rect_on_screen(0)
    ih.touch_event(_Touch(QEvent.TouchBegin, [_Point(1, start, "Pressed")]))
    for k in range(1, 10):                   # drag straight onto the sheet
        x = start.x() + (r.center().x() - start.x()) * k / 9.0
        ih.touch_event(_Touch(QEvent.TouchUpdate,
                              [_Point(1, QPointF(x, start.y() - k * 18))]))
    ih.touch_event(_Touch(QEvent.TouchEnd,
                          [_Point(1, QPointF(r.center().x(), 100.0), "Released")]))
    assert _counts(c) == before, "a scroll that crossed onto a sheet left ink"


def test_a_palm_resting_off_the_sheets_does_not_scroll():
    """Palm rejection still comes first. A hand parked on the board while the
    other one writes must not drag the view around."""
    c, ih = _list_board()
    c._touch_mode = "palm_rejection"
    start = _off_sheet_point(c)
    palm = _Point(1, start, "Pressed", diameter=260.0)
    ih.touch_event(_Touch(QEvent.TouchBegin, [palm]))
    for k in range(1, 10):
        ih.touch_event(_Touch(QEvent.TouchUpdate,
                              [_Point(1, QPointF(start.x(), start.y() - k * 20),
                                      diameter=260.0)]))
    assert ih._one_finger_scroll is False, "a rejected palm became a scroll"
    assert c._scroll_y == 0.0, "a rejected palm scrolled the board"


def test_one_finger_scrolling_obeys_the_same_arbitration():
    """It is the same code path, so it gets the same dead zone and the same
    lock — not a second gesture system with its own rules."""
    c, ih = _list_board()
    start = _off_sheet_point(c)
    ih.touch_event(_Touch(QEvent.TouchBegin, [_Point(1, start, "Pressed")]))
    for k in range(4):                       # jitter only
        ih.touch_event(_Touch(QEvent.TouchUpdate,
                              [_Point(1, QPointF(start.x() + (k % 2) * 3,
                                                 start.y() + (k % 2) * 3))]))
    assert ih._gesture_kind == "pending", "one finger classified on jitter"
    assert c._scroll_y == 0.0
    for k in range(1, 8):                    # now a real sideways drag
        ih.touch_event(_Touch(QEvent.TouchUpdate,
                              [_Point(1, QPointF(start.x() + k * 25, start.y()))]))
    assert ih._gesture_kind == "horizontal", "one finger ignored the dominant axis"
    assert c._scroll_y == 0.0, "a sideways one-finger drag scrolled vertically"


def test_one_finger_can_never_zoom():
    """A single contact has no spread to change, so the pinch measure is zero
    by construction. This is what guarantees a plain one-finger swipe can only
    ever scroll."""
    c, ih = _list_board()
    start = _off_sheet_point(c)
    _one_finger_swipe(ih, start)
    assert ih._gesture_kind != "zoom"
    assert abs(c.list_scale - 0.30) < 1e-9, "one finger zoomed the list"


# ── the scroll bar on the right wall ────────────────────────────────────────

def _scrollable(pages=6):
    c, ih = _board(pages=pages)
    c.set_canvas_style("scroll_list")
    c.set_list_scale(0.30)
    c.show()
    _app.processEvents()
    return c, ih


def test_the_scroll_bar_sits_on_the_right_wall_and_spans_the_window():
    c, _ = _scrollable()
    g = c._vbar.geometry()
    assert g.right() >= c.width() - 1, "the bar is not against the right wall"
    assert g.top() == 0 and g.height() == c.height(), "the bar is not full height"


def test_the_scroll_bar_appears_only_when_there_is_something_to_scroll():
    """A full-height track with a full-height handle tells the user nothing and
    covers a strip of board they could otherwise draw on."""
    c, _ = _scrollable()
    assert c._max_scroll() > 0 and c._vbar.isVisible(), "no bar to scroll with"
    c.set_list_scale(c._LIST_SCALE_MIN)    # the whole board fits
    _app.processEvents()
    assert c._max_scroll() == 0.0
    assert not c._vbar.isVisible(), "the bar stayed up with nothing to scroll"


def test_dragging_the_scroll_bar_scrolls_the_list():
    c, _ = _scrollable()
    c._vbar.setValue(c._vbar.maximum() // 2)
    _app.processEvents()
    assert c._scroll_y > 0, "the bar moved but the list did not"
    assert abs(c._scroll_y - c._vbar.value()) < 2, (
        "the bar and the view disagree about where we are")


def test_scrolling_the_list_moves_the_bar_with_it():
    """The other direction, or the bar drifts away from the view it reports."""
    c, _ = _scrollable()
    c.scroll_by(400.0)
    assert abs(c._vbar.value() - c._scroll_y) < 2, "the bar ignored the scroll"


def test_the_bar_and_the_canvas_do_not_feed_each_other():
    """setValue emits valueChanged, whose slot scrolls the canvas, which syncs
    the bar again. Without the re-entry guard that is a loop, and with a
    rounded offset it is a loop that does not settle."""
    c, _ = _scrollable()
    c.scroll_by(250.0)
    _app.processEvents()
    assert abs(c._scroll_y - 250.0) < 2, (
        f"the offset drifted to {c._scroll_y} — the sync fed back")


# ── momentum ────────────────────────────────────────────────────────────────
#
# The glide is driven by a QTimer in the real app. These tests step it by hand
# instead — same physics, same code, no wall-clock dependence — except for the
# one that drives a genuine TouchEnd, which has to be real because the wiring
# between the gesture ending and the glide starting is precisely what a
# unit test of the velocity maths cannot see.


def _step_glide(c, frames=600):
    """Run the glide forward by hand, one 16 ms frame at a time."""
    n = 0
    while c.is_flinging() and n < frames:
        c._fling_last_t = time.time() - 0.016
        c._fling_step()
        n += 1
    return n


def _coast_distance(c, vy):
    start = c._scroll_y
    c.start_fling(vy=vy)
    _step_glide(c)
    return c._scroll_y - start


def test_a_flick_coasts_on_and_stops_by_itself():
    c, _ = _list_board(pages=25)
    c.start_fling(vy=1.5)
    assert c.is_flinging(), "a firm flick did not start a glide"
    before = c._scroll_y
    frames = _step_glide(c)
    assert c._scroll_y > before, "the glide never moved the board"
    assert not c.is_flinging(), "the glide never stopped"
    assert frames < 400, "the glide took absurdly long to settle"


def test_a_slow_release_does_not_coast():
    """A slow release is somebody putting the board down where they want it.
    Gliding on past takes it away from them again."""
    c, _ = _list_board(pages=25)
    c.start_fling(vy=c._FLING_MIN_V * 0.5)
    assert not c.is_flinging(), "a slow release started a glide"
    assert c._scroll_y == 0.0


def test_a_harder_flick_coasts_further():
    c, _ = _list_board(pages=40)
    gentle = _coast_distance(c, 0.5)
    c.reset_scroll()
    hard = _coast_distance(c, 2.0)
    assert hard > gentle * 2.0, (
        f"a 4x faster flick only coasted {hard:.0f} px against {gentle:.0f}")


def test_the_release_velocity_is_capped():
    """A big digitiser can report nonsense for a single sample, and uncapped
    that throws the board the length of the document."""
    c, _ = _list_board(pages=25)
    c.start_fling(vy=10_000.0)
    assert abs(c._fling_vy) == c._FLING_MAX_V, "the velocity was not capped"


def test_a_new_contact_stops_the_glide_dead():
    """The most important line in the feature. On a phone, putting a finger on
    a moving list stops it — without that the board keeps sliding out from
    under a pen that has already started its stroke."""
    c, ih = _list_board(pages=25)
    c.start_fling(vy=2.0)
    for _ in range(4):
        c._fling_last_t = time.time() - 0.016
        c._fling_step()
    assert c.is_flinging()
    parked = c._scroll_y
    ih.touch_event(_Touch(QEvent.TouchBegin,
                          [_Point(1, QPointF(450.0, 300.0), "Pressed")]))
    assert not c.is_flinging(), "a finger landing did not stop the glide"
    _step_glide(c)
    assert c._scroll_y == parked, "the board kept moving under the contact"


def test_a_pen_press_stops_the_glide():
    """tablet_event synthesises a press and calls straight into mouse_press,
    so this is the path a stylus takes."""
    c, ih = _list_board(pages=25)
    c.start_fling(vy=2.0)
    from PySide6.QtGui import QMouseEvent
    r = c.page_rect_on_screen(0)
    ih.mouse_press(QMouseEvent(QEvent.MouseButtonPress,
                               QPointF(r.center().x(), r.center().y()),
                               Qt.LeftButton, Qt.LeftButton, Qt.NoModifier))
    assert not c.is_flinging(), "a pen coming down did not stop the glide"


def test_a_wheel_notch_stops_the_glide():
    c, _ = _list_board(pages=25)
    c.start_fling(vy=2.0)
    _wheel(c, dy=-120)
    assert not c.is_flinging(), "the wheel did not take the view over"


def test_grabbing_the_scroll_bar_stops_the_glide():
    c, _ = _scrollable(pages=25)
    c.start_fling(vy=2.0)
    c._vbar.setValue(c._vbar.maximum() // 3)
    assert not c.is_flinging(), "the bar did not take the view over"


def test_a_glide_that_reaches_the_end_stops_there():
    """``scroll_by`` answers False once the offset stops changing, which is
    what keeps a fast flick from spending the next second decaying against a
    clamp with nothing on screen changing."""
    c, _ = _list_board(pages=25)
    c.scroll_by(c._max_scroll() - 40.0)
    c.start_fling(vy=4.0)
    frames = _step_glide(c)
    assert c._scroll_y == c._max_scroll(), "the glide did not reach the end"
    assert not c.is_flinging()
    assert frames < 30, f"the glide ground on for {frames} frames against a clamp"


# ── handing the velocity over ───────────────────────────────────────────────


def _seed_history(ih, kind, offsets, dt_ms=50.0, sideways=False):
    """A synthetic movement history, newest sample as of right now."""
    now = time.time() * 1000.0
    n = len(offsets)
    ih._gesture_kind = kind
    ih._scroll_history = [
        (now - (n - 1 - i) * dt_ms,
         0.0 if sideways else v,
         v if sideways else 0.0)
        for i, v in enumerate(offsets)]


def test_the_velocity_comes_from_the_scroll_not_the_finger():
    """Recording the OFFSET rather than the finger is what makes a flick into
    the end of the board harmless: the drag was clamped, so nothing moved, so
    there is no velocity and no glide into a wall."""
    c, ih = _list_board(pages=25)
    _seed_history(ih, "vertical", [0.0, 0.0, 0.0])   # clamped: never moved
    ih._launch_fling()
    assert not c.is_flinging(), "a clamped drag launched a glide"

    _seed_history(ih, "vertical", [0.0, 60.0, 130.0])
    ih._launch_fling()
    assert c.is_flinging(), "a real drag did not launch a glide"
    assert c._fling_vy > 1.0, f"velocity came out at {c._fling_vy}"


def _age_history(ih, ms):
    """Push the whole recorded history back in time — i.e. the hand stopped
    moving ``ms`` ago and only then let go."""
    ih._scroll_history = [(t - ms, y, x) for t, y, x in ih._scroll_history]


def test_holding_still_before_lifting_does_not_fling():
    """The gesture people use to place a board precisely — and the bug this
    was reported as: drag, stop, release, and the board twitches once and
    then keeps going, because the velocity being handed over was the one
    from BEFORE the stop.

    A digitiser stops reporting when nothing moves, so the idle time leaves
    no trace in the history at all. The only way it can count is for the
    measurement window to end at the RELEASE rather than at the last sample.
    """
    c, ih = _list_board(pages=25)
    _seed_history(ih, "vertical", [0.0, 60.0, 130.0])
    _age_history(ih, ih._VELOCITY_WINDOW_MS + 40.0)
    ih._launch_fling()
    assert not c.is_flinging(), "a hand held still still threw the board"
    assert c._scroll_y == 0.0, "the board moved after a cancelled flick"


def test_pausing_briefly_before_release_shrinks_the_momentum():
    """Continuous, not a threshold: the longer the hand rests, the less is
    left. A cliff here is just a different gesture to land on the wrong side
    of."""
    def velocity_after_pause(pause_ms):
        c, ih = _list_board(pages=25)
        _seed_history(ih, "vertical", [0.0, 60.0, 130.0], dt_ms=20.0)
        _age_history(ih, pause_ms)
        ih._launch_fling()
        return abs(c._fling_vy)

    none = velocity_after_pause(0.0)
    brief = velocity_after_pause(40.0)
    longer = velocity_after_pause(80.0)
    assert none > brief > longer >= 0.0, (
        f"pausing did not bleed the momentum off: {none}, {brief}, {longer}")


def test_a_flick_released_while_still_moving_keeps_its_momentum():
    """The other half — the fix must not have made momentum unreachable."""
    c, ih = _list_board(pages=25)
    _seed_history(ih, "vertical", [0.0, 60.0, 130.0], dt_ms=20.0)
    ih._launch_fling()
    assert c.is_flinging(), "releasing mid-flick handed over nothing"


def test_a_sideways_flick_creates_no_vertical_momentum():
    """Axes are measured independently and only the locked one is handed
    over, so a sideways gesture cannot drift the board downwards."""
    c, ih = _list_board(pages=25)
    c.set_list_scale(2.0)
    _seed_history(ih, "horizontal", [0.0, 60.0, 130.0], sideways=True)
    ih._launch_fling()
    assert c.is_flinging()
    assert c._fling_vy == 0.0, f"sideways flick made vy={c._fling_vy}"
    assert abs(c._fling_vx) > 0.0, "the sideways axis got no momentum"


def test_the_velocity_only_looks_at_the_recent_window():
    """Not an average over the whole gesture. A long slow drag that ends in a
    fast flick has to hand over the FLICK, and a long fast drag that ends
    slowly has to hand over the slow part."""
    c, ih = _list_board(pages=25)
    # 400 ms of barely moving, then a burst — only the burst is in range.
    slow = [(i * 2.0) for i in range(20)]
    fast = [slow[-1] + 40.0, slow[-1] + 90.0, slow[-1] + 150.0]
    _seed_history(ih, "vertical", slow + fast, dt_ms=20.0)
    ih._launch_fling()
    assert c.is_flinging(), "the closing burst was averaged away"
    assert abs(c._fling_vy) > 1.0, (
        f"velocity {c._fling_vy} looks like the whole drag, not its end")


def test_momentum_never_leaks_from_one_gesture_into_the_next():
    """A new contact cancels the glide AND drops the recorded history, so the
    next gesture starts its own measurement from scratch."""
    c, ih = _list_board(pages=25)
    _seed_history(ih, "vertical", [0.0, 60.0, 130.0], dt_ms=20.0)
    stale = {y for _t, y, _x in ih._scroll_history}
    ih._launch_fling()
    assert c.is_flinging()
    # ON a sheet, so this is the pen rather than a fresh scroll — the point
    # is what the new contact CLEARS, not what it then goes on to record.
    r = c.page_rect_on_screen(0)
    ih.touch_event(_Touch(QEvent.TouchBegin,
                          [_Point(1, QPointF(r.center().x(), r.center().y()),
                                  "Pressed")]))
    assert not c.is_flinging(), "the old glide survived a new contact"
    assert ih._gesture_kind == "", "the old classification survived"
    carried = {y for _t, y, _x in ih._scroll_history} & stale
    assert not carried, f"movement from the last gesture survived: {carried}"


def test_a_pinch_never_flings():
    """Only a locked pan hands a velocity over. A zoom has no scroll velocity
    to speak of, and coasting after one would be the board inventing a scroll
    the user never asked for."""
    c, ih = _list_board(pages=25)
    _seed_history(ih, "zoom", [0.0, 60.0, 130.0])
    ih._launch_fling()
    assert not c.is_flinging(), "a pinch launched a glide"


def test_only_the_locked_axis_coasts():
    c, ih = _list_board(pages=25)
    c.set_list_scale(2.0)
    assert c._max_scroll_x() > 0
    _seed_history(ih, "horizontal", [0.0, 60.0, 130.0], sideways=True)
    ih._launch_fling()
    assert c.is_flinging()
    y_before = c._scroll_y
    _step_glide(c)
    assert c._scroll_x > 0, "the sideways glide did not move"
    assert c._scroll_y == y_before, "a sideways glide scrolled vertically"


def test_a_real_swipe_hands_its_momentum_over():
    """End to end, through a genuine TouchEnd.

    This is the one that cannot be replaced by a unit test of the velocity
    maths. ``active`` skips Released points and TouchEnd means they are all
    released, so the scroll branch never sees the hand leave the glass — the
    handoff has to live in the tail path, and a test that called
    ``_launch_fling`` directly would pass either way.
    """
    c, ih = _list_board(pages=25)
    y = 600.0
    for k in range(9):
        if k:
            time.sleep(0.010)
            y -= 26.0
        ih.touch_event(_Touch(QEvent.TouchUpdate,
                              [_Point(100, QPointF(530.0, y)),
                               _Point(101, QPointF(370.0, y))]))
    assert ih._gesture_kind == "vertical"
    dragged = c._scroll_y
    assert dragged > 0, "the swipe did not scroll at all"
    ih.touch_event(_Touch(QEvent.TouchEnd,
                          [_Point(100, QPointF(530.0, y), "Released"),
                           _Point(101, QPointF(370.0, y), "Released")]))
    assert c.is_flinging(), "the swipe ended without handing its momentum over"
    _step_glide(c)
    assert c._scroll_y > dragged, "the glide added nothing to the drag"


# ── the page number in the gap ──────────────────────────────────────────────

def test_the_gap_above_a_sheet_names_it():
    """The gap between sheets has to be left anyway, so the label costs no
    room — but only if it is really painted there. Look for ink in the band
    above the sheet, at its left edge, where nothing else draws."""
    c, _ = _board(pages=5)
    c.set_canvas_style("scroll_list")
    c.set_list_scale(0.30)
    c.invalidate_cache()
    img = c.grab().toImage()
    r = c.page_rect_on_screen(1)
    bg = img.pixel(4, 4)                 # backdrop, far from any sheet
    ink = 0
    for y in range(max(0, int(r.top() - c._LIST_GAP)), int(r.top()) - 1):
        for x in range(int(r.left()), min(img.width(), int(r.left()) + 90)):
            if img.pixel(x, y) != bg:
                ink += 1
    assert ink > 0, "the gap above the sheet is empty — no page number drawn"


def test_the_gap_is_tall_enough_for_the_label():
    """A label taller than the gap it prints in would collide with the sheet
    above. The gap is sized FOR the text, so this keeps the two in step if
    either is ever retuned."""
    c, _ = _board()
    assert c._LIST_GAP >= c._LIST_LABEL_PX + 6, (
        "the page label will not fit in the gap between sheets")


def test_trackpad_zoom_is_ignored_in_one_page_style():
    c, _ = _board()
    c.set_canvas_style("one_page")
    c.set_zoom_sensitivity(3.0)
    vs, ls = c.view_scale, c.list_scale
    assert c._handle_native_gesture(_Pinch(0.4)) is False
    _ctrl_wheel(c, 5)
    assert c.view_scale == vs and c.list_scale == ls,         "a trackpad gesture changed the one-page view"


def test_zooming_out_never_parks_the_view_past_the_end():
    c, _ = _board(pages=8)
    c.set_canvas_style("scroll_list")
    c.set_list_scale(1.5)
    c.scroll_by(9000)
    c.set_list_scale(0.10)
    assert c._scroll_y <= c._max_scroll() + 0.01,         f"scroll {c._scroll_y} left past the end ({c._max_scroll()})"


# ── the other touch systems keep working in the list ───────────────────────

def _sheet_pt(c, page, fx, fy):
    r = c.page_rect_on_screen(page)
    return QPointF(r.x() + r.width() * fx, r.y() + r.height() * fy)


def _scene_pt_on(c, page, sx, sy):
    """A screen point for scene coords (sx, sy) on a given sheet."""
    r = c.page_rect_on_screen(page)
    return QPointF(r.x() + sx * c.list_scale, r.y() + sy * c.list_scale)


def test_the_eraser_ring_matches_what_it_actually_erases():
    """``eraser_size`` is in SCENE units, so the ring has to be scaled like
    the ink it removes. Unscaled, the list showed a circle three times the
    size of the area it rubbed out — you aim with the ring, so that is a
    tool that lies about itself."""
    c, _ = _board()
    c.set_tool("eraser"); c.set_eraser_size(60.0)
    c.set_canvas_style("scroll_list"); c.set_list_scale(0.30)
    on_screen = c.ring_radius_px() * 2
    footprint = c.eraser_size * c.list_scale
    assert abs(on_screen - footprint) < 4,         f"ring is {on_screen}px across but erases {footprint}px worth"
    # …and one-page is untouched: scale 1.0, so the old numbers exactly.
    c.set_canvas_style("one_page")
    assert c.ring_radius_px() == int(c.eraser_size / 2)


def test_erasing_in_the_list_cuts_only_the_page_under_the_finger():
    c, ih = _board()
    c.set_canvas_style("scroll_list"); c.set_list_scale(0.30)
    c.set_tool("eraser"); c.set_eraser_mode("pixel"); c.set_eraser_size(60.0)
    before = _counts(c)
    # page 1's stroke lies along scene y=180
    start = _scene_pt_on(c, 1, 100.0, 180.0)
    ih.touch_event(_Touch(QEvent.TouchBegin, [_Point(1, start, "Pressed")]))
    for k in range(1, 12):
        ih.touch_event(_Touch(QEvent.TouchUpdate,
                              [_Point(1, QPointF(start.x() + k * 10, start.y()))]))
    ih.touch_event(_Touch(QEvent.TouchEnd,
                          [_Point(1, QPointF(start.x() + 120, start.y()), "Released")]))
    after = _counts(c)
    assert after[1] != before[1], "the eraser never reached page 1's ink"
    assert [after[i] for i in (0, 2, 3)] == [before[i] for i in (0, 2, 3)],         "the eraser also cut another page"


def test_selection_picks_the_object_on_the_touched_sheet():
    c, ih = _board()
    c.set_canvas_style("scroll_list"); c.set_list_scale(0.30)
    c.set_tool("select")
    tap = _scene_pt_on(c, 2, 200.0, 210.0)     # on page 2's stroke
    ih.touch_event(_Touch(QEvent.TouchBegin, [_Point(1, tap, "Pressed")]))
    ih.touch_event(_Touch(QEvent.TouchEnd, [_Point(1, tap, "Released")]))
    assert c.current_page == 2
    assert len(c.get_selected_ids()) == 1, "tapping a stroke selected nothing"


def test_shape_tools_commit_to_the_touched_sheet():
    c, ih = _board()
    c.set_canvas_style("scroll_list"); c.set_list_scale(0.30)
    c.set_tool("rect")
    before = _counts(c)
    a, b = _sheet_pt(c, 1, 0.2, 0.2), _sheet_pt(c, 1, 0.7, 0.6)
    ih.touch_event(_Touch(QEvent.TouchBegin, [_Point(1, a, "Pressed")]))
    ih.touch_event(_Touch(QEvent.TouchUpdate, [_Point(1, b)]))
    ih.touch_event(_Touch(QEvent.TouchEnd, [_Point(1, b, "Released")]))
    after = _counts(c)
    assert after[1] == before[1] + 1, "the shape did not land on page 1"
    assert [after[i] for i in (0, 2, 3)] == [before[i] for i in (0, 2, 3)]


def test_undo_and_redo_stay_per_page_in_the_list():
    """Each page owns a command stack. The list must reach the same one the
    stroke went into, or undo would pop a different page's history."""
    c, ih = _board()
    c.set_canvas_style("scroll_list"); c.set_list_scale(0.30)
    before = c.scene_at(2).object_count
    _stroke_on(ih, c, _sheet_pt(c, 2, 0.2, 0.3))
    assert c.scene_at(2).object_count == before + 1
    c.undo()
    assert c.scene_at(2).object_count == before, "undo missed the stroke"
    c.redo()
    assert c.scene_at(2).object_count == before + 1, "redo missed the stroke"


def test_the_palm_eraser_works_on_the_touched_sheet():
    import tempfile as _tf
    from pathlib import Path as _P
    from ui.canvas.touch_classifier import TouchClassifier
    from ui.util.input_calibration import InputCalibration
    c, ih = _board()
    c.set_canvas_style("scroll_list"); c.set_list_scale(0.30)
    c._touch_mode = "palm_eraser"
    c.set_eraser_mode("object"); c.set_eraser_size(80.0)
    cal = InputCalibration(_P(_tf.mkdtemp()) / "c.json")
    cal.eraser_profile.calibrated = True; cal.eraser_profile.median_area = 900.0
    cal.palm_profile.calibrated = True;   cal.palm_profile.median_area = 4000.0
    c._touch_classifier = TouchClassifier(cal)
    before = _counts(c)
    start = _scene_pt_on(c, 1, 200.0, 180.0)
    BIG = 75.0
    ih.touch_event(_Touch(QEvent.TouchBegin, [_Point(1, start, "Pressed", BIG)]))
    for k in range(1, 8):
        ih.touch_event(_Touch(QEvent.TouchUpdate,
                              [_Point(1, QPointF(start.x() + k * 8, start.y()),
                                      "Updated", BIG)]))
    ih.touch_event(_Touch(QEvent.TouchEnd,
                          [_Point(1, QPointF(start.x() + 64, start.y()),
                                  "Released", BIG)]))
    after = _counts(c)
    assert after[1] < before[1], "the palm erased nothing on page 1"
    assert [after[i] for i in (0, 2, 3)] == [before[i] for i in (0, 2, 3)],         "the palm reached another page"


# ── the bug hunt: everything the list style broke on its own ────────────────

def test_a_press_off_the_sheets_never_draws():
    """The gaps between pages and the margins beside them belong to no page.
    A press there used to be mapped against whichever page was current and
    drawn anyway, landing OUTSIDE that page's bounds: invisible (the sheet
    clips it) but a real object in the scene, in the undo stack, and in every
    save from then on."""
    c, ih = _board()
    c.set_canvas_style("scroll_list"); c.set_list_scale(0.30)
    c.goto_page(0)
    before = _counts(c)
    r0 = c.page_rect_on_screen(0)
    for label, pt in (("gap below page 0",
                       QPointF(r0.center().x(), r0.bottom() + 8)),
                      ("left margin",
                       QPointF(12.0, r0.center().y())),
                      ("right margin",
                       QPointF(c.width() - 12.0, r0.center().y()))):
        assert c.page_at_screen(pt) is None, f"{label} unexpectedly hit a page"
        _stroke_on(ih, c, pt)
        assert _counts(c) == before, f"a press in the {label} drew ink"


def test_a_stroke_started_on_a_sheet_may_run_off_its_edge():
    """The guard is on the PRESS only. Drawing off the edge of a page is
    normal, and the stroke it belongs to has already been decided."""
    c, ih = _board()
    c.set_canvas_style("scroll_list"); c.set_list_scale(0.30)
    r = c.page_rect_on_screen(1)
    before = _counts(c)
    start = QPointF(r.center().x(), r.bottom() - 10)
    ih.touch_event(_Touch(QEvent.TouchBegin, [_Point(1, start, "Pressed")]))
    for k in range(1, 8):
        ih.touch_event(_Touch(QEvent.TouchUpdate,
                              [_Point(1, QPointF(start.x(), start.y() + k * 6))]))
    ih.touch_event(_Touch(QEvent.TouchEnd,
                          [_Point(1, QPointF(start.x(), start.y() + 48), "Released")]))
    assert _counts(c)[1] == before[1] + 1,         "a stroke that ran off the page edge was thrown away"


def test_the_off_sheet_guard_does_not_touch_one_page_style():
    """One-page style has no such thing as 'off the page'."""
    c, ih = _board()
    c.set_canvas_style("one_page")
    before = _counts(c)
    for pt in (QPointF(5.0, 5.0), QPointF(880.0, 690.0), QPointF(450.0, 350.0)):
        assert c.accepts_input_at(pt) is True
        _stroke_on(ih, c, pt)
    assert _counts(c)[0] == before[0] + 3, "the guard leaked into one-page style"


def test_a_stylus_press_focuses_the_sheet_it_landed_on():
    """tablet_event synthesises a press and calls the handler DIRECTLY, so a
    guard in the handler without the focus beside it left the stylus drawing
    on whichever sheet the last contact happened to touch."""
    from PySide6.QtGui import QMouseEvent
    c, ih = _board()
    c.set_canvas_style("scroll_list"); c.set_list_scale(0.30)
    c.goto_page(0)
    r = c.page_rect_on_screen(2)
    before = _counts(c)

    def press(kind, x, y):
        return QMouseEvent(kind, QPointF(x, y), Qt.LeftButton, Qt.LeftButton,
                           Qt.NoModifier)

    ih.mouse_press(press(QEvent.MouseButtonPress, r.center().x(), r.center().y()))
    ih.mouse_move(press(QEvent.MouseMove, r.center().x() + 30, r.center().y()))
    ih.mouse_release(press(QEvent.MouseButtonRelease,
                           r.center().x() + 30, r.center().y()))
    after = _counts(c)
    assert c.current_page == 2, "the press did not focus the sheet it hit"
    assert after[2] == before[2] + 1, "the stroke went to the wrong page"


def test_adding_a_page_repaints_the_list():
    """The list paints every page, so a new one has to invalidate the cache
    or the sheet never appears. One-page mode never needed it: the new page
    isn't on screen until you navigate to it."""
    c, _ = _board()
    c.set_canvas_style("scroll_list")
    c.grab()                                   # settle a clean cache
    c._cache_dirty = False
    c.add_page()
    assert c._cache_dirty, "the new sheet would not have been painted"


def test_touching_a_different_sheet_repaints_the_highlight():
    """The current sheet carries an accent border — the only clue about where
    ink will go. Without invalidating, it stayed on the page you left."""
    c, _ = _board()
    c.set_canvas_style("scroll_list"); c.set_list_scale(0.30)
    c.grab(); c._cache_dirty = False
    r = c.page_rect_on_screen(2)
    c.focus_page_at(QPointF(r.center()))
    assert c.current_page == 2
    assert c._cache_dirty, "the current-sheet highlight would not have moved"
    # …and re-touching the SAME sheet must not force a needless repaint.
    c.grab(); c._cache_dirty = False
    c.focus_page_at(QPointF(r.center().x() + 5, r.center().y() + 5))
    assert not c._cache_dirty, "same-sheet press caused a wasteful rebuild"


def test_the_view_is_never_left_past_the_end():
    """Three separate bugs were this one missing call: delete pages, resize
    the window, or zoom out while scrolled to the bottom and the offset
    pointed past the content — a blank board."""
    c, _ = _board(pages=8)
    c.set_canvas_style("scroll_list"); c.set_list_scale(0.30)

    c.scroll_by(99999)
    for _ in range(6):
        c.delete_page(c.page_count - 1)
    assert c._scroll_y <= c._max_scroll() + 0.01, "deleting pages left it past the end"

    c2, _ = _board(pages=8)
    c2.set_canvas_style("scroll_list"); c2.set_list_scale(0.30)
    c2.scroll_by(99999)
    c2.resize(900, 2400)
    c2.grab()          # a paint is the moment the geometry is certainly live
    assert c2._scroll_y <= c2._max_scroll() + 0.01, "resizing left it past the end"


def test_the_list_cache_is_never_patched_in_place():
    """``repair_cache_boxes`` patches a ONE-PAGE composition with the
    one-page transform. The list cache is many sheets at their own origins,
    so patching it wrote repaired ink at the wrong place on screen. It must
    decline and let the caller do the full rebuild the contract allows for."""
    c, _ = _board()
    c.set_canvas_style("scroll_list")
    c.grab()
    assert c.repair_cache_boxes([(0.0, 0.0, 50.0, 50.0)]) is False,         "the list cache accepted an in-place patch"


# ── each sheet is its own canvas ────────────────────────────────────────────

def test_ink_never_escapes_its_sheet():
    """Every page clips its own content. The committed renderer always did;
    the LIVE overlays did not, so while a gesture was in flight the ink ran
    out across the gaps and the margins as though it were drawn on the
    window rather than on a page."""
    c, ih = _board()
    c.set_canvas_style("scroll_list"); c.set_list_scale(0.30)
    r = c.page_rect_on_screen(1)
    bounds = r.adjusted(-2, -2, 2, 2)
    start = QPointF(r.right() - 15, r.center().y())
    ih.touch_event(_Touch(QEvent.TouchBegin, [_Point(1, start, "Pressed")]))
    for k in range(1, 20):        # drags far off the right/bottom edges
        ih.touch_event(_Touch(QEvent.TouchUpdate,
                              [_Point(1, QPointF(start.x() + k * 14,
                                                 start.y() + k * 9))]))
    _app.processEvents()
    live = _red_ink_bbox(c)
    assert live is not None, "no live ink at all"
    assert bounds.contains(live),         f"live ink {live} escaped its sheet {r}"
    ih.touch_event(_Touch(QEvent.TouchEnd,
                          [_Point(1, QPointF(start.x() + 280, start.y() + 180),
                                  "Released")]))
    _app.processEvents()
    committed = _red_ink_bbox(c)
    assert committed is not None and bounds.contains(committed),         f"committed ink {committed} escaped its sheet {r}"


# ── navigation means something in a list ────────────────────────────────────

def test_the_current_page_follows_the_middle_of_the_window():
    """In a list every page exists at once, so 'which page am I on' is a
    question about the VIEW. The navigator has to name whatever fills the
    middle of the window, not whichever sheet was last drawn on."""
    c, _ = _board(pages=6)
    c.set_canvas_style("scroll_list"); c.set_list_scale(0.30)
    seen = []
    c.page_changed.connect(lambda i, n: seen.append(i))
    for _ in range(8):
        c.scroll_by(120)
        assert c.current_page == c.page_at_viewport_center(),             (f"indicator says {c.current_page} but page "
             f"{c.page_at_viewport_center()} is in the middle")
    assert seen, "the page indicator never updated while scrolling"
    assert seen == sorted(seen), f"the indicator jumped around: {seen}"


def test_going_to_a_page_puts_it_in_the_middle():
    """'Next page' has to actually take you there — and in a list, there
    means the middle of the screen, not parked against the top edge where it
    competes with its neighbour for the indicator."""
    c, _ = _board(pages=6)
    c.set_canvas_style("scroll_list"); c.set_list_scale(0.30)
    for target in (3, 4, 2):
        c.goto_page(target)
        r = c.page_rect_on_screen(target)
        off = abs(r.center().y() - c.height() / 2.0)
        assert off < 2.0, f"page {target} centre is {off:.0f}px off the middle"
        assert c.current_page == target


def test_next_and_previous_walk_the_list():
    c, _ = _board(pages=6)
    c.set_canvas_style("scroll_list"); c.set_list_scale(0.30)
    c.goto_page(0)
    c.next_page(); assert c.current_page == 1
    c.next_page(); assert c.current_page == 2
    assert c.page_at_viewport_center() == 2, "next didn't land on the page"
    c.prev_page(); assert c.current_page == 1
    assert c.page_at_viewport_center() == 1, "prev didn't land on the page"


def test_next_past_the_end_adds_a_page_and_shows_it():
    """next_page calls _add_page_internal directly when it runs off the end,
    so the repaint has to live there — added that way the sheet never
    appeared in the list."""
    c, _ = _board(pages=2)
    c.set_canvas_style("scroll_list")
    c.goto_page(1); c.grab(); c._cache_dirty = False
    c.next_page()
    assert c.page_count == 3, "next at the end did not add a page"
    assert c._cache_dirty, "the added sheet would not have been painted"
    assert c.current_page == 2


def test_zooming_and_deleting_keep_the_indicator_honest():
    """Both change WHICH page sits in the middle. Without re-syncing, the
    navigator kept naming the page from before the change."""
    c, _ = _board(pages=6)
    c.set_canvas_style("scroll_list"); c.set_list_scale(0.30)
    c.scroll_by(500)
    c.set_list_scale(0.60)
    assert c.current_page == c.page_at_viewport_center(),         "the indicator went stale after a zoom"
    c2, _ = _board(pages=5)
    c2.set_canvas_style("scroll_list"); c2.set_list_scale(0.30)
    c2.goto_page(3)
    c2.delete_page(3)
    assert c2.current_page == c2.page_at_viewport_center(),         "the indicator went stale after deleting a page"
    assert c2.current_page < c2.page_count


def test_one_page_navigation_is_unchanged():
    """goto_page still resets the scroll and shows one page at a time."""
    c, _ = _board(pages=4)
    assert c.canvas_style == "one_page"
    c.scroll_by(200)
    c.goto_page(2)
    assert c.current_page == 2
    assert c._scroll_y == 0.0, "one-page goto stopped resetting the scroll"
    c.next_page(); assert c.current_page == 3
    c.prev_page(); assert c.current_page == 2


# ── media: backgrounds, images, PDF/PPT pages ──────────────────────────────

def _media_board(pages=4):
    """A board whose pages carry real background images."""
    import tempfile as _tf
    from PySide6.QtGui import QPixmap
    tmp = Path(_tf.mkdtemp(prefix="lb_media_"))
    wide = QPixmap(1600, 900);  wide.fill(QColor(200, 40, 40))
    tall = QPixmap(800, 2400);  tall.fill(QColor(40, 160, 60))
    wide.save(str(tmp / "wide.png")); tall.save(str(tmp / "tall.png"))
    c, ih = _board(pages)
    c._board_dir = str(tmp)
    return c, ih, tmp


def test_each_sheet_paints_its_own_background():
    c, _ih, _tmp = _media_board()
    c.scene_at(0).bg_image = "wide.png"
    c.scene_at(1).bg_image = "tall.png"
    c.set_canvas_style("scroll_list"); c.set_list_scale(0.30)
    c.invalidate_cache()
    img = c.grab().toImage()
    for i, want in ((0, (200, 40, 40)), (1, (40, 160, 60))):
        r = c.page_rect_on_screen(i)
        assert r is not None and r.top() < c.height()
        px = img.pixel(int(r.center().x()), int(r.center().y()))
        got = ((px >> 16) & 255, (px >> 8) & 255, px & 255)
        assert got == want, f"page {i} centre is rgb{got}, expected rgb{want}"
    # …and none of it leaks into the gap between sheets.
    r0 = c.page_rect_on_screen(0)
    px = img.pixel(int(r0.center().x()), int(r0.bottom() + 8))
    got = ((px >> 16) & 255, (px >> 8) & 255, px & 255)
    assert got not in ((200, 40, 40), (40, 160, 60)), \
        "a page background bled into the gap between sheets"


def test_a_tall_background_gets_a_tall_sheet():
    """A scan or a tall PDF page is imported in "natural" fit, which keeps
    the aspect by running BELOW the canvas. One-page style lets you scroll
    down inside the page to reach it; a list has no inside to scroll, so
    sizing the sheet to the canvas alone cut a 2400-tall background off at
    800 with no way to see the rest."""
    c, _ih, _tmp = _media_board(pages=3)
    c.set_canvas_style("scroll_list"); c.set_list_scale(0.30)
    plain_h = c.page_layout()[0][1].height()
    c.scene_at(1).bg_image = "tall.png"
    c.invalidate_cache()
    tall_h = c.page_layout()[1][1].height()
    assert tall_h > plain_h * 2, \
        f"the tall page's sheet is {tall_h}, barely more than {plain_h}"
    _pm, dest = c._bg_image_layout(c.scene_at(1))
    assert abs(tall_h - dest.bottom() * c.list_scale) < 2, \
        "the sheet doesn't cover the whole background"


def test_a_wide_background_is_cropped_not_widened():
    """"Cover" fit overflows sideways ON PURPOSE and is meant to be centre
    cropped. Widening the sheet to swallow that turned a 16:9 background's
    page into a wide slab."""
    c, _ih, _tmp = _media_board(pages=2)
    c.set_canvas_style("scroll_list"); c.set_list_scale(0.30)
    plain_w = c.page_layout()[0][1].width()
    c.scene_at(0).bg_image = "wide.png"
    c.invalidate_cache()
    assert c.page_layout()[0][1].width() == plain_w, \
        "a cover-fit background widened its page instead of being cropped"


def test_changing_the_fit_mode_resizes_the_sheet():
    c, _ih, _tmp = _media_board(pages=3)
    c.set_canvas_style("scroll_list"); c.set_list_scale(0.30)
    c.scene_at(1).bg_image = "tall.png"
    c.invalidate_cache()
    tall = c.page_layout()[1][1].height()
    c.set_bg_fit_mode("fit", "tall.png")      # contain — nothing overflows
    contained = c.page_layout()[1][1].height()
    assert contained < tall, "the fit change never reached the layout"
    assert abs(contained - c.page_layout()[0][1].height()) < 1


def test_an_image_object_renders_and_erases_on_its_own_page():
    c, ih, tmp = _media_board(pages=4)
    from PySide6.QtGui import QPixmap
    from PySide6.QtCore import QRectF as _R
    pic = QPixmap(400, 300); pic.fill(QColor(255, 140, 0))
    pic.save(str(tmp / "pic.png"))
    V = lbe.Vec2f
    c.scene_at(2).add_object(lbe.ImageObject("pic.png", V(120.0, 120.0),
                                             V(360.0, 270.0)))
    c.set_canvas_style("scroll_list"); c.set_list_scale(0.30)
    c.invalidate_cache(); c.grab()

    img = c.grab().toImage()
    xs, ys = [], []
    for y in range(img.height()):
        for x in range(img.width()):
            px = img.pixel(x, y)
            r, g, b = (px >> 16) & 255, (px >> 8) & 255, px & 255
            if r > 200 and 100 < g < 190 and b < 70:
                xs.append(x); ys.append(y)
    assert xs, "the image object never rendered in the list"
    bbox = _R(min(xs), min(ys), max(xs) - min(xs), max(ys) - min(ys))
    sheet = c.page_rect_on_screen(2)
    assert sheet.adjusted(-2, -2, 2, 2).contains(bbox), \
        f"image {bbox} escaped its sheet {sheet}"

    c.set_tool("eraser"); c.set_eraser_mode("object"); c.set_eraser_size(120.0)
    c.set_object_eraser_affects_media(True)
    before = _counts(c)
    r = c.page_rect_on_screen(2)
    _stroke_on(ih, c, QPointF(r.x() + 250 * 0.30, r.y() + 250 * 0.30))
    after = _counts(c)
    assert after[2] < before[2], "the object eraser never reached the image"
    assert [after[i] for i in (0, 1, 3)] == [before[i] for i in (0, 1, 3)], \
        "the eraser reached another page"


def test_pages_of_different_sizes_lay_out_and_hit_test():
    """A PDF import can mix portrait and landscape pages in one board."""
    c, _ = _board(pages=3)
    c.scene_at(1).set_canvas_size(1200, 400)
    c.set_canvas_style("scroll_list"); c.set_list_scale(0.30)
    c.invalidate_cache()
    layout = c.page_layout()
    assert layout[1][1].width() > layout[0][1].width(), \
        "the landscape page did not get its own width"
    for i, r in layout:
        assert abs((r.x() + r.width() / 2) - c.width() / 2) < 2, \
            f"page {i} is not centred"
    for k in range(1, len(layout)):
        assert layout[k][1].top() > layout[k - 1][1].bottom(), "pages overlap"
    for i, _r in layout:
        scr = c.page_rect_on_screen(i)
        if scr.top() > c.height() or scr.bottom() < 0:
            continue
        assert c.page_at_screen(scr.center()) == i, \
            f"hit test missed page {i} in a mixed-size list"


def test_the_layout_is_memoised_but_never_stale():
    """page_layout is asked for several times per FRAME and now measures
    every page's background. Memoised — but every path that can change a
    sheet's size has to drop the memo."""
    c, _ih, _tmp = _media_board(pages=3)
    c.set_canvas_style("scroll_list"); c.set_list_scale(0.30)
    first = c.page_layout()
    assert c.page_layout() is first, "the layout is not being memoised at all"

    c.scene_at(1).bg_image = "tall.png"
    c.invalidate_cache()
    assert c.page_layout()[1][1].height() > first[1][1].height(), \
        "a background change left a stale layout"
    h = c.page_layout()[1][1].height()

    c.set_bg_fit_mode("fit", "tall.png")
    assert c.page_layout()[1][1].height() < h, "a fit change left a stale layout"
    n = len(c.page_layout())
    c.add_page()
    assert len(c.page_layout()) == n + 1, "add left a stale layout"
    c.delete_page(0)
    assert len(c.page_layout()) == n, "delete left a stale layout"
    w = c.page_layout()[0][1].width()
    c.set_list_scale(0.6)
    assert c.page_layout()[0][1].width() > w, "zoom left a stale layout"
    x = c.page_layout()[0][1].x()
    c.resize(1400, 700)
    assert c.page_layout()[0][1].x() != x, "resize left a stale layout"


# ── persistence ─────────────────────────────────────────────────────────────

def test_the_style_and_sensitivity_persist():
    from ui.util.input_calibration import (InputCalibration,
                                           CANVAS_STYLE_SCROLL_LIST)
    path = Path(tempfile.mkdtemp(prefix="lb_style_")) / "settings.json"
    cal = InputCalibration(path); cal.load()
    assert cal.canvas_style == "one_page", "one page must remain the default"
    assert cal.zoom_sensitivity == 1.0
    cal.set_canvas_style(CANVAS_STYLE_SCROLL_LIST)
    cal.set_zoom_sensitivity(2.25)

    fresh = InputCalibration(path); fresh.load()
    assert fresh.canvas_style == CANVAS_STYLE_SCROLL_LIST, \
        "the canvas style did not survive a restart"
    assert abs(fresh.zoom_sensitivity - 2.25) < 1e-6


def test_sensitivity_is_clamped_to_a_usable_band():
    """It multiplies a per-sample scale change, so an out-of-range value
    doesn't zoom 'fast' — one twitch jumps the whole range."""
    from ui.util.input_calibration import InputCalibration
    path = Path(tempfile.mkdtemp(prefix="lb_style2_")) / "settings.json"
    cal = InputCalibration(path); cal.load()
    cal.set_zoom_sensitivity(999)
    assert cal.zoom_sensitivity <= 3.0
    cal.set_zoom_sensitivity(-5)
    assert cal.zoom_sensitivity >= 0.25


def test_an_unknown_style_falls_back_to_one_page():
    from ui.util.input_calibration import InputCalibration
    path = Path(tempfile.mkdtemp(prefix="lb_style3_")) / "settings.json"
    cal = InputCalibration(path); cal.load()
    cal.set_canvas_style("something_else")
    assert cal.canvas_style == "one_page"
    c, _ = _board()
    c.set_canvas_style("nonsense")
    assert c.canvas_style == "one_page", "the canvas entered an unrenderable mode"


# ── the settings UI ─────────────────────────────────────────────────────────

def test_the_settings_panel_offers_both_styles_and_the_slider():
    from ui.panels.settings_panel import SettingsPanel
    seen = {}
    p = SettingsPanel(None, canvas_style="one_page",
                      on_canvas_style=lambda s: seen.update(style=s),
                      zoom_sensitivity=1.0,
                      on_zoom_sensitivity=lambda v: seen.update(zoom=v))
    p.show(); _app.processEvents()
    assert set(p._style_btns) == {"one_page", "scroll_list"}
    # Zoom Sensitivity is dead under One Page, so it must not look live.
    assert not p._zoom_slider.isEnabled()
    p._style_btns["scroll_list"].click(); _app.processEvents()
    assert seen.get("style") == "scroll_list"
    assert p._zoom_slider.isEnabled(), "the slider stayed disabled in list style"
    p._zoom_slider.setValue(220); _app.processEvents()
    assert abs(seen.get("zoom", 0) - 2.20) < 1e-6


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
    # os._exit, not sys.exit: a normal interpreter shutdown tears the Qt
    # objects down in no particular order and segfaulted now and then AFTER
    # every test had passed, failing the run for nothing.
    sys.stdout.flush()
    os._exit(1 if failures else 0)
