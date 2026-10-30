"""
test_announcements.py — history, reports, and staying alive to warn.

Run it directly (``python test_announcements.py``); it needs no pytest.

What is worth protecting here
-----------------------------
Everything in this file exists because of one property of the acknowledgment
feature: **the warning arrives five minutes late.** A teacher sends an
announcement, closes the window, and walks to the next room. Every step
between those two facts is a place the warning can be silently lost, and none
of them announce themselves when they break — the failure mode is a
notification that simply never appears.

So these check, in order:

  * The history row offers the report and nothing else. Resend and Duplicate
    were removed; Duplicate in particular wrote a fake "(copy)" row into a
    record of things that were actually sent.
  * A row with no tracking data explains itself instead of doing nothing.
  * The composer can still be opened. It used to be handed a tuple where it
    expected a dict, and took the app down with an AttributeError.
  * Closing the window does NOT end the process while a tray icon exists,
    because the sweep that produces the warning dies with the process.
  * A tray balloon click opens the report for the announcement it was about
    — not the most recent one. There used to be a second kind of balloon,
    the "LogicShare is still running" notice shown on first close; it was
    removed, and what remains of it is the state reset that stops a tray
    click opening a stale report afterwards.
"""
import os
import sys
import time

os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")
sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))

from PySide6.QtWidgets import QApplication, QPushButton

_app = QApplication.instance() or QApplication([])

import LogicShare as LS
from logicshare_ack import Announcement, BoardStatus, BoardState

LS._init_scaling(_app)

PROFILE = {"first_name": "Yash", "surname": "Jain", "short_name": "YJ",
           "title": "Sir"}


# ── scaffolding ─────────────────────────────────────────────────────────────

class _FakeTracker:
    """The tracker's read surface, without a socket or a sweep thread."""

    class _Sig:
        def connect(self, *a): pass
        def disconnect(self, *a): pass
        def emit(self, *a): pass

    def __init__(self, items=None):
        self._items = items or {}
        self.status_changed = self._Sig()

    def get(self, aid):
        return self._items.get(aid)


def _announcement(aid="a1", *, read=1, silent=1):
    """One announcement, part acknowledged and part not."""
    ann = Announcement(announcement_id=aid, title="Exam Schedule",
                       message="Unit test tomorrow.", sent_at=time.time() - 600)
    for i in range(read):
        b = BoardStatus(board_device_id=f"r{i}", ip=f"10.0.0.{i}",
                        class_standard="11", section="A", room=f"R{i}")
        b.state = BoardState.READ
        b.read_at = time.time() - 300
        ann.boards[b.key] = b
    for i in range(silent):
        b = BoardStatus(board_device_id=f"s{i}", ip=f"10.0.1.{i}",
                        class_standard="12", section="B", room=f"S{i}")
        b.state = BoardState.TIMEOUT
        b.delivered_at = time.time() - 590
        ann.boards[b.key] = b
    return ann


def _history(entries):
    """Point load_history at a fixed list for the duration of a test."""
    class _Ctx:
        def __enter__(self):
            self._orig = LS.load_history
            LS.load_history = lambda: entries
            return self
        def __exit__(self, *e):
            LS.load_history = self._orig
            return False
    return _Ctx()


def _buttons(widget):
    return [b.text() for b in widget.findChildren(QPushButton)]


def _entry(**over):
    e = {"id": "h1", "title": "Exam Schedule", "message": "Unit test tomorrow.",
         "recipients": ["10.0.0.0", "10.0.1.0"], "sent_at": time.time() - 600,
         "announcement_id": "a1"}
    e.update(over)
    return e


# ── the history row ─────────────────────────────────────────────────────────

def test_the_row_offers_the_report_and_not_a_resend():
    tracker = _FakeTracker({"a1": _announcement()})
    with _history([_entry()]):
        dlg = LS.AnnouncementHistoryDialog(PROFILE, tracker=tracker)
    texts = " ".join(_buttons(dlg))
    assert "Show Full Report" in texts, texts
    assert "Resend" not in texts, "Resend is back"
    assert "Duplicate" not in texts, "Duplicate is back"
    dlg.close()


def test_the_report_is_offered_even_with_nothing_to_report():
    """A greyed-out button on a touch screen has no tooltip to explain
    itself with, so it just reads as broken."""
    with _history([_entry(announcement_id="")]):
        dlg = LS.AnnouncementHistoryDialog(PROFILE, tracker=_FakeTracker())
    report = [b for b in dlg.findChildren(QPushButton)
              if "Show Full Report" in b.text()]
    assert report, "the report button vanished for an untracked row"
    assert report[0].isEnabled()
    dlg.close()


