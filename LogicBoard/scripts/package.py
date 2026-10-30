"""
LogicBoard Packaging Script

Packages LogicBoard into a standalone executable using PyInstaller.

Included:
    ✔ assets/
    ✔ logicboard_engine.cp311-win_amd64.pyd
    ✔ ONLY the DLLs the engine actually links against (see engine_dlls)
    ✔ PySide6 (only the modules actually used)

Size notes — two things used to make the build enormous:

  * "every DLL in project root" shipped all 54 of them, ~31 MB. Those are
    MSYS2/CMake toolchain leftovers from building the engine — curl,
    gnutls, openssl, GCC's gmp/mpfr/isl, a debug-adapter library. Reading
    the engine's PE import table shows it links exactly three:
    libstdc++-6, libgcc_s_seh-1 and libwinpthread-1 (2.5 MB). The other
    51 were dead weight. ``engine_dlls`` now walks the import table
    instead of globbing, so this stays correct if the engine is ever
    rebuilt against something new.

  * ``--collect-data PySide6`` collected PySide6's data directories
    WHOLESALE, and module excludes don't apply to it: translations/ is
    59 MB and resources/ is 101 MB (almost entirely the QtWebEngine
    payload this build already excludes as a module). PyInstaller's own
    PySide6 hook collects the plugins and translations the imported
    modules actually need, so the flag is dropped.
"""

from pathlib import Path
import shutil
import struct
import subprocess
import sys
import stat
import os


# ----------------------------------------------------------
# Which DLLs does a binary actually link against?
# ----------------------------------------------------------

def pe_imports(path: Path) -> list[str]:
    """DLL names from a PE file's import directory. Pure stdlib."""
    d = path.read_bytes()
    pe = struct.unpack_from("<I", d, 0x3C)[0]
    if d[pe:pe + 4] != b"PE\0\0":
        return []
    nsec = struct.unpack_from("<H", d, pe + 6)[0]
    opt = pe + 24
    magic = struct.unpack_from("<H", d, opt)[0]
    dd = opt + (112 if magic == 0x20B else 96)
    imp_rva = struct.unpack_from("<I", d, dd + 8)[0]
    if not imp_rva:
        return []
    sec0 = opt + struct.unpack_from("<H", d, pe + 20)[0]
    secs = []
    for i in range(nsec):
        o = sec0 + 40 * i
        secs.append((struct.unpack_from("<I", d, o + 12)[0],
                     struct.unpack_from("<I", d, o + 16)[0],
                     struct.unpack_from("<I", d, o + 20)[0]))

    def r2o(rva):
        for va, sz, raw in secs:
            if va <= rva < va + max(sz, 1):
                return raw + (rva - va)
        return None

    out, o = [], r2o(imp_rva)
    if o is None:
        return out
    while True:
        ent = d[o:o + 20]
        if len(ent) < 20 or ent == b"\0" * 20:
            break
        name_rva = struct.unpack_from("<I", ent, 12)[0]
        if not name_rva:
            break
        no = r2o(name_rva)
        if no is None:
            break
        out.append(d[no:d.index(b"\0", no)].decode("ascii", "replace"))
        o += 20
    return out


def engine_dlls(project: Path, engine: Path) -> list[Path]:
    """The transitive closure of project-root DLLs the engine needs.

    Anything not reachable from the engine's import table is a build-time
    leftover and is left out of the bundle.
    """
    local = {p.name.lower(): p for p in project.glob("*.dll")}
    needed: set[str] = set()
    stack, seen = [engine], set()
    while stack:
        cur = stack.pop()
        key = cur.name.lower()
        if key in seen:
            continue
        seen.add(key)
        for imp in pe_imports(cur):
            low = imp.lower()
            if low in local and low not in needed:
                needed.add(low)
                stack.append(local[low])
    return [local[n] for n in sorted(needed)]


# ----------------------------------------------------------
# Delete read-only folders (Windows)
# ----------------------------------------------------------

def remove_readonly(func, path, excinfo):
    os.chmod(path, stat.S_IWRITE)
    func(path)


# ----------------------------------------------------------
# Main
# ----------------------------------------------------------

