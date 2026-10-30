#!/usr/bin/env python3
"""
export_binaries.py — build all three shippables and collect them in one place.

    python export_binaries.py

Produces "Exported binaries/" next to this script:

    Exported binaries/
    ├── LogicBoard V3.1.1 Installer.exe      (Windows, single-file installer)
    ├── LogicShare V1.1.1 Installer.exe      (Windows, single-file installer)
    ├── LogicShare V1.1.1.apk                (Android, release)
    └── manifest.txt                         (versions, sizes, SHA-256)

What this is and is not
-----------------------
It is an ORCHESTRATOR. Each product already owns a working release pipeline
and this calls it rather than reimplementing it:

    LogicBoard          LogicBoard/scripts/package_installer.py
    LogicShare Windows  LogicShare/LogicShare Windows/package_installer.py
    LogicShare Android  gradlew assembleRelease   (Kotlin; was Flutter)

Those three know things this script should not have to — which DLLs the
engine actually links against, that the payload ZIP needs the exe at its
root, which PySide6 modules to exclude. Duplicating any of that here would
give the ecosystem two answers to the same question, and the one that went
stale would be this one.

So the only work that belongs here is the work that spans products: running
them in order, naming the outputs consistently, and saying plainly what
came out.

The emoji in the path
---------------------
This tree lives under "Source Code📜", and that single character defeats
MSVC, Gradle and Flutter — for two different reasons, which need two
different answers.

**cmd.exe** resolves a relative command name against an OEM-codepage copy
of the current directory, and that conversion fails on the emoji: `call
build.bat` reports "not recognized" while `dir` can see the file perfectly
well. ``ascii_path_to`` sidesteps that with a temporary directory junction,
the same way both Windows pipelines already do. A junction and not a copy:
the Android build tree is large and its outputs have to land in the real
project.

**AGP refuses outright.** "Your project path contains non-ASCII characters"
— thrown while applying the Android plugin, before a single resource is
compiled. And the junction does NOT rescue this one: Gradle canonicalises
the junction back to its target and sees the emoji again. What gets past it
is ``android.overridePathCheck=true`` in the Android project's own
gradle.properties, which is where that flag now lives.

Worth stating plainly because the junction looks like it should be enough,
and this script asserted that it was until the Android build was actually
run through it.

Exit codes
----------
    0  everything present and current
    1  a target is missing — its build failed, or there was nothing to collect
    2  bad arguments, or "Exported binaries" is not a usable directory
    3  everything present, but at least one binary is older than its source

Staleness
---------
``--collect-only`` skips every build and gathers whatever is already on
disk, which is what you want when you have just built by hand and only need
the folder assembled. That makes it possible to publish something older
than the source it claims to be, so every artifact is checked against the
newest source file of its own product and anything older is flagged — in
the summary, in the manifest, and in the exit code.
"""
from __future__ import annotations

import argparse
import hashlib
import os
import re
import shutil
import subprocess
import sys
import tempfile
import time
from dataclasses import dataclass, field
from pathlib import Path
from typing import Callable, List, Optional

ROOT = Path(__file__).resolve().parent
EXPORT_DIR = ROOT / "Exported binaries"

# Exit codes. 0 is "everything built and collected".
EXIT_FAILED = 1     # a target is missing from the export folder
EXIT_USAGE = 2      # bad arguments, or the output path is unusable
EXIT_STALE = 3      # all present, but at least one is older than its source

LOGICBOARD = ROOT / "LogicBoard"
LS_WINDOWS = ROOT / "LogicShare" / "LogicShare Windows"
LS_MOBILE = ROOT / "LogicShare" / "LogicShare Mobile"

