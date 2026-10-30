"""
test_net_watch.py — the network watcher reacts to the right things.

    python tests/python/test_net_watch.py

The bug this guards against is not "we failed to notice the network went
away". Everything already noticed that. It is "we failed to notice the
network became a DIFFERENT network", which no amount of online/offline
checking can see, because online is True on both sides of a Wi-Fi hop.

So most of what is asserted here is about identity, debouncing, and not
dropping a reading on the floor.
"""
from __future__ import annotations

import os
import sys
import threading
import time

sys.path.insert(0, os.path.abspath(os.path.join(os.path.dirname(__file__),
                                                "..", "..")))

from ui.logicshare import net_watch as N          # noqa: E402


# Two networks that are both perfectly online. Every presence-based check
# in the app said "connected" for both of these and did nothing.
NET_A = ("192.168.1.42", ("192.168.1.42",))
NET_B = ("10.0.0.77", ("10.0.0.77",))
OFFLINE = ("", ())


class _Fake:
    """Stands in for the OS, so a test can hop networks."""

    def __init__(self, fp=NET_A):
        self.fp = fp
        self._real = N.fingerprint

    def __enter__(self):
        N.fingerprint = lambda: self.fp
        return self

    def __exit__(self, *_a):
        N.fingerprint = self._real


def _watcher(fake, **kw):
    """A watcher with the timing knobs wound down, never started — the
    tests drive ``_evaluate`` directly so nothing depends on wall clock."""
    seen = []
    kw.setdefault("poll_seconds", 0.05)
    kw.setdefault("settle_seconds", 0.0)
    kw.setdefault("min_gap", 0.0)
    w = N.NetworkWatcher(lambda o, n, r: seen.append((o, n, r)), **kw)
    w._fp = fake.fp
    return w, seen


# ── identity, not presence ────────────────────────────────────────────────

def test_a_hop_between_two_working_networks_is_a_change():
    """THE case. Both sides are online, so nothing that asks "is there a
    network" can tell them apart — and that is exactly the hop a teacher
    makes walking from the corridor into the classroom."""
    with _Fake(NET_A) as fake:
        w, seen = _watcher(fake)
        fake.fp = NET_B
        w._evaluate("poll")
        assert len(seen) == 1, "hopping to a different network raised nothing"
        old, new, _r = seen[0]
        assert old == NET_A and new == NET_B
        assert N.is_online(old) and N.is_online(new), \
            "this test is meaningless unless both networks are online"


def test_staying_on_the_same_network_is_not_a_change():
    """A poll that finds nothing new must stay silent, or every tick would
    rebuild Zeroconf."""
    with _Fake(NET_A) as fake:
        w, seen = _watcher(fake)
        for _ in range(5):
            w._evaluate("poll")
        assert seen == [], f"idle polling fired {len(seen)} rebuild(s)"


def test_an_os_event_fires_even_when_the_address_is_identical():
    """DHCP re-issues the same lease constantly, and two SSIDs on one
    router share a subnet. The address cannot witness an adapter having
    gone down and come back — but the socket bound to it died anyway, so
    the notification has to be believed over the address."""
    with _Fake(NET_A) as fake:
        w, seen = _watcher(fake)
        w._evaluate("os-event", always=True)
        assert len(seen) == 1, \
            "an OS notification with an unchanged IP was ignored"
        assert seen[0][2] == "os-event"


def test_going_offline_and_coming_back_are_both_changes():
    with _Fake(NET_A) as fake:
        w, seen = _watcher(fake)
        fake.fp = OFFLINE
        w._evaluate("poll")
        fake.fp = NET_B
        w._evaluate("poll")
        assert len(seen) == 2, [s[2] for s in seen]
        assert not N.is_online(seen[0][1]), "offline was not reported as offline"
        assert N.is_online(seen[1][1])


# ── debouncing ────────────────────────────────────────────────────────────

def test_a_burst_of_notifications_rebuilds_once():
    """Bringing an adapter up emits several notifications — per address
    family, per stage. Each callback tears down and rebuilds a Zeroconf
    instance, which is expensive enough that six of them freeze the UI."""
    clock = [100.0]
    with _Fake(NET_A) as fake:
        w, seen = _watcher(fake, min_gap=1.0, clock=lambda: clock[0])
        for _ in range(6):
            fake.fp = NET_B
            w._evaluate("os-event", always=True)
        assert len(seen) == 1, f"a single hop fired {len(seen)} rebuilds"


def test_the_suppressed_readings_are_still_remembered():
    """The subtle one. If a debounced call discards its reading, the
    watcher keeps comparing against a stale fingerprint — so the NEXT
    real change looks like it has already been handled and is dropped.
    That turns a debounce into a missed hop."""
    clock = [100.0]
    with _Fake(NET_A) as fake:
        w, seen = _watcher(fake, min_gap=1.0, clock=lambda: clock[0])
        fake.fp = NET_B
        w._evaluate("poll")                 # fires, we are now on B
        assert len(seen) == 1
        fake.fp = NET_A
        w._evaluate("poll")                 # suppressed by the gap
        assert len(seen) == 1
        assert w.fingerprint == NET_A, \
            "the suppressed reading was thrown away, not recorded"
        clock[0] += 5.0                     # gap has passed
        fake.fp = NET_A
        w._evaluate("poll")
        assert len(seen) == 1, \
            "re-reported a network it had already caught up to"
        fake.fp = NET_B
        w._evaluate("poll")
        assert len(seen) == 2, "the next genuine hop was swallowed"


