"""
test_uninstall_elevation.py — the uninstaller's administrator hand-off.

Run it standalone:  python test_uninstall_elevation.py
(Needs MSVC. Nothing else — no gtest, no CMake, no payload ZIP.)

What this is protecting
-----------------------
The uninstaller re-launches itself through "runas" before it paints
anything, so the machine-wide leftovers a long-lived install accumulates
(HKLM keys, all-users shortcuts, firewall rules) are actually removable.
That elevation brings one hazard with it, and this file exists for that
hazard:

    "Elevated" and "the same user" are not the same thing.

When a standard user starts the uninstall and an administrator types their
credentials into the consent prompt, the elevated copy runs as the
ADMINISTRATOR. Its HKCU is the administrator's hive; SHGetFolderPath hands
back the administrator's Desktop. Cleaning up "the current user" there
wipes the wrong profile and leaves the real one listed in Apps & Features
with no way to remove it — a worse outcome than not elevating at all.

``ResolveInvokingUser`` is the guard, and the property that matters is the
negative one: for a different account it must return that account's folders
or NOTHING. Never ours.

How it works
------------
The functions under test are EXTRACTED VERBATIM from
LogicBoardInstaller.cpp and compiled into a small console program. Nothing
is retyped, so the test cannot drift away from the code it is testing —
edit a helper and this picks the edit up on the next run. The rest of the
installer (WinMain, GDI+, the 100 MB payload) is not involved.

What it does NOT cover
----------------------
The "runas" round trip itself. Verifying that needs a real consent prompt
on a real desktop, which no unattended test can answer.
"""
from __future__ import annotations

import re
import shutil
import subprocess
import sys
import tempfile
from pathlib import Path

HERE = Path(__file__).resolve().parent
SOURCE = HERE / "LogicBoardInstaller.cpp"

# Same compiler the installer is built with. _build_with_vcvars.bat names
# this path too; if Visual Studio moves, both want updating.
VCVARS = Path(r"C:\Program Files\Microsoft Visual Studio\18\Community"
              r"\VC\Auxiliary\Build\vcvars64.bat")


# ── pulling the helpers out of the installer ───────────────────────────────

def extract(src: str, signature_start: str) -> str:
    """From the line beginning ``signature_start`` to its closing brace.

    A struct ends ``};`` and a function ends ``}``, so take whichever
    terminator comes first — otherwise a struct swallows the function
    defined after it and the harness ends up with two copies of it, which
    is a confusing way to fail.
    """
    i = src.index(signature_start)
    cands = []
    for term in ("\n}\n", "\n};\n"):
        e = src.find(term, i)
        if e != -1:
            cands.append((e, len(term)))
    if not cands:
        raise SystemExit(f"no closing brace found for {signature_start!r}")
    j, width = min(cands)
    return src[i:j + width]


WANTED = [
    "static std::wstring RegReadString(",
    "static bool IsProcessElevated()",
    "static std::wstring CurrentUserSidString()",
    "static std::wstring ParseUserSid(",
    "struct PerUserRoots {",
    "static PerUserRoots ResolveInvokingUser(",
]

