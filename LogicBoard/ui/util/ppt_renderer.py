"""
ppt_renderer.py — PowerPoint rendering via pywin32 COM (Windows-only).

Pipeline:
    Original PPTX  →  PowerPoint COM (pywin32)  →  PNG export per slide/step
                                                ↓
                                          Pillow → WEBP cache
                                                ↓
                                          temps/media/<name>/
                                            slide_001_anim_000.webp
                                            slide_001_anim_001.webp
                                            thumb_001.webp
                                            cache_manifest.json

Design notes
────────────
* PowerPoint's `Slide.Export()` filter list does NOT include WEBP. We export
  PNG to a temp directory, then re-encode via Pillow (~30 % smaller than PNG
  at indistinguishable quality, which matters on an 8 GB smart-board).
* For animation BUILD STEPS we walk `Slide.TimeLine.MainSequence`, find the
  click-triggered groups, hide shapes that haven't entered yet via
  `shape.Visible = msoFalse`, export, then restore. The original .pptx is
  never modified.
* Adaptive resolution: we render at the primary display's native width
  (capped at 3840 px) instead of an unconditional 4 K — saves significant
  RAM/disk on lower-DPI machines.
* COM lifecycle is the safety-critical part. `_PowerPointSession` is a
  context manager so even on an unhandled exception POWERPNT.EXE gets
  closed and `CoUninitialize` is called. We also `gc.collect()` between
  the COM-release and Uninitialize to make sure the wrapper objects let
  go of their interface pointers.
* Cache reuse: a `cache_manifest.json` carries the source file's
  (size, mtime, sha1-of-first-1MB) fingerprint. On reopen we don't render
  unless the source actually changed.

Public API (unchanged signature so media_library doesn't have to be touched):

    render_ppt_with_build_steps(source_pptx, cache_dir, *,
                                force=False, on_progress=None)
        → PptRenderResult

    fingerprint_file(path)              → dict
    is_cache_fresh(source, cache_dir)   → bool
    have_powerpoint()                   → bool
"""
from __future__ import annotations

import atexit
import gc
import hashlib
import io
import json
import logging
import os
import shutil
import sys
import tempfile
from concurrent.futures import ThreadPoolExecutor
from dataclasses import dataclass, field
from pathlib import Path
from typing import Callable, List, Optional, Tuple

log = logging.getLogger(__name__)

# ── Windows-only deps (pywin32 + Pillow) ─────────────────────────────────────
try:
    import pythoncom               # type: ignore
    import win32com.client          # type: ignore
    _HAS_COM = True
except ImportError:
    _HAS_COM = False

try:
    from PIL import Image           # type: ignore
    _HAS_PIL = True
except ImportError:
    _HAS_PIL = False


# PowerPoint COM constants (subset of msocore.h that we actually use)
msoFalse = 0
msoTrue  = -1
ppActionFirstSlide   = 1     # not used directly but documented for reference
ppEffectAppear       = 3


__all__ = [
    "PptRenderResult", "PptRenderError",
    "fingerprint_file", "is_cache_fresh",
    "render_ppt_with_build_steps", "have_powerpoint", "powerpoint_installed",
    "NO_POWERPOINT_MESSAGE",
]


# ════════════════════════════════════════════════════════════════════════════
# Result + error types
# ════════════════════════════════════════════════════════════════════════════

class PptRenderError(RuntimeError):
    pass


@dataclass
class PptRenderResult:
    slide_count:  int               = 0
    anim_counts:  List[int]         = field(default_factory=list)
    step_files:   List[List[str]]   = field(default_factory=list)
    thumb_files:  List[str]         = field(default_factory=list)
    format:       str               = "webp"
    render_size:  Tuple[int, int]   = (0, 0)

    def step_file(self, slide_idx: int, step_idx: int) -> Optional[str]:
        if 0 <= slide_idx < len(self.step_files):
            steps = self.step_files[slide_idx]
            if 0 <= step_idx < len(steps):
                return steps[step_idx]
        return None

    def thumb_file(self, slide_idx: int) -> Optional[str]:
        if 0 <= slide_idx < len(self.thumb_files):
            return self.thumb_files[slide_idx]
        return None

    def to_dict(self) -> dict:
        return {
            "slide_count":  self.slide_count,
            "anim_counts":  list(self.anim_counts),
            "step_files":   [list(s) for s in self.step_files],
            "thumb_files":  list(self.thumb_files),
            "format":       self.format,
            "render_size":  list(self.render_size),
        }

    @classmethod
    def from_dict(cls, d: dict) -> "PptRenderResult":
        rs = tuple(d.get("render_size", [0, 0]))[:2] or (0, 0)
        return cls(
            slide_count = int(d.get("slide_count", 0)),
            anim_counts = list(d.get("anim_counts", [])),
            step_files  = [list(s) for s in d.get("step_files", [])],
            thumb_files = list(d.get("thumb_files", [])),
            format      = str(d.get("format", "webp")),
            render_size = (int(rs[0]) if len(rs) > 0 else 0,
                           int(rs[1]) if len(rs) > 1 else 0),
        )


# ════════════════════════════════════════════════════════════════════════════
# Dependency probes
# ════════════════════════════════════════════════════════════════════════════

# What to tell a teacher whose computer has no PowerPoint. One wording,
# used by the import alerts, the Media Manager and the Render action alike.
NO_POWERPOINT_MESSAGE = (
    "LogicBoard needs Microsoft PowerPoint to render PowerPoint files, and "
    "PowerPoint isn't installed on this computer — so this presentation "
    "couldn't be rendered. Install PowerPoint, then press Render in the "
    "Media Manager.")


def powerpoint_installed() -> bool:
    """Is Microsoft PowerPoint registered for automation on this computer?

    Read from the registry (HKCR\\PowerPoint.Application\\CLSID), which the
    PowerPoint installer writes and uninstalling removes. Instant, and it
    does not start PowerPoint. ``have_powerpoint`` used to check only that
    pywin32 and Pillow were importable — true on every LogicBoard build —
    so a board without PowerPoint went on to try the render, failed to
    start PowerPoint, and told the teacher only that the file "couldn't be
    read".
    """
    if sys.platform != "win32":
        return False
    try:
        import winreg
        with winreg.OpenKey(winreg.HKEY_CLASSES_ROOT,
                            r"PowerPoint.Application\CLSID"):
            return True
    except OSError:
        return False


def have_powerpoint() -> bool:
    """True iff PowerPoint can actually render here: Windows, pywin32,
    Pillow — and PowerPoint itself installed."""
    if sys.platform != "win32":
        return False
    return _HAS_COM and _HAS_PIL and powerpoint_installed()


# ════════════════════════════════════════════════════════════════════════════
# Cache fingerprint (size + mtime + sha1 of first 1 MB — fast + collision-safe)
# ════════════════════════════════════════════════════════════════════════════

_HASH_SAMPLE_BYTES = 1024 * 1024   # 1 MB


def fingerprint_file(path: Path) -> dict:
    p = Path(path)
    try:
        st = p.stat()
    except OSError:
        return {"size": 0, "mtime": 0, "sha1_1mb": "", "name": getattr(p, "name", "")}

    h = hashlib.sha1()
    try:
        with p.open("rb") as f:
            h.update(f.read(_HASH_SAMPLE_BYTES))
    except OSError:
        pass

    return {
        "size":      st.st_size,
        "mtime":     int(st.st_mtime),
        "sha1_1mb":  h.hexdigest(),
        "name":      p.name,
    }


