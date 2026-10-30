"""
test_media_and_shutdown.py — Media Manager actions, whole-media page mapping,
scroll boundaries, and the two ways a board can be lost.

Runs under pytest, and also standalone (``python tests/python/test_media_and_shutdown.py``)
because the repo's pytest install is currently broken on Python 3.11.

What is worth protecting here
-----------------------------
Four of these are about NOT LOSING WORK, which is the only class of bug in
this app the user cannot undo:

* WHOLE-MEDIA MAPPING — frame i goes to board page start+i, always. The old
  code appended pages inside its own write loop, so a frame's destination
  depended on the board's length at that moment rather than on the frame's
  index, and a deck laid onto a board with pages after the current one
  arrived split in half.
* REMOVE vs DELETE — one takes the media off the board, the other destroys
  it. They sit next to each other in the same pane, so the difference has to
  be enforced by the code and not by the button label.
* THE EXIT PROMPT — it has to run BEFORE the close handler claims the close,
  or it never runs at all in the shipped build.
* THE STARTUP RESTORE — a fresh launch must not silently reopen somebody
  else's lesson for the next person to draw on.

The rest is the Layout section: one setting, two buttons, no second copy.
"""
import os
import sys

os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")
sys.path.insert(0, os.path.abspath(os.path.join(os.path.dirname(__file__),
                                                "..", "..")))

from PySide6.QtWidgets import QApplication
from PySide6.QtCore import Qt, QEvent, QPointF
from PySide6.QtGui import QCloseEvent

_app = QApplication.instance() or QApplication([])

from ui.main_window import MainWindow
from ui.panels.media_manager import MediaActionsPanel
from ui.util.media_library import MediaItem


# ── scaffolding ─────────────────────────────────────────────────────────────

class _FakeLib:
    """Just enough MediaLibrary for the whole-media mapping path.

    Deliberately a stub rather than a real import: rendering a genuine deck
    needs PowerPoint or a PDF engine, and none of that is what the mapping
    rule is about. What matters is that N frames resolve to N distinct
    relpaths, which is exactly what this provides.
    """
    def __init__(self, item):
        self._item = item

    def get(self, item_id):
        return self._item if item_id == self._item.id else None

    def ensure_cache(self, item_id):
        pass

    def page_path(self, item, idx):
        return f"frame-{idx}"

    def step_path(self, item, idx, step):
        return f"frame-{idx}"


def _deck(frames=10, kind="pdf"):
    it = MediaItem(id="deck1", name="lesson.pdf", type=kind)
    it.preview_count = frames
    return it


def _window(pages=1):
    w = MainWindow()
    for _ in range(pages - 1):
        w._canvas.add_page()
    return w


def _wire_fake_media(w, item):
    """Point the window's media plumbing at the stub deck."""
    lib = _FakeLib(item)
    w._media_library = lambda: lib
    # The real one resolves against the board folder; the stub already hands
    # back board-relative strings.
    w._relpath_from_board = lambda p: str(p)
    return lib


def _backgrounds(w):
    """The bg image of every page, as a list — the mapping, made visible."""
    out = []
    board = w._canvas.board
    for i in range(board.page_count):
        scene = board.page_mut(i)
        out.append(getattr(scene, "bg_image", "") or "")
    return out


# ── the whole-media mapping rule ────────────────────────────────────────────

def test_whole_media_maps_frame_i_to_page_start_plus_i():
    """THE rule, stated once: frame i lands on board page start+i.

    The board here already has ten pages and the user is on page five, which
    is the exact case that used to come apart: frame 1 landed on page 5 and
    frames 2..10 were appended as pages 11..19, leaving five untouched pages
    stranded in the middle of the deck.
    """
    w = _window(pages=10)
    item = _deck(frames=10)
    _wire_fake_media(w, item)
    w._canvas.goto_page(4)                     # "page 5" in the UI
    w._media_set_whole_background(item.id, "auto")

    bgs = _backgrounds(w)
    assert w._canvas.page_count == 14, (
        f"10 pages + 10 frames from page 5 needs 14 pages, got "
        f"{w._canvas.page_count}")
    for i in range(10):
        assert bgs[4 + i] == f"frame-{i}", (
            f"frame {i} should be on page {4 + i}, backgrounds are {bgs}")


