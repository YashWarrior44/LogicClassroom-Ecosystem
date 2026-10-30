"""
shape_geometry.py — what every shape looks like, in ONE place.

Every shape the Shapes panel offers is described here as a list of strokes,
and EVERYTHING that draws a shape draws it from this list:

  * the canvas, for a placed shape            (CanvasWidget._r_shape)
  * the live preview while it is being dragged (CanvasWidget._paint_shape_preview)
  * the icon on its tile in the Shapes panel   (shape_icon)
  * the Shapes button on the toolbar           (icon_loader.tool_icon)

so an icon cannot promise something the canvas does not draw. The old icons
were separately drawn artwork — gradients, fills, perspective the canvas
never produced — which is exactly the "misleading" the panel was criticised
for. An icon here is the canvas drawing, scaled down.

Geometry
--------
A shape is defined by the two points of the drag (``start``/``end``):

  * LINE shapes (lines and arrows) run from start to end;
  * every other shape fills the box the two points span, upright whichever
    way the drag went.

A stroke is ``(points, closed, style)`` with style one of SOLID, DASHED,
DOTTED or FILL (arrowheads).

3D solids
---------
Each solid is a real 3D model, projected the way a maths textbook draws it,
and its hidden edges are FOUND rather than hand-placed: an edge is solid when
either face beside it faces the viewer, dashed when both face away. Curved
solids are handled the same way along their rims, and their side silhouettes
are the true tangent lines (for a cone, the tangent from the apex to the base
ellipse — not the base's left and right extremes, which is the usual
shortcut and visibly wrong on a wide cone).

Two projections:

  * OBLIQUE (cabinet, 45 deg, half depth) for the box-like solids — cube,
    cuboid, triangular prism. Front face square-on, depth going up-right:
    the textbook cuboid.
  * ELEVATED (orthographic, looking slightly down) for everything with a
    round or pointed top — cylinder, cone, frustum, sphere, hemisphere,
    pyramids, octahedron, hexagonal prism. Circles become ellipses whose
    height is ``ELEV`` of their width.

The projected drawing is then fitted to the drag box. Shapes with one right
proportion (square, circle, cube, sphere, hemisphere) keep it while being
dragged — see ``constrain_end`` — the same way the circle always has.
"""
from __future__ import annotations

import math
from functools import lru_cache
from typing import Iterable, List, Sequence, Tuple

SOLID, DASHED, DOTTED, FILL = "solid", "dashed", "dotted", "fill"

Pt = Tuple[float, float]
Stroke = Tuple[Tuple[Pt, ...], bool, str]

# ── the catalogue ───────────────────────────────────────────────────────────
# (tool id, label). The order is the order of the panel's grid, row by row:
# 2D is 5 wide — lines / round / triangles & quads / polygons; 3D is 4 wide.
SHAPES_2D: List[Tuple[str, str]] = [
    ("line", "Line"), ("dashed_line", "Dashed Line"),
    ("dotted_line", "Dotted Line"), ("arrow", "Arrow"),
    ("double_arrow", "Double Arrow"),

    ("circle", "Circle"), ("ellipse", "Ellipse"), ("semicircle", "Semicircle"),
    ("square", "Square"), ("rect", "Rectangle"),

    ("triangle", "Triangle"), ("right_triangle", "Right Triangle"),
    ("parallelogram", "Parallelogram"), ("trapezium", "Trapezium"),
    ("rhombus", "Rhombus"),

    ("kite", "Kite"), ("pentagon", "Pentagon"), ("hexagon", "Hexagon"),
    ("octagon", "Octagon"), ("star", "Star"),
]
SHAPES_3D: List[Tuple[str, str]] = [
    ("cube", "Cube"), ("cuboid", "Cuboid"), ("cylinder", "Cylinder"),
    ("cone", "Cone"),

    ("sphere", "Sphere"), ("hemisphere", "Hemisphere"),
    ("frustum", "Frustum"), ("pyramid", "Pyramid"),

    ("tetrahedron", "Tetrahedron"), ("triangular_prism", "Triangular Prism"),
    ("hexagonal_prism", "Hexagonal Prism"), ("octahedron", "Octahedron"),
]
GRID_COLS_2D = 5
GRID_COLS_3D = 4

