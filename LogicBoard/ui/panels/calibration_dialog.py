"""
Input Calibration — calibration dialogs (Phase 2).

A single glassy, touch-friendly dialog (``CalibrationDialog``) drives all three
calibration flows (stylus / eraser tip / palm) via a small config object. It:

  • Shows clear instructions + a large capture area sized for smart boards.
  • Captures real touch geometry from QTouchEvent points:
        ellipseDiameters() -> contact width/height  (=> area, aspect ratio)
        pressure()         -> contact pressure (if the hardware reports it)
        rotation()         -> contact orientation/angle (if available)
  • Tracks progress ("Sample 3 of 5"), supports Retry.
  • Anchors the run to the input family of its FIRST tap, and drops any
    later tap that arrives as a different one — that single sample, not the
    whole run. See ``_record_sample``.
  • On completion runs the Phase-1 statistics (median / std-dev / min/max,
    outlier rejection; palm also gets an aspect-ratio range) and shows a
    success summary.
  • Hands the finished profile back to the caller, which persists it.

This module does NOT touch the live drawing pipeline — it only listens for
touches while the dialog is open. Runtime classification is Phase 3.
"""
from __future__ import annotations

import math
import time
import logging
from dataclasses import dataclass
from typing import List, Optional, Callable

from PySide6.QtWidgets import (
    QDialog, QVBoxLayout, QHBoxLayout, QLabel, QPushButton, QFrame, QWidget,
)
from PySide6.QtCore import Qt, QEvent, QTimer
from PySide6.QtGui import QColor

from ui.util.scaling import UiScale
from ui.util.aero import prepare_aero_widget, apply_acrylic, enable_shadow
from ui.util.animations import fade_in
from ui.util.audio import click_sound
from ui.theme.theme_manager import theme_mgr
from ui.util.input_calibration import (
    TouchSample, ContactProfile, PalmProfile, summarize, reject_outliers,
    sources_of, SOURCE_TOUCH, SOURCE_TABLET, SOURCE_MOUSE,
    SOURCELESS_GEOMETRY,
)

# Human labels for the input families, used in the summary + error text.
SOURCE_LABELS = {
    SOURCE_TOUCH:  "touch",
    SOURCE_TABLET: "pen / tablet",
    SOURCE_MOUSE:  "mouse click",
}

logger = logging.getLogger(__name__)

# Minimum time between two ACCEPTED samples, in seconds.
#
# One physical tap does not reliably arrive as one sample. A touch frame can
# report the same contact again a few milliseconds later as a fresh Pressed
# point, a stylus tip can bounce, and a board that delivers a tip as both a
# tablet event and a click hands us two. Each of those filled a progress dot
# the user never earned, so "tap 5 times" finished in three taps on samples
# that are really two or three readings of the same contact — and the median
# and spread the profile is built from are then measuring the hardware's
# chatter rather than the tip.
#
# 250 ms is comfortably longer than any of that and far shorter than the gap
# between two deliberate taps, so a real second tap is never swallowed.
SAMPLE_COOLDOWN_S = 0.25


# =============================================================================
# Config — one per calibration kind
# =============================================================================
@dataclass
class CalibrationConfig:
    kind: str                 # "stylus" | "eraser" | "palm"
    title: str                # dialog title
    instruction: str          # what the user should do
    target_samples: int       # how many taps to collect
    is_palm: bool = False      # palm captures extra aspect-ratio metrics


STYLUS_CONFIG = CalibrationConfig(
    kind="stylus",
    title="Calibrate Stylus",
    instruction="Tap the area below naturally with your stylus tip 5 times.",
    target_samples=5,
)
ERASER_CONFIG = CalibrationConfig(
    kind="eraser",
    title="Calibrate Eraser Tip",
    instruction="Tap the area below naturally with the eraser tip 5 times.",
    target_samples=5,
)
PALM_CONFIG = CalibrationConfig(
    kind="palm",
    title="Calibrate Palm",
    instruction="Rest your palm on the area below naturally 10 times.\n"
                "Lift and place it again each time, as you would while writing.",
    target_samples=10,
    is_palm=True,
)


