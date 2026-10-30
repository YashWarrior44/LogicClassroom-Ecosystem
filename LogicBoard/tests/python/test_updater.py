"""
test_updater.py — the update mechanism: pick, download, hand off.

Runs under pytest, and also standalone (``python tests/python/test_updater.py``)
because the repo's pytest install is currently broken on Python 3.11.

What is worth protecting here
-----------------------------
LogicBoard no longer installs its own updates. It downloads
``LogicBoardInstaller.exe`` from the release and runs it, exactly as a fresh
install does — see ``ui/util/updater.py`` for why. That removes most of what
the previous version of this file tested (ZIP extraction, versioned-folder
creation, shortcut re-registration, the elevated ``--apply-update`` helper)
and leaves a much smaller surface that is worth being strict about:

  * We must download an INSTALLER, not whatever .exe happens to be first in
    the asset list — we execute what we download.
  * A truncated download must never be launched. It is a broken exe that
    Windows will happily start, and it arrives on exactly the flaky Wi-Fi a
    classroom has.
  * The unsaved board must be settled BEFORE the hand-off. The installer's
    first act is to terminate every LogicBoard.exe, so a prompt after that
    point never happens.
  * The two sides of the install — the C++ installer and this app — still
    have to agree about where a version lives.

What these deliberately do NOT do is trigger a real UAC prompt or launch a
real installer: ``launch_installer`` is checked for the contract it presents
to its caller, and substituted everywhere the routing is under test. An
automated test that raised a consent dialog would hang the suite on a
machine with nobody watching it.
"""
import os
import shutil
import sys
import tempfile
from pathlib import Path

os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")
sys.path.insert(0, os.path.abspath(os.path.join(os.path.dirname(__file__),
                                                "..", "..")))

from PySide6.QtWidgets import QApplication

_app = QApplication.instance() or QApplication([])

from ui.util import updater, paths
from ui.util.version import SemVer, APP_NAME, APP_VERSION


# ── scaffolding ─────────────────────────────────────────────────────────────

class _FakeUpdatesDir:
    """Point ``paths.updates_dir`` at a temp dir for the duration.

    The download path deletes every ``*.exe`` it finds next to its target.
    Anything less than a hard redirect risks a test wiping a real download.
    """

    def __enter__(self):
        self.dir = Path(tempfile.mkdtemp(prefix="lb_upd_"))
        self._orig = paths.updates_dir
        paths.updates_dir = lambda: self.dir
        updater.paths.updates_dir = lambda: self.dir
        return self.dir

    def __exit__(self, *exc):
        paths.updates_dir = self._orig
        updater.paths.updates_dir = self._orig
        shutil.rmtree(self.dir, ignore_errors=True)
        return False


class _FakeResponse:
    """Stand-in for what ``net_http.open_url`` hands back."""

    def __init__(self, payload: bytes, *, content_length=None):
        self._buf = payload
        self._pos = 0
        self._len = content_length if content_length is not None else len(payload)

    def getheader(self, name, default=None):
        if name.lower() == "content-length":
            return str(self._len) if self._len is not None else default
        return default

    def read(self, n=-1):
        if n is None or n < 0:
            n = len(self._buf) - self._pos
        chunk = self._buf[self._pos:self._pos + n]
        self._pos += len(chunk)
        return chunk

    def __enter__(self): return self
    def __exit__(self, *exc): return False


class _FakeUrlopen:
    """Serve a fixed payload to the download worker instead of the network."""

    def __init__(self, payload: bytes, *, content_length=None):
        self._payload = payload
        self._len = content_length

    def __enter__(self):
        self._orig = updater._open
        updater._open = (
            lambda url, headers=None, timeout=None: _FakeResponse(
                self._payload, content_length=self._len))
        return self

    def __exit__(self, *exc):
        updater._open = self._orig
        return False


def _release(*assets, tag="v3.2.0", body="notes"):
    """A GitHub ``releases/latest`` payload with the given assets."""
    return {
        "tag_name": tag,
        "body": body,
        "html_url": "https://github.com/x/y/releases/tag/" + tag,
        "assets": [
            {"name": n, "size": s,
             "browser_download_url": f"https://example.invalid/{n}"}
            for n, s in assets
        ],
    }


def _info(**over):
    """An UpdateInfo pointing at nothing in particular."""
    base = dict(
        latest_version="v3.2.0",
        latest_semver=SemVer.parse("3.2.0"),
        installer_url="https://example.invalid/LogicBoardInstaller.exe",
        asset_name="LogicBoardInstaller.exe",
        asset_size=0,
        release_notes="",
        release_url="",
    )
    base.update(over)
    return updater.UpdateInfo(**base)


