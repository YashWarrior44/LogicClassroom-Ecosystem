"""
aero.py — Windows Aero glass-effect helper for LogicBoard.

The module was originally called ``acrylic.py``, from an early attempt to use
the Windows Acrylic backdrop. That is not what the app ships: ``EFFECT`` below
is ``"aero"``, the focus-independent Gaussian blur, because Windows only draws
Acrylic on the ACTIVE window and LogicBoard's toolbar and panels are floating
windows that rarely hold focus. The file, its functions and its theme tokens
are named for the effect that actually renders. "Acrylic" survives in here only
where it names a real Win32 constant or pywinstyles style — those are OS
vocabulary, not ours.

Public API
----------
apply_aero(widget, tint_abgr=None)
    Applies the best available Windows backdrop effect to a widget:
    • pywinstyles ‘mica’ / ‘aero’  (if installed — recommended)
    • Windows 11 (build ≥ 22000): DWM SYSTEMBACKDROP_TYPE
    • Windows 10 (build ≥ 17763): SetWindowCompositionAttribute blur
    • Other platforms / older Windows: no-op (app still works, just no glass)

apply_dark_titlebar(widget)
    Requests a dark-mode title bar from DWM (Windows 10/11).

enable_shadow(widget)
    Ensures the DWM drop-shadow is present (often lost on custom-frame windows).

prepare_aero_widget(widget)
    Sets WA_TranslucentBackground BEFORE the first show so the Win32 aero
    blur has a transparent Qt surface to render through, and installs the
    scrim / hit-catcher every translucent window needs.

Usage
-----
    from ui.util.aero import apply_aero, prepare_aero_widget
    ...
    def __init__(self, parent):
        super().__init__(parent, Qt.FramelessWindowHint | Qt.Popup)
        prepare_aero_widget(self)           # BEFORE show()
        ...
    def showEvent(self, e):
        super().showEvent(e)
        apply_aero(self)                    # AFTER show() — HWND now exists
"""
from __future__ import annotations

import sys
import logging

__all__ = [
    "apply_acrylic", "apply_aero", "apply_dark_titlebar", "enable_shadow",
    "apply_rounded_corners", "install_hit_catcher",
    "prepare_acrylic_widget", "prepare_aero_widget", "have_pywinstyles",
]

logger = logging.getLogger(__name__)

# ---------------------------------------------------------------------------
# EFFECT — the backdrop style for the whole app. Change this one value.
#
#   "mica"    → always visible, even on inactive windows. Softer, tints from
#               the desktop wallpaper. RECOMMENDED for floating toolbar/panels
#               because they rarely hold focus.
#   "acrylic" → live blur, but Windows 11 ONLY draws acrylic on the ACTIVE
#               window. So when effect == "acrylic" we call activateWindow()
#               on each widget as its acrylic is applied, to force it active.
#
# NOTE (hard OS limit): Windows has exactly ONE active window per desktop.
# You cannot blur two windows with acrylic at the same time — activating the
# panel necessarily deactivates the toolbar, which makes the toolbar's acrylic
# go opaque. This is a Win32 rule, not a bug. Use "mica" if you need both the
# toolbar and an open panel to show their backdrop simultaneously.
#   "aero"    → plain Gaussian (Win7-style) blur via the WCA path. Focus-
#               independent. Deprecated separately from acrylic, so it MAY
#               still render on builds where acrylic-blur was disabled. No
#               frosted texture or wallpaper tint — just a flat blur.
#   "none"    → NO backdrop effect. What ships.
#
# The blur was scrapped. Every option above makes a panel's look depend on
# whatever happens to be behind it — fine over the dark canvas, a grey smear
# over a white page or a bright slide — and keeping text legible on it took
# a stack of compensating tints. The panels are SOLID now: the surface under
# every floating window is painted by Qt (see install_hit_catcher /
# _SurfaceFrame) in the neutral greys from tokens.py, so they look the same
# over anything. The other effects stay selectable here only so the
# behaviour is one switch away if it is ever wanted back.
# ---------------------------------------------------------------------------
EFFECT = "none"

# ---------------------------------------------------------------------------
# pywinstyles — preferred path. Same Win32 APIs underneath; the wrapper is
# well-maintained and handles edge cases (build detection, error paths) for us.
# ---------------------------------------------------------------------------

