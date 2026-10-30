"""
about_dialog.py — LogicBoard's About dialog.

Aero-glassy, frameless, and — critically — SCROLLABLE. The old design
was a fixed 500×560 box with 15 bullet points hard-coded via ``\\n``
splits, which meant that at anything other than 100% UI scale the last
few bullets clipped off the bottom or the footer overlapped the close
button.

The rewrite:
  * Root QDialog stays transparent so DWM aero shows through.
  * Translucent card holds the content and draws its OWN rounded
    corners via QSS ``border-radius``. The window's actual HWND is
    kept SQUARE (``apply_rounded_corners(..., mode="square")``) —
    everything outside the card is fully transparent and invisible,
    so visually it still reads as one rounded window. Letting DWM
    ALSO round the raw window edge (its old ``mode="round"``) used a
    different, smaller, fixed system radius than the card's CSS
    radius, so the two curves didn't line up — a faint second
    "border inside a border" arc was visible at each corner. Square
    HWND + one CSS-radius card is the only combination with a single
    unambiguous curve.
  * Content sits inside a QScrollArea — the hero (logo / title /
    version) is fixed, everything below scrolls if it overflows. No
    matter the scale factor, nothing clips.
  * All sizes come through ``size_mgr``'s semantic helpers
    (``scale_font``, ``scale_size``, ``scale_margin``, ``scale_spacing``,
    ``scale_radius``) rather than the older bare ``UiScale.px``.
  * Features are rendered as individual rows with an accent-dot bullet
    and consistent typography — much cleaner than the old newline-
    stuffed QLabel.
"""

from __future__ import annotations

from PySide6.QtCore import Qt, QPoint, QTimer
from PySide6.QtGui import QCursor, QGuiApplication, QPixmap
from PySide6.QtWidgets import (
    QDialog, QFrame, QHBoxLayout, QLabel, QPushButton, QScrollArea,
    QVBoxLayout, QWidget,
)

from ui.util.size_manager import size_mgr as S
from ui.util.audio import click_sound
from ui.util.paths import find_app_icon_path
from ui.util.aero import (apply_aero, enable_shadow,
                              prepare_aero_widget)
from ui.util.version import APP_VERSION
from ui.theme.theme_manager import theme_mgr

try:
    from ui.util.aero import apply_rounded_corners
except ImportError:  # older builds without the helper
    apply_rounded_corners = None  # type: ignore[assignment]

__all__ = ["AboutDialog"]


# Feature entries — one per row in the features list. Kept as a plain
# tuple so ordering is obvious and reordering is trivial.
_FEATURES = (
    "Vector-based object engine (C++ core)",
    "Infinite command-pattern undo / redo",
    "JSON human-readable save files",
    "GPU-ready architecture",
    "Freehand pen with pressure support",
    "Highlighter with rectangular cross-section",
    "Shape tools: line, rect, ellipse, circle, triangle, arrow",
    "Pixel & object erasers, both undo-safe",
    "Palm detection with stylus calibration",
    "Five-finger scroll gesture",
    "Dark theme with live token conversion",
    "Multi-page board management",
    "Touch-first modern UI",
    "Auto-save and LogicShare file receiving",
)


