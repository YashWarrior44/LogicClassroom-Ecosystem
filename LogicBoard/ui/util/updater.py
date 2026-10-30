"""
updater.py — LogicBoard's GitHub-backed updater.

How this works, and why it is this short
========================================

LogicBoard updates by DOWNLOADING AND RUNNING ITS OWN INSTALLER. It does
not unpack a release ZIP over itself any more.

The old scheme did: it streamed ``LogicBoard.zip`` from the release,
extracted it into a fresh ``LogicBoard_V<new>`` folder beside the running
one, re-registered the Start Menu / Desktop shortcuts, rewrote the Apps &
Features entry, refreshed App Paths and the HKLM Run value, pruned the old
version folders, and — because Program Files is not writable by an
asInvoker process — did the whole privileged half inside an elevated
re-launch of itself behind an ``--apply-update`` flag.

Every one of those steps is something ``LogicBoardInstaller.exe`` already
does, has always done, and is exercised by every fresh install. Duplicating
them in Python meant maintaining a second implementation of the install that
only ever ran on the update path — so it was the one that got a shortcut
pointing at a deleted folder, or an Apps & Features entry still claiming the
previous version. Handing the job back to the installer collapses install
and update into a single code path, and the installer already terminates a
running LogicBoard (tray service included) as its first step, which is why
nothing here has to.

So the whole flow is:

    ask GitHub for the newest release
      -> compare tags
      -> download the .exe asset
      -> launch it elevated (one UAC prompt)
      -> quit and let the installer take over.

``ui/panels/update_dialog.py`` drives that, and asks about an unsaved board
before the hand-off — the installer kills this process, so anything not on
disk by then is gone.

Threading: the HTTP work happens on background QThreads so the UI stays
responsive. Progress and results come back through Qt signals.

Failure modes handled:
  * Network unreachable       -> "couldn't reach GitHub"
  * GitHub 404 / no releases yet
  * Rate-limited API responses (one shared school IP hits this)
  * Release with no installer attached
  * Truncated download        -> rejected before it is ever launched
  * UAC prompt declined       -> reported, app stays running
"""
from __future__ import annotations

import json
import logging
import re
import sys
import urllib.error
import urllib.request
from dataclasses import dataclass
from pathlib import Path
from typing import Optional

from PySide6.QtCore import QObject, QThread, Signal

from ui.util.version import APP_VERSION, SemVer
from ui.util import net_http, paths

log = logging.getLogger(__name__)

# ---------------------------------------------------------------------------
# Configuration
# ---------------------------------------------------------------------------
# The repo whose Releases page publishes LogicBoard builds, as
# "<owner>/<repo>". Everything in this module is inert while it is empty,
# so blanking it is the supported way to turn updates off in a dev build.
#
# ONE repo for the whole ecosystem: LogicBoard and LogicShare release from
# the same Releases page, told apart by their tags —
#
#     LogicBoard-v3.1.3      the LogicBoard installer
#     LogicShare-v1.1.2      LogicShare's Windows installer and Android APK
#
# so "releases/latest" can no longer be trusted: it is whichever app was
# published last. The check lists the releases and takes the newest one
# whose tag carries OUR prefix and which has an installer attached.
GITHUB_REPOSITORY = "YashWarrior44/LogicClassroom-Ecosystem"
RELEASE_TAG_PREFIX = "LogicBoard-"

# Non-draft releases, newest first. A page of 50 reaches back far enough to
# find ours however often the other app has shipped in between.
_GITHUB_API_TMPL = "https://api.github.com/repos/{repo}/releases?per_page=50"

# Every request goes through net_http: WinINet on Windows, so certificates
# and the school's proxy settings are handled the way Edge handles them.
# urllib alone could not reach GitHub from a classroom board — see that
# module's docstring. A module attribute so tests can stand in for it.
_open = net_http.open_url

# HTTP knobs. ``_UA`` matters — GitHub returns 403 to a request with no
# User-Agent header.
_UA = f"LogicBoard-Updater/{APP_VERSION}"
_HTTP_TIMEOUT_SECS = 20

# Bytes per read on the download stream. Big enough to keep syscalls
# infrequent, small enough that the progress bar moves smoothly.
_DOWNLOAD_CHUNK = 256 * 1024