_HAS_PYWINSTYLES = False
try:
    import pywinstyles                   # type: ignore
    _HAS_PYWINSTYLES = True
    logger.debug("pywinstyles loaded from %s", pywinstyles.__file__)
except ImportError as _e:
    pywinstyles = None                   # type: ignore
    logger.debug("pywinstyles not installed (%s); glass effects disabled", _e)


def have_pywinstyles() -> bool:
    """True iff the pywinstyles library is installed in the current env."""
    return _HAS_PYWINSTYLES


def _apply_aero_via_pywinstyles(widget) -> bool:
    """Use pywinstyles.apply_style. Returns True on success.

    Fallback order — only style names that ACTUALLY EXIST in pywinstyles
    1.8 (see ValueError listing):
        ['dark', 'mica', 'aero', 'transparent', 'acrylic',
         'win7', 'inverse', 'popup', 'native', 'optimised',
         'light', 'normal']

    When EFFECT is ``acrylic`` we try that style first — DWM
    SystemBackdropType = 3 = TransientWindow / Acrylic.
    Mica is a softer fallback (SystemBackdropType = 2) for older
    Win11 builds where Acrylic isn't supported on a per-window basis.
    Aero is the ancient pre-Win11 backdrop blur.

    A previous version tried 'mica-alt' first — that style name does
    NOT exist in pywinstyles 1.8 and raised ValueError every time
    before falling through. Removed.
    """
    if not _HAS_PYWINSTYLES:
        return False
    # Honour the EFFECT switch: mica first when mica is chosen, acrylic first
    # when acrylic is chosen. Remaining styles stay as graceful fallbacks for
    # older Win11 builds where the preferred one isn't supported per-window.
    if EFFECT == "acrylic":
        order = ("acrylic", "mica", "aero")
    else:
        order = ("mica", "acrylic", "aero")
    try:
        for style in order:
            try:
                pywinstyles.apply_style(widget, style)
                logger.debug("pywinstyles: applied %s", style)
                return True
            except Exception as e:
                logger.debug("pywinstyles: %s failed (%s); trying next", style, e)
        return False
    except Exception as e:
        logger.warning("pywinstyles error: %s", e)
        return False


# ---------------------------------------------------------------------------
# Internal: Windows detection + ctypes setup (fallback path)
# ---------------------------------------------------------------------------

_IS_WIN = sys.platform == "win32"
_WIN_BUILD = 0

if _IS_WIN:
    try:
        import ctypes
        import ctypes.wintypes as _wt
        import platform as _plat

        _ver = _plat.version().split(".")
        _WIN_BUILD = int(_ver[2]) if len(_ver) >= 3 else 0

        _user32 = ctypes.WinDLL("user32")
        _dwmapi = ctypes.WinDLL("dwmapi")
    except Exception as _e:
        logger.debug("Aero: failed to load Win32 DLLs: %s", _e)
        _IS_WIN = False


# ---------------------------------------------------------------------------
# Windows 10: SetWindowCompositionAttribute  (build ≥ 17763 = 1809)
# ---------------------------------------------------------------------------

