"""
application_core.py — long-lived process state for LogicBoard.

Owns the components that must survive the user closing the main window:

  * QApplication
  * LogicShareService (file receiver + Zeroconf + notifications)
  * NotificationManager (toast on file/announcement arrival)
  * LSConfig (room/class/section/device configuration)
  * ActivityLog
  * InstanceServer (named-pipe IPC for second-launch handling)
  * System tray icon (so the user can re-open the UI or quit completely)

DOES NOT own:

  * MainWindow (created lazily; destroyed on close)
  * Canvas / engine state / boards
  * Toolbars / panels / floating windows

Lifecycle states:

   ┌─────────────┐  manual launch    ┌─────────────┐
   │  not running │ ───────────────▶ │   UI shown  │
   └─────────────┘                   └──────┬──────┘
          ▲                                 │  Close button
          │                                 ▼
          │ Quit (tray)                ┌────────────┐
          └──────────────────────────  │ background │
                                       └──────┬─────┘
                                              │  Open (tray click / second launch)
                                              ▼
                                       ┌─────────────┐
                                       │   UI shown  │
                                       └─────────────┘

Windows-startup launch (``--background``) skips the first "UI shown" step
and boots straight into the background state with no window.

The ``last_board_path`` is persisted on close so reopen can restore the
user's work in 1-3 seconds instead of starting blank.
"""
from __future__ import annotations

import logging
from pathlib import Path
from typing import Optional, Any

from PySide6.QtCore import QObject, QTimer
from PySide6.QtGui import QIcon, QAction
from PySide6.QtWidgets import QApplication, QSystemTrayIcon, QMenu

from ui.util.single_instance import InstanceServer

log = logging.getLogger(__name__)


# ════════════════════════════════════════════════════════════════════════════

