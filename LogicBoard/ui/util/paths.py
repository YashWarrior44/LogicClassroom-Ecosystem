"""
paths.py — Asset and data directory resolution.

Handles both development (source tree) and frozen (PyInstaller .exe) layouts.
All path helpers return pathlib.Path objects.
"""

from __future__ import annotations

import os
import sys
import shutil
import time
import logging
from pathlib import Path
from typing import Optional

__all__ = [
    "base_path",
    "app_dir",
    "assets_dir",
    "sounds_dir",
    "icons_dir",
    "saves_dir",
    "resource_path",
    "find_app_icon_path",
    "config_dir",
    "cache_dir",
    "temp_dir",
    "sweep_temp_dirs",
    "LOG_FILE",
]

# ---------------------------------------------------------------------------
# Root directories
# ---------------------------------------------------------------------------

def base_path() -> Path:
    """Bundle root (PyInstaller _MEIPASS) or project source root."""
    if getattr(sys, "frozen", False):
        return Path(sys._MEIPASS)  # type: ignore[attr-defined]
    # In dev mode, go up one level from ui/ to reach project root.
    return Path(__file__).resolve().parent.parent.parent


def app_dir() -> Path:
    """Writable directory next to the executable (frozen) or project root."""
    if getattr(sys, "frozen", False):
        return Path(sys.executable).resolve().parent
    return Path(__file__).resolve().parent.parent.parent


# ---------------------------------------------------------------------------
# Asset directories
# ---------------------------------------------------------------------------

def assets_dir() -> Path:
    return base_path() / "assets"


def sounds_dir() -> Path:
    return assets_dir() / "sounds"


def icons_dir() -> Path:
    return assets_dir() / "icons"


# ---------------------------------------------------------------------------
# Persistent data
# ---------------------------------------------------------------------------

_saves_migrated = False


def saves_dir() -> Path:
    """Boards, settings.json and the LogicShare config.

    MUST be version-independent, for the same reason as config/cache/temp
    below — and more urgently. Installing an update WIPES the install
    folder and lays the new build down in its place. With saves sitting
    next to the executable, updating didn't merely orphan the user's data
    — it deleted it: boards, settings, room/class/section and the
    LogicShare password hash all went with the old install.

    Frozen  -> %LOCALAPPDATA%/LogicBoard/saves   (survives updates)
    Dev     -> <project>/saves                   (keeps the source tree
                                                  self-contained, and
                                                  out of real user data)
    """
    if not getattr(sys, "frozen", False):
        d = app_dir() / "saves"
        d.mkdir(parents=True, exist_ok=True)
        return d

    d = _user_state_root() / "saves"
    d.mkdir(parents=True, exist_ok=True)
    _migrate_legacy_saves(d)
    return d


def _migrate_legacy_saves(dest: Path) -> None:
    """One-shot rescue of a pre-fix install's saves folder.

    Builds before this fix wrote to ``<exe dir>/saves``. If such a folder
    is still there and the new location is untouched, COPY the contents
    across so an existing user keeps their boards. Only ever runs when
    the destination is empty, so it can never clobber newer data, and
    only once per process.

    Copy, never move. ``shutil.move`` is copy-then-DELETE, and the delete
    half can fail for reasons this function cannot control (a file open in
    another app, a read-only source, an old Program Files install a normal
    user cannot write to). Worse, it fails per item: a
    move that dies partway through leaves the boards split across two
    folders, and this function never runs again. Copying leaves the
    original untouched as a free backup and makes a partial failure
    harmless.
    """
    global _saves_migrated
    if _saves_migrated:
        return
    _saves_migrated = True
    try:
        legacy = app_dir() / "saves"
        if not legacy.is_dir() or legacy.resolve() == dest.resolve():
            return
        if any(dest.iterdir()):        # already holds data — leave it alone
            return
        items = list(legacy.iterdir())
        if not items:
            return
    except OSError:
        return

    copied = 0
    for src in items:
        try:
            target = dest / src.name
            if src.is_dir():
                shutil.copytree(src, target, dirs_exist_ok=True)
            else:
                shutil.copy2(src, target)
            copied += 1
        except OSError as e:
            # Skip the individual item — the original is still in place,
            # so nothing is lost and the user can copy it by hand.
            logging.getLogger(__name__).warning(
                "Could not migrate %s: %s", src.name, e)
    if copied:
        logging.getLogger(__name__).info(
            "Migrated %d of %d item(s) from the legacy saves folder "
            "%s -> %s (originals left in place)",
            copied, len(items), legacy, dest)


