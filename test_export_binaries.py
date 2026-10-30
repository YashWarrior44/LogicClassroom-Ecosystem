"""
test_export_binaries.py — the export orchestrator, including the parts a
real run would only reach after a fifteen-minute build.

    python test_export_binaries.py

Why this exists
---------------
The first cut of export_binaries.py was "tested" by running --collect-only
and reading the output. That exercises the collect path and the argument
parser and nothing else — not the three build functions, not the junction
the Android build depends on, and not one failure branch. Those are most of
the script, and all of the risk: a bug in them shows up only after a long
build, which is the worst possible moment to find out.

So the builds are dispatched against a recorded fake here. That is not a
weaker test than running them for real — it is a test of a DIFFERENT thing,
and the right thing for this file to own. Whether PyInstaller works is
PyInstaller's business; whether this script hands it the right command, in
the right directory, and copes with it failing, is this script's.

What is genuinely exercised for real
------------------------------------
``ascii_path_to``. It creates an actual directory junction on an actual
emoji path and reads a file back through it, because the Android build is
the one step with no fallback if that goes wrong, and mocking a filesystem
feature to prove a filesystem feature works would prove nothing.
"""
from __future__ import annotations

import os
import shutil
import sys
import tempfile
import time
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))

import export_binaries as X


# ── scaffolding ─────────────────────────────────────────────────────────────

class Recorder:
    """Stands in for ``_run``. Records calls, returns a scripted code."""

    def __init__(self, rc=0):
        self.calls = []
        self.saw = []
        self.rc = rc

    def __call__(self, cmd, cwd):
        cwd = Path(cwd)
        # Probe INSIDE the call. build_logicshare_android runs _run inside
        # the ascii_path_to block, so by the time __call__ has returned the
        # junction is already torn down and anything under it is gone.
        self.saw.append({
            "cwd": cwd,
            "ascii": str(cwd).isascii(),
            "is_android_project": (cwd / "app" / "build.gradle.kts").exists(),
        })
        self.calls.append(([str(c) for c in cmd], cwd))
        return self.rc if not callable(self.rc) else self.rc(cmd, cwd)


class patched:
    """Swap attributes on the module under test, and put them back."""

    def __init__(self, **kw):
        self.kw = kw
        self.old = {}

    def __enter__(self):
        for k, v in self.kw.items():
            self.old[k] = getattr(X, k)
            setattr(X, k, v)
        return self

    def __exit__(self, *exc):
        for k, v in self.old.items():
            setattr(X, k, v)
        return False


def _sandbox():
    """A throwaway EXPORT_DIR so tests never touch the real folder."""
    return Path(tempfile.mkdtemp(prefix="lcx_export_test_"))


def _fake_target(tmp, key="fake", artifact_bytes=b"binary", exists=True):
    tmp = Path(tmp)
    src = tmp / "src"
    src.mkdir(parents=True, exist_ok=True)
    (src / "thing.py").write_text("# source\n", encoding="utf-8")
    art = tmp / "out.bin"
    if exists:
        art.write_bytes(artifact_bytes)
    return X.Target(
        key=key, label=f"{key} (fake)", source_root=src, artifact=art,
        export_name=lambda: "Fake V1.0.0.bin",
        build=lambda: 0,
    )


# ═══════════════════════════════════════════════════════════════════════════
# The build commands — what actually gets run, and where
# ═══════════════════════════════════════════════════════════════════════════

def test_logicboard_runs_its_own_pipeline_and_not_a_copy_of_it():
    """The whole point of the orchestrator: call the product's script."""
    r = Recorder()
    with patched(_run=r):
        assert X.build_logicboard() == 0
    (cmd, cwd), = r.calls
    assert cmd[0] == sys.executable, cmd
    assert cmd[1].endswith(os.path.join("scripts", "package_installer.py")), cmd
    assert Path(cmd[1]).parent.parent.name == "LogicBoard", cmd
    assert cwd == X.LOGICBOARD


