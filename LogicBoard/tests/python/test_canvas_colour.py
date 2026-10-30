"""
test_canvas_colour.py — the board is pure black, old boards included.

Runs under pytest, and also standalone
(``python tests/python/test_canvas_colour.py``).

The canvas colour is written INTO each page when the page is made, so
changing the default alone would leave every board saved before the change
on the old grey. ``CanvasWidget.adopt_default_bg`` moves pages that still
carry an OLD DEFAULT onto the current one when a board is opened — and must
leave alone any page whose colour was actually chosen.
"""
import os
import sys

os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")
sys.path.insert(0, os.path.abspath(os.path.join(os.path.dirname(__file__),
                                                "..", "..")))

from PySide6.QtWidgets import QApplication

_app = QApplication.instance() or QApplication([])

import logicboard_engine as lbe
from ui.theme.theme_manager import theme_mgr


def _canvas(pages=3):
    from ui.main_window import MainWindow
    win = MainWindow()
    c = win._canvas
    while c.page_count < pages:
        c.add_page()
    return win, c


def _rgb(c, i):
    bg = c.scene_at(i).background_color
    return (bg.r, bg.g, bg.b)


def test_the_canvas_is_pure_black():
    q = theme_mgr.qcolor(theme_mgr.tokens.canvas_bg_color)
    assert (q.red(), q.green(), q.blue()) == (0, 0, 0), q.name()


def test_a_new_page_is_black():
    _win, c = _canvas(2)
    assert _rgb(c, 0) == (0, 0, 0) and _rgb(c, 1) == (0, 0, 0)


def test_an_old_board_on_the_old_grey_turns_black():
    _win, c = _canvas(3)
    for i in range(3):
        c.scene_at(i).background_color = lbe.RGBA(28, 28, 28, 255)
    assert c.adopt_default_bg() == 3
    assert [_rgb(c, i) for i in range(3)] == [(0, 0, 0)] * 3


def test_a_chosen_colour_is_left_alone():
    _win, c = _canvas(2)
    c.scene_at(0).background_color = lbe.RGBA(28, 28, 28, 255)      # old default
    c.scene_at(1).background_color = lbe.RGBA(250, 250, 245, 255)   # chosen
    assert c.adopt_default_bg() == 1
    assert _rgb(c, 0) == (0, 0, 0)
    assert _rgb(c, 1) == (250, 250, 245), "a page the teacher coloured was changed"


def test_the_list_style_desk_is_not_the_page_colour():
    """In the scrolling style the pages lie on a desk. Black pages on a
    black desk was the complaint: you could not see where a page ended."""
    win, c = _canvas(3)
    win.resize(1400, 900)
    win.show()
    _app.processEvents()
    c.set_canvas_style("scroll_list")
    c.invalidate_cache()
    _app.processEvents()
    img = c.grab().toImage()
    desk = img.pixelColor(3, img.height() // 2)        # the left margin
    want = theme_mgr.qcolor(theme_mgr.tokens.canvas_desk)
    assert (desk.red(), desk.green(), desk.blue()) == \
        (want.red(), want.green(), want.blue()), (
        "the desk at the edge is %s, expected %s" % (desk.name(), want.name()))
    assert desk.lightness() >= 30, "the desk is as black as the pages"


def test_opening_a_board_runs_the_migration():
    """Both open paths, not just one: a board opened from the file panel and
    one opened by double-click go through different methods."""
    src = open(os.path.join(os.path.dirname(__file__), "..", "..", "ui",
                            "main_window.py"), encoding="utf-8").read()
    assert src.count("self._canvas.adopt_default_bg()") == 2, \
        "an open path skips the canvas colour migration"


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
    print("\n%d failed" % len(failures) if failures else "\nall passed")
    sys.exit(1 if failures else 0)
