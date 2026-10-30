"""
mica.py — real Windows 11 Mica behind a Qt window, with a solid fallback.

Used by the startup splash and the LogicShare popups.

WHAT IT TOOK (measured, not assumed — see tests/python/test_mica.py)
====================================================================
Mica is drawn by DWM behind the window, from the desktop wallpaper. Three
things decide whether it actually appears:

1. THE WINDOW MUST NOT BE LAYERED. Qt creates a translucent window with
   ``FramelessWindowHint`` as ``WS_EX_LAYERED``, and DWM gives a layered
   window only Mica's flat fallback colour, #202020 — measured identical
   for Mica and Mica Alt, which is how it gives itself away. The same
   window WITHOUT the frameless flag is not layered and gets the real
   material: (26, 31, 49) on the development machine, tinted by its blue
   wallpaper. So a Mica window here is an ORDINARY window whose frame is
   removed by answering WM_NCCALCSIZE with "the client area is the whole
   window" — the technique Fluent-style Qt libraries use.

2. THE CLIENT AREA MUST BE TRANSPARENT where Mica should show: the frame is
   extended over the whole window (DwmExtendFrameIntoClientArea -1) and Qt
   paints alpha 0 there.

3. DWM DRAWS A BACKDROP ONLY FOR A WINDOW THAT LOOKS ACTIVE. Left alone, an
   inactive Mica window shows nothing at all — measured with a red window
   behind it: the red came straight through. Answering WM_NCACTIVATE(FALSE)
   with the ACTIVE appearance keeps the backdrop, so both windows keep their
   Mica when the teacher taps the board behind them (measured: identical
   colour focused and unfocused, no red).

Wherever Mica is unavailable (Windows 10, transparency effects off, Energy
Saver on) the window is built the normal LogicBoard way instead, with its
opaque surface — so it is never left see-through.

Not layered also means Windows hit-tests the whole window, so no clicks
fall through transparent pixels, and DWM rounds the corners and draws the
shadow natively.
"""
from __future__ import annotations

import ctypes
import logging
import sys

logger = logging.getLogger(__name__)

_IS_WIN = sys.platform == "win32"

DWMWA_USE_IMMERSIVE_DARK_MODE = 20
DWMWA_WINDOW_CORNER_PREFERENCE = 33
DWMWA_SYSTEMBACKDROP_TYPE = 38
DWMSBT_MAINWINDOW = 2            # Mica
WM_NCCALCSIZE = 0x0083
WM_NCACTIVATE = 0x0086


def _win_build() -> int:
    if not _IS_WIN:
        return 0
    try:
        return int(sys.getwindowsversion().build)
    except Exception:
        return 0


def _transparency_enabled() -> bool:
    try:
        import winreg
        with winreg.OpenKey(winreg.HKEY_CURRENT_USER,
                            r"Software\Microsoft\Windows\CurrentVersion"
                            r"\Themes\Personalize") as k:
            v, _ = winreg.QueryValueEx(k, "EnableTransparency")
            return bool(v)
    except OSError:
        return True                    # the Windows default


def _energy_saver_on() -> bool:
    """Energy / battery saver turns system backdrops off."""
    try:
        from ctypes import wintypes as wt

        class SPS(ctypes.Structure):
            _fields_ = [("ACLineStatus", wt.BYTE), ("BatteryFlag", wt.BYTE),
                        ("BatteryLifePercent", wt.BYTE),
                        ("SystemStatusFlag", wt.BYTE),
                        ("BatteryLifeTime", wt.DWORD),
                        ("BatteryFullLifeTime", wt.DWORD)]
        s = SPS()
        if ctypes.windll.kernel32.GetSystemPowerStatus(ctypes.byref(s)):
            return bool(s.SystemStatusFlag & 1)
    except Exception:
        pass
    return False


def mica_available() -> bool:
    """Will DWM draw Mica for us right now? (Windows 11 22H2+ for the
    documented backdrop attribute.)"""
    if not _IS_WIN or _win_build() < 22621:
        return False
    try:
        # Offscreen / minimal platforms have no real HWND for DWM to draw on.
        from PySide6.QtGui import QGuiApplication
        if QGuiApplication.platformName() != "windows":
            return False
    except Exception:
        return False
    return _transparency_enabled() and not _energy_saver_on()


# ── the frame remover ───────────────────────────────────────────────────────
_HWNDS: set = set()
_FILTER = None


