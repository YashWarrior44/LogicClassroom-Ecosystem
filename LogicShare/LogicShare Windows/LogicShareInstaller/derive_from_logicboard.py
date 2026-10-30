"""Derive LogicShareInstaller.cpp from LogicBoardInstaller.cpp.

The two installers are the same program with a different product identity, so
the LogicShare source is GENERATED from the LogicBoard one rather than
hand-forked: a fix to the install pipeline, the update detection or the
uninstaller lands in both by re-running this, instead of being applied twice
and drifting the second time.

What is NOT mechanical gets an explicit edit below: the Welcome screen copy,
the feature list, and the startup-registration difference (LogicBoard runs a
board service at boot; LogicShare is a teacher's sender app and has no reason
to).
"""
import io
import sys
from pathlib import Path

HERE = Path(__file__).resolve().parent
DEFAULT_SRC = (HERE.parents[2] / "LogicBoard" / "LogicBoardInstaller"
               / "LogicBoardInstaller.cpp")
DEFAULT_DST = HERE / "LogicShareInstaller.cpp"

src_path = Path(sys.argv[1]) if len(sys.argv) > 1 else DEFAULT_SRC
dst_path = Path(sys.argv[2]) if len(sys.argv) > 2 else DEFAULT_DST
if not src_path.exists():
    raise SystemExit(f"LogicBoard installer source not found at {src_path}")
s = io.open(src_path, encoding="utf-8").read()


def sub(old, new, why, count=1):
    global s
    assert old in s, "MISSING: " + why
    assert s.count(old) == count, "COUNT %d != %d: %s" % (s.count(old), count, why)
    s = s.replace(old, new, count)


# ---------------------------------------------------------------------------
# 1. Targeted edits that must happen BEFORE the blanket rename, because they
#    match on LogicBoard-specific text that the rename would otherwise eat.
# ---------------------------------------------------------------------------

# Version — both sides read from the ecosystem's versions.py rather than
# being spelled out here.
#
# They used to be literals, and both were wrong: this matched L"3.1.0" (so it
# would simply fail once LogicBoard moved on) and wrote L"1.0.0" into a tree
# whose own app already said 1.1.0. A generator that hardcodes the version of
# the thing it generates from is a generator that breaks on the next release.
_VERSIONS = HERE.parents[2] / "versions.py"
_ns: dict = {}
exec(compile(_VERSIONS.read_text(encoding="utf-8"), str(_VERSIONS), "exec"),
     _ns)
sub(f'#define APP_VERSION_TEXT L"{_ns["LOGICBOARD"]}"',
    f'#define APP_VERSION_TEXT L"{_ns["LOGICSHARE"]}"',
    "version")

# AppUserModelID must match what LogicShare.py passes to
# SetCurrentProcessExplicitAppUserModelID, or the shell treats the shortcut
# and the running process as two different applications: pinning the taskbar
# icon then pins a second, dead entry.
sub('constexpr wchar_t APP_AUMID[]      = L"YashJain.LogicBoard";',
    'constexpr wchar_t APP_AUMID[]      = L"InnovaLogiX.LogicShare";',
    "aumid")

# Tagline under the logo on the Welcome screen.
sub('L"The smart classroom whiteboard"',
    'L"Send files straight to the board"',
    "tagline")

# About blurb, first-install variant.
sub('''              : L"LogicBoard helps teachers run a modern classroom - drawing, "
                L"presenting, and sharing notes, all in one place and fully "
                L"offline. Free to use, share and improve.",''',
    '''              : L"LogicShare lets teachers send files and announcements to the "
                L"classroom Smart Board straight from their own laptop - no "
                L"cables, no USB sticks. Free to use, share and improve.",''',
    "about blurb")

# What the product is, under the title on a first install.
sub('            subLine = L"A free, open-source whiteboard for classrooms";',
    '            subLine = L"Free, open-source file sharing for classrooms";',
    "subline")

# Feature list.
sub('''        const wchar_t* feats[] = {
            L"Smooth pen, highlighter & shape tools",
            L"Import PowerPoint & PDF slides as backgrounds",
            L"Multi-page boards with unlimited undo",
            L"LogicShare - send files across the classroom LAN",
            L"Palm rejection & stylus support for smart boards",
        };''',
    '''        const wchar_t* feats[] = {
            L"Finds every Smart Board on the Wi-Fi automatically",
            L"Send PDFs, slides, documents, images & video",
            L"Broadcast announcements to one board or all of them",
            L"Live transfer progress with speed + time remaining",
            L"Updates itself straight from GitHub",
        };''',
    "feature list")

