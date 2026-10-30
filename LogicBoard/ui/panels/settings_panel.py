"""
settings_panel.py — the Settings surface, laid out like a phone's Settings app.

A category list down the left, one page of controls on the right. That shape
is the point: the old panel was a single scrolling column of eight cards, so
finding "Auto-Save" meant flicking past palm calibration and PowerPoint, and
every card competed for the same attention. Categories give each setting a
home, and the list doubles as a map of what the app can even do.

Three things this panel has to get right, all of them consequences of where it
runs — a wall-mounted touch board in a classroom:

* TOUCH TARGETS. Every row is at least ``control_h_lg`` tall and spans the
  full width of its card, so it can be hit with a finger from arm's length.
* SCROLL vs TAP. Both panes are kinetic-scrollable. The discrimination is
  ``DragStartDistance`` (5 mm) in ``ui.util.touch_scroll``: under it the press
  goes to the control as a tap, over it the gesture becomes a scroll and the
  control never fires. That is why nav items and toggles can live inside
  flickable panes without a finger-drag ever toggling something by accident.
* AERO. The window is translucent so DWM blurs the board behind it, which
  means an unpainted pixel is a HOLE — Windows hit-tests layered windows by
  alpha and taps fall through to whatever is behind. Every surface here paints
  a real (if translucent) fill, and ``prepare_aero_widget`` installs the
  full-bleed hit catcher underneath. See ``ui.util.aero.install_hit_catcher``.
"""

from __future__ import annotations

from typing import Optional, Callable

from PySide6.QtWidgets import (
    QDialog,
    QVBoxLayout,
    QHBoxLayout,
    QLabel,
    QPushButton,
    QFrame,
    QSlider,
    QWidget,
    QScrollArea,
    QStackedWidget,
    QAbstractButton,
    QSizePolicy,
    QApplication,
    QProxyStyle,
    QStyle,
)
from PySide6.QtCore import Qt, QPoint, QSize, QRectF
from PySide6.QtGui import QPainter, QColor, QPen, QBrush

from ui.util.scaling import UiScale
from ui.util.aero import (prepare_aero_widget, apply_aero, enable_shadow,
                          apply_rounded_corners)
from ui.util.audio import click_sound
from ui.theme.theme_manager import theme_mgr
from ui.util.icon_loader import tool_icon

__all__ = ["SettingsPanel"]


_slider_style_cache: Optional[tuple] = None


def _slider_style() -> QStyle:
    """The one Fusion style every settings slider draws with.

    It used to be ``QStyleFactory.create("Fusion")`` per slider, and that
    crashed the process on exit. The factory's style belongs to PYTHON, held
    only by the slider's wrapper, and has no Qt parent — so at interpreter
    shutdown PySide deletes it with every other Python-owned QObject, in hash
    order, often before the slider still drawing with it. Under ``os._exit``
    shiboken's own teardown trips over the same pair instead: it releases the
    style's wrapper, then reaches it again through the slider and reads a
    freed pointer. Either way: an access violation after the last line ran.

    So: one style for the app, parented to the QApplication. Qt owns it, it
    outlives every slider, and it goes last, with the app. Built in Python as
    a QProxyStyle rather than taken from the factory, because shiboken's
    teardown only releases (and so only trips over) wrappers of objects C++
    created. Draws identically — the proxy forwards everything to Fusion.
    """
    global _slider_style_cache
    app = QApplication.instance()
    if _slider_style_cache is None or _slider_style_cache[0] is not app:
        style = QProxyStyle("Fusion")
        style.setParent(app)
        _slider_style_cache = (app, style)
    return _slider_style_cache[1]


class _Switch(QAbstractButton):
    """The capsule toggle iOS uses, drawn rather than styled.

    A stylesheet can round a QPushButton into a pill, but it cannot move a
    knob across it, and "ON"/"OFF" written inside a rounded rectangle is the
    tell that a UI is imitating the look without the mechanism. Painting it
    costs one small paintEvent and gives the real thing: the knob's position
    IS the state, readable across a classroom without reading a word.

    Kept the same height as ``control_h`` so it lines up with every other
    control in a row and stays a comfortable finger target.
    """

    def __init__(self, parent=None) -> None:
        super().__init__(parent)
        self.setCheckable(True)
        self.setCursor(Qt.PointingHandCursor)
        sc = UiScale.px
        t = theme_mgr.tokens
        self.setFixedSize(sc(64), sc(t.control_h))
        self.setFocusPolicy(Qt.NoFocus)

    def paintEvent(self, _event) -> None:
        from PySide6.QtGui import QPainter, QColor
        from PySide6.QtCore import QRectF
        t = theme_mgr.tokens
        p = QPainter(self)
        p.setRenderHint(QPainter.Antialiasing, True)
        r = QRectF(self.rect()).adjusted(0.5, 0.5, -0.5, -0.5)
        radius = r.height() / 2.0
        if self.isChecked():
            track = theme_mgr.qcolor(t.accent)
        else:
            # Neutral, not "red for off": off is a resting state, not an
            # error. Matches iOS's systemFill grey.
            track = QColor(120, 120, 128, 110)
        if not self.isEnabled():
            track.setAlpha(int(track.alpha() * 0.4))
        p.setPen(Qt.NoPen)
        p.setBrush(track)
        p.drawRoundedRect(r, radius, radius)

        pad = r.height() * 0.10
        d = r.height() - 2 * pad
        x = (r.right() - pad - d) if self.isChecked() else (r.left() + pad)
        knob = QColor(255, 255, 255, 255 if self.isEnabled() else 150)
        p.setBrush(knob)
        p.drawEllipse(QRectF(x, r.top() + pad, d, d))
        p.end()

    def sizeHint(self):
        return self.size()


