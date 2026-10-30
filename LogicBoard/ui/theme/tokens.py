"""
tokens.py — Windows 11 Fluent Design tokens for LogicBoard.

This is the single source of truth for EVERY color, radius, spacing, shadow
intensity, and animation duration used in the app. The cardinal rule:

    Nothing else in the codebase contains hex/rgb/rgba literals.

Reorganised around the official Fluent Design token taxonomy:

    LAYER  : background surface levels (window → surface → card → overlay)
    STROKE : translucent border colours that work on any layer
    TEXT   : foreground colours with proper alpha curves
    ACCENT : interactive / brand colours
    FILL   : control-surface fills (button bg, input bg) — also translucent
    STATUS : success / warning / danger

Plus scale tokens:

    radius : xs (4) / sm (6) / md (8) / lg (12) / xl (16) — Fluent radii
    space  : 2 / 4 / 8 / 12 / 16 / 20 / 24 / 32
    motion : duration_fast (120) / duration (200) / duration_slow (320) +
             easing curves matching Microsoft's motion specification
"""

from __future__ import annotations

from dataclasses import dataclass

__all__ = ["ThemeTokens", "DARK_TOKENS"]


# ─────────────────────────────────────────────────────────────────────────────
# Token dataclass
# ─────────────────────────────────────────────────────────────────────────────

