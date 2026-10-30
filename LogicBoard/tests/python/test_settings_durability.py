"""
test_settings_durability.py — settings.json is shared, so nobody may rewrite it.

Runs under pytest, and also standalone
(``python tests/python/test_settings_durability.py``).

What is worth protecting here
-----------------------------
``settings.json`` is not one component's file. LogicShare keeps its ENTIRE
configuration in it under one key — classroom name, password hash, port,
auto-start — and the app keeps ``last_board_path`` under another. More will
follow, because it is the obvious place to put things.

That makes every writer a custodian of everyone else's data, and one of them
was not behaving like one. ``_persist_last_board_path`` read the file, added
one string, and wrote the whole thing back with ``Path.write_text``.

THE BUG
=======
``Path.write_text`` opens for writing, and opening for writing TRUNCATES.
The old contents are gone before a single new byte is written, so anything
that fails in between — disk full, power cut, the process killed as Windows
shuts down — leaves the truncated file behind.

Reproduced by failing the write mid-call: 4 KB of configuration became 0
bytes. And ``read_settings`` reports a corrupt file as an EMPTY one, on
purpose, so there is no error on the next launch either — LogicShare simply
comes up as though it had never been set up.

To persist a convenience field nobody would miss.

THE RULE
========
One writer, ``write_settings_merged``. It merges a single key and swaps the
file in with an atomic rename, so a reader sees the whole old file or the
whole new one and never a mixture.
"""
import json
import os
import sys
import tempfile
from pathlib import Path
from unittest import mock

os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")
sys.path.insert(0, os.path.abspath(os.path.join(os.path.dirname(__file__),
                                                "..", "..")))

from PySide6.QtWidgets import QApplication

_app = QApplication.instance() or QApplication([])

from ui.logicshare.config import (read_settings, write_settings_merged,
                                  delete_settings_key)

CLASSROOM = {
    "classroom_name": "Room 013 — Physics",
    "password_salt": "a1b2c3d4",
    "password_hash": "deadbeef",
    "port": 8770,
    "auto_start": True,
}


def _settings_with_a_classroom():
    path = Path(tempfile.mkdtemp(prefix="lb_settings_")) / "settings.json"
    assert write_settings_merged(path, "logicshare", CLASSROOM) is True
    return path


class _DiesOnWrite:
    """A file object that opens fine and then fails, the way a full disk
    does. Failing at OPEN would not reproduce anything — the damage is done
    between the truncate and the write."""

    def __init__(self, real):
        self._real = real

    def __enter__(self):
        return self

    def __exit__(self, *exc):
        self._real.close()
        return False

    def write(self, _data):
        raise OSError(28, "No space left on device")

    def flush(self):
        pass

    def fileno(self):
        return self._real.fileno()


def _failing_writes():
    real_fdopen = os.fdopen

    def dying(fd, *a, **k):
        return _DiesOnWrite(real_fdopen(fd, *a, **k))

    return mock.patch.object(os, "fdopen", dying)


# ── the regression ──────────────────────────────────────────────────────────

def test_a_failed_write_leaves_the_previous_settings_intact():
    """THE regression, from the consequence end: whatever happens to the
    write, the classroom must still be configured afterwards."""
    path = _settings_with_a_classroom()

    with _failing_writes():
        ok = write_settings_merged(path, "last_board_path", r"C:\Lesson 4.lb")

    assert ok is False, "a failed write reported success"
    got = read_settings(path).get("logicshare")
    assert got == CLASSROOM, (
        "the classroom configuration did not survive a failed write of an "
        "unrelated key; got %r" % (got,))


def test_a_failed_write_does_not_truncate_the_file():
    path = _settings_with_a_classroom()
    before = path.stat().st_size
    assert before > 0
    with _failing_writes():
        write_settings_merged(path, "last_board_path", "x")
    assert path.stat().st_size == before, \
        "settings.json was truncated to %d bytes" % path.stat().st_size


def test_a_failed_write_leaves_no_temp_file_behind():
    """Otherwise every full disk and every mid-save shutdown drops another
    orphan next to the real file."""
    path = _settings_with_a_classroom()
    with _failing_writes():
        write_settings_merged(path, "last_board_path", "x")
    strays = [f.name for f in path.parent.iterdir() if f.name.endswith(".tmp")]
    assert not strays, "temp files left behind: %s" % strays


# ── merging, not replacing ──────────────────────────────────────────────────

def test_writing_one_key_preserves_every_other():
    path = _settings_with_a_classroom()
    write_settings_merged(path, "last_board_path", r"C:\Lesson 4.lb")
    write_settings_merged(path, "calibration", {"palm_area": 4000.0})
    data = read_settings(path)
    assert data["logicshare"] == CLASSROOM
    assert data["last_board_path"] == r"C:\Lesson 4.lb"
    assert data["calibration"] == {"palm_area": 4000.0}


