"""
ui/logicshare/notifications.py — LogicShare notification windows (spec §5).

Frameless, aero-backed, rounded, NO title bar — built entirely on LogicBoard's
own utilities (prepare_aero_widget / apply_aero / enable_shadow /
scale_in / UiScale / theme_mgr) so they're a natural extension of the app and
share its glass look, shadows, and scaling. No duplicate styling system.

  • FileNotification        — shows an incoming transfer with live progress,
                              then teacher / file / size / timestamp +
                              [Close] [Open Shared Files Folder].
  • AnnouncementNotification — big, classroom-readable teacher / title /
                              message / timestamp + [Close].
"""
from __future__ import annotations

import html as _html
import logging
import re as _re
import time
from pathlib import Path

from PySide6.QtCore import Qt, QPointF, QUrl, QTimer
from PySide6.QtGui import (
    QPainter, QBrush, QPen, QColor, QLinearGradient, QFont, QDesktopServices,
    QFontMetrics, QPixmap,
)
from PySide6.QtWidgets import (
    QDialog, QVBoxLayout, QHBoxLayout, QLabel, QPushButton, QFrame,
    QProgressBar, QWidget, QSizePolicy,
)

from ui.util.scaling    import UiScale
from ui.util.aero    import apply_aero, enable_shadow, prepare_aero_widget
from ui.util.animations import fade_in
from ui.theme.theme_manager import theme_mgr

_log = logging.getLogger(__name__)

# How long the OK button stays disabled after an announcement appears.
#
# The point is to make the reader actually look at the message rather than
# clearing the popup by reflex, which is what happens to anything that can be
# dismissed the instant it appears. Three seconds is enough to break the
# reflex without becoming an obstacle — and the button shows the countdown, so
# it reads as "wait a moment", not as a broken control.
ANNOUNCEMENT_READ_DELAY_SECONDS = 3

# How often a sticky announcement re-asserts itself over whatever took the
# foreground. Frequent enough that another window cannot sit on top of it for
# a noticeable time, cheap enough to be irrelevant: the check is a couple of
# Win32 calls and does nothing at all when the popup is already on top.
_STICKY_INTERVAL_MS = 700

__all__ = ["FileNotification", "AnnouncementNotification", "NotificationManager"]


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------
def _format_size(n: float) -> str:
    n = float(n)
    if n < 1024:
        return f"{int(n)} B"
    for unit in ("KB", "MB", "GB"):
        n /= 1024
        if n < 1024:
            return f"{n:.1f} {unit}"
    return f"{n / 1024:.1f} TB"


def _format_elapsed(seconds: float) -> str:
    """Human-readable transfer duration: '0.8s' / '4.3s' / '1m 12s' / '2h 5m'."""
    s = float(seconds)
    if s < 10:    return f"{s:.1f}s"
    if s < 60:    return f"{int(round(s))}s"
    m, sec = divmod(int(s), 60)
    if m < 60:    return f"{m}m {sec}s"
    h, m = divmod(m, 60)
    return f"{h}h {m}m"


def _time_now_label() -> str:
    return "just now  ·  " + time.strftime("%I:%M %p").lstrip("0")


_AVATAR_PALETTE = [
    ("#7cd0ff", "#2596e3"), ("#f56de2", "#8b3fd3"), ("#6ccf80", "#2e8a4a"),
    ("#f0c14b", "#d28722"), ("#ff8a80", "#d04848"), ("#67e8f9", "#0891b2"),
    ("#a78bfa", "#6d28d9"),
]


def _avatar_colors(seed: str):
    if not seed:
        return _AVATAR_PALETTE[0]
    return _AVATAR_PALETTE[sum(ord(c) for c in seed.upper()) % len(_AVATAR_PALETTE)]


# ---------------------------------------------------------------------------
# Turning a plain-text announcement into clickable rich text
# ---------------------------------------------------------------------------
# Matches, in order: an explicit scheme, a bare "www." host, an e-mail
# address. Ordering matters — the scheme alternative has to win first, or
# "https://a.com" would match the bare-host branch from "a.com" onward and
# produce a broken link.
#
# The character class excludes < > " ' so a URL can never run past the end of
# the attribute it will be embedded in.
_URL_RE = _re.compile(
    r"""(
          (?:https?://|ftp://)[^\s<>"']+
        | www\.[^\s<>"']+
        | [\w.+-]+@[\w-]+\.[\w.-]+
    )""",
    _re.VERBOSE | _re.IGNORECASE,
)

# Punctuation that is far more likely to be sentence furniture than part of
# the address, when it appears at the very end of a match.
_TRAILING_PUNCT = ".,;:!?\u2019'\""