#: Drawn by the engine's own object types (LineObject, RectObject, ...).
NATIVE = ("line", "rect", "ellipse", "circle", "triangle", "arrow")
#: Everything else is an engine ShapeObject carrying one of these kinds.
SHAPE_KINDS = tuple(k for k, _ in SHAPES_2D + SHAPES_3D if k not in NATIVE)
#: Every tool id the shape tools answer to.
SHAPE_TOOLS = tuple(k for k, _ in SHAPES_2D + SHAPES_3D)
LABELS = dict(SHAPES_2D + SHAPES_3D)
IS_3D = frozenset(k for k, _ in SHAPES_3D)

#: Shapes drawn from start to end rather than inside a box.
LINE_KINDS = frozenset({"line", "dashed_line", "dotted_line", "arrow",
                        "double_arrow"})

# Dash patterns, in units of the pen width (Qt's convention).
DASH_PATTERN = (3.5, 2.6)
DOT_PATTERN = (0.01, 2.4)       # a zero-length dash + RoundCap = a round dot

# The projections.
ELEV = 0.30                       # ellipse height / width, elevated view
_SIN_P = ELEV
_COS_P = math.sqrt(1.0 - ELEV * ELEV)
_OBL_K = 0.5                      # cabinet: depth drawn at half length
_OBL_A = math.radians(45.0)


def arrow_head_dims(pen_width: float) -> Tuple[float, float]:
    """(head length, head width) — shared with the engine-native ArrowObject
    so the single and double arrow have the same heads."""
    return max(16.0, pen_width * 6), max(14.0, pen_width * 5)


# ═══════════════════════════════════════════════════════════════════════════
# 2D
# ═══════════════════════════════════════════════════════════════════════════

def _box(s: Pt, e: Pt):
    x0, x1 = sorted((s[0], e[0]))
    y0, y1 = sorted((s[1], e[1]))
    return x0, y0, x1, y1


def _ellipse_pts(cx, cy, rx, ry, t0, t1, *, rot=None) -> List[Pt]:
    """Points along an ellipse from angle t0 to t1 (radians, y DOWN, so
    positive angles run below the centre)."""
    span = abs(t1 - t0)
    n = max(8, int(96 * span / (2 * math.pi)))
    return [(cx + rx * math.cos(t0 + (t1 - t0) * i / n),
             cy + ry * math.sin(t0 + (t1 - t0) * i / n)) for i in range(n + 1)]


def _fit(pts: Sequence[Pt], box) -> List[Pt]:
    """Map points so their bounding box fills ``box`` (y already down)."""
    x0, y0, x1, y1 = box
    xs = [p[0] for p in pts]
    ys = [p[1] for p in pts]
    mx0, mx1, my0, my1 = min(xs), max(xs), min(ys), max(ys)
    sx = (x1 - x0) / (mx1 - mx0) if mx1 > mx0 else 0.0
    sy = (y1 - y0) / (my1 - my0) if my1 > my0 else 0.0
    return [(x0 + (x - mx0) * sx, y0 + (y - my0) * sy) for x, y in pts]


def _regular(n: int, rot: float, box, inner: float = 0.0) -> List[Pt]:
    pts = []
    for k in range(n * (2 if inner else 1)):
        a = rot + math.pi * 2 * k / (n * (2 if inner else 1))
        r = inner if (inner and k % 2) else 1.0
        pts.append((r * math.cos(a), r * math.sin(a)))
    return _fit(pts, box)