def is_cache_fresh(source: Path, cache_dir: Path) -> bool:
    manifest = Path(cache_dir) / "cache_manifest.json"
    if not manifest.exists():
        return False
    try:
        data = json.loads(manifest.read_text(encoding="utf-8"))
    except Exception:
        return False
    src_fp    = fingerprint_file(Path(source))
    cached_fp = data.get("fingerprint") or {}
    if src_fp.get("size")  != cached_fp.get("size"):  return False
    # CONTENT decides, not the clock.
    #
    # This used to also require an exact mtime match, which quietly threw the
    # cache away every time a saved board was reopened: a .lb is a zip, and
    # ``zipfile.extractall`` does NOT restore timestamps, so the extracted
    # .pptx gets "now" as its mtime while the manifest — being data — still
    # carries the mtime from import. Byte-identical file, mismatched clock,
    # verdict "stale". Every Set As Background after opening a board then
    # relaunched PowerPoint and re-rendered the entire deck (tens of seconds
    # for a 50 MB deck, plus a POWERPNT.EXE per attempt) to produce images
    # that were already sitting on disk.
    #
    # Same size AND the same sha1 over the first megabyte is a far stronger
    # statement about identity than a timestamp. mtime is kept only as the
    # fallback for legacy manifests written before hashes existed.
    if src_fp.get("sha1_1mb") and cached_fp.get("sha1_1mb"):
        if src_fp["sha1_1mb"] != cached_fp["sha1_1mb"]:
            return False
    elif src_fp.get("mtime") != cached_fp.get("mtime"):
        return False
    # Spot-check listed files actually exist.
    cd = Path(cache_dir)
    for rel in data.get("files", []):
        if not (cd / rel).exists():
            return False
    return True


# ════════════════════════════════════════════════════════════════════════════
# Display-adaptive render resolution
# ════════════════════════════════════════════════════════════════════════════

# Cap render width — never wastes RAM on >4K rasters.
_MAX_RENDER_WIDTH = 3840
_DEFAULT_WIDTH    = 1920
# Media-Manager thumbnails are intentionally small (RAM-friendly).
_THUMB_WIDTH      = 512


def _detect_display_width() -> int:
    """Primary display width in CSS pixels, capped at _MAX_RENDER_WIDTH."""
    try:
        from PySide6.QtGui import QGuiApplication
        app = QGuiApplication.instance()
        if app is not None:
            screen = app.primaryScreen()
            if screen:
                # Use the geometry's pixel width (already accounts for DPI on
                # most Windows setups).
                w = int(screen.geometry().width() * screen.devicePixelRatio())
                return min(max(_DEFAULT_WIDTH, w), _MAX_RENDER_WIDTH)
    except Exception as e:
        log.debug("display detect: %s", e)
    return _DEFAULT_WIDTH


# ════════════════════════════════════════════════════════════════════════════
# PNG → WEBP conversion (slides are flat → use RGB, save ~25 %)
# ════════════════════════════════════════════════════════════════════════════

def _png_to_webp(png_path: Path, webp_path: Path,
                 *, quality: int = 85, lossless: bool = False) -> bool:
    if not _HAS_PIL:
        return False
    try:
        # ``with Image.open(...)`` is essential on Windows — PIL uses lazy
        # loading and can hold the source file open until GC otherwise,
        # which makes the ``png_path.unlink()`` call in ``_convert_task``
        # fail silently with PermissionError (swallowed by the
        # ``except OSError: pass`` there → orphan PNGs pile up in the temp
        # dir undetected).
        with Image.open(png_path) as img:
            # Flatten alpha against white so slides stay readable on dark canvases.
            if img.mode in ("RGBA", "LA", "P"):
                bg = Image.new("RGB", img.size, "white")
                if img.mode == "P":
                    img = img.convert("RGBA")
                mask = img.split()[-1] if img.mode == "RGBA" else None
                bg.paste(img, mask=mask)
                img = bg
            elif img.mode != "RGB":
                img = img.convert("RGB")
            webp_path.parent.mkdir(parents=True, exist_ok=True)
            if lossless:
                img.save(str(webp_path), "WEBP", lossless=True, method=4)
            else:
                # ``method`` only changes how hard the encoder SEARCHES for a
                # smaller file — never the decoded pixels — and the search
                # cost climbs steeply for almost nothing in return. Measured
                # on a 1920 px slide:
                #
                #     method=2   211 ms   218 KB    <- chosen
                #     method=3   550 ms   211 KB    <- previous
                #     method=4   550 ms   211 KB
                #     method=6   672 ms   209 KB
                #
                # 3 -> 2 is 2.6x faster for 3% more disk, and encoding is
                # ~66% of the whole PPT import (COM export is only 25%), so
                # this is the single biggest lever here. Same value the PDF
                # pipeline uses — see media_cache._WEBP_METHOD.
                img.save(str(webp_path), "WEBP", quality=quality, method=2)
        return webp_path.exists() and webp_path.stat().st_size > 0
    except Exception as e:
        log.warning("PNG→WEBP failed (%s): %s", png_path.name, e)
        return False


def _png_thumb_to_webp(png_path: Path, webp_path: Path,
                        max_width: int = _THUMB_WIDTH,
                        quality: int = 75) -> bool:
    if not _HAS_PIL:
        return False
    try:
        # See ``_png_to_webp`` for why the context manager matters.
        with Image.open(png_path) as img:
            if img.mode in ("RGBA", "LA", "P"):
                bg = Image.new("RGB", img.size, "white")
                if img.mode == "P": img = img.convert("RGBA")
                mask = img.split()[-1] if img.mode == "RGBA" else None
                bg.paste(img, mask=mask)
                img = bg
            elif img.mode != "RGB":
                img = img.convert("RGB")
            # Aspect-preserving thumbnail
            ratio = max_width / img.width if img.width > 0 else 1.0
            new_h = max(1, int(img.height * ratio))
            # ``reducing_gap`` lets Pillow box-reduce down to roughly 2x the
            # target first, so the expensive LANCZOS pass runs over far fewer
            # source pixels. Filtering every one of 1920x1080 at full weight
            # is wasted work for a 512 px thumbnail nobody inspects closely.
            img = img.resize((max_width, new_h), Image.LANCZOS,
                             reducing_gap=2.0)
            webp_path.parent.mkdir(parents=True, exist_ok=True)
            img.save(str(webp_path), "WEBP", quality=quality, method=2)
        return webp_path.exists() and webp_path.stat().st_size > 0
    except Exception as e:
        log.warning("Thumb PNG→WEBP failed (%s): %s", png_path.name, e)
        return False


def _convert_task(png_path: Path, webp_path: Path, thumb_path: Optional[Path],
                  s_idx: int, step: int):
    """Worker run on a thread pool: convert one exported PNG to WEBP (plus a
    thumbnail when requested), then delete the source PNG. Pure Pillow work —
    no COM — so it is safe off the main thread. Returns a tuple the caller uses
    to reassemble the (slide, step) → filename mapping in the original order."""
    full_name = webp_path.name if _png_to_webp(png_path, webp_path) else None
    thumb_name = None
    if thumb_path is not None and _png_thumb_to_webp(png_path, thumb_path):
        thumb_name = thumb_path.name
    try:
        png_path.unlink()
    except OSError:
        pass
    return (s_idx, step, full_name, thumb_name)


