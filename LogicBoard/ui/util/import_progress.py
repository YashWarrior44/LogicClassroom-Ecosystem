"""
import_progress.py — Frameless modal progress dialog + background workers
                     for media imports (PPT + PDF).

PowerPoint COM and PyMuPDF rendering both block the calling thread for many
seconds (even minutes for large decks). Doing that on the GUI thread freezes
the app. This module wraps each render path in a QThread, exposes progress
signals, and shows a small frameless dialog with a progress bar.

Usage from main_window:

    from ui.util.import_progress import run_ppt_import_with_progress, \
                                        run_pdf_import_with_progress

    item = run_ppt_import_with_progress(parent_window, library, source_path)
    if item is not None:
        # … add to media list, refresh UI …

The helper returns the new MediaItem on success or None if the user
cancelled / it failed.
"""
from __future__ import annotations

import logging
import traceback
from pathlib import Path
from typing import Optional

from PySide6.QtCore    import (Qt, QThread, Signal, QTimer, QSize,
                                QEasingCurve, QPropertyAnimation)
from PySide6.QtGui     import QColor, QPainter, QPainterPath, QPen, QFont
from ui.util.size_manager import size_mgr as S
from ui.theme.theme_manager import theme_mgr
from PySide6.QtWidgets import (QDialog, QVBoxLayout, QHBoxLayout, QLabel,
                                QPushButton, QProgressBar, QGraphicsDropShadowEffect,
                                QApplication, QWidget, QLayout, QSizePolicy)

log = logging.getLogger(__name__)

# Aero glass helpers (no-op on non-Windows; safe to import anywhere).
try:
    from ui.util.aero import (apply_aero, prepare_aero_widget,
                                  enable_shadow, apply_rounded_corners)
except Exception:                                   # pragma: no cover
    def apply_aero(*a, **k): ...
    def prepare_aero_widget(*a, **k): ...
    def enable_shadow(*a, **k): ...
    def apply_rounded_corners(*a, **k): ...

__all__ = [
    "ImportProgressDialog",
    "PptImportWorker", "PdfImportWorker",
    "run_ppt_import_with_progress",
    "run_pdf_import_with_progress",
]


# ════════════════════════════════════════════════════════════════════════════
# Worker threads
# ════════════════════════════════════════════════════════════════════════════

class _BaseImportWorker(QThread):
    """Common base for PPT/PDF import workers.

    Signals:
        progress(message, fraction)  — 0.0 to 1.0
        finished_ok(item_id)         — the MediaLibrary item id on success
        failed(message)              — error message on failure
    """
    progress    = Signal(str, float)
    finished_ok = Signal(str)
    failed      = Signal(str)

    def __init__(self, library, source: Path, parent=None):
        super().__init__(parent)
        self._library = library
        self._source  = Path(source)
        self._cancel  = False

    def request_cancel(self) -> None:
        self._cancel = True

    @property
    def cancelled(self) -> bool:
        return self._cancel


class PptImportWorker(_BaseImportWorker):
    """Runs the full PPT import (copy original → render WEBP cache via
    PowerPoint COM)."""

    def __init__(self, library, source: Path, parent=None,
                 with_animations: bool = False):
        super().__init__(library, source, parent)
        self._with_animations = bool(with_animations)

    def run(self) -> None:
        try:
            def cb(msg: str, frac: float) -> None:
                # Forward to GUI thread via signal (Qt auto-marshals).
                self.progress.emit(msg, frac)
                if self._cancel:
                    raise RuntimeError("Import cancelled by user.")

            self.progress.emit("Storing original…", 0.0)
            item = self._library.import_file(
                self._source, on_progress=cb,
                with_animations=self._with_animations)
            if item is None:
                self.failed.emit("Import failed (see log).")
                return
            self.progress.emit("Done", 1.0)
            self.finished_ok.emit(item.id)
        except Exception as e:
            tb = traceback.format_exc()
            log.error("PPT import worker:\n%s", tb)
            self.failed.emit(str(e))


