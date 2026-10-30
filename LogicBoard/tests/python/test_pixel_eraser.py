"""
test_pixel_eraser.py — the Simple (pixel) eraser, end to end.

Runs under pytest, and also standalone (``python tests/python/test_pixel_eraser.py``)
because the repo's pytest install is currently broken on Python 3.11.

What is worth protecting here
-----------------------------
The eraser has two jobs that pull against each other. It has to cut ink to
the exact shape of its own footprint — that is the behaviour — and it has to
leave behind something a whiteboard can keep working with. Storing the
subtracted region as polygons gets the first right and the second wrong: a
smoothed stroke's outline flattens to hundreds of vertices, so erasing
inflates the board every time you do it.

So the commit re-expresses the survivors as STROKES whenever it can prove
they paint the same picture, and falls back to polygons when they can't. The
tests below pin both halves of that: the cases that must stay strokes, the
cases that must NOT, and the invariants (colour, width, undo, media safety)
that hold either way.
"""
import math
import os
import sys

os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")
sys.path.insert(0, os.path.abspath(os.path.join(os.path.dirname(__file__),
                                                "..", "..")))

from PySide6.QtWidgets import QApplication
from PySide6.QtCore import QPoint, QPointF, Qt

_app = QApplication.instance() or QApplication([])

import logicboard_engine as lbe
import ui.canvas.erase_geometry as eg
from ui.canvas.canvas_widget import CanvasWidget
from ui.canvas.input_handler import InputHandler

_BUILDERS = dict(
    build_freehand_path=lambda pts: CanvasWidget._build_freehand_path(None, pts),
    chisel_path=CanvasWidget._chisel_path,
)


# ---------------------------------------------------------------- helpers

def _seg(a, b, step=5.0):
    """Pointer samples along a straight drag."""
    n = max(1, int(math.hypot(b[0] - a[0], b[1] - a[1]) / step))
    return [(a[0] + (b[0] - a[0]) * i / n, a[1] + (b[1] - a[1]) * i / n)
            for i in range(n + 1)]


def _capsules(pts, r):
    out = [(pts[0][0], pts[0][1], pts[0][0], pts[0][1], float(r))]
    out += [(pts[i][0], pts[i][1], pts[i + 1][0], pts[i + 1][1], float(r))
            for i in range(len(pts) - 1)]
    return out


def _canvas():
    c = CanvasWidget()
    c.resize(1200, 800)
    ih = InputHandler(c)
    c._input_handler = ih
    c.set_tool("eraser")
    c.set_eraser_mode("pixel")
    return c, ih


def _pen(scene, pts, width=3.0, rgba=(255, 0, 0, 255)):
    o = lbe.FreehandStroke()
    o.set_points([(float(x), float(y)) for x, y in pts])
    o.stroke_width = width
    o.stroke_color = lbe.RGBA(*rgba)
    scene.add_object(o)
    return o


def _highlighter(scene, pts, height=35.0):
    o = lbe.HighlighterStroke()
    o.set_points([(float(x), float(y)) for x, y in pts])
    o.height = height
    o.stroke_color = lbe.RGBA(255, 235, 0, 255)
    o.opacity = 0.4
    scene.add_object(o)
    return o


def _rub(ih, canvas, path_pts, size):
    """Drive a whole pixel-eraser gesture: press, moves, release."""
    canvas.set_eraser_size(size)
    ih._pixel_eraser_extend(QPointF(*path_pts[0]), first=True)
    for p in path_pts[1:]:
        ih._pixel_eraser_extend(QPointF(*p))
    ih._commit_pixel_erase()


def _kinds(scene):
    d = {}
    for i in range(scene.object_count):
        t = scene.object_at(i).type_name
        d[t] = d.get(t, 0) + 1
    return d


_STRAIGHT = [(100 + i * 8, 300) for i in range(50)]


# ------------------------------------------------------- stays a stroke

def test_pen_cut_stays_two_pen_strokes():
    c, ih = _canvas()
    s = c.current_scene()
    _pen(s, _STRAIGHT)
    _rub(ih, c, _seg((400, 240), (400, 360)), 50.0)

    assert _kinds(s) == {"freehand": 2}, _kinds(s)
    assert {round(s.object_at(i).stroke_width, 2)
            for i in range(s.object_count)} == {3.0}
    col = s.object_at(0).stroke_color
    assert (col.r, col.g, col.b, col.a) == (255, 0, 0, 255)


def test_erased_stroke_does_not_balloon_its_vertex_count():
    """The reason to keep strokes at all. A curved stroke's outline flattens
    to hundreds of polygon vertices; the stroke form keeps roughly what it
    started with."""
    curve = [(100 + i * 7, 300 + 40 * math.sin(i / 5.0)) for i in range(60)]
    c, ih = _canvas()
    s = c.current_scene()
    _pen(s, curve, width=4.0)
    before = s.object_at(0).point_count
    _rub(ih, c, _seg((300, 200), (300, 400)), 44.0)

    after = sum(s.object_at(i).point_count for i in range(s.object_count))
    assert _kinds(s) == {"freehand": 2}, _kinds(s)
    assert after <= before + 8, f"{before} -> {after} points"


