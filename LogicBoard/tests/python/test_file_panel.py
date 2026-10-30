"""
test_file_panel.py — the board manager panel.

Runs under pytest, and also standalone
(``python tests/python/test_file_panel.py``).

What is worth protecting here
-----------------------------
Two classes of thing.

**The toggle.** The Files button and the panel are one control. Pressing it
while the panel is open has to put the panel away — before this, the press
rebuilt an identical panel from scratch, which looks exactly like a panel
that refuses to close. Pen and Eraser already worked this way; Files was the
odd one out.

**The layout holding together with real content.** A board called "Class 11-A
Thermodynamics Chapter 4 Revision Notes" is about 520 px of text, and a plain
QLabel reports that as its minimum width. The card grew to fit, the delete
button went 80 px off the right edge of the panel, and a horizontal scrollbar
appeared under the list. Test names in this file are long for the same reason
board names are: that is what the real data looks like.

**The card being big enough to look like a card.** The panel is a two-column
grid of cards with a logo hero, not the single column of 72 px rows it was.
That row gave its icon 42 logical pixels — small enough that the mark read as
pixelated and shaved off at the edges, which is what got it changed. Both of
those are measurable, so they are measured here rather than left to the next
person to notice by eye.

**The panel being the SAME SIZE every time.** It is anchored above the
toolbar button that opens it, so a panel that sizes itself to its contents
does not merely look different with three boards than with two — it puts the
footer buttons somewhere else on the wall. The first cut of the grid sized
the viewport between one and two card rows and roughly doubled in height at
the third board.
"""
import os
import sys
import tempfile
import time
import zipfile
from pathlib import Path

os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")
sys.path.insert(0, os.path.abspath(os.path.join(os.path.dirname(__file__),
                                                "..", "..")))

from PySide6.QtWidgets import QApplication, QScrollArea, QPushButton, QLabel

_app = QApplication.instance() or QApplication([])

import ui.util.paths as paths

LONG_NAME = "Class 11-A Thermodynamics Chapter 4 Revision Notes"


# ── scaffolding ─────────────────────────────────────────────────────────────

def _saves_with(*boards) -> Path:
    """A temp saves dir holding ``(name, age_seconds, padding_bytes)`` boards."""
    tmp = Path(tempfile.mkdtemp(prefix="lb_panel_test_"))
    paths.saves_dir = lambda: tmp
    import ui.panels.file_panel as fp
    fp.saves_dir = lambda: tmp
    now = time.time()
    for name, age, pad in boards:
        p = tmp / f"{name}.lb"
        with zipfile.ZipFile(p, "w") as z:
            z.writestr("board.json", "{}" + " " * pad)
        os.utime(p, (now - age, now - age))
    return tmp


def _panel(tmp, **kw):
    from ui.panels.file_panel import FilePanel
    p = FilePanel(**kw)
    p.show()
    _app.processEvents()
    _app.processEvents()
    return p


# ── the toggle ──────────────────────────────────────────────────────────────

def test_pressing_files_again_closes_the_panel():
    """THE request. Second press on the button puts the panel away."""
    tmp = _saves_with(("Lesson", 0, 10))
    import ui.main_window as mw
    mw.saves_dir = lambda: tmp
    win = mw.MainWindow()

    win._show_file_panel()
    from ui.panels.file_panel import FilePanel
    assert isinstance(win._active_panel, FilePanel), "the panel did not open"
    assert win._active_panel.isVisible()

    win._show_file_panel()
    assert win._active_panel is None, \
        "a second press left a panel open — the toggle is not wired up"


def test_a_third_press_opens_it_again():
    """Toggling shut must not latch it shut."""
    tmp = _saves_with(("Lesson", 0, 10))
    import ui.main_window as mw
    mw.saves_dir = lambda: tmp
    win = mw.MainWindow()
    from ui.panels.file_panel import FilePanel
    win._show_file_panel()
    win._show_file_panel()
    win._show_file_panel()
    assert isinstance(win._active_panel, FilePanel)
    assert win._active_panel.isVisible()