def test_logicshare_windows_runs_its_own_pipeline():
    r = Recorder()
    with patched(_run=r):
        assert X.build_logicshare_windows() == 0
    (cmd, cwd), = r.calls
    assert cmd[0] == sys.executable
    assert cmd[1].endswith("package_installer.py")
    assert cwd == X.LS_WINDOWS


def test_android_builds_a_release_apk():
    r = Recorder()
    with patched(_run=r):
        rc = X.build_logicshare_android()
    if rc == 127:
        print("     (no JDK on this machine — command shape not checked)")
        return
    (cmd, _cwd), = r.calls
    assert Path(cmd[0]).name in ("gradlew.bat", "gradlew"), cmd
    assert cmd[1] == "assembleRelease", cmd


def test_android_builds_through_an_ascii_path():
    """The emoji defeats Gradle. If this regresses, the Android build fails
    with something that looks nothing like a path problem."""
    r = Recorder()
    with patched(_run=r):
        rc = X.build_logicshare_android()
    if rc == 127:
        print("     (no JDK on this machine — skipped)")
        return
    seen, = r.saw
    assert seen["ascii"], f"built from a non-ASCII path: {seen['cwd']}"
    # And it must still be the Android project, not some other directory —
    # a junction pointing at the wrong place would also be ASCII.
    assert seen["is_android_project"], (
        f"{seen['cwd']} did not resolve to the Android project")


def test_a_missing_jdk_is_reported_and_not_a_traceback():
    with patched(_run=Recorder(), _java_home=lambda: None):
        rc = X.build_logicshare_android()
    assert rc == 127, rc


def test_the_apk_is_collected_from_where_gradle_actually_puts_it():
    """build.gradle.kts relocates build output when the checkout path is not
    ASCII. If the exporter looked in app/build regardless, it would report
    a fresh build as missing — or collect a stale one left there earlier."""
    target = next(t for t in X.TARGETS if t.key == "logicshare-android")
    expected_root = X.android_build_dir()
    assert str(target.artifact).startswith(str(expected_root)), target.artifact
    if not str(X.LS_MOBILE.resolve()).isascii():
        assert str(expected_root).isascii(), expected_root


def test_every_target_points_at_a_pipeline_that_exists_on_disk():
    """Cheap, and it catches the failure mode where a product's script is
    renamed and nobody notices until release day."""
    assert (X.LOGICBOARD / "scripts" / "package_installer.py").exists()
    assert (X.LS_WINDOWS / "package_installer.py").exists()
    assert (X.LS_MOBILE / "app" / "build.gradle.kts").exists()
    assert (X.LS_MOBILE / "gradlew.bat").exists()


# ═══════════════════════════════════════════════════════════════════════════
# The junction — exercised for real, on a real emoji path
# ═══════════════════════════════════════════════════════════════════════════

def test_the_junction_gives_a_readable_ascii_path_to_an_emoji_directory():
    base = Path(tempfile.mkdtemp(prefix="lcx_junc_"))
    emoji = base / "Source Code\U0001F4DC" / "project"
    emoji.mkdir(parents=True)
    (emoji / "pubspec.yaml").write_text("name: probe\n", encoding="utf-8")
    try:
        with X.ascii_path_to(emoji) as p:
            assert str(p).isascii(), p
            assert p != emoji, "no junction was made for an emoji path"
            assert (p / "pubspec.yaml").read_text().strip() == "name: probe", \
                "the junction does not reach the real directory"
            link = p
        assert not link.exists(), "the junction was left behind"
        assert (emoji / "pubspec.yaml").exists(), \
            "removing the junction removed the real files — catastrophic"
    finally:
        shutil.rmtree(base, ignore_errors=True)


