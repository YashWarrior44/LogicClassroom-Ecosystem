"""
canvas_widget.py — Central drawing surface for LogicBoard v3.0.
"""
from __future__ import annotations
import logging, math, time
from typing import Optional, TYPE_CHECKING
from PySide6.QtWidgets import QWidget, QApplication, QScrollBar
from PySide6.QtGui import (QPainter, QPen, QBrush, QColor, QPixmap, QPainterPath,
                            QFont, QPolygonF, QTransform, QImageReader)
from PySide6.QtCore import (Qt, QPoint, QPointF, QRect, QRectF, QEvent, Signal,
                            QTimer)
from ui.util.scaling import UiScale
from ui.util.pixmap_cache import PixmapCache
from ui.util.image_header import image_size_from_header
from ui.theme.theme_manager import theme_mgr
import ui.canvas.erase_geometry as eg
from ui.canvas import shape_geometry as sg

if TYPE_CHECKING:
    from ui.canvas.input_handler import InputHandler

try:
    import logicboard_engine as lbe
    HAS_ENGINE = True
except ImportError:
    lbe = None
    HAS_ENGINE = False

__all__ = ["CanvasWidget"]

# Rotation handle geometry (screen pixels). input_handler imports these so the
# drawn handle and the hit-test stay in sync. Sized large for touch / smart
# boards, and offset well clear of the box so the selection menu doesn't cover it.
ROTATE_HANDLE_OFFSET = 54.0   # gap above the selection's top edge
ROTATE_HANDLE_RADIUS = 19.0   # radius of the round 🔄 grab handle
# The tallest highlighter (board px). The pen panel's slider stops here and
# its preview is built to show this size whole.
HIGHLIGHTER_MAX_HEIGHT = 60.0


