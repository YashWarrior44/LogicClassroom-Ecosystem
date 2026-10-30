"""
test_update_consent.py — an update is downloaded only when the user says so,
and installed only when the user says so AGAIN.

    python test_update_consent.py

Drives the real UpdateDialog with the network and the installer launch
replaced by recorders:

  1. nothing downloads before "Download";
  2. a finished download does NOT start the installer — the dialog turns
     into "Update downloaded — install now?";
  3. "Install Now" starts it; "Later" keeps the file;
  4. an installer already on disk opens straight on "install now?";
  5. the automatic startup check no longer fetches anything quietly.
"""
from __future__ import annotations

import os
import sys
import tempfile
from pathlib import Path

os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")
HERE = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, HERE)

import LogicShare as L                                    # noqa: E402
from PySide6.QtWidgets import QApplication                # noqa: E402

_app = QApplication.instance() or QApplication([])


def _info(downloaded=None):
    i = L.UpdateInfo("v9.9.9", (9, 9, 9), "https://example.invalid/x.exe",
                     "LogicShareInstaller.exe", 5_000_000, "notes", "")
    i.downloaded_path = downloaded
    return i


class _Fakes:
    """Record downloads and installer launches instead of doing them."""

    def __enter__(self):
        self.downloads, self.launches = [], []
        fakes = self

        class FakeWorker(L.QObject):
            progress = L.Signal(object, object)
            done = L.Signal(str, str)

            def __init__(self, info, parent=None):
                super().__init__(parent)
                fakes.downloads.append(info.version_text)

            def start(self):
                pass

            def isRunning(self):
                return False

            def cancel(self):
                pass

        self._saved = (L.UpdateDownloadWorker, L._launch_installer)
        L.UpdateDownloadWorker = FakeWorker
        L._launch_installer = lambda p: (fakes.launches.append(p) or (True, ""))
        return self

    def __exit__(self, *_a):
        L.UpdateDownloadWorker, L._launch_installer = self._saved


def _quit_guard():
    saved = QApplication.quit
    QApplication.quit = staticmethod(lambda: None)
    return saved


def test_nothing_downloads_before_the_user_says_download():
    with _Fakes() as f:
        d = L.UpdateDialog(None, _info())
        assert f.downloads == [], "a download started before anyone agreed"
        assert d.go_btn.text() == "Download", d.go_btn.text()
        assert "available" in d.title.text().lower()
        d.go_btn.click()
        assert f.downloads == ["9.9.9"]


def test_a_finished_download_asks_before_installing():
    with _Fakes() as f:
        d = L.UpdateDialog(None, _info())
        d.go_btn.click()
        d._on_downloaded(r"C:\fake\LogicShareInstaller_v9.9.9.exe", "")
        assert f.launches == [], "the installer started without being asked"
        assert d.go_btn.text() == "Install Now", d.go_btn.text()
        assert "downloaded" in d.title.text().lower(), d.title.text()
        assert d.later_btn.text() == "Later"


def test_install_now_starts_the_installer():
    saved = _quit_guard()
    try:
        with _Fakes() as f:
            d = L.UpdateDialog(None, _info())
            d.go_btn.click()
            path = r"C:\fake\LogicShareInstaller_v9.9.9.exe"
            d._on_downloaded(path, "")
            d.go_btn.click()
            assert f.launches == [path], f.launches
    finally:
        QApplication.quit = saved


def test_later_after_downloading_installs_nothing():
    with _Fakes() as f:
        d = L.UpdateDialog(None, _info())
        d.go_btn.click()
        d._on_downloaded(r"C:\fake\LogicShareInstaller_v9.9.9.exe", "")
        d.later_btn.click()
        assert f.launches == []


def test_an_installer_already_on_disk_opens_on_install_now():
    with _Fakes() as f:
        d = L.UpdateDialog(None, _info(downloaded=r"C:\fake\x.exe"))
        assert d.go_btn.text() == "Install Now"
        assert f.downloads == [], "re-downloaded a file already on disk"


def test_the_startup_check_no_longer_fetches_quietly():
    import inspect
    assert not hasattr(L, "_prefetch_update_then"), \
        "the quiet pre-download is back"
    src = inspect.getsource(L.check_for_updates)
    assert "UpdateDownloadWorker" not in src, \
        "the check downloads before the user has been asked"


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
    sys.stdout.flush()
    os._exit(1 if failures else 0)
