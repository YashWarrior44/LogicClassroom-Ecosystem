#!/usr/bin/env python3
"""
package_installer.py

The whole LogicShare release pipeline, end to end:

    1. package.py            -> dist/LogicShare/          (PyInstaller onedir)
    2. zip that folder       -> LogicShareInstaller/LogicShare.zip
    3. LogicShareInstaller/build.bat
                             -> LogicShareInstaller/LogicShareInstaller.exe

Run it with no arguments to do all three. ``--skip-app`` reuses whatever is
already in dist/ and only re-zips and re-links, which is what you want when
you have only touched the installer's C++.

Two things here are not obvious and are load-bearing:

* **The ZIP is written with LogicShare.exe at its ROOT**, not nested inside a
  "LogicShare/" folder. The installer does handle a single wrapping folder by
  stripping it, but only as a rescue; writing it flat means the layout the
  installer verifies (``<version folder>\\LogicShare.exe``) is the layout it
  was handed, with no inference in between.

* **MSVC is invoked from a path with no emoji in it.** The project lives under
  "Source Code<emoji>", and cmd.exe resolves relative command names against an
  OEM-codepage copy of the current directory — a conversion that fails on an
  emoji, so ``call build.bat`` reports "not recognized" while ``dir`` can see
  the file perfectly well. A temporary directory junction with an ASCII path
  sidesteps the whole class of problem, and is removed afterwards.
"""

from pathlib import Path
import argparse
import os
import shutil
import stat
import subprocess
import sys
import tempfile
import zipfile


ROOT = Path(__file__).resolve().parent
INSTALLER_DIR = ROOT / "LogicShareInstaller"
DIST_APP = ROOT / "dist" / "LogicShare"
ZIP_PATH = INSTALLER_DIR / "LogicShare.zip"
INSTALLER_EXE = INSTALLER_DIR / "LogicShareInstaller.exe"

VCVARS = Path(
    r"C:\Program Files\Microsoft Visual Studio\18\Community\VC\Auxiliary"
    r"\Build\vcvars64.bat"
)


def rule(text):
    print()
    print("=" * 70)
    print(text)
    print("=" * 70)


def remove_readonly(func, path, excinfo):
    try:
        os.chmod(path, stat.S_IWRITE)
        func(path)
    except Exception:
        pass


# ---------------------------------------------------------------------------
# 1. Build the app
# ---------------------------------------------------------------------------

def build_app():
    rule("STEP 1/3  Building LogicShare (PyInstaller, --onedir)")
    rc = subprocess.run([sys.executable, str(ROOT / "package.py")], cwd=ROOT).returncode
    if rc != 0:
        raise SystemExit(f"package.py failed (exit {rc})")
    exe = DIST_APP / "LogicShare.exe"
    if not exe.exists():
        raise SystemExit(f"Build reported success but {exe} is missing.")
    print(f"\n  -> {DIST_APP}")


# ---------------------------------------------------------------------------
# 2. Zip it into the installer folder
# ---------------------------------------------------------------------------

def zip_app():
    rule("STEP 2/3  Zipping the app into the installer payload")
    if not DIST_APP.exists():
        raise SystemExit(f"{DIST_APP} does not exist — run without --skip-app.")

    INSTALLER_DIR.mkdir(parents=True, exist_ok=True)
    if ZIP_PATH.exists():
        ZIP_PATH.unlink()

    files = [p for p in DIST_APP.rglob("*") if p.is_file()]
    total = len(files)
    # ZIP_DEFLATED, not ZIP_STORED: the payload is embedded in the EXE as a
    # resource, so every byte saved here is a byte off the download.
    with zipfile.ZipFile(ZIP_PATH, "w", zipfile.ZIP_DEFLATED,
                         compresslevel=6) as zf:
        for i, path in enumerate(files, 1):
            # arcname relative to dist/LogicShare, so LogicShare.exe lands at
            # the archive root. See the module docstring.
            zf.write(path, path.relative_to(DIST_APP).as_posix())
            if i % 200 == 0 or i == total:
                print(f"  {i}/{total} files...")

    size_mb = ZIP_PATH.stat().st_size / 1024 / 1024
    print(f"\n  -> {ZIP_PATH}  ({size_mb:.1f} MB, {total} files)")

    with zipfile.ZipFile(ZIP_PATH) as zf:
        names = zf.namelist()
    if "LogicShare.exe" not in names:
        raise SystemExit(
            "LogicShare.exe is not at the root of the ZIP — the installer "
            "would refuse this payload.")
    print("  verified: LogicShare.exe is at the archive root")


# ---------------------------------------------------------------------------
# 3. Build the installer
# ---------------------------------------------------------------------------

