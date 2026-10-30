"""
erase_geometry.py — the ONE definition of "what the eraser removes".

Both the live preview and the commit call into this module, so they cannot
disagree: they are not two implementations that happen to produce the same
picture, they are the same function called twice.

What the eraser removes
-----------------------
We compute the painted OUTLINE of the ink and subtract the eraser's swept
capsule from it with real boolean path operations. That is the only way to
get the shape right: a ``FreehandStroke`` is a polyline plus ONE scalar
width and a ``HighlighterStroke`` is a centreline plus a height, so neither
can express a bite taken out of its own edge. The commit used to cut the
CENTRELINE and rebuild fragments at full width — which is why brushing the
edge of a thick stroke removed the whole width and severed it, while the
preview (which subtracted the true circular footprint) had shown a neat
crescent with the stroke still joined.

What the eraser STORES
----------------------
Subtraction gives a region, and OpenBoard stores exactly that: a stroke
there is a bag of ``UBGraphicsPolygonItem``s and ``eraseLineTo`` replaces
them with ``subtracted(...).simplified().toFillPolygons()``. We used to do
the same, and it cost us: the outline of a Bezier-smoothed stroke flattens
to hundreds of vertices, so every erase inflated a 40-point polyline into
several 300-point polygons. Erase a few times and the board carries an
order of magnitude more geometry than the user ever drew.

So we go one step further than OpenBoard. After subtracting, we try to
express the survivors as STROKES again — the sub-runs of the original
centreline the eraser did not cover, at the original width — and we keep
that answer only when we can PROVE it paints the same picture, by measuring
the symmetric difference against the true subtracted region
(``runs_are_faithful``). When it fits, an erased pen stroke stays a pen
stroke with a handful of points. When it cannot fit — a thin eraser
punching a hole through a fat highlighter, or a graze that bites the edge
without crossing the centreline — we fall back to polygons, because there
the polygon is the only honest answer.

Holes
-----
Subtracting a disc from the middle of a wide stroke leaves a region with a
HOLE, and ``PolygonObject`` — like OpenBoard's polygon item — stores a single
flat vertex list and cannot represent one. Qt solves this for us:
``QPainterPath.toFillPolygons()`` connects enclosed subpaths to their
container with a zero-width bridge, yielding hole-free polygons that fill
identically. ``toSubpathPolygons()`` would NOT do this and would leave the
hole filled in.

Cost
----
Nothing here builds the whole swept eraser path per frame. The live preview
folds ONE new capsule at a time into each affected object's running
remainder (``CanvasWidget.erase_region_for``), which is what keeps a long
drag linear instead of quadratic. ``EraserField`` does the same job for the
point-in-eraser queries the run splitter needs: a uniform grid, so testing
2000 centreline samples against a 1000-capsule drag is not 2 million
distance computations.

...and the remainder is not ONE path
------------------------------------
Folding capsules into a single whole-stroke remainder was still the most
expensive thing a drag did, and for a reason no amount of caching fixes:
``QPainterPath.subtracted`` costs what its operands cost, and a 200-sample
stroke's outline is ~2700 path elements. Every capsule reprocessed all of
them, so a 160-sample rub across one stroke spent 630 ms in booleans with
5 ms spikes on individual samples — well past a 120 Hz stylus's 8 ms budget
before anything was drawn.

OpenBoard does not have this problem, and it is worth being precise about
why. Its ink is not a stroke: ``drawLineTo`` emits ONE small
``UBGraphicsPolygonItem`` per input segment, so a stroke is a bag of little
polygons. ``eraseLineTo`` then asks the scene index for the items the
eraser's own capsule touches and subtracts from those alone — the operands
are tiny and stay tiny however long the drag runs, and items the capsule
covers outright are dropped without any boolean at all.

``InkRegion`` brings that here without giving up strokes as the storage
format. The painted region of an object is split ONCE, on first contact,
into small pieces whose union is exactly the original — a freehand outline
into arcs of the smoothed curve cut at Bezier midpoints (which is what makes
the split exact rather than approximate: see ``_freehand_fragment_paths``),
a highlighter ribbon into groups of the very quads ``_chisel_path`` already
builds it from. A capsule then only touches the two or three pieces it
actually reaches. Same picture — verified by rendering both and comparing
pixels, not by argument — for a fifth of the work and a third of the worst
sample:

    freehand w=6   630 ms / 4.7 ms peak  ->  100 ms / 1.6 ms peak
    freehand w=40 1124 ms / 5.1 ms peak  ->  223 ms / 1.8 ms peak
    highlighter    380 ms / 1.6 ms peak  ->  159 ms / 1.0 ms peak

The pieces overlap where they join, so they are painted as ONE
winding-filled path (``InkRegion.composite``) — a single fill operation, so
overlaps cannot double-darken a translucent highlighter. That is only sound
because a capsule is subtracted from EVERY piece whose box it touches, not
just the first: ink removed from one piece is therefore removed from every
piece that overlaps it, and no piece can fill in a hole another one has.

It is also, unexpectedly, more accurate
---------------------------------------
A randomised sweep of 60 strokes, checked against what ``_r_freehand``
actually paints rather than against another computation, put the pieces at
0 wrong and ``ink_outline_world`` — one ``createStroke`` over the whole
curve, then ``simplified()`` — at 2. On a scrubbed-out line (a stroke run
back and forth over itself a dozen times, which is what people do before
they reach for the eraser) the single path silently loses a chunk of its own
outline: 12,000 pixels of ink that Qt's boolean pass drops on the floor.

That was never a cosmetic problem, because the remainder is what the commit
STORES. Rubbing one end of such a stroke deleted the other end too — ink
that had never been under the eraser, gone, with an undo entry claiming it
was an erase. The pieces do not hit it because each is a short arc that
barely overlaps itself, so no single boolean operation is ever handed the
pathological input. ``test_pieces_hold_ink_the_single_outline_loses`` pins
the case.
"""
from __future__ import annotations

