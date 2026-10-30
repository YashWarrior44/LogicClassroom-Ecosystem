"""
test_mica.py — Mica on the splash and the LogicShare popups, and the
solid fallback everywhere Mica cannot be drawn.

Runs under pytest, and also standalone (``python tests/python/test_mica.py``).

Mica itself is drawn by DWM and cannot be seen offscreen; what it NEEDS is
checked here instead — a non-layered window, no frameless flag, the frame
removed via WM_NCCALCSIZE, the active look kept via WM_NCACTIVATE, no
opacity fade — together with the fallback. The on-screen measurement this
is all based on (real Mica (26, 31, 49) behind a non-layered window,
#202020 behind a layered one, see-through when inactive unless the active
look is kept) is recorded in ui/util/mica.py.

The last test DOES use a real window, its own, parked off-screen, to check
the slide-capture GDI rewrite in ppt_renderer: PrintWindow of OUR window,
nothing of the desktop.
"""
import ctypes
import os
import subprocess
import sys
from pathlib import Path

os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")
ROOT = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(ROOT))

from PySide6.QtCore import Qt
from PySide6.QtGui import QIcon
from PySide6.QtWidgets import QApplication

_app = QApplication.instance() or QApplication([])

from ui.util import mica


def _forced(on: bool):
    saved = mica.mica_available
    mica.mica_available = lambda: on
    return saved


def test_offscreen_never_claims_mica():
    assert mica.mica_available() is False


def test_without_mica_the_splash_is_built_the_old_solid_way():
    from ui.app import LogicBoardSplash
    s = LogicBoardSplash(QIcon())
    assert s.windowFlags() & Qt.FramelessWindowHint
    assert getattr(s, "_lb_hit_catcher", None) is not None, \
        "no solid surface on the fallback splash"
    assert s._lb_mica is False


def test_without_mica_the_popup_is_built_the_old_solid_way():
    from ui.logicshare.notifications import AnnouncementNotification
    a = AnnouncementNotification({"short_name": "RS", "first_name": "R"})
    assert a.windowFlags() & Qt.FramelessWindowHint
    assert getattr(a, "_lb_hit_catcher", None) is not None


def test_with_mica_the_windows_are_not_frameless():
    """The frameless flag is what makes Qt create a LAYERED window, and a
    layered window only ever gets Mica's flat #202020 fallback."""
    saved = _forced(True)
    try:
        from ui.app import LogicBoardSplash
        from ui.logicshare.notifications import AnnouncementNotification
        s = LogicBoardSplash(QIcon())
        a = AnnouncementNotification({"short_name": "RS", "first_name": "R"})
        for w in (s, a):
            assert not (w.windowFlags() & Qt.FramelessWindowHint), type(w)
            assert w.testAttribute(Qt.WA_TranslucentBackground)
            assert w._lb_mica is True
            assert getattr(w, "_lb_mica_surface", None) is not None
        # Not a SplashScreen-type window either: Qt makes that layered too.
        assert s.windowType() == Qt.Tool
    finally:
        mica.mica_available = saved


def test_a_mica_popup_does_not_fade_in():
    """windowOpacity makes a window layered — which switches Mica off."""
    import inspect
    from ui.logicshare.notifications import _AeroPopup
    src = inspect.getsource(_AeroPopup.showEvent)
    i = src.index("fade_in(self)")
    assert "if not getattr(self, \"_lb_mica\", False)" in src[:i], \
        "the popup fades in even when it has Mica"


def _msg(hwnd, message, wparam=0, lparam=0):
    from ctypes import wintypes as wt
    m = wt.MSG()
    m.hWnd = hwnd
    m.message = message
    m.wParam = wparam
    m.lParam = lparam
    return m


