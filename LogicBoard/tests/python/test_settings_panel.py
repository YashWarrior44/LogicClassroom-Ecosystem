"""
test_settings_panel.py — the Settings panel's shape, and everything it kept.

Runs under pytest, and also standalone (``python tests/python/test_settings_panel.py``)
because the repo's pytest install is currently broken on Python 3.11.

What is worth protecting here
-----------------------------
The panel was rearranged from one long scrolling column into a sidebar of
categories and one page at a time. A layout change is the easiest possible way
to silently drop a setting — the control still exists in the file, it just
never gets added to anything, and nothing fails until a teacher goes looking
for it mid-lesson. So the bulk of this file is: every category is reachable,
and every control still drives the callback it always drove.

The rest is what the hardware demands:

* TOUCH TARGETS — anything you tap is at least ``control_h_lg``.
* SCROLL vs TAP — both panes are kinetic-scrollable, and the thing that keeps
  a flick from toggling a setting is ``DragStartDistance``. Under it the press
  is a tap; over it the gesture becomes a scroll and the control never fires.
  A scroll area without that configured is a pane where every drag is a
  mis-click, so the tests check the mechanism is actually installed.
* AERO — the window is translucent, which on Windows means unpainted pixels
  are click-through holes. The hit catcher must be there, and the sidebar has
  to paint a real fill rather than relying on the blur.
"""
import os
import sys

os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")
sys.path.insert(0, os.path.abspath(os.path.join(os.path.dirname(__file__),
                                                "..", "..")))

from PySide6.QtWidgets import QApplication, QScrollArea, QPushButton
from PySide6.QtCore import Qt
from PySide6.QtGui import qAlpha

_app = QApplication.instance() or QApplication([])

from ui.panels.settings_panel import SettingsPanel
from ui.util.scaling import UiScale
from ui.theme.theme_manager import theme_mgr


# The glyph the icon tests draw. A real category emoji, so the tests
# exercise the same colour-font path the sidebar does.
EMOJI = "💾"


def _panel(**kw):
    calls = {}

    def rec(name):
        def fn(*a):
            calls[name] = a[0] if len(a) == 1 else (a or True)
        return fn

    defaults = dict(
        autosave_enabled=True,
        autosave_interval_min=5,
        touch_mode="palm_rejection",
        stylus_calibrated=False,
        eraser_calibrated=False,
        palm_calibrated=False,
        ppt_animations=False,
        mouse_is_stylus_front=False,
        object_eraser_affects_media=False,
        on_media_eraser_toggle=rec("media"),
        on_autosave_toggle=rec("autosave"),
        on_autosave_interval=rec("interval"),
        on_ppt_animations=rec("ppt"),
        on_mouse_is_stylus_front=rec("mouse_front"),
        on_touch_mode=rec("mode"),
        on_calibrate_stylus=rec("cal_stylus"),
        on_calibrate_eraser=rec("cal_eraser"),
        on_calibrate_palm=rec("cal_palm"),
    )
    defaults.update(kw)
    p = SettingsPanel(None, **defaults)
    p.resize(1000, 680)
    p.show()
    _app.processEvents()
    return p, calls


# ── the shape ───────────────────────────────────────────────────────────────

def test_every_category_has_a_nav_row_and_a_page():
    p, _ = _panel()
    for key, _emoji, _label, _sub, _colour in p.NAV:
        if key == "share":
            continue                     # only when the service is wired
        assert key in p._nav_btns, f"no sidebar row for '{key}'"
        assert key in p._pages, f"no page built for '{key}'"
    assert p._stack.count() == len(p._pages)


def test_selecting_a_category_switches_the_page_and_the_header():
    p, _ = _panel()
    for key, emoji, label, sub, _colour in p.NAV:
        if key not in p._nav_btns:
            continue
        p._select_page(key)
        _app.processEvents()
        assert p._stack.currentWidget() is p._pages[key], \
            f"'{key}' did not bring its page to the front"
        assert label in p._page_title.text(), \
            f"header shows {p._page_title.text()!r} for '{key}'"
        assert p._page_sub.text() == sub
        assert p._nav_btns[key].isChecked(), f"'{key}' row not marked current"
        others = [k for k in p._nav_btns if k != key]
        assert not any(p._nav_btns[k].isChecked() for k in others), \
            "more than one category looks selected"


def test_the_panel_opens_on_the_first_category():
    p, _ = _panel()
    assert p._current_page == p.NAV[0][0]
    assert p._stack.currentWidget() is p._pages[p.NAV[0][0]]


