r"""file_association.py — tell Windows which files LogicBoard opens.

Two kinds of file, registered two different ways:

* **.lb — LogicBoard's own format.** LogicBoard owns the extension outright:
  ``.lb`` points at our ProgID, so a double-click opens LogicBoard and
  Explorer draws the LogicBoard logo on every board file.

* **.pdf, .pptx, .ppt — other people's formats.** A teacher's PDFs already
  open in Edge or Acrobat and their decks in PowerPoint, and Windows has a
  record of that choice (``UserChoice``) which it protects with a hash
  precisely so that no installer can quietly take it over. So LogicBoard
  does what Windows asks an application to do instead:

      HKCU\Software\Classes\LogicBoard.PDF            the ProgID: name,
          DefaultIcon  -> pdf.ico                     LogicBoard's PDF icon,
          shell\open\command -> "<exe>" "%1"          how to open one
      HKCU\Software\Classes\.pdf\OpenWithProgids      "LogicBoard can open
          LogicBoard.PDF                               these" (Open with menu)
      HKCU\Software\LogicBoard\Capabilities           the app's entry in
      HKCU\Software\RegisteredApplications             Settings > Default apps

  That puts LogicBoard in every "Open with" menu for those files and gives
  it its own page in Default apps. The moment the user picks LogicBoard there
  (or ticks "Always" in Open with), Windows switches the files over to our
  ProgID — and with it to LogicBoard's PDF / PowerPoint icon, because the
  icon Explorer draws is the DefaultIcon of whichever ProgID owns the file.
  ``open_default_apps_settings`` takes the user straight to that page.

Design decisions
----------------
* **HKCU only.** Everything here is per-user and needs no administrator.

* **Only an installed copy registers.** Every key is shared with the
  installed app, so whatever registers last wins — and a run from the source
  tree or a ``dist`` folder used to win, leaving .lb double-clicks pointing at
  an exe that no longer existed. Same rule as ``windows_registration``: the
  installer drops ``Uninstaller.exe`` beside the exe, and that is what
  "installed" means.

* **Only re-register if something changed.** A fingerprint of every icon path
  and the command is kept in a marker file; an unchanged launch writes
  nothing and does not make Explorer refresh its icons.

* **Shell refresh via SHChangeNotify(SHCNE_ASSOCCHANGED)**, so new icons
  appear in already-open Explorer windows without restarting explorer.exe.

* **The uninstaller removes all of it** — see ``unregister`` here and the
  matching block in LogicBoardInstaller.cpp's UninstallWorker. Both lists are
  ``ALL_PROGIDS`` / ``DOCUMENT_TYPES``; a test holds the C++ side to them.
"""

from __future__ import annotations

import hashlib
import logging
import os
import sys
from pathlib import Path
from typing import Optional

logger = logging.getLogger(__name__)

# ── LogicBoard's own format ─────────────────────────────────────────────────
# ProgID — versioned so a future format bump doesn't collide.
PROGID = "LogicBoard.Board.v3"
FRIENDLY_NAME = "LogicBoard Board"
EXTENSION = ".lb"

# ── Documents LogicBoard can open but does not own ──────────────────────────
# extension -> (ProgID, Explorer "Type" column, icon file in assets/icons)
DOCUMENT_TYPES: dict[str, tuple[str, str, str]] = {
    ".pdf":  ("LogicBoard.PDF",  "PDF Document",                    "pdf.ico"),
    ".pptx": ("LogicBoard.PPTX", "PowerPoint Presentation",         "ppt.ico"),
    ".ppt":  ("LogicBoard.PPT",  "PowerPoint 97-2003 Presentation", "ppt.ico"),
}

ALL_PROGIDS = (PROGID,) + tuple(v[0] for v in DOCUMENT_TYPES.values())

# The name LogicBoard is registered under in Settings > Default apps. Also
# the deep link's ``registeredAppUser`` parameter, so the two must match.
REGISTERED_APP_NAME = "LogicBoard"
CAPABILITIES_KEY = r"Software\LogicBoard\Capabilities"
APP_DESCRIPTION = ("Open PDFs and PowerPoint presentations on the smart "
                   "board, as pages you can write on.")

# Bumped whenever the SET of things registered changes, so an existing
# install re-registers once even though its exe path is the same.
_MARKER_NAME = ".file_assoc_v2"

