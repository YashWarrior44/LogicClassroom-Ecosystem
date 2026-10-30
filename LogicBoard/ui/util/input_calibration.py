"""
Input Calibration & Palm Handling — data layer (Phase 1).

This module owns:
  • The profile data classes (stylus / eraser / palm).
  • Statistics helpers (median, std-dev, min/max, outlier rejection) used by the
    calibration dialogs in Phase 2.
  • InputCalibration: the runtime configuration manager that loads/saves
    everything to ``settings.json``.

NOTHING here is wired to live touch events yet — that's Phase 3. This is purely
the data model + persistence so the rest of the app has a stable foundation.

Design notes
------------
- "touch_mode" is one of TOUCH_MODE_REJECTION / TOUCH_MODE_ERASER (mutually
  exclusive), replacing the old single palm_eraser boolean.
- Stylus / eraser profiles describe a small, fairly uniform contact area, so a
  median + std-dev + min/max is enough.
- Palm contact is irregular, so the palm profile additionally tracks an aspect
  ratio range. Classification (Phase 3) will use tolerance RANGES, not a single
  threshold.
- All dataclasses round-trip cleanly to/from plain dicts for JSON.
"""
from __future__ import annotations

import json
import logging
import math
import statistics
from dataclasses import dataclass, asdict, field
from pathlib import Path
from typing import List, Optional, Sequence

logger = logging.getLogger(__name__)

# Touch-mode constants (mutually exclusive).
TOUCH_MODE_REJECTION = "palm_rejection"
TOUCH_MODE_ERASER    = "palm_eraser"
# Palm handling disabled entirely: no contact is ever classified as a palm,
# so every touch draws like a finger. The escape hatch for boards whose
# contact geometry misleads the classifier — a big fingertip read as a palm
# means strokes silently vanish, and "turn it off" is a better answer than
# asking a teacher mid-lesson to recalibrate.
TOUCH_MODE_OFF       = "palm_off"
VALID_TOUCH_MODES    = (TOUCH_MODE_REJECTION, TOUCH_MODE_ERASER,
                        TOUCH_MODE_OFF)

# Canvas layout styles.
#   one_page    — the original: one page fills the viewport.
#   scroll_list — every page of the board stacked vertically, like the pages
#                 of a document, scrolled and zoomed as one list.
CANVAS_STYLE_ONE_PAGE    = "one_page"
CANVAS_STYLE_SCROLL_LIST = "scroll_list"
VALID_CANVAS_STYLES      = (CANVAS_STYLE_ONE_PAGE, CANVAS_STYLE_SCROLL_LIST)

# Bounds for the five-finger zoom strength. 1.0 is "one unit of finger
# spread is one unit of scale"; the band either side is what keeps a small
# hand movement from throwing the list across its whole zoom range.
ZOOM_SENSITIVITY_MIN = 0.25
ZOOM_SENSITIVITY_MAX = 3.0


# =============================================================================
# Statistics helpers (used by the calibration dialogs in Phase 2)
# =============================================================================
def reject_outliers(values: Sequence[float], n_std: float = 2.0) -> List[float]:
    """Return ``values`` with samples more than ``n_std`` standard deviations
    from the mean removed. With < 3 samples there's nothing meaningful to
    reject, so the input is returned unchanged."""
    vals = [float(v) for v in values if v is not None]
    if len(vals) < 3:
        return vals
    mean = statistics.fmean(vals)
    sd = statistics.pstdev(vals)
    if sd <= 0:
        return vals
    lo, hi = mean - n_std * sd, mean + n_std * sd
    kept = [v for v in vals if lo <= v <= hi]
    # Never throw everything away (e.g. tiny sample with high spread).
    return kept if kept else vals


