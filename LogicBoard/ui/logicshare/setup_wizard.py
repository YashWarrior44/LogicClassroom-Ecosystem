"""
ui/logicshare/setup_wizard.py — First-time setup wizard.

Frameless, aero-backed, and — critically for the fix — SCROLLABLE. The
old fixed-height card crammed logo + title + warning banner + four
fields + Continue button into 650px, which worked at 1× UI scale but
had things stacking on top of each other the moment the display scale
factor dropped below 1.0 (a 0.912 setup was where it showed).

The rewrite:
  * Root QDialog stays transparent so DWM aero shows through.
  * Translucent inner card holds the content.
  * Header (logo + title + subtitle) is fixed at the top of the card.
  * Warning banner + all form fields sit inside a QScrollArea — the
    dialog CAN'T clip them regardless of scale, they'll scroll if
    the card isn't tall enough.
  * The Continue button is anchored at the bottom of the card (outside
    the scroll region) so it's always visible.
  * All measurements go through ``size_mgr`` semantic helpers
    (``scale_font``, ``scale_size``, ``scale_margin``, ``scale_spacing``,
    ``scale_radius``) — no more raw ``UiScale.px``.

Collects the required fields:
    • Room Number    — exactly 3 digits, numeric only
    • Class Standard — dropdown (LGK / UKG / 1st … 12th)
    • Section        — dropdown (A … G)
    • Password       — required, hidden

On Continue: validate → persist to settings.json (via LSConfig.apply_setup)
→ accept(). The caller then launches LogicBoard normally.
"""

from __future__ import annotations

from PySide6.QtCore import Qt, QEvent, QRegularExpression
from PySide6.QtGui import QCursor, QGuiApplication, QPixmap, QRegularExpressionValidator
from PySide6.QtWidgets import (
    QDialog, QVBoxLayout, QHBoxLayout, QLabel, QPushButton, QFrame,
    QLineEdit, QComboBox, QScrollArea, QWidget,
)

from ui.util.size_manager import size_mgr as S
from ui.util.touch_scroll import setup_combo_popup
from ui.util.paths import find_app_icon_path
from ui.util.aero import (apply_aero, enable_shadow,
                              prepare_aero_widget)
from ui.util.animations import press_squish
from ui.theme.theme_manager import theme_mgr
from ui.theme import fluent_styles

from .config import CLASS_STANDARDS, SECTIONS, validate_room

try:
    from ui.util.aero import apply_rounded_corners
except ImportError:
    apply_rounded_corners = None  # type: ignore[assignment]

__all__ = ["SetupWizard"]