def config_for(kind: str) -> CalibrationConfig:
    return {"stylus": STYLUS_CONFIG, "eraser": ERASER_CONFIG,
            "palm": PALM_CONFIG}[kind]


# =============================================================================
# Capture surface — a large touch target that records samples
# =============================================================================
class _CaptureArea(QFrame):
    """Big rounded touch zone. Behavior depends on the calibration kind:

    STYLUS / ERASER MODE (``palm_mode=False``):
        Each new touch-down records one ``TouchSample`` immediately, one per
        contact. This matches the natural "5 taps" flow — one tap = one
        sample, whether the hardware emits it as TouchBegin or a subsequent
        TouchUpdate with a newly-pressed point.

    PALM MODE (``palm_mode=True``):
        A palm resting on a screen emits MANY simultaneous touch points
        (fingertips, palm heel, base of thumb) and often more via
        TouchUpdate as the palm settles. The old code counted every
        Pressed state as a separate sample, which meant one rest could
        generate a dozen contaminated samples in seconds — some of them
        from fingertips (small contact area), which pulls the palm
        profile's ``min`` down toward stylus-tip range and breaks
        classification.

        Instead, palm mode tracks the LARGEST contact seen throughout an
        entire touch sequence (from the first TouchBegin until every
        finger lifts) and commits it as a SINGLE sample on release. The
        largest patch during a rest is the palm envelope itself; smaller
        simultaneous points are just fingers along for the ride and get
        discarded.

    On desktop / no touch hardware, both modes fall back to a synthetic
    mouse click so the flow is still testable without a touch screen.
    """

    def __init__(self, on_sample: Callable[[TouchSample], None],
                 parent=None, *, palm_mode: bool = False):
        super().__init__(parent)
        self._on_sample = on_sample
        self._palm_mode = bool(palm_mode)
        self.setObjectName("CaptureArea")
        self.setAttribute(Qt.WA_AcceptTouchEvents, True)
        self._pulse = False
        # Palm-mode aggregation state — tracks the LARGEST contact seen
        # during a touch sequence, plus the set of currently-active touch
        # point ids so we can detect when the whole hand has lifted.
        self._palm_best: Optional[TouchSample] = None
        self._active_ids: set = set()
        # Real-touch witness. Flips True the moment ANY genuine touch
        # event (native QTouchEvent) reaches ``event()``. Used by
        # ``_finish()`` to detect the "user calibrated with a mouse on
        # a desktop and doesn't realise the profile is meaningless"
        # case: if we never saw a real touch, the whole sample set was
        # synthesised from hardcoded ``_synth_w/_synth_h`` fallbacks
        # and would only bear on a smart board's actual sensor
        # response by coincidence.
        self._saw_real_touch: bool = False

    def saw_real_touch(self) -> bool:
        """Whether any native touch event has been observed during the
        calibration session. If False, every recorded sample came from
        the mouse fallback path and its geometry is synthetic."""
        return self._saw_real_touch

    def _sample_from_touchpoint(self, tp) -> TouchSample:
        """Build a TouchSample from one Qt touch point. Does NOT emit —
        the caller decides whether to commit immediately (stylus/eraser)
        or aggregate across a whole sequence (palm)."""
        try:
            d = tp.ellipseDiameters()
            w, h = float(d.width()), float(d.height())
        except Exception:
            w = h = 0.0
        if w <= 0 and h <= 0:
            w = h = 0.0
        area = math.pi * (w / 2.0) * (h / 2.0) if (w > 0 and h > 0) else 0.0
        try:
            pressure = float(tp.pressure())
        except Exception:
            pressure = None
        try:
            rotation = float(tp.rotation())
        except Exception:
            rotation = None
        return TouchSample(
            area=area, width=w, height=h,
            pressure=pressure if pressure and pressure > 0 else None,
            orientation=rotation if rotation else None,
            touch_points=1,
            source=SOURCE_TOUCH,
        )

    def tabletEvent(self, ev):
        """Capture the stylus when the board delivers it as a PEN event.

        This path did not exist, and its absence is why 'Calibrate Stylus'
        was unwinnable on an IR touch frame: such a frame classifies a thin
        tip as pen input and routes it to the tablet digitizer, so the tip
        NEVER arrives as a QTouchEvent. The capture area listened only for
        touch and mouse, so every tap with the front tip was either ignored
        or fell through to the mouse fallback and got refused as synthetic.

        A tablet event carries no contact ellipse, so there is no area to
        record — and we do not invent one. The sample's value is its SOURCE:
        it proves this tip is identified by input family, which is a stronger
        and more stable discriminator than a size the hardware won't report.
        """
        from PySide6.QtCore import QEvent as _QEvent
        if ev.type() == _QEvent.TabletPress:
            self._saw_real_touch = True
            try:
                pressure = float(ev.pressure())
            except Exception:
                pressure = None
            self._on_sample(TouchSample(
                area=0.0, width=0.0, height=0.0,
                pressure=pressure if pressure and pressure > 0 else None,
                touch_points=1, source=SOURCE_TABLET,
            ))
        ev.accept()

    def event(self, ev):
        et = ev.type()
        if et in (QEvent.TouchBegin, QEvent.TouchUpdate, QEvent.TouchEnd,
                  QEvent.TouchCancel):
            from PySide6.QtGui import QEventPoint
            # Real touch hardware in play — anything captured from now
            # on is genuine sensor data, not synthetic mouse fallback.
            self._saw_real_touch = True

            if self._palm_mode:
                # Track every active point through the whole sequence; keep
                # the LARGEST-area sample as our representative palm patch.
                # A palm arriving progressively (fingers first, then heel)
                # or all at once (slap flat) both end up with the true
                # palm envelope committed as ONE sample on final release.
                for tp in ev.points():
                    state = tp.state()
                    if state == QEventPoint.State.Released:
                        self._active_ids.discard(tp.id())
                    else:
                        self._active_ids.add(tp.id())
                        cand = self._sample_from_touchpoint(tp)
                        if (self._palm_best is None
                                or cand.area > self._palm_best.area):
                            self._palm_best = cand
                # Commit on TouchEnd/Cancel OR any event where no points
                # remain active — that's the moment the palm has fully
                # lifted. One rest → one sample of the largest patch.
                if (et in (QEvent.TouchEnd, QEvent.TouchCancel)
                        or not self._active_ids):
                    if self._palm_best is not None and self._palm_best.area > 0:
                        self._on_sample(self._palm_best)
                    self._palm_best = None
                    self._active_ids.clear()
                ev.accept()
                return True

            # Non-palm (stylus / eraser) — original behaviour: one sample
            # per newly-pressed point, immediately.
            if et in (QEvent.TouchBegin, QEvent.TouchUpdate):
                for tp in ev.points():
                    if tp.state() == QEventPoint.State.Pressed:
                        self._on_sample(self._sample_from_touchpoint(tp))
                ev.accept()
                return True
        return super().event(ev)

    # Mouse fallback — for boards that report a stylus tip as a plain left
    # click (see InputCalibration.mouse_is_stylus_front).
    def mousePressEvent(self, e):
        """Record a mouse sample with NO fabricated geometry.

        This used to cook up a width/height from hardcoded ``_synth_w`` /
        ``_synth_h`` constants plus jitter, producing a profile that looked
        measured but described nothing the hardware had reported — a
        made-up threshold is worse than no threshold, because it silently
        misclassifies instead of visibly failing. A mouse click genuinely
        has no contact area, so the sample records area 0 and identifies
        itself by SOURCE alone.
        """
        if self._on_sample:
            self._on_sample(TouchSample(area=0.0, width=0.0, height=0.0,
                                        touch_points=1, source=SOURCE_MOUSE))
        super().mousePressEvent(e)