# PDF / PowerPoint are offered to Windows ONCE per install. After that,
# LogicBoard never puts back an entry that is gone — a teacher who removed
# it from "Open with", or an admin or Windows update that reset the
# associations, is not overruled on every launch. Entries that still exist
# only get their paths repaired (an update can move the exe or its icons;
# a stale command would be an Open-with item that does nothing).
#
# The flag lives in the registry beside the Capabilities key rather than in
# the saves folder: that folder survives an uninstall, and a reinstall must
# count as a fresh first run. The uninstaller deletes this value (see
# UninstallWorker in LogicBoardInstaller.cpp).
DOCS_ONCE_KEY = r"Software\LogicBoard"
DOCS_ONCE_VALUE = "DocumentTypesRegistered"


def register_file_association(icon_path: Optional[Path],
                              saves_dir: Path) -> bool:
    """Register .lb, .pdf, .pptx and .ppt with Windows for this user.
    Returns True if registration was performed OR was already up to date,
    False on a real failure or when this copy may not register. No-op off
    Windows.

    ``icon_path`` — the LogicBoard logo (.ico), used for .lb files and for
    the app itself. The PDF / PowerPoint icons are found beside it.

    ``saves_dir`` — where the "last-registered fingerprint" marker lives.
    """
    if sys.platform != "win32":
        return True   # nothing to do

    exe = _installed_exe()
    if exe is None:
        logger.info("File associations left alone: this is not an "
                    "installed copy of LogicBoard")
        return False

    if not icon_path or not Path(icon_path).exists():
        logger.warning("File association skipped: no icon file")
        return False
    logo = Path(icon_path)

    open_command = f'"{exe}" "%1"'
    icons = {ext: str(_document_icon(logo, ico))
             for ext, (_pid, _name, ico) in DOCUMENT_TYPES.items()}

    # Every launch, before the "nothing changed" early return: a per-user
    # choice can send .lb elsewhere at any time.
    reclaim_lb()

    fingerprint = _fingerprint(str(logo), open_command,
                               *(icons[e] for e in sorted(icons)))
    marker = saves_dir / _MARKER_NAME
    if _fingerprint_matches(marker, fingerprint):
        logger.debug("File associations already up to date; skipping")
        return True

    first_time = not documents_registered()
    ok = _write_registry(exe, str(logo), open_command, icons,
                         documents=first_time)
    if ok:
        if first_time:
            _mark_documents_registered()
        _write_fingerprint(marker, fingerprint)
        _refresh_shell()
        logger.info("Registered .lb%s with Windows",
                    " and offered .pdf/.pptx/.ppt (once)" if first_time
                    else "; PDF/PowerPoint entries only repaired")
    return ok


_LB_USERCHOICE = (r"Software\Microsoft\Windows\CurrentVersion\Explorer"
                  rf"\FileExts\{EXTENSION}\UserChoice")


def reclaim_lb() -> bool:
    """A .lb file ALWAYS opens in LogicBoard.

    The extension's own key already names LogicBoard's ProgID. The one thing
    that overrides it is a per-user "Always use this app" choice Windows
    records in UserChoice — made by an "Open with" click, or left behind by
    some other program. .lb is LogicBoard's own format and nothing else can
    open it, so a choice pointing anywhere else is only ever a mistake.
    Removing it hands the extension back to its registered handler.

    Only ever for .lb. PDF and PowerPoint belong to whichever app the user
    picks, and their choices are never touched.

    Returns True if a foreign choice was removed.
    """
    if sys.platform != "win32":
        return False
    try:
        import winreg
    except ImportError:
        return False
    def read(sub):
        try:
            with winreg.OpenKey(winreg.HKEY_CURRENT_USER, sub) as k:
                return str(winreg.QueryValueEx(k, "ProgId")[0])
        except OSError:
            return None

    hk = winreg.HKEY_CURRENT_USER
    removed = False
    # Windows records the choice in UserChoice (value ProgId) and, on
    # current Windows 11 builds, ALSO in UserChoiceLatest (sub-key ProgId,
    # value ProgId) — read off this machine's .pdf/.txt/.png. Either one
    # pointing elsewhere sends .lb elsewhere, so both are checked.
    for key, progid_at in ((_LB_USERCHOICE, _LB_USERCHOICE),
                           (_LB_USERCHOICE + "Latest",
                            _LB_USERCHOICE + "Latest\\ProgId")):
        got = read(progid_at)
        if got is None or got.lower() == PROGID.lower():
            continue
        try:
            if progid_at != key:
                winreg.DeleteKey(hk, progid_at)
            winreg.DeleteKey(hk, key)
            removed = True
            logger.info(".lb had been set to open with %r; handed back to "
                        "LogicBoard", got)
        except OSError:
            logger.warning(".lb is set to open with %r and Windows would not "
                           "let LogicBoard reset it", got, exc_info=True)
    if removed:
        _refresh_shell()
    return removed