@dataclass(frozen=True)
class ThemeTokens:
    """Immutable design-token set for one theme variant."""

    name: str   # "dark" | "light"

    # ════════════════════════════════════════════════════════════════════════
    # LAYER — opaque surface levels (z-index in colour form)
    # ════════════════════════════════════════════════════════════════════════
    # Layers go from MOST recessed → MOST elevated, in solid neutral greys
    # (the iOS dark-mode ramp). There is no blur behind any surface any more
    # — see ui/util/aero.py, EFFECT — so every value here is OPAQUE and
    # elevation is told by lightness:
    #
    #   canvas         0   the board itself — pure black (CANVAS, below)
    #   panel body    36-46 the floating windows (SURFACE, below)
    #   card          52   cards, tiles, list rows ON a panel
    #   card hover    62
    #
    # The translucent values these replaced only made sense over a blur;
    # over nothing they let the canvas show straight through every panel.
    bg_window:      str = "rgb(0, 0, 0)"      # behind the canvas: black too
    bg_surface:     str = "rgb(40, 40, 44)"
    bg_card:        str = "rgb(52, 52, 56)"
    bg_card_hover:  str = "rgb(62, 62, 67)"

    # Page cards in the Pages panel: one step above the panel body, so the
    # column reads as a stack of separate sheets.
    page_card_bg:       str = "rgb(52, 52, 56)"
    page_card_bg_hover: str = "rgb(60, 60, 65)"
    # Inputs are INSET: a step darker than the panel they sit in.
    bg_input:       str = "rgb(30, 30, 33)"
    bg_overlay:     str = "rgb(40, 40, 44)"
    bg_tile_hover:  str = "rgb(62, 62, 67)"

    # ── SURFACE — the body every floating window is painted with ─────────
    # Painted by ui.util.aero's SurfaceFrame: a soft top-to-bottom fall-off,
    # and a hairline edge that is brighter along the top (lit from above).
    surface_top:      str = "rgb(46, 46, 50)"
    surface_bottom:   str = "rgb(36, 36, 40)"
    surface_edge_top: str = "rgba(255, 255, 255, 0.16)"
    surface_edge:     str = "rgba(255, 255, 255, 0.07)"

    # ════════════════════════════════════════════════════════════════════════
    # STROKE — translucent borders (work over any layer + aero)
    # ════════════════════════════════════════════════════════════════════════
    # Fluent's signature "subtle" stroke is rgba(255,255,255,0.0578) in dark,
    # rgba(0,0,0,0.0578) in light — barely there but defines the layer.
    stroke_subtle:  str = "rgb(56, 56, 56)"
    stroke_default: str = "rgb(64, 64, 64)"
    stroke_strong:  str = "rgb(81, 81, 81)"
    stroke_focus:   str = "rgb(102, 102, 102)"
    # Legacy aliases kept so existing call sites keep working.
    border:         str = "rgb(64, 64, 64)"
    border_subtle:  str = "rgb(56, 56, 56)"
    border_strong:  str = "rgb(81, 81, 81)"

    # ── Glass strokes — for ui.util.rounded_border, NOT for QSS ────────────
    # The strokes above are opaque because a translucent QSS border with a
    # border-radius cannot be drawn correctly: Qt paints the straight edges
    # and the corner arcs separately, so translucent pixels composite twice
    # and the corners come out much darker than the edges (measured +58 alpha
    # at 3 px, +118 at 1 px — see ui/util/rounded_border.py). Opaque hid the
    # bug but made every panel outline a hard dark line, which is glaring
    # over a white background with aero behind it.
    #
    # These are the real translucent values, and they are only correct when
    # painted by install_rounded_border(), which strokes ONE path. Do not put
    # them in a QSS `border:` property — that reintroduces the artifact.
    #
    # NOTE: no panel strokes an outline any more. The floating surfaces are
    # clipped and rounded by DWM and separated by their drop shadow, and an
    # overlay drawn at the panel's own radius disagreed with the compositor's
    # — visibly, as an arc cutting across each corner INSIDE the panel. These
    # tokens stay for anything that wants a glass hairline on a NON-rounded
    # surface; ``ui/util/rounded_border.py`` is currently unused.
    glass_stroke:        str = "rgba(255, 255, 255, 0.10)"
    glass_stroke_strong: str = "rgba(255, 255, 255, 0.16)"

    # ════════════════════════════════════════════════════════════════════════
    # TEXT — alpha-curved foregrounds
    # ════════════════════════════════════════════════════════════════════════
    text_primary:    str = "rgba(255, 255, 255, 0.88)"
    text_secondary:  str = "rgba(255, 255, 255, 0.62)"
    text_muted:      str = "rgba(255, 255, 255, 0.42)"
    text_disabled:   str = "rgba(255, 255, 255, 0.28)"
    text_on_accent:  str = "rgb(255, 255, 255)"

    # Glyph colour for tool icons. OPAQUE, and neutral.
    #
    # Opaque because it is substituted into an SVG's ``currentColor`` and
    # painted as a text fallback — neither path wants an alpha channel it
    # then has to composite twice.
    #
    # Neutral because the default used to be ``#e2e8f0``, which is a cool
    # slate white. Over a dark panel ``text_primary`` composites to roughly
    # ``#e0e0e0``, so every icon in the toolbar sat a measurable step bluer
    # than the label directly beneath it. One or the other is fine; the two
    # side by side is what reads as unfinished.
    icon_default:    str = "#E0E0E0"

    # ════════════════════════════════════════════════════════════════════════
    # ACCENT — interactive / brand colour
    # ════════════════════════════════════════════════════════════════════════
    # Windows 11 dark accent: #4CC2FF (default).
    accent:         str = "rgb(76, 194, 255)"
    accent_hover:   str = "rgb(98, 204, 255)"     # +8 % light
    accent_pressed: str = "rgb(58, 175, 235)"     # -8 % light
    accent_muted:   str = "rgba(76, 194, 255, 0.18)"
    accent_border:  str = "rgb(76, 194, 255)"     # legacy alias

    # ════════════════════════════════════════════════════════════════════════
    # FILL — control-surface tints used over Aero / cards
    # ════════════════════════════════════════════════════════════════════════
    # Buttons/inputs use translucent fills so they pick up the Aero
    # backdrop instead of looking like solid rectangles glued on top.
    fill_subtle:   str = "rgba(255, 255, 255, 0.04)"
    fill_default:  str = "rgba(255, 255, 255, 0.06)"
    fill_hover:    str = "rgba(255, 255, 255, 0.10)"
    fill_pressed:  str = "rgba(255, 255, 255, 0.05)"
    fill_disabled: str = "rgba(255, 255, 255, 0.02)"

    # The fills above are all LIGHTENING tints, which is what a control on a
    # card wants. A control sitting on ARTWORK wants the opposite: the
    # delete button on a board card in the file panel lies over the
    # LogicBoard logo, whose own strokes are pale, and a lightening fill
    # under a pale glyph on pale artwork is invisible at exactly the moment
    # somebody is reaching for it. This darkens instead, so the control
    # reads as a control wherever it lands.
    fill_on_media: str = "rgba(0, 0, 0, 0.38)"

    # ════════════════════════════════════════════════════════════════════════
    # GRADIENTS — the iOS finish
    # ════════════════════════════════════════════════════════════════════════
    # Stylesheet BRUSHES, not colours: use them only as a ``background`` /
    # ``background-color`` value, never through theme_mgr.qcolor (which
    # parses one colour, not a gradient). Every one is lit from above —
    # lighter at the top, deeper at the bottom — which is what makes a
    # control read as a physical, pressable thing rather than a flat fill.
    accent_grad: str = ("qlineargradient(x1:0, y1:0, x2:0, y2:1, "
                        "stop:0 rgb(110, 212, 255), stop:1 rgb(40, 156, 232))")
    accent_grad_hover: str = ("qlineargradient(x1:0, y1:0, x2:0, y2:1, "
                              "stop:0 rgb(132, 220, 255), stop:1 rgb(58, 170, 240))")
    accent_grad_pressed: str = ("qlineargradient(x1:0, y1:0, x2:0, y2:1, "
                                "stop:0 rgb(70, 180, 240), stop:1 rgb(30, 132, 205))")
    # A selected tile / option / row: the accent, softly.
    selected_grad: str = ("qlineargradient(x1:0, y1:0, x2:0, y2:1, "
                          "stop:0 rgba(76, 194, 255, 0.38), "
                          "stop:1 rgba(37, 150, 227, 0.20))")
    # A raised card or tile on a panel.
    card_grad: str = ("qlineargradient(x1:0, y1:0, x2:0, y2:1, "
                      "stop:0 rgb(60, 60, 65), stop:1 rgb(48, 48, 52))")
    tile_grad: str = ("qlineargradient(x1:0, y1:0, x2:0, y2:1, "
                      "stop:0 rgb(66, 66, 71), stop:1 rgb(54, 54, 58))")

    # ════════════════════════════════════════════════════════════════════════
    # STATUS — danger / success / warning
    # ════════════════════════════════════════════════════════════════════════
    danger:               str = "rgb(255, 99, 99)"
    danger_bg:            str = "rgba(255, 99, 99, 0.14)"
    danger_border:        str = "rgb(146, 70, 70)"
    danger_text:          str = "rgb(255, 153, 153)"
    danger_bg_hover:      str = "rgba(255, 99, 99, 0.22)"
    danger_border_btn:    str = "rgb(173, 77, 77)"

    success:              str = "rgb(108, 203, 95)"
    success_bg:           str = "rgba(108, 203, 95, 0.14)"
    success_border:       str = "rgb(77, 127, 70)"
    success_text:         str = "rgb(167, 220, 156)"
    success_bg_hover:     str = "rgba(108, 203, 95, 0.22)"
    success_border_btn:   str = "rgb(83, 141, 75)"

    warning:              str = "rgb(252, 200, 0)"
    warning_bg:           str = "rgba(252, 200, 0, 0.14)"
    warning_border:       str = "rgb(153, 126, 20)"
    warning_text:         str = "rgb(253, 215, 102)"
    warning_bg_hover:     str = "rgba(252, 200, 0, 0.24)"

    # ════════════════════════════════════════════════════════════════════════
    # Former glass tokens — now SOLID
    # ════════════════════════════════════════════════════════════════════════
    # These names are used all over the panels for "the panel's own fill".
    # They were translucent tints laid over the Windows blur; the blur is gone,
    # so they are the panel body colour now, opaque. Kept under their old
    # names so the call sites did not all have to change at once.
    aero_tint:        str = "rgb(40, 40, 44)"
    aero_tint_strong: str = "rgb(36, 36, 40)"
    # Unused since the blur went (nothing calls SetWindowCompositionAttribute
    # while EFFECT is "none"), kept so a theme lookup never fails.
    acrylic_win32_abgr: int = 0xC0282828
    aero_win32_abgr:  int = 0xC0282828
    # The hit catcher IS the panel body now (see install_hit_catcher); this
    # is what the few windows that paint their own catcher-coloured frame
    # (About, the setup wizard) fill it with — the surface's middle tone.
    hit_catcher:         str = "rgb(40, 40, 44)"

    # ════════════════════════════════════════════════════════════════════════
    # TOOLBAR — translucent layer that sits over Aero
    # ════════════════════════════════════════════════════════════════════════
    toolbar_bg:         str = "rgb(40, 40, 44)"
    toolbar_border:     str = "rgb(64, 64, 64)"
    # Faint, lit-from-above fill for a toolbar tile (a stylesheet brush).
    tool_btn_bg:        str = ("qlineargradient(x1:0, y1:0, x2:0, y2:1, "
                               "stop:0 rgba(255, 255, 255, 0.09), "
                               "stop:1 rgba(255, 255, 255, 0.03))")
    # Hover is noticeably brighter so the button lights up under a finger.
    tool_btn_hover:     str = "rgba(255, 255, 255, 0.14)"
    tool_btn_active_bg: str = ("qlineargradient(x1:0, y1:0, x2:0, y2:1, "
                               "stop:0 rgba(76, 194, 255, 0.36), "
                               "stop:1 rgba(37, 150, 227, 0.16))")
    tool_caption:       str = "rgba(255, 255, 255, 0.72)"
    tool_caption_active:str = "rgb(255, 255, 255)"

    # ════════════════════════════════════════════════════════════════════════
    # NAV (page navigator) — was opaque rgb(50,50,50). Now a translucent
    # Fluent pill: barely-there fill + subtle stroke. Sits on the toolbar
    # which is itself translucent over the Win32 aero blur.
    # ════════════════════════════════════════════════════════════════════════
    nav_shell:    str = "rgba(255, 255, 255, 0.06)"
    nav_border:   str = "rgb(64, 64, 64)"
    # ── ink swatches (pen panel grid, shapes panel column) ──
    # The resting ring is translucent white so it reads on every ink, dark
    # or light. The tick takes whichever of the two contrasts with the ink.
    swatch_ring:       str = "rgba(255, 255, 255, 0.16)"
    swatch_tick_dark:  str = "rgb(22, 24, 31)"
    swatch_tick_light: str = "rgb(255, 255, 255)"
    # The custom-colour cell: a colour wheel built from the ink palette.
    swatch_custom_grad: str = ("qconicalgradient(cx:0.5, cy:0.5, angle:90, "
                               "stop:0 rgb(255, 90, 95), stop:0.17 rgb(255, 201, 60), "
                               "stop:0.33 rgb(22, 168, 104), stop:0.5 rgb(24, 196, 196), "
                               "stop:0.67 rgb(92, 162, 255), stop:0.83 rgb(176, 98, 240), "
                               "stop:1 rgb(255, 90, 95))")
    nav_btn_hover:str = "rgba(255, 255, 255, 0.10)"
    nav_text:     str = "rgba(255, 255, 255, 0.86)"

    # ════════════════════════════════════════════════════════════════════════
    # SCROLLBARS
    # ════════════════════════════════════════════════════════════════════════
    scrollbar_bg:     str = "transparent"
    # OPAQUE. The handle used to be 16 % white, which over the aero blur
    # (and over a busy slide thumbnail) was close to invisible: a teacher
    # could not tell a list scrolled at all. A solid grey reads on anything.
    scrollbar_handle: str = "rgb(150, 156, 168)"
    scrollbar_handle_hover: str = "rgb(186, 192, 204)"
    # Pressed is BRIGHTER than hover, not dimmer — a scrollbar handle is
    # being dragged, so the feedback has to survive a finger or a cursor
    # sitting on top of it. The accent says "you have hold of it".
    scrollbar_handle_pressed: str = "rgb(98, 204, 255)"

    # ════════════════════════════════════════════════════════════════════════
    # DIALOG / PROGRESS / SLIDER  (legacy aliases — kept for compat)
    # ════════════════════════════════════════════════════════════════════════
    # `dialog_bg` mirrors aero_tint_strong so old call sites that paint
    # the dialog background directly still get the right glass feel.
    dialog_bg:         str = "rgb(38, 38, 42)"
    dialog_border:     str = "rgb(64, 64, 64)"
    progress_bg:       str = "rgba(255, 255, 255, 0.06)"
    progress_chunk:    str = "rgb(76, 194, 255)"
    progress_border:   str = "rgb(64, 64, 64)"
    slider_groove:     str = "rgba(255, 255, 255, 0.10)"
    slider_accent:     str = "rgb(76, 194, 255)"
    slider_sub_page:   str = "rgb(76, 194, 255)"

    # ════════════════════════════════════════════════════════════════════════
    # Selected option tile (used by pen / eraser / shapes pickers)
    # ════════════════════════════════════════════════════════════════════════
    # Brushes (see GRADIENTS) — only ever used as a stylesheet background.
    accent_selected_bg:     str = ("qlineargradient(x1:0, y1:0, x2:0, y2:1, "
                                   "stop:0 rgba(76, 194, 255, 0.38), "
                                   "stop:1 rgba(37, 150, 227, 0.20))")
    accent_selected_border: str = "rgb(76, 194, 255)"

    # ════════════════════════════════════════════════════════════════════════
    # CANVAS — the part of the app that is NEVER aero
    # ════════════════════════════════════════════════════════════════════════
    # PURE black. Boards saved with the old default grey (28, 28, 28) are
    # moved onto this when opened — see CanvasWidget.adopt_default_bg.
    canvas_bg_color: str = "rgb(0, 0, 0)"
    # The DESK the pages lie on in the scrolling (list) canvas style. Not
    # black: with black pages on a black desk you could not see where one
    # page ended and the gap began.
    canvas_desk:     str = "rgb(38, 38, 42)"

    # ════════════════════════════════════════════════════════════════════════
    # RADIUS scale  (px) — Windows 11 generous Fluent radii. Bumped from the
    # first pass so the whole UI reads as "soft modern" rather than "soft-ish".
    # ════════════════════════════════════════════════════════════════════════
    radius_xs: int = 6     # text inputs, chips
    radius_sm: int = 8     # default buttons
    radius_md: int = 10    # cards, tiles
    radius_lg: int = 14    # large panels
    radius_xl: int = 18    # dialogs, top-level popovers, toolbar
    radius_2xl: int = 24   # page cards — a thumbnail needs a softer corner
                           # than a dialog or the picture fights the frame

    # ════════════════════════════════════════════════════════════════════════
    # SPACING scale  (px)
    # ════════════════════════════════════════════════════════════════════════
    space_xxs: int = 2
    space_xs:  int = 4
    space_sm:  int = 8
    space_md:  int = 12
    space_lg:  int = 16
    space_xl:  int = 20
    space_2xl: int = 24
    space_3xl: int = 32

    # ════════════════════════════════════════════════════════════════════════
    # TYPE SCALE
    # ════════════════════════════════════════════════════════════════════════
    # Five steps, and every piece of text in the UI is one of them. This did
    # not exist before, and without it the panels drifted: the Settings panel
    # alone shipped SEVEN sizes (10/11/12/13/14/15/22), several of them one
    # step apart, which is close enough to look like a mistake but far enough
    # to destroy the hierarchy. A size that isn't on this list is a bug rather
    # than a decision.
    #
    # Values are DESIGN pixels — always pass them through UiScale.px.
    font_caption:  int = 11   # descriptions, section labels, metadata
    font_body:     int = 13   # control labels, buttons, list rows
    font_subtitle: int = 14   # card titles and row titles
    font_title:    int = 20   # panel / dialog titles
    font_display:  int = 26   # hero text (About)

    # Weights. Emphasis should come from WEIGHT first and size second — two
    # sizes one pixel apart read as sloppy, whereas 400 vs 700 at the same
    # size reads as deliberate.
    weight_regular: int = 400
    weight_medium:  int = 600
    weight_bold:    int = 700

    # ════════════════════════════════════════════════════════════════════════
    # CONTROL METRICS
    # ════════════════════════════════════════════════════════════════════════
    # One height for peer controls in a row. Settings previously mixed 40
    # (mode buttons), 42 (calibration rows) and 36 (toggles) among controls
    # that sit inches apart, which is what makes a panel look hand-assembled.
    control_h:    int = 40
    control_h_sm: int = 32
    control_h_lg: int = 48

    # ════════════════════════════════════════════════════════════════════════
    # ELEVATION  (drop-shadow tuning — picked up by ui.util.shadows)
    # ════════════════════════════════════════════════════════════════════════
    # (blur_radius, x_off, y_off, alpha 0-255)
    elev_rest:    tuple = (8,  0, 2,  36)   # cards at rest
    elev_hover:   tuple = (16, 0, 4,  54)   # cards under cursor
    elev_active:  tuple = (4,  0, 1,  28)   # pressed state — squashed
    elev_float:   tuple = (40, 0, 12, 80)   # menus / dialogs

    # ════════════════════════════════════════════════════════════════════════
    # MOTION — duration + easing for transitions
    # ════════════════════════════════════════════════════════════════════════
    duration_fast:    int = 120   # micro interactions (hover-in)
    duration_default: int = 200   # standard (button press, panel pop)
    duration_slow:    int = 320   # major (dialog open, theme switch)

    # Cubic-bezier control points matching Microsoft's motion library:
    #   ease_standard  : decel (UI entering)
    #   ease_accelerate: ease-in (UI exiting)
    #   ease_decelerate: ease-out (UI revealing)
    #   ease_emphasized: snappy with overshoot (Fluent panel pop)
    ease_standard:    tuple = (0.0,  0.0, 0.0, 1.0)
    ease_accelerate:  tuple = (0.7,  0.0, 1.0, 0.5)
    ease_decelerate:  tuple = (0.1,  0.9, 0.2, 1.0)
    ease_emphasized:  tuple = (0.3,  0.0, 0.1, 1.0)

    # ════════════════════════════════════════════════════════════════════════
    # Convenience predicates
    # ════════════════════════════════════════════════════════════════════════
    @property
    def is_dark(self) -> bool: return self.name == "dark"

    @property
    def is_light(self) -> bool: return self.name == "light"


# ─────────────────────────────────────────────────────────────────────────────
# DARK — Windows 11 Fluent dark theme
# ─────────────────────────────────────────────────────────────────────────────
# LogicBoard is dark-only. There used to be a LIGHT_TOKENS palette here for a
# light/dark switch; the theme option was removed, so only the dark tokens
# remain. Everything in the app resolves to DARK_TOKENS.
DARK_TOKENS = ThemeTokens(name="dark")