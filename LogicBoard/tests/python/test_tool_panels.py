"""
test_tool_panels.py — the Pen and Eraser toolbar buttons as toggles.

Runs under pytest, and also standalone (``python tests/python/test_tool_panels.py``)
because the repo's pytest install is currently broken on Python 3.11.

What is worth protecting here
-----------------------------
A tool button does two different jobs depending on what is already true:

  * the tool is NOT active  → select it (and open nothing)
  * the tool IS active      → open its panel
  * the tool is active AND its panel is open → put the panel away

The third case is the one that has to be kept honest. The panels are rebuilt
from scratch on every open, so a press that "reopens" is indistinguishable on
screen from one that did nothing — the panel simply looks stuck. And the
outside-press route that dismisses panels deliberately exempts the toolbar (a
press there is how you switch tools), so it was never going to close this one
either.

The other half is that a toggle must not swallow a legitimate swap: pressing
Eraser while the Pen panel is up has to leave the Eraser panel open, not
close the pen one and leave the user with nothing.
"""
import os
import sys

os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")
sys.path.insert(0, os.path.abspath(os.path.join(os.path.dirname(__file__),
                                                "..", "..")))

from PySide6.QtWidgets import QApplication

_app = QApplication.instance() or QApplication([])

from ui.main_window import MainWindow
from ui.panels.pen_panel import PenPanel
from ui.panels.eraser_panel import EraserPanel


_WIN = None


def _win(tool=None):
    """One MainWindow for the whole module — building it is not cheap, and
    nothing here depends on a fresh one as long as each test states the state
    it starts from.

    ``tool`` establishes exactly that: the tool ACTIVE and no panel open,
    which is the state the toggle is defined against. Both halves matter, and
    neither can be assumed from the previous test — these run in whatever
    order the runner picks, and every one of them ends holding some tool.
    """
    global _WIN
    if _WIN is None:
        _WIN = MainWindow()
        _WIN.show()
        _app.processEvents()
    if tool is not None:
        # One press selects it if it wasn't active, or opens its panel if it
        # was; closing after covers both without caring which happened.
        _WIN._set_tool(tool)
    _WIN._close_active_panel()
    _app.processEvents()
    return _WIN


def _open_panel(w):
    """The panel currently on screen, or None."""
    p = w._active_panel
    return p if (p is not None and p.isVisible()) else None


# ── the toggle ──────────────────────────────────────────────────────────────

def test_pressing_the_pen_button_again_closes_its_panel():
    w = _win("pen")                          # pen active, nothing open
    w._set_tool("pen")                       # tool already active -> panel opens
    assert isinstance(_open_panel(w), PenPanel), "the pen panel never opened"
    w._set_tool("pen")
    assert _open_panel(w) is None, "a second press left the pen panel open"
    w._set_tool("pen")
    assert isinstance(_open_panel(w), PenPanel), \
        "a third press should bring it back — this is a toggle, not a one-shot"


def test_pressing_the_eraser_button_again_closes_its_panel():
    w = _win("eraser")                       # eraser active, nothing open
    assert w._canvas.tool == "eraser"
    w._set_tool("eraser")
    assert isinstance(_open_panel(w), EraserPanel), "the eraser panel never opened"
    w._set_tool("eraser")
    assert _open_panel(w) is None, "a second press left the eraser panel open"
    w._set_tool("eraser")
    assert isinstance(_open_panel(w), EraserPanel), "the eraser panel did not come back"


def test_the_pen_button_toggles_in_highlighter_mode_too():
    """The pen button carries three modes (pen / highlighter / Dual Pen) and
    routes through _on_pen_button rather than _set_tool. Same control, so the
    same rule has to hold for whichever mode it is currently wearing."""
    w = _win("pen")
    w._on_pen_mode_change("highlighter")
    assert w._canvas.tool == "highlighter"
    w._close_active_panel()
    w._on_pen_button()
    assert isinstance(_open_panel(w), PenPanel), "the pen panel never opened"
    w._on_pen_button()
    assert _open_panel(w) is None, "a second press left the pen panel open"
    w._on_pen_mode_change("pen")


