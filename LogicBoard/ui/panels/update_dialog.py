"""
update_dialog.py — Aero-styled UI for the update flow.

The flow, end to end
====================

    check GitHub  ->  offer  ->  save the board?  ->  download  ->  hand off

Two dialogs do the visible work:

  1. ``UpdatePromptDialog``   — "LogicBoard X.Y.Z is available. Install now?"
                                 with the release notes. The user confirms.
  2. ``UpdateDownloadDialog`` — progress bar bound to
                                 ``updater.ThreadedDownload``. On success
                                 ``.installer_path`` holds the downloaded
                                 LogicBoardInstaller.exe.

Between the two sits the one guard that has to be here rather than in
``updater``: **the unsaved board**. The installer's first step is to
terminate every running LogicBoard.exe — it has to, because Windows won't
let it overwrite a mapped image — so anything the teacher hasn't saved by
the time we hand over is gone, with no dialog left on screen to ask about
it. So we ask BEFORE the hand-off, through the same Save / Don't Save /
Cancel prompt that closing the window uses, and Cancel aborts the whole
update rather than the prompt.

Once the installer is launched we quit. It does the rest — files,
shortcuts, Apps & Features, the Run entry — exactly as it does for a fresh
install. See ``ui/util/updater.py`` for why the update path is the
installer rather than a second implementation of it.

All dialogs follow the existing Aero design rules:
  * Root widget transparent.
  * Card is translucent (rgba fill), never opaque.
  * DWM aero applied via ``ui.util.aero.apply_aero``.
  * Rounded HWND corners via ``apply_rounded_corners`` — NO ``setMask``.
"""
from __future__ import annotations

import logging
import os
import sys
from pathlib import Path
from typing import Optional

from PySide6.QtCore import Qt
from PySide6.QtWidgets import (
    QDialog, QLabel, QProgressBar, QPushButton, QTextBrowser,
    QVBoxLayout, QHBoxLayout,
)

from ui.theme.theme_manager import theme_mgr
from ui.util.size_manager import size_mgr as S
from ui.util.version import APP_VERSION
from ui.util import updater

log = logging.getLogger(__name__)


# ============================================================================
# Shared helpers
# ============================================================================

def _install_aero(dlg: QDialog) -> None:
    """Frameless translucent dialog with DWM aero + native rounded
    corners + drop shadow.

    The HWND is rounded by DWM at its native ~8px radius — this is the
    ONLY rounding source. No inner QFrame "card" with its own CSS
    border-radius (that produced a double-curve "border inside a border"
    artifact), and NO CSS border on the dialog (Qt draws CSS borders as
    paint ops on the widget's rectangular geometry — at DWM-rounded
    corners those paint lines overlap each other and look ugly).
    """
    dlg.setWindowFlag(Qt.FramelessWindowHint, True)
    dlg.setAttribute(Qt.WA_TranslucentBackground, True)
    # The translucency above is exactly what turns the gaps between this
    # dialog's controls into click-through holes, so the catcher belongs
    # with it. These dialogs set the attribute themselves rather than going
    # through prepare_aero_widget, and so were the one family of aero
    # windows in the app with no backing surface at all: a tap in the
    # padding around the buttons landed on the canvas behind and drew on it.
    try:
        from ui.util.aero import install_hit_catcher
        install_hit_catcher(dlg)
    except Exception:
        pass
    try:
        from ui.util.aero import (apply_aero, enable_shadow,
                                       apply_rounded_corners)
        apply_aero(dlg)
        enable_shadow(dlg)
        apply_rounded_corners(dlg, mode="round")
    except Exception:
        pass


def _make_card(dlg: QDialog) -> QVBoxLayout:
    """Set up the dialog's content layout — lives directly on the dialog,
    no inner QFrame card. Returns the content layout."""
    dlg.setStyleSheet("QDialog { background: none; border: none; }")

    layout = QVBoxLayout(dlg)
    layout.setContentsMargins(
        S.scale_margin(32), S.scale_margin(28),
        S.scale_margin(32), S.scale_margin(28))
    layout.setSpacing(S.scale_spacing(16))
    return layout


def _title_label(text: str, *, muted: bool = False) -> QLabel:
    lbl = QLabel(text)
    lbl.setAlignment(Qt.AlignCenter)
    t = theme_mgr.tokens
    color = t.text_secondary if muted else t.text_primary
    lbl.setStyleSheet(
        f"font-size: {S.scale_font(18)}px; font-weight: 700; "
        f"color: {color}; background: transparent;")
    return lbl