# Files that count as SOURCE when deciding whether an artifact is stale. A
# whitelist rather than a list of things to ignore: a blacklist has to be
# right about every build artifact any of three toolchains might drop, and
# being wrong there silently disables the check.
SOURCE_SUFFIXES = {
    ".py", ".pyi",                       # LogicBoard + LogicShare
    ".cpp", ".c", ".h", ".hpp",          # engine, installers
    ".rc", ".xml", ".bat", ".manifest",  # installer resources
    ".gradle", ".kts", ".kt", ".java",   # Android (Kotlin + Gradle)
    ".pro",                              # R8/ProGuard rules
    ".dart", ".yaml", ".yml",            # Flutter-era names, harmless to keep
    ".ico", ".svg", ".png",              # art that ends up in a binary
    ".pyd", ".dll",                      # the prebuilt engine + its runtime
    ".spec",                             # PyInstaller specs
}
# Extensionless-ish names that ARE source. CMakeLists.txt is the only .txt
# that is; ".txt" as a blanket suffix also swept up LogicBoard's generated
# project_structure.txt, which regenerating would have reported as a reason
# to rebuild an installer it cannot affect.
SOURCE_NAMES = {
    "CMakeLists.txt",
    # android/gradle.properties carries android.overridePathCheck=true,
    # without which the Android build does not merely differ — it refuses
    # to start. By SUFFIX would also pull in local.properties, which holds
    # this machine's SDK path: different on every machine, and part of no
    # binary. So this one is by name.
    "gradle.properties",
}

# Deliberately NOT here: ".json". The only ones the walk reached were
# LogicShare's announcements.json and data.json, and those are RUNTIME
# STATE the app rewrites while it runs — so merely launching LogicShare
# would have declared its own installer out of date.

# Directories whose contents are never source, however they are named
# inside. Cuts the walk down enormously as well — LogicBoard/build alone
# holds tens of thousands of files.
SKIP_DIRS = {
    "build", "dist", "__pycache__", ".git", ".dart_tool", ".gradle",
    "saves", "logs", "node_modules", "Exported binaries", ".idea",
    ".vscode", "temps", ".working",
    # Tests are source, but not source that can reach a binary. Without
    # this, editing a test says the installer needs rebuilding — which it
    # measurably did: two of the three products' staleness verdicts were
    # being driven by a test file when this was written.
    "test", "tests",
}


def is_source(name: str) -> bool:
    """Does a FILE NAME look like something that can change a binary?"""
    if name in SOURCE_NAMES:
        return True
    stem, dot, suffix = name.rpartition(".")
    if not dot or f".{suffix.lower()}" not in SOURCE_SUFFIXES:
        return False
    # A test file that happens to sit beside the code rather than under a
    # tests/ directory — LogicShare Windows keeps test_announcements.py in
    # the project root, and LogicBoard's installer test sits next to the
    # .cpp it exercises.
    low = stem.lower()
    return not (low.startswith("test_") or low.endswith("_test"))


# ═══════════════════════════════════════════════════════════════════════════
# An emoji-free path, for toolchains that cannot cope with this one
# ═══════════════════════════════════════════════════════════════════════════

def _ascii_temp_base():
    """A directory with no non-ASCII characters in its path.

    The junction exists to hand a toolchain an ASCII path. Building it
    under ``tempfile.gettempdir()`` assumes TEMP is itself ASCII, and on
    Windows it often is not: TEMP lives inside the user's profile, so an
    account name with an accent in it puts one straight into the path —
    C:/Users/Jose/AppData/Local/Temp is fine, the same name spelled with
    an acute is not. The junction is then no more ASCII than the path it
    was standing in for, and the workaround silently delivers nothing:
    it looks like it worked and the build fails exactly as it would
    have without it.

    So the candidates are tried in order and the first ASCII one that
    can actually be written to wins.
    """
    import os
    import tempfile
    candidates = [tempfile.gettempdir()]
    drive = os.environ.get("SystemDrive", "C:")
    candidates.append(os.path.join(drive + os.sep, "Temp"))
    candidates.append(os.path.join(drive + os.sep, "lcx-build"))
    for base in candidates:
        try:
            if not str(base).isascii():
                continue
            os.makedirs(base, exist_ok=True)
            probe = os.path.join(base, ".lcx-write-probe")
            with open(probe, "w") as fh:
                fh.write("")
            os.remove(probe)
            return base
        except OSError:
            continue
    # Nothing ASCII is writable. Returning TEMP keeps the old behaviour —
    # which may still work, since only some toolchains care — rather than
    # refusing to build at all.
    return tempfile.gettempdir()