def _arrow_strokes(s: Pt, e: Pt, width: float, both: bool) -> List[Stroke]:
    dx, dy = e[0] - s[0], e[1] - s[1]
    length = math.hypot(dx, dy)
    if length < 1e-6:
        return []
    ux, uy = dx / length, dy / length
    hl, hw = arrow_head_dims(width)
    # Heads never overlap or overshoot on a short arrow.
    hl = min(hl, length * (0.45 if both else 0.9))
    hw = min(hw, hl * 1.2)
    out: List[Stroke] = []

    def head(tip, sign):
        bx, by = tip[0] - sign * ux * hl, tip[1] - sign * uy * hl
        nx, ny = -uy * hw / 2, ux * hw / 2
        out.append((((tip[0], tip[1]), (bx + nx, by + ny), (bx - nx, by - ny)),
                    True, FILL))
        return bx, by

    end_base = head(e, 1.0)
    start_base = head(s, -1.0) if both else s
    out.insert(0, ((tuple(start_base), tuple(end_base)), False, SOLID))
    return out


def _outline_2d(kind: str, s: Pt, e: Pt, width: float) -> List[Stroke]:
    if kind in ("line", "dashed_line", "dotted_line"):
        style = {"line": SOLID, "dashed_line": DASHED,
                 "dotted_line": DOTTED}[kind]
        return [((tuple(s), tuple(e)), False, style)]
    if kind in ("arrow", "double_arrow"):
        return _arrow_strokes(s, e, width, kind == "double_arrow")

    box = _box(s, e)
    x0, y0, x1, y1 = box
    w, h = x1 - x0, y1 - y0
    cx, cy = (x0 + x1) / 2, (y0 + y1) / 2

    def poly(pts, closed=True, style=SOLID):
        return (tuple(pts), closed, style)

    if kind in ("rect", "square"):
        return [poly([(x0, y0), (x1, y0), (x1, y1), (x0, y1)])]
    if kind in ("ellipse", "circle"):
        return [poly(_ellipse_pts(cx, cy, w / 2, h / 2, 0, 2 * math.pi))]
    if kind == "semicircle":
        arc = _ellipse_pts(cx, y1, w / 2, h, math.pi, 2 * math.pi)
        return [poly(arc)]                      # closed → the diameter
    if kind == "triangle":
        return [poly([(cx, y0), (x0, y1), (x1, y1)])]
    if kind == "right_triangle":
        m = min(0.16 * min(w, h), max(10.0, width * 4))
        return [poly([(x0, y0), (x0, y1), (x1, y1)]),
                poly([(x0, y1 - m), (x0 + m, y1 - m), (x0 + m, y1)],
                     closed=False)]
    if kind == "parallelogram":
        k = 0.25 * w
        return [poly([(x0 + k, y0), (x1, y0), (x1 - k, y1), (x0, y1)])]
    if kind == "trapezium":
        k = 0.22 * w
        return [poly([(x0 + k, y0), (x1 - k, y0), (x1, y1), (x0, y1)])]
    if kind == "rhombus":
        return [poly([(cx, y0), (x1, cy), (cx, y1), (x0, cy)])]
    if kind == "kite":
        k = y0 + 0.32 * h
        return [poly([(cx, y0), (x1, k), (cx, y1), (x0, k)])]
    if kind == "pentagon":
        return [poly(_regular(5, -math.pi / 2, box))]
    if kind == "hexagon":
        return [poly(_regular(6, 0.0, box))]
    if kind == "octagon":
        return [poly(_regular(8, math.pi / 8, box))]
    if kind == "star":
        return [poly(_regular(5, -math.pi / 2, box, inner=0.382))]
    return []


# ═══════════════════════════════════════════════════════════════════════════
# 3D
# ═══════════════════════════════════════════════════════════════════════════
# 3D convention: x right, y UP, z INTO the screen (front faces at small z).

