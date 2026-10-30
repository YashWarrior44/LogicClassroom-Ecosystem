"""
test_autosave_toast.py — autosave notifies, it does not interrupt.

Runs under pytest, and also standalone (``python tests/python/test_autosave_toast.py``)
because the repo's pytest install is currently broken on Python 3.11.

What is worth protecting here
-----------------------------
Autosave used to take the save path built for a save the user ASKED for: a
centred modal with a spinner, blocking input until the zip finished. Nobody is
waiting on an autosave, so nobody should be stopped by one — it now runs on a
worker thread and says "Auto Saved" in the corner for three seconds.

The interesting parts are not the toast. They are the two things that stop
being true the moment a save stops blocking the app:

  * the board stays live while the zip runs, so it can be EDITED mid-save.
    Those edits are not in the archive being written, so the unsaved-changes
    flag must survive the save that didn't include them.
  * two writers can now reach the same .lb — a manual save, a close, or the
    next tick landing on top of one still running. All of them go through the
    same file, and half of them run on the UI thread.
"""
import os
import shutil
import sys
import tempfile
import time
from pathlib import Path

os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")
sys.path.insert(0, os.path.abspath(os.path.join(os.path.dirname(__file__),
                                                "..", "..")))

from PySide6.QtWidgets import QApplication, QDialog

_app = QApplication.instance() or QApplication([])

import logicboard_engine as lbe
from ui.main_window import MainWindow
from ui.panels.toast import Toast
from ui.util.size_manager import size_mgr as S


_WIN = None
_TMP = None


def _board():
    """A window backed by a throwaway workdir + .lb target, with ink on it.

    Module-scoped: building a MainWindow is not cheap. Each test re-arms the
    state it cares about rather than relying on the previous one.
    """
    global _WIN, _TMP
    if _WIN is None:
        _WIN = MainWindow()
        _WIN.resize(1400, 800)
        _WIN.show()
        _app.processEvents()
        _TMP = Path(tempfile.mkdtemp(prefix="lb_autosave_test_"))
        (_TMP / "work").mkdir()
    _WIN._current_board_path = _TMP / "work"
    _WIN._current_lb_path = _TMP / "board.lb"
    _WIN._current_board_name = "board"
    return _WIN, _TMP / "board.lb"


def _ink(w, n=1):
    s = w._canvas.current_scene()
    for k in range(n):
        o = lbe.FreehandStroke()
        o.set_points([(10.0 + i, 20.0 + k * 15) for i in range(30)])
        o.stroke_width = 3.0
        o.stroke_color = lbe.RGBA(255, 0, 0, 255)
        s.add_object(o)
    w._canvas.mark_dirty()


def _settle(w, timeout_s=30.0):
    """Spin the event loop until the autosave worker is done."""
    worker = w._autosave_worker
    end = time.time() + timeout_s
    while worker is not None and worker.isRunning() and time.time() < end:
        _app.processEvents()
        time.sleep(0.01)
    _app.processEvents()


class _NoModals:
    """Fail loudly if anything opens a modal dialog inside the block."""

    def __enter__(self):
        self._real = QDialog.exec

        def boom(_self, *a):
            raise AssertionError("a modal dialog was opened")

        QDialog.exec = boom
        return self

    def __exit__(self, *exc):
        QDialog.exec = self._real
        return False


# ── it notifies ─────────────────────────────────────────────────────────────

def test_autosave_shows_a_toast_and_no_modal():
    w, lb = _board()
    _ink(w)
    with _NoModals():
        w._auto_save()
        _settle(w)
    assert lb.exists(), "the autosave never wrote the .lb"
    toast = getattr(w, "_toast", None)
    assert isinstance(toast, Toast), "no toast was posted"
    assert toast._label.text() == "Auto Saved"
    assert toast.isVisible()


def test_the_toast_sits_in_the_top_left_and_scales():
    """Top-left of the window, at a margin derived from the SizeManager — so
    it lands in the same visual spot on a 1080p laptop and a 4K board."""
    w, _ = _board()
    toast = Toast.show_message(w, "Auto Saved", 3000)
    _app.processEvents()
    m = S.scale_margin(22)
    assert toast.pos().x() == m and toast.pos().y() == m, \
        f"toast at {toast.pos()}, expected the scaled margin {m}"
    # Comfortably inside the window's top-left quadrant, not centred.
    assert toast.geometry().right() < w.width() // 2
    assert toast.geometry().bottom() < w.height() // 2