import math
from typing import List, Optional, Sequence, Tuple

from PySide6.QtCore import QPointF, Qt
from PySide6.QtGui import QPainterPath, QPainterPathStroker, QPolygonF

__all__ = [
    "capsule_path", "build_eraser_path", "ink_outline_world", "stroke_outline",
    "subtract", "path_to_polygons", "path_area", "segment_hits_box",
    "EraserField", "surviving_runs", "runs_to_outline", "runs_are_faithful",
    "InkRegion", "ink_region_world", "runs_outline_parts", "parts_overlap",
    "union_of", "ERASABLE_TYPES", "STROKE_TYPES",
]

# Object types the pixel eraser may cut. Imported media is deliberately absent
# — the pixel eraser is for ink only, and that protection is load-bearing (a
# stray rub must never destroy a PPT slide).
ERASABLE_TYPES = ("freehand", "highlighter", "polygon")

# ...of those, the ones that are a centreline plus a size, and so can be put
# back together as strokes instead of polygons.
STROKE_TYPES = ("freehand", "highlighter")


def capsule_path(ax: float, ay: float, bx: float, by: float,
                 r: float) -> QPainterPath:
    """ONE capsule, as a single closed contour: side, cap, side, cap.

    All float. An older preview built this as a ``QRegion`` from
    ``int``-truncated ellipses, and a QRegion is a stack of 1px scanline
    rectangles — which is exactly why erased edges came out stair-stepped.
    A QPainterPath has no such quantisation.

    The construction is ``UBGeometryUtils::lineToPolygon`` — two offset
    edges joined by two half-circle ``arcTo``s — and it is worth being
    faithful to it. The version this replaces built three separate paths
    (two ellipses and the body quad) and glued them with ``united()``, which
    is a full boolean sweep-line pass, twice, to produce a shape whose
    outline was known in closed form the whole time. It profiled at 0.33 ms
    a call against 0.02 ms for this, and it was called for every object the
    eraser touched on every pointer sample — 0.98 s of a 8.4 s drag, more
    than the actual subtractions it was feeding.

    Qt's ``arcTo`` takes degrees counter-clockwise on a y-down axis, hence
    the negated ``dy`` in the angle: it is ``QLineF::angle()``.
    """
    path = QPainterPath()
    if r <= 0:
        return path
    dx, dy = bx - ax, by - ay
    length = math.hypot(dx, dy)
    if length < 1e-9:
        path.addEllipse(QPointF(ax, ay), r, r)
        return path
    # Offset to the two sides of the segment.
    px, py = -dy / length * r, dx / length * r
    d = r * 2.0
    angle = math.degrees(math.atan2(-dy, dx))
    path.moveTo(ax - px, ay - py)
    path.lineTo(bx - px, by - py)
    path.arcTo(bx - r, by - r, d, d, 90.0 + angle, -180.0)
    path.lineTo(ax + px, ay + py)
    path.arcTo(ax - r, ay - r, d, d, -(90.0 - angle), -180.0)
    path.closeSubpath()
    return path


def build_eraser_path(segments: Sequence[tuple]) -> QPainterPath:
    """The whole swept region as one path.

    NOT on any hot path any more, and it must not go back on one: each union
    is against a path that keeps growing, so building this per frame made a
    long drag quadratic and visibly laggy. Both the preview and the commit
    now fold capsules into each object's own remainder one at a time instead
    (see ``CanvasWidget.erase_region_for``). Kept because tests and any
    one-shot caller still want a single path.
    """
    path = QPainterPath()
    for seg in segments:
        cap = capsule_path(*seg)
        if not cap.isEmpty():
            path = path.united(cap) if not path.isEmpty() else cap
    return path


def segment_hits_box(seg, min_x: float, min_y: float,
                     max_x: float, max_y: float) -> bool:
    """Could this capsule possibly touch that box? Cheap reject, no allocation."""
    ax, ay, bx, by, r = seg
    if max(ax, bx) + r < min_x or min(ax, bx) - r > max_x:
        return False
    if max(ay, by) + r < min_y or min(ay, by) - r > max_y:
        return False
    return True


