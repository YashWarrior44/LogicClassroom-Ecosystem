"""
test_brush.py — the brush pen.

Runs under pytest, and also standalone (``python tests/python/test_brush.py``).

What is worth protecting here
-----------------------------
The brush is the highlighter's chisel ribbon, opaque and with the nib tilted.
Two consequences are worth pinning down.

**The tilt has to actually do something.** "Slightly tilted for a 3D effect"
is the whole feature; if the angle stopped reaching the path builder the brush
would silently become an opaque highlighter and look like a fat marker. So the
geometry is measured — a down-right stroke has to be several times wider than
an up-right one — rather than merely checked for existing.

**How a brush stroke is STORED is load-bearing.** It is a HighlighterStroke
with opacity 1.0, because the engine has a fixed set of object types and the
brush needed no new field beyond "opaque". That only works while a highlighter
can never itself be opaque, which is true today because its alpha is a
hard-coded 75/255 with no setter and no UI. ``test_the_highlighter_is_never_
opaque`` is what stops someone quietly breaking that by adding a highlighter
opacity slider — at which point old boards would start rendering their
highlighters tilted. If that test fails, the fix is a real BrushStroke type in
the engine, not a looser threshold. See ui/canvas/brush_ink.py.
"""
import math
import os
import sys

os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")
sys.path.insert(0, os.path.abspath(os.path.join(os.path.dirname(__file__),
                                                "..", "..")))

from PySide6.QtWidgets import QApplication
from PySide6.QtGui import QImage, QPainter, QColor, QBrush
from PySide6.QtCore import Qt, QPointF

_app = QApplication.instance() or QApplication([])

from ui.canvas.canvas_widget import CanvasWidget
from ui.canvas.brush_ink import (BRUSH_NIB_ANGLE_DEG, BRUSH_OPACITY,
                                 OPAQUE_THRESHOLD, is_brush_stroke,
                                 nib_angle_for)


# ── measuring the ribbon ────────────────────────────────────────────────────

def _ink_width(pts, height, angle):
    """Average width of the ribbon: filled area over centreline length.

    Measured off a real render rather than computed from the geometry, so it
    tests what the user sees and not a restatement of the maths.
    """
    path = CanvasWidget._chisel_path([QPointF(x, y) for x, y in pts],
                                     float(height), angle_deg=angle)
    img = QImage(300, 260, QImage.Format_ARGB32)
    img.fill(Qt.transparent)
    p = QPainter(img)
    p.setRenderHint(QPainter.Antialiasing, False)
    p.setPen(Qt.NoPen)
    p.setBrush(QBrush(QColor("black")))
    p.drawPath(path)
    p.end()
    covered = sum(1 for y in range(260) for x in range(300)
                  if img.pixelColor(x, y).alpha() > 128)
    length = math.dist(pts[0], pts[-1])
    return covered / max(1.0, length)


DOWN_RIGHT = [(30, 30), (210, 190)]
UP_RIGHT = [(30, 190), (210, 30)]
HORIZONTAL = [(30, 120), (230, 120)]
VERTICAL = [(120, 30), (120, 210)]


# ── the tilt does something ─────────────────────────────────────────────────

def test_the_brush_nib_is_tilted_at_all():
    assert BRUSH_NIB_ANGLE_DEG > 0, "the brush nib is upright — no calligraphy"
    assert 15.0 <= BRUSH_NIB_ANGLE_DEG <= 60.0, (
        "past about 60 degrees the thick and thin axes swap over and writing "
        "leans backwards; below 15 the effect is invisible")


def test_a_pulled_stroke_is_much_fatter_than_a_lifted_one():
    """THE feature. A broad nib held at an angle writes thick one way and
    thin the other; that contrast IS the calligraphic look."""
    down = _ink_width(DOWN_RIGHT, 30, BRUSH_NIB_ANGLE_DEG)
    up = _ink_width(UP_RIGHT, 30, BRUSH_NIB_ANGLE_DEG)
    assert down > up * 2.5, (
        f"down-right {down:.1f}px vs up-right {up:.1f}px — the nib is barely "
        "tilted, so the stroke looks like a plain marker")


