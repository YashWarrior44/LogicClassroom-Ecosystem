"""
fluent_styles.py — Centralised Fluent-style QSS generators.

Every function in this module returns a Qt stylesheet string built from the
*active* ThemeTokens. There are NO hex/rgb/rgba literals in this file.
That makes the entire UI theme-aware automatically:

    panel.setStyleSheet(fluent.surface(elevation="raised"))
    btn  .setStyleSheet(fluent.button(variant="accent"))

When the user toggles light ⇄ dark, callers can simply re-apply the same
calls and pick up the new colours.

Variant grammar:
    button(variant) :  "default" | "accent" | "subtle" | "ghost" | "danger"
    card(elevation) :  "flat" | "raised"
    surface(layer)  :  "window" | "panel" | "card" | "overlay"

All generators accept an optional ``tokens`` argument for callers that want
to style a specific theme; otherwise they read from the global manager.
"""

from __future__ import annotations

from typing import Optional

from ui.theme.theme_manager import theme_mgr
from ui.theme.tokens        import ThemeTokens
from ui.util.size_manager   import size_mgr as S

__all__ = [
    "tokens", "button", "card", "surface", "input_field", "scrollbar",
    "checkbox", "label", "separator", "menu", "tooltip",
]


# ─────────────────────────────────────────────────────────────────────────────
# Helpers
# ─────────────────────────────────────────────────────────────────────────────

def tokens(tk: Optional[ThemeTokens] = None) -> ThemeTokens:
    """Return the passed-in tokens or the currently active theme tokens."""
    return tk if tk is not None else theme_mgr.tokens


# ─────────────────────────────────────────────────────────────────────────────
# BUTTON
# ─────────────────────────────────────────────────────────────────────────────

def button(variant: str = "default",
           *,
           tk: Optional[ThemeTokens] = None,
           radius: Optional[int] = None,
           pad_h: int = 14,
           pad_v: int = 7) -> str:
    """Fluent button stylesheet.

    Variants:
      "default"  — translucent fill on layer (most common)
      "accent"   — brand-coloured primary action
      "subtle"   — extra-low-emphasis (text-only with hover fill)
      "ghost"    — transparent until hovered
      "danger"   — destructive action
    """
    t = tokens(tk)
    r = t.radius_sm if radius is None else radius

    if variant == "accent":
        bg, fg          = t.accent_grad,    t.text_on_accent
        bg_hover        = t.accent_grad_hover
        bg_pressed      = t.accent_grad_pressed
        border          = "transparent"
        border_hover    = "transparent"

    elif variant == "subtle":
        bg, fg          = "transparent",    t.text_primary
        bg_hover        = t.fill_hover
        bg_pressed      = t.fill_pressed
        border          = "transparent"
        border_hover    = t.stroke_subtle

    elif variant == "ghost":
        bg, fg          = "transparent",    t.text_secondary
        bg_hover        = t.fill_default
        bg_pressed      = t.fill_pressed
        border          = "transparent"
        border_hover    = "transparent"

    elif variant == "danger":
        bg, fg          = t.danger_bg,      t.danger_text
        bg_hover        = t.danger_bg_hover
        bg_pressed      = t.danger_bg
        border          = t.danger_border
        border_hover    = t.danger_border_btn

    else:   # default
        bg, fg          = t.card_grad,      t.text_primary
        bg_hover        = t.tile_grad
        bg_pressed      = t.fill_pressed
        border          = t.stroke_default
        border_hover    = t.stroke_strong

    return f"""
        QPushButton {{
            background-color: {bg};
            color: {fg};
            border: 1px solid {border};
            border-radius: {r}px;
            padding: {pad_v}px {pad_h}px;
            font-weight: 600;
        }}
        QPushButton:hover {{
            background-color: {bg_hover};
            border-color: {border_hover};
        }}
        QPushButton:pressed {{
            background-color: {bg_pressed};
        }}
        QPushButton:disabled {{
            background-color: {t.fill_disabled};
            color: {t.text_disabled};
            border-color: {t.stroke_subtle};
        }}
    """


# ─────────────────────────────────────────────────────────────────────────────
# CARD / SURFACE
# ─────────────────────────────────────────────────────────────────────────────

def card(elevation: str = "raised",
         *,
         tk: Optional[ThemeTokens] = None,
         radius: Optional[int] = None) -> str:
    """A Fluent card — opaque-ish surface with a subtle stroke.

    Pair with ``ui.util.shadows.apply_shadow(card_widget, depth="md")``
    to get the elevation drop-shadow (CSS box-shadow isn't supported in Qt
    stylesheets, so the shadow lives on a QGraphicsDropShadowEffect).
    """
    t = tokens(tk)
    r = t.radius_md if radius is None else radius
    bg = t.bg_card if elevation == "raised" else t.bg_surface
    return f"""
        QFrame, QWidget {{
            background-color: {bg};
            border: 1px solid {t.stroke_subtle};
            border-radius: {r}px;
        }}
    """