def _run_download(info):
    """Drive DownloadWorker synchronously and collect what it emitted."""
    out = {"path": None, "err": None, "progress": []}
    w = updater.DownloadWorker(info)
    w.progress.connect(lambda m, p: out["progress"].append((m, p)))
    w.finished.connect(lambda path, err: out.update(path=path, err=err))
    w.run()
    return out


# ── choosing what to download ───────────────────────────────────────────────

def test_the_installer_wins_over_a_bare_exe():
    """We RUN what we download, so 'first .exe in the list' is not good
    enough — a release can carry a portable build alongside the installer."""
    a = updater._pick_installer_asset([
        {"name": "LogicBoardPortable.exe", "size": 1,
         "browser_download_url": "u1"},
        {"name": "LogicBoardInstaller.exe", "size": 2,
         "browser_download_url": "u2"},
    ])
    assert a["name"] == "LogicBoardInstaller.exe"


def test_a_zip_is_never_chosen():
    """The old updater downloaded a ZIP. This one can do nothing with an
    archive — it would launch it as a program."""
    assert updater._pick_installer_asset(
        [{"name": "LogicBoard.zip", "size": 1, "browser_download_url": "u"}]
    ) is None


def test_the_uninstaller_is_never_chosen():
    """A release that ships Uninstaller.exe must not have it handed to
    somebody who asked to update."""
    assert updater._pick_installer_asset(
        [{"name": "Uninstaller.exe", "size": 1, "browser_download_url": "u"}]
    ) is None


def test_an_asset_with_no_download_url_is_skipped():
    assert updater._pick_installer_asset(
        [{"name": "LogicBoardInstaller.exe", "size": 1}]) is None


def test_a_release_with_no_installer_is_reported_not_crashed():
    try:
        updater._parse_release(_release(("LogicBoard.zip", 10)))
    except updater._NoUsableAsset:
        return
    raise AssertionError("a ZIP-only release should not parse as installable")


def test_an_unparseable_tag_is_reported_not_crashed():
    try:
        updater._parse_release(_release(("LogicBoardInstaller.exe", 10),
                                        tag="latest"))
    except updater._BadVersionTag:
        return
    raise AssertionError("'latest' is not a version and must be refused")


def test_a_good_release_parses_into_everything_the_ui_needs():
    info = updater._parse_release(
        _release(("LogicBoardInstaller.exe", 1234), tag="v3.2.0",
                 body="# What's new"))
    assert info.latest_version == "v3.2.0"
    assert info.latest_semver == SemVer.parse("3.2.0")
    assert info.installer_url.endswith("LogicBoardInstaller.exe")
    assert info.asset_size == 1234
    assert "What's new" in info.release_notes


# ── which releases count as an update ───────────────────────────────────────

def test_the_running_version_is_not_an_update():
    assert updater.newer_than_current(
        _info(latest_semver=SemVer.parse(APP_VERSION))) is False


def test_an_older_release_is_not_an_update():
    """Failing closed matters more here than being clever: offering a
    downgrade would push an old build over a working install."""
    assert updater.newer_than_current(
        _info(latest_semver=SemVer.parse("0.0.1"))) is False


def test_an_unparseable_version_is_not_an_update():
    assert updater.newer_than_current(_info(latest_semver=None)) is False


def test_a_newer_release_is_an_update():
    major = SemVer.parse(APP_VERSION).major + 1
    assert updater.newer_than_current(
        _info(latest_semver=SemVer.parse(f"{major}.0.0"))) is True


# ── the download ────────────────────────────────────────────────────────────

def test_a_complete_download_lands_a_file_named_for_the_version():
    payload = b"MZ" + b"\0" * 4096
    with _FakeUpdatesDir() as d, _FakeUrlopen(payload):
        out = _run_download(_info())
    assert out["err"] == "", out["err"]
    assert Path(out["path"]).name == "LogicBoardInstaller_v3.2.0.exe"
    assert Path(out["path"]).parent == d


def test_the_saved_name_ignores_a_hostile_asset_name():
    """Naming the file after the ASSET lets a release with a space or a
    Windows-illegal character in it produce a path that won't launch."""
    payload = b"MZ" + b"\0" * 4096
    with _FakeUpdatesDir(), _FakeUrlopen(payload):
        out = _run_download(_info(asset_name='Logic Board <setup>.exe'))
    assert out["err"] == ""
    assert Path(out["path"]).name == "LogicBoardInstaller_v3.2.0.exe"


