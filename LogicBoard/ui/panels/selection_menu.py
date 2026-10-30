"""
selection_menu.py — Floating context menu shown near the selection box.

Visual style matches the main toolbar: each option is a TILE with an SVG icon
centered ABOVE a small caption label. Hover lifts the tile with a translucent
fill brighter than the menu's own translucent background. Aero-friendly:
the outer pill uses ``bg_overlay`` (translucent), tiles use ``transparent``
so they pick up the menu's blur.

Buttons (dynamic per selection):
  * Delete    (always)   — trash-bin icon, destructive
  * Duplicate (always)   — overlapping-rects icon
  * Color     (drawable) — paint-palette icon
  * ◀ / ▶     (PPT only) — prev/next slide
"""
from __future__ import annotations

from typing import Optional, Callable

from PySide6.QtWidgets import (QWidget, QHBoxLayout, QPushButton, QVBoxLayout,
                                 QLabel, QFrame)
from PySide6.QtGui     import QColor, QPixmap, QPainter
from PySide6.QtCore    import Qt, QPoint, QSize, QRectF
from PySide6.QtSvg     import QSvgRenderer

from ui.util.scaling   import UiScale
from ui.util.animations import fade_in
from ui.util.audio     import click_sound
from ui.util.paths     import find_app_icon_path   # not used directly but ensures icon path discovery
from ui.theme.theme_manager import theme_mgr

__all__ = ["SelectionMenu"]


# ─────────────────────────────────────────────────────────────────────────────
# Helper: render an SVG icon at a given size using the theme accent (or any
# color) as the stroke. The SVG files use ``currentColor`` so we re-tint by
# painting the rendered pixmap.
# ─────────────────────────────────────────────────────────────────────────────

def _project_root():
    from pathlib import Path
    return Path(__file__).resolve().parents[2]


def _render_svg_icon(svg_relpath: str, size: int,
                      color: Optional[QColor] = None) -> QPixmap:
    """Render assets/icons/<svg_relpath> at `size` × `size`, tinted to `color`.

    DPI-aware: backing pixmap is allocated at physical pixels and tagged
    with devicePixelRatio so 4K boards render crisp."""
    from PySide6.QtGui import QGuiApplication, QImage
    from PySide6.QtCore import Qt as _Qt

    screen = QGuiApplication.primaryScreen()
    dpr    = screen.devicePixelRatio() if screen else 1.0
    phys   = max(1, int(size * dpr))

    pm = QPixmap(phys, phys)
    pm.setDevicePixelRatio(dpr)
    pm.fill(Qt.transparent)

    svg_path = _project_root() / "assets" / "icons" / svg_relpath
    if not svg_path.exists():
        return pm

    renderer = QSvgRenderer(str(svg_path))
    if not renderer.isValid():
        return pm

    p = QPainter(pm)
    try:
        p.setRenderHint(QPainter.Antialiasing, True)
        p.setRenderHint(QPainter.SmoothPixmapTransform, True)
        # Render in LOGICAL units (size), NOT physical (phys): a QPainter on a
        # dpr-tagged pixmap scales coordinates by dpr itself, so a phys-sized
        # rect double-scales and clips the bottom/right on Hi-DPI displays.
        # A small inset keeps the glyph clear of the edge, aspect-preserved.
        src = renderer.defaultSize()
        sw, sh = max(1, src.width()), max(1, src.height())
        inset  = max(1.0, size * 0.05)
        avail  = size - 2 * inset
        scale  = min(avail / sw, avail / sh)
        dw, dh = sw * scale, sh * scale
        ox, oy = (size - dw) / 2.0, (size - dh) / 2.0
        renderer.render(p, QRectF(ox, oy, dw, dh))
    finally:
        p.end()

    # Re-tint: composite using SourceIn so only the icon pixels get the
    # target colour, transparent pixels stay transparent.
    if color is not None and color.isValid():
        tinted = QPixmap(phys, phys)
        tinted.setDevicePixelRatio(dpr)
        tinted.fill(Qt.transparent)
        p2 = QPainter(tinted)
        try:
            # Source already carries the right dpr; draw it 1:1 so the painter
            # doesn't scale it again.
            pm.setDevicePixelRatio(dpr)
            p2.drawPixmap(0, 0, pm)
            p2.setCompositionMode(QPainter.CompositionMode_SourceIn)
            p2.fillRect(tinted.rect(), color)
        finally:
            p2.end()
        return tinted
    return pm