def _pt_seg_dist_sq(px: float, py: float, ax: float, ay: float,
                    bx: float, by: float) -> float:
    """Squared distance from point (px,py) to segment (a,b)."""
    dx, dy = bx - ax, by - ay
    l2 = dx * dx + dy * dy
    if l2 <= 1e-12:
        ex, ey = px - ax, py - ay
        return ex * ex + ey * ey
    t = ((px - ax) * dx + (py - ay) * dy) / l2
    t = 0.0 if t < 0.0 else (1.0 if t > 1.0 else t)
    ex, ey = px - (ax + t * dx), py - (ay + t * dy)
    return ex * ex + ey * ey


class EraserField:
    """Point-in-eraser queries over a whole drag, in constant time per point.

    A drag is a list of capsules, and the run splitter asks "is this
    centreline sample covered?" for every sample of every stroke the eraser
    went near — thousands of queries against up to thousands of capsules.
    Done naively that is a nested loop with millions of iterations in Python,
    which would move the cost the preview no longer pays straight into the
    commit.

    So capsules are bucketed into a uniform grid, each registered in every
    cell its padded bounding box covers. A query then only has to look in the
    single cell containing the point: any capsule within ``r`` of that point
    necessarily has the point inside its padded box, so it is guaranteed to
    be in that cell's bucket.
    """

    __slots__ = ("segs", "r", "cell", "_grid")

    def __init__(self, segments: Sequence[tuple]):
        self.segs = [s for s in segments if s[4] > 0]
        self.r = max((s[4] for s in self.segs), default=0.0)
        # One capsule of radius r spans ~2r, so cells of 2r keep both the
        # bucket lists and the per-capsule insertion count small.
        self.cell = max(1.0, self.r * 2.0)
        grid: dict = {}
        inv = 1.0 / self.cell
        for idx, (ax, ay, bx, by, r) in enumerate(self.segs):
            gx0 = int(math.floor((min(ax, bx) - r) * inv))
            gx1 = int(math.floor((max(ax, bx) + r) * inv))
            gy0 = int(math.floor((min(ay, by) - r) * inv))
            gy1 = int(math.floor((max(ay, by) + r) * inv))
            for gx in range(gx0, gx1 + 1):
                for gy in range(gy0, gy1 + 1):
                    grid.setdefault((gx, gy), []).append(idx)
        self._grid = grid

    def __bool__(self) -> bool:
        return bool(self.segs)

    def covers(self, px: float, py: float) -> bool:
        """True when (px,py) lies inside the swept eraser region."""
        inv = 1.0 / self.cell
        bucket = self._grid.get((int(math.floor(px * inv)),
                                 int(math.floor(py * inv))))
        if not bucket:
            return False
        segs = self.segs
        for idx in bucket:
            ax, ay, bx, by, r = segs[idx]
            if _pt_seg_dist_sq(px, py, ax, ay, bx, by) <= r * r:
                return True
        return False


def _boundary(p_out: Tuple[float, float], p_in: Tuple[float, float],
              field: "EraserField", iters: int = 14) -> Tuple[float, float]:
    """Where the segment p_out -> p_in crosses the eraser edge.

    Bisection rather than an analytic capsule intersection: the eraser is a
    UNION of capsules, so the true boundary is whichever capsule happens to
    be nearest, and solving that analytically means solving all of them and
    taking a min. Fourteen halvings put the crossing within a sub-pixel
    fraction of a step, which is finer than the ink it is cutting, and the
    call only happens twice per cut — not per sample.

    Returns the last point known to be OUTSIDE, so the surviving run never
    reaches into the erased region.
    """
    ax, ay = p_out
    bx, by = p_in
    for _ in range(iters):
        mx, my = (ax + bx) * 0.5, (ay + by) * 0.5
        if field.covers(mx, my):
            bx, by = mx, my
        else:
            ax, ay = mx, my
    return ax, ay


def surviving_runs(pts: Sequence[Tuple[float, float]], field: "EraserField",
                   *, min_len: float = 0.0) -> List[List[Tuple[float, float]]]:
    """The sub-runs of a centreline the eraser did not cover.

    Detection runs on a DENSIFIED copy of the polyline. It has to: strokes
    are RDP-simplified when they are committed, so two consecutive samples
    can be far apart, and a capsule that dips between them would otherwise
    go unnoticed and the cut would be missed entirely.

    The runs handed back carry only the ORIGINAL samples plus the two
    interpolated boundary points per cut, though — not the dense ones. That
    is the whole point of the exercise: an erased stroke should come out with
    about as many vertices as it went in with, not with one per half-radius
    of its length.
    """
    n = len(pts)
    if n == 0:
        return []
    if n == 1:
        return [] if field.covers(pts[0][0], pts[0][1]) else [[tuple(pts[0])]]

    # Step fine enough that a capsule cannot hide between two probes: half a
    # radius means any disc of radius r is sampled at least twice across.
    step = max(0.75, field.r * 0.5)

    dense: List[Tuple[float, float, bool]] = []
    for i in range(n - 1):
        ax, ay = pts[i][0], pts[i][1]
        bx, by = pts[i + 1][0], pts[i + 1][1]
        dense.append((ax, ay, True))
        seg_len = math.hypot(bx - ax, by - ay)
        if seg_len > step:
            # Cap the subdivision of any one segment. A pathological stroke
            # (two samples a whole screen apart) must not be able to turn one
            # erase into a million probes.
            k = min(int(seg_len / step), 512)
            for j in range(1, k + 1):
                t = j / (k + 1.0)
                dense.append((ax + (bx - ax) * t, ay + (by - ay) * t, False))
    dense.append((pts[-1][0], pts[-1][1], True))

    covers = field.covers
    last = len(dense) - 1
    runs: List[List[Tuple[float, float]]] = []
    cur: Optional[List[Tuple[float, float]]] = None

    for i, (x, y, is_orig) in enumerate(dense):
        if covers(x, y):
            if cur is not None:
                # Leaving the ink: end the run exactly on the eraser edge
                # instead of at the last sample that happened to be outside.
                cur.append(_boundary((dense[i - 1][0], dense[i - 1][1]),
                                     (x, y), field))
                runs.append(cur)
                cur = None
            continue
        if cur is None:
            cur = []
            if i > 0:
                cur.append(_boundary((x, y),
                                     (dense[i - 1][0], dense[i - 1][1]), field))
            cur.append((x, y))
        elif is_orig or i == last:
            # Interior probe points are collinear filler between two real
            # samples — dropping them is exactly what keeps the vertex count
            # down, and it loses no shape.
            cur.append((x, y))
    if cur is not None:
        runs.append(cur)

    if min_len > 0.0:
        runs = [r for r in runs if _polyline_len(r) >= min_len or len(r) == 1]
    return [_dedupe(r) for r in runs if r]