def test_a_write_through_the_junction_lands_in_the_real_directory():
    """This is how the APK gets out: Gradle writes to the junction path."""
    base = Path(tempfile.mkdtemp(prefix="lcx_junc_"))
    emoji = base / "Code\U0001F4DC" / "proj"
    emoji.mkdir(parents=True)
    try:
        with X.ascii_path_to(emoji) as p:
            (p / "artifact.apk").write_bytes(b"apk")
        assert (emoji / "artifact.apk").read_bytes() == b"apk"
    finally:
        shutil.rmtree(base, ignore_errors=True)


def test_an_ascii_path_is_left_alone_entirely():
    """No junction, no temp directory, no cleanup to get wrong."""
    plain = Path(tempfile.mkdtemp(prefix="lcx_plain_"))
    try:
        with X.ascii_path_to(plain) as p:
            assert p == plain
    finally:
        shutil.rmtree(plain, ignore_errors=True)


def test_the_junction_is_removed_even_when_the_build_raises():
    base = Path(tempfile.mkdtemp(prefix="lcx_junc_"))
    emoji = base / "C\U0001F4DC" / "proj"
    emoji.mkdir(parents=True)
    seen = {}
    try:
        try:
            with X.ascii_path_to(emoji) as p:
                seen["link"] = p
                raise RuntimeError("build blew up")
        except RuntimeError:
            pass
        assert not seen["link"].exists(), \
            "an exception leaked the junction into the temp directory"
    finally:
        shutil.rmtree(base, ignore_errors=True)


# ═══════════════════════════════════════════════════════════════════════════
# Failure paths — none of which a successful run ever visits
# ═══════════════════════════════════════════════════════════════════════════

def test_a_failed_build_is_reported_and_not_collected():
    """Publishing yesterday's binary because today's build failed is the
    worst outcome available: it looks like success."""
    tmp = _sandbox()
    t = _fake_target(tmp)
    t.build = lambda: 1
    try:
        with patched(EXPORT_DIR=tmp / "out", TARGETS=[t]):
            rc = X.main()
        assert rc == X.EXIT_FAILED, rc
        assert not (tmp / "out" / "Fake V1.0.0.bin").exists(), \
            "a failed build still got published"
    finally:
        shutil.rmtree(tmp, ignore_errors=True)


def test_a_build_that_raises_is_caught_and_named():
    tmp = _sandbox()
    t = _fake_target(tmp)

    def boom():
        raise OSError("toolchain went missing")
    t.build = boom
    try:
        with patched(EXPORT_DIR=tmp / "out", TARGETS=[t]):
            rc = X.main()
        assert rc == X.EXIT_FAILED
        assert "toolchain went missing" in t.error, t.error
    finally:
        shutil.rmtree(tmp, ignore_errors=True)


def test_one_target_failing_does_not_stop_the_others():
    """A three-product build is long. Stopping at the first failure means
    finding out about the second only after fixing the first."""
    tmp = _sandbox()
    bad = _fake_target(tmp / "a", key="bad")
    good = _fake_target(tmp / "b", key="good")
    bad.build = lambda: 9
    good.export_name = lambda: "Good V1.0.0.bin"
    try:
        with patched(EXPORT_DIR=tmp / "out", TARGETS=[bad, good]):
            (tmp / "out").mkdir()
            rc = X.main()
        assert rc == X.EXIT_FAILED
        assert good.collected is not None, "the second target never ran"
        assert (tmp / "out" / "Good V1.0.0.bin").exists()
    finally:
        shutil.rmtree(tmp, ignore_errors=True)


def test_a_missing_artifact_after_a_successful_build_is_still_a_failure():
    """A pipeline that exits 0 without producing anything must not pass."""
    tmp = _sandbox()
    t = _fake_target(tmp, exists=False)
    try:
        with patched(EXPORT_DIR=tmp / "out", TARGETS=[t]):
            rc = X.main()
        assert rc == X.EXIT_FAILED
        assert "does not exist" in t.error, t.error
    finally:
        shutil.rmtree(tmp, ignore_errors=True)