# Anything smaller than this cannot be an installer, whatever the server
# said. Guards against a captive-portal HTML page being launched as an exe.
_MIN_INSTALLER_BYTES = 1024


# ---------------------------------------------------------------------------
# Result type passed through signals
# ---------------------------------------------------------------------------

@dataclass(frozen=True)
class UpdateInfo:
    """What the release endpoint told us about the newest published build.

    Immutable so the UI can stash it and hand it to the download thread
    without cloning."""
    latest_version: str          # tag string as GitHub reported it
    latest_semver: SemVer        # parsed, for comparison
    installer_url: str           # .exe asset download URL
    asset_name: str              # the asset's name on the release
    asset_size: int              # bytes, 0 if GitHub didn't say
    release_notes: str           # markdown body of the release
    release_url: str             # HTML URL for "read more"


# ---------------------------------------------------------------------------
# Public entrypoints
# ---------------------------------------------------------------------------

def is_configured() -> bool:
    """Whether the updater is usable at all. If ``GITHUB_REPOSITORY`` is
    blank we treat every request as a no-op and callers surface a friendly
    "updates are disabled in this build" message."""
    return bool(GITHUB_REPOSITORY.strip())


def newer_than_current(info: UpdateInfo) -> bool:
    """True if ``info`` is strictly newer than the running version.

    A free function so the Settings page, the background notifier and the
    update dialog can all ask without instantiating anything. An
    unparseable version on either side answers False — a missed update the
    user can still install by hand is a much better failure than pushing a
    bogus one over a working install."""
    current = SemVer.parse(APP_VERSION)
    if current is None or info is None or info.latest_semver is None:
        return False
    return info.latest_semver > current


def launch_installer(path: Path) -> tuple[bool, str]:
    """Start the downloaded installer, elevated. Returns ``(ok, message)``.

    ``ShellExecuteW`` with the ``runas`` verb, not ``subprocess``.
    LogicBoardInstaller.exe carries a ``requireAdministrator`` manifest, and
    ``CreateProcess`` — which is what subprocess uses — refuses those
    outright with ERROR_ELEVATION_REQUIRED instead of showing a UAC prompt.
    Only the shell can raise the prompt.

    The call returns once the user has answered that prompt, so a decline is
    detectable and we can stay running instead of quitting into nothing.
    """
    if sys.platform != "win32":
        return False, "Automatic updates are only supported on Windows."
    try:
        import ctypes
        SW_SHOWNORMAL = 1
        rc = int(ctypes.windll.shell32.ShellExecuteW(
            None, "runas", str(path), None, str(Path(path).parent),
            SW_SHOWNORMAL))
        # ShellExecuteW returns a fake HINSTANCE: anything above 32 is
        # success, and the values at or below it are error codes.
        if rc > 32:
            log.info("Update installer launched: %s", path)
            return True, ""
        if rc == 5:     # SE_ERR_ACCESSDENIED — the UAC prompt was declined
            return False, ("The update needs administrator permission to "
                           "install, and the request was declined.")
        return False, f"Windows couldn't start the installer (error {rc})."
    except Exception as e:
        log.exception("Could not launch the update installer")
        return False, f"Couldn't start the installer: {e}"


# ============================================================================
# Check worker — runs the GitHub API call on a background thread
# ============================================================================