def test_logicshare_is_hidden_when_the_service_is_not_wired():
    """Advertising a category that opens an empty page is worse than not
    showing it — the user goes looking for a feature that isn't there."""
    p, _ = _panel(logicshare_objects=None)
    assert "share" not in p._nav_btns
    assert "share" not in p._pages


def test_logicshare_appears_when_the_service_is_wired():
    class _Any(str):
        """Answers to being read as text AND to being called as a method —
        the card does both to the service/config/log it is handed, and a
        plain lambda makes ``setText`` complain about being passed a
        function, which buries any real error in the same output."""
        def __call__(self, *a, **k):
            return _Any("")

    class _Stub:
        def __getattr__(self, _n):
            return _Any("")

    p, _ = _panel(logicshare_objects=(_Stub(), _Stub(), _Stub()))
    assert "share" in p._nav_btns, "LogicShare category missing"
    assert "share" in p._pages
    p._select_page("share")
    _app.processEvents()
    assert p._stack.currentWidget() is p._pages["share"]


# ── nothing was dropped in the move ─────────────────────────────────────────

def test_touch_mode_buttons_still_drive_the_callback():
    p, calls = _panel()
    for mode in ("palm_eraser", "palm_off", "palm_rejection"):
        p._mode_btns[mode].click()
        _app.processEvents()
        assert p._touch_mode == mode
        assert calls.get("mode") == mode, f"callback missed {mode}"
    assert p._mode_hint.text(), "the mode hint went blank"


def test_autosave_toggle_and_interval_still_work():
    p, calls = _panel()
    p._autosave_btn.click(); _app.processEvents()
    assert calls.get("autosave") is False and p._autosave_enabled is False
    # Interval controls grey out while auto-save is off — the setting has no
    # meaning then, and an enabled-looking control that does nothing is a lie.
    assert not p._interval_btns[1].isEnabled()
    p._autosave_btn.click(); _app.processEvents()
    assert p._autosave_enabled is True
    assert p._interval_btns[1].isEnabled()
    p._interval_btns[10].click(); _app.processEvents()
    assert calls.get("interval") == 10 and p._autosave_interval == 10


def test_the_remaining_toggles_still_work():
    p, calls = _panel()
    p._media_btn.click();       _app.processEvents()
    p._ppt_anim_btn.click();    _app.processEvents()
    p._mouse_front_btn.click(); _app.processEvents()
    assert calls.get("media") is True
    assert calls.get("ppt") is True
    assert calls.get("mouse_front") is True


def test_calibration_rows_fire_and_report_state():
    p, calls = _panel()
    for key in ("stylus", "eraser", "palm"):
        p._calib_btns[key][0].click()
        _app.processEvents()
        assert f"cal_{key}" in calls, f"{key} calibration didn't fire"
    p.set_calibration_state(stylus=True, eraser=True, palm=True)
    for key in ("stylus", "eraser", "palm"):
        assert "✓" in p._calib_btns[key][0].text(), \
            f"{key} row doesn't show it is calibrated"
    p.set_calibration_state(stylus=False)
    assert "Not calibrated" in p._calib_btns["stylus"][0].text()


def test_calibration_lives_inside_input_and_touch():
    """Deciding what a palm DOES and teaching the board what a palm IS are
    the same job from two ends. Split across two categories, the user set
    half of one thing in each place."""
    p, _ = _panel()
    assert "calib" not in p._nav_btns, "Calibration is still its own category"
    assert "calib" not in p._pages
    page = p._pages["input"]
    buttons = page.findChildren(QPushButton)
    for key in ("stylus", "eraser", "palm"):
        assert p._calib_btns[key][0] in buttons, \
            f"{key} calibration is not on the Input & Touch page"
    # …and the touch-mode controls are still there with it.
    for b in p._mode_btns.values():
        assert b in buttons


def test_the_small_settings_share_an_others_page():
    """A page with one switch on it reads as though something failed to
    load. Eraser scope and PowerPoint import were a category each; they
    still get a heading and a footer, they just stop being sections."""
    p, _ = _panel()
    assert "eraser" not in p._nav_btns and "ppt" not in p._nav_btns, \
        "the one-switch categories are still in the sidebar"
    assert "others" in p._pages, "no Others page"
    page = p._pages["others"]
    from ui.panels.settings_panel import _Switch
    switches = page.findChildren(_Switch)
    assert p._media_btn in switches, "the eraser setting went missing"
    assert p._ppt_anim_btn in switches, "the PowerPoint setting went missing"
    assert p._ppt_warn is not None and p._ppt_warn.text(), \
        "the experimental-import warning was dropped"