class ascii_path_to:
    """Context manager yielding an ASCII path to ``target``.

    Creates a directory junction under the system temp dir and removes it on
    exit. A junction, not a copy: an Android build tree is hundreds of
    megabytes and its outputs must land in the real project.

    Lifted deliberately from the two package_installer.py scripts rather
    than imported from one of them — importing would make the ecosystem
    root depend on a file inside one product, and this is thirty lines.
    """

    def __init__(self, target: Path) -> None:
        self.target = Path(target)
        self.link: Optional[Path] = None

    def __enter__(self) -> Path:
        if self.target.as_posix().isascii():
            return self.target
        self.link = Path(_ascii_temp_base()) / f"_lcx_{os.getpid()}"
        if self.link.exists():
            self._unlink()
        subprocess.run(
            ["cmd", "/c", "mklink", "/J", str(self.link), str(self.target)],
            check=True, stdout=subprocess.DEVNULL)
        return self.link

    def __exit__(self, *exc) -> bool:
        self._unlink()
        return False

    def _unlink(self) -> None:
        if not self.link:
            return
        try:
            # rmdir removes the junction itself, never what it points at.
            os.rmdir(self.link)
        except OSError:
            subprocess.run(["cmd", "/c", "rmdir", str(self.link)],
                            stdout=subprocess.DEVNULL,
                            stderr=subprocess.DEVNULL)
        self.link = None


# ═══════════════════════════════════════════════════════════════════════════
# Reading each product's version from the one place that owns it
# ═══════════════════════════════════════════════════════════════════════════

def _grep_version(path: Path, pattern: str) -> str:
    """First capture group of ``pattern`` in ``path``, or "0.0.0".

    Read with a regex rather than imported. LogicShare.py is a quarter of a
    megabyte of Qt application that builds windows at import time, and
    importing a GUI just to learn a version string would be an odd way to
    start a build.
    """
    try:
        text = path.read_text(encoding="utf-8", errors="replace")
    except OSError:
        return "0.0.0"

    # For PYTHON, ask the parser, not a regex.
    #
    # ``^APP_VERSION = "..."`` anchored with MULTILINE matches a line at the
    # start of a line — including a line inside a triple-quoted docstring,
    # which is exactly where old versions get quoted when someone writes
    # down what changed. Verified: a file whose docstring mentions
    # APP_VERSION = "9.9.9" and whose real declaration says "1.2.3"
    # returned 9.9.9. LogicShare.py is a quarter of a megabyte of code and
    # prose, so this is not a hypothetical shape.
    if path.suffix.lower() == ".py":
        import ast
        try:
            tree = ast.parse(text)
        except SyntaxError:
            tree = None
        if tree is not None:
            want = re.search(r"\^?\(?([A-Za-z_]+)", pattern)
            name = want.group(1) if want else None
            for node in tree.body:            # module level only
                if not isinstance(node, ast.Assign):
                    continue
                for tgt in node.targets:
                    if (isinstance(tgt, ast.Name) and tgt.id == name
                            and isinstance(node.value, ast.Constant)
                            and isinstance(node.value.value, str)):
                        return node.value.value

    # Everything else (pubspec, Dart) is small and has no docstrings.
    m = re.search(pattern, text, re.MULTILINE)
    return m.group(1) if m else "0.0.0"


def logicboard_version() -> str:
    return _grep_version(LOGICBOARD / "ui" / "util" / "version.py",
                         r'^APP_VERSION\s*=\s*"([^"]+)"')


def logicshare_version() -> str:
    return _grep_version(LS_WINDOWS / "LogicShare.py",
                         r'^APP_VERSION\s*=\s*"([^"]+)"')


def mobile_version() -> str:
    # versionName, not versionCode: the code is Android's internal build
    # number and belongs in the package, not in a filename somebody has to
    # read off a screen.
    return _grep_version(LS_MOBILE / "app" / "build.gradle.kts",
                         r'^\s*versionName\s*=\s*"([^"]+)"')


def _java_home() -> "str | None":
    """A JDK for Gradle: JAVA_HOME if set, else Android Studio's bundled one."""
    env = os.environ.get("JAVA_HOME")
    if env and (Path(env) / "bin").exists():
        return env
    for c in (Path(os.environ.get("ProgramFiles", r"C:\Program Files"))
              / "Android" / "Android Studio" / "jbr",
              Path(os.environ.get("LOCALAPPDATA", "")) / "Programs"
              / "Android Studio" / "jbr"):
        if (c / "bin").exists():
            return str(c)
    return None