def test_an_output_path_that_is_a_file_is_refused_not_crashed():
    tmp = _sandbox()
    blocker = tmp / "out"
    blocker.write_text("not a directory", encoding="utf-8")
    try:
        with patched(EXPORT_DIR=blocker, TARGETS=[_fake_target(tmp)]):
            rc = X.main()
        assert rc == X.EXIT_USAGE, rc
    finally:
        shutil.rmtree(tmp, ignore_errors=True)


def test_a_copy_that_fails_is_reported_not_raised():
    """Where a quarter of a gigabyte gets written. The machine can be out of
    room, and throwing here would discard results the builds already paid
    for."""
    tmp = _sandbox()
    t = _fake_target(tmp)
    real_copy = shutil.copy2

    def no_space(src, dst, *a, **kw):
        raise OSError(28, "No space left on device")
    try:
        shutil.copy2 = no_space
        try:
            rc = _cli(targets=[t], out=tmp / "out")
        finally:
            shutil.copy2 = real_copy
        assert rc == X.EXIT_FAILED, rc
        assert "No space left" in t.error, t.error
        assert t.collected is None
    finally:
        shutil.rmtree(tmp, ignore_errors=True)


def test_a_failed_copy_leaves_no_half_written_file_behind():
    """The next run's 'also in this folder' scan would hash a truncated
    binary and present it as a real one."""
    tmp = _sandbox()
    out = tmp / "out"
    out.mkdir()
    t = _fake_target(tmp)
    real_copy = shutil.copy2

    def half_then_fail(src, dst, *a, **kw):
        Path(dst).write_bytes(b"trunc")
        raise OSError(28, "No space left on device")
    try:
        shutil.copy2 = half_then_fail
        try:
            _cli(targets=[t], out=out)
        finally:
            shutil.copy2 = real_copy
        leftovers = [p.name for p in out.iterdir() if p.name != "manifest.txt"]
        assert leftovers == [], f"partial file left behind: {leftovers}"
    finally:
        shutil.rmtree(tmp, ignore_errors=True)


def test_an_unwritable_manifest_does_not_lose_the_summary():
    tmp = _sandbox()
    t = _fake_target(tmp)
    real_write = X.write_manifest

    def boom(*a, **kw):
        raise OSError(13, "Permission denied")
    try:
        with patched(write_manifest=boom):
            rc = _cli(targets=[t], out=tmp / "out")
        # The build and the collect both succeeded, so the run did too —
        # a missing manifest is not a missing binary.
        assert t.collected is not None, "the copy was lost with the manifest"
        assert rc in (0, X.EXIT_STALE), rc
    finally:
        X.write_manifest = real_write
        shutil.rmtree(tmp, ignore_errors=True)


# ═══════════════════════════════════════════════════════════════════════════
# Staleness — the check, and the false positives it used to produce
# ═══════════════════════════════════════════════════════════════════════════

def test_a_binary_older_than_its_source_is_flagged_but_still_collected():
    tmp = _sandbox()
    t = _fake_target(tmp)
    os.utime(t.artifact, (time.time() - 7200, time.time() - 7200))
    try:
        rc = _cli(targets=[t], out=tmp / "out")
        assert t.stale_by > 0, "an out-of-date binary was not flagged"
        assert any("STALE" in n for n in t.notes), t.notes
        assert rc == X.EXIT_STALE, rc
        # Flagged, but still delivered — a stale binary is usable and the
        # caller may well have meant to ship it.
        assert (tmp / "out" / "Fake V1.0.0.bin").exists(),             "a stale binary was flagged AND withheld"
    finally:
        shutil.rmtree(tmp, ignore_errors=True)


def test_a_binary_newer_than_its_source_is_clean():
    tmp = _sandbox()
    t = _fake_target(tmp)
    os.utime(t.artifact, (time.time() + 60, time.time() + 60))
    try:
        X.check_staleness(t)
        assert t.stale_by == 0, "a current binary was called stale"
    finally:
        shutil.rmtree(tmp, ignore_errors=True)


