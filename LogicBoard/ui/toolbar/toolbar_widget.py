"""
toolbar_widget.py — Floating toolbar groups for LogicBoard v3.0.

The toolbar is split into THREE independent aero-pill groups:

    [Exit Min Settings]        [Sel Shapes | Pen Eraser | Undo Redo | Media File]        [< 1/4 >]
     bottom-LEFT                       bottom-CENTER                                       bottom-RIGHT

Each group is a top-level Qt.Tool window with the same Win11 aero /
DWM rounded corners / drop shadow as the previous unified toolbar — the
visual language is identical, just split horizontally.

Architecture:
  * ``ToolbarGroup`` — the reusable aero-pill primitive. Takes a list
    of ``ButtonSpec`` plus an optional trailing widget (PageNavigator).
  * ``ToolbarStack``  — composes 3 ToolbarGroups (left/center/right) and
    exposes the SAME public API as the previous monolithic ToolbarWidget
    so the main_window integration stays a one-line change.
"""

from __future__ import annotations

import sys
import logging
from dataclasses import dataclass, field
from typing import Optional, Callable, List

from PySide6.QtWidgets import QWidget, QHBoxLayout
from PySide6.QtCore import Qt

from ui.toolbar.tool_button import ToolButton, ToolbarDivider
from ui.toolbar.page_navigator import PageNavigator
from ui.util.scaling import UiScale
from ui.util.aero import apply_aero
from ui.util.fluent_icons import icon_char
from ui.canvas.shape_geometry import SHAPE_TOOLS
from ui.util.audio import click_sound

logger = logging.getLogger(__name__)
from ui.theme.theme_manager import theme_mgr

__all__ = ["ToolbarStack", "ToolbarGroup", "ButtonSpec", "ToolbarWidget"]


# ════════════════════════════════════════════════════════════════════════════
# Config dataclasses
# ════════════════════════════════════════════════════════════════════════════

# 85 % of the general UI scale: the toolbar sits over the lesson on every
# page, and at full size it took more of the board than its buttons need.
# Tap targets stay comfortably finger-sized (a 64 px button becomes ~54 px).
TOOLBAR_SIZE = 0.85


@dataclass
class ButtonSpec:
    """Declarative spec for a button in a ``ToolbarGroup``.

    ``id``             unique name to retrieve the button afterwards.
    ``icon``           SVG/Fluent icon name.
    ``label``          hover label beneath the icon.
    ``callback``       click handler. ``None`` = inert placeholder.
    ``mode``           active-state group ("pen"/"eraser"/"select"/"shapes").
                       ``None`` = non-stateful.
    ``divider_after``  draw a vertical divider after this button.
    """
    id: str
    icon: str
    label: str
    callback: Optional[Callable] = None
    mode: Optional[str] = None
    divider_after: bool = False


@dataclass
class GroupSpec:
    object_name: str
    buttons: List[ButtonSpec] = field(default_factory=list)
    trailing_widget: Optional[QWidget] = None


# ════════════════════════════════════════════════════════════════════════════
# ToolbarGroup — one aero pill
# ════════════════════════════════════════════════════════════════════════════