class CheckWorker(QObject):
    """Emits ``finished(UpdateInfo | None, error_message)`` once done.

    On success: ``(UpdateInfo, "")``. On known failure: ``(None, message)``.
    "Already up to date" is NOT an error here — the caller compares versions
    itself via ``newer_than_current`` so the UI can say "you're up to date,
    running vX.Y.Z" instead of showing nothing.
    """
    finished = Signal(object, str)   # UpdateInfo | None, error_msg

    def run(self) -> None:
        if not is_configured():
            self.finished.emit(None, "Update checks are disabled in this build.")
            return
        url = _GITHUB_API_TMPL.format(repo=GITHUB_REPOSITORY.strip("/ "))
        try:
            data = _http_json(url)
        except urllib.error.HTTPError as e:
            if e.code == 404:
                self.finished.emit(None, "No releases have been published yet.")
                return
            if e.code == 403:
                # Unauthenticated API calls are rate-limited per IP. On a
                # school network that is one shared IP for every machine in
                # the building, so this is a realistic outcome and deserves
                # its own wording rather than a bare HTTP code.
                self.finished.emit(None, "GitHub is rate-limiting update "
                                         "checks right now. Try again in a "
                                         "little while.")
                return
            self.finished.emit(None, f"GitHub responded with HTTP {e.code}.")
            return
        except urllib.error.URLError as e:
            # The reason used to be dropped here, so a board that could not
            # reach GitHub said only that, and its log said nothing at all.
            log.warning("Update check could not reach GitHub: %s",
                        getattr(e, "detail", None) or e.reason)
            self.finished.emit(None, _unreachable_message(e))
            return
        except ValueError:
            self.finished.emit(None, "GitHub sent back something unreadable.")
            return
        except Exception as e:
            log.exception("Unexpected update-check failure")
            self.finished.emit(None, f"Update check failed: {e}")
            return

        try:
            info = _parse_release(_select_release(data))
        except _NoRelease:
            self.finished.emit(None, "No LogicBoard release has been "
                                     "published yet.")
            return
        except _NoUsableAsset:
            self.finished.emit(None, "The latest release doesn't have an "
                                     "installer attached to it yet.")
            return
        except _BadVersionTag as e:
            log.warning("Latest release tag is unparseable: %s", e)
            self.finished.emit(None, f"The latest release is tagged “{e}”, "
                                     "which isn't a version number LogicBoard "
                                     "understands.")
            return
        self.finished.emit(info, "")


# ============================================================================
# Download worker — streams the installer to disk
# ============================================================================