def test_wide_eraser_across_highlighter_stays_highlighter():
    c, ih = _canvas()
    s = c.current_scene()
    _highlighter(s, _STRAIGHT, height=35.0)
    _rub(ih, c, _seg((300, 220), (300, 380)), 50.0)

    assert _kinds(s) == {"highlighter": 2}, _kinds(s)
    assert {round(s.object_at(i).height, 2)
            for i in range(s.object_count)} == {35.0}
    assert {round(s.object_at(i).opacity, 2)
            for i in range(s.object_count)} == {0.4}


def test_second_rub_over_erased_ink_splits_again():
    """Regression: erased ink used to become a polygon, and a polygon the
    eraser did not recognise meant the second rub did nothing at all."""
    c, ih = _canvas()
    s = c.current_scene()
    _pen(s, _STRAIGHT)
    _rub(ih, c, _seg((300, 240), (300, 360)), 50.0)
    first = _kinds(s)
    _rub(ih, c, _seg((420, 240), (420, 360)), 50.0)

    assert _kinds(s).get("freehand", 0) > first.get("freehand", 0)


# --------------------------------------------------- falls back to polygon

def test_thin_eraser_along_a_fat_highlighter_falls_back_to_polygon():
    """A hole punched lengthways through a band leaves ink above AND below
    the cut. No polyline-plus-one-height can hold that, and cutting the
    centreline would wrongly remove the whole band."""
    c, ih = _canvas()
    s = c.current_scene()
    _highlighter(s, _STRAIGHT, height=35.0)
    _rub(ih, c, _seg((250, 300), (350, 300)), 10.0)

    k = _kinds(s)
    assert k.get("polygon", 0) > 0 and "highlighter" not in k, k


def test_narrow_eraser_across_a_tall_band_falls_back_to_polygon():
    """The bug the screenshots showed. A stroke stores ONE thickness, so
    cutting its centreline removes the ink's full height across the cut — and
    that is only what the eraser did if the eraser was at least as wide as the
    band is tall. Drag a 250 px eraser through a 560 px band and the preview
    shows a neat capsule bite while the commit stored a full-height notch.

    It slipped past ``runs_are_faithful`` because every term of that allowance
    scales with the stroke's own area: 36,000 px² of wrongly removed ink is
    under 4% of a band that size.
    """
    c, ih = _canvas()
    c.resize(1400, 900)
    s = c.current_scene()
    _highlighter(s, [(60 + i * 30, 500) for i in range(40)], height=560.0)
    _rub(ih, c, [(700, 900 - i * 12) for i in range(40)], 250.0)

    k = _kinds(s)
    assert k.get("polygon", 0) > 0 and "highlighter" not in k, \
        f"a band taller than the eraser must not stay a stroke: {k}"


def test_translucent_ink_is_not_split_into_pieces_that_overlap():
    """A scribble — one highlighter run back and forth to block something out.

    Cutting it leaves six runs that overlap each other, and six stored strokes
    are six objects with their own opacity, so every overlap darkens. One
    stroke painted in a single fill does not. The commit came back BANDED,
    38,000 px different from the preview the user had just been watching.

    ``runs_are_faithful`` is blind to this by construction: it compares the
    runs' UNION against the true remainder, and the union is exactly right.
    The shape is perfect and the picture is still wrong.
    """
    c, ih = _canvas()
    c.resize(1400, 800)
    s = c.current_scene()
    pts = []
    for row in range(5):
        y = 300 + row * 45
        xs = range(60, 1340, 40) if row % 2 == 0 else range(1340, 60, -40)
        pts += [(x, y) for x in xs]
    _highlighter(s, pts, height=120.0)
    _rub(ih, c, [(700, 780 - i * 16) for i in range(48)], 250.0)

    k = _kinds(s)
    assert "highlighter" not in k and k.get("polygon", 0) > 0, \
        f"overlapping pieces of translucent ink must not stay strokes: {k}"


def test_a_vertical_band_needs_polygons_because_its_CAPS_would_meet():
    """The chisel nib is an axis-aligned bar, so a vertical band cut across
    gets a full-height flat cap at each run end — and those two caps reach
    towards each other and close the gap the eraser just made.

    Worth pinning as its own case because the thickness rule ALLOWS this one
    through: a vertical stroke is only a nib thick across its direction of
    travel, so the eraser does cover it. Measured, strokes here differ from
    the preview by 420 px and polygons by 0.
    """
    c, ih = _canvas()
    c.resize(1000, 900)
    s = c.current_scene()
    _highlighter(s, [(500, 100 + i * 12) for i in range(50)], height=200.0)
    _rub(ih, c, [(380 + i * 10, 400) for i in range(24)], 60.0)

    k = _kinds(s)
    assert "highlighter" not in k and k.get("polygon", 0) > 0, \
        f"caps that meet across the gap must not stay strokes: {k}"


