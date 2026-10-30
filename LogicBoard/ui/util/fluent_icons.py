"""
fluent_icons.py — Windows 11 Segoe Fluent Icons (with emoji fallback).

Segoe Fluent Icons is the official Fluent icon font shipped with Windows 11.
Icons are Unicode private-use code points rendered with that font. On older
Windows we fall back to Segoe MDL2 Assets (same approach, Win10/8). On other
platforms we render the equivalent emoji with the default font.

Usage
-----
    from ui.util.fluent_icons import icon_char, icon_font_family
    char  = icon_char("pen")          # e.g. "\\uED63" on Win11, "🖊️" elsewhere
    fam   = icon_font_family()        # "Segoe Fluent Icons" or "" (default font)
"""
from __future__ import annotations

import sys
from PySide6.QtGui import QFontDatabase

__all__ = ["icon_char", "icon_font_family", "ICONS"]


# Segoe Fluent Icons / MDL2 Assets share the same private-use codepoint
# ranges. These code points are stable across both fonts.
_FLUENT: dict[str, str] = {
    # Toolbar
    "select":       "\uE7C9",   # TouchPointer
    "shapes":       "\uF158",   # Shapes
    "pen":          "\uED63",   # EditMirrored
    "highlighter":  "\uE929",   # Highlight
    # Brush — no calligraphy glyph in Segoe Fluent either. The real identity
    # is assets/icons/brush.svg, which icon_loader resolves first; this is
    # only what shows if the SVG is missing.
    "brush":        "\uE70F",   # Edit (a nib)
    # Dual Pen — Segoe Fluent has no dedicated dual-tip glyph, so we reuse the
    # standard pen glyph as the Fluent-font fallback. The real visual identity
    # is carried by assets/icons/dual_pen.svg (resolved first by icon_loader).
    "dual_pen":     "\uED63",   # EditMirrored (visual reuse of "pen")
    "eraser":       "\uED60",   # EraseTool
    "imports":      "\uE898",   # Import
    "media":        "\uEB9F",   # Photo / media library
    "undo":         "\uE7A7",   # Undo
    "redo":         "\uE7A6",   # Redo
    "file":         "\uED25",   # FolderOpen
    "settings":     "\uE713",   # Settings

    # Selection menu / context
    "delete":       "\uE74D",   # Delete
    "color":        "\uE790",   # Color
    "prev":         "\uE76B",   # ChevronLeft
    "next":         "\uE76C",   # ChevronRight

    # Pages panel
    "add":          "\uE710",   # Add
    "insert_above": "\uE70E",   # ChevronUp
    "insert_below": "\uE70D",   # ChevronDown
    "touch":        "\uE7C9",   # TouchPointer

    # Shapes
    "line":         "\uE9E2",   # Line (Fluent)
    "rect":         "\uE003",   # Rectangle (fallback to box)
    "ellipse":      "\uEA3A",   # Ellipse
    "circle":       "\uEA3A",   # Same glyph; "Circle" is conceptual only
    "arrow":        "\uE72A",   # Forward / arrow

    # Imports
    "image":        "\uEB9F",   # Picture
    "pdf":          "\uEA90",   # PDF (best-match)
    "ppt":          "\uE8B5",   # Slideshow

    # Eraser modes
    "eraser_pixel": "\uED60",
    "eraser_obj":   "\uE74D",

    # Misc
    "play":         "\uE768",
    "pause":        "\uE769",
    "check":        "\uE73E",
    "close":        "\uE711",
    "more":         "\uE712",
}

# Pretty emoji fallback that reads well on every OS.
_EMOJI: dict[str, str] = {
    "select":       "👆",
    "insert_above": "⬆️",
    "insert_below": "⬇️",
    "touch":        "👆",
    "shapes":       "⬜",
    "pen":          "🖊️",
    "highlighter":  "🖍️",
    "brush":        "🖌",   # paintbrush
    "dual_pen":     "🖊️",
    "eraser":       "🧽",
    "imports":      "📥",
    "media":        "🖼️",
    "undo":         "↩️",
    "redo":         "↪️",
    "file":         "📂",
    "settings":     "⚙️",
    "delete":       "🗑️",
    "color":        "🎨",
    "prev":         "←",
    "next":         "→",
    "add":          "+",
    "line":         "╱",
    "rect":         "▭",
    "ellipse":      "⬭",
    "circle":       "⭕",
    "arrow":        "➜",
    "image":        "🖼️",
    "pdf":          "📄",
    "ppt":          "📽️",
    "eraser_pixel": "🖌️",
    "eraser_obj":   "🧽",
    "play":         "▶",
    "pause":        "⏸",
    "check":        "✓",
    "close":        "✕",
    "more":         "⋯",
}

ICONS = sorted(set(_FLUENT) | set(_EMOJI))


# ---------------------------------------------------------------------------
# Font detection — LAZY, cached on first real use.
#
# IMPORTANT: this must NOT run at import time. QFontDatabase.families()
# requires a live QGuiApplication. Normal app startup always creates the
# QApplication before this module is imported, so it used to "work" — but
# PyInstaller's binary-dependency analysis imports every package (including
# this one, via ui.toolbar) in an isolated subprocess with NO QApplication
# running, just to probe for DLLs. Calling QFontDatabase there crashes the
# subprocess outright (0xC0000409). Deferring detection until the first
# actual call (icon_char / icon_font_family / is_fluent_active) — which only
# ever happens after QApplication exists — fixes the PyInstaller build while
# keeping behavior identical at runtime.
# ---------------------------------------------------------------------------
def _detect_fluent_font() -> str:
    """Return the name of the best available Fluent icon font, or '' if none."""
    if sys.platform != "win32":
        return ""
    try:
        families = set(QFontDatabase.families())
    except Exception:
        return ""
    if "Segoe Fluent Icons" in families:
        return "Segoe Fluent Icons"
    if "Segoe MDL2 Assets" in families:
        return "Segoe MDL2 Assets"
    return ""


_FONT_FAMILY: str | None = None   # None = not yet detected


def _ensure_detected() -> None:
    global _FONT_FAMILY
    if _FONT_FAMILY is None:
        _FONT_FAMILY = _detect_fluent_font()


def icon_char(name: str) -> str:
    """Return the icon glyph for `name`. Fluent codepoint on Windows, emoji
    elsewhere. Falls back to '?' if the name is unknown."""
    _ensure_detected()
    if _FONT_FAMILY and name in _FLUENT:
        return _FLUENT[name]
    return _EMOJI.get(name, "?")


def icon_font_family() -> str:
    """Font family to use when rendering the icon character. Empty string
    means 'use the default font' (emoji rendering)."""
    _ensure_detected()
    return _FONT_FAMILY or ""


def is_fluent_active() -> bool:
    """True when Segoe Fluent Icons / MDL2 Assets is available."""
    _ensure_detected()
    return bool(_FONT_FAMILY)