# ─────────────────────────────────────────────────────────────────────────────
# Tool-tile button — icon on top, caption below. Matches toolbar style.
# ─────────────────────────────────────────────────────────────────────────────

class _SelectionTile(QPushButton):
    """Square tile with icon above a caption label. Same visual language as
    the main toolbar's ToolButton."""

    def __init__(self, *, icon_svg: str, label: str,
                  variant: str = "default", parent=None):
        super().__init__(parent)
        self._icon_svg = icon_svg
        self._label_text = label
        self._variant = variant
        self.setCursor(Qt.PointingHandCursor)
        self.setFlat(True)

        sc = UiScale.px
        # Bumped from 68×68 → 76×76 so the icon + caption have proper
        # breathing room. Previously the round stroke caps on the Fluent
        # SVGs were getting clipped at the bottom and right edges because
        # the icon container was sized exactly to the SVG viewBox.
        self.setFixedSize(sc(76), sc(76))

        v = QVBoxLayout(self)
        v.setContentsMargins(sc(4), sc(8), sc(4), sc(6))
        v.setSpacing(sc(3))

        self._icon_label = QLabel()
        self._icon_label.setAlignment(Qt.AlignCenter)
        self._icon_label.setAttribute(Qt.WA_TransparentForMouseEvents, True)
        v.addWidget(self._icon_label, 0, Qt.AlignCenter)

        self._caption = QLabel(label)
        self._caption.setAlignment(Qt.AlignCenter)
        self._caption.setAttribute(Qt.WA_TransparentForMouseEvents, True)
        v.addWidget(self._caption, 0, Qt.AlignCenter)

        self._apply_style()

    def _apply_style(self):
        sc = UiScale.px
        t  = theme_mgr.tokens

        if self._variant == "danger":
            icon_color  = theme_mgr.qcolor(t.danger_text)
            text_color  = t.danger_text
            border_hov  = t.danger_border_btn
            bg_hov      = t.danger_bg_hover
        else:
            icon_color  = theme_mgr.qcolor(t.text_primary)
            text_color  = t.text_primary
            border_hov  = t.stroke_strong
            bg_hov      = t.fill_hover

        # Icon pixmap — larger render (28 px) + 4-px internal padding so the
        # rounded SVG stroke caps don't kiss the label edge.
        icon_px = sc(28)
        pm = _render_svg_icon(self._icon_svg, icon_px, color=icon_color)
        self._icon_label.setPixmap(pm)
        # Give the QLabel itself 4 px more than the pixmap so antialiased
        # edges of the SVG aren't clipped at the label boundary.
        self._icon_label.setFixedSize(icon_px + sc(4), icon_px + sc(4))

        # Caption text
        self._caption.setStyleSheet(
            f"QLabel {{ color: {text_color}; "
            f"font-size: {sc(10)}px; font-weight: 600; "
            f"background: transparent; border: none; }}"
        )

        # Tile background — transparent at rest so the menu's translucent
        # backdrop is what reads. Hover gives a brighter fill + stroke.
        self.setStyleSheet(f"""
            QPushButton {{
                background-color: transparent;
                border: 1px solid transparent;
                border-radius: {t.radius_md}px;
            }}
            QPushButton:hover {{
                background-color: {bg_hov};
                border: 1px solid {border_hov};
            }}
            QPushButton:pressed {{
                background-color: {t.fill_pressed};
            }}
        """)

    def refresh_theme(self):
        self._apply_style()


# ─────────────────────────────────────────────────────────────────────────────
# SelectionMenu
# ─────────────────────────────────────────────────────────────────────────────

