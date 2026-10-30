"""
test_android_parity.py — the main window is LogicShare Android's, on solid
neutral surfaces.

    python test_android_parity.py

The Windows board list was redesigned to match the phone: the gear beside
the status badge, an always-visible search box with the filter square at its
end, one-line card titles, a slim two-row toolbar. Then the blur was scrapped
for solid black-and-grey surfaces. Each of those is easy to undo by
accident, so each is pinned here.

The window is built for real, in a child process with LOCALAPPDATA pointed
at a temp folder (the teacher's profile is never touched) and discovery
stubbed.
"""
from __future__ import annotations

import json
import os
import subprocess
import sys
import tempfile
import textwrap

HERE = os.path.dirname(os.path.abspath(__file__))

CHILD = textwrap.dedent(r'''
    import json, os, sys
    sys.path.insert(0, __HERE__)
    from PySide6.QtCore import Qt, QPoint, QEventLoop, QTimer
    from PySide6.QtGui import QKeyEvent, QFontMetricsF
    from PySide6.QtWidgets import QApplication
    app = QApplication(sys.argv)
    import LogicShare as L
    L._init_scaling(app)

    def settle(ms=500):
        loop = QEventLoop(); QTimer.singleShot(ms, loop.quit); loop.exec()

    out = {}
    L.save_profile("Yash", "Jain", "YJ", "Sir")
    L.LogicShareUI._start_zeroconf = lambda self: True
    L.LogicShareUI._greet_board = lambda self, ip: None
    w = L.LogicShareUI(L.load_profile())
    w._stop_timers(); w._net_timer.stop()
    w.resize(680, 880)
    for cls, sec, room, ip in [("9", "C", "3", "192.0.2.1"), ("11", "A", "12", "192.0.2.2"),
                               ("12", "B", "204B", "192.0.2.3")]:
        w._on_board_detected(cls, sec, room, ip)
    w.show(); settle()

    def right_edge(wd, inset=0):
        return wd.mapTo(w, QPoint(wd.width() - 1 - inset, 0)).x()

    def top(wd, inset=0):
        return wd.mapTo(w, QPoint(0, inset)).y()

    # Header geometry.
    gear = w._gear; fb = w._filter_btn
    room = round(L.IconSquare.BADGE_ROOM * L.UI_SCALE)
    out["search_visible"] = w._search_row.isVisible()
    out["gear_right"] = right_edge(gear)
    out["filter_square_right"] = right_edge(fb, room)
    out["gear_beside_badge"] = gear.mapTo(w, QPoint(0, 0)).x() > w._badge.mapTo(w, QPoint(0, 0)).x()
    out["gear_row_above_search"] = top(gear) < top(w._search_row)
    out["search_bottom"] = w._search_row.mapTo(w, QPoint(0, w._search_row.height())).y()
    out["filter_bottom"] = fb.mapTo(w, QPoint(0, fb.height())).y()

    # Toolbar: one height for every bar button, a slim bar.
    out["bar_heights"] = sorted({w._announce_btn.height(), w._sel_send_btn.height()})
    out["ctrl_h"] = round(L.T.CTRL_H * L.UI_SCALE)

    # Filter badge counts filters; Esc clears search and drops the caret.
    w._set_filter(cls="11"); out["badge1"] = fb.badge()
    w._set_filter(section="A"); out["badge2"] = fb.badge()
    w._set_filter(cls=None, section=None); out["badge0"] = fb.badge(); out["active0"] = fb.is_active()
    w._set_search_open(True); w._search_in.setText("11a"); settle(50)
    out["focused"] = w._search_in.hasFocus()
    w._search_in.keyPressEvent  # noqa
    QApplication.sendEvent(w._search_in, QKeyEvent(QKeyEvent.Type.KeyPress, Qt.Key.Key_Escape, Qt.KeyboardModifier.NoModifier))
    settle(50)
    out["after_esc_text"] = w._search_in.text()
    out["after_esc_focus"] = w._search_in.hasFocus()
    out["after_esc_visible"] = w._search_row.isVisible()

    # Card titles: one line whatever the name; same height for every card.
    heights = sorted({c.height() for c in w.board_cards.values()})
    out["card_heights"] = heights
    lbl = L.FitLabel("Class 12-B · Room 204B — a deliberately enormous room name",
                     color=L.T.TEXT, max_px=14.5, min_px=8.5)
    lbl.resize(120, 30); lbl.show(); settle(20)
    fm = QFontMetricsF(lbl.font())
    out["fit_one_line"] = fm.height() <= lbl.height() + 1
    out["fit_shrunk"] = lbl.font().pixelSize() < round(14.5 * L.UI_SCALE * L.FONT_SCALE)
    out["fit_elided"] = lbl.text() != lbl.full_text() and lbl.toolTip() == lbl.full_text()
    short = L.FitLabel("Class 9-C · Room 3", color=L.T.TEXT, max_px=14.5, min_px=8.5)
    short.resize(400, 30); short.show(); settle(20)
    out["short_full_size"] = short.font().pixelSize() == round(14.5 * L.UI_SCALE * L.FONT_SCALE)
    out["short_untouched"] = short.text() == short.full_text()

    w._really_quitting = True
    w._shutdown_ack()
    print("RESULT " + json.dumps(out))
    sys.stdout.flush()
    os._exit(0)
''').replace("__HERE__", repr(HERE))


