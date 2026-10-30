"""
selection_handles.py — Draw resize/rotate handles over selected objects.

Renders 8 square corner/edge handles and a rotation handle above the
bounding box of the currently selected object.
"""

from __future__ import annotations

from typing import Optional

from PySide6.QtGui import QPainter, QPen, QBrush, QColor
from PySide6.QtCore import Qt, QPointF, QRectF

from ui.theme.theme_manager import theme_mgr

__all__ = ["SelectionHandles"]


HANDLE_SIZE = 10.0
ROTATION_OFFSET = 30.0


class SelectionHandles:
    """Draws and hit-tests selection handles for an object's bounding box."""

    def __init__(self) -> None:
        self._bbox: Optional[QRectF] = None
        self._active_handle: Optional[str] = None

    @property
    def bbox(self) -> Optional[QRectF]:
        return self._bbox

    def set_bbox(self, rect: QRectF) -> None:
        self._bbox = QRectF(rect)

    def clear(self) -> None:
        self._bbox = None
        self._active_handle = None

    @property
    def active_handle(self) -> Optional[str]:
        return self._active_handle

    def handle_positions(self) -> dict[str, QPointF]:
        """Return named handle center positions."""
        if self._bbox is None:
            return {}

        b = self._bbox
        cx = b.center().x()
        cy = b.center().y()
        x0, y0 = b.left(), b.top()
        x1, y1 = b.right(), b.bottom()

        return {
            "tl": QPointF(x0, y0),
            "tc": QPointF(cx, y0),
            "tr": QPointF(x1, y0),
            "ml": QPointF(x0, cy),
            "mr": QPointF(x1, cy),
            "bl": QPointF(x0, y1),
            "bc": QPointF(cx, y1),
            "br": QPointF(x1, y1),
            "rotate": QPointF(cx, y0 - ROTATION_OFFSET),
        }

    def hit_test_handle(self, pos: QPointF) -> Optional[str]:
        """Return the name of the handle at `pos`, or None."""
        half = HANDLE_SIZE / 2.0 + 2.0
        for name, center in self.handle_positions().items():
            if abs(pos.x() - center.x()) <= half and abs(pos.y() - center.y()) <= half:
                self._active_handle = name
                return name
        self._active_handle = None
        return None

    def draw(self, painter: QPainter) -> None:
        """Render selection rectangle and handles."""
        if self._bbox is None:
            return

        t = theme_mgr.tokens
        accent = theme_mgr.qcolor(t.accent)
        handle_fill = QColor("#ffffff")
        handle_border = accent

        # Selection rectangle
        painter.setPen(QPen(accent, 1.5, Qt.DashLine))
        painter.setBrush(Qt.NoBrush)
        painter.drawRect(self._bbox)

        # Handles
        half = HANDLE_SIZE / 2.0
        positions = self.handle_positions()

        for name, center in positions.items():
            if name == "rotate":
                # Rotation handle: circle + connecting line
                painter.setPen(QPen(accent, 1.5))
                painter.setBrush(Qt.NoBrush)
                top_center = positions.get("tc")
                if top_center:
                    painter.drawLine(top_center, center)
                painter.setBrush(QBrush(handle_fill))
                painter.setPen(QPen(handle_border, 2))
                painter.drawEllipse(center, half, half)
            else:
                rect = QRectF(center.x() - half, center.y() - half, HANDLE_SIZE, HANDLE_SIZE)
                painter.setPen(QPen(handle_border, 2))
                painter.setBrush(QBrush(handle_fill))
                painter.drawRect(rect)

    @staticmethod
    def cursor_for_handle(name: Optional[str]) -> Qt.CursorShape:
        """Return the appropriate resize cursor for a handle name."""
        cursors = {
            "tl": Qt.SizeFDiagCursor,
            "br": Qt.SizeFDiagCursor,
            "tr": Qt.SizeBDiagCursor,
            "bl": Qt.SizeBDiagCursor,
            "tc": Qt.SizeVerCursor,
            "bc": Qt.SizeVerCursor,
            "ml": Qt.SizeHorCursor,
            "mr": Qt.SizeHorCursor,
            "rotate": Qt.CrossCursor,
        }
        return cursors.get(name or "", Qt.ArrowCursor)