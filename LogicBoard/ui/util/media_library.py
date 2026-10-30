"""
media_library.py — Board-scoped media library (v3 — pywin32 / WEBP edition).

New storage layout (originals separate from regenerable cache):

    <board>/
      imports/                          ← originals, with their REAL names
          lecture.pptx                  ← never modified
          handout.pdf
          diagram.png
      temps/
          media/                        ← regenerable WEBP cache
              lecture/                  ← one folder per imported file
                  cache_manifest.json
                  slide_001_anim_000.webp
                  slide_001_anim_001.webp
                  slide_002_anim_000.webp
                  thumb_001.webp        ← small Media-Manager thumbnails
                  thumb_002.webp
              handout/
                  cache_manifest.json
                  page_001.webp
                  thumb_001.webp
          nav_pages/                    ← page-navigator thumbnails (WEBP)
              page_0001.webp

Why originals live in ``imports/`` (not ``media/originals/``)
────────────────────────────────────────────────────────────
* Human-readable filenames — `lecture.pptx` not `f7a3.pptx`.
* The render cache lives under ``temps/`` which the user can safely delete
  to free disk — originals survive intact.
* Stays compatible with v1/v2 boards: when a board has a v2 ``media/``
  folder, this library still loads it through the legacy code paths.

Cache reuse
───────────
Each cache folder has its own ``cache_manifest.json`` with the source file's
(size, mtime, partial-sha1) fingerprint. Reopening a board does NOT
re-render unless the source file was actually modified.

Forward-compat: unknown manifest keys are preserved on round-trip.
"""
from __future__ import annotations

import json
import logging
import re
import secrets
import shutil
from datetime import datetime, timezone
from pathlib import Path
from typing import Callable, List, Optional

logger = logging.getLogger(__name__)

__all__ = [
    "MediaItem", "MediaLibrary",
    "IMPORTS_DIRNAME", "TEMPS_DIRNAME", "TEMPS_MEDIA_SUBDIR",
    "detect_type", "human_size", "safe_name",
]

# ─────────────────────────────────────────────────────────────────────────────
# Layout constants
# ─────────────────────────────────────────────────────────────────────────────
IMPORTS_DIRNAME    = "imports"          # originals
TEMPS_DIRNAME      = "temps"            # regenerable
TEMPS_MEDIA_SUBDIR = "media"            # PPT / PDF render cache
MANIFEST_NAME      = "library.json"

# Legacy v1/v2 layout — we still load these but don't write into them.
_LEGACY_MEDIA_DIRNAME  = "media"
_LEGACY_ORIGINALS      = "originals"
_LEGACY_CACHE          = "cache"
_LEGACY_PREVIEWS       = "previews"
_LEGACY_MANIFESTS = (
    MANIFEST_NAME,                      # /<board>/imports/library.json (new)
    "library.json",                     # /<board>/media/library.json   (v2)
)

# ─────────────────────────────────────────────────────────────────────────────
# File-type detection
# ─────────────────────────────────────────────────────────────────────────────
IMAGE_EXTS = {".png", ".jpg", ".jpeg", ".bmp", ".gif", ".webp"}
PPT_EXTS   = {".ppt", ".pptx"}
PDF_EXTS   = {".pdf"}


def detect_type(path: Path) -> Optional[str]:
    ext = path.suffix.lower()
    if ext in IMAGE_EXTS: return "image"
    if ext in PDF_EXTS:   return "pdf"
    if ext in PPT_EXTS:   return "ppt"
    return None


def human_size(n: int) -> str:
    if n < 1024: return f"{n} B"
    size = float(n)
    for unit in ("KB", "MB", "GB", "TB"):
        size /= 1024
        if size < 1024:
            return f"{size:.1f} {unit}"
    return f"{size:.1f} TB"


def safe_name(stem: str, max_len: int = 60) -> str:
    """Sanitise a filename stem for use as a folder name.

    Replaces anything that's not a word-char or hyphen with an underscore,
    collapses repeats, and trims to ``max_len``. Falls back to ``"item"`` if
    the result is empty (e.g. all-emoji name)."""
    s = re.sub(r"[^\w\-]+", "_", stem).strip("_")
    s = re.sub(r"_+", "_", s)
    s = s[:max_len].rstrip("_") or "item"
    return s