class SettingsPanel(QDialog):
    """Floating settings overlay — sidebar categories + one page at a time."""

    # ── Categories ──────────────────────────────────────────────────────
    # (key, emoji, title, subtitle). Emoji as icons is deliberate for now:
    # they carry colour and meaning at a glance, need no asset pipeline, and
    # render at any size without the Hi-DPI rasterising care an SVG needs.
    # Calibration lives INSIDE Input & Touch rather than beside it: teaching
    # the board what your stylus looks like and choosing what it does with a
    # palm are the same job seen from two ends, and splitting them made the
    # user set half of one thing in each of two places.
    # The colour is category metadata, kept for anywhere a category needs
    # accenting. It is NOT painted behind the sidebar emoji any more: a
    # filled tile put a second shape and a second colour behind a glyph that
    # already has both, and six of them down one edge looked like clutter.
    NAV: tuple = (
        ("input",    "✍️", "Input & Touch", "Touch behaviour and stylus calibration", "#0A84FF"),
        ("autosave", "💾", "Auto-Save",     "Save the board on a timer",              "#30D158"),
        ("others",   "🧩", "Others",        "Eraser, PowerPoint and other odds and ends", "#5E5CE6"),
        ("share",    "📡", "LogicShare",    "Collect work from the class",            "#40C8E0"),
        ("updates",  "🔄", "Updates",       "Check for a newer LogicBoard",           "#FF9F0A"),
        ("about",    "ℹ️", "About",         "Version, licence and features",          "#8E8E93"),
    )

    # ── Shared styles ───────────────────────────────────────────────────
    # Every card, title, label and description in this panel comes from one
    # of these. Previously each widget carried its own inline stylesheet,
    # which is how the panel ended up with card titles at 14 px next to a
    # peer row title at 13 px, and descriptions in two different greys. With
    # one definition per ROLE, that class of drift can't happen — and a
    # change to a role is one edit rather than a hunt through the file.

    @staticmethod
    def _card_qss() -> str:
        sc = UiScale.px; t = theme_mgr.tokens
        return (f"QFrame {{ background: {t.card_grad}; "
                f"border: 1px solid {t.border}; "
                f"border-radius: {sc(t.radius_lg)}px; }}"
                f"QLabel {{ background: transparent; border: none; }}")

    @staticmethod
    def _card_title_qss() -> str:
        """Card heading — the most prominent text inside a card."""
        sc = UiScale.px; t = theme_mgr.tokens
        return (f"color: {t.text_primary}; font-size: {sc(t.font_subtitle)}px; "
                f"font-weight: {t.weight_bold}; "
                f"background: transparent; border: none;")

    @staticmethod
    def _row_title_qss() -> str:
        """A setting's name, where the setting is a row inside a card.
        Same size as a card title, lighter weight — hierarchy from WEIGHT,
        not from a one-pixel size difference."""
        sc = UiScale.px; t = theme_mgr.tokens
        return (f"color: {t.text_primary}; font-size: {sc(t.font_subtitle)}px; "
                f"font-weight: {t.weight_medium}; "
                f"background: transparent; border: none;")

    @staticmethod
    def _section_label_qss() -> str:
        """Small label introducing a group of controls ('Touch Mode')."""
        sc = UiScale.px; t = theme_mgr.tokens
        return (f"color: {t.text_secondary}; font-size: {sc(t.font_caption)}px; "
                f"font-weight: {t.weight_bold}; letter-spacing: 0.3px; "
                f"background: transparent; border: none;")

    @staticmethod
    def _pill_qss(active: bool, *, hover: bool = True) -> str:
        """The ON/OFF toggle AND the segmented Touch Mode buttons.

        They are the same control visually — accent when active, inset when
        not — but were written out SIX separate times in this file, with
        radii of 8 in some copies and 10 in others, and font sizes of 12 and
        13 mixed between them. Identical-looking controls that differ by a
        pixel is the exact thing that makes a UI feel unfinished.

        ``color`` on the active state also moves to the token instead of a
        hard-coded ``#000``, so it stays legible if the accent ever changes.
        """
        sc = UiScale.px; t = theme_mgr.tokens
        if active:
            return (f"QPushButton {{ background: {t.accent_grad}; "
                    f"color: {t.text_on_accent}; border: none; "
                    f"border-radius: {sc(t.radius_md)}px; "
                    f"font-weight: {t.weight_bold}; "
                    f"font-size: {sc(t.font_body)}px; }}")
        base = (f"QPushButton {{ background: {t.bg_input}; "
                f"color: {t.text_secondary}; "
                f"border: 1px solid {t.border}; "
                f"border-radius: {sc(t.radius_md)}px; "
                f"font-weight: {t.weight_bold}; "
                f"font-size: {sc(t.font_body)}px; }}")
        if hover:
            base += (f"QPushButton:hover {{ background: {t.bg_card_hover}; "
                     f"color: {t.text_primary}; border-color: {t.accent}; }}")
        return base

    @staticmethod
    def _segment_qss(active: bool) -> str:
        """One segment of a segmented control.

        The iOS shape: a recessed track holding capsules, with the selected
        one raised out of it. Only the SELECTED segment is filled — an
        unselected segment is transparent so the track shows through, which
        is what makes the group read as one control rather than three
        adjacent buttons.
        """
        sc = UiScale.px; t = theme_mgr.tokens
        if active:
            return (f"QPushButton {{ background: {t.accent_grad}; "
                    f"color: {t.text_on_accent}; border: none; "
                    f"border-radius: {sc(t.radius_sm)}px; "
                    f"padding: 0 {sc(t.space_sm)}px; "
                    f"font-weight: {t.weight_bold}; "
                    f"font-size: {sc(t.font_body)}px; }}")
        return (f"QPushButton {{ background: transparent; "
                f"color: {t.text_secondary}; border: none; "
                f"border-radius: {sc(t.radius_sm)}px; "
                f"padding: 0 {sc(t.space_sm)}px; "
                f"font-weight: {t.weight_medium}; "
                f"font-size: {sc(t.font_body)}px; }}"
                f"QPushButton:hover {{ background: {t.bg_card_hover}; "
                f"color: {t.text_primary}; }}")

    @staticmethod
    def _desc_qss() -> str:
        """Explanatory text under a setting. ONE colour for this role — it
        used to be text_secondary in some places and text_muted in others."""
        sc = UiScale.px; t = theme_mgr.tokens
        return (f"color: {t.text_secondary}; font-size: {sc(t.font_caption)}px; "
                f"font-weight: {t.weight_regular}; "
                f"background: transparent; border: none;")

    @staticmethod
    def _nav_qss(active: bool) -> str:
        """A category row in the sidebar.

        Left-aligned and full-width because it is a LIST ITEM, not a button —
        the accent block on the selected one is what tells you where you are.
        Padding rather than a fixed height so the two-line label never clips
        when the UI scale grows.
        """
        sc = UiScale.px; t = theme_mgr.tokens
        if active:
            return (f"QPushButton {{ background: {t.accent_selected_bg}; "
                    f"color: {t.text_primary}; "
                    f"border: 1px solid {t.accent_selected_border}; "
                    f"border-radius: {sc(t.radius_md)}px; "
                    f"padding: {sc(t.space_sm)}px {sc(t.space_md)}px; "
                    f"text-align: left; "
                    f"font-size: {sc(t.font_body)}px; "
                    f"font-weight: {t.weight_bold}; }}")
        return (f"QPushButton {{ background: transparent; "
                f"color: {t.text_secondary}; "
                f"border: 1px solid transparent; "
                f"border-radius: {sc(t.radius_md)}px; "
                f"padding: {sc(t.space_sm)}px {sc(t.space_md)}px; "
                f"text-align: left; "
                f"font-size: {sc(t.font_body)}px; "
                f"font-weight: {t.weight_medium}; }}"
                f"QPushButton:hover {{ background: {t.bg_card_hover}; "
                f"color: {t.text_primary}; }}")

    # ── Small builders ──────────────────────────────────────────────────
    def _card(self, title: str = "") -> tuple:
        """A card frame + its layout, with the panel's standard padding."""
        sc = UiScale.px; t = theme_mgr.tokens
        card = QFrame()
        card.setStyleSheet(self._card_qss())
        lay = QVBoxLayout(card)
        lay.setContentsMargins(sc(t.space_lg), sc(t.space_md),
                               sc(t.space_lg), sc(t.space_md))
        lay.setSpacing(sc(t.space_md))
        if title:
            lbl = QLabel(title)
            lbl.setStyleSheet(self._card_title_qss())
            lay.addWidget(lbl)
        return card, lay

    def _row(self, title: str, subtitle: str = "",
             control: QWidget = None) -> QWidget:
        """One line of a grouped list: name (+ explanation) left, control right.

        Returns a WIDGET rather than a layout because rows are the things a
        group stacks and draws separators between — a layout can't be given a
        minimum height or asked how tall it is.

        The control is pinned to the TOP rather than centred, so a one-line
        toggle stays level with the title it belongs to instead of drifting
        down beside a three-line description.
        """
        sc = UiScale.px; t = theme_mgr.tokens
        w = QWidget()
        w.setMinimumHeight(sc(t.control_h_lg))
        row = QHBoxLayout(w)
        row.setContentsMargins(sc(t.space_lg), sc(t.space_md),
                               sc(t.space_lg), sc(t.space_md))
        row.setSpacing(sc(t.space_md))
        text = QVBoxLayout()
        text.setSpacing(sc(t.space_xxs))
        name = QLabel(title)
        name.setStyleSheet(self._row_title_qss())
        text.addWidget(name)
        if subtitle:
            sub = QLabel(subtitle)
            sub.setWordWrap(True)
            sub.setStyleSheet(self._desc_qss())
            text.addWidget(sub)
        row.addLayout(text, 1)
        if control is not None:
            row.addWidget(control, 0, Qt.AlignTop)
        return w

    def _group(self, rows: list, *, header: str = "",
               footer: str = "") -> QWidget:
        """An inset grouped list — the shape iOS Settings is built from.

        One rounded container holding several rows, separated by hairlines
        that start at the text rather than at the container edge, with the
        section name above it and the explanation BELOW it. That last part is
        the substantive difference from a card: putting the prose outside the
        container keeps every row the same height and lets the eye run down a
        column of controls without stepping over paragraphs.
        """
        sc = UiScale.px; t = theme_mgr.tokens
        box = QWidget()
        v = QVBoxLayout(box)
        v.setContentsMargins(0, 0, 0, 0)
        v.setSpacing(sc(t.space_xs))

        if header:
            h = QLabel(header.upper())
            h.setStyleSheet(
                f"color: {t.text_secondary}; font-size: {sc(t.font_caption)}px; "
                f"font-weight: {t.weight_bold}; letter-spacing: 0.6px; "
                f"background: transparent; border: none; "
                f"padding-left: {sc(t.space_md)}px;")
            v.addWidget(h)

        card = QFrame()
        card.setStyleSheet(
            f"QFrame {{ background: {t.card_grad}; "
            f"border: 1px solid {t.border}; "
            f"border-radius: {sc(t.radius_lg)}px; }}"
            f"QLabel {{ background: transparent; border: none; }}")
        cl = QVBoxLayout(card)
        cl.setContentsMargins(0, 0, 0, 0)
        cl.setSpacing(0)
        for i, row in enumerate(rows):
            if i:
                line = QFrame()
                line.setFixedHeight(1)
                line.setStyleSheet(
                    f"background: {t.stroke_subtle}; border: none; "
                    f"margin-left: {sc(t.space_lg)}px;")
                cl.addWidget(line)
            cl.addWidget(row)
        v.addWidget(card)

        if footer:
            f = QLabel(footer)
            f.setWordWrap(True)
            f.setStyleSheet(
                f"color: {t.text_secondary}; font-size: {sc(t.font_caption)}px; "
                f"background: transparent; border: none; "
                f"padding: 0 {sc(t.space_md)}px;")
            v.addWidget(f)
        return box

    def _toggle_button(self) -> QWidget:
        """The switch used by every on/off setting in the panel."""
        return _Switch()

    def _emoji_icon(self, emoji: str, size: int) -> "QIcon":
        """The category emoji, on nothing.

        It used to be drawn on a filled rounded colour tile — iOS's category
        glyph. Colour emoji already carry their own shape and colour, so the
        tile added a second competing one behind every glyph, and six
        saturated squares stacked down a sidebar read as clutter rather than
        as a way in. The emoji alone sits on the panel background like the
        page title does, which is also where the same glyph is already shown
        untiled (see ``_select_page``) — one look for one category instead of
        two.

        Rasterised through the icon loader's Hi-DPI helpers rather than a
        bare QPixmap, for the same reason the toolbar icons were: ``size``
        here is LOGICAL, and an untagged pixmap gets stretched to physical
        pixels at paint time and looks soft on a 4K board.
        """
        from PySide6.QtGui import QIcon, QPainter, QFont
        from PySide6.QtCore import QRectF
        from ui.util.icon_loader import blank_icon_pixmap, icon_target_dpr
        # blank_icon_pixmap hands back a TRANSPARENT pixmap, which is the
        # whole trick here: with nothing painted under the glyph the button
        # shows its own background straight through, so the icon keeps
        # following the hover/selected states and the theme for free.
        pm = blank_icon_pixmap(size, icon_target_dpr())
        p = QPainter(pm)
        p.setRenderHint(QPainter.Antialiasing, True)
        p.setRenderHint(QPainter.TextAntialiasing, True)
        r = QRectF(0, 0, size, size)
        # The pen still matters even though a colour emoji ignores it: some
        # of these glyphs have a monochrome text-presentation form a font
        # can fall back to, and the pen is what THAT gets drawn in. White
        # was right against a coloured tile and would be near-invisible on
        # the panel background, so use the panel's own text colour.
        p.setPen(theme_mgr.qcolor(theme_mgr.tokens.text_primary))
        f = QFont()
        # Name the emoji font rather than trusting the default family to fall
        # back to it. The default here is "Sans Serif", which has no emoji
        # coverage, and Qt's fallback for a colour glyph inside drawText is
        # not something to rely on when one setFamily settles it.
        import sys as _sys
        if _sys.platform == "win32":
            f.setFamily("Segoe UI Emoji")
        # Bigger than the old 0.56. That figure was sized to leave a margin
        # of tile visible around the glyph; with no tile there is nothing to
        # leave a margin of, and 0.56 just left the icons looking small.
        f.setPixelSize(max(8, int(size * 0.82)))
        p.setFont(f)
        p.drawText(r, Qt.AlignCenter, emoji)
        p.end()
        return QIcon(pm)

    def _page(self, key: str) -> tuple:
        """A scrollable page for the content stack.

        Each page gets its OWN scroll area rather than one scroller around
        the stack: a page remembers its scroll position while you visit
        another, and a short page can't inherit a long one's offset and open
        looking blank.
        """
        sc = UiScale.px; t = theme_mgr.tokens
        body = QWidget()
        outer = QHBoxLayout(body)
        outer.setContentsMargins(sc(t.space_lg), sc(t.space_sm),
                                 sc(t.space_lg), sc(t.space_lg))
        outer.setSpacing(0)
        # Cap the reading width. Cards that stretch the full width of a 4K
        # board put a toggle a metre away from the label it belongs to, and
        # a line of description text becomes unreadably long. Everything
        # stays left-aligned under the page title rather than centring, so
        # the eye keeps one starting edge as it moves down the page.
        column = QWidget()
        column.setMaximumWidth(sc(760))
        lay = QVBoxLayout(column)
        lay.setContentsMargins(0, 0, 0, 0)
        lay.setSpacing(sc(t.space_md))
        outer.addWidget(column, 1, Qt.AlignTop)
        outer.addStretch(0)
        scroll = QScrollArea()
        scroll.setWidgetResizable(True)
        scroll.setHorizontalScrollBarPolicy(Qt.ScrollBarAlwaysOff)
        scroll.setVerticalScrollBarPolicy(Qt.ScrollBarAsNeeded)
        scroll.setWidget(body)
        from ui.util.touch_scroll import setup_scroll
        setup_scroll(scroll)
        self._pages[key] = scroll
        return scroll, lay

    def __init__(
        self,
        parent=None,
        *,
        palm_eraser_enabled: bool = True,
        palm_min_size: int = 80,
        object_eraser_affects_media: bool = False,
        autosave_enabled: bool = True,
        autosave_interval_min: int = 5,
        touch_mode: str = "palm_rejection",
        stylus_calibrated: bool = False,
        eraser_calibrated: bool = False,
        palm_calibrated: bool = False,
        # Live InputCalibration, so the calibration rows can show WHAT was
        # measured (input type + median area) and not just yes/no.
        input_calibration=None,
        on_palm_toggle: Optional[Callable[[bool], None]] = None,
        on_palm_size: Optional[Callable[[int], None]] = None,
        on_media_eraser_toggle: Optional[Callable[[bool], None]] = None,
        on_autosave_toggle: Optional[Callable[[bool], None]] = None,
        on_autosave_interval: Optional[Callable[[int], None]] = None,
        ppt_animations: bool = False,
        on_ppt_animations: Optional[Callable[[bool], None]] = None,
        canvas_style: str = "one_page",
        on_canvas_style: Optional[Callable[[str], None]] = None,
        zoom_sensitivity: float = 1.0,
        on_zoom_sensitivity: Optional[Callable[[float], None]] = None,
        mouse_is_stylus_front: bool = False,
        on_mouse_is_stylus_front: Optional[Callable[[bool], None]] = None,
        on_touch_mode: Optional[Callable[[str], None]] = None,
        on_calibrate_stylus: Optional[Callable] = None,
        on_calibrate_eraser: Optional[Callable] = None,
        on_calibrate_palm: Optional[Callable] = None,
        logicshare_objects=None,
    ) -> None:
        super().__init__(
            parent,
            # WindowStaysOnTopHint is NOT optional here, and leaving it off is
            # what made Settings "open, vanish, then come back a second or two
            # later". Qt.Tool alone puts the panel above its PARENT and
            # nothing else — the toolbar groups are separate top-level
            # Qt.Tool windows in their own right, and they kept the z-order
            # they had. So the panel mapped, painted one frame, and was
            # covered; it only surfaced again when Windows got round to
            # raising the active window. Every other floating panel in the
            # app (pen, eraser, shapes, file) already carries this flag —
            # Settings was the one that didn't.
            Qt.Tool | Qt.FramelessWindowHint | Qt.WindowStaysOnTopHint
        )
        self.setModal(False)
        # Set WA_TranslucentBackground BEFORE the first show so the
        # Win32 aero blur applied in showEvent has a transparent
        # Qt surface to render through. Also installs the hit catcher —
        # without it the glass gaps between controls are click-through holes.
        prepare_aero_widget(self)

        self._on_palm_toggle = on_palm_toggle
        self._on_palm_size = on_palm_size
        self._on_media_eraser_toggle = on_media_eraser_toggle
        self._on_autosave_toggle = on_autosave_toggle
        self._on_autosave_interval = on_autosave_interval
        self._on_touch_mode = on_touch_mode
        self._on_calibrate_stylus = on_calibrate_stylus
        self._on_calibrate_eraser = on_calibrate_eraser
        self._on_calibrate_palm = on_calibrate_palm
        self._logicshare_objects = logicshare_objects
        self._palm_enabled = palm_eraser_enabled
        self._palm_size = palm_min_size
        self._media_eraser_enabled = object_eraser_affects_media
        self._autosave_enabled = autosave_enabled
        self._autosave_interval = autosave_interval_min
        self._ppt_animations = bool(ppt_animations)
        self._on_ppt_animations = on_ppt_animations
        self._canvas_style = (canvas_style if canvas_style in
                              ("one_page", "scroll_list") else "one_page")
        self._on_canvas_style = on_canvas_style
        self._zoom_sensitivity = max(0.25, min(3.0, float(zoom_sensitivity)))
        self._on_zoom_sensitivity = on_zoom_sensitivity
        self._mouse_is_stylus_front = bool(mouse_is_stylus_front)
        self._on_mouse_is_stylus_front = on_mouse_is_stylus_front
        self._touch_mode = touch_mode if touch_mode in (
            "palm_rejection", "palm_eraser", "palm_off") else "palm_rejection"
        self._stylus_calibrated = stylus_calibrated
        self._eraser_calibrated = eraser_calibrated
        self._palm_calibrated = palm_calibrated
        self._input_cal = input_calibration

        self._pages: dict = {}
        self._nav_btns: dict = {}
        self._current_page: str = ""

        sc = UiScale.px
        t = theme_mgr.tokens

        # Centered floating dialog. Wider than the old single column — a
        # master/detail layout needs room for both halves, and on a board
        # this is a comfortable reading width rather than a cramped strip.
        from PySide6.QtGui import QGuiApplication
        screen = QGuiApplication.primaryScreen()
        avail = screen.availableGeometry() if screen else None
        if avail is not None:
            w = int(avail.width() * 0.56)
            h = int(avail.height() * 0.72)
            self.resize(max(sc(720), w), max(sc(520), h))
            self.setMinimumSize(sc(640), sc(460))
        else:
            self.resize(sc(860), sc(620))
            self.setMinimumSize(sc(640), sc(460))

        self.setStyleSheet("QDialog { background: transparent; }")

        root = QHBoxLayout(self)
        root.setContentsMargins(0, 0, 0, 0)
        root.setSpacing(0)
        root.addWidget(self._build_sidebar())
        root.addWidget(self._build_content(), 1)

        self._select_page(self.NAV[0][0])

    # ── Sidebar ─────────────────────────────────────────────────────────
    def _build_sidebar(self) -> QWidget:
        sc = UiScale.px; t = theme_mgr.tokens

        side = QFrame()
        side.setObjectName("LBSettingsSidebar")
        # A fill of its own, translucent so the aero blur still reads
        # through: the sidebar has to be a distinct REGION, and on a
        # translucent window "distinct" cannot mean "unpainted".
        # A LIFT over the window's tint, not the tint again. The hit catcher
        # now paints ``aero_tint`` across the whole window, so repeating it
        # here would stack 58 % on 58 % and the sidebar would read as a black
        # bar next to the content rather than a neighbouring region. A few
        # percent of white plus the divider is all the separation it needs.
        side.setStyleSheet(f"""
            QFrame#LBSettingsSidebar {{
                background: {t.fill_subtle};
                border: none;
                border-right: 1px solid {t.border};
            }}
            QLabel {{ background: transparent; border: none; }}
        """)

        col = QVBoxLayout(side)
        col.setContentsMargins(sc(t.space_md), sc(t.space_lg),
                               sc(t.space_md), sc(t.space_md))
        col.setSpacing(sc(t.space_md))

        # ---- Brand row: gear + "Settings" ----
        head = QHBoxLayout()
        head.setSpacing(sc(t.space_sm))
        gear = QLabel()
        gear.setFixedSize(sc(26), sc(26))
        gear.setPixmap(tool_icon("settings", sc(26),
                                 color=t.text_primary).pixmap(sc(26), sc(26)))
        gear.setStyleSheet("background: transparent; border: none;")
        head.addWidget(gear, alignment=Qt.AlignVCenter)
        title = QLabel("Settings")
        title.setStyleSheet(f"""
            color: {t.text_primary}; font-size: {sc(t.font_title)}px;
            font-weight: {t.weight_bold}; background: transparent;
        """)
        head.addWidget(title)
        head.addStretch()
        col.addLayout(head)

        # ---- Category list ----
        nav_body = QWidget()
        nav_col = QVBoxLayout(nav_body)
        nav_col.setContentsMargins(0, 0, 0, 0)
        nav_col.setSpacing(sc(t.space_xs))

        for key, emoji, label, _sub, _colour in self.NAV:
            if key == "share" and self._logicshare_objects is None:
                continue          # service not wired — don't advertise it
            # "&&", not "&": QAbstractButton reads a single ampersand as a
            # mnemonic marker, so "Input & Touch" rendered as "Input  Touch"
            # with the T underlined — the ampersand silently eaten.
            b = QPushButton("   " + label.replace("&", "&&"))
            tile = sc(30)
            b.setIcon(self._emoji_icon(emoji, tile))
            b.setIconSize(QSize(tile, tile))
            b.setCheckable(True)
            b.setCursor(Qt.PointingHandCursor)
            # Tall enough to hit from arm's length on a wall-mounted board,
            # and EXPANDING so the list fills the sidebar instead of ending
            # halfway down with a dead grey column beneath it. The cap keeps
            # a 4K board from turning seven categories into seven banners.
            b.setMinimumHeight(sc(t.control_h_lg))
            b.setMaximumHeight(sc(t.control_h_lg * 2))
            b.setSizePolicy(QSizePolicy.Preferred, QSizePolicy.Expanding)
            b.clicked.connect(lambda _c, k=key: self._on_nav_clicked(k))
            self._nav_btns[key] = b
            nav_col.addWidget(b)
        # No trailing stretch — the rows themselves take up the slack. A
        # stretch here is exactly what left the empty space at the bottom.

        # Width from the WIDEST label rather than a fixed number. A
        # hand-picked width is a guess about the font, the UI scale and the
        # emoji metrics all at once, and it only has to be wrong once for a
        # category name to sit clipped against the divider. Clamped so it
        # stays a sidebar either way.
        side.setFixedWidth(self._sidebar_width())

        # The list scrolls in its own right: at a large UI scale seven rows
        # plus the brand can exceed a short screen, and a category you can't
        # reach is worse than one you have to flick to.
        nav_scroll = QScrollArea()
        nav_scroll.setWidgetResizable(True)
        nav_scroll.setHorizontalScrollBarPolicy(Qt.ScrollBarAlwaysOff)
        nav_scroll.setVerticalScrollBarPolicy(Qt.ScrollBarAsNeeded)
        nav_scroll.setWidget(nav_body)
        from ui.util.touch_scroll import setup_scroll
        setup_scroll(nav_scroll)
        col.addWidget(nav_scroll, 1)

        return side

    def _sidebar_width(self) -> int:
        """Wide enough for the longest category, within sane bounds."""
        from PySide6.QtGui import QFontMetrics
        sc = UiScale.px; t = theme_mgr.tokens
        widest = 0
        for b in self._nav_btns.values():
            widest = max(widest, QFontMetrics(b.font()).horizontalAdvance(b.text()))
        # Button padding both sides + the column's own margins + a little
        # slack for the emoji, which measures narrower than it draws in the
        # fallback font.
        chrome = 2 * sc(t.space_md) + 2 * sc(t.space_md) + sc(28)
        return max(sc(232), min(sc(360), widest + chrome))

    def _on_nav_clicked(self, key: str) -> None:
        click_sound.play_click()
        self._select_page(key)

    def _select_page(self, key: str) -> None:
        if key not in self._nav_btns:
            return
        self._current_page = key
        for k, b in self._nav_btns.items():
            active = (k == key)
            b.setChecked(active)
            b.setStyleSheet(self._nav_qss(active))
        page = self._pages.get(key)
        if page is not None:
            self._stack.setCurrentWidget(page)
        for k, emoji, label, sub, _c in self.NAV:
            if k == key:
                self._page_title.setText(f"{emoji}  {label}")
                self._page_sub.setText(sub)
                break

    # ── Content ─────────────────────────────────────────────────────────
    def _build_content(self) -> QWidget:
        sc = UiScale.px; t = theme_mgr.tokens

        wrap = QFrame()
        wrap.setObjectName("LBSettingsContent")
        wrap.setStyleSheet(f"""
            QFrame#LBSettingsContent {{ background: transparent; border: none; }}
            QLabel {{ background: transparent; border: none; }}
        """)
        col = QVBoxLayout(wrap)
        col.setContentsMargins(0, 0, 0, 0)
        col.setSpacing(0)

        # ---- Page header: title + subtitle, close button on the right ----
        head = QHBoxLayout()
        head.setContentsMargins(sc(t.space_lg), sc(t.space_lg),
                                sc(t.space_lg), sc(t.space_sm))
        head.setSpacing(sc(t.space_md))

        titles = QVBoxLayout()
        titles.setSpacing(sc(t.space_xxs))
        self._page_title = QLabel("")
        self._page_title.setStyleSheet(f"""
            color: {t.text_primary}; font-size: {sc(t.font_title)}px;
            font-weight: {t.weight_bold}; background: transparent;
        """)
        self._page_sub = QLabel("")
        self._page_sub.setStyleSheet(self._desc_qss())
        titles.addWidget(self._page_title)
        titles.addWidget(self._page_sub)
        head.addLayout(titles, 1)

        self._close_btn = QPushButton("✕")
        self._close_btn.setFixedSize(sc(t.control_h), sc(t.control_h))
        self._close_btn.setCursor(Qt.PointingHandCursor)
        self._close_btn.setToolTip("Close")
        self._close_btn.setStyleSheet(f"""
            QPushButton {{
                background-color: {t.card_grad}; color: {t.text_primary};
                border: 1px solid {t.border};
                border-radius: {sc(t.radius_md)}px;
                font-size: {sc(t.font_body)}px;
                font-weight: {t.weight_bold};
            }}
            QPushButton:hover {{
                background-color: {t.danger}; color: white;
                border-color: {t.danger};
            }}
        """)
        self._close_btn.clicked.connect(self.close)
        head.addWidget(self._close_btn, 0, Qt.AlignTop)
        col.addLayout(head)

        # ---- The pages ----
        self._stack = QStackedWidget()
        self._stack.setStyleSheet("background: transparent;")
        self._build_input_page()
        self._build_autosave_page()
        self._build_others_page()
        self._build_logicshare_page()
        self._build_updates_page()
        self._build_about_page()
        for key in self._pages:
            self._stack.addWidget(self._pages[key])
        col.addWidget(self._stack, 1)
        return wrap

    # ── Pages ───────────────────────────────────────────────────────────
    def _build_input_page(self) -> None:
        sc = UiScale.px; t = theme_mgr.tokens
        _page, lay = self._page("input")

        # ---- Touch mode: a segmented control, iOS-style ----
        self._mode_btns = {}
        seg = QFrame()
        seg.setStyleSheet(
            f"QFrame {{ background: {t.fill_subtle}; border: none; "
            f"border-radius: {sc(t.radius_md)}px; }}")
        seg_row = QHBoxLayout(seg)
        pad = max(1, sc(3))
        seg_row.setContentsMargins(pad, pad, pad, pad)
        seg_row.setSpacing(pad)
        for key, text in (("palm_rejection", "Palm Rejection"),
                          ("palm_eraser", "Palm Eraser"),
                          ("palm_off", "Off")):
            b = QPushButton(text)
            b.setCheckable(True)
            b.setCursor(Qt.PointingHandCursor)
            b.setMinimumHeight(sc(t.control_h))
            b.clicked.connect(lambda _c, k=key: self._on_mode_clicked(k))
            self._mode_btns[key] = b
            seg_row.addWidget(b)
        seg_holder = QWidget()
        holder_l = QVBoxLayout(seg_holder)
        holder_l.setContentsMargins(sc(t.space_lg), sc(t.space_md),
                                    sc(t.space_lg), sc(t.space_md))
        holder_l.setSpacing(sc(t.space_sm))
        holder_l.addWidget(seg)
        self._mode_hint = QLabel("")
        self._mode_hint.setWordWrap(True)
        self._mode_hint.setStyleSheet(self._desc_qss())
        holder_l.addWidget(self._mode_hint)
        self._refresh_mode_btns()
        lay.addWidget(self._group([seg_holder], header="Touch Mode"))

        # -- Mouse-as-front-tip fallback --------------------------------
        # Some boards never register the thin plastic front tip as a touch
        # contact and emit a plain left click for it instead, so stylus
        # calibration correctly refuses it as "that was a mouse". This lets
        # the user say "on my board, a left click IS the front tip".
        self._mouse_front_btn = self._toggle_button()
        self._update_mouse_front_btn()
        self._mouse_front_btn.clicked.connect(self._toggle_mouse_front)
        lay.addWidget(self._group(
            [self._row("Mouse click = stylus front tip", "",
                       self._mouse_front_btn)],
            header="Stylus",
            footer="Turn on if your board reports the front tip as a mouse "
                   "click instead of a touch, so stylus calibration keeps "
                   "rejecting it."))

        # -- Calibration, on the same page --------------------------------
        # The touch mode above decides what a palm DOES; these decide what
        # the board thinks a palm IS. Reading one without the other is how
        # you end up with a back tip that erases when it should draw.
        lay.addWidget(self._calibration_group())
        lay.addWidget(self._palm_size_group())
        lay.addStretch()

    # -- Palm size: the hand-set alternative to palm calibration ----------
    def _palm_size_group(self) -> QWidget:
        """A knob for the palm size, beside the calibration that measures it.

        Calibration is the accurate route, but it needs a few minutes at the
        board; this is the quick one — and the fix when a calibrated palm
        still catches the side of a hand, or flags a big fingertip. The dot
        on the left is the contact size, drawn to scale against the largest
        setting, so you can see what you are choosing.
        """
        sc = UiScale.px; t = theme_mgr.tokens
        cal = getattr(self, "_input_cal", None)
        row = QWidget()
        h = QHBoxLayout(row)
        h.setContentsMargins(sc(t.space_lg), sc(t.space_md),
                             sc(t.space_lg), sc(t.space_md))
        h.setSpacing(sc(t.space_lg))

        self._palm_dot = _PalmDot()
        self._palm_dot.setFixedSize(sc(64), sc(64))
        h.addWidget(self._palm_dot, 0, Qt.AlignVCenter)

        mid = QVBoxLayout()
        mid.setSpacing(sc(t.space_xs))
        title_row = QHBoxLayout()
        title_row.setSpacing(sc(t.space_sm))
        title = QLabel("Palm Size")
        title.setStyleSheet(self._row_title_qss())
        title_row.addWidget(title)
        title_row.addStretch(1)
        self._palm_value = QLabel("")
        self._palm_value.setStyleSheet(
            f"color: {t.accent}; font-size: {sc(t.font_body)}px; "
            f"font-weight: 800; background: transparent;")
        title_row.addWidget(self._palm_value)
        mid.addLayout(title_row)
        self._palm_source = QLabel("")
        self._palm_source.setStyleSheet(self._desc_qss())
        mid.addWidget(self._palm_source)
        self._palm_slider = QSlider(Qt.Horizontal)
        lo = int(getattr(cal, "PALM_SIZE_MIN", 30))
        hi = int(getattr(cal, "PALM_SIZE_MAX", 240))
        self._palm_slider.setRange(lo, hi)
        self._apply_slider_style(self._palm_slider)
        self._palm_slider.valueChanged.connect(self._on_palm_slider)
        mid.addWidget(self._palm_slider)
        h.addLayout(mid, 1)

        self._palm_reset = QPushButton("Reset")
        self._palm_reset.setCursor(Qt.PointingHandCursor)
        self._palm_reset.setFixedHeight(sc(t.control_h))
        self._palm_reset.setToolTip("Go back to the calibrated (or default) "
                                    "palm size")
        self._palm_reset.setStyleSheet(f"""
            QPushButton {{
                background: {t.fill_subtle}; color: {t.text_primary};
                border: 1px solid {t.border}; border-radius: {sc(t.radius_md)}px;
                font-size: {sc(t.font_caption)}px; font-weight: 700;
                padding: 0 {sc(t.space_md)}px;
            }}
            QPushButton:hover {{ border: 1px solid {t.accent}; }}
            QPushButton:disabled {{ color: {t.text_disabled};
                                    border: 1px solid transparent; }}
        """)
        self._palm_reset.clicked.connect(self._reset_palm_size)
        h.addWidget(self._palm_reset, 0, Qt.AlignVCenter)

        self._refresh_palm_size()
        return self._group(
            [row], header="Palm Size",
            footer="Any contact at least this wide counts as a palm. Set it "
                   "by hand, or calibrate the palm above to measure it — "
                   "whichever you did last is the one in use.")

    def _refresh_palm_size(self) -> None:
        cal = getattr(self, "_input_cal", None)
        if cal is None or not hasattr(self, "_palm_slider"):
            return
        d = float(getattr(cal, "effective_palm_diameter", 80.0))
        manual = bool(getattr(cal, "palm_size_is_manual", False))
        self._palm_slider.blockSignals(True)
        self._palm_slider.setValue(int(round(d)))
        self._palm_slider.blockSignals(False)
        self._palm_value.setText(f"{d:.0f} px")
        if manual:
            src = "Set by hand"
        elif cal.palm_profile.calibrated and cal.palm_profile.median_area > 0:
            src = "From palm calibration"
        else:
            src = "Default"
        self._palm_source.setText(src)
        self._palm_reset.setEnabled(manual)
        self._palm_dot.set_diameter(d, float(self._palm_slider.maximum()))

    def _on_palm_slider(self, value: int) -> None:
        cal = getattr(self, "_input_cal", None)
        if cal is None:
            return
        cal.set_palm_size(float(value))
        if self._on_palm_size:
            # The legacy (unclassified) path reads the canvas's own value.
            self._on_palm_size(int(value))
        self._refresh_palm_size()

    def _reset_palm_size(self) -> None:
        cal = getattr(self, "_input_cal", None)
        if cal is None:
            return
        click_sound.play_click()
        cal.set_palm_size(0)
        if self._on_palm_size:
            self._on_palm_size(int(round(cal.effective_palm_diameter)))
        self._refresh_palm_size()

    def _calibration_group(self) -> QWidget:
        sc = UiScale.px; t = theme_mgr.tokens
        self._calib_btns = {}
        rows = []
        for key, text, cb_attr in (
            ("stylus", "Calibrate Stylus",     "_on_calibrate_stylus"),
            ("eraser", "Calibrate Eraser Tip", "_on_calibrate_eraser"),
            ("palm",   "Calibrate Palm",       "_on_calibrate_palm"),
        ):
            b = QPushButton()
            b.setCursor(Qt.PointingHandCursor)
            # control_h_lg, not control_h: these are the rows a teacher taps
            # while standing at the board, and they carry two lines of text.
            b.setMinimumHeight(sc(t.control_h_lg))
            # Flat and full-bleed so it reads as a LIST ROW you tap, not a
            # button sitting inside a row — the group's own rounding and
            # separators supply the frame.
            b.setStyleSheet(f"""
                QPushButton {{
                    background: transparent; color: {t.text_primary};
                    border: none;
                    font-size: {sc(t.font_body)}px;
                    font-weight: {t.weight_medium};
                    text-align: left;
                    padding: {sc(t.space_md)}px {sc(t.space_lg)}px;
                }}
                QPushButton:hover {{ background: {t.bg_card_hover}; }}
            """)
            b.clicked.connect(lambda _c, a=cb_attr: self._fire_calibration(a))
            self._calib_btns[key] = (b, text)
            rows.append(b)
        self._refresh_calib_btns()
        return self._group(
            rows, header="Contact Profiles",
            footer="Each profile teaches the board what that contact looks "
                   "like, so a stylus tip, the back tip and a resting palm "
                   "can be told apart.")

    def _build_autosave_page(self) -> None:
        sc = UiScale.px; t = theme_mgr.tokens
        _page, lay = self._page("autosave")

        self._autosave_btn = self._toggle_button()
        self._update_autosave_btn()
        self._autosave_btn.clicked.connect(self._toggle_autosave)

        # Interval lives in its own row of the same group, so turning
        # auto-save off visibly greys the thing it controls right below it.
        self._interval_btns = {}
        seg = QFrame()
        seg.setStyleSheet(
            f"QFrame {{ background: {t.fill_subtle}; border: none; "
            f"border-radius: {sc(t.radius_md)}px; }}")
        seg_row = QHBoxLayout(seg)
        pad = max(1, sc(3))
        seg_row.setContentsMargins(pad, pad, pad, pad)
        seg_row.setSpacing(pad)
        for minutes in (1, 5, 10):
            b = QPushButton(f"{minutes} min")
            b.setCheckable(True)
            b.setCursor(Qt.PointingHandCursor)
            b.setMinimumHeight(sc(t.control_h))
            b.clicked.connect(
                lambda _checked, m=minutes: self._on_interval_clicked(m))
            self._interval_btns[minutes] = b
            seg_row.addWidget(b)

        interval_row = QWidget()
        ir = QHBoxLayout(interval_row)
        ir.setContentsMargins(sc(t.space_lg), sc(t.space_md),
                              sc(t.space_lg), sc(t.space_md))
        ir.setSpacing(sc(t.space_md))
        self._interval_label = QLabel("Interval")
        self._interval_label.setStyleSheet(self._row_title_qss())
        ir.addWidget(self._interval_label)
        ir.addStretch()
        ir.addWidget(seg)

        lay.addWidget(self._group(
            [self._row("Auto-Save", "", self._autosave_btn), interval_row],
            header="Saving",
            footer="Saves your board for you every few minutes, without "
                   "interrupting what you're doing. 💾"))
        self._refresh_interval_btns()
        self._refresh_interval_enabled()
        lay.addStretch()

    def _build_others_page(self) -> None:
        """The settings that don't fill a category on their own.

        Eraser scope and PowerPoint import were a page each, and a page with
        one switch on it reads as though something failed to load. Grouped
        here they still get a heading and a footer of their own, so nothing
        is buried — they just stop pretending to be sections.
        """
        sc = UiScale.px; t = theme_mgr.tokens
        _page, lay = self._page("others")

        # ---- Canvas Style ------------------------------------------------
        self._style_btns = {}
        seg = QFrame()
        seg.setStyleSheet(
            f"QFrame {{ background: {t.fill_subtle}; border: none; "
            f"border-radius: {sc(t.radius_md)}px; }}")
        seg_row = QHBoxLayout(seg)
        pad = max(1, sc(3))
        seg_row.setContentsMargins(pad, pad, pad, pad)
        seg_row.setSpacing(pad)
        for key, text in (("one_page", "One Page"),
                          ("scroll_list", "Scrollable List")):
            b = QPushButton(text)
            b.setCheckable(True)
            b.setCursor(Qt.PointingHandCursor)
            b.setMinimumHeight(sc(t.control_h))
            b.clicked.connect(lambda _c, k=key: self._on_canvas_style_clicked(k))
            self._style_btns[key] = b
            seg_row.addWidget(b)
        style_row = QWidget()
        sr = QVBoxLayout(style_row)
        sr.setContentsMargins(sc(t.space_lg), sc(t.space_md),
                              sc(t.space_lg), sc(t.space_md))
        sr.setSpacing(sc(t.space_sm))
        sr.addWidget(seg)

        # Zoom Sensitivity — two-finger zoom strength. Lives in the SAME
        # group as the style it belongs to, and greys out under One Page
        # because it has no effect there at all. An enabled control that
        # does nothing is a lie about what the setting does.
        self._zoom_row = QWidget()
        zr = QHBoxLayout(self._zoom_row)
        zr.setContentsMargins(sc(t.space_lg), sc(t.space_md),
                              sc(t.space_lg), sc(t.space_md))
        zr.setSpacing(sc(t.space_md))
        zbox = QVBoxLayout()
        zbox.setSpacing(sc(t.space_xxs))
        self._zoom_label = QLabel("Zoom Sensitivity")
        self._zoom_label.setStyleSheet(self._row_title_qss())
        zbox.addWidget(self._zoom_label)
        self._zoom_value = QLabel("")
        self._zoom_value.setStyleSheet(self._desc_qss())
        zbox.addWidget(self._zoom_value)
        zr.addLayout(zbox, 1)
        self._zoom_slider = QSlider(Qt.Horizontal)
        self._zoom_slider.setMinimumWidth(sc(240))
        # Integer slider over a float range: 25..300 = 0.25x..3.00x.
        self._zoom_slider.setRange(25, 300)
        self._zoom_slider.setValue(int(round(self._zoom_sensitivity * 100)))
        self._apply_slider_style(self._zoom_slider)
        self._zoom_slider.valueChanged.connect(self._on_zoom_slider)
        zr.addWidget(self._zoom_slider, 0, Qt.AlignVCenter)

        lay.addWidget(self._group(
            [style_row, self._zoom_row],
            header="Canvas Style",
            footer="One Page shows a single page at a time — the original "
                   "behaviour. Scrollable List stacks every page of the board "
                   "vertically like a document; two fingers scroll and zoom "
                   "the list, and Zoom Sensitivity controls how strongly."))
        self._refresh_canvas_style()

        self._media_btn = self._toggle_button()
        self._update_media_btn()
        self._media_btn.clicked.connect(self._toggle_media_eraser)
        lay.addWidget(self._group(
            [self._row("Also erase imported media", "", self._media_btn)],
            header="Eraser",
            footer="If on, the object eraser also deletes images and "
                   "PowerPoint slides — not just ink."))

        self._ppt_anim_btn = self._toggle_button()
        self._update_ppt_anim_btn()
        self._ppt_anim_btn.clicked.connect(self._toggle_ppt_animations)
        self._ppt_warn = QLabel(
            "⚠  Animation import is experimental — it's underdeveloped, "
            "can be imperfect, and is much slower (it waits out every animation "
            "on every slide). Leave this OFF unless you really need "
            "build-by-build slides. 😅")
        self._ppt_warn.setWordWrap(True)
        self._ppt_warn.setStyleSheet(f"""
            color: {t.warning_text}; font-size: {sc(t.font_caption)}px;
            background: {t.warning_bg};
            border: 1px solid {t.warning_border};
            border-radius: {sc(t.radius_sm)}px; padding: {sc(t.space_sm)}px;
        """)
        warn_row = QWidget()
        wl = QVBoxLayout(warn_row)
        wl.setContentsMargins(sc(t.space_lg), sc(t.space_md),
                              sc(t.space_lg), sc(t.space_md))
        wl.addWidget(self._ppt_warn)
        lay.addWidget(self._group(
            [self._row("Import animations", "", self._ppt_anim_btn), warn_row],
            header="PowerPoint",
            footer="Renders each click/build state when importing a .pptx."))

        # ---- PDF & PowerPoint files ---------------------------------------
        # LogicBoard is registered for both (see util/file_association), but
        # WHICH app opens them is the user's call — Windows protects that
        # choice from programs. This row says who owns them now and opens
        # the one Windows page where it can be changed. Making LogicBoard the
        # default is also what puts its PDF / PPT icons on those files.
        self._assoc_btn = QPushButton("Choose in Windows")
        self._assoc_btn.setCursor(Qt.PointingHandCursor)
        self._assoc_btn.setMinimumHeight(sc(t.control_h))
        self._assoc_btn.setStyleSheet(
            self._pill_qss(False)
            + f"QPushButton {{ padding: 0 {sc(t.space_lg)}px; }}")
        self._assoc_btn.clicked.connect(self._open_default_apps)
        # The subtitle is the live status; _row's labels are [title, subtitle].
        self._assoc_row = self._row("Open with LogicBoard", "…",
                                    self._assoc_btn)
        self._assoc_sub = self._assoc_row.findChildren(QLabel)[1]
        lay.addWidget(self._group(
            [self._assoc_row],
            header="PDF & PowerPoint Files",
            footer="Opening a PDF or presentation with LogicBoard asks whether "
                   "it goes on a new board or a saved one, and every page "
                   "arrives as a page you can write on. Windows lets only you "
                   "choose the default app — pick LogicBoard for .pdf and "
                   ".pptx on the page this opens, and those files get "
                   "LogicBoard's icons too."))
        self._refresh_assoc()
        lay.addStretch()

    def _refresh_assoc(self) -> None:
        try:
            from ui.util.file_association import is_default_for
            owned = [e for e in (".pdf", ".pptx") if is_default_for(e)]
        except Exception:
            owned = []
        if len(owned) == 2:
            text = "LogicBoard opens PDFs and presentations ✓"
        elif owned == [".pdf"]:
            text = "LogicBoard opens PDFs. Presentations open in another app."
        elif owned == [".pptx"]:
            text = "LogicBoard opens presentations. PDFs open in another app."
        else:
            text = "PDFs and presentations open in another app right now."
        self._assoc_sub.setText(text)

    def _open_default_apps(self) -> None:
        click_sound.play_click()
        from ui.util.file_association import open_default_apps_settings
        open_default_apps_settings()

    # ── Canvas style ────────────────────────────────────────────────────
    def _on_canvas_style_clicked(self, style: str) -> None:
        click_sound.play_click()
        self._canvas_style = style
        self._refresh_canvas_style()
        if self._on_canvas_style:
            self._on_canvas_style(style)

    def _on_zoom_slider(self, value: int) -> None:
        """Slider ints -> the 0.25..3.00 float the rest of the app uses.

        Named ``_on_zoom_slider``, NOT ``_on_zoom_sensitivity``: the
        constructor stores the caller's callback under the latter, and an
        attribute assigned in __init__ shadows a method of the same name.
        Connecting to it wired the slider straight to main_window with the
        RAW slider integer — 220 instead of 2.20 — which the calibration
        setter then clamped to the maximum. The conversion and the label
        refresh never ran at all.
        """
        self._zoom_sensitivity = value / 100.0
        self._refresh_zoom_label()
        if self._on_zoom_sensitivity:
            self._on_zoom_sensitivity(self._zoom_sensitivity)

    def _refresh_zoom_label(self) -> None:
        self._zoom_value.setText(f"{self._zoom_sensitivity:.2f}×  "
                                 f"five-finger zoom strength")

    def _refresh_canvas_style(self) -> None:
        for key, b in self._style_btns.items():
            selected = (key == self._canvas_style)
            b.setChecked(selected)
            b.setStyleSheet(self._segment_qss(selected))
        # Zoom Sensitivity applies to the list style ONLY, so it is disabled
        # under One Page rather than silently doing nothing.
        live = (self._canvas_style == "scroll_list")
        for w in (self._zoom_label, self._zoom_value, self._zoom_slider):
            w.setEnabled(live)
        self._refresh_zoom_label()

    def _build_logicshare_page(self) -> None:
        if self._logicshare_objects is None:
            return
        _page, lay = self._page("share")
        try:
            from ui.logicshare.settings_section import LogicShareSettingsCard
            ls_service, ls_config, ls_activity = self._logicshare_objects
            lay.addWidget(LogicShareSettingsCard(ls_service, ls_config,
                                                 ls_activity))
        except Exception:
            import logging
            logging.getLogger("LogicShare").exception(
                "Could not build LogicShare settings card")
            fail = QLabel("LogicShare settings could not be loaded.")
            fail.setStyleSheet(self._desc_qss())
            lay.addWidget(fail)
        lay.addStretch()

    # ── Updates ─────────────────────────────────────────────────────────
    def _build_updates_page(self) -> None:
        """Updates as a STATE you can read, not a dialog you have to launch.

        The old flow was a button that opened a stack of modals: a checking
        dialog, then a prompt, then a "you're up to date" box you had to
        dismiss. For the common answer — nothing to do — that is three taps
        to learn one fact. Here the page just says which version is running
        and whether anything is newer, and the install button only exists
        when there is something to install.
        """
        sc = UiScale.px; t = theme_mgr.tokens
        _page, lay = self._page("updates")

        card, cl = self._card()

        # ---- Current version, stated plainly ----
        ver_row = QHBoxLayout()
        ver_row.setSpacing(sc(t.space_md))
        vbox = QVBoxLayout()
        vbox.setSpacing(sc(t.space_xxs))
        name = QLabel("LogicBoard")
        name.setStyleSheet(self._row_title_qss())
        vbox.addWidget(name)
        self._version_label = QLabel(f"Version {self._app_version()}")
        self._version_label.setStyleSheet(self._desc_qss())
        vbox.addWidget(self._version_label)
        ver_row.addLayout(vbox, 1)

        self._check_btn = QPushButton("Check for Updates")
        self._check_btn.setMinimumHeight(sc(t.control_h))
        self._check_btn.setMinimumWidth(sc(180))
        self._check_btn.setCursor(Qt.PointingHandCursor)
        self._check_btn.setStyleSheet(f"""
            QPushButton {{
                background: transparent; color: {t.text_primary};
                border: 1px solid {t.stroke_default};
                border-radius: {sc(t.radius_md)}px;
                padding: 0 {sc(t.space_md)}px;
                font-size: {sc(t.font_body)}px;
                font-weight: {t.weight_medium};
            }}
            QPushButton:hover {{ background: {t.bg_card_hover}; }}
            QPushButton:disabled {{ color: {t.text_disabled};
                                    border-color: {t.border}; }}
        """)
        self._check_btn.clicked.connect(self._start_update_check)
        ver_row.addWidget(self._check_btn, 0, Qt.AlignTop)
        cl.addLayout(ver_row)

        # ---- Status line: the answer, in one sentence ----
        self._update_status = QLabel("")
        self._update_status.setWordWrap(True)
        self._update_status.setStyleSheet(self._desc_qss())
        cl.addWidget(self._update_status)

        # ---- Release notes, only populated when there IS a release ----
        self._release_notes = QLabel("")
        self._release_notes.setWordWrap(True)
        self._release_notes.setTextInteractionFlags(Qt.TextSelectableByMouse)
        self._release_notes.setStyleSheet(f"""
            color: {t.text_secondary}; font-size: {sc(t.font_caption)}px;
            background: {t.bg_input};
            border: 1px solid {t.border};
            border-radius: {sc(t.radius_sm)}px;
            padding: {sc(t.space_sm)}px;
        """)
        self._release_notes.hide()
        cl.addWidget(self._release_notes)

        # ---- Download / install, hidden until there is an update ----
        # Its label is set when an update is found: "Download Update", or
        # "Install Update" when the installer is already on disk.
        self._install_btn = QPushButton("Download Update")
        self._install_btn.setMinimumHeight(sc(t.control_h_lg))
        self._install_btn.setCursor(Qt.PointingHandCursor)
        self._install_btn.setStyleSheet(f"""
            QPushButton {{
                background: {t.accent_grad}; color: {t.text_on_accent};
                border: none; border-radius: {sc(t.radius_md)}px;
                font-size: {sc(t.font_body)}px;
                font-weight: {t.weight_bold};
            }}
            QPushButton:hover {{ background: {t.accent_grad_hover}; }}
        """)
        self._install_btn.clicked.connect(self._install_update)
        self._install_btn.hide()
        cl.addWidget(self._install_btn)

        lay.addWidget(card)
        lay.addStretch()
        self._reset_update_state()

    @staticmethod
    def _app_version() -> str:
        try:
            from ui.util.version import APP_VERSION
            return str(APP_VERSION)
        except Exception:
            return "unknown"

    def _reset_update_state(self) -> None:
        """Idle text — what the page says before anyone has asked."""
        self._pending_update = None
        try:
            from ui.util import updater
            configured = updater.is_configured()
        except Exception:
            configured = False
        if configured:
            self._update_status.setText(
                "Tap Check for Updates to see if a newer version is available.")
        else:
            self._update_status.setText(
                "Updates are disabled in this build.")
            self._check_btn.setEnabled(False)

    def _start_update_check(self) -> None:
        click_sound.play_click()
        try:
            from ui.util import updater
        except Exception:
            self._update_status.setText("The updater isn't available in this build.")
            return
        if not updater.is_configured():
            self._update_status.setText("Updates are disabled in this build.")
            return
        self._check_btn.setEnabled(False)
        self._check_btn.setText("Checking…")
        self._release_notes.hide()
        self._install_btn.hide()
        self._update_status.setText("Contacting the update server…")
        # ThreadedCheck keeps the network call off the UI thread, so the
        # panel stays live and scrollable while it runs — the whole reason
        # this is a page rather than a modal.
        #
        # Parented to the APP, not to the panel: closing Settings deletes the
        # panel, and a QThread destroyed while it is still running aborts the
        # whole process. The check outlives the panel, finishes on its own and
        # then deletes itself; the result slot is simply dropped if the panel
        # has gone by then.
        self._update_check = updater.ThreadedCheck(parent=QApplication.instance())
        self._update_check.finished.connect(self._update_check.deleteLater)
        self._update_check.finished.connect(self._on_update_checked)
        self._update_check.start()

    def _on_update_checked(self, info, err: str) -> None:
        t = theme_mgr.tokens
        self._update_check = None
        try:
            self._check_btn.setEnabled(True)
            self._check_btn.setText("Check for Updates")
        except RuntimeError:
            return              # panel went away mid-check
        if err:
            self._update_status.setText(f"Couldn't check for updates — {err}")
            return
        try:
            from ui.util import updater
            newer = info is not None and updater.newer_than_current(info)
        except Exception:
            newer = False
        if not newer:
            self._update_status.setText(
                f"✅  You're up to date — LogicBoard {self._app_version()} "
                f"is the latest version.")
            return
        self._pending_update = info
        self._update_status.setText(
            f"🎉  LogicBoard {info.latest_version} is available. "
            f"You're on {self._app_version()}.")
        notes = (getattr(info, "release_notes", "") or "").strip()
        if notes:
            # Trimmed rather than scrolled: the page is already scrollable,
            # and release notes are a summary here, not the changelog.
            if len(notes) > 900:
                notes = notes[:900].rstrip() + " …"
            self._release_notes.setText(notes)
            self._release_notes.show()
        # The button says what it does: nothing downloads until it is
        # pressed, and an installer already on disk installs (no download).
        try:
            from ui.util import updater as _up
            self._update_ready = _up.existing_download(info) is not None
        except Exception:
            self._update_ready = False
        self._install_btn.setText(
            "Install Update" if self._update_ready else "Download Update")
        self._install_btn.show()

    def _install_update(self) -> None:
        click_sound.play_click()
        info = getattr(self, "_pending_update", None)
        if info is None:
            return
        try:
            from ui.panels.update_dialog import run_confirmed_update_flow
            # "Install Update" was an answer to "install now?"; "Download
            # Update" was not, so the flow asks it once the file is in.
            run_confirmed_update_flow(
                self, info,
                install_confirmed=bool(getattr(self, "_update_ready", False)))
        except Exception:
            import logging
            logging.getLogger("updater").exception("Install flow crashed")
            self._update_status.setText(
                "The update couldn't be installed. See the log for details.")

    # ── About ───────────────────────────────────────────────────────────
    def _build_about_page(self) -> None:
        """About, rendered IN the panel.

        It used to open a separate modal, which meant closing Settings to
        show it and then having nowhere to go back to. Same content, no
        second window — and one less aero surface to keep in step.
        """
        sc = UiScale.px; t = theme_mgr.tokens
        _page, lay = self._page("about")

        card, cl = self._card()

        head = QHBoxLayout()
        head.setSpacing(sc(t.space_md))
        logo = QLabel()
        logo.setFixedSize(sc(56), sc(56))
        # Centred, because a QLabel defaults to AlignLeft|AlignVCenter for a
        # pixmap and does NOT scale one to fit. sc(56) is 51 here while the
        # ratio-tagged pixmap measures 51.2 logical, so the default alignment
        # pinned it left and shaved the overhang off the right edge. About
        # and the setup wizard already centre theirs.
        logo.setAlignment(Qt.AlignCenter)
        logo.setStyleSheet("background: transparent; border: none;")
        try:
            from ui.util.paths import find_app_icon_path
            from ui.util.icon_loader import app_logo_pixmap
            pm = app_logo_pixmap(sc(56))
            if pm is not None:
                logo.setPixmap(pm)
        except Exception:
            pass
        head.addWidget(logo, 0, Qt.AlignTop)

        titles = QVBoxLayout()
        titles.setSpacing(sc(t.space_xxs))
        nm = QLabel("LogicBoard")
        nm.setStyleSheet(f"""
            color: {t.text_primary}; font-size: {sc(t.font_title)}px;
            font-weight: {t.weight_bold}; background: transparent;
        """)
        titles.addWidget(nm)
        vv = QLabel(f"v{self._app_version()}  ·  Free and open source")
        vv.setStyleSheet(self._desc_qss())
        titles.addWidget(vv)
        cls = QLabel("GNU General Public License v3.0")
        cls.setStyleSheet(self._desc_qss())
        titles.addWidget(cls)
        head.addLayout(titles, 1)
        cl.addLayout(head)

        blurb = QLabel(
            "A free, open-source whiteboard for classrooms — anyone can use "
            "it, study how it works, and improve it.")
        blurb.setWordWrap(True)
        blurb.setStyleSheet(self._desc_qss())
        cl.addWidget(blurb)
        lay.addWidget(card)

        feat_card, fl = self._card("Features ✨")
        try:
            from ui.panels.about_dialog import _FEATURES
        except Exception:
            _FEATURES = ()
        for text in _FEATURES:
            row = QLabel(f"•   {text}")
            row.setWordWrap(True)
            row.setStyleSheet(self._desc_qss())
            fl.addWidget(row)
        lay.addWidget(feat_card)

        foot = QLabel(
            "Built with Python · PySide6 · C++17 · pybind11 · nlohmann/json 🛠️")
        foot.setWordWrap(True)
        foot.setStyleSheet(self._desc_qss())
        lay.addWidget(foot)
        lay.addStretch()

    # ================================================================
    # Centering — show in the middle of the screen like Media Manager
    # ================================================================
    def _center_on_screen(self) -> None:
        """Put the panel in the middle of the available screen area.

        Idempotent, so it is safe to call both before the first show (where
        it belongs — see ``show_anchored``) and from ``showEvent`` for any
        caller that reaches ``show()`` directly.
        """
        from PySide6.QtGui import QGuiApplication
        screen = QGuiApplication.primaryScreen()
        if screen is None:
            return
        geo = screen.availableGeometry()
        fg = self.frameGeometry()
        fg.moveCenter(geo.center())
        self.move(fg.topLeft())

    def showEvent(self, event):
        super().showEvent(event)
        # A safety net for a direct show(), not the main path. Centring HERE
        # and only here is the other half of the "it jumps about on open"
        # report: the window is already mapped by the time showEvent runs, so
        # it appeared wherever Qt first put it and then moved. Every other
        # panel in the app moves before show() for exactly this reason, and
        # show_anchored now does the same.
        self._center_on_screen()
        apply_aero(self)
        # Rounded HWND so the sidebar's own fill doesn't end in a hard square
        # corner against the blurred desktop. Applied to the WINDOW, not to a
        # card: DWM rounds the real surface, which is the only way the corner
        # of a translucent window can be clipped without leaving an alpha-0
        # notch the OS would treat as click-through.
        apply_rounded_corners(self, mode="round")
        enable_shadow(self)

    # ================================================================
    # Palm eraser
    # ================================================================

    def _toggle_media_eraser(self) -> None:
        click_sound.play_click()
        self._media_eraser_enabled = not self._media_eraser_enabled
        self._update_media_btn()
        if self._on_media_eraser_toggle:
            self._on_media_eraser_toggle(self._media_eraser_enabled)

    def _update_media_btn(self) -> None:
        # The switch draws its own state — position IS the answer, so there
        # is no text to set and no second stylesheet to keep in step.
        self._media_btn.setChecked(bool(self._media_eraser_enabled))

    # ================================================================
    # Input & Touch Settings
    # ================================================================
    def _on_mode_clicked(self, mode: str) -> None:
        click_sound.play_click()
        self._touch_mode = mode
        self._refresh_mode_btns()
        if self._on_touch_mode:
            self._on_touch_mode(mode)

    def _refresh_mode_btns(self) -> None:
        sc = UiScale.px
        t = theme_mgr.tokens
        hints = {
            "palm_rejection": "Ignore palm touches while writing with the stylus.",
            "palm_eraser":    "Treat palm touches as an eraser.",
            "palm_off":       "No palm detection — every touch just draws.",
        }
        for key, b in self._mode_btns.items():
            selected = (key == self._touch_mode)
            b.setChecked(selected)
            b.setStyleSheet(self._segment_qss(selected))
        self._mode_hint.setText(hints.get(self._touch_mode, ""))

    def _fire_calibration(self, cb_attr: str) -> None:
        click_sound.play_click()
        cb = getattr(self, cb_attr, None)
        if cb:
            cb()

    def set_calibration_state(self, stylus: bool = None, eraser: bool = None,
                              palm: bool = None) -> None:
        """Update the 'Calibrated/Not calibrated' status shown on the buttons.
        Called after a Phase-2 calibration dialog finishes."""
        if stylus is not None: self._stylus_calibrated = stylus
        if eraser is not None: self._eraser_calibrated = eraser
        if palm   is not None: self._palm_calibrated   = palm
        self._refresh_calib_btns()
        # A palm calibration replaces a hand-set size; show the new one.
        self._refresh_palm_size()

    def _calib_detail(self, key: str) -> str:
        """What a calibrated profile actually measured — input type, and the
        median contact area when the input reports one.

        Shown on the row itself so the numbers stay inspectable after the
        calibration dialog has closed. A profile with no area isn't broken:
        a pen-digitizer tip carries no contact ellipse and is identified by
        its input family instead, which the text says explicitly rather than
        showing a blank or a zero."""
        cal = getattr(self, "_input_cal", None)
        if cal is None:
            return "Calibrated"
        prof = {"stylus": cal.stylus_profile,
                "eraser": cal.eraser_profile,
                "palm":   cal.palm_profile}.get(key)
        if prof is None or not prof.calibrated:
            return "Calibrated"
        try:
            from ui.panels.calibration_dialog import SOURCE_LABELS
            src = SOURCE_LABELS.get(prof.source, prof.source or "unknown")
        except Exception:
            src = prof.source or "unknown"
        if getattr(prof, "median_area", 0) > 0:
            return f"{src} · {prof.median_area:.0f} px²"
        return f"{src} · no contact size"

    def _refresh_calib_btns(self) -> None:
        states = {
            "stylus": self._stylus_calibrated,
            "eraser": self._eraser_calibrated,
            "palm":   self._palm_calibrated,
        }
        for key, (b, text) in self._calib_btns.items():
            done = bool(states.get(key))
            tick = "  ✓" if done else ""
            status = self._calib_detail(key) if done else "Not calibrated"
            b.setText(f"{text}{tick}     ·  {status}")


    def _toggle_autosave(self) -> None:
        click_sound.play_click()
        self._autosave_enabled = not self._autosave_enabled
        self._update_autosave_btn()
        self._refresh_interval_enabled()
        if self._on_autosave_toggle:
            self._on_autosave_toggle(self._autosave_enabled)

    def _toggle_mouse_front(self) -> None:
        click_sound.play_click()
        self._mouse_is_stylus_front = not self._mouse_is_stylus_front
        self._update_mouse_front_btn()
        if self._on_mouse_is_stylus_front:
            self._on_mouse_is_stylus_front(self._mouse_is_stylus_front)

    def _update_mouse_front_btn(self) -> None:
        self._mouse_front_btn.setChecked(bool(self._mouse_is_stylus_front))

    def _toggle_ppt_animations(self) -> None:
        click_sound.play_click()
        self._ppt_animations = not self._ppt_animations
        self._update_ppt_anim_btn()
        if self._on_ppt_animations:
            self._on_ppt_animations(self._ppt_animations)

    def _update_ppt_anim_btn(self) -> None:
        self._ppt_anim_btn.setChecked(bool(self._ppt_animations))

    def _update_autosave_btn(self) -> None:
        self._autosave_btn.setChecked(bool(self._autosave_enabled))

    def _on_interval_clicked(self, minutes: int) -> None:
        click_sound.play_click()
        self._autosave_interval = minutes
        self._refresh_interval_btns()
        if self._on_autosave_interval:
            self._on_autosave_interval(minutes)

    def _refresh_interval_btns(self) -> None:
        """Highlight the selected interval; style the segmented buttons."""
        for minutes, b in self._interval_btns.items():
            selected = (minutes == self._autosave_interval)
            b.setChecked(selected)
            b.setStyleSheet(self._segment_qss(selected))

    def _refresh_interval_enabled(self) -> None:
        """Grey out the interval buttons + label when auto-save is OFF."""
        enabled = self._autosave_enabled
        self._interval_label.setEnabled(enabled)
        for b in self._interval_btns.values():
            b.setEnabled(enabled)

    # ================================================================
    # Slider styling
    # ================================================================

    def _apply_slider_style(self, slider: QSlider) -> None:
        sc = UiScale.px
        t = theme_mgr.tokens

        groove_h = sc(10)
        handle_sz = sc(38)
        handle_r = handle_sz // 2
        groove_r = groove_h // 2
        margin = -((handle_sz - groove_h) // 2)
        border_px = sc(2)

        slider.setStyle(_slider_style())
        slider.setFixedHeight(handle_sz + sc(14))
        slider.setStyleSheet(f"""
            QSlider::groove:horizontal {{
                height: {groove_h}px; background: {t.slider_groove};
                border-radius: {groove_r}px;
            }}
            QSlider::handle:horizontal {{
                width: {handle_sz}px; height: {handle_sz}px;
                margin: {margin}px 0;
                background: {t.slider_accent};
                border: {border_px}px solid white;
                border-radius: {handle_r}px;
            }}
            QSlider::sub-page:horizontal {{
                background: {t.slider_sub_page};
                border-radius: {groove_r}px;
            }}
            QSlider::add-page:horizontal {{
                background: {t.slider_groove};
                border-radius: {groove_r}px;
            }}
            QSlider {{ background: transparent; border: none; }}
        """)

    # ================================================================
    # Show anchored
    # ================================================================

    def show_anchored(self, anchor: QWidget = None, offset: QPoint = QPoint(0, 0)) -> None:
        # Settings is a CENTERED floating dialog (like Media Manager), so the
        # anchor is ignored; the arg is kept for call-site compatibility.
        #
        # Order matters. adjustSize/centre BEFORE show() so the window is
        # mapped once, already in its final place — showing first and moving
        # afterwards is a visible flash at the wrong position.
        self._center_on_screen()
        self.show()
        # And raise it explicitly. The stays-on-top flag settles the z-order
        # against ordinary windows, but the toolbar groups are top-level
        # Qt.Tool windows too, so among equals the last one raised wins.
        self.raise_()
        self.activateWindow()


class _PalmDot(QWidget):
    """The palm size, drawn to scale: a circle whose diameter is the chosen
    size as a share of the largest setting, in a fixed square."""

    def __init__(self, parent=None) -> None:
        super().__init__(parent)
        self._frac = 0.4

    def set_diameter(self, d: float, d_max: float) -> None:
        self._frac = max(0.12, min(1.0, d / max(1.0, d_max)))
        self.update()

    def paintEvent(self, _e):                       # noqa: N802
        t = theme_mgr.tokens
        p = QPainter(self)
        p.setRenderHint(QPainter.Antialiasing, True)
        side = float(min(self.width(), self.height()))
        # The frame: the largest palm the knob allows.
        p.setPen(QPen(theme_mgr.qcolor(t.border), 1.0, Qt.DashLine))
        p.setBrush(Qt.NoBrush)
        p.drawEllipse(QRectF(0.5, 0.5, side - 1, side - 1))
        d = (side - 4) * self._frac
        acc = theme_mgr.qcolor(t.accent)
        fill = QColor(acc)
        fill.setAlpha(70)
        p.setPen(QPen(acc, 2.0))
        p.setBrush(QBrush(fill))
        p.drawEllipse(QRectF((side - d) / 2, (side - d) / 2, d, d))
        p.end()
