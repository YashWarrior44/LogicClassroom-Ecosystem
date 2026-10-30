"""
ui/logicshare/settings_section.py — LogicShare section for the Settings window
(spec §4) + password gates + History page (spec §7).

All built on LogicBoard's own card style + aero dialogs (prepare_aero_widget
/ apply_aero / UiScale / theme_mgr), so it slots into the existing Settings
panel as just another card.

Public:
  • LogicShareSettingsCard — the section card (status / device / room / class /
        connection + Restart · Change password · View history; shows the
        conflict message + Retry · Restart when disabled by a LAN conflict).
  • PasswordPromptDialog    — single hidden field, verifies the current password.
  • ChangePasswordDialog    — current → new → confirm.
  • HistoryDialog           — loads ls.json, filter by type + sort by time.
"""
from __future__ import annotations

import logging
import time

from PySide6.QtCore import Qt, QUrl
from PySide6.QtGui import QPixmap, QDesktopServices
from PySide6.QtWidgets import (
    QDialog, QWidget, QVBoxLayout, QHBoxLayout, QGridLayout, QLabel, QPushButton,
    QFrame, QLineEdit, QComboBox, QScrollArea, QApplication,
)

from ui.util.scaling     import UiScale
from ui.util.aero     import apply_aero, enable_shadow, prepare_aero_widget
from ui.util.animations  import fade_in
from ui.theme.theme_manager import theme_mgr
from ui.theme import fluent_styles
from ui.panels.aero_alert import AeroAlert

from .activity_log import (
    EVENT_LABELS, EVENT_FILE_RECEIVED, EVENT_ANNOUNCEMENT_RECEIVED,
)
from .config import CLASS_STANDARDS, SECTIONS, validate_room

# LogicBoard is RECEIVE-ONLY — only these two event types ever occur here.
_RECEIVED_EVENTS = (EVENT_FILE_RECEIVED, EVENT_ANNOUNCEMENT_RECEIVED)

# The padlock that marks a password-gated action. It lives here rather than
# inline because two places write these labels: make_btn() when the row is
# first built, and refresh() when the Stop/Start toggle flips. refresh()
# rebuilt the text from a bare literal and dropped the padlock, and it runs
# on every service-status change — so the button was created with a lock and
# had lost it before anyone looked at the panel, while Restart, Edit and
# Change password beside it kept theirs.
_LOCK = "🔒  "

__all__ = [
    "LogicShareSettingsCard", "PasswordPromptDialog",
    "ChangePasswordDialog", "EditClassDetailsDialog", "HistoryDialog",
]


def _fmt_time(ts: float) -> str:
    try:
        return time.strftime("%d %b %Y · %I:%M %p", time.localtime(ts)).replace(" 0", " ")
    except Exception:
        return ""


_EVENT_ICONS = {
    "file_received": "📥", "announcement_received": "📢",
    "file_sent": "📤", "announcement_sent": "📣",
}


# ===========================================================================
# Shared frameless-aero dialog base
# ===========================================================================
class _AeroDialog(QDialog):
    def __init__(self, parent=None, *, width=440, height=300, modal=True):
        super().__init__(parent, Qt.WindowType.FramelessWindowHint)
        self.setModal(modal)
        prepare_aero_widget(self)
        sc = UiScale.px
        self.setFixedSize(sc(width), sc(height))
        t = theme_mgr.tokens
        self.setStyleSheet(f"""
            QDialog {{
                background-color: {t.dialog_bg};
                border: 1px solid {t.stroke_default};
                border-radius: {t.radius_xl}px;
            }}
            QLabel {{ color: {t.text_primary}; background: transparent; border: none; }}
            {fluent_styles.input_field(tk=t)}
            QPushButton#secondary {{
                background-color: {t.fill_default}; color: {t.text_primary};
                border: 1px solid {t.stroke_default}; border-radius: {t.radius_sm}px;
                padding: {sc(9)}px {sc(18)}px; font-size: {sc(13)}px; font-weight: 600;
            }}
            QPushButton#secondary:hover {{ background: {t.bg_card_hover}; }}
            QPushButton#primary {{
                background-color: {t.accent_grad}; color: {t.text_on_accent};
                border: 1px solid {t.accent}; border-radius: {t.radius_sm}px;
                padding: {sc(9)}px {sc(18)}px; font-size: {sc(13)}px; font-weight: 700;
            }}
            QPushButton#primary:hover {{ background: {t.accent_grad_hover}; }}
            QPushButton#primary:pressed {{ background: {t.accent_grad_pressed}; }}
        """)
        try:
            geo = self.screen().geometry()
            self.move(geo.center() - self.rect().center())
        except Exception:
            pass

    def showEvent(self, e):
        super().showEvent(e)
        apply_aero(self); enable_shadow(self); fade_in(self)


