"""
test_page_reorder.py — dragging a page, inserting one, and not freezing.

Runs under pytest, and also standalone
(``python tests/python/test_page_reorder.py``).

THREE THINGS

1. MOVING A PAGE MOVES ITS UNDO STACK
   ``_command_stacks`` is a list indexed by page number. A page that
   changes index without its stack going with it inherits the history of
   whatever used to live there, and the next undo edits a page the user is
   not looking at. Nothing on screen would show that had happened.

2. INSERTING A PAGE SHIFTS EVERYTHING AFTER IT
   Including the thumbnail cache, which is also keyed by index — so every
   preview past the insertion point is attached to the wrong page until it
   is invalidated. The stack list has to grow in the right place too.

3. THE 29-SECOND FREEZE
   Laying a 1000-page PDF across a board and then switching to list style
   took 29 seconds, because ``page_layout()`` asks ``_page_size`` for every
   page and ``_page_size`` used to DECODE each page's background just to
   read its dimensions. Nothing about the layout needs the pixels.
"""
import os
import sys
import tempfile
import time
from pathlib import Path

os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")
sys.path.insert(0, os.path.abspath(os.path.join(os.path.dirname(__file__),
                                                "..", "..")))

from PySide6.QtWidgets import QApplication
from PySide6.QtGui import QImage, QColor
from PySide6.QtCore import QPoint, Qt

_app = QApplication.instance() or QApplication([])

import logicboard_engine as lbe
import ui.util.paths as paths
from ui.util.scaling import UiScale


def _window(pages=6, tag=True):
    tmp = Path(tempfile.mkdtemp(prefix="lb_reorder_"))
    paths.saves_dir = lambda: tmp
    import ui.main_window as mw
    mw.saves_dir = lambda: tmp
    import ui.panels.file_panel as fp
    fp.saves_dir = lambda: tmp
    for n in ("warning", "question", "information", "critical"):
        setattr(mw.AeroAlert, n, staticmethod(lambda *a, **k: mw.AeroAlert.Yes))
    win = mw.MainWindow()
    win.resize(1500, 900)
    win.show()
    _app.processEvents()
    c = win._canvas
    while c.page_count < pages:
        c.add_page()
    # Tag each page so its identity survives a reorder. Skipped for the
    # big boards the virtualisation tests use: those never ask WHICH page
    # they are looking at, and a stroke per page is the bulk of the setup
    # cost once the board is in the hundreds.
    if tag:
        for i in range(c.page_count):
            o = lbe.FreehandStroke()
            o.set_points([(float(i * 10), 5.0), (float(i * 10 + 3), 9.0)])
            c.scene_at(i).add_object(o)
    return win, tmp