def _linkify(text: str, link_color: str) -> str:
    """Escape ``text`` for Qt rich text and wrap any URLs in anchors.

    Escaping happens FIRST and the anchors are built from the escaped text on
    purpose. A teacher's announcement is arbitrary input, and it reaches this
    label straight off the network — pasting it into a rich-text widget
    unescaped would let a stray '<' silently eat the rest of the message, and
    would let deliberate markup style or restructure the popup. Escaping first
    makes that impossible.

    Escaping a query string turns '&' into '&amp;', which is not a problem:
    that is the correct way to write an ampersand inside an HTML attribute,
    and it decodes back to '&' when the link is opened.
    """
    out = []
    pos = 0
    for m in _URL_RE.finditer(text):
        out.append(_html.escape(text[pos:m.start()]))
        raw = m.group(0)

        # Don't swallow the punctuation that ends the sentence. A closing
        # bracket is kept when the URL opened one itself, since plenty of
        # real links (Wikipedia especially) legitimately end in ')'.
        trail = ""
        while raw and raw[-1] in _TRAILING_PUNCT + ")]}":
            # ">=" not ">". At this point the closing bracket is still ON
            # raw, so a URL that legitimately ends in one — Wikipedia's
            # /wiki/Foo_(bar) being the classic — has EQUAL counts, and ">"
            # judged that unbalanced and chopped the bracket off, breaking
            # the link. Sentence furniture like "(see https://a.io/x)" gives
            # 0 open against 1 close, which still fails ">=" and is stripped.
            if raw[-1] == ")" and raw.count("(") >= raw.count(")"):
                break
            if raw[-1] == "]" and raw.count("[") >= raw.count("]"):
                break
            trail = raw[-1] + trail
            raw = raw[:-1]
        if not raw:
            out.append(_html.escape(trail))
            pos = m.end()
            continue

        href = raw
        if raw.lower().startswith("www."):
            href = "https://" + raw
        elif "@" in raw and "://" not in raw:
            href = "mailto:" + raw

        out.append(
            '<a href="{h}" style="color:{c}; text-decoration:underline;">{t}</a>'
            .format(h=_html.escape(href, quote=True),
                    c=link_color,
                    t=_html.escape(raw))
        )
        out.append(_html.escape(trail))
        pos = m.end()

    out.append(_html.escape(text[pos:]))
    # Qt rich text collapses newlines like a browser does, so they have to be
    # made explicit or a multi-line announcement arrives as one run-on line.
    return "".join(out).replace("\n", "<br>")


class _ElidingLabel(QLabel):
    """A label that middle-elides its text to fit its width (keeps the file
    extension visible) and shows the full name on hover."""

    def __init__(self, text="", parent=None):
        super().__init__(parent)
        self._full = text
        self.setSizePolicy(QSizePolicy.Policy.Ignored, QSizePolicy.Policy.Preferred)
        self.setMinimumWidth(0)
        super().setText(text)
        self.setToolTip(text)

    def setFullText(self, text: str):
        self._full = text
        self.setToolTip(text)
        self._elide()

    def resizeEvent(self, e):
        super().resizeEvent(e)
        self._elide()

    def _elide(self):
        fm = QFontMetrics(self.font())
        super().setText(fm.elidedText(self._full, Qt.TextElideMode.ElideMiddle,
                                      max(0, self.width())))


def _file_ext_label(filename: str) -> str:
    """Short uppercase type tag for the badge, e.g. 'MP4', 'PDF', 'FILE'."""
    suffix = filename.rsplit(".", 1)[-1] if "." in filename else ""
    suffix = "".join(c for c in suffix if c.isalnum()).upper()
    return suffix[:4] if suffix else "FILE"


# extension → which bundled SVG icon to show on the file card
_PDF_EXTS   = {"pdf"}
_PPT_EXTS   = {"ppt", "pptx", "pps", "ppsx", "potx"}
_IMAGE_EXTS = {"png", "jpg", "jpeg", "gif", "webp", "bmp", "tif", "tiff",
               "heic", "heif", "svg", "ico"}


def _icon_name_for(filename: str):
    """Return the bundled icon name for a file, or None for 'use the default'."""
    ext = filename.rsplit(".", 1)[-1].lower() if "." in filename else ""
    if ext in _PDF_EXTS:
        return "pdf"
    if ext in _PPT_EXTS:
        return "ppt"
    if ext in _IMAGE_EXTS:
        return "image"
    return None