# ===========================================================================
# Password prompt (verifies current password inline)
# ===========================================================================
class PasswordPromptDialog(_AeroDialog):
    def __init__(self, config, *, title="Enter password",
                 subtitle="Enter the LogicShare password to continue.", parent=None):
        super().__init__(parent, width=440, height=270)
        self._config = config
        sc = UiScale.px
        t = theme_mgr.tokens
        root = QVBoxLayout(self)
        root.setContentsMargins(sc(28), sc(24), sc(28), sc(22)); root.setSpacing(sc(12))

        h = QLabel(title)
        h.setStyleSheet(f"font-size: {sc(17)}px; font-weight: 800; color: {t.text_primary};")
        root.addWidget(h)
        s = QLabel(subtitle); s.setWordWrap(True)
        s.setStyleSheet(f"font-size: {sc(12.5)}px; color: {t.text_secondary};")
        root.addWidget(s)

        self._pw = QLineEdit(); self._pw.setEchoMode(QLineEdit.EchoMode.Password)
        self._pw.setPlaceholderText("Password"); self._pw.setFixedHeight(sc(40))
        self._pw.textChanged.connect(lambda *_: self._err.setVisible(False))
        self._pw.returnPressed.connect(self._on_ok)
        root.addWidget(self._pw)

        self._err = QLabel("That password doesn't match. 💀"); self._err.setVisible(False)
        self._err.setStyleSheet(f"font-size: {sc(11.5)}px; font-weight: 600; color: {t.danger_text};")
        root.addWidget(self._err)
        root.addStretch()

        brow = QHBoxLayout(); brow.addStretch(); brow.setSpacing(sc(10))
        cancel = QPushButton("Cancel"); cancel.setObjectName("secondary")
        cancel.setCursor(Qt.CursorShape.PointingHandCursor); cancel.clicked.connect(self.reject)
        ok = QPushButton("Confirm"); ok.setObjectName("primary")
        ok.setCursor(Qt.CursorShape.PointingHandCursor); ok.clicked.connect(self._on_ok)
        brow.addWidget(cancel); brow.addWidget(ok)
        root.addLayout(brow)
        self._pw.setFocus()

    def _on_ok(self):
        if self._config.check_password(self._pw.text()):
            self.accept()
        else:
            self._err.setVisible(True); self._pw.selectAll(); self._pw.setFocus()


# ===========================================================================
# Change password (current → new → confirm)
# ===========================================================================
class ChangePasswordDialog(_AeroDialog):
    def __init__(self, config, parent=None):
        super().__init__(parent, width=460, height=420)
        self._config = config
        sc = UiScale.px
        t = theme_mgr.tokens
        root = QVBoxLayout(self)
        root.setContentsMargins(sc(28), sc(24), sc(28), sc(22)); root.setSpacing(sc(10))

        h = QLabel("Change password")
        h.setStyleSheet(f"font-size: {sc(17)}px; font-weight: 800; color: {t.text_primary};")
        root.addWidget(h)

        def cap(text):
            c = QLabel(text); c.setStyleSheet(
                f"font-size: {sc(11)}px; font-weight: 700; color: {t.text_secondary};")
            return c

        def field(ph):
            e = QLineEdit(); e.setEchoMode(QLineEdit.EchoMode.Password)
            e.setPlaceholderText(ph); e.setFixedHeight(sc(38))
            e.textChanged.connect(lambda *_: self._err.setVisible(False))
            return e

        root.addWidget(cap("CURRENT PASSWORD"))
        self._cur = field("Current password"); root.addWidget(self._cur)
        root.addWidget(cap("NEW PASSWORD"))
        self._new = field("New password"); root.addWidget(self._new)
        root.addWidget(cap("CONFIRM NEW PASSWORD"))
        self._conf = field("Re-enter new password"); root.addWidget(self._conf)

        self._err = QLabel(""); self._err.setVisible(False); self._err.setWordWrap(True)
        self._err.setStyleSheet(f"font-size: {sc(11.5)}px; font-weight: 600; color: {t.danger_text};")
        root.addWidget(self._err)
        root.addStretch()

        brow = QHBoxLayout(); brow.addStretch(); brow.setSpacing(sc(10))
        cancel = QPushButton("Cancel"); cancel.setObjectName("secondary")
        cancel.setCursor(Qt.CursorShape.PointingHandCursor); cancel.clicked.connect(self.reject)
        ok = QPushButton("Update password"); ok.setObjectName("primary")
        ok.setCursor(Qt.CursorShape.PointingHandCursor); ok.clicked.connect(self._on_ok)
        brow.addWidget(cancel); brow.addWidget(ok)
        root.addLayout(brow)
        self._cur.setFocus()

    def _fail(self, msg, focus=None):
        self._err.setText(msg); self._err.setVisible(True)
        if focus: focus.setFocus()

    def _on_ok(self):
        if not self._config.check_password(self._cur.text()):
            return self._fail("That's not the current password. 💀", self._cur)
        new, conf = self._new.text(), self._conf.text()
        if not new:
            return self._fail("New password can't be empty. 😅", self._new)
        if new != conf:
            return self._fail("The two new passwords don't match. 💀", self._conf)
        self._config.set_password(new); self._config.save()
        self.accept()


