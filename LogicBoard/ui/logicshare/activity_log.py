"""
ui/logicshare/activity_log.py — LogicShare activity history (ls.json).

Stores a complete history of LogicShare events in ``ls.json`` (next to the
executable / project root, same place as settings.json). Each entry carries a
timestamp, event type, teacher name, room info, file name, and announcement
content. The History page reads from here with sort + filter support.

Supported event types (spec §6):
    file_received, announcement_received, file_sent, announcement_sent
"""
from __future__ import annotations

import os
import json
import time
import logging
from pathlib import Path
from typing import Optional
from uuid import uuid4

logger = logging.getLogger("LogicShare.activity")

EVENT_FILE_RECEIVED         = "file_received"
EVENT_ANNOUNCEMENT_RECEIVED = "announcement_received"
EVENT_FILE_SENT             = "file_sent"
EVENT_ANNOUNCEMENT_SENT     = "announcement_sent"

EVENT_TYPES = (
    EVENT_FILE_RECEIVED,
    EVENT_ANNOUNCEMENT_RECEIVED,
    EVENT_FILE_SENT,
    EVENT_ANNOUNCEMENT_SENT,
)

# Friendly labels for the History UI.
EVENT_LABELS = {
    EVENT_FILE_RECEIVED:         "File received",
    EVENT_ANNOUNCEMENT_RECEIVED: "Announcement received",
    EVENT_FILE_SENT:             "File sent",
    EVENT_ANNOUNCEMENT_SENT:     "Announcement sent",
}

_HISTORY_LIMIT = 500   # keep the file bounded


class ActivityLog:
    """Append-only-ish JSON history with sort + filter helpers."""

    def __init__(self, path: Path):
        self._path = Path(path)

    # ---- io ----
    def _read(self) -> list:
        try:
            if self._path.exists():
                data = json.loads(self._path.read_text(encoding="utf-8"))
                if isinstance(data, list):
                    return data
        except Exception as e:
            logger.warning("Could not read ls.json (%s); starting empty.", e)
        return []

    def _write(self, entries: list) -> None:
        try:
            self._path.parent.mkdir(parents=True, exist_ok=True)
            tmp = self._path.with_suffix(self._path.suffix + ".tmp")
            tmp.write_text(json.dumps(entries[:_HISTORY_LIMIT], indent=2,
                                      ensure_ascii=False), encoding="utf-8")
            tmp.replace(self._path)
        except Exception as e:
            logger.error("Could not write ls.json: %s", e)

    # ---- public ----
    def add(self, event_type: str, *,
            teacher_name: str = "",
            room: str = "",
            file_name: str = "",
            announcement: str = "",
            extra: Optional[dict] = None) -> dict:
        """Record one event (newest stored first) and persist."""
        if event_type not in EVENT_TYPES:
            logger.warning("Unknown event type %r — recording anyway.", event_type)
        entry = {
            "id":           uuid4().hex,
            "timestamp":    time.time(),
            "event_type":   event_type,
            "teacher_name": teacher_name,
            "room":         room,
            "file_name":    file_name,
            "announcement": announcement,
        }
        if extra:
            entry.update(extra)
        entries = self._read()
        entries.insert(0, entry)
        self._write(entries)
        return entry

    def all(self) -> list:
        return self._read()

    def filtered(self, event_type: Optional[str] = None) -> list:
        """All entries, optionally filtered to a single event type."""
        entries = self._read()
        if event_type and event_type != "all":
            entries = [e for e in entries if e.get("event_type") == event_type]
        return entries

    def query(self, *, event_type: Optional[str] = None,
              ascending: bool = False) -> list:
        """Filter by type + sort by timestamp (newest-first by default)."""
        entries = self.filtered(event_type)
        entries.sort(key=lambda e: e.get("timestamp", 0), reverse=not ascending)
        return entries

    def clear(self) -> None:
        self._write([])

    # ---- convenience recorders (used by the service / notifications) ----
    def record_file_received(self, *, teacher_name: str, room: str, file_name: str):
        return self.add(EVENT_FILE_RECEIVED, teacher_name=teacher_name,
                        room=room, file_name=file_name)

    def record_announcement_received(self, *, teacher_name: str, room: str,
                                     title: str, message: str):
        return self.add(EVENT_ANNOUNCEMENT_RECEIVED, teacher_name=teacher_name,
                        room=room, file_name="", announcement=message,
                        extra={"title": title})