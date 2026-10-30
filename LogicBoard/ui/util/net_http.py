"""
net_http.py — HTTPS GETs that behave like the rest of Windows does.

Why not plain ``urllib``
------------------------
LogicBoard's update check worked on the development laptop and failed on a
classroom smart board with nothing more than "couldn't reach GitHub". Two
things a school machine routinely does differently, and ``urllib`` copes
with neither:

* **Root certificates arrive on demand.** Windows ships with a small root
  store and downloads the rest the first time its OWN chain engine meets
  one (Edge, Windows Update, WinINet). Python's ``ssl`` only ever reads the
  store as it stands. Since 30 Aug 2026 api.github.com chains to "Sectigo
  Public Server Authentication Root E46" — a 2021 root that older images do
  not carry — so on a board where nothing has fetched that root yet, Python
  fails certificate verification while Edge, on the same board, is fine.
* **Proxies come from a script.** Schools configure proxies through PAC /
  WPAD auto-configuration. ``urllib`` reads only a manually entered proxy,
  so it tries to connect directly and the school's network refuses.

WinINet is the stack behind the Windows "Internet Options" settings: it
builds certificate chains with CryptoAPI (fetching missing roots), follows
the user's proxy configuration including PAC/WPAD, and answers an
authenticating proxy with the signed-in user's credentials. So on Windows
requests go through WinINet, and ``urllib`` remains only as the fallback —
for other platforms, and for the (never yet seen) case where WinINet itself
cannot be used.

Failures say what went wrong
----------------------------
Every failure is raised as ``urllib.error.HTTPError`` (the server answered
with an error status) or :class:`NetError` (a ``urllib.error.URLError``), so
callers keep their existing ``except`` clauses. NetError's ``reason`` is a
sentence a teacher can act on — "this computer's date is wrong" — and its
``kind`` / ``detail`` are for the log.
"""
from __future__ import annotations

import datetime as _dt
import logging
import socket
import ssl
import sys
import urllib.error
import urllib.request
from typing import Dict, Optional

log = logging.getLogger(__name__)

# NetError.kind values.
KIND_OFFLINE = "offline"
KIND_DNS = "dns"
KIND_CONNECT = "connect"
KIND_TIMEOUT = "timeout"
KIND_CLOCK = "clock"
KIND_CERT = "certificate"
KIND_PROXY = "proxy"
KIND_TLS = "tls"
KIND_OTHER = "other"


class NetError(urllib.error.URLError):
    """The request never got an HTTP answer. ``reason`` is for people."""

    def __init__(self, kind: str, reason: str, detail: str = "") -> None:
        super().__init__(reason)
        self.kind = kind
        self.detail = detail or reason

    def __str__(self) -> str:                 # URLError's is "<urlopen error …>"
        return self.reason


def _clock_reason() -> str:
    today = _dt.datetime.now().strftime("%d %b %Y, %H:%M")
    return ("GitHub's security certificate looks expired or not yet valid, "
            "which almost always means this computer's date or time is "
            f"wrong (it says {today}). Set the correct date and time in "
            "Windows, then try again.")


_CERT_REASON = (
    "This computer doesn't trust GitHub's security certificate. On a "
    "school network this usually means the network inspects secure "
    "traffic — ask IT to allow github.com and *.githubusercontent.com.")

_PROXY_REASON = (
    "The network's proxy server refused the connection. Ask IT to allow "
    "github.com and *.githubusercontent.com for LogicBoard.")


# ═══════════════════════════════════════════════════════════════════════════
# Public API
# ═══════════════════════════════════════════════════════════════════════════