def _aero_win10(hwnd: int, abgr_color: int) -> bool:
    """Aero/acrylic blur via SetWindowCompositionAttribute (WCA).

    Mirrors pywinstyles' working sequence exactly, because that's verified to
    render on this build:
      1. a PRIMING WCA call (attribute 30, state 2) that wakes the compositor
      2. the actual blur WCA call (attribute 19) with state 3 (aero) / 4
    Flags are left at 0 (NOT host-backdrop 0x20 — that bit breaks the
    deprecated path on current builds). The window background must already be
    painted opaque black by the caller — blur composites against that base.
    """
    try:
        ACCENT_ENABLE_BLURBEHIND        = 3   # aero (plain gaussian)
        ACCENT_ENABLE_ACRYLICBLURBEHIND = 4   # frosted acrylic

        class _Accent(ctypes.Structure):
            _fields_ = [
                ("AccentState",   ctypes.wintypes.DWORD),
                ("AccentFlags",   ctypes.wintypes.DWORD),
                ("GradientColor", ctypes.wintypes.DWORD),
                ("AnimationId",   ctypes.wintypes.DWORD),
            ]

        class _WCA_DATA(ctypes.Structure):
            _fields_ = [
                ("Attribute",  ctypes.wintypes.DWORD),
                ("Data",       ctypes.POINTER(_Accent)),
                ("SizeOfData", ctypes.c_size_t),
            ]

        fn = _user32.SetWindowCompositionAttribute

        def _wca(attribute: int, state: int, color: int | None = None) -> int:
            accent = _Accent()
            accent.AccentState = state
            if color is not None:
                accent.GradientColor = color
            data = _WCA_DATA()
            data.Attribute  = attribute
            data.Data       = ctypes.pointer(accent)
            data.SizeOfData = ctypes.sizeof(accent)
            return fn(hwnd, ctypes.byref(data))

        # Prime the compositor first. This call is not decoration: on a
        # window that has never had an accent policy set, the first real
        # request is intermittently ignored, and priming makes it reliable.
        _wca(30, 2)

        if EFFECT == "aero":
            # Plain Gaussian blur, no tint. Retained only as the fallback for
            # builds where acrylic is unavailable.
            r = _wca(19, ACCENT_ENABLE_BLURBEHIND, color=abgr_color)
        else:
            # Real acrylic, tinted by the CALLER's colour rather than a
            # hardcoded constant.
            #
            # Both branches used to throw the caller's tint away — aero
            # passed 0x000000 and acrylic passed 0x292929, and BOTH of those
            # have an alpha byte of 0x00, i.e. no tint whatsoever. So the
            # compositor blurred the backdrop and then tinted it with
            # nothing, and every panel over a white page came out white.
            # That single detail is the whole "panels look awful over a white
            # background" bug; the tint now comes from
            # tokens.acrylic_win32_abgr, whose alpha is 0xC0.
            #
            # DwmExtendFrameIntoClientArea(-1,-1,-1,-1) is deliberately NOT
            # called. pywinstyles does it, but on a WS_EX_LAYERED window
            # (which WA_TranslucentBackground forces) extending the frame
            # over the whole client area fights the per-pixel alpha and can
            # leave the backdrop rendering inconsistently between shows.
            r = _wca(19, ACCENT_ENABLE_ACRYLICBLURBEHIND, color=abgr_color)

        logger.debug("WCA sequence done, last call -> %s (1=ok, 0=fail), state=%s",
                     r, 'aero(3)' if EFFECT == 'aero' else 'acrylic(4)')
        return bool(r)
    except Exception as e:
        logger.debug("Aero Win10/WCA: %s", e)
        return False


# ---------------------------------------------------------------------------
# Windows 11: DwmSetWindowAttribute SYSTEMBACKDROP_TYPE  (build ≥ 22000)
# ---------------------------------------------------------------------------

def _backdrop_win11(hwnd: int) -> bool:
    """Apply the EFFECT backdrop via DwmSetWindowAttribute SYSTEMBACKDROP_TYPE.

    Mica (2) when EFFECT == "mica" — draws on inactive windows too.
    Acrylic (3) when EFFECT == "acrylic" — active window only (see apply_aero).
    """
    try:
        DWMWA_SYSTEMBACKDROP_TYPE = 38
        DWMSBT_MAINWINDOW         = 2    # Mica
        DWMSBT_TRANSIENTWINDOW    = 3    # Acrylic

        backdrop = DWMSBT_TRANSIENTWINDOW if EFFECT == "acrylic" else DWMSBT_MAINWINDOW

        fn = _dwmapi.DwmSetWindowAttribute
        fn.restype  = ctypes.c_long
        fn.argtypes = [
            ctypes.wintypes.HWND,
            ctypes.c_uint,
            ctypes.c_void_p,
            ctypes.c_uint,
        ]
        val = ctypes.c_int(backdrop)
        fn(hwnd, DWMWA_SYSTEMBACKDROP_TYPE,
           ctypes.byref(val), ctypes.sizeof(val))
        return True
    except Exception as e:
        logger.debug("Backdrop Win11: %s", e)
        return False


# ---------------------------------------------------------------------------
# Dark titlebar
# ---------------------------------------------------------------------------

