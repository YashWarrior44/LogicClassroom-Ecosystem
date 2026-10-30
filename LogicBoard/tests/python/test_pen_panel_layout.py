"""
test_pen_panel_layout.py — the Drawing Tool panel: tools on the left,
everything else on the right.

    python tests/python/test_pen_panel_layout.py

What the layout promises, pinned:

1. TWO COLUMNS THAT END LEVEL. The four tools are stacked on the left; the
   top tile starts on the size heading's line and the bottom tile ends on
   the last row of colours — no empty band under either column.
2. EVERY RIGHT-HAND ROW IS ONE WIDTH. Slider, preview/back-tip strip and
   colour grid share their left and right edges.
3. ONE SIZE IN EVERY MODE AND CALIBRATION STATE, so nothing moves under the
   stylus. Dual Pen's back-tip picker shares a fixed strip with the preview.
4. DUAL PEN KEEPS EVERY COLOUR.
5. The back tip can be the brush.
6. The palette is the shared ink palette: 23 distinct shades + custom.
7. The Eraser panel is the same family: same width, same tile width.
"""
import os
import sys

os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")
sys.path.insert(0, os.path.abspath(os.path.join(os.path.dirname(__file__),
                                                "..", "..")))

from PySide6.QtWidgets import QApplication

_app = QApplication.instance() or QApplication([])

from ui.panels.pen_panel import (PenPanel, _PRESET_COLORS, _SWATCH_COLS,
                                 _SWATCH_ROWS, _col_w, _tile_w)
from ui.theme import ink_palette
from ui.util.scaling import UiScale

MODES = ("pen", "highlighter", "brush", "dual_pen")
CALIBRATIONS = ((False, False), (False, True), (True, False), (True, True))


def _panel(mode="pen", stylus=True, eraser=True):
    p = PenPanel(None, mode=mode, stylus_calibrated=stylus,
                 eraser_calibrated=eraser)
    p.show()
    _app.processEvents()
    _app.processEvents()
    return p


def _x_span(p, w):
    tl = w.mapTo(p, w.rect().topLeft())
    return tl.x(), tl.x() + w.width()


def _y_span(p, w):
    tl = w.mapTo(p, w.rect().topLeft())
    return tl.y(), tl.y() + w.height()


def _tiles(p):
    return [p._pen_btn, p._hl_btn, p._brush_btn, p._dual_btn]


def _swatches(p):
    return [b for b, _c in p._swatch_btns] + [p._custom_btn]


# ── two columns ─────────────────────────────────────────────────────────────

def test_the_tools_are_stacked_in_a_left_column():
    p = _panel()
    tiles = _tiles(p)
    xs = {_x_span(p, t) for t in tiles}
    assert len(xs) == 1, "the tool tiles are not one column: %s" % xs
    tops = [_y_span(p, t)[0] for t in tiles]
    assert tops == sorted(tops), "the tools are not top to bottom"
    right_edge = _x_span(p, tiles[0])[1]
    for w in [p._slider, p._strip] + _swatches(p):
        assert _x_span(p, w)[0] > right_edge, (
            "a right-column row overlaps the tool column")


def test_the_two_columns_end_level():
    for mode in MODES:
        p = _panel(mode)
        tiles = _tiles(p)
        top_l = _y_span(p, tiles[0])[0]
        bot_l = _y_span(p, tiles[-1])[1]
        top_r = _y_span(p, p._size_heading)[0]
        bot_r = max(_y_span(p, b)[1] for b in _swatches(p))
        assert abs(top_l - top_r) <= 1, (
            "%s: tiles start at %d, the right column at %d"
            % (mode, top_l, top_r))
        assert abs(bot_l - bot_r) <= 1, (
            "%s: tiles end at %d, the colours at %d" % (mode, bot_l, bot_r))
        p.close()