def open_url(url: str, headers: Optional[Dict[str, str]] = None,
             timeout: float = 20.0):
    """GET ``url``. Returns a response with ``status``, ``getheader()`` and
    ``read(n)``, usable as a context manager. Raises HTTPError / NetError."""
    headers = dict(headers or {})
    if sys.platform == "win32":
        try:
            return _wininet_open(url, headers, timeout)
        except urllib.error.HTTPError:
            raise                       # the server answered: that's final
        except NetError as e:
            first = e
        except Exception as e:          # noqa: BLE001 — WinINet unusable
            first = NetError(KIND_OTHER, "WinINet failed", repr(e))
        log.warning("WinINet request failed (%s: %s); trying urllib",
                    first.kind, first.detail)
        try:
            return _urllib_open(url, headers, timeout)
        except urllib.error.HTTPError:
            raise
        except NetError as second:
            log.warning("urllib request failed too (%s: %s)",
                        second.kind, second.detail)
            # WinINet's reading is the better-informed one (it knows the
            # proxy and the real chain), unless it had nothing specific.
            raise first if first.kind != KIND_OTHER else second
    return _urllib_open(url, headers, timeout)


# ═══════════════════════════════════════════════════════════════════════════
# WinINet
# ═══════════════════════════════════════════════════════════════════════════

_INTERNET_OPEN_TYPE_PRECONFIG = 0
_INTERNET_DEFAULT_HTTPS_PORT = 443
_INTERNET_SERVICE_HTTP = 3
_INTERNET_OPTION_CONNECT_TIMEOUT = 2
_INTERNET_OPTION_SEND_TIMEOUT = 5
_INTERNET_OPTION_RECEIVE_TIMEOUT = 6
_INTERNET_OPTION_SECURITY_FLAGS = 31
_SECURITY_FLAG_IGNORE_REVOCATION = 0x00000080

_FLAG_RELOAD = 0x80000000
_FLAG_SECURE = 0x00800000
_FLAG_NO_CACHE_WRITE = 0x04000000
_FLAG_PRAGMA_NOCACHE = 0x00000100
_FLAG_NO_COOKIES = 0x00080000
_FLAG_NO_UI = 0x00000200
_FLAG_KEEP_CONNECTION = 0x00400000

_HTTP_QUERY_STATUS_CODE = 19
_HTTP_QUERY_STATUS_TEXT = 20
_HTTP_QUERY_RAW_HEADERS_CRLF = 22
_HTTP_QUERY_FLAG_NUMBER = 0x20000000

_ERR_TIMEOUT = 12002
_ERR_NAME_NOT_RESOLVED = 12007
_ERR_CANNOT_CONNECT = 12029
_ERR_CONNECTION_ABORTED = 12030
_ERR_CONNECTION_RESET = 12031
_ERR_SEC_CERT_DATE_INVALID = 12037
_ERR_SEC_CERT_CN_INVALID = 12038
_ERR_INVALID_CA = 12045
_ERR_SEC_CERT_ERRORS = 12055
_ERR_SEC_CERT_REV_FAILED = 12057
_ERR_SEC_CERT_NO_REV = 12056
_ERR_SEC_CERT_REVOKED = 12170
_ERR_SECURITY_CHANNEL_ERROR = 12157
_ERR_DISCONNECTED = 12163
_ERR_PROXY_SERVER_UNREACHABLE = 12165
_ERR_BAD_AUTO_PROXY_SCRIPT = 12166
_ERR_UNABLE_TO_DOWNLOAD_SCRIPT = 12167