class DownloadWorker(QObject):
    """Streams the release's installer into ``paths.updates_dir()``.

    Does NOT launch it — that is the caller's job, so the UI can show a
    "starting the installer…" line and quit cleanly on its own terms.
    """
    progress = Signal(str, int)          # message, percent 0-100
    finished = Signal(str, str)          # saved installer path, error_msg

    def __init__(self, info: UpdateInfo) -> None:
        super().__init__()
        self._info = info
        self._cancelled = False

    def cancel(self) -> None:
        # Best-effort cooperative cancel; the download loop checks this
        # between chunks so the UI can abort a running download.
        self._cancelled = True

    def run(self) -> None:
        info = self._info
        dest = None
        try:
            dest = _installer_dest(info)

            # Already here and intact? Then there is nothing to download,
            # and re-fetching 200 MB the machine is already holding is the
            # opposite of helpful on a school connection.
            ready = existing_download(info)
            if ready is not None:
                _prune_old_downloads(keep=ready)
                self.progress.emit("Already downloaded.", 100)
                self.finished.emit(str(ready), "")
                return

            self.progress.emit("Downloading the installer…", 0)
            _prune_old_downloads(keep=dest)

            with _open(info.installer_url, {"User-Agent": _UA},
                       _HTTP_TIMEOUT_SECS) as resp:
                total = int(resp.getheader("Content-Length") or info.asset_size or 0)
                received = 0
                # buffering=0 → the OS write cache absorbs the writes
                # directly. The chunk size is already large enough that a
                # Python-side buffer would be pure overhead.
                with open(dest, "wb", buffering=0) as fp:
                    while True:
                        if self._cancelled:
                            raise _Cancelled()
                        chunk = resp.read(_DOWNLOAD_CHUNK)
                        if not chunk:
                            break
                        fp.write(chunk)
                        received += len(chunk)
                        self._report(received, total)

            # A truncated download is a real failure mode on flaky school
            # Wi-Fi, and a half-downloaded installer is a broken exe that
            # Windows will happily try to run. If the server told us how big
            # the file should be, hold it to that.
            actual = dest.stat().st_size
            if total and actual < total:
                raise _DownloadFailed(
                    f"it stopped early ({_mb(actual)} of {_mb(total)})")
            if actual < _MIN_INSTALLER_BYTES:
                raise _DownloadFailed(
                    "the downloaded file is too small to be an installer")

            self.progress.emit("Download complete.", 100)
            self.finished.emit(str(dest), "")

        except _Cancelled:
            _discard(dest)
            self.finished.emit("", "")          # cancelled: nothing to report
        except _DownloadFailed as e:
            log.warning("Update download failed: %s", e)
            _discard(dest)
            self.finished.emit("", f"The download failed — {e}")
        except urllib.error.HTTPError as e:
            log.warning("Update download refused: HTTP %s", e.code)
            _discard(dest)
            self.finished.emit("", f"The download failed — GitHub responded "
                                   f"with HTTP {e.code}.")
        except urllib.error.URLError as e:
            log.warning("Update download could not reach GitHub: %s",
                        getattr(e, "detail", None) or e.reason)
            _discard(dest)
            self.finished.emit("", "The download failed. "
                                   + _unreachable_message(e))
        except Exception as e:
            log.exception("Update download crashed")
            _discard(dest)
            self.finished.emit("", f"The download failed: {e}")

    def _report(self, received: int, total: int) -> None:
        if total > 0:
            pct = int(min(100, received * 100 // total))
            self.progress.emit(
                f"Downloading… {_mb(received)} of {_mb(total)}", pct)
        else:
            # No Content-Length: report bytes rather than inventing a
            # percentage. The dialog turns a 0 into a busy bar.
            self.progress.emit(f"Downloading… {_mb(received)}", 0)


# ============================================================================
# Internals
# ============================================================================

class _NoUsableAsset(Exception): pass
class _NoRelease(Exception): pass
class _BadVersionTag(Exception): pass
class _DownloadFailed(Exception): pass
class _Cancelled(Exception): pass


def _http_json(url: str) -> dict:
    """GET a URL and parse JSON."""
    with _open(url, {"User-Agent": _UA,
                     "Accept": "application/vnd.github+json"},
               _HTTP_TIMEOUT_SECS) as resp:
        body = resp.read()
    return json.loads(body.decode("utf-8"))


def _unreachable_message(e: urllib.error.URLError) -> str:
    """What to tell the teacher when GitHub could not be reached.

    net_http phrases its failures as advice — a wrong clock, a network that
    inspects secure traffic — and that is the useful part; a bare URLError
    (from anything else) gets the old generic line."""
    if isinstance(e, net_http.NetError):
        return e.reason
    return "Couldn't reach GitHub — check your network."


_TAG_VERSION = re.compile(r"(\d+)\.(\d+)(?:\.(\d+))?")


def _tag_semver(tag: str) -> Optional[SemVer]:
    """The version in a release tag: "LogicBoard-v3.1.3", "v3.1.3" and
    "3.1.3" all give 3.1.3. SemVer.parse alone reads "LogicBoard-v3.1.3"
    as the prerelease suffix of a version called "LogicBoard"."""
    s = str(tag or "").strip()
    if s.lower().startswith(RELEASE_TAG_PREFIX.lower()):
        s = s[len(RELEASE_TAG_PREFIX):]
    sv = SemVer.parse(s)
    if sv is not None:
        return sv
    m = _TAG_VERSION.search(s)
    return SemVer.parse(m.group(0)) if m else None


def _select_release(data) -> dict:
    """THIS app's newest release out of the shared repo's list.

    Accepts the list ``/releases`` returns; a single release object (the old
    ``/releases/latest`` shape) is treated as a list of one, so a test or a
    future endpoint change hands it straight through. Drafts and
    prereleases are skipped, as ``/latest`` skipped them. A release must be
    tagged for LogicBoard AND carry an installer; the highest version wins,
    whatever order GitHub listed them in."""
    items = data if isinstance(data, list) else [data]
    best, best_v = None, None
    tagged_ours = False
    for r in items:
        if not isinstance(r, dict) or r.get("draft") or r.get("prerelease"):
            continue
        tag = str(r.get("tag_name") or "")
        if isinstance(data, list) and not tag.lower().startswith(
                RELEASE_TAG_PREFIX.lower()):
            continue
        tagged_ours = True
        v = _tag_semver(tag)
        if v is None or _pick_installer_asset(r.get("assets")) is None:
            continue
        if best_v is None or v > best_v:
            best, best_v = r, v
    if best is not None:
        return best
    if not tagged_ours:
        raise _NoRelease()
    # Ours exist but none is usable — let _parse_release say exactly why.
    for r in items:
        if isinstance(r, dict) and str(r.get("tag_name") or "").lower() \
                .startswith(RELEASE_TAG_PREFIX.lower()):
            return r
    return items[0]


def _parse_release(data: dict) -> UpdateInfo:
    tag = str(data.get("tag_name") or data.get("name") or "").strip()
    sv = _tag_semver(tag)
    if sv is None:
        raise _BadVersionTag(tag)
    asset = _pick_installer_asset(data.get("assets"))
    if asset is None:
        raise _NoUsableAsset()
    return UpdateInfo(
        # "v3.1.3", not the raw "LogicBoard-v3.1.3": this is what the
        # dialogs and the toast show the teacher.
        latest_version=f"v{sv}",
        latest_semver=sv,
        installer_url=str(asset.get("browser_download_url")),
        asset_name=str(asset.get("name") or ""),
        asset_size=int(asset.get("size") or 0),
        release_notes=str(data.get("body") or ""),
        release_url=str(data.get("html_url") or ""),
    )


def _pick_installer_asset(assets) -> Optional[dict]:
    """Choose the installer from a release's asset list.

    Ranked rather than "first .exe", because a release can carry more than
    one attachment — a portable build, a debug bundle, checksums — and
    downloading the wrong one means launching something that is not an
    installer and cannot update anything. An installer-looking name wins
    over a bare .exe, and a .zip never wins at all: this updater RUNS what
    it downloads, so an archive is useless to it.
    """
    best, best_rank = None, -1
    for a in assets or []:
        name = str(a.get("name") or "")
        if not a.get("browser_download_url") or not name.lower().endswith(".exe"):
            continue
        low = name.lower()
        if "uninstall" in low:
            continue                      # never hand the user that one
        rank = 0
        if "installer" in low or "setup" in low:
            rank = 2
        elif "logicboard" in low:
            rank = 1
        if rank > best_rank:
            best, best_rank = a, rank
    return best


def _installer_dest(info: UpdateInfo) -> Path:
    """Where this release's installer gets saved.

    Named after the VERSION rather than after the asset, so a release whose
    asset name carries a space or a character Windows dislikes can't produce
    a path that won't launch."""
    return paths.updates_dir() / f"LogicBoardInstaller_v{info.latest_semver}.exe"


def existing_download(info: UpdateInfo) -> Optional[Path]:
    """A previously-downloaded installer for THIS release, if it is intact.

    Returns the path, or None if there isn't one worth trusting.

    WHY THIS MATTERS
    ================
    The installer is around 200 MB. Downloading it again because the app
    restarted, or because the user pressed Update after dismissing the
    notification once, is 200 MB of a school's bandwidth for a file already
    sitting on the disk.

    WHAT "INTACT" MEANS, AND WHAT IT DOES NOT
    =========================================
    The size GitHub published, matched exactly. That catches the failure
    that actually happens — a download cut short by a flaky connection,
    which leaves a file that exists, looks plausible, and is a broken exe
    that Windows will cheerfully try to run.

    It is NOT a checksum. GitHub does not publish one for release assets in
    the form this endpoint returns, so there is nothing to compare against;
    inventing a weaker check and calling it verification would be worse than
    being clear about the limit. The size comes from the release metadata
    fetched moments earlier over HTTPS, and the file was written by this
    app into a per-user directory, so the threat this cannot address is a
    local attacker who can already write to the user's own AppData — at
    which point the installer is not the weakest thing on the machine.

    When GitHub reports no size at all, a file is only reused if it is at
    least plausibly an installer. That is weaker, and it is the same bar the
    download path itself applies before handing anything to Windows.
    """
    try:
        dest = _installer_dest(info)
        if not dest.is_file():
            return None
        actual = dest.stat().st_size
        if info.asset_size:
            if actual != info.asset_size:
                log.info("Discarding %s: %s on disk, release says %s",
                         dest.name, _mb(actual), _mb(info.asset_size))
                _discard(dest)
                return None
        elif actual < _MIN_INSTALLER_BYTES:
            _discard(dest)
            return None
        log.info("Reusing the installer already downloaded for v%s (%s)",
                 info.latest_version, _mb(actual))
        return dest
    except OSError as e:
        log.debug("Could not check for an existing download: %s", e)
        return None


def _prune_old_downloads(*, keep: Path) -> None:
    """Delete installers left by earlier update attempts.

    These are ~200 MB each and nothing ever reads an old one — without this
    the folder grows by an installer per update, forever. A file still
    locked by a running installer simply refuses to delete, which is fine:
    the next attempt tries again."""
    try:
        for stale in paths.updates_dir().glob("*.exe"):
            if stale == keep:
                continue
            try:
                stale.unlink()
            except OSError:
                pass
    except OSError as e:
        log.debug("Could not prune old installer downloads: %s", e)


def prune_spent_installers(current: Optional[str] = None) -> list:
    """Delete downloaded installers this machine no longer needs.

    ``_prune_old_downloads`` only runs when the NEXT update is downloaded,
    so the ~200 MB installer that just did its job sat in the Updates folder
    until some future release came along. Every launch now sweeps it:

      - an installer for a version <= the one running is spent;
      - one for a NEWER version is kept — the user downloaded it and chose
        "Later" at "install now?", and it is what the next offer installs;
      - a file whose name carries no version can never be reused; it goes.

    Returns the names deleted. One still locked (the installer that just
    relaunched LogicBoard may not have exited yet) is left for the retry —
    see the two timers in ``app.run``.
    """
    removed: list = []
    cur = SemVer.parse(current or APP_VERSION)
    if cur is None:
        return removed
    try:
        for f in paths.updates_dir().glob("*.exe"):
            # SemVer.parse wants the version on its own; the name is
            # "LogicBoardInstaller_v3.1.3".
            m = re.search(r"(\d+\.\d+(?:\.\d+)?)", f.stem)
            v = SemVer.parse(m.group(1)) if m else None
            if v is not None and v > cur:
                continue                       # a waiting update — keep it
            try:
                f.unlink()
                removed.append(f.name)
            except OSError as e:
                log.debug("Could not remove %s yet: %s", f.name, e)
    except OSError as e:
        log.debug("Could not sweep old installers: %s", e)
    if removed:
        log.info("Removed spent installer(s): %s", ", ".join(removed))
    return removed


def _discard(path: Optional[Path]) -> None:
    """Delete a partial download so it can never be mistaken for a good one
    by a later attempt that only checked whether the file existed."""
    if not path:
        return
    try:
        Path(path).unlink(missing_ok=True)
    except OSError:
        pass


def _mb(n: int) -> str:
    return f"{n / (1024 * 1024):.1f} MB"


# ============================================================================
# Convenience — QThread runners so callers don't build one each time
# ============================================================================

class ThreadedCheck(QObject):
    """Wraps CheckWorker + QThread lifecycle. Owns both, cleans up when
    finished. The preferred entry point from UI code::

        chk = ThreadedCheck(parent=self)
        chk.finished.connect(self._on_check_result)
        chk.start()
    """
    finished = Signal(object, str)

    def __init__(self, parent=None) -> None:
        super().__init__(parent)
        self._thread = QThread(self)
        self._worker = CheckWorker()
        self._worker.moveToThread(self._thread)
        self._thread.started.connect(self._worker.run)
        self._worker.finished.connect(self._on_done)

    def start(self) -> None:
        self._thread.start()

    def isRunning(self) -> bool:
        return self._thread.isRunning()

    def _on_done(self, info, err) -> None:
        self.finished.emit(info, err)
        self._thread.quit()
        self._thread.wait()


class ThreadedDownload(QObject):
    """Same as ``ThreadedCheck`` but for the download phase."""
    progress = Signal(str, int)
    finished = Signal(str, str)

    def __init__(self, info: UpdateInfo, parent=None) -> None:
        super().__init__(parent)
        self._thread = QThread(self)
        self._worker = DownloadWorker(info)
        self._worker.moveToThread(self._thread)
        self._thread.started.connect(self._worker.run)
        self._worker.progress.connect(self.progress)
        self._worker.finished.connect(self._on_done)

    def start(self) -> None:
        self._thread.start()

    def cancel(self) -> None:
        self._worker.cancel()

    def _on_done(self, installer_path, err) -> None:
        self.finished.emit(installer_path, err)
        self._thread.quit()
        self._thread.wait()