def test_edge_graze_on_a_thick_stroke_falls_back_to_polygon():
    """The bug the polygon path was originally added for: brushing the EDGE
    of a thick stroke must take a crescent bite, not sever it."""
    c, ih = _canvas()
    s = c.current_scene()
    _pen(s, _STRAIGHT, width=24.0)
    _rub(ih, c, _seg((240, 308), (360, 308)), 16.0)

    k = _kinds(s)
    assert k.get("polygon", 0) > 0 and "freehand" not in k, k


# --------------------------------------------------------- invariants

def test_grazing_the_bounding_box_without_touching_ink_changes_nothing():
    c, ih = _canvas()
    s = c.current_scene()
    _pen(s, [(100, 100), (500, 500)])
    _rub(ih, c, _seg((450, 150), (500, 200)), 20.0)   # above the diagonal

    assert _kinds(s) == {"freehand": 1}
    assert s.object_at(0).point_count == 2


def test_fully_covered_stroke_is_deleted():
    c, ih = _canvas()
    s = c.current_scene()
    _pen(s, [(300, 300), (320, 300), (340, 300)])
    _rub(ih, c, _seg((280, 300), (360, 300)), 90.0)

    assert s.object_count == 0


def test_imported_media_is_never_cut():
    """Load-bearing: a stray rub must not destroy a slide."""
    c, ih = _canvas()
    s = c.current_scene()
    s.add_object(lbe.ImageObject())
    _pen(s, [(200 + i * 8, 300) for i in range(30)])
    _rub(ih, c, _seg((300, 240), (300, 360)), 50.0)

    assert _kinds(s).get("image", 0) == 1


def test_one_gesture_is_one_undo_entry():
    c, ih = _canvas()
    s = c.current_scene()
    _pen(s, _STRAIGHT)
    _rub(ih, c, _seg((300, 240), (300, 360)), 50.0)
    assert s.object_count == 2

    c.current_stack().undo(s)
    assert _kinds(s) == {"freehand": 1}
    assert s.object_at(0).point_count == len(_STRAIGHT)


# ------------------------------------------------------------- geometry

def test_incremental_subtraction_matches_subtracting_the_union():
    """The preview folds capsules in one at a time; the old code subtracted
    one big union. They must agree — the whole design rests on it."""
    pts = [(100 + i * 8, 300) for i in range(40)]
    segs = _capsules(_seg((200, 250), (200, 350)) + _seg((330, 250), (330, 350)),
                     20.0)
    base = eg.stroke_outline(pts, "freehand", 6.0, **_BUILDERS)

    at_once = eg.subtract(base, eg.build_eraser_path(segs))
    one_by_one = base
    for s in segs:
        one_by_one = one_by_one.subtracted(eg.capsule_path(*s))

    assert abs(eg.path_area(at_once) - eg.path_area(one_by_one)) < 1.0


def test_eraser_field_agrees_with_brute_force_distance():
    """The grid index is an optimisation, not a behaviour change."""
    segs = _capsules(_seg((100, 100), (400, 380)), 17.0)
    field = eg.EraserField(segs)
    for i in range(400):
        x = 60 + (i * 37) % 400
        y = 60 + (i * 53) % 360
        brute = any(eg._pt_seg_dist_sq(x, y, a, b, cc, d) <= r * r
                    for (a, b, cc, d, r) in segs)
        assert field.covers(x, y) == brute, (x, y)


def test_surviving_runs_keep_the_original_samples():
    """Detection densifies the polyline; the runs handed back must not."""
    pts = [(100 + i * 40, 300) for i in range(12)]     # sparse, 40px apart
    field = eg.EraserField(_capsules(_seg((300, 250), (300, 350)), 15.0))
    runs = eg.surviving_runs(pts, field)

    assert len(runs) == 2
    assert sum(len(r) for r in runs) <= len(pts) + 2   # +1 boundary per run


def test_a_capsule_between_two_far_apart_samples_is_not_missed():
    """RDP simplification leaves samples far apart; a thin eraser dipping
    between two of them still has to cut."""
    pts = [(100, 300), (500, 300)]                     # two samples, 400px
    field = eg.EraserField(_capsules([(300, 300)], 12.0))
    runs = eg.surviving_runs(pts, field)

    assert len(runs) == 2, runs


