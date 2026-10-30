"""
logicshare_ack.py — teacher-side announcement delivery & acknowledgment.

This is the LogicShare half of the protocol implemented in LogicBoard's
``ui/logicshare/ack.py``. Read that module's docstring for the wire format and
the reasoning behind it; this file is about what the TEACHER device does with
it.

Three responsibilities, deliberately in three classes:

    TeacherIdentity      a stable device id for this installation
    AckServer            a small HTTP listener boards POST acknowledgments to
    AnnouncementTracker  per-board state, timeouts, persistence, signals

Kept out of LogicShare.py on purpose. The tracker is a state machine with
real edge cases — duplicate acks, restarts, boards that answer late — and it
is worth being able to test it without constructing a QApplication, a window
or a LAN. LogicShare.py imports from here and owns only the presentation.

WHY A SERVER ON THE TEACHER DEVICE
==================================
An acknowledgment travels board -> teacher, which is the opposite direction to
everything else in the product. The alternative would be the teacher polling
every board it ever sent to, forever, on the chance one of them has news. That
is O(boards x forever) requests to learn something that happens once. A tiny
listener costs one socket and gets the answer the instant it exists.

WHAT "READ" MEANS HERE
======================
Only an acknowledgment POST moves a board to READ. Not a successful send, not
an HTTP 200 from /announce, not a timer. A 200 means the board's process
accepted the bytes; it says nothing about whether a human in that room looked
at the wall. Conflating them would make the whole report a lie in exactly the
case a teacher cares about — the class that wasn't there.
"""
from __future__ import annotations

import json
import logging
import os
import socket
import tempfile
import threading
import time
from dataclasses import dataclass, field, asdict
from pathlib import Path
from typing import Optional

log = logging.getLogger("LogicShare")

__all__ = [
    "ACK_PROTOCOL_VERSION", "TEACHER_ACK_PORT", "ACK_TIMEOUT_SECONDS",
    "BoardState", "BoardStatus", "Announcement",
    "TeacherIdentity", "AckServer", "AnnouncementTracker",
]

# ---------------------------------------------------------------------------
# Protocol — must match LogicBoard/ui/logicshare/ack.py
# ---------------------------------------------------------------------------
ACK_PROTOCOL_VERSION = 1
TEACHER_ACK_PORT = 16385

# How long a delivered announcement may sit unacknowledged before the teacher
# is told. Five minutes: long enough that a class mid-activity isn't reported
# as unresponsive, short enough that a teacher who needs to chase it up still
# has the lesson to do it in.
ACK_TIMEOUT_SECONDS = 5 * 60

# How often the tracker re-examines announcements for elapsed timeouts.
# A sweep rather than one timer per board: a teacher with 30 boards would
# otherwise be holding 30 QTimers whose only job is to fire once.
_SWEEP_INTERVAL_SECONDS = 15

# Announcements older than this stop being swept. They keep their history
# entry — the teacher can still read the report — but nothing further will
# change, so there is no reason to keep examining them. Matches LogicBoard's
# acknowledgment TTL, because after that no board will ever send one.
ACK_GIVE_UP_SECONDS = 8 * 60 * 60


class BoardState:
    """Per-board lifecycle within ONE announcement.

    A deliberately richer set than "read / not read". The states a teacher
    needs to tell apart are *why* something isn't read yet, and a boolean
    cannot express the difference between "the board never got it", "the
    board has it and nobody has looked", and "we waited long enough".
    """
    PENDING = "pending"        # send attempted, no result yet
    DELIVERED = "delivered"    # board accepted it; nobody has pressed OK
    READ = "read"              # a human pressed OK — the only real success
    OFFLINE = "offline"        # could not be reached at send time
    TIMEOUT = "timeout"        # delivered, but 5 minutes passed with no ack
    EXPIRED = "expired"        # past the point any ack could still arrive

    #: Order used when summarising: worst news first, because that is what a
    #: teacher opening the report needs to see.
    SEVERITY = {OFFLINE: 0, TIMEOUT: 1, EXPIRED: 2, PENDING: 3,
                DELIVERED: 4, READ: 5}

    #: States that still might become READ. Used to decide whether an
    #: announcement is worth sweeping at all.
    OPEN = {PENDING, DELIVERED, TIMEOUT}


