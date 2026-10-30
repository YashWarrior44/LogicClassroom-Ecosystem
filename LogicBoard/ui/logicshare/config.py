"""
ui/logicshare/config.py — LogicShare configuration (room, class, password, device).

Persisted inside the SAME ``settings.json`` LogicBoard already uses (under a
dedicated ``"logicshare"`` key) so there's one config file, not a duplicate
system. Writes are merge-safe: we load the whole file, update only our key,
and write it back — so we never clobber the input-calibration / autosave keys
that also live there. (InputCalibration.save() is likewise patched to preserve
our key.)

Password handling uses stdlib PBKDF2-HMAC-SHA256 with a per-config random salt
— no third-party dependency. We store only salt + hash, never the plaintext.
"""
from __future__ import annotations

import os
import json
import socket
import hmac
import hashlib
import logging
import tempfile
from pathlib import Path
from typing import Optional

logger = logging.getLogger("LogicShare.config")

# The "logicshare" sub-key inside settings.json.
SETTINGS_KEY = "logicshare"

# Class Standard dropdown values, in display order (from the spec).
CLASS_STANDARDS = [
    "LGK", "UKG", "1st", "2nd", "3rd", "4th", "5th", "6th",
    "7th", "8th", "9th", "10th", "11th", "12th",
]

# Section dropdown values (A through G).
SECTIONS = ["A", "B", "C", "D", "E", "F", "G"]

_PBKDF2_ROUNDS = 120_000


# ---------------------------------------------------------------------------
# Shared merge-safe settings.json helpers (used here AND by the calibration
# patch) so multiple managers can share one file without clobbering each other.
# ---------------------------------------------------------------------------
def read_settings(path: Path) -> dict:
    """Return the whole settings.json as a dict (—{} if missing/corrupt)."""
    try:
        p = Path(path)
        if p.exists():
            data = json.loads(p.read_text(encoding="utf-8"))
            if isinstance(data, dict):
                return data
    except Exception as e:
        logger.warning("Could not read settings.json (%s); treating as empty.", e)
    return {}


def write_settings_merged(path: Path, key: str, value) -> bool:
    """Update ONE top-level key in settings.json, preserving the rest.

    THE ONLY WAY THIS FILE SHOULD BE WRITTEN.

    settings.json is shared: LogicShare's whole configuration lives under
    one key, the last board path under another, and more will follow. A
    writer that rewrites the file wholesale therefore has everyone else's
    data in its hands every time it saves a field of its own — and
    ``Path.write_text`` opens for writing, which TRUNCATES first and only
    then writes. Anything that goes wrong in between (disk full, power cut,
    the process being killed as Windows shuts down) leaves the truncated
    file behind.

    That is not hypothetical. Saving ``last_board_path`` — a convenience
    field nobody would miss — destroyed the classroom name, the password
    hash and the port, and ``read_settings`` reports a corrupt file as an
    empty one, so the app simply came up as though it had never been set
    up. Reproduced by failing the write mid-call: 0 bytes, everything gone.

    So: write a UNIQUE temp file beside the target, flush it all the way to
    the platter, then rename over the top. Rename is atomic on both NTFS
    and POSIX, so a reader sees either the whole old file or the whole new
    one, never a mixture. The temp name comes from ``mkstemp`` rather than
    being a fixed ``settings.json.tmp``, because two managers saving at the
    same moment would otherwise be writing to the SAME scratch file.

    ``value`` is deliberately untyped: the LogicShare block is a dict, the
    last board path is a string, and a signature that only admits dicts is
    what sent the second caller off to write the file by hand.

    Returns True if the file now contains ``value``.
    """
    p = Path(path)
    tmp_path = None
    try:
        data = read_settings(p)
        data[key] = value
        p.parent.mkdir(parents=True, exist_ok=True)
        fd, tmp_name = tempfile.mkstemp(prefix=p.stem + "_", suffix=".tmp",
                                        dir=str(p.parent))
        tmp_path = Path(tmp_name)
        with os.fdopen(fd, "w", encoding="utf-8") as f:
            json.dump(data, f, indent=2)
            f.flush()
            # The rename is only atomic with respect to a file that has
            # actually reached the disk. Without this, a power cut just
            # after the rename can leave a correctly-named EMPTY file.
            os.fsync(f.fileno())
        os.replace(tmp_path, p)
        tmp_path = None
        return True
    except Exception as e:
        logger.error("Failed to write settings.json key '%s': %s", key, e)
        return False
    finally:
        if tmp_path is not None:
            try:
                tmp_path.unlink()
            except OSError:
                pass