def test_partial_repaints_leave_no_stale_pixels():
    """A drag repaints small rectangles instead of the whole canvas. That is
    only safe if those rectangles cover everything that actually changes —
    and the overlay culls itself to them, so an under-sized rect would leave
    half an erased stroke on screen until something else forced a repaint.

    Render before and after each sample, diff, and require every changed
    pixel to fall inside the region that sample asked for.
    """
    from PySide6.QtCore import QRect
    from PySide6.QtGui import QRegion

    asked = []
    real_update = CanvasWidget.update

    def spy(self, *a):
        if len(a) == 4:
            asked.append(QRect(a[0], a[1], a[2], a[3]))
        elif len(a) == 1 and isinstance(a[0], QRect):
            asked.append(QRect(a[0]))
        else:
            asked.append(None)              # full-widget repaint
        return real_update(self, *a)

    CanvasWidget.update = spy
    try:
        c, ih = _canvas()
        c.resize(900, 620)
        s = c.current_scene()
        for row in range(10):
            y = 70 + row * 50
            _pen(s, [(80 + i * 9, y + 14 * math.sin(i / 4.0)) for i in range(70)],
                 width=4.0)
        _highlighter(s, [(90 + i * 10, 560) for i in range(60)], height=40.0)
        c.invalidate_cache()
        c.set_eraser_size(44.0)

        pts = [(200 + 180 * math.sin(i / 18.0), 60 + i * 3.4) for i in range(150)]
        # Baseline AFTER the press. The press asks for a full repaint — it has
        # to, since whatever that first dab cuts stops being cached ink and
        # starts being a live subtracted region, and those two render the same
        # shape with slightly different antialiasing along its whole length.
        # Taking the baseline before it would diff across a repaint the loop
        # below never recorded, and blame the next sample for it.
        ih._pixel_eraser_extend(QPointF(*pts[0]), first=True)
        prev = c.grab().toImage()

        stale = 0
        for x, y in pts[1:]:
            asked.clear()
            ih._pixel_eraser_extend(QPointF(x, y))
            c._cursor_pos = c.scene_to_screen(QPointF(x, y)).toPoint()
            region, full = QRegion(), False
            for r in asked:
                if r is None:
                    full = True
                    break
                region = region.united(QRegion(r))
            cur = c.grab().toImage()
            if not full:
                region = region.united(
                    QRegion(c._ring_rect(c._cursor_pos).adjusted(-2, -2, 2, 2)))
                for py in range(0, cur.height(), 2):
                    for px in range(0, cur.width(), 2):
                        if (cur.pixel(px, py) != prev.pixel(px, py)
                                and not region.contains(QPoint(px, py))):
                            stale += 1
            prev = cur
        assert stale == 0, f"{stale} pixels changed outside the repainted region"
    finally:
        CanvasWidget.update = real_update


# --------------------------------------------- fragments == the whole thing

def _render_path(path, w=900, h=700):
    from PySide6.QtGui import QImage, QPainter, QColor
    img = QImage(w, h, QImage.Format_ARGB32)
    img.fill(0)
    p = QPainter(img)
    p.setRenderHint(QPainter.Antialiasing, True)
    p.setPen(Qt.NoPen)
    p.setBrush(QColor(0, 0, 0))
    p.drawPath(path)
    p.end()
    return img


def _hard_pixel_diff(a, b):
    """Pixels solid in one image and empty in the other.

    Deliberately blind to antialiasing: two descriptions of the same region
    flattened at different moments disagree by a few units of coverage all
    along their shared edge, and counting that would make the test a
    thermometer for Qt's rasteriser. A piece missing, a hole not punched, or
    a nub of ink left behind shows up as solid-versus-empty.
    """
    ba, bb = a.constBits().tobytes(), b.constBits().tobytes()
    w, h = a.width(), a.height()
    bad = 0
    for y in range(h):
        row = y * w * 4
        for x in range(w):
            pa, pb = ba[row + x * 4 + 3], bb[row + x * 4 + 3]
            if (pa > 200 and pb < 40) or (pb > 200 and pa < 40):
                bad += 1
    return bad


def _region_of(canvas, obj):
    return eg.ink_region_world(
        obj,
        to_world=lambda x, y, t: (t.a() * x + t.b() * y + t.tx(),
                                  t.c() * x + t.d() * y + t.ty()),
        transform_scale=CanvasWidget._eg_transform_scale,
        build_freehand_path=canvas._build_freehand_path,
        chisel_path=CanvasWidget._chisel_path,
    )


def _fragment_cases():
    """(label, object, size, kind) over the shapes that break naive splitting.

    ``sparse_corner`` is the one that matters: RDP-simplified samples far
    apart with a hard direction change. Splitting the smoothed curve at a
    SAMPLE instead of at a Bezier midpoint passes every dense-ink case and
    fails this one, because two straight chords replace a quad and the piece
    comes out fat at the join.
    """
    c, _ = _canvas()
    s = c.current_scene()
    cases = []
    cases.append(("dense_curve", _pen(
        s, [(80 + i * 6, 350 + 120 * math.sin(i / 9.0)) for i in range(120)],
        width=5.0), 5.0, "freehand"))
    cases.append(("thick_curve", _pen(
        s, [(80 + i * 9, 350 + 90 * math.cos(i / 7.0)) for i in range(80)],
        width=38.0), 38.0, "freehand"))
    cases.append(("sparse_corner", _pen(
        s, [(100, 200), (300, 210), (520, 640), (700, 180), (860, 600)],
        width=22.0), 22.0, "freehand"))
    cases.append(("highlighter", _highlighter(
        s, [(80 + i * 8, 350 + 130 * math.sin(i / 8.0)) for i in range(90)],
        height=44.0), 44.0, "highlighter"))
    return c, cases