def _set_dark_titlebar(hwnd: int, dark: bool) -> None:
    try:
        DWMWA_USE_IMMERSIVE_DARK_MODE = 20
        fn = _dwmapi.DwmSetWindowAttribute
        val = ctypes.c_int(1 if dark else 0)
        fn(hwnd, DWMWA_USE_IMMERSIVE_DARK_MODE,
           ctypes.byref(val), ctypes.sizeof(val))
    except Exception as e:
        logger.debug("DarkTitleBar: %s", e)


# ---------------------------------------------------------------------------
# Drop shadow
# ---------------------------------------------------------------------------

def _enable_shadow(hwnd: int) -> None:
    try:
        DWMWA_NCRENDERING_POLICY = 2
        DWMNCRP_ENABLED          = 2
        fn = _dwmapi.DwmSetWindowAttribute
        val = ctypes.c_int(DWMNCRP_ENABLED)
        fn(hwnd, DWMWA_NCRENDERING_POLICY,
           ctypes.byref(val), ctypes.sizeof(val))
    except Exception as e:
        logger.debug("Shadow: %s", e)


# ---------------------------------------------------------------------------
# Public helpers — safe on any OS
# ---------------------------------------------------------------------------

def apply_acrylic(widget, tint_abgr: int | None = None) -> None:
    """Apply the acrylic backdrop to a Qt widget that has a native HWND.

    Must be called AFTER the widget is shown (so its HWND is valid).
    The widget must have ``WA_TranslucentBackground`` set so the compositor
    can see through the Qt surface. Call ``prepare_aero_widget()`` to
    handle that flag before showing.
    """
    if not _IS_WIN:
        return
    if EFFECT == "none":
        # No backdrop. What the old path did besides blurring is still
        # wanted: a dark title bar and DWM-rounded corners.
        try:
            hwnd = int(widget.winId())
            _set_dark_titlebar(hwnd, True)
            apply_rounded_corners(widget, mode="round")
        except Exception as e:
            logger.debug("window chrome: %s", e)
        return
    try:
        from ui.theme.theme_manager import theme_mgr
        if tint_abgr is None:
            tok = theme_mgr.tokens
            # Prefer the explicit acrylic name; fall back to the legacy one so
            # a theme that hasn't been updated still gets a working tint.
            tint_abgr = getattr(tok, "acrylic_win32_abgr",
                                tok.aero_win32_abgr)
        hwnd = int(widget.winId())

        if EFFECT in ("acrylic", "aero"):
            # Match pywinstyles' working path: ensure WA_TranslucentBackground
            # and a transparent root stylesheet (its paint() does this for Qt),
            # THEN run the primed WCA sequence. Focus-independent on this build.
            try:
                from PySide6.QtCore import Qt
                widget.setAttribute(Qt.WA_TranslucentBackground, True)
            except Exception as e:
                logger.debug("WA_TranslucentBackground: %s", e)
            logger.debug("EFFECT=%s _WIN_BUILD=%s hwnd=%s", EFFECT, _WIN_BUILD, hwnd)
            if _WIN_BUILD >= 17763:
                ok = _aero_win10(hwnd, tint_abgr)
                logger.debug("_aero_win10 returned %s", ok)
            else:
                logger.debug("aero SKIPPED - _WIN_BUILD %s < 17763", _WIN_BUILD)
        else:
            # Mica — the one remaining path that is not hand-rolled.
            # Everything else goes through SetWindowCompositionAttribute
            # above, directly, because pywinstyles' presets hardcode
            # zero-alpha tints and give no way to pass one in. That is the
            # bug this module was rewritten to escape, so the library is not
            # consulted for acrylic or aero any more.
            if _WIN_BUILD >= 22000:
                _backdrop_win11(hwnd)

        _set_dark_titlebar(hwnd, True)
        # Round the actual window corners. With WA_TranslucentBackground + aero,
        # DWM squares the corners off unless we explicitly request rounding, so
        # the CSS border-radius alone isn't enough — do it here so every panel
        # that applies aero also gets rounded corners automatically.
        try:
            apply_rounded_corners(widget, mode="round")
        except Exception as e:
            logger.debug("auto rounded corners: %s", e)
    except Exception as e:
        logger.warning("apply_aero failed: %s", e)