def test_the_toast_never_swallows_input():
    """It floats over the canvas. A rectangle of board that silently refuses
    ink for three seconds would be a worse interruption than the modal."""
    w, _ = _board()
    from PySide6.QtCore import Qt
    toast = Toast.show_message(w, "Auto Saved", 3000)
    assert toast.testAttribute(Qt.WA_TransparentForMouseEvents)


def test_repeated_saves_reuse_one_toast():
    """A timer posts these. Building a widget per tick would quietly stack
    them in the corner for the life of the session."""
    w, _ = _board()
    for _ in range(4):
        Toast.show_message(w, "Auto Saved", 200)
        _app.processEvents()
    assert len([c for c in w.children() if isinstance(c, Toast)]) == 1


def test_the_toast_goes_away_on_its_own():
    w, _ = _board()
    toast = Toast.show_message(w, "Auto Saved", 120)
    _app.processEvents()
    assert toast.isVisible()
    end = time.time() + 3.0
    while toast.isVisible() and time.time() < end:
        _app.processEvents()
        time.sleep(0.02)
    assert not toast.isVisible(), "the toast never dismissed itself"


# ── it does not interrupt ───────────────────────────────────────────────────

def test_autosave_does_not_block_the_ui_thread():
    """The whole point. The zip is seconds of work on a media-heavy board;
    _auto_save must hand it to a thread and return immediately."""
    w, _ = _board()
    _ink(w, 3)
    t0 = time.time()
    w._auto_save()
    elapsed_ms = (time.time() - t0) * 1000
    running = w._autosave_worker is not None and w._autosave_worker.isRunning()
    _settle(w)
    assert elapsed_ms < 250, f"_auto_save blocked the UI thread for {elapsed_ms:.0f} ms"
    assert running, "the zip did not go to a worker thread"


def test_the_board_stays_unsaved_when_edited_during_the_save():
    """board.json was written before the zip started, so a stroke drawn while
    it runs is NOT in the archive. Clearing the flag would call it saved — and
    worse, the next tick would see a clean board and skip it for good."""
    w, _ = _board()
    _ink(w)
    w._auto_save()
    _ink(w)                                   # …drawn while the zip runs
    _settle(w)
    assert w._canvas.is_dirty, \
        "an edit made during the save was marked as saved"


def test_a_clean_save_clears_the_unsaved_flag():
    """The other half — otherwise the title would carry its unsaved marker
    forever no matter how often autosave ran."""
    w, _ = _board()
    _ink(w)
    w._auto_save()
    _settle(w)
    assert not w._canvas.is_dirty, "a completed autosave left the board dirty"


def test_a_tick_that_lands_mid_save_is_skipped():
    """Same work, one writer. Stacking a second is only possible now that the
    save no longer blocks."""
    w, _ = _board()
    _ink(w, 4)
    w._auto_save()
    first = w._autosave_worker
    w._auto_save()
    assert w._autosave_worker is first, "a second autosave started on top of one"
    _settle(w)


def test_a_manual_save_waits_for_an_autosave_in_flight():
    """Both write the same .lb via the same .lb.tmp. Racing them is how a
    board gets truncated."""
    w, lb = _board()
    _ink(w, 4)
    w._auto_save()
    w._wait_for_autosave()
    assert not w._autosave_worker.isRunning(), \
        "the manual-save path proceeded with an autosave still writing"
    assert lb.exists()
    assert not list(lb.parent.glob("*.tmp")), "a .lb.tmp was left behind"


def test_autosave_skips_a_board_that_was_never_saved():
    """Existing behaviour: no .lb path means no name to save under, and we
    don't invent one."""
    w, _ = _board()
    w._current_lb_path = None
    _ink(w)
    before = w._autosave_worker
    w._auto_save()
    assert w._autosave_worker is before, "autosaved a board with no file"
    assert w._canvas.is_dirty


if __name__ == "__main__":
    failures = []
    try:
        for name, fn in sorted(globals().items()):
            if not name.startswith("test_") or not callable(fn):
                continue
            try:
                fn()
                print(f"  ok    {name}")
            except AssertionError as exc:
                failures.append(name)
                print(f"  FAIL  {name}: {exc}")
            except Exception as exc:                   # noqa: BLE001
                failures.append(name)
                print(f"  ERROR {name}: {type(exc).__name__}: {exc}")
    finally:
        if _TMP is not None:
            shutil.rmtree(_TMP, ignore_errors=True)
    print(f"\n{len(failures)} failed" if failures else "\nall passed")
    sys.exit(1 if failures else 0)
