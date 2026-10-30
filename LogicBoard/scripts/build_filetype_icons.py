#!/usr/bin/env python3
"""
build_filetype_icons.py — the .ico files Windows shows for PDF and PowerPoint
files that LogicBoard opens.

Explorer cannot draw an SVG as a file icon; it wants a real multi-frame .ico,
and it wants the small frames drawn at their own size rather than squeezed
out of a 256 on the fly (see rebuild_icon.py for what that looks like). So
the artwork the app already uses — ``assets/icons/pdf.svg`` and
``assets/icons/ppt.svg`` — is rendered once per standard frame size with Qt's
SVG renderer and packed into ``pdf.ico`` / ``ppt.ico`` beside it.

The SVG documents sit inside x 12..52 of a 64-unit box. Rendered as-is they
would be a narrow page in a lot of empty tile, visibly smaller than the
neighbouring icons in an Explorer column — so the viewBox is tightened to the
page itself (with a hair of margin) before rendering.

Usage
-----
    python scripts/build_filetype_icons.py          # write the .ico files
    python scripts/build_filetype_icons.py --check  # report only
"""
from __future__ import annotations

import io
import os
import sys
from pathlib import Path

os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")
ROOT = Path(__file__).resolve().parent.parent
ICONS = ROOT / "assets" / "icons"

# Every size Windows asks for, from the Details view up to Extra large icons.
SIZES = (16, 20, 24, 32, 40, 48, 64, 96, 128, 256)

# The page occupies y 8..58 and x 12..52; a square box around its taller
# side, centred on it, keeps the aspect and fills the tile.
VIEWBOX = "6 7 52 52"

SOURCES = {"pdf.svg": "pdf.ico", "ppt.svg": "ppt.ico"}


def _render(svg_bytes: bytes, side: int):
    from PySide6.QtCore import QByteArray, QRectF, Qt
    from PySide6.QtGui import QImage, QPainter
    from PySide6.QtSvg import QSvgRenderer

    r = QSvgRenderer(QByteArray(svg_bytes))
    r.setViewBox(QRectF(*(float(v) for v in VIEWBOX.split())))
    img = QImage(side, side, QImage.Format_ARGB32_Premultiplied)
    img.fill(Qt.transparent)
    p = QPainter(img)
    p.setRenderHint(QPainter.Antialiasing)
    p.setRenderHint(QPainter.TextAntialiasing)
    r.render(p)
    p.end()
    return img


def _to_pil(img):
    from PIL import Image
    from PySide6.QtCore import QBuffer, QIODevice
    buf = QBuffer()
    buf.open(QIODevice.WriteOnly)
    img.save(buf, "PNG")
    return Image.open(io.BytesIO(bytes(buf.data()))).convert("RGBA")


def build(check: bool = False) -> int:
    from PySide6.QtWidgets import QApplication
    _app = QApplication.instance() or QApplication([])   # noqa: F841 (fonts)
    for svg_name, ico_name in SOURCES.items():
        svg = (ICONS / svg_name).read_bytes()
        frames = [_to_pil(_render(svg, s)) for s in SIZES]
        out = ICONS / ico_name
        if check:
            print(f"{ico_name}: would write {len(frames)} frames "
                  f"({', '.join(map(str, SIZES))})")
            continue
        # Pillow writes one frame per requested size, picking each from the
        # image whose size matches — so every frame is the one drawn at that
        # size, not a resample of the largest.
        big = frames[-1]
        big.save(out, format="ICO", sizes=[(s, s) for s in SIZES],
                 append_images=frames[:-1])
        print(f"wrote {out.relative_to(ROOT)} ({out.stat().st_size} bytes)")
    return 0


if __name__ == "__main__":
    sys.exit(build(check="--check" in sys.argv[1:]))