def _wininet_reason(code: int) -> NetError:
    """A WinINet error code, as something a teacher can act on."""
    if code == _ERR_DISCONNECTED:
        return NetError(KIND_OFFLINE, "This computer isn't connected to the "
                        "internet.", f"WinINet {code}")
    if code == _ERR_NAME_NOT_RESOLVED:
        return NetError(KIND_DNS, "Couldn't look up github.com — the "
                        "network's name server didn't answer, or the site "
                        "is blocked on this network.", f"WinINet {code}")
    if code in (_ERR_CANNOT_CONNECT, _ERR_CONNECTION_ABORTED,
                _ERR_CONNECTION_RESET):
        return NetError(KIND_CONNECT, "GitHub couldn't be reached — the "
                        "connection was refused or cut off. A firewall or "
                        "the school network may be blocking it.",
                        f"WinINet {code}")
    if code == _ERR_TIMEOUT:
        return NetError(KIND_TIMEOUT, "GitHub took too long to answer. The "
                        "connection may be slow — try again in a moment.",
                        f"WinINet {code}")
    if code == _ERR_SEC_CERT_DATE_INVALID:
        return NetError(KIND_CLOCK, _clock_reason(), f"WinINet {code}")
    if code in (_ERR_SEC_CERT_CN_INVALID, _ERR_INVALID_CA, _ERR_SEC_CERT_ERRORS,
                _ERR_SEC_CERT_REVOKED):
        return NetError(KIND_CERT, _CERT_REASON, f"WinINet {code}")
    if code == _ERR_SECURITY_CHANNEL_ERROR:
        return NetError(KIND_TLS, "A secure connection to GitHub couldn't be "
                        "set up. Windows may need updating.", f"WinINet {code}")
    if code in (_ERR_PROXY_SERVER_UNREACHABLE, _ERR_BAD_AUTO_PROXY_SCRIPT,
                _ERR_UNABLE_TO_DOWNLOAD_SCRIPT):
        return NetError(KIND_PROXY, "The network's proxy settings couldn't "
                        "be used — the proxy server or its setup script is "
                        "unreachable.", f"WinINet {code}")
    return NetError(KIND_OTHER, f"Couldn't reach GitHub (Windows network "
                    f"error {code}).", f"WinINet {code}")


_api = None


def _wininet():
    """The WinINet entry points, typed once. 64-bit handles must be
    declared as pointers — left to ctypes' int default they are truncated."""
    global _api
    if _api is not None:
        return _api
    import ctypes
    from ctypes import wintypes as w
    dll = ctypes.WinDLL("wininet", use_last_error=True)
    H = ctypes.c_void_p
    sig = {
        "InternetOpenW": (H, [w.LPCWSTR, w.DWORD, w.LPCWSTR, w.LPCWSTR, w.DWORD]),
        "InternetConnectW": (H, [H, w.LPCWSTR, w.WORD, w.LPCWSTR, w.LPCWSTR,
                                 w.DWORD, w.DWORD, ctypes.c_size_t]),
        "HttpOpenRequestW": (H, [H, w.LPCWSTR, w.LPCWSTR, w.LPCWSTR, w.LPCWSTR,
                                 ctypes.c_void_p, w.DWORD, ctypes.c_size_t]),
        "HttpSendRequestW": (w.BOOL, [H, w.LPCWSTR, w.DWORD, ctypes.c_void_p,
                                      w.DWORD]),
        "HttpQueryInfoW": (w.BOOL, [H, w.DWORD, ctypes.c_void_p,
                                    ctypes.POINTER(w.DWORD),
                                    ctypes.POINTER(w.DWORD)]),
        "InternetReadFile": (w.BOOL, [H, ctypes.c_void_p, w.DWORD,
                                      ctypes.POINTER(w.DWORD)]),
        "InternetSetOptionW": (w.BOOL, [H, w.DWORD, ctypes.c_void_p, w.DWORD]),
        "InternetQueryOptionW": (w.BOOL, [H, w.DWORD, ctypes.c_void_p,
                                          ctypes.POINTER(w.DWORD)]),
        "InternetCloseHandle": (w.BOOL, [H]),
    }
    for name, (res, args) in sig.items():
        fn = getattr(dll, name)
        fn.restype, fn.argtypes = res, args
    _api = dll
    return dll


class _WinInetResponse:
    """The slice of ``http.client.HTTPResponse`` the updater uses."""

    def __init__(self, handles, status: int, headers: Dict[str, str]):
        self._handles = handles            # request, connection, session
        self.status = status
        self._headers = headers

    def getheader(self, name: str, default=None):
        return self._headers.get(name.lower(), default)

    def read(self, n: int = -1) -> bytes:
        import ctypes
        from ctypes import wintypes as w
        api = _wininet()
        if n is None or n < 0:
            parts = []
            while True:
                chunk = self.read(256 * 1024)
                if not chunk:
                    return b"".join(parts)
                parts.append(chunk)
        buf = ctypes.create_string_buffer(n)
        got = w.DWORD(0)
        # InternetReadFile may return fewer bytes than asked; only 0 is EOF.
        if not api.InternetReadFile(self._handles[0], buf, n, ctypes.byref(got)):
            raise _wininet_reason(ctypes.get_last_error())
        return buf.raw[:got.value]

    def close(self) -> None:
        if self._handles:
            api = _wininet()
            for h in self._handles:
                if h:
                    api.InternetCloseHandle(h)
            self._handles = ()

    def __enter__(self):
        return self

    def __exit__(self, *exc):
        self.close()
        return False

    def __del__(self):
        try:
            self.close()
        except Exception:
            pass


