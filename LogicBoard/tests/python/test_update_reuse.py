"""
test_update_reuse.py — an installer is downloaded once, named by version,
and never fetched twice.

Runs under pytest, and also standalone
(``python tests/python/test_update_reuse.py``).

THE RULES, which are the same in all three apps
===============================================
1. The downloaded file is named after the RELEASE VERSION, not after the
   asset. A release whose asset name carries a space, or a character
   Windows dislikes, cannot then produce a path that will not launch — and
   the file on disk says which build it is, so two releases can never be
   confused for one another.

2. If the installer for this release is already on disk and intact, it is
   REUSED. Re-fetching 200 MB the machine is already holding is not a small
   waste on a school connection.

3. "Intact" means the size GitHub published, matched exactly. That catches
   the failure that actually happens — a download cut short by flaky Wi-Fi
   leaves a file that exists, looks plausible, and is a broken exe Windows
   will happily try to run.

4. A file that fails that check is DELETED, not merely ignored. Left alone,
   it would fool the next attempt, which only checks whether the file
   exists.

5. Old installers are pruned, but never the one about to be used. LogicShare
   Windows got this backwards: it deleted every .exe in the folder BEFORE
   naming the destination, so the file it was about to reuse was destroyed
   every single time.

6. CONSENT, TWICE (2026-10). Nothing is downloaded until the user agrees,
   and a finished download is not installed until the user agrees again —
   "install now?" with Later, which keeps the file. All three apps used to
   download quietly before asking anything.

7. A spent installer (version <= the one running) is deleted at startup,
   not left until the next update is downloaded.
"""
import os
import sys
import tempfile
from pathlib import Path

os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")
sys.path.insert(0, os.path.abspath(os.path.join(os.path.dirname(__file__),
                                                "..", "..")))

from PySide6.QtWidgets import QApplication

_app = QApplication.instance() or QApplication([])

import ui.util.paths as paths
import ui.util.updater as up

SIZE = 5_000_000


def _fresh(version="3.2.0", size=SIZE):
    """An UpdateInfo pointing at an updates dir nothing else is using."""
    tmp = Path(tempfile.mkdtemp(prefix="lb_upd_"))
    paths.updates_dir = lambda: tmp
    up.paths.updates_dir = lambda: tmp
    info = up.UpdateInfo(
        latest_version="V" + version,
        latest_semver=up.SemVer.parse(version),
        installer_url="https://example.invalid/whatever.exe",
        asset_name="Some Installer (x64).exe",
        asset_size=size,
        release_notes="",
        release_url="",
    )
    return info, tmp


# ── naming ──────────────────────────────────────────────────────────────────

def test_the_file_is_named_after_the_version_not_the_asset():
    info, _tmp = _fresh("3.2.0")
    dest = up._installer_dest(info)
    assert dest.name == "LogicBoardInstaller_v3.2.0.exe", dest.name
    assert "Some Installer" not in dest.name, \
        "the asset's name reached the path — spaces and brackets and all"


def test_two_releases_never_share_a_filename():
    a, _ = _fresh("3.2.0")
    b, _ = _fresh("3.3.0")
    assert up._installer_dest(a).name != up._installer_dest(b).name


# ── reuse ───────────────────────────────────────────────────────────────────

def test_nothing_is_reused_when_nothing_was_downloaded():
    info, _tmp = _fresh()
    assert up.existing_download(info) is None


def test_a_complete_download_is_reused():
    info, _tmp = _fresh()
    dest = up._installer_dest(info)
    dest.write_bytes(b"\0" * SIZE)
    assert up.existing_download(info) == dest


