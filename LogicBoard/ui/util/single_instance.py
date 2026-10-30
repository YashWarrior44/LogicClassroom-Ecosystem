"""
single_instance.py — local-socket IPC for the LogicBoard process.

Replaces the old "is_already_running → show error dialog" model with a
proper cross-process message channel:

  * The first ``LogicBoard.exe`` to start opens a ``QLocalServer`` on a
    well-known pipe name and becomes the running instance.
  * Every subsequent launch is a CLIENT: it opens a ``QLocalSocket`` to
    that pipe, sends a one-line command, then exits cleanly.

Wire format
-----------
Two coexisting formats — new callers write JSON, legacy callers still
work:

  * LEGACY (backward-compatible): a single lowercase word,
    ``"show\\n"`` / ``"quit\\n"`` / ``"ping\\n"``. Older LogicBoard
    versions that haven't been rebuilt against this module send this,
    and this server still understands it.

  * NEW (version-aware): a single line of JSON, e.g.
    ``{"command":"show","version":"3.1.0"}\\n``. Every new client
    writes this so the running server can decide whether to hand off
    control to a newer version.

Version negotiation
-------------------
When a JSON message arrives with a ``version`` field:

  * If the received version is EQUAL to our own → treat as a normal
    ``show`` / ``quit`` / ``ping`` (existing behaviour).
  * If the received version is NEWER than ours → the launched process
    is a fresh update. WE are the outdated one. We ACK, then emit
    ``supersede_requested`` so ``ApplicationCore`` can shut down
    cleanly. The new process, seeing our ACK + our disappearance, will
    become the primary server on its next connect attempt.
  * If the received version is OLDER than ours → the second launch is
    a stale binary. We ACK with a message hint, then normal ``show``
    dispatch happens on our (newer) UI. The stale client exits after
    its ACK.

The stale-pipe cleanup, ACK protocol, and timeouts are unchanged from
the pre-versioning implementation.
"""
from __future__ import annotations

import json
import logging
import sys
from typing import Optional

from PySide6.QtCore import QObject, Signal, QByteArray, QTimer
from PySide6.QtNetwork import QLocalServer, QLocalSocket

from ui.util.version import APP_VERSION, APP_SEMVER, SemVer

__all__ = ["SERVER_NAME", "InstanceServer", "send_to_running_instance",
           "peer_is_listening"]

log = logging.getLogger(__name__)

# Pipe name. On Windows this becomes ``\\.\pipe\<SERVER_NAME>``; on
# Linux/Mac it lives under ``$XDG_RUNTIME_DIR`` / ``/tmp``.
#
# Deliberately includes only the MAJOR version (e.g. "v3"), not the full
# APP_VERSION — this is a wire-protocol marker, not a release tag. Every
# 3.x.y build must find every other 3.x.y build so the version-negotiation
# handshake (see ``InstanceServer._on_new_connection``) can work at all;
# tying the pipe name to the exact patch version would mean 3.0.0 and
# 3.0.1 could never even connect to compare versions. The major number IS
# allowed to change the pipe name — if a future v4 ever changes the wire
# format incompatibly, giving it a fresh pipe automatically (no manual
# edit needed here) is exactly the right behaviour: old and new simply
# don't try to talk to each other.
_major = APP_SEMVER.major if APP_SEMVER is not None else 3
SERVER_NAME = f"LogicBoard_v{_major}_IPC"

# Socket I/O timeouts in milliseconds. Generous because the running
# instance only ACKs after handling the command, which may include
# showing the UI (window construction can take ~1 sec on a cold cache).
_CONNECT_TIMEOUT_MS = 1500
_WRITE_TIMEOUT_MS   = 1500
_READ_TIMEOUT_MS    = 3000


def _encode(command: str, version: Optional[str],
            path: Optional[str] = None) -> bytes:
    """Build the wire payload. New callers pass their version → JSON;
    legacy behaviour (no version) → bare command word for backward
    compatibility with servers built before this file.

    ``path`` rides along with "open". json.dumps escapes every non-ASCII
    character, so a path with an emoji or a Hindi folder name in it still
    travels as the pure ASCII the reader decodes."""
    if version is None and path is None:
        return (command + "\n").encode("ascii")
    obj = {"command": command, "version": version}
    if path is not None:
        obj["path"] = path
    payload = json.dumps(obj, separators=(",", ":"))
    return (payload + "\n").encode("ascii")