def _order(canvas):
    """The tag on each page, in page order. -1 for a blank page."""
    out = []
    for i in range(canvas.page_count):
        objs = list(canvas.scene_at(i).all_objects())
        out.append(int(objs[0].points()[0][0] // 10) if objs else -1)
    return out


# ── an inserted page is the same size as the ones around it ────────────────

def _sizes(canvas):
    out = []
    for i in range(canvas.page_count):
        sc = canvas.scene_at(i)
        out.append((int(sc.canvas_width or 0), int(sc.canvas_height or 0)))
    return out


def test_a_page_inserted_in_the_middle_matches_its_neighbours():
    """The reported bug: pages inserted by right-click came out at sizes
    that matched nothing, seemingly at random.

    The size was not being computed wrongly — it was being READ from a
    dangling reference. ``page_mut`` returns a C++ reference into a
    ``std::deque``, and ``add_page_at`` emplaces into the MIDDLE of that
    deque, which invalidates every reference to every element. The old code
    took the neighbour's handle BEFORE the insert and read its dimensions
    after, so what it copied was whatever happened to occupy that storage.
    """
    win, _tmp = _window(pages=5)
    c = win._canvas
    # A4-ish portrait, i.e. NOT the widget's own 16:9 shape — so a page that
    # falls back to the screen size is unmistakable.
    for i in range(c.page_count):
        c.scene_at(i).set_canvas_size(1240, 1754)
    before = _sizes(c)
    assert len(set(before)) == 1, before

    for at in (0, 3, c.page_count):
        assert c.insert_page_at(at) is True
        sizes = _sizes(c)
        assert len(set(sizes)) == 1, (
            f"inserting at {at} produced mixed page sizes: {sorted(set(sizes))}")
        assert sizes[0] == (1240, 1754), sizes[0]


def test_inserting_many_times_never_drifts():
    """Once is luck; a dangling read is intermittent by nature, so hammer it."""
    win, _tmp = _window(pages=4)
    c = win._canvas
    for i in range(c.page_count):
        c.scene_at(i).set_canvas_size(1240, 1754)
    for k in range(25):
        at = (k * 7) % (c.page_count + 1)
        assert c.insert_page_at(at) is True
    sizes = _sizes(c)
    assert len(set(sizes)) == 1, f"drifted to {sorted(set(sizes))}"
    assert sizes[0] == (1240, 1754)


def test_the_inserted_page_lands_where_it_was_asked_for():
    win, _tmp = _window(pages=5)
    c = win._canvas
    assert _order(c) == [0, 1, 2, 3, 4]
    assert c.insert_page_at(2) is True
    assert _order(c) == [0, 1, -1, 2, 3, 4]


def test_inserting_into_an_empty_board_does_not_crash():
    """n == 0 means there is no neighbour to copy, and the guard for that
    was tangled up with the handle that had to go."""
    win, _tmp = _window(pages=1)
    c = win._canvas
    assert c.insert_page_at(0) is True
    assert c.page_count == 2


# ── moving ──────────────────────────────────────────────────────────────────

def test_a_page_moves_to_where_it_was_dropped():
    win, _tmp = _window()
    c = win._canvas
    assert _order(c) == [0, 1, 2, 3, 4, 5]
    assert c.move_page(1, 4) is True
    assert _order(c) == [0, 2, 3, 4, 1, 5]


def test_a_page_moves_upward_too():
    win, _tmp = _window()
    c = win._canvas
    assert c.move_page(4, 1) is True
    assert _order(c) == [0, 4, 1, 2, 3, 5]


def test_moving_to_the_same_place_changes_nothing():
    win, _tmp = _window()
    c = win._canvas
    before = _order(c)
    assert c.move_page(2, 2) is False
    assert _order(c) == before


def test_out_of_range_moves_are_refused():
    win, _tmp = _window()
    c = win._canvas
    before = _order(c)
    for src, dst in ((-1, 2), (0, 99), (99, 0), (0, -3)):
        assert c.move_page(src, dst) is False, "%d->%d was accepted" % (src, dst)
    assert _order(c) == before


def test_the_undo_stack_travels_with_its_page():
    """THE one that is invisible when it breaks."""
    win, _tmp = _window()
    c = win._canvas
    stacks = list(c._command_stacks)
    c.move_page(1, 4)
    moved = list(c._command_stacks)
    expected = [stacks[0], stacks[2], stacks[3], stacks[4], stacks[1],
                stacks[5]]
    assert moved == expected, (
        "the undo stacks did not follow their pages — undo would edit a "
        "page the user is not looking at")


def test_the_view_follows_the_page_that_was_dragged():
    win, _tmp = _window()
    c = win._canvas
    c.goto_page(1)
    c.move_page(1, 4)
    assert c.current_page == 4, \
        "the view stayed on index 1, which now holds a different page"


def test_a_move_around_the_current_page_keeps_it_in_view():
    """Dragging some OTHER page past the one being looked at must not
    change which page is on screen."""
    win, _tmp = _window()
    c = win._canvas
    c.goto_page(3)
    before = _order(c)[3]
    c.move_page(0, 5)
    assert _order(c)[c.current_page] == before, \
        "the visible page changed when a different page was moved"


def test_moving_never_loses_or_duplicates_a_page():
    win, _tmp = _window(8)
    c = win._canvas
    import random
    rng = random.Random(4)
    for _ in range(30):
        c.move_page(rng.randrange(c.page_count), rng.randrange(c.page_count))
        tags = _order(c)
        assert sorted(tags) == list(range(8)), \
            "pages went missing or doubled: %s" % tags
        assert len(c._command_stacks) == c.page_count


# ── inserting ───────────────────────────────────────────────────────────────

def test_a_page_is_inserted_where_it_was_asked_for():
    win, _tmp = _window(4)
    c = win._canvas
    assert c.insert_page_at(2) is True
    assert _order(c) == [0, 1, -1, 2, 3]


def test_inserting_at_zero_and_at_the_end():
    win, _tmp = _window(3)
    c = win._canvas
    c.insert_page_at(0)
    assert _order(c) == [-1, 0, 1, 2]
    c.insert_page_at(c.page_count)
    assert _order(c) == [-1, 0, 1, 2, -1]


def test_an_out_of_range_insert_is_clamped_not_refused():
    """The panel computes this from a click position, so a value past the
    end means "at the end" rather than "do nothing"."""
    win, _tmp = _window(3)
    c = win._canvas
    n = c.page_count
    assert c.insert_page_at(999) is True
    assert c.page_count == n + 1
    assert _order(c)[-1] == -1


def test_the_inserted_page_gets_its_own_undo_stack():
    win, _tmp = _window(4)
    c = win._canvas
    stacks = list(c._command_stacks)
    c.insert_page_at(2)
    assert len(c._command_stacks) == c.page_count
    assert c._command_stacks[2] not in stacks, \
        "the new page is sharing another page's undo history"
    assert c._command_stacks[3] is stacks[2], \
        "the pages after the insertion point lost their stacks"


def test_the_inserted_page_matches_the_sheet_size_around_it():
    """A board built from a PDF is a stack of A4 sheets; a 16:9 page
    dropped into the middle looks like a mistake."""
    win, _tmp = _window(3)
    c = win._canvas
    for i in range(c.page_count):
        c.scene_at(i).set_canvas_size(1240, 1754)
    c.insert_page_at(1)
    fresh = c.scene_at(1)
    assert (fresh.canvas_width, fresh.canvas_height) == (1240, 1754), \
        "the new page is %dx%d among 1240x1754 sheets" % (
            fresh.canvas_width, fresh.canvas_height)


def test_main_window_invalidates_the_previews_it_has_to():
    """Everything from the insertion point on has shifted by one, so every
    cached preview past it now belongs to the wrong page."""
    win, _tmp = _window(5)
    seen = []
    win._thumb_manager.invalidate = lambda idx: seen.append(list(idx))
    win._insert_page(2)
    assert seen, "no previews were invalidated after an insert"
    assert min(seen[0]) <= 2, "the invalidation starts after the new page"
    assert max(seen[0]) >= win._canvas.page_count - 1, \
        "the pages at the end were left showing the wrong preview"

    seen.clear()
    win._move_page(1, 4)
    assert seen and set(range(1, 5)).issubset(set(seen[0])), \
        "a move did not invalidate every page between the two ends"


# ── the panel only builds what you can see ──────────────────────────────────

def _built(panel):
    return [i for i, c in enumerate(panel._cards) if c is not None]


def test_a_long_board_does_not_get_a_widget_per_page():
    """THE performance fix. A QVBoxLayout holding one card per page did not
    survive a long board: 25 pages rebuilt in 114 ms, 100 in 555 ms, 400 in
    3.8 s and 1000 in 18.8 s — 4.5 ms per card climbing to 18.8. Building a
    PageCard on its own is flat at 1.35 ms however many already exist, so
    the cost was entirely the scroll area and the layout managing N
    children, and the only fix is to stop handing them N children."""
    win, _tmp = _window(400, tag=False)
    panel = _panel(win)
    total = win._canvas.page_count
    built = _built(panel)
    assert len(built) < 60, (
        "%d of %d cards were built; the panel is not virtualised"
        % (len(built), total))
    assert len(panel._cards) == total, "the panel forgot how long the board is"


def test_every_page_still_has_a_slot_even_without_a_card():
    """The scrollbar range comes from the whole board, not from the part of
    it that happens to be built — otherwise the list looks as short as its
    realised window."""
    win, _tmp = _window(300, tag=False)
    panel = _panel(win)
    expected = panel._pad * 2 + 300 * panel._card_h + 299 * panel._gap
    assert panel._content.height() == expected, (
        "content is %d px, expected %d for 300 pages"
        % (panel._content.height(), expected))
    assert panel._scroll.verticalScrollBar().maximum() > 0


def test_scrolling_builds_the_pages_you_scrolled_to():
    win, _tmp = _window(400, tag=False)
    panel = _panel(win)
    bar = panel._scroll.verticalScrollBar()

    bar.setValue(0)
    assert 0 in _built(panel), "the top of the board is not built"

    # valueChanged is delivered synchronously, so the window has already
    # been rebuilt by the time setValue returns. No processEvents here: the
    # panel scrolls itself to the current page on a 100 ms timer, and
    # letting that fire would move the bar out from under the assertion.
    bar.setValue(bar.maximum())
    built = _built(panel)
    assert 399 in built, "the last page has no card at the bottom of the list"
    assert 0 not in built, "the first page is still built from 400 pages away"


def test_the_page_you_jump_to_gets_built():
    """_scroll_to_card cannot hand the scroll area a widget any more — the
    whole point is that a page far down the board has none yet."""
    win, _tmp = _window(400, tag=False)
    panel = _panel(win)
    panel._scroll_to_card(350)
    assert panel._cards[350] is not None, "jumped to page 350 and built nothing"


def test_opening_a_two_thousand_page_board_is_not_a_freeze():
    """A guard on the thing that was actually wrong, at the size the product
    is meant to carry. Generous — this is about catching a return to
    per-page widgets, not about defending a millisecond."""
    import time
    win, _tmp = _window(2000, tag=False)
    panel = _panel(win)
    t0 = time.perf_counter()
    panel._rebuild()
    _app.processEvents()
    ms = (time.perf_counter() - t0) * 1000
    assert ms < 1500, (
        "rebuilding a 2000-page panel took %.0f ms; a widget per page cost "
        "18,800 ms at 1000 pages, and virtualised it is ~115 ms at any "
        "length" % ms)


def test_a_card_recycled_onto_another_page_tells_the_truth_about_it():
    """A far jump replaces the whole visible window, so cards are re-pointed
    at new pages rather than rebuilt — 54 ms of construction became 2 ms of
    property writes. The hazard that buys is a card still showing the page
    it USED to be."""
    from PySide6.QtGui import QPixmap
    win, _tmp = _window(400, tag=False)
    win._canvas.goto_page(7)
    panel = _panel(win)
    bar = panel._scroll.verticalScrollBar()

    # Give everything on screen a preview, so a stale one would be obvious.
    for card in panel._cards:
        if card is not None:
            pm = QPixmap(10, 10)
            pm.fill()
            card.set_thumbnail(pm)

    bar.setValue(bar.maximum())          # every card gets recycled

    for i, card in enumerate(panel._cards):
        if card is None:
            continue
        assert card._index == i, (
            "slot %d holds a card that still thinks it is page %d"
            % (i, card._index + 1))
        assert card._name_lbl.text() == "Page %d" % (i + 1), (
            "slot %d is labelled %r" % (i, card._name_lbl.text()))
        assert not card.has_thumbnail(), (
            "page %d is showing the preview of the page this card used to be"
            % (i + 1))
        assert card._is_current == (i == 7), (
            "slot %d thinks current=%s" % (i, card._is_current))


def test_a_recycled_card_deletes_the_page_it_now_shows():
    """The delete button used to close over the index the card was BUILT
    with, which after recycling is a different page entirely."""
    win, _tmp = _window(400, tag=False)
    panel = _panel(win)
    bar = panel._scroll.verticalScrollBar()
    bar.setValue(bar.maximum())
    asked = []
    panel._on_delete = lambda i: asked.append(i)
    card = next(c for c in panel._cards if c is not None)
    # Captured BEFORE the call: deleting rebuilds the column, which
    # re-points this very card at a different page.
    page = card._index
    card._on_delete(page)
    assert asked == [page], asked


def test_far_jumps_stop_building_widgets_once_warmed_up():
    """The measurement behind the recycling: jumping to the far end of a
    1000-page board cost 54 ms rebuilding the whole visible window, and 92 ms
    to reach an arbitrary page. Counting constructions is steadier than
    timing them.

    Warmed up first, deliberately. The window is SMALLER at the ends of the
    board — there is no margin past page 1 or the last page — so the first
    trip to the middle genuinely grows the working set from about ten cards
    to about sixteen. Those are first-touch allocations, not churn. What
    must be zero is everything after that.
    """
    import ui.panels.pages_panel as pp
    win, _tmp = _window(400, tag=False)
    panel = _panel(win)
    bar = panel._scroll.verticalScrollBar()

    # Warm-up: touch the end, the top and the middle so every window size
    # this board can ask for has been seen once.
    for value in (bar.maximum(), 0, bar.maximum() // 2):
        bar.setValue(value)

    built = [0]
    real = pp.PageCard.__init__

    def counting(self, *a, **k):
        built[0] += 1
        return real(self, *a, **k)

    pp.PageCard.__init__ = counting
    try:
        for value in (bar.maximum(), 0, bar.maximum() // 3,
                      bar.maximum(), bar.maximum() // 2, 0):
            bar.setValue(value)
    finally:
        pp.PageCard.__init__ = real

    assert built[0] == 0, (
        "%d cards were constructed for six far jumps on a warmed-up panel; "
        "they should all have been re-pointed at their new pages" % built[0])


def test_the_spare_card_pool_stays_bounded():
    """Recycling must not turn into hoarding: the pool is capped at one
    window's worth however far the board is scrolled."""
    win, _tmp = _window(400, tag=False)
    panel = _panel(win)
    bar = panel._scroll.verticalScrollBar()
    for k in range(30):
        bar.setValue((k * 7919) % max(1, bar.maximum()))
    live = sum(1 for c in panel._cards if c is not None)
    assert len(panel._card_pool) <= max(live, panel._pool_cap), (
        "pool holds %d spares against a window of %d"
        % (len(panel._card_pool), live))
    assert len(panel._card_pool) < 64, (
        "pool grew to %d spares" % len(panel._card_pool))


# ── the panel's gesture and menu maths ──────────────────────────────────────

def _panel(win):
    win._show_page_options()
    _app.processEvents()
    _app.processEvents()
    return win._active_panel


def _settle(panel, frames=400):
    """Run the slide to rest, synchronously — the drop and the insert are
    animated now, and a test must not depend on wall-clock frames."""
    for _ in range(frames):
        if not panel._slide.isActive():
            break
        panel._slide_step()
    _app.processEvents()


def test_a_long_press_picks_a_card_up_and_a_release_puts_it_down():
    win, _tmp = _window(8)
    panel = _panel(win)
    card = panel._cards[1]
    start = card.mapToGlobal(QPoint(card.width() // 2, card.height() // 2))

    panel._begin_drag(1, start)
    assert panel._drag_ghost is not None, "no ghost was created"
    # NOT `not card.isVisible()`. The card used to be hidden, and hiding it
    # is what broke the drag: a hidden widget loses the implicit mouse grab
    # it earned by being pressed, so the moves and the release stopped
    # arriving. It is parked outside the content instead — just as absent
    # from the stack, still able to receive events.
    assert card.isVisible(),         "the lifted card was hidden — that drops its mouse grab mid-drag"
    assert not panel._content.rect().intersects(card.geometry()),         "the lifted card is still sitting in the stack as well"
    # There is no layout to freeze any more — the stack positions its
    # cards by hand whether or not a drag is running, which is a
    # consequence of virtualising it. What still has to be true is that
    # the panel knows a drag is in progress.
    assert panel._drag_src == 1, "the panel did not take the card"

    panel._drag_moved(start + QPoint(0, panel._drag_slot_h * 3))
    assert panel._drag_to == 4, "drop slot is %s" % panel._drag_to

    panel._end_drag(start + QPoint(0, panel._drag_slot_h * 3))
    _app.processEvents()
    assert _order(win._canvas) == [0, 2, 3, 4, 1, 5, 6, 7]


def test_a_long_press_still_arms_while_the_scroller_is_merely_PRESSED():
    """THE reason drag-to-reorder never worked at all.

    QScroller has four states and a press puts it straight into ``Pressed``,
    which means "a finger went down and I have not decided what this is" —
    not "the list is scrolling". A long press to pick up a card lives
    entirely inside that state.

    ``_scroller_busy`` used to be ``state != Inactive``, so it called that
    busy. The card still ARMED on press, because it gets the press while the
    scroller is a moment behind — which is exactly why tapping a page went on
    working and hid this — but 380 ms later ``_hold_elapsed`` asked again,
    found ``Pressed``, and gave up. The card was never picked up.

    Driven through QScroller's own ``handleInput`` so the state machine is
    the real one, not a stand-in.
    """
    from PySide6.QtWidgets import QScroller
    from PySide6.QtCore import QPointF
    win, _tmp = _window(10)
    panel = _panel(win)
    vp = panel._scroll.viewport()
    card = panel._cards[2]
    sc = QScroller.scroller(vp)

    assert card._scroller_busy() is False, "busy while nothing is happening"

    sc.handleInput(QScroller.Input.InputPress, QPointF(50.0, 200.0), 0)
    assert sc.state() == QScroller.State.Pressed,         "QScroller did not enter Pressed; this test is measuring nothing"
    assert card._scroller_busy() is False, (
        "a bare press counts as 'the list is busy', so the long-press timer "
        "never survives to start a drag")
    sc.handleInput(QScroller.Input.InputRelease, QPointF(50.0, 200.0), 400)


def test_a_press_while_the_list_is_coasting_is_still_hands_off():
    """The other half of the same rule: pressing a list that is mid-flick is
    a 'stop the scroll' gesture and must never become a tap or a drag."""
    from PySide6.QtWidgets import QScroller
    from PySide6.QtCore import QPointF
    win, _tmp = _window(40, tag=False)
    panel = _panel(win)
    vp = panel._scroll.viewport()
    card = panel._cards[0]
    sc = QScroller.scroller(vp)
    # Press, move far and fast, release -> a flick.
    sc.handleInput(QScroller.Input.InputPress, QPointF(50.0, 400.0), 0)
    sc.handleInput(QScroller.Input.InputMove, QPointF(50.0, 200.0), 40)
    busy_mid_drag = card._scroller_busy()
    sc.handleInput(QScroller.Input.InputRelease, QPointF(50.0, 120.0), 80)
    assert busy_mid_drag is True,         "dragging the list is not reported as busy; a tap could fire mid-scroll"
    sc.stop()


def test_the_kinetic_scroller_lets_go_while_a_card_is_being_dragged():
    """THE reorder bug. A drag-to-reorder and a flick-to-scroll want the
    same gesture — one pointer, pressed and moved — and QScroller wins that
    contest by design: its recognizer watches the viewport and claims the
    gesture the moment the pointer passes its drag threshold, sending the
    child a synthetic release. So a card picked up by a long press froze on
    its first millimetre: the ghost stopped following, no card slid, and the
    drop never landed, because every event after the first went to the
    scroller.

    Nothing caught it because every other drag test calls _begin_drag and
    _drag_moved directly, which is exactly the path a gesture recognizer
    cannot interfere with.

    What is asserted here is the panel's side of the bargain — that it
    borrows the gesture and gives it back. Qt exposes no way to ask a widget
    whether a gesture is grabbed (``hasScroller`` answers whether a QScroller
    OBJECT exists, which stays true either way), so the release itself is
    checked through ``suspend_touch_scroll``'s own report.
    """
    from PySide6.QtWidgets import QScroller
    from ui.util.touch_scroll import suspend_touch_scroll, resume_touch_scroll
    win, _tmp = _window(8)
    panel = _panel(win)
    vp = panel._scroll.viewport()
    assert QScroller.hasScroller(vp),         "no scroller on the list — this test is no longer measuring anything"

    card = panel._cards[2]
    at = card.mapToGlobal(card.rect().center())
    panel._begin_drag(2, at)
    assert panel._scroller_suspended is True,         "the drag did not take the gesture off the scroller; it will be stolen"

    panel._end_drag(at + QPoint(0, panel._drag_slot_h))
    _app.processEvents()
    assert panel._scroller_suspended is False,         "flick-scrolling was never handed back"
    # And it really is grabbed again: suspending now must find something to
    # release, which it can only do if resume actually re-grabbed.
    assert suspend_touch_scroll(panel._scroll) is True,         "resume_touch_scroll did not restore the gesture"
    resume_touch_scroll(panel._scroll)


def test_an_abandoned_drag_gives_the_scroller_back_too():
    """_finish_drag_visuals also runs when a drag is abandoned — the panel
    closing, a rebuild arriving from elsewhere. Leaving the gesture borrowed
    would kill scrolling for the rest of the session."""
    from ui.util.touch_scroll import suspend_touch_scroll, resume_touch_scroll
    win, _tmp = _window(6)
    panel = _panel(win)
    panel._begin_drag(1, panel._cards[1].mapToGlobal(QPoint(5, 5)))
    assert panel._scroller_suspended is True
    panel._finish_drag_visuals()          # abandoned, not dropped
    assert panel._scroller_suspended is False,         "scrolling stayed broken after an aborted drag"
    assert suspend_touch_scroll(panel._scroll) is True
    resume_touch_scroll(panel._scroll)


def test_suspending_a_list_with_no_scroller_is_a_no_op():
    """resume must not conjure a grab onto something that never had one."""
    from PySide6.QtWidgets import QScrollArea, QWidget
    from ui.util.touch_scroll import suspend_touch_scroll
    a = QScrollArea()
    a.setWidget(QWidget())
    assert suspend_touch_scroll(a) is False


def test_a_ghost_held_near_the_edge_turns_auto_scroll_on():
    """The pointer half of auto-scroll: holding the card near the bottom of
    the viewport has to arm a scroll in that direction."""
    win, _tmp = _window(30, tag=False)
    panel = _panel(win)
    card = panel._cards[0]
    at = card.mapToGlobal(card.rect().center())
    panel._begin_drag(0, at)
    vp = panel._scroll.viewport()
    # A point comfortably inside the bottom edge zone, expressed in the
    # viewport's own coordinates and then taken global — so it does not
    # depend on where this window happens to have been placed.
    low_global = vp.mapToGlobal(QPoint(vp.width() // 2,
                                       vp.height() - panel._EDGE_ZONE_PX // 3))
    panel._drag_moved(low_global)
    assert panel._edge_dir == 1, (
        "holding the card at the bottom edge did not arm a downward scroll "
        "(_edge_dir=%s)" % panel._edge_dir)
    panel._finish_drag_visuals()


def test_auto_scrolling_moves_the_drop_target_with_the_list():
    """Auto-scroll used to move the list and nothing else. The drop target is
    computed from where the ghost sits over the CARDS, and the cards were
    sliding past underneath a ghost that never moved — so a long drag scrolled
    page after page while the insertion point stayed pinned where the scroll
    began, and the page landed somewhere nobody chose.

    _edge_dir is set directly rather than via a screen position: this is about
    what happens once auto-scroll is RUNNING, and tying it to pixel geometry
    made it depend on where the window landed.
    """
    win, _tmp = _window(30, tag=False)
    panel = _panel(win)
    bar = panel._scroll.verticalScrollBar()
    assert bar.maximum() > 0, "list is not scrollable; nothing to measure"

    # Start from the top. The panel scrolls itself to the current page on
    # open, via a 100 ms timer — so whether there is any room left to scroll
    # DOWN depends on how much event loop has gone by, which made this test
    # pass alone and fail in a suite.
    bar.setValue(0)
    _app.processEvents()

    card = panel._cards[0]
    at = card.mapToGlobal(card.rect().center())
    panel._begin_drag(0, at)
    panel._drag_moved(at)
    target_at_start = panel._drag_to
    scroll_at_start = bar.value()
    assert scroll_at_start < bar.maximum(), "no room left to scroll down"

    panel._edge_dir = 1                     # auto-scroll running, downward
    for _ in range(40):
        panel._edge_scroll_step()

    # Deliberately NO processEvents here. The panel scrolls itself to the
    # current page on a 100 ms timer, and letting that fire now would drag
    # the bar back to page 0 and hide the very thing being measured.
    # _edge_scroll_step is synchronous, so there is nothing to wait for.
    assert bar.value() > scroll_at_start, "the list never auto-scrolled"
    assert panel._drag_to != target_at_start, (
        "the list scrolled %d px but the drop target stayed at slot %s"
        % (bar.value() - scroll_at_start, target_at_start))
    panel._finish_drag_visuals()


def test_the_slide_timer_stops_once_the_cards_have_settled():
    """It used to require the ghost to be gone as well, which never happens
    mid-drag — so it span at 60 Hz for the whole drag doing nothing."""
    win, _tmp = _window(6)
    panel = _panel(win)
    at = panel._cards[0].mapToGlobal(QPoint(5, 5))
    panel._begin_drag(0, at)
    panel._drag_moved(at + QPoint(0, panel._drag_slot_h * 2))
    for _ in range(200):
        panel._slide_step()
    assert not panel._slide.isActive(),         "the slide timer is still running with every card in place"
    panel._finish_drag_visuals()


def test_the_lifted_card_keeps_its_place_in_the_stack_afterwards():
    """It is parked off to the side during the drag; a drag that ends
    without a reorder has to put it back where it was."""
    win, _tmp = _window(6)
    panel = _panel(win)
    card = panel._cards[3]
    x_before = card.x()
    at = card.mapToGlobal(QPoint(5, 5))
    panel._begin_drag(3, at)
    assert card.x() != x_before, "the card was never moved out of the stack"
    panel._finish_drag_visuals()
    assert card.x() == x_before, f"card came back at x={card.x()}, was {x_before}"


def test_the_panel_is_left_usable_after_a_drag():
    win, _tmp = _window(6)
    panel = _panel(win)
    card = panel._cards[0]
    at = card.mapToGlobal(QPoint(5, 5))
    panel._begin_drag(0, at)
    panel._drag_moved(at + QPoint(0, panel._drag_slot_h * 2))
    panel._end_drag(at + QPoint(0, panel._drag_slot_h * 2))
    _settle(panel)                      # the dropped page glides into place
    assert panel._drag_ghost is None, "the ghost outlived the drag"
    assert panel._drag_src is None
    assert panel._drag_src is None, "the panel is still stuck mid-drag"
    assert all(c.isVisible() for c in panel._cards)
    assert not panel._slide.isActive() and not panel._edge_scroll.isActive()


def test_a_rebuild_during_a_drag_does_not_freeze_the_list():
    """A background finishing, or a page deleted from elsewhere, can
    rebuild the panel mid-gesture. Leaving the layout disabled would lock
    the list solid."""
    win, _tmp = _window(5)
    panel = _panel(win)
    card = panel._cards[2]
    panel._begin_drag(2, card.mapToGlobal(QPoint(5, 5)))
    panel._rebuild()
    _app.processEvents()
    assert panel._drag_src is None
    assert panel._drag_ghost is None
    assert panel._drag_src is None


def test_dragging_is_refused_when_there_is_nothing_to_reorder():
    win, _tmp = _window(1)
    panel = _panel(win)
    panel._begin_drag(0, QPoint(10, 10))
    assert panel._drag_src is None, "a one-page board started a drag"
    assert panel._drag_src is None


def test_the_cards_never_overlap_while_they_slide():
    win, _tmp = _window(7)
    panel = _panel(win)
    card = panel._cards[0]
    at = card.mapToGlobal(QPoint(5, 5))
    panel._begin_drag(0, at)
    panel._drag_moved(at + QPoint(0, panel._drag_slot_h * 4))
    for _ in range(60):
        panel._slide_step()
    # Only the cards that actually exist: most slots on a long board have
    # none, and on a short one they all do.
    ys = [c.y() for i, c in enumerate(panel._cards)
          if c is not None and i != panel._drag_src]
    assert ys == sorted(ys), "cards ended up out of order: %s" % ys
    assert len(set(ys)) == len(ys), "two cards settled in the same slot"
    panel._end_drag(at + QPoint(0, panel._drag_slot_h * 4))


def test_the_insert_index_is_defined_everywhere_in_the_column():
    """Measured against card midpoints, so a right-click anywhere means
    something — a menu that only worked in the gaps would feel broken."""
    win, _tmp = _window(4)
    panel = _panel(win)
    for i, card in enumerate(panel._cards):
        assert panel._insert_index_at(card.y()) == i
        assert panel._insert_index_at(card.y() + card.height() // 2 + 1) == i + 1
    assert panel._insert_index_at(10 ** 6) == len(panel._cards)
    assert panel._insert_index_at(-500) == 0


# ── inserting above / below a page ─────────────────────────────────────────
# (The right-click menu that used to offer these is gone — see the grip and
# no-menu tests below; the "+ Page" rows insert now. The arithmetic of
# "above page N" / "below page N" is still what they do.)


def test_insert_above_puts_the_new_page_where_the_clicked_one_was():
    """Above page N means the new sheet BECOMES page N and pushes the old
    one down. Checked on the board, not on the wording."""
    for page, expect in ((0, 1), (2, 3), (4, 5)):
        win, _tmp = _window(5)
        panel = _panel(win)
        before = _order(win._canvas)
        at = page                                   # above
        panel._insert_and_refresh(at)
        _app.processEvents()
        after = _order(win._canvas)
        assert after.count(-1) == 1, after
        assert after.index(-1) + 1 == expect, (
            "insert above page %d landed at page %d, expected %d: %s"
            % (page + 1, after.index(-1) + 1, expect, after))
        # and nothing else moved out of order
        assert [x for x in after if x != -1] == before


def test_insert_below_puts_the_new_page_after_the_clicked_one():
    for page, expect in ((0, 2), (2, 4), (4, 6)):
        win, _tmp = _window(5)
        panel = _panel(win)
        before = _order(win._canvas)
        at = page + 1                               # below
        panel._insert_and_refresh(at)
        _app.processEvents()
        after = _order(win._canvas)
        assert after.index(-1) + 1 == expect, (
            "insert below page %d landed at page %d, expected %d: %s"
            % (page + 1, after.index(-1) + 1, expect, after))
        assert [x for x in after if x != -1] == before


def test_the_menu_targets_the_card_under_the_pointer_not_the_current_page():
    """The one the brief is most emphatic about. The current page is 0
    throughout; right-clicking page 4 must talk about page 4."""
    win, _tmp = _window(6)
    panel = _panel(win)
    assert win._canvas.current_page == 0
    for page in range(6):
        y = panel._pad + page * panel._slot_h() + panel._card_h // 2
        assert panel._page_index_at(y) == page, (
            "a click over page %d resolved to page %d"
            % (page + 1, panel._page_index_at(y) + 1))


def test_a_click_in_the_gap_belongs_to_the_card_above_it():
    """So the menu means something everywhere in the column."""
    win, _tmp = _window(5)
    panel = _panel(win)
    for page in range(4):
        y = panel._pad + page * panel._slot_h() + panel._card_h + 2
        assert panel._page_index_at(y) == page


def test_inserting_from_the_menu_goes_through_the_app_s_own_machinery():
    """Not a private copy of page creation: the panel asks MainWindow, which
    is what keeps the undo stack, the thumbnail cache, the dirty flag and
    the page counter in step."""
    win, _tmp = _window(4)
    panel = _panel(win)
    seen = []
    panel._on_insert = lambda at: seen.append(at)
    panel._insert_and_refresh(2)
    assert seen == [2], seen


def test_an_inserted_page_keeps_the_previews_after_it_honest():
    """Every page from the insertion point on has shifted by one, and the
    thumbnail cache is keyed by index."""
    win, _tmp = _window(5)
    panel = _panel(win)
    calls = []
    win._invalidate_page_thumbs = lambda idx: calls.append(list(idx))
    at = 1                                   # above page 2
    panel._insert_and_refresh(at)
    _app.processEvents()
    assert calls and min(calls[0]) == 1, calls


# ── the card surface ───────────────────────────────────────────────────────

def test_a_card_actually_paints_its_own_background():
    """It did not. WA_TranslucentBackground reaches the card from the panel
    and means "draw no background", which a stylesheet cannot override — so
    a pixel inside a card and a pixel in the gap between two cards were the
    same value, (23, 23, 23, 22). An earlier attempt at making the cards
    stand out raised the fill from 0.30 to 0.45 and changed nothing
    visible, because the number was never used."""
    from PySide6.QtCore import Qt
    win, _tmp = _window(4)
    panel = _panel(win)
    card = panel._cards[0]
    assert not card.testAttribute(Qt.WA_TranslucentBackground), (
        "the card is translucent-background again; its fill will not paint")

    img = panel.grab().toImage()
    # LIGHTNESS, not alpha. grab() flattens the panel, so alpha says more
    # about which widget owns a pixel than about what the eye sees; what
    # the brief asks for is that the card reads as a lighter surface than
    # the panel around it.
    x = card.x() + card.width() // 2
    inside = img.pixelColor(x, card.y() + card.height() // 2)
    outside = img.pixelColor(x, card.y() + card.height()
                             + max(1, panel._gap // 2))

    def light(c):
        return (c.red() + c.green() + c.blue()) / 3.0

    assert light(inside) > light(outside) + 20, (
        "card reads at %.0f against a panel at %.0f — it does not stand out"
        % (light(inside), light(outside)))


def test_a_card_is_solid_and_lit_from_above():
    """There is no blur behind the panel any more, so a translucent card
    would just show the canvas through itself. It is opaque, and a touch
    lighter at the top than at the bottom (lit from above)."""
    win, _tmp = _window(4)
    panel = _panel(win)
    card = panel._cards[0]
    img = panel.grab().toImage()
    x = card.x() + card.width() // 2
    near_top = img.pixelColor(x, card.y() + 6)
    near_bottom = img.pixelColor(x, card.y() + card.height() - 6)
    assert near_top.alpha() == 255 and near_bottom.alpha() == 255, (
        "the card is see-through (alpha %d)" % near_top.alpha())
    assert near_top.lightness() > near_bottom.lightness(), (
        "the card is not lit from above: top %d, bottom %d"
        % (near_top.lightness(), near_bottom.lightness()))


def test_the_current_page_is_still_obviously_the_current_page():
    win, _tmp = _window(5)
    win._canvas.goto_page(2)
    panel = _panel(win)
    from ui.theme.theme_manager import theme_mgr
    accent = theme_mgr.tokens.accent
    cur = panel._cards[2].styleSheet()
    other = panel._cards[0].styleSheet()
    assert accent in cur, "the current card lost its accent stroke"
    assert accent not in other, "every card looks current"
    assert "2px" in cur and "2px" not in other


def test_the_cards_have_a_visibly_rounded_corner():
    from ui.theme.theme_manager import theme_mgr
    from ui.util.scaling import UiScale
    win, _tmp = _window(3)
    panel = _panel(win)
    want = UiScale.px(theme_mgr.tokens.radius_2xl)
    assert "border-radius: %dpx" % want in panel._cards[0].styleSheet()
    assert want >= UiScale.px(20), "radius_2xl is not actually a large radius"


# ── the busy window ─────────────────────────────────────────────────────────

def test_a_long_busy_message_is_shortened_to_fit_the_window():
    """The reported clipping. The window is a fixed square and the label was
    drawn on ONE line into a strip about 136 px wide with no eliding, so it
    ran past the end and Qt clipped it at the window edge — a word sliced in
    half with nothing to say a word had been lost. Measured, EVERY message
    this window was used with overflowed, "Opening board…" included.

    Asserted on the STRING, because a pixel test cannot see this: clipped
    and elided text stop at almost the same column (measured, 14 vs 16 of
    210), and only the final glyph differs.
    """
    from PySide6.QtGui import QFont, QFontMetrics
    from ui.panels.busy_window import BusyWindow
    from ui.util.scaling import UiScale
    from ui.theme.theme_manager import theme_mgr

    long_msg = "Laying out page 250 of 1,000 on this board…"
    win = BusyWindow(None, long_msg)
    win.show()
    _app.processEvents()
    try:
        canvas = win._canvas
        f = QFont(canvas.font())
        f.setPixelSize(UiScale.px(theme_mgr.tokens.font_body))
        strip = canvas.width() - UiScale.px(theme_mgr.tokens.space_md) * 2
        fm = QFontMetrics(f)

        assert fm.horizontalAdvance(long_msg) > strip, (
            "this message now fits, so it no longer tests anything — pick a "
            "longer one")

        painted = canvas.label_for_width(f, strip)
        assert fm.horizontalAdvance(painted) <= strip, (
            "the painted label is %d px wide in a %d px strip — it will be "
            "cut off" % (fm.horizontalAdvance(painted), strip))
        assert painted != long_msg and painted.endswith("…"), (
            "the label was shortened without an ellipsis (%r), so a dropped "
            "word looks like the end of the sentence" % painted)
    finally:
        win._allow_close = True
        win.close()


def test_a_short_busy_message_is_left_exactly_as_written():
    from PySide6.QtGui import QFont
    from ui.panels.busy_window import BusyWindow
    from ui.util.scaling import UiScale
    from ui.theme.theme_manager import theme_mgr
    win = BusyWindow(None, "Opening…")
    win.show()
    _app.processEvents()
    try:
        f = QFont(win._canvas.font())
        f.setPixelSize(UiScale.px(theme_mgr.tokens.font_body))
        strip = win._canvas.width() - UiScale.px(theme_mgr.tokens.space_md) * 2
        assert win._canvas.label_for_width(f, strip) == "Opening…"
    finally:
        win._allow_close = True
        win.close()


def test_the_window_paints_the_fitted_label_and_not_the_raw_one():
    """label_for_width only helps if paintEvent actually uses it. Checked on
    the AST so a comment quoting the old call cannot satisfy it."""
    import ast
    import inspect
    import textwrap
    from ui.panels import busy_window as bw
    src = textwrap.dedent(inspect.getsource(bw._BusyCanvas.paintEvent))
    tree = ast.parse(src)
    raw_draws = []
    for node in ast.walk(tree):
        if not (isinstance(node, ast.Call)
                and isinstance(node.func, ast.Attribute)
                and node.func.attr == "drawText"):
            continue
        for arg in node.args:
            # p.drawText(strip, flags, self._label) — the raw attribute.
            if (isinstance(arg, ast.Attribute) and arg.attr == "_label"):
                raw_draws.append(ast.dump(arg))
    assert not raw_draws, (
        "paintEvent draws self._label directly again; it will be clipped")


def test_a_job_that_can_count_shows_a_percentage():
    from ui.panels.busy_window import BusyWindow
    win = BusyWindow(None, "Laying out pages…")
    win.show()
    win.set_progress(250, 1000)
    _app.processEvents()
    try:
        assert win._canvas._pct == 25.0, win._canvas._pct
    finally:
        win._allow_close = True
        win.close()


def test_a_job_that_cannot_count_shows_no_number():
    """The window is used for work that genuinely cannot report progress —
    unpacking an archive knows bytes read and nothing about time left. Those
    keep the plain sweeping ring rather than being given an invented number."""
    from ui.panels.busy_window import BusyWindow
    win = BusyWindow(None, "Opening…")
    win.show()
    _app.processEvents()
    try:
        assert win._canvas._pct is None
    finally:
        win._allow_close = True
        win.close()


def test_the_percentage_survives_nonsense_without_lying():
    from ui.panels.busy_window import BusyWindow
    win = BusyWindow(None, "")
    win.show()
    try:
        for args, want in (((250, 1000), 25.0),
                           ((75.0,), 75.0),
                           ((-20, 100), 0.0),        # clamped
                           ((500, 100), 100.0),      # clamped
                           ((5, 0), None),           # nothing to count
                           ((None,), None),          # back to indeterminate
                           (("wat", 10), None)):
            win.set_progress(*args)
            got = win._canvas._pct
            assert (got is None and want is None) or                    (got is not None and abs(got - want) < 0.01),                    f"set_progress{args} -> {got}, wanted {want}"
    finally:
        win._allow_close = True
        win.close()


def test_the_page_layout_job_reports_progress_rather_than_a_long_sentence():
    """It used to set 'Laying out page 250 of 1,000…' — 319 px of text in a
    136 px strip. It knows exactly how far along it is, so it says so as a
    number the ring can hold."""
    import ast
    import inspect
    import textwrap
    import ui.main_window as _mw
    src = textwrap.dedent(
        inspect.getsource(_mw.MainWindow._apply_whole_background))
    assert "set_progress" in src, "the layout loop no longer reports progress"
    # Walk the AST, not the text: the explanation of WHY the long sentence
    # went away quotes it, and a grep cannot tell a comment from a call.
    said = [n.value for n in ast.walk(ast.parse(src))
            if isinstance(n, ast.Constant) and isinstance(n.value, str)]
    long_ones = [m for m in said if len(m) > 24 and "…" in m]
    assert not long_ones, (
        "a sentence too long for the 136 px strip is being shown again: %r"
        % long_ones)


def test_the_busy_window_is_square_and_modal():
    from ui.panels.busy_window import BusyWindow
    b = BusyWindow(None, "Working…")
    b.show()
    _app.processEvents()
    try:
        assert b.width() == b.height(), "the busy window is not square"
        assert b.isModal(), "the board can be edited while it is rewritten"
    finally:
        b.finish()


def test_the_busy_window_actually_animates():
    """A spinner that has stopped reads as a hung app more strongly than a
    blank screen does, so this checks the pixels rather than the timer."""
    from ui.panels.busy_window import BusyWindow
    from PySide6.QtGui import QImage
    b = BusyWindow(None, "")
    b.show()
    _app.processEvents()
    try:
        frames = []
        for _ in range(4):
            b._canvas._t0 -= 0.18
            img = QImage(b._canvas.size(), QImage.Format_ARGB32)
            img.fill(0)
            b._canvas.render(img)
            frames.append(img.constBits().tobytes())
        assert len(set(frames)) == len(frames),             "the animation drew the same frame twice — it is not moving"
    finally:
        b.finish()


def test_the_busy_window_cannot_be_dismissed_by_accident():
    """The work carries on regardless, and a board being rewritten with no
    sign of it is the worst of both."""
    from ui.panels.busy_window import BusyWindow
    from PySide6.QtGui import QKeyEvent
    from PySide6.QtCore import QEvent, Qt
    b = BusyWindow(None, "")
    b.show()
    _app.processEvents()
    try:
        b.keyPressEvent(QKeyEvent(QEvent.KeyPress, Qt.Key_Escape,
                                  Qt.NoModifier))
        _app.processEvents()
        assert b.isVisible(), "Escape closed it"
        b.close()
        _app.processEvents()
        assert b.isVisible(), "close() from the window manager closed it"
    finally:
        b.finish()
        _app.processEvents()
    assert not b.isVisible(), "finish() did not close it"


def test_run_with_busy_returns_the_work_and_lets_it_paint():
    from ui.panels.busy_window import run_with_busy
    ticks = []

    def work(busy):
        for i in range(5):
            busy.set_message("step %d" % i)
            busy.tick()
            ticks.append(i)
        return "result"

    assert run_with_busy(None, "Working…", work) == "result"
    assert len(ticks) == 5
    # A callable that takes no arguments works too.
    assert run_with_busy(None, "x", lambda: 7) == 7


def test_run_with_busy_does_not_swallow_failures():
    """Hiding an exception behind a window that then closes would leave the
    user with no error and no result."""
    from ui.panels.busy_window import run_with_busy
    try:
        run_with_busy(None, "x",
                      lambda b: (_ for _ in ()).throw(RuntimeError("boom")))
    except RuntimeError as exc:
        assert "boom" in str(exc)
    else:
        raise AssertionError("the exception was swallowed")


def test_a_long_layout_runs_behind_the_busy_window():
    """The 1000-page case. Below the threshold there must be no window at
    all — a flash of one is more distracting than the pause it covers."""
    import inspect
    import ui.main_window as mw
    src = inspect.getsource(mw.MainWindow._media_set_whole_background)
    body = src.split('"""')[-1]
    assert "run_with_busy" in body,         "laying out a whole PDF no longer shows anything while it works"
    assert "_BUSY_PAGE_THRESHOLD" in body,         "the busy window is shown for every deck, however small"
    assert mw.MainWindow._BUSY_PAGE_THRESHOLD >= 10


def test_the_layout_loop_yields_so_the_window_can_paint():
    import inspect
    import ui.main_window as mw
    body = inspect.getsource(mw.MainWindow._apply_whole_background)
    assert "busy.tick()" in body,         "the loop never yields, so the window appears and then freezes"



# ── reordering, fixed ──────────────────────────────────────────────────────

def test_the_drop_slot_is_symmetric_up_and_down():
    """THE lurch. The slot used to be measured from the card's centre, which
    already sits ~half a card below the slot's top — so 7 px DOWN jumped a
    slot while going UP took more than half a card. Now it is half a slot
    either way."""
    win, _tmp = _window(8)
    panel = _panel(win)
    card = panel._cards[3]
    at = card.mapToGlobal(QPoint(card.width() // 2, card.height() // 2))
    panel._begin_drag(3, at)
    h = panel._drag_slot_h
    for dy, want in ((int(h * 0.4), 3), (-int(h * 0.4), 3),
                     (int(h * 0.6), 4), (-int(h * 0.6), 2)):
        panel._drag_moved(at + QPoint(0, dy))
        assert panel._drag_to == want, (
            "moving %+d px (%.1f slots) gave slot %d, expected %d"
            % (dy, dy / h, panel._drag_to, want))
    panel._finish_drag_visuals()


def test_scrolling_mid_drag_does_not_snap_the_cards_back():
    """Any scroll during a drag re-lays the column. It used to put every
    card back in its OWN slot — over the gap the page was meant to drop
    into — and nothing slid them out again."""
    win, _tmp = _window(7)
    panel = _panel(win)
    at = panel._cards[0].mapToGlobal(QPoint(5, 5))
    panel._begin_drag(0, at)
    panel._drag_moved(at + QPoint(0, panel._drag_slot_h * 3))
    for _ in range(200):
        panel._slide_step()
    panel._relayout_cards()                 # what every scroll does
    for i, c in enumerate(panel._cards):
        if c is None or i == panel._drag_src:
            continue
        assert c.y() == panel._target_y(i), (
            "card %d snapped to y=%d; during the drag it belongs at %d"
            % (i, c.y(), panel._target_y(i)))
    panel._finish_drag_visuals()


def test_the_dropped_page_glides_into_its_slot():
    win, _tmp = _window(6)
    panel = _panel(win)
    card = panel._cards[0]
    at = card.mapToGlobal(QPoint(5, 5))
    panel._begin_drag(0, at)
    # Let go a little short of slot 3, so there is somewhere to glide FROM.
    let_go = at + QPoint(0, int(panel._drag_slot_h * 2.8))
    panel._drag_moved(let_go)
    panel._end_drag(let_go)
    # The board is reordered at once; what glides is the lifted picture of
    # the page, settling into the slot while the real card waits under it.
    assert _order(win._canvas) == [1, 2, 3, 0, 4, 5]
    landed = panel._cards[3]
    assert landed is not None
    ghost = panel._drop_ghost
    assert ghost is not None and panel._slide.isActive(), (
        "the dropped page appeared in its slot instead of gliding there")
    home = panel._drop_home()
    assert (abs(ghost.geometry().center().y() - home[1]) > 2
            and not landed.isVisible()), "nothing was left to glide"
    _settle(panel)
    assert panel._drop_ghost is None, "the landing ghost outlived the glide"
    assert landed.isVisible() and landed.y() == panel._slot_y_abs(3)


def test_a_lifted_page_grows_and_a_dropped_one_shrinks_back():
    """Picked up, the page grows to its lifted size over a few frames
    instead of popping; let go, it shrinks back to card size as it lands —
    no jump in size at either end."""
    win, _tmp = _window(6)
    panel = _panel(win)
    card = panel._cards[1]
    at = card.mapToGlobal(QPoint(5, 5))
    panel._begin_drag(1, at)
    g = panel._drag_ghost
    w0 = g.width()
    assert panel._ghost_scale == 1.0, "the ghost popped up at full lift"
    assert g.width() == card.width() + 2 * g.pad, "not drawn at card size"
    sizes = []
    for _ in range(12):
        panel._slide_step()
        sizes.append(g.width())
    assert sizes == sorted(sizes) and sizes[-1] > w0, sizes
    panel._drag_moved(at + QPoint(0, panel._drag_slot_h * 2))
    panel._end_drag(at + QPoint(0, panel._drag_slot_h * 2))
    d = panel._drop_ghost
    assert d is not None
    _settle(panel)
    assert panel._ghost_scale == 1.0


def test_a_drop_slot_shows_where_the_page_will_land():
    win, _tmp = _window(6)
    panel = _panel(win)
    card = panel._cards[0]
    at = card.mapToGlobal(QPoint(5, 5))
    panel._begin_drag(0, at)
    ds = panel._drop_slot
    assert ds is not None and ds.isVisible()
    panel._drag_moved(at + QPoint(0, panel._drag_slot_h * 3))
    _settle(panel)
    assert ds.y() == panel._slot_y(panel._drag_to) == panel._slot_y(3)
    panel._end_drag(at + QPoint(0, panel._drag_slot_h * 3))
    assert not ds.isVisible(), "the drop slot stayed after the drop"


def _paint(card, rgb):
    from PySide6.QtGui import QPixmap, QColor
    pm = QPixmap(40, 24)
    pm.fill(QColor(*rgb))
    card.adopt_thumbnail(pm)


def _thumb_rgb(card):
    pm = card._thumb.pixmap()
    if pm is None or pm.isNull():
        return None
    c = pm.toImage().pixelColor(2, 2)
    return (c.red(), c.green(), c.blue())


def test_previews_follow_their_pages_through_a_move():
    """A drop rebuilds the column, and rebuilt cards used to start blank and
    refill one by one — the whole column flashed every time."""
    win, _tmp = _window(5)
    panel = _panel(win)
    panel._render_thumbnails = lambda *a, **k: None     # only carried ones
    colours = [(200, 0, 0), (0, 200, 0), (0, 0, 200), (200, 200, 0), (0, 200, 200)]
    for card, rgb in zip(panel._cards, colours):
        _paint(card, rgb)
    at = panel._cards[0].mapToGlobal(QPoint(5, 5))
    panel._begin_drag(0, at)
    panel._drag_moved(at + QPoint(0, panel._drag_slot_h * 2))
    panel._end_drag(at + QPoint(0, panel._drag_slot_h * 2))
    got = [_thumb_rgb(c) for c in panel._cards]
    assert got == [colours[1], colours[2], colours[0], colours[3], colours[4]], got


# ── the "+ Page" rows ──────────────────────────────────────────────────────

def test_every_gap_has_a_plus_page_row():
    win, _tmp = _window(5)
    panel = _panel(win)
    rows = sorted(panel._gaps.items())
    assert [i for i, _g in rows] == [0, 1, 2, 3], [i for i, _g in rows]
    assert [g.insert_at for _i, g in rows] == [1, 2, 3, 4]
    for i, g in rows:
        assert g.isVisible(), "gap %d's row is hidden at rest" % i
        above = panel._cards[i].geometry()
        below = panel._cards[i + 1].geometry()
        assert g.y() >= above.bottom() and g.geometry().bottom() <= below.top(), (
            "the row for gap %d overlaps a card" % i)


def test_a_plus_page_row_inserts_exactly_there_and_opens_it():
    for gap in range(4):
        win, _tmp = _window(5)
        panel = _panel(win)
        before = _order(win._canvas)
        panel._gaps[gap].click()
        _settle(panel)
        after = _order(win._canvas)
        assert after.count(-1) == 1, after
        assert after.index(-1) == gap + 1, (
            "the row between page %d and %d inserted at page %d: %s"
            % (gap + 1, gap + 2, after.index(-1) + 1, after))
        assert [x for x in after if x != -1] == before, "other pages moved"
        assert win._canvas.current_page == gap + 1, \
            "the new page was not opened"
        assert panel.isVisible(), "the panel closed; the user cannot see where it went"
        assert all(c is None or c.isVisible() for c in panel._cards), \
            "the new card never appeared"


def test_inserting_slides_the_pages_below_down():
    win, _tmp = _window(5)
    panel = _panel(win)
    panel._gaps[1].click()                  # new page becomes page 3
    moved = panel._cards[3]
    assert moved.y() == panel._slot_y_abs(2) and panel._slide.isActive(), (
        "the pages below jumped instead of sliding down")
    assert not panel._cards[2].isVisible(), "the new card is up before there is room"
    _settle(panel)
    assert moved.y() == panel._slot_y_abs(3)
    assert panel._cards[2].isVisible()


def test_the_plus_page_rows_step_aside_during_a_drag():
    win, _tmp = _window(5)
    panel = _panel(win)
    at = panel._cards[1].mapToGlobal(QPoint(5, 5))
    panel._begin_drag(1, at)
    assert not any(g.isVisible() for g in panel._gaps.values()), \
        "a + Page row is sitting in the gap the cards slide through"
    panel._drag_moved(at + QPoint(0, panel._drag_slot_h))
    panel._end_drag(at + QPoint(0, panel._drag_slot_h))
    _settle(panel)
    assert panel._gaps and all(g.isVisible() for g in panel._gaps.values()), \
        "the rows did not come back after the drop"


def test_a_long_board_does_not_build_a_row_per_gap():
    win, _tmp = _window(300, tag=False)
    panel = _panel(win)
    assert 0 < len(panel._gaps) < 60, "%d rows built for 300 pages" % len(panel._gaps)
    bar = panel._scroll.verticalScrollBar()
    bar.setValue(bar.maximum())
    _app.processEvents()
    assert max(panel._gaps) == 298, "no row for the last gap after scrolling there"


def test_a_scroll_right_after_opening_is_not_yanked_back():
    """Opening the panel scrolls to the current page 100 ms later. A flick
    that started inside those 100 ms used to be undone by it — and in a
    slow full-suite run, so was the scroll in the test above."""
    import time
    win, _tmp = _window(300, tag=False)
    win._canvas.goto_page(150)
    panel = _panel(win)
    bar = panel._scroll.verticalScrollBar()
    bar.setValue(bar.maximum())
    t = time.time()
    while time.time() - t < 0.4:
        _app.processEvents()
    assert bar.value() == bar.maximum(), \
        "the list was pulled back to the current page (%d)" % bar.value()



# ── the reorder grip, and no right-click menu ──────────────────────────────

def _grip_drag(panel, card, dy_px, steps=8):
    """Press the grip, move it by dy_px, release — through the grip's own
    event handlers, the path a finger takes."""
    from PySide6.QtGui import QMouseEvent
    from PySide6.QtCore import QEvent, QPointF
    grip = card._grip
    start = grip.mapToGlobal(QPoint(grip.width() // 2, grip.height() // 2))

    def ev(kind, gp):
        lp = grip.mapFromGlobal(gp)
        return QMouseEvent(kind, QPointF(lp), QPointF(gp), Qt.LeftButton,
                           Qt.LeftButton, Qt.NoModifier)
    grip.mousePressEvent(ev(QEvent.MouseButtonPress, start))
    for i in range(1, steps + 1):
        gp = start + QPoint(0, int(dy_px * i / steps))
        grip.mouseMoveEvent(ev(QEvent.MouseMove, gp))
        _app.processEvents()
    grip.mouseReleaseEvent(ev(QEvent.MouseButtonRelease,
                              start + QPoint(0, dy_px)))
    _settle(panel)


def test_every_card_has_a_reorder_grip():
    win, _tmp = _window(5)
    panel = _panel(win)
    cards = [c for c in panel._cards if c is not None]
    assert cards and all(c._grip is not None for c in cards)
    for c in cards:
        assert c._grip.isVisible(), "a grip is built but not shown"
        g = c._grip.geometry()
        assert g.width() >= UiScale.px(28) and g.height() >= UiScale.px(28), \
            "the grip is too small to hit with a finger: %s" % g


def test_a_single_page_has_no_grip():
    win, _tmp = _window(1)
    panel = _panel(win)
    g = panel._cards[0]._grip
    assert g is None or not g.isVisibleTo(panel._cards[0])


def test_pressing_the_grip_picks_the_card_up_at_once():
    """No long press: the grip means "move this", nothing else."""
    win, _tmp = _window(5)
    panel = _panel(win)
    card = panel._cards[1]
    from PySide6.QtGui import QMouseEvent
    from PySide6.QtCore import QEvent, QPointF
    gp = card._grip.mapToGlobal(QPoint(5, 5))
    card._grip.mousePressEvent(QMouseEvent(
        QEvent.MouseButtonPress, QPointF(5, 5), QPointF(gp), Qt.LeftButton,
        Qt.LeftButton, Qt.NoModifier))
    assert panel._drag_src == 1, "pressing the grip did not start a drag"
    assert panel._drag_ghost is not None
    card._grip.mouseReleaseEvent(QMouseEvent(
        QEvent.MouseButtonRelease, QPointF(5, 5), QPointF(gp), Qt.LeftButton,
        Qt.NoButton, Qt.NoModifier))
    _settle(panel)
    assert panel._drag_src is None


def test_dragging_the_grip_moves_the_page():
    win, _tmp = _window(5)
    panel = _panel(win)
    before = _order(win._canvas)
    slot = panel._slot_h()
    _grip_drag(panel, panel._cards[0], int(slot * 2.2))
    after = _order(win._canvas)
    assert after == [before[1], before[2], before[0], before[3], before[4]], \
        "page 1 dragged down two slots gave %s (was %s)" % (after, before)


def test_dragging_the_grip_up_moves_the_page_up():
    win, _tmp = _window(5)
    panel = _panel(win)
    before = _order(win._canvas)
    slot = panel._slot_h()
    _grip_drag(panel, panel._cards[3], -int(slot * 1.2))
    after = _order(win._canvas)
    assert after == [before[0], before[1], before[3], before[2], before[4]], \
        (after, before)


def test_the_cards_slide_while_the_grip_is_held():
    """Not a jump at the end: the cards in the way move aside DURING the
    drag, towards the slot they will end in."""
    from PySide6.QtGui import QMouseEvent
    from PySide6.QtCore import QEvent, QPointF
    win, _tmp = _window(5)
    panel = _panel(win)
    card0, card1 = panel._cards[0], panel._cards[1]
    y1 = card1.y()
    grip = card0._grip
    start = grip.mapToGlobal(QPoint(5, 5))

    def ev(kind, gp):
        return QMouseEvent(kind, QPointF(grip.mapFromGlobal(gp)), QPointF(gp),
                           Qt.LeftButton, Qt.LeftButton, Qt.NoModifier)
    grip.mousePressEvent(ev(QEvent.MouseButtonPress, start))
    grip.mouseMoveEvent(ev(QEvent.MouseMove,
                           start + QPoint(0, int(panel._slot_h() * 1.3))))
    import time
    t0 = time.time()
    while time.time() - t0 < 0.6:
        _app.processEvents()
    assert card1.y() < y1 - panel._slot_h() * 0.6, \
        "page 2 did not slide up into page 1's slot while dragging"
    grip.mouseReleaseEvent(ev(QEvent.MouseButtonRelease, start))
    _settle(panel)


def test_there_is_no_right_click_menu_on_the_pages():
    """On a touch board a press-and-hold IS a right-click; the menu kept
    popping up over the long press that picks a card up."""
    from ui.panels import pages_panel as pp
    import inspect
    src = inspect.getsource(pp)
    assert "QMenu" not in src.replace("No right-click menu", "")
    assert not hasattr(pp.PagesPanel, "page_menu_plan")
    assert "contextMenuEvent" not in pp.PagesPanel.__dict__


# ── deleting a page from the panel ─────────────────────────────────────────

def _bin(card):
    from PySide6.QtWidgets import QPushButton
    return [b for b in card.findChildren(QPushButton) if b.text() == "🗑"][0]


def _panel_is_sound(panel, canvas):
    """Every built card on its own slot, naming its own page; no stray
    cards painting anywhere; a bin exactly when there is more than one."""
    assert len(panel._cards) == canvas.page_count
    for i, c in enumerate(panel._cards):
        if c is None or not c.isVisible():
            continue
        assert c._index == i, "slot %d holds a card for page %d" % (i, c._index)
        assert c._name_lbl.text() == "Page %d" % (i + 1)
        assert c.y() == panel._slot_y_abs(i), (i, c.y(), panel._slot_y_abs(i))
        assert _bin(c).isVisibleTo(c) == (canvas.page_count > 1)
    stray = [w for w in panel._content.findChildren(type(panel._cards[0]))
             if w.isVisible() and w not in panel._cards]
    assert not stray, "%d cards painting outside the list" % len(stray)


def test_tapping_a_bin_does_not_fling_the_list():
    """THE delete bug. The pressed bin kept the keyboard focus; the rebuild
    hid its card, Qt passed the focus to the next card's bin and the scroll
    area scrolled to it — card after card, four cards down per delete. And
    each of those scrolls rebuilt the window in the middle of the rebuild,
    so cards ended up on the wrong pages or vanished altogether."""
    from PySide6.QtTest import QTest
    win, _tmp = _window(20, tag=False)
    win._canvas.goto_page(0)
    panel = _panel(win)
    bar = panel._scroll.verticalScrollBar()
    bar.setValue(0)
    _app.processEvents()
    for _ in range(6):
        before = bar.value()
        QTest.mouseClick(_bin(panel._cards[0]), Qt.LeftButton)
        _settle(panel)
        assert bar.value() == before, (
            "deleting a page scrolled the list from %d to %d"
            % (before, bar.value()))
        _panel_is_sound(panel, win._canvas)
    assert win._canvas.page_count == 14


def test_deleting_down_to_one_page_leaves_a_working_panel():
    from PySide6.QtTest import QTest
    win, _tmp = _window(8, tag=False)
    panel = _panel(win)
    while win._canvas.page_count > 1:
        card = next(c for c in panel._cards if c is not None and c.isVisible())
        QTest.mouseClick(_bin(card), Qt.LeftButton)
        _settle(panel)
        _panel_is_sound(panel, win._canvas)
    assert panel.isVisible(), "the panel closed itself"


def test_cards_carry_no_live_shadow_effect():
    """A QGraphicsDropShadowEffect re-blurs its widget on every repaint, and
    during a slide every card repaints every frame: that was the stutter.
    The shadows are baked pictures painted by the stack now."""
    win, _tmp = _window(4, tag=False)
    panel = _panel(win)
    for c in panel._cards:
        assert c.graphicsEffect() is None
        assert c._thumb_frame.graphicsEffect() is None


def test_no_preview_is_rasterised_while_cards_are_moving():
    win, _tmp = _window(6)
    panel = _panel(win)
    calls = []
    real = panel._thumb_manager.get_thumbnail
    panel._thumb_manager.get_thumbnail = lambda i: (calls.append(i), real(i))[1]
    at = panel._cards[0].mapToGlobal(QPoint(5, 5))
    panel._begin_drag(0, at)
    panel._drag_moved(at + QPoint(0, panel._drag_slot_h * 2))
    for c in panel._cards:
        c.clear_thumbnail()
    panel._render_thumbnails()
    panel._pump_thumbnails()
    assert calls == [], "rasterised %s mid-drag" % calls
    panel._end_drag(at + QPoint(0, panel._drag_slot_h * 2))
    _settle(panel)
    for _ in range(40):
        _app.processEvents()
    assert calls, "the previews never caught up once the cards settled"


def test_the_bin_never_takes_focus():
    win, _tmp = _window(3, tag=False)
    panel = _panel(win)
    assert _bin(panel._cards[0]).focusPolicy() == Qt.NoFocus


def test_the_pages_below_a_deleted_one_slide_up():
    win, _tmp = _window(6)
    panel = _panel(win)
    panel._on_card_delete(1)
    moving = [c for i, c in enumerate(panel._cards)
              if c is not None and i >= 1 and c.y() != panel._slot_y_abs(i)]
    assert moving and panel._slide.isActive(), "the column jumped instead"
    _settle(panel)
    _panel_is_sound(panel, win._canvas)
    assert _order(win._canvas) == [0, 2, 3, 4, 5]


def test_a_recycled_card_gets_its_bin_back_when_the_board_grows():
    """Built while the board had one page, a card used to stay binless."""
    win, _tmp = _window(1, tag=False)
    panel = _panel(win)
    panel._on_add_clicked()
    _settle(panel)
    for c in panel._cards:
        assert _bin(c).isVisibleTo(c), "a card on a 2-page board has no bin"
        assert c._grip is not None and c._grip.isVisibleTo(c)

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