class SelectionMenu(QWidget):
    def __init__(self, parent=None, *,
                 on_delete:     Optional[Callable] = None,
                 on_duplicate:  Optional[Callable] = None,
                 on_copy:       Optional[Callable] = None,
                 on_bring_front: Optional[Callable] = None,
                 on_colors:     Optional[Callable] = None,
                 on_prev_slide: Optional[Callable] = None,
                 on_next_slide: Optional[Callable] = None):
        # PLAIN CHILD WIDGET — deliberately NOT a Qt.Tool window any more.
        #
        # This used to be a native top-level (Qt.Tool | FramelessWindowHint |
        # WA_TranslucentBackground), which bought DWM aero at the cost of
        # its own HWND. On a touch board that made it barely usable: a
        # separate window has its own activation state, so the first tap went
        # into activating the window instead of pressing the button under the
        # finger, and WA_ShowWithoutActivating made the focus behaviour
        # inconsistent on top of that. Taps got eaten and the user had to
        # press twice.
        #
        # As a child of the main window there is no second HWND, no
        # activation hop, and no layered-window alpha hit-testing — the
        # widget is just part of the main window's surface, so every press
        # and every touch lands the first time. The cost is the aero blur:
        # DWM effects are per-HWND, so a child simply cannot have one. The
        # fill below is a real translucent colour composited over the canvas,
        # which keeps it looking like glass without the blur.
        super().__init__(parent)
        self.setAttribute(Qt.WA_DeleteOnClose, False)
        # MANDATORY for the fill below to appear at all. A plain QWidget
        # subclass does not draw its stylesheet background unless it is
        # told to — Qt only does that automatically for the built-in
        # widget classes. Without this the menu rendered as nothing but
        # its icons over the canvas.
        self.setAttribute(Qt.WA_StyledBackground, True)
        # Take touch directly rather than relying on the canvas underneath
        # to hand it over.
        self.setAttribute(Qt.WA_AcceptTouchEvents, True)

        self._on_delete     = on_delete
        self._on_duplicate  = on_duplicate
        self._on_copy       = on_copy
        self._on_bring_front = on_bring_front
        self._on_colors     = on_colors
        self._on_prev_slide = on_prev_slide
        self._on_next_slide = on_next_slide

        sc = UiScale.px
        self._layout = QHBoxLayout(self)
        # Tighter padding so the pill matches the toolbar density.
        self._layout.setContentsMargins(sc(10), sc(8), sc(10), sc(8))
        self._layout.setSpacing(sc(4))

        self._buttons = {}

    def rebuild(self, selected_types: set):
        """Rebuild buttons based on what's selected."""
        # Clear
        while self._layout.count():
            item = self._layout.takeAt(0)
            w = item.widget()
            if w:
                # setParent(None) UNPARENTS immediately; deleteLater alone
                # leaves the tile a child until the event loop next runs, so
                # anything measuring the menu in between still counted it.
                w.setParent(None)
                w.deleteLater()
        self._buttons.clear()

        t = theme_mgr.tokens

        # Outer pill — a REAL, OPAQUE fill now. This was ``transparent``
        # because the DWM blur behind the old top-level window did the
        # painting; a child widget has no blur, so transparent left the menu
        # invisible apart from its icons. ``bg_surface`` is a step lighter
        # than the canvas, which with the border and the Qt drop shadow
        # reads clearly as a floating menu.
        #
        # The border is a SOLID grey, deliberately not a translucent one.
        # A translucent border double-composites with itself where the
        # rounded corners curve back on the fill, drawing a darker,
        # thicker-looking arc at each corner — the same double-edge
        # artifact the aero dialogs had. One opaque line, one radius, and
        # (unlike the old top-level) no DWM rounding underneath competing
        # with the CSS one, so there is exactly one curve here.
        #
        # radius_lg, not radius_xl. The panels use xl (18 px) but they are
        # large surfaces; on a menu only ~83 px tall the same radius eats
        # most of the edge and reads as a pill rather than a panel. lg
        # keeps the corner treatment visually consistent at this size.
        self.setStyleSheet(
            f"SelectionMenu {{ "
            f"background-color: {t.bg_surface}; "
            f"border: 1px solid {t.stroke_strong}; "
            f"border-radius: {t.radius_lg}px; "
            f"}}"
        )

        # Delete (always) — destructive variant
        del_btn = _SelectionTile(icon_svg="delete.svg", label="Delete",
                                  variant="danger")
        del_btn.clicked.connect(self._fire_delete)
        self._layout.addWidget(del_btn)
        self._buttons["delete"] = del_btn

        # Duplicate (always)
        dup_btn = _SelectionTile(icon_svg="duplicate.svg", label="Duplicate")
        dup_btn.clicked.connect(self._fire_duplicate)
        self._layout.addWidget(dup_btn)
        self._buttons["duplicate"] = dup_btn

        # Copy (always) — onto LogicBoard's clipboard; the Paste chip in the
        # top-right corner then puts it on whatever page is showing.
        copy_btn = _SelectionTile(icon_svg="copy.svg", label="Copy")
        copy_btn.clicked.connect(self._fire_copy)
        self._layout.addWidget(copy_btn)
        self._buttons["copy"] = copy_btn

        # Bring to Front (always) — above everything else on the page.
        front_btn = _SelectionTile(icon_svg="front.svg", label="To Front")
        front_btn.clicked.connect(self._fire_bring_front)
        self._layout.addWidget(front_btn)
        self._buttons["front"] = front_btn

        # Color — only if at least one drawable (non-image/ppt) selected
        drawable = selected_types - {"image", "ppt"}
        if drawable:
            col_btn = _SelectionTile(icon_svg="color.svg", label="Color")
            col_btn.clicked.connect(self._fire_colors)
            self._layout.addWidget(col_btn)
            self._buttons["colors"] = col_btn

        # PPT prev / next — only if a ppt object is selected. These step
        # through cached animation states (then slides). The step counter
        # label appears only when the current slide has multiple steps.
        if "ppt" in selected_types:
            prev_btn = _SelectionTile(icon_svg="prev.svg", label="Prev")
            prev_btn.clicked.connect(self._fire_prev)
            self._layout.addWidget(prev_btn)
            self._buttons["prev"] = prev_btn

            t2 = theme_mgr.tokens
            self._anim_label = QLabel("")
            self._anim_label.setAlignment(Qt.AlignCenter)
            self._anim_label.setStyleSheet(
                f"color: {t2.text_secondary}; font-size: {UiScale.px(11)}px; "
                f"font-weight: 700; background: transparent; border: none; "
                f"padding: 0 {UiScale.px(4)}px;")
            self._anim_label.hide()
            self._layout.addWidget(self._anim_label)
            self._buttons["anim_label"] = self._anim_label

            next_btn = _SelectionTile(icon_svg="next.svg", label="Next")
            next_btn.clicked.connect(self._fire_next)
            self._layout.addWidget(next_btn)
            self._buttons["next"] = next_btn

        # SHOW the freshly-built tiles explicitly.
        #
        # Qt only auto-shows children when the PARENT is shown, so a widget
        # added to an already-visible parent stays hidden until told
        # otherwise — and QLayout::sizeHint() ignores hidden widgets. That
        # combination is the whole "menu shrinks after Duplicate" bug: on a
        # first selection the menu is still hidden, so show() reveals every
        # tile and the size is right; duplicating rebuilds while the menu is
        # ALREADY visible, so the new tiles never appeared, the layout
        # measured an empty box and the menu collapsed to its margins
        # (18x14 px). Re-selecting only "fixed" it because that path hid and
        # re-showed the menu.
        for w in self._buttons.values():
            w.show()

        self._layout.activate()
        self.adjustSize()

    def set_anim_step(self, current_one_based: int, total: int) -> None:
        """Show 'Animation Step: X / Y' when the current slide has multiple
        cached states; hide it otherwise. Called by the main window after each
        navigation and whenever the selection changes."""
        lbl = self._buttons.get("anim_label")
        if lbl is None:
            return
        if total > 1:
            lbl.setText(f"Animation Step: {current_one_based} / {total}")
            lbl.show()
        else:
            lbl.hide()
        self.adjustSize()

    # ── callbacks ───────────────────────────────────────────────────────
    def _fire_delete(self):
        click_sound.play_click()
        if self._on_delete: self._on_delete()

    def _fire_duplicate(self):
        click_sound.play_click()
        if self._on_duplicate: self._on_duplicate()

    def _fire_copy(self):
        click_sound.play_click()
        if self._on_copy: self._on_copy()

    def _fire_bring_front(self):
        click_sound.play_click()
        if self._on_bring_front: self._on_bring_front()

    def _fire_colors(self):
        click_sound.play_click()
        if self._on_colors: self._on_colors()

    def _fire_prev(self):
        click_sound.play_click()
        if self._on_prev_slide: self._on_prev_slide()

    def _fire_next(self):
        click_sound.play_click()
        if self._on_next_slide: self._on_next_slide()

    # ── show + positioning ──────────────────────────────────────────────
    def showEvent(self, event):
        super().showEvent(event)
        # No apply_aero / enable_shadow here: both are DWM calls acting on
        # an HWND, and this is a child widget with no window of its own.
        #
        # No Qt drop shadow either, tempting as it is as a stand-in. A widget
        # can hold only ONE QGraphicsEffect, and ``fade_in`` installs a
        # QGraphicsOpacityEffect on child widgets — so a drop shadow and the
        # fade silently destroy each other, and whichever loses just never
        # happens. The solid fill plus the opaque border already read as a
        # floating menu, so the fade is the one worth keeping.
        fade_in(self)

    def place_around_rect(self, canvas, rect, clearance: int = 0):
        """Position the menu on whichever side of the selection box has room:
        ABOVE (preferred — leaving ``clearance`` px for the rotation handle),
        else BELOW, else to the RIGHT, else to the LEFT; if none fit cleanly,
        the side with the most space wins. The final position is clamped fully
        on-screen. ``rect`` is the selection's bounding box in GLOBAL (screen)
        coordinates.
        """
        from PySide6.QtGui import QGuiApplication
        # Settle the layout BEFORE measuring. ``rebuild()`` swaps the whole
        # tile set out, and adjustSize() reads sizeHint() — which is stale
        # until the layout has been recomputed. Duplicating an object
        # reselects the clones, which changes the selection signature, which
        # rebuilds the tiles, and the menu was then measured mid-swap and
        # collapsed to a sliver. Re-selecting by hand "fixed" it only because
        # by then the layout had caught up.
        lay = self.layout()
        if lay is not None:
            lay.activate()
        self.adjustSize()
        w = self.width()
        h = self.height()
        gap = UiScale.px(12)
        margin = UiScale.px(8)

        scr = (QGuiApplication.screenAt(rect.center())
               or QGuiApplication.primaryScreen())
        avail = scr.availableGeometry() if scr is not None else rect

        cx = rect.center().x()
        cy = rect.center().y()
        space_above = rect.top() - avail.top()
        space_below = avail.bottom() - rect.bottom()
        space_right = avail.right() - rect.right()
        space_left = rect.left() - avail.left()
        need_v_top = h + gap + clearance      # top also clears the rotate handle
        need_v = h + gap
        need_h = w + gap

        if space_above >= need_v_top:
            side = "top"
        elif space_below >= need_v:
            side = "bottom"
        elif space_right >= need_h:
            side = "right"
        elif space_left >= need_h:
            side = "left"
        else:
            side = max(("top", space_above), ("bottom", space_below),
                       ("right", space_right), ("left", space_left),
                       key=lambda kv: kv[1])[0]

        if side == "top":
            x = cx - w // 2
            y = rect.top() - clearance - h - gap
        elif side == "bottom":
            x = cx - w // 2
            y = rect.bottom() + gap
        elif side == "right":
            x = rect.right() + gap
            y = cy - h // 2
        else:  # left
            x = rect.left() - w - gap
            y = cy - h // 2

        # keep the whole menu inside the available screen area
        x = max(avail.left() + margin, min(int(x), avail.right() - w - margin))
        y = max(avail.top() + margin, min(int(y), avail.bottom() - h - margin))

        # All of the above reasons in GLOBAL coordinates, because that is the
        # space the screen edges live in. This is a CHILD widget now, so the
        # final position has to be expressed relative to the parent, and the
        # clamp re-applied against the parent's own rect — a child clipped by
        # its parent would simply be cut off rather than pushed back inside.
        parent = self.parentWidget()
        if parent is not None:
            local = parent.mapFromGlobal(QPoint(int(x), int(y)))
            x = max(margin, min(local.x(), parent.width() - w - margin))
            y = max(margin, min(local.y(), parent.height() - h - margin))

        self.move(int(x), int(y))
        if not self.isVisible():
            self.show()
        # raise_() still does the right thing for a child: it lifts the menu
        # above its SIBLINGS, i.e. above the canvas, without the old
        # always-on-top behaviour that used to cover colour pickers.
        self.raise_()

    def show_above_rect(self, canvas, screen_rect_top_center: QPoint,
                        screen_rect_bottom_center: QPoint = None,
                        clearance: int = 0):
        """Position the menu above the selection box, leaving ``clearance`` px
        so it never covers the rotation handle. If that would push it above the
        visible canvas, drop it BELOW the selection instead (the rotation
        handle only lives above, so no clearance is needed there)."""
        self.adjustSize()
        w = self.width()
        h = self.height()
        gap = UiScale.px(12)
        x = screen_rect_top_center.x() - w // 2
        y = screen_rect_top_center.y() - clearance - h - gap
        try:
            canvas_top = canvas.mapToGlobal(QPoint(0, 0)).y()
        except Exception:
            canvas_top = 0
        if y < canvas_top + UiScale.px(8) and screen_rect_bottom_center is not None:
            y = screen_rect_bottom_center.y() + gap
        self.move(x, y)
        if not self.isVisible():
            self.show()
        # Stack above the canvas without WindowStaysOnTopHint (which forced
        # the menu above color pickers / dialogs too). A plain raise() lifts
        # it above sibling windows but still lets modal popups appear on top.
        self.raise_()