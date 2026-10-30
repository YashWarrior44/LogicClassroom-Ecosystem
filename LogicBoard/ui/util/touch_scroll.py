"""
ui/util/touch_scroll.py — one place for nice scrolling everywhere.

Two things every scroll area in LogicBoard wants:

1. a slim, modern scrollbar (not the chunky default Qt one), and
2. finger-flick / kinetic scrolling that actually feels right — LogicBoard
   runs on a touch smart board, so scrolling has to be phone-like: drag to
   pan, flick to coast, gentle rubber-band at the ends.

Usage:
    from ui.util.touch_scroll import setup_scroll, setup_combo_popup
    setup_scroll(my_scroll_area)         # scrollbar + touch flick, both panes
    setup_combo_popup(my_combo)          # same, for a QComboBox drop-down

``setup_scroll`` is idempotent and safe to call once after the scroll area
and its viewport exist.

Tuning history
--------------
The QScroller property values in ``enable_touch_scroll`` used to be:
  * ``DragStartDistance = 0.0015`` — 1.5 mm. Too small: a stationary tap
    almost always wobbles by more than 1.5 mm as the user's finger
    releases, so what should have been a click on an item ended up
    triggering scroll instead. Now 5 mm (Qt's own default) — tap-safe,
    still fast to start on a real drag.
  * ``DecelerationFactor = 0.42`` — kinetic scroll dropped to zero in
    a fraction of a second, so a flick that should have coasted
    several screens' worth of content stopped almost immediately.
    Now 0.10 for phone-like coast.
  * ``DragVelocitySmoothingFactor = 0.6`` — noisy velocity samples
    made flicks unpredictable. Now 0.85 to smooth out finger jitter.
  * ``MaximumVelocity = 0.55`` — bumped to 0.80 so hard flicks actually
    fly.

Also added ``AxisLockThreshold`` so a slightly-diagonal drag snaps
cleanly to vertical or horizontal instead of drifting off-axis, and
``MousePressEventDelay = 0`` so buttons inside scroll areas remain
instantly clickable — the drag-vs-tap decision is made by movement
distance, not by holding the press back.

ONE gesture per target
----------------------
``enable_touch_scroll`` used to grab BOTH ``TouchGesture`` and
``LeftMouseButtonGesture`` on the same viewport, on the assumption that
the two would coexist. They don't. ``QScroller::grabGesture`` starts
with ``if (sp->recognizer) ungrabGesture(target);`` — the second call
THROWS AWAY the first recognizer. Measured on PySide6 6.11.1 with a
mouse drag over a scroll area:

    grab TOUCH only            -> scrolled to 0     (mouse ignored)
    grab MOUSE only            -> scrolled to 350
    grab TOUCH then MOUSE      -> scrolled to 348   (mouse wins)
    grab MOUSE then TOUCH      -> scrolled to 0     (touch wins)

So the old code silently ran on the MOUSE recognizer everywhere, and
real ``QTouchEvent``s from the board only ever scrolled by accident,
via Qt's synthesised-mouse fallback — which Qt skips the moment any
widget under the finger accepts the touch. ``_gesture_for_device``
below now picks the ONE recognizer that matches the hardware.
"""
from __future__ import annotations

import ctypes
import sys

from PySide6.QtCore import QEvent, QObject, Qt
import logging

from PySide6.QtWidgets import (
    QAbstractScrollArea, QFrame, QScroller, QScrollerProperties,
)

from ui.util.size_manager import size_mgr as S
from ui.theme.theme_manager import theme_mgr

logger = logging.getLogger(__name__)


def _scrollbar_qss(width: int | None = None) -> str:
    """The shared scrollbar style — see ``ui.theme.fluent_styles.scrollbar``.

    This module used to carry its own copy, and the copy is what every
    scroll area in the app actually rendered. It had drifted from the one in
    fluent_styles on width, handle length, track margin and the pressed
    state, which is the ordinary fate of a style defined twice.
    """
    from ui.theme.fluent_styles import scrollbar
    return scrollbar(width, tk=theme_mgr.tokens)


def canvas_scrollbar_qss(width: int | None = None) -> str:
    """The same slim bar, for a STANDALONE QScrollBar rather than a scroll area.

    ``width`` overrides the default track thickness. The canvas passes a
    fatter one: its bar is dragged with a finger on a wall-mounted board,
    where the 10 px a mouse is happy with is a miss.
    """
    return _scrollbar_qss(width)


