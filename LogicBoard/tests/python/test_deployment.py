"""
test_deployment.py — where LogicBoard installs, and what it registers.

Runs under pytest, and also standalone
(``python tests/python/test_deployment.py``) because the repo's pytest install
is currently broken on Python 3.11.

What is worth protecting here
-----------------------------
Two pieces of code decide what a LogicBoard installation looks like:
``LogicBoardInstaller.cpp`` lays it down, and
``ui/util/windows_registration.py`` re-asserts it every time the app starts.
Nothing forces them to agree, and when they disagreed the result was not an
error — it was a machine that slowly accumulated junk. Two desktop icons.
Two rows in Task Manager → Startup. A firewall prompt on every release.

So these tests are mostly about AGREEMENT, and about the three properties
the whole per-user layout rests on:

  * The install path never contains a version, because to Windows Firewall
    the exe's path IS its identity. Version it and every release is a program
    the machine has never seen, which is exactly what happened on 3.0.0 →
    3.1.0. This is checked by reading the installer source, because the value
    that matters is a compile-time decision.

  * Nothing machine-wide is written on the normal path. The installer's
    manifest is asInvoker, it registers under HKCU, and the app does the
    same. Elevation exists in exactly one place: removing an old Program
    Files install.

  * Every shortcut has ONE home, and both sides name the same one. A shortcut
    is identified by its path, so agreeing on the path is what makes writing
    it twice an overwrite instead of a duplicate.

The C++ side is verified by reading its source rather than by running it: an
installer test that actually installed would need a machine to install onto,
and one that actually elevated would hang the suite on a UAC prompt with
nobody watching.
"""
import os
import re
import sys
from pathlib import Path

os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")
sys.path.insert(0, os.path.abspath(os.path.join(os.path.dirname(__file__),
                                                "..", "..")))

from ui.util import windows_registration as wr
from ui.util.version import APP_NAME, APP_VERSION

_HERE = Path(__file__).resolve().parents[2]
_INSTALLER_DIR = _HERE / "LogicBoardInstaller"
_CPP = _INSTALLER_DIR / "LogicBoardInstaller.cpp"
_MANIFEST = _INSTALLER_DIR / "manifest.xml"


def _src() -> str:
    return _CPP.read_text(encoding="utf-8", errors="replace")


def _have_installer() -> bool:
    return _CPP.exists()


# ── where the app is installed ──────────────────────────────────────────────

def test_the_installer_installs_into_the_user_profile():
    """%USERPROFILE%\\LogicBoard, resolved through the known-folder API.

    Not %LOCALAPPDATA% — that is where saved boards and settings live, and
    the two must stay separate — and not a hard-coded account name."""
    if not _have_installer():
        return
    src = _src()
    assert "FOLDERID_Profile" in src, \
        "the install root is no longer resolved from the user's profile"
    assert "static fs::path InstallDir()" in src
    # The user-data directory must not become the install directory.
    assert "FOLDERID_LocalAppData" not in src, \
        "the installer is reaching into %LOCALAPPDATA%, which holds user data"
    assert "yashw" not in src.lower(), "a developer's username is baked in"


def test_the_install_path_carries_no_version():
    """The firewall regression, guarded at its source.

    A Windows Firewall rule for a desktop app records the PROGRAM PATH. Put
    the version in the path and every release looks like a program that has
    never been allowed through, so Windows asks again — which is exactly what
    3.0.0 → 3.1.0 did, and exactly what the many 3.0.0 → 3.0.0 reinstalls
    before it did not, because those landed on the same path.
    """
    if not _have_installer():
        return
    src = _src()
    assert "VersionedFolderName" not in src, \
        ("the installer creates a per-version folder again — every update "
         "will ask for firewall permission")
    assert "_V\" + APP_VERSION" not in src, \
        "a version is being appended to an install path somewhere"


def test_program_files_is_only_ever_removed_never_installed_into():
    if not _have_installer():
        return
    src = _src()
    assert "LegacyProgramFilesDir" in src
    # The one function that names Program Files should be the legacy lookup.
    assert src.count("CSIDL_PROGRAM_FILES") == 1, \
        "Program Files is referenced somewhere other than legacy detection"
    # And the install pipeline must build its path from InstallDir().
    assert "fs::path installDir = InstallDir();" in src


# ── elevation ───────────────────────────────────────────────────────────────

