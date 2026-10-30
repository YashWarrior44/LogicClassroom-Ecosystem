"""
test_bindings.py — smoke tests for the pybind11 ``logicboard_engine`` module.

Runs under pytest, and also standalone
(``python tests/python/test_bindings.py``) like every other suite here.

That is not a stylistic preference. The only pytest on this machine is
2.6.0, released in 2014, which cannot start under Python 3.11 at all — so
for as long as this file could ONLY be run by pytest, it was never run. A
test that cannot execute is worse than no test, because the suite still
lists it and it still looks like coverage.
"""
import os
import sys

sys.path.insert(0, os.path.abspath(os.path.join(os.path.dirname(__file__),
                                                "..", "..")))


class _Skip(Exception):
    """Raised when the engine is not built. Reported as a skip, not a
    failure — an unbuilt .pyd is a missing build step, not a bug."""


def _try_import():
    try:
        import logicboard_engine
        return logicboard_engine
    except ImportError:
        raise _Skip("logicboard_engine not built — "
                    "run scripts/build_engine.py first")


def test_vec2f():
    lbe = _try_import()
    v = lbe.Vec2f(3.0, 4.0)
    assert v.x == 3.0
    assert v.y == 4.0
    assert abs(v.length() - 5.0) < 1e-5


def test_rgba():
    lbe = _try_import()
    c = lbe.RGBA(255, 0, 128, 200)
    assert c.r == 255
    assert c.g == 0
    assert c.b == 128
    assert c.a == 200
    assert c.to_hex_string() == "#ff0080c8"


def test_uuid():
    lbe = _try_import()
    u = lbe.UUID.generate()
    assert not u.is_null()
    s = u.to_string()
    assert len(s) == 36
    u2 = lbe.UUID.from_string(s)
    assert u == u2


def test_scene_add_remove():
    lbe = _try_import()
    scene = lbe.Scene(1920, 1080)
    assert scene.object_count == 0

    stroke = lbe.FreehandStroke()
    stroke.add_point(lbe.Vec2f(10, 20))
    stroke.add_point(lbe.Vec2f(30, 40))

    scene.add_object(stroke)
    assert scene.object_count == 1

    obj = scene.object_at(0)
    assert obj.type_name == "freehand"

    scene.remove(obj.id)
    assert scene.object_count == 0


def test_board_json_roundtrip():
    lbe = _try_import()
    board = lbe.Board("TestBoard")
    board.theme = "dark"

    page = board.page_mut(0)
    page.set_canvas_size(800, 600)

    stroke = lbe.FreehandStroke()
    stroke.stroke_color = lbe.RGBA(255, 0, 0)
    stroke.add_point(lbe.Vec2f(1, 2))
    stroke.add_point(lbe.Vec2f(3, 4))
    page.add_object(stroke)

    json_str = board.serialize_to_string()
    assert "TestBoard" in json_str
    assert "freehand" in json_str

    board2 = lbe.Board()
    ok = board2.deserialize_from_string(json_str)
    assert ok
    assert board2.name == "TestBoard"
    assert board2.page_count == 1
    assert board2.page(0).object_count == 1


def test_command_stack_undo_redo():
    lbe = _try_import()
    scene = lbe.Scene(800, 600)
    stack = lbe.CommandStack(100)

    stroke = lbe.FreehandStroke()
    stroke.add_point(lbe.Vec2f(0, 0))
    stroke.add_point(lbe.Vec2f(100, 100))

    stack.push_stroke(scene, stroke)
    assert scene.object_count == 1
    assert stack.can_undo

    stack.undo(scene)
    assert scene.object_count == 0
    assert stack.can_redo

    stack.redo(scene)
    assert scene.object_count == 1


def test_hit_test():
    lbe = _try_import()
    scene = lbe.Scene(800, 600)

    stroke = lbe.FreehandStroke()
    stroke.stroke_width = 5.0
    stroke.add_point(lbe.Vec2f(100, 100))
    stroke.add_point(lbe.Vec2f(200, 100))
    scene.add_object(stroke)

    hit = scene.hit_test(lbe.Vec2f(150, 100), 4.0)
    assert hit is not None
    assert hit.type_name == "freehand"

    miss = scene.hit_test(lbe.Vec2f(500, 500), 4.0)
    assert miss is None


if __name__ == "__main__":
    failures, skipped = [], []
    for name, fn in sorted(globals().items()):
        if not name.startswith("test_") or not callable(fn):
            continue
        try:
            fn()
            print(f"  ok    {name}")
        except _Skip as exc:
            skipped.append(name)
            print(f"  skip  {name}: {exc}")
        except AssertionError as exc:
            failures.append(name)
            print(f"  FAIL  {name}: {exc}")
        except Exception as exc:                       # noqa: BLE001
            failures.append(name)
            print(f"  ERROR {name}: {type(exc).__name__}: {exc}")
    tail = f" ({len(skipped)} skipped)" if skipped else ""
    summary = ("%d failed" % len(failures)) if failures else "all passed"
    print("\n" + summary + tail)
    sys.exit(1 if failures else 0)