class PdfImportWorker(_BaseImportWorker):
    """Same shape as the PPT worker but for PDF imports."""

    def run(self) -> None:
        try:
            def cb(msg: str, frac: float) -> None:
                self.progress.emit(msg, frac)
                if self._cancel:
                    raise RuntimeError("Import cancelled by user.")

            self.progress.emit("Storing original…", 0.0)
            item = self._library.import_file(self._source, on_progress=cb)
            if item is None:
                self.failed.emit("Import failed (see log).")
                return
            self.progress.emit("Done", 1.0)
            self.finished_ok.emit(item.id)
        except Exception as e:
            tb = traceback.format_exc()
            log.error("PDF import worker:\n%s", tb)
            self.failed.emit(str(e))


class RenderWorker(_BaseImportWorker):
    """Renders media ALREADY in the library — from the original file stored
    on the board, so nothing is imported again. ``source`` is that original
    (it only names the dialog); ``item_id`` is what gets rendered."""

    def __init__(self, library, source: Path, parent=None, *,
                 item_id: str = "", with_animations: bool = False):
        super().__init__(library, source, parent)
        self._item_id = item_id
        self._with_animations = bool(with_animations)

    def run(self) -> None:
        try:
            def cb(msg: str, frac: float) -> None:
                self.progress.emit(msg, frac)
                if self._cancel:
                    raise RuntimeError("Render cancelled by user.")

            self.progress.emit("Rendering…", 0.0)
            self._library.regenerate_cache(
                self._item_id, force=True, on_progress=cb,
                with_animations=self._with_animations)
            item = self._library.get(self._item_id)
            if item is None or item.preview_count == 0:
                self.failed.emit(getattr(item, "render_error", "") or "failed")
                return
            self.progress.emit("Done", 1.0)
            self.finished_ok.emit(item.id)
        except Exception as e:
            log.error("Render worker:\n%s", traceback.format_exc())
            self.failed.emit(str(e))


def run_render_with_progress(parent, library, item,
                             with_animations: bool = False):
    """Render an unrendered library item with a progress dialog. Returns the
    item (re-read from the library) — check ``preview_count`` and
    ``render_error`` for the outcome."""
    original = library.absolute_path(item.original_relpath)
    if not original or not Path(original).exists():
        return None
    _run_with_dialog(
        parent, library, Path(original), RenderWorker,
        "Rendering " + ("PowerPoint" if item.type == "ppt" else "PDF"),
        subtitle=f"Rendering {item.name}",
        worker_kwargs={"item_id": item.id,
                       "with_animations": with_animations})
    return library.get(item.id)


# ════════════════════════════════════════════════════════════════════════════
# Frameless progress dialog
# ════════════════════════════════════════════════════════════════════════════