def _polyline_len(pts: Sequence[Tuple[float, float]]) -> float:
    total = 0.0
    for i in range(len(pts) - 1):
        total += math.hypot(pts[i + 1][0] - pts[i][0], pts[i + 1][1] - pts[i][1])
    return total


def _dedupe(pts: Sequence[Tuple[float, float]],
            eps: float = 1e-4) -> List[Tuple[float, float]]:
    """Drop consecutive duplicates — an interpolated boundary can land on top
    of the sample next to it, and a zero-length segment makes the stroker
    emit a spurious cap."""
    out: List[Tuple[float, float]] = []
    for p in pts:
        if out and abs(p[0] - out[-1][0]) < eps and abs(p[1] - out[-1][1]) < eps:
            continue
        out.append((p[0], p[1]))
    return out


def stroke_outline(pts: Sequence[Tuple[float, float]], kind: str, size: float,
                   *, build_freehand_path, chisel_path) -> Optional[QPainterPath]:
    """The filled region a stroke of ``kind`` and ``size`` actually paints.

    Deliberately reuses the renderer's own path builders rather than
    re-deriving the shape. The painted curve does not pass through the raw
    samples — ``_build_freehand_path`` uses quadratic-Bezier midpoint
    smoothing with the samples as control points — so any independently
    derived outline would be subtly offset from the visible ink, and the
    erased edge would not line up with what the user sees.
    """
    if not pts:
        return None
    if kind == "freehand":
        width = max(0.1, size)
        if len(pts) == 1:
            dot = QPainterPath()
            dot.addEllipse(QPointF(pts[0][0], pts[0][1]), width / 2.0, width / 2.0)
            return dot
        stroker = QPainterPathStroker()
        stroker.setWidth(width)
        stroker.setCapStyle(Qt.RoundCap)     # must match _r_freehand's pen
        stroker.setJoinStyle(Qt.RoundJoin)
        return stroker.createStroke(build_freehand_path(pts)).simplified()
    if kind == "highlighter":
        if len(pts) < 2:
            return None
        ribbon = chisel_path(pts, max(1.0, size))
        return ribbon.simplified() if ribbon is not None else None
    return None


def _world_points(obj, to_world) -> List[Tuple[float, float]]:
    t = obj.transform
    pts = obj.points()
    if t.is_identity():
        return [(p[0], p[1]) for p in pts]
    return [to_world(p[0], p[1], t) for p in pts]


def ink_outline_world(obj, *, to_world, transform_scale,
                      build_freehand_path, chisel_path) -> Optional[QPainterPath]:
    """The FILLED region this ink object paints, in world space.

    Returns None for anything not erasable.
    """
    tn = obj.type_name
    t = obj.transform
    scale = transform_scale(t)

    if tn == "freehand":
        pts = _world_points(obj, to_world)
        return stroke_outline(pts, "freehand", obj.stroke_width * scale,
                              build_freehand_path=build_freehand_path,
                              chisel_path=chisel_path)

    if tn == "highlighter":
        pts = _world_points(obj, to_world)
        return stroke_outline(pts, "highlighter", obj.height * scale,
                              build_freehand_path=build_freehand_path,
                              chisel_path=chisel_path)

    if tn == "polygon":
        verts = obj.vertices()
        if len(verts) < 3:
            return None
        poly = QPolygonF([QPointF(*(to_world(v[0], v[1], t) if not t.is_identity()
                                    else (v[0], v[1]))) for v in verts])
        path = QPainterPath()
        path.addPolygon(poly)
        path.closeSubpath()
        return path.simplified()

    return None


