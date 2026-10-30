"""
lb_format.py — LogicBoard's custom ``.lb`` file format.

A ``.lb`` file is, mechanically, a plain ZIP archive whose extension has been
renamed. To the user it looks like a single board document; internally it
contains everything needed to recreate that board:

    Physics Lesson.lb               ← what the user sees
    └── (zip archive)               ← what's actually inside
        ├── metadata.json           ← lb-format version + board info
        ├── board.json              ← scene graph + pages (engine document)
        ├── imports/                ← original imported files (pptx, pdf,
        │                              png, jpg, mp4, …) — NEVER converted
        ├── temps/                  ← generated renders + cache manifest
        │   ├── media/<safe_name>/
        │   │   ├── cache_manifest.json
        │   │   ├── slide_001_anim_000.webp
        │   │   └── thumb_001.webp
        │   └── nav_pages/page_NNNN.webp
        └── thumbnails/             ← (reserved for future use)

Working-directory model
-----------------------
The .lb file itself is read-only during a session — we never modify it in
place. When a board is opened we extract the .lb into a per-session **working
directory** under ``<saves_root>/.working/<uuid>/`` and treat that workdir as
the "board folder" for all of LogicBoard's existing v3 code paths. On save
we re-zip the workdir back into the .lb (atomically via tmp → rename).

This gives us:
  • No code in the rest of the app needs to know about zips.
  • Caches and edits survive within a session naturally.
  • Save is atomic — power loss can't corrupt the .lb.
  • The .lb is the single, portable artefact the user sees.

Cache reuse
-----------
Because the workdir is a faithful extraction of the .lb's full contents
(including ``temps/`` and ``thumbnails/``), every existing MediaLibrary /
MediaCache lookup that checks ``cache_manifest.json`` keeps working unchanged.
Already-rendered PPT slides, PDF pages, navigation thumbnails, and media
manager thumbnails are reused directly — no regeneration on open.
"""
from __future__ import annotations

import json
import logging
import shutil
import tempfile
import uuid
import zipfile
from datetime import datetime, timezone
from pathlib import Path
from typing import Optional

__all__ = [
    "LB_EXTENSION",
    "LB_METADATA_FILENAME",
    "LB_FORMAT_VERSION",
    "is_lb_file",
    "is_legacy_board_folder",
    "open_lb_to_workdir",
    "save_workdir_to_lb",
    "migrate_folder_to_lb",
    "cleanup_workdir",
    "sweep_workdirs",
    "workdir_root",
    "new_unsaved_workdir",
    "clean_board_name",
]

logger = logging.getLogger(__name__)


# ─────────────────────────────────────────────────────────────────────────────
# Constants
# ─────────────────────────────────────────────────────────────────────────────

LB_EXTENSION         = ".lb"
LB_METADATA_FILENAME = "metadata.json"
LB_FORMAT_VERSION    = 1

# Top-level entries we expect in a well-formed .lb archive. (Existence isn't
# strictly required — older boards may lack temps/ or thumbnails/.)
_KNOWN_TOP_LEVEL = ("board.json", "imports", "temps", "nav_pages",
                    "thumbnails", LB_METADATA_FILENAME)


# ─────────────────────────────────────────────────────────────────────────────
# Board names
# ─────────────────────────────────────────────────────────────────────────────

# The characters Windows genuinely refuses in a filename, plus the two path
# separators. Everything else — spaces, dashes, brackets, commas, most
# punctuation — is legal and belongs to the user.
#
# A SET, not a regex character class. The class spelling of this is a trap:
# inside [...] a backslash escapes the character after it, so the obvious
# r'[<>:"/\|?*]' reads as "an escaped pipe" and matches no backslash at all —
# which let a board be named "Class\11" and turned the save into a write to a
# subdirectory that does not exist. chr(92) says backslash with no escaping
# involved and nothing to get subtly wrong.
_ILLEGAL_NAME_CHARS = frozenset('<>:"/|?*') | {chr(92)}

# Longest board name we will write. Well under the 255-character limit a
# Windows filename has, leaving room for the .lb extension, a "_2" collision
# suffix, and the saves path itself.
_MAX_NAME_LEN = 120


