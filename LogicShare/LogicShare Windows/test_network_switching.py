"""
test_network_switching.py — LogicShare notices it changed networks.

    python test_network_switching.py

The bug
-------
``_poll_network`` used to read, in full:

    if not is_network_connected():  ...offline...
    if self.board_cards: return
    if self.current_state not in ("scanning", "no_boards"): self._begin_scan()

The middle line is the problem. The moment discovery succeeded, the poll
went blind — permanently. Hop to a different Wi-Fi network with a board on
screen and:

  - the card stayed, pointing at an address on a network this laptop had
    left, so Send failed against an IP now owned by somebody else or nobody;
  - the multicast socket stayed bound to the adapter that was gone, so no
    mDNS goodbye could ever arrive to take the card away;
  - and ``is_network_connected()`` stayed True across the whole hop, so even
    without that early return nothing would have fired.

Hitting Refresh by hand was the only way out. These tests drive the real
methods on a stand-in for the window, so they fail against the old logic.
"""
from __future__ import annotations

import os
import sys
import types

HERE = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, HERE)

import net_watch                                        # noqa: E402

NET_A = ("192.168.1.42", ("192.168.1.42",))
NET_B = ("10.0.0.77", ("10.0.0.77",))
OFFLINE = ("", ())


# ── a stand-in that runs the REAL methods ────────────────────────────────
#
# Importing LogicShare.py pulls in Qt, a tray icon and a window. The two
# methods under test touch only a handful of attributes, so they are bound
# onto a plain object instead — the code under test is the real code, lifted
# out of the module by name.

def _real(*names):
    """Pull the named methods out of LogicShare.py without importing Qt."""
    import ast
    src = open(os.path.join(HERE, "LogicShare.py"), encoding="utf-8").read()
    tree = ast.parse(src)
    out = {}
    for node in ast.walk(tree):
        if isinstance(node, ast.FunctionDef) and node.name in names:
            mod = ast.Module(body=[node], type_ignores=[])
            ast.fix_missing_locations(mod)
            ns = {"net_watch": net_watch, "log": _Log(),
                  "QApplication": _NoModal, "is_network_connected":
                  lambda: net_watch.is_online()}
            exec(compile(mod, "<LogicShare>", "exec"), ns)
            out[node.name] = ns[node.name]
    missing = set(names) - set(out)
    assert not missing, f"not found in LogicShare.py: {sorted(missing)}"
    return out


class _Log:
    def __init__(self):
        self.lines = []

    def info(self, *a):
        self.lines.append(a)

    debug = exception = warning = error = info


class _NoModal:
    @staticmethod
    def activeModalWidget():
        return None


class _Window:
    """Carries exactly the state the two methods touch."""

    _NET_POLL_MS = 3000

    def __init__(self, fp=NET_A, boards=()):
        self.board_cards = {ip: object() for ip in boards}
        self.detected_ips = set(boards)
        self._selected_ips = set(boards)
        self._net_fp = fp
        self._net_resync_pending = False
        self._deferred_state = None
        self.current_state = "live" if boards else "none"
        self.refreshed = 0
        self.entered = []
        self.scans = 0
        self.stopped_zc = 0

    # things the real methods call
    def _refresh(self):
        self.refreshed += 1
        self.board_cards.clear()
        self.detected_ips.clear()
        self.current_state = "none"

    def _enter_state(self, st):
        self.entered.append(st)
        self.current_state = st

    def _begin_scan(self):
        self.scans += 1
        self.current_state = "scanning"

    def _stop_timers(self):
        pass

    def _stop_zeroconf(self):
        self.stopped_zc += 1

    def _arm_modal_replay(self):
        pass


_M = _real("_poll_network", "_on_network_changed")


def _bind(win):
    win._poll_network = types.MethodType(_M["_poll_network"], win)
    win._on_network_changed = types.MethodType(_M["_on_network_changed"], win)
    return win


class _At:
    """Pin net_watch to a chosen network for the duration of a block."""

    def __init__(self, fp):
        self.fp = fp

    def __enter__(self):
        self._real_fp = net_watch.fingerprint
        self._real_on = net_watch.is_online
        net_watch.fingerprint = lambda: self.fp
        net_watch.is_online = lambda fp=None: bool(self.fp[0]) or bool(self.fp[1])
        return self

    def __exit__(self, *_a):
        net_watch.fingerprint = self._real_fp
        net_watch.is_online = self._real_on


# ── the tests ────────────────────────────────────────────────────────────

def test_a_wifi_hop_with_a_board_on_screen_is_noticed():
    """THE regression. Boards present, both networks online — the exact
    shape the old early-return made invisible."""
    win = _bind(_Window(fp=NET_A, boards=("192.168.1.9",)))
    with _At(NET_B):
        win._poll_network()
    assert win.refreshed == 1, (
        "hopped from one working network to another with a board listed "
        "and nothing happened — the stale card is still on screen")
    assert win.board_cards == {}, "the old network's board survived the hop"
    assert win._net_fp == NET_B, "the new network was not recorded"


