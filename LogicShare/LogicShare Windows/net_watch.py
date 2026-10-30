"""
net_watch.py — know the moment the network changes, not two seconds later.

The problem this solves
-----------------------
Every part of LogicShare used to answer one question about the network:
is there one? That is not the question that matters in a classroom. A
teacher's laptop hops from the 2.4 GHz SSID to the 5 GHz one, or from
guest Wi-Fi to the staff LAN, and at no point does the answer to "is there
a network" change. It stays True the whole way across. So nothing rebuilt,
and:

  - the board kept advertising an address it no longer owned,
  - the transmitter kept showing boards that had ceased to exist,
  - the multicast socket stayed bound to an adapter that was gone, so not
    even the mDNS goodbye for those boards could arrive.

The only way out was hitting Restart by hand.

Two things fix that, and this module is both of them.

IDENTITY, NOT PRESENCE
    ``fingerprint()`` describes WHICH network we are on, not whether we
    have one. When it changes, whoever is watching has to re-register,
    re-scan, and drop anything it learned from the old network.

IMMEDIACY
    Windows will tell us. ``iphlpapi!NotifyAddrChange`` blocks until an
    address actually changes and then returns — no poll interval, no
    latency budget, no UDP probe per tick. Measured on this machine: the
    call sits blocked indefinitely on a stable network, so it costs one
    parked thread and nothing else.

A poll still runs underneath, at a relaxed interval, because the
notification says something changed, not that the change has finished.
DHCP can take a moment to settle after the adapter reports up, and a
fingerprint read too early is the old one.

Rebuild on NOTIFICATION, not only on a changed fingerprint
----------------------------------------------------------
The watcher fires its callback whenever the OS reports a change, even if
``fingerprint()`` comes back identical. That is deliberate. Two SSIDs on
the same router hand out the same subnet, and DHCP often re-issues the
same lease on reconnect, so the address is not a reliable witness to the
adapter having gone down and up. The socket bound to it died either way.
The address is the cheap signal; the notification is the truthful one.
"""
from __future__ import annotations

import logging
import socket
import sys
import threading
import time

logger = logging.getLogger(__name__)

__all__ = ["local_ipv4s", "primary_ipv4", "fingerprint", "describe",
           "is_online", "NetworkWatcher", "os_notifications_available"]

# How long to wait after a notification before reading the network back.
# The OS reports the adapter transition; DHCP has not necessarily finished
# by then, and a fingerprint taken mid-negotiation is the old one plus a
# link-local placeholder. A quarter second misses that window and is short
# enough that nobody perceives it.
SETTLE_SECONDS = 0.25

# The floor between two callbacks. Bringing an adapter up emits a burst of
# notifications — one per address family, per stage — and without this the
# callback would fire five or six times for a single Wi-Fi hop, each one
# tearing down and rebuilding a Zeroconf instance.
MIN_CALLBACK_GAP = 1.0

# The safety-net poll. The notification path is the fast one; this covers
# what it cannot see: a platform without it, or a change that somehow
# produced no notification.
DEFAULT_POLL_SECONDS = 5.0


def primary_ipv4() -> str:
    """The address the OS would use to reach the outside world.

    A UDP socket that is connected but never sent on. It reads the LIVE
    routing table, which is the point: on Windows the hostname-based
    lookups are served from a cache that can lag a Wi-Fi hop badly, and a
    cached answer is worse than no answer here.
    """
    s = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
    try:
        s.connect(("10.255.255.255", 1))
        return s.getsockname()[0]
    except Exception:
        try:
            return socket.gethostbyname(socket.gethostname())
        except Exception:
            return "127.0.0.1"
    finally:
        s.close()


def _usable(ip: str) -> bool:
    """Whether an address means we are actually ON a network.

    169.254.x.x is what Windows assigns when DHCP fails. Counting it as an
    address makes a network that did not come up look like one that did,
    which is how a failed hop turns into a silent dead end instead of an
    offline badge.
    """
    return bool(ip) and not ip.startswith("127.") and not ip.startswith("169.254.")


def local_ipv4s() -> frozenset:
    """Every usable non-loopback IPv4 address the machine currently holds.

    The set, not the primary one. An adapter coming up or going down
    almost always changes the set even when the primary address is
    untouched, so the set sees transitions the primary misses.
    """
    ips = set()
    try:
        _, _, addrs = socket.gethostbyname_ex(socket.gethostname())
        for ip in addrs:
            if _usable(ip):
                ips.add(ip)
    except Exception:
        pass
    try:
        primary = primary_ipv4()
        if _usable(primary):
            ips.add(primary)
    except Exception:
        pass
    return frozenset(ips)


def fingerprint() -> tuple:
    """Which network we are on, as a comparable value.

    ``(primary, sorted-addresses)``. Compare two of these to decide
    whether everything learned from the network is still worth keeping.
    An empty primary and no addresses means offline.
    """
    ips = local_ipv4s()
    primary = primary_ipv4()
    if not _usable(primary):
        primary = ""
    return (primary, tuple(sorted(ips)))


def is_online(fp=None) -> bool:
    fp = fingerprint() if fp is None else fp
    return bool(fp[0]) or bool(fp[1])