# ════════════════════════════════════════════════════════════════════════════
# POWERPNT.EXE process bookkeeping
# ════════════════════════════════════════════════════════════════════════════
#
# ``Application.Quit()`` is a POLITE REQUEST sent over COM, not a guarantee.
# It is ignored or deferred whenever PowerPoint decides it is busy — an export
# still flushing, a modal "do you want to save" prompt behind the scenes, a
# wrapper object released a moment too late — and the process then sits there
# holding a few hundred MB and a lock on the user's deck. One orphan per
# render, and they accumulate silently until the machine is out of RAM.
#
# So each session records exactly WHICH POWERPNT.EXE it is responsible for,
# gives Quit() a few seconds to work, and terminates whatever is left. Two
# rules keep this safe:
#
#   * only pids that appeared while WE were starting are ever touched, so a
#     teacher's own open PowerPoint is never in the set (DispatchEx already
#     guarantees ours is a separate process from theirs);
#   * the set is module-level and swept again at interpreter exit, so a crash
#     mid-render can't leave one behind either.
_OUR_PPT_PIDS: set = set()

_PROCESS_TERMINATE = 0x0001
_SYNCHRONIZE       = 0x00100000
_WAIT_OBJECT_0     = 0x00000000


def _k32():
    import ctypes
    from ctypes import wintypes
    k = ctypes.WinDLL("kernel32", use_last_error=True)
    # Handles are pointer-sized. Without these restypes ctypes truncates them
    # to a 32-bit int on 64-bit Python and every call silently fails.
    k.CreateToolhelp32Snapshot.restype  = wintypes.HANDLE
    k.CreateToolhelp32Snapshot.argtypes = [wintypes.DWORD, wintypes.DWORD]
    k.OpenProcess.restype   = wintypes.HANDLE
    k.OpenProcess.argtypes  = [wintypes.DWORD, wintypes.BOOL, wintypes.DWORD]
    k.CloseHandle.argtypes  = [wintypes.HANDLE]
    k.WaitForSingleObject.argtypes = [wintypes.HANDLE, wintypes.DWORD]
    k.TerminateProcess.argtypes    = [wintypes.HANDLE, ctypes.c_uint]
    return k, ctypes, wintypes


def _running_ppt_pids() -> set:
    """Pids of every POWERPNT.EXE currently running (empty off Windows)."""
    if sys.platform != "win32":
        return set()
    try:
        k, ctypes, wintypes = _k32()

        class _PE32W(ctypes.Structure):
            _fields_ = [("dwSize", wintypes.DWORD),
                        ("cntUsage", wintypes.DWORD),
                        ("th32ProcessID", wintypes.DWORD),
                        ("th32DefaultHeapID", ctypes.POINTER(ctypes.c_ulong)),
                        ("th32ModuleID", wintypes.DWORD),
                        ("cntThreads", wintypes.DWORD),
                        ("th32ParentProcessID", wintypes.DWORD),
                        ("pcPriClassBase", ctypes.c_long),
                        ("dwFlags", wintypes.DWORD),
                        ("szExeFile", ctypes.c_wchar * 260)]

        k.Process32FirstW.argtypes = [wintypes.HANDLE, ctypes.POINTER(_PE32W)]
        k.Process32NextW.argtypes  = [wintypes.HANDLE, ctypes.POINTER(_PE32W)]
        snap = k.CreateToolhelp32Snapshot(0x00000002, 0)   # TH32CS_SNAPPROCESS
        if not snap or snap == ctypes.c_void_p(-1).value:
            return set()
        pids = set()
        try:
            e = _PE32W(); e.dwSize = ctypes.sizeof(_PE32W)
            ok = k.Process32FirstW(snap, ctypes.byref(e))
            while ok:
                if (e.szExeFile or "").lower() == "powerpnt.exe":
                    pids.add(int(e.th32ProcessID))
                ok = k.Process32NextW(snap, ctypes.byref(e))
        finally:
            k.CloseHandle(snap)
        return pids
    except Exception as e:
        log.debug("PowerPoint pid scan failed: %s", e)
        return set()


def _reap_pid(pid: int, grace_ms: int = 0) -> bool:
    """Wait up to ``grace_ms`` for ``pid`` to exit on its own, then terminate
    it. True if the process is gone (or was never there) afterwards."""
    if sys.platform != "win32":
        return True
    try:
        k, ctypes, _wt = _k32()
        h = k.OpenProcess(_SYNCHRONIZE | _PROCESS_TERMINATE, False, int(pid))
        if not h:
            return True                       # already exited, or not ours
        try:
            if grace_ms > 0 and k.WaitForSingleObject(h, grace_ms) == _WAIT_OBJECT_0:
                return True                   # Quit() worked, nothing to kill
            log.info("PowerPoint pid %s outlived Quit() — terminating", pid)
            k.TerminateProcess(h, 0)
            k.WaitForSingleObject(h, 2000)
            return True
        finally:
            k.CloseHandle(h)
    except Exception as e:
        log.debug("Could not reap PowerPoint pid %s: %s", pid, e)
        return False


def close_all_powerpoint_instances(grace_ms: int = 0) -> int:
    """Close every POWERPNT.EXE LogicBoard started that is still alive.

    Safe to call at any time: only pids this process launched are in the set,
    and ones that already exited are no-ops. Returns how many were reaped.
    Called on app shutdown and again at interpreter exit.
    """
    n = 0
    for pid in list(_OUR_PPT_PIDS):
        if _reap_pid(pid, grace_ms):
            n += 1
        _OUR_PPT_PIDS.discard(pid)
    return n


# Last line of defence: if a render is interrupted (crash, kill, unhandled
# exception on the way out), the interpreter still sweeps what we started.
atexit.register(close_all_powerpoint_instances)


# ════════════════════════════════════════════════════════════════════════════
# PowerPoint COM session (context manager — guarantees process cleanup)
# ════════════════════════════════════════════════════════════════════════════

