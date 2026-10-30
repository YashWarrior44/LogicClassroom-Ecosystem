"""
single_instance.py — one LogicShare per teacher, and launching it again
brings that one back.

The problem
-----------
Closing the LogicShare window does not end the process. It hides into the
tray so the acknowledgment listener keeps running — a warning about an
unanswered announcement fires five minutes after it went out, long after
the teacher has closed the window. That part is deliberate.

What was not deliberate: launching LogicShare again from the Start menu or
the desktop started a SECOND, fully independent process. Now there were two
tray icons, two ack listeners fighting over one port, two Zeroconf browsers,
two update checks — and the one the teacher was looking at was not the one
holding their pending acknowledgments.

What this does
--------------
The first process to start claims the instance and opens a local pipe.
Every later launch finds the claim, tells the first process to show its
window, and exits before it has built any UI at all.

Two mechanisms, because each is good at one half:

  WHO IS FIRST — a named kernel mutex. Its existence is the claim. Windows
    destroys it when the last handle closes, including when the owner
    crashes or is killed by the installer, so there is no such thing as a
    stale lock to clean up. A file lock would need PID-liveness guessing;
    this needs nothing. It also settles a double-click race: two launches
    in the same millisecond both try to create the mutex and the kernel
    lets exactly one of them be the creator.

  HOW TO TALK — a QLocalServer, which on Windows is a named pipe. The
    second launch connects, writes ``show``, waits for ``ok``, and leaves.

The mutex is named in the ``Local\\`` namespace, which is per logon session:
two teachers signed in to the same board through fast user switching each
get their own LogicShare, which is right — the ack listener and the tray
icon both belong to a session. The pipe name carries the session id for the
same reason, because pipe names are machine-wide.

Focus
-----
Windows refuses to let a background process pull its window to the front;
it flashes the taskbar button instead. The process that IS allowed to take
the foreground is the one the user just launched — the second instance. So
before it signals, the second instance calls ``AllowSetForegroundWindow``
and hands that right to the first. Without this the window "comes back"
behind whatever the teacher was looking at, which reads as nothing having
happened at all.
"""
from __future__ import annotations

import ctypes
import getpass
import logging
import os
import re
import sys
import time

log = logging.getLogger("LogicShare.single_instance")

APP_ID = "LogicShare"

# How long a second launch keeps trying to reach the first. The first may
# hold the claim but not be listening yet — it claims before it builds any
# UI, so there is a short window during startup where the mutex exists and
# the pipe does not. Measured startup to listening is well under a second;
# this is generous so a slow board does not spawn a duplicate.
CONNECT_DEADLINE_S = 6.0
CONNECT_RETRY_S = 0.1
REPLY_TIMEOUT_MS = 2000

MSG_SHOW = b"show"
MSG_OK = b"ok"

_ERROR_ALREADY_EXISTS = 183
_ASFW_ANY = -1


def _safe(text: str) -> str:
    """A string usable inside a mutex or pipe name."""
    return re.sub(r"[^A-Za-z0-9_.-]", "_", text or "user")[:64]


def _session_id() -> int:
    if not sys.platform.startswith("win"):
        return 0
    try:
        sid = ctypes.c_ulong(0)
        if ctypes.windll.kernel32.ProcessIdToSessionId(
                os.getpid(), ctypes.byref(sid)):
            return int(sid.value)
    except Exception:
        pass
    return 0


def _user() -> str:
    try:
        return getpass.getuser()
    except Exception:
        return os.environ.get("USERNAME", "user")


def instance_names(app_id: str = APP_ID) -> tuple:
    """(mutex name, pipe name) for this user in this session."""
    user = _safe(_user())
    sess = _session_id()
    mutex = "Local\\%s.SingleInstance.%s" % (_safe(app_id), user)
    pipe = "%s-%s-s%d" % (_safe(app_id), user, sess)
    return mutex, pipe