def test_a_truncated_download_is_rejected_and_deleted():
    """The server said 9 MB and sent 4 KB. Half an installer is a broken
    exe Windows will happily run."""
    payload = b"MZ" + b"\0" * 4096
    with _FakeUpdatesDir() as d, _FakeUrlopen(payload, content_length=9_000_000):
        out = _run_download(_info())
    assert out["path"] == ""
    assert "stopped early" in out["err"]
    assert list(d.glob("*.exe")) == [], "a partial download was left on disk"


def test_a_tiny_response_is_rejected():
    """A captive portal's HTML login page is a 200 with a body. It is not
    an installer, and Content-Length agrees with itself, so size is the
    only thing left to catch it."""
    with _FakeUpdatesDir() as d, _FakeUrlopen(b"<html>nope</html>"):
        out = _run_download(_info())
    assert out["path"] == ""
    assert "too small" in out["err"]
    assert list(d.glob("*.exe")) == []


def test_a_cancelled_download_reports_neither_path_nor_error():
    """Cancelling is a choice, not a failure — the UI must not pop an
    error dialog at somebody who just pressed Cancel."""
    payload = b"MZ" + b"\0" * (updater._DOWNLOAD_CHUNK * 4)
    with _FakeUpdatesDir() as d, _FakeUrlopen(payload):
        w = updater.DownloadWorker(_info())
        out = {}
        w.finished.connect(lambda path, err: out.update(path=path, err=err))
        w.cancel()
        w.run()
    assert out["path"] == "" and out["err"] == ""
    assert list(d.glob("*.exe")) == []


def test_downloading_removes_the_previous_installer():
    """These are ~200 MB each and nothing ever reads an old one."""
    payload = b"MZ" + b"\0" * 4096
    with _FakeUpdatesDir() as d, _FakeUrlopen(payload):
        stale = d / "LogicBoardInstaller_v3.1.0.exe"
        stale.write_bytes(b"old")
        out = _run_download(_info())
    assert out["err"] == ""
    assert not stale.exists(), "an old installer survived the download"


def test_progress_without_a_content_length_reports_zero_percent():
    """No Content-Length means no honest percentage. The dialog turns the
    zero into a busy bar; inventing a number would be worse."""
    payload = b"MZ" + b"\0" * 4096
    with _FakeUpdatesDir(), _FakeUrlopen(payload, content_length=None):
        # _FakeResponse falls back to the real length, so ask for the
        # header-less case explicitly.
        info = _info()
        w = updater.DownloadWorker(info)
        seen = []
        w.progress.connect(lambda m, p: seen.append((m, p)))
        w._report(1024, 0)
    assert seen and seen[-1][1] == 0
    assert "MB" in seen[-1][0]


# ── the hand-off ────────────────────────────────────────────────────────────

def test_launching_is_not_attempted_off_windows():
    """``runas`` is a Windows shell verb. Elsewhere this must decline
    politely rather than raise."""
    real = sys.platform
    try:
        sys.platform = "linux"
        ok, err = updater.launch_installer(Path("x.exe"))
        assert ok is False and "Windows" in err
    finally:
        sys.platform = real


def test_a_declined_uac_prompt_is_explained_not_reported_as_an_error_code():
    """SE_ERR_ACCESSDENIED is the user answering No. Saying 'error 5' to
    somebody who just clicked No sends them hunting for a fault."""
    if sys.platform != "win32":
        return
    import ctypes
    orig = ctypes.windll.shell32.ShellExecuteW
    try:
        ctypes.windll.shell32.ShellExecuteW = lambda *a: 5
        ok, err = updater.launch_installer(Path("x.exe"))
        assert ok is False
        assert "declined" in err and "administrator" in err
    finally:
        ctypes.windll.shell32.ShellExecuteW = orig


def test_a_successful_launch_reports_no_message():
    if sys.platform != "win32":
        return
    import ctypes
    orig = ctypes.windll.shell32.ShellExecuteW
    try:
        ctypes.windll.shell32.ShellExecuteW = lambda *a: 42
        ok, err = updater.launch_installer(Path("x.exe"))
        assert ok is True and err == ""
    finally:
        ctypes.windll.shell32.ShellExecuteW = orig


# ── the unsaved board ───────────────────────────────────────────────────────
#
# The installer terminates every running LogicBoard.exe as its first step —
# it has to, Windows won't let it overwrite a mapped image. So a prompt about
# unsaved work has exactly one moment to happen, and these pin it there.