def test_editing_a_test_does_not_make_a_binary_stale():
    """It cannot reach the binary. This was live: two of the three products'
    verdicts were being driven by a test file."""
    assert not X.is_source("test_announcements.py")
    assert not X.is_source("ack_background_test.dart")
    assert not X.is_source("test_uninstall_elevation.py")
    assert X.is_source("announcements.py"), "real source was swept up too"


def test_runtime_state_files_do_not_make_a_binary_stale():
    """announcements.json and data.json are rewritten by LogicShare AS IT
    RUNS — counting them meant launching the app aged its own installer."""
    assert not X.is_source("announcements.json")
    assert not X.is_source("data.json")


def test_generated_listings_do_not_make_a_binary_stale():
    assert not X.is_source("project_structure.txt")
    assert X.is_source("CMakeLists.txt"), "real build files were swept up too"


def test_the_things_that_SHOULD_count_still_do():
    for name in ("file_panel.py", "LogicBoardInstaller.cpp", "manifest.xml",
                 "main.dart", "build.gradle", "build.gradle.kts",
                 "MainActivity.kt", "proguard-rules.pro",
                 "pubspec.yaml", "logo.ico", "LogicShare.spec",
                 "resources.rc", "logicboard_engine.cp311-win_amd64.pyd"):
        assert X.is_source(name), name


def test_a_file_with_no_extension_is_not_source():
    assert not X.is_source("LICENSE")
    assert not X.is_source("Makefile")


# ═══════════════════════════════════════════════════════════════════════════
# Versions and names
# ═══════════════════════════════════════════════════════════════════════════

def test_versions_come_from_the_files_that_own_them():
    assert X.logicboard_version() != "0.0.0", "LogicBoard version not found"
    assert X.logicshare_version() != "0.0.0", "LogicShare version not found"
    assert X.mobile_version() != "0.0.0", "Android versionName not found"


def test_the_android_build_number_is_not_in_the_filename():
    """versionCode is Android's internal build number and belongs in the
    package, not in a name somebody reads off a screen."""
    assert "+" not in X.mobile_version(), X.mobile_version()


def test_a_missing_version_file_degrades_instead_of_crashing():
    assert X._grep_version(Path("nope/nowhere.py"), r'^X\s*=\s*"([^"]+)"') \
        == "0.0.0"


def test_export_names_are_distinct_and_carry_their_version():
    names = [t.export_name() for t in X.TARGETS]
    assert len(set(names)) == len(names), names
    for n in names:
        assert " V" in n, n


# ═══════════════════════════════════════════════════════════════════════════
# The manifest
# ═══════════════════════════════════════════════════════════════════════════

def test_the_manifest_describes_the_whole_folder_not_just_this_run():
    """--only rewrites the manifest. A manifest silent about a binary that
    is sitting right beside it is worse than none, because nothing tells the
    reader to doubt it."""
    tmp = _sandbox()
    out = tmp / "out"
    out.mkdir()
    (out / "Left over V0.9.0.exe").write_bytes(b"older release")
    t = _fake_target(tmp)
    try:
        with patched(EXPORT_DIR=out, TARGETS=[t]):
            rc = X.main()
        text = (out / "manifest.txt").read_text(encoding="utf-8")
        assert "Left over V0.9.0.exe" in text, text
        assert "Fake V1.0.0.bin" in text, text
    finally:
        shutil.rmtree(tmp, ignore_errors=True)


def test_the_manifest_hash_matches_the_file_on_disk():
    import hashlib
    tmp = _sandbox()
    out = tmp / "out"
    out.mkdir()
    t = _fake_target(tmp, artifact_bytes=b"the actual bytes")
    try:
        with patched(EXPORT_DIR=out, TARGETS=[t]):
            X.main()
        want = hashlib.sha256(b"the actual bytes").hexdigest()
        assert want in (out / "manifest.txt").read_text(encoding="utf-8")
    finally:
        shutil.rmtree(tmp, ignore_errors=True)


