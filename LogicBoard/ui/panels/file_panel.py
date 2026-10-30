"""
file_panel.py — the board manager.

A floating, aero-backed popover showing every saved board as a card, with
New / Save / Save As / Open along the bottom.

Layout notes
------------
TWO COLUMNS of tall cards, each with a thumbnail hero above the name. This
replaced a single column of 72 px rows whose 42 px logo was the smallest
thing on screen and read as pixelated — because it was: the row was giving
its icon well under half its own height, and no amount of resampling makes
42 logical pixels of a detailed mark look deliberate.

A grid was tried once before and abandoned, for a reason worth writing down
so it is not repeated: 230 px tiles truncated "Class 11 Chapter 4 —
Thermodynamics" to about three words. The fix is not "go back to rows", it
is to stop making the tiles narrow. Two columns in a 640 px panel gives each
card ~285 px — wider than the OLD ROW's text column was, once the row's icon
and delete button had taken their share — so the name is better off here
than it was in the list, and the hero has room for a 84 px logo.

The hero is where the metadata used to compete with the name for the same
line. Now the card reads top to bottom: what it looks like, what it is
called, when it was saved and how big it is.

Touch first
-----------
This runs on a smart board, so nothing is hover-only. The delete button on
each card is always visible rather than appearing under a cursor that does
not exist, and every target is at least 32 px square. The same reasoning is
why the search box is a real field rather than a slide-out.
"""

from __future__ import annotations

import shutil
import logging
from datetime import datetime
from pathlib import Path
from typing import Optional, Callable

from PySide6.QtWidgets import (
    QDialog,
    QVBoxLayout,
    QHBoxLayout,
    QLabel,
    QLineEdit,
    QPushButton,
    QFrame,
    QScrollArea,
    QWidget,
    QInputDialog,
    QSizePolicy,
    QGridLayout,
)
from ui.panels.aero_alert import AeroAlert
from PySide6.QtCore import Qt, QPoint, QTimer
from PySide6.QtGui  import QPixmap

from ui.util.size_manager import size_mgr as S
from ui.util.audio       import click_sound
from ui.util.paths       import saves_dir, find_app_icon_path
from ui.util.aero     import (apply_aero, enable_shadow,
                                  prepare_aero_widget)
from ui.util.animations  import scale_in
from ui.theme.theme_manager import theme_mgr

__all__ = ["FilePanel", "saved_boards"]


# ── Grid geometry ────────────────────────────────────────────────────────
# All in REFERENCE pixels — every use goes through one of size_mgr's
# semantic helpers, so these are the numbers for a 1920x1080 board and
# shrink or grow from there.
#
# The panel is 640 rather than the list's 560 because two columns of usable
# card need the width: 640 less the 24 px side margins, the 12 px gutter and
# the scrollbar's 8 leaves ~285 per card. Narrower than that and the names
# start losing words, which is the failure mode the old single-column list
# existed to avoid.
_PANEL_W  = 640
_GRID_COLS = 2
# The viewport is pinned to this many card rows WHATEVER is in it — see the
# note in __init__. Two is the most that fits above the toolbar at the
# smallest scale factor the SizeManager clamps to.
_GRID_ROWS = 2
_TILE_H   = 186
_HERO_H   = 116
# 96, not 42. The logo is the thing being identified, so it gets the space.
# The artwork is 250 x 213 of ink inside its square frame, so 96 across is
# 82 tall — it fills the hero's 100 px of usable height without touching the
# padding, and it is resampled DOWN from logo.ico's 256 master rather than
# up from a small frame. See icon_loader.app_logo_pixmap for why that
# distinction is the whole difference between crisp and mush.
_LOGO_PX  = 96


def _lb_card_pixmap(size_px: int) -> Optional[QPixmap]:
    """The LogicBoard logo for a board row, at ``size_px`` LOGICAL pixels.

    Delegates to ``icon_loader.app_logo_pixmap``, which owns the caching and
    — the part that matters here — reads the RIGHT frame out of the
    multi-resolution logo.ico. This function used to build the pixmap itself
    with ``QPixmap(icon_path)``, which silently loaded the 16x16 frame and
    then blew it up to card size. Device pixel ratio was handled correctly
    all along; the source image was the problem.

    Returns ``None`` if the icon can't be loaded, so the caller falls back to
    an emoji rather than an empty box.
    """
    from ui.util.icon_loader import app_logo_pixmap
    return app_logo_pixmap(size_px)