def runs_outline_parts(runs: Sequence[Sequence[Tuple[float, float]]], kind: str,
                       size: float, *, build_freehand_path,
                       chisel_path) -> List[QPainterPath]:
    """One outline per run — what each stored stroke would paint on its own."""
    parts: List[QPainterPath] = []
    for run in runs:
        piece = stroke_outline(run, kind, size,
                               build_freehand_path=build_freehand_path,
                               chisel_path=chisel_path)
        if piece is not None and not piece.isEmpty():
            parts.append(piece)
    return parts


def parts_overlap(parts: Sequence[QPainterPath]) -> bool:
    """Do any two of these outlines cover the same pixel?

    This is the question that decides whether a TRANSLUCENT stroke may be
    split into several stored strokes. One stroke is painted in a single fill
    operation, so wherever it doubles back over itself the overlap does not
    darken. Cut that same stroke into six and each is a separate object with
    its own opacity, so every overlap darkens — and a highlighter scribbled
    back and forth to block something out is nothing BUT overlaps.

    ``runs_are_faithful`` cannot see this: it compares the runs' UNION against
    the true remainder, and the union is right. The shape matches perfectly
    and the picture still changes the moment the user lifts the pen.
    """
    n = len(parts)
    if n < 2:
        return False
    boxes = [p.boundingRect() for p in parts]
    for i in range(n):
        bi = boxes[i]
        for j in range(i + 1, n):
            if not bi.intersects(boxes[j]):
                continue
            if parts[i].intersects(parts[j]):
                return True
    return False


def runs_to_outline(runs: Sequence[Sequence[Tuple[float, float]]], kind: str,
                    size: float, *, build_freehand_path,
                    chisel_path) -> Optional[QPainterPath]:
    """What the surviving runs would paint if we stored them as strokes."""
    return union_of(runs_outline_parts(runs, kind, size,
                                       build_freehand_path=build_freehand_path,
                                       chisel_path=chisel_path))


def union_of(parts: Sequence[QPainterPath]) -> Optional[QPainterPath]:
    out = QPainterPath()
    for piece in parts:
        out = piece if out.isEmpty() else out.united(piece)
    return None if out.isEmpty() else out


# ---------------------------------------------------------------------------
# Fragmented ink — the OpenBoard idea, see the module docstring.
# ---------------------------------------------------------------------------

# How much of a stroke one piece covers, in world units. The eraser is
# 50 px across by default, so ~96 keeps a capsule touching two or three
# pieces: small enough that a subtraction is cheap, large enough that the
# per-piece bookkeeping and the box tests don't take over. Measured across
# 60 / 100 / 200 there is no meaningful difference in total drag cost, so
# this is chosen for the shape of the pieces rather than for a benchmark.
FRAGMENT_SPAN = 96.0

# ...and a hard stop on samples per piece, for ink so dense that ``span``
# would never be reached (a 200 Hz stylus held nearly still).
FRAGMENT_MAX_PTS = 24


def _mid(a, b):
    return (a[0] + b[0]) * 0.5, (a[1] + b[1]) * 0.5


def _freehand_fragment_paths(pts: Sequence[Tuple[float, float]],
                             span: float, max_pts: int) -> List[QPainterPath]:
    """The smoothed freehand curve, cut into arcs that rejoin EXACTLY.

    The cuts land on Bezier MIDPOINTS, not on samples, and that distinction
    is the whole correctness argument. ``_build_freehand_path`` emits

        p0 -> mid(p0,p1) -> [quad via p1] -> mid(p1,p2) -> [quad via p2] ...

    so a piece that starts at ``mid(p[i-1],p[i])`` and re-emits the quads
    from ``i`` reproduces that stretch bit for bit — the same reasoning
    ``_build_freehand_tail_path`` already relies on. Cutting at a SAMPLE
    instead looks like it works and does not: the piece would join its
    neighbour with two straight chords where the curve has one quad, and
    since a quad lies inside its control polygon the union would come out
    slightly FAT at every join. Sub-pixel on dense ink, which is exactly why
    it survives casual testing, and visible on an RDP-simplified stroke with
    a sharp corner — as a nub of ink the eraser passed over and left behind.
    """
    n = len(pts)
    out: List[QPainterPath] = []
    if n < 3:
        return out                      # caller handles the trivial cases

    # Quads are indexed 1..n-2; group them into runs of `span` world units.
    ranges: List[Tuple[int, int]] = []
    i = 1
    while i <= n - 2:
        acc = 0.0
        j = i
        while j <= n - 2 and acc < span and (j - i) < max_pts:
            acc += math.hypot(pts[j][0] - pts[j - 1][0],
                              pts[j][1] - pts[j - 1][1])
            j += 1
        ranges.append((i, j))
        i = j

    last = len(ranges) - 1
    for k, (qa, qb) in enumerate(ranges):
        path = QPainterPath()
        if k == 0:
            path.moveTo(pts[0][0], pts[0][1])
            m = _mid(pts[0], pts[1])
            path.lineTo(m[0], m[1])
        else:
            m = _mid(pts[qa - 1], pts[qa])
            path.moveTo(m[0], m[1])
        for q in range(qa, qb):
            m = _mid(pts[q], pts[q + 1])
            path.quadTo(pts[q][0], pts[q][1], m[0], m[1])
        if k == last:
            path.lineTo(pts[n - 1][0], pts[n - 1][1])
        out.append(path)
    return out


