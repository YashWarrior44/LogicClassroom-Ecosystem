"""
update_notifier.py — Background update check + native Windows toast.

Called from ``app.py`` when LogicBoard boots in background mode (the
Windows-startup pathway that also spins up the LogicShare service).
Runs the GitHub update check silently on a background thread; if a
newer version is out, shows a native Windows toast. NOTHING is downloaded
before the user agrees:

  * not downloaded yet  ->  "Update available"  [Download] [Later]
  * already on disk     ->  "Update ready to install"  [Install now] [Later]

Either button calls ``on_install_confirmed(info, ready)`` on the Qt main
thread, and the caller runs ``update_dialog.run_confirmed_update_flow``:
download (if needed) -> "install now?" (unless that was the button just
pressed) -> unsaved-board check -> installer. "Later" to a download is
remembered for that version; "Later" to an installer on disk is asked
again next boot.

Fallback
--------
The best-in-class actionable Windows toasts come from the
``windows-toasts`` pip package. If it isn't installed, we degrade to
``QSystemTrayIcon.showMessage`` — a passive Windows notification that
still surfaces the update, but the "Later" button vanishes (the user
just closes the notification to defer). Clicking the notification
still triggers the install flow.

Nag prevention
--------------
Once the user hits "Later" for a specific version, that version is
stored in ``Config/update_notifier_state.json``. Subsequent boots
won't re-notify for the SAME version — but a newer release resets
the check. Prevents the "I already said later, stop bugging me" bad
UX while still surfacing genuinely new updates.
"""
from __future__ import annotations

import json
import logging
from pathlib import Path
from typing import Callable, Optional

from PySide6.QtCore import QObject, QTimer, Signal

from ui.util import paths, updater
from ui.util.version import APP_VERSION, APP_NAME

log = logging.getLogger(__name__)

__all__ = ["UpdateNotifier", "check_in_background"]


# ---------------------------------------------------------------------------
# Persistence — remember what the user has already said "later" to
# ---------------------------------------------------------------------------

def _state_path() -> Path:
    return paths.config_dir() / "update_notifier_state.json"