# ---------------------------------------------------------------------------
# Per-user state  (%LOCALAPPDATA%/LogicBoard/...)
#
# These deliberately do NOT live next to the executable. Every version
# installs into its own ``LogicBoard_V<ver>`` folder (see updater.py), so
# anything stored app-dir-relative is orphaned on the next update. Config,
# cache and scratch space belong to the USER, not to a build, so they go to
# a stable per-user location that every version resolves identically.
# ---------------------------------------------------------------------------

def _user_state_root() -> Path:
    """Root for per-user, version-independent state.

    Windows -> %LOCALAPPDATA%/LogicBoard
    POSIX   -> $XDG_DATA_HOME/LogicBoard, else ~/.local/share/LogicBoard

    Falls back to ``app_dir()`` if the environment gives us nothing usable,
    so callers never have to handle a None.
    """
    if os.name == "nt":
        base = os.environ.get("LOCALAPPDATA") or os.environ.get("APPDATA")
    else:
        base = os.environ.get("XDG_DATA_HOME") or str(Path.home() / ".local" / "share")
    if not base:
        return app_dir()
    try:
        return Path(base) / "LogicBoard"
    except (OSError, ValueError):
        return app_dir()


def config_dir() -> Path:
    """Per-user config that must survive updates.

    Holds the Windows-registration fingerprint and the update-notifier
    state — both of which exist precisely to remember something across
    version changes, so they cannot live in a versioned folder.
    """
    d = _user_state_root() / "Config"
    d.mkdir(parents=True, exist_ok=True)
    return d


def cache_dir() -> Path:
    """Regenerable per-user cache. Anything here can be deleted at any time
    without data loss; ``sweep_temp_dirs()`` prunes stale entries on startup.
    """
    d = _user_state_root() / "Cache"
    d.mkdir(parents=True, exist_ok=True)
    return d


def temp_dir() -> Path:
    """Scratch space for this app only.

    Used for generated helper scripts (``windows_registration``). Emptied at
    every startup by ``sweep_temp_dirs()``, so nothing written here may be
    assumed to survive a restart.
    """
    d = _user_state_root() / "Temp"
    d.mkdir(parents=True, exist_ok=True)
    return d


def updates_dir() -> Path:
    """Where ``updater`` parks a downloaded LogicBoardInstaller.exe.

    Deliberately NOT ``temp_dir()``: the update hand-off downloads an
    installer, launches it, and quits, and the installer relaunches
    LogicBoard when it is done. That new process runs ``sweep_temp_dirs()``
    on startup — which would try to delete the installer that is still on
    screen finishing up. Windows refuses to unlink a running image so the
    delete would fail harmlessly, but a directory whose contract is "emptied
    at every startup" is the wrong home for a ~200 MB file another process
    is reading. ``updater`` prunes this folder itself before each download.
    """
    d = _user_state_root() / "Updates"
    d.mkdir(parents=True, exist_ok=True)
    return d


# ---------------------------------------------------------------------------
# Scratch cleanup
# ---------------------------------------------------------------------------

# Cache entries older than this are pruned at startup.
_CACHE_MAX_AGE_DAYS = 14


def _is_within(child: Path, parent: Path) -> bool:
    """True if ``child`` LIVES inside ``parent``. Guard for every delete
    below — a bad path resolution should fail closed, not wipe something
    else.

    Note we resolve the child's PARENT and re-attach its name, rather than
    resolving the child itself. Resolving a symlink yields its TARGET,
    which is usually outside this root — so the naive version refuses to
    delete symlinks at all and they accumulate forever. What matters is
    where the link sits, not where it points; ``_remove_entry`` unlinks it
    without following it.
    """
    try:
        p = parent.resolve(strict=False)
        c = child.parent.resolve(strict=False) / child.name
        c.relative_to(p)
        return True
    except (ValueError, OSError):
        return False


def _remove_entry(entry: Path, root: Path) -> bool:
    """Best-effort delete of one file or directory. Returns True on success.

    Refuses anything outside ``root``, and never recurses through a symlink
    (``is_symlink`` is checked before ``is_dir``, since a link to a directory
    reports True for both).
    """
    if not _is_within(entry, root):
        logging.getLogger(__name__).warning(
            "Refusing to delete %s — outside %s", entry, root)
        return False
    try:
        if entry.is_symlink() or entry.is_file():
            entry.unlink()
        elif entry.is_dir():
            shutil.rmtree(entry, ignore_errors=True)
        return not entry.exists()
    except OSError as e:
        # Locked by another process (very common on Windows) — leave it.
        logging.getLogger(__name__).debug("Could not remove %s: %s", entry, e)
        return False