def test_the_installer_does_not_ask_for_administrator_up_front():
    """An installer that elevates to run at all elevates on every update —
    on machines where the person doing the update often cannot approve it."""
    if not _MANIFEST.exists():
        return
    manifest = _MANIFEST.read_text(encoding="utf-8", errors="replace")
    assert 'level="asInvoker"' in manifest, \
        "the installer manifest requests elevation for the whole process"
    assert "requireAdministrator" not in manifest


def test_elevation_exists_only_where_it_is_actually_needed():
    """TWO places now, and the second one is deliberate.

    This used to assert exactly one — removing a Program Files leftover —
    and that was right when the uninstall did not elevate. It does now: an
    uninstall has more to remove than the install put down (HKLM keys,
    all-users shortcuts, firewall rules) and every one of those deletes
    used to fail silently for want of rights, which is how a machine ended
    up with LogicBoard still listed in Apps & Features after being
    uninstalled. See ElevateForUninstall.

    The point of the test is unchanged: elevation is not sprinkled about.
    It is named here so that a THIRD one has to be argued for rather than
    slipped in.
    """
    if not _have_installer():
        return
    src = _src()
    assert 'sei.lpVerb = L"runas"' in src, \
        "nothing can elevate — a Program Files install could not be removed"
    assert src.count('L"runas"') == 2, (
        "%d things elevate; there should be exactly two — removing a legacy "
        "install, and the uninstall" % src.count('L"runas"'))
    assert "RemoveLegacyInstall" in src
    assert "ElevateForUninstall" in src
    # The legacy one must only run when there is actually something to
    # remove. The uninstall one always runs, which is the whole point of it.
    assert "if (g_legacy.present) {" in src, \
        "elevation is requested unconditionally"


def test_the_install_itself_still_never_elevates():
    """The property the asInvoker manifest exists for: an update runs on a
    school board where whoever is standing at it cannot answer a consent
    prompt. Only the uninstall and the legacy cleanup may ask."""
    if not _have_installer():
        return
    src = _src()
    i = src.index("static int ElevateAndRemoveLegacy")
    j = src.index("static int ElevateForUninstall")
    # Both runas calls live inside those two functions and nowhere else.
    for pos in [k for k in range(len(src)) if src.startswith('L"runas"', k)]:
        in_legacy = i <= pos < src.index("\n}\n", i)
        in_uninst = j <= pos < src.index("\n}\n", j)
        assert in_legacy or in_uninst, (
            "something outside the two elevation helpers asks for runas")


def test_a_declined_prompt_stops_rather_than_installing_anyway():
    """Installing per-user on top of a Program Files copy leaves two
    LogicBoards, the old one still starting at boot from HKLM."""
    if not _have_installer():
        return
    src = _src()
    i = src.index("if (g_legacy.present) {")
    block = src[i:i + 2000]
    assert "was declined" in block
    assert "Nothing has been" in block
    assert "SetScreen(Screen::Error);" in block


# ── nothing machine-wide on the normal path ─────────────────────────────────

def test_the_installer_registers_under_hkcu():
    if not _have_installer():
        return
    src = _src()
    # Where the Uninstall / App Paths / Run values are WRITTEN.
    assert "RegCreateKeyExW(HKEY_CURRENT_USER, UNINSTALL_KEY" in src
    assert "RegCreateKeyExW(HKEY_CURRENT_USER, APP_PATHS_KEY" in src
    assert "RegCreateKeyExW(HKEY_CURRENT_USER, RUN_KEY" in src
    assert "RegCreateKeyExW(HKEY_LOCAL_MACHINE" not in src, \
        "the installer writes a machine-wide key again"


def test_hklm_appears_only_where_leftovers_are_cleaned_up():
    """HKLM is still read and deleted — that is how a legacy install is
    found and removed — but never created."""
    if not _have_installer():
        return
    # ``drop_run`` is a two-line helper that opens a Run key and DELETES a
    # value from it — a delete, which is what this rule allows. It is named
    # here because the check reads one line at a time and cannot see
    # through a call; the helper's own body is verified just below.
    allowed = ("RegReadString", "RegDeleteKeyW", "RegOpenKeyExW",
               "HKEY_CURRENT_USER", "drop_run")
    for line in _src().splitlines():
        if "HKEY_LOCAL_MACHINE" not in line:
            continue
        assert any(tok in line for tok in allowed), \
            f"unexpected machine-wide registry write: {line.strip()}"


