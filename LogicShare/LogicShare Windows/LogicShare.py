import sys, os, json, time, socket, math, ctypes, platform, threading, requests
from pathlib import Path
from urllib.parse import quote

from PySide6.QtCore import (
    Qt, Signal, QObject, QThread, QTimer, QElapsedTimer, QPoint, QPointF, QRectF,
    QPropertyAnimation, QEasingCurve, QEventLoop, QBuffer
)
from PySide6.QtWidgets import (
    QApplication, QWidget, QVBoxLayout, QHBoxLayout, QPushButton, QLabel,
    QScrollArea, QFrame, QFileDialog, QMessageBox, QSizePolicy, QSpacerItem,
    QDialog, QLineEdit, QRadioButton, QButtonGroup,
    QProgressBar, QGraphicsDropShadowEffect, QStackedWidget, QPlainTextEdit
)
from PySide6.QtGui import (
    QColor, QPainter, QPen, QBrush, QLinearGradient, QConicalGradient,
    QRadialGradient, QIcon, QPixmap, QImage, QFont, QPainterPath,
    QShortcut, QKeySequence, QTransform, QFontMetricsF,
)
from PySide6.QtSvg import QSvgRenderer
from zeroconf import Zeroconf, ServiceBrowser

import firewall
import net_watch
import single_instance
import smart_search

# Announcement acknowledgment layer. Lives in its own module because it is a
# state machine worth testing without a QApplication, a window or a LAN —
# see logicshare_ack.py.
from logicshare_ack import (
    ACK_PROTOCOL_VERSION, ACK_TIMEOUT_SECONDS, TEACHER_ACK_PORT,
    AckServer, AnnouncementTracker, BoardState, TeacherIdentity,
)

import re as _re
import logging
import tempfile
from logging.handlers import RotatingFileHandler


# ============================================================================
#  APPLICATION PATHS
#
#  Two different directories, and conflating them is what this block exists
#  to stop:
#
#    RESOURCE_DIR  read-only files that ship WITH the build (LogicShare.svg,
#                  LogicShare.ico). Under PyInstaller these live in the
#                  bundle, which after an install sits under Program Files.
#
#    APP_DATA_DIR  everything LogicShare WRITES — the teacher profile, the
#                  announcement history, the log, downloaded updates. This is
#                  %LOCALAPPDATA%\LogicShare.
#
#  Everything used to be written next to the script, which for a frozen build
#  means inside the install directory. That is wrong in three separate ways:
#  Program Files is not user-writable, so on a real install every save either
#  failed outright or was silently redirected into a per-user VirtualStore
#  copy the app could not find again; the installer wipes the install
#  directory on update, so a profile stored there is destroyed by the next
#  version; and a machine with two accounts had them fighting over one file.
#  Writes go to the user's own AppData now, which fixes all three.
# ============================================================================
def _resource_dir():
    """Where the bundled read-only resources live."""
    if getattr(sys, "frozen", False):
        # PyInstaller sets _MEIPASS to the bundle root: the _internal folder
        # in a --onedir build, a scratch dir in --onefile. Falling back to
        # the exe's own folder keeps this sane if that ever changes.
        return Path(getattr(sys, "_MEIPASS", None) or Path(sys.executable).parent)
    return Path(__file__).resolve().parent


RESOURCE_DIR = _resource_dir()


def _default_data_dir():
    if sys.platform == "win32":
        base = os.environ.get("LOCALAPPDATA") or os.path.join(
            os.path.expanduser("~"), "AppData", "Local")
    else:
        base = os.environ.get("XDG_DATA_HOME") or os.path.join(
            os.path.expanduser("~"), ".local", "share")
    return Path(base) / "LogicShare"


def _init_data_dir():
    """Create (and return) the writable data directory.

    Falls back to RESOURCE_DIR only if AppData itself is unusable — a
    degraded mode that at least keeps the app running rather than crashing
    on import, which is the one outcome a path helper must never cause.
    """
    d = _default_data_dir()
    try:
        d.mkdir(parents=True, exist_ok=True)
        return d
    except Exception:
        return RESOURCE_DIR


APP_DATA_DIR = _init_data_dir()

# Downloaded installers land here. Kept out of the system temp dir on
# purpose: a ~200 MB download that Windows may clear mid-update is not
# something to hand the updater.
UPDATE_DIR = APP_DATA_DIR / "Updates"


# ============================================================================
#  STRUCTURED LOGGING
#
#  A single rotating-file + console logger used everywhere instead of bare
#  print()/silent excepts. The rotating handler caps disk usage for a process
#  that runs for weeks on a classroom smart board. log.exception() captures
#  full stack traces so classroom issues can be diagnosed after deployment.
# ============================================================================
def _setup_logging():
    logger = logging.getLogger("LogicShare")
    if logger.handlers:        # already configured
        return logger
    logger.setLevel(logging.INFO)
    fmt = logging.Formatter(
        "%(asctime)s  %(levelname)-7s  %(message)s",
        datefmt="%Y-%m-%d %H:%M:%S",
    )
    ch = logging.StreamHandler()
    ch.setFormatter(fmt)
    logger.addHandler(ch)
    try:
        log_path = APP_DATA_DIR / "logicshare.log"
        fh = RotatingFileHandler(
            log_path, maxBytes=1_000_000, backupCount=5, encoding="utf-8"
        )
        fh.setFormatter(fmt)
        logger.addHandler(fh)
    except Exception as e:
        logger.warning(f"File logging unavailable, console only: {e}")
    return logger


log = _setup_logging()


def _migrate_legacy_data():
    """Copy pre-AppData files into APP_DATA_DIR on first run after upgrading.

    COPY rather than move, deliberately. The legacy location is the install
    directory for a frozen build and the source tree in development; moving
    would silently gut a developer's working copy, and the leftover in
    Program Files is inert once nothing reads it. Only ever copies into a
    slot that is still empty, so a real profile can never be clobbered by a
    stale one left behind from an old install.
    """
    if APP_DATA_DIR == RESOURCE_DIR:
        return                     # degraded mode — nothing to migrate
    import shutil
    for name in ("data.json", "announcements.json"):
        src, dst = RESOURCE_DIR / name, APP_DATA_DIR / name
        try:
            if src.exists() and not dst.exists():
                shutil.copy2(src, dst)
                log.info(f"Migrated {name} into {APP_DATA_DIR}")
        except Exception:
            log.exception(f"Could not migrate {name}")


_migrate_legacy_data()
log.info(f"Data directory: {APP_DATA_DIR}")


def _atomic_write_json(path, data):
    """Write JSON atomically so the file can never be left half-written even
    if the process is killed mid-write.

    Writes to a temp file in the SAME directory (so os.replace is a
    same-filesystem atomic rename), flushes + fsyncs to force bytes to disk,
    then os.replace() — atomic on Windows and POSIX. On failure the original
    file is untouched and the temp is cleaned up.
    """
    path = Path(path)
    tmp_path = None
    try:
        fd, tmp_name = tempfile.mkstemp(
            prefix=path.stem + "_", suffix=".tmp", dir=str(path.parent)
        )
        tmp_path = Path(tmp_name)
        with os.fdopen(fd, "w", encoding="utf-8") as f:
            json.dump(data, f, indent=2, ensure_ascii=False)
            f.flush()
            os.fsync(f.fileno())
        os.replace(tmp_path, path)   # atomic commit
        return True
    except Exception:
        log.exception(f"Atomic write failed for {path}")
        if tmp_path is not None:
            try:
                if tmp_path.exists():
                    tmp_path.unlink()
            except Exception:
                log.exception("Failed to clean up temp file")
        return False


# ============================================================================
#  RESPONSIVE UI SCALING
#
#  All hardcoded sizes in this file are tuned for a 1080p (1920×1080) display.
#  On higher-resolution screens (e.g. a 4K Windows 11 smart board) those sizes
#  would render tiny. Instead of hand-tuning every widget, we compute ONE
#  scale factor from the screen height (1080 = 1.0 reference) and apply it
#  globally by monkeypatching the QWidget / QLayout sizing + stylesheet
#  methods. Every setFixedSize / setMinimumSize / resize / margins / spacing
#  and every `Npx` in a stylesheet is multiplied by UI_SCALE automatically —
#  so the entire UI scales proportionally with zero per-widget changes.
#
#  Custom-painted widgets read self.width()/self.height() (already scaled),
#  so their drawing scales too.
# ============================================================================
UI_SCALE = 1.0   # set at runtime from the screen resolution (see _init_scaling)

# Set this to a number (e.g. 1.25, 1.5, 2.0) to FORCE a scale and bypass
# auto-detection entirely. Leave as None for automatic resolution-based scaling.
UI_SCALE_OVERRIDE = None

# Extra multiplier applied to TEXT ONLY, on top of UI_SCALE.
#
# Deliberately separate from UI_SCALE rather than folded into it. UI_SCALE is
# derived from the screen and governs how much room the layout has; raising it
# to enlarge type would also enlarge every panel, margin and window, which is
# how you end up back at a window that does not fit. This knob moves the type
# and nothing else — the surrounding boxes grow only as far as their own
# content forces them to, because the layouts are size-hint driven.
#
# 1.12 is the "a little bigger" the type needed to read comfortably at the
# back of a classroom. Raise toward 1.25 for noticeably larger text; set to
# 1.0 to switch the adjustment off entirely.
FONT_SCALE = 1.12


def sc(n):
    """Scale a single numeric size by the global UI scale factor."""
    return int(round(n * UI_SCALE))


def scss(css):
    """Scale the `Npx` values in a stylesheet string.

    Two SEPARATE passes, and the order matters:

      1. every `Npx` is multiplied by UI_SCALE — sizes, borders, radii and
         font sizes alike;
      2. `font-size` alone is then multiplied again by FONT_SCALE.

    Doing it in one pass is not possible, because the generic rule cannot tell
    a font size from a border radius. Doing the font pass FIRST would mean the
    generic pass scaled those values a second time, which compounds into
    wildly oversized type on a 4K board — the exact class of bug that the
    devicePixelRatio double-scaling was.

    Safe because `px` is only ever used for sizes/borders/radii — never inside
    rgba()/gradient(), which use unitless numbers.
    """
    if abs(UI_SCALE - 1.0) >= 0.001:
        css = _re.sub(
            r'(\d+(?:\.\d+)?)px',
            lambda m: f"{int(round(float(m.group(1)) * UI_SCALE))}px",
            css,
        )
    if abs(FONT_SCALE - 1.0) >= 0.001:
        css = _re.sub(
            r'(font-size\s*:\s*)(\d+(?:\.\d+)?)px',
            lambda m: f"{m.group(1)}{int(round(float(m.group(2)) * FONT_SCALE))}px",
            css,
            flags=_re.IGNORECASE,
        )
    return css


def _init_scaling(app):
    """Compute UI_SCALE from the primary screen and install global scaling.
    Called once, right after QApplication is created and before any window
    is built.

    IMPORTANT: this measures LOGICAL pixels — ``geometry().height()`` as Qt
    reports it — and must NOT be multiplied by devicePixelRatio.

    That multiplication was the scaling bug. Qt already scales the whole UI
    by devicePixelRatio: every logical pixel we lay out gets painted as
    ``dpr`` device pixels. Multiplying our own factor by dpr as well applies
    the display scaling TWICE, so the UI came out dpr times too large — on a
    4K board at 150% Windows scaling, UI_SCALE computed 2.0 and Qt then drew
    each of those at 1.5×, for an effective 3.0× against a design tuned for
    1.0×. Windows-sized chrome looked normal beside a LogicShare window with
    enormous everything, which is exactly the reported symptom.

    Logical height is also the right quantity on its own terms: it is the
    space we actually have to lay out in. A 1080p screen at 125% scaling
    gives us 864 logical pixels of height, and a layout designed for 1080
    genuinely does have to shrink to ~0.8 to fit it — Qt then paints that at
    1.25×, so it lands at the same physical size the designer intended and
    still fits on screen. The old code refused that 0.8, called it wrong,
    and overflowed the window instead.

    Worked examples, where "effective" = UI_SCALE × dpr, i.e. what the user
    actually sees relative to the 1080p reference:

        1920×1080 @ 100%   logical 1080  dpr 1.00   scale 1.00  effective 1.00
        1920×1080 @ 125%   logical  864  dpr 1.25   scale 0.80  effective 1.00
        3840×2160 @ 150%   logical 1440  dpr 1.50   scale 1.33  effective 2.00
        3840×2160 @ 100%   logical 2160  dpr 1.00   scale 2.00  effective 2.00
    """
    global UI_SCALE
    if UI_SCALE_OVERRIDE is not None:
        UI_SCALE = float(UI_SCALE_OVERRIDE)
    else:
        try:
            scr = app.primaryScreen()
            # Logical height, as laid out in. NOT × devicePixelRatio.
            geo_h = scr.geometry().height() if scr else 1080
            # Clamp so an unusual display never produces an absurd factor.
            # The floor is below 0.8 on purpose: 1080p at 150% scaling is a
            # real classroom configuration and needs 0.67 to fit at all.
            UI_SCALE = max(0.62, min(geo_h / 1080.0, 2.5))
        except Exception:
            UI_SCALE = 1.0

    # NOTE: no early return at UI_SCALE == 1.0 any more.
    #
    # There used to be one, on the reasoning that a 1.0 factor has nothing to
    # scale. That stopped being true when FONT_SCALE arrived: the hooks below
    # are also what applies the type adjustment, so returning early meant text
    # stayed at its literal size on exactly the 1080p boards the reference was
    # chosen for. The geometry hooks are harmless at 1.0 — sc(n) returns n —
    # so installing them unconditionally costs a multiply per call and keeps
    # one code path instead of two.
    from PySide6.QtWidgets import QWidget, QLayout, QBoxLayout
    from PySide6.QtCore import QSize, QMargins

    # ---- stylesheet px scaling ----
    _ss = QWidget.setStyleSheet
    QWidget.setStyleSheet = lambda self, css: _ss(self, scss(css))

    # ---- widget sizing ----
    _fs = QWidget.setFixedSize
    def _fs_s(self, *a):
        if len(a) == 1: _fs(self, QSize(sc(a[0].width()), sc(a[0].height())))
        else:           _fs(self, sc(a[0]), sc(a[1]))
    QWidget.setFixedSize = _fs_s

    _fw = QWidget.setFixedWidth;  QWidget.setFixedWidth  = lambda self, w: _fw(self, sc(w))
    _fh = QWidget.setFixedHeight; QWidget.setFixedHeight = lambda self, h: _fh(self, sc(h))

    _msz = QWidget.setMinimumSize
    def _msz_s(self, *a):
        if len(a) == 1: _msz(self, QSize(sc(a[0].width()), sc(a[0].height())))
        else:           _msz(self, sc(a[0]), sc(a[1]))
    QWidget.setMinimumSize = _msz_s

    _mw = QWidget.setMinimumWidth;  QWidget.setMinimumWidth  = lambda self, w: _mw(self, sc(w))
    _mh = QWidget.setMinimumHeight; QWidget.setMinimumHeight = lambda self, h: _mh(self, sc(h))
    _xw = QWidget.setMaximumWidth;  QWidget.setMaximumWidth  = lambda self, w: _xw(self, sc(w))
    _xh = QWidget.setMaximumHeight; QWidget.setMaximumHeight = lambda self, h: _xh(self, sc(h))

    _rsz = QWidget.resize
    def _rsz_s(self, *a):
        if len(a) == 1: _rsz(self, QSize(sc(a[0].width()), sc(a[0].height())))
        else:           _rsz(self, sc(a[0]), sc(a[1]))
    QWidget.resize = _rsz_s

    # ---- layout margins / spacing ----
    _scm = QLayout.setContentsMargins
    def _scm_s(self, *a):
        if len(a) == 1:
            m = a[0]; _scm(self, QMargins(sc(m.left()), sc(m.top()), sc(m.right()), sc(m.bottom())))
        else:
            _scm(self, sc(a[0]), sc(a[1]), sc(a[2]), sc(a[3]))
    QLayout.setContentsMargins = _scm_s

    _ssp = QLayout.setSpacing; QLayout.setSpacing = lambda self, s: _ssp(self, sc(s))
    _asp = QBoxLayout.addSpacing; QBoxLayout.addSpacing = lambda self, s: _asp(self, sc(s))

    log.info(f"UI scale: {UI_SCALE:.2f}x   font scale: {FONT_SCALE:.2f}x "
             f"(effective type {UI_SCALE * FONT_SCALE:.2f}x)")


# ============================================================================
#  THEME
# ============================================================================
def _channels(token: str) -> tuple[int, int, int]:
    """The r, g, b of a ``#rrggbb`` token."""
    h = token.lstrip("#")
    return int(h[0:2], 16), int(h[2:4], 16), int(h[4:6], 16)


def rgba(token: str, alpha: int) -> str:
    """``token`` at ``alpha`` (0-255), as a Qt stylesheet colour.

    This exists so that an accent-tinted fill is written in terms of the
    accent instead of repeating its channels. The file used to carry
    ``{rgba(T.ACCENT, 72)}`` in six places — correct today, and silently
    wrong the moment somebody changes ``T.ACCENT``, because nothing
    connects the two.
    """
    r, g, b = _channels(token)
    return "rgba(%d,%d,%d,%d)" % (r, g, b, alpha)


def vgrad(top: tuple, bottom: tuple) -> str:
    """A vertical two-stop gradient, from ``(r, g, b, a)`` to ``(r, g, b, a)``.

    Every elevated surface in this app is one of these, and they used to be
    written out longhand at each site. Six of them existed; two pairs
    differed by five units of alpha out of 255, which is a difference nobody
    can see and anybody can accidentally create.
    """
    return ("qlineargradient(x1:0,y1:0,x2:0,y2:1,"
            "stop:0 rgba(%d,%d,%d,%d),"
            "stop:1 rgba(%d,%d,%d,%d))" % (tuple(top) + tuple(bottom)))


def fill(c: tuple) -> str:
    """``(r, g, b, a)`` as a stylesheet colour."""
    return "rgba(%d,%d,%d,%d)" % tuple(c)


def shade(token: str, amount: float) -> str:
    """``token`` moved toward white (amount > 0) or black (amount < 0).

    Hover and pressed states are DERIVED with this rather than picked, so
    they cannot drift away from the colour they are a state of.
    """
    r, g, b = _channels(token)
    if amount >= 0:
        f = lambda c: int(round(c + (255 - c) * amount))     # noqa: E731
    else:
        f = lambda c: int(round(c * (1.0 + amount)))         # noqa: E731
    return "#%02x%02x%02x" % (f(r), f(g), f(b))


class T:
    # ── Neutral dark ramp ─────────────────────────────────────────────────
    # Solid greys in the iOS dark-mode family, from the window (darkest) up
    # through chrome and cards. There is no blur behind any of this any
    # more, so every value is OPAQUE: elevation is told by lightness, not by
    # how much of the desktop shows through.
    #
    # BODY is the window itself, a top-to-bottom fall-off (lit from above,
    # like every other surface here).
    BODY        = ((24, 24, 27), (14, 14, 16))
    LAYER_1     = "rgba(30, 30, 33, 255)"
    LAYER_2     = "rgba(40, 40, 44, 255)"
    LAYER_3     = "rgba(22, 22, 25, 255)"
    # Dialogs hold dense text: one step above the window, same as the bars.
    LAYER_DLG   = "rgba(28, 28, 31, 255)"

    # ── Strokes ──────────────────────────────────────────────────────────
    # Four steps. There were TWELVE opaque greys in this file — #2B3138,
    # #3A3F46, #3E4349, #41464D, #43484E, #454A50, #494E54, #4E5259,
    # #555A5F, #575B61, #60646A, #65696E, #696D72, #7B7F83 — several of them
    # four units apart, which is far too close to be a decision and far too
    # scattered to be an accident. They are all one of these now.
    #
    # SOFT is a divider or a barely-there edge; STROKE is the standard
    # outline; HI is a raised edge; BRIGHT is the top-lit bevel that the
    # cards and buttons use for their upper border, which is what makes them
    # read as lit from above. Neutral now, like the surfaces they outline.
    STROKE_SOFT   = "#2C2C2F"
    STROKE        = "#3A3A3D"
    STROKE_HI     = "#4A4A4F"
    STROKE_BRIGHT = "#5C5C62"
    STROKE_ACC    = "#62A2C7"
    DIVIDER       = "#2A2A2D"

    # ── Control fills ────────────────────────────────────────────────────
    # White at low alpha, over the solid surfaces. There were TEN of these
    # (10, 12, 14, 16, 18, 20, 24, 28, 38, 40 out of 255), which is more
    # steps than the eye can tell apart and exactly as many as it takes for
    # two controls doing the same job to end up different.
    #
    # PRESSED sits BELOW the rest state on purpose — a Fluent control gets
    # quieter under the finger, not louder, which is the opposite of what
    # a hover does.
    FILL_SUBTLE = "rgba(255,255,255,12)"
    FILL        = "rgba(255,255,255,16)"
    FILL_HOVER  = "rgba(255,255,255,26)"
    FILL_ACTIVE = "rgba(255,255,255,38)"
    FILL_PRESSED = FILL_SUBTLE

    # Neutral, not the cool slate they were: on neutral greys a blue-white
    # reads as a colour cast rather than as white.
    TEXT        = "#f5f5f7"
    TEXT_SEC    = "#c7c7cc"
    TEXT_MUTED  = "#9a9aa1"
    TEXT_DIM    = "#66666c"

    ACCENT      = "#4cc2ff"
    ACCENT_HI   = "#7cd0ff"
    ACCENT_LO   = "#2596e3"

    SUCCESS     = "#6ccf80"; SUCCESS_BG = "rgba(108,207,128,40)"; SUCCESS_BD = "rgba(108,207,128,120)"
    WARNING     = "#f0c14b"; WARNING_BG = "rgba(240,193,75,40)";  WARNING_BD = "rgba(240,193,75,120)"
    DANGER      = "#ff6b6b"; DANGER_BG  = "rgba(255,107,107,40)"; DANGER_BD  = "rgba(255,107,107,120)"
    DANGER_BG_HI = "rgba(255,107,107,72)"   # danger control under the cursor

    # ── Elevated surfaces ────────────────────────────────────────────────
    # Three, where there were ten. Each is (top, bottom) in rgba, and the
    # gradient is built by ``vgrad``.
    #
    # BAR is window chrome: the title bar and a section header, both of
    # which used to have their own values (alpha 150/90 and 195/150) for
    # the same job. CARD is a raised panel — there were FOUR of these, at
    # 220/200, 215/195, 225/205 and 235/225. CARD_SEL is the accent-tinted
    # selected state, which is the only one that was genuinely distinct.
    #
    # TILE is a board card in the list, and it is the clearest case of the
    # lot: the same tile painted itself two different colours depending on
    # whether the list happened to be in selection mode. Selection mode
    # changes the BORDER and shows a checkbox; it is not a different
    # surface, and the tile should not change shade underneath it.
    SURF_BAR       = ((32, 32, 35, 255), (27, 27, 30, 255))
    SURF_CARD      = ((50, 50, 54, 255), (40, 40, 44, 255))
    SURF_CARD_SEL  = ((34, 64, 96, 255), (26, 50, 78, 255))
    SURF_TILE      = (40, 40, 44, 255)
    SURF_TILE_HOVER = (48, 48, 53, 255)

    DUR_FAST    = 120
    DUR_MED     = 220
    DUR_SLOW    = 360
    EASE        = QEasingCurve.Type.OutCubic

    R_SM        = 8
    R_MD        = 12
    R_LG        = 16
    # LogicShare Android's geometry, which the main window now mirrors: a
    # board card's corners (its R_LG), and the controls in the header and
    # toolbar — search box, icon squares, bar buttons — at one height and
    # one radius so they line up as a set.
    R_CARD      = 20
    R_CTRL      = 12
    CTRL_H      = 42

    # The radius of a TOP-LEVEL WINDOW's outline. Not a style choice — it has
    # to equal the radius DWM clips the window to, or the two shapes disagree
    # and both are visible.
    #
    # DwmSetWindowAttribute(DWMWA_WINDOW_CORNER_PREFERENCE, DWMWCP_ROUND)
    # rounds at 8 device-independent pixels, and that is not configurable.
    # Painting the outline at R_LG (16) therefore drew an arc that cut across
    # the inside of the compositor's arc — the "extra border in the bottom
    # corners". They match now, so there is exactly one curve per corner.
    #
    # Deliberately NOT run through the UI_SCALE monkeypatch: DWM's 8 is in
    # Qt's own logical pixels, so scaling this would re-introduce the very
    # mismatch it exists to remove. R_LG is still correct for everything
    # INSIDE the window, which nothing clips.
    R_WINDOW    = 8

    FONT_FAMILY = "'Segoe UI Variable Display','Segoe UI Variable','Segoe UI',Helvetica,Arial,sans-serif"


# ============================================================================
#  Profile I/O
# ============================================================================
PROFILE_PATH = APP_DATA_DIR / "data.json"
REQUIRED_FIELDS = ("first_name", "surname", "title")

def load_profile():
    try:
        if PROFILE_PATH.exists():
            data = json.loads(PROFILE_PATH.read_text(encoding="utf-8"))
            if all(data.get(k) for k in REQUIRED_FIELDS):
                if not data.get("short_name"):
                    data["short_name"] = derive_short_name(
                        data["first_name"], data["surname"]
                    )
                return data
            log.warning("Profile file missing required fields; treating as no profile")
    except json.JSONDecodeError:
        # Corrupt JSON — quarantine it so a fresh profile can be created
        # instead of failing on every launch.
        log.exception("Profile JSON is corrupt; quarantining and starting fresh")
        try:
            PROFILE_PATH.replace(PROFILE_PATH.with_suffix(".corrupt"))
        except Exception:
            log.exception("Could not quarantine corrupt profile")
    except Exception:
        log.exception("Could not load profile")
    return None

def save_profile(first_name, surname, short_name, title):
    ok = _atomic_write_json(PROFILE_PATH, {
        "first_name": first_name,
        "surname":    surname,
        "short_name": short_name,
        "title":      title,
    })
    if ok:
        log.info("Profile saved")
    return ok


def derive_short_name(first_name, surname):
    parts = (first_name + " " + surname).split()
    return "".join(p[0].upper() for p in parts if p)[:4]


# ============================================================================
#  Announcement history — local JSON store of sent broadcasts
# ============================================================================
HISTORY_PATH = APP_DATA_DIR / "announcements.json"
HISTORY_LIMIT = 50

def load_history():
    """Return the list of past announcements (newest first). Tolerant of a
    missing or corrupt file — always returns a list (never raises)."""
    try:
        if HISTORY_PATH.exists():
            data = json.loads(HISTORY_PATH.read_text(encoding="utf-8"))
            if isinstance(data, list):
                return data
            log.warning("History file is not a list; ignoring")
    except json.JSONDecodeError:
        log.exception("History JSON is corrupt; quarantining and starting fresh")
        try:
            HISTORY_PATH.replace(HISTORY_PATH.with_suffix(".corrupt"))
        except Exception:
            log.exception("Could not quarantine corrupt history")
    except Exception:
        log.exception("Could not load history")
    return []

def save_history(entries):
    if not _atomic_write_json(HISTORY_PATH, entries[:HISTORY_LIMIT]):
        log.error("Failed to persist announcement history")

def new_announcement_id():
    """A fresh id for one announcement.

    Generated on the TEACHER device, before anything is sent, so the same
    logical announcement carries one identity everywhere: in this history
    file, in every board's duplicate-suppression set, and in every
    acknowledgment that comes back. A board-generated id could not do that —
    five boards would invent five ids for one announcement.
    """
    return "an_" + os.urandom(8).hex()


def add_history_entry(title, message, recipients, announcement_id=None):
    """Prepend a new entry and persist. `recipients` is a list of
    {class, room, ip} dicts that were targeted.

    ``announcement_id`` ties this row to the acknowledgment tracker. Optional
    so older call sites keep working; a row without one simply has no
    per-board acknowledgment report.
    """
    entries = load_history()
    entries.insert(0, {
        "id":              os.urandom(6).hex(),
        "announcement_id": announcement_id or "",
        "title":           title,
        "message":         message,
        "recipients":      recipients,
        "sent_at":         time.time(),
    })
    save_history(entries)
    return entries

def delete_history_entry(entry_id):
    entries = [e for e in load_history() if e.get("id") != entry_id]
    save_history(entries)
    return entries

def clear_history():
    save_history([])
    return []


# ============================================================================
#  Windows app identity
# ============================================================================
#  By default Windows sees a Python script as "python.exe" — so LogicShare
#  groups under the generic Python taskbar icon, Alt-Tab shows the Python
#  feather, and pinning to the taskbar pins Python rather than LogicShare.
#
#  Microsoft's official solution is the Application User Model ID (AUMID).
#  Any process that calls `SetCurrentProcessExplicitAppUserModelID` BEFORE
#  creating its first window is treated by the shell as its own application:
#  it gets its own taskbar slot, its own Alt-Tab entry, its own pin target.
#
#  Reference: https://learn.microsoft.com/windows/win32/shell/appids
# ============================================================================

# Stable identifier in `CompanyName.ProductName[.SubProduct]` form per MS conventions.
# Must not change between releases or the shell will treat updates as separate apps.
APP_USER_MODEL_ID = "InnovaLogiX.LogicShare"

# ---------------------------------------------------------------------------
# Version identity — bumping THIS line is the whole release ritual.
#
# It has to agree with three things outside this file or the updater
# misbehaves: the VERSIONINFO block in LogicShareInstaller/resources.rc,
# the version folder the installer creates, and the tag on the GitHub
# release. If the app under-reports its version it will offer an "update"
# to the build already installed; if it over-reports, it will never see a
# real one. Format is MAJOR.MINOR.PATCH, and GitHub tags carry a leading
# "v" that _parse_version strips.
# ---------------------------------------------------------------------------
APP_VERSION = "1.1.2"

# The repo whose Releases page publishes LogicShare builds. The updater is
# inert while this is blank, which is what makes a dev build safe to run.
#
# ONE repo for the whole ecosystem. Releases are told apart by tag:
#     LogicShare-v1.1.2   this app's installer AND the Android APK
#     LogicBoard-v3.1.3   LogicBoard — not ours, never offered here
# so the check lists releases and picks the newest one tagged for us.
GITHUB_REPOSITORY = "YashWarrior44/LogicClassroom-Ecosystem"
RELEASE_TAG_PREFIX = "LogicShare-"


def _ensure_windows_ico_from_svg():
    """If LOGO_PATH is an SVG, rasterise it once into a proper multi-size .ico
    next to the script. Windows window/taskbar icons traditionally come from
    .ico files (the format encodes multiple sizes in one file), and having
    one on disk also helps PyInstaller / shortcut creation later.

    Returns the .ico path on success, or None if generation failed or
    wasn't needed. This is a one-time write — if the .ico already exists
    and is newer than the SVG, we reuse it.
    """
    if not LOGO_PATH or not LOGO_IS_SVG:
        return None
    try:
        svg_path = Path(LOGO_PATH)
        # The generated .ico is a WRITE, so it belongs in AppData — not
        # beside the SVG, which after an install is a read-only folder under
        # Program Files. Writing there either failed or got shunted into the
        # VirtualStore, so the cache check below never found its own output
        # and the icon was rebuilt on every single launch.
        ico_path = APP_DATA_DIR / (svg_path.stem + ".ico")
        # Cache: reuse existing .ico if it's at least as new as the SVG.
        if ico_path.exists() and ico_path.stat().st_mtime >= svg_path.stat().st_mtime:
            return str(ico_path)

        # PIL is the standard, dependency-light way to build a multi-size .ico.
        # If Pillow isn't installed we silently skip — the Qt QIcon still has
        # all the rasterised sizes from app_icon(), so the app remains fully
        # functional without an on-disk .ico.
        try:
            from PIL import Image
        except ImportError:
            log.debug("Pillow not installed; skipping .ico generation")
            return None

        import io as _io
        # Standard Windows icon sizes — 256 is required for high-DPI / large
        # taskbar variants; 16/20/24/32/40/48/64 cover everything else.
        sizes = [16, 20, 24, 32, 40, 48, 64, 96, 128, 256]
        images = []
        for s in sizes:
            # dpr=1.0: an .ico entry is a fixed number of real pixels.
            pm = logo_pixmap(s, dpr=1.0)
            if not pm or pm.isNull():
                continue
            buf = QBuffer()
            buf.open(QBuffer.OpenModeFlag.WriteOnly)
            pm.save(buf, "PNG")
            buf.close()
            images.append(Image.open(_io.BytesIO(bytes(buf.data()))).convert("RGBA"))
        if not images:
            return None

        # Pillow encodes a multi-resolution .ico when `sizes=` is provided
        # (it uses the image's own data for each requested size if available).
        # We sort largest first so PIL picks the highest-quality source.
        images.sort(key=lambda im: -im.size[0])
        base = images[0]
        base.save(
            ico_path,
            format="ICO",
            sizes=[(im.size[0], im.size[1]) for im in images],
        )
        log.info(f"Generated {ico_path.name} from {svg_path.name}")
        return str(ico_path)
    except Exception:
        log.exception("Could not generate .ico from SVG")
        return None


def _set_windows_app_user_model_id():
    """Tell Windows this process is LogicShare, not python.exe.

    Calling this BEFORE the first QWidget is created is what makes Windows
    give LogicShare its own taskbar slot. This is the official Microsoft
    API for the purpose — no hacks, no shortcuts.

    Safe to call on non-Windows platforms: it's a no-op.
    """
    if sys.platform != "win32":
        return
    try:
        import ctypes
        # The function lives in shell32.dll; ctypes.windll.shell32 is a
        # standard binding that ships with CPython on Windows.
        # https://learn.microsoft.com/windows/win32/api/shobjidl_core/nf-shobjidl_core-setcurrentprocessexplicitappusermodelid
        hresult = ctypes.windll.shell32.SetCurrentProcessExplicitAppUserModelID(
            ctypes.c_wchar_p(APP_USER_MODEL_ID)
        )
        if hresult != 0:
            log.warning(f"SetCurrentProcessExplicitAppUserModelID returned HRESULT 0x{hresult:08X}")
        else:
            log.info(f"Windows AppUserModelID set: {APP_USER_MODEL_ID}")
    except Exception:
        # If we can't set the AUMID (very old Windows, sandboxing, etc.) we
        # just continue — the app still works, it just groups under Python.
        log.exception("Could not set Windows AppUserModelID")



def _find_logo():
    base = RESOURCE_DIR
    # SVG first — scales perfectly at any size; ICO as fallback.
    for name in ("LogicShare.svg", "logicshare.svg", "logo.svg", "icon.svg", "app.svg"):
        p = base / name
        if p.exists():
            return str(p)
    for name in ("LogicShare.ico", "logicshare.ico", "logo.ico", "icon.ico", "app.ico"):
        p = base / name
        if p.exists():
            return str(p)
    for ext in (".svg", ".ico"):
        hits = list(base.glob(f"*{ext}"))
        if hits:
            return str(hits[0])
    return None

LOGO_PATH = _find_logo()
LOGO_IS_SVG = bool(LOGO_PATH and LOGO_PATH.lower().endswith(".svg"))


def app_icon():
    """Build a QIcon. For SVG, pre-rasterise at common sizes so the OS
    always picks a crisp variant; for ICO, just load directly."""
    if not LOGO_PATH:
        return QIcon()
    if LOGO_IS_SVG:
        icon = QIcon()
        for size in (16, 20, 24, 32, 40, 48, 64, 96, 128, 256):
            # dpr=1.0: these are the sizes Windows asks for in real pixels.
            pm = logo_pixmap(size, dpr=1.0)
            if pm and not pm.isNull():
                icon.addPixmap(pm)
        return icon
    return QIcon(LOGO_PATH)


def logo_pixmap(size=24, *, dpr=None):
    """Render the logo at ``size`` LOGICAL pixels.

    ``dpr`` is the device-pixel ratio to rasterise at. None means "ask the
    screen", which is what anything going into a QLabel wants: the pixmap
    comes back with that ratio stamped on it, so Qt draws it at ``size``
    logical pixels out of ``size * dpr`` real ones, and it stays sharp on a
    150%-scaled 4K board instead of being a blurry upscale.

    Pass ``dpr=1.0`` when the caller needs a pixmap of an exact number of
    REAL pixels — the OS icon sizes and the .ico writer both do, because
    Windows is asking for a 16x16 image, not a 16-logical-pixel one.
    """
    if not LOGO_PATH:
        return None
    if dpr is None:
        try:
            scr = QApplication.primaryScreen()
            dpr = float(scr.devicePixelRatio()) if scr is not None else 1.0
        except Exception:
            dpr = 1.0
    dpr = max(1.0, float(dpr))
    px = max(1, int(round(size * dpr)))
    if LOGO_IS_SVG:
        renderer = QSvgRenderer(LOGO_PATH)
        if not renderer.isValid():
            return None
        pm = QPixmap(px, px)
        pm.fill(Qt.GlobalColor.transparent)
        painter = QPainter(pm)
        painter.setRenderHint(QPainter.RenderHint.Antialiasing)
        painter.setRenderHint(QPainter.RenderHint.SmoothPixmapTransform)
        renderer.render(painter)
        painter.end()
        pm.setDevicePixelRatio(dpr)
        return pm
    pm = QPixmap(LOGO_PATH).scaled(
        px, px,
        Qt.AspectRatioMode.KeepAspectRatio,
        Qt.TransformationMode.SmoothTransformation,
    )
    pm.setDevicePixelRatio(dpr)
    return pm