def android_build_dir() -> Path:
    """Where Gradle puts the Android build — mirrors the rule in the
    project's build.gradle.kts. Under a non-ASCII checkout path (this one has
    an emoji in it) build output is relocated to an ASCII folder, because the
    unit-test JVM cannot load classes from a path it cannot URL-decode."""
    if str(LS_MOBILE.resolve()).isascii():
        return LS_MOBILE / "app" / "build"
    base = Path(os.environ.get("LOCALAPPDATA") or tempfile.gettempdir())
    return base / "LogicShareBuild" / "app"


# ═══════════════════════════════════════════════════════════════════════════
# The three builds
# ═══════════════════════════════════════════════════════════════════════════

def _run(cmd: List[str], cwd: Path) -> int:
    print(f"    $ {' '.join(str(c) for c in cmd)}")
    print(f"      (in {cwd})")
    print()
    # FLUSH before handing the console to the child.
    #
    # This is a fifteen-minute build and the only thing telling anyone it
    # is alive is what the child prints. Python buffers its own stdout when
    # it is not a terminal — piped to a file, or into a log — so all of
    # OUR headers queue up until the process exits while the children write
    # straight through. The result is a run that appears to print nothing
    # for a quarter of an hour and then emits everything at once, in the
    # wrong order: the "BUILD 1/3" banner arriving after the build it
    # announced.
    sys.stdout.flush()
    sys.stderr.flush()
    rc = subprocess.run([str(c) for c in cmd], cwd=str(cwd)).returncode
    sys.stdout.flush()
    return rc


def build_logicboard() -> int:
    return _run([sys.executable, LOGICBOARD / "scripts" / "package_installer.py"],
                cwd=LOGICBOARD)


def build_logicshare_windows() -> int:
    return _run([sys.executable, LS_WINDOWS / "package_installer.py"],
                cwd=LS_WINDOWS)


def build_logicshare_android() -> int:
    gradlew = LS_MOBILE / ("gradlew.bat" if os.name == "nt" else "gradlew")
    if not gradlew.exists():
        print(f"    [!] {gradlew.name} is missing from {LS_MOBILE}.")
        return 127
    java = _java_home()
    if java is None:
        print("    [!] No JDK found — set JAVA_HOME, or install Android Studio.")
        return 127
    os.environ["JAVA_HOME"] = java
    # Through a junction, which is necessary and NOT sufficient: it fixes
    # cmd.exe, while AGP's own non-ASCII path check is answered by
    # android.overridePathCheck=true in the project's gradle.properties.
    with ascii_path_to(LS_MOBILE) as build_dir:
        if build_dir != LS_MOBILE:
            print(f"    (building via {build_dir} — the real path has an "
                  f"emoji in it)")
        return _run([build_dir / gradlew.name, "assembleRelease",
                     "--console=plain"], cwd=build_dir)


# ═══════════════════════════════════════════════════════════════════════════
# What we are collecting
# ═══════════════════════════════════════════════════════════════════════════

@dataclass
class Target:
    key: str                      # what you pass to --only
    label: str                    # what gets printed
    source_root: Path             # walked for the staleness check
    artifact: Path                # what the build produces
    export_name: Callable[[], str]   # what it is called in the export folder
    build: Callable[[], int]
    # filled in as we go
    built: bool = False
    collected: Optional[Path] = None
    error: str = ""
    stale_by: float = 0.0         # seconds the artifact predates its source
    notes: List[str] = field(default_factory=list)

    def reset(self) -> None:
        """Forget the last run.

        TARGETS is module level, so these objects outlive a call to main().
        Without this a second run in the same process inherits the first
        one's verdict — and ``main`` skips collecting a target that already
        has an ``error``, so a build that failed once reported "does not
        exist" forever afterwards even with the artifact sitting there.
        Anyone scripting "run, fix, run again" got a stale answer, and so
        did the test suite.
        """
        self.built = False
        self.collected = None
        self.error = ""
        self.stale_by = 0.0
        self.notes = []


