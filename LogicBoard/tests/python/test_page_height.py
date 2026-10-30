"""
test_page_height.py — a page must not shrink out from under its own ink.

Runs under pytest, and also standalone
(``python tests/python/test_page_height.py``).

What is worth protecting here
-----------------------------
A tall background — a scanned sheet, a PDF frame — makes a page TALLER than
the default canvas. Nothing about that height is stored: ``_sheet_size`` and
``_compute_content_height`` both re-derive it by measuring the background
image every time they are asked.

So the moment the background is removed, the page snaps back to
``canvas_height``, and any ink drawn down in the extended part is suddenly
outside the page — clipped away in list style, with the sheet visibly changing
shape under the strokes that remain. The work is not deleted, either, which is
the worst part: the strokes keep their original coordinates and simply become
unreachable, so the page looks like it ate them.

A background can be removed from three places, and all of them funnel through
two calls to ``clear_bg_image``: the Media Manager's "remove from board", its
"delete", and the per-page "reset background". Both are covered below.

THE RULE
========
A page with ink on it keeps the height its background gave it. An untouched
page goes back to normal — pinning every page at whatever height some
since-deleted PDF happened to be would leave a board full of oddly tall blank
pages with no way to undo it.
"""
import os
import sys
import tempfile

os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")
sys.path.insert(0, os.path.abspath(os.path.join(os.path.dirname(__file__),
                                                "..", "..")))

from PySide6.QtWidgets import QApplication
from PySide6.QtGui import QImage, QColor
from PySide6.QtCore import QPointF

_app = QApplication.instance() or QApplication([])

import ui.util.paths as paths

DEFAULT_W, DEFAULT_H = 1600, 900


def _window():
    tmp = tempfile.mkdtemp(prefix="lb_pageheight_")
    from pathlib import Path
    tmp = Path(tmp)
    paths.saves_dir = lambda: tmp
    import ui.main_window as mw
    mw.saves_dir = lambda: tmp
    import ui.panels.file_panel as fp
    fp.saves_dir = lambda: tmp
    win = mw.MainWindow()
    win.resize(1000, 700)
    return win, tmp


def _give_tall_background(win, scene, height=4000, name="tall.png"):
    """Put a very tall image on the page, the way a scanned PDF page is."""
    from pathlib import Path
    scene.set_canvas_size(DEFAULT_W, DEFAULT_H)
    board_dir = win._ensure_board_dir()
    img = QImage(1000, height, QImage.Format_RGB32)
    img.fill(QColor("white"))
    f = Path(board_dir) / name
    img.save(str(f))
    rel = win._relpath_from_board(f)
    try:
        scene.set_bg_image(rel)
    except Exception:
        scene.bg_image = rel
    # "auto" is the fit that extends downward rather than cropping.
    win._canvas.set_bg_fit_mode("auto", rel)
    return rel


def _draw_low(win, y=2400):
    """A stroke in the extended region — only reachable because the tall
    background made the page that long."""
    c = win._canvas
    c.set_tool("pen")
    c.begin_live_stroke()
    for i in range(30):
        c.add_live_point(QPointF(200 + i * 10, y + i * 3), 1.0)
    win._input_handler._commit_stroke("pen")
    objs = list(c.current_scene().all_objects())
    return objs[-1].bounding_box().max_pt.y


# ── the page grows for a tall background ────────────────────────────────────

def test_a_tall_background_makes_the_page_taller():
    """The premise. If this stops being true the rest is meaningless."""
    win, _tmp = _window()
    scene = win._canvas.current_scene()
    _give_tall_background(win, scene)
    content = win._canvas._compute_content_height(scene)
    assert content > DEFAULT_H * 2, (
        f"content height is {content}; a 4000px background is not extending "
        "the page any more")


# ── removing it must not strand the ink ─────────────────────────────────────

def test_removing_the_background_keeps_the_page_tall_when_there_is_ink():
    """THE regression."""
    win, _tmp = _window()
    scene = win._canvas.current_scene()
    _give_tall_background(win, scene)
    extended = win._canvas._compute_content_height(scene)
    ink_bottom = _draw_low(win)
    assert ink_bottom > DEFAULT_H, "the fixture did not draw in the extended part"

    win._pin_page_height_before_bg_loss(scene)
    scene.clear_bg_image()

    assert scene.canvas_height >= extended - 1.0, (
        f"page shrank to {scene.canvas_height} from {extended}")
    assert ink_bottom <= scene.canvas_height, (
        f"ink at y={ink_bottom} is outside a page {scene.canvas_height} tall")


