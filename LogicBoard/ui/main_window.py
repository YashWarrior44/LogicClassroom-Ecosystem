"""
main_window.py — LogicBoard v3.0 main window.

Owns the CanvasWidget, InputHandler, bottom toolbar, and connects
all panels (file, settings, pen, eraser) to the canvas and engine.
"""

from __future__ import annotations

import json
import shutil
import logging
import math
from pathlib import Path
from typing import Optional

from PySide6.QtWidgets import (
    QWidget,
    QVBoxLayout,
    QHBoxLayout,
    QMessageBox,
    QInputDialog,
    QApplication,
    QFileDialog,
    QColorDialog,
)
from PySide6.QtGui import QIcon, QShortcut, QKeySequence, QColor
from PySide6.QtCore import Qt, QTimer, QEvent

from ui.util.paths import find_app_icon_path, saves_dir
from ui.util.lb_format import (
    LB_EXTENSION,
    is_lb_file,
    is_legacy_board_folder,
    open_lb_to_workdir,
    save_workdir_to_lb,
    migrate_folder_to_lb,
    cleanup_workdir,
    new_unsaved_workdir,
)
from ui.util.audio import click_sound
from ui.util.scaling import UiScale
from ui.theme.theme_manager import theme_mgr
from ui.panels.aero_alert import AeroAlert
from ui.canvas.canvas_widget import CanvasWidget
from ui.canvas.input_handler import InputHandler
from ui.toolbar.toolbar_widget import ToolbarWidget
from ui.panels.file_panel import FilePanel
from ui.panels.settings_panel import SettingsPanel
from ui.panels.pen_panel import PenPanel
from ui.panels.eraser_panel import EraserPanel
from ui.panels.media_manager import MediaManagerDialog
from ui.panels.shapes_panel import ShapesPanel
from ui.panels.selection_menu import SelectionMenu
from ui.panels.pages_panel import PagesPanel
from ui.util.thumbnail_manager import ThumbnailManager
from ui.util.aero import apply_dark_titlebar, enable_shadow
from ui.util import imports as imports_util
from ui.util import media_import
from ui.util.media_library import MediaLibrary, MediaItem
from ui.util.import_progress import (
    run_ppt_import_with_progress,
    run_pdf_import_with_progress,
)

__all__ = ["MainWindow"]