# ---------------------------------------------------------------------------
# Legacy names.
#
# The effect is acrylic now, but ``apply_aero`` / ``prepare_aero_widget`` are
# called from 76 places across 20 modules. Renaming all of them in the same
# change as switching the effect would mean a large mechanical diff sitting on
# top of a behavioural one, with no way to tell which caused a regression.
# The aliases keep those call sites working and cost nothing.
# ---------------------------------------------------------------------------
def apply_aero(widget, tint_abgr: int | None = None) -> None:
    """Deprecated alias for :func:`apply_acrylic`."""
    apply_acrylic(widget, tint_abgr)


def apply_dark_titlebar(widget) -> None:
    """Request a dark-mode title bar from DWM."""
    if not _IS_WIN:
        return
    try:
        _set_dark_titlebar(int(widget.winId()), True)
    except Exception as e:
        logger.debug("apply_dark_titlebar: %s", e)


def enable_shadow(widget, *, border=None) -> None:
    """Ensure DWM drop-shadow is active (useful after FramelessWindowHint).

    ``border`` controls the window border that comes with the shadow, and
    defaults to None = no border.

    That default is deliberate. Turning on non-client rendering to get the
    shadow ALSO makes Windows 11 paint its own 1 px window border, in a fixed
    system grey. It can't adapt, so it reads as a hard bright line over a
    white background and a hard bright line over the dark canvas — the
    "borders highlight themselves everywhere" problem. It is not a Qt border,
    which is why restyling QSS never moved it.

    Pass a QColor to keep a border in a chosen colour instead. The drop shadow
    is unaffected either way and still separates the panel from the canvas.
    """
    if not _IS_WIN:
        return
    try:
        _enable_shadow(int(widget.winId()))
    except Exception as e:
        logger.debug("enable_shadow: %s", e)
    # Must run AFTER _enable_shadow — that call is what creates the border.
    set_window_border(widget, border)


def set_window_border(widget, color=None) -> bool:
    """Control the DWM window border on a top-level widget.

    ``color=None`` removes it; a QColor sets it explicitly.

    This is the border you actually see around every floating surface, and it
    is NOT a Qt border — which is why restyling QSS never changed it. It comes
    from ``enable_shadow``: that turns on DWMWA_NCRENDERING_POLICY to get the
    drop shadow, and with non-client rendering enabled Windows 11 also paints
    its own 1 px window border. The colour is a fixed system grey, so it sits
    at roughly the same brightness whatever is behind it — which is exactly
    the "borders highlight themselves over a white background" problem. Making
    it translucent isn't possible; DWM has one flat colour or nothing.

    Uses DWMWA_BORDER_COLOR (attribute 34), Windows 11 build 22000+. Older
    builds and non-Windows: graceful no-op, returns False.

    Call AFTER the widget is shown (needs a valid HWND) and after
    enable_shadow(), which is what creates the border in the first place.
    """
    if not _IS_WIN or _WIN_BUILD < 22000:
        return False
    try:
        import ctypes
        DWMWA_BORDER_COLOR = 34
        DWMWA_COLOR_NONE   = 0xFFFFFFFE
        if color is None:
            value = DWMWA_COLOR_NONE
        else:
            # COLORREF is 0x00BBGGRR — byte order is reversed from RGB.
            value = (int(color.blue()) << 16 | int(color.green()) << 8
                     | int(color.red()))
        val = ctypes.c_uint(value)
        hr = _dwmapi.DwmSetWindowAttribute(
            int(widget.winId()), DWMWA_BORDER_COLOR,
            ctypes.byref(val), ctypes.sizeof(val))
        ok = (hr == 0)
        if not ok:
            logger.debug("set_window_border: HRESULT 0x%08X", hr & 0xFFFFFFFF)
        return ok
    except Exception as e:
        logger.debug("set_window_border: %s", e)
        return False