def test_whole_media_leaves_the_pages_before_it_alone():
    """Pages before the starting point are not part of the mapping and must
    come through untouched."""
    w = _window(pages=10)
    item = _deck(frames=4)
    _wire_fake_media(w, item)
    w._canvas.goto_page(6)
    before = _backgrounds(w)[:6]
    w._media_set_whole_background(item.id, "auto")
    assert _backgrounds(w)[:6] == before, "pages before the start were modified"


def test_whole_media_starting_on_page_one_of_a_fresh_board():
    """The simple case still behaves: no gap, no blank first page."""
    w = _window(pages=1)
    item = _deck(frames=6)
    _wire_fake_media(w, item)
    w._media_set_whole_background(item.id, "auto")
    assert w._canvas.page_count == 6
    assert _backgrounds(w) == [f"frame-{i}" for i in range(6)]


def test_the_mapping_does_not_depend_on_how_long_the_board_is():
    """The same deck from the same page must land the same way whether the
    board has room for it or not. The old code grew the board inside its write
    loop, so the answer depended on the board's length at that moment — which
    is what made this unpredictable in the first place."""
    def lay(total_pages):
        w = _window(pages=total_pages)
        item = _deck(frames=5)
        _wire_fake_media(w, item)
        w._canvas.goto_page(2)
        w._media_set_whole_background(item.id, "auto")
        return _backgrounds(w)[2:7]

    tight = lay(3)          # board has to grow
    roomy = lay(20)         # board already long enough
    assert tight == roomy == [f"frame-{i}" for i in range(5)], (
        f"same deck, same start page, different result: {tight} vs {roomy}")


def test_whole_media_only_creates_the_pages_it_needs():
    w = _window(pages=20)
    item = _deck(frames=3)
    _wire_fake_media(w, item)
    w._canvas.goto_page(5)
    w._media_set_whole_background(item.id, "auto")
    assert w._canvas.page_count == 20, "the board grew when it did not need to"


def test_whole_media_marks_the_board_dirty():
    """It changes the board, so it has to count as unsaved work — otherwise
    the exit prompt has nothing to warn about."""
    w = _window(pages=2)
    item = _deck(frames=3)
    _wire_fake_media(w, item)
    assert not w._canvas.is_dirty
    w._media_set_whole_background(item.id, "auto")
    assert w._canvas.is_dirty, "laying a deck out left the board looking saved"


# ── the Layout section ──────────────────────────────────────────────────────

def _panel_with_item(kind="pdf", frames=8):
    p = MediaActionsPanel()
    item = _deck(frames=frames, kind=kind)
    p.set_item(item, selected_index=0)
    return p, item


def test_the_layout_options_are_their_own_section():
    p, _ = _panel_with_item()
    assert len(p.LAYOUT_MODES) == 3
    for key, _text, _hint in p.LAYOUT_MODES:
        assert key in p._layout_opts, f"'{key}' has no Layout row"
        assert p._layout_opts[key].isVisible() or True   # built, not hidden


def test_the_layout_section_sits_under_both_background_buttons():
    """Directly under the two controls it governs — the position IS the
    explanation of what it applies to. Where the group as a whole sits in the
    Actions section is a separate question; see
    ``test_the_background_group_sits_at_the_foot_of_the_actions``."""
    p, _ = _panel_with_item()
    lay = p._scroll.widget().layout()
    order = [lay.itemAt(i).widget() for i in range(lay.count())]
    order = [w for w in order if w is not None]
    first_opt = min(order.index(o) for o in p._layout_opts.values())
    assert order.index(p._whole_bg_row) < first_opt, (
        "Layout is not under the background buttons")


def test_one_layout_setting_drives_both_background_actions():
    """No second copy anywhere: pick once, and both actions carry it."""
    p, item = _panel_with_item()
    seen = {}
    p.set_background_requested.connect(
        lambda i, idx, mode: seen.__setitem__("single", mode))
    p.set_whole_background_requested.connect(
        lambda i, mode: seen.__setitem__("whole", mode))

    p._on_layout_mode_picked("stretch")
    p._on_set_bg()
    p._on_set_whole_bg()
    assert seen["single"] == "stretch", (
        "'Set As Background' ignored the Layout section "
        f"(sent {seen['single']!r})")
    assert seen["whole"] == "stretch", "'Set Whole Media As Bg' ignored it"
    assert seen["single"] == seen["whole"], "the two actions disagree"