def test_fragments_paint_the_same_region_as_the_whole_outline():
    canvas, cases = _fragment_cases()
    for label, obj, size, kind in cases:
        whole = eg.ink_outline_world(
            obj,
            to_world=lambda x, y, t: (t.a() * x + t.b() * y + t.tx(),
                                      t.c() * x + t.d() * y + t.ty()),
            transform_scale=CanvasWidget._eg_transform_scale,
            build_freehand_path=canvas._build_freehand_path,
            chisel_path=CanvasWidget._chisel_path,
        )
        region = _region_of(canvas, obj)
        assert region is not None and len(region.frags) > 1, \
            f"{label}: expected the ink to be split into pieces"
        bad = _hard_pixel_diff(_render_path(whole), _render_path(region.composite()))
        assert bad == 0, f"{label}: {bad} pixels differ before any erasing"


def test_fragments_erase_to_the_same_region_as_one_path():
    """The property the whole rewrite rests on: biting capsules out of the
    pieces has to leave the picture that biting them out of the single
    outline would have."""
    canvas, cases = _fragment_cases()
    rub = _seg((120, 620), (860, 200), step=7.0) + _seg((860, 200), (140, 260), step=7.0)
    caps = _capsules(rub, 21.0)
    for label, obj, size, kind in cases:
        whole = eg.ink_outline_world(
            obj,
            to_world=lambda x, y, t: (t.a() * x + t.b() * y + t.tx(),
                                      t.c() * x + t.d() * y + t.ty()),
            transform_scale=CanvasWidget._eg_transform_scale,
            build_freehand_path=canvas._build_freehand_path,
            chisel_path=CanvasWidget._chisel_path,
        )
        region = _region_of(canvas, obj)
        for cap in caps:
            cut = eg.capsule_path(*cap)
            if whole.intersects(cut):
                whole = whole.subtracted(cut)
            region.subtract_capsule(cap)
        assert region.hit, f"{label}: the rub should have removed ink"
        bad = _hard_pixel_diff(_render_path(whole),
                               _render_path(region.composite()))
        assert bad == 0, f"{label}: {bad} pixels differ after erasing"
        # ...and the areas the commit measures have to agree too, or a
        # faithful stroke re-expression gets rejected for the wrong reason.
        a1 = eg.path_area(whole)
        a2 = eg.path_area(region.composite().simplified())
        assert abs(a1 - a2) <= max(1.0, a1 * 0.001), f"{label}: {a1} vs {a2}"


def test_a_capsule_swallowing_a_piece_needs_no_boolean():
    """The OpenBoard ``eraserPath.contains(item)`` shortcut. A piece whose box
    sits inside the capsule is dropped outright — if this stops firing, the
    fast path is gone and only the timings would notice."""
    c, _ = _canvas()
    s = c.current_scene()
    obj = _pen(s, [(100 + i * 6, 300) for i in range(100)], width=4.0)
    region = _region_of(c, obj)
    before = len(region.frags)
    real = eg.capsule_path
    calls = []

    def counted(*a):
        calls.append(a)
        return real(*a)

    eg.capsule_path = counted
    try:
        # A 220-wide eraser centred on the line swallows whole pieces (they
        # span ~96 units) and only has to cut the two at the edges.
        region.subtract_capsule((400.0, 300.0, 400.0, 300.0, 110.0))
    finally:
        eg.capsule_path = real
    assert len(region.frags) < before, "nothing was removed"
    assert len(calls) == 1, f"expected one capsule path, built {len(calls)}"
    # Everything inside the disc is gone: sample the middle of the swept area.
    assert not region.composite().contains(QPointF(400.0, 300.0))


def test_overlay_grid_finds_the_same_objects_as_a_linear_scan():
    """The grid replaced a scan over every object on the page. It has to be
    an index, not a filter — an object it forgets is ink the eraser silently
    refuses to cut."""
    c, ih = _canvas()
    s = c.current_scene()
    for row in range(9):
        for col in range(9):
            _pen(s, [(60 + col * 120 + i * 9, 60 + row * 80)
                     for i in range(8)], width=3.0)
    # One deliberately page-spanning scribble, to exercise the oversized list.
    _pen(s, [(60 + i * 11, 60 + 620 * abs(math.sin(i / 6.0))) for i in range(90)])

    c.set_eraser_size(40.0)
    ih._pixel_eraser_extend(QPointF(80, 80), first=True)
    for x, y in _seg((80, 80), (1000, 700), step=11.0):
        ih._pixel_eraser_extend(QPointF(x, y))

    grid_ids = c._erase_overlay_ids()
    brute = set()
    for oid, x0, y0, x1, y1, _idx in c._erase_bbox_snapshot:
        for seg in c._eraser_mask_segments:
            if eg.segment_hits_box(seg, x0, y0, x1, y1):
                brute.add(oid)
                break
    assert grid_ids == brute, \
        f"grid found {len(grid_ids)}, linear scan found {len(brute)}"


