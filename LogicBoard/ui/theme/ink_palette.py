"""
ink_palette.py — the ink colours LogicBoard offers, in one place.

Shared by the pen panel (8 x 3 grid) and the shapes panel (one column), so
a shape and the ink beside it are picked from the same shades.

Not the stock primaries. Every board app ships the same seven Material
swatches — #F44336 red, #2196F3 blue, #4CAF50 green — and a board drawn
with them looks like every other board. These are each a deliberate shade
of the colour a teacher reaches for: a coral rather than a fire-engine red,
an azure rather than the system blue, a sunflower rather than a lemon. Same
job, recognisably LogicBoard.

Each still has to work as INK: legible on the dark slate and on the white
page, distinct from its neighbours at a glance, and printable. The three
rows keep the old roles:

    row 1  vivid  — making a point
    row 2  deep   — body text and diagrams
    row 3  soft   — marking over dark ink without hiding it
"""
from __future__ import annotations

from PySide6.QtGui import QColor

__all__ = ["INK_COLORS", "INK_NAMES", "GRID_COLS", "GRID_ROWS",
           "SHAPE_COLORS", "DEFAULT_SHAPE_COLOR"]

GRID_COLS = 8
GRID_ROWS = 3

# (name, rgb). The name is the tooltip.
_INKS = [
    # ── vivid ──────────────────────────────────────────────────────────
    ("Chalk",       (246, 244, 238)),
    ("Sunflower",   (255, 201,  60)),
    ("Tangerine",   (255, 138,  61)),
    ("Coral",       (255,  90,  95)),
    ("Raspberry",   (232,  63, 140)),
    ("Orchid",      (176,  98, 240)),
    ("Azure",       ( 92, 162, 255)),
    ("Lagoon",      ( 24, 196, 196)),
    # ── deep ───────────────────────────────────────────────────────────
    ("Ink",         ( 22,  24,  32)),
    ("Matcha",      (150, 200,  70)),
    ("Jade",        ( 22, 168, 104)),
    ("Pine",        ( 26, 102,  72)),
    ("Indigo",      ( 66,  64, 190)),
    ("Merlot",      (140,  35,  72)),
    ("Cocoa",       (138,  90,  60)),
    ("Slate",       (104, 120, 142)),
    # ── soft ───────────────────────────────────────────────────────────
    ("Blush",       (255, 184, 198)),
    ("Apricot",     (255, 211, 165)),
    ("Butter",      (255, 240, 168)),
    ("Mint",        (184, 240, 210)),
    ("Ice",         (185, 220, 255)),
    ("Lilac",       (217, 198, 255)),
    ("Mist",        (211, 219, 229)),
    # (the 24th cell of the pen grid is the custom picker)
]

INK_COLORS = [QColor(*rgb) for _n, rgb in _INKS]
INK_NAMES = [n for n, _rgb in _INKS]

assert len(INK_COLORS) == GRID_COLS * GRID_ROWS - 1

# The shapes panel's column: the strongest, most diagram-friendly of the
# inks above — the ones you would draw an axis or a triangle in. Ten, plus
# the custom cell, spread down the full height of the panel.
_SHAPE_NAMES = ("Chalk", "Ink", "Coral", "Tangerine", "Sunflower", "Jade",
                "Lagoon", "Azure", "Orchid", "Raspberry")
SHAPE_COLORS = [(n, QColor(*dict(_INKS)[n])) for n in _SHAPE_NAMES]
DEFAULT_SHAPE_COLOR = QColor(*dict(_INKS)["Azure"])