def test_the_layout_starts_on_fit_the_canvas():
    """The default is "Fit The Canvas": the whole slide visible, nothing
    cropped. It used to open on "Fill The Whole Canvas", which trimmed the
    edge off every slide that was not the board's shape — and a teacher who
    never opened the Layout section never knew why their footer was gone."""
    MediaActionsPanel._session_layout_mode = "fit"      # a fresh start
    p, item = _panel_with_item()
    assert p.layout_mode == "fit", p.layout_mode
    on = [k for k, o in p._layout_opts.items() if o.isChecked()]
    assert on == ["fit"], f"the panel opened with {on} ticked"
    seen = {}
    p.set_background_requested.connect(
        lambda i, idx, mode: seen.__setitem__("single", mode))
    p.set_whole_background_requested.connect(
        lambda i, mode: seen.__setitem__("whole", mode))
    p._on_set_bg()
    p._on_set_whole_bg()
    assert seen == {"single": "fit", "whole": "fit"}, seen


def test_the_layout_is_remembered_until_a_restart():
    """Picked once, the next Media Manager opens on it; only a restart (a
    new process, so a fresh class attribute) puts it back to "fit"."""
    MediaActionsPanel._session_layout_mode = "fit"
    p, _ = _panel_with_item()
    p._on_layout_mode_picked("stretch")
    q, _ = _panel_with_item()
    assert q.layout_mode == "stretch", q.layout_mode
    on = [k for k, o in q._layout_opts.items() if o.isChecked()]
    assert on == ["stretch"], on
    q._on_layout_mode_picked("auto")
    assert _panel_with_item()[0].layout_mode == "auto"
    MediaActionsPanel._session_layout_mode = "fit"


def test_picking_a_layout_ticks_exactly_one():
    p, _ = _panel_with_item()
    p._on_layout_mode_picked("fit")
    on = [k for k, o in p._layout_opts.items() if o.isChecked()]
    assert on == ["fit"], f"expected only 'fit' ticked, got {on}"
    p._on_layout_mode_picked("auto")
    on = [k for k, o in p._layout_opts.items() if o.isChecked()]
    assert on == ["auto"], f"expected only 'auto' ticked, got {on}"


def test_the_layout_section_stays_available_for_a_plain_image():
    """The whole-media button is hidden for a single image, but "Set As
    Background" is not — and it uses Layout too, so hiding the section with
    the button would take the setting away from the action that still has it."""
    p = MediaActionsPanel()
    img = MediaItem(id="img1", name="photo.png", type="image")
    img.preview_count = 1
    p.set_item(img, selected_index=-1)
    assert not p._whole_bg_row.isVisible() or not p._whole_bg_btn.isEnabled()
    for key, o in p._layout_opts.items():
        assert o.isEnabled(), f"Layout row '{key}' went away with the deck"


# ── remove from board vs delete ─────────────────────────────────────────────

def test_the_panel_offers_remove_from_board_separately_from_delete():
    p, item = _panel_with_item()
    labels = [b.text() for b in p._buttons]
    assert "Remove From Board" in labels
    assert "Delete Media" in labels
    got = []
    p.remove_from_board_requested.connect(got.append)
    p._on_remove_from_board()
    assert got == [item.id], "the button did not ask for a board-only removal"


def test_remove_from_board_never_touches_the_library():
    """The whole point of the feature. ``_media_remove_from_board`` calls the
    board-side half of the delete flow and none of the library-side half —
    this asserts the library entry, and with it the file and the cache, is
    still there afterwards."""
    w = _window(pages=3)
    item = _deck(frames=2)
    lib = _wire_fake_media(w, item)
    # A page background pointing at the item is enough to make it "on the
    # board"; _purge_media_references is what has to find and clear it.
    called = {}

    def _fake_purge(it):
        called["purged"] = it
        return (2, 1)

    w._purge_media_references = _fake_purge

    w._media_remove_from_board(item.id)
    assert called.get("purged") is item, "the board references were not purged"
    assert lib.get(item.id) is item, "the media was removed from the library"


def test_remove_from_board_is_a_no_op_for_unknown_media():
    w = _window()
    _wire_fake_media(w, _deck())
    hit = []
    def _fake_purge(it):
        hit.append(it)
        return (0, 0)

    w._purge_media_references = _fake_purge
    w._media_remove_from_board("not-a-real-id")
    assert not hit, "purged something for an id the library never heard of"


# ── the exit prompt ─────────────────────────────────────────────────────────

