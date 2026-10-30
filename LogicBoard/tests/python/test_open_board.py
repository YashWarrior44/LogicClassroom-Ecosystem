"""
test_open_board.py — a failed open must not cost you the lesson you had open.

Runs under pytest, and also standalone
(``python tests/python/test_open_board.py``).

What is worth protecting here
-----------------------------
A .lb is opened by extracting it into a working directory. The workdir is not
scratch space: it holds the imported pictures, the rendered PDF pages, and
board.json itself. It IS the open lesson.

``_open_board`` used to release the previous workdir and THEN extract the new
one, so every failure below the extract landed on a board whose contents had
already been deleted — and the window carried on displaying it, titled, with
``_current_board_path`` pointing at a directory that no longer existed.

``is_lb_file`` runs first and catches the common corrupt file, which is why a
truncated download survived and this went unnoticed. It does not catch:

  * an archive that opens fine but holds an unsafe member path,
  * a network drive that drops between the check and the extract,
  * a full disk during extraction,
  * a permission error.

Reproduced with the first of those — a real zip, containing board.json, plus
one traversal member. The picture imported into the open lesson was gone.

THE RULE
========
The previous workdir is released only after the new board is loaded and
``_current_board_path`` has moved to it. Until then a failure is just a
failure.

AND, SEPARATELY
===============
``board.name`` reads like it comes from the file and does not — the engine's
``load_from_file`` never touches it. With "Chemistry Term 1" open, opening
"Physics Term 2.lb" put "Chemistry Term 1" in the title bar, and that is the
name the next Save would have used.
"""
import json
import os
import sys
import tempfile
import zipfile
from pathlib import Path

os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")
sys.path.insert(0, os.path.abspath(os.path.join(os.path.dirname(__file__),
                                                "..", "..")))

from PySide6.QtWidgets import QApplication
from PySide6.QtGui import QImage, QColor

_app = QApplication.instance() or QApplication([])

import ui.util.paths as paths


def _window():
    tmp = Path(tempfile.mkdtemp(prefix="lb_open_"))
    paths.saves_dir = lambda: tmp
    import ui.main_window as mw
    mw.saves_dir = lambda: tmp
    import ui.panels.file_panel as fp
    fp.saves_dir = lambda: tmp
    # The failure dialogs must not block a headless run.
    mw.AeroAlert.warning = staticmethod(lambda *a, **k: None)
    win = mw.MainWindow()
    win.resize(900, 640)
    return win, tmp


def _lesson_with_a_picture(win, name="Lesson 3"):
    """A saved board whose workdir holds an imported image — the thing that
    is actually lost when a workdir is deleted."""
    board_dir = Path(win._ensure_board_dir())
    img = QImage(400, 300, QImage.Format_RGB32)
    img.fill(QColor("steelblue"))
    img.save(str(board_dir / "diagram.png"))
    assert win._save_as_board(name) is True
    return Path(win._current_board_path)


def _lb(tmp, filename, payload=None, extra=None):
    p = tmp / filename
    with zipfile.ZipFile(p, "w") as z:
        z.writestr("board.json", json.dumps(
            payload if payload is not None
            else {"pages": [{"objects": []}], "last_page": 0}))
        for member, data in (extra or {}).items():
            z.writestr(member, data)
    return p


# ── the regression ──────────────────────────────────────────────────────────

def test_a_failed_open_leaves_the_current_lesson_untouched():
    """THE regression. The archive below passes ``is_lb_file`` — it is a
    real zip and it contains board.json — and is then refused during
    extraction for its traversal member."""
    win, tmp = _window()
    wd = _lesson_with_a_picture(win)
    from ui.util.lb_format import is_lb_file
    bad = _lb(tmp, "Lesson 4.lb", extra={"../../elsewhere.txt": "x"})
    assert is_lb_file(bad), "fixture no longer gets past the pre-check"

    win._open_board(bad)

    assert wd.exists(), "the open lesson's working directory was deleted"
    assert (wd / "diagram.png").exists(), \
        "the picture imported into the open lesson was deleted"
    assert Path(win._current_board_path).exists(), \
        "_current_board_path points at a directory that no longer exists"
    assert win._current_board_name == "Lesson 3", \
        "the failed open changed which board the window thinks it has"


def test_a_failed_open_does_not_leave_a_half_extracted_workdir():
    """The other side of it — refusing the new board must not leave its
    debris behind either."""
    win, tmp = _window()
    _lesson_with_a_picture(win)
    before = len(list((tmp / ".working").glob("*")))
    win._open_board(_lb(tmp, "Bad.lb", extra={"../../x.txt": "x"}))
    after = len(list((tmp / ".working").glob("*")))
    assert after == before, \
        "a failed open left %d extra working directories" % (after - before)


def test_a_zip_with_no_board_json_is_refused_without_collateral():
    win, tmp = _window()
    wd = _lesson_with_a_picture(win)
    p = tmp / "NotABoard.lb"
    with zipfile.ZipFile(p, "w") as z:
        z.writestr("readme.txt", "hello")
    win._open_board(p)
    assert wd.exists() and (wd / "diagram.png").exists()