def _wininet_open(url: str, headers: Dict[str, str], timeout: float):
    import ctypes
    from ctypes import wintypes as w
    from urllib.parse import urlsplit

    parts = urlsplit(url)
    if parts.scheme != "https" or not parts.hostname:
        raise NetError(KIND_OTHER, "Only https:// addresses are supported.",
                       url)
    api = _wininet()
    headers = dict(headers)              # the urllib fallback needs the UA
    agent = headers.pop("User-Agent", "LogicBoard")
    path = parts.path or "/"
    if parts.query:
        path += "?" + parts.query
    extra = "".join(f"{k}: {v}\r\n" for k, v in headers.items())

    session = api.InternetOpenW(agent, _INTERNET_OPEN_TYPE_PRECONFIG,
                                None, None, 0)
    if not session:
        raise _wininet_reason(ctypes.get_last_error())
    handles = [None, None, session]
    try:
        ms = w.DWORD(max(1000, int(timeout * 1000)))
        for opt in (_INTERNET_OPTION_CONNECT_TIMEOUT,
                    _INTERNET_OPTION_SEND_TIMEOUT,
                    _INTERNET_OPTION_RECEIVE_TIMEOUT):
            api.InternetSetOptionW(session, opt, ctypes.byref(ms),
                                   ctypes.sizeof(ms))
        conn = api.InternetConnectW(session, parts.hostname,
                                    parts.port or _INTERNET_DEFAULT_HTTPS_PORT,
                                    None, None, _INTERNET_SERVICE_HTTP, 0, 0)
        if not conn:
            raise _wininet_reason(ctypes.get_last_error())
        handles[1] = conn
        flags = (_FLAG_SECURE | _FLAG_RELOAD | _FLAG_NO_CACHE_WRITE
                 | _FLAG_PRAGMA_NOCACHE | _FLAG_NO_COOKIES | _FLAG_NO_UI
                 | _FLAG_KEEP_CONNECTION)
        req = api.HttpOpenRequestW(conn, "GET", path, None, None, None,
                                   flags, 0)
        if not req:
            raise _wininet_reason(ctypes.get_last_error())
        handles[0] = req

        ok = api.HttpSendRequestW(req, extra or None,
                                  len(extra) if extra else 0, None, 0)
        if not ok and ctypes.get_last_error() in (_ERR_SEC_CERT_REV_FAILED,
                                                  _ERR_SEC_CERT_NO_REV):
            # The chain is valid but its revocation list could not be
            # fetched — school proxies often block the CRL/OCSP hosts.
            # Browsers soft-fail here; so do we. A REVOKED certificate is a
            # different error (12170) and still refuses.
            log.info("Certificate revocation check unavailable; continuing "
                     "without it")
            sec = w.DWORD(0)
            size = w.DWORD(ctypes.sizeof(sec))
            api.InternetQueryOptionW(req, _INTERNET_OPTION_SECURITY_FLAGS,
                                     ctypes.byref(sec), ctypes.byref(size))
            sec.value |= _SECURITY_FLAG_IGNORE_REVOCATION
            api.InternetSetOptionW(req, _INTERNET_OPTION_SECURITY_FLAGS,
                                   ctypes.byref(sec), ctypes.sizeof(sec))
            ok = api.HttpSendRequestW(req, extra or None,
                                      len(extra) if extra else 0, None, 0)
        if not ok:
            raise _wininet_reason(ctypes.get_last_error())

        status = w.DWORD(0)
        size = w.DWORD(ctypes.sizeof(status))
        if not api.HttpQueryInfoW(req, _HTTP_QUERY_STATUS_CODE
                                  | _HTTP_QUERY_FLAG_NUMBER,
                                  ctypes.byref(status), ctypes.byref(size),
                                  None):
            raise _wininet_reason(ctypes.get_last_error())
        hdrs = _raw_headers(api, req)
        resp = _WinInetResponse(tuple(handles), int(status.value), hdrs)
        handles = []                     # the response owns them now
    finally:
        for h in handles:
            if h:
                api.InternetCloseHandle(h)

    if resp.status == 407:
        resp.close()
        raise NetError(KIND_PROXY, _PROXY_REASON, "HTTP 407")
    if resp.status >= 400:
        code = resp.status
        resp.close()
        raise urllib.error.HTTPError(url, code, f"HTTP {code}", None, None)
    return resp