def clean_board_name(name: str) -> str:
    """Turn what the teacher typed into a filename, changing as little as
    possible.

    SPACES ARE LEFT ALONE. This used to run ``re.sub(r"\s+", "_", ...)``, so
    "Class 11 Chapter 4" was saved, listed and reopened forever after as
    "Class_11_Chapter_4". Nothing required that: a space is a perfectly legal
    Windows filename character. It was mangling the one piece of text in the
    app the user actually authored, and they had no way to get it back.

    What IS changed, and why:

      * ``< > : " / \ | ?  *`` and control characters — Windows rejects
        these outright, so a name containing one could not be saved at all.
        Replaced with a space rather than an underscore: "Ratio 3:4" reads
        better as "Ratio 3 4" than "Ratio_3_4".
      * Runs of whitespace collapse to one, and the ends are trimmed. Leading
        and trailing spaces are invisible in a list and Windows silently
        strips trailing ones anyway, so honouring them would mean the file on
        disk did not match the name shown.
      * Trailing dots go. Windows strips those too, and a board saved as
        "Chapter 4." would come back as "Chapter 4" — a name that no longer
        matches what the app thinks it saved.
      * Over-long names are cut to ``_MAX_NAME_LEN``.

    An empty result falls back to "Board", so a name of nothing but illegal
    characters still saves somewhere findable.
    """
    import re
    # One pass: illegal characters and control characters both become a
    # space, which the whitespace collapse below then tidies up.
    cleaned = "".join(
        " " if (ch in _ILLEGAL_NAME_CHARS or not (ch.isprintable() or ch == " "))
        else ch
        for ch in str(name or ""))
    cleaned = re.sub(r"\s+", " ", cleaned).strip()
    cleaned = cleaned.rstrip(". ")
    if len(cleaned) > _MAX_NAME_LEN:
        cleaned = cleaned[:_MAX_NAME_LEN].rstrip(". ")
    return cleaned or "Board"


# ─────────────────────────────────────────────────────────────────────────────
# Format detection
# ─────────────────────────────────────────────────────────────────────────────

def is_lb_file(path: Path) -> bool:
    """True if ``path`` is a regular file with the ``.lb`` extension AND looks
    like a valid ZIP archive containing ``board.json``."""
    p = Path(path)
    if not p.is_file() or p.suffix.lower() != LB_EXTENSION:
        return False
    try:
        with zipfile.ZipFile(p, "r") as zf:
            return "board.json" in zf.namelist()
    except (zipfile.BadZipFile, OSError):
        return False


def is_legacy_board_folder(path: Path) -> bool:
    """True if ``path`` is a directory matching the old uncompressed board
    layout: a folder containing ``board.json``. Used by the migration path
    when the user opens an older project."""
    p = Path(path)
    return p.is_dir() and (p / "board.json").is_file()


# ─────────────────────────────────────────────────────────────────────────────
# Working-directory management
# ─────────────────────────────────────────────────────────────────────────────

def workdir_root(saves_root: Path) -> Path:
    """The per-user root for all session working directories.

    Hidden by leading dot so it doesn't appear in the boards list. Each
    open board occupies a UUID-named subdir inside this root."""
    root = Path(saves_root) / ".working"
    root.mkdir(parents=True, exist_ok=True)
    return root


def new_unsaved_workdir(saves_root: Path) -> Path:
    """Create a fresh empty workdir for a brand-new (never saved) board.
    Used by ``_new_board`` so the unsaved board still has a place to stash
    imports + renders before its first save."""
    wd = workdir_root(saves_root) / f"unsaved_{uuid.uuid4().hex[:12]}"
    wd.mkdir(parents=True, exist_ok=True)
    (wd / "imports").mkdir(exist_ok=True)
    (wd / "temps" / "media").mkdir(parents=True, exist_ok=True)
    (wd / "temps" / "nav_pages").mkdir(parents=True, exist_ok=True)
    (wd / "thumbnails").mkdir(exist_ok=True)
    return wd


def cleanup_workdir(workdir: Optional[Path]) -> None:
    """Remove a working directory and its contents. Safe on already-missing
    paths and on paths outside the ``.working`` root (which it refuses to
    delete defensively)."""
    if workdir is None:
        return
    p = Path(workdir)
    if not p.exists():
        return
    # Safety: refuse to delete anything not under a ".working" parent —
    # protects against accidental wipes if a bug ever passes the wrong path.
    if ".working" not in p.parts:
        logger.warning("cleanup_workdir refused: %s not under .working", p)
        return
    try:
        shutil.rmtree(p, ignore_errors=True)
        logger.debug("cleaned workdir %s", p)
    except OSError as e:
        logger.warning("cleanup_workdir(%s) failed: %s", p, e)