def test_the_thin_direction_is_still_visible():
    """A mathematically zero-width nib would vanish; a real chisel tip has
    thickness, and a stroke that disappears reads as a broken tool."""
    up = _ink_width(UP_RIGHT, 30, BRUSH_NIB_ANGLE_DEG)
    assert up >= 3.0, f"the thin direction is only {up:.1f}px wide"


def test_the_upright_nib_still_behaves_like_a_highlighter():
    """Angle 0 is the highlighter, and it must keep its marker behaviour:
    wide across, nearly nothing down."""
    wide = _ink_width(HORIZONTAL, 30, 0.0)
    thin = _ink_width(VERTICAL, 30, 0.0)
    assert wide > 25, wide
    assert thin < 6, thin


def test_the_brush_is_not_just_an_opaque_highlighter():
    """If the angle stopped being passed through, the brush would still
    draw — as a fat marker. Its direction profile has to differ."""
    brush_v = _ink_width(VERTICAL, 30, BRUSH_NIB_ANGLE_DEG)
    hl_v = _ink_width(VERTICAL, 30, 0.0)
    assert brush_v > hl_v * 3, (
        f"vertical stroke: brush {brush_v:.1f}px vs highlighter {hl_v:.1f}px "
        "— the tilt is not reaching the path builder")


def test_the_ribbon_is_continuous_not_a_row_of_blobs():
    """Each pair of samples contributes the hull of the two nib positions,
    which is exactly the swept region. Gaps at direction changes are what
    killed an earlier chisel implementation."""
    zig = [(30, 60), (110, 160), (190, 60), (250, 150)]
    path = CanvasWidget._chisel_path([QPointF(x, y) for x, y in zig], 26.0,
                                     angle_deg=BRUSH_NIB_ANGLE_DEG)
    img = QImage(300, 260, QImage.Format_ARGB32)
    img.fill(Qt.transparent)
    p = QPainter(img)
    p.setRenderHint(QPainter.Antialiasing, False)
    p.setPen(Qt.NoPen); p.setBrush(QBrush(QColor("black")))
    p.drawPath(path)
    p.end()
    # Walk the centreline the ink is actually swept along — the SMOOTHED
    # one. Walking the raw samples would be wrong: midpoint-quadratic
    # smoothing makes the curve pass between them and cut corners, exactly as
    # the pen has always done, so a hard zig-zag vertex sits outside its own
    # stroke by design. Continuity of the sweep is the real property here.
    dense = CanvasWidget._smooth_dense(
        CanvasWidget._thinned_centreline(
            [QPointF(x, y) for x, y in zig], 0, len(zig), 0.5))
    misses = 0
    for i in range(len(dense) - 1):
        (x0, y0), (x1, y1) = dense[i], dense[i + 1]
        for t in (0.25, 0.5, 0.75):
            x = int(x0 + (x1 - x0) * t)
            y = int(y0 + (y1 - y0) * t)
            if img.pixelColor(x, y).alpha() < 128:
                misses += 1
    assert misses == 0, f"{misses} gaps along the centreline"


def test_the_ribbon_follows_a_smooth_curve_not_the_raw_samples():
    """The blockiness fix, pinned.

    The nib is swept along the midpoint-quadratic curve through the samples,
    the same one the pen draws. A stroke built straight from the samples has
    one polygon edge per sample pair and shows as flat facets on any slow
    arc; this is what stops that.
    """
    raw = [(20, 100), (80, 40), (140, 100), (200, 40)]
    thinned = CanvasWidget._thinned_centreline(
        [QPointF(x, y) for x, y in raw], 0, len(raw), 0.5)
    dense = CanvasWidget._smooth_dense(thinned)
    assert len(dense) > len(thinned) * 2, (
        f"{len(thinned)} samples became {len(dense)} points — the curve is "
        "not being re-sampled, so the ribbon is still a chain of chords")
    # …and a straight run must NOT be densified, or every stroke pays for it.
    straight = CanvasWidget._smooth_dense(
        CanvasWidget._thinned_centreline(
            [QPointF(x, 100) for x in (20, 80, 140, 200)], 0, 4, 0.5))
    assert len(straight) <= 6, (
        f"a straight line produced {len(straight)} points; densification is "
        "not adaptive")