class _AvatarBadge(QWidget):
    """Small gradient circle with the teacher's initials (visual identity)."""

    def __init__(self, initials: str, size: int = 56, parent=None):
        super().__init__(parent)
        self.initials = (initials or "?").upper()[:3]
        self._size = size
        self.setFixedSize(size, size)
        self._c1, self._c2 = _avatar_colors(self.initials)

    def paintEvent(self, _):
        p = QPainter(self)
        p.setRenderHint(QPainter.RenderHint.Antialiasing)
        s = self._size
        grad = QLinearGradient(0, 0, s, s)
        grad.setColorAt(0, QColor(self._c1)); grad.setColorAt(1, QColor(self._c2))
        p.setBrush(QBrush(grad)); p.setPen(QPen(QColor(255, 255, 255, 70), 1.4))
        p.drawEllipse(2, 2, s - 4, s - 4)
        p.setPen(QColor("#ffffff"))
        f = QFont(); f.setBold(True); f.setPointSizeF(s * 0.30)
        p.setFont(f)
        p.drawText(self.rect(), Qt.AlignmentFlag.AlignCenter, self.initials)


# ---------------------------------------------------------------------------
# Shared frameless-aero base (mirrors AboutDialog's lifecycle exactly)
# ---------------------------------------------------------------------------
class _AeroPopup(QDialog):
    """Frameless, aero, rounded, no-title-bar popup base."""

    def __init__(self, parent=None, *, width=540, height=380):
        super().__init__(parent, Qt.WindowType.FramelessWindowHint)
        # Non-modal: notifications shouldn't freeze the board, and several can
        # stack. The owner keeps a reference so they aren't garbage-collected.
        self.setModal(False)
        # Real Windows 11 Mica behind the popup where the system can draw
        # it; the usual solid LogicBoard surface where it cannot, and while
        # the popup is not the active window (see ui/util/mica.py). Either
        # way the surface is painted by a background child, so the dialog
        # itself must not paint an opaque body over it.
        from ui.util import mica
        self._mica = mica.prepare(self)

        sc = UiScale.px
        self.setFixedSize(sc(width), sc(height))
        t = theme_mgr.tokens
        self.setStyleSheet(f"""
            QDialog {{
                background: transparent;
                border: none;
            }}
            QLabel {{ color: {t.text_primary}; background: transparent; border: none; }}
            QPushButton#secondary {{
                background-color: {t.fill_default};
                color: {t.text_primary};
                border: 1px solid {t.stroke_default};
                border-radius: {t.radius_sm}px;
                padding: {sc(9)}px {sc(18)}px;
                font-size: {sc(13)}px; font-weight: 600;
            }}
            QPushButton#secondary:hover {{ background: {t.bg_card_hover}; }}
            QPushButton#primary {{
                background-color: {t.accent_grad};
                color: {t.text_on_accent};
                border: 1px solid {t.accent};
                border-radius: {t.radius_sm}px;
                padding: {sc(9)}px {sc(18)}px;
                font-size: {sc(13)}px; font-weight: 700;
            }}
            QPushButton#primary:hover {{ background: {t.accent_grad_hover}; }}
            QPushButton#primary:pressed {{ background: {t.accent_grad_pressed}; }}
        """)
        self._center_on_screen()

    def _center_on_screen(self):
        try:
            geo = self.screen().geometry()
            self.move(geo.center() - self.rect().center())
        except Exception:
            pass

    def showEvent(self, event):
        super().showEvent(event)
        from ui.util import mica
        mica.apply(self)
        if not getattr(self, "_lb_mica", False):
            enable_shadow(self)
            # windowOpacity makes a window LAYERED, which is exactly what
            # turns Mica off — so the fade is for the solid fallback only.
            # A Mica window gets Windows' own open animation instead.
            fade_in(self)