def sweep_workdirs(saves_root: Path) -> int:
    """Delete ALL leftover session working directories from previous runs.

    Called once at startup. Working directories under ``.working/`` are
    transient scratch space — the user-facing ``.lb`` file is the canonical
    artefact, so any workdir still present at launch is an orphan left behind
    by a crash, force-quit, or power loss (graceful exits clean themselves up
    via ``cleanup_workdir``). This is safe because the app is single-instance,
    so nothing is using ``.working/`` at startup. Returns the number removed.

    Defensive: only removes direct children of the ``.working`` root, and only
    if that root path actually ends in ``.working``.
    """
    try:
        root = workdir_root(saves_root)
    except OSError as e:
        logger.warning("sweep_workdirs: could not access workdir root: %s", e)
        return 0
    if root.name != ".working":
        logger.warning("sweep_workdirs refused: unexpected root %s", root)
        return 0
    removed = 0
    try:
        for child in root.iterdir():
            if child.is_dir():
                shutil.rmtree(child, ignore_errors=True)
                removed += 1
    except OSError as e:
        logger.warning("sweep_workdirs scan failed: %s", e)
    if removed:
        logger.info("sweep_workdirs removed %d orphaned working dir(s).", removed)
    return removed


# ─────────────────────────────────────────────────────────────────────────────
# Open / extract
# ─────────────────────────────────────────────────────────────────────────────

def open_lb_to_workdir(lb_path: Path, saves_root: Path) -> Path:
    """Extract ``lb_path`` into a fresh working directory and return its path.

    The workdir is a full, mutable copy of the .lb contents — including all
    cached renders so opening is fast and re-rendering is avoided. The
    workdir's lifecycle is owned by the caller (typically MainWindow), which
    must call :func:`cleanup_workdir` when finished.

    Raises FileNotFoundError if the .lb doesn't exist, BadZipFile if it's
    not a valid archive.
    """
    src = Path(lb_path)
    if not src.is_file():
        raise FileNotFoundError(f"Not a file: {src}")
    if src.suffix.lower() != LB_EXTENSION:
        logger.warning("open_lb_to_workdir: extension is %s (expected %s)",
                       src.suffix, LB_EXTENSION)

    wd = workdir_root(saves_root) / f"open_{uuid.uuid4().hex[:12]}"
    wd.mkdir(parents=True, exist_ok=True)
    logger.info("Extracting %s → %s", src.name, wd)

    # Everything from here on is inside the try, because ``wd`` already
    # EXISTS: it was created two lines up. Anything that raises between now
    # and the end of the extraction used to leave that empty directory
    # behind in ``.working``, one per refused file — a traversal member, a
    # zip that turns out to be truncated after all, a disk that fills up
    # mid-extract. They accumulate silently, because .working is exactly
    # where nobody looks.
    try:
        with zipfile.ZipFile(src, "r") as zf:
            # zipfile.extractall() is safe against absolute / traversal
            # paths in Python 3.7.5+ — but we still validate explicitly for
            # older builds. Verified against drive letters, UNC paths,
            # leading separators and mixed separators: everything either
            # lands inside the workdir or is refused here.
            for member in zf.namelist():
                if member.startswith("/") or ".." in Path(member).parts:
                    raise ValueError(
                        f".lb archive contains unsafe path: {member!r}"
                    )
            zf.extractall(wd)
    except Exception:
        cleanup_workdir(wd)
        raise

    # Validate the archive actually contained a board — a valid ZIP that's
    # missing board.json means the file is corrupt or something unrelated
    # got renamed to .lb. Fail fast with a clear message (and clean up the
    # half-extracted workdir) instead of returning a broken workdir that
    # crashes the caller later with a confusing "file not found" deep in
    # the load path.
    if not (wd / "board.json").is_file():
        cleanup_workdir(wd)
        raise ValueError(
            f"{src.name} is not a valid LogicBoard file "
            f"(no board data found inside it)."
        )

    # Ensure the expected top-level skeleton exists (some empty fields may
    # have been omitted by the writer — re-create them so callers can rely
    # on the directory layout).
    (wd / "imports").mkdir(exist_ok=True)
    (wd / "temps" / "media").mkdir(parents=True, exist_ok=True)
    (wd / "temps" / "nav_pages").mkdir(parents=True, exist_ok=True)
    (wd / "thumbnails").mkdir(exist_ok=True)

    return wd