def describe(fp: tuple) -> str:
    """A fingerprint in a form worth putting in a log line."""
    primary, ips = fp
    if not primary and not ips:
        return "offline"
    others = [i for i in ips if i != primary]
    head = primary or "no route"
    return head + (" (+%s)" % ", ".join(others) if others else "")


_notify_fn = None
_notify_looked_up = False


def _load_notify():
    """iphlpapi!NotifyAddrChange, or None where there is no such thing.

    Resolved once. ``NotifyAddrChange(NULL, NULL)`` is the synchronous
    form: it blocks until an address changes, then returns NO_ERROR. There
    is no way to cancel a blocked call, which is why the thread making it
    is a daemon and why the watcher checks a flag on the way out rather
    than trying to interrupt it.
    """
    global _notify_fn, _notify_looked_up
    if _notify_looked_up:
        return _notify_fn
    _notify_looked_up = True
    if not sys.platform.startswith("win"):
        return None
    try:
        import ctypes
        iph = ctypes.WinDLL("iphlpapi")
        fn = iph.NotifyAddrChange
        fn.argtypes = [ctypes.c_void_p, ctypes.c_void_p]
        fn.restype = ctypes.c_ulong
        _notify_fn = fn
    except Exception:
        logger.debug("NotifyAddrChange unavailable", exc_info=True)
        _notify_fn = None
    return _notify_fn


def os_notifications_available() -> bool:
    """Whether this platform can wake us instead of being polled."""
    return _load_notify() is not None


class NetworkWatcher:
    """Calls ``on_change(old_fp, new_fp, reason)`` when the network moves.

    Start it once; stop it on shutdown. The callback runs on a worker
    thread, so anything touching Qt widgets has to hop to the GUI thread
    itself — this class does not know what it is driving.

    ``reason`` is ``"os-event"`` when Windows told us, ``"poll"`` when the
    safety net found it, and ``"forced"`` when something else already knew.
    All three are real; the first is just faster.
    """

    def __init__(self, on_change, poll_seconds: float = DEFAULT_POLL_SECONDS,
                 settle_seconds: float = SETTLE_SECONDS,
                 min_gap: float = MIN_CALLBACK_GAP, clock=None, name: str = ""):
        self._on_change = on_change
        self._poll = float(poll_seconds)
        self._settle = float(settle_seconds)
        self._min_gap = float(min_gap)
        self._clock = clock or time.monotonic
        self._name = name or "LogicShare"

        self._stop = threading.Event()
        # Set by the notifier thread and by force_check(). The poll loop
        # waits on it, so a notification cuts the wait short instead of
        # racing alongside it.
        self._wake = threading.Event()
        self._threads: list = []
        self._fp = fingerprint()
        self._last_fire = 0.0
        self._lock = threading.Lock()

    @property
    def fingerprint(self) -> tuple:
        with self._lock:
            return self._fp

    @property
    def running(self) -> bool:
        return bool(self._threads) and not self._stop.is_set()

    def start(self) -> None:
        if self._threads:
            return
        self._stop.clear()
        loops = [("%s-NetPoll" % self._name, self._poll_loop)]
        if _load_notify() is not None:
            loops.append(("%s-NetNotify" % self._name, self._notify_loop))
        else:
            logger.info("No OS network notifications here — polling every %.1fs",
                        self._poll)
        for tname, target in loops:
            th = threading.Thread(target=target, daemon=True, name=tname)
            th.start()
            self._threads.append(th)

    def stop(self) -> None:
        self._stop.set()
        self._wake.set()
        self._threads = []

    def force_check(self) -> None:
        """Ask for a check now. For when something else already knows the
        network moved — a send that failed, a socket that died."""
        self._wake.set()

    # ---- the two loops -------------------------------------------------
    def _notify_loop(self) -> None:
        """Blocks in the OS until an address changes, forever.

        A returned call means something changed. It does not mean the
        fingerprint differs, and the callback fires either way — see the
        module docstring on why the address is not a trustworthy witness
        to an adapter having gone down and come back.
        """
        fn = _load_notify()
        while not self._stop.is_set():
            try:
                fn(None, None)          # blocks until the OS says so
            except Exception:
                logger.debug("NotifyAddrChange failed; leaving it to the poll",
                             exc_info=True)
                return
            if self._stop.is_set():
                return
            if self._stop.wait(self._settle):   # let DHCP finish
                return
            self._evaluate("os-event", always=True)

    def _poll_loop(self) -> None:
        while not self._stop.is_set():
            woken = self._wake.wait(self._poll)
            if self._stop.is_set():
                return
            if woken:
                self._wake.clear()
                if self._stop.wait(self._settle):
                    return
            self._evaluate("forced" if woken else "poll", always=bool(woken))

    def _evaluate(self, reason: str, always: bool = False) -> None:
        new = fingerprint()
        fire = False
        with self._lock:
            old = self._fp
            changed = (new != old)
            if changed or always:
                now = self._clock()
                if now - self._last_fire >= self._min_gap:
                    self._last_fire = now
                    fire = True
                # Either way keep the newest reading, so the next
                # comparison is against what is true now rather than
                # against a value a suppressed burst left behind.
                self._fp = new
        if not fire:
            return
        logger.info("Network %s: %s -> %s (%s)",
                    "changed" if changed else "event",
                    describe(old), describe(new), reason)
        try:
            self._on_change(old, new, reason)
        except Exception:
            logger.exception("network change handler failed")