def _run_child():
    data = tempfile.mkdtemp(prefix="ls_parity_")
    env = dict(os.environ, LOCALAPPDATA=data, QT_QPA_PLATFORM="offscreen")
    script = os.path.join(data, "child.py")
    with open(script, "w", encoding="utf-8") as fh:
        fh.write(CHILD)
    r = subprocess.run([sys.executable, script], capture_output=True, text=True,
                       env=env, timeout=120)
    line = [l for l in r.stdout.splitlines() if l.startswith("RESULT ")]
    assert line, f"child failed:\n{r.stdout[-1500:]}\n{r.stderr[-2500:]}"
    return json.loads(line[-1][7:])


_R = None


def R():
    global _R
    if _R is None:
        _R = _run_child()
    return _R


# ── the header ──────────────────────────────────────────────────────────────

def test_the_search_box_is_always_there():
    assert R()["search_visible"], "the search box is hidden behind a button again"


def test_the_gear_sits_beside_the_status_badge_on_the_top_row():
    r = R()
    assert r["gear_beside_badge"], "the gear is not to the right of the status badge"
    assert r["gear_row_above_search"], "the gear is not on the top row"


def test_the_filter_square_lines_up_under_the_gear():
    """The filter square reserves room for its badge on the right; without
    pulling its row in by that much, its edge sat 6 px short of the gear's."""
    r = R()
    assert abs(r["gear_right"] - r["filter_square_right"]) <= 1, \
        f"gear right edge {r['gear_right']} vs filter square {r['filter_square_right']}"


def test_the_search_box_and_filter_square_share_a_baseline():
    r = R()
    assert abs(r["search_bottom"] - r["filter_bottom"]) <= 1, (r["search_bottom"], r["filter_bottom"])


# ── filter + search behaviour ───────────────────────────────────────────────

def test_the_filter_badge_counts_the_filters():
    r = R()
    assert (r["badge1"], r["badge2"], r["badge0"]) == (1, 2, 0), (r["badge1"], r["badge2"], r["badge0"])
    assert not r["active0"], "the filter square stayed lit with no filter on"


def test_escape_clears_the_search_and_puts_the_caret_away():
    r = R()
    assert r["after_esc_text"] == "", r["after_esc_text"]
    assert not r["after_esc_focus"], "Esc left the caret in the search box"
    assert r["after_esc_visible"], "Esc hid the search box"


# ── cards + toolbar ─────────────────────────────────────────────────────────

def test_every_card_is_the_same_height():
    r = R()
    assert len(r["card_heights"]) == 1, f"cards of different heights: {r['card_heights']}"


def test_a_long_title_shrinks_then_elides_but_never_wraps():
    r = R()
    assert r["fit_one_line"], "the title wrapped"
    assert r["fit_shrunk"], "the title did not shrink before eliding"
    assert r["fit_elided"], "an elided title lost its full text (no tooltip)"


def test_a_short_title_is_left_alone():
    r = R()
    assert r["short_full_size"] and r["short_untouched"]


