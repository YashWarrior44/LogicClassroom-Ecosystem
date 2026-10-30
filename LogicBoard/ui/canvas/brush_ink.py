"""
brush_ink.py — what a brush stroke IS, in one place.

The brush pen draws the same chisel ribbon as the highlighter, with two
differences: it is fully opaque, and its nib is TILTED, so a stroke swells
and thins with its direction the way a broad-nib calligraphy pen does.

HOW A BRUSH STROKE IS STORED
============================
As a ``HighlighterStroke`` with ``opacity == 1.0``.

That deserves an explanation, because it is a decision about persistence and
it constrains the future.

The C++ engine defines a fixed set of object types — FreehandStroke,
HighlighterStroke, shapes, images — and a stroke is committed by constructing
one of them. There is no generic "some other kind of stroke", so a brush
either reuses an existing type or the engine grows a new one and every board
file, every renderer and the prebuilt ``logicboard_engine`` binary change with
it.

HighlighterStroke is already exactly the right shape for this: a point list
plus a nib ``height``, rendered as a swept chisel ribbon. The ONLY thing a
brush adds is "draw it opaque, and tilt the nib". Opacity is already a stored,
round-tripping field, and the highlighter's is a hard-coded 75/255 with no
setter and no UI anywhere in the app — so "fully opaque" is a value the
highlighter can never produce, and it identifies a brush stroke exactly.

The nib angle is a constant rather than per-stroke data. Storing it would mean
finding a spare field, and there isn't an honest one: ``stroke_width`` looks
free (the chisel renderer ignores it) but defaults to 3.0, so every highlighter
ever saved would come back claiming a 3-degree tilt.

WHAT WOULD BREAK THIS
=====================
Giving the highlighter a user-adjustable opacity. If a highlighter can be set
to fully opaque, it becomes indistinguishable from a brush and old boards
would start rendering highlighter strokes tilted.

That is a plausible feature to want, so it is pinned by a test rather than
left to whoever reads this next: ``test_brush.py`` asserts the highlighter's
alpha stays below the opaque threshold. If that test fails, the fix is not to
loosen the threshold — it is to add a real BrushStroke type to the engine.
"""

from __future__ import annotations

__all__ = [
    "BRUSH_NIB_ANGLE_DEG",
    "BRUSH_OPACITY",
    "OPAQUE_THRESHOLD",
    "is_brush_stroke",
    "nib_angle_for",
    "MIN_NIB_THICKNESS",
    "NIB_THICKNESS_RATIO",
    "nib_dimensions",
]

# How far the nib is rotated from upright, in degrees.
#
# 45 from upright: the nib's long edge runs at exactly 45 degrees above the
# +x axis — up and to the right, the way a broad nib sits in a right hand.
# Diagonals then carry the whole contrast: a stroke pulled down-right is the
# full nib wide, one lifted up-right is a hairline, and horizontals and
# verticals sit in between at the same weight. (It was 60 — 30 degrees above
# horizontal — and 40 before that.) At 0
# there is no effect at all (that is the highlighter).
BRUSH_NIB_ANGLE_DEG = 45.0

# Brush strokes are committed with exactly this opacity, and it is what
# distinguishes them on the way back in.
BRUSH_OPACITY = 1.0

# Anything at or above this counts as "fully opaque". Not an equality test on
# 1.0: opacity is a float that round-trips through JSON, and a value that
# comes back as 0.9999999 is still a brush stroke. The highlighter's 75/255 is
# 0.294, nowhere near this, so the gap is enormous either way.
OPAQUE_THRESHOLD = 0.99


def is_brush_stroke(obj) -> bool:
    """True if this highlighter-type object was drawn with the brush.

    Callers should already know the object is a highlighter stroke; this only
    answers the opaque-or-not half.
    """
    try:
        return float(obj.opacity) >= OPAQUE_THRESHOLD
    except (AttributeError, TypeError, ValueError):
        return False


def nib_angle_for(obj) -> float:
    """Nib tilt in degrees for a committed chisel stroke — the brush angle
    for a brush, upright for a highlighter."""
    return BRUSH_NIB_ANGLE_DEG if is_brush_stroke(obj) else 0.0


# ── the nib's physical size ────────────────────────────────────────────────
#
# A chisel nib is a bar: long across one axis, thin across the other. The
# long axis is the tool size the user picked. The thin axis is the part that
# needed thought, because it decides how a stroke looks at the SMALL sizes
# people actually write at.
#
# The thin axis has a floor, because a mathematically zero-thickness nib
# would make a stroke travelling along its own axis vanish, and a real chisel
# still has some thickness. That floor used to be 1.5px, and 1.5px is
# precisely the wrong number: it is wider than one pixel, so it always
# straddles two pixel rows, and antialiasing splits the ink between them.
# Neither row ever reaches full black. The stroke reads as grey and soft —
# "not crisp" — right where a 3px pen next to it is solid.
#
# 2.0 is the smallest floor that can land as a solid band, and at a 3px tool
# size it is the difference between a washed-out hairline and ink.
MIN_NIB_THICKNESS = 2.0

# Above the floor the nib thickens with the tool, so a big highlighter keeps
# a chisel's proportions instead of a wire-thin edge.
NIB_THICKNESS_RATIO = 0.06


def nib_dimensions(height: float) -> tuple[float, float]:
    """``(half_length, half_thickness)`` of the nib bar for a tool size.

    Halves, because everything downstream offsets from the centre.

    The clamp at the end is what keeps small sizes sane: with a 2.0px floor
    a 3px tool would otherwise get a nib 3 long and 2 thick, and by 2px it
    would be THICKER than it is long — the calligraphic axes would swap over
    and the stroke would swell where it should thin. Capping thickness at the
    length makes the nib degenerate gracefully to square, which just draws a
    uniform stroke, which is the right answer for a nib too small to have a
    direction.
    """
    try:
        h = float(height)
    except (TypeError, ValueError):
        h = 1.0
    if not (h > 0.0) or h != h:                 # non-positive, or NaN
        h = 1.0
    half_len = max(0.5, h * 0.5)
    thickness = max(MIN_NIB_THICKNESS, h * NIB_THICKNESS_RATIO)
    thickness = min(thickness, half_len * 2.0)
    return half_len, thickness * 0.5