def _decode_path(raw: str) -> Optional[str]:
    """The file an "open" message carries, or None."""
    raw = raw.strip()
    if not raw.startswith("{"):
        return None
    try:
        p = json.loads(raw).get("path")
    except (json.JSONDecodeError, AttributeError):
        return None
    return str(p) if p else None


def _decode(raw: str) -> tuple[str, Optional[str]]:
    """Return ``(command, version_or_None)`` from a wire line.
    Falls back to legacy interpretation on anything that doesn't parse
    as JSON — so an old client speaking bare "show" is understood."""
    raw = raw.strip()
    if raw.startswith("{"):
        try:
            obj = json.loads(raw)
        except json.JSONDecodeError:
            return raw.lower(), None
        cmd = str(obj.get("command", "")).lower()
        ver = obj.get("version")
        return cmd, (str(ver) if ver else None)
    return raw.lower(), None


# ════════════════════════════════════════════════════════════════════════════
# Server side — runs inside the FIRST LogicBoard process
# ════════════════════════════════════════════════════════════════════════════

class InstanceServer(QObject):
    """Listens on the named pipe and emits signals when commands arrive.

    Owner (typically ``ApplicationCore``) connects to:
      * ``show_requested``       — another launch asked for the UI
      * ``quit_requested``       — user (or external tool) asked for
                                    a full shutdown
      * ``supersede_requested``  — a NEWER LogicBoard version just
                                    launched and wants us to hand
                                    control over. Owner should shut
                                    down cleanly; the new process will
                                    become the primary server on its
                                    next connect attempt.
    """

    show_requested      = Signal()
    open_requested      = Signal(str)      # a file to open (its path)
    quit_requested      = Signal()
    supersede_requested = Signal(str)      # new version's version string

    def __init__(self, parent: Optional[QObject] = None) -> None:
        super().__init__(parent)
        self._server = QLocalServer(self)
        self._server.newConnection.connect(self._on_new_connection)
        # Set by start() when the bind failed BECAUSE a live instance owns
        # the pipe, as opposed to a genuine bind failure. The owner uses it
        # to tell "I'm a redundant second launch" from "IPC is broken".
        self.peer_alive = False

    def start(self) -> bool:
        """Bind the named pipe. Returns True on success.

        If a stale pipe file is sitting around from a crashed previous
        process, removeServer clears it before listen() retries.
        """
        self.peer_alive = False
        try:
            QLocalServer.removeServer(SERVER_NAME)
        except Exception as e:
            log.debug("removeServer raised (harmless): %s", e)
        if not self._server.listen(SERVER_NAME):
            # Work out WHICH failure this is before shouting about it.
            #
            # On Windows a pipe already owned by a live process fails here
            # with ACCESS_DENIED rather than AddressInUse, which reads like
            # a permissions problem and was logged as one — an ERROR with no
            # actionable content, when the real story is "another LogicBoard
            # is already running and this process should not have got this
            # far". ``ui.app`` now checks the pipe before starting anything,
            # so reaching this line means the peer appeared during our own
            # startup (two launches racing). That's expected, not an error.
            self.peer_alive = peer_is_listening()
            if self.peer_alive:
                log.info("InstanceServer: %s is already owned by a live "
                         "LogicBoard — this process is a second instance.",
                         SERVER_NAME)
            else:
                log.error("InstanceServer.listen failed: %s",
                          self._server.errorString())
            return False
        log.info("InstanceServer listening on %s (I am v%s)",
                  SERVER_NAME, APP_VERSION)
        return True

    def stop(self) -> None:
        try:
            self._server.close()
        except Exception:
            pass

    # ─── Connection handling ─────────────────────────────────────────────
    def _on_new_connection(self) -> None:
        sock = self._server.nextPendingConnection()
        if sock is None:
            return

        def _on_ready():
            try:
                raw = bytes(sock.readAll()).decode("ascii", errors="replace")
            except Exception as e:
                log.warning("InstanceServer read failed: %s", e)
                sock.disconnectFromServer()
                return
            cmd, ver = _decode(raw)
            log.info("InstanceServer received: cmd=%r sender_version=%r",
                      cmd, ver)

            # Version negotiation — only applies to the version-aware
            # (JSON) format. Legacy bare-word messages don't populate
            # ``ver``, so this whole block is skipped for them.
            supersede = False
            ack_body = b"ok\n"
            if ver:
                mine = SemVer.parse(APP_VERSION)
                theirs = SemVer.parse(ver)
                if mine and theirs:
                    if theirs > mine:
                        # A newer version just launched. We're going to
                        # step aside — but only AFTER acknowledging so
                        # the new process knows the handshake succeeded.
                        log.info("Superseded by v%s (I am v%s) — "
                                  "shutting down after ACK.",
                                  theirs, mine)
                        supersede = True
                        ack_body = b"superseded\n"
                    elif theirs < mine:
                        # Client is older than us. Their launch was
                        # stale — probably a leftover shortcut. Nothing
                        # for us to do beyond a normal show.
                        log.info("Stale-version client (v%s vs. mine v%s) "
                                  "— treating as show.", theirs, mine)

            # ACK back so the client can verify delivery.
            try:
                sock.write(ack_body)
                sock.flush()
                sock.waitForBytesWritten(_WRITE_TIMEOUT_MS)
            except Exception:
                pass
            QTimer.singleShot(50, lambda: sock.disconnectFromServer())

            # Dispatch — supersede overrides everything else, but even
            # for non-supersede messages the normal show/quit/ping
            # semantics still apply.
            if supersede:
                self.supersede_requested.emit(ver or "")
                return
            if cmd == "show":
                self.show_requested.emit()
            elif cmd == "open":
                path = _decode_path(raw)
                if path:
                    self.open_requested.emit(path)
                else:
                    self.show_requested.emit()
            elif cmd == "quit":
                self.quit_requested.emit()
            elif cmd == "ping":
                pass
            else:
                log.warning("InstanceServer unknown command: %r", cmd)

        sock.readyRead.connect(_on_ready)


