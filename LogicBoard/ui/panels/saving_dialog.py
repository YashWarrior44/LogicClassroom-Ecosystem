"""Save-in-progress modal for LogicBoard.

Shows a small, centred, frameless aero dialog while the heavy
``save_workdir_to_lb`` call (DEFLATE-compressed zip of board.json plus all
media caches) runs on a background QThread. The UI thread stays fully
responsive throughout — no more multi-second freeze on save.

Visual style matches ``ui/util/import_progress.py``:
* Aero backdrop with frosted card
* DWM rounded corners + drop shadow
* Centred over the parent window
* Brief fade-in
* Spinning indeterminate loader instead of a progress bar (we can't
  meaningfully report % through the C++ engine's save_to_file + Python's
  zip write — pretending we can would just lie to the user).

Usage:

    ok = SavingDialog.run_save(parent=main_window,
                               workdir=wd,
                               lb_path=lb_path,
                               board_name="My Board")
    if ok:
        # save completed; UI is responsive again
"""
from __future__ import annotations

import logging
import traceback
from pathlib import Path
from typing import Optional

from PySide6.QtCore import (
    Qt, QThread, Signal, QPropertyAnimation, QEasingCurve, QSize, QTimer,
)
from PySide6.QtGui import QFont, QPainter, QPen, QColor
from PySide6.QtWidgets import (
    QDialog, QWidget, QVBoxLayout, QHBoxLayout, QLabel, QLayout, QSizePolicy,
    QApplication,
)

from ui.util.size_manager import size_mgr as S
from ui.theme.theme_manager import theme_mgr
from ui.util.lb_format import save_workdir_to_lb
from ui.util.version import APP_VERSION

# Aero glass helpers — same defensive imports as import_progress so
# this module also works on non-Windows / pywinstyles-less environments.
try:
    from ui.util.aero import (
        apply_aero, prepare_aero_widget,
        enable_shadow, apply_rounded_corners,
    )
except Exception:                                       # pragma: no cover
    def apply_aero(*a, **k): ...
    def prepare_aero_widget(*a, **k): ...
    def enable_shadow(*a, **k): ...
    def apply_rounded_corners(*a, **k): ...

log = logging.getLogger(__name__)

__all__ = ["SavingDialog", "SaveWorker"]


# ════════════════════════════════════════════════════════════════════════════
# Worker thread
# ════════════════════════════════════════════════════════════════════════════

class SaveWorker(QThread):
    """Runs ``save_workdir_to_lb`` off the UI thread.

    Signals:
        finished_ok()                 — save completed successfully
        failed(message, traceback)    — save threw; UI shows the message

    Architecture note: the save is mostly disk I/O + DEFLATE compression
    (Python zipfile). Python's GIL releases for I/O, so the UI thread paints
    smoothly while the worker writes. The C++ engine's ``board.save_to_file``
    runs first (typically <100ms — just board.json) — that's the only piece
    that briefly holds the GIL via pybind11; not enough to be noticeable.
    """

    finished_ok = Signal()
    failed      = Signal(str, str)

    def __init__(self,
                 workdir: Path,
                 lb_path: Path,
                 board_name: str,
                 logicboard_version: str = APP_VERSION,
                 parent=None):
        super().__init__(parent)
        self._workdir = Path(workdir)
        self._lb_path = Path(lb_path)
        self._board_name = board_name
        self._lb_version = logicboard_version

    def run(self) -> None:
        try:
            save_workdir_to_lb(
                self._workdir,
                self._lb_path,
                board_name=self._board_name,
                logicboard_version=self._lb_version,
            )
            self.finished_ok.emit()
        except Exception as e:
            log.exception("SaveWorker failed: %s", e)
            self.failed.emit(str(e), traceback.format_exc())


# ════════════════════════════════════════════════════════════════════════════
# Spinning loader — a custom QWidget animation
# ════════════════════════════════════════════════════════════════════════════

