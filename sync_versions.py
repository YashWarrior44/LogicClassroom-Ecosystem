#!/usr/bin/env python3
"""
sync_versions.py — push the numbers in versions.py out to every copy.

    python sync_versions.py           # write them
    python sync_versions.py --check   # verify, change nothing (exit 1 if not)

The shipped products cannot import versions.py — a PyInstaller freeze, a
C++ installer and an APK each carry only their own tree — so each one holds
a literal. This is what keeps those literals honest: one edit in
versions.py, one run of this, and ``--check`` to prove none of them drifted
afterwards.

Each target below is (file, regex, replacement). The regex has to match the
DECLARATION and nothing else, which is why they are anchored rather than
matching a bare version string: every one of these files mentions old
versions in its comments, and a blind search-and-replace would rewrite the
history someone wrote down on purpose.
"""
from __future__ import annotations

import argparse
import re
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parent
sys.path.insert(0, str(ROOT))
import versions as V                                        # noqa: E402

LOGICBOARD = ROOT / "LogicBoard"
LS_WINDOWS = ROOT / "LogicShare" / "LogicShare Windows"
LS_MOBILE = ROOT / "LogicShare" / "LogicShare Mobile"


def comma_form(v: str) -> str:
    """"3.1.1" -> "3,1,1,0" — what a Win32 VERSIONINFO block wants."""
    parts = (v.split(".") + ["0", "0", "0", "0"])[:4]
    return ",".join(parts)


def targets():
    """(path, pattern, replacement, what) for every derived literal."""
    lb, ls = V.LOGICBOARD, V.LOGICSHARE
    out = []

    # ── LogicBoard ───────────────────────────────────────────────────────
    out.append((
        LOGICBOARD / "ui" / "util" / "version.py",
        r'(?m)^(APP_VERSION\s*=\s*")[^"]+(")',
        rf'\g<1>{lb}\g<2>', "LogicBoard app"))
    out.append((
        LOGICBOARD / "LogicBoardInstaller" / "LogicBoardInstaller.cpp",
        r'(?m)^(#define APP_VERSION_TEXT L")[^"]+(")',
        rf'\g<1>{lb}\g<2>', "LogicBoard installer code"))

    # ── LogicShare, Windows ──────────────────────────────────────────────
    out.append((
        LS_WINDOWS / "LogicShare.py",
        r'(?m)^(APP_VERSION\s*=\s*")[^"]+(")',
        rf'\g<1>{ls}\g<2>', "LogicShare app"))
    out.append((
        LS_WINDOWS / "LogicShareInstaller" / "LogicShareInstaller.cpp",
        r'(?m)^(#define APP_VERSION_TEXT L")[^"]+(")',
        rf'\g<1>{ls}\g<2>', "LogicShare installer code"))

    # ── the two VERSIONINFO blocks ───────────────────────────────────────
    for rc, v, what in (
        (LOGICBOARD / "LogicBoardInstaller" / "resources.rc", lb,
         "LogicBoard installer resources"),
        (LS_WINDOWS / "LogicShareInstaller" / "resources.rc", ls,
         "LogicShare installer resources"),
    ):
        out.append((rc, r'(?m)^(FILEVERSION\s+)[\d,]+',
                    rf'\g<1>{comma_form(v)}', what + " (FILEVERSION)"))
        out.append((rc, r'(?m)^(PRODUCTVERSION\s+)[\d,]+',
                    rf'\g<1>{comma_form(v)}', what + " (PRODUCTVERSION)"))
        out.append((rc, r'(VALUE "FileVersion",\s*")[^"]+(")',
                    rf'\g<1>{v}\g<2>', what + " (FileVersion)"))
        out.append((rc, r'(VALUE "ProductVersion",\s*")[^"]+(")',
                    rf'\g<1>{v}\g<2>', what + " (ProductVersion)"))

    # ── LogicShare, Android (Kotlin) ─────────────────────────────────────
    # Both values live in app/build.gradle.kts; the app reads them back at
    # runtime through BuildConfig, so this is the only Android literal.
    gradle = LS_MOBILE / "app" / "build.gradle.kts"
    out.append((
        gradle,
        r'(?m)^(\s*versionName\s*=\s*")[^"]+(")',
        rf'\g<1>{ls}\g<2>', "LogicShare Android app"))
    out.append((
        gradle,
        r'(?m)^(\s*versionCode\s*=\s*)\d+',
        rf'\g<1>{V.LOGICSHARE_ANDROID_BUILD}', "LogicShare Android build number"))

    return out


def main() -> int:
    ap = argparse.ArgumentParser(
        description="Push versions.py out to every derived literal.")
    ap.add_argument("--check", action="store_true",
                    help="report drift and change nothing")
    args = ap.parse_args()

    print(f"LogicBoard  {V.LOGICBOARD}")
    print(f"LogicShare  {V.LOGICSHARE}  "
          f"(Android build {V.LOGICSHARE_ANDROID_BUILD})")
    print()

    changed, stale, missing = [], [], []
    for path, pattern, repl, what in targets():
        if not path.exists():
            missing.append((what, path))
            print(f"  [!]  {what:<40} {path.name} is missing")
            continue
        text = path.read_text(encoding="utf-8")
        # How many lines COULD this rewrite? More than one and the script
        # would silently edit the first — which may be a version quoted in
        # a docstring, leaving the real declaration untouched and stale
        # while reporting success. Refusing is the only safe answer.
        hits = len(re.findall(pattern, text))
        if hits > 1:
            missing.append((what, path))
            print(f"  [!]  {what:<40} {hits} possible declarations in "
                  f"{path.name}; refusing to guess")
            continue
        new, n = re.subn(pattern, repl, text, count=1)
        if n == 0:
            missing.append((what, path))
            print(f"  [!]  {what:<40} declaration not found in {path.name}")
            continue
        if new == text:
            print(f"  ok   {what:<40} {path.name}")
            continue
        if args.check:
            stale.append((what, path))
            print(f"  DRIFT {what:<40} {path.name}")
        else:
            path.write_text(new, encoding="utf-8")
            changed.append((what, path))
            print(f"  wrote {what:<40} {path.name}")

    print()
    if missing:
        print("A declaration could not be found — this script and the file "
              "have gone out of step, which is worse than the drift it is "
              "meant to prevent.")
        return 2
    if args.check:
        if stale:
            print(f"{len(stale)} literal(s) do not match versions.py. "
                  "Run sync_versions.py to fix.")
            return 1
        print("every version literal matches versions.py")
        return 0
    print(f"{len(changed)} updated, {len(targets()) - len(changed)} "
          "already current")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