def test_an_untracked_row_explains_itself_instead_of_doing_nothing():
    said = {}
    orig = LS.CustomMessageBox.information
    LS.CustomMessageBox.information = (
        lambda parent, title, body, **k: said.update(title=title, body=body,
                                                     detail=k.get("detail", "")))
    try:
        with _history([_entry(announcement_id="")]):
            dlg = LS.AnnouncementHistoryDialog(PROFILE, tracker=_FakeTracker())
        for b in dlg.findChildren(QPushButton):
            if "Show Full Report" in b.text():
                b.click()
        dlg.close()
    finally:
        LS.CustomMessageBox.information = orig
    assert said, "clicking an untracked row's report button did nothing"
    assert "no delivery record" in said["body"].lower()


def test_clicking_the_report_opens_the_report_for_that_announcement():
    tracker = _FakeTracker({"a1": _announcement()})
    with _history([_entry()]):
        dlg = LS.AnnouncementHistoryDialog(PROFILE, tracker=tracker)
        for b in dlg.findChildren(QPushButton):
            if "Show Full Report" in b.text():
                b.click()
    opened = getattr(dlg, "_open_reports", [])
    assert len(opened) == 1, "no report window opened"
    assert opened[0]._announcement_id == "a1"
    opened[0].close(); dlg.close()


def test_the_report_names_the_boards_that_never_replied():
    """The whole point of the report: which room didn't answer."""
    tracker = _FakeTracker({"a1": _announcement(read=2, silent=1)})
    rep = LS.AnnouncementReportDialog(tracker, "a1")
    from PySide6.QtWidgets import QLabel
    text = " ".join(l.text() for l in rep.findChildren(QLabel))
    assert "2 of 3 boards acknowledged" in text, text
    assert "NO ACKNOWLEDGMENT FROM 1 BOARD" in text, text
    assert "Room S0" in text, "the silent board is not named in the report"
    rep.close()


def test_a_fully_acknowledged_report_has_no_alarm_section():
    tracker = _FakeTracker({"a1": _announcement(read=2, silent=0)})
    rep = LS.AnnouncementReportDialog(tracker, "a1")
    from PySide6.QtWidgets import QLabel
    text = " ".join(l.text() for l in rep.findChildren(QLabel))
    assert "NO ACKNOWLEDGMENT FROM" not in text, text
    assert "2 of 2 boards acknowledged" in text, text
    rep.close()


# ── the composer ────────────────────────────────────────────────────────────

def test_the_composer_opens():
    """It crashed on ``prefill.get`` — a tuple where a dict was expected —
    every time somebody hit Resend in history."""
    c = LS.AnnouncementComposer(PROFILE, tracker=_FakeTracker())
    assert c.title_in.text() == ""
    assert not c.to_select_btn.isEnabled(), "empty composer offers to send"
    c.title_in.setText("T"); c.msg_in.setPlainText("M")
    assert c.to_select_btn.isEnabled()
    c.close()


def test_the_composer_takes_no_prefill_any_more():
    import inspect
    params = inspect.signature(LS.AnnouncementComposer.__init__).parameters
    assert "prefill" not in params, \
        "prefill is back — the tuple/dict crash can happen again"


def test_the_history_dialog_no_longer_advertises_a_resend():
    assert not hasattr(LS.AnnouncementHistoryDialog, "resend_requested")
    assert not hasattr(LS.AnnouncementHistoryDialog, "_duplicate")


# ── acknowledgments land on the board that sent them ────────────────────────
#
# Rows are keyed by the board's device id. When a delivery response is lost
# the row keeps only an IP, and an acknowledgment then has to be matched back
# to it. That fallback used to accept ANY row missing a device id, because
# nothing ever supplied an address to compare against — so on a send where
# several deliveries went unanswered, the first board to reply was credited to
# whichever row happened to come first, and its room number was written over
# that row on the way past. One classroom appeared twice in the report and
# another vanished from it.

def _tracker(tmpname="ack"):
    import tempfile
    from pathlib import Path as _Path
    from logicshare_ack import AnnouncementTracker
    d = tempfile.mkdtemp()
    return AnnouncementTracker(_Path(d) / f"{tmpname}.json", "td_teacher")


def _three_rooms(tr):
    """Three boards, all delivered-but-unanswered, so every row is IP-only."""
    tr.record_sent("a1", "Exam", "tomorrow", [
        {"ip": "10.0.0.11", "room": "011", "class": "11", "section": "A"},
        {"ip": "10.0.0.12", "room": "012", "class": "12", "section": "B"},
        {"ip": "10.0.0.13", "room": "013", "class": "9",  "section": "C"},
    ])
    for ip in ("10.0.0.11", "10.0.0.12", "10.0.0.13"):
        tr.record_delivery("a1", ip, False, "timed out", None)
    return tr.get("a1")