class ToolbarGroup(QWidget):
    """One floating aero-pill toolbar group.

    The aero / DWM rounded-corner / drop-shadow / theme logic is
    identical to the prior unified ToolbarWidget — only the button set is
    now configurable via ``GroupSpec``. Three of these compose the full
    bottom toolbar bar.
    """

    def __init__(self, spec: GroupSpec, *, parent: QWidget | None = None) -> None:
        super().__init__(
            parent,
            Qt.Tool | Qt.FramelessWindowHint | Qt.NoDropShadowWindowHint,
        )
        self.setAttribute(Qt.WA_TranslucentBackground, True)
        self.setAttribute(Qt.WA_ShowWithoutActivating, True)
        self.setObjectName(spec.object_name)
        # The pill's body: the same solid painted surface every panel has,
        # with fully rounded ends. It is also what makes the toolbar's own
        # padding hit-testable — without a surface, a tap that missed a
        # button by a few millimetres went through to the canvas and left a
        # dot of ink on the lesson.
        try:
            from ui.util.aero import install_hit_catcher
            install_hit_catcher(self, radius=8)
        except Exception:
            logger.debug("toolbar hit catcher failed", exc_info=True)

        self._spec = spec
        # The toolbar's own size relative to the rest of the UI. Everything in
        # it — buttons, icons, captions, dividers, the page counter, padding —
        # is derived from this one factor, so it shrinks in proportion.
        self._scale = UiScale.factor() * TOOLBAR_SIZE
        self._buttons_by_id: dict[str, ToolButton] = {}
        self._buttons_meta: list[dict] = []
        self._dividers: list[ToolbarDivider] = []
        self._active_tool: str = "pen"

        self._build_ui()
        theme_mgr.theme_changed.connect(self._on_theme_changed)

    def _s(self, v: float) -> int:
        return int(round(v * self._scale))

    # ─── Build ────────────────────────────────────────────────────────────
    def _build_ui(self) -> None:
        bar = QHBoxLayout(self)
        bar.setContentsMargins(self._s(18), self._s(12),
                               self._s(18), self._s(10))
        bar.setSpacing(self._s(8))
        self._bar_layout = bar

        for spec in self._spec.buttons:
            checkable = spec.mode is not None
            btn = ToolButton(
                icon_char(spec.icon),
                spec.label,
                checkable=checkable,
                icon_name=spec.icon,
            )
            if spec.callback is not None:
                btn.clicked.connect(spec.callback)
            bar.addWidget(btn, alignment=Qt.AlignVCenter)
            self._buttons_by_id[spec.id] = btn
            self._buttons_meta.append({
                "button": btn, "mode": spec.mode, "icon_name": spec.icon
            })
            if spec.divider_after:
                d = ToolbarDivider()
                bar.addWidget(d)
                self._dividers.append(d)

        if self._spec.trailing_widget is not None:
            bar.addWidget(self._spec.trailing_widget,
                          alignment=Qt.AlignVCenter)

        self._apply_scale()
        self._update_tool_ui()

    # ─── Aero / rounded / shadow ───────────────────────────────────────
    def _apply_real_aero(self) -> None:
        if sys.platform != "win32":
            return
        try:
            apply_aero(self)
        except Exception as e:
            logger.debug("toolbar %s apply_aero failed: %s", self._spec.object_name, e)

    def showEvent(self, event) -> None:
        super().showEvent(event)
        self._apply_real_aero()
        try:
            from ui.util.aero import apply_rounded_corners
            apply_rounded_corners(self, mode="round")
        except Exception as e:
            logger.debug("toolbar %s apply_rounded_corners failed: %s", self._spec.object_name, e)
        try:
            from ui.util.aero import enable_shadow
            enable_shadow(self)
        except Exception as e:
            logger.debug("toolbar %s enable_shadow failed: %s", self._spec.object_name, e)

    # ─── Public access ────────────────────────────────────────────────────
    def button(self, btn_id: str) -> Optional[ToolButton]:
        return self._buttons_by_id.get(btn_id)

    def set_active_tool(self, tool: str) -> None:
        self._active_tool = tool
        self._update_tool_ui()

    def refresh(self) -> None:
        self._update_tool_ui()

    # ─── Active-state styling ─────────────────────────────────────────────
    def _update_tool_ui(self) -> None:
        tool = self._active_tool
        for item in self._buttons_meta:
            btn = item["button"]; mode = item["mode"]
            if mode is not None:
                is_active = False
                if mode == "pen" and tool in ("pen", "highlighter", "brush"):
                    is_active = True
                elif mode == "eraser" and tool == "eraser":
                    is_active = True
                elif mode == "shapes" and tool in SHAPE_TOOLS:
                    is_active = True
                elif mode == "select" and tool == "select":
                    is_active = True
                btn.set_active(is_active)
            btn.apply_theme()
        for d in self._dividers:
            d.apply_theme()
        self.setStyleSheet(
            f"QWidget#{self._spec.object_name} "
            f"{{ background: transparent; border: none; }}"
        )

    # ─── Scale / Theme ────────────────────────────────────────────────────
    def _apply_scale(self) -> None:
        s = self._s
        self._bar_layout.setContentsMargins(s(18), s(12), s(18), s(10))
        self._bar_layout.setSpacing(s(8))
        for item in self._buttons_meta:
            item["button"].apply_scale(self._scale)
        for d in self._dividers:
            d.apply_scale(self._scale)
        if self._spec.trailing_widget is not None and hasattr(
                self._spec.trailing_widget, "apply_scale"):
            self._spec.trailing_widget.apply_scale(self._scale)

    def _on_theme_changed(self, tokens) -> None:
        self._update_tool_ui()


# ════════════════════════════════════════════════════════════════════════════
# ToolbarStack — composes 3 groups, exposes the unified public API
# ════════════════════════════════════════════════════════════════════════════

