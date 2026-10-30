"""
test_aero_hit_catcher.py — every aero surface has a backing that covers it.

Runs under pytest, and also standalone (``python tests/python/test_aero_hit_catcher.py``)
because the repo's pytest install is currently broken on Python 3.11.

What is worth protecting here
-----------------------------
``WA_TranslucentBackground`` makes Qt create a ``WS_EX_LAYERED`` window with
per-pixel alpha, and Windows hit-tests layered windows BY ALPHA. A pixel with
alpha 0 is not merely invisible — it is not there, and the tap goes to
whatever window is behind. A panel that paints nothing of its own therefore
only responds where a child happened to draw something, and every glass gap
between its controls is a hole.

On a desk that reads as "the click did nothing". On a wall-mounted board with
a canvas behind every panel, it reads as ink: a tap that misses a button by a
few millimetres draws a dot on the lesson.

So this file sweeps the app rather than testing one panel. For every
translucent top-level it asserts a catcher exists AND that it covers the whole
client rect — a catcher sized to a stale geometry is the same bug with extra
steps.

The catcher does a second job now. The Windows blur was scrapped (see
ui/util/aero.py, EFFECT), and the catcher IS the panel body: a solid, rounded,
lit-from-above surface. So this file also pins that it is opaque — over
nothing, a translucent body just shows the canvas through the panel.
"""
import os
import sys

os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")
sys.path.insert(0, os.path.abspath(os.path.join(os.path.dirname(__file__),
                                                "..", "..")))

from PySide6.QtWidgets import QApplication
from PySide6.QtCore import Qt

_app = QApplication.instance() or QApplication([])

from ui.theme.theme_manager import theme_mgr


def _settle(w, ms=250):
    import time
    w.show()
    end = time.time() + ms / 1000.0
    while time.time() < end:
        _app.processEvents()
        time.sleep(0.01)


def _surfaces():
    """One instance of every translucent top-level the app shows.

    Built lazily inside the test so an import error in one panel names that
    panel instead of taking the whole module down with it.
    """
    from ui.panels.settings_panel import SettingsPanel
    from ui.panels.pen_panel import PenPanel
    from ui.panels.eraser_panel import EraserPanel
    from ui.panels.file_panel import FilePanel
    from ui.panels.shapes_panel import ShapesPanel
    from ui.panels.about_dialog import AboutDialog
    from ui.panels.saving_dialog import SavingDialog
    from ui.panels.update_dialog import UpdateCheckDialog

    return [
        ("SettingsPanel",     SettingsPanel(None)),
        ("PenPanel",          PenPanel(None)),
        ("EraserPanel",       EraserPanel(None)),
        ("FilePanel",         FilePanel(None)),
        ("ShapesPanel",       ShapesPanel(None)),
        ("AboutDialog",       AboutDialog(None)),
        ("SavingDialog",      SavingDialog(None)),
        ("UpdateCheckDialog", UpdateCheckDialog(None)),
    ]


def _logicshare_surfaces():
    """Every LogicShare window. Separate from ``_surfaces`` only because
    these need a config / activity log to construct."""
    import tempfile
    from pathlib import Path
    from ui.logicshare.config import LSConfig
    from ui.logicshare.activity_log import ActivityLog
    from ui.logicshare.notifications import (FileNotification,
                                             AnnouncementNotification)
    from ui.logicshare.settings_section import (PasswordPromptDialog,
                                                ChangePasswordDialog,
                                                EditClassDetailsDialog,
                                                HistoryDialog)
    from ui.logicshare.setup_wizard import SetupWizard

    tmp = Path(tempfile.mkdtemp(prefix="lb_ls_test_"))
    cfg = LSConfig(tmp / "settings.json")
    log = ActivityLog(tmp / "log.json")
    info = {"name": "w.pdf", "size": 12, "sender": "A", "device": "D",
            "path": "x", "title": "T", "body": "B", "time": "now",
            "bytes": 12, "filename": "w.pdf"}
    return [
        ("FileNotification",   FileNotification(info, tmp)),
        ("Announcement",       AnnouncementNotification(info)),
        ("PasswordPrompt",     PasswordPromptDialog(cfg)),
        ("ChangePassword",     ChangePasswordDialog(cfg)),
        ("EditClassDetails",   EditClassDetailsDialog(cfg)),
        ("HistoryDialog",      HistoryDialog(log, tmp, None, cfg)),
        ("SetupWizard",        SetupWizard(cfg)),
    ]