@dataclass
class BoardStatus:
    """One board's state within one announcement."""

    # Identity. board_device_id is authoritative once known; ip is only a
    # transport address and may be stale by the time the ack arrives.
    board_device_id: str = ""
    ip: str = ""
    board_name: str = ""
    room: str = ""
    class_standard: str = ""
    section: str = ""

    state: str = BoardState.PENDING
    detail: str = ""

    # Lifecycle timestamps. All optional — a board that was never reached has
    # no delivered_at, and one that was never acknowledged has no read_at.
    delivered_at: Optional[float] = None
    read_at: Optional[float] = None
    timeout_at: Optional[float] = None
    expired_at: Optional[float] = None
    board_version: str = ""

    @property
    def key(self) -> str:
        """Stable-ish identity for matching.

        Prefers the device id. Falls back to IP only for a board too old to
        report one — in which case the report degrades to whatever that IP
        was, which is the best that can be known about it.
        """
        return self.board_device_id or f"ip:{self.ip}"

    @property
    def label(self) -> str:
        """Human name for the report, best available."""
        bits = []
        if self.class_standard:
            bits.append(f"{self.class_standard}{self.section or ''}")
        if self.room:
            bits.append(f"Room {self.room}")
        if bits:
            return "  ·  ".join(bits)
        return self.board_name or self.ip or "Unknown board"


@dataclass
class Announcement:
    """One announcement and every board it was sent to."""

    announcement_id: str
    title: str
    message: str
    sent_at: float
    boards: dict = field(default_factory=dict)   # key -> BoardStatus
    # Set once the teacher has been told about a timeout, so they are told
    # exactly once per announcement rather than every sweep.
    timeout_notified_at: Optional[float] = None

    # ---- derived views ----
    def counts(self) -> dict:
        out = {}
        for b in self.boards.values():
            out[b.state] = out.get(b.state, 0) + 1
        return out

    def unacknowledged(self) -> list:
        """Boards that have not been read, worst news first."""
        pending = [b for b in self.boards.values() if b.state != BoardState.READ]
        return sorted(pending, key=lambda b: (BoardState.SEVERITY.get(b.state, 9),
                                              b.label))

    def is_open(self) -> bool:
        """Could anything still change?"""
        if (time.time() - self.sent_at) > ACK_GIVE_UP_SECONDS:
            return False
        return any(b.state in BoardState.OPEN for b in self.boards.values())

    def all_read(self) -> bool:
        return bool(self.boards) and all(
            b.state == BoardState.READ for b in self.boards.values())

    def to_dict(self) -> dict:
        return {
            "announcement_id": self.announcement_id,
            "title": self.title,
            "message": self.message,
            "sent_at": self.sent_at,
            "timeout_notified_at": self.timeout_notified_at,
            "boards": {k: asdict(v) for k, v in self.boards.items()},
        }

    @classmethod
    def from_dict(cls, d: dict) -> "Announcement":
        boards = {}
        for k, bv in (d.get("boards") or {}).items():
            try:
                boards[k] = BoardStatus(**bv)
            except (TypeError, ValueError):
                continue
        return cls(
            announcement_id=str(d.get("announcement_id") or ""),
            title=str(d.get("title") or ""),
            message=str(d.get("message") or ""),
            sent_at=float(d.get("sent_at") or 0.0),
            boards=boards,
            timeout_notified_at=d.get("timeout_notified_at"),
        )


# ---------------------------------------------------------------------------
# Atomic JSON
# ---------------------------------------------------------------------------
def _atomic_write_json(path: Path, data) -> bool:
    path = Path(path)
    tmp_path = None
    try:
        path.parent.mkdir(parents=True, exist_ok=True)
        fd, tmp_name = tempfile.mkstemp(prefix=path.stem + "_", suffix=".tmp",
                                        dir=str(path.parent))
        tmp_path = Path(tmp_name)
        with os.fdopen(fd, "w", encoding="utf-8") as f:
            json.dump(data, f, indent=2, ensure_ascii=False)
            f.flush()
            os.fsync(f.fileno())
        os.replace(tmp_path, path)
        return True
    except Exception:
        log.exception(f"Atomic write failed for {path}")
        if tmp_path is not None:
            try:
                if tmp_path.exists():
                    tmp_path.unlink()
            except OSError:
                pass
        return False