def test_a_single_tap_still_leaves_a_mark():
    path = CanvasWidget._chisel_path([QPointF(50, 50)], 24.0,
                                     angle_deg=BRUSH_NIB_ANGLE_DEG)
    assert not path.isEmpty(), "a tap with the brush drew nothing"


def test_an_empty_stroke_is_harmless():
    assert CanvasWidget._chisel_path([], 24.0,
                                     angle_deg=BRUSH_NIB_ANGLE_DEG).isEmpty()


# ── how a brush stroke is stored ────────────────────────────────────────────

def test_the_highlighter_is_never_opaque():
    """THE invariant the storage scheme rests on.

    A brush stroke is recognised on the way back in by being a fully opaque
    highlighter stroke. Give the highlighter a user-settable opacity and that
    stops being true — old boards would render their highlighters tilted and
    solid. If this fails, add a BrushStroke type to the engine rather than
    moving the threshold.
    """
    import inspect
    from ui.canvas import canvas_widget
    src = inspect.getsource(canvas_widget)
    assert "self._highlighter_alpha = 75" in src, \
        "the highlighter's alpha moved — check it still cannot reach opaque"
    assert "def set_highlighter_alpha" not in src, \
        ("the highlighter gained an opacity setter, which collides with how "
         "brush strokes are identified — see ui/canvas/brush_ink.py")
    assert 75 / 255.0 < OPAQUE_THRESHOLD


def test_an_opaque_chisel_stroke_is_a_brush():
    class _O:
        opacity = BRUSH_OPACITY
    assert is_brush_stroke(_O()) is True
    assert nib_angle_for(_O()) == BRUSH_NIB_ANGLE_DEG


def test_a_translucent_chisel_stroke_is_a_highlighter():
    class _O:
        opacity = 75 / 255.0
    assert is_brush_stroke(_O()) is False
    assert nib_angle_for(_O()) == 0.0


def test_a_rounding_wobble_still_reads_as_a_brush():
    """Opacity round-trips through JSON; 0.9999999 is still opaque."""
    class _O:
        opacity = 0.9999999
    assert is_brush_stroke(_O()) is True


def test_a_junk_object_is_not_mistaken_for_a_brush():
    class _O:
        opacity = None
    assert is_brush_stroke(_O()) is False
    assert is_brush_stroke(object()) is False


# ── the tool, end to end ────────────────────────────────────────────────────

def _board_window():
    import tempfile
    from pathlib import Path
    import ui.util.paths as paths
    tmp = Path(tempfile.mkdtemp(prefix="lb_brush_test_"))
    paths.saves_dir = lambda: tmp
    import ui.main_window as mw
    mw.saves_dir = lambda: tmp
    import ui.panels.file_panel as fp
    fp.saves_dir = lambda: tmp
    return mw.MainWindow(), tmp


def _draw(win, tool, pts):
    c = win._canvas
    c.set_tool(tool)
    c.begin_live_stroke(is_highlighter=(tool == "highlighter"),
                        is_brush=(tool == "brush"))
    for x, y in pts:
        c.add_live_point(QPointF(x, y), 1.0)
    win._input_handler._commit_stroke(tool)


def test_drawing_with_the_brush_commits_an_opaque_stroke():
    win, _tmp = _board_window()
    _draw(win, "brush", [(50, 50), (150, 150)])
    objs = list(win._canvas.current_scene().all_objects())
    assert len(objs) == 1
    assert objs[0].type_name == "highlighter"
    assert is_brush_stroke(objs[0]), \
        f"committed with opacity {objs[0].opacity} — not recognisable as a brush"


def test_the_brush_uses_its_own_ink_and_size():
    """Not the highlighter's. They are used for different things and each
    keeps its own colour and nib size."""
    win, _tmp = _board_window()
    win._canvas.set_brush_height(18.0)
    win._canvas.set_brush_color(QColor(10, 20, 30))
    _draw(win, "brush", [(50, 50), (150, 150)])
    o = list(win._canvas.current_scene().all_objects())[0]
    assert o.height == 18.0
    assert (o.stroke_color.r, o.stroke_color.g, o.stroke_color.b) == (10, 20, 30)


