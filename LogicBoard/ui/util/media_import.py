"""
media_import.py — Convert PDFs and PowerPoint files into page/slide images.

Strategy (per the project's chosen toolchain):
  * PDF  -> PyMuPDF (fitz) rasterizes each page to a PNG.
  * PPT  -> LibreOffice headless converts .ppt/.pptx to a PDF, then PyMuPDF
            rasterizes each slide. (LibreOffice gives far better fidelity than
            python-pptx for visual rendering.)

All produced PNGs are written into the board's imports/ folder (caller passes
the destination dir). Functions degrade gracefully and return [] / None with a
logged warning if a dependency is missing, so the app never crashes on import.

This module is UI-agnostic and importable without PySide6.
"""
from __future__ import annotations

import logging
import shutil
import subprocess
import tempfile
import uuid
from pathlib import Path
from typing import List, Optional

logger = logging.getLogger(__name__)

__all__ = [
    "have_pymupdf",
    "find_libreoffice",
    "render_pdf_to_images",
    "render_ppt_to_images",
    "pdf_page_count",
]

# Render DPI for rasterizing PDF/PPT pages. 150 is a good quality/size balance.
DEFAULT_DPI = 150


def have_pymupdf() -> bool:
    try:
        import fitz  # noqa: F401  (PyMuPDF)
        return True
    except Exception:
        return False


def find_libreoffice() -> Optional[str]:
    """Locate the LibreOffice/soffice executable across platforms."""
    # 1. On PATH.
    for name in ("soffice", "libreoffice"):
        p = shutil.which(name)
        if p:
            return p
    # 2. Common Windows install locations.
    candidates = [
        r"C:\Program Files\LibreOffice\program\soffice.exe",
        r"C:\Program Files (x86)\LibreOffice\program\soffice.exe",
        "/Applications/LibreOffice.app/Contents/MacOS/soffice",
        "/usr/bin/soffice",
        "/usr/bin/libreoffice",
    ]
    for c in candidates:
        if Path(c).exists():
            return c
    return None


def _unique_stem(prefix: str) -> str:
    return f"{prefix}_{uuid.uuid4().hex[:6]}"


def pdf_page_count(pdf_path: Path) -> int:
    if not have_pymupdf():
        return 0
    _silence_mupdf()
    import fitz
    doc = None
    try:
        doc = fitz.open(str(pdf_path))
        return doc.page_count
    except Exception as e:
        logger.warning("pdf_page_count failed: %s", e)
        return 0
    finally:
        # Guarantee the handle is released even if page_count raises on a
        # corrupt PDF — otherwise fitz keeps the file open until GC, and
        # any later attempt to move/delete the source fails on Windows.
        if doc is not None:
            try: doc.close()
            except Exception: pass


def _silence_mupdf():
    """Silence MuPDF's non-fatal stderr warnings (e.g. 'No common ancestor in
    structure tree' from LibreOffice-generated PDFs). The rendering output is
    unaffected; this just keeps the console clean."""
    try:
        import fitz
        # Two API styles depending on PyMuPDF version.
        try:
            fitz.TOOLS.mupdf_display_errors(False)
        except Exception:
            pass
        try:
            fitz.TOOLS.mupdf_display_warnings(False)
        except Exception:
            pass
    except Exception:
        pass


def render_pdf_to_images(pdf_path: Path, out_dir: Path,
                         dpi: int = DEFAULT_DPI,
                         name_prefix: str = "pdfpage") -> List[str]:
    """
    Rasterize each page of a PDF to a PNG inside out_dir.
    Returns a list of FILE NAMES (not full paths), in page order.
    Aspect ratio is preserved (PyMuPDF renders at the page's native ratio).
    """
    if not have_pymupdf():
        logger.warning("PyMuPDF (fitz) not installed — cannot import PDF.")
        return []

    _silence_mupdf()
    import fitz
    pdf_path = Path(pdf_path)
    out_dir = Path(out_dir)
    out_dir.mkdir(parents=True, exist_ok=True)

    names: List[str] = []
    doc = None
    try:
        doc = fitz.open(str(pdf_path))
        zoom = dpi / 72.0
        matrix = fitz.Matrix(zoom, zoom)
        stem = _unique_stem(name_prefix)
        for i in range(doc.page_count):
            page = doc.load_page(i)
            pix = page.get_pixmap(matrix=matrix, alpha=False)
            fname = f"{stem}_{i + 1:03d}.png"
            pix.save(str(out_dir / fname))
            names.append(fname)
    except Exception as e:
        logger.warning("render_pdf_to_images failed: %s", e)
        return names
    finally:
        # Always close — a mid-loop failure would otherwise leak the PDF
        # handle and lock the source file on Windows.
        if doc is not None:
            try: doc.close()
            except Exception: pass
    return names


def render_ppt_to_images(ppt_path: Path, out_dir: Path,
                         dpi: int = DEFAULT_DPI,
                         name_prefix: str = "slide") -> List[str]:
    """
    Convert a .ppt/.pptx to images, one per slide, via LibreOffice -> PDF ->
    PyMuPDF rasterization. Returns FILE NAMES in slide order.
    """
    soffice = find_libreoffice()
    if not soffice:
        logger.warning("LibreOffice (soffice) not found — cannot import PPT.")
        return []
    if not have_pymupdf():
        logger.warning("PyMuPDF (fitz) not installed — cannot rasterize PPT.")
        return []

    ppt_path = Path(ppt_path)
    out_dir = Path(out_dir)
    out_dir.mkdir(parents=True, exist_ok=True)

    with tempfile.TemporaryDirectory() as tmp:
        tmp_dir = Path(tmp)
        # LibreOffice headless: convert ppt -> pdf into tmp_dir.
        try:
            result = subprocess.run(
                [soffice, "--headless", "--convert-to", "pdf",
                 "--outdir", str(tmp_dir), str(ppt_path)],
                capture_output=True, timeout=120,
            )
            if result.returncode != 0:
                logger.warning("LibreOffice convert failed: %s",
                               result.stderr.decode(errors="ignore"))
        except Exception as e:
            logger.warning("LibreOffice invocation failed: %s", e)
            return []

        # Find the produced PDF.
        pdfs = list(tmp_dir.glob("*.pdf"))
        if not pdfs:
            logger.warning("LibreOffice produced no PDF for %s", ppt_path.name)
            return []

        return render_pdf_to_images(pdfs[0], out_dir, dpi=dpi,
                                    name_prefix=name_prefix)