def _group_subpaths(path: QPainterPath, span: float) -> List[QPainterPath]:
    """Group a path's own subpaths into pieces of roughly ``span`` across.

    Used for the highlighter, and it is exact for free: ``_chisel_path``
    already builds the ribbon as one convex quad per centreline segment, so
    handing those very quads back in groups cannot change the region. Trying
    to rebuild the ribbon per chunk instead does change it — the chisel
    thins near-collinear samples first, and thinning a slice drops different
    samples than thinning the whole, which measured as real holes rather
    than antialiasing noise.
    """
    out: List[QPainterPath] = []
    cur: Optional[QPainterPath] = None
    x0 = y0 = x1 = y1 = 0.0
    for poly in path.toSubpathPolygons():
        r = poly.boundingRect()
        if cur is not None and (max(x1, r.right()) - min(x0, r.left()) > span or
                                max(y1, r.bottom()) - min(y0, r.top()) > span):
            out.append(cur)
            cur = None
        if cur is None:
            cur = QPainterPath()
            cur.setFillRule(Qt.WindingFill)
            x0, y0, x1, y1 = r.left(), r.top(), r.right(), r.bottom()
        cur.addPolygon(poly)
        x0 = min(x0, r.left()); y0 = min(y0, r.top())
        x1 = max(x1, r.right()); y1 = max(y1, r.bottom())
    if cur is not None:
        out.append(cur)
    return out


def _as_fragment(path: QPainterPath) -> Optional[list]:
    """``[path, min_x, min_y, max_x, max_y]``, or None if there's nothing there.

    The box is stored unpacked rather than as a QRectF because the reject
    test in ``InkRegion.subtract_capsule`` runs over every piece on every
    pointer sample, and four floats compare in the interpreter while
    ``QRectF.right()`` is a binding call.
    """
    if path is None or path.isEmpty():
        return None
    r = path.boundingRect()
    return [path, r.left(), r.top(), r.right(), r.bottom()]


class InkRegion:
    """An object's painted region, held as small pieces the eraser can bite.

    The pieces overlap where they join and are NOT kept disjoint — making
    them disjoint would need the boolean ops this class exists to avoid.
    They are painted as one winding-filled path instead (``composite``), so
    a single fill operation resolves the overlaps and a translucent
    highlighter cannot darken along its own seams.
    """

    __slots__ = ("frags", "hit", "_composite")

    def __init__(self, frags: List[list]):
        self.frags = frags
        self.hit = False            # has a capsule actually removed any ink?
        self._composite: Optional[QPainterPath] = None

    def __bool__(self) -> bool:
        return bool(self.frags)

    def is_empty(self) -> bool:
        return not self.frags

    def composite(self, clip=None) -> QPainterPath:
        """Every surviving piece in ONE winding-filled path.

        ``addPath`` only copies elements — no boolean op — so this is cheap
        enough to rebuild whenever the pieces change, which is what the live
        preview does.

        ``clip`` is an optional ``(min_x, min_y, max_x, max_y)`` in the same
        world space as the pieces, and it is how the preview stops paying for
        ink it is not repainting. A pointer sample dirties a small rectangle
        around the eraser, but the object being cut is held out of the scene
        cache in ONE piece, so the old code handed ``drawPath`` the whole
        stroke — every element of it — for Qt to clip away. That was 3.7 s of
        an 8.4 s drag. Pieces have boxes, so the ones outside the rectangle
        can simply be left out; every piece that covers a pixel inside it has
        a box that touches it, so what does get drawn is exactly right,
        overlaps and all.
        """
        if clip is None:
            if self._composite is None:
                path = QPainterPath()
                path.setFillRule(Qt.WindingFill)
                for f in self.frags:
                    path.addPath(f[0])
                self._composite = path
            return self._composite
        x0, y0, x1, y1 = clip
        path = QPainterPath()
        path.setFillRule(Qt.WindingFill)
        kept = 0
        for f in self.frags:
            if f[3] < x0 or f[1] > x1 or f[4] < y0 or f[2] > y1:
                continue
            path.addPath(f[0])
            kept += 1
        if kept == len(self.frags):
            # Nothing was culled — hand back the cached one so a full repaint
            # doesn't rebuild the same path every frame.
            return self.composite()
        return path

    def subtract_capsule(self, seg) -> bool:
        """Bite one capsule out. True if any piece changed.

        Three tiers, cheapest first — this ordering is where the speed comes
        from, not the subtraction itself:

        1. box reject, four float compares, no allocation;
        2. box entirely inside the capsule -> drop the piece outright. The
           capsule is convex, so a box whose four corners are all within
           ``r`` of the segment contains no point that isn't. This is
           OpenBoard's ``eraserPath.contains(itemPainterPath)`` shortcut, and
           it carries most of a rub along a thin stroke with no boolean at
           all;
        3. only now build the capsule path and subtract.
        """
        ax, ay, bx, by, r = seg
        if r <= 0:
            return False
        lo_x, hi_x = min(ax, bx) - r, max(ax, bx) + r
        lo_y, hi_y = min(ay, by) - r, max(ay, by) + r
        r2 = r * r
        cut: Optional[QPainterPath] = None
        out: List[list] = []
        changed = False

        for f in self.frags:
            if f[3] < lo_x or f[1] > hi_x or f[4] < lo_y or f[2] > hi_y:
                out.append(f)
                continue
            if (_pt_seg_dist_sq(f[1], f[2], ax, ay, bx, by) <= r2 and
                    _pt_seg_dist_sq(f[3], f[2], ax, ay, bx, by) <= r2 and
                    _pt_seg_dist_sq(f[3], f[4], ax, ay, bx, by) <= r2 and
                    _pt_seg_dist_sq(f[1], f[4], ax, ay, bx, by) <= r2):
                changed = True          # wholly covered — gone, no boolean
                continue
            if cut is None:
                cut = capsule_path(ax, ay, bx, by, r)
                if cut.isEmpty():
                    return False
            if not f[0].intersects(cut):
                out.append(f)
                continue
            changed = True
            res = f[0].subtracted(cut)
            if res.isEmpty():
                continue
            # Kept WHOLE, contours and all. Re-splitting looks right — a
            # capsule through the middle of a piece leaves two lobes, and
            # smaller pieces mean tighter boxes — but the cheap way to do it
            # is ``toFillPolygons``, and that measured SLOWER end to end (a
            # 220-sample rub on a 400-object board: 37.7 ms a sample against
            # 27.3), because the extra pieces cost more in box tests and in
            # ``composite`` than their tighter boxes ever saved.
            #
            # It is also the riskier of the two. ``toFillPolygons`` BRIDGES
            # holes — an enclosed contour is joined to its container, which
            # fills identically under that path's own fill rule but not
            # necessarily in the winding composite these pieces are painted
            # through, where a hole wound WITH its container stops being a
            # hole. ``subtracted`` orients outer contours and holes against
            # each other, so its output composites correctly as it stands.
            frag = _as_fragment(res)
            if frag is not None:
                out.append(frag)

        if changed:
            self.frags = out
            self.hit = True
            self._composite = None
        return changed