def _read_json(path: Path, default):
    try:
        p = Path(path)
        if not p.exists():
            return default
        return json.loads(p.read_text(encoding="utf-8"))
    except Exception:
        log.warning(f"Could not read {path}; starting fresh", exc_info=True)
        return default


# ---------------------------------------------------------------------------
# Identity
# ---------------------------------------------------------------------------
class TeacherIdentity:
    """This LogicShare installation's stable device id.

    Generated once and persisted. It is what boards address acknowledgments
    to, so regenerating it would orphan every acknowledgment still queued on
    every board — they would be addressed to a device that no longer exists.
    """

    def __init__(self, path: Path):
        self._path = Path(path)
        data = _read_json(self._path, {})
        if not isinstance(data, dict):
            data = {}
        self.device_id = str(data.get("device_id") or "")
        if not self.device_id:
            from uuid import uuid4
            self.device_id = "td_" + uuid4().hex[:16]
            _atomic_write_json(self._path, {"device_id": self.device_id,
                                            "created_at": time.time()})
            log.info(f"Generated stable teacher device_id: {self.device_id}")
        else:
            log.info(f"Teacher device_id: {self.device_id}")


# ---------------------------------------------------------------------------
# The listener
# ---------------------------------------------------------------------------
class AckServer:
    """Small HTTP server that receives acknowledgments from boards.

    Runs for the whole life of the app, not just while a report window is
    open — a board may acknowledge at any moment, and requiring the teacher
    to keep a window up to hear it would make the feature useless in the one
    situation it is for.
    """

    def __init__(self, tracker: "AnnouncementTracker",
                 preferred_port: int = TEACHER_ACK_PORT):
        self._tracker = tracker
        self._preferred_port = preferred_port
        self._srv = None
        self._thread: Optional[threading.Thread] = None
        self.port: int = 0

    def start(self) -> bool:
        if self._srv is not None:
            return True
        from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

        tracker = self._tracker

        class Handler(BaseHTTPRequestHandler):
            protocol_version = "HTTP/1.1"

            def log_message(self, *a):
                pass          # the app has its own logging

            def _reply(self, code: int, payload: dict):
                body = json.dumps(payload).encode("utf-8")
                self.send_response(code)
                self.send_header("Content-Type", "application/json")
                self.send_header("Content-Length", str(len(body)))
                self.end_headers()
                self.wfile.write(body)

            def do_GET(self):
                # A board (or a curious human) can check this device is alive.
                if self.path.rstrip("/") in ("/ping", ""):
                    self._reply(200, {"status": "ok",
                                      "role": "logicshare-teacher",
                                      "ack_version": ACK_PROTOCOL_VERSION,
                                      "teacher_device_id": tracker.device_id})
                else:
                    self._reply(404, {"status": "error"})

            def do_POST(self):
                if self.path.rstrip("/") != "/ack":
                    self._reply(404, {"status": "error"})
                    return
                try:
                    n = int(self.headers.get("Content-Length", 0) or 0)
                    raw = self.rfile.read(n) if n else b"{}"
                    payload = json.loads(raw.decode("utf-8"))
                except Exception:
                    log.warning("Malformed acknowledgment body", exc_info=True)
                    self._reply(400, {"status": "error",
                                      "message": "bad json"})
                    return
                # Stamp the peer address. The SOCKET knows who connected;
                # the body only claims to. Boards do not send their own ip
                # (see LogicBoard's to_wire), so without this the row-matching
                # fallback in record_ack had nothing to match on and adopted
                # the first row that happened to be missing a device id —
                # crediting one classroom's acknowledgment to another, and
                # relabelling that row with the wrong room number on the way
                # through. Overwrites anything in the body on purpose: a peer
                # does not get to choose which row it lands on.
                try:
                    peer = (self.client_address[0]
                            if self.client_address else "")
                except Exception:
                    peer = ""
                if peer:
                    # A dual-stack listener reports IPv4 peers in the mapped
                    # form ::ffff:10.0.0.7, which would never string-match the
                    # 10.0.0.7 we sent to.
                    if peer.startswith("::ffff:"):
                        peer = peer[7:]
                    payload["ip"] = peer
                try:
                    accepted = tracker.record_ack(payload)
                except Exception:
                    log.exception("record_ack failed")
                    # 500 so the board RETRIES. Answering 200 on an internal
                    # failure would make the board drop an acknowledgment
                    # this device never actually stored.
                    self._reply(500, {"status": "error"})
                    return
                # 200 even when the ack was a duplicate or for an unknown
                # announcement: in both cases the board has done its job and
                # should stop retrying. Anything else leaves it retrying for
                # eight hours over something this device will never want.
                self._reply(200, {"status": "ok", "accepted": bool(accepted)})

        class _ExclusiveHTTPServer(ThreadingHTTPServer):
            """HTTP server that refuses to share its port.

            ``HTTPServer`` sets ``allow_reuse_address = True``, which sets
            SO_REUSEADDR — and SO_REUSEADDR does NOT mean the same thing on
            Windows as it does on Unix. On Unix it lets a restarting server
            rebind a port still in TIME_WAIT. On Windows it lets a SECOND
            live socket bind an address another socket is already listening
            on, and the two then split incoming connections unpredictably.

            Measured: two AckServers asked for the same port and BOTH
            reported success. Two LogicShare instances would each believe
            they owned 16385, and a board's acknowledgment would arrive at
            whichever socket happened to win — so a teacher could watch a
            board stay amber forever while the ack was delivered to another
            copy of the app.

            Turning it off makes the second bind fail properly, which is what
            drives the fallback to the next port below.
            """
            allow_reuse_address = False

        last_err = None
        # Try the agreed port first, then a few above it. A second LogicShare
        # on the same machine (or any unrelated process holding 16385) must
        # not stop this one from receiving acknowledgments — every
        # announcement carries the port actually in use, so a non-default one
        # still works end to end.
        for offset in range(0, 12):
            port = self._preferred_port + offset
            try:
                srv = _ExclusiveHTTPServer(("0.0.0.0", port), Handler)
            except OSError as e:
                last_err = e
                continue
            srv.daemon_threads = True
            self._srv = srv
            self.port = port
            self._thread = threading.Thread(target=srv.serve_forever,
                                            daemon=True,
                                            name="LogicShare-AckServer")
            self._thread.start()
            if offset:
                log.warning(f"Ack port {self._preferred_port} unavailable; "
                            f"listening on {port} instead")
            log.info(f"Acknowledgment listener on 0.0.0.0:{port}")
            return True
        log.error(f"Could not bind an acknowledgment port: {last_err}")
        return False

    def stop(self) -> None:
        srv = self._srv
        self._srv = None
        if srv is not None:
            try:
                srv.shutdown()
                srv.server_close()
            except Exception:
                pass
        t = self._thread
        if t is not None and t.is_alive():
            t.join(timeout=2.0)
        self._thread = None