class _Spinner(QWidget):
    """Twelve-segment rotating loader. Lightweight: one QTimer + one
    QPainter pass per ~80ms tick (~12 fps), each pass draws 12 tiny lines.
    """

    def __init__(self, parent=None, dot_count: int = 12,
                 diameter_px: int = 36, line_width_px: int = 3):
        super().__init__(parent)
        self._n         = dot_count
        self._diameter  = diameter_px
        self._line_w    = line_width_px
        self._phase     = 0
        self.setFixedSize(diameter_px + 6, diameter_px + 6)
        self.setAttribute(Qt.WA_TranslucentBackground)
        # ~12 fps is enough for a "smooth" spinner without burning CPU during
        # a save — the user's eye reads continuous rotation at 8+ fps.
        self._timer = QTimer(self)
        self._timer.timeout.connect(self._tick)
        self._timer.start(80)

    def _tick(self):
        self._phase = (self._phase + 1) % self._n
        self.update()

    def stop(self):
        self._timer.stop()

    def paintEvent(self, _):
        p = QPainter(self)
        p.setRenderHint(QPainter.Antialiasing, True)
        w = self.width(); h = self.height()
        cx, cy = w / 2.0, h / 2.0
        r_outer = self._diameter / 2.0
        r_inner = r_outer * 0.55
        import math
        accent = theme_mgr.qcolor(theme_mgr.tokens.accent)
        for i in range(self._n):
            # Fade from full (just-passed phase) down to 25% (oldest)
            age = (i - self._phase) % self._n
            t = age / max(1, self._n - 1)
            alpha = int(255 * (1.0 - 0.7 * t))
            angle = (2 * math.pi * i) / self._n - math.pi / 2.0
            x1 = cx + math.cos(angle) * r_inner
            y1 = cy + math.sin(angle) * r_inner
            x2 = cx + math.cos(angle) * r_outer
            y2 = cy + math.sin(angle) * r_outer
            # The accent, not a hardcoded blue. This spinner used to be
            # rgb(0,122,255) — iOS system blue — while the rest of the app
            # is rgb(76,194,255). Side by side with the progress bar during
            # a PDF import, that was two different blues on two surfaces
            # doing the same job.
            c = QColor(accent)
            c.setAlpha(alpha)
            pen = QPen(c, self._line_w, Qt.SolidLine, Qt.RoundCap)
            p.setPen(pen)
            p.drawLine(int(x1), int(y1), int(x2), int(y2))
        p.end()


# ════════════════════════════════════════════════════════════════════════════
# SavingDialog
# ════════════════════════════════════════════════════════════════════════════