def test_opening_a_different_panel_still_swaps_rather_than_closing():
    """The toggle keys on the panel TYPE. Pressing Settings while Files is
    open must show Settings, not leave the user with nothing."""
    tmp = _saves_with(("Lesson", 0, 10))
    import ui.main_window as mw
    mw.saves_dir = lambda: tmp
    win = mw.MainWindow()
    from ui.panels.file_panel import FilePanel
    win._show_file_panel()
    win._show_settings_panel()
    assert win._active_panel is not None
    assert not isinstance(win._active_panel, FilePanel)


# ── the layout survives real board names ────────────────────────────────────

def test_a_long_board_name_does_not_push_the_row_off_the_panel():
    """The row must never be wider than the viewport showing it."""
    tmp = _saves_with((LONG_NAME, 0, 1000))
    p = _panel(tmp)
    sa = p.findChildren(QScrollArea)[0]
    row = p._board_buttons[0][1]
    assert row.width() <= sa.viewport().width(), (
        f"row is {row.width()}px in a {sa.viewport().width()}px viewport — "
        "a long name is stretching it again")


def test_a_long_board_name_produces_no_horizontal_scrollbar():
    tmp = _saves_with((LONG_NAME, 0, 1000))
    p = _panel(tmp)
    sa = p.findChildren(QScrollArea)[0]
    assert sa.horizontalScrollBar().maximum() == 0, \
        "the list scrolls sideways — something is wider than the panel"


def test_the_delete_button_stays_inside_the_panel():
    """It went 80px outside on the first cut of this layout, which makes it
    unclickable rather than merely ugly."""
    tmp = _saves_with((LONG_NAME, 0, 1000))
    p = _panel(tmp)
    row = p._board_buttons[0][1]
    btns = row.findChildren(QPushButton)
    assert btns, "the row has no delete button"
    right = btns[0].mapTo(p, btns[0].rect().topRight()).x()
    assert right <= p.width(), \
        f"delete button ends at x={right} on a {p.width()}px panel"


def test_a_long_name_is_elided_rather_than_truncated_silently():
    """An ellipsis says "there is more"; a hard cut just looks wrong. The
    full name stays reachable in the tooltip."""
    tmp = _saves_with((LONG_NAME, 0, 1000))
    p = _panel(tmp)
    row = p._board_buttons[0][1]
    from ui.panels.file_panel import _ElidingLabel
    names = [l for l in row.findChildren(_ElidingLabel)]
    assert names, "the name label is not an eliding one"
    shown = names[0].text()
    assert shown != LONG_NAME, "nothing was elided; the row must be overflowing"
    assert shown.endswith("…"), f"cut without an ellipsis: {shown!r}"
    assert names[0].toolTip() == LONG_NAME, "the full name is unreachable"


# ── the list itself ─────────────────────────────────────────────────────────

def test_boards_are_listed_newest_first():
    """The board somebody wants is nearly always the last one they touched,
    not the one that sorts first alphabetically."""
    tmp = _saves_with(("Alpha", 86400 * 3, 10),
                      ("Zulu", 0, 10),
                      ("Mike", 3600, 10))
    p = _panel(tmp)
    order = [path.stem for path, _ in p._board_buttons]
    assert order == ["Zulu", "Mike", "Alpha"], order


def test_the_newest_board_starts_selected_so_open_is_live():
    tmp = _saves_with(("Old", 86400, 10), ("New", 0, 10))
    p = _panel(tmp)
    assert p._selected_path is not None
    assert p._selected_path.stem == "New"
    assert p._open_btn.isEnabled()


def test_open_is_disabled_when_there_is_nothing_to_open():
    """Pressing Open with no selection used to do nothing at all, which
    reads as a broken button rather than an unavailable one."""
    tmp = _saves_with()
    p = _panel(tmp)
    assert p._board_buttons == []
    assert not p._open_btn.isEnabled()