def test_a_non_dict_value_is_allowed():
    """``last_board_path`` is a string. A signature that only admitted
    dicts is precisely what sent the second caller off to write the file by
    hand."""
    path = _settings_with_a_classroom()
    for key, value in (("last_board_path", r"C:\a.lb"), ("count", 7),
                       ("enabled", True), ("ratios", [1, 2, 3])):
        assert write_settings_merged(path, key, value) is True
        assert read_settings(path)[key] == value


def test_deleting_a_key_preserves_every_other():
    path = _settings_with_a_classroom()
    write_settings_merged(path, "last_board_path", r"C:\Lesson 4.lb")
    assert delete_settings_key(path, "last_board_path") is True
    data = read_settings(path)
    assert "last_board_path" not in data
    assert data["logicshare"] == CLASSROOM


def test_deleting_a_key_that_is_not_there_is_not_an_error():
    path = _settings_with_a_classroom()
    assert delete_settings_key(path, "never_existed") is True
    assert read_settings(path)["logicshare"] == CLASSROOM


# ── two writers, one file ───────────────────────────────────────────────────

def test_two_writers_do_not_share_a_scratch_file():
    """The temp name used to be a fixed ``settings.json.tmp``. Two managers
    saving at the same moment would then be writing to the SAME scratch
    file, and whichever renamed second would publish a half-written mixture
    of both."""
    path = _settings_with_a_classroom()
    seen = []
    real_mkstemp = tempfile.mkstemp

    def spy(*a, **k):
        fd, name = real_mkstemp(*a, **k)
        seen.append(name)
        return fd, name

    with mock.patch.object(tempfile, "mkstemp", spy):
        write_settings_merged(path, "a", 1)
        write_settings_merged(path, "b", 2)
        delete_settings_key(path, "a")

    assert len(seen) == 3, "a writer bypassed mkstemp: %s" % seen
    assert len(set(seen)) == 3, "two writers used the same temp file: %s" % seen


def test_the_settings_file_is_never_written_any_other_way():
    """The rule, enforced. Anything that opens settings.json for writing
    outside config.py is a second custodian of everyone else's data."""
    import io as _io
    import re
    root = Path(__file__).resolve().parents[2] / "ui"
    offenders = []
    for path in sorted(root.rglob("*.py")):
        if path.name == "config.py" and path.parent.name == "logicshare":
            continue                      # the writer itself
        src = _io.open(path, encoding="utf-8").read()
        if "settings.json" not in src and "_settings_file" not in src:
            continue
        for i, line in enumerate(src.split("\n"), 1):
            if line.strip().startswith("#"):
                continue
            if re.search(r"_settings_(file|path)\s*\.\s*write_text|"
                         r"_settings_(file|path)\s*\.\s*open\s*\(\s*[\"']w",
                         line):
                offenders.append("%s:%d  %s"
                                 % (path.name, i, line.strip()[:70]))
    assert not offenders, (
        "settings.json written outside config.py — use "
        "write_settings_merged:\n  " + "\n  ".join(offenders))


# ── the caller that had the bug ─────────────────────────────────────────────

def test_the_last_board_path_round_trips_through_the_shared_writer():
    import ui.application_core as ac
    import inspect
    src = inspect.getsource(ac.ApplicationCore._persist_last_board_path)
    body = src.split('"""')[-1]
    assert "write_settings_merged" in body, \
        "_persist_last_board_path is rewriting settings.json by hand again"
    assert "write_text" not in body, \
        "_persist_last_board_path still calls write_text"


def test_clearing_the_last_board_path_keeps_the_classroom():
    """The None branch used to ``data.pop(...)`` and rewrite the file, which
    is the same whole-file rewrite by another name."""
    import ui.application_core as ac
    import inspect
    src = inspect.getsource(ac.ApplicationCore._persist_last_board_path)
    body = src.split('"""')[-1]
    assert "delete_settings_key" in body, \
        "the clear path still rewrites the whole file"


if __name__ == "__main__":
    failures = []
    for name, fn in sorted(globals().items()):
        if not name.startswith("test_") or not callable(fn):
            continue
        try:
            fn()
            print("  ok    " + name)
        except AssertionError as exc:
            failures.append(name)
            print("  FAIL  %s: %s" % (name, exc))
        except Exception as exc:                       # noqa: BLE001
            failures.append(name)
            print("  ERROR %s: %s: %s" % (name, type(exc).__name__, exc))
    print(("\n%d failed" % len(failures)) if failures else "\nall passed")
    sys.exit(1 if failures else 0)