def test_every_right_row_shares_its_left_and_right_edges():
    for mode in MODES:
        p = _panel(mode)
        sw = _swatches(p)
        left = min(_x_span(p, b)[0] for b in sw)
        right = max(_x_span(p, b)[1] for b in sw)
        assert abs((right - left) - _col_w()) <= 2, (right - left, _col_w())
        for name, w in {"slider": p._slider, "strip": p._strip}.items():
            l, r = _x_span(p, w)
            assert abs(l - left) <= 2 and abs(r - right) <= 2, (
                "%s: %s spans %d..%d, the colour grid %d..%d"
                % (mode, name, l, r, left, right))
        p.close()


def test_the_panel_is_centred_on_its_columns():
    p = _panel()
    l = _x_span(p, p._pen_btn)[0]
    r = max(_x_span(p, b)[1] for b in _swatches(p))
    assert abs(l - (p.width() - r)) <= 1, (
        "left margin %d, right margin %d" % (l, p.width() - r))


def test_the_tiles_are_one_size_and_big_enough_to_tap():
    p = _panel()
    sizes = {(t.width(), t.height()) for t in _tiles(p)}
    assert len(sizes) == 1, sizes
    w, h = sizes.pop()
    assert w == _tile_w(), (w, _tile_w())
    assert h >= UiScale.px(70), "the tool tiles are only %d px tall" % h


def test_the_eraser_panel_is_the_same_family():
    from ui.panels.eraser_panel import EraserPanel
    p = _panel()
    e = EraserPanel(None)
    e.show()
    _app.processEvents()
    assert e.width() == p.width(), (e.width(), p.width())
    assert e._pixel_btn.width() == p._pen_btn.width()


# ── one size ────────────────────────────────────────────────────────────────

def test_the_panel_is_the_same_size_in_every_mode_and_state():
    sizes = set()
    for stylus, eraser in CALIBRATIONS:
        for mode in MODES:
            p = _panel(mode, stylus, eraser)
            sizes.add((p.width(), p.height()))
            p.close()
    assert len(sizes) == 1, "the panel changes size: %s" % sorted(sizes)


def test_switching_modes_does_not_resize_it_under_the_stylus():
    p = _panel("pen")
    first = p.size()
    for mode in ("highlighter", "brush", "dual_pen", "pen"):
        p._select_mode(mode)
        _app.processEvents()
        assert p.size() == first, "%s mode resized the panel" % mode


# ── dual pen ────────────────────────────────────────────────────────────────

def test_dual_pen_keeps_every_colour():
    p = _panel("dual_pen")
    assert p._dual_section.isVisible()
    hidden = [b for b, _c in p._swatch_btns if not b.isVisible()]
    assert not hidden, "%d swatches hidden in Dual Pen" % len(hidden)
    assert p._custom_btn.isVisible(), "the custom colour is gone in Dual Pen"


def test_the_back_tip_can_be_the_brush():
    got = []
    p = PenPanel(None, mode="dual_pen", stylus_calibrated=True,
                 eraser_calibrated=True, on_dual_back_change=got.append)
    assert set(p._bt_btns) == {"pen", "highlighter", "brush", "eraser",
                               "select"}
    p._bt_btns["brush"].click()
    assert got == ["brush"]


def test_the_back_tip_strip_replaces_the_preview_in_the_same_slot():
    p = _panel("dual_pen")
    assert p._strip.currentWidget() is p._dual_section
    assert p._strip_heading.text() == "BACK TIP"
    q = _panel("pen")
    assert q._strip.currentWidget() is q._preview
    assert q._strip_heading.text() == "PREVIEW"


# ── the preview ─────────────────────────────────────────────────────────────

def test_the_preview_is_a_strip_not_a_card():
    """It takes up the levelling slack, but never grows into a card: it
    stays shorter than the colour grid under it."""
    p = _panel()
    sw = _swatches(p)
    grid_h = (max(_y_span(p, b)[1] for b in sw)
              - min(_y_span(p, b)[0] for b in sw))
    assert p._preview.height() < grid_h, (
        "the preview is %d px tall, the colour grid %d"
        % (p._preview.height(), grid_h))
    assert p._preview.size() == p._dual_section.size() == p._strip.size()


