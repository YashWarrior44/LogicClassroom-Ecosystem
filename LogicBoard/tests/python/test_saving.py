"""
test_saving.py — saving a board, and the panel that lists them.

Runs under pytest, and also standalone (``python tests/python/test_saving.py``)
because the repo's pytest install is currently broken on Python 3.11.

What is worth protecting here
-----------------------------
Saving failed SILENTLY, which is the worst way for it to fail. Open
LogicBoard, draw, press Save, type a name — and nothing happened. No file, no
error dialog, not one line in the log. The board stayed on screen looking
saved, and the work was gone at the next New or Open.

The cause was one clause. ``_write_board_json`` began:

    if board is None or self._current_board_path is None:
        return None

``_current_board_path`` is the working DIRECTORY, and on a freshly launched
board it is None until something creates one — which only happened when the
user imported media. So a lesson drawn from scratch could not be saved at
all, while one that had a picture dropped into it first saved fine. That is
why it looked intermittent rather than broken.

So the tests below are mostly about the first save on a fresh board, and
about the promise that a save which cannot happen SAYS SO. A regression here
costs a teacher their lesson, and they find out at the point where nothing
can be recovered.

The card icon is checked in the same file because it is the other half of
what the file panel shows: LogicBoard.ico holds ten frames from 16x16 up, and
``QPixmap(path)`` silently loads the FIRST one — the 16 — which every caller
then blew up to card size.
"""
import os
import sys
import tempfile
import zipfile
from pathlib import Path

os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")
sys.path.insert(0, os.path.abspath(os.path.join(os.path.dirname(__file__),
                                                "..", "..")))

from PySide6.QtWidgets import QApplication

_app = QApplication.instance() or QApplication([])

import ui.util.paths as paths


# ── scaffolding ─────────────────────────────────────────────────────────────

def _fresh_window():
    """A MainWindow whose saves directory is a throwaway temp dir.

    Redirected on the module AND on main_window, which imported the name
    directly — patching only one of the two leaves the window writing into
    the real saves folder.
    """
    tmp = Path(tempfile.mkdtemp(prefix="lb_save_test_"))
    paths.saves_dir = lambda: tmp
    import ui.main_window as mw
    mw.saves_dir = lambda: tmp
    # file_panel does `from ui.util.paths import saves_dir`, so it holds its
    # own reference from import time and would keep reading whichever temp
    # directory the FIRST test created.
    try:
        import ui.panels.file_panel as fp
        fp.saves_dir = lambda: tmp
    except Exception:
        pass
    win = mw.MainWindow()
    return win, tmp


def _alerts(win):
    """Capture what the window would have shown the user."""
    import ui.main_window as mw
    seen = []
    orig = mw.AeroAlert.warning
    mw.AeroAlert.warning = staticmethod(
        lambda parent, title, body, *a, **k: seen.append((title, body)))
    return seen, (lambda: setattr(mw.AeroAlert, "warning", orig))


# ── the first save on a fresh board ─────────────────────────────────────────

def test_a_brand_new_board_can_be_saved():
    """THE regression. No media imported, nothing opened — just draw and
    save, which is what a teacher does first on a fresh panel."""
    win, tmp = _fresh_window()
    assert win._current_board_path is None, \
        "the fixture no longer reproduces the state the bug needed"

    assert win._save_as_board("MyLesson") is True, \
        "saving a fresh board failed — the silent-save bug is back"

    lb = tmp / "MyLesson.lb"
    assert lb.exists(), f"no .lb was written; saves dir holds {list(tmp.iterdir())}"
    assert lb.stat().st_size > 0


def test_the_saved_file_is_a_real_board_archive():
    """A zero-byte or half-written .lb passes an exists() check and loses
    the lesson anyway."""
    win, tmp = _fresh_window()
    win._save_as_board("Lesson")
    with zipfile.ZipFile(tmp / "Lesson.lb") as zf:
        names = zf.namelist()
        assert "board.json" in names, names
        assert zf.testzip() is None, "the archive is corrupt"


def test_saving_creates_the_working_directory_it_needs():
    """The fix is that the workdir gets created on demand rather than the
    save bailing because it was missing."""
    win, tmp = _fresh_window()
    win._save_as_board("Lesson")
    assert win._current_board_path is not None
    assert Path(win._current_board_path).is_dir()