def test_every_logicshare_window_is_backed_too():
    """LogicShare's windows are built from their own aero base classes, not
    from the panel ones, so "the panels are covered" says nothing about them.
    They are also the surfaces most likely to appear over someone else's
    work — a notification pops up unannounced while a lesson is on screen.

    ``HistoryDialog`` earns its place here twice: it sets
    ``WA_TranslucentBackground`` on a CHILD scroll area and its host widget,
    which paints nothing of its own. Without the window-wide catcher behind
    it, the empty parts of that list would be alpha 0 — holes straight
    through the dialog.
    """
    missing, short = [], []
    for name, w in _logicshare_surfaces():
        _settle(w, 200)
        if not w.testAttribute(Qt.WA_TranslucentBackground):
            continue
        frame = getattr(w, "_lb_hit_catcher", None)
        if frame is None:
            missing.append(name)
        elif frame.geometry() != w.rect():
            short.append(f"{name} ({frame.geometry().size().toTuple()} "
                         f"vs {w.rect().size().toTuple()})")
    assert not missing, "LogicShare windows with no hit catcher: " + ", ".join(missing)
    assert not short, "LogicShare catchers not covering: " + ", ".join(short)


def test_every_translucent_surface_has_a_catcher_covering_it():
    missing, short = [], []
    for name, w in _surfaces():
        _settle(w)
        if not w.testAttribute(Qt.WA_TranslucentBackground):
            continue                     # opaque platform — nothing to catch
        frame = getattr(w, "_lb_hit_catcher", None)
        if frame is None:
            missing.append(name)
            continue
        if frame.geometry() != w.rect():
            short.append(f"{name} ({frame.geometry().size().toTuple()} "
                         f"vs {w.rect().size().toTuple()})")
    assert not missing, "translucent windows with no hit catcher: " + ", ".join(missing)
    assert not short, "catchers not covering their window: " + ", ".join(short)


def test_the_catcher_still_covers_after_a_resize():
    """A window built at one size and grown later is the case a
    show-time-only catcher gets wrong."""
    from ui.panels.settings_panel import SettingsPanel
    w = SettingsPanel(None)
    _settle(w)
    for size in ((900, 600), (1400, 900), (760, 520)):
        w.resize(*size)
        _app.processEvents()
        frame = getattr(w, "_lb_hit_catcher", None)
        if frame is None:
            return                       # opaque platform
        assert frame.geometry() == w.rect(), \
            f"catcher {frame.geometry()} != window {w.rect()} at {size}"


def _panel_surface():
    from ui.panels.settings_panel import SettingsPanel
    w = SettingsPanel(None)
    w.resize(700, 500)
    _settle(w)
    return w, getattr(w, "_lb_hit_catcher", None)