def _install_filter() -> None:
    """One application-wide native filter that removes the frame of every
    registered Mica window: on WM_NCCALCSIZE it answers "the client area is
    the whole window", which leaves the window an ordinary (non-layered)
    top-level with no title bar or border."""
    global _FILTER
    if _FILTER is not None:
        return
    from PySide6.QtCore import QAbstractNativeEventFilter
    from PySide6.QtWidgets import QApplication
    from ctypes import wintypes as wt

    class _NcFilter(QAbstractNativeEventFilter):
        def nativeEventFilter(self, event_type, message):
            try:
                if event_type != b"windows_generic_MSG" or not _HWNDS:
                    return False, 0
                msg = wt.MSG.from_address(int(message))
                hwnd = int(msg.hWnd or 0)
                if hwnd not in _HWNDS:
                    return False, 0
                if msg.message == WM_NCCALCSIZE and msg.wParam:
                    return True, 0
                if msg.message == WM_NCACTIVATE and not msg.wParam:
                    # "You are no longer the active window." Keep the
                    # ACTIVE appearance: DWM draws a system backdrop only
                    # for a window that looks active, and without this the
                    # popup goes see-through the moment the teacher taps
                    # the board behind it. Qt learns about activation from
                    # WM_ACTIVATE, not this message, so its focus handling
                    # is untouched.
                    _def_window_proc(hwnd, WM_NCACTIVATE, 1, msg.lParam)
                    return True, 1
            except Exception:
                pass
            return False, 0

    _FILTER = _NcFilter()
    QApplication.instance().installNativeEventFilter(_FILTER)


def _def_window_proc(hwnd, msg, wparam, lparam):
    from ctypes import wintypes as wt
    fn = ctypes.windll.user32.DefWindowProcW
    fn.argtypes = [wt.HWND, wt.UINT, wt.WPARAM, wt.LPARAM]
    fn.restype = ctypes.c_ssize_t
    return fn(hwnd, msg, wparam, lparam)


def prepare(widget, radius=None) -> bool:
    """Call BEFORE show(). Returns True if the window will get Mica, False
    if it falls back to LogicBoard's normal solid surface (in which case it
    is set up exactly as prepare_aero_widget would)."""
    from PySide6.QtCore import Qt
    if not mica_available():
        widget.setWindowFlag(Qt.WindowType.FramelessWindowHint, True)
        from ui.util.aero import prepare_aero_widget
        prepare_aero_widget(widget, radius)
        widget._lb_mica = False
        return False
    widget.setWindowFlag(Qt.WindowType.FramelessWindowHint, False)
    widget.setAttribute(Qt.WA_TranslucentBackground, True)
    _install_surface(widget)
    _install_filter()
    widget._lb_mica = True
    return True


def apply(widget) -> None:
    """Call AFTER show() (the HWND must exist)."""
    if not getattr(widget, "_lb_mica", False):
        try:
            from ui.util.aero import apply_aero
            apply_aero(widget)
        except Exception:
            pass
        return
    try:
        from ctypes import wintypes as wt
        hwnd = int(widget.winId())
        _HWNDS.add(hwnd)
        dwm = ctypes.windll.dwmapi
        dwm.DwmSetWindowAttribute.argtypes = [wt.HWND, wt.DWORD,
                                              ctypes.c_void_p, wt.DWORD]

        def attr(a, v):
            val = ctypes.c_int(v)
            return dwm.DwmSetWindowAttribute(hwnd, a, ctypes.byref(val), 4)

        class MARGINS(ctypes.Structure):
            _fields_ = [("l", ctypes.c_int), ("r", ctypes.c_int),
                        ("t", ctypes.c_int), ("b", ctypes.c_int)]

        # No caption buttons: with the frame extended over the whole window
        # DWM draws Close/Min/Max straight onto the Mica if the window has a
        # system menu, which a borderless splash or popup must not show.
        user32 = ctypes.windll.user32
        user32.GetWindowLongW.argtypes = [wt.HWND, ctypes.c_int]
        user32.SetWindowLongW.argtypes = [wt.HWND, ctypes.c_int, ctypes.c_long]
        style = user32.GetWindowLongW(hwnd, -16)                 # GWL_STYLE
        user32.SetWindowLongW(hwnd, -16,
                              style & ~(0x00080000 | 0x00020000 | 0x00010000))
        attr(DWMWA_USE_IMMERSIVE_DARK_MODE, 1)
        attr(DWMWA_WINDOW_CORNER_PREFERENCE, 2)          # round
        m = MARGINS(-1, -1, -1, -1)
        dwm.DwmExtendFrameIntoClientArea(wt.HWND(hwnd), ctypes.byref(m))
        hr = attr(DWMWA_SYSTEMBACKDROP_TYPE, DWMSBT_MAINWINDOW)
        # A defined hairline. Windows 11's own window border is translucent,
        # and over LogicBoard's pure-black canvas it melted into the black,
        # leaving the popup's edge undefined. One opaque neutral grey, the
        # brightness of the panels' top edge.
        try:
            from PySide6.QtGui import QColor
            from ui.util.aero import set_window_border
            set_window_border(widget, QColor(72, 72, 78))
        except Exception:
            pass
        # Re-run WM_NCCALCSIZE now that the filter knows this window, so the
        # frame Windows gave it at creation is dropped.
        ctypes.windll.user32.SetWindowPos(
            wt.HWND(hwnd), None, 0, 0, 0, 0,
            0x0001 | 0x0002 | 0x0004 | 0x0010 | 0x0020)  # NOSIZE|NOMOVE|NOZORDER|NOACTIVATE|FRAMECHANGED
        if hr != 0:
            logger.info("Mica refused (hr=%#x); using the solid surface",
                        hr & 0xFFFFFFFF)
            widget._lb_mica = False
        surf = getattr(widget, "_lb_mica_surface", None)
        if surf is not None:
            surf.update()
    except Exception:
        logger.debug("Mica apply failed", exc_info=True)
        widget._lb_mica = False