def test_quick_save_after_save_as_reuses_the_same_file():
    """Save must not spawn Lesson_2.lb, Lesson_3.lb on every press."""
    win, tmp = _fresh_window()
    win._save_as_board("Lesson")
    assert win._save_board() is True
    assert win._save_board() is True
    lbs = sorted(p.name for p in tmp.glob("*.lb"))
    assert lbs == ["Lesson.lb"], lbs


def test_a_second_board_of_the_same_name_does_not_overwrite_the_first():
    win, tmp = _fresh_window()
    win._save_as_board("Lesson")
    win._save_as_board("Lesson")
    lbs = sorted(p.name for p in tmp.glob("*.lb"))
    assert lbs == ["Lesson (2).lb", "Lesson.lb"], lbs


def test_the_name_the_teacher_typed_is_the_name_they_get():
    """Spaces used to become underscores, so "Class 11 Chapter 4" was saved,
    listed and reopened forever after as "Class_11_Chapter_4"."""
    win, tmp = _fresh_window()
    assert win._save_as_board("Class 11 Chapter 4") is True
    assert (tmp / "Class 11 Chapter 4.lb").exists(),         sorted(p.name for p in tmp.glob("*.lb"))
    assert win._current_board_name == "Class 11 Chapter 4"


def test_saving_clears_the_unsaved_marker():
    """The unsaved-changes prompt keys off this. If a save leaves the board
    dirty, closing asks about work that is already on disk."""
    win, tmp = _fresh_window()
    win._canvas.mark_dirty()
    assert win._canvas.is_dirty
    win._save_as_board("Lesson")
    assert not win._canvas.is_dirty


# ── a save that cannot happen has to say so ─────────────────────────────────

def test_a_failed_save_tells_the_user():
    """The whole point. Silence is what made the original bug cost work.

    Failure is injected at the zip step — a full disk, a file locked by a
    sync client, an antivirus holding the target — which is the realistic
    way a save dies once the board itself is fine.
    """
    win, tmp = _fresh_window()
    from ui.panels import saving_dialog
    seen, restore = _alerts(win)
    orig = saving_dialog.SavingDialog.run_save
    saving_dialog.SavingDialog.run_save = staticmethod(
        lambda **kw: (False, "the disk is full"))
    try:
        ok = win._save_as_board("Doomed")
    finally:
        saving_dialog.SavingDialog.run_save = orig
        restore()
    assert ok is False
    assert seen, "the save failed and the user was told nothing"
    title, body = seen[-1]
    assert "save" in title.lower()
    assert "Doomed" in body
    assert "disk is full" in body, "the reason was dropped on the way out"


def test_the_background_save_stays_quiet():
    """``silent=True`` runs while the window is being destroyed for
    background mode — there is nobody in front of a modal there."""
    win, tmp = _fresh_window()
    import ui.util.lb_format as lbf
    seen, restore = _alerts(win)
    orig = lbf.save_workdir_to_lb

    def _boom(*a, **k):
        raise OSError("no space left on device")
    lbf.save_workdir_to_lb = _boom
    try:
        ok = win._do_save(tmp / "Quiet.lb", silent=True)
    finally:
        lbf.save_workdir_to_lb = orig
        restore()
    assert ok is False
    assert not seen, "a modal was shown on the teardown path"


# ── leaving the app ─────────────────────────────────────────────────────────

def _prompt_spy(answer):
    """Answer the unsaved-changes alert, counting how often it is raised."""
    import ui.main_window as mw
    seen = []

    def spy(parent, kind, title, body, buttons=None, default=0):
        seen.append(title)
        return answer

    orig = mw.AeroAlert.show_alert
    mw.AeroAlert.show_alert = staticmethod(spy)
    return seen, (lambda: setattr(mw.AeroAlert, "show_alert", orig))


def test_leaving_with_unsaved_work_asks_once_not_twice():
    """THE regression.

    Every route out closes the window more than once: closeEvent asks,
    ApplicationCore's handler claims the close for background mode, and
    hide_ui then calls win.close() itself — re-entering closeEvent. Choosing
    "Don't Save" leaves the board dirty, so the second pass asked again.
    """
    win, _tmp = _fresh_window()
    win._canvas.mark_dirty()
    seen, restore = _prompt_spy(0)          # Don't Save
    try:
        assert win.confirm_close() is True
        assert win.confirm_close() is True   # what the re-close hits
        assert win.confirm_close() is True
    finally:
        restore()
    assert len(seen) == 1, f"asked {len(seen)} times"


