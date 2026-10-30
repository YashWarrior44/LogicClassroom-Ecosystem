"""
input_handler.py — Input state machine for CanvasWidget.
"""
from __future__ import annotations
import logging, math, time
from typing import Optional, TYPE_CHECKING
from PySide6.QtCore import Qt, QPoint, QPointF, QRectF, QEvent
from PySide6.QtGui import QMouseEvent, QTabletEvent
from ui.canvas.touch_classifier import TouchFeatures
import ui.canvas.erase_geometry as eg
from ui.canvas import shape_geometry as sg

if TYPE_CHECKING:
    from ui.canvas.canvas_widget import CanvasWidget

try:
    import logicboard_engine as lbe
    HAS_ENGINE = True
except ImportError:
    lbe = None
    HAS_ENGINE = False

__all__ = ["InputHandler"]

HANDLE_HALF = 8.0
# Rotation grab handle — must match the values canvas_widget draws with.
ROTATE_HANDLE_OFFSET = 54.0
ROTATE_HANDLE_RADIUS = 19.0


class InputHandler:
    # ---- Gesture arbitration tunables ----
    # How far the contacts have to travel (or splay) before the gesture is
    # classified, in SCREEN pixels. Below this nothing happens at all: a
    # finger resting on a board is never perfectly still, and without a dead
    # zone that noise is a scroll of a pixel or two in a direction nobody
    # chose. Sized for a finger on a wall-mounted board, not for a mouse.
    _GESTURE_SLOP = 8.0
    # How much a pinch has to beat a pan by to win the classification.
    # Fingers rarely travel perfectly in parallel, so a two-finger swipe
    # always changes the spread a little; without the bias that stray
    # component can out-argue the swipe itself and the board zooms when the
    # user meant to scroll.
    _PINCH_BIAS = 1.35
    # ---- Momentum ----
    # How much recent history the release velocity is averaged over, in ms.
    # Averaged rather than taken from the last pair of samples, because the
    # final sample before a lift is the noisiest one there is: the finger is
    # already rolling off the glass. Too long a window and a flick that
    # started slow is under-reported; ~120 ms is about one deliberate flick.
    # The window the release velocity is averaged over, measured BACKWARDS
    # FROM THE RELEASE. Roughly one deliberate flick: long enough that a few
    # sparse samples still average sensibly, short enough that only what the
    # hand was doing at the end counts.
    _VELOCITY_WINDOW_MS = 120.0
    # Shortest span worth dividing by. Two samples a few milliseconds apart
    # turn one jittery pixel into a large velocity.
    _MIN_VELOCITY_SPAN_MS = 8.0
    #
    # There is deliberately NO "was the last sample recent enough" threshold
    # any more. There used to be one, and it was a proxy for the thing
    # _release_velocity now measures directly: with the window ending at the
    # release, idle time is already counted as time with no movement in it,
    # so a hand that stopped before letting go produces a small velocity and
    # a hand that stopped for longer produces none — continuously, with no
    # cliff for a gesture to land on the wrong side of. A threshold on top of
    # that could only ever disagree with it.

    def __init__(self, canvas: CanvasWidget):
        self._canvas = canvas
        self._drawing = False
        self._last_point = QPointF()
        self._current_pressure = -1.0
        self._erased_ids: set = set()
        # Widest half-thickness of any ink on the page, measured once when a
        # pixel-eraser drag starts. Cutting a stroke changes pixels up to a
        # half-width away from the eraser's own footprint, so this is how far
        # the dirty rect has to be padded. See _pixel_eraser_extend.
        self._live_erase_pad: float = 0.0
        # Is a PIXEL-eraser drag in flight from a touch contact (a palm, or a
        # finger with the eraser tool)? The pixel eraser is a press/extend/
        # commit sequence, unlike the object eraser which deletes outright on
        # every sample, so a touch erase now needs to be closed out on
        # TouchEnd. See _erase_touch_press. ``_touch_erase_id`` is the contact
        # driving it — exactly one may, since the mask is canvas-wide.
        self._touch_erasing: bool = False
        self._touch_erase_id = None
        self._touch_lassoing = False    # a finger is drawing a lasso-erase loop

        # Dual Pen — when canvas.tool == "dual_pen", press dispatch picks one
        # of these two depending on which physical tip is in use. Tablet
        # events set ``_active_tip`` from QTabletEvent.pointerType(); the rest
        # of the handler reads the effective tool via ``_current_tool()``.
        self._dual_front_tool: str = "pen"
        self._dual_back_tool:  str = "select"
        self._active_tip: str = "front"   # "front" | "back"
        # See InputCalibration.mouse_is_stylus_front — some boards emit a
        # plain left click for the stylus front tip instead of a touch.
        self._mouse_is_stylus_front: bool = False

        self._active_touches = {}
        self._touch_drawing = False
        # Which tool the CURRENT touch interaction started with, and the last
        # scene point it saw. Frozen at press for the same reason _active_tip
        # is: a press-time decision (begin a shape preview) and a release-time
        # decision (commit that shape) must not disagree because the tool
        # changed mid-stroke.
        self._touch_tool: str = ""
        self._touch_last_pt = QPointF()
        self._gesture_active = False
        self._scroll_gesture_active = False
        self._scroll_last_cy = 0.0
        self._scroll_last_cx = 0.0
        self._scroll_last_spread = 0.0
        # --- gesture arbitration -------------------------------------------
        # ONE action per touch sequence, decided once and then locked.
        #
        # Every sample used to be read on its own terms: the same event
        # scrolled vertically, scrolled sideways AND rescaled the list, each
        # from its own component of the same movement. A straight swipe up
        # the board therefore also drifted sideways by whatever the hand
        # happened to do and rescaled by whatever the fingers happened to
        # splay — so a scroll never came out as only a scroll, and the small
        # movement no hand can avoid read as the board twitching.
        #
        # "" = no gesture; "pending" = tracking, still inside the dead zone
        # and deliberately doing NOTHING; then one of "vertical",
        # "horizontal" or "zoom" for the rest of the sequence.
        self._gesture_kind = ""
        # Centroid and spread at the moment tracking began. Classification
        # measures against THIS, never against the previous sample: a verdict
        # taken from one sample-to-sample delta is a verdict taken from a
        # couple of pixels of jitter.
        self._gesture_origin = None
        # A single contact that landed OFF the sheets scrolls. Latched at
        # touch-down, because the finger can perfectly well be dragged onto a
        # page afterwards — and a gesture that turns into a pen halfway is
        # exactly the "it drew on my board while I was scrolling" bug.
        self._one_finger_scroll = False
        # (time, scroll_y, scroll_x) samples for the release velocity.
        # The SCROLL offset is recorded, not the finger position, and that
        # distinction does the work: samples spent inside the dead zone move
        # no offset and so contribute no velocity, and a drag already clamped
        # against the last page records no movement either — so a flick into
        # the end of the board cannot launch a glide into a wall.
        self._scroll_history = []
        # (the _gesture_start_dist / _scale / _center / _offset fields that
        # used to live here were pinch-zoom anchors — removed with it)

        self._shape_start = QPointF()
        self._shape_end = QPointF()

        self._selected_ids = []
        self._sel_mode = ""
        self._drag_start = QPointF()
        self._drag_last = QPointF()
        self._move_dx = 0.0; self._move_dy = 0.0
        # Resize state
        self._resize_handle = ""
        self._resize_orig_rect = None
        self._resize_old_transforms = {}
        self._resize_start_pt = QPointF()
        self._resize_anchor = QPointF()
        self._resize_accum_sx = 1.0
        self._resize_accum_sy = 1.0
        # Rotation state
        self._rotate_center = QPointF()
        self._rotate_last_angle = 0.0

    # ================================================================
    # Mouse
    # ================================================================
    def mouse_press(self, event, *, from_tablet: bool = False):
        if event.button() != Qt.LeftButton: return
        canvas = self._canvas
        # Same rule as a touch landing, and this is the path a STYLUS takes
        # (tablet_event synthesises a press and calls straight in here), so
        # without it a pen coming down on a coasting board draws a stroke
        # across a view that is still moving.
        canvas.stop_fling()
        # Nothing to draw on — a gap between sheets, or the margin beside
        # them. Guarded HERE rather than in the widget because tablet_event
        # synthesises a press and calls this directly, so this is the one
        # place every non-touch press passes through.
        if not canvas.accepts_input_at(event.position()):
            return
        # Guard and FOCUS belong together. The widget's mousePressEvent also
        # calls this (it has to, before it positions the eraser ring), but
        # tablet_event synthesises a press and calls this method directly —
        # so a stylus press guarded here while the current page was still
        # whatever the last contact touched, and the stroke went to the wrong
        # sheet at wrong coordinates. It is idempotent: same page, no-op.
        canvas.focus_page_at(event.position())
        # Boards that never report the stylus front tip as a touch contact
        # deliver it as a plain left click. When the user has told us that's
        # what's happening, a left press IS the front tip, so Dual Pen must
        # resolve to the FRONT tool rather than whichever tip happened to be
        # seen last. Without this the front tip would inherit the back tip's
        # tool after every flip of the pen.
        #
        # ``from_tablet`` exempts the synthetic press that ``tablet_event``
        # builds. A tablet event carries ``pointerType()`` — the digitizer
        # telling us outright which end of the stylus is down — and that is
        # strictly better evidence than a setting about what a MOUSE click
        # means. Without the exemption the override undid the tip that
        # tablet_event had just resolved, so a back-tip press ran the FRONT
        # tool while the moves and the release that followed ran the back
        # one: the stroke previewed as one tool and committed as another,
        # in the other tool's colour.
        if self._mouse_is_stylus_front and not from_tablet:
            self._active_tip = "front"
        scene_pt = canvas.screen_to_scene(event.position())
        # _current_tool() resolves Dual Pen to front_tool or back_tool depending
        # on which tip is in use. For every other tool it just returns canvas.tool.
        tool = self._current_tool()

        if tool == "eraser":
            self._drawing = True; self._erased_ids.clear()
            if canvas.eraser_mode == "pixel":
                # Pixel eraser — NEW live preview architecture.
                #
                # The old code did `canvas.begin_live_stroke(is_eraser=True)`,
                # which painted the canvas background colour onto the live
                # layer along the eraser path. That technique only worked on
                # an empty background; the moment the eraser passed over an
                # image or PPT slide, the bg-coloured stroke was drawn ON TOP
                # of the media — which is why media briefly turned black
                # while erasing and "came back" on release.
                #
                # The new architecture stores world-space capsule segments
                # in ``canvas._eraser_mask_segments`` and lets the freehand /
                # highlighter renderers clip themselves around the mask. No
                # paint is ever applied OVER the strokes or media. The
                # commit on release runs the same engine-side split logic as
                # before, producing one undo entry.
                canvas._eraser_mask_segments = []
                self._pixel_eraser_extend(scene_pt, first=True)
            elif canvas.eraser_mode == "lasso":
                canvas.begin_lasso(erase=True); canvas.add_lasso_point(scene_pt)
            else:
                self._do_erase_at(scene_pt)
        elif tool in ("pen", "highlighter", "brush"):
            self._drawing = True; self._last_point = scene_pt
            canvas.begin_live_stroke(is_highlighter=(tool == "highlighter"),
                                     is_brush=(tool == "brush"))
            canvas.add_live_point(scene_pt, self._current_pressure)
        elif tool in sg.SHAPE_TOOLS:
            self._drawing = True
            self._shape_start = scene_pt; self._shape_end = scene_pt
            canvas.begin_shape_preview(tool, scene_pt)
        elif tool == "select":
            self._handle_select_press(scene_pt)
        canvas.mark_dirty()

    def mouse_move(self, event):
        canvas = self._canvas
        scene_pt = canvas.screen_to_scene(event.position())
        tool = self._current_tool()

        if tool == "select":
            self._handle_select_move(scene_pt); return

        if not self._drawing: return
        if tool == "eraser":
            if canvas.eraser_mode == "pixel":
                self._pixel_eraser_extend(scene_pt)
            elif canvas.eraser_mode == "lasso":
                canvas.add_lasso_point(scene_pt)
            else:
                self._do_erase_at(scene_pt)
        elif tool in ("pen", "highlighter", "brush"):
            canvas.add_live_point(scene_pt, self._current_pressure)
            self._last_point = scene_pt
        elif tool in sg.SHAPE_TOOLS:
            self._shape_end = scene_pt
            canvas.update_shape_preview(scene_pt)

    def mouse_release(self, event):
        if event.button() != Qt.LeftButton: return
        canvas = self._canvas
        scene_pt = canvas.screen_to_scene(event.position())
        tool = self._current_tool()

        if tool == "select":
            self._handle_select_release(scene_pt); return

        if not self._drawing: return
        self._drawing = False
        if tool == "eraser":
            if canvas.eraser_mode == "pixel":
                self._commit_pixel_erase()
            elif canvas.eraser_mode == "lasso":
                self._erase_in_lasso(canvas.end_lasso())
            self._erased_ids.clear()
        elif tool in ("pen", "highlighter", "brush"):
            self._commit_stroke(tool)
        elif tool in sg.SHAPE_TOOLS:
            self._commit_shape(tool)
        canvas.update()

    # ================================================================
    # Tablet
    # ================================================================
    # ----------------------------------------------------------------
    # Dual-Pen helpers
    # ----------------------------------------------------------------
    def set_dual_pen_assignment(self, front: str, back: str) -> None:
        """Persistent configuration: which logical tool each tip uses when
        the active canvas tool is ``"dual_pen"``. Called from main_window
        after the user picks tools in the Pen panel."""
        VALID = {"pen", "highlighter", "brush", "select", "eraser"}
        if front in VALID: self._dual_front_tool = front
        if back  in VALID: self._dual_back_tool  = back

    def set_mouse_is_stylus_front(self, enabled: bool) -> None:
        """Treat a left click as the stylus FRONT tip (see the attribute
        comment in ``__init__``). Called from main_window when the setting
        loads and whenever the user toggles it."""
        self._mouse_is_stylus_front = bool(enabled)

    def eraser_ring_active(self) -> bool:
        """Should the eraser ring be on screen for what is happening NOW?

        During a touch stroke the answer is the tool frozen at press
        (``_touch_tool``) — the one every sample of that stroke is actually
        dispatched by — and outside a stroke it's the live tool. Reading the
        live tool during a stroke lets the ring disagree with the contact:
        flip the pen mid-rub and the tip changes under a stroke that is
        still, correctly, erasing.
        """
        if getattr(self._canvas, "eraser_mode", "pixel") == "lasso":
            return False            # the lasso eraser draws a loop, no ring
        if self._touch_drawing and self._touch_tool:
            return self._touch_tool == "eraser"
        return self._current_tool() == "eraser"

    def _current_tool(self) -> str:
        """Effective tool for THIS stroke.

        For regular tools (pen / highlighter / select / eraser / shapes) this
        is just ``canvas.tool``. For ``"dual_pen"`` it picks front vs back
        based on which stylus tip last touched the screen — no manual switch
        required, the user just flips the pen.

        Critically: callers must use this instead of reading ``canvas.tool``
        directly in any code path that runs *between* press and release —
        otherwise a press-time decision (e.g. begin lasso) would diverge from
        a move-time decision (e.g. add lasso point) if the user flipped tips
        mid-stroke. We freeze the tip at press time via ``_active_tip``."""
        t = self._canvas.tool
        if t != "dual_pen":
            return t
        return self._dual_back_tool if self._active_tip == "back" else self._dual_front_tool

    def tablet_event(self, event):
        self._current_pressure = event.pressure()
        # Identify the physical tip in use. Most digitizers report the back
        # end of a stylus as ``Pointer.Eraser`` (legacy WinTablet contract),
        # which is what we key off here. Done BEFORE the event is dispatched
        # so _current_tool() reads the right tip from the very first press.
        try:
            pt = event.pointerType()
            from PySide6.QtGui import QPointingDevice
            self._active_tip = "back" if pt == QPointingDevice.PointerType.Eraser else "front"
        except Exception:
            self._active_tip = "front"
        t = event.type()
        emap = {QEvent.TabletPress: QEvent.MouseButtonPress,
                QEvent.TabletMove: QEvent.MouseMove,
                QEvent.TabletRelease: QEvent.MouseButtonRelease}
        fake = QMouseEvent(emap.get(t, QEvent.MouseMove), event.position(),
                           Qt.LeftButton, Qt.LeftButton, event.modifiers())
        if t == QEvent.TabletPress: self.mouse_press(fake, from_tablet=True)
        elif t == QEvent.TabletMove: self.mouse_move(fake)
        elif t == QEvent.TabletRelease: self.mouse_release(fake); self._current_pressure = -1.0
        event.accept()

    # ================================================================
    # Touch
    # ================================================================
    def touch_event(self, event):
        canvas = self._canvas; points = event.points(); etype = event.type()
        # Touch mode decides what a palm verdict MEANS:
        #   palm_eraser    → palm erases.
        #   palm_rejection → palm is ignored entirely (skip the point).
        # Hoisted out of the per-point loop because the gesture test below
        # needs it too.
        palm_is_eraser = (getattr(canvas, "_touch_mode", "palm_rejection")
                          == "palm_eraser")
        if etype == QEvent.TouchBegin:
            # A contact landing stops a coasting board DEAD, before anything
            # else looks at the event. This is the most important line in the
            # momentum feature: on a phone, putting a finger on a moving list
            # stops it, and without that the board keeps sliding out from
            # under a pen that has already started its stroke.
            canvas.stop_fling()
            self._active_touches.clear(); self._touch_drawing = False
            self._gesture_active = False; self._erased_ids.clear()
            # A fresh sequence starts with NOTHING carried over — in
            # particular no locked gesture kind, or this swipe would inherit
            # the last one and skip its own classification.
            self._end_gesture()
        # Only contacts that are ALLOWED TO ACT count toward "is this a
        # two-finger gesture?". A palm rejected by palm_rejection mode is
        # still parked in ``_active_touches`` (tagged "ignored") so the
        # released-state bookkeeping stays sane — but counting it here
        # meant a hand resting on the board plus one erasing finger came
        # to 2 contacts and got routed into ``_handle_gesture`` as a
        # pinch. Erasing stopped mid-stroke and the eraser ring froze
        # where it was, because the gesture path never touches
        # ``_cursor_pos``. That needs a real palm to reproduce, which is
        # why it shows up on the smart board and never on a desk.
        live = sum(1 for d in self._active_touches.values()
                   if d.get("tool") != "ignored")
        # Contacts going down in THIS event that aren't registered yet. Only
        # the first finger of a sequence arrives as TouchBegin — every one
        # after it comes as a TouchUpdate carrying a Pressed point — so a test
        # that only looked at TouchBegin could not see the second finger land.
        # It took another whole event before ``live`` caught up, and the first
        # finger drew ink for all of it.
        arriving = 0
        for tp in points:
            if self._tstate(tp.state()) == "Released":
                continue
            if tp.id() in self._active_touches:
                continue
            if palm_is_eraser or not self._is_palm(tp):
                arriving += 1
        # LATCHED, not recomputed per event. Two fingers down at any moment
        # makes the WHOLE sequence a gesture, until every one of them lifts.
        #
        # Deciding it afresh each event is what put broken strokes on the
        # board during a two-finger scroll: fingers do not land together and
        # they do not leave together, so the count dips under two on the way
        # in and on the way out, and each dip handed the remaining contact
        # back to the drawing path. It would start a live stroke, collect
        # points from a hand that was only trying to scroll, and commit them
        # when the last finger came up. A heavy background made it worse
        # rather than causing it — slower frames mean more input samples
        # inside the same dip.
        if live + arriving >= 2:
            self._gesture_active = True
        # ONE finger scrolls too — but only if it came down OFF the sheets.
        #
        # That space was already dead: ``accepts_input_at`` parks a contact
        # in the margins or in a gap between sheets as "ignored" so it cannot
        # draw ink outside a page, so nothing is being taken away from any
        # existing tool here. On the sheet a single finger still means the
        # pen, exactly as before.
        #
        # Latched at touch-DOWN and never re-tested. Where the finger landed
        # is what the user chose; where it has been dragged to since is not,
        # and a gesture allowed to change its mind halfway is a scroll that
        # starts drawing the moment it crosses onto a page.
        if (etype == QEvent.TouchBegin and not self._gesture_active
                and live + arriving == 1):
            for tp in points:
                if self._tstate(tp.state()) == "Released":
                    continue
                if self._is_palm(tp) and not palm_is_eraser:
                    continue          # a resting palm is not a scroll
                if not canvas.accepts_input_at(tp.position()):
                    self._one_finger_scroll = True
                    self._gesture_active = True
                break
        if self._gesture_active:
            return self._handle_gesture(event)
        for tp in points:
            pos = tp.position(); scene_pt = canvas.screen_to_scene(pos)
            state = self._tstate(tp.state()); is_palm = self._is_palm(tp)
            if is_palm and self._is_dual_pen_tip_contact(state):
                # In Dual Pen the contact starting a stroke is a stylus TIP,
                # and the back tip is a big blunt contact by its nature — the
                # same thing a palm looks like to a frame that only measures
                # size. Judging it a palm throws away the tool the user
                # deliberately assigned to that tip: set the back tip to Pen,
                # press firmly enough to cross the palm threshold, and it
                # erases (palm_eraser) or does nothing (palm_rejection)
                # instead of drawing.
                #
                # Only the contact that OPENS the interaction gets this. A
                # second contact landing while a tip is already drawing is
                # what palm handling is actually for — a hand resting on the
                # board — and it still goes through the palm path.
                is_palm = False
                self._active_tip = "back"
            if is_palm and not palm_is_eraser:
                # Palm rejection: swallow this contact so it neither draws nor
                # erases. Mark it so released-state bookkeeping stays sane.
                if state == "Pressed":
                    self._active_touches[tp.id()] = {"pos": scene_pt, "tool": "ignored"}
                elif state == "Released":
                    self._active_touches.pop(tp.id(), None)
                continue
            if state == "Pressed":
                # A contact that landed on no sheet at all (the gap between
                # pages, or the margin) is parked as "ignored" — the same
                # bookkeeping palm rejection uses, so released-state
                # accounting stays sane — and never reaches a tool.
                if not canvas.accepts_input_at(pos):
                    self._active_touches[tp.id()] = {"pos": scene_pt,
                                                     "tool": "ignored"}
                    continue
                # List mode: the sheet under the finger becomes the current
                # page before anything maps coordinates. scene_pt was computed
                # against the PREVIOUS page, so it has to be recomputed.
                if canvas.focus_page_at(pos):
                    scene_pt = canvas.screen_to_scene(pos)
                self._active_touches[tp.id()] = {"pos": scene_pt, "tool": "palm" if is_palm else "normal"}
                # Dual Pen: work out WHICH TIP this contact is before anything
                # reads the effective tool — the eraser ring's visibility is
                # one of those readers.
                #
                # This is why the ring never appeared for a back tip set to
                # Eraser. ``_show_eraser_cursor`` is recomputed by
                # _sync_eraser_cursor, which the mouse and tablet paths call
                # on every event — but the touch path never did. On this board
                # the back tip arrives as a TOUCH contact (the front tip comes
                # in as a tablet event; see _update_tip_from_touch), so the
                # flag kept whatever value it had when the tool was chosen,
                # i.e. computed against the FRONT tip's tool. Erasing worked
                # perfectly — _current_tool() was right all along — but the
                # ring stayed invisible, so you were rubbing blind.
                #
                # Guarded by ``not self._touch_drawing`` so the tip stays
                # frozen for the whole stroke, exactly as it was when this
                # lived further down.
                if canvas.tool == "dual_pen" and not self._touch_drawing:
                    self._update_tip_from_touch(tp)
                    canvas._sync_eraser_cursor()
                # Snap the eraser ring to the finger the moment it lands.
                # Only the "Updated" branch used to do this, so the first
                # frame of every erase drew the ring at wherever the mouse
                # last was — and a TAP to rub out one blob produces no
                # Updated event at all, so the ring never moved: it either
                # sat frozen somewhere else or stayed invisible at the
                # off-screen parking spot. Both of the symptoms reported.
                #
                # AFTER the sync above, so the flag it consults is the one
                # for the tip that just landed.
                canvas.move_eraser_cursor(pos.toPoint())
                if is_palm: self._erase_touch_press(scene_pt, tp.id(), palm=True); canvas._palm_erasing = True
                elif not self._touch_drawing:
                    self._touch_drawing = True
                    # Dispatch by tool, exactly as mouse_press does. This used
                    # to be "eraser erases, EVERYTHING ELSE begins a freehand
                    # stroke" — so drawing with a finger while the Select or
                    # any shape tool was active painted a live-preview stroke
                    # that was never committed and never ended. It stayed on
                    # screen as a ghost the eraser could not touch (it is not
                    # a scene object at all), and vanished only when the next
                    # touch called begin_live_stroke() and cleared the buffer.
                    #
                    # The stylus tip was resolved above, before the ring was
                    # positioned — _current_tool() reads _active_tip, so a
                    # stale tip here would pick the wrong tool for the entire
                    # stroke.
                    tool = self._current_tool()
                    self._touch_tool = tool
                    self._touch_last_pt = scene_pt
                    if tool == "eraser":
                        self._erase_touch_press(scene_pt, tp.id())
                    elif tool in ("pen", "highlighter", "brush"):
                        canvas.begin_live_stroke(
                            is_highlighter=(tool == "highlighter"),
                            is_brush=(tool == "brush"))
                        canvas.add_live_point(scene_pt)
                    elif tool in sg.SHAPE_TOOLS:
                        self._shape_start = scene_pt; self._shape_end = scene_pt
                        canvas.begin_shape_preview(tool, scene_pt)
                    elif tool == "select":
                        self._handle_select_press(scene_pt)
            elif state == "Updated":
                d = self._active_touches.get(tp.id(), {"pos": scene_pt, "tool": "normal"})
                self._active_touches[tp.id()] = d
                if d.get("tool") == "ignored":
                    d["pos"] = scene_pt; continue
                # A contact admitted as a TIP stays a tip for its whole life.
                # The back tip's footprint grows as the user presses harder,
                # so a stroke that started correctly could cross the palm
                # threshold halfway along and turn into an erase under the
                # user's hand — mid-word. The press-time decision is the one
                # that counts, exactly as _touch_tool freezes the tool.
                if is_palm and not (canvas.tool == "dual_pen"
                                    and d.get("tool") == "normal"):
                    d["tool"] = "palm"
                if d["tool"] == "palm": self._erase_touch_move(scene_pt, tp.id(), palm=True); canvas._palm_erasing = True
                else:
                    # Mirror the press-time dispatch, driven by the tool this
                    # interaction STARTED with (see _touch_tool).
                    tool = self._touch_tool or self._current_tool()
                    self._touch_last_pt = scene_pt
                    if tool == "eraser":
                        self._erase_touch_move(scene_pt, tp.id())
                    elif tool in ("pen", "highlighter", "brush"):
                        canvas.add_live_point(scene_pt)
                    elif tool in sg.SHAPE_TOOLS:
                        self._shape_end = scene_pt
                        canvas.update_shape_preview(scene_pt)
                    elif tool == "select":
                        self._handle_select_move(scene_pt)
                # Dual Pen: keep the ring's VISIBILITY in step with the tip
                # for the whole stroke, not just its position. The press
                # syncs it, but a contact whose first frame carried no
                # usable ellipse classifies as "unknown" — the tip stays
                # whatever it was, and the ring is decided against the wrong
                # tool. Re-syncing per sample costs two attribute reads and
                # settles it as soon as the frame reports a real contact.
                if canvas.tool == "dual_pen":
                    canvas._sync_eraser_cursor()
                # move_eraser_cursor, not a bare assignment: the ring has to
                # scrub the box it came FROM or a fast rub leaves the previous
                # circle behind next to the new one.
                d["pos"] = scene_pt; canvas.move_eraser_cursor(pos.toPoint())
            elif state == "Released":
                # If the contact driving a pixel rub lifts while others stay
                # down, bank it now rather than waiting for the whole sequence
                # to end — the rub is over, and until it commits the cut ink is
                # only a preview.
                self._erase_touch_end(tp.id())
                self._active_touches.pop(tp.id(), None)
        canvas._palm_erasing = any(d["tool"] == "palm" for d in self._active_touches.values())
        if etype in (QEvent.TouchEnd, QEvent.TouchCancel):
            # Close out a pixel erase FIRST. Until this runs, the ink the rub
            # cut is only a live preview — the scene still holds the originals
            # and the mask is what hides them — so a touch sequence that ended
            # without it would put every erased stroke back the moment the mask
            # was dropped. The object eraser has no equivalent step, which is
            # why the touch path never had one.
            self._erase_touch_end()
            if self._touch_drawing:
                tool = self._touch_tool or self._current_tool()
                if tool in ("pen", "highlighter", "brush"):
                    # Pass the tool frozen at PRESS, so a tip flipped mid
                    # stroke can't change what the stroke commits as.
                    self._commit_stroke(tool)
                elif tool in sg.SHAPE_TOOLS:
                    self._commit_shape(tool)
                elif tool == "select":
                    self._handle_select_release(self._touch_last_pt)
                self._touch_drawing = False
                self._touch_tool = ""
            # Belt-and-braces: NO live stroke may outlive a touch sequence.
            # Every ghost-stroke bug in this file has been some path that
            # began one and had no matching commit/end; this makes that
            # class of bug impossible rather than fixing them one at a time.
            if getattr(canvas, "_live_active", False):
                canvas.end_live_stroke()
            self._active_touches.clear(); canvas._ignore_mouse_until = time.time()+0.35
            canvas._palm_erasing = False; self._erased_ids.clear()
            # Park the ring off-screen when the last finger lifts. Leaving
            # it where it was is what made it look "stuck": touch has no
            # hover, so nothing would ever move it again — and
            # ``_ignore_mouse_until`` above deliberately blocks mouse
            # updates for the next 350 ms on top of that. A ring sitting
            # frozen where you finished erasing reads as a broken app.
            # With a mouse also connected, the next mouse move brings it
            # straight back.
            #
            # Parking has to REPAINT the box it was parked from, or the ring
            # is only logically gone: the pixels stay lit until something
            # else happens to repaint that part of the canvas.
            canvas.move_eraser_cursor(None)
        canvas._ignore_mouse_until = time.time()+0.35
        # note_edited, not mark_dirty: this runs on EVERY touch sample, and
        # mark_dirty drops the scene cache and repaints the whole widget.
        # That undid the tight dirty rects the eraser and live stroke compute
        # a few lines earlier, re-rasterising the page once per sample — the
        # dropped frames are what make a fast rub look torn. Everything here
        # that actually changes pixels has already invalidated its own
        # region; all that's left to record is "this board has changes".
        canvas.note_edited(); event.accept(); return True

    def _classify_gesture(self, cx: float, cy: float, spread: float,
                          n: int) -> None:
        """Decide once what this touch sequence IS, then never revisit it.

        Measured from where the gesture began, not from the previous sample.
        A verdict taken from one sample-to-sample delta is a verdict taken
        from a couple of pixels of jitter, and it flips every few frames —
        which is what made a straight swipe scroll a bit, drift sideways a
        bit and rescale a bit, all at once.

        Nothing happens until the movement clears ``_GESTURE_SLOP``. Staying
        in "pending" is a real state, not a fallback: a hand resting on the
        board is never perfectly still, and acting on that noise is the
        "it scrolls when I am not scrolling" complaint.

        Then the LARGER movement wins outright — pinch against pan, and
        within a pan, vertical against horizontal. Comparing magnitudes is
        the whole trick; reading the direction of the last little movement is
        what a jittering classifier does.
        """
        if self._gesture_origin is None:
            return
        ox, oy, ospread = self._gesture_origin
        dx = cx - ox
        dy = cy - oy
        pan = math.hypot(dx, dy)
        # Doubled because ``spread`` is the mean distance from the centroid:
        # for two contacts that is half the distance between them, so the gap
        # between the fingers — the thing a person thinks of as the pinch —
        # changes by twice as much. Without the doubling a pinch is measured
        # at half its real size and loses to pans it should beat.
        #
        # One contact cannot pinch, at all. That is what guarantees a plain
        # one-finger swipe can only ever scroll.
        pinch = abs(spread - ospread) * 2.0 if n >= 2 else 0.0
        if max(pan, pinch) < self._GESTURE_SLOP:
            return                      # still inside the dead zone
        if pinch > pan * self._PINCH_BIAS:
            self._gesture_kind = "zoom"
        elif abs(dy) >= abs(dx):
            self._gesture_kind = "vertical"
        else:
            self._gesture_kind = "horizontal"

    def _note_scroll_sample(self) -> None:
        """Record where the scroll offset is, for the release velocity.

        Trimmed by TIME rather than to a fixed number of samples: digitisers
        report anywhere between 60 and 300 Hz, and a fixed-length history
        would cover half a second on one board and a twentieth of a second on
        another — i.e. the fling would feel like a different feature
        depending on the hardware.

        One sample OUTSIDE the window is kept on purpose. The window is
        measured backwards from the release (see ``_release_velocity``), and
        a reference point that has already been discarded cannot be measured
        from.
        """
        canvas = self._canvas
        now = time.time() * 1000.0
        self._scroll_history.append((now, canvas._scroll_y, canvas._scroll_x))
        cutoff = now - self._VELOCITY_WINDOW_MS
        # Drop while the SECOND sample is still old enough to serve as the
        # reference — that keeps exactly one sample from before the window
        # and everything inside it.
        while len(self._scroll_history) > 2 and self._scroll_history[1][0] < cutoff:
            self._scroll_history.pop(0)

    def _release_velocity(self, now: float) -> tuple:
        """Scroll velocity (px/ms) over the window ENDING AT ``now``.

        The window ends at the RELEASE, not at the last sample, and that is
        the whole fix. It used to be measured between the first and last
        samples in the history — which meant the time a finger spent sitting
        still before letting go was invisible to the sum. A digitiser stops
        reporting when nothing moves, so "drag, stop, release" left a history
        full of nothing but the earlier movement, and the board was handed
        the velocity from BEFORE the stop: a flick that the user had already
        cancelled, arriving a beat late. That is the "it twitches and then
        keeps going" report exactly.

        Measuring to ``now`` instead makes the idle time count as what it is
        — time with no movement in it. Hold still for half the window and the
        velocity halves; hold still for the whole window and it is zero, with
        no threshold anywhere deciding that. The behaviour is continuous, so
        there is no cliff for a gesture to land on the wrong side of.

        Returns ``(vy, vx)``, measured independently: each axis is the
        displacement of ITS OWN offset over the same span, so a sideways drag
        cannot manufacture vertical momentum out of the other axis's noise.
        """
        hist = self._scroll_history
        if len(hist) < 2:
            return (0.0, 0.0)
        start = now - self._VELOCITY_WINDOW_MS
        # The oldest sample still inside the window. If every sample is older
        # than that, the finger has been still for the whole window — and
        # falling back to the newest sample makes the displacement zero,
        # which is the right answer rather than a special case.
        ref = None
        for s in hist:
            if s[0] >= start:
                ref = s
                break
        if ref is None:
            ref = hist[-1]
        dt = now - ref[0]
        if dt < self._MIN_VELOCITY_SPAN_MS:
            # Too short a span to divide by: two samples a few ms apart turn
            # one jittery pixel into a large velocity.
            return (0.0, 0.0)
        last = hist[-1]
        return ((last[1] - ref[1]) / dt, (last[2] - ref[2]) / dt)

    def _launch_fling(self) -> None:
        """Hand the canvas the velocity the view was moving at on release."""
        canvas = self._canvas
        if self._gesture_kind not in ("vertical", "horizontal"):
            # Only a locked pan hands momentum over. A zoom has no scroll
            # velocity to speak of, and a gesture still sitting in the dead
            # zone never moved the board at all.
            return
        vy, vx = self._release_velocity(time.time() * 1000.0)
        # ONLY along the axis this gesture was locked to. The other axis is
        # not merely small, it is meaningless — the scroll path never applied
        # anything to it, so whatever it reads is the clamp or nothing. This
        # is what keeps a sideways flick from also drifting the board
        # downwards, and it is the same lock the drag itself obeyed.
        if self._gesture_kind == "vertical":
            canvas.start_fling(vy=vy)
        else:
            canvas.start_fling(vx=vx)

    def _end_gesture(self) -> None:
        """Close a touch sequence out cleanly.

        One place, called from every path that can end one, because a gesture
        that ends with half its state still set is a gesture that leaks into
        the next one — a locked kind left behind would make the NEXT swipe
        skip its own classification and inherit this one.
        """
        self._scroll_gesture_active = False
        self._gesture_kind = ""
        self._gesture_origin = None
        self._one_finger_scroll = False
        self._scroll_history.clear()

    def _handle_gesture(self, event):
        """Multi-touch gestures: two-finger scroll/zoom, and swallowing
        any other multi-finger contact so it can't draw.

        PINCH-ZOOM IS DELIBERATELY GONE. Two fingers used to rescale the
        view; on a classroom smart board that fired constantly by accident
        — a second knuckle, a sleeve, two students at the board — and left
        the canvas at some arbitrary zoom with no visible control to undo
        it. ``canvas.set_view`` / ``view_scale`` still exist and every
        render path still honours them, so the transform machinery is
        intact if zoom is ever wanted back behind a real UI control;
        nothing in the touch path calls them any more, which means the
        scale simply stays 1.0.
        """
        canvas = self._canvas; points = event.points()
        # Suppress the synthesised mouse stream for the WHOLE gesture, not
        # just its last event. This was armed on every sample of the drawing
        # path and only on TouchEnd here, so during a two-finger sweep the
        # mouse path ran at full strength: it saw the eraser tool selected,
        # moved the ring to the synthesised pointer and repainted it there,
        # once per sample, while this function was parking the ring in
        # between. Two writers, each scrubbing its own idea of the previous
        # position, so neither scrubbed the pixels that were really lit —
        # an arc of the ring's colour left behind at every sample. That is
        # the trail the two-finger erase leaves, and why parking the ring
        # here (below) did not on its own get rid of it.
        canvas._ignore_mouse_until = time.time() + 0.35
        # A second contact can arrive mid-rub — a knuckle, a sleeve, a second
        # student — and route the whole sequence here. Bank whatever the rub
        # cut before that happens: the user watched that ink disappear, and a
        # pixel erase that is never committed silently puts it all back. This
        # also drops the mask, which would otherwise keep hiding those strokes
        # for the rest of the gesture.
        #
        # An UNFINISHED lasso is the opposite case: nothing has been erased
        # yet, and committing a half-drawn loop would erase whatever it
        # happened to cover. A gesture taking over cancels it instead.
        self._erase_touch_end(cancel_lasso=True)
        # Nothing is erasing during a gesture, so the ring must not be left
        # hanging at the last erase point for the whole gesture — this path
        # never updates ``_cursor_pos``, so whatever it held would freeze.
        #
        # And it must be repainted away, not just moved: a gesture erases
        # nothing, so no dirty rect is coming from anywhere else. A bare
        # assignment here is what left an arc of the ring's colour behind at
        # every sample of a two-finger sweep.
        canvas.move_eraser_cursor(None)
        active = [tp.position() for tp in points if self._tstate(tp.state()) != "Released"]

        # Whatever the first finger started before the others landed is
        # ABANDONED, not committed — the user meant a gesture, not ink. This
        # has to happen before the scroll branch below, not after it: the
        # scroll branch returns early, so a hand that went from one contact
        # straight to two left a live stroke running for the whole scroll,
        # still collecting points, to be committed as a broken stroke when the
        # last finger came up.
        if self._touch_drawing or getattr(canvas, "_live_active", False):
            canvas.end_live_stroke()
            self._touch_drawing = False
            self._touch_tool = ""
        # Same for a shape the first contact may have begun.
        if getattr(canvas, "_shape_active", False):
            canvas.end_shape_preview()

        # --- the scroll / zoom gesture ----------------------------------------
        # One action, chosen once and then locked for the rest of the
        # sequence. What runs here is decided by ``_gesture_kind``; the
        # movement that decides it is measured by ``_classify_gesture``.
        #
        # Two contacts is the whole gesture. It used to need five, which
        # nobody discovers on their own. ONE contact also gets in, but only
        # when it landed off the sheets — see ``_one_finger_scroll``.
        #
        # ``_gesture_kind`` keeps the branch alive once a pan has been locked
        # in, so lifting one finger of a two-finger scroll carries on
        # scrolling with the other rather than dropping the gesture on the
        # floor halfway through — which is what a phone does, and what the
        # hand expects.
        scrolling = (len(active) >= 2
                     or self._one_finger_scroll
                     or self._gesture_kind in ("vertical", "horizontal", "zoom"))
        if active and scrolling:
            cy = sum(p.y() for p in active) / len(active)
            cx = sum(p.x() for p in active) / len(active)
            # Mean distance from the centroid — the contacts' "spread".
            # With two fingers this is exactly half the distance between
            # them, i.e. the classic pinch measure. Written as a mean over
            # ALL contacts rather than as one pair's distance so a third
            # finger (or a knuckle) joining mid-gesture widens the measure
            # smoothly instead of jumping the zoom.
            spread = 0.0
            for p in active:
                spread += math.hypot(p.x() - cx, p.y() - cy)
            spread /= len(active)
            if not self._scroll_gesture_active:
                # First sample of the gesture — or the first after the contact
                # count dipped. Remember where it started and do NOTHING with
                # this sample: there is no previous position to measure a
                # delta against, and acting on one would be acting on noise.
                self._scroll_gesture_active = True
                # Only ever open the question once. Re-seeding a kind that is
                # already locked is what would let the classification change
                # mid-gesture, which is the thing this whole mechanism exists
                # to prevent.
                if not self._gesture_kind:
                    self._gesture_kind = "pending"
                    self._gesture_origin = (cx, cy, spread)
                self._scroll_last_cy = cy
                self._scroll_last_cx = cx
                self._scroll_last_spread = spread
                self._scroll_history.clear()
                self._note_scroll_sample()
            else:
                if self._gesture_kind == "pending":
                    self._classify_gesture(cx, cy, spread, len(active))
                # Deltas are always measured from the PREVIOUS sample, never
                # from the origin, so the board tracks the finger continuously
                # instead of jumping to a position computed from where the
                # gesture began.
                if self._gesture_kind == "vertical":
                    # Finger up => content scrolls down. The sideways
                    # component of the same movement is deliberately dropped:
                    # this control is scrolling vertically and nothing else
                    # until the hand comes off the glass.
                    canvas.scroll_by(self._scroll_last_cy - cy)
                elif self._gesture_kind == "horizontal":
                    canvas.scroll_by_x(self._scroll_last_cx - cx)
                elif self._gesture_kind == "zoom":
                    # LIST style only. One-page zoom is deliberately
                    # untouched: its scale is a different field and the
                    # sensitivity setting must not reach it. A pinch there
                    # therefore does nothing at all, which is correct — a
                    # pinch is not a scroll, and silently panning instead
                    # would be the board guessing.
                    if (getattr(canvas, "canvas_style", "one_page") == "scroll_list"
                            and self._scroll_last_spread > 1.0 and spread > 1.0):
                        ratio = spread / self._scroll_last_spread
                        # Sensitivity as an EXPONENT on the ratio, not a
                        # factor on a delta: it keeps the mapping
                        # multiplicative, so the same hand movement zooms by
                        # the same proportion whatever the current scale, and
                        # a ratio of 1 stays exactly 1 at every setting
                        # instead of drifting.
                        sens = float(getattr(canvas, "zoom_sensitivity", 1.0))
                        # Anchored AT THE FINGERS, via the same entry the
                        # trackpad pinch uses. This used to call
                        # set_list_scale, which changes the scale and nothing
                        # else — so the list grew from the top of page one and
                        # a pinch half way down a long board walked the view
                        # up into the earlier pages, carrying off the sheet
                        # the user was pinching.
                        #
                        # The anchor is the CURRENT centroid, re-read every
                        # sample rather than frozen at the start of the pinch:
                        # if the hand moves while pinching, the paper should
                        # move with it, which is what the fingers are telling
                        # the board to do.
                        canvas.zoom_list_at(ratio ** sens, cx, cy)
                # Advanced on EVERY sample, including the ones spent inside
                # the dead zone. That is what keeps the first movement after
                # classification worth exactly one sample: carry the origin
                # forward instead and the board would lurch by the whole
                # dead-zone distance the instant it made up its mind.
                self._scroll_last_cy = cy
                self._scroll_last_cx = cx
                self._scroll_last_spread = spread
                self._note_scroll_sample()
            for tp in points:
                s = self._tstate(tp.state())
                if s == "Released": self._active_touches.pop(tp.id(), None)
                else: self._active_touches[tp.id()] = {"pos": canvas.screen_to_scene(tp.position()), "tool": "scroll"}
            if event.type() in (QEvent.TouchEnd, QEvent.TouchCancel):
                self._end_gesture(); self._active_touches.clear()
                canvas._ignore_mouse_until = time.time()+0.35
            event.accept(); return True
        else:
            # Not scrolling (or nothing left down). The contact count can dip
            # below two mid-gesture, so only the TRACKING is dropped here —
            # ``_gesture_kind`` is not, or the next sample would re-classify
            # from scratch and the lock would mean nothing.
            self._scroll_gesture_active = False

        # Not a scroll: no zoom, no pan — just absorb the contacts. Two
        # things land here. A multi-finger touch that never cleared the dead
        # zone, so it was never classified as anything; and the tail of one
        # that did, once every contact has lifted.
        #
        # They still have to be tracked and swallowed rather than ignored:
        # falling through would let a finger start drawing a stroke, and
        # dropping the ids would strand entries in ``_active_touches`` so the
        # NEXT single-finger touch would be miscounted as a gesture.
        #
        # ``_gesture_active`` is deliberately NOT cleared here. It used to be,
        # which meant the latch lasted exactly one event: the moment a scroll
        # thinned out to one finger, the next event went back to the drawing
        # path. It stays set until every contact has lifted (below).
        for tp in points:
            s = self._tstate(tp.state())
            if s == "Released": self._active_touches.pop(tp.id(), None)
            else: self._active_touches[tp.id()] = {"pos": canvas.screen_to_scene(tp.position()), "tool": "gesture"}
        if event.type() in (QEvent.TouchEnd, QEvent.TouchCancel):
            # Every touch sequence ends HERE, including one that spent its
            # whole life scrolling. ``active`` above skips Released points and
            # TouchEnd means they are all released, so the scroll branch is
            # never the one that sees the hand come off the glass — which is
            # exactly why the momentum handoff belongs in this path and not
            # in that one.
            #
            # TouchEnd only, never Cancel. A Cancel is the system taking the
            # sequence away — a palm landing, the window losing the grab — and
            # coasting on from one is the board reacting to something the user
            # never did.
            if event.type() == QEvent.TouchEnd:
                self._launch_fling()
            self._gesture_active = False
            self._end_gesture()
            self._active_touches.clear()
            canvas._ignore_mouse_until = time.time()+0.35
        event.accept(); return True

    # ================================================================
    # Selection
    # ================================================================
    def _handle_select_press(self, scene_pt):
        if not HAS_ENGINE: return
        canvas = self._canvas; scene = canvas.current_scene()
        if not scene: return

        # Check resize handle on existing selection
        if canvas._selection_rect and self._selected_ids:
            # Rotation handle takes priority (it sits above the box).
            if self._hit_rotate_handle(scene_pt, canvas._selection_rect):
                self._sel_mode = "rotate"
                self._rotate_center = canvas._selection_rect.center()
                self._rotate_last_angle = math.atan2(
                    scene_pt.y() - self._rotate_center.y(),
                    scene_pt.x() - self._rotate_center.x())
                # Snapshot transforms so the whole rotation is one undo step.
                self._resize_old_transforms = {}
                if scene:
                    for oid in self._selected_ids:
                        o = scene.find(oid)
                        if o:
                            tt = o.transform
                            self._resize_old_transforms[oid] = lbe.AffineTransform(
                                tt.a(), tt.b(), tt.tx(), tt.c(), tt.d(), tt.ty())
                # Flip the canvas into "rotating" state so it stops painting
                # the (stale) selection rectangle + handles, and main_window
                # hides the floating SelectionMenu until release. Paired
                # with the set_rotating(False) call in _handle_select_release.
                canvas.set_rotating(True)
                self._drawing = True
                return
            handle = self._hit_handle(scene_pt, canvas._selection_rect)
            if handle:
                self._sel_mode = "resize"; self._resize_handle = handle
                self._resize_orig_rect = QRectF(canvas._selection_rect)
                self._resize_start_pt = QPointF(scene_pt)
                self._resize_anchor = self._anchor_for_handle(handle, canvas._selection_rect)
                self._resize_accum_sx = 1.0; self._resize_accum_sy = 1.0
                # Snapshot transforms so the whole resize is one undoable command
                self._resize_old_transforms = {}
                for oid in self._selected_ids:
                    o = scene.find(oid)
                    if o:
                        tt = o.transform
                        self._resize_old_transforms[oid] = lbe.AffineTransform(
                            tt.a(), tt.b(), tt.tx(), tt.c(), tt.d(), tt.ty())
                self._drag_start = scene_pt; self._drag_last = scene_pt
                self._drawing = True; return
            # Inside box → move all
            if canvas._selection_rect.contains(scene_pt):
                self._sel_mode = "move"; self._drag_start = scene_pt
                self._drag_last = scene_pt; self._move_dx = 0; self._move_dy = 0
                # Save the combined bbox at drag-start so we can offset it
                # cheaply on every move (no per-object bbox query needed).
                self._move_start_rect = QRectF(canvas._selection_rect)
                canvas.begin_move_overlay(self._selected_ids)
                self._drawing = True; return

        # Click on object → select it
        pt = lbe.Vec2f(scene_pt.x(), scene_pt.y())
        hit = scene.hit_test(pt, 8.0)
        if hit and not hit.locked:
            self._selected_ids = [hit.id]
            self._update_combined_rect()
            self._sel_mode = "move"; self._drag_start = scene_pt
            self._drag_last = scene_pt; self._move_dx = 0; self._move_dy = 0
            self._move_start_rect = QRectF(canvas._selection_rect)
            canvas.begin_move_overlay(self._selected_ids)
            self._drawing = True; return

        # Empty space → lasso select
        self._selected_ids.clear(); canvas.clear_selection()
        self._sel_mode = "lasso"; self._drawing = True
        canvas.begin_lasso(); canvas.add_lasso_point(scene_pt)

    def _handle_select_move(self, scene_pt):
        if not self._drawing: return
        canvas = self._canvas; scene = canvas.current_scene()
        if not scene or not HAS_ENGINE: return

        if self._sel_mode == "move":
            # PERFORMANCE: don't translate objects in the engine + invalidate
            # the scene cache on every pointer sample (used to be O(N) per
            # move event — a full re-raster of every visible object). Instead,
            # the canvas paints the dragged objects as a floating overlay on
            # top of a static cache, translated by an ever-growing (dx, dy)
            # offset. The engine sees ONE final translate at release.
            dx = scene_pt.x() - self._drag_start.x()
            dy = scene_pt.y() - self._drag_start.y()
            self._drag_last = scene_pt
            self._move_dx = dx; self._move_dy = dy
            canvas.update_move_overlay(dx, dy)
            self._update_combined_rect_overlay(dx, dy)
        elif self._sel_mode == "resize":
            self._do_resize(scene_pt)
        elif self._sel_mode == "rotate":
            self._do_rotate(scene_pt)
        elif self._sel_mode == "lasso":
            canvas.add_lasso_point(scene_pt)

    def _handle_select_release(self, scene_pt):
        canvas = self._canvas; scene = canvas.current_scene()

        if self._sel_mode == "lasso":
            lasso_pts = canvas.end_lasso()
            self._drawing = False; self._sel_mode = ""
            if len(lasso_pts) >= 3:
                self._select_in_lasso(lasso_pts)
            return

        if self._sel_mode == "move" and HAS_ENGINE:
            self._drawing = False
            # Engine state is unchanged from drag-start — the overlay was a
            # pure visual preview. Apply the final translation once now, as
            # a single undo entry. Threshold 1 px in world units to ignore
            # accidental sub-pixel jitter.
            moved = abs(self._move_dx) > 1 or abs(self._move_dy) > 1
            if moved:
                stack = canvas.current_stack()
                if scene and stack:
                    # DROP-SNAP BUG FIX: previously the code called
                    # ``obj.translate(dx, dy)`` here BEFORE pushing the
                    # MoveObjectCmd, believing push_move was undo-metadata
                    # only. It isn't — ``CommandStack::push`` calls
                    # ``execute()`` on the new command, and
                    # ``MoveObjectCmd::execute`` runs ``obj->translate()``
                    # with the same delta (see engine/src/command/
                    # MoveObjectCmd.cpp:19). So the object was being
                    # translated TWICE per release, once by Python and once
                    # by the command's execute — landing at ``2·(dx, dy)``.
                    # Visually the overlay stopped at ``start+delta``
                    # (where the finger lifted), but the engine sat at
                    # ``start+2·delta``. On the next repaint the object
                    # "snapped" to a point offset by the drag distance
                    # again past the release location — exactly the bug
                    # that was reported.
                    #
                    # Fix: push the command and let its execute() do the
                    # ONE translate. Do NOT translate here.
                    for oid in self._selected_ids:
                        stack.push_move(scene, oid,
                                        lbe.Vec2f(self._move_dx, self._move_dy))
            # ALWAYS end the overlay — whether the move was committed or
            # was a sub-threshold click. Otherwise the floating objects
            # would stay invisible-on-canvas (excluded from cache, no
            # overlay update happening anymore).
            canvas.end_move_overlay()
            if moved:
                self._update_combined_rect()
            self._move_start_rect = None
            self._sel_mode = ""; return

        if self._sel_mode == "resize":
            self._drawing = False; self._sel_mode = ""
            # Record the resize as a single transform command per object (undoable),
            # so the new transform persists and isn't lost by later operations.
            if HAS_ENGINE and scene and hasattr(self, "_resize_old_transforms"):
                stack = canvas.current_stack()
                if stack:
                    for oid in self._selected_ids:
                        old_t = self._resize_old_transforms.get(oid)
                        o = scene.find(oid)
                        if old_t is not None and o is not None:
                            cur = o.transform
                            new_t = lbe.AffineTransform(cur.a(), cur.b(), cur.tx(),
                                                        cur.c(), cur.d(), cur.ty())
                            # Reset to old, then push command that re-applies new.
                            o.transform = old_t
                            stack.push_transform(scene, oid, old_t, new_t)
                self._resize_old_transforms = {}
            self._update_combined_rect(); canvas.invalidate_cache(); return

        if self._sel_mode == "rotate":
            self._drawing = False; self._sel_mode = ""
            # One undoable transform command per object (old → rotated).
            if HAS_ENGINE and scene and self._resize_old_transforms:
                stack = canvas.current_stack()
                if stack:
                    for oid in self._selected_ids:
                        old_t = self._resize_old_transforms.get(oid)
                        o = scene.find(oid)
                        if old_t is not None and o is not None:
                            cur = o.transform
                            new_t = lbe.AffineTransform(cur.a(), cur.b(), cur.tx(),
                                                        cur.c(), cur.d(), cur.ty())
                            o.transform = old_t
                            stack.push_transform(scene, oid, old_t, new_t)
                self._resize_old_transforms = {}
            # Recompute the selection rect FIRST so the box that reappears
            # matches the new (rotated) content bbox, THEN drop the
            # rotating flag so the newly-correct box + the SelectionMenu
            # come back in one paint. Doing set_rotating(False) BEFORE
            # _update_combined_rect would briefly repaint the old stale
            # rect for one frame.
            self._update_combined_rect(); canvas.invalidate_cache()
            canvas.set_rotating(False)
            return

        self._drawing = False; self._sel_mode = ""

    def _update_combined_rect(self):
        if not HAS_ENGINE: return
        canvas = self._canvas; scene = canvas.current_scene()
        if not scene: return
        valid_ids = []; combined = None
        for oid in self._selected_ids:
            obj = scene.find(oid)
            if obj:
                bb = obj.bounding_box()
                r = QRectF(bb.min_pt.x, bb.min_pt.y, bb.max_pt.x-bb.min_pt.x, bb.max_pt.y-bb.min_pt.y)
                combined = r if combined is None else combined.united(r)
                valid_ids.append(oid)
        self._selected_ids = valid_ids
        canvas.set_selection(valid_ids, combined)

    def _update_combined_rect_overlay(self, dx: float, dy: float) -> None:
        """O(1) drag-preview update for the selection bbox: takes the rect
        captured at drag-start and translates it by (dx, dy) in world units.
        Avoids the per-pointer-sample O(N) bbox query that
        ``_update_combined_rect`` performs."""
        start = getattr(self, "_move_start_rect", None)
        if start is None:
            self._update_combined_rect()
            return
        shifted = start.translated(dx, dy)
        self._canvas.set_selection(self._selected_ids, shifted)

    def _select_in_lasso(self, lasso_pts):
        if not HAS_ENGINE: return
        canvas = self._canvas; scene = canvas.current_scene()
        if not scene: return
        xs = [p.x() for p in lasso_pts]; ys = [p.y() for p in lasso_pts]
        lr = QRectF(min(xs), min(ys), max(xs)-min(xs), max(ys)-min(ys))
        if lr.width() < 5 and lr.height() < 5: canvas.clear_selection(); return
        aabb = lbe.AABB(lbe.Vec2f(lr.left(), lr.top()), lbe.Vec2f(lr.right(), lr.bottom()))
        hits = scene.query_rect(aabb)
        sel = []
        for obj in hits:
            if obj.locked: continue
            bb = obj.bounding_box()
            cx = (bb.min_pt.x+bb.max_pt.x)/2; cy = (bb.min_pt.y+bb.max_pt.y)/2
            if self._pip(cx, cy, lasso_pts): sel.append(obj.id)
        self._selected_ids = sel; self._update_combined_rect()

    def _hit_rotate_handle(self, pt, rect):
        scale = max(1e-6, self._canvas.view_scale)
        off = ROTATE_HANDLE_OFFSET / scale
        hx = rect.center().x()
        hy = rect.top() - off
        tol = (ROTATE_HANDLE_RADIUS + 10.0) / scale
        return abs(pt.x() - hx) <= tol and abs(pt.y() - hy) <= tol

    def _do_rotate(self, scene_pt):
        canvas = self._canvas; scene = canvas.current_scene()
        if not scene or not HAS_ENGINE:
            return
        cx = self._rotate_center.x(); cy = self._rotate_center.y()
        cur = math.atan2(scene_pt.y() - cy, scene_pt.x() - cx)
        d = cur - self._rotate_last_angle
        # shortest-path normalise to (-pi, pi]
        while d > math.pi:  d -= 2 * math.pi
        while d <= -math.pi: d += 2 * math.pi
        self._rotate_last_angle = cur
        if abs(d) < 1e-6:
            return
        cosd = math.cos(d); sind = math.sin(d)
        # Rotate every object about the selection-centre pivot (cx, cy). The
        # engine's rotate() doesn't pivot on a known fixed point, so — exactly
        # like _do_resize does for scaling — we rotate the object, then MEASURE
        # its resulting centre and translate it to where it belongs: the old
        # centre rotated by `d` about the pivot. A single object therefore spins
        # in place; a multi-selection orbits the shared centre.
        for oid in self._selected_ids:
            obj = scene.find_mut(oid)
            if not obj:
                continue
            bb = obj.bounding_box()
            ox = (bb.min_pt.x + bb.max_pt.x) / 2.0
            oy = (bb.min_pt.y + bb.max_pt.y) / 2.0
            rx = ox - cx; ry = oy - cy
            new_cx = cx + (cosd * rx - sind * ry)
            new_cy = cy + (sind * rx + cosd * ry)
            obj.rotate(d)                         # orientation (+ shifts centre)
            bb2 = obj.bounding_box()
            cx2 = (bb2.min_pt.x + bb2.max_pt.x) / 2.0
            cy2 = (bb2.min_pt.y + bb2.max_pt.y) / 2.0
            obj.translate(new_cx - cx2, new_cy - cy2)
        self._update_combined_rect(); canvas.invalidate_cache()

    def _hit_handle(self, pt, rect):
        hs = HANDLE_HALF/self._canvas.view_scale
        handles = {"tl": QPointF(rect.left(),rect.top()), "tc": QPointF(rect.center().x(),rect.top()),
                   "tr": QPointF(rect.right(),rect.top()), "ml": QPointF(rect.left(),rect.center().y()),
                   "mr": QPointF(rect.right(),rect.center().y()), "bl": QPointF(rect.left(),rect.bottom()),
                   "bc": QPointF(rect.center().x(),rect.bottom()), "br": QPointF(rect.right(),rect.bottom())}
        for name, hpt in handles.items():
            if abs(pt.x()-hpt.x()) <= hs and abs(pt.y()-hpt.y()) <= hs: return name
        return ""

    def _do_resize(self, scene_pt):
        """Professional group resize: scale positions relative to anchor AND scale sizes."""
        canvas = self._canvas; scene = canvas.current_scene()
        if not scene or not self._resize_orig_rect: return

        orig = self._resize_orig_rect; h = self._resize_handle
        # Total drag delta from where resize STARTED
        dx = scene_pt.x() - self._resize_start_pt.x()
        dy = scene_pt.y() - self._resize_start_pt.y()

        # Compute TOTAL scale factors from start of resize
        ow = max(1.0, orig.width()); oh = max(1.0, orig.height())
        target_sx = target_sy = 1.0
        if "r" in h: target_sx = max(0.05, (ow + dx) / ow)
        elif "l" in h: target_sx = max(0.05, (ow - dx) / ow)
        if "b" in h: target_sy = max(0.05, (oh + dy) / oh)
        elif "t" in h: target_sy = max(0.05, (oh - dy) / oh)
        # Corner handles → uniform scale
        if h in ("tl","tr","bl","br"):
            s = max(target_sx, target_sy)
            target_sx = target_sy = s

        # Incremental scale this frame (to apply on top of last frame's state)
        inc_sx = target_sx / max(0.0001, self._resize_accum_sx)
        inc_sy = target_sy / max(0.0001, self._resize_accum_sy)

        ax = self._resize_anchor.x(); ay = self._resize_anchor.y()

        # For each object: scale around the GROUP anchor, not its own origin.
        # Math: new_world_center = anchor + (current_world_center - anchor) * inc_scale
        # Implementation: scale object first (this changes its size, may shift center),
        # then translate to put its center exactly where it should be.
        for oid in self._selected_ids:
            obj = scene.find_mut(oid)
            if not obj: continue
            bb1 = obj.bounding_box()
            cx = (bb1.min_pt.x + bb1.max_pt.x) / 2.0
            cy = (bb1.min_pt.y + bb1.max_pt.y) / 2.0
            # Target new center after scaling this frame
            new_cx = ax + (cx - ax) * inc_sx
            new_cy = ay + (cy - ay) * inc_sy
            # Circles must stay circular: scale uniformly by the geometric mean
            # so an edge-handle (non-uniform) drag can't distort them.
            if obj.type_name == "circle" or (
                    obj.type_name == "shape" and sg.locked_aspect(obj.kind)):
                u = (max(0.0001, inc_sx) * max(0.0001, inc_sy)) ** 0.5
                obj.scale(u, u)
            else:
                obj.scale(inc_sx, inc_sy)
            # Recompute its center after scale
            bb2 = obj.bounding_box()
            cx2 = (bb2.min_pt.x + bb2.max_pt.x) / 2.0
            cy2 = (bb2.min_pt.y + bb2.max_pt.y) / 2.0
            # Translate to put center where it should be
            obj.translate(new_cx - cx2, new_cy - cy2)

        # Save accumulated scale for next frame
        self._resize_accum_sx = target_sx
        self._resize_accum_sy = target_sy
        self._update_combined_rect()
        canvas.invalidate_cache()

    @staticmethod
    def _anchor_for_handle(handle, rect):
        """Anchor = opposite corner / edge of the dragged handle."""
        if handle == "br": return QPointF(rect.left(), rect.top())
        elif handle == "bl": return QPointF(rect.right(), rect.top())
        elif handle == "tr": return QPointF(rect.left(), rect.bottom())
        elif handle == "tl": return QPointF(rect.right(), rect.bottom())
        elif handle == "tc": return QPointF(rect.center().x(), rect.bottom())
        elif handle == "bc": return QPointF(rect.center().x(), rect.top())
        elif handle == "ml": return QPointF(rect.right(), rect.center().y())
        elif handle == "mr": return QPointF(rect.left(), rect.center().y())
        return QPointF(rect.center())

    @staticmethod
    def _pip(px, py, polygon):
        n = len(polygon); inside = False; j = n-1
        for i in range(n):
            xi,yi = polygon[i].x(),polygon[i].y()
            xj,yj = polygon[j].x(),polygon[j].y()
            if ((yi>py) != (yj>py)) and (px < (xj-xi)*(py-yi)/(yj-yi)+xi):
                inside = not inside
            j = i
        return inside

    # ================================================================
    # Stroke commit
    # ================================================================
    def _commit_stroke(self, tool: str = ""):
        """Turn the finished live stroke into a scene object.

        ``tool`` is the EFFECTIVE tool this stroke was drawn with. It must be
        passed (or resolved via ``_current_tool()``) and never read from
        ``canvas.tool`` directly: in Dual Pen mode ``canvas.tool`` is the
        literal string "dual_pen", which matches neither branch below, so the
        stroke fell through to the freehand path. A tip assigned to the
        highlighter therefore PREVIEWED as a highlighter — begin_live_stroke()
        was correctly told is_highlighter=True at press — and then committed
        as a thin opaque pen stroke on release. That mismatch is exactly what
        the _current_tool() docstring warns about: a press-time decision and a
        release-time decision diverging.
        """
        canvas = self._canvas
        pts = canvas.end_live_stroke(); pressures = canvas.get_live_pressures()
        if not pts or not HAS_ENGINE: return
        scene = canvas.current_scene(); stack = canvas.current_stack()
        if not scene or not stack: return
        tool = tool or self._current_tool()
        if tool in ("highlighter", "brush"):
            # Both are chisel ribbons and both commit as HighlighterStroke.
            # The brush is the opaque one, and its opacity is what marks it as
            # a brush when the board is read back — see ui/canvas/brush_ink.py
            # for why the two share a stored type.
            from ui.canvas.brush_ink import BRUSH_OPACITY
            is_brush = (tool == "brush")
            stroke = lbe.HighlighterStroke()
            sc = canvas.brush_color if is_brush else canvas.highlighter_color
            stroke.stroke_color = lbe.RGBA(sc.red(),sc.green(),sc.blue(),sc.alpha())
            stroke.opacity = (BRUSH_OPACITY if is_brush
                              else canvas._highlighter_alpha/255.0)
            stroke.height = (canvas.brush_height if is_brush
                             else canvas.highlighter_height)
            stroke.reserve(len(pts))
            for p in pts: stroke.add_point(lbe.Vec2f(p.x(),p.y()))
            # No simplify — keep the ribbon identical to its live preview.
        else:
            stroke = lbe.FreehandStroke()
            # active_pen_color, not pen_color: a Dual Pen stroke resolves to
            # the pen tool but draws with Dual Pen's own ink.
            sc = canvas.active_pen_color()
            stroke.stroke_color = lbe.RGBA(sc.red(),sc.green(),sc.blue(),sc.alpha())
            stroke.stroke_width = canvas.pen_width
            stroke.reserve(len(pts))
            for i,p in enumerate(pts):
                pr = pressures[i] if i<len(pressures) else -1.0
                stroke.add_point(lbe.Vec2f(p.x(),p.y()), pr)
            # NO simplify(). Douglas-Peucker reduction changes the point set,
            # which makes the quad-Bézier smoothing produce a slightly
            # different curve from the live preview — exactly the "stroke
            # changes after release" bug. Highlighter has never simplified
            # for the same reason; bring pen in line.
        stack.push_stroke(scene, stroke)
        canvas.invalidate_cache(); canvas.mark_dirty()

    # ================================================================
    # Transform helpers
    # ================================================================
    @staticmethod
    def _to_world(x, y, t):
        """Apply an AffineTransform to a local point -> world coords.
        Engine layout: x' = a*x + b*y + tx ; y' = c*x + d*y + ty."""
        if t.is_identity():
            return x, y
        return (t.a() * x + t.b() * y + t.tx(),
                t.c() * x + t.d() * y + t.ty())

    @staticmethod
    def _transform_scale(t):
        """Average linear scale factor of an affine transform.
        Used to convert a local stroke_width into its on-screen world width."""
        if t.is_identity():
            return 1.0
        # Column vector lengths give x/y scale; average them.
        sx = math.hypot(t.a(), t.c())
        sy = math.hypot(t.b(), t.d())
        s = (sx + sy) * 0.5
        return s if s > 1e-6 else 1.0

    @staticmethod
    def _dist_to_segment_sq(px, py, ax, ay, bx, by):
        """Squared distance from point (px,py) to segment [(ax,ay),(bx,by)]."""
        dx = bx - ax
        dy = by - ay
        l2 = dx * dx + dy * dy
        if l2 < 1e-9:
            return (px - ax) ** 2 + (py - ay) ** 2
        t = max(0.0, min(1.0, ((px - ax) * dx + (py - ay) * dy) / l2))
        cx = ax + t * dx
        cy = ay + t * dy
        return (px - cx) ** 2 + (py - cy) ** 2

    # ================================================================
    # Pixel eraser commit
    #
    # Splits intersecting strokes/shapes along the eraser path. All geometry
    # is computed in WORLD space (transforms applied), and new pieces are
    # created in world space with an identity transform. Stroke widths are
    # converted to world space too, so erasing a moved/resized object keeps
    # its visual thickness and position EXACTLY the same.
    # ================================================================
    # ================================================================
    # Pixel eraser — live preview
    # ================================================================
    def _pixel_eraser_extend(self, scene_pt, *, first: bool = False) -> None:
        """Add a new capsule segment to the live eraser mask.

        Each capsule connects the previous pointer sample to the new one,
        with the active eraser radius. The canvas's freehand / highlighter
        renderers consult this mask on every paint to visually subtract the
        covered pixels — no bg-colour blit, no engine mutation per move,
        media never touched. Performance: O(1) per move (we just append +
        repaint a small dirty rect around the new segment).
        """
        canvas = self._canvas
        radius = canvas.eraser_size / 2.0
        x, y = scene_pt.x(), scene_pt.y()
        mask = canvas._eraser_mask_segments
        if first or not mask:
            # Degenerate "point" segment so the very first dot is visible
            # before the user has moved.
            canvas.reset_erase_caches()
            self._measure_erase_pad()
            canvas.append_eraser_mask_segment(x, y, x, y, radius)
            self._last_point = scene_pt
            # Nothing is held out of the cache yet, so there is nothing to
            # rebuild — press only has to lift out whatever this first dab
            # actually cuts. Marking the cache dirty here re-rasterised the
            # whole page on every single press of the eraser.
            vacated = canvas.advance_erase()
            if vacated and not canvas.repair_cache_boxes(vacated):
                canvas._cache_dirty = True
            canvas.update()
            return
        prev = self._last_point
        canvas.append_eraser_mask_segment(prev.x(), prev.y(), x, y, radius)
        self._last_point = scene_pt
        # Fold the new capsule into every stroke it could reach, and find out
        # which ones it actually CUT. Only those have to come out of the scene
        # cache and start being painted live; a stroke the eraser merely swept
        # past keeps its cached pixels and costs nothing more this drag.
        #
        # And when a stroke does come out, patch the cache instead of dropping
        # it. Only the vacated boxes changed, so re-rasterising the whole page
        # is throwing away every other object's pixels to fix one — see
        # ``CanvasWidget.repair_cache_boxes`` for what that cost.
        vacated = canvas.advance_erase()
        if vacated and not canvas.repair_cache_boxes(vacated):
            canvas._cache_dirty = True
        # Dirty-rect repaint: bbox of new segment + the eraser radius padding,
        # converted from world space to widget space. Falls back to full
        # repaint if the conversion isn't available (e.g. during init).
        try:
            wa = canvas.scene_to_screen(QPointF(prev.x(), prev.y()))
            wb = canvas.scene_to_screen(QPointF(x, y))
            # Pad by the tallest highlighter half-height, not just the
            # eraser radius: cutting a centreline sample blanks the band's
            # FULL height at that x, which can reach well outside the
            # eraser's own footprint. Repainting only the eraser bbox left
            # stale band pixels hanging above/below the cut.
            pad = int(max(radius, self._live_erase_pad)
                      * canvas._view_scale) + 4
            x0 = int(min(wa.x(), wb.x()) - pad); y0 = int(min(wa.y(), wb.y()) - pad)
            x1 = int(max(wa.x(), wb.x()) + pad); y1 = int(max(wa.y(), wb.y()) + pad)
            # A stroke the eraser has just cut was REMOVED from the scene
            # cache a few lines up and from now on exists only in the live
            # overlay — which culls itself to this rectangle. So the whole of
            # that stroke has to be inside it, not just the part under the
            # eraser, or the rest of it blanks out until something else forces
            # a repaint. Only its bounds, though: repainting the whole widget
            # here cost ~18 ms every time the eraser met a new stroke.
            for bx0, by0, bx1, by1 in vacated:
                ta = canvas.scene_to_screen(QPointF(bx0, by0))
                tb = canvas.scene_to_screen(QPointF(bx1, by1))
                x0 = min(x0, int(ta.x()) - pad); y0 = min(y0, int(ta.y()) - pad)
                x1 = max(x1, int(tb.x()) + pad); y1 = max(y1, int(tb.y()) + pad)
            canvas.update(x0, y0, max(1, x1 - x0), max(1, y1 - y0))
        except Exception:
            canvas.update()

    # ---- Drag bookkeeping -------------------------------------------
    def _measure_erase_pad(self) -> None:
        """Widest half-thickness of any ink on the page, measured ONCE.

        Only the repaint rectangle needs this: cutting a stroke changes
        pixels up to a half-width either side of the centreline, which can
        reach well outside the eraser's own footprint, and padding the dirty
        rect by only the eraser radius left stale band pixels hanging above
        and below the cut.

        This replaces a per-sample scan that flagged which centreline points
        each new capsule covered. Nothing read those flags any more — the
        preview subtracts real geometry now — so every pointer sample was
        paying an objects-by-samples loop to fill in a dictionary no one
        opened. The only thing that scan still produced worth keeping is this
        one number, and it does not change during a drag.
        """
        canvas = self._canvas
        max_half = 0.0
        scene = canvas.current_scene() if HAS_ENGINE else None
        if scene:
            for i in range(scene.object_count):
                obj = scene.object_at(i)
                tn = obj.type_name
                if tn == "highlighter":
                    half = obj.height * self._transform_scale(obj.transform) * 0.5
                elif tn == "freehand":
                    half = obj.stroke_width * self._transform_scale(obj.transform) * 0.5
                else:
                    continue
                if half > max_half:
                    max_half = half
        self._live_erase_pad = max_half

    def _runs_for(self, obj, tn, field, remainder):
        """Try to put an erased stroke back together AS A STROKE.

        Returns ``(runs, world_size)`` when the surviving sub-runs of the
        centreline, drawn at the original thickness, paint the same picture
        as the true subtracted region — and ``None`` when they don't, which
        sends the caller to the polygon fallback.

        The check is a measurement, not a guess (``eg.runs_are_faithful``).
        That matters: the whole reason the commit stopped emitting strokes in
        the first place was that centreline cutting silently produced a
        DIFFERENT shape from the preview, and nothing in the code noticed.
        Here, if the two disagree by more than the unavoidable
        cap-versus-scoop sliver at each cut, we don't ship the stroke.
        """
        canvas = self._canvas
        pts = obj.points()
        if not pts:
            return None
        t = obj.transform
        scale = self._transform_scale(t)
        size = (obj.height if tn == "highlighter" else obj.stroke_width) * scale
        if size <= 0.0:
            return None
        if t.is_identity():
            world = [(p[0], p[1]) for p in pts]
        else:
            world = [self._to_world(p[0], p[1], t) for p in pts]

        half = size * 0.5
        runs = eg.surviving_runs(world, field, min_len=max(0.5, half * 0.4))
        if tn == "highlighter":
            # A one-point highlighter run has no ribbon to sweep, so it would
            # paint nothing while the remainder still holds ink there.
            runs = [r for r in runs if len(r) >= 2]
        if not runs:
            # The centreline is entirely covered but ink survives anyway —
            # crumbs along an edge the eraser passed beside. Polygons.
            return None

        # Count only the ends the ERASER made. A run that still starts at the
        # stroke's own first sample was never cut there, and allowing it a
        # cap-sized discrepancy it cannot have would loosen the test for free.
        fx, fy = world[0]
        lx, ly = world[-1]

        def _is_original_end(p):
            return ((abs(p[0] - fx) < 1e-6 and abs(p[1] - fy) < 1e-6) or
                    (abs(p[0] - lx) < 1e-6 and abs(p[1] - ly) < 1e-6))

        cut_ends = 0
        thickest_cut = 0.0
        for r in runs:
            if not _is_original_end(r[0]):
                cut_ends += 1
                if len(r) >= 2:
                    thickest_cut = max(thickest_cut,
                                       self._cut_thickness(tn, size, r[0], r[1]))
            if not _is_original_end(r[-1]):
                cut_ends += 1
                if len(r) >= 2:
                    thickest_cut = max(thickest_cut,
                                       self._cut_thickness(tn, size, r[-2], r[-1]))

        # ---- Did the eraser even COVER the ink it is being asked to cut? ----
        # A stroke stores one thickness, so cutting its centreline removes the
        # ink's FULL thickness across the cut. That is only what the eraser did
        # if the eraser was at least as wide as the ink is thick. When it was
        # narrower, the split necessarily takes a band of
        # ``thickness - 2r`` that the eraser never went near — and on a big
        # stroke that is a lot of ink while still being a small FRACTION of it,
        # which is exactly how it slipped past ``runs_are_faithful``: every
        # term of that allowance scales with the stroke's own area, so a 250 px
        # eraser drawn through a 560 px band had its 36,000 px² of wrongly
        # removed ink forgiven as 4% of a million.
        #
        # This is not a heuristic standing in for the measurement — it is the
        # precondition the measurement's error model assumes. The
        # cap-versus-scoop sliver it prices at ``half²`` is only a sliver while
        # the eraser covers the thickness; past that the true difference is a
        # full-height gap and no tolerance calibrated on slivers describes it.
        if cut_ends and field.r * 2.0 < thickest_cut:
            return None

        # The chisel builder is handed in with THIS object's nib angle bound.
        # It used to be passed bare, so every chisel outline the eraser
        # computed used an upright nib — correct for a highlighter, wrong for
        # a brush stroke, which is drawn tilted. The eraser was measuring
        # coverage against a shape the renderer never painted, so cuts landed
        # in the wrong place and the surviving fragments came back a different
        # shape from the stroke that went in.
        from ui.canvas.brush_ink import nib_angle_for
        _angle = nib_angle_for(obj)

        def _chisel(pts_, size_, *a, **k):
            k.setdefault("angle_deg", _angle)
            return canvas._chisel_path(pts_, size_, *a, **k)

        parts = eg.runs_outline_parts(
            runs, tn, size,
            build_freehand_path=canvas._build_freehand_path,
            chisel_path=_chisel,
        )

        # ---- Would splitting change how the ink SHADES itself? -------------
        # A stroke is painted in one fill operation, so wherever it doubles
        # back over itself the overlap does not darken. Six stored strokes are
        # six objects, each with its own opacity, so every overlap between them
        # darkens — and a highlighter scribbled back and forth to block
        # something out is nothing but overlaps. Erase across one and it came
        # back banded, 38,000 pixels different from the preview the user had
        # just been watching.
        #
        # ``runs_are_faithful`` cannot catch this and it is not its job: it
        # compares the runs' UNION against the true remainder, and the union is
        # exactly right. The shape is perfect. The picture is not.
        #
        # Only translucent ink cares — overlapping opaque strokes look the same
        # however many objects they are — and the polygon fallback is safe
        # either way, because ``path_to_polygons`` cuts a simplified path into
        # pieces that by construction do not overlap.
        if self._is_translucent(obj) and eg.parts_overlap(parts):
            return None

        cand = eg.union_of(parts)
        if cand is None:
            return None

        if not eg.runs_are_faithful(cand, remainder,
                                    half_size=half, cut_ends=cut_ends):
            return None
        return runs, size

    @staticmethod
    def _is_translucent(obj) -> bool:
        """Does this object blend with what is under it?

        Both channels count: the renderer applies ``obj.opacity`` to the
        painter and the colour carries its own alpha, and either one being
        short of full makes overlaps visible.
        """
        try:
            if float(obj.opacity) < 0.999:
                return True
        except Exception:
            pass
        try:
            return int(obj.stroke_color.a) < 255
        except Exception:
            return False

    @staticmethod
    def _cut_thickness(tn: str, size: float, a, b) -> float:
        """How thick the ink is ACROSS the direction of travel ``a`` -> ``b``.

        A pen is a disc, so it is ``size`` whichever way it goes. A
        highlighter is not: ``_chisel_path`` sweeps an axis-aligned bar
        ``nib`` wide and ``size`` tall, so a horizontal stroke is a tall band
        and a vertical one is a thin line. Using the height for both would
        send every vertical highlighter cut to the polygon fallback it does
        not need — the eraser covers a nib easily.
        """
        if tn != "highlighter":
            return size
        dx, dy = b[0] - a[0], b[1] - a[1]
        length = math.hypot(dx, dy)
        if length < 1e-9:
            return size
        nib = max(1.5, size * 0.06)
        # Support width of the nib rectangle along the normal to travel.
        return (nib * abs(dy) + size * abs(dx)) / length

    # ================================================================
    # Pixel eraser — commit (engine-side stroke splitting)
    # ================================================================
    def _clear_erase_drag_state(self) -> None:
        canvas = self._canvas
        canvas._eraser_mask_segments = []
        self._live_erase_pad = 0.0
        canvas.reset_erase_caches()

    def _commit_pixel_erase(self):
        canvas = self._canvas
        # The mask stays in place for the whole of phase 1 and is only cleared
        # once we know what we are going to do. That is not cosmetic: phase 1
        # asks the canvas for each object's remainder, and those remainders
        # are the ones the DRAG already computed — most of the subtraction
        # work for this commit was done frame by frame while the user was
        # rubbing, and clearing the mask first would throw it away and make
        # the commit redo all of it.
        mask = canvas._eraser_mask_segments
        if not mask or not HAS_ENGINE:
            self._clear_erase_drag_state()
            canvas.update()
            return
        scene = canvas.current_scene()
        stack = canvas.current_stack()
        if not scene or not stack:
            self._clear_erase_drag_state()
            canvas.update()
            return

        # All segments in a single drag share the same radius (the eraser size
        # is locked at press time). Pull it from segment 0.
        radius = float(mask[0][4])

        # ``eraser_segs`` is a list of (ax,ay,bx,by) tuples (no radius —
        # radius is shared) for the line/shape distance helpers below.
        eraser_segs = [(s[0], s[1], s[2], s[3]) for s in mask]
        exs = [s[0] for s in mask] + [s[2] for s in mask]
        eys = [s[1] for s in mask] + [s[3] for s in mask]
        emin_x, emax_x = min(exs) - radius, max(exs) + radius
        emin_y, emax_y = min(eys) - radius, max(eys) + radius

        # Grid-indexed point-in-eraser queries, used by the run splitter that
        # tries to keep erased ink as ink. Built once for the whole commit.
        field = eg.EraserField(mask)

        # Phase 1: gather changes. Each entry:
        #   (object_id, original_obj, type_name, payload, extras)
        to_split = []

        for i in range(scene.object_count):
            obj = scene.object_at(i)
            if not obj or obj.locked:
                continue
            tn = obj.type_name
            t = obj.transform

            # World-space bbox fast reject for everything.
            bb = obj.bounding_box()
            if bb.max_pt.x < emin_x or bb.min_pt.x > emax_x:
                continue
            if bb.max_pt.y < emin_y or bb.min_pt.y > emax_y:
                continue

            if tn in ("line", "arrow"):
                sx, sy = self._to_world(obj.start.x, obj.start.y, t)
                ex, ey = self._to_world(obj.end.x, obj.end.y, t)
                result = self._split_line_world(sx, sy, ex, ey, eraser_segs, radius)
                if result is not None:
                    extras = {
                        "head_length": obj.head_length,
                        "head_width": obj.head_width,
                    } if tn == "arrow" else {}
                    # World-space stroke width.
                    extras["world_width"] = obj.stroke_width * self._transform_scale(t)
                    to_split.append((obj.id, obj, tn, result, extras))
                continue
            elif tn in ("rect", "ellipse"):
                # These delete wholesale if the eraser touches them.
                if self._eraser_hits_bbox(bb, eraser_segs, radius):
                    to_split.append((obj.id, obj, tn, [], {}))
                continue
            elif tn in ("image", "ppt"):
                # Imported media is NEVER erased by the pixel eraser, regardless
                # of settings — the pixel eraser is for ink only.
                continue
            # POLYGON is in this list because that is what erased ink now IS.
            # Leaving it out meant a second rub over an already-erased area did
            # nothing at all — the ink had become a polygon and the eraser no
            # longer recognised it as erasable.
            if tn not in eg.ERASABLE_TYPES:
                continue

            # ---- SUBTRACT the eraser from the painted outline ----------
            # This is the SAME remainder the live preview painted — literally
            # the same path object, carried over from the drag — so what the
            # user watched is what lands here, and almost all of the boolean
            # work has already been paid for frame by frame.
            outline, remainder, hit = canvas.erased_pair_for(obj)
            if outline is None or outline.isEmpty():
                continue
            # Nothing actually removed -> leave the original alone rather than
            # churning it (keeps undo clean and avoids re-typing an object the
            # user never touched). The eraser crossing an object's bounding box
            # without touching its ink is the common case for a diagonal rub,
            # and the drag already established this geometrically — no need to
            # measure two areas here to rediscover it.
            if not hit:
                continue
            if remainder is None or remainder.isEmpty():
                to_split.append((obj.id, obj, tn, [], {}))   # fully erased
                continue

            # ---- Can this stay a STROKE? -------------------------------
            # Try first, always. A stroke keeps its handful of points, keeps
            # its type, and can be erased again cheaply; the polygon version
            # of the same ink is hundreds of vertices of flattened outline.
            # ``_runs_for`` only says yes when it has MEASURED that the two
            # paint the same thing.
            if tn in eg.STROKE_TYPES:
                runs = self._runs_for(obj, tn, field, remainder)
                if runs is not None:
                    to_split.append((obj.id, obj, tn, runs[0],
                                     {"as_runs": True, "world_size": runs[1]}))
                    continue

            # ---- ...no. Store the region itself ------------------------
            # A thin eraser punching a hole through a fat highlighter, or a
            # graze that bites the edge without crossing the centreline. A
            # polyline plus one width cannot hold either shape; a polygon can,
            # and this is the case polygons are here for.
            polys = eg.path_to_polygons(remainder)
            if not polys:
                to_split.append((obj.id, obj, tn, [], {}))
                continue
            to_split.append((obj.id, obj, tn, polys, {"as_polygons": True}))

        # Phase 1 is done with the mask and the per-object remainders, so the
        # preview state goes now — after the reads, before anything repaints.
        self._clear_erase_drag_state()

        if not to_split:
            canvas.invalidate_cache()
            canvas.mark_dirty()
            return

        # Phase 2: apply. Delete each original, add surviving pieces in world space
        # with identity transform and world-space widths.
        #
        # Every add/delete goes into ONE ``actions`` list and is pushed as
        # a single compound command. The old per-piece push_add/push_delete
        # calls made a single erase cost N undo entries — pressing Undo
        # once popped only the LAST of them (one original's delete), which
        # resurrected that stroke on top of its own split pieces: doubled
        # ink, then the pieces peeled away one Undo at a time. One gesture,
        # one undo entry.
        actions: list = []
        for oid, orig, tn, payload, extras in to_split:
            if extras.get("as_runs"):
                # Erased ink STAYS ink. Each surviving run of the centreline
                # becomes a stroke of the same kind at the original world-space
                # thickness, with an identity transform, so a rubbed stroke
                # comes out of the eraser with roughly the vertex count it went
                # in with instead of a few hundred per fragment — and can be
                # selected, recoloured and re-erased as the stroke it still is.
                c = orig.stroke_color
                size = extras["world_size"]
                for run in payload:
                    pts = [(float(x), float(y)) for x, y in run]
                    if tn == "highlighter":
                        ns = lbe.HighlighterStroke()
                        ns.set_points(pts)
                        ns.height = size
                    else:
                        ns = lbe.FreehandStroke()
                        ns.set_points(pts)
                        ns.stroke_width = size
                    ns.stroke_color = lbe.RGBA(c.r, c.g, c.b, c.a)
                    ns.opacity = orig.opacity
                    actions.append(("add", ns))
            elif extras.get("as_polygons"):
                # Erased ink becomes FILLED POLYGONS. A polygon can hold the
                # concave, arbitrarily-shaped remains of a subtracted stroke;
                # a polyline + one width cannot, which is the whole reason the
                # old cut had to take the full stroke width. Colour moves to
                # fill_color (the shape IS the ink now) and the outline pen is
                # switched off, so the piece paints exactly the region the
                # preview showed.
                #
                # For a highlighter the source colour lives on stroke_color and
                # its translucency on opacity; both carry across so overlapping
                # highlighter marks keep blending the way they did.
                src = (orig.fill_color if tn == "polygon" else orig.stroke_color)
                for verts in payload:
                    # The binding converts sequences itself (list_to_vec2f_vec);
                    # passing Vec2f instances is rejected.
                    np_ = lbe.PolygonObject([(float(x), float(y))
                                             for x, y in verts], True)
                    np_.fill_color = lbe.RGBA(src.r, src.g, src.b, src.a)
                    np_.stroke_color = lbe.RGBA(0, 0, 0, 0)
                    np_.stroke_width = 0.0
                    np_.opacity = orig.opacity
                    actions.append(("add", np_))
            elif tn in ("line", "arrow"):
                c = orig.stroke_color
                w = extras.get("world_width", orig.stroke_width)
                hl = extras.get("head_length", 16.0)
                hw = extras.get("head_width", 14.0)
                sc = self._transform_scale(orig.transform)
                for (sx2, sy2), (ex2, ey2), at_end in payload:
                    keep_head = at_end and tn == "arrow"
                    if keep_head:
                        no = lbe.ArrowObject(lbe.Vec2f(sx2, sy2), lbe.Vec2f(ex2, ey2))
                        no.head_length = hl * sc
                        no.head_width = hw * sc
                        no.head_at_end = True
                        no.head_at_start = False
                    else:
                        no = lbe.LineObject(lbe.Vec2f(sx2, sy2), lbe.Vec2f(ex2, ey2))
                    no.stroke_color = lbe.RGBA(c.r, c.g, c.b, c.a)
                    no.stroke_width = w
                    actions.append(("add", no))
            # rect / ellipse / image / ppt: payload empty -> just delete.
            actions.append(("delete", oid))

        # ONE undo entry for the whole gesture. push_compound needs the
        # rebuilt engine; fall back to the old per-piece pushes on an older
        # .pyd so erasing still works there (with the old undo granularity).
        try:
            stack.push_compound(scene, "Pixel erase", actions)
        except Exception:
            for kind, payload in actions:
                if kind == "add":
                    stack.push_add(scene, payload)
                else:
                    stack.push_delete(scene, payload)

        canvas.invalidate_cache()
        canvas.mark_dirty()

    def _commit_shape(self, tool):
        canvas = self._canvas; canvas.end_shape_preview()
        if not HAS_ENGINE: return
        scene = canvas.current_scene(); stack = canvas.current_stack()
        if not scene or not stack: return
        s, e = self._shape_start, self._shape_end
        if abs(e.x()-s.x()) < 3 and abs(e.y()-s.y()) < 3: return
        # The shape's own colour, from the shapes panel — not the pen's.
        sc = canvas.shape_color; col = lbe.RGBA(sc.red(),sc.green(),sc.blue(),sc.alpha())
        width = canvas.pen_width; obj = None
        if tool in sg.SHAPE_KINDS:
            # Every shape beyond the original six: a kind plus the drag.
            # Proportion-locked kinds commit the SAME end the preview showed.
            ex, ey = sg.constrain_end(tool, (s.x(), s.y()), (e.x(), e.y()))
            if not hasattr(lbe, "ShapeObject"):
                return            # an engine built before ShapeObject existed
            obj = lbe.ShapeObject(tool, lbe.Vec2f(s.x(), s.y()),
                                  lbe.Vec2f(ex, ey))
            obj.stroke_color = col; obj.stroke_width = width
        elif tool == "line":
            obj = lbe.LineObject(lbe.Vec2f(s.x(),s.y()), lbe.Vec2f(e.x(),e.y()))
            obj.stroke_color = col; obj.stroke_width = width
        elif tool == "rect":
            x0,y0 = min(s.x(),e.x()), min(s.y(),e.y())
            obj = lbe.RectObject(lbe.Vec2f(x0,y0), lbe.Vec2f(abs(e.x()-s.x()),abs(e.y()-s.y())))
            obj.stroke_color = col; obj.stroke_width = width
        elif tool == "ellipse":
            cx,cy = (s.x()+e.x())/2, (s.y()+e.y())/2
            obj = lbe.EllipseObject(lbe.Vec2f(cx,cy), lbe.Vec2f(abs(e.x()-s.x())/2,abs(e.y()-s.y())/2))
            obj.stroke_color = col; obj.stroke_width = width
        elif tool == "circle":
            # Native CircleObject — the canvas already constrained e to a square
            # corner, so |dx| == |dy|; the radius is half that side.
            cx,cy = (s.x()+e.x())/2, (s.y()+e.y())/2
            r = abs(e.x()-s.x())/2
            obj = lbe.CircleObject(lbe.Vec2f(cx,cy), r)
            obj.stroke_color = col; obj.stroke_width = width
        elif tool == "triangle":
            # Apex at top-centre of the drag rect, base spanning its bottom edge.
            # Prefer the native TriangleObject (first-class engine type); fall
            # back to PolygonObject when running against an older engine build
            # that hasn't been rebuilt with TriangleObject yet.
            x0, x1 = min(s.x(), e.x()), max(s.x(), e.x())
            y0, y1 = min(s.y(), e.y()), max(s.y(), e.y())
            if hasattr(lbe, "TriangleObject"):
                obj = lbe.TriangleObject(
                    lbe.Vec2f((x0 + x1) / 2.0, y0),   # apex
                    lbe.Vec2f(x0, y1),                 # bottom-left
                    lbe.Vec2f(x1, y1),                 # bottom-right
                )
            else:
                obj = lbe.PolygonObject()
                obj.add_vertex(lbe.Vec2f((x0 + x1) / 2.0, y0))   # apex
                obj.add_vertex(lbe.Vec2f(x0, y1))                # bottom-left
                obj.add_vertex(lbe.Vec2f(x1, y1))                # bottom-right
                obj.closed = True
            obj.stroke_color = col
            obj.stroke_width = width
        elif tool == "arrow":
            hl, hw = canvas.arrow_head_dims(width)
            obj = lbe.ArrowObject(lbe.Vec2f(s.x(),s.y()), lbe.Vec2f(e.x(),e.y()))
            obj.stroke_color = col; obj.stroke_width = width
            obj.head_length = hl; obj.head_width = hw
            obj.head_at_end = True; obj.head_at_start = False
        if obj:
            stack.push_add(scene, obj)
            canvas.invalidate_cache(); canvas.mark_dirty()

    # ================================================================
    # Erasing from a touch contact — palm, or a finger with the eraser tool
    # ================================================================
    # These three exist so a touch erase goes through the SAME mode switch the
    # mouse does. Both touch paths used to call ``_do_erase_at`` directly,
    # which is the object eraser and nothing else — so a palm always deleted
    # whole strokes, and so did a finger, no matter what the eraser menu said.
    # Pick "Simple", rub a corner off a word with your hand, and the entire
    # word vanished.
    #
    # The two modes have different SHAPES, which is why this needs helpers
    # rather than an inline ``if``. The object eraser deletes on every sample
    # and is finished the moment it returns; the pixel eraser is a
    # press / extend / commit sequence that paints a live preview in between
    # and produces one undo entry at the end. Touch had no "end" for erasing
    # at all before, because the object eraser never needed one.

    # Only ONE contact may drive a pixel erase, and these take the touch id so
    # they can enforce it. The mask is a single canvas-wide list, so a second
    # hand or a knuckle joining mid-rub has nowhere of its own to draw: letting
    # it start a drag wiped everything rubbed so far, and letting it extend the
    # existing one swept a capsule clear across the board between the two
    # contacts. The object eraser has no such state — each sample deletes and
    # is done — which is why this constraint is new rather than something the
    # touch path already had.

    def _erase_touch_press(self, scene_pt, tid, palm: bool = False) -> None:
        """Begin an erase at ``scene_pt`` in whichever mode is selected.

        ``palm``: a resting hand erases by touch even in lasso mode — a
        palm cannot draw a loop, and leaving it to do nothing would be a
        palm eraser that silently stopped working."""
        canvas = self._canvas
        if canvas.eraser_mode == "lasso" and not palm:
            if self._touch_erasing:
                return                  # one loop at a time
            canvas.begin_lasso(erase=True)
            canvas.add_lasso_point(scene_pt)
            self._touch_erasing = True
            self._touch_lassoing = True
            self._touch_erase_id = tid
            return
        if canvas.eraser_mode != "pixel":
            self._do_erase_at(scene_pt)
            return
        if self._touch_erasing:
            return                      # someone else already owns the rub
        canvas._eraser_mask_segments = []
        self._pixel_eraser_extend(scene_pt, first=True)
        self._touch_erasing = True
        self._touch_erase_id = tid

    def _erase_touch_move(self, scene_pt, tid, palm: bool = False) -> None:
        """Continue an erase. Starts one if none is in flight — a contact can
        be reclassified as a palm partway through (see the "Updated" branch of
        ``touch_event``), and in pixel mode that is the first sample of the
        drag even though it arrived on a move."""
        canvas = self._canvas
        if canvas.eraser_mode == "lasso" and not palm:
            if not self._touch_erasing:
                self._erase_touch_press(scene_pt, tid)
            elif tid == self._touch_erase_id and getattr(self, "_touch_lassoing", False):
                canvas.add_lasso_point(scene_pt)
            return
        if canvas.eraser_mode != "pixel":
            self._do_erase_at(scene_pt)
        elif not self._touch_erasing:
            self._erase_touch_press(scene_pt, tid)
        elif tid == self._touch_erase_id:
            self._pixel_eraser_extend(scene_pt)

    def _erase_touch_end(self, tid=None, cancel_lasso: bool = False) -> None:
        """Close out a touch erase. Safe to call when none is in flight.

        ``tid`` closes it only if that contact is the one driving it; omit it
        to close out unconditionally (end of the sequence, or a gesture taking
        over)."""
        if not self._touch_erasing:
            return
        if tid is not None and tid != self._touch_erase_id:
            return
        self._touch_erasing = False
        self._touch_erase_id = None
        if getattr(self, "_touch_lassoing", False):
            self._touch_lassoing = False
            pts = self._canvas.end_lasso()
            if not cancel_lasso:
                self._erase_in_lasso(pts)
            return
        self._commit_pixel_erase()

    # ================================================================
    # Lasso eraser
    # ================================================================
    # A stroke counts as inside when at least this share of its points is.
    # Not "any point": a long line that merely passes through the loop is
    # not what anyone circled. Not "every point": a loop drawn quickly
    # clips the ends of what it was meant to catch.
    LASSO_STROKE_SHARE = 0.5

    def _erase_in_lasso(self, lasso_pts) -> int:
        """Erase everything inside the loop, as ONE undo step. Returns how
        many objects went.

        What is "inside" follows the selection lasso, so the two tools agree
        about what a loop catches: an object is inside when the centre of
        its box is. Strokes are judged more strictly, by how many of their
        points are inside, because a stroke's box can be far bigger than
        the ink in it. Locked objects stay; imported media stays unless the
        "object eraser affects media" setting says otherwise."""
        canvas = self._canvas
        scene = canvas.current_scene(); stack = canvas.current_stack()
        if not scene or not stack or not HAS_ENGINE or len(lasso_pts) < 3:
            return 0
        xs = [p.x() for p in lasso_pts]; ys = [p.y() for p in lasso_pts]
        if max(xs) - min(xs) < 5 and max(ys) - min(ys) < 5:
            return 0
        aabb = lbe.AABB(lbe.Vec2f(min(xs), min(ys)), lbe.Vec2f(max(xs), max(ys)))
        affects_media = canvas.object_eraser_affects_media

        def _xy(q):
            try:
                return q.x(), q.y()
            except TypeError:
                return q.x, q.y
            except AttributeError:
                return q[0], q[1]

        doomed = []
        for obj in scene.query_rect(aabb):
            if obj.locked:
                continue
            if (not affects_media) and obj.type_name in ("image", "ppt"):
                continue
            inside = False
            pts = None
            if obj.type_name in ("freehand", "highlighter"):
                try:
                    pts = list(obj.points())
                except Exception:
                    pts = None
            if pts:
                # Sample long strokes; the share is what matters.
                step = max(1, len(pts) // 200)
                sample = pts[::step]
                hits = sum(1 for q in sample if self._pip(*_xy(q), lasso_pts))
                inside = hits >= self.LASSO_STROKE_SHARE * len(sample)
            else:
                bb = obj.bounding_box()
                cx = (bb.min_pt.x + bb.max_pt.x) / 2
                cy = (bb.min_pt.y + bb.max_pt.y) / 2
                inside = self._pip(cx, cy, lasso_pts)
            if inside:
                doomed.append(obj.id)
        if not doomed:
            canvas.update()
            return 0
        stack.push_compound(scene, "Lasso Erase",
                            [("delete", oid) for oid in doomed])
        canvas.invalidate_cache(); canvas.mark_dirty(); canvas.update()
        return len(doomed)

    # ================================================================
    # Object eraser
    # ================================================================
    def _do_erase_at(self, scene_pt):
        canvas = self._canvas; scene = canvas.current_scene(); stack = canvas.current_stack()
        if not scene or not stack or not HAS_ENGINE: return
        pt = lbe.Vec2f(scene_pt.x(), scene_pt.y())
        hits = scene.hit_test_all(pt, canvas.eraser_size/2.0)
        affects_media = canvas.object_eraser_affects_media
        for obj in hits:
            oid_str = str(obj.id)
            if oid_str in self._erased_ids or obj.locked: continue
            # By default, imported media (images, ppt) is protected from the
            # object eraser. The user can flip this in Settings.
            if (not affects_media) and obj.type_name in ("image", "ppt"):
                continue
            self._erased_ids.add(oid_str)
            stack.push_delete(scene, obj.id)
        if hits: canvas.invalidate_cache(); canvas.mark_dirty()

    # ================================================================
    # Helpers
    # ================================================================
    def _classify_touch(self, tp) -> str:
        """Full classifier verdict for a touch point: stylus / eraser / palm
        / unknown. ``_is_palm`` throws everything except the palm answer away;
        Dual Pen needs the rest.

        Returns "unknown" when nothing can be said (no calibration, or the
        frame reports no contact ellipse at all).
        """
        canvas = self._canvas
        clf = getattr(canvas, "_touch_classifier", None)
        if clf is None:
            return "unknown"
        try:
            sz = tp.ellipseDiameters()
            w, h = float(sz.width()), float(sz.height())
        except Exception:
            return "unknown"
        if w <= 0 and h <= 0:
            return "unknown"
        try:
            area = math.pi * (w / 2.0) * (h / 2.0)
            cls, _conf = clf.classify(TouchFeatures(area=area, width=w, height=h))
            return cls
        except Exception:
            return "unknown"

    def _is_dual_pen_tip_contact(self, state: str) -> bool:
        """Is this the contact that OPENS a Dual Pen interaction?

        True only for a press in dual-pen mode with nothing else already in
        flight — i.e. the one contact that must be a stylus tip, because in
        Dual Pen that is what the user is holding. Everything after it (a
        resting hand, a second student, a knuckle) is left to the normal palm
        and gesture handling.
        """
        if state != "Pressed" or self._canvas.tool != "dual_pen":
            return False
        if self._touch_drawing or self._touch_erasing:
            return False
        return not any(d.get("tool") != "ignored"
                       for d in self._active_touches.values())

    def _update_tip_from_touch(self, tp) -> None:
        """Set ``_active_tip`` from a TOUCH contact, for Dual Pen.

        Why this has to exist: ``_active_tip`` was only ever written by
        ``tablet_event`` and ``mouse_press``. That silently assumes both ends
        of the stylus arrive as tablet events — true for a real active pen,
        false for an IR touch frame, which classifies contacts by SIZE and
        routes them to different Windows digitizers. On the classroom board
        the thin front tip comes in as a tablet event but the thick back tip
        comes in as a plain touch, so nothing ever set the tip to "back" and
        flipping the pen kept using the FRONT tool.

        The eraser profile is exactly "what the back tip looks like as a
        contact", and the classifier already recognises it — that verdict was
        just being discarded. Note this needs the eraser profile calibrated to
        mean anything, which is guaranteed here: Dual Pen won't unlock without
        it (see InputCalibration.dual_pen_available).
        """
        cls = self._classify_touch(tp)
        if cls == "eraser":
            self._active_tip = "back"
        elif cls == "stylus":
            self._active_tip = "front"
        # palm / unknown: leave the last known tip alone. A palm is not a tip,
        # and guessing on "unknown" would flip the tool at random.

    def _is_palm(self, tp):
        """Decide whether a touch point is a palm contact (Phase 3).

        Uses the calibrated TouchClassifier when available, falling back to the
        legacy size heuristic otherwise. Honors the touch mode:
          • palm_eraser    → a palm verdict means 'erase with palm'.
          • palm_rejection → a palm verdict means 'ignore this touch'.
        The CALLER (touch_event) acts on the verdict; here we only classify.

        Stylus priority + false-negative bias live inside the classifier, so a
        stylus is never misread as a palm (which would erase the user's work).
        """
        canvas = self._canvas
        # Palm handling switched off — nothing is ever a palm, so every
        # contact behaves like an ordinary finger and simply draws.
        if getattr(canvas, "_touch_mode", "palm_rejection") == "palm_off":
            return False
        try:
            sz = tp.ellipseDiameters()
            w, h = float(sz.width()), float(sz.height())
        except Exception:
            return False
        if w <= 0 and h <= 0:
            return False

        clf = getattr(canvas, "_touch_classifier", None)
        if clf is not None:
            area = math.pi * (w / 2.0) * (h / 2.0)
            cls, conf = clf.classify(TouchFeatures(area=area, width=w, height=h))
            # Only pay for string formatting / logging when the dev overlay is
            # on — the hot path stays allocation-free during normal drawing.
            if getattr(canvas, "_debug_input_overlay", False):
                canvas._last_input_class = f"{cls} {conf:.2f}"
                logging.debug("touch: %.0fx%.0f area=%.0f -> %s (%.2f)",
                              w, h, area, cls, conf)
            return cls == "palm"

        # ---- legacy fallback (no classifier wired) ----
        if not canvas.palm_eraser_enabled:
            return False
        ps = canvas.palm_min_size
        return w >= ps or h >= ps or w * h >= ps * ps * 0.55

    def _split_line_world(self, sx, sy, ex, ey, eraser_segs, radius):
        """
        Split a line in WORLD coords against eraser path segments.
        Uses chord-based interval math (only erases what is physically under the brush).
        Returns list of ((x0,y0),(x1,y1), is_end_segment) or None if not erased at all.
        """
        dx = ex - sx; dy = ey - sy
        length_sq = dx*dx + dy*dy
        if length_sq < 1: return None
        length = math.sqrt(length_sq)
        r_sq = radius * radius

        # Sample the line densely and check distance to each eraser segment.
        # This handles fast eraser drags correctly.
        n_samples = max(20, int(length / max(2.0, radius / 2)))
        sample_ts = [i / n_samples for i in range(n_samples + 1)]
        # Mark each sample as erased if too close to any eraser segment
        sample_erased = [False] * len(sample_ts)
        for i, t in enumerate(sample_ts):
            sxp = sx + t*dx; syp = sy + t*dy
            for (ax, ay, bx, by) in eraser_segs:
                if self._dist_to_segment_sq(sxp, syp, ax, ay, bx, by) <= r_sq:
                    sample_erased[i] = True; break

        if not any(sample_erased): return None

        # Convert run-of-erased-samples into intervals
        erased_intervals = []
        i = 0; n = len(sample_ts)
        while i < n:
            if sample_erased[i]:
                j = i
                while j < n and sample_erased[j]: j += 1
                erased_intervals.append((sample_ts[i], sample_ts[min(j, n-1)]))
                i = j
            else:
                i += 1

        # Find surviving intervals
        surviving = []
        prev = 0.0; MIN_FRAC = 0.02
        for (a, b) in erased_intervals:
            if a - prev > MIN_FRAC: surviving.append((prev, a))
            prev = b
        if 1.0 - prev > MIN_FRAC: surviving.append((prev, 1.0))

        if not surviving: return []

        result = []
        for t0, t1 in surviving:
            x0 = sx + t0*dx; y0 = sy + t0*dy
            x1 = sx + t1*dx; y1 = sy + t1*dy
            at_end = t1 >= 0.97
            result.append(((x0, y0), (x1, y1), at_end))
        return result

    def _eraser_hits_bbox(self, bb, eraser_segs, radius):
        """Eraser-vs-bbox check (rect/ellipse fallback). Uses world AABB."""
        r_sq = radius * radius
        minx, miny = bb.min_pt.x, bb.min_pt.y
        maxx, maxy = bb.max_pt.x, bb.max_pt.y
        sides = [(minx, miny, maxx, miny), (maxx, miny, maxx, maxy),
                 (maxx, maxy, minx, maxy), (minx, maxy, minx, miny)]
        for (ax, ay, bx, by) in sides:
            for (sa, sb, sc, sd) in eraser_segs:
                # Just sample the eraser segment and check against bbox side
                for tt in (0.0, 0.25, 0.5, 0.75, 1.0):
                    px = sa + tt*(sc-sa); py = sb + tt*(sd-sb)
                    if self._dist_to_segment_sq(px, py, ax, ay, bx, by) <= r_sq:
                        return True
        return False

    @staticmethod
    def _tstate(state):
        try: return state.name
        except AttributeError: return str(state).split(".")[-1]