TARGETS: List[Target] = [
    Target(
        key="logicboard",
        label="LogicBoard (Windows installer)",
        source_root=LOGICBOARD,
        artifact=LOGICBOARD / "LogicBoardInstaller" / "LogicBoardInstaller.exe",
        export_name=lambda: f"LogicBoard V{logicboard_version()} Installer.exe",
        build=build_logicboard,
    ),
    Target(
        key="logicshare-windows",
        label="LogicShare (Windows installer)",
        source_root=LS_WINDOWS,
        artifact=(LS_WINDOWS / "LogicShareInstaller"
                  / "LogicShareInstaller.exe"),
        export_name=lambda: f"LogicShare V{logicshare_version()} Installer.exe",
        build=build_logicshare_windows,
    ),
    Target(
        key="logicshare-android",
        label="LogicShare (Android APK)",
        source_root=LS_MOBILE,
        artifact=(android_build_dir() / "outputs" / "apk" / "release"
                  / "app-release.apk"),
        export_name=lambda: f"LogicShare V{mobile_version()}.apk",
        build=build_logicshare_android,
    ),
]


# ═══════════════════════════════════════════════════════════════════════════
# Staleness — is this artifact older than the code it claims to be?
# ═══════════════════════════════════════════════════════════════════════════

def newest_source_mtime(root: Path) -> tuple[float, Optional[Path]]:
    """(mtime, path) of the most recently edited source file under ``root``."""
    newest, where = 0.0, None
    for dirpath, dirnames, filenames in os.walk(root):
        dirnames[:] = [d for d in dirnames if d not in SKIP_DIRS
                       and not d.startswith(".")]
        for name in filenames:
            if not is_source(name):
                continue
            p = Path(dirpath) / name
            try:
                m = p.stat().st_mtime
            except OSError:
                continue
            if m > newest:
                newest, where = m, p
    return newest, where


def check_staleness(t: Target) -> None:
    if not t.artifact.exists():
        return
    art = t.artifact.stat().st_mtime
    src, where = newest_source_mtime(t.source_root)

    if where is None:
        # Nothing was even looked at. Either the source root is wrong or
        # every file under it was filtered out, and BOTH of those make the
        # comparison below vacuously true — an artifact from last year
        # would be reported as current. A check that cannot fail is worse
        # than no check, because the summary still says it ran.
        t.notes.append(
            f"CANNOT CHECK — no source files found under {_rel(t.source_root)}"
            + ("; the directory does not exist"
               if not t.source_root.exists() else ""))
        return

    if src > art:
        t.stale_by = src - art
        rel = _rel(where) if where else "?"
        note = f"STALE — {rel} is newer than this binary"
        # Not appended blindly: check_staleness is called once per run, but
        # nothing stops a caller asking twice, and the manifest would then
        # print the same line two or three times over.
        if note not in t.notes:
            t.notes.append(note)


def human_age(seconds: float) -> str:
    if seconds < 90:
        return f"{int(seconds)}s"
    if seconds < 5400:
        return f"{seconds / 60:.0f} min"
    if seconds < 86400 * 2:
        return f"{seconds / 3600:.1f} hours"
    return f"{seconds / 86400:.1f} days"


def _rel(p: Path) -> str:
    """``p`` relative to the ecosystem root, or absolute if it is outside it.

    ``Path.relative_to`` RAISES on a path that is not underneath its
    argument, and both callers of this are on the error-reporting path —
    the code that runs when something has already gone wrong. An
    unguarded ValueError there does not just garble one message: it
    escapes main() and takes the other targets' results with it, turning
    "LogicShare failed, here is why, the other two are fine" into a
    traceback. Cosmetics are not worth that.
    """
    try:
        return str(p.relative_to(ROOT))
    except ValueError:
        return str(p)


def human_size(n: int) -> str:
    if n < 1024 * 1024:
        return f"{n / 1024:.0f} KB"
    return f"{n / 1024 / 1024:.1f} MB"


def sha256(path: Path) -> str:
    h = hashlib.sha256()
    with path.open("rb") as f:
        for chunk in iter(lambda: f.read(1024 * 1024), b""):
            h.update(chunk)
    return h.hexdigest()


# ═══════════════════════════════════════════════════════════════════════════
# Collect
# ═══════════════════════════════════════════════════════════════════════════