def forget(widget) -> None:
    try:
        h = int(widget.winId())
    except Exception:
        return
    _HWNDS.discard(h)


# ── the surface: transparent over Mica, solid when Mica is not there ────────

def _install_surface(widget) -> None:
    from PySide6.QtCore import QEvent, QObject, QRectF, Qt
    from PySide6.QtGui import QBrush, QLinearGradient, QPainter
    from PySide6.QtWidgets import QWidget

    class MicaSurface(QWidget):
        """Lowered background child of a Mica window.

        Active + Mica: paints only a faint top-lit sheen and a hairline, so
        the material is the body. Otherwise: the opaque LogicBoard surface
        (same gradient tokens as every other panel), so the window is never
        see-through.
        """

        def __init__(self, host):
            super().__init__(host)
            self.setAttribute(Qt.WA_TransparentForMouseEvents, True)
            self.setFocusPolicy(Qt.NoFocus)

        def mica_showing(self) -> bool:
            # Mica on means Mica showing, focused or not — measured on both
            # the splash (a tool window) and the announcement (a dialog):
            # with the WM_NCACTIVATE handling above, each keeps its backdrop
            # after losing focus. Not Qt's isActiveWindow(), which reports a
            # parentless tool window as active whenever ANY LogicBoard
            # window is, and was the wrong question anyway.
            return bool(getattr(self.window(), "_lb_mica", False))

        def paintEvent(self, _e):
            from ui.theme.theme_manager import theme_mgr
            t = theme_mgr.tokens
            q = theme_mgr.qcolor
            p = QPainter(self)
            p.setRenderHint(QPainter.Antialiasing)
            r = QRectF(self.rect())
            p.setCompositionMode(QPainter.CompositionMode_Source)
            if self.mica_showing():
                # Completely clear: the Mica IS the body. An earlier cut laid
                # a dark tint and a top sheen over it "for depth", and on the
                # real screen that read as a gradient background with the
                # Mica hidden underneath — exactly what it was. Dark-mode
                # Mica is already dark enough for white text.
                p.fillRect(r, Qt.transparent)
            else:
                body = QLinearGradient(0, 0, 0, r.height())
                body.setColorAt(0.0, q(t.surface_top))
                body.setColorAt(1.0, q(t.surface_bottom))
                p.fillRect(r, QBrush(body))
            p.end()

    class _Tracker(QObject):
        def eventFilter(self, obj, ev):
            et = ev.type()
            if et == QEvent.Resize:
                surf.setGeometry(obj.rect())
            elif et in (QEvent.WindowActivate, QEvent.WindowDeactivate,
                        QEvent.ActivationChange):
                surf.update()
            elif et == QEvent.Close:
                forget(obj)
            return False

    surf = MicaSurface(widget)
    surf.setGeometry(widget.rect())
    surf.lower()
    tracker = _Tracker(widget)
    widget.installEventFilter(tracker)
    widget._lb_mica_surface = surf
    widget._lb_mica_tracker = tracker