class _PowerPointSession:
    """Owns one PowerPoint.Application COM instance for the duration of a
    `with` block. Guarantees POWERPNT.EXE is killed even on exception."""

    # How long Quit() gets to work before the process is terminated. Generous
    # enough that a busy PowerPoint finishing a flush is never killed
    # mid-write, short enough that nobody sits watching a frozen app.
    _QUIT_GRACE_MS = 5000

    def __init__(self):
        self._app          = None
        self._presentation = None
        self._com_init     = False
        self._pids: set    = set()      # the POWERPNT.EXE this session owns

    def __enter__(self):
        # Apartment-threaded COM is required by Office automation.
        pythoncom.CoInitialize()
        self._com_init = True
        before = _running_ppt_pids()
        try:
            # DispatchEx forces a NEW PowerPoint instance — never piggy-backs
            # onto a user's already-open PowerPoint, so closing ours doesn't
            # kill their work.
            self._app = win32com.client.DispatchEx("PowerPoint.Application")
            # Office 2013+ requires DisplayAlerts = 1 (ppAlertsNone).
            try: self._app.DisplayAlerts = 1
            except Exception: pass
        except Exception as e:
            # Adopt anything the failed launch left running before unwinding —
            # a half-started PowerPoint is exactly the kind of orphan that
            # used to survive forever.
            self._adopt(_running_ppt_pids() - before)
            self.__exit__(type(e), e, None)
            raise PptRenderError(
                f"Could not start PowerPoint COM: {e}\n"
                f"Ensure Microsoft PowerPoint is installed and licensed."
            )
        self._adopt(_running_ppt_pids() - before)
        self._adopt_from_hwnd()
        return self

    def _adopt(self, pids) -> None:
        """Take ownership of pids — ours to close, and registered globally so
        an interpreter-exit sweep catches them if we never get to __exit__."""
        for p in pids:
            self._pids.add(int(p))
            _OUR_PPT_PIDS.add(int(p))

    def _adopt_from_hwnd(self) -> None:
        """Ask the Application object which process it lives in.

        The snapshot diff is usually exact, but it can miss (PowerPoint was
        slow to appear in the process list) or over-match (the user launched
        their own PowerPoint in the same instant). The window handle is the
        authoritative answer, so use it when PowerPoint will give us one.
        """
        if sys.platform != "win32" or self._app is None:
            return
        try:
            import ctypes
            from ctypes import wintypes
            hwnd = int(self._app.HWND)
            if not hwnd:
                return
            pid = wintypes.DWORD()
            ctypes.windll.user32.GetWindowThreadProcessId(
                wintypes.HWND(hwnd), ctypes.byref(pid))
            if pid.value:
                self._adopt({pid.value})
        except Exception as e:
            log.debug("PowerPoint HWND→pid failed (using pid scan): %s", e)

    def __exit__(self, exc_type, exc_val, exc_tb):
        if self._presentation is not None:
            try: self._presentation.Close()
            except Exception as e: log.debug("Presentation.Close: %s", e)
            self._presentation = None
        if self._app is not None:
            # A window left open keeps PowerPoint "busy" and makes Quit() a
            # no-op on some Office builds, so close them first.
            try:
                for i in range(self._app.Windows.Count, 0, -1):
                    try: self._app.Windows.Item(i).Close()
                    except Exception: pass
            except Exception as e:
                log.debug("Windows.Close sweep: %s", e)
            try: self._app.Quit()
            except Exception as e: log.debug("Application.Quit: %s", e)
            self._app = None
        # Critical: release COM wrapper objects BEFORE CoUninitialize.
        # Otherwise pywin32 may try to release them after the COM apartment
        # is torn down, leaving an orphan POWERPNT.EXE.
        gc.collect()
        if self._com_init:
            try: pythoncom.CoUninitialize()
            except Exception: pass
            self._com_init = False
        # Now verify rather than assume. Quit() is asynchronous and best-effort;
        # this is what actually makes "no orphan POWERPNT.EXE" true.
        for pid in list(self._pids):
            _reap_pid(pid, self._QUIT_GRACE_MS)
            self._pids.discard(pid)
            _OUR_PPT_PIDS.discard(pid)
        return False  # never suppress exceptions

    def open(self, pptx_path: Path):
        if self._app is None:
            raise PptRenderError("PowerPoint application not running.")
        try:
            self._presentation = self._app.Presentations.Open(
                FileName = str(Path(pptx_path).absolute()),
                # Read-only: a slideshow only READS the deck, so the original
                # file is never modified. A window IS required — a real
                # slideshow renders to the screen (that's how we capture true
                # animation states).
                ReadOnly = msoTrue,
                Untitled = msoFalse,
                WithWindow = msoTrue,
            )
        except Exception as e:
            raise PptRenderError(f"PowerPoint couldn't open {pptx_path.name}: {e}")
        return self._presentation


# ════════════════════════════════════════════════════════════════════════════
# Animation analysis — read click groups from PowerPoint's timeline
# ════════════════════════════════════════════════════════════════════════════

def _click_groups(slide) -> List[list]:
    """For one slide, walk MainSequence and return list-of-lists of Shape COM
    references where each inner list is the set of shapes that enter at one
    click. Returning the shape objects directly (instead of their Ids) means we
    hide exactly the right shapes — no fragile Id round-trip.

    Empty list = no click animations (single-state slide).
    """
    groups: List[list] = []
    current: list = []
    try:
        seq = slide.TimeLine.MainSequence
    except Exception:
        return groups

    try:
        n = seq.Count
    except Exception:
        return groups

    for i in range(1, n + 1):
        try:
            effect = seq.Item(i)
        except Exception:
            continue
        # TriggerType: 1 = msoAnimTriggerOnClick, 2 = WithPrevious,
        # 3 = AfterPrevious. Only TriggerType==1 starts a new click step.
        try:
            ttype = effect.Timing.TriggerType
        except Exception:
            ttype = 1
        if ttype == 1 and current:
            groups.append(current)
            current = []
        try:
            shape = effect.Shape
            if shape is not None:
                current.append(shape)
        except Exception:
            pass

    if current:
        groups.append(current)
    return groups


def _hide_shapes(shapes) -> list:
    """Set Visible = False on each shape reference. Returns the list of shapes
    actually hidden so they can be restored after export. Hiding the shape
    objects directly (rather than searching by Id) is what makes the build-step
    render correct — every not-yet-clicked shape is reliably removed."""
    hidden = []
    for sh in shapes:
        try:
            sh.Visible = msoFalse
            hidden.append(sh)
        except Exception:
            pass
    return hidden


def _restore_shapes(shapes) -> None:
    for sh in shapes:
        try: sh.Visible = msoTrue
        except Exception: pass


# ════════════════════════════════════════════════════════════════════════════
# The main public entry point
# ════════════════════════════════════════════════════════════════════════════

def _find_slideshow_hwnd():
    """Find a running PowerPoint slideshow window by its window CLASS name.

    PowerPoint's full-screen slideshow renders in a window whose class is
    "screenClass". Enumerating top-level windows for that class is far more
    reliable than the COM HWND properties (ssw.HWND / SlideShowWindow.HWND),
    which return 0 or are missing on several PowerPoint builds. Returns an HWND
    (int) or None."""
    import win32gui  # type: ignore
    found = []

    def _cb(h, _):
        try:
            if win32gui.GetClassName(h) == "screenClass":
                found.append(h)
        except Exception:
            pass
        return True

    try:
        win32gui.EnumWindows(_cb, None)
    except Exception:
        pass
    # Prefer a visible one if several exist.
    for h in found:
        try:
            if win32gui.IsWindowVisible(h):
                return h
        except Exception:
            pass
    return found[0] if found else None


def _slideshow_window_info(hwnd):
    """Return (title, (left, top, right, bottom), (w, h)) for a window HWND, or
    None if the handle is no longer a real window."""
    import win32gui  # type: ignore
    if not hwnd or not win32gui.IsWindow(hwnd):
        return None
    title = ""
    try:
        title = win32gui.GetWindowText(hwnd)
    except Exception:
        pass
    l, t, r, b = win32gui.GetWindowRect(hwnd)
    return title, (l, t, r, b), (r - l, b - t)


def _foreground_window(hwnd):
    """Best-effort: bring a window to the foreground and keep it on top. Capture
    does NOT depend on this (PrintWindow reads the window's own buffer), but the
    slideshow should be active and unobstructed while we render."""
    try:
        import win32gui, win32con  # type: ignore
        try: win32gui.ShowWindow(hwnd, win32con.SW_SHOW)
        except Exception: pass
        try:
            win32gui.SetWindowPos(hwnd, win32con.HWND_TOPMOST, 0, 0, 0, 0,
                                  win32con.SWP_NOMOVE | win32con.SWP_NOSIZE)
        except Exception: pass
        try: win32gui.BringWindowToTop(hwnd)
        except Exception: pass
        try: win32gui.SetForegroundWindow(hwnd)
        except Exception: pass
    except Exception:
        pass


