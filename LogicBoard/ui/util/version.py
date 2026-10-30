"""
version.py — Central version identity for LogicBoard.

Every module that needs to know "what version am I?" imports ``APP_VERSION``
from here. That way a version bump is a single-line change and there's no
chance of drift between the updater, the single-instance handshake, the
About dialog, and the Windows registration.

``SemVer`` provides comparison semantics for the version-negotiation
handshake in ``single_instance.py`` — we can't just string-compare
"3.0.10" vs "3.0.9" and get the right answer, so we tuple-compare
integers instead.
"""
from __future__ import annotations

from dataclasses import dataclass
from typing import Optional

__all__ = ["APP_VERSION", "APP_NAME", "SemVer"]

# ---------------------------------------------------------------------------
# The version identity — bumping THIS line is the release ritual.
# Format: MAJOR.MINOR.PATCH (semver). No pre-release / build suffixes for
# now; the updater's SemVer parser handles those if we ever need them.
# ---------------------------------------------------------------------------
APP_VERSION = "3.1.3"

# Human-readable product name. Used for Windows registrations, shortcuts,
# taskbar tooltip, etc. NEVER include the version here — the version is a
# separate field so Windows treats "LogicBoard 3.0.0" and "LogicBoard 3.1.0"
# as ONE application with a version bump, not two apps.
APP_NAME = "LogicBoard"


@dataclass(frozen=True, order=True)
class SemVer:
    """Ordered semantic version tuple.

    ``@dataclass(order=True)`` gives us ``<`` / ``>`` / ``==`` comparing
    ``(major, minor, patch)`` in tuple order — exactly what version
    negotiation needs. We store fields in comparison order (major first).
    """
    major: int
    minor: int
    patch: int

    def __str__(self) -> str:
        return f"{self.major}.{self.minor}.{self.patch}"

    @classmethod
    def parse(cls, s: str) -> Optional["SemVer"]:
        """Parse a version string like '3.0.0' or 'v3.0.0'. Returns
        ``None`` on any malformed input rather than raising — the callers
        (updater, single_instance) treat "can't parse" as "not a valid
        version" and skip the comparison, which is the safest fallback."""
        if not s:
            return None
        s = s.strip().lstrip("vV")
        # Drop any pre-release / build suffix — we don't handle these yet
        # but we shouldn't crash if a future release ever ships '3.0.0-rc1'.
        for sep in ("-", "+"):
            if sep in s:
                s = s.split(sep, 1)[0]
        parts = s.split(".")
        if len(parts) < 2 or len(parts) > 3:
            return None
        try:
            major = int(parts[0])
            minor = int(parts[1])
            patch = int(parts[2]) if len(parts) == 3 else 0
        except ValueError:
            return None
        if major < 0 or minor < 0 or patch < 0:
            return None
        return cls(major, minor, patch)


# Pre-parsed for hot-path use (single_instance comparison runs once per
# duplicate-launch attempt, but caching costs nothing).
APP_SEMVER = SemVer.parse(APP_VERSION)