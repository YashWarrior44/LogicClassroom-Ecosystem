"""
test_net_http.py — the update check reaches GitHub the way Windows does,
and when it cannot, it says why.

    python tests/python/test_net_http.py

A classroom board could not reach GitHub while the development laptop
could. api.github.com moved (30 Aug 2026) to a certificate chaining to
"Sectigo Public Server Authentication Root E46", which older Windows images
only acquire on demand through Windows' own chain engine — and Python's
``ssl`` never asks for it. Requests now go through WinINet on Windows.

Everything here is offline: the network paths are exercised by substituting
the two transports, never by calling them.
"""
from __future__ import annotations

import os
import socket
import ssl
import sys
import urllib.error
from pathlib import Path

os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")
sys.path.insert(0, str(Path(__file__).resolve().parents[2]))

from ui.util import net_http as n                          # noqa: E402
from ui.util import updater                                # noqa: E402


class _Swap:
    """Replace attributes for the duration of a with-block."""

    def __init__(self, obj, **attrs):
        self.obj, self.attrs, self.saved = obj, attrs, {}

    def __enter__(self):
        for k, v in self.attrs.items():
            self.saved[k] = getattr(self.obj, k)
            setattr(self.obj, k, v)
        return self

    def __exit__(self, *exc):
        for k, v in self.saved.items():
            setattr(self.obj, k, v)
        return False


def _raise(exc):
    def f(*a, **k):
        raise exc
    return f


# ── the transport choice ────────────────────────────────────────────────────

def test_windows_goes_through_wininet_first():
    if sys.platform != "win32":
        return
    seen = []
    with _Swap(n, _wininet_open=lambda *a: seen.append("wininet") or "W",
               _urllib_open=lambda *a: seen.append("urllib") or "U"):
        assert n.open_url("https://x.invalid/", {}) == "W"
    assert seen == ["wininet"], "urllib was used although WinINet worked"


def test_urllib_is_the_fallback_when_wininet_fails():
    if sys.platform != "win32":
        return
    with _Swap(n, _wininet_open=_raise(n.NetError(n.KIND_CONNECT, "no", "W")),
               _urllib_open=lambda *a: "U"):
        assert n.open_url("https://x.invalid/", {}) == "U"


def test_wininet_being_unusable_is_not_fatal():
    if sys.platform != "win32":
        return
    with _Swap(n, _wininet_open=_raise(OSError("wininet.dll missing")),
               _urllib_open=lambda *a: "U"):
        assert n.open_url("https://x.invalid/", {}) == "U"


def test_an_http_error_is_final_and_not_retried():
    """The server answered. Asking again by another route only doubles the
    rate-limit cost on a school's one shared IP."""
    if sys.platform != "win32":
        return
    seen = []
    err = urllib.error.HTTPError("u", 403, "rate", None, None)
    with _Swap(n, _wininet_open=_raise(err),
               _urllib_open=lambda *a: seen.append("urllib")):
        try:
            n.open_url("https://x.invalid/", {})
            raise AssertionError("no error raised")
        except urllib.error.HTTPError as e:
            assert e.code == 403
    assert seen == []


def test_when_both_fail_the_better_informed_reason_wins():
    if sys.platform != "win32":
        return
    proxy = n.NetError(n.KIND_PROXY, "proxy said no", "WinINet 12165")
    vague = n.NetError(n.KIND_CONNECT, "connect", "urllib")
    with _Swap(n, _wininet_open=_raise(proxy), _urllib_open=_raise(vague)):
        try:
            n.open_url("https://x.invalid/", {})
        except n.NetError as e:
            assert e is proxy, e
    other = n.NetError(n.KIND_OTHER, "?", "WinINet 99999")
    with _Swap(n, _wininet_open=_raise(other), _urllib_open=_raise(vague)):
        try:
            n.open_url("https://x.invalid/", {})
        except n.NetError as e:
            assert e is vague, "a WinINet shrug hid urllib's specific reason"


def test_the_user_agent_survives_into_the_fallback():
    """GitHub answers 403 to a request without one — and WinINet takes it
    out of the header dict to pass it separately."""
    if sys.platform != "win32":
        return
    got = {}

    def fake_wininet(url, headers, timeout):
        headers = dict(headers)
        headers.pop("User-Agent")
        raise n.NetError(n.KIND_CONNECT, "x")

    def fake_urllib(url, headers, timeout):
        got.update(headers)
        return "U"

    with _Swap(n, _wininet_open=fake_wininet, _urllib_open=fake_urllib):
        n.open_url("https://x.invalid/", {"User-Agent": "LB/1"})
    assert got.get("User-Agent") == "LB/1"