class MainWindow(QWidget):
    """Top-level full-screen window for LogicBoard."""

    def __init__(self, parent: QWidget | None = None) -> None:
        super().__init__(parent)

        self.setMouseTracking(True)
        self.setFocusPolicy(Qt.StrongFocus)
        self.setWindowTitle("LogicBoard v3.0 — Untitled")

        icon_path = find_app_icon_path()
        if icon_path:
            self.setWindowIcon(QIcon(str(icon_path)))
            app = QApplication.instance()
            if app:
                app.setWindowIcon(QIcon(str(icon_path)))

        # ---- Board metadata ----
        # ``_current_lb_path`` is the user-facing .lb file (or None if the
        # board has never been saved). ``_current_board_path`` is the working
        # directory we extracted that .lb into — and is what every existing
        # code path treats as "the board folder". On save we re-zip the
        # workdir back into ``_current_lb_path``.
        # Set once this window has been given permission to close, so the
        # unsaved-work question is asked ONCE per window however many times
        # the close is re-entered. See ``confirm_close``.
        self._closing = False
        self._current_lb_path:    Optional[Path] = None
        self._current_board_path: Optional[Path] = None
        self._current_board_name: Optional[str]  = None
        self._saves_dir     = saves_dir()
        self._settings_file = self._saves_dir / "settings.json"
        # Input calibration + app settings manager (Phase 1). Loads touch mode,
        # stylus/eraser/palm profiles, and auto-save prefs from settings.json.
        # NOT yet consulted by the touch pipeline (that's Phase 3).
        from ui.util.input_calibration import InputCalibration
        self._input_cal = InputCalibration(self._settings_file)
        self._input_cal.load()

        # ---- Canvas + input handler ----
        self._canvas = CanvasWidget(self)
        self._input_handler = InputHandler(self._canvas)
        # Dual Pen — restore persisted tip assignments before any input flows.
        self._input_handler.set_dual_pen_assignment(
            front=self._input_cal.dual_pen_front,
            back=self._input_cal.dual_pen_back,
        )
        # Boards that emit a left click for the stylus front tip.
        self._input_handler.set_mouse_is_stylus_front(
            self._input_cal.mouse_is_stylus_front)
        # Eraser sizes — restore each mode's last-used size independently.
        self._canvas._eraser_pixel_size  = self._input_cal.eraser_pixel_size
        self._canvas._eraser_object_size = self._input_cal.eraser_object_size
        # Canvas layout + list zoom strength, restored before the first paint
        # so the board opens in the style the user left it in.
        self._canvas.set_canvas_style(self._input_cal.canvas_style)
        self._canvas.set_zoom_sensitivity(self._input_cal.zoom_sensitivity)
        self._canvas.set_input_handler(self._input_handler)
        # Phase 3: build the runtime touch classifier from the calibration
        # profiles and attach it to the canvas. The input handler consults it
        # to tell stylus / eraser / palm apart (stylus prioritized).
        from ui.canvas.touch_classifier import TouchClassifier
        self._touch_classifier = TouchClassifier(self._input_cal)
        self._touch_classifier.legacy_palm_min_size = self._canvas.palm_min_size
        self._canvas._touch_classifier = self._touch_classifier
        self._canvas._touch_mode = self._input_cal.touch_mode
        self._canvas.selection_changed.connect(self._on_selection_changed)
        # While the user drags the rotation handle, the floating
        # SelectionMenu should disappear (its position is anchored to a
        # bbox we DELIBERATELY don't recompute during rotation, so it
        # would sit at a stale spot and read as a bug). The canvas emits
        # rotating_changed(True) on drag-start and (False) on release —
        # we simply gate the menu's visibility on that.
        self._canvas.rotating_changed.connect(self._on_rotating_changed)
        self._selection_menu = None
        # Page thumbnail manager (RAM/CPU-optimised, disk-cached in temps/).
        self._thumb_manager = ThumbnailManager(self._canvas, self)
        self._thumb_manager.start()
        # Live current-page preview: on every committed change the manager
        # refreshes the OPEN page's thumbnail within ~150 ms (debounced).
        # Other pages are still handled by the 5-second periodic tick.
        self._canvas.content_changed.connect(self._thumb_manager.refresh_current_soon)

        # ---- Toolbar (3-group split: bottom-left / center / right) ----
        # The previous monolithic toolbar is now three independent aero
        # pills. Layout / positioning handled in ``_position_toolbar``.
        self._toolbar = ToolbarWidget(
            # bottom-LEFT pill
            on_minimize=self.showMinimized,
            on_exit=self.close,
            on_settings=self._show_settings_panel,
            # bottom-CENTER pill
            on_select=lambda: self._set_tool("select"),
            on_pen=self._on_pen_button,
            on_eraser=lambda: self._set_tool("eraser"),
            on_undo=self._undo,
            on_redo=self._redo,
            on_imports=self._show_imports_panel,
            on_file=self._show_file_panel,
            # bottom-RIGHT pill
            on_prev_page=self._prev_page,
            on_next_page=self._next_page,
            on_page_center=self._show_page_options,
            # Shapes still wired in case future versions add the button.
            on_shapes=self._show_shapes_panel,
            parent=self,
        )

        # ---- Active panel tracking ----
        self._active_panel: Optional[QWidget] = None
        from ui.canvas.shape_geometry import SHAPE_TOOLS
        self._shape_tools = list(SHAPE_TOOLS)
        self._shape_index = 0
        self._last_shape_label = ""
        self._pen_tool_mode = "pen"  # remembers pen vs highlighter for the pen button
        # Tint the Pen button with the starting ink colour so it is correct
        # before the user touches anything, not only after the first change.
        self._sync_pen_button_ink()
        self._on_shape_color(self._canvas.shape_color)   # Shapes wears its ink too

        # ---- Layout (FLOATING toolbar over a full-screen canvas) ----
        # The canvas fills the window edge-to-edge. The toolbar is a sibling
        # widget overlaid on top, repositioned in resizeEvent. This way the
        # canvas is visible behind the toolbar bg and the toolbar always sits
        # near the bottom-center as a true floating bar.
        layout = QVBoxLayout(self)
        layout.setContentsMargins(0, 0, 0, 0)
        layout.setSpacing(0)
        layout.addWidget(self._canvas, 1)

        # The toolbar is now a top-level tool window — NOT a child of the
        # main window. This is required so pywinstyles can apply DWM Aero
        # to its own HWND (DWM compositor effects only work on top-level
        # windows, not on embedded child widgets).
        # We keep a reference but do NOT call setParent / raise_.
        # _toolbar_bottom_margin MUST be set before calling _position_toolbar.
        self._toolbar_bottom_margin = UiScale.px(20)   # in step with the smaller toolbar
        self._toolbar.show()
        self._position_toolbar()
        # The toolbar must NOT pass mouse clicks through to the canvas — Qt
        # widgets are opaque to events by default; we set this explicitly for
        # clarity (no WA_TransparentForMouseEvents).

        # Position the toolbar after construction; resizeEvent handles
        # subsequent repositions.
        # ---- Shortcuts ----
        self._shortcut_save = QShortcut(QKeySequence.Save, self)
        self._shortcut_save.setContext(Qt.ShortcutContext.ApplicationShortcut)
        self._shortcut_save.activated.connect(self._save_board)

        self._shortcut_undo = QShortcut(QKeySequence("Ctrl+Z"), self)
        self._shortcut_undo.setContext(Qt.ShortcutContext.ApplicationShortcut)
        self._shortcut_undo.activated.connect(self._undo)

        self._shortcut_redo = QShortcut(QKeySequence("Ctrl+Y"), self)
        self._shortcut_redo.setContext(Qt.ShortcutContext.ApplicationShortcut)
        self._shortcut_redo.activated.connect(self._redo)

        self._shortcut_esc = QShortcut(QKeySequence("Esc"), self)
        self._shortcut_esc.setContext(Qt.ShortcutContext.ApplicationShortcut)
        self._shortcut_esc.activated.connect(self.showMinimized)

        # Copy / paste for boards with a keyboard plugged in. Window-scoped,
        # not application-wide: a text field in a dialog keeps its own
        # Ctrl+C / Ctrl+V.
        self._shortcut_copy = QShortcut(QKeySequence.Copy, self)
        self._shortcut_copy.activated.connect(self._copy_selection)
        self._shortcut_paste = QShortcut(QKeySequence.Paste, self)
        self._shortcut_paste.activated.connect(self._paste_clipboard)

        # Developer: toggle the input-type overlay (Phase 4). Ctrl+Shift+D.
        self._shortcut_dbg = QShortcut(QKeySequence("Ctrl+Shift+D"), self)
        self._shortcut_dbg.setContext(Qt.ShortcutContext.ApplicationShortcut)
        self._shortcut_dbg.activated.connect(self._toggle_input_overlay)

        # ---- Auto-save timer ----
        # User-configurable via Settings: on/off + interval (1 / 5 / 10 min).
        # Initial values come from settings.json (loaded above).
        self._autosave_enabled = self._input_cal.autosave_enabled
        self._autosave_interval_min = self._input_cal.autosave_interval_min
        self._autosave_timer = QTimer(self)
        self._autosave_timer.setInterval(self._autosave_interval_min * 60 * 1000)
        self._autosave_timer.timeout.connect(self._auto_save)
        # The SaveWorker an autosave is currently running on, if any. Unlike a
        # user-initiated save there is no modal holding the app still while it
        # works, so every path that writes the same .lb has to be able to ask
        # whether one is in flight. See _wait_for_autosave.
        self._autosave_worker = None
        if self._autosave_enabled:
            self._autosave_timer.start()

        # ---- Signals ----
        self._canvas.page_changed.connect(self._on_page_changed)
        self._canvas.dirty_changed.connect(self._on_dirty_changed)
        theme_mgr.theme_changed.connect(self._on_theme_changed)

        # Click/touch outside an open Qt.Tool panel should dismiss it. Qt.Popup
        # did this for free but grabbed input (bad for touch), so we watch
        # presses app-wide instead and close the panel when the press lands
        # outside it. No input grab, touch-friendly.
        app = QApplication.instance()
        if app is not None:
            app.installEventFilter(self)

        # ---- Initial state ----
        self._apply_theme()
        self._update_page_label()

    # ================================================================
    # Theme
    # ================================================================

    def _apply_theme(self) -> None:
        t = theme_mgr.tokens
        # Scope to the main window's own class. An UNSCOPED rule
        # ("background: ...") cascades into child widgets — including modal
        # dialogs like the Media Manager — and was overriding their
        # transparent (aero) backgrounds with this opaque bg_window fill.
        self.setObjectName("LogicBoardWindow")
        self.setStyleSheet(
            f"QWidget#LogicBoardWindow {{ background: {t.bg_window}; "
            f"color: {t.text_primary}; }}"
        )

    def _on_theme_changed(self, tokens) -> None:
        self._apply_theme()
        # Theme switch changes the canvas background AND flips stroke colors,
        # so every cached page thumbnail is now stale. Wipe ALL thumbnails
        # (RAM + disk) and mark the current page dirty so it re-renders on
        # the next manager tick.
        if hasattr(self, "_thumb_manager"):
            self._thumb_manager.clear_ram()
            self._thumb_manager.clear_disk()
            self._thumb_manager.mark_current_dirty()
        # Panels read text/icon colors from the tokens only when they are
        # built. Any panel already open is showing the OLD theme's text color,
        # so close it — it'll pick up the new colors next time it's opened.
        # (The toolbar refreshes itself via its own theme_changed listener.)
        self._close_active_panel()
        # Refloat the toolbar (theme changes don't move it, but a safety call).
        self._position_toolbar()

    # ================================================================
    # Tool selection
    # ================================================================

    # ================================================================
    # Floating toolbar positioning
    # ================================================================
    def _position_toolbar(self) -> None:
        """Reposition the three floating toolbar groups along the bottom of
        the main window.

        Layout (each group is a top-level Qt.Tool window so they need
        explicit global coordinates):

            ┌─────────────────────────────────────────────────────────────┐
            │                                                             │
            │                         (canvas)                            │
            │                                                             │
            │ [Min Exit Sett]   [Sel Pen Eras | Undo Redo | Med File]   [< 1/4 >] │
            │                                                             │
            └─────────────────────────────────────────────────────────────┘
                 ↑                          ↑                          ↑
              left margin                 centered                right margin
        """
        if not hasattr(self, "_toolbar") or self._toolbar is None:
            return
        if not hasattr(self, "_toolbar_bottom_margin"):
            return

        from PySide6.QtCore import QPoint
        side_margin = UiScale.px(20)
        bottom_y_off = self._toolbar_bottom_margin

        # adjustSize() forces each group to size to its sizeHint BEFORE we
        # query .width() — otherwise the first frame after construction
        # reports a stale 100×30 default.
        for g in (self._toolbar.left, self._toolbar.center, self._toolbar.right):
            g.adjustSize()

        origin = self.mapToGlobal(QPoint(0, 0))
        win_w = self.width()
        win_h = self.height()

        # Each group's own height (they may differ slightly if button
        # contents differ in vertical extent; usually they match exactly).
        lw, lh = self._toolbar.left.sizeHint().width(),   self._toolbar.left.sizeHint().height()
        cw, ch = self._toolbar.center.sizeHint().width(), self._toolbar.center.sizeHint().height()
        rw, rh = self._toolbar.right.sizeHint().width(),  self._toolbar.right.sizeHint().height()

        # All three sit on the same Y baseline (their TOP edges align —
        # centre group is usually tallest, so use its height to compute Y
        # and let smaller groups sit a few px higher visually if needed).
        baseline_h = max(lh, ch, rh)
        y = origin.y() + max(0, win_h - baseline_h - bottom_y_off)

        # Left group: side_margin from window left edge.
        lx = origin.x() + side_margin
        self._toolbar.left.setGeometry(lx, y + (baseline_h - lh) // 2, lw, lh)

        # Center group: centered.
        cx = origin.x() + max(0, (win_w - cw) // 2)
        self._toolbar.center.setGeometry(cx, y + (baseline_h - ch) // 2, cw, ch)

        # Right group: anchored side_margin from window right edge.
        rx = origin.x() + max(0, win_w - rw - side_margin)
        self._toolbar.right.setGeometry(rx, y + (baseline_h - rh) // 2, rw, rh)

        # Collision avoidance: if the centre group would overlap left or
        # right on very narrow windows, slide it toward the wider side.
        # (Unlikely on a 1080p+ smartboard but a safety net.)
        if cx < lx + lw + side_margin:
            cx = lx + lw + side_margin
            self._toolbar.center.move(cx, y + (baseline_h - ch) // 2)
        if cx + cw > rx - side_margin:
            cx = rx - cw - side_margin
            self._toolbar.center.move(cx, y + (baseline_h - ch) // 2)

        if self._toolbar.isVisible():
            self._toolbar.raise_()

        # Tell the canvas how much of its bottom edge is being covered, so it
        # can put that much SCROLLABLE space after the last page. The toolbar
        # is a set of top-level Qt.Tool windows floating over the canvas, so
        # nothing in the layout knows it exists — without this the board
        # stopped scrolling with the final page flush to the bottom of the
        # window, i.e. underneath these pills, and its last couple of
        # centimetres could never be looked at.
        #
        # Measured here rather than guessed as a constant because the pill
        # height follows the UI scale, and a hard-coded figure would be wrong
        # on every board except the one it was measured on.
        try:
            self._canvas.set_bottom_inset(baseline_h + bottom_y_off
                                          + UiScale.px(16))
        except AttributeError:
            pass          # older canvas without the inset hook

    def resizeEvent(self, event):
        super().resizeEvent(event)
        self._position_toolbar()
        chip = getattr(self, "_paste_chip", None)
        if chip is not None and chip.isVisible():
            chip.reposition()

    def moveEvent(self, event):
        """When the main window is dragged to a new screen position, the
        floating top-level toolbar needs to follow. Without this the
        toolbar stays glued to its old global coordinates."""
        super().moveEvent(event)
        self._position_toolbar()

    def changeEvent(self, event):
        """Keep all floating UI in sync with the main window's state.

        The toolbar, the selection menu, and any open tool panel are Qt.Tool
        windows. When the main window is minimised they must hide so they
        don't float on screen alone; on restore they reappear (the selection
        menu only if a valid selection still exists).
        """
        super().changeEvent(event)
        from PySide6.QtCore import QEvent
        if event.type() == QEvent.WindowStateChange:
            minimized = self.isMinimized()
            # --- Toolbar ---
            if hasattr(self, "_toolbar") and self._toolbar is not None:
                if minimized:
                    self._toolbar.hide()
                else:
                    if not self._toolbar.isVisible():
                        self._toolbar.show()
                    self._position_toolbar()
            # --- Selection menu (hide on minimise; restore only if a
            #     selection still exists) ---
            sm = getattr(self, "_selection_menu", None)
            if sm is not None:
                if minimized:
                    sm.hide()
                else:
                    if self._canvas.get_selected_ids():
                        sm.show()
                        self._position_selection_menu()
            # --- Any open tool panel ---
            panel = getattr(self, "_active_panel", None)
            if panel is not None:
                if minimized:
                    panel.hide()
                elif not panel.isVisible():
                    panel.show()

    def showEvent(self, event):
        super().showEvent(event)
        self._position_toolbar()
        # Dark title bar + shadow on Windows (graceful no-op elsewhere).
        apply_dark_titlebar(self)
        enable_shadow(self)

    def _on_pen_button(self) -> None:
        """Pen toolbar button: switches to the remembered pen/highlighter mode,
        or opens the config panel if that mode is already active."""
        current = self._canvas.tool
        target = self._pen_tool_mode  # "pen" or "highlighter"
        if current == target:
            click_sound.play_click()
            self._show_pen_panel()
            return
        click_sound.play_click()
        # Leaving a tool retires its panel — see the note in _set_tool.
        self._close_active_panel()
        self._canvas.set_tool(target)
        self._toolbar.set_active_tool("pen")  # pen button is the active group
        self._sync_pen_button_ink()

    def _set_tool(self, tool: str) -> None:
        current = self._canvas.tool

        if current == tool:
            click_sound.play_click()
            if tool in ("pen", "highlighter", "brush"):
                self._show_pen_panel()
            elif tool == "eraser":
                self._show_eraser_panel()
            return

        click_sound.play_click()
        # Switching tools retires the panel belonging to the one being left.
        # A panel is the settings FOR a tool — an eraser panel floating over a
        # board the user is now drawing on with the pen is offering choices
        # that no longer apply to anything, and it sits over the canvas until
        # something unrelated happens to dismiss it. The press that changes
        # tool is the clearest possible statement that they are done with it.
        #
        # Only on this branch. A press that finds its own tool already active
        # falls through to the panel toggle above, and a mode change made
        # INSIDE the pen panel goes through _on_pen_mode_change → set_tool
        # directly — neither is the user leaving the tool, so neither closes
        # the panel they are working in.
        self._close_active_panel()
        self._canvas.set_tool(tool)
        self._toolbar.set_active_tool(tool)

    def _show_shapes_panel(self) -> None:
        click_sound.play_click()
        # Tapping the Shapes toolbar button should IMMEDIATELY activate the
        # last-used shape tool (so the user can draw right away), while ALSO
        # opening the panel for a different choice. This matches Pen/Eraser
        # behaviour where tapping the button activates the tool.
        cur = self._canvas.tool
        if cur not in self._shape_tools:
            # Use the last-selected shape (tracked by _shape_index), default rect.
            target = self._shape_tools[self._shape_index] \
                if 0 <= self._shape_index < len(self._shape_tools) else "rect"
            self._canvas.set_tool(target)
            self._toolbar.set_active_tool(target)

        self._close_active_panel()
        # Reflect the now-active shape in the panel's highlight.
        cur = self._canvas.tool if self._canvas.tool in self._shape_tools else "rect"
        panel = ShapesPanel(
            self,
            current_shape=cur,
            current_label=self._last_shape_label,
            on_select=self._on_shape_chosen,
            current_color=self._canvas.shape_color,
            on_color=self._on_shape_color,
        )
        self._active_panel = panel
        panel.show_anchored(self._toolbar.shapes_btn)

    def _on_shape_color(self, c) -> None:
        """The shapes panel's own colour column. Applies to the next shape
        drawn, and the Shapes button wears it the way Pen wears its ink."""
        self._canvas.set_shape_color(c)
        btn = getattr(self._toolbar, "shapes_btn", None)
        try:
            if btn is not None:
                btn.set_ink_color(self._canvas.shape_color)
        except Exception:
            logging.debug("shapes button ink sync failed", exc_info=True)

    def _on_shape_chosen(self, tool_id: str, emoji: str, label: str) -> None:
        self._canvas.set_tool(tool_id)
        self._toolbar.set_active_tool(tool_id)
        # Sticky display: replace the shapes button face with the chosen shape.
        # Use the SVG-aware setter so triangle.svg / rect.svg / etc render
        # crisp on the toolbar (with emoji fallback when no SVG exists).
        self._toolbar.set_shapes_display(tool_id, label,
                                          emoji_fallback=emoji or None)
        # Remember the exact label too (so Ellipse vs Circle survives).
        self._last_shape_label = label
        try:
            self._shape_index = self._shape_tools.index(tool_id)
        except ValueError:
            pass

    # ================================================================
    # Undo / Redo
    # ================================================================

    def _undo(self) -> None:
        click_sound.play_click()
        # Drop any selection first so its box/menu don't linger over objects
        # that the undo is about to change or remove (also covers Ctrl+Z, which
        # doesn't go through the toolbar press handler).
        self._dismiss_selection()
        self._canvas.undo()
        self._resync_all_ppt_srcs()

    def _redo(self) -> None:
        click_sound.play_click()
        self._dismiss_selection()
        self._canvas.redo()
        self._resync_all_ppt_srcs()

    def _resync_all_ppt_srcs(self) -> None:
        """After undo/redo, make every PPT object's src match its current slide
        index so the rendered slide image is correct."""
        scene = self._canvas.current_scene()
        if scene is None:
            return
        for i in range(scene.object_count):
            o = scene.object_at(i)
            if o and o.type_name == "ppt":
                self._sync_ppt_src(o)
        self._canvas.invalidate_cache()

    # ================================================================
    # Page navigation
    # ================================================================

    def _prev_page(self) -> None:
        click_sound.play_click()
        self._canvas.prev_page()

    def _next_page(self) -> None:
        click_sound.play_click()
        self._canvas.next_page()

    def _on_page_changed(self, current: int, total: int) -> None:
        self._update_page_label()
        # Selection doesn't carry across pages; hide its context menu.
        if getattr(self, "_selection_menu", None) is not None:
            self._selection_menu.hide()

    def _update_page_label(self) -> None:
        current = self._canvas.current_page + 1
        total = self._canvas.page_count
        text = f"{current:02d} / {total:02d}"
        self._toolbar.set_page_text(text)

    def _show_page_options(self) -> None:
        click_sound.play_click()
        self._close_active_panel()
        panel = PagesPanel(
            self._canvas,
            self,
            on_goto=self._goto_page,
            on_delete=self._delete_page,
            on_add=self._add_page,
            on_move=self._move_page,
            on_insert=self._insert_page,
            thumb_manager=self._thumb_manager,
        )
        self._active_panel = panel
        panel.show_right_side()

    def _goto_page(self, index: int) -> None:
        self._canvas.goto_page(index)
        self._update_page_display()

    def _delete_page(self, index: int) -> None:
        self._canvas.delete_page(index)
        self._update_page_display()

    def _add_page(self) -> None:
        self._canvas.add_page()
        self._update_page_display()

    def _move_page(self, src: int, dst: int) -> None:
        """Reorder after a drag in the pages panel.

        Every page between the two ends up at a different index, and the
        thumbnail manager caches by index — so without this invalidation
        the panel would redraw the stack in the new order while still
        showing each slot's OLD picture, which looks exactly like the
        reorder silently failed.
        """
        if not self._canvas.move_page(src, dst):
            return
        lo, hi = (src, dst) if src <= dst else (dst, src)
        self._invalidate_page_thumbs(range(lo, hi + 1))
        self._canvas.mark_dirty()
        self._update_page_display()

    def _insert_page(self, at: int) -> None:
        """Add a blank page at ``at``, pushing the rest down.

        Same thumbnail problem as a move, and worse: every page from the
        insertion point onward has shifted by one, so every cached preview
        past it is now attached to the wrong page.
        """
        if not self._canvas.insert_page_at(at):
            return
        self._invalidate_page_thumbs(range(at, self._canvas.page_count))
        self._canvas.mark_dirty()
        self._update_page_display()

    def _invalidate_page_thumbs(self, indices) -> None:
        mgr = getattr(self, "_thumb_manager", None)
        if mgr is None:
            return
        try:
            mgr.invalidate(list(indices))
        except Exception:
            logging.exception("thumbnail invalidate failed")

    def _update_page_display(self) -> None:
        try:
            cur = self._canvas.current_page + 1
            total = self._canvas.page_count
            self._toolbar.set_page_text(f"{cur:02d} / {total:02d}")
        except Exception:
            pass

    # ================================================================
    # Board folder / imports helpers
    # ================================================================
    def _ensure_board_dir(self):
        """Return the current board's folder, creating it (and imports/) if the
        board has been saved. If unsaved, create a temp working folder so imports
        still work and get migrated on first save."""
        # With the .lb workdir model, _current_board_path is ALWAYS set —
        # either to the extracted .lb's workdir (when a board is open) or to
        # an "unsaved_" workdir created by _new_board (when the user starts
        # fresh). The legacy tempfile.mkdtemp branch is gone.
        if self._current_board_path is None:
            # Defensive fallback — create a workdir directly. We must NOT call
            # _new_board() here: it starts with a _confirm_discard() prompt and
            # returns early if declined, leaving _current_board_path as None and
            # crashing Path(None) below. Just make a fresh unsaved workdir.
            wd = new_unsaved_workdir(self._saves_dir)
            self._current_board_path = wd
            self._canvas._board_dir = str(wd)
            self._thumb_manager.set_board_dir(wd)
            self._invalidate_media_library()
        d = Path(self._current_board_path)
        d.mkdir(parents=True, exist_ok=True)
        imports_util.ensure_imports_dir(d)
        return d

    # ================================================================
    # Media library (per-board)
    # ================================================================
    def _media_library(self) -> MediaLibrary:
        """Return the MediaLibrary instance for the current board folder.

        Creates one lazily and caches it on the instance. Re-created whenever
        the board folder changes (new/open/save-as)."""
        board_dir = self._ensure_board_dir()
        cached = getattr(self, "_media_lib_cache", None)
        cached_for = getattr(self, "_media_lib_dir", None)
        if cached is None or cached_for != board_dir:
            lib = MediaLibrary(Path(board_dir))
            lib.load()
            self._media_lib_cache = lib
            self._media_lib_dir = board_dir
        return self._media_lib_cache

    def _invalidate_media_library(self) -> None:
        """Force re-loading the MediaLibrary on next access."""
        self._media_lib_cache = None
        self._media_lib_dir = None

    # ================================================================
    # Media Manager (replaces the old Imports popup)
    # ================================================================
    def _show_imports_panel(self) -> None:
        """Open the Media Manager. (Method name kept for toolbar binding
        compatibility — the *panel* is the Media Manager dialog now.)"""
        click_sound.play_click()
        self._close_active_panel()

        lib = self._media_library()
        dlg = MediaManagerDialog(
            lib, self,
            on_browse_import=self._browse_and_import_media,
            on_set_background=self._media_set_background,
            on_set_whole_background=self._media_set_whole_background,
            on_add_object=self._media_add_object,
            on_reset_background=self._media_reset_background,
            get_background_info=self._media_background_info,
            on_background_step=self._media_background_set_step,
            on_media_deleted=self._media_deleted,
            on_remove_from_board=self._media_remove_from_board,
            on_render=self._media_render,
        )
        self._active_panel = dlg
        dlg.show()

    # ---- Render media that never rendered ----------------------------
    def _media_render(self, item_id: str) -> None:
        """The Media Manager's Render action: build the pages of a PDF or
        presentation from the original already on the board."""
        lib = self._media_library()
        item = lib.get(item_id)
        if item is None:
            return
        from ui.util.ppt_renderer import NO_POWERPOINT_MESSAGE, have_powerpoint
        if item.type == "ppt" and not have_powerpoint():
            item.render_error = "no_powerpoint"
            lib.save()
            AeroAlert.warning(self, "PowerPoint needed 💀", NO_POWERPOINT_MESSAGE)
            return
        from ui.util.import_progress import run_render_with_progress
        item = run_render_with_progress(
            self, lib, item, with_animations=self._input_cal.ppt_animations)
        if item is None:
            AeroAlert.warning(self, "Couldn't render 💀",
                              "The original file is missing from this board.")
            return
        if item.preview_count == 0:
            if item.render_error == "no_powerpoint":
                AeroAlert.warning(self, "PowerPoint needed 💀",
                                  NO_POWERPOINT_MESSAGE)
            else:
                AeroAlert.warning(self, "Couldn't render 💀",
                                  f"'{item.name}' still couldn't be rendered. "
                                  "Try opening it in PowerPoint or a PDF "
                                  "reader to check it isn't damaged.")
            return
        noun = "slide" if item.type == "ppt" else "page"
        AeroAlert.information(
            self, "Rendered! ✨",
            f"'{item.name}' is ready — {item.preview_count} {noun}"
            f"{'s' if item.preview_count != 1 else ''}.")

    def _warn_if_needs_powerpoint(self, item) -> None:
        """After an import that rendered nothing because PowerPoint is
        missing, say THAT — not that the file is broken."""
        if (item is not None and item.type == "ppt"
                and item.preview_count == 0
                and getattr(item, "render_error", "") == "no_powerpoint"):
            from ui.util.ppt_renderer import NO_POWERPOINT_MESSAGE
            AeroAlert.warning(self, "PowerPoint needed 💀",
                              NO_POWERPOINT_MESSAGE)

    # ---- Browse + import callback ----------------------------------
    def _browse_and_import_media(self) -> Optional[str]:
        """File-dialog browse → import each file. Returns the LAST new
        item id so the Media Manager can re-select it.

        PPT and PDF imports run on a background QThread with a frameless
        progress dialog. Images are imported inline (no rendering needed).
        """
        files, _ = QFileDialog.getOpenFileNames(
            self, "Import Media", "",
            "Media (*.png *.jpg *.jpeg *.bmp *.gif *.webp *.pdf *.ppt *.pptx)"
        )
        if not files:
            return None
        lib = self._media_library()
        last_id: Optional[str] = None
        for f in files:
            src = Path(f)
            ext = src.suffix.lower()
            try:
                if ext in (".ppt", ".pptx"):
                    item = run_ppt_import_with_progress(
                        self, lib, src,
                        with_animations=self._input_cal.ppt_animations)
                elif ext == ".pdf":
                    item = run_pdf_import_with_progress(self, lib, src)
                else:
                    # Images — no rendering, inline import is fine.
                    item = lib.import_file(src)
                if item is not None:
                    last_id = item.id
                    self._warn_if_needs_powerpoint(item)
            except Exception as e:
                logging.exception("Import failed for %s: %s", f, e)
        return last_id

    # ---- Add to board (selected slide/page, or whole item) ---------
    def _media_add_object(self, item_id: str, selected_index: int) -> None:
        lib = self._media_library()
        item = lib.get(item_id)
        if item is None:
            return
        # Make sure the cache exists (handles previously-imported items
        # whose cache was wiped externally).
        if item.type != "image":
            lib.ensure_cache(item_id)

        if item.type == "image":
            rel = item.original_relpath
            abs_path = lib.absolute_path(rel)
            self._add_image_object(rel, abs_path)
        elif item.type == "pdf":
            if selected_index >= 0:
                # Single page → image object referencing the cached SVG/WEBP.
                p = lib.page_path(item, selected_index)
                if p is not None:
                    rel = self._relpath_from_board(p)
                    self._add_image_object(rel, p)
            else:
                # Whole PDF → one image object per page, stacked diagonally.
                for i in range(item.preview_count):
                    p = lib.page_path(item, i)
                    if p is None:
                        continue
                    rel = self._relpath_from_board(p)
                    # Bulk stack: explicit origins, and NO per-page selection —
                    # selecting each one in turn would thrash the selection
                    # menu N times and leave only the last page selected.
                    self._add_image_object(rel, p, select=False,
                                           origin=(80 + i * 30, 80 + i * 30))
        elif item.type == "ppt":
            if selected_index >= 0:
                # Single slide → a navigable PptObject pinned to that slide,
                # starting at animation step 0 (initial state). This gives it
                # the animation step controls instead of a flat final image.
                self._add_ppt_object_from_item(item, start_slide=selected_index)
            else:
                # Whole deck → ONE navigable PPT object (with build steps).
                self._add_ppt_object_from_item(item)

        self._canvas.invalidate_cache()
        self._canvas.mark_dirty()


    # ---- Background fit modes (sidecar next to board.json) -------------
    # The engine's Scene has no field for this and board.json is written by
    # the C++ side, so the map rides in its own small file in the workdir.
    # The workdir is what gets zipped into the .lb, so it travels with the
    # board for free — no format change, and an older build just ignores a
    # file it doesn't know about.
    BG_FIT_SIDECAR = "bg_fit.json"

    def _save_bg_fit(self, wd) -> None:
        try:
            data = self._canvas.bg_fit_map()
            f = Path(wd) / self.BG_FIT_SIDECAR
            if not data:
                # Nothing chosen: don't leave a stale file claiming otherwise.
                if f.exists():
                    f.unlink()
                return
            f.write_text(json.dumps(data, indent=1), encoding="utf-8")
        except Exception:
            logging.exception("Could not write %s", self.BG_FIT_SIDECAR)

    def _load_bg_fit(self, wd) -> None:
        data = {}
        try:
            f = Path(wd) / self.BG_FIT_SIDECAR
            if f.exists():
                data = json.loads(f.read_text(encoding="utf-8"))
        except Exception:
            logging.exception("Could not read %s", self.BG_FIT_SIDECAR)
            data = {}
        self._canvas.load_bg_fit_map(data)

    # ---- Set as background ----------------------------------------
    def _media_set_background(self, item_id: str, selected_index: int,
                              fit_mode: str = "auto") -> None:
        """Put this media on the current page's background.

        ``fit_mode`` is how the picture is laid onto the page — "auto" (the
        long-standing behaviour), "fit" or "stretch". See
        ``CanvasWidget.BG_FIT_MODES``.
        """
        lib = self._media_library()
        item = lib.get(item_id)
        if item is None:
            return
        if item.type != "image":
            lib.ensure_cache(item_id)

        rel: Optional[str] = None
        if item.type == "image":
            rel = item.original_relpath
        elif item.type == "pdf":
            idx = max(0, selected_index)
            p = lib.page_path(item, idx)
            if p is not None:
                rel = self._relpath_from_board(p)
        elif item.type == "ppt":
            slide = max(0, selected_index)
            # Default to animation step 0 (the INITIAL presentation state).
            # The Media Manager controls then navigate between cached steps.
            p = lib.step_path(item, slide, 0)
            if p is not None:
                rel = self._relpath_from_board(p)

        if not rel:
            AeroAlert.warning(self, "Background didn't load 💀",
                "Couldn't set that as the background. Try a different image?")
            return
        scene = self._canvas.current_scene()
        stack = self._canvas.current_stack()
        if scene is not None and stack is not None:
            # Record the fit BEFORE the image lands, so the very first paint
            # after the change already uses it — otherwise the page flashes
            # the auto layout for a frame.
            self._canvas.set_bg_fit_mode(fit_mode, rel)
            stack.push_set_background(scene, rel)
            self._canvas.invalidate_cache()
            self._canvas.mark_dirty()


    def _seed_media_sizes(self, item, rels) -> None:
        """Tell the canvas how big these pages are, so it never has to look.

        The canvas needs each page's background dimensions to lay the board
        out. Finding them out means opening the file, and on a cold cache
        that measured 9 ms EACH — nine seconds of frozen UI for a
        thousand-page PDF, every time the layout was rebuilt.

        The renderer already produced these pages and knows exactly how big
        they are, so the number is handed over rather than rediscovered.
        Every page of one document shares a size, which is why one pair
        answers for all of them.
        """
        size = getattr(item, "page_size", None) or (0, 0)
        if not (len(size) == 2 and size[0] > 0 and size[1] > 0):
            return
        note = getattr(self._canvas, "note_image_size", None)
        if note is None:
            return
        for rel in rels:
            note(rel, size[0], size[1])

    def _media_set_whole_background(self, item_id: str,
                                    fit_mode: str = "auto") -> None:
        """Lay a whole PDF / PPT across the board — one page per frame, each
        with that frame as its background.

        ``fit_mode`` is the layout ticked in the Media Manager's split-button
        dropdown ("auto" = fill, "fit", "stretch") and is applied to EVERY page
        this creates — see ``CanvasWidget.BG_FIT_MODES``.

        THE MAPPING RULE, which is the whole point of this function:

            frame 0 -> the current page
            frame 1 -> the page after it
            frame 2 -> the page after that
            ...

        One rule, the same for every deck and every starting page. Board
        pages are created only when the mapping runs off the end of the
        board, and only at the end, where appending is what the engine
        supports.

        What it replaced: frame 0 went to the current page and every frame
        after it called ``add_page()``, which APPENDS. On a 10-page board
        opened at page 5, a 10-frame deck put frame 1 on page 5 and frames
        2..10 on new pages 11..19 — the deck arrived split in half with
        nine untouched pages sitting in the middle of it. The rule above
        cannot produce that, because the destination of frame i is a
        function of i alone.
        """
        lib = self._media_library()
        item = lib.get(item_id)
        if item is None or item.type not in ("ppt", "pdf"):
            return
        lib.ensure_cache(item_id)

        # Resolve every frame BEFORE touching the board — a half-applied
        # deck with some pages backed and some blank is worse than none.
        rels: list[str] = []
        for idx in range(max(0, item.preview_count)):
            p = (lib.page_path(item, idx) if item.type == "pdf"
                 else lib.step_path(item, idx, 0))
            if p is None:
                continue
            rel = self._relpath_from_board(p)
            if rel:
                rels.append(rel)
        if not rels:
            AeroAlert.warning(self, "Couldn't lay that out 💀",
                "None of those pages could be loaded. Try re-importing it?")
            return

        self._seed_media_sizes(item, rels)

        # Behind a busy window when there is enough of it to notice. A
        # thousand pages is about a second and a half of the app painting
        # nothing, which Windows relabels "Not Responding" and a teacher
        # reads as a crash.
        from ui.panels.busy_window import run_with_busy
        if len(rels) >= self._BUSY_PAGE_THRESHOLD:
            return run_with_busy(
                # Short on purpose: the ring shows the percentage, so the
                # label only has to say WHICH job is running.
                self, "Laying out pages…",
                lambda busy: self._apply_whole_background(item, rels,
                                                          fit_mode, busy))
        return self._apply_whole_background(item, rels, fit_mode, None)

    #: Page count past which an operation gets the busy window. Below it the
    #: work finishes inside a frame or two and a window would be a flash.
    _BUSY_PAGE_THRESHOLD = 60

    def _apply_whole_background(self, item, rels, fit_mode, busy) -> None:
        start = self._canvas.current_page
        # Grow the board FIRST, in one go, so that every destination page
        # exists before a single background is written.
        #
        # Doing it this way is what makes the mapping deterministic. The old
        # code grew the board inside the write loop and then wrote to
        # "whatever page add_page just made", so the destination of a frame
        # depended on the board's length at that moment rather than on the
        # frame's own index — which is how frames ended up scattered.
        # ``add_page`` appends, and appending is the only shape of growth the
        # engine exposes, so it is used only to reach the length the mapping
        # needs and never to choose a destination.
        needed = start + len(rels)
        while self._canvas.page_count < needed:
            self._canvas.add_page()

        # Which page each frame actually landed on — needed to invalidate
        # exactly those previews below.
        used_pages: list[int] = []
        for i, rel in enumerate(rels):
            page_i = start + i
            if page_i >= self._canvas.page_count:
                # Cannot happen after the growth above; if the engine ever
                # refuses a page, stop rather than write frames to the wrong
                # place. A short deck is recoverable, a shuffled one is not.
                logging.error("whole-media bg: board is %d pages, needed %d",
                              self._canvas.page_count, needed)
                break
            self._canvas.goto_page(page_i)
            scene = self._canvas.current_scene()
            stack = self._canvas.current_stack()
            if scene is None or stack is None:
                continue
            # Record the fit BEFORE the image lands so the first paint already
            # uses it, same ordering as _media_set_background.
            self._canvas.set_bg_fit_mode(fit_mode, rel)
            stack.push_set_background(scene, rel)
            used_pages.append(page_i)
            # Let the busy window paint. Every 25 pages rather than every
            # page: pumping the event loop is not free, and 25 pages is
            # well under a frame's worth of work.
            if busy is not None and (i % 25) == 24:
                # A percentage, not "Laying out page 250 of 1,000…". That
                # message is 319 px of text in a 136 px strip — it lost its
                # last third on every board it was ever shown for. The
                # number goes inside the ring instead, where four characters
                # cannot overflow. See busy_window.
                busy.set_progress(i + 1, len(rels))
                busy.tick()

        # Every page that just got a new background needs its preview thrown
        # away — the page navigator caches them per index, and index reuse (a
        # board that previously had pages at these numbers) would otherwise
        # show the OLD page under the new slide.
        thumb_mgr = getattr(self, "_thumb_manager", None)
        if thumb_mgr is not None and used_pages:
            try:
                thumb_mgr.invalidate(used_pages)
            except Exception:
                logging.exception("thumb invalidate after whole-media bg")

        self._canvas.goto_page(start)
        self._canvas.invalidate_cache()
        self._canvas.mark_dirty()
        self._update_page_label()
        logging.info("Whole-media background: %s -> %d page(s) [fit=%s]",
                     item.name, len(rels), fit_mode)

    # ---- Remove from board (Media Manager) -------------------------
    def _media_remove_from_board(self, item_id: str) -> None:
        """Take a media item off this board, keeping the media itself.

        The board-side work is EXACTLY what deleting does — every placed copy
        removed through the page's undo stack, every page background that
        pointed at it cleared, affected thumbnails invalidated — so it reuses
        ``_purge_media_references`` rather than growing a second implementation
        that would drift out of step with it.

        What it deliberately does NOT do is the other half of delete: no
        ``library.delete()``, no cache eviction, no file removal. The item
        stays in the Media Manager and can be placed again immediately. That
        difference is the whole feature, and keeping it to "call one of the
        two halves" is what makes it hard to get wrong later.

        The Media Manager has already confirmed with the user by the time this
        runs — see ``_on_remove_from_board_requested``.
        """
        lib = self._media_library()
        item = lib.get(item_id)
        if item is None:
            return
        objs, bgs = self._purge_media_references(item)
        # Repaint and re-thumbnail the page in view. _purge_media_references
        # invalidates the canvas cache and the OFF-page thumbnails, but the
        # open page's own preview is rendered on a different path — the same
        # gap _media_deleted closes after a delete.
        try:
            self._canvas.invalidate_cache()
            self._canvas.update()
            thumb_mgr = getattr(self, "_thumb_manager", None)
            if thumb_mgr is not None:
                thumb_mgr.invalidate([self._canvas.current_page])
        except Exception:
            logging.exception("refresh after remove-from-board")
        self._update_page_label()
        logging.info("Removed '%s' from the board: %d object(s), "
                     "%d background(s). Library untouched.",
                     item.name, objs, bgs)

    # ---- Media deleted (Media Manager) -----------------------------
    def _media_deleted(self, item_id: str) -> None:
        """A media item was just deleted from the library.

        ``_purge_media_references`` has already stripped it from every page and
        invalidated those pages' previews. What's left is to make the change
        VISIBLE: repaint the canvas, re-render the open page's thumbnail, and
        refresh the page navigator if it happens to be on screen.

        No page rasterising happens here — the panel re-fetches lazily and only
        for cards in the viewport, so this stays instant on a 100-page board.
        """
        thumb_mgr = getattr(self, "_thumb_manager", None)
        if thumb_mgr is not None:
            try:
                thumb_mgr.refresh_current_soon()
            except Exception:
                logging.exception("refresh_current_soon after media delete")
        try:
            self._canvas.invalidate_cache()
            self._canvas.update()
        except Exception:
            pass
        panel = getattr(self, "_active_panel", None)
        if (panel is not None and hasattr(panel, "refresh_previews")
                and panel.isVisible()):
            try:
                panel.refresh_previews()
            except Exception:
                logging.exception("pages panel refresh after media delete")

    # ---- Background info + reset (Media Manager) -------------------
    def _media_background_info(self) -> dict:
        """Describe the current page's background for the Media Manager's
        Background Information section."""
        scene = self._canvas.current_scene()
        if scene is None:
            return {"title": "Default Background", "detail": "", "has_bg": False}
        has_bg = bool(getattr(scene, "has_bg_image", False))
        if not has_bg:
            return {"title": "Default Background", "detail": "", "has_bg": False}
        rel = scene.bg_image or ""
        info = self._media_library().describe_background(rel)
        info["has_bg"] = True
        # Animation step info for PPT-slide backgrounds (None for others).
        anim = self._media_library().background_anim_info(rel)
        if anim and anim.get("total_steps", 1) > 1:
            info["anim"] = anim
        return info

    def _media_background_set_step(self, item_id: str, slide: int, step: int) -> None:
        """Swap the page background to a different cached animation step of the
        same PPT slide. No rendering — just points the background at the
        already-cached WEBP for that step."""
        lib = self._media_library()
        item = lib.get(item_id)
        if item is None or item.type != "ppt":
            return
        max_step = item.anim_counts[slide] if 0 <= slide < len(item.anim_counts) else 0
        step = max(0, min(step, max_step))
        p = lib.step_path(item, slide, step)
        if p is None:
            return
        rel = self._relpath_from_board(p)
        scene = self._canvas.current_scene()
        stack = self._canvas.current_stack()
        if scene is not None and stack is not None and rel:
            stack.push_set_background(scene, rel)
            self._canvas.invalidate_cache()
            self._canvas.mark_dirty()

    def _media_reset_background(self) -> None:
        """Clear the page's background, restoring the default."""
        scene = self._canvas.current_scene()
        if scene is None:
            return
        try:
            # Same reasoning as the purge path: a page with ink on it keeps
            # the height its background gave it.
            self._pin_page_height_before_bg_loss(scene)
            scene.clear_bg_image()
            try:
                scene.mark_dirty()
            except Exception:
                pass
            self._canvas.invalidate_cache()
            self._canvas.mark_dirty()
            logging.info("Background reset to default for current page.")
        except Exception as e:
            logging.exception("Reset background failed: %s", e)

    # ---- helper: path → board-relative posix string ----------------
    def _relpath_from_board(self, abs_path: Path) -> str:
        board_dir = self._ensure_board_dir()
        try:
            return Path(abs_path).resolve().relative_to(
                Path(board_dir).resolve()).as_posix()
        except Exception:
            return str(abs_path)

    # ---- Add PPT object from a MediaItem ---------------------------
    def _add_ppt_object_from_item(self, item: MediaItem, start_slide: int = 0) -> None:
        """Create a navigable PptObject backed by the SVG/WEBP build-step
        cache. The object starts on ``start_slide`` at animation step 0 (the
        INITIAL presentation state) and ``src`` points at that step's cached
        file; navigation updates ``src`` to the file for the new (slide, step).
        No rendering happens at runtime — only a cached-image swap."""
        if not item.step_files:
            AeroAlert.warning(self, "PowerPoint missing 💀",
                "That PowerPoint hasn't been rendered yet. Try re-importing it.")
            return

        import logicboard_engine as lbe
        lib = self._media_library()
        scene = self._canvas.current_scene()
        stack = self._canvas.current_stack()
        if scene is None or stack is None:
            return

        # Size: fit within 70 % of the canvas. Use the FIRST cached file's
        # natural size if possible (SVG renders sharper to a known ratio).
        first_step_file = item.step_files[0][0] if item.step_files[0] else None
        cw = scene.canvas_width or 1920
        ch = scene.canvas_height or 1080
        iw, ih = 960, 540
        if first_step_file:
            first_abs = lib.item_cache_dir(item) / first_step_file
            iw, ih = self._natural_size_of(first_abs, (960, 540))
        scale = min(cw * 0.7 / iw, ch * 0.7 / ih, 1.0)
        w, h = iw * scale, ih * scale
        ox, oy = (cw - w) / 2.0, (ch - h) / 2.0

        # src = first step of slide 0 (board-relative).
        first_abs = lib.item_cache_dir(item) / first_step_file if first_step_file else None
        first_rel = self._relpath_from_board(first_abs) if first_abs else ""
        ppt = lbe.PptObject(first_rel, lbe.Vec2f(ox, oy), lbe.Vec2f(w, h))
        ppt.slide_count = item.preview_count
        ppt.current_slide = max(0, min(int(start_slide), max(0, item.preview_count - 1)))
        ppt.anim_step = 0
        try:
            ppt.anim_counts = list(item.anim_counts)
        except Exception:
            pass

        # Track the binding (object id → media item id) so navigation can
        # look up the right file later.
        self._track_ppt_binding(ppt, item.id)
        stack.push_add(scene, ppt)
        # push_add stores a CLONE — operate on the scene's copy, not the local
        # object, so the src actually updates on the rendered object.
        scene_obj = scene.find_mut(ppt.id)
        self._sync_ppt_src(scene_obj if scene_obj is not None else ppt)

    # ---- PPT object ↔ MediaItem binding ---------------------------
    def _track_ppt_binding(self, ppt_obj, item_id: str) -> None:
        """Remember which MediaItem backs each PPT object on the current
        canvas. Survives via per-object 'src' parsing — see
        :py:meth:`_resolve_ppt_item` — but the in-memory cache is faster."""
        if not hasattr(self, "_ppt_to_item"):
            self._ppt_to_item = {}
        self._ppt_to_item[str(ppt_obj.id)] = item_id

    def _resolve_ppt_item(self, ppt_obj) -> Optional[MediaItem]:
        """Find the MediaItem backing a PPT object. Uses the in-memory binding
        when available; otherwise infers from the object's src path
        (`media/cache/<id>/...`)."""
        if not hasattr(self, "_ppt_to_item"):
            self._ppt_to_item = {}
        lib = self._media_library()
        oid_key = str(ppt_obj.id)
        item_id = self._ppt_to_item.get(oid_key)
        if item_id:
            it = lib.get(item_id)
            if it is not None:
                return it
        # Fall back: parse src ("media/cache/<id>/...").
        src = ppt_obj.src or ""
        prefix = f"{lib.media_dir.name}/{lib.cache_dir.name}/"
        if src.startswith(prefix):
            rest = src[len(prefix):]
            cand_id = rest.split("/", 1)[0]
            it = lib.get(cand_id)
            if it is not None:
                self._ppt_to_item[oid_key] = it.id
                return it
        return None

    # ────────────────────────────────────────────────────────────────────
    # Media cleanup — used by the Media Manager's Delete flow
    # ────────────────────────────────────────────────────────────────────
    def _object_references_item(self, obj, item) -> bool:
        """Return True if ``obj`` was placed from ``item``.

        Match rule: the object's ``src`` (a board-relative path) either
        equals the item's original file path (images placed from the
        original) OR sits inside the item's cache directory (PDF pages,
        PPT slides, animation steps).
        """
        try:
            src = (obj.src or "").replace("\\", "/").strip("/")
        except Exception:
            return False
        if not src:
            return False
        orig = (item.original_relpath or "").replace("\\", "/").strip("/")
        cache = (item.cache_dir_relpath or "").replace("\\", "/").strip("/")
        if orig and src == orig:
            return True
        if cache and (src == cache or src.startswith(cache + "/")):
            return True
        return False

    def _scene_bg_references_item(self, scene, item) -> bool:
        try:
            if not scene.has_bg_image:
                return False
            bg = (scene.bg_image or "").replace("\\", "/").strip("/")
        except Exception:
            return False
        if not bg:
            return False
        orig = (item.original_relpath or "").replace("\\", "/").strip("/")
        cache = (item.cache_dir_relpath or "").replace("\\", "/").strip("/")
        if orig and bg == orig:
            return True
        if cache and (bg == cache or bg.startswith(cache + "/")):
            return True
        return False

    def _count_media_usages(self, item) -> tuple[int, int]:
        """Walk every page of the board and count references to ``item``.

        Returns ``(object_count, background_count)``. Counts are board-wide
        — not just the current page — so the deletion warning can tell the
        teacher exactly what's about to disappear.
        """
        board = self._canvas.board if self._canvas else None
        if board is None:
            return (0, 0)
        obj_count = 0
        bg_count  = 0
        try:
            for page_i in range(board.page_count):
                scene = board.page_mut(page_i)
                if scene is None:
                    continue
                if self._scene_bg_references_item(scene, item):
                    bg_count += 1
                for i in range(scene.object_count):
                    obj = scene.object_at(i)
                    if obj and self._object_references_item(obj, item):
                        obj_count += 1
        except Exception:
            logging.exception("_count_media_usages failed")
        return (obj_count, bg_count)

    def _pin_page_height_before_bg_loss(self, scene) -> bool:
        """Freeze a page's current height before its background is taken away.

        A tall background — a scanned page, a PDF frame — makes the page
        TALLER than the default canvas. Nothing about that height is stored:
        ``_sheet_size`` and ``_compute_content_height`` both derive it by
        measuring the background image every time they are asked. So the
        moment the background goes, the page snaps back to ``canvas_height``,
        and any ink the teacher drew down in the extended part is suddenly
        outside the page — clipped away in list style, and the sheet visibly
        changing shape underneath the strokes that are left.

        The work was never recoverable either: the strokes still hold their
        original coordinates, so they are not deleted, just unreachable, and
        the page looks like it ate them.

        So before the background is cleared, the height it was giving the page
        is written into the page itself, where it becomes real saved data
        rather than something re-derived from an image that no longer exists.

        ONLY IF THERE IS INK. An untouched page whose background is removed
        should go back to normal — pinning every page at whatever height some
        since-deleted PDF happened to be would leave a board full of oddly
        tall blank pages with no way to undo it.

        ``_compute_content_height`` is the measurement, because it already
        accounts for BOTH the background's extent and the lowest object on the
        page. Ink can end up below the background's own bottom edge in
        one-page style, where the view scrolls past it, and pinning only to
        the image would clip exactly that ink.

        Returns True if the page was pinned. Not routed through the undo
        stack, deliberately: the background clear it accompanies is not either
        (see ``_purge_media_references``), and a resize that could be undone
        while the strokes it protects stayed put would reintroduce the bug it
        exists to prevent.
        """
        try:
            if scene is None or scene.object_count <= 0:
                return False          # nothing to protect
            cw = float(scene.canvas_width or 0.0)
            ch = float(scene.canvas_height or 0.0)
            if cw <= 0.0 or ch <= 0.0:
                return False
            needed = float(self._canvas._compute_content_height(scene))
            # A hair of slack, so floating-point noise in the measurement
            # cannot pin a page one pixel taller on every single removal.
            if needed <= ch + 1.0:
                return False
            # set_canvas_size takes INTS. Handing it the floats these
            # measurements produce raises TypeError, which the except below
            # would swallow — the page would quietly go unpinned and the bug
            # would look unfixed. Round UP so the pin can never land a
            # fraction of a pixel short of the ink it is protecting.
            scene.set_canvas_size(int(math.ceil(cw)), int(math.ceil(needed)))
            logging.info("Pinned page height %.0f -> %.0f before its "
                         "background was removed (%d object(s) on the page).",
                         ch, needed, scene.object_count)
            return True
        except Exception:
            logging.exception("Could not pin page height before bg removal")
            return False

    def _purge_media_references(self, item) -> tuple[int, int]:
        """Remove every object that references ``item`` AND clear any page
        background pointing at it. Each removal goes through the page's
        undo stack so the whole operation can be undone (one entry per
        affected page is the cleanest grouping the engine API allows).

        Returns ``(objects_removed, backgrounds_cleared)``.

        There are two subtle engine quirks we work around here:

        1. ``Scene::clear_bg_image()`` in the C++ engine is a header-inline
           that just does ``bg_image_.clear();`` — it does NOT call
           ``mark_dirty()`` (see engine/include/scene/Scene.h:72, where the
           method is defined without the dirty flag flip that `remove()`
           does). Without the dirty flag, the render cache won't invalidate
           the next time we look at that page, and the (now-stale) baked-in
           background would still appear. Fix: explicitly call
           ``scene.mark_dirty()`` right after clearing.

        2. The canvas render cache is single-page (``_scene_cache`` is one
           pixmap for the OPEN page), and the thumbnail manager caches per
           page. Invalidating the canvas cache handles the current page,
           but stale thumbnails for off-current pages would linger until
           you navigate to them. Fix: mark every affected page dirty on
           the thumbnail manager so it re-renders them on the next tick.
        """
        board = self._canvas.board if self._canvas else None
        if board is None:
            # No board = nothing to purge. Also naturally covers the
            # "engine failed to load" case since the canvas can't hold a
            # board without the engine binding.
            return (0, 0)
        objs_removed = 0
        bgs_cleared  = 0
        # Track exactly which pages we touched so we can invalidate their
        # thumbnails afterwards. A page might not have been directly
        # visited by the loop (if the board grew during purge), but every
        # page we DID touch gets its thumb refreshed.
        touched_pages: set[int] = set()
        try:
            for page_i in range(board.page_count):
                scene = board.page_mut(page_i)
                if scene is None:
                    continue
                # Command stacks live on the canvas (one per page), NOT on
                # MainWindow — see e.g. main_window.py:2147 which appends to
                # ``self._canvas._command_stacks``. The old
                # ``self._command_stacks`` reference here was inherited
                # from an earlier draft and crashed with AttributeError on
                # every actual delete.
                stacks = getattr(self._canvas, "_command_stacks", [])
                stack = stacks[page_i] if page_i < len(stacks) else None
                # Collect ids first — mutating the scene's object list while
                # iterating it would shift indices under us.
                ids_to_remove: list = []
                for i in range(scene.object_count):
                    obj = scene.object_at(i)
                    if obj and self._object_references_item(obj, item):
                        ids_to_remove.append(obj.id)
                for oid in ids_to_remove:
                    try:
                        if stack is not None:
                            stack.push_delete(scene, oid)
                            objs_removed += 1
                            touched_pages.add(page_i)
                        # Also drop the in-memory PPT binding if present
                        # (defensive — the engine call already removed the
                        # object, but we don't want the binding map keeping
                        # a stale id alive).
                        try:
                            self._ppt_to_item.pop(str(oid), None)
                        except Exception:
                            pass
                    except Exception:
                        logging.exception("delete oid=%s on page=%s", oid, page_i)
                # Background — clear if it pointed at this item.
                if self._scene_bg_references_item(scene, item):
                    try:
                        # Keep the height this background was giving the page,
                        # so ink drawn in the extended area does not fall
                        # outside the page when the image goes.
                        self._pin_page_height_before_bg_loss(scene)
                        scene.clear_bg_image()
                        # Work around the engine quirk described in the
                        # docstring — clear_bg_image doesn't flip dirty,
                        # so we do it manually.
                        try:
                            scene.mark_dirty()
                        except Exception:
                            # Older engine binding without mark_dirty
                            # exposed — falls back to canvas-side
                            # invalidate_cache() below, which is still
                            # sufficient for the current page.
                            pass
                        bgs_cleared += 1
                        touched_pages.add(page_i)
                    except Exception:
                        logging.exception("clear_bg_image on page=%s", page_i)
        except Exception:
            logging.exception("_purge_media_references failed")
        # Rebuild caches across affected pages and refresh the current view.
        try:
            self._canvas.invalidate_cache()
            self._canvas.mark_dirty()
        except Exception:
            pass
        # Invalidate the thumbnail for every page we actually modified —
        # otherwise the sidebar / page-picker would show stale previews
        # containing the removed content until the next automatic sweep.
        thumb_mgr = getattr(self, "_thumb_manager", None)
        if thumb_mgr is not None:
            try:
                # invalidate() drops the RAM copy AND deletes the cached WEBP.
                # mark_dirty() alone only set an in-memory flag, so the stale
                # preview came back from disk the next time the board was
                # opened — the deleted media reappearing in the page
                # navigator, on a board where it no longer exists anywhere.
                thumb_mgr.invalidate(touched_pages)
            except Exception:
                logging.exception("thumb invalidate pages=%s", touched_pages)
            # If the current page was touched, kick an immediate refresh
            # rather than waiting for the debounce / periodic tick.
            try:
                if self._canvas.current_page in touched_pages \
                        and hasattr(thumb_mgr, "refresh_current_soon"):
                    thumb_mgr.refresh_current_soon()
            except Exception:
                pass
        # Force one repaint of the canvas right now — the invalidate above
        # sets the flag, but on some platforms the widget doesn't repaint
        # until the next event loop tick, which is enough of a delay that
        # the user sees the deleted media flash before it disappears.
        try:
            self._canvas.update()
        except Exception:
            pass
        return (objs_removed, bgs_cleared)

    @staticmethod
    def _natural_size_of(path: Path, default: tuple) -> tuple:
        """Return (w, h) for an SVG/WEBP/PNG. Falls back to ``default`` on
        failure (e.g. file missing)."""
        path = Path(path)
        if not path.exists():
            return default
        low = path.suffix.lower()
        try:
            if low == ".svg":
                from PySide6.QtSvg import QSvgRenderer
                r = QSvgRenderer(str(path))
                if r.isValid():
                    s = r.defaultSize()
                    if s.width() > 0 and s.height() > 0:
                        return (s.width(), s.height())
            from PySide6.QtGui import QImage
            img = QImage(str(path))
            if not img.isNull():
                return (img.width(), img.height())
        except Exception:
            pass
        return default

    # ================================================================
    # Legacy image import (still used by image-add helpers above)
    # ================================================================
    def _import_image(self) -> None:
        files, _ = QFileDialog.getOpenFileNames(
            self, "Import Image(s)", "",
            "Images (*.png *.jpg *.jpeg *.bmp *.gif *.webp *.tiff)",
        )
        if not files:
            return
        lib = self._media_library()
        for fpath in files:
            try:
                item = lib.import_file(Path(fpath))
                if item is not None:
                    rel = item.original_relpath
                    self._add_image_object(rel, lib.absolute_path(rel))
            except Exception as e:
                logging.exception("Import image failed: %s", e)
        self._canvas.invalidate_cache()
        self._canvas.mark_dirty()

    def _add_image_object(self, rel_src: str, source_for_size: Optional[Path] = None,
                          origin=None, size=None, select: bool = True) -> None:
        import logicboard_engine as lbe
        scene = self._canvas.current_scene()
        stack = self._canvas.current_stack()
        if scene is None or stack is None:
            return
        # Determine a sensible default size from the image's natural dimensions.
        w, h = 600.0, 400.0
        if source_for_size is not None:
            try:
                from PySide6.QtGui import QImage
                img = QImage(str(source_for_size))
                if not img.isNull():
                    iw, ih = img.width(), img.height()
                    cw = scene.canvas_width or 1920
                    ch = scene.canvas_height or 1080
                    # Fit within ~60% of canvas while preserving aspect.
                    max_w, max_h = cw * 0.6, ch * 0.6
                    scale = min(max_w / iw, max_h / ih, 1.0)
                    w, h = iw * scale, ih * scale
            except Exception:
                pass
        if size is not None:
            w, h = size
        if origin is None:
            # Centre of what the user is LOOKING AT, not the centre of the
            # canvas. Those differ the moment the board is scrolled, and the
            # canvas centre could land the image off-screen entirely.
            # screen_to_scene folds in scroll and view scale for us.
            try:
                from PySide6.QtCore import QPointF as _QPointF
                c = self._canvas.screen_to_scene(
                    _QPointF(self._canvas.width() / 2.0,
                             self._canvas.height() / 2.0))
                origin = (c.x() - w / 2.0, c.y() - h / 2.0)
            except Exception:
                cw = scene.canvas_width or 1920
                ch = scene.canvas_height or 1080
                origin = ((cw - w) / 2.0, (ch - h) / 2.0)
        img_obj = lbe.ImageObject(rel_src, lbe.Vec2f(origin[0], origin[1]),
                                  lbe.Vec2f(w, h))
        stack.push_add(scene, img_obj)

        # Hand it over SELECTED so the user can drag it straight into place
        # without hunting for it with the select tool first. push_add clones
        # via clone_exact(), which preserves the id (same property the
        # duplicate path relies on), so this id is the one now in the scene.
        if select:
            try:
                from PySide6.QtCore import QRectF as _QRectF
                self._canvas.set_tool("select")
                self._toolbar.set_active_tool("select")
                self._canvas.set_selection(
                    [img_obj.id],
                    _QRectF(origin[0], origin[1], w, h))
            except Exception:
                logging.debug("select-after-add failed", exc_info=True)

    def _import_pdf(self) -> None:
        """Legacy direct PDF import — runs through the progress dialog so the
        GUI stays responsive while PyMuPDF renders pages to WEBP."""
        fpath, _ = QFileDialog.getOpenFileName(
            self, "Import PDF", "", "PDF Files (*.pdf)")
        if not fpath:
            return
        try:
            import fitz   # noqa: F401
        except ImportError:
            AeroAlert.warning(
                self, "PDF support needs PyMuPDF 💀",
                "PDF import needs PyMuPDF.\n\n"
                "Install it with:\n    pip install PyMuPDF")
            return
        lib = self._media_library()
        try:
            item = run_pdf_import_with_progress(self, lib, Path(fpath))
        except Exception as e:
            logging.exception("PDF import failed: %s", e)
            item = None
        if item is None or item.preview_count == 0:
            AeroAlert.warning(self, "PDF didn't import 💀",
                "Couldn't read any pages from that PDF. Is the file valid?")
            return
        # Add each page as an image object pointing at the cache file.
        for i in range(item.preview_count):
            p = lib.page_path(item, i)
            if p is None:
                continue
            rel = self._relpath_from_board(p)
            self._add_image_object(rel, p,
                                   origin=(80 + i * 30, 80 + i * 30))
        self._canvas.invalidate_cache()
        self._canvas.mark_dirty()
        AeroAlert.information(self, "PDF imported! ✨",
                                f"Brought in {item.preview_count} page(s) as images. 📁")

    def _import_ppt(self, as_object: bool) -> None:
        """Legacy direct PPT import. PowerPoint rendering now runs through
        pywin32 COM on a background thread with a progress dialog."""
        fpath, _ = QFileDialog.getOpenFileName(
            self, "Import PowerPoint", "",
            "PowerPoint (*.ppt *.pptx)")
        if not fpath:
            return
        try:
            from ui.util.ppt_renderer import have_powerpoint
        except Exception:
            have_powerpoint = lambda: False        # type: ignore
        if not have_powerpoint():
            from ui.util.ppt_renderer import NO_POWERPOINT_MESSAGE
            AeroAlert.warning(self, "PowerPoint needed 💀",
                              NO_POWERPOINT_MESSAGE)
            return
        lib = self._media_library()
        try:
            item = run_ppt_import_with_progress(
                self, lib, Path(fpath),
                with_animations=self._input_cal.ppt_animations)
        except Exception as e:
            logging.exception("PPT import failed: %s", e)
            item = None
        if item is None or item.preview_count == 0:
            if getattr(item, "render_error", "") == "no_powerpoint":
                from ui.util.ppt_renderer import NO_POWERPOINT_MESSAGE
                AeroAlert.warning(self, "PowerPoint needed 💀",
                                  NO_POWERPOINT_MESSAGE)
            else:
                AeroAlert.warning(self, "PowerPoint didn't import 💀",
                                  "Couldn't read any slides from that file. "
                                  "Try opening it in PowerPoint first?")
            return

        if as_object:
            self._add_ppt_object_from_item(item)
        else:
            # Each slide as a separate image object (final build step).
            for i in range(item.preview_count):
                step_idx = (item.anim_counts[i]
                            if i < len(item.anim_counts) else 0)
                p = lib.step_path(item, i, step_idx)
                if p is None:
                    continue
                rel = self._relpath_from_board(p)
                self._add_image_object(rel, p,
                                       origin=(80 + i * 30, 80 + i * 30))

        self._canvas.invalidate_cache()
        self._canvas.mark_dirty()
        AeroAlert.information(
            self, "PowerPoint imported! ✨",
            f"Brought in {item.preview_count} slide(s)"
            + (" as a navigable PPT object. 📁" if as_object else " as images. 📁"))

    # ================================================================
    # Files opened WITH LogicBoard (double-click / Explorer "Open with")
    # ================================================================
    #: What a file opened from Windows can be. .lb opens as a board; the
    #: other three are documents that become pages on a board.
    DOCUMENT_EXTS = (".pdf", ".pptx", ".ppt")

    def open_external_file(self, path: Path) -> None:
        """Open a file Windows handed to LogicBoard.

        A board opens. A PDF or deck asks where it should go — a new board,
        or a saved one — and arrives as pages, one per page or slide, fitted
        to the canvas: the same thing "Set Whole Media As Bg" does from the
        Media Manager, because that is what a teacher opening a PDF on a
        smart board wants — pages to write on, not a picture to drag about.
        """
        path = Path(path)
        ext = path.suffix.lower()
        if not path.is_file():
            AeroAlert.warning(self, "File not found 💀",
                              f"'{path.name}' is not there any more.")
            return
        if ext == LB_EXTENSION:
            self._open_board(path)
            return
        if ext not in self.DOCUMENT_EXTS:
            AeroAlert.warning(self, "Can't open that 💀",
                              f"LogicBoard can't open '{path.name}'.")
            return

        # Can this machine read it at all? Asked BEFORE "where should it
        # go", so nobody picks a board and then hears it was never possible.
        if ext == ".pdf":
            try:
                import fitz   # noqa: F401
            except ImportError:
                AeroAlert.warning(self, "PDF support needs PyMuPDF 💀",
                                  "This copy of LogicBoard can't read PDFs.")
                return
        else:
            try:
                from ui.util.ppt_renderer import have_powerpoint
            except Exception:
                have_powerpoint = lambda: False        # type: ignore
            if not have_powerpoint():
                from ui.util.ppt_renderer import NO_POWERPOINT_MESSAGE
                AeroAlert.warning(self, "PowerPoint needed 💀",
                                  NO_POWERPOINT_MESSAGE)
                return

        self._close_active_panel()
        from ui.panels.open_into_dialog import OpenIntoDialog, NEW_BOARD
        choice = OpenIntoDialog.ask(path, self,
                                    current_board=self._current_lb_path)
        if choice is None:
            return
        if not self._switch_to_board_for(choice, NEW_BOARD):
            return
        self._import_document_as_pages(path, new_board=(choice == NEW_BOARD))

    def _switch_to_board_for(self, choice, new_marker) -> bool:
        """Make the chosen board the open one. False if the user backed out
        (Cancel on "unsaved changes") or the board would not open — in which
        case nothing has been imported anywhere."""
        if choice == new_marker:
            before = self._current_board_path
            self._new_board()
            return self._current_board_path != before
        target = Path(choice)
        cur = self._current_lb_path
        if cur is not None and Path(cur).resolve() == target.resolve():
            return True                     # already open — add to it as is
        self._open_board(target)
        cur = self._current_lb_path
        return cur is not None and Path(cur).resolve() == target.resolve()

    def _page_is_blank(self, i: int) -> bool:
        try:
            scene = self._canvas.scene_at(i)
            return (scene is not None and scene.object_count == 0
                    and not getattr(scene, "has_bg_image", False))
        except Exception:
            return False

    def _import_document_as_pages(self, path: Path, *, new_board: bool) -> None:
        """Bring a PDF / deck in, one board page per page or slide.

        Into a saved board the pages go AFTER its last page — onto that last
        page itself only when it is still blank, so a board never ends up
        with an empty page sitting between the old work and the new.
        """
        lib = self._media_library()
        try:
            if path.suffix.lower() == ".pdf":
                item = run_pdf_import_with_progress(self, lib, path)
            else:
                item = run_ppt_import_with_progress(
                    self, lib, path,
                    with_animations=self._input_cal.ppt_animations)
        except Exception:
            logging.exception("Import of %s failed", path)
            item = None
        if item is None:
            return                          # cancelled, or already reported
        if item.preview_count == 0:
            AeroAlert.warning(self, "Nothing to open 💀",
                              f"Couldn't read any pages from '{path.name}'.")
            return

        if new_board:
            self._canvas.goto_page(0)
        else:
            last = self._canvas.page_count - 1
            if not self._page_is_blank(last):
                self._canvas.add_page()
                last = self._canvas.page_count - 1
            self._canvas.goto_page(last)
        first = self._canvas.current_page
        self._media_set_whole_background(item.id, "fit")
        self._canvas.goto_page(first)
        self._update_page_label()
        n = item.preview_count
        unit = "page" if path.suffix.lower() == ".pdf" else "slide"
        try:
            from ui.panels.toast import Toast
            Toast.show_message(
                self, f"Added {n} {unit}{'s' if n != 1 else ''} from "
                      f"{path.name}", 3500)
        except Exception:
            logging.debug("toast failed", exc_info=True)

    # ================================================================
    # Set As Background (file-picker shortcut — Media Manager has its own path)
    # ================================================================
    def _set_background(self) -> None:
        """Pick a file and set it as the current page's background."""
        fpath, _ = QFileDialog.getOpenFileName(
            self, "Set As Background", "",
            "Media (*.png *.jpg *.jpeg *.bmp *.webp *.pdf *.ppt *.pptx)")
        if not fpath:
            return
        lib = self._media_library()
        try:
            item = lib.import_file(Path(fpath))
        except Exception as e:
            logging.exception("Set background import failed: %s", e)
            item = None
        if item is None:
            AeroAlert.warning(self, "Background didn't load 💀",
                "Couldn't set that as the background. Try a different image?")
            return

        if item.type == "image":
            rel = item.original_relpath
        elif item.type == "pdf":
            p = lib.page_path(item, 0)
            rel = self._relpath_from_board(p) if p else None
        elif item.type == "ppt":
            step_idx = item.anim_counts[0] if item.anim_counts else 0
            p = lib.step_path(item, 0, step_idx)
            rel = self._relpath_from_board(p) if p else None
        else:
            rel = None

        if not rel:
            AeroAlert.warning(self, "Background didn't load 💀",
                "Couldn't set that as the background. Try a different image?")
            return

        scene = self._canvas.current_scene()
        stack = self._canvas.current_stack()
        if scene is not None and stack is not None:
            stack.push_set_background(scene, rel)
            self._canvas.invalidate_cache()
            self._canvas.mark_dirty()

    # ================================================================
    # Selection context menu
    # ================================================================
    def _dismiss_selection(self) -> None:
        """Clear the selection completely — the on-canvas selection box AND its
        floating Selection menu both close. Safe to call when nothing is
        selected (no-op). Used when the toolbar is touched and on undo / redo,
        so a stale box never lingers over objects that just changed."""
        if self._canvas.get_selected_ids():
            # Emits selection_changed → _on_selection_changed hides the menu.
            self._canvas.clear_selection()
        sm = getattr(self, "_selection_menu", None)
        if sm is not None and sm.isVisible():
            sm.hide()
        self._last_sel_signature = None

    def _on_rotating_changed(self, rotating: bool) -> None:
        """Hide the floating SelectionMenu while a rotation is in progress
        and re-position it after release.

        The canvas' internal _rotating flag already suppresses the
        selection rectangle + handles (see canvas_widget._paint_selection);
        the SelectionMenu lives in a separate top-level widget so it needs
        its own hide/show.

        On release we call the same _on_selection_changed() path the rest
        of the code uses, so the menu reappears in the right place for the
        new (post-rotation) bbox. This routes through _last_sel_signature,
        which means the button set is only rebuilt if the selection
        contents changed — a rotation alone (same ids, same types) just
        reposition-and-shows, no wasted layout work.
        """
        sm = getattr(self, "_selection_menu", None)
        if rotating:
            if sm is not None and sm.isVisible():
                sm.hide()
            return
        # Rotation done — reshow at the new bbox if there's still a selection.
        if self._canvas.get_selected_ids():
            self._on_selection_changed()

    def _on_selection_changed(self) -> None:
        ids = self._canvas.get_selected_ids()
        if not ids:
            if self._selection_menu is not None:
                self._selection_menu.hide()
            self._last_sel_signature = None
            return
        # An object is now selected — the Selection menu becomes the primary
        # surface. Close any other floating tool panel (Shapes / Pen / etc.)
        # so they don't compete with or overlap the selection menu.
        self._close_active_panel()
        types = self._canvas.selected_types()
        if self._selection_menu is None:
            # Parent to the main window so the menu is OWNED by LogicBoard:
            # it minimizes/restores with the app and stacks relative to it,
            # instead of floating as an independent always-on-top window.
            self._selection_menu = SelectionMenu(
                self,
                on_delete=self._delete_selection,
                on_duplicate=self._duplicate_selection,
                on_copy=self._copy_selection,
                on_bring_front=self._bring_selection_to_front,
                on_colors=self._color_selection,
                on_prev_slide=lambda: self._ppt_nav(-1),
                on_next_slide=lambda: self._ppt_nav(+1),
            )
        # Only rebuild the buttons when the SET of selected ids/types changes;
        # during a drag we just reposition (cheap) instead of rebuilding.
        signature = (tuple(sorted(str(i) for i in ids)), tuple(sorted(types)))
        if signature != getattr(self, "_last_sel_signature", None):
            self._selection_menu.rebuild(types)
            self._last_sel_signature = signature
        self._update_anim_step_label()
        self._position_selection_menu()

    def _update_anim_step_label(self) -> None:
        """Refresh the 'Animation Step: X / Y' indicator for the selected PPT
        object (hidden automatically when the current slide has only one
        cached state). No-op for non-PPT selections."""
        menu = getattr(self, "_selection_menu", None)
        if menu is None or not hasattr(menu, "set_anim_step"):
            return
        scene = self._canvas.current_scene()
        ids = self._canvas.get_selected_ids()
        if scene is None or not ids:
            menu.set_anim_step(1, 1)
            return
        for oid in ids:
            o = scene.find_mut(oid)
            if o is not None and o.type_name == "ppt":
                try:
                    cur_step = int(getattr(o, "anim_step", 0))
                    max_step = int(o.anim_count_for_current)
                except Exception:
                    cur_step, max_step = 0, 0
                menu.set_anim_step(cur_step + 1, max_step + 1)
                return
        menu.set_anim_step(1, 1)

    def _position_selection_menu(self) -> None:
        if self._selection_menu is None:
            return
        # Visibility is owned by _on_selection_changed (driven by whether any
        # objects are selected). Here we ONLY move the menu. If the rect is
        # momentarily unavailable we leave the menu where it is rather than
        # hiding it — hiding here caused the menu to vanish mid-interaction.
        rect = self._canvas.get_selection_rect()
        if rect is None:
            return
        # Convert the selection's scene-space bounding box to an axis-aligned
        # GLOBAL (screen) rect, so the menu can be placed on whichever side
        # (top / bottom / left / right) actually has room.
        from PySide6.QtCore import QPointF, QPoint, QRect
        from ui.canvas.canvas_widget import (ROTATE_HANDLE_OFFSET,
                                             ROTATE_HANDLE_RADIUS)
        tl = self._canvas.scene_to_screen(QPointF(rect.left(), rect.top()))
        br = self._canvas.scene_to_screen(QPointF(rect.right(), rect.bottom()))
        g_tl = self._canvas.mapToGlobal(QPoint(int(tl.x()), int(tl.y())))
        g_br = self._canvas.mapToGlobal(QPoint(int(br.x()), int(br.y())))
        global_rect = QRect(g_tl, g_br).normalized()
        # Leave room for the rotation handle (which sticks up above the box)
        # plus a little breathing space, so the menu never sits over it.
        clearance = int(ROTATE_HANDLE_OFFSET + ROTATE_HANDLE_RADIUS + UiScale.px(10))
        self._selection_menu.place_around_rect(self._canvas, global_rect, clearance)

    def _delete_selection(self) -> None:
        import logicboard_engine as lbe
        ids = self._canvas.get_selected_ids()
        scene = self._canvas.current_scene()
        stack = self._canvas.current_stack()
        if not ids or scene is None or stack is None:
            return
        for oid in ids:
            stack.push_delete(scene, oid)
        self._canvas.clear_selection()
        self._canvas.invalidate_cache()
        self._canvas.mark_dirty()

    def _duplicate_selection(self) -> None:
        """Duplicate every selected object with a small offset.

        Verified against the C++ bindings (bindings/bind_scene.cpp):
        - WhiteboardObject.serialize() returns nlohmann::json, which pybind11
          CANNOT convert to Python — calling it raises TypeError. So the old
          JSON round-trip was impossible.
        - CommandStack.push_add(scene, obj) deep-clones obj via clone_exact(),
          which PRESERVES the object's id. We exploit that: temporarily assign
          the source a fresh UUID, push_add (clone inherits it), restore the
          source's id. Scene::find is a linear id scan (no index), so in-place
          id mutation is safe and undoable (push_add is a real command).
        """
        import logicboard_engine as lbe
        ids   = self._canvas.get_selected_ids()
        scene = self._canvas.current_scene()
        stack = self._canvas.current_stack()
        if not ids or scene is None or stack is None:
            return

        new_ids: list = []
        for oid in ids:
            src = scene.find_mut(oid)
            if src is None:
                logging.warning("Duplicate: source object %s not found", oid)
                continue
            try:
                # The engine's push_add() deep-clones the passed object via
                # clone_exact(), which PRESERVES the object's id. To get a
                # distinct copy we temporarily give the source a fresh UUID,
                # push_add (clone inherits that fresh id), then restore the
                # source's original id. Scene::find is a linear id scan with no
                # separate index, so mutating ids in place is safe.
                original_id = src.id
                fresh_id = lbe.UUID.generate()
                src.id = fresh_id
                stack.push_add(scene, src)   # clone_exact copies fresh_id
                src.id = original_id         # restore the original immediately

                # The clone now lives in the scene under fresh_id. Offset it so
                # it sits visibly down-right of the original.
                clone = scene.find_mut(fresh_id)
                if clone is None:
                    logging.error("Duplicate: clone %s not found after push_add",
                                  fresh_id)
                    continue
                clone.translate(20.0, 20.0)
                # clone_exact() copies the source's z_order, so without this the
                # duplicate renders at the SAME depth as (and visually behind)
                # the original. Lift it above everything so the copy sits on top.
                try:
                    scene.bring_to_front(fresh_id)
                except Exception:
                    pass
                new_ids.append(fresh_id)
                logging.info("Duplicate: created clone %s from %s",
                             fresh_id, original_id)
            except Exception as e:
                logging.exception("Duplicate failed for object %s: %s", oid, e)
                # Best-effort: make sure we didn't leave the source with a
                # swapped id if something threw mid-way.
                try:
                    if scene.find(oid) is None:
                        src.id = oid
                except Exception:
                    pass

        if new_ids:
            # Replace the selection with the clones: originals deselect, the new
            # copies become the active selection (so the user can drag them
            # immediately). Compute the union bounding rect for the selection box.
            rect = None
            try:
                from PySide6.QtCore import QRectF as _QRectF
                for nid in new_ids:
                    o = scene.find(nid)
                    if o is None:
                        continue
                    bb = o.bounding_box()
                    r = _QRectF(bb.min_pt.x, bb.min_pt.y,
                                bb.max_pt.x - bb.min_pt.x,
                                bb.max_pt.y - bb.min_pt.y)
                    rect = r if rect is None else rect.united(r)
            except Exception as e:
                logging.warning("Selection rect for clones failed: %s", e)
                rect = None
            self._canvas.set_selection(new_ids, rect)
            self._canvas.invalidate_cache()
            self._canvas.mark_dirty()
            self._position_selection_menu()

    # ================================================================
    # Copy / Paste / Bring to Front
    # ================================================================
    #: How far each successive paste lands from the last, so stacked pastes
    #: on one page fan out instead of hiding exactly behind each other.
    _PASTE_STEP = 20.0

    def _union_rect(self, scene, ids):
        """Scene-space bounding rect of ``ids`` — what the selection box
        is drawn around."""
        from PySide6.QtCore import QRectF as _QRectF
        rect = None
        for oid in ids:
            o = scene.find(oid)
            if o is None:
                continue
            bb = o.bounding_box()
            r = _QRectF(bb.min_pt.x, bb.min_pt.y,
                        bb.max_pt.x - bb.min_pt.x, bb.max_pt.y - bb.min_pt.y)
            rect = r if rect is None else rect.united(r)
        return rect

    def _ordered_selection(self, scene) -> list:
        """The selected ids in drawing order, bottom first, so a copy or a
        raise keeps them stacked the way they were."""
        ids = [i for i in self._canvas.get_selected_ids()
               if scene.find_index(i) >= 0]
        return sorted(ids, key=scene.find_index)

    def _copy_selection(self) -> None:
        """Copy the selection onto LogicBoard's clipboard.

        The clipboard is a standalone engine Scene holding DEEP copies, not
        a list of ids: the teacher copies, turns the page, maybe erases the
        original — and the copy must still paste exactly as it was copied.
        """
        import logicboard_engine as lbe
        scene = self._canvas.current_scene()
        if scene is None:
            return
        ids = self._ordered_selection(scene)
        if not ids:
            return
        clip = lbe.Scene()
        for oid in ids:
            src = scene.find(oid)
            if src is not None:
                clip.add_object(src)        # clone_exact into the clipboard
        if clip.object_count == 0:
            return
        self._clipboard = clip
        self._clip_page = self._canvas.current_page
        self._clip_pastes = {}
        self._show_paste_chip(clip.object_count)

    def _show_paste_chip(self, count: int) -> None:
        if getattr(self, "_paste_chip", None) is None:
            from ui.panels.paste_chip import PasteChip
            self._paste_chip = PasteChip(self, on_paste=self._paste_clipboard,
                                         on_dismiss=self._clear_clipboard)
        self._paste_chip.present(count)

    def _clear_clipboard(self) -> None:
        """Forget what was copied. Also run on every board change: a copied
        picture or slide points at a file inside ITS board's folder, and
        pasted into another board it would be a broken image."""
        self._clipboard = None
        self._clip_pastes = {}
        chip = getattr(self, "_paste_chip", None)
        if chip is not None:
            chip.hide()

    def _paste_clipboard(self) -> None:
        """Put a fresh copy of the clipboard on the page showing now — one
        undo step, on top of everything, and selected so it can be dragged
        straight away.

        Where it lands: on the page it was copied from, offset down-right
        so it is not hidden exactly behind the original; on any other page,
        in the very same spot first (the natural place for "the same
        diagram on the next page"), fanning out after that.
        """
        import logicboard_engine as lbe
        clip = getattr(self, "_clipboard", None)
        scene = self._canvas.current_scene()
        stack = self._canvas.current_stack()
        if clip is None or scene is None or stack is None:
            return
        page = self._canvas.current_page
        n = self._clip_pastes.get(page, 0)
        steps = n + 1 if page == self._clip_page else n
        d = self._PASTE_STEP * steps

        objs = [clip.find_mut(o.id) for o in clip.all_objects()]
        objs = [o for o in objs if o is not None]
        new_ids = []
        actions = []
        for o in objs:
            # A new id per paste — push_compound clones exactly, id included,
            # and two pastes must be two objects.
            o.id = lbe.UUID.generate()
            if d:
                o.translate(d, d)
            new_ids.append(o.id)
            actions.append(("add", o))
        try:
            stack.push_compound(scene, "Paste", actions)
        finally:
            if d:
                # The clipboard keeps the ORIGINAL position; the offset
                # belonged to this paste only.
                for o in objs:
                    o.translate(-d, -d)
        self._clip_pastes[page] = n + 1

        self._canvas.set_selection(new_ids, self._union_rect(scene, new_ids))
        self._canvas.invalidate_cache()
        self._canvas.mark_dirty()
        self._position_selection_menu()

    def _bring_selection_to_front(self) -> None:
        """Raise the selection above everything else on the page, as ONE
        undoable step.

        The engine has ``Scene.bring_to_front`` but no command for it, so
        on its own it could not be undone. Removing an object and adding it
        back is exactly a raise — ``Scene.add`` always places on top — and
        both halves ARE commands, whose undo puts the object back at its
        old depth. Bottom-most first, so the raised objects keep their
        order among themselves.
        """
        import logicboard_engine as lbe
        scene = self._canvas.current_scene()
        stack = self._canvas.current_stack()
        if scene is None or stack is None:
            return
        ids = self._ordered_selection(scene)
        if not ids:
            return
        top = scene.object_count - len(ids)
        if [scene.find_index(i) for i in ids] == list(range(top, top + len(ids))):
            return                          # already the top of the stack
        hold = lbe.Scene()                  # keeps the clones alive until pushed
        actions = []
        for oid in ids:
            src = scene.find(oid)
            if src is None:
                continue
            actions.append(("delete", oid))
            actions.append(("add", hold.add_object(src)))
        stack.push_compound(scene, "Bring to Front", actions)
        self._canvas.set_selection(ids, self._union_rect(scene, ids))
        self._canvas.invalidate_cache()
        self._canvas.mark_dirty()
        self._position_selection_menu()

    def _color_selection(self) -> None:
        import logicboard_engine as lbe
        ids = self._canvas.get_selected_ids()
        scene = self._canvas.current_scene()
        stack = self._canvas.current_stack()
        if not ids or scene is None or stack is None:
            return
        # Exclude images and ppt objects.
        colorable = []
        for oid in ids:
            o = scene.find(oid)
            if o and o.type_name not in ("image", "ppt"):
                colorable.append(oid)
        if not colorable:
            return
        # Hide the selection menu while the color dialog is open so it can
        # never stack above the picker or block its controls. Restore it
        # afterwards (the selection is unchanged).
        sm = getattr(self, "_selection_menu", None)
        sm_was_visible = bool(sm and sm.isVisible())
        if sm_was_visible:
            sm.hide()
        col = QColorDialog.getColor(QColor(self._canvas.pen_color), self,
                                    "Choose Color")
        if sm_was_visible and self._canvas.get_selected_ids():
            sm.show()
            self._position_selection_menu()
        if not col.isValid():
            return
        rgba = lbe.RGBA(col.red(), col.green(), col.blue(), col.alpha())
        stack.push_set_color(scene, colorable, rgba)
        self._canvas.invalidate_cache()
        self._canvas.mark_dirty()
        # Selection box doesn't move, but refresh menu position anyway.
        self._position_selection_menu()

    def _ppt_nav(self, direction: int) -> None:
        """Advance/retreat the build-step OR slide of the selected PPT
        object(s). Mirrors a presenter clicker:

          * forward  → next build step within the current slide; if already
                       at the last step, advance to the next slide (step 0).
          * back     → previous build step; if already at step 0, retreat
                       to the previous slide's final step.

        Undoable via push_set_slide for slide changes; build-step changes are
        considered cheap UI state and are NOT pushed onto the undo stack
        (they live on the PptObject directly).
        """
        ids = self._canvas.get_selected_ids()
        scene = self._canvas.current_scene()
        stack = self._canvas.current_stack()
        if not ids or scene is None or stack is None:
            return
        changed = False
        for oid in ids:
            o = scene.find_mut(oid)
            if o is None or o.type_name != "ppt":
                continue
            cur_slide = int(o.current_slide)
            cur_step  = int(getattr(o, "anim_step", 0))
            cur_max_step = 0
            try:
                cur_max_step = int(o.anim_count_for_current)
            except Exception:
                cur_max_step = 0

            if direction > 0:
                if cur_step < cur_max_step:
                    o.anim_step = cur_step + 1
                    changed = True
                elif cur_slide + 1 < o.slide_count:
                    stack.push_set_slide(scene, oid, cur_slide + 1)
                    o.anim_step = 0
                    changed = True
            else:
                if cur_step > 0:
                    o.anim_step = cur_step - 1
                    changed = True
                elif cur_slide > 0:
                    stack.push_set_slide(scene, oid, cur_slide - 1)
                    # Land on the final build-step of the previous slide.
                    item = self._resolve_ppt_item(o)
                    final_step = 0
                    if item is not None and (cur_slide - 1) < len(item.anim_counts):
                        final_step = item.anim_counts[cur_slide - 1]
                    o.anim_step = final_step
                    changed = True

            self._sync_ppt_src(o)
        if changed:
            self._canvas.invalidate_cache()
            self._canvas.mark_dirty()
            self._update_anim_step_label()

    def _sync_ppt_src(self, ppt_obj) -> None:
        """Point the PPT object's ``src`` at the cache file for its current
        (slide, anim_step). Looks up the MediaItem in the board's media
        library; falls back to a no-op when the binding can't be resolved
        (the renderer then shows the placeholder)."""
        item = self._resolve_ppt_item(ppt_obj)
        if item is None or item.type != "ppt":
            return
        lib = self._media_library()
        slide_idx = max(0, int(getattr(ppt_obj, "current_slide", 0)))
        if slide_idx >= item.preview_count:
            slide_idx = item.preview_count - 1
        step_idx = max(0, int(getattr(ppt_obj, "anim_step", 0)))
        if slide_idx < len(item.anim_counts):
            max_step = item.anim_counts[slide_idx]
            step_idx = min(step_idx, max_step)
        p = lib.step_path(item, slide_idx, step_idx)
        if p is None and slide_idx < len(item.step_files) and item.step_files[slide_idx]:
            # Fall back to the LAST available step for this slide.
            cache_dir = lib.item_cache_dir(item)
            p = cache_dir / item.step_files[slide_idx][-1]
        if p is not None:
            try:
                ppt_obj.src = self._relpath_from_board(p)
            except Exception:
                ppt_obj.src = str(p)

    # ================================================================
    # Dirty tracking
    # ================================================================

    def _on_dirty_changed(self, dirty: bool) -> None:
        self._update_title()
        # The visible page changed; its thumbnail should refresh on the next
        # manager tick (timer-driven, not immediate — keeps it cheap).
        if dirty and hasattr(self, "_thumb_manager"):
            self._thumb_manager.mark_current_dirty()

    def _update_title(self) -> None:
        name = self._current_board_name or "Untitled"
        star = " *" if self._canvas.is_dirty else ""
        self.setWindowTitle(f"LogicBoard v3.0 — {name}{star}")

    # ================================================================
    # Panels
    # ================================================================

    def _close_active_panel(self) -> None:
        if self._active_panel is not None:
            try:
                self._active_panel.close()
                # Every open builds a fresh panel parented to this window, so
                # close() alone kept each one alive, hidden, until exit — ten
                # visits to Settings meant ten Settings panels. deleteLater,
                # not delete: this is often called from inside one of the
                # panel's own slots, which is still on the stack.
                self._active_panel.deleteLater()
            except Exception:
                pass
            self._active_panel = None

    def _toggled_shut(self, panel_type) -> bool:
        """Close the open panel and answer True when it IS a ``panel_type``.

        The second press on a tool button that is already active means "put
        that panel away" — the button and the panel are one control, so it has
        to work both ways. Without this the press rebuilt the same panel from
        scratch: identical on screen, so the panel looked stuck open and the
        only way to dismiss it was to press somewhere else entirely. (The
        outside-press route in ``eventFilter`` deliberately exempts the
        toolbar, since a press there is how you switch tools — so it was never
        going to close this one.)

        Keyed on the panel TYPE rather than a flag, because the answer depends
        on which panel is open, not merely whether one is: pressing Eraser
        while the Pen panel is up must swap panels, not close and leave the
        user with nothing.
        """
        panel = self._active_panel
        if isinstance(panel, panel_type) and panel.isVisible():
            self._close_active_panel()
            return True
        return False

    # The only two event types eventFilter cares about. A frozenset of the
    # enum members, resolved once, so the hot path is a hash lookup.
    _WATCHED_EVENTS = frozenset((QEvent.Type.MouseButtonPress,
                                 QEvent.Type.TouchBegin))

    def eventFilter(self, obj, event):
        """Observe app-wide presses to (1) dismiss the current selection the
        instant the toolbar is touched and (2) close an open tool panel when a
        press lands outside it. Touch-safe: replaces Qt.Popup's auto-close
        without its input grab, and never consumes the event.

        The floating Selection menu is a protected surface — a press inside it
        (Duplicate / Delete / Colors) must never close anything; it manages its
        own visibility via selection state.
        """
        # The type check comes FIRST and costs one comparison.
        #
        # This filter is installed on the whole application, so it sees every
        # event every widget generates — Polish, LayoutRequest, ChildAdded,
        # StyleChange, the lot — and each one crosses from C++ into Python to
        # get here. Rebuilding the pages panel on a 400-page board fired it
        # 25,200 times, 63 per card, and the first thing it used to do was
        # re-run ``from PySide6.QtCore import QEvent``: 27,200 module-import
        # lookups to answer a question two integers could answer.
        #
        # The set is built once at class scope (_WATCHED_EVENTS) and the
        # import is gone. Everything below still only runs for a real press.
        if event.type() not in self._WATCHED_EVENTS:
            return False
        if True:
            try:
                # Resolve a global press point for either a mouse press or a
                # touch begin (touch carries points rather than a cursor pos).
                if event.type() == QEvent.Type.TouchBegin:
                    pts = event.points()
                    gp = pts[0].globalPosition().toPoint() if pts else None
                else:
                    gp = event.globalPosition().toPoint()
                if gp is not None:
                    # Toolbar is now a 3-group ToolbarStack (not a QWidget),
                    # so we can't ask it for a single geometry(). Walk the
                    # left/center/right pills individually — a press counts
                    # as "on the toolbar" if it lands inside ANY of them.
                    tb = getattr(self, "_toolbar", None)
                    on_toolbar = False
                    if tb is not None:
                        for g in (getattr(tb, "left", None),
                                  getattr(tb, "center", None),
                                  getattr(tb, "right", None)):
                            if g is not None and g.isVisible() \
                                    and g.geometry().contains(gp):
                                on_toolbar = True
                                break
                    # ANY touch on the toolbar dismisses the current selection:
                    # the on-canvas box AND its floating menu close together.
                    # This is also what makes the Undo / Redo buttons drop the
                    # stale selection box that an undo/redo would leave behind.
                    if on_toolbar:
                        self._dismiss_selection()
                    # Close an open tool panel when the press lands fully outside
                    # it (and not on the toolbar or the protected Selection menu,
                    # both of which are separate top-level windows).
                    panel = self._active_panel
                    if panel is not None and panel.isVisible():
                        inside_panel = panel.geometry().contains(gp)
                        sm = getattr(self, "_selection_menu", None)
                        # mapFromGlobal, NOT geometry().contains(gp): the
                        # selection menu is a CHILD widget now, so its
                        # geometry() is in main-window coordinates while gp
                        # is global. Comparing the two directly would treat
                        # presses on the menu as presses outside it and
                        # close the active panel underneath. This form is
                        # correct for a child and a top-level alike.
                        on_selection = bool(
                            sm is not None and sm.isVisible()
                            and sm.rect().contains(sm.mapFromGlobal(gp)))
                        if not inside_panel and not on_toolbar and not on_selection:
                            self._close_active_panel()
            except Exception:
                pass
        # Never consume the event — just observe it.
        return super().eventFilter(obj, event)

    def _show_file_panel(self) -> None:
        # The click lands before the branch: closing the panel is as much a
        # press of this button as opening it, and a silent one reads as a
        # press that did not register.
        click_sound.play_click()
        # Already showing? Then this press is the one that puts it away —
        # same contract as Pen and Eraser. The button and the panel are one
        # control, so it has to work both ways; without this the press just
        # rebuilt an identical panel and the thing looked stuck open.
        if self._toggled_shut(FilePanel):
            return
        self._close_active_panel()

        panel = FilePanel(
            self,
            on_new=self._new_board,
            on_save=self._save_board,
            on_save_as=self._save_as_board,
            on_open=self._open_board,
            on_delete=self._on_board_deleted,
            current_board_path=self._current_lb_path,
        )
        self._active_panel = panel
        panel.show_anchored(self._toolbar.file_btn)

    def _show_settings_panel(self) -> None:
        click_sound.play_click()
        self._close_active_panel()

        # LogicShare section is shown only when the service has been wired up
        # at startup (app.run). Absent → the card is simply omitted.
        ls_objects = None
        if getattr(self, "ls_service", None) is not None:
            ls_objects = (self.ls_service, self.ls_config, self.ls_activity)

        panel = SettingsPanel(
            self,
            palm_eraser_enabled=self._canvas.palm_eraser_enabled,
            palm_min_size=self._canvas.palm_min_size,
            object_eraser_affects_media=self._canvas.object_eraser_affects_media,
            autosave_enabled=self._autosave_enabled,
            autosave_interval_min=self._autosave_interval_min,
            touch_mode=self._input_cal.touch_mode,
            stylus_calibrated=self._input_cal.stylus_profile.calibrated,
            eraser_calibrated=self._input_cal.eraser_profile.calibrated,
            palm_calibrated=self._input_cal.palm_profile.calibrated,
            input_calibration=self._input_cal,
            on_palm_toggle=self._canvas.set_palm_eraser_enabled,
            on_palm_size=self._canvas.set_palm_min_size,
            on_media_eraser_toggle=self._canvas.set_object_eraser_affects_media,
            on_autosave_toggle=self._set_autosave_enabled,
            on_autosave_interval=self._set_autosave_interval,
            ppt_animations=self._input_cal.ppt_animations,
            on_ppt_animations=self._set_ppt_animations,
            canvas_style=self._input_cal.canvas_style,
            on_canvas_style=self._set_canvas_style,
            zoom_sensitivity=self._input_cal.zoom_sensitivity,
            on_zoom_sensitivity=self._set_zoom_sensitivity,
            mouse_is_stylus_front=self._input_cal.mouse_is_stylus_front,
            on_mouse_is_stylus_front=self._set_mouse_is_stylus_front,
            on_touch_mode=self._set_touch_mode,
            on_calibrate_stylus=self._calibrate_stylus,
            on_calibrate_eraser=self._calibrate_eraser,
            on_calibrate_palm=self._calibrate_palm,
            logicshare_objects=ls_objects,
        )
        self._active_panel = panel
        panel.show_anchored(self._toolbar.settings_btn)

    def _sync_pen_button_ink(self) -> None:
        """Tint the Pen toolbar button with the colour it will actually draw.

        Which colour that is depends on the mode:
          * pen          -> pen colour
          * highlighter  -> highlighter colour
          * brush        -> brush colour
          * dual_pen     -> the FRONT tip's colour, because the front tip is
                            what touches the board by default; the back tip
                            is the deliberate flip.

        Dual pen resolves through the persisted front-tip TOOL, so a front
        tip assigned to the highlighter shows the highlighter colour rather
        than the pen's.
        """
        btn = getattr(self._toolbar, "pen_btn", None)
        if btn is None:
            return
        try:
            tool = self._canvas.tool
            if tool == "dual_pen":
                front = getattr(self._input_cal, "dual_pen_front", "pen")
                front = front if front in ("pen", "highlighter", "brush") else "pen"
                if front == "pen":
                    # Dual Pen's own ink, not the plain pen's.
                    btn.set_ink_color(self._canvas.dual_pen_color)
                    return
                tool = front
            if tool == "highlighter":
                color = self._canvas.highlighter_color
            elif tool == "brush":
                color = self._canvas.brush_color
            elif tool == "pen":
                color = self._canvas.pen_color
            else:
                color = None          # eraser/select front tip: no ink to show
            btn.set_ink_color(color)
        except Exception:
            logging.debug("pen button ink sync failed", exc_info=True)

    def _show_pen_panel(self) -> None:
        # Already showing? Then this press is the one that puts it away.
        if self._toggled_shut(PenPanel):
            return
        self._close_active_panel()

        # Determine which mode tile should appear selected. The canvas tool
        # is the authoritative source: pen / highlighter / dual_pen are all
        # valid starting states for this panel.
        if self._canvas.tool == "highlighter":
            mode = "highlighter"
        elif self._canvas.tool == "brush":
            mode = "brush"
        elif self._canvas.tool == "dual_pen":
            mode = "dual_pen"
        else:
            mode = "pen"
        panel = PenPanel(
            self,
            mode=mode,
            pen_width=int(self._canvas.pen_width),
            highlighter_height=int(self._canvas.highlighter_height),
            brush_height=int(self._canvas.brush_height),
            pen_color=self._canvas.pen_color,
            highlighter_color=self._canvas.highlighter_color,
            brush_color=self._canvas.brush_color,
            dual_color=self._canvas.dual_pen_color,
            on_mode_change=self._on_pen_mode_change,
            on_width_change=self._on_pen_width_change,
            on_color_change=self._on_pen_color_change,
            # Dual Pen — pass the persisted assignments + the change callback
            # so the back-tip selector pills reflect the saved state and
            # mutations round-trip through InputCalibration → input_handler.
            dual_front=self._input_cal.dual_pen_front,
            dual_back=self._input_cal.dual_pen_back,
            on_dual_back_change=self._on_dual_back_change,
            # Dual Pen needs BOTH tips calibrated. These were never passed, so
            # the panel fell back to its (then permissive) defaults and the
            # lock never engaged — Dual Pen was selectable with nothing
            # calibrated at all.
            # front_tip_known, not stylus_profile.calibrated: on a board that
            # reports the front tip as a mouse click there is no contact to
            # calibrate, and the "mouse click is the stylus front tip" setting
            # supplies the same fact.
            stylus_calibrated=self._input_cal.front_tip_known,
            eraser_calibrated=self._input_cal.eraser_profile.calibrated,
        )
        self._active_panel = panel
        panel.show_anchored(self._toolbar.pen_btn)

    def _show_eraser_panel(self) -> None:
        # Already showing? Then this press is the one that puts it away.
        if self._toggled_shut(EraserPanel):
            return
        self._close_active_panel()

        def _on_size(v: float) -> None:
            """Save the size against the CURRENT mode only — never touches the
            other mode's stored value."""
            self._canvas.set_eraser_size(float(v))
            self._input_cal.set_eraser_size(self._canvas.eraser_mode, float(v))

        def _on_mode(m: str) -> None:
            self._canvas.set_eraser_mode(m)
            # Switching modes restores the size that mode was last used with.
            # The eraser panel needs to refresh its slider to reflect this.
            if self._active_panel is not None:
                try:    self._active_panel.set_size_silent(int(self._canvas.eraser_size))
                except AttributeError: pass

        panel = EraserPanel(
            self,
            eraser_size=int(self._canvas.eraser_size),
            eraser_mode=self._canvas.eraser_mode,
            on_size_change=_on_size,
            on_mode_change=_on_mode,
        )
        self._active_panel = panel
        panel.show_anchored(self._toolbar.eraser_btn)

    # ================================================================
    # Pen panel callbacks
    # ================================================================

    def _on_pen_mode_change(self, mode: str) -> None:
        # Final gate. The Pen panel already greys the tile out, but this is
        # the one place every route into Dual Pen passes through — panel tap,
        # a restored mode from settings.json, any future shortcut — so the
        # rule is enforced where it can't be bypassed rather than only in the
        # widget that happens to draw it.
        if mode == "dual_pen" and not self._input_cal.dual_pen_available:
            logging.info("Dual Pen refused: both stylus tips must be known "
                         "(front=%s [calibrated=%s, mouse_is_front=%s], "
                         "back=%s).",
                         self._input_cal.front_tip_known,
                         self._input_cal.stylus_profile.calibrated,
                         self._input_cal.mouse_is_stylus_front,
                         self._input_cal.eraser_profile.calibrated)
            mode = "pen"
        self._pen_tool_mode = mode  # remember for the toolbar pen button
        if mode == "highlighter":
            self._canvas.set_tool("highlighter")
            self._toolbar.set_pen_display("highlighter", "Highlighter")
        elif mode == "brush":
            self._canvas.set_tool("brush")
            self._toolbar.set_pen_display("brush", "Brush")
        elif mode == "dual_pen":
            # Dual Pen: the canvas tool is the dispatcher, and the input
            # handler resolves it to the right front/back tool per stroke.
            self._canvas.set_tool("dual_pen")
            self._toolbar.set_pen_display("dual_pen", "Dual Pen")
        else:
            self._canvas.set_tool("pen")
            self._toolbar.set_pen_display("pen", "Pen")
        # The pen button represents pen / highlighter / dual_pen — keep it active.
        self._toolbar.set_active_tool("pen")
        # Mode decides WHICH colour the button shows (pen vs highlighter vs
        # the dual-pen front tip), so re-resolve it here too.
        self._sync_pen_button_ink()

    def _on_dual_back_change(self, tool_id: str) -> None:
        """User picked a tool for the back tip in the Pen panel.

        Updates the live input handler so the next back-tip stroke uses the
        new tool, AND persists the choice in InputCalibration so it survives
        a restart. The front tip's assignment is left untouched — it has its
        own controls (the existing pen / highlighter mode buttons)."""
        VALID = {"pen", "highlighter", "brush", "select", "eraser"}
        if tool_id not in VALID:
            return
        self._input_handler.set_dual_pen_assignment(
            front=self._input_cal.dual_pen_front,   # unchanged
            back=tool_id,
        )
        self._input_cal.set_dual_pen(back=tool_id)

    def _on_pen_width_change(self, value: int) -> None:
        if self._canvas.tool == "highlighter":
            self._canvas.set_highlighter_height(float(value))
        elif self._canvas.tool == "brush":
            self._canvas.set_brush_height(float(value))
        else:
            self._canvas.set_pen_width(float(value))

    def _on_pen_color_change(self, color) -> None:
        from PySide6.QtGui import QColor
        c = QColor(color)
        if self._canvas.tool == "highlighter":
            self._canvas.set_highlighter_color(c)
        elif self._canvas.tool == "brush":
            self._canvas.set_brush_color(c)
        elif self._canvas.tool == "dual_pen":
            # Dual Pen keeps its own ink; writing the pen's here is what made
            # a colour picked in one mode appear in the other.
            self._canvas.set_dual_pen_color(c)
        else:
            self._canvas.set_pen_color(c)
        # Toolbar button follows the ink immediately.
        self._sync_pen_button_ink()

    # ================================================================
    # Board save / load
    # ================================================================

    def _new_board(self) -> None:
        if not self._confirm_discard():
            return

        # Tear down any existing workdir before creating a new one.
        prev_wd = self._current_board_path
        if prev_wd is not None:
            cleanup_workdir(prev_wd)

        self._canvas._init_engine()
        # Fresh unsaved workdir — gives the board a place for imports until
        # the user saves for the first time.
        wd = new_unsaved_workdir(self._saves_dir)
        self._clear_clipboard()           # copies point into the OLD board
        self._current_lb_path    = None
        self._current_board_path = wd
        self._current_board_name = None
        self._canvas._board_dir  = str(wd)
        self._thumb_manager.set_board_dir(wd)
        self._invalidate_media_library()
        if hasattr(self, "_ppt_to_item"):
            self._ppt_to_item.clear()
        self._canvas.clear_dirty()
        self._update_title()
        self._update_page_label()
        self._canvas.invalidate_cache()

    # ──────────────────────────────────────────────────────────────────────
    # SAVE — re-zip the working directory back into the .lb file
    # ──────────────────────────────────────────────────────────────────────

    def _save_board(self) -> bool:
        """Quick-save to the existing .lb. If the board has never been
        saved, fall back to Save-As to prompt for a name."""
        if self._current_lb_path is None:
            return self._save_as_board_dialog()
        return self._do_save(self._current_lb_path)

    def _save_as_board_dialog(self) -> bool:
        text, ok = QInputDialog.getText(self, "Save As", "Board name:")
        if not (ok and text.strip()):
            return False
        return self._save_as_board(text.strip())

    def _save_as_board(self, name: str) -> bool:
        """Compute a unique ``<saves_dir>/<name>.lb`` path then save.

        The name the teacher typed is the name they get, minus only what
        Windows genuinely refuses — see ``clean_board_name``.
        """
        from ui.util.lb_format import clean_board_name
        clean = clean_board_name(name)

        lb_path = self._saves_dir / f"{clean}{LB_EXTENSION}"
        if lb_path.exists():
            # " (2)", not "_2". Board names keep their spaces now, so an
            # underscore suffix read as a leftover from the old mangling —
            # and it is the same convention the LogicShare receiver already
            # uses for incoming files with a name that is taken.
            idx = 2
            while (self._saves_dir / f"{clean} ({idx}){LB_EXTENSION}").exists():
                idx += 1
            lb_path = self._saves_dir / f"{clean} ({idx}){LB_EXTENSION}"

        self._current_lb_path    = lb_path
        self._current_board_name = lb_path.stem
        return self._do_save(lb_path)

    def _report_save_failure(self, lb_path: Path, reason: str, *,
                             silent: bool) -> None:
        """Tell the user a save did not happen.

        A save that fails quietly is the worst outcome this app has: the
        teacher carries on believing the lesson is on disk, closes the board,
        and finds out at the point where nothing can be recovered. Every
        ``return False`` on the save path now comes through here.

        Nothing is shown on the silent path — that one runs while the window
        is being torn down for background mode, where a modal has nobody in
        front of it. It still reaches the log.
        """
        logging.error("Save FAILED for %s: %s", lb_path, reason)
        if silent:
            return
        try:
            AeroAlert.warning(
                self, "Couldn't save the board",
                f"'{lb_path.stem}' was not saved.\n\n{reason}\n\n"
                "Your work is still open here — try saving again, or use "
                "Save As with a different name.")
        except Exception:
            logging.exception("Could not show the save-failure alert")

    def _write_board_json(self, lb_path: Path) -> Optional[Path]:
        """Step 1 of every save: flush the board into the workdir.

        Fast enough for the UI thread (one JSON file, typically <100 ms) and
        deliberately separate from step 2, the DEFLATE zip, which is the part
        that takes seconds and therefore the part each caller wants to run
        differently — modal, silent, or on a worker with a toast.

        Returns the workdir on success, None if there is nothing to save to
        or the engine refused the write.
        """
        board = self._canvas.board
        if board is None:
            logging.error("Save aborted: the canvas has no board to save.")
            return None
        try:
            # _ensure_board_dir, NOT a bare None check.
            #
            # This used to read `if board is None or self._current_board_path
            # is None: return None`, and that second half silently threw away
            # every save on a freshly launched board. _current_board_path is
            # the WORKING directory, and on a fresh start it is None until
            # something creates one — which only happened when the user
            # imported media. So: open LogicBoard, draw, hit Save, type a
            # name, and nothing was written. No file, no error dialog, not
            # even a log line, because this returned None before reaching any
            # of them. Importing a picture first "fixed" it, which is why it
            # looked intermittent.
            #
            # _ensure_board_dir already creates an unsaved workdir when there
            # isn't one, which is exactly what a first save needs.
            wd = self._ensure_board_dir()
            board.name      = self._current_board_name or lb_path.stem
            board.theme     = theme_mgr.theme_name
            board.last_page = self._canvas.current_page
            if not board.save_to_file(str(wd / "board.json")):
                logging.error("save_to_file returned False")
                return None
            self._save_bg_fit(wd)
            return wd
        except Exception:
            logging.exception("Writing board.json failed")
            return None

    def _wait_for_autosave(self) -> None:
        """Block until any in-flight autosave worker has finished.

        Cheap and almost always a no-op — but the alternative is two threads
        zipping into the same ``.lb.tmp`` and racing to replace the same
        target file, which is how a board gets truncated.
        """
        w = getattr(self, "_autosave_worker", None)
        if w is not None and w.isRunning():
            logging.debug("Waiting for the in-flight autosave to finish…")
            w.wait()

    def _do_save(self, lb_path: Path, *, silent: bool = False) -> bool:
        """Persist the current workdir to ``lb_path`` as a .lb archive.

        ``board.save_to_file`` writes board.json (fast, <100 ms even for
        large boards). The heavy step is ``save_workdir_to_lb`` which
        DEFLATE-compresses the workdir (board.json + media cache + all
        WEBP frames) into a .lb zip — easily several seconds on a
        media-heavy board, which used to freeze the UI completely.

        Two modes:

        * **Normal (silent=False)** — user-initiated save. The zip step
          runs on a ``SaveWorker`` QThread while a small aero
          ``SavingDialog`` modal stays animated in the foreground. UI
          remains responsive throughout.

        * **Silent (silent=True)** — background transition. The zip runs
          SYNCHRONOUSLY on the UI thread with no modal. Used when
          ApplicationCore.hide_ui needs to persist the open board before
          destroying the window — popping a modal during a
          close-to-background transition would be jarring (and on close,
          there's no surviving UI anyway).

        The periodic autosave uses NEITHER; see ``_auto_save``.
        """
        wd = self._write_board_json(lb_path)
        if wd is None:
            self._report_save_failure(
                lb_path, "the board could not be written to disk",
                silent=silent)
            return False

        try:
            # Never let two writers near the same .lb. An autosave now runs
            # on a worker thread with no modal to block the user, so a manual
            # save can genuinely arrive on top of one in flight — and both
            # would be writing the same ``.lb.tmp`` before replacing the same
            # target. Waiting for the autosave costs a moment on a path the
            # user already expects to take one.
            self._wait_for_autosave()

            if silent:
                # 2a) Background autosave path — call save_workdir_to_lb
                #     directly on the UI thread. The user has no UI to look
                #     at (we're about to destroy it); blocking briefly is
                #     fine. Avoids the SavingDialog dependency, which means
                #     this path keeps working even if saving_dialog.py is
                #     missing from the installed tree.
                from ui.util.lb_format import save_workdir_to_lb
                save_workdir_to_lb(
                    wd, lb_path,
                    board_name=self._current_board_name or lb_path.stem,
                )
            else:
                # 2b) User-initiated save — heavy step on a worker thread
                #     with the modal showing.
                from ui.panels.saving_dialog import SavingDialog
                ok, err = SavingDialog.run_save(
                    parent     = self,
                    workdir    = wd,
                    lb_path    = lb_path,
                    board_name = self._current_board_name or lb_path.stem,
                )
                if not ok:
                    logging.error("Save worker failed: %s", err)
                    self._report_save_failure(lb_path, err, silent=silent)
                    return False

            self._canvas.clear_dirty()
            self._update_title()
            logging.info("Board saved: %s%s", lb_path.name,
                         " (silent)" if silent else "")
            return True
        except Exception as e:
            logging.exception("Save error: %s", e)
            self._report_save_failure(lb_path, str(e), silent=silent)
            return False

    # ──────────────────────────────────────────────────────────────────────
    # OPEN — extract the .lb into a fresh working directory then load
    # ──────────────────────────────────────────────────────────────────────

    def _open_board(self, path: Path) -> None:
        """Open a board from a .lb file, or migrate a legacy folder first.

        Accepts:
          * .lb file        → extract → workdir → load
          * legacy folder   → migrate to .lb (with confirmation) → load
        """
        if not self._confirm_discard():
            return

        path = Path(path)

        # ── Legacy-folder migration path ─────────────────────────────────
        if path.is_dir() and is_legacy_board_folder(path):
            reply = AeroAlert.question(
                self,
                "Convert to .lb format? ✨",
                f"'{path.name}' is in the older folder-based format.\n\n"
                f"Convert it to a single '{path.name}{LB_EXTENSION}' file?\n"
                "(The original folder is kept intact.)",
                AeroAlert.Yes | AeroAlert.No,
                AeroAlert.Yes,
            )
            if reply != AeroAlert.Yes:
                return
            try:
                lb_path = migrate_folder_to_lb(path)
                logging.info("Migrated %s → %s", path.name, lb_path.name)
                path = lb_path
            except Exception as e:
                logging.exception("Migration failed: %s", e)
                AeroAlert.warning(self, "Couldn't convert 💀", str(e))
                return

        if not is_lb_file(path):
            logging.warning("Not a valid .lb file: %s", path)
            AeroAlert.warning(
                self, "Not a LogicBoard file 💀",
                f"'{path.name}' is not a valid LogicBoard (.lb) file.",
            )
            return

        # ── Extract the NEW board before touching the old one ─────────────
        #
        # This used to run the other way round: delete the previous
        # workdir, then extract. Every failure below therefore landed on a
        # board whose working directory had ALREADY been destroyed — and
        # the workdir is not scratch space, it holds the imported pictures,
        # the PDF renders and board.json itself. The app carried on titled
        # "Lesson 3", with ``_current_board_path`` pointing at a directory
        # that no longer existed.
        #
        # ``is_lb_file`` above catches the common corrupt file, which is why
        # this survived a truncated download. It does not catch the rest:
        # an archive that opens but holds an unsafe path, a network drive
        # that drops between the check and the extract, a full disk during
        # extraction, or a permission error. Reproduced with the first of
        # those — a real zip, with board.json, plus one traversal member —
        # and the picture imported into the open lesson was gone.
        #
        # So the previous workdir is now released at the END, once the new
        # board is loaded and ``_current_board_path`` has moved. Until then
        # a failure is just a failure, and the teacher keeps their lesson.
        prev_wd = self._current_board_path

        def _abandon(wd_):
            """Give up on the new board, leaving the old one exactly as it
            was."""
            cleanup_workdir(wd_)

        try:
            # Unpacking a board full of rendered PDF pages is seconds of
            # silence. Big ones get the busy window; small ones open inside
            # a frame and would only get a flash of one.
            from ui.panels.busy_window import run_with_busy
            if self._looks_slow_to_open(path):
                wd = run_with_busy(
                    # Not the board's name: "Class 11-A Thermodynamics
                    # Chapter 4" is 484 px of text in a 136 px strip, so it
                    # would be elided down to a couple of words anyway, and
                    # the user just picked the board — they know which one.
                    # Unpacking an archive cannot report a percentage
                    # honestly, so this one keeps the plain sweeping ring.
                    self, "Opening…",
                    lambda _b: open_lb_to_workdir(path, self._saves_dir))
            else:
                wd = open_lb_to_workdir(path, self._saves_dir)
        except Exception as e:
            logging.exception("Extract failed: %s", e)
            AeroAlert.warning(self, "Couldn't open 💀", str(e))
            return

        # ── Load board.json from the workdir ─────────────────────────────
        board = self._canvas.board
        if board is None:
            _abandon(wd)          # this path used to leak the workdir too
            return
        board_file = wd / "board.json"
        if not board_file.exists():
            logging.warning("No board.json in extracted .lb: %s", path)
            _abandon(wd)
            return

        try:
            # Clear the name BEFORE loading.
            #
            # ``board.name`` reads like it comes from the file. It does
            # not: the engine's ``load_from_file`` never touches ``name``,
            # so the only thing that ever sets it is _write_board_json on
            # the way OUT. The Board object is long-lived and loaded into
            # over and over, so what ``board.name or path.stem`` actually
            # returned was the name of the LAST board saved or opened.
            #
            # Measured: with "Chemistry Term 1" open, opening
            # "Physics Term 2.lb" put "Chemistry Term 1" in the title bar —
            # and that is the name the next Save would have used.
            #
            # Blanking it first makes the fallback the truth: a .lb is
            # identified by its FILE NAME, which is what the files panel
            # shows and what the user renames. metadata.json carries a copy
            # of the name for information, but nothing reads it back, and a
            # title that disagrees with the filename would be worse than
            # one that follows it.
            try:
                board.name = ""
            except Exception:
                pass
            if not board.load_from_file(str(board_file)):
                logging.error("Failed to load board.json from %s", path.name)
                _abandon(wd)
                return

            # Past this line the new board IS the open board, so the old
            # workdir is finally safe to release.
            self._current_lb_path    = path
            self._clear_clipboard()       # copies point into the OLD board
            self._current_board_path = wd
            self._current_board_name = board.name or path.stem
            if prev_wd is not None and Path(prev_wd) != wd:
                cleanup_workdir(prev_wd)

            # Existing code paths still treat _current_board_path as "the
            # board folder" — point the canvas + thumb manager at the workdir.
            self._load_bg_fit(wd)
            self._canvas._board_dir = str(wd)
            if self._canvas.adopt_default_bg():
                # Every preview on disk shows the old colour.
                self._invalidate_page_thumbs(range(self._canvas.page_count))
            self._canvas.clear_media_caches()
            self._invalidate_media_library()
            if hasattr(self, "_ppt_to_item"):
                self._ppt_to_item.clear()
            self._thumb_manager.set_board_dir(wd)

            # Rebuild one command stack per loaded page.
            self._canvas._command_stacks.clear()
            try:
                import logicboard_engine as lbe
                for _ in range(board.page_count):
                    self._canvas._command_stacks.append(lbe.CommandStack(200))
            except ImportError:
                pass

            self._canvas._current_page = min(
                max(0, board.last_page),
                max(0, self._canvas.page_count - 1),
            )

            self._canvas.clear_dirty()
            self._canvas.invalidate_cache()
            self._update_title()
            self._update_page_label()
            logging.info("Board opened: %s", path.name)
        except Exception as e:
            # The board was already installed as the current one a few lines
            # up, so this is a partially-configured window rather than an
            # untouched one. Releasing the workdir it is pointing at would
            # leave it pointing at nothing, which is the very state this
            # reordering exists to prevent — so the workdir stays, and the
            # next open or New Board reclaims it.
            logging.exception("Open error: %s", e)

    #: A .lb bigger than this is worth a busy window while it unpacks.
    #: 12 MB is about a fifteen-page PDF board — below that, extraction is
    #: fast enough that the window would appear and vanish.
    _BUSY_OPEN_BYTES = 12 * 1024 * 1024

    def _looks_slow_to_open(self, path) -> bool:
        try:
            return Path(path).stat().st_size >= self._BUSY_OPEN_BYTES
        except OSError:
            return False

    def _on_board_deleted(self, path: Path) -> None:
        """Called when the user deletes a board from the file panel.
        With the .lb format, ``path`` is the .lb FILE the user removed."""
        if self._current_lb_path and Path(self._current_lb_path) == Path(path):
            self._new_board()

    def confirm_close(self) -> bool:
        """Ask about unsaved work — once for this window.

        Every route out of LogicBoard ends up closing the window more than
        once, and the question used to be asked on each of them:

          * the user presses Exit (or the window close button);
          * ``closeEvent`` asks, and the user answers;
          * ApplicationCore's close handler CLAIMS the close so it can go to
            background mode instead, so Qt's close is ignored;
          * ``hide_ui`` then tears the window down and calls ``win.close()``
            itself — which re-enters ``closeEvent``, which asks again.

        The second prompt only appeared after "Don't Save", because that
        leaves the board dirty and the question therefore still has an
        answer worth asking for. Choosing Save cleaned the board and the
        re-entry passed silently, which is why it looked intermittent.

        The tray's "Quit completely" had the same shape: it prompts, then
        ``shutdown`` runs ``hide_ui``, which closes the window again.

        So the decision is recorded rather than re-derived. Answering once
        commits this window to closing; a window that is not closing (the
        user pressed Cancel) is left exactly as it was, and the next attempt
        asks again.
        """
        if self._closing:
            return True
        if not self._confirm_discard():
            return False
        self._closing = True
        return True

    def _confirm_discard(self) -> bool:
        if not self._canvas.is_dirty:
            return True

        # Save / Don't Save / Cancel — macOS-style aero alert. Save is the
        # default so that dismissing the dialog with the keyboard cannot be
        # the thing that throws the work away.
        choice = AeroAlert.show_alert(
            self, "warning", "Unsaved changes",
            "This board has unsaved changes.\n\n"
            "Save them before continuing? If you don't, they are lost.",
            buttons=("Don't Save", "Cancel", "Save"), default=2)
        if choice == 2:          # Save
            # Only proceed if the save actually SUCCEEDED. A failed or
            # cancelled Save-As returns False, and carrying on regardless
            # would discard the work the user just asked to keep.
            return self._save_board()
        if choice == 0:          # Don't Save
            return True
        return False             # Cancel (or Esc)

    # ================================================================
    # Auto-save
    # ================================================================

    def _set_autosave_enabled(self, enabled: bool) -> None:
        """Turn the auto-save timer on/off (from Settings)."""
        self._autosave_enabled = bool(enabled)
        if self._autosave_enabled:
            self._autosave_timer.start(self._autosave_interval_min * 60 * 1000)
            logging.info("Auto-save enabled (%d min).", self._autosave_interval_min)
        else:
            self._autosave_timer.stop()
            logging.info("Auto-save disabled.")
        self._input_cal.set_autosave(self._autosave_enabled,
                                     self._autosave_interval_min)

    def _set_autosave_interval(self, minutes: int) -> None:
        """Change the auto-save interval (1 / 5 / 10 min) from Settings."""
        if minutes not in (1, 5, 10):
            return
        self._autosave_interval_min = minutes
        # Restart with the new interval only if auto-save is currently on.
        if self._autosave_enabled:
            self._autosave_timer.start(minutes * 60 * 1000)
        logging.info("Auto-save interval set to %d min.", minutes)
        self._input_cal.set_autosave(self._autosave_enabled,
                                     self._autosave_interval_min)

    def _set_canvas_style(self, style: str) -> None:
        """Switch the canvas layout and remember it.

        Presentation only — the board and its pages are untouched, so this
        is safe to flip at any time, including mid-board.
        """
        self._canvas.set_canvas_style(style)
        self._input_cal.set_canvas_style(style)
        logging.info("Canvas style: %s", style)

    def _set_zoom_sensitivity(self, value: float) -> None:
        self._canvas.set_zoom_sensitivity(value)
        self._input_cal.set_zoom_sensitivity(value)

    def _set_ppt_animations(self, enabled: bool) -> None:
        """Persist the PowerPoint-import animation mode (from Settings)."""
        self._input_cal.set_ppt_animations(bool(enabled))
        logging.info("PPT import animations %s.",
                     "ENABLED" if enabled else "disabled")

    def _set_mouse_is_stylus_front(self, enabled: bool) -> None:
        """Persist + apply 'a left click is the stylus front tip'.

        Applied to the live input handler immediately so the user can flip
        it and test with the pen without restarting.
        """
        self._input_cal.set_mouse_is_stylus_front(bool(enabled))
        self._input_handler.set_mouse_is_stylus_front(bool(enabled))
        logging.info("Mouse-as-stylus-front-tip %s.",
                     "ENABLED" if enabled else "disabled")

    # ================================================================
    # Input & Touch Settings (Phase 1: persist mode; calibration = Phase 2)
    # ================================================================
    def _set_touch_mode(self, mode: str) -> None:
        """Persist the selected touch mode AND apply it to the live canvas so
        palm handling switches immediately (Phase 3)."""
        self._input_cal.set_touch_mode(mode)
        self._canvas._touch_mode = mode
        logging.info("Touch mode set to %s.", mode)

    def _toggle_input_overlay(self) -> None:
        """Dev aid: flip the canvas input-type overlay (Ctrl+Shift+D)."""
        on = not getattr(self._canvas, "_debug_input_overlay", False)
        self._canvas.set_debug_input_overlay(on)
        logging.info("Input overlay %s.", "ON" if on else "OFF")

    def _calibrate_stylus(self) -> None:
        self._run_calibration("stylus")

    def _calibrate_eraser(self) -> None:
        self._run_calibration("eraser")

    def _calibrate_palm(self) -> None:
        self._run_calibration("palm")

    def _run_calibration(self, kind: str) -> None:
        """Open the calibration dialog for ``kind`` and persist the resulting
        profile to settings.json on success. Refreshes the Settings panel's
        calibrated-status if it's still open."""
        from ui.panels.calibration_dialog import CalibrationDialog, config_for
        logging.info("Opening %s calibration dialog.", kind)
        # Only the STYLUS flow honours the mouse-as-front-tip setting. The
        # eraser tip and the palm are wide contacts that every board
        # reports as real touch, so a mouse there still means the user is
        # on a desktop and the guard should still fire.
        allow_mouse = (kind == "stylus"
                       and getattr(self._input_cal, "mouse_is_stylus_front", False))
        # Pass the live calibration so the dialog can reject a back-tip /
        # palm pair whose medians coincide — that check needs the profile
        # already stored for the OTHER one.
        dlg = CalibrationDialog(config_for(kind), self,
                                allow_mouse=allow_mouse,
                                calibration=self._input_cal)
        accepted = dlg.exec()
        if not accepted or not dlg.completed or dlg.result_profile is None:
            logging.info("%s calibration cancelled / incomplete.", kind)
            return
        if kind == "stylus":
            self._input_cal.set_stylus_profile(dlg.result_profile)
        elif kind == "eraser":
            self._input_cal.set_eraser_profile(dlg.result_profile)
        else:
            self._input_cal.set_palm_profile(dlg.result_profile)
        logging.info("%s profile saved: %s", kind, dlg.result_profile)
        # If the Settings panel is still open, reflect the new calibrated state.
        panel = getattr(self, "_active_panel", None)
        if panel is not None and hasattr(panel, "set_calibration_state"):
            panel.set_calibration_state(
                stylus=self._input_cal.stylus_profile.calibrated,
                eraser=self._input_cal.eraser_profile.calibrated,
                palm=self._input_cal.palm_profile.calibrated,
            )

    def _auto_save(self) -> None:
        """Auto-save fires only when the board has been saved at least once
        (so a .lb path exists). For brand-new unsaved boards we skip — we
        don't want to invent a name on the user's behalf.

        Deliberately NOT ``_do_save``. Neither of its modes suits a save the
        user never asked for:

        * the modal one puts a centred "Saving Board…" spinner over the board
          and blocks input behind it. The user is mid-lesson. Nobody is
          waiting on this, so nobody should be stopped by it.
        * the silent one runs the zip on the UI thread, which is worse rather
          than better: on a media-heavy board that is a multi-second freeze
          with nothing on screen to explain it. It exists for the close path,
          where there is no UI left to freeze.

        So: the zip goes to a worker thread and the board stays live
        throughout, and when it lands the user gets a small "Auto Saved" in
        the corner for three seconds. Notify, don't interrupt.
        """
        if not (self._current_lb_path and self._canvas.is_dirty):
            return
        # One autosave at a time. A board big enough to still be zipping when
        # the next tick arrives is exactly the board where stacking a second
        # writer would hurt — and the work is the same work, so skipping
        # loses nothing: the next tick catches whatever changed meanwhile.
        w = getattr(self, "_autosave_worker", None)
        if w is not None and w.isRunning():
            logging.debug("Autosave still running — skipping this tick.")
            return

        lb_path = self._current_lb_path
        wd = self._write_board_json(lb_path)
        if wd is None:
            logging.error("Auto-save failed: could not write board.json")
            return

        try:
            from ui.panels.saving_dialog import SaveWorker
            worker = SaveWorker(wd, lb_path,
                                self._current_board_name or lb_path.stem,
                                parent=self)
        except Exception:
            logging.exception("Auto-save failed: could not start the worker")
            return

        # The board stays live while the zip runs, so it can pick up edits
        # that this save does NOT contain — board.json was written a moment
        # ago and the archive is being built from it. Clearing the unsaved
        # flag unconditionally on completion would call those strokes saved,
        # and worse, the next tick would see a clean board and skip them
        # entirely. ``_dirty`` is a plain bool with no revision to compare, so
        # watch the change signal for the length of the window instead.
        edited_during = {"yes": False}

        def _note_edit() -> None:
            edited_during["yes"] = True

        try:
            self._canvas.content_changed.connect(_note_edit)
        except Exception:
            edited_during["yes"] = True     # can't tell → assume dirty

        def _release() -> None:
            try:
                self._canvas.content_changed.disconnect(_note_edit)
            except Exception:
                pass

        def _ok() -> None:
            _release()
            if not edited_during["yes"]:
                self._canvas.clear_dirty()
            self._update_title()
            logging.info("Board auto-saved: %s%s", lb_path.name,
                         " (edited during save — still unsaved)"
                         if edited_during["yes"] else "")
            try:
                from ui.panels.toast import Toast
                Toast.show_message(self, "Auto Saved", 3000)
            except Exception:
                logging.debug("autosave toast failed", exc_info=True)

        def _fail(msg: str, _tb: str) -> None:
            # Leave the board marked unsaved — it IS. And no toast: a message
            # that says nothing actionable is worse than silence, while a real
            # error belongs in the log, where it can be read after the fact
            # rather than for three seconds.
            _release()
            logging.error("Auto-save failed: %s", msg)

        worker.finished_ok.connect(_ok)
        worker.failed.connect(_fail)
        self._autosave_worker = worker
        worker.start()

    # ================================================================
    # Close event
    # ================================================================

    def set_close_handler(self, handler) -> None:
        """Register a callable that intercepts the Close button.

        ``handler() -> bool``. Return True from the handler to take
        responsibility for the window (closeEvent.ignore() is called —
        the handler is expected to call ``deleteLater`` or similar).
        Return False to fall through to normal Qt close behaviour (the
        process will exit if this is the last top-level window).

        Used by ApplicationCore to swap "close = exit" for "close =
        hide UI, keep service alive in background mode".
        """
        self._close_handler = handler

    def _autosave_for_background(self) -> Optional[Path]:
        """Auto-save the open board for background-mode transition.

        Returns the .lb path that was saved (so ApplicationCore can
        remember it for reopen). Returns None if there's no save target
        (untitled board with no path) or if save failed.

        Doesn't prompt the user — this is a side-effect-free hand-off.
        The autosave timer is stopped to prevent overlapping saves.
        """
        try:
            self._autosave_timer.stop()
        except Exception:
            pass
        # Only save if there's a concrete .lb file backing this board.
        # An untitled board has _current_board_name=None — we lose those
        # on close-to-background. (The doc accepted 1-3s reopen time as
        # acceptable, so we deliberately do NOT prompt the user; the
        # background transition stays silent.)
        if self._current_board_path is None or self._current_board_name is None:
            return None
        try:
            lb_path = saves_dir() / f"{self._current_board_name}{LB_EXTENSION}"
            if self._do_save(lb_path, silent=True):
                logging.info("Autosaved for background: %s", lb_path)
                return lb_path
        except Exception as e:
            logging.exception("Autosave-for-background failed: %s", e)
        return None

    def _open_board_path(self, path: Path) -> bool:
        """Silent variant of ``_open_board`` used by ApplicationCore on
        reopen — we auto-saved on the previous close so there's no
        unsaved-state prompt to worry about, and no legacy-folder
        migration dialog either. Returns True on success.

        BUG FIX: the previous version called ``self._canvas.load_board(...)``
        which does not exist — CanvasWidget doesn't load boards, the
        C++ engine's Board object does (via ``board.load_from_file(...)``).
        The old code would crash with AttributeError on every reopen,
        leaving the user staring at an empty canvas even though the file
        was on disk. This version mirrors the working pattern from
        ``_open_board`` exactly — same load call, same post-load rebuild
        of command stacks / caches / page state — just without the
        interactive confirm dialogs.
        """
        try:
            path = Path(path)
            if not path.exists():
                return False
            # The legacy-folder migration in _open_board is interactive;
            # for the silent reopen path, only handle real .lb files.
            if not path.is_file() or path.suffix.lower() != LB_EXTENSION:
                return False

            prev_wd = self._current_board_path
            # Extract FIRST. See _open_board for the whole reasoning: the
            # previous workdir holds the open lesson's imported pictures and
            # PDF renders, and every failure path below used to land on a
            # board that had already lost them.
            wd = open_lb_to_workdir(path, self._saves_dir)

            # Load board.json from the workdir into the ENGINE board (not
            # into the canvas — that's the bug we're fixing here).
            board = self._canvas.board
            if board is None:
                cleanup_workdir(wd)
                return False
            board_file = wd / "board.json"
            if not board_file.exists():
                cleanup_workdir(wd)
                return False
            # Same reasoning as _open_board: blank the name first so a
            # board.json without one falls back to the file's own name
            # rather than keeping the previous board's.
            try:
                board.name = ""
            except Exception:
                pass
            if not board.load_from_file(str(board_file)):
                logging.error("Failed to load board.json from %s", path.name)
                cleanup_workdir(wd)
                return False

            # Post-load setup — mirrors _open_board's flow so the restored
            # state is identical to what an interactive open would give.
            self._current_lb_path    = path
            self._clear_clipboard()       # copies point into the OLD board
            self._current_board_path = wd
            self._current_board_name = board.name or path.stem
            # The new board is installed, so the old workdir is now safe to
            # release.
            if prev_wd is not None and Path(prev_wd) != wd:
                try:
                    cleanup_workdir(prev_wd)
                except Exception:
                    logging.exception("cleanup_workdir(prev) failed")

            self._load_bg_fit(wd)
            self._canvas._board_dir = str(wd)
            if self._canvas.adopt_default_bg():
                # Every preview on disk shows the old colour.
                self._invalidate_page_thumbs(range(self._canvas.page_count))
            self._canvas.clear_media_caches()
            self._invalidate_media_library()
            if hasattr(self, "_ppt_to_item"):
                self._ppt_to_item.clear()
            self._thumb_manager.set_board_dir(wd)

            # Rebuild one command stack per loaded page — otherwise undo
            # would be broken until the user creates a new page.
            self._canvas._command_stacks.clear()
            try:
                import logicboard_engine as lbe
                for _ in range(board.page_count):
                    self._canvas._command_stacks.append(lbe.CommandStack(200))
            except ImportError:
                pass

            self._canvas._current_page = min(
                max(0, board.last_page),
                max(0, self._canvas.page_count - 1),
            )

            self._canvas.clear_dirty()
            self._canvas.invalidate_cache()
            self._update_title()
            self._update_page_label()
            logging.info("Board reopened: %s", path.name)
            return True
        except Exception as e:
            logging.exception("_open_board_path(%s) failed: %s", path, e)
            return False

    def closeEvent(self, event) -> None:
        # UNSAVED WORK IS ASKED ABOUT FIRST — before the close handler gets a
        # say, and before anything is torn down.
        #
        # This used to sit below the handler block, which meant it never ran
        # at all in the normal build: ApplicationCore registers a close
        # handler, the handler claims every close to go into background mode,
        # and the function returned before reaching the prompt. Both routes
        # out of the app (the toolbar Exit button, which is wired to
        # ``self.close``, and the window close button) come through here, so
        # both were silently dropping unsaved work.
        #
        # ``_confirm_discard`` is the app's existing Save / Discard / Cancel
        # alert and already no-ops on a clean board, so a board with nothing
        # to lose still closes in one press.
        if not self.confirm_close():
            event.ignore()
            return
        # If something registered a close handler (typically ApplicationCore
        # for background-mode), give it first crack. If it returns True we
        # ignore() Qt's close (so the widget isn't immediately destroyed)
        # and let the handler tear the window down on its own schedule.
        handler = getattr(self, "_close_handler", None)
        if handler is not None:
            try:
                claimed = bool(handler())
            except Exception:
                logging.exception("close handler raised")
                claimed = False
            if claimed:
                event.ignore()
                return
        # No handler — original behaviour (full app exit). The discard prompt
        # already ran at the top, so by here the user has committed to
        # closing one way or the other and there is nothing left to ask.
        self._autosave_timer.stop()
        # An autosave zip may still be running on its worker thread.
        # Destroying the window (and with it the QThread it parents)
        # mid-write would abort the archive and can leave a ``.lb.tmp``
        # orphan next to the board. It is at most a couple of seconds,
        # and only when a save is genuinely in flight.
        self._wait_for_autosave()
        # Floating toolbar groups are top-level windows with no Qt parent;
        # close each one explicitly so the app exits cleanly.
        if hasattr(self, "_toolbar") and self._toolbar is not None:
            try:    self._toolbar.hide()
            except Exception: pass
        # Selection menu (parented, but close explicitly so nothing
        # lingers if event ordering is odd).
        sm = getattr(self, "_selection_menu", None)
        if sm is not None:
            sm.close()
        # Remove the per-session working directory — the user-facing
        # .lb file is the canonical artefact; the workdir is disposable.
        cleanup_workdir(self._current_board_path)
        logging.info("Application closed.")
        event.accept()