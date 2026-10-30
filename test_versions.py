"""
test_versions.py — every version literal agrees with versions.py.

    python test_versions.py

Why this is a test and not a comment
------------------------------------
It used to be a comment. resources.rc still carries the original:

    "Must stay in step with APP_VERSION_TEXT in LogicShareInstaller.cpp and
     APP_VERSION in LogicShare.py"

That instruction was followed by a human eight times per release, across
four languages, and it drifted exactly as you would expect — the LogicShare
installer's derive script was writing 1.0.0 into a tree whose app said
1.1.0. A version that disagrees with itself is not a cosmetic problem: the
installer's VERSIONINFO is what Windows shows in File Properties and what
the updater compares against, so a stale one makes a release look like a
downgrade or like no release at all.

So the instruction is now executable.
"""
from __future__ import annotations

import os
import re
import subprocess
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parent
sys.path.insert(0, str(ROOT))

import versions as V                                        # noqa: E402
import sync_versions as S                                   # noqa: E402


def test_the_declared_versions_look_like_versions():
    for name, value in (("LOGICBOARD", V.LOGICBOARD),
                        ("LOGICSHARE", V.LOGICSHARE)):
        assert re.fullmatch(r"\d+\.\d+\.\d+", value), \
            f"{name} is {value!r}, which is not MAJOR.MINOR.PATCH"
    assert isinstance(V.LOGICSHARE_ANDROID_BUILD, int), \
        "the Android build number has to be an integer"
    assert V.LOGICSHARE_ANDROID_BUILD > 0


def test_every_literal_in_the_ecosystem_matches():
    """THE test. Runs the real checker, so it cannot pass by knowing less
    than the thing it is checking."""
    r = subprocess.run([sys.executable, str(ROOT / "sync_versions.py"),
                        "--check"], capture_output=True, text=True)
    assert r.returncode == 0, (
        "a version literal has drifted from versions.py — run "
        "sync_versions.py.\n\n" + r.stdout + r.stderr)


def test_the_checker_knows_about_every_file_it_should():
    """A target that quietly stops matching is worse than drift: the check
    would go green while the file it was meant to guard went stale. The
    script already exits 2 in that case; this makes sure the list has not
    simply been emptied."""
    found = {what for _p, _pat, _rep, what in S.targets()}
    for expected in ("LogicBoard app", "LogicShare app",
                     "LogicBoard installer code", "LogicShare installer code",
                     "LogicShare Android build number", "LogicShare Android app"):
        assert expected in found, f"{expected} is no longer being checked"
    assert len(S.targets()) >= 14, \
        f"only {len(S.targets())} literals are being checked"


def test_the_windows_and_android_halves_of_logicshare_ship_together():
    """One product on two platforms. A teacher pairing a phone to a board
    should not have to reason about two LogicShare versions."""
    gradle = (ROOT / "LogicShare" / "LogicShare Mobile" / "app"
              / "build.gradle.kts").read_text(encoding="utf-8")
    py = (ROOT / "LogicShare" / "LogicShare Windows"
          / "LogicShare.py").read_text(encoding="utf-8")

    m = re.search(r'(?m)^\s*versionName\s*=\s*"([^"]+)"', gradle)
    assert m, "build.gradle.kts has no versionName"
    assert m.group(1) == V.LOGICSHARE
    c = re.search(r"(?m)^\s*versionCode\s*=\s*(\d+)", gradle)
    assert c and int(c.group(1)) == V.LOGICSHARE_ANDROID_BUILD
    w = re.search(r'(?m)^APP_VERSION\s*=\s*"([^"]+)"', py)
    assert w and w.group(1) == V.LOGICSHARE