class ToolbarStack:
    """Owns the three toolbar groups and exposes the same public API as
    the previous monolithic ToolbarWidget. Not itself a QWidget — each
    group is a top-level Qt.Tool window.
    """

    def __init__(
        self,
        *,
        on_minimize: Optional[Callable] = None,
        on_exit: Optional[Callable] = None,
        on_settings: Optional[Callable] = None,
        on_select: Optional[Callable] = None,
        on_pen: Optional[Callable] = None,
        on_eraser: Optional[Callable] = None,
        on_undo: Optional[Callable] = None,
        on_redo: Optional[Callable] = None,
        on_imports: Optional[Callable] = None,
        on_file: Optional[Callable] = None,
        on_prev_page: Optional[Callable] = None,
        on_next_page: Optional[Callable] = None,
        on_page_center: Optional[Callable] = None,
        on_shapes: Optional[Callable] = None,
        parent: QWidget | None = None,
    ) -> None:
        self._parent = parent

        # ── LEFT group: Exit | Minimize | Settings ────────────────────────
        # Order is deliberate (window controls first, app controls last) and
        # is what the user asked for; the ids are unchanged so every
        # main_window accessor below still resolves.
        left_spec = GroupSpec(
            object_name="ToolbarGroupLeft",
            buttons=[
                ButtonSpec("exit",     "exit",     "Exit",     on_exit),
                ButtonSpec("minimize", "minimize", "Minimize", on_minimize),
                ButtonSpec("settings", "settings", "Settings", on_settings),
            ],
        )

        # ── CENTER group: tools | undo/redo | media | file ────────────────
        center_buttons = [
            # Divider after Shapes: Select + Shapes act ON objects, Pen +
            # Eraser act ON ink. The rule makes that grouping visible instead
            # of leaving four unrelated-looking tools in a row.
            ButtonSpec("select",  "select",  "Select", on_select,  mode="select"),
            ButtonSpec("shapes",  "rect",    "Shapes", on_shapes,  mode="shapes",
                       divider_after=True),
            ButtonSpec("pen",     "pen",     "Pen",    on_pen,     mode="pen"),
            ButtonSpec("eraser",  "eraser",  "Eraser", on_eraser,  mode="eraser",
                       divider_after=True),
            ButtonSpec("undo",    "undo",    "Undo",   on_undo),
            ButtonSpec("redo",    "redo",    "Redo",   on_redo,
                       divider_after=True),
            ButtonSpec("imports", "media",   "Media",  on_imports),
            ButtonSpec("file",    "file",    "File",   on_file),
        ]
        center_spec = GroupSpec(
            object_name="ToolbarGroupCenter",
            buttons=center_buttons,
        )

        # ── RIGHT group: prev | page nav | next (PageNavigator owns all 3) ─
        page_nav = PageNavigator()
        if on_prev_page is not None:
            page_nav.prev_clicked.connect(
                lambda: (click_sound.play_click(), on_prev_page()))
        if on_next_page is not None:
            page_nav.next_clicked.connect(
                lambda: (click_sound.play_click(), on_next_page()))
        if on_page_center is not None:
            page_nav.center_clicked.connect(
                lambda: (click_sound.play_click(), on_page_center()))
        right_spec = GroupSpec(
            object_name="ToolbarGroupRight",
            buttons=[],
            trailing_widget=page_nav,
        )

        self.left   = ToolbarGroup(left_spec,   parent=parent)
        self.center = ToolbarGroup(center_spec, parent=parent)
        self.right  = ToolbarGroup(right_spec,  parent=parent)
        self._page_nav = page_nav
        self._on_shapes = on_shapes

    # ─── Show / hide / raise — apply to all three pills ───────────────────
    def show(self) -> None:
        for g in (self.left, self.center, self.right):
            g.show()

    def hide(self) -> None:
        for g in (self.left, self.center, self.right):
            g.hide()

    def raise_(self) -> None:
        for g in (self.left, self.center, self.right):
            g.raise_()

    def isVisible(self) -> bool:
        return self.center.isVisible()

    # ─── Backward-compat button accessors used by main_window ─────────────
    @property
    def pen_btn(self):      return self.center.button("pen")
    @property
    def eraser_btn(self):   return self.center.button("eraser")
    @property
    def select_btn(self):   return self.center.button("select")
    @property
    def imports_btn(self):  return self.center.button("imports")
    @property
    def file_btn(self):     return self.center.button("file")
    @property
    def settings_btn(self): return self.left.button("settings")
    @property
    def minimize_btn(self): return self.left.button("minimize")
    @property
    def exit_btn(self):     return self.left.button("exit")
    @property
    def shapes_btn(self):   return self.center.button("shapes")

    # ─── Public methods used by main_window ───────────────────────────────
    def set_active_tool(self, tool: str) -> None:
        for g in (self.left, self.center, self.right):
            g.set_active_tool(tool)

    def set_pen_display(self, icon_name: str, label: str) -> None:
        btn = self.center.button("pen")
        if btn is not None:
            btn.set_tool_display_icon(
                icon_name, label, emoji_fallback=icon_char(icon_name))

    def set_shapes_display(self, icon_name: str, label: str,
                           emoji_fallback: str | None = None) -> None:
        btn = self.center.button("shapes")
        if btn is not None:
            btn.set_tool_display_icon(
                icon_name, label,
                emoji_fallback=emoji_fallback or icon_char(icon_name) or "")

    def set_page_text(self, text: str) -> None:
        self._page_nav.set_page_text(text)

    def refresh(self) -> None:
        for g in (self.left, self.center, self.right):
            g.refresh()


# Backwards-compat alias so existing ``from ui.toolbar.toolbar_widget
# import ToolbarWidget`` imports keep working.
ToolbarWidget = ToolbarStack