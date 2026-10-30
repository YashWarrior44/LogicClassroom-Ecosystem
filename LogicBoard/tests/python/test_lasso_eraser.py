"""
test_lasso_eraser.py — the Lasso eraser: draw a loop, what's inside goes.

    python tests/python/test_lasso_eraser.py

Driven through the real canvas, input handler and engine — mouse/stylus and
finger paths both — plus the panel that switches it on.

What "inside" means is pinned here:
  * a stroke is inside when at least half its points are: a long line that
    merely passes through the loop survives; ink circled loosely does not;
  * any other object is inside when the centre of its box is — the rule the
    selection lasso uses, so the two tools agree;
  * locked objects stay, and so does imported media unless the "object
    eraser affects media" setting says otherwise;
  * one loop is ONE undo step;
  * a palm never draws a loop — it erases by touch, as the palm eraser does;
  * a two-finger gesture taking over cancels an unfinished loop rather than
    erasing whatever it happened to cover.
"""
import os
import sys

os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")
sys.path.insert(0, os.path.abspath(os.path.join(os.path.dirname(__file__),
                                                "..", "..")))

from PySide6.QtCore import QEvent, QPointF, Qt
from PySide6.QtGui import QMouseEvent
from PySide6.QtWidgets import QApplication

_app = QApplication.instance() or QApplication([])

import logicboard_engine as lbe
from ui.canvas.canvas_widget import CanvasWidget
from ui.canvas.input_handler import InputHandler


def _board():
    c = CanvasWidget()
    c.resize(1200, 800)
    ih = InputHandler(c)
    c._input_handler = ih
    c.set_tool("eraser")
    c.set_eraser_mode("lasso")
    return c, ih, c.current_scene()


def _pen(scene, pts, width=4.0):
    o = lbe.FreehandStroke()
    o.set_points([(float(x), float(y)) for x, y in pts])
    o.stroke_width = width
    o.stroke_color = lbe.RGBA(20, 20, 20, 255)
    scene.add_object(o)
    return o


def _rect(scene, x, y, w, h):
    o = lbe.RectObject(lbe.Vec2f(x, y), lbe.Vec2f(w, h))
    o.stroke_color = lbe.RGBA(0, 0, 255, 255)
    o.stroke_width = 3.0
    scene.add_object(o)
    return o


def _loop(x0, y0, x1, y1, n=40):
    """A closed loop (an ellipse) around the box, in SCENE coordinates."""
    import math
    cx, cy, rx, ry = (x0 + x1) / 2, (y0 + y1) / 2, (x1 - x0) / 2, (y1 - y0) / 2
    return [QPointF(cx + rx * math.cos(2 * math.pi * i / n),
                    cy + ry * math.sin(2 * math.pi * i / n)) for i in range(n)]


def _ids(scene):
    return {str(scene.object_at(i).id) for i in range(scene.object_count)}


# ── what is inside ─────────────────────────────────────────────────────────

def test_a_circled_stroke_goes_and_its_neighbour_stays():
    c, ih, s = _board()
    a = _pen(s, [(100 + i * 5, 200) for i in range(20)])
    b = _pen(s, [(600 + i * 5, 200) for i in range(20)])
    n = ih._erase_in_lasso(_loop(70, 150, 230, 250))
    assert n == 1, n
    left = _ids(s)
    assert str(a.id) not in left and str(b.id) in left


def test_a_line_that_only_passes_through_the_loop_survives():
    c, ih, s = _board()
    long_line = _pen(s, [(50 + i * 10, 300) for i in range(80)])   # 50..840
    ih._erase_in_lasso(_loop(380, 250, 480, 350))
    assert str(long_line.id) in _ids(s), \
        "a long line was erased because a small loop crossed it"


def test_loosely_circled_ink_still_goes():
    """The loop clips a little of each end — majority rule, not 'all'."""
    c, ih, s = _board()
    word = _pen(s, [(100 + i * 5, 400) for i in range(40)])          # 100..295
    ih._erase_in_lasso(_loop(110, 360, 285, 440))
    assert str(word.id) not in _ids(s), "loosely circled ink survived"


def test_shapes_follow_the_selection_rule():
    c, ih, s = _board()
    inside = _rect(s, 120, 120, 60, 40)
    outside = _rect(s, 700, 500, 60, 40)
    ih._erase_in_lasso(_loop(80, 80, 260, 220))
    left = _ids(s)
    assert str(inside.id) not in left and str(outside.id) in left


def test_locked_objects_are_left_alone():
    c, ih, s = _board()
    keep = _pen(s, [(100 + i * 5, 200) for i in range(20)])
    # The scene holds its own copy: lock THAT one.
    for i in range(s.object_count):
        if str(s.object_at(i).id) == str(keep.id):
            s.object_at(i).locked = True
    assert any(s.object_at(i).locked for i in range(s.object_count))
    ih._erase_in_lasso(_loop(70, 150, 230, 250))
    assert str(keep.id) in _ids(s)