class CanvasWidget(QWidget):
    page_changed = Signal(int, int)
    dirty_changed = Signal(bool)
    selection_changed = Signal()        # emitted when selection set/cleared
    content_changed = Signal()          # emitted on every committed scene change
    rotating_changed = Signal(bool)     # emitted when rotation starts / ends
                                        # — main_window connects to this so
                                        # the floating SelectionMenu can hide
                                        # while a rotate is in flight and
                                        # reappear when the user lets go.
                                        # (stroke commit, delete, color, etc.) —
                                        # NOT during live drawing

    # ---- Live-stroke rendering tunables ----
    # How many already-baked samples the incremental tail path re-covers. 2 is
    # exactly what's needed: resuming at mid(p[s], p[s+1]) requires both of
    # those samples, and that midpoint is where the baked chain ended.
    _TAIL_OVERLAP = 2

    # Settled geometry is baked into the layer in batches rather than one quad
    # per sample. Each bake is a separately stroked path, and adjacent round
    # caps double-blend the antialias fringe at the junction — so fewer,
    # longer bakes means less accumulated error. The still-unsettled tail is
    # redrawn per frame, keeping the leading edge at one-shot quality.
    _BAKE_BATCH = 12

    # ---- Scrollable-list tunables ----
    # Gap between sheets, in SCREEN pixels — a constant gap regardless of
    # zoom, which is what makes the stack read as separate sheets of paper
    # rather than one long strip when zoomed out.
    #
    # It is also where each sheet's page number is printed, which is what
    # sets the size: the gap has to fit a line of text with a little air
    # above and below it, not merely separate two rectangles.
    _LIST_GAP = 34.0
    # Horizontal breathing room beside the sheets. Was the same constant as
    # the vertical gap until the gap had to grow for the page label — these
    # are two different jobs and only one of them has text in it.
    #
    # Wide enough to be a real margin rather than a hairline: at a high zoom
    # the edge of a sheet is where diagrams and annotations end up, and ink
    # right against the window edge is awkward to reach and easy to clip with
    # a palm resting on the bezel.
    _LIST_MARGIN = 48.0
    # Dead space AFTER the last sheet, on top of the usual gap. Without it the
    # board stops scrolling with the final page flush to the bottom of the
    # window — which is exactly where the floating toolbar sits, so the last
    # couple of centimetres of the last page could never be looked at. See
    # ``set_bottom_inset`` for the toolbar's own contribution.
    _LIST_END_PAD = 40.0
    # Page-number label, drawn in the gap ABOVE the sheet it names.
    _LIST_LABEL_PX = 13.0
    _LIST_SCALE_MIN = 0.08
    _LIST_SCALE_MAX = 3.0

    # ---- Momentum ----
    # Release velocities are in SCREEN PIXELS PER MILLISECOND throughout, so
    # these read as "how fast was the finger going" rather than needing a
    # frame rate to interpret.
    #
    # Below MIN a release starts nothing: a slow release is somebody putting
    # the board down where they want it, and gliding on past takes it away
    # from them again. Above MAX is a digitiser reporting nonsense for one
    # sample, which uncapped throws the board the length of the document.
    _FLING_MIN_V = 0.10          # ~100 px/s
    _FLING_MAX_V = 8.0           # ~8000 px/s
    _FLING_STOP_V = 0.05         # the glide has arrived
    # Exponential decay per millisecond. Distance left to travel is v/k, so
    # this constant IS the feel: at 0.0028 a 2 px/ms flick coasts about
    # 700 px and settles in a bit over a second.
    #
    # Tuned against the Media Manager's panes, which are the reference for
    # how scrolling should feel here: those run on QScroller with
    # DecelerationFactor 0.10 (see touch_scroll._tune_scroller, where the Qt
    # default of 0.42 is called out for stopping a flick almost immediately).
    # A hand-rolled fling is a different mechanism, so the number is not
    # transferable — what was matched is the RESULT: a firm flick crosses
    # several screens of content and keeps moving for about a second, rather
    # than stopping the moment the finger leaves.
    _FLING_DECAY = 0.0028
    _FLING_TICK_MS = 16

    # Samples closer together than this (in SCREEN px) are dropped. Stylus and
    # touch digitisers report at 100-300 Hz; at normal writing speed that is
    # many samples per pixel, and every one of them used to trigger a full
    # re-render. 1.5 px is below the visible threshold at any sane pen width.
    _MIN_SAMPLE_DIST_SQ = 2.25

    def __init__(self, parent=None):
        super().__init__(parent)
        self.setMouseTracking(True)
        self.setAttribute(Qt.WA_AcceptTouchEvents, True)
        self.setFocusPolicy(Qt.StrongFocus)

        # Momentum. The timer is built on first use — a board that is only
        # ever drawn on should not carry an idle QTimer around.
        self._fling_timer = None
        self._fling_vy = 0.0
        self._fling_vx = 0.0
        self._fling_last_t = 0.0
        # Bottom of the viewport covered by the floating toolbar. Reported by
        # MainWindow._position_toolbar; see set_bottom_inset.
        self._bottom_inset = 0.0
        # Guards re-entry while _sync_scrollbar writes the bar's value: the
        # write emits valueChanged, whose slot scrolls the canvas, which
        # syncs the bar again. Without the flag that is a loop, and with a
        # rounded float offset it is a loop that does not even settle.
        self._sb_syncing = False
        # Set, so the bar is sized from the first real layout rather than
        # from the empty board this constructor leaves behind.
        self._sb_stale = True
        self._vbar = self._build_scrollbar()

        self._board = None; self._current_page = 0
        self._command_stacks = []; self._input_handler = None
        # Background fit modes, keyed by the background's relative path.
        # See BG_FIT_MODES.
        self._bg_fit: dict = {}

        self._tool = "pen"
        # Defaults come from the ink palette, so the panel opens with a
        # real swatch selected rather than the custom cell.
        self._pen_color = QColor(92, 162, 255)        # Azure
        self._pen_width = 3.0
        # Eraser sizes are stored PER MODE so changing one tool's size never
        # touches the other's. ``eraser_size`` is a thin façade that resolves
        # to the active mode's value, keeping the existing API stable.
        self._eraser_pixel_size  = 50.0
        self._eraser_object_size = 50.0
        # "pixel" is the Simple eraser — it rubs out only what the brush
        # actually passes over. That's what a whiteboard eraser does, so it's
        # the least surprising default; "object" deletes a whole stroke on
        # contact, which is powerful but startling if you didn't ask for it.
        self._eraser_mode = "pixel"
        # Live pixel-eraser preview: capsule segments (ax, ay, bx, by, radius)
        # in WORLD-space coordinates. While the user drags the pixel eraser,
        # input_handler appends to this list (instead of painting bg-colour
        # onto the live layer like the old broken design). The freehand /
        # highlighter renderers consult this mask to CUT OUT the covered
        # pixels in real time — without ever touching the underlying scene
        # geometry or any imported media. Cleared by _commit_pixel_erase on
        # release, which then makes the change permanent in the engine.
        self._eraser_mask_segments: list = []
        # Live erase state, per object:
        #   str(obj.id) -> [n_applied, eg.InkRegion, untouched base path]
        # The region starts as the object's painted area, pre-split into
        # small pieces, and has each new capsule subtracted from it ONCE;
        # ``n_applied`` records how far down _eraser_mask_segments that has
        # got to.
        #
        # This shape is the whole performance fix, in two steps. The original
        # code kept a single ever-growing union of every capsule and
        # re-subtracted that entire path from every touched stroke on every
        # frame, so a drag cost O(segments) per stroke per frame and got
        # slower the longer you rubbed — the lag. Folding one capsule into
        # one running remainder made that O(1) per frame. Splitting the
        # remainder into pieces then made the constant small, because a
        # boolean op costs what its operands cost and a whole stroke outline
        # is ~2700 path elements — see the erase_geometry docstring.
        self._erase_state: dict = {}
        self._erase_overlay_cache = (-1, set())
        # Ids whose ink the eraser has actually REMOVED some of. These are the
        # ones held out of the scene cache and painted live; everything else
        # stays cached and untouched.
        #
        # The distinction is not pedantic. The set the overlay used to use was
        # "ink whose bounding box a capsule crossed", which on a page of
        # ordinary handwriting is five to ten times larger — a diagonal rub
        # crosses hundreds of boxes and cuts a few dozen strokes. Every one of
        # those had to be lifted out of the cache and redrawn on every frame
        # for the rest of the drag, and that was 91% of what a pointer sample
        # cost once the boolean work had been dealt with.
        self._erase_cut_ids: set = set()
        # ...and the same objects as snapshot entries in scene order, so the
        # overlay can iterate the handful it draws instead of filtering the
        # whole page's worth of snapshot on every frame.
        self._erase_cut_entries: list = []
        self._erase_fresh_hits: dict = {}
        # World bboxes of every erasable object, snapshotted once per drag,
        # plus a uniform-grid index over them — see _erase_overlay_ids.
        self._erase_bbox_snapshot: "list | None" = None
        self._erase_bbox_grid: tuple = ({}, [])

        # Cached "everything-minus-capsules" clip REGION, built lazily — see
        # _eraser_mask_clip_region. Only shapes that the eraser deletes
        # wholesale (line/rect/ellipse/arrow/...) use it, so on a board made
        # of ink it is never built at all.
        self._eraser_clip_cache: "QRegion | None" = None
        self._eraser_clip_len: int = 0
        # str(obj.id) -> (n_points, height, QPainterPath) for committed
        # highlighter ribbons. See _r_highlighter.
        self._hl_path_cache: dict = {}
        # Decoded backgrounds and image objects, bounded by bytes rather
        # than by nothing. Created here so callers that reset it — opening
        # a board — can always just clear() it.
        self._pixmap_cache = PixmapCache()
        # Image DIMENSIONS, read from file headers rather than by decoding.
        # Tiny — a tuple per source — so it is never evicted.
        self._image_size_cache: dict = {}

        # ── Move overlay (drag-translate live preview) ────────────────────
        # While the user drags a selection, the dragged objects are rendered
        # as a floating overlay (paintEvent) at a live (dx, dy) offset
        # instead of being baked into the scene cache. Cache rebuild is the
        # expensive part of a move; with an overlay it only fires on
        # drag-start (when the moving objects are excluded from the cache)
        # and drag-release (when they're baked back in at their final spot).
        # ``_move_overlay_ids``: ids of objects currently being floated.
        # ``_move_overlay_dx/dy``: live translation in world units.
        self._move_overlay_ids: set = set()
        self._move_overlay_dx: float = 0.0
        self._move_overlay_dy: float = 0.0

        # ``_rotating``: True while the user is dragging the rotation handle.
        # Two things read this flag:
        #   1. _paint_selection() skips drawing the selection rectangle +
        #      handles when it's True. Painting them mid-rotation shows the
        #      pre-rotation bbox (they aren't updated live), which is
        #      distracting and misleading.
        #   2. main_window listens on rotating_changed to hide/show the
        #      floating SelectionMenu widget — that one lives outside the
        #      canvas so canvas can't hide it directly.
        # set_rotating(bool) is the sole entry point that mutates the flag,
        # so both effects stay in lockstep.
        self._rotating: bool = False
        self._highlighter_color = QColor(255, 201, 60)   # Sunflower
        self._highlighter_height = 35.0
        self._highlighter_alpha = 75
        # Brush pen — the same chisel ribbon, opaque and tilted. Its own
        # colour and size, because it is used for writing rather than
        # marking up and the two want different settings.
        # Same ink as the pen, so picking the brush up is a change of
        # stroke shape and nothing else. A 12 px nib, not the pen's 3: a nib
        # that small is nearly square, so a fresh brush drew a uniform line
        # indistinguishable from the pen — the thick-and-thin only appears
        # once the nib is several times longer than it is thick.
        self._brush_color = QColor(92, 162, 255)
        self._brush_height = 12.0
        # Dual Pen carries its OWN ink rather than borrowing the pen's.
        # Sharing meant recolouring either one silently recoloured the other,
        # which is a surprise in both directions: the two are configured on
        # separate screens and used for different things.
        self._dual_pen_color = QColor(92, 162, 255)
        # Shapes carry their own colour too, picked in the shapes panel —
        # they used to draw in the pen's, so recolouring a shape recoloured
        # your handwriting.
        from ui.theme.ink_palette import DEFAULT_SHAPE_COLOR
        self._shape_color = QColor(DEFAULT_SHAPE_COLOR)

        self._view_offset = QPointF(0, 0); self._view_scale = 1.0
        self._scroll_y = 0.0  # vertical scroll offset (pixels, >=0 scrolls down)
        # Horizontal companion to _scroll_y, and LIST STYLE ONLY. One-page
        # has nothing to scroll sideways: its scale is pinned at 1.0 and a
        # page is fitted to the viewport by definition, so _max_scroll_x
        # answers 0 there and every path below is inert. The list is
        # different — zoom in past the window width and the sheet genuinely
        # extends past both edges with no way to reach it.
        self._scroll_x = 0.0
        self._content_height = 0.0  # cached scrollable content height
        # ---- Canvas style ------------------------------------------------
        # "one_page"    : the original. One page fills the viewport, and
        #                 _view_scale / _view_offset / _scroll_y mean exactly
        #                 what they always meant.
        # "scroll_list" : every page of the SAME board stacked vertically,
        #                 like a document. Backed by the same page model —
        #                 there is no second list of pages anywhere.
        #
        # The list's zoom lives in its OWN value rather than reusing
        # _view_scale. That is deliberate: one-page zoom must be untouched by
        # the sensitivity setting, and sharing the field would make every
        # two-finger pinch in list mode change what a one-page user sees
        # when they switch back.
        self._canvas_style = "one_page"
        self._list_scale = 0.35
        # Memoised page_layout(). It is asked for several times per FRAME —
        # the paint, the scroll clamp, the centre-page sync, and once per
        # live overlay via page_rect_on_screen — and each call now measures
        # every page's background to size its sheet. On a 100-page PDF that
        # was ~2 ms a call, i.e. milliseconds of ink latency per stroke
        # sample on the board this runs on.
        self._layout_cache = None
        self._layout_key = None
        self._layout_epoch = 0
        self._list_zoom_sensitivity = 1.0
        self._show_eraser_cursor = False; self._cursor_pos = QPoint(-100, -100)
        # Where the ring was ACTUALLY painted last frame, or None if it wasn't
        # painted at all. ``_cursor_pos`` is only an intention — the ring is
        # drawn from it, but a tool change or a lifted contact can hide the
        # ring while the position stays put. Clearing stale ring pixels needs
        # to know where the ring really is on screen right now, which is what
        # this records (set in paintEvent, consumed by move_eraser_cursor).
        self._ring_last_painted = None
        # Radius the ring was painted WITH, recorded alongside the position.
        # The size can change under a ring that is already on screen (the
        # size slider), and scrubbing the old pixels with the NEW radius
        # leaves a rim of the old circle behind when the new one is smaller.
        self._ring_last_radius = 0
        # Watchdog that parks the ring when input goes quiet on hardware
        # with no hovering pointer. See _arm_ring_watchdog.
        self._ring_watchdog = QTimer(self)
        self._ring_watchdog.setSingleShot(True)
        self._ring_watchdog.setTimerType(Qt.CoarseTimer)
        self._ring_watchdog.timeout.connect(self._on_ring_idle)
        # --- eraser-ring tracking (see mousePressEvent for the full story) ---
        # ``_pointer_down`` is "a contact/button is currently held". It is the
        # only thing that keeps the ring on screen on hardware with no hover.
        # ``_hover_seen`` latches True the first time a REAL mouse moves over
        # the canvas with no button held. A touch-only board can never produce
        # that event, so this doubles as a runtime probe for "does this machine
        # have a hovering pointer at all?" — which decides whether the ring
        # should stay put after a release (mouse: yes, it follows the pointer)
        # or be parked (touchscreen: nothing will EVER move it again).
        self._pointer_down = False
        self._hover_seen = False
        # Buttonless moves seen since the last press/release. Windows emits
        # ONE of those right before a touch-down (it warps the cursor to the
        # contact first), so a single hover move proves nothing — but a real
        # mouse emits a continuous stream of them the instant a hand rests on
        # it. Requiring a run of them is what keeps a touch-only board from
        # being mistaken for a hovering pointer. Reset on every press and
        # release so taps can't accumulate their way past the threshold.
        self._hover_moves = 0
        self._HOVER_MOVES_TO_TRUST = 3
        self._scene_cache = None; self._cache_dirty = True
        self._cache_dpr = 1.0        # DPR the cache was rasterised at

        # Live stroke
        self._live_points = []; self._live_pressures = []
        self._live_active = False; self._live_is_highlighter = False; self._live_is_eraser = False
        # Chisel = highlighter OR brush; they share the ribbon geometry and
        # differ only in tilt and opacity.
        self._live_is_brush = False; self._live_is_chisel = False
        self._live_nib_angle = 0.0
        # Incremental live-stroke cache: the in-progress stroke is rasterised
        # ONCE into this layer (segment-by-segment as points arrive) instead of
        # rebuilding the whole path every frame — keeps long strokes O(1)/point.
        self._live_layer = None        # QPixmap in widget space (or None)
        self._live_drawn_idx = 0       # points already drawn into the layer
        self._live_baked_idx = 0       # samples baked into the layer so far
        self._live_tail_start = 0      # first sample of the unsettled tail
        # ONE centreline per chisel stroke, grown as samples arrive and
        # sliced by both the baked layer and the tail. See _live_chisel_dense.
        self._live_thin = []           # thinned points, chained from sample 0
        self._live_thin_src = []       # the raw sample each one came from
        self._live_thin_upto = 0       # raw samples already folded in
        self._live_dense = None        # cached _smooth_dense of the above
        self._live_dense_n = -1        # sample count that cache was built at
        self._live_dense_settled = 0   # dense points that can no longer move
        self._live_bake_k = 0          # dense points already in the layer
        self._live_tail_rect = None    # its widget rect, for invalidation

        # Shape preview
        self._shape_tool = ""; self._shape_start = QPointF(); self._shape_end = QPointF()
        self._shape_active = False

        # Selection
        self._selection_rect = None; self._selected_ids = []

        # Rect-select
        self._rectsel_start = None; self._rectsel_end = None; self._rectsel_active = False

        # Lasso — see _paint_lasso / add_lasso_point for the perf design.
        #   _lasso_points     : authoritative point list (kept for selection hit-test)
        #   _lasso_path       : cached QPainterPath, extended incrementally
        #   _lasso_last_screen: prior point in *screen* coords, for dirty-rect math
        self._lasso_points = []; self._lasso_active = False
        # True while the lasso is the LASSO ERASER's: drawn in the eraser's
        # red with a faint fill over what is about to go.
        self._lasso_erase = False
        self._lasso_path = None; self._lasso_last_screen = None

        # Palm
        self._palm_eraser_enabled = True; self._palm_min_size = 80
        # Object eraser: whether it can delete imported media (image/ppt). Off
        # by default so users don't lose imports by tapping them.
        self._object_eraser_affects_media = False
        self._palm_erasing = False; self._ignore_mouse_until = 0.0
        # Phase 3: runtime touch classifier + touch mode. Set by MainWindow
        # after the InputCalibration manager is created. When None, the input
        # handler falls back to the legacy size heuristic.
        self._touch_classifier = None
        self._touch_mode = "palm_rejection"
        # Developer overlay: when True, draws the last detected input type.
        self._debug_input_overlay = False
        self._last_input_class = ""
        # Which Qt event family last reached the canvas ("mouse" / "touch" /
        # "tablet <tip>"). Diagnostic only — read by the Ctrl+Shift+D
        # overlay. Worth having permanently: the eraser-ring bugs were all
        # "this input path never updates the ring", and knowing which path
        # a given board actually uses is the first question every time.
        self._last_input_family = ""
        # --- input diagnostics (Ctrl+Shift+D overlay + optional logging) ---
        # ``_last_input_family`` records only the family that was ACTED ON.
        # It used to be stamped unconditionally at the top of every handler,
        # including handlers that discard the event one line later — so on a
        # board that emits touch and synthesised mouse together, the HUD read
        # "mouse" for the whole stroke while touch was actually driving it.
        # Suppressed families are tracked separately and shown in brackets.
        self._suppressed_family = ""
        self._suppressed_at = 0.0
        # Live contact geometry, for diagnosing a digitizer that won't report
        # large contacts: how many points are down, and the biggest one's
        # ellipse in px² (0 when the board reports no ellipse at all — itself
        # a useful finding, since palm detection depends on that number).
        self._touch_count = 0
        self._touch_max_area = 0.0
        self._touch_max_wh = (0.0, 0.0)
        self._touch_peak_area = 0.0   # high-water mark since the overlay went on
        # When True every input event is written to the debug log. Off by
        # default — this is a lot of lines. Toggled with the overlay.
        self._log_input_events = False
        self._dirty = False

        self._init_engine()
        theme_mgr.theme_changed.connect(self._on_theme_changed)

    # ---- Engine ----
    def _screen_size(self):
        scr = QApplication.primaryScreen()
        return (scr.geometry().width(), scr.geometry().height()) if scr else (1920, 1080)
    def _init_engine(self):
        if not HAS_ENGINE: return
        w, h = self._screen_size()
        self._board = lbe.Board("Untitled"); self._board.theme = theme_mgr.theme_name
        first = self._board.page_mut(0); first.set_canvas_size(w, h); self._apply_bg(first)
        # NOTE: never cache Scene& references — the board owns them and they can
        # move. We only cache command stacks (one per page index) and re-fetch
        # the live Scene from the board on demand via current_scene().
        self._command_stacks = [lbe.CommandStack(200)]
        # A fresh board carries none of the previous board's fit choices.
        self._bg_fit = {}
        self._current_page = 0; self._cache_dirty = True
    def _apply_bg(self, scene):
        bg = theme_mgr.qcolor(theme_mgr.tokens.canvas_bg_color)
        scene.background_color = lbe.RGBA(bg.red(), bg.green(), bg.blue(), 255)
    #: Canvas colours earlier versions wrote into every page as the DEFAULT.
    #: A page still carrying one of these never chose its colour — it just
    #: got whatever the default was when it was made — so it follows the
    #: current default instead of being frozen in the old one.
    OLD_DEFAULT_BG = {(28, 28, 28)}

    def adopt_default_bg(self) -> int:
        """Move pages still on an OLD default canvas colour to the current
        one. Returns how many pages changed. Not an edit: the board is not
        marked dirty, it is simply shown in today's default."""
        if not HAS_ENGINE or self._board is None:
            return 0
        bg = theme_mgr.qcolor(theme_mgr.tokens.canvas_bg_color)
        new = (bg.red(), bg.green(), bg.blue())
        changed = 0
        for i in range(self._board.page_count):
            s = self._board.page_mut(i)
            c = s.background_color
            if (c.r, c.g, c.b) in self.OLD_DEFAULT_BG and (c.r, c.g, c.b) != new:
                s.background_color = lbe.RGBA(*new, 255)
                changed += 1
        if changed:
            self.invalidate_cache()
        return changed

    def get_bg_color(self):
        scene = self.current_scene()
        if scene and HAS_ENGINE:
            c = scene.background_color; return QColor(c.r, c.g, c.b, c.a)
        return theme_mgr.qcolor(theme_mgr.tokens.canvas_bg_color)

    # ---- Properties ----
    @property
    def tool(self): return self._tool
    def set_tool(self, t):
        self._tool = t; self._sync_eraser_cursor()
        if t == "eraser" and self._eraser_mode == "lasso": self.setCursor(Qt.CrossCursor)
        elif t in ("pen", "highlighter", "brush", "eraser", "dual_pen"): self.setCursor(Qt.BlankCursor)
        elif t in sg.SHAPE_TOOLS: self.setCursor(Qt.CrossCursor)
        else: self.setCursor(Qt.ArrowCursor)
        # Selection survives a switch to "select" OR "dual_pen" (since Dual Pen
        # might route the back tip to the selection tool).
        if t not in ("select", "dual_pen"): self.clear_selection()
        self.update()
    @property
    def pen_color(self): return QColor(self._pen_color)
    def set_pen_color(self, c): self._pen_color = QColor(c)
    @property
    def pen_width(self): return self._pen_width
    def set_pen_width(self, w): self._pen_width = max(1.0, min(100.0, w))
    @property
    def eraser_size(self):
        """Active eraser size — resolves to whichever mode is selected.
        Each mode keeps its own value (``_eraser_pixel_size`` /
        ``_eraser_object_size``); switching modes restores that mode's
        previously-set size automatically."""
        if self._eraser_mode == "pixel":
            return self._eraser_pixel_size
        return self._eraser_object_size

    def set_eraser_size(self, s):
        s = max(5.0, min(500.0, s))
        if self._eraser_mode == "pixel":
            self._eraser_pixel_size = s
        else:
            self._eraser_object_size = s
        # The ring may be on screen at the OLD radius — while the size
        # slider is being dragged it always is. Repaint both boxes;
        # _stale_ring_rect knows what was really painted.
        self._repaint_eraser_ring()
    @property
    def eraser_mode(self): return self._eraser_mode
    def set_eraser_mode(self, m):
        self._eraser_mode = m
        # Lasso has no size and no ring: it is drawn, like the selection
        # lasso, so the pointer is a crosshair instead of the eraser ring.
        self._sync_eraser_cursor()
        if self._tool == "eraser":
            self.setCursor(Qt.CrossCursor if m == "lasso" else Qt.BlankCursor)
        self._repaint_eraser_ring()
    @property
    def dual_pen_color(self): return QColor(self._dual_pen_color)
    def set_dual_pen_color(self, c): self._dual_pen_color = QColor(c)

    def active_pen_color(self) -> QColor:
        """The ink a PEN stroke should use right now.

        Dual Pen resolves to the ordinary pen tool for its front tip, so
        without this the stroke would reach for ``pen_color`` and the two
        modes would be sharing one colour again through the back door. One
        helper, consulted by both the live preview and the commit, keeps
        them apart wherever a pen stroke is actually made.
        """
        if self.tool == "dual_pen":
            return QColor(self._dual_pen_color)
        return QColor(self._pen_color)

    @property
    def shape_color(self): return QColor(self._shape_color)
    def set_shape_color(self, c): self._shape_color = QColor(c)

    @property
    def brush_color(self): return QColor(self._brush_color)
    def set_brush_color(self, c): self._brush_color = QColor(c)

    @property
    def brush_height(self): return self._brush_height
    def set_brush_height(self, h): self._brush_height = max(5.0, min(200.0, h))

    @property
    def highlighter_color(self): return QColor(self._highlighter_color)
    def set_highlighter_color(self, c): self._highlighter_color = QColor(c)
    @property
    def highlighter_height(self): return self._highlighter_height
    def set_highlighter_height(self, h):
        self._highlighter_height = max(5.0, min(HIGHLIGHTER_MAX_HEIGHT, float(h)))
    @property
    def palm_eraser_enabled(self): return self._palm_eraser_enabled
    def set_palm_eraser_enabled(self, v): self._palm_eraser_enabled = v
    @property
    def palm_min_size(self): return self._palm_min_size
    def set_palm_min_size(self, v): self._palm_min_size = max(10, min(300, v))
    @property
    def object_eraser_affects_media(self): return self._object_eraser_affects_media
    def set_object_eraser_affects_media(self, v): self._object_eraser_affects_media = bool(v)
    @property
    def view_offset(self): return QPointF(self._view_offset)
    @property
    def view_scale(self): return self._view_scale
    def set_view(self, o, s):
        self._view_offset = QPointF(o); self._view_scale = max(0.1, min(10.0, s))
        self._cache_dirty = True; self.update()
    def reset_view(self):
        self._view_offset = QPointF(0, 0); self._view_scale = 1.0
        self._cache_dirty = True; self.update()

    # ---- Canvas style -------------------------------------------------
    @property
    def canvas_style(self) -> str:
        return self._canvas_style

    def set_canvas_style(self, style: str) -> None:
        """Switch between one-page and the scrollable page list.

        Only presentation changes. The board, its pages, their command
        stacks and the current-page index are all untouched, so switching
        back and forth cannot corrupt page state — there is nothing to keep
        in sync because there is only one model.
        """
        style = "scroll_list" if style == "scroll_list" else "one_page"
        if style == self._canvas_style:
            return
        self.stop_fling()
        self._canvas_style = style
        # Scroll is measured in different units in the two modes (one page's
        # content vs the whole list), so a value carried across would put the
        # view somewhere arbitrary.
        self._scroll_y = 0.0
        self._scroll_x = 0.0
        self._cache_dirty = True
        self.clamp_scroll()
        self.update()

    @property
    def list_scale(self) -> float:
        return self._list_scale

    def set_list_scale(self, s: float) -> None:
        s = max(self._LIST_SCALE_MIN, min(self._LIST_SCALE_MAX, float(s)))
        if abs(s - self._list_scale) > 1e-4:
            self._list_scale = s
            self._cache_dirty = True
            # Zooming OUT shrinks the list under the current offset.
            self.clamp_scroll()
            # …and changes WHICH page is in the middle, so the indicator has
            # to follow. Without this the navigator kept naming the page you
            # were on before the zoom while a different one filled the view.
            self.sync_current_page_to_center()
            self.update()

    def set_zoom_sensitivity(self, v: float) -> None:
        self._list_zoom_sensitivity = max(0.25, min(3.0, float(v)))

    @property
    def zoom_sensitivity(self) -> float:
        return self._list_zoom_sensitivity

    def _page_size(self, i: int):
        """A page's size in scene units — big enough to show all of it.

        Height is the canvas OR the background image's rendered bottom,
        whichever is lower. A scanned page or a tall PDF is imported in
        "natural" fit, which keeps the image's aspect by extending BELOW the
        canvas: one-page style lets you scroll down inside the page to reach
        it, but a list has no inside to scroll. Sizing the sheet to the
        canvas alone silently cut a 2400-tall background off at 800 and
        there was no way to see the rest.

        Deliberately NOT ``_compute_content_height``, which also counts the
        lowest object: sheets would then grow as ink strays past the bottom
        edge, and the page would change shape while being drawn on.
        """
        sc = self.scene_at(i)
        if sc is None:
            return float(self.width() or 1), float(self.height() or 1)
        w = float(sc.canvas_width or self.width() or 1)
        h = float(sc.canvas_height or self.height() or 1)
        try:
            # _bg_dest_rect, not _bg_image_layout: this only needs to know
            # how far down the image reaches, and page_layout() asks it for
            # EVERY page. Decoding each one to measure it is what froze a
            # 1000-page board for 29 seconds on a style switch.
            dest = self._bg_dest_rect(sc)
            if dest is not None:
                # HEIGHT only — exactly what _compute_content_height extends
                # in one-page style. "Cover" fit overflows sideways ON
                # PURPOSE and is meant to be centre-cropped; widening the
                # sheet to swallow that overflow made a 16:9 background turn
                # its page into a wide slab instead of cropping it.
                h = max(h, float(dest.bottom()))
        except Exception:
            pass
        return w, h

    def _bump_layout(self) -> None:
        """Invalidate the memoised layout. Cheap; call it liberally."""
        self._layout_epoch += 1
        self._layout_cache = None
        # Adding, deleting or re-sizing a page changes how far there is to
        # scroll, so the bar needs re-syncing — but only a FLAG is set here,
        # never the sync itself. Syncing measures the layout, and half these
        # callers bump first and change the thing being measured second
        # (``set_bg_fit_mode`` is the clearest), so a read from inside this
        # function re-memoises the layout it was called to throw away. The
        # paint path below acts on the flag, by which point every caller has
        # finished changing whatever it was changing.
        self._sb_stale = True

    def page_layout(self):
        """[(index, QRectF)] for every page, in LIST space (scroll not applied).

        List space is the whole stack laid out top to bottom at
        ``_list_scale``, each sheet centred horizontally. Screen space is
        this minus ``_scroll_y``. Keeping the two separate is what lets hit
        testing, painting and the scroll clamp all share one definition of
        where a page is.
        """
        key = (self.page_count, round(self._list_scale, 5), self.width(),
               self._layout_epoch)
        if self._layout_key == key and self._layout_cache is not None:
            return self._layout_cache
        rects = []
        y = self._LIST_GAP
        for i in range(self.page_count):
            pw, ph = self._page_size(i)
            w = pw * self._list_scale
            h = ph * self._list_scale
            # Centred while the sheet fits, pinned to the left margin once it
            # doesn't. The clamp is what makes horizontal scrolling MEAN
            # something: without it a sheet wider than the window would be
            # centred on a negative x and half of it would be unreachable off
            # the left edge no matter how far you scrolled right.
            x = max(self._LIST_MARGIN, (self.width() - w) / 2.0)
            rects.append((i, QRectF(x, y, w, h)))
            y += h + self._LIST_GAP
        self._layout_key = key
        self._layout_cache = rects
        return rects

    def set_bottom_inset(self, px: float) -> None:
        """How much of the viewport's bottom edge is covered by other UI.

        The toolbar floats OVER the canvas as its own top-level window, so
        nothing in the layout knows it is there and the canvas would happily
        stop scrolling with the last page underneath it. MainWindow measures
        the pills and reports the figure here, which turns into scrollable
        space at the end of the board rather than into a moved page — the
        content still starts where it always did, there is simply more room
        to scroll past it.
        """
        px = max(0.0, float(px))
        if abs(px - self._bottom_inset) < 0.5:
            return
        self._bottom_inset = px
        self._bump_layout()
        self.clamp_scroll()
        self.update()

    def _list_content_height(self) -> float:
        rects = self.page_layout()
        if not rects:
            return float(self.height())
        # Gap, then the fixed tail pad, then whatever the toolbar is covering.
        # All three are SCROLLABLE space past the last sheet, which is what
        # lets the bottom of that sheet be brought up clear of the toolbar.
        return (rects[-1][1].bottom() + self._LIST_GAP
                + self._LIST_END_PAD + self._bottom_inset)

    def _list_content_width(self) -> float:
        """Width of the whole stack in LIST space, margin included.

        The widest sheet decides it. Pages of different sizes can share a
        board (a PDF import beside a blank page), and scrolling has to reach
        the edge of the widest one rather than of whichever happens to be
        current.
        """
        rects = self.page_layout()
        if not rects:
            return float(self.width())
        return max(r.right() for _i, r in rects) + self._LIST_MARGIN

    def page_rect_on_screen(self, i: int):
        for idx, r in self.page_layout():
            if idx == i:
                return QRectF(r.x() - self._scroll_x, r.y() - self._scroll_y,
                              r.width(), r.height())
        return None

    def page_at_viewport_center(self) -> int:
        """The page the user is actually LOOKING at: the one nearest the
        middle of the window.

        In a list every page exists at once, so "which page am I on" stops
        being a stored index and becomes a question about the view. Nearest
        centre rather than strictly-contains, so the answer is still sensible
        while the middle of the screen is in a gap between two sheets.
        """
        mid = self.height() / 2.0
        best, best_d = self._current_page, None
        for i, r in self.page_layout():
            top = r.top() - self._scroll_y
            centre = top + r.height() / 2.0
            d = abs(centre - mid)
            if best_d is None or d < best_d:
                best, best_d = i, d
        return best

    def sync_current_page_to_center(self) -> bool:
        """Make the centre page current. True if it changed.

        Called after every scroll in list style so the page navigator reports
        what is on screen instead of whatever was last drawn on. Only fires
        when the index actually changes — at most once per page boundary
        crossed, not once per frame of a flick.
        """
        if self._canvas_style != "scroll_list":
            return False
        i = self.page_at_viewport_center()
        if i == self._current_page or not (0 <= i < self.page_count):
            return False
        self._current_page = i
        self._cache_dirty = True
        self.page_changed.emit(self._current_page, self.page_count)
        self.update()
        return True

    def page_at_screen(self, sp):
        """Index of the page under a screen point, or None for the gaps.

        Used to route input: a press in list mode makes the page under the
        pointer current, so every existing tool keeps working through
        ``current_scene`` / ``current_stack`` exactly as it does in one-page
        mode. That is what keeps drawing, erasing, selection and per-page
        undo on the existing model instead of a parallel one.
        """
        y = sp.y() + self._scroll_y
        x = sp.x() + self._scroll_x
        for i, r in self.page_layout():
            if r.top() <= y <= r.bottom() and r.left() <= x <= r.right():
                return i
        return None

    def accepts_input_at(self, sp) -> bool:
        """Is there a page under this point to draw ON?

        In list style the board is sheets with GAPS between them and empty
        margins either side, and none of that is part of any page. A press
        there used to be mapped relative to whichever page was current and
        drawn anyway, landing outside the page's own bounds — invisible,
        because the sheet clips it, but a real object in the scene: object
        counts grew, undo filled with entries for ink nobody could see, and
        it went into every save.

        One-page style has no such thing as "off the page", so it always
        accepts and behaves exactly as before.
        """
        if self._canvas_style != "scroll_list":
            return True
        return self.page_at_screen(sp) is not None

    def focus_page_at(self, sp) -> bool:
        """Make the page under ``sp`` current, WITHOUT moving the view.

        ``goto_page`` resets the scroll, which is right when a page fills the
        viewport and very wrong when the pages are a list the user is already
        scrolled into.
        """
        if self._canvas_style != "scroll_list":
            return False
        i = self.page_at_screen(sp)
        if i is None or i == self._current_page:
            return i is not None
        self._current_page = i
        # The list draws an accent border round the CURRENT sheet, so the
        # cache has to be rebuilt — without this the highlight stayed on the
        # page you left while your ink went to the one you touched.
        self._cache_dirty = True
        self.page_changed.emit(self._current_page, self.page_count)
        self.update()
        return True

    # ---- Coords ----
    def screen_to_scene(self, sp):
        # In list mode a screen point belongs to a PAGE, and the scene
        # coordinates it maps to are that page's own — which is exactly what
        # every tool downstream expects, because the page it lands on is the
        # current one (see focus_page_at).
        if self._canvas_style == "scroll_list":
            r = self.page_rect_on_screen(self._current_page)
            if r is not None and self._list_scale > 0:
                return QPointF((sp.x() - r.x()) / self._list_scale,
                               (sp.y() - r.y()) / self._list_scale)
        # _scroll_y shifts content UP, so add it back when mapping to scene space.
        return QPointF((sp.x()-self._view_offset.x())/ self.effective_scale(),
                       (sp.y()-self._view_offset.y()+self._scroll_y)/ self.effective_scale())
    def scene_to_screen(self, sp):
        if self._canvas_style == "scroll_list":
            r = self.page_rect_on_screen(self._current_page)
            if r is not None:
                return QPointF(sp.x() * self._list_scale + r.x(),
                               sp.y() * self._list_scale + r.y())
        return QPointF(sp.x()*self._view_scale+self._view_offset.x(),
                       sp.y()*self._view_scale+self._view_offset.y()-self._scroll_y)

    # ---- Scene ----
    def current_scene(self):
        if not (HAS_ENGINE and self._board is not None):
            return None
        if 0 <= self._current_page < self._board.page_count:
            return self._board.page_mut(self._current_page)
        return None
    def scene_at(self, i):
        if not (HAS_ENGINE and self._board is not None):
            return None
        if 0 <= i < self._board.page_count:
            return self._board.page_mut(i)
        return None
    def current_stack(self):
        if HAS_ENGINE and 0 <= self._current_page < len(self._command_stacks):
            return self._command_stacks[self._current_page]
        return None
    @property
    def current_page(self): return self._current_page
    @property
    def page_count(self):
        return self._board.page_count if (HAS_ENGINE and self._board) else 1
    @property
    def board(self): return self._board
    def set_input_handler(self, h): self._input_handler = h

    # ---- Pages ----
    def goto_page(self, i):
        # Jumping to a page and coasting somewhere else are two answers to
        # the same question, and the glide would win because it runs last.
        self.stop_fling()
        if HAS_ENGINE and 0 <= i < self.page_count:
            self._current_page = i; self._cache_dirty = True
            if self._canvas_style == "scroll_list":
                # Bring the page into view instead of jumping to the top of
                # the board: in a list the pages all exist at once, so "go to
                # page 4" means scroll to it, not reset.
                self.scroll_page_into_view(i)
            else:
                self._scroll_y = 0.0  # reset scroll when changing pages
            self.page_changed.emit(self._current_page, self.page_count); self.update()
    def scroll_page_into_view(self, i: int) -> None:
        """Centre page ``i`` in the viewport (clamped at the ends).

        CENTRED, not top-aligned: Next/Previous exist to take you to a page,
        and in a list "took you there" means the page is where you are
        looking. Parked at the top edge it competes with its neighbour for
        the middle of the screen, and the indicator — which reports the
        centre page — could still be naming the other one.
        """
        for idx, r in self.page_layout():
            if idx == i:
                target = r.top() + r.height() / 2.0 - self.height() / 2.0
                self._scroll_y = max(0.0, min(target, self._max_scroll()))
                self._cache_dirty = True
                self.update()
                return

    def next_page(self):
        if not HAS_ENGINE: return
        if self._current_page >= self.page_count - 1: self._add_page_internal()
        self.goto_page(self._current_page + 1)
    def prev_page(self):
        if self._current_page > 0: self.goto_page(self._current_page - 1)
    def _add_page_internal(self):
        if not HAS_ENGINE: return
        w, h = self._screen_size()
        ns = self._board.add_page()
        ns.set_canvas_size(w, h); self._apply_bg(ns)
        self._command_stacks.append(lbe.CommandStack(200))
        self._bump_layout()
        # Here rather than in add_page(): next_page() calls THIS directly when
        # it runs off the end, so a page added that way never repainted the
        # list and the new sheet simply wasn't there.
        self._cache_dirty = True
    def add_page(self):
        self._add_page_internal()
        # The list paints every page, so a new one has to invalidate the
        # cache or the sheet simply never appears. One-page mode never
        # needed this: the new page isn't on screen until you navigate to it.
        self._cache_dirty = True
        self.clamp_scroll()
        self.update()
        self.page_changed.emit(self._current_page, self.page_count)
    def move_page(self, src: int, dst: int) -> bool:
        """Move the page at ``src`` so it ends up at index ``dst``.

        Returns True if anything moved.

        WHAT HAS TO MOVE WITH IT
        ------------------------
        The page's UNDO STACK. ``_command_stacks`` is a list indexed by page
        number, so a page that changes index without its stack changing with
        it inherits the undo history of whatever page used to live there —
        and pressing undo then edits a page the user is not looking at.

        The engine has no move, only ``swap_pages``, so this walks the page
        one position at a time. That is O(distance) swaps, and the distance
        is however far a finger dragged, so it is small by construction —
        and every intermediate state is a valid board, which a bulk reorder
        would not guarantee.

        The background-fit map does not move: it is keyed by image source,
        not by page. Thumbnails ARE keyed by index, so every page between
        src and dst has to be re-rendered; that is the caller's job, since
        the panel owns the thumbnail manager.
        """
        if not HAS_ENGINE:
            return False
        n = self.page_count
        if not (0 <= src < n) or not (0 <= dst < n) or src == dst:
            return False
        step = 1 if dst > src else -1
        i = src
        while i != dst:
            j = i + step
            self._board.swap_pages(i, j)
            if (0 <= i < len(self._command_stacks)
                    and 0 <= j < len(self._command_stacks)):
                self._command_stacks[i], self._command_stacks[j] = (
                    self._command_stacks[j], self._command_stacks[i])
            i = j
        # The user is looking at the page they dragged, so the view follows
        # it rather than staying on an index that now holds something else.
        if self._current_page == src:
            self._current_page = dst
        elif src < self._current_page <= dst:
            self._current_page -= 1
        elif dst <= self._current_page < src:
            self._current_page += 1
        self._bump_layout()
        self._cache_dirty = True
        self.clamp_scroll()
        self.page_changed.emit(self._current_page, self.page_count)
        self.update()
        return True

    def insert_page_at(self, i: int) -> bool:
        """Insert a blank page so that it becomes page ``i``.

        ``i == page_count`` appends, which is what "insert after the last
        page" means.

        The new page is sized like the one it is being inserted next to
        rather than like the screen: a board built from a PDF is a stack of
        A4 sheets, and dropping a 16:9 page into the middle of it would look
        like a mistake.
        """
        if not HAS_ENGINE:
            return False
        n = self.page_count
        i = max(0, min(int(i), n))
        # Read the neighbour's SIZE now — do not carry a handle across the
        # insert.
        #
        # ``page_mut`` hands back a C++ reference into the Board's
        # ``std::deque<Scene>``. A deque promises stable addresses when you
        # push at an END, which is what its comment in Scene.h is about, but
        # ``add_page_at`` emplaces in the MIDDLE, and inserting mid-deque
        # invalidates every reference to every element. So a Scene& taken
        # before the insert is dangling by the time it is read afterwards:
        # sometimes it still gives the neighbour's numbers, sometimes the
        # numbers of whatever now occupies that storage. That is the whole
        # explanation for pages inserted with a size that matched nothing —
        # the size was not wrong, it was read from a reference that no
        # longer meant anything.
        nb_w = nb_h = 0
        if n:
            nb = self.scene_at(min(i, n - 1))
            if nb is not None:
                try:
                    nb_w = int(nb.canvas_width or 0)
                    nb_h = int(nb.canvas_height or 0)
                except Exception:
                    nb_w = nb_h = 0
        try:
            self._board.add_page_at(i)
        except Exception:
            logging.exception("add_page_at(%d) failed", i)
            return False
        if self.page_count <= n:
            return False
        try:
            import logicboard_engine as _lbe
            self._command_stacks.insert(i, _lbe.CommandStack(200))
        except Exception:
            logging.exception("could not give the new page an undo stack")
        # Match the neighbour's sheet size, from the numbers copied out
        # before the insert.
        try:
            fresh = self.scene_at(i)
            if fresh is not None:
                if nb_w > 0 and nb_h > 0:
                    fresh.set_canvas_size(nb_w, nb_h)
                self._apply_bg(fresh)
        except Exception:
            logging.exception("could not size the inserted page")
        if self._current_page >= i:
            self._current_page = min(self._current_page + 1,
                                     self.page_count - 1)
        self._bump_layout()
        self._cache_dirty = True
        self.clamp_scroll()
        self.page_changed.emit(self._current_page, self.page_count)
        self.update()
        return True

    def delete_page(self, i):
        if not HAS_ENGINE or self.page_count <= 1 or not (0 <= i < self.page_count): return
        self._board.remove_page(i)
        self._bump_layout()
        if 0 <= i < len(self._command_stacks):
            self._command_stacks.pop(i)
        if self._current_page >= self.page_count:
            self._current_page = self.page_count - 1
        self._cache_dirty = True
        # Deleting pages shortens the list under the current offset AND
        # changes which page sits in the middle of it.
        self.clamp_scroll()
        self.sync_current_page_to_center()
        self.page_changed.emit(self._current_page, self.page_count); self.update()

    # ---- Undo/Redo ----
    def undo(self):
        s, st = self.current_scene(), self.current_stack()
        if s and st and st.can_undo: st.undo(s); self.invalidate_cache()
    def redo(self):
        s, st = self.current_scene(), self.current_stack()
        if s and st and st.can_redo: st.redo(s); self.invalidate_cache()

    # ---- Cache ----
    def invalidate_cache(self):
        # Backgrounds, imports and page edits all come through here, and a
        # background is what decides a sheet's height — so the layout has to
        # go with the pixels.
        self._bump_layout()
        self._cache_dirty = True
        self.update()
        self.content_changed.emit()    # signal listeners (e.g. thumbnail manager)

    # ── Move overlay API ─────────────────────────────────────────────────
    # These three methods are the perf path for "drag selection around the
    # canvas". Without them, ``input_handler._handle_select_move`` would
    # invalidate the cache on every mouse move (forcing a full O(N) re-raster
    # of every visible object per pointer sample). With them, the cache is
    # rebuilt exactly TWICE per drag — once at begin (excluding the moving
    # ids), once at end (baking the new positions in).

    def begin_move_overlay(self, object_ids) -> None:
        """Start floating the given objects above the static scene cache.
        Forces ONE cache rebuild that excludes these objects so the
        underlying scene shows correctly with the dragged objects 'lifted
        off' the canvas. After this call, ``update_move_overlay(dx, dy)``
        is the only thing that needs to fire on each pointer sample.
        """
        ids = set(object_ids) if object_ids else set()
        if not ids:
            return
        self._move_overlay_ids = ids
        self._move_overlay_dx = 0.0
        self._move_overlay_dy = 0.0
        self._cache_dirty = True
        self.update()

    def update_move_overlay(self, dx: float, dy: float) -> None:
        """Set the live translation for the overlay. Called from every
        pointer-move event. O(1): just stores two floats and triggers a
        widget repaint. The static cache is NOT touched."""
        if not self._move_overlay_ids:
            return
        self._move_overlay_dx = dx
        self._move_overlay_dy = dy
        self.update()

    def end_move_overlay(self) -> None:
        """Stop floating. The caller (input_handler) is responsible for
        committing the final translation into the engine BEFORE calling
        this; we just clear overlay state and rebuild the cache so the
        objects are baked back in at their committed positions."""
        if not self._move_overlay_ids:
            return
        self._move_overlay_ids = set()
        self._move_overlay_dx = 0.0
        self._move_overlay_dy = 0.0
        self._cache_dirty = True
        self.update()

    def set_rotating(self, on: bool) -> None:
        """Enter or leave the "rotation in progress" state.

        Called from input_handler at rotate-start (True) and rotate-release
        (False). Two effects fire off this:
          * _paint_selection() reads self._rotating and skips drawing the
            selection rectangle + handles for the duration — since those
            are drawn from _selection_rect which is a static (pre-rotation)
            bbox, painting them mid-rotation shows a rectangle at the
            wrong angle.
          * rotating_changed(on) is emitted so main_window can hide the
            floating SelectionMenu widget too (that widget is not owned
            by the canvas and can't be reached from here directly).

        Idempotent — calling set_rotating(True) twice in a row is a no-op
        for the second call, so the signal never spuriously double-fires.
        """
        if self._rotating == on:
            return
        self._rotating = on
        # Force a repaint so the selection box shows/hides immediately;
        # otherwise the box would linger until the next scheduled update.
        self.update()
        self.rotating_changed.emit(on)

    # How a background image is laid onto the page.
    #
    #   "auto"    — the original behaviour, and still the default so existing
    #               boards look exactly as they did: a background taller than
    #               the canvas is fit to WIDTH and allowed to run off the
    #               bottom (scrollable), anything else is fit to COVER, which
    #               crops whatever doesn't fit.
    #   "fit"     — contain. Scale until the whole image is inside the canvas.
    #               Nothing cropped, nothing off-screen, aspect kept.
    #   "stretch" — fill the canvas exactly, aspect be damned.
    #
    # Keyed by the background's own relative PATH rather than by page number.
    # A page index would have to be kept in step with every add, delete and
    # reorder — and would still be wrong after an undo, because undoing a
    # background change puts the previous IMAGE back while the mode stayed
    # with the page. Keying by path means the mode follows the picture: undo
    # restores the image and its fit together, and a deleted page takes
    # nothing with it. The cost is that the same file used on two pages shares
    # one mode, which is the rarer thing to want.
    BG_FIT_MODES = ("auto", "fit", "stretch")

    def bg_fit_mode(self, key: str = None) -> str:
        """The fit mode for a background path (default: the current page's)."""
        if key is None:
            scene = self.current_scene()
            key = (scene.bg_image or "") if scene is not None else ""
        return self._bg_fit.get(self._bg_fit_key(key), "auto")

    def set_bg_fit_mode(self, mode: str, key: str = None) -> None:
        # Fit mode decides how far a background runs past the canvas, which
        # is exactly what sizes the sheet in list style.
        self._bump_layout()
        if mode not in self.BG_FIT_MODES:
            return
        if key is None:
            scene = self.current_scene()
            key = (scene.bg_image or "") if scene is not None else ""
        k = self._bg_fit_key(key)
        if not k:
            return
        # Only explicit choices are stored, so the map holds one entry per
        # background the user has actually decided about rather than one per
        # slide of every deck they ever imported.
        if mode == "auto":
            self._bg_fit.pop(k, None)
        else:
            self._bg_fit[k] = mode
        self._cache_dirty = True
        self.update()

    @staticmethod
    def _bg_fit_key(path: str) -> str:
        """Normalise a background path so the same file keys the same entry
        however the platform spelled it."""
        return (path or "").replace("\\", "/").strip("/").lower()

    def bg_fit_map(self) -> dict:
        """The whole map, for saving alongside the board."""
        return dict(self._bg_fit)

    def load_bg_fit_map(self, data) -> None:
        self._bg_fit = {}
        if not isinstance(data, dict):
            return
        for k, v in data.items():
            if isinstance(k, str) and v in self.BG_FIT_MODES and v != "auto":
                self._bg_fit[self._bg_fit_key(k)] = v

    def _image_size(self, src):
        """``(width, height)`` of a media image WITHOUT decoding it.

        THE 29-SECOND FREEZE.

        ``_page_size`` needs a page's background dimensions to know how tall
        the sheet is, and it used to get them by asking for the decoded
        pixmap. ``page_layout()`` calls ``_page_size`` for EVERY page, so
        laying out a board took one full image decode per page — and
        switching to list style on a 1000-page PDF sat there for 29 seconds
        doing exactly that, then thrashed the pixmap cache (971 evictions)
        so the paint that followed had to decode them all over again.

        A file header carries the dimensions. Measured on a 3840 px WEBP
        page: ``QImageReader.size()`` takes 1.05 ms against 95 ms to decode
        it — 91x cheaper, and it allocates nothing.

        Cached forever: a source's size cannot change without the file
        changing, and a tuple per page is nothing next to the pixmaps.
        SVGs and anything the reader cannot parse fall back to decoding,
        which is correct and no worse than before.
        """
        cached = self._image_size_cache.get(src)
        if cached is not None:
            return cached
        size = None
        try:
            path = self._resolve_media_path(src)
            if path:
                # The file's own header first. ``QImageReader.size()`` is
                # documented as reading only what it needs and for WEBP it
                # does not: measured at 9.19 ms a page against 0.088 ms for
                # a 32-byte read, on files that agreed exactly. Over a
                # thousand pages that is nine seconds against a tenth of one.
                size = image_size_from_header(path)
                if size is None:
                    reader = QImageReader(path)
                    reader.setAutoTransform(True)
                    sz = reader.size()
                    if sz.isValid() and sz.width() > 0 and sz.height() > 0:
                        size = (sz.width(), sz.height())
        except Exception:
            size = None
        if size is None:
            pm = self._get_cached_pixmap(src)
            size = ((pm.width(), pm.height())
                    if pm and not pm.isNull() else (0, 0))
        self._image_size_cache[src] = size
        return size

    def note_image_size(self, src, width, height) -> None:
        """Record a media source's pixel size without opening the file.

        The layout needs every page's background dimensions, and finding
        them out costs a file open — measured at 9 ms each on a cold cache,
        which is nine seconds of frozen UI for a thousand-page PDF and the
        reason switching to list style used to hang.

        The renderer already knew the answer. This is how it gets told.
        """
        try:
            w, h = int(width), int(height)
        except (TypeError, ValueError):
            return
        if src and w > 0 and h > 0:
            self._image_size_cache[src] = (w, h)

    def _bg_dest_rect(self, scene):
        """Where this page's background lands, in scene coords, or None.

        The GEOMETRY half of ``_bg_image_layout``, split out because most
        callers only want to know how much room the image takes — the page
        layout, the content height, the scroll extent — and decoding a
        picture to measure it is what made a long document unusable.
        """
        try:
            if not scene.has_bg_image:
                return None
        except Exception:
            return None
        pw, phh = self._image_size(scene.bg_image)
        if pw <= 0 or phh <= 0:
            return None
        cw = scene.canvas_width or self.width()
        ch = scene.canvas_height or self.height()

        mode = self.bg_fit_mode(scene.bg_image)
        if mode == "stretch":
            return QRectF(0.0, 0.0, float(cw), float(ch))
        if mode == "fit":
            # Contain: the limiting dimension decides, and the slack is split
            # evenly so the image sits centred.
            scale = min(cw / pw, ch / phh)
            dw, dh = pw * scale, phh * scale
            return QRectF((cw - dw) / 2.0, (ch - dh) / 2.0, dw, dh)

        img_aspect = phh / pw
        canvas_aspect = ch / cw
        if img_aspect > canvas_aspect:
            # Taller than canvas -> fit width, extend downward (scrollable).
            scale = cw / pw
            dw, dh = cw, phh * scale
            ox, oy = 0.0, 0.0
        else:
            # Cover the canvas (center-crop horizontally).
            scale = max(cw / pw, ch / phh)
            dw, dh = pw * scale, phh * scale
            ox, oy = (cw - dw) / 2.0, (ch - dh) / 2.0
        return QRectF(ox, oy, dw, dh)

    def _bg_image_layout(self, scene):
        """Return (pixmap, dest_rect QRectF in scene coords) for the page's
        background image, or (None, None).

        For DRAWING. Anything that only needs the rectangle should call
        ``_bg_dest_rect``, which does not decode anything.
        """
        dest = self._bg_dest_rect(scene)
        if dest is None:
            return None, None
        pm = self._get_cached_pixmap(scene.bg_image)
        if not pm or pm.isNull():
            return None, None
        return pm, dest

    def _render_bg_image(self, p, scene):
        """Draw the page background image (if any) behind everything. Read-only
        — drawing tools never touch it."""
        pm, dest = self._bg_image_layout(scene)
        if pm is None:
            return
        pw, phh = pm.width(), pm.height()
        p.drawPixmap(dest, pm, QRectF(0, 0, pw, phh))

    def _compute_content_height(self, scene):
        """Scrollable content height in scene coords: the max of the canvas
        height, the background image's rendered bottom, and the lowest object."""
        if scene is None:
            return float(self.height())
        ch = float(scene.canvas_height or self.height())
        bottom = ch
        # Background image extent — the rectangle only, no decode.
        dest = self._bg_dest_rect(scene)
        if dest is not None:
            bottom = max(bottom, dest.bottom())
        # Lowest object extent.
        try:
            for i in range(scene.object_count):
                o = scene.object_at(i)
                if not o or not o.visible:
                    continue
                bb = o.bounding_box()
                bottom = max(bottom, bb.max_pt.y)
        except Exception:
            pass
        return bottom

    def _max_scroll(self):
        """Maximum vertical scroll in screen pixels (0 if content fits)."""
        if self._canvas_style == "scroll_list":
            return max(0.0, self._list_content_height() - self.height())
        scene = self.current_scene()
        if scene is None:
            return 0.0
        content_h = self._compute_content_height(scene) * self._view_scale
        room = content_h - self.height()
        if room <= 0.0:
            # The page fits the viewport, which in one-page style is the
            # whole design — it is fitted to the window by definition. Adding
            # the inset here would make an ordinary page scroll by the height
            # of the toolbar, which reads as the board drifting when you
            # touch it. Only content that ALREADY overflows gets the extra
            # room to clear the toolbar.
            return 0.0
        return room + self._LIST_END_PAD + self._bottom_inset

    # ================================================================
    # Momentum
    # ================================================================

    def start_fling(self, vy: float = 0.0, vx: float = 0.0) -> None:
        """Carry on scrolling after the finger leaves, and coast to a stop.

        ``vy`` / ``vx`` are the SCROLL velocity at the moment of release, in
        pixels per millisecond and in the same sign as ``scroll_by`` — which
        is the opposite of the finger's own direction. Taking the scroll's
        velocity rather than the hand's is what keeps this honest at the ends
        of the board: if the drag was already clamped against the last page,
        the offset was not moving, so there is no velocity and no fling into
        a wall.

        Below ``_FLING_MIN_V`` nothing starts at all. A slow release is a
        person putting the board down where they want it, and gliding on
        past would take it away from them.
        """
        self.stop_fling()
        # Cap it. A flick with a wet finger on a big digitiser can report an
        # absurd velocity for one sample, and an uncapped fling turns that
        # into the board leaping the length of the document.
        vy = max(-self._FLING_MAX_V, min(self._FLING_MAX_V, float(vy)))
        vx = max(-self._FLING_MAX_V, min(self._FLING_MAX_V, float(vx)))
        if max(abs(vy), abs(vx)) < self._FLING_MIN_V:
            return
        self._fling_vy = vy
        self._fling_vx = vx
        self._fling_last_t = time.time()
        if self._fling_timer is None:
            self._fling_timer = QTimer(self)
            self._fling_timer.setTimerType(Qt.PreciseTimer)
            self._fling_timer.timeout.connect(self._fling_step)
        self._fling_timer.start(self._FLING_TICK_MS)

    def stop_fling(self) -> None:
        """Kill any glide in flight.

        Called the instant anything else touches the view — a new contact, a
        press, a wheel notch, a jump to another page. On a phone, putting a
        finger down on a coasting list stops it dead and that is the single
        most important part of the whole gesture: without it the board keeps
        moving under a pen that has already started drawing.
        """
        if self._fling_timer is not None and self._fling_timer.isActive():
            self._fling_timer.stop()
        self._fling_vy = 0.0
        self._fling_vx = 0.0

    def is_flinging(self) -> bool:
        return bool(self._fling_timer is not None
                    and self._fling_timer.isActive())

    def _fling_step(self) -> None:
        now = time.time()
        # Measured, not assumed. The timer asks for 16 ms and gets whatever
        # the board can spare — and on a page heavy enough to miss frames,
        # a fling that advanced by a fixed step per TICK would coast further
        # the slower it ran, which is the wrong way round.
        dt = max(1.0, min(64.0, (now - self._fling_last_t) * 1000.0))
        self._fling_last_t = now
        # Exponential decay, evaluated over the real elapsed time for the
        # same reason. Distance travelled from here is v/k, so the constant
        # is the one knob that decides how far a flick throws the board.
        damp = math.exp(-self._FLING_DECAY * dt)
        moved = False
        if abs(self._fling_vy) >= self._FLING_STOP_V:
            moved = self.scroll_by(self._fling_vy * dt) or moved
            self._fling_vy *= damp
        else:
            self._fling_vy = 0.0
        if abs(self._fling_vx) >= self._FLING_STOP_V:
            moved = self.scroll_by_x(self._fling_vx * dt) or moved
            self._fling_vx *= damp
        else:
            self._fling_vx = 0.0
        # ``moved`` is False once the offset stops changing, which happens
        # the moment the glide reaches the end of the board. Stopping on it
        # is what keeps a fast flick from spending the next second silently
        # decaying against a clamp with nothing on screen changing.
        if not moved or max(abs(self._fling_vy), abs(self._fling_vx)) < self._FLING_STOP_V:
            self.stop_fling()

    # ================================================================
    # The scroll bar on the right wall
    # ================================================================

    def _build_scrollbar(self) -> QScrollBar:
        """A vertical scroll bar living as a CHILD of the canvas.

        A child widget rather than something painted into ``paintEvent``: a
        real QScrollBar already knows how to be dragged, paged and flicked,
        and — the part that matters on a board — a press that lands on it
        goes to IT rather than to the canvas, so grabbing the bar can never
        leave a stray dot of ink behind. Hit-testing a hand-painted bar out
        of the canvas's own press path is how that bug gets written.
        """
        from ui.util.touch_scroll import canvas_scrollbar_qss
        bar = QScrollBar(Qt.Vertical, self)
        bar.setStyleSheet(canvas_scrollbar_qss())
        bar.setCursor(Qt.ArrowCursor)
        # Never take focus. The canvas needs it for keyboard tools, and a bar
        # that steals it on the way past is a tool that silently stops
        # responding to the keyboard.
        bar.setFocusPolicy(Qt.NoFocus)
        bar.valueChanged.connect(self._on_scrollbar_moved)
        bar.hide()
        return bar

    def _scrollbar_width(self) -> int:
        # Wider than a desktop scroll bar. This one is dragged with a finger
        # on a wall-mounted board, where the usual 10 px ribbon is a miss.
        return UiScale.px(16)

    def _position_scrollbar(self) -> None:
        """Park the bar against the right wall, full height."""
        bar = getattr(self, "_vbar", None)
        if bar is None:
            return
        w = self._scrollbar_width()
        bar.setGeometry(self.width() - w, 0, w, self.height())
        bar.raise_()

    def _sync_scrollbar(self) -> None:
        """Make the bar agree with ``_scroll_y``, and hide it when there is
        nothing to scroll.

        Called from the places that can CHANGE the scrollable extent — the
        scroll itself, the clamp, a zoom, a resize, a layout bump — and
        deliberately not from ``paintEvent``. In one-page style
        ``_max_scroll`` walks every object on the page to find the lowest
        one, so a per-frame sync would put an O(objects) loop in the middle
        of the drawing path, which is the one place this file works hardest
        to keep empty.
        """
        bar = getattr(self, "_vbar", None)
        if bar is None:
            return
        self._sb_stale = False
        self._position_scrollbar()
        max_s = int(round(self._max_scroll()))
        if max_s <= 0:
            # Nothing to scroll: no bar. A full-height track with a
            # full-height handle tells the user nothing and covers a strip of
            # board they could otherwise draw on.
            bar.hide()
            return
        self._sb_syncing = True
        try:
            bar.setRange(0, max_s)
            # A page of scrolling is a viewport-full, and a line is a
            # comfortable nudge — the same units the wheel path uses, so the
            # bar and the wheel move the board by the same amount.
            bar.setPageStep(max(1, self.height()))
            bar.setSingleStep(max(1, UiScale.px(60)))
            bar.setValue(int(round(self._scroll_y)))
        finally:
            self._sb_syncing = False
        if not bar.isVisible():
            bar.show()
            bar.raise_()

    def _on_scrollbar_moved(self, value: int) -> None:
        if self._sb_syncing:
            return
        # Grabbing the bar takes the view over, exactly as putting a finger
        # on a coasting list does.
        self.stop_fling()
        # scroll_by, not a bare assignment: it clamps, drops the scene cache,
        # re-syncs which page is current and repaints. Dragging the bar has to
        # go through the same door as every other way of scrolling or the
        # page indicator quietly stops matching the view.
        self.scroll_by(float(value) - self._scroll_y)

    def _max_scroll_x(self):
        """Maximum horizontal scroll in screen pixels (0 if content fits).

        Zero outside the list style on purpose — see ``_scroll_x``. One-page
        draws a page fitted to the viewport, so there is nothing off to the
        side to reach and a sideways gesture there must do nothing rather
        than slide the page out from under the user.
        """
        if self._canvas_style != "scroll_list":
            return 0.0
        return max(0.0, self._list_content_width() - self.width())

    def scroll_by(self, dy):
        """Scroll vertically by dy screen pixels (positive = content moves up)."""
        max_s = self._max_scroll()
        new_y = max(0.0, min(self._scroll_y + dy, max_s))
        if abs(new_y - self._scroll_y) > 0.01:
            self._scroll_y = new_y
            self._cache_dirty = True
            # The page indicator follows the view in list style.
            self.sync_current_page_to_center()
            self._sync_scrollbar()
            self.update()
            return True
        return False

    def scroll_by_x(self, dx):
        """Scroll horizontally by dx screen pixels (positive = content moves left).

        Deliberately NOT folded into ``scroll_by`` as a second argument: the
        vertical scroll has to re-sync the current page (which sheet is in
        the middle of the window) and the horizontal one never does — sliding
        sideways cannot change which page you are looking at. Two jobs, two
        functions, and no caller pays for the half it doesn't use.
        """
        max_s = self._max_scroll_x()
        new_x = max(0.0, min(self._scroll_x + dx, max_s))
        if abs(new_x - self._scroll_x) > 0.01:
            self._scroll_x = new_x
            self._cache_dirty = True
            self.update()
            return True
        return False

    def clamp_scroll(self, *, quiet: bool = False) -> None:
        """Pull the scroll offset back inside the content.

        ``quiet`` adjusts the offset without asking for a repaint — for the
        paint path, which is already drawing and must not schedule work from
        inside itself.

        Anything that changes how tall the content is — adding or deleting a
        page, zooming the list, resizing the window, switching style — can
        leave the view parked past the end, which renders as a blank board.
        Three separate bugs were the same missing call, so there is one
        function and every one of those paths uses it.
        """
        moved = False
        max_s = self._max_scroll()
        if self._scroll_y > max_s or self._scroll_y < 0.0:
            self._scroll_y = max(0.0, min(self._scroll_y, max_s))
            moved = True
        # The horizontal offset needs exactly the same guarantee, and for the
        # same reasons: zooming OUT, deleting the widest page or widening the
        # window all shrink the content under a live offset, and an
        # unclamped one parks the view in blank space beside the stack.
        max_x = self._max_scroll_x()
        if self._scroll_x > max_x or self._scroll_x < 0.0:
            self._scroll_x = max(0.0, min(self._scroll_x, max_x))
            moved = True
        self._sync_scrollbar()
        if moved and not quiet:
            self._cache_dirty = True
            self.update()

    def reset_scroll(self):
        self.stop_fling()
        if self._scroll_y != 0.0 or self._scroll_x != 0.0:
            self._scroll_y = 0.0
            self._scroll_x = 0.0
            self._cache_dirty = True
            self._sync_scrollbar()
            self.update()

    def _rebuild_list_cache(self, pix, dpr) -> None:
        """Paint every page of the board as a sheet, stacked vertically.

        Reads the SAME scenes ``current_scene`` returns — there is no second
        page model here, just a second arrangement of the one that exists.
        Each sheet is painted with the identical ``_render_object`` /
        ``_render_bg_image`` calls one-page mode uses, so media, PDF and PPT
        pages render exactly as they always have, only smaller.
        """
        t = theme_mgr.tokens
        # Belt and braces for every way the content height can change out
        # from under the offset. resizeEvent covers the common case, but it
        # is not delivered for a widget that has never been shown — so the
        # guarantee lives here, where the viewport size is whatever we are
        # actually about to paint into.
        self.clamp_scroll(quiet=True)
        pix.fill(theme_mgr.qcolor(t.canvas_desk))
        p = QPainter(pix)
        try:
            p.setRenderHint(QPainter.Antialiasing, True)
            p.setRenderHint(QPainter.SmoothPixmapTransform, True)
            view = QRectF(0, 0, self.width(), self.height())
            for i, r in self.page_layout():
                sheet = QRectF(r.x() - self._scroll_x, r.y() - self._scroll_y,
                               r.width(), r.height())
                # Cull off-screen pages. A 40-page board would otherwise
                # rasterise every object on every page for each frame, and
                # the whole point of the list is that it stays usable at 40
                # pages.
                if sheet.bottom() < view.top() - 2 or sheet.top() > view.bottom() + 2:
                    continue
                # Sideways culling too, now that a sheet can be scrolled off
                # the edge. Same reason as the vertical test: at a high zoom
                # on a wide board the sheets that are off to the side still
                # cost a full render each if nothing stops them.
                if sheet.right() < view.left() - 2 or sheet.left() > view.right() + 2:
                    continue
                scene = self.scene_at(i)
                if scene is None:
                    continue
                # The page number, printed in the gap ABOVE the sheet it
                # names. The gap is dead space that the layout has to leave
                # anyway, so the label costs no room — and putting it above
                # rather than below means it reads as a heading for the sheet
                # you are scrolling INTO, which is the one you are looking
                # for when you scroll.
                self._draw_page_label(p, i, sheet)
                bg = scene.background_color
                # The sheet itself, plus a hairline so a white page still
                # reads as a page against a light backdrop.
                p.setPen(QPen(theme_mgr.qcolor(t.stroke_default), 1))
                p.setBrush(QColor(bg.r, bg.g, bg.b, bg.a))
                p.drawRect(sheet)
                p.save()
                try:
                    p.setClipRect(sheet)
                    p.translate(sheet.x(), sheet.y())
                    p.scale(self._list_scale, self._list_scale)
                    self._render_bg_image(p, scene)
                    skip = self._move_overlay_ids if i == self._current_page else None
                    erasing = self._erase_cut_ids if i == self._current_page else None
                    for k in range(scene.object_count):
                        obj = scene.object_at(k)
                        if not obj or not obj.visible:
                            continue
                        if skip and obj.id in skip:
                            continue
                        if erasing and obj.id in erasing:
                            continue
                        self._render_object(p, obj)
                finally:
                    p.restore()
                # Mark which sheet input will land on. Without it the board
                # gives no clue where the next stroke goes.
                if i == self._current_page and self.page_count > 1:
                    p.setBrush(Qt.NoBrush)
                    p.setPen(QPen(theme_mgr.qcolor(t.accent), 2))
                    p.drawRect(sheet.adjusted(-1, -1, 1, 1))
        finally:
            p.end()
        self._scene_cache = pix
        self._cache_dirty = False
        self._cache_dpr = dpr

    def _draw_page_label(self, p, index: int, sheet: QRectF) -> None:
        """Print "Page N" in the gap above ``sheet``, aligned to its left edge.

        Left edge of the SHEET rather than of the window: the sheets are
        centred, so a window-aligned label would drift away from the page it
        belongs to at every zoom level.

        Except that it is CLAMPED into view. Scroll sideways on a sheet wider
        than the window and a strictly sheet-aligned label slides off the left
        edge — losing the page number at exactly the moment the user is
        navigating. Clamped, it rides along the top of its own gap and stays
        readable however far the view has moved.
        """
        t = theme_mgr.tokens
        f = QFont(p.font())
        f.setPixelSize(int(self._LIST_LABEL_PX))
        f.setWeight(QFont.DemiBold)
        p.save()
        try:
            p.setFont(f)
            # The current sheet's label picks up the accent, matching the
            # ring drawn round that sheet — one page highlighted in one
            # colour, rather than two unrelated cues.
            current = (index == self._current_page and self.page_count > 1)
            p.setPen(theme_mgr.qcolor(t.accent if current else t.text_secondary))
            # The band between the previous sheet and this one, inset by a
            # couple of pixels so the text is not welded to the paper edge.
            # Never left of the window, and never pushed past the sheet it
            # names — on a narrow sheet scrolled hard right, clamping only the
            # near edge would leave the label hanging over the page beside it.
            left = min(max(sheet.left(), self._LIST_MARGIN / 2.0),
                       max(sheet.right() - 120.0, 0.0))
            band = QRectF(left, sheet.top() - self._LIST_GAP,
                          max(sheet.width(), 120.0), self._LIST_GAP - 3.0)
            p.drawText(band, Qt.AlignLeft | Qt.AlignBottom,
                       f"Page {index + 1}")
        finally:
            p.restore()

    def _rebuild_cache(self):
        scene = self.current_scene(); w, h = self.width(), self.height()
        if w <= 0 or h <= 0: return
        # Allocate at PHYSICAL resolution and tag the pixmap with the screen's
        # device-pixel-ratio, exactly like _live_layer does. Without this the
        # cache is rasterised at logical size and the compositor upscales it to
        # the panel's real pixels — which is why committed strokes looked soft
        # while the in-progress preview (drawn into a DPR-aware layer) stayed
        # crisp. Setting the DPR also makes QPainter accept LOGICAL coordinates
        # below, so none of the drawing code needs to change.
        dpr = self.devicePixelRatioF()
        pix = QPixmap(max(1, int(round(w * dpr))), max(1, int(round(h * dpr))))
        pix.setDevicePixelRatio(dpr)
        if self._canvas_style == "scroll_list":
            self._rebuild_list_cache(pix, dpr)
            return
        if scene is None:
            pix.fill(theme_mgr.qcolor(theme_mgr.tokens.canvas_bg_color))
            self._scene_cache = pix; self._cache_dirty = False; self._cache_dpr = dpr; return
        bg = scene.background_color; pix.fill(QColor(bg.r, bg.g, bg.b, bg.a))
        p = QPainter(pix)
        try:
            p.setRenderHint(QPainter.Antialiasing, True)
            p.setRenderHint(QPainter.SmoothPixmapTransform, True)
            p.translate(self._view_offset.x(), self._view_offset.y() - self._scroll_y)
            p.scale(self._view_scale, self._view_scale)
            # Background image (behind all objects, clipped to the canvas).
            self._render_bg_image(p, scene)
            # Skip any objects that the user is currently dragging — they're
            # rendered as a floating overlay in paintEvent at their live
            # transform. This keeps the static cache valid across drag move
            # events so we don't need to re-rasterise the entire scene every
            # pointer sample. The set is small (usually 1–20 ids).
            #
            # Ink under an active eraser is excluded for the same reason and
            # painted live in paintEvent. Without this the cache was marked
            # dirty on EVERY pointer sample, so a 60-stroke board re-rasterised
            # all 60 strokes per move — measured 40ms/frame before the eraser
            # even did anything. Now only the handful of strokes the eraser is
            # actually touching are redrawn per move.
            skip = self._move_overlay_ids
            erasing = self._erase_cut_ids
            for i in range(scene.object_count):
                obj = scene.object_at(i)
                if obj and obj.visible:
                    if skip and obj.id in skip:
                        continue
                    if erasing and obj.id in erasing:
                        continue
                    self._render_object(p, obj)
        finally: p.end()
        self._scene_cache = pix; self._cache_dirty = False; self._cache_dpr = dpr

    def repair_cache_boxes(self, world_boxes) -> bool:
        """Redraw just these world-space boxes into the existing scene cache.

        Returns False when the cache can't be patched and the caller should
        fall back to a full rebuild.

        The LIST cache is one of those cases and returns False immediately.
        Its composition is many sheets at their own origins, so patching it
        with the one-page transform wrote the repaired ink at the wrong place
        on screen — visible as smears while erasing. Falling back to the full
        rebuild the contract already provides for is correct and costs a
        frame; getting it wrong costs the drawing.

        This exists because of what happens the moment the eraser reaches a
        stroke it hasn't touched yet. That stroke has to come OUT of the
        cache — from then on it is drawn live, with the subtraction applied —
        and you cannot remove something from a bitmap without repainting what
        was underneath it. The old answer was ``_cache_dirty = True``, i.e.
        re-rasterise the entire page.

        Once per drag that would be fine. It is not once per drag: a rub
        crosses new ink again and again, and each crossing cost a full
        rebuild. Measured on a 400-object board, one 220-sample rub triggered
        76 rebuilds costing 4.2 s — and on a board of page-wide strokes, 62
        rebuilds costing 10 s. Nothing else in the eraser came close; every
        boolean operation of the whole drag put together was a fraction of
        it.

        Only the vacated boxes actually changed, so only they need
        redrawing — along with whatever else overlaps them, since the cache
        is flat and a neighbouring stroke's pixels went with it.
        """
        if self._canvas_style == "scroll_list":
            return False
        scene = self.current_scene()
        if scene is None or self._cache_dirty or self._scene_cache is None:
            return False
        dpr = self.devicePixelRatioF()
        if (self._cache_dpr != dpr
                or self._scene_cache.size() / dpr != self.size()):
            return False
        boxes = [b for b in world_boxes if b is not None]
        if not boxes:
            return True

        from PySide6.QtGui import QRegion
        clip = QRegion()
        widget = self.rect().adjusted(-1, -1, 1, 1)
        for (x0, y0, x1, y1) in boxes:
            a = self.scene_to_screen(QPointF(x0, y0))
            b = self.scene_to_screen(QPointF(x1, y1))
            r = QRectF(min(a.x(), b.x()), min(a.y(), b.y()),
                       abs(b.x() - a.x()), abs(b.y() - a.y()))
            # Antialiased edges bleed a pixel past the geometry.
            r = r.adjusted(-2, -2, 2, 2).toAlignedRect().intersected(widget)
            if not r.isEmpty():
                clip = clip.united(QRegion(r))
        if clip.isEmpty():
            return True

        p = QPainter(self._scene_cache)
        try:
            p.setClipRegion(clip)
            p.setRenderHint(QPainter.Antialiasing, True)
            p.setRenderHint(QPainter.SmoothPixmapTransform, True)
            bg = scene.background_color
            p.fillRect(clip.boundingRect(), QColor(bg.r, bg.g, bg.b, bg.a))
            # From here on, identical to _rebuild_cache — same transform, same
            # exclusions — so a patched cache is indistinguishable from a
            # rebuilt one.
            p.translate(self._view_offset.x(), self._view_offset.y() - self._scroll_y)
            p.scale(self._view_scale, self._view_scale)
            self._render_bg_image(p, scene)
            skip = self._move_overlay_ids
            erasing = self._erase_cut_ids
            for i in range(scene.object_count):
                obj = scene.object_at(i)
                if obj is None or not obj.visible:
                    continue
                if skip and obj.id in skip:
                    continue
                if erasing and obj.id in erasing:
                    continue
                bb = obj.bounding_box()
                ox0, oy0 = bb.min_pt.x, bb.min_pt.y
                ox1, oy1 = bb.max_pt.x, bb.max_pt.y
                for (x0, y0, x1, y1) in boxes:
                    if not (ox1 < x0 or ox0 > x1 or oy1 < y0 or oy0 > y1):
                        self._render_object(p, obj)
                        break
        finally:
            p.end()
        return True

    # ---- Render ----
    def _qc(self, rgba): return QColor(rgba.r, rgba.g, rgba.b, rgba.a)

    def _eraser_mask_clip_region(self):
        """Return a cached QRegion that EXCLUDES the live pixel-eraser
        capsules. Used during a pixel-eraser drag so ink renders with the
        covered pixels visually cut out.

        PERF: ``QRegion`` clip is O(rectCount) regardless of how many
        capsules accumulated, while ``QPainterPath.subtracted`` grows
        quadratically. Benchmarked: at 300 capsules / 20 ink objects
        per paint, paths cost ~34ms (≈ 30fps cap), regions stay at ~5ms
        (≈ 200fps cap). The region is built incrementally by
        ``append_eraser_mask_segment`` — per-extend cost is O(1).

        Built LAZILY, and only extended when someone actually asks for it.
        The only callers are the shapes the eraser deletes wholesale
        (line/rect/ellipse/arrow/...), which most boards do not have anywhere
        near the eraser — and building it eagerly on every pointer sample,
        as the old code did, meant every drag paid for a QRegion boolean per
        move whether or not a single shape existed to be clipped.

        Returns ``None`` when no drag is active so per-object render is fast.
        """
        from PySide6.QtGui import QRegion, QPolygon
        from PySide6.QtCore import QPoint
        segs = self._eraser_mask_segments
        n = len(segs)
        if not n:
            self._eraser_clip_cache = None
            self._eraser_clip_len = 0
            return None
        if self._eraser_clip_len >= n and self._eraser_clip_cache is not None:
            return self._eraser_clip_cache

        for (ax, ay, bx, by, r) in segs[self._eraser_clip_len:]:
            # The capsule is TWO end-cap circles plus the rotated rectangle
            # that joins them — an honest capsule, not a bounding box. The
            # original code used ``QRegion(bbox of a,b padded by r)`` as the
            # body, with the comment that it "slightly under-covers the
            # diagonal middle". It doesn't under-cover, it massively
            # OVER-covers: for a slow or nearly-stationary drag the bbox
            # degenerates to a 2r x 2r SQUARE, so the eraser bit a hard-edged
            # rectangle out of the ink instead of a round hole.
            ir = int(r)
            dx, dy = bx - ax, by - ay
            length = math.hypot(dx, dy)
            if length < 1e-6:
                cap = QRegion(int(ax - r), int(ay - r), ir * 2, ir * 2,
                              QRegion.Ellipse)
            else:
                cap_a = QRegion(int(ax - r), int(ay - r), ir * 2, ir * 2,
                                QRegion.Ellipse)
                cap_b = QRegion(int(bx - r), int(by - r), ir * 2, ir * 2,
                                QRegion.Ellipse)
                px, py = -dy / length * r, dx / length * r
                body = QRegion(QPolygon([
                    QPoint(int(ax + px), int(ay + py)),
                    QPoint(int(bx + px), int(by + py)),
                    QPoint(int(bx - px), int(by - py)),
                    QPoint(int(ax - px), int(ay - py)),
                ]))
                cap = body.united(cap_a).united(cap_b)
            if self._eraser_clip_cache is None:
                # A generous world-space box that we keep subtracting from.
                full = QRegion(-100_000, -100_000, 200_000, 200_000)
                self._eraser_clip_cache = full.subtracted(cap)
            else:
                self._eraser_clip_cache = self._eraser_clip_cache.subtracted(cap)
        self._eraser_clip_len = n
        return self._eraser_clip_cache

    def append_eraser_mask_segment(self, ax: float, ay: float,
                                   bx: float, by: float, r: float) -> None:
        """Record ONE capsule of the live eraser mask.

        Deliberately does nothing but append. Everything derived from the
        mask — the clip region, each object's remainder — is now built on
        demand from this list, so the cost of a pointer sample does not
        depend on what happens to be on the board.
        """
        self._eraser_mask_segments.append((ax, ay, bx, by, r))

    def _render_object(self, p, obj):
        tn = obj.type_name
        # ---- Live pixel-eraser preview -----------------------------------
        # Ink under an active eraser is drawn as its SUBTRACTED OUTLINE, using
        # the very function the commit uses (ui.canvas.erase_geometry). That is
        # what makes preview and result identical: not two implementations
        # tuned to agree, but one computation called from both places.
        #
        # Imported media is excluded on purpose and must stay that way — the
        # pixel eraser is for ink only.
        if self._eraser_mask_segments and tn in eg.ERASABLE_TYPES:
            if self._paint_erased_preview(p, obj):
                return
        p.save()
        t = obj.transform
        if not t.is_identity():
            p.setTransform(QTransform(t.a(), t.c(), 0, t.b(), t.d(), 0, t.tx(), t.ty(), 1), True)
        p.setOpacity(obj.opacity)
        # Shapes the eraser deletes wholesale (line/rect/ellipse/arrow/...) keep
        # the old clip-region preview: they aren't subtracted, they vanish, and
        # the clip is only a hint that they're about to.
        is_ink = tn in ("line", "rect", "ellipse", "circle", "arrow", "triangle",
                        "shape")
        if is_ink and self._eraser_mask_segments:
            cr = self._eraser_mask_clip_region()
            if cr is not None:
                p.setClipRegion(cr)
        if tn == "freehand": self._r_freehand(p, obj)
        elif tn == "highlighter": self._r_highlighter(p, obj)
        elif tn == "line": self._r_line(p, obj)
        elif tn == "rect": self._r_rect(p, obj)
        elif tn == "ellipse": self._r_ellipse(p, obj)
        elif tn == "circle": self._r_circle(p, obj)
        elif tn == "arrow": self._r_arrow(p, obj)
        elif tn == "polygon": self._r_polygon(p, obj)
        elif tn == "triangle": self._r_triangle(p, obj)
        elif tn == "shape": self._r_shape(p, obj)
        elif tn == "image": self._r_image(p, obj)
        elif tn == "ppt": self._r_ppt(p, obj)
        p.restore()

    def _r_freehand(self, p, o):
        p.setPen(QPen(self._qc(o.stroke_color), o.stroke_width, Qt.SolidLine, Qt.RoundCap, Qt.RoundJoin))
        p.setBrush(Qt.NoBrush); pts = o.points()
        if not pts: return
        if len(pts) == 1:
            p.drawPoint(QPointF(pts[0][0], pts[0][1])); return
        # Same path builder as the live preview — single source of truth.
        # Anything that changes about smoothing must change in
        # ``_build_freehand_path`` and will automatically apply to both
        # call sites, eliminating any chance of live/final divergence.
        p.drawPath(self._build_freehand_path(pts))

    # ---- Live erase preview (shares erase_geometry with the commit) -------

    def _erase_overlay_ids(self):
        """Ids of ink whose BOUNDING BOX the eraser has crossed — candidates.

        Crossing a box is not the same as losing ink, and the two sets are
        kept apart on purpose: this one drives which objects are worth doing
        geometry for, while ``_erase_cut_ids`` — the objects that really lost
        ink — is what comes out of the scene cache. Recomputed only when the
        mask grows; the set only ever gains members during a drag, so it is
        stable enough to cache and cheap to extend.

        Also records ``_erase_fresh_hits``: the candidates the capsules added
        since the last call reach. Only those can possibly have been cut by
        them, which is what stops ``advance_erase`` from re-examining every
        candidate it has ever seen on every pointer sample.
        """
        if not self._eraser_mask_segments:
            self._erase_overlay_cache = (0, set())
            self._erase_bbox_snapshot = None
            self._erase_fresh_hits = set()
            return None
        n = len(self._eraser_mask_segments)
        cached_n, ids = getattr(self, "_erase_overlay_cache", (-1, set()))
        if cached_n == n:
            return ids
        scene = self.current_scene()
        if scene is None:
            return None
        # Snapshot every erasable object's world bbox ONCE per drag. This runs
        # on every pointer sample, and reaching across the binding for
        # object_at / type_name / bounding_box on a few hundred objects, a few
        # hundred times, is real time spent doing nothing — the geometry
        # cannot change mid-drag, because only the commit mutates the scene.
        snap = getattr(self, "_erase_bbox_snapshot", None)
        if snap is None:
            snap = []
            for i in range(scene.object_count):
                obj = scene.object_at(i)
                if obj is None or obj.type_name not in eg.ERASABLE_TYPES:
                    continue
                bb = obj.bounding_box()
                snap.append((obj.id, bb.min_pt.x, bb.min_pt.y,
                             bb.max_pt.x, bb.max_pt.y, i))
            self._erase_bbox_snapshot = snap
            self._erase_bbox_grid = self._build_erase_bbox_grid(snap)
        new = set(ids)
        touched = {}
        fresh = self._eraser_mask_segments[cached_n:] if cached_n >= 0 else \
            self._eraser_mask_segments
        hits = eg.segment_hits_box
        grid, oversized = self._erase_bbox_grid
        for seg in fresh:
            # Only the objects filed in the cells this capsule covers, plus
            # the handful too big to file usefully. OpenBoard gets this from
            # QGraphicsScene's BSP index (``items(eraserBoundingRect)``); we
            # have no scene graph, so the grid is the index. The linear scan
            # it replaces cost 0.54 ms per pointer sample on a 600-object
            # board — paid on every sample, for a question whose answer is
            # "no" for 599 of them.
            for entry in self._erase_grid_candidates(seg):
                oid = entry[0]
                if oid in touched:
                    continue
                if hits(seg, entry[1], entry[2], entry[3], entry[4]):
                    touched[oid] = entry
                    new.add(oid)
            for entry in oversized:
                oid = entry[0]
                if oid in touched:
                    continue
                if hits(seg, entry[1], entry[2], entry[3], entry[4]):
                    touched[oid] = entry
                    new.add(oid)
        self._erase_overlay_cache = (n, new)
        self._erase_fresh_hits = touched
        return new

    # Cells are much larger than an eraser (~50 px) so a capsule lands in one
    # or two of them, and large enough that ordinary ink files into one or
    # two as well.
    _ERASE_GRID_CELL = 256.0
    # Past this many cells an object is not worth filing — a page-spanning
    # scribble would otherwise be inserted hundreds of times and then found
    # again in every one of them.
    _ERASE_GRID_MAX_CELLS = 48

    def _build_erase_bbox_grid(self, snap):
        """``({(gx, gy): [entry, ...]}, [oversized entries])``."""
        cell = self._ERASE_GRID_CELL
        grid: dict = {}
        oversized: list = []
        for entry in snap:
            _oid, x0, y0, x1, y1, _idx = entry
            gx0 = int(math.floor(x0 / cell)); gx1 = int(math.floor(x1 / cell))
            gy0 = int(math.floor(y0 / cell)); gy1 = int(math.floor(y1 / cell))
            if (gx1 - gx0 + 1) * (gy1 - gy0 + 1) > self._ERASE_GRID_MAX_CELLS:
                oversized.append(entry)
                continue
            for gx in range(gx0, gx1 + 1):
                for gy in range(gy0, gy1 + 1):
                    grid.setdefault((gx, gy), []).append(entry)
        return grid, oversized

    def _erase_grid_candidates(self, seg):
        """Entries filed in any cell this capsule's padded box touches."""
        grid = self._erase_bbox_grid[0]
        if not grid:
            return ()
        ax, ay, bx, by, r = seg
        cell = self._ERASE_GRID_CELL
        gx0 = int(math.floor((min(ax, bx) - r) / cell))
        gx1 = int(math.floor((max(ax, bx) + r) / cell))
        gy0 = int(math.floor((min(ay, by) - r) / cell))
        gy1 = int(math.floor((max(ay, by) + r) / cell))
        if gx0 == gx1 and gy0 == gy1:
            return grid.get((gx0, gy0), ())      # the overwhelmingly common case
        seen: dict = {}
        for gx in range(gx0, gx1 + 1):
            for gy in range(gy0, gy1 + 1):
                for entry in grid.get((gx, gy), ()):
                    seen[entry[0]] = entry
        return seen.values()

    # How long the ring may sit still with nothing touching before the
    # watchdog parks it. Long enough that it can't fight a slow input
    # stream, short enough that a dropped release doesn't read as a freeze.
    _RING_IDLE_MS = 250

    def ring_radius_px(self) -> int:
        """The eraser ring's radius in SCREEN pixels.

        ``eraser_size`` is in SCENE units, so the ring has to be scaled the
        same way the ink it removes is. In list style at 0.3 the ring was
        drawn 60 px across while the erase footprint was 18 — the user saw a
        circle three times the size of the area it actually rubbed out. In
        one-page style the scale is 1.0, so this is exactly what it was.
        """
        return max(2, int(round(self.eraser_size / 2.0 * self.effective_scale())))

    def _ring_rect(self, pos, r=None) -> QRect:
        """Widget-space box the eraser ring paints into (+ pen width slack).

        ``r`` overrides the radius, for scrubbing a ring that was painted at
        a size the tool no longer has."""
        if r is None:
            r = self.ring_radius_px()
        r = int(r) + 4
        return QRect(pos.x() - r, pos.y() - r, r * 2, r * 2)

    def _stale_ring_rect(self) -> "QRect | None":
        """Box of the ring as it is ACTUALLY on screen right now, or None.

        ``_ring_last_painted`` — not ``_cursor_pos`` — is the honest answer
        to "where do I have to repaint to get rid of the ring", and the
        difference is the whole trail bug. ``_cursor_pos`` is only an
        intention, and it has several writers: the touch handler parks it,
        then a synthesised mouse move moves it somewhere else, and each one
        used its own idea of "the previous position" to scrub. Whenever
        those two disagreed, the pixels that were really lit got scrubbed by
        nobody and stayed on the canvas — an arc per sample, which is what a
        two-finger sweep leaves behind.
        """
        p = self._ring_last_painted
        if p is None or p.x() < 0:
            return None
        return self._ring_rect(p, self._ring_last_radius)

    def move_eraser_cursor(self, pos=None) -> None:
        """Move the eraser ring to ``pos`` (or park it off-screen with None),
        repainting the pixels it LEAVES as well as the ones it lands on.

        THE ONLY WRITER of ``_cursor_pos``. Every input path — mouse, tablet,
        touch, leave, the idle watchdog — comes through here, because the
        eraser-ring bugs were all some path that moved the ring by assignment
        and then repainted using its own guess at where the old one was:

          * Fast rub, ring "distorts" — a touch drag drives the ring from the
            touch stream while Windows separately replays the same contact as
            synthesised mouse moves, which lag it. Both were writing the
            position, so the ring alternated between the finger and a stale
            point a few samples behind it. At slow speed those coincide and it
            looks fine; at speed they're centimetres apart and the circle
            appears to tear in two. The mouse path now stands down entirely
            while touch owns the ring (see _ring_owned_by_touch).
          * Two fingers, an arc of the ring's colour left behind at every
            sample — the gesture path parks the ring, then the synthesised
            mouse move (never suppressed during a gesture, since
            ``_ignore_mouse_until`` was only armed on the drawing path)
            repainted it somewhere else using the PARKED position as "old".
            Nothing ever repainted where the pixels actually were.
          * Ring stuck where you lifted off — same shape of bug from the
            other end: after the contact is gone, a stray buttonless mouse
            move resurrects the ring at the last pointer position, and on a
            touch-only board nothing will ever move it again.
        """
        stale = self._stale_ring_rect()
        self._cursor_pos = QPoint(-100, -100) if pos is None else QPoint(pos)
        # Cheap out when there is no ring in play at all: this is called from
        # the shared touch handler, so it runs for finger-drawn pen strokes
        # too, and two spurious repaints per sample is exactly the kind of
        # cost the dirty-rect work elsewhere in this file exists to avoid.
        if stale is None and not (self._show_eraser_cursor or self._palm_erasing):
            return
        if stale is not None:
            self.update(stale)
        if ((self._show_eraser_cursor or self._palm_erasing)
                and self._cursor_pos.x() >= 0):
            self.update(self._ring_rect(self._cursor_pos))
            self._arm_ring_watchdog()

    def _ring_owned_by_touch(self) -> bool:
        """Is the touch handler currently driving the ring?

        True while a contact is on the glass, and for the tail window after
        it lifts during which Windows is still replaying that contact as
        synthesised mouse events. Those events carry a position that lags the
        touch stream, so anything they do to the ring is at best redundant
        and at worst the tearing described in ``move_eraser_cursor``.
        """
        h = self._input_handler
        if h is not None and getattr(h, "_active_touches", None):
            return True
        return time.time() < self._ignore_mouse_until

    def _arm_ring_watchdog(self) -> None:
        """(Re)start the idle timer that parks a ring nothing is holding up.

        Belt and braces for the one failure no event handler can see: an IR
        touch frame that drops a contact WITHOUT sending the matching release
        — lift a finger fast on a classroom board and it happens. No TouchEnd
        means nothing runs the parking code, so the ring stays lit at the
        lift-off point until the next tap repaints it; "the circle stays where
        I took my finger off".

        Only armed on hardware with no hovering pointer. A mouse or a pen in
        proximity keeps reporting position, so there the ring SHOULD follow
        the pointer and sit still when the pointer does.
        """
        if self._hover_seen:
            return
        self._ring_watchdog.start(self._RING_IDLE_MS)

    def _on_ring_idle(self) -> None:
        """Watchdog fired: park the ring unless something is still holding it."""
        if self._hover_seen:
            return
        if self._cursor_pos.x() < 0 and self._ring_last_painted is None:
            # Already parked and already repainted: there is nothing left to
            # watch, so let the timer stop. Checked BEFORE the contact test so
            # a ``_pointer_down`` that got stuck true can't keep it ticking
            # forever over a ring that isn't even on screen.
            return
        h = self._input_handler
        if self._pointer_down or (h is not None
                                  and getattr(h, "_active_touches", None)):
            # Still in contact — a finger resting still is not idle input.
            # Re-arm so a release lost after THIS point is still caught.
            self._ring_watchdog.start(self._RING_IDLE_MS)
            return
        self.move_eraser_cursor(None)

    def _repaint_eraser_ring(self, old_pos=None) -> None:
        """Repaint the ring where it IS and where it was last painted.

        Kept as the "something else changed, refresh the ring" entry point
        (a size change, a tool change). ``old_pos`` is accepted for callers
        that still pass one, but the authoritative old box comes from
        ``_stale_ring_rect`` — see the note there on why a caller-supplied
        previous position is not to be trusted.
        """
        rect = self._stale_ring_rect()
        if old_pos is not None and old_pos.x() >= 0:
            r = self._ring_rect(old_pos)
            rect = r if rect is None else rect.united(r)
        if ((self._show_eraser_cursor or self._palm_erasing)
                and self._cursor_pos.x() >= 0):
            r = self._ring_rect(self._cursor_pos)
            rect = r if rect is None else rect.united(r)
        if rect is not None:
            self.update(rect)

    def _paint_erase_overlay(self, p, dirty=None) -> None:
        """Draw the ink held out of the cache, with the eraser subtracted.

        ``dirty`` is paintEvent's update rect in WIDGET space. Everything the
        eraser has touched so far this drag lives in this overlay, so without
        that filter a long rub ends up re-drawing every stroke it has ever
        been near on every single frame — the repaint was already limited to a
        small rect around the eraser, and we were handing Qt a dozen complex
        paths to clip away. Skipping them outright turned out to be the single
        biggest remaining cost of a drag.
        """
        ids = self._erase_cut_ids
        if not ids:
            return
        scene = self.current_scene()
        if scene is None:
            return
        wr = None
        if dirty is not None and dirty.isValid():
            tl = self.screen_to_scene(QPointF(dirty.left(), dirty.top()))
            br = self.screen_to_scene(QPointF(dirty.right(), dirty.bottom()))
            wr = (min(tl.x(), br.x()), min(tl.y(), br.y()),
                  max(tl.x(), br.x()), max(tl.y(), br.y()))
        # Walk the CUT LIST, not the scene. It holds only the objects this
        # drag has actually taken ink off — kept in scene order, so the paint
        # order is unchanged — and each entry already carries the scene index
        # and world box, so the dirty-rect cull is a tuple compare rather than
        # a ``bounding_box()`` call across the binding. Walking
        # ``scene.object_count`` here meant a 600-object board paid 600
        # binding round-trips per frame to find the six strokes it wanted.
        for oid, x0, y0, x1, y1, idx in self._erase_cut_entries:
            if wr is not None and (x1 < wr[0] or x0 > wr[2]
                                   or y1 < wr[1] or y0 > wr[3]):
                continue
            obj = scene.object_at(idx)
            if obj is None or not obj.visible:
                continue
            if not self._paint_erased_preview(p, obj, wr):
                self._render_object(p, obj)

    def reset_erase_caches(self) -> None:
        """Drop every cached erase outline.

        Must run at the START of a drag and again after the COMMIT. The caches
        are keyed by object id, and an id can come back with different geometry
        — undo restores the original stroke under its old id — so a stale entry
        would paint the pre-undo shape. Cheap to rebuild; never worth risking.
        """
        self._erase_state.clear()
        self._erase_overlay_cache = (-1, set())
        self._erase_cut_ids = set()
        self._erase_cut_entries = []
        self._erase_fresh_hits = {}
        self._erase_bbox_snapshot = None
        self._erase_bbox_grid = ({}, [])
        self._eraser_clip_cache = None
        self._eraser_clip_len = 0

    def erase_region_for(self, obj):
        """The live ``eg.InkRegion`` for ``obj``, brought up to date.

        The region is kept per object and only ever moves FORWARD: each
        capsule the mask has gained since this object was last looked at is
        subtracted once, and then never again. Subtracting capsules one at a
        time is equivalent to subtracting their union — the remainder is
        always a subset of the base — so this gives the same answer as
        "union everything, subtract the lot" for a fraction of the work, and,
        crucially, for a cost that does not grow with the length of the drag.

        The region holds the ink as SMALL PIECES rather than as one path, so
        a capsule only ever re-does the boolean work for the two or three
        pieces it can reach. That is the OpenBoard shape of the problem and
        it is what keeps a worst-case pointer sample near 1.5 ms instead of
        5 ms; see the ``erase_geometry`` module docstring for the numbers.

        Capsules that cannot reach the object are skipped on a bounding-box
        test before any path is allocated, which is what stops a long snaking
        drag from dragging every stroke on the board into a boolean op.
        """
        key = str(obj.id)
        n = len(self._eraser_mask_segments)
        st = self._erase_state.get(key)
        if st is None:
            # Bind this object's nib angle into the chisel builder — see the
            # note in input_handler._runs_for. An upright outline for a
            # tilted brush stroke makes the eraser disagree with the renderer
            # about where the ink actually is.
            from ui.canvas.brush_ink import nib_angle_for
            _angle = nib_angle_for(obj)

            def _chisel(pts_, size_, *a, **k):
                k.setdefault("angle_deg", _angle)
                return self._chisel_path(pts_, size_, *a, **k)

            region = eg.ink_region_world(
                obj,
                to_world=lambda x, y, t: (t.a() * x + t.b() * y + t.tx(),
                                          t.c() * x + t.d() * y + t.ty()),
                transform_scale=self._eg_transform_scale,
                build_freehand_path=self._build_freehand_path,
                chisel_path=_chisel,
            )
            # [n_applied, region, untouched base, world bbox]. The base is
            # the reference the stroke re-expression is measured against;
            # ``region.hit`` answers "did this rub remove any ink, or did it
            # only cross the bounding box?" without anyone having to measure
            # two areas. The bbox is snapshotted because it cannot change
            # mid-drag — only the commit mutates the scene — and asking the
            # engine for it again is a binding call this runs once per object
            # per frame.
            base = region.composite() if region is not None else None
            bb = obj.bounding_box()
            st = [0, region, base,
                  (bb.min_pt.x, bb.min_pt.y, bb.max_pt.x, bb.max_pt.y)]
            self._erase_state[key] = st
        applied, region = st[0], st[1]
        if region is None or applied >= n or region.is_empty():
            st[0] = n
            return region
        st[0] = n
        x0, y0, x1, y1 = st[3]
        mask = self._eraser_mask_segments
        hits = eg.segment_hits_box
        for i in range(applied, n):
            seg = mask[i]
            if hits(seg, x0, y0, x1, y1):
                # Folded in one at a time on purpose. The old code unioned a
                # batch of pending capsules first, because back then the
                # operand on the other side was the whole stroke and worth
                # touching once — union the batch, subtract once. Now the
                # other operand is one small piece, so the union costs more
                # than the extra subtractions it saves, and it also throws
                # away the "capsule covers this piece outright" shortcut that
                # carries most of a rub.
                region.subtract_capsule(seg)
        return region

    def advance_erase(self):
        """Fold the newest capsule into every candidate, report what it CUT.

        Returns the world boxes of the objects whose ink this call removed
        for the first time — the ones that have to come out of the scene
        cache and start being painted live.

        Doing the geometry here rather than lazily inside the paint is what
        makes the distinction possible at all: "the eraser crossed this
        object's bounding box" is knowable without any geometry and is the
        wrong question, while "the eraser removed some of this object's ink"
        needs the subtraction to have been attempted. A candidate that turns
        out not to be cut costs a handful of box tests against its pieces and
        stays in the cache, where it belongs.
        """
        if not self._erase_overlay_ids():
            return ()
        scene = self.current_scene()
        if scene is None:
            return ()
        # Only the candidates the NEWEST capsules reach. Anything else cannot
        # have been cut since the last call, however near the eraser has been
        # to it before — walking every candidate instead put 12 ms a sample
        # into re-asking objects whose answer could not have changed.
        cut = self._erase_cut_ids
        cut_now = []
        for oid, entry in self._erase_fresh_hits.items():
            if oid in cut:
                continue
            obj = scene.object_at(entry[5])
            if obj is None:
                continue
            region = self.erase_region_for(obj)
            if region is not None and region.hit:
                cut.add(oid)
                self._erase_cut_entries.append(entry)
                cut_now.append((entry[1], entry[2], entry[3], entry[4]))
        if cut_now:
            # Keep scene order, so the overlay paints in the same order the
            # cache would have.
            self._erase_cut_entries.sort(key=lambda e: e[5])
        return cut_now

    def erased_pair_for(self, obj):
        """``(untouched outline, remainder, ink_was_hit)`` for ``obj``.

        The commit needs all three: the remainder is what to store, the base
        is the reference the stroke re-expression has to match, and the flag
        says whether the rub touched real ink or merely crossed the bounding
        box.

        Unlike the preview's path this one is ``simplified()`` — the commit
        measures its area and converts it to polygons, and both of those
        would count the overlaps between pieces twice. It is also the reason
        the simplify happens HERE and only when ink was actually hit: it is
        the one genuinely expensive step left (2–7 ms on a heavily chewed
        stroke), it runs once per touched object at pen-up, and objects the
        eraser merely passed near must not pay it.
        """
        region = self.erase_region_for(obj)
        st = self._erase_state.get(str(obj.id))
        if region is None or not st:
            return None, None, False
        if not region.hit:
            return st[2], None, False
        return st[2], region.composite().simplified(), True

    def _eraser_reaches(self, obj) -> bool:
        """Has the eraser actually taken ink off ``obj`` this drag?

        Answered from the incrementally-maintained cut set rather than by
        walking the mask. Walking it looked cheap — one bounding-box compare
        per capsule — but it ran per object per frame against a list that
        grows all drag long, so the total was quadratic in drag length all
        over again: profiling a 180-sample rub over 14 strokes caught it doing
        77,000 box tests and burning 30% of the frame budget on them.
        ``advance_erase`` maintains this set as the drag goes.
        """
        ids = self._erase_cut_ids
        return bool(ids) and obj.id in ids

    @staticmethod
    def _eg_transform_scale(t):
        if t.is_identity():
            return 1.0
        import math as _m
        sx = _m.hypot(t.a(), t.c()); sy = _m.hypot(t.b(), t.d())
        return (sx + sy) * 0.5 if (sx or sy) else 1.0

    def _paint_erased_preview(self, p, obj, clip=None) -> bool:
        """Draw ``obj`` with the eraser subtracted. True if it handled the draw.

        The result is FILLED, not stroked: after subtraction the survivor is a
        region, not a centreline, so there is no pen to run along it.

        ``clip`` is paintEvent's dirty rect in WORLD space, when there is one.
        It only ever removes pieces of the object that fall entirely outside
        the rectangle, so it changes what Qt is asked to rasterise and not
        what ends up on screen.
        """
        # Reject against the individual CAPSULES, not the mask's overall
        # bounding box. A long snaking drag has a bbox covering most of the
        # board while the capsules themselves touch only a few strokes — the
        # bbox test dragged every stroke on the board into a boolean
        # subtraction every frame, which is what made erasing heavy on a busy
        # page (measured 73ms/frame with 60 strokes).
        if not self._eraser_mask_segments:
            return False
        if not self._eraser_reaches(obj):
            return False          # nowhere near — render normally
        region = self.erase_region_for(obj)
        if region is None:
            return False
        path = region.composite(clip)
        p.save()
        try:
            p.setOpacity(obj.opacity)
            p.setPen(Qt.NoPen)
            col = self._qc(obj.fill_color if obj.type_name == "polygon"
                           else obj.stroke_color)
            p.setBrush(QBrush(col))
            if not path.isEmpty():
                p.drawPath(path)
        finally:
            p.restore()
        return True


    # ---- chisel ribbon (highlighter + brush) --------------------------
    #
    # THIN -> SMOOTH -> DENSIFY -> SWEEP.
    #
    # The middle two steps are why these strokes stopped looking blocky. The
    # ribbon used to be swept along the raw samples straight after thinning,
    # so every polygon edge was a chord of the curve the user actually drew,
    # and a slow deliberate arc came out as a chain of flat facets. Most
    # visible on the brush, which is used small, but the highlighter had it
    # too. The pen never did, because it has always drawn through
    # midpoint-quadratic smoothing.
    #
    # So the centreline now goes through the SAME smoothing the pen uses and
    # is re-sampled along those curves before the nib is swept along it.
    # Densifying is adaptive: a straight run stays as it was, a tight corner
    # gets as many points as it needs. That keeps the polygon count near the
    # old one on strokes that were already fine and spends it only where the
    # faceting was visible.

    @staticmethod
    def _thinned_centreline(pts, s: int, end: int, tol: float):
        """Raw samples with the near-collinear ones dropped.

        A 200 Hz stylus emits hundreds of samples along a straight run and
        every one of them would otherwise cost polygons. Measured on a
        600-sample stroke before this existed: 46.9 ms per frame, against
        2.8 ms for the round pen it replaced — and a pixel-eraser drag
        rebuilds the scene cache every frame, so that lands directly on erase
        latency.
        """
        def raw(i):
            q = pts[i]
            try:
                return q.x(), q.y()
            except AttributeError:      # committed strokes hand back tuples
                return q[0], q[1]

        keep = [raw(s)]
        for i in range(s + 1, end - 1):
            ax, ay = keep[-1]
            bx, by = raw(i)
            cx, cy = raw(i + 1)
            ux, uy = cx - ax, cy - ay
            seg = math.hypot(ux, uy)
            if seg < 1e-6:
                continue
            # Perpendicular distance of b from the line a->c.
            if abs((bx - ax) * uy - (by - ay) * ux) / seg > tol:
                keep.append((bx, by))
        if end - 1 > s:
            keep.append(raw(end - 1))
        return keep

    @staticmethod
    def _smooth_dense(keep, flat_tol: float = 0.35, max_split: int = 24,
                      with_marks: bool = False):
        """Points along the midpoint-quadratic curve through ``keep``.

        The same curve ``_build_freehand_tail_path`` draws for the pen: the
        chain runs midpoint -> midpoint with each original sample as the
        control point, so the ink passes BETWEEN the samples rather than
        through them and inherits their smoothing for free. Sweeping the nib
        along these points instead of along the samples is the whole fix for
        the faceting.

        Each quad is split only as far as its own bend needs — the split
        count comes from how far the control point sits off the chord — so a
        straight stroke costs no more than it did before.
        """
        n = len(keep)
        if n < 3:
            return (list(keep), [0] * n) if with_marks else list(keep)

        def mid(a, b):
            return ((a[0] + b[0]) * 0.5, (a[1] + b[1]) * 0.5)

        out = [keep[0]]
        # marks[i] is where the output for input span i starts. The live
        # pipeline needs it to know which part of the centreline is settled:
        # a span's output depends on keep[i-1..i+1], so the ONLY span that
        # can still be revised as the stroke grows is the last one, which
        # reaches the moving tip. Everything before marks[-1] is final.
        marks = [0] * n
        for i in range(1, n - 1):
            marks[i] = len(out)
            p0 = out[-1]
            c = keep[i]
            p2 = mid(keep[i], keep[i + 1])
            # Sagitta of the quad: how far the curve departs from its chord.
            # Zero for a control point on the line, so a straight run adds
            # one point per sample and no subdivision at all.
            mx, my = mid(p0, p2)
            bend = math.hypot(c[0] - mx, c[1] - my) * 0.5
            if bend <= flat_tol:
                k = 1
            else:
                k = min(max_split, int(math.sqrt(bend / flat_tol)) + 1)
            for j in range(1, k + 1):
                t = j / k
                it = 1.0 - t
                out.append((it * it * p0[0] + 2 * it * t * c[0] + t * t * p2[0],
                            it * it * p0[1] + 2 * it * t * c[1] + t * t * p2[1]))
        marks[n - 1] = len(out)
        out.append(keep[-1])
        return (out, marks) if with_marks else out

    @staticmethod
    def _chisel_path(pts, height: float, s: int = 0,
                     include_tip: bool = True,
                     angle_deg: float = 0.0) -> "QPainterPath":
        """Filled ribbon swept by a CHISEL nib — a thin bar.

        This is what makes a highlighter look like a highlighter: wide on
        horizontal strokes, thin on vertical ones, exactly like the real
        marker. A uniform round-capped line cannot express that; it just
        looks like a fat translucent pen.

        ``angle_deg`` tilts that bar. Zero is upright, which is the
        highlighter. The brush pen passes a real angle, and that is what
        gives it the swelling-and-thinning calligraphic look: a nib held at
        40 degrees sweeps a wide ribbon going one way across the page and a
        narrow one going the other, changing continuously through a curve.

        Each pair of consecutive points contributes ONE convex piece, and
        they all go into ONE path with ``WindingFill``. That is the bit an
        early chisel implementation got wrong: it offset every point into a
        single polygon outline, which self-intersects on direction changes
        and left the holes and gaps that got it replaced by a round pen.
        Overlapping convex pieces under a winding fill merge into one region
        — no gaps, and no double-darkening either, because it is a single
        fill operation.

        ``s`` / ``include_tip`` mirror ``_build_freehand_tail_path`` so the
        live pipeline's batched baking works unchanged.
        """
        n = len(pts)
        end = n if include_tip else n - 1
        if end - s < 1:
            return QPainterPath()

        # Thinning tolerance scales with the nib: a wide highlighter band
        # hides a dropped sample that a small brush nib would show.
        tol = max(0.25, min(0.75, height * 0.02))
        keep = CanvasWidget._smooth_dense(
            CanvasWidget._thinned_centreline(pts, s, end, tol))
        return CanvasWidget._chisel_ribbon(keep, height, angle_deg)

    @staticmethod
    def _chisel_ribbon(keep, height: float,
                       angle_deg: float = 0.0) -> "QPainterPath":
        """The nib sweep itself, along an ALREADY-dense centreline.

        Split out of ``_chisel_path`` so the live pipeline can hand in a
        centreline it computed once for the whole stroke. That is not a
        convenience — it is the fix for a real defect. While you draw, the
        ink is two pieces: a baked layer for the settled part and a tail
        redrawn each frame. Both used to call ``_chisel_path`` with their
        own start index, and ``_thinned_centreline`` is NOT a pure filter —
        it chains each keep decision off its own last kept point, so
        thinning from sample 0 and from sample 40 disagree about the stretch
        they share. Measured at a bake boundary: 22 pixels of ink missing,
        23 more with a doubled antialiased edge, worst case 112/255 out. A
        step in the stroke, once every _BAKE_BATCH samples, for the whole
        time you were writing — and gone the instant you lifted, because
        committing redraws the stroke as one path. That is what "the brush
        isn't smooth" was.

        Widening _TAIL_OVERLAP cannot fix it: measured, a bigger overlap
        trades missing ink for doubled ink (2044 -> 147 missing, but
        2013 -> 21514 doubled) because the two ribbons differ across the
        WHOLE overlap, not just at its ends.
        """
        path = QPainterPath()
        path.setFillRule(Qt.WindingFill)
        if not keep:
            return path

        from ui.canvas.brush_ink import nib_dimensions
        hh, half_nib = nib_dimensions(height)

        a = math.radians(angle_deg)
        ux, uy = math.sin(a), -math.cos(a)
        px, py = math.cos(a), math.sin(a)

        def nib_at(i):
            x, y = keep[i]
            return [
                (x + hh * ux + half_nib * px, y + hh * uy + half_nib * py),
                (x + hh * ux - half_nib * px, y + hh * uy - half_nib * py),
                (x - hh * ux - half_nib * px, y - hh * uy - half_nib * py),
                (x - hh * ux + half_nib * px, y - hh * uy + half_nib * py),
            ]

        def hull(points):
            """Convex hull, monotone chain. Never more than eight points in,
            so the sort costs nothing worth measuring."""
            ps = sorted(set(points))
            if len(ps) < 3:
                return ps

            def cross(o, p1, p2):
                return ((p1[0] - o[0]) * (p2[1] - o[1])
                        - (p1[1] - o[1]) * (p2[0] - o[0]))

            lower = []
            for q in ps:
                while len(lower) >= 2 and cross(lower[-2], lower[-1], q) <= 0:
                    lower.pop()
                lower.append(q)
            upper = []
            for q in reversed(ps):
                while len(upper) >= 2 and cross(upper[-2], upper[-1], q) <= 0:
                    upper.pop()
                upper.append(q)
            return lower[:-1] + upper[:-1]

        def add(corners):
            """One convex piece, wound consistently.

            Orientation matters, and getting it wrong is not a subtle defect.
            Under ``WindingFill`` the winding numbers of overlapping subpaths
            ADD — but a piece wound the other way contributes -1, so an
            overlap sums to zero and punches a hole. With per-segment pieces
            that shows up as hairline stripes through every curve.
            Normalising by signed area makes every piece wind the same way,
            so overlaps only ever reinforce.
            """
            if len(corners) < 3:
                return
            area = 0.0
            for j in range(len(corners)):
                x1, y1 = corners[j]
                x2, y2 = corners[(j + 1) % len(corners)]
                area += x1 * y2 - x2 * y1
            if area < 0:
                corners = corners[::-1]
            path.addPolygon(QPolygonF([QPointF(x, y) for x, y in corners]))

        if len(keep) == 1:
            add(nib_at(0))
            return path

        # Sweeping a convex shape along a straight segment covers exactly the
        # convex hull of the shape at both ends — its Minkowski sum with the
        # segment. So one hull per step covers the nib at both ends and
        # everything between, which fills the wedge at a direction change for
        # free; the earlier builder needed extra corner stamps for that.
        for i in range(len(keep) - 1):
            add(hull(nib_at(i) + nib_at(i + 1)))
        return path

    def _r_highlighter(self, p, o):
        # ROOT-CAUSE FIX: render as a THICK ROUND-CAPPED LINE, not a vertical
        # band polygon. The old band offset each point by ±hh in Y only, so it
        # behaved like a chisel tip — wide for horizontal strokes but collapsing
        # to near-zero width on vertical/fast-diagonal strokes (those were the
        # "holes/gaps"). A stroked polyline has uniform width in EVERY direction,
        # no self-intersection holes, and a smooth continuous look. The outer
        # _render_object() already applied p.setOpacity(obj.opacity); we re-set
        # the same value defensively (no-op) so direct callers are safe too.
        sc = o.stroke_color
        color = QColor(sc.r, sc.g, sc.b, 255)
        pts = o.points(); h = float(o.height)
        if not pts: return
        # Cache the ribbon per object. A pixel-eraser drag marks the scene
        # cache dirty on every move, so without this the path is rebuilt
        # from scratch for every highlighter on screen, every frame.
        # Committed strokes are immutable — erasing SPLITS them into new
        # objects with new ids — so (id, point count, height) is a sound key.
        # str() key on purpose — engine UUID wrappers are fresh Python
        # objects per access, so identity-based hashing would miss forever.
        # A brush stroke is a highlighter stroke committed opaque; the tilt
        # follows from that, so the same renderer covers both. See
        # ui/canvas/brush_ink.py for why the two share a stored type.
        from ui.canvas.brush_ink import nib_angle_for
        angle = nib_angle_for(o)
        key = str(o.id)
        ent = self._hl_path_cache.get(key)
        # The angle is part of the key: an object is either a brush or a
        # highlighter for life, but a cache that ignored it would hand a
        # tilted ribbon to an upright stroke after an id collision across a
        # clear-and-reload.
        if ent is None or ent[0] != len(pts) or ent[1] != h or ent[3] != angle:
            path = self._chisel_path(pts, h, angle_deg=angle)
            if len(self._hl_path_cache) > 512:      # bound the memory
                self._hl_path_cache.clear()
            self._hl_path_cache[key] = (len(pts), h, path, angle)
        else:
            path = ent[2]
        prev_op = p.opacity()
        p.setOpacity(o.opacity)
        p.setPen(Qt.NoPen)
        p.setBrush(QBrush(color))
        p.drawPath(path)
        p.setOpacity(prev_op)


    def _r_line(self, p, o):
        p.setPen(QPen(self._qc(o.stroke_color), o.stroke_width, Qt.SolidLine, Qt.RoundCap, Qt.RoundJoin))
        s,e=o.start,o.end; p.drawLine(QPointF(s.x,s.y), QPointF(e.x,e.y))

    def _r_rect(self, p, o):
        fc,sc=o.fill_color,o.stroke_color
        p.setBrush(QBrush(self._qc(fc)) if fc.a>0 else Qt.NoBrush)
        p.setPen(QPen(self._qc(sc), o.stroke_width) if sc.a>0 and o.stroke_width>0 else Qt.NoPen)
        og,sz=o.origin,o.size; cr=o.corner_radius; rect=QRectF(og.x,og.y,sz.x,sz.y)
        if cr>0: p.drawRoundedRect(rect,cr,cr)
        else: p.drawRect(rect)

    def _r_ellipse(self, p, o):
        fc,sc=o.fill_color,o.stroke_color
        p.setBrush(QBrush(self._qc(fc)) if fc.a>0 else Qt.NoBrush)
        p.setPen(QPen(self._qc(sc), o.stroke_width) if sc.a>0 and o.stroke_width>0 else Qt.NoPen)
        c,r=o.center,o.radii; p.drawEllipse(QPointF(c.x,c.y), r.x, r.y)

    def _r_circle(self, p, o):
        fc,sc=o.fill_color,o.stroke_color
        p.setBrush(QBrush(self._qc(fc)) if fc.a>0 else Qt.NoBrush)
        p.setPen(QPen(self._qc(sc), o.stroke_width) if sc.a>0 and o.stroke_width>0 else Qt.NoPen)
        c,r=o.center,o.radius; p.drawEllipse(QPointF(c.x,c.y), r, r)

    def _r_arrow(self, p, o):
        color=self._qc(o.stroke_color); sw=o.stroke_width
        s,e=o.start,o.end
        # Draw shaft stopping at arrowhead base
        if o.head_at_end:
            ang = math.atan2(e.y-s.y, e.x-s.x); hl = o.head_length
            shaft_ex = e.x - hl*math.cos(ang); shaft_ey = e.y - hl*math.sin(ang)
            p.setPen(QPen(color, sw, Qt.SolidLine, Qt.RoundCap, Qt.RoundJoin))
            p.setBrush(Qt.NoBrush)
            p.drawLine(QPointF(s.x,s.y), QPointF(shaft_ex, shaft_ey))
        else:
            p.setPen(QPen(color, sw, Qt.SolidLine, Qt.RoundCap, Qt.RoundJoin))
            p.setBrush(Qt.NoBrush)
            p.drawLine(QPointF(s.x,s.y), QPointF(e.x,e.y))
        p.setPen(Qt.NoPen); p.setBrush(QBrush(color))
        def tri(t):
            pa=QPainterPath(); pa.moveTo(t[0][0],t[0][1])
            pa.lineTo(t[1][0],t[1][1]); pa.lineTo(t[2][0],t[2][1])
            pa.closeSubpath(); p.drawPath(pa)
        if o.head_at_end: tri(o.end_head_triangle())
        if o.head_at_start: tri(o.start_head_triangle())

    def _r_polygon(self, p, o):
        fc,sc=o.fill_color,o.stroke_color
        p.setBrush(QBrush(self._qc(fc)) if fc.a>0 else Qt.NoBrush)
        p.setPen(QPen(self._qc(sc), o.stroke_width, Qt.SolidLine, Qt.RoundCap, Qt.RoundJoin) if sc.a>0 and o.stroke_width>0 else Qt.NoPen)
        verts=o.vertices()
        if len(verts)<2: return
        path=QPainterPath(); path.moveTo(verts[0][0],verts[0][1])
        for i in range(1,len(verts)): path.lineTo(verts[i][0],verts[i][1])
        if o.closed: path.closeSubpath()
        p.drawPath(path)

    def _r_triangle(self, p, o):
        fc, sc = o.fill_color, o.stroke_color
        p.setBrush(QBrush(self._qc(fc)) if fc.a > 0 else Qt.NoBrush)
        p.setPen(QPen(self._qc(sc), o.stroke_width, Qt.SolidLine, Qt.RoundCap, Qt.RoundJoin)
                 if sc.a > 0 and o.stroke_width > 0 else Qt.NoPen)
        a, b, c = o.p1, o.p2, o.p3
        path = QPainterPath()
        path.moveTo(a.x, a.y)
        path.lineTo(b.x, b.y)
        path.lineTo(c.x, c.y)
        path.closeSubpath()
        p.drawPath(path)

    def _r_shape(self, p, o):
        """Every shape kind beyond the original six — drawn by the same
        routine that draws its preview and its icon (shape_geometry)."""
        if o.stroke_width <= 0 or o.stroke_color.a == 0:
            return
        s, e = o.start, o.end
        sg.paint(p, o.kind, (s.x, s.y), (e.x, e.y),
                 self._qc(o.stroke_color), o.stroke_width)

    def _resolve_media_path(self, src):
        """Resolve a board-relative src ("imports/...") to an absolute path."""
        if not src:
            return None
        board_dir = getattr(self, "_board_dir", None)
        if board_dir:
            from pathlib import Path as _P
            cand = _P(board_dir) / src
            if cand.exists():
                return str(cand)
        from pathlib import Path as _P
        cand = _P(src)
        return str(cand) if cand.exists() else None

    def clear_media_caches(self) -> None:
        """Drop every decoded background AND the sizes read from them.

        Called when a different board is opened. The size cache is keyed by
        the same media-relative path as the pixmap cache, and two boards can
        use the same relative name for different files.
        """
        try:
            self._pixmap_cache.clear()
        except Exception:
            pass
        self._image_size_cache.clear()

    def _get_cached_pixmap(self, src):
        """Load + cache a QPixmap for an image/SVG src (keyed by path).

        SVG sources are rasterised once at the natural size of the underlying
        document, then cached as a QPixmap — so subsequent draws are as cheap
        as raster, and we still get sharp scaling for the first render at the
        intended size."""
        # BOUNDED. This used to be a plain dict that nothing removed
        # anything from, so every image, rendered PDF page and PowerPoint
        # slide the board had ever drawn stayed decoded for the whole
        # session. Measured at 8.3 MB per A4 page rendered at 150 dpi, with
        # the process's working set tracking it almost exactly: 75 MB with
        # a board open, 275 MB after twenty-four pages. PDFs import at up
        # to 3840 px wide, where one A4 page decodes to about 83 MB, so a
        # scanned chapter of thirty pages was 2.5 GB in this dict alone.
        if not isinstance(getattr(self, "_pixmap_cache", None), PixmapCache):
            self._pixmap_cache = PixmapCache()
        if src in self._pixmap_cache:
            return self._pixmap_cache[src]
        path = self._resolve_media_path(src)
        pm = QPixmap()
        if path:
            low = path.lower()
            if low.endswith(".svg"):
                pm = self._rasterize_svg(path)
            else:
                pm = QPixmap(path)
        self._pixmap_cache[src] = pm
        return pm

    def _rasterize_svg(self, path):
        """Render an SVG file to a QPixmap. Sized so the longest edge is at
        most ``_max_svg_edge`` px (default 4096) — sharp on a 4K board, but
        bounded so a runaway SVG can't blow up RAM."""
        try:
            from PySide6.QtSvg import QSvgRenderer
        except Exception:
            return QPixmap()
        try:
            renderer = QSvgRenderer(path)
        except Exception:
            return QPixmap()
        if not renderer.isValid():
            return QPixmap()
        from PySide6.QtCore import QSize
        size = renderer.defaultSize()
        w, h = size.width(), size.height()
        if w <= 0 or h <= 0:
            w, h = 1920, 1080
        max_edge = getattr(self, "_max_svg_edge", 4096)
        scale = min(1.0, max_edge / max(w, h))
        out_w, out_h = max(1, int(w * scale)), max(1, int(h * scale))
        pm = QPixmap(out_w, out_h)
        pm.fill(Qt.transparent)
        p = QPainter(pm)
        try:
            p.setRenderHint(QPainter.Antialiasing, True)
            p.setRenderHint(QPainter.SmoothPixmapTransform, True)
            renderer.render(p)
        finally:
            p.end()
        return pm

    def _r_image(self, p, o):
        rect = QRectF(o.origin.x, o.origin.y, o.size.x, o.size.y)
        pm = self._get_cached_pixmap(o.src)
        if pm and not pm.isNull():
            p.drawPixmap(rect, pm, QRectF(0, 0, pm.width(), pm.height()))
        else:
            # Placeholder frame when the image can't be loaded yet.
            p.setPen(QPen(QColor(150, 150, 150), 1, Qt.DashLine))
            p.setBrush(QBrush(QColor(40, 40, 40, 60)))
            p.drawRect(rect)

    def _r_ppt(self, p, o):
        rect = QRectF(o.origin.x, o.origin.y, o.size.x, o.size.y)
        # o.src is kept pointed at the CURRENT slide's image. Render it like an
        # image, scaled to fit the object rect (preserve aspect, centered).
        pm = self._get_cached_pixmap(o.src)
        if pm and not pm.isNull():
            pw, ph = pm.width(), pm.height()
            scale = min(rect.width() / pw, rect.height() / ph) if pw and ph else 1.0
            dw, dh = pw * scale, ph * scale
            ox = rect.x() + (rect.width() - dw) / 2.0
            oy = rect.y() + (rect.height() - dh) / 2.0
            p.drawPixmap(QRectF(ox, oy, dw, dh), pm, QRectF(0, 0, pw, ph))
        else:
            # Placeholder if the slide image can't be loaded.
            p.setPen(QPen(QColor(120, 160, 220), 2))
            p.setBrush(QBrush(QColor(30, 40, 60, 120)))
            p.drawRect(rect)
        # Small slide-counter badge in the corner (also shows build step).
        try:
            ac = 0
            try:
                ac = int(getattr(o, "anim_count_for_current", 0))
            except Exception:
                ac = 0
            step = max(0, int(getattr(o, "anim_step", 0)))
            base = "Slide {}/{}".format(o.current_slide + 1, max(1, o.slide_count))
            if ac > 0:
                label = "{}  •  {}/{}".format(base, step + 1, ac + 1)
                badge_w, badge_h = 132, 22
            else:
                label = base
                badge_w, badge_h = 84, 22
            f = QFont("Segoe UI", 11)
            p.setFont(f)
            bx = rect.x() + 6
            by = rect.y() + 6
            p.setPen(Qt.NoPen)
            p.setBrush(QBrush(QColor(0, 0, 0, 150)))
            p.drawRoundedRect(QRectF(bx, by, badge_w, badge_h), 6, 6)
            p.setPen(QPen(QColor(220, 230, 255)))
            p.drawText(QRectF(bx, by, badge_w, badge_h), int(Qt.AlignCenter), label)
        except Exception:
            pass

    # ---- Arrow helper ----
    @staticmethod
    def arrow_head_dims(pen_width):
        # One definition, shared with the double arrow (shape_geometry).
        return sg.arrow_head_dims(pen_width)

    # ---- paintEvent ----
    def paintEvent(self, event):
        # Rebuild the static cache if it is stale, if the widget was resized,
        # or if the window moved to a screen with a different scale factor.
        # The DPR check is a float compare per frame — negligible, and it
        # catches the monitor-swap case without depending on which Qt version
        # delivers DevicePixelRatioChange / ScreenChangeInternal.
        if (self._cache_dirty
                or self._scene_cache is None
                or self._cache_dpr != self.devicePixelRatioF()
                or self._scene_cache.size() / self._cache_dpr != self.size()):
            self._rebuild_cache()
        # Only when a layout change flagged it. _max_scroll walks every object
        # on the page in one-page style, so this must never become per-frame
        # work — the flag is what keeps it to once per page add, delete,
        # import or background change.
        if self._sb_stale:
            self._sync_scrollbar()
        p = QPainter(self)
        try:
            p.setRenderHint(QPainter.SmoothPixmapTransform, True)
            p.setRenderHint(QPainter.Antialiasing, True)
            if self._scene_cache: p.drawPixmap(0, 0, self._scene_cache)
            else: p.fillRect(self.rect(), theme_mgr.qcolor(theme_mgr.tokens.canvas_bg_color))

            # Move overlay — render the floating ids translated by (dx, dy).
            # They were excluded from the cache rebuild, so the only place
            # they appear in the rendered frame is right here. Paint them in
            # world-space (same coordinate setup as _rebuild_cache) so the
            # transform composes correctly with each object's own affine.
            if self._move_overlay_ids:
                self._paint_move_overlay(p)

            # Ink the eraser is touching was held out of the cache; paint it
            # here with the subtraction applied. Same world-space setup as the
            # cache rebuild so the transform composes identically.
            if self._eraser_mask_segments:
                owed = self.apply_scene_transform(p)
                try:
                    self._paint_erase_overlay(p, event.rect())
                finally:
                    if owed:
                        p.restore()

            if self._live_active and self._live_points: self._paint_live(p)
            if self._shape_active: self._paint_shape_preview(p)
            if self._rectsel_active: self._paint_rectsel(p)
            if self._lasso_active and self._lasso_points: self._paint_lasso(p)
            if self._selection_rect: self._paint_selection(p)

            if (self._show_eraser_cursor or self._palm_erasing) and self._cursor_pos.x()>=0:
                # Cursor colour signals which eraser mode is active so the
                # user knows whether they're about to splice strokes (pixel,
                # light blue) or delete whole objects (object, red). Same
                # size / opacity / line weight in both modes — only the hue
                # changes.
                #
                # The palm used to be excluded from this and always drew the
                # light-blue ring, which was exactly backwards: back then a
                # palm ALWAYS did an object erase, so the one contact that was
                # guaranteed to delete whole strokes was the one showing the
                # colour that means "splice". Now that a palm follows the menu
                # like everything else, the mode alone is the honest answer.
                if self._eraser_mode == "object":
                    ring_color = QColor(255, 80, 80, 200)
                else:
                    ring_color = QColor(154, 208, 255, 200)
                p.setPen(QPen(ring_color, 3, Qt.SolidLine, Qt.RoundCap))
                p.setBrush(Qt.NoBrush); r = self.ring_radius_px()
                p.drawEllipse(self._cursor_pos, r, r)
                self._ring_last_painted = QPoint(self._cursor_pos)
                self._ring_last_radius = r
            else:
                # The ring is not supposed to be on screen. If it was drawn
                # last frame its pixels are still sitting there — this repaint
                # may well be a small dirty rect somewhere else entirely — so
                # ask for that box back. Scheduling an update from inside a
                # paint doesn't recurse; it just costs the one extra frame,
                # and only on the transition.
                stale = self._stale_ring_rect()
                self._ring_last_painted = None
                if stale is not None:
                    self.update(stale)

            # Developer overlay: show the last detected input type (Phase 4).
            # Off by default; toggled via set_debug_input_overlay(). Drawn last
            # so it sits on top of everything.
            if self._debug_input_overlay:
                self._paint_input_overlay(p)
        finally: p.end()

    def _paint_input_overlay(self, p) -> None:
        """Top-left HUD showing the most recent touch classification + the
        active touch mode. Purely a development/testing aid."""
        from PySide6.QtGui import QFont
        sc = UiScale.px
        text = self._last_input_class or "—"
        mode = getattr(self, "_touch_mode", "?")
        line1 = f"INPUT: {text}"
        line2 = f"mode: {mode}"
        # Which Qt event family the board is ACTUALLY sending, and where the
        # eraser ring currently thinks it is. ``_last_input_class`` alone
        # can't answer the first question — it's written by the palm
        # classifier, which only ever runs on touch events, so a board
        # driving the app through tablet or mouse events shows "—" here
        # forever. Ring position included because "is the ring tracking?"
        # is exactly the thing that needs eyes on real hardware.
        fam = getattr(self, "_last_input_family", "") or "—"
        # Anything received-then-discarded in the last second is shown in
        # brackets rather than replacing the driving family. Without this the
        # HUD read "mouse" through an entire touch stroke, because the
        # synthesised mouse events Windows adds mid-drag are stamped and then
        # thrown away — see mouseMoveEvent.
        supp = ""
        if (self._suppressed_family
                and time.time() - self._suppressed_at < 1.0):
            supp = f"  ({self._suppressed_family} suppressed)"
        line3 = f"events: {fam}{supp}"
        line4 = f"ring: {self._cursor_pos.x()},{self._cursor_pos.y()}" + (
            "" if self._cursor_pos.x() >= 0 else "  (hidden)")
        # Contact geometry. peak is a high-water mark since the overlay was
        # switched on, so you can press a palm flat and then read off what the
        # frame actually reported without needing a third hand.
        w0, h0 = self._touch_max_wh
        line5 = (f"touches: {self._touch_count}   "
                 f"max: {w0:.0f}x{h0:.0f} = {self._touch_max_area:.0f}px²")
        line6 = f"peak area: {self._touch_peak_area:.0f}px²" + (
            "   (frame reports NO ellipse)"
            if self._touch_count and self._touch_peak_area <= 0 else "")
        f = QFont(); f.setPointSize(max(9, sc(11))); f.setBold(True)
        p.setFont(f)
        fm = p.fontMetrics()
        lines = (line1, line2, line3, line4, line5, line6)
        w = max(fm.horizontalAdvance(s) for s in lines) + sc(20)
        h = fm.height() * len(lines) + sc(16)
        x, y = sc(12), sc(12)
        # Color-code by detected class for quick reading.
        cls = text.split(" ")[0] if text else ""
        color = {
            "stylus": QColor(96, 165, 250, 230),
            "eraser": QColor(244, 114, 182, 230),
            "palm":   QColor(248, 113, 113, 230),
        }.get(cls, QColor(148, 163, 184, 230))
        p.setBrush(QColor(0, 0, 0, 165)); p.setPen(Qt.NoPen)
        p.drawRoundedRect(QRectF(x, y, w, h), sc(8), sc(8))
        p.setPen(color)
        p.drawText(QRectF(x + sc(10), y + sc(6), w, fm.height()),
                   int(Qt.AlignLeft | Qt.AlignVCenter), line1)
        p.setPen(QColor(200, 200, 200, 230))
        for i, line in enumerate(lines[1:], start=1):
            p.drawText(
                QRectF(x + sc(10), y + sc(6) + fm.height() * i, w, fm.height()),
                int(Qt.AlignLeft | Qt.AlignVCenter), line)

    def set_debug_input_overlay(self, on: bool) -> None:
        """Enable/disable the developer input-type overlay and repaint.

        Also arms per-event logging, so one stroke on real hardware leaves a
        complete transcript in logicboard_debug.log. Turning the overlay off
        disarms it again — this is verbose by design and must not run in a
        normal lesson."""
        self._debug_input_overlay = bool(on)
        self._log_input_events = bool(on)
        if on:
            self._touch_peak_area = 0.0
            logging.info("INPUT DIAG armed — family | detail | action. "
                         "'suppressed' = event was received and deliberately "
                         "discarded (see _ignore_mouse_until).")
        self.update()

    # ---- input diagnostics -------------------------------------------------
    def _note_input(self, family: str, detail: str = "", *,
                    acted: bool = True) -> None:
        """Record one input event for the HUD and the log.

        ``acted`` is the whole point: an event the app RECEIVES is not
        necessarily an event the app USES. Conflating the two is exactly what
        made the HUD report "mouse" during touch strokes."""
        if acted:
            self._last_input_family = family
        else:
            self._suppressed_family = family
            self._suppressed_at = time.time()
        if self._log_input_events:
            logging.debug("INPUT %-14s %-34s %s", family, detail,
                          "acted" if acted else "SUPPRESSED")

    def _note_touch_geometry(self, points) -> None:
        """Capture contact count and the largest contact's ellipse.

        The board won't register a palm, and palm classification keys off
        ellipseDiameters() — so the question "what does this digitizer report
        for a big contact?" is the one that has to be answered on the glass.
        An area of 0 with contacts down means the frame reports no ellipse at
        all, in which case palm detection cannot work by any tuning."""
        live = [tp for tp in points
                if self._tstate_name(tp) != "Released"]
        self._touch_count = len(live)
        best_area, best_wh = 0.0, (0.0, 0.0)
        for tp in live:
            try:
                d = tp.ellipseDiameters()
                w, h = float(d.width()), float(d.height())
            except Exception:
                continue
            area = 3.14159265 * (w / 2.0) * (h / 2.0)
            if area > best_area:
                best_area, best_wh = area, (w, h)
        self._touch_max_area = best_area
        self._touch_max_wh = best_wh
        if best_area > self._touch_peak_area:
            self._touch_peak_area = best_area

    @staticmethod
    def _tstate_name(tp) -> str:
        try:
            return tp.state().name.split("_")[-1]
        except Exception:
            return "?"

    def _paint_selection(self, p):
        # While the rotation handle is being dragged we skip drawing the
        # selection box + handles entirely. Reason: _selection_rect is the
        # PRE-rotation axis-aligned bbox — it's not recomputed live during
        # a rotate. Painting it mid-drag shows the box at the wrong angle
        # relative to the (rotating) content, which reads as broken.
        # A repaint at rotate-release rebuilds the bbox and the selection
        # UI comes back correct.
        if getattr(self, "_rotating", False):
            return
        nv = self.apply_scene_transform(p)
        # CRITICAL: theme_mgr.qcolor() parses 'rgb(76, 194, 255)' correctly.
        # Calling QColor(token_string) DIRECTLY returned an invalid colour
        # (Qt6 doesn't parse CSS rgb() syntax) — that's why the selection
        # rectangle was rendering as solid black.
        accent = theme_mgr.qcolor(theme_mgr.tokens.accent)
        r = self._selection_rect
        pen = QPen(accent, 3.0/ self.effective_scale(), Qt.DashLine,
                    Qt.RoundCap, Qt.RoundJoin)
        pen.setDashPattern([5, 4])
        p.setPen(pen); p.setBrush(Qt.NoBrush); p.drawRect(r)
        # Handles — white-filled circles with accent stroke.
        hs = 12.0/ self.effective_scale()
        p.setPen(QPen(accent, 2.0/ self.effective_scale()))
        p.setBrush(QBrush(QColor(255, 255, 255)))
        for x,y in [(r.left(),r.top()),(r.right(),r.top()),(r.left(),r.bottom()),(r.right(),r.bottom()),
                     (r.center().x(),r.top()),(r.center().x(),r.bottom()),
                     (r.left(),r.center().y()),(r.right(),r.center().y())]:
            p.drawEllipse(QPointF(x,y), hs/2, hs/2)
        if nv or self._scroll_y: p.restore()

        # ── Rotation handle (PowerPoint-style) ────────────────────────────
        # Drawn in SCREEN space so it stays a constant size and the 🔄 emoji
        # renders crisp regardless of zoom. A short vertical line rises from the
        # middle of the top edge to a round handle the user grabs to rotate.
        top_c = self._to_widget(QPointF(r.center().x(), r.top()))
        rot_c = QPointF(top_c.x(), top_c.y() - ROTATE_HANDLE_OFFSET)
        p.save()
        p.setRenderHint(QPainter.Antialiasing, True)
        p.setPen(QPen(accent, 2.0)); p.setBrush(Qt.NoBrush)
        p.drawLine(top_c, rot_c)
        p.setBrush(QBrush(QColor(255, 255, 255)))
        p.setPen(QPen(accent, 2.0))
        p.drawEllipse(rot_c, ROTATE_HANDLE_RADIUS, ROTATE_HANDLE_RADIUS)
        f = p.font(); f.setPixelSize(int(ROTATE_HANDLE_RADIUS * 1.4)); p.setFont(f)
        p.drawText(QRectF(rot_c.x() - ROTATE_HANDLE_RADIUS, rot_c.y() - ROTATE_HANDLE_RADIUS,
                          ROTATE_HANDLE_RADIUS * 2, ROTATE_HANDLE_RADIUS * 2),
                   Qt.AlignCenter, "🔄")
        p.restore()

    def _paint_rectsel(self, p):
        if not self._rectsel_start or not self._rectsel_end: return
        nv = self.apply_scene_transform(p)
        s,e = self._rectsel_start, self._rectsel_end
        pen = QPen(QColor(100,180,255,200), 1.5/ self.effective_scale(), Qt.DashLine)
        pen.setDashPattern([5,3]); p.setPen(pen)
        p.setBrush(QBrush(QColor(100,180,255,25)))
        p.drawRect(QRectF(min(s.x(),e.x()), min(s.y(),e.y()), abs(e.x()-s.x()), abs(e.y()-s.y())))
        if nv or self._scroll_y: p.restore()

    def _paint_lasso(self, p):
        nv = xf = self.apply_scene_transform(p)
        if self._lasso_erase:
            # The lasso ERASER: red, with a faint fill over what will go —
            # the area matters here, not just the outline.
            if len(self._lasso_points) > 2:
                p.setPen(Qt.NoPen)
                p.setBrush(QBrush(QColor(255, 90, 95, 34)))
                p.drawPolygon(QPolygonF(self._lasso_points))
            pen = QPen(QColor(255, 90, 95, 230), 2.4 / self.effective_scale(),
                       Qt.DashLine)
            pen.setDashPattern([5, 4]); pen.setCapStyle(Qt.RoundCap); pen.setJoinStyle(Qt.RoundJoin)
            p.setPen(pen); p.setBrush(Qt.NoBrush)
            if self._lasso_path is not None:
                p.drawPath(self._lasso_path)
            if len(self._lasso_points) > 2:
                p.setPen(QPen(QColor(255, 90, 95, 120), 1.2 / self.effective_scale(), Qt.DotLine))
                p.drawLine(self._lasso_points[-1], self._lasso_points[0])
            if xf: p.restore()
            return
        # Outline-only lasso — no fill, like a real graphics-editor lasso tool
        pen = QPen(QColor(100,180,255,220), 2.0/ self.effective_scale(), Qt.DashLine)
        pen.setDashPattern([5, 4]); pen.setCapStyle(Qt.RoundCap); pen.setJoinStyle(Qt.RoundJoin)
        p.setPen(pen); p.setBrush(Qt.NoBrush)
        # PERF: draw the CACHED path. It's grown incrementally in add_lasso_point
        # via path.lineTo(new_pt) — so this paint is O(1) in N (just a path
        # stroke). Rebuilding it here was O(N) per paint, which is why long
        # strokes used to grind to a halt.
        if self._lasso_path is not None:
            p.drawPath(self._lasso_path)
        # Optional faint closing line (dotted, very subtle)
        if len(self._lasso_points) > 2:
            close_pen = QPen(QColor(100,180,255,100), 1.0/ self.effective_scale(), Qt.DotLine)
            p.setPen(close_pen)
            p.drawLine(self._lasso_points[-1], self._lasso_points[0])
        if xf: p.restore()

    def _paint_shape_preview(self, p):
        nv = xf = self.apply_scene_transform(p)
        pen = QPen(self._shape_color, self._pen_width, Qt.SolidLine, Qt.RoundCap, Qt.RoundJoin)
        p.setPen(pen); p.setBrush(Qt.NoBrush)
        s,e = self._shape_start, self._shape_end; t = self._shape_tool
        if t in sg.SHAPE_KINDS:
            sg.paint(p, t, (s.x(), s.y()), (e.x(), e.y()),
                     self._shape_color, self._pen_width)
        elif t == "line": p.drawLine(s, e)
        elif t == "rect":
            p.drawRect(QRectF(min(s.x(),e.x()),min(s.y(),e.y()),abs(e.x()-s.x()),abs(e.y()-s.y())))
        elif t in ("ellipse", "circle"):
            # For "circle" the end was already constrained to a square in
            # update_shape_preview, so drawing as an ellipse renders a circle.
            p.drawEllipse(QPointF((s.x()+e.x())/2,(s.y()+e.y())/2), abs(e.x()-s.x())/2, abs(e.y()-s.y())/2)
        elif t == "triangle":
            # Apex top-centre, base on the bottom of the drag rect.
            x0, x1 = min(s.x(), e.x()), max(s.x(), e.x())
            y0, y1 = min(s.y(), e.y()), max(s.y(), e.y())
            pa = QPainterPath()
            pa.moveTo(QPointF((x0 + x1) / 2.0, y0))   # apex
            pa.lineTo(QPointF(x0, y1))                # bottom-left
            pa.lineTo(QPointF(x1, y1))                # bottom-right
            pa.closeSubpath()
            p.drawPath(pa)
        elif t == "arrow":
            hl, hw = self.arrow_head_dims(self._pen_width)
            ang = math.atan2(e.y()-s.y(), e.x()-s.x())
            # Shaft stops at arrowhead base
            bx = e.x()-hl*math.cos(ang); by = e.y()-hl*math.sin(ang)
            p.drawLine(s, QPointF(bx, by))
            # Filled arrowhead triangle — tip at e
            lx = bx+hw/2*math.cos(ang+math.pi/2); ly = by+hw/2*math.sin(ang+math.pi/2)
            rx = bx+hw/2*math.cos(ang-math.pi/2); ry = by+hw/2*math.sin(ang-math.pi/2)
            pa = QPainterPath(); pa.moveTo(e); pa.lineTo(QPointF(lx,ly))
            pa.lineTo(QPointF(rx,ry)); pa.closeSubpath()
            p.setPen(Qt.NoPen); p.setBrush(QBrush(self._pen_color)); p.drawPath(pa)
        if xf: p.restore()

    def _paint_move_overlay(self, p):
        """Render the dragged objects on top of the static scene cache,
        translated by the live ``(dx, dy)`` offset. Same coordinate setup
        as ``_rebuild_cache`` so each object's own affine composes
        correctly with the view transform AND the live drag translation.
        """
        scene = self.current_scene()
        if scene is None or not self._move_overlay_ids:
            return
        owed = self.apply_scene_transform(p)
        if not owed:
            p.save()
        try:
            # Apply the live drag translation in WORLD space — every
            # floating object slides together by exactly this amount.
            p.translate(self._move_overlay_dx, self._move_overlay_dy)
            ids = self._move_overlay_ids
            # Iterate by object index (cheap) and filter — scene.object_at
            # is O(1), find_mut would be a hash lookup per id but we'd lose
            # render order. Preserving order matters for z-stacking.
            for i in range(scene.object_count):
                obj = scene.object_at(i)
                if obj and obj.visible and obj.id in ids:
                    self._render_object(p, obj)
        finally:
            p.restore()

    def _paint_live(self, p):
        # The SETTLED part of the in-progress stroke is already rasterised in
        # the cache layer (baked in batches by _draw_live_segment). We blit it
        # — constant cost regardless of stroke length. The layer is in widget
        # space, so no transform is applied for the blit.
        #
        # The UNSETTLED tail (at most _BAKE_BATCH + _TAIL_OVERLAP samples) is
        # NOT in the layer. Its trailing segment is provisional — the next
        # sample supersedes it with a quad — so baking it would leave a
        # permanent whisker overshooting every sample. Drawing it here instead
        # costs one short path per frame and keeps the leading edge of the
        # stroke at full one-shot quality, which is where the eye is looking.
        #
        # Highlighter translucency is applied once, to the whole group, so
        # overlapping joints never darken.
        if self._live_layer is None:
            return
        color, width, op = self._live_stroke_pen()
        if op != 1.0:
            p.setOpacity(op)
        p.drawPixmap(0, 0, self._live_layer)

        pts = self._live_points
        n = len(pts)
        if n:
            owed = self.apply_scene_transform(p)
            if not owed:
                p.save()
            p.setRenderHint(QPainter.Antialiasing, True)
            if self._live_is_chisel:
                # Chisel nib — a FILLED ribbon, not a stroked centerline, so
                # the live preview matches what _r_highlighter commits.
                p.setPen(Qt.NoPen)
                p.setBrush(QBrush(color))
                dense = self._live_chisel_dense(pts, n, width)
                # One dense point back from where the layer stops, so the
                # tail's first segment closes onto the baked one exactly.
                start_k = max(0, self._live_bake_k - 1)
                p.drawPath(self._chisel_ribbon(
                    dense[start_k:], width, self._live_nib_angle))
            else:
                p.setPen(QPen(color, width, Qt.SolidLine, Qt.RoundCap, Qt.RoundJoin))
                p.setBrush(Qt.NoBrush)
                if n == 1:
                    # A single tap still leaves a dot (matches old behaviour).
                    p.drawPoint(pts[0])
                else:
                    p.drawPath(self._build_freehand_tail_path(
                        pts, self._live_tail_start, tip=True))
            p.restore()

        if op != 1.0:
            p.setOpacity(1.0)

    # ---- APIs ----
    def begin_live_stroke(self, *, is_highlighter=False, is_eraser=False,
                          is_brush=False):
        from ui.canvas.brush_ink import BRUSH_NIB_ANGLE_DEG
        self._live_points.clear(); self._live_pressures.clear()
        self._live_active = True; self._live_is_highlighter = is_highlighter; self._live_is_eraser = is_eraser
        self._live_is_brush = is_brush
        self._live_is_chisel = is_highlighter or is_brush
        self._live_nib_angle = BRUSH_NIB_ANGLE_DEG if is_brush else 0.0
        # Fresh transparent cache layer the size of the viewport (DPR-aware).
        dpr = self.devicePixelRatioF()
        w = max(1, int(self.width()  * dpr))
        h = max(1, int(self.height() * dpr))
        self._live_layer = QPixmap(w, h)
        self._live_layer.setDevicePixelRatio(dpr)
        self._live_layer.fill(Qt.transparent)
        self._live_drawn_idx = 0
        # Batched-bake bookkeeping — see _draw_live_segment.
        self._live_baked_idx = 0
        self._live_tail_start = 0
        self._live_tail_rect = None
        self._live_thin = []
        self._live_thin_src = []
        self._live_thin_upto = 0
        self._live_dense = None
        self._live_dense_n = -1
        self._live_dense_settled = 0
        self._live_bake_k = 0

    def _live_stroke_pen(self):
        """(QColor, width, layer_opacity) for the current live tool. The layer
        is always painted at FULL alpha; translucency (highlighter) is applied
        once when the layer is blitted, so overlapping joints never darken."""
        if self._live_is_eraser:
            return QColor(self.get_bg_color()), float(self.eraser_size), 1.0
        if self._live_is_brush:
            # Opaque: no layer translucency to apply on the blit.
            return QColor(self._brush_color), float(self._brush_height), 1.0
        if self._live_is_highlighter:
            c = QColor(self._highlighter_color); c.setAlpha(255)
            return c, float(self._highlighter_height), self._highlighter_alpha / 255.0
        return self.active_pen_color(), float(self._pen_width), 1.0

    def effective_scale(self) -> float:
        """Scene-to-screen scale for the CURRENT page in the current style.

        Overlay painters divide their pen widths by this so a dashed
        selection box stays 2 px on screen whatever the zoom.
        """
        if self._canvas_style == "scroll_list":
            return self._list_scale
        return self._view_scale

    def apply_scene_transform(self, p) -> bool:
        """Put ``p`` into CURRENT-PAGE scene coordinates. Returns True if a
        save() is owed (the caller must restore()).

        THE fix for "it draws across the whole screen until I lift the pen".
        Every live overlay — the in-progress stroke, shape preview, lasso,
        selection box, erase overlay — mapped scene points with the ONE-PAGE
        transform. In list style those points are page-local, so at
        view_scale 1.0 and no page offset they landed relative to the widget
        origin at full size: a stroke sprawled across the window, then
        snapped into its sheet on release when the committed object was
        drawn by the (correct) list renderer instead.

        One definition, used by every overlay, so the live preview and the
        committed object cannot disagree again.
        """
        if self._canvas_style == "scroll_list":
            r = self.page_rect_on_screen(self._current_page)
            if r is None:
                return False
            p.save()
            # CLIP TO THE SHEET, before the transform so the rect is still in
            # widget coordinates. Each page is its own canvas: ink that runs
            # off its edge stops at the edge instead of continuing across the
            # gap and the margins, which is what made strokes look like they
            # were drawn on the window rather than on a page. The committed
            # renderer already clipped per sheet; every LIVE overlay — the
            # in-progress stroke, the shape preview, the lasso, the erase
            # overlay — did not, so the two disagreed the whole time a
            # gesture was in flight.
            p.setClipRect(r)
            p.translate(r.x(), r.y())
            p.scale(self._list_scale, self._list_scale)
            return True
        nv = (self._view_scale != 1.0 or self._view_offset != QPointF(0, 0)
              or self._scroll_y)
        if not nv:
            return False
        p.save()
        p.translate(self._view_offset.x(),
                    self._view_offset.y() - self._scroll_y)
        p.scale(self._view_scale, self._view_scale)
        return True

    def _to_widget(self, ptf):
        # Same mapping as scene_to_screen — the live stroke and the committed
        # object have to land in the same place.
        if self._canvas_style == "scroll_list":
            r = self.page_rect_on_screen(self._current_page)
            if r is not None:
                return QPointF(ptf.x() * self._list_scale + r.x(),
                               ptf.y() * self._list_scale + r.y())
        return QPointF(
            ptf.x() * self._view_scale + self._view_offset.x(),
            ptf.y() * self._view_scale + self._view_offset.y() - self._scroll_y,
        )

    def _build_freehand_path(self, pts) -> "QPainterPath":
        """Build the path for a freehand stroke. SINGLE SOURCE OF TRUTH used
        by BOTH the live preview (``_paint_live`` / ``_draw_live_segment``) and
        the committed renderer (``_r_freehand``). Any future tweak to the
        smoothing algorithm goes here once and affects both call sites — there's
        no way for the two paths to drift apart.

        Accepts either a list of QPointF (live) or a list of (x, y) tuples
        (engine ``obj.points()``). Returns an empty path for empty input.

        PERF: the coordinate accessors are resolved ONCE up front. The old
        version called ``hasattr(p, "x")`` inside two nested helpers on every
        single coordinate access — 4 Python calls + 2 hasattr per point, i.e.
        ~3,600 interpreter round-trips for a 600-point stroke, on every rebuild.
        """
        path = QPainterPath()
        n = len(pts)
        if n == 0:
            return path

        # Normalise once, not per access.
        if hasattr(pts[0], "x"):
            xs = [p.x() for p in pts]
            ys = [p.y() for p in pts]
        else:
            xs = [p[0] for p in pts]
            ys = [p[1] for p in pts]

        if n == 1:
            path.moveTo(xs[0], ys[0])
            return path
        if n == 2:
            path.moveTo(xs[0], ys[0])
            path.lineTo(xs[1], ys[1])
            return path

        # Quadratic-Bezier midpoint smoothing.
        path.moveTo(xs[0], ys[0])
        path.lineTo((xs[0] + xs[1]) * 0.5, (ys[0] + ys[1]) * 0.5)
        for i in range(1, n - 1):
            path.quadTo(xs[i], ys[i],
                        (xs[i] + xs[i + 1]) * 0.5,
                        (ys[i] + ys[i + 1]) * 0.5)
        # Close with a straight tail to the latest sample so the brush
        # tracks the actual cursor position with no perceptible lag.
        path.lineTo(xs[n - 1], ys[n - 1])
        return path

    def _build_freehand_tail_path(self, pts, s: int, *, tip: bool) -> "QPainterPath":
        """The section of the smoothed path covering samples ``s..n-1``.

        Exact, not an approximation. In midpoint-quad smoothing the path is a
        chain of quads whose endpoints are the midpoints between consecutive
        samples:

            p0 -> mid(p0,p1) -> [quad via p1] -> mid(p1,p2) -> [quad via p2] ...

        so the shape near sample ``i`` depends only on ``i-1, i, i+1``.
        Resuming at ``mid(p[s], p[s+1])`` and emitting quads from ``s+1``
        reproduces that section bit-for-bit — no seams, no kinks. The old
        code's visible batch-boundary kinks came from resuming at a raw sample
        instead of at a midpoint, not from a limitation of incremental drawing.

        ``tip`` controls the trailing segment to the newest sample:

          * ``tip=True``  — include it. Used for the UNSETTLED tail, which is
            redrawn every frame so the brush tracks the cursor.
          * ``tip=False`` — omit it. Used when BAKING into the layer, because
            that segment is provisional: the next sample supersedes it with a
            quad, and baking it would leave a permanent whisker overshooting
            every sample (measured up to ~15 px on a hard direction change).
        """
        path = QPainterPath()
        n = len(pts)
        if n - s < 2:
            return path

        xs = [pts[i].x() for i in range(s, n)]
        ys = [pts[i].y() for i in range(s, n)]
        m = len(xs)

        if s == 0:
            # Stroke head — identical opening to the full builder.
            path.moveTo(xs[0], ys[0])
            path.lineTo((xs[0] + xs[1]) * 0.5, (ys[0] + ys[1]) * 0.5)
        else:
            # Resume exactly where the baked chain ended.
            path.moveTo((xs[0] + xs[1]) * 0.5, (ys[0] + ys[1]) * 0.5)

        for i in range(1, m - 1):
            path.quadTo(xs[i], ys[i],
                        (xs[i] + xs[i + 1]) * 0.5,
                        (ys[i] + ys[i + 1]) * 0.5)
        if tip:
            path.lineTo(xs[m - 1], ys[m - 1])
        return path

    def _live_chisel_dense(self, pts, n: int, width: float):
        """The dense centreline for samples ``[0, n)`` of the live stroke.

        Grown in place rather than rebuilt. Thinning is the expensive half
        — measured at 0.5 ms for 600 samples and 1.0 ms for 1200, against a
        near-constant 0.17 ms for the smoothing on top, because thinning
        collapses a long stroke hard before the smoothing ever sees it. So
        the thinned list is extended one sample at a time and kept, and only
        the cheap half is redone.

        Extending is sound for exactly the reason the old per-piece thinning
        was not: the keep decision looks at the previous KEPT point and the
        next raw sample, so growing the stroke never revises a decision
        already made. The last raw sample is deliberately NOT folded in — it
        is the moving tip, and it would have to be un-kept on the next
        sample — so it is appended fresh each time instead.
        """
        def raw(i):
            q = pts[i]
            try:
                return q.x(), q.y()
            except AttributeError:      # committed strokes hand back tuples
                return q[0], q[1]

        if n <= 0:
            return []
        if self._live_dense is not None and self._live_dense_n == n:
            return self._live_dense

        tol = max(0.25, min(0.75, width * 0.02))
        thin, src = self._live_thin, self._live_thin_src
        if not thin:
            thin.append(raw(0))
            src.append(0)
            self._live_thin_upto = 1

        for i in range(max(1, self._live_thin_upto), max(1, n - 1)):
            ax, ay = thin[-1]
            bx, by = raw(i)
            cx, cy = raw(i + 1)
            ux, uy = cx - ax, cy - ay
            seg = math.hypot(ux, uy)
            if seg < 1e-6:
                continue
            if abs((bx - ax) * uy - (by - ay) * ux) / seg > tol:
                thin.append((bx, by))
                src.append(i)
        self._live_thin_upto = max(self._live_thin_upto, max(1, n - 1))

        chain = list(thin)
        if n > 1:
            tip = raw(n - 1)
            if tip != chain[-1]:
                chain.append(tip)
        dense, marks = CanvasWidget._smooth_dense(chain, with_marks=True)
        self._live_dense = dense
        # Where the still-moving part begins. One input span back from the
        # end, because that span is the one reaching the tip; a span can
        # expand to two dozen dense points, so counting dense points instead
        # would bake geometry that has not settled and leave the tail
        # redrawing a DIFFERENT shape over it.
        self._live_dense_settled = marks[max(0, len(chain) - 2)]
        self._live_dense_n = n
        return dense

    def _live_tail_raw_start(self) -> int:
        """A raw sample index at or before everything the tail redraws.

        Only used to bound the repaint region, so erring early is free and
        erring late leaves trails on screen. Two thinned spans back from the
        end covers the couple of dense points the bake leaves behind.
        """
        src = self._live_thin_src
        if not src:
            return 0
        return src[max(0, len(src) - 3)]

    def _widget_rect_of(self, pts, lo: int, hi: int, width: float):
        """Widget-space bounding QRect of ``pts[lo:hi]``, padded for the pen.

        ``pts`` are in scene space; ``_to_widget`` applies the view scale,
        offset and scroll. Padding covers half the on-screen stroke width plus
        a few px for the antialias fringe and round cap/join overhang.
        """
        min_x = min_y = 1e18
        max_x = max_y = -1e18
        for i in range(lo, hi):
            q = self._to_widget(pts[i])
            qx, qy = q.x(), q.y()
            if qx < min_x: min_x = qx
            if qx > max_x: max_x = qx
            if qy < min_y: min_y = qy
            if qy > max_y: max_y = qy
        pad = (width * self.effective_scale()) * 0.5 + 3.0
        return QRectF(min_x - pad, min_y - pad,
                      (max_x - min_x) + 2.0 * pad,
                      (max_y - min_y) + 2.0 * pad).toAlignedRect()

    def _draw_live_segment(self):
        """Advance the live stroke: bake settled geometry, track the tail.

        Replaces the old full-stroke rebuild, which per pointer event did:

          * ``_live_layer.fill(Qt.transparent)`` — a full-viewport clear. The
            layer is allocated at viewport size x DPR, so on a 4K panel that
            was a ~33 MB memset. At a 200 Hz stylus rate that is ~6.6 GB/s of
            memory bandwidth spent clearing a buffer about to be overdrawn.
            THIS is why the lag scaled with monitor resolution.
          * an O(N) path rebuild + O(N) antialiased rasterise of the ENTIRE
            stroke — O(N^2) integrated over the stroke. THIS is why the lag
            scaled with stroke length.
          * ``return self.rect()`` — the "dirty rect" was the whole widget, so
            paintEvent could not clip and re-blitted the full scene cache plus
            the full live layer every frame.

        Now: no fill, one bake per ``_BAKE_BATCH`` samples, and a tight dirty
        rect. Cost is O(1) in both stroke length and resolution.

        Returns the widget-space dirty rect, or None if nothing changed.
        """
        if self._live_layer is None:
            return None
        pts = self._live_points
        n = len(pts)
        if n == 0:
            return None

        color, width, _op = self._live_stroke_pen()
        prev_tail_rect = self._live_tail_rect
        baked_rect = None

        # ---- 1. bake, but only once every _BAKE_BATCH samples ----
        if n - self._live_baked_idx >= self._BAKE_BATCH:
            s = max(0, self._live_baked_idx - self._TAIL_OVERLAP)
            if self._live_is_chisel:
                # Slice the stroke's ONE centreline. Consecutive baked runs
                # and the tail SHARE AN ENDPOINT instead of overlapping a
                # span: identical geometry either side of a single common
                # point, so there is no gap to show and no doubled region to
                # darken. The old raw-sample overlap could deliver neither,
                # because the two ribbons were built from different
                # thinnings. See _chisel_ribbon.
                dense = self._live_chisel_dense(pts, n, width)
                # Only what can no longer move. _live_dense_settled is the
                # start of the span still reaching the tip.
                new_k = self._live_dense_settled
                start_k = max(0, self._live_bake_k - 1)
                if new_k - start_k >= 2:
                    settled = self._chisel_ribbon(dense[start_k:new_k], width,
                                                  self._live_nib_angle)
                    # Advanced ONLY on an actual draw. Moving it without
                    # drawing skips that span in the layer, and the tail
                    # starts after it, so the ink is simply never laid down.
                    self._live_bake_k = new_k
                else:
                    settled = QPainterPath()
            else:
                settled = self._build_freehand_tail_path(pts, s, tip=False)
            if settled.elementCount() > 0:
                p = QPainter(self._live_layer)
                p.setRenderHint(QPainter.Antialiasing, True)
                # The baked layer is in WIDGET space, so the settled path
                # needs the same scene->widget transform the overlay uses —
                # in list style that is the current sheet's origin and the
                # list scale, not the one-page view.
                xf = self.apply_scene_transform(p)
                if self._live_is_chisel:
                    p.setPen(Qt.NoPen); p.setBrush(QBrush(color))
                else:
                    p.setPen(QPen(color, width, Qt.SolidLine,
                                  Qt.RoundCap, Qt.RoundJoin))
                    p.setBrush(Qt.NoBrush)
                p.drawPath(settled)
                if xf:
                    p.restore()
                p.end()
                baked_rect = self._widget_rect_of(pts, s, n, width)
            self._live_baked_idx = n

        # ---- 2. the unsettled tail, redrawn each frame by _paint_live ----
        # Starts _TAIL_OVERLAP samples back from the baked boundary so its
        # opening midpoint coincides exactly with where the baked chain ended.
        # For a chisel the tail is bounded in centreline terms, not by a
        # fixed number of raw samples — thinning may have collapsed a long
        # straight run into one span — so the repaint box is measured from
        # the thinned points instead. Getting this too small is what leaves
        # ink trails behind the nib.
        self._live_tail_start = (self._live_tail_raw_start()
                                 if self._live_is_chisel
                                 else max(0, self._live_baked_idx
                                          - self._TAIL_OVERLAP))
        self._live_tail_rect = self._widget_rect_of(
            pts, self._live_tail_start, n, width)

        # ---- 3. dirty region: new tail + old tail + anything just baked ----
        dirty = self._live_tail_rect
        if prev_tail_rect is not None:
            dirty = dirty.united(prev_tail_rect)
        if baked_rect is not None:
            dirty = dirty.united(baked_rect)
        return dirty.intersected(self.rect())

    def add_live_point(self, pt, pressure=-1.0):
        """Append a sample to the live stroke and repaint just what changed.

        Coalesces sub-pixel samples: a digitiser reporting at 100-300 Hz
        produces far more samples than there are pixels to draw them on.
        Dropping the redundant ones costs nothing visually and also shortens
        the point list that gets simplified and serialised on commit.
        """
        qpt = QPointF(pt)

        if self._live_points:
            last = self._live_points[-1]
            dx = qpt.x() - last.x()
            dy = qpt.y() - last.y()
            # Compare in SCREEN px — at low zoom, samples far apart in scene
            # space can still land on the same pixel.
            sc = self.effective_scale() or 1.0
            if (dx * dx + dy * dy) * (sc * sc) < self._MIN_SAMPLE_DIST_SQ:
                return

        self._live_points.append(qpt)
        if pressure >= 0:
            self._live_pressures.append(pressure)

        dirty = self._draw_live_segment()
        if dirty is not None and not dirty.isEmpty():
            self.update(dirty)          # repaint ONLY the affected region
        else:
            self.update()

    def end_live_stroke(self):
        pts = list(self._live_points); self._live_active = False; self._live_points.clear()
        self._live_layer = None; self._live_drawn_idx = 0
        self._live_baked_idx = 0; self._live_tail_start = 0; self._live_tail_rect = None
        self._live_pressures_copy = list(self._live_pressures); self._live_pressures.clear(); return pts
    def get_live_pressures(self): return getattr(self, '_live_pressures_copy', [])

    def begin_shape_preview(self, tool, start):
        self._shape_tool = tool; self._shape_start = QPointF(start)
        self._shape_end = QPointF(start); self._shape_active = True; self.update()
    def update_shape_preview(self, end):
        # For "circle" the bounding rect is constrained to a square (perfect
        # circle, only one degree of freedom: the radius).
        if self._shape_tool == "circle":
            end = self._constrain_to_circle(self._shape_start, QPointF(end))
        elif sg.locked_aspect(self._shape_tool):
            # Square, semicircle, cube, sphere, hemisphere: one right
            # proportion, kept while dragging — the circle's rule.
            s = self._shape_start
            x, y = sg.constrain_end(self._shape_tool, (s.x(), s.y()),
                                    (end.x(), end.y()))
            end = QPointF(x, y)
        self._shape_end = QPointF(end); self.update()

    @staticmethod
    def _constrain_to_circle(s, e):
        """Snap end point so the bounding rect is a perfect square (side =
        max(|dx|, |dy|)), preserving the drag direction."""
        dx = e.x() - s.x(); dy = e.y() - s.y()
        side = max(abs(dx), abs(dy))
        return QPointF(
            s.x() + (side if dx >= 0 else -side),
            s.y() + (side if dy >= 0 else -side),
        )
    def end_shape_preview(self): self._shape_active = False; self._shape_tool = ""; self.update()

    def set_selection(self, ids, rect):
        self._selected_ids = list(ids); self._selection_rect = rect
        self.update(); self.selection_changed.emit()
    def clear_selection(self):
        had = bool(self._selected_ids)
        self._selected_ids.clear(); self._selection_rect = None
        self.update()
        if had: self.selection_changed.emit()
    def get_selected_ids(self): return list(self._selected_ids)
    def get_selection_rect(self): return self._selection_rect
    def selected_types(self):
        """Return the set of type_names among currently selected objects."""
        types = set()
        scene = self.current_scene()
        if scene is None: return types
        for oid in self._selected_ids:
            o = scene.find(oid)
            if o: types.add(o.type_name)
        return types

    def begin_rectsel(self, start):
        self._rectsel_start = QPointF(start); self._rectsel_end = QPointF(start); self._rectsel_active = True
    def update_rectsel(self, end): self._rectsel_end = QPointF(end); self.update()
    def end_rectsel(self):
        s,e = self._rectsel_start, self._rectsel_end; self._rectsel_active = False
        self._rectsel_start = None; self._rectsel_end = None; self.update()
        if s and e: return QRectF(min(s.x(),e.x()),min(s.y(),e.y()),abs(e.x()-s.x()),abs(e.y()-s.y()))
        return None

    def begin_lasso(self, erase: bool = False):
        self._lasso_points.clear()
        self._lasso_active = True
        self._lasso_erase = bool(erase)
        self._lasso_path = None
        self._lasso_last_screen = None

    def add_lasso_point(self, pt):
        """Append a point to the in-progress lasso.

        PERF: three things are deliberately optimised here vs the naive
        ``points.append(pt); self.update()`` version:

          1. **Point thinning** — a stylus reports ~200 Hz; consecutive
             samples are often sub-pixel. We drop points closer than ~0.7
             scene-px to the previous one. The lasso still feels smooth
             (the eye can't see <1 px wiggle on a 4K screen) and the
             post-stroke point-in-polygon test stays cheap.

          2. **Incremental path** — extend the cached ``_lasso_path`` with
             one ``lineTo`` instead of rebuilding the whole path from N
             points every paint. Paint becomes O(1) in stroke length.

          3. **Dirty-rect repaint** — ``self.update()`` with NO rect repainted
             the ENTIRE canvas every move event (millions of pixels on a 4K
             screen). We now ask Qt to repaint only the rectangle covering
             the new segment plus pen-width padding.
        """
        sp = QPointF(pt)
        if self._lasso_points:
            prev = self._lasso_points[-1]
            dx = sp.x() - prev.x(); dy = sp.y() - prev.y()
            if dx*dx + dy*dy < 0.5:        # ~0.7 px — sub-pixel jitter
                return
        self._lasso_points.append(sp)
        # Extend the cached path with one segment (O(1)).
        if self._lasso_path is None:
            self._lasso_path = QPainterPath()
            self._lasso_path.moveTo(sp)
        else:
            self._lasso_path.lineTo(sp)
        # Repaint ONLY the screen-space rect spanning the new segment.
        try:
            new_screen = self.scene_to_screen(sp)
            if self._lasso_last_screen is not None:
                x0, x1 = self._lasso_last_screen.x(), new_screen.x()
                y0, y1 = self._lasso_last_screen.y(), new_screen.y()
                pad = 6                     # pen width + a touch
                left   = int(min(x0, x1) - pad);  top    = int(min(y0, y1) - pad)
                right  = int(max(x0, x1) + pad);  bottom = int(max(y0, y1) + pad)
                self.update(left, top, right - left, bottom - top)
            else:
                self.update()                # first point — full repaint once
            self._lasso_last_screen = new_screen
        except Exception:
            self.update()                    # safety: degrade to full update

    def end_lasso(self):
        pts = list(self._lasso_points)
        self._lasso_active = False
        self._lasso_erase = False
        self._lasso_points.clear()
        self._lasso_path = None
        self._lasso_last_screen = None
        self.update()
        return pts

    # ---- Events ----
    @staticmethod
    def _is_synth_mouse(e) -> bool:
        """True when this mouse event was manufactured by the OS (or Qt) out
        of a touch/pen contact rather than produced by a physical mouse.

        Used ONLY to answer "does this machine have a hovering pointer?" —
        never to decide whether to draw. On the classroom board every single
        event is synthesised, so filtering on this would make the app inert."""
        try:
            return e.source() != Qt.MouseEventNotSynthesized
        except Exception:
            return False

    def _sync_eraser_cursor(self) -> None:
        """Recompute whether the eraser ring should be drawn for the tool
        that is in effect RIGHT NOW.

        For every ordinary tool this is just ``tool == "eraser"``, exactly as
        before. Dual Pen is the reason it can't stay a one-shot assignment in
        set_tool(): its effective tool depends on which stylus tip is in use,
        which changes mid-session, so a tip carrying the eraser drew no ring
        at all."""
        t = self._tool
        if t == "dual_pen":
            # getattr: set_tool() can run before _input_handler is attached.
            #
            # ``eraser_ring_active`` rather than ``_current_tool``: during a
            # stroke the handler dispatches every sample by the tool it
            # FROZE at press, so that is the tool the ring has to agree
            # with. Asking for the live one could disagree mid-stroke — the
            # contact erases while the ring says pen, or the reverse.
            try:
                h = getattr(self, "_input_handler", None)
                self._show_eraser_cursor = h is not None and h.eraser_ring_active()
            except Exception:
                self._show_eraser_cursor = False
        else:
            self._show_eraser_cursor = (t == "eraser")
        if getattr(self, "_eraser_mode", "pixel") == "lasso":
            self._show_eraser_cursor = False      # the lasso has no ring

    def mousePressEvent(self, e):
        if time.time() < self._ignore_mouse_until:
            self._note_input("mouse", f"press synth={self._is_synth_mouse(e)}",
                             acted=False)
            e.accept(); return
        self._note_input("mouse", f"press synth={self._is_synth_mouse(e)}")
        # THE ring fix. _cursor_pos used to be written by mouseMoveEvent and
        # nothing else on this path, so a TAP — press+release with no move in
        # between — never moved the ring anywhere near the contact. It sat at
        # the off-screen parking spot left by leaveEvent, i.e. invisible.
        #
        # Confirmed on the classroom board: the IR frame classifies contacts
        # ITSELF and routes them to different Windows digitizers — the thin
        # front tip arrives as TABLET events, a finger or the thick back tip
        # as TOUCH, and Windows additionally promotes a moving touch contact
        # to synthesised MOUSE partway through the stroke. So all three
        # families are live on one board, and every one of them has to keep
        # the ring honest. (An earlier comment here claimed a passive stylus
        # means no tablet events — wrong. The pen needs no electronics; the
        # frame does the classifying.)
        self._pointer_down = True; self._hover_moves = 0
        # List mode: whichever sheet you touch becomes the current page, so
        # every tool below works through current_scene()/current_stack() with
        # no knowledge that a list exists. Must happen BEFORE the handler
        # runs — it maps the press through screen_to_scene, which is relative
        # to the current page.
        self.focus_page_at(e.position())
        self._sync_eraser_cursor()
        self.move_eraser_cursor(e.position().toPoint())
        if self._input_handler: self._input_handler.mouse_press(e)
    def mouseMoveEvent(self, e):
        if time.time() < self._ignore_mouse_until:
            # THIS is the branch that made the HUD lie. The event is about to
            # be discarded, so it must not claim to be what's driving input.
            self._note_input("mouse", f"move btns={int(e.buttons().value)}",
                             acted=False)
            # These are the synthesised mouse events that trail a touch
            # stroke. They must not draw, and they must not touch the ring
            # either: the touch handler is already driving it from the real
            # contact stream, sample for sample, while this event carries a
            # position several samples behind. Letting both write it made
            # the ring jump between the finger and a point trailing it —
            # the "circle distorts when I move fast" report. Whichever of
            # the two wrote last also decided where to scrub, so they
            # scrubbed each other's boxes and left half-circles behind.
            #
            # This used to keep tracking here because the ring would
            # otherwise freeze for the whole 350 ms window. It no longer
            # can: every touch sample moves the ring, TouchEnd parks it, and
            # the idle watchdog parks it even if the release is dropped.
            e.accept(); return
        self._note_input("mouse", f"move btns={int(e.buttons().value)} "
                                  f"synth={self._is_synth_mouse(e)}")
        # A move with NO button held is a hover, and only a real mouse can
        # produce one. Latch it: it's what tells mouseReleaseEvent whether
        # leaving the ring on screen after a release is meaningful (mouse —
        # it will keep following the pointer) or a bug (touchscreen — nothing
        # will ever move it again, so it just sits there looking frozen).
        if e.buttons() == Qt.NoButton:
            # Self-heal: an IR touch frame that drops a contact without ever
            # sending the matching release would otherwise leave
            # ``_pointer_down`` stuck True, and with it a ring that leaveEvent
            # is no longer allowed to park. The button state on the very next
            # move settles the argument.
            self._pointer_down = False
            self._hover_moves += 1
            if (self._hover_moves >= self._HOVER_MOVES_TO_TRUST
                    and not self._is_synth_mouse(e)):
                self._hover_seen = True
        else:
            self._hover_moves = 0
        self._sync_eraser_cursor()
        # A touch contact that is live (or was, moments ago) owns the ring —
        # see _ring_owned_by_touch. Otherwise: a move with a button held is a
        # drag and always moves the ring; a BUTTONLESS move only means
        # something on hardware that actually hovers. On a touch-only board
        # the stray buttonless move Windows emits after a lift would
        # otherwise resurrect the ring at the lift-off point and leave it
        # there forever, since nothing will ever move it again.
        if not self._ring_owned_by_touch():
            if e.buttons() != Qt.NoButton or self._hover_seen:
                self.move_eraser_cursor(e.position().toPoint())
            elif self._cursor_pos.x() >= 0:
                self.move_eraser_cursor(None)
        if self._input_handler: self._input_handler.mouse_move(e)
    def mouseReleaseEvent(self, e):
        self._pointer_down = False; self._hover_moves = 0
        if time.time() < self._ignore_mouse_until: e.accept(); return
        if self._input_handler: self._input_handler.mouse_release(e)
        # Contact lifted. On a hovering pointer the ring keeps tracking the
        # mouse, so leave it exactly where it is. With no hover there is no
        # such thing as "the pointer is still there" — the stylus is off the
        # glass — and every later frame would redraw the ring at the last
        # contact point forever: the "circle gets stuck where I touched it"
        # report. Park it so it disappears cleanly on lift instead.
        if self._hover_seen and not self._is_synth_mouse(e):
            self.move_eraser_cursor(e.position().toPoint())
        else:
            self.move_eraser_cursor(None)
    def leaveEvent(self, e):
        # Don't blank the eraser ring while fingers are still down. A touch
        # panel fires Leave as the synthesised pointer jumps around during a
        # stroke, and blanking there made the ring vanish mid-erase — with
        # ``_ignore_mouse_until`` active, no mouse move could bring it back
        # either. Touch drives the ring itself (see InputHandler.touch_event)
        # and parks it when the last contact lifts.
        # ``_pointer_down`` covers the same hazard on the MOUSE path, which
        # the touch check alone missed: a board driven by mouse emulation has
        # no touch points to look at, and the synthesised pointer routinely
        # throws a Leave partway through a drag. That blanked the ring mid
        # erase and — with no hover to bring it back — for the rest of the
        # stroke. Only park the ring when nothing is actually touching.
        h = self._input_handler
        if not self._pointer_down and (h is None or not getattr(h, "_active_touches", None)):
            # move_eraser_cursor, not an assignment plus a full repaint: a
            # touch panel throws Leave repeatedly during a stroke, and
            # repainting the whole canvas on each one drops frames — which
            # is itself part of what makes a fast rub look torn. The ring's
            # own two boxes are all that changed.
            self.move_eraser_cursor(None)
    def resizeEvent(self, e):
        """Keep the scroll inside the content when the viewport changes size.

        A taller window means less to scroll, and the list style can go from
        'scrolls a long way' to 'fits entirely' in one drag of the window
        edge — leaving the old offset pointing past the end at a blank board.
        """
        super().resizeEvent(e)
        self._bump_layout()
        # Before clamp_scroll, which syncs the bar: the bar has to be the
        # right SIZE for the new viewport before it is told what range to
        # show, or it spends a frame stretched to the old height.
        self._position_scrollbar()
        self.clamp_scroll()

    def wheelEvent(self, e):
        # Ctrl + wheel = zoom, the convention every document viewer uses and
        # the fallback for trackpads whose driver sends wheel events rather
        # than a native pinch gesture.
        if (self._canvas_style == "scroll_list"
                and (e.modifiers() & Qt.ControlModifier)):
            steps = e.angleDelta().y() / 120.0
            if not steps:
                steps = e.pixelDelta().y() / 60.0
            if steps:
                factor = 1.0 + 0.12 * steps * self._list_zoom_sensitivity
                self._zoom_list_by(max(0.2, min(5.0, factor)),
                                   e.position().y())
            e.accept()
            return
        # Trackpads send pixelDelta; mouse wheels send angleDelta (120 units
        # per notch).
        pd = e.pixelDelta()
        # Shift + wheel = sideways, the convention every document viewer
        # uses. Without it a mouse-only machine has NO way to reach the far
        # side of a zoomed-in sheet: the sideways gesture needs two fingers,
        # and the scroll bar on the right only moves the view up and down.
        sideways = bool(e.modifiers() & Qt.ShiftModifier)
        if not pd.isNull():
            delta = -(pd.x() if sideways and pd.x() else pd.y())
        else:
            ad = e.angleDelta()
            steps = (ad.x() if sideways and ad.x() else ad.y()) / 120.0
            delta = -steps * 60.0  # ~60px per notch
        # A trackpad's own horizontal component counts as sideways whether or
        # not Shift is held — that IS the sideways gesture on a trackpad.
        if not sideways and not pd.isNull() and abs(pd.x()) > abs(pd.y()):
            sideways = True
            delta = -pd.x()
        # A wheel notch is the user taking the view over.
        self.stop_fling()
        moved = self.scroll_by_x(delta) if sideways else self.scroll_by(delta)
        if moved:
            e.accept()
        else:
            e.ignore()
    def _anchor_ref(self, x: float, y: float):
        """Describe a screen point as "this spot ON this sheet".

        Returns ``(page_index, frac_x, frac_y)`` where the fracs are the
        point's position across that sheet as a fraction of its size — so
        (0.5, 0.5) is the middle of the page whatever the zoom is.

        This exists because list space is NOT a scaled copy of the board.
        Sheet heights scale with the zoom but the gaps between them are a
        constant number of SCREEN pixels, so the two do not move together:
        dividing a list coordinate by the scale (what this used to do) leaves
        the accumulated gap error in the answer, and that error grows with the
        page index. Near the top of a board it is a few pixels; six pages
        down it was over half a page, which is enough to slide the sheet you
        were pinching out from under your fingers.

        A fraction OF A NAMED PAGE has no such error, because it is measured
        against the one rectangle that is actually being scaled.

        The fracs are deliberately not clamped to 0..1: an anchor in a gap, or
        past the last page, still gets a sensible answer relative to the
        nearest sheet, and the zoom stays continuous as the finger crosses a
        page boundary instead of jumping.
        """
        rects = self.page_layout()
        if not rects:
            return None
        ly = y + self._scroll_y
        lx = x + self._scroll_x
        best = None
        best_d = None
        for i, r in rects:
            if r.top() <= ly <= r.bottom():
                best, best_d = (i, r), -1.0
                break
            d = min(abs(ly - r.top()), abs(ly - r.bottom()))
            if best_d is None or d < best_d:
                best, best_d = (i, r), d
        if best is None:
            return None
        i, r = best
        h = max(1e-6, r.height())
        w = max(1e-6, r.width())
        return (i, (lx - r.left()) / w, (ly - r.top()) / h)

    def _restore_anchor(self, ref, x: float, y: float) -> None:
        """Put ``ref`` back under the screen point it came from."""
        if ref is None:
            return
        i, fx, fy = ref
        for j, r in self.page_layout():
            if j != i:
                continue
            want_y = r.top() + fy * r.height()
            want_x = r.left() + fx * r.width()
            self._scroll_y = max(0.0, min(want_y - y, self._max_scroll()))
            self._scroll_x = max(0.0, min(want_x - x, self._max_scroll_x()))
            return

    def _zoom_list_by(self, factor: float, anchor=None, anchor_x=None) -> None:
        """Scale the page list, keeping ``anchor`` (a screen y) put.

        Anchoring matters more here than for the board's touch gesture: a
        trackpad pinch happens with the pointer resting on a specific page,
        and zooming around the viewport origin instead would slide that page
        out from under it.

        ``anchor_x`` does the same for the horizontal offset, and only has
        anything to do once the sheet is wider than the window. Without it,
        zooming into a page that is already scrolled sideways drags the part
        you were looking at off the edge — the zoom would be anchored in one
        axis and not the other, which feels like the board fighting you.

        The anchor is remembered as a spot on a NAMED SHEET rather than as a
        list coordinate; see ``_anchor_ref`` for why that distinction is the
        difference between "stays under your fingers" and "drifts further the
        lower down the board you are".
        """
        if self._canvas_style != "scroll_list" or factor <= 0:
            return
        y = float(self.height()) / 2.0 if anchor is None else float(anchor)
        x = float(self.width()) / 2.0 if anchor_x is None else float(anchor_x)
        ref = self._anchor_ref(x, y)
        before = self._list_scale
        self.set_list_scale(self._list_scale * factor)
        if abs(self._list_scale - before) < 1e-9:
            return                      # clamped — leave the scroll alone
        self._restore_anchor(ref, x, y)
        self._cache_dirty = True
        # After the anchor moved the view, not before — set_list_scale synced
        # against the pre-anchor offset.
        self.sync_current_page_to_center()
        self.update()

    def zoom_list_at(self, factor: float, x=None, y=None) -> None:
        """Scale the page list about a point on SCREEN — the public pinch.

        Every pinch has to come through here rather than calling
        ``set_list_scale`` directly. set_list_scale changes the scale and
        nothing else, which means the list grows from list-space origin, i.e.
        the top of page one: pinch open half way down a long board and the
        view marches off towards the earlier pages, taking the sheet you were
        looking at with it. Anchoring is what makes a zoom stay where the
        fingers are.

        ``x`` / ``y`` default to the middle of the viewport, which is the
        sensible anchor when a caller has no pointer to speak of.
        """
        self._zoom_list_by(max(0.2, min(5.0, float(factor))), y, x)

    def _handle_native_gesture(self, ev) -> bool:
        """Trackpad pinch-to-zoom (a Windows precision-touchpad gesture).

        Qt delivers this as a NativeGesture with an incremental scale delta,
        NOT as touch points — a two-finger pinch on a laptop trackpad never
        reaches the widget as the touch contacts the board gesture reads, so
        without this there is no way to zoom the list on a laptop at all.

        List style only, for the same reason the touch zoom is: the one-page
        view's zoom behaviour is deliberately left alone.
        """
        if self._canvas_style != "scroll_list":
            return False
        try:
            if ev.gestureType() != Qt.ZoomNativeGesture:
                return False
            delta = float(ev.value())
        except Exception:
            return False
        if abs(delta) < 1e-6:
            return True
        # Same sensitivity knob as the touch gesture, so the setting means
        # one thing across every way of zooming the list.
        factor = 1.0 + delta * self._list_zoom_sensitivity
        try:
            anchor_x, anchor_y = ev.position().x(), ev.position().y()
        except Exception:
            anchor_x = anchor_y = None
        self.zoom_list_at(factor, anchor_x, anchor_y)
        return True

    def event(self, ev):
        if ev.type() == QEvent.NativeGesture:
            if self._handle_native_gesture(ev):
                ev.accept()
                return True
        if ev.type() in (QEvent.TouchBegin,QEvent.TouchUpdate,QEvent.TouchEnd,QEvent.TouchCancel):
            try:
                self._note_touch_geometry(ev.points())
                self._note_input(
                    "touch",
                    f"{ev.type().name.replace('Touch','')} n={self._touch_count} "
                    f"max={self._touch_max_wh[0]:.0f}x{self._touch_max_wh[1]:.0f}"
                    f" ({self._touch_max_area:.0f}px2)")
            except Exception:
                self._last_input_family = "touch"
            if self._input_handler: return self._input_handler.touch_event(ev)
            ev.accept(); return True
        return super().event(ev)
    def tabletEvent(self, e):
        # Track the eraser ring on the STYLUS path too. This was the last
        # place it could still get stuck: InputHandler.tablet_event
        # synthesises a QMouseEvent and calls InputHandler.mouse_move()
        # *directly*, which bypasses this widget's mouseMoveEvent — and
        # that was the only place moving _cursor_pos for non-touch input.
        # A smart board that reports as a pen digitizer therefore drove
        # the erase itself perfectly while the ring sat frozen wherever
        # the mouse pointer happened to be, or stayed parked off-screen
        # and invisible. Nothing about this reproduces with a mouse or
        # with synthetic touch, which is why it survived the last fix.
        #
        # NOTE for anyone debugging the classroom board: this path is DEAD
        # there. The stylus is a passive plastic stick — no digitizer, no
        # electronics — so Windows never raises a tablet event for it and no
        # amount of work here can affect that board. It stays for machines
        # with a genuine active pen.
        try:
            new_ring_pos = e.position().toPoint()
            self._note_input(f"tablet {e.pointerType().name}",
                             f"{e.type().name.replace('Tablet','')} "
                             f"press={e.pressure():.2f}")
        except Exception:
            new_ring_pos = None
            self._last_input_family = "tablet"
        et = e.type()
        if et == QEvent.TabletPress:
            self._pointer_down = True
            try:    self.focus_page_at(e.position())
            except Exception: pass
        elif et == QEvent.TabletRelease: self._pointer_down = False
        elif et == QEvent.TabletMove and not self._pointer_down:
            # Pen moving with the tip off the glass = proximity hover. Same
            # meaning as a buttonless mouse move: something will keep moving
            # the ring after release, so it's safe to leave it on screen.
            self._hover_seen = True
        if self._input_handler: self._input_handler.tablet_event(e)
        else: super().tabletEvent(e)
        # BEFORE the ring is moved, not after. _sync_eraser_cursor is what
        # decides whether the ring is drawn at all, and for Dual Pen its
        # answer depends on which tip is in use — which the handler above
        # has just worked out from this event's pointerType. Moving the ring
        # first meant the move was evaluated against the PREVIOUS tip: flip
        # the pen to the eraser end and the first thing it did was decline
        # to draw a ring, because as far as this line knew the front tool
        # was still in effect. The ring only caught up on the next event,
        # and on a quick dab there isn't one.
        self._sync_eraser_cursor()
        # A pen that is merely hovering still reports position, so unlike a
        # touchscreen there IS something to track after release — but only
        # while the pen stays in proximity. Qt stops sending tablet events
        # once it leaves, so park on release rather than freeze.
        if et == QEvent.TabletRelease and not self._hover_seen:
            self.move_eraser_cursor(None)
        elif new_ring_pos is not None:
            self.move_eraser_cursor(new_ring_pos)

    def _on_theme_changed(self, tokens):
        if HAS_ENGINE and self._board is not None:
            new_bg_qc = theme_mgr.qcolor(theme_mgr.tokens.canvas_bg_color)
            new_r, new_g, new_b = new_bg_qc.red(), new_bg_qc.green(), new_bg_qc.blue()
            # Fetch each page FRESH from the board (never trust cached Scene refs).
            for i in range(self._board.page_count):
                s = self._board.page_mut(i)
                old_bg = s.background_color
                s.background_color = lbe.RGBA(new_r, new_g, new_b, 255)
                self._flip_colors_for_theme(s, old_bg, s.background_color)
        self._cache_dirty = True; self.update()

    def _flip_colors_for_theme(self, scene, old_bg, new_bg):
        """When switching dark<->light, keep strokes visible:
          - A stroke that was pure black or pure white (the default ink colors)
            flips to the OTHER one so it stays readable on the new background.
          - A stroke colored like the OLD background (effectively an eraser /
            invisible cover) is recolored to the NEW background.
          - Other custom colors (blue, red, etc.) are left unchanged.
        Locked pixel-erase strokes are recolored to the new bg so they keep hiding."""
        if not HAS_ENGINE: return
        def similar(a, b, tol=40):
            return abs(a.r-b.r)<tol and abs(a.g-b.g)<tol and abs(a.b-b.b)<tol

        BLACK = lbe.RGBA(0,0,0,255); WHITE = lbe.RGBA(255,255,255,255)

        for i in range(scene.object_count):
            obj = scene.object_at(i)
            if not obj: continue
            # Imported media (images, ppt slides) must never be recolored.
            if obj.type_name in ("image", "ppt"):
                continue
            sc = obj.stroke_color

            # 1) Locked cover-up / bg-matching strokes → new bg
            if obj.locked or similar(sc, old_bg):
                obj.stroke_color = lbe.RGBA(new_bg.r, new_bg.g, new_bg.b, sc.a)
                continue
            # 2) Pure black ink → white, pure white ink → black
            if similar(sc, BLACK, 50):
                obj.stroke_color = lbe.RGBA(255,255,255, sc.a)
            elif similar(sc, WHITE, 50):
                obj.stroke_color = lbe.RGBA(0,0,0, sc.a)
            # 3) Everything else (colored ink) stays as-is

            # Highlighter/fill colors: also flip black/white fills
            try:
                fc = obj.fill_color
                if fc.a > 0:
                    if similar(fc, BLACK, 50):
                        obj.fill_color = lbe.RGBA(255,255,255, fc.a)
                    elif similar(fc, WHITE, 50):
                        obj.fill_color = lbe.RGBA(0,0,0, fc.a)
            except Exception:
                pass
    def mark_dirty(self):
        if not self._dirty: self._dirty = True; self.dirty_changed.emit(True)
        self._cache_dirty = True; self.update()
        self.content_changed.emit()

    def note_edited(self):
        """Flag the board as edited — WITHOUT re-rasterising the page.

        ``mark_dirty`` does three things: raise the unsaved-changes flag,
        throw away the scene cache, and repaint the whole widget. The first
        is bookkeeping; the other two are the most expensive operations this
        widget has, and the touch handler was calling all three on EVERY
        sample of every gesture — including moves that changed nothing at
        all, like an eraser passing over blank canvas.

        The effect on a board with real ink on it: a full re-raster of every
        object per input sample, which is exactly the cost the dirty-rect
        machinery in _pixel_eraser_extend and repair_cache_boxes exists to
        avoid. Frames start dropping, and a dropped frame during a fast rub
        is what makes the eraser ring look torn.

        Nothing that actually changes pixels needs this method — the live
        stroke, the erase overlay and the commit paths each invalidate their
        own region. This is only for "the document is no longer saved".
        """
        if not self._dirty: self._dirty = True; self.dirty_changed.emit(True)
        self.content_changed.emit()
    def clear_dirty(self):
        if self._dirty: self._dirty = False; self.dirty_changed.emit(False)
    @property
    def is_dirty(self): return self._dirty