def test_the_open_board_is_marked_in_the_list():
    tmp = _saves_with(("Current", 0, 10), ("Other", 3600, 10))
    p = _panel(tmp, current_board_path=tmp / "Current.lb")
    row = dict((path.stem, w) for path, w in p._board_buttons)["Current"]
    chips = [l.text() for l in row.findChildren(QLabel) if l.text() == "OPEN"]
    assert chips, "the board already on screen is not marked"
    other = dict((path.stem, w) for path, w in p._board_buttons)["Other"]
    assert not [l for l in other.findChildren(QLabel) if l.text() == "OPEN"]


def test_every_row_shows_when_it_was_saved():
    tmp = _saves_with(("Lesson", 0, 10))
    p = _panel(tmp)
    row = p._board_buttons[0][1]
    texts = " ".join(l.text() for l in row.findChildren(QLabel))
    assert "just now" in texts, texts


def test_rows_no_longer_repeat_the_same_advice():
    """"Double-click to open" on every row is texture, not information."""
    tmp = _saves_with(("A", 0, 10), ("B", 10, 10))
    p = _panel(tmp)
    for _path, row in p._board_buttons:
        texts = " ".join(l.text() for l in row.findChildren(QLabel))
        assert "Double-click" not in texts, texts


# ── the panel does not resize itself ───────────────────────────────────────

def _heights_for(*counts):
    out = []
    for n in counts:
        tmp = _saves_with(*[(f"Board {i}", i * 60, 10) for i in range(n)])
        out.append((n, _panel(tmp).height()))
    return out


def test_the_panel_is_the_same_height_however_many_boards_are_saved():
    """One board, four, or a dozen — same panel. It is anchored above the
    Files button, so height IS position for everything in the footer."""
    heights = _heights_for(0, 1, 2, 3, 4, 9)
    distinct = {h for _n, h in heights}
    assert len(distinct) == 1, (
        "the panel changes height with its contents: "
        + ", ".join(f"{n} boards -> {h}px" for n, h in heights))


def test_the_panel_is_the_same_width_however_many_boards_are_saved():
    tmp = _saves_with(("Only", 0, 10))
    one = _panel(tmp).width()
    tmp = _saves_with(*[(f"Board {i}", i * 60, 10) for i in range(7)])
    many = _panel(tmp).width()
    assert one == many, f"{one}px with one board, {many}px with seven"


def test_a_search_that_hides_boards_does_not_shrink_the_panel():
    """Typing in the search box filters the grid. If the viewport tracked
    its contents the panel would resize under the user's finger, mid-word."""
    tmp = _saves_with(*[(f"Board {i}", i * 60, 10) for i in range(6)])
    p = _panel(tmp)
    before = p.height()
    p._search.setText("Board 3")
    _app.processEvents()
    p.adjustSize()
    _app.processEvents()
    assert len(p._board_buttons) == 1, "the search did not filter"
    assert p.height() == before, \
        f"panel went {before}px -> {p.height()}px while filtering"


def test_the_viewport_holds_exactly_two_card_rows():
    """Not two-and-a-bit. A partial third row is a nice affordance and a bad
    trade here: it would be permanent dead space at the bottom for every
    user with four boards or fewer."""
    from ui.util.size_manager import size_mgr as S
    from ui.panels.file_panel import _TILE_H, _GRID_ROWS
    from ui.theme.theme_manager import theme_mgr
    tmp = _saves_with(*[(f"Board {i}", i * 60, 10) for i in range(6)])
    p = _panel(tmp)
    sa = p.findChildren(QScrollArea)[0]
    expected = (S.scale_size(_TILE_H) * _GRID_ROWS
                + S.scale_spacing(theme_mgr.tokens.space_md) * (_GRID_ROWS - 1))
    assert sa.height() == expected, f"{sa.height()} != {expected}"


# ── sizes come from the SizeManager, not from bare pixels ──────────────────

def test_the_panel_scales_everything_through_the_size_manager():
    """The module was on the legacy ``UiScale.px`` shim, which routes every
    dimension through ``scale_size`` — including font sizes, which the
    SizeManager deliberately grows on a gentler curve so text does not
    outrun its box on a 4K board. about_dialog and setup_wizard already use
    the semantic helpers; this one does now too."""
    import inspect
    import ui.panels.file_panel as fp
    src = inspect.getsource(fp)
    assert "UiScale" not in src, "back on the legacy scaling shim"
    assert "size_mgr" in src, "not importing the SizeManager at all"