def logo_label(size=24, fallback_emoji="⚡"):
    """A QLabel showing the logo, sized to fit it exactly.

    The order here is load-bearing. ``_init_scaling`` monkeypatches
    ``setFixedSize`` to multiply by UI_SCALE, so a label asked for 36 is 29
    on a display where UI_SCALE is 0.8 — while the pixmap, built from the
    number we passed in, stayed 36. QLabel does not scale a pixmap down to
    fit (``setScaledContents`` is off, and turning it on would blur it); it
    draws it at full size and CLIPS whatever hangs over the edge. That is
    what cropped the logo in the title bar, the header and every dialog on
    any board not running at exactly the 1080p reference.

    So: set the size first, ask the label how big it actually ended up, and
    render to THAT. Correct whether or not the scaling hooks are installed.
    """
    l = QLabel()
    l.setFixedSize(size, size)
    actual = max(1, l.width())
    pm = logo_pixmap(actual)
    if pm is not None:
        l.setPixmap(pm)
    else:
        l.setText(fallback_emoji)
        l.setAlignment(Qt.AlignmentFlag.AlignCenter)
    l.setStyleSheet("background:transparent; border:none;")
    return l


# ============================================================================
#  Window corners
# ============================================================================
def _round_window_corners(hwnd):
    """Tell DWM (Win11+) to clip the actual window — including its
    body — to rounded corners.

    THE white-square bug: the accent backdrop fills the entire *rectangular*
    HWND. Our paintEvent only paints a rounded
    rect, so the 4 corners outside that path show the raw rectangular
    backdrop (which reads as a white/grey square poking out behind the
    rounded UI). Painting can't fix this — the window region itself is
    square. DwmSetWindowAttribute(DWMWA_WINDOW_CORNER_PREFERENCE = ROUND)
    makes the compositor clip the whole window to a rounded
    shape, so nothing bleeds past the corners. No-ops on Win10/non-Windows.
    """
    try:
        import ctypes
        DWMWA_WINDOW_CORNER_PREFERENCE = 33
        DWMWCP_ROUND = 2
        pref = ctypes.c_int(DWMWCP_ROUND)
        ctypes.windll.dwmapi.DwmSetWindowAttribute(
            int(hwnd), DWMWA_WINDOW_CORNER_PREFERENCE,
            ctypes.byref(pref), ctypes.sizeof(pref),
        )
    except Exception as e:
        log.debug(f"DWM corner round skipped: {e}")


# ---------------------------------------------------------------------------
# Window surface — solid, no blur
# ---------------------------------------------------------------------------
# LogicShare used to sit on a blurred backdrop (Aero, then acrylic through
# the window-accent API, then acrylic with a painted grain on top).
# All of it is gone. A blur makes every surface depend on whatever happens to
# be behind the window — fine over a dark desktop, a grey smear over a white
# page — and the work of keeping text legible on it (tint tuning, grain,
# washes) was complexity spent fighting the effect rather than designing the
# app. The surfaces are now solid, in the neutral iOS dark ramp in ``T``:
# a near-black window, chrome bars a step up, cards a step above that.
#
# What remains of the old machinery is the one part that was never about
# blur: the window is still frameless with a translucent HWND, so the body
# can be painted as a ROUNDED shape, and DWM is asked to round the window to
# the same radius so the two outlines agree.


def _prepare_window_surface(widget, *, attempts=5, retry_ms=80):
    """Round a frameless top-level window's corners (Windows 11 DWM).

    Retries because it needs a realised HWND: a window that has not been
    shown yet has no usable ``winId``, so the first attempt after ``show()``
    can legitimately arrive too early.
    """
    if getattr(widget, "_surface_prepared", False):
        return

    def _try(left):
        if getattr(widget, "_surface_prepared", False):
            return
        try:
            if not widget.isWindow() or not widget.isVisible():
                if left > 0:
                    QTimer.singleShot(retry_ms, lambda: _try(left - 1))
                return
            widget.setAttribute(Qt.WA_TranslucentBackground, True)
            _round_window_corners(int(widget.winId()))
            widget._surface_prepared = True
        except Exception as e:
            log.debug("Window surface setup skipped: %s" % e)

    QTimer.singleShot(retry_ms, lambda: _try(attempts))


def paint_window_glass(widget):
    """Paint a top-level LogicShare window's body: a solid rounded surface
    with a soft top-to-bottom fall-off, and a hairline outline.

    One function for every window. There were five copies of the fill +
    outline — the main window, FramelessWindow, FramelessDialog,
    CustomMessageBox and _SimpleDialog — which is how windows drift apart.

    The name is historical; nothing here is glass any more. The body is
    opaque, so what is behind the window can never change how it looks.
    """
    p = QPainter(widget)
    p.setRenderHint(QPainter.RenderHint.Antialiasing)
    r = QRectF(widget.rect()).adjusted(0.5, 0.5, -0.5, -0.5)
    path = QPainterPath()
    radius = 0 if widget.isMaximized() else T.R_WINDOW
    path.addRoundedRect(r, radius, radius)
    body = QLinearGradient(0, r.top(), 0, r.bottom())
    body.setColorAt(0, QColor(*T.BODY[0]))
    body.setColorAt(1, QColor(*T.BODY[1]))
    p.fillPath(path, body)
    pen = QPen(_qcolor(T.STROKE)); pen.setWidthF(1.0)
    p.setPen(pen); p.setBrush(Qt.BrushStyle.NoBrush)
    p.drawPath(path)


# ============================================================================
#  Helpers
# ============================================================================
def make_shadow(widget, *, blur=28, y=8, alpha=140):
    eff = QGraphicsDropShadowEffect(widget)
    eff.setBlurRadius(blur)
    eff.setOffset(0, y)
    eff.setColor(QColor(0, 0, 0, alpha))
    widget.setGraphicsEffect(eff)
    return eff


def grow_in(widget, *, dur=T.DUR_SLOW):
    widget.setMaximumHeight(0)
    def _go():
        target = max(widget.sizeHint().height(), widget.minimumHeight(), 60)
        anim = QPropertyAnimation(widget, b"maximumHeight", widget)
        anim.setDuration(dur)
        anim.setEasingCurve(T.EASE)
        anim.setStartValue(0)
        anim.setEndValue(target)
        anim.finished.connect(lambda: widget.setMaximumHeight(16777215))
        anim.start(QPropertyAnimation.DeletionPolicy.DeleteWhenStopped)
    QTimer.singleShot(0, _go)


class TimerAnim(QObject):
    """120 Hz timer-driven numeric animation with OutCubic easing.

    Qt's QPropertyAnimation is driven by the global QUnifiedTimer which
    defaults to ~60 Hz and can't easily be retargeted. This class drives
    updates from its own QTimer at 8 ms intervals, so on a 120 Hz display
    the BoardCard hover animation actually steps at the panel's refresh
    rate instead of being stuck at 60 fps.

    `setter` receives the current value each tick. A try/except around the
    setter call protects against the target widget being destroyed
    mid-animation (e.g. a card swept away while hover-animating).
    """
    def __init__(self, setter, duration_ms=T.DUR_MED, parent=None):
        super().__init__(parent)
        self._setter = setter
        self._duration = max(1, duration_ms)
        self._start_val = 0.0
        self._end_val = 0.0
        self._elapsed = QElapsedTimer()
        self._timer = QTimer(self)
        self._timer.timeout.connect(self._step)

    def animate(self, start, end):
        self._start_val = float(start)
        self._end_val   = float(end)
        self._elapsed.restart()
        if not self._timer.isActive():
            self._timer.start(8)   # 120 Hz

    def stop(self):
        self._timer.stop()

    def _step(self):
        t = self._elapsed.elapsed() / self._duration
        if t >= 1.0:
            try: self._setter(self._end_val)
            except RuntimeError: pass
            self._timer.stop()
            return
        # OutCubic easing (matches T.EASE used elsewhere)
        eased = 1.0 - (1.0 - t) ** 3
        try:
            self._setter(self._start_val + (self._end_val - self._start_val) * eased)
        except RuntimeError:
            self._timer.stop()


def caption_css(color=None):
    """The small-caps caption style, in a colour. A function rather than
    reading styleSheet() back: that returns the already-scaled CSS, and
    setting it again would scale every size a second time."""
    return (f"color:{color or T.TEXT_MUTED}; font-size:10px; font-weight:800;"
            f"letter-spacing:2.2px; background:transparent; border:none;")


def caption_label(text, color=None):
    l = QLabel(text)
    l.setStyleSheet(caption_css(color))
    return l


def field_label(text):
    l = QLabel(text)
    l.setStyleSheet(
        f"color:{T.TEXT_SEC}; font-size:11px; font-weight:700;"
        f"letter-spacing:0.4px; background:transparent; border:none;"
    )
    return l


def hline():
    f = QFrame()
    f.setFrameShape(QFrame.Shape.HLine)
    f.setStyleSheet(f"background:{T.DIVIDER}; border:none; max-height:1px;")
    return f


# ----- Avatar palettes
AVATAR_PALETTES = [
    ("#7cd0ff", "#2596e3"),
    ("#f56de2", "#8b3fd3"),
    ("#6ccf80", "#2e8a4a"),
    ("#f0c14b", "#d28722"),
    ("#ff8a80", "#d04848"),
    ("#67e8f9", "#0891b2"),
    ("#a78bfa", "#6d28d9"),
]

def avatar_palette(seed):
    if not seed:
        return AVATAR_PALETTES[0]
    return AVATAR_PALETTES[sum(ord(c) for c in seed.upper()) % len(AVATAR_PALETTES)]


class AvatarCircle(QWidget):
    def __init__(self, initials, size=88, parent=None):
        super().__init__(parent)
        self.initials = (initials or "?").upper()[:3]
        self._size = size
        self.setFixedSize(size, size)
        self._c1, self._c2 = avatar_palette(self.initials)

    def setInitials(self, initials):
        self.initials = (initials or "?").upper()[:3]
        self._c1, self._c2 = avatar_palette(self.initials)
        self.update()

    def paintEvent(self, _):
        p = QPainter(self)
        p.setRenderHint(QPainter.RenderHint.Antialiasing)
        # Use the actual painted width (which reflects any global UI scaling)
        # rather than the constructor's size, so the gradient + initials stay
        # correctly proportioned at higher resolutions.
        s = self.width()

        grad = QLinearGradient(0, 0, s, s)
        grad.setColorAt(0, QColor(self._c1))
        grad.setColorAt(1, QColor(self._c2))
        p.setBrush(QBrush(grad))
        p.setPen(QPen(QColor(255, 255, 255, 70), 1.5))
        p.drawEllipse(2, 2, s - 4, s - 4)

        hl = QLinearGradient(0, 0, 0, s / 2)
        hl.setColorAt(0, QColor(255, 255, 255, 50))
        hl.setColorAt(1, QColor(255, 255, 255, 0))
        p.setBrush(QBrush(hl)); p.setPen(Qt.PenStyle.NoPen)
        p.drawEllipse(3, 3, s - 6, int((s - 6) / 2))

        p.setPen(QColor("#ffffff"))
        font = QFont()
        font.setFamily("Segoe UI Variable Display")
        font.setBold(True)
        font.setPointSizeF(s * 0.30)
        p.setFont(font)
        p.drawText(self.rect(), Qt.AlignmentFlag.AlignCenter, self.initials)


# ============================================================================
#  Stylesheet fragments
# ============================================================================
ACCENT_BTN_CSS = f"""
    QPushButton {{
        background: qlineargradient(x1:0,y1:0,x2:0,y2:1,
            stop:0 {rgba(T.ACCENT_HI, 235)},
            stop:1 {rgba(T.ACCENT_LO, 230)});
        color: white;
        border: 1px solid {T.STROKE_HI};
        border-top: 1px solid {T.STROKE_BRIGHT};
        border-radius: {T.R_MD}px;
        font-size: 12px; font-weight: 700;
        padding: 8px 18px;
    }}
    QPushButton:hover {{
        background: qlineargradient(x1:0,y1:0,x2:0,y2:1,
            stop:0 {rgba(shade(T.ACCENT_HI, 0.14), 250)},
            stop:1 {rgba(shade(T.ACCENT_LO, 0.14), 245)});
    }}
    QPushButton:pressed {{
        background: qlineargradient(x1:0,y1:0,x2:0,y2:1,
            stop:0 {rgba(shade(T.ACCENT_HI, -0.20), 230)},
            stop:1 {rgba(shade(T.ACCENT_LO, -0.20), 235)});
    }}
    QPushButton:disabled {{
        background: {T.FILL}; color: {T.TEXT_DIM};
        border: 1px solid {T.STROKE_SOFT};
    }}
"""

SUBTLE_BTN_CSS = f"""
    QPushButton {{
        background: {T.STROKE_SOFT}; color: {T.TEXT};
        border: 1px solid {T.STROKE};
        border-top: 1px solid {T.STROKE_HI};
        border-radius: {T.R_MD}px;
        font-size: 12px; font-weight: 700;
        padding: 8px 16px;
    }}
    QPushButton:hover {{
        background: {T.FILL_ACTIVE};
        border: 1px solid {T.STROKE_HI};
    }}
    QPushButton:pressed {{ background: {T.FILL_SUBTLE}; }}
"""

# The main window's toolbar buttons — LogicShare Android's BarButton: one
# line, a toolbar's height rather than a hero's, the accent running
# left-to-right the way the phone draws it.
BAR_PRIMARY_CSS = f"""
    QPushButton {{
        background: qlineargradient(x1:0,y1:0,x2:1,y2:0,
            stop:0 {T.ACCENT_LO}, stop:1 {T.ACCENT_HI});
        color: white;
        border: 1px solid {rgba(T.ACCENT_HI, 150)};
        border-radius: {T.R_CTRL}px;
        font-size: 12.5px; font-weight: 700;
        padding: 0 16px;
    }}
    QPushButton:hover {{
        background: qlineargradient(x1:0,y1:0,x2:1,y2:0,
            stop:0 {shade(T.ACCENT_LO, 0.12)}, stop:1 {shade(T.ACCENT_HI, 0.12)});
    }}
    QPushButton:pressed {{
        background: qlineargradient(x1:0,y1:0,x2:1,y2:0,
            stop:0 {shade(T.ACCENT_LO, -0.18)}, stop:1 {shade(T.ACCENT_HI, -0.18)});
    }}
    QPushButton:disabled {{
        background: {T.FILL}; color: {T.TEXT_DIM}; border: 1px solid {T.STROKE_SOFT};
    }}
"""

BAR_SUBTLE_CSS = f"""
    QPushButton {{
        background: {T.FILL_HOVER}; color: {T.TEXT};
        border: 1px solid {T.STROKE};
        border-radius: {T.R_CTRL}px;
        font-size: 12.5px; font-weight: 700;
        padding: 0 16px;
    }}
    QPushButton:hover {{ background: {T.FILL_ACTIVE}; border: 1px solid {T.STROKE_HI}; }}
    QPushButton:pressed {{ background: {T.FILL_SUBTLE}; }}
"""

# A text button with no box — "Clear", "Select all": Android's accent links.
LINK_BTN_CSS = f"""
    QPushButton {{
        background: transparent; border: none; color: {T.ACCENT_HI};
        font-size: 12px; font-weight: 800; padding: 2px 6px;
    }}
    QPushButton:hover {{ color: {T.TEXT}; }}
"""

LINE_EDIT_CSS = f"""
    QLineEdit {{
        background: {T.FILL}; color: {T.TEXT};
        border: 1px solid {T.STROKE};
        border-bottom: 1.5px solid {T.STROKE};
        border-radius: {T.R_MD}px;
        padding: 11px 14px;
        font-size: 13px; font-weight: 600;
        selection-background-color: {T.ACCENT_LO};
    }}
    QLineEdit:hover  {{ background: {T.FILL_HOVER}; }}
    QLineEdit:focus  {{
        background: {T.FILL_HOVER};
        border: 1px solid {T.STROKE_HI};
        border-bottom: 2px solid {T.ACCENT};
    }}
"""

TEXT_AREA_CSS = f"""
    QPlainTextEdit {{
        background: {T.FILL}; color: {T.TEXT};
        border: 1px solid {T.STROKE};
        border-radius: {T.R_MD}px;
        padding: 10px 12px;
        font-size: 13px; font-weight: 500;
        selection-background-color: {T.ACCENT_LO};
    }}
    QPlainTextEdit:hover  {{ background: {T.FILL_HOVER}; }}
    QPlainTextEdit:focus  {{
        background: {T.FILL_HOVER};
        border: 1px solid {T.ACCENT};
    }}
"""

RADIO_CSS = f"""
    QRadioButton {{
        color: {T.TEXT}; font-size: 13px; font-weight: 600;
        background: transparent; padding: 6px 8px; spacing: 8px;
    }}
    QRadioButton::indicator {{ width: 18px; height: 18px; }}
    QRadioButton::indicator:unchecked {{
        background: {T.FILL_SUBTLE};
        border: 1.5px solid {T.STROKE_HI};
        border-radius: 10px;
    }}
    QRadioButton::indicator:unchecked:hover {{
        border: 1.5px solid {T.STROKE_BRIGHT};
    }}
    QRadioButton::indicator:checked {{
        background: qradialgradient(cx:0.5,cy:0.5,radius:0.5,
            stop:0.0 {T.ACCENT_HI}, stop:0.40 {T.ACCENT_HI},
            stop:0.45 white, stop:0.70 transparent);
        border: 1.5px solid {T.ACCENT};
        border-radius: 10px;
    }}
"""

SCROLLBAR_CSS = f"""
    QScrollArea {{ border: none; background: transparent; }}
    QScrollBar:vertical {{
        background: transparent; width: 8px; margin: 4px 2px 4px 0;
    }}
    QScrollBar::handle:vertical {{
        background: {T.FILL_ACTIVE};
        border-radius: 4px; min-height: 30px;
    }}
    QScrollBar::handle:vertical:hover {{ background: {T.STROKE_BRIGHT}; }}
    QScrollBar::add-line:vertical, QScrollBar::sub-line:vertical,
    QScrollBar::add-page:vertical, QScrollBar::sub-page:vertical {{
        background: transparent; height: 0;
    }}
"""


# ============================================================================
#  Android-parity controls — drawn icons, icon squares, the search box
#
#  The main window mirrors LogicShare Android's board list. The phone draws
#  its icons rather than using emoji (an emoji 🔍 or ⚙ renders differently on
#  every machine and sits off-centre in a round button), so these do too:
#  every glyph is a few lines of QPainter in fractions of its box, and so
#  scales cleanly with UI_SCALE.
# ============================================================================
def _draw_search_glyph(p, r, color):
    w = r.width()
    pen = QPen(color); pen.setWidthF(max(1.6, w * 0.12)); pen.setCapStyle(Qt.PenCapStyle.RoundCap)
    p.setPen(pen); p.setBrush(Qt.BrushStyle.NoBrush)
    p.drawEllipse(QPointF(r.left() + w * 0.43, r.top() + w * 0.43), w * 0.31, w * 0.31)
    p.drawLine(QPointF(r.left() + w * 0.66, r.top() + w * 0.66),
               QPointF(r.left() + w * 0.92, r.top() + w * 0.92))


def _draw_filter_glyph(p, r, color):
    """Three sliders. The track stops short of each knob, so the knob reads
    as a ring on any background — the accent gradient included."""
    w = r.width()
    stroke = max(1.4, w * 0.08)
    knob = w * 0.095
    pen = QPen(color); pen.setWidthF(stroke); pen.setCapStyle(Qt.PenCapStyle.RoundCap)
    p.setPen(pen); p.setBrush(Qt.BrushStyle.NoBrush)
    x0, x1 = r.left() + w * 0.04, r.left() + w * 0.96
    for fy, fx in ((0.18, 0.70), (0.50, 0.30), (0.82, 0.62)):
        y = r.top() + w * fy; x = r.left() + w * fx
        gap = knob + stroke
        if x - gap > x0: p.drawLine(QPointF(x0, y), QPointF(x - gap, y))
        if x + gap < x1: p.drawLine(QPointF(x + gap, y), QPointF(x1, y))
        p.drawEllipse(QPointF(x, y), knob, knob)


def _draw_gear_glyph(p, r, color):
    """Eight teeth round a ring, with the hole cut out of the path itself so
    it is transparent whatever the button is filled with."""
    w = r.width(); c = r.center()
    body = QPainterPath()
    body.addEllipse(c, w * 0.34, w * 0.34)
    for i in range(8):
        tooth = QPainterPath()
        tooth.addRoundedRect(QRectF(-w * 0.11, -w * 0.5, w * 0.22, w * 0.3), w * 0.05, w * 0.05)
        tr = QTransform(); tr.translate(c.x(), c.y()); tr.rotate(i * 45)
        body = body.united(tr.map(tooth))
    hole = QPainterPath(); hole.addEllipse(c, w * 0.14, w * 0.14)
    p.setPen(Qt.PenStyle.NoPen); p.setBrush(color)
    p.drawPath(body.subtracted(hole))


class IconSquare(QPushButton):
    """A square glass button holding a drawn icon — Android's filter and
    settings buttons. ``set_active`` fills it with the accent gradient;
    ``set_badge`` hangs a count off its top-right corner.

    A badge needs room outside the square, and a widget cannot paint outside
    itself, so a badged button is a little larger than its square and the
    square sits in its bottom-left. Lay it out bottom-aligned next to a
    CTRL_H control and the squares line up.
    """
    BADGE_ROOM = 6

    def __init__(self, glyph, tooltip, *, badge_room=False, parent=None):
        super().__init__(parent)
        self._glyph = glyph
        self._active = False
        self._badge = 0
        self._room = self.BADGE_ROOM if badge_room else 0
        side = T.CTRL_H + self._room
        self.setFixedSize(side, side)
        self.setCursor(Qt.CursorShape.PointingHandCursor)
        self.setToolTip(tooltip); self.setAccessibleName(tooltip)
        self.setAttribute(Qt.WA_Hover, True)
        self.setStyleSheet("QPushButton { background: transparent; border: none; }")

    def set_active(self, on):
        if on != self._active:
            self._active = on; self.update()

    def is_active(self):
        return self._active

    def set_badge(self, n):
        if n != self._badge:
            self._badge = n; self.update()

    def badge(self):
        return self._badge

    def paintEvent(self, _):
        p = QPainter(self)
        p.setRenderHint(QPainter.RenderHint.Antialiasing)
        k = self.width() / float(T.CTRL_H + self._room)      # UI_SCALE, measured
        room = self._room * k
        sq = QRectF(0.5, room + 0.5, self.width() - room - 1, self.height() - room - 1)
        rad = T.R_CTRL * k
        if self._active:
            g = QLinearGradient(sq.left(), 0, sq.right(), 0)
            g.setColorAt(0, QColor(T.ACCENT_LO)); g.setColorAt(1, QColor(T.ACCENT_HI))
            p.setBrush(g)
            border = QColor(255, 255, 255, 64)
        else:
            fill_tok = (T.FILL_SUBTLE if self.isDown()
                        else T.FILL_ACTIVE if self.underMouse() else T.FILL_HOVER)
            p.setBrush(_qcolor(fill_tok))
            border = QColor(T.STROKE_HI if self.underMouse() else T.STROKE)
        p.setPen(QPen(border, 1.0))
        p.drawRoundedRect(sq, rad, rad)
        gs = sq.width() * 0.45
        gr = QRectF(sq.center().x() - gs / 2, sq.center().y() - gs / 2, gs, gs)
        self._glyph(p, gr, QColor("white") if self._active else QColor(T.TEXT_SEC))
        if self._badge:
            d = 18 * k
            br = QRectF(self.width() - d - 0.5, 0.5, d, d)
            p.setBrush(QColor("white")); p.setPen(QPen(QColor(T.ACCENT), 1.5 * k))
            p.drawEllipse(br)
            f = QFont(self.font()); f.setPixelSize(max(8, int(round(10.5 * k)))); f.setWeight(QFont.Weight.Black)
            p.setFont(f); p.setPen(QColor(T.ACCENT))
            p.drawText(br, Qt.AlignmentFlag.AlignCenter, str(self._badge))


def _qcolor(token):
    """A ``T`` colour token — ``#rrggbb`` or ``rgba(r,g,b,a)`` — as a QColor."""
    if token.startswith("#"):
        return QColor(token)
    r, g, b, a = (int(v) for v in token[token.index("(") + 1:-1].split(","))
    return QColor(r, g, b, a)


class _GlyphView(QWidget):
    """A drawn glyph as a widget, recolourable — the search box's magnifier."""
    def __init__(self, glyph, size, color, parent=None):
        super().__init__(parent)
        self._glyph = glyph; self._color = QColor(color)
        self.setFixedSize(size, size)
        self.setAttribute(Qt.WA_TransparentForMouseEvents)

    def set_color(self, color):
        self._color = QColor(color); self.update()

    def paintEvent(self, _):
        p = QPainter(self); p.setRenderHint(QPainter.RenderHint.Antialiasing)
        self._glyph(p, QRectF(self.rect()).adjusted(0.5, 0.5, -0.5, -0.5), self._color)


class _ClearButton(QPushButton):
    """The ⓧ at the end of the search box — drawn, so it sits dead centre."""
    def __init__(self, parent=None):
        super().__init__(parent)
        self.setFixedSize(30, 30)
        self.setCursor(Qt.CursorShape.PointingHandCursor)
        self.setToolTip("Clear search"); self.setAccessibleName("Clear search")
        self.setFocusPolicy(Qt.FocusPolicy.NoFocus)
        self.setStyleSheet("QPushButton { background: transparent; border: none; }")

    def paintEvent(self, _):
        p = QPainter(self); p.setRenderHint(QPainter.RenderHint.Antialiasing)
        c = QRectF(self.rect()).center(); r = self.width() * 0.3
        p.setPen(Qt.PenStyle.NoPen)
        p.setBrush(QColor(T.TEXT_SEC if self.underMouse() else T.TEXT_MUTED))
        p.drawEllipse(c, r, r)
        pen = QPen(QColor(T.STROKE_SOFT)); pen.setWidthF(max(1.4, r * 0.18))
        pen.setCapStyle(Qt.PenCapStyle.RoundCap); p.setPen(pen)
        a = r * 0.38
        p.drawLine(QPointF(c.x() - a, c.y() - a), QPointF(c.x() + a, c.y() + a))
        p.drawLine(QPointF(c.x() - a, c.y() + a), QPointF(c.x() + a, c.y() - a))


class SearchBox(QFrame):
    """Android's always-visible search bar: magnifier, the field, and a clear
    button once there is something to clear. The frame lights its border in
    the accent while the field has focus. ``edit`` is the QLineEdit.

    The placeholder is TEXT_MUTED, not the phone's TEXT_DIM: TEXT_DIM on the
    field's fill is too faint to read at the back of a classroom.
    """
    escape_pressed = Signal()

    def __init__(self, placeholder, parent=None):
        super().__init__(parent)
        self.setObjectName("SearchBox")
        self.setAttribute(Qt.WA_StyledBackground, True)
        self.setFixedHeight(T.CTRL_H)
        self.setSizePolicy(QSizePolicy.Policy.Expanding, QSizePolicy.Policy.Fixed)
        lay = QHBoxLayout(self); lay.setContentsMargins(12, 0, 6, 0); lay.setSpacing(9)
        self._glyph = _GlyphView(_draw_search_glyph, 17, T.TEXT_MUTED)
        lay.addWidget(self._glyph, alignment=Qt.AlignmentFlag.AlignVCenter)
        self.edit = QLineEdit()
        self.edit.setFrame(False)
        self.edit.setPlaceholderText(placeholder)
        self.edit.setStyleSheet(
            f"QLineEdit {{ background: transparent; border: none; color: {T.TEXT};"
            f" font-size: 13.5px; font-weight: 600; padding: 0;"
            f" selection-background-color: {T.ACCENT_LO}; }}")
        pal = self.edit.palette()
        pal.setColor(pal.ColorRole.PlaceholderText, QColor(T.TEXT_MUTED))
        self.edit.setPalette(pal)
        lay.addWidget(self.edit, stretch=1)
        self._clear = _ClearButton()
        self._clear.hide()
        self._clear.clicked.connect(self._clear_text)
        lay.addWidget(self._clear, alignment=Qt.AlignmentFlag.AlignVCenter)
        self.edit.textChanged.connect(lambda t: self._clear.setVisible(bool(t)))
        self.edit.installEventFilter(self)
        self._restyle(False)

    def _clear_text(self):
        self.edit.clear()
        self.edit.setFocus()

    def _restyle(self, focused):
        self.setStyleSheet(f"""
            #SearchBox {{
                background: {T.FILL_ACTIVE if focused else T.FILL_HOVER};
                border: 1px solid {T.STROKE_ACC if focused else T.STROKE};
                border-radius: {T.R_CTRL}px;
            }}
        """)
        self._glyph.set_color(T.ACCENT_HI if focused else T.TEXT_MUTED)

    def eventFilter(self, obj, ev):
        if obj is self.edit:
            et = ev.type()
            if et == ev.Type.FocusIn:
                self._restyle(True)
            elif et == ev.Type.FocusOut:
                self._restyle(False)
            elif et == ev.Type.KeyPress and ev.key() == Qt.Key.Key_Escape:
                self.escape_pressed.emit()
                return True
        return super().eventFilter(obj, ev)

    def mousePressEvent(self, e):
        # The whole box is the target, not just the text. Accepted here so
        # the press does not travel on up to the header, which would read it
        # as the start of a window drag.
        self.edit.setFocus()
        e.accept()


class FitLabel(QLabel):
    """One line, always — Android's auto-sizing board title.

    Starts at ``max_px`` and steps the font down, as far as ``min_px``, until
    the text fits; only then does it elide. Long class names used to wrap a
    card onto two lines, which made the cards in one list different heights.
    Sizes are design pixels, scaled like every stylesheet size.
    """
    def __init__(self, text, *, color, weight=QFont.Weight.Bold, max_px=14.5, min_px=8.5, parent=None):
        super().__init__(parent)
        self._full = text
        self._max = max_px * UI_SCALE * FONT_SCALE
        self._min = min_px * UI_SCALE * FONT_SCALE
        f = QFont(); f.setFamilies(["Segoe UI Variable Display", "Segoe UI Variable", "Segoe UI"])
        f.setWeight(weight); f.setPixelSize(int(round(self._max)))
        self._base = f
        self.setFont(f)
        self.setStyleSheet(f"color:{color}; background:transparent; border:none;")
        self.setSizePolicy(QSizePolicy.Policy.Ignored, QSizePolicy.Policy.Fixed)
        self.setMinimumHeight(QFontMetricsF(f).height() + 2)
        self.setText(text)

    def full_text(self):
        return self._full

    def setText(self, text):
        self._full = text
        self._fit()

    def resizeEvent(self, e):
        super().resizeEvent(e)
        self._fit()

    def _fit(self):
        avail = max(1, self.contentsRect().width())
        f = QFont(self._base)
        px = self._max
        while True:
            f.setPixelSize(max(1, int(round(px))))
            if QFontMetricsF(f).horizontalAdvance(self._full) <= avail or px <= self._min:
                break
            px -= 0.5
        self.setFont(f)
        shown = QFontMetricsF(f).elidedText(self._full, Qt.TextElideMode.ElideRight, avail)
        QLabel.setText(self, shown)
        self.setToolTip(self._full if shown != self._full else "")


# ============================================================================
#  Backend helpers
# ============================================================================
def format_size(n):
    if n < 1024: return f"{int(n)} B"
    for unit in ("KB", "MB", "GB"):
        n /= 1024
        if n < 1024: return f"{n:.1f} {unit}"
    return f"{n / 1024:.1f} TB"

def is_network_connected():
    """Whether this machine is on a LAN worth scanning.

    Delegates to ``net_watch`` so there is one definition of "online" across
    the board service and both transmitters. The version that used to live
    here walked ``getaddrinfo(gethostname())`` and accepted any address that
    was not loopback — which meant 169.254.x.x counted as connected. That is
    the address Windows assigns when DHCP FAILS, so a Wi-Fi network that
    never actually came up presented as online: the app scanned, found
    nothing, and sat on "Looking for LogicBoards" instead of saying it was
    offline.
    """
    try:
        return net_watch.is_online()
    except Exception:
        log.debug("network check failed", exc_info=True)
        return False

def is_host_reachable(ip, port=16384, timeout=1.5):
    try:
        with socket.create_connection((ip, port), timeout=timeout):
            return True
    except ConnectionRefusedError:
        return True
    except (socket.timeout, OSError):
        return False


# ============================================================================
#  Zeroconf
# ============================================================================
class ZeroconfSignals(QObject):
    board_detected = Signal(str, str, str, str)   # class, section, room, ip
    # Emitted from the NetworkWatcher's thread. Qt queues a cross-thread
    # connection automatically, so the slot runs on the GUI thread and is
    # free to tear down Zeroconf and rebuild the card list — neither of
    # which is safe from a worker.
    network_changed = Signal(str)                 # reason


class LogicBoardListener:
    def __init__(self, signals): self.signals = signals
    def update_service(self, zc, svc, name): pass
    def remove_service(self, zc, svc, name): pass
    def add_service(self, zc, svc, name):
        info = None
        for _ in range(5):
            info = zc.get_service_info(svc, name)
            if info and info.properties:
                break
            time.sleep(0.4)
        if not info or not info.properties:
            log.warning(f"No TXT properties for service: {name}")
            return
        try:
            ip  = info.parsed_addresses()[0]
            cls = info.properties.get(b"class",   b"").decode("utf-8", errors="ignore")
            sec = info.properties.get(b"section", b"").decode("utf-8", errors="ignore")
            rm  = info.properties.get(b"room",    b"").decode("utf-8", errors="ignore")
            log.info(f"Discovered board: class={cls!r} section={sec!r} room={rm!r} ip={ip}")
            self.signals.board_detected.emit(cls, sec, rm, ip)
        except Exception as e:
            log.exception(f"Failed to parse service {name!r}")


# ============================================================================
#  PulseDot
# ============================================================================
class PulseDot(QWidget):
    def __init__(self, color="#6ccf80", parent=None):
        super().__init__(parent)
        self._color = QColor(color)
        self._t = 0.0
        self.setFixedSize(12, 12)
        self._timer = QTimer(self)
        self._timer.timeout.connect(self._tick)
        self._timer.start(50)

    def set_color(self, color):
        self._color = QColor(color); self.update()

    def _tick(self):
        self._t = (self._t + 0.06) % 6.28318
        self.update()

    def paintEvent(self, _):
        p = QPainter(self); p.setRenderHint(QPainter.RenderHint.Antialiasing)
        cx, cy = self.width() / 2, self.height() / 2
        pulse = (math.sin(self._t) + 1) / 2
        halo = QColor(self._color); halo.setAlpha(int(60 + 80 * pulse))
        r = 3 + pulse * 3
        p.setBrush(QBrush(halo)); p.setPen(Qt.PenStyle.NoPen)
        p.drawEllipse(QPointF(cx, cy), r, r)
        p.setBrush(QBrush(self._color))
        p.drawEllipse(QPointF(cx, cy), 2.6, 2.6)