def test_the_frame_is_removed_and_the_active_look_kept_for_our_windows_only():
    if sys.platform != "win32":
        return
    mica._install_filter()
    f = mica._FILTER
    ours, theirs = 0x12340, 0x56780
    mica._HWNDS.add(ours)
    calls = []
    saved = mica._def_window_proc
    mica._def_window_proc = lambda *a: calls.append(a) or 1
    try:
        m = _msg(ours, mica.WM_NCCALCSIZE, 1)
        assert f.nativeEventFilter(b"windows_generic_MSG",
                                   ctypes.addressof(m)) == (True, 0)
        m = _msg(theirs, mica.WM_NCCALCSIZE, 1)
        assert f.nativeEventFilter(b"windows_generic_MSG",
                                   ctypes.addressof(m)) == (False, 0), \
            "a window that is not ours lost its frame"
        m = _msg(ours, mica.WM_NCACTIVATE, 0, 7)
        assert f.nativeEventFilter(b"windows_generic_MSG",
                                   ctypes.addressof(m)) == (True, 1)
        assert calls and calls[-1][1:3] == (mica.WM_NCACTIVATE, 1), \
            "the inactive message was not answered with the active look"
        m = _msg(ours, mica.WM_NCACTIVATE, 1)
        assert f.nativeEventFilter(b"windows_generic_MSG",
                                   ctypes.addressof(m)) == (False, 0)
    finally:
        mica._def_window_proc = saved
        mica._HWNDS.discard(ours)


_CAPTURE_CHILD = r'''
import os, sys, ctypes
sys.path.insert(0, sys.argv[1])
from ctypes import wintypes as wt
from PySide6.QtWidgets import QApplication, QWidget
from PySide6.QtGui import QPainter, QColor
app = QApplication([])
class W(QWidget):
    def paintEvent(self, _):
        QPainter(self).fillRect(self.rect(), QColor(10, 200, 90))
w = W(); w.setGeometry(-6000, -6000, 1280, 720); w.show()
for _ in range(20): app.processEvents()
from ui.util import ppt_renderer as pr
k32, u32 = ctypes.windll.kernel32, ctypes.windll.user32
k32.GetCurrentProcess.restype = wt.HANDLE
u32.GetGuiResources.argtypes = [wt.HANDLE, wt.DWORD]
gdi = lambda: u32.GetGuiResources(k32.GetCurrentProcess(), 0)
img, size, ok = pr._capture_hwnd(int(w.winId()))
before = gdi()
for _ in range(150):
    img, size, ok = pr._capture_hwnd(int(w.winId()))
after = gdi()
c = img.getpixel((img.width // 2, img.height // 2)) if img else None
print("RESULT", ok, size, c, after - before)
sys.stdout.flush(); os._exit(0)
'''


def test_the_slide_capture_reads_the_window_and_leaks_nothing():
    """PrintWindow of a window of OUR OWN, parked off-screen — the capture
    the animated PowerPoint import makes ~25 times a second."""
    if sys.platform != "win32":
        return
    env = dict(os.environ)
    env.pop("QT_QPA_PLATFORM", None)            # needs a real HWND
    out = subprocess.run([sys.executable, "-c", _CAPTURE_CHILD, str(ROOT)],
                         capture_output=True, text=True, timeout=120, env=env)
    line = [l for l in out.stdout.splitlines() if l.startswith("RESULT")]
    assert line, "the capture process died: %s" % out.stderr[-800:]
    _r, ok, *rest = line[0].split(" ", 2)
    assert ok == "True", line[0]
    assert "(10, 200, 90)" in line[0], "captured the wrong pixels: " + line[0]
    leaked = int(line[0].rsplit(" ", 1)[1])
    assert leaked <= 0, "%d GDI objects leaked over 150 captures" % leaked


if __name__ == "__main__":
    import logging
    logging.disable(logging.WARNING)
    failures = []
    for name, fn in sorted(globals().items()):
        if not name.startswith("test_") or not callable(fn):
            continue
        try:
            fn()
            print("  ok    " + name)
        except AssertionError as exc:
            failures.append(name)
            print("  FAIL  %s: %s" % (name, exc))
        except Exception as exc:                       # noqa: BLE001
            failures.append(name)
            print("  ERROR %s: %s: %s" % (name, type(exc).__name__, exc))
    print(("\n%d failed" % len(failures)) if failures else "\nall passed")
    sys.stdout.flush()
    os._exit(1 if failures else 0)
