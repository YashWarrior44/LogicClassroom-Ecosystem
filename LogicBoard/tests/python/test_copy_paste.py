"""
test_copy_paste.py — Copy, the Paste chip, and Bring to Front.

Runs under pytest, and also standalone
(``python tests/python/test_copy_paste.py``).

Driven through MainWindow's real handlers on a real engine scene — the same
calls the Selection menu's Copy / To Front tiles and the chip's Paste button
make.
"""
import os
import sys
import tempfile
from pathlib import Path

os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")
ROOT = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(ROOT))

from PySide6.QtWidgets import QApplication

_app = QApplication.instance() or QApplication([])

import logicboard_engine as lbe


def _window():
    import ui.util.paths as paths
    tmp = Path(tempfile.mkdtemp())
    paths.saves_dir = lambda: tmp
    from ui.main_window import MainWindow
    w = MainWindow()
    w._saves_dir = tmp
    w.resize(1400, 900)
    w.show()
    _app.processEvents()
    return w


def _stroke(scene, y, color=(255, 0, 0)):
    o = lbe.FreehandStroke()
    o.set_points([(100.0 + i * 4, y) for i in range(30)])
    o.stroke_width = 3.0
    o.stroke_color = lbe.RGBA(*color, 255)
    return scene.add_object(o).id


def _select(w, ids):
    scene = w._canvas.current_scene()
    w._canvas.set_selection(ids, w._union_rect(scene, ids))
    _app.processEvents()


def _ids(scene):
    return [str(scene.object_at(i).id) for i in range(scene.object_count)]


def test_the_selection_menu_has_copy_and_to_front():
    w = _window()
    a = _stroke(w._canvas.current_scene(), 200)
    _select(w, [a])
    w._on_selection_changed()
    keys = set(w._selection_menu._buttons)
    assert {"copy", "front"} <= keys, keys


def test_copy_shows_the_paste_chip_in_the_top_right():
    w = _window()
    a = _stroke(w._canvas.current_scene(), 200)
    _select(w, [a])
    w._copy_selection()
    chip = w._paste_chip
    assert chip.isVisible()
    assert "1 item copied" in chip._label.text()
    g = chip.geometry()
    assert g.right() > w.width() * 0.8 and g.top() < w.height() * 0.1, \
        "the chip is not in the top-right corner: %s" % g


def test_paste_on_another_page_lands_in_the_same_spot():
    w = _window()
    c = w._canvas
    a = _stroke(c.current_scene(), 200)
    bb0 = c.current_scene().find(a).bounding_box()
    _select(w, [a])
    w._copy_selection()
    c.add_page()
    c.goto_page(1)
    w._paste_clipboard()
    s1 = c.current_scene()
    assert s1.object_count == 1, "nothing was pasted"
    bb1 = s1.object_at(0).bounding_box()
    assert abs(bb1.min_pt.x - bb0.min_pt.x) < 0.01 and \
        abs(bb1.min_pt.y - bb0.min_pt.y) < 0.01
    assert c.get_selected_ids() == [s1.object_at(0).id], \
        "the pasted copy is not selected"


def test_pasting_twice_gives_two_objects_that_fan_out():
    w = _window()
    c = w._canvas
    scene = c.current_scene()
    a = _stroke(scene, 200)
    _select(w, [a])
    w._copy_selection()
    w._paste_clipboard()
    w._paste_clipboard()
    assert scene.object_count == 3
    ids = {str(scene.object_at(i).id) for i in range(3)}
    assert len(ids) == 3, "two pastes shared an id"
    xs = sorted(scene.object_at(i).bounding_box().min_pt.x for i in range(3))
    assert xs[1] - xs[0] > 10 and xs[2] - xs[1] > 10, \
        "pastes on the source page stack exactly on top of each other"


def test_the_copy_survives_the_original_being_deleted():
    w = _window()
    c = w._canvas
    scene = c.current_scene()
    a = _stroke(scene, 200)
    _select(w, [a])
    w._copy_selection()
    w._delete_selection()
    assert scene.object_count == 0
    w._paste_clipboard()
    assert scene.object_count == 1


def test_paste_is_one_undo_step():
    w = _window()
    c = w._canvas
    scene = c.current_scene()
    ids = [_stroke(scene, 200), _stroke(scene, 260)]
    _select(w, ids)
    w._copy_selection()
    w._paste_clipboard()
    assert scene.object_count == 4
    c.undo()
    assert scene.object_count == 2, "one Undo did not remove the whole paste"


def test_a_new_board_clears_the_clipboard():
    w = _window()
    a = _stroke(w._canvas.current_scene(), 200)
    _select(w, [a])
    w._copy_selection()
    w._canvas.clear_dirty()
    w._new_board()
    assert w._clipboard is None and not w._paste_chip.isVisible()


def test_bring_to_front_raises_and_undoes():
    w = _window()
    c = w._canvas
    scene = c.current_scene()
    a = _stroke(scene, 200, (255, 0, 0))
    b = _stroke(scene, 205, (0, 255, 0))
    d = _stroke(scene, 210, (0, 0, 255))
    before = _ids(scene)
    _select(w, [a])
    w._bring_selection_to_front()
    after = _ids(scene)
    assert after == [str(b), str(d), str(a)], after
    assert [scene.object_at(i).z_order for i in range(3)] == [0, 1, 2]
    assert c.get_selected_ids() == [a], "the raised object lost its selection"
    c.undo()
    assert _ids(scene) == before, "Undo did not put it back underneath"


def test_bring_to_front_keeps_a_group_in_its_own_order():
    w = _window()
    scene = w._canvas.current_scene()
    a, b, d, e = (_stroke(scene, 200 + k) for k in range(4))
    _select(w, [b, a])                        # selection order is not depth
    w._bring_selection_to_front()
    assert _ids(scene) == [str(d), str(e), str(a), str(b)], _ids(scene)


if __name__ == "__main__":
    import logging
    logging.disable(logging.WARNING)
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
