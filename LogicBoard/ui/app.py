"""
app.py — LogicBoard application entry point.
"""

from __future__ import annotations

import sys
import logging
from pathlib import Path

from PySide6.QtWidgets import (
    QApplication, QSplashScreen, QWidget,
    QVBoxLayout, QHBoxLayout, QLabel, QProgressBar,
)
from PySide6.QtGui import QPixmap, QIcon
from ui.util.scaling import UiScale
from ui.util.size_manager import size_mgr as S
from PySide6.QtCore import Qt, QTimer, QSize

from ui.util.paths import setup_logging, find_app_icon_path, saves_dir
from ui.util.platform import (
    is_already_running,
    named_pipe_exists,
    show_already_running_dialog,
    set_windows_app_id,
    auto_create_shortcut_if_missing,
)
from ui.util.audio import click_sound
from ui.theme.theme_manager import theme_mgr
from ui.util.version import APP_VERSION, APP_NAME


# Splash logo edge length in design px (before UiScale). Kept as a constant so
# the label box, the requested pixmap size and the "LB" text fallback can never
# drift apart.
_LOGO_PX = 150


class LogicBoardSplash(QSplashScreen):
    """Frameless startup splash with prominent Win11 aero blur.

    Vertically-centered composition, per the design brief:

        ┌─────────────────────────────────────────┐
        │                                         │
        │              ┌────────┐                 │
        │              │  logo  │   (centered)    │
        │              │  96px  │                 │
        │              └────────┘                 │
        │                                         │
        │             LogicBoard                  │
        │                v3.0                     │
        │                                         │
        │   ▰▰▰▰▰▱▱▱▱▱▱▱▱▱▱▱▱▱▱▱▱▱▱▱▱▱▱▱▱▱▱▱▱   │
        │                                         │
        │          Free and open source           │
        └─────────────────────────────────────────┘

    Transparency: dialog_bg from the theme is ~78% opaque which almost
    completely hides the aero blur — the card ends up looking like a
    flat dark rectangle. Splash + wizard override that with a much lower
    alpha (~28%) so the DWM blur is genuinely the dominant visual
    behind the content, which is the "aero" look the design asks for.
    """

    # Card fill alpha as a fraction (0..1). 0.28 lets the aero blur
    # read through strongly while keeping text legible. Tuned against the
    # About-dialog reference: any lower and small text starts to bleed
    # into the desktop; any higher and the blur becomes decorative rather
    # than the main visual.
    _CARD_ALPHA = 0.28

    def __init__(self, icon: QIcon | None = None) -> None:
        # 480×360 — taller than before so the centered vertical stack has
        # room to breathe. Width kept modest so the card doesn't feel like
        # a full window; height increased to accommodate title + version +
        # progress + credit line without cramming.
        self._w, self._h = UiScale.px(480), UiScale.px(360)
        pixmap = QPixmap(self._w, self._h)
        pixmap.fill(Qt.transparent)
        super().__init__(pixmap)

        # A TOOL window, not a splash-screen window, and without the
        # frameless flag: Qt makes both of those LAYERED, and DWM gives a
        # layered window only Mica's flat fallback grey. Tool keeps it off
        # the taskbar; ui.util.mica removes the frame and puts real Mica
        # behind it — or, where Mica is unavailable, falls back to the
        # usual solid LogicBoard surface. See ui/util/mica.py.
        self.setWindowFlags(Qt.Tool | Qt.WindowStaysOnTopHint)
        from ui.util import mica
        mica.prepare(self)

        t = theme_mgr.tokens
        sc = UiScale.px

        # ─── Container — NO border, NO border-radius ────────────────────
        # Same pattern as every other LogicBoard dialog: ``background: none``
        # + ``border: none`` on the widget, DWM aero is the fill,
        # ``apply_rounded_corners(mode="round")`` in showEvent is the ONLY
        # rounding. No CSS border that would overlap at DWM-rounded corners.
        container = QWidget(self)
        container.setObjectName("SplashCard")
        container.setFixedSize(self._w, self._h)
        container.setStyleSheet("""
            QWidget#SplashCard {
                background: none;
                border: none;
            }
            QLabel {
                background: transparent;
            }
        """)

        root = QVBoxLayout(container)
        root.setContentsMargins(sc(28), sc(28), sc(28), sc(22))
        root.setSpacing(sc(0))
        root.setAlignment(Qt.AlignHCenter | Qt.AlignTop)

        # A little breathing room at the top so the logo isn't glued to the
        # top edge — visually centres the whole stack a touch below middle.
        root.addStretch(1)

        # ─── Centered logo ──────────────────────────────────────────────────
        # 150px, up from 96. At 96 the mark read as an afterthought against a
        # 480-wide card — it is the first thing the user sees on launch, so it
        # carries the composition. 150 still leaves ~29px of stretch above and
        # below the block, so nothing crowds.
        logo = QLabel()
        logo.setAlignment(Qt.AlignCenter)
        logo.setFixedSize(sc(_LOGO_PX), sc(_LOGO_PX))
        if icon and not icon.isNull():
            # Ask for PHYSICAL pixels and tag the result with the DPR, so the
            # first thing the user sees isn't a blurry upscale on a 4K board.
            #
            # But ask ONCE. ``QIcon.pixmap()`` already applies the device
            # pixel ratio in Qt 6, so multiplying the request by dpr scaled
            # it twice: a 137 px box asked for 170 physical and got 214 back
            # tagged dpr 1.25 — 171 LOGICAL px inside a 137 px QLabel. QLabel
            # does not scale a pixmap to fit, it centres and CROPS, which is
            # why the logo had its edges sliced off. Force the exact physical
            # size we want and tag the ratio once.
            edge = sc(_LOGO_PX)
            dpr = self.devicePixelRatioF()
            side = max(1, int(round(edge * dpr)))
            target = QSize(side, side)
            pm = icon.pixmap(target)
            if not pm.isNull():
                if pm.size() != target:
                    pm = pm.scaled(target, Qt.KeepAspectRatio,
                                    Qt.SmoothTransformation)
                pm.setDevicePixelRatio(dpr)
                logo.setPixmap(pm)
            else:
                logo.setText("LB")
                logo.setStyleSheet(
                    f"font-size: {sc(_LOGO_PX // 2)}px; font-weight: 700; "
                    f"color: {t.text_primary}; background: transparent;")
        else:
            logo.setText("LB")
            logo.setStyleSheet(
                f"font-size: {sc(_LOGO_PX // 2)}px; font-weight: 700; "
                f"color: {t.text_primary}; background: transparent;")
        root.addWidget(logo, 0, Qt.AlignHCenter)

        # ─── Title (below the logo) ─────────────────────────────────────────
        root.addSpacing(sc(14))
        title = QLabel("LogicBoard")
        title.setAlignment(Qt.AlignCenter)
        title.setStyleSheet(
            f"font-size: {sc(26)}px; font-weight: 700; "
            f"color: {t.text_primary}; background: transparent;")
        root.addWidget(title, 0, Qt.AlignHCenter)

        # ─── Version (below the title) ──────────────────────────────────────
        version = QLabel(f"v{APP_VERSION}")
        version.setAlignment(Qt.AlignCenter)
        version.setStyleSheet(
            f"font-size: {sc(12)}px; font-weight: 500; "
            f"color: {t.text_secondary}; background: transparent; "
            f"letter-spacing: 0.5px;")
        root.addWidget(version, 0, Qt.AlignHCenter)

        # Stretch pushes the progress + credit to the bottom half.
        root.addStretch(1)

        # ─── Progress bar (thin, accent-coloured) ───────────────────────────
        # No status label — the spec wants a clean composition with just
        # logo, title, version, progress, licence line. Text like "Building the
        # interface…" clutters the design.
        self._progress = QProgressBar()
        self._progress.setRange(0, 100)
        self._progress.setValue(0)
        self._progress.setFixedHeight(sc(4))
        self._progress.setFixedWidth(sc(320))
        self._progress.setTextVisible(False)
        self._progress.setStyleSheet(f"""
            QProgressBar {{
                background-color: {t.stroke_default};
                border: none;
                border-radius: {sc(2)}px;
            }}
            QProgressBar::chunk {{
                background-color: {t.accent_grad};
                border-radius: {sc(2)}px;
            }}
        """)
        root.addWidget(self._progress, 0, Qt.AlignHCenter)

        # ─── Licence line at the bottom ─────────────────────────────────────
        root.addSpacing(sc(14))
        credit = QLabel("Free and open source")
        credit.setAlignment(Qt.AlignCenter)
        credit.setStyleSheet(
            f"font-size: {sc(11)}px; font-weight: 500; "
            f"color: {t.text_secondary}; background: transparent; "
            f"letter-spacing: 0.4px;")
        root.addWidget(credit, 0, Qt.AlignHCenter)

        container.show()

        # ``showMessage`` is what QSplashScreen callers use to signal
        # progress — we keep it as a no-op sink so existing code paths
        # ({splash.showMessage(...)} in application_core) don't crash. The
        # visual progress comes purely from set_progress() below.
    def showMessage(self, *_a, **_kw):  # type: ignore[override]
        # Splash design is text-free — status messages are ignored.
        pass

    def progress_bar(self) -> QProgressBar:
        """The live QProgressBar, for callers that want to ANIMATE it rather
        than step it.

        ``set_progress`` calls ``processEvents`` on every write, which is
        right for the startup path (each call follows a long blocking step
        and needs to paint before the next one) but wrong for a property
        animation, which already ticks on the event loop — re-entering it
        from inside a frame is how you get repaint storms. The restore splash
        in ApplicationCore drives this object directly instead.
        """
        return self._progress

    def set_progress(self, value: int, message: str = "") -> None:
        """Set the progress bar 0..100.

        The ``message`` argument is accepted for backwards compatibility
        with the older splash API (callers in ``run()`` pass a message
        string alongside the percentage), but the new splash design is
        text-free — the message is silently ignored. The spec was for
        a clean, minimal composition with just logo/title/version/
        progress/credit, no status line.
        """
        try:
            self._progress.setValue(max(0, min(100, int(value))))
        except Exception:
            pass
        # Pump events so the paint happens before the next call — the
        # original API relied on this behaviour to make progress visible
        # during long startup steps.
        QApplication.processEvents()

    def set_status(self, text: str = "") -> None:
        """Backwards-compat no-op. Old splash showed an indeterminate bar
        while a status message ticked over; new splash keeps the
        determinate progress bar and doesn't render status text."""
        # Keep the progress bar in determinate mode — an indeterminate
        # bar would look inconsistent with the rest of the sequence.
        QApplication.processEvents()

    # ─── Aero lifecycle — the same three-step every LogicBoard dialog uses ─
    def showEvent(self, event):
        super().showEvent(event)
        # Applied AFTER show(): on Windows the HWND now exists and DWM can
        # attach the aero backdrop + drop shadow. On non-Windows both
        # calls are no-ops, so the splash stays functional but flat.
        try:
            from ui.util import mica
            mica.apply(self)
            if not getattr(self, "_lb_mica", False):
                from ui.util.aero import enable_shadow
                enable_shadow(self)
            # Mica exists only while the window is ACTIVE — inactive, the
            # splash shows its solid surface instead (see mica.py).
            self.raise_()
            self.activateWindow()
        except Exception:
            pass