def test_a_tiny_scribble_erases_nothing():
    c, ih, s = _board()
    _pen(s, [(100 + i * 5, 200) for i in range(20)])
    assert ih._erase_in_lasso(_loop(150, 198, 153, 201)) == 0
    assert s.object_count == 1


def test_one_loop_is_one_undo_step():
    c, ih, s = _board()
    for k in range(4):
        _pen(s, [(100 + i * 5, 150 + k * 30) for i in range(20)])
    ih._erase_in_lasso(_loop(60, 110, 260, 280))
    assert s.object_count == 0
    c.current_stack().undo(s)
    assert s.object_count == 4, \
        f"one undo should bring back all four, got {s.object_count}"


# ── the real input paths ───────────────────────────────────────────────────

def _mouse(ih, c, kind, scene_pt):
    sp = c.scene_to_screen(scene_pt)
    ev = QMouseEvent(kind, QPointF(sp), QPointF(sp), Qt.LeftButton,
                     Qt.LeftButton, Qt.NoModifier)
    {QEvent.MouseButtonPress: ih.mouse_press,
     QEvent.MouseMove: ih.mouse_move,
     QEvent.MouseButtonRelease: ih.mouse_release}[kind](ev)


def test_drawing_a_loop_with_the_mouse_or_stylus_erases():
    c, ih, s = _board()
    target = _pen(s, [(300 + i * 5, 300) for i in range(20)])
    other = _pen(s, [(800 + i * 5, 600) for i in range(20)])
    pts = _loop(260, 250, 440, 350)
    _mouse(ih, c, QEvent.MouseButtonPress, pts[0])
    assert c._lasso_active and c._lasso_erase, "no red erase-lasso while drawing"
    for p in pts[1:]:
        _mouse(ih, c, QEvent.MouseMove, p)
    _mouse(ih, c, QEvent.MouseButtonRelease, pts[0])
    left = _ids(s)
    assert str(target.id) not in left and str(other.id) in left
    assert not c._lasso_active, "the loop stayed on screen after release"


class _P:
    def __init__(self, pid, pos, state, d):
        self._id, self._pos, self._state, self._d = pid, pos, state, d

    def id(self): return self._id
    def position(self): return self._pos
    def state(self):
        from types import SimpleNamespace
        return SimpleNamespace(name=self._state)
    def ellipseDiameters(self):
        from PySide6.QtCore import QSizeF
        return QSizeF(self._d, self._d)


class _T:
    def __init__(self, etype, pts): self._t, self._p = etype, pts
    def type(self): return self._t
    def points(self): return self._p
    def accept(self): pass


def _finger_loop(ih, c, scene_pts, contact=18.0, end=True):
    sp = [QPointF(c.scene_to_screen(p)) for p in scene_pts]
    ih.touch_event(_T(QEvent.TouchBegin, [_P(1, sp[0], "Pressed", contact)]))
    for q in sp[1:]:
        ih.touch_event(_T(QEvent.TouchUpdate, [_P(1, q, "Updated", contact)]))
    if end:
        ih.touch_event(_T(QEvent.TouchEnd, [_P(1, sp[0], "Released", contact)]))


def test_drawing_a_loop_with_a_finger_erases():
    c, ih, s = _board()
    c._touch_mode = "palm_rejection"
    target = _pen(s, [(300 + i * 5, 300) for i in range(20)])
    other = _pen(s, [(800 + i * 5, 600) for i in range(20)])
    _finger_loop(ih, c, _loop(260, 250, 440, 350))
    left = _ids(s)
    assert str(target.id) not in left and str(other.id) in left


def test_a_palm_still_erases_by_touch_in_lasso_mode():
    """A palm cannot draw a loop. Leaving it to do nothing would be a palm
    eraser that silently stopped working whenever Lasso was picked."""
    c, ih, s = _board()
    c._touch_mode = "palm_eraser"
    c._palm_eraser_enabled = True
    c._palm_min_size = 40
    c.set_eraser_size(46.0)
    under = _pen(s, [(300 + i * 5, 300) for i in range(20)])
    sp = QPointF(c.scene_to_screen(QPointF(340, 300)))
    ih.touch_event(_T(QEvent.TouchBegin, [_P(1, sp, "Pressed", 90.0)]))
    ih.touch_event(_T(QEvent.TouchEnd, [_P(1, sp, "Released", 90.0)]))
    assert str(under.id) not in _ids(s), "the palm erased nothing in lasso mode"
    assert not c._lasso_active, "a palm started a lasso"