# ===========================================================================
# Edit class details (room / class / section) — password is gated by caller
# ===========================================================================
class EditClassDetailsDialog(_AeroDialog):
    def __init__(self, config, parent=None):
        super().__init__(parent, width=480, height=440)
        self._config = config
        sc = UiScale.px
        t = theme_mgr.tokens
        # translucent combobox + line-edit styling (matches the wizard vibe)
        self.setStyleSheet(self.styleSheet() + f"""
            QLineEdit {{ background-color: {t.fill_default};
                border: 1px solid {t.stroke_default}; border-radius: {t.radius_xs}px;
                color: {t.text_primary}; }}
            QLineEdit:focus {{ background-color: {t.fill_hover}; border-color: {t.accent}; }}
            QComboBox {{ background-color: {t.fill_default}; color: {t.text_primary};
                border: 1px solid {t.stroke_default}; border-radius: {t.radius_xs}px;
                padding: {sc(7)}px {sc(11)}px; }}
            QComboBox:hover {{ border-color: {t.stroke_strong}; background-color: {t.fill_hover}; }}
            QComboBox:focus, QComboBox:on {{ border-color: {t.accent}; }}
            QComboBox::drop-down {{ border: none; width: {sc(24)}px; }}
            QComboBox::down-arrow {{ image: none;
                border-left: {sc(4)}px solid transparent; border-right: {sc(4)}px solid transparent;
                border-top: {sc(5)}px solid {t.text_secondary}; margin-right: {sc(9)}px; }}
            QComboBox QAbstractItemView {{ background-color: {t.bg_surface}; color: {t.text_primary};
                border: 1px solid {t.stroke_default}; border-radius: {t.radius_sm}px;
                selection-background-color: {t.accent_muted}; outline: none; padding: {sc(4)}px; }}
        """)
        root = QVBoxLayout(self)
        root.setContentsMargins(sc(28), sc(24), sc(28), sc(22)); root.setSpacing(sc(10))

        h = QLabel("Edit class details")
        h.setStyleSheet(f"font-size: {sc(17)}px; font-weight: 800; color: {t.text_primary};")
        root.addWidget(h)
        s = QLabel("Update the room, class and section for this smart board. ✏️")
        s.setWordWrap(True)
        s.setStyleSheet(f"font-size: {sc(12)}px; color: {t.text_secondary};")
        root.addWidget(s)

        def cap(text):
            c = QLabel(text); c.setStyleSheet(
                f"font-size: {sc(11)}px; font-weight: bold; color: {t.text_secondary};")
            return c

        root.addWidget(cap("ROOM NUMBER"))
        self._room = QLineEdit(config.room); self._room.setMaxLength(3)
        from PySide6.QtGui import QRegularExpressionValidator
        from PySide6.QtCore import QRegularExpression
        self._room.setValidator(QRegularExpressionValidator(QRegularExpression(r"\d{0,3}")))
        self._room.setFixedHeight(sc(38))
        self._room.textChanged.connect(lambda *_: self._err.setVisible(False))
        root.addWidget(self._room)

        rowcs = QHBoxLayout(); rowcs.setSpacing(sc(12))
        ccol = QVBoxLayout(); ccol.setSpacing(sc(5))
        ccol.addWidget(cap("CLASS STANDARD"))
        self._class = QComboBox(); self._class.addItems(CLASS_STANDARDS); self._class.setFixedHeight(sc(38))
        if config.class_standard in CLASS_STANDARDS:
            self._class.setCurrentText(config.class_standard)
        else:
            self._class.setCurrentIndex(-1); self._class.setPlaceholderText("Select")
        ccol.addWidget(self._class); rowcs.addLayout(ccol, 1)

        scol = QVBoxLayout(); scol.setSpacing(sc(5))
        scol.addWidget(cap("SECTION"))
        self._section = QComboBox(); self._section.addItems(SECTIONS); self._section.setFixedHeight(sc(38))
        if config.section in SECTIONS:
            self._section.setCurrentText(config.section)
        else:
            self._section.setCurrentIndex(-1); self._section.setPlaceholderText("Select")
        scol.addWidget(self._section); rowcs.addLayout(scol, 1)
        root.addLayout(rowcs)

        self._err = QLabel(""); self._err.setVisible(False); self._err.setWordWrap(True)
        self._err.setStyleSheet(f"font-size: {sc(11.5)}px; font-weight: bold; color: {t.danger_text};")
        root.addWidget(self._err)
        root.addStretch()

        brow = QHBoxLayout(); brow.addStretch(); brow.setSpacing(sc(10))
        cancel = QPushButton("Cancel"); cancel.setObjectName("secondary")
        cancel.setCursor(Qt.CursorShape.PointingHandCursor); cancel.clicked.connect(self.reject)
        ok = QPushButton("Save changes"); ok.setObjectName("primary")
        ok.setCursor(Qt.CursorShape.PointingHandCursor); ok.clicked.connect(self._on_ok)
        brow.addWidget(cancel); brow.addWidget(ok)
        root.addLayout(brow)

    def _on_ok(self):
        room = self._room.text().strip()
        std  = self._class.currentText().strip() if self._class.currentIndex() >= 0 else ""
        sec  = self._section.currentText().strip() if self._section.currentIndex() >= 0 else ""
        if not validate_room(room):
            self._err.setText("Room number must be exactly 3 digits."); self._err.setVisible(True); return
        if not std:
            self._err.setText("Please choose a class standard."); self._err.setVisible(True); return
        if not sec:
            self._err.setText("Please choose a section (A–G)."); self._err.setVisible(True); return
        ok, err = self._config.update_class_details(room=room, class_standard=std, section=sec)
        if not ok:
            self._err.setText(err); self._err.setVisible(True); return
        self.accept()


# ===========================================================================
# History page (loads ls.json, filter + sort)
# ===========================================================================
class _ClickableCard(QFrame):
    """A history row that calls a handler with its entry when clicked."""

    def __init__(self, entry: dict, on_click):
        super().__init__()
        self._entry = entry
        self._on_click = on_click

    def mouseReleaseEvent(self, e):
        if e.button() == Qt.MouseButton.LeftButton and self.rect().contains(e.pos()):
            try:
                self._on_click(self._entry)
            except Exception:
                pass
        super().mouseReleaseEvent(e)


