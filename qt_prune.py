"""
qt_prune.py — strip a PyInstaller --onedir bundle down to what it loads.

Shared by LogicBoard's and LogicShare's packaging scripts:

    from qt_prune import prune_bundle
    prune_bundle(dist / "LogicBoard")          # after PyInstaller succeeds

PyInstaller's PySide6 hook collects Qt by MODULE, and a Qt module drags in
plugins and libraries the app never touches: the 20 MB software-OpenGL
fallback, QtQuick and Qml (pulled in by the virtual-keyboard input plugin),
the TLS backends with a second OpenSSL, every image format, translations
for forty languages. None of it can be excluded as a Python module, because
none of it IS one.

So this deletes a known list of candidates — but never blindly:

  1. A candidate DLL that anything remaining still IMPORTS is kept. The
     import tables are read from the PE files themselves, so a future Qt
     that moves a dependency around is caught rather than broken.
  2. After deleting, every import of every remaining binary must resolve —
     to a file in the bundle, or to Windows (System32, or an API set). If
     one does not, this raises, and the build fails instead of shipping an
     app that cannot start.

Plugins are not in any import table (Qt loads them by directory), which is
why the plugin list is explicit and conservative: only plugin kinds these
apps provably never use.
"""
from __future__ import annotations

import os
import shutil
import struct
from pathlib import Path
from typing import Iterable

__all__ = ["prune_bundle", "pe_imports", "unresolved_imports"]

# Files and folders (relative to the PySide6 package dir) that the two apps
# never load. Image formats: PNG is built in; JPEG, WebP, GIF, ICO and SVG
# stay — boards hold photos, the page cache is WebP, the icons are SVG.
_QT_CANDIDATES = [
    "opengl32sw.dll",                     # software GL fallback; we paint with QPainter
    "Qt6Quick.dll", "Qt6QuickWidgets.dll", "Qt6Qml.dll", "Qt6QmlModels.dll",
    "Qt6QmlMeta.dll", "Qt6QmlWorkerScript.dll", "Qt6VirtualKeyboard.dll",
    "Qt6VirtualKeyboardQml.dll", "Qt6Pdf.dll", "Qt6OpenGL.dll",
    "Qt6OpenGLWidgets.dll", "Qt6Multimedia.dll", "Qt6MultimediaWidgets.dll",
    "avcodec-61.dll", "avformat-61.dll", "avutil-59.dll", "swscale-8.dll",
    "swresample-5.dll",
    "translations",
    "plugins/platforminputcontexts",      # Qt's own virtual keyboard
    "plugins/tls",                        # no Qt TLS; HTTPS goes through WinINet/requests
    "plugins/networkinformation",
    "plugins/generic",                    # TUIO touch-over-UDP
    "plugins/multimedia",
    "plugins/platforms/qdirect2d.dll",
    "plugins/platforms/qminimal.dll",
    "plugins/platforms/qoffscreen.dll",
    "plugins/imageformats/qtiff.dll",
    "plugins/imageformats/qpdf.dll",
    "plugins/imageformats/qtga.dll",
    "plugins/imageformats/qwbmp.dll",
    "plugins/imageformats/qicns.dll",
]

# Relative to the bundle's _internal dir.
_ROOT_CANDIDATES = [
    "libcrypto-3-x64.dll", "libssl-3-x64.dll",   # only Qt's TLS plugin used these
]

_PE_SUFFIXES = {".dll", ".pyd", ".exe"}


# ── PE import tables ────────────────────────────────────────────────────────

