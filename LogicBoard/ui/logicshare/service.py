"""
ui/logicshare/service.py — LogicShare background service (Flask + Zeroconf).

Runs silently on the smart board: receives files (/upload) and announcements
(/announce) from teachers' LogicShare apps, and broadcasts this board on the
LAN via Zeroconf. Bridges worker-thread events to the Qt main thread through
signals so the UI (notifications, settings, history) can react.

Device-conflict fix (spec §8)
-----------------------------
Old behaviour: two instances on the same LAN → the one started later
terminated itself. Root cause: both registered the SAME Zeroconf service
name, so the second register_service() raised NonUniqueNameException and that
propagated into shutdown.

New behaviour:
  • The service NEVER auto-terminates.
  • The Zeroconf name is made unique per DEVICE, so two boards with different
    device names coexist cleanly (no false conflict).
  • A GENUINE conflict — another board advertising the SAME room + class —
    is detected via TXT records (and via a name collision as a fallback).
    On conflict we DISABLE serving (endpoints answer 503), keep the process
    alive, and emit ``conflict_detected`` so Settings can show the message
    plus Retry / Restart actions.
"""
from __future__ import annotations

import re
import os
import time
import shutil
import socket
import sys
import logging
import threading
import traceback
from pathlib import Path
from uuid import uuid4
from urllib.parse import unquote

from PySide6.QtCore import QObject, Signal

from ui.logicshare import net_watch

from ui.logicshare.ack import (
    ACK_PROTOCOL_VERSION, TEACHER_ACK_PORT, AckRecord,
)

logger = logging.getLogger("LogicShare.service")

# 16384 (2^14) — moved off the default Flask/Werkzeug port 5000 so it
# doesn't collide with other dev tooling that also grabs 5000. The
# transmitter side has been updated to match.
PORT = 16384

# ---------------------------------------------------------------------------
# Upload hot-path tuning
# ---------------------------------------------------------------------------
# CHUNK_SIZE — MUST match the transmitter's send size. The Windows sender
# streams uploads in 256 KB chunks (benchmark-proven sweet spot on the
# OnePlus 9 Pro / Wi-Fi 6 test setup: below 256 KB per-chunk overhead
# dominates, above ~1 MB TCP send/recv windows start refusing early ACKs
# and throughput plateaus). Reading with a smaller size here would just
# split every sender chunk into 4× ``read()`` calls receiver-side and cost
# 4× the Python round-trips for zero throughput gain.
# Receive chunk size. 256 KB, and the ceiling on this is NOT throughput.
#
# ``request.stream.read(n)`` blocks until n bytes have arrived, so the read
# size also sets how long the receiver can go without observing ANY progress
# — and the stall watchdog (``_STALL_SECONDS`` = 8 s) is armed off exactly
# that observation. The relationship is:
#
#       slowest client that avoids a false stall  =  chunk / _STALL_SECONDS
#
#       256 KB  ->   32 KB/s      (a link this slow really is dead)
#         1 MB  ->  131 KB/s      (a phone on weak Wi-Fi lives here)
#
# This was briefly raised to 1 MB to cut Python loop iterations, which was
# not worth doing: at 150 MiB/s it is the difference between 600 and 150
# reads per second, a few microseconds either way, and it would have made
# LogicShare Mobile on a bad signal show "Transmitter unresponsive…" during
# a transfer that was progressing perfectly well.
_CHUNK_SIZE = 256 * 1024

# ---------------------------------------------------------------------------
# Socket tuning for the RECEIVING side. This is where the transfer was
# actually being throttled, and it is not obvious, so:
#
# TCP is flow-controlled by the RECEIVER. The sender may only have as many
# unacknowledged bytes in flight as the receiver's advertised window, so
#
#       throughput  <=  receive_window / RTT
#
# The transmitter was carefully tuned — zero-copy sendfile(), TCP_NODELAY,
# a 4 MB SO_SNDBUF — but none of that can help, because a sender cannot make
# a receiver accept data faster than it says it can. Meanwhile werkzeug's
# server never touches SO_RCVBUF, so the accepted socket sat at the Windows
# default of 65,536 bytes. Measured on the bench machine. That default puts a
# hard ceiling on the link:
#
#       RTT  1 ms  ->   62 MiB/s     (fine — this is roughly loopback)
#       RTT  5 ms  ->   12 MiB/s
#       RTT 10 ms  ->    6 MiB/s     <- ordinary classroom Wi-Fi
#       RTT 25 ms  ->    2 MiB/s     <- busy AP, or a board far from it
#
# Which is why transfers felt fine when tested next to the router and slow in
# an actual classroom: the code was never the limit, the advertised window
# was. At 4 MB the same table reads 400 MiB/s at 10 ms — far above what any
# Wi-Fi link will deliver, so the window stops being the binding constraint
# and the radio becomes the limit, which is the correct place for it to be.
#
# Set on the LISTENING socket, not just on accepted ones. The TCP window-scale
# option is negotiated in the SYN handshake, so a buffer enlarged after accept
# is already too late to raise the scale factor for that connection. Accepted
# sockets inherit the listener's buffer size, so the listener is the right
# place for it.
_SO_RCVBUF = 4 * 1024 * 1024

# Sliding-window speed calculation. Bytes-per-second is reported over the
# last WINDOW seconds so a brief hiccup doesn't zero out the reading.
_SPEED_WINDOW = 1.0

# UI progress emit rate. 5 Hz keeps the label smooth without spamming the
# Qt event queue — anything faster than ~10 Hz is imperceptible on the
# smart-board display and just costs cross-thread hops.
_PROGRESS_EMIT_INTERVAL = 0.2

# Stall watchdog. If no chunk lands for this long the popup flips to
# "Transmitter unresponsive…". TCP itself will eventually drop the
# connection if the sender really vanished, but this makes it VISIBLE to
# the teacher long before TCP notices.
_STALL_SECONDS = 8.0

# The most one upload is allowed to be, and the headroom that must be left
# on the drive afterwards.
#
# Nothing used to bound either. ``/upload`` read from the socket and wrote to
# disk until the stream ended, so a single oversized file — or one sender
# stuck in a retry loop — fills the drive the board is running from. That is
# not just a failed transfer: LogicBoard autosaves to the same disk, so the
# teacher loses the ability to save the lesson they are in the middle of.
#
# 2 GiB is far above anything a classroom actually sends (a slide deck, a
# scanned worksheet, the occasional short video) and far below the point
# where a full drive is the real problem.
#
# The reserve matters as much as the cap: a 1.5 GB upload onto a drive with
# 1.6 GB free would pass the cap and still leave the machine with nothing to
# work in. 512 MiB is enough for a board with its media and a few autosaves.
_MAX_UPLOAD_BYTES = 2 * 1024 * 1024 * 1024
_DISK_RESERVE_BYTES = 512 * 1024 * 1024

# How long an announcement id stays in the "already displayed" set.
#
# Bounded because the set would otherwise grow for the lifetime of the
# process. 12 hours comfortably outlives any retry a teacher device will
# make for one announcement (its own send retries are minutes, and the
# acknowledgment TTL is 8 h), while keeping the set to a day's worth of
# announcements at most.
_SEEN_ANNOUNCEMENT_TTL = 12 * 60 * 60

# On Linux/macOS ``os.fdatasync`` flushes just the data pages (skips
# metadata). We're about to ``shutil.move`` the file into Shared Files
# anyway, which mutates the metadata (mtime, containing dir), so the data
# flush is all we actually need for durability. ``os.fsync`` is used as
# the portable fallback on Windows where fdatasync doesn't exist.
_fdatasync = getattr(os, "fdatasync", os.fsync)

# ---------------------------------------------------------------------------
# Lifecycle tuning
# ---------------------------------------------------------------------------
# Restart delay (seconds). When the user hits "Restart" in Settings we
# deliberately linger in the "restarting" state for a beat so the UI can
# animate the transition and any transmitters see the mDNS "goodbye"
# packets clear their caches before we re-advertise. Kept in one place so
# it's easy to tune without diving through the code.
_RESTART_DELAY = 3.0

# Network monitor interval (seconds). Polls the local IP + reachability
# in a background thread. Catches two situations the old code missed:
#   1. Wi-Fi cable pulled while service is running → emit "offline" so the
#      Settings card can flip its badge to red without waiting for the
#      user to interact.
#   2. Wi-Fi hops from 2.4 GHz → 5 GHz, or Wi-Fi → LAN, mid-session → the
#      local IP changes but Zeroconf is still advertising the OLD one, so
#      transmitters try the stale address and fail. On detected IP change
#      we re-broadcast Zeroconf with the fresh IP.
# 2 seconds is a good compromise: fast enough that a network switch is
# invisible to the teacher, slow enough that the UDP probe in
# ``get_local_ip()`` doesn't tax the interface.
_MONITOR_INTERVAL = 2.0