def _ack(room, ip=None):
    p = {"announcement_id": "a1", "board_device_id": f"bd_{room}",
         "teacher_device_id": "td_teacher", "room": room}
    if ip:
        p["ip"] = ip
    return p


def test_an_acknowledgment_lands_on_the_room_that_sent_it():
    tr = _tracker()
    ann = _three_rooms(tr)
    tr.record_ack(_ack("013", "10.0.0.13"))
    read = [b for b in ann.boards.values() if b.state == BoardState.READ]
    assert len(read) == 1
    assert read[0].ip == "10.0.0.13",         f"room 013 replied but the report credited {read[0].ip}"
    assert read[0].room == "013"


def test_the_other_rooms_keep_their_own_identity():
    """The wrong-row adoption also OVERWROTE the room number, so the report
    showed the acknowledging room twice and lost the other one."""
    tr = _tracker()
    ann = _three_rooms(tr)
    tr.record_ack(_ack("013", "10.0.0.13"))
    by_ip = {b.ip: b.room for b in ann.boards.values() if b.ip}
    assert by_ip == {"10.0.0.11": "011", "10.0.0.12": "012",
                     "10.0.0.13": "013"}, by_ip


def test_an_unmatchable_acknowledgment_is_added_not_guessed():
    """With no address to match on, adopting SOME row is a coin flip. An
    extra row is visibly odd; a wrongly-credited one is invisibly wrong."""
    tr = _tracker()
    ann = _three_rooms(tr)
    tr.record_ack(_ack("013"))              # no ip stamped
    for b in ann.boards.values():
        if b.ip:
            assert b.state != BoardState.READ,                 f"row {b.ip} was credited with an acknowledgment it cannot be"
    added = [b for b in ann.boards.values() if not b.ip]
    assert len(added) == 1 and added[0].state == BoardState.READ


def test_a_known_board_is_still_matched_by_its_device_id():
    """The normal path: the delivery response carried an id, so the row is
    keyed by it and no address matching is involved at all."""
    tr = _tracker()
    tr.record_sent("a1", "Exam", "x", [{"ip": "10.0.0.11", "room": "011"}])
    tr.record_delivery("a1", "10.0.0.11", True, "",
                       {"board_device_id": "bd_011"})
    assert tr.record_ack(_ack("011")) is True
    ann = tr.get("a1")
    assert all(b.state == BoardState.READ for b in ann.boards.values())


def test_the_ack_server_stamps_the_peer_address():
    """The socket knows who connected; the body only claims to. Without the
    stamp there is nothing to match a row against."""
    import inspect
    import logicshare_ack
    src = inspect.getsource(logicshare_ack.AckServer.start)
    assert "client_address" in src,         "the ack server no longer records who the acknowledgment came from"
    assert "::ffff:" in src,         "IPv4-mapped peers would never match the address we sent to"


# ── the tray: clicking the right balloon opens the right thing ──────────────

class _Recorder:
    def __init__(self): self.calls = []
    def emit(self, *a): self.calls.append(a)


def _notifier():
    n = LS.AckNotifier.__new__(LS.AckNotifier)
    from PySide6.QtCore import QObject
    QObject.__init__(n)
    n._tray = None
    n._menu = None
    n._pending_id = ""
    n._click_opens_report = False
    return n


def test_a_warning_balloon_opens_that_announcements_report():
    n = _notifier()
    rec = _Recorder(); n.open_report = rec
    n._pending_id = "a1"; n._click_opens_report = True
    n._on_clicked()
    assert rec.calls == [("a1",)], rec.calls


def test_the_still_running_notice_opens_the_window_not_a_report():
    """One messageClicked serves every balloon this icon shows. Without the
    distinction, the 'LogicShare is still running' notice would open the
    report from whatever warning fired before it."""
    n = _notifier()
    report, window = _Recorder(), _Recorder()
    n.open_report, n.show_window = report, window
    n._pending_id = "a1"          # a warning fired earlier in the session
    n._click_opens_report = False  # …but THIS balloon is the tray notice
    n._on_clicked()
    assert report.calls == [], "the tray notice opened a stale report"
    assert len(window.calls) == 1


def test_the_second_of_two_warnings_wins_the_click():
    """A teacher who sends three announcements in a lesson must land on the
    one the balloon they clicked was about."""
    n = _notifier()
    rec = _Recorder(); n.open_report = rec
    n._tray = None                       # showMessage is a no-op without one
    n.notify_timeout("a1", "First", [])
    n.notify_timeout("a2", "Second", [])
    n._on_clicked()
    assert rec.calls == [("a2",)], rec.calls


# ── staying alive ───────────────────────────────────────────────────────────