def _ink_rows(p):
    """Rows of the preview pixmap that have ink in them, in logical px."""
    pm = p._preview.pixmap()
    img, dpr = pm.toImage(), pm.devicePixelRatio()
    rows = [y for y in range(img.height())
            if any(img.pixelColor(x, y).alpha() > 0
                   for x in range(0, img.width(), 5))]
    return rows, img.height(), dpr


def test_the_biggest_highlighter_is_shown_whole():
    """60 px is drawn 60 px tall — not capped to the strip — and clear of
    its edges."""
    p = PenPanel(None, mode="highlighter", highlighter_height=60)
    p.show(); _app.processEvents()
    rows, h, dpr = _ink_rows(p)
    tall = (max(rows) - min(rows) + 1) / dpr
    assert abs(tall - 60) <= 1.5, "the 60 px highlighter is %.1f px tall" % tall
    assert min(rows) > 0 and max(rows) < h - 1, "it touches the strip edge"


def test_the_biggest_brush_is_shown_whole():
    p = PenPanel(None, mode="brush", brush_height=60)
    p.show(); _app.processEvents()
    rows, h, _dpr = _ink_rows(p)
    assert min(rows) > 0 and max(rows) < h - 1, "the 60 px brush is cut off"


def test_the_highlighter_stops_at_60():
    from ui.canvas.canvas_widget import CanvasWidget, HIGHLIGHTER_MAX_HEIGHT
    from ui.panels.pen_panel import _HL_RANGE
    assert HIGHLIGHTER_MAX_HEIGHT == 60 == _HL_RANGE[1]
    p = _panel("highlighter")
    assert p._slider.maximum() == 60
    c = CanvasWidget()
    c.set_highlighter_height(110)
    assert c.highlighter_height == 60


def test_an_old_oversized_highlighter_opens_at_the_cap():
    """Saved when highlighters went to 110: show 60, not a value the
    slider cannot reach."""
    p = PenPanel(None, mode="highlighter", highlighter_height=110)
    assert p._value_label.text().startswith("60")


# ── the pen slider ──────────────────────────────────────────────────────────

def test_the_first_third_of_the_pen_slider_is_1_to_10_px():
    from ui.panels.pen_panel import (_PEN_STEPS, _pen_pos_to_width,
                                     _pen_width_to_pos)
    third = _PEN_STEPS // 3
    assert _pen_pos_to_width(0) == 1
    assert _pen_pos_to_width(third) == 10
    assert _pen_pos_to_width(third + 1) == 11
    assert _pen_pos_to_width(_PEN_STEPS) == 40
    assert _pen_width_to_pos(10) == third
    for pos in range(third + 1):
        assert 1 <= _pen_pos_to_width(pos) <= 10, pos
    for pos in range(third + 1, _PEN_STEPS + 1):
        assert 11 <= _pen_pos_to_width(pos) <= 40, pos


def test_every_pen_width_can_be_reached_and_shown():
    from ui.panels.pen_panel import (_PEN_STEPS, _pen_pos_to_width,
                                     _pen_width_to_pos)
    assert sorted({_pen_pos_to_width(q) for q in range(_PEN_STEPS + 1)}) == \
        list(range(1, 41))
    for w in range(1, 41):
        assert _pen_pos_to_width(_pen_width_to_pos(w)) == w, w


def test_the_small_sizes_get_more_room_than_the_big_ones():
    from ui.panels.pen_panel import _pen_width_to_pos
    fine = _pen_width_to_pos(3) - _pen_width_to_pos(2)
    coarse = _pen_width_to_pos(30) - _pen_width_to_pos(29)
    assert fine > coarse, (fine, coarse)