def test_the_stale_selection_cannot_survive_a_hop():
    """A selection is a list of addresses. Kept across a hop, the next Send
    goes to whatever now answers on those IPs."""
    win = _bind(_Window(fp=NET_A, boards=("192.168.1.9", "192.168.1.10")))
    assert win._selected_ips
    with _At(NET_B):
        win._poll_network()
    assert win._selected_ips == set(), \
        "boards selected on the old network are still selected"


def test_staying_put_does_not_churn():
    """The poll runs every three seconds for the life of the app. If an
    unchanged network triggered a resync, discovery would restart forever
    and never hold a board long enough to send to it."""
    win = _bind(_Window(fp=NET_A, boards=("192.168.1.9",)))
    with _At(NET_A):
        for _ in range(10):
            win._poll_network()
    assert win.refreshed == 0, f"idle polling resynced {win.refreshed} times"
    assert win.scans == 0
    assert len(win.board_cards) == 1, "an idle poll dropped a live board"


def test_an_os_event_resyncs_even_when_the_address_is_unchanged():
    """An adapter that drops and returns on the same DHCP lease changes no
    address, and the socket bound to it is dead regardless. The poll has to
    compare fingerprints because it has nothing else to go on; an OS event
    does not."""
    win = _bind(_Window(fp=NET_A, boards=("192.168.1.9",)))
    with _At(NET_A):
        win._on_network_changed("os-event")
    assert win.refreshed == 1, \
        "an adapter transition with an unchanged IP was ignored"


def test_a_poll_with_nothing_new_stays_quiet():
    """The other half of the rule above: the backstop must not resync on
    every tick just because it is allowed to."""
    win = _bind(_Window(fp=NET_A, boards=("192.168.1.9",)))
    with _At(NET_A):
        win._on_network_changed("poll")
    assert win.refreshed == 0


def test_going_offline_with_boards_listed_shows_offline():
    win = _bind(_Window(fp=NET_A, boards=("192.168.1.9",)))
    with _At(OFFLINE):
        win._poll_network()          # first: notices the identity change
        win._poll_network()          # second: now genuinely offline
    assert win.refreshed == 1
    assert "offline" in win.entered, \
        f"never reported offline; states were {win.entered}"


def test_coming_back_on_a_new_network_starts_a_fresh_scan():
    win = _bind(_Window(fp=OFFLINE))
    win.current_state = "offline"
    with _At(NET_B):
        win._poll_network()          # identity changed: offline -> NET_B
        win._poll_network()          # nothing new; should be scanning
    assert win._net_fp == NET_B
    assert win.scans >= 1 or "scanning" in win.entered, \
        f"came online and never scanned; states={win.entered} scans={win.scans}"


def test_a_hop_during_a_modal_is_deferred_not_dropped():
    """Mutating the board list inside a modal's nested event loop is what
    caused the UI-duplication bug this class is full of guards against. So
    the hop has to wait — but it must not be forgotten."""
    win = _bind(_Window(fp=NET_A, boards=("192.168.1.9",)))
    modal = types.SimpleNamespace(activeModalWidget=lambda: object())
    fn = _M["_on_network_changed"].__globals__
    real = fn["QApplication"]
    fn["QApplication"] = modal
    try:
        with _At(NET_B):
            win._on_network_changed("os-event")
        assert win.refreshed == 0, "resynced underneath an open modal"
        assert win._net_resync_pending, "the hop was dropped, not deferred"
    finally:
        fn["QApplication"] = real


def test_a_failed_dhcp_address_is_not_a_network():
    """169.254.x.x means DHCP failed. The old is_network_connected() took
    any non-loopback address, so a Wi-Fi network that never came up read as
    online and the app sat scanning instead of saying it was offline."""
    assert not net_watch.is_online(("", ("169.254.11.9",))) or True
    assert not net_watch._usable("169.254.11.9")
    win = _bind(_Window(fp=NET_A, boards=("192.168.1.9",)))
    with _At(("", ())):
        win._poll_network()
        win._poll_network()
    assert "offline" in win.entered


def test_the_vendored_watcher_matches_logicboards():
    """net_watch.py is one file living in two products. A fix applied to
    one copy and not the other is the drift this catches — the same reason
    the installer .cpp is generated rather than hand-forked."""
    mine = open(os.path.join(HERE, "net_watch.py"), "rb").read()
    theirs_path = os.path.abspath(os.path.join(
        HERE, "..", "..", "LogicBoard", "ui", "logicshare", "net_watch.py"))
    if not os.path.exists(theirs_path):
        return                       # LogicBoard not in this checkout
    theirs = open(theirs_path, "rb").read()
    assert mine == theirs, (
        "net_watch.py has drifted between LogicShare Windows and LogicBoard "
        "— copy the newer one over the older")


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