def test_the_highlighter_still_commits_translucent():
    win, _tmp = _board_window()
    _draw(win, "highlighter", [(50, 50), (150, 150)])
    o = list(win._canvas.current_scene().all_objects())[0]
    assert not is_brush_stroke(o), "the highlighter now commits opaque"


def test_a_brush_stroke_survives_save_and_reload():
    """The identity is carried by the file, not by anything in memory."""
    win, tmp = _board_window()
    _draw(win, "brush", [(50, 50), (150, 150)])
    _draw(win, "highlighter", [(50, 200), (150, 200)])
    assert win._save_as_board("Brush Board") is True

    import ui.main_window as mw
    win2 = mw.MainWindow()
    win2._open_board(tmp / "Brush Board.lb")
    objs = list(win2._canvas.current_scene().all_objects())
    brushes = [o for o in objs if o.type_name == "highlighter"
               and is_brush_stroke(o)]
    marks = [o for o in objs if o.type_name == "highlighter"
             and not is_brush_stroke(o)]
    assert len(brushes) == 1, "the brush stroke did not come back as a brush"
    assert len(marks) == 1, "the highlighter came back as something else"


def test_the_panel_offers_the_brush():
    from ui.panels.pen_panel import PenPanel
    from PySide6.QtWidgets import QToolButton
    p = PenPanel(mode="brush")
    tiles = [b.text() for b in p.findChildren(QToolButton)]
    assert "Brush" in tiles, tiles
    assert p._slider.minimum() >= 5 and p._slider.maximum() <= 80, \
        "the brush slider covers highlighter-sized nibs; it is a writing tool"
    assert "nib" in p._value_label.text()
    p.close()


def test_picking_brush_in_the_panel_switches_the_canvas_tool():
    win, _tmp = _board_window()
    win._on_pen_mode_change("brush")
    assert win._canvas.tool == "brush"


def test_the_brush_starts_with_the_pen_ink_and_a_calligraphic_nib():
    """Picking the brush up should change the stroke SHAPE, not the ink —
    and the shape has to be visible. It used to start at the pen's 3 px,
    where the nib is nearly square and the brush drew exactly like the pen:
    "it just looks like a normal pen". The nib must be several times longer
    than it is thick for thick-and-thin to show."""
    from ui.canvas.brush_ink import nib_dimensions
    win, _tmp = _board_window()
    assert win._canvas.brush_color.name() == win._canvas.pen_color.name()
    half_len, half_thick = nib_dimensions(win._canvas.brush_height)
    assert half_len >= 4 * half_thick, (
        "a %.0f px brush nib is %.1f x %.1f — too square to look like a brush"
        % (win._canvas.brush_height, 2 * half_len, 2 * half_thick))


def test_the_pen_and_the_dual_pen_keep_separate_ink():
    """They shared one colour, so recolouring either silently recoloured the
    other — in both directions."""
    win, _tmp = _board_window()
    win._canvas.set_tool("pen")
    win._on_pen_color_change(QColor(255, 0, 0))
    win._canvas.set_tool("dual_pen")
    win._on_pen_color_change(QColor(0, 255, 0))

    assert win._canvas.pen_color.name() == "#ff0000",         "picking a colour in Dual Pen changed the plain pen's"
    assert win._canvas.dual_pen_color.name() == "#00ff00"

    win._canvas.set_tool("pen")
    win._on_pen_color_change(QColor(0, 0, 255))
    assert win._canvas.dual_pen_color.name() == "#00ff00",         "picking a colour in Pen changed Dual Pen's"


