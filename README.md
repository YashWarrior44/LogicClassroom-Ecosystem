# LogicClassroom Ecosystem

Free, open-source software for the classroom smart board — a whiteboard the
teacher writes on, and a way to send it files and announcements from a
laptop or a phone, with no cables and no USB sticks.

| App | Platform | What it does |
|---|---|---|
| **LogicBoard** | Windows (smart board) | A touch-first whiteboard. It also receives files and announcements over the classroom network. |
| **LogicShare** | Windows (teacher's PC) | Sends files and announcements straight to the boards on the same network, and shows which classrooms acknowledged them. |
| **LogicShare** | Android | The same, from a phone. |

All three work entirely on the local network. Nothing goes through a server.

## What LogicBoard does

- **Writing tools** — pen, highlighter, a chisel-nib calligraphy brush, and
  a dual-tip pen whose back end can be a highlighter, brush, eraser or the
  selection tool. A shared palette of distinct shades plus a custom colour.
- **Erasers** — Simple (rubs out exactly where it touches), Object (removes
  whole strokes) and Lasso (circle what should go; one undo brings it back).
- **Shapes** — 2D (straight, dashed and dotted lines, arrows, rectangles,
  circles, ellipses, triangles, polygons…) and 3D figures for maths lessons
  (cubes, cylinders, cones, spheres, prisms, pyramids…), with their own
  colours.
- **Touch first** — palm rejection with a calibrated palm size, a palm
  eraser, two-finger pan and zoom, and stylus calibration for both tips.
- **Pages** — multi-page boards with a pages panel: drag a page to reorder
  it, insert one between any two, delete with one tap.
- **Media** — import PDFs, images and PowerPoint decks (slides step through
  their click animations); set any page or a whole deck as the background.
- **Boards** are single `.lb` files that open by double-click.
- **Updates** itself from this repository's releases, asking first.

## Download

Installers are on the [Releases](../../releases) page:

- **LogicBoard** — `LogicBoard-v*` releases → `LogicBoard … Installer.exe`
- **LogicShare** — `LogicShare-v*` releases → the Windows installer and the Android `.apk`

The apps update themselves from those same releases, asking before they
download and again before they install.

## Privacy

There are no accounts, no analytics and no telemetry. The only address any
of the apps contacts outside the classroom network is GitHub's API, to look
for a newer release. Everything else — finding boards, sending files and
announcements, acknowledgements — stays on the local network.

## How it fits together

```
 LogicShare (PC / phone)                         LogicBoard (smart board)
 ───────────────────────                         ────────────────────────
  finds boards via mDNS  ◄── _logicboard._tcp ──  advertises itself (zeroconf)
  sends files, notices   ───── HTTP :16384 ────►  receives, shows a toast
  shows who has seen it  ◄──── HTTP :16385* ────  acknowledges
```

\* the default; every announcement carries the port its sender is listening on.

LogicBoard is a PySide6 (Qt) app on top of a C++ drawing engine. The engine
holds the scene — objects, pages, undo/redo commands, hit-testing and
serialisation — and is exposed to Python with pybind11 as the
`logicboard_engine` module. Everything you see is the Python UI.

Both Windows apps need inbound access through Windows Firewall on the
classroom network; LogicShare warns when it does not have it and can ask
Windows for it again.

## Repository layout

```
LogicBoard/
  engine/            C++ engine: scene objects, commands (undo/redo), maths
  bindings/          pybind11 bindings → logicboard_engine
  ui/                the PySide6 app (canvas, toolbar, panels, LogicShare service)
  assets/            icons and sounds shipped with the app
  branding/          the logo master
  scripts/           build the engine, package the app, build the installer
  LogicBoardInstaller/   the native installer (C++, miniz)
  tests/python/      test suites, one file per area
LogicShare/
  LogicShare Windows/    the Windows app, its installer and tests
  LogicShare Mobile/     the Android app (Kotlin, Gradle)
  shared/                test cases both LogicShare apps must agree on
versions.py          the one place version numbers are written
sync_versions.py     copies them into every app and build file
export_binaries.py   builds all three apps into Exported binaries/
qt_prune.py          strips unused Qt libraries from the packaged apps
```

## Building from source

Requirements:

- Windows 10/11 and **Python 3.11**
- **CMake ≥ 3.20**, a **C++17 compiler** (MinGW-w64 or MSVC) and **Git** —
  CMake downloads pybind11 and nlohmann/json while configuring the engine
- **Android Studio** for the Android app. To run `gradlew` from a terminal,
  point `JAVA_HOME` at a JDK 11 or newer (Android Studio's own is in
  `…\Android Studio\jbr`)
- Microsoft **PowerPoint** on the board, at run time, to import `.ppt`/`.pptx`
  (LogicBoard renders slides through it; without it, it says so rather than
  failing)

```bash
pip install -r requirements.txt
```

**LogicBoard**

```bash
cd LogicBoard
python scripts/build_engine.py       # the C++ engine (pybind11 module)
python ui/app.py                     # run from source
python scripts/package_installer.py  # build the installer
```

**LogicShare (Windows)**

```bash
cd "LogicShare/LogicShare Windows"
python LogicShare.py                 # run from source
python package_installer.py          # build the installer
```

**LogicShare (Android)** — open `LogicShare/LogicShare Mobile` in Android
Studio, or run `gradlew assembleRelease` there.

**Everything at once** — `python export_binaries.py` builds all three and
collects them in `Exported binaries/`.

> A checkout path containing non-ASCII characters (emoji, accents) trips up
> MSVC and the Android Gradle plugin. Build from an ASCII path, or through a
> directory junction to one.

## Tests

Every test file is a plain script that runs on its own and prints
`all passed` — no test runner needed.

```bash
python LogicBoard/tests/python/test_pen_panel_layout.py
python "LogicShare/LogicShare Windows/test_release_select.py"
python test_versions.py
```

To run a whole app's suite:

```bash
for f in LogicBoard/tests/python/test_*.py; do python "$f" || echo "FAILED: $f"; done
for f in "LogicShare/LogicShare Windows"/test_*.py; do python "$f" || echo "FAILED: $f"; done
```

Android unit tests: `gradlew testDebugUnitTest` in `LogicShare/LogicShare Mobile`.

## Versions

`versions.py` is the only place a version number is written by hand.
`python sync_versions.py` copies it into every app, installer and build
file; `python sync_versions.py --check` verifies nothing has drifted.

## Publishing a release

The three apps share this repository's Releases, and each updater finds its
own by tag, so tags must follow this pattern:

| Tag | Assets |
|---|---|
| `LogicBoard-v3.1.3` | the LogicBoard installer (`.exe`) |
| `LogicShare-v1.1.2` | the LogicShare Windows installer (`.exe`) **and** the Android `.apk` |

Mark a release as a pre-release to keep it from being offered to anyone.

## Contributing

Issues and pull requests are welcome.

- Run the tests for whatever you touched; add one for a bug you fix.
- Change versions only in `versions.py`, then run `python sync_versions.py`.
- Keep personal data out of the repository — boards, LogicShare profiles,
  logs and build output are already in `.gitignore`.
- See [CHANGELOG.md](CHANGELOG.md) for what changed between releases.

## Logo

The LogicBoard logo was created for the project with ChatGPT.
`LogicBoard/branding/logo-original.png` is the image as generated,
`LogicBoard/branding/logo.png` the cleaned-up 1024 px master, and the app's
icon (`assets/icons/logo.ico`, every size Windows uses) is generated from it.

## Licence

Copyright © 2026 Yash Jain

This program is free software: you can redistribute it and/or modify it
under the terms of the GNU General Public License as published by the Free
Software Foundation, either version 3 of the License, or (at your option)
any later version. See [LICENSE](LICENSE).