def test_every_font_size_goes_through_scale_font():
    """scale_size on a font is the specific bug: at 4K it is ~10% larger
    than the font curve intends, which is where text starts clipping."""
    import inspect
    import re
    import ui.panels.file_panel as fp
    src = inspect.getsource(fp)
    bad = [m.group(0) for m in re.finditer(r"font-size:\s*\{[^}]*\}", src)
           if "scale_font" not in m.group(0)]
    assert not bad, f"font sizes not on the font curve: {bad}"


def test_no_dimension_is_a_bare_unscaled_pixel_literal():
    """A hard-coded ``12px`` in a stylesheet is correct at 1080p and wrong
    at every other resolution the SizeManager exists to handle."""
    import ast
    import inspect
    import re
    import ui.panels.file_panel as fp

    # Walk the AST rather than grepping the text. Every pixel dimension that
    # reaches Qt is inside a string literal; a ``#`` comment saying "1px to
    # 2px" is prose, and an f-string's ``{S.scale_radius(8)}`` is an
    # expression node, so neither shows up here. Grepping the source counts
    # both and cannot tell them apart.
    literals = []
    for node in ast.walk(ast.parse(inspect.getsource(fp))):
        if isinstance(node, ast.Constant) and isinstance(node.value, str):
            literals += re.findall(r"[:\s](\d+)px", node.value)
    # 0 and 1 are hairlines and hinges, not scalable dimensions: a 1px
    # border scaled by 1.14 rounds straight back to 1, and 0 stays 0.
    offenders = [n for n in literals if int(n) > 1]
    assert not offenders, f"unscaled pixel literals: {sorted(set(offenders))}"


# ── the cards are a grid, and the logo is legible ───────────────────────────

def test_boards_are_laid_out_in_a_grid_not_a_single_column():
    """Two boards sit side by side. A single column would put them in
    rows 0 and 1 of the same column, which is the layout this replaced."""
    tmp = _saves_with(("Alpha", 0, 10), ("Beta", 10, 10))
    p = _panel(tmp)
    pos = []
    for _path, card in p._board_buttons:
        idx = p._grid.indexOf(card)
        assert idx >= 0, "a card is not in the grid at all"
        r, c, _rs, _cs = p._grid.getItemPosition(idx)
        pos.append((r, c))
    assert pos[0][0] == pos[1][0], f"cards are on different rows: {pos}"
    assert pos[0][1] != pos[1][1], f"cards share a column: {pos}"


def test_the_grid_wraps_to_a_second_row_rather_than_a_third_column():
    tmp = _saves_with(("A", 0, 10), ("B", 10, 10), ("C", 20, 10))
    p = _panel(tmp)
    rows_cols = []
    for _path, card in p._board_buttons:
        r, c, _rs, _cs = p._grid.getItemPosition(p._grid.indexOf(card))
        rows_cols.append((r, c))
    assert rows_cols == [(0, 0), (0, 1), (1, 0)], rows_cols


def test_the_empty_state_spans_the_whole_grid():
    """Centred in the left column it reads as a card that failed to load."""
    from ui.panels.file_panel import _GRID_COLS
    tmp = _saves_with()
    p = _panel(tmp)
    assert p._grid.count() == 1
    _r, _c, _rs, cs = p._grid.getItemPosition(0)
    assert cs == _GRID_COLS, f"empty state spans {cs} of {_GRID_COLS} columns"