# A scrub-out: one long horizontal stroke run back and forth over itself,
# which is what people do to cross something out before reaching for the
# eraser. Found by a randomised sweep, kept verbatim. Stroking the whole
# smoothed curve and calling ``simplified()`` on the result silently drops
# part of the outline here — the piecewise build does not, because each piece
# is a short arc that barely overlaps itself.
_SCRUB = [(262.5, 61.9), (265.8, 56.6), (262.7, 46.6), (295.3, 20.0),
          (344.2, 20.0), (365.4, 20.0), (400.3, 20.0), (426.2, 20.0),
          (458.6, 20.0), (462.3, 20.0), (467.0, 20.0), (507.7, 20.0),
          (529.6, 20.0), (502.9, 20.0), (512.3, 20.0), (523.2, 20.0),
          (536.7, 20.0), (562.2, 20.0), (588.2, 20.0), (600.0, 20.0),
          (625.3, 20.0), (618.2, 20.0), (621.7, 20.0), (613.4, 20.0),
          (608.4, 20.0), (561.7, 20.0), (510.2, 20.0), (459.8, 20.0),
          (441.3, 20.0), (397.3, 20.0), (374.5, 20.0), (351.0, 20.0),
          (318.0, 20.0), (311.4, 20.0), (301.0, 20.0), (271.2, 20.0),
          (242.7, 20.0), (221.0, 20.0), (203.5, 20.0), (183.9, 20.0),
          (143.8, 20.0), (120.4, 20.0), (98.6, 20.0), (73.3, 20.0),
          (24.4, 20.0), (20.0, 20.0), (20.0, 20.0)]


def test_pieces_hold_ink_the_single_outline_loses():
    """The pieces are measured against WHAT THE RENDERER PAINTS, not against
    the single-path outline — because on a stroke like this the single path is
    the one that is wrong.

    This matters beyond tidiness. The remainder is what the commit stores, so
    outline geometry that goes missing is ink that disappears from the board:
    rub one end of a scrubbed-out line and the other end vanishes with it,
    having never been under the eraser.
    """
    from PySide6.QtGui import QImage, QPainter, QColor, QPen
    c, _ = _canvas()
    s = c.current_scene()
    obj = _pen(s, _SCRUB, width=33.0)

    truth = QImage(720, 720, QImage.Format_ARGB32)
    truth.fill(0)
    q = QPainter(truth)
    q.setRenderHint(QPainter.Antialiasing, True)
    q.setPen(QPen(QColor(0, 0, 0), 33.0, Qt.SolidLine, Qt.RoundCap, Qt.RoundJoin))
    q.setBrush(Qt.NoBrush)
    q.drawPath(c._build_freehand_path(_SCRUB))
    q.end()

    region = _region_of(c, obj)
    pieces = _hard_pixel_diff(truth, _render_path(region.composite(), 720, 720))
    assert pieces == 0, f"the pieces differ from the painted stroke by {pieces} px"

    one_path = eg.ink_outline_world(
        obj,
        to_world=lambda x, y, t: (t.a() * x + t.b() * y + t.tx(),
                                  t.c() * x + t.d() * y + t.ty()),
        transform_scale=CanvasWidget._eg_transform_scale,
        build_freehand_path=c._build_freehand_path,
        chisel_path=CanvasWidget._chisel_path)
    # Pinned as a fact about the old approach, not as something to preserve:
    # if a future Qt fixes this, the assertion fires and the comment above can
    # be relaxed — but the pieces must keep matching the renderer either way.
    assert _hard_pixel_diff(truth, _render_path(one_path, 720, 720)) > 1000, \
        "the single-path outline no longer loses ink here — good, re-check why"


def test_patched_cache_matches_a_rebuilt_one():
    """A drag patches the scene cache in place instead of re-rasterising the
    page every time it reaches new ink. The patch has to be indistinguishable
    from the rebuild it replaces — a missed object or a z-order slip would
    show as a stroke that vanishes, or one that jumps in front of another,
    and only while erasing."""
    c, ih = _canvas()
    c.resize(900, 620)
    s = c.current_scene()
    for row in range(9):
        y = 70 + row * 55
        _pen(s, [(70 + i * 9, y + 18 * math.sin(i / 4.0)) for i in range(72)],
             width=5.0)
    _highlighter(s, [(90 + i * 10, 300) for i in range(60)], height=44.0)
    # Overlapping ink, so the patch has to get the stacking right and not just
    # the presence of each object.
    _pen(s, [(120 + i * 6, 120 + i * 4) for i in range(90)], width=9.0,
         rgba=(0, 90, 255, 255))
    c.invalidate_cache()
    c.grab()                      # settle the cache; there is nothing to patch
    c.set_eraser_size(46.0)       # until one exists

    ih._pixel_eraser_extend(QPointF(160, 90), first=True)
    for x, y in _seg((160, 90), (760, 520), step=9.0):
        ih._pixel_eraser_extend(QPointF(x, y))
    assert c._erase_cut_ids, "the rub should have cut something"
    assert not c._cache_dirty, "the cache should have been patched, not dropped"

    patched = c._scene_cache.toImage()
    c._rebuild_cache()
    rebuilt = c._scene_cache.toImage()
    assert _hard_pixel_diff(patched, rebuilt) == 0, "patched cache != rebuilt cache"