def pe_imports(path: Path) -> list[str]:
    """DLL names in a PE file's import AND delay-import directories."""
    try:
        d = path.read_bytes()
    except OSError:
        return []
    if len(d) < 0x40 or d[:2] != b"MZ":
        return []
    pe = struct.unpack_from("<I", d, 0x3C)[0]
    if d[pe:pe + 4] != b"PE\0\0":
        return []
    nsec = struct.unpack_from("<H", d, pe + 6)[0]
    opt = pe + 24
    magic = struct.unpack_from("<H", d, opt)[0]
    dd = opt + (112 if magic == 0x20B else 96)
    sec0 = opt + struct.unpack_from("<H", d, pe + 20)[0]
    secs = []
    for i in range(nsec):
        o = sec0 + 40 * i
        vsize, va, rsize, raw = struct.unpack_from("<IIII", d, o + 8)
        secs.append((va, max(vsize, rsize), raw))

    def off(rva):
        for va, size, raw in secs:
            if va <= rva < va + size:
                return rva - va + raw
        return None

    def cstr(o):
        e = d.find(b"\0", o)
        return d[o:e].decode("ascii", "replace")

    names = []
    # Regular imports: data directory 1, 20-byte descriptors, name at +12.
    rva = struct.unpack_from("<I", d, dd + 8)[0]
    o = off(rva) if rva else None
    while o is not None and o + 20 <= len(d):
        name_rva = struct.unpack_from("<I", d, o + 12)[0]
        if not name_rva:
            break
        no = off(name_rva)
        if no is not None:
            names.append(cstr(no))
        o += 20
    # Delay imports: data directory 13, 32-byte descriptors, name at +4.
    rva = struct.unpack_from("<I", d, dd + 8 * 13)[0]
    o = off(rva) if rva else None
    while o is not None and o + 32 <= len(d):
        name_rva = struct.unpack_from("<I", d, o + 4)[0]
        if not name_rva:
            break
        no = off(name_rva)
        if no is not None:
            names.append(cstr(no))
        o += 32
    return names


def _pe_files(root: Path) -> Iterable[Path]:
    for p in root.rglob("*"):
        if p.is_file() and p.suffix.lower() in _PE_SUFFIXES:
            yield p


def _system_dlls() -> set[str]:
    sysroot = Path(os.environ.get("SystemRoot", r"C:\Windows")) / "System32"
    try:
        return {p.name.lower() for p in sysroot.iterdir()}
    except OSError:
        return set()


def unresolved_imports(bundle: Path) -> dict[str, list[str]]:
    """{dll name: [importers]} for every import nothing can satisfy."""
    present = {p.name.lower() for p in _pe_files(bundle)}
    system = _system_dlls()
    missing: dict[str, list[str]] = {}
    for f in _pe_files(bundle):
        for name in pe_imports(f):
            low = name.lower()
            if (low in present or low in system
                    or low.startswith(("api-ms-win-", "ext-ms-"))):
                continue
            missing.setdefault(name, []).append(str(f.relative_to(bundle)))
    return missing


# ── pruning ─────────────────────────────────────────────────────────────────

def _size(p: Path) -> int:
    if p.is_file():
        return p.stat().st_size
    return sum(q.stat().st_size for q in p.rglob("*") if q.is_file())


def prune_bundle(bundle: Path, *, verbose: bool = True) -> int:
    """Delete what the app never loads from a --onedir bundle. Returns the
    bytes removed. Raises RuntimeError if the result has a broken import."""
    bundle = Path(bundle)
    internal = bundle / "_internal"
    if not internal.is_dir():
        internal = bundle
    qt = internal / "PySide6"
    cands = [qt / c for c in _QT_CANDIDATES] + [internal / c for c in _ROOT_CANDIDATES]
    cands = [c for c in cands if c.exists()]
    doomed_files = set()
    for c in cands:
        if c.is_dir():
            doomed_files.update(q.resolve() for q in c.rglob("*") if q.is_file())
        else:
            doomed_files.add(c.resolve())

    # Rule 1: never delete a DLL that a SURVIVING binary imports. Iterate,
    # since keeping one candidate can make its own imports needed too.
    while True:
        survivors = [p for p in _pe_files(internal) if p.resolve() not in doomed_files]
        wanted = {n.lower() for p in survivors for n in pe_imports(p)}
        rescued = [c for c in cands if c.is_file() and c.name.lower() in wanted
                   and c.resolve() in doomed_files]
        if not rescued:
            break
        for c in rescued:
            doomed_files.discard(c.resolve())
            if verbose:
                print(f"  keep   {c.relative_to(bundle)}  (still imported)")
        cands = [c for c in cands if c not in rescued]

    removed = 0
    for c in cands:
        if not c.exists():
            continue
        n = _size(c)
        removed += n
        if verbose:
            print(f"  prune  {str(c.relative_to(bundle)):60s} {n / 1048576:7.2f} MB")
        if c.is_dir():
            shutil.rmtree(c)
        else:
            c.unlink()

    # Rule 2: everything left must still load.
    missing = unresolved_imports(bundle)
    if missing:
        lines = "\n".join(f"    {dll}  <- {', '.join(by[:3])}"
                          for dll, by in sorted(missing.items()))
        raise RuntimeError("pruned bundle has unresolved imports:\n" + lines)
    if verbose:
        print(f"  pruned {removed / 1048576:.1f} MB; every import still resolves")
    return removed