def test_settings_are_grouped_with_a_heading_and_a_footnote():
    """The iOS grouped-list shape: the section name above the rounded
    group, the explanation BELOW it. Keeping prose out of the container is
    what lets every row stay the same height."""
    from PySide6.QtWidgets import QLabel
    p, _ = _panel()
    page = p._pages["others"]
    texts = [l.text() for l in page.findChildren(QLabel) if l.text()]
    assert any(x == "ERASER" for x in texts), "no section heading"
    assert any(x == "POWERPOINT" for x in texts)
    assert any("not just ink" in x for x in texts), "no explanatory footer"


def test_toggles_are_switches_whose_position_shows_the_state():
    """"ON"/"OFF" written in a rounded rectangle is a UI imitating the look
    without the mechanism. The knob's position is the state."""
    from ui.panels.settings_panel import _Switch
    p, _ = _panel(autosave_enabled=True, object_eraser_affects_media=False)
    assert isinstance(p._autosave_btn, _Switch)
    assert p._autosave_btn.isChecked(), "an ON setting shows as off"
    assert not p._media_btn.isChecked(), "an OFF setting shows as on"
    p._media_btn.click(); _app.processEvents()
    assert p._media_btn.isChecked(), "the switch didn't follow the model"
    # It must render without a stylesheet doing the work.
    p._autosave_btn.grab()


def test_every_category_has_an_icon():
    """The emoji is the only glyph a category gets now that the colour tile
    behind it is gone, so a missing one leaves a blank space in the sidebar
    rather than a merely duller row."""
    p, _ = _panel()
    for key, b in p._nav_btns.items():
        assert not b.icon().isNull(), f"'{key}' has no icon"


def test_the_icon_has_no_tile_behind_it():
    """The emoji used to sit on a filled rounded colour tile. It doesn't any
    more — it is drawn on a transparent pixmap so the button's own background
    (and with it every hover/selected state and the theme) shows through.

    Sample the CORNERS: a rounded tile still covers them, and they are the
    one region a centred glyph never reaches, so ink there means something
    is being painted behind the emoji again."""
    p, _ = _panel()
    size = UiScale.px(30)
    img = p._emoji_icon(EMOJI, size).pixmap(size, size).toImage()
    w, h = img.width(), img.height()
    for x, y in ((1, 1), (w - 2, 1), (1, h - 2), (w - 2, h - 2)):
        assert qAlpha(img.pixel(x, y)) == 0, (
            f"pixel ({x},{y}) is painted — something sits behind the emoji")


def test_the_icon_actually_draws_its_glyph():
    """drawText paints with the PEN, and this used to run with the pen left
    at NoPen — set that way to keep the tile's edge clean — which drew a
    blank colour square and no emoji at all. With the tile gone the same
    slip gives a fully EMPTY icon, so check for painted pixels rather than
    for more than one colour."""
    p, _ = _panel()
    size = UiScale.px(30)
    icon = p._emoji_icon(EMOJI, size)
    img = icon.pixmap(size, size).toImage()
    # The interior only. The margin around the glyph is legitimately
    # transparent now that nothing fills the icon's full square.
    x0, x1 = int(img.width() * 0.25), int(img.width() * 0.75)
    y0, y1 = int(img.height() * 0.25), int(img.height() * 0.75)
    painted = sum(1 for y in range(y0, y1) for x in range(x0, x1)
                  if qAlpha(img.pixel(x, y)) > 0)
    assert painted > 0, (
        "the icon centre is fully transparent — the glyph was never painted")


def test_category_labels_survive_their_ampersand():
    """QAbstractButton reads a lone '&' as a mnemonic marker, so
    "Input & Touch" rendered as "Input  Touch" with the T underlined."""
    p, _ = _panel()
    for key, _emoji, label, _sub, _colour in p.NAV:
        if key not in p._nav_btns:
            continue
        shown = p._nav_btns[key].text()
        if "&" in label:
            assert "&&" in shown, \
                f"'{key}' label {shown!r} will lose its ampersand"
        # What Qt finally draws is the text with '&&' collapsed back to '&'.
        assert shown.replace("&&", "&").strip() == label