class ImportProgressDialog(QDialog):
    """A small, modal, title-bar-less dialog that shows import progress.

    The dialog:
      * Has no title bar (FramelessWindowHint).
      * Is translucent + paints a rounded card with a drop shadow.
      * Centres itself over the parent window.
      * Disables its Cancel button while the worker is finalising
        (last 4 % of progress) so the user can't abort during file writes.
    """

    cancel_requested = Signal()

    def __init__(self, parent=None,
                 title: str = "Importing media",
                 subtitle: str = ""):
        super().__init__(
            parent,
            Qt.FramelessWindowHint | Qt.Dialog | Qt.WindowStaysOnTopHint,
        )
        self.setAttribute(Qt.WA_TranslucentBackground)
        self.setModal(True)
        self.setStyleSheet("background: transparent;")
        prepare_aero_widget(self)        # translucent surface for the glass

        self._title_text    = title
        self._subtitle_text = subtitle

        # Zero margin so the card fills the window and the Aero glass
        # covers the whole dialog. A DWM drop-shadow + rounded corners are
        # applied in showEvent instead of a manual margin + QGraphicsDropShadow
        # (which also avoids the layered-window conflict with Win32 aero).
        outer = QVBoxLayout(self)
        outer.setContentsMargins(0, 0, 0, 0)
        outer.setSpacing(0)

        # Card holder (frosted-glass panel)
        self._card = QWidget(self)
        self._card.setObjectName("ImportProgressCard")
        outer.addWidget(self._card)

        # Card layout
        v = QVBoxLayout(self._card)
        v.setContentsMargins(28, 24, 28, 22)
        v.setSpacing(14)

        # ─── Title row ───────────────────────────────────────────────
        # Everything below comes from the theme tokens. This dialog used to
        # carry its own grey ladder (#e6e6e6 / #a8a8a8 / #cfcfcf / #6e6e6e)
        # and its own blue, which is how a surface drifts: nothing here was
        # wrong on its own, it just stopped matching the rest of the app.
        t = theme_mgr.tokens

        self._title_label = QLabel(title)
        # Sized in PIXELS off the type scale, not points. A point size is
        # resolved against the screen's logical DPI, so it is the one text
        # measurement in the app that does NOT track UiScale — which is why
        # this title used to grow out of step with every other panel title.
        self._title_label.setStyleSheet(
            f"color: {t.text_primary}; background: transparent;"
            f"font-size: {S.scale_px(t.font_subtitle)}px;"
            f"font-weight: {t.weight_bold};"
        )
        v.addWidget(self._title_label)

        # ─── Subtitle / status row ──────────────────────────────────
        self._status_label = QLabel(subtitle or "Preparing…")
        self._status_label.setStyleSheet(
            f"color: {t.text_secondary}; background: transparent;"
            f"font-size: {S.scale_px(t.font_caption)}px;"
        )
        self._status_label.setWordWrap(True)
        # Let the label claim as many lines as the (possibly long) warning needs;
        # its height is driven by heightForWidth at the card's fixed width.
        self._status_label.setSizePolicy(QSizePolicy.Policy.Preferred,
                                         QSizePolicy.Policy.Minimum)
        v.addWidget(self._status_label)

        # ─── Progress bar ───────────────────────────────────────────
        self._bar = QProgressBar()
        self._bar.setRange(0, 100)
        self._bar.setValue(0)
        self._bar.setTextVisible(False)
        self._bar.setFixedHeight(S.scale_px(8))
        # The chunk was a #4f7cff -> #7eb0ff gradient: a violet-leaning blue,
        # next to an app whose accent is a cyan-leaning one. Two blues that
        # close together do not read as a second colour, they read as a
        # mistake. It is the accent now, flat, like every other filled
        # control.
        self._bar.setStyleSheet(f"""
            QProgressBar {{
                background: {t.progress_bg};
                border: 1px solid {t.stroke_subtle};
                border-radius: {S.scale_px(4)}px;
            }}
            QProgressBar::chunk {{
                background: {t.progress_chunk};
                border-radius: {S.scale_px(3)}px;
            }}
        """)
        v.addWidget(self._bar)

        # ─── Percentage + cancel row ────────────────────────────────
        row = QHBoxLayout()
        row.setContentsMargins(0, 0, 0, 0)
        self._pct_label = QLabel("0 %")
        self._pct_label.setStyleSheet(
            f"color: {t.text_secondary}; background: transparent;"
            f"font-size: {S.scale_px(t.font_caption)}px;"
        )
        row.addWidget(self._pct_label)
        row.addStretch()
        self._cancel_btn = QPushButton("Cancel")
        self._cancel_btn.setCursor(Qt.PointingHandCursor)
        # control_h_sm, so this button is the same height as every other
        # secondary button in the app rather than 28px on its own authority.
        self._cancel_btn.setFixedHeight(S.scale_px(t.control_h_sm))
        self._cancel_btn.setStyleSheet(f"""
            QPushButton {{
                background: {t.fill_default};
                color: {t.text_primary};
                border: 1px solid {t.stroke_default};
                border-radius: {S.scale_px(t.radius_sm)}px;
                padding: 0 {S.scale_px(t.space_md)}px;
                font-size: {S.scale_px(t.font_body)}px;
            }}
            QPushButton:hover {{
                background: {t.fill_hover};
                border-color: {t.stroke_strong};
            }}
            QPushButton:pressed {{
                background: {t.fill_pressed};
            }}
            QPushButton:disabled {{
                color: {t.text_disabled};
                background: {t.fill_disabled};
                border-color: {t.stroke_subtle};
            }}
        """)
        self._cancel_btn.clicked.connect(self._on_cancel_clicked)
        row.addWidget(self._cancel_btn)
        v.addLayout(row)

        # Sizing: fixed WIDTH (so the warning word-wraps against a known width)
        # but DYNAMIC HEIGHT. SetFixedSize makes the dialog track the layout's
        # sizeHint, so when update_progress() sets a longer (multi-line) message
        # the dialog grows to fit it instead of clipping. A minimum height keeps
        # short-message states looking balanced. All values are logical px and
        # are scaled by Qt's High-DPI handling, so this holds at any resolution.
        self._card.setFixedWidth(S.scale_px(364))
        self._card.setMinimumHeight(S.scale_px(108))
        outer.setSizeConstraint(QLayout.SizeConstraint.SetFixedSize)

        # Frosted-glass card — no CSS border or border-radius on the OUTER
        # card (DWM rounding is the only window-edge shape). Inner widgets
        # like the progress bar keep their own small radii — those are
        # safely interior and never touch the DWM-rounded window corners.
        self._card.setStyleSheet("""
            QWidget#ImportProgressCard {
                background: transparent;
                border: none;
                border-radius: 0px;
            }
        """)

    # ── public API ──────────────────────────────────────────────────

    def update_progress(self, message: str, fraction: float) -> None:
        """Slot connected to worker.progress."""
        pct = max(0, min(100, int(round(fraction * 100))))
        self._bar.setValue(pct)
        self._pct_label.setText(f"{pct} %")
        if message:
            self._status_label.setText(message)
        # Disable cancel during the last 4 % (file writes, manifest, etc.).
        self._cancel_btn.setEnabled(fraction < 0.96)
        QApplication.processEvents()   # keep the bar painted promptly

    def set_finalising(self) -> None:
        self._cancel_btn.setEnabled(False)
        self._status_label.setText("Wrapping up… 🚀")

    # ── private ─────────────────────────────────────────────────────

    def _on_cancel_clicked(self) -> None:
        self._cancel_btn.setEnabled(False)
        self._status_label.setText("Stopping… 😅")
        self.cancel_requested.emit()

    def _apply_glass(self):
        """Apply the Aero backdrop + DWM square corners + shadow.
        Done after the fade because animating windowOpacity makes the window
        layered, which fights the Win32 aero composition attribute.

        ``mode="square"`` on the DWM call — this dialog's card draws its
        own QSS border-radius. Leaving DWM at its default "round" rounds
        the raw HWND at a fixed ~8px system radius that doesn't match the
        card's radius, producing a faint double-curve artifact at each
        corner. Square HWND + one CSS-radius card = a single curve."""
        try: apply_aero(self)
        except Exception as e: log.debug("apply_aero: %s", e)
        try: apply_rounded_corners(self, mode="round")
        except Exception as e: log.debug("apply_rounded_corners: %s", e)
        try: enable_shadow(self)
        except Exception as e: log.debug("enable_shadow: %s", e)

    def showEvent(self, event):
        # Centre on parent window
        super().showEvent(event)
        parent = self.parent()
        if isinstance(parent, QWidget):
            geo  = self.frameGeometry()
            pgeo = parent.frameGeometry()
            geo.moveCenter(pgeo.center())
            self.move(geo.topLeft())
        # Brief fade-in, then drop in the Aero glass.
        self.setWindowOpacity(0.0)
        self._fade = QPropertyAnimation(self, b"windowOpacity")
        self._fade.setDuration(140)
        self._fade.setStartValue(0.0)
        self._fade.setEndValue(1.0)
        self._fade.setEasingCurve(QEasingCurve.OutCubic)
        self._fade.finished.connect(self._apply_glass)
        self._fade.start()