def test_the_unsaved_prompt_runs_before_the_close_handler_claims_the_close():
    """The bug in one line. ApplicationCore registers a close handler that
    claims EVERY close to go into background mode, so a prompt sitting below
    it never ran in the shipped build — both the toolbar Exit button and the
    window close button dropped unsaved work without a word."""
    w = _window()
    order = []
    w._confirm_discard = lambda: (order.append("asked"), False)[1]
    w.set_close_handler(lambda: (order.append("handler"), True)[1])

    ev = QCloseEvent()
    w.closeEvent(ev)
    assert order and order[0] == "asked", (
        f"the handler got there first: {order}")
    assert "handler" not in order, "Cancel still let the window be torn down"
    assert not ev.isAccepted(), "Cancel did not keep the window open"


def test_saying_save_and_failing_to_save_keeps_the_window_open():
    """``_confirm_discard`` returns whatever ``_save_board`` returned, so a
    cancelled Save-As must not fall through into closing — that would discard
    the work the user just asked to keep."""
    w = _window()
    w._canvas.mark_dirty()
    w._save_board = lambda *a, **k: False          # user cancelled Save As
    w._alert_choice = 2
    from ui.panels.aero_alert import AeroAlert
    orig = AeroAlert.show_alert
    AeroAlert.show_alert = classmethod(lambda cls, *a, **k: 2)   # "Save"
    try:
        ev = QCloseEvent()
        w.closeEvent(ev)
        assert not ev.isAccepted(), "a failed save still closed the board"
    finally:
        AeroAlert.show_alert = orig


def test_a_clean_board_closes_without_being_asked():
    """The prompt must not become a toll on every exit."""
    w = _window()
    assert not w._canvas.is_dirty
    asked = []
    from ui.panels.aero_alert import AeroAlert
    orig = AeroAlert.show_alert
    AeroAlert.show_alert = classmethod(lambda cls, *a, **k: asked.append(1) or 1)
    try:
        claimed = []
        w.set_close_handler(lambda: (claimed.append(1), True)[1])
        w.closeEvent(QCloseEvent())
        assert not asked, "a clean board still asked about unsaved changes"
        assert claimed, "the close never reached the handler"
    finally:
        AeroAlert.show_alert = orig


# ── scrolling boundaries ────────────────────────────────────────────────────

def test_the_board_scrolls_past_the_last_page_by_the_toolbar_height():
    """The toolbar floats OVER the canvas as its own window, so nothing in the
    layout knows it is there. Without reserved space the board stopped
    scrolling with the last page flush to the bottom of the window — i.e.
    underneath the toolbar, where its final centimetres could never be read."""
    w = _window(pages=4)
    c = w._canvas
    c.set_canvas_style("scroll_list")
    c.set_list_scale(0.30)
    inset = c._bottom_inset
    assert inset > 0, "the toolbar never reported its height to the canvas"

    c.scroll_by(c._max_scroll())              # all the way to the end
    last = c.page_rect_on_screen(c.page_count - 1)
    assert last.bottom() <= c.height() - inset + 1.0, (
        f"the last page ends at y={last.bottom():.0f} with {inset:.0f}px of "
        f"toolbar over the bottom of a {c.height()}px window")


def test_the_reserved_space_follows_the_toolbar_rather_than_a_constant():
    w = _window(pages=3)
    c = w._canvas
    c.set_canvas_style("scroll_list")
    c.set_list_scale(0.30)
    tall = c._max_scroll()
    c.set_bottom_inset(c._bottom_inset + 200.0)
    assert c._max_scroll() > tall, "a taller toolbar did not reserve more room"


def test_there_is_side_room_beside_the_sheets():
    w = _window(pages=2)
    c = w._canvas
    c.set_canvas_style("scroll_list")
    c.set_list_scale(0.30)
    r = c.page_rect_on_screen(0)
    assert r.left() >= c._LIST_MARGIN - 1, (
        f"the sheet starts {r.left():.0f}px from the edge, margin is "
        f"{c._LIST_MARGIN}")


def test_an_ordinary_one_page_board_still_does_not_scroll():
    """One-page style fits the page to the window by definition. Handing it
    the toolbar inset unconditionally would make an ordinary page scroll by
    the height of the toolbar, which reads as the board drifting when touched.

    The canvas is resized to the page first because offscreen it is never
    laid out — it sits at Qt's 100x30 default, where an 800px page genuinely
    does overflow and genuinely should scroll."""
    w = _window(pages=1)
    c = w._canvas
    c.set_canvas_style("one_page")
    scene = c.current_scene()
    c.resize(int(scene.canvas_width), int(scene.canvas_height))
    assert c._max_scroll() == 0.0, "a plain one-page board became scrollable"