def test_about_renders_in_the_panel_rather_than_a_dialog():
    """It used to open a separate modal, which meant closing Settings to see
    it and having nowhere to go back to."""
    p, _ = _panel()
    assert not hasattr(p, "_show_about"), \
        "the About dialog launcher is still wired up"
    page = p._pages["about"]
    texts = " ".join(l.text() for l in page.findChildren(type(p._page_title)))
    assert "LogicBoard" in texts
    assert "open source" in texts, "About page lost its licence line"
    assert "GNU General Public License v3.0" in texts
    assert "Features" in texts, "About page lost the feature list"


def test_show_anchored_still_exists_for_the_call_site():
    """main_window calls this with the toolbar button. Signature kept."""
    p, _ = _panel()
    p.close()
    p.show_anchored(None)
    _app.processEvents()
    assert p.isVisible()


# ── updates ─────────────────────────────────────────────────────────────────

class _FakeInfo:
    latest_version = "3.1.0"
    latest_semver = None
    zip_url = ""
    release_notes = "• Sharper icons\n• Dual pen fixes"
    release_url = ""


def _force_newer(is_newer: bool):
    """Patch the version comparison so the page's three outcomes can be
    driven without a network call or a real release."""
    from ui.util import updater
    original = updater.newer_than_current
    updater.newer_than_current = lambda info: is_newer
    return original


def test_updates_page_states_the_current_version_up_front():
    p, _ = _panel()
    assert "updates" in p._pages, "Updates has no page of its own"
    assert p._app_version() in p._version_label.text(), \
        "the running version isn't shown"
    # Nothing to install until a check says otherwise.
    assert not p._install_btn.isVisible()
    assert not p._release_notes.isVisible()


def test_up_to_date_says_so_without_a_dialog():
    """The common answer used to cost three taps and two modals."""
    p, _ = _panel()
    p._select_page("updates"); _app.processEvents()
    original = _force_newer(False)
    try:
        p._on_update_checked(None, "")
    finally:
        from ui.util import updater
        updater.newer_than_current = original
    text = p._update_status.text()
    assert "up to date" in text.lower(), f"status reads {text!r}"
    assert p._app_version() in text, "up-to-date message omits the version"
    assert not p._install_btn.isVisible(), "offered an install with nothing to install"


def test_a_newer_version_offers_the_notes_and_an_install_button():
    p, _ = _panel()
    p._select_page("updates"); _app.processEvents()
    original = _force_newer(True)
    try:
        p._on_update_checked(_FakeInfo(), "")
    finally:
        from ui.util import updater
        updater.newer_than_current = original
    text = p._update_status.text()
    assert "3.1.0" in text, f"new version not announced: {text!r}"
    assert p._app_version() in text, "doesn't say what you're on now"
    assert p._install_btn.isVisible(), "no way to install the update"
    assert p._release_notes.isVisible() and "Sharper icons" in p._release_notes.text()
    assert p._pending_update is not None


def test_a_failed_check_reports_the_reason_and_offers_nothing():
    p, _ = _panel()
    p._select_page("updates"); _app.processEvents()
    p._on_update_checked(None, "network unreachable")
    assert "network unreachable" in p._update_status.text()
    assert not p._install_btn.isVisible()
    # …and the button comes back so the user can retry.
    assert p._check_btn.isEnabled()
    assert p._check_btn.text() == "Check for Updates"


def test_install_does_nothing_without_a_pending_update():
    """Guards the button's own state — it is hidden, but a stray call must
    not walk into the installer with None."""
    p, _ = _panel()
    p._pending_update = None
    p._install_update()          # must not raise


# ── touch ───────────────────────────────────────────────────────────────────

def _scroll_areas(p):
    return p.findChildren(QScrollArea)


def test_every_pane_is_kinetically_scrollable_with_tap_discrimination():
    """The mechanism that lets a nav row live inside a flickable pane: a
    press that moves less than DragStartDistance is a tap and reaches the
    button; more, and QScroller claims it as a scroll."""
    from PySide6.QtWidgets import QScroller, QScrollerProperties
    M = QScrollerProperties.ScrollMetric
    p, _ = _panel()
    areas = _scroll_areas(p)
    assert len(areas) >= len(p._pages) + 1, \
        f"expected a scroller per page plus the sidebar, found {len(areas)}"
    for area in areas:
        vp = area.viewport()
        assert QScroller.hasScroller(vp), \
            "a pane has no kinetic scroller — drags there will be dead"
        props = QScroller.scroller(vp).scrollerProperties()
        drag = props.scrollMetric(M.DragStartDistance)
        assert drag >= 0.004, \
            (f"DragStartDistance is {drag} — too small to tell a tap from a "
             f"drag, so taps will scroll instead of clicking")
        assert props.scrollMetric(M.MousePressEventDelay) == 0.0, \
            "presses are delayed, so controls inside the pane feel laggy"