# ════════════════════════════════════════════════════════════════════════════
# Convenience helpers — show dialog + run worker + return MediaItem
# ════════════════════════════════════════════════════════════════════════════

def confirm_large_document(parent, source: Path, *,
                          kind: str = "document",
                          page_count: int = 0) -> bool:
    """Warn before importing a very large document. True = go ahead.

    A four-hundred-page scan is a legitimate thing for a teacher to have,
    and it is also several minutes of a modal progress dialog. Starting
    that without saying so is how somebody ends up force-quitting the app
    two minutes in, believing it has hung — which loses the board they had
    open as well as the import.

    The estimate is MEASURED on this machine, not derived from the core
    count: ``estimate_render_seconds`` renders the first page and times it,
    which is the only way to account for a laptop against a workstation, a
    text report against a scanned chapter, and a 1920 px board against a
    4 K one. It costs one page's worth of work — tens to hundreds of
    milliseconds against the minutes being predicted.

    Below the threshold, or when the count or the estimate cannot be
    established, this returns True without saying anything. A warning is
    only worth interrupting for when there is something real to say.
    """
    from ui.util.media_cache import (LARGE_DOCUMENT_PAGES, format_duration,
                                     estimate_render_seconds)
    if page_count < LARGE_DOCUMENT_PAGES:
        return True

    noun = "slides" if kind == "presentation" else "pages"
    try:
        seconds = estimate_render_seconds(source, page_count=page_count)
    except Exception:
        seconds = None

    if seconds is not None:
        timing = ("On this computer it should take %s."
                  % format_duration(seconds))
    else:
        # Rather than invent a number. "A while" is honest; a wrong
        # estimate is worse than none, because the next long import will
        # not be believed either.
        timing = "It will take a while."

    nl = "\n\n"
    body = (
        "“%s” has %s %s.%s"
        "%s Every %s is rendered once, up front, so the board can show them "
        "instantly afterwards — but the import itself cannot be hurried "
        "along, and LogicBoard will be busy until it finishes.%s"
        "You can cancel part-way through if you change your mind.%s"
        "Import it now?"
        % (source.name, "{:,}".format(page_count), noun, nl,
           timing, noun[:-1], nl, nl)
    )

    try:
        from ui.panels.aero_alert import AeroAlert
        reply = AeroAlert.question(
            parent, "That is a big %s 💯" % kind, body,
            AeroAlert.Yes | AeroAlert.No, AeroAlert.Yes)
        return reply == AeroAlert.Yes
    except Exception:
        log.exception("large-document confirmation failed; importing anyway")
        return True