class _FakeWindow:
    """Just enough MainWindow for the guard to find and question."""

    def __init__(self, answer):
        self._answer = answer
        self.asked = False
        self._canvas = object()          # part of the duck-type

    def _confirm_discard(self):
        self.asked = True
        if isinstance(self._answer, Exception):
            raise self._answer
        return self._answer

    def window(self):
        return self


def _flow_with(window, *, download_ok=True, install_now=True):
    """Run ``run_confirmed_update_flow`` against fakes and report what
    happened. Nothing here touches the network, disk or the shell.

    ``install_now`` is the answer to the "Update downloaded — install now?"
    question that follows every fresh download."""
    from PySide6.QtWidgets import QDialog
    from ui.panels import update_dialog as ud

    calls = {"downloaded": False, "launched": False, "asked_install": False}

    class _FakeReady:
        def __init__(self, info, parent=None):
            calls["asked_install"] = True
        def exec(self):
            return QDialog.Accepted if install_now else QDialog.Rejected

    class _FakeDialog:
        def __init__(self, info, parent=None):
            calls["downloaded"] = True
            self.installer_path = "C:/fake/LogicBoardInstaller.exe" if download_ok else ""
            self.error_msg = ""
        def exec(self): return 1

    orig_dlg, orig_find, orig_launch, orig_msg, orig_ready, orig_existing = (
        ud.UpdateDownloadDialog, ud._find_main_window,
        ud.updater.launch_installer, ud._info_msg, ud.UpdateReadyDialog,
        ud.updater.existing_download)
    orig_quit = None
    try:
        ud.UpdateDownloadDialog = _FakeDialog
        ud.UpdateReadyDialog = _FakeReady
        ud.updater.existing_download = lambda info: None
        ud._find_main_window = lambda parent: window
        ud._info_msg = lambda *a, **k: None

        def _fake_launch(path):
            calls["launched"] = True
            return True, ""
        ud.updater.launch_installer = _fake_launch

        from PySide6.QtWidgets import QApplication as _QA
        orig_quit = _QA.quit
        _QA.quit = staticmethod(lambda *a, **k: None)

        major = SemVer.parse(APP_VERSION).major + 1
        ud.run_confirmed_update_flow(None, _info(
            latest_semver=SemVer.parse(f"{major}.0.0")))
    finally:
        ud.UpdateDownloadDialog = orig_dlg
        ud.UpdateReadyDialog = orig_ready
        ud.updater.existing_download = orig_existing
        ud._find_main_window = orig_find
        ud.updater.launch_installer = orig_launch
        ud._info_msg = orig_msg
        if orig_quit is not None:
            from PySide6.QtWidgets import QApplication as _QA
            _QA.quit = orig_quit
    return calls


def test_the_board_is_settled_before_the_installer_starts():
    """The save question comes right before the hand-off — after the
    download and the "install now?" answer, so a board edited while the
    installer downloaded is still the one asked about."""
    win = _FakeWindow(True)
    calls = _flow_with(win)
    assert win.asked, "the update never asked about the unsaved board"
    assert calls["downloaded"] and calls["asked_install"] and calls["launched"]


def test_cancelling_the_save_prompt_abandons_the_install():
    """Cancel there can only mean 'not now' — carrying on would hand the
    machine to an installer that kills us with the board unsaved. The
    download is kept; it was agreed to, and Later reuses it."""
    win = _FakeWindow(False)
    calls = _flow_with(win)
    assert win.asked
    assert not calls["launched"], "the installer started after a Cancel"


def test_later_at_install_now_never_reaches_the_board_or_the_installer():
    win = _FakeWindow(True)
    calls = _flow_with(win, install_now=False)
    assert calls["downloaded"] and calls["asked_install"]
    assert not win.asked, "asked about the board for an install that is not happening"
    assert not calls["launched"]


def test_a_broken_save_prompt_abandons_the_update_too():
    """Fail closed: a missed update costs a retry, a lost board costs the
    lesson."""
    win = _FakeWindow(RuntimeError("boom"))
    calls = _flow_with(win)
    assert not calls["launched"]


def test_background_mode_has_no_board_to_ask_about():
    """With no window there is nothing unsaved, and the update must not
    stall waiting for an answer nobody can give."""
    calls = _flow_with(None)
    assert calls["downloaded"] and calls["launched"]


def test_the_guard_finds_the_window_by_shape_not_by_import():
    """``update_dialog`` is reached FROM main_window, so it cannot import
    it back. The duck-type is load-bearing — if MainWindow ever renames
    ``_confirm_discard``, this is what notices."""
    from ui.panels import update_dialog as ud
    win = _FakeWindow(True)
    assert ud._find_main_window(win) is win
    import ui.main_window as mw
    assert hasattr(mw.MainWindow, "_confirm_discard"), \
        "MainWindow no longer has _confirm_discard — the guard is now a no-op"