class _ElidingLabel(QLabel):
    """A QLabel that truncates with an ellipsis instead of demanding width.

    "Class 11-A Thermodynamics Chapter 4" is about 520 px of text. A plain
    QLabel reports that as its minimum width, so the row grew to fit it, and
    the row's delete button ended up 80 px outside the panel — which is
    exactly what the first cut of this layout did on a realistic board name.

    An Ignored horizontal policy stops the label pushing its row around, and
    the resize handler re-elides to whatever width it was actually given. The
    full text stays in the tooltip, so a truncated name is still readable.
    """

    def __init__(self, text: str = "", parent=None) -> None:
        super().__init__(text, parent)
        self._full = text
        self.setSizePolicy(QSizePolicy.Ignored, QSizePolicy.Preferred)
        self.setToolTip(text)

    def setText(self, text: str) -> None:      # noqa: N802 (Qt casing)
        self._full = text
        self.setToolTip(text)
        self._elide()

    def resizeEvent(self, e) -> None:          # noqa: N802 (Qt casing)
        super().resizeEvent(e)
        self._elide()

    def _elide(self) -> None:
        # fontMetrics(), not a metric built from the stylesheet's px value —
        # this reads the font the widget ENDED UP with after Qt polished the
        # stylesheet, which is the one being painted.
        fm = self.fontMetrics()
        QLabel.setText(
            self, fm.elidedText(self._full, Qt.ElideRight, max(0, self.width())))


def _human_size(n: int) -> str:
    if n < 1024:
        return f"{n} B"
    if n < 1024 * 1024:
        return f"{n / 1024:.0f} KB"
    return f"{n / (1024 * 1024):.1f} MB"