def rule(text: str) -> None:
    print()
    print("=" * 74)
    print(text)
    print("=" * 74)
    # See _run: a banner nobody sees until the end is not progress.
    sys.stdout.flush()


def collect(t: Target) -> None:
    if not t.artifact.exists():
        t.error = f"{_rel(t.artifact)} does not exist"
        return
    dest = EXPORT_DIR / t.export_name()
    # Copy, never move: the installers' own pipelines and the Flutter build
    # both expect their output to still be sitting where they left it, and
    # the next incremental build would otherwise start by rebuilding
    # something it had no reason to.
    #
    # Guarded because this is where a quarter of a gigabyte gets written and
    # the machine can simply be out of room. An uncaught OSError here would
    # abort the run after the builds had already succeeded, which is the
    # most expensive possible moment to throw away the other results.
    try:
        shutil.copy2(t.artifact, dest)
    except OSError as exc:
        t.error = f"could not copy to {EXPORT_DIR.name}: {exc.strerror or exc}"
        # A half-written file is worse than none: it would be collected by
        # the next run's "also in this folder" scan and hashed as if real.
        try:
            dest.unlink(missing_ok=True)
        except OSError:
            pass
        return
    t.collected = dest


def write_manifest(targets: List[Target], built: bool) -> Path:
    lines = [
        "Logic Classroom Ecosystem — exported binaries",
        "=" * 60,
        f"Collected: {time.strftime('%Y-%m-%d %H:%M:%S')}",
        f"Mode:      {'full build' if built else 'collect-only (no build run)'}",
        "",
    ]
    for t in targets:
        lines.append(t.label)
        if t.collected:
            st = t.collected.stat()
            lines += [
                f"  file    {t.collected.name}",
                f"  size    {human_size(st.st_size)}",
                f"  built   {time.strftime('%Y-%m-%d %H:%M:%S', time.localtime(t.artifact.stat().st_mtime))}",
                f"  sha256  {sha256(t.collected)}",
            ]
        else:
            lines.append(f"  MISSING — {t.error}")
        for n in t.notes:
            lines.append(f"  ! {n}")
        lines.append("")

    # Anything in the folder this run did not put there. Without this, a
    # --only run leaves a manifest that describes one file while three sit
    # beside it — and a manifest that is silent about a binary is worse
    # than no manifest, because the reader has no reason to doubt it.
    mine = {t.collected.name for t in targets if t.collected} | {"manifest.txt"}
    others = sorted(p for p in EXPORT_DIR.iterdir()
                    if p.is_file() and p.name not in mine)
    if others:
        lines += ["Also in this folder, from an earlier run", "-" * 40]
        for p in others:
            st = p.stat()
            lines.append(
                f"  {p.name}  ({human_size(st.st_size)}, collected "
                f"{time.strftime('%Y-%m-%d %H:%M', time.localtime(st.st_mtime))})")
            lines.append(f"    sha256  {sha256(p)}")
        lines.append("")

    stale = [t for t in targets if t.stale_by]
    if stale:
        lines += [
            "WARNING",
            "-------",
            "One or more binaries above are older than the source they were",
            "built from, so they do not contain the latest changes. Re-run",
            "without --collect-only to rebuild them.",
            "",
        ]

    path = EXPORT_DIR / "manifest.txt"
    path.write_text("\n".join(lines), encoding="utf-8")
    return path


# ═══════════════════════════════════════════════════════════════════════════