def build_installer():
    rule("STEP 3/3  Building LogicShareInstaller.exe (MSVC)")
    if not VCVARS.exists():
        raise SystemExit(
            f"Can't find vcvars64.bat at {VCVARS}.\n"
            "Install the Visual C++ Build Tools, or edit VCVARS in this file.")
    if not ZIP_PATH.exists():
        raise SystemExit(f"{ZIP_PATH} is missing — run step 2 first.")
    if not (INSTALLER_DIR / "logo.ico").exists():
        raise SystemExit(f"{INSTALLER_DIR / 'logo.ico'} is missing.")

    if INSTALLER_EXE.exists():
        INSTALLER_EXE.unlink()

    # Invoke the wrapper by absolute path rather than composing a
    # `cmd /c "call "..." && ..."` string. Nested quoting like that is
    # unsurvivable: Python quotes the whole argument, cmd then re-parses the
    # inner quotes around the vcvars path and loses the spaces in "Program
    # Files". A .bat with the quoting baked in has exactly one level of
    # parsing to get wrong, and it is already correct.
    with ascii_path_to(INSTALLER_DIR) as build_dir:
        wrapper = Path(build_dir) / "_build_with_vcvars.bat"
        rc = subprocess.run([str(wrapper)], cwd=str(build_dir)).returncode

    if rc != 0 or not INSTALLER_EXE.exists():
        raise SystemExit(f"Installer build failed (exit {rc}).")

    size_mb = INSTALLER_EXE.stat().st_size / 1024 / 1024
    print(f"\n  -> {INSTALLER_EXE}  ({size_mb:.1f} MB)")


def _ascii_temp_base():
    """A directory with no non-ASCII characters in its path.

    The junction exists to hand a toolchain an ASCII path. Building it
    under ``tempfile.gettempdir()`` assumes TEMP is itself ASCII, and on
    Windows it often is not: TEMP lives inside the user's profile, so an
    account name with an accent in it puts one straight into the path —
    C:/Users/Jose/AppData/Local/Temp is fine, the same name spelled with
    an acute is not. The junction is then no more ASCII than the path it
    was standing in for, and the workaround silently delivers nothing:
    it looks like it worked and the build fails exactly as it would
    have without it.

    So the candidates are tried in order and the first ASCII one that
    can actually be written to wins.
    """
    import os
    import tempfile
    candidates = [tempfile.gettempdir()]
    drive = os.environ.get("SystemDrive", "C:")
    candidates.append(os.path.join(drive + os.sep, "Temp"))
    candidates.append(os.path.join(drive + os.sep, "lcx-build"))
    for base in candidates:
        try:
            if not str(base).isascii():
                continue
            os.makedirs(base, exist_ok=True)
            probe = os.path.join(base, ".lcx-write-probe")
            with open(probe, "w") as fh:
                fh.write("")
            os.remove(probe)
            return base
        except OSError:
            continue
    # Nothing ASCII is writable. Returning TEMP keeps the old behaviour —
    # which may still work, since only some toolchains care — rather than
    # refusing to build at all.
    return tempfile.gettempdir()


class ascii_path_to:
    """Context manager yielding an emoji-free path to ``target``.

    Creates a directory junction under the system temp dir and removes it on
    exit. A junction, not a copy: the payload ZIP is hundreds of megabytes and
    the build writes its output back into the real folder.
    """

    def __init__(self, target):
        self.target = Path(target)
        self.link = None

    def __enter__(self):
        if self.target.as_posix().isascii():
            return self.target
        self.link = Path(_ascii_temp_base()) / f"_lsbuild_{os.getpid()}"
        if self.link.exists():
            self._unlink()
        subprocess.run(
            ["cmd", "/c", "mklink", "/J", str(self.link), str(self.target)],
            check=True, stdout=subprocess.DEVNULL)
        return self.link

    def __exit__(self, *exc):
        self._unlink()
        return False

    def _unlink(self):
        if not self.link:
            return
        try:
            # rmdir removes the junction itself, never what it points at.
            os.rmdir(self.link)
        except OSError:
            subprocess.run(["cmd", "/c", "rmdir", str(self.link)],
                           stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)


# ---------------------------------------------------------------------------

def main():
    ap = argparse.ArgumentParser(description="Build the LogicShare installer.")
    ap.add_argument("--skip-app", action="store_true",
                    help="reuse the existing dist/ build")
    ap.add_argument("--skip-installer", action="store_true",
                    help="stop after producing the ZIP payload")
    args = ap.parse_args()

    if not args.skip_app:
        build_app()
    else:
        rule("STEP 1/3  SKIPPED (--skip-app) — reusing dist/LogicShare")

    zip_app()

    if args.skip_installer:
        rule("STEP 3/3  SKIPPED (--skip-installer)")
    else:
        build_installer()

    rule("DONE")
    print(f"Installer: {INSTALLER_EXE}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