class AboutDialog(QDialog):
    """Modern frameless About dialog."""

    def __init__(self, parent=None) -> None:
        super().__init__(parent, Qt.FramelessWindowHint)
        self.setModal(True)
        # Transparent surface so the DWM aero backdrop reads through.
        prepare_aero_widget(self)
        self.setAttribute(Qt.WA_TranslucentBackground, True)

        # 520×640 in design units. The scroll area inside means we don't
        # HAVE to be pixel-perfect — the content adapts — but a shape
        # that's roughly hero + a full features scroll pane at ~1× keeps
        # the dialog feeling deliberate rather than an infinite dumping
        # ground.
        self.setFixedSize(S.scale_width(520), S.scale_height(640))

        t = theme_mgr.tokens

        # ─── Root QDialog styled DIRECTLY ─────────────────────────────
        # NO inner QFrame "card". The working panels (pen, eraser, shapes)
        # taught us the correct pattern: style the QDialog itself with
        # ``background: none`` (so the DWM aero backdrop is the fill)
        # and NO border (because Qt draws CSS borders as a paint op on
        # top of the widget's rectangular geometry — at curved DWM
        # corners those paint lines overlap each other and produce the
        # ugly double-border / thick-corner artifacts that were reported).
        #
        # ``apply_rounded_corners(mode="round")`` in showEvent tells DWM
        # to round the HWND at its native ~8px radius. Since there's no
        # competing CSS border-radius on any inner card, there's only
        # ONE curve source — the DWM compositor — and it shapes both
        # the aero fill and the native shadow in one pass. Clean.
        self.setStyleSheet(f"""
            QDialog {{ background: none; border: none; }}
            QFrame#GlassCatcher {{
                background: {t.hit_catcher}; border: none;
            }}
        """)

        # ─── The tap catcher ──────────────────────────────────────────
        # Same 1 %-alpha frame the setup wizard uses, and it's what makes
        # the features list finger-scrollable at all.
        # ``WA_TranslucentBackground`` gives this dialog a WS_EX_LAYERED
        # window with per-pixel alpha, and Windows hit-tests layered
        # windows BY ALPHA — with the dialog painting nothing, the only
        # pixels that existed as far as the OS was concerned were the
        # Close button and the glyphs of the text. A finger landing in
        # the gap between two feature rows was delivered to the window
        # BEHIND the dialog, so the scroll area never saw the touch and
        # no amount of QScroller tuning could have helped. 1 % alpha is
        # invisible and completely solid to the hit tester. See
        # ``tokens.hit_catcher``.
        catcher = QFrame(self)
        catcher.setObjectName("GlassCatcher")
        shell = QVBoxLayout(self)
        shell.setContentsMargins(0, 0, 0, 0)
        shell.setSpacing(0)
        shell.addWidget(catcher)

        # Card content layout — lives on the catcher frame.
        card_v = QVBoxLayout(catcher)
        card_v.setContentsMargins(
            S.scale_margin(28), S.scale_margin(28),
            S.scale_margin(28), S.scale_margin(22),
        )
        card_v.setSpacing(S.scale_spacing(14))

        # ─── HERO: logo + title + version + subtitle ────────────────
        self._build_hero(card_v, t)

        # Subtle divider between hero and scrollable content.
        divider = QFrame()
        divider.setFixedHeight(1)
        divider.setStyleSheet(
            f"background: {t.stroke_subtle}; border: none;")
        card_v.addWidget(divider)

        # ─── SCROLLABLE MIDDLE: creator + features ──────────────────
        # QScrollArea guarantees the content is never clipped regardless
        # of UI scale — if it overflows, it scrolls.
        from PySide6.QtWidgets import QSizePolicy
        scroll = QScrollArea()
        scroll.setWidgetResizable(True)
        scroll.setFrameShape(QFrame.NoFrame)
        # Explicit AsNeeded (not the default) — a couple of Qt / PySide6
        # versions default to "Never" when the viewport width query loop
        # doesn't converge (which happens with word-wrapped labels
        # inside a resizable scroll area). Explicit AsNeeded avoids the
        # ambiguity.
        scroll.setVerticalScrollBarPolicy(Qt.ScrollBarAsNeeded)
        scroll.setHorizontalScrollBarPolicy(Qt.ScrollBarAlwaysOff)
        # ``Expanding`` vertically — "I can shrink, but give me any spare
        # height." NOT ``MinimumExpanding``, which was here before and means
        # the opposite of what its comment claimed: it makes sizeHint() a
        # HARD FLOOR. The scroll area's sizeHint tracks all 15 feature rows
        # (~655 px), so that floor propagated up and gave the dialog a
        # minimumSizeHint of 180x695 while setFixedSize pinned it to
        # 474x584. Qt can't satisfy both, so Windows clamped it and logged
        # "Unable to set geometry ... minimum size: 474x584 maximum size:
        # 474x584", handing back an 869 px-tall window. That fight is what
        # made the dialog grow after showEvent and land off-centre.
        #
        # An explicit small minimum keeps the scroll area from ever
        # demanding its content's full height again — the whole point of a
        # scroll area is that the content does NOT have to fit.
        scroll.setSizePolicy(QSizePolicy.Preferred, QSizePolicy.Expanding)
        scroll.setMinimumHeight(S.scale_size(120))
        scroll.setStyleSheet("QScrollArea { background: transparent; border: none; }")
        # Modern scrollbar + touch flick if the util is present.
        try:
            from ui.util.touch_scroll import setup_scroll
            setup_scroll(scroll)
        except Exception:
            pass

        scroll_body = QWidget()
        scroll_body.setStyleSheet("background: transparent;")
        # Body wants its own natural height (word-wrap plus row heights).
        # Preferred×Preferred so the scroll area's ``setWidgetResizable``
        # only forces WIDTH matching — the height stays free to grow past
        # the viewport, which is exactly what triggers scrolling.
        scroll_body.setSizePolicy(QSizePolicy.Preferred, QSizePolicy.Preferred)
        body = QVBoxLayout(scroll_body)
        body.setContentsMargins(0, S.scale_margin(4), 0, S.scale_margin(4))
        body.setSpacing(S.scale_spacing(14))

        # What it is, and the licence that keeps it that way.
        creator_txt = QLabel("A free, open-source whiteboard for classrooms.")
        creator_txt.setAlignment(Qt.AlignCenter)
        creator_txt.setWordWrap(True)
        creator_txt.setStyleSheet(
            f"font-size: {S.scale_font(12.5)}px; "
            f"color: {t.text_secondary}; background: transparent;")
        body.addWidget(creator_txt)

        by = QLabel("Licensed under the <b>GNU GPL v3.0</b>")
        by.setAlignment(Qt.AlignCenter)
        by.setStyleSheet(
            f"font-size: {S.scale_font(13.5)}px; "
            f"color: {t.text_primary}; background: transparent;")
        body.addWidget(by)

        cls = QLabel("Anyone can use it, study how it works, and improve it.")
        cls.setAlignment(Qt.AlignCenter)
        cls.setWordWrap(True)
        cls.setStyleSheet(
            f"font-size: {S.scale_font(11.5)}px; "
            f"color: {t.text_secondary}; background: transparent;")
        body.addWidget(cls)

        # Small vertical breathing room before the features section.
        body.addSpacing(S.scale_spacing(6))

        # Features section title.
        feat_title = QLabel("Features ✨")
        feat_title.setStyleSheet(
            f"font-size: {S.scale_font(14)}px; font-weight: 700; "
            f"color: {t.text_primary}; background: transparent;")
        body.addWidget(feat_title)

        # Individual feature rows.
        for text in _FEATURES:
            body.addWidget(self._feature_row(text, t))

        body.addStretch(1)
        scroll.setWidget(scroll_body)
        card_v.addWidget(scroll, 1)  # stretch — this is the tallest child

        # ─── Footer + Close button ─────────────────────────────────
        footer = QLabel(
            "Built with Python · PySide6 · C++17 · pybind11 · nlohmann/json 🛠️"
        )
        footer.setAlignment(Qt.AlignCenter)
        footer.setWordWrap(True)
        footer.setStyleSheet(
            f"font-size: {S.scale_font(10.5)}px; "
            f"color: {t.text_secondary}; background: transparent;")
        card_v.addWidget(footer)

        close_btn = QPushButton("Close")
        close_btn.setObjectName("primary")
        close_btn.setFixedHeight(S.scale_size(42))
        close_btn.setCursor(Qt.PointingHandCursor)
        close_btn.setStyleSheet(f"""
            QPushButton#primary {{
                background: {t.accent_grad}; color: {t.text_on_accent};
                border: none;
                border-radius: {S.scale_radius(12)}px;
                font-size: {S.scale_font(13.5)}px; font-weight: 700;
                padding: 0 {S.scale_size(20)}px;
            }}
            QPushButton#primary:hover {{
                background: {t.accent_grad_hover};
            }}
        """)
        close_btn.clicked.connect(
            lambda: (click_sound.play_click(), self.accept()))
        card_v.addWidget(close_btn)

    # ---- helpers ------------------------------------------------------

    def _build_hero(self, root: QVBoxLayout, t) -> None:
        """Logo + title + version-pill stack at the top of the card."""
        # Logo. Fixed size so Qt's layout can never stretch it wider
        # than the pixmap and cause a visual overlap.
        logo_sz = S.scale_size(84)
        logo = QLabel()
        logo.setAlignment(Qt.AlignCenter)
        logo.setFixedSize(logo_sz, logo_sz)
        logo.setStyleSheet("background: transparent; border: none;")
        icon_path = find_app_icon_path()
        # app_logo_pixmap picks the right frame out of the multi-resolution
        # logo.ico and rasterises at the device ratio. Doing it here by hand
        # is what produced a hero logo built from the file's 16x16 frame.
        from ui.util.icon_loader import app_logo_pixmap
        pm = app_logo_pixmap(logo_sz) if icon_path else None
        if pm is not None and not pm.isNull():
            logo.setPixmap(pm)
        else:
            logo.setText("LB")
        if not logo.pixmap() or logo.pixmap().isNull():
            logo.setStyleSheet(
                f"font-size: {S.scale_font(40)}px; font-weight: 700; "
                f"color: {t.text_primary}; background: transparent;")
        root.addWidget(logo, 0, Qt.AlignHCenter)

        root.addSpacing(S.scale_spacing(6))

        # Title.
        title = QLabel("LogicBoard")
        title.setAlignment(Qt.AlignCenter)
        title.setStyleSheet(
            f"font-size: {S.scale_font(26)}px; font-weight: 800; "
            f"color: {t.text_primary}; background: transparent;")
        root.addWidget(title)

        # Version pill.
        version = QLabel(f"v{APP_VERSION}")
        version.setAlignment(Qt.AlignCenter)
        version.setFixedHeight(S.scale_size(22))
        version.setStyleSheet(f"""
            font-size: {S.scale_font(11)}px; font-weight: 600;
            letter-spacing: 0.6px;
            color: {t.text_secondary};
            background: {t.fill_default};
            border: 1px solid {t.stroke_subtle};
            border-radius: {S.scale_radius(11)}px;
            padding: 0 {S.scale_size(12)}px;
        """)
        # Wrap version in an HBox so we can centre without stretching.
        pill_row = QHBoxLayout()
        pill_row.setContentsMargins(0, 0, 0, 0)
        pill_row.addStretch(1)
        pill_row.addWidget(version)
        pill_row.addStretch(1)
        root.addLayout(pill_row)

    def _feature_row(self, text: str, t) -> QWidget:
        """One feature row: accent dot + text. Clean row alignment
        without relying on QLabel's ``line-height`` interpretation
        (which Qt only respects for RichText, not for the plain multi-
        line labels the old design used).

        Fixed row height so the scroll area's overflow calc is
        deterministic — word-wrapped labels report ``sizeHint().height()``
        that depends on their width, which itself depends on the scroll
        viewport's width; that circular dependency sometimes leaves Qt
        computing height=0 and no scroll fires. Locking the row height
        breaks the loop.
        """
        row = QWidget()
        row.setStyleSheet("background: transparent;")
        row.setFixedHeight(S.scale_size(24))
        hb = QHBoxLayout(row)
        hb.setContentsMargins(S.scale_margin(4), 0, 0, 0)
        hb.setSpacing(S.scale_spacing(10))

        dot_sz = S.scale_size(6)
        dot = QLabel()
        dot.setFixedSize(dot_sz, dot_sz)
        dot.setStyleSheet(
            f"background: {t.accent_grad}; border-radius: {dot_sz // 2}px;")
        hb.addWidget(dot, 0, Qt.AlignVCenter)

        lbl = QLabel(text)
        # No word wrap — the feature strings all fit on a single line at
        # the dialog width, and word wrap re-introduces the width-height
        # circular dependency this row is designed to avoid.
        lbl.setStyleSheet(
            f"font-size: {S.scale_font(12)}px; "
            f"color: {t.text_primary}; background: transparent;")
        hb.addWidget(lbl, 1)

        return row

    # ---- lifecycle ----------------------------------------------------

    def _center_on_display(self) -> None:
        """Put the dialog at the dead centre of the physical display.

        Measured from the LIVE frame size rather than the requested one.
        Screen comes from the cursor first: ``self.screen()`` reports
        wherever Qt happened to place the window, which on a laptop +
        smart-board setup can be the display the teacher isn't looking at.
        """
        try:
            scr = QGuiApplication.screenAt(QCursor.pos()) \
                  or self.screen() or QGuiApplication.primaryScreen()
            if scr is None:
                return
            geo = scr.geometry()
            size = self.frameGeometry().size()
            pos = geo.center() - QPoint(size.width() // 2, size.height() // 2)
            # A dialog taller than the screen must not hang off the top —
            # its Close button is at the bottom but the hero is at the top.
            pos.setX(max(geo.left(), pos.x()))
            pos.setY(max(geo.top(), pos.y()))
            self.move(pos)
        except Exception:
            pass

    def showEvent(self, event):
        """Centre, then apply DWM aero + native drop shadow + rounded
        corners once the HWND exists. All three are idempotent.

        The aero block MUST live here. It briefly got moved into the
        resize-triggered helper below, and once the layout over-constraint
        was fixed the dialog stopped resizing after show — so the helper
        never ran and the About dialog came up as flat grey with no glass
        at all.
        """
        super().showEvent(event)
        self._center_on_display()
        # Guarded once-per-instance so repeated shows don't re-stack the
        # effect (some Windows builds glitch on that).
        if not getattr(self, "_aero_applied", False):
            try: apply_aero(self)
            except Exception: pass
            try: enable_shadow(self)
            except Exception: pass
            if apply_rounded_corners is not None:
                try: apply_rounded_corners(self, mode="round")
                except Exception: pass
            self._aero_applied = True

    def resizeEvent(self, event):
        """Re-centre if the dialog's real size ever changes after show.

        Belt-and-braces now that the scroll area no longer forces a
        minimum bigger than ``setFixedSize`` — with that fixed the size is
        stable and this rarely fires. The move is DEFERRED rather than done
        here: this is a translucent WS_EX_LAYERED window with DWM aero
        behind it, and repositioning it mid-resize left the composited
        surface stale (the dialog rendered completely blank).
        """
        super().resizeEvent(event)
        if not self.isVisible() or event.oldSize() == event.size():
            return
        QTimer.singleShot(0, self._center_and_repaint)

    def _center_and_repaint(self) -> None:
        if not self.isVisible():
            return
        self._center_on_display()
        self.update()