# ─────────────────────────────────────────────────────────────────────────────
# MediaItem  (schema v3)
# ─────────────────────────────────────────────────────────────────────────────
class MediaItem:
    """One entry in the board's media library.

    Fields used by the new (v3) layout:
      original_relpath  : "imports/<name>.<ext>"
      cache_dir_relpath : "temps/media/<safe_name>"
      preview_count     : number of slides / pages
      anim_counts       : per-slide click count (PPT only; len == preview_count)
      cache_format      : "webp" (legacy values "svg"/"raster" still load)
      step_files        : PPT — list[slide][step] of filenames in cache_dir
      page_files        : PDF — list[page] of filenames in cache_dir
      thumb_files       : list[slide_or_page] of small thumbnail filenames

    Both ``step_files`` and ``page_files`` may be empty for legacy items —
    the ``cache_index`` legacy field carries the same info and is kept for
    one-way migration.
    """

    SCHEMA_VERSION = 3

    def __init__(self, **kwargs):
        self.id:                str       = kwargs.get("id") or secrets.token_hex(6)
        self.name:              str       = kwargs.get("name", "")
        self.type:              str       = kwargs["type"]          # required
        self.original_relpath:  str       = kwargs.get("original_relpath", "")
        self.cache_dir_relpath: str       = kwargs.get("cache_dir_relpath", "")
        self.preview_count:     int       = int(kwargs.get("preview_count", 0))
        # Pixel size of ONE rendered page/slide, as the renderer produced it.
        #
        # Kept so nothing has to open a file to find out. The canvas needs
        # each page's dimensions to lay a board out, and reading them back
        # off disk cost 9 ms PER PAGE on a cold cache — nine seconds for a
        # thousand-page PDF, every time the board was laid out. Every page
        # of one document is the same size, so one pair of numbers answers
        # for all of them.
        _ps = kwargs.get("page_size") or (0, 0)
        self.page_size: tuple = (int(_ps[0]) if len(_ps) > 0 else 0,
                                 int(_ps[1]) if len(_ps) > 1 else 0)
        self.anim_counts:       List[int] = list(kwargs.get("anim_counts", []))
        self.cache_format:      str       = str(kwargs.get("cache_format", "webp"))
        # New v3 explicit lists:
        self.step_files:        List[List[str]] = [list(s) for s in kwargs.get("step_files", [])]
        self.page_files:        List[str]       = list(kwargs.get("page_files", []))
        self.thumb_files:       List[str]       = list(kwargs.get("thumb_files", []))
        # Legacy v2 cache_index — used during one-way upgrade only.
        legacy_idx = kwargs.get("cache_index", [])
        if legacy_idx and not self.step_files and not self.page_files:
            if self.type == "ppt":
                self.step_files = [list(s) for s in legacy_idx]
            elif self.type == "pdf":
                self.page_files = list(legacy_idx)

        self.size_bytes:  int = int(kwargs.get("size_bytes", 0))
        self.imported_at: str = kwargs.get("imported_at") or datetime.now(timezone.utc).isoformat()
        self.tags:        list[str] = list(kwargs.get("tags", []))
        self.favorite:    bool      = bool(kwargs.get("favorite", False))
        # Why the last render produced nothing: "" (it worked, or never
        # ran), RENDER_ERR_NO_POWERPOINT, or RENDER_ERR_FAILED. Saved with
        # the board, so the Media Manager can say the right thing later.
        self.render_error: str = str(kwargs.get("render_error", "") or "")

        # v1 → v3 legacy field
        legacy_pdr = kwargs.get("preview_dir_relpath", "")
        if legacy_pdr and not self.cache_dir_relpath:
            self.cache_dir_relpath = legacy_pdr

        # Preserve unknown future keys for forward-compat round-trip.
        known = {
            "id","name","type",
            "original_relpath","cache_dir_relpath","preview_dir_relpath",
            "preview_count","anim_counts","cache_format","page_size",
            "step_files","page_files","thumb_files","cache_index",
            "size_bytes","imported_at","tags","favorite","schema_version",
            "render_error",
        }
        self._extra: dict = {k: v for k, v in kwargs.items() if k not in known}

    def to_dict(self) -> dict:
        d = {
            "schema_version":     self.SCHEMA_VERSION,
            "id":                 self.id,
            "name":               self.name,
            "type":               self.type,
            "original_relpath":   self.original_relpath,
            "cache_dir_relpath":  self.cache_dir_relpath,
            "preview_count":      self.preview_count,
            "page_size":          list(self.page_size),
            "anim_counts":        list(self.anim_counts),
            "cache_format":       self.cache_format,
            "step_files":         [list(s) for s in self.step_files],
            "page_files":         list(self.page_files),
            "thumb_files":        list(self.thumb_files),
            "size_bytes":         self.size_bytes,
            "imported_at":        self.imported_at,
            "tags":               self.tags,
            "favorite":           self.favorite,
        }
        if self.render_error:
            d["render_error"] = self.render_error
        d.update(self._extra)
        return d

    @classmethod
    def from_dict(cls, d: dict) -> "MediaItem":
        return cls(**d)

    @property
    def needs_render(self) -> bool:
        """A PDF or presentation with nothing rendered — the original is on
        the board, the pages are not, and Render can make them."""
        return self.type in ("pdf", "ppt") and self.preview_count == 0