def _run_with_dialog(parent, library, source: Path,
                     worker_cls, title: str,
                     subtitle: Optional[str] = None,
                     worker_kwargs: Optional[dict] = None) -> Optional[object]:
    """Generic driver shared by PPT and PDF import.

    Returns the new MediaItem, or None if the user cancelled / it failed.
    """
    dlg = ImportProgressDialog(parent, title=title,
                                subtitle=subtitle or f"Processing {source.name}")
    worker = worker_cls(library, source, **(worker_kwargs or {}))

    # Wire up signals
    worker.progress.connect(dlg.update_progress)
    final_item_id: dict = {"id": None, "ok": False, "err": None}

    def on_ok(item_id: str):
        final_item_id["id"] = item_id
        final_item_id["ok"] = True
        # Brief delay so the user sees "Done" / 100 % before the dialog closes.
        QTimer.singleShot(250, dlg.accept)

    def on_fail(msg: str):
        final_item_id["err"] = msg
        dlg.set_finalising()
        QTimer.singleShot(0, dlg.reject)

    worker.finished_ok.connect(on_ok)
    worker.failed.connect(on_fail)
    dlg.cancel_requested.connect(worker.request_cancel)

    worker.start()
    dlg.exec()
    # If the dialog closed but the worker is still running (e.g. user
    # cancelled mid-render), give it a moment to wrap up cleanly.
    if worker.isRunning():
        worker.wait(8000)
        if worker.isRunning():
            worker.terminate()
            worker.wait(2000)

    if not final_item_id["ok"]:
        if final_item_id["err"]:
            log.warning("Import of %s failed: %s",
                        source.name, final_item_id["err"])
        return None

    return library.get(final_item_id["id"])


def run_ppt_import_with_progress(parent, library,
                                  source: Path,
                                  with_animations: bool = False) -> Optional[object]:
    """Import a PowerPoint file with a progress dialog. Returns the new
    MediaItem on success or None on cancel/failure."""
    from ui.util.media_cache import count_ppt_slides
    # Counted from the .pptx's own zip directory rather than by opening the
    # deck — asking PowerPoint to open a 900-slide file is the very thing
    # the warning exists to warn about.
    if not confirm_large_document(parent, source, kind="presentation",
                                  page_count=count_ppt_slides(source)):
        return None
    mode = "with animations" if with_animations else "without animations"
    return _run_with_dialog(
        parent, library, source, PptImportWorker, "Importing PowerPoint",
        subtitle=f"Importing {source.name} — {mode}",
        worker_kwargs={"with_animations": with_animations})


def run_pdf_import_with_progress(parent, library,
                                  source: Path) -> Optional[object]:
    """Import a PDF file with a progress dialog. Returns the new MediaItem
    on success or None on cancel/failure."""
    from ui.util.media_cache import count_pdf_pages
    if not confirm_large_document(parent, source, kind="PDF",
                                  page_count=count_pdf_pages(source)):
        return None
    return _run_with_dialog(parent, library, source,
                             PdfImportWorker, "Importing PDF")