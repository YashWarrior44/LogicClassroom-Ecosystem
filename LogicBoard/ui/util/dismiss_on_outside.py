"""
dismiss_on_outside.py — "close when the user taps outside me" behaviour.

Attach ``install_outside_dismiss(panel)`` to any frameless panel window
(pen panel, eraser panel, shapes panel, selection menu, pages panel) and
the panel will close itself when a press / touch / stylus contact lands
outside its geometry.

Why this exists as a shared helper
----------------------------------
``main_window.eventFilter`` already has the same intent — dismiss the
active tool panel on outside taps. But on some smart-board touch stacks
the event never surfaces at the QMainWindow's application-wide filter in
a form the code recognises (Qt version drift on ``QTouchEvent.points()``,
``event.globalPosition()`` returning unusual point types on synthesised
mouse events, etc.). This helper is the belt-and-suspenders backup: each
panel installs its own filter on the QApplication, so even if the
main-window path misses, the panel dismisses itself.

Design notes
------------
* Multiple event types checked: ``MouseButtonPress``, ``TouchBegin``,
  ``TabletPress``, plus ``MouseButtonRelease`` as a last-resort fallback
  (some touch hardware only fires release events reliably). Never
  triggers on ``TouchUpdate`` because that would kill legit drags.
* ``QCursor.pos()`` fallback when ``event.globalPosition()`` doesn't
  work on the incoming event — happens on some Qt 6 minor versions with
  synthesised events from Windows Pointer Input.
* Never consumes the event: the filter always returns False, so tapping
  outside still lets the target widget process the tap normally (e.g.
  a canvas draw stroke starting at the tap point).
* Respects an ``ignore`` predicate: some panels have companion floaters
  (e.g. Selection menu + selection box) where a press "outside the
  menu" should NOT close the menu if it's landing on the sibling.
"""

from __future__ import annotations

import logging
from typing import Callable, Iterable, Optional

from PySide6.QtCore import QEvent, QObject, QPoint
from PySide6.QtGui import QCursor
from PySide6.QtWidgets import QApplication, QWidget

log = logging.getLogger(__name__)

__all__ = ["install_outside_dismiss", "OutsideDismissFilter"]


# Event types that count as an "input tap". Any of these landing outside
# the panel dismisses it. TouchUpdate is deliberately excluded — we don't
# want an in-flight drag to close the panel just because the finger
# happens to move outside.
_TRIGGER_TYPES = {
    QEvent.Type.MouseButtonPress,
    QEvent.Type.TouchBegin,
    QEvent.Type.TabletPress,
    # MouseButtonRelease is a fallback for hardware that swallows presses
    # but reliably fires releases (rare, but seen on some Windows Pointer
    # Input drivers when a stylus overrides touch mid-event).
    QEvent.Type.MouseButtonRelease,
}


def _resolve_global_pos(event) -> Optional[QPoint]:
    """Best-effort global position for a press / touch / tablet event.

    Order of attempts:
      1. For touch events, the first point's ``globalPosition``.
      2. ``event.globalPosition()`` (Qt 6 QMouseEvent / QTabletEvent).
      3. Fallback to ``QCursor.pos()`` (last known cursor position).

    Returns None only if every strategy fails, which shouldn't happen on
    any supported platform but keeps the caller safe if it does."""
    et = event.type()
    # Touch first — QTouchEvent has no direct globalPosition().
    if et == QEvent.Type.TouchBegin:
        try:
            pts = event.points()
            if pts:
                try:
                    return pts[0].globalPosition().toPoint()
                except Exception:
                    # Some Qt versions return integer already
                    return pts[0].globalPosition()  # type: ignore
        except Exception:
            pass
    # Mouse / tablet — QMouseEvent.globalPosition() is a QPointF in Qt 6.
    try:
        gp = event.globalPosition()
        try:
            return gp.toPoint()
        except Exception:
            return gp  # type: ignore
    except Exception:
        pass
    # Absolute last resort — cursor position is always available.
    try:
        return QCursor.pos()
    except Exception:
        return None


