"""
test_single_instance.py — a second launch wakes the first instead of
becoming a second process.

    python test_single_instance.py

Everything here runs REAL processes. A mutex race, a pipe handoff and a
crashed owner are properties of the OS, not of a function, and a test that
mocked them would pass against code that does not work.

Every test uses its own random app id, so none of them can collide with a
LogicShare the teacher actually has open while the suite runs.
"""
from __future__ import annotations

import os
import subprocess
import sys
import tempfile
import textwrap
import time
import uuid

HERE = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, HERE)

import single_instance as SI                           # noqa: E402

# A stand-in for LogicShare's startup, using SingleInstance exactly the way
# LogicShare.py does: claim first, hand off if secondary, otherwise build a
# window and register the show handler.
CHILD = textwrap.dedent(r'''
    import os, sys, time
    sys.path.insert(0, __HERE__)
    mode, app_id, status = sys.argv[1], sys.argv[2], sys.argv[3]

    def note(line):
        # One file per process. Two processes appending to one shared file
        # at the same instant can both seek to the same end offset on
        # Windows, and one line silently overwrites the other — which made
        # a race test report a missing primary that had run perfectly.
        with open("%s.%d" % (status, os.getpid()), "a", encoding="utf-8") as fh:
            fh.write(line + "\n")

    from PySide6.QtWidgets import QApplication, QWidget
    from PySide6.QtCore import QTimer
    app = QApplication(sys.argv)
    app.setQuitOnLastWindowClosed(False)
    import single_instance as SI

    guard = SI.SingleInstance(app_id)
    if not guard.is_primary:
        ok = guard.hand_off()
        note("secondary handed_off=%s pid=%d" % (ok, os.getpid()))
        sys.exit(0 if ok else 3)

    if mode == "hung":
        # Holds the claim and never services its event loop.
        note("primary-hung pid=%d" % os.getpid())
        time.sleep(30)
        sys.exit(0)

    w = QWidget()
    w.setWindowTitle("si-test-" + app_id)
    w.resize(200, 120)
    w.show()
    app.processEvents()
    if mode == "tray":
        w.hide()                       # what closing to the tray does
    elif mode == "minimised":
        w.showMinimized()
    app.processEvents()

    def shown():
        SI.bring_forward(w)
        app.processEvents()
        note("shown visible=%s minimised=%s" % (w.isVisible(), w.isMinimized()))
    guard.set_show_handler(shown)
    note("primary pid=%d listening=%s" % (os.getpid(), guard.server.listening))
    QTimer.singleShot(int(float(os.environ.get("SI_LIFETIME", "20")) * 1000),
                      app.quit)
    app.exec()
''').replace("__HERE__", repr(HERE))


def _script():
    path = os.path.join(tempfile.gettempdir(), "si_child_%d.py" % os.getpid())
    with open(path, "w", encoding="utf-8") as fh:
        fh.write(CHILD)
    return path


SCRIPT = _script()


def _spawn(mode, app_id, status, lifetime=20):
    env = dict(os.environ, SI_LIFETIME=str(lifetime))
    return subprocess.Popen([sys.executable, SCRIPT, mode, app_id, status],
                            env=env, stdout=subprocess.DEVNULL,
                            stderr=subprocess.PIPE)


def _lines(status):
    import glob
    out = []
    for f in sorted(glob.glob(glob.escape(status) + ".*")):
        try:
            with open(f, encoding="utf-8") as fh:
                out += [l.strip() for l in fh if l.strip()]
        except OSError:
            pass
    return out


def _wait_for(status, prefix, timeout=15.0):
    end = time.monotonic() + timeout
    while time.monotonic() < end:
        hits = [l for l in _lines(status) if l.startswith(prefix)]
        if hits:
            return hits
        time.sleep(0.05)
    raise AssertionError("never saw %r in %s: %s"
                         % (prefix, status, _lines(status)))


def _fresh():
    app_id = "LSTest" + uuid.uuid4().hex[:10]
    status = os.path.join(tempfile.gettempdir(), app_id + ".status")
    return app_id, status


def _kill(*procs):
    for p in procs:
        if p and p.poll() is None:
            p.kill()
            p.wait(5)


# ── the tests ────────────────────────────────────────────────────────────