def _sub(a, b): return (a[0] - b[0], a[1] - b[1], a[2] - b[2])
def _dot(a, b): return a[0] * b[0] + a[1] * b[1] + a[2] * b[2]


def _cross(a, b):
    return (a[1] * b[2] - a[2] * b[1], a[2] * b[0] - a[0] * b[2],
            a[0] * b[1] - a[1] * b[0])


# Projection: 3D point -> (X, Y_up), plus the direction TOWARD the viewer
# that decides which faces are seen.
def _oblique(p):
    return (p[0] + _OBL_K * p[2] * math.cos(_OBL_A),
            p[1] + _OBL_K * p[2] * math.sin(_OBL_A))


_V_OBLIQUE = (_OBL_K * math.cos(_OBL_A), _OBL_K * math.sin(_OBL_A), -1.0)


def _elevated(p):
    return (p[0], p[1] * _COS_P + p[2] * _SIN_P)


_V_ELEVATED = (0.0, _SIN_P, -_COS_P)


def _yaw(verts, deg):
    a = math.radians(deg)
    c, s = math.cos(a), math.sin(a)
    return [(x * c + z * s, y, -x * s + z * c) for x, y, z in verts]


def _polyhedron(verts, faces, project, view) -> List[Tuple[Tuple[Pt, Pt], str]]:
    """Edges of a CONVEX polyhedron, each solid or dashed by whether a face
    beside it faces the viewer."""
    n = len(verts)
    centre = tuple(sum(v[i] for v in verts) / n for i in range(3))
    visible = []
    for f in faces:
        a, b, c = verts[f[0]], verts[f[1]], verts[f[2]]
        nrm = _cross(_sub(b, a), _sub(c, a))
        fc = tuple(sum(verts[i][k] for i in f) / len(f) for k in range(3))
        if _dot(nrm, _sub(fc, centre)) < 0:        # make it point outward
            nrm = (-nrm[0], -nrm[1], -nrm[2])
        visible.append(_dot(nrm, view) > 1e-9)
    edge_faces = {}
    for fi, f in enumerate(faces):
        for i in range(len(f)):
            key = tuple(sorted((f[i], f[(i + 1) % len(f)])))
            edge_faces.setdefault(key, []).append(fi)
    out = []
    for (i, j), fl in edge_faces.items():
        seen = any(visible[k] for k in fl)
        out.append(((project(verts[i]), project(verts[j])),
                    SOLID if seen else DASHED))
    return out


def _rim(radius, y, side_normal, cap_visible, steps=144):
    """A horizontal circle, split into solid / dashed runs. A rim point is
    seen if the cap above or below it is seen, or the side surface there
    is."""
    runs: List[Tuple[List, str]] = []
    for i in range(steps + 1):
        phi = 2 * math.pi * i / steps
        p = (radius * math.cos(phi), y, radius * math.sin(phi))
        seen = cap_visible or (side_normal is not None
                               and _dot(side_normal(phi), _V_ELEVATED) > 1e-9)
        style = SOLID if seen else DASHED
        if runs and runs[-1][1] == style:
            runs[-1][0].append(_elevated(p))
        else:
            if runs:                              # share the joint point
                runs[-1][0].append(_elevated(p))
            runs.append(([_elevated(p)], style))
    return [(tuple(pts), False, st) for pts, st in runs if len(pts) > 1]


def _frustum(R, r, H) -> List[Stroke]:
    """A cone (r == 0), frustum or cylinder (r == R) standing on y = 0."""
    k = (R - r) / H

    def side_n(phi):
        return (math.cos(phi), k, math.sin(phi))

    strokes = _rim(R, 0.0, side_n, False)                    # base rim
    if r > 0:
        strokes += _rim(r, H, None, True)                    # top: cap seen
    # Silhouette generators: where the side surface turns edge-on.
    s0 = max(-1.0, min(1.0, (_SIN_P / _COS_P) * k))
    for phi in (math.asin(s0), math.pi - math.asin(s0)):
        a = (R * math.cos(phi), 0.0, R * math.sin(phi))
        b = (r * math.cos(phi), H, r * math.sin(phi))
        strokes.append(((_elevated(a), _elevated(b)), False, SOLID))
    return strokes


