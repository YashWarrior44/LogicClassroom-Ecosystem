"""
test_icon_sharpness.py — icons are rasterised at the display's pixel ratio.

Runs under pytest, and also standalone (``python tests/python/test_icon_sharpness.py``)
because the repo's pytest install is currently broken on Python 3.11.

What is worth protecting here
-----------------------------
Every size in this app is a LOGICAL pixel count — ``size_manager`` scales for
resolution and leaves the DPI factor to Qt, which its module docstring states
outright. So a pixmap allocated at ``size × size`` with no device-pixel ratio
holds a bitmap of logical extent that Qt then STRETCHES to physical extent
when it paints. At 125 % Windows scaling that is 28 px blown up to 35; on a
4K board at 150 % it is 28 to 42. That upscale is the blur, and nothing about
the SVG source or the icon size hides it — the only fix is to rasterise at
the ratio the screen actually has.

The tests below run under a forced 2x screen, because at 1x the bug is
invisible by construction: the wrong code and the right code produce byte
identical output, which is exactly why this survived on a dev machine and
only showed up on the board.
"""
import os
import sys

os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")
# MUST be set before the QGuiApplication exists — Qt reads it at screen
# construction. A 2x screen is what a 4K board at 200 % reports.
os.environ.setdefault("QT_SCREEN_SCALE_FACTORS", "2")

sys.path.insert(0, os.path.abspath(os.path.join(os.path.dirname(__file__),
                                                "..", "..")))

from PySide6.QtWidgets import QApplication
from PySide6.QtGui import QGuiApplication

_app = QApplication.instance() or QApplication([])

from ui.util.icon_loader import (tool_icon, icon_target_dpr, blank_icon_pixmap,
                                 ICONS_DIR)

LOGICAL = 28
_DPR = QGuiApplication.primaryScreen().devicePixelRatio()

# Every icon the toolbar and the panels resolve by name.
NAMES = sorted(p.stem for p in ICONS_DIR.glob("*.svg"))


def _skip_reason():
    if _DPR <= 1.0:
        return ("this environment reports a 1x screen, so the defect cannot "
                "be observed — QT_SCREEN_SCALE_FACTORS had no effect")
    return None


def test_the_test_environment_is_actually_hi_dpi():
    """Guard the guard. Every assertion below is vacuous at 1x, so if the
    forced scale factor ever stops working these tests must fail loudly
    rather than quietly pass forever."""
    assert _skip_reason() is None, _skip_reason()
    assert icon_target_dpr() == _DPR, \
        f"loader targets {icon_target_dpr()}x, screen is {_DPR}x"


def test_every_tool_icon_is_backed_at_the_device_ratio():
    """The whole bug in one assertion: a 28 px icon on a 2x screen must be a
    56 px bitmap, not a 28 px one stretched to fill 56."""
    assert NAMES, "no SVGs found — the icon folder moved?"
    soft = []
    for name in NAMES:
        pm = tool_icon(name, LOGICAL).pixmap(LOGICAL, LOGICAL)
        if pm.devicePixelRatio() < _DPR or pm.width() < int(LOGICAL * _DPR):
            soft.append(f"{name} ({pm.width()}px @ {pm.devicePixelRatio()}x)")
    assert not soft, "icons rasterised below the screen ratio: " + ", ".join(soft)


def test_the_icon_still_reports_the_size_that_was_asked_for():
    """Sharpness must not come at the cost of the icon growing. Layout asks
    in logical units and has to keep getting logical units back."""
    for name in NAMES[:6]:
        pm = tool_icon(name, LOGICAL).pixmap(LOGICAL, LOGICAL)
        logical_w = pm.width() / pm.devicePixelRatio()
        assert abs(logical_w - LOGICAL) < 1.0, \
            f"{name} occupies {logical_w:.1f} logical px, expected {LOGICAL}"


def test_the_glyph_fills_its_box_and_is_not_double_scaled():
    """A QPainter on a ratio-tagged pixmap already scales coordinates, so
    rendering into the device-sized viewport scales twice: the icon overflows
    and gets clipped to its top-left corner. Check the ink lands where it
    should — roughly centred, well inside the edges, and using most of the box.
    """
    for name in NAMES:
        pm = tool_icon(name, LOGICAL, "#ffffff").pixmap(LOGICAL, LOGICAL)
        img = pm.toImage()
        w, h = img.width(), img.height()
        xs, ys = [], []
        for y in range(h):
            for x in range(w):
                if (img.pixel(x, y) >> 24) & 0xFF:
                    xs.append(x); ys.append(y)
        assert xs, f"{name} rendered nothing at all"
        span_x = (max(xs) - min(xs) + 1) / w
        span_y = (max(ys) - min(ys) + 1) / h
        # ONE axis, not both: some icons are legitimately linear. `minimize`
        # is a single horizontal bar 40x8 in a 64 box, so demanding height
        # too would fail a correctly drawn glyph.
        assert max(span_x, span_y) > 0.5, \
            (f"{name} ink covers only {span_x:.0%}x{span_y:.0%} of its box — "
             f"rendered too small for the target")
        # A double-scaled render puts the icon at 2x in a 1x box: the visible
        # part is the TOP-LEFT quadrant, so the ink runs into the far edge and
        # its centre of mass sits well up and left of the middle.
        cx = (min(xs) + max(xs)) / 2.0 / w
        cy = (min(ys) + max(ys)) / 2.0 / h
        assert 0.3 < cx < 0.7 and 0.3 < cy < 0.7, \
            f"{name} ink is centred at ({cx:.0%}, {cy:.0%}) — not in its box"


def test_a_blank_icon_pixmap_is_also_ratio_backed():
    """The shared helper the other modules build on — the toolbar's glyph
    path and the media-type icons both allocate through it."""
    pm = blank_icon_pixmap(LOGICAL, icon_target_dpr())
    assert pm.devicePixelRatio() == icon_target_dpr()
    assert pm.width() == int(round(LOGICAL * icon_target_dpr()))


def test_the_toolbar_glyph_fallback_is_ratio_backed():
    """Buttons without an SVG fall back to a font glyph, which was allocated
    the same wrong way one module over."""
    from ui.toolbar.tool_button import emoji_icon
    pm = emoji_icon("✏️", LOGICAL, "#ffffff").pixmap(LOGICAL, LOGICAL)
    assert pm.devicePixelRatio() == _DPR, \
        f"glyph icon backed at {pm.devicePixelRatio()}x, screen is {_DPR}x"


def test_media_type_icons_are_ratio_backed():
    from ui.panels.media_manager import media_type_icon
    for kind in ("image", "pdf", "ppt", "unknown-kind"):
        pm = media_type_icon(kind, 64).pixmap(64, 64)
        assert pm.devicePixelRatio() == _DPR, \
            f"media icon '{kind}' backed at {pm.devicePixelRatio()}x"


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
