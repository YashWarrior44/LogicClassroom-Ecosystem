"""
test_update_cleanup.py — spent update installers do not pile up.

    python test_update_cleanup.py

Each update leaves a ~70 MB installer in %LOCALAPPDATA%\\LogicShare\\Updates.
The only cleanup used to run when the NEXT update was downloaded, so the
installer that produced the running version sat there until a later release
replaced it. Found on the development machine: LogicShareInstaller_v1.1.1.exe,
70 MB, next to an installed 1.1.1.
"""
from __future__ import annotations

import os
import shutil
import sys
import tempfile
from pathlib import Path

HERE = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, HERE)

import LogicShare as L                                    # noqa: E402


class _Updates:
    """A throwaway Updates folder swapped in for the real one."""

    def __init__(self, *names):
        self.names = names

    def __enter__(self):
        self.dir = Path(tempfile.mkdtemp(prefix="ls_upd_"))
        for n in self.names:
            (self.dir / n).write_bytes(b"MZ" + b"\0" * 4096)
        self._real = L.UPDATE_DIR
        L.UPDATE_DIR = self.dir
        return self

    def __exit__(self, *_a):
        L.UPDATE_DIR = self._real
        shutil.rmtree(self.dir, ignore_errors=True)

    def left(self):
        return sorted(p.name for p in self.dir.iterdir())


def test_the_installer_for_the_running_version_is_deleted():
    """THE case found on disk: updated to 1.1.1, installer for 1.1.1 kept."""
    with _Updates("LogicShareInstaller_v1.1.1.exe") as u:
        L.prune_installed_updates("1.1.1")
        assert u.left() == [], f"the spent installer survived: {u.left()}"


def test_installers_for_older_versions_are_deleted():
    with _Updates("LogicShareInstaller_v1.0.0.exe",
                  "LogicShareInstaller_v1.1.0.exe") as u:
        L.prune_installed_updates("1.1.1")
        assert u.left() == []


def test_a_waiting_newer_update_is_kept():
    """Downloaded, then "Later" at "install now?". Deleting that file at
    launch would turn the next "install now" back into a 70 MB re-download."""
    with _Updates("LogicShareInstaller_v1.1.1.exe",
                  "LogicShareInstaller_v1.2.0.exe") as u:
        L.prune_installed_updates("1.1.1")
        assert u.left() == ["LogicShareInstaller_v1.2.0.exe"], u.left()


def test_versions_compare_as_numbers_not_text():
    """1.10.0 is newer than 1.9.0. As strings it sorts before it — and a
    string comparison would delete the newer installer as the older one."""
    with _Updates("LogicShareInstaller_v1.10.0.exe",
                  "LogicShareInstaller_v1.9.0.exe") as u:
        L.prune_installed_updates("1.9.0")
        assert u.left() == ["LogicShareInstaller_v1.10.0.exe"], u.left()


def test_an_unversioned_exe_is_removed():
    """``update_installer_path`` names every download by version, so an exe
    without one can never be matched for reuse — it is dead weight."""
    with _Updates("LogicShareInstaller.exe", "setup (1).exe") as u:
        L.prune_installed_updates("1.1.1")
        assert u.left() == []


def test_only_installers_are_touched():
    with _Updates("LogicShareInstaller_v1.0.0.exe", "notes.txt") as u:
        L.prune_installed_updates("1.1.1")
        assert u.left() == ["notes.txt"]


def test_a_locked_installer_is_skipped_not_fatal():
    """The installer that just relaunched us can still be exiting and hold
    its own exe open. That must not raise, and must not stop the rest of the
    sweep."""
    with _Updates("LogicShareInstaller_v1.0.0.exe",
                  "LogicShareInstaller_v1.1.0.exe") as u:
        held = open(u.dir / "LogicShareInstaller_v1.0.0.exe", "rb")
        try:
            # Windows refuses to delete a file with an open handle that did
            # not ask for FILE_SHARE_DELETE — which Python's open() does not.
            removed = L.prune_installed_updates("1.1.1")
        finally:
            held.close()
        assert "LogicShareInstaller_v1.1.0.exe" in removed
        if sys.platform.startswith("win"):
            assert "LogicShareInstaller_v1.0.0.exe" in u.left(), \
                "expected the locked file to survive this pass"
        L.prune_installed_updates("1.1.1")      # the retry
        assert u.left() == [], f"the retry did not finish the job: {u.left()}"


def test_a_missing_folder_is_fine():
    real = L.UPDATE_DIR
    L.UPDATE_DIR = Path(tempfile.gettempdir()) / "ls_upd_does_not_exist_x9"
    try:
        assert L.prune_installed_updates("1.1.1") == []
    finally:
        L.UPDATE_DIR = real


def test_the_sweep_runs_at_launch_and_again_later():
    """At launch is not enough on its own — see the locked-file case."""
    src = open(os.path.join(HERE, "LogicShare.py"), encoding="utf-8").read()
    main = src[src.index('if __name__ == "__main__":'):]
    assert "prune_installed_updates()" in main, "never swept at launch"
    assert "singleShot(120_000, prune_installed_updates)" in main, \
        "no retry for an installer that was still exiting"
    # And only by the instance that actually runs — a second launch exits
    # before it gets here.
    assert main.index("single_instance.SingleInstance()") < \
        main.index("prune_installed_updates()")


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