def test_collecting_copies_rather_than_moves():
    """A move would leave the next incremental build rebuilding something it
    had no reason to."""
    tmp = _sandbox()
    out = tmp / "out"
    out.mkdir()
    t = _fake_target(tmp)
    try:
        with patched(EXPORT_DIR=out, TARGETS=[t]):
            X.main()
        assert t.artifact.exists(), "the artifact was moved out of the build tree"
    finally:
        shutil.rmtree(tmp, ignore_errors=True)


def test_clean_empties_the_folder_first():
    tmp = _sandbox()
    out = tmp / "out"
    out.mkdir()
    (out / "stale-thing.exe").write_bytes(b"x")
    t = _fake_target(tmp)
    argv = sys.argv
    sys.argv = ["export_binaries.py", "--clean"]
    try:
        with patched(EXPORT_DIR=out, TARGETS=[t]):
            X.main()
        assert not (out / "stale-thing.exe").exists(), "--clean left junk behind"
        assert (out / "Fake V1.0.0.bin").exists()
    finally:
        sys.argv = argv
        shutil.rmtree(tmp, ignore_errors=True)


# ═══════════════════════════════════════════════════════════════════════════
# CLI
# ═══════════════════════════════════════════════════════════════════════════

def _cli(*args, targets=None, out=None):
    argv = sys.argv
    sys.argv = ["export_binaries.py", *args]
    try:
        with patched(EXPORT_DIR=out, TARGETS=targets or X.TARGETS):
            return X.main()
    finally:
        sys.argv = argv


def test_an_unknown_target_is_a_usage_error():
    tmp = _sandbox()
    try:
        assert _cli("--only", "nope", out=tmp) == X.EXIT_USAGE
    finally:
        shutil.rmtree(tmp, ignore_errors=True)


def test_only_selects_a_subset_and_leaves_the_rest_unbuilt():
    tmp = _sandbox()
    a = _fake_target(tmp / "a", key="alpha")
    b = _fake_target(tmp / "b", key="beta")
    a.export_name = lambda: "Alpha V1.bin"
    b.export_name = lambda: "Beta V1.bin"
    hit = []
    a.build = lambda: (hit.append("a"), 0)[1]
    b.build = lambda: (hit.append("b"), 0)[1]
    try:
        _cli("--only", "alpha", targets=[a, b], out=tmp / "out")
        assert hit == ["a"], f"--only built {hit}"
    finally:
        shutil.rmtree(tmp, ignore_errors=True)


def test_collect_only_never_calls_a_build():
    tmp = _sandbox()
    t = _fake_target(tmp)
    called = []
    t.build = lambda: (called.append(1), 0)[1]
    try:
        _cli("--collect-only", targets=[t], out=tmp / "out")
        assert not called, "--collect-only ran a build"
        assert (tmp / "out" / "Fake V1.0.0.bin").exists()
    finally:
        shutil.rmtree(tmp, ignore_errors=True)


def test_a_clean_run_of_everything_exits_zero():
    tmp = _sandbox()
    t = _fake_target(tmp)
    # Artifact newer than source, build succeeds: the happy path.
    os.utime(t.artifact, (time.time() + 60, time.time() + 60))
    try:
        rc = _cli(targets=[t], out=tmp / "out")
        assert rc == 0, f"a perfectly good run exited {rc}"
    finally:
        shutil.rmtree(tmp, ignore_errors=True)


def test_stale_and_failed_are_different_exit_codes():
    tmp = _sandbox()
    t = _fake_target(tmp)
    os.utime(t.artifact, (time.time() - 7200, time.time() - 7200))
    try:
        rc = _cli(targets=[t], out=tmp / "out")
        assert rc == X.EXIT_STALE, rc
        assert rc != X.EXIT_FAILED
    finally:
        shutil.rmtree(tmp, ignore_errors=True)


# ═══════════════════════════════════════════════════════════════════════════
# Defects found auditing the script, each with the shape that produced it
# ═══════════════════════════════════════════════════════════════════════════

