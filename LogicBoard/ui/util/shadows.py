"""
shadows.py — Fluent elevation system.

The Fluent Design system distinguishes four elevation tiers; each card or
floating panel sits at exactly one. The shadow PRESETS for those tiers are
sourced from `ThemeTokens.elev_*` so light/dark mode can use different
opacities (light mode shadows are subtler — Fluent spec).

Public API
──────────
    apply_shadow(widget, depth="rest")
        Attach the right shadow for the requested tier. Idempotent —
        replaces any prior shadow effect.

    elev_rest    : flat on surface (cards in their normal state)
    elev_hover   : lifted (cards under cursor, raised buttons)
    elev_active  : pressed / actively-being-manipulated
    elev_float   : floating panels / dialogs / menus

Legacy compatibility:
    "sm" → "rest",  "md" → "hover",  "lg" → "float"
"""
from __future__ import annotations

from typing import Literal

from PySide6.QtGui     import QColor
from PySide6.QtWidgets import QGraphicsDropShadowEffect, QWidget

from ui.theme.theme_manager import theme_mgr

__all__ = ["apply_shadow", "remove_shadow", "ShadowDepth", "ELEVATION_NAMES"]

ShadowDepth = Literal[
    "rest", "hover", "active", "float",
    "sm",   "md",    "lg",                  # legacy aliases
]

ELEVATION_NAMES = ("rest", "hover", "active", "float")

_LEGACY_ALIASES = {
    "sm": "rest",
    "md": "hover",
    "lg": "float",
}


def _preset_for(depth: ShadowDepth) -> tuple[int, int, int, int]:
    """Resolve a depth name to a (blur, dx, dy, alpha) tuple from active theme."""
    name = _LEGACY_ALIASES.get(depth, depth)
    t    = theme_mgr.tokens
    match name:
        case "rest":   return t.elev_rest
        case "hover":  return t.elev_hover
        case "active": return t.elev_active
        case "float":  return t.elev_float
        case _:
            return t.elev_rest


def apply_shadow(widget: QWidget, depth: ShadowDepth = "rest") -> QGraphicsDropShadowEffect:
    """Attach a Fluent-style drop shadow to `widget`.

    Idempotent — replaces any prior shadow effect. Returns the effect so
    callers can animate its `blurRadius` / `yOffset` on hover.
    """
    blur, dx, dy, alpha = _preset_for(depth)
    fx = QGraphicsDropShadowEffect(widget)
    fx.setBlurRadius(blur)
    fx.setOffset(dx, dy)
    fx.setColor(QColor(0, 0, 0, alpha))
    widget.setGraphicsEffect(fx)
    return fx


def remove_shadow(widget: QWidget) -> None:
    widget.setGraphicsEffect(None)