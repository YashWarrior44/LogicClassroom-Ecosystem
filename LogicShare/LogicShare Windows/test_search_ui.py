"""
test_search_ui.py — search and filter in the REAL LogicShare window.

    python test_search_ui.py

test_smart_search.py proves the query parser; this proves the window uses it:
that typing filters the board cards, that the filter menu narrows them, that
"Select all shown" ticks exactly the visible ones, and that History filters
by text, status and date. It builds the actual LogicShareUI and
AnnouncementHistoryDialog — in a child process with LOCALAPPDATA pointed at a
temp folder, so the teacher's real profile and history are never touched,
and with discovery stubbed so no network is involved.
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
    import json, os, sys, time
    sys.path.insert(0, __HERE__)
    from PySide6.QtWidgets import QApplication
    app = QApplication(sys.argv)
    import LogicShare as L

    out = {}
    L.save_profile("Yash", "Jain", "YJ", "Sir")
    L.LogicShareUI._start_zeroconf = lambda self: True
    L.LogicShareUI._greet_board = lambda self, ip: None
    w = L.LogicShareUI(L.load_profile())
    w._stop_timers()
    for cls, sec, room, ip in [("11", "A", "12", "192.0.2.10"), ("11", "B", "14", "192.0.2.11"),
                               ("12", "A", "11", "192.0.2.12"), ("9", "C", "3", "192.0.2.13")]:
        w._on_board_detected(cls, sec, room, ip)
    app.processEvents()

    def visible():
        return sorted(c.ip[-2:] for c in w._visible_cards())

    def shown_in_layout():
        ips = []
        for i in range(w._list.count()):
            wd = w._list.itemAt(i).widget()
            if wd is not None and hasattr(wd, "ip"):
                ips.append(wd.ip[-2:])
        return sorted(ips)

    out["all"] = shown_in_layout()
    w._set_search_open(True)
    out["search_row_visible"] = w._search_row.isVisible() or not w.isVisible()
    w._search_in.setText("XI A"); app.processEvents()
    out["q_XI_A"] = shown_in_layout()
    out["showing"] = w._showing_lbl.text()
    w._search_in.setText("room 14"); app.processEvents()
    out["q_room14"] = shown_in_layout()
    w._search_in.setText("nothing here"); app.processEvents()
    out["no_match_panel"] = any(
        "No boards match" in lbl.text()
        for lbl in w._scroll_host.findChildren(L.QLabel))
    w._set_search_open(False); app.processEvents()
    out["after_cancel"] = shown_in_layout()

    w._set_filter(cls="11"); app.processEvents()
    out["f_class11"] = shown_in_layout()
    out["filter_badge"] = w._filter_btn.badge()
    out["filter_active"] = w._filter_btn.is_active()
    out["chips_visible"] = not w._chips_row.isHidden()
    w._set_filter(section="B"); app.processEvents()
    out["f_class11_secB"] = shown_in_layout()
    w._set_filter(cls=None, section=None); app.processEvents()
    out["f_cleared"] = shown_in_layout()

    # Select all shown: filter to class 11, enter selection, tick all.
    w._set_filter(cls="11")
    w._enter_selection_mode("T", "M")
    out["select_all_visible"] = not w._select_all_btn.isHidden()
    w._select_all_shown(); app.processEvents()
    out["selected"] = sorted(ip[-2:] for ip in w._selected_ips)
    out["select_all_hidden_after"] = w._select_all_btn.isHidden()
    w._exit_selection_mode(); w._set_filter(cls=None)

    # History: three entries, one tracked + fully read, one untracked.
    now = time.time()
    L.save_history([
        {"id": "h1", "announcement_id": "an_read", "title": "Exam Schedule",
         "message": "Maths exam on 12th", "sent_at": now,
         "recipients": [{"class": "11", "section": "A", "room": "12", "ip": "x"}]},
        {"id": "h2", "announcement_id": "", "title": "Sports Day",
         "message": "Wear house colours", "sent_at": now - 10 * 86400,
         "recipients": [{"class": "9", "section": "C", "room": "3", "ip": "y"}]},
        {"id": "h3", "announcement_id": "an_wait", "title": "Library",
         "message": "Return books", "sent_at": now - 3600,
         "recipients": [{"class": "12", "section": "A", "room": "11", "ip": "z"}]},
    ])
    t = w._ack_tracker
    t.record_sent("an_read", "Exam Schedule", "m", [{"ip": "x", "class": "11"}])
    t.record_delivery("an_read", "x", True, "Delivered", {"board_device_id": "bd1"})
    t.record_ack({"announcement_id": "an_read", "board_device_id": "bd1",
                  "teacher_device_id": t.device_id})
    t.record_sent("an_wait", "Library", "m", [{"ip": "z", "class": "12"}])
    t.record_delivery("an_wait", "z", True, "Delivered", {})

    d = L.AnnouncementHistoryDialog(w.profile, w, tracker=t)

    def titles():
        return sorted(e["title"] for e in d._shown_entries(L.load_history()))

    out["h_all"] = titles()
    d._h_search.setText("exam 11a"); out["h_exam11a"] = titles()
    d._h_search.setText("sprts");    out["h_typo"] = titles()
    d._h_search.setText("")
    d._set_h_filter(status="all_read"); out["h_all_read"] = titles()
    d._set_h_filter(status="awaiting"); out["h_awaiting"] = titles()
    d._set_h_filter(status="all", period="week"); out["h_week"] = titles()
    d._set_h_filter(period="today"); out["h_today"] = titles()
    out["h_showing"] = d._h_showing.text()

    w._really_quitting = True
    w._shutdown_ack()
    print("RESULT " + json.dumps(out))
''').replace("__HERE__", repr(HERE))


def _run_child():
    data = tempfile.mkdtemp(prefix="ls_ui_")
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


def test_the_search_box_filters_the_real_cards():
    r = R()
    assert r["all"] == ["10", "11", "12", "13"], r["all"]
    assert r["q_XI_A"] == ["10"], f"'XI A' should find 11-A only: {r['q_XI_A']}"
    assert r["q_room14"] == ["11"], r["q_room14"]
    assert r["showing"] == "SHOWING 1 OF 4 BOARDS", r["showing"]


def test_no_match_says_so_and_cancel_restores_everything():
    r = R()
    assert r["no_match_panel"], "an empty result showed a blank list"
    assert r["after_cancel"] == ["10", "11", "12", "13"], \
        f"closing search left boards hidden: {r['after_cancel']}"


def test_filters_narrow_and_clear():
    r = R()
    assert r["f_class11"] == ["10", "11"], r["f_class11"]
    assert r["f_class11_secB"] == ["11"], r["f_class11_secB"]
    assert r["f_cleared"] == ["10", "11", "12", "13"], r["f_cleared"]
    assert r["filter_active"] and r["filter_badge"] == 1, \
        "the Filter button does not show that a filter is on, or how many"
    assert r["chips_visible"], "no chip tells the teacher a filter is on"


def test_select_all_shown_ticks_exactly_the_visible_boards():
    r = R()
    assert r["select_all_visible"]
    assert r["selected"] == ["10", "11"], \
        f"Select all shown ticked hidden boards too: {r['selected']}"
    assert r["select_all_hidden_after"], "the button stayed after everything was ticked"


def test_history_search_and_filters():
    r = R()
    assert r["h_all"] == ["Exam Schedule", "Library", "Sports Day"]
    assert r["h_exam11a"] == ["Exam Schedule"], r["h_exam11a"]
    assert r["h_typo"] == ["Sports Day"], r["h_typo"]
    assert r["h_all_read"] == ["Exam Schedule"], r["h_all_read"]
    assert r["h_awaiting"] == ["Library"], r["h_awaiting"]
    assert r["h_week"] == ["Exam Schedule", "Library"], r["h_week"]
    assert r["h_today"] == ["Exam Schedule", "Library"], r["h_today"]
    assert r["h_showing"].startswith("SHOWING 2 OF 3"), r["h_showing"]


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