def apply_modern_scrollbar(area: QAbstractScrollArea,
                            *, transparent_bg: bool = True) -> None:
    """Give a scroll area a slim modern scrollbar (and a transparent body so
    the aero blur shows through, unless ``transparent_bg`` is False)."""
    base = (
        "QScrollArea, QScrollArea > QWidget > QWidget "
        "{ background: transparent; border: none; }"
    ) if transparent_bg else ""
    area.setStyleSheet(base + _scrollbar_qss())
    try:
        area.setFrameShape(QFrame.Shape.NoFrame)
    except Exception:
        pass
    vp = area.viewport()
    if vp is not None and transparent_bg:
        vp.setAutoFillBackground(False)
        vp.setStyleSheet("background: transparent;")


def _has_touch_screen() -> bool:
    """True if this machine has a touch digitizer.

    Asks WINDOWS, not Qt. ``QInputDevice.devices()`` looks like the
    obvious answer and is not usable here: Qt registers input devices
    lazily, on the first event of that kind, so during dialog
    construction — which is exactly when we have to choose a recognizer
    — the list is still empty and every board would be misread as
    mouse-only. Measured on this dev laptop: ``devices()`` returned
    ``[]`` while the machine genuinely has no digitizer, and it returns
    the same ``[]`` on hardware that does. ``GetSystemMetrics`` answers
    from the device tree and is correct before any input has happened.

    The metric to trust is ``SM_MAXIMUMTOUCHES`` — the number of
    simultaneous contacts the digitizer supports. ``SM_DIGITIZER``'s
    NID_*_TOUCH bits look like the precise answer and are not
    dependable: a spacedesk tablet/phone display reports ``0xC5`` — the
    PEN bits plus NID_MULTI_INPUT, with neither touch bit set — while
    happily delivering ten contacts. Reading only the touch bits scored
    that setup as mouse-only. ``SM_MAXIMUMTOUCHES`` returned 10 and got
    it right, and it returns 0 on a plain laptop with no digitizer.
    """
    try:
        from PySide6.QtGui import QInputDevice
        for dev in QInputDevice.devices():
            if dev.type() == QInputDevice.DeviceType.TouchScreen:
                return True
    except Exception:
        pass
    if sys.platform == "win32":
        try:
            SM_DIGITIZER, SM_MAXIMUMTOUCHES = 94, 95
            gsm = ctypes.windll.user32.GetSystemMetrics
            if gsm(SM_MAXIMUMTOUCHES) > 0:
                return True
            NID_INTEGRATED_TOUCH, NID_EXTERNAL_TOUCH = 0x02, 0x08
            return bool(gsm(SM_DIGITIZER)
                        & (NID_INTEGRATED_TOUCH | NID_EXTERNAL_TOUCH))
        except Exception:
            pass
    return False


def _gesture_for_device():
    """The ONE ``ScrollerGestureType`` that matches this machine.

    Only one recognizer can be grabbed per target (see the module
    docstring), so we pick rather than stack:

      * touch screen present → ``TouchGesture``, driven by native
        ``QTouchEvent``s straight off the smart board. This is the path
        that matters: it keeps working even when a widget under the
        finger accepts the touch, which is exactly the case where Qt's
        synthesised-mouse fallback gives up and the old code went dead.
      * no touch screen (plain desktop, or a driver that only ever emits
        WM_MOUSE) → ``LeftMouseButtonGesture``, so drag-to-scroll still
        works while developing and on mouse-only fallback configs.

    Mouse-wheel and the scrollbar itself are unaffected either way.
    """
    G = QScroller.ScrollerGestureType
    return G.TouchGesture if _has_touch_screen() else G.LeftMouseButtonGesture


def suspend_touch_scroll(area: QAbstractScrollArea) -> bool:
    """Let go of the gesture on ``area`` so something else can use it.

    A kinetic scroller and a drag-to-reorder want the SAME stream — one
    finger, pressed and moved. QScroller wins that contest by design: its
    recognizer watches the viewport, and the moment the pointer travels
    past DragStartDistance it takes the gesture over and sends the child a
    synthetic release. So a card picked up by a long press froze the
    instant you moved it: the ghost stopped following, nothing slid, and
    the drop never landed, because every event after the first was going
    to the scroller instead.

    Both behaviours are correct on their own; they just cannot both be
    listening. So the drag borrows the gesture and gives it back.

    Returns True if a grab was actually released, so the caller knows
    whether to restore one.
    """
    try:
        vp = area.viewport()
        if not QScroller.hasScroller(vp):
            return False
        # Stop any flick already in flight, or the list keeps coasting
        # underneath the card being dragged.
        QScroller.scroller(vp).stop()
        QScroller.ungrabGesture(vp)
        return True
    except Exception:
        logger.debug("could not suspend the scroller", exc_info=True)
        return False