def _move_offscreen(hwnd):
    """Move the slideshow window completely off the visible desktop so the user
    never sees PowerPoint. PrintWindow still captures its pixels (it renders the
    window's OWN buffer regardless of position), so the window does not need to
    be on-screen, focused, or unobstructed. Does not steal focus."""
    try:
        import win32gui, win32con  # type: ignore
        win32gui.SetWindowPos(
            hwnd, 0, -32000, -32000, 0, 0,
            win32con.SWP_NOZORDER | win32con.SWP_NOACTIVATE | win32con.SWP_NOSIZE)
    except Exception as e:
        log.debug("move offscreen failed: %s", e)


def _suppress_show_overlays(view):
    """Hide the slideshow pointer / pen / laser overlay so it can never appear
    in a capture. (The popup control toolbar only shows on mouse-hover, which
    never happens here since the window is off-screen and we never move the
    cursor over it.)"""
    for val in (3, 0):     # 3 = ppSlideShowPointerAlwaysHidden, 0 = None
        try:
            view.PointerType = val
            return
        except Exception:
            continue


def _capture_hwnd(hwnd):
    """Capture the pixels belonging to ONE specific window via PrintWindow.

    Unlike a screen grab, PrintWindow renders the window's OWN content into an
    off-screen bitmap straight from its HWND — so it is immune to whatever is on
    top of it (Start menu, notifications, LogicBoard, the mouse cursor). The
    PW_RENDERFULLCONTENT flag (0x2) captures GPU-accelerated surfaces such as a
    PowerPoint slideshow on Windows 8.1+.

    Returns (PIL.Image | None, (w, h), ok: bool).

    GDI DISCIPLINE — the previous version broke two of GDI's rules on every
    capture, and this runs ~25 times a second for as long as an animation
    is playing:

      * it called DeleteDC on the WINDOW DC (via win32ui's wrapper) and then
        ReleaseDC on the same handle. A DC from GetWindowDC may only ever be
        released; deleting it is undefined behaviour, and freeing one handle
        twice is the kind of fault that kills a process on one graphics
        driver and passes on another;
      * it deleted the bitmap while it was still selected into the memory
        DC, which GDI refuses, so freeing it was left to garbage collection.

    Now: plain Win32 through ctypes with 64-bit-safe signatures, every
    handle checked, the original bitmap selected back BEFORE ours is
    deleted, the memory DC deleted, the window DC released — once each, in
    that order. The bitmap is a 32-bit top-down DIB section, so its pixel
    layout no longer depends on the display's colour depth either.
    """
    g = _gdi()
    if g is None:
        return None, (0, 0), False
    user32, gdi32, wt = g
    rect = wt.RECT()
    if not user32.GetWindowRect(hwnd, ctypes.byref(rect)):
        return None, (0, 0), False
    w, h = rect.right - rect.left, rect.bottom - rect.top
    # A sane ceiling as well as a floor: 16k x 16k at 4 bytes is already
    # 1 GB, and a bogus rect must never become a giant allocation.
    if w <= 0 or h <= 0 or w > 16384 or h > 16384:
        return None, (w, h), False

    win_dc = user32.GetWindowDC(hwnd)
    if not win_dc:
        return None, (w, h), False
    mem_dc = dib = old = None
    img, ok = None, False
    try:
        mem_dc = gdi32.CreateCompatibleDC(win_dc)
        if not mem_dc:
            return None, (w, h), False
        bmi = _BITMAPINFO()
        bmi.bmiHeader.biSize = ctypes.sizeof(_BITMAPINFOHEADER)
        bmi.bmiHeader.biWidth = w
        bmi.bmiHeader.biHeight = -h            # negative = top-down rows
        bmi.bmiHeader.biPlanes = 1
        bmi.bmiHeader.biBitCount = 32
        bmi.bmiHeader.biCompression = 0        # BI_RGB
        bits = ctypes.c_void_p()
        dib = gdi32.CreateDIBSection(win_dc, ctypes.byref(bmi), 0,  # DIB_RGB_COLORS
                                     ctypes.byref(bits), None, 0)
        if not dib or not bits.value:
            return None, (w, h), False
        old = gdi32.SelectObject(mem_dc, dib)
        # 2 = PW_RENDERFULLCONTENT (captures DirectX / GPU-rendered windows).
        ok = bool(user32.PrintWindow(hwnd, mem_dc, 2))
        gdi32.GdiFlush()
        if ok:
            # Copied out of the DIB before it is freed below.
            raw = ctypes.string_at(bits.value, w * h * 4)
            img = Image.frombuffer("RGB", (w, h), raw, "raw", "BGRX", 0, 1)
    finally:
        if mem_dc and old:
            gdi32.SelectObject(mem_dc, old)    # ours out BEFORE it is deleted
        if dib:
            gdi32.DeleteObject(dib)
        if mem_dc:
            gdi32.DeleteDC(mem_dc)
        user32.ReleaseDC(hwnd, win_dc)         # released, never deleted
    return img, (w, h), ok


# ── Win32 GDI through ctypes, with 64-bit-correct signatures ────────────────
# Without argtypes/restype ctypes passes and returns handles as 32-bit ints,
# which silently truncates them on 64-bit Windows.
import ctypes


class _BITMAPINFOHEADER(ctypes.Structure):
    _fields_ = [("biSize", ctypes.c_uint32), ("biWidth", ctypes.c_int32),
                ("biHeight", ctypes.c_int32), ("biPlanes", ctypes.c_uint16),
                ("biBitCount", ctypes.c_uint16),
                ("biCompression", ctypes.c_uint32),
                ("biSizeImage", ctypes.c_uint32),
                ("biXPelsPerMeter", ctypes.c_int32),
                ("biYPelsPerMeter", ctypes.c_int32),
                ("biClrUsed", ctypes.c_uint32),
                ("biClrImportant", ctypes.c_uint32)]


class _BITMAPINFO(ctypes.Structure):
    _fields_ = [("bmiHeader", _BITMAPINFOHEADER),
                ("bmiColors", ctypes.c_uint32 * 3)]


_GDI = None


def _gdi():
    global _GDI
    if _GDI is not None:
        return _GDI
    if sys.platform != "win32":
        return None
    from ctypes import wintypes as wt
    user32, gdi32 = ctypes.windll.user32, ctypes.windll.gdi32
    H = wt.HANDLE
    user32.GetWindowRect.argtypes = [wt.HWND, ctypes.POINTER(wt.RECT)]
    user32.GetWindowRect.restype = wt.BOOL
    user32.GetWindowDC.argtypes = [wt.HWND]
    user32.GetWindowDC.restype = wt.HDC
    user32.ReleaseDC.argtypes = [wt.HWND, wt.HDC]
    user32.ReleaseDC.restype = ctypes.c_int
    user32.PrintWindow.argtypes = [wt.HWND, wt.HDC, wt.UINT]
    user32.PrintWindow.restype = wt.BOOL
    gdi32.CreateCompatibleDC.argtypes = [wt.HDC]
    gdi32.CreateCompatibleDC.restype = wt.HDC
    gdi32.CreateDIBSection.argtypes = [wt.HDC, ctypes.POINTER(_BITMAPINFO),
                                       wt.UINT,
                                       ctypes.POINTER(ctypes.c_void_p),
                                       H, wt.DWORD]
    gdi32.CreateDIBSection.restype = wt.HBITMAP
    gdi32.SelectObject.argtypes = [wt.HDC, H]
    gdi32.SelectObject.restype = H
    gdi32.DeleteObject.argtypes = [H]
    gdi32.DeleteObject.restype = wt.BOOL
    gdi32.DeleteDC.argtypes = [wt.HDC]
    gdi32.DeleteDC.restype = wt.BOOL
    gdi32.GdiFlush.argtypes = []
    gdi32.GdiFlush.restype = wt.BOOL
    _GDI = (user32, gdi32, wt)
    return _GDI


