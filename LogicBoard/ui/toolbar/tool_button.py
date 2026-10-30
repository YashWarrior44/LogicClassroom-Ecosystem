"""
tool_button.py — Custom toolbar button with geometric icon rendering,
animated hover/active states, and an optional ink-color accent bar.

Each button is a square tile with an icon centered above a bold caption
label. Icons resolve via :func:`ui.util.icon_loader.tool_icon` which checks
``assets/icons/<name>.svg`` first (so any tool with a bundled SVG — select,
undo, redo, file, line, rect, ellipse, circle, triangle, prev, next, pdf,
ppt — renders crisp at any DPI), then the Segoe Fluent Icons font on
Windows, then an emoji glyph. The active tool gets a highlighted border +
accent bar showing the current pen/highlighter color.
"""

from __future__ import annotations

from typing import Optional

from PySide6.QtWidgets import QToolButton, QFrame
from PySide6.QtGui import QIcon, QPixmap, QPainter, QFont, QColor
from PySide6.QtCore import (Qt, QSize, QRectF, QPropertyAnimation, QEasingCurve,
                            Property)

from ui.util.scaling import UiScale
from ui.util.icon_loader import tool_icon, ICONS_DIR
from ui.theme.theme_manager import theme_mgr
from ui.theme.tokens import ThemeTokens

__all__ = ["ToolButton", "ToolbarDivider"]


# =====================================================================
# Emoji → QIcon helper (kept for callers that still pass raw glyphs)
# =====================================================================

def emoji_icon(emoji_or_char: str, size: int = 32,
               color: Optional[str] = None) -> QIcon:
    """Render an icon character into a QIcon.

    Accepts either an emoji or a Fluent Icons codepoint. When a Fluent font is
    available, sets that as the font family so PUA codepoints render as the
    proper vector glyph instead of an empty box.
    """
    from ui.util.fluent_icons import icon_font_family, is_fluent_active
    from ui.util.icon_loader import blank_icon_pixmap, icon_target_dpr
    # ``size`` is LOGICAL, so the pixmap has to be backed at the device ratio
    # or Qt upscales the glyph at paint time — the same blur the SVG path had.
    pix = blank_icon_pixmap(size, icon_target_dpr())
    p = QPainter(pix)
    try:
        p.setRenderHint(QPainter.Antialiasing, True)
        p.setRenderHint(QPainter.TextAntialiasing, True)
        font = QFont()
        # When using Fluent codepoints (private-use area, U+E000+), pick the
        # Segoe Fluent Icons / MDL2 font so the glyph renders correctly.
        if is_fluent_active() and emoji_or_char and ord(emoji_or_char[0]) >= 0xE000:
            font.setFamily(icon_font_family())
            font.setPixelSize(max(1, int(size * 0.62)))
        else:
            font.setPixelSize(max(1, int(size * 0.65)))
        p.setFont(font)
        if color:
            p.setPen(QColor(color))
        # LOGICAL rect — pix.rect() is in device pixels and the painter is
        # already scaled by the ratio.
        p.drawText(QRectF(0, 0, size, size), Qt.AlignCenter, emoji_or_char)
    finally:
        p.end()
    return QIcon(pix)


# =====================================================================
# ToolButton
# =====================================================================