def ink_region_world(obj, *, to_world, transform_scale,
                     build_freehand_path, chisel_path,
                     span: float = FRAGMENT_SPAN) -> Optional["InkRegion"]:
    """``ink_outline_world`` again, but pre-split into erasable pieces.

    The union of the pieces is the same region ``ink_outline_world`` returns
    — that is checked by rendering both and comparing pixels, in
    ``test_pixel_eraser.py``.
    """
    tn = obj.type_name
    t = obj.transform
    scale = transform_scale(t)

    if tn in ("freehand", "highlighter"):
        pts = _world_points(obj, to_world)
        size = (obj.height if tn == "highlighter" else obj.stroke_width) * scale
        paths: List[QPainterPath] = []
        if tn == "freehand":
            width = max(0.1, size)
            if len(pts) >= 3:
                stroker = QPainterPathStroker()
                stroker.setWidth(width)
                stroker.setCapStyle(Qt.RoundCap)      # must match _r_freehand
                stroker.setJoinStyle(Qt.RoundJoin)
                # ``simplified()`` is not optional here, tempting as it looks:
                # it is 86% of the build cost (2.9 ms an object against 0.3),
                # the un-simplified outline subtracts to the same region, and
                # a pixel comparison of the two finds no difference. What it
                # does change is WHEN the normalisation happens. A cut
                # replaces a piece with ``toFillPolygons`` output, which IS
                # normalised — so an un-simplified piece would shift its
                # antialiasing along its whole length the first time anything
                # cut it, including the 96 world units of it that lie outside
                # the rectangle being repainted. The stale-pixel test catches
                # exactly that. Pay it up front and every piece renders the
                # same before and after.
                for arc in _freehand_fragment_paths(pts, span, FRAGMENT_MAX_PTS):
                    paths.append(stroker.createStroke(arc).simplified())
            else:
                whole = stroke_outline(pts, tn, size,
                                       build_freehand_path=build_freehand_path,
                                       chisel_path=chisel_path)
                if whole is not None:
                    paths.append(whole)
        else:
            if len(pts) < 2:
                return None
            ribbon = chisel_path(pts, max(1.0, size))
            if ribbon is None:
                return None
            # Simplify each GROUP, not the whole ribbon — for the reason
            # given in the freehand case above, and because doing it per
            # group is the same answer over much smaller operands.
            for grp in _group_subpaths(ribbon, span):
                paths.append(grp.simplified())
        frags = [f for f in (_as_fragment(p) for p in paths) if f is not None]
        return InkRegion(frags) if frags else None

    whole = ink_outline_world(obj, to_world=to_world,
                              transform_scale=transform_scale,
                              build_freehand_path=build_freehand_path,
                              chisel_path=chisel_path)
    frag = _as_fragment(whole)
    return InkRegion([frag]) if frag is not None else None


def subtract(outline: QPainterPath, eraser: QPainterPath) -> QPainterPath:
    """What survives. The single operation preview and commit share."""
    if outline is None or outline.isEmpty():
        return QPainterPath()
    if eraser is None or eraser.isEmpty():
        return outline
    return outline.subtracted(eraser)