def _global_stylesheet() -> str:
    # The tooltip is the one surface that appears over EVERY other one, so
    # it is the easiest place for a palette to give itself away. This rule
    # used to carry #1c1c24 / #ececf2 / #3d3d4c — three colours with a
    # violet cast, in an app whose greys are neutral. Pointing at a toolbar
    # button put a faintly purple card next to a faintly neutral one.
    #
    # Tooltips are opaque on purpose: they are small, they land on top of
    # arbitrary content, and glass would make them harder to read for no
    # gain, since there is nothing meaningful behind a 20px-tall card.
    from ui.theme.theme_manager import theme_mgr
    from ui.theme.fluent_styles import scrollbar
    t = theme_mgr.tokens
    # The scrollbar is app-wide too: a text box, a drop-down list or a list
    # widget that never called apply_modern_scrollbar() used to get the
    # native Windows bar, a different thing from every other one.
    return scrollbar(tk=t) + f"""
        * {{ font-family: "Segoe UI", "SF Pro Display", "Helvetica Neue", Arial, sans-serif; }}
        QToolTip {{
            background: {t.bg_window}; color: {t.text_primary};
            border: 1px solid {t.stroke_default};
            padding: {S.scale_px(t.space_xs)}px {S.scale_px(t.space_sm)}px;
            border-radius: {S.scale_px(t.radius_xs)}px;
            font-size: {S.scale_px(t.font_body)}px;
        }}
    """