def test_a_dual_pen_stroke_draws_with_the_dual_pen_ink():
    """The separation has to reach the ink on the page, not just the panel:
    Dual Pen resolves to the pen TOOL, so the commit has to ask for the
    active colour rather than the pen's."""
    win, _tmp = _board_window()
    win._canvas.set_dual_pen_color(QColor(9, 99, 199))
    win._canvas.set_pen_color(QColor(255, 0, 0))
    win._canvas.set_tool("dual_pen")
    c = win._canvas
    c.begin_live_stroke()
    for x, y in ((40, 40), (120, 120)):
        c.add_live_point(QPointF(x, y), 1.0)
    win._input_handler._commit_stroke("pen")
    o = list(c.current_scene().all_objects())[0]
    assert (o.stroke_color.r, o.stroke_color.g, o.stroke_color.b) == (9, 99, 199)


def test_the_panel_offers_four_tiles_without_growing():
    """Adding Brush must not make the panel taller: the four tiles share
    one size and fit in the column three of the old 140 px tiles took."""
    from ui.panels.pen_panel import PenPanel
    p = PenPanel(mode="pen")
    p.show()
    _app.processEvents(); _app.processEvents()
    # By attribute, not by caption: the Dual Pen back-tip picker has its own
    # "Pen" and "Highlighter" tiles and a text search collects those too.
    tiles = [p._pen_btn, p._hl_btn, p._brush_btn, p._dual_btn]
    # The Dual Pen tile carries a padlock in its caption when a stylus tip
    # is uncalibrated — the "needs calibration" warning lives INSIDE the
    # tile now rather than in a label underneath it. Strip it before
    # comparing; which tile is which is the thing being checked here.
    captions = [b.text().replace("🔒", "").strip() for b in tiles]
    assert captions == ["Pen", "Highlighter", "Brush", "Dual Pen"]
    assert len({(b.width(), b.height()) for b in tiles}) == 1, (
        "the four tool tiles are not one size")
    from ui.util.scaling import UiScale
    # Three old tiles + their gaps, as a ceiling for four new ones.
    old_column = 3 * UiScale.px(140) + 2 * UiScale.px(10)
    new_column = 4 * tiles[0].height() + 3 * UiScale.px(10)
    assert new_column <= old_column, (
        f"the tile column grew from {old_column} to {new_column}")
    p.close()


def test_erasing_a_brush_stroke_uses_the_tilted_outline():
    """The eraser computed a chisel outline with an UPRIGHT nib whatever the
    stroke was, so for a brush it measured coverage against a shape the
    renderer never painted and cut in the wrong place."""
    import inspect
    from ui.canvas import input_handler
    src = inspect.getsource(input_handler._InputHandler._runs_for)         if hasattr(input_handler, "_InputHandler") else ""
    if not src:
        # Class name differs; fall back to the module.
        src = inspect.getsource(input_handler)
    assert "nib_angle_for" in src,         "the erase outline no longer accounts for the nib angle"


# ── the paths that only run in the real app ────────────────────────────────
#
# Committing a stroke and rendering a committed stroke are not the whole tool.
# The LIVE preview is separate code — it bakes settled samples into a layer in
# batches and redraws only the tail each frame — and the PIXEL eraser is
# separate again. Both were changed for the brush, and neither is touched by
# the geometry tests above, so both get driven here the way the app drives
# them.

def _erase_across_middle(win, tool, height):
    """Draw a long horizontal stroke, then rub the pixel eraser through it."""
    c, ih = win._canvas, win._input_handler
    if tool == "brush":
        c.set_brush_height(height)
    else:
        c.set_highlighter_height(height)
    c.set_tool(tool)
    c.begin_live_stroke(is_highlighter=(tool == "highlighter"),
                        is_brush=(tool == "brush"))
    for i in range(60):
        c.add_live_point(QPointF(60 + i * 8, 300), 1.0)
    ih._commit_stroke(tool)

    c.set_tool("eraser")
    try:
        c.set_eraser_mode("pixel")
    except AttributeError:
        c._eraser_mode = "pixel"
    c._eraser_mask_segments = []
    ih._pixel_eraser_extend(QPointF(250, 300), first=True)
    for x in range(256, 330, 6):
        ih._pixel_eraser_extend(QPointF(x, 300))
    ih._commit_pixel_erase()
    return list(c.current_scene().all_objects())