def _grab_slideshow(hwnd, settle: float):
    """Validate, then capture ONLY the slideshow window via its HWND.

    Verifies the handle still exists, the window is visible and has valid
    dimensions, stabilises briefly, captures with PrintWindow, and asserts the
    returned bitmap matches the window bounds (aborts otherwise). Never touches
    the desktop, active window, or full screen."""
    import time
    import win32gui          # type: ignore

    if not hwnd or not win32gui.IsWindow(hwnd):
        raise PptRenderError("Slideshow window handle no longer exists — aborting.")
    # An off-screen window still reports visible; if PowerPoint truly hid it,
    # nudge it back to a SHOWN (but still off-screen) state — never on-screen.
    if not win32gui.IsWindowVisible(hwnd):
        try:
            win32gui.ShowWindow(hwnd, 8)   # SW_SHOWNA — show without activating
        except Exception:
            pass
        _move_offscreen(hwnd)
        if not win32gui.IsWindowVisible(hwnd):
            raise PptRenderError("Slideshow window is not visible — aborting.")

    l, t, r, b = win32gui.GetWindowRect(hwnd)
    w, h = r - l, b - t
    if w <= 0 or h <= 0:
        raise PptRenderError(f"Slideshow window has invalid size {w}x{h} — aborting.")

    time.sleep(settle)        # stabilisation delay (build state finishes painting)

    img, (cw, ch), ok = _capture_hwnd(hwnd)
    if img is None or not ok:
        raise PptRenderError("PrintWindow failed to capture the slideshow window.")
    # Validation: captured bitmap MUST match the window bounds.
    if img.size != (w, h) or (cw, ch) != (w, h):
        raise PptRenderError(
            f"Capture {img.size} does not match slideshow bounds {(w, h)} — aborting.")
    log.debug("Captured slideshow frame: bounds=%s size=%s", (l, t, r, b), img.size)
    return img.convert("RGB")


def _frame_sig(img):
    """Tiny grayscale thumbnail used as a fast frame-comparison signature."""
    return img.convert("L").resize((64, 36))


def _frame_mad(a, b) -> float:
    """Mean absolute pixel difference between two tiny grayscale images (0-255).
    ~0 means the frames are identical (animation has settled); a large value
    means the slide is still visibly changing."""
    from PIL import ImageChops
    diff = ImageChops.difference(a, b)
    hist = diff.histogram()
    total = sum(i * c for i, c in enumerate(hist))
    n = sum(hist) or 1
    return total / n


def _settled_frame(hwnd, target_aspect, poll: float = 0.04,
                   max_wait: float = 4.0, thresh: float = 2.0,
                   settle_frames: int = 2):
    """Grab the slideshow window repeatedly until its pixels STOP changing —
    i.e. the current build's animation has finished playing — then return the
    settled, aspect-cropped RGB image.

    PowerPoint's COM API exposes no 'animation complete' signal, so completion
    is detected visually: once consecutive grabs are essentially identical
    (mean pixel difference below `thresh` for `settle_frames` checks in a row),
    the animation is done. This waits exactly as long as each animation needs
    (instant builds settle in ~100 ms; a 2 s fly-in waits ~2 s) without ever
    hardcoding an animation duration. A hard `max_wait` ceiling guarantees we
    never block forever on a looping animation."""
    import time
    import win32gui  # type: ignore
    if not hwnd or not win32gui.IsWindow(hwnd):
        raise PptRenderError("Slideshow window handle no longer exists — aborting.")

    def _grab():
        img, (w, h), ok = _capture_hwnd(hwnd)
        if img is None or not ok or w <= 0 or h <= 0 or img.size != (w, h):
            return None
        return _crop_to_aspect(img, target_aspect)

    time.sleep(poll)                      # let the build's animation begin
    last      = _grab()
    last_sig  = _frame_sig(last) if last is not None else None
    stable    = 0
    deadline  = time.time() + max_wait
    while time.time() < deadline:
        time.sleep(poll)
        cur = _grab()
        if cur is None:
            continue
        sig = _frame_sig(cur)
        if last_sig is not None and _frame_mad(sig, last_sig) <= thresh:
            stable += 1
            if stable >= settle_frames:
                return cur                # animation finished — stable image
        else:
            stable = 0
        last, last_sig = cur, sig
    if last is not None:
        return last                       # timed out (e.g. looping) — last frame
    raise PptRenderError("Slideshow capture produced no frames — aborting.")


def _crop_to_aspect(img, target_aspect: float):
    """Center-crop the captured frame to the slide's aspect ratio, removing any
    letterbox / pillarbox bars the full-screen slideshow added when the screen
    aspect didn't match the slide aspect."""
    w, h = img.size
    if w <= 0 or h <= 0:
        return img
    cur = w / h
    if abs(cur - target_aspect) < 0.02:
        return img
    if cur > target_aspect:                      # too wide -> crop sides
        nw = max(1, int(round(h * target_aspect)))
        x = (w - nw) // 2
        return img.crop((x, 0, x + nw, h))
    nh = max(1, int(round(w / target_aspect)))   # too tall -> crop top/bottom
    y = (h - nh) // 2
    return img.crop((0, y, w, y + nh))


ProgressCb = Callable[[str, float], None]