def test_the_run_key_helper_only_ever_deletes():
    """The one indirection the rule above has to trust."""
    if not _have_installer():
        return
    src = _src()
    i = src.index("auto drop_run = [&]")
    body = src[i:src.index("};", i)]
    assert "RegDeleteValueW" in body, "drop_run no longer deletes"
    for forbidden in ("RegSetValueEx", "RegCreateKey"):
        assert forbidden not in body, \
            f"drop_run now WRITES to the registry: {forbidden}"


def test_the_app_registers_under_hkcu_only():
    src = Path(wr.__file__).read_text(encoding="utf-8", errors="replace")
    # The only HKLM reference left is the read that avoids duplicating a
    # machine-wide Apps & Features entry.
    for line in src.splitlines():
        if "HKEY_LOCAL_MACHINE" not in line:
            continue
        assert "OpenKey" in line, \
            f"the app writes a machine-wide registry key: {line.strip()}"


def test_no_service_or_scheduled_task_is_created():
    """Startup is a per-user Run value and nothing else."""
    if not _have_installer():
        return
    src = _src()
    for forbidden in ("CreateServiceW", "OpenSCManager", "schtasks",
                      "ITaskService", "TaskScheduler"):
        assert forbidden not in src, f"the installer creates a {forbidden}"


# ── shortcuts: one home each, and both sides agree on it ────────────────────

def test_the_installer_writes_only_per_user_shortcuts():
    if not _have_installer():
        return
    src = _src()
    # Find the shortcut-writing calls and check none target a common folder.
    for m in re.finditer(r"EnsureShortcut\(([^;]*?)\);", src, re.S):
        assert "COMMON" not in m.group(1).upper(), \
            f"a shortcut is written to an all-users folder: {m.group(1)[:80]}"
    # The all-users folders may still be referenced — to DELETE leftovers.
    assert "dropCommon(CSIDL_COMMON_DESKTOPDIRECTORY" in src, \
        "the old all-users desktop icon is never cleaned up"


def test_the_app_writes_only_per_user_shortcuts():
    """This is the half that caused the duplicate icon: the installer
    deleted the per-user copy and wrote the all-users one, then the app's
    next launch wrote the per-user one straight back."""
    desktop = list(wr._desktop_shortcut_targets())
    startmenu = list(wr._startmenu_shortcut_targets())
    assert len(desktop) <= 1, f"more than one desktop shortcut: {desktop}"
    assert len(startmenu) <= 1, f"more than one Start Menu shortcut: {startmenu}"
    public = os.environ.get("PUBLIC", "").lower()
    progdata = os.environ.get("ProgramData", "").lower()
    for lnk in desktop + startmenu:
        low = str(lnk).lower()
        assert not (public and low.startswith(public)), \
            f"the app writes to the all-users desktop again: {lnk}"
        assert not (progdata and low.startswith(progdata)), \
            f"the app writes to the all-users Start Menu again: {lnk}"


def test_both_sides_put_the_shortcuts_in_the_same_places():
    """A shortcut is identified by its path. Agreeing on the path is the
    whole reason writing it twice cannot produce two icons."""
    if not _have_installer():
        return
    src = _src()
    # Desktop: <user desktop>\LogicBoard.lnk
    assert "CSIDL_DESKTOPDIRECTORY" in src
    app_desktop = list(wr._desktop_shortcut_targets())
    if app_desktop:
        assert app_desktop[0].name == "LogicBoard.lnk"
    # Start Menu: <user programs>\LogicBoard\LogicBoard.lnk
    assert 'fs::path lbFolder = fs::path(startMenu) / L"LogicBoard";' in src
    assert "CSIDL_PROGRAMS" in src
    app_sm = list(wr._startmenu_shortcut_targets())
    if app_sm:
        assert app_sm[0].name == "LogicBoard.lnk"
        assert app_sm[0].parent.name == "LogicBoard"


def test_the_installer_reuses_an_existing_shortcut():
    """Re-running the installer, or upgrading from Program Files, must
    update the icon the user already has rather than add a second one."""
    if not _have_installer():
        return
    src = _src()
    assert "static bool EnsureShortcut(" in src
    i = src.index("static bool EnsureShortcut(")
    body = src[i:i + 1200]
    assert "ReadShortcut" in body and "SamePath" in body, \
        "EnsureShortcut no longer checks what is already there"
    assert "already correct" in body
    # And nothing may call the raw creator directly for a user-facing icon.
    assert "CreateShellShortcut(fs::path(commonDesk)" not in src


