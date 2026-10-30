"""
size_manager.py — Centralised responsive sizing for LogicBoard.

THE single source of truth for every UI dimension. The design reference is
1920x1080; the manager measures the live display and derives proportional scale
factors so the interface keeps the SAME visual proportions on 1080p, 1440p, 4K
and large smart boards, at any Windows display-scaling / DPI setting.

How it fits the app's DPI model
-------------------------------
LogicBoard runs Qt with *PassThrough* high-DPI rounding, so Qt already
multiplies logical pixels by the monitor's device-pixel-ratio (the Windows
125 / 150 / 200 % setting). Therefore the helpers here return *logical* pixels
scaled only by the RESOLUTION factor; Qt layers DPI on top. Net effect:
identical physical proportions across resolutions AND scaling settings, with no
double-scaling.

Reference invariant
-------------------
At exactly 1920x1080 every factor equals ``BOOST`` (1.14), which is the boost
the current UI was authored with — so 1080p stays pixel-identical after this
migration. Larger screens scale up proportionally, smaller ones down, both
clamped so nothing becomes unusable.

Usage
-----
    from ui.util.size_manager import size_mgr as S
    btn.setFixedHeight(S.scale_height(48))
    icon = tool_icon("pen", S.scale_icon(28))
    layout.setSpacing(S.scale_spacing(12))
    f.setPixelSize(S.scale_font(14))
"""
from __future__ import annotations

from PySide6.QtWidgets import QApplication
from PySide6.QtGui import QScreen

__all__ = ["SizeManager", "size_mgr"]