def test_a_second_launch_exits_and_the_first_comes_back_from_the_tray():
    """THE behaviour. The first instance is hidden in the tray, exactly as
    closing the window leaves it. Launching again must not leave a second
    process behind, and must bring the first one's window back."""
    app_id, status = _fresh()
    a = _spawn("tray", app_id, status)
    try:
        _wait_for(status, "primary")
        t0 = time.monotonic()
        b = _spawn("tray", app_id, status)
        rc = b.wait(15)
        elapsed = time.monotonic() - t0
        assert rc == 0, "second launch exited %s: %s" % (
            rc, b.stderr.read().decode(errors="replace")[-400:])
        assert a.poll() is None, "the FIRST instance died"
        shown = _wait_for(status, "shown")
        assert "visible=True" in shown[-1], \
            "the tray window was not brought back: %s" % shown[-1]
        assert "minimised=False" in shown[-1]
        prim = [l for l in _lines(status) if l.startswith("primary")]
        assert len(prim) == 1, "two processes claimed to be primary: %s" % prim
        print("      (second launch handed off and exited in %.2fs)" % elapsed)
    finally:
        _kill(a)


def test_a_minimised_window_is_restored_not_left_minimised():
    """A window hidden while minimised comes back minimised, which looks
    exactly like the launch doing nothing."""
    app_id, status = _fresh()
    a = _spawn("minimised", app_id, status)
    try:
        _wait_for(status, "primary")
        b = _spawn("minimised", app_id, status)
        assert b.wait(15) == 0
        shown = _wait_for(status, "shown")
        assert "minimised=False" in shown[-1], shown[-1]
        assert "visible=True" in shown[-1], shown[-1]
    finally:
        _kill(a)


def test_launching_five_more_times_still_leaves_one_process():
    """Teachers double-click, then click again because nothing seemed to
    happen. Every one of those must hand off."""
    app_id, status = _fresh()
    a = _spawn("tray", app_id, status)
    try:
        _wait_for(status, "primary")
        extra = [_spawn("tray", app_id, status) for _ in range(5)]
        codes = [p.wait(20) for p in extra]
        assert codes == [0] * 5, "exit codes %s" % codes
        assert a.poll() is None
        secs = [l for l in _lines(status) if l.startswith("secondary")]
        assert len(secs) == 5 and all("handed_off=True" in s for s in secs), secs
        assert len([l for l in _lines(status) if l.startswith("primary")]) == 1
    finally:
        _kill(a)


def test_two_simultaneous_launches_with_nothing_running_make_one_primary():
    """The double-click race. Both start with no instance running; both try
    to claim in the same instant. The kernel must let exactly one win, and
    the loser must hand off to it rather than also starting up."""
    for round_ in range(5):
        app_id, status = _fresh()
        a = _spawn("tray", app_id, status)
        b = _spawn("tray", app_id, status)
        try:
            end = time.monotonic() + 20
            while time.monotonic() < end:
                lines = _lines(status)
                if any(l.startswith("primary") for l in lines) and \
                        any(l.startswith("secondary") for l in lines):
                    break
                time.sleep(0.05)
            lines = _lines(status)
            prim = [l for l in lines if l.startswith("primary")]
            secs = [l for l in lines if l.startswith("secondary")]
            assert len(prim) == 1, "round %d: %d primaries: %s" % (
                round_, len(prim), lines)
            assert len(secs) == 1 and "handed_off=True" in secs[0], \
                "round %d: loser did not hand off: %s" % (round_, lines)
            # The loser writes its line and THEN exits, so give it the moment
            # it needs to actually be gone before counting.
            end = time.monotonic() + 5
            while time.monotonic() < end and \
                    sum(p.poll() is None for p in (a, b)) > 1:
                time.sleep(0.05)
            alive = [p for p in (a, b) if p.poll() is None]
            assert len(alive) == 1, "round %d: %d processes left running" % (
                round_, len(alive))
        finally:
            _kill(a, b)