def test_the_card_logo_is_big_enough_not_to_read_as_pixelated():
    """42 logical pixels is what it used to get in a 72 px row. The hero
    exists so it can have roughly twice that."""
    from ui.util.size_manager import size_mgr as S
    from ui.panels.file_panel import _LOGO_PX
    tmp = _saves_with(("Lesson", 0, 10))
    p = _panel(tmp)
    card = p._board_buttons[0][1]
    shown = [l for l in card.findChildren(QLabel)
             if l.pixmap() is not None and not l.pixmap().isNull()]
    assert shown, "the card is not showing the logo at all"
    pm = shown[0].pixmap()
    # Logical size — the pixmap is backed at the device ratio on a Hi-DPI
    # board, so its raw width is not what lands on screen.
    logical_w = pm.width() / max(1.0, pm.devicePixelRatio())
    assert logical_w >= S.scale_icon(_LOGO_PX) * 0.9, (
        f"logo is {logical_w:.0f} logical px, expected about "
        f"{S.scale_icon(_LOGO_PX)}")
    assert logical_w > S.scale_icon(50), \
        "the logo is back down at row-icon size"


def test_the_logo_does_not_run_flush_to_the_edge_of_its_pixmap():
    """The reported "clipped off" look. logo.ico's own margin is ~2% of the
    frame, which rounds to nothing at small sizes and puts the artwork's
    antialiased border on the boundary pixel."""
    tmp = _saves_with(("Lesson", 0, 10))
    p = _panel(tmp)
    card = p._board_buttons[0][1]
    shown = [l for l in card.findChildren(QLabel)
             if l.pixmap() is not None and not l.pixmap().isNull()]
    img = shown[0].pixmap().toImage()
    w, h = img.width(), img.height()

    def col_inked(x):
        return any(img.pixelColor(x, y).alpha() > 12 for y in range(h))

    left = next((x for x in range(w) if col_inked(x)), w)
    right = w - 1 - next((x for x in range(w - 1, -1, -1) if col_inked(x)), -1)
    assert left >= 1 and right >= 1, (
        f"ink runs to the edge: {left}px clear on the left, {right}px on the "
        "right — the logo will look shaved off again")


# ── search ──────────────────────────────────────────────────────────────────

def test_search_filters_the_list():
    tmp = _saves_with(("Trigonometry", 0, 10), ("Thermodynamics", 10, 10),
                      ("Chemistry", 20, 10))
    p = _panel(tmp)
    p._search.setText("chem")
    _app.processEvents()
    assert [x.stem for x, _ in p._board_buttons] == ["Chemistry"]


def test_search_matches_anywhere_in_the_name_not_just_the_start():
    """A teacher looking for "Chapter 4" should not have to remember that
    the board is called "Class 11-A Chapter 4"."""
    tmp = _saves_with(("Trigonometry", 0, 10), ("Chemistry", 10, 10),
                      ("Physics", 20, 10))
    p = _panel(tmp)
    p._search.setText("try")          # trigonome-TRY and chemis-TRY
    _app.processEvents()
    assert sorted(x.stem for x, _ in p._board_buttons) == \
        ["Chemistry", "Trigonometry"]


def test_search_is_case_insensitive():
    tmp = _saves_with(("Trigonometry", 0, 10))
    p = _panel(tmp)
    p._search.setText("TRIGO")
    _app.processEvents()
    assert len(p._board_buttons) == 1


def test_clearing_the_search_brings_everything_back():
    tmp = _saves_with(("A", 0, 10), ("B", 10, 10))
    p = _panel(tmp)
    p._search.setText("A")
    _app.processEvents()
    p._search.setText("")
    _app.processEvents()
    assert len(p._board_buttons) == 2


def test_a_search_that_matches_nothing_says_so_differently():
    """"No boards yet" and "nothing matches what you typed" need different
    answers — one of them wrongly suggests the work is gone."""
    tmp = _saves_with(("Trigonometry", 0, 10))
    p = _panel(tmp)
    p._search.setText("zzzz")
    _app.processEvents()
    assert p._board_buttons == []
    texts = " ".join(l.text() for l in p.findChildren(QLabel))
    assert "Nothing matches" in texts, texts
    # …and the count still reports what is actually saved.
    assert "1 board saved" in p._count_lbl.text()


def test_the_empty_panel_does_not_claim_a_search_found_nothing():
    tmp = _saves_with()
    p = _panel(tmp)
    texts = " ".join(l.text() for l in p.findChildren(QLabel))
    assert "No saved boards yet" in texts, texts
    assert "Nothing matches" not in texts


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