# ---------------------------------------------------------------------------
# The tracker
# ---------------------------------------------------------------------------
try:
    from PySide6.QtCore import QObject, Signal, QTimer
    _HAS_QT = True
except Exception:                                    # pragma: no cover
    _HAS_QT = False

    class QObject:                                   # type: ignore
        def __init__(self, *a, **k): pass

    class _Sig:
        def __init__(self, *a): self._subs = []
        def connect(self, fn): self._subs.append(fn)
        def emit(self, *a):
            for fn in list(self._subs):
                fn(*a)

    def Signal(*a, **k):                             # type: ignore
        return _Sig()


class AnnouncementTracker(QObject):
    """Per-board announcement state, timeouts and persistence.

    Thread-safety matters here: acknowledgments arrive on AckServer worker
    threads while the UI reads the same structures on the GUI thread. All
    mutation happens under one lock, and everything the UI is told arrives as
    a Qt signal so it lands on the GUI thread properly.
    """

    # announcement_id — something about this announcement changed
    status_changed = Signal(str)
    # announcement_id, list[BoardStatus] — five minutes elapsed, tell the user
    timeout_reached = Signal(str, list)

    def __init__(self, path: Path, device_id: str, parent=None):
        super().__init__(parent)
        self._path = Path(path)
        self.device_id = device_id
        self._lock = threading.RLock()
        self._items: dict = {}
        self._load()
        self._sweep_timer = None
        if _HAS_QT:
            # A single repeating sweep, owned by the tracker. Timers must be
            # created on the thread that runs them, which is why this is here
            # and not in whatever thread happens to call start_sweeping().
            self._sweep_timer = QTimer(self)
            self._sweep_timer.setInterval(_SWEEP_INTERVAL_SECONDS * 1000)
            self._sweep_timer.timeout.connect(self.sweep)

    # ---- lifecycle ----
    def start_sweeping(self) -> None:
        if self._sweep_timer is not None and not self._sweep_timer.isActive():
            self._sweep_timer.start()
            log.info("Announcement timeout sweep started "
                     f"(every {_SWEEP_INTERVAL_SECONDS}s, "
                     f"timeout {ACK_TIMEOUT_SECONDS // 60} min)")
        # Catch anything that timed out while the app was closed.
        self.sweep()

    def stop_sweeping(self) -> None:
        if self._sweep_timer is not None and self._sweep_timer.isActive():
            self._sweep_timer.stop()

    # ---- persistence ----
    def _load(self) -> None:
        raw = _read_json(self._path, [])
        if not isinstance(raw, list):
            raw = []
        for d in raw:
            try:
                ann = Announcement.from_dict(d)
            except Exception:
                continue
            if ann.announcement_id:
                self._items[ann.announcement_id] = ann
        if self._items:
            log.info(f"Restored {len(self._items)} tracked announcement(s)")

    def _save_locked(self) -> None:
        # Newest first, and bounded — the report is a working record, not an
        # archive, and an unbounded file would eventually make every save
        # cost more than the thing it is recording.
        items = sorted(self._items.values(), key=lambda a: -a.sent_at)[:200]
        self._items = {a.announcement_id: a for a in items}
        _atomic_write_json(self._path, [a.to_dict() for a in items])

    # ---- queries ----
    def get(self, announcement_id: str) -> Optional[Announcement]:
        with self._lock:
            return self._items.get(announcement_id)

    def all(self) -> list:
        with self._lock:
            return sorted(self._items.values(), key=lambda a: -a.sent_at)

    # ---- mutations ----
    def record_sent(self, announcement_id: str, title: str, message: str,
                    targets: list) -> Announcement:
        """Register an announcement and the boards it is going to.

        Called BEFORE delivery is attempted, so a send that dies half way
        still leaves a report showing which boards were meant to receive it.
        """
        with self._lock:
            boards = {}
            for t in targets:
                st = BoardStatus(
                    board_device_id=str(t.get("board_device_id") or ""),
                    ip=str(t.get("ip") or ""),
                    board_name=str(t.get("device") or t.get("board_name") or ""),
                    room=str(t.get("room") or ""),
                    class_standard=str(t.get("class") or t.get("class_standard") or ""),
                    section=str(t.get("section") or ""),
                    state=BoardState.PENDING,
                )
                boards[st.key] = st
            ann = Announcement(
                announcement_id=announcement_id,
                title=title, message=message,
                sent_at=time.time(), boards=boards,
            )
            self._items[announcement_id] = ann
            self._save_locked()
        log.info(f"Announcement sent: id={announcement_id[:8]} "
                 f"title={title[:40]!r} boards={len(targets)}")
        self.status_changed.emit(announcement_id)
        return ann

    def record_delivery(self, announcement_id: str, ip: str, ok: bool,
                        detail: str = "", board_info: dict = None) -> None:
        """Result of the /announce POST to one board.

        ``board_info`` is the board's reply, which is where its stable device
        id comes from. Learning that at delivery time is what lets a later
        acknowledgment — arriving from a possibly different IP — be matched
        to the right row.
        """
        board_info = board_info or {}
        with self._lock:
            ann = self._items.get(announcement_id)
            if ann is None:
                return
            # Find by IP, since that is all we knew when the send started.
            st = None
            for b in ann.boards.values():
                if b.ip == ip:
                    st = b
                    break
            if st is None:
                return

            new_id = str(board_info.get("board_device_id") or "")
            if new_id and new_id != st.board_device_id:
                # Re-key: the row was stored under "ip:..." and now has a
                # real identity. Without this the acknowledgment, which
                # arrives keyed by device id, would not find its row.
                ann.boards.pop(st.key, None)
                st.board_device_id = new_id
                ann.boards[st.key] = st
            for src, dst in (("board_name", "board_name"), ("room", "room"),
                             ("class", "class_standard"), ("section", "section")):
                v = board_info.get(src)
                if v:
                    setattr(st, dst, str(v))

            if ok:
                # Never downgrade a board that has ALREADY been read. A
                # duplicate send whose 200 arrives after the acknowledgment
                # must not turn a green row amber again.
                if st.state != BoardState.READ:
                    st.state = BoardState.DELIVERED
                    st.delivered_at = time.time()
                st.detail = detail or "Delivered"
            else:
                if st.state != BoardState.READ:
                    st.state = BoardState.OFFLINE
                st.detail = detail or "Unreachable"
            self._save_locked()
        log.info(f"Announcement {announcement_id[:8]} -> {ip}: "
                 f"{'delivered' if ok else 'FAILED'} ({detail})")
        self.status_changed.emit(announcement_id)

    def record_ack(self, payload: dict) -> bool:
        """Handle an acknowledgment POSTed by a board.

        Returns True if it moved something, False if it was a duplicate or
        for an announcement this device doesn't know. Either way the caller
        answers 200 — see AckServer for why.
        """
        announcement_id = str(payload.get("announcement_id") or "").strip()
        board_device_id = str(payload.get("board_device_id") or "").strip()
        teacher_device_id = str(payload.get("teacher_device_id") or "").strip()
        if not announcement_id or not board_device_id:
            log.warning("Acknowledgment missing ids; ignoring")
            return False

        # Announcements are addressed. One addressed to a different teacher
        # device arriving here means a board has a stale registry entry, and
        # accepting it would put someone else's data in this teacher's report.
        if teacher_device_id and teacher_device_id != self.device_id:
            log.warning(f"Acknowledgment for teacher {teacher_device_id[:8]} "
                        f"arrived at {self.device_id[:8]}; ignoring")
            return False

        with self._lock:
            ann = self._items.get(announcement_id)
            if ann is None:
                log.info(f"Acknowledgment for unknown announcement "
                         f"{announcement_id[:8]} (cleared history?) — ignoring")
                return False

            st = ann.boards.get(board_device_id)
            if st is None:
                # The row may still be keyed by IP: this board was too old to
                # report an id at delivery time, or the delivery response was
                # lost before we learned one. Adopt it by IP — the address the
                # acknowledgment actually arrived from, stamped by AckServer.
                #
                # The IP must MATCH. This used to read
                # ``(not ip or b.ip == ip)``, and since nothing ever supplied
                # an ip the left half was always true: the first row missing a
                # device id got adopted no matter which board was speaking. On
                # a send where several deliveries went unanswered that credits
                # the wrong classroom AND overwrites its room number with the
                # acknowledging board's, so the report showed one room twice
                # and lost another entirely.
                #
                # With no ip to match on we now adopt nothing and fall through
                # to adding a row below. An extra row is visibly odd; a
                # wrongly-credited one is invisibly wrong.
                ip = str(payload.get("ip") or "")
                if ip:
                    for b in list(ann.boards.values()):
                        if not b.board_device_id and b.ip == ip:
                            ann.boards.pop(b.key, None)
                            b.board_device_id = board_device_id
                            ann.boards[b.key] = b
                            st = b
                            break
            if st is None:
                # A board acknowledging something it was never sent. Record
                # it rather than dropping it — it is real, and hiding it
                # would make the report quietly wrong.
                st = BoardStatus(board_device_id=board_device_id)
                ann.boards[st.key] = st
                log.warning(f"Acknowledgment from board {board_device_id[:8]} "
                            f"not in the original target list; added")

            if st.state == BoardState.READ:
                log.info(f"Duplicate acknowledgment ignored: "
                         f"announcement={announcement_id[:8]} "
                         f"board={board_device_id[:8]}")
                return False

            st.state = BoardState.READ
            # Coerced defensively. This value comes off the network, and a
            # board sending a malformed one must not be able to raise inside
            # the handler: the AckServer would answer 500, the board would
            # retry for eight hours, and the acknowledgment would never be
            # recorded — a hostile or simply buggy peer could keep one board
            # amber forever. An unusable timestamp falls back to "now", which
            # is at worst a few seconds off and always sane.
            try:
                st.read_at = float(payload.get("acknowledged_at") or time.time())
            except (TypeError, ValueError):
                log.warning("Acknowledgment had an unusable timestamp "
                            f"({payload.get('acknowledged_at')!r}); using now")
                st.read_at = time.time()
            st.detail = "Read"
            for src, dst in (("board_name", "board_name"), ("room", "room"),
                             ("class", "class_standard"),
                             ("section", "section"),
                             ("board_version", "board_version")):
                v = payload.get(src)
                # Only accept scalars. A nested dict or list would str() into
                # something like "{'nested': 'dict'}" and be rendered straight
                # into the teacher's report.
                if v and isinstance(v, (str, int, float)):
                    setattr(st, dst, str(v)[:80])
            self._save_locked()
            remaining = len([b for b in ann.boards.values()
                             if b.state != BoardState.READ])

        log.info(f"Acknowledgment received: announcement={announcement_id[:8]} "
                 f"board={board_device_id[:8]} ({remaining} board(s) still "
                 f"unacknowledged)")
        self.status_changed.emit(announcement_id)
        return True

    # ---- timeouts ----
    def sweep(self) -> None:
        """Move elapsed announcements to TIMEOUT / EXPIRED and notify once."""
        now = time.time()
        to_notify = []
        changed = set()
        with self._lock:
            for ann in self._items.values():
                age = now - ann.sent_at
                if age < ACK_TIMEOUT_SECONDS:
                    continue
                dirty = False
                for b in ann.boards.values():
                    if b.state in (BoardState.DELIVERED, BoardState.PENDING):
                        b.state = BoardState.TIMEOUT
                        b.timeout_at = now
                        b.detail = "No acknowledgment yet"
                        dirty = True
                    if (age >= ACK_GIVE_UP_SECONDS
                            and b.state in (BoardState.TIMEOUT,
                                            BoardState.OFFLINE)):
                        b.state = BoardState.EXPIRED
                        b.expired_at = now
                        b.detail = "Expired — no acknowledgment"
                        dirty = True
                if dirty:
                    changed.add(ann.announcement_id)
                # Notify once per announcement, and only if something is
                # actually outstanding. An announcement every board read
                # inside five minutes should never produce a warning.
                if ann.timeout_notified_at is None:
                    outstanding = [b for b in ann.boards.values()
                                   if b.state != BoardState.READ]
                    if outstanding:
                        ann.timeout_notified_at = now
                        to_notify.append((ann.announcement_id,
                                          list(outstanding)))
                        dirty = True
                    else:
                        # Fully acknowledged in time — mark it handled so it
                        # is never reconsidered.
                        ann.timeout_notified_at = now
                        dirty = True
                if dirty:
                    changed.add(ann.announcement_id)
            if changed:
                self._save_locked()

        for aid in changed:
            self.status_changed.emit(aid)
        for aid, boards in to_notify:
            log.warning(f"Acknowledgment TIMEOUT ({ACK_TIMEOUT_SECONDS // 60} "
                        f"min): announcement={aid[:8]} "
                        f"{len(boards)} board(s) have not acknowledged")
            self.timeout_reached.emit(aid, boards)

    # ---- maintenance ----
    def forget(self, announcement_id: str) -> None:
        with self._lock:
            self._items.pop(announcement_id, None)
            self._save_locked()

    def clear(self) -> None:
        with self._lock:
            self._items.clear()
            self._save_locked()