# ---------------------------------------------------------------------------
# File notification (live progress → received summary)
# ---------------------------------------------------------------------------
class FileNotification(_AeroPopup):
    def __init__(self, info: dict, shared_dir: Path, parent=None):
        super().__init__(parent, width=540, height=360)
        self.info = info
        self._shared_dir = Path(shared_dir)
        sc = UiScale.px
        t = theme_mgr.tokens

        root = QVBoxLayout(self)
        root.setContentsMargins(sc(28), sc(24), sc(28), sc(22))
        root.setSpacing(sc(14))

        # Heading
        head = QLabel("Incoming file 📁")
        head.setStyleSheet(f"font-size: {sc(12)}px; font-weight: 800; "
                           f"letter-spacing: 2px; color: {t.accent};")
        root.addWidget(head)

        # Teacher row (avatar + name)
        teacher = f"{info.get('first_name','')} {info.get('surname','')} {info.get('title','')}".strip()
        trow = QHBoxLayout(); trow.setSpacing(sc(12))
        trow.addWidget(_AvatarBadge(info.get("short_name", "?"), sc(48)))
        tcol = QVBoxLayout(); tcol.setSpacing(sc(1))
        name = QLabel(teacher or "A teacher")
        name.setStyleSheet(f"font-size: {sc(15)}px; font-weight: 700; color: {t.text_primary};")
        self._time = QLabel(_time_now_label())
        self._time.setStyleSheet(f"font-size: {sc(11)}px; color: {t.text_muted};")
        tcol.addWidget(name); tcol.addWidget(self._time)
        trow.addLayout(tcol, 1)
        root.addLayout(trow)

        # File card
        card = QFrame(); card.setObjectName("fcard")
        card.setStyleSheet(
            f"QFrame#fcard {{ background: {t.fill_subtle}; "
            f"border: 1px solid {t.stroke_subtle}; border-radius: {t.radius_md}px; }}")
        cl = QHBoxLayout(card); cl.setContentsMargins(sc(12), sc(11), sc(14), sc(11)); cl.setSpacing(sc(13))

        # file-type badge: real SVG icon for PDF / PPT / image, else a
        # default file emoji for everything else.
        self._badge = QLabel()
        self._badge.setFixedSize(sc(44), sc(44))
        self._badge.setAlignment(Qt.AlignmentFlag.AlignCenter)
        self._apply_badge(info.get("filename", ""))
        cl.addWidget(self._badge)

        fcol = QVBoxLayout(); fcol.setSpacing(sc(3))
        self._fname = _ElidingLabel(info.get("filename", "file"))
        self._fname.setStyleSheet(f"font-size: {sc(13)}px; font-weight: 700; color: {t.text_primary};")
        self._fsize = QLabel(_format_size(info.get("size", 0)))
        self._fsize.setStyleSheet(f"font-size: {sc(11)}px; color: {t.text_muted}; font-weight: 600;")
        fcol.addWidget(self._fname); fcol.addWidget(self._fsize)
        cl.addLayout(fcol, 1)
        root.addWidget(card)

        # Status + progress
        self._status = QLabel("Receiving… 📡")
        self._status.setStyleSheet(f"font-size: {sc(12)}px; font-weight: 600; color: {t.text_secondary};")
        root.addWidget(self._status)

        self._bar = QProgressBar(); self._bar.setMinimum(0); self._bar.setMaximum(100)
        self._bar.setTextVisible(False); self._bar.setFixedHeight(sc(10))
        self._bar.setStyleSheet(f"""
            QProgressBar {{ background: {t.fill_default};
                border: 1px solid {t.stroke_subtle}; border-radius: {sc(5)}px; }}
            QProgressBar::chunk {{
                background: qlineargradient(x1:0,y1:0,x2:1,y2:0,
                    stop:0 {t.accent_pressed}, stop:1 {t.accent});
                border-radius: {sc(5)}px; }}
        """)
        root.addWidget(self._bar)
        root.addStretch()

        # Buttons
        brow = QHBoxLayout(); brow.addStretch(); brow.setSpacing(sc(10))
        self._close_btn = QPushButton("Close"); self._close_btn.setObjectName("secondary")
        self._close_btn.setCursor(Qt.CursorShape.PointingHandCursor)
        self._close_btn.clicked.connect(self.accept)
        self._open_btn = QPushButton("Open Shared Files Folder"); self._open_btn.setObjectName("primary")
        self._open_btn.setCursor(Qt.CursorShape.PointingHandCursor)
        self._open_btn.setEnabled(False)
        self._open_btn.clicked.connect(self._open_folder)
        brow.addWidget(self._close_btn); brow.addWidget(self._open_btn)
        root.addLayout(brow)

    # ---- file-type badge (icon for known types, emoji otherwise) ----
    def _apply_badge(self, filename: str):
        sc = UiScale.px
        t = theme_mgr.tokens
        icon_name = _icon_name_for(filename)
        if icon_name:
            from ui.util.icon_loader import tool_icon
            px = tool_icon(icon_name, sc(40)).pixmap(sc(40), sc(40))
            self._badge.setText("")
            self._badge.setPixmap(px)
            self._badge.setStyleSheet("background: transparent; border: none;")
        else:
            # default: a clean file emoji on a soft rounded tile
            self._badge.setPixmap(QPixmap())
            self._badge.setText("📄")
            self._badge.setStyleSheet(
                f"background: {t.fill_default}; border: none; "
                f"border-radius: {sc(11)}px; font-size: {sc(24)}px;")

    # ---- live updates (driven by the service signals) ----
    def update_progress(self, received, speed_bps=None):
        total = max(int(self.info.get("size", 0)), 1)
        pct = int(int(received) / total * 100)
        self._bar.setValue(min(pct, 100))
        speed_txt = ""
        if speed_bps is not None and float(speed_bps) > 0:
            speed_txt = f"   ·   {_format_size(float(speed_bps))}/s"
        # If we're currently flagged stalled and a new chunk arrives, the
        # caller (mark_resumed) flips the status colour first, so we don't
        # overwrite that here — we only refresh the live counters/speed.
        if not getattr(self, "_is_stalled", False):
            t = theme_mgr.tokens
            self._status.setText(
                f"Receiving… 📡   {_format_size(received)} / {_format_size(total)}"
                f"   ({pct}%){speed_txt}")
            self._status.setStyleSheet(
                f"font-size: {UiScale.px(12)}px; font-weight: 600; color: {t.text_secondary};")

    def mark_stalled(self):
        """Transmitter hasn't sent a chunk in a while — likely went offline or
        WiFi blip. We don't abort; TCP itself will eventually drop if it's
        truly dead. Until then, surface it so the user knows it's not frozen."""
        self._is_stalled = True
        t = theme_mgr.tokens
        self._status.setText("⚠ The sender went quiet — waiting…")
        self._status.setStyleSheet(
            f"font-size: {UiScale.px(12)}px; font-weight: 700; color: {t.warning_text};")

    def mark_resumed(self):
        self._is_stalled = False
        # Next update_progress() call will repaint the normal status line.

    def mark_complete(self, saved_name: str, saved_path: str = "",
                      elapsed_s: float = 0.0, avg_speed_bps: float = 0.0):
        t = theme_mgr.tokens
        self._bar.setValue(100)
        if saved_path:
            self._saved_path = Path(saved_path)
        self._fname.setFullText(saved_name)
        self._apply_badge(saved_name)
        # Build a one-line completion summary: total size · time taken · avg speed.
        total = max(int(self.info.get("size", 0)), 1)
        bits = [f"🎉 Saved · {_format_size(total)}"]
        if elapsed_s and elapsed_s > 0:
            bits.append(_format_elapsed(elapsed_s))
        if avg_speed_bps and avg_speed_bps > 0:
            bits.append(f"avg {_format_size(avg_speed_bps)}/s")
        self._status.setText("   ·   ".join(bits))
        self._status.setStyleSheet(
            f"font-size: {UiScale.px(12)}px; font-weight: 700; color: {t.success_text};")
        self._open_btn.setEnabled(True)
        self._time.setText(_time_now_label())

    def mark_failed(self, error: str):
        t = theme_mgr.tokens
        self._status.setText(f"✗  {error}")
        self._status.setStyleSheet(
            f"font-size: {UiScale.px(12)}px; font-weight: 700; color: {t.danger_text};")

    def _open_folder(self):
        target = getattr(self, "_saved_path", None)
        folder = target.parent if target else self._shared_dir
        try:
            QDesktopServices.openUrl(QUrl.fromLocalFile(str(folder)))
        except Exception:
            pass