def _body_label(text: str) -> QLabel:
    lbl = QLabel(text)
    lbl.setWordWrap(True)
    lbl.setAlignment(Qt.AlignCenter)
    t = theme_mgr.tokens
    lbl.setStyleSheet(
        f"font-size: {S.scale_font(12.5)}px; "
        f"color: {t.text_secondary}; background: transparent; "
        f"line-height: 140%;")
    return lbl


def _primary_button(text: str) -> QPushButton:
    b = QPushButton(text)
    b.setFixedHeight(S.scale_size(44))
    b.setCursor(Qt.PointingHandCursor)
    t = theme_mgr.tokens
    b.setStyleSheet(f"""
        QPushButton {{
            background: {t.accent_grad}; color: {t.text_on_accent};
            border: none; border-radius: {S.scale_radius(12)}px;
            font-size: {S.scale_font(13.5)}px; font-weight: 700;
            padding: 0 {S.scale_size(20)}px;
        }}
        QPushButton:hover {{ background: {t.accent_grad_hover}; }}
        QPushButton:disabled {{ background: {t.fill_disabled};
                                 color: {t.text_disabled}; }}
    """)
    return b


def _secondary_button(text: str) -> QPushButton:
    b = QPushButton(text)
    b.setFixedHeight(S.scale_size(44))
    b.setCursor(Qt.PointingHandCursor)
    t = theme_mgr.tokens
    b.setStyleSheet(f"""
        QPushButton {{
            background: transparent; color: {t.text_primary};
            border: 1px solid {t.stroke_default};
            border-radius: {S.scale_radius(12)}px;
            font-size: {S.scale_font(13.5)}px; font-weight: 600;
            padding: 0 {S.scale_size(20)}px;
        }}
        QPushButton:hover {{ background: {t.bg_card_hover}; }}
    """)
    return b


# ============================================================================
# 1. Check-in-progress dialog
# ============================================================================

class UpdateCheckDialog(QDialog):
    """Small "Checking for updates…" dialog. Runs a ``ThreadedCheck`` and
    forwards the result to the caller through the ``.result_info`` /
    ``.error_msg`` attributes once ``exec()`` returns.

    The caller decides what to do next:
      * ``result_info`` is not None AND
        ``updater.newer_than_current(result_info)`` — open
        ``UpdatePromptDialog``.
      * Otherwise — say "you're up to date".
    """

    def __init__(self, parent=None) -> None:
        super().__init__(parent)
        self.setFixedSize(S.scale_width(420), S.scale_height(220))
        _install_aero(self)

        layout = _make_card(self)
        layout.addStretch(1)
        layout.addWidget(_title_label("Checking for updates…"))
        layout.addWidget(_body_label(
            "Contacting GitHub for the latest release."))
        layout.addStretch(1)

        # Result plumbing.
        self.result_info: Optional[updater.UpdateInfo] = None
        self.error_msg: str = ""

        self._chk = updater.ThreadedCheck(parent=self)
        self._chk.finished.connect(self._on_finished)
        self._chk.start()

    def _on_finished(self, info, err) -> None:
        self.result_info = info
        self.error_msg = err
        # Accept regardless — the caller reads the two attributes to
        # decide follow-up UI. Rejecting instead would mask a real error.
        self.accept()


# ============================================================================
# 2. Prompt dialog — "install now?"
# ============================================================================

class UpdatePromptDialog(QDialog):
    """Question 1 — "a new version is available; download it?"

    Presents the new version + release notes. Accepts when the user chooses
    Download. Nothing is fetched before that: the updater used to download
    quietly first, spending the school's bandwidth on a decision nobody had
    made yet. (If the installer is ALREADY on disk from an earlier "Later",
    the flow skips this question and goes straight to UpdateReadyDialog.)
    """

    def __init__(self, info: updater.UpdateInfo, parent=None) -> None:
        super().__init__(parent)
        self._info = info
        self.setFixedSize(S.scale_width(560), S.scale_height(520))
        _install_aero(self)

        layout = _make_card(self)

        layout.addWidget(_title_label("Update available"))
        layout.addWidget(_body_label(
            f"LogicBoard {info.latest_version} is available. "
            f"You're running {APP_VERSION}. Download it now?"))

        # Release-notes panel — a translucent inner card with scrollable
        # markdown. Uses the same rgba pattern so Aero shows through.
        t = theme_mgr.tokens
        notes = QTextBrowser()
        notes.setOpenExternalLinks(True)
        notes.setStyleSheet(f"""
            QTextBrowser {{
                background: {t.fill_subtle};
                color: {t.text_primary};
                border: 1px solid {t.stroke_subtle};
                border-radius: {S.scale_radius(12)}px;
                padding: {S.scale_size(12)}px;
                font-size: {S.scale_font(t.font_body)}px;
            }}
        """)
        notes.setMarkdown(info.release_notes or "*No release notes provided.*")
        layout.addWidget(notes, 1)  # stretch — this is the tallest child

        layout.addWidget(_body_label(
            "Nothing is installed yet — once it has downloaded, you'll be "
            "asked whether to install it now or later."))

        # Buttons.
        row = QHBoxLayout()
        row.setSpacing(S.scale_spacing(12))
        later = _secondary_button("Later")
        later.clicked.connect(self.reject)
        go = _primary_button("Download")
        go.clicked.connect(self.accept)
        row.addWidget(later)
        row.addStretch(1)
        row.addWidget(go)
        layout.addLayout(row)