def main():

    project = Path(__file__).resolve().parent.parent
    app = project / "ui" / "app.py"
    assets = project / "assets"
    icon = assets / "icons" / "logo.ico"
    engine = project / "logicboard_engine.cp311-win_amd64.pyd"

    build = project / "build"
    dist = project / "dist"
    spec = project / "LogicBoard.spec"

    sep = ";" if sys.platform == "win32" else ":"

    print("=" * 65)
    print("Building LogicBoard")
    print("=" * 65)

    # ------------------------------------------------------
    # Clean previous builds
    # ------------------------------------------------------

    for folder in (build, dist):
        if folder.exists():
            print(f"Removing {folder.name}...")
            shutil.rmtree(folder, onerror=remove_readonly)

    if spec.exists():
        print("Removing old spec...")
        spec.unlink()

    # ------------------------------------------------------
    # Build command
    # ------------------------------------------------------

    cmd = [
        sys.executable,
        "-m",
        "PyInstaller",

        "--noconfirm",
        "--clean",

        "--onedir",
        "--windowed",

        "--name",
        "LogicBoard",

        "--icon",
        str(icon),

        # -----------------------------
        # Qt: collect only what we actually use.
        #
        # IMPORTANT: do NOT use --collect-all PySide6 here.
        # That forces PyInstaller to isolated-import every package
        # reachable from your app (including ui.toolbar, ui.theme,
        # etc.) outside of a running QApplication, just to probe for
        # binaries. If any module creates a QFont/QIcon/QPixmap/etc.
        # at *module import time* (not inside a function), that probe
        # crashes with exit code 3221226505 (0xC0000409 - stack buffer
        # overrun), which is exactly what you hit. Narrowing the Qt
        # collection avoids dragging in QtWebEngine/Qt3D/etc. and
        # shrinks the surface area for that crash.
        # -----------------------------

        "--hidden-import", "PySide6.QtCore",
        "--hidden-import", "PySide6.QtGui",
        "--hidden-import", "PySide6.QtWidgets",
        "--hidden-import", "PySide6.QtSvg",
        # NOT QtMultimedia: the click sound plays through winmm now (see
        # ui/util/audio.py), and QtMultimedia's FFmpeg backend was ~20 MB.
        "--hidden-import", "PySide6.QtNetwork",
        # NO "--collect-data PySide6" — see the size notes at the top of
        # this file. It bundles PySide6's data directories wholesale
        # (translations 59 MB + resources 101 MB, the latter almost
        # entirely QtWebEngine's payload) and ignores the module excludes
        # below while doing it. PyInstaller's built-in PySide6 hook
        # already collects the plugins/translations the imported modules
        # need.
        "--hidden-import", "windows_toasts",

        # pywin32, for ui/util/firewall.py.
        #
        # Every one of these is imported INSIDE a function, deliberately —
        # the firewall check must not cost anything on a machine that never
        # opens the LogicShare settings, and must not stop the app starting
        # on a build where pywin32 is missing. PyInstaller does follow
        # function-level imports, but win32com.client resolves its
        # interfaces through a dynamic Dispatch that static analysis cannot
        # see, so the shell/event/process modules have to be named.
        #
        # Without them the frozen app answers STATE_UNKNOWN forever: the
        # banner never appears, the button never appears, and the feature
        # silently does not exist in the only build that ships.
        "--hidden-import", "pythoncom",
        "--hidden-import", "pywintypes",
        "--hidden-import", "win32com",
        "--hidden-import", "win32com.client",
        "--hidden-import", "win32com.shell",
        "--hidden-import", "win32com.shell.shell",
        "--hidden-import", "win32event",
        "--hidden-import", "win32process",
        "--hidden-import", "win32con",

        "--exclude-module", "PySide6.Qt3DAnimation",
        "--exclude-module", "PySide6.Qt3DCore",
        "--exclude-module", "PySide6.Qt3DRender",
        "--exclude-module", "PySide6.Qt3DExtras",
        "--exclude-module", "PySide6.Qt3DInput",
        "--exclude-module", "PySide6.Qt3DLogic",
        "--exclude-module", "PySide6.QtWebEngineCore",
        "--exclude-module", "PySide6.QtWebEngineWidgets",
        "--exclude-module", "PySide6.QtWebEngineQuick",
        "--exclude-module", "PySide6.QtQml",
        "--exclude-module", "PySide6.QtQuick",
        "--exclude-module", "PySide6.QtQuickWidgets",
        "--exclude-module", "PySide6.QtQuick3D",
        "--exclude-module", "PySide6.QtQuickControls2",
        "--exclude-module", "PySide6.QtBluetooth",
        "--exclude-module", "PySide6.QtNfc",
        "--exclude-module", "PySide6.QtSerialPort",
        "--exclude-module", "PySide6.QtSerialBus",
        "--exclude-module", "PySide6.QtSensors",
        "--exclude-module", "PySide6.QtPositioning",
        "--exclude-module", "PySide6.QtLocation",
        "--exclude-module", "PySide6.QtRemoteObjects",
        "--exclude-module", "PySide6.QtScxml",
        "--exclude-module", "PySide6.QtTextToSpeech",
        "--exclude-module", "PySide6.QtDataVisualization",
        "--exclude-module", "PySide6.QtGraphs",
        "--exclude-module", "PySide6.QtGraphsWidgets",
        "--exclude-module", "PySide6.QtHttpServer",
        "--exclude-module", "PySide6.QtSpatialAudio",
        "--exclude-module", "PySide6.QtNetworkAuth",
        "--exclude-module", "PySide6.QtAxContainer",
        "--exclude-module", "PySide6.QtDesigner",
        "--exclude-module", "PySide6.QtHelp",
        "--exclude-module", "PySide6.QtDBus",
        "--exclude-module", "PySide6.QtCharts",
        "--exclude-module", "PySide6.QtStateMachine",
        "--exclude-module", "PySide6.scripts",
        "--exclude-module", "PySide6.QtMultimedia",
        "--exclude-module", "PySide6.QtMultimediaWidgets",
        "--exclude-module", "PySide6.QtPdf",
        "--exclude-module", "PySide6.QtPdfWidgets",
        "--exclude-module", "PySide6.QtOpenGL",
        "--exclude-module", "PySide6.QtOpenGLWidgets",
        # Reached only through OPTIONAL imports in libraries we use —
        # PyMuPDF's table extractor (pandas -> numpy) and fontTools' SVG
        # subsetting (lxml). LogicBoard calls neither. ~35 MB together.
        "--exclude-module", "numpy",
        "--exclude-module", "pandas",
        "--exclude-module", "lxml",
        "--exclude-module", "fontTools",
        "--exclude-module", "tkinter",
        "--exclude-module", "matplotlib",
        "--exclude-module", "scipy",
        "--exclude-module", "IPython",
        "--exclude-module", "pytest",
        # Pillow's AVIF codec, 7.5 MB. The page cache is WebP and nothing
        # reads or writes AVIF; Pillow disables the format when it is absent.
        "--exclude-module", "PIL._avif",
        "--exclude-module", "PIL.AvifImagePlugin",

        # -----------------------------
        # Assets
        # -----------------------------

        "--add-data",
        f"{assets}{sep}assets",

        # -----------------------------
        # Engine
        # -----------------------------

        "--add-binary",
        f"{engine}{sep}.",
    ]

    # ------------------------------------------------------
    # Add ONLY the DLLs the engine actually links against
    # ------------------------------------------------------

    present = sorted(project.glob("*.dll"))
    dlls = engine_dlls(project, engine)
    keep = {d.name.lower() for d in dlls}
    skipped = [p for p in present if p.name.lower() not in keep]

    print(f"\n{len(present)} DLL(s) in project root — "
          f"bundling {len(dlls)}, skipping {len(skipped)}")
    for dll in dlls:
        print(f" + {dll.name}  ({dll.stat().st_size / 1024 / 1024:.2f} MB)")
        cmd.extend([
            "--add-binary",
            f"{dll}{sep}."
        ])
    if skipped:
        saved = sum(p.stat().st_size for p in skipped) / 1024 / 1024
        print(f" - skipped {len(skipped)} unused build-time DLL(s), "
              f"{saved:.2f} MB saved")

    # ------------------------------------------------------

    cmd.append(str(app))

    print("\nRunning PyInstaller...\n")

    result = subprocess.run(
        cmd,
        cwd=project
    )

    print()

    if result.returncode == 0:
        # Strip the Qt plugins and libraries the app never loads, then
        # prove every remaining import still resolves (raises if not).
        sys.path.insert(0, str(project.parent))
        from qt_prune import prune_bundle
        print("Pruning unused Qt pieces...")
        prune_bundle(dist / "LogicBoard")

        print("=" * 65)
        print("BUILD SUCCESSFUL")
        print("=" * 65)
        print(f"\nOutput:\n{dist / 'LogicBoard'}")

    else:

        print("=" * 65)
        print("BUILD FAILED")
        print("=" * 65)

    return result.returncode


if __name__ == "__main__":
    raise SystemExit(main())