def test_the_panel_body_is_solid():
    """The blur is gone, so nothing shows through the panel any more — and
    nothing may: a translucent body over the canvas reads as a hole with
    controls floating in it."""
    from ui.util import aero
    assert aero.EFFECT == "none", (
        "a backdrop effect is back on (%r); the surfaces are designed for "
        "no blur" % aero.EFFECT)
    for name in ("surface_top", "surface_bottom", "hit_catcher", "aero_tint",
                 "aero_tint_strong", "dialog_bg", "bg_overlay", "toolbar_bg"):
        c = theme_mgr.qcolor(getattr(theme_mgr.tokens, name))
        assert c.alpha() == 255, "%s is translucent (alpha %d)" % (name, c.alpha())
    w, frame = _panel_surface()
    assert frame is not None, "the panel has no surface"
    img = frame.grab().toImage()
    mid = img.pixelColor(img.width() // 2, img.height() // 2)
    assert mid.alpha() == 255, "the painted body is see-through (alpha %d)" % mid.alpha()


def test_the_body_is_lit_from_above():
    """Lighter at the top than the bottom, like every iOS surface."""
    w, frame = _panel_surface()
    img = frame.grab().toImage()
    x = img.width() // 2
    top = img.pixelColor(x, 12).lightness()
    bottom = img.pixelColor(x, img.height() - 12).lightness()
    assert top > bottom, f"body top {top} is not lighter than bottom {bottom}"


def test_the_body_has_rounded_see_through_corners():
    """The window is translucent so its CORNERS can be — the body is a
    rounded shape, not a rectangle."""
    w, frame = _panel_surface()
    # The WINDOW, not the surface child: grabbing a child paints the
    # palette behind it, which says nothing about what is on screen.
    img = w.grab().toImage()
    assert img.pixelColor(0, 0).alpha() < 40, "the top-left corner is square"
    assert img.pixelColor(img.width() - 1, img.height() - 1).alpha() < 40, \
        "the bottom-right corner is square"


def test_the_edge_is_a_soft_hairline_brighter_on_top():
    """The edge is painted as ONE path (a QSS border with a radius composites
    its corners twice), brighter along the top — the lit bevel."""
    w, frame = _panel_surface()
    img = frame.grab().toImage()
    x = img.width() // 2
    top_edge = img.pixelColor(x, 0).lightness()
    just_in = img.pixelColor(x, 3).lightness()
    assert top_edge > just_in, "there is no lit edge along the top"


def test_the_toolbar_pill_is_not_a_hole():
    """The toolbar paints ``background: transparent`` and leans on the blur,
    so without a catcher its padding is alpha 0 — and it sits directly over
    the canvas, where a tap that misses a button leaves ink."""
    from ui.toolbar.toolbar_widget import ToolbarWidget
    tb = ToolbarWidget(parent=None)
    groups = [g for g in (getattr(tb, "left", None), getattr(tb, "center", None),
                          getattr(tb, "right", None)) if g is not None]
    assert groups, "no toolbar groups to check"
    for g in groups:
        _settle(g)
        if not g.testAttribute(Qt.WA_TranslucentBackground):
            continue
        frame = getattr(g, "_lb_hit_catcher", None)
        assert frame is not None, f"toolbar group {g.objectName()} has no catcher"
        assert frame.geometry() == g.rect(), \
            f"toolbar group {g.objectName()} catcher doesn't cover the pill"


def test_no_panel_strokes_an_outline_inside_itself():
    """The floating panels are clipped and rounded by DWM. An overlay that
    strokes its own rounded rect inside the client area draws at the PANEL's
    radius while the compositor clips at the SYSTEM's, so the two disagree
    and the arc cuts across the interior — a stray outline at each corner,
    visible from across the room."""
    from ui.util.rounded_border import RoundedBorder
    from ui.panels.pen_panel import PenPanel
    from ui.panels.eraser_panel import EraserPanel
    from ui.panels.file_panel import FilePanel
    from ui.panels.shapes_panel import ShapesPanel

    offenders = []
    for name, w in (("PenPanel", PenPanel(None)),
                    ("EraserPanel", EraserPanel(None)),
                    ("FilePanel", FilePanel(None)),
                    ("ShapesPanel", ShapesPanel(None))):
        _settle(w, 200)
        if w.findChildren(RoundedBorder):
            offenders.append(name)
    assert not offenders, \
        "panels stroking their own outline: " + ", ".join(offenders)


def test_the_catcher_paints_and_never_eats_a_click():
    """It is a SURFACE, not a control: it paints itself and never takes
    focus from the real controls."""
    w, frame = _panel_surface()
    assert frame is not None
    assert frame.focusPolicy() == Qt.NoFocus
    assert type(frame).paintEvent is not type(frame).__mro__[1].paintEvent, \
        "the surface does not paint its own body"


def test_the_toolbar_keeps_its_small_corners():
    """The toolbar groups keep the small corner they always had — a full
    pill was tried and looked wrong."""
    from ui.toolbar.toolbar_widget import ToolbarWidget
    tb = ToolbarWidget(parent=None)
    for g in (tb.left, tb.center, tb.right):
        _settle(g)
        frame = g._lb_hit_catcher
        r = frame.corner_radius()
        assert r <= 10, f"toolbar corner radius is {r}; it should stay small"


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