# ============================================================================
# 2b. Ready dialog — "downloaded; install now?"
# ============================================================================

class UpdateReadyDialog(QDialog):
    """Question 2 — "the update has downloaded; install it now?"

    Asked even though the user agreed to the download, because installing
    is a different decision: it closes LogicBoard, possibly in the middle of
    a lesson. Accepts on Install now. "Later" KEEPS the installer
    (``updater.prune`` spares a version newer than the one running), so the
    next offer of this version comes straight back to this question.
    """

    def __init__(self, info: updater.UpdateInfo, parent=None) -> None:
        super().__init__(parent)
        self.setFixedSize(S.scale_width(520), S.scale_height(300))
        _install_aero(self)
        layout = _make_card(self)
        layout.addWidget(_title_label("Update downloaded ✅"))
        layout.addWidget(_body_label(
            f"LogicBoard {info.latest_version} is ready to install. "
            f"Install it now?"))
        # Say this BEFORE it happens. LogicBoard closing itself mid-update
        # reads as a crash on a classroom board unless it was announced.
        note = ("LogicBoard will close while the installer runs, then open "
                "again on the new version. Choose Later to keep it for "
                "another time.")
        # The installer runs as the ordinary user and installs into the user's
        # own folder, so an update normally involves no consent prompt at all.
        # The one exception is a machine still carrying the old system-wide
        # install: moving THAT out of Program Files does need administrator
        # rights, once — and a UAC prompt nobody was told about is exactly the
        # thing that reads as something having gone wrong.
        if _installed_system_wide():
            note += ("\n🔒  This copy is installed in Program Files, so "
                     "Windows will ask for administrator permission once to "
                     "move it into your user folder. Later updates won't.")
        layout.addWidget(_body_label(note))
        layout.addStretch(1)
        row = QHBoxLayout()
        row.setSpacing(S.scale_spacing(12))
        later = _secondary_button("Later")
        later.clicked.connect(self.reject)
        install = _primary_button("Install now")
        install.clicked.connect(self.accept)
        row.addWidget(later)
        row.addStretch(1)
        row.addWidget(install)
        layout.addLayout(row)


# ============================================================================
# 3. Download-in-progress dialog
# ============================================================================