class HistoryDialog(_AeroDialog):
    def __init__(self, activity_log, shared_dir=None, parent=None, config=None):
        super().__init__(parent, width=600, height=620, modal=False)
        self._log = activity_log
        self._shared_dir = shared_dir
        self._config = config            # for the password gate on Clear
        self._open_popups = []   # keep reopened notification windows alive
        sc = UiScale.px
        t = theme_mgr.tokens
        # combobox styling for the filter/sort controls
        self.setStyleSheet(self.styleSheet() + f"""
            QComboBox {{
                background-color: {t.bg_input}; color: {t.text_primary};
                border: 1px solid {t.stroke_default}; border-radius: {t.radius_xs}px;
                padding: {sc(6)}px {sc(10)}px;
            }}
            QComboBox:hover {{ border-color: {t.stroke_strong}; }}
            QComboBox::drop-down {{ border: none; width: {sc(22)}px; }}
            QComboBox::down-arrow {{ image: none;
                border-left: {sc(4)}px solid transparent; border-right: {sc(4)}px solid transparent;
                border-top: {sc(5)}px solid {t.text_secondary}; margin-right: {sc(8)}px; }}
            QComboBox QAbstractItemView {{
                background-color: {t.bg_surface}; color: {t.text_primary};
                border: 1px solid {t.stroke_default}; border-radius: {t.radius_sm}px;
                selection-background-color: {t.accent_muted}; outline: none; padding: {sc(4)}px; }}
            {fluent_styles.scrollbar(tk=t)}
        """)
        root = QVBoxLayout(self)
        root.setContentsMargins(sc(26), sc(22), sc(26), sc(20)); root.setSpacing(sc(12))

        h = QLabel("LogicShare History 📚")
        h.setStyleSheet(f"font-size: {sc(18)}px; font-weight: 800; color: {t.text_primary};")
        root.addWidget(h)

        # filter + sort controls
        ctrl = QHBoxLayout(); ctrl.setSpacing(sc(10))
        self._filter = QComboBox(); self._filter.setFixedHeight(sc(34))
        self._filter.addItem("All events", "all")
        for et in _RECEIVED_EVENTS:
            self._filter.addItem(EVENT_LABELS.get(et, et), et)
        self._sort = QComboBox(); self._sort.setFixedHeight(sc(34))
        self._sort.addItem("Newest first", False)
        self._sort.addItem("Oldest first", True)
        self._filter.currentIndexChanged.connect(self._reload)
        self._sort.currentIndexChanged.connect(self._reload)
        ctrl.addWidget(self._filter, 1); ctrl.addWidget(self._sort, 1)
        root.addLayout(ctrl)

        # scrollable list
        scroll = QScrollArea(); scroll.setWidgetResizable(True)
        scroll.setFrameShape(QFrame.Shape.NoFrame)
        scroll.setAttribute(Qt.WidgetAttribute.WA_TranslucentBackground, True)
        from ui.util.touch_scroll import setup_scroll
        setup_scroll(scroll)   # modern scrollbar + touch flick
        self._host = QWidget()
        self._host.setAttribute(Qt.WidgetAttribute.WA_TranslucentBackground, True)
        self._host.setStyleSheet("background: transparent;")
        self._list = QVBoxLayout(self._host)
        self._list.setContentsMargins(0, 0, sc(4), 0); self._list.setSpacing(sc(8))
        self._list.addStretch()
        scroll.setWidget(self._host)
        root.addWidget(scroll, 1)

        brow = QHBoxLayout()
        # Clear-history is destructive + password-gated → sits on the left,
        # visually separated from the harmless Close on the right.
        clear = QPushButton("🔒  Clear history"); clear.setObjectName("danger")
        clear.setCursor(Qt.CursorShape.PointingHandCursor)
        clear.clicked.connect(self._on_clear_history)
        clear.setStyleSheet(
            f"QPushButton#danger {{ background: {t.danger_bg}; color: {t.danger_text}; "
            f"border: 1px solid {t.danger_border_btn}; border-radius: {t.radius_sm}px; "
            f"padding: {sc(8)}px {sc(16)}px; font-weight: 700; }} "
            f"QPushButton#danger:hover {{ background: {t.danger_bg_hover}; }}"
        )
        brow.addWidget(clear)
        brow.addStretch()
        close = QPushButton("Close"); close.setObjectName("secondary")
        close.setCursor(Qt.CursorShape.PointingHandCursor); close.clicked.connect(self.accept)
        brow.addWidget(close)
        root.addLayout(brow)

        self._empty = QLabel("Quiet so far… 😅 Nothing has happened yet.")
        self._empty.setAlignment(Qt.AlignmentFlag.AlignCenter)
        self._empty.setStyleSheet(f"font-size: {sc(13)}px; color: {t.text_muted}; padding: {sc(40)}px 0;")
        self._reload()

    def _on_clear_history(self):
        """Permanently wipe the LogicShare activity history (ls.json). Gated by
        the LogicShare password when one is set — same gate as Restart / Edit."""
        cfg = self._config
        if cfg is not None and cfg.has_password():
            dlg = PasswordPromptDialog(
                cfg,
                title="Clear history",
                subtitle="Enter the LogicShare password to permanently clear the "
                         "activity history. This cannot be undone.",
                parent=self.window(),
            )
            if dlg.exec() != QDialog.DialogCode.Accepted:
                return
        self._log.clear()
        self._reload()

    def _clear(self):
        while self._list.count():
            it = self._list.takeAt(0)
            w = it.widget() if it else None
            if w is not None:
                w.setParent(None); w.deleteLater()

    def _row(self, entry: dict) -> QWidget:
        sc = UiScale.px
        t = theme_mgr.tokens
        card = _ClickableCard(entry, self._open_entry)
        card.setStyleSheet(f"QFrame {{ background: {t.card_grad}; "
                           f"border: 1px solid {t.stroke_default}; "
                           f"border-radius: {t.radius_md}px; }} "
                           f"QFrame:hover {{ background: {t.bg_card_hover}; "
                           f"border-color: {t.stroke_strong}; }} "
                           f"QLabel {{ background: transparent; border: none; }}")
        card.setCursor(Qt.CursorShape.PointingHandCursor)
        row = QHBoxLayout(card); row.setContentsMargins(sc(14), sc(11), sc(14), sc(11)); row.setSpacing(sc(12))
        et = entry.get("event_type", "")
        icon = QLabel(_EVENT_ICONS.get(et, "•")); icon.setFixedWidth(sc(26))
        icon.setStyleSheet(f"font-size: {sc(18)}px;")
        row.addWidget(icon, alignment=Qt.AlignmentFlag.AlignTop)

        col = QVBoxLayout(); col.setSpacing(sc(2))
        # primary line: file name or announcement title
        primary = entry.get("file_name") or entry.get("title") or entry.get("announcement") or EVENT_LABELS.get(et, et)
        p = QLabel(primary); p.setWordWrap(True)
        p.setStyleSheet(f"font-size: {sc(13)}px; font-weight: 700; color: {t.text_primary};")
        col.addWidget(p)
        # type + teacher
        teacher = entry.get("teacher_name", "")
        meta = f"{EVENT_LABELS.get(et, et)}"
        if teacher:
            meta += f"  ·  {teacher}"
        m = QLabel(meta)
        m.setStyleSheet(f"font-size: {sc(11)}px; color: {t.text_secondary};")
        col.addWidget(m)
        # timestamp
        ts = QLabel(_fmt_time(entry.get("timestamp", 0)))
        ts.setStyleSheet(f"font-size: {sc(10.5)}px; color: {t.text_muted};")
        col.addWidget(ts)
        row.addLayout(col, 1)

        # affordance: a small chevron hinting the card opens
        chev = QLabel("›")
        chev.setStyleSheet(f"font-size: {sc(20)}px; color: {t.text_muted};")
        row.addWidget(chev, alignment=Qt.AlignmentFlag.AlignVCenter)
        return card

    def _reload(self, *_):
        self._clear()
        et = self._filter.currentData()
        ascending = bool(self._sort.currentData())
        entries = self._log.query(event_type=et, ascending=ascending)
        # Receive-only board: never show sent events even if old logs contain them.
        entries = [e for e in entries if e.get("event_type") in _RECEIVED_EVENTS]
        if not entries:
            self._list.addWidget(self._empty)
        else:
            for e in entries:
                self._list.addWidget(self._row(e))
        self._list.addStretch()

    # ---- reopen the original file / announcement when a card is clicked ----
    def _open_entry(self, entry: dict):
        et = entry.get("event_type", "")
        if et == EVENT_ANNOUNCEMENT_RECEIVED:
            self._open_announcement(entry)
        elif et == EVENT_FILE_RECEIVED:
            self._open_file(entry)

    def _open_announcement(self, entry: dict):
        from .notifications import AnnouncementNotification
        teacher = entry.get("teacher_name", "") or "A teacher"
        initials = "".join(w[0] for w in teacher.split()[:2]).upper() or "?"
        info = {
            "first_name": teacher, "surname": "", "title_honorific": "",
            "short_name": initials,
            "title": entry.get("title", "") or "Announcement",
            "message": entry.get("announcement", ""),
        }
        popup = AnnouncementNotification(info, parent=self)
        self._open_popups.append(popup)
        popup.finished.connect(lambda _=0, p=popup: self._release(p))
        popup.show(); popup.raise_()

    def _open_file(self, entry: dict):
        # Open the shared file if we can find it, else the shared folder.
        from pathlib import Path
        name = entry.get("file_name", "")
        target = None
        if self._shared_dir:
            cand = Path(self._shared_dir) / name
            target = cand if (name and cand.exists()) else Path(self._shared_dir)
        if target is not None:
            try:
                QDesktopServices.openUrl(QUrl.fromLocalFile(str(target)))
            except Exception:
                pass

    def _release(self, popup):
        try:
            self._open_popups.remove(popup)
        except ValueError:
            pass
        popup.deleteLater()