def test_a_gesture_cancels_an_unfinished_loop():
    c, ih, s = _board()
    c._touch_mode = "palm_rejection"
    target = _pen(s, [(300 + i * 5, 300) for i in range(20)])
    _finger_loop(ih, c, _loop(260, 250, 440, 350), end=False)
    ih._erase_touch_end(cancel_lasso=True)
    assert str(target.id) in _ids(s), \
        "an unfinished loop erased when a gesture took over"
    assert not c._lasso_active


def test_lasso_mode_has_no_ring_and_a_crosshair():
    c, ih, s = _board()
    assert c._show_eraser_cursor is False, "the eraser ring shows in lasso mode"
    assert c.cursor().shape() == Qt.CrossCursor
    c.set_eraser_mode("pixel")
    assert c._show_eraser_cursor is True
    assert c.cursor().shape() == Qt.BlankCursor


# ── the panel ──────────────────────────────────────────────────────────────

def test_the_panel_offers_lasso_and_reports_it():
    from ui.panels.eraser_panel import EraserPanel
    picked = []
    p = EraserPanel(None, eraser_size=40, eraser_mode="pixel",
                    on_mode_change=picked.append)
    p.show(); _app.processEvents()
    p._lasso_btn.click()
    assert picked == ["lasso"]
    assert p._size_slot.currentIndex() == 1, "the size slider shows for lasso"


def test_the_panel_is_one_size_and_as_wide_as_the_pen_panel():
    from ui.panels.eraser_panel import EraserPanel
    from ui.panels.pen_panel import PenPanel
    sizes = set()
    for m in ("pixel", "object", "lasso"):
        p = EraserPanel(None, eraser_mode=m)
        p.show(); _app.processEvents()
        sizes.add((p.width(), p.height()))
        p.close()
    assert len(sizes) == 1, sizes
    pen = PenPanel(None)
    assert next(iter(sizes))[0] == pen.width(), \
        "the eraser panel is not the pen panel's width"


def test_the_modes_are_a_left_column_level_with_the_right():
    from ui.panels.eraser_panel import EraserPanel
    p = EraserPanel(None, eraser_mode="pixel")
    p.show(); _app.processEvents()

    def span(w):
        tl = w.mapTo(p, w.rect().topLeft())
        return tl.x(), tl.y(), tl.x() + w.width(), tl.y() + w.height()
    tiles = [span(b) for b in (p._pixel_btn, p._object_btn, p._lasso_btn)]
    assert len({(t[0], t[2]) for t in tiles}) == 1, "the modes are not a column"
    desc = span(p._mode_desc)
    assert desc[0] > tiles[0][2], "the right column overlaps the modes"
    assert abs(tiles[0][1] - span(p._size_slot)[1]) <= 1, "tops not level"
    assert abs(tiles[-1][3] - desc[3]) <= 1, "bottoms not level"


def test_the_preview_shows_the_biggest_eraser_whole():
    """At 200 px the ring is drawn 200 px across and none of it is cut off —
    it used to stop growing at the strip's height."""
    from ui.panels.eraser_panel import EraserPanel
    p = EraserPanel(None, eraser_size=200, eraser_mode="pixel")
    p.show(); _app.processEvents()
    pm = p._preview.pixmap()
    img, dpr = pm.toImage(), pm.devicePixelRatio()
    w, h = img.width(), img.height()

    def is_ring(c):            # the pale-blue Simple ring, not the ink
        return c.alpha() > 200 and c.red() > 120 and c.blue() > 230
    ring = [(x, y) for y in range(0, h, 2) for x in range(0, w, 2)
            if is_ring(img.pixelColor(x, y))]
    assert ring, "no ring drawn"
    xs = [x for x, _ in ring]
    ys = [y for _, y in ring]
    across = (max(ys) - min(ys)) / dpr
    assert across >= 196, "the 200 px ring is only %.0f px across" % across
    assert min(ys) > 0 and max(ys) < h - 1, "the ring touches the edge"
    assert min(xs) > 0 and max(xs) < w - 1


def test_switching_mode_keeps_each_modes_size():
    """Simple and Object still remember their own sizes; Lasso has none and
    must not overwrite either."""
    c, ih, s = _board()
    c.set_eraser_mode("pixel"); c.set_eraser_size(30.0)
    c.set_eraser_mode("object"); c.set_eraser_size(90.0)
    c.set_eraser_mode("lasso")
    c.set_eraser_mode("pixel")
    assert c.eraser_size == 30.0
    c.set_eraser_mode("object")
    assert c.eraser_size == 90.0


if __name__ == "__main__":
    import logging
    logging.disable(logging.CRITICAL)
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
    sys.stdout.flush()
    os._exit(1 if failures else 0)