def test_the_toolbar_buttons_are_one_slim_height():
    r = R()
    assert r["bar_heights"] == [r["ctrl_h"]], f"{r['bar_heights']} vs CTRL_H {r['ctrl_h']}"


def _lum(c):
    def ch(v):
        v /= 255
        return v / 12.92 if v <= 0.04045 else ((v + 0.055) / 1.055) ** 2.4
    return 0.2126 * ch(c[0]) + 0.7152 * ch(c[1]) + 0.0722 * ch(c[2])


def _contrast(a, b):
    la, lb = sorted((_lum(a), _lum(b)), reverse=True)
    return (la + 0.05) / (lb + 0.05)


def _hex(h):
    h = h.lstrip("#")
    return tuple(int(h[i:i + 2], 16) for i in (0, 2, 4))


def _ls():
    os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")
    os.environ["LOCALAPPDATA"] = tempfile.mkdtemp(prefix="ls_parity_mod_")
    sys.path.insert(0, HERE)
    from PySide6.QtWidgets import QApplication
    QApplication.instance() or QApplication([])
    import LogicShare
    return LogicShare


# ── no blur ─────────────────────────────────────────────────────────────────
# The acrylic was scrapped: a blur makes every surface depend on whatever is
# behind the window. These keep it scrapped.

def _src():
    return open(os.path.join(HERE, "LogicShare.py"), encoding="utf-8").read()


def test_nothing_blurs_the_desktop():
    src = _src()
    for needle in ("SetWindowCompositionAttribute", "ACCENT_ENABLE", "DWMWA_SYSTEMBACKDROP",
                   "QGraphicsBlurEffect", "grabWindow", "_grain_tile"):
        assert needle not in src, f"{needle} is back — the window is blurring again"


def test_the_window_body_is_solid_and_dark():
    """The body is painted opaque, so what is behind the window can never
    change how it looks; and it is the darkest surface in the app."""
    src = _src()
    i = src.index("def paint_window_glass(widget):")
    body_fn = src[i:src.index("\n\n\n", i)]
    assert "QColor(0, 0, 0, 1)" not in body_fn, "the body went back to a 1 % hit-test fill"
    assert "T.BODY" in body_fn
    L = _ls()
    top, bottom = L.T.BODY
    assert len(top) == len(bottom) == 3, "BODY carries an alpha — it must be opaque"
    assert sum(top) > sum(bottom), "the body is lit from below"
    assert max(top) <= 40, f"the window body {top} is not a dark tone"


def test_the_palette_is_neutral_grey():
    """Black and grey: no channel of a surface more than a few units off the
    others (the old palette ran visibly navy)."""
    L = _ls()
    T = L.T
    for name in ("SURF_BAR", "SURF_CARD"):
        for stop in getattr(T, name):
            r, g, b = stop[:3]
            assert max(r, g, b) - min(r, g, b) <= 6, f"{name} {stop[:3]} is tinted"
    for stop in T.BODY:
        assert max(stop) - min(stop) <= 6, f"BODY {stop} is tinted"


def test_body_text_contrast_on_every_surface():
    L = _ls()
    T = L.T
    for surf_name, rgb in (("body", T.BODY[1]), ("bar", T.SURF_BAR[1][:3]),
                           ("card", T.SURF_CARD[0][:3])):
        for tok, minimum in (("TEXT", 7.0), ("TEXT_SEC", 4.5)):
            cr = _contrast(_hex(getattr(T, tok)), rgb)
            assert cr >= minimum, f"{tok} on {surf_name} {rgb} is {cr:.1f}:1"


def test_the_bare_caption_is_not_in_the_dim_colour():
    """The one caption that sits straight on the window body."""
    assert "self._caption_color = T.TEXT_SEC" in _src()


def test_every_window_paints_the_same_glass():
    assert _src().count("paint_window_glass(self)") == 5, \
        "a window paints its own body again instead of paint_window_glass"


if __name__ == "__main__":
    failures = []
    for name, fn in sorted(globals().items()):
        if not name.startswith("test_") or not callable(fn):
            continue
        try:
            fn()
            print(f"  ok    {name}")
        except AssertionError as e:
            failures.append(name)
            print(f"  FAIL  {name}: {e}")
    print("all passed" if not failures else f"{len(failures)} failed")
    sys.exit(1 if failures else 0)