def surface(layer: str = "panel",
            *,
            tk: Optional[ThemeTokens] = None,
            radius: Optional[int] = None,
            object_name: Optional[str] = None) -> str:
    """A flat surface — window background, panel base, or overlay.

    Use ``object_name`` to scope the style to a specific widget so it
    doesn't cascade into every child:

        my_panel.setObjectName("PenPanel")
        my_panel.setStyleSheet(fluent.surface("panel", object_name="PenPanel"))
    """
    t = tokens(tk)
    r = (t.radius_xl if radius is None and layer in ("panel", "overlay")
         else t.radius_md if radius is None
         else radius)

    bg_map = {
        "window":  t.bg_window,
        "panel":   t.aero_tint,           # sits over Aero blur
        "card":    t.bg_card,
        "overlay": t.aero_tint_strong,    # menus / tooltips
    }
    bg = bg_map.get(layer, t.bg_surface)
    border = "transparent" if layer == "window" else t.stroke_default

    selector = f"#{object_name}" if object_name else ""
    if selector:
        return f"""
            QWidget{selector} {{
                background-color: {bg};
                border: 1px solid {border};
                border-radius: {r}px;
            }}
            QWidget{selector} QLabel {{
                background: transparent;
                color: {t.text_primary};
            }}
        """
    return f"""
        QWidget {{
            background-color: {bg};
            border: 1px solid {border};
            border-radius: {r}px;
        }}
        QLabel {{
            background: transparent;
            color: {t.text_primary};
        }}
    """


# ─────────────────────────────────────────────────────────────────────────────
# INPUT FIELD  (QLineEdit / QPlainTextEdit / QComboBox)
# ─────────────────────────────────────────────────────────────────────────────

def input_field(*, tk: Optional[ThemeTokens] = None,
                radius: Optional[int] = None) -> str:
    t = tokens(tk)
    r = t.radius_xs if radius is None else radius
    return f"""
        QLineEdit, QPlainTextEdit, QTextEdit, QSpinBox, QDoubleSpinBox {{
            background-color: {t.bg_input};
            color: {t.text_primary};
            border: 1px solid {t.stroke_default};
            border-radius: {r}px;
            padding: {S.scale_px(6)}px {S.scale_px(10)}px;
            selection-background-color: {t.accent_muted};
        }}
        QLineEdit:hover, QPlainTextEdit:hover, QTextEdit:hover {{
            border-color: {t.stroke_strong};
        }}
        QLineEdit:focus, QPlainTextEdit:focus, QTextEdit:focus {{
            border-color: {t.accent};
        }}
        QLineEdit:disabled, QPlainTextEdit:disabled, QTextEdit:disabled {{
            background-color: {t.fill_disabled};
            color: {t.text_disabled};
            border-color: {t.stroke_subtle};
        }}
    """


# ─────────────────────────────────────────────────────────────────────────────
# SCROLLBAR
# ─────────────────────────────────────────────────────────────────────────────