def test_a_truncated_download_is_refused_and_deleted():
    """THE failure this exists for. A half-downloaded installer is a broken
    exe, and leaving it on disk would fool the next attempt."""
    info, _tmp = _fresh()
    dest = up._installer_dest(info)
    dest.write_bytes(b"\0" * (SIZE // 2))
    assert up.existing_download(info) is None
    assert not dest.exists(), \
        "the partial download was left behind for the next attempt to find"


def test_a_file_of_the_wrong_size_is_refused_and_deleted():
    """A release rebuilt under the same tag changes size. Installing the
    old bytes because the name matched would be worse than re-downloading."""
    info, _tmp = _fresh()
    dest = up._installer_dest(info)
    dest.write_bytes(b"\0" * (SIZE + 4096))
    assert up.existing_download(info) is None
    assert not dest.exists()


def test_without_a_published_size_only_a_plausible_file_is_reused():
    """GitHub does not always report a size. The fallback is the same bar
    the download path applies before handing anything to Windows."""
    info, _tmp = _fresh(size=0)
    dest = up._installer_dest(info)
    dest.write_bytes(b"\0" * 200)
    assert up.existing_download(info) is None, "a 200-byte 'installer' was reused"
    dest.write_bytes(b"\0" * SIZE)
    assert up.existing_download(info) == dest


def test_a_directory_in_the_way_does_not_crash_it():
    info, _tmp = _fresh()
    dest = up._installer_dest(info)
    dest.mkdir(parents=True)
    assert up.existing_download(info) is None


# ── pruning ─────────────────────────────────────────────────────────────────

def test_old_installers_are_pruned_but_never_the_current_one():
    info, tmp = _fresh("3.2.0")
    dest = up._installer_dest(info)
    dest.write_bytes(b"\0" * SIZE)
    for old in ("LogicBoardInstaller_v3.0.0.exe",
                "LogicBoardInstaller_v3.1.0.exe"):
        (tmp / old).write_bytes(b"\0" * 1000)
    up._prune_old_downloads(keep=dest)
    assert dest.exists(), "the installer about to be used was deleted"
    assert list(tmp.glob("*.exe")) == [dest], \
        "old installers survived: %s" % [p.name for p in tmp.glob("*.exe")]


# ── the worker skips the network ────────────────────────────────────────────

def test_the_download_worker_returns_an_existing_file_without_downloading():
    """The URL is unreachable on purpose: if the worker touches the network
    at all, this fails."""
    info, _tmp = _fresh()
    dest = up._installer_dest(info)
    dest.write_bytes(b"\0" * SIZE)

    seen = {}
    worker = up.DownloadWorker(info)
    worker.finished.connect(lambda p, e: seen.update(path=p, err=e))
    worker.progress.connect(lambda m, pct: seen.setdefault("first", m))
    worker.run()

    assert seen.get("err") == "", seen
    assert Path(seen["path"]) == dest
    assert "Already" in seen.get("first", ""), \
        "it reported downloading something it did not download"


def test_the_worker_still_downloads_when_there_is_nothing_to_reuse():
    """The other half — the reuse shortcut must not swallow real work. The
    unreachable URL means this fails rather than downloads, which is the
    proof it TRIED."""
    info, _tmp = _fresh()
    seen = {}
    worker = up.DownloadWorker(info)
    worker.finished.connect(lambda p, e: seen.update(path=p, err=e))
    worker.run()
    assert not seen.get("path"), "it invented a path for a file it never got"
    assert seen.get("err"), "a failed download reported success"


# ── consent, twice ──────────────────────────────────────────────────────────

def test_the_notifier_never_downloads_before_asking():
    import inspect
    from ui.util import update_notifier as un
    src = inspect.getsource(un)
    assert "ThreadedDownload" not in src, \
        "the background notifier downloads before the user has agreed"
    assert not hasattr(un.UpdateNotifier, "_prefetch"), \
        "the quiet pre-download is back"


def test_the_notification_does_not_promise_what_it_cannot_deliver():
    """It offers to DOWNLOAD, or — with the installer on disk — to INSTALL."""
    from ui.util import update_notifier as un
    info, _tmp = _fresh()
    n = un.UpdateNotifier.__new__(un.UpdateNotifier)

    n._ready_to_install = True
    ready = n._notification_body(info)
    n._ready_to_install = False
    not_ready = n._notification_body(info)

    assert ready != not_ready, "the wording is the same either way"
    assert "downloaded" in ready.lower()
    assert "download" in not_ready.lower()
    assert "downloaded and is ready" not in not_ready.lower()


def test_later_to_a_download_is_remembered_but_later_to_an_install_is_not():
    from ui.util import update_notifier as un
    info, _tmp = _fresh()
    marked = []
    saved = un._mark_dismissed
    un._mark_dismissed = lambda v: marked.append(v)
    try:
        n = un.UpdateNotifier()
        n._ready_to_install = False
        n._handle_toast_choice(info, "later")
        assert marked == [info.latest_version], \
            "Later to a download should stop the nagging for that version"
        marked.clear()
        n._ready_to_install = True
        n._handle_toast_choice(info, "later")
        assert marked == [], \
            "an installer on disk was dismissed for good — it is never offered again"
    finally:
        un._mark_dismissed = saved


def _run_flow(*, on_disk, ready_answer, install_confirmed=False):
    """run_confirmed_update_flow with every window and the installer launch
    replaced by recorders. Returns what happened, in order."""
    from PySide6.QtWidgets import QDialog
    import ui.panels.update_dialog as ud
    info, tmp = _fresh()
    if on_disk:
        up._installer_dest(info).write_bytes(b"\0" * SIZE)
    seen = []

    class FakeDownload:
        def __init__(self, info, parent=None):
            seen.append("download")
            dest = up._installer_dest(info)
            dest.write_bytes(b"\0" * SIZE)
            self.installer_path, self.error_msg = str(dest), ""

        def exec(self):
            return QDialog.Accepted

    class FakeReady:
        def __init__(self, info, parent=None):
            seen.append("ask-install")

        def exec(self):
            return QDialog.Accepted if ready_answer else QDialog.Rejected

    saved = (ud.UpdateDownloadDialog, ud.UpdateReadyDialog,
             ud._hand_off_to_installer, ud._confirm_board_saved,
             up.newer_than_current)
    ud.UpdateDownloadDialog, ud.UpdateReadyDialog = FakeDownload, FakeReady
    ud._hand_off_to_installer = lambda parent, path: seen.append("install")
    ud._confirm_board_saved = lambda parent: (seen.append("save-check") or True)
    up.newer_than_current = lambda i: True
    try:
        ud.run_confirmed_update_flow(None, info,
                                     install_confirmed=install_confirmed)
    finally:
        (ud.UpdateDownloadDialog, ud.UpdateReadyDialog,
         ud._hand_off_to_installer, ud._confirm_board_saved,
         up.newer_than_current) = saved
    return seen, up._installer_dest(info)


def test_a_finished_download_asks_before_installing():
    seen, _f = _run_flow(on_disk=False, ready_answer=True)
    assert seen == ["download", "ask-install", "save-check", "install"], seen


def test_later_after_downloading_keeps_the_installer():
    seen, f = _run_flow(on_disk=False, ready_answer=False)
    assert seen == ["download", "ask-install"], seen
    assert "install" not in seen
    assert f.is_file(), "Later deleted the download"


def test_an_installer_on_disk_is_not_downloaded_again_but_still_asked_about():
    seen, _f = _run_flow(on_disk=True, ready_answer=True)
    assert seen == ["ask-install", "save-check", "install"], seen


def test_install_now_from_the_toast_is_not_asked_twice():
    seen, _f = _run_flow(on_disk=True, ready_answer=False,
                         install_confirmed=True)
    assert seen == ["save-check", "install"], seen


def test_a_fresh_download_is_always_asked_about_even_if_confirmed():
    """install_confirmed means "they pressed Install now on a file that was
    already there" — if the file had to be fetched, they have not."""
    seen, _f = _run_flow(on_disk=False, ready_answer=False,
                         install_confirmed=True)
    assert "ask-install" in seen and "install" not in seen, seen


def test_spent_installers_are_swept_at_startup():
    info, tmp = _fresh()
    for v in ("3.0.0", "3.1.2", "9.9.9"):
        (tmp / ("LogicBoardInstaller_v%s.exe" % v)).write_bytes(b"x")
    (tmp / "random.exe").write_bytes(b"x")
    removed = up.prune_spent_installers("3.1.2")
    left = sorted(p.name for p in tmp.iterdir())
    assert left == ["LogicBoardInstaller_v9.9.9.exe"], left
    assert len(removed) == 3
    src = (Path(__file__).resolve().parents[2] / "ui" / "app.py").read_text(
        encoding="utf-8")
    assert "singleShot(15_000, _updater.prune_spent_installers)" in src
    assert "singleShot(120_000, _updater.prune_spent_installers)" in src


# ── the same contract, in the other two apps ────────────────────────────────

def _read(rel):
    root = Path(__file__).resolve().parents[3]
    return (root / rel).read_text(encoding="utf-8", errors="replace")


def test_logicshare_windows_reuses_and_prunes_safely():
    src = _read("LogicShare/LogicShare Windows/LogicShare.py")
    assert "def existing_update_download(" in src, \
        "LogicShare Windows never checks for an existing download"
    assert "_prune_updates_except(" in src, \
        "LogicShare Windows still deletes every installer before downloading"
    assert 'UPDATE_DIR / f"LogicShareInstaller_v{info.version_text}.exe"' in src, \
        "the installer is no longer named after the version"
    # The prune must come AFTER the reuse check, or it destroys the file it
    # is about to reuse — which is exactly what it used to do.
    i = src.index("ready = existing_update_download(info)")
    j = src.index("_prune_updates_except(dest)")
    assert i < j, "LogicShare Windows prunes before checking for a reusable file"


# LogicShare Android was rebuilt in Kotlin (the Flutter source was lost), so
# these read the Kotlin updater. The promises are the same ones the Flutter
# version made; only where they live changed.
_ANDROID = "LogicShare/LogicShare Mobile/app/src/main/java/com/innovalogix/logicshare/"


def test_logicshare_android_reuses_and_keeps_its_apk():
    src = _read(_ANDROID + "update/Updater.kt")
    assert "fun existingDownload(ctx: Context, info: Info): File?" in src, \
        "the Android updater never checks for an existing APK"
    assert '"LogicShare_v${info.versionText}.apk"' in src, \
        "the APK is no longer named after the version"
    vm = _read(_ANDROID + "ui/MainViewModel.kt")
    assert "prefetchThenOffer" not in vm, \
        "Android downloads the update before asking again"


def test_android_downloads_then_asks_before_installing():
    vm = _read(_ANDROID + "ui/MainViewModel.kt")
    dl = vm[vm.index("private fun downloadUpdate()"):vm.index("fun dismissUpdate()")]
    assert "launchInstaller" not in dl, \
        "a finished download starts the installer without asking"
    assert "downloaded = true" in dl
    prim = vm[vm.index("fun updatePrimary()"):vm.index("private fun installUpdate()")]
    assert "if (offer.downloaded) installUpdate() else downloadUpdate()" in prim
    dlg = _read(_ANDROID + "ui/Dialogs.kt")
    assert '"Download & Install"' not in dlg and '"Download"' in dlg


def test_android_deletes_the_apk_right_after_updating():
    """Android does not reopen the app after an update, so a sweep that only
    ran when the main screen opened could leave the APK for days."""
    rx = _read(_ANDROID + "ack/AckBootReceiver.kt")
    i = rx.index("ACTION_MY_PACKAGE_REPLACED) {")
    assert "Updater.pruneSpent(" in rx[i:i + 800], \
        "the APK is not deleted when the update completes"


def test_the_android_startup_sweep_no_longer_runs_blind():
    """It used to delete every APK on every launch, including one fetched
    quietly moments earlier. The sweep now keeps a package for a version
    NEWER than the one running — that is an update waiting to be installed."""
    src = _read(_ANDROID + "update/Updater.kt")
    sweep = src[src.index("fun pruneSpent("):]
    assert "compare(v, cur) > 0) return@forEach" in sweep, \
        "the startup sweep deletes a waiting update again"
    assert 'endsWith(".apk")' in sweep, \
        "the sweep touches partial downloads that may be in progress"


def test_the_android_native_side_stopped_deleting_on_every_request():
    """A download must look for a finished package BEFORE it clears old
    ones, and the clear-out must spare the file it is about to write."""
    src = _read(_ANDROID + "update/Updater.kt")
    dl = src[src.index("fun download("):src.index("class CancelledException")]
    reuse = dl.index("existingDownload(ctx, info)")
    clear = dl.index("forEach { it.delete() }")
    assert reuse < clear, "the download clears old packages before checking for reuse"
    assert "it != dest" in dl, "the clear-out deletes the target as well"


if __name__ == "__main__":
    import logging
    logging.disable(logging.ERROR)
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