def test_a_truncated_download_is_refused_without_collateral():
    """This one was already safe, because ``is_lb_file`` catches it. Kept
    so the pre-check cannot quietly stop running."""
    win, tmp = _window()
    wd = _lesson_with_a_picture(win)
    p = _lb(tmp, "Half.lb")
    raw = p.read_bytes()
    p.write_bytes(raw[:len(raw) // 2])
    win._open_board(p)
    assert wd.exists() and (wd / "diagram.png").exists()


def test_a_missing_file_is_refused_without_collateral():
    win, tmp = _window()
    wd = _lesson_with_a_picture(win)
    win._open_board(tmp / "NeverExisted.lb")
    assert wd.exists() and (wd / "diagram.png").exists()


# ── a SUCCESSFUL open must still do all of its work ─────────────────────────

def test_a_good_open_swaps_the_board_and_releases_the_old_workdir():
    """Deferring the cleanup must not turn into never doing it — otherwise
    every board opened in a session stays on disk."""
    win, tmp = _window()
    old = _lesson_with_a_picture(win)
    win._open_board(_lb(tmp, "Lesson 5.lb"))
    new = Path(win._current_board_path)
    assert new != old, "the window did not move to the new workdir"
    assert new.exists()
    assert not old.exists(), "the previous working directory was never released"


def test_opening_several_boards_does_not_pile_up_workdirs():
    win, tmp = _window()
    _lesson_with_a_picture(win)
    for i in range(5):
        win._open_board(_lb(tmp, "Board %d.lb" % i))
    live = list((tmp / ".working").glob("*"))
    assert len(live) == 1, \
        "%d working directories left on disk: %s" % (len(live),
                                                     [w.name for w in live])
    assert Path(win._current_board_path) == live[0]


def test_a_failed_open_between_two_good_ones_does_not_leak_either():
    win, tmp = _window()
    _lesson_with_a_picture(win)
    win._open_board(_lb(tmp, "Good A.lb"))
    win._open_board(_lb(tmp, "Bad.lb", extra={"../../x.txt": "x"}))
    win._open_board(_lb(tmp, "Good B.lb"))
    live = list((tmp / ".working").glob("*"))
    assert len(live) == 1, [w.name for w in live]
    assert win._current_board_name == "Good B"


# ── the board's name ────────────────────────────────────────────────────────

def test_the_title_follows_the_file_that_was_opened():
    """THE second regression."""
    win, tmp = _window()
    win._ensure_board_dir()
    win._save_as_board("Chemistry Term 1")
    assert win._current_board_name == "Chemistry Term 1"

    win._open_board(_lb(tmp, "Physics Term 2.lb"))
    assert win._current_board_name == "Physics Term 2", (
        "opening Physics Term 2.lb left the title reading %r"
        % win._current_board_name)
    assert "Physics Term 2" in win.windowTitle()


def test_the_name_does_not_survive_from_one_board_to_the_next():
    """Three opens in a row: each title must be its own file's."""
    win, tmp = _window()
    win._ensure_board_dir()
    win._save_as_board("First")
    for stem in ("Second", "Third", "Fourth"):
        win._open_board(_lb(tmp, stem + ".lb"))
        assert win._current_board_name == stem, (
            "after opening %s.lb the title read %r"
            % (stem, win._current_board_name))


def test_an_empty_name_in_board_json_still_falls_back_to_the_filename():
    win, tmp = _window()
    win._ensure_board_dir()
    win._save_as_board("First")
    win._open_board(_lb(tmp, "Has Empty Name.lb",
                        payload={"pages": [{"objects": []}], "name": ""}))
    assert win._current_board_name == "Has Empty Name"


def test_the_engine_board_name_is_blanked_before_every_load():
    """The mechanism, in case the fallback above ever starts passing for
    the wrong reason."""
    import inspect
    import ui.main_window as mw
    for fn in (mw.MainWindow._open_board, mw.MainWindow._open_board_path):
        body = inspect.getsource(fn).split('"""')[-1]
        assert 'board.name = ""' in body, \
            "%s does not clear the board name before loading" % fn.__name__
        # Compare against the CALL, not the bare word: the comment above the
        # reset explains what load_from_file does and does not do, and
        # searching for the name alone finds that prose first, then reports
        # the wrong order for code that is perfectly correct.
        i = body.index('board.name = ""')
        j = body.index("board.load_from_file(")
        assert i < j, \
            "%s clears the name AFTER loading" % fn.__name__


# ── the silent reopen path gets the same treatment ──────────────────────────

def test_the_silent_reopen_path_also_extracts_before_it_tears_down():
    """``_open_board_path`` is what ApplicationCore uses on relaunch. It
    had the identical ordering."""
    import inspect
    import ui.main_window as mw
    body = inspect.getsource(mw.MainWindow._open_board_path).split('"""')[-1]
    i = body.index("open_lb_to_workdir")
    j = body.index("cleanup_workdir(prev_wd)")
    assert i < j, \
        "_open_board_path still deletes the previous workdir before extracting"


def test_the_silent_reopen_path_keeps_the_lesson_on_failure():
    win, tmp = _window()
    wd = _lesson_with_a_picture(win)
    ok = win._open_board_path(_lb(tmp, "Bad.lb", extra={"../../x.txt": "x"}))
    assert ok is False
    assert wd.exists() and (wd / "diagram.png").exists(), \
        "the silent reopen path destroyed the open lesson"


if __name__ == "__main__":
    import logging
    logging.disable(logging.ERROR)
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
    sys.exit(1 if failures else 0)
