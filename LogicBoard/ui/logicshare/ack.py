"""
ack.py — announcement acknowledgment: state, persistence, retry, delivery.

WHAT THIS SOLVES
================
A teacher sends an announcement to several boards and needs to know which ones
a human actually read — not which ones the TCP write succeeded on. Those are
different questions, and the gap between them is the whole point of this
module: delivery is a network fact, acknowledgment is a human one.

THE PROTOCOL, and why it is shaped this way
===========================================
Nothing new is invented at the transport layer. The existing LogicShare LAN
architecture already has everything needed:

  * boards advertise over Zeroconf and serve HTTP on PORT,
  * teacher devices discover boards and POST to ``/announce``.

So acknowledgment is added as three small, backward-compatible extensions:

  1. ``POST /announce`` gains ``announcement_id``, ``teacher_device_id`` and
     ``ack_port``. Its RESPONSE gains ``board_device_id`` and identity fields,
     which is how the teacher learns the stable id of the board it just
     reached. An old teacher talking to a new board simply omits the new
     fields; an old board answering a new teacher omits them in the reply.
     Neither combination breaks — the feature just degrades to "delivered".

  2. ``POST /ack`` on the TEACHER device carries the acknowledgment back.

  3. ``POST /teacher-hello`` lets a teacher device announce its current
     transport address to a board when it (re)joins the LAN.

That third one deserves a note, because the obvious alternative is worse.
A board could poll for the teacher's return, or the teacher could register a
Zeroconf service the board browses for. Polling burns battery and LAN chatter
for an event that happens a few times a day; Zeroconf registration is not
available to the Android client at all (``multicast_dns`` browses but cannot
register). A hello POST costs one request at the moment connectivity is
actually regained, works identically on Windows and Android, and gives the
board a fresh address AND a reason to flush its queue in the same message.

IDENTITY
========
Everything is keyed on stable device ids, never IP addresses — LAN addresses
change when a board hops APs or a DHCP lease turns over, and an acknowledgment
routed by IP after that lands on the wrong device or nowhere. An IP is only
ever a *current transport address*, refreshed from whatever message last
arrived from that device.

An acknowledgment's identity is ``(announcement_id, board_device_id)``. That
pair is what makes redelivery safe: a duplicate ack overwrites its own record
rather than creating a second one, so retries, reconnects and restarts can all
re-send freely without corrupting the teacher's report.

DURABILITY
==========
The queue is on disk. A board that is switched off between "user pressed OK"
and "teacher came back" must still deliver that acknowledgment when it next
boots, or the guarantee is worthless. Writes are atomic (temp file + replace)
so a power cut mid-write cannot leave a truncated queue behind.
"""
from __future__ import annotations

import json
import logging
import os
import tempfile
import threading
import time
from dataclasses import dataclass, field, asdict
from pathlib import Path
from typing import Callable, Iterable, Optional

logger = logging.getLogger(__name__)

__all__ = [
    "ACK_PROTOCOL_VERSION", "TEACHER_ACK_PORT",
    "AckState", "AckRecord", "AckQueue", "TeacherRegistry", "AckDispatcher",
]

# ---------------------------------------------------------------------------
# Protocol constants — must stay in step with the LogicShare clients.
# ---------------------------------------------------------------------------
ACK_PROTOCOL_VERSION = 1

# Default port a LogicShare teacher device listens on for acknowledgments.
# Only a DEFAULT: every announcement carries the sender's real ``ack_port``,
# because a teacher machine may already have this port taken and fall back to
# another. The constant exists so a board with a queued ack from before this
# field existed still has something sane to try.
TEACHER_ACK_PORT = 16385

# Retry schedule for an acknowledgment that could not be delivered.
#
# Exponential with a ceiling. The first retries are quick because the common
# failure is a few seconds of Wi-Fi roaming, but a teacher who has gone home
# for the night should not be probed every 15 seconds for eight hours — that
# is thousands of pointless connection attempts. Capping at 5 minutes bounds
# the whole 8-hour window to about a hundred.
_RETRY_BASE_SECONDS = 15
_RETRY_MAX_SECONDS = 300

# How long a queued acknowledgment is worth retrying before it is abandoned.
# Eight hours covers "sent at the end of one school day, teacher's laptop
# reappears the next morning" without keeping records alive indefinitely.
ACK_TTL_SECONDS = 8 * 60 * 60


class AckState:
    """Lifecycle states an acknowledgment record can hold on the BOARD.

    Deliberately a small set of strings rather than an enum: these values are
    written to disk and sent over the wire, and a plain string survives a
    version change on either side without a translation table.
    """
    PENDING = "pending"      # user pressed OK; not yet delivered to teacher
    SENT = "sent"            # teacher accepted it; record can be dropped
    EXPIRED = "expired"      # TTL passed without successful delivery


@dataclass
class AckRecord:
    """One acknowledgment awaiting delivery to one teacher device."""

    announcement_id: str
    board_device_id: str
    teacher_device_id: str
    acknowledged_at: float
    # Transport hints. Refreshed whenever the teacher is seen again; never
    # used as identity.
    teacher_host: str = ""
    teacher_port: int = TEACHER_ACK_PORT
    # Board identity, carried so the teacher can render a useful report even
    # if the board is offline by the time the ack lands.
    board_name: str = ""
    room: str = ""
    class_standard: str = ""
    section: str = ""
    board_version: str = ""
    # Retry bookkeeping.
    attempts: int = 0
    last_attempt_at: float = 0.0
    next_attempt_at: float = 0.0
    state: str = AckState.PENDING

    @property
    def key(self) -> tuple:
        """Identity of this acknowledgment.

        announcement + board, NOT a random row id. Two records with the same
        pair are the same fact, however many times it was queued.
        """
        return (self.announcement_id, self.board_device_id)

    def is_expired(self, now: Optional[float] = None) -> bool:
        now = time.time() if now is None else now
        return (now - self.acknowledged_at) >= ACK_TTL_SECONDS

    def due(self, now: Optional[float] = None) -> bool:
        now = time.time() if now is None else now
        return self.state == AckState.PENDING and now >= self.next_attempt_at

    def schedule_retry(self, now: Optional[float] = None) -> None:
        now = time.time() if now is None else now
        self.attempts += 1
        self.last_attempt_at = now
        delay = min(_RETRY_BASE_SECONDS * (2 ** max(0, self.attempts - 1)),
                    _RETRY_MAX_SECONDS)
        self.next_attempt_at = now + delay

    def to_wire(self) -> dict:
        """The JSON body POSTed to the teacher's ``/ack`` endpoint."""
        return {
            "ack_version": ACK_PROTOCOL_VERSION,
            "announcement_id": self.announcement_id,
            "board_device_id": self.board_device_id,
            "teacher_device_id": self.teacher_device_id,
            "state": "read",
            "acknowledged_at": self.acknowledged_at,
            "board_name": self.board_name,
            "room": self.room,
            "class": self.class_standard,
            "section": self.section,
            "board_version": self.board_version,
        }


# ---------------------------------------------------------------------------
# Atomic JSON helpers
# ---------------------------------------------------------------------------
def _atomic_write_json(path: Path, data) -> bool:
    """Write JSON so the file is never observed half-written.

    Temp file in the SAME directory (so os.replace is an atomic same-volume
    rename), flush + fsync, then replace. A crash leaves either the old file
    or the new one, never a truncated mixture — which matters because this
    file is the only record that an acknowledgment is owed.
    """
    path = Path(path)
    tmp_path = None
    try:
        path.parent.mkdir(parents=True, exist_ok=True)
        fd, tmp_name = tempfile.mkstemp(prefix=path.stem + "_", suffix=".tmp",
                                        dir=str(path.parent))
        tmp_path = Path(tmp_name)
        with os.fdopen(fd, "w", encoding="utf-8") as f:
            json.dump(data, f, indent=2)
            f.flush()
            os.fsync(f.fileno())
        os.replace(tmp_path, path)
        return True
    except Exception:
        logger.exception("Atomic write failed for %s", path)
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
        logger.warning("Could not read %s; starting fresh", path, exc_info=True)
        return default


# ---------------------------------------------------------------------------
# Teacher transport registry
# ---------------------------------------------------------------------------
class TeacherRegistry:
    """Stable teacher device id -> last known transport address.

    Exists because acknowledgment routing must survive the teacher's IP
    changing. The board learns an address from any message that arrives from
    that teacher (an announcement, or a hello) and remembers it; when the ack
    needs sending, the address is looked up by DEVICE ID.
    """

    def __init__(self, path: Path):
        self._path = Path(path)
        self._lock = threading.RLock()
        raw = _read_json(self._path, {})
        self._devices: dict = raw if isinstance(raw, dict) else {}

    def remember(self, device_id: str, host: str, port: int) -> None:
        if not device_id or not host:
            return
        with self._lock:
            prev = self._devices.get(device_id) or {}
            changed = (prev.get("host") != host or prev.get("port") != port)
            self._devices[device_id] = {
                "host": host,
                "port": int(port or TEACHER_ACK_PORT),
                "last_seen": time.time(),
            }
            self._save_locked()
        if changed:
            logger.info("Teacher transport updated: %s -> %s:%s",
                        _short(device_id), host, port)

    def lookup(self, device_id: str) -> Optional[dict]:
        with self._lock:
            entry = self._devices.get(device_id)
            return dict(entry) if entry else None

    def _save_locked(self) -> None:
        _atomic_write_json(self._path, self._devices)


# ---------------------------------------------------------------------------
# The queue
# ---------------------------------------------------------------------------
class AckQueue:
    """Durable set of acknowledgments owed to teacher devices.

    Keyed by (announcement_id, board_device_id), so re-queuing the same
    acknowledgment — which happens whenever a user taps OK on something
    already acknowledged, or a restart replays state — updates the existing
    record instead of creating a duplicate the teacher would have to
    deduplicate later.
    """

    def __init__(self, path: Path):
        self._path = Path(path)
        self._lock = threading.RLock()
        self._records: dict = {}
        self._load()

    # ---- persistence ----
    def _load(self) -> None:
        raw = _read_json(self._path, [])
        if not isinstance(raw, list):
            raw = []
        loaded, dropped = 0, 0
        for item in raw:
            try:
                rec = AckRecord(**item)
            except (TypeError, ValueError):
                dropped += 1
                continue
            if rec.state == AckState.SENT:
                continue                      # nothing left to do
            if rec.is_expired():
                dropped += 1
                continue
            self._records[rec.key] = rec
            loaded += 1
        if loaded or dropped:
            logger.info("Ack queue restored: %d pending, %d dropped "
                        "(expired or unreadable)", loaded, dropped)

    def _save_locked(self) -> None:
        _atomic_write_json(self._path,
                           [asdict(r) for r in self._records.values()])

    # ---- mutations ----
    def enqueue(self, rec: AckRecord) -> AckRecord:
        """Add or refresh an acknowledgment. Returns the stored record."""
        with self._lock:
            existing = self._records.get(rec.key)
            if existing is not None:
                # Same fact, seen again. Keep the ORIGINAL acknowledged_at —
                # that timestamp is when the human actually pressed OK, and
                # it also anchors the TTL. Refresh only the transport hints,
                # which may well be why it hasn't been delivered yet.
                existing.teacher_host = rec.teacher_host or existing.teacher_host
                existing.teacher_port = rec.teacher_port or existing.teacher_port
                existing.next_attempt_at = 0.0     # retry promptly
                self._save_locked()
                logger.debug("Ack already queued for %s; refreshed transport",
                             _short(rec.announcement_id))
                return existing
            self._records[rec.key] = rec
            self._save_locked()
            logger.info("Ack queued: announcement=%s board=%s teacher=%s",
                        _short(rec.announcement_id), _short(rec.board_device_id),
                        _short(rec.teacher_device_id))
            return rec

    def mark_sent(self, rec: AckRecord) -> None:
        with self._lock:
            self._records.pop(rec.key, None)
            self._save_locked()
        logger.info("Ack delivered and dequeued: announcement=%s board=%s "
                    "after %d attempt(s)", _short(rec.announcement_id),
                    _short(rec.board_device_id), rec.attempts)

    def reschedule(self, rec: AckRecord) -> None:
        with self._lock:
            rec.schedule_retry()
            self._save_locked()

    def sweep_expired(self) -> int:
        """Drop acknowledgments past their TTL. Returns how many went."""
        with self._lock:
            gone = [r for r in self._records.values() if r.is_expired()]
            for rec in gone:
                self._records.pop(rec.key, None)
                logger.warning(
                    "Ack EXPIRED after %.1f h without delivery: announcement=%s "
                    "board=%s teacher=%s (%d attempts) — giving up",
                    ACK_TTL_SECONDS / 3600.0, _short(rec.announcement_id),
                    _short(rec.board_device_id), _short(rec.teacher_device_id),
                    rec.attempts)
            if gone:
                self._save_locked()
            return len(gone)

    # ---- queries ----
    def due_records(self, now: Optional[float] = None) -> list:
        with self._lock:
            return [r for r in self._records.values() if r.due(now)]

    def for_teacher(self, teacher_device_id: str) -> list:
        with self._lock:
            return [r for r in self._records.values()
                    if r.teacher_device_id == teacher_device_id]

    def pending_count(self) -> int:
        with self._lock:
            return len(self._records)

    def all_records(self) -> list:
        with self._lock:
            return list(self._records.values())


def _short(value: str) -> str:
    """Trim an id for logs.

    Full ids are long and appear on every line; the first 8 characters are
    plenty to correlate one announcement across a log file, and keeping logs
    readable is what makes them get read.
    """
    value = value or ""
    return value[:8] if len(value) > 8 else value


# ---------------------------------------------------------------------------
# Delivery
# ---------------------------------------------------------------------------
class AckDispatcher:
    """Delivers queued acknowledgments to teacher devices, with retries.

    One background thread, waking on a condition variable rather than a fixed
    poll: ``flush_now()`` wakes it the instant something interesting happens
    (a user pressed OK, or a teacher device said hello), and otherwise it
    sleeps until the earliest scheduled retry. So the common case costs no
    CPU at all, and the reconnect case is handled immediately rather than on
    the next tick of a timer.
    """

    def __init__(self, queue: AckQueue, registry: TeacherRegistry,
                 *, post_json: Optional[Callable] = None):
        self._queue = queue
        self._registry = registry
        self._post_json = post_json or _default_post_json
        self._wake = threading.Condition()
        self._stop = threading.Event()
        self._thread: Optional[threading.Thread] = None

    # ---- lifecycle ----
    def start(self) -> None:
        if self._thread is not None and self._thread.is_alive():
            return
        self._stop.clear()
        self._thread = threading.Thread(target=self._run, daemon=True,
                                        name="LogicShare-AckDispatcher")
        self._thread.start()
        pending = self._queue.pending_count()
        logger.info("Ack dispatcher started (%d pending)", pending)
        if pending:
            self.flush_now()

    def stop(self) -> None:
        self._stop.set()
        with self._wake:
            self._wake.notify_all()
        t = self._thread
        if t is not None and t.is_alive():
            t.join(timeout=2.0)
        self._thread = None

    def flush_now(self) -> None:
        """Ask the dispatcher to try everything that is due, right now."""
        with self._wake:
            self._wake.notify_all()

    def flush_teacher(self, teacher_device_id: str) -> None:
        """A teacher device just became reachable — retry its acks at once.

        Clears the backoff for that teacher specifically. Without this a
        record already backed off to a 5-minute delay would sit idle for
        minutes after the teacher was demonstrably back, which is exactly the
        moment the user is watching for it.
        """
        records = self._queue.for_teacher(teacher_device_id)
        if not records:
            return
        for rec in records:
            rec.next_attempt_at = 0.0
        logger.info("Teacher %s reachable again — flushing %d queued ack(s)",
                    _short(teacher_device_id), len(records))
        self.flush_now()

    # ---- worker ----
    def _run(self) -> None:
        while not self._stop.is_set():
            try:
                self._queue.sweep_expired()
                for rec in self._queue.due_records():
                    if self._stop.is_set():
                        break
                    self._attempt(rec)
            except Exception:
                logger.exception("Ack dispatcher iteration failed")

            timeout = self._seconds_until_next()
            with self._wake:
                self._wake.wait(timeout=timeout)

    def _seconds_until_next(self) -> float:
        """How long to sleep: until the soonest retry, bounded.

        The upper bound is not busy-work — it is what makes the TTL sweep run
        on an empty queue, so expired records are cleaned up even when nothing
        else ever wakes the thread.
        """
        records = self._queue.all_records()
        if not records:
            return 300.0
        now = time.time()
        soonest = min((r.next_attempt_at for r in records
                       if r.state == AckState.PENDING), default=now + 300.0)
        return max(1.0, min(300.0, soonest - now))

    def _attempt(self, rec: AckRecord) -> None:
        host, port = self._resolve(rec)
        if not host:
            logger.debug("Ack for %s: no transport address for teacher %s yet",
                         _short(rec.announcement_id),
                         _short(rec.teacher_device_id))
            self._queue.reschedule(rec)
            return

        url = f"http://{host}:{port}/ack"
        try:
            ok, status = self._post_json(url, rec.to_wire(), timeout=(4, 8))
        except Exception as e:
            logger.debug("Ack POST to %s raised: %s", url, e)
            ok, status = False, 0

        if ok:
            self._queue.mark_sent(rec)
            return

        self._queue.reschedule(rec)
        logger.info("Ack retry scheduled: announcement=%s board=%s "
                    "attempt=%d status=%s next in %.0fs",
                    _short(rec.announcement_id), _short(rec.board_device_id),
                    rec.attempts, status or "unreachable",
                    max(0.0, rec.next_attempt_at - time.time()))

    def _resolve(self, rec: AckRecord) -> tuple:
        """Current transport for the record's teacher.

        Registry first: it holds the most recently OBSERVED address, which is
        newer than whatever was captured when the announcement arrived. The
        record's own hint is the fallback for a queue restored from disk
        before the teacher has been seen this session.
        """
        entry = self._registry.lookup(rec.teacher_device_id)
        if entry and entry.get("host"):
            return entry["host"], int(entry.get("port") or TEACHER_ACK_PORT)
        if rec.teacher_host:
            return rec.teacher_host, int(rec.teacher_port or TEACHER_ACK_PORT)
        return "", TEACHER_ACK_PORT


def _default_post_json(url: str, payload: dict, timeout=(4, 8)) -> tuple:
    """POST JSON. Returns (ok, status_code).

    ``requests`` is imported lazily so importing this module stays cheap and
    unit tests can inject their own transport without the dependency.
    """
    import requests
    resp = requests.post(url, json=payload, timeout=timeout)
    return (resp.status_code == 200), resp.status_code