def _raw_headers(api, req) -> Dict[str, str]:
    import ctypes
    from ctypes import wintypes as w
    size = w.DWORD(0)
    api.HttpQueryInfoW(req, _HTTP_QUERY_RAW_HEADERS_CRLF, None,
                       ctypes.byref(size), None)
    if not size.value:
        return {}
    buf = ctypes.create_unicode_buffer(size.value // 2 + 1)
    if not api.HttpQueryInfoW(req, _HTTP_QUERY_RAW_HEADERS_CRLF, buf,
                              ctypes.byref(size), None):
        return {}
    out: Dict[str, str] = {}
    for line in buf.value.split("\r\n")[1:]:          # [0] is the status line
        name, sep, value = line.partition(":")
        if sep:
            out[name.strip().lower()] = value.strip()
    return out


# ═══════════════════════════════════════════════════════════════════════════
# urllib — other platforms, and the fallback
# ═══════════════════════════════════════════════════════════════════════════

class _UrllibResponse:
    def __init__(self, resp):
        self._resp = resp
        self.status = resp.status

    def getheader(self, name, default=None):
        return self._resp.getheader(name, default)

    def read(self, n=-1):
        try:
            return self._resp.read(n)
        except (OSError, ssl.SSLError) as e:
            raise _urllib_reason(e) from e

    def close(self):
        self._resp.close()

    def __enter__(self):
        return self

    def __exit__(self, *exc):
        self.close()
        return False


def _urllib_reason(e: BaseException) -> NetError:
    inner = getattr(e, "reason", e)
    detail = f"{type(inner).__name__}: {inner}"
    if isinstance(inner, ssl.SSLCertVerificationError):
        # OpenSSL's X509_V_ERR_CERT_NOT_YET_VALID (9) / _HAS_EXPIRED (10).
        if getattr(inner, "verify_code", None) in (9, 10):
            return NetError(KIND_CLOCK, _clock_reason(), detail)
        return NetError(KIND_CERT, _CERT_REASON, detail)
    if isinstance(inner, ssl.SSLError):
        return NetError(KIND_TLS, "A secure connection to GitHub couldn't be "
                        "set up.", detail)
    if isinstance(inner, socket.gaierror):
        return NetError(KIND_DNS, "Couldn't look up github.com — check the "
                        "internet connection.", detail)
    if isinstance(inner, (socket.timeout, TimeoutError)):
        return NetError(KIND_TIMEOUT, "GitHub took too long to answer. Try "
                        "again in a moment.", detail)
    if isinstance(inner, (ConnectionError, OSError)):
        return NetError(KIND_CONNECT, "GitHub couldn't be reached — check the "
                        "internet connection.", detail)
    return NetError(KIND_OTHER, f"Couldn't reach GitHub ({inner}).", detail)


def _urllib_open(url: str, headers: Dict[str, str], timeout: float):
    req = urllib.request.Request(url, headers=headers)
    try:
        resp = urllib.request.urlopen(req, timeout=timeout)
    except urllib.error.HTTPError as e:
        if e.code == 407:
            raise NetError(KIND_PROXY, _PROXY_REASON, "HTTP 407") from e
        raise
    except (urllib.error.URLError, OSError, ssl.SSLError) as e:
        raise _urllib_reason(e) from e
    return _UrllibResponse(resp)