class ToolButton(QToolButton):
    """Square toolbar tile: icon + bold label, with theme-aware styling.

    Pass ``icon_name`` to use a named icon from ``assets/icons/<name>.svg``
    (preferred — keeps the toolbar visually consistent and crisp on 4K).
    Pass only ``emoji`` for emoji-only buttons that don't have a bundled
    SVG (e.g. pen, eraser, highlighter, settings).
    """

    def __init__(
        self,
        emoji: str,
        label: str,
        checkable: bool = False,
        parent=None,
        *,
        icon_name: Optional[str] = None,
    ) -> None:
        super().__init__(parent)
        self._emoji = emoji
        self._label = label
        # When icon_name is set and a matching SVG exists, the SVG is used.
        # Otherwise the resolver falls back to Fluent/emoji.
        self._icon_name: Optional[str] = icon_name
        self._active = False
        self._ink_color: QColor | None = None
        self._scale = 1.0
        self._hover_opacity = 0.0

        self.setText(label)
        self.setToolButtonStyle(Qt.ToolButtonTextUnderIcon)
        self.setCursor(Qt.PointingHandCursor)
        self.setCheckable(checkable)
        self.setAutoRaise(False)

        self._hover_anim = QPropertyAnimation(self, b"hover_opacity")
        self._hover_anim.setDuration(120)
        self._hover_anim.setEasingCurve(QEasingCurve.InOutQuad)

        self._apply_geometry()
        self._refresh_style()

    # ---- hover opacity property for animation ----

    def _get_hover_opacity(self) -> float:
        return self._hover_opacity

    def _set_hover_opacity(self, v: float) -> None:
        self._hover_opacity = v
        self.update()

    hover_opacity = Property(float, _get_hover_opacity, _set_hover_opacity)

    def enterEvent(self, event) -> None:
        self._hover_anim.stop()
        self._hover_anim.setStartValue(self._hover_opacity)
        self._hover_anim.setEndValue(1.0)
        self._hover_anim.start()
        super().enterEvent(event)

    def leaveEvent(self, event) -> None:
        self._hover_anim.stop()
        self._hover_anim.setStartValue(self._hover_opacity)
        self._hover_anim.setEndValue(0.0)
        self._hover_anim.start()
        super().leaveEvent(event)

    # ---- public API ----

    def apply_scale(self, scale: float) -> None:
        self._scale = scale
        self._apply_geometry()
        self._refresh_style()

    def set_active(self, active: bool) -> None:
        self._active = active
        if self.isCheckable():
            self.setChecked(active)
        # Re-render the icon so its tint matches the active/idle caption colour.
        self._apply_geometry()
        self._refresh_style()

    def set_ink_color(self, color: QColor | None) -> None:
        self._ink_color = QColor(color) if color else None
        self._refresh_style()

    def set_tool_display(self, emoji: str, label: str) -> None:
        """Replace the displayed icon (raw emoji/Fluent char) and caption.

        Use when the new icon does NOT have a bundled SVG (e.g. the pen
        button switching between '🖊️' and '🖍️'). For named SVG icons, use
        :py:meth:`set_tool_display_icon` instead so the SVG path is taken.
        """
        self._emoji = emoji
        self._icon_name = None
        self._label = label
        self.setText(label)
        self._apply_geometry()

    def set_tool_display_icon(self, icon_name: str, label: str,
                              emoji_fallback: Optional[str] = None) -> None:
        """Replace the displayed icon via icon_name + caption.

        ``icon_name`` is looked up in ``assets/icons/<name>.svg`` first; when
        no SVG exists, ``emoji_fallback`` (if given) is used as the displayed
        glyph instead of leaving the button blank.
        """
        self._icon_name = icon_name
        if emoji_fallback is not None:
            self._emoji = emoji_fallback
        self._label = label
        self.setText(label)
        self._apply_geometry()

    def apply_theme(self) -> None:
        # Theme changed → re-render the icon so currentColor matches the
        # new caption colour, AND refresh the stylesheet.
        self._apply_geometry()
        self._refresh_style()

    # ---- geometry ----

    def _s(self, v: float) -> int:
        return int(round(v * self._scale))

    def _icon_color(self) -> str:
        """Hex colour used to tint SVG ``currentColor`` (and emoji fallback).

        Uses the theme's active-caption colour when the tool is active so the
        icon visually pops with the highlighted border. Idle tools use the
        regular caption colour. We avoid going below ~70 % luminance against
        the panel background for legibility.
        """
        t = theme_mgr.tokens
        return t.tool_caption_active if self._active else t.tool_caption

    def _apply_geometry(self) -> None:
        side = self._s(64)
        icon_sz = max(24, min(40, self._s(34)))
        self.setFixedSize(side, side)

        color = self._icon_color()
        # Prefer the named-icon resolver when we have a name (covers SVG +
        # Fluent + emoji fallbacks in one shot).
        if self._icon_name:
            icon = tool_icon(self._icon_name, icon_sz, color)
        else:
            # No icon name → caller supplied a raw emoji/Fluent char. We still
            # try the SVG path one last time in case the glyph IS a known
            # icon name (handy for legacy call sites passing tool_id chars).
            icon = emoji_icon(self._emoji, icon_sz, color)
        self.setIcon(icon)
        self.setIconSize(QSize(icon_sz, icon_sz))

        f = self.font()
        f.setBold(True)
        f.setPixelSize(max(8, self._s(9)))
        self.setFont(f)

    # ---- style ----

    def _refresh_style(self) -> None:
        t = theme_mgr.tokens
        s = self._s

        bg = t.tool_btn_bg
        hover_bg = t.tool_btn_hover
        active_bg = t.tool_btn_active_bg
        active_border = t.accent_border
        caption = t.tool_caption_active if self._active else t.tool_caption
        label_color = caption

        # When the tool carries an ink colour (pen / highlighter / dual pen),
        # the CAPTION and the selected border take that colour, so the button
        # itself tells you what colour you are about to draw with — you don't
        # have to open the panel to find out.
        #
        # Guarded by a luminance floor: a user is free to pick near-black ink,
        # and painting the caption in it on a dark toolbar would make the word
        # unreadable. Below the floor we keep the theme caption colour and let
        # the existing ink BAR carry the colour instead, which stays visible
        # against the button because it sits on the border.
        if self._ink_color is not None:
            c = self._ink_color
            # The selected-state TINT follows the ink too. It used to be
            # ``tool_btn_active_bg``, a fixed accent blue, so a red pen still
            # sat on a blue tile. Same 22 % alpha the token used, just built
            # from the live colour.
            active_bg = f"rgba({c.red()},{c.green()},{c.blue()},0.22)"
            # Rec. 601 luma — cheap and good enough for a readability gate.
            luma = 0.299 * c.red() + 0.587 * c.green() + 0.114 * c.blue()
            if luma >= 70:
                ink_css = f"rgb({c.red()},{c.green()},{c.blue()})"
                label_color = ink_css
                active_border = ink_css

        radius = s(13)
        border_w = max(1, s(2))
        ink_w = max(3, s(3))
        pad_top = s(4)
        pad_bot = s(2)

        # Build border rules
        if self._active:
            if self._ink_color:
                rgb = f"rgb({self._ink_color.red()},{self._ink_color.green()},{self._ink_color.blue()})"
                border_css = (
                    f"border-top: {border_w}px solid {active_border}; "
                    f"border-left: {border_w}px solid {active_border}; "
                    f"border-right: {border_w}px solid {active_border}; "
                    f"border-bottom: {ink_w}px solid {rgb};"
                )
            else:
                border_css = f"border: {border_w}px solid {active_border};"
            normal_bg = active_bg
        else:
            if self._ink_color:
                rgb = f"rgb({self._ink_color.red()},{self._ink_color.green()},{self._ink_color.blue()})"
                border_css = (
                    f"border: {border_w}px solid transparent; "
                    f"border-bottom: {ink_w}px solid {rgb};"
                )
            else:
                border_css = f"border: {border_w}px solid transparent;"
            normal_bg = bg

        self.setStyleSheet(f"""
            QToolButton {{
                color: {label_color};
                font-size: {max(8, s(9))}px;
                font-weight: bold;
                border-radius: {radius}px;
                background: {normal_bg};
                {border_css}
                padding: {pad_top}px 2px {pad_bot}px 2px;
                text-align: center;
            }}
            QToolButton:hover {{
                color: {t.text_primary};
                background: {hover_bg};
                border: 1px solid {t.stroke_default};
            }}
            QToolButton:pressed {{
                color: {t.text_primary};
                background: {t.fill_pressed};
                border: 1px solid {t.stroke_strong};
            }}
            QToolButton:checked {{
                color: {label_color};
                background: {active_bg};
                border: {border_w}px solid {active_border};
            }}
        """)


# =====================================================================
# ToolbarDivider
# =====================================================================

class ToolbarDivider(QFrame):
    """Thin vertical separator between toolbar groups."""

    def __init__(self, parent=None) -> None:
        super().__init__(parent)
        self.setFrameShape(QFrame.VLine)
        self._scale = 1.0
        self._apply()

    def apply_scale(self, scale: float) -> None:
        self._scale = scale
        self._apply()

    def apply_theme(self) -> None:
        self._apply()

    def _s(self, v: float) -> int:
        return int(round(v * self._scale))

    def _apply(self) -> None:
        t = theme_mgr.tokens
        self.setFixedWidth(max(1, self._s(1)))
        self.setFixedHeight(self._s(64))
        margin = self._s(10)
        line = t.border_subtle if t.is_dark else t.border
        self.setStyleSheet(f"""
            QFrame {{
                background: {line};
                border: none;
                max-width: 1px;
                margin-bottom: {margin}px;
            }}
        """)