# ===========================================================================
# The Settings section card
# ===========================================================================
class LogicShareSettingsCard(QFrame):
    """Drop-in card for the Settings panel. Reacts to service state."""

    def __init__(self, service, config, activity_log, parent=None):
        super().__init__(parent)
        self._svc = service
        self._cfg = config
        self._log = activity_log
        self.setObjectName("LSCard")

        sc = UiScale.px
        t = theme_mgr.tokens
        self.setStyleSheet(f"""
            #LSCard {{ background: {t.card_grad}; border: 1px solid {t.border};
                       border-radius: {sc(14)}px; }}
            QLabel {{ background: transparent; border: none; }}
            QPushButton#lsbtn {{
                background-color: {t.fill_default}; color: {t.text_primary};
                border: 1px solid {t.border}; border-radius: {sc(8)}px;
                padding: {sc(8)}px {sc(14)}px; font-size: {sc(12)}px; font-weight: bold;
            }}
            QPushButton#lsbtn:hover {{ background: {t.bg_card_hover}; border-color: {t.stroke_strong}; }}
            QPushButton#lsbtn:pressed {{ background: {t.fill_subtle}; }}
            QPushButton#danger {{
                background-color: {t.danger_bg}; color: {t.danger_text};
                border: 1px solid {t.danger_border}; border-radius: {sc(8)}px;
                padding: {sc(8)}px {sc(14)}px; font-size: {sc(12)}px; font-weight: bold;
            }}
            QPushButton#danger:hover {{ background: {t.danger_bg_hover}; }}
        """)
        root = QVBoxLayout(self)
        root.setContentsMargins(sc(14), sc(12), sc(14), sc(12)); root.setSpacing(sc(12))

        # header: title + status pill (matches other cards: sc(14) bold)
        head = QHBoxLayout()
        title = QLabel("LogicShare")
        title.setStyleSheet(f"font-size: {sc(14)}px; font-weight: bold; color: {t.text_primary};")
        head.addWidget(title); head.addStretch()
        self._pill = QLabel("—")
        head.addWidget(self._pill)
        root.addLayout(head)

        # info grid
        grid = QGridLayout(); grid.setHorizontalSpacing(sc(16)); grid.setVerticalSpacing(sc(7))
        def row(r, label):
            l = QLabel(label.upper())
            l.setStyleSheet(f"font-size: {sc(11)}px; color: {t.text_secondary}; font-weight: bold;")
            v = QLabel("—")
            v.setStyleSheet(f"font-size: {sc(12.5)}px; color: {t.text_primary}; font-weight: 600;")
            grid.addWidget(l, r, 0, alignment=Qt.AlignmentFlag.AlignLeft)
            grid.addWidget(v, r, 1, alignment=Qt.AlignmentFlag.AlignLeft)
            return v
        self._v_device = row(0, "Device")
        self._v_room   = row(1, "Room")
        self._v_class  = row(2, "Class")
        self._v_section = row(3, "Section")
        self._v_conn   = row(4, "Connection")
        grid.setColumnStretch(1, 1)
        root.addLayout(grid)

        # subtle divider between info and actions (matches LB card rhythm)
        div = QFrame(); div.setFixedHeight(1)
        div.setStyleSheet(f"background: {t.stroke_subtle}; border: none;")
        root.addWidget(div)

        # ── firewall banner (hidden unless Windows is in the way) ─────
        #
        # Above the conflict banner deliberately. A class conflict means
        # another board took your name; a firewall block means nothing can
        # reach you at all, which is the more fundamental problem and the
        # one that makes every other status on this card misleading — the
        # service happily reports "Running" either way, because binding a
        # socket succeeds whether or not Windows will deliver to it.
        self._fw_box = QFrame()
        self._fw_box.setStyleSheet(
            f"QFrame {{ background: {t.danger_bg}; "
            f"border: 1px solid {t.danger_border}; "
            f"border-radius: {sc(10)}px; }} "
            f"QLabel {{ background: transparent; border: none; }}")
        fwb = QVBoxLayout(self._fw_box)
        fwb.setContentsMargins(sc(14), sc(11), sc(14), sc(11))
        fwb.setSpacing(sc(6))
        self._fw_msg = QLabel("")
        self._fw_msg.setWordWrap(True)
        self._fw_msg.setStyleSheet(
            f"font-size: {sc(12)}px; font-weight: bold; color: {t.danger_text};")
        fwb.addWidget(self._fw_msg)
        self._fw_detail = QLabel("")
        self._fw_detail.setWordWrap(True)
        self._fw_detail.setStyleSheet(
            f"font-size: {sc(11)}px; color: {t.text_secondary};")
        fwb.addWidget(self._fw_detail)

        fw_row = QHBoxLayout()
        fw_row.setSpacing(sc(8))
        self._btn_allow = QPushButton("Allow network access")
        self._btn_allow.setObjectName("lsbtn")
        self._btn_allow.setCursor(Qt.CursorShape.PointingHandCursor)
        self._btn_allow.clicked.connect(self._allow_network)
        fw_row.addWidget(self._btn_allow)
        fw_row.addStretch()
        fwb.addLayout(fw_row)

        root.addWidget(self._fw_box)
        self._fw_box.setVisible(False)

        # conflict banner (hidden unless conflicted)
        self._conflict_box = QFrame()
        self._conflict_box.setStyleSheet(
            f"QFrame {{ background: {t.danger_bg}; border: 1px solid {t.danger_border}; "
            f"border-radius: {sc(10)}px; }} QLabel {{ background: transparent; border: none; }}")
        cb = QVBoxLayout(self._conflict_box)
        cb.setContentsMargins(sc(14), sc(11), sc(14), sc(11)); cb.setSpacing(sc(4))
        self._conflict_msg = QLabel(
            "LogicShare is paused — another smart board claimed the same "
            "class on this network. 💀")
        self._conflict_msg.setWordWrap(True)
        self._conflict_msg.setStyleSheet(f"font-size: {sc(12)}px; font-weight: bold; color: {t.danger_text};")
        cb.addWidget(self._conflict_msg)
        self._conflict_detail = QLabel("")
        self._conflict_detail.setWordWrap(True)
        self._conflict_detail.setStyleSheet(f"font-size: {sc(11)}px; color: {t.text_secondary};")
        cb.addWidget(self._conflict_detail)
        root.addWidget(self._conflict_box)
        self._conflict_box.setVisible(False)

        # actions — two tidy rows so they don't overflow on narrow panels
        def make_btn(text, slot, lock=True, obj="lsbtn"):
            b = QPushButton((_LOCK if lock else "") + text)
            b.setObjectName(obj)
            b.setCursor(Qt.CursorShape.PointingHandCursor)
            b.clicked.connect(slot)
            return b

        row1 = QHBoxLayout(); row1.setSpacing(sc(8))
        self._btn_edit = make_btn("Edit class details", self._edit_details)
        self._btn_changepw = make_btn("Change password", self._change_password)
        row1.addWidget(self._btn_edit); row1.addWidget(self._btn_changepw); row1.addStretch()
        root.addLayout(row1)

        row2 = QHBoxLayout(); row2.setSpacing(sc(8))
        self._btn_restart = make_btn("Restart service", self._restart)
        # Stop / Start toggle — label flips in refresh() based on service state.
        # Both directions are password-gated; Stop additionally refuses while a
        # file is being received.
        self._btn_stopstart = make_btn("Stop service", self._toggle_service)
        self._btn_history = make_btn("View history", self._view_history, lock=False)
        row2.addWidget(self._btn_restart); row2.addWidget(self._btn_stopstart)
        row2.addWidget(self._btn_history); row2.addStretch()
        root.addLayout(row2)

        # conflict-only actions
        self._conflict_actions = QHBoxLayout(); self._conflict_actions.setSpacing(sc(8))
        self._btn_retry = QPushButton("Retry"); self._btn_retry.setObjectName("lsbtn")
        self._btn_retry.setCursor(Qt.CursorShape.PointingHandCursor)
        self._btn_retry.clicked.connect(lambda: self._svc.retry())
        self._btn_restart2 = QPushButton("Restart LogicShare"); self._btn_restart2.setObjectName("danger")
        self._btn_restart2.setCursor(Qt.CursorShape.PointingHandCursor)
        self._btn_restart2.clicked.connect(lambda: self._svc.restart())
        self._conflict_actions.addWidget(self._btn_retry)
        self._conflict_actions.addWidget(self._btn_restart2)
        self._conflict_actions.addStretch()
        self._conflict_widget = QWidget(); self._conflict_widget.setLayout(self._conflict_actions)
        root.addWidget(self._conflict_widget)
        self._conflict_widget.setVisible(False)

        # react to service state
        try:
            service.status_changed.connect(lambda *_: self.refresh())
            service.conflict_detected.connect(lambda *_: self.refresh())
            service.conflict_cleared.connect(lambda *_: self.refresh())
        except Exception:
            pass

        # Poll every 5 s so the "Offline" pill appears the moment WiFi drops
        # (status_changed only fires on explicit lifecycle changes — losing
        # network on its own is a passive event, nothing emits a signal).
        from PySide6.QtCore import QTimer
        self._net_timer = QTimer(self)
        self._net_timer.setInterval(5000)
        self._net_timer.timeout.connect(self.refresh)
        self._net_timer.start()

        self.refresh()

    # ---- Windows network permission ----
    def _refresh_firewall(self):
        """Show the banner only when Windows is genuinely in the way.

        Re-read on every refresh rather than cached, because the answer
        changes without the app doing anything: moving between a home and
        a school network switches the profile, and a rule that covered one
        may not cover the other. That is not a hypothetical — the machine
        this was written on had a rule for the installed board limited to
        domain+private while sitting on a public network.
        """
        from ui.util import firewall as fw
        try:
            state = fw.inbound_state()
        except Exception:
            logging.debug("firewall check failed", exc_info=True)
            state = fw.STATE_UNKNOWN
        self._fw_state = state

        # UNKNOWN stays quiet. It means the check could not run — no
        # pywin32 in this build, COM refused — and a red banner accusing
        # Windows of blocking, on no evidence, is worse than saying
        # nothing: the teacher cannot tell a real problem from a broken
        # check, and the one thing they would try (the button) would ask
        # for an administrator password to fix something that may not be
        # wrong.
        show = state in (fw.STATE_BLOCKED, fw.STATE_ABSENT)
        self._fw_box.setVisible(show)
        if not show:
            return

        if state == fw.STATE_BLOCKED:
            self._fw_msg.setText(
                "Windows is BLOCKING this board — LogicShare will not work 💀")
            # Said up front because the button now does it: a blocking rule
            # beats any allow, so "Allow network access" switches those
            # rules off (not deletes — they stay in Windows Firewall to be
            # turned back on). The teacher should know that before they
            # approve the prompt, not discover it afterwards.
            self._fw_detail.setText(
                "A Windows Firewall rule is blocking LogicBoard, so phones "
                "will not find this board and nothing can be sent. Allowing "
                "it switches that rule off and needs an administrator, so "
                "Windows will ask for permission.")
        else:
            self._fw_msg.setText(
                "Windows has not allowed this board through — "
                "LogicShare will not work 💀")
            self._fw_detail.setText(
                "Devices cannot reach this board over the network, so phones "
                "will not find it and nothing can be sent. Allowing it needs "
                "an administrator, so Windows will ask for permission.")

    def _allow_network(self):
        """Ask Windows — through the user — to let this board through."""
        from ui.util import firewall as fw
        btn = self._btn_allow
        btn.setEnabled(False)
        btn.setText("Waiting for Windows…")
        QApplication.processEvents()
        try:
            ok = fw.request_access()
        except Exception:
            logging.exception("requesting network access failed")
            ok = False
        finally:
            btn.setEnabled(True)
            btn.setText("Allow network access")

        self._refresh_firewall()
        if ok:
            # The service bound its socket before the rule existed. It does
            # not need rebinding for the rule to take effect — the rule is
            # matched per packet, not at bind time — but a restart is the
            # honest way to clear any half-state and get the badge right.
            try:
                self._svc.restart()
            except Exception:
                logging.debug("restart after allowing failed", exc_info=True)
        else:
            self._fw_detail.setText(
                "Windows did not grant access. If the prompt was declined, "
                "try again; if it never appeared, this board may be managed "
                "by an administrator.")
        self.refresh()

    # ---- state display ----
    def refresh(self):
        sc = UiScale.px
        t = theme_mgr.tokens
        self._v_device.setText(self._cfg.device_name or "—")
        self._v_room.setText(self._cfg.room or "—")
        self._v_class.setText(self._cfg.class_standard or "—")
        self._v_section.setText(self._cfg.section or "—")

        self._refresh_firewall()

        status = self._svc.status()
        conflicted = (status == "disabled" and self._svc.conflict)
        blocked = getattr(self, "_fw_state", None) in ("blocked", "absent")
        if status == "running" and blocked:
            # The service IS running. It just cannot be reached, and a
            # green "Online" over a red banner is the contradiction that
            # sends people looking at their WiFi instead of their firewall.
            self._pill_set("Unreachable", t.danger, t.danger_bg,
                           t.danger_border)
            self._v_conn.setText("Running, but Windows is blocking it 💀")
        elif status == "running":
            self._pill_set("Running", t.success, t.success_bg, t.success_border)
            self._v_conn.setText("Online 📡")
        elif status == "restarting":
            # Mid-restart: user hit the Restart button. During the delay
            # window the service is fully down (mDNS unregistered, HTTP
            # socket closed) — showing "Restarting" instead of the
            # transient "Stopped" tells the user why the badge just
            # briefly went dark and that it'll come back on its own.
            self._pill_set("Restarting…", t.accent,
                           t.accent_selected_bg, t.accent)
            self._v_conn.setText("Bringing the service back up…")
        elif status == "offline":
            # Service is enabled but the board has no LAN/WiFi at all.
            # Rendered in RED (danger tokens) — this is a genuine problem
            # the teacher needs to fix, not a soft warning.
            self._pill_set("Offline", t.danger, t.danger_bg, t.danger_border)
            self._v_conn.setText("No WiFi / LAN — nobody can reach this board 💀")
        elif conflicted:
            self._pill_set("Disabled", t.danger, t.danger_bg, t.danger_border)
            self._v_conn.setText("Disabled (network conflict)")
        else:  # stopped
            self._pill_set("Not Running", t.text_muted, t.fill_default, t.stroke_default)
            self._v_conn.setText("Not running")

        # Stop/Start toggle: label depends on whether the service is enabled.
        # 'enabled' is independent of network state — a service can be ON but
        # 'offline' (no WiFi), and in that case the button should still say
        # 'Stop service' so the user can shut it down.
        #
        # During a restart both action buttons are disabled — clicking
        # through the transient window would just fight the background
        # restart thread.
        is_restarting = (status == "restarting")
        try:
            self._btn_stopstart.setText(
                _LOCK + ("Stop service" if self._svc.enabled
                         else "Start service"))
            self._btn_stopstart.setEnabled(not is_restarting)
        except Exception:
            pass
        try:
            self._btn_restart.setEnabled(not is_restarting and self._svc.enabled)
        except Exception:
            pass

        self._conflict_box.setVisible(bool(conflicted))
        self._conflict_widget.setVisible(bool(conflicted))
        if conflicted:
            detail = (self._svc.conflict or {}).get("detail", "")
            self._conflict_detail.setText(detail)
            self._conflict_detail.setVisible(bool(detail))

    def _pill_set(self, text, fg, bg, border):
        sc = UiScale.px
        self._pill.setText(text)
        self._pill.setStyleSheet(
            f"font-size: {sc(11)}px; font-weight: 800; color: {fg}; "
            f"background: {bg}; border: 1px solid {border}; "
            f"border-radius: {sc(11)}px; padding: {sc(3)}px {sc(12)}px;")

    # ---- actions (password-gated where required) ----
    def _require_password(self, title, subtitle) -> bool:
        dlg = PasswordPromptDialog(self._cfg, title=title, subtitle=subtitle,
                                   parent=self.window())
        return dlg.exec() == QDialog.DialogCode.Accepted

    def _restart(self):
        if not self._cfg.has_password():
            self._svc.restart(); return
        if self._require_password("Restart service",
                                  "Enter the LogicShare password to restart the service."):
            self._svc.restart()

    def _toggle_service(self):
        """Stop the service if it's running, start it if it's not.

        STOP is guarded twice: (a) refused outright while a file is being
        received (otherwise the receiver would corrupt the partial file mid-
        write), and (b) password-gated. START is password-gated only.
        """
        if self._svc.enabled:
            # --- attempting to STOP ---
            try:
                active = list(self._svc.active_transfers())
            except AttributeError:
                active = []   # service predates active_transfers() — degrade gracefully
            if active:
                # Show a warning naming the sender of the first in-flight
                # transfer, and refuse. No abort option — that would corrupt
                # the partial file already on disk.
                first = active[0]
                who = " ".join(filter(None, [first.get("first_name", ""),
                                             first.get("surname", "")])).strip() or "a teacher"
                fname = first.get("filename", "a file")
                extra = "" if len(active) == 1 else f" (+{len(active)-1} more)"
                AeroAlert.warning(
                    self.window(),
                    "Hold on! File still incoming 📁",
                    f"{who} is still sending “{fname}”{extra}. The service "
                    f"can't be stopped mid-transfer — that would corrupt "
                    f"the partial download. 💀",
                )
                return
            if self._cfg.has_password() and not self._require_password(
                    "Stop service",
                    "Enter the LogicShare password to stop the LogicShare service."):
                return
            self._svc.stop()
        else:
            # --- attempting to START ---
            if self._cfg.has_password() and not self._require_password(
                    "Start service",
                    "Enter the LogicShare password to start the LogicShare service."):
                return
            self._svc.start()

    def _change_password(self):
        ChangePasswordDialog(self._cfg, parent=self.window()).exec()

    def _edit_details(self):
        # Password-gated: only the class teacher should change class details.
        if self._cfg.has_password() and not self._require_password(
                "Edit class details",
                "Enter the LogicShare password to change the class details."):
            return
        dlg = EditClassDetailsDialog(self._cfg, parent=self.window())
        if dlg.exec() == QDialog.DialogCode.Accepted:
            self.refresh()
            # identity changed → re-broadcast on the LAN with the new details
            try:
                self._svc.restart()
            except Exception:
                pass

    def _view_history(self):
        # non-modal so the teacher can keep Settings open
        shared = getattr(self._svc, "shared_dir", None)
        self._history = HistoryDialog(self._log, shared_dir=shared,
                                      parent=self.window(), config=self._cfg)
        self._history.show(); self._history.raise_()