class SetupWizard(QDialog):
    def __init__(self, config, parent=None):
        super().__init__(parent, Qt.WindowType.FramelessWindowHint)
        self._config = config
        self.setModal(True)
        prepare_aero_widget(self)
        self.setAttribute(Qt.WA_TranslucentBackground, True)

        # 620×680 in design units. The scroll area inside the card means
        # this doesn't have to fit every field pixel-perfectly at
        # non-standard scale factors — the fields scroll if squeezed —
        # but the design intent is that at ~1× everything fits without
        # scrolling.
        self.setFixedSize(S.scale_width(620), S.scale_height(680))
        t = theme_mgr.tokens

        # ─── Root: no border, no inner card ───────────────────────────
        # Styled exactly like the working panels (pen, eraser, shapes):
        # ``background: none`` so DWM aero is the fill, ``border: none``
        # so Qt never draws a CSS border at the corners (those overlap
        # at curved DWM edges producing the ugly double-border artifacts).
        # ``apply_rounded_corners(mode="round")`` in showEvent gives the
        # window its DWM-native rounded shape. One curve source, clean.
        #
        # ``QFrame#GlassCatcher`` is the one addition, and it is the fix
        # for "the setup window minimises itself when I tap it" — see
        # where it's built below.
        self.setStyleSheet(f"""
            QDialog {{ background: none; border: none; }}
            QFrame#GlassCatcher {{
                background: {t.hit_catcher}; border: none;
            }}
            QLabel  {{ color: {t.text_primary}; background: transparent;
                       border: none; }}
            {fluent_styles.input_field(tk=t)}
            QLineEdit {{
                background-color: {t.fill_default};
                border: 1px solid {t.stroke_default};
                border-radius: {S.scale_radius(8)}px;
                color: {t.text_primary};
                padding: {S.scale_size(9)}px {S.scale_size(14)}px;
                font-size: {S.scale_font(13)}px;
            }}
            QLineEdit:hover {{ background-color: {t.bg_card_hover}; }}
            QLineEdit:focus {{
                background-color: {t.fill_default};
                border-color: {t.accent};
            }}
            QComboBox {{
                background-color: {t.fill_default};
                color: {t.text_primary};
                border: 1px solid {t.stroke_default};
                border-radius: {S.scale_radius(8)}px;
                padding: {S.scale_size(9)}px {S.scale_size(14)}px;
                font-size: {S.scale_font(13)}px;
            }}
            QComboBox:hover {{ background-color: {t.bg_card_hover}; }}
            QComboBox:focus, QComboBox:on {{ border-color: {t.accent}; }}
            QComboBox::drop-down {{ border: none;
                                      width: {S.scale_size(26)}px; }}
            QComboBox::down-arrow {{
                image: none;
                border-left:  {S.scale_size(5)}px solid transparent;
                border-right: {S.scale_size(5)}px solid transparent;
                border-top:   {S.scale_size(6)}px solid {t.text_secondary};
                margin-right: {S.scale_size(10)}px;
            }}
            /* Drop-down list. ``aero_tint`` (0.58) replaces the old
               ``dialog_bg`` (0.78) — and, just as importantly,
               ``setup_combo_popup`` makes the popup WINDOW translucent,
               without which any alpha here composites against solid
               black and reads as a flat opaque slab no matter how low
               it goes. 0.55-0.62 is the token file's documented sweet
               spot for popovers.
               This fill also has to stay non-transparent: it's what
               gives the translucent popup a hittable alpha channel.
               Border/radius are left off — DWM rounds and shadows the
               popup natively and a second CSS curve on top of that is
               what produces the double-border artifact at the corners. */
            QComboBox QAbstractItemView {{
                background-color: {t.aero_tint};
                color: {t.text_primary};
                border: none;
                selection-background-color: {t.accent};
                selection-color: {t.text_on_accent};
                outline: none;
                padding: {S.scale_size(4)}px;
                font-size: {S.scale_font(13)}px;
            }}
            /* Finger-sized rows — a 20px list row is unhittable on a
               smart board, and tall rows also give the flick gesture
               room to tell a drag from a tap. */
            QComboBox QAbstractItemView::item {{
                min-height: {S.scale_size(38)}px;
                padding-left: {S.scale_size(10)}px;
                border: none;
            }}
            QComboBox QAbstractItemView::item:hover {{
                background-color: {t.fill_hover};
            }}
            QPushButton#primary {{
                background-color: {t.accent_grad};
                color: {t.text_on_accent};
                border: none;
                border-radius: {S.scale_radius(12)}px;
                padding: {S.scale_size(12)}px;
                font-size: {S.scale_font(14.5)}px; font-weight: 700;
            }}
            QPushButton#primary:hover {{
                background-color: {t.accent_grad_hover};
            }}
            QPushButton#primary:pressed {{
                background-color: {t.accent_grad_pressed};
            }}
            QScrollArea {{ background: transparent; border: none; }}
        """)

        # ─── The tap catcher ──────────────────────────────────────────
        # Every widget in the wizard lives inside this frame, and the
        # frame paints a 1 %-alpha fill across the entire client area.
        #
        # Windows will not let a developer be happy about translucent
        # windows: ``WA_TranslucentBackground`` gives the dialog a
        # WS_EX_LAYERED window with per-pixel alpha, and Windows
        # hit-tests layered windows BY ALPHA. With the dialog painting
        # nothing, the only pixels that existed as far as the OS was
        # concerned were the inputs, the Continue button and the actual
        # glyphs of the text — everything else had alpha 0 and was a
        # hole. Confirmed with WindowFromPoint: taps on the card's
        # centre, its margins and the gaps between fields were all
        # handed to the window BEHIND the wizard. That window raised
        # itself, and the wizard — a frameless popup with no owner, and
        # nothing else of LogicBoard on screen this early in startup —
        # dropped out of sight. Which looks exactly like it minimised
        # itself.
        #
        # 1 % alpha is invisible to the eye and completely solid to the
        # hit tester, so the aero glass looks identical and every tap
        # now lands on the wizard. See ``tokens.hit_catcher``.
        catcher = QFrame(self)
        catcher.setObjectName("GlassCatcher")
        shell = QVBoxLayout(self)
        shell.setContentsMargins(0, 0, 0, 0)
        shell.setSpacing(0)
        shell.addWidget(catcher)

        # Content layout lives on the catcher frame.
        card_v = QVBoxLayout(catcher)
        card_v.setContentsMargins(
            S.scale_margin(36), S.scale_margin(28),
            S.scale_margin(36), S.scale_margin(24),
        )
        card_v.setSpacing(S.scale_spacing(12))

        # ─── Fixed header (logo + title + subtitle) ────────────────
        self._build_header(card_v, t)

        # ─── SCROLLABLE middle (warning + fields + error) ──────────
        # QScrollArea is the safety net that guarantees nothing clips or
        # stacks regardless of UI scale. On a normal 1× display the
        # content fits without scrolling; on a 0.912× scale (or
        # any smaller factor) the fields spill into scroll space
        # instead of overlapping each other.
        scroll = QScrollArea()
        scroll.setWidgetResizable(True)
        scroll.setFrameShape(QFrame.NoFrame)
        scroll.setHorizontalScrollBarPolicy(Qt.ScrollBarAlwaysOff)
        try:
            from ui.util.touch_scroll import setup_scroll
            setup_scroll(scroll)
        except Exception:
            pass

        scroll_body = QWidget()
        scroll_body.setStyleSheet("background: transparent;")
        body = QVBoxLayout(scroll_body)
        body.setContentsMargins(0, 0, 0, 0)
        body.setSpacing(S.scale_spacing(11))

        # Warning banner.
        body.addWidget(self._build_warning_banner(t))
        body.addSpacing(S.scale_spacing(8))

        # Form fields.
        self._build_fields(body, t)

        # Error label (hidden until validation fails).
        self._error = QLabel("")
        self._error.setWordWrap(True)
        self._error.setStyleSheet(
            f"font-size: {S.scale_font(11.5)}px; font-weight: 600; "
            f"color: {t.danger_text};")
        self._error.setVisible(False)
        body.addWidget(self._error)

        body.addStretch(1)
        scroll.setWidget(scroll_body)
        card_v.addWidget(scroll, 1)  # stretch — scroll region is elastic

        # ─── Fixed Continue button at the bottom ───────────────────
        # OUTSIDE the scroll region so it's always visible regardless
        # of scroll position.
        cont = QPushButton("Continue")
        cont.setObjectName("primary")
        cont.setFixedHeight(S.scale_size(48))
        cont.setCursor(Qt.CursorShape.PointingHandCursor)
        cont.clicked.connect(self._on_continue)
        press_squish(cont)
        card_v.addWidget(cont)

        self._room.setFocus()

    # =====================================================================
    # Header
    # =====================================================================
    def _build_header(self, root: QVBoxLayout, t) -> None:
        # Logo — smaller than before (72 vs. 96) so the header block
        # takes less vertical space and leaves more room for the four
        # form fields below at any scale.
        logo_sz = S.scale_size(72)
        logo = QLabel()
        logo.setAlignment(Qt.AlignmentFlag.AlignCenter)
        logo.setFixedSize(logo_sz, logo_sz)
        logo.setStyleSheet("background: transparent; border: none;")
        from ui.util.icon_loader import app_logo_pixmap
        pm = app_logo_pixmap(logo_sz)
        if pm and not pm.isNull():
            logo.setPixmap(pm)
        else:
            logo.setText("LB")
            logo.setStyleSheet(
                f"font-size: {S.scale_font(36)}px; font-weight: 700; "
                f"color: {t.text_primary}; background: transparent; "
                f"border: none;")
        root.addWidget(logo, 0, Qt.AlignmentFlag.AlignHCenter)

        root.addSpacing(S.scale_spacing(10))

        title = QLabel("LogicShare Setup ✨")
        title.setAlignment(Qt.AlignmentFlag.AlignCenter)
        title.setStyleSheet(
            f"font-size: {S.scale_font(22)}px; font-weight: 800; "
            f"color: {t.text_primary}; background: transparent;")
        root.addWidget(title)

        sub = QLabel("Just a few details and your smart board is ready to go 😎")
        sub.setAlignment(Qt.AlignmentFlag.AlignCenter)
        sub.setWordWrap(True)
        sub.setStyleSheet(
            f"font-size: {S.scale_font(12.5)}px; "
            f"color: {t.text_secondary}; background: transparent;")
        root.addWidget(sub)

    # =====================================================================
    # Warning banner (unchanged design — this part was working)
    # =====================================================================
    def _build_warning_banner(self, t) -> QFrame:
        banner = QFrame()
        banner.setObjectName("SetupWarnBanner")
        # Straight off the tokens. These two lines used to be
        # ``hasattr(t, "warning_bg")`` guards with literal fallbacks — dead
        # branches, since the tokens have carried both for as long as the
        # warning palette has existed, and the only thing they achieved was
        # keeping a hardcoded amber in the file.
        bg = t.warning_bg
        border = t.warning_border
        banner.setStyleSheet(
            f"QFrame#SetupWarnBanner {{ background: {bg}; "
            f"border: 1px solid {border}; "
            f"border-radius: {S.scale_radius(14)}px; }} "
            f"QFrame#SetupWarnBanner QLabel {{ background: transparent; "
            f"border: none; }}")
        bl = QHBoxLayout(banner)
        bl.setContentsMargins(
            S.scale_margin(16), S.scale_margin(14),
            S.scale_margin(16), S.scale_margin(14))
        bl.setSpacing(S.scale_spacing(12))

        badge_sz = S.scale_size(32)
        warn_icon = QLabel("⚠")
        warn_icon.setFixedSize(badge_sz, badge_sz)
        warn_icon.setAlignment(Qt.AlignmentFlag.AlignCenter)
        warn_icon.setStyleSheet(
            f"font-size: {S.scale_font(15)}px; color: {t.warning}; "
            f"background: {t.warning_bg}; "
            f"border-radius: {badge_sz // 2}px;")
        bl.addWidget(warn_icon, 0, Qt.AlignmentFlag.AlignTop)

        # Single rich-text label for the two lines (previously bugged as
        # nested vbox — see comment history — kept as one rich-text
        # QLabel because that architecture can't overlap itself).
        title_color = t.warning_text
        body_color = t.text_secondary
        title_px = int(S.scale_font(12.5))
        body_px = int(S.scale_font(11.5))
        warn_html = (
            f'<div style="font-size:{title_px}px; font-weight:700; '
            f'color:{title_color}; margin-bottom:{S.scale_size(3)}px;">'
            f'Teacher setup required</div>'
            f'<div style="font-size:{body_px}px; font-weight:500; '
            f'color:{body_color}; line-height:135%;">'
            f'These details should be filled in by the class teacher '
            f'before using LogicShare.</div>'
        )
        warn_txt = QLabel(warn_html)
        warn_txt.setTextFormat(Qt.TextFormat.RichText)
        warn_txt.setWordWrap(True)
        warn_txt.setAlignment(Qt.AlignmentFlag.AlignLeft
                                | Qt.AlignmentFlag.AlignVCenter)
        bl.addWidget(warn_txt, 1)
        return banner

    # =====================================================================
    # Form fields
    # =====================================================================
    def _build_fields(self, body: QVBoxLayout, t) -> None:
        def field_caption(text: str) -> QLabel:
            c = QLabel(text)
            c.setStyleSheet(
                f"font-size: {S.scale_font(11)}px; font-weight: 700; "
                f"letter-spacing: 0.6px; color: {t.text_secondary};")
            return c

        field_h = S.scale_size(40)

        # Room number.
        body.addWidget(field_caption("ROOM NUMBER"))
        self._room = QLineEdit()
        self._room.setPlaceholderText("e.g. 227")
        self._room.setMaxLength(3)
        self._room.setValidator(
            QRegularExpressionValidator(QRegularExpression(r"\d{0,3}")))
        self._room.setFixedHeight(field_h)
        self._room.textChanged.connect(self._clear_error)
        body.addWidget(self._room)

        # Class standard.
        body.addWidget(field_caption("CLASS STANDARD"))
        self._class = QComboBox()
        self._class.addItems(CLASS_STANDARDS)
        self._class.setCurrentIndex(-1)
        self._class.setPlaceholderText("Select class standard")
        self._class.setFixedHeight(field_h)
        self._class.currentIndexChanged.connect(self._clear_error)
        setup_combo_popup(self._class)
        body.addWidget(self._class)

        # Section.
        body.addWidget(field_caption("SECTION"))
        self._section = QComboBox()
        self._section.addItems(SECTIONS)
        self._section.setCurrentIndex(-1)
        self._section.setPlaceholderText("Select section")
        self._section.setFixedHeight(field_h)
        self._section.currentIndexChanged.connect(self._clear_error)
        setup_combo_popup(self._section)
        body.addWidget(self._section)

        # Password.
        body.addWidget(field_caption("PASSWORD"))
        self._pw = QLineEdit()
        self._pw.setEchoMode(QLineEdit.EchoMode.Password)
        self._pw.setPlaceholderText("Set a password")
        self._pw.setFixedHeight(field_h)
        self._pw.textChanged.connect(self._clear_error)
        body.addWidget(self._pw)

    # =====================================================================
    # Validation + persistence
    # =====================================================================
    def _clear_error(self, *_):
        if self._error.isVisible():
            self._error.setVisible(False)

    def _show_error(self, msg: str):
        self._error.setText(msg)
        self._error.setVisible(True)

    def _on_continue(self):
        room = self._room.text().strip()
        std = (self._class.currentText().strip()
                if self._class.currentIndex() >= 0 else "")
        sec = (self._section.currentText().strip()
                if self._section.currentIndex() >= 0 else "")
        pw = self._pw.text()

        if not validate_room(room):
            self._show_error("Room number must be exactly 3 digits.")
            self._room.setFocus(); return
        if not std:
            self._show_error("Select a class standard.")
            self._class.setFocus(); return
        if not sec:
            self._show_error("Select a section.")
            self._section.setFocus(); return
        if not pw:
            self._show_error("Set a password to continue.")
            self._pw.setFocus(); return

        try:
            ok, err = self._config.apply_setup(
                room=room, class_standard=std,
                section=sec, password=pw,
            )
        except Exception as e:
            self._show_error(f"Couldn't save settings: {e}")
            return
        if not ok:
            self._show_error(err or "Setup failed.")
            return
        self.accept()

    # =====================================================================
    # Lifecycle
    # =====================================================================
    def _center_on_display(self) -> None:
        """Put the wizard at the dead centre of the physical display.

        ``geometry()`` rather than ``availableGeometry()`` on purpose —
        the brief is the centre of the WHOLE display, not the centre of
        the space left over above the taskbar, which sits the card
        ~24 px high on a 1536×864 board.

        The screen is resolved from the cursor first. ``self.screen()``
        answers with whichever screen Qt happened to place the window on
        before it was positioned, so on a two-display setup (laptop +
        smart board) it can centre the wizard on the display the teacher
        isn't looking at. The cursor is on the board they're touching.
        """
        try:
            scr = QGuiApplication.screenAt(QCursor.pos()) \
                  or self.screen() or QGuiApplication.primaryScreen()
            if scr is None:
                return
            geo = scr.geometry()
            pos = geo.center() - self.rect().center()
            # Never let a card taller than the screen hang off the top —
            # its header would be unreachable.
            pos.setX(max(geo.left(), pos.x()))
            pos.setY(max(geo.top(), pos.y()))
            self.move(pos)
        except Exception:
            pass

    def changeEvent(self, event):
        """Refuse to be minimised.

        The window's own transparent pixels used to hand taps to the
        window behind it (fixed by the ``GlassCatcher`` frame),
        but a frameless first-run dialog can still be sent away by
        anything that reaches Windows directly — the taskbar button,
        Win+D, a stray edge gesture on the board. There is nothing to
        go back to at this point in startup: no main window, no tray, no
        title bar to click. So if the window state ever picks up
        ``WindowMinimized``, drop it again immediately and come back.
        """
        if event.type() == QEvent.Type.WindowStateChange:
            if self.windowState() & Qt.WindowState.WindowMinimized:
                self.setWindowState(
                    self.windowState() & ~Qt.WindowState.WindowMinimized)
                self.showNormal()
                self.raise_()
                self.activateWindow()
                self._center_on_display()
        super().changeEvent(event)

    def showEvent(self, event):
        super().showEvent(event)
        self._center_on_display()
        if not getattr(self, "_aero_applied", False):
            try: apply_aero(self)
            except Exception: pass
            try: enable_shadow(self)
            except Exception: pass
            if apply_rounded_corners is not None:
                try: apply_rounded_corners(self, mode="round")
                except Exception: pass
            self._aero_applied = True
        # The wizard is the only thing on screen at first run and it has
        # no taskbar entry worth hunting for — make sure it starts with
        # focus rather than behind whatever the teacher had open.
        self.raise_()
        self.activateWindow()