MAIN = r'''
static int fails = 0;
static void ok(bool cond, const char* what) {
    printf("  %s  %s\n", cond ? "ok   " : "FAIL ", what);
    if (!cond) fails++;
}

int wmain() {
    std::wstring me = CurrentUserSidString();
    wprintf(L"running as SID %s, elevated=%d\n\n",
            me.c_str(), (int)IsProcessElevated());
    ok(!me.empty(), "CurrentUserSidString returns something");
    ok(me.rfind(L"S-1-", 0) == 0, "and it looks like a SID");

    // -- ParseUserSid ---------------------------------------------------
    ok(ParseUserSid(L"/uninstall --elevated --user-sid:S-1-5-21-7-8-9-1001")
           == L"S-1-5-21-7-8-9-1001", "parses the SID off a real cmdline");
    ok(ParseUserSid(L"--user-sid:S-1-5-18 --elevated") == L"S-1-5-18",
       "stops at the space when another switch follows");
    ok(ParseUserSid(L"/uninstall") == L"", "absent switch -> empty");
    ok(ParseUserSid(nullptr) == L"", "null cmdline -> empty");
    ok(ParseUserSid(L"/uninstall --elevated") == L"",
       "--elevated alone is not mistaken for a SID");

    // -- same user: nothing changes -------------------------------------
    // The common case by far. The user who clicked Uninstall is an admin,
    // UAC hands back their own full token, and HKCU is already right.
    PerUserRoots self = ResolveInvokingUser(me);
    ok(self.root == HKEY_CURRENT_USER && self.prefix.empty(),
       "own SID -> plain HKCU, no prefix");
    ok(!self.foreign, "own SID -> not flagged foreign");
    ok(self.desktop.empty(),
       "own SID -> shell folders left to SHGetFolderPath");

    PerUserRoots none = ResolveInvokingUser(L"");
    ok(none.root == HKEY_CURRENT_USER && none.prefix.empty(),
       "empty SID -> plain HKCU (we elevated in place)");

    // SIDs come back uppercase from ConvertSidToStringSid, but nothing
    // guarantees the one that made the command-line round trip did.
    std::wstring lower = me;
    for (auto& c : lower) c = towlower(c);
    ok(!ResolveInvokingUser(lower).foreign,
       "own SID in lower case is still recognised as us");

    // -- a DIFFERENT account --------------------------------------------
    // S-1-5-18 is LocalSystem, whose hive is loaded on every running
    // Windows. It stands in for "an administrator elevated on the
    // teacher's behalf".
    if (me != L"S-1-5-18") {
        PerUserRoots other = ResolveInvokingUser(L"S-1-5-18");
        ok(other.root == HKEY_USERS, "other SID -> HKEY_USERS");
        ok(other.prefix == L"S-1-5-18\\", "and carries the SID prefix");
        ok(other.foreign, "and is flagged as a different account");
        wprintf(L"     its Desktop reads as: [%s]\n", other.desktop.c_str());

        // THE property. Reading one account's hive from another's token
        // can be refused even when the hive IS loaded, and this harness
        // runs unelevated so it usually is. Either way what must never
        // come back is OUR desktop: the caller would delete the wrong
        // profile's shortcut. Empty is the correct answer when we cannot
        // find out, and PerUserRoots::foreign is what stops the caller
        // quietly filling it in from the shell.
        wchar_t ours[MAX_PATH] = {};
        SHGetFolderPathW(nullptr, CSIDL_DESKTOPDIRECTORY, nullptr, 0, ours);
        ok(other.desktop.empty()
               || _wcsicmp(other.desktop.c_str(), ours) != 0,
           "a foreign account never resolves to OUR desktop");
        ok(other.desktop.find(L"%") == std::wstring::npos,
           "any path it returns is expanded, not a %VAR% template");
    }

    // -- a hive that is not loaded --------------------------------------
    // Logged off, or a SID that never existed. Falling back to our own
    // view is right here: there is nothing else to go on, and the common
    // case that reaches it is the SID simply being stale.
    PerUserRoots ghost = ResolveInvokingUser(L"S-1-5-21-0-0-0-31337");
    ok(ghost.root == HKEY_CURRENT_USER && ghost.prefix.empty(),
       "unloaded hive falls back to this process's own view");

    printf("\n%s\n", fails ? "FAILURES" : "all passed");
    return fails ? 1 : 0;
}
'''

PREAMBLE = r'''
#define _WIN32_WINNT 0x0A00
#define WINVER       0x0A00
#define NOMINMAX
#include <windows.h>
#include <shlobj.h>
#include <sddl.h>
#include <string>
#include <vector>
#include <cstdio>
#pragma comment(lib, "advapi32.lib")
#pragma comment(lib, "shell32.lib")
'''


def build_harness(src: str) -> str:
    consts = re.search(
        r"constexpr wchar_t ELEVATED_SWITCH.*?"
        r"constexpr wchar_t USER_SID_SWITCH[^\n]*\n", src, re.S)
    if not consts:
        raise SystemExit("the --elevated / --user-sid: constants moved")
    parts = [extract(src, sig) for sig in WANTED]
    return PREAMBLE + "\n" + consts.group(0) + "\n" + "\n".join(parts) + MAIN


def main() -> int:
    if not SOURCE.exists():
        print(f"[!] {SOURCE.name} not found next to this script")
        return 1
    if not VCVARS.exists():
        print(f"[!] MSVC not found at {VCVARS}\n"
              "    Install the Visual C++ Build Tools, or fix the path above.")
        return 1

    # Compile somewhere with an ASCII path. The project lives under a
    # directory with an emoji in its name and cl.exe cannot open files
    # through it.
    work = Path(tempfile.mkdtemp(prefix="lb_elev_test_"))
    try:
        cpp = work / "harness.cpp"
        cpp.write_text(build_harness(SOURCE.read_text(encoding="utf-8")),
                       encoding="utf-8")

        bat = work / "run.bat"
        bat.write_text(
            "@echo off\r\n"
            f'call "{VCVARS}" >nul\r\n'
            f'cd /d "{work}"\r\n'
            "cl /nologo /EHsc /std:c++17 /W3 harness.cpp /Fe:harness.exe "
            "/link advapi32.lib shell32.lib\r\n"
            "if errorlevel 1 exit /b 1\r\n"
            # Absolute path, not a bare name: NoDefaultCurrentDirectoryInExePath
            # is set on some machines, and cmd then refuses to run an exe
            # sitting in the directory it is standing in.
            f'"{work}\\harness.exe"\r\n'
            "exit /b %errorlevel%\r\n",
            encoding="ascii")

        r = subprocess.run(["cmd", "/c", str(bat)], capture_output=True,
                            text=True, errors="replace")
        # vcvars whinges about a missing vswhere on some installs and it
        # means nothing here; everything else is worth seeing.
        for line in (r.stdout + r.stderr).splitlines():
            if "vswhere" in line or "operable program" in line:
                continue
            print(line)
        return r.returncode
    finally:
        shutil.rmtree(work, ignore_errors=True)


if __name__ == "__main__":
    sys.exit(main())
