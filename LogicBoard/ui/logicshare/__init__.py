"""
ui.logicshare — LogicShare integrated into LogicBoard.

The half of LogicShare that runs ON the smart board: a silent background
service (Flask + Zeroconf) that receives files and announcements from teachers'
LogicShare apps, plus the aero UI (setup wizard, notifications, settings
section, history) — all built on LogicBoard's own theme / aero / scaling /
shadow utilities, not a duplicate styling system.
"""
from __future__ import annotations

from .config import (
    LSConfig, CLASS_STANDARDS, SECTIONS, SETTINGS_KEY,
    validate_room, validate_class_standard, validate_section, default_device_name,
    read_settings, write_settings_merged, delete_settings_key,
)
from .activity_log import (
    ActivityLog, EVENT_TYPES, EVENT_LABELS,
    EVENT_FILE_RECEIVED, EVENT_ANNOUNCEMENT_RECEIVED,
    EVENT_FILE_SENT, EVENT_ANNOUNCEMENT_SENT,
)
from .service import LogicShareService, PORT

__all__ = [
    "LSConfig", "CLASS_STANDARDS", "SETTINGS_KEY",
    "validate_room", "validate_class_standard", "default_device_name",
    "read_settings", "write_settings_merged", "delete_settings_key",
    "ActivityLog", "EVENT_TYPES", "EVENT_LABELS",
    "EVENT_FILE_RECEIVED", "EVENT_ANNOUNCEMENT_RECEIVED",
    "EVENT_FILE_SENT", "EVENT_ANNOUNCEMENT_SENT",
    "LogicShareService", "PORT",
]