def create_application(argv: list[str] | None = None) -> QApplication:
    if argv is None:
        argv = sys.argv
    QApplication.setHighDpiScaleFactorRoundingPolicy(
        Qt.HighDpiScaleFactorRoundingPolicy.PassThrough
    )
    app = QApplication(argv)
    app.setApplicationName(APP_NAME)
    app.setApplicationVersion(APP_VERSION)
    app.setOrganizationName("LogicBoard")
    # Measure the live display once the QApplication (and thus primaryScreen)
    # exists, so every widget built afterwards gets correct responsive sizes.
    from ui.util.size_manager import size_mgr
    size_mgr.refresh()
    try:
        import logging
        logging.getLogger("logicboard").info("Display sizing: %s", size_mgr.info())
    except Exception:
        pass
    icon_path = find_app_icon_path()
    if icon_path:
        app.setWindowIcon(QIcon(str(icon_path)))
    app.setStyleSheet(_global_stylesheet())
    return app


def _qt_msg_handler(mode, context, message):
    """Filter out the harmless 'setPointSize: Point size <= 0 (-1)' warning from Qt."""
    if "setPointSize" in message and "-1" in message:
        return  # silently drop this specific known-harmless warning
    # Otherwise forward to default behavior (print to stderr)
    import sys
    sys.stderr.write(f"{message}\n")