def sweep_temp_dirs() -> None:
    """Reclaim scratch space at startup. Never raises.

    Three jobs:

    1. Empty ``temp_dir()`` outright — nothing in it is meant to persist.
    2. Prune ``cache_dir()`` entries older than ``_CACHE_MAX_AGE_DAYS``.
    3. Remove a stray ``temps/`` folder next to the executable. Board
       workdirs legitimately contain ``temps/`` (see ``lb_format``), but
       those live under ``saves_dir()``; one directly in ``app_dir()`` is
       a leak from an older layout.

    Board workdirs are NOT touched here — ``lb_format.sweep_workdirs()``
    owns those, and app.py calls it separately just above this.
    """
    log = logging.getLogger(__name__)
    freed = 0

    # 1. Temp — empty it completely.
    try:
        td = temp_dir()
        for entry in td.iterdir():
            if _remove_entry(entry, td):
                freed += 1
    except OSError as e:
        log.debug("Temp sweep skipped: %s", e)

    # 2. Cache — age-based prune only.
    try:
        cd = cache_dir()
        cutoff = time.time() - _CACHE_MAX_AGE_DAYS * 86400
        for entry in cd.iterdir():
            try:
                if entry.stat().st_mtime >= cutoff:
                    continue
            except OSError:
                continue
            if _remove_entry(entry, cd):
                freed += 1
    except OSError as e:
        log.debug("Cache prune skipped: %s", e)

    # 3. Stray temps/ next to the executable.
    try:
        stray = app_dir() / "temps"
        if stray.is_dir() and not stray.is_symlink():
            if _remove_entry(stray, app_dir()):
                freed += 1
                log.info("Removed stray temps/ folder from the app directory")
    except OSError as e:
        log.debug("Stray temps check skipped: %s", e)

    if freed:
        log.info("Temp sweep: removed %d entr%s",
                 freed, "y" if freed == 1 else "ies")


# ---------------------------------------------------------------------------
# Resource resolver
# ---------------------------------------------------------------------------

def resource_path(relative: str) -> Path:
    """Resolve a relative path against the bundle/base root."""
    return base_path() / relative


# ---------------------------------------------------------------------------
# Icon finder
# ---------------------------------------------------------------------------

def find_app_icon_path() -> Optional[Path]:
    """Find the first .ico or .png icon in the icons directory."""
    d = icons_dir()
    if not d.exists():
        # Fallback: look in base_path directly (legacy layout).
        d = base_path()
    # By name first. The folder also holds pdf.ico and ppt.ico (the icons
    # Windows shows for documents LogicBoard opens), and "first .ico in
    # alphabetical order" only lands on the logo for as long as nothing
    # sorts ahead of "logo".
    if (d / "logo.ico").is_file():
        return d / "logo.ico"
    for ext in ("*.ico", "*.png", "*.svg"):
        found = sorted(d.glob(ext))
        if found:
            return found[0]
    return None


# ---------------------------------------------------------------------------
# Logging
# ---------------------------------------------------------------------------

def _log_file_path() -> Path:
    """Where the debug log lives.

    The install folder is wiped and rewritten by every update, so a log
    beside the executable is a log that vanishes exactly when someone wants
    to read it — and on the older Program Files layout it was worse than
    that: not writable by a normal user at all, so the app died on startup
    with ``PermissionError`` before it could report anything, and a windowed
    build shows the user nothing. The per-user state root is writable and
    update-safe, same as saves/config.

    Frozen  -> %LOCALAPPDATA%/LogicBoard/logicboard_debug.log
    Dev     -> <project>/logicboard_debug.log   (unchanged)
    """
    if getattr(sys, "frozen", False):
        try:
            root = _user_state_root()
            root.mkdir(parents=True, exist_ok=True)
            return root / "logicboard_debug.log"
        except OSError:
            import tempfile
            return Path(tempfile.gettempdir()) / "logicboard_debug.log"
    return app_dir() / "logicboard_debug.log"


LOG_FILE = str(_log_file_path())

#: Kept open for the life of the process — faulthandler writes to it from
#: inside a crash, when opening a file is no longer possible.
_CRASH_FILE = None