@lru_cache(maxsize=None)
def _solid_unit(kind: str) -> Tuple[Stroke, ...]:
    """The solid in model space, projected, y UP. Fitted to the box later."""
    if kind in ("cube", "cuboid"):
        v = [(x, y, z) for z in (0, 1) for y in (0, 1) for x in (0, 1)]
        faces = [(0, 1, 3, 2), (4, 5, 7, 6), (0, 1, 5, 4), (2, 3, 7, 6),
                 (0, 2, 6, 4), (1, 3, 7, 5)]
        edges = _polyhedron(v, faces, _oblique, _V_OBLIQUE)
    elif kind == "triangular_prism":
        tri = [(0.0, 0.0), (1.0, 0.0), (0.5, 0.866)]
        v = [(x, y, 0.0) for x, y in tri] + [(x, y, 1.3) for x, y in tri]
        faces = [(0, 1, 2), (3, 4, 5), (0, 1, 4, 3), (1, 2, 5, 4),
                 (2, 0, 3, 5)]
        edges = _polyhedron(v, faces, _oblique, _V_OBLIQUE)
    elif kind == "pyramid":
        base = _yaw([(-1, 0, -1), (1, 0, -1), (1, 0, 1), (-1, 0, 1)], 32)
        v = base + [(0.0, 1.9, 0.0)]
        faces = [(0, 1, 2, 3), (0, 1, 4), (1, 2, 4), (2, 3, 4), (3, 0, 4)]
        edges = _polyhedron(v, faces, _elevated, _V_ELEVATED)
    elif kind == "tetrahedron":
        # A base CORNER toward the viewer (270 deg is the front), so two
        # faces show and only the back base edge is hidden — the textbook
        # view. A base EDGE at the front shows one face and reads flat.
        base = [(math.cos(a), 0.0, math.sin(a))
                for a in (math.radians(d) for d in (270, 30, 150))]
        v = _yaw(base, 14) + [(0.0, 1.55, 0.0)]
        faces = [(0, 1, 2), (0, 1, 3), (1, 2, 3), (2, 0, 3)]
        edges = _polyhedron(v, faces, _elevated, _V_ELEVATED)
    elif kind == "octahedron":
        ring = _yaw([(1, 0, 0), (0, 0, 1), (-1, 0, 0), (0, 0, -1)], 15)
        v = ring + [(0.0, 1.25, 0.0), (0.0, -1.25, 0.0)]
        faces = [(0, 1, 4), (1, 2, 4), (2, 3, 4), (3, 0, 4),
                 (0, 1, 5), (1, 2, 5), (2, 3, 5), (3, 0, 5)]
        edges = _polyhedron(v, faces, _elevated, _V_ELEVATED)
    elif kind == "hexagonal_prism":
        ring = [(math.cos(a), math.sin(a))
                for a in (math.radians(60 * k) for k in range(6))]
        v = [(x, 0.0, z) for x, z in ring] + [(x, 1.7, z) for x, z in ring]
        faces = [tuple(range(6)), tuple(range(6, 12))] + \
            [(k, (k + 1) % 6, 6 + (k + 1) % 6, 6 + k) for k in range(6)]
        edges = _polyhedron(v, faces, _elevated, _V_ELEVATED)
    elif kind == "cylinder":
        return tuple(_frustum(1.0, 1.0, 1.9))
    elif kind == "cone":
        return tuple(_frustum(1.0, 0.0, 2.0))
    elif kind == "frustum":
        return tuple(_frustum(1.0, 0.55, 1.4))
    elif kind == "sphere":
        circle = _ellipse_pts(0, 0, 1, 1, 0, 2 * math.pi)
        out: List[Stroke] = [(tuple(circle), True, SOLID)]
        out += _rim(1.0, 0.0, lambda phi: (math.cos(phi), 0.0, math.sin(phi)),
                    False)
        return tuple(out)
    elif kind == "hemisphere":
        dome = [(math.cos(a), math.sin(a))
                for a in (math.pi * i / 72 for i in range(73))]
        out = [(tuple(dome), False, SOLID)]
        out += _rim(1.0, 0.0, lambda phi: (math.cos(phi), 0.0, math.sin(phi)),
                    False)
        return tuple(out)
    else:
        return ()
    return tuple((pts, False, style) for pts, style in edges)