def test_ink_the_eraser_only_passes_near_stays_cached():
    """Only ink the eraser has actually CUT comes out of the scene cache. A
    stroke whose bounding box the rub crossed without touching its ink must
    keep its cached pixels — that set being the loose one was most of what a
    pointer sample cost."""
    c, ih = _canvas()
    s = c.current_scene()
    # A long diagonal: its bounding box covers most of the page while the ink
    # is a thin ribbon through it.
    _pen(s, [(100 + i * 8, 100 + i * 6) for i in range(80)], width=3.0)
    # ...and one horizontal stroke the eraser really will cut.
    target = _pen(s, [(120 + i * 8, 520) for i in range(60)], width=3.0)

    c.set_eraser_size(40.0)
    ih._pixel_eraser_extend(QPointF(120, 520), first=True)
    for x, y in _seg((120, 520), (600, 520), step=6.0):
        ih._pixel_eraser_extend(QPointF(x, y))

    candidates = c._erase_overlay_ids() or set()
    assert len(candidates) == 2, f"both boxes should be crossed, got {len(candidates)}"
    assert c._erase_cut_ids == {target.id}, \
        f"only the horizontal stroke was cut, held out: {len(c._erase_cut_ids)}"


# ------------------------------------------------- touch honours the menu

class _FakePoint:
    """Duck-typed QEventPoint. PySide6 won't let a test set a point's state,
    and ``touch_event`` only ever asks for these."""

    def __init__(self, pid, pos, state, contact):
        self._id, self._pos, self._state, self._c = pid, pos, state, contact

    def id(self):
        return self._id

    def position(self):
        return self._pos

    def state(self):
        from types import SimpleNamespace
        return SimpleNamespace(name=self._state)

    def ellipseDiameters(self):
        from PySide6.QtCore import QSizeF
        return QSizeF(self._c, self._c)


class _FakeTouch:
    def __init__(self, etype, pts):
        self._t, self._p = etype, pts

    def type(self):
        return self._t

    def points(self):
        return self._p

    def accept(self):
        pass


def _touch_board(mode, *, palm):
    """Six parallel strokes, 70 px apart, and a canvas set up so a contact is
    read as a palm (or deliberately not)."""
    c, ih = _canvas()
    c.resize(900, 700)
    s = c.current_scene()
    for k in range(6):
        _pen(s, [(80 + i * 12, 120 + k * 70) for i in range(50)], width=5.0)
    c.invalidate_cache()
    c.set_eraser_mode(mode)
    c.set_eraser_size(46.0)
    if palm:
        c._touch_mode = "palm_eraser"
        c._palm_eraser_enabled = True
        c._palm_min_size = 40           # a 90 px contact reads as a palm
        c.set_tool("pen")               # the TOOL must not matter for a palm
        contact = 90.0
    else:
        c._touch_mode = "palm_rejection"
        c.set_tool("eraser")
        contact = 18.0                  # a fingertip, well under palm size
    return c, ih, s, contact


def _rub_down(ih, contact, *, x=380.0, steps=12, end=True, pid=1):
    """Straight down through the middle of one stroke and nothing else."""
    from PySide6.QtCore import QEvent as _QE
    ih.touch_event(_FakeTouch(_QE.TouchBegin,
                              [_FakePoint(pid, QPointF(x, 168.0), "Pressed", contact)]))
    for i in range(1, steps):
        ih.touch_event(_FakeTouch(_QE.TouchUpdate,
                                  [_FakePoint(pid, QPointF(x, 168.0 + i * 2.0),
                                              "Updated", contact)]))
    if end:
        ih.touch_event(_FakeTouch(_QE.TouchEnd,
                                  [_FakePoint(pid, QPointF(x, 190.0), "Released", contact)]))


def test_palm_erase_follows_the_selected_eraser_mode():
    """A palm is an eraser, so it has to be the eraser the user PICKED.

    Both touch paths used to call the object eraser outright, so a palm always
    deleted whole strokes however the menu was set — rub a corner off a word
    with your hand on "Simple" and the whole word went.
    """
    for mode, expected in (("object", 5), ("pixel", 7)):
        c, ih, s, contact = _touch_board(mode, palm=True)
        _rub_down(ih, contact)
        assert s.object_count == expected, \
            f"palm in {mode} mode: 6 -> {s.object_count}, expected {expected}"
        assert not c._eraser_mask_segments, \
            f"palm in {mode} mode left a live mask behind"