def test_a_second_run_does_not_inherit_the_first_ones_verdict():
    """TARGETS is module level, so its Target objects outlive main(). A
    failure left ``error`` set, and main SKIPS collecting a target that has
    one — so a build that failed once reported "does not exist" forever
    after, with the artifact sitting right there. Anyone scripting
    run/fix/run got a stale answer."""
    tmp = _sandbox()
    t = _fake_target(tmp, exists=False)
    try:
        with patched(EXPORT_DIR=tmp / "out", TARGETS=[t]):
            assert X.main() == X.EXIT_FAILED
            assert t.error, "the first run should have failed"
            # Now produce the artifact and run again, same process.
            t.artifact.write_bytes(b"binary")
            os.utime(t.artifact, (time.time() + 60,) * 2)
            rc = X.main()
        assert rc == 0, f"second run exited {rc}: {t.error!r}"
        assert t.collected is not None, "the second run refused to collect"
        assert not t.error
    finally:
        shutil.rmtree(tmp, ignore_errors=True)


def test_a_source_root_that_matches_nothing_is_reported_not_ignored():
    """The silent failure: newest_source_mtime returns 0 for a directory
    that does not exist or holds nothing recognised, and 0 is older than
    every artifact — so the staleness check passes vacuously and the
    summary still claims it ran. A check that cannot fail is worse than no
    check."""
    tmp = _sandbox()
    art = tmp / "old.bin"
    art.write_bytes(b"x")
    os.utime(art, (1, 1))                      # ancient
    t = X.Target(key="k", label="k", source_root=tmp / "typo", artifact=art,
                 export_name=lambda: "k.bin", build=lambda: 0)
    try:
        X.check_staleness(t)
        assert any(n.startswith("CANNOT CHECK") for n in t.notes), t.notes
        # And it must reach the exit code, not just the manifest.
        with patched(EXPORT_DIR=tmp / "out", TARGETS=[t]):
            rc = X.main()
        assert rc == X.EXIT_STALE, (
            "an unverifiable staleness check exited %s; 0 would claim the "
            "binary is current on no evidence" % rc)
    finally:
        shutil.rmtree(tmp, ignore_errors=True)


def test_the_same_note_is_not_repeated():
    tmp = _sandbox()
    src = tmp / "s"
    src.mkdir()
    (src / "a.py").write_text("x")
    art = tmp / "old.bin"
    art.write_bytes(b"x")
    os.utime(art, (1, 1))
    t = X.Target(key="k", label="k", source_root=src, artifact=art,
                 export_name=lambda: "k.bin", build=lambda: 0)
    try:
        for _ in range(3):
            X.check_staleness(t)
        assert len(t.notes) == 1, t.notes
    finally:
        shutil.rmtree(tmp, ignore_errors=True)


def test_gradle_properties_counts_as_source_and_local_properties_does_not():
    """android/gradle.properties carries android.overridePathCheck=true,
    without which the Android build refuses to start — editing it changes
    the build. local.properties holds this machine's SDK path: different
    everywhere, part of no binary. A suffix rule cannot tell them apart."""
    assert X.is_source("gradle.properties")
    assert not X.is_source("local.properties")


def test_the_junction_is_ascii_even_when_TEMP_is_not():
    """The junction exists to hand a toolchain an ASCII path. Built under
    tempfile.gettempdir() it inherits whatever is in TEMP — and TEMP lives
    in the user's profile, so an accented account name puts a non-ASCII
    character straight back into the "ASCII" path. It looks like it worked
    and the build fails exactly as it would have without it."""
    import tempfile as _tf
    real = _tf.gettempdir
    holder = Path(_tf.mkdtemp(prefix="lcx_nonascii_"))
    accented = holder / "Jos\u00e9"
    accented.mkdir(parents=True, exist_ok=True)
    emoji = holder / "Code\U0001F4DC" / "proj"
    emoji.mkdir(parents=True, exist_ok=True)
    (emoji / "pubspec.yaml").write_text("name: probe", encoding="utf-8")
    try:
        _tf.gettempdir = lambda: str(accented)
        assert not str(_tf.gettempdir()).isascii(), "the fake TEMP is ascii"
        base = X._ascii_temp_base()
        assert str(base).isascii(), f"fell back to a non-ASCII base: {base}"
        with X.ascii_path_to(emoji) as p:
            assert str(p).isascii(), f"junction is not ASCII: {p}"
            assert (p / "pubspec.yaml").read_text() == "name: probe", \
                "the junction does not reach the real directory"
            link = p
        assert not link.exists(), "the junction was left behind"
    finally:
        _tf.gettempdir = real
        shutil.rmtree(holder, ignore_errors=True)