def _outline_3d(kind: str, s: Pt, e: Pt) -> List[Stroke]:
    unit = _solid_unit(kind)
    if not unit:
        return []
    allp = [p for pts, _c, _s in unit for p in pts]
    xs = [p[0] for p in allp]
    ys = [p[1] for p in allp]
    mx0, mx1, my0, my1 = min(xs), max(xs), min(ys), max(ys)
    x0, y0, x1, y1 = _box(s, e)
    sx = (x1 - x0) / (mx1 - mx0) if mx1 > mx0 else 0.0
    sy = (y1 - y0) / (my1 - my0) if my1 > my0 else 0.0
    # y UP in model space, DOWN on the canvas.
    return [(tuple((x0 + (x - mx0) * sx, y1 - (y - my0) * sy) for x, y in pts),
             closed, style) for pts, closed, style in unit]


@lru_cache(maxsize=None)
def natural_aspect(kind: str) -> float:
    """Width / height of a 3D solid's drawing as modelled."""
    unit = _solid_unit(kind)
    allp = [p for pts, _c, _s in unit for p in pts]
    w = max(p[0] for p in allp) - min(p[0] for p in allp)
    h = max(p[1] for p in allp) - min(p[1] for p in allp)
    return w / h if h > 0 else 1.0


#: Shapes with one right proportion, kept while dragging.
_LOCKED = {"circle": 1.0, "square": 1.0, "semicircle": 2.0}
_LOCKED_3D = ("cube", "sphere", "hemisphere")


def locked_aspect(kind: str):
    if kind in _LOCKED:
        return _LOCKED[kind]
    if kind in _LOCKED_3D:
        return natural_aspect(kind)
    return None


def constrain_end(kind: str, s: Pt, e: Pt) -> Pt:
    """Snap the drag's end so a proportion-locked shape keeps its
    proportion. Grows from the start point in the drag's direction, sized
    by the larger of the two movements — the circle's rule."""
    a = locked_aspect(kind)
    if a is None:
        return e
    dx, dy = e[0] - s[0], e[1] - s[1]
    w = max(abs(dx), abs(dy) * a)
    h = w / a
    return (s[0] + (w if dx >= 0 else -w), s[1] + (h if dy >= 0 else -h))


# ═══════════════════════════════════════════════════════════════════════════
# Public
# ═══════════════════════════════════════════════════════════════════════════

def outline(kind: str, start: Pt, end: Pt, width: float = 4.0) -> List[Stroke]:
    """Every stroke of ``kind`` dragged from ``start`` to ``end``."""
    start = (float(start[0]), float(start[1]))
    end = (float(end[0]), float(end[1]))
    if not all(math.isfinite(v) for v in start + end):
        return []
    if kind in IS_3D:
        return _outline_3d(kind, start, end)
    return _outline_2d(kind, start, end, width)