# ── what the toggle must NOT swallow ────────────────────────────────────────

def test_the_first_press_on_an_inactive_tool_only_selects_it():
    """Existing behaviour, and the reason the toggle is keyed on the panel
    rather than on the button: reaching for a tool you are not holding selects
    it and shows nothing."""
    w = _win("pen")
    w._set_tool("eraser")
    assert w._canvas.tool == "eraser", "the press should have switched tools"
    assert _open_panel(w) is None, "switching tools should not open a panel"


def test_pressing_the_other_tool_swaps_panels_rather_than_closing():
    """A toggle keyed on "is a panel open" instead of "is THIS panel open"
    would close the pen panel here and leave the user with nothing — one press
    wasted, and the eraser panel they asked for nowhere in sight."""
    w = _win("pen")
    w._set_tool("pen")
    assert isinstance(_open_panel(w), PenPanel)
    w._set_tool("eraser")                    # switches tool
    w._set_tool("eraser")                    # asks for the eraser panel
    assert isinstance(_open_panel(w), EraserPanel), \
        "pressing Eraser over an open Pen panel should show the eraser panel"


# ── leaving a tool retires its panel ────────────────────────────────────────

def test_switching_tools_dismisses_the_panel_of_the_tool_left_behind():
    """A panel is the settings FOR a tool. Left floating after the user moves
    on, it offers choices that apply to nothing and sits over the canvas until
    something unrelated dismisses it."""
    w = _win("eraser")
    w._set_tool("eraser")                    # eraser panel up
    assert isinstance(_open_panel(w), EraserPanel)
    w._set_tool("select")                    # …and the user moves on
    assert _open_panel(w) is None, "the eraser panel outlived the eraser"
    assert w._canvas.tool == "select"


def test_switching_away_via_the_pen_button_dismisses_the_old_panel_too():
    """The pen button doesn't go through _set_tool — it has its own switch
    path, so it needs the same rule or the fix only covers two thirds of the
    toolbar."""
    w = _win("eraser")
    w._set_tool("eraser")
    assert isinstance(_open_panel(w), EraserPanel)
    w._on_pen_button()                       # switches to the pen
    assert _open_panel(w) is None, "the eraser panel survived the pen button"
    assert w._canvas.tool in ("pen", "highlighter", "dual_pen")


def test_a_mode_change_inside_the_pen_panel_leaves_it_open():
    """Pen → Highlighter changes the canvas tool, but the user made that
    choice INSIDE the panel and is still working in it. Closing on any tool
    change at all — rather than on the press that LEAVES a tool — would shut
    the panel under their finger mid-adjustment."""
    w = _win("pen")
    w._set_tool("pen")
    assert isinstance(_open_panel(w), PenPanel)
    w._on_pen_mode_change("highlighter")
    assert w._canvas.tool == "highlighter", "the mode change didn't take"
    assert isinstance(_open_panel(w), PenPanel), \
        "the panel closed under the user while they were using it"
    w._on_pen_mode_change("pen")


def test_closing_by_toggle_leaves_the_tool_selected():
    """Putting the panel away is about the panel. The tool the user is holding
    must survive it — otherwise dismissing the panel would silently drop them
    out of the eraser."""
    w = _win("eraser")
    w._set_tool("eraser")                    # opens
    w._set_tool("eraser")                    # toggles shut
    assert _open_panel(w) is None
    assert w._canvas.tool == "eraser", "the toggle changed the active tool"


# ── closing frees the panel ─────────────────────────────────────────────────

def _settle():
    """Run pending deleteLater()s now. processEvents alone leaves them for the
    event loop proper, which these tests never return to."""
    from PySide6.QtCore import QCoreApplication, QEvent
    _app.processEvents()
    QCoreApplication.sendPostedEvents(None, QEvent.DeferredDelete)