def apply_rounded_corners(widget, *, mode: str = "round") -> None:
    """Apply Windows 11 native rounded corners to a top-level widget.

    Uses ``DwmSetWindowAttribute`` with ``DWMWA_WINDOW_CORNER_PREFERENCE``
    (attribute 33). The compositor masks the window — INCLUDING its aero
    backdrop — to the rounded shape. No aliased ``setMask`` regions, no
    custom paintEvent: the GPU does it natively.

    Requires Windows 11 build 22000+. Older builds: graceful no-op (the
    window stays rectangular, but the blur still applies).

    Modes:
      "round"       → DWMWCP_ROUND       (~8 px Fluent radius)
      "round_small" → DWMWCP_ROUNDSMALL  (~4 px tighter radius)
      "square"      → DWMWCP_DONOTROUND  (force square)
    """
    if not _IS_WIN or _WIN_BUILD < 22000:
        return
    try:
        import ctypes
        DWMWA_WINDOW_CORNER_PREFERENCE = 33
        VALUES = {
            "round":       2,   # DWMWCP_ROUND
            "round_small": 3,   # DWMWCP_ROUNDSMALL
            "square":      1,   # DWMWCP_DONOTROUND
        }
        value = ctypes.c_int(VALUES.get(mode, 2))
        hwnd  = int(widget.winId())
        _dwmapi.DwmSetWindowAttribute(
            hwnd,
            DWMWA_WINDOW_CORNER_PREFERENCE,
            ctypes.byref(value),
            ctypes.sizeof(value),
        )
    except Exception as e:
        logger.debug("apply_rounded_corners: %s", e)


def _make_catcher_resizer(widget, frame):
    """Event filter keeping the hit-catcher the size of its host window.

    Built lazily inside a function because this module is imported before
    any QApplication exists on some paths, and it must be a real QObject —
    ``installEventFilter`` silently rejects a plain Python object.
    """
    from PySide6.QtCore import QEvent, QObject

    class _CatcherResizer(QObject):
        def eventFilter(self, obj, event):
            # Resize and Show are the two that move the goalposts, but
            # PolishRequest and LayoutRequest are in here for the case that
            # actually bit: a window built at its default size, laid out on
            # first polish, and never resized again. Its Show fires with the
            # OLD rect on some paths, leaving the catcher covering a fraction
            # of the client area and the rest of the panel still a hole.
            # Re-syncing on every one of them is a setGeometry on a frame —
            # cheap enough not to need to be clever about it.
            if event.type() in (QEvent.Type.Resize,
                                QEvent.Type.Show,
                                QEvent.Type.PolishRequest,
                                QEvent.Type.LayoutRequest):
                try:
                    if frame.geometry() != obj.rect():
                        frame.setGeometry(obj.rect())
                    frame.lower()
                except Exception:
                    pass
            return False

    return _CatcherResizer(widget)


def _surface_frame_class():
    """The painted surface every floating window sits on.

    Built lazily for the same reason as the resizer above: this module is
    imported before any QApplication exists on some paths.
    """
    global _SurfaceFrame
    if _SurfaceFrame is not None:
        return _SurfaceFrame
    from PySide6.QtCore import Qt, QRectF
    from PySide6.QtGui import QPainter, QPainterPath, QLinearGradient, QPen, QBrush
    from PySide6.QtWidgets import QFrame

    class SurfaceFrame(QFrame):
        """A solid, rounded panel body: a soft top-to-bottom fall-off and a
        hairline edge that is brighter along the top — lit from above, the
        way iOS surfaces read.

        Painted rather than styled: a QSS ``border`` with a ``border-radius``
        composites its corners twice (see ui/util/rounded_border.py), and a
        stylesheet cannot grade a stroke from top to bottom.
        """

        def __init__(self, host, radius):
            super().__init__(host)
            self._radius = radius

        def set_radius(self, radius):
            self._radius = radius
            self.update()

        def corner_radius(self) -> float:
            r = self._radius
            if r == "pill":
                return min(self.width(), self.height()) / 2.0
            if r is None:
                from ui.theme.theme_manager import theme_mgr
                from ui.util.scaling import UiScale
                return float(UiScale.px(theme_mgr.tokens.radius_xl))
            return float(r)

        def paintEvent(self, _event):
            from ui.theme.theme_manager import theme_mgr
            t = theme_mgr.tokens
            q = theme_mgr.qcolor
            p = QPainter(self)
            p.setRenderHint(QPainter.RenderHint.Antialiasing)
            rect = QRectF(self.rect()).adjusted(0.5, 0.5, -0.5, -0.5)
            rad = min(self.corner_radius(), rect.height() / 2, rect.width() / 2)
            path = QPainterPath()
            path.addRoundedRect(rect, rad, rad)
            body = QLinearGradient(0, rect.top(), 0, rect.bottom())
            body.setColorAt(0.0, q(t.surface_top))
            body.setColorAt(1.0, q(t.surface_bottom))
            p.fillPath(path, QBrush(body))
            edge = QLinearGradient(0, rect.top(), 0, rect.bottom())
            edge.setColorAt(0.0, q(t.surface_edge_top))
            edge.setColorAt(1.0, q(t.surface_edge))
            pen = QPen(QBrush(edge), 1.0)
            p.setPen(pen)
            p.setBrush(Qt.BrushStyle.NoBrush)
            p.drawPath(path)

    _SurfaceFrame = SurfaceFrame
    return SurfaceFrame


