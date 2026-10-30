"""
test_bg_fit.py — how a media background is laid onto the page.

Runs under pytest, and also standalone (``python tests/python/test_bg_fit.py``)
because the repo's pytest install is currently broken on Python 3.11.

Three modes (``CanvasWidget.BG_FIT_MODES``):

  auto     the long-standing behaviour, and still the default so old boards
           look unchanged: a tall image is fit to WIDTH and runs off the
           bottom to be scrolled, anything else is fit to COVER and cropped.
  fit      contain — the whole image inside the canvas, aspect kept.
  stretch  fill the canvas exactly, aspect distorted.

What's worth protecting is that "fit" and "stretch" both show ALL of the
media on ONE screen — that is the entire point of the two buttons — and that
neither of them can quietly re-introduce the scrolling or the cropping the
user picked them to avoid.
"""
import json
import os
import sys

os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")
sys.path.insert(0, os.path.abspath(os.path.join(os.path.dirname(__file__),
                                                "..", "..")))

from PySide6.QtWidgets import QApplication
from PySide6.QtGui import QPixmap, QColor

_app = QApplication.instance() or QApplication([])

from ui.canvas.canvas_widget import CanvasWidget

CW, CH = 1600, 900

# Wider than the canvas, taller than it, square, and an exact match.
SHAPES = [(3200, 900), (800, 2400), (1000, 1000), (1600, 900)]


def _canvas(img_w, img_h, mode="auto"):
    c = CanvasWidget()
    c.resize(CW, CH)
    s = c.current_scene()
    s.set_canvas_size(CW, CH)
    rel = f"media/pic_{img_w}x{img_h}.png"
    s.bg_image = rel
    pm = QPixmap(img_w, img_h)
    pm.fill(QColor(120, 90, 200))
    if not hasattr(c, "_pixmap_cache"):
        c._pixmap_cache = {}
    c._pixmap_cache[rel] = pm          # stand in for the file on disk
    c.set_bg_fit_mode(mode, rel)
    return c, s, rel


def _dest(c, s):
    pm, rect = c._bg_image_layout(s)
    assert pm is not None and rect is not None
    return rect


# ------------------------------------------------------------- fit == contain

def test_fit_shows_the_whole_image_on_one_screen():
    for w, h in SHAPES:
        c, s, _ = _canvas(w, h, "fit")
        d = _dest(c, s)
        assert d.left() >= -0.6 and d.top() >= -0.6, f"{w}x{h}: {d} spills off"
        assert d.right() <= CW + 0.6 and d.bottom() <= CH + 0.6, \
            f"{w}x{h}: {d} spills off"
        # ...and nothing is left below the fold to scroll to.
        assert c._compute_content_height(s) <= CH + 0.6, f"{w}x{h} still scrolls"


def test_fit_keeps_the_aspect_ratio():
    for w, h in SHAPES:
        c, s, _ = _canvas(w, h, "fit")
        d = _dest(c, s)
        assert abs((h / w) - (d.height() / d.width())) < 0.01, \
            f"{w}x{h} came out {d.width():.0f}x{d.height():.0f}"


def test_fit_is_centred():
    for w, h in SHAPES:
        c, s, _ = _canvas(w, h, "fit")
        d = _dest(c, s)
        assert abs(d.left() - (CW - d.width()) / 2.0) < 0.6, f"{w}x{h} off-centre"
        assert abs(d.top() - (CH - d.height()) / 2.0) < 0.6, f"{w}x{h} off-centre"


def test_fit_uses_the_whole_canvas_in_one_direction():
    """Contain means touching two edges — otherwise it isn't fitted, it's just
    small."""
    for w, h in SHAPES:
        c, s, _ = _canvas(w, h, "fit")
        d = _dest(c, s)
        assert abs(d.width() - CW) < 0.6 or abs(d.height() - CH) < 0.6, \
            f"{w}x{h}: {d.width():.0f}x{d.height():.0f} touches neither pair of edges"


# ----------------------------------------------------------- stretch == fill

def test_stretch_fills_the_canvas_exactly():
    for w, h in SHAPES:
        c, s, _ = _canvas(w, h, "stretch")
        d = _dest(c, s)
        assert (abs(d.left()) < 0.6 and abs(d.top()) < 0.6
                and abs(d.width() - CW) < 0.6 and abs(d.height() - CH) < 0.6), \
            f"{w}x{h}: {d}"
        assert c._compute_content_height(s) <= CH + 0.6, f"{w}x{h} still scrolls"


# --------------------------------------------------------- auto is untouched

def test_auto_still_crops_wide_and_scrolls_tall():
    """The default has to be exactly what it always was, or every existing
    board re-lays itself the first time it is opened."""
    c, s, _ = _canvas(3200, 900, "auto")
    d = _dest(c, s)
    assert d.left() < -0.6, "a wide image should still be cropped to cover"

    c, s, _ = _canvas(800, 2400, "auto")
    assert c._compute_content_height(s) > CH + 1, \
        "a tall image should still extend below the fold"


def test_a_background_with_no_choice_recorded_is_auto():
    c, s, _ = _canvas(800, 2400)          # never told what to do with it
    assert c.bg_fit_mode(s.bg_image) == "auto"
    assert c.bg_fit_map() == {}, "defaults must not be stored"


# ------------------------------------------------------------- the fit map

def test_the_mode_follows_the_IMAGE_not_the_page():
    """Keyed by path on purpose: undo puts the previous background back, and
    the fit has to come back with it rather than staying on the page."""
    c, s, rel_tall = _canvas(800, 2400, "fit")
    c.set_bg_fit_mode("stretch", "media/other.png")

    s.bg_image = "media/other.png"
    assert c.bg_fit_mode(s.bg_image) == "stretch"
    s.bg_image = rel_tall                  # as an undo would
    assert c.bg_fit_mode(s.bg_image) == "fit"


def test_the_map_survives_a_save_and_load():
    c, _, _ = _canvas(800, 2400, "fit")
    c.set_bg_fit_mode("stretch", "media/other.png")
    saved = c.bg_fit_map()

    c2 = CanvasWidget()
    c2.resize(CW, CH)
    c2.load_bg_fit_map(json.loads(json.dumps(saved)))
    assert c2.bg_fit_map() == saved


def test_paths_match_however_the_platform_spelled_them():
    c = CanvasWidget()
    c.resize(CW, CH)
    c.load_bg_fit_map({"media/Pic.png": "fit"})
    assert c.bg_fit_mode("media\\PIC.png") == "fit"
    assert c.bg_fit_mode("/media/pic.png") == "fit"


def test_a_corrupt_sidecar_cannot_break_the_board():
    c = CanvasWidget()
    c.resize(CW, CH)
    for junk in (None, [], "nope", 7, {"a.png": "bogus"}, {"a.png": None},
                 {7: "fit"}, {"a.png": "auto"}):
        c.load_bg_fit_map(junk)
        assert c.bg_fit_map() == {}, junk
    c.load_bg_fit_map({"a.png": "fit", "b.png": "wat"})
    assert c.bg_fit_map() == {"a.png": "fit"}


def test_setting_a_mode_back_to_auto_forgets_it():
    c, s, rel = _canvas(800, 2400, "fit")
    assert c.bg_fit_map()
    c.set_bg_fit_mode("auto", rel)
    assert c.bg_fit_map() == {}
    assert c.bg_fit_mode(rel) == "auto"


def test_a_fresh_board_drops_the_previous_boards_choices():
    c, _, rel = _canvas(800, 2400, "fit")
    c._init_engine()                       # what File > New goes through
    assert c.bg_fit_map() == {}, "fit choices leaked into a new board"


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