def summarize(values: Sequence[float], n_std: float = 2.0) -> dict:
    """Compute robust summary statistics for a list of samples, after outlier
    rejection. Returns median / std_dev / min / max (all floats). Empty input
    yields zeros so callers never have to special-case it."""
    kept = reject_outliers(values, n_std=n_std)
    if not kept:
        return {"median": 0.0, "std_dev": 0.0, "min": 0.0, "max": 0.0}
    return {
        "median":  float(statistics.median(kept)),
        "std_dev": float(statistics.pstdev(kept)) if len(kept) > 1 else 0.0,
        "min":     float(min(kept)),
        "max":     float(max(kept)),
    }


# =============================================================================
# Profile data classes
# =============================================================================
# Input families a sample can arrive as. A profile records WHICH family the
# calibrated contact used, because on real hardware that is often a stronger
# identifier than size: an IR touch frame routes a thin stylus tip to the pen
# digitizer (SOURCE_TABLET) and a thick tip or finger to the touch digitizer
# (SOURCE_TOUCH), while reporting a near-constant token contact area for
# everything. On such a board the family separates the tips perfectly and the
# area separates nothing.
SOURCE_TOUCH  = "touch"
SOURCE_TABLET = "tablet"
SOURCE_MOUSE  = "mouse"

# Families that carry no contact-geometry at all. A profile captured from one
# of these is identified by its SOURCE alone; its area is meaningless and is
# deliberately stored as 0 rather than fabricated.
SOURCELESS_GEOMETRY = (SOURCE_TABLET, SOURCE_MOUSE)


def sources_of(samples: Sequence["TouchSample"]) -> List[str]:
    """Distinct input families present in a sample set, order-stable."""
    seen: List[str] = []
    for s in samples:
        src = getattr(s, "source", "") or ""
        if src and src not in seen:
            seen.append(src)
    return seen


@dataclass
class ContactProfile:
    """Stylus / eraser contact profile.

    Two ways a profile can identify its contact, and which one applies is
    decided by the hardware rather than by us:

      • ``source`` — the input family the calibration samples arrived as.
        Always recorded. For tablet/mouse families this is the ONLY usable
        identifier, since neither carries a contact ellipse.

      • ``median_area`` — used as a THRESHOLD, not as the centre of a band.
        The eraser (back tip) matches from its median up to the palm's
        median; see ``InputCalibration.classification_thresholds``. Median
        rather than mean because a single mis-tap shouldn't move it, and a
        threshold rather than a min/max window because a window has to guess
        how wide it should be — which is exactly the hardcoded-constant
        problem this replaces.
    """
    median_area: float = 0.0
    std_dev:     float = 0.0
    min:         float = 0.0
    max:         float = 0.0
    calibrated:  bool  = False
    source:      str   = ""      # SOURCE_TOUCH / SOURCE_TABLET / SOURCE_MOUSE
    samples:     int   = 0       # how many samples the median came from

    @property
    def has_area(self) -> bool:
        """Whether this profile carries usable contact geometry."""
        return self.median_area > 0.0

    def to_dict(self) -> dict:
        return asdict(self)

    @classmethod
    def from_dict(cls, d: Optional[dict]) -> "ContactProfile":
        d = d or {}
        return cls(
            median_area=float(d.get("median_area", 0.0)),
            std_dev=float(d.get("std_dev", 0.0)),
            min=float(d.get("min", 0.0)),
            max=float(d.get("max", 0.0)),
            calibrated=bool(d.get("calibrated", False)),
            # Older profiles predate source tracking. They were necessarily
            # captured from touch, since that was the only family the capture
            # area listened to.
            source=str(d.get("source", "") or SOURCE_TOUCH),
            samples=int(d.get("samples", 0)),
        )

    @classmethod
    def from_samples(cls, samples: Sequence["TouchSample"]) -> "ContactProfile":
        """Build from captured samples, recording the input family.

        Samples from a family with no contact geometry (tablet / mouse) yield
        a source-only profile: ``median_area`` stays 0 and identification
        falls to the family. We do NOT invent a plausible-looking area for
        them — a fabricated number would look calibrated while describing
        nothing the hardware actually reported.
        """
        srcs = sources_of(samples)
        source = srcs[0] if len(srcs) == 1 else ""
        areas = [s.area for s in samples if s.area > 0]
        if not areas:
            return cls(calibrated=bool(samples), source=source,
                       samples=len(samples))
        st = summarize(areas)
        return cls(median_area=st["median"], std_dev=st["std_dev"],
                   min=st["min"], max=st["max"], calibrated=True,
                   source=source, samples=len(areas))