class ApplicationCore(QObject):
    """The long-lived owner of LogicShare + the process lifecycle.

    Usage:

        core = ApplicationCore(qapp, ls_config, ls_activity, ls_service,
                               ls_notifications, last_board_path)
        core.start_ipc_server()
        if not args.background:
            core.show_ui()
        sys.exit(qapp.exec())
    """

    def __init__(self,
                 qapp: QApplication,
                 ls_config,
                 ls_activity,
                 ls_service,
                 ls_notifications,
                 app_icon: QIcon,
                 settings_file: Path,
                 saves_dir: Path,
                 parent: Optional[QObject] = None) -> None:
        super().__init__(parent)
        self._qapp = qapp
        self._ls_config = ls_config
        self._ls_activity = ls_activity
        self._ls_service = ls_service
        self._ls_notifications = ls_notifications
        self._app_icon = app_icon
        self._settings_file = settings_file
        self._saves_dir = saves_dir

        # MainWindow is born lazily and may die any number of times.
        self._main_window: Optional[Any] = None
        # Last board path the user had open. Persisted in settings.json so
        # we survive process restart too.
        self._last_board_path: Optional[Path] = self._load_last_board_path()
        # False at construction, and only hide_ui ever sets it. That is what
        # separates "the user put the board away a minute ago and is opening
        # it again" from "LogicBoard just started" — see _construct_window.

        # Single-instance IPC server. ``start_ipc_server`` binds the pipe;
        # second-launch processes that connect end up here.
        self._ipc = InstanceServer(self)
        self._ipc.show_requested.connect(self.show_ui)
        # A board / PDF / deck double-clicked while we were already running.
        self._ipc.open_requested.connect(self.open_file)
        # Files waiting for a window to open into, oldest first. A queue
        # because Explorer starts one LogicBoard per file when several are
        # opened together, and each arrives here as its own message.
        self._pending_files: list[str] = []
        self._opening_file = False
        self._ipc.quit_requested.connect(self.shutdown)
        # A newer LogicBoard version just launched → gracefully step aside
        # so it can take over the primary role. ``supersede_requested``
        # passes the newer version string for logging.
        self._ipc.supersede_requested.connect(self._on_superseded)

        # System tray — gives the user a way to re-open the UI without
        # double-clicking the desktop shortcut (which goes through the
        # second-launch IPC path).
        self._tray: Optional[QSystemTrayIcon] = None
        self._init_tray()

        # Restore splash (see _build_window). The FIRST show is already
        # covered by the startup splash in app.run(), so only re-opens from
        # background mode get one. ``_restore_anim`` holds the running
        # progress animation — without a reference Python would collect it
        # mid-flight and the splash would hang at 0 %.
        self._first_show_done: bool = False
        self._restore_anim = None
        self._restore_splash = None

        # When the LAST top-level Qt window goes away, Qt's default is to
        # quit the QApplication. We DON'T want that — the process should
        # stay alive holding LogicShareService. setQuitOnLastWindowClosed
        # disables it; only ``shutdown()`` quits the app.
        self._qapp.setQuitOnLastWindowClosed(False)

    # ──────────────────────────────────────────────────────────────────────
    # Public API
    # ──────────────────────────────────────────────────────────────────────

    def start_ipc_server(self) -> bool:
        """Bind the named-pipe IPC server. Must be called early — every
        future launch that detects an existing pipe will try to talk to us.
        """
        return self._ipc.start()

    def ipc_peer_alive(self) -> bool:
        """After a failed ``start_ipc_server``: was the pipe taken by a LIVE
        LogicBoard (we're a redundant second launch) rather than lost to a
        genuine bind failure?"""
        return bool(getattr(self._ipc, "peer_alive", False))

    @property
    def ls_service(self):
        return self._ls_service

    @property
    def main_window(self):
        return self._main_window

    def show_ui(self) -> None:
        """Create (or re-raise) the MainWindow.

        Idempotent — calling this when the UI is already up just brings it
        forward. The UI build is on the GUI thread (it touches QWidgets);
        it typically takes 800ms-2s on a cold cache for a typical board.

        Re-entrancy: ``MainWindow.__init__`` calls ``processEvents`` for
        the splash; if a second show_ui fires from a queued signal
        (e.g. user mashes the tray icon while the first build is in
        progress), the ``_main_window is None`` check would still see
        None and start a second build. ``_building_window`` flag closes
        that gap.
        """
        if self._main_window is not None:
            log.info("show_ui: window already exists, raising")
            self._raise_existing_window()
            return
        if getattr(self, "_building_window", False):
            log.info("show_ui: build already in progress, ignoring re-entry")
            return
        self._building_window = True
        # NOTE: no `finally` clearing the flag. A restore shows a splash for
        # three seconds BEFORE the window is built, so this call can return
        # long before the build happens. Clearing the flag here would reopen
        # the re-entrancy hole it exists to close — a second tray click
        # during the splash would start a parallel build. Ownership of the
        # flag belongs to _construct_window (and the except below).
        try:
            self._build_window()
        except Exception:
            self._building_window = False
            raise

    def open_file(self, path: str) -> None:
        """Open a file Windows handed us — a .lb board, a PDF or a deck.

        Shows the window first if it is not up (a file opened while
        LogicBoard sat in the background), then lets MainWindow decide what
        opening means for that kind of file. Queued: the window may still be
        building (the restore path builds it after a three-second splash),
        and a PDF opens a picker that must not be stacked on another.
        """
        if not path:
            return
        self._pending_files.append(str(path))
        self.show_ui()
        QTimer.singleShot(0, self._drain_pending_files)

    def _drain_pending_files(self) -> None:
        if self._opening_file or not self._pending_files:
            return
        win = self._main_window
        if win is None or getattr(self, "_building_window", False):
            # Not built yet — _construct_window calls back when it is.
            return
        self._opening_file = True
        try:
            path = self._pending_files.pop(0)
            try:
                self._raise_existing_window()
            except Exception:
                log.debug("raise before open failed", exc_info=True)
            try:
                win.open_external_file(Path(path))
            except Exception:
                log.exception("Opening %s failed", path)
        finally:
            self._opening_file = False
        if self._pending_files:
            QTimer.singleShot(0, self._drain_pending_files)

    # How long the restore splash stays up, and therefore how long the
    # progress bar takes to travel 0 → 100 %.
    _RESTORE_SPLASH_MS = 3000

    def _build_window(self) -> None:
        """Decide whether this show gets a splash, then build.

        Completes ASYNCHRONOUSLY on the restore path — the window is built
        from the animation's finished signal, not before this returns.
        """
        # The first interactive show already sat behind app.run()'s startup
        # splash; a second one would just flash. Only re-opens get their own.
        if not self._first_show_done:
            self._first_show_done = True
            self._construct_window()
            return

        splash = self._make_restore_splash()
        if splash is None:
            self._construct_window()
            return

        from PySide6.QtCore import QPropertyAnimation, QEasingCurve
        # Linear, deliberately. The ask was a bar that travels smoothly to the
        # end in three seconds; any eased curve makes it visibly hurry or
        # dawdle somewhere in the middle.
        anim = QPropertyAnimation(splash.progress_bar(), b"value", splash)
        anim.setDuration(self._RESTORE_SPLASH_MS)
        anim.setStartValue(0)
        anim.setEndValue(100)
        anim.setEasingCurve(QEasingCurve.Type.Linear)
        anim.finished.connect(self._finish_restore)
        self._restore_anim = anim
        self._restore_splash = splash
        anim.start()

    def _make_restore_splash(self):
        """The startup splash, reused for a background-mode re-open.

        Imported lazily: ui.app imports THIS module, so a module-level import
        would be circular. Any failure here is non-fatal — we just fall back
        to opening the window with no splash at all.
        """
        try:
            from ui.app import LogicBoardSplash
            splash = LogicBoardSplash(self._app_icon)
            splash.set_progress(0)
            splash.show()
            splash.raise_()
            self._qapp.processEvents()
            return splash
        except Exception:
            log.exception("Restore splash failed to build (non-fatal)")
            return None

    def _finish_restore(self) -> None:
        """Animation done → build the window, then drop the splash.

        The splash is closed AFTER the window exists so there is never a
        frame with neither on screen. It sits at 100 % during the build,
        which is also the only stretch where the GUI thread is blocked —
        the three animated seconds run with the event loop free, which is
        what keeps the bar smooth.
        """
        self._restore_anim = None
        splash = self._restore_splash
        self._restore_splash = None
        try:
            self._construct_window()
        finally:
            # Nothing above show_ui() is holding a try/except for this path,
            # so if the build throws, the guard has to be released here or
            # every later show_ui() would be silently ignored and the app
            # would look dead to the tray icon forever.
            self._building_window = False
            if splash is not None:
                try:
                    splash.close()
                except Exception:
                    log.exception("Restore splash close failed (non-fatal)")

    def _construct_window(self) -> None:
        log.info("show_ui: building MainWindow")
        # Lazy import — keeps the background-mode startup path from
        # paying the cost of importing the whole canvas / engine modules.
        from ui.main_window import MainWindow
        window = MainWindow()
        # Wire the service references the way app.py used to.
        window.ls_service = self._ls_service
        window.ls_config = self._ls_config
        window.ls_activity = self._ls_activity
        window.ls_notifications = self._ls_notifications
        # Re-parent the notification manager onto the new window so its
        # toast widgets anchor to the right HWND.
        try:
            self._ls_notifications.setParent(window)
        except Exception:
            pass

        # Tell the window to call us back when the user clicks Close so we
        # can destroy it cleanly while keeping the process alive.
        try:
            window.set_close_handler(self._on_window_close_requested)
        except AttributeError:
            log.warning("MainWindow has no set_close_handler — close button "
                        "will leak the window into background mode")

        # LogicBoard ALWAYS opens on a clean board.
        #
        # It used to reopen whatever board was on screen when the window was
        # last put away, on the reasoning that closing to background mode is
        # not really closing and the work should come back with you. On a
        # shared classroom panel that is the wrong instinct: the next person
        # to walk up gets the previous teacher's lesson, and anything they
        # draw goes into somebody else's file.
        #
        # Nothing is lost by starting clean. ``hide_ui`` still auto-saves the
        # board on its way out and it is sitting at the top of the Files
        # panel, newest first, one tap away for whoever actually wants it.
        #
        # ``_last_board_path`` is still tracked and persisted — it is what
        # tells hide_ui where the board it saved ended up — it just no longer
        # decides what is on screen at startup.
        if self._last_board_path:
            log.info("Opening on a new board (last board was %s)",
                     self._last_board_path)

        window.showFullScreen()
        window.raise_()
        window.activateWindow()
        self._main_window = window
        # Build finished — release the re-entrancy guard show_ui() took. This
        # lives here rather than in show_ui() because the restore path returns
        # to the event loop before ever reaching this point.
        self._building_window = False
        if self._pending_files:
            # A file that arrived while the window was being built.
            QTimer.singleShot(0, self._drain_pending_files)

    def hide_ui(self) -> None:
        """Destroy the MainWindow but keep the process running.

        Auto-saves the board first (so reopen restores user work), records
        the path in settings, then deletes the window and runs a GC pass
        to release the C++ engine objects + caches.
        """
        if self._main_window is None:
            return
        win = self._main_window
        log.info("hide_ui: auto-saving + destroying MainWindow")

        # 1) Auto-save the current board. Best effort: if it fails, log
        #    and continue — better to lose the latest 30 seconds of work
        #    than to leak the whole MainWindow.
        path = self._save_current_board(win)
        if path is not None:
            self._last_board_path = path
            self._persist_last_board_path()

        # 2) Detach service refs so the window's destructor can't take
        #    them down with it.
        try:
            self._ls_notifications.setParent(None)
        except Exception:
            pass
        for attr in ("ls_service", "ls_config", "ls_activity",
                     "ls_notifications"):
            try:    setattr(win, attr, None)
            except Exception: pass

        # 2b) Close top-level toolbar pills FIRST. They're each their own
        #     Qt.Tool HWND on Windows; if we go straight to window
        #     deleteLater, the underlying HWNDs can outlive the Qt object
        #     graph briefly and "ghost" on the desktop for a frame or two.
        #     Calling .hide() explicitly tells DWM to unmap them cleanly
        #     before Qt tears the QObject side down. ToolbarStack.hide()
        #     iterates left/center/right groups.
        try:
            tb = getattr(win, "_toolbar", None)
            if tb is not None:
                tb.hide()
        except Exception:
            log.exception("toolbar.hide() in hide_ui")

        # 2c) Close any active panel (pen panel, eraser panel, file panel,
        #     etc) explicitly. These are tracked via _active_panel — a
        #     single slot in main_window. Floating dialogs that aren't in
        #     that slot leak silently and will pile up on every UI cycle
        #     until the process restarts.
        try:
            ap = getattr(win, "_active_panel", None)
            if ap is not None:
                ap.close()
        except Exception:
            log.exception("active_panel.close() in hide_ui")

        # 3) Destroy. ``deleteLater`` schedules the C++ deletion on the
        #    next event loop iteration — safer than `del` for QObjects.
        self._main_window = None
        try:    win.close()
        except Exception: pass
        try:    win.deleteLater()
        except Exception: pass

        # 4) Encourage Python's GC to release the engine + pixmap caches.
        import gc
        gc.collect()
        log.info("hide_ui: complete, process now in background mode")

    def _on_superseded(self, new_version: str) -> None:
        """A newer LogicBoard version has just launched and is asking
        us to step aside. Called from ``InstanceServer.supersede_requested``.

        We do a normal ``shutdown`` — the new instance will detect our
        server going away and become the primary on its retry. The
        difference from a user-initiated quit is purely cosmetic (this
        log line and any future "you were superseded" notification).
        """
        log.info("Superseded by newer version v%s — shutting down.",
                  new_version)
        self.shutdown()

    def _quit_from_tray(self) -> bool:
        """Tray "Quit completely", with the same unsaved-work guard as Exit.

        ``shutdown`` itself is deliberately left silent: it is also the path
        an auto-update takes when a newer instance supersedes this one, and
        a modal asking about unsaved changes has nobody in front of it there.
        The prompt belongs on the routes a PERSON takes, which is this one and
        MainWindow.closeEvent.
        """
        win = self._main_window
        if win is not None:
            try:
                # confirm_close, not _confirm_discard: this asks the question
                # AND records that it has been answered, so the win.close()
                # inside hide_ui below does not ask it a second time.
                if not win.confirm_close():
                    return False        # Cancel — stay running
            except Exception:
                log.exception("discard prompt on tray quit failed; "
                              "quitting rather than trapping the user")
        self.shutdown()
        return True

    def shutdown(self) -> None:
        """Full graceful shutdown — destroy UI if any, stop the service,
        quit the QApplication.

        Sequencing matters here. ``hide_ui`` schedules ``deleteLater``
        on the MainWindow; if we call ``app.quit()`` immediately, the
        deferred deletion may race with the event-loop teardown. The
        race usually resolves cleanly (Qt processes pending deletions on
        quit) but on Windows we've seen rare HWND-leak warnings in the
        log. The safe fix is to drain the event queue ONCE between
        hide_ui and quit, giving deleteLater + tray.hide + IPC stop time
        to commit before the loop exits.
        """
        log.info("shutdown: full quit requested")
        if self._main_window is not None:
            self.hide_ui()
        try:
            self._ipc.stop()
        except Exception:
            pass
        try:
            self._ls_service.stop()
            log.info("LogicShare service stopped cleanly.")
        except Exception:
            log.exception("Error stopping LogicShare service.")
        if self._tray is not None:
            self._tray.hide()
        # Nothing of ours may outlive the app. A rendering PowerPoint is a
        # separate OS process that Quit() only ASKS to close, so sweep any we
        # started — with a short grace, since the user is already leaving.
        # (ppt_renderer also registers this at interpreter exit; belt and
        # braces, because a tray-quit path may never reach atexit cleanly.)
        try:
            from ui.util.ppt_renderer import close_all_powerpoint_instances
            closed = close_all_powerpoint_instances(grace_ms=1500)
            if closed:
                log.info("Closed %d PowerPoint instance(s) on shutdown.", closed)
        except Exception:
            log.exception("PowerPoint cleanup on shutdown failed.")
        # Drain one event-loop pass so pending deleteLater() finalisations
        # run before quit() tears the loop down. processEvents is a noop
        # if nothing's pending, so calling it unconditionally is fine.
        try:
            self._qapp.processEvents()
        except Exception:
            pass
        # Defer the actual quit by one event loop tick so any pending
        # deleteLater()s queued by the processEvents pass also flush.
        QTimer.singleShot(0, self._qapp.quit)

    # ──────────────────────────────────────────────────────────────────────
    # Internal
    # ──────────────────────────────────────────────────────────────────────

    def _on_window_close_requested(self) -> bool:
        """Hooked by MainWindow's closeEvent — returning True means "the
        caller should NOT let Qt destroy the window; ApplicationCore will
        handle it". This is how we intercept Close to go into background
        mode instead of terminating the process.
        """
        # Schedule on next event tick so the closeEvent finishes returning
        # to Qt before we tear the window down (avoids re-entrancy on the
        # widget's own destructor).
        QTimer.singleShot(0, self.hide_ui)
        return True   # tell caller "we'll handle it"

    def _raise_existing_window(self) -> None:
        """Bring the existing MainWindow back to the front — always in
        fullscreen, never with a title bar.

        THE THIRD-LAUNCH BUG this used to have:
        The old code did ``if win.isMinimized(): win.showNormal()``. On
        Windows, ``showNormal()`` clears BOTH the Minimized flag AND the
        FullScreen flag — so a fullscreen window that had been minimised
        would restore into windowed mode WITH A TITLE BAR, which is not a
        state LogicBoard is ever supposed to be in. The user saw this on
        the third click of the .exe (first launch = fullscreen; user
        minimises; user clicks .exe again → IPC ping → show_ui → this
        method → showNormal → title-bar-mode).

        The fix is to route through ``showFullScreen()`` (or the state-flag
        equivalent) so the window always comes back the way it was
        originally shown. If the window was somehow left in a non-
        fullscreen state (bugs elsewhere, external tools, alt-tab weirdness
        on DPI change), this ALSO forces it back to fullscreen — which is
        the correct behaviour for LogicBoard since it's the only mode the
        UI is designed for (frameless HWND, no min/max/close in Qt's own
        titlebar).
        """
        win = self._main_window
        if win is None:
            return
        try:
            # setWindowState with a single flag flip is atomic on Windows —
            # it goes straight from Minimized to FullScreen without briefly
            # visiting the intermediate Normal state, which is what would
            # otherwise flash a title bar for one paint. Use the setter
            # rather than the show*() methods so we don't hit the
            # showNormal-clears-fullscreen trap.
            from PySide6.QtCore import Qt as _Qt
            win.setWindowState(_Qt.WindowFullScreen)
            # Defensive belt-and-braces: if the window WAS hidden (rare —
            # this method shouldn't be called then, since show_ui checks
            # _main_window is not None BEFORE calling here — but there's a
            # brief window where _main_window is set but the widget got
            # hide()d by external code), make sure it's actually visible.
            if not win.isVisible():
                win.showFullScreen()
            win.raise_()
            win.activateWindow()
        except Exception as e:
            log.warning("raise existing window failed: %s", e)

    def _save_current_board(self, win) -> Optional[Path]:
        """Auto-save the board open in ``win`` and return its on-disk path."""
        try:
            if hasattr(win, "_autosave_for_background"):
                return win._autosave_for_background()
        except Exception as e:
            log.warning("autosave on background failed: %s", e)
        return None

    # ─── Last-board persistence ──────────────────────────────────────────
    def _load_last_board_path(self) -> Optional[Path]:
        try:
            if not self._settings_file.exists():
                return None
            from ui.logicshare.config import read_settings
            p = read_settings(self._settings_file).get("last_board_path")
            return Path(p) if p else None
        except Exception:
            return None

    def _persist_last_board_path(self) -> None:
        """Remember which board was open, without endangering the rest of
        the file.

        settings.json is SHARED — LogicShare keeps its whole configuration
        in it, under its own key. This used to read the file, add one
        string, and write the lot back with ``Path.write_text``, which
        truncates before it writes. A failure anywhere in between left the
        file empty, and ``read_settings`` reports a corrupt settings file as
        an empty one, so the next launch came up as though LogicShare had
        never been set up: no classroom name, no password, no port.

        Reproduced by failing the write mid-call — 4 KB of configuration
        became 0 bytes, to persist a convenience field nobody would miss.

        ``write_settings_merged`` is the shared writer that already existed
        for exactly this: it merges one key and swaps the file in with an
        atomic rename, so a reader sees the whole old file or the whole new
        one. It is now the only way this file gets written.
        """
        try:
            from ui.logicshare.config import (write_settings_merged,
                                              delete_settings_key)
            if self._last_board_path is not None:
                ok = write_settings_merged(self._settings_file,
                                           "last_board_path",
                                           str(self._last_board_path))
            else:
                ok = delete_settings_key(self._settings_file,
                                         "last_board_path")
            if not ok:
                log.warning("persist last_board_path failed (see above)")
        except Exception as e:
            log.warning("persist last_board_path failed: %s", e)

    # ─── System tray ─────────────────────────────────────────────────────
    def _init_tray(self) -> None:
        if not QSystemTrayIcon.isSystemTrayAvailable():
            log.info("No system tray available — UI re-open only via shortcut")
            return
        tray = QSystemTrayIcon(self._app_icon, self)
        tray.setToolTip("LogicBoard — receiver running")
        menu = QMenu()
        open_act = QAction("Open LogicBoard", menu)
        open_act.triggered.connect(self.show_ui)
        menu.addAction(open_act)
        menu.addSeparator()
        quit_act = QAction("Quit completely", menu)
        # Through _quit_from_tray, not straight to shutdown: the tray is the
        # one exit route that never passes through MainWindow.closeEvent, so
        # it is the one that would still take unsaved work down with it.
        quit_act.triggered.connect(self._quit_from_tray)
        menu.addAction(quit_act)
        tray.setContextMenu(menu)
        # Left-click also opens the UI — standard Windows tray pattern.
        tray.activated.connect(self._on_tray_activated)
        tray.show()
        self._tray = tray

    def _on_tray_activated(self, reason) -> None:
        if reason == QSystemTrayIcon.Trigger:
            self.show_ui()