# ---------------------------------------------------------------------------
# Announcement notification (big + classroom-readable)
# ---------------------------------------------------------------------------
class AnnouncementNotification(_AeroPopup):
    """An announcement the user must acknowledge before it will go away.

    Three behaviours make it "sticky", and each closes a different escape
    route:

      * ``WindowStaysOnTopHint`` keeps it above ordinary windows.
      * ``closeEvent`` refuses every close that did not come from the OK
        button — Alt+F4, the system menu, a shell "close all windows".
      * a repeating timer re-asserts the position, because the flag alone is
        not enough. Another topmost window (a second app's alert, a
        full-screen video, LogicBoard's own toolbar) can still land above it,
        and a minimise can be triggered by Win+D or Win+M without ever
        delivering a close event.

    That last one is why this is a timer rather than a one-shot raise: the
    window has to keep being on top, not merely start that way.
    """

    def __init__(self, info: dict, parent=None):
        super().__init__(parent, width=640, height=540)
        self.info = info
        # Set by NotificationManager via set_ack_callback(). None means
        # "nothing to report back to", which is the correct state for a
        # popup built without a service behind it.
        self._ack_callback = None
        # Flipped by _on_ok. Until then closeEvent refuses to let the window
        # go, so the ONLY exit is the button.
        self._acknowledged = False
        # Re-assert timer, started on show.
        self._sticky_timer = None
        # Seconds left before OK becomes usable.
        self._read_delay_left = ANNOUNCEMENT_READ_DELAY_SECONDS
        self._delay_timer = None

        # Stay above ordinary windows from the very first frame. Combined
        # with the frameless hint the base class already applies.
        self.setWindowFlags(self.windowFlags()
                            | Qt.WindowType.WindowStaysOnTopHint)

        sc = UiScale.px
        t = theme_mgr.tokens

        root = QVBoxLayout(self)
        root.setContentsMargins(sc(40), sc(30), sc(40), sc(28))
        root.setSpacing(sc(14))

        head = QLabel("📢   New announcement")
        head.setAlignment(Qt.AlignmentFlag.AlignCenter)
        head.setStyleSheet(f"font-size: {sc(13)}px; font-weight: 800; "
                           f"letter-spacing: 2.2px; color: {t.accent};")
        root.addWidget(head)

        root.addWidget(_AvatarBadge(info.get("short_name", "?"), sc(96)),
                       alignment=Qt.AlignmentFlag.AlignCenter)

        teacher = (f"{info.get('first_name','')} {info.get('surname','')} "
                   f"{info.get('title_honorific','')}").strip()
        t_lbl = QLabel(teacher or "A teacher")
        t_lbl.setAlignment(Qt.AlignmentFlag.AlignCenter)
        t_lbl.setStyleSheet(f"font-size: {sc(16)}px; font-weight: 700; color: {t.text_primary};")
        root.addWidget(t_lbl)

        time_lbl = QLabel(_time_now_label())
        time_lbl.setAlignment(Qt.AlignmentFlag.AlignCenter)
        time_lbl.setStyleSheet(f"font-size: {sc(11)}px; color: {t.text_muted};")
        root.addWidget(time_lbl)

        root.addSpacing(sc(6))

        title_lbl = QLabel(info.get("title", ""))
        title_lbl.setAlignment(Qt.AlignmentFlag.AlignCenter)
        title_lbl.setWordWrap(True)
        title_lbl.setStyleSheet(f"font-size: {sc(24)}px; font-weight: 800; color: {t.text_primary};")
        # Selectable so the title can be copied along with the body — a
        # teacher reading out a room number or a code should not have to
        # retype it.
        title_lbl.setTextInteractionFlags(
            Qt.TextInteractionFlag.TextSelectableByMouse
            | Qt.TextInteractionFlag.TextSelectableByKeyboard)
        title_lbl.setCursor(Qt.CursorShape.IBeamCursor)
        root.addWidget(title_lbl)

        # The body is rich text so URLs in an announcement are live.
        #
        # TextBrowserInteraction is the flag set that gives BOTH behaviours
        # asked for here: it combines LinksAccessibleByMouse/ByKeyboard with
        # TextSelectableByMouse/ByKeyboard, so the message can be selected and
        # copied AND a link inside it can be tapped. Setting only
        # TextSelectableByMouse would make links dead; setting only
        # LinksAccessibleByMouse would make the text uncopyable.
        #
        # setOpenExternalLinks(True) hands the URL to QDesktopServices, which
        # is what opens it in the system default browser. Without it the label
        # emits linkActivated and nothing happens.
        msg_lbl = QLabel()
        msg_lbl.setTextFormat(Qt.TextFormat.RichText)
        msg_lbl.setText(_linkify(info.get("message", "") or "", t.accent))
        msg_lbl.setAlignment(Qt.AlignmentFlag.AlignCenter)
        msg_lbl.setWordWrap(True)
        msg_lbl.setOpenExternalLinks(True)
        msg_lbl.setTextInteractionFlags(
            Qt.TextInteractionFlag.TextBrowserInteraction)
        msg_lbl.setStyleSheet(f"font-size: {sc(15)}px; font-weight: 500; color: {t.text_secondary};")
        root.addWidget(msg_lbl)

        root.addStretch()

        # A hint under the button, but ONLY when there is a teacher device
        # waiting to hear back. On a legacy announcement (one sent by a
        # LogicShare build that predates acknowledgments) promising to tell
        # the teacher would be a lie, so the line simply isn't shown.
        self._ack_hint = QLabel("")
        self._ack_hint.setAlignment(Qt.AlignmentFlag.AlignCenter)
        self._ack_hint.setStyleSheet(
            f"font-size: {sc(11)}px; color: {t.text_muted};")
        if info.get("ack_expected"):
            self._ack_hint.setText("Your teacher will be told you have read this.")
        root.addWidget(self._ack_hint)

        brow = QHBoxLayout(); brow.addStretch()
        # "OK", not "Close". The word matters: this button is an explicit
        # acknowledgment by a human, and it is the only event that makes the
        # announcement Read on the teacher's report. Delivery and display do
        # NOT acknowledge — the whole feature exists to tell those apart.
        self._ok_btn = QPushButton("OK"); self._ok_btn.setObjectName("primary")
        self._ok_btn.setMinimumWidth(sc(150))
        self._ok_btn.setCursor(Qt.CursorShape.PointingHandCursor)
        self._ok_btn.clicked.connect(self._on_ok)
        # Starts disabled. _tick_read_delay counts it down and enables it.
        self._ok_btn.setEnabled(False)
        self._ok_btn.setText(f"OK  ({self._read_delay_left})")
        brow.addWidget(self._ok_btn); brow.addStretch()
        root.addLayout(brow)

    # ---- read delay ----
    def _tick_read_delay(self) -> None:
        """Count the OK button in, then enable it."""
        self._read_delay_left -= 1
        if self._read_delay_left > 0:
            self._ok_btn.setText(f"OK  ({self._read_delay_left})")
            return
        if self._delay_timer is not None:
            self._delay_timer.stop()
            self._delay_timer = None
        self._ok_btn.setText("OK")
        self._ok_btn.setEnabled(True)
        _log.debug("Announcement OK button enabled after %ds",
                   ANNOUNCEMENT_READ_DELAY_SECONDS)

    # ---- staying in front ----
    def _assert_front(self) -> None:
        """Put this window back on top if anything displaced it.

        Deliberately cheap and idempotent. ``raise_`` on a window that is
        already top is a no-op, and the un-minimise only runs when the state
        actually says minimised — so the common case costs nothing.

        activateWindow() is NOT called on every tick on purpose. Stealing
        focus repeatedly would fight the user's typing anywhere else on the
        machine; the window being visible and on top is what matters, and
        focus is claimed once when it first appears.
        """
        if self._acknowledged:
            return
        try:
            if self.isMinimized():
                # Win+D / Win+M minimise without sending a close, so this is
                # the only place that can catch it.
                self.showNormal()
                _log.debug("Announcement was minimised — restored")
            if not self.isVisible():
                self.show()
            self.raise_()
        except RuntimeError:
            # Window already destroyed; the timer will be stopped by
            # closeEvent, but a tick can still be in flight.
            pass

    def _on_ok(self) -> None:
        """Acknowledge, then dismiss — in that order, and never the reverse.

        Acknowledging first means a failure to queue cannot swallow the
        user's dismissal: whatever happens to the acknowledgment, the popup
        goes away, because a classroom cannot be left with an undismissable
        window on the wall because a teacher's laptop was asleep.

        Queuing is also non-blocking. The dispatcher does the network work on
        its own thread, so the button closes instantly whether the teacher
        device is present, absent or unreachable.
        """
        # Mark first: closeEvent consults this, and accept() below would
        # otherwise be refused by our own guard.
        self._acknowledged = True
        self._stop_timers()
        try:
            if self._ack_callback is not None:
                self._ack_callback(self.info)
        except Exception:
            _log.exception("Acknowledging announcement failed "
                           "(dismissing anyway)")
        self.accept()

    def _stop_timers(self) -> None:
        for attr in ("_sticky_timer", "_delay_timer"):
            t = getattr(self, attr, None)
            if t is not None:
                try:
                    t.stop()
                except Exception:
                    pass
                setattr(self, attr, None)

    # ---- refuse every exit that is not the OK button ----
    def closeEvent(self, event):
        """Ignore any close the OK button did not ask for.

        Alt+F4, the system menu and a shell-wide "close all" all arrive here.
        Until the user has acknowledged, none of them may take the
        announcement off the wall — that is the entire guarantee the teacher's
        report depends on. Once acknowledged, this is an ordinary window
        again and closes normally.
        """
        if not self._acknowledged:
            _log.debug("Close refused — announcement not acknowledged yet")
            event.ignore()
            return
        self._stop_timers()
        super().closeEvent(event)

    def reject(self):
        """Swallow Escape.

        QDialog maps Escape to reject(), which would close the window without
        ever passing through the OK button. Overriding it here is narrower
        than filtering key events and cannot be bypassed by a shortcut that
        also routes through reject().
        """
        if not self._acknowledged:
            _log.debug("Escape ignored — announcement not acknowledged yet")
            return
        super().reject()

    def showEvent(self, event):
        super().showEvent(event)
        if self._sticky_timer is None:
            self._sticky_timer = QTimer(self)
            self._sticky_timer.setInterval(_STICKY_INTERVAL_MS)
            self._sticky_timer.timeout.connect(self._assert_front)
            self._sticky_timer.start()
        if self._delay_timer is None and self._read_delay_left > 0:
            self._delay_timer = QTimer(self)
            self._delay_timer.setInterval(1000)
            self._delay_timer.timeout.connect(self._tick_read_delay)
            self._delay_timer.start()
        # Claim focus once, here, rather than on every sticky tick.
        try:
            self.raise_()
            self.activateWindow()
        except Exception:
            pass

    def set_ack_callback(self, fn) -> None:
        """Install the function invoked when the user presses OK.

        Injected rather than reaching for a global service: this popup is
        constructed in tests and previews without a LogicShareService
        anywhere, and it must not require one to be displayable.
        """
        self._ack_callback = fn