class UpdateDownloadDialog(QDialog):
    """Runs ``ThreadedDownload`` and shows a progress bar.

    On success the caller reads ``.installer_path``; on failure
    ``.error_msg``. Both empty means the user cancelled.
    """

    def __init__(self, info: updater.UpdateInfo, parent=None) -> None:
        super().__init__(parent)
        self.setFixedSize(S.scale_width(500), S.scale_height(260))
        _install_aero(self)

        layout = _make_card(self)
        layout.addStretch(1)
        layout.addWidget(_title_label("Downloading update…"))
        self._status = _body_label("Preparing…")
        layout.addWidget(self._status)

        t = theme_mgr.tokens
        self._bar = QProgressBar()
        self._bar.setRange(0, 100)
        self._bar.setValue(0)
        self._bar.setTextVisible(False)
        self._bar.setFixedHeight(S.scale_size(6))
        self._bar.setStyleSheet(f"""
            QProgressBar {{
                background-color: {t.stroke_default};
                border: none;
                border-radius: {S.scale_radius(3)}px;
            }}
            QProgressBar::chunk {{
                background-color: {t.accent_grad};
                border-radius: {S.scale_radius(3)}px;
            }}
        """)
        layout.addWidget(self._bar)

        layout.addStretch(1)

        # Cancel button. Not primary — downloading is the expected happy
        # path; cancel is an escape hatch.
        row = QHBoxLayout()
        row.addStretch(1)
        self._cancel_btn = _secondary_button("Cancel")
        self._cancel_btn.clicked.connect(self._on_cancel)
        row.addWidget(self._cancel_btn)
        layout.addLayout(row)

        # Result plumbing.
        self.installer_path: str = ""
        self.error_msg: str = ""

        self._dl = updater.ThreadedDownload(info, parent=self)
        self._dl.progress.connect(self._on_progress)
        self._dl.finished.connect(self._on_finished)
        self._dl.start()

    def _on_progress(self, msg: str, pct: int) -> None:
        self._status.setText(msg)
        # A zero percent from the worker means "no Content-Length" — show a
        # busy bar rather than a percentage that would be invented.
        self._bar.setRange(0, 100 if pct else 0)
        self._bar.setValue(pct)

    def _on_finished(self, installer_path: str, err: str) -> None:
        self.installer_path = installer_path
        self.error_msg = err
        self.accept()

    def _on_cancel(self) -> None:
        self._dl.cancel()
        # UI feedback so the user sees the click registered.
        self._cancel_btn.setEnabled(False)
        self._status.setText("Cancelling…")

    # Prevent closing via Esc / X while the download is running — same
    # rationale as the setup wizard. If the user really wants out, the
    # explicit Cancel button is right there.
    def keyPressEvent(self, e) -> None:
        if e.key() == Qt.Key_Escape:
            return
        super().keyPressEvent(e)


def _installed_system_wide() -> bool:
    """Is the running build installed under Program Files?

    Only affects what the update dialog PROMISES about administrator
    prompts, so it fails to False: over-promising "no prompt" and then
    showing one is a smaller surprise than warning about a prompt that never
    arrives.
    """
    if sys.platform != "win32":
        return False
    try:
        exe = Path(sys.executable).resolve()
        for var in ("ProgramFiles", "ProgramW6432", "ProgramFiles(x86)"):
            root = os.environ.get(var)
            if root and exe.is_relative_to(Path(root)):
                return True
    except Exception:
        pass
    return False


# ============================================================================
# The unsaved-board guard
# ============================================================================

def _find_main_window(parent) -> Optional[object]:
    """The MainWindow, if one is on screen.

    Duck-typed on ``_confirm_discard`` rather than imported: this module is
    reached FROM ``main_window`` (Settings lives inside it), and importing
    it back at module scope is a cycle.

    Returns None in background mode, where LogicBoard runs as a tray
    process with no window and therefore no board to lose.
    """
    from PySide6.QtWidgets import QApplication
    try:
        w = parent.window() if parent is not None else None
    except Exception:
        w = None
    if w is not None and hasattr(w, "_confirm_discard"):
        return w
    for tl in QApplication.topLevelWidgets():
        if hasattr(tl, "_confirm_discard") and hasattr(tl, "_canvas"):
            return tl
    return None


def _confirm_board_saved(parent) -> bool:
    """Ask about unsaved work before we hand the machine to the installer.

    Returns True to carry on with the update, False to abandon it.

    This is the same Save / Don't Save / Cancel prompt that closing the
    window raises, deliberately: the user already knows it, and "Save" there
    only reports success once the board is actually on disk (a cancelled
    Save-As answers False). Cancel abandons the UPDATE, not just the prompt
    — there is nothing else sensible for it to mean at this point.

    An exception from the prompt also abandons the update. That is the
    opposite of the choice ``ApplicationCore._quit_from_tray`` makes, and
    for the opposite reason: quitting on a broken prompt merely risks the
    board, but continuing here would have the installer terminate the
    process seconds later with the board unsaved and no way back. A missed
    update costs a retry.
    """
    win = _find_main_window(parent)
    if win is None:
        return True
    try:
        return bool(win._confirm_discard())
    except Exception:
        log.exception("Unsaved-changes prompt failed — abandoning the update "
                      "rather than risking an unsaved board")
        return False


# ============================================================================
# Flows
# ============================================================================