# --- REMOVED: aggressive Zeroconf keep-alive rebuild ---
# The previous version rebuilt the Zeroconf instance every ``N`` monitor
# ticks as a safety net for "silently-dead multicast socket" cases. It
# turned out to be actively harmful:
#   • Every rebuild creates a fresh asyncio event loop + IOCP proactor on
#     Windows — HEAVY, not cheap the way UDP re-announces would be.
#   • ``Zeroconf.close()`` doesn't wait for its pending async goodbye
#     tasks, so every teardown leaks a "Task was destroyed but it is
#     pending!" warning. At 4 s cadence those pile up into GC pressure,
#     leaked event loops, and eventually Windows starts flagging the main
#     thread as unresponsive.
#   • Transmitters saw the board's service name churn (new random suffix
#     every 4 s), so the classroom board list was flickering.
#
# Zeroconf handles TTL refresh internally — a stable ``ServiceInfo``
# re-announces before the record TTL (~75-120 s) expires without any
# help from us. Rebuild is now triggered ONLY on the events that
# actually require a fresh multicast socket:
#     (a) network transitioned offline → online, or
#     (b) the primary IP changed while running.
# Both are handled by the monitor's normal detection path.


def get_local_ip() -> str:
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


# The most an announcement field is allowed to be, in characters.
#
# An announcement is a popup on a classroom wall. A title longer than a
# headline is already unreadable there, and there was no limit at all: the
# body of the request went to the UI as it arrived.
_ANNOUNCE_LIMITS = {
    "title": 200,
    "message": 4000,
    "first_name": 100,
    "surname": 100,
    "short_name": 8,
    "title_honorific": 40,
    "announcement_id": 128,
    "teacher_device_id": 128,
}


def _clean_field(value, limit: int) -> str:
    """One JSON value from the network, as a bounded, printable string.

    Every field of an announcement goes through this, because every one of
    them arrives over HTTP from another machine and none of them can be
    assumed to be a string.

    THE BUG THIS REPLACES
    ---------------------
    ``(data.get("title") or "").strip()`` looks defensive and is not. The
    ``or`` only substitutes when the value is FALSY, so a title of ``0`` or
    ``""`` was handled — while ``123`` and ``{"a": 1}`` sailed through and
    hit ``.strip()`` on an int and on a dict. Both were 500s, and the
    exception text was echoed back to the caller.

    Control characters go too. A NUL in a title is not a security problem
    here, but it truncates the string in some Win32 text APIs, which turns
    "Assembly at 10" into "Assembly" on the one screen that matters.
    """
    if value is None or isinstance(value, (dict, list, tuple, set)):
        # A structure is not a mistyped string — there is nothing sensible
        # to render, so treat it as absent.
        return ""
    if isinstance(value, bool):
        return ""
    if not isinstance(value, str):
        value = str(value)
    value = "".join(ch for ch in value if ch == " " or ch.isprintable())
    value = value.strip()
    if len(value) > limit:
        value = value[:limit].rstrip()
    return value


def _free_bytes(path) -> "int | None":
    """Free space on the volume holding ``path``, or None if unknowable.

    Walks up to the nearest directory that exists, because the temp
    directory is created lazily and asking about a path that is not there
    yet raises rather than answering about its volume.
    """
    try:
        p = Path(path)
        for candidate in (p, *p.parents):
            if candidate.exists():
                return shutil.disk_usage(str(candidate)).free
    except Exception:
        logger.debug("free space check failed for %s", path, exc_info=True)
    return None


def _safe_token(text: str) -> str:
    """Make a string safe for a Zeroconf instance name."""
    return re.sub(r"[^A-Za-z0-9_-]", "-", (text or "").strip()) or "x"


def format_size(n: float) -> str:
    if n < 1024:
        return f"{int(n)} B"
    for unit in ("KB", "MB", "GB"):
        n /= 1024
        if n < 1024:
            return f"{n:.1f} {unit}"
    return f"{n / 1024:.1f} TB"


def safe_incoming_filename(raw: str) -> str:
    """Make a filename from an untrusted header safe to write, WITHOUT
    throwing away what it says.

    ``werkzeug.utils.secure_filename`` was doing this job and is the wrong
    tool for it here: it is ASCII-only, so it deletes every non-Latin
    character. In a school where a teacher might reasonably send
    ``गणित.pdf``, that produced a file called ``pdf`` — the name gone AND the
    extension eaten, so the board could not identify it as a PDF, could not
    preview it, and Windows could not open it. ``नोट्स.png`` landed as
    ``png``; even ``Ωmega.pdf`` came out as ``mega.pdf``.

    What actually has to be prevented is escaping the destination folder and
    writing something Windows rejects. So:

      * take only the last path component, treating BOTH separators as
        separators, so "..\..\etc\passwd" and "../../etc/passwd" both
        reduce to "passwd";
      * drop anything that is only dots ("." / ".."), which name a directory
        rather than a file;
      * replace the characters Windows refuses, and control characters;
      * trim trailing dots and spaces, which Windows silently strips — a
        file we thought we wrote as "notes." is "notes" on disk;
      * cap the length, leaving room for the " (2)" that ``unique_path``
        may add;
      * fall back to "upload.bin" if nothing usable survives.

    Unicode letters are kept. NTFS stores filenames as UTF-16 and has no
    trouble with any script.
    """
    import re
    name = str(raw or "")
    # Both separators, whatever the sender's platform uses.
    name = name.replace("\\", "/").split("/")[-1]
    name = re.sub(r'[<>:"|?*]', "_", name)
    name = "".join(ch if (ch.isprintable() or ch == " ") else "_"
                   for ch in name)
    name = re.sub(r"\s+", " ", name).strip().rstrip(". ")
    if not name or set(name) <= {"."}:
        return "upload.bin"
    # Length is in CHARACTERS here; a Devanagari name is short in characters
    # and long in bytes, and it is the filesystem's character limit that
    # matters on NTFS.
    if len(name) > 180:
        stem, dot, ext = name.rpartition(".")
        if dot and len(ext) <= 12:
            name = stem[:180 - len(ext) - 1].rstrip(". ") + "." + ext
        else:
            name = name[:180].rstrip(". ")
    return name or "upload.bin"


def unique_path(target: Path) -> Path:
    if not target.exists():
        return target
    stem, suffix, parent = target.stem, target.suffix, target.parent
    n = 2
    while True:
        cand = parent / f"{stem} ({n}){suffix}"
        if not cand.exists():
            return cand
        n += 1