def test_the_board_is_still_dirty_after_choosing_dont_save():
    """Why the second prompt appeared at all — the answer has to be
    remembered, because the board's state cannot imply it."""
    win, _tmp = _fresh_window()
    win._canvas.mark_dirty()
    seen, restore = _prompt_spy(0)
    try:
        win.confirm_close()
    finally:
        restore()
    assert win._canvas.is_dirty,         "the fixture no longer reproduces the state the bug needed"


def test_cancelling_the_exit_does_not_commit_the_window_to_closing():
    win, _tmp = _fresh_window()
    win._canvas.mark_dirty()
    seen, restore = _prompt_spy(1)          # Cancel
    try:
        assert win.confirm_close() is False
        assert win._closing is False
        assert win.confirm_close() is False  # asks again next time
    finally:
        restore()
    assert len(seen) == 2


def test_a_clean_board_never_asks_at_all():
    win, _tmp = _fresh_window()
    seen, restore = _prompt_spy(0)
    try:
        assert win.confirm_close() is True
    finally:
        restore()
    assert seen == []


def test_the_tray_quit_goes_through_the_same_gate():
    """It prompts, then shutdown -> hide_ui -> win.close() re-enters
    closeEvent. Same double-ask unless both share the recorded answer."""
    import inspect
    from ui import application_core
    src = inspect.getsource(application_core.ApplicationCore._quit_from_tray)
    assert "confirm_close" in src,         "tray quit asks its own question again instead of the recorded one"


# ── startup always begins on a clean board ──────────────────────────────────

def test_launching_does_not_reopen_the_previous_board():
    """A shared classroom panel must not hand the next person the last
    person's lesson — anything they draw would go into that file."""
    import inspect
    from ui import application_core
    src = inspect.getsource(application_core.ApplicationCore.show_ui)
    assert "_open_board_path" not in src,         "show_ui reopens a board again"
    assert "_restore_on_next_show" not in inspect.getsource(application_core),         "the restore flag is back"


def test_a_new_window_starts_with_no_board_file():
    win, _tmp = _fresh_window()
    assert win._current_lb_path is None
    assert win._current_board_name is None


def test_hiding_still_saves_the_board_so_nothing_is_lost():
    """Starting clean is only acceptable because the work is on disk and at
    the top of the Files panel, newest first."""
    import inspect
    from ui import application_core
    src = inspect.getsource(application_core.ApplicationCore.hide_ui)
    assert "_save_current_board" in src,         "hide_ui stopped saving, so starting clean now loses work"


# ── the panel that lists them ───────────────────────────────────────────────

def test_the_file_panel_lists_a_saved_board():
    win, tmp = _fresh_window()
    win._save_as_board("Physics")
    from ui.panels.file_panel import FilePanel
    panel = FilePanel(parent=win)
    names = [p.name for p, _card in panel._board_buttons]
    assert "Physics.lb" in names, names
    panel.close()


def test_the_panel_shows_nothing_when_there_is_nothing():
    win, tmp = _fresh_window()
    from ui.panels.file_panel import FilePanel
    panel = FilePanel(parent=win)
    assert panel._board_buttons == []
    panel.close()


# ── the card icon ───────────────────────────────────────────────────────────

def test_the_card_logo_is_not_a_blown_up_16_pixel_frame():
    """logo.ico holds ten frames. QPixmap(path) loads the FIRST — the 16 —
    and scaling that to a 28-pixel card is a 1.75x upscale of an icon that
    has a 256x256 master sitting right there in the same file."""
    from ui.panels.file_panel import _lb_card_pixmap
    pm = _lb_card_pixmap(28)
    if pm is None:
        return                      # no icon in this tree; nothing to check
    # The pixmap must be at least the size asked for, in real pixels.
    assert pm.width() >= 28, \
        f"card logo is only {pm.width()}px — it is being upscaled again"


def test_the_logo_helper_uses_a_frame_big_enough_for_what_was_asked():
    from ui.util.icon_loader import app_logo_pixmap
    for size in (28, 56, 84):
        pm = app_logo_pixmap(size)
        if pm is None:
            return
        assert pm.width() >= size, \
            f"asked for {size}px and got {pm.width()}px of real image"


def test_the_logo_is_laid_out_at_the_size_that_was_asked_for():
    """Rendering at device resolution is only correct if the ratio is
    stamped on the pixmap — otherwise it overflows its label instead of
    being sharp inside it."""
    from ui.util.icon_loader import app_logo_pixmap
    pm = app_logo_pixmap(28)
    if pm is None:
        return
    logical = pm.width() / pm.devicePixelRatio()
    assert abs(logical - 28) <= 1, \
        f"a 28px request lays out at {logical}px and will be clipped"


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