def test_the_gap_lets_a_later_change_through():
    clock = [100.0]
    with _Fake(NET_A) as fake:
        w, seen = _watcher(fake, min_gap=1.0, clock=lambda: clock[0])
        fake.fp = NET_B
        w._evaluate("poll")
        clock[0] += 2.0
        fake.fp = NET_A
        w._evaluate("poll")
        assert len(seen) == 2, "a change after the debounce window was lost"


# ── the address rules ─────────────────────────────────────────────────────

def test_a_failed_dhcp_address_does_not_count_as_a_network():
    """169.254.x.x is what Windows assigns when DHCP fails. Counting it
    makes a network that did not come up look like one that did, so the
    app shows connected and then silently reaches nothing."""
    assert not N._usable("169.254.10.4")
    assert not N._usable("127.0.0.1")
    assert not N._usable("")
    assert N._usable("192.168.1.5")


def test_describe_says_offline_when_there_is_nothing():
    assert N.describe(OFFLINE) == "offline"
    assert "192.168.1.42" in N.describe(NET_A)
    multi = ("192.168.1.42", ("10.5.5.5", "192.168.1.42"))
    text = N.describe(multi)
    assert text.startswith("192.168.1.42") and "10.5.5.5" in text


def test_the_real_machine_reports_a_coherent_fingerprint():
    """Runs the actual code path against the actual OS."""
    fp = N.fingerprint()
    primary, ips = fp
    assert isinstance(ips, tuple) and list(ips) == sorted(ips)
    if primary:
        assert primary in ips, \
            f"primary {primary} is missing from the address set {ips}"
    for ip in ips:
        assert N._usable(ip), f"unusable address in the set: {ip}"


# ── lifecycle ─────────────────────────────────────────────────────────────

def test_a_broken_handler_does_not_kill_the_watcher():
    """The callback tears down Zeroconf and touches the UI. If a bad tick
    could kill the thread, one transient failure would leave the app
    permanently blind to the network for the rest of the session."""
    with _Fake(NET_A) as fake:
        calls = []

        def boom(_o, _n, _r):
            calls.append(1)
            raise RuntimeError("handler exploded")

        w = N.NetworkWatcher(boom, min_gap=0.0)
        w._fp = NET_A
        fake.fp = NET_B
        w._evaluate("poll")
        fake.fp = NET_A
        w._evaluate("poll")
        assert len(calls) == 2, "the watcher stopped after a handler raised"


def test_stop_ends_the_threads_and_the_callbacks():
    seen = []
    w = N.NetworkWatcher(lambda o, n, r: seen.append(r),
                         poll_seconds=0.05, settle_seconds=0.0, min_gap=0.0,
                         name="test")
    w.start()
    assert w.running
    w.stop()
    time.sleep(0.25)
    assert not w.running
    before = len(seen)
    time.sleep(0.25)
    assert len(seen) == before, "callbacks kept arriving after stop()"


def test_force_check_wakes_the_poll_loop_without_waiting_for_it():
    """Something else — a send that failed, a dead socket — can already
    know the network moved. It must not have to wait out the poll."""
    seen = []
    w = N.NetworkWatcher(lambda o, n, r: seen.append(r),
                         poll_seconds=30.0,        # far longer than the test
                         settle_seconds=0.0, min_gap=0.0, name="test")
    w.start()
    try:
        t0 = time.monotonic()
        w.force_check()
        deadline = t0 + 3.0
        while not seen and time.monotonic() < deadline:
            time.sleep(0.02)
        assert seen, "force_check did not produce a check within 3s"
        assert time.monotonic() - t0 < 3.0
        assert seen[0] == "forced"
    finally:
        w.stop()


def test_starting_twice_does_not_double_the_threads():
    w = N.NetworkWatcher(lambda *_a: None, poll_seconds=0.05, name="test")
    w.start()
    n = len(w._threads)
    w.start()
    try:
        assert len(w._threads) == n, "start() spawned a second set of threads"
    finally:
        w.stop()


def test_the_watcher_uses_the_os_instead_of_polling_where_it_can():
    """On Windows the whole point is not to poll. If the lookup ever
    silently starts failing, the app quietly reverts to 5-second latency
    and nothing else says so."""
    if not sys.platform.startswith("win"):
        return
    assert N.os_notifications_available(), \
        "NotifyAddrChange did not resolve — reaction time falls back to the poll"
    w = N.NetworkWatcher(lambda *_a: None, poll_seconds=30.0, name="test")
    w.start()
    try:
        names = [t.name for t in w._threads]
        assert any("NetNotify" in n for n in names), \
            f"no OS-notification thread was started: {names}"
    finally:
        w.stop()


def test_the_notification_thread_does_not_spin():
    """``NotifyAddrChange`` blocks until something happens. If it ever
    returned immediately instead, this loop would become a busy-wait that
    rebuilds Zeroconf as fast as the CPU allows."""
    if not sys.platform.startswith("win"):
        return
    fn = N._load_notify()
    assert fn is not None
    done = threading.Event()

    def call():
        try:
            fn(None, None)
        finally:
            done.set()

    threading.Thread(target=call, daemon=True).start()
    returned = done.wait(2.0)
    assert not returned, \
        "NotifyAddrChange returned on a stable network — the notify loop " \
        "would spin at full speed"


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