def documents_registered() -> bool:
    """Has this install already offered itself for PDF / PowerPoint?"""
    if sys.platform != "win32":
        return False
    try:
        import winreg
        with winreg.OpenKey(winreg.HKEY_CURRENT_USER, DOCS_ONCE_KEY) as k:
            v, _ = winreg.QueryValueEx(k, DOCS_ONCE_VALUE)
            return bool(v)
    except OSError:
        return False


def _mark_documents_registered() -> None:
    try:
        import winreg
        with winreg.CreateKey(winreg.HKEY_CURRENT_USER, DOCS_ONCE_KEY) as k:
            winreg.SetValueEx(k, DOCS_ONCE_VALUE, 0, winreg.REG_DWORD, 1)
    except OSError:
        logger.debug("could not record the one-time registration",
                     exc_info=True)


def unregister() -> None:
    """Remove every key ``register_file_association`` writes. Leaves the
    extensions' own keys (``.pdf`` etc.) to the apps that own them — only
    our value inside their OpenWithProgids goes."""
    if sys.platform != "win32":
        return
    import winreg
    hk = winreg.HKEY_CURRENT_USER
    for pid in ALL_PROGIDS:
        _delete_tree(hk, rf"Software\Classes\{pid}")
    for ext, (pid, _n, _i) in DOCUMENT_TYPES.items():
        _delete_value(hk, rf"Software\Classes\{ext}\OpenWithProgids", pid)
    _delete_value(hk, rf"Software\Classes\{EXTENSION}\OpenWithProgids", PROGID)
    _delete_tree(hk, rf"Software\Classes\{EXTENSION}")
    _delete_tree(hk, r"Software\Classes\Applications\LogicBoard.exe")
    _delete_tree(hk, CAPABILITIES_KEY)
    _delete_value(hk, r"Software\RegisteredApplications", REGISTERED_APP_NAME)
    _delete_value(hk, DOCS_ONCE_KEY, DOCS_ONCE_VALUE)
    _refresh_shell()


# ═══════════════════════════════════════════════════════════════════════════
# The default-app question — Windows answers it, not us
# ═══════════════════════════════════════════════════════════════════════════

def is_default_for(ext: str) -> bool:
    """Has the user made LogicBoard the default app for ``ext``?

    Reads the per-user ``UserChoice`` Windows keeps. Only ever READ — it is
    hash-protected, and writing it is exactly what Windows does not allow.
    """
    if sys.platform != "win32":
        return False
    ext = ext.lower()
    want = PROGID if ext == EXTENSION else DOCUMENT_TYPES.get(ext, ("",))[0]
    if not want:
        return False
    try:
        import winreg
        sub = (r"Software\Microsoft\Windows\CurrentVersion\Explorer"
               rf"\FileExts\{ext}\UserChoice")
        with winreg.OpenKey(winreg.HKEY_CURRENT_USER, sub) as k:
            got, _ = winreg.QueryValueEx(k, "ProgId")
        return str(got).lower() == want.lower()
    except OSError:
        return False


def open_default_apps_settings() -> bool:
    """Open LogicBoard's own page in Settings > Default apps, where the user
    can hand PDF and PowerPoint files to LogicBoard. Falls back to the
    Default apps list on Windows builds without the per-app deep link."""
    if sys.platform != "win32":
        return False
    for uri in (f"ms-settings:defaultapps?registeredAppUser="
                f"{REGISTERED_APP_NAME}",
                "ms-settings:defaultapps"):
        try:
            os.startfile(uri)               # noqa: S606 — a settings URI
            return True
        except OSError:
            continue
    return False


# ═══════════════════════════════════════════════════════════════════════════
# Internals
# ═══════════════════════════════════════════════════════════════════════════

def _installed_exe() -> Optional[Path]:
    """The installed LogicBoard.exe we are running as, or None."""
    if not getattr(sys, "frozen", False):
        return None
    try:
        exe = Path(sys.executable).resolve()
    except OSError:
        return None
    from ui.util.windows_registration import _is_installed_copy
    return exe if _is_installed_copy(exe) else None