def test_a_version_quoted_in_a_docstring_does_not_win():
    """``^APP_VERSION = "..."`` with MULTILINE matches a line at the start
    of a line — including one inside a triple-quoted string, which is
    exactly where an old version gets written down. Measured: a file whose
    docstring said 9.9.9 and whose declaration said 1.2.3 reported 9.9.9,
    which would have named the exported installer after a version that
    does not exist."""
    tmp = _sandbox()
    f = tmp / "fake_version.py"
    f.write_text('"""\nAPP_VERSION = "9.9.9" is what it used to say.\n"""\n'
                 'APP_VERSION = "1.2.3"\n', encoding="utf-8")
    try:
        got = X._grep_version(f, r'^APP_VERSION\s*=\s*"([^"]+)"')
        assert got == "1.2.3", f"a docstring won: {got}"
    finally:
        shutil.rmtree(tmp, ignore_errors=True)


def test_the_real_products_still_report_their_real_versions():
    """The AST path must not have broken the ordinary case."""
    import re
    for reader in (X.logicboard_version, X.logicshare_version,
                   X.mobile_version):
        v = reader()
        assert re.fullmatch(r"\d+\.\d+\.\d+", v), (reader.__name__, v)
        assert v != "0.0.0", f"{reader.__name__} could not read its file"


def test_progress_reaches_a_pipe_before_the_run_ends():
    """A fifteen-minute build that prints nothing until it finishes is
    indistinguishable from one that has hung — which is exactly how it
    read. Python buffers its own stdout when that is not a terminal, so
    piping the run to a file or a log queued every banner until exit while
    the child builds wrote straight through: no output for a quarter of an
    hour, then everything at once, with "BUILD 1/3" arriving after the
    build it announced."""
    import subprocess
    import time as _t
    proc = subprocess.Popen(
        [sys.executable, str(X.ROOT / "export_binaries.py"), "--collect-only"],
        stdout=subprocess.PIPE, stderr=subprocess.STDOUT,
        text=True, bufsize=1, cwd=str(X.ROOT))
    started = _t.perf_counter()
    first_at = None
    try:
        for line in proc.stdout:
            if line.strip():
                first_at = _t.perf_counter() - started
                break
    finally:
        proc.stdout.close()
        proc.wait(timeout=120)
    total = _t.perf_counter() - started
    assert first_at is not None, "nothing was printed at all"
    # The first line must not arrive only as the process dies. Generous, so
    # a slow machine does not fail it: what is being caught is output held
    # back until exit, not a few hundred milliseconds either way.
    assert first_at < max(1.0, total * 0.9), (
        "first output arrived %.2fs into a %.2fs run — it is being buffered "
        "until the end" % (first_at, total))


# ═══════════════════════════════════════════════════════════════════════════
# Formatting helpers — small, but they run inside the summary
# ═══════════════════════════════════════════════════════════════════════════

def test_the_size_and_age_helpers_never_blow_up():
    for n in (0, 1, 1023, 1024, 1024 * 1024 - 1, 1024 * 1024, 10 ** 9):
        assert X.human_size(n)
    for s in (0, 1, 89, 90, 5399, 5400, 86400, 86400 * 3, 10 ** 7):
        assert X.human_age(s)


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