def main() -> int:
    ap = argparse.ArgumentParser(
        description="Build LogicBoard + LogicShare (Windows and Android) and "
                    "collect the installers into 'Exported binaries'.")
    ap.add_argument("--only", metavar="KEYS",
                    help="comma-separated subset of: "
                         + ", ".join(t.key for t in TARGETS))
    ap.add_argument("--collect-only", action="store_true",
                    help="skip every build; gather what is already on disk")
    ap.add_argument("--clean", action="store_true",
                    help="empty 'Exported binaries' before collecting")
    args = ap.parse_args()

    targets = TARGETS
    if args.only:
        wanted = {k.strip().lower() for k in args.only.split(",") if k.strip()}
        unknown = wanted - {t.key for t in TARGETS}
        if unknown:
            print(f"[!] unknown target(s): {', '.join(sorted(unknown))}")
            print(f"    known: {', '.join(t.key for t in TARGETS)}")
            return EXIT_USAGE
        targets = [t for t in TARGETS if t.key in wanted]

    for t in targets:
        t.reset()

    rule("Logic Classroom Ecosystem — export")
    print(f"Root:   {ROOT}")
    print(f"Output: {EXPORT_DIR}")
    print(f"Targets: {', '.join(t.key for t in targets)}")
    if args.collect_only:
        print("Mode:   collect-only — nothing will be rebuilt")

    if EXPORT_DIR.exists() and not EXPORT_DIR.is_dir():
        print(f"\n[!] {EXPORT_DIR} exists and is not a directory.")
        return 2
    if args.clean and EXPORT_DIR.exists():
        print(f"\nEmptying {EXPORT_DIR.name}...")
        shutil.rmtree(EXPORT_DIR, ignore_errors=True)
    EXPORT_DIR.mkdir(parents=True, exist_ok=True)

    # ---- build ----------------------------------------------------------
    if not args.collect_only:
        for i, t in enumerate(targets, 1):
            rule(f"BUILD {i}/{len(targets)}  {t.label}")
            started = time.perf_counter()
            try:
                rc = t.build()
            except Exception as exc:                      # noqa: BLE001
                rc, exc_text = 1, f"{type(exc).__name__}: {exc}"
                print(f"    [!] {exc_text}")
                t.error = exc_text
            # Every target is attempted even when an earlier one fails. A
            # three-product build is long, and stopping at the first failure
            # means finding out about the second one only after fixing the
            # first — two waits where one would have done. The summary and
            # the exit code carry the bad news instead.
            if rc != 0:
                t.error = t.error or f"build failed (exit {rc})"
                print(f"\n    [!] {t.label}: {t.error}")
            else:
                t.built = True
                print(f"\n    {t.label} finished in "
                      f"{human_age(time.perf_counter() - started)}")
                sys.stdout.flush()

    # ---- collect --------------------------------------------------------
    rule("COLLECT")
    for t in targets:
        check_staleness(t)
        if not t.error:
            collect(t)
        if t.collected:
            st = t.collected.stat()
            flag = "  [STALE]" if t.stale_by else ""
            print(f"  ok    {t.collected.name}  ({human_size(st.st_size)}){flag}")
        else:
            print(f"  FAIL  {t.label}: {t.error}")

    # The manifest is documentation, and the summary below is the actual
    # answer. If writing it fails, say so and carry on rather than losing
    # the report of a build that may have taken a quarter of an hour.
    try:
        manifest = write_manifest(targets, built=not args.collect_only)
        print(f"  ok    {manifest.name}")
    except OSError as exc:
        print(f"  FAIL  manifest.txt: {exc.strerror or exc}")

    # ---- report ---------------------------------------------------------
    ok = [t for t in targets if t.collected]
    bad = [t for t in targets if not t.collected]
    stale = [t for t in targets if t.stale_by]
    unchecked = [t for t in targets
                 if any(n.startswith("CANNOT CHECK") for n in t.notes)]

    rule("DONE" if not bad and not stale else "DONE — with problems")
    print(f"{len(ok)}/{len(targets)} collected into {EXPORT_DIR}")
    for t in bad:
        print(f"  missing: {t.label} — {t.error}")
    for t in stale:
        print(f"  stale:   {t.collected.name} — built "
              f"{human_age(t.stale_by)} before the newest source change")
    for t in unchecked:
        print(f"  UNCHECKED: {t.label} — its staleness could not be "
              f"determined, so 'current' here means nothing")
    if stale:
        print("\n  Re-run without --collect-only to rebuild the stale ones.")

    # Three outcomes, three codes, because a caller that shells out to this
    # wants to treat them differently: EXIT_STALE means every binary is
    # present and usable but one of them is behind its source, which is a
    # warning; EXIT_FAILED means something is missing outright.
    if bad:
        return EXIT_FAILED
    # An unverifiable staleness check is reported the same way a stale
    # binary is. The alternative is exiting 0 on a run whose central
    # promise — "this is current" — was never actually tested.
    return EXIT_STALE if (stale or unchecked) else 0


if __name__ == "__main__":
    raise SystemExit(main())