def test_a_closed_panel_is_deleted_not_just_hidden():
    """Every open builds a fresh panel parented to the main window. While
    closing only hid it, each one stayed alive until exit — ten trips to
    Settings left ten Settings panels behind."""
    from ui.panels.settings_panel import SettingsPanel
    w = _win("pen")
    for _ in range(3):
        w._show_settings_panel(); _settle()
        w._close_active_panel(); _settle()
        w._set_tool("pen"); _settle()        # pen already active -> its panel
        w._close_active_panel(); _settle()
    for cls in (SettingsPanel, PenPanel):
        left = w.findChildren(cls)
        assert not left, f"{len(left)} closed {cls.__name__}(s) still alive"


class _SlowCheck:
    """Stands in for the network: the update check's worker sleeps instead
    of calling GitHub, so it is still running when the test closes Settings."""

    def __enter__(self):
        import threading
        from ui.util import updater
        self.updater = updater
        self.finished = threading.Event()
        self._saved = (updater.CheckWorker.run, updater.is_configured)

        def slow_run(worker):
            import time
            time.sleep(0.3)
            worker.finished.emit(None, "offline")
            self.finished.set()

        updater.CheckWorker.run = slow_run
        updater.is_configured = lambda: True
        return self

    def __exit__(self, *exc):
        self.updater.CheckWorker.run, self.updater.is_configured = self._saved

    def wait(self, seconds: float = 5.0) -> bool:
        """Wait for the worker while still pumping events, the way the real
        event loop would. Blocking the GUI thread on the Event instead left
        the worker never starting in about one run in fifteen."""
        import time
        deadline = time.time() + seconds
        while not self.finished.is_set() and time.time() < deadline:
            _settle()
            time.sleep(0.01)
        return self.finished.is_set()


def test_a_running_update_check_does_not_belong_to_the_panel():
    """Closing deletes the panel, and everything it owns goes with it. A
    QThread destroyed while it is still running aborts the process ("QThread:
    Destroyed while thread is still running"), so the update check's thread
    must not be anywhere under the panel. It was, as a child of the check,
    which was a child of the panel. Whether that abort actually fires depends
    on which Python wrappers are alive at that moment, so test the ownership
    rather than waiting for a crash that only happens sometimes."""
    from PySide6.QtCore import QThread
    with _SlowCheck() as check:
        w = _win("pen")
        w._show_settings_panel(); _settle()
        panel = w._active_panel
        panel._start_update_check()
        # A count, not the list: holding the thread's wrapper is one of the
        # things that decides whether the abort fires.
        owned = len(panel.findChildren(QThread))
        w._close_active_panel(); _settle()
        assert check.wait(), "the update check never finished"
        _settle()
    assert not owned, \
        "the update check's thread is owned by the panel, so closing kills it"


def test_closing_settings_mid_update_check_drops_the_result_cleanly():
    """The check outlives the panel and reports after it is gone. That
    result must go nowhere quietly, and the check must then clean itself up
    rather than accumulate under the application."""
    import time
    errors = []
    real_hook = sys.excepthook
    sys.excepthook = lambda t, v, tb: errors.append(f"{t.__name__}: {v}")
    try:
        with _SlowCheck() as check:
            w = _win("pen")
            w._show_settings_panel(); _settle()
            w._active_panel._start_update_check()
            w._close_active_panel(); _settle()   # panel gone, check in flight
            assert check.wait(), "the update check never finished"
            deadline = time.time() + 1.0
            while time.time() < deadline:        # let the result arrive
                _settle()
            left = _app.findChildren(check.updater.ThreadedCheck)
    finally:
        sys.excepthook = real_hook
    assert not errors, f"the late result hit the deleted panel: {errors}"
    assert not left, "the finished check was never cleaned up"


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
