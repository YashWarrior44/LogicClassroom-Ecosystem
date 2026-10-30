"""
platform.py — Platform-specific helpers: single-instance guard and
desktop shortcut creation (Windows).
"""

from __future__ import annotations

import sys
import ctypes
import logging
import subprocess
import threading
from pathlib import Path

from .paths import app_dir, find_app_icon_path

__all__ = [
    "is_already_running",
    "named_pipe_exists",
    "show_already_running_dialog",
    "set_windows_app_id",
    "auto_create_shortcut_if_missing",
]

_SINGLE_INSTANCE_MUTEX = None
_ERROR_ALREADY_EXISTS  = 183
_ERROR_FILE_NOT_FOUND  = 2

# Session-local, NOT "Global\". Creating a kernel object in the Global
# namespace needs SeCreateGlobalPrivilege, which a normal interactive user
# does not hold — CreateMutexW returned NULL with ERROR_ACCESS_DENIED (5)
# on every single launch, so the "already exists" test below could never
# fire and the guard silently passed EVERY time. Every launch then went on
# to become a full second instance, and it was the second instance's
# ``InstanceServer.listen`` that failed with "Access is denied" (the first
# instance already owned the pipe) — the error in the startup log.
#
# "Local\" is also the RIGHT scope on the merits: one LogicBoard per
# desktop session. Two users on the same machine (fast user switching, RDP)
# should each get their own.
_MUTEX_NAME = "Local\\LogicBoard_v3_SingleInstance"


def _kernel32():
    """kernel32 with ``use_last_error`` so ``ctypes.get_last_error()`` is
    meaningful. ``ctypes.windll.kernel32.GetLastError()`` is NOT: ctypes
    swaps in its own private copy of the thread's last-error value around
    every foreign call, so that reads back whatever ctypes' own bookkeeping
    last set, not what CreateMutexW reported."""
    return ctypes.WinDLL("kernel32", use_last_error=True)


def is_already_running() -> bool:
    """Windows single-instance guard using a named kernel mutex.

    Fast path only — it answers "is another LogicBoard holding the session
    mutex?" in one syscall, with no socket timeouts. ``named_pipe_exists``
    is the authoritative check; see the call site in ``ui/app.py``.
    """
    global _SINGLE_INSTANCE_MUTEX

    if sys.platform != "win32":
        return False

    try:
        k32 = _kernel32()
        # restype MUST be set: a HANDLE is 64-bit on win64 and ctypes
        # defaults to c_int, which truncates it to garbage.
        k32.CreateMutexW.restype  = ctypes.c_void_p
        k32.CreateMutexW.argtypes = [ctypes.c_void_p, ctypes.c_bool,
                                     ctypes.c_wchar_p]
        ctypes.set_last_error(0)
        handle = k32.CreateMutexW(None, False, _MUTEX_NAME)
        err = ctypes.get_last_error()
        if not handle:
            # Couldn't create it at all — say "not running" and let the
            # pipe probe decide. Never block startup on this.
            logging.debug("single-instance mutex unavailable (err=%s)", err)
            return False
        # Keep the handle for the life of the process: closing it (or
        # letting the value get garbage-collected out of a truncated int)
        # would release the mutex and let the next launch think it's first.
        _SINGLE_INSTANCE_MUTEX = handle
        return err == _ERROR_ALREADY_EXISTS
    except Exception:
        logging.debug("is_already_running failed", exc_info=True)
        return False


def named_pipe_exists(pipe_name: str) -> bool:
    """Does ``\\\\.\\pipe\\<pipe_name>`` exist right now?

    This is the single-instance question that actually matters: the pipe is
    what the running instance is reachable ON, and a named pipe dies with
    the process that created it, so there is no such thing as a stale one to
    confuse us. It needs no Qt objects and no event loop, which is why it
    can run at the very top of startup — before any service is bound —
    instead of after, where a "someone else is here" verdict would arrive
    too late to act on cleanly.
    """
    if sys.platform != "win32":
        return False
    try:
        k32 = _kernel32()
        k32.WaitNamedPipeW.restype  = ctypes.c_bool
        k32.WaitNamedPipeW.argtypes = [ctypes.c_wchar_p, ctypes.c_ulong]
        ctypes.set_last_error(0)
        if k32.WaitNamedPipeW("\\\\.\\pipe\\" + pipe_name, 0):
            return True
        # FALSE means "no instance is available right now", which covers two
        # very different cases. Only ERROR_FILE_NOT_FOUND means the pipe
        # isn't there; anything else (typically ERROR_SEM_TIMEOUT — it
        # exists but every instance is busy serving someone) means it IS.
        return ctypes.get_last_error() != _ERROR_FILE_NOT_FOUND
    except Exception:
        logging.debug("named_pipe_exists failed", exc_info=True)
        return False


def show_already_running_dialog() -> None:
    """Show a native Windows error dialog when a second instance is detected."""
    if sys.platform != "win32":
        return

    try:
        ctypes.windll.user32.MessageBoxW(
            None,
            "LogicBoard is already running!\n\n"
            "Close the existing window first.",
            "LogicBoard — Already Running",
            0x10,
        )
    except Exception:
        pass


def set_windows_app_id() -> None:
    """Set the Windows taskbar application identity.

    Uses ``windows_registration.APP_USER_MODEL_ID`` — a STABLE,
    version-less identity string ("InnovaLogiX.LogicBoard"). This is
    intentional: the AppUserModelID is what Windows uses to group
    taskbar buttons, keep a pinned taskbar icon attached to the app,
    and remember jump-list history. If this string changed on every
    version bump (as the old hardcoded "LogicBoard.v3.0" effectively
    would have once other version-literal spots were fixed), every
    update would look like a brand-new, unrelated application to
    Windows — pinned icons would detach, jump lists would reset.
    """
    if sys.platform != "win32":
        return

    try:
        from ui.util.windows_registration import APP_USER_MODEL_ID
        ctypes.windll.shell32.SetCurrentProcessExplicitAppUserModelID(
            APP_USER_MODEL_ID
        )
        logging.info("Windows Taskbar ID set: %s", APP_USER_MODEL_ID)
    except Exception as e:
        logging.warning(f"Failed to set Windows app ID: {e}")


def auto_create_shortcut_if_missing() -> None:
    """Legacy no-op.

    Shortcut creation is now handled centrally by
    ``ui.util.windows_registration.register()`` which app.py calls at
    startup. That newer path:
      * Creates BOTH desktop and Start Menu shortcuts (this old function
        only did the desktop one).
      * Points shortcuts at the versioned install folder so an update
        just re-registers with the new path — no orphaned shortcuts.
      * Registers the Apps & Features entry so LogicBoard shows up as
        a proper installed app in Settings.

    This function is kept as an alias so existing callers keep working
    without needing to be rewritten — it's called from app.py's own
    startup path but the real work has already happened by then.
    """
    # Intentionally empty. See docstring above.
    return