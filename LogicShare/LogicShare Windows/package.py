#!/usr/bin/env python3
"""
package.py

Packages LogicShare into a single EXE.

Project Structure
-----------------
LogicShare/
│
├── LogicShare.py
├── LogicShare.ico
├── LogicShare.svg
└── package.py
"""

from pathlib import Path
import subprocess
import shutil
import sys
import os
import stat


def remove_readonly(func, path, excinfo):
    try:
        os.chmod(path, stat.S_IWRITE)
        func(path)
    except Exception:
        pass


def main():

    root = Path(__file__).resolve().parent

    app = root / "LogicShare.py"
    icon = root / "LogicShare.ico"
    svg = root / "LogicShare.svg"

    dist = root / "dist"
    build = root / "build"
    spec = root / "LogicShare.spec"

    sep = ";" if os.name == "nt" else ":"

    print("=" * 60)
    print("Building LogicShare")
    print("=" * 60)

    # -------------------------------------------------
    # Clean
    # -------------------------------------------------

    if dist.exists():
        print("Removing dist...")
        shutil.rmtree(dist, onerror=remove_readonly)

    if build.exists():
        print("Removing build...")
        shutil.rmtree(build, onerror=remove_readonly)

    if spec.exists():
        print("Removing old spec...")
        spec.unlink()

    # -------------------------------------------------
    # PyInstaller command
    # -------------------------------------------------

    cmd = [
        sys.executable,
        "-m",
        "PyInstaller",

        "--noconfirm",
        "--clean",

        "--onedir",
        "--windowed",

        "--name", "LogicShare",

        "--icon", str(icon),

        # Files needed at runtime
        "--add-data", f"{svg}{sep}.",
        "--add-data", f"{icon}{sep}.",

        # Third-party packages.
        #
        # pywinstyles is gone: the acrylic backdrop is applied by calling
        # SetWindowCompositionAttribute directly (see _apply_acrylic), because
        # the library's "acrylic" preset hardcodes a zero-alpha tint that
        # leaves the UI unreadable over a white background.
        "--hidden-import", "requests",
        "--hidden-import", "zeroconf",

        # net_watch sits next to LogicShare.py and is a plain static import,
        # so PyInstaller finds it on its own. Named anyway: it is the module
        # that decides the app has changed networks, and a build that
        # silently omitted it would still start, still scan, and simply stop
        # noticing Wi-Fi hops — a failure with no error message attached.
        "--hidden-import", "net_watch",

        # single_instance is what stops a second launch from becoming a
        # second process; it speaks over a QLocalServer, which lives in
        # QtNetwork. Nothing else in the app imports QtNetwork, so name it.
        "--hidden-import", "single_instance",
        "--hidden-import", "PySide6.QtNetwork",

        # firewall.py — the "Allow network access" banner. pywin32 reads the
        # firewall rules over COM and elevates LogicShare through UAC; every
        # import is inside a function, and win32com resolves interfaces
        # dynamically, so name them or the frozen app silently answers
        # "unknown" forever and the banner never appears.
        "--hidden-import", "firewall",
        "--hidden-import", "pythoncom",
        "--hidden-import", "pywintypes",
        "--hidden-import", "win32com",
        "--hidden-import", "win32com.client",
        "--hidden-import", "win32com.shell",
        "--hidden-import", "win32com.shell.shell",
        "--hidden-import", "win32event",
        "--hidden-import", "win32process",
        "--hidden-import", "win32con",

        # Size. None of these is imported by LogicShare; each arrives through
        # an OPTIONAL import in something that is (numpy and pandas behind
        # optional paths, lxml behind fontTools, Pillow behind an .ico helper
        # that nothing calls — the installer ships LogicShare.ico ready-made).
        # ~45 MB together. qt_prune below handles the Qt side.
        "--exclude-module", "numpy",
        "--exclude-module", "pandas",
        "--exclude-module", "lxml",
        "--exclude-module", "fontTools",
        "--exclude-module", "PIL",
        "--exclude-module", "tkinter",
        "--exclude-module", "matplotlib",
        "--exclude-module", "scipy",
        "--exclude-module", "IPython",
        "--exclude-module", "pytest",
        "--exclude-module", "PySide6.QtMultimedia",
        "--exclude-module", "PySide6.QtMultimediaWidgets",
        "--exclude-module", "PySide6.QtPdf",
        "--exclude-module", "PySide6.QtPdfWidgets",
        "--exclude-module", "PySide6.QtOpenGL",
        "--exclude-module", "PySide6.QtOpenGLWidgets",
        "--exclude-module", "PySide6.QtQml",
        "--exclude-module", "PySide6.QtQuick",
        "--exclude-module", "PySide6.QtQuickWidgets",
        "--exclude-module", "PySide6.QtWebEngineCore",
        "--exclude-module", "PySide6.QtWebEngineWidgets",
        "--exclude-module", "PySide6.Qt3DCore",

        str(app),
    ]

    print("\nRunning PyInstaller...\n")

    result = subprocess.run(cmd, cwd=root)

    print()

    if result.returncode == 0:
        # Strip the Qt plugins and libraries LogicShare never loads, then
        # prove every remaining import still resolves (raises if not).
        sys.path.insert(0, str(root.parents[1]))
        from qt_prune import prune_bundle
        print("Pruning unused Qt pieces...")
        prune_bundle(dist / "LogicShare")

        print("=" * 60)
        print("BUILD SUCCESSFUL")
        print("=" * 60)
        print("\nOutput:")
        print(dist / "LogicShare" / "LogicShare.exe")
    else:
        print("=" * 60)
        print("BUILD FAILED")
        print("=" * 60)

    return result.returncode


if __name__ == "__main__":
    raise SystemExit(main())