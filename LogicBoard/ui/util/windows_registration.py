"""
windows_registration.py — LogicBoard's Windows-native identity.

Whenever the installed executable path changes (fresh install, update to
a new versioned folder, repair), Windows needs to be told about it in
several places at once, or shortcuts / Start Menu / Apps & Features will
keep pointing at the old exe.

This module is the single owner of those touch-points. Nothing else
should write shortcuts, `Uninstall` registry keys, or AppUserModelID
metadata — those all funnel through the helpers here. That way the
installer / uninstaller can share the SAME code paths as the runtime
self-registration this module does at startup.

What gets registered — PER-USER, every one of them, no admin required:
  * ``Uninstall\\LogicBoard`` key (HKCU) — makes LogicBoard show up in
    Windows Settings → Apps → Installed Apps with our icon and version.
  * Desktop shortcut ``~/Desktop/LogicBoard.lnk``
  * Start Menu shortcut ``%APPDATA%/.../Programs/LogicBoard/LogicBoard.lnk``
    (inside a ``LogicBoard`` subfolder — this placement is what makes the
    app reliably show up in Windows / taskbar search)
  * App Paths ``LogicBoard.exe`` key — so Windows Search / the Run
    dialog find and launch LogicBoard by name.
  * Startup — an HKCU ``Run`` value carrying ``--background``, and NOTHING
    ELSE, so LogicBoard auto-starts its LogicShare service at login.

ONE LOCATION PER THING, AND IT IS THE PER-USER ONE
==================================================
This module used to write the all-users Desktop and Start Menu as well as
the per-user ones, and a Startup-FOLDER shortcut as well as the Run value.
Both were described as belt-and-suspenders. Neither was: Explorer MERGES the
all-users and per-user desktops into the single desktop you look at, and
Windows enumerates BOTH the Run key and the Startup folder when it builds the
startup list. So each pair produced a visible duplicate — two identical
LogicBoard icons on the desktop, two LogicBoard rows in Task Manager →
Startup.

Worse, it was a duplicate that could not be cleaned up, because the installer
believed the opposite: it wrote the all-users desktop shortcut and deleted the
per-user one, and then the app's next launch put the per-user one back. The
two halves undid each other on every update, forever.

The installer now writes per-user shortcuts only, in exactly the locations
below, under exactly these filenames. This module writes the same ones. Since
a shortcut is identified by its path, writing it twice can only ever overwrite
— never add a second icon.

Every one of these points at the CURRENT exe path, and every one is
refreshed on update. That last part matters most for the startup entry
and App Paths: if they aren't repointed after an update, the boot / a
Search-launch keeps invoking the OLD version's exe — whose folder the
installer wiped on its way in, leaving a dead entry.

All three live under a STABLE identity — the key name is just
``LogicBoard``, the shortcut file is ``LogicBoard.lnk``. Only the
``DisplayVersion`` field and the ``TargetPath`` change on updates.
Windows treats successive installs as ONE app receiving version bumps,
not N separate apps piling up.

Non-Windows platforms: every function is a no-op that returns quietly.
"""
from __future__ import annotations

import logging
import os
import subprocess
import sys
from pathlib import Path
from typing import Iterable, Optional

log = logging.getLogger(__name__)

# ---------------------------------------------------------------------------
# Stable identity — versionless, so successive updates are ONE app.
# ---------------------------------------------------------------------------
_APP_KEY_NAME       = "LogicBoard"          # HKCU\...\Uninstall\<this>
_DESKTOP_LNK_NAME   = "LogicBoard.lnk"
_STARTMENU_LNK_NAME = "LogicBoard.lnk"
# Not written any more — only removed. See _write_startup_entry.
_STARTUP_LNK_NAME   = "LogicBoard.lnk"
_PUBLISHER          = "LogicClassroom"
_APP_USER_MODEL_ID  = "YashJain.LogicBoard"
_APP_EXE_NAME       = "LogicBoard.exe"
# Argument passed to the startup shortcut so LogicBoard boots headless
# (LogicShare service only, UI deferred). Must match what app.py expects.
_STARTUP_ARG        = "--background"

# Public constants callers can import when they need to reference the
# identity themselves (e.g. a caller checking whether we're registered at
# all).
__all__ = [
    "register",
    "unregister",
    "current_exe_path",
    "APP_USER_MODEL_ID",
]

APP_USER_MODEL_ID = _APP_USER_MODEL_ID


# ============================================================================
# High-level API — call these
# ============================================================================

def register(exe_path: Path, version: str, *, icon_path: Optional[Path] = None,
             install_size_bytes: Optional[int] = None) -> None:
    """Register (or re-register) LogicBoard with Windows for the current
    executable path.

    Called on:
      * Very first launch — creates every touch-point.
      * Every subsequent launch — idempotent refresh (fingerprint check
        below skips no-op work).
      * After a successful update — the new versioned exe re-registers
        with its new path so shortcuts / Start Menu / Apps & Features
        all reflect the new location.

    On non-Windows this is a no-op.
    """
    if sys.platform != "win32":
        return

    exe_path = Path(exe_path).resolve()
    if not exe_path.exists():
        log.warning("register(): executable not found at %s — skipping",
                     exe_path)
        return

    # Only an INSTALLED copy may speak for LogicBoard. Every touch-point
    # here is shared with the installed app — one Uninstall key, one
    # desktop shortcut, one Run value — so whatever registers last wins.
    # A run from the source tree used to win: it pointed the desktop and
    # Start Menu shortcuts at ui\app.py and, finding no Uninstaller.exe
    # beside app.py, marked the Apps & Features entry NoRemove — the
    # greyed-out Uninstall button. The installer drops Uninstaller.exe next
    # to the exe it installs, so that file is what "installed" means; a dev
    # tree, a dist\ folder or an unzipped copy has none and leaves the
    # installed app's registration alone.
    if not _is_installed_copy(exe_path):
        log.info("register(): %s is not an installed copy — leaving the "
                 "installed app's registration alone", exe_path)
        return

    icon = icon_path or exe_path  # exe embeds its own icon as fallback

    # A fingerprint of (exe_path, version) lets us skip the whole
    # registration when nothing meaningful changed since last launch.
    # Cheap safety belt: the individual steps are all idempotent anyway,
    # but this avoids re-writing the registry + rebuilding the .lnk on
    # every plain start.
    #
    # The schema tag is bumped whenever the SET of things we register
    # changes, so existing installs re-run registration once to pick up
    # the change even if their exe path + version are unchanged:
    #   v2 - added startup-entry + App-Paths registration
    #   v3 - moved the Start Menu shortcut into a LogicBoard subfolder
    #        (Programs\LogicBoard\LogicBoard.lnk) for search visibility,
    #        matching the installer. Machines still on the old flat
    #        Programs\LogicBoard.lnk re-register once to relocate it.
    #   v4 - per-user only: dropped the all-users desktop / Start Menu
    #        copies and the Startup-folder shortcut, all of which were
    #        duplicates of something we already registered. Existing
    #        machines re-register once so the leftovers get deleted.
    #   v5 - the Uninstall key's NoRemove flag is cleared rather than set.
    #        Re-register once so an entry a source run marked unremovable
    #        gets its Uninstall button back.
    fingerprint = f"v5|{version}|{exe_path}|{icon}"
    if _fingerprint_matches(fingerprint):
        log.debug("Windows registration unchanged since last run — skipping.")
        return

    try:
        _write_uninstall_key(exe_path, version, icon, install_size_bytes)
    except Exception:
        log.exception("Uninstall key registration failed")
    try:
        _write_shortcuts(exe_path, icon)
    except Exception:
        log.exception("Shortcut refresh failed")
    try:
        # App Paths — lets Windows Search / the Run dialog find and launch
        # "LogicBoard" by name. MUST be refreshed on update or Search
        # keeps launching the previous version's exe.
        _write_app_paths(exe_path)
    except Exception:
        log.exception("App Paths registration failed")
    try:
        # Startup entry — LogicBoard auto-starts at login to run its
        # LogicShare background service. This is the one everybody forgets
        # to update: if it isn't refreshed, every boot after an update
        # launches the OLD exe from the OLD version folder (which the
        # installer wiped on its way in → broken startup).
        _write_startup_entry(exe_path, icon)
    except Exception:
        log.exception("Startup entry refresh failed")
    _save_fingerprint(fingerprint)
    log.info("Windows registration refreshed: exe=%s version=%s",
              exe_path, version)


def _is_installed_copy(exe_path: Path) -> bool:
    """Was this exe put here by the installer? It is the only thing that
    drops ``Uninstaller.exe`` beside the app, so that file is the mark."""
    exe_path = Path(exe_path)
    return (exe_path.suffix.lower() == ".exe"
            and (exe_path.parent / "Uninstaller.exe").is_file())


def unregister() -> None:
    """Fully remove every Windows touch-point. Called by an eventual
    uninstaller. Never called by the runtime — an update reuses the
    same identity, it doesn't clear it."""
    if sys.platform != "win32":
        return
    try:
        _delete_uninstall_key()
    except Exception:
        log.exception("Uninstall key removal failed")
    try:
        _delete_shortcuts()
    except Exception:
        log.exception("Shortcut removal failed")
    try:
        _delete_app_paths()
    except Exception:
        log.exception("App Paths removal failed")
    try:
        _delete_startup_entry()
    except Exception:
        log.exception("Startup entry removal failed")
    try:
        _clear_fingerprint()
    except Exception:
        pass


def current_exe_path() -> Path:
    """Path Windows currently thinks LogicBoard's executable lives at
    (reads back the ``InstallLocation`` value from the Uninstall key).
    Returns an empty ``Path`` if not registered. Used to tell whether
    we've been installed at all yet."""
    if sys.platform != "win32":
        return Path()
    try:
        import winreg  # type: ignore
        with winreg.OpenKey(winreg.HKEY_CURRENT_USER, _uninstall_key_path()) as k:
            val, _ = winreg.QueryValueEx(k, "InstallLocation")
            return Path(val)
    except Exception:
        return Path()


# ============================================================================
# Uninstall key (Apps & Features / Settings → Installed Apps)
# ============================================================================

def _uninstall_key_path() -> str:
    return (r"Software\Microsoft\Windows\CurrentVersion\Uninstall\\" +
            _APP_KEY_NAME)


def _hklm_uninstall_exists() -> bool:
    """True if a machine-wide (HKLM) Uninstall entry for LogicBoard
    already exists — i.e. the app was installed by the elevated
    installer. Used to avoid creating a DUPLICATE per-user (HKCU) entry
    that would make LogicBoard show up twice in Apps & Features."""
    try:
        import winreg  # type: ignore
        with winreg.OpenKey(winreg.HKEY_LOCAL_MACHINE,
                            _uninstall_key_path()):
            return True
    except (FileNotFoundError, OSError):
        return False


def _write_uninstall_key(exe_path: Path, version: str, icon: Path,
                          install_size_bytes: Optional[int]) -> None:
    """Populate HKCU Uninstall key so Windows lists LogicBoard in Settings
    → Apps → Installed Apps, with our icon, current version, and a
    working "Uninstall" button.

    IMPORTANT: if a machine-wide HKLM entry already exists (the elevated
    installer created one), we do NOT write a second HKCU entry — two
    entries would list LogicBoard twice in Apps & Features. We can't
    update the HKLM entry's version without admin, so its DisplayVersion
    goes slightly stale after a user-scope update; that's a far better
    trade than a confusing duplicate listing. When there's NO HKLM entry
    (pure portable / user-scope install), the HKCU entry below is the
    canonical one and gets the fresh version normally."""
    if _hklm_uninstall_exists():
        log.debug("HKLM Uninstall entry exists — skipping HKCU duplicate.")
        return
    import winreg  # type: ignore
    key_path = _uninstall_key_path()
    with winreg.CreateKey(winreg.HKEY_CURRENT_USER, key_path) as k:
        winreg.SetValueEx(k, "DisplayName",    0, winreg.REG_SZ, "LogicBoard")
        winreg.SetValueEx(k, "DisplayVersion", 0, winreg.REG_SZ, version)
        winreg.SetValueEx(k, "Publisher",      0, winreg.REG_SZ, _PUBLISHER)
        winreg.SetValueEx(k, "DisplayIcon",    0, winreg.REG_SZ, str(icon))
        winreg.SetValueEx(k, "InstallLocation", 0, winreg.REG_SZ,
                           str(exe_path.parent))
        # Uninstall command. The installer drops an Uninstaller.exe beside
        # the app and that is the only thing that can actually uninstall
        # anything — the runtime has no --uninstall of its own. (The app
        # used to write `"<exe>" --uninstall`, a flag nothing implements.)
        # register() only gets here for an installed copy, so the
        # uninstaller is always there to point at.
        uninstaller = exe_path.parent / "Uninstaller.exe"
        winreg.SetValueEx(k, "UninstallString", 0, winreg.REG_SZ,
                           f'"{uninstaller}" /uninstall')
        # NoRemove greys out the Uninstall button in Settings. Nothing
        # should set it, but older builds did whenever they ran from a
        # folder with no uninstaller (the source tree) — and a value that
        # is never written is also never cleared. Remove it on sight.
        try:
            winreg.DeleteValue(k, "NoRemove")
        except FileNotFoundError:
            pass
        # These extra flags disable the "Modify" / "Repair" buttons on
        # the Apps & Features entry — a future installer can flip them
        # on. NoModify/NoRepair are the standard way to express this.
        winreg.SetValueEx(k, "NoModify", 0, winreg.REG_DWORD, 1)
        winreg.SetValueEx(k, "NoRepair", 0, winreg.REG_DWORD, 1)
        if install_size_bytes and install_size_bytes > 0:
            # Windows expects KB, not bytes.
            winreg.SetValueEx(k, "EstimatedSize", 0, winreg.REG_DWORD,
                               max(1, install_size_bytes // 1024))


def _delete_uninstall_key() -> None:
    import winreg  # type: ignore
    try:
        winreg.DeleteKey(winreg.HKEY_CURRENT_USER, _uninstall_key_path())
    except FileNotFoundError:
        pass


# ============================================================================
# Shortcuts (Desktop + Start Menu)
# ============================================================================

def _desktop_dir() -> Optional[Path]:
    d = Path.home() / "Desktop"
    return d if d.exists() else None


def _startmenu_dir() -> Optional[Path]:
    """User-scope Start Menu Programs directory."""
    appdata = os.environ.get("APPDATA")
    if not appdata:
        return None
    d = Path(appdata) / "Microsoft" / "Windows" / "Start Menu" / "Programs"
    return d if d.exists() else None


# ---------------------------------------------------------------------------
# All-users locations. NOTHING is written to these any more — they are listed
# only so leftovers from older builds can be found and deleted. Writing them
# is what put a second icon on the desktop; see the module docstring.
# ---------------------------------------------------------------------------

def _common_desktop_dir() -> Optional[Path]:
    public = os.environ.get("PUBLIC")
    if public:
        d = Path(public) / "Desktop"
        return d if d.exists() else None
    return None


def _common_startmenu_dir() -> Optional[Path]:
    programdata = os.environ.get("ProgramData")
    if programdata:
        d = (Path(programdata) / "Microsoft" / "Windows"
             / "Start Menu" / "Programs")
        return d if d.exists() else None
    return None


def _desktop_shortcut_targets() -> Iterable[Path]:
    """The desktop shortcut: ``~/Desktop/LogicBoard.lnk``, and only that.

    The all-users desktop used to be in here too. It is what produced the
    duplicate icon — see the module docstring."""
    base = _desktop_dir()
    if base is not None:
        yield base / _DESKTOP_LNK_NAME


def _startmenu_shortcut_targets() -> Iterable[Path]:
    """The Start Menu shortcut, inside a "LogicBoard" SUBFOLDER
    (``%APPDATA%\\...\\Programs\\LogicBoard\\LogicBoard.lnk``), not flat in
    Programs. A shortcut in its own named folder is what makes Windows Search
    reliably surface the app in the taskbar / Start search, and it is exactly
    where the installer puts it — so a refresh here updates that shortcut
    rather than creating a second one somewhere else."""
    base = _startmenu_dir()
    if base is not None:
        yield base / "LogicBoard" / _STARTMENU_LNK_NAME


def _all_shortcut_targets() -> Iterable[Path]:
    """Every managed shortcut — desktop (flat) + Start Menu (subfolder).
    Iterated by unregister to delete them all."""
    yield from _desktop_shortcut_targets()
    yield from _startmenu_shortcut_targets()


def _write_shortcuts(exe_path: Path, icon: Path) -> None:
    """(Re)create the Desktop and Start Menu shortcuts pointing at
    ``exe_path``. Desktop shortcuts are flat; the Start Menu shortcut goes
    inside a ``LogicBoard`` subfolder (created if needed) to match the
    installer and keep the app visible in Windows Search. Uses a WScript
    VBS shim — the only 100%-stdlib way to author a .lnk without pywin32."""
    # Desktop (flat).
    for lnk in _desktop_shortcut_targets():
        try:
            _write_one_shortcut(lnk, exe_path, icon)
        except Exception:
            log.exception("Could not write desktop shortcut: %s", lnk)
    # Start Menu (subfolder — ensure the LogicBoard dir exists first).
    for lnk in _startmenu_shortcut_targets():
        try:
            lnk.parent.mkdir(parents=True, exist_ok=True)
            _write_one_shortcut(lnk, exe_path, icon)
        except Exception:
            log.exception("Could not write Start Menu shortcut: %s", lnk)
    _remove_unmanaged_shortcuts()


def _remove_unmanaged_shortcuts() -> None:
    """Delete shortcuts this module used to create and no longer does.

    Every one of these is a duplicate of something above — a second desktop
    icon, or a second entry in the startup list — left on machines that ran an
    older build. Without this they persist forever, because nothing else would
    ever look at them again.

    The all-users copies need administrator rights and are the installer's job
    (it removes them while elevated to clear out a Program Files install);
    attempting them here is free and simply fails when it cannot.
    """
    stale: list[Path] = []
    # Flat Programs\LogicBoard.lnk, from before the Start Menu shortcut moved
    # into its own folder.
    for base in (_startmenu_dir(), _common_startmenu_dir()):
        if base is not None:
            stale.append(base / _STARTMENU_LNK_NAME)
    # The all-users desktop twin.
    cd = _common_desktop_dir()
    if cd is not None:
        stale.append(cd / _DESKTOP_LNK_NAME)
    # Startup-FOLDER shortcuts — the Run value is the only startup
    # registration now.
    for base in (_startup_dir(), _common_startup_dir()):
        if base is not None:
            stale.append(base / _STARTUP_LNK_NAME)
    for lnk in stale:
        try:
            if lnk.exists():
                lnk.unlink()
                log.info("Removed a duplicate shortcut this build no longer "
                          "creates: %s", lnk)
        except OSError:
            pass                # all-users locations without admin: expected


def _write_one_shortcut(lnk: Path, exe: Path, icon: Path,
                        arguments: str = "") -> None:
    # Ensure the containing directory exists (Start Menu Programs
    # always does; belt-and-suspenders in case the user has an unusual
    # profile).
    lnk.parent.mkdir(parents=True, exist_ok=True)

    # VBS approach — CreateShortcut ships with every Windows since XP.
    # Writing to a temp .vbs then invoking cscript is the standard
    # stdlib-only pattern; no external dependency required.
    from ui.util.paths import temp_dir
    vbs = temp_dir() / "_make_shortcut.vbs"
    lines = [
        'Set oWS = WScript.CreateObject("WScript.Shell")',
        f'Set oLink = oWS.CreateShortcut("{lnk}")',
        f'oLink.TargetPath = "{exe}"',
        f'oLink.WorkingDirectory = "{exe.parent}"',
        f'oLink.IconLocation = "{icon}"',
        'oLink.Description = "LogicBoard"',
    ]
    # Only emit the Arguments line when we actually have arguments —
    # keeps the plain Desktop / Start Menu shortcuts argument-free.
    if arguments:
        lines.append(f'oLink.Arguments = "{arguments}"')
    lines.append('oLink.Save')
    vbs.write_text("\n".join(lines), encoding="utf-8")
    creationflags = getattr(subprocess, "CREATE_NO_WINDOW", 0)
    subprocess.run(
        ["cscript.exe", "//nologo", str(vbs)],
        stdout=subprocess.DEVNULL,
        stderr=subprocess.DEVNULL,
        creationflags=creationflags,
        check=False,
    )
    # Best-effort cleanup — leaving the VBS behind is harmless, but tidy.
    try: vbs.unlink()
    except OSError: pass


def _delete_shortcuts() -> None:
    for lnk in _all_shortcut_targets():
        try:
            if lnk.exists(): lnk.unlink()
        except OSError:
            pass
    # Remove the now-empty Start Menu "LogicBoard" subfolder(s) so we don't
    # leave an orphaned empty folder in the Start Menu.
    for base in (_startmenu_dir(), _common_startmenu_dir()):
        if base is None:
            continue
        folder = base / "LogicBoard"
        try:
            if folder.is_dir():
                # Only remove if empty (rmdir refuses a non-empty dir,
                # which is the safe behaviour we want).
                folder.rmdir()
        except OSError:
            pass


# ============================================================================
# App Paths (Windows Search / Run dialog "LogicBoard")
# ============================================================================

def _app_paths_key() -> str:
    return (r"Software\Microsoft\Windows\CurrentVersion\App Paths\\" +
            _APP_EXE_NAME)


def _write_app_paths(exe_path: Path) -> None:
    """Register the App Paths key so typing "LogicBoard" into Start /
    Run / Search finds and launches the CURRENT exe. Refreshed on every
    update so Search never launches a stale version.

    Note the installer writes this under HKLM (machine-wide); we write
    the HKCU (per-user) view. Windows Search reads both, and a per-user
    App Paths entry pointing at the fresh exe correctly shadows the
    machine-wide one from the original install."""
    import winreg  # type: ignore
    with winreg.CreateKey(winreg.HKEY_CURRENT_USER, _app_paths_key()) as k:
        # (Default) value = full path to the exe.
        winreg.SetValueEx(k, None, 0, winreg.REG_SZ, str(exe_path))
        # "Path" value = the dir to add to PATH while the app launches.
        winreg.SetValueEx(k, "Path", 0, winreg.REG_SZ, str(exe_path.parent))


def _delete_app_paths() -> None:
    import winreg  # type: ignore
    try:
        winreg.DeleteKey(winreg.HKEY_CURRENT_USER, _app_paths_key())
    except FileNotFoundError:
        pass


# ============================================================================
# Startup entry — Run registry value + Startup-folder shortcut
# ============================================================================
# LogicBoard auto-starts at login to run its LogicShare background service.
# The installer sets this up on every install, update included; startup
# registration then re-asserts it against the running exe. Either way it
# must never be left pointing at a previous version's folder — the
# installer wipes those, so a stale entry is one that silently does
# nothing at boot and LogicShare never comes up.

def _run_key_path() -> str:
    return r"Software\Microsoft\Windows\CurrentVersion\Run"


def _startup_dir() -> Optional[Path]:
    """User-scope Startup folder (apps here launch at login)."""
    appdata = os.environ.get("APPDATA")
    if not appdata:
        return None
    d = (Path(appdata) / "Microsoft" / "Windows" / "Start Menu"
         / "Programs" / "Startup")
    return d


def _common_startup_dir() -> Optional[Path]:
    """All-users Startup folder (where the elevated installer drops its
    startup shortcut). Refreshed on update so the installer's startup
    shortcut repoints in place rather than going stale."""
    programdata = os.environ.get("ProgramData")
    if not programdata:
        return None
    d = (Path(programdata) / "Microsoft" / "Windows" / "Start Menu"
         / "Programs" / "Startup")
    return d


def _startup_shortcut_targets() -> Iterable[Path]:
    """Startup-folder shortcuts. Only ever DELETED now — see
    ``_write_startup_entry``."""
    seen = set()
    for base in (_startup_dir(), _common_startup_dir()):
        if base is None:
            continue
        lnk = base / _STARTUP_LNK_NAME
        key = str(lnk).lower()
        if key in seen:
            continue
        seen.add(key)
        yield lnk


def _write_startup_entry(exe_path: Path, icon: Path) -> None:
    """Register LogicBoard to start at login — via the HKCU Run value, and
    only that.

    It used to write a Startup-FOLDER shortcut as well. Windows enumerates
    both the Run key and the Startup folder when it builds the startup list,
    so that showed LogicBoard TWICE in Task Manager → Startup and twice in
    Settings → Apps → Startup, with a switch next to each that only turned off
    half of it.

    HKCU rather than HKLM is the other deliberate half: starting LogicBoard is
    something THIS account wants, it needs no administrator rights, and
    Windows labels an HKCU Run entry by its value name ("LogicBoard") instead
    of the raw exe path.
    """
    import winreg  # type: ignore
    run_cmd = f'"{exe_path}" {_STARTUP_ARG}'
    with winreg.CreateKey(winreg.HKEY_CURRENT_USER, _run_key_path()) as k:
        winreg.SetValueEx(k, _APP_KEY_NAME, 0, winreg.REG_SZ, run_cmd)


def _delete_startup_entry() -> None:
    import winreg  # type: ignore
    # Remove the Run value.
    try:
        with winreg.OpenKey(winreg.HKEY_CURRENT_USER, _run_key_path(), 0,
                            winreg.KEY_SET_VALUE) as k:
            try:
                winreg.DeleteValue(k, _APP_KEY_NAME)
            except FileNotFoundError:
                pass
    except FileNotFoundError:
        pass
    # Remove the Startup-folder shortcut(s).
    for lnk in _startup_shortcut_targets():
        try:
            if lnk.exists():
                lnk.unlink()
        except OSError:
            pass


# ============================================================================
# Fingerprint cache — skip a full re-registration when nothing changed
# ============================================================================

def _fingerprint_path() -> Path:
    """Fingerprint lives in %LOCALAPPDATA%/LogicBoard/Config/ so it
    survives updates (different exe paths write the same fingerprint
    location) and doesn't clutter the install dir."""
    from ui.util.paths import config_dir
    return config_dir() / ".windows_registration_fingerprint"


def _fingerprint_matches(fingerprint: str) -> bool:
    try:
        return _fingerprint_path().read_text(encoding="utf-8") == fingerprint
    except OSError:
        return False


def _save_fingerprint(fingerprint: str) -> None:
    try:
        _fingerprint_path().write_text(fingerprint, encoding="utf-8")
    except OSError as e:
        log.debug("Could not persist registration fingerprint: %s", e)


def _clear_fingerprint() -> None:
    try:
        _fingerprint_path().unlink()
    except OSError:
        pass