class _Claim:
    """Ownership of the instance. Held for the life of the process."""

    def __init__(self, name: str):
        self.name = name
        self.handle = None
        self.lockfile = None

    def acquire(self) -> bool:
        if sys.platform.startswith("win"):
            # use_last_error + ctypes.get_last_error(), the form the ctypes
            # docs require, rather than a second foreign call to
            # GetLastError. The interpreter is free to make its own Win32
            # calls between two ctypes calls, and if one of them reset the
            # thread's last-error, ERROR_ALREADY_EXISTS would read as 0 and
            # both racers would believe they created the mutex. That was
            # suspected and NOT observed — 30 simultaneous-launch rounds with
            # the plain GetLastError read produced one primary every time —
            # but the whole design rests on this one value, so it is read
            # the way that is guaranteed rather than the way that happens to
            # work today.
            k32 = ctypes.WinDLL("kernel32", use_last_error=True)
            k32.CreateMutexW.restype = ctypes.c_void_p
            k32.CreateMutexW.argtypes = [ctypes.c_void_p, ctypes.c_bool,
                                         ctypes.c_wchar_p]
            k32.CloseHandle.argtypes = [ctypes.c_void_p]
            ctypes.set_last_error(0)
            h = k32.CreateMutexW(None, False, self.name)
            err = ctypes.get_last_error()
            if not h:
                # Could not even create it — permissions, a name clash with
                # a non-mutex object. Being unable to prove another instance
                # exists is not proof that one does; run normally.
                log.warning("Single-instance mutex unavailable (error %d) — "
                            "continuing without it", err)
                return True
            if err == _ERROR_ALREADY_EXISTS:
                k32.CloseHandle(h)
                return False
            self.handle = h          # kept open: its existence IS the claim
            self._k32 = k32
            return True
        # Elsewhere (development on another OS): a QLockFile, which handles
        # stale locks by checking whether the recorded PID is alive.
        from PySide6.QtCore import QLockFile, QDir
        path = os.path.join(QDir.tempPath(), _safe(self.name) + ".lock")
        lf = QLockFile(path)
        lf.setStaleLockTime(0)
        if lf.tryLock(0):
            self.lockfile = lf
            return True
        return False

    def release(self) -> None:
        if self.handle:
            try:
                self._k32.CloseHandle(self.handle)
            except Exception:
                pass
            self.handle = None
        if self.lockfile is not None:
            try:
                self.lockfile.unlock()
            except Exception:
                pass
            self.lockfile = None


def _allow_foreground() -> None:
    """Hand our right to take the foreground to whichever process asks next.

    Only meaningful from the process the user just launched — that one holds
    the right. ASFW_ANY rather than the first instance's PID because the pipe
    never tells us the PID, and this call is scoped to the next
    SetForegroundWindow anyway.
    """
    if not sys.platform.startswith("win"):
        return
    try:
        ctypes.windll.user32.AllowSetForegroundWindow(_ASFW_ANY)
    except Exception:
        pass


def signal_existing(pipe: str, deadline_s: float = CONNECT_DEADLINE_S) -> bool:
    """Ask the running instance to show itself. True once it confirmed.

    Retries the connection because the first instance claims the mutex before
    it opens the pipe — see CONNECT_DEADLINE_S.
    """
    from PySide6.QtNetwork import QLocalSocket

    _allow_foreground()
    end = time.monotonic() + deadline_s
    attempt = 0
    while True:
        attempt += 1
        sock = QLocalSocket()
        sock.connectToServer(pipe)
        if sock.waitForConnected(500):
            try:
                sock.write(MSG_SHOW + b"\n")
                sock.flush()
                sock.waitForBytesWritten(1000)
                reply = b""
                t_end = time.monotonic() + REPLY_TIMEOUT_MS / 1000.0
                while MSG_OK not in reply and time.monotonic() < t_end:
                    if sock.waitForReadyRead(100):
                        reply += bytes(sock.readAll())
                    elif sock.state() != QLocalSocket.LocalSocketState.ConnectedState:
                        break
                if MSG_OK in reply:
                    log.info("Existing LogicShare told to show itself "
                             "(attempt %d)", attempt)
                    return True
                log.warning("Existing LogicShare accepted the connection "
                            "but did not confirm")
            finally:
                sock.disconnectFromServer()
        if time.monotonic() >= end:
            return False
        time.sleep(CONNECT_RETRY_S)