def scrollbar(width: int | None = None, *,
              tk: Optional[ThemeTokens] = None) -> str:
    """The scrollbar. Singular — there is one of these in the app.

    There used to be two: this one, and a second inside
    ``ui/util/touch_scroll.py``. They disagreed about track width (8 vs 10),
    handle length (24 vs 36), track margins (none vs 3/2) and whether a
    pressed handle looked any different (it did not, here). Nothing caught
    it because THIS one turned out to be dead — every scroll area in the app
    went through touch_scroll — so the divergence was invisible right up
    until somebody used the wrong one.

    The touch version's numbers won, and they are the ones below. LogicBoard
    is dragged with a finger on a wall-mounted board; 8px of track and a
    24px handle are a mouse's measurements, and a miss at arm's length.

    The handle is a PILL, Android-style: solid (see the tokens), a little
    wider than it was, and with ends rounded to exact semicircles — the
    corner radius is half the handle's own thickness, worked out from the
    track width and its margins, so it stays round at every width.

    ``width`` overrides the track thickness. The canvas passes a fatter one.
    """
    t = tokens(tk)
    w = S.scale_size(14) if width is None else int(width)
    side = S.scale_size(2)                    # track margin either side
    r = max(1, (w - 2 * side) // 2)           # semicircular ends
    handle   = t.scrollbar_handle
    handle_h = t.scrollbar_handle_hover
    handle_p = t.scrollbar_handle_pressed
    return f"""
        QScrollBar:vertical {{
            background: {t.scrollbar_bg}; width: {w}px;
            margin: {S.scale_size(3)}px {side}px
                    {S.scale_size(3)}px {side}px;
        }}
        QScrollBar::handle:vertical {{
            background: {handle}; border-radius: {r}px;
            min-height: {S.scale_size(44)}px;
        }}
        QScrollBar::handle:vertical:hover   {{ background: {handle_h}; }}
        QScrollBar::handle:vertical:pressed {{ background: {handle_p}; }}
        QScrollBar::add-line:vertical, QScrollBar::sub-line:vertical {{
            height: 0; background: none; border: none;
        }}
        QScrollBar::add-page:vertical, QScrollBar::sub-page:vertical {{
            background: none;
        }}

        QScrollBar:horizontal {{
            background: {t.scrollbar_bg}; height: {w}px;
            margin: {side}px {S.scale_size(3)}px
                    {side}px {S.scale_size(3)}px;
        }}
        QScrollBar::handle:horizontal {{
            background: {handle}; border-radius: {r}px;
            min-width: {S.scale_size(44)}px;
        }}
        QScrollBar::handle:horizontal:hover   {{ background: {handle_h}; }}
        QScrollBar::handle:horizontal:pressed {{ background: {handle_p}; }}
        QScrollBar::add-line:horizontal, QScrollBar::sub-line:horizontal {{
            width: 0; background: none; border: none;
        }}
        QScrollBar::add-page:horizontal, QScrollBar::sub-page:horizontal {{
            background: none;
        }}
    """


# ─────────────────────────────────────────────────────────────────────────────
# CHECKBOX  (Fluent style — pill toggle when used with QCheckBox.tristate=0)
# ─────────────────────────────────────────────────────────────────────────────

def checkbox(*, tk: Optional[ThemeTokens] = None) -> str:
    t = tokens(tk)
    return f"""
        QCheckBox {{
            color: {t.text_primary};
            spacing: {S.scale_px(8)}px;
        }}
        QCheckBox::indicator {{
            width: {S.scale_px(18)}px;
            height: {S.scale_px(18)}px;
            border-radius: {S.scale_px(4)}px;
            border: 1px solid {t.stroke_strong};
            background-color: {t.fill_subtle};
        }}
        QCheckBox::indicator:hover {{
            background-color: {t.fill_hover};
        }}
        QCheckBox::indicator:checked {{
            background-color: {t.accent_grad};
            border-color: {t.accent};
        }}
        QCheckBox::indicator:checked:hover {{
            background-color: {t.accent_grad_hover};
        }}
        QCheckBox::indicator:disabled {{
            background-color: {t.fill_disabled};
            border-color: {t.stroke_subtle};
        }}
    """


# ─────────────────────────────────────────────────────────────────────────────
# LABEL / SEPARATOR / MENU / TOOLTIP
# ─────────────────────────────────────────────────────────────────────────────

def label(role: str = "primary", *,
          tk: Optional[ThemeTokens] = None,
          size_px: int = 13) -> str:
    """Quick label stylesheet by role.

    Roles: "primary", "secondary", "muted", "caption", "heading".
    """
    t = tokens(tk)
    role_map = {
        "primary":   (t.text_primary,   600, size_px),
        "secondary": (t.text_secondary, 500, size_px),
        "muted":     (t.text_muted,     500, size_px - 1),
        "caption":   (t.text_secondary, 500, max(10, size_px - 2)),
        "heading":   (t.text_primary,   700, size_px + 4),
    }
    color, weight, sz = role_map.get(role, role_map["primary"])
    return f"""
        QLabel {{
            color: {color};
            font-weight: {weight};
            font-size: {sz}px;
            background: transparent;
        }}
    """


def separator(orientation: str = "horizontal", *,
              tk: Optional[ThemeTokens] = None) -> str:
    t = tokens(tk)
    if orientation == "vertical":
        return f"""
            QFrame {{
                background: {t.stroke_subtle};
                max-width: 1px;
                min-width: 1px;
                margin: {S.scale_px(4)}px {S.scale_px(8)}px;
            }}
        """
    return f"""
        QFrame {{
            background: {t.stroke_subtle};
            max-height: 1px;
            min-height: 1px;
            margin: {S.scale_px(8)}px {S.scale_px(4)}px;
        }}
    """


def menu(*, tk: Optional[ThemeTokens] = None) -> str:
    t = tokens(tk)
    return f"""
        QMenu {{
            background-color: {t.aero_tint_strong};
            color: {t.text_primary};
            border: 1px solid {t.stroke_default};
            border-radius: {t.radius_md}px;
            padding: {S.scale_px(6)}px;
        }}
        QMenu::item {{
            background-color: transparent;
            padding: {S.scale_px(7)}px {S.scale_px(14)}px;
            border-radius: {t.radius_xs}px;
        }}
        QMenu::item:selected {{
            background-color: {t.fill_hover};
        }}
        QMenu::separator {{
            height: 1px;
            background: {t.stroke_subtle};
            margin: {S.scale_px(4)}px {S.scale_px(8)}px;
        }}
    """


def tooltip(*, tk: Optional[ThemeTokens] = None) -> str:
    t = tokens(tk)
    return f"""
        QToolTip {{
            background-color: {t.aero_tint_strong};
            color: {t.text_primary};
            border: 1px solid {t.stroke_default};
            border-radius: {t.radius_xs}px;
            padding: {S.scale_px(6)}px {S.scale_px(10)}px;
        }}
    """