def resume_touch_scroll(area: QAbstractScrollArea) -> None:
    """Give the gesture back after ``suspend_touch_scroll``.

    Re-grabs whatever this machine should be using rather than
    remembering what was released — the answer is a property of the
    hardware, not of the moment it was suspended.
    """
    try:
        QScroller.grabGesture(area.viewport(), _gesture_for_device())
    except Exception:
        logger.debug("could not resume the scroller", exc_info=True)


def enable_touch_scroll(area: QAbstractScrollArea) -> None:
    """Enable finger-flick / kinetic scrolling on ``area``.

    Grabs exactly ONE gesture type on the viewport — whichever
    ``_gesture_for_device`` says this hardware actually speaks. Grabbing
    two is not additive: the second grab silently discards the first
    (proof in the module docstring), which is what left every scroll
    area in the app running on the mouse recognizer and therefore dead
    to a real finger.

    ``grabGesture`` also flips ``WA_AcceptTouchEvents`` on the target as
    a side effect when the touch recognizer is the one being installed,
    so touch events reliably reach the scroller instead of being
    dropped.

    All scroller properties are set to phone-like values (see the
    module docstring's "Tuning history" section for what changed from
    the previous defaults and why).
    """
    vp = area.viewport()
    if vp is None:
        return

    try:
        QScroller.grabGesture(vp, _gesture_for_device())
    except Exception:
        return

    scroller = QScroller.scroller(vp)
    props = scroller.scrollerProperties()
    M = QScrollerProperties.ScrollMetric

    def setm(metric, value):
        try:
            props.setScrollMetric(metric, value)
        except Exception:
            pass

    # ── Drag detection ────────────────────────────────────────────────
    # 5 mm — Qt's own default. Small enough that a real drag starts
    # snappily; big enough that natural tap-release finger wobble
    # never triggers scroll instead of a click. The previous 1.5 mm
    # made almost every button-tap in a scrollable area accidentally
    # register as a scroll gesture.
    setm(M.DragStartDistance, 0.005)

    # Deliver press events to the child widget IMMEDIATELY (no delay).
    # The drag-vs-tap decision is made by movement distance
    # (``DragStartDistance`` above), not by holding the press back.
    # This is what keeps buttons inside scroll areas feeling
    # instantly responsive.
    setm(M.MousePressEventDelay, 0.0)

    # ── Motion feel ────────────────────────────────────────────────────
    # Smoother velocity estimate — the previous 0.6 let finger jitter
    # bleed into the flick vector, so identical-feeling flicks could
    # coast very different distances. 0.85 filters out the noise.
    setm(M.DragVelocitySmoothingFactor, 0.85)

    # 2/3 of a movement direction commits us to that axis. Diagonal
    # drift now snaps cleanly to vertical OR horizontal instead of
    # scrolling both axes at once (which felt uncontrolled on the
    # smart board's larger scroll areas).
    setm(M.AxisLockThreshold, 0.66)

    # ── Kinetic scroll ────────────────────────────────────────────────
    # Coast for ~1 s instead of the previous ~0.3 s. A hard flick now
    # actually flies several screens, matching every phone / tablet
    # the user is used to.
    setm(M.DecelerationFactor, 0.10)

    # High enough that a determined flick zips. Previous 0.55 capped
    # long-distance flicks awkwardly.
    setm(M.MaximumVelocity, 0.80)
    setm(M.MinimumVelocity, 0.001)

    # ── Rubber-band at the ends ───────────────────────────────────────
    # Elastic feel when the user drags past the top / bottom. Softer
    # resistance + medium bounce = same feel as iOS / Android.
    setm(M.OvershootDragResistanceFactor, 0.5)
    setm(M.OvershootDragDistanceFactor, 0.15)
    setm(M.OvershootScrollDistanceFactor, 0.15)
    setm(M.HorizontalOvershootPolicy,
         QScrollerProperties.OvershootPolicy.OvershootWhenScrollable)
    setm(M.VerticalOvershootPolicy,
         QScrollerProperties.OvershootPolicy.OvershootWhenScrollable)

    scroller.setScrollerProperties(props)

    # Smoother mouse-wheel micro-scrolling too — the scroll bar step is
    # what the wheel deltas roll into. Bigger step = fewer, chunkier
    # jumps; smaller step = smoother scroll. Half a line height feels
    # about right on a smart board's large content areas.
    for bar in (area.verticalScrollBar(), area.horizontalScrollBar()):
        if bar is not None:
            bar.setSingleStep(S.scale_size(22))


