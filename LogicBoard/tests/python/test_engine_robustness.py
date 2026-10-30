"""
test_engine_robustness.py — the C++ engine must never take the app down.

Runs under pytest, and also standalone
(``python tests/python/test_engine_robustness.py``).

What is worth protecting here
-----------------------------
``logicboard_engine`` is native code called straight from the UI thread. It
has two failure modes Python cannot contain:

  * a **segfault** kills the process outright — no dialog, no log, no
    autosave, mid-lesson;
  * a **hang** is arguably worse, because the board just stops responding and
    there is nothing on screen to say why.

Neither can be caught with ``try/except``, so the only defence is that the
engine refuses bad input instead of trusting it. Raising a Python exception is
a fine outcome here and counts as a pass — what must not happen is a crash or
a freeze.

The inputs below are the ones a board file can actually carry. A .lb is a zip
of JSON that has been on a shared drive, copied between machines, and possibly
truncated by a disconnect mid-save, so "the file is malformed" is an ordinary
Tuesday, not an attack.

THE ONE THAT WAS REAL
=====================
``simplify(-1.0)`` hung forever. Douglas-Peucker splits a span at its furthest
point; on a run of collinear points every distance is 0, so ``max_dist`` stays
0 and the split index never moves off ``start``. The test ``max_dist > epsilon``
then reads ``0 > -1`` — true — and pushes back the very range it just popped.
The stack never shrinks.

It was in THREE places: ``PointSimplify.cpp``, and private copies inside
``FreehandStroke.cpp`` and ``HighlighterStroke.cpp``. Fixing one would have
left two, which is the whole reason these tests drive the public API for each
stroke type separately rather than testing the shared helper once.
"""
import math
import os
import subprocess
import sys
import textwrap

sys.path.insert(0, os.path.abspath(os.path.join(os.path.dirname(__file__),
                                                "..", "..")))

import logicboard_engine as lbe

_ROOT = os.path.abspath(os.path.join(os.path.dirname(__file__), "..", ".."))


def _in_subprocess(body: str, timeout: float = 20.0):
    """Run engine code in its own process; report crash / hang / clean exit.

    Anything that might loop forever goes through here. A hang inside the
    test suite itself would be indistinguishable from CI being slow, and a
    segfault would take the runner with it.
    """
    code = textwrap.dedent("""
        import sys, math
        sys.path.insert(0, r"{root}")
        import logicboard_engine as lbe
        {body}
        print("OK")
    """).format(root=_ROOT, body=textwrap.indent(body, "        ").strip())
    try:
        r = subprocess.run([sys.executable, "-c", code], capture_output=True,
                           text=True, timeout=timeout, cwd=_ROOT)
    except subprocess.TimeoutExpired:
        return "HANG", ""
    if r.returncode not in (0, 1):
        return "CRASH", f"exit={r.returncode}"
    return "OK", (r.stdout or "") + (r.stderr or "")


# ── the simplification hang ─────────────────────────────────────────────────

def test_simplifying_a_straight_line_with_a_negative_tolerance_returns():
    """THE regression. A collinear stroke plus a negative epsilon looped
    forever and froze the process."""
    for kind in ("FreehandStroke", "HighlighterStroke"):
        state, detail = _in_subprocess(f"""
            s = lbe.{kind}()
            for i in range(200):
                s.add_point(lbe.Vec2f(i, i))
            s.simplify(-1.0)
        """)
        assert state == "OK", f"{kind}.simplify(-1.0) -> {state} {detail}"


def test_simplify_survives_every_degenerate_tolerance():
    for kind in ("FreehandStroke", "HighlighterStroke"):
        for eps in ("-1.0", "0.0", "float('nan')", "float('-inf')",
                    "float('inf')", "1e30"):
            state, detail = _in_subprocess(f"""
                s = lbe.{kind}()
                for i in range(150):
                    s.add_point(lbe.Vec2f(i, i))
                s.simplify({eps})
            """)
            assert state == "OK", f"{kind}.simplify({eps}) -> {state} {detail}"


def test_simplify_still_actually_simplifies():
    """The guard clamps the tolerance; it must not neuter the algorithm."""
    s = lbe.FreehandStroke()
    for i in range(200):
        s.add_point(lbe.Vec2f(i, math.sin(i / 8.0) * 30))
    before = s.point_count
    s.simplify(1.0)
    assert s.point_count < before / 2, \
        f"{before} points became {s.point_count} — simplification stopped working"
    assert s.point_count >= 2


def test_a_degenerate_tolerance_keeps_the_shape():
    """Clamping a negative epsilon to zero means "drop only what is exactly
    redundant" — the stroke must not be mangled."""
    s = lbe.FreehandStroke()
    for i in range(100):
        s.add_point(lbe.Vec2f(i, math.sin(i / 5.0) * 40))
    box_before = s.bounding_box()
    s.simplify(-1.0)
    box_after = s.bounding_box()
    for a, b in ((box_before.min_pt.x, box_after.min_pt.x),
                 (box_before.max_pt.x, box_after.max_pt.x),
                 (box_before.min_pt.y, box_after.min_pt.y),
                 (box_before.max_pt.y, box_after.max_pt.y)):
        assert abs(a - b) < 0.51, f"bounding box moved: {a} -> {b}"


# ── indices handed in from Python ───────────────────────────────────────────

def test_out_of_range_page_indices_raise_rather_than_crash():
    b = lbe.Board()
    b.add_page()
    for call in (lambda: b.page(10 ** 9),
                 lambda: b.page_mut(10 ** 9),
                 lambda: b.remove_page(10 ** 9)):
        try:
            call()
        except Exception:
            continue
        raise AssertionError("an out-of-range page index was accepted")