def _document_icon(logo: Path, name: str) -> Path:
    """``pdf.ico`` / ``ppt.ico`` beside the logo; the logo if one is missing
    (a LogicBoard-branded file is better than a blank one)."""
    p = logo.parent / name
    return p if p.is_file() else logo


def _fingerprint(*parts: str) -> str:
    """Deterministic hash of the state we're about to write. If this
    matches what's on disk we skip re-registration."""
    h = hashlib.sha1()
    for part in parts:
        h.update(part.encode("utf-8"))
        h.update(b"\x00")
    return h.hexdigest()


def _fingerprint_matches(marker: Path, fingerprint: str) -> bool:
    try:
        return marker.exists() and marker.read_text(encoding="utf-8").strip() == fingerprint
    except OSError:
        return False


def _write_fingerprint(marker: Path, fingerprint: str) -> None:
    try:
        marker.parent.mkdir(parents=True, exist_ok=True)
        marker.write_text(fingerprint, encoding="utf-8")
    except OSError:
        # Non-fatal — worst case we re-register next launch.
        logger.debug("Couldn't persist file-assoc fingerprint", exc_info=True)


def _write_progid(winreg, progid: str, name: str, icon: str,
                  open_command: str) -> None:
    base = rf"Software\Classes\{progid}"
    with winreg.CreateKey(winreg.HKEY_CURRENT_USER, base) as k:
        winreg.SetValueEx(k, "", 0, winreg.REG_SZ, name)
        # FriendlyTypeName is what Explorer's "Type" column shows.
        winreg.SetValueEx(k, "FriendlyTypeName", 0, winreg.REG_SZ, name)
    # The trailing ",0" is icon index 0 — harmless on a single .ico,
    # required when the path is an exe or dll.
    with winreg.CreateKey(winreg.HKEY_CURRENT_USER,
                          base + r"\DefaultIcon") as k:
        winreg.SetValueEx(k, "", 0, winreg.REG_SZ, f'"{icon}",0')
    with winreg.CreateKey(winreg.HKEY_CURRENT_USER,
                          base + r"\shell\open\command") as k:
        winreg.SetValueEx(k, "", 0, winreg.REG_SZ, open_command)


def _key_exists(winreg, sub: str) -> bool:
    try:
        winreg.CloseKey(winreg.OpenKey(winreg.HKEY_CURRENT_USER, sub))
        return True
    except OSError:
        return False