@dataclass
class PalmProfile:
    """Palm contact profile — irregular, so we track an aspect-ratio range in
    addition to area statistics."""
    median_area:      float = 0.0
    std_dev:          float = 0.0
    min:              float = 0.0
    max:              float = 0.0
    aspect_ratio_min: float = 0.0
    aspect_ratio_max: float = 0.0
    calibrated:       bool  = False
    source:           str   = ""
    samples:          int   = 0

    @property
    def has_area(self) -> bool:
        return self.median_area > 0.0

    def to_dict(self) -> dict:
        return asdict(self)

    @classmethod
    def from_dict(cls, d: Optional[dict]) -> "PalmProfile":
        d = d or {}
        return cls(
            median_area=float(d.get("median_area", 0.0)),
            std_dev=float(d.get("std_dev", 0.0)),
            min=float(d.get("min", 0.0)),
            max=float(d.get("max", 0.0)),
            aspect_ratio_min=float(d.get("aspect_ratio_min", 0.0)),
            aspect_ratio_max=float(d.get("aspect_ratio_max", 0.0)),
            calibrated=bool(d.get("calibrated", False)),
            source=str(d.get("source", "") or SOURCE_TOUCH),
            samples=int(d.get("samples", 0)),
        )

    @classmethod
    def from_touch_samples(cls, samples: Sequence["TouchSample"]) -> "PalmProfile":
        """Build from captured samples, recording the input family."""
        srcs = sources_of(samples)
        source = srcs[0] if len(srcs) == 1 else ""
        areas  = [s.area for s in samples if s.area > 0]
        ratios = [s.aspect_ratio for s in samples if s.aspect_ratio > 0]
        prof = cls.from_samples(areas, ratios)
        prof.source = source
        prof.samples = len(areas)
        return prof

    @classmethod
    def from_samples(cls, areas: Sequence[float],
                     aspect_ratios: Sequence[float]) -> "PalmProfile":
        """Build a palm profile from raw area + aspect-ratio samples.

        Area stats use median + outlier rejection (robust to uneven palm
        placement). The aspect-ratio RANGE is taken from the min/max of the
        outlier-rejected ratios, giving a tolerance band rather than a single
        value (Phase 2 entry point).

        NEW: aspect ratios are folded to their orientation-invariant form
        (``max/min`` of the ellipse axes, always ``>= 1``) before the range
        is computed. Without this, a palm calibrated horizontally would
        have a range like ``[1.2, 1.6]`` that couldn't match the SAME palm
        rested vertically (``[0.63, 0.83]``). The classifier does the same
        normalization at match time, so profiles saved by earlier builds
        still work — but new profiles land pre-normalized which is easier
        to inspect."""
        a = summarize(areas)
        # Fold every ratio to ``>= 1`` before outlier rejection so the
        # kept set represents the same shape regardless of orientation.
        norm_ars = [
            (r if r >= 1.0 else (1.0 / r))
            for r in aspect_ratios if r and r > 0
        ]
        kept_ar = reject_outliers(norm_ars)
        ar_min = float(min(kept_ar)) if kept_ar else 0.0
        ar_max = float(max(kept_ar)) if kept_ar else 0.0
        return cls(
            median_area=a["median"], std_dev=a["std_dev"],
            min=a["min"], max=a["max"],
            aspect_ratio_min=ar_min, aspect_ratio_max=ar_max,
            calibrated=bool(areas),
        )


