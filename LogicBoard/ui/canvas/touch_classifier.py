"""
Input Calibration — runtime touch classifier (Phase 3).

Given a live touch's geometry, decide whether it's a STYLUS, ERASER, or PALM
contact, using the calibrated profiles from Phase 1/2.

Design principles
-----------------
- **Thresholds, from the data.** The back tip and the palm each contribute one
  number — the MEDIAN contact area measured during their calibration — and
  those two numbers partition the size axis:

      area < back_tip           -> stylus (front tip / finger)
      back_tip <= area < palm   -> eraser (stylus back tip)
      area >= palm              -> palm

  Nothing in this module supplies a magic width, pad or margin. An earlier
  design scored each profile with a confidence, comparing against [min, max]
  bands widened by a ``pad_frac`` fudge factor and arbitrating with
  ``_MIN_CONFIDENCE`` / ``_PALM_MARGIN``. That meant how far a contact could
  stray and still match was decided in this file rather than by the hardware,
  and the numbers had to be re-guessed for every board.

- **Stylus priority is structural, not a tiebreak.** The stylus occupies the
  range BELOW the smallest measured threshold, so a contact is only ever
  promoted to eraser or palm by genuinely exceeding a measured size. A stylus
  can no longer be mistaken for a palm by a scoring accident — which mattered,
  because that mistake erases the user's work mid-stroke.

- **Never guess.** A contact the frame reports no ellipse for returns
  ``unknown`` rather than a best effort.

- Degrade gracefully: an uncalibrated profile simply contributes no threshold.
  If NOTHING is calibrated, fall back to the legacy size heuristic so behaviour
  never regresses from the pre-calibration build.

This module is pure logic — no Qt, no I/O — so it's cheap to call on every
touch point and trivial to unit-test.
"""
from __future__ import annotations

import logging
from dataclasses import dataclass
from typing import Optional

from ui.util.input_calibration import (
    InputCalibration, ContactProfile, PalmProfile,
    TOUCH_MODE_REJECTION, TOUCH_MODE_ERASER,
)

logger = logging.getLogger(__name__)

# Classification results.
CLASS_STYLUS = "stylus"
CLASS_ERASER = "eraser"
CLASS_PALM   = "palm"
CLASS_UNKNOWN = "unknown"

# NOTE: the old _MIN_CONFIDENCE / _PALM_MARGIN / pad_frac constants that used
# to live here are GONE. They decided how far a contact could stray from a
# calibrated band and still match — i.e. the tolerance came from numbers picked
# in this file rather than from the hardware. Classification is now a straight
# comparison against calibrated medians; see TouchClassifier.classify.


@dataclass
class TouchFeatures:
    """The geometry of a single live touch, extracted by the caller from a
    QEventPoint (kept Qt-free here so this module stays pure)."""
    area: float
    width: float
    height: float

    @property
    def aspect_ratio(self) -> float:
        if self.height and self.height > 0:
            return self.width / self.height
        return 0.0


class TouchClassifier:
    """Classifies live touches using the calibrated profiles in an
    InputCalibration instance. Cheap, allocation-free per call."""

    def __init__(self, calibration: InputCalibration):
        self._cal = calibration
        # Legacy fallback threshold (matches the old _is_palm heuristic) used
        # only when no palm profile is calibrated.
        self.legacy_palm_min_size: float = 80.0

    # ---- main entry point ----
    def classify(self, feat: TouchFeatures) -> tuple[str, float]:
        """Return (class, confidence). class ∈ stylus/eraser/palm/unknown.

        THRESHOLD MODEL (replaces the old band + confidence scoring):

            area  <  back_tip           -> stylus (front tip / finger)
            back_tip <= area <  palm    -> eraser (stylus BACK TIP)
            area >= palm                -> palm

        Both thresholds are the calibrated MEDIANS, taken live from
        ``InputCalibration.classification_thresholds``. No constant in this
        module contributes to the decision — the old code compared against
        [min, max] bands widened by a ``pad_frac`` fudge factor, so how far a
        contact could stray and still match came from a magic number rather
        than from the hardware.

        Stylus priority is preserved structurally: the smallest range is the
        stylus one, so a contact can only be promoted to eraser or palm by
        being genuinely bigger than a measured threshold. A stylus can never
        be mistaken for a palm by a scoring accident.
        """
        cal = self._cal
        th = cal.classification_thresholds
        back, palm = th["back_tip"], th["palm"]

        # Nothing usable calibrated -> legacy heuristic, unchanged, so a board
        # that was never calibrated behaves exactly as it did before.
        if back is None and palm is None:
            return self._legacy_classify(feat)

        # A contact the frame reported no ellipse for tells us nothing about
        # size. Refusing to guess is important: guessing "palm" here would
        # erase the user's work.
        if feat.area <= 0:
            return CLASS_UNKNOWN, 0.0

        # Broken calibration (palm <= back tip) leaves no range for the back
        # tip. Fall back to palm-only rather than acting on a contradiction.
        if back is not None and palm is not None and palm <= back:
            back = None

        if palm is not None and feat.area >= palm:
            return CLASS_PALM, self._confidence(feat.area, palm)
        if back is not None and feat.area >= back:
            return CLASS_ERASER, self._confidence(feat.area, back)
        return CLASS_STYLUS, 1.0

    @staticmethod
    def _confidence(area: float, threshold: float) -> float:
        """How far past the threshold the contact is, saturating at 2x.

        Reported for the debug overlay and logging only — no decision in this
        class depends on it, which is the point: the verdict is a comparison,
        not a score that could tip the wrong way."""
        if threshold <= 0:
            return 1.0
        return max(0.0, min(1.0, (area - threshold) / threshold + 0.5))

    def is_palm(self, feat: TouchFeatures) -> bool:
        """Convenience: should this touch be treated as a palm?

        Honors the touch mode — palm handling only applies when a palm-related
        mode is active. (Both current modes care about palms: rejection ignores
        them, eraser erases with them; the *caller* decides what to do with a
        palm verdict. This just answers 'is it a palm'.)"""
        cls, _conf = self.classify(feat)
        return cls == CLASS_PALM

    # ---- legacy fallback (no calibration yet) ----
    def _legacy_classify(self, feat: TouchFeatures) -> tuple[str, float]:
        # A hand-set palm size counts with no calibration too.
        manual = getattr(self._cal, "palm_size_px", 0.0) or 0.0
        ps = manual if manual > 0 else self.legacy_palm_min_size
        w, h = feat.width, feat.height
        if w <= 0 and h <= 0:
            return CLASS_UNKNOWN, 0.0
        if w >= ps or h >= ps or (w * h) >= ps * ps * 0.55:
            return CLASS_PALM, 0.5
        return CLASS_STYLUS, 0.5