def test_a_crashed_instance_leaves_no_stale_lock():
    """The installer ends LogicShare with TerminateProcess before replacing
    it, and a crash ends it the same way. If the claim survived that, the
    next launch would hand off to nobody and LogicShare would never open
    again until a reboot."""
    app_id, status = _fresh()
    a = _spawn("tray", app_id, status)
    _wait_for(status, "primary")
    a.kill()                              # TerminateProcess, no cleanup
    a.wait(5)
    b = _spawn("tray", app_id, status)
    try:
        end = time.monotonic() + 15
        while time.monotonic() < end:
            if len([l for l in _lines(status) if l.startswith("primary")]) >= 2:
                break
            time.sleep(0.05)
        prim = [l for l in _lines(status) if l.startswith("primary")]
        assert len(prim) == 2, \
            "after the first was killed, the next launch did not take over: %s" \
            % _lines(status)
        assert b.poll() is None, "the replacement exited instead of running"
    finally:
        _kill(b)


def test_a_hung_instance_is_reported_not_duplicated():
    """An instance that holds the claim but never answers. The second launch
    must give up and say so — not wait forever, and not start a second copy
    anyway, which would bring back every problem this exists to prevent."""
    app_id, status = _fresh()
    a = _spawn("hung", app_id, status)
    try:
        _wait_for(status, "primary-hung")
        t0 = time.monotonic()
        b = _spawn("tray", app_id, status)
        rc = b.wait(SI.CONNECT_DEADLINE_S + 10)
        took = time.monotonic() - t0
        assert rc == 3, "expected the 'could not hand off' exit, got %s" % rc
        assert took < SI.CONNECT_DEADLINE_S + 8, "gave up after %.1fs" % took
        assert not [l for l in _lines(status) if l.startswith("primary pid")], \
            "a second instance started up next to the hung one"
    finally:
        _kill(a)


def test_a_launch_during_startup_waits_for_the_pipe():
    """The first instance claims the mutex before it opens the pipe. A
    second launch landing in that gap must retry, not conclude the first is
    hung."""
    app_id, status = _fresh()
    mutex, pipe = SI.instance_names(app_id)
    claim = SI._Claim(mutex)
    assert claim.acquire()
    from PySide6.QtWidgets import QApplication
    app = QApplication.instance() or QApplication([])
    try:
        b = _spawn("tray", app_id, status)
        time.sleep(1.0)                      # B is now retrying
        server = SI.InstanceServer(pipe, on_show=lambda: None)
        end = time.monotonic() + 10
        while b.poll() is None and time.monotonic() < end:
            app.processEvents()
            time.sleep(0.02)
        assert b.poll() == 0, "the launch gave up during the startup gap"
        assert server.requests == 1
        server.close()
    finally:
        claim.release()


def test_names_are_per_user_and_per_session():
    mutex, pipe = SI.instance_names("LogicShare")
    assert mutex.startswith("Local\\"), \
        "the mutex must be session-local, or two teachers on one board via " \
        "fast user switching would share one LogicShare: %s" % mutex
    assert SI._safe(SI._user()) in mutex and SI._safe(SI._user()) in pipe
    assert "-s%d" % SI._session_id() in pipe
    assert all(c.isalnum() or c in "._-\\" for c in mutex), mutex


def test_the_real_app_claims_before_it_builds_anything():
    """The guard has to sit before the profile dialog, the window, Zeroconf
    and the update check — a second launch should cost nothing and touch
    nothing."""
    src = open(os.path.join(HERE, "LogicShare.py"), encoding="utf-8").read()
    main = src[src.index('if __name__ == "__main__":'):]
    guard = main.index("single_instance.SingleInstance()")
    for later in ("load_profile()", "LogicShareUI(profile)",
                  "check_for_updates(", "_init_scaling(qt_app)\n\n    profile"):
        if later in main:
            assert guard < main.index(later), \
                "the instance check runs after %s" % later.strip()
    assert main.index("QApplication(sys.argv)") < guard, \
        "the guard needs QApplication to exist first"
    assert "set_show_handler(" in main, "nobody handles the show request"


if __name__ == "__main__":
    failures = []
    try:
        for name, fn in sorted(globals().items()):
            if not name.startswith("test_") or not callable(fn):
                continue
            try:
                fn()
                print(f"  ok    {name}")
            except AssertionError as exc:
                failures.append(name)
                print(f"  FAIL  {name}: {exc}")
            except Exception as exc:                   # noqa: BLE001
                failures.append(name)
                print(f"  ERROR {name}: {type(exc).__name__}: {exc}")
    finally:
        try:
            os.remove(SCRIPT)
        except OSError:
            pass
    print(f"\n{len(failures)} failed" if failures else "\nall passed")
    sys.exit(1 if failures else 0)