def _enable_crash_log() -> None:
    """Make a NATIVE crash leave a trace.

    The excepthook above only sees Python exceptions. An access violation
    inside a C extension — PyMuPDF, pywin32, the engine, a graphics driver —
    ends the process on the spot: no exception, no log line, the window just
    vanishes. That is exactly what a PowerPoint import did on a school smart
    board, and there was nothing to read afterwards. faulthandler writes
    every thread's Python stack to ``logicboard_crash.log`` at the moment of
    the crash, so the next one says which thread, in which file, on which
    line.

    The previous session's crash, if there was one, is copied into the main
    log on the way up — so the one file a teacher sends contains it.
    """
    global _CRASH_FILE
    try:
        import faulthandler
        crash = Path(LOG_FILE).with_name("logicboard_crash.log")
        try:
            prev = crash.read_text(encoding="utf-8", errors="replace")
            last = prev.rsplit("=== session ", 1)[-1]
            if "Fatal Python error" in last or "fatal exception" in last:
                logging.error("THE PREVIOUS SESSION CRASHED (native). "
                              "Its last stacks:\n%s", last[-6000:])
        except OSError:
            pass
        # Keep the file small: a crash log that grows forever is one nobody
        # can attach.
        try:
            if crash.exists() and crash.stat().st_size > 512 * 1024:
                crash.unlink()
        except OSError:
            pass
        import datetime, os as _os
        _CRASH_FILE = open(crash, "a", encoding="utf-8")
        _CRASH_FILE.write("\n=== session %s pid %d ===\n"
                          % (datetime.datetime.now().isoformat(" ", "seconds"),
                             _os.getpid()))
        _CRASH_FILE.flush()
        faulthandler.enable(file=_CRASH_FILE, all_threads=True)
    except Exception:
        logging.debug("crash log unavailable", exc_info=True)

def setup_logging() -> None:
    """Configure root logger to write to the debug log file.

    Uses force=True: if any imported library (Qt multimedia, pywinstyles,
    etc.) touched the root logger before we got here, a plain basicConfig
    would silently become a no-op and NO log file would be created. force=True
    tears down any pre-existing handlers and installs ours unconditionally.
    Also mirror to the console so logs show in the terminal too.

    Before starting a fresh log, the PREVIOUS session's log is rotated to
    ``<name>.1`` — otherwise opening in 'w' mode would truncate it, and a
    crash log would be gone the instant the user relaunched to read it.
    """
    global LOG_FILE          # reassigned below if the primary path fails
    import sys as _sys
    # Preserve the previous session's log so a crash is still diagnosable
    # after a relaunch. Best-effort — never let log rotation stop startup.
    try:
        _log_path = Path(LOG_FILE)
        if _log_path.exists():
            prev = _log_path.with_suffix(_log_path.suffix + ".1")
            try:
                if prev.exists():
                    prev.unlink()
            except OSError:
                pass
            _log_path.replace(prev)
    except OSError:
        pass

    # A log file must NEVER be the reason the app won't start. Opening it
    # can still fail for reasons the path choice can't prevent — a full
    # disk, a locked file, a redirected LOCALAPPDATA — so fall back to the
    # system temp dir, and if even that fails carry on with console-only
    # logging rather than taking the whole app down.
    file_handler = None
    for candidate in (LOG_FILE, None):
        if candidate is None:
            import tempfile
            candidate = str(Path(tempfile.gettempdir()) / "logicboard_debug.log")
        try:
            file_handler = logging.FileHandler(candidate, mode="w", encoding="utf-8")
            LOG_FILE = candidate
            break
        except OSError:
            continue

    handlers = [h for h in (file_handler, logging.StreamHandler(_sys.stderr))
                if h is not None]
    # Third-party libraries that are chatty at DEBUG. The root logger runs at
    # DEBUG so OUR diagnostics land in the file, but that also captures every
    # library's internals — Pillow alone probes ~40 optional image plugins on
    # first use and logs a failure line for each missing one
    # ("failed to import MicImagePlugin: No module named 'olefile'"), which
    # is normal and drowns the log we actually read when something breaks.
    for noisy in ("PIL", "PIL.Image", "PIL.PngImagePlugin",
                  "PIL.TiffImagePlugin", "fitz", "matplotlib",
                  "urllib3", "asyncio", "comtypes", "zeroconf"):
        try:
            logging.getLogger(noisy).setLevel(logging.WARNING)
        except Exception:
            pass

    logging.basicConfig(
        level=logging.DEBUG,
        format="%(asctime)s | %(levelname)s | %(message)s",
        handlers=handlers,
        force=True,
    )

    # Global excepthook — a windowed app has no console, so without this an
    # unhandled exception (e.g. raised inside a Qt slot) would vanish with
    # no trace. Route it to the log file so field crashes are diagnosable.
    def _log_uncaught(exc_type, exc_value, exc_tb):
        # Let Ctrl-C behave normally rather than dumping a scary traceback.
        if issubclass(exc_type, KeyboardInterrupt):
            _sys.__excepthook__(exc_type, exc_value, exc_tb)
            return
        logging.critical("UNCAUGHT EXCEPTION",
                         exc_info=(exc_type, exc_value, exc_tb))

    _sys.excepthook = _log_uncaught

    _enable_crash_log()

    logging.info("--- APP STARTUP ---")
    logging.info(f"Log file:   {LOG_FILE}")
    logging.info(f"Base path:  {base_path()}")
    logging.info(f"App dir:    {app_dir()}")
    logging.info(f"Assets dir: {assets_dir()}")
    logging.info(f"Saves dir:  {saves_dir()}")