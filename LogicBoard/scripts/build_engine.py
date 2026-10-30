#!/usr/bin/env python3
"""
build_engine.py — CMake configure + build helper for the C++ engine.

Usage:
    python scripts/build_engine.py           # Release build
    python scripts/build_engine.py --debug   # Debug build
    python scripts/build_engine.py --clean   # Clean rebuild
"""

import argparse
import shutil
import subprocess
import sys
from pathlib import Path


def main() -> int:
    parser = argparse.ArgumentParser(description="Build the LogicBoard C++ engine")
    parser.add_argument("--debug", action="store_true", help="Build in Debug mode")
    parser.add_argument("--clean", action="store_true", help="Clean build directory first")
    parser.add_argument("--jobs", "-j", type=int, default=0, help="Parallel build jobs (0=auto)")
    args = parser.parse_args()

    project_root = Path(__file__).resolve().parent.parent
    build_dir = project_root / "build"
    build_type = "Debug" if args.debug else "Release"

    if args.clean and build_dir.exists():
        print(f"Cleaning {build_dir}...")
        shutil.rmtree(build_dir)

    build_dir.mkdir(exist_ok=True)

    # Configure
    print(f"Configuring ({build_type})...")
    configure_cmd = [
        "cmake",
        "-S", str(project_root),
        "-B", str(build_dir),
        f"-DCMAKE_BUILD_TYPE={build_type}",
    ]

    result = subprocess.run(configure_cmd, cwd=project_root)
    if result.returncode != 0:
        print("CMake configure failed!")
        return result.returncode

    # Build
    print("Building...")
    build_cmd = ["cmake", "--build", str(build_dir), "--config", build_type]
    if args.jobs > 0:
        build_cmd += ["--parallel", str(args.jobs)]
    else:
        build_cmd += ["--parallel"]

    result = subprocess.run(build_cmd, cwd=project_root)
    if result.returncode != 0:
        print("Build failed!")
        return result.returncode

    # Find the built module
    for ext in (".so", ".pyd", ".dylib"):
        for f in build_dir.rglob(f"logicboard_engine*{ext}"):
            print(f"\nBuilt module: {f}")
            target = project_root / f.name
            shutil.copy2(f, target)
            print(f"Copied to: {target}")
            break

    print("\nBuild complete!")
    return 0


if __name__ == "__main__":
    sys.exit(main())