def _when(ts: float) -> str:
    """"Saved" time, phrased the way somebody would say it out loud.

    Absolute timestamps are precise and useless at a glance: what a teacher
    wants from this list is "the one I did last lesson", and "2 days ago"
    answers that where "2026-08-23 14:07" makes them do arithmetic.
    """
    try:
        now = datetime.now()
        then = datetime.fromtimestamp(ts)
        delta = now - then
        secs = delta.total_seconds()
        if secs < 60:
            return "just now"
        if secs < 3600:
            m = int(secs // 60)
            return f"{m} minute{'s' if m != 1 else ''} ago"
        if then.date() == now.date():
            return f"today at {then.strftime('%H:%M')}"
        if (now.date() - then.date()).days == 1:
            return f"yesterday at {then.strftime('%H:%M')}"
        if delta.days < 7:
            return f"{delta.days} days ago"
        return then.strftime("%d %b %Y")
    except Exception:
        return ""


def saved_boards() -> list[Path]:
    """Every board on disk, newest first.

    Newest first, not alphabetical: the board somebody wants is almost
    always the one they were last working on, and it should not be
    somewhere in the middle of the alphabet. Shared with the "Open
    with LogicBoard" picker, so both list the same boards in the same
    order.
    """
    sd = saves_dir()
    # The listing contains TWO kinds of entries:
    #   • Modern:  *.lb files               (single-file boards)
    #   • Legacy:  folders with board.json  (offered for migration)
    # Hidden working dirs (".working") and the legacy "temps" folder are
    # filtered out so they don't pollute the user-facing list.
    from ui.util.lb_format import LB_EXTENSION, is_legacy_board_folder
    entries: list[Path] = []
    try:
        for p in sd.iterdir():
            if p.name.startswith("."):
                continue        # skip .working and other dotfiles
            if p.is_file() and p.suffix.lower() == LB_EXTENSION:
                entries.append(p)
            elif p.is_dir() and p.name != "temps" and is_legacy_board_folder(p):
                entries.append(p)
    except OSError as e:
        logging.warning("Could not read the saves folder: %s", e)
        return []

    def sort_key(p: Path):
        try:
            return -p.stat().st_mtime
        except OSError:
            return 0.0
    return sorted(entries, key=sort_key)


class FilePanel(QDialog):
    """Floating board management panel."""

    def __init__(
        self,
        parent=None,
        *,
        on_new: Optional[Callable] = None,
        on_save: Optional[Callable] = None,
        on_save_as: Optional[Callable[[str], bool]] = None,
        on_open: Optional[Callable[[Path], None]] = None,
        on_delete: Optional[Callable[[Path], None]] = None,
        current_board_path: Optional[Path] = None,
    ) -> None:
        super().__init__(
            parent,
            Qt.Tool | Qt.FramelessWindowHint | Qt.WindowStaysOnTopHint
        )
        self.setModal(False)
        # CRITICAL: must run BEFORE show() so Win32 can blur into the
        # translucent Qt surface. Without this the panel is just a hole.
        prepare_aero_widget(self)

        self._on_new = on_new
        self._on_save = on_save
        self._on_save_as = on_save_as
        self._on_open = on_open
        self._on_delete = on_delete
        self._selected_path: Optional[Path] = None
        self._current_board_path = current_board_path
        self._board_buttons: list[tuple[Path, QWidget]] = []
        self._filter = ""

        t = theme_mgr.tokens

        # The outer QDialog background uses bg_overlay — now translucent in
        # tokens, so the Win32 aero blur is what actually renders behind.
        #
        # border: none, and no stroked outline either. DWM already clips
        # and rounds this window and the drop shadow separates it from the
        # canvas, so a 1 px rounded rect painted inside the client area can
        # only disagree with the real corner — it used this panel's radius
        # while the compositor clips at the system one, leaving an arc
        # cutting across each corner. border-radius stays: it clips the fill.
        self.setStyleSheet(f"""
            QDialog {{
                background-color: transparent;
                border: none;
                border-radius: {t.radius_xl}px;
            }}
        """)

        container = QWidget(self)
        container.setFixedWidth(S.scale_size(_PANEL_W))
        container.setStyleSheet("background: transparent;")

        main = QVBoxLayout(container)
        main.setContentsMargins(S.scale_margin(t.space_2xl),
                                S.scale_margin(t.space_xl),
                                S.scale_margin(t.space_2xl),
                                S.scale_margin(t.space_xl))
        main.setSpacing(S.scale_spacing(t.space_md))

        main.addLayout(self._build_header())
        main.addWidget(self._build_search())

        # ---- Scrollable list of boards ----
        scroll = QScrollArea()
        scroll.setWidgetResizable(True)
        scroll.setFrameShape(QFrame.NoFrame)
        # ONE height, always — not a minimum and a maximum with the board
        # count deciding where between them the panel lands. Sizing it to
        # the content meant the panel roughly doubled in height between the
        # second board and the third, and a popover that is a different
        # size every time you open it reads as a different panel. It also
        # moves: this thing is anchored ABOVE the toolbar button that opens
        # it (see show_anchored), so every pixel the panel grows is a pixel
        # the New / Save / Open row travels up the wall, out from under the
        # hand already reaching for it.
        #
        # Exactly two card rows and the gutter between them, built from the
        # same two calls the grid itself uses — so the second row ends flush
        # with the bottom of the viewport instead of a rounding error inside
        # or outside it. Anything past four boards scrolls.
        viewport_h = (S.scale_size(_TILE_H) * _GRID_ROWS
                      + S.scale_spacing(t.space_md) * (_GRID_ROWS - 1))
        scroll.setFixedHeight(viewport_h)
        from ui.util.touch_scroll import setup_scroll
        setup_scroll(scroll)   # modern scrollbar + touch flick
        main.addWidget(scroll, 1)

        inner = QWidget()
        inner.setStyleSheet("background: transparent;")
        self._grid = QGridLayout(inner)
        # The right margin is scrollbar clearance: without it the last
        # column sits under the overlay scrollbar and its delete button is
        # the part that ends up underneath.
        self._grid.setContentsMargins(0, 0, S.scale_margin(t.space_sm), 0)
        self._grid.setHorizontalSpacing(S.scale_spacing(t.space_md))
        self._grid.setVerticalSpacing(S.scale_spacing(t.space_md))
        self._grid.setAlignment(Qt.AlignTop)
        # Equal stretch on every column, so a half-full last row leaves its
        # cards the same width as a full one rather than letting them
        # spread to fill the gap.
        for c in range(_GRID_COLS):
            self._grid.setColumnStretch(c, 1)
        scroll.setWidget(inner)

        main.addLayout(self._build_footer())

        self._populate_boards()

        popup_layout = QVBoxLayout(self)
        popup_layout.setContentsMargins(0, 0, 0, 0)
        popup_layout.addWidget(container)
        self.adjustSize()

    # ================================================================
    # Chrome
    # ================================================================

    def _build_header(self) -> QHBoxLayout:
        t = theme_mgr.tokens

        row = QHBoxLayout()
        row.setSpacing(S.scale_spacing(t.space_sm))

        titles = QVBoxLayout()
        titles.setSpacing(0)
        title = QLabel("Boards")
        title.setStyleSheet(
            f"font-size: {S.scale_font(t.font_title)}px; font-weight: 700; "
            f"color: {t.text_primary}; border: none; background: transparent;")
        titles.addWidget(title)
        # The count lives under the title rather than in it, so the heading
        # does not change width every time a board is saved or deleted.
        self._count_lbl = QLabel("")
        self._count_lbl.setStyleSheet(
            f"font-size: {S.scale_font(t.font_caption)}px; "
            f"color: {t.text_secondary}; "
            f"border: none; background: transparent;")
        titles.addWidget(self._count_lbl)
        row.addLayout(titles)
        row.addStretch(1)

        close = QPushButton("✕")
        close_px = S.scale_size(t.control_h_sm)
        close.setFixedSize(close_px, close_px)
        close.setCursor(Qt.PointingHandCursor)
        close.setToolTip("Close")
        close.setStyleSheet(f"""
            QPushButton {{
                background: transparent; color: {t.text_secondary};
                border: none; border-radius: {close_px // 2}px;
                font-size: {S.scale_font(13)}px;
            }}
            QPushButton:hover {{
                background: {t.fill_hover}; color: {t.text_primary};
            }}
        """)
        close.clicked.connect(self._act_close)
        row.addWidget(close, 0, Qt.AlignTop)
        return row

    def _build_search(self) -> QWidget:
        t = theme_mgr.tokens
        box = QLineEdit()
        box.setPlaceholderText("Search boards")
        box.setClearButtonEnabled(True)
        box.setFixedHeight(S.scale_size(t.control_h))
        box.setStyleSheet(f"""
            QLineEdit {{
                background: {t.bg_input};
                color: {t.text_primary};
                border: 1px solid {t.border};
                border-radius: {S.scale_radius(t.radius_sm)}px;
                padding: 0 {S.scale_margin(t.space_md)}px;
                font-size: {S.scale_font(t.font_body)}px;
                selection-background-color: {t.accent};
            }}
            QLineEdit:focus {{ border: 1px solid {t.accent}; }}
        """)
        box.textChanged.connect(self._on_search)
        self._search = box
        return box

    def _build_footer(self) -> QHBoxLayout:
        t = theme_mgr.tokens

        secondary = f"""
            QPushButton {{
                background: {t.fill_default};
                color: {t.text_primary};
                border: 1px solid {t.border};
                border-radius: {S.scale_radius(t.radius_sm)}px;
                font-weight: 600;
                font-size: {S.scale_font(t.font_body)}px;
                padding: 0 {S.scale_margin(t.space_md)}px;
            }}
            QPushButton:hover  {{ background: {t.fill_hover};
                                  border: 1px solid {t.border_strong}; }}
            QPushButton:pressed {{ background: {t.fill_pressed}; }}
        """
        self._primary_css = f"""
            QPushButton {{
                background: {t.accent_grad};
                color: {t.text_on_accent};
                border: none;
                border-radius: {S.scale_radius(t.radius_sm)}px;
                font-weight: 700;
                font-size: {S.scale_font(t.font_body)}px;
                padding: 0 {S.scale_margin(t.space_lg)}px;
            }}
            QPushButton:hover   {{ background: {t.accent_grad_hover}; }}
            QPushButton:pressed {{ background: {t.accent_grad_pressed}; }}
            QPushButton:disabled {{
                background: {t.fill_disabled};
                color: {t.text_disabled};
            }}
        """

        # The four buttons SHARE the row rather than being pushed to
        # opposite ends. The left/right split is the usual dialog shape, but
        # at this width it left a hand's breadth of nothing in the middle.
        # Open still reads as the primary action — it is the accent one, and
        # it gets half again the width of the others.
        row = QHBoxLayout()
        row.setSpacing(S.scale_spacing(t.space_sm))
        for label, func in (("New", self._act_new),
                            ("Save", self._act_save),
                            ("Save As", self._act_save_as)):
            b = QPushButton(label)
            b.setFixedHeight(S.scale_size(t.control_h_lg))
            b.setStyleSheet(secondary)
            b.setCursor(Qt.PointingHandCursor)
            b.clicked.connect(func)
            row.addWidget(b, 2)

        self._open_btn = QPushButton("Open")
        self._open_btn.setFixedHeight(S.scale_size(t.control_h_lg))
        self._open_btn.setStyleSheet(self._primary_css)
        self._open_btn.setCursor(Qt.PointingHandCursor)
        self._open_btn.clicked.connect(self._act_open)
        # Disabled until a board is picked. The old panel let you press Open
        # with nothing selected, which did nothing at all and looked broken.
        self._open_btn.setEnabled(False)
        row.addWidget(self._open_btn, 3)
        return row

    # ================================================================
    # Board list
    # ================================================================

    def _all_boards(self) -> list[Path]:
        return saved_boards()

    def _populate_boards(self) -> None:
        t = theme_mgr.tokens

        while self._grid.count():
            item = self._grid.takeAt(0)
            w = item.widget()
            if w:
                w.setParent(None)
                w.deleteLater()
        self._board_buttons.clear()

        every = self._all_boards()
        needle = self._filter.strip().lower()
        boards = [p for p in every
                  if not needle or needle in p.stem.lower()]

        n = len(every)
        self._count_lbl.setText(
            "No boards saved yet" if n == 0
            else f"{n} board{'s' if n != 1 else ''} saved")

        if not boards:
            # Spans every column: an empty state centred in the left half of
            # the panel reads as a card that failed to load.
            self._grid.addWidget(self._empty_state(searching=bool(needle)),
                                 0, 0, 1, _GRID_COLS)
            self._refresh_selection()
            return

        # Keep a selection that still exists; otherwise pick the newest, so
        # Open is live the moment the panel appears.
        if self._selected_path is not None and self._selected_path not in boards:
            self._selected_path = None
        if self._selected_path is None:
            self._selected_path = boards[0]

        for i, entry in enumerate(boards):
            card = self._make_card(entry)
            self._board_buttons.append((entry, card))
            self._grid.addWidget(card, i // _GRID_COLS, i % _GRID_COLS)

        self._refresh_selection()

    def _empty_state(self, *, searching: bool) -> QWidget:
        """Two different nothings, said two different ways.

        "No boards yet" and "nothing matches what you typed" call for
        completely different next actions, and one message for both leaves
        the user wondering whether their work is gone.
        """
        t = theme_mgr.tokens
        host = QWidget()
        host.setStyleSheet("background: transparent;")
        v = QVBoxLayout(host)
        v.setContentsMargins(0, S.scale_margin(60), 0, S.scale_margin(60))
        v.setSpacing(S.scale_spacing(t.space_sm))

        icon = QLabel("🔍" if searching else "📋")
        icon.setAlignment(Qt.AlignCenter)
        icon.setStyleSheet(
            f"font-size: {S.scale_font(30)}px; "
            f"border: none; background: transparent;")
        v.addWidget(icon)

        head = QLabel("Nothing matches that" if searching
                      else "No saved boards yet")
        head.setAlignment(Qt.AlignCenter)
        head.setStyleSheet(
            f"font-size: {S.scale_font(t.font_subtitle)}px; font-weight: 700; "
            f"color: {t.text_primary}; border: none; background: transparent;")
        v.addWidget(head)

        sub = QLabel("Try a different search."  if searching else
                     "Save the board you're working on and it'll show up here.")
        sub.setAlignment(Qt.AlignCenter)
        sub.setWordWrap(True)
        sub.setStyleSheet(
            f"font-size: {S.scale_font(t.font_caption)}px; "
            f"color: {t.text_secondary}; "
            f"border: none; background: transparent;")
        v.addWidget(sub)
        return host

    def _make_card(self, entry: Path) -> QFrame:
        """One grid card, for either a .lb file or a legacy folder.

        Reads top to bottom — hero, name, metadata — rather than left to
        right, which is what lets the logo be 84 px instead of the 42 it got
        when it was sharing a 72 px row with two lines of text and a delete
        button.
        """
        t = theme_mgr.tokens
        from ui.util.lb_format import LB_EXTENSION

        is_lb = entry.is_file() and entry.suffix.lower() == LB_EXTENSION
        # Display name strips the .lb extension so the user sees just the
        # board name (the extension is an implementation detail).
        display_name = entry.stem if is_lb else entry.name
        is_current = (self._current_board_path is not None
                      and Path(entry) == Path(self._current_board_path))

        # -- Metadata line ------------------------------------------------
        # Its own line under the name, so neither has to give up width to
        # the other. This is what the pre-list two-column tiles had nowhere
        # to put; a 182 px tall card has somewhere.
        bits: list[str] = []
        try:
            st = entry.stat()
            when = _when(st.st_mtime)
            if when:
                bits.append(when)
            if is_lb:
                bits.append(_human_size(st.st_size))
        except OSError:
            pass
        if not is_lb:
            bits.append("older folder format")
        subtitle = "  ·  ".join(bits)

        card = QFrame()
        card.setObjectName("BoardTile")
        card.setFixedHeight(S.scale_size(_TILE_H))
        card.setSizePolicy(QSizePolicy.Expanding, QSizePolicy.Fixed)
        card.setCursor(Qt.PointingHandCursor)

        card_layout = QVBoxLayout(card)
        card_layout.setContentsMargins(S.scale_margin(t.space_sm),
                                       S.scale_margin(t.space_sm),
                                       S.scale_margin(t.space_sm),
                                       S.scale_margin(t.space_md))
        card_layout.setSpacing(S.scale_spacing(t.space_sm))

        # --- Hero ---------------------------------------------------------
        # The card's picture area. A QGridLayout with everything in cell
        # (0, 0) so the chip and the delete button OVERLAY the artwork
        # instead of stealing rows from it - Qt is happy to stack items in
        # one cell, and alignment flags decide which corner each lands in.
        # Doing it with manual geometry instead would need a resizeEvent to
        # stay correct as the column width changes.
        hero = QFrame()
        hero.setObjectName("BoardHero")
        hero.setFixedHeight(S.scale_size(_HERO_H))
        hero_layout = QGridLayout(hero)
        hero_layout.setContentsMargins(S.scale_margin(t.space_sm),
                                       S.scale_margin(t.space_sm),
                                       S.scale_margin(t.space_sm),
                                       S.scale_margin(t.space_sm))
        hero_layout.setSpacing(0)

        # For .lb boards we show the actual LogicBoard logo instead of a
        # generic emoji - the file IS a LogicBoard document, so this makes
        # the card identifiable at a glance. Legacy folders keep the folder
        # emoji because they're a transitional format, not a first-class
        # .lb file.
        icon_lbl = QLabel()
        icon_lbl.setAlignment(Qt.AlignCenter)
        icon_lbl.setStyleSheet("border: none; background: transparent;")
        if is_lb:
            pm = _lb_card_pixmap(S.scale_icon(_LOGO_PX))
            if pm is not None and not pm.isNull():
                icon_lbl.setPixmap(pm)
            else:
                # Fallback if the icon file is missing - better a document
                # glyph than an empty box.
                icon_lbl.setText("📄")
                icon_lbl.setStyleSheet(
                    f"font-size: {S.scale_font(44)}px; border: none; "
                    f"background: transparent;")
        else:
            icon_lbl.setText("📁")
            icon_lbl.setStyleSheet(
                f"font-size: {S.scale_font(44)}px; border: none; "
                f"background: transparent;")
        hero_layout.addWidget(icon_lbl, 0, 0, Qt.AlignCenter)

        del_btn = QPushButton("🗑")
        del_px = S.scale_size(t.control_h_sm)
        del_btn.setFixedSize(del_px, del_px)
        del_btn.setCursor(Qt.PointingHandCursor)
        del_btn.setToolTip(f"Delete {display_name}")
        # A translucent dark disc, not a bare glyph: it sits ON the artwork
        # now, and an unbacked glyph over the logo's own light strokes is
        # unreadable at exactly the moment somebody is reaching for it.
        # fill_on_media darkens where the other fills lighten — see tokens.
        del_btn.setStyleSheet(f"""
            QPushButton {{
                background: {t.fill_on_media}; color: {t.text_secondary};
                border: none; border-radius: {del_px // 2}px;
                font-size: {S.scale_font(13)}px;
            }}
            QPushButton:hover {{
                background: {t.danger_bg}; color: {t.danger};
            }}
            QPushButton:pressed {{
                background: {t.danger_bg_hover}; color: {t.danger};
            }}
        """)
        del_btn.clicked.connect(lambda _, p=entry: self._act_delete(p))
        hero_layout.addWidget(del_btn, 0, 0, Qt.AlignTop | Qt.AlignRight)

        if is_current:
            # The board already on screen. Without this the grid gives no
            # clue which one you are looking at, and "Open" on the current
            # board is a no-op the user cannot predict.
            chip = QLabel("OPEN")
            chip.setStyleSheet(
                f"font-size: {S.scale_font(9)}px; font-weight: 800; "
                f"letter-spacing: 0.5px;"
                f"color: {t.accent}; background: {t.accent_muted};"
                f"border: none; border-radius: {S.scale_radius(t.radius_xs)}px;"
                f"padding: {S.scale_margin(3)}px {S.scale_margin(7)}px;")
            hero_layout.addWidget(chip, 0, 0, Qt.AlignBottom | Qt.AlignLeft)

        card_layout.addWidget(hero)

        # --- Name + metadata ----------------------------------------------
        info_layout = QVBoxLayout()
        info_gap = S.scale_margin(t.space_xs)
        info_layout.setContentsMargins(info_gap, 0, info_gap, 0)
        info_layout.setSpacing(S.scale_spacing(t.space_xxs))

        name_lbl = _ElidingLabel(display_name)
        name_lbl.setStyleSheet(
            f"font-size: {S.scale_font(t.font_subtitle)}px; font-weight: 600; "
            f"color: {t.text_primary}; border: none; background: transparent;")
        info_layout.addWidget(name_lbl)

        if subtitle:
            sub_lbl = _ElidingLabel(subtitle)
            sub_lbl.setStyleSheet(
                f"font-size: {S.scale_font(t.font_caption)}px; "
                f"color: {t.text_secondary}; "
                f"border: none; background: transparent;")
            info_layout.addWidget(sub_lbl)

        card_layout.addLayout(info_layout)
        # Absorbs the few pixels of slack in a fixed-height card, so a board
        # with no readable metadata does not get its hero and name pushed
        # apart by the spacing Qt would otherwise redistribute.
        card_layout.addStretch(1)

        def on_press(e, p=entry):
            if e.button() == Qt.LeftButton:
                click_sound.play_click()
                self._selected_path = p
                self._refresh_selection()
            e.accept()

        def on_dbl(e, p=entry):
            if e.button() == Qt.LeftButton:
                click_sound.play_click()
                self._selected_path = p
                self._act_open()
            e.accept()

        card.mousePressEvent = on_press
        card.mouseDoubleClickEvent = on_dbl

        return card

    def _refresh_selection(self) -> None:
        t = theme_mgr.tokens

        for path, card in self._board_buttons:
            is_sel = (self._selected_path is not None
                      and Path(path) == Path(self._selected_path))
            # Selection is carried by the fill and a full accent border
            # rather than by a border that thickens from 1px to 2px: a
            # widening border reflows the card's contents by a pixel, which
            # reads as a twitch every time you tap a different board.
            bg = t.accent_muted if is_sel else t.bg_card
            bc = t.accent if is_sel else t.border_subtle
            # The hero is styled from HERE rather than in _make_card, in the
            # same stylesheet as the card. A rule set on a widget applies to
            # its children too, so one setStyleSheet re-tints both surfaces
            # on selection; styling the hero separately meant tapping a card
            # left its picture area looking unselected.
            hero_top = t.accent_muted if is_sel else t.fill_hover
            hero_bot = t.fill_subtle
            hero_bc = t.accent_muted if is_sel else t.border_subtle
            card.setStyleSheet(f"""
                QFrame#BoardTile {{
                    background: {bg};
                    border: 1px solid {bc};
                    border-radius: {S.scale_radius(t.radius_lg)}px;
                }}
                QFrame#BoardTile:hover {{
                    background: {t.accent_muted if is_sel else t.tile_grad};
                    border: 1px solid {t.accent if is_sel else t.border_strong};
                }}
                QFrame#BoardHero {{
                    background: qlineargradient(
                        x1:0, y1:0, x2:0, y2:1,
                        stop:0 {hero_top}, stop:1 {hero_bot});
                    border: 1px solid {hero_bc};
                    border-radius: {S.scale_radius(t.radius_md)}px;
                }}
            """)

        has_sel = self._selected_path is not None and bool(self._board_buttons)
        try:
            self._open_btn.setEnabled(has_sel)
        except (AttributeError, RuntimeError):
            pass

    def _on_search(self, text: str) -> None:
        self._filter = text or ""
        self._populate_boards()

    # ================================================================
    # Actions
    # ================================================================

    def _act_close(self) -> None:
        click_sound.play_click()
        self.close()

    def _act_new(self) -> None:
        click_sound.play_click()
        if self._on_new:
            self._on_new()
        self.close()

    def _act_save(self) -> None:
        click_sound.play_click()
        if self._on_save:
            self._on_save()
        self.close()

    def _act_save_as(self) -> None:
        click_sound.play_click()
        text, ok = QInputDialog.getText(self, "Save As", "Board name:")
        if ok and text.strip():
            # Hand the raw text over. main_window cleans it through
            # lb_format.clean_board_name, which is the single place that
            # decides what a board may be called; sanitising here as well
            # meant the name was mangled twice by two different rules.
            if self._on_save_as:
                self._on_save_as(text)
        self.close()

    def _act_open(self) -> None:
        click_sound.play_click()
        if self._selected_path and self._on_open:
            self._on_open(self._selected_path)
        self.close()

    def _act_delete(self, path: Path) -> None:
        click_sound.play_click()
        from ui.util.lb_format import LB_EXTENSION
        is_lb = path.is_file() and path.suffix.lower() == LB_EXTENSION
        display_name = path.stem if is_lb else path.name

        reply = AeroAlert.question(
            self,
            "Delete Board",
            f"Delete '{display_name}' permanently?",
            AeroAlert.Yes | AeroAlert.No,
            AeroAlert.No,
        )

        if reply == AeroAlert.Yes:
            try:
                # .lb is a single file → unlink. Legacy folders → rmtree.
                if is_lb:
                    path.unlink(missing_ok=True)
                else:
                    shutil.rmtree(path, ignore_errors=True)
                logging.info(f"Board deleted: {display_name}")
            except Exception as e:
                logging.error(f"Failed to delete board: {e}")

            if self._on_delete:
                self._on_delete(path)

            if self._selected_path == path:
                self._selected_path = None

            self._populate_boards()

    # ================================================================
    # Keyboard
    # ================================================================

    def keyPressEvent(self, e) -> None:
        """Esc closes; Enter opens the highlighted board.

        Worth having even on a touch board: these panels get driven from a
        keyboard whenever one is plugged into the OPS panel, and a popover
        that ignores Esc is a popover somebody has to hunt for a way out of.
        """
        if e.key() == Qt.Key_Escape:
            self.close()
            return
        if e.key() in (Qt.Key_Return, Qt.Key_Enter):
            if self._selected_path is not None:
                self._act_open()
            return
        super().keyPressEvent(e)

    # ================================================================
    # Show anchored to a button
    # ================================================================

    def show_anchored(self, anchor: QWidget, offset: QPoint = QPoint(0, 0)) -> None:
        """Sit just above the button that opened it, centred on it.

        Positioned from the size the panel ACTUALLY took, the same way the
        pen and eraser panels do it. This used to move by a hard-coded
        (-100, -500), which is only correct for one panel height on one
        display scale — everywhere else it opened too low, overlapping the
        toolbar it was supposed to sit above.
        """
        self.adjustSize()
        size = self.size()
        x = -(size.width() // 2) + (anchor.width() // 2)
        y = -size.height() - S.scale_size(25)
        self.move(anchor.mapToGlobal(QPoint(x, y)) + offset)
        self.show()
        # apply_aero NEEDS a valid HWND, so call after show(). Combined
        # with prepare_aero_widget(self) in __init__, this is what gives
        # the panel real Win32 backdrop blur instead of a flat rectangle.
        apply_aero(self)
        enable_shadow(self)
        # Subtle Fluent pop animation on first reveal.
        scale_in(self)
        # Focus the search box so typing filters immediately — but only
        # after the pop animation, or the caret blinks mid-scale.
        QTimer.singleShot(160, self._focus_search)

    def _focus_search(self) -> None:
        try:
            self._search.setFocus(Qt.OtherFocusReason)
        except (AttributeError, RuntimeError):
            pass