# ── startup: exactly one registration ───────────────────────────────────────

def test_startup_is_the_run_value_and_nothing_else():
    """Windows enumerates BOTH the Run key and the Startup folder, so
    registering both showed LogicBoard twice in Task Manager → Startup."""
    if not _have_installer():
        return
    src = _src()
    # The installer must not write a Startup-folder shortcut...
    i = src.index("// Step 5 — shortcuts + startup")
    j = src.index("// Step 6 —")
    step5 = src[i:j]
    assert "CSIDL_STARTUP" in step5, "the stale Startup shortcut is not removed"
    assert "EnsureShortcut" in step5
    for m in re.finditer(r"EnsureShortcut\(([^;]*?)\);", step5, re.S):
        assert "STARTUP" not in m.group(1).upper(), \
            "the installer writes a Startup-folder shortcut again"
    assert "RUN_VALUE_NAME" in step5

    # ...and neither may the app.
    reg_src = Path(wr.__file__).read_text(encoding="utf-8", errors="replace")
    k = reg_src.index("def _write_startup_entry(")
    end = reg_src.index("def _delete_startup_entry(")
    body = reg_src[k:end]
    assert "_write_one_shortcut" not in body, \
        "the app writes a Startup-folder shortcut again"
    assert "SetValueEx" in body


def test_both_sides_use_the_same_run_value_name():
    """Different names would be two startup entries, not one."""
    if not _have_installer():
        return
    m = re.search(r'RUN_VALUE_NAME\[\]\s*=\s*L"([^"]+)"', _src())
    assert m, "the installer no longer names its Run value"
    assert m.group(1) == wr._APP_KEY_NAME, (
        f"installer writes Run\\{m.group(1)} but the app writes "
        f"Run\\{wr._APP_KEY_NAME} — that is two startup entries")


def test_both_sides_pass_the_same_background_flag():
    if not _have_installer():
        return
    m = re.search(r'STARTUP_ARGS\[\]\s*=\s*L"([^"]+)"', _src())
    assert m and m.group(1) == wr._STARTUP_ARG, \
        "the installer and the app disagree about the startup argument"


# ── the app must not fight the installer over Apps & Features ───────────────

def test_the_app_does_not_overwrite_a_working_uninstall_command():
    """Both entries live in HKCU now. The app used to write
    ``"<exe>" --uninstall``, a flag nothing implements — which would replace
    the installer's working Uninstaller.exe command with a dead button."""
    src = Path(wr.__file__).read_text(encoding="utf-8", errors="replace")
    i = src.index("def _write_uninstall_key(")
    end = src.index("def _delete_uninstall_key(")
    # The literal that used to be written, comments about it aside.
    body = "\n".join(l for l in src[i:end].splitlines()
                     if not l.lstrip().startswith("#"))
    assert '{exe_path}" --uninstall' not in body, \
        "the app writes an uninstall command the runtime cannot honour"
    assert "Uninstaller.exe" in body
    # NoRemove is what greyed out the Uninstall button in Settings. The app
    # may only ever CLEAR it.
    assert 'SetValueEx(k, "NoRemove"' not in body, \
        "the app marks its Apps & Features entry unremovable again"
    assert 'DeleteValue(k, "NoRemove")' in body, \
        "an entry an older build marked unremovable is never healed"


def test_only_an_installed_copy_registers_with_windows():
    """A run from the source tree pointed the desktop and Start Menu
    shortcuts at ui\\app.py and greyed out Uninstall in Settings. Driven
    for real with every registry/shortcut writer replaced by a recorder."""
    import tempfile
    calls = []
    saved = {n: getattr(wr, n) for n in (
        "_write_uninstall_key", "_write_shortcuts", "_write_app_paths",
        "_write_startup_entry", "_save_fingerprint", "_fingerprint_matches")}
    try:
        for n in saved:
            setattr(wr, n, (lambda name: lambda *a, **k:
                            calls.append(name) or False)(n))
        with tempfile.TemporaryDirectory() as d:
            d = Path(d)
            src = d / "app.py"
            src.write_text("")
            exe = d / "LogicBoard.exe"
            exe.write_bytes(b"MZ")
            wr.register(src, "9.9.9")
            wr.register(exe, "9.9.9")       # an unzipped copy: no uninstaller
            assert "_write_uninstall_key" not in calls, \
                "a copy the installer did not put down claimed the " \
                "installed app's registration: %s" % calls
            (d / "Uninstaller.exe").write_bytes(b"MZ")
            wr.register(exe, "9.9.9")
            assert "_write_uninstall_key" in calls and \
                "_write_shortcuts" in calls, \
                "the installed copy did not register: %s" % calls
    finally:
        for n, f in saved.items():
            setattr(wr, n, f)


