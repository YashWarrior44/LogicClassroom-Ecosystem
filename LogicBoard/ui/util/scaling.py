"""
scaling.py — Backwards-compatible shim over the central SizeManager.

Historically all UI sizing went through ``UiScale``. Sizing is now owned by
``ui.util.size_manager.SizeManager`` (resolution + DPI + Windows-scaling aware,
1920x1080 reference). ``UiScale`` is kept so the ~80 existing call sites keep
working unchanged, but every method simply delegates to the SizeManager — so
there is exactly ONE source of truth. New code should import ``size_mgr``
directly and use its richer API (scale_width / scale_height / scale_icon / ...).
"""
from __future__ import annotations

from ui.util.size_manager import size_mgr

__all__ = ["UiScale"]


class UiScale:
    """Thin compatibility facade. All logic lives in SizeManager."""

    # Mirror the reference constants for any code that read them.
    REF_HEIGHT = size_mgr.REF_HEIGHT
    REF_WIDTH = size_mgr.REF_WIDTH
    BOOST = size_mgr.BOOST

    @classmethod
    def screen_height(cls) -> int:
        size_mgr._ensure(); return size_mgr._logical_h

    @classmethod
    def screen_width(cls) -> int:
        size_mgr._ensure(); return size_mgr._logical_w

    @classmethod
    def invalidate_cache(cls) -> None:
        size_mgr.invalidate()

    @classmethod
    def factor(cls, height: int | None = None) -> float:
        return size_mgr.ui_scale()

    @classmethod
    def px(cls, value: float, height: int | None = None) -> int:
        return size_mgr.scale_size(value)

    @classmethod
    def px_f(cls, value: float, height: int | None = None) -> float:
        return size_mgr.scale_f(value)

    @classmethod
    def font_px(cls, ref_size: float, height: int | None = None) -> int:
        return size_mgr.scale_font(ref_size)