_SurfaceFrame = None


def install_hit_catcher(widget, radius=None) -> None:
    """Give a floating window its solid surface — and a surface the OS will
    actually hit-test.

    Two jobs, one frame:

    * HIT TESTING. ``WA_TranslucentBackground`` makes Windows create the
      window as ``WS_EX_LAYERED`` with PER-PIXEL alpha, and Windows
      hit-tests layered windows BY ALPHA: a pixel with alpha 0 is not
      merely invisible, it is not there, and the click or tap is delivered
      to whatever window is BEHIND. Without a surface, every gap between a
      panel's controls was a hole, which is why taps fell through panels
      and why scroll areas never saw the drag.

    * THE PANEL'S BODY. This used to be a 1 % film over a Windows blur. The
      blur is gone (see EFFECT), so this is now the panel itself: a solid
      rounded surface in the neutral greys from tokens.py. Every floating
      window gets it from here, which is why none of the sixteen panels had
      to learn to paint a background of its own.

    The window stays translucent so that its CORNERS can be — that is what
    lets the surface be a rounded shape rather than a rectangle.

    ``radius``: None for the standard panel radius (``radius_xl``), a number
    of physical pixels, or ``"pill"`` for a fully rounded end (the toolbar).

    Added as a LOWERED BACKGROUND CHILD rather than by re-parenting each
    panel's layout into it, so no panel needed layout surgery. Idempotent,
    and it tracks the window's size via an event filter.
    """
    from PySide6.QtCore import Qt
    from PySide6.QtWidgets import QFrame

    existing = getattr(widget, "_lb_hit_catcher", None)
    if existing is not None:
        if radius is not None:
            try:
                existing.set_radius(radius)
            except Exception:
                pass
        return

    try:
        frame = _surface_frame_class()(widget, radius)
        frame.setObjectName("LBHitCatcher")
        frame.setFrameShape(QFrame.Shape.NoFrame)
        frame.setFrameShadow(QFrame.Shadow.Plain)
        frame.setLineWidth(0)
        frame.setMidLineWidth(0)
        # Purely a surface: never steal clicks from the real controls, and
        # never take focus. (It still catches the clicks that land on IT —
        # that is the point — they just go nowhere.)
        frame.setFocusPolicy(Qt.FocusPolicy.NoFocus)
        frame.setGeometry(widget.rect())
        frame.lower()

        resizer = _make_catcher_resizer(widget, frame)
        widget.installEventFilter(resizer)
        # Held on the widget so both survive as long as it does.
        widget._lb_hit_catcher = frame
        widget._lb_hit_catcher_resizer = resizer
    except Exception as e:
        logger.debug("install_hit_catcher: %s", e)


def prepare_aero_widget(widget, radius=None) -> None:
    """Call BEFORE show() on a TOP-LEVEL window only.

    Makes the window translucent (so its corners can be rounded) and gives
    it its solid surface — see install_hit_catcher. Doing it in one place
    means every panel gets the same body, including any added later,
    instead of 16 call sites that can drift apart.

    Never call this on child widgets: a child cannot have see-through
    corners, so the surface would just be a square slab inside its parent.

    Applies on every platform now. It used to be Windows-only because the
    translucency existed for the DWM blur; it exists for the rounded
    corners now, and those are drawn by Qt.
    """
    from PySide6.QtCore import Qt
    if not widget.isWindow():
        logger.debug("prepare_aero_widget on a child widget (%s) ignored",
                     type(widget).__name__)
        return
    widget.setAttribute(Qt.WA_TranslucentBackground, True)
    install_hit_catcher(widget, radius)

def prepare_acrylic_widget(widget, radius=None) -> None:
    """Older name for :func:`prepare_aero_widget`."""
    prepare_aero_widget(widget, radius)