class SavingDialog(QDialog):
    """Small frameless modal shown during board save.

    Design constraints (from the user):
    * Aero background (Mica where supported)
    * Same rounded corners + drop shadow as the project's other modals
    * Same fonts + colours as ``ImportProgressDialog``
    * Small, centred, no controls
    * Spinning loader + "Saving Board…" title + "Please wait…" subtitle
    * Cannot be closed by the user (close button + Esc both ignored)
    """

    def __init__(self, parent=None,
                 title: str = "Saving Board…",
                 subtitle: str = "Please wait…"):
        super().__init__(
            parent,
            Qt.FramelessWindowHint | Qt.Dialog | Qt.WindowStaysOnTopHint,
        )
        self.setAttribute(Qt.WA_TranslucentBackground)
        self.setModal(True)
        prepare_aero_widget(self)

        # Zero outer margin so the aero glass covers the whole window.
        outer = QVBoxLayout(self)
        outer.setContentsMargins(0, 0, 0, 0)
        outer.setSpacing(0)

        # Card holder — frosted-glass panel, matches ImportProgressCard look.
        self._card = QWidget(self)
        self._card.setObjectName("SavingCard")
        outer.addWidget(self._card)

        v = QVBoxLayout(self._card)
        v.setContentsMargins(S.scale_px(32), S.scale_px(26),
                             S.scale_px(32), S.scale_px(24))
        v.setSpacing(S.scale_px(14))

        # ── Spinner + title row ─────────────────────────────────────
        row = QHBoxLayout()
        row.setSpacing(S.scale_px(14))
        self._spinner = _Spinner(self._card,
                                  diameter_px=S.scale_px(34),
                                  line_width_px=max(2, S.scale_px(3)))
        row.addWidget(self._spinner, alignment=Qt.AlignVCenter)

        col = QVBoxLayout(); col.setSpacing(S.scale_px(4))
        # Same tokens as the import-progress dialog, which is the surface
        # this one is a sibling of. Both used to carry a private grey ladder
        # and a point-sized title — point sizes resolve against the screen's
        # logical DPI and so ignore UiScale, which is why these titles drifted
        # out of step with the rest of the app.
        t = theme_mgr.tokens

        self._title_label = QLabel(title)
        self._title_label.setStyleSheet(
            f"color: {t.text_primary}; background: transparent;"
            f"font-size: {S.scale_px(t.font_subtitle)}px;"
            f"font-weight: {t.weight_bold};"
        )
        col.addWidget(self._title_label)

        self._subtitle_label = QLabel(subtitle)
        self._subtitle_label.setStyleSheet(
            f"color: {t.text_secondary}; background: transparent;"
            f"font-size: {S.scale_px(t.font_caption)}px;"
        )
        col.addWidget(self._subtitle_label)
        row.addLayout(col, 1)
        v.addLayout(row)

        # Card sizing — small, comfortable, fixed (a save dialog shouldn't
        # resize to its contents mid-save).
        self._card.setFixedWidth(S.scale_px(340))
        self._card.setMinimumHeight(S.scale_px(96))
        outer.setSizeConstraint(QLayout.SizeConstraint.SetFixedSize)

        # Frosted card QSS — no border (DWM rounding is the only edge).
        # ``aero_tint_strong``, the dialog-weight tint, rather than a private
        # rgba(30,33,40,0.62) slab. At 62% this card was dense enough to hide
        # the DWM blur almost completely, so a save dialog and an import
        # dialog appearing seconds apart in the same workflow did not look
        # like the same kind of thing.
        self._card.setStyleSheet(f"""
            QWidget#SavingCard {{
                background: {t.aero_tint_strong};
                border: none;
                border-radius: 0px;
            }}
        """)

    # ── Block user-driven close attempts ─────────────────────────────────
    # The save is short and atomic — letting the user close mid-save would
    # leave the ``.lb.tmp`` orphan and the engine in a half-saved state.

    def closeEvent(self, e):
        e.ignore()

    def keyPressEvent(self, e):
        # Swallow Escape — the dialog auto-closes when the worker emits.
        if e.key() == Qt.Key_Escape:
            e.ignore()
            return
        super().keyPressEvent(e)

    # ── Aero / glass / centring ──────────────────────────────────────────
    def _apply_glass(self):
        try: apply_aero(self)
        except Exception as e: log.debug("apply_aero: %s", e)
        # ``mode="square"`` — this dialog's card draws its own QSS
        # border-radius. Leaving DWM at its default "round" rounds the
        # raw HWND at a fixed ~8px system radius that doesn't match the
        # card's radius, producing a faint double-curve artifact at each
        # corner. Square HWND + one CSS-radius card = a single curve.
        try: apply_rounded_corners(self, mode="round")
        except Exception as e: log.debug("apply_rounded_corners: %s", e)
        try: enable_shadow(self)
        except Exception as e: log.debug("enable_shadow: %s", e)

    def showEvent(self, event):
        super().showEvent(event)
        parent = self.parent()
        if isinstance(parent, QWidget):
            geo  = self.frameGeometry()
            pgeo = parent.frameGeometry()
            geo.moveCenter(pgeo.center())
            self.move(geo.topLeft())
        # Brief fade-in then apply the glass — same pattern as the import
        # dialog (animating windowOpacity makes the window layered, which
        # fights Win32 aero composition, so we apply aero *after*).
        self.setWindowOpacity(0.0)
        self._fade = QPropertyAnimation(self, b"windowOpacity")
        self._fade.setDuration(140)
        self._fade.setStartValue(0.0)
        self._fade.setEndValue(1.0)
        self._fade.setEasingCurve(QEasingCurve.OutCubic)
        self._fade.finished.connect(self._apply_glass)
        self._fade.start()

    # ── Public driver — show + spawn worker + spin event loop ────────────

    @classmethod
    def run_save(cls, parent, workdir: Path, lb_path: Path,
                 board_name: str,
                 logicboard_version: str = APP_VERSION) -> tuple[bool, str]:
        """Show the dialog, run the save off-thread, return when done.

        Returns ``(ok, error_message)``. ``ok`` is True on successful save;
        ``error_message`` is empty on success or carries the exception
        text on failure (caller can show its own AeroAlert with it).
        """
        dlg = cls(parent)
        worker = SaveWorker(workdir, lb_path, board_name,
                            logicboard_version=logicboard_version,
                            parent=dlg)
        result = {"ok": False, "err": ""}

        def _ok():
            result["ok"] = True
            dlg.accept()

        def _fail(msg, _tb):
            result["err"] = msg
            dlg.accept()

        worker.finished_ok.connect(_ok)
        worker.failed.connect(_fail)
        # Start the worker just before showing so the spinner starts
        # animating immediately — there's no perceptible gap between
        # "user clicked Save" and "dialog visible + work running".
        worker.start()
        # exec() blocks the UI thread on the local event loop only — the
        # save itself runs on the worker QThread, so the main window stays
        # responsive (paint events flow normally, spinner animates).
        dlg.exec()
        # Make sure the thread is fully done before we return.
        worker.wait()
        # Stop the spinner timer so we don't leak a QTimer.
        dlg._spinner.stop()
        return result["ok"], result["err"]