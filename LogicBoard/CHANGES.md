# LogicBoard — v3 redesign notes

> **Historical.** These are the engineering notes from the v3 redesign, kept
> for the reasoning behind the design. Some of it has since been replaced —
> in particular PowerPoint decks are now rendered by PowerPoint itself (see
> `ui/util/ppt_renderer.py`); LibreOffice and `python-pptx` are no longer
> needed. For what changed recently, see [CHANGELOG.md](../CHANGELOG.md).

## What changed at a glance

1. **Triangle is now a first-class engine object.** New `TriangleObject` C++
   class with its own header, .cpp, factory registration, and Python binding
   — full serialize / deserialize / clone / hit-test / bounding-box / theme
   integration. The shape tool prefers the native `lbe.TriangleObject` and
   falls back to `PolygonObject` if the engine hasn't been rebuilt yet, so
   the app keeps working before you run a build.

2. **PPT pipeline redesigned around `python-pptx`.** The original `.pptx` is
   stored verbatim and never converted. A new `ui/util/ppt_renderer.py`
   parses click-triggered animation timing, then renders one SVG per
   **(slide, build step)** by hiding shapes that haven't appeared yet,
   running LibreOffice → PDF, and converting each page to SVG via PyMuPDF.

3. **Media storage** — originals always live in `<board>/media/originals/`.
   The render cache lives in `<board>/media/cache/<id>/` and is *separate*
   from originals so it can be wiped and regenerated freely.

4. **SVG-first, WEBP fallback.** PPT slides and most PDFs render to SVG;
   raster-dominated PDFs and any SVG that balloons past 2 MB per page fall
   back to WEBP automatically.

5. **Page navigation thumbnails → WEBP** in `<board>/temps/nav_pages/`.
   Legacy `<board>/temps/page_*.png` files from old saves are migrated on
   first read.

6. **Persistent cache.** Every cache folder carries a `manifest.json` with a
   `(size, mtime)` fingerprint of the source file. On reopen, caches are
   reused — only regenerated when the source changes, the cache is missing,
   or the per-file integrity check fails.

7. **Media Manager replaces the Imports panel.** The toolbar "Imports"
   button now opens the Media Manager dialog, which is the single primary
   media interface. The old `ui/panels/imports_panel.py` was deleted.

8. **PPT navigation now steps through build-steps.** Forward steps through
   `anim_step` within a slide, then advances to the next slide (step 0).
   Backwards is the mirror. Slide changes are undoable; build-step changes
   are cheap UI state.

9. **Erased ink stays ink.** The Simple (pixel) eraser still cuts to the
   exact shape of its own footprint, but it no longer turns everything it
   touches into polygons. After subtracting, the commit rebuilds the
   survivors as `FreehandStroke` / `HighlighterStroke` fragments along the
   uncut runs of the original centreline, and keeps that answer only when it
   has *measured* that the two paint the same picture
   (`erase_geometry.runs_are_faithful`). Polygons remain for the shapes a
   stroke genuinely cannot hold — a thin eraser punching a hole through a
   fat highlighter, or a graze that bites an edge without crossing the
   centreline. On a randomised sweep, 81% of erases stay strokes; a curved
   pen stroke that used to become 647 polygon vertices now stays 51.

10. **Erasing is no longer quadratic in drag length.** It used to keep one
    ever-growing union of every eraser capsule and re-subtract that whole
    path from every touched stroke on every frame, so a long rub got slower
    the longer it went (measured 6.4 ms/frame at 100 samples rising to
    11.2 ms at 800). Each object now carries its own remainder and folds in
    only the new capsule — flat at ~1.05 ms regardless of drag length. Three
    other per-sample costs went with it: a scan that flagged covered
    centreline points for a preview that no longer read them, eager QRegion
    building for a clip only shapes use, and a full-mask rescan per object
    per frame. The eraser ring also stopped calling a bare `update()` on
    every pointer move, which had been repainting the entire canvas per
    sample and quietly undoing the dirty-rect work around it. End to end on
    a 15-object board, a 179-sample drag went from 1213 ms to 499 ms, median
    frame 5.2 ms → 2.4 ms.

## Rebuild the engine

The Triangle object is C++ source — you need to rebuild the
`logicboard_engine` Python extension to actually use it natively:

```bash
python scripts/build_engine.py
```

Until you do, the Python `_commit_shape` path silently falls back to
`PolygonObject` for triangles. Everything else (PPT pipeline, media manager,
WEBP thumbnails, SVG rendering on canvas) is pure Python and works
immediately.

## Optional Python dependencies

If you don't already have them:

```bash
pip install python-pptx PyMuPDF lxml Pillow
```

- `python-pptx` + `lxml`: animation/build-step parsing
- `PyMuPDF`: PDF → SVG / WEBP rendering
- `Pillow`: last-resort WEBP encoding fallback on older PyMuPDF builds

LibreOffice (`soffice` on PATH) is still required for PPT rendering.

## Layout of a saved board (new files marked ★)

```
<board>/
    board.json
    imports/                    -- legacy media for v1 boards (still loaded)
    media/                      ★ new
        library.json            ★ manifest of MediaItems
        originals/              ★ original .pptx / .pdf / .png (never converted)
            <id>.pptx
            <id>.pdf
        cache/                  ★ regenerable render cache
            <id>/
                manifest.json   ★ fingerprint + per-step/page index
                slide_1_step_0.svg
                slide_1_step_1.svg
                slide_2_step_0.svg
                page_001.svg
                ...
    temps/
        nav_pages/              ★ moved here, WEBP instead of PNG
            page_0000.webp
            page_0001.webp
```

## Backward compatibility

- v1 MediaLibrary manifests upgrade in-place on load
  (`preview_dir_relpath` → `cache_dir_relpath`).
- Old `<board>/temps/page_*.png` thumbnails are re-encoded as WEBP on first
  access and the PNG is removed.
- Old boards without a `media/` folder still load — their `imports/*` paths
  resolve exactly like before through `canvas._resolve_media_path`.

## File-by-file summary

### Engine (C++)
- `engine/include/scene/TriangleObject.h` — **new**
- `engine/src/scene/TriangleObject.cpp` — **new**
- `engine/src/scene/WhiteboardObject.cpp` — Triangle added to factory
- `bindings/bind_scene.cpp` — `py::class_<TriangleObject>` added

### Python — new files
- `ui/util/ppt_renderer.py` — build-step PPT renderer
- `ui/util/media_cache.py` — PDF SVG/WEBP cache + format heuristic

### Python — rewritten
- `ui/util/media_library.py` — v2 schema, originals-preserving, cache index
- `ui/util/thumbnail_manager.py` — WEBP, `temps/nav_pages/`, PNG migration
- `ui/main_window.py` — Media Manager wired in, build-step navigation,
  legacy `_import_pdf`/`_import_ppt` re-routed through MediaLibrary

### Python — edited
- `ui/canvas/canvas_widget.py` — `_r_triangle` renderer, SVG-aware
  `_get_cached_pixmap`, PPT badge shows build step
- `ui/canvas/input_handler.py` — `_commit_shape` uses TriangleObject
  with PolygonObject fallback
- `ui/panels/media_manager.py` — SVG-aware preview tiles
- `ui/toolbar/toolbar_widget.py` — "Imports" → "Media", triangle in
  shape-active group

### Removed
- `ui/panels/imports_panel.py`
