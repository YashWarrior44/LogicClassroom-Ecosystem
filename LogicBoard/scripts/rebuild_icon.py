#!/usr/bin/env python3
"""
rebuild_icon.py — make LogicBoard's icon fill its tile and stay sharp.

Two separate defects in the shipped ``assets/icons/logo.ico``, both measurable:

1. **It only contained ONE frame, 256x256.**
   Windows needs 16/20/24/32/40/48 for the taskbar, Alt-Tab, the tray and
   small list views. With only a 256 available it downscales on the fly with
   a cheap filter, which is why the icon looked soft next to apps that ship
   real small frames. A multi-frame .ico lets Windows *pick* rather than
   resample.

2. **The artwork used 85.2% x 72.7% of its canvas**, padded 31/36/7/34 px
   (left/top/right/bottom) — not even centred. Windows scales the whole
   256x256 tile, transparent margin included, so the visible glyph rendered
   noticeably smaller than neighbouring icons that fill their tile. That is
   the "LogicBoard's icon looks smaller than the others" report, on the
   desktop and in the taskbar alike.

The fix: crop to the real artwork, re-centre it on a square canvas at full
bleed (minus a hair of margin so resampling can't clip an edge), and emit
every standard frame size with a high-quality filter.

Usage
-----
    python scripts/rebuild_icon.py                  # rebuild in place
    python scripts/rebuild_icon.py --check          # report only, no writes

Originals are kept alongside as ``*.orig.ico`` the first time it runs.
"""
from __future__ import annotations

import shutil
import sys
from pathlib import Path

from PIL import Image

ROOT = Path(__file__).resolve().parent.parent

# Every place the LogicBoard icon is consumed from.
TARGETS = [
    ROOT / "assets" / "icons" / "logo.ico",
    ROOT / "LogicBoardInstaller" / "logo.ico",
]

# The frame sizes Windows actually asks for. 256 is required for the large
# "Extra large icons" view and the Store-style tile; 16-48 cover taskbar,
# Alt-Tab, tray and Explorer's list modes.
SIZES = [16, 20, 24, 32, 40, 48, 64, 96, 128, 256]

# Fraction of the canvas left as transparent margin on the longest side.
# Not zero: a glyph run right to the pixel edge picks up clipping artefacts
# when Windows resamples it, and every first-party Windows icon carries a
# little breathing room. 2% is about 5 px at 256 — invisible as space, enough
# to keep the edges clean.
MARGIN = 0.02


def load_best_frame(path: Path) -> Image.Image:
    """Largest frame in an .ico, as RGBA."""
    im = Image.open(path)
    sizes = sorted(im.ico.sizes()) if hasattr(im, "ico") else [im.size]
    im.size = max(sizes)
    return im.convert("RGBA")


def describe(path: Path) -> None:
    im = Image.open(path)
    sizes = sorted(im.ico.sizes()) if hasattr(im, "ico") else [im.size]
    best = load_best_frame(path)
    ink = best.split()[-1].getbbox()
    w, h = best.size
    print(f"  {path.name}")
    print(f"    frames : {sizes}")
    if ink:
        print(f"    artwork: {ink[2]-ink[0]}x{ink[3]-ink[1]} on {w}x{h} "
              f"({(ink[2]-ink[0])/w*100:.1f}% x {(ink[3]-ink[1])/h*100:.1f}%)")
        print(f"    padding: L{ink[0]} T{ink[1]} R{w-ink[2]} B{h-ink[3]}")


def rebuild(src: Image.Image) -> Image.Image:
    """Crop to the artwork and re-centre it at full bleed on a square canvas."""
    ink = src.split()[-1].getbbox()
    if ink is None:
        raise SystemExit("source icon is fully transparent")
    art = src.crop(ink)

    canvas = 1024                       # work large, downsample once per frame
    avail = int(canvas * (1 - 2 * MARGIN))
    aw, ah = art.size
    # Fit the LONGER side to the available box so the glyph is as large as it
    # can be without distorting. Aspect ratio is preserved — stretching a logo
    # to fill a square is worse than leaving one axis short.
    scale = min(avail / aw, avail / ah)
    new = (max(1, round(aw * scale)), max(1, round(ah * scale)))
    art = art.resize(new, Image.LANCZOS)

    out = Image.new("RGBA", (canvas, canvas), (0, 0, 0, 0))
    out.paste(art, ((canvas - new[0]) // 2, (canvas - new[1]) // 2))
    return out


def main() -> int:
    check_only = "--check" in sys.argv

    print("=== BEFORE ===")
    for t in TARGETS:
        if t.exists():
            describe(t)
        else:
            print(f"  {t}: MISSING")
    if check_only:
        return 0

    source = next((t for t in TARGETS if t.exists()), None)
    if source is None:
        raise SystemExit("no icon found to rebuild from")

    # Always rebuild from the pristine original, so running twice does not
    # crop-and-recentre an already-cropped image and creep the margin in.
    orig = source.with_suffix(".orig.ico")
    if not orig.exists():
        shutil.copy2(source, orig)
        print(f"\nkept original as {orig.name}")
    base = rebuild(load_best_frame(orig))

    print("\n=== WRITING ===")
    for t in TARGETS:
        t.parent.mkdir(parents=True, exist_ok=True)
        # Pillow builds each requested size from the image it is given, so a
        # single 1024 master downsampled per frame keeps all of them sharp.
        base.save(t, format="ICO", sizes=[(s, s) for s in SIZES])
        print(f"  wrote {t}  ({t.stat().st_size/1024:.1f} KB)")

    print("\n=== AFTER ===")
    for t in TARGETS:
        describe(t)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