def _parse_cli(argv: list[str]) -> dict:
    """Tiny CLI parser. Returns a dict {'background': bool,
    'grant_firewall_rule': str | None}.

    We don't use argparse to keep the cost out of the cold-start path —
    Windows Startup launches go through this code before anything else.
    """
    grant_rule = None
    skip = set()
    if "--grant-firewall-access" in argv[1:]:
        i = argv.index("--grant-firewall-access")
        if i + 1 < len(argv):
            grant_rule = argv[i + 1]
            skip.add(i + 1)
    # The file Explorer hands over on a double-click or "Open with" — the
    # "%1" in the registered command. The first argument that is not a flag
    # (and not a flag's value) and names a file that exists.
    open_file = None
    for i, a in enumerate(argv[1:], start=1):
        if i in skip or a.startswith("-"):
            continue
        try:
            if Path(a).is_file():
                open_file = str(Path(a).resolve())
                break
        except OSError:
            continue
    return {
        "background": "--background" in argv[1:],
        "grant_firewall_rule": grant_rule,
        "open_file": open_file,
    }


def run() -> int:
    from PySide6.QtCore import qInstallMessageHandler
    qInstallMessageHandler(_qt_msg_handler)
    setup_logging()

    args = _parse_cli(sys.argv)

    # This is the elevated re-launch ui.util.firewall.request_access
    # performs — UAC has already approved US (not a bare terminal) by
    # the time we get here. Grant the rule and exit; there is no UI for
    # this invocation, so we skip straight past the single-instance
    # check and QApplication entirely.
    if args["grant_firewall_rule"] is not None:
        from ui.util import firewall as fw
        return fw.run_elevated_grant(args["grant_firewall_rule"])

    logging.info("Startup mode: %s",
                 "BACKGROUND (Windows Startup)" if args["background"]
                 else "NORMAL (user launch)")

    # ── Single-instance check ────────────────────────────────────────────
    # NEW MODEL: instead of "another instance is running → show error and
    # exit", we now talk to the already-running instance via QLocalSocket
    # and ask it to show its UI. Then THIS process exits cleanly.
    #
    # The named-mutex from util.platform.is_already_running is still used
    # as a fast path: it tells us in one ctypes call whether we're the
    # first instance, without waiting on socket connect timeouts.
    #
    # It is NOT trusted on its own any more. The mutex answers "did someone
    # else create this named object", which is one inference away from the
    # thing we actually care about; the pipe answers it directly, because
    # the pipe IS the running instance's front door. When the mutex was
    # silently failing (it asked for the Global namespace, which a normal
    # user may not create in — see platform._MUTEX_NAME) this check passed
    # on every launch, and the second instance got all the way to binding
    # its own pipe before anything noticed. That produced the
    # "InstanceServer.listen failed: Access is denied" error in the log,
    # two live instances sharing one saves folder, and two LogicShare
    # services broadcasting the same room on the network.
    from ui.util.single_instance import SERVER_NAME
    if is_already_running() or named_pipe_exists(SERVER_NAME):
        # Another LogicBoard is running — DELEGATE the show request.
        # We need a QApplication to use QLocalSocket, even briefly.
        from PySide6.QtWidgets import QApplication
        _ = QApplication(sys.argv)
        from ui.util.single_instance import send_to_running_instance
        if args["background"]:
            # Windows Startup fired a second time? Just exit silently —
            # there's no UI to show and the first instance is already
            # holding the service.
            logging.info("Second --background launch: exiting silently")
            return 0
        if args["open_file"]:
            # A double-clicked board / PDF / deck while LogicBoard is
            # already running: the running copy opens it, so the teacher
            # gets one LogicBoard with the file in it rather than a second.
            if send_to_running_instance("open", path=args["open_file"]):
                logging.info("Handed %s to the running instance — exiting",
                             args["open_file"])
                return 0
        elif send_to_running_instance("show"):
            logging.info("Asked running instance to show UI — exiting")
            return 0
        # Fall through if IPC failed. Show the legacy "already running"
        # dialog so the user has SOMETHING to look at.
        #
        # The common cause on Windows is an ELEVATED running instance: a
        # normal-privilege launch can see that its pipe exists (which is why
        # we got this far and did NOT start a duplicate) but Windows will not
        # let it connect across the integrity boundary. Nothing in the app
        # can bridge that — the fix is to run LogicBoard unelevated — so say
        # so in the log rather than leaving a bare "IPC failed".
        logging.info("Could not deliver 'show' to the running instance. If it "
                     "was started as administrator, an unelevated launch is "
                     "not allowed to talk to it; falling back to the dialog.")
        show_already_running_dialog()
        return 0

    # We're the first instance. Continue startup.
    set_windows_app_id()
    app = create_application()

    # ── Register with Windows (Start Menu, Apps & Features, shortcuts) ──
    # Idempotent + fingerprinted — no-op on subsequent launches with the
    # same exe path. This is what makes LogicBoard behave like a proper
    # installed application (Start Menu, Windows Search, "Installed Apps"
    # in Settings). Also runs on every update because the exe path
    # changes between versioned folders, invalidating the fingerprint.
    try:
        from ui.util import windows_registration
        exe = Path(sys.executable) if getattr(sys, "frozen", False) \
                else (Path(__file__).resolve().parent / "app.py")
        windows_registration.register(exe, APP_VERSION,
                                       icon_path=find_app_icon_path())
    except Exception:
        logging.exception("Windows registration failed (non-fatal)")

    # ── Register .lb file association with Windows ───────────────────────
    # Idempotent + fingerprinted: writes to HKCU only if the icon path or
    # launch command has changed since last run. No-op on non-Windows.
    # Runs BEFORE the splash so the very first launch registers the icon
    # while we're still doing other startup work — user never sees a
    # separate "registering…" step.
    try:
        from ui.util.file_association import register_file_association
        register_file_association(find_app_icon_path(), saves_dir())
    except Exception:
        # Registration is a "nice to have" — a failure here should never
        # prevent the app from starting. The user just doesn't get the
        # .lb icon in Explorer; everything else works.
        logging.exception("File association registration failed (non-fatal)")

    # In background mode, skip the splash entirely (the whole point is
    # invisibility — flashing a splash on Windows login would betray
    # the design).
    splash = None
    if not args["background"]:
        icon_path = find_app_icon_path()
        app_icon = QIcon(str(icon_path)) if icon_path else QIcon()
        splash = LogicBoardSplash(app_icon)
        splash.show()
        app.processEvents()
        splash.set_progress(10, "Loading audio… 🎵")
        click_sound.preload()
        app.processEvents()
        splash.set_progress(25, "Loading settings… ⚙️")
    else:
        # Background mode still needs the icon for the system tray.
        icon_path = find_app_icon_path()
        app_icon = QIcon(str(icon_path)) if icon_path else QIcon()

    settings_file = saves_dir() / "settings.json"
    theme_mgr.set_settings_path(settings_file)
    theme_mgr.load_preference()
    if splash: app.processEvents()

    # Sweep orphaned session working dirs (graceful exits clean themselves).
    try:
        from ui.util.lb_format import sweep_workdirs
        sweep_workdirs(saves_dir())
    except Exception:
        logging.exception("Workdir sweep failed (non-fatal).")

    # Sweep scratch space: empty the Temp dir, prune stale Cache entries,
    # and remove any stray "temps" folder next to the executable. Keeps
    # LogicBoard from piling up disk over time.
    try:
        from ui.util.paths import sweep_temp_dirs
        sweep_temp_dirs()
    except Exception:
        logging.exception("Temp sweep failed (non-fatal).")

    # ── LogicShare config + setup wizard ──────────────────────────────────
    # In background mode we SKIP the setup wizard — if the board isn't
    # configured yet, we just won't serve. The wizard runs on first
    # interactive launch (user-driven).
    #
    # NOTE: Path is intentionally NOT re-imported here. It's already
    # imported at module scope (top of file). A local `from pathlib
    # import Path` here would make Python treat `Path` as a local name
    # for this ENTIRE function — including the earlier `Path(...)` calls
    # in the Windows-registration block above, which execute before this
    # line but would then be reading an as-yet-unassigned local variable.
    # That's exactly the bug that caused
    # `UnboundLocalError: cannot access local variable 'Path'`.
    from ui.logicshare import LSConfig
    ls_config = LSConfig(settings_file)
    if not ls_config.is_configured():
        if args["background"]:
            logging.info("Background mode + unconfigured LogicShare — "
                         "service stays idle until user launches the UI")
        else:
            from ui.logicshare.setup_wizard import SetupWizard
            if splash: splash.hide()
            wizard = SetupWizard(ls_config)
            if wizard.exec() != wizard.DialogCode.Accepted:
                logging.info("LogicShare setup cancelled — exiting.")
                return 0
            if splash: splash.show(); app.processEvents()

    if splash:
        splash.set_progress(45, "Building the interface… 🛠️")
        app.processEvents()

    # ── LogicShare service + activity log + notifications ────────────────
    # These three are owned by ApplicationCore (long-lived). They DO NOT
    # depend on MainWindow — service.py + activity_log.py + notifications.py
    # are fully UI-clean (notifications creates its own toast widgets
    # parented to None until a MainWindow comes along to host them).
    ls_service = None
    ls_activity = None
    ls_notifications = None
    try:
        from ui.logicshare import LogicShareService, ActivityLog
        from ui.logicshare.notifications import NotificationManager
        ls_shared_dir = Path.home() / "Documents" / "Shared Files"
        # The staging dir lives INSIDE the shared folder, not beside the
        # saves dir, and that placement is a performance decision.
        #
        # Every upload lands in the temp dir and is then shutil.move()d into
        # Shared Files. Same volume, that is a rename: instant, no I/O. Across
        # volumes, shutil.move silently degrades to a full copy-then-delete —
        # so a board whose Documents folder is redirected to another drive or
        # a network share (both normal on managed school machines) paid to
        # write every received file TWICE, once to staging and once again to
        # its destination, while the sender sat waiting on the HTTP response.
        #
        # Putting staging inside the destination folder makes them the same
        # volume by construction, so the move can never quietly become a copy.
        # The leading dot keeps it out of the way, and the service already
        # sweeps this directory on startup.
        ls_temp_dir = ls_shared_dir / ".incoming"
        ls_activity = ActivityLog(saves_dir() / "ls.json")
        # Pass parent=None — the service lives at the ApplicationCore level,
        # not the MainWindow level (which may not exist yet in background).
        ls_service = LogicShareService(
            ls_config, ls_activity,
            shared_dir=ls_shared_dir, temp_dir=ls_temp_dir, parent=None,
            # Acknowledgment queue + teacher registry live beside the other
            # LogicShare state in saves_dir, NOT in the shared folder: they
            # are internal bookkeeping the teacher should never see, and
            # they must survive an app update.
            ack_dir=saves_dir(),
        )
        ls_notifications = NotificationManager(
            ls_service, shared_dir=ls_shared_dir, parent=None)
        if ls_config.is_configured():
            ls_service.start()
            logging.info("LogicShare background service started.")
        else:
            logging.info("LogicShare not started — not yet configured")
    except Exception:
        logging.exception("LogicShare service failed to start (continuing).")

    # ── ApplicationCore ──────────────────────────────────────────────────
    # The long-lived owner. Takes the service, exposes show_ui()/hide_ui()/
    # shutdown(), binds the named-pipe IPC, and creates the system tray.
    from ui.application_core import ApplicationCore
    core = ApplicationCore(
        qapp             = app,
        ls_config        = ls_config,
        ls_activity      = ls_activity,
        ls_service       = ls_service,
        ls_notifications = ls_notifications,
        app_icon         = app_icon,
        settings_file    = settings_file,
        saves_dir        = saves_dir(),
    )
    if not core.start_ipc_server():
        if core.ipc_peer_alive():
            # Two launches raced: the pipe was free when we checked at the
            # top of run() and someone else took it while we were starting
            # up. We are the loser — hand our "show" over and get out,
            # releasing the LogicShare service and the tray icon on the way
            # so the winner is the only LogicBoard on this machine.
            logging.info("Lost the single-instance race — delegating to the "
                         "running instance and exiting.")
            from ui.util.single_instance import send_to_running_instance
            if args["open_file"]:
                send_to_running_instance("open", path=args["open_file"])
            elif not args["background"]:
                send_to_running_instance("show")
            if splash:
                try:    splash.close()
                except Exception: pass
            core.shutdown()
            return 0
        logging.warning("IPC server failed to bind — second launches "
                        "will fall back to the legacy error dialog")

    if splash:
        splash.set_progress(65, "Starting the LogicBoard engine… ⚡")
        app.processEvents()
    try:
        import logicboard_engine as lbe  # noqa: F401
        logging.info("C++ engine module loaded successfully.")
    except ImportError:
        logging.warning("logicboard_engine not found — Python-only mode.")

    # ── UI vs background ─────────────────────────────────────────────────
    if args["background"]:
        # Windows Startup mode — DO NOT create the MainWindow. The process
        # sits on its event loop holding the service + tray icon. User can
        # bring up the UI later by relaunching LogicBoard.exe (which goes
        # through send_to_running_instance("show")) or clicking the tray.
        logging.info("Background mode: UI deferred. Awaiting user action.")

        # ── Background update check + Windows toast ──────────────────
        # Startup mode is where update prompts really pay off: LogicBoard
        # boots with Windows every morning, so if a new release ships
        # the teacher sees a toast the next time they turn the smart
        # board on — no manual "check for updates" click required. The
        # toast asks before downloading ("Download") and, once the
        # installer is on disk, before installing ("Install now"); the
        # flow below asks "install now?" itself after a fresh download.
        try:
            from ui.util.update_notifier import check_in_background
            from ui.panels.update_dialog import run_confirmed_update_flow

            def _on_update_confirmed(info, ready=False) -> None:
                # No main window in background mode, so the download
                # dialog is parented to None (top-level) and the
                # unsaved-board guard finds no board to ask about. Runs
                # synchronously in the main thread's event loop.
                run_confirmed_update_flow(None, info,
                                          install_confirmed=ready)

            # Held on ``core`` so its lifetime matches ApplicationCore's
            # — that way the toast library's COM apartment / callbacks
            # aren't torn down under the notifier's feet if the user
            # takes a minute to click through.
            core._update_notifier = check_in_background(
                core, _on_update_confirmed,
                tray_icon=getattr(core, "_tray", None),
            )
        except Exception:
            logging.exception(
                "Background update notifier failed to start (non-fatal)")
    else:
        if splash:
            splash.set_progress(95, "Almost there… 🚀")
            app.processEvents()
            splash.set_progress(100, "Everything's ready! 😎🔥")
            app.processEvents()
            QTimer.singleShot(400, splash.close)
        # ApplicationCore owns the window now — it'll restore the last
        # board automatically if there is one.
        core.show_ui()
        if args["open_file"]:
            # Launched BY a file (double-click / Open with). Queued rather
            # than opened here: the picker it may raise wants the event
            # loop running and the splash gone.
            core.open_file(args["open_file"])

    # Installers that already did their job. Twice: when this launch IS the
    # installer's relaunch, the installer may still be exiting and its file
    # locked for a moment, so the second pass catches it.
    try:
        from ui.util import updater as _updater
        QTimer.singleShot(15_000, _updater.prune_spent_installers)
        QTimer.singleShot(120_000, _updater.prune_spent_installers)
    except Exception:
        logging.debug("installer sweep not scheduled", exc_info=True)

    auto_create_shortcut_if_missing()
    logging.info("Application startup complete.")
    exit_code = app.exec()

    # On exit, ApplicationCore.shutdown was already called (it's the only
    # path to app.quit()); the service is stopped, the IPC server unbound,
    # any open window destroyed. Belt-and-suspenders below.
    try:
        if ls_service is not None:
            ls_service.stop()
    except Exception:
        pass
    return exit_code


if __name__ == "__main__":
    sys.exit(run())