def test_finger_erase_follows_the_selected_eraser_mode():
    """Same defect, one line away: the eraser TOOL driven by a finger also went
    straight to the object eraser."""
    for mode, expected in (("object", 5), ("pixel", 7)):
        c, ih, s, contact = _touch_board(mode, palm=False)
        _rub_down(ih, contact)
        assert s.object_count == expected, \
            f"finger in {mode} mode: 6 -> {s.object_count}, expected {expected}"


def test_one_palm_rub_is_one_undo_entry():
    c, ih, s, contact = _touch_board("pixel", palm=True)
    _rub_down(ih, contact)
    assert s.object_count == 7
    c.current_stack().undo(s)
    assert s.object_count == 6, \
        f"one undo should restore the stroke, got {s.object_count} objects"


def test_a_second_contact_cannot_hijack_a_pixel_rub():
    """The pixel mask is canvas-wide, so exactly one contact may drive it.

    A knuckle landing mid-rub used to restart the drag and throw away
    everything rubbed so far — the object eraser has no such state, which is
    why nothing needed to guard against it before.

    Two contacts mean a GESTURE, and a gesture erases nothing. So the rub is
    BANKED the moment the second contact lands, rather than carried on: until
    the commit runs, the cut ink is only a live preview over originals the
    scene still holds, so a rub abandoned mid-flight silently puts back ink
    the user already watched disappear. Everything after the bank is inert —
    which is the "cannot hijack" part, and the part worth pinning. The second
    contact must not start a rub of its own, and no capsule may be swept from
    the first contact clear across the board to where the second one landed.

    (This test used to require the mask to SURVIVE the second contact, from
    when the design was "ignore the intruder and keep rubbing". Banking
    replaced that, deliberately; see the head of InputHandler._handle_gesture.)
    """
    from PySide6.QtCore import QEvent as _QE
    c, ih, s, contact = _touch_board("pixel", palm=True)
    _rub_down(ih, contact, end=False)
    assert len(c._eraser_mask_segments) > 5, "the rub should have laid down a mask"
    assert s.object_count == 6, "nothing is committed until the rub ends"

    # A knuckle lands, far away from the rub.
    ih.touch_event(_FakeTouch(_QE.TouchUpdate, [
        _FakePoint(1, QPointF(380.0, 190.0), "Updated", contact),
        _FakePoint(2, QPointF(600.0, 400.0), "Pressed", contact)]))
    assert s.object_count == 7, \
        f"the rub should have banked what it cut, got {s.object_count}"
    assert not ih._touch_erasing and ih._touch_erase_id is None, \
        "a rub was left in flight for the gesture to inherit"
    assert not c._eraser_mask_segments, \
        "the mask outlived its rub and kept hiding ink"

    # Both contacts sweep on. Nothing may erase: not a new rub under the
    # knuckle, and above all not a capsule dragged between the two.
    ih.touch_event(_FakeTouch(_QE.TouchUpdate, [
        _FakePoint(1, QPointF(380.0, 260.0), "Updated", contact),
        _FakePoint(2, QPointF(640.0, 430.0), "Updated", contact)]))
    assert s.object_count == 7, \
        f"the gesture erased more ink, got {s.object_count} objects"
    assert not c._eraser_mask_segments, "the gesture started a mask of its own"

    ih.touch_event(_FakeTouch(_QE.TouchEnd, [
        _FakePoint(1, QPointF(380.0, 260.0), "Released", contact),
        _FakePoint(2, QPointF(640.0, 430.0), "Released", contact)]))
    assert s.object_count == 7, f"the lift erased more ink, got {s.object_count}"
    assert not ih._touch_erasing and not c._eraser_mask_segments


def test_pixel_rub_commits_when_its_own_contact_lifts():
    """Until the commit runs the cut ink is only a preview, so a rub whose
    contact lifts while another stays down must not be left hanging."""
    from PySide6.QtCore import QEvent as _QE
    c, ih, s, contact = _touch_board("pixel", palm=True)
    _rub_down(ih, contact, end=False)
    ih.touch_event(_FakeTouch(_QE.TouchUpdate,
                              [_FakePoint(2, QPointF(600.0, 400.0), "Pressed", contact)]))
    ih.touch_event(_FakeTouch(_QE.TouchUpdate,
                              [_FakePoint(1, QPointF(380.0, 190.0), "Released", contact)]))
    assert s.object_count == 7, \
        f"the rub should have committed on release, got {s.object_count}"
    assert not ih._touch_erasing and not c._eraser_mask_segments


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
    print(f"\n{len(failures)} failed" if failures else "\nall passed")
    sys.exit(1 if failures else 0)
