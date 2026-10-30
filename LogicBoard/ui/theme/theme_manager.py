"""
theme_manager.py — Fluent theme manager (dark-only).

Singleton QObject that:
  * Holds the active ThemeTokens (always DARK_TOKENS — LogicBoard is dark-only).
  * Exposes Fluent-style stylesheet builders so widgets don't need to
    repeat token plumbing.
  * Provides a token → QColor parser (``qcolor``) for code that needs a
    QColor object rather than a stylesheet string.

The ``theme_changed`` signal and ``set_theme`` / ``toggle`` methods are kept
so existing call sites keep working, but switching is disabled — there is no
light theme. Everything resolves to dark.

Usage:
    from ui.theme import theme_mgr

    self.setStyleSheet(theme_mgr.window_stylesheet())
    color = theme_mgr.qcolor(theme_mgr.tokens.accent)
"""

from __future__ import annotations

from pathlib import Path
from typing import Optional

from ui.util.size_manager import size_mgr as S

from PySide6.QtCore import QObject, Signal

from .tokens import ThemeTokens, DARK_TOKENS

__all__ = ["ThemeManager", "theme_mgr"]


class ThemeManager(QObject):
    """Singleton that owns the active theme and persists the user's choice."""

    theme_changed = Signal(object)   # emits the new ThemeTokens

    def __init__(self, parent: QObject | None = None) -> None:
        super().__init__(parent)
        self._tokens: ThemeTokens = DARK_TOKENS
        self._settings_path: Optional[Path] = None

    # ── Current state ───────────────────────────────────────────────────
    @property
    def tokens(self) -> ThemeTokens:           return self._tokens
    @property
    def is_dark(self) -> bool:                 return self._tokens.is_dark
    @property
    def theme_name(self) -> str:               return self._tokens.name

    # ── Set theme ───────────────────────────────────────────────────────
    # LogicBoard is dark-only. These remain so existing callers keep working,
    # but they are intentionally no-ops — there is no light theme to switch to.
    def set_theme(self, name: str) -> None:
        return

    def toggle(self) -> None:
        return

    # ── Persistence ─────────────────────────────────────────────────────
    # Dark is the only theme, so there's nothing to persist or restore. These
    # stay as no-ops for call-site compatibility (app.py still calls them).
    def set_settings_path(self, path: Path) -> None:
        self._settings_path = path

    def load_preference(self) -> None:
        return

    def _save_preference(self) -> None:
        return

    # ════════════════════════════════════════════════════════════════════
    # Stylesheet builders (delegate to fluent_styles for the heavy lifting)
    #
    # The methods below stay so old callers keep working — they all now
    # produce Fluent-styled output sourced entirely from tokens.
    # ════════════════════════════════════════════════════════════════════

    def window_stylesheet(self) -> str:
        """Top-level window: bg + default text colour. Aero happens on
        top of this via the aero helper."""
        t = self._tokens
        return f"""
            QMainWindow, QWidget#LogicBoardWindow {{
                background-color: {t.bg_window};
                color: {t.text_primary};
            }}
        """

    def toolbar_stylesheet(self) -> str:
        """Translucent Fluent toolbar that sits over the Aero backdrop."""
        t = self._tokens
        return f"""
            QWidget#LogicBoardToolbar {{
                background-color: {t.toolbar_bg};
                border: 1px solid {t.toolbar_border};
                border-radius: {t.radius_xl}px;
            }}
        """

    def menu_stylesheet(self) -> str:
        """Popup menu / overlay panel — Aero tint + Fluent stroke."""
        # Imported lazily to avoid circular import.
        from .fluent_styles import menu, label
        return menu(tk=self._tokens) + label("primary", tk=self._tokens)

    def dialog_stylesheet(self) -> str:
        """Modal dialog — strong Aero tint + Fluent buttons."""
        from .fluent_styles import button
        t = self._tokens
        return f"""
            QMessageBox, QDialog {{
                background-color: {t.aero_tint_strong};
                color: {t.text_primary};
                border: 1px solid {t.stroke_default};
                border-radius: {t.radius_xl}px;
            }}
            QLabel {{
                color: {t.text_primary};
                background: transparent;
            }}
            {button("default", tk=self._tokens, pad_h=18, pad_v=8)}
        """

    def scrollbar_stylesheet(self, width: int | None = None) -> str:
        """``width=None`` gets the app's standard touch-sized track."""
        from .fluent_styles import scrollbar
        return scrollbar(width=width, tk=self._tokens)

    def progress_stylesheet(self) -> str:
        t = self._tokens
        return f"""
            QProgressBar {{
                border: 1px solid {t.progress_border};
                border-radius: {t.radius_xs}px;
                background-color: {t.progress_bg};
                text-align: center;
                color: {t.text_primary};
                font-weight: 600;
                font-size: {S.scale_px(11)}px;
                min-height: {S.scale_px(8)}px;
            }}
            QProgressBar::chunk {{
                background-color: {t.progress_chunk};
                border-radius: {t.radius_xs - 1}px;
            }}
        """

    def card_stylesheet(self, *, elevation: str = "raised",
                        radius: Optional[int] = None) -> str:
        """Convenience wrapper around fluent_styles.card."""
        from .fluent_styles import card
        return card(elevation, tk=self._tokens, radius=radius)

    def button_stylesheet(self, variant: str = "default") -> str:
        from .fluent_styles import button
        return button(variant, tk=self._tokens)

    # ════════════════════════════════════════════════════════════════════
    # Token → QColor parser
    # ════════════════════════════════════════════════════════════════════
    # Critical: Qt6's `QColor(str)` constructor only accepts `#RRGGBB`,
    # `#AARRGGBB`, or SVG colour names. It does NOT parse CSS `rgb()` /
    # `rgba()` syntax, which is what our tokens are in (because stylesheets
    # do support that syntax).  Any direct `QColor(theme_mgr.tokens.accent)`
    # call therefore produced an INVALID colour, which Qt renders as solid
    # black — this is exactly the "selection rectangle is absolute black"
    # bug the user reported. Use `theme_mgr.qcolor(token)` instead.
    @staticmethod
    def qcolor(token: str):
        """Parse a token string ('rgb(...)' / 'rgba(...)' / '#RRGGBB' / name)
        into a valid QColor. Bridges the gap between stylesheet-friendly
        token strings and code that needs a QColor object."""
        from PySide6.QtGui import QColor
        import re
        if not isinstance(token, str):
            return QColor(token)
        m = re.match(
            r'rgba?\(\s*(\d+)\s*,\s*(\d+)\s*,\s*(\d+)(?:\s*,\s*([\d.]+))?\s*\)',
            token,
        )
        if m:
            r, g, b = int(m.group(1)), int(m.group(2)), int(m.group(3))
            a = int(round(float(m.group(4)) * 255)) if m.group(4) else 255
            return QColor(r, g, b, max(0, min(255, a)))
        return QColor(token)


# ─────────────────────────────────────────────────────────────────────────────
# Module-level singleton
# ─────────────────────────────────────────────────────────────────────────────
theme_mgr = ThemeManager()