def test_the_installer_version_block_is_a_four_part_number():
    """Win32 VERSIONINFO wants MAJOR,MINOR,PATCH,BUILD. A three-part value
    there does not fail the build, it just makes File Properties wrong."""
    assert S.comma_form("3.1.1") == "3,1,1,0"
    assert S.comma_form("1.1.1") == "1,1,1,0"
    for rc in (ROOT / "LogicBoard" / "LogicBoardInstaller" / "resources.rc",
               ROOT / "LogicShare" / "LogicShare Windows"
               / "LogicShareInstaller" / "resources.rc"):
        text = rc.read_text(encoding="utf-8")
        for key in ("FILEVERSION", "PRODUCTVERSION"):
            m = re.search(r"(?m)^%s\s+([\d,]+)" % key, text)
            assert m, f"{rc.name} has no {key}"
            assert len(m.group(1).split(",")) == 4, \
                f"{rc.name} {key} is {m.group(1)}, not four parts"


def test_a_release_is_one_edit():
    """The whole point. Bumping versions.py and running sync_versions.py has
    to be enough — nothing else may still hold a hand-written number."""
    import tempfile
    import shutil
    # Rehearse a bump against copies, so the real tree is untouched.
    with tempfile.TemporaryDirectory() as tmp:
        # Content AND timestamp. Writing identical bytes back still bumps
        # the mtime, and export_binaries decides whether a binary is stale
        # by comparing artifact mtime against the newest source mtime — so
        # a rehearsal that "restored" the tree made every freshly built
        # installer report itself out of date. Measured: a build finished
        # at 13:41 and this test, run at 13:45, aged all three.
        backup = {}
        for path, _pat, _rep, _what in S.targets():
            if path not in backup:
                st = path.stat()
                backup[path] = (path.read_text(encoding="utf-8"),
                                (st.st_atime, st.st_mtime))
        try:
            old_lb, old_ls = V.LOGICBOARD, V.LOGICSHARE
            V.LOGICBOARD, V.LOGICSHARE = "9.9.9", "8.8.8"
            r = subprocess.run(
                [sys.executable, "-c",
                 "import sys; sys.path.insert(0, %r);"
                 "import versions as V; V.LOGICBOARD='9.9.9';"
                 "V.LOGICSHARE='8.8.8';"
                 "import sync_versions as S; sys.exit(S.main())"
                 % str(ROOT)],
                capture_output=True, text=True, cwd=str(ROOT))
            assert r.returncode == 0, r.stdout + r.stderr
            for path, _pat, _rep, what in S.targets():
                text = path.read_text(encoding="utf-8")
                want = "9.9.9" if "LogicBoard" in what else "8.8.8"
                comma = want.replace(".", ",") + ",0"
                assert want in text or comma in text, \
                    f"{what} did not take the rehearsed bump"
        finally:
            V.LOGICBOARD, V.LOGICSHARE = old_lb, old_ls
            for path, (text, times) in backup.items():
                path.write_text(text, encoding="utf-8")
                os.utime(path, times)
    # And the tree is exactly as it was.
    r = subprocess.run([sys.executable, str(ROOT / "sync_versions.py"),
                        "--check"], capture_output=True, text=True)
    assert r.returncode == 0, "the rehearsal did not restore the tree"


def test_the_rehearsal_leaves_no_fingerprints_on_the_clock():
    """The rehearsal above rewrites every version literal and writes the
    original text back. That restores the CONTENT — and silently ages the
    tree, because writing identical bytes still moves the mtime.

    export_binaries decides a binary is stale by comparing it against the
    newest source mtime. So merely running this suite made three freshly
    built installers report themselves out of date, and the honest fix
    would have looked like a 4-minute rebuild of code that had not changed.
    Measured: build finished 13:41, suite ran 13:45, all three went stale.

    A test that perturbs the workspace it is testing is a bad test even
    when it passes."""
    watched = sorted({path for path, _p, _r, _w in S.targets()})
    before = {p: p.stat().st_mtime for p in watched}
    test_a_release_is_one_edit()
    aged = [p.name for p in watched if p.stat().st_mtime != before[p]]
    assert not aged, (
        "running the release rehearsal aged %d file(s) — every artifact "
        "built before this point now looks stale: %s" % (len(aged), aged))



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