# ── the two sides of the install still have to agree ────────────────────────

_INSTALLER_CPP = (Path(__file__).resolve().parents[2]
                  / "LogicBoardInstaller" / "LogicBoardInstaller.cpp")


def _cpp_constant(name: str) -> str:
    import re
    src = _INSTALLER_CPP.read_text(encoding="utf-8", errors="replace")
    m = re.search(rf'{name}\[\]\s*=\s*(?:L"([^"]+)"|APP_VERSION_TEXT)', src)
    assert m, f"{name} not found in the installer source"
    if m.group(1):
        return m.group(1)
    m2 = re.search(r'#define APP_VERSION_TEXT L"([^"]+)"', src)
    assert m2, "APP_VERSION_TEXT not found in the installer source"
    return m2.group(1)


def test_the_installer_still_closes_a_running_logicboard_first():
    """This is the step that makes the whole update scheme work — and the
    reason the app asks about unsaved work before handing over."""
    if not _INSTALLER_CPP.exists():
        return
    src = _INSTALLER_CPP.read_text(encoding="utf-8", errors="replace")
    assert "TerminateLogicBoard();" in src, \
        "the installer no longer closes a running LogicBoard — installing " \
        "over a live copy will fail on locked files"


# The install LAYOUT and everything the installer registers now live in
# test_deployment.py — they stopped being an updater concern the moment the
# update path became "run the installer".


def test_downloads_do_not_live_in_the_startup_swept_temp_dir():
    """``sweep_temp_dirs`` empties temp_dir() at every startup — including
    the startup the installer itself triggers when it relaunches us."""
    assert paths.updates_dir() != paths.temp_dir()



# ── one repo for the whole ecosystem ────────────────────────────────────────

def _listed(tag, *assets, pre=False, draft=False):
    r = _release(*assets, tag=tag)
    r["prerelease"] = pre
    r["draft"] = draft
    return r


def test_the_updater_points_at_the_shared_repo_and_lists_releases():
    assert updater.GITHUB_REPOSITORY == "YashWarrior44/LogicClassroom-Ecosystem"
    assert "/releases/latest" not in updater._GITHUB_API_TMPL, \
        "/latest is whichever app shipped last in a shared repo"


def test_logicshares_release_is_never_offered_to_logicboard():
    data = [_listed("LogicShare-v9.9.9", ("LogicShareInstaller.exe", 10)),
            _listed("LogicBoard-v3.2.0", ("LogicBoardInstaller.exe", 10))]
    info = updater._parse_release(updater._select_release(data))
    assert str(info.latest_semver) == "3.2.0"
    assert info.latest_version == "v3.2.0", info.latest_version


def test_the_highest_logicboard_version_wins_whatever_the_order():
    data = [_listed("LogicBoard-v3.1.9", ("LogicBoardInstaller.exe", 10)),
            _listed("LogicBoard-v3.2.0", ("LogicBoardInstaller.exe", 10)),
            _listed("LogicBoard-v3.1.4", ("LogicBoardInstaller.exe", 10))]
    assert updater._select_release(data)["tag_name"] == "LogicBoard-v3.2.0"


def test_prereleases_drafts_and_installerless_releases_are_passed_over():
    data = [_listed("LogicBoard-v4.0.0", ("LogicBoardInstaller.exe", 10), pre=True),
            _listed("LogicBoard-v3.9.0", ("LogicBoardInstaller.exe", 10), draft=True),
            _listed("LogicBoard-v3.5.0", ("notes.zip", 10)),
            _listed("LogicBoard-v3.2.0", ("LogicBoardInstaller.exe", 10))]
    assert updater._select_release(data)["tag_name"] == "LogicBoard-v3.2.0"


def test_a_repo_with_no_logicboard_release_says_so():
    data = [_listed("LogicShare-v1.1.2", ("LogicShareInstaller.exe", 10))]
    try:
        updater._select_release(data)
        raise AssertionError("a LogicShare-only repo produced a LogicBoard release")
    except updater._NoRelease:
        pass
    out = []
    w = updater.CheckWorker()
    w.finished.connect(lambda i, m: out.append(m))
    saved = updater._http_json
    updater._http_json = lambda url: data
    try:
        w.run()
    finally:
        updater._http_json = saved
    assert out == ["No LogicBoard release has been published yet."], out

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