class _StubNotifier:
    """A tray that exists, without needing a real one (offscreen has none)."""
    available = True
    def __init__(self):
        self.went_bg = 0
        self.click_opens_report = None
    def went_to_background(self):
        self.went_bg += 1
        self.click_opens_report = False


def _ui():
    win = LS.LogicShareUI(PROFILE)
    win._ack_notifier_real = win._ack_notifier
    win._ack_notifier = _StubNotifier()
    return win


def test_closing_the_window_does_not_end_logicshare():
    """The sweep that produces the five-minute warning dies with the
    process. Quitting on close is why the warning never arrived."""
    win = _ui()
    win.show()
    win.close()
    assert not win.isVisible(), "the window did not hide"
    assert win._ack_tracker._sweep_timer is not None, \
        "the timeout sweep stopped when the window closed"
    win._really_quitting = True; win.close(); win._shutdown_ack()


def test_closing_the_window_says_nothing():
    """It used to pop "LogicShare is still running" the first time the
    window was closed. Removed on request — and worth removing, because a
    notification whose content is "nothing has gone wrong" is exactly the
    kind that teaches people to dismiss notifications unread, including the
    one that matters: a board that never acknowledged."""
    win = _ui()
    win.show()
    win.close()
    win.close()
    assert win._ack_notifier.went_bg >= 1, \
        "the notifier was never told the window had hidden"
    assert not hasattr(LS.AckNotifier, "announce_background"), \
        "the background balloon is back"
    win._really_quitting = True; win.close(); win._shutdown_ack()


def test_the_tray_click_goes_back_to_meaning_open_the_window():
    """The tray icon's click has two meanings. After a timeout warning it
    opens that report; after an ordinary close it has to mean "give me the
    window back" again — which is the one piece of real work the removed
    notification was also doing."""
    win = _ui()
    win.show(); win.close()
    assert win._ack_notifier.click_opens_report is False, \
        "clicking the tray icon would still open a stale report"
    win._really_quitting = True; win.close(); win._shutdown_ack()


def test_the_window_comes_back_from_the_tray():
    win = _ui()
    win.show(); win.close()
    assert not win.isVisible()
    win._restore_from_tray()
    assert win.isVisible(), "the tray click did not bring the window back"
    win._really_quitting = True; win.close(); win._shutdown_ack()


def test_quitting_from_the_tray_really_quits():
    win = _ui()
    quit_called = []
    orig = QApplication.quit
    QApplication.quit = staticmethod(lambda *a: quit_called.append(1))
    try:
        win.show()
        win._quit_completely()
    finally:
        QApplication.quit = orig
    assert win._really_quitting is True
    assert not win.isVisible()
    assert quit_called, "tray Quit did not quit"
    win._shutdown_ack()


def test_a_machine_with_no_tray_shuts_down_on_close():
    """Hiding into a tray that isn't there would leave a process with no way
    back to it."""
    win = LS.LogicShareUI(PROFILE)
    class _NoTray:
        available = False
        def went_to_background(self): pass
    win._ack_notifier = _NoTray()
    quit_called = []
    orig = QApplication.quit
    QApplication.quit = staticmethod(lambda *a: quit_called.append(1))
    try:
        win.show(); win.close()
    finally:
        QApplication.quit = orig
    assert not win.isVisible()
    assert quit_called, "closing without a tray left the process running"


def test_the_report_is_parentless_while_the_window_is_hidden():
    """A child of a hidden window is hidden too — which is exactly the case
    the notification exists for."""
    win = _ui()
    win._ack_tracker._items["a1"] = _announcement()
    win.show(); win.close()               # now in the tray
    win._open_announcement_report("a1")
    dlg = win._open_reports[-1]
    assert dlg.parent() is None, "the report opened as a child of a hidden window"
    assert dlg.isVisible(), "the notification click showed nothing"
    dlg.close()
    win._really_quitting = True; win.close(); win._shutdown_ack()


def test_the_ack_listener_is_actually_stopped_on_a_real_quit():
    """``_shutdown_ack`` existed but nothing ever called it, so the listening
    socket outlived every run."""
    win = LS.LogicShareUI(PROFILE)
    class _NoTray:
        available = False
        def went_to_background(self): pass
    win._ack_notifier = _NoTray()
    orig = QApplication.quit
    QApplication.quit = staticmethod(lambda *a: None)
    try:
        win.show(); win.close()
    finally:
        QApplication.quit = orig
    assert not win._ack_tracker._sweep_timer.isActive(),         "the sweep is still running after a real quit"


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
            import traceback; traceback.print_exc()
            print(f"  ERROR {name}: {type(exc).__name__}: {exc}")
    print(f"\n{len(failures)} failed" if failures else "\nall passed")
    sys.exit(1 if failures else 0)