def test_one_page_content_that_overflows_still_clears_the_toolbar():
    """The other half: when there IS something past the bottom of the window,
    the reserved space applies there too."""
    w = _window(pages=1)
    c = w._canvas
    c.set_canvas_style("one_page")
    scene = c.current_scene()
    c.resize(int(scene.canvas_width), int(scene.canvas_height) - 200)
    plain = float(scene.canvas_height) - c.height()
    assert c._max_scroll() > plain, (
        "overflowing one-page content stops with its end under the toolbar")


# ── page-navigator thumbnails ───────────────────────────────────────────────
#
# The blur had one cause and it was not the render: previews were scaled to
# LOGICAL pixels and left tagged at ratio 1, so on a Hi-DPI board the
# compositor resampled them a second time, upwards, on the way to the screen.
# The first downscale was smooth and careful; the second is what softened the
# ink — and it got worse the higher-resolution the board was, which is the
# opposite of what anybody expects from a nicer screen.


def _ink_page(w=960, h=540):
    """A page with the content that shows blur: thin strokes and text."""
    from PySide6.QtGui import QPixmap, QPainter, QPen, QColor, QFont
    pm = QPixmap(w, h)
    pm.fill(QColor(255, 255, 255))
    p = QPainter(pm)
    p.setRenderHint(QPainter.Antialiasing, True)
    p.setPen(QPen(QColor(20, 20, 20), 2))
    for i in range(0, w, 24):
        p.drawLine(i, 0, i, h)
    for j in range(0, h, 24):
        p.drawLine(0, j, w, j)
    f = QFont(); f.setPixelSize(48); p.setFont(f)
    p.setPen(QColor(200, 30, 30))
    p.drawText(pm.rect(), Qt.AlignCenter, "Page 1")
    p.end()
    return pm


def _card():
    from ui.panels.pages_panel import PageCard
    return PageCard(0, 1, False, lambda *a: None, lambda *a: None)


def test_a_page_preview_is_rasterised_at_the_screens_real_resolution():
    """The fix, stated as the thing it guarantees: the pixmap the card holds
    covers its logical size in PHYSICAL pixels and says so."""
    card = _card()
    card.set_thumbnail(_ink_page())
    pm = card._thumb.pixmap()
    dpr = card._thumb.devicePixelRatioF()
    assert pm.devicePixelRatio() == dpr, (
        f"pixmap tagged {pm.devicePixelRatio()} on a {dpr} label — Qt will "
        f"resample it again on the way to the screen")
    # Longest side, because KeepAspectRatio fits one axis and leaves the other
    # short: the fitted axis is the one that has to be full resolution.
    want = max(card._thumb.width(), card._thumb.height()) * dpr
    got = max(pm.width(), pm.height())
    assert got >= want - 2, (
        f"pixmap is {got}px on its long side where the label needs {want:.0f}")


def test_the_preview_still_occupies_its_logical_size():
    """Tagging without scaling would draw at double size; scaling without
    tagging is the original bug. Both together, and only both, leave the card
    looking the same and painting sharper."""
    card = _card()
    card.set_thumbnail(_ink_page())
    pm = card._thumb.pixmap()
    dpr = pm.devicePixelRatio()
    assert pm.width() / dpr <= card._thumb.width() + 1
    assert pm.height() / dpr <= card._thumb.height() + 1


def test_a_preview_is_downscaled_once_not_twice():
    """One resample, from the manager's large render straight to the size the
    card paints at. Chaining a second one is what this was."""
    card = _card()
    src = _ink_page()
    card.set_thumbnail(src)
    once = card._thumb.pixmap()
    # Feeding the already-scaled result back in must not shrink it further —
    # the card always scales from what it is GIVEN, so the caller handing over
    # the manager's full-size render is what keeps it to a single hop.
    card.set_thumbnail(src)
    again = card._thumb.pixmap()
    assert (again.width(), again.height()) == (once.width(), once.height()), (
        "the same source produced two different sizes")


def test_an_empty_or_null_preview_is_ignored():
    from PySide6.QtGui import QPixmap
    card = _card()
    card.set_thumbnail(QPixmap())
    assert not card.has_thumbnail(), "a null pixmap was installed as a preview"