class SizeManager:
    # ---- design reference ----
    REF_WIDTH = 1920
    REF_HEIGHT = 1080

    # Touch-friendliness boost applied at the reference resolution. Keeps the
    # current 1080p layout pixel-identical (the UI was authored with it).
    BOOST = 1.14

    # Clamp so tiny / huge panels never become unusable. Generous upper bound
    # so true 4K and big smart boards scale up instead of being capped.
    MIN_SCALE = 0.85
    MAX_SCALE = 3.00

    # ---- detected (lazy, cached) ----
    _ready: bool = False
    _logical_w: int = REF_WIDTH
    _logical_h: int = REF_HEIGHT
    _dpr: float = 1.0
    _dpi: float = 96.0
    _w_scale: float = BOOST
    _h_scale: float = BOOST
    _ui_scale: float = BOOST
    _font_scale: float = BOOST

    # ------------------------------------------------------------- internal
    @classmethod
    def _clamp(cls, v: float) -> float:
        return max(cls.MIN_SCALE, min(cls.MAX_SCALE, v))

    @classmethod
    def _ensure(cls) -> None:
        if not cls._ready:
            cls.refresh()

    # ------------------------------------------------------------- detection
    @classmethod
    def refresh(cls) -> None:
        """(Re)measure the primary screen and recompute every factor. Call once
        after the QApplication exists and again on a screen / DPI change."""
        app = QApplication.instance()
        scr: QScreen | None = app.primaryScreen() if app is not None else None
        if scr is not None:
            geo = scr.geometry()                  # logical px (Qt already / DPR)
            cls._logical_w = max(1, geo.width())
            cls._logical_h = max(1, geo.height())
            try:    cls._dpr = float(scr.devicePixelRatio())
            except Exception: cls._dpr = 1.0
            try:    cls._dpi = float(scr.logicalDotsPerInch())
            except Exception: cls._dpi = 96.0
        else:
            cls._logical_w, cls._logical_h = cls.REF_WIDTH, cls.REF_HEIGHT
            cls._dpr, cls._dpi = 1.0, 96.0

        cls._w_scale = cls._clamp((cls._logical_w / cls.REF_WIDTH) * cls.BOOST)
        cls._h_scale = cls._clamp((cls._logical_h / cls.REF_HEIGHT) * cls.BOOST)
        # Recommended UNIFORM scale = the tighter axis. Using the smaller of the
        # two prevents overflow / clipping in the constrained dimension; on the
        # usual 16:9 panels both axes match, so it equals the height factor.
        cls._ui_scale = cls._clamp(min(cls._w_scale, cls._h_scale))
        # Fonts grow a touch more gently than geometry (matches the previous
        # UiScale.font_px curve so text stays identical at 1080p).
        cls._font_scale = 0.90 + (cls._ui_scale - 0.90) * 0.85
        cls._ready = True

    @classmethod
    def invalidate(cls) -> None:
        """Force a re-measure on next use (e.g. after moving monitors)."""
        cls._ready = False

    # ------------------------------------------------------------- factors
    @classmethod
    def ui_scale(cls) -> float:     cls._ensure(); return cls._ui_scale
    @classmethod
    def width_scale(cls) -> float:  cls._ensure(); return cls._w_scale
    @classmethod
    def height_scale(cls) -> float: cls._ensure(); return cls._h_scale
    @classmethod
    def font_scale(cls) -> float:   cls._ensure(); return cls._font_scale
    @classmethod
    def dpi_scale(cls) -> float:    cls._ensure(); return cls._dpr  # applied by Qt
    @classmethod
    def dpi(cls) -> float:          cls._ensure(); return cls._dpi

    @classmethod
    def info(cls) -> dict:
        """Diagnostic snapshot (logged at startup)."""
        cls._ensure()
        return {
            "logical_resolution": (cls._logical_w, cls._logical_h),
            "device_pixel_ratio": round(cls._dpr, 3),
            "dpi": round(cls._dpi, 1),
            "width_scale": round(cls._w_scale, 3),
            "height_scale": round(cls._h_scale, 3),
            "ui_scale": round(cls._ui_scale, 3),
            "font_scale": round(cls._font_scale, 3),
        }

    # ------------------------------------------------------------- helpers
    # Uniform (square things: icons, radii, spacing, generic sizes).
    @classmethod
    def scale_size(cls, v: float) -> int:
        cls._ensure(); return int(round(v * cls._ui_scale))

    # Dimension-specific (use when width vs height should track its own axis).
    @classmethod
    def scale_width(cls, v: float) -> int:
        cls._ensure(); return int(round(v * cls._w_scale))

    @classmethod
    def scale_height(cls, v: float) -> int:
        cls._ensure(); return int(round(v * cls._h_scale))

    @classmethod
    def scale_font(cls, v: float) -> int:
        cls._ensure(); return max(8, int(round(v * cls._font_scale)))

    @classmethod
    def scale_icon(cls, v: float) -> int:
        cls._ensure(); return int(round(v * cls._ui_scale))

    @classmethod
    def scale_spacing(cls, v: float) -> int:
        cls._ensure(); return int(round(v * cls._ui_scale))

    @classmethod
    def scale_margin(cls, v: float) -> int:
        cls._ensure(); return int(round(v * cls._ui_scale))

    @classmethod
    def scale_radius(cls, v: float) -> int:
        cls._ensure(); return int(round(v * cls._ui_scale))

    @classmethod
    def scale_f(cls, v: float) -> float:
        """Float result for sub-pixel precision (shadow blur, pen widths)."""
        cls._ensure(); return v * cls._ui_scale

    # ---- literal-pixel scaler -------------------------------------------
    # ``scale_size`` & friends treat their argument as a DESIGN UNIT and apply
    # the touch BOOST (so a design unit of 20 -> 23 px at 1080p, matching the
    # legacy UiScale). Hand-tuned QSS literals, however, are ALREADY actual
    # 1080p pixels, so they must NOT get the boost again. ``scale_px`` scales
    # such a literal by the pure resolution ratio (ui_scale / BOOST): it is
    # *exactly* identity at 1920x1080 and ~2x at 4K. Use it when migrating a
    # raw ``NNpx`` value out of a stylesheet.
    @classmethod
    def scale_px(cls, v: float) -> int:
        cls._ensure()
        return int(round(v * (cls._ui_scale / cls.BOOST)))


# Importable singleton-style handle (it's all classmethods, so the class IS the
# instance — `size_mgr.scale_size(8)` reads naturally).
size_mgr = SizeManager
