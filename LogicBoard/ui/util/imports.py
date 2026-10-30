"""
imports.py — Manage a board's "imports" folder.

Every saved board is a folder that contains:
    <board>/board.json
    <board>/imports/        <- all imported media live here

Imported files (images, .pptx, .pdf, .txt) are COPIED into <board>/imports/
with a short unique suffix so two files of the same name never collide. The
engine objects (ImageObject / PptObject) store their `src` as a path RELATIVE
to the board folder, e.g. "imports/photo_a1b2c3.png", which keeps a board fully
self-contained and portable.

This module is deliberately UI-agnostic: it only does filesystem work, so it
can be unit-tested and reused by any future toolbar import action.
"""

from __future__ import annotations

import shutil
import uuid
from pathlib import Path
from typing import Optional

__all__ = [
    "IMPORTS_DIRNAME",
    "IMAGE_EXTS",
    "PPT_EXTS",
    "PDF_EXTS",
    "TEXT_EXTS",
    "kind_for_extension",
    "ensure_imports_dir",
    "import_file",
    "resolve_src",
]

IMPORTS_DIRNAME = "imports"

IMAGE_EXTS = {".png", ".jpg", ".jpeg", ".bmp", ".gif", ".webp", ".tiff", ".svg"}
PPT_EXTS = {".ppt", ".pptx"}
PDF_EXTS = {".pdf"}
TEXT_EXTS = {".txt", ".md"}


def kind_for_extension(suffix: str) -> Optional[str]:
    """Return 'image' | 'ppt' | 'pdf' | 'text' for a file extension, else None."""
    s = suffix.lower()
    if s in IMAGE_EXTS:
        return "image"
    if s in PPT_EXTS:
        return "ppt"
    if s in PDF_EXTS:
        return "pdf"
    if s in TEXT_EXTS:
        return "text"
    return None


def ensure_imports_dir(board_dir: Path) -> Path:
    """Create (if needed) and return the board's imports/ folder."""
    board_dir = Path(board_dir)
    imports = board_dir / IMPORTS_DIRNAME
    imports.mkdir(parents=True, exist_ok=True)
    return imports


def _unique_name(stem: str, suffix: str) -> str:
    """Build a collision-resistant filename: <stem>_<6hex><suffix>."""
    short = uuid.uuid4().hex[:6]
    # Sanitize the stem to safe characters.
    safe = "".join(c if (c.isalnum() or c in "-_") else "_" for c in stem)[:48]
    if not safe:
        safe = "file"
    return f"{safe}_{short}{suffix.lower()}"


def import_file(board_dir: Path, source_path: Path) -> Optional[str]:
    """
    Copy `source_path` into <board_dir>/imports/ with a unique name.

    Returns the board-relative path string (e.g. "imports/photo_a1b2c3.png")
    suitable for storing in an ImageObject/PptObject `src`, or None on failure.
    """
    source_path = Path(source_path)
    if not source_path.exists() or not source_path.is_file():
        return None

    imports = ensure_imports_dir(board_dir)
    target_name = _unique_name(source_path.stem, source_path.suffix)
    target = imports / target_name

    try:
        shutil.copy2(source_path, target)
    except OSError:
        return None

    # Always use forward slashes in the stored relative path for portability.
    return f"{IMPORTS_DIRNAME}/{target_name}"


def resolve_src(board_dir: Path, src: str) -> Optional[Path]:
    """
    Turn a stored board-relative `src` back into an absolute path on disk.
    Returns None if the board folder or file does not exist.
    """
    if not board_dir or not src:
        return None
    candidate = Path(board_dir) / src
    return candidate if candidate.exists() else None