RENDER_ERR_NO_POWERPOINT = "no_powerpoint"
RENDER_ERR_FAILED = "failed"


# ─────────────────────────────────────────────────────────────────────────────
# MediaLibrary
# ─────────────────────────────────────────────────────────────────────────────
class MediaLibrary:
    """Per-board media library: import → store original → render WEBP cache."""

    def __init__(self, board_dir: Path):
        self.board_dir: Path = Path(board_dir)
        self._items: List[MediaItem] = []
        self._loaded: bool = False

    # ── path helpers ────────────────────────────────────────────────────────
    @property
    def imports_dir(self)  -> Path: return self.board_dir / IMPORTS_DIRNAME
    @property
    def temps_dir(self)    -> Path: return self.board_dir / TEMPS_DIRNAME
    @property
    def cache_root(self)   -> Path: return self.temps_dir / TEMPS_MEDIA_SUBDIR
    @property
    def manifest_path(self) -> Path:
        # Manifest lives WITH the originals so deleting `temps/` doesn't lose it.
        return self.imports_dir / MANIFEST_NAME

    def absolute_path(self, relpath: str) -> Optional[Path]:
        return self.board_dir / relpath if relpath else None

    def item_cache_dir(self, item: MediaItem) -> Optional[Path]:
        if not item.cache_dir_relpath:
            return None
        return self.absolute_path(item.cache_dir_relpath)

    def _ensure_dirs(self) -> None:
        self.imports_dir.mkdir(parents=True, exist_ok=True)
        self.cache_root.mkdir(parents=True, exist_ok=True)

    # ── load / save ─────────────────────────────────────────────────────────
    def load(self) -> None:
        self._ensure_dirs()
        self._items.clear()

        # Prefer the new manifest location; fall back to v2 (under `media/`).
        new_manifest = self.manifest_path
        legacy_manifest = (self.board_dir / _LEGACY_MEDIA_DIRNAME / MANIFEST_NAME)

        used = new_manifest if new_manifest.exists() else (
            legacy_manifest if legacy_manifest.exists() else None
        )
        if used is not None:
            try:
                data = json.loads(used.read_text(encoding="utf-8"))
                for entry in data.get("items", []):
                    self._items.append(MediaItem.from_dict(entry))
            except Exception as e:
                logger.warning("media manifest load: %s", e)

        # If we loaded from the legacy manifest, save a copy at the new
        # location so we don't keep reading from the old place.
        if used is legacy_manifest:
            try: self.save()
            except OSError as e: logger.warning("manifest migrate: %s", e)

        # Backfill on-disk size for items whose persisted ``size_bytes`` only
        # reflects the source file (pre-fix imports). We detect this by
        # checking whether the cache directory exists AND the persisted size
        # is suspiciously close to the source size — but the simplest, most
        # reliable thing is to just recompute on every load. The walk is
        # cheap (one stat per cached WEBP) and runs once at startup.
        changed = False
        for it in self._items:
            old = it.size_bytes
            new = self._compute_disk_footprint(it)
            if new and new != old:
                it.size_bytes = new
                changed = True
        if changed:
            try: self.save()
            except OSError as e: logger.warning("manifest size-backfill: %s", e)

        self._loaded = True

    def save(self) -> None:
        self._ensure_dirs()
        payload = {
            "schema_version": MediaItem.SCHEMA_VERSION,
            "items":          [it.to_dict() for it in self._items],
        }
        tmp = self.manifest_path.with_suffix(".json.tmp")
        tmp.write_text(json.dumps(payload, indent=2), encoding="utf-8")
        tmp.replace(self.manifest_path)

    # ── queries ─────────────────────────────────────────────────────────────
    def items(self) -> List[MediaItem]:
        if not self._loaded: self.load()
        return list(self._items)

    def get(self, item_id: str) -> Optional[MediaItem]:
        if not self._loaded: self.load()
        return next((it for it in self._items if it.id == item_id), None)

    def find_by_original(self, original_relpath: str) -> Optional[MediaItem]:
        if not original_relpath: return None
        if not self._loaded: self.load()
        return next((it for it in self._items
                     if it.original_relpath == original_relpath), None)

    def describe_background(self, relpath: str) -> dict:
        """Resolve a scene background path into human-readable info.

        Returns a dict with keys:
          title  : short label, e.g. "PPT Slide 3", "PDF Page 8",
                   "Image Background", "Default Background", "Custom Background"
          detail : optional second line (source file name), or "" if none.

        Matching strategy (no fragile parsing): find which library item owns
        the path by comparing against each item's original path and cache dir,
        then read the slide/page number from the filename's NNN component.
        """
        if not relpath:
            return {"title": "Default Background", "detail": ""}
        if not self._loaded:
            self.load()

        rp = relpath.replace("\\", "/")
        fname = rp.rsplit("/", 1)[-1]

        # 1) Direct image background — matches an item's original path.
        for it in self._items:
            if it.original_relpath and it.original_relpath.replace("\\", "/") == rp:
                if it.type == "image":
                    return {"title": "Image Background", "detail": it.name}
                # original of a ppt/pdf used directly (rare)
                return {"title": f"{it.type.upper()} Background", "detail": it.name}

        # 2) Slide / page render — path sits inside an item's cache dir.
        def _num_from(stem: str) -> Optional[int]:
            # slide_003_anim_000 -> 3 ; page_008 -> 8
            import re
            m = re.search(r"(?:slide|page)_0*(\d+)", stem)
            return int(m.group(1)) if m else None

        for it in self._items:
            cdir = (it.cache_dir_relpath or "").replace("\\", "/")
            if cdir and rp.startswith(cdir.rstrip("/") + "/"):
                n = _num_from(fname)
                if it.type == "ppt":
                    title = f"PPT Slide {n}" if n else "PPT Slide"
                    return {"title": title, "detail": it.name}
                if it.type == "pdf":
                    title = f"PDF Page {n}" if n else "PDF Page"
                    return {"title": title, "detail": it.name}
                return {"title": f"{it.type.upper()} Background", "detail": it.name}

        # 3) Fallback: a path we can't attribute to a known item.
        n = _num_from(fname)
        if "slide" in fname:
            return {"title": f"PPT Slide {n}" if n else "PPT Slide", "detail": fname}
        if "page" in fname:
            return {"title": f"PDF Page {n}" if n else "PDF Page", "detail": fname}
        return {"title": "Custom Background", "detail": fname}

    # ── PREVIEW PATH RESOLUTION ─────────────────────────────────────────────
    def background_anim_info(self, relpath: str):
        """If `relpath` is a PPT slide render with animation build-steps,
        return ``{item_id, slide, step, total_steps}`` (0-based slide/step),
        else ``None``. Used to drive the background animation-step controls."""
        if not relpath:
            return None
        if not self._loaded:
            self.load()
        import re
        rp = relpath.replace("\\", "/")
        fname = rp.rsplit("/", 1)[-1]
        m = re.search(r"slide_0*(\d+)_anim_0*(\d+)", fname)
        if not m:
            return None
        slide = int(m.group(1)) - 1     # filenames are 1-based slides
        step  = int(m.group(2))         # anim steps are 0-based
        for it in self._items:
            cdir = (it.cache_dir_relpath or "").replace("\\", "/")
            if it.type == "ppt" and cdir and rp.startswith(cdir.rstrip("/") + "/"):
                total = (it.anim_counts[slide] + 1) if 0 <= slide < len(it.anim_counts) else 1
                return {"item_id": it.id, "slide": slide, "step": step,
                        "total_steps": total}
        return None

    def preview_paths(self, item: MediaItem) -> List[Path]:
        """Per-slide / per-page preview paths used by the Media Manager grid.

        PPT: prefers the small ``thumb_*.webp`` files (RAM-friendly). Falls
             back to the final-state ``slide_*_anim_*.webp`` if thumbs are
             missing.
        PDF: returns the per-page WEBP renders. (No separate thumbs needed —
             the renders are already only a few hundred KB each.)
        IMG: returns the original file path.
        """
        if item.type == "image":
            p = self.absolute_path(item.original_relpath)
            return [p] if p and p.exists() else []

        cache = self.item_cache_dir(item)
        if not cache or not cache.exists():
            return []

        if item.type == "ppt":
            # Prefer thumbnails (smaller, faster to decode).
            if item.thumb_files:
                paths = [cache / fn for fn in item.thumb_files]
                if all(p.exists() for p in paths):
                    return paths
            # Fall back to the final step of each slide.
            result: List[Path] = []
            for slide_steps in item.step_files:
                if not slide_steps:
                    continue
                p = cache / slide_steps[-1]
                if p.exists():
                    result.append(p)
            return result

        if item.type == "pdf":
            return [cache / fn for fn in item.page_files
                    if (cache / fn).exists()]

        return []

    def step_path(self, item: MediaItem, slide_idx: int,
                   step_idx: int) -> Optional[Path]:
        if item.type != "ppt":
            return None
        cache = self.item_cache_dir(item)
        if not cache or not cache.exists():
            return None
        if 0 <= slide_idx < len(item.step_files):
            steps = item.step_files[slide_idx]
            if 0 <= step_idx < len(steps):
                p = cache / steps[step_idx]
                if p.exists():
                    return p
        return None

    def page_path(self, item: MediaItem, page_idx: int) -> Optional[Path]:
        if item.type != "pdf":
            return None
        cache = self.item_cache_dir(item)
        if not cache or not cache.exists():
            return None
        if 0 <= page_idx < len(item.page_files):
            p = cache / item.page_files[page_idx]
            if p.exists():
                return p
        return None

    # ── IMPORT ──────────────────────────────────────────────────────────────
    def import_file(
        self,
        source: Path,
        on_progress: Optional[Callable[[str, float], None]] = None,
        with_animations: bool = False,
    ) -> Optional[MediaItem]:
        """Copy original to ``imports/`` and build the WEBP cache.

        ``on_progress(message, fraction)`` is forwarded to the renderer so
        the UI can show real progress through PPT slide/build-step rendering.
        """
        if not self._loaded: self.load()
        source = Path(source)
        if not source.exists():
            logger.warning("import_file: missing source %s", source)
            return None

        kind = detect_type(source)
        if kind is None:
            logger.warning("import_file: unsupported file %s", source.name)
            return None

        self._ensure_dirs()
        if on_progress: on_progress("Copying original…", 0.01)

        # ── Copy original → imports/<name> (collision-suffixed if needed) ──
        original_abs = self._claim_original_path(source.name)
        try:
            shutil.copy2(source, original_abs)
        except Exception as e:
            logger.error("original copy failed: %s", e)
            return None

        item = MediaItem(
            name=source.name,
            type=kind,
            size_bytes=source.stat().st_size,
            original_relpath=f"{IMPORTS_DIRNAME}/{original_abs.name}",
        )

        # ── Decide cache folder name (human-readable; collision-suffixed) ──
        cache_stem  = safe_name(Path(original_abs.name).stem)
        cache_abs   = self._claim_cache_dir(cache_stem)
        item.cache_dir_relpath = (f"{TEMPS_DIRNAME}/{TEMPS_MEDIA_SUBDIR}/"
                                   f"{cache_abs.name}")

        # ── Build cache ─────────────────────────────────────────────────────
        if kind == "image":
            item.preview_count = 1
            item.cache_format  = "raster"
            # No render needed — the original IS the visual.
        elif kind == "pdf":
            self._build_pdf_cache(item, original_abs, cache_abs,
                                   on_progress=on_progress)
        elif kind == "ppt":
            self._build_ppt_cache(item, original_abs, cache_abs,
                                   on_progress=on_progress,
                                   with_animations=with_animations)

        # Recompute size to include the cache footprint (animated PPT imports
        # generate many more WEBP frames than non-animated, and that has to
        # show up in the Media Manager). Done BEFORE save() so the persisted
        # value matches what the panel will read.
        self.refresh_size_bytes(item)

        # ── Register ────────────────────────────────────────────────────────
        self._items.append(item)
        self.save()
        if on_progress: on_progress("Done", 1.0)
        return item

    def _claim_original_path(self, filename: str) -> Path:
        """Return a path in ``imports/`` that doesn't collide with existing
        files. ``lecture.pptx`` → ``lecture.pptx`` (or ``lecture_2.pptx`` …)."""
        base   = Path(filename).stem
        ext    = Path(filename).suffix
        target = self.imports_dir / filename
        n = 2
        while target.exists():
            target = self.imports_dir / f"{base}_{n}{ext}"
            n += 1
        return target

    def _claim_cache_dir(self, stem: str) -> Path:
        target = self.cache_root / stem
        n = 2
        while target.exists() and any(target.iterdir()):
            target = self.cache_root / f"{stem}_{n}"
            n += 1
        target.mkdir(parents=True, exist_ok=True)
        return target

    # ── REGENERATION (used when cache was wiped externally) ─────────────────
    def regenerate_cache(self, item_id: str, force: bool = False,
                          on_progress: Optional[Callable[[str, float], None]] = None,
                          with_animations: bool = False,
                          ) -> bool:
        item = self.get(item_id)
        if item is None:                          return False
        if item.type == "image":                  return True
        original_abs = self.absolute_path(item.original_relpath)
        if not original_abs or not original_abs.exists():
            return False
        cache_abs = self.item_cache_dir(item) or (
            self.cache_root / safe_name(Path(original_abs.name).stem))
        cache_abs.mkdir(parents=True, exist_ok=True)
        item.cache_dir_relpath = self._board_relative(cache_abs)
        if item.type == "pdf":
            self._build_pdf_cache(item, original_abs, cache_abs,
                                   on_progress=on_progress, force=force)
        elif item.type == "ppt":
            self._build_ppt_cache(item, original_abs, cache_abs,
                                   on_progress=on_progress, force=force,
                                   with_animations=with_animations)
        # Cache may have grown / shrunk — keep size_bytes truthful.
        self.refresh_size_bytes(item)
        self.save()
        return True

    def ensure_cache(self, item_id: str) -> bool:
        return self.regenerate_cache(item_id, force=False)

    def _board_relative(self, abs_path: Path) -> str:
        try:
            return abs_path.resolve().relative_to(
                self.board_dir.resolve()).as_posix()
        except Exception:
            return str(abs_path)

    # ── Disk footprint accounting ───────────────────────────────────────────
    def _compute_disk_footprint(self, item: "MediaItem") -> int:
        """Total bytes consumed on disk by this media item: the original
        source file PLUS every cached preview (PDF page WEBPs, PPT slide /
        animation-build WEBPs, the cache_manifest.json, etc).

        The previous implementation only stored ``source.stat().st_size`` at
        import time, which made two PPT imports of the same file appear
        identical even when one was imported WITH animations and had 5–10×
        more WEBP frames on disk than the other. Walking the cache directory
        gives the user the truth.

        Returns total bytes; never raises (best-effort).
        """
        total = 0
        try:
            src = self.absolute_path(item.original_relpath)
            if src and src.is_file():
                total += src.stat().st_size
        except OSError:
            pass
        try:
            cache_dir = self.item_cache_dir(item)
            if cache_dir and cache_dir.is_dir():
                for p in cache_dir.rglob("*"):
                    if p.is_file():
                        try:    total += p.stat().st_size
                        except OSError: continue
        except OSError:
            pass
        return total

    def refresh_size_bytes(self, item: "MediaItem") -> None:
        """Recompute and store the current on-disk footprint. Cheap enough to
        call after every cache build; the panel reads ``item.size_bytes``
        when it draws the row, so once this runs the displayed size is right.
        """
        item.size_bytes = self._compute_disk_footprint(item)

    # ── Cache builders (delegate to ppt_renderer / media_cache) ─────────────
    def _build_pdf_cache(self, item: MediaItem,
                          original_abs: Path, cache_abs: Path,
                          on_progress=None, force: bool = False) -> None:
        try:
            from ui.util.media_cache import render_pdf_with_cache
        except Exception as e:
            logger.error("media_cache import: %s", e)
            item.preview_count = 0
            return
        try:
            res = render_pdf_with_cache(original_abs, cache_abs,
                                         force=force, on_progress=on_progress)
            item.preview_count = res.page_count
            item.page_size     = tuple(res.render_size or (0, 0))
            item.cache_format  = res.format
            item.page_files    = list(res.page_files)
            item.thumb_files   = list(res.thumb_files)
            item.anim_counts   = []
            item.step_files    = []
            item.render_error  = "" if item.preview_count else RENDER_ERR_FAILED
        except Exception as e:
            logger.error("PDF cache build failed: %s", e)
            item.preview_count = 0
            item.render_error  = RENDER_ERR_FAILED

    def _build_ppt_cache(self, item: MediaItem,
                          original_abs: Path, cache_abs: Path,
                          on_progress=None, force: bool = False,
                          with_animations: bool = False) -> None:
        try:
            from ui.util.ppt_renderer import (render_ppt_with_build_steps,
                                                PptRenderError,
                                                powerpoint_installed)
        except Exception as e:
            logger.error("ppt_renderer import: %s", e)
            item.preview_count = 0
            item.render_error = RENDER_ERR_FAILED
            return
        if not powerpoint_installed():
            # Not worth trying: without PowerPoint the render can only fail,
            # and the reason is the one thing worth telling the teacher.
            logger.warning("PPT not rendered: PowerPoint is not installed")
            item.preview_count = 0
            item.render_error = RENDER_ERR_NO_POWERPOINT
            return
        try:
            res = render_ppt_with_build_steps(
                original_abs, cache_abs,
                force=force, with_animations=with_animations,
                on_progress=on_progress)
            item.preview_count = res.slide_count
            item.page_size     = tuple(res.render_size or (0, 0))
            item.cache_format  = res.format
            item.anim_counts   = list(res.anim_counts)
            item.step_files    = [list(s) for s in res.step_files]
            item.thumb_files   = list(res.thumb_files)
            item.page_files    = []
            item.render_error  = "" if item.preview_count else RENDER_ERR_FAILED
        except PptRenderError as e:
            logger.error("PPT cache build failed: %s", e)
            item.preview_count = 0
            item.render_error = (RENDER_ERR_FAILED if powerpoint_installed()
                                 else RENDER_ERR_NO_POWERPOINT)
        except Exception as e:
            logger.error("PPT cache unexpected error: %s", e)
            item.preview_count = 0
            item.render_error = RENDER_ERR_FAILED

    # ── DELETE ──────────────────────────────────────────────────────────────
    def delete(self, item_id: str) -> bool:
        item = self.get(item_id)
        if item is None:
            return False
        try:
            orig = self.absolute_path(item.original_relpath)
            if orig and orig.exists(): orig.unlink()
        except Exception as e:
            logger.warning("delete original: %s", e)
        try:
            cache = self.item_cache_dir(item)
            if cache and cache.exists():
                shutil.rmtree(cache, ignore_errors=True)
        except Exception as e:
            logger.warning("delete cache: %s", e)
        self._items.remove(item)
        self.save()
        return True

    # ── Helpers used by the Media Manager ───────────────────────────────────
    def rename(self, item_id: str, new_name: str) -> bool:
        item = self.get(item_id)
        if item is None: return False
        item.name = new_name
        self.save()
        return True

    def set_favorite(self, item_id: str, favorite: bool) -> bool:
        item = self.get(item_id)
        if item is None: return False
        item.favorite = favorite
        self.save()
        return True

    def set_tags(self, item_id: str, tags: list[str]) -> bool:
        item = self.get(item_id)
        if item is None: return False
        item.tags = list(tags)
        self.save()
        return True