# ============================================================================
#  ScanningRadar — sci-fi radar with centered antenna emoji
#
#  Layers (painted bottom → top):
#    1. Static ring grid + crosshair (decorative grid)
#    2. Expanding center pulses (waves emanating from antenna)
#    3. Rotating sweep with conical-gradient TRAIL (the iconic radar arc)
#    4. Sweep line itself — soft glow + mid glow + sharp inner line
#    5. Center antenna platform — radial glow + solid dot
#  Then a child QLabel renders 📡 on top, perfectly centered.
#
#  Performance:
#    • Single ~30 FPS QTimer
#    • All effects via QPainter primitives (no graphics-effect blurs)
#    • Conical gradient trail is one draw call — much cheaper than N wedges
#    • Auto-stops timer on hide so it's free when not scanning
# ============================================================================
class ScanningRadar(QWidget):
    def __init__(self, parent=None):
        super().__init__(parent)
        self.setAttribute(Qt.WA_TranslucentBackground)
        self.setMinimumSize(260, 260)
        self.setSizePolicy(QSizePolicy.Policy.Preferred, QSizePolicy.Policy.Preferred)

        self._sweep_angle = 0.0
        self._pulse_phase = 0.0

        # The 📡 emoji as a child label so it always sits on top, perfectly centered
        self._antenna = QLabel("📡", self)
        self._antenna.setAlignment(Qt.AlignmentFlag.AlignCenter)
        self._antenna.setAttribute(Qt.WA_TransparentForMouseEvents)
        self._antenna.setStyleSheet(
            "background:transparent; border:none; color:white; font-size:38px;"
        )
        self._antenna.setFixedSize(64, 64)

        # Time-based animation — speed stays constant even if frames drop,
        # and bumping the timer interval doesn't change perceived rotation speed.
        self._elapsed = QElapsedTimer()
        self._timer = QTimer(self)
        self._timer.timeout.connect(self._tick)

    # ---- lifecycle: tie animation cost to visibility ----
    def showEvent(self, e):
        super().showEvent(e)
        if not self._timer.isActive():
            self._elapsed.restart()
            # 16ms ≈ 60 FPS for buttery smooth motion. Time-based math below
            # means even if some frames are skipped on slow machines, the
            # animation never *slows down* — it just renders fewer steps.
            self._timer.start(16)

    def hideEvent(self, e):
        super().hideEvent(e)
        self._timer.stop()

    def resizeEvent(self, e):
        super().resizeEvent(e)
        # Keep antenna pinned to the exact visual center
        s = self._antenna.width()
        self._antenna.move((self.width() - s) // 2, (self.height() - s) // 2)

    def _tick(self):
        # Drive the whole animation from wall-clock seconds so the look
        # is identical at 30/60/144 FPS, and doesn't drift if a frame
        # is delayed by GC or layout work.
        t = self._elapsed.elapsed() / 1000.0   # seconds since start
        self._sweep_angle = (t * 80.0) % 360.0      # 80°/sec → full sweep in 4.5s
        self._pulse_phase = (t * 0.42) % 1.0        # full pulse cycle ~2.4s
        self.update()

    def paintEvent(self, _):
        p = QPainter(self)
        p.setRenderHint(QPainter.RenderHint.Antialiasing)

        w, h = self.width(), self.height()
        cx, cy = w / 2.0, h / 2.0
        radius = min(w, h) / 2.0 - 8
        if radius < 20:
            return

        center = QPointF(cx, cy)

        # ── 1. Static ring grid + crosshair ───────────────────────────────
        p.setBrush(Qt.BrushStyle.NoBrush)
        for r_mult, alpha in ((1.0, 40), (0.66, 30), (0.33, 22)):
            pen = QPen(QColor(124, 208, 255, alpha))
            pen.setWidthF(1.0)
            p.setPen(pen)
            p.drawEllipse(center, radius * r_mult, radius * r_mult)

        cross_pen = QPen(QColor(124, 208, 255, 18))
        cross_pen.setWidthF(1.0)
        p.setPen(cross_pen)
        p.drawLine(QPointF(cx - radius, cy), QPointF(cx + radius, cy))
        p.drawLine(QPointF(cx, cy - radius), QPointF(cx, cy + radius))

        # ── 2. Expanding center pulses ────────────────────────────────────
        # Three overlapping rings born at the antenna and fading as they grow
        PULSE_COUNT = 3
        for i in range(PULSE_COUNT):
            t = (self._pulse_phase + i / PULSE_COUNT) % 1.0
            r = radius * t
            fade_in  = min(t / 0.10, 1.0)
            fade_out = max(0.0, 1.0 - t)
            alpha = int(150 * fade_in * fade_out)
            if alpha <= 0:
                continue
            pen = QPen(QColor(140, 215, 255, alpha))
            pen.setWidthF(1.8)
            p.setPen(pen)
            p.drawEllipse(center, r, r)

        # ── 3. Conical sweep trail ────────────────────────────────────────
        # Qt's QConicalGradient measures CCW from 3 o'clock (visually).
        # Our sweep uses screen Y-down sin/cos, which rotates clockwise visually
        # for increasing angle.  Negating aligns gradient position 0 with the
        # sweep line, with positions > 0 trailing BEHIND the sweep.
        grad = QConicalGradient(center, -self._sweep_angle)
        grad.setColorAt(0.00, QColor(140, 220, 255, 195))   # at the line
        grad.setColorAt(0.04, QColor(124, 208, 255, 135))
        grad.setColorAt(0.10, QColor(110, 195, 250,  80))
        grad.setColorAt(0.20, QColor( 96, 180, 240,  32))
        grad.setColorAt(0.30, QColor( 80, 170, 230,  10))
        grad.setColorAt(0.36, QColor( 76, 194, 255,   0))
        grad.setColorAt(1.00, QColor( 76, 194, 255,   0))
        p.setBrush(QBrush(grad))
        p.setPen(Qt.PenStyle.NoPen)
        p.drawEllipse(center, radius, radius)

        # ── 4. Sweep line: soft glow + mid glow + sharp inner ─────────────
        rad = math.radians(self._sweep_angle)
        ex = cx + math.cos(rad) * radius
        ey = cy + math.sin(rad) * radius
        end = QPointF(ex, ey)

        for width, color in (
            (7.5, QColor(180, 230, 255,  55)),   # wide soft glow
            (3.8, QColor(200, 235, 255, 115)),   # mid glow
            (1.6, QColor(235, 248, 255, 235)),   # sharp inner line
        ):
            pen = QPen(color)
            pen.setWidthF(width)
            pen.setCapStyle(Qt.PenCapStyle.RoundCap)
            p.setPen(pen)
            p.drawLine(center, end)

        # Glowing tip dot at the end of the sweep
        tip = QRadialGradient(end, 9)
        tip.setColorAt(0.0, QColor(220, 240, 255, 230))
        tip.setColorAt(0.5, QColor(140, 215, 255, 120))
        tip.setColorAt(1.0, QColor(140, 215, 255,   0))
        p.setBrush(QBrush(tip)); p.setPen(Qt.PenStyle.NoPen)
        p.drawEllipse(end, 10, 10)

        # ── 5. Center antenna platform ────────────────────────────────────
        plate_r = 34
        plate_glow = QRadialGradient(center, plate_r + 8)
        plate_glow.setColorAt(0.0, QColor(76, 194, 255, 130))
        plate_glow.setColorAt(0.6, QColor(76, 194, 255,  55))
        plate_glow.setColorAt(1.0, QColor(76, 194, 255,   0))
        p.setBrush(QBrush(plate_glow)); p.setPen(Qt.PenStyle.NoPen)
        p.drawEllipse(center, plate_r + 8, plate_r + 8)

        # Inner solid backplate — gives the emoji a clean, dark base
        plate_fill = QRadialGradient(center, plate_r)
        plate_fill.setColorAt(0.0, QColor(28, 38, 56, 245))
        plate_fill.setColorAt(1.0, QColor(16, 22, 34, 230))
        p.setBrush(QBrush(plate_fill))
        p.setPen(QPen(QColor(140, 215, 255, 170), 1.4))
        p.drawEllipse(center, plate_r, plate_r)


# ============================================================================
#  Upload backend
# ============================================================================
# ============================================================================
#  Upload backend — MAXIMUM-THROUGHPUT IMPLEMENTATION
# ============================================================================
#
# The desktop upload path is now hand-written at the socket level instead of
# using ``requests.post``. This isn't premature optimization — every layer we
# just bypassed was a real, measurable cost:
#
#  1. ``requests`` → ``urllib3`` → ``http.client`` — three layers of pure-
#     Python bookkeeping per byte, all of which we skip.
#
#  2. urllib3 chunked its reads to 8 KB regardless of what we passed in.
#     A generator with 256 KB chunks helped (the previous optimization),
#     but the Python-level ``next()`` → ``socket.send()`` loop still had to
#     do a full Python-call-and-syscall pair per chunk.
#
#  3. THE BIG ONE: ``socket.sendfile()`` uses the kernel's native
#     ``TransmitFile`` (Windows) / ``sendfile()`` (Linux) / ``sendfile()``
#     (macOS) syscall. That's ZERO-COPY: the file descriptor is handed to
#     the kernel and the kernel streams it straight from the file-system
#     cache to the network card, without ever bouncing through Python
#     user-space at all. This is the same technique nginx and Apache use.
#     Empirically: on a fast link + wired receiver, ``sendfile`` goes 3-5×
#     faster than the fastest possible ``requests.post(data=generator)``.
#
#  4. Tuned socket options for bulk upload:
#       * TCP_NODELAY = 1  → disable Nagle. We know we're doing bulk
#         transfer, not RPC-style small writes; batching-for-latency is
#         pure loss here.
#       * SO_SNDBUF = 4 MB → give the kernel more room to buffer outgoing
#         data. Default is ~64 KB on Windows, which caps throughput on
#         high-RTT Wi-Fi links regardless of everything else.
#
# What we still do the same as before:
#   * Same wire protocol (same URL, same headers, same POST body format).
#     The LogicBoard receiver is 100% unchanged.
#   * Same 50 ms progress throttle, same final-100% guarantee.
#   * Same (5s connect, 300s idle) timeout policy.
#   * Same error taxonomy — ConnectionError / Timeout / other, converted
#     into the same friendly messages the UI already knows how to show.
# ============================================================================

# Module-level tuning knobs
UPLOAD_SO_SNDBUF   = 4 * 1024 * 1024   # 4 MB — bulk send buffer
PROGRESS_THROTTLE  = 0.05              # 50 ms ≈ 20 FPS
UPLOAD_CONNECT_TIMEOUT = 5.0
UPLOAD_IDLE_TIMEOUT    = 300.0


class UploadError(Exception):
    """Raised by _send_file_over_socket. Message is user-friendly."""
    def __init__(self, kind, message):
        super().__init__(message)
        self.kind = kind          # 'connect' | 'timeout' | 'protocol' | 'io'
        self.message = message


def _send_file_over_socket(url, headers, file_path, on_progress):
    """Hand-crafted HTTP/1.1 POST that uses ``socket.sendfile()`` for the body.

    Bypasses ``requests``, ``urllib3`` and ``http.client`` entirely — the file
    is streamed from the OS file cache directly to the socket by the kernel,
    zero-copy, zero Python overhead per byte.

    Args
    ----
    url          : full ``http://ip:port/path`` — we parse it manually because
                   we're not using any HTTP library.
    headers      : dict of str -> str. Content-Type / Content-Length are set
                   here; caller supplies the LogicBoard-specific X-* headers.
    file_path    : path to the file to send.
    on_progress  : callable(sent_bytes, total_bytes). Called throttled to
                   PROGRESS_THROTTLE seconds. Final call with (total, total)
                   is always emitted.

    Returns
    -------
    (status_code:int, response_body:bytes)

    Raises
    ------
    UploadError — with .kind set to 'connect' / 'timeout' / 'protocol' / 'io'
    for clean upstream dispatching.
    """
    from urllib.parse import urlparse
    parsed = urlparse(url)
    if parsed.scheme != "http":
        raise UploadError("protocol", f"Unsupported scheme: {parsed.scheme}")
    host = parsed.hostname
    port = parsed.port or 80
    path = parsed.path or "/"
    if parsed.query:
        path = f"{path}?{parsed.query}"

    file_size = os.path.getsize(file_path)

    # Compose the request line + headers as one bytestring. We flush this in
    # a single send() before handing the socket to sendfile() so the kernel
    # transitions cleanly from "user-space bytes" to "kernel-managed file".
    full_headers = {
        "Host":           f"{host}:{port}",
        "Content-Length": str(file_size),
        "Connection":     "close",
        **headers,
    }
    req_head = f"POST {path} HTTP/1.1\r\n".encode("ascii")
    for k, v in full_headers.items():
        req_head += f"{k}: {v}\r\n".encode("ascii")
    req_head += b"\r\n"

    sock = None
    fp = None
    try:
        # 1. Connect with the short 5s timeout — fail fast if the board is down.
        try:
            sock = socket.create_connection((host, port),
                                            timeout=UPLOAD_CONNECT_TIMEOUT)
        except (socket.gaierror, ConnectionRefusedError, OSError) as e:
            raise UploadError("connect", str(e)) from e

        # 2. Tune for bulk upload.
        try:
            sock.setsockopt(socket.IPPROTO_TCP, socket.TCP_NODELAY, 1)
        except OSError:
            pass  # not fatal on any platform we care about
        try:
            sock.setsockopt(socket.SOL_SOCKET, socket.SO_SNDBUF, UPLOAD_SO_SNDBUF)
        except OSError:
            pass  # kernel may clamp — no problem, we just don't get the boost

        # 3. Switch to the longer idle timeout for the bulk transfer phase.
        sock.settimeout(UPLOAD_IDLE_TIMEOUT)

        # 4. Send headers.
        sock.sendall(req_head)

        # 5. Stream the file. ``socket.sendfile`` returns the number of bytes
        #    actually sent per call; on Windows it uses TransmitFile, on Linux
        #    it uses sendfile(2), on macOS it uses sendfile(2). All three are
        #    zero-copy at the kernel level.
        #
        #    We call sendfile in a loop so we can:
        #      (a) resume from wherever the previous call stopped (partial
        #          sends are legal for sendfile),
        #      (b) throttle progress callbacks at PROGRESS_THROTTLE cadence
        #          without touching the actual byte stream.
        fp = open(file_path, "rb")
        sent_total = 0
        last_emit  = 0.0
        # Cap per-call to a reasonable window so the progress cadence stays
        # smooth. 8 MB gives good syscall amortisation while still allowing
        # ~20 FPS updates at 160 MB/s and above.
        SENDFILE_WINDOW = 8 * 1024 * 1024
        try:
            while sent_total < file_size:
                remaining = file_size - sent_total
                want = min(remaining, SENDFILE_WINDOW)
                # socket.sendfile(fp, offset=None, count=N) is the standard
                # cross-platform interface. Returns bytes actually transferred.
                bytes_sent = sock.sendfile(fp, offset=sent_total, count=want)
                if bytes_sent == 0:
                    # Peer half-closed us mid-upload.
                    raise UploadError("io", "Connection closed by peer mid-upload")
                sent_total += bytes_sent
                now = time.monotonic()
                if (now - last_emit) >= PROGRESS_THROTTLE or sent_total >= file_size:
                    last_emit = now
                    if on_progress:
                        on_progress(sent_total, file_size)
        except socket.timeout as e:
            raise UploadError("timeout", "Upload idle for too long") from e
        except OSError as e:
            raise UploadError("io", str(e)) from e

        # 6. Read the response. Simple parse — we only need status + JSON body.
        buf = bytearray()
        while b"\r\n\r\n" not in buf:
            try:
                chunk = sock.recv(4096)
            except socket.timeout as e:
                raise UploadError("timeout", "No response from receiver") from e
            if not chunk:
                break
            buf.extend(chunk)
        header_end = buf.find(b"\r\n\r\n")
        if header_end < 0:
            raise UploadError("protocol", "Malformed response (no header)")
        head_bytes = bytes(buf[:header_end])
        body = bytes(buf[header_end + 4:])

        # Status line: "HTTP/1.1 200 OK"
        first_line = head_bytes.split(b"\r\n", 1)[0]
        try:
            status_code = int(first_line.split(b" ", 2)[1])
        except (IndexError, ValueError) as e:
            raise UploadError("protocol",
                              f"Malformed status line: {first_line!r}") from e

        # Read the rest of the body honouring Content-Length. The receiver's
        # JSON response is small (< 1 KB in practice) so this is cheap.
        content_length = None
        for line in head_bytes.split(b"\r\n")[1:]:
            if b":" not in line:
                continue
            k, v = line.split(b":", 1)
            if k.strip().lower() == b"content-length":
                try: content_length = int(v.strip())
                except ValueError: pass
                break
        if content_length is not None:
            while len(body) < content_length:
                try:
                    chunk = sock.recv(min(4096, content_length - len(body)))
                except socket.timeout:
                    break
                if not chunk:
                    break
                body += chunk

        return status_code, body

    finally:
        try:
            if fp is not None: fp.close()
        except Exception:
            log.exception("Error closing upload source file")
        try:
            if sock is not None:
                try: sock.shutdown(socket.SHUT_RDWR)
                except OSError: pass
                sock.close()
        except Exception:
            log.exception("Error closing upload socket")


class UploadWorker(QThread):
    """Send a file to a LogicBoard receiver using kernel zero-copy sendfile().

    See the module-level docstring above for the full rationale. Public API
    (signals, constructor, thread lifecycle) is unchanged from the previous
    ``requests``-based version — call sites don't need to know we swapped
    the guts underneath.
    """
    progress    = Signal(object, object)
    finished_ok = Signal(dict)
    failed      = Signal(str)

    def __init__(self, url, file_path, profile):
        super().__init__()
        self.url = url
        self.file_path = file_path
        self.profile = profile

    def run(self):
        try:
            filename = os.path.basename(self.file_path)
            headers = {
                "X-First-Name":  quote(self.profile["first_name"]),
                "X-Surname":     quote(self.profile["surname"]),
                "X-Short-Name":  self.profile.get("short_name", ""),
                "X-Title":       self.profile["title"],
                "X-Filename":    quote(filename),
                "Content-Type":  "application/octet-stream",
            }
            file_size = os.path.getsize(self.file_path)
            log.info(
                f"Upload start: {filename} ({format_size(file_size)}) "
                f"→ {self.url}  [sendfile]"
            )

            status_code, body = _send_file_over_socket(
                url         = self.url,
                headers     = headers,
                file_path   = self.file_path,
                on_progress = lambda s, t: self.progress.emit(s, t),
            )

            if status_code == 200:
                log.info(f"Upload OK: {filename}")
                # Parse the receiver's JSON response if it sent one.
                try:
                    self.finished_ok.emit(json.loads(body.decode("utf-8")))
                except Exception:
                    self.finished_ok.emit({"status": "ok"})
            else:
                log.error(f"Upload failed HTTP {status_code}: {filename}")
                self.failed.emit(f"Something went wrong (HTTP {status_code})💀")

        except UploadError as e:
            # Map the specific failure kind to the friendly UI message.
            # kind is set at the throw site so we don't string-match here.
            if e.kind == "connect":
                log.warning(f"Upload connect error → {self.url}: {e.message}")
                self.failed.emit("Couldn't reach the Smart Board…💀")
            elif e.kind == "timeout":
                log.warning(f"Upload timeout → {self.url}: {e.message}")
                self.failed.emit("Took too long — try again 😅")
            else:  # 'io' | 'protocol'
                log.warning(f"Upload {e.kind} error → {self.url}: {e.message}")
                self.failed.emit("Something went wrong while sending the file…💀")
        except Exception:
            log.exception("Unexpected upload error")
            self.failed.emit("Something went wrong while sending the file…💀")


# ============================================================================
#  Announcement worker
# ============================================================================
class AnnouncementWorker(QThread):
    success = Signal(dict)
    failed  = Signal(str)

    def __init__(self, url, title, message, profile):
        super().__init__()
        self.url = url
        self.payload = {
            "title":           title,
            "message":         message,
            "first_name":      profile["first_name"],
            "surname":         profile["surname"],
            "short_name":      profile.get("short_name", derive_short_name(profile["first_name"], profile["surname"])),
            "title_honorific": profile["title"],
        }

    def run(self):
        try:
            # (connect, read): fast-fail on a dead host, short read window
            # since announcements are tiny.
            r = requests.post(self.url, json=self.payload, timeout=(5, 10))
            if r.status_code == 200:
                try: self.success.emit(r.json())
                except Exception: self.success.emit({"status": "ok"})
            else:
                log.error(f"Announce failed HTTP {r.status_code} → {self.url}")
                self.failed.emit(f"Something went wrong (HTTP {r.status_code})💀")
        except requests.exceptions.ConnectionError:
            log.warning(f"Announce connection error → {self.url}")
            self.failed.emit("Couldn't reach the Smart Board…💀")
        except requests.exceptions.Timeout:
            log.warning(f"Announce timed out → {self.url}")
            self.failed.emit("Took too long — try again 😅")
        except Exception:
            log.exception("Unexpected announce error")
            self.failed.emit("Something went wrong while sending it…💀")


class BroadcastWorker(QThread):
    """Delivers ONE announcement to MANY boards independently.

    Each board is POSTed to in sequence on this background thread (keeps the
    UI responsive). A failure on one board never aborts the rest — we record
    per-board results and emit progress as we go. This is the core of the
    'compose once → send to many' workflow.

    Signals:
      board_done(ip, ok, detail)  — emitted after each board attempt
      progress(done, total)       — running counter for the progress bar
      finished_all(ok_count, fail_count, results)
    """
    board_done   = Signal(str, bool, str)
    progress     = Signal(int, int)
    finished_all = Signal(int, int, list)
    # ip, ok, detail, board_info — everything the tracker needs to record one
    # delivery, including the board's stable id from its reply.
    delivery     = Signal(str, bool, str, dict)

    def __init__(self, targets, title, message, profile,
                 announcement_id="", teacher_device_id="", ack_port=0):
        super().__init__()
        # targets: list of dicts {class, room, ip}
        self.targets = list(targets)
        self.title = title
        self.message = message
        self.announcement_id = announcement_id
        self.payload_base = {
            "title":           title,
            "message":         message,
            "first_name":      profile["first_name"],
            "surname":         profile["surname"],
            "short_name":      profile.get("short_name", derive_short_name(profile["first_name"], profile["surname"])),
            "title_honorific": profile["title"],
        }
        # Acknowledgment fields. Added ONLY when we actually have them, so a
        # send from a build with the tracker disabled produces byte-identical
        # traffic to the old client and old boards stay happy.
        if announcement_id and teacher_device_id:
            self.payload_base.update({
                "announcement_id":   announcement_id,
                "teacher_device_id": teacher_device_id,
                "ack_port":          int(ack_port or TEACHER_ACK_PORT),
                "ack_version":       ACK_PROTOCOL_VERSION,
            })

    def run(self):
        total = len(self.targets)
        ok_count = 0
        fail_count = 0
        results = []
        for i, tgt in enumerate(self.targets, start=1):
            ip = tgt.get("ip", "")
            ok = False
            detail = ""
            board_info = {}
            try:
                r = requests.post(
                    f"http://{ip}:16384/announce",
                    json=self.payload_base,
                    timeout=(5, 10),   # (connect, read)
                )
                if r.status_code == 200:
                    ok = True
                    detail = "Delivered"
                    # The board answers with its stable identity. Capturing it
                    # here is what lets an acknowledgment arriving later —
                    # possibly from a different IP — be matched to this row.
                    try:
                        body = r.json()
                        if isinstance(body, dict):
                            board_info = body
                    except Exception:
                        pass          # legacy board: no body worth reading
                else:
                    detail = f"HTTP {r.status_code}"
                    log.error(f"Broadcast to {ip} failed HTTP {r.status_code}")
            except requests.exceptions.ConnectionError:
                detail = "Offline"
                log.warning(f"Broadcast to {ip}: offline")
            except requests.exceptions.Timeout:
                detail = "Timed out"
                log.warning(f"Broadcast to {ip}: timed out")
            except Exception:
                detail = "Error"
                log.exception(f"Broadcast to {ip}: unexpected error")

            if ok: ok_count += 1
            else:  fail_count += 1
            results.append({**tgt, "ok": ok, "detail": detail,
                            "board_device_id": board_info.get("board_device_id", "")})

            self.delivery.emit(ip, ok, detail, board_info)
            self.board_done.emit(ip, ok, detail)
            self.progress.emit(i, total)

        log.info(f"Broadcast complete: {ok_count} ok, {fail_count} failed of {total}")
        self.finished_all.emit(ok_count, fail_count, results)


# ============================================================================
#  Upload progress dialog
# ============================================================================
class _WinButton(QPushButton):
    """A single caption button (minimise / maximise / close)."""
    def __init__(self, kind, parent=None):
        super().__init__(parent)
        self._kind = kind  # "min" | "max" | "close"
        # Object-name-scoped styles (#CaptionBtn) so a dialog-level
        # `QPushButton {...}` rule (e.g. ACCENT_BTN_CSS) can NEVER cascade
        # into these caption buttons. An ID selector outranks a type
        # selector, so these win. This was the cause of the "broken title
        # bar / save button minimises the window" bug — the dialog's button
        # stylesheet was bleeding onto the min/max/close buttons.
        self.setObjectName("CaptionBtn")
        self.setFixedSize(46, 32)
        self.setCursor(Qt.CursorShape.PointingHandCursor)
        self.setFocusPolicy(Qt.FocusPolicy.NoFocus)
        if kind == "close":
            # Transparent → red on hover (standard Windows close-button look).
            self.setStyleSheet(f"""
                #CaptionBtn {{
                    background: transparent; border: none;
                    padding: 0; min-width: 0;
                    border-top-right-radius: {T.R_WINDOW}px;
                }}
                #CaptionBtn:hover   {{ background: rgba(232,80,90,235); }}
                #CaptionBtn:pressed {{ background: rgba(200,60,70,235); }}
            """)
        else:
            self.setStyleSheet(f"""
                #CaptionBtn {{
                    background: transparent; border: none;
                    padding: 0; min-width: 0; border-radius: 0px;
                }}
                #CaptionBtn:hover   {{ background: rgba(255,255,255,30); }}
                #CaptionBtn:pressed {{ background: rgba(255,255,255,16); }}
            """)

    def set_maximized(self, is_max):
        if self._kind == "max":
            self._is_max = is_max
            self.update()

    def paintEvent(self, e):
        super().paintEvent(e)  # paints the hover/press background
        p = QPainter(self)
        p.setRenderHint(QPainter.RenderHint.Antialiasing)
        pen = QPen(QColor(T.TEXT))
        pen.setWidthF(1.3)
        p.setPen(pen)
        cx, cy = self.width() / 2, self.height() / 2

        if self._kind == "min":
            # Clean centered horizontal bar (Windows 11 style)
            p.drawLine(QPointF(cx - 5, cy), QPointF(cx + 5, cy))
        elif self._kind == "max":
            if getattr(self, "_is_max", False):
                # restore glyph: two offset squares
                p.drawRect(QRectF(cx - 4, cy - 2, 7, 7))
                p.drawLine(QPointF(cx - 2, cy - 2), QPointF(cx - 2, cy - 4))
                p.drawLine(QPointF(cx - 2, cy - 4), QPointF(cx + 5, cy - 4))
                p.drawLine(QPointF(cx + 5, cy - 4), QPointF(cx + 5, cy + 3))
                p.drawLine(QPointF(cx + 5, cy + 3), QPointF(cx + 3, cy + 3))
            else:
                p.drawRect(QRectF(cx - 5, cy - 4, 10, 9))
        elif self._kind == "close":
            # X — turns white on the red hover automatically (pen stays light)
            p.drawLine(QPointF(cx - 5, cy - 5), QPointF(cx + 5, cy + 5))
            p.drawLine(QPointF(cx + 5, cy - 5), QPointF(cx - 5, cy + 5))


class TitleBar(QFrame):
    """Custom draggable title bar matching the glass UI."""
    def __init__(self, host, *, title="LogicShare", show_logo=True,
                 can_maximize=True, bare=False, parent=None):
        """``bare``: no fill, no divider, no title — just the caption
        buttons and a drag strip, for a window whose own header carries the
        name (the main window, laid out like LogicShare Android's)."""
        super().__init__(parent)
        self._host = host
        self._can_maximize = can_maximize
        self._drag_offset = None

        self.setObjectName("TitleBar")
        self.setFixedHeight(32 if bare else 40)
        self.setStyleSheet("#TitleBar { background: transparent; border: none; }" if bare else f"""
            #TitleBar {{
                background: {vgrad(*T.SURF_BAR)};
                border-top-left-radius: {T.R_WINDOW}px;
                border-top-right-radius: {T.R_WINDOW}px;
                border-bottom: 1px solid {T.DIVIDER};
            }}
        """)

        lay = QHBoxLayout(self)
        lay.setContentsMargins(12, 0, 0, 0); lay.setSpacing(8)

        if show_logo:
            lay.addWidget(logo_label(18), alignment=Qt.AlignmentFlag.AlignVCenter)

        self._title_lbl = QLabel(title)
        self._title_lbl.setStyleSheet(
            f"color:{T.TEXT}; font-size:12.5px; font-weight:700;"
            f"background:transparent; border:none; letter-spacing:0.2px;"
        )
        lay.addWidget(self._title_lbl, alignment=Qt.AlignmentFlag.AlignVCenter)
        self._title_lbl.setVisible(bool(title))
        lay.addStretch()

        self._btn_min = _WinButton("min")
        self._btn_min.clicked.connect(self._host.showMinimized)
        lay.addWidget(self._btn_min, alignment=Qt.AlignmentFlag.AlignTop)

        if can_maximize:
            self._btn_max = _WinButton("max")
            self._btn_max.clicked.connect(self._toggle_max)
            lay.addWidget(self._btn_max, alignment=Qt.AlignmentFlag.AlignTop)
        else:
            self._btn_max = None

        self._btn_close = _WinButton("close")
        self._btn_close.clicked.connect(self._host.close)
        lay.addWidget(self._btn_close, alignment=Qt.AlignmentFlag.AlignTop)

    def set_title(self, text):
        self._title_lbl.setText(text)

    def _toggle_max(self):
        if self._host.isMaximized():
            self._host.showNormal()
        else:
            self._host.showMaximized()
        self._sync_max_btn()

    def _sync_max_btn(self):
        if self._btn_max is not None:
            self._btn_max.set_maximized(self._host.isMaximized())

    # ----- drag to move / double-click maximize -----
    def mousePressEvent(self, e):
        if e.button() == Qt.MouseButton.LeftButton:
            # store global press offset relative to window top-left
            self._drag_offset = e.globalPosition().toPoint() - self._host.frameGeometry().topLeft()
            e.accept()

    def mouseMoveEvent(self, e):
        if self._drag_offset is not None and e.buttons() & Qt.MouseButton.LeftButton:
            if self._host.isMaximized():
                # Un-maximize and continue dragging from under the cursor
                self._host.showNormal()
                self._sync_max_btn()
                # Re-anchor offset proportionally to the cursor x
                w = self._host.width()
                self._drag_offset = QPoint(min(self._drag_offset.x(), w - 80), self._drag_offset.y())
            self._host.move(e.globalPosition().toPoint() - self._drag_offset)
            e.accept()

    def mouseReleaseEvent(self, e):
        self._drag_offset = None
        # Snap-to-top → maximize (Windows-style aero snap feel)
        if (self._can_maximize and not self._host.isMaximized()
                and e.globalPosition().toPoint().y() <= 2):
            self._host.showMaximized()
            self._sync_max_btn()

    def mouseDoubleClickEvent(self, e):
        if self._can_maximize and e.button() == Qt.MouseButton.LeftButton:
            self._toggle_max()


class FramelessWindow(QWidget):
    """Top-level frameless window with a solid rounded body.

    Subclasses build their content into `self.content_layout`. The window
    paints its own rounded translucent body so the corners look right even
    though the HWND is frameless.
    """
    def __init__(self, *, title="LogicShare", can_maximize=True,
                 min_size=(460, 560), parent=None):
        super().__init__(parent)
        self.setWindowFlags(
            Qt.WindowType.Window |
            Qt.WindowType.FramelessWindowHint
        )
        self.setAttribute(Qt.WA_TranslucentBackground, True)
        self.setWindowTitle(title)
        self.setWindowIcon(app_icon())
        self.setMinimumSize(*min_size)

        outer = QVBoxLayout(self)
        outer.setContentsMargins(0, 0, 0, 0); outer.setSpacing(0)

        self.title_bar = TitleBar(self, title=title, can_maximize=can_maximize)
        outer.addWidget(self.title_bar)

        # Body host — subclasses add into content_layout
        self._body = QWidget()
        self._body.setStyleSheet("background:transparent;")
        self.content_layout = QVBoxLayout(self._body)
        self.content_layout.setContentsMargins(0, 0, 0, 0)
        self.content_layout.setSpacing(0)
        outer.addWidget(self._body, stretch=1)

    def set_title(self, text):
        self.setWindowTitle(text)
        self.title_bar.set_title(text)

    def exec(self):
        """Show the window modally and block until closed, mimicking
        QDialog.exec(). FramelessWindow is a QWidget (no native exec), so we
        run a local event loop and quit it on close. Returns 1 for call-site
        compatibility (e.g. AnnouncementHistoryDialog opened via .exec())."""
        self.setWindowModality(Qt.WindowModality.ApplicationModal)
        self._modal_loop = QEventLoop()
        self.show()
        self._modal_loop.exec()
        return getattr(self, "_modal_result", 1)

    def closeEvent(self, e):
        loop = getattr(self, "_modal_loop", None)
        if loop is not None and loop.isRunning():
            loop.quit()
        super().closeEvent(e)

    def paintEvent(self, _):
        paint_window_glass(self)

    def showEvent(self, e):
        super().showEvent(e)
        _prepare_window_surface(self)


class FramelessDialog(QDialog):
    """Modal dialog with custom title bar. Drop-in replacement for
    QDialog — call .exec() as usual. Subclasses build into
    self.content_layout. Never shows a native title bar."""

    def __init__(self, parent=None, *, title="LogicShare", min_size=(460, 400)):
        super().__init__(parent)
        self.setWindowFlags(
            Qt.WindowType.Dialog |
            Qt.WindowType.FramelessWindowHint
        )
        self.setAttribute(Qt.WA_TranslucentBackground, True)
        self.setWindowTitle(title)
        self.setWindowIcon(app_icon())
        self.setModal(True)
        self.setMinimumSize(*min_size)

        outer = QVBoxLayout(self)
        outer.setContentsMargins(0, 0, 0, 0); outer.setSpacing(0)

        self.title_bar = TitleBar(
            self, title=title, can_maximize=False, show_logo=True
        )
        outer.addWidget(self.title_bar)

        self._body = QWidget()
        self._body.setStyleSheet("background:transparent;")
        self.content_layout = QVBoxLayout(self._body)
        self.content_layout.setContentsMargins(0, 0, 0, 0)
        self.content_layout.setSpacing(0)
        outer.addWidget(self._body, stretch=1)

    def set_title(self, text):
        self.setWindowTitle(text)
        self.title_bar.set_title(text)

    def paintEvent(self, _):
        paint_window_glass(self)

    def showEvent(self, e):
        super().showEvent(e)
        _prepare_window_surface(self)


class CustomMessageBox(QDialog):
    """macOS-style alert — no title bar, centered icon + message, full-width
    bottom button separated by a thin divider."""

    def __init__(self, parent=None, *, icon="⚠️", title="Warning",
                 message="Something went wrong.", detail=""):
        super().__init__(parent)
        self.setWindowFlags(
            Qt.WindowType.Dialog |
            Qt.WindowType.FramelessWindowHint
        )
        self.setAttribute(Qt.WA_TranslucentBackground, True)
        self.setWindowTitle(title)
        self.setWindowIcon(app_icon())
        self.setModal(True)
        self.setMinimumSize(400, 220)
        self.resize(440, 280)

        root = QVBoxLayout(self)
        root.setContentsMargins(0, 0, 0, 0); root.setSpacing(0)

        # ---- body ----
        body = QWidget(); body.setStyleSheet("background:transparent;")
        v = QVBoxLayout(body)
        v.setContentsMargins(32, 28, 32, 20); v.setSpacing(10)

        ico = QLabel(icon)
        ico.setStyleSheet("font-size:44px; background:transparent; border:none;")
        ico.setAlignment(Qt.AlignmentFlag.AlignCenter)
        v.addWidget(ico)

        msg = QLabel(message)
        msg.setStyleSheet(f"color:{T.TEXT}; font-size:14px; font-weight:700; background:transparent; border:none;")
        msg.setAlignment(Qt.AlignmentFlag.AlignCenter)
        msg.setWordWrap(True)
        v.addWidget(msg)

        if detail:
            det = QLabel(detail)
            det.setStyleSheet(f"color:{T.TEXT_SEC}; font-size:12px; font-weight:500; background:transparent; border:none;")
            det.setAlignment(Qt.AlignmentFlag.AlignCenter)
            det.setWordWrap(True)
            v.addWidget(det)

        v.addStretch()
        root.addWidget(body, stretch=1)

        # ---- full-width solid accent button (matches file-share buttons) ----
        btn_wrap = QWidget(); btn_wrap.setStyleSheet("background:transparent;")
        bw = QVBoxLayout(btn_wrap)
        bw.setContentsMargins(24, 4, 24, 22); bw.setSpacing(0)
        ok = QPushButton("OK")
        ok.setCursor(Qt.CursorShape.PointingHandCursor)
        ok.setMinimumHeight(44)
        ok.setSizePolicy(QSizePolicy.Policy.Expanding, QSizePolicy.Policy.Fixed)
        ok.setStyleSheet(ACCENT_BTN_CSS)
        ok.clicked.connect(lambda: (self.accept(), self.close()))
        bw.addWidget(ok)
        root.addWidget(btn_wrap)

    def paintEvent(self, _):
        paint_window_glass(self)

    def showEvent(self, e):
        super().showEvent(e)
        _prepare_window_surface(self)

    @staticmethod
    def _show(parent, icon, title, message, detail):
        # deleteLater() after exec so the dialog (a child of `parent`) doesn't
        # linger in the parent's child list for the whole app lifetime — that
        # was a slow memory leak with repeated warnings over a long session.
        dlg = CustomMessageBox(parent, icon=icon, title=title,
                               message=message, detail=detail)
        dlg.exec()
        dlg.deleteLater()

    @staticmethod
    def warning(parent, title, message, detail=""):
        CustomMessageBox._show(parent, "⚠️", title, message, detail)

    @staticmethod
    def information(parent, title, message, detail=""):
        CustomMessageBox._show(parent, "ℹ️", title, message, detail)

    @staticmethod
    def critical(parent, title, message, detail=""):
        CustomMessageBox._show(parent, "❌", title, message, detail)


# ============================================================================
#  Announcement composer — COMPOSE-ONLY dialog
#
#  Per UX redesign: this no longer contains a board picker. The teacher
#  writes the announcement here, hits "Select Recipients", and the MAIN
#  WINDOW takes over for board selection (single source of truth — no
#  duplicate board list, no second scanner). On click we emit
#  proceed_to_select(title, message) and close.
# ============================================================================
class UploadProgressDialog(FramelessDialog):
    def __init__(self, parent, file_path, class_name, section, room):
        super().__init__(parent, title="Sending File", min_size=(480, 260))
        # Hide the close button — teacher can't cancel mid-transfer; the
        # "Done" button appears when it finishes.
        self.title_bar._btn_close.hide()
        self.setStyleSheet(f"""
            QLabel {{ color: {T.TEXT}; background: transparent; border: none; }}
            QProgressBar {{
                background: {T.FILL};
                border: 1px solid {T.STROKE};
                border-radius: 7px; height: 12px;
                text-align: center; color: transparent;
            }}
            QProgressBar::chunk {{
                background: qlineargradient(x1:0,y1:0,x2:1,y2:0,
                    stop:0 {T.ACCENT_LO}, stop:1 {T.ACCENT_HI});
                border-radius: 6px;
            }}
            {ACCENT_BTN_CSS}
        """)

        filename = os.path.basename(file_path)
        total = os.path.getsize(file_path)
        self._start_time = time.time()
        self._last_sent = 0
        self._last_time = self._start_time
        self._speed_str = ""

        _w = QWidget(); _w.setStyleSheet("background:transparent;")
        v = QVBoxLayout(_w)
        v.setContentsMargins(30, 20, 30, 22); v.setSpacing(12)

        target = QLabel(
            f"<span style='color:{T.TEXT_MUTED};'>Sending</span>  "
            f"<b style='color:{T.TEXT};'>{filename}</b>  "
            f"<span style='color:{T.TEXT_DIM};'>({format_size(total)})</span>"
        )
        target.setStyleSheet("font-size:13px;")
        target.setWordWrap(True)
        v.addWidget(target)

        dest = QLabel(
            f"<span style='color:{T.TEXT_MUTED};'>→</span>  "
            f"<b style='color:{T.TEXT};'>{format_class(class_name, section)}  ·  Room {room}</b>"
        )
        dest.setStyleSheet("font-size:12px;")
        v.addWidget(dest)

        v.addSpacing(4)
        self.bar = QProgressBar(); self.bar.setMinimum(0); self.bar.setMaximum(100)
        v.addWidget(self.bar)

        self.status = QLabel("Getting everything ready…💀")
        self.status.setStyleSheet(f"color:{T.TEXT_SEC}; font-size:12px; font-weight:600;")
        v.addWidget(self.status)

        v.addStretch()
        row = QHBoxLayout(); row.addStretch()
        self.close_btn = QPushButton("Done")
        self.close_btn.setEnabled(False); self.close_btn.setMinimumWidth(100)
        self.close_btn.setStyleSheet(ACCENT_BTN_CSS)
        self.close_btn.setCursor(Qt.CursorShape.PointingHandCursor)
        self.close_btn.clicked.connect(lambda: (self.accept(), self.close()))
        row.addWidget(self.close_btn)
        v.addLayout(row)
        self.content_layout.addWidget(_w)

    def keyPressEvent(self, e):
        # Block Escape (which QDialog maps to reject/close) while the transfer
        # is still running — the close button is only enabled on completion,
        # so Escape would otherwise abandon the dialog mid-upload.
        if e.key() == Qt.Key.Key_Escape and not self.close_btn.isEnabled():
            e.ignore()
            return
        super().keyPressEvent(e)

    def update_progress(self, sent, total):
        pct = int(sent / max(total, 1) * 100)
        self.bar.setValue(pct)

        # Calculate transfer speed (MB/s) over a rolling ~300ms window so the
        # number reflects the current burst rather than the lifetime average.
        # IMPORTANT: we PERSIST the last speed string (self._speed_str) and
        # only recompute it every 300ms. The progress callback fires far more
        # often than that, so if we blanked the speed between recomputes it
        # would flicker / vanish — which is the bug. Keeping the stored value
        # means the speed is always visible and just refreshes periodically.
        now = time.time()
        dt = now - self._last_time
        if dt >= 0.3:
            bytes_delta = sent - self._last_sent
            speed = bytes_delta / dt if dt > 0 else 0   # bytes/sec
            self._last_sent = sent
            self._last_time = now
            mbps = speed / (1024 * 1024)
            self._speed_str = f"{mbps:.1f} MB/s"

        parts = [
            f"<span style='color:{T.TEXT_MUTED};'>Sending your file…📡</span>",
            f"<b>{format_size(sent)}</b> / {format_size(total)}",
            f"<span style='color:{T.ACCENT};'>{pct}%</span>",
        ]
        if self._speed_str:
            parts.append(f"<span style='color:{T.SUCCESS};'>⚡ {self._speed_str}</span>")
        self.status.setText("&nbsp;&nbsp;·&nbsp;&nbsp;".join(parts))

    def mark_success(self, response):
        saved = response.get("saved_as", "")
        total = self.bar.maximum()   # progress bar max is 100; we use real bytes via self._last_sent
        # Calculate average speed across the whole transfer using the actual
        # bytes sent (self._last_sent) and the elapsed wall-clock time. This
        # is the true end-to-end average — not a rolling instantaneous rate.
        elapsed = max(time.time() - self._start_time, 0.001)
        avg_bps = self._last_sent / elapsed
        if avg_bps >= 1024 * 1024:
            avg_str = f"{avg_bps / (1024 * 1024):.1f} MB/s"
        else:
            avg_str = f"{avg_bps / 1024:.0f} KB/s"
        self.status.setText(
            f"<span style='color:{T.SUCCESS}; font-weight:700;'>✓  "
            f"AYY!! File is on the board" + (f" ({saved})" if saved else "") + " 😎🔥</span>"
            f"<br><span style='color:{T.TEXT_MUTED}; font-weight:600; font-size:11px;'>"
            f"Average speed: <b style='color:{T.ACCENT};'>{avg_str}</b>"
            f"  ·  Took {elapsed:.1f}s</span>"
        )
        self.close_btn.setText("Done!!😎"); self.close_btn.setEnabled(True)

    def mark_failure(self, msg):
        self.status.setText(f"<span style='color:{T.DANGER}; font-weight:700;'>✗  {msg}</span>")
        self.close_btn.setEnabled(True)


# ============================================================================
#  Announcement dialog
# ============================================================================
class AnnouncementDialog(FramelessDialog):
    def __init__(self, parent, profile, ip, class_name, section, room):
        super().__init__(parent, title="Send Announcement", min_size=(560, 600))
        self.profile = profile
        self.ip = ip
        self.class_name = class_name
        self.section = section
        self.room = room
        self._worker = None

        self.setStyleSheet(f"""
            QLabel {{ color: {T.TEXT}; background: transparent; border: none; }}
            {LINE_EDIT_CSS}
            {TEXT_AREA_CSS}
            {ACCENT_BTN_CSS}
            {SUBTLE_BTN_CSS}
        """)

        self._stack = QStackedWidget(self)
        self._stack.addWidget(self._build_form())
        self._stack.addWidget(self._build_status())

        self.content_layout.addWidget(self._stack)

    def _build_form(self):
        w = QWidget(); v = QVBoxLayout(w)
        v.setContentsMargins(30, 26, 30, 24); v.setSpacing(14)

        head = QHBoxLayout(); head.setSpacing(10)
        head_lbl = QLabel("📢   Send Announcement")
        head_lbl.setStyleSheet(f"color:{T.TEXT}; font-size:18px; font-weight:700;")
        head.addWidget(head_lbl); head.addStretch()
        v.addLayout(head)

        target = QLabel(
            f"<span style='color:{T.TEXT_MUTED};'>Going to</span> &nbsp;&nbsp; "
            f"<b style='color:{T.TEXT};'>{format_class(self.class_name, self.section)}  ·  Room {self.room}</b>"
        )
        target.setStyleSheet("font-size:13px;")
        v.addWidget(target)
        v.addWidget(hline())

        v.addWidget(field_label("TITLE"))
        self.title_in = QLineEdit()
        self.title_in.setPlaceholderText("e.g. Class Test Tomorrow")
        self.title_in.setMaxLength(80)
        v.addWidget(self.title_in)

        v.addWidget(field_label("MESSAGE"))
        self.msg_in = QPlainTextEdit()
        self.msg_in.setPlaceholderText("Write your announcement here…")
        self.msg_in.setMinimumHeight(110)
        v.addWidget(self.msg_in)

        v.addWidget(field_label("PREVIEW"))
        self._preview = self._build_preview()
        v.addWidget(self._preview)

        v.addStretch()

        btns = QHBoxLayout(); btns.addStretch()
        cancel = QPushButton("Cancel"); cancel.setStyleSheet(SUBTLE_BTN_CSS)
        cancel.setMinimumWidth(100); cancel.setMinimumHeight(40)
        cancel.setCursor(Qt.CursorShape.PointingHandCursor)
        cancel.clicked.connect(self.reject)
        self.send_btn = QPushButton("Send 🚀")
        self.send_btn.setMinimumWidth(130); self.send_btn.setMinimumHeight(40)
        self.send_btn.setEnabled(False)
        self.send_btn.setCursor(Qt.CursorShape.PointingHandCursor)
        self.send_btn.setStyleSheet(ACCENT_BTN_CSS)
        self.send_btn.clicked.connect(self._do_send)
        btns.addWidget(cancel); btns.addSpacing(8); btns.addWidget(self.send_btn)
        v.addLayout(btns)

        self.title_in.textChanged.connect(self._validate)
        self.msg_in.textChanged.connect(self._validate)
        self._validate()
        return w

    def _build_preview(self):
        card = QFrame()
        card.setObjectName("PreviewCard")
        card.setStyleSheet(f"""
            #PreviewCard {{
                background: {vgrad(*T.SURF_CARD)};
                border: 1px solid {T.STROKE_HI};
                border-radius: {T.R_LG}px;
            }}
        """)
        make_shadow(card, blur=18, y=6, alpha=110)

        row = QHBoxLayout(card)
        row.setContentsMargins(16, 14, 16, 14); row.setSpacing(14)

        short = self.profile.get("short_name") or derive_short_name(
            self.profile["first_name"], self.profile["surname"]
        )
        self._preview_avatar = AvatarCircle(short, size=52)
        row.addWidget(self._preview_avatar, alignment=Qt.AlignmentFlag.AlignTop)

        text_col = QVBoxLayout(); text_col.setSpacing(2)
        teacher = f"{self.profile['first_name']} {self.profile['surname']} {self.profile['title']}"
        teach_lbl = QLabel(teacher)
        teach_lbl.setStyleSheet(f"color:{T.TEXT_MUTED}; font-size:11px; font-weight:600;")
        text_col.addWidget(teach_lbl)

        self._preview_title = QLabel("Title goes here")
        self._preview_title.setStyleSheet(f"color:{T.TEXT}; font-size:15px; font-weight:800;")
        self._preview_title.setWordWrap(True)
        text_col.addWidget(self._preview_title)

        self._preview_msg = QLabel("Your message will appear here…")
        self._preview_msg.setStyleSheet(f"color:{T.TEXT_SEC}; font-size:12px; font-weight:500;")
        self._preview_msg.setWordWrap(True)
        text_col.addWidget(self._preview_msg)

        row.addLayout(text_col, stretch=1)
        return card

    def _validate(self):
        title = self.title_in.text().strip()
        msg   = self.msg_in.toPlainText().strip()
        self.send_btn.setEnabled(bool(title) and bool(msg))
        self._preview_title.setText(title or "Title goes here")
        preview_text = msg or "Your message will appear here…"
        if len(preview_text) > 140:
            preview_text = preview_text[:140] + "…"
        self._preview_msg.setText(preview_text)

    def _build_status(self):
        w = QWidget(); v = QVBoxLayout(w)
        v.setContentsMargins(30, 30, 30, 30); v.setSpacing(18)
        v.setAlignment(Qt.AlignmentFlag.AlignCenter)

        self._status_icon = QLabel("📡")
        self._status_icon.setStyleSheet("font-size:54px;")
        self._status_icon.setAlignment(Qt.AlignmentFlag.AlignCenter)
        v.addWidget(self._status_icon, alignment=Qt.AlignmentFlag.AlignCenter)

        self._status_title = QLabel("Sending it…📡")
        self._status_title.setStyleSheet(f"color:{T.TEXT}; font-size:18px; font-weight:700;")
        self._status_title.setAlignment(Qt.AlignmentFlag.AlignCenter)
        v.addWidget(self._status_title)

        self._status_sub = QLabel("Hold on a sec…😅")
        self._status_sub.setStyleSheet(f"color:{T.TEXT_MUTED}; font-size:12px; font-weight:500;")
        self._status_sub.setAlignment(Qt.AlignmentFlag.AlignCenter)
        self._status_sub.setWordWrap(True)
        v.addWidget(self._status_sub)

        self._status_btn = QPushButton("Close")
        self._status_btn.setMinimumWidth(120)
        self._status_btn.setEnabled(False)
        self._status_btn.setStyleSheet(ACCENT_BTN_CSS)
        self._status_btn.setCursor(Qt.CursorShape.PointingHandCursor)
        self._status_btn.clicked.connect(lambda: (self.accept(), self.close()))
        v.addWidget(self._status_btn, alignment=Qt.AlignmentFlag.AlignCenter)
        return w

    def _do_send(self):
        title = self.title_in.text().strip()
        msg   = self.msg_in.toPlainText().strip()
        if not title or not msg:
            return
        self._stack.setCurrentIndex(1)

        self._worker = AnnouncementWorker(
            url     = f"http://{self.ip}:16384/announce",
            title   = title,
            message = msg,
            profile = self.profile,
        )
        self._worker.success.connect(self._on_success)
        self._worker.failed.connect(self._on_failed)
        self._worker.start()

    def _on_success(self, _resp):
        self._status_icon.setText("🚀")
        self._status_title.setText("Let's go!! Sent!!😎🔥")
        self._status_title.setStyleSheet(f"color:{T.SUCCESS}; font-size:18px; font-weight:700;")
        self._status_sub.setText("It's now showing on the Smart Board ✨")
        self._status_btn.setText("Done!!😎")
        self._status_btn.setEnabled(True)
        # Auto-dismiss after 2.2s. Owned by `self` so Qt cancels it
        # automatically when the dialog is destroyed — without this the timer
        # kept firing on a freed C++ object (RuntimeError: Internal C++ object
        # (AnnouncementDialog) already deleted) if the user closed the dialog
        # manually within that 2.2s window.
        self._auto_close_timer = QTimer(self)
        self._auto_close_timer.setSingleShot(True)
        self._auto_close_timer.timeout.connect(self._safe_close)
        self._auto_close_timer.start(2200)

    def _safe_close(self):
        # Defensive: even with QTimer(self) parenting, on some shutdown paths
        # the C++ object can be gone before this fires. Guard with a try so
        # a stray timeout can never crash the app.
        try:
            self.accept()
            self.close()
        except RuntimeError:
            # Underlying QDialog already deleted — nothing to do.
            pass

    def _on_failed(self, msg):
        self._status_icon.setText("⚠️")
        self._status_title.setText("Couldn't Send…💀")
        self._status_title.setStyleSheet(f"color:{T.DANGER}; font-size:18px; font-weight:700;")
        self._status_sub.setText(msg)
        self._status_btn.setText("Close")
        self._status_btn.setEnabled(True)

    def closeEvent(self, e):
        # Cancel the auto-close timer if it's still queued — otherwise it can
        # fire against a freed C++ object after the dialog is manually closed.
        t = getattr(self, "_auto_close_timer", None)
        if t is not None:
            try: t.stop()
            except Exception: pass
        if self._worker and self._worker.isRunning():
            self._worker.wait(1000)
        super().closeEvent(e)


# ============================================================================
#  Teacher profile dialog
# ============================================================================
class TeacherProfileDialog(FramelessDialog):
    def __init__(self, parent=None, existing=None):
        _t = "Welcome to LogicShare" if existing is None else "Edit Profile"
        super().__init__(parent, title=_t, min_size=(480, 520))
        self.setStyleSheet(f"""
            QLabel {{ color: {T.TEXT}; background: transparent; border: none; }}
            {LINE_EDIT_CSS}
            {RADIO_CSS}
            {ACCENT_BTN_CSS}
        """)

        _w = QWidget(); _w.setStyleSheet("background:transparent;")
        v = QVBoxLayout(_w)
        v.setContentsMargins(32, 28, 32, 24); v.setSpacing(14)

        title_row = QHBoxLayout(); title_row.setSpacing(12); title_row.setContentsMargins(0,0,0,0)
        title_row.addWidget(logo_label(32), alignment=Qt.AlignmentFlag.AlignVCenter)
        title = QLabel("LogicShare" if existing is None else "Edit Profile")
        title.setStyleSheet(f"color:{T.TEXT}; font-size:22px; font-weight:700;")
        title_row.addWidget(title, alignment=Qt.AlignmentFlag.AlignVCenter)
        title_row.addStretch()
        v.addLayout(title_row)

        sub = QLabel("Quick setup — tell me who you are 👋"
                     if existing is None else
                     "Update your name, short name, or title.")
        sub.setStyleSheet(f"color:{T.TEXT_MUTED}; font-size:13px; font-weight:500;")
        v.addWidget(sub)
        v.addWidget(hline())

        v.addSpacing(2)
        v.addWidget(field_label("FIRST NAME"))
        self.first_in = QLineEdit(); self.first_in.setPlaceholderText("e.g. Priya")
        v.addWidget(self.first_in)

        v.addWidget(field_label("SURNAME"))
        self.sur_in = QLineEdit(); self.sur_in.setPlaceholderText("e.g. Sharma")
        v.addWidget(self.sur_in)

        v.addWidget(field_label("SHORT NAME  (your initials)"))
        short_row = QHBoxLayout(); short_row.setSpacing(12)
        self.short_in = QLineEdit()
        self.short_in.setPlaceholderText("e.g. PS")
        self.short_in.setMaxLength(4)
        short_row.addWidget(self.short_in, stretch=1)
        self._short_preview = AvatarCircle("?", size=48)
        short_row.addWidget(self._short_preview)
        v.addLayout(short_row)

        v.addWidget(field_label("TITLE"))
        title_row2 = QHBoxLayout(); title_row2.setSpacing(24)
        self.sir_rb  = QRadioButton("Sir")
        self.maam_rb = QRadioButton("Ma'am")
        self.grp = QButtonGroup(self)
        self.grp.addButton(self.sir_rb); self.grp.addButton(self.maam_rb)
        self.sir_rb.setChecked(True)
        title_row2.addWidget(self.sir_rb); title_row2.addWidget(self.maam_rb); title_row2.addStretch()
        v.addLayout(title_row2)
        v.addStretch()

        btn_row = QHBoxLayout(); btn_row.addStretch()
        self.save_btn = QPushButton("Let's go!! 😎" if existing is None else "Save")
        self.save_btn.setMinimumWidth(120)
        self.save_btn.setMinimumHeight(40)
        self.save_btn.setCursor(Qt.CursorShape.PointingHandCursor)
        self.save_btn.setStyleSheet(ACCENT_BTN_CSS)
        self.save_btn.clicked.connect(self._on_save)
        btn_row.addWidget(self.save_btn)
        v.addLayout(btn_row)

        if existing:
            self.first_in.setText(existing.get("first_name", ""))
            self.sur_in.setText(existing.get("surname", ""))
            self.short_in.setText(existing.get("short_name", "") or derive_short_name(
                existing.get("first_name",""), existing.get("surname","")))
            if existing.get("title") == "Ma'am": self.maam_rb.setChecked(True)
            else: self.sir_rb.setChecked(True)

        self._user_edited_short = bool(existing and existing.get("short_name"))
        self.first_in.textChanged.connect(self._validate)
        self.sur_in.textChanged.connect(self._validate)
        self.short_in.textEdited.connect(self._on_short_edited)
        self._validate()
        self.content_layout.addWidget(_w)

    def _on_short_edited(self, text):
        # Treat the user as "having a custom short name" ONLY while the
        # field actually has content. Clearing it resets the flag so the
        # auto-suggested initials kick back in once first/surname are
        # populated again.
        #
        # Bug it fixes: previously this flag latched to True forever the
        # moment the user touched the field — including when they cleared
        # it. After wiping all three fields and refilling first/surname,
        # the short_name stayed empty (no auto-suggest, because the flag
        # was True), which left Save permanently disabled until the user
        # also retyped the short name by hand.
        self._user_edited_short = bool(text.strip())
        self._refresh_preview()

    def _refresh_preview(self):
        short = self.short_in.text().strip().upper() or derive_short_name(
            self.first_in.text().strip(), self.sur_in.text().strip()
        ) or "?"
        self._short_preview.setInitials(short)

    def _validate(self):
        first = self.first_in.text().strip()
        surn  = self.sur_in.text().strip()
        suggested = derive_short_name(first, surn)
        if not self._user_edited_short:
            self.short_in.setText(suggested)
        self.save_btn.setEnabled(bool(first) and bool(surn) and bool(self.short_in.text().strip()))
        self._refresh_preview()

    def _on_save(self):
        first = self.first_in.text().strip()
        surn  = self.sur_in.text().strip()
        short = (self.short_in.text().strip().upper() or derive_short_name(first, surn))[:4]
        title = "Sir" if self.sir_rb.isChecked() else "Ma'am"
        save_profile(first, surn, short, title)
        self.accept()
        self.close()


# ============================================================================
#  Fluent popup menu
# ============================================================================
class _MenuItem(QFrame):
    clicked = Signal()
    def __init__(self, icon_text, label, parent=None):
        super().__init__(parent)
        self.setObjectName("MenuItem")
        self.setCursor(Qt.CursorShape.PointingHandCursor)
        self.setFixedHeight(42)
        self.setAttribute(Qt.WA_Hover, True)
        self.setStyleSheet(f"""
            #MenuItem {{
                background: transparent;
                border: 1px solid transparent;
                border-radius: 10px;
            }}
            #MenuItem:hover {{
                background: qlineargradient(x1:0,y1:0,x2:1,y2:0,
                    stop:0 {rgba(T.ACCENT, 72)},
                    stop:1 {rgba(T.ACCENT, 28)});
                border: 1px solid {rgba(T.ACCENT_HI, 110)};
            }}
        """)
        lay = QHBoxLayout(self)
        lay.setContentsMargins(14, 0, 18, 0); lay.setSpacing(14)
        ic = QLabel(icon_text); ic.setFixedWidth(22)
        ic.setStyleSheet("font-size:16px; background:transparent; border:none;")
        ic.setAlignment(Qt.AlignmentFlag.AlignCenter)
        lay.addWidget(ic)
        lb = QLabel(label)
        lb.setStyleSheet(f"color:{T.TEXT}; font-size:13px; font-weight:600;"
                         f"background:transparent; border:none;")
        lay.addWidget(lb, stretch=1)

    def mousePressEvent(self, e):
        if e.button() == Qt.MouseButton.LeftButton:
            self.clicked.emit()
        super().mousePressEvent(e)


class _MenuCaption(QLabel):
    def __init__(self, text, parent=None):
        super().__init__(text, parent)
        self.setStyleSheet(
            f"color:{T.TEXT_MUTED}; font-size:9.5px; font-weight:800;"
            f"letter-spacing:2px; background:transparent; border:none;"
            f"padding: 8px 18px 4px 18px;"
        )


class FluentPopup(QFrame):
    def __init__(self):
        super().__init__(None,
            Qt.WindowType.Popup |
            Qt.WindowType.FramelessWindowHint |
            Qt.WindowType.NoDropShadowWindowHint
        )
        self.setAttribute(Qt.WA_TranslucentBackground, True)
        self.setObjectName("FluentPopup")
        # No CSS background — paintEvent draws the whole surface (solid
        # rounded body + border). Keeping the stylesheet empty avoids Qt
        # double-painting underneath us.
        self.setStyleSheet("")

        self._v = QVBoxLayout(self)
        self._v.setContentsMargins(8, 8, 8, 10); self._v.setSpacing(2)

    def add_caption(self, text):
        self._v.addWidget(_MenuCaption(text))

    def add_separator(self):
        sep = QFrame(); sep.setFixedHeight(1)
        sep.setStyleSheet(f"background:{T.DIVIDER}; margin:6px 14px;")
        self._v.addWidget(sep)

    def add_item(self, icon, label, on_click):
        item = _MenuItem(icon, label)
        def _go():
            self.close()
            on_click()
        item.clicked.connect(_go)
        self._v.addWidget(item)
        return item

    def show_at(self, global_pos):
        self.adjustSize()

        # Slide-down show animation (unchanged)
        end_pos = global_pos
        start_pos = end_pos + QPoint(0, -8)
        self.move(start_pos); self.show()
        anim = QPropertyAnimation(self, b"pos", self)
        anim.setDuration(180); anim.setEasingCurve(T.EASE)
        anim.setStartValue(start_pos); anim.setEndValue(end_pos)
        anim.start(QPropertyAnimation.DeletionPolicy.DeleteWhenStopped)

    def paintEvent(self, _):
        p = QPainter(self)
        p.setRenderHint(QPainter.RenderHint.Antialiasing)

        # Build rounded-rect clip path — gives us anti-aliased corners
        # without needing the HWND itself to be rounded (no DWM dependency).
        r = QRectF(self.rect()).adjusted(0.5, 0.5, -0.5, -0.5)
        path = QPainterPath()
        path.addRoundedRect(r, T.R_WINDOW, T.R_WINDOW)

        p.save()
        p.setClipPath(path)

        # A solid surface one step above the window it opens over — the
        # popup used to blur a screenshot of whatever was behind it, which
        # made a menu over a white page a grey smear.
        body = QLinearGradient(0, 0, 0, self.height())
        body.setColorAt(0, _qcolor(T.LAYER_2))
        body.setColorAt(1, _qcolor(T.LAYER_1))
        p.fillRect(self.rect(), body)

        # Subtle top sheen: the lit-from-above cue every surface here has.
        sheen = QLinearGradient(0, 0, 0, self.height() * 0.4)
        sheen.setColorAt(0.0, QColor(255, 255, 255, 14))
        sheen.setColorAt(1.0, QColor(255, 255, 255, 0))
        p.fillRect(self.rect(), sheen)

        p.restore()

        # 4) Crisp 1-px rounded border on top (outside the clip so the
        #    stroke renders centered on the path, not clipped in half).
        pen = QPen(_qcolor(T.STROKE_HI))
        pen.setWidthF(1.0)
        p.setPen(pen)
        p.setBrush(Qt.BrushStyle.NoBrush)
        p.drawPath(path)


# ============================================================================
#  Board card
# ============================================================================
def format_class(class_name, section=""):
    """Format class display: 'Class 11-A' if section exists, 'Class 11' if not."""
    if section:
        return f"Class {class_name}-{section}"
    return f"Class {class_name}"


class BoardCard(QFrame):
    removed                     = Signal(str)
    send_file_requested         = Signal(str)
    send_announcement_requested = Signal(str)
    selection_toggled           = Signal(str, bool)   # ip, selected

    def __init__(self, class_name, section, room, ip):
        super().__init__()
        self.class_name = class_name
        self.section = section
        self.room = room
        self.ip = ip
        self._popup = None

        # selection-mode state
        self._select_mode = False
        self._selected = False
        self._check_scale = 0.0

        self.setObjectName("BoardCard")
        # IMPORTANT: do NOT set WA_TranslucentBackground on a child widget —
        # it makes Qt skip painting the stylesheet background entirely, which
        # is why the card looked "completely transparent with no borders".
        # WA_StyledBackground tells Qt to honour the QSS `background:` rule
        # (including rounded corners with anti-aliasing).
        self.setAttribute(Qt.WA_StyledBackground, True)
        self.setMinimumHeight(78)
        self.setSizePolicy(QSizePolicy.Policy.Expanding, QSizePolicy.Policy.Fixed)

        # Soft static drop shadow — gives the card lift without any
        # hover-driven animation (per design: cards must stay calm/stable).
        self.shadow = QGraphicsDropShadowEffect(self)
        self.shadow.setBlurRadius(26)
        self.shadow.setOffset(0, 8)
        self.shadow.setColor(QColor(0, 0, 0, 150))
        self.setGraphicsEffect(self.shadow)

        # 120 Hz checkmark scale animation (selection feedback only — this is
        # NOT a hover effect; it just animates the check badge in/out).
        self._check_anim = TimerAnim(self._set_check_scale, duration_ms=T.DUR_MED, parent=self)

        self._apply_style()

        # LogicShare Android's BoardCard, dimension for dimension: pulse dot,
        # a 50 px gradient tile, a one-line title over a monospace IP, and a
        # square "⋯" button.
        row = QHBoxLayout(self)
        row.setContentsMargins(16, 14, 10, 14); row.setSpacing(10)

        self._pulse = PulseDot(T.SUCCESS)
        row.addWidget(self._pulse, alignment=Qt.AlignmentFlag.AlignVCenter)

        icon = QLabel("🖥")
        icon.setFixedSize(50, 50)
        icon.setAlignment(Qt.AlignmentFlag.AlignCenter)
        icon.setStyleSheet(f"""
            background: qlineargradient(x1:0,y1:0,x2:1,y2:1,
                stop:0 {rgba(T.ACCENT_HI, 97)},
                stop:1 {rgba(T.ACCENT, 31)});
            border: 1px solid {T.STROKE_BRIGHT};
            border-radius: {T.R_CTRL + 2}px;
            font-size: 21px;
        """)
        row.addWidget(icon)
        row.addSpacing(4)

        txt = QVBoxLayout(); txt.setSpacing(3); txt.setContentsMargins(0,0,0,0)
        # ONE line, always, as on the phone: the title shrinks to fit rather
        # than wrapping, so every card in the list is the same height. One
        # space either side of the dot, not two — those extra spaces were
        # what pushed a long room name over the edge on Android.
        n = FitLabel(f"{format_class(class_name, section)} · Room {room}", color=T.TEXT,
                     weight=QFont.Weight.Bold, max_px=14.5, min_px=8.5)
        self._title = n
        ipl = QLabel(ip)
        ipl.setStyleSheet(
            f"color:{T.TEXT_MUTED}; font-size:11px; font-weight:600;"
            f"font-family:'Consolas','JetBrains Mono','Cascadia Mono',monospace;"
            f"background:transparent; border:none;"
        )
        txt.addWidget(n); txt.addWidget(ipl)
        row.addLayout(txt, stretch=1)

        # Three-dot menu button — hidden while in selection mode
        self._menu_btn = QPushButton("⋯")
        self._menu_btn.setFixedSize(T.CTRL_H, T.CTRL_H)
        self._menu_btn.setCursor(Qt.CursorShape.PointingHandCursor)
        self._menu_btn.setToolTip("Actions")
        self._menu_btn.setStyleSheet(f"""
            QPushButton {{
                background: {T.FILL};
                color: {T.TEXT};
                border: 1px solid {T.STROKE};
                border-radius: {T.R_CTRL}px;
                font-size: 20px; font-weight: 800;
                padding: 0 0 4px 0;
            }}
            QPushButton:hover {{
                background: {rgba(T.ACCENT_HI, 55)};
                border: 1px solid {T.STROKE_ACC};
                color: white;
            }}
            QPushButton:pressed {{
                background: {rgba(T.ACCENT, 90)};
            }}
        """)
        self._menu_btn.clicked.connect(self._open_menu)
        row.addWidget(self._menu_btn, alignment=Qt.AlignmentFlag.AlignVCenter)

        # Selection check badge holder (only shown in selection mode). Painted
        # in paintEvent; this reserves layout space on the right.
        self._check_holder = QWidget()
        self._check_holder.setFixedSize(30, 30)
        self._check_holder.setAttribute(Qt.WA_TransparentForMouseEvents)
        self._check_holder.hide()
        row.addWidget(self._check_holder, alignment=Qt.AlignmentFlag.AlignVCenter)

        grow_in(self, dur=T.DUR_SLOW)

    # ---- styling (no hover rule — calm + stable) ----
    def _apply_style(self):
        if self._select_mode and self._selected:
            # Selected: accent border + slightly brighter glass
            self.setStyleSheet(f"""
                #BoardCard {{
                    background: {vgrad(*T.SURF_CARD_SEL)};
                    border: 1.6px solid {T.STROKE_ACC};
                    border-top: 1.6px solid {rgba(shade(T.ACCENT_HI, 0.42), 170)};
                    border-radius: {T.R_CARD}px;
                }}
            """)
        elif self._select_mode:
            # Selectable but not selected — calm glass, subtle hover tint only
            self.setStyleSheet(f"""
                #BoardCard {{
                    background: {fill(T.SURF_TILE)};
                    border: 1px solid {T.STROKE};
                    border-top: 1px solid {T.STROKE_BRIGHT};
                    border-radius: {T.R_CARD}px;
                }}
                #BoardCard:hover {{
                    background: {fill(T.SURF_TILE_HOVER)};
                    border: 1px solid {rgba(T.ACCENT_HI, 120)};
                }}
            """)
        else:
            # Normal mode — translucent glass panel, gentle border-only hover
            self.setStyleSheet(f"""
                #BoardCard {{
                    background: {fill(T.SURF_TILE)};
                    border: 1.2px solid {T.STROKE_HI};
                    border-top: 1.2px solid {T.STROKE_BRIGHT};
                    border-radius: {T.R_CARD}px;
                }}
                #BoardCard:hover {{
                    background: {fill(T.SURF_TILE_HOVER)};
                    border: 1.2px solid {rgba(T.ACCENT_HI, 120)};
                }}
            """)

    # ---- selection mode API (driven by the main window) ----
    def set_select_mode(self, on):
        self._select_mode = on
        if not on:
            self._selected = False
            self._set_check_scale(0.0)
        self._menu_btn.setVisible(not on)
        self._check_holder.setVisible(on)
        self.setCursor(Qt.CursorShape.PointingHandCursor if on else Qt.CursorShape.ArrowCursor)
        self._apply_style()

    def set_selected(self, sel, *, animate=True, emit=True):
        if not self._select_mode or sel == self._selected:
            return
        self._selected = sel
        self._apply_style()
        if animate:
            self._check_anim.animate(self._check_scale, 1.0 if sel else 0.0)
        else:
            self._set_check_scale(1.0 if sel else 0.0)
        if emit:
            self.selection_toggled.emit(self.ip, sel)

    def is_selected(self):
        return self._selected

    def _set_check_scale(self, v):
        self._check_scale = max(0.0, min(1.0, v))
        self.update()

    # ---- input ----
    def mousePressEvent(self, e):
        # In selection mode a click anywhere toggles selection.
        if self._select_mode and e.button() == Qt.MouseButton.LeftButton:
            self.set_selected(not self._selected)
            e.accept()
            return
        super().mousePressEvent(e)

    def paintEvent(self, e):
        super().paintEvent(e)
        if not self._select_mode:
            return
        p = QPainter(self)
        p.setRenderHint(QPainter.RenderHint.Antialiasing)
        geo = self._check_holder.geometry()
        cx, cy = geo.center().x(), geo.center().y()
        k = geo.width() / 30.0                      # the holder is 30 design px
        # The empty ring, always there in selection mode, as on the phone —
        # without it an unticked card gave no hint it could be ticked.
        p.setBrush(Qt.BrushStyle.NoBrush)
        p.setPen(QPen(QColor(T.STROKE_BRIGHT), 1.5 * k))
        p.drawEllipse(QPointF(cx, cy), 12 * k, 12 * k)
        if self._check_scale <= 0.01:
            return
        # The animated check badge over it
        r = 13 * self._check_scale * k

        if self._selected:
            grad = QLinearGradient(cx - r, cy - r, cx + r, cy + r)
            grad.setColorAt(0, QColor(124, 208, 255))
            grad.setColorAt(1, QColor(37, 150, 227))
            p.setBrush(QBrush(grad))
            p.setPen(QPen(QColor(255, 255, 255, 180), 1.2))
            p.drawEllipse(QPointF(cx, cy), r, r)
            if self._check_scale > 0.45:
                tick = QPen(QColor("#ffffff")); tick.setWidthF(2.2)
                tick.setCapStyle(Qt.PenCapStyle.RoundCap)
                tick.setJoinStyle(Qt.PenJoinStyle.RoundJoin)
                p.setPen(tick)
                s = self._check_scale * k
                p.drawLine(QPointF(cx - 5*s, cy + 0.5*s), QPointF(cx - 1.5*s, cy + 4*s))
                p.drawLine(QPointF(cx - 1.5*s, cy + 4*s), QPointF(cx + 5*s, cy - 4*s))

    def _open_menu(self):
        if self._popup is not None and self._popup.isVisible():
            self._popup.close()
            self._popup = None
            return

        popup = FluentPopup()
        popup.setFixedWidth(220)
        popup.add_caption("ACTIONS")
        popup.add_item("📤", "Send File",         self._do_send_file)
        popup.add_item("📢", "Send Announcement", self._do_send_announcement)

        anchor = self._menu_btn.mapToGlobal(QPoint(self._menu_btn.width(), self._menu_btn.height() + 6))
        anchor -= QPoint(popup.sizeHint().width(), 0)
        popup.show_at(anchor)
        self._popup = popup

    def _do_send_file(self):
        self._menu_btn.setEnabled(False); self._menu_btn.setText("…")
        QApplication.processEvents()
        alive = is_host_reachable(self.ip)
        self._menu_btn.setText("⋯"); self._menu_btn.setEnabled(True)
        if not alive:
            CustomMessageBox.warning(
                self, "Board Offline 💀",
                f"Looks like {format_class(self.class_name, self.section)} (Room {self.room}) just dipped offline…",
                detail="Maybe the board lost Wi-Fi. Hit Refresh to look again 😎"
            )
            # Defer removal to the next event-loop tick. Emitting (which
            # triggers deleteLater on THIS card) while still executing inside
            # the card's own method is unsafe, and doing it right after a
            # modal dialog can race with rescan timers that fired during the
            # modal loop. QTimer.singleShot(0) lets the stack unwind first.
            ip = self.ip
            QTimer.singleShot(0, lambda: self._safe_emit_removed(ip))
            return
        self.send_file_requested.emit(self.ip)

    def _do_send_announcement(self):
        self._menu_btn.setEnabled(False); self._menu_btn.setText("…")
        QApplication.processEvents()
        alive = is_host_reachable(self.ip)
        self._menu_btn.setText("⋯"); self._menu_btn.setEnabled(True)
        if not alive:
            CustomMessageBox.warning(
                self, "Board Offline 💀",
                f"{format_class(self.class_name, self.section)} (Room {self.room}) is offline rn.",
                detail="Try Refresh to look again 😎"
            )
            ip = self.ip
            QTimer.singleShot(0, lambda: self._safe_emit_removed(ip))
            return
        self.send_announcement_requested.emit(self.ip)

    def _safe_emit_removed(self, ip):
        """Emit `removed` only if this card's C++ object is still alive.

        The deferred singleShot(0) can fire after a rescan has already
        deleteLater()'d this card (the modal warning pumps the event loop),
        in which case touching self raises RuntimeError from shiboken.
        Same use-after-free family as the announcement-dialog crash.
        """
        try:
            self.removed.emit(ip)
        except RuntimeError:
            pass  # C++ object already deleted — removal already happened


# ============================================================================
#  Scanning / NoBoard panels
# ============================================================================
class ScanningPanel(QFrame):
    """Scanning state — the new sci-fi radar lives in the middle, with the
    📡 antenna emoji pinned at its exact visual center."""
    def __init__(self):
        super().__init__()
        self.setStyleSheet("background:transparent; border:none;")
        self.setSizePolicy(QSizePolicy.Policy.Expanding, QSizePolicy.Policy.Expanding)
        v = QVBoxLayout(self)
        v.setContentsMargins(20, 24, 20, 24); v.setSpacing(14)
        v.setAlignment(Qt.AlignmentFlag.AlignCenter)

        self._radar = ScanningRadar()
        v.addWidget(self._radar, alignment=Qt.AlignmentFlag.AlignCenter)

        head = QLabel("Looking for Smart Boards…📡")
        head.setStyleSheet(f"color:{T.TEXT}; font-size:18px; font-weight:800;"
                           f"background:transparent; border:none;")
        head.setAlignment(Qt.AlignmentFlag.AlignCenter)
        v.addWidget(head)

        hint = QLabel("Make sure your board and laptop are on the same Wi-Fi 😎")
        hint.setStyleSheet(f"color:{T.TEXT_SEC}; font-size:12.5px; font-weight:600;"
                           f"background:transparent; border:none;")
        hint.setAlignment(Qt.AlignmentFlag.AlignCenter)
        hint.setWordWrap(True)
        v.addWidget(hint)


class OfflinePanel(QFrame):
    """No Wi-Fi, or discovery disabled — LogicShare Android's OfflinePanel.

    The list used to be simply empty under a red badge. An empty window
    reads as broken; this says what the badge says, and what to do."""
    def __init__(self, disabled=False):
        super().__init__()
        self.setStyleSheet("background:transparent; border:none;")
        self.setSizePolicy(QSizePolicy.Policy.Expanding, QSizePolicy.Policy.Expanding)
        v = QVBoxLayout(self)
        v.setContentsMargins(32, 40, 32, 32); v.setSpacing(8)
        v.addStretch(1)                 # stretches, not AlignCenter — see _no_match_panel
        ico = QLabel("⚠️" if disabled else "📶")
        ico.setStyleSheet("font-size:48px; background:transparent; border:none;")
        ico.setAlignment(Qt.AlignmentFlag.AlignCenter)
        v.addWidget(ico)
        v.addSpacing(6)
        head = QLabel("LogicShare is temporarily disabled" if disabled else "No Wi-Fi 💀")
        head.setStyleSheet(f"color:{T.TEXT}; font-size:18px; font-weight:800;"
                           f"background:transparent; border:none;")
        head.setAlignment(Qt.AlignmentFlag.AlignCenter); head.setWordWrap(True)
        v.addWidget(head)
        hint = QLabel("Device discovery could not start. Restart LogicShare to try again."
                      if disabled else
                      "Connect to the same Wi-Fi as the Smart Board — LogicShare will pick it up instantly 😎")
        hint.setStyleSheet(f"color:{T.TEXT_SEC}; font-size:12.5px; font-weight:600;"
                           f"background:transparent; border:none;")
        hint.setAlignment(Qt.AlignmentFlag.AlignCenter); hint.setWordWrap(True)
        v.addWidget(hint)
        v.addStretch(2)


class NoBoardPanel(QFrame):
    help_requested = Signal()

    def __init__(self):
        super().__init__()
        self.setObjectName("NoBoardPanel")
        self.setAttribute(Qt.WA_TranslucentBackground, True)
        self.setSizePolicy(QSizePolicy.Policy.Expanding, QSizePolicy.Policy.Fixed)
        self.setStyleSheet(f"""
            #NoBoardPanel {{
                background: {vgrad(*T.SURF_CARD)};
                border: 1px solid {T.STROKE};
                border-top: 1px solid {T.STROKE_BRIGHT};
                border-radius: {T.R_LG}px;
            }}
        """)
        make_shadow(self, blur=26, y=10, alpha=160)

        v = QVBoxLayout(self)
        v.setContentsMargins(24, 22, 24, 22); v.setSpacing(14)

        tr = QHBoxLayout(); tr.setSpacing(12)
        ti = QLabel("📡")
        ti.setStyleSheet("font-size:30px; background:transparent; border:none;")
        ti.setFixedWidth(38)
        tt_col = QVBoxLayout(); tt_col.setSpacing(2)
        tt = QLabel("No Smart Boards yet…💀")
        tt.setStyleSheet(f"color:{T.TEXT}; font-size:16px; font-weight:800; background:transparent; border:none;")
        ts = QLabel("Let's get things connected 😎")
        ts.setStyleSheet(f"color:{T.TEXT_MUTED}; font-size:12px; font-weight:500; background:transparent; border:none;")
        tt_col.addWidget(tt); tt_col.addWidget(ts)
        tr.addWidget(ti); tr.addLayout(tt_col); tr.addStretch()
        v.addLayout(tr)
        v.addWidget(hline())

        sub = QLabel("Quick checks:")
        sub.setStyleSheet(f"color:{T.TEXT_MUTED}; font-size:11px; font-weight:700;"
                          f"letter-spacing:0.4px; background:transparent; border:none;")
        v.addWidget(sub)

        for em, txt in [
            ("🔌", "LogicBoard is running on the Smart Board"),
            ("📶", "The Smart Board is on Wi-Fi"),
            ("💻", "Your laptop is on the same Wi-Fi"),
        ]:
            r = QHBoxLayout(); r.setSpacing(12)
            b = QLabel(em); b.setStyleSheet("font-size:15px; background:transparent; border:none;"); b.setFixedWidth(26)
            h = QLabel(txt); h.setStyleSheet(f"color:{T.TEXT_SEC}; font-size:12.5px; font-weight:600;"
                                              f"background:transparent; border:none;"); h.setWordWrap(True)
            r.addWidget(b); r.addWidget(h, stretch=1)
            v.addLayout(r)

        v.addSpacing(4)
        help_row = QHBoxLayout(); help_row.addStretch()
        help_btn = QPushButton("Need help?🛠")
        help_btn.setMinimumWidth(120)
        help_btn.setCursor(Qt.CursorShape.PointingHandCursor)
        help_btn.setStyleSheet(SUBTLE_BTN_CSS)
        help_btn.clicked.connect(self.help_requested.emit)
        help_row.addWidget(help_btn)
        v.addLayout(help_row)


# ============================================================================
#  About / Help dialogs
# ============================================================================
_DLG_CSS = f"""
    QDialog {{
        background: {T.LAYER_DLG}; color:{T.TEXT};
        font-family: {T.FONT_FAMILY};
    }}
    QLabel {{ color:{T.TEXT}; background:transparent; border:none; }}
    {ACCENT_BTN_CSS}
"""


class _SimpleDialog(QDialog):
    """Minimal frameless popup for About / Help — no title bar, just the
    body content with a 'Got it' close button."""
    def __init__(self, parent, title, body):
        super().__init__(parent)
        self.setWindowFlags(
            Qt.WindowType.Dialog |
            Qt.WindowType.FramelessWindowHint
        )
        self.setAttribute(Qt.WA_TranslucentBackground, True)
        self.setWindowTitle(title)
        self.setWindowIcon(app_icon())
        self.setModal(True)
        self.setMinimumWidth(440)
        self.setStyleSheet(_DLG_CSS)

        lay = QVBoxLayout(self)
        lay.setContentsMargins(28, 24, 28, 22); lay.setSpacing(16)
        lay.addWidget(body)
        btn_row = QHBoxLayout(); btn_row.addStretch()
        ok = QPushButton("Got it 😎")
        ok.setMinimumWidth(110)
        ok.setStyleSheet(ACCENT_BTN_CSS)
        ok.setCursor(Qt.CursorShape.PointingHandCursor)
        # Use both accept() AND close() to guarantee the dialog dismisses
        # regardless of how the event loop is running (exec vs show).
        def _dismiss():
            self.accept()
            self.close()
        ok.clicked.connect(_dismiss)
        btn_row.addWidget(ok)
        lay.addLayout(btn_row)

    def paintEvent(self, _):
        paint_window_glass(self)

    def showEvent(self, e):
        super().showEvent(e)
        _prepare_window_surface(self)


def _lbl(text, css="", wrap=False, w=None):
    l = QLabel(text)
    if css: l.setStyleSheet(css)
    if wrap: l.setWordWrap(True)
    if w is not None: l.setFixedWidth(w)
    return l


def show_about(parent):
    w = QWidget(); w.setStyleSheet("background:transparent;")
    v = QVBoxLayout(w); v.setContentsMargins(0,0,0,0); v.setSpacing(12)

    title_row = QHBoxLayout(); title_row.setSpacing(12); title_row.setContentsMargins(0,0,0,0)
    title_row.addWidget(logo_label(36), alignment=Qt.AlignmentFlag.AlignVCenter)
    tl = QLabel("LogicShare")
    tl.setStyleSheet(f"color:{T.TEXT}; font-size:22px; font-weight:700;")
    title_row.addWidget(tl, alignment=Qt.AlignmentFlag.AlignVCenter)
    title_row.addStretch()
    v.addLayout(title_row)

    for widget in [
        _lbl(f"Version {APP_VERSION}  •  Beta", f"color:{T.TEXT_DIM}; font-size:11px; font-weight:600;"),
        hline(),
        _lbl("Send files and announcements straight to the\n"
             "Smart Board in your classroom — no cables,\n"
             "no USB sticks. Just hit Send and go 😎🔥",
             f"color:{T.TEXT_SEC}; font-size:13px; font-weight:500;", wrap=True),
        _lbl("Free and open source  •  GPL-3.0", f"color:{T.TEXT_MUTED}; font-size:11px; font-weight:600;"),
    ]:
        v.addWidget(widget)
    _d = _SimpleDialog(parent, "About LogicShare", w); _d.exec(); _d.deleteLater()


def show_help(parent):
    w = QWidget(); w.setStyleSheet("background:transparent;")
    v = QVBoxLayout(w); v.setContentsMargins(0,0,0,0); v.setSpacing(14)
    v.addWidget(_lbl("🛠   Help & Tips", f"color:{T.TEXT}; font-size:16px; font-weight:700;"))
    v.addWidget(hline())
    for em, q, a in [
        ("📡", "Don't see your board?💀",
         "Make sure LogicBoard is open on the Smart Board and both devices are on the same Wi-Fi."),
        ("📄", "File won't send?💀",
         "Tap ⋮ → Send File, pick your file and confirm. Works with PDFs, Word, PowerPoint, images and videos."),
        ("📢", "How do I send an announcement?",
         "Tap ⋮ on a board card → Send Announcement. Type a title and message, then hit Send 🚀"),
        ("🔄", "Still stuck?😅",
         "Hit Refresh to look again. If nothing shows up, try restarting LogicBoard on the Smart Board."),
    ]:
        qr = QHBoxLayout(); qr.setSpacing(10)
        qr.addWidget(_lbl(em, "font-size:15px;", w=24))
        qr.addWidget(_lbl(q, f"color:{T.TEXT}; font-size:13px; font-weight:700;"))
        v.addLayout(qr)
        v.addWidget(_lbl(a, f"color:{T.TEXT_SEC}; font-size:12px; font-weight:500; padding-left:34px;", wrap=True))
    _d = _SimpleDialog(parent, "Help — LogicShare", w); _d.exec(); _d.deleteLater()


# ============================================================================
#  UPDATES
#
#  LogicShare updates by DOWNLOADING AND RUNNING ITS OWN INSTALLER, not by
#  unpacking files over itself. That is the whole design, and it is a
#  deliberate departure from the scheme LogicBoard uses.
#
#  A self-extracting updater has to reimplement, correctly, every single
#  thing the installer already does: close the running copy, lay files into
#  a versioned folder, repoint the Desktop and Start Menu shortcuts, rewrite
#  the Apps & Features entry so the version shown there is not a lie, update
#  App Paths, and delete the previous version. Each of those is a separate
#  chance for an updated install to end up subtly wrong — a shortcut aiming
#  at a folder that no longer exists, or Settings claiming 1.0.0 forever.
#  LogicShareInstaller.exe already does all of it, is already tested, and is
#  already the thing a fresh install goes through. Handing the job back to
#  it means the update path and the install path are the SAME code path, so
#  there is only one of them to get right.
#
#  So the flow is: ask GitHub for the newest release, compare tags, download
#  the .exe asset, launch it elevated, and quit. The installer takes it from
#  there — including terminating any LogicShare still running, which is why
#  this module does not have to.
# ============================================================================
# The release LIST, not /releases/latest: in a shared repo "latest" is
# whichever app shipped last. Drafts are never listed to anonymous callers.
_GITHUB_LATEST_TMPL = "https://api.github.com/repos/{repo}/releases?per_page=50"


def _select_release(data):
    """LogicShare's newest usable release from the shared repo's list.

    A single release object (the old /releases/latest shape) is accepted as
    a list of one. Prereleases are skipped, as /latest skipped them. Of the
    releases tagged ``LogicShare-`` that carry a Windows installer, the
    highest version wins whatever order GitHub listed them in. Returns
    ``(release, None)`` or ``(None, reason)``."""
    items = data if isinstance(data, list) else [data]
    best, best_v, ours = None, None, []
    for r in items:
        if not isinstance(r, dict) or r.get("draft") or r.get("prerelease"):
            continue
        tag = str(r.get("tag_name") or "")
        if isinstance(data, list) and not tag.lower().startswith(
                RELEASE_TAG_PREFIX.lower()):
            continue
        ours.append(r)
        v = _parse_version(tag)
        if v is None or _pick_installer_asset(r.get("assets")) is None:
            continue
        if best_v is None or v > best_v:
            best, best_v = r, v
    if best is not None:
        return best, None
    if not ours:
        return None, "No LogicShare release has been published yet."
    return ours[0], None          # let the caller say exactly what is wrong

# GitHub returns 403 to requests with no User-Agent. Not optional.
_UPDATE_UA          = f"LogicShare-Updater/{APP_VERSION}"
_UPDATE_TIMEOUT     = 20
_DOWNLOAD_CHUNK     = 256 * 1024


def updates_configured():
    """Whether this build can check for updates at all."""
    return bool(GITHUB_REPOSITORY.strip())


def _parse_version(text):
    """Turn a release tag into a comparable ``(major, minor, patch)`` tuple.

    Tags are published as ``vX.X.X``, but "published as" and "always exactly"
    are different claims, and a tag is typed by hand. This accepts anything
    that CONTAINS a dotted version and ignores the rest, so every one of
    these lands on (1, 2, 3):

        v1.2.3    V1.2.3    1.2.3    v1.2.3-beta.1    LogicShare v1.2.3
        v1.2.3+win    release-1.2.3     1.2.3   (with stray whitespace)

    A two-part ``v1.2`` becomes (1, 2, 0), since a missing patch is zero.

    Returns None when there is no version in the string at all — callers
    treat that as "cannot compare" and decline to offer an update, which is
    the safe direction to fail in: the worst case is a missed update the
    user can still install by hand, rather than a bogus one being pushed
    over a working install.
    """
    if not text:
        return None
    m = _re.search(r"(\d+)\.(\d+)(?:\.(\d+))?", str(text))
    if not m:
        return None
    try:
        return (int(m.group(1)), int(m.group(2)), int(m.group(3) or 0))
    except (TypeError, ValueError):
        return None


def _version_text(parsed):
    return ".".join(str(n) for n in parsed) if parsed else "?"


class UpdateInfo:
    """What the release endpoint told us about the newest published build."""

    def __init__(self, tag, version, url, asset_name, asset_size,
                 notes, release_url):
        self.tag         = tag           # raw tag, e.g. "v1.1.0"
        self.version     = version       # parsed tuple
        self.url         = url           # installer download URL
        self.asset_name  = asset_name
        self.asset_size  = asset_size    # bytes, 0 if GitHub didn't say
        self.notes       = notes
        self.release_url = release_url
        # Set once the installer for THIS release is on disk — either
        # fetched quietly before the offer went out, or left by an earlier
        # attempt. The dialog uses it to say "ready to install" rather than
        # "download and install", and to skip straight to launching.
        self.downloaded_path = None

    @property
    def version_text(self):
        return _version_text(self.version)

    def is_newer(self):
        current = _parse_version(APP_VERSION)
        if current is None or self.version is None:
            return False
        return self.version > current


def _pick_installer_asset(assets):
    """Choose the installer from a release's asset list.

    Ranked rather than "first .exe" because a release can carry more than one
    attachment — a portable build, a debug symbol bundle, checksums — and
    downloading the wrong one means launching something that is not an
    installer and cannot update anything. An installer-looking name wins over
    a bare .exe, and a .zip never wins at all: this updater runs what it
    downloads, so an archive is useless to it.
    """
    best, best_rank = None, -1
    for a in assets or []:
        name = str(a.get("name") or "")
        url  = a.get("browser_download_url")
        if not url or not name.lower().endswith(".exe"):
            continue
        low = name.lower()
        rank = 0
        if "installer" in low or "setup" in low:
            rank = 2
        elif "logicshare" in low:
            rank = 1
        if rank > best_rank:
            best, best_rank = a, rank
    return best


class UpdateCheckWorker(QThread):
    """Asks GitHub for the latest release. Emits ``done(UpdateInfo|None, err)``."""

    done = Signal(object, str)

    def run(self):
        if not updates_configured():
            self.done.emit(None, "Updates are turned off in this build.")
            return
        url = _GITHUB_LATEST_TMPL.format(repo=GITHUB_REPOSITORY.strip("/ "))
        try:
            r = requests.get(
                url,
                headers={"User-Agent": _UPDATE_UA,
                         "Accept": "application/vnd.github+json"},
                timeout=_UPDATE_TIMEOUT,
            )
        except requests.exceptions.RequestException as e:
            log.warning(f"Update check could not reach GitHub: {e}")
            self.done.emit(None, "Couldn't reach GitHub — check your internet "
                                 "connection and try again.")
            return

        if r.status_code == 404:
            self.done.emit(None, "No releases have been published yet.")
            return
        if r.status_code == 403:
            # Unauthenticated API calls are rate-limited per IP. On a school
            # network that is one shared IP for every machine in the building,
            # so this is a realistic outcome and deserves its own wording
            # rather than a generic HTTP error.
            self.done.emit(None, "GitHub is rate-limiting update checks right "
                                 "now. Try again in a little while.")
            return
        if r.status_code != 200:
            self.done.emit(None, f"GitHub responded with HTTP {r.status_code}.")
            return

        try:
            data = r.json()
        except ValueError:
            self.done.emit(None, "GitHub sent back something unreadable.")
            return
        data, why = _select_release(data)
        if data is None:
            self.done.emit(None, why)
            return

        tag = str(data.get("tag_name") or data.get("name") or "").strip()
        version = _parse_version(tag)
        if version is None:
            log.warning(f"Latest release tag is unparseable: {tag!r}")
            self.done.emit(None, f"The latest release is tagged “{tag}”, "
                                 "which isn't a version number LogicShare "
                                 "understands.")
            return

        asset = _pick_installer_asset(data.get("assets"))
        if asset is None:
            self.done.emit(None, "The latest release doesn't have an installer "
                                 "attached to it yet.")
            return

        self.done.emit(UpdateInfo(
            tag=tag,
            version=version,
            url=str(asset.get("browser_download_url")),
            asset_name=str(asset.get("name")),
            asset_size=int(asset.get("size") or 0),
            notes=str(data.get("body") or "").strip(),
            release_url=str(data.get("html_url") or ""),
        ), "")


class UpdateDownloadWorker(QThread):
    """Streams the installer to UPDATE_DIR. Emits progress, then ``done``.

    Skips the streaming entirely when the release's installer is already on
    disk — see ``existing_update_download``."""

    progress = Signal(int, int)      # received, total (total 0 = unknown)
    done     = Signal(str, str)      # saved path, error message

    def __init__(self, info, parent=None):
        super().__init__(parent)
        self._info = info
        self._cancelled = False

    def cancel(self):
        self._cancelled = True

    def run(self):
        info = self._info
        dest = None
        try:
            UPDATE_DIR.mkdir(parents=True, exist_ok=True)

            # Name the file after the version rather than after the asset, so
            # a release whose asset name has a space or a character Windows
            # dislikes can't produce an unlaunchable path.
            dest = update_installer_path(info)

            # Already downloaded and intact? Then there is nothing to fetch.
            #
            # The prune below used to run FIRST and delete every .exe in the
            # folder, including the one about to be re-downloaded — so an
            # installer that was already sitting on the disk was destroyed
            # and fetched again, every single time. That is ~100 MB of a
            # school's bandwidth for a file the machine already had.
            ready = existing_update_download(info)
            if ready is not None:
                _prune_updates_except(ready)
                self.progress.emit(1, 1)
                self.done.emit(str(ready), "")
                return

            # Clear out anything a previous attempt left behind — but never
            # the file we are about to write.
            _prune_updates_except(dest)

            with requests.get(info.url, stream=True,
                              headers={"User-Agent": _UPDATE_UA},
                              timeout=_UPDATE_TIMEOUT) as r:
                r.raise_for_status()
                total = int(r.headers.get("Content-Length") or info.asset_size or 0)
                received = 0
                with open(dest, "wb") as f:
                    for chunk in r.iter_content(chunk_size=_DOWNLOAD_CHUNK):
                        if self._cancelled:
                            raise _UpdateCancelled()
                        if not chunk:
                            continue
                        f.write(chunk)
                        received += len(chunk)
                        self.progress.emit(received, total)

            # A truncated download is a real failure mode on a flaky school
            # Wi-Fi, and a half-downloaded installer is a broken exe that
            # Windows will happily try to run. If the server told us how big
            # the file should be, hold it to that.
            actual = dest.stat().st_size
            if total and actual < total:
                raise IOError(
                    f"download stopped early ({format_size(actual)} of "
                    f"{format_size(total)})")
            if actual < 1024:
                raise IOError("the downloaded file is too small to be an installer")

            self.done.emit(str(dest), "")

        except _UpdateCancelled:
            self._discard(dest)
            self.done.emit("", "")          # cancelled: no error to report
        except requests.exceptions.RequestException as e:
            log.warning(f"Update download failed: {e}")
            self._discard(dest)
            self.done.emit("", "The download failed. Check your internet "
                               "connection and try again.")
        except Exception as e:
            log.exception("Update download failed")
            self._discard(dest)
            self.done.emit("", f"The download failed: {e}")

    @staticmethod
    def _discard(path):
        """Delete a partial download so it can never be mistaken for a good
        one, or launched by a later attempt that only checked for existence."""
        if not path:
            return
        try:
            Path(path).unlink(missing_ok=True)
        except OSError:
            pass


def update_installer_path(info) -> Path:
    """Where THIS release's installer lives once downloaded.

    Named by version, so the file on disk says which build it is and two
    releases can never be confused for one another.
    """
    return UPDATE_DIR / f"LogicShareInstaller_v{info.version_text}.exe"


def existing_update_download(info):
    """A previously-downloaded installer for this release, or None.

    Reused rather than re-fetched. The check is the size GitHub published,
    matched exactly, which catches the failure that actually happens: a
    download cut short by a flaky connection leaves a file that exists,
    looks plausible, and is a broken exe Windows will happily run.

    It is not a checksum — the release endpoint does not publish one — and
    a file that fails the size check is deleted rather than kept around to
    fool the next attempt.
    """
    try:
        dest = update_installer_path(info)
        if not dest.is_file():
            return None
        actual = dest.stat().st_size
        expected = int(getattr(info, "asset_size", 0) or 0)
        if expected:
            if actual != expected:
                log.info(f"Discarding {dest.name}: {format_size(actual)} on "
                         f"disk, release says {format_size(expected)}")
                try:
                    dest.unlink()
                except OSError:
                    pass
                return None
        elif actual < 1024:
            try:
                dest.unlink()
            except OSError:
                pass
            return None
        log.info(f"Reusing the installer already downloaded for "
                 f"{info.version_text} ({format_size(actual)})")
        return dest
    except OSError as e:
        log.debug(f"Could not check for an existing download: {e}")
        return None


def _prune_updates_except(keep) -> None:
    """Delete installers from earlier updates, keeping ``keep``.

    These are ~100 MB each and nothing ever reads an old one, so without
    this the folder grows by an installer per release, forever. A file
    still locked by a running installer simply refuses to delete, which is
    fine — the next attempt tries again.
    """
    try:
        for stale in UPDATE_DIR.glob("*.exe"):
            if keep is not None and stale == Path(keep):
                continue
            try:
                stale.unlink()
            except OSError:
                pass
    except OSError as e:
        log.debug(f"Could not prune old installer downloads: {e}")


def prune_installed_updates(current=None) -> list:
    """Delete downloaded installers this machine no longer needs.

    ``_prune_updates_except`` only ran when the NEXT update was downloaded,
    which left a gap exactly where it mattered: update to 1.1.1, and the
    70 MB ``LogicShareInstaller_v1.1.1.exe`` that did the job stayed in
    %LOCALAPPDATA%\\LogicShare\\Updates until some future release came along
    to replace it. On a board that skips a few releases, that is forever.

    So every launch sweeps the folder:

      - an installer for a version <= the one running is spent: it either
        installed this build or something older, and nothing reads it again;
      - an installer for a NEWER version is kept — the user downloaded it and
        chose "Later" at "install now?", and that file is what lets the next
        offer skip straight to installing;
      - anything whose name carries no version cannot be matched by
        ``update_installer_path`` and so can never be reused; it goes too.

    Returns the names deleted. A file still locked — the installer that just
    relaunched us may not have finished exiting — refuses to delete and is
    left for the retry; see the call in ``__main__``.
    """
    removed = []
    cur = _parse_version(current or APP_VERSION)
    if cur is None:
        return removed
    try:
        if not UPDATE_DIR.is_dir():
            return removed
        for f in UPDATE_DIR.glob("*.exe"):
            v = _parse_version(f.stem)
            if v is not None and v > cur:
                continue                      # a waiting update — keep it
            try:
                f.unlink()
                removed.append(f.name)
            except OSError as e:
                log.debug(f"Could not remove {f.name} yet: {e}")
    except OSError as e:
        log.debug(f"Could not sweep old installer downloads: {e}")
    if removed:
        log.info(f"Removed spent update installer(s): {', '.join(removed)}")
    return removed


class _UpdateCancelled(Exception):
    pass


def _launch_installer(path):
    """Start the downloaded installer, elevated. Returns (ok, message).

    ``ShellExecuteW`` with the ``runas`` verb, not ``subprocess``.
    LogicShareInstaller.exe carries a ``requireAdministrator`` manifest, and
    ``CreateProcess`` — which is what subprocess uses — refuses those
    outright with ERROR_ELEVATION_REQUIRED instead of showing a UAC prompt.
    Only the shell can raise the prompt.

    The call returns once the user has answered that prompt, so a decline is
    detectable and we can leave the app running instead of quitting into
    nothing.
    """
    if sys.platform != "win32":
        return False, "Automatic updates are only supported on Windows."
    try:
        SW_SHOWNORMAL = 1
        rc = ctypes.windll.shell32.ShellExecuteW(
            None, "runas", str(path), None, str(Path(path).parent), SW_SHOWNORMAL)
        # ShellExecuteW returns a fake HINSTANCE: anything > 32 is success,
        # and the values at or below it are error codes.
        rc = int(rc)
        if rc > 32:
            return True, ""
        if rc == 5:      # SE_ERR_ACCESSDENIED — the UAC prompt was declined
            return False, ("The update needs administrator permission to "
                           "install, and the request was declined.")
        return False, f"Windows couldn't start the installer (error {rc})."
    except Exception as e:
        log.exception("Could not launch the update installer")
        return False, f"Couldn't start the installer: {e}"


class UpdateDialog(FramelessDialog):
    """Ask -> download -> ask again -> hand off to the installer.

    TWO questions, on purpose:

      1. "A new version is available — download it?"  Nothing is fetched
         until the user says so. It used to download quietly first, which
         spent a school's bandwidth on a decision nobody had made.
      2. "Downloaded — install it now?"  Installing closes LogicShare, so
         it waits for a yes even after the download finished. "Later" keeps
         the installer (prune_installed_updates spares a version newer than
         the one running), so the next offer goes straight to question 2.

    One window for all of it rather than a chain of them, so the progress
    bar and the second question appear where the first one was asked.
    """

    def __init__(self, parent, info):
        super().__init__(parent, title="Update LogicShare", min_size=(500, 380))
        self._info = info
        self._worker = None
        self._downloading = False

        self.setStyleSheet(f"""
            QLabel {{ color: {T.TEXT}; background: transparent; border: none; }}
            QProgressBar {{
                background: {T.FILL};
                border: 1px solid {T.STROKE};
                border-radius: 7px; height: 12px;
                text-align: center; color: transparent;
            }}
            QProgressBar::chunk {{
                background: qlineargradient(x1:0,y1:0,x2:1,y2:0,
                    stop:0 {T.ACCENT_LO}, stop:1 {T.ACCENT_HI});
                border-radius: 6px;
            }}
            QPlainTextEdit {{
                background: {T.FILL_SUBTLE};
                border: 1px solid {T.STROKE};
                border-radius: {T.R_MD}px;
                color: {T.TEXT_SEC};
                font-size: 12px;
                padding: 8px;
            }}
        """)

        host = QWidget(); host.setStyleSheet("background:transparent;")
        v = QVBoxLayout(host)
        v.setContentsMargins(30, 20, 30, 22); v.setSpacing(12)

        head = QHBoxLayout(); head.setSpacing(12)
        head.addWidget(logo_label(36), alignment=Qt.AlignmentFlag.AlignVCenter)
        self.title = QLabel("")
        self.title.setStyleSheet(f"color:{T.TEXT}; font-size:18px; font-weight:700;")
        head.addWidget(self.title, alignment=Qt.AlignmentFlag.AlignVCenter)
        head.addStretch()
        v.addLayout(head)

        v.addWidget(QLabel(
            f"<span style='color:{T.TEXT_MUTED};'>You're on</span> "
            f"<b style='color:{T.TEXT};'>v{APP_VERSION}</b>"
            f"<span style='color:{T.TEXT_MUTED};'> &nbsp;→&nbsp; latest is</span> "
            f"<b style='color:{T.ACCENT};'>v{info.version_text}</b>"
        ))

        if info.notes:
            notes = QPlainTextEdit()
            notes.setPlainText(info.notes)
            notes.setReadOnly(True)
            notes.setMinimumHeight(120)
            v.addWidget(notes, stretch=1)
        else:
            v.addStretch()

        self.bar = QProgressBar()
        self.bar.setMinimum(0); self.bar.setMaximum(100)
        self.bar.setVisible(False)
        v.addWidget(self.bar)

        self.status = QLabel("")
        self.status.setWordWrap(True)
        self.status.setStyleSheet(f"color:{T.TEXT_SEC}; font-size:12px; font-weight:600;")
        v.addWidget(self.status)

        row = QHBoxLayout(); row.addStretch(); row.setSpacing(10)
        self.later_btn = QPushButton("Later")
        self.later_btn.setStyleSheet(SUBTLE_BTN_CSS)
        self.later_btn.setMinimumWidth(100)
        self.later_btn.setCursor(Qt.CursorShape.PointingHandCursor)
        self.later_btn.clicked.connect(self._on_later)
        row.addWidget(self.later_btn)

        self.go_btn = QPushButton("")
        self.go_btn.setStyleSheet(ACCENT_BTN_CSS)
        self.go_btn.setMinimumWidth(130)
        self.go_btn.setCursor(Qt.CursorShape.PointingHandCursor)
        self.go_btn.clicked.connect(self._on_go)
        row.addWidget(self.go_btn)
        v.addLayout(row)

        self.content_layout.addWidget(host)
        if getattr(info, "downloaded_path", None):
            self._show_ready()
        else:
            self._show_offer()

    # ---- phases -----------------------------------------------------------

    def _plain_status(self, text):
        self.status.setText(text)
        self.status.setStyleSheet(
            f"color:{T.TEXT_SEC}; font-size:12px; font-weight:600;")

    def _show_offer(self):
        """Question 1: download?"""
        self._ready = False
        self.title.setText("A new version is available 🚀")
        size = getattr(self._info, "asset_size", 0) or 0
        self._plain_status(
            "Download it now?"
            + (f" It's {format_size(size)}." if size else "")
            + " You'll be asked again before anything is installed.")
        self.go_btn.setText("Download")
        self.go_btn.setEnabled(True)
        self.later_btn.setText("Later")

    def _show_ready(self):
        """Question 2: install? (The download is done.)"""
        self._ready = True
        self.title.setText("Update downloaded ✅")
        self.bar.setVisible(False)
        self._plain_status(
            f"LogicShare v{self._info.version_text} is ready to install. "
            "Install it now? LogicShare will close while the installer "
            "runs. Choose Later to keep it for next time.")
        self.go_btn.setText("Install Now")
        self.go_btn.setEnabled(True)
        self.later_btn.setText("Later")

    def _on_go(self):
        if getattr(self, "_ready", False):
            self._install(self._info.downloaded_path)
        else:
            self._start_download()

    def _start_download(self):
        self._downloading = True
        self.go_btn.setEnabled(False)
        self.later_btn.setText("Cancel")
        self.bar.setVisible(True)
        self.bar.setValue(0)
        self._plain_status("Downloading the installer…")

        self._worker = UpdateDownloadWorker(self._info, self)
        self._worker.progress.connect(self._on_progress)
        self._worker.done.connect(self._on_downloaded)
        self._worker.start()

    def _on_progress(self, received, total):
        if total > 0:
            self.bar.setMaximum(100)
            self.bar.setValue(int(received * 100 / total))
            self.status.setText(
                f"Downloading… {format_size(received)} of {format_size(total)}")
        else:
            # No Content-Length: show a busy bar rather than a percentage
            # that would be invented.
            self.bar.setMaximum(0)
            self.status.setText(f"Downloading… {format_size(received)}")

    def _on_downloaded(self, path, err):
        self._downloading = False
        if err:
            self.bar.setVisible(False)
            self.later_btn.setText("Close")
            self.go_btn.setEnabled(True)
            self.go_btn.setText("Try Again")
            self.status.setText(err)
            self.status.setStyleSheet(
                f"color:{T.DANGER}; font-size:12px; font-weight:600;")
            return
        if not path:
            self.reject(); self.close()      # cancelled
            return

        # Downloaded — and now ASK. Installing closes LogicShare, which is
        # the user's call to make, not the download's.
        self._info.downloaded_path = path
        log.info(f"Update {self._info.version_text} downloaded; asking "
                 f"before installing")
        self._show_ready()

    def _install(self, path):
        self.go_btn.setEnabled(False)
        self._plain_status("Starting the installer…")
        ok, msg = _launch_installer(path)
        if not ok:
            self.later_btn.setText("Close")
            self.go_btn.setEnabled(True)
            self.go_btn.setText("Try Again")
            self.status.setText(msg)
            self.status.setStyleSheet(
                f"color:{T.DANGER}; font-size:12px; font-weight:600;")
            return

        # The installer is up and will close LogicShare itself, but quitting
        # now is cleaner than being terminated: it lets Qt tear windows down
        # in order and flush the log, and the user sees us step aside for the
        # installer instead of vanishing under it.
        log.info("Update installer launched — exiting for the handoff")
        self.accept(); self.close()
        QApplication.quit()

    def _on_later(self):
        if getattr(self, "_ready", False):
            log.info(f"Update {self._info.version_text} kept for later "
                     f"(not installed)")
        if self._downloading and self._worker is not None:
            self._worker.cancel()
            self.status.setText("Cancelling…")
            return
        self.reject(); self.close()

    def closeEvent(self, e):
        # Never leave a download thread running behind a closed window: it
        # would keep writing into UPDATE_DIR with nothing left to report to.
        if self._worker is not None and self._worker.isRunning():
            self._worker.cancel()
            self._worker.wait(3000)
        super().closeEvent(e)

    def keyPressEvent(self, e):
        if e.key() == Qt.Key.Key_Escape and self._downloading:
            e.ignore()
            return
        super().keyPressEvent(e)


# One live checker at a time. Without a module-level reference the QThread is
# garbage-collected the moment the calling function returns, which on PySide6
# destroys a running thread and takes the process with it.
_update_checker = None


def check_for_updates(parent, *, silent=False):
    """Kick off an update check.

    ``silent=True`` is the automatic check at startup: it shows the dialog
    only if there is genuinely a newer version, and says nothing at all
    otherwise. A background check that interrupts to announce that nothing
    happened is a check that teaches people to dismiss it unread.

    ``silent=False`` is the menu item, where the user asked a question and
    is owed an answer either way.
    """
    global _update_checker

    if not updates_configured():
        if not silent:
            CustomMessageBox.information(
                parent, "Updates",
                "This build isn't set up to check for updates.")
        return

    if _update_checker is not None and _update_checker.isRunning():
        if not silent:
            CustomMessageBox.information(
                parent, "Updates", "Already checking — hang on a sec 😅")
        return

    def _finished(info, err):
        if err:
            log.info(f"Update check: {err}")
            if not silent:
                CustomMessageBox.warning(parent, "Updates", err)
            return
        if info is None or not info.is_newer():
            log.info(f"Update check: up to date (v{APP_VERSION})")
            if not silent:
                CustomMessageBox.information(
                    parent, "Updates",
                    f"You're on the latest version 😎\n\nLogicShare v{APP_VERSION}")
            return
        log.info(f"Update available: v{info.version_text} (running v{APP_VERSION})")

        # Nothing is downloaded before the user agrees to it. If an installer
        # for this version is already on disk (they downloaded it earlier
        # and chose Later), the dialog opens on "install now?" instead.
        ready = existing_update_download(info)
        if ready is not None:
            info.downloaded_path = str(ready)
        dlg = UpdateDialog(parent, info)
        dlg.exec()
        dlg.deleteLater()

    _update_checker = UpdateCheckWorker()
    _update_checker.done.connect(_finished)
    _update_checker.start()
    if not silent:
        log.info("Checking for updates…")


# ============================================================================
#  Settings button + popup
# ============================================================================
def _pill_css(active):
    """Header pill: quiet glass at rest, the accent gradient when ON."""
    if active:
        return f"""
            QPushButton {{
                background: qlineargradient(x1:0,y1:0,x2:1,y2:0,
                    stop:0 {T.ACCENT_LO}, stop:1 {T.ACCENT_HI});
                color: white; border: 1px solid {rgba(T.ACCENT_HI, 160)};
                border-radius: 16px; font-size: 12px; font-weight: 800;
                padding: 0 14px;
            }}"""
    return f"""
        QPushButton {{
            background: {T.FILL}; color: {T.TEXT};
            border: 1px solid {T.STROKE}; border-radius: 16px;
            font-size: 12px; font-weight: 800; padding: 0 14px;
        }}
        QPushButton:hover {{ background: {T.FILL_HOVER}; border: 1px solid {T.STROKE_HI}; }}
        QPushButton:pressed {{ background: {T.FILL_SUBTLE}; }}"""


def pill_button(text):
    b = QPushButton(text)
    b.setCursor(Qt.CursorShape.PointingHandCursor)
    b.setFixedHeight(32)
    b.setStyleSheet(_pill_css(False))
    return b


def _numeric_sort(values):
    """Distinct non-empty values, numbers in numeric order."""
    vals = {v for v in values if str(v).strip()}
    return sorted(vals, key=lambda v: (int(v) if str(v).isdigit() else 10**9, str(v).lower()))


class SettingsButton(IconSquare):
    """The gear square at the right of the header, beside the status badge —
    where LogicShare Android keeps it."""
    edit_profile_requested = Signal()
    history_requested = Signal()

    def __init__(self, parent=None):
        super().__init__(_draw_gear_glyph, "Settings", parent=parent)
        self._popup = None
        self.clicked.connect(self._show_menu)

    def _show_menu(self):
        if self._popup is not None and self._popup.isVisible():
            self._popup.close(); self._popup = None
            return
        popup = FluentPopup()
        popup.setFixedWidth(240)
        popup.add_caption("PROFILE")
        popup.add_item("👤", "Edit Profile",     self.edit_profile_requested.emit)
        popup.add_separator()
        popup.add_caption("ANNOUNCEMENTS")
        popup.add_item("🕘", "History & Reports", self.history_requested.emit)
        popup.add_separator()
        popup.add_caption("ABOUT")
        popup.add_item("ℹ️", "About LogicShare", lambda: show_about(self.window()))
        popup.add_item("🛠", "Help & Tips",      lambda: show_help(self.window()))
        popup.add_item("🔄", "Check for Updates",
                       lambda: check_for_updates(self.window()))

        anchor = self.mapToGlobal(QPoint(self.width(), self.height() + 8))
        anchor -= QPoint(popup.sizeHint().width(), 0)
        popup.show_at(anchor)
        self._popup = popup


# ============================================================================
#  Custom title bar  +  frameless window base
#
#  Native title bars can't be styled to match, so every top-level LogicShare
#  window is frameless and draws its own bar. TitleBar provides drag-to-move,
#  double-click-maximize, and custom min / max / close buttons that match the
#  glass UI. FramelessWindow wires these to the host window and re-applies
#  rounds the window on show.
# ============================================================================
class AnnouncementComposer(FramelessWindow):
    proceed_to_select = Signal(str, str)   # title, message

    def __init__(self, profile, parent=None, tracker=None):
        super().__init__(title="Create Announcement", can_maximize=False,
                         min_size=(520, 560), parent=parent)
        self.profile = profile
        # Passed in, never assumed. See the note on _open_history.
        self._ack_tracker = tracker
        self.resize(560, 640)

        body = QWidget(); body.setStyleSheet("background:transparent;")
        v = QVBoxLayout(body); v.setContentsMargins(28, 22, 28, 22); v.setSpacing(13)

        head = QLabel("📢   Create Announcement")
        head.setStyleSheet(f"color:{T.TEXT}; font-size:18px; font-weight:800;")
        v.addWidget(head)
        sub = QLabel("Write it once — you'll pick which boards get it next 😎")
        sub.setStyleSheet(f"color:{T.TEXT_MUTED}; font-size:12px; font-weight:500;")
        v.addWidget(sub)
        v.addWidget(hline())

        # Teacher info chip (auto-populated name + short name)
        info = QFrame(); info.setStyleSheet(f"""
            QFrame {{ background: {T.FILL_SUBTLE};
                border:1px solid {T.STROKE}; border-radius:{T.R_MD}px; }}
        """)
        ir = QHBoxLayout(info); ir.setContentsMargins(12, 10, 14, 10); ir.setSpacing(12)
        short = self.profile.get("short_name") or derive_short_name(
            self.profile["first_name"], self.profile["surname"])
        ir.addWidget(AvatarCircle(short, size=40), alignment=Qt.AlignmentFlag.AlignVCenter)
        who = QVBoxLayout(); who.setSpacing(1)
        nm = QLabel(f"{self.profile['first_name']} {self.profile['surname']} {self.profile['title']}")
        nm.setStyleSheet(f"color:{T.TEXT}; font-size:13px; font-weight:700; background:transparent; border:none;")
        sn = QLabel(f"Short name: {short}")
        sn.setStyleSheet(f"color:{T.TEXT_MUTED}; font-size:11px; font-weight:600; background:transparent; border:none;")
        who.addWidget(nm); who.addWidget(sn)
        ir.addLayout(who); ir.addStretch()
        v.addWidget(info)

        v.addWidget(field_label("TITLE"))
        self.title_in = QLineEdit()
        self.title_in.setPlaceholderText("e.g. Exam Schedule")
        self.title_in.setMaxLength(80)
        self.title_in.setStyleSheet(LINE_EDIT_CSS)
        v.addWidget(self.title_in)

        v.addWidget(field_label("MESSAGE"))
        self.msg_in = QPlainTextEdit()
        self.msg_in.setPlaceholderText("Type your announcement here…\nLine breaks and paragraphs are supported.")
        self.msg_in.setStyleSheet(TEXT_AREA_CSS)
        self.msg_in.setMinimumHeight(120)
        v.addWidget(self.msg_in, stretch=1)

        v.addWidget(field_label("PREVIEW"))
        self._preview = self._build_preview_card()
        v.addWidget(self._preview)

        # footer
        foot = QHBoxLayout()
        hist_btn = QPushButton("🕘  History")
        hist_btn.setStyleSheet(SUBTLE_BTN_CSS)
        hist_btn.setMinimumWidth(110)
        hist_btn.setCursor(Qt.CursorShape.PointingHandCursor)
        hist_btn.clicked.connect(self._open_history)
        foot.addWidget(hist_btn)
        foot.addStretch()
        self.to_select_btn = QPushButton("Select Recipients  →")
        self.to_select_btn.setStyleSheet(ACCENT_BTN_CSS)
        self.to_select_btn.setMinimumWidth(180)
        self.to_select_btn.setEnabled(False)
        self.to_select_btn.setCursor(Qt.CursorShape.PointingHandCursor)
        self.to_select_btn.clicked.connect(self._go_select)
        foot.addWidget(self.to_select_btn)
        v.addLayout(foot)

        self.content_layout.addWidget(body)

        self.title_in.textChanged.connect(self._on_changed)
        self.msg_in.textChanged.connect(self._on_changed)

        self._on_changed()

    def _build_preview_card(self):
        card = QFrame(); card.setObjectName("PrevCard")
        card.setStyleSheet(f"""
            #PrevCard {{
                background: {vgrad(*T.SURF_CARD)};
                border: 1px solid {T.STROKE_HI};
                border-radius: {T.R_LG}px;
            }}
        """)
        make_shadow(card, blur=16, y=5, alpha=110)
        row = QHBoxLayout(card); row.setContentsMargins(16, 14, 16, 14); row.setSpacing(14)

        short = self.profile.get("short_name") or derive_short_name(
            self.profile["first_name"], self.profile["surname"])
        self._prev_avatar = AvatarCircle(short, size=50)
        row.addWidget(self._prev_avatar, alignment=Qt.AlignmentFlag.AlignTop)

        col = QVBoxLayout(); col.setSpacing(2)
        teacher = f"{self.profile['first_name']} {self.profile['surname']} {self.profile['title']}"
        tl = QLabel(teacher)
        tl.setStyleSheet(f"color:{T.TEXT_MUTED}; font-size:11px; font-weight:600;")
        col.addWidget(tl)
        self._prev_title = QLabel("Title goes here")
        self._prev_title.setStyleSheet(f"color:{T.TEXT}; font-size:15px; font-weight:800;")
        self._prev_title.setWordWrap(True)
        col.addWidget(self._prev_title)
        self._prev_msg = QLabel("Your message will appear here…")
        self._prev_msg.setStyleSheet(f"color:{T.TEXT_SEC}; font-size:12px; font-weight:500;")
        self._prev_msg.setWordWrap(True)
        col.addWidget(self._prev_msg)
        row.addLayout(col, stretch=1)
        return card

    def _on_changed(self):
        title = self.title_in.text().strip()
        msg = self.msg_in.toPlainText().strip()
        self._prev_title.setText(title or "Title goes here")
        txt = msg or "Your message will appear here…"
        if len(txt) > 160: txt = txt[:160] + "…"
        self._prev_msg.setText(txt)
        self.to_select_btn.setEnabled(bool(title) and bool(msg))

    def _go_select(self):
        title = self.title_in.text().strip()
        msg = self.msg_in.toPlainText().strip()
        if not title or not msg:
            return
        # Hand off to the main window for recipient selection, then close.
        self.proceed_to_select.emit(title, msg)
        self.close()

    def _open_history(self):
        dlg = AnnouncementHistoryDialog(
            self.profile, self,
            # getattr, not attribute access: this window is also constructed
            # from the share flow and from tests, and a missing tracker must
            # degrade the report rather than break the button.
            tracker=getattr(self, "_ack_tracker", None))
        dlg.exec()
        dlg.deleteLater()



# ============================================================================
#  Announcement history dialog — view / report / delete
# ============================================================================
class AnnouncementHistoryDialog(FramelessWindow):
    def __init__(self, profile, parent=None, tracker=None):
        super().__init__(title="Announcement History", can_maximize=False,
                         min_size=(520, 560), parent=parent)
        self.profile = profile
        # Optional so the dialog can still be constructed without the
        # acknowledgment layer; it then renders exactly the old history.
        self._tracker = tracker
        self.resize(560, 640)

        body = QWidget(); body.setStyleSheet("background:transparent;")
        v = QVBoxLayout(body); v.setContentsMargins(26, 22, 26, 22); v.setSpacing(12)

        top = QHBoxLayout()
        head = QLabel("🕘   Recent Announcements")
        head.setStyleSheet(f"color:{T.TEXT}; font-size:17px; font-weight:800;")
        top.addWidget(head); top.addStretch()
        self.clear_btn = QPushButton("Clear All")
        self.clear_btn.setStyleSheet(SUBTLE_BTN_CSS); self.clear_btn.setMinimumWidth(100)
        self.clear_btn.setCursor(Qt.CursorShape.PointingHandCursor)
        self.clear_btn.clicked.connect(self._clear_all)
        top.addWidget(self.clear_btn)
        v.addLayout(top)
        v.addWidget(hline())

        # Search + filter.
        self._hq = ""
        self._h_status = "all"
        self._h_period = "all"
        srow = QHBoxLayout(); srow.setSpacing(8)
        self._h_search = QLineEdit()
        self._h_search.setPlaceholderText("Search titles, messages, classes — try “exam 11a”")
        self._h_search.setClearButtonEnabled(True)
        self._h_search.setMaxLength(80)
        self._h_search.setStyleSheet(LINE_EDIT_CSS)
        self._h_search.textChanged.connect(self._on_h_query)
        srow.addWidget(self._h_search, stretch=1)
        self._h_filter_btn = pill_button("⚡  Filter")
        self._h_filter_btn.setFixedHeight(38)
        self._h_filter_btn.clicked.connect(self._show_h_filter)
        srow.addWidget(self._h_filter_btn)
        v.addLayout(srow)
        self._h_showing = caption_label("")
        self._h_showing.setStyleSheet(self._h_showing.styleSheet()
                                      .replace(T.TEXT_MUTED, T.ACCENT_HI))
        v.addWidget(self._h_showing)

        self._scroll = QScrollArea(); self._scroll.setWidgetResizable(True)
        self._scroll.setFrameShape(QFrame.Shape.NoFrame)
        self._scroll.setAttribute(Qt.WA_TranslucentBackground, True)
        self._scroll.viewport().setAutoFillBackground(False)
        self._scroll.setStyleSheet(SCROLLBAR_CSS)
        self._host = QWidget(); self._host.setStyleSheet("background:transparent;")
        self._list = QVBoxLayout(self._host)
        self._list.setContentsMargins(2, 4, 2, 4); self._list.setSpacing(10)
        self._scroll.setWidget(self._host)
        v.addWidget(self._scroll, stretch=1)

        self.content_layout.addWidget(body)
        # Repaint when an acknowledgment lands so the summary chips stay
        # honest while the window is open.
        if tracker is not None:
            try:
                # A BOUND METHOD, never a lambda.
                #
                # Qt disconnects a slot automatically when its receiver QObject
                # is destroyed — but only if it can SEE a receiver. A lambda
                # has none, so the connection outlives this dialog and fires
                # into a deleted C++ object: "Internal C++ object
                # (QVBoxLayout) already deleted". Binding the method gives Qt
                # the receiver it needs, and closeEvent disconnects explicitly
                # as well rather than relying on destruction order.
                tracker.status_changed.connect(self._on_tracker_changed)
            except Exception:
                log.debug("Could not connect tracker to history", exc_info=True)
        self._reload()

    def _on_tracker_changed(self, _announcement_id):
        """An acknowledgment landed — refresh the list."""
        self._reload()

    def closeEvent(self, e):
        # Belt and braces alongside Qt's automatic disconnect: an explicit
        # disconnect means a signal emitted between close() and destruction
        # cannot reach a half-torn-down widget.
        try:
            if self._tracker is not None:
                self._tracker.status_changed.disconnect(self._on_tracker_changed)
        except (RuntimeError, TypeError):
            pass          # already disconnected, or never connected
        super().closeEvent(e)

    def _on_h_query(self, text):
        self._hq = text
        self._reload()

    def _set_h_filter(self, status=None, period=None):
        if status is not None: self._h_status = status
        if period is not None: self._h_period = period
        self._reload()

    def _show_h_filter(self):
        popup = FluentPopup()
        popup.setFixedWidth(250)
        popup.add_caption("STATUS")
        for key, label in HISTORY_STATUSES:
            popup.add_item("✓" if self._h_status == key else "", label,
                           lambda key=key: self._set_h_filter(status=key))
        popup.add_separator()
        popup.add_caption("WHEN")
        for key, label in HISTORY_PERIODS:
            popup.add_item("✓" if self._h_period == key else "", label,
                           lambda key=key: self._set_h_filter(period=key))
        popup.show_at(self._h_filter_btn.mapToGlobal(QPoint(0, self._h_filter_btn.height() + 8)))
        self._h_popup = popup

    def _shown_entries(self, entries):
        q = smart_search.parse(getattr(self, "_hq", ""))
        status = getattr(self, "_h_status", "all")
        period = getattr(self, "_h_period", "all")
        tracker = getattr(self, "_tracker", None)
        out = []
        for e in entries:
            aid = e.get("announcement_id", "")
            ann = tracker.get(aid) if (tracker is not None and aid) else None
            if not history_status_matches(ann, status):
                continue
            if not history_period_matches(e.get("sent_at", 0), period):
                continue
            recips = [(str(r.get("class", "")), str(r.get("section", "")), str(r.get("room", "")))
                      for r in e.get("recipients", []) if isinstance(r, dict)]
            if smart_search.match_history(q, e.get("title", ""), e.get("message", ""), recips):
                out.append(e)
        return out

    def _reload(self):
        # clear
        while self._list.count():
            it = self._list.takeAt(0)
            wd = it.widget()
            if wd: wd.setParent(None); wd.deleteLater()

        entries = load_history()
        shown = self._shown_entries(entries)
        filtering = (getattr(self, "_h_status", "all") != "all"
                     or getattr(self, "_h_period", "all") != "all")
        try:
            self._h_filter_btn.setStyleSheet(_pill_css(filtering))
            self._h_filter_btn.setText("⚡  Filter •" if filtering else "⚡  Filter")
            self._h_showing.setText(f"SHOWING {len(shown)} OF {len(entries)}"
                                    if entries and (filtering or self._hq.strip()) else "")
        except (AttributeError, RuntimeError):
            pass
        if not entries:
            empty = QLabel("No announcements yet.\nSend one and it'll show up here 😎")
            empty.setStyleSheet(f"color:{T.TEXT_DIM}; font-size:12.5px; font-weight:500; padding:40px;")
            empty.setAlignment(Qt.AlignmentFlag.AlignCenter)
            empty.setWordWrap(True)
            self._list.addWidget(empty)
        elif not shown:
            empty = QLabel("Nothing matches.\nTry fewer words, or clear the filters.")
            empty.setStyleSheet(f"color:{T.TEXT_DIM}; font-size:12.5px; font-weight:500; padding:40px;")
            empty.setAlignment(Qt.AlignmentFlag.AlignCenter)
            empty.setWordWrap(True)
            self._list.addWidget(empty)
        else:
            for entry in shown:
                self._list.addWidget(self._make_card(entry))
        self._list.addItem(QSpacerItem(20, 30, QSizePolicy.Policy.Minimum, QSizePolicy.Policy.Expanding))

    def _make_card(self, entry):
        card = QFrame(); card.setObjectName("HistCard")
        card.setStyleSheet(f"""
            #HistCard {{
                background: {vgrad(*T.SURF_CARD)};
                border: 1px solid {T.STROKE};
                border-top: 1px solid {T.STROKE_BRIGHT};
                border-radius: {T.R_LG}px;
            }}
            QLabel {{ background:transparent; border:none; }}
        """)
        make_shadow(card, blur=18, y=6, alpha=140)
        v = QVBoxLayout(card); v.setContentsMargins(16, 13, 16, 13); v.setSpacing(7)

        title = QLabel(entry.get("title", "—"))
        title.setStyleSheet(f"color:{T.TEXT}; font-size:14px; font-weight:800;")
        title.setWordWrap(True)
        v.addWidget(title)

        msg = entry.get("message", "")
        if len(msg) > 110: msg = msg[:110] + "…"
        mlbl = QLabel(msg)
        mlbl.setStyleSheet(f"color:{T.TEXT_SEC}; font-size:11.5px; font-weight:500;")
        mlbl.setWordWrap(True)
        v.addWidget(mlbl)

        recips = entry.get("recipients", [])
        n = len(recips)
        meta = QLabel(f"📡 {n} board{'s' if n != 1 else ''}   ·   {time_ago_short(entry.get('sent_at', 0))}")
        meta.setStyleSheet(f"color:{T.TEXT_MUTED}; font-size:10.5px; font-weight:600;")
        v.addWidget(meta)

        # Acknowledgment summary, when this row has a tracked announcement.
        # Rendered as one chip per state rather than a sentence: the teacher
        # is scanning a list, and "3 Read / 2 Pending" reads faster in colour
        # than in prose.
        tracker = getattr(self, "_tracker", None)
        aid = entry.get("announcement_id", "")
        ann = tracker.get(aid) if (tracker is not None and aid) else None
        if ann is not None and ann.boards:
            counts = ann.counts()
            chips = QHBoxLayout(); chips.setSpacing(6)
            for state in (BoardState.READ, BoardState.DELIVERED,
                          BoardState.PENDING, BoardState.TIMEOUT,
                          BoardState.EXPIRED, BoardState.OFFLINE):
                c = counts.get(state, 0)
                if not c:
                    continue
                colour, bg, label = ack_state_style(state)
                chip = QLabel(f"{c}  {label}")
                chip.setStyleSheet(
                    f"color:{colour}; background:{bg}; border:1px solid {colour};"
                    f" border-radius:9px; padding:3px 9px;"
                    " font-size:10.5px; font-weight:800;")
                chips.addWidget(chip)
            chips.addStretch()
            v.addLayout(chips)

        # Action buttons.
        #
        # ONE action, not three. Resend and Duplicate both existed to get an
        # old announcement back into the composer — Duplicate did not even do
        # that, it just added a "(copy)" row to a list of things already sent,
        # which is a record of something that never happened. Neither answered
        # the question a teacher actually opens history with, which is "did it
        # land?". The report answers exactly that, so it gets the accent
        # button and the width, and the row stops offering two ways to do the
        # thing the composer's own text box does better.
        btns = QHBoxLayout(); btns.setSpacing(8)
        delete = QPushButton("🗑")
        delete.setStyleSheet(f"""
            QPushButton {{
                background: {T.DANGER_BG}; color:{T.DANGER};
                border:1px solid {T.DANGER_BD}; border-radius:{T.R_MD}px;
                font-size:14px; font-weight:700; padding:8px 12px;
            }}
            QPushButton:hover {{ background: {T.DANGER_BG_HI}; }}
        """)
        delete.setCursor(Qt.CursorShape.PointingHandCursor)
        delete.setToolTip("Delete from history")
        delete.clicked.connect(lambda: self._delete(entry))

        report = QPushButton("📋   Show Full Report")
        report.setStyleSheet(ACCENT_BTN_CSS); report.setMinimumWidth(190)
        report.setCursor(Qt.CursorShape.PointingHandCursor)
        report.clicked.connect(lambda _=0, a=aid: self.open_report(a))
        # Offered on every row, including the ones with nothing to report, and
        # explained on CLICK rather than greyed out: this runs on touch
        # screens, where a disabled button has no tooltip to explain itself
        # with and simply reads as broken.
        btns.addWidget(report)
        btns.addStretch(); btns.addWidget(delete)
        v.addLayout(btns)
        return card

    def open_report(self, announcement_id):
        """Open the board-by-board report for one announcement.

        Announcements sent before the acknowledgment layer existed have no
        ``announcement_id``, and a cleared tracking file leaves rows whose id
        no longer resolves. Both are ordinary states for an old history row to
        be in, so they get an explanation rather than a button that does
        nothing when pressed.
        """
        tracker = getattr(self, "_tracker", None)
        ann = (tracker.get(announcement_id)
               if (tracker is not None and announcement_id) else None)
        if ann is None:
            CustomMessageBox.information(
                self, "No report for this one",
                "LogicShare has no delivery record for this announcement.",
                detail=("It was sent before acknowledgment tracking existed, "
                        "or its record has since been cleared. Everything you "
                        "send from now on gets a full board-by-board report."))
            return
        dlg = AnnouncementReportDialog(tracker, announcement_id, parent=self)
        dlg.show()
        dlg.raise_()
        dlg.activateWindow()
        # Held so Qt does not collect it the moment this method returns.
        self._open_reports = getattr(self, "_open_reports", [])
        self._open_reports.append(dlg)
        dlg.destroyed.connect(
            lambda *_: self._open_reports.remove(dlg)
            if dlg in self._open_reports else None)

    def _delete(self, entry):
        delete_history_entry(entry.get("id", ""))
        self._reload()

    def _clear_all(self):
        clear_history()
        self._reload()


# History filters — the same choices and the same meaning as LogicShare
# Android's HistoryStatus / HistoryPeriod.
HISTORY_STATUSES = (("all", "All"), ("awaiting", "Awaiting replies"),
                    ("all_read", "Read by everyone"),
                    ("problems", "No response / offline"))
HISTORY_PERIODS = (("all", "All time"), ("today", "Today"), ("week", "Last 7 days"))


def history_status_matches(ann, status):
    """``ann`` is the tracker's Announcement for a row, or None for a row
    that predates tracking — which only ever appears under "All"."""
    if status == "all":
        return True
    if ann is None:
        return False
    states = [b.state for b in ann.boards.values()]
    if status == "awaiting":
        return any(st in (BoardState.PENDING, BoardState.DELIVERED) for st in states)
    if status == "all_read":
        return ann.all_read()
    if status == "problems":
        return any(st in (BoardState.TIMEOUT, BoardState.EXPIRED, BoardState.OFFLINE)
                   for st in states)
    return True


def history_period_matches(ts, period, now=None):
    import datetime as _dt
    now = time.time() if now is None else now
    if period == "week":
        return (ts or 0) >= now - 7 * 86400
    if period == "today":
        return (_dt.datetime.fromtimestamp(ts or 0).date()
                == _dt.datetime.fromtimestamp(now).date())
    return True


def time_ago_short(ts):
    """Compact 'time ago' for history rows."""
    if not ts:
        return "just now"
    delta = max(0, time.time() - ts)
    if delta < 60:    return "just now"
    if delta < 3600:  return f"{int(delta/60)}m ago"
    if delta < 86400: return f"{int(delta/3600)}h ago"
    days = int(delta / 86400)
    if days == 1:     return "yesterday"
    if days < 7:      return f"{days}d ago"
    import datetime as _dt
    return _dt.datetime.fromtimestamp(ts).strftime("%d %b")


# ============================================================================
#  Announcement acknowledgment — teacher-facing report
#
#  Colour is the whole point of this screen. A teacher glancing at it during a
#  lesson needs to know, without reading, which rooms are still outstanding —
#  so state maps to colour first and words second, and the palette reuses the
#  tokens the rest of the app already uses rather than inventing a new one.
# ============================================================================
ACK_STATE_STYLE = {
    #  state            colour        background            label
    # Each chip's background is now the _BG token of the same colour as its
    # text. The Read chip in particular was drawing #6ccf80 text on a
    # rgba(108,203,95) background — two different greens, the second of
    # which is LogicBoard's success colour that had wandered across.
    BoardState.READ:      (T.SUCCESS,     T.SUCCESS_BG, "Read"),
    BoardState.DELIVERED: (T.WARNING,     T.WARNING_BG, "Pending"),
    BoardState.PENDING:   (T.WARNING,     T.WARNING_BG, "Sending…"),
    BoardState.TIMEOUT:   (T.DANGER,      T.DANGER_BG,  "No response"),
    BoardState.EXPIRED:   (T.DANGER,      T.DANGER_BG,  "Expired"),
    BoardState.OFFLINE:   (T.TEXT_MUTED,  T.FILL,       "Offline"),
}


def ack_state_style(state):
    """(colour, background, label) for a board state. Never raises."""
    return ACK_STATE_STYLE.get(state, (T.TEXT_MUTED,
                                       T.FILL, state or "—"))


class AckStatusChip(QFrame):
    """One board's acknowledgment state, as a coloured pill."""

    def __init__(self, board, parent=None):
        super().__init__(parent)
        colour, bg, label = ack_state_style(board.state)
        self.setStyleSheet(f"""
            QFrame {{
                background: {bg};
                border: 1px solid {colour};
                border-radius: 9px;
            }}
            QLabel {{ background: transparent; border: none; }}
        """)
        row = QHBoxLayout(self)
        row.setContentsMargins(9, 3, 9, 3)
        row.setSpacing(6)
        dot = QLabel("●")
        dot.setStyleSheet(f"color:{colour}; font-size:10px;")
        row.addWidget(dot)
        txt = QLabel(label)
        txt.setStyleSheet(f"color:{colour}; font-size:11px; font-weight:800;")
        row.addWidget(txt)


class AckBoardRow(QFrame):
    """A board and its state within one announcement."""

    def __init__(self, board, parent=None):
        super().__init__(parent)
        # All three fragments are f-strings. The first one was NOT, which is
        # why its braces are single: concatenating a plain fragment onto
        # f-string ones is legal Python and reads as one string, so a token
        # reference dropped into the plain half comes out as the literal
        # text "{T.FILL_SUBTLE}" and Qt silently discards the whole rule.
        self.setStyleSheet(
            f"QFrame {{ background: {T.FILL_SUBTLE}; border: none;"
            f" border-radius: {T.R_MD}px; }}"
            " QLabel { background: transparent; border: none; }")
        row = QHBoxLayout(self)
        row.setContentsMargins(12, 8, 12, 8)
        row.setSpacing(10)

        left = QVBoxLayout(); left.setSpacing(2)
        name = QLabel(board.label)
        name.setStyleSheet(f"color:{T.TEXT}; font-size:12.5px; font-weight:700;")
        left.addWidget(name)

        # A timestamp for whichever lifecycle event this board last reached.
        # Showing the moment something happened is far more useful in a
        # report than showing the state alone — "Read 14:03" answers the
        # follow-up question before it is asked.
        stamp = ""
        if board.state == BoardState.READ and board.read_at:
            stamp = f"Read at {_ack_clock(board.read_at)}"
        elif board.state in (BoardState.TIMEOUT, BoardState.EXPIRED) and board.delivered_at:
            stamp = f"Delivered {_ack_clock(board.delivered_at)} · no reply"
        elif board.delivered_at:
            stamp = f"Delivered {_ack_clock(board.delivered_at)}"
        elif board.detail:
            stamp = board.detail
        if stamp:
            sub = QLabel(stamp)
            sub.setStyleSheet(f"color:{T.TEXT_MUTED}; font-size:10.5px; font-weight:600;")
            left.addWidget(sub)
        row.addLayout(left, stretch=1)
        row.addWidget(AckStatusChip(board), alignment=Qt.AlignmentFlag.AlignVCenter)


def _ack_clock(ts):
    try:
        import datetime as _dt
        return _dt.datetime.fromtimestamp(ts).strftime("%H:%M")
    except Exception:
        return ""


class AnnouncementReportDialog(FramelessWindow):
    """Board-by-board acknowledgment report for ONE announcement.

    Opened from the history list, and from the five-minute notification. In
    the notification case the boards that have NOT acknowledged are pulled to
    the top under their own heading — the teacher clicked because something
    is outstanding, so showing a scrollable list they have to hunt through
    would waste the one interaction they gave us.
    """

    def __init__(self, tracker, announcement_id, parent=None):
        super().__init__(title="Announcement Report", can_maximize=False,
                         min_size=(520, 560), parent=parent)
        self._tracker = tracker
        self._announcement_id = announcement_id
        self.resize(600, 720)

        body = QWidget(); body.setStyleSheet("background:transparent;")
        self._v = QVBoxLayout(body)
        self._v.setContentsMargins(26, 20, 26, 22)
        self._v.setSpacing(12)

        self._scroll = QScrollArea(); self._scroll.setWidgetResizable(True)
        self._scroll.setFrameShape(QFrame.Shape.NoFrame)
        self._scroll.setAttribute(Qt.WA_TranslucentBackground, True)
        self._scroll.viewport().setAutoFillBackground(False)
        self._scroll.setStyleSheet(SCROLLBAR_CSS)
        self._host = QWidget(); self._host.setStyleSheet("background:transparent;")
        self._list = QVBoxLayout(self._host)
        self._list.setContentsMargins(2, 2, 2, 2); self._list.setSpacing(9)
        self._scroll.setWidget(self._host)
        self._v.addWidget(self._scroll, stretch=1)

        self.content_layout.addWidget(body)

        # Live updates. The teacher never refreshes this by hand — an
        # acknowledgment landing while the report is open repaints the row
        # that changed.
        try:
            tracker.status_changed.connect(self._on_changed)
        except Exception:
            log.debug("Could not connect tracker signal", exc_info=True)
        self._rebuild()

    def _on_changed(self, announcement_id):
        if announcement_id == self._announcement_id:
            self._rebuild()

    def _clear(self):
        while self._list.count():
            it = self._list.takeAt(0)
            wd = it.widget()
            if wd:
                wd.setParent(None); wd.deleteLater()

    def _section(self, text, colour):
        lbl = QLabel(text)
        lbl.setStyleSheet(f"color:{colour}; font-size:11px; font-weight:800; "
                          "letter-spacing:1.2px;")
        return lbl

    def _rebuild(self):
        self._clear()
        ann = self._tracker.get(self._announcement_id)
        if ann is None:
            miss = QLabel("This announcement is no longer in your history.")
            miss.setStyleSheet(f"color:{T.TEXT_DIM}; font-size:12.5px; padding:40px;")
            miss.setAlignment(Qt.AlignmentFlag.AlignCenter)
            miss.setWordWrap(True)
            self._list.addWidget(miss)
            return

        self.set_title(ann.title or "Announcement Report")

        head = QLabel(ann.title or "—")
        head.setStyleSheet(f"color:{T.TEXT}; font-size:16px; font-weight:800;")
        head.setWordWrap(True)
        self._list.addWidget(head)

        msg = QLabel(ann.message or "")
        msg.setStyleSheet(f"color:{T.TEXT_SEC}; font-size:12px; font-weight:500;")
        msg.setWordWrap(True)
        self._list.addWidget(msg)

        counts = ann.counts()
        read_n = counts.get(BoardState.READ, 0)
        total = len(ann.boards)
        summary = QLabel(f"{read_n} of {total} board{'s' if total != 1 else ''} "
                         f"acknowledged   ·   sent {time_ago_short(ann.sent_at)}")
        summary.setStyleSheet(f"color:{T.TEXT_MUTED}; font-size:11px; font-weight:700;")
        self._list.addWidget(summary)
        self._list.addWidget(hline())

        outstanding = ann.unacknowledged()
        if outstanding:
            self._list.addWidget(self._section(
                f"NO ACKNOWLEDGMENT FROM {len(outstanding)} BOARD"
                f"{'S' if len(outstanding) != 1 else ''}", T.DANGER))
            note = QLabel(
                "These boards have not confirmed yet. They may be offline, or "
                "the class may not be in the room right now — it does not mean "
                "the announcement was never received.")
            note.setStyleSheet(f"color:{T.TEXT_MUTED}; font-size:10.5px; font-weight:500;")
            note.setWordWrap(True)
            self._list.addWidget(note)
            for b in outstanding:
                self._list.addWidget(AckBoardRow(b))
            self._list.addWidget(hline())

        self._list.addWidget(self._section("ALL BOARDS", T.TEXT_MUTED))
        for b in sorted(ann.boards.values(),
                        key=lambda x: (BoardState.SEVERITY.get(x.state, 9), x.label)):
            self._list.addWidget(AckBoardRow(b))

        self._list.addItem(QSpacerItem(20, 24, QSizePolicy.Policy.Minimum,
                                       QSizePolicy.Policy.Expanding))


class AckNotifier(QObject):
    """The tray presence: timeout notifications, and the way back to the app.

    QSystemTrayIcon.showMessage is the platform-appropriate route for the
    warning on Windows: it produces a real Windows notification, it needs no
    extra dependency, and — the part that matters most — messageClicked tells
    us the user acted on it, so the notification can open the right report.

    The announcement id is remembered per notification rather than "the most
    recent", because a teacher who sends three announcements in a lesson must
    not click the notification for one and be shown another.

    The same icon is also what makes closing the window survivable. A
    five-minute warning is worthless if the app is gone four minutes in, so
    the window closes to here instead of quitting, and this is where the two
    ways back out live: open the window again, or actually quit.
    """

    open_report = Signal(str)      # announcement_id
    show_window = Signal()
    quit_requested = Signal()

    def __init__(self, parent=None):
        super().__init__(parent)
        self._tray = None
        self._menu = None
        self._pending_id = ""
        # Which balloon is currently on screen decides what clicking it does.
        # Only ONE messageClicked exists for every message this icon shows, so
        # without this the "LogicShare is still running" notice would open the
        # report from whatever warning happened to fire before it.
        self._click_opens_report = False
        try:
            from PySide6.QtWidgets import QSystemTrayIcon, QMenu
            if QSystemTrayIcon.isSystemTrayAvailable():
                self._tray = QSystemTrayIcon(app_icon(), self)
                self._tray.setToolTip("LogicShare — watching for replies")
                self._tray.messageClicked.connect(self._on_clicked)
                self._tray.activated.connect(self._on_activated)
                # Held on self: a QMenu that only the tray icon references is
                # collected the moment this constructor returns, and the
                # right-click then produces nothing at all.
                self._menu = QMenu()
                self._menu.addAction("Open LogicShare",
                                     self.show_window.emit)
                self._menu.addSeparator()
                self._menu.addAction("Quit LogicShare",
                                     self.quit_requested.emit)
                self._tray.setContextMenu(self._menu)
                self._tray.show()
                log.info("Tray notifier ready")
            else:
                log.info("No system tray available — timeout alerts will be "
                         "logged only")
        except Exception:
            log.exception("Could not create tray notifier")

    @property
    def available(self):
        """Whether there is a tray icon to minimise into.

        Without one, closing the window has to mean quitting: hiding into a
        tray that isn't there would leave a process the user can see no way
        back to, which is worse than losing the warning.
        """
        return self._tray is not None

    def _on_activated(self, reason):
        from PySide6.QtWidgets import QSystemTrayIcon
        # Left click or double click — the standard Windows "give me the
        # window back". The context menu handles right click on its own.
        if reason in (QSystemTrayIcon.ActivationReason.Trigger,
                      QSystemTrayIcon.ActivationReason.DoubleClick):
            self.show_window.emit()

    def went_to_background(self):
        """The window has hidden into the tray.

        NOTHING IS SHOWN. This used to pop a balloon saying "LogicShare is
        still running", once per session, the first time the window was
        closed. It was removed on request, and the reason it is worth
        removing is that it interrupts to report an absence of news: the
        app did what the user asked, the tray icon is right there, and a
        notification whose content is "nothing has gone wrong" is the kind
        that teaches people to dismiss notifications unread — including the
        one that matters, which is a board failing to acknowledge.

        The state reset stays, and it is the part that was doing real work:
        the tray icon's click has two meanings, and after an ordinary
        close it must mean "give me the window back" rather than "open the
        report from whatever timeout fired earlier".
        """
        self._click_opens_report = False

    def _on_clicked(self):
        if self._click_opens_report and self._pending_id:
            log.info(f"Timeout notification clicked -> opening report "
                     f"{self._pending_id[:8]}")
            self.open_report.emit(self._pending_id)
        else:
            self.show_window.emit()

    def notify_timeout(self, announcement_id, title, boards):
        """Tell the teacher that boards have not acknowledged."""
        names = [b.label for b in boards]
        shown = names[:4]
        more = len(names) - len(shown)
        body = "No acknowledgment from:\n• " + "\n• ".join(shown)
        if more > 0:
            body += f"\n…and {more} more"
        self._pending_id = announcement_id
        self._click_opens_report = True
        log.warning(f"Notifying teacher: {len(names)} board(s) have not "
                    f"acknowledged {title[:40]!r}")
        if self._tray is None:
            return
        try:
            from PySide6.QtWidgets import QSystemTrayIcon
            self._tray.showMessage(
                f"No reply yet — {title[:48]}", body,
                QSystemTrayIcon.MessageIcon.Warning, 15000)
        except Exception:
            log.exception("Could not show tray notification")


# ============================================================================
#  Broadcast progress dialog — shows live multi-board send progress
# ============================================================================
class BroadcastProgressDialog(FramelessDialog):
    def __init__(self, parent, title, total):
        super().__init__(parent, title="Sending Announcement", min_size=(480, 420))
        self._total = total
        self.setStyleSheet(f"""
            QLabel {{ color: {T.TEXT}; background: transparent; border: none; }}
            {ACCENT_BTN_CSS}
            {SCROLLBAR_CSS}
        """)

        _w = QWidget(); _w.setStyleSheet("background:transparent;")
        v = QVBoxLayout(_w)
        v.setContentsMargins(28, 24, 28, 22); v.setSpacing(14)

        self._icon = QLabel("🚀")
        self._icon.setStyleSheet("font-size:42px;")
        self._icon.setAlignment(Qt.AlignmentFlag.AlignCenter)
        v.addWidget(self._icon)

        self._title = QLabel("Sending Announcement")
        self._title.setStyleSheet(f"color:{T.TEXT}; font-size:17px; font-weight:800;")
        self._title.setAlignment(Qt.AlignmentFlag.AlignCenter)
        v.addWidget(self._title)

        head = QLabel(f"“{title}”")
        head.setStyleSheet(f"color:{T.TEXT_MUTED}; font-size:12.5px; font-weight:600;")
        head.setAlignment(Qt.AlignmentFlag.AlignCenter)
        head.setWordWrap(True)
        v.addWidget(head)

        self._bar = QProgressBar(); self._bar.setMinimum(0); self._bar.setMaximum(total)
        self._bar.setTextVisible(False); self._bar.setFixedHeight(10)
        self._bar.setStyleSheet(f"""
            QProgressBar {{ background: {T.FILL};
                border:1px solid {T.STROKE}; border-radius:5px; }}
            QProgressBar::chunk {{
                background: qlineargradient(x1:0,y1:0,x2:1,y2:0,
                    stop:0 {T.ACCENT_LO}, stop:1 {T.ACCENT_HI});
                border-radius:5px; }}
        """)
        v.addWidget(self._bar)

        self._sub = QLabel(f"Delivering to {total} board{'s' if total != 1 else ''}…")
        self._sub.setStyleSheet(f"color:{T.TEXT_SEC}; font-size:12px; font-weight:600;")
        self._sub.setAlignment(Qt.AlignmentFlag.AlignCenter)
        v.addWidget(self._sub)

        # results list
        scroll = QScrollArea(); scroll.setWidgetResizable(True)
        scroll.setFrameShape(QFrame.Shape.NoFrame)
        scroll.setAttribute(Qt.WA_TranslucentBackground, True)
        scroll.viewport().setAutoFillBackground(False)
        host = QWidget(); host.setStyleSheet("background:transparent;")
        self._results = QVBoxLayout(host)
        self._results.setContentsMargins(2, 2, 2, 2); self._results.setSpacing(7)
        self._results.addItem(QSpacerItem(20, 8, QSizePolicy.Policy.Minimum, QSizePolicy.Policy.Expanding))
        scroll.setWidget(host)
        v.addWidget(scroll, stretch=1)

        row = QHBoxLayout(); row.addStretch()
        self._done_btn = QPushButton("Done")
        self._done_btn.setMinimumWidth(120); self._done_btn.setEnabled(False)
        self._done_btn.setStyleSheet(ACCENT_BTN_CSS)
        self._done_btn.setCursor(Qt.CursorShape.PointingHandCursor)
        self._done_btn.clicked.connect(lambda: (self.accept(), self.close()))
        row.addWidget(self._done_btn)
        v.addLayout(row)
        self.content_layout.addWidget(_w)

    def keyPressEvent(self, e):
        # Block Escape while the broadcast is still in flight (Done enables
        # only when every board has been attempted).
        if e.key() == Qt.Key.Key_Escape and not self._done_btn.isEnabled():
            e.ignore()
            return
        super().keyPressEvent(e)

    def on_board_done(self, ip, ok, detail):
        target = {"ip": ip}
        # find class/room from parent's cards if available
        parent = self.parent()
        cls = room = sec = "?"
        if parent and hasattr(parent, "board_cards"):
            c = parent.board_cards.get(ip)
            if c: cls, sec, room = c.class_name, c.section, c.room
        row = QFrame()
        bd = T.SUCCESS_BD if ok else T.DANGER_BD
        row.setStyleSheet(f"""
            QFrame {{ background: {T.FILL_SUBTLE};
                border:1px solid {bd}; border-radius:{T.R_MD}px; }}
        """)
        rl = QHBoxLayout(row); rl.setContentsMargins(14, 9, 14, 9); rl.setSpacing(10)
        ico = QLabel("✓" if ok else "✕")
        ico.setStyleSheet(
            f"color:{T.SUCCESS if ok else T.DANGER}; font-size:14px; font-weight:900;"
            f"background:transparent; border:none;"
        )
        ico.setFixedWidth(18); rl.addWidget(ico)
        name = QLabel(f"{format_class(cls, sec)} · Room {room}")
        name.setStyleSheet(f"color:{T.TEXT}; font-size:12.5px; font-weight:700; background:transparent; border:none;")
        rl.addWidget(name, stretch=1)
        det = QLabel(detail)
        det.setStyleSheet(f"color:{T.SUCCESS if ok else T.DANGER}; font-size:11px; font-weight:700; background:transparent; border:none;")
        rl.addWidget(det)
        idx = max(0, self._results.count() - 1)
        self._results.insertWidget(idx, row)

    def on_progress(self, done, total):
        self._bar.setValue(done)
        self._sub.setText(f"Delivering…  {done} / {total}")

    def on_finished(self, ok_count, fail_count, results):
        if fail_count == 0:
            self._icon.setText("✅")
            self._title.setText(f"Sent to {ok_count} board{'s' if ok_count != 1 else ''}!")
            self._title.setStyleSheet(f"color:{T.SUCCESS}; font-size:17px; font-weight:800;")
            self._sub.setText("Everything looks good 👍")
        else:
            self._icon.setText("⚠️")
            self._title.setText(f"Sent to {ok_count}, {fail_count} failed")
            self._title.setStyleSheet(f"color:{T.WARNING}; font-size:17px; font-weight:800;")
            self._sub.setText("Some boards didn't get it — check the list.")
        self._done_btn.setEnabled(True)


# ============================================================================
#  Status badge
# ============================================================================
# ═══════════════════════════════════════════════════════════════════════════
# Network permission — the Windows Firewall warning
# ═══════════════════════════════════════════════════════════════════════════

class _FirewallCheck(QThread):
    """Reads the firewall rules off the UI thread — a few hundred COM
    objects on a school machine, which is not something to stall a paint
    on. Emits one of the firewall.STATE_* values."""
    done = Signal(str)

    def run(self):
        try:
            self.done.emit(firewall.inbound_state())
        except Exception:
            log.debug("firewall check failed", exc_info=True)
            self.done.emit(firewall.STATE_UNKNOWN)


class _FirewallGrant(QThread):
    """Runs the elevated grant and waits for it — including however long
    the user takes over the Windows consent prompt. Emits whether Windows
    allows LogicShare afterwards (re-read from the rules, not assumed)."""
    done = Signal(bool)

    def run(self):
        try:
            self.done.emit(bool(firewall.request_access()))
        except Exception:
            log.debug("firewall grant failed", exc_info=True)
            self.done.emit(False)


class FirewallBanner(QFrame):
    """Shown above the board list when Windows will not let LogicShare use
    the network — the one failure that otherwise looks exactly like "there
    are no boards here".

    Silent unless the answer is BLOCKED or ABSENT: a check that could not
    run (UNKNOWN) is not evidence of a problem, and sending a teacher after
    an administrator password to fix nothing is worse than saying nothing.
    """
    granted = Signal()          # permission was just obtained — rescan

    def __init__(self, parent=None):
        super().__init__(parent)
        self.setObjectName("FirewallBanner")
        self._state = None
        self._checker = None
        self._granter = None

        row = QHBoxLayout(self)
        row.setContentsMargins(14, 12, 12, 12)
        row.setSpacing(12)
        self._icon = QLabel("🛡️")
        self._icon.setFixedWidth(28)
        self._icon.setAlignment(Qt.AlignmentFlag.AlignTop | Qt.AlignmentFlag.AlignHCenter)
        self._icon.setStyleSheet("font-size:20px; background:transparent; border:none;")
        row.addWidget(self._icon)
        col = QVBoxLayout(); col.setSpacing(3); col.setContentsMargins(0, 0, 0, 0)
        self._title = QLabel("LogicShare can't use the network")
        self._msg = QLabel("")
        self._msg.setWordWrap(True)
        col.addWidget(self._title)
        col.addWidget(self._msg)
        row.addLayout(col, stretch=1)
        self.allow_btn = QPushButton("Allow network access")
        self.allow_btn.setFixedHeight(36)
        self.allow_btn.setCursor(Qt.CursorShape.PointingHandCursor)
        self.allow_btn.setStyleSheet(BAR_PRIMARY_CSS)
        self.allow_btn.setToolTip("Windows will ask for permission (an "
                                  "administrator prompt).")
        self.allow_btn.clicked.connect(self.request)
        row.addWidget(self.allow_btn, alignment=Qt.AlignmentFlag.AlignVCenter)
        self._style("warning")
        self.hide()

    # ── look ───────────────────────────────────────────────────────────
    def _style(self, tone):
        fg, bg, bd = ((T.SUCCESS, T.SUCCESS_BG, T.SUCCESS_BD) if tone == "ok"
                      else (T.WARNING, T.WARNING_BG, T.WARNING_BD))
        self.setStyleSheet(f"""
            #FirewallBanner {{
                background: {bg}; border: 1px solid {bd};
                border-radius: {T.R_LG}px;
            }}
        """)
        self._title.setStyleSheet(
            f"color:{fg}; font-size:13px; font-weight:800;"
            f"background:transparent; border:none;")
        self._msg.setStyleSheet(
            f"color:{T.TEXT_SEC}; font-size:12px; font-weight:600;"
            f"background:transparent; border:none;")

    # ── checking ───────────────────────────────────────────────────────
    def check(self):
        """Ask the firewall again (startup, network change, after a grant)."""
        if self._checker is not None and self._checker.isRunning():
            return
        self._checker = _FirewallCheck(self)
        self._checker.done.connect(self.apply_state)
        self._checker.start()

    def state(self):
        return self._state

    def apply_state(self, state):
        self._state = state
        if state in (firewall.STATE_BLOCKED, firewall.STATE_ABSENT):
            log.info("Network permission: %s", state)
            self._style("warning")
            self._icon.setText("🛡️")
            self._title.setText("LogicShare can't use the network")
            self._msg.setText(firewall.explain(state) + " Allow it once and "
                              "LogicShare will find them.")
            self.allow_btn.setText("Allow network access")
            self.allow_btn.setEnabled(True)
            self.allow_btn.show()
            self.show()
        elif self._granter is None:
            self.hide()

    # ── fixing ─────────────────────────────────────────────────────────
    def request(self):
        """Ask Windows, through its own consent prompt, to let us through."""
        if self._granter is not None and self._granter.isRunning():
            return
        self.allow_btn.setEnabled(False)
        self.allow_btn.setText("Waiting for Windows…")
        self._msg.setText("Approve the Windows prompt to give LogicShare "
                          "network permission.")
        self._granter = _FirewallGrant(self)
        self._granter.done.connect(self._on_granted)
        self._granter.start()

    def _on_granted(self, ok):
        self._granter = None
        if ok:
            log.info("Network permission granted")
            self._state = firewall.STATE_ALLOWED
            self._style("ok")
            self._icon.setText("✅")
            self._title.setText("Network access allowed")
            self._msg.setText("LogicShare can see the boards on this "
                              "network now.")
            self.allow_btn.hide()
            self.show()
            self.granted.emit()
            QTimer.singleShot(3500, self._hide_if_allowed)
        else:
            log.info("Network permission NOT granted (declined or blocked "
                     "by policy)")
            self.allow_btn.setEnabled(True)
            self.allow_btn.setText("Try again")
            self._msg.setText("Windows didn't allow it — the prompt was "
                              "declined, or a school policy blocks it. Try "
                              "again, or ask your IT team to allow "
                              "LogicShare.")
            self.check()

    def _hide_if_allowed(self):
        if self._state == firewall.STATE_ALLOWED:
            self.hide()


class StatusBadge(QFrame):
    _STATES = {
        "scanning":  (T.WARNING, T.WARNING_BG, T.WARNING_BD, "Looking…"),
        "no_boards": (T.DANGER,  T.DANGER_BG,  T.DANGER_BD,  "No Boards"),
        "offline":   (T.DANGER,  T.DANGER_BG,  T.DANGER_BD,  "No Wi-Fi"),
        "live":      (T.SUCCESS, T.SUCCESS_BG, T.SUCCESS_BD, "Ready!!"),
    }

    def __init__(self):
        super().__init__()
        self.setFixedHeight(28)
        self.setSizePolicy(QSizePolicy.Policy.Maximum, QSizePolicy.Policy.Fixed)
        lay = QHBoxLayout(self); lay.setContentsMargins(10, 0, 14, 0); lay.setSpacing(8)
        self._dot = PulseDot(T.WARNING)
        self._lbl = QLabel("Looking…")
        lay.addWidget(self._dot, alignment=Qt.AlignmentFlag.AlignVCenter)
        lay.addWidget(self._lbl, alignment=Qt.AlignmentFlag.AlignVCenter)
        self.set_state("scanning")

    def set_state(self, state):
        fg, bg, bd, text = self._STATES.get(state, self._STATES["scanning"])
        self.setStyleSheet(
            f"QFrame {{ background:{bg}; border:1px solid {bd}; border-radius:14px; }}"
        )
        self._dot.set_color(fg)
        self._lbl.setText(text)
        self._lbl.setStyleSheet(
            f"color:{fg}; font-size:11px; font-weight:800;"
            f"letter-spacing:0.4px; background:transparent; border:none;"
        )


# ============================================================================
#  Main window
# ============================================================================
class LogicShareUI(QWidget):
    _SCAN_TIMEOUT_MS  = 10_000
    _AUTO_INTERVAL_MS = 10_000
    _NET_POLL_MS      =  3_000

    def __init__(self, profile):
        super().__init__()
        self.profile = profile
        self.setStyleSheet("background:transparent")

        self.detected_ips   = set()
        self.board_cards    = {}
        self.current_state  = "none"
        self._upload_worker = None
        self._announcement_dlg = None
        self._composer = None

        # Announcement selection-mode state
        self._select_mode = False
        self._pending_title = ""
        self._pending_message = ""
        self._selected_ips = set()
        self._broadcast_worker = None
        self._broadcast_dlg = None
        self._deferred_state = None   # state to apply after a modal closes
        # Search + filter on the board list (single choice per filter, the
        # same as LogicShare Android).
        self._query = ""
        self._f_class = None
        self._f_section = None
        self._net_resync_pending = False  # network hopped while a modal was up
        self._disabled = False        # set True if discovery fatally fails

        self._zc = None; self._browser = None
        self._signals = ZeroconfSignals()
        self._signals.board_detected.connect(self._on_board_detected)
        self._signals.network_changed.connect(self._on_network_changed)

        # What network we were last on. Compared against, not just tested
        # for truthiness: hopping from the corridor SSID to the classroom
        # one leaves "are we online" True the whole way across, which is
        # exactly why the old poll never noticed it.
        self._net_fp = net_watch.fingerprint()
        self._net_watcher = None
        try:
            self._net_watcher = net_watch.NetworkWatcher(
                lambda _old, _new, reason:
                    self._signals.network_changed.emit(reason),
                name="LogicShare")
            self._net_watcher.start()
        except Exception:
            log.exception("Network watcher failed to start — falling back "
                          "to the %d ms poll", self._NET_POLL_MS)

        self._build_ui()

        self._net_timer    = QTimer(self); self._net_timer.timeout.connect(self._poll_network)
        self._net_timer.start(self._NET_POLL_MS)
        self._scan_timeout = QTimer(self); self._scan_timeout.setSingleShot(True)
        self._scan_timeout.timeout.connect(self._on_scan_timeout)
        self._auto_timer   = QTimer(self); self._auto_timer.timeout.connect(self._auto_rescan)
        # Tracked single-shot for the rescan window end (so it's cancellable
        # via _stop_timers — orphan QTimer.singleShot calls were the cause of
        # the UI-duplication-on-rescan bug).
        self._rescan_end_timer = QTimer(self); self._rescan_end_timer.setSingleShot(True)
        self._rescan_end_timer.timeout.connect(self._auto_rescan_end)

        self._poll_network()

    def _build_ui(self):
        # Frameless, with the caption buttons drawn in the header (matching
        # all the other LogicShare windows).
        self.setWindowFlags(
            Qt.WindowType.Window | Qt.WindowType.FramelessWindowHint
        )
        self.setAttribute(Qt.WA_TranslucentBackground)
        self.setWindowTitle("LogicShare")
        # ---- acknowledgment layer -------------------------------------
        # Started here, with the main window, and NOT with the report dialog.
        # A board can acknowledge at any moment; requiring a window to be
        # open for that to be heard would defeat the point. The listener and
        # the timeout sweep both run for the whole life of the app.
        self._ack_identity = TeacherIdentity(APP_DATA_DIR / "device.json")
        self._ack_tracker = AnnouncementTracker(
            APP_DATA_DIR / "announcement_tracking.json",
            self._ack_identity.device_id, parent=self)
        self._ack_server = AckServer(self._ack_tracker)
        self._ack_server.start()
        self._ack_tracker.start_sweeping()
        self._ack_notifier = AckNotifier(self)
        self._ack_notifier.open_report.connect(self._open_announcement_report)
        self._ack_notifier.show_window.connect(self._restore_from_tray)
        self._ack_notifier.quit_requested.connect(self._quit_completely)
        self._ack_tracker.timeout_reached.connect(self._on_ack_timeout)
        self._open_reports = []
        # Distinguishes "the user closed the window" from "the app is really
        # exiting", which are the same event to Qt and must not be to us.
        self._really_quitting = False
        # Boards we have already greeted this session, so the hello is sent
        # once per board rather than on every Zeroconf re-advertise.
        self._greeted_boards = set()

        self.setMinimumSize(520, 660)
        # Roomier default — the old 520x760 left the board list cramped enough
        # that three or four classrooms filled it. These are design units at
        # the 1080p reference; _init_scaling multiplies them, so this stays
        # proportional on a 4K board rather than becoming a postage stamp.
        self.resize(680, 880)
        self._clamp_to_screen()

        self.setStyleSheet(f"""
            QWidget {{
                background: transparent;
                color: {T.TEXT};
                font-family: {T.FONT_FAMILY};
            }}
            QLabel {{ background:transparent; border:none; color:{T.TEXT}; }}
            {SCROLLBAR_CSS}
        """)

        root = QVBoxLayout(self); root.setContentsMargins(0,0,0,0); root.setSpacing(0)

        # ── HEADER — LogicShare Android's, row for row ───────────────────
        # Row 1: logo, name + teacher, status badge, settings gear.
        # Row 2: the search box, always on screen, and the filter square.
        # The caption buttons (minimise, close) ride in a bare title strip
        # at the top of the same panel, so the header is one surface rather
        # than a title bar stacked on a toolbar.
        hdr = QFrame()
        hdr.setObjectName("Header")
        hdr.setStyleSheet(f"""
            #Header {{
                background: {vgrad(*T.SURF_BAR)};
                border-bottom: 1px solid {T.DIVIDER};
                border-top-left-radius: {T.R_WINDOW}px;
                border-top-right-radius: {T.R_WINDOW}px;
            }}
        """)
        hdr.setSizePolicy(QSizePolicy.Policy.Expanding, QSizePolicy.Policy.Fixed)
        hv = QVBoxLayout(hdr); hv.setContentsMargins(0, 0, 0, 12); hv.setSpacing(0)

        self.title_bar = TitleBar(self, title="", show_logo=False, can_maximize=False, bare=True)
        hv.addWidget(self.title_bar)
        # The whole header drags the window, not just the strip above it —
        # on the phone there is no strip at all, so there is nothing to aim at.
        hdr.installEventFilter(self)
        self._hdr = hdr

        row1 = QHBoxLayout(); row1.setContentsMargins(16, 0, 16, 0); row1.setSpacing(0)
        row1.addWidget(logo_label(40), alignment=Qt.AlignmentFlag.AlignVCenter)
        row1.addSpacing(11)
        name_col = QVBoxLayout(); name_col.setSpacing(0); name_col.setContentsMargins(0, 0, 0, 0)
        logo_text = QLabel("LogicShare")
        logo_text.setStyleSheet(
            f"color:{T.TEXT}; font-size:20px; font-weight:800;"
            f"background:transparent; border:none; letter-spacing:-0.3px;"
        )
        teacher = f"{self.profile['first_name']} {self.profile['surname']} {self.profile['title']}".strip()
        self.teacher_lbl = QLabel(teacher)
        self.teacher_lbl.setStyleSheet(
            f"color:{T.TEXT_SEC}; font-size:12px; font-weight:600;"
            f"background:transparent; border:none;"
        )
        name_col.addWidget(logo_text); name_col.addWidget(self.teacher_lbl)
        row1.addLayout(name_col, stretch=1)

        self._badge = StatusBadge()
        row1.addWidget(self._badge, alignment=Qt.AlignmentFlag.AlignVCenter)
        row1.addSpacing(8)
        gear = SettingsButton()
        gear.edit_profile_requested.connect(self._edit_profile)
        gear.history_requested.connect(self._open_history_window)
        row1.addWidget(gear, alignment=Qt.AlignmentFlag.AlignVCenter)
        self._gear = gear
        hv.addLayout(row1)
        hv.addSpacing(12)

        # The filter square carries room for its badge on the right; pull the
        # row in by that much so the square's edge lines up under the gear's.
        row2 = QHBoxLayout(); row2.setContentsMargins(16, 0, 16 - IconSquare.BADGE_ROOM, 0); row2.setSpacing(8)
        self._search_row = SearchBox("Search class, section or room")
        self._search_row.setToolTip("Try 11a, XI A, room 12 — or part of an IP address  (Ctrl+F)")
        self._search_in = self._search_row.edit
        self._search_in.setMaxLength(80)
        self._search_in.textChanged.connect(self._on_query)
        # Esc: clear the search and put the caret away — the phone's Back.
        self._search_row.escape_pressed.connect(lambda: self._set_search_open(False))
        QShortcut(QKeySequence("Ctrl+F"), self, activated=lambda: self._set_search_open(True))
        row2.addWidget(self._search_row, stretch=1, alignment=Qt.AlignmentFlag.AlignBottom)
        self._filter_btn = IconSquare(_draw_filter_glyph, "Filter by class or section", badge_room=True)
        self._filter_btn.clicked.connect(self._show_filter_menu)
        row2.addWidget(self._filter_btn, alignment=Qt.AlignmentFlag.AlignBottom)
        hv.addLayout(row2)
        root.addWidget(hdr)

        # ── BODY ─────────────────────────────────────────────────────────
        body = QWidget(); body.setStyleSheet("background:transparent;")
        body.setSizePolicy(QSizePolicy.Policy.Expanding, QSizePolicy.Policy.Expanding)
        bl = QVBoxLayout(body); bl.setContentsMargins(16, 14, 16, 0); bl.setSpacing(8)

        # ONE caption, as on the phone: while searching or filtering, the
        # count takes the title's place instead of sitting beside it.
        # TEXT_SEC, not the phone's TEXT_MUTED: it sits straight on the window
        # body, the darkest surface, where the muted grey reads as disabled.
        self._caption_color = T.TEXT_SEC
        self._showing_lbl = caption_label("SMART BOARDS NEARBY", self._caption_color)
        self._showing_lbl.setContentsMargins(6, 0, 0, 0)
        # Above the list it explains: "no boards" because Windows is
        # dropping their replies looks identical to "no boards here".
        self._fw_banner = FirewallBanner()
        self._fw_banner.granted.connect(self._refresh)
        bl.addWidget(self._fw_banner)
        bl.addWidget(self._showing_lbl)
        # First check once the window is up; it reads the rules off-thread.
        QTimer.singleShot(1200, self._fw_banner.check)

        # Active filters, shown as removable chips, and "Clear".
        self._chips_row = QWidget(); self._chips_row.setStyleSheet("background:transparent;")
        self._chips = QHBoxLayout(self._chips_row)
        self._chips.setContentsMargins(0, 0, 0, 0); self._chips.setSpacing(8)
        self._chips_row.hide()
        bl.addWidget(self._chips_row)

        self._scroll = QScrollArea(); self._scroll.setWidgetResizable(True)
        self._scroll.setFrameShape(QFrame.Shape.NoFrame)
        self._scroll.setAttribute(Qt.WA_TranslucentBackground, True)
        self._scroll.viewport().setAutoFillBackground(False)
        self._scroll_host = QWidget(); self._scroll_host.setStyleSheet("background:transparent;")
        self._list = QVBoxLayout(self._scroll_host)
        self._list.setContentsMargins(0, 4, 0, 16); self._list.setSpacing(12)
        self._list.addItem(QSpacerItem(20, 40, QSizePolicy.Policy.Minimum, QSizePolicy.Policy.Expanding))
        self._scroll.setWidget(self._scroll_host)
        bl.addWidget(self._scroll)
        root.addWidget(body, stretch=1)

        # ── TOOLBAR — Android's slim bottom bar ──────────────────────────
        # The count on its own line, then the actions: Create Announcement
        # taking the width, Refresh beside it. CTRL_H tall, one line each —
        # a toolbar, not a pair of hero buttons.
        def bar(name, accent_edge):
            f = QFrame(); f.setObjectName(name)
            f.setStyleSheet(f"""
                #{name} {{
                    background: {vgrad(*T.SURF_BAR)};
                    border-top: 1px solid {T.STROKE_ACC if accent_edge else T.DIVIDER};
                    border-bottom-left-radius: {T.R_WINDOW}px;
                    border-bottom-right-radius: {T.R_WINDOW}px;
                }}
            """)
            f.setSizePolicy(QSizePolicy.Policy.Expanding, QSizePolicy.Policy.Fixed)
            v = QVBoxLayout(f); v.setContentsMargins(16, 9, 16, 12); v.setSpacing(8)
            return f, v

        def bar_button(text, primary):
            b = QPushButton(text)
            b.setFixedHeight(T.CTRL_H)
            b.setCursor(Qt.CursorShape.PointingHandCursor)
            b.setStyleSheet(BAR_PRIMARY_CSS if primary else BAR_SUBTLE_CSS)
            return b

        tlb, tv = bar("Toolbar", False)
        self._count_lbl = QLabel("Looking for LogicBoards…📡")
        self._count_lbl.setStyleSheet(
            f"color:{T.TEXT_SEC}; font-size:12px; font-weight:700;"
            f"background:transparent; border:none;"
        )
        tv.addWidget(self._count_lbl)
        tr = QHBoxLayout(); tr.setSpacing(8)
        self._announce_btn = bar_button("📢  Create Announcement", True)
        self._announce_btn.clicked.connect(self._open_composer)
        rbtn = bar_button("🔄  Refresh", False)
        rbtn.setMinimumWidth(120)
        rbtn.clicked.connect(self._refresh)
        tr.addWidget(self._announce_btn, stretch=1); tr.addWidget(rbtn)
        tv.addLayout(tr)

        # ---- Selection action bar (shown only during selection mode) ----
        # Lives in the SAME toolbar slot via a QStackedWidget so we never
        # spawn a second window. Page 0 = normal toolbar, page 1 = the
        # "N boards selected / Cancel / Send" bar.
        self._sel_bar, sv = bar("SelBar", True)
        sr = QHBoxLayout(); sr.setSpacing(8)
        self._sel_count_lbl = QLabel("0 boards selected")
        self._sel_count_lbl.setStyleSheet(
            f"color:{T.ACCENT_HI}; font-size:12px; font-weight:800;"
            f"background:transparent; border:none;"
        )
        sr.addWidget(self._sel_count_lbl, stretch=1)
        self._select_all_btn = QPushButton("Select all")
        self._select_all_btn.setStyleSheet(LINK_BTN_CSS)
        self._select_all_btn.setCursor(Qt.CursorShape.PointingHandCursor)
        self._select_all_btn.clicked.connect(self._select_all_shown)
        sr.addWidget(self._select_all_btn)
        sv.addLayout(sr)
        sb = QHBoxLayout(); sb.setSpacing(8)
        cancel_btn = bar_button("Cancel", False)
        cancel_btn.clicked.connect(self._exit_selection_mode)
        self._sel_send_btn = bar_button("🚀  Send Announcement", True)
        self._sel_send_btn.setEnabled(False)
        self._sel_send_btn.clicked.connect(self._send_to_selected)
        sb.addWidget(cancel_btn, stretch=38); sb.addWidget(self._sel_send_btn, stretch=62)
        sv.addLayout(sb)

        self._toolbar_stack = QStackedWidget()
        self._toolbar_stack.addWidget(tlb)            # page 0 — normal
        self._toolbar_stack.addWidget(self._sel_bar)  # page 1 — selection
        self._toolbar_stack.setSizePolicy(QSizePolicy.Policy.Expanding, QSizePolicy.Policy.Fixed)
        root.addWidget(self._toolbar_stack)

    def eventFilter(self, obj, ev):
        # Header drag → the title strip's own move logic.
        if obj is getattr(self, "_hdr", None):
            et = ev.type()
            if et == ev.Type.MouseButtonPress:
                self.title_bar.mousePressEvent(ev); return True
            if et == ev.Type.MouseMove:
                self.title_bar.mouseMoveEvent(ev); return True
            if et == ev.Type.MouseButtonRelease:
                self.title_bar.mouseReleaseEvent(ev); return True
        return super().eventFilter(obj, ev)

    def paintEvent(self, _):
        paint_window_glass(self)

    def showEvent(self, event):
        super().showEvent(event)
        _prepare_window_surface(self)

    def _rebuild(self):
        # Re-entrance guard: if a Zeroconf signal fires inside a modal's
        # nested event loop and triggers _rebuild while another _rebuild
        # is still mid-execution, we'd end up with two layout populations
        # racing each other → visible duplicate UI. Skip the nested call.
        if getattr(self, "_rebuilding", False):
            return
        self._rebuilding = True
        try:
            self._do_rebuild()
        finally:
            self._rebuilding = False

    def _do_rebuild(self):
        # Bulletproof teardown: fully clear the layout AND explicitly reparent
        # every existing child widget so no zombie can render on top of the
        # new content. The previous version only called takeAt() without
        # reparenting cards that were still in `board_cards.values()`,
        # leaving them in a half-removed state. If a card was removed via
        # the offline-warning flow during a modal, the modal's nested event
        # loop could re-enter _rebuild while a stale layout item was still
        # half-attached — producing a visible duplicate of the entire UI
        # (radar AND board card both visible) until the next full rebuild.
        live = set(self.board_cards.values())
        # 1. Remove every layout item.
        while self._list.count():
            it = self._list.takeAt(0)
            w = it.widget() if it else None
            if w is None:
                continue
            if w in live:
                # Keep alive but fully detach so it can't paint until re-added.
                w.setParent(None)
                w.hide()
            else:
                # Not a live card — destroy it. Calling deleteLater alone is
                # not enough because the queued deletion runs at the next
                # event loop tick; until then the widget could re-render.
                # setParent(None) + hide() ensures it's invisible immediately.
                w.setParent(None)
                w.hide()
                w.deleteLater()
        # 2. Also walk the scroll-host's direct children to catch any widget
        #    that somehow lost its layout slot but still has the host as
        #    parent (the exact failure mode of the duplication bug).
        for child in list(self._scroll_host.findChildren(QWidget, options=Qt.FindDirectChildrenOnly)):
            if child not in live:
                child.setParent(None)
                child.hide()
                child.deleteLater()
        # 3. Now repopulate.
        if self.board_cards:
            visible = self._visible_cards()
            for c in self.board_cards.values():
                c.set_select_mode(self._select_mode)
                if self._select_mode:
                    c.set_selected(c.ip in self._selected_ips, animate=False, emit=False)
                if c in visible:
                    c.show()
                    self._list.addWidget(c)
                else:
                    c.hide()
            if not visible:
                self._list.addWidget(self._no_match_panel())
        elif self.current_state == "scanning":
            self._list.addWidget(ScanningPanel())
        elif self._disabled or self.current_state == "offline":
            self._list.addWidget(OfflinePanel(disabled=self._disabled))
        elif self.current_state == "no_boards":
            panel = NoBoardPanel()
            panel.help_requested.connect(lambda: show_help(self))
            self._list.addWidget(panel)
            grow_in(panel, dur=T.DUR_SLOW)
        self._list.addItem(QSpacerItem(20, 40, QSizePolicy.Policy.Minimum, QSizePolicy.Policy.Expanding))
        self._refresh_search_chrome()

    # ---------------------------------------------------------- search + filter
    def _filter_active(self):
        return bool(self._query.strip()) or self._f_class is not None \
            or self._f_section is not None

    def _visible_cards(self):
        """Cards that pass the search and the filters, in list order."""
        cards = list(self.board_cards.values())
        if not self._filter_active():
            return cards
        q = smart_search.parse(self._query)
        out = []
        for c in cards:
            if self._f_class is not None and str(c.class_name).lower() != self._f_class.lower():
                continue
            if self._f_section is not None and str(c.section).lower() != self._f_section.lower():
                continue
            if smart_search.match_board(q, c.class_name, c.section, c.room, c.ip):
                out.append(c)
        return out

    def _no_match_panel(self):
        w = QFrame(); w.setStyleSheet("background:transparent; border:none;")
        # Centred with stretches, not setAlignment(AlignCenter): a centred
        # layout sizes a word-wrapped label to its one-line hint and the
        # second line of a long query drew on top of the first.
        w.setSizePolicy(QSizePolicy.Policy.Expanding, QSizePolicy.Policy.Expanding)
        v = QVBoxLayout(w); v.setContentsMargins(32, 40, 32, 20); v.setSpacing(8)
        v.addStretch(1)
        ico = _GlyphView(_draw_search_glyph, 46, T.TEXT_SEC)
        v.addWidget(ico, alignment=Qt.AlignmentFlag.AlignCenter)
        v.addSpacing(6)
        head = QLabel(f"No boards match “{self._query.strip()}”" if self._query.strip()
                      else "No boards match these filters")
        head.setStyleSheet(f"color:{T.TEXT}; font-size:18px; font-weight:800;")
        head.setAlignment(Qt.AlignmentFlag.AlignCenter); head.setWordWrap(True)
        v.addWidget(head)
        hint = QLabel("Try “11a”, “XI A”, “room 12” or part of an IP address.")
        hint.setStyleSheet(f"color:{T.TEXT_SEC}; font-size:12.5px; font-weight:600;")
        hint.setAlignment(Qt.AlignmentFlag.AlignCenter); hint.setWordWrap(True)
        v.addWidget(hint)
        v.addSpacing(8)
        btn = QPushButton("Clear search && filters")
        btn.setFixedHeight(T.CTRL_H)
        btn.setStyleSheet(BAR_SUBTLE_CSS); btn.setCursor(Qt.CursorShape.PointingHandCursor)
        btn.clicked.connect(self._clear_search_and_filters)
        v.addWidget(btn, alignment=Qt.AlignmentFlag.AlignCenter)
        v.addStretch(2)
        return w

    def _refresh_search_chrome(self):
        """The filter square's state and badge, the caption, the filter chips
        and the Select-all link — everything that follows search/filter."""
        if not hasattr(self, "_select_all_btn"):
            return        # _rebuild can run before the toolbar is built
        try:
            n_filters = (self._f_class is not None) + (self._f_section is not None)
            filtering = n_filters > 0
            self._filter_btn.set_active(filtering)
            self._filter_btn.set_badge(n_filters)
            total = len(self.board_cards)
            shown = len(self._visible_cards())
            # One caption, as on the phone: while a search or filter is
            # narrowing the list, the count replaces the title.
            if self._filter_active() and total:
                self._showing_lbl.setText(f"SHOWING {shown} OF {total} BOARDS")
                self._showing_lbl.setStyleSheet(caption_css(T.ACCENT_HI))
            else:
                self._showing_lbl.setText("SMART BOARDS NEARBY")
                self._showing_lbl.setStyleSheet(caption_css(self._caption_color))
            # chips
            while self._chips.count():
                it = self._chips.takeAt(0)
                # Held once: after setParent(None) the layout item no longer
                # owns the widget and it.widget() returns None, so calling it
                # a second time raised — which the except below swallowed,
                # silently skipping every update after it (the Select-all
                # button never appeared).
                wd = it.widget()
                if wd is not None:
                    wd.setParent(None); wd.deleteLater()
            if filtering:
                for text, clear in (
                        (f"Class {self._f_class}  ✕", lambda: self._set_filter(cls=None))
                        if self._f_class is not None else (None, None),
                        (f"Section {str(self._f_section).upper()}  ✕",
                         lambda: self._set_filter(section=None))
                        if self._f_section is not None else (None, None)):
                    if text is None:
                        continue
                    chip = QPushButton(text)
                    chip.setCursor(Qt.CursorShape.PointingHandCursor)
                    chip.setFixedHeight(30)
                    chip.setToolTip("Remove this filter")
                    chip.setStyleSheet(_pill_css(True).replace("border-radius: 16px", "border-radius: 15px"))
                    chip.clicked.connect(clear)
                    self._chips.addWidget(chip)
                self._chips.addStretch()
                clear_all = QPushButton("Clear")
                clear_all.setCursor(Qt.CursorShape.PointingHandCursor)
                clear_all.setStyleSheet(LINK_BTN_CSS)
                clear_all.clicked.connect(lambda: self._set_filter(cls=None, section=None))
                self._chips.addWidget(clear_all)
            self._chips_row.setVisible(filtering)
            # Select all: only while there is something visible left to tick.
            vis = self._visible_cards()
            remaining = [c for c in vis if c.ip not in self._selected_ips]
            self._select_all_btn.setVisible(self._select_mode and bool(remaining))
            self._select_all_btn.setText("Select all" if len(vis) == total else "Select all shown")
        except RuntimeError:
            # Qt objects already deleted during teardown. NOT AttributeError:
            # catching that is what hid the chip bug above for a whole test
            # run. The only legitimate early call is guarded explicitly.
            pass

    def _toggle_search(self):
        """Kept for callers of the old Search button: there is no button any
        more — the box is always there — so this just puts the caret in it."""
        self._set_search_open(True)

    def _set_search_open(self, open_):
        """The search box never hides now (it is always on screen, as on the
        phone). "Open" puts the caret in it; "closed" clears the query and
        takes the caret away — Esc, the desktop's Back."""
        if open_:
            self._search_in.setFocus()
            self._search_in.selectAll()
        else:
            self._search_in.clear()          # triggers _on_query("")
            self._search_in.clearFocus()
        self._refresh_search_chrome()

    def _on_query(self, text):
        self._query = text
        self._rebuild()

    def _set_filter(self, **kw):
        if "cls" in kw: self._f_class = kw["cls"]
        if "section" in kw: self._f_section = kw["section"]
        self._rebuild()

    def _clear_search_and_filters(self):
        self._f_class = None; self._f_section = None
        self._search_in.clear()
        self._rebuild()

    def _show_filter_menu(self):
        popup = FluentPopup()
        popup.setFixedWidth(240)
        classes = _numeric_sort(c.class_name for c in self.board_cards.values())
        sections = _numeric_sort(c.section for c in self.board_cards.values())
        if not classes and not sections:
            popup.add_caption("NOTHING TO FILTER YET")
            popup.add_item("📡", "Filters appear once boards are found", lambda: None)
        if classes:
            popup.add_caption("CLASS")
            popup.add_item("✓" if self._f_class is None else "", "All classes",
                           lambda: self._set_filter(cls=None))
            for c in classes:
                popup.add_item("✓" if self._f_class == c else "", f"Class {c}",
                               lambda c=c: self._set_filter(cls=c))
        if sections:
            popup.add_separator()
            popup.add_caption("SECTION")
            popup.add_item("✓" if self._f_section is None else "", "All sections",
                           lambda: self._set_filter(section=None))
            for sct in sections:
                popup.add_item("✓" if (self._f_section or "").lower() == sct.lower() else "",
                               f"Section {sct.upper()}", lambda sct=sct: self._set_filter(section=sct))
        anchor = self._filter_btn.mapToGlobal(QPoint(0, self._filter_btn.height() + 8))
        popup.show_at(anchor)
        self._filter_popup = popup

    def _select_all_shown(self):
        for c in self._visible_cards():
            if not c.is_selected():
                c.set_selected(True, animate=True, emit=True)
        self._refresh_search_chrome()

    def _enter_state(self, state):
        # Guard: never mutate the board list / rebuild the UI while a modal
        # dialog (warning popup, file picker) is open. Timers that fire
        # during a modal's nested event loop would otherwise rebuild the
        # scroll area underneath the dialog and duplicate widgets.
        #
        # ROOT-CAUSE FIX for the UI-duplication bug: previously the deferred
        # state was stored but NEVER applied — after the modal closed, the
        # state machine (current_state) and the actual widgets on screen
        # were out of sync. A later _enter_state(same_state) would early-
        # return ("already in that state") without rebuilding, leaving a
        # stale ScanningPanel on screen while board cards were added by a
        # different code path → radar + cards visible together, i.e. the
        # "duplicated UI". Now every deferral arms a replay timer that
        # applies the pending state the moment the modal closes.
        if QApplication.activeModalWidget() is not None:
            self._deferred_state = state
            self._arm_modal_replay()
            return
        if self.current_state == state: return
        self.current_state = state
        self._badge.set_state(state)
        self._rebuild()

    def _arm_modal_replay(self):
        """Start (or keep running) the timer that replays deferred work
        once no modal dialog is active anymore."""
        if not hasattr(self, "_modal_replay_timer"):
            self._modal_replay_timer = QTimer(self)
            self._modal_replay_timer.setInterval(120)
            self._modal_replay_timer.timeout.connect(self._modal_replay_tick)
        if not self._modal_replay_timer.isActive():
            self._modal_replay_timer.start()

    def _modal_replay_tick(self):
        if QApplication.activeModalWidget() is not None:
            return          # modal still open — keep polling
        self._modal_replay_timer.stop()
        # 0. A network change outranks everything queued behind it. The
        #    removals and the deferred state below both describe the network
        #    we were on before the hop, and replaying them would rebuild the
        #    card list from boards that are no longer reachable.
        if getattr(self, "_net_resync_pending", False):
            self._net_resync_pending = False
            self._pending_removals = []
            self._deferred_state = None
            self._on_network_changed("deferred")
            return
        # 1. Replay card removals that were queued during the modal.
        pending = getattr(self, "_pending_removals", None)
        if pending:
            self._pending_removals = []
            for ip in pending:
                self._on_card_removed(ip)
            return          # _on_card_removed drives state; deferred is stale
        # 2. Replay the deferred state transition.
        state = self._deferred_state
        self._deferred_state = None
        if state is not None and state != self.current_state:
            self._enter_state(state)

    def _poll_network(self):
        # Don't touch scan state while a modal dialog is open — wait until
        # it closes (prevents rebuilds underneath the dialog).
        if QApplication.activeModalWidget() is not None:
            return

        # Identity first, and BEFORE the early return below.
        #
        # This used to read: if there are board cards, do nothing. Which
        # meant that the moment discovery succeeded, this poll went blind —
        # permanently. Hop to another Wi-Fi network with a board on screen
        # and the card stayed there forever, pointing at an address on a
        # network the laptop had left, while the multicast socket sat bound
        # to the dead adapter. No mDNS goodbye could arrive to remove the
        # card either, because the socket that would have carried it was the
        # one that died. Hitting Refresh by hand was the only way out.
        #
        # The watcher normally gets here first; this is the backstop for a
        # change the OS did not report.
        if net_watch.fingerprint() != self._net_fp:
            self._on_network_changed("poll")
            return

        if not is_network_connected():
            self._stop_timers(); self._stop_zeroconf()
            self._enter_state("offline"); return
        if self.board_cards: return
        if self.current_state not in ("scanning", "no_boards"):
            self._begin_scan()

    def _begin_scan(self):
        # Kill any stale timers / Zeroconf from a prior cycle so we never
        # have two overlapping scan flows firing _rebuild concurrently.
        self._stop_timers()
        self._stop_zeroconf()
        self._enter_state("scanning")
        if not self._start_zeroconf():
            return   # discovery failed → disabled state already shown
        self._scan_timeout.start(self._SCAN_TIMEOUT_MS)

    def _on_scan_timeout(self):
        if QApplication.activeModalWidget() is not None: return
        if self.board_cards: return
        self._enter_state("no_boards")
        if not self._auto_timer.isActive():
            self._auto_timer.start(self._AUTO_INTERVAL_MS)

    def _auto_rescan(self):
        if QApplication.activeModalWidget() is not None: return
        if self.board_cards: self._auto_timer.stop(); return
        # Flip back to scanning state so the radar animation shows for the
        # rescan window.
        self._enter_state("scanning")
        self._stop_zeroconf()
        if not self._start_zeroconf():
            return   # discovery failed → disabled state already shown
        log.info("Auto-rescanning for Smart Boards")
        # Use the TRACKED single-shot timer, not an orphan QTimer.singleShot.
        # Orphan singleShots can't be cancelled, so clicking Refresh / a board
        # going offline / overlapping rescans would leave stale callbacks
        # queued that later fire _enter_state→_rebuild and duplicate the UI.
        self._rescan_end_timer.start(4_000)

    def _auto_rescan_end(self):
        if QApplication.activeModalWidget() is not None: return
        if self.board_cards: return
        # Nothing found — fall back to the NoBoardPanel until the next tick.
        self._enter_state("no_boards")

    def _stop_timers(self):
        self._scan_timeout.stop()
        self._auto_timer.stop()
        self._rescan_end_timer.stop()   # kill any pending rescan-end callback

    def _start_zeroconf(self):
        """Start mDNS discovery. Returns True on success. On failure (mDNS
        port unavailable, another responder, network down, NIC change) it
        logs, shows the graceful 'temporarily disabled' message, and returns
        False instead of letting the exception crash the app — so two
        LogicShare instances on a LAN can never silently kill each other."""
        try:
            self._zc = Zeroconf()
            self._browser = ServiceBrowser(
                self._zc, "_logicboard._tcp.local.",
                LogicBoardListener(self._signals),
            )
            return True
        except Exception:
            log.exception("Failed to start device discovery (Zeroconf)")
            # Clean up a half-initialised Zeroconf so we don't leak a socket.
            try:
                if self._zc: self._zc.close()
            except Exception:
                log.exception("Error closing partial Zeroconf")
            self._zc = None; self._browser = None
            self._enter_disabled_state()
            return False

    def _stop_zeroconf(self):
        try:
            if self._browser: self._browser.cancel()
        except Exception:
            log.exception("Error cancelling Zeroconf browser")
        try:
            if self._zc: self._zc.close()
        except Exception:
            log.exception("Error closing Zeroconf")
        self._browser = None; self._zc = None

    def _enter_disabled_state(self):
        """Graceful fatal-discovery handling: show a one-time message and
        stop polling, WITHOUT terminating the app (teachers should never see
        a silent crash). Restarting the app recovers."""
        if getattr(self, "_disabled", False):
            return
        self._disabled = True
        log.error("LogicShare entering disabled state (discovery unavailable)")
        try:
            self._net_timer.stop()
            self._stop_timers()
        except Exception:
            log.exception("Error stopping timers on disable")
        self._badge.set_state("offline")
        self._count_lbl.setText("LogicShare is temporarily disabled")
        CustomMessageBox.warning(
            self, "LogicShare",
            "LogicShare on this smart board has been temporarily disabled due to an error.",
            detail="Device discovery could not start. Please restart LogicShare or check the network connection.",
        )

    def closeEvent(self, e):
        """Closing the window is not closing LogicShare.

        The acknowledgment warning fires five minutes after an announcement
        goes out. A teacher sends one, closes the window, and walks to the
        next class — under the old behaviour that quit the process, took the
        listener down with it, and the warning they were owed never happened.
        So the window hides into the tray and everything underneath it keeps
        running: the ack listener, the timeout sweep, board discovery.

        Actually quitting is still one right-click away (see
        ``_quit_completely``), and if there is no tray to hide into then
        there is no way back to a hidden window either, so that case shuts
        down for real.
        """
        if not self._really_quitting and self._ack_notifier.available:
            e.ignore()
            self.hide()
            log.info("Window closed to tray — acknowledgment layer still running")
            # No notification. See AckNotifier.went_to_background — the tray
            # icon is the affordance, and a balloon that says nothing has
            # gone wrong trains people to ignore the ones that mean
            # something.
            self._ack_notifier.went_to_background()
            return

        log.info("LogicShare shutting down")
        self._shutdown_ack()
        try:
            self._net_timer.stop()
            self._stop_timers()
        except Exception:
            log.exception("Error stopping timers during shutdown")
        self._stop_zeroconf()
        for name, w in (("upload", self._upload_worker),
                        ("broadcast", self._broadcast_worker)):
            try:
                if w is not None and w.isRunning():
                    log.info(f"Waiting for {name} worker to finish…")
                    if not w.wait(5000):
                        log.warning(f"{name} worker did not finish in time")
            except Exception:
                log.exception(f"Error waiting for {name} worker")
        super().closeEvent(e)
        # Explicit, because quitOnLastWindowClosed is off — see main(). This
        # is the no-tray path (or a real quit that came in some other way),
        # and without it the process would live on with nothing on screen.
        QApplication.quit()

    def _restore_from_tray(self):
        """Bring the window back from the tray.

        ``showNormal`` rather than ``show``: a window hidden while minimised
        comes back minimised, which looks exactly like the click doing
        nothing. ``activateWindow`` after ``raise_`` because Windows will
        otherwise leave it behind whatever had focus.
        """
        self.showNormal()
        self.raise_()
        self.activateWindow()

    def _quit_completely(self):
        """Tray → Quit. The only route that actually ends the process."""
        log.info("Quit requested from the tray")
        self._really_quitting = True
        self.close()
        QApplication.quit()

    def _on_network_changed(self, reason):
        """The network moved. Everything we learned from the old one is junk.

        Board cards, the detected-IP set and the multicast socket all belong
        to the network that was here a moment ago. A card left behind after a
        Wi-Fi hop is worse than no card: it looks live, and sending to it
        fails against an address that now belongs to somebody else — or to
        nobody.

        Note this cannot be conditional on the address having changed. An
        adapter that drops and returns on the same DHCP lease changes no
        address while still killing the socket bound to it, so an OS event
        is acted on whether or not the fingerprint moved. The poll is the
        one that has to compare, because it has no other way of knowing
        anything happened.
        """
        fp = net_watch.fingerprint()
        unchanged = (fp == self._net_fp)
        self._net_fp = fp
        if unchanged and reason == "poll":
            return
        # A new network can be a new firewall profile (public vs private),
        # and a rule can cover one and not the other.
        if getattr(self, "_fw_banner", None) is not None:
            self._fw_banner.check()
        log.info("Network changed (%s): now %s — dropping %d board(s) and "
                 "rescanning", reason, net_watch.describe(fp),
                 len(self.board_cards))
        if QApplication.activeModalWidget() is not None:
            # Same rule as everywhere else in this class: never mutate the
            # board list inside a modal's nested event loop. The replay
            # timer picks it up the moment the dialog closes.
            self._net_resync_pending = True
            self._arm_modal_replay()
            return
        self._net_resync_pending = False
        # A selection made against boards that no longer exist would send
        # to stale addresses the moment the user hit Send.
        try:
            self._selected_ips.clear()
        except Exception:
            pass
        self._refresh()

    def _refresh(self):
        self._stop_timers(); self._stop_zeroconf()
        for c in self.board_cards.values():
            c.setParent(None); c.deleteLater()
        self.board_cards.clear(); self.detected_ips.clear()
        self._count_lbl.setText("Looking for LogicBoards…📡"); self.current_state = "none"
        if is_network_connected(): self._begin_scan()
        else: self._enter_state("offline")

    def _on_board_detected(self, cls, section, room, ip):
        # Greet BEFORE the early return. A board re-advertising itself is
        # exactly the case where it may be holding acknowledgments for us —
        # a Wi-Fi hop on either side re-triggers discovery — and _greet_board
        # has its own once-per-board guard, so calling it here is free.
        self._greet_board(ip)
        if ip in self.detected_ips: return
        self.detected_ips.add(ip); self._stop_timers()

        card = BoardCard(cls, section, room, ip)
        card.removed.connect(self._on_card_removed)
        card.send_file_requested.connect(self._on_send_file_requested)
        card.send_announcement_requested.connect(self._on_send_announcement_requested)
        card.selection_toggled.connect(self._on_selection_toggled)
        # If we're mid-selection, a freshly discovered board joins as
        # selectable immediately (single source of truth stays in sync).
        if self._select_mode:
            card.set_select_mode(True)
        self.board_cards[ip] = card

        # Force a rebuild by resetting the state — but only when no modal is
        # open. During a modal, _enter_state defers; force-resetting
        # current_state to "none" first would leave the state machine in
        # "none" with the card missing from the layout (deferral used to
        # lose the transition entirely). The replay timer now applies the
        # deferred "live" after the modal closes, and rebuild picks the
        # card up from board_cards.
        if QApplication.activeModalWidget() is None:
            self.current_state = "none"
        self._enter_state("live")

        n = len(self.board_cards)
        if self._select_mode:
            self._update_selection_count()
        else:
            self._count_lbl.setText(f"{n} LogicBoard{'s' if n != 1 else ''} found 😎🔥")

    def _on_card_removed(self, ip):
        # A board dying while a dialog (announcement composer, upload
        # progress, warning box) is open must NOT mutate the board list /
        # restart discovery inside the modal's nested event loop — the
        # rebuild would be deferred-and-lost, desyncing state from UI
        # (the other half of the duplication bug). Queue it; the replay
        # timer processes it right after the modal closes.
        if QApplication.activeModalWidget() is not None:
            if not hasattr(self, "_pending_removals"):
                self._pending_removals = []
            if ip not in self._pending_removals:
                self._pending_removals.append(ip)
            self._arm_modal_replay()
            return
        self.detected_ips.discard(ip)
        self._selected_ips.discard(ip)
        c = self.board_cards.pop(ip, None)
        if c: c.setParent(None); c.deleteLater()
        # Any state change that was deferred while the modal was open is now
        # stale (the board list just changed) — clear it so it can't replay.
        self._deferred_state = None
        n = len(self.board_cards)
        if self._select_mode:
            self._update_selection_count()
            self._rebuild()
            return
        if n == 0:
            self._count_lbl.setText("Looking for LogicBoards…📡")
            # Full reset: kill all timers + Zeroconf before restarting so
            # stale auto-rescan cycles can't fire _rebuild() and duplicate
            # the UI while the new scan is starting.
            self._stop_timers()
            self._stop_zeroconf()
            self.current_state = "none"
            if is_network_connected(): self._begin_scan()
            else: self._enter_state("offline")
        else:
            self._count_lbl.setText(f"{n} LogicBoard{'s' if n != 1 else ''} found 😎🔥")
            self._rebuild()

    def _on_send_file_requested(self, ip):
        card = self.board_cards.get(ip)
        if not card: return

        file_path, _ = QFileDialog.getOpenFileName(self, "Pick a file to send")
        if not file_path:
            return

        dlg = UploadProgressDialog(self, file_path, card.class_name, card.section, card.room)

        worker = UploadWorker(
            url       = f"http://{ip}:16384/upload",
            file_path = file_path,
            profile   = self.profile,
        )
        worker.progress    .connect(dlg.update_progress)
        worker.finished_ok .connect(dlg.mark_success)
        worker.failed      .connect(dlg.mark_failure)

        self._upload_worker = worker
        worker.start()
        dlg.exec()
        worker.wait()
        self._upload_worker = None
        dlg.deleteLater()

    def _on_send_announcement_requested(self, ip):
        card = self.board_cards.get(ip)
        if not card: return
        dlg = AnnouncementDialog(self, self.profile, ip, card.class_name, card.section, card.room)
        self._announcement_dlg = dlg
        dlg.exec()
        self._announcement_dlg = None
        dlg.deleteLater()

    def _open_composer(self, *_):
        # ``*_`` because this is wired straight to QPushButton.clicked, which
        # passes a `checked` bool. It used to be absorbed by a `prefill`
        # parameter, where a False quietly did the right thing for the wrong
        # reason — and the one caller that passed a real value passed a tuple
        # to something expecting a dict, which is how "Resend" crashed the app.
        #
        # Compose-only dialog. When the teacher hits "Select Recipients",
        # it hands (title, message) back here and the MAIN WINDOW enters
        # selection mode — no second window, no duplicate board list.
        self._composer = AnnouncementComposer(
            self.profile, parent=self, tracker=self._ack_tracker)
        self._composer.proceed_to_select.connect(self._enter_selection_mode)
        self._composer.show()
        self._composer.raise_()
        self._composer.activateWindow()

    # ---------------------------------------------------------- selection mode
    def _enter_selection_mode(self, title, message):
        self._pending_title = title
        self._pending_message = message
        self._selected_ips.clear()

        if not self.board_cards:
            CustomMessageBox.information(
                self, "No Smart Boards",
                "No Smart Boards are online right now.",
                detail="Try Refresh, then create your announcement again."
            )
            return

        self._select_mode = True
        # Flip every existing card into selectable state
        for c in self.board_cards.values():
            c.set_select_mode(True)
            c.set_selected(False, animate=False, emit=False)

        self._update_selection_count()
        self._toolbar_stack.setCurrentIndex(1)   # show selection action bar
        self.raise_(); self.activateWindow()

    def _exit_selection_mode(self):
        self._select_mode = False
        self._selected_ips.clear()
        for c in self.board_cards.values():
            c.set_select_mode(False)
        self._toolbar_stack.setCurrentIndex(0)   # back to normal toolbar
        # Refresh the count label to the normal "N found" text
        n = len(self.board_cards)
        if n:
            self._count_lbl.setText(f"{n} LogicBoard{'s' if n != 1 else ''} found 😎🔥")

    def _on_selection_toggled(self, ip, selected):
        if selected: self._selected_ips.add(ip)
        else:        self._selected_ips.discard(ip)
        self._update_selection_count()

    def _update_selection_count(self):
        n = len(self._selected_ips)
        self._sel_count_lbl.setText(f"{n} board{'s' if n != 1 else ''} selected")
        self._sel_send_btn.setEnabled(n > 0)
        self._refresh_search_chrome()

    def _send_to_selected(self):
        targets = [
            {"class": c.class_name, "section": c.section, "room": c.room, "ip": c.ip}
            for ip, c in self.board_cards.items()
            if ip in self._selected_ips
        ]
        if not targets:
            return

        title = self._pending_title
        message = self._pending_message

        # Exit selection mode immediately — cards return to normal state.
        self._exit_selection_mode()

        # Fire the broadcast (non-blocking) + show a small progress popup.
        dlg = BroadcastProgressDialog(self, title, len(targets))
        self._broadcast_dlg = dlg

        # One id for this announcement, generated here and carried by
        # every board, every acknowledgment and the history row alike.
        announcement_id = new_announcement_id()
        self._ack_tracker.record_sent(announcement_id, title, message, targets)

        worker = BroadcastWorker(
            targets, title, message, self.profile,
            announcement_id=announcement_id,
            teacher_device_id=self._ack_identity.device_id,
            ack_port=self._ack_server.port or TEACHER_ACK_PORT,
        )
        worker.board_done.connect(dlg.on_board_done)
        worker.progress.connect(dlg.on_progress)
        worker.finished_all.connect(dlg.on_finished)
        worker.delivery.connect(
            lambda ip, ok, detail, info, aid=announcement_id:
            self._ack_tracker.record_delivery(aid, ip, ok, detail, info))
        self._broadcast_worker = worker
        worker.start()

        # Record in history (intent + recipients), tied to the same id so the
        # history row can open the acknowledgment report.
        add_history_entry(title, message, targets,
                          announcement_id=announcement_id)

        dlg.exec()
        worker.wait()
        self._broadcast_worker = None
        self._broadcast_dlg = None
        dlg.deleteLater()

    # ------------------------------------------------------ acknowledgments
    def _greet_board(self, ip):
        """Tell a board where to send acknowledgments for this device.

        This is what makes an offline teacher recoverable without polling.
        A board holding an undelivered acknowledgment cannot know this laptop
        is back; it would have to keep probing an address that was wrong in
        the first place. Instead we announce ourselves the moment we see the
        board, and the board flushes whatever it was holding for us.

        Fire-and-forget on a worker thread: discovery runs on the Zeroconf
        thread and must not be blocked by a board that is slow to answer.
        """
        if not ip or ip in self._greeted_boards:
            return
        self._greeted_boards.add(ip)
        payload = {
            "teacher_device_id": self._ack_identity.device_id,
            "ack_port": self._ack_server.port or TEACHER_ACK_PORT,
            "ack_version": ACK_PROTOCOL_VERSION,
        }

        def _hello():
            try:
                r = requests.post(f"http://{ip}:16384/teacher-hello",
                                  json=payload, timeout=(3, 6))
                if r.status_code == 200:
                    body = r.json() if r.content else {}
                    pending = body.get("pending_acks", 0)
                    if pending:
                        log.info(f"Board {ip} was holding {pending} "
                                 f"acknowledgment(s) for us — flushing")
                    else:
                        log.debug(f"Greeted board {ip}")
                else:
                    # An older board has no such route. Not an error: it
                    # simply predates acknowledgments.
                    log.debug(f"Board {ip} did not accept hello "
                              f"(HTTP {r.status_code}) — older build")
            except Exception as e:
                log.debug(f"Hello to {ip} failed: {e}")
                # Allow a retry next time this board is seen.
                self._greeted_boards.discard(ip)

        threading.Thread(target=_hello, daemon=True,
                         name=f"LS-hello-{ip}").start()

    def _on_ack_timeout(self, announcement_id, boards):
        """Five minutes elapsed with boards still unacknowledged."""
        ann = self._ack_tracker.get(announcement_id)
        title = ann.title if ann else "Announcement"
        self._ack_notifier.notify_timeout(announcement_id, title, boards)

    def _open_announcement_report(self, announcement_id):
        """Open the report for a specific announcement.

        Keyed by announcement id, never by "the latest" — a teacher who sent
        several announcements in one lesson must land on the one the
        notification was about.
        """
        if not announcement_id:
            return
        # No parent when we are hidden in the tray. A child of a hidden window
        # is itself hidden on Windows, so parenting it here would answer the
        # notification click with nothing on screen — which is precisely the
        # case the notification exists for.
        dlg = AnnouncementReportDialog(self._ack_tracker, announcement_id,
                                       parent=self if self.isVisible() else None)
        dlg.show(); dlg.raise_(); dlg.activateWindow()
        self._open_reports.append(dlg)
        dlg.destroyed.connect(
            lambda *_: self._open_reports.remove(dlg)
            if dlg in self._open_reports else None)

    def _shutdown_ack(self):
        """Stop the listener and the sweep at app exit."""
        try:
            self._ack_tracker.stop_sweeping()
        except Exception:
            pass
        try:
            self._ack_server.stop()
        except Exception:
            pass

    def _open_history_window(self):
        """Open announcement history from the main window.

        Separate from the composer's copy on purpose: reaching History should
        not require being part-way through writing a new announcement, which
        is the only route that existed before.
        """
        dlg = AnnouncementHistoryDialog(self.profile, self,
                                        tracker=self._ack_tracker)
        dlg.exec()
        dlg.deleteLater()

    def _clamp_to_screen(self):
        """Shrink to fit the work area if the scaled default would overflow.

        UI_SCALE is derived from screen HEIGHT, so the window and the space it
        has to live in usually move together — but not always: a very wide,
        very short display (a 1920x720 stretch panel, or a board with a big
        docked taskbar) scales the window up while leaving less room for it.
        Clamping to availableGeometry costs one call and turns "title bar off
        the top of the screen, buttons below the bottom" into a window that
        simply fits.
        """
        try:
            scr = self.screen() or QApplication.primaryScreen()
            if scr is None:
                return
            avail = scr.availableGeometry()
            w = min(self.width(),  int(avail.width()  * 0.95))
            h = min(self.height(), int(avail.height() * 0.95))
            if w != self.width() or h != self.height():
                # setGeometry, NOT resize. _init_scaling monkeypatches
                # QWidget.resize to multiply by UI_SCALE, and these values are
                # already scaled — they came out of width()/height(). Going
                # through resize() would scale them a second time and shrink
                # the window by the scale factor on every single launch.
                # setGeometry is not patched, so it takes real pixels.
                self.setGeometry(self.x(), self.y(), w, h)
                log.info(f"Window clamped to fit screen: {w}x{h}")
        except Exception:
            log.debug("Could not clamp window to screen", exc_info=True)

    def _edit_profile(self):
        dlg = TeacherProfileDialog(self, existing=self.profile)
        accepted = dlg.exec() == QDialog.DialogCode.Accepted
        dlg.deleteLater()
        if accepted:
            self.profile = load_profile() or self.profile
            teacher = f"{self.profile['first_name']} {self.profile['surname']} {self.profile['title']}".strip()
            self.teacher_lbl.setText(teacher)
            log.info("Profile updated")


# ============================================================================
if __name__ == "__main__":
    # Global exception hook: PyQt can otherwise swallow exceptions raised
    # inside slots, and a hard crash would leave no trace. This logs every
    # uncaught exception with a full stack trace so classroom failures can be
    # diagnosed from logicshare.log afterwards.
    def _excepthook(exc_type, exc_value, exc_tb):
        log.critical("Uncaught exception", exc_info=(exc_type, exc_value, exc_tb))
        sys.__excepthook__(exc_type, exc_value, exc_tb)
    sys.excepthook = _excepthook

    # The elevated half of "Allow network access": LogicShare relaunched by
    # itself through UAC to write its firewall rules. No UI, no single-
    # instance check — write the rules and leave.
    if "--grant-firewall-access" in sys.argv[1:]:
        _i = sys.argv.index("--grant-firewall-access")
        _rule = sys.argv[_i + 1] if _i + 1 < len(sys.argv) else "LogicShare"
        log.info("Elevated firewall grant for rule %r", _rule)
        sys.exit(firewall.run_elevated_grant(_rule))

    log.info("=" * 60)
    log.info("LogicShare starting up")

    # Tell Windows this process is its own application, not python.exe.
    # Must happen BEFORE the first window is created.
    _set_windows_app_user_model_id()

    try:
        QApplication.setHighDpiScaleFactorRoundingPolicy(
            Qt.HighDpiScaleFactorRoundingPolicy.PassThrough
        )
    except Exception:
        log.debug("Could not set HiDPI rounding policy", exc_info=True)

    qt_app = QApplication(sys.argv)
    # Closing the window puts LogicShare in the tray; it does not end it. Qt's
    # default would quit the process the moment the last window went away,
    # which is the one thing the acknowledgment warning cannot survive — it
    # fires five minutes after the announcement, long after a teacher has
    # closed the window and moved on. Every real exit goes through
    # LogicShareUI (tray → Quit, or the no-tray close path), and both call
    # quit() themselves.
    qt_app.setQuitOnLastWindowClosed(False)

    # ── One LogicShare per teacher ──────────────────────────────────────────
    # Closing the window only hides it into the tray, so launching LogicShare
    # again from the Start menu used to start a SECOND, fully independent
    # process: two tray icons, two ack listeners fighting over one port, two
    # Zeroconf browsers — and the window the teacher could see was not the
    # one holding their pending acknowledgments. Now a second launch tells
    # the first to show itself and leaves, before it has built any UI.
    # Decided here — after QApplication (the pipe needs Qt), before the
    # profile dialog, scaling, Zeroconf or the update check.
    _instance = single_instance.SingleInstance()
    if not _instance.is_primary:
        if _instance.hand_off():
            log.info("Handed off to the running LogicShare — exiting")
            sys.exit(0)
        # It holds the claim but did not answer within the deadline: hung,
        # or stuck in something synchronous. Starting a second copy anyway
        # would bring back every problem this exists to prevent, so say so
        # and leave the decision with the teacher.
        log.error("A LogicShare process holds the instance but did not answer")
        _init_scaling(qt_app)
        CustomMessageBox.warning(
            None, "LogicShare",
            "LogicShare is already running but isn't responding.\n\n"
            "Give it a moment and try again, or end LogicShare from "
            "Task Manager and reopen it.")
        sys.exit(1)

    # Full application identity so Qt reports the right metadata to the OS
    # (used by Windows for the taskbar tooltip and Alt-Tab title).
    qt_app.setApplicationName("LogicShare")
    qt_app.setApplicationDisplayName("LogicShare")
    qt_app.setApplicationVersion(APP_VERSION)
    qt_app.setOrganizationName("InnovaLogiX")
    qt_app.setOrganizationDomain("innovalogix.local")
    qt_app.setDesktopFileName("LogicShare")   # ignored on Windows, used on Linux

    # Generate the .ico from the SVG once (cached on disk). Done AFTER
    # QApplication construction because logo_pixmap() needs Qt initialised
    # to render the SVG, but BEFORE any window so Windows sees the right
    # icon from the very first frame.
    _ensure_windows_ico_from_svg()
    qt_app.setWindowIcon(app_icon())

    # Compute + install resolution-based UI scaling BEFORE any window or
    # dialog is constructed (so every size/stylesheet gets scaled).
    _init_scaling(qt_app)

    profile = load_profile()
    if profile is None:
        log.info("No profile found — showing setup dialog")
        dlg = TeacherProfileDialog()
        accepted = dlg.exec() == QDialog.DialogCode.Accepted
        dlg.deleteLater()
        if not accepted:
            log.info("Setup cancelled — exiting")
            sys.exit(0)
        profile = load_profile()
        if profile is None:
            log.error("Profile could not be saved — exiting")
            CustomMessageBox.critical(None, "LogicShare", "Could not save profile. Exiting.")
            sys.exit(1)

    log.info(f"Profile loaded: {profile.get('short_name', '?')}")
    window = LogicShareUI(profile)
    window.setWindowIcon(app_icon())
    window.show()

    def _show_requested():
        """Another launch asked for us. Bring the window back from the tray
        (or from behind other windows), and if a dialog is open on top of it
        — the announcement composer, an upload — surface that too, since it
        is what the teacher was in the middle of."""
        single_instance.bring_forward(window)
        modal = QApplication.activeModalWidget()
        if modal is not None and modal is not window:
            single_instance.bring_forward(modal)
    _instance.set_show_handler(_show_requested)

    # Look for a new release a few seconds in — long enough that the network
    # request never competes with the first paint or with Zeroconf finding
    # boards, which is what the teacher is actually waiting on. Silent, so it
    # surfaces only when there is genuinely something to install.
    QTimer.singleShot(5000, lambda: check_for_updates(window, silent=True))

    # Installers that already did their job. Swept now, and once more two
    # minutes in: when this launch IS the installer's "Launch LogicShare",
    # the installer process can still be exiting and hold its own exe open,
    # so the first attempt fails on exactly the file it most needs to delete.
    prune_installed_updates()
    QTimer.singleShot(120_000, prune_installed_updates)

    log.info("LogicShare ready")
    exit_code = qt_app.exec()
    log.info(f"LogicShare exited (code {exit_code})")
    sys.exit(exit_code)