def test_dragging_the_pen_slider_reports_px_not_positions():
    got = []
    p = PenPanel(None, mode="pen", pen_width=4, on_width_change=got.append)
    p.show(); _app.processEvents()
    from ui.panels.pen_panel import _PEN_STEPS
    for pos in range(0, _PEN_STEPS + 1, 7):
        p._slider.setValue(pos)
    p._slider.setValue(_PEN_STEPS // 3)
    assert got and all(1 <= w <= 40 for w in got), got[:10]
    assert got[-1] == 10 and p._value_label.text() == "10 px"
    assert all(a != b for a, b in zip(got, got[1:])), "repeated callbacks"


# ── calibration lock ────────────────────────────────────────────────────────

def test_an_uncalibrated_dual_pen_tile_carries_its_own_warning():
    for stylus, eraser in ((False, False), (False, True), (True, False)):
        p = _panel("pen", stylus, eraser)
        assert "🔒" in p._dual_btn.text()
        assert "Calibrate" in p._dual_btn.toolTip()


def test_the_warning_names_the_tip_that_is_missing():
    cases = {(False, False): ("front", "back"), (False, True): ("FRONT",),
             (True, False): ("BACK",)}
    for (stylus, eraser), words in cases.items():
        tip = _panel("pen", stylus, eraser)._dual_btn.toolTip()
        for w in words:
            assert w in tip, (stylus, eraser, tip)


def test_a_calibrated_tile_is_a_plain_tile_again():
    p = _panel("pen", True, True)
    assert p._dual_btn.text() == "Dual Pen" and p._dual_btn.toolTip() == ""


def test_a_locked_tile_still_refuses_to_be_selected():
    p = _panel("pen", False, True)
    p._select_mode("dual_pen")
    assert p._mode == "pen"


# ── the palette ─────────────────────────────────────────────────────────────

def test_the_grid_is_the_shared_ink_palette():
    assert _PRESET_COLORS is ink_palette.INK_COLORS
    assert len(_PRESET_COLORS) == _SWATCH_COLS * _SWATCH_ROWS - 1


def test_none_of_the_stock_material_primaries_survive():
    stock = {(244, 67, 54), (33, 150, 243), (76, 175, 80), (255, 235, 59),
             (255, 152, 0), (156, 39, 176), (0, 0, 0), (255, 255, 255)}
    used = {(c.red(), c.green(), c.blue()) for c in _PRESET_COLORS}
    assert not (used & stock), "stock colours back: %s" % (used & stock)


def test_no_two_swatches_are_the_same_colour():
    keys = [(c.red(), c.green(), c.blue()) for c in _PRESET_COLORS]
    assert len(keys) == len(set(keys))


def test_every_swatch_is_distinguishable_from_its_neighbours():
    cols = list(_PRESET_COLORS)
    for i, a in enumerate(cols):
        for b in cols[i + 1:]:
            d = (abs(a.red() - b.red()) + abs(a.green() - b.green())
                 + abs(a.blue() - b.blue()))
            assert d >= 30, ((a.getRgb()[:3]), (b.getRgb()[:3]), d)


def test_every_swatch_can_actually_be_picked():
    p = _panel()
    for btn, colour in p._swatch_btns:
        btn.click()
        got = p._active_color()
        assert got.rgb() == colour.rgb()
        assert btn.text() == "✓", "the picked swatch is not marked"


def test_a_colour_off_the_grid_marks_the_custom_cell():
    from PySide6.QtGui import QColor
    p = PenPanel(None, mode="pen", pen_color=QColor(1, 2, 3))
    assert not any(b.text() for b, _c in p._swatch_btns)
    assert "3px" in p._custom_btn.styleSheet().replace(" ", "") or \
        str(UiScale.px(3)) + "px" in p._custom_btn.styleSheet()


def test_the_default_inks_open_on_a_real_swatch():
    """The canvas defaults come from the palette, so the panel never opens
    with the custom cell selected for no reason."""
    from ui.canvas.canvas_widget import CanvasWidget
    c = CanvasWidget()
    rgbs = {x.rgb() for x in _PRESET_COLORS}
    for name in ("pen_color", "highlighter_color", "brush_color",
                 "dual_pen_color", "shape_color"):
        assert getattr(c, name).rgb() in rgbs, "%s is off-palette" % name


if __name__ == "__main__":
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