# ════════════════════════════════════════════════════════════════════════════
# Client side — runs inside the SECOND launch trying to talk to the first
# ════════════════════════════════════════════════════════════════════════════

def peer_is_listening(timeout_ms: int = 400) -> bool:
    """Is another LogicBoard accepting connections on the pipe right now?

    Connect-and-drop, no command sent — so it costs the running instance
    nothing and can't be mistaken for a "show" request. Deliberately
    shorter than ``_CONNECT_TIMEOUT_MS``: this only ever runs on a path
    that already knows something is wrong, and it must not add a second of
    startup latency to answering that question.
    """
    sock = QLocalSocket()
    try:
        sock.connectToServer(SERVER_NAME)
        return sock.waitForConnected(timeout_ms)
    except Exception:
        return False
    finally:
        try:    sock.abort()
        except Exception: pass


def send_to_running_instance(command: str,
                              *, include_version: bool = True,
                              path: Optional[str] = None) -> bool:
    """Connect to the running LogicBoard process and send ``command``.

    ``include_version=True`` (the default for every new caller) sends
    the version-aware JSON payload so the running instance can decide
    to step aside if we're newer. Legacy callers can pass
    ``include_version=False`` for the pre-versioning bare-word wire.

    Returns True if delivery succeeded (running instance ACKed).
    Returns False if no instance is listening OR the I/O failed.
    """
    if command not in ("show", "quit", "ping", "open"):
        raise ValueError(f"unsupported instance command: {command!r}")
    if command in ("show", "open") and sys.platform == "win32":
        # Windows only lets the process the user just interacted with take
        # the foreground. That is US — the launch Explorer started — not
        # the running LogicBoard we are about to ask to come forward, whose
        # activateWindow() would otherwise just flash its taskbar button
        # while the PDF the teacher double-clicked opened out of sight.
        try:
            import ctypes
            ctypes.windll.user32.AllowSetForegroundWindow(-1)  # ASFW_ANY
        except Exception:
            pass
    sock = QLocalSocket()
    sock.connectToServer(SERVER_NAME)
    if not sock.waitForConnected(_CONNECT_TIMEOUT_MS):
        log.debug("send_to_running_instance: no server (%s)",
                  sock.errorString())
        return False
    try:
        payload = _encode(command,
                           APP_VERSION if include_version else None,
                           path)
        sock.write(QByteArray(payload))
        if not sock.waitForBytesWritten(_WRITE_TIMEOUT_MS):
            return False
        if not sock.waitForReadyRead(_READ_TIMEOUT_MS):
            return False
        reply = bytes(sock.readAll()).decode("ascii", errors="replace").strip()
        # Both "ok" and "superseded" are successful ACKs from our POV
        # — either way the running instance received the message. The
        # "superseded" reply means WE (the client) will now become the
        # primary once the old server exits.
        return reply.lower() in ("ok", "superseded")
    finally:
        try:    sock.disconnectFromServer()
        except Exception: pass