def test_negative_page_indices_are_refused():
    b = lbe.Board()
    b.add_page()
    for call in (lambda: b.page(-1),
                 lambda: b.page_mut(-1),
                 lambda: b.remove_page(-1),
                 lambda: b.swap_pages(-1, 0)):
        try:
            call()
        except Exception:
            continue
        raise AssertionError("a negative page index was accepted")


def test_a_board_always_keeps_at_least_one_page():
    """Every renderer indexes page(current); a board with none is a crash
    waiting for the next paint."""
    b = lbe.Board()
    while b.page_count > 1:
        b.remove_page(0)
    b.remove_page(0)
    assert b.page_count >= 1


# ── empty and degenerate geometry ───────────────────────────────────────────

def test_empty_strokes_are_harmless():
    for kind in (lbe.FreehandStroke, lbe.HighlighterStroke):
        s = kind()
        s.bounding_box()
        s.simplify(1.0)
        s.pop_back()
        for _ in range(20):
            s.pop_back()
        assert s.point_count == 0


def test_non_finite_points_do_not_crash():
    """A NaN reaches the engine the moment a board file carries one."""
    for bad in (float("nan"), float("inf"), float("-inf"), 1e308):
        s = lbe.FreehandStroke()
        s.add_point(lbe.Vec2f(bad, bad))
        s.add_point(lbe.Vec2f(1.0, 1.0))
        s.bounding_box()
        s.simplify(1.0)
        s.hit_test(lbe.Vec2f(0.0, 0.0), 5.0)


def test_degenerate_transforms_do_not_crash():
    for op in ("scale(0.0, 0.0)", "scale(float('inf'), float('inf'))",
               "rotate(float('nan'))", "translate(1e308, 1e308)"):
        state, detail = _in_subprocess(f"""
            s = lbe.FreehandStroke()
            for i in range(20):
                s.add_point(lbe.Vec2f(i, i))
            s.{op}
            s.bounding_box()
        """)
        assert state == "OK", f"{op} -> {state} {detail}"


# ── malformed board files ───────────────────────────────────────────────────

MALFORMED = [
    '', 'null', '[]', '{}', 'not json at all',
    '{"type":"nope"}', '{"type":123}',
    '{"type":"freehand","points":["a","b"]}',
    '{"type":"freehand","points":[1,2,3]}',
    '{"type":"freehand","points":[[1]]}',
    '{"type":"freehand","points":[[1,2,3,4,5]]}',
    '{"type":"freehand","points":42}',
    '{"type":"freehand","stroke_color":"zzz"}',
    '{"type":"freehand","stroke_color":5}',
    '{"type":"freehand","stroke_color":"#f"}',
    '{"type":"freehand","id":"not-a-uuid"}',
    '{"type":"freehand","id":[1,2,3]}',
    '{"type":"highlighter","height":-50}',
    '{"type":"polygon","vertices":[]}',
    '{"type":"image"}',
]


def test_no_malformed_object_can_crash_the_loader():
    """A .lb is a zip of JSON off a shared drive. Malformed is ordinary."""
    for blob in MALFORMED:
        try:
            lbe.object_from_json_string(blob)
        except Exception:
            pass          # refusing it is the correct outcome


def test_deeply_nested_json_does_not_blow_the_stack():
    state, detail = _in_subprocess("""
        blob = '{"type":"freehand","points":' + '[' * 400 + ']' * 400 + '}'
        try:
            lbe.object_from_json_string(blob)
        except Exception:
            pass
    """)
    assert state == "OK", f"deep nesting -> {state} {detail}"


def test_no_malformed_board_can_crash_the_loader():
    for blob in ('', 'null', '[]', '{"pages":42}', '{"pages":[1,2,3]}',
                 '{"pages":[{"objects":[1,2]}]}',
                 '{"pages":[{}],"last_page":999999}',
                 '{"pages":[]}'):
        b = lbe.Board()
        try:
            b.deserialize_from_string(blob)
        except Exception:
            continue
        # If it accepted the file, the result must still be usable — every
        # renderer will immediately index into it.
        assert b.page_count >= 1, f"{blob!r} left a board with no pages"
        b.page(max(0, min(b.last_page, b.page_count - 1)))


# ── object lifetime across Python/C++ ───────────────────────────────────────

def test_using_an_object_after_its_scene_is_cleared():
    """pybind11 hands back references into C++ containers. Clearing the
    scene while Python still holds one is a use-after-free unless the
    binding owns or copies."""
    state, detail = _in_subprocess("""
        b = lbe.Board(); b.add_page(); sc = b.page_mut(0)
        s = lbe.FreehandStroke(); s.add_point(lbe.Vec2f(0, 0))
        sc.add_object(s)
        o = list(sc.all_objects())[0]
        sc.clear()
        print(o.type_name, o.point_count)
    """)
    assert state == "OK", f"use-after-clear -> {state} {detail}"


def test_using_a_scene_after_its_page_is_removed():
    state, detail = _in_subprocess("""
        b = lbe.Board(); b.add_page(); b.add_page()
        sc = b.page_mut(1)
        b.remove_page(1)
        s = lbe.FreehandStroke(); s.add_point(lbe.Vec2f(0, 0))
        sc.add_object(s)
        print(len(list(sc.all_objects())))
    """)
    assert state == "OK", f"use-after-remove -> {state} {detail}"


def test_undo_and_redo_on_an_empty_stack():
    st = lbe.CommandStack()
    for _ in range(20):
        try:
            st.undo()
        except Exception:
            pass
        try:
            st.redo()
        except Exception:
            pass


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