# =============================================================================
# Raw sample container (used while a calibration dialog is collecting taps)
# =============================================================================
@dataclass
class TouchSample:
    """One captured touch during calibration. Fields that the hardware/Qt
    can't provide are left as None and simply ignored by the statistics."""
    area:         float = 0.0          # contact area in px²
    width:        float = 0.0          # ellipse width  (px)
    height:       float = 0.0          # ellipse height (px)
    pressure:     Optional[float] = None
    orientation:  Optional[float] = None   # contact angle, if available
    touch_points: Optional[int]   = None   # simultaneous points, if available
    # Which input family delivered this sample. Recorded so a calibration can
    # (a) refuse a mixed set — if half your taps came in as tablet events and
    # half as touch, the hardware isn't treating that contact consistently and
    # the resulting profile would describe neither — and (b) identify contacts
    # whose family is the only thing that distinguishes them.
    source:       str = ""

    @property
    def aspect_ratio(self) -> float:
        """Orientation-invariant elongation of the contact patch.

        Returns ``max(w,h)/min(w,h)`` (always ``>= 1``) rather than the
        raw ``w/h``, so a palm calibrated in landscape orientation and
        the same palm captured in portrait yield the same value. See the
        matching property on ``TouchFeatures`` in ``touch_classifier``
        for the full rationale."""
        w, h = self.width, self.height
        if w <= 0 or h <= 0:
            return 0.0
        big = max(w, h); small = min(w, h)
        if small <= 0:
            return 0.0
        return big / small