def _load_state() -> dict:
    try:
        return json.loads(_state_path().read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return {}


def _save_state(state: dict) -> None:
    try:
        _state_path().write_text(
            json.dumps(state, indent=2), encoding="utf-8")
    except OSError as e:
        log.debug("Could not persist notifier state: %s", e)


def _already_dismissed(version: str) -> bool:
    return version in _load_state().get("dismissed_versions", [])


def _mark_dismissed(version: str) -> None:
    state = _load_state()
    dismissed = list(state.get("dismissed_versions", []))
    if version not in dismissed:
        dismissed.append(version)
        state["dismissed_versions"] = dismissed
        _save_state(state)


# ---------------------------------------------------------------------------
# Notifier — bridges the background thread + toast + main-thread callback
# ---------------------------------------------------------------------------

class UpdateNotifier(QObject):
    """Lives for the process lifetime. Owns the check worker and marshals
    the user's toast choice back to the main thread via
    ``install_confirmed``."""

    # Fires ONLY when the user actively chose Download / Install now (a
    # toast button, or clicking the tray-notification body). Ignored
    # dismissals emit nothing.
    #: (updater.UpdateInfo, ready) — ``ready`` True when the choice was
    #: "Install now" on an installer already on disk; False when it was
    #: "Download".
    install_confirmed = Signal(object, bool)

    def __init__(self, parent: Optional[QObject] = None,
                 *, tray_icon=None) -> None:
        super().__init__(parent)
        # Optional QSystemTrayIcon to fall back to if native toasts
        # aren't available. If None and toasts fail, the check just
        # logs and moves on.
        self._tray = tray_icon
        self._check: Optional[updater.ThreadedCheck] = None
        # Held reference to the toaster so its background COM apartment
        # stays alive through the toast's lifetime — dropping the ref
        # too early on Windows can nuke the action-button callbacks
        # before the user gets a chance to click.
        self._toaster = None

    def start(self) -> None:
        """Kick off a silent GitHub check. Returns immediately."""
        if not updater.is_configured():
            log.debug("Update notifier: updater not configured — skipping.")
            return
        # Small startup delay lets the network settle, LogicShare finish
        # registering, and (importantly) any splash / warmup finish before
        # a toast pops up on top of them.
        QTimer.singleShot(5000, self._begin_check)

    #: Set by ``_announce``: whether the installer is already on disk when
    #: the notification goes out — which decides whether it offers to
    #: DOWNLOAD or to INSTALL.
    _ready_to_install: bool = False

    def _begin_check(self) -> None:
        self._check = updater.ThreadedCheck(parent=self)
        self._check.finished.connect(self._on_check_done)
        self._check.start()

    def _on_check_done(self, info, err) -> None:
        if err:
            log.info("Background update check: %s", err)
            return
        if info is None or not updater.newer_than_current(info):
            log.info("Background update check: on latest (%s).", APP_VERSION)
            return
        # NOTHING is downloaded before the user agrees to it. If the
        # installer is already on disk (they downloaded it and chose Later),
        # the question is "install now?"; otherwise it is "download?".
        ready = updater.existing_download(info) is not None
        if not ready and _already_dismissed(info.latest_version):
            log.info("Update %s already dismissed by user — skipping toast.",
                      info.latest_version)
            return
        self._announce(info, ready=ready)

    def _announce(self, info: updater.UpdateInfo, *, ready: bool) -> None:
        """Tell the user, once the answer to "how long will this take" is
        known."""
        self._ready_to_install = bool(ready)
        # Try native actionable toast first; fall back to tray on failure.
        if not self._show_actionable_toast(info):
            self._show_tray_fallback(info)

    # ── Native Windows toast (windows-toasts library) ─────────────────
    def _show_actionable_toast(self, info: updater.UpdateInfo) -> bool:
        """Show a native Windows 10/11 toast with two action buttons.
        Returns True on success. Requires ``windows-toasts`` pip pkg
        AND a registered AppUserModelID (which windows_registration.py
        sets up at startup)."""
        try:
            from windows_toasts import (
                Toast, ToastButton, WindowsToaster,
            )
        except ImportError:
            log.debug("windows-toasts not installed — will use tray fallback.")
            return False

        try:
            # The AUMID here must match the one registered by
            # ``windows_registration.APP_USER_MODEL_ID`` so Windows can
            # look up the app icon + click routing correctly.
            from ui.util.windows_registration import APP_USER_MODEL_ID
            toaster = WindowsToaster(APP_NAME)
            # Some versions of windows-toasts expose the AUMID setter,
            # some infer it from the app-registration entry. Try both
            # without crashing if the attribute is absent.
            try:
                toaster.applicationText = APP_USER_MODEL_ID
            except Exception:
                pass

            toast = Toast()
            toast.text_fields = [
                "Update ready to install" if self._ready_to_install
                else "Update available",
                self._notification_body(info),
            ]
            # Argument strings are echoed back verbatim in the activated
            # event, so the click handler can tell WHICH button fired. The
            # button says what pressing it does: download, or install.
            toast.AddAction(ToastButton(
                "Install now" if self._ready_to_install else "Download",
                "update"))
            toast.AddAction(ToastButton("Later", "later"))

            def _on_activated(event_args):
                # ``arguments`` is the button's argument string, OR empty
                # if the user clicked the toast BODY (not a button). We
                # treat body-click the same as "update" — that matches
                # the platform convention (clicking a notification body
                # launches the associated action).
                try:
                    arg = getattr(event_args, "arguments", "") or ""
                except Exception:
                    arg = ""
                self._handle_toast_choice(info, arg or "update")

            def _on_dismissed(_event_args):
                # Toast timed out without a click, or user cleared it.
                # Same treatment as "Later" — record so we don't nag
                # again for this exact version.
                self._handle_toast_choice(info, "later")

            toast.on_activated = _on_activated
            toast.on_dismissed = _on_dismissed
            toaster.show_toast(toast)
            self._toaster = toaster    # keep alive — see field docstring
            log.info("Update toast shown for v%s.", info.latest_version)
            return True
        except Exception:
            log.exception("Actionable toast failed — falling back to tray.")
            return False

    def _notification_body(self, info: updater.UpdateInfo) -> str:
        """What the notification says.

        The wording turns on whether the installer is already on disk,
        because those are genuinely different offers: one costs a click,
        the other costs a click and however long a school connection takes
        to move 200 MB. Saying "ready to install" when it is not is how an
        app teaches people to distrust its notifications.
        """
        if self._ready_to_install:
            return (f"LogicBoard {info.latest_version} has been downloaded "
                    f"and is ready to install. You're on {APP_VERSION}.")
        return (f"LogicBoard {info.latest_version} is available. Download "
                f"it? You'll be asked again before it installs. "
                f"You're on {APP_VERSION}.")

    # ── Fallback tray notification ────────────────────────────────────
    def _show_tray_fallback(self, info: updater.UpdateInfo) -> None:
        """Passive Windows tray notification. No 'Later' button — the
        user just closes the notification to defer. Clicking the body
        triggers the update flow, same as the toast."""
        if self._tray is None:
            log.info("Update available (v%s) but no toast / tray path "
                      "available. Use Settings → Check for Updates.",
                      info.latest_version)
            return
        try:
            # Route a single messageClicked signal → install_confirmed.
            # Disconnect any prior binding first so repeat notifications
            # don't stack callbacks.
            try:
                self._tray.messageClicked.disconnect()
            except Exception:
                pass
            self._tray.messageClicked.connect(
                lambda: self._handle_toast_choice(info, "update"))
            self._tray.showMessage(
                "LogicBoard update ready" if self._ready_to_install
                else "LogicBoard update available",
                self._notification_body(info)
                + (" Click here to install." if self._ready_to_install
                   else " Click here to download."),
                self._tray.icon(),
                10_000,   # 10 s visible
            )
            log.info("Tray fallback notification shown for v%s.",
                      info.latest_version)
        except Exception:
            log.exception("Tray notification failed.")

    # ── Handling the user's choice ────────────────────────────────────
    def _handle_toast_choice(self, info: updater.UpdateInfo, arg: str) -> None:
        """Called from whatever thread the toast library dispatches on.
        Marshals onto the Qt main thread via a queued singleShot before
        touching the UI-side signal."""
        arg = (arg or "").lower().strip()
        ready = bool(self._ready_to_install)
        if arg == "later":
            # "Later" to a DOWNLOAD is remembered for this version — no nag.
            # "Later" to an installer already on disk is not: it is asked
            # again next time, because it is sitting there ready.
            if not ready:
                _mark_dismissed(info.latest_version)
            log.info("User chose 'Later' for update v%s (%s).",
                      info.latest_version,
                      "downloaded, kept" if ready else "not downloaded")
            return
        # Anything else — "update", empty (body click), unrecognised —
        # runs the install flow. QTimer.singleShot(0, ...) posts to the
        # main thread's event queue so signal emission happens there
        # instead of on the toast library's callback thread.
        log.info("User chose to %s v%s from the toast.",
                  "install" if ready else "download", info.latest_version)
        QTimer.singleShot(
            0, lambda: self.install_confirmed.emit(info, ready))


# ---------------------------------------------------------------------------
# Convenience one-liner used by app.py
# ---------------------------------------------------------------------------

def check_in_background(
    parent: QObject,
    on_install_confirmed: Callable[[object, bool], None],
    *,
    tray_icon=None,
) -> UpdateNotifier:
    """Fire-and-forget background update check with toast. Returns the
    notifier so the caller can keep it alive for the process lifetime
    (dropping the reference cancels the check + kills the toast
    callbacks). ``on_install_confirmed(UpdateInfo)`` runs on the Qt
    main thread when the user clicks Download / Install now."""
    notifier = UpdateNotifier(parent=parent, tray_icon=tray_icon)
    notifier.install_confirmed.connect(on_install_confirmed)
    notifier.start()
    return notifier