def test_the_ink_itself_is_never_moved():
    """The page is resized around the strokes; the strokes do not shift.
    Anything that rescaled them would distort the handwriting."""
    win, _tmp = _window()
    scene = win._canvas.current_scene()
    _give_tall_background(win, scene)
    _draw_low(win)
    obj = list(scene.all_objects())[0]
    before = [(p[0], p[1]) for p in obj.points()]

    win._pin_page_height_before_bg_loss(scene)
    scene.clear_bg_image()

    after = [(p[0], p[1]) for p in list(scene.all_objects())[0].points()]
    assert before == after, "the stroke's coordinates changed"


def test_an_untouched_page_goes_back_to_normal():
    """The other half of the rule — otherwise a deleted PDF leaves a board
    full of oddly tall blank pages."""
    win, _tmp = _window()
    scene = win._canvas.current_scene()
    _give_tall_background(win, scene)
    assert scene.object_count == 0

    pinned = win._pin_page_height_before_bg_loss(scene)
    scene.clear_bg_image()

    assert pinned is False
    assert scene.canvas_height == DEFAULT_H


def test_pinning_is_idempotent():
    """Removing a background twice, or removing one page's after another's,
    must not ratchet the page taller each time."""
    win, _tmp = _window()
    scene = win._canvas.current_scene()
    _give_tall_background(win, scene)
    _draw_low(win)
    win._pin_page_height_before_bg_loss(scene)
    scene.clear_bg_image()
    first = scene.canvas_height
    for _ in range(5):
        win._pin_page_height_before_bg_loss(scene)
    assert scene.canvas_height == first, (
        f"height crept from {first} to {scene.canvas_height}")


def test_a_page_that_never_grew_is_left_alone():
    """No background, ink well inside the page: nothing to pin."""
    win, _tmp = _window()
    scene = win._canvas.current_scene()
    scene.set_canvas_size(DEFAULT_W, DEFAULT_H)
    c = win._canvas
    c.set_tool("pen")
    c.begin_live_stroke()
    for i in range(10):
        c.add_live_point(QPointF(100 + i * 5, 100 + i * 5), 1.0)
    win._input_handler._commit_stroke("pen")
    assert win._pin_page_height_before_bg_loss(scene) is False
    assert scene.canvas_height == DEFAULT_H


# ── every route that clears a background goes through it ────────────────────

def test_the_reset_background_action_pins_first():
    win, _tmp = _window()
    scene = win._canvas.current_scene()
    _give_tall_background(win, scene)
    extended = win._canvas._compute_content_height(scene)
    _draw_low(win)

    win._media_reset_background()          # the real menu action

    assert scene.canvas_height >= extended - 1.0, \
        "Reset Background shrank a page that had ink on it"


def test_the_media_purge_path_pins_first():
    """"Remove from board" and "Delete" both go through
    _purge_media_references, which clears the background on every page that
    referenced the item."""
    import inspect
    import ui.main_window as mw
    src = inspect.getsource(mw.MainWindow._purge_media_references)
    # Compare positions in the CODE only. The docstring discusses
    # clear_bg_image at length, and matching that prose reports the wrong
    # order for a function whose code is perfectly correct.
    body = src.split('"""')[-1]
    assert "_pin_page_height_before_bg_loss" in body, \
        "deleting media can shrink a page out from under its ink again"
    i = body.index("_pin_page_height_before_bg_loss")
    j = body.index("scene.clear_bg_image()")
    assert i < j, "the pin runs AFTER the clear, by which point the height is gone"


def test_the_height_is_written_as_an_integer():
    """set_canvas_size takes ints. Handing it the floats these measurements
    produce raises TypeError, which the helper's except swallows — the page
    goes quietly unpinned and the bug looks unfixed."""
    win, _tmp = _window()
    scene = win._canvas.current_scene()
    _give_tall_background(win, scene)
    _draw_low(win)
    assert win._pin_page_height_before_bg_loss(scene) is True, \
        "pinning reported failure — most likely a TypeError being swallowed"
    assert isinstance(scene.canvas_height, int)


def test_the_pinned_height_survives_save_and_reload():
    """Pinning is only worth anything if it is saved with the board."""
    win, tmp = _window()
    scene = win._canvas.current_scene()
    _give_tall_background(win, scene)
    _draw_low(win)
    win._pin_page_height_before_bg_loss(scene)
    scene.clear_bg_image()
    tall = scene.canvas_height
    assert win._save_as_board("Tall Page") is True

    import ui.main_window as mw
    win2 = mw.MainWindow()
    win2._open_board(tmp / "Tall Page.lb")
    assert win2._canvas.current_scene().canvas_height == tall, \
        "the pinned height was not saved with the board"


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