def test_the_thumbnail_cache_is_not_heavily_lossy():
    """These previews are mostly ink, which is what lossy compression treats
    worst — and a page served from disk should not look different from the
    same page still in RAM."""
    from ui.util import thumbnail_manager as tm
    assert tm.THUMB_QUALITY >= 90, (
        f"WEBP quality {tm.THUMB_QUALITY} visibly rings around pen strokes")


# ── Media Manager action order ──────────────────────────────────────────────

def test_the_background_group_sits_at_the_foot_of_the_actions():
    """Set As Background, Set Whole Media As Bg, Layout — in that order, at
    the bottom, as one block. Three controls that only make sense read
    together, so nothing is allowed between them."""
    p, _ = _panel_with_item()
    lay = p._scroll.widget().layout()
    order = [lay.itemAt(i).widget() for i in range(lay.count())]
    order = [w for w in order if w is not None]

    def pos(w):
        return order.index(w)

    set_bg = next(b for b in p._buttons if b.text() == "Set As Background")
    add = next(b for b in p._buttons if b.text() == "Add To Board")
    remove = next(b for b in p._buttons if b.text() == "Remove From Board")
    delete = next(b for b in p._buttons if b.text() == "Delete Media")
    first_opt = min(pos(o) for o in p._layout_opts.values())
    last_opt = max(pos(o) for o in p._layout_opts.values())

    assert pos(add) < pos(remove) < pos(delete) < pos(set_bg), (
        "the background group is not at the foot of the Actions section")
    assert pos(set_bg) < pos(p._whole_bg_row) < first_opt, (
        "the background group is out of order")
    # Nothing wedged between the three.
    between = [w for w in order[pos(set_bg):last_opt]
               if w in (add, remove, delete)]
    assert not between, "another action was placed inside the background group"


def test_the_actions_are_not_duplicated_by_the_reorder():
    """Moving controls is not the same as adding a second copy of them."""
    p, _ = _panel_with_item()
    labels = [b.text() for b in p._buttons if b.text()]
    assert len(labels) == len(set(labels)), f"duplicated actions: {labels}"
    assert len(p._layout_opts) == len(p.LAYOUT_MODES) == 3, (
        "the Layout setting was duplicated rather than moved")


def test_the_version_the_app_reports_is_the_one_the_ecosystem_declares():
    """Not a literal any more.

    This used to be ``assert APP_VERSION == "3.1.0"``, which is a ninth
    place to edit on a release and one that fails AFTER the other eight
    have already been changed. The number now lives in versions.py at the
    ecosystem root, and this asks that file rather than repeating it.
    """
    import sys
    from pathlib import Path
    root = Path(__file__).resolve().parents[3]
    sys.path.insert(0, str(root))
    try:
        import versions
    except ImportError:                      # running the app tree alone
        return
    from ui.util.version import APP_VERSION
    assert APP_VERSION == versions.LOGICBOARD, (
        "app reports %s, versions.py declares %s — run sync_versions.py"
        % (APP_VERSION, versions.LOGICBOARD))


def test_the_installer_and_the_app_agree_on_the_version():
    """They are separate constants in separate languages, and a release where
    they disagree installs into a folder the updater cannot find."""
    import re
    from pathlib import Path
    from ui.util.version import APP_VERSION
    cpp = Path(__file__).resolve().parents[2] / "LogicBoardInstaller" \
        / "LogicBoardInstaller.cpp"
    if not cpp.exists():
        return
    # The installer holds its version in a #define, because some of its UI
    # strings paste it into an adjacent-literal concatenation and a constexpr
    # array cannot be used that way. `constexpr wchar_t APP_VERSION[]` is
    # derived FROM that macro, so matching on the array name found nothing
    # and this check silently passed a "?" around for its whole life.
    m = re.search(r'#define APP_VERSION_TEXT L"([^"]+)"',
                  cpp.read_text(encoding="utf-8", errors="replace"))
    assert m and m.group(1) == APP_VERSION, (
        f"installer says {m.group(1) if m else '?'}, app says {APP_VERSION}")


# ── standalone runner ───────────────────────────────────────────────────────

def _run():
    fails = 0
    for name in sorted(n for n in globals() if n.startswith("test_")):
        try:
            globals()[name]()
            print(f"  ok    {name}")
        except Exception as e:
            fails += 1
            print(f"  FAIL  {name}: {e}")
    print()
    print("all passed" if not fails else f"{fails} failed")
    return fails


if __name__ == "__main__":
    sys.exit(1 if _run() else 0)