class OutsideDismissFilter(QObject):
    """Application-wide event filter that closes ``panel`` on outside taps.

    Owns its own lifecycle: installs on QApplication when the panel is
    shown, uninstalls automatically when the panel is closed. Callers
    normally use :func:`install_outside_dismiss` rather than instantiating
    this class directly.
    """

    def __init__(self, panel: QWidget,
                 ignore: Optional[Callable[[QPoint], bool]] = None) -> None:
        super().__init__(panel)
        self._panel = panel
        self._ignore = ignore
        # Install ourselves on the QApplication so we see events even
        # while the panel doesn't have focus — critical for touch, where
        # the tap on the canvas can be delivered directly to the canvas
        # without the panel getting a chance to be the target.
        app = QApplication.instance()
        if app is not None:
            app.installEventFilter(self)
        # Clean up when the panel is destroyed OR closed — either way we
        # want the filter gone so we don't linger past our panel.
        try:
            panel.destroyed.connect(self._on_panel_gone)
        except Exception:
            pass

    def _on_panel_gone(self) -> None:
        try:
            app = QApplication.instance()
            if app is not None:
                app.removeEventFilter(self)
        except Exception:
            pass

    def eventFilter(self, _obj, event) -> bool:  # noqa: N802
        if event.type() not in _TRIGGER_TYPES:
            return False
        # If the panel is already gone / hidden, uninstall and bail.
        panel = self._panel
        try:
            if panel is None or not panel.isVisible():
                return False
        except RuntimeError:
            # The underlying C++ widget was already destroyed.
            self._on_panel_gone()
            return False

        gp = _resolve_global_pos(event)
        if gp is None:
            return False

        # Ignore hook lets callers protect companion floaters (e.g. a
        # selection box that pairs with a selection menu).
        if self._ignore is not None:
            try:
                if self._ignore(gp):
                    return False
            except Exception:
                pass

        try:
            if not panel.geometry().contains(gp):
                # Deferred close via a short timer would risk racing with
                # subsequent events; direct close is fine because we
                # never consume the event, so the target widget still
                # processes the tap normally.
                try:
                    panel.close()
                except Exception:
                    log.debug("Panel close failed during outside dismiss",
                               exc_info=True)
        except Exception:
            # Any geometry check failure → be silent, don't kill event
            # dispatch. Better to leave the panel open than crash the app.
            pass
        # Never consume — the tap must still reach the canvas so the
        # user's next drawing stroke starts at the tap point.
        return False


def install_outside_dismiss(
    panel: QWidget,
    *,
    ignore_widgets: Iterable[QWidget] = (),
    ignore_geometries: Iterable[Callable[[], "QRect | None"]] = (),
) -> OutsideDismissFilter:
    """Attach an outside-tap dismissal filter to ``panel``.

    Optional protections:

    * ``ignore_widgets`` — a tap inside any of these widgets does NOT
      close the panel. Use for companion floaters (toolbar, sibling
      menus). Each widget's live geometry is queried at tap time so
      re-layouts don't stale-cache anything.
    * ``ignore_geometries`` — same idea but as callables returning a
      ``QRect`` (or None). Useful when the "protected zone" isn't a
      whole widget but a computed region (e.g. the on-canvas selection
      box, which lives in canvas coordinates, not on a separate widget).

    Returns the filter so the caller can attach additional ignore rules
    or manually uninstall if it wants finer control.
    """
    igs = list(ignore_widgets)
    igg = list(ignore_geometries)

    def _ignore(gp: QPoint) -> bool:
        # Widget-based protections.
        for w in igs:
            try:
                if w is None or not w.isVisible():
                    continue
                if w.geometry().contains(gp):
                    return True
            except Exception:
                continue
        # Geometry-callback protections.
        for fn in igg:
            try:
                rect = fn()
                if rect is not None and rect.contains(gp):
                    return True
            except Exception:
                continue
        return False

    return OutsideDismissFilter(panel, ignore=_ignore)