class InstanceServer:
    """The first instance's end of the pipe.

    ``on_show`` runs on the GUI thread (QLocalServer delivers signals on the
    thread that owns it, which is the main one). It is settable after
    construction because the server has to exist before the main window
    does — a launch during the first-run profile dialog must still find
    someone listening.
    """

    def __init__(self, pipe: str, on_show=None):
        from PySide6.QtNetwork import QLocalServer
        self.pipe = pipe
        self.on_show = on_show
        self.requests = 0
        self._server = QLocalServer()
        # Restrict the pipe to this user. On Windows the default ACL would
        # let any account on the machine connect — harmless here (the only
        # verb is "show"), but there is no reason to offer it.
        try:
            self._server.setSocketOptions(
                QLocalServer.SocketOption.UserAccessOption)
        except Exception:
            pass
        # No-op on Windows; on other platforms clears a socket file left by a
        # crashed run, which would otherwise make listen() fail forever.
        QLocalServer.removeServer(pipe)
        self.listening = self._server.listen(pipe)
        if not self.listening:
            log.warning("Single-instance pipe could not listen: %s",
                        self._server.errorString())
        self._server.newConnection.connect(self._accept)
        self._clients = []

    def _accept(self):
        while self._server.hasPendingConnections():
            sock = self._server.nextPendingConnection()
            if sock is None:
                break
            buf = {"data": b""}
            self._clients.append(sock)

            def read(sock=sock, buf=buf):
                buf["data"] += bytes(sock.readAll())
                if b"\n" not in buf["data"]:
                    return
                line = buf["data"].split(b"\n", 1)[0].strip()
                if line == MSG_SHOW:
                    self.requests += 1
                    log.info("Another launch asked LogicShare to show itself")
                    try:
                        if self.on_show is not None:
                            self.on_show()
                        else:
                            bring_forward_any()
                    except Exception:
                        log.exception("Show request failed")
                    sock.write(MSG_OK + b"\n")
                    sock.flush()

            def gone(sock=sock):
                if sock in self._clients:
                    self._clients.remove(sock)
                # The socket is a child of the server. When the server closes
                # it deletes its children, and ``disconnected`` fires during
                # that teardown — at which point the C++ object is already
                # gone and deleteLater raises. Nothing left to clean up then.
                try:
                    sock.deleteLater()
                except RuntimeError:
                    pass

            sock.readyRead.connect(read)
            sock.disconnected.connect(gone)
            if sock.bytesAvailable():
                read()

    def close(self):
        try:
            self._server.close()
        except Exception:
            pass


def bring_forward_any() -> None:
    """Before the main window exists — first run, the profile dialog is up —
    a second launch should still surface what IS on screen."""
    from PySide6.QtWidgets import QApplication
    w = QApplication.activeModalWidget()
    if w is None:
        tops = [t for t in QApplication.topLevelWidgets() if t.isVisible()]
        w = tops[0] if tops else None
    if w is not None:
        bring_forward(w)


def bring_forward(widget) -> None:
    """Show, un-minimise, raise and focus ``widget``.

    ``showNormal`` because a window hidden while minimised comes back
    minimised, which looks exactly like nothing happening. The Win32 calls
    after Qt's own because ``activateWindow`` alone is subject to the
    foreground lock — the second instance has granted us the right, but Qt
    does not always exercise it.
    """
    if widget.isMinimized() or not widget.isVisible():
        widget.showNormal()
    else:
        widget.show()
    widget.raise_()
    widget.activateWindow()
    if sys.platform.startswith("win"):
        try:
            hwnd = int(widget.winId())
            u32 = ctypes.windll.user32
            u32.ShowWindow(hwnd, 9)          # SW_RESTORE
            u32.SetForegroundWindow(hwnd)
        except Exception:
            pass


class SingleInstance:
    """Decide, at startup, whether this process should run at all.

        guard = SingleInstance()
        if not guard.is_primary:
            guard.hand_off()          # tells the running one to show
            sys.exit(0)
        ...
        guard.set_show_handler(window._restore_from_tray)

    Construct it AFTER QApplication (the pipe needs Qt) and BEFORE any
    window, profile dialog or network work — the point is that a second
    launch costs nothing.
    """

    def __init__(self, app_id: str = APP_ID):
        self.mutex_name, self.pipe_name = instance_names(app_id)
        self._claim = _Claim(self.mutex_name)
        self.is_primary = self._claim.acquire()
        self.server = None
        if self.is_primary:
            self.server = InstanceServer(self.pipe_name)
            log.info("Single-instance claim held (%s)", self.pipe_name)
        else:
            log.info("LogicShare is already running for this user — "
                     "handing off")

    def set_show_handler(self, fn) -> None:
        if self.server is not None:
            self.server.on_show = fn

    def hand_off(self) -> bool:
        """Second instance only: wake the first. Returns whether it answered."""
        return signal_existing(self.pipe_name)

    def release(self) -> None:
        if self.server is not None:
            self.server.close()
        self._claim.release()