def render_ppt_with_build_steps(
    source_pptx: Path,
    cache_dir:   Path,
    *,
    force:           bool                = False,
    with_animations: bool                = False,
    on_progress:     Optional[ProgressCb] = None,
) -> PptRenderResult:
    """Render `source_pptx` into `cache_dir` as WEBP files.

    `with_animations`:
        • False (default) — ONE flat image per slide (every object visible),
          rendered fast + reliably via Slide.Export. No slideshow.
        • True            — capture each click/build state from a live
          slideshow (slower, experimental). Produces per-step images.

    The original .pptx is NEVER modified.

    Cache layout (always WEBP):

        cache_dir/
            cache_manifest.json
            slide_001_anim_000.webp   ← step 0 (before any click)
            slide_001_anim_001.webp   ← after click 1
            ...
            slide_001_anim_<N>.webp   ← final state
            slide_002_anim_000.webp
            ...
            thumb_001.webp            ← small Media-Manager thumbnails
            thumb_002.webp
            ...
    """
    src   = Path(source_pptx)
    cache = Path(cache_dir)
    cache.mkdir(parents=True, exist_ok=True)

    if not src.exists():
        raise PptRenderError(f"Source missing: {src}")
    if not have_powerpoint():
        raise PptRenderError(
            "PowerPoint rendering needs Windows + pywin32 + Pillow.\n"
            "  pip install pywin32 Pillow"
        )

    # ── Cache hit? ────────────────────────────────────────────────────────
    manifest_path = cache / "cache_manifest.json"
    if not force and is_cache_fresh(src, cache):
        try:
            data = json.loads(manifest_path.read_text(encoding="utf-8"))
            res  = PptRenderResult.from_dict(data.get("result", {}))
            if res.slide_count > 0:
                log.info("PPT cache hit: %s (%d slides)", src.name, res.slide_count)
                if on_progress:
                    on_progress("Using cached render", 1.0)
                return res
        except Exception as e:
            log.warning("Manifest unreadable (%s); regenerating.", e)

    # ── Wipe stale files (keep manifest until success) ────────────────────
    for old in cache.iterdir():
        if old.name == "cache_manifest.json":
            continue
        try:
            if old.is_file(): old.unlink()
            else:             shutil.rmtree(old, ignore_errors=True)
        except OSError:
            pass

    render_w = _detect_display_width()

    if on_progress:
        on_progress("Starting PowerPoint…", 0.02)

    # ── Render ────────────────────────────────────────────────────────────
    with tempfile.TemporaryDirectory(prefix="ppt_render_") as tmp:
        tmp_path = Path(tmp)

        with _PowerPointSession() as sess:
            if on_progress:
                on_progress(f"Opening {src.name}…", 0.06)
            prs = sess.open(src)

            try:
                slide_count = int(prs.Slides.Count)
            except Exception:
                raise PptRenderError("Could not read slide count.")

            # Render height per page aspect — PowerPoint reports SlideWidth/Height
            # in points (1pt = 1/72 in). Aspect drives our render dimensions.
            try:
                page_w = float(prs.PageSetup.SlideWidth)
                page_h = float(prs.PageSetup.SlideHeight)
                aspect = page_w / page_h if page_h > 0 else 16 / 9
            except Exception:
                aspect = 16 / 9
            render_h = max(1, int(render_w / aspect))

            log.info("Rendering %s @ %d×%d (%d slides)",
                     src.name, render_w, render_h, slide_count)

            anim_counts: List[int]       = []
            step_files:  List[List[str]] = [[] for _ in range(slide_count)]
            thumb_files: List[str]       = []
            # Results from the two render paths. The animation path
            # submits conversion jobs to a pool (futures); the
            # no-animation path renders synchronously via the shared
            # PDF pipeline and appends finished tuples here. The
            # assembler downstream consumes both.
            futures: list = []
            direct_results: list = []

            if with_animations:
                # Detect click-animation groups for EVERY slide up-front. Cheap (it
                # only walks each timeline) and lets us show accurate animation
                # progress and log exactly what was found.
                slide_groups: List[list] = []
                for s_idx in range(1, slide_count + 1):
                    try:
                        g = _click_groups(prs.Slides(s_idx))
                    except Exception as e:
                        log.warning("Slide %d: animation scan failed: %s", s_idx, e)
                        g = []
                    slide_groups.append(g)
                    log.info("Slide %d: %d click-animation group(s) detected",
                             s_idx, len(g))
                total_images = sum(len(g) + 1 for g in slide_groups) or 1
                done_images  = 0

                # PowerPoint exports must stay sequential (single COM instance), but
                # the CPU-bound PNG→WEBP encoding is the real time sink — so we hand
                # each exported PNG to a thread pool and keep exporting.
                # -- SLIDESHOW-MODE CAPTURE ------------------------------------
                # Slide.Export() only renders the AUTHORING layout and has no
                # concept of animation build states, so every "step" came out
                # identical. Instead we run a REAL slideshow and screen-capture
                # each click state -- exactly what a presenter sees. The CPU-bound
                # PNG->WEBP encoding still runs on a thread pool (saving pipeline is
                # untouched: same filenames, same _convert_task).
                import time
                target_aspect = render_w / max(1, render_h)
                # Stabilization delay is ONLY long enough for the slideshow window to
                # repaint the new build state — it is NOT tied to animation duration.
                # PowerPoint reaches each click's build state and we grab it; we do
                # not wait for fade/fly/wipe playback to visually finish.
                REFRESH_DELAY = 0.01   # 10 ms — window-refresh only (max per spec)
                WARN = "Do NOT touch the mouse or keyboard until this finishes  -  "

                # PowerPoint will only create a real slideshow window (with a valid
                # HWND) when the application is visible. Make it visible first.
                try:
                    prs.Application.Visible = True
                except Exception as e:
                    log.debug("Application.Visible=True failed: %s", e)

                # Configure the show so NO presenter UI / overlays appear and the
                # show never auto-advances out from under the capture loop.
                try:
                    sss = prs.SlideShowSettings
                    for _attr, _val in (("ShowType", 1),          # ppShowTypeSpeaker
                                        ("AdvanceMode", 1),       # manual advance only
                                        ("ShowWithNarration", msoFalse),
                                        ("ShowPresenterView", msoFalse),  # never open Presenter View
                                        ("LoopUntilStopped", msoFalse)):
                        try: setattr(sss, _attr, _val)
                        except Exception: pass
                except Exception as e:
                    log.debug("SlideShowSettings config failed: %s", e)

                try:
                    ssw = prs.SlideShowSettings.Run()
                except Exception as e:
                    raise PptRenderError(
                        "Could not start the PowerPoint slideshow required to "
                        f"render animation states: {e}")

                # Resolve the slideshow VIEW (for navigation) and the window HWND
                # (for capture). The COM HWND properties are unreliable across
                # PowerPoint builds, so the HWND is found primarily by window class
                # ("screenClass"); the View comes from whichever COM source works.
                import win32gui  # type: ignore

                def _get_view():
                    for g in (lambda: ssw.View,
                              lambda: prs.SlideShowWindow.View,
                              lambda: prs.Application.SlideShowWindows(1).View):
                        try:
                            v = g()
                            if v is not None:
                                return v
                        except Exception:
                            pass
                    return None

                view = None
                hwnd = None
                moved = False
                for _attempt in range(60):                     # up to ~6s
                    if view is None:
                        view = _get_view()
                    if hwnd is None:
                        for getter in (
                            lambda: _find_slideshow_hwnd() or 0,       # by class name
                            lambda: int(ssw.HWND),                     # COM fallbacks
                            lambda: int(prs.SlideShowWindow.HWND),
                            lambda: int(prs.Application.SlideShowWindows(1).HWND),
                        ):
                            try:
                                h = getter()
                            except Exception:
                                h = 0
                            if h and win32gui.IsWindow(h):
                                hwnd = h
                                break
                    # Hide it the instant we have a handle to minimise any flash on
                    # the user's display.
                    if hwnd is not None and not moved:
                        _move_offscreen(hwnd)
                        moved = True
                    if hwnd is not None and view is not None:
                        break
                    time.sleep(0.1)

                # Diagnostics so any remaining failure is actionable from the log.
                try:
                    _cnt = int(prs.Application.SlideShowWindows.Count)
                except Exception:
                    _cnt = -1
                log.info("Slideshow start: ssw=%s view=%s hwnd=%s "
                         "SlideShowWindows.Count=%s",
                         ssw is not None, view is not None, hwnd, _cnt)

                if view is None or hwnd is None:
                    raise PptRenderError(
                        "PowerPoint did not return a usable slideshow window "
                        f"(view={view is not None}, hwnd={hwnd}, shows={_cnt}). "
                        "The slideshow may have been blocked from starting.")
                _info = _slideshow_window_info(hwnd)
                _title, _bounds, _wh = _info if _info else ("", (0, 0, 0, 0), (0, 0))
                log.info("Slideshow capture target: HWND=%s title=%r bounds=%s size=%s",
                         hwnd, _title, _bounds, _wh)
                # Keep it off-screen + hide pointer/pen/laser overlays. Do NOT bring
                # it to the foreground — the user must never see PowerPoint.
                _suppress_show_overlays(view)
                _move_offscreen(hwnd)
                time.sleep(0.1)

                # 8, matching the PDF pipeline. These threads do Pillow
                # encoding, which releases the GIL, so they scale — the
                # old cap of 4 left half the cores idle while encoding
                # was two thirds of the import. Capped rather than
                # unbounded because each in-flight slide holds a
                # full-resolution bitmap; the cap is a memory ceiling.
                workers = max(2, min(8, (os.cpu_count() or 2)))
                futures = []          # list of Future -> (s_idx, step, webp, thumb)
                try:
                    with ThreadPoolExecutor(max_workers=workers) as pool:
                        for s_idx in range(1, slide_count + 1):
                            groups  = slide_groups[s_idx - 1]
                            n_steps = len(groups)
                            anim_counts.append(n_steps)

                            # Keep the window off-screen in case PowerPoint nudged it
                            # back when navigating (cheap no-op if already hidden).
                            _move_offscreen(hwnd)

                            # Jump to this slide's INITIAL state (before any click).
                            try:
                                view.GotoSlide(s_idx)
                            except Exception as e:
                                log.warning("GotoSlide(%d) failed: %s", s_idx, e)

                            # step 0 = initial; each later step = one more click.
                            for step in range(n_steps + 1):
                                if step > 0:
                                    try:
                                        view.Next()      # advance exactly one click
                                    except Exception as e:
                                        log.warning("Slide %d Next() failed @step %d: %s",
                                                    s_idx, step, e)

                                if on_progress:
                                    on_progress(
                                        f"{WARN}Slide {s_idx}/{slide_count} - animation "
                                        f"{step + 1}/{n_steps + 1}",
                                        0.05 + 0.90 * done_images / total_images)

                                # Capture the build state ONLY once its animation has
                                # finished — _settled_frame grabs repeatedly until the
                                # window's pixels stop changing, so a fade/fly/wipe is
                                # captured at its final state, not mid-play. Waits
                                # exactly as long as each animation needs, no more.
                                is_final = (step == n_steps)
                                png  = tmp_path / (f"s{s_idx}_final.png" if is_final
                                                   else f"s{s_idx}_k{step}.png")
                                webp = cache / f"slide_{s_idx:03d}_anim_{step:03d}.webp"
                                thumb = (cache / f"thumb_{s_idx:03d}.webp") if is_final else None
                                try:
                                    img = _settled_frame(hwnd, target_aspect)
                                    if img.size != (int(render_w), int(render_h)):
                                        img = img.resize((int(render_w), int(render_h)),
                                                         Image.LANCZOS)
                                    img.save(str(png))
                                    futures.append(pool.submit(
                                        _convert_task, png, webp, thumb, s_idx, step))
                                except PptRenderError:
                                    # Capture validation failed -> abort the whole
                                    # render (propagate to the outer handler + log).
                                    log.error("Aborting render: slideshow capture "
                                              "validation failed on slide %d step %d",
                                              s_idx, step)
                                    raise
                                except Exception as e:
                                    log.warning("Slide %d step %d capture failed: %s",
                                                s_idx, step, e)
                                    try: png.unlink()
                                    except OSError: pass
                                done_images += 1

                        if on_progress:
                            on_progress("Encoding slides...", 0.96)
                        # Leaving the pool waits for every conversion to finish --
                        # still inside the temp dir, so no PNG vanishes early.
                finally:
                    # Always end the slideshow, even on error.
                    try: view.Exit()
                    except Exception: pass
            else:
                # ── NO-ANIMATION MODE (default, fast + reliable) ──────────
                # ONE COM call exports the whole deck to PDF; the shared PDF
                # pipeline then rasterises it with 8 threads and the tuned
                # WEBP settings.
                #
                # This replaces a per-slide ``Slide.Export()`` loop, which
                # could NOT be parallelised. PowerPoint is a single-instance
                # COM server: several client processes all marshal into one
                # POWERPNT.EXE. Measured — 2 workers gave 1.06x (no gain, just
                # marshalling overhead) and 4 workers produced 24 of 30 slides
                # with "Slide.Export : Object does not exist", because the
                # clients trampled each other's state and whichever finished
                # first called Quit() on the instance the others were still
                # using. Silently dropping slides is far worse than being
                # slow, so COM is now a single call and everything after it
                # runs through MuPDF, which parallelises properly.
                #
                # Fidelity: PowerPoint's PDF export is VECTOR and is the same
                # exporter behind "Save as PDF", so rasterising it is at least
                # as faithful as a Slide.Export bitmap.
                if on_progress:
                    on_progress("Exporting deck…", 0.06)
                anim_counts.extend([0] * slide_count)   # no build steps

                pdf_path = tmp_path / "deck_export.pdf"
                prs.SaveAs(str(pdf_path.absolute()), 32)   # 32 = ppSaveAsPDF

                from ui.util.media_cache import render_pdf_pages
                n_pages, ordered, _first_h = render_pdf_pages(
                    pdf_path, cache, int(render_w),
                    page_namer=lambda i: f"slide_{i + 1:03d}_anim_000.webp",
                    thumb_namer=lambda i: f"thumb_{i + 1:03d}.webp",
                    on_progress=on_progress,
                    progress_lo=0.10, progress_span=0.84,
                )
                if n_pages != slide_count:
                    # Hidden slides are omitted from the PDF. Trust the COM
                    # slide count for the structures already sized to it and
                    # just map across what the PDF actually produced.
                    log.info("PPT->PDF produced %d page(s) for %d slide(s)",
                             n_pages, slide_count)
                for i, (pname, tname, _h) in enumerate(ordered, start=1):
                    if i > slide_count:
                        break
                    direct_results.append((i, 0, pname, tname))

            # ── Assemble results in the original order ────────────────────
            def _key(n: str) -> int:
                try:
                    return int(n.rsplit("_anim_", 1)[1].split(".", 1)[0])
                except Exception:
                    return 0

            collected: dict[int, list[str]] = {i: [] for i in range(1, slide_count + 1)}
            thumb_ok: dict[int, str] = {}
            # The animation path produces futures; the no-animation path
            # renders synchronously and appends straight to direct_results.
            produced = [f.result for f in futures]
            for get in produced + [(lambda r=r: r) for r in direct_results]:
                try:
                    s_idx, step, webp_name, thumb_name = get()
                except Exception as e:
                    log.warning("Conversion task failed: %s", e)
                    continue
                if webp_name:
                    collected[s_idx].append(webp_name)
                if thumb_name:
                    thumb_ok[s_idx] = thumb_name

            for s_idx in range(1, slide_count + 1):
                names = collected.get(s_idx, [])
                seen: dict[int, str] = {}
                for n in names:
                    seen[_key(n)] = n          # dedupe by anim index
                step_files[s_idx - 1] = [seen[k] for k in sorted(seen)]
                if s_idx in thumb_ok:          # preserve slide-order thumb list
                    thumb_files.append(thumb_ok[s_idx])

        # ↑ _PowerPointSession.__exit__ cleans up POWERPNT.EXE here.

    if on_progress:
        on_progress("Wrapping up… 🚀", 0.96)

    result = PptRenderResult(
        slide_count = slide_count,
        anim_counts = anim_counts,
        step_files  = step_files,
        thumb_files = thumb_files,
        format      = "webp",
        render_size = (render_w, render_h),
    )

    # ── Write manifest ────────────────────────────────────────────────────
    try:
        manifest = {
            "schema_version": 3,
            "fingerprint":    fingerprint_file(src),
            "result":         result.to_dict(),
            "files":          ([fn for s in step_files for fn in s]
                                + list(thumb_files)),
        }
        manifest_path.write_text(json.dumps(manifest, indent=2),
                                 encoding="utf-8")
    except OSError as e:
        log.warning("Manifest write failed: %s", e)

    if on_progress:
        on_progress("Complete", 1.0)

    return result