# =============================================================================
# The calibration dialog
# =============================================================================
class CalibrationDialog(QDialog):
    """Glassy, centered calibration dialog. On success, ``result_profile``
    holds the computed ContactProfile/PalmProfile; the caller persists it."""

    def __init__(self, config: CalibrationConfig, parent=None,
                 *, allow_mouse: bool = False, calibration=None):
        """``allow_mouse`` — accept mouse clicks as genuine samples.

        Set when the user has turned on "Use mouse left click as stylus
        front tip", i.e. their board never reports that tip as a touch
        contact and emits a plain left click for it instead. On such a
        board the refuse-mouse guard in ``_finish`` is not protecting the
        user from anything — it is rejecting the only input the hardware
        can produce for that tip.
        """
        # WindowStaysOnTopHint is load-bearing, and leaving it off is what
        # made calibration lock the whole app.
        #
        # Calibration is launched FROM the Settings panel, and that panel is a
        # Qt.Tool | WindowStaysOnTopHint window. A topmost window sits above a
        # non-topmost one no matter what else is true — parentage and modality
        # do not enter into it. So this dialog mapped underneath Settings,
        # while exec() made it application-modal and refused every click
        # aimed at the Settings panel now covering it. Nothing on screen
        # responded and nothing could be closed, which is why the only way out
        # was Task Manager.
        #
        # Matching the flag puts both windows in the same topmost band, where
        # the last one raised wins — and the raise_/activateWindow in
        # showEvent makes sure that is this dialog.
        super().__init__(parent,
                         Qt.Dialog
                         | Qt.FramelessWindowHint
                         | Qt.WindowStaysOnTopHint)
        self.setModal(True)
        prepare_aero_widget(self)

        self._allow_mouse = bool(allow_mouse)
        # Live InputCalibration, so a new profile can be checked against the
        # one already stored for its counterpart (back tip vs palm).
        self._calibration = calibration
        self._config = config
        self._samples: List[TouchSample] = []
        # When the last accepted sample landed, for the debounce in
        # _record_sample. 0.0 means "no sample yet", which is always outside
        # the cooldown, so the first tap of a run is never delayed.
        self._last_sample_at: float = 0.0
        # The input family this run is anchored to. Set by the first accepted
        # sample; every later one has to match it or it is dropped. None
        # means "nothing captured yet, the next tap decides". Retry clears
        # it, which is the way out if the FIRST tap was the odd one.
        self._expected_source: Optional[str] = None
        self.result_profile = None      # set on success
        self.completed = False

        sc = UiScale.px
        t = theme_mgr.tokens

        self.setFixedSize(sc(520), sc(560))
        self.setStyleSheet("QDialog { background: transparent; }")

        root = QVBoxLayout(self)
        root.setContentsMargins(sc(22), sc(20), sc(22), sc(20))
        root.setSpacing(sc(14))

        # ---- Header: title + close ----
        header = QHBoxLayout()
        title = QLabel(config.title)
        title.setStyleSheet(
            f"color: {t.text_primary}; font-size: {sc(20)}px; "
            f"font-weight: bold; background: transparent;"
        )
        header.addWidget(title)
        header.addStretch()
        close_btn = QPushButton("✕")
        close_btn.setFixedSize(sc(34), sc(34))
        close_btn.setCursor(Qt.PointingHandCursor)
        close_btn.setStyleSheet(f"""
            QPushButton {{
                background-color: {t.card_grad}; color: {t.text_primary};
                border: 1px solid {t.border}; border-radius: {sc(8)}px;
                font-size: {sc(14)}px; font-weight: 700;
            }}
            QPushButton:hover {{
                background-color: {t.danger}; color: white; border-color: {t.danger};
            }}
        """)
        close_btn.clicked.connect(self.reject)
        header.addWidget(close_btn)
        root.addLayout(header)

        # ---- Instruction ----
        self._instruction = QLabel(config.instruction)
        self._instruction.setWordWrap(True)
        self._instruction.setStyleSheet(
            f"color: {t.text_secondary}; font-size: {sc(13)}px; "
            f"background: transparent;"
        )
        root.addWidget(self._instruction)

        # ---- Capture area ----
        # Palm calibration uses aggregate-on-release capture (one sample per
        # rest, taking the largest simultaneous contact as the palm's
        # envelope). Stylus/eraser use the simpler one-sample-per-tap flow.
        self._capture = _CaptureArea(self._record_sample,
                                     palm_mode=config.is_palm)
        self._capture.setMinimumHeight(sc(260))
        self._capture.setStyleSheet(f"""
            QFrame#CaptureArea {{
                background-color: {t.card_grad};
                border: 2px dashed {t.border};
                border-radius: {sc(16)}px;
            }}
        """)
        # (No synthetic geometry seeding any more — see mousePressEvent.
        #  Fabricated per-kind sizes were the last hardcoded px² values that
        #  could reach a saved profile.)

        cap_inner = QVBoxLayout(self._capture)
        cap_inner.setAlignment(Qt.AlignCenter)
        self._capture_hint = QLabel("Tap here")
        self._capture_hint.setAlignment(Qt.AlignCenter)
        self._capture_hint.setStyleSheet(
            f"color: {t.text_muted}; font-size: {sc(15)}px; "
            f"font-weight: bold; background: transparent; border: none;"
        )
        cap_inner.addWidget(self._capture_hint)
        root.addWidget(self._capture, 1)

        # ---- Progress ----
        self._progress = QLabel()
        self._progress.setAlignment(Qt.AlignCenter)
        self._progress.setStyleSheet(
            f"color: {t.accent}; font-size: {sc(15)}px; "
            f"font-weight: bold; background: transparent;"
        )
        root.addWidget(self._progress)

        # progress dots
        self._dots_row = QHBoxLayout()
        self._dots_row.setSpacing(sc(6))
        self._dots_row.setAlignment(Qt.AlignCenter)
        self._dots: List[QFrame] = []
        for _ in range(config.target_samples):
            dot = QFrame()
            dot.setFixedSize(sc(12), sc(12))
            self._dots.append(dot)
            self._dots_row.addWidget(dot)
        root.addLayout(self._dots_row)

        # ---- Buttons: Retry + Done ----
        btn_row = QHBoxLayout()
        btn_row.setSpacing(sc(10))
        self._retry_btn = QPushButton("Retry")
        self._retry_btn.setMinimumHeight(sc(44))
        self._retry_btn.setCursor(Qt.PointingHandCursor)
        self._retry_btn.setStyleSheet(f"""
            QPushButton {{
                background: {t.card_grad}; color: {t.text_primary};
                border: 1px solid {t.border}; border-radius: {sc(10)}px;
                font-size: {sc(14)}px; font-weight: bold;
            }}
            QPushButton:hover {{ background: {t.bg_card_hover}; border-color: {t.accent}; }}
            QPushButton:disabled {{ color: {t.text_muted}; }}
        """)
        self._retry_btn.clicked.connect(self._reset)
        btn_row.addWidget(self._retry_btn)

        self._done_btn = QPushButton("Done")
        self._done_btn.setMinimumHeight(sc(44))
        self._done_btn.setCursor(Qt.PointingHandCursor)
        self._done_btn.setStyleSheet(f"""
            QPushButton {{
                background: {t.accent_grad}; color: #000;
                border: none; border-radius: {sc(10)}px;
                font-size: {sc(14)}px; font-weight: bold;
            }}
            QPushButton:hover {{ background: {t.accent_grad_hover}; }}
            QPushButton:disabled {{ background: {t.bg_input}; color: {t.text_muted}; }}
        """)
        self._done_btn.clicked.connect(self.accept)
        btn_row.addWidget(self._done_btn)
        root.addLayout(btn_row)

        self._reset()

    # ---- centering + aero ----
    def showEvent(self, event):
        super().showEvent(event)
        from PySide6.QtGui import QGuiApplication
        screen = QGuiApplication.primaryScreen()
        if screen is not None:
            geo = screen.availableGeometry()
            fg = self.frameGeometry()
            fg.moveCenter(geo.center())
            self.move(fg.topLeft())
        apply_acrylic(self)
        enable_shadow(self)
        fade_in(self)

        # Claim the top of the topmost band and take focus. The flag above
        # settles this dialog against ORDINARY windows; among fellow topmost
        # windows (the Settings panel that opened us, the toolbar groups)
        # z-order is simply whoever was raised last, so say so explicitly.
        #
        # Deferred by one event-loop turn as well: Settings calls raise_() on
        # itself when it opens, and on the tick where both happen the later
        # call is the one that sticks. Doing it twice costs nothing and
        # removes the ordering dependency entirely.
        self.raise_()
        self.activateWindow()
        QTimer.singleShot(0, self._claim_foreground)

    def _claim_foreground(self) -> None:
        """Re-assert this dialog's z-order and focus after the event loop has
        settled. Guarded because a fast Escape can destroy the dialog before
        the deferred call lands."""
        try:
            if self.isVisible():
                self.raise_()
                self.activateWindow()
        except RuntimeError:
            # Already destroyed — nothing to raise.
            pass

    # ---- workflow ----
    def _reset(self) -> None:
        self._samples.clear()
        # Retry starts a fresh run, so the next tap must be accepted at once
        # rather than serving out the cooldown of the run being abandoned.
        self._last_sample_at = 0.0
        # And it re-anchors: Retry is exactly what somebody reaches for when
        # the run latched onto the wrong input family on its first tap.
        self._expected_source = None
        self.result_profile = None
        self.completed = False
        self._capture_hint.setText("Tap here")
        self._instruction.setText(self._config.instruction)
        self._done_btn.setEnabled(False)
        self._update_progress()
        self._paint_dots()
        # Also drop any in-flight palm aggregation — otherwise if the user
        # hit Retry mid-rest (e.g. with another hand), the leftover best
        # sample would commit into the fresh session on next release.
        if hasattr(self, "_capture"):
            self._capture._palm_best = None
            self._capture._active_ids.clear()

    def _record_sample(self, sample: TouchSample) -> None:
        """The ONE funnel every sample passes through — touch, tablet and
        mouse, for all three calibration kinds. Two gates, in this order.

        **Debounce.** One physical tap does not reliably arrive as one
        sample; see SAMPLE_COOLDOWN_S.

        **The input family has to match the rest of the run.** A tip the
        board reports inconsistently — some taps as pen events, others as
        touch — has no single profile to describe it, and averaging across
        two sensors describes neither.

        That second rule used to be enforced at the END, in _finish, over
        the finished set: one odd tap out of ten and the entire run was
        thrown away, with nothing to do but start the ten again. Ten taps
        is a long way to walk back for one bad one, and on a board that
        mis-reports occasionally the odds of getting ten clean in a row are
        not good — the flow could be genuinely unwinnable.

        So the check moved here, per sample. A tap that does not match is
        dropped on its own: the counter does not advance, the dot stays
        empty, and the user is asked for that one tap again. The run
        survives, and the set that reaches _finish is homogeneous by
        construction.
        """
        if len(self._samples) >= self._config.target_samples:
            return

        now = time.monotonic()
        if now - self._last_sample_at < SAMPLE_COOLDOWN_S:
            logger.debug("Calibration (%s): ignored a sample %.0f ms after the "
                         "last one", self._config.kind,
                         (now - self._last_sample_at) * 1000.0)
            return

        src = (getattr(sample, "source", "") or "")
        if src and self._expected_source and src != self._expected_source:
            # NOTE the ordering: the debounce ran first and this returns
            # WITHOUT stamping _last_sample_at. That combination is what
            # makes a board that delivers one tap twice, once per family,
            # work correctly — the mismatched delivery is discarded and the
            # matching one, milliseconds later, is still inside no cooldown
            # and lands. Stamping here would swallow it and cost the user a
            # tap they did make.
            self._reject_sample(src)
            return

        self._last_sample_at = now
        if src and not self._expected_source:
            self._expected_source = src
            logger.debug("Calibration (%s): anchored to %s input",
                         self._config.kind, src)
        click_sound.play_click()
        self._samples.append(sample)
        self._note_cleared()
        self._update_progress()
        self._paint_dots()
        if len(self._samples) >= self._config.target_samples:
            self._finish()

    def _reject_sample(self, got: str) -> None:
        """Drop one mismatched tap and ask for it again."""
        want = self._expected_source or ""
        logger.info("Calibration (%s): dropped a %s sample — this run is "
                    "anchored to %s", self._config.kind, got, want)
        got_label = SOURCE_LABELS.get(got, got or "unknown")
        want_label = SOURCE_LABELS.get(want, want or "unknown")
        self._instruction.setText(
            f"That tap arrived as {got_label}, not {want_label} — "
            f"it wasn't counted.\n"
            f"Tap again with the same tip you started with."
        )
        self._capture_hint.setText("Tap again")

    def _note_cleared(self) -> None:
        """Put the instruction and hint back after a good tap.

        Called on every accepted sample rather than only after a rejection:
        a board that double-delivers can flash the rejection notice for the
        few milliseconds between the two halves of one physical tap, and
        leaving it on screen after the tap was in fact counted would be a
        lie about what just happened.
        """
        if self._capture_hint.text() != "Tap here":
            self._capture_hint.setText("Tap here")
        if self._instruction.text() != self._config.instruction:
            self._instruction.setText(self._config.instruction)

    def _update_progress(self) -> None:
        n = len(self._samples)
        total = self._config.target_samples
        if n >= total:
            self._progress.setText("Calibration complete ✓")
        else:
            self._progress.setText(f"Sample {n + 1} of {total}")

    def _paint_dots(self) -> None:
        sc = UiScale.px
        t = theme_mgr.tokens
        n = len(self._samples)
        for i, dot in enumerate(self._dots):
            filled = i < n
            color = t.accent if filled else t.border
            dot.setStyleSheet(
                f"background-color: {color}; border-radius: {sc(6)}px;"
            )

    def _fail(self, message: str) -> None:
        """Reject this calibration run and tell the user to try again."""
        logger.warning("Calibration (%s) rejected: %s",
                       self._config.kind, message)
        self.result_profile = None
        self.completed = False
        self._instruction.setText(message + "\n\nTap Retry to calibrate again.")
        self._capture_hint.setText("✕")
        self._done_btn.setEnabled(False)

    def _finish(self) -> None:
        """Compute the profile from captured samples and show the summary."""
        # Refuse mouse-only calibration. When the user drives the
        # calibration flow with a mouse (desktop testing, or a smart
        # board where the touch stack isn't reporting native
        # ``QTouchEvent`` to Qt), every sample came from the synthetic
        # fallback in ``_CaptureArea.mousePressEvent`` which cooks up
        # geometry from ``_synth_w`` / ``_synth_h`` — hardcoded
        # per-kind constants that have no relation to how the actual
        # hardware will report a real touch. The resulting profile
        # looks calibrated but is completely made up. Refuse to save
        # so the user gets a clear signal that they need real touch
        # data.
        if (self._samples and not self._capture.saw_real_touch()
                and not self._allow_mouse):
            logger.warning(
                "Calibration (%s): every sample came from the mouse "
                "fallback — refusing to save a synthetic profile.",
                self._config.kind)
            self._instruction.setText(
                "This calibration was done with the mouse, not real "
                "touch input. The captured sizes are placeholders and "
                "wouldn't match your smart board's actual sensor. "
                "Retry using touches, or skip calibration."
            )
            self._done_btn.setEnabled(False)
            return

        # ---- Rule 1: every sample must be the SAME input family ----------
        # A BACKSTOP now, not the enforcement point. _record_sample refuses a
        # mismatched sample as it arrives, so a mixed set can no longer be
        # assembled through the normal path — reaching this branch means
        # samples got in some other way, and the profile would describe
        # neither sensor. Kept rather than deleted because the cost is one
        # comparison and the failure it catches is silent.
        srcs = sources_of(self._samples)
        if len(srcs) > 1:
            pretty = ", ".join(SOURCE_LABELS.get(s, s) for s in srcs)
            self._fail(
                f"Your board reported these taps as MORE THAN ONE kind of "
                f"input ({pretty}). Every tap has to come through the same "
                f"way for the calibration to mean anything — try again and "
                f"keep using the same tip, at the same angle and pressure."
            )
            return
        source = srcs[0] if srcs else ""
        source_label = SOURCE_LABELS.get(source, source or "unknown")

        # ---- Build the profile -------------------------------------------
        if self._config.is_palm:
            profile = PalmProfile.from_touch_samples(self._samples)
        else:
            profile = ContactProfile.from_samples(self._samples)

        # ---- Rule 2: geometry is required where it's the discriminator ----
        # The back tip and the palm are separated purely by size, so a
        # source-only profile is useless for them. The FRONT tip is exempt:
        # on an IR frame it arrives as a pen event with no ellipse at all,
        # and its input family identifies it perfectly well on its own.
        needs_area = self._config.kind in ("eraser", "palm")
        if needs_area and not profile.has_area:
            self._fail(
                f"Your board reported these as {source_label} input with no "
                f"contact size, so there's no area to measure. The "
                f"{'palm' if self._config.is_palm else 'back tip'} is "
                f"identified by how big the contact is, so it can't be "
                f"calibrated on this input."
            )
            return

        # ---- Rule 3: back tip and palm must be distinguishable -----------
        # Checked against whatever is ALREADY stored for the other one, in
        # both directions, so it fires whichever order the user calibrates in.
        conflict = self._cross_check(profile)
        if conflict:
            self._fail(conflict)
            return

        self.result_profile = profile
        self.completed = True

        # ---- Summary: input type + median area ---------------------------
        if profile.has_area:
            area_line = (f"Median contact area: {profile.median_area:.1f} px²\n"
                         f"Range seen: {profile.min:.1f} – {profile.max:.1f} px²")
        else:
            area_line = ("Contact area: not reported by this input\n"
                         "(identified by input type instead)")
        label = {"stylus": "Front tip", "eraser": "Back tip",
                 "palm": "Palm"}.get(self._config.kind, self._config.kind)
        self._instruction.setText(
            f"{label} calibrated ✓\n\n"
            f"Input type: {source_label}\n"
            f"{area_line}\n"
            f"Samples used: {profile.samples} of {len(self._samples)}"
        )
        self._capture_hint.setText("✓")
        self._done_btn.setEnabled(True)
        logger.info("Calibration (%s) computed: source=%s median=%.2f px2 %s",
                    self._config.kind, source, profile.median_area, profile)

    def _cross_check(self, profile) -> Optional[str]:
        """Reject a back-tip / palm pair that can't be told apart.

        Returns an explanation, or None if the pair is fine. Runs against the
        LIVE calibration so it works whichever of the two the user does first,
        and it compares medians because those are the actual thresholds the
        classifier will use.
        """
        cal = self._calibration
        if cal is None or self._config.kind not in ("eraser", "palm"):
            return None
        other = (cal.palm_profile if self._config.kind == "eraser"
                 else cal.eraser_profile)
        if not (other.calibrated and other.median_area > 0):
            return None
        mine = profile.median_area
        theirs = other.median_area
        back, palm = ((mine, theirs) if self._config.kind == "eraser"
                      else (theirs, mine))
        if abs(palm - back) < 1e-6:
            return (f"Your palm and the stylus back tip both measured "
                    f"{back:.1f} px² — exactly the same size, so there's no "
                    f"way to tell them apart. Your board may not report "
                    f"contact size accurately enough for palm detection.")
        if palm < back:
            return (f"Your palm measured SMALLER ({palm:.1f} px²) than the "
                    f"stylus back tip ({back:.1f} px²), which can't be right. "
                    f"Check you used the right one for each calibration.")
        return None