# ---------------------------------------------------------------------------
# Controller — wires LogicShareService signals to popup lifecycle
# ---------------------------------------------------------------------------
from PySide6.QtCore import QObject  # noqa: E402


class NotificationManager(QObject):
    """Connects a LogicShareService to on-screen notifications.

    Holds strong references to live popups (so Qt doesn't garbage-collect
    them) and cleans them up when they close. Drop-in: construct once with the
    running service and it handles everything.
    """

    def __init__(self, service, *, shared_dir: Path, parent=None):
        super().__init__(parent)
        self._service = service
        self._shared_dir = Path(shared_dir)
        self._file_popups: dict[str, FileNotification] = {}
        self._live: list = []   # keeps announcement + finished file popups alive

        service.upload_started.connect(self._on_started)
        service.upload_progress.connect(self._on_progress)
        service.upload_finished.connect(self._on_finished)
        service.upload_failed.connect(self._on_failed)
        # Stall watchdog signals — surfaces 'transmitter went offline' mid-
        # transfer without aborting the underlying TCP read.
        try:
            service.upload_stalled.connect(self._on_stalled)
            service.upload_resumed.connect(self._on_resumed)
        except AttributeError:
            pass     # older service build without stall signals — non-fatal
        service.announcement_received.connect(self._on_announcement)

    # ---- file transfer ----
    def _on_started(self, info: dict):
        tid = info.get("transfer_id", "")
        popup = FileNotification(info, self._shared_dir)
        self._file_popups[tid] = popup
        self._keep(popup)
        popup.show(); popup.raise_(); popup.activateWindow()

    def _on_progress(self, transfer_id: str, received, speed_bps=None):
        popup = self._file_popups.get(transfer_id)
        if popup:
            popup.update_progress(received, speed_bps)

    def _on_stalled(self, transfer_id: str, info: dict):
        popup = self._file_popups.get(transfer_id)
        if popup:
            popup.mark_stalled()

    def _on_resumed(self, transfer_id: str, info: dict):
        popup = self._file_popups.get(transfer_id)
        if popup:
            popup.mark_resumed()

    def _on_finished(self, info: dict):
        popup = self._file_popups.pop(info.get("transfer_id", ""), None)
        if popup:
            popup.mark_complete(info.get("saved_name", "file"),
                                info.get("saved_path", ""),
                                elapsed_s=float(info.get("elapsed_s", 0.0)),
                                avg_speed_bps=float(info.get("avg_speed", 0.0)))

    def _on_failed(self, transfer_id: str, error: str):
        popup = self._file_popups.pop(transfer_id, None)
        if popup:
            popup.mark_failed(error)

    # ---- announcements ----
    def _on_announcement(self, info: dict):
        popup = AnnouncementNotification(info)
        # The popup does not know about the service, and should not. It is
        # handed one callable whose only job is "the user pressed OK on
        # this"; routing, queuing and retrying all live behind that.
        popup.set_ack_callback(self._acknowledge)
        self._keep(popup)
        popup.show(); popup.raise_(); popup.activateWindow()
        _log.info("Announcement displayed: id=%s title=%r ack_expected=%s",
                  str(info.get("announcement_id") or "-")[:8],
                  str(info.get("title", ""))[:40],
                  bool(info.get("ack_expected")))

    def _acknowledge(self, info: dict) -> None:
        """Hand an OK press to the service for delivery."""
        svc = self._service
        fn = getattr(svc, "acknowledge_announcement", None)
        if fn is None:
            # Service predates acknowledgments. Nothing to do, and nothing
            # broken — the popup still dismisses.
            _log.debug("Service has no acknowledge_announcement(); skipping")
            return
        fn(info)

    # ---- lifecycle ----
    def _keep(self, popup):
        """Hold a reference until the popup is closed, then release it."""
        self._live.append(popup)
        popup.finished.connect(lambda _=0, p=popup: self._release(p))

    def _release(self, popup):
        try:
            self._live.remove(popup)
        except ValueError:
            pass
        popup.deleteLater()