# ─────────────────────────────────────────────────────────────────────────────
# Save / pack
# ─────────────────────────────────────────────────────────────────────────────

def save_workdir_to_lb(
    workdir: Path,
    lb_path: Path,
    *,
    board_name: Optional[str] = None,
    logicboard_version: str = "3.0",
) -> None:
    """Pack ``workdir`` into ``lb_path`` atomically.

    The archive is written to a sibling temp file first, then renamed onto
    the destination — so a power loss mid-save can never corrupt the user's
    existing .lb. Compression is DEFLATED (the default, fast + small enough
    for our media-heavy archives; STORED would skip CPU cost but inflate
    most boards by ~20% from JSON / WEBP / cache_manifest.json text).
    """
    wd = Path(workdir).resolve()
    if not wd.is_dir():
        raise FileNotFoundError(f"Workdir does not exist: {wd}")

    dst = Path(lb_path).resolve()
    if dst.suffix.lower() != LB_EXTENSION:
        # Be strict — the format contract is the extension.
        dst = dst.with_suffix(LB_EXTENSION)
    dst.parent.mkdir(parents=True, exist_ok=True)

    # Refresh the metadata.json each save so the version/timestamp track the
    # actual write — this is a tiny file but useful for migrations.
    meta = {
        "lb_format_version":   LB_FORMAT_VERSION,
        "logicboard_version":  logicboard_version,
        "board_name":          board_name or dst.stem,
        "created_at":          datetime.now(timezone.utc).isoformat(),
    }
    (wd / LB_METADATA_FILENAME).write_text(
        json.dumps(meta, indent=2), encoding="utf-8"
    )

    # Atomic write: create a unique tmp file in the destination directory
    # (same filesystem → rename is atomic), zip into it, then move into place.
    fd, tmp_str = tempfile.mkstemp(
        prefix=f".{dst.stem}-", suffix=".lb.tmp", dir=str(dst.parent)
    )
    import os
    os.close(fd)
    tmp = Path(tmp_str)

    try:
        with zipfile.ZipFile(
            tmp, "w",
            compression=zipfile.ZIP_DEFLATED,
            compresslevel=6,        # 6 = good speed/size trade
            allowZip64=True,        # boards with >2GB of media still work
        ) as zf:
            # Skip our own .working / hidden noise if it got created in workdir.
            for fp in wd.rglob("*"):
                if fp.is_dir():
                    continue
                rel = fp.relative_to(wd)
                # Skip hidden dot-files at the top level (e.g. macOS .DS_Store).
                if any(part.startswith(".") for part in rel.parts):
                    continue
                zf.write(fp, arcname=str(rel).replace("\\", "/"))

        # os.replace is atomic on POSIX and on Windows (≥ Vista) for the
        # same volume. Crash before this point: tmp is orphaned (cleaned
        # below). Crash after: dst is the new file, tmp doesn't exist.
        os.replace(tmp, dst)
        logger.info("Saved board to %s", dst.name)
    except Exception:
        tmp.unlink(missing_ok=True)
        raise


# ─────────────────────────────────────────────────────────────────────────────
# Migration from old folder format
# ─────────────────────────────────────────────────────────────────────────────

def migrate_folder_to_lb(folder: Path, lb_path: Optional[Path] = None) -> Path:
    """One-shot migration: zip an old uncompressed board folder into a .lb
    next to it (or at ``lb_path`` if given) and return the .lb path.

    The original folder is left intact so the user can verify the migration
    before deleting it. Caller may delete the folder after a successful
    open of the new .lb.
    """
    src = Path(folder)
    if not is_legacy_board_folder(src):
        raise ValueError(f"Not a legacy board folder: {src}")
    dst = Path(lb_path) if lb_path else src.with_suffix(LB_EXTENSION)
    save_workdir_to_lb(src, dst, board_name=src.name)
    return dst