def run_update_flow(parent) -> None:
    """Blocking end-to-end flow: check → offer → save → download → hand off.

    Uses ``exec()`` calls chained via dialog return values, so this stays
    synchronous from the caller's perspective and needs no signal wiring in
    the parent. The actual work (network I/O) happens on the QThread workers
    wrapped by the dialogs — the UI thread never blocks.
    """
    if not updater.is_configured():
        _info_msg(parent, "Updates disabled",
                   "The updater isn't configured in this build.")
        return

    # Step 1 — check.
    check = UpdateCheckDialog(parent)
    check.exec()
    info = check.result_info
    err = check.error_msg
    if err:
        _info_msg(parent, "Update check failed", err)
        return
    if info is None or not updater.newer_than_current(info):
        _info_msg(parent, "Already up to date",
                   f"You're on the latest version ({APP_VERSION}).")
        return

    # Step 2 — offer the download. Skipped when the installer is already
    # on disk (an earlier "Later"): the question left is whether to install.
    if updater.existing_download(info) is None:
        prompt = UpdatePromptDialog(info, parent)
        if prompt.exec() != QDialog.Accepted:
            return  # user chose Later

    # Steps 3-6 — download, ask to install, save guard, hand off.
    run_confirmed_update_flow(parent, info)


def run_confirmed_update_flow(parent, info: "updater.UpdateInfo", *,
                              install_confirmed: bool = False) -> None:
    """The flow once the user has agreed to DOWNLOAD the update.

    Used after the offer dialog, the Settings page's Download button, or the
    Windows-toast action surfaced by ``update_notifier.UpdateNotifier``:

        download (unless already on disk)  ->  "install now?"  ->
        ask about the unsaved board  ->  launch  ->  quit

    The "install now?" question is skipped only when the user has ALREADY
    answered it — ``install_confirmed=True``, from an "Install now" button
    that was offered because the installer was known to be on disk.

    Safe to call from anywhere on the main thread, including with
    ``parent=None`` from a background-mode boot where no window exists.
    """
    if not updater.is_configured():
        # Shouldn't be reachable — the check that produced ``info`` would
        # have failed first. A silent no-op is right either way.
        return
    if not updater.newer_than_current(info):
        # No longer newer: the user may have updated by some other route
        # between the toast firing and this handler running.
        return

    # Step 1 — the installer: already on disk, or downloaded now (the user
    # has agreed to that much).
    ready = updater.existing_download(info)
    if ready is not None:
        installer_path = str(ready)
    else:
        dl = UpdateDownloadDialog(info, parent)
        dl.exec()
        if dl.error_msg:
            _info_msg(parent, "Update failed", dl.error_msg)
            return
        if not dl.installer_path:
            log.info("Update download cancelled by the user.")
            return
        installer_path = dl.installer_path
        install_confirmed = False      # a fresh download is always asked about

    # Step 2 — install now, or keep it for later?
    if not install_confirmed:
        if UpdateReadyDialog(info, parent).exec() != QDialog.Accepted:
            log.info("Update %s downloaded and kept for later.",
                     info.latest_version)
            return

    # Step 3 — the board, right before the installer starts killing
    # processes. Cancel here abandons the install, not the download.
    if not _confirm_board_saved(parent):
        log.info("Update abandoned at the unsaved-changes prompt.")
        return

    # Step 4 — hand off.
    _hand_off_to_installer(parent, installer_path)


def _hand_off_to_installer(parent, installer_path: str) -> None:
    """Launch the installer elevated and step aside.

    Quitting is cleaner than waiting to be terminated: the installer WILL
    kill this process (it cannot overwrite a mapped LogicBoard.exe
    otherwise), but going quietly lets Qt tear the windows down in order,
    flush the log, and stop the LogicShare service — and the user sees us
    make way for the installer rather than vanish under it.

    A declined UAC prompt is not a failure of the update so much as an
    answer to it: say so and stay running.
    """
    from PySide6.QtWidgets import QApplication
    ok, msg = updater.launch_installer(Path(installer_path))
    if not ok:
        _info_msg(parent, "Couldn't start the installer", msg)
        return
    log.info("Installer launched — exiting for the hand-off.")
    QApplication.quit()


def _info_msg(parent, title: str, body: str) -> None:
    """Minimal Aero-styled info dialog. Used for terminal messages
    where an interactive prompt would be overkill."""
    dlg = QDialog(parent)
    dlg.setFixedSize(S.scale_width(420), S.scale_height(220))
    _install_aero(dlg)
    layout = _make_card(dlg)
    layout.addStretch(1)
    layout.addWidget(_title_label(title))
    layout.addWidget(_body_label(body))
    layout.addStretch(1)
    row = QHBoxLayout()
    row.addStretch(1)
    ok = _primary_button("OK")
    ok.clicked.connect(dlg.accept)
    row.addWidget(ok)
    row.addStretch(1)
    layout.addLayout(row)
    dlg.exec()