def paint(painter, kind: str, start: Pt, end: Pt, color, width: float) -> None:
    """Draw a shape with a QPainter — the one drawing routine every caller
    uses. ``color`` is a QColor."""
    from PySide6.QtCore import QPointF, Qt
    from PySide6.QtGui import QBrush, QPainterPath, QPen

    width = max(0.5, float(width))
    for pts, closed, style in outline(kind, start, end, width):
        if len(pts) < 2:
            continue
        path = QPainterPath(QPointF(*pts[0]))
        for p in pts[1:]:
            path.lineTo(QPointF(*p))
        if closed:
            path.closeSubpath()
        if style == FILL:
            painter.setPen(QPen(color, max(1.0, width * 0.5), Qt.SolidLine,
                                Qt.RoundCap, Qt.RoundJoin))
            painter.setBrush(QBrush(color))
        else:
            pen = QPen(color, width, Qt.SolidLine, Qt.RoundCap, Qt.RoundJoin)
            if style == DASHED:
                pen.setCapStyle(Qt.FlatCap)
                pen.setDashPattern(list(DASH_PATTERN))
            elif style == DOTTED:
                pen.setDashPattern(list(DOT_PATTERN))
            painter.setPen(pen)
            painter.setBrush(Qt.NoBrush)
        painter.drawPath(path)


#: The drawing an icon is made from: a shape in a 100-unit box with a 4.5
#: unit pen, scaled down — so its proportions, arrowheads and dashes are the
#: canvas's own.
_ICON_BOX = 100.0
_ICON_PEN = 4.5


def icon_strokes_box(kind: str) -> Tuple[Pt, Pt]:
    """Start / end of the reference drawing an icon is scaled from."""
    m = 8.0
    if kind in LINE_KINDS:
        return (m, _ICON_BOX - m), (_ICON_BOX - m, m)        # rising diagonal
    a = locked_aspect(kind) or (natural_aspect(kind)
                                if kind in IS_3D and kind != "cuboid" else None)
    w = h = _ICON_BOX - 2 * m
    if a is not None:
        if a >= 1:
            h = w / a
        else:
            w = h * a
    elif kind in ("rect", "ellipse", "parallelogram", "trapezium"):
        h = w * 0.66                        # these read best landscape
    elif kind == "cuboid":
        h = w * 0.62                        # or it is just the cube again
    x0, y0 = (_ICON_BOX - w) / 2, (_ICON_BOX - h) / 2
    return (x0, y0), (x0 + w, y0 + h)


def _to_qcolor(color):
    """Theme colours are CSS strings — "rgba(245, 245, 247, 0.96)" among
    them, which QColor() cannot parse and turns into black."""
    from PySide6.QtGui import QColor
    if isinstance(color, QColor):
        return color
    try:
        from ui.theme.theme_manager import theme_mgr
        q = theme_mgr.qcolor(color)
        if q.isValid():
            return q
    except Exception:
        pass
    q = QColor(str(color))
    return q if q.isValid() else QColor(255, 255, 255)


def shape_icon(kind: str, size: int, color: str):
    """A QPixmap of ``kind`` at ``size`` logical pixels, drawn in
    ``color`` — single-weight lines, exactly as the canvas draws it."""
    from PySide6.QtCore import Qt
    from PySide6.QtGui import QColor, QGuiApplication, QPainter, QPixmap

    screen = QGuiApplication.primaryScreen()
    dpr = max(1.0, screen.devicePixelRatio() if screen else 1.0)
    pm = QPixmap(int(size * dpr), int(size * dpr))
    pm.setDevicePixelRatio(dpr)
    pm.fill(Qt.transparent)
    p = QPainter(pm)
    p.setRenderHint(QPainter.Antialiasing)
    scale = size / _ICON_BOX
    p.scale(scale, scale)
    s, e = icon_strokes_box(kind)
    # Keep the line about 2 px on screen whatever the icon size.
    pen = max(_ICON_PEN, 2.0 / scale)
    paint(p, kind, s, e, _to_qcolor(color), pen)
    p.end()
    return pm


def all_points(strokes: Iterable[Stroke]) -> List[Pt]:
    return [pt for pts, _c, _s in strokes for pt in pts]