def setup_scroll(area: QAbstractScrollArea,
                  *, transparent_bg: bool = True) -> None:
    """Modern scrollbar + touch flick in one call. Safe to call once after
    the scroll area and its viewport exist. Idempotent."""
    apply_modern_scrollbar(area, transparent_bg=transparent_bg)
    enable_touch_scroll(area)


def setup_combo_popup(combo, *, max_visible: int = 8) -> None:
    """Make a ``QComboBox``'s drop-down behave like the rest of LogicBoard:
    finger-scrollable, and glass instead of a flat opaque slab.

    A combo's popup is easy to forget because it isn't a widget anyone
    builds — Qt makes it internally. But ``combo.view()`` IS a
    ``QAbstractScrollArea``, so it takes the same treatment as every
    other scroll region in the app; without it a 14-entry class list is
    a dead, un-flickable column on a touch board.

    Two things happen here:

    1. Kinetic flick + slim scrollbar on the view, so a 14-entry class
       list is actually flickable on a touch board instead of a dead
       column.
    2. The popup WINDOW becomes translucent and picks up DWM aero the
       same way every other floating surface in the app does. This is
       what finally makes the tint mean something: the list already
       painted an 0.78-alpha fill before, it just sat on an opaque
       window, so 0.78 composited against solid black and read as a flat
       slab. Translucent window + the fill dropped to ``aero_tint``
       (0.58, the token file's documented sweet spot for popovers) is
       what turns it into glass.

    The fill deliberately stays on the ITEM VIEW rather than moving to
    the popup's container frame. The container has
    ``WA_TranslucentBackground``, which implies ``WA_NoSystemBackground``
    and takes it out of the background paint path entirely — a
    stylesheet fill put there renders nothing at all (grabbed the live
    popup to check: every pixel came back rgba(0,0,0,0)). The view has
    no such problem, and it spans the whole popup, so it's both the
    reliable painter and full coverage for hit testing.

    ``max_visible`` caps the popup height so it always has something to
    scroll rather than growing into a full-height column.
    """
    view = combo.view()
    if view is None:
        return
    combo.setMaxVisibleItems(max_visible)

    # ``transparent_bg=False`` — the view must keep painting its own fill
    # (set by the caller's ``QComboBox QAbstractItemView`` rule). On a
    # translucent popup window an unpainted pixel is alpha 0, and Windows
    # hit-tests layered windows by alpha, so a transparent view would
    # turn the whole drop-down into a hole taps fall through. See
    # ``tokens.hit_catcher``.
    setup_scroll(view, transparent_bg=False)

    popup = view.window()
    if popup is None or popup is combo.window():
        return
    try:
        popup.setWindowFlag(Qt.WindowType.FramelessWindowHint, True)
        popup.setAttribute(Qt.WidgetAttribute.WA_TranslucentBackground, True)
    except Exception:
        return

    # Aero can only attach once the popup has a live HWND, which is
    # the first time it's shown.
    _GlassPopupFilter.install(popup)


class _GlassPopupFilter(QObject):
    """Applies DWM aero to a combo popup the first time it is shown."""

    # Guard + lifetime are held in Qt, not on the Python wrapper. PySide
    # hands out wrappers per call and an attribute set on one is not
    # readable from the next, so a ``_applied`` attribute on the popup
    # silently never registered and the aero was applied on every
    # single show. A dynamic property lives on the C++ object and a
    # parented QObject is kept alive by Qt's ownership.
    _FLAG = "lb_glass_popup"

    @classmethod
    def install(cls, popup) -> None:
        if popup.property(cls._FLAG):
            return
        popup.setProperty(cls._FLAG, True)
        popup.installEventFilter(cls(popup))

    def __init__(self, popup) -> None:
        super().__init__(popup)
        self._applied = False

    def eventFilter(self, obj, event):
        if event.type() == QEvent.Type.Show and not self._applied:
            self._applied = True
            try:
                from ui.util.aero import apply_aero, apply_rounded_corners
                apply_aero(obj)
                apply_rounded_corners(obj, mode="round")
            except Exception:
                pass
        return False