# ── firewall ────────────────────────────────────────────────────────────────

def test_the_firewall_rule_is_authored_not_faked():
    """The fix for the repeat prompts is a stable path plus, once, a real
    rule — never touching the binary's identity to fool Windows."""
    if not _have_installer():
        return
    src = _src()
    assert "INetFwRule" in src and "NET_FW_ACTION_ALLOW" in src
    # Public profile deliberately excluded — a classroom LAN is not a cafe.
    assert "NET_FW_PROFILE2_DOMAIN | NET_FW_PROFILE2_PRIVATE" in src
    assert "NET_FW_PROFILE2_PUBLIC" not in src
    # Idempotent: an existing rule for this path must not be duplicated.
    assert "FirewallHasRuleFor" in src


def test_stale_firewall_rules_are_only_removed_when_unambiguous():
    """The COM API removes rules BY NAME, and the prompt names every rule
    after the program — so one name can cover paths we are not replacing."""
    if not _have_installer():
        return
    src = _src()
    i = src.index("static int FirewallDropRulesUnder(")
    body = src[i:src.index("static bool FirewallHasRuleFor(")]
    assert "e.second.second != 0" in body, \
        "a shared rule name would be removed along with unrelated rules"


def test_the_firewall_work_happens_only_while_elevated():
    if not _have_installer():
        return
    src = _src()
    i = src.index("static int RemoveLegacyInstall(")
    body = src[i:src.index("static int ElevateAndRemoveLegacy(")]
    assert "FirewallAllowProgram" in body and "FirewallDropRulesUnder" in body
    # The install pipeline itself must not try — it has no rights to.
    j = src.index("static void RunInstall()")
    assert "FirewallAllowProgram" not in src[j:src.index("static void StartInstallThread()")], \
        "the unelevated install path tries to write a firewall rule"


# ── user data is never the install directory ────────────────────────────────

def test_user_data_lives_somewhere_the_installer_never_touches():
    """%LOCALAPPDATA%\\LogicBoard holds boards, settings and the LogicShare
    config. An install wipes its target folder, so the two must not meet."""
    from ui.util import paths
    saves = paths.saves_dir().resolve()
    if not getattr(sys, "frozen", False):
        return                      # dev tree keeps saves in the project
    profile = Path(os.environ.get("USERPROFILE", "")).resolve()
    install = profile / "LogicBoard"
    assert not str(saves).lower().startswith(str(install).lower()), \
        f"saves ({saves}) are inside the install folder ({install})"


def test_the_two_sides_agree_on_the_version():
    """The installer's version names what Apps & Features reports; the
    app's names what an update compares against."""
    if not _have_installer():
        return
    src = _src()
    m = re.search(r'#define APP_VERSION_TEXT L"([^"]+)"', src)
    assert m, "APP_VERSION_TEXT not found in the installer source"
    assert m.group(1) == APP_VERSION, (
        f"installer reports v{m.group(1)} but the app is v{APP_VERSION}")
    n = re.search(r'APP_NAME\[\]\s*=\s*L"([^"]+)"', src)
    assert n and n.group(1) == APP_NAME


if __name__ == "__main__":
    failures = []
    for name, fn in sorted(globals().items()):
        if not name.startswith("test_") or not callable(fn):
            continue
        try:
            fn()
            print(f"  ok    {name}")
        except AssertionError as exc:
            failures.append(name)
            print(f"  FAIL  {name}: {exc}")
        except Exception as exc:                       # noqa: BLE001
            failures.append(name)
            print(f"  ERROR {name}: {type(exc).__name__}: {exc}")
    print(f"\n{len(failures)} failed" if failures else "\nall passed")
    sys.exit(1 if failures else 0)