# Update-mode blurb — the sentence about saved boards is LogicBoard's.
sub('''              ? L"This replaces the copy already on this PC with the latest "
                L"build. Your saved boards, settings and shortcuts stay "
                L"exactly where they are - only the app itself is swapped."''',
    '''              ? L"This replaces the copy already on this PC with the latest "
                L"build. Your teacher profile, announcement history and "
                L"shortcuts stay exactly where they are - only the app "
                L"itself is swapped."''',
    "update blurb")

# Uninstall confirmation copy.
sub('''        L"This will completely remove LogicBoard from your PC.\\n"
        L"Your saved boards and personal settings will stay untouched.",''',
    '''        L"This will completely remove LogicShare from your PC.\\n"
        L"Your profile and announcement history will stay untouched.",''',
    "uninstall confirm copy")

# ---------------------------------------------------------------------------
# 2. Startup registration.
#
# LogicBoard registers itself under HKCU\\Run with --background, because on a
# classroom board it has to be listening for incoming files before anyone logs
# into anything. LogicShare is the SENDING half and runs on a teacher's own
# laptop: it has nothing to listen for, so auto-starting it at every login
# would put a window (and a Zeroconf browser) in front of someone who did not
# ask for it. The registration is dropped; the uninstaller still CLEANS the
# value, so a machine that ever had a build which set it gets it removed.
# ---------------------------------------------------------------------------
run_start = s.index("        // Startup entry — LogicBoard auto-starts at login")
run_end = s.index("        // Step 6 — Apps & Features + Search registration.")
s = s[:run_start] + '''        // Startup entry — deliberately NOT registered.
        //
        // LogicBoard writes an HKCU\\\\Run value so the board is listening for
        // incoming files from the moment it boots. LogicShare is the other
        // half of that conversation: it runs on a teacher's own laptop and
        // only does anything while someone is actively sending something. An
        // app with nothing to wait for has no business launching itself at
        // every login, so there is no Run value here.
        //
        // The uninstaller still deletes that value (see UninstallWorker) —
        // cleaning up after a build that did set it costs one API call and
        // means nobody is left with an orphaned startup entry.

''' + s[run_end:]

# The now-unused ``bgArgs`` local went with it; make sure nothing references it.
assert "bgArgs" not in s, "bgArgs still referenced after dropping the Run entry"

# ---------------------------------------------------------------------------
# 3. Blanket identity rename. Ordered longest-first so the compound tokens are
#    consumed before the bare product name they contain.
# ---------------------------------------------------------------------------
for old, new in [
    ("LOGICBOARD_ZIP",          "LOGICSHARE_ZIP"),
    ("LogicBoardInstallerWnd",  "LogicShareInstallerWnd"),
    ("LogicBoardUninstallerWnd", "LogicShareUninstallerWnd"),
    ("LogicBoardInstaller",     "LogicShareInstaller"),
    ("TerminateLogicBoard",     "TerminateLogicShare"),
    ("LogicBoard",              "LogicShare"),
    ("logicboard",              "logicshare"),
]:
    s = s.replace(old, new)

assert "LogicBoard" not in s, "stray LogicBoard reference"
assert "logicboard" not in s, "stray logicboard reference"

# ---------------------------------------------------------------------------
# 4. Header comment — the derived file should say where it came from.
# ---------------------------------------------------------------------------
sub("""// =============================================================================
// LogicShareInstaller.cpp
//""",
    """// =============================================================================
// LogicShareInstaller.cpp
//
// GENERATED from LogicBoardInstaller.cpp — see
// LogicShareInstaller/derive_from_logicboard.py. The two
// installers are one program with two product identities, so fixes to the
// install pipeline belong in the LogicBoard source and get re-derived here
// rather than being applied twice.
//""",
    "header note")

io.open(dst_path, "w", encoding="utf-8", newline="\n").write(s)
print("wrote %s (%d bytes)" % (dst_path, len(s)))