class LogicShareService(QObject):
    """Owns the Flask server + Zeroconf registration/browse for one board."""

    # File transfer
    upload_started  = Signal(dict)
    # transfer_id, bytes received so far, instantaneous speed (bytes/sec)
    upload_progress = Signal(str, object, object)
    upload_finished = Signal(dict)
    upload_failed   = Signal(str, str)       # transfer_id, error
    # Stall watchdog — fires when no chunk has arrived for ``_STALL_SECONDS``,
    # fires the inverse signal once data starts flowing again.
    upload_stalled  = Signal(str, dict)      # transfer_id, info
    upload_resumed  = Signal(str, dict)      # transfer_id, info

    # Announcements
    announcement_received = Signal(dict)

    # Service state
    # "running" | "stopped" | "offline" | "disabled" | "restarting"
    status_changed    = Signal(str)
    conflict_detected = Signal(dict)         # {peer_device, room, class, ...}
    conflict_cleared  = Signal()

    def __init__(self, config, activity_log, *,
                 shared_dir: Path, temp_dir: Path, parent=None,
                 ack_dir: Path = None):
        super().__init__(parent)
        self.config = config
        self.activity = activity_log
        self.shared_dir = Path(shared_dir)
        self.temp_dir = Path(temp_dir)

        # ---- announcement acknowledgment plumbing -----------------------
        # Kept in three small collaborators rather than folded into this
        # class: the queue is pure state, the registry is pure routing, and
        # the dispatcher is the only part that touches the network. That
        # split is what let the whole thing be unit-tested without a board,
        # a teacher device or a LAN.
        from ui.logicshare.ack import AckQueue, TeacherRegistry, AckDispatcher
        base = Path(ack_dir) if ack_dir else Path(shared_dir).parent
        self._ack_queue = AckQueue(base / "ls_ack_queue.json")
        self._teachers = TeacherRegistry(base / "ls_teachers.json")
        self._ack_dispatcher = AckDispatcher(self._ack_queue, self._teachers)
        # Announcements this board has SHOWN, so a second delivery of the
        # same id is recognised as a duplicate rather than popped up twice.
        self._seen_announcements: dict = {}
        self._seen_lock = threading.RLock()

        self._enabled = False        # serving? (False while conflicted/stopped)
        self._conflict = None        # dict describing an active conflict
        self._local_ip = "127.0.0.1"

        # HTTP server lifecycle handles.
        # ``_server`` is a werkzeug ``BaseWSGIServer`` instance (or None when
        # stopped). We hold an explicit handle so ``stop()`` can call
        # ``.shutdown()`` on it — the old ``app.run()`` design had no way to
        # bring the socket down, which meant transmitters with a cached IP
        # still hit ``/ping`` after the user pressed Stop and treated the
        # board as "existing but temporarily off" instead of gone. Now the
        # listen socket actually goes away, so a connection attempt gets
        # ECONNREFUSED and the transmitter's UI drops us entirely.
        self._server = None
        self._flask_thread = None
        self._app = None
        self._zc = None
        self._svc_info = None
        self._browser = None

        # Restart state — set while ``restart()`` is between stop and
        # start so ``status()`` can report "restarting" for the UI.
        self._restarting = False

        # Live transfer tracking (transfer_id -> mutable dict). Populated by
        # the upload route. ``active_transfers()`` reads this so the settings
        # card can refuse to stop the service while a file is still arriving.
        self._active_transfers: dict = {}
        self._transfer_lock = threading.Lock()

        # Network monitor thread — polls the local IP + reachability every
        # _MONITOR_INTERVAL seconds. Handles:
        #   • offline / online transitions (emits status_changed)
        #   • IP change while running (re-broadcasts Zeroconf on new IP so
        #     the transmitter can still find us after a Wi-Fi hop)
        # NB: a periodic keep-alive rebuild used to live here as a safety
        # net for silently-dead multicast sockets. It was removed after
        # profiling showed it was freezing the UI on Windows (heavy
        # asyncio event-loop churn from ``Zeroconf.close()`` not waiting
        # on pending goodbye tasks). Zeroconf refreshes TTLs internally,
        # so rebuild is now triggered ONLY by the two real transitions
        # above. See the block comment on the removed ``_KEEPALIVE_TICKS``
        # constant for the full write-up.
        # Runs for the entire process lifetime; ``shutdown()`` stops it.
        self._last_status = self.status()
        # Snapshot of every non-loopback IP the OS knows about — not just
        # the primary. The primary IP alone doesn't change on a Wi-Fi hop
        # when DHCP renews with the same lease, which is exactly what was
        # causing the "still invisible after switching bands" bug. Tracking
        # the whole set catches adapter up/down events even when the IP
        # string is unchanged.
        self._last_ip_set: frozenset = frozenset()

        # Stable Zeroconf service instance name. Computed once per
        # process (or once per config change) and reused across every
        # rebuild — the transmitter sees ONE steady identity for this
        # board across a Wi-Fi hop or a keep-alive re-announce, instead
        # of a stream of "-e9a7 / -848b / -6b86 …" ghost boards
        # appearing and disappearing. Populated lazily on first
        # ``_register_zeroconf`` call.
        self._svc_instance_name = None

        self._monitor_stop = threading.Event()

        # ── Being told, instead of finding out ──────────────────────────
        # The monitor below used to wake on a fixed 2 s timer, so the worst
        # case for noticing a Wi-Fi hop was 2 s of advertising an address
        # this board no longer owns — 2 s in which a teacher's transmitter
        # tries the stale IP and reports the board as unreachable.
        #
        # Windows will just tell us. ``net_watch.NetworkWatcher`` parks a
        # thread inside ``NotifyAddrChange`` and sets ``_net_wake`` the
        # instant an address changes, which cuts the monitor's wait short.
        # The 2 s tick stays as the floor for status propagation and as the
        # safety net for anything the OS does not report.
        #
        # ``_force_rebuild`` carries the other half. The monitor rebuilds
        # when the ADDRESS changed, and an adapter that drops and returns
        # on the same DHCP lease changes no address at all while still
        # killing the multicast socket bound to it. When the watcher fires,
        # the rebuild is not conditional on the address having moved.
        self._net_wake = threading.Event()
        self._force_rebuild = threading.Event()
        self._net_watcher = None
        try:
            self._net_watcher = net_watch.NetworkWatcher(
                self._on_network_event, name="LogicShare")
            self._net_watcher.start()
        except Exception:
            logger.exception(
                "Network watcher failed to start — falling back to the "
                "%.1fs poll", _MONITOR_INTERVAL)

        self._monitor_thread = threading.Thread(
            target=self._network_monitor, daemon=True,
            name="LogicShare-NetMonitor")
        self._monitor_thread.start()

    def _on_network_event(self, old_fp, new_fp, reason) -> None:
        """The watcher saw something. Hand it to the monitor.

        Deliberately does almost nothing: this runs on the watcher's
        thread, and Zeroconf teardown plus the Qt signal that follows it
        belong on the monitor thread that already owns them. Setting two
        events is the whole job.
        """
        logger.info("Network event (%s): %s -> %s", reason,
                    net_watch.describe(old_fp), net_watch.describe(new_fp))
        self._force_rebuild.set()
        self._net_wake.set()

    # --------------------------------------------------------- active transfers
    def active_transfers(self) -> list:
        """Snapshot of in-flight uploads (list of dicts). Empty when idle."""
        with self._transfer_lock:
            return [dict(v) for v in self._active_transfers.values()]

    def has_active_transfer(self) -> bool:
        with self._transfer_lock:
            return bool(self._active_transfers)

    # --------------------------------------------------------------- net status
    @staticmethod
    def _is_online() -> bool:
        """True iff the board has ANY usable LAN/WiFi IP (i.e. not just
        loopback). Pure local-network check — does NOT probe the internet,
        because LogicShare is a LAN-only tool: a classroom WiFi with no WAN is
        still 'online' for our purposes."""
        try:
            ip = get_local_ip()
            return bool(ip) and not ip.startswith("127.")
        except Exception:
            return False

    # ------------------------------------------------------------------ state
    @property
    def enabled(self) -> bool:
        return self._enabled

    @property
    def conflict(self):
        return self._conflict

    def status(self) -> str:
        """Five-state lifecycle:
        - ``restarting`` — mid-restart (highest priority; hides every other
                          state so the UI can show a clear "please wait…").
        - ``running``    — service on AND a usable LAN IP is present.
        - ``offline``    — service on but the board has no LAN/WiFi at all
                          (no point trying to broadcast — nobody can reach
                          us). Rendered in red by the Settings card.
        - ``stopped``    — explicitly stopped by the user.
        - ``disabled``   — auto-disabled because another board owns this
                          class.
        """
        if self._restarting:
            return "restarting"
        if self._conflict:
            return "disabled"
        if not self._enabled:
            return "stopped"
        if not self._is_online():
            return "offline"
        return "running"

    # ---------------------------------------------------------------- service
    def start(self) -> None:
        """Start (or re-start) serving + broadcasting. Safe to call
        repeatedly — if the HTTP server is already up we just refresh the
        Zeroconf broadcast.

        Unlike the old ``_flask_started``-once-per-process design, ``start``
        now brings up a fresh ``werkzeug`` HTTP server every time. That's
        what lets ``stop()`` actually close the listen socket so
        transmitters with a cached IP get ECONNREFUSED and drop us from
        their board list, instead of hitting a still-alive port and seeing
        a 503-flavoured "board exists but is disabled" response.
        """
        self._local_ip = get_local_ip()
        self.shared_dir.mkdir(parents=True, exist_ok=True)
        self.temp_dir.mkdir(parents=True, exist_ok=True)
        self._hide_temp_dir()
        self._sweep_temp()

        self._start_server()
        self._ack_dispatcher.start()
        self._conflict = None
        self._enabled = True
        self._register_zeroconf()
        # Seed the monitor's IP snapshot so the very next tick doesn't
        # treat "went from empty set → current set" as a change and
        # immediately rebuild.
        self._last_ip_set = self._enumerate_local_ips()
        self._emit_status_if_changed()
        logger.info("LogicShare service started (room=%s class=%s, %s:%d)",
                    self.config.room, self.config.class_standard,
                    self._local_ip, PORT)

    def _hide_temp_dir(self) -> None:
        """Mark the staging directory hidden on Windows.

        The staging dir now lives inside Shared Files so the final move is
        always a same-volume rename (see ui/app.py). The cost of that is a
        working directory sitting in a folder the teacher actually opens, and
        a leading dot does NOT hide anything on Windows — that is a Unix
        convention. Explorer needs the FILE_ATTRIBUTE_HIDDEN bit, so set it.

        Best effort: if it fails the only consequence is a visible folder.
        """
        if not sys.platform.startswith("win"):
            return
        try:
            import ctypes
            FILE_ATTRIBUTE_HIDDEN = 0x02
            attrs = ctypes.windll.kernel32.GetFileAttributesW(str(self.temp_dir))
            if attrs != -1 and not (attrs & FILE_ATTRIBUTE_HIDDEN):
                ctypes.windll.kernel32.SetFileAttributesW(
                    str(self.temp_dir), attrs | FILE_ATTRIBUTE_HIDDEN)
        except Exception as e:
            logger.debug("Could not hide staging dir: %s", e)

    # ------------------------------------------------------ acknowledgments
    def _prune_seen_locked(self) -> None:
        """Drop remembered announcement ids past their TTL.

        Caller must already hold ``_seen_lock``.
        """
        cutoff = time.time() - _SEEN_ANNOUNCEMENT_TTL
        stale = [k for k, v in self._seen_announcements.items() if v < cutoff]
        for k in stale:
            self._seen_announcements.pop(k, None)

    def acknowledge_announcement(self, info: dict) -> bool:
        """Record that a human pressed OK on ``info``, and get it delivered.

        Called by the announcement popup and by nothing else — that is the
        point. An announcement is NOT acknowledged because it arrived, or
        because it was displayed, or because a timer elapsed. Only this
        method marks it read, and only the OK button calls it.

        Returns True when an acknowledgment was queued for delivery, False
        when there is nobody to tell (a legacy sender that supplied no
        announcement id or teacher device id). False is not an error — it is
        the honest answer for an announcement that predates the protocol.
        """
        announcement_id = str(info.get("announcement_id") or "").strip()
        teacher_device_id = str(info.get("teacher_device_id") or "").strip()
        if not announcement_id or not teacher_device_id:
            logger.info("OK pressed on a legacy announcement (no id/teacher) "
                        "— nothing to acknowledge")
            return False

        try:
            from ui.util.version import APP_VERSION
        except Exception:
            APP_VERSION = ""

        entry = self._teachers.lookup(teacher_device_id) or {}
        rec = AckRecord(
            announcement_id=announcement_id,
            board_device_id=self.config.device_id,
            teacher_device_id=teacher_device_id,
            acknowledged_at=time.time(),
            teacher_host=entry.get("host", ""),
            teacher_port=int(entry.get("port") or TEACHER_ACK_PORT),
            board_name=self.config.device_name,
            room=self.config.room,
            class_standard=self.config.class_standard,
            section=self.config.section,
            board_version=APP_VERSION,
        )
        logger.info("OK pressed: announcement=%s board=%s -> teacher=%s",
                    announcement_id[:8], self.config.device_id[:8],
                    teacher_device_id[:8])
        self._ack_queue.enqueue(rec)
        # Try immediately. The overwhelmingly common case is that the teacher
        # is still on the LAN and this lands within milliseconds, so the
        # queue is a durability net rather than a delay.
        self._ack_dispatcher.flush_now()
        return True

    def pending_ack_count(self) -> int:
        """How many acknowledgments are still owed. For diagnostics/UI."""
        return self._ack_queue.pending_count()

    def _sweep_temp(self) -> None:
        """Delete leftover partial-transfer files in the temp dir (orphans
        from a crash/interruption mid-download). Safe: only touches our own
        temp dir, never the Shared Files folder."""
        try:
            if not self.temp_dir.exists():
                return
            removed = 0
            for f in self.temp_dir.iterdir():
                if f.is_file():
                    try:
                        f.unlink(); removed += 1
                    except OSError:
                        pass
            if removed:
                logger.info("Cleared %d stale transfer temp file(s).", removed)
        except OSError as e:
            logger.warning("temp sweep failed: %s", e)

    def stop(self) -> None:
        """Fully stop broadcasting AND serving. Board disappears from
        transmitters.

        Three things happen in order:
          1. Zeroconf ``unregister_service`` — sends mDNS goodbye packets
             (TTL=0) so well-behaved transmitters purge the record right
             away instead of waiting for the TTL to expire.
          2. HTTP server ``shutdown`` + socket close — kills port 5000 so
             even a transmitter that already cached our IP can't reach us.
             Without this the board would stay listed as "temporarily
             disabled" on the transmitter side (as was reported).
          3. Emit ``status_changed``.
        """
        self._enabled = False
        self._unregister_zeroconf()
        self._stop_server()
        # Drop the cached service-instance name so the next start() picks
        # a fresh random suffix. This isn't strictly required for
        # correctness — the same name would work fine on a re-start —
        # but it means if the user is bouncing the service to shake off
        # a weird cache on a peer transmitter, they get a clean new
        # identity that the peer can't have stale.
        self._svc_instance_name = None
        self._emit_status_if_changed()

    def restart(self) -> None:
        """User-initiated restart from the Settings card.

        Returns immediately; the actual work runs on a background thread
        so the UI stays responsive. During the ``_RESTART_DELAY`` window
        ``status()`` reports ``"restarting"`` — the Settings card renders
        that as a spinner + label. After the delay we bring everything
        back up on the (possibly-changed) current IP.

        The delay isn't just cosmetic: it gives transmitters time to
        process the mDNS goodbye packets from the stop phase, so when we
        re-advertise nobody's cache has us in a weird transient half-
        state.
        """
        logger.info("LogicShare service restart requested.")
        if self._restarting:
            logger.info("Restart already in progress — ignoring.")
            return
        self._restarting = True
        # Bring the current instance down synchronously so the socket is
        # actually gone before we tell the user "restarting". The delay
        # + re-start then run on a background thread.
        try:
            self._enabled = False
            self._unregister_zeroconf()
            self._stop_server()
        except Exception:
            logger.exception("stop phase of restart failed")
        # Emit AFTER stop-side cleanup so the UI's first "restarting"
        # frame lines up with the socket actually being closed.
        self._last_status = "restarting"
        self.status_changed.emit("restarting")

        def _finish():
            time.sleep(_RESTART_DELAY)
            self._restarting = False
            try:
                self.start()
            except Exception:
                logger.exception("start phase of restart failed")
                self._emit_status_if_changed()

        threading.Thread(target=_finish, daemon=True,
                          name="LogicShare-Restart").start()

    def retry(self) -> None:
        """Re-attempt after a conflict (alias of start that clears
        conflict)."""
        was_conflict = self._conflict is not None
        self.start()
        if was_conflict and self._conflict is None:
            self.conflict_cleared.emit()

    def shutdown(self) -> None:
        """Full teardown at process exit — stops the monitor thread too,
        so the interpreter can exit cleanly instead of waiting on a
        daemon join. Callers: application_core on quit.

        Note the asymmetry with ``stop()``, which deliberately leaves the
        ack dispatcher running: ``stop()`` means "this board should not
        serve any more", which does not cancel acknowledgments the board
        already owes for announcements a human already read. Only a real
        process exit stops the dispatcher — and even then the queue is on
        disk, so anything undelivered resumes on the next launch.
        """
        try:
            self._monitor_stop.set()
            # Release the monitor from its wait immediately rather than
            # letting it sit out the remaining tick.
            self._net_wake.set()
        except Exception:
            pass
        try:
            if self._net_watcher is not None:
                self._net_watcher.stop()
        except Exception:
            pass
        try:
            self.stop()
        except Exception:
            pass
        try:
            self._ack_dispatcher.stop()
        except Exception:
            pass

    def _emit_status_if_changed(self) -> None:
        """Emit ``status_changed`` only when the status actually
        transitioned. Called from every place that might change state so
        we don't spam the UI with redundant emits."""
        new = self.status()
        if new != self._last_status:
            self._last_status = new
            self.status_changed.emit(new)

    @staticmethod
    def _enumerate_local_ips() -> frozenset:
        """Snapshot every local IPv4 address the OS reports right now,
        excluding loopback. Used by the monitor's duty scan.

        Why not just ``get_local_ip()``? That helper returns a SINGLE IP
        (the one the OS would pick for outbound traffic to 8.8.8.8). When
        you hop from 2.4 GHz to 5 GHz on the same Wi-Fi network — or the
        adapter goes down and comes right back up — DHCP often renews
        with the same lease, so ``get_local_ip()`` returns an IP that
        looks unchanged. But under the hood the multicast socket that
        Zeroconf bound to the old adapter is DEAD. The service silently
        stops being reachable and the only way to fix it was to hit
        Restart manually. That's the reported bug.

        Enumerating ALL addresses catches the transition even when the
        primary IP happens to stay the same: adapters coming up/down
        virtually always change the set. When the set changes we do a
        full Zeroconf rebuild, which re-binds the multicast socket to
        the current interfaces.

        Uses only the stdlib (``socket.gethostbyname_ex``) so no extra
        dependency. On systems where the hostname doesn't resolve to
        all local IPs (rare — happens with weird ``/etc/hosts`` setups)
        the primary IP is still added as a fallback.
        """
        ips = set()
        try:
            hostname = socket.gethostname()
            _, _, addrs = socket.gethostbyname_ex(hostname)
            for ip in addrs:
                if ip and not ip.startswith("127."):
                    ips.add(ip)
        except Exception:
            pass
        # Belt-and-braces: always include whatever the "primary outbound"
        # IP is, in case the hostname didn't resolve to it.
        try:
            primary = get_local_ip()
            if primary and not primary.startswith("127."):
                ips.add(primary)
        except Exception:
            pass
        return frozenset(ips)

    def _network_monitor(self) -> None:
        """Background poller that keeps the service honest about the
        current network state. Runs for the process lifetime.

        Responsibilities on each tick:

          (1) STATUS PROPAGATION — emit ``status_changed`` when the
              computed state changes so the UI's Offline badge lights
              up (in red) the moment Wi-Fi drops.

          (2) OFFLINE HANDLING — when the primary IP goes to loopback
              (i.e. no interfaces at all), explicitly unregister
              Zeroconf. Otherwise the library sits holding a multicast
              socket bound to a dead adapter, and even after the network
              comes back the socket never recovers — the original
              "have to hit Restart" bug.

          (3) COMING-BACK-ONLINE FORCE-REBUILD — when we transition
              offline → online, ALWAYS rebuild Zeroconf, even if the
              new primary IP happens to equal the old one (DHCP often
              re-issues the same lease within the timeout). The
              transmitter-visible socket needs to be re-bound to the
              live adapter regardless of the address string.

          (4) IP-CHANGE REBUILD — while online, if the primary IP
              differs from what Zeroconf was registered with, rebuild.
              This catches the 2.4 → 5 GHz hop, Wi-Fi → LAN, etc.

        NB: there is DELIBERATELY no periodic "keep-alive" rebuild any
        more. See the comment on the removed ``_KEEPALIVE_TICKS``
        constant above for why — the short version is that Zeroconf
        already refreshes TTLs internally, and forcing a fresh instance
        every few seconds froze the UI on Windows.
        """
        # ``was_online`` tracks the previous tick's online-ness so we can
        # detect the offline → online transition explicitly. Starts True
        # to avoid spuriously firing a "we just came online!" rebuild on
        # the very first tick right after start().
        was_online = self._is_online()

        while True:
            # Wake on whichever comes first: the watcher saying the network
            # moved, or the tick expiring. ``Event.wait`` returns True only
            # when it was set, which is how we tell those apart.
            woken = self._net_wake.wait(_MONITOR_INTERVAL)
            if self._monitor_stop.is_set():
                break
            if woken:
                self._net_wake.clear()

            # Latch and clear together: the watcher can fire again while
            # this tick runs, and that next event deserves its own rebuild
            # rather than being folded into this one.
            forced = self._force_rebuild.is_set()
            if forced:
                self._force_rebuild.clear()

            if self._restarting:
                # Don't fight the restart flow — the follow-up start()
                # will re-latch state itself.
                continue

            # (1) Status transition — evaluated whether enabled or not.
            self._emit_status_if_changed()

            if not self._enabled:
                # No point poking Zeroconf when we're explicitly stopped
                # or in conflict. Keep ``was_online`` current though so
                # the next start() lands with a correct baseline.
                was_online = self._is_online()
                continue

            # Primary-IP check via UDP-connect (reads the LIVE routing
            # table on Windows; not cached like ``gethostbyname_ex``).
            try:
                primary_ip = get_local_ip()
            except Exception:
                primary_ip = "127.0.0.1"
            is_online = bool(primary_ip) and not primary_ip.startswith("127.")

            # (2) OFFLINE — explicitly tear Zeroconf down so we don't
            # accumulate a dangling socket on a dead adapter.
            if not is_online:
                if was_online:
                    logger.info("Network went offline — unregistering Zeroconf.")
                    try:
                        self._unregister_zeroconf()
                    except Exception:
                        logger.exception("Zeroconf teardown on offline failed")
                was_online = False
                self._last_ip_set = frozenset()
                self._local_ip = primary_ip
                continue

            # (3) BACK-ONLINE — force full rebuild regardless of whether
            # the address string changed. DHCP renewals often re-issue
            # the same address on a fresh adapter, so a string compare
            # alone would miss this transition.
            if not was_online:
                logger.info("Network back online (primary=%s) — rebuilding Zeroconf.",
                             primary_ip)
                self._force_rebuild.clear()
                self._local_ip = primary_ip
                try:
                    self._unregister_zeroconf()
                    self._register_zeroconf()
                except Exception:
                    logger.exception("Zeroconf rebuild on back-online failed")
                self._last_ip_set = self._enumerate_local_ips()
                was_online = True
                continue

            # (4) IP change while online.
            new_ips = self._enumerate_local_ips()
            ip_set_changed = (new_ips != self._last_ip_set)
            primary_changed = (primary_ip != self._local_ip)

            # ``forced`` is the case the address comparison cannot see: the
            # adapter went down and came back on the same lease, or the
            # teacher moved between two SSIDs on one router that share a
            # subnet. Nothing about the address moved; the multicast socket
            # bound to the old adapter is dead regardless.
            if primary_changed or ip_set_changed or forced:
                old_ip = self._local_ip
                self._local_ip = primary_ip
                self._last_ip_set = new_ips
                try:
                    self._unregister_zeroconf()
                    self._register_zeroconf()
                    logger.info(
                        "Network change: primary %s → %s, "
                        "interfaces=%s%s. Zeroconf rebuilt.",
                        old_ip, primary_ip, sorted(new_ips),
                        " (adapter event, address unchanged)"
                        if forced and not (primary_changed or ip_set_changed)
                        else "")
                except Exception:
                    logger.exception("Zeroconf rebuild on IP change failed")

    # ---- HTTP server lifecycle (werkzeug directly, so we can shut it) ----
    def _start_server(self) -> None:
        """Bring up a fresh ``werkzeug.serving`` HTTP server. Idempotent —
        if a server is already alive we leave it (the Zeroconf broadcast
        is what needs refreshing on start() calls after the first)."""
        if self._server is not None:
            return
        if self._app is None:
            self._build_flask()
        try:
            from werkzeug.serving import make_server
            from werkzeug.serving import WSGIRequestHandler

            class _TunedHandler(WSGIRequestHandler):
                """Per-connection socket tuning for bulk uploads.

                ``disable_nagle_algorithm`` is a socketserver hook: it sets
                TCP_NODELAY on the accepted socket. Nagle batches small writes
                to save packets, which is the wrong trade for a bulk transfer
                whose response we want back the instant the file lands.

                SO_RCVBUF is re-asserted here as well as on the listener.
                Inheritance from the listening socket is the documented
                behaviour and does happen, but it costs one syscall to be
                certain, and being wrong about it silently reintroduces the
                very bottleneck this exists to remove.
                """
                disable_nagle_algorithm = True

                def setup(self):
                    # super() FIRST. socketserver sets self.connection inside
                    # StreamRequestHandler.setup(); touching it before that
                    # raises, and werkzeug's __getattr__ proxy turns the
                    # AttributeError into a confusing one about 'super'.
                    # Applying Nagle/TCP_NODELAY is also that base call's job.
                    super().setup()
                    try:
                        self.connection.setsockopt(
                            socket.SOL_SOCKET, socket.SO_RCVBUF, _SO_RCVBUF)
                    except OSError:
                        pass      # kernel may clamp; we still get its maximum

            self._server = make_server(
                host="0.0.0.0", port=PORT, app=self._app,
                threaded=True, request_handler=_TunedHandler)
            # And on the LISTENING socket, which is what actually governs the
            # window scale negotiated during each connection's handshake.
            try:
                self._server.socket.setsockopt(
                    socket.SOL_SOCKET, socket.SO_RCVBUF, _SO_RCVBUF)
                actual = self._server.socket.getsockopt(
                    socket.SOL_SOCKET, socket.SO_RCVBUF)
                logger.info("HTTP receive buffer: requested %d, got %d bytes",
                            _SO_RCVBUF, actual)
            except OSError as e:
                logger.debug("SO_RCVBUF on listener not applied: %s", e)
        except Exception as e:
            logger.error("Failed to bind HTTP server: %s", e)
            self._server = None
            return
        self._flask_thread = threading.Thread(
            target=self._run_flask, daemon=True,
            name="LogicShare-HTTP")
        self._flask_thread.start()

    def _stop_server(self) -> None:
        """Shut down the werkzeug server and wait for its thread to exit
        so the listen socket is genuinely gone by the time this returns."""
        srv = self._server
        self._server = None
        if srv is not None:
            try:
                srv.shutdown()
            except Exception:
                logger.exception("server.shutdown() failed")
            try:
                srv.server_close()
            except Exception:
                pass
        thr = self._flask_thread
        if thr is not None and thr.is_alive():
            thr.join(timeout=2.0)
        self._flask_thread = None

    # ----------------------------------------------------- conflict handling
    def _enter_conflict(self, detail: dict) -> None:
        """Disable serving WITHOUT terminating, and notify the UI. Also
        brings the HTTP socket down for the same reason ``stop()`` does —
        a conflicted board should be invisible to transmitters, not just
        marked disabled."""
        if self._conflict is not None:
            return
        self._conflict = detail
        self._enabled = False
        logger.warning("LogicShare conflict detected: %s", detail)
        try:
            self._unregister_zeroconf()
        except Exception:
            pass
        try:
            self._stop_server()
        except Exception:
            pass
        self._emit_status_if_changed()
        self.conflict_detected.emit(detail)

    # ----------------------------------------------------------- zeroconf
    def _register_zeroconf(self) -> None:
        try:
            from zeroconf import Zeroconf, ServiceInfo, ServiceBrowser
            from zeroconf import NonUniqueNameException, InterfaceChoice
        except Exception as e:
            logger.warning("Zeroconf unavailable (%s) — LAN discovery off.", e)
            return

        self._unregister_zeroconf()

        # CRITICAL: bind Zeroconf explicitly to the CURRENT primary IP.
        #
        # Without this, ``Zeroconf()`` uses ``InterfaceChoice.Default``,
        # which on Windows queries the OS for the "default" adapter at
        # instantiation time — and on a machine that just switched from
        # Wi-Fi to Ethernet (or 2.4 → 5 GHz on multi-radio dongles) the
        # OS's idea of "default" can still point at the old, now-dead
        # adapter for several seconds. The Zeroconf multicast socket
        # ends up bound to the wrong NIC and NEVER recovers — even a
        # full rebuild does the same thing again. That's the root of
        # "I have to hit Restart to be visible on the new network".
        #
        # Passing an explicit interface list forces the mDNS socket to
        # bind to THIS specific IP, which is the address we've already
        # confirmed corresponds to the currently-active adapter (via the
        # UDP-connect trick in ``get_local_ip``). If for some reason the
        # binding fails on the specific IP we fall back to ``All`` — one
        # more chance to succeed instead of dying quietly.
        interfaces_arg = ([self._local_ip]
                          if self._local_ip and not self._local_ip.startswith("127.")
                          else InterfaceChoice.All)
        try:
            self._zc = Zeroconf(interfaces=interfaces_arg)
        except Exception as e:
            logger.warning("Zeroconf bind on %s failed (%s) — retrying on All",
                            interfaces_arg, e)
            try:
                self._zc = Zeroconf(interfaces=InterfaceChoice.All)
            except Exception as e2:
                logger.warning("Could not start Zeroconf: %s", e2)
                self._zc = None
                return

        device = _safe_token(self.config.device_name)
        cls    = _safe_token(self.config.class_standard or "x")
        sec    = _safe_token(self.config.section or "x")
        room   = _safe_token(self.config.room or "x")
        # Stable service instance name — computed ONCE per start()/stop()
        # cycle and reused across every subsequent rebuild (Wi-Fi hop,
        # back-online, etc.). The transmitter sees ONE steady identity
        # for this board across the whole session instead of a stream of
        # "-e9a7 / -848b / -6b86 …" ghost boards flickering in and out
        # of its discovery list every rebuild.
        #
        # The random suffix is retained (short 4-hex) so a same-device
        # process restart that races with its own pending de-registration
        # doesn't collide, but within one process lifetime the suffix is
        # frozen. Cleared in ``stop()`` so the next start() picks a fresh
        # one.
        if not self._svc_instance_name:
            self._svc_instance_name = (
                f"LogicBoard-{device}-{cls}{sec}-{room}-{uuid4().hex[:4]}")
        instance = self._svc_instance_name
        self._svc_info = ServiceInfo(
            "_logicboard._tcp.local.",
            f"{instance}._logicboard._tcp.local.",
            addresses=[socket.inet_aton(self._local_ip)],
            port=PORT,
            properties={
                b"room":    (self.config.room or "").encode(),
                b"class":   (self.config.class_standard or "").encode(),
                b"section": (self.config.section or "").encode(),
                b"device":  (self.config.device_name or "").encode(),
                # Stable identity, so a teacher device can recognise this
                # board across an IP change. The short key keeps the TXT
                # record small — mDNS TXT is size-constrained.
                b"did":     (self.config.device_id or "").encode(),
                b"ackv":    str(ACK_PROTOCOL_VERSION).encode(),
            },
        )
        try:
            self._zc.register_service(self._svc_info)
            logger.info("Zeroconf broadcasting as %s", instance)
        except NonUniqueNameException:
            # Fallback conflict signal (shouldn't happen now names are unique).
            self._enter_conflict({
                "reason": "name_collision",
                "room": self.config.room,
                "class": self.config.class_standard,
                "detail": "Another board is already registered with this name.",
            })
            return
        except Exception as e:
            logger.warning("register_service failed: %s", e)

        # Browse for OTHER boards to detect a genuine classroom-identity clash.
        try:
            self._browser = ServiceBrowser(
                self._zc, "_logicboard._tcp.local.", handlers=[self._on_zc_change]
            )
        except Exception as e:
            logger.debug("ServiceBrowser failed: %s", e)

    def _on_zc_change(self, zeroconf, service_type, name, state_change):
        """Detect another board claiming the SAME room + class (real conflict)."""
        try:
            from zeroconf import ServiceStateChange
            if state_change is not ServiceStateChange.Added:
                return
            if self._svc_info and name == self._svc_info.name:
                return  # ourselves
            info = zeroconf.get_service_info(service_type, name, timeout=2000)
            if not info or not info.properties:
                return
            props = info.properties
            peer_room  = (props.get(b"room")    or b"").decode(errors="ignore")
            peer_class = (props.get(b"class")   or b"").decode(errors="ignore")
            peer_sec   = (props.get(b"section") or b"").decode(errors="ignore")
            peer_dev   = (props.get(b"device")  or b"").decode(errors="ignore")
            same_identity = (
                peer_room == (self.config.room or "")
                and peer_class == (self.config.class_standard or "")
                and peer_sec == (self.config.section or "")
            )
            different_device = peer_dev != (self.config.device_name or "")
            if same_identity and different_device:
                self._enter_conflict({
                    "reason": "duplicate_classroom",
                    "room": self.config.room,
                    "class": self.config.class_standard,
                    "section": self.config.section,
                    "peer_device": peer_dev or "Unknown device",
                    "detail": (f"Another smart board ({peer_dev or 'unknown'}) is "
                               f"already serving Class {self.config.class_standard}-"
                               f"{self.config.section} "
                               f"· Room {self.config.room} on this network."),
                })
        except Exception as e:
            logger.debug("zc change handler error: %s", e)

    def _unregister_zeroconf(self) -> None:
        try:
            if self._browser:
                self._browser.cancel()
        except Exception:
            pass
        self._browser = None
        # ORDER MATTERS: unregister_service schedules a mDNS goodbye
        # packet as an async task on Zeroconf's internal event loop.
        # If we call ``close()`` immediately after, the loop stops with
        # that task still pending — Python then dumps "Task was
        # destroyed but it is pending!" all over the log and (much
        # worse) the goodbye may never actually make it onto the wire,
        # so transmitters keep our now-defunct record cached until TTL.
        # A short sleep in between gives the goodbye task time to
        # transmit before we tear the loop down.
        try:
            if self._zc and self._svc_info:
                self._zc.unregister_service(self._svc_info)
                # 150 ms is generous for a single-hop LAN multicast to
                # actually leave the socket, and imperceptible to the
                # user during a network transition. Only sleeps if we
                # actually had a service to unregister.
                time.sleep(0.15)
        except Exception:
            pass
        try:
            if self._zc:
                self._zc.close()
        except Exception:
            pass
        self._zc = None
        self._svc_info = None

    # -------------------------------------------------------------- flask
    def _build_flask(self) -> None:
        from flask import Flask, request, jsonify

        app = Flask("logicshare_board")
        svc = self  # closure handle

        @app.errorhandler(Exception)
        def _unhandled(e):
            logger.error("Flask error:\n%s", traceback.format_exc())
            return jsonify({"status": "error", "message": str(e)}), 500

        def _guard():
            """Return a 503 response if serving is disabled, else None."""
            if not svc._enabled:
                return jsonify({"status": "disabled",
                                "message": "LogicShare is temporarily disabled "
                                           "on this board."}), 503
            return None

        @app.route("/ping", methods=["GET"])
        def ping():
            return jsonify({
                "status": "ok" if svc._enabled else "disabled",
                "class":  svc.config.class_standard,
                "room":   svc.config.room,
                "device": svc.config.device_name,
            })

        @app.route("/upload", methods=["POST"])
        def upload():
            blocked = _guard()
            if blocked:
                return blocked
            transfer_id = str(uuid4())
            tmp_path = None
            try:
                # Content-Length arrives from the network, so it is
                # parsed rather than trusted. ``int()`` on a malformed
                # header used to raise straight into the generic handler
                # and answer 500 — "the board is broken" — for what is
                # simply a bad request.
                try:
                    declared = int(request.headers.get("Content-Length", 0) or 0)
                except (TypeError, ValueError):
                    declared = 0
                if declared < 0:
                    declared = 0

                if declared > _MAX_UPLOAD_BYTES:
                    logger.warning("Refusing %s byte upload (cap is %s)",
                                   declared, _MAX_UPLOAD_BYTES)
                    return jsonify({
                        "status": "too_large",
                        "message": ("That file is %s. This board accepts up "
                                    "to %s." % (format_size(declared),
                                                format_size(_MAX_UPLOAD_BYTES))),
                        "max_bytes": _MAX_UPLOAD_BYTES,
                    }), 413

                # Room to land it AND to keep working afterwards. Checked
                # against the temp directory, which is where the bytes
                # actually go.
                free = _free_bytes(svc.temp_dir)
                if free is not None and declared:
                    if declared + _DISK_RESERVE_BYTES > free:
                        logger.warning("Refusing upload: %s needed, %s free",
                                       declared, free)
                        return jsonify({
                            "status": "no_space",
                            "message": ("Not enough space on this board for a "
                                        "%s file." % format_size(declared)),
                        }), 507

                info = {
                    "transfer_id": transfer_id,
                    "first_name":  unquote(request.headers.get("X-First-Name", "")),
                    "surname":     unquote(request.headers.get("X-Surname", "")),
                    "short_name":  request.headers.get("X-Short-Name", ""),
                    "title":       request.headers.get("X-Title", ""),
                    "filename":    unquote(request.headers.get("X-Filename", "upload.bin")),
                    "size":        declared,
                }
                svc.upload_started.emit(info)
                # Register the in-flight transfer so the settings card can
                # refuse to stop the service while bytes are arriving.
                started_at = time.monotonic()
                with svc._transfer_lock:
                    svc._active_transfers[transfer_id] = {
                        **info, "received": 0, "started_at": started_at,
                    }
                safe_name = safe_incoming_filename(info["filename"])
                svc.temp_dir.mkdir(parents=True, exist_ok=True)
                tmp_path = svc.temp_dir / f"{transfer_id}_{safe_name}"
                received = 0
                # --- speed + stall tracking ---
                # Instant speed = bytes in the last ~1 s window. Stall = no
                # chunk in STALL_SECONDS → transmitter probably went offline
                # mid-transfer; we surface this without aborting (TCP itself
                # will eventually time out if it's truly gone).
                last_chunk_at = [time.monotonic()]   # mutable for the watchdog
                stalled = [False]
                stop_watchdog = threading.Event()

                def _watchdog():
                    """A separate thread is required because ``request.stream.
                    read()`` below blocks indefinitely while no data is in
                    flight — without this thread, a stall could never be
                    surfaced until TCP itself eventually drops the connection.
                    We poll cheaply (every 0.5 s) and flip ``stalled[0]`` so
                    the popup shows 'Transmitter unresponsive…'."""
                    while not stop_watchdog.wait(0.5):
                        idle = time.monotonic() - last_chunk_at[0]
                        if not stalled[0] and idle > _STALL_SECONDS:
                            stalled[0] = True
                            try:    svc.upload_stalled.emit(transfer_id, dict(info))
                            except RuntimeError: return
                wd = threading.Thread(target=_watchdog, daemon=True); wd.start()

                win_bytes = 0
                win_start = last_chunk_at[0]
                last_emit = 0.0

                # ``buffering=0`` — the stream arrives in 256 KB chunks from
                # the sender (see ``_CHUNK_SIZE`` docstring) and we hand each
                # chunk straight to the OS with one ``write(2)`` syscall.
                # Python's default binary buffer is 8 KB, so with buffering
                # left on every 256 KB chunk would be COPIED into that buffer
                # and flushed 32 times per chunk — pure overhead when the
                # chunk we're handing over is already an ideal write size.
                # Direct fd writes remove the middle-man buffer entirely.
                with open(tmp_path, "wb", buffering=0) as fp:
                    # Local aliases — LOAD_FAST is a couple of ns cheaper than
                    # LOAD_GLOBAL per hit, and this loop runs at ~130 Hz on a
                    # 30 MB/s Wi-Fi link, so it adds up over a large transfer.
                    stream_read   = request.stream.read
                    fp_write      = fp.write
                    time_monotonic = time.monotonic
                    chunk_size    = _CHUNK_SIZE
                    emit_interval = _PROGRESS_EMIT_INTERVAL
                    speed_window  = _SPEED_WINDOW
                    upload_progress_emit = svc.upload_progress.emit
                    upload_resumed_emit  = svc.upload_resumed.emit
                    xfer_lock     = svc._transfer_lock
                    active_xfers  = svc._active_transfers

                    while True:
                        chunk = stream_read(chunk_size)
                        if not chunk:
                            break
                        # The header said one thing; this is what is
                        # arriving. A sender that under-declares — or lies —
                        # must not get further than a sender that is honest,
                        # so the ceiling is enforced on real bytes as well.
                        if received + len(chunk) > _MAX_UPLOAD_BYTES:
                            raise ValueError(
                                "Upload exceeded the %s limit."
                                % format_size(_MAX_UPLOAD_BYTES))
                        fp_write(chunk)
                        now = time_monotonic()
                        n = len(chunk)
                        received  += n
                        win_bytes += n
                        last_chunk_at[0] = now
                        # Resumed after a stall — let the popup unflag itself.
                        if stalled[0]:
                            stalled[0] = False
                            upload_resumed_emit(transfer_id, dict(info))
                        # Emit progress at ~5 Hz; speed = bytes/sec over the
                        # sliding window (reset the window every ~1 s).
                        if now - last_emit >= emit_interval:
                            dt = now - win_start
                            if dt < 1e-3: dt = 1e-3
                            speed = win_bytes / dt
                            upload_progress_emit(transfer_id, received, speed)
                            with xfer_lock:
                                slot = active_xfers.get(transfer_id)
                                if slot is not None:
                                    slot["received"] = received
                                    slot["speed"]    = speed
                            last_emit = now
                            if now - win_start >= speed_window:
                                win_bytes = 0; win_start = now
                    # Flush to disk so the completed temp file is fully durable
                    # BEFORE we move it into the visible Shared Files folder.
                    # We're renaming into another dir immediately after this,
                    # which will mutate the file's metadata regardless, so
                    # ``fdatasync`` (data-only) is sufficient — no need to
                    # pay for a full ``fsync`` on Linux. On Windows the
                    # attribute doesn't exist and ``_fdatasync`` falls back
                    # to ``os.fsync`` transparently.
                    try:
                        _fdatasync(fp.fileno())
                    except OSError:
                        pass
                stop_watchdog.set()
                # The full file now exists ONLY in the temp dir. Move it into
                # Shared Files atomically-ish (shutil.move handles the
                # cross-drive case that Path.rename() can't). Until this
                # succeeds, nothing partial is ever visible in Shared Files.
                svc.shared_dir.mkdir(parents=True, exist_ok=True)
                final_path = unique_path(svc.shared_dir / safe_name)
                shutil.move(str(tmp_path), str(final_path))
                tmp_path = None
                elapsed = max(1e-3, time.monotonic() - started_at)
                info["saved_path"] = str(final_path)
                info["saved_name"] = final_path.name
                info["received"]   = received
                info["received_at"] = time.time()
                info["elapsed_s"]  = elapsed
                info["avg_speed"]  = received / elapsed
                svc.upload_finished.emit(info)
                # Activity log (file_received)
                try:
                    teacher = f"{info['first_name']} {info['surname']} {info['title']}".strip()
                    svc.activity.record_file_received(
                        teacher_name=teacher, room=svc.config.room,
                        file_name=final_path.name)
                except Exception:
                    logger.debug("activity log (file) failed", exc_info=True)
                return jsonify({"status": "ok", "saved_as": final_path.name,
                                "received": received})
            except Exception as e:
                logger.error("Upload failed:\n%s", traceback.format_exc())
                if tmp_path and tmp_path.exists():
                    try:
                        tmp_path.unlink()
                    except Exception:
                        pass
                try:
                    svc.upload_failed.emit(transfer_id, str(e))
                except RuntimeError:
                    pass
                return jsonify({"status": "error", "message": str(e)}), 500
            finally:
                # Always release the active-transfer slot, success or fail —
                # otherwise a crashed upload would lock the service forever.
                try:    stop_watchdog.set()
                except NameError: pass    # exception before watchdog created
                with svc._transfer_lock:
                    svc._active_transfers.pop(transfer_id, None)

        @app.route("/announce", methods=["POST"])
        def announce():
            blocked = _guard()
            if blocked:
                return blocked
            try:
                data = request.get_json(force=True, silent=True)
                # A JSON list is truthy, so ``... or {}`` left it in place
                # and ``data.get`` raised AttributeError — a 500 for what is
                # simply a badly-shaped request.
                if not isinstance(data, dict):
                    return jsonify({
                        "status": "error",
                        "message": "Expected a JSON object.",
                    }), 400

                # ``announcement_id`` is supplied by the teacher device so the
                # SAME logical announcement has one id everywhere. Falling
                # back to a locally-generated id keeps older LogicShare
                # clients working — they simply get an announcement nobody
                # can acknowledge, which is exactly the old behaviour.
                announcement_id = _clean_field(
                    data.get("announcement_id"), _ANNOUNCE_LIMITS["announcement_id"])
                teacher_device_id = _clean_field(
                    data.get("teacher_device_id"), _ANNOUNCE_LIMITS["teacher_device_id"])
                ack_port = data.get("ack_port")
                legacy = not (announcement_id and teacher_device_id)

                info = {
                    "id":              announcement_id or uuid4().hex,
                    "announcement_id": announcement_id,
                    "teacher_device_id": teacher_device_id,
                    "title":           _clean_field(data.get("title"),
                                                    _ANNOUNCE_LIMITS["title"]),
                    "message":         _clean_field(data.get("message"),
                                                    _ANNOUNCE_LIMITS["message"]),
                    "first_name":      _clean_field(data.get("first_name"),
                                                    _ANNOUNCE_LIMITS["first_name"]),
                    "surname":         _clean_field(data.get("surname"),
                                                    _ANNOUNCE_LIMITS["surname"]),
                    "short_name":      (_clean_field(
                                            data.get("short_name"),
                                            _ANNOUNCE_LIMITS["short_name"])
                                        or "?").upper(),
                    "title_honorific": _clean_field(
                                            data.get("title_honorific"),
                                            _ANNOUNCE_LIMITS["title_honorific"]),
                    "received_at":     time.time(),
                    # False for a legacy sender: the UI uses this to decide
                    # whether pressing OK has anywhere to report back to.
                    "ack_expected":    not legacy,
                }
                if not info["title"] or not info["message"]:
                    return jsonify({"status": "error",
                                    "message": "Title and message are required"}), 400

                # Remember where this teacher currently lives. request.remote_addr
                # is the authoritative source for that — a device cannot be
                # wrong about its own source address, whereas a self-reported
                # host in the body can be stale or behind a NAT.
                if teacher_device_id:
                    try:
                        svc._teachers.remember(
                            teacher_device_id, request.remote_addr,
                            int(ack_port or 0) or TEACHER_ACK_PORT)
                    except Exception:
                        logger.debug("Could not record teacher transport",
                                     exc_info=True)

                # Duplicate delivery of the same announcement id. The teacher
                # retrying a send it never got a response to is a NORMAL
                # event, not an error, and it must not put a second popup on
                # a classroom wall.
                dup = False
                if announcement_id:
                    with svc._seen_lock:
                        dup = announcement_id in svc._seen_announcements
                        if not dup:
                            svc._seen_announcements[announcement_id] = time.time()
                            svc._prune_seen_locked()

                logger.info(
                    "Announcement %s: id=%s from teacher=%s title=%r",
                    "DUPLICATE (already displayed)" if dup else "delivered",
                    (announcement_id or "-")[:8],
                    (teacher_device_id or "-")[:8], info["title"][:40])

                if not dup:
                    svc.announcement_received.emit(info)
                try:
                    teacher = (f"{info['first_name']} {info['surname']} "
                               f"{info['title_honorific']}").strip()
                    svc.activity.record_announcement_received(
                        teacher_name=teacher, room=svc.config.room,
                        title=info["title"], message=info["message"])
                except Exception:
                    logger.debug("activity log (announcement) failed", exc_info=True)

                # Answer with this board's STABLE identity. This is how the
                # teacher device learns which board it actually reached, so
                # its per-board report can be keyed on something that
                # survives the board changing IP.
                return jsonify({
                    "status": "ok",
                    "ack_version": ACK_PROTOCOL_VERSION,
                    "board_device_id": svc.config.device_id,
                    "board_name": svc.config.device_name,
                    "room": svc.config.room,
                    "class": svc.config.class_standard,
                    "section": svc.config.section,
                    "delivered_at": time.time(),
                    "duplicate": dup,
                })
            except Exception as e:
                logger.error("Announce failed:\n%s", traceback.format_exc())
                return jsonify({"status": "error", "message": str(e)}), 500

        @app.route("/teacher-hello", methods=["POST"])
        def teacher_hello():
            """A LogicShare device announcing its current transport address.

            This is what replaces polling. A board holding an undelivered
            acknowledgment has no way to know the teacher's laptop just came
            back — it would have to keep probing an address that was wrong
            anyway. Instead the teacher says hello when IT knows it is back,
            which is both cheaper and earlier, and the board flushes the
            queue for that device on the spot.

            Deliberately NOT guarded by ``_guard()``: a disabled board should
            still finish delivering acknowledgments it already owes for
            announcements accepted while it was enabled. Refusing here would
            strand them until the TTL killed them.
            """
            try:
                data = request.get_json(force=True, silent=True) or {}
                teacher_device_id = str(data.get("teacher_device_id") or "").strip()
                if not teacher_device_id:
                    return jsonify({"status": "error",
                                    "message": "teacher_device_id required"}), 400
                ack_port = int(data.get("ack_port") or 0) or TEACHER_ACK_PORT
                svc._teachers.remember(teacher_device_id,
                                       request.remote_addr, ack_port)
                pending = len(svc._ack_queue.for_teacher(teacher_device_id))
                logger.info("Teacher hello: %s at %s:%d (%d ack(s) queued)",
                            teacher_device_id[:8], request.remote_addr,
                            ack_port, pending)
                if pending:
                    svc._ack_dispatcher.flush_teacher(teacher_device_id)
                return jsonify({
                    "status": "ok",
                    "ack_version": ACK_PROTOCOL_VERSION,
                    "board_device_id": svc.config.device_id,
                    "board_name": svc.config.device_name,
                    "room": svc.config.room,
                    "class": svc.config.class_standard,
                    "section": svc.config.section,
                    "pending_acks": pending,
                })
            except Exception as e:
                logger.error("teacher-hello failed:\n%s", traceback.format_exc())
                return jsonify({"status": "error", "message": str(e)}), 500

        self._app = app

    def _run_flask(self) -> None:
        """Thread body: block in ``serve_forever`` on the pre-built
        werkzeug server. Returns cleanly when ``_stop_server`` calls
        ``shutdown()``."""
        srv = self._server
        if srv is None:
            return
        try:
            logging.getLogger("werkzeug").setLevel(logging.WARNING)
            srv.serve_forever()
        except Exception as e:
            logger.error("HTTP server crashed: %s", e)