def delete_settings_key(path: Path, key: str) -> bool:
    """Remove ONE top-level key, with the same guarantees as writing one."""
    p = Path(path)
    data = read_settings(p)
    if key not in data:
        return True
    data.pop(key, None)
    # Re-use the writer by handing back a sentinel-free copy: simplest
    # correct thing is to write every remaining key through the same path.
    tmp_path = None
    try:
        p.parent.mkdir(parents=True, exist_ok=True)
        fd, tmp_name = tempfile.mkstemp(prefix=p.stem + "_", suffix=".tmp",
                                        dir=str(p.parent))
        tmp_path = Path(tmp_name)
        with os.fdopen(fd, "w", encoding="utf-8") as f:
            json.dump(data, f, indent=2)
            f.flush()
            os.fsync(f.fileno())
        os.replace(tmp_path, p)
        tmp_path = None
        return True
    except Exception as e:
        logger.error("Failed to remove settings.json key '%s': %s", key, e)
        return False
    finally:
        if tmp_path is not None:
            try:
                tmp_path.unlink()
            except OSError:
                pass


# ---------------------------------------------------------------------------
# Password hashing (stdlib only)
# ---------------------------------------------------------------------------
def hash_password(plain: str, salt_hex: Optional[str] = None) -> tuple[str, str]:
    """Return (salt_hex, hash_hex) for a plaintext password using PBKDF2."""
    salt = bytes.fromhex(salt_hex) if salt_hex else os.urandom(16)
    dk = hashlib.pbkdf2_hmac("sha256", plain.encode("utf-8"), salt, _PBKDF2_ROUNDS)
    return salt.hex(), dk.hex()


def verify_password(plain: str, salt_hex: str, hash_hex: str) -> bool:
    if not salt_hex or not hash_hex:
        return False
    try:
        _, candidate = hash_password(plain, salt_hex)
        # constant-time compare (compare_digest is in hmac, not hashlib)
        return hmac.compare_digest(candidate, hash_hex)
    except Exception:
        return False


def default_device_name() -> str:
    """A friendly default device name derived from the machine hostname."""
    try:
        host = socket.gethostname() or "SmartBoard"
    except Exception:
        host = "SmartBoard"
    return host.split(".")[0]


def validate_room(room: str) -> bool:
    """Room number must be EXACTLY 3 digits, numeric only (spec)."""
    return bool(room) and len(room) == 3 and room.isdigit()


def validate_class_standard(std: str) -> bool:
    return std in CLASS_STANDARDS


def validate_section(sec: str) -> bool:
    return sec in SECTIONS


# ---------------------------------------------------------------------------
# Config manager
# ---------------------------------------------------------------------------
class LSConfig:
    """LogicShare configuration, persisted in settings.json["logicshare"]."""

    def __init__(self, settings_path: Path):
        self._path = Path(settings_path)
        self.room: str = ""
        self.class_standard: str = ""
        self.section: str = ""
        self.device_name: str = default_device_name()
        # Stable machine identity for this board — see ``device_id`` below.
        self.device_id: str = ""
        self._password_salt: str = ""
        self._password_hash: str = ""
        self.load()

    # ---- persistence ----
    def load(self) -> None:
        data = read_settings(self._path).get(SETTINGS_KEY, {})
        if not isinstance(data, dict):
            data = {}
        self.room = str(data.get("room", "") or "")
        self.class_standard = str(data.get("class_standard", "") or "")
        self.section = str(data.get("section", "") or "")
        self.device_name = str(data.get("device_name", "") or "") or default_device_name()
        # ---------------------------------------------------------------
        # device_id — the board's STABLE identity on the LAN.
        #
        # Distinct from device_name, which is a human label the teacher can
        # change, and from the IP address, which changes whenever the board
        # roams to another AP or its DHCP lease turns over. Announcement
        # acknowledgments are matched on this value, so it has to outlive
        # both of those: an ack routed by IP after a lease change is
        # delivered to whoever now holds that address.
        #
        # Generated once, on first run, and persisted. Never regenerated —
        # a board that changed its id would look like a brand new board to
        # every teacher device that had already recorded an announcement
        # against the old one.
        # ---------------------------------------------------------------
        self.device_id = str(data.get("device_id", "") or "")
        if not self.device_id:
            from uuid import uuid4
            self.device_id = "bd_" + uuid4().hex[:16]
            logger.info("Generated stable board device_id: %s", self.device_id)
            try:
                write_settings_merged(self._path, SETTINGS_KEY,
                                      {**data, "device_id": self.device_id})
            except Exception:
                logger.exception("Could not persist new device_id")
        self._password_salt = str(data.get("password_salt", "") or "")
        self._password_hash = str(data.get("password_hash", "") or "")
        logger.info("Loaded LogicShare config: room=%r class=%r section=%r device=%r configured=%s",
                    self.room, self.class_standard, self.section, self.device_name, self.is_configured())

    def to_dict(self) -> dict:
        return {
            "room":          self.room,
            "class_standard": self.class_standard,
            "section":       self.section,
            "device_name":   self.device_name,
            "device_id":     self.device_id,
            "password_salt": self._password_salt,
            "password_hash": self._password_hash,
        }

    def save(self) -> None:
        write_settings_merged(self._path, SETTINGS_KEY, self.to_dict())

    # ---- state ----
    def is_configured(self) -> bool:
        """True once room, class, section, AND a password have all been set."""
        return (validate_room(self.room)
                and validate_class_standard(self.class_standard)
                and validate_section(self.section)
                and bool(self._password_hash))

    def has_password(self) -> bool:
        return bool(self._password_hash)

    # ---- mutations ----
    def set_password(self, plain: str) -> None:
        self._password_salt, self._password_hash = hash_password(plain)

    def check_password(self, plain: str) -> bool:
        return verify_password(plain, self._password_salt, self._password_hash)

    def apply_setup(self, *, room: str, class_standard: str, section: str,
                    password: str, device_name: str = "") -> tuple[bool, str]:
        """Validate + apply first-run setup. Returns (ok, error_message)."""
        room = (room or "").strip()
        class_standard = (class_standard or "").strip()
        section = (section or "").strip()
        device_name = (device_name or "").strip()
        if not validate_room(room):
            return False, "Room number must be exactly 3 digits."
        if not validate_class_standard(class_standard):
            return False, "Please choose a valid class standard."
        if not validate_section(section):
            return False, "Please choose a section (A–G)."
        if not password:
            return False, "Password is required."
        self.room = room
        self.class_standard = class_standard
        self.section = section
        self.device_name = device_name or default_device_name()
        self.set_password(password)
        self.save()
        return True, ""

    def update_class_details(self, *, room: str, class_standard: str,
                             section: str, device_name: str = None) -> tuple[bool, str]:
        """Change room/class/section (and optionally device name) WITHOUT
        touching the password. Used by the password-gated 'Edit class details'
        action in Settings. Returns (ok, error_message)."""
        room = (room or "").strip()
        class_standard = (class_standard or "").strip()
        section = (section or "").strip()
        if not validate_room(room):
            return False, "Room number must be exactly 3 digits."
        if not validate_class_standard(class_standard):
            return False, "Please choose a valid class standard."
        if not validate_section(section):
            return False, "Please choose a section (A–G)."
        self.room = room
        self.class_standard = class_standard
        self.section = section
        if device_name is not None:
            self.device_name = device_name.strip() or self.device_name
        self.save()
        return True, ""