def _write_registry(exe: Path, logo: str, open_command: str,
                    icons: dict[str, str], *, documents: bool = True) -> bool:
    """Do the actual HKCU registry writes. Returns True on success.

    ``documents`` True: the one-time offer for .pdf / .pptx / .ppt (and the
    app's Open-with and Default-apps entries). False: those are only
    REPAIRED where they still exist — nothing that is gone is recreated.
    The .lb association is LogicBoard's own file type and is always kept
    current.
    """
    try:
        import winreg
    except ImportError:
        return False
    hk = winreg.HKEY_CURRENT_USER
    app_key = r"Software\Classes\Applications\LogicBoard.exe"
    try:
        # ── .lb: ours outright, always ─────────────────────────────────
        with winreg.CreateKey(hk, rf"Software\Classes\{EXTENSION}") as k:
            winreg.SetValueEx(k, "", 0, winreg.REG_SZ, PROGID)
        with winreg.CreateKey(
                hk, rf"Software\Classes\{EXTENSION}\OpenWithProgids") as k:
            winreg.SetValueEx(k, PROGID, 0, winreg.REG_NONE, b"")
        _write_progid(winreg, PROGID, FRIENDLY_NAME, logo, open_command)

        if not documents:
            # Repair, never recreate.
            for ext, (pid, name, _ico) in DOCUMENT_TYPES.items():
                if _key_exists(winreg, rf"Software\Classes\{pid}"):
                    _write_progid(winreg, pid, name, icons[ext], open_command)
            if _key_exists(winreg, app_key):
                with winreg.CreateKey(hk, app_key + r"\DefaultIcon") as k:
                    winreg.SetValueEx(k, "", 0, winreg.REG_SZ, f'"{logo}",0')
                with winreg.CreateKey(hk, app_key + r"\shell\open\command") as k:
                    winreg.SetValueEx(k, "", 0, winreg.REG_SZ, open_command)
            if _key_exists(winreg, CAPABILITIES_KEY):
                with winreg.CreateKey(hk, CAPABILITIES_KEY) as k:
                    winreg.SetValueEx(k, "ApplicationIcon", 0, winreg.REG_SZ,
                                      f'"{logo}",0')
            return True

        # ── .pdf / .pptx / .ppt: offered, never taken — ONCE ───────────
        # Note what is NOT written: the extension key's default value. That
        # belongs to whichever app the user chose, and overwriting it is the
        # takeover Windows' UserChoice hash exists to stop.
        for ext, (pid, name, _ico) in DOCUMENT_TYPES.items():
            _write_progid(winreg, pid, name, icons[ext], open_command)
            with winreg.CreateKey(
                    hk, rf"Software\Classes\{ext}\OpenWithProgids") as k:
                winreg.SetValueEx(k, pid, 0, winreg.REG_NONE, b"")

        # ── How the app itself appears in "Open with" ──────────────────
        with winreg.CreateKey(hk, app_key) as k:
            winreg.SetValueEx(k, "FriendlyAppName", 0, winreg.REG_SZ,
                              "LogicBoard")
        with winreg.CreateKey(hk, app_key + r"\DefaultIcon") as k:
            winreg.SetValueEx(k, "", 0, winreg.REG_SZ, f'"{logo}",0')
        with winreg.CreateKey(hk, app_key + r"\shell\open\command") as k:
            winreg.SetValueEx(k, "", 0, winreg.REG_SZ, open_command)
        with winreg.CreateKey(hk, app_key + r"\SupportedTypes") as k:
            for ext in (EXTENSION, *DOCUMENT_TYPES):
                winreg.SetValueEx(k, ext, 0, winreg.REG_SZ, "")

        # ── Settings > Default apps ────────────────────────────────────
        with winreg.CreateKey(hk, CAPABILITIES_KEY) as k:
            winreg.SetValueEx(k, "ApplicationName", 0, winreg.REG_SZ,
                              "LogicBoard")
            winreg.SetValueEx(k, "ApplicationDescription", 0, winreg.REG_SZ,
                              APP_DESCRIPTION)
            winreg.SetValueEx(k, "ApplicationIcon", 0, winreg.REG_SZ,
                              f'"{logo}",0')
        with winreg.CreateKey(hk, CAPABILITIES_KEY + r"\FileAssociations") as k:
            winreg.SetValueEx(k, EXTENSION, 0, winreg.REG_SZ, PROGID)
            for ext, (pid, _n, _i) in DOCUMENT_TYPES.items():
                winreg.SetValueEx(k, ext, 0, winreg.REG_SZ, pid)
        with winreg.CreateKey(hk, r"Software\RegisteredApplications") as k:
            winreg.SetValueEx(k, REGISTERED_APP_NAME, 0, winreg.REG_SZ,
                              CAPABILITIES_KEY)
        return True
    except OSError as e:
        logger.warning("Registry write failed for file associations: %s", e)
        return False


def _delete_tree(root, sub: str) -> None:
    import winreg
    try:
        winreg.DeleteKeyEx(root, sub)
        return
    except FileNotFoundError:
        return
    except OSError:
        pass        # has subkeys — walk them
    try:
        with winreg.OpenKey(root, sub) as k:
            children = []
            i = 0
            while True:
                try:
                    children.append(winreg.EnumKey(k, i))
                    i += 1
                except OSError:
                    break
        for c in children:
            _delete_tree(root, sub + "\\" + c)
        winreg.DeleteKeyEx(root, sub)
    except OSError:
        logger.debug("could not delete %s", sub, exc_info=True)


def _delete_value(root, sub: str, name: str) -> None:
    import winreg
    try:
        with winreg.OpenKey(root, sub, 0, winreg.KEY_SET_VALUE) as k:
            winreg.DeleteValue(k, name)
    except OSError:
        pass


def _refresh_shell() -> None:
    """Tell Explorer to re-read file associations so the new icons show
    up immediately in already-open windows."""
    try:
        import ctypes
        SHCNE_ASSOCCHANGED = 0x08000000
        SHCNF_IDLIST       = 0x0000
        ctypes.windll.shell32.SHChangeNotify(
            SHCNE_ASSOCCHANGED, SHCNF_IDLIST, None, None,
        )
    except Exception:
        logger.debug("SHChangeNotify failed (non-fatal)", exc_info=True)
