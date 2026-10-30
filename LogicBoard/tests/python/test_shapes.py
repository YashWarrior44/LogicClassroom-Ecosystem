"""
test_shapes.py — the 2D / 3D shapes: drawn, saved, loaded, and correct.

Runs under pytest, and also standalone
(``python tests/python/test_shapes.py``).

Every shape is drawn through the REAL input path — mouse press, move,
release into InputHandler — and what lands in the scene is checked: the
right engine object, the right kind, the proportion it should keep, and
that it survives a save and load. The 3D solids are checked for the one
thing that makes them read as solids: which edges are hidden.
"""
import math
import os
import sys
from pathlib import Path

os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")
sys.path.insert(0, str(Path(__file__).resolve().parents[2]))

from PySide6.QtCore import QEvent, QPointF, Qt
from PySide6.QtGui import QColor, QImage, QMouseEvent, QPainter
from PySide6.QtWidgets import QApplication

_app = QApplication.instance() or QApplication([])

import logicboard_engine as lbe
from ui.canvas import shape_geometry as sg
from ui.canvas.canvas_widget import CanvasWidget
from ui.canvas.input_handler import InputHandler


def _board():
    c = CanvasWidget()
    c.resize(1200, 900)
    ih = InputHandler(c)
    c._input_handler = ih
    c.set_pen_color(QColor(255, 255, 255))
    c.set_pen_width(4.0)
    c._touch_mode = "palm_off"
    return c, ih


def _drag(ih, a, b, steps=6):
    def ev(kind, x, y):
        return QMouseEvent(kind, QPointF(x, y), Qt.LeftButton, Qt.LeftButton,
                           Qt.NoModifier)
    ih.mouse_press(ev(QEvent.MouseButtonPress, *a))
    for i in range(1, steps + 1):
        t = i / steps
        ih.mouse_move(ev(QEvent.MouseMove, a[0] + (b[0] - a[0]) * t,
                         a[1] + (b[1] - a[1]) * t))
    ih.mouse_release(ev(QEvent.MouseButtonRelease, *b))


# ── the catalogue ───────────────────────────────────────────────────────────

def test_the_panel_offers_the_asked_for_shapes():
    ids = {k for k, _ in sg.SHAPES_2D}
    for want in ("parallelogram", "trapezium", "dotted_line", "dashed_line",
                 "rhombus", "pentagon", "hexagon", "right_triangle"):
        assert want in ids, want
    solids = {k for k, _ in sg.SHAPES_3D}
    for want in ("cube", "cuboid", "cylinder", "cone", "sphere", "pyramid"):
        assert want in solids, want
    assert len(sg.SHAPES_2D) == 20 and len(sg.SHAPES_3D) == 12


def test_every_shape_has_a_drawing_inside_its_box():
    for kind in sg.SHAPE_TOOLS:
        s, e = (100.0, 100.0), (300.0, 260.0)
        strokes = sg.outline(kind, s, e, 4.0)
        assert strokes, "%s draws nothing" % kind
        pts = sg.all_points(strokes)
        assert all(math.isfinite(x) and math.isfinite(y) for x, y in pts), kind
        if kind in sg.LINE_KINDS:
            continue
        xs = [p[0] for p in pts]
        ys = [p[1] for p in pts]
        assert min(xs) >= 99.9 and max(xs) <= 300.1 and \
            min(ys) >= 99.9 and max(ys) <= 260.1, \
            "%s spills out of the box it was dragged in" % kind


def test_a_degenerate_drag_draws_nothing_bad():
    for kind in sg.SHAPE_TOOLS:
        for s, e in (((5, 5), (5, 5)), ((5, 5), (5, 90)),
                     ((0, 0), (float("nan"), 3))):
            pts = sg.all_points(sg.outline(kind, s, e, 4.0))
            assert all(math.isfinite(v) for p in pts for v in p), kind


# ── 3D: hidden edges are where a textbook puts them ─────────────────────────

def _styles(kind):
    out = {}
    for _pts, _c, st in sg.outline(kind, (0, 0), (300, 300)):
        out[st] = out.get(st, 0) + 1
    return out


def test_box_solids_hide_the_three_edges_at_the_back_corner():
    for kind in ("cube", "cuboid"):
        st = _styles(kind)
        assert st.get(sg.DASHED) == 3 and st.get(sg.SOLID) == 9, (kind, st)


def test_pointed_and_prism_solids_hide_the_right_edges():
    expect = {"pyramid": (3, 5), "tetrahedron": (1, 5),
              "triangular_prism": (3, 6), "hexagonal_prism": (5, 13),
              "octahedron": (4, 8)}
    for kind, (dashed, solid) in expect.items():
        st = _styles(kind)
        assert (st.get(sg.DASHED, 0), st.get(sg.SOLID, 0)) == (dashed, solid), \
            (kind, st)


def test_round_solids_dash_the_back_of_their_rims():
    for kind in ("cylinder", "cone", "frustum", "sphere", "hemisphere"):
        st = _styles(kind)
        assert st.get(sg.DASHED, 0) >= 1, "%s has no hidden rim" % kind
        assert st.get(sg.SOLID, 0) >= 2, kind


def test_the_cone_sides_touch_the_base_ellipse_tangentially():
    """The usual shortcut joins the apex to the base's left and right ends,
    which visibly cuts INTO the ellipse on a wide cone."""
    strokes = sg.outline("cone", (0, 0), (400, 300))
    lines = [pts for pts, _c, st in strokes if len(pts) == 2]
    rim = [p for pts, _c, _s in strokes if len(pts) > 2 for p in pts]
    for a, b in lines:
        base = a if a[1] > b[1] else b
        d = min(math.dist(base, p) for p in rim)
        assert d < 2.0, "a side line does not meet the rim (%.1f px off)" % d


# ── drawn on the canvas, through real input ────────────────────────────────

def test_every_shape_tool_draws_through_the_real_input_path():
    c, ih = _board()
    for kind in sg.SHAPE_TOOLS:
        before = c.current_scene().object_count
        c.set_tool(kind)
        _drag(ih, (200, 200), (420, 360))
        sc = c.current_scene()
        assert sc.object_count == before + 1, "%s drew nothing" % kind
        o = sc.object_at(sc.object_count - 1)
        if kind in sg.SHAPE_KINDS:
            assert o.type_name == "shape" and o.kind == kind, (kind, o.type_name)
        assert abs(o.stroke_width - 4.0) < 1e-6


def test_locked_shapes_keep_their_proportion_on_the_canvas():
    c, ih = _board()
    for kind in ("square", "cube", "sphere", "hemisphere", "semicircle"):
        c.set_tool(kind)
        _drag(ih, (100, 100), (500, 180))            # deliberately squashed
        o = c.current_scene().object_at(c.current_scene().object_count - 1)
        w, h = abs(o.end.x - o.start.x), abs(o.end.y - o.start.y)
        assert abs(w / h - sg.locked_aspect(kind)) < 0.01, (kind, w, h)


def test_shapes_survive_save_and_load():
    c, ih = _board()
    for kind in sg.SHAPE_KINDS:
        c.set_tool(kind)
        _drag(ih, (150, 150), (330, 300))
    txt = c.current_scene().serialize_to_string(2)
    s2 = lbe.Scene()
    s2.deserialize_from_string(txt)
    kinds = [s2.object_at(i).kind for i in range(s2.object_count)]
    assert kinds == list(sg.SHAPE_KINDS), kinds


def test_a_placed_shape_renders_on_the_canvas():
    c, ih = _board()
    c.set_tool("cube")
    _drag(ih, (300, 200), (600, 500))
    img = c.grab().toImage()
    lit = 0
    for x in range(300, 600, 3):
        for y in range(200, 500, 3):
            if img.pixelColor(x, y).lightness() > 128:
                lit += 1
    assert lit > 40, "the cube did not appear on the canvas (%d lit px)" % lit


def test_the_icon_is_the_canvas_drawing():
    """No separate artwork to drift: the canvas renderer, the preview and
    the icon all call shape_geometry.paint."""
    import inspect
    from ui.canvas.canvas_widget import CanvasWidget as CW
    assert "sg.paint(" in inspect.getsource(CW._r_shape)
    assert "sg.paint(" in inspect.getsource(CW._paint_shape_preview)
    assert "paint(p, kind" in inspect.getsource(sg.shape_icon)


def test_icons_are_drawn_in_the_colour_asked_for():
    """Theme colours are CSS rgba() strings; QColor() reads those as black,
    which is how the first cut of these icons came out invisible."""
    pm = sg.shape_icon("cube", 64, "rgba(245, 245, 247, 0.96)")
    img = pm.toImage()
    lit = [img.pixelColor(x, y) for x in range(img.width())
           for y in range(img.height()) if img.pixelColor(x, y).alpha() > 200]
    assert lit and max(q.lightness() for q in lit) > 200, "the icon is not white"


# ── the panel ───────────────────────────────────────────────────────────────

def test_the_panel_switches_between_2d_and_3d_without_resizing():
    from ui.panels.shapes_panel import ShapesPanel
    p = ShapesPanel(None, current_shape="rect")
    p.show()
    _app.processEvents()
    assert p.current_tab() == "2D"
    size = p.size()
    p._set_tab("3D", sound=False)
    _app.processEvents()
    assert p.current_tab() == "3D" and p.size() == size, \
        "the panel resized between tabs: %s -> %s" % (size, p.size())


def test_the_panel_opens_on_the_tab_of_the_current_shape():
    from ui.panels.shapes_panel import ShapesPanel
    assert ShapesPanel(None, current_shape="cone").current_tab() == "3D"
    assert ShapesPanel(None, current_shape="star").current_tab() == "2D"


def test_choosing_a_shape_reports_it():
    from ui.panels.shapes_panel import ShapesPanel
    got = []
    p = ShapesPanel(None, on_select=lambda k, _e, l: got.append((k, l)))
    p._tiles["frustum"].click()
    assert got == [("frustum", "Frustum")]


if __name__ == "__main__":
    import logging
    logging.disable(logging.WARNING)
    failures = []
    for name, fn in sorted(globals().items()):
        if not name.startswith("test_") or not callable(fn):
            continue
        try:
            fn()
            print("  ok    " + name)
        except AssertionError as exc:
            failures.append(name)
            print("  FAIL  %s: %s" % (name, exc))
        except Exception as exc:                       # noqa: BLE001
            failures.append(name)
            print("  ERROR %s: %s: %s" % (name, type(exc).__name__, exc))
    print(("\n%d failed" % len(failures)) if failures else "\nall passed")
    sys.stdout.flush()
    os._exit(1 if failures else 0)