def test_tappable_rows_are_big_enough_for_a_finger():
    t = theme_mgr.tokens
    assert t.control_h_lg >= 44, "the large-control token is below touch size"
    floor = UiScale.px(t.control_h_lg)
    p, _ = _panel()
    for key, b in p._nav_btns.items():
        assert b.minimumHeight() >= floor, \
            f"nav row '{key}' is {b.minimumHeight()}px, want >= {floor}"
    for key, (b, _txt) in p._calib_btns.items():
        assert b.minimumHeight() >= floor, \
            f"calibration row '{key}' is {b.minimumHeight()}px"
    small = UiScale.px(t.control_h)
    for b in (p._autosave_btn, p._media_btn, p._ppt_anim_btn,
              p._mouse_front_btn):
        assert b.height() >= small, "a toggle is below the control height"
    for b in list(p._mode_btns.values()) + list(p._interval_btns.values()):
        assert b.minimumHeight() >= small


def test_the_category_list_fills_the_sidebar():
    """The rows take up the slack themselves. A trailing stretch instead
    leaves a dead grey column under the last category, which is what this
    replaced."""
    p, _ = _panel()
    p.resize(1020, 700)
    _app.processEvents()
    rows = list(p._nav_btns.values())
    last = rows[-1]
    bottom = last.mapTo(p, last.rect().bottomLeft()).y()
    # Within a margin's worth of the bottom — not hundreds of pixels short.
    slack = p.height() - bottom
    assert slack < UiScale.px(72), \
        f"{slack}px of empty sidebar below the last category"
    assert all(b.height() >= UiScale.px(theme_mgr.tokens.control_h_lg)
               for b in rows), "a row was squeezed under the touch minimum"


def test_no_category_label_is_clipped_by_the_sidebar():
    """The sidebar sizes itself to the longest label. A fixed width is a
    guess about font, UI scale and emoji metrics at once, and only has to be
    wrong once to leave a category name cut off against the divider."""
    from PySide6.QtGui import QFontMetrics
    p, _ = _panel()
    width = p._sidebar_width()
    for key, b in p._nav_btns.items():
        need = QFontMetrics(b.font()).horizontalAdvance(b.text())
        assert need < width, \
            f"'{key}' needs {need}px of text in a {width}px sidebar"


# ── aero ────────────────────────────────────────────────────────────────────

def test_the_window_has_a_hit_catcher_when_it_is_translucent():
    """WA_TranslucentBackground makes Windows hit-test the window BY ALPHA,
    so every unpainted pixel is a hole taps fall through. The catcher is the
    1 %-alpha surface that makes the glass gaps solid to the hit tester."""
    p, _ = _panel()
    if not p.testAttribute(Qt.WA_TranslucentBackground):
        return                      # opaque platform — nothing to catch
    assert getattr(p, "_lb_hit_catcher", None) is not None, \
        "translucent settings window has no hit catcher"


def test_the_sidebar_paints_its_own_surface():
    """It must read as a distinct region, and on a translucent window
    'distinct' cannot mean 'unpainted'."""
    from PySide6.QtWidgets import QFrame
    p, _ = _panel()
    side = p.findChild(QFrame, "LBSettingsSidebar")
    assert side is not None, "sidebar frame missing"
    assert "background" in side.styleSheet(), "sidebar paints nothing"
    assert side.width() > 0


# ── teardown ────────────────────────────────────────────────────────────────

def test_no_style_is_left_for_python_to_delete():
    """A QStyle that Python owns and no Qt parent holds is deleted at
    interpreter shutdown in hash order — often before the slider still
    drawing with it, which is an access violation after the last test has
    passed. The sliders used to get one each from QStyleFactory, and this
    suite crashed on exit most runs. Whatever style the panel hands a widget
    has to belong to Qt."""
    import shiboken6
    from PySide6.QtWidgets import QStyle
    _panel()
    loose = [w.metaObject().className()
             for w in shiboken6.getAllValidWrappers()
             if isinstance(w, QStyle) and shiboken6.ownedByPython(w)]
    assert not loose, f"Python-owned styles outlive the panel: {loose}"


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
    # os._exit, not sys.exit: a normal interpreter shutdown tears the Qt
    # objects down in no particular order.
    sys.stdout.flush()
    os._exit(1 if failures else 0)