def path_area(path: QPainterPath) -> float:
    """Total filled area, via the shoelace formula over the fill polygons.

    Written to keep the per-vertex work out of the interpreter loop. A
    flattened stroke outline runs to a thousand-odd vertices and this is
    called several times per object at commit, so the obvious
    ``for i in range(n): poly.at(i)`` version — two binding round-trips and
    an index wrap per vertex — measured 1.9 ms per path against 1.2 ms for
    this one. Same answer, 35% less of it.
    """
    if path is None or path.isEmpty():
        return 0.0
    total = 0.0
    for poly in path.toFillPolygons():
        pts = list(poly)
        if len(pts) < 3:
            continue
        xs = [p.x() for p in pts]
        ys = [p.y() for p in pts]
        total += abs(sum(map(lambda x0, y1, x1, y0: x0 * y1 - x1 * y0,
                             xs, ys[1:] + ys[:1],
                             xs[1:] + xs[:1], ys))) * 0.5
    return total


def runs_are_faithful(candidate: QPainterPath, remainder: QPainterPath,
                      *, half_size: float, cut_ends: int,
                      tol_frac: float = 0.04, cap_factor: float = 1.6,
                      max_frac: float = 0.12) -> bool:
    """Does storing the runs as strokes paint the same picture as the real
    subtraction? Measured, not assumed.

    The comparison is the SYMMETRIC difference, not a total-area match: two
    quite different shapes can happen to have equal area, and accepting one
    of those would silently move ink.

    Two ends can never agree exactly, and that is not a defect. Where the
    eraser cuts, the true subtraction ends on the eraser's circular boundary
    — a concave scoop — while a stroke ends in its own cap. The difference is
    a lune roughly the size of the stroke's half-width squared, so each cut
    end is allowed one of those and no more. A bite taken out of the SIDE of
    a stroke, or a hole punched through a band by a narrower eraser, is far
    bigger than that allowance, gets rejected here, and falls back to
    polygons — which is exactly the case polygons exist for.

    ``max_frac`` caps the whole thing, and it is not redundant. The cap
    allowance is an ABSOLUTE area, so on a fragment barely bigger than a cap
    it stops being a small correction and becomes a blank cheque: a
    randomised sweep caught an 84 px² highlighter sliver being accepted at
    62% off, because 1.6·half² came to 313 px² and forgave everything.
    Whatever the caps are worth, something that differs from the truth by
    more than a small fraction of the ink it is meant to BE is not the same
    picture, and a blob that small is better off as the polygon it exactly is.

    The value is the knee of the curve, measured over a randomised sweep of
    stroke widths and eraser radii: 0.18 keeps 87% of erases as strokes with
    a worst case 17.5% off, 0.12 keeps 81% at 11.9%, 0.06 keeps only 66%.
    Normal pen work — a thin nib under a much wider eraser — lands under 2%
    either way and is unaffected; what moves is thick ink cut by a wide
    eraser, where the round cap replacing the eraser's concave scoop is
    genuinely visible. Prefer showing the truth there.
    """
    if candidate is None or remainder is None or remainder.isEmpty():
        return False
    ref = path_area(remainder)
    if ref <= 0.0:
        return False
    allowance = min(tol_frac * ref
                    + cap_factor * max(0, cut_ends) * half_size * half_size,
                    max_frac * ref)
    missing = path_area(remainder.subtracted(candidate))   # ink we would lose
    if missing > allowance:
        return False                       # already over; don't price the rest
    extra = path_area(candidate.subtracted(remainder))     # ink we would add
    return (missing + extra) <= allowance


def path_to_polygons(path: QPainterPath,
                     min_area: float = 4.0) -> List[List[Tuple[float, float]]]:
    """Hole-free polygons for storage as PolygonObjects.

    ``toFillPolygons`` (NOT ``toSubpathPolygons``) is what makes holes
    representable: it bridges an enclosed subpath to its container so the
    filled result is identical while the vertex list stays flat.

    Slivers below ``min_area`` px² are dropped — boolean ops leave degenerate
    crumbs along cut edges, and each one would otherwise become a real scene
    object with an undo entry and a hit-test footprint.
    """
    if path is None or path.isEmpty():
        return []
    out: List[List[Tuple[float, float]]] = []
    for poly in path.toFillPolygons():
        n = poly.count()
        if n < 3:
            continue
        pts = [(poly.at(i).x(), poly.at(i).y()) for i in range(n)]
        # Drop a duplicated closing vertex; PolygonObject closes implicitly.
        if len(pts) > 3 and abs(pts[0][0] - pts[-1][0]) < 1e-6 \
                and abs(pts[0][1] - pts[-1][1]) < 1e-6:
            pts.pop()
        if len(pts) < 3:
            continue
        area = 0.0
        for i in range(len(pts)):
            x1, y1 = pts[i]
            x2, y2 = pts[(i + 1) % len(pts)]
            area += x1 * y2 - x2 * y1
        if abs(area) * 0.5 < min_area:
            continue
        out.append(pts)
    return out