# ── reasons a teacher can act on ────────────────────────────────────────────

def test_wininet_errors_become_specific_reasons():
    cases = {12037: n.KIND_CLOCK, 12045: n.KIND_CERT, 12038: n.KIND_CERT,
             12007: n.KIND_DNS, 12029: n.KIND_CONNECT, 12002: n.KIND_TIMEOUT,
             12163: n.KIND_OFFLINE, 12166: n.KIND_PROXY, 12157: n.KIND_TLS,
             12999: n.KIND_OTHER}
    for code, kind in cases.items():
        e = n._wininet_reason(code)
        assert isinstance(e, urllib.error.URLError), "callers catch URLError"
        assert e.kind == kind, (code, e.kind)
        assert str(code) in e.detail


def test_a_wrong_clock_is_named_as_the_problem():
    e = n._wininet_reason(12037)
    assert "date" in e.reason.lower() and "time" in e.reason.lower()
    expired = ssl.SSLCertVerificationError("certificate has expired")
    expired.verify_code = 10
    assert n._urllib_reason(urllib.error.URLError(expired)).kind == n.KIND_CLOCK


def test_urllib_errors_become_specific_reasons():
    untrusted = ssl.SSLCertVerificationError("unable to get issuer certificate")
    untrusted.verify_code = 2
    cases = [
        (untrusted, n.KIND_CERT),
        (socket.gaierror(11001, "getaddrinfo failed"), n.KIND_DNS),
        (TimeoutError("timed out"), n.KIND_TIMEOUT),
        (ConnectionRefusedError(10061, "refused"), n.KIND_CONNECT),
    ]
    for inner, kind in cases:
        got = n._urllib_reason(urllib.error.URLError(inner))
        assert got.kind == kind, (inner, got.kind)


def test_a_proxy_demanding_a_login_is_named():
    err = urllib.error.HTTPError("u", 407, "Proxy Authentication Required",
                                 None, None)
    with _Swap(n.urllib.request, urlopen=_raise(err)):
        try:
            n._urllib_open("https://x.invalid/", {}, 5)
            raise AssertionError("no error")
        except n.NetError as e:
            assert e.kind == n.KIND_PROXY


# ── the updater passes the reason on, and logs it ───────────────────────────

def _check_message(exc):
    out = []
    w = updater.CheckWorker()
    w.finished.connect(lambda info, msg: out.append((info, msg)))
    with _Swap(updater, _open=_raise(exc), is_configured=lambda: True):
        w.run()
    return out[0]


def test_the_update_check_says_why_github_is_unreachable():
    """It used to say "Couldn't reach GitHub — check your network" for every
    cause, which on a board whose network was fine sent everyone looking in
    the wrong place."""
    e = n._wininet_reason(12045)
    info, msg = _check_message(e)
    assert info is None and msg == e.reason, msg


def test_the_reason_reaches_the_log():
    import logging
    records = []

    class H(logging.Handler):
        def emit(self, r):
            records.append(r.getMessage())

    h = H()
    updater.log.addHandler(h)
    try:
        _check_message(n._wininet_reason(12037))
    finally:
        updater.log.removeHandler(h)
    assert any("12037" in r for r in records), records


def test_a_plain_urlerror_still_gets_the_generic_line():
    info, msg = _check_message(urllib.error.URLError("whatever"))
    assert "Couldn't reach GitHub" in msg


def test_the_download_says_why_too():
    out = []
    info = updater.UpdateInfo(
        latest_version="v9.9.9", latest_semver=updater.SemVer.parse("9.9.9"),
        installer_url="https://x.invalid/i.exe", asset_name="i.exe",
        asset_size=5_000_000, release_notes="", release_url="")
    w = updater.DownloadWorker(info)
    w.finished.connect(lambda path, msg: out.append((path, msg)))
    e = n._wininet_reason(12007)
    with _Swap(updater, _open=_raise(e), existing_download=lambda i: None,
               _prune_old_downloads=lambda keep=None: None):
        w.run()
    path, msg = out[0]
    assert path == "" and e.reason in msg, msg


def test_nothing_in_the_updater_opens_a_connection_itself():
    """One transport, so the next fix to it reaches every request."""
    src = Path(updater.__file__).read_text(encoding="utf-8")
    assert "urlopen(" not in src, "the updater bypasses net_http again"


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
    sys.stdout.flush()
    os._exit(1 if failures else 0)