def test_the_pixel_eraser_splits_a_brush_stroke_in_two():
    """Rubbing through the middle has to CUT the stroke, not delete it and
    not leave it whole."""
    win, _tmp = _board_window()
    after = _erase_across_middle(win, "brush", 26.0)
    assert len(after) == 2, f"{len(after)} fragment(s) after the rub"
    left, right = sorted(after, key=lambda o: min(p[0] for p in o.points()))
    assert max(p[0] for p in left.points()) < min(p[0] for p in right.points())


def test_erasing_leaves_the_fragments_still_brush_strokes():
    """The split rebuilds objects. If height or opacity were dropped the
    surviving pieces would come back as a different tool — which is exactly
    what "erasing changes the shape" looked like."""
    win, _tmp = _board_window()
    after = _erase_across_middle(win, "brush", 26.0)
    for o in after:
        assert o.height == 26.0, f"height became {o.height}"
        assert is_brush_stroke(o), "a fragment stopped being a brush stroke"


def test_erasing_a_highlighter_still_works_too():
    win, _tmp = _board_window()
    after = _erase_across_middle(win, "highlighter", 35.0)
    assert len(after) == 2
    for o in after:
        assert o.height == 35.0
        assert not is_brush_stroke(o), "a highlighter fragment turned opaque"


def _live_stroke(win, tool, height, n=140):
    """Drive a live stroke long enough to cross the bake threshold repeatedly,
    then paint the widget for real."""
    c = win._canvas
    if tool == "brush":
        c.set_brush_height(height)
    else:
        c.set_highlighter_height(height)
    c.set_tool(tool)
    c.begin_live_stroke(is_highlighter=(tool == "highlighter"),
                        is_brush=(tool == "brush"))
    for i in range(n):
        c.add_live_point(QPointF(80 + i * 5, 250 + math.sin(i / 12.0) * 60), 1.0)
    img = QImage(c.size(), QImage.Format_ARGB32)
    img.fill(QColor(20, 20, 20))
    c.render(img)
    return img


def test_the_live_preview_bakes_a_long_chisel_stroke_without_dying():
    """The live layer bakes settled samples in batches of 12 and redraws only
    the tail. That path builds its own chisel ribbon and had to learn the nib
    angle; none of the geometry tests reach it."""
    win, _tmp = _board_window()
    win.resize(900, 500)
    win.show()
    _app.processEvents()
    img = _live_stroke(win, "brush", 20.0)
    c = win._canvas
    assert c._live_is_chisel is True
    assert c._live_nib_angle == BRUSH_NIB_ANGLE_DEG
    assert c._live_baked_idx > 100, (
        f"only {c._live_baked_idx} samples baked — the batching never ran, so "
        "this did not exercise the path it was meant to")
    ink = sum(1 for y in range(0, img.height(), 4)
              for x in range(0, img.width(), 4)
              if img.pixelColor(x, y).lightness() > 60)
    assert ink > 200, f"the live preview painted almost nothing ({ink} samples)"


def test_the_live_highlighter_preview_stays_upright():
    win, _tmp = _board_window()
    win.resize(900, 500)
    win.show()
    _app.processEvents()
    _live_stroke(win, "highlighter", 34.0)
    assert win._canvas._live_is_chisel is True
    assert win._canvas._live_nib_angle == 0.0,         "the highlighter's live preview picked up the brush tilt"


def test_the_live_preview_and_the_committed_stroke_agree():
    """They are built by the same path builder, so what settles onto the page
    must not change shape at the moment the stroke is released."""
    win, _tmp = _board_window()
    win.resize(900, 500)
    win.show()
    _app.processEvents()
    _live_stroke(win, "brush", 20.0)
    live_pts = list(win._canvas._live_points)
    win._input_handler._commit_stroke("brush")
    o = list(win._canvas.current_scene().all_objects())[0]
    assert o.point_count == len(live_pts), (
        f"{len(live_pts)} live samples became {o.point_count} committed ones")
    assert o.height == 20.0


def test_the_brush_has_its_own_icon():
    from pathlib import Path
    from ui.util.icon_loader import tool_icon
    svg = (Path(__file__).resolve().parents[2] / "assets" / "icons" / "brush.svg")
    assert svg.exists(), "assets/icons/brush.svg is missing"
    assert not tool_icon("brush", 48, "#ffffff").isNull()
    # It must not silently fall back to some other tool's artwork.
    text = svg.read_text(encoding="utf-8")
    assert "viewBox" in text



# ── the nib's proportions ───────────────────────────────────────────────────

def test_the_thin_axis_never_lands_on_a_half_pixel():
    """WHY 2.0 AND NOT 1.5.

    The old floor was 1.5px. A 1.5px band cannot cover a whole pixel row no
    matter where it falls — it always straddles two, antialiasing splits the
    ink between them, and neither reaches full black. At the 3px default that
    is most of the stroke, so the brush read grey and soft next to a pen that
    read as ink. This is the number the fix turns on.
    """
    from ui.canvas.brush_ink import MIN_NIB_THICKNESS
    assert MIN_NIB_THICKNESS >= 2.0, (
        "a sub-2px nib floor cannot fill a pixel row; brush strokes go hazy")


def test_the_nib_never_becomes_wider_than_it_is_long():
    """Past that point the thick and thin axes swap and a stroke swells
    exactly where calligraphy says it should thin."""
    from ui.canvas.brush_ink import nib_dimensions
    for h in (0.5, 1.0, 2.0, 3.0, 4.0, 6.0, 10.0, 30.0, 80.0):
        half_len, half_thick = nib_dimensions(h)
        assert half_thick <= half_len + 1e-9, (
            f"at height {h} the nib is {half_thick * 2:.2f} thick and only "
            f"{half_len * 2:.2f} long")


def test_the_long_axis_is_still_the_size_the_user_picked():
    """The floor must not quietly fatten the tool. Whatever it does to the
    thin axis, a 10px brush is still 10px across its broad face."""
    from ui.canvas.brush_ink import nib_dimensions
    for h in (3.0, 6.0, 10.0, 30.0):
        half_len, _ = nib_dimensions(h)
        assert abs(half_len * 2.0 - h) < 1e-9


def test_a_nonsense_height_does_not_crash_the_renderer():
    """height comes off disk with the board file."""
    from ui.canvas.brush_ink import nib_dimensions
    for bad in (0.0, -5.0, float("nan"), None, "big"):
        half_len, half_thick = nib_dimensions(bad)
        assert half_len > 0.0 and half_thick > 0.0, f"{bad!r} -> degenerate nib"


def test_the_thin_direction_got_measurably_darker():
    """The regression, measured rather than asserted by eye: a stroke drawn
    across the nib must put down a decent share of FULLY black pixels, not a
    wide smear of grey."""
    import math
    from PySide6.QtGui import QImage, QPainter, QColor, QBrush
    from PySide6.QtCore import Qt, QPointF
    from ui.canvas.canvas_widget import CanvasWidget
    from ui.canvas.brush_ink import BRUSH_NIB_ANGLE_DEG

    # 130 degrees is across the 40-degree nib: the thin sweep, worst case.
    a = math.radians(130.0)
    pts = [QPointF(150 + math.cos(a) * 200 * i / 139.0,
                   150 + math.sin(a) * 200 * i / 139.0) for i in range(140)]

    img = QImage(300, 300, QImage.Format_ARGB32)
    img.fill(QColor(255, 255, 255))
    p = QPainter(img)
    p.setRenderHint(QPainter.Antialiasing, True)
    p.setPen(Qt.NoPen)
    p.setBrush(QBrush(QColor(0, 0, 0)))
    p.drawPath(CanvasWidget._chisel_path(pts, 3.0, angle_deg=BRUSH_NIB_ANGLE_DEG))
    p.end()

    solid = inked = 0
    for y in range(300):
        for x in range(300):
            v = img.pixelColor(x, y).red()
            if v < 255:
                inked += 1
            if v <= 8:
                solid += 1
    assert inked > 0, "the thin direction drew nothing at all"
    share = solid / inked
    # The 1.5px floor scored 0.156 here; 2.0px scores 0.285. Anything back
    # under a fifth means the floor has been lowered again.
    assert share >= 0.22, (
        f"only {share:.1%} of the thin-direction ink is fully black — the "
        "brush is rendering as grey haze again")

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