# =============================================================================
# InputCalibration — runtime configuration manager + settings.json persistence
# =============================================================================
class InputCalibration:
    """Single source of truth for input settings. Holds the touch mode and the
    three profiles, and reads/writes ``settings.json``.

    Phase 1 scope: load at startup, expose getters/setters, persist on change.
    It is NOT consulted by the touch pipeline yet (Phase 3).
    """

    def __init__(self, settings_path: Path):
        self._path = Path(settings_path)
        self.touch_mode: str = TOUCH_MODE_REJECTION
        self.stylus_profile = ContactProfile()
        self.eraser_profile = ContactProfile()
        self.palm_profile   = PalmProfile()
        # Auto-save settings live in the same file so there's ONE settings.json.
        self.autosave_enabled: bool = True
        self.autosave_interval_min: int = 5
        # PPT import: render with per-click animation build states (slow,
        # experimental) vs one flat image per slide (default).
        self.ppt_animations: bool = False
        # ---- Canvas presentation -------------------------------------
        # How the board is laid out on screen. "one_page" is the original
        # behaviour and stays the default; "scroll_list" stacks every page
        # of the SAME board vertically like a document.
        self.canvas_style: str = CANVAS_STYLE_ONE_PAGE
        # How strongly the five-finger gesture rescales the page list.
        # Only consulted by the list style — one-page zoom is untouched by
        # it, which is the whole point of keeping it a separate value
        # rather than a multiplier on the shared view scale.
        self.zoom_sensitivity: float = 1.0
        # Dual Pen — persisted assignments for the two stylus tips. Defaults
        # match the most useful pairing: write with the front, select with
        # the back (mimics flipping a real pen to "select something").
        self.dual_pen_front: str = "pen"
        self.dual_pen_back:  str = "select"
        # Eraser sizes are PER MODE — pixel and object each remember their
        # own. Default 50 matches the pre-split behaviour.
        self.eraser_pixel_size:  float = 50.0
        self.eraser_object_size: float = 50.0
        # Some smart boards never report the stylus FRONT tip as a touch
        # contact at all. The tip is a passive plastic point, and on those
        # panels it is too small for the touch frame to register, so the
        # driver falls back to plain mouse emulation — the front tip
        # arrives as a left click while the finger and the (much wider)
        # back tip come through as genuine touch. Calibration then refuses
        # the front tip, correctly, as "that was a mouse".
        #
        # With this on, a left click IS the stylus front tip: calibration
        # accepts it instead of warning, and the classifier stops trying to
        # size-match a contact that the hardware never reports.
        self.mouse_is_stylus_front: bool = False
        # A palm size the teacher set by hand, as a contact DIAMETER in px.
        # 0 means "automatic": the calibrated palm, or the built-in
        # default when there is none. Set, it replaces the calibrated
        # threshold; calibrating the palm again clears it, so the newest
        # measurement — hand-set or measured — is always the one in force.
        self.palm_size_px: float = 0.0

    # ---- touch mode ----
    def set_touch_mode(self, mode: str) -> None:
        if mode not in VALID_TOUCH_MODES:
            logger.warning("Ignoring invalid touch_mode %r", mode)
            return
        self.touch_mode = mode
        self.save()

    @property
    def palm_rejection(self) -> bool:
        return self.touch_mode == TOUCH_MODE_REJECTION

    @property
    def palm_eraser(self) -> bool:
        return self.touch_mode == TOUCH_MODE_ERASER

    # ---- profile setters (called by Phase 2 dialogs) ----
    def set_stylus_profile(self, profile: ContactProfile) -> None:
        self.stylus_profile = profile
        self.save()

    def set_eraser_profile(self, profile: ContactProfile) -> None:
        self.eraser_profile = profile
        self.save()

    def set_palm_profile(self, profile: PalmProfile) -> None:
        self.palm_profile = profile
        # A fresh measurement supersedes a hand-set size.
        self.palm_size_px = 0.0
        self.save()

    # ---- manual palm size ----
    PALM_SIZE_MIN = 30.0
    PALM_SIZE_MAX = 240.0
    PALM_SIZE_DEFAULT = 80.0      # the legacy heuristic's width

    def set_palm_size(self, diameter_px: float) -> None:
        """Hand-set the palm size (contact diameter, px); 0 = automatic."""
        try:
            d = float(diameter_px)
        except (TypeError, ValueError):
            return
        d = 0.0 if d <= 0 else max(self.PALM_SIZE_MIN,
                                   min(self.PALM_SIZE_MAX, d))
        if d != self.palm_size_px:
            self.palm_size_px = d
            self.save()

    @property
    def palm_size_is_manual(self) -> bool:
        return self.palm_size_px > 0

    @property
    def effective_palm_diameter(self) -> float:
        """The palm size in force right now, as a contact diameter in px:
        the hand-set one, else the calibrated median, else the default.
        Contact area is pi * (w/2) * (h/2), so a circle of diameter d has
        area pi * d^2 / 4 and d = 2 * sqrt(area / pi)."""
        if self.palm_size_px > 0:
            return self.palm_size_px
        p = self.palm_profile
        if p.calibrated and p.median_area > 0:
            return 2.0 * math.sqrt(p.median_area / math.pi)
        return self.PALM_SIZE_DEFAULT

    # ---- auto-save passthrough (kept in the same file) ----
    def set_autosave(self, enabled: bool, interval_min: Optional[int] = None) -> None:
        self.autosave_enabled = bool(enabled)
        if interval_min is not None:
            self.autosave_interval_min = int(interval_min)
        self.save()

    # ---- PPT animation import toggle ----
    def set_canvas_style(self, style: str) -> None:
        """Persist the canvas layout. Unknown values fall back to one-page
        rather than leaving the canvas in a mode nothing renders."""
        style = style if style in VALID_CANVAS_STYLES else CANVAS_STYLE_ONE_PAGE
        if style != self.canvas_style:
            self.canvas_style = style
            self.save()

    def set_zoom_sensitivity(self, value: float) -> None:
        """Persist the five-finger zoom strength, clamped to a usable band.

        The clamp is not decoration: this multiplies a per-sample scale
        delta, so an out-of-range value doesn't make zooming 'very fast', it
        makes one twitch of a hand jump the list from unreadable to a single
        page filling the screen.
        """
        value = float(max(ZOOM_SENSITIVITY_MIN,
                          min(ZOOM_SENSITIVITY_MAX, float(value))))
        if abs(value - self.zoom_sensitivity) > 1e-6:
            self.zoom_sensitivity = value
            self.save()

    def set_ppt_animations(self, enabled: bool) -> None:
        self.ppt_animations = bool(enabled)
        self.save()

    # ---- Mouse-as-front-tip fallback ----
    def set_mouse_is_stylus_front(self, enabled: bool) -> None:
        """Treat a plain left click as the stylus FRONT tip.

        For boards whose touch frame never sees the front tip and emits
        mouse events for it instead. See the attribute's comment in
        ``__init__`` for the full picture.
        """
        self.mouse_is_stylus_front = bool(enabled)
        self.save()

    # ---- Classification thresholds (derived, never hardcoded) ----
    @property
    def classification_thresholds(self) -> dict:
        """The live size thresholds, straight from the calibrated medians.

        Returns ``{"back_tip": float|None, "palm": float|None}``.

        The model, deliberately threshold-based rather than band-based:

            area  <  back_tip            -> front tip / finger
            back_tip <= area <  palm     -> stylus BACK TIP
            area >= palm                 -> PALM

        so the back tip occupies exactly the range between the two calibrated
        medians. A band would need a width, and any width we picked would be a
        made-up constant — the whole thing this replaces. A threshold needs
        nothing beyond the data.

        Either value is None when that profile isn't calibrated or carries no
        geometry, in which case the corresponding rule simply doesn't apply.
        """
        def thr(p):
            return p.median_area if (p.calibrated and p.median_area > 0) else None
        palm = thr(self.palm_profile)
        if self.palm_size_px > 0:
            # The hand-set size, as the same area measure the frame reports.
            palm = math.pi * (self.palm_size_px / 2.0) ** 2
        return {"back_tip": thr(self.eraser_profile),
                "palm":     palm}

    def threshold_conflict(self) -> Optional[str]:
        """Human-readable reason the calibrated thresholds are unusable, or
        None when they're fine.

        The back tip and the palm are separated ONLY by their medians, so if
        those medians coincide — or invert — there is no range left for the
        back tip and no way to tell the two apart. That happens for real: on a
        frame that reports a near-constant token contact area, a palm and a
        fingertip measure the same, and a calibration that looks successful
        would silently classify every palm as a back tip.
        """
        th = self.classification_thresholds
        back, palm = th["back_tip"], th["palm"]
        if back is None or palm is None:
            return None                      # not both calibrated yet
        if palm > back:
            return None                      # healthy
        if abs(palm - back) < 1e-6:
            return ("The back tip and your palm measured the SAME contact "
                    "area, so they can't be told apart.")
        return ("Your palm measured SMALLER than the stylus back tip, which "
                "can't be right — the two calibrations look swapped.")

    # ---- Dual Pen availability ----
    @property
    def front_tip_known(self) -> bool:
        """Whether the app can identify a front-tip contact.

        Normally that means the stylus profile was calibrated. But on a board
        whose touch frame never reports the front tip at all — it arrives as a
        plain left click instead — calibration is impossible by construction:
        there is no touch contact to measure, so the user could sit through
        the wizard forever and never register one. ``mouse_is_stylus_front``
        is the user telling us "a left click IS the front tip", which supplies
        exactly the same fact by a different route, and the input handler
        already acts on it (see InputHandler.mouse_press). Treat it as
        equivalent here so the Dual Pen gate doesn't lock out the very
        hardware that setting exists for.
        """
        return bool(self.stylus_profile.calibrated or self.mouse_is_stylus_front)

    @property
    def dual_pen_available(self) -> bool:
        """Whether Dual Pen may be offered at all.

        BOTH tips must be calibrated. The whole feature is "tell the front tip
        apart from the back tip and run a different tool for each" — that is a
        comparison between two profiles, so one profile on its own decides
        nothing. Calibrating only the back tip (easy to do by accident: on a
        board that won't register a large contact you end up registering the
        back tip and the palm with a finger) previously still left Dual Pen
        selectable, and every stroke then resolved through a front profile
        that was never measured.

        Single source of truth on purpose — the Pen panel greys out its tile
        from this, and main_window refuses the mode switch from it, so the UI
        and the behaviour can't drift apart.

        Deliberately ``eraser_profile.calibrated`` and NOT
        ``classification_thresholds["back_tip"] is not None``, even though
        those two come apart: a profile can be flagged calibrated and still
        carry ``median_area == 0``, which is what a profile measured from a
        family with no contact ellipse looks like (the mouse route does it by
        construction, and so does an IR frame that reports no ellipse). The
        threshold is None there, so the classifier can never answer "eraser".

        That state used to break the back tip completely — it fell through to
        the legacy size heuristic, which calls any big blunt contact a palm,
        so a back tip assigned to Pen erased (palm_eraser) or did nothing
        (palm_rejection). The fix for that lives where the damage was, in
        ``InputHandler._is_dual_pen_tip_contact``: the contact that OPENS a
        Dual Pen stroke is a stylus tip whatever its size says, so the back
        tip is now identified on exactly the boards whose frames cannot
        measure it. Tightening this gate instead would have answered the same
        bug by withdrawing the feature from the hardware that needs it most.
        """
        return bool(self.front_tip_known and self.eraser_profile.calibrated)

    # ---- Dual Pen tip assignments ----
    def set_dual_pen(self, front: Optional[str] = None,
                     back:  Optional[str] = None) -> None:
        """Persist which logical tool each stylus tip uses in Dual Pen mode.
        Each argument is independent; pass only the side you want to change."""
        VALID = {"pen", "highlighter", "brush", "select", "eraser"}
        changed = False
        if front in VALID:
            self.dual_pen_front = front; changed = True
        if back in VALID:
            self.dual_pen_back = back; changed = True
        if changed:
            self.save()

    # ---- Eraser sizes (independent per mode) ----
    def set_eraser_size(self, mode: str, size: float) -> None:
        """Persist the eraser size for a SPECIFIC mode without touching the
        other. ``mode`` is ``"pixel"`` or ``"object"``."""
        size = float(max(5.0, min(500.0, size)))
        if mode == "pixel":
            if self.eraser_pixel_size != size:
                self.eraser_pixel_size = size; self.save()
        elif mode == "object":
            if self.eraser_object_size != size:
                self.eraser_object_size = size; self.save()

    # ---- persistence ----
    def to_dict(self) -> dict:
        return {
            "touch_mode":     self.touch_mode,
            "stylus_profile": self.stylus_profile.to_dict(),
            "eraser_profile": self.eraser_profile.to_dict(),
            "palm_profile":   self.palm_profile.to_dict(),
            "autosave_enabled":      self.autosave_enabled,
            "autosave_interval_min": self.autosave_interval_min,
            "ppt_animations":        self.ppt_animations,
            "canvas_style":          self.canvas_style,
            "zoom_sensitivity":      self.zoom_sensitivity,
            "dual_pen_front":        self.dual_pen_front,
            "dual_pen_back":         self.dual_pen_back,
            "eraser_pixel_size":     self.eraser_pixel_size,
            "eraser_object_size":    self.eraser_object_size,
            "mouse_is_stylus_front": self.mouse_is_stylus_front,
            "palm_size_px":          self.palm_size_px,
        }

    def load(self) -> None:
        """Load from settings.json. Missing file or bad JSON → keep defaults
        (and a fresh file is written on the next save)."""
        try:
            if not self._path.exists():
                logger.info("No settings.json yet; using defaults.")
                return
            data = json.loads(self._path.read_text(encoding="utf-8"))
        except Exception as e:
            logger.warning("Could not read settings.json (%s); using defaults.", e)
            return
        mode = data.get("touch_mode", TOUCH_MODE_REJECTION)
        self.touch_mode = mode if mode in VALID_TOUCH_MODES else TOUCH_MODE_REJECTION
        self.stylus_profile = ContactProfile.from_dict(data.get("stylus_profile"))
        self.eraser_profile = ContactProfile.from_dict(data.get("eraser_profile"))
        self.palm_profile   = PalmProfile.from_dict(data.get("palm_profile"))
        self.autosave_enabled = bool(data.get("autosave_enabled", True))
        try:
            iv = int(data.get("autosave_interval_min", 5))
        except (TypeError, ValueError):
            iv = 5
        self.autosave_interval_min = iv if iv in (1, 5, 10) else 5
        self.ppt_animations = bool(data.get("ppt_animations", False))
        cs = data.get("canvas_style", CANVAS_STYLE_ONE_PAGE)
        self.canvas_style = cs if cs in VALID_CANVAS_STYLES else CANVAS_STYLE_ONE_PAGE
        try:
            zs = float(data.get("zoom_sensitivity", 1.0))
        except (TypeError, ValueError):
            zs = 1.0
        self.zoom_sensitivity = max(ZOOM_SENSITIVITY_MIN,
                                    min(ZOOM_SENSITIVITY_MAX, zs))
        self.mouse_is_stylus_front = bool(
            data.get("mouse_is_stylus_front", False))
        try:
            ps = float(data.get("palm_size_px", 0.0))
        except (TypeError, ValueError):
            ps = 0.0
        self.palm_size_px = (0.0 if ps <= 0 else
                             max(self.PALM_SIZE_MIN, min(self.PALM_SIZE_MAX, ps)))
        # Dual Pen — validate against the same whitelist used by setters; an
        # unrecognised string falls back to the safe default rather than
        # crashing the input handler later.
        VALID_TIP = {"pen", "highlighter", "brush", "select", "eraser"}
        f = data.get("dual_pen_front", "pen")
        b = data.get("dual_pen_back",  "select")
        self.dual_pen_front = f if f in VALID_TIP else "pen"
        self.dual_pen_back  = b if b in VALID_TIP else "select"
        # Eraser sizes — same clamp as the canvas setter (5..500). Bad JSON
        # values (None, strings, etc.) fall back to the safe default 50.
        def _clamp_eraser(v):
            try:    fv = float(v)
            except (TypeError, ValueError): return 50.0
            return max(5.0, min(500.0, fv))
        self.eraser_pixel_size  = _clamp_eraser(data.get("eraser_pixel_size",  50.0))
        self.eraser_object_size = _clamp_eraser(data.get("eraser_object_size", 50.0))
        logger.info("Loaded input settings: touch_mode=%s", self.touch_mode)

    def save(self) -> None:
        """Atomically write settings.json (temp file + replace) so a crash
        mid-write can't corrupt the user's settings.

        MERGE-SAFE: settings.json is shared with other subsystems (notably the
        LogicShare config under the "logicshare" key). We load the existing
        file first and update ONLY our own keys, so we never clobber keys we
        don't own."""
        try:
            self._path.parent.mkdir(parents=True, exist_ok=True)
            # Preserve any keys we don't own (e.g. "logicshare").
            existing = {}
            try:
                if self._path.exists():
                    loaded = json.loads(self._path.read_text(encoding="utf-8"))
                    if isinstance(loaded, dict):
                        existing = loaded
            except Exception as e:
                logger.warning("Could not read settings.json before save (%s); "
                               "writing fresh.", e)
            existing.update(self.to_dict())
            tmp = self._path.with_suffix(self._path.suffix + ".tmp")
            tmp.write_text(json.dumps(existing, indent=2), encoding="utf-8")
            tmp.replace(self._path)
        except Exception as e:
            logger.error("Failed to save settings.json: %s", e)