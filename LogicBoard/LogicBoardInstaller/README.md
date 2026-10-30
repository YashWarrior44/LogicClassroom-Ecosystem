# LogicBoard Installer

Single-file Windows installer for LogicBoard 😎🔥. Modern Aero-glass UI, admin
elevation, ZIP payload baked into the EXE, full Apps & Features registration,
and a working uninstaller.

---

## What's in this folder

```
LogicBoardInstaller/
├── LogicBoardInstaller.cpp   ← main installer source (all UI + install logic)
├── resources.rc              ← embeds LogicBoard.zip + logo.ico into the EXE
├── manifest.xml              ← admin elevation + DPI awareness declaration
├── miniz.h                   ← ZIP decompression header (fetch — see below)
├── miniz.c                   ← ZIP decompression impl    (fetch — see below)
├── build.bat                 ← one-shot build script
├── LogicBoard.zip            ← YOU DROP THIS IN before building
├── logo.ico                  ← YOU DROP THIS IN before building
└── README.md                 ← this file
```

---

## Before you build — grab miniz

`miniz` is a permissively-licensed single-file ZIP library that lives at:

    https://github.com/richgel999/miniz

Grab the two files:

    miniz.h
    miniz.c

from that repo's latest release and drop them next to `LogicBoardInstaller.cpp`.
That's the only third-party dependency the installer has — everything else is
pure Windows API.

---

## Build

1. Open a **x64 Native Tools Command Prompt for VS 2022** (or later — 2019 works
   too). Comes with Visual Studio OR with the free
   [Visual C++ Build Tools](https://visualstudio.microsoft.com/visual-cpp-build-tools/).
   Pick "Desktop development with C++" during install.

2. Put `LogicBoard.zip` and `logo.ico` in this folder.

3. Run:

   ```
   build.bat
   ```

   Or skip steps 1–2 entirely and run the whole pipeline — build the app, zip
   it into this folder, then link the installer — with:

   ```
   python scripts/package_installer.py
   ```

   Add `--skip-app` to reuse the existing `dist/` build when you have only
   touched the installer's C++.

   The script compiles the resource file, `miniz.c`, and `LogicBoardInstaller.cpp`,
   then links everything with the required Win32 libraries and embeds `manifest.xml`
   so Windows knows to prompt for elevation on launch.

4. Ship `LogicBoardInstaller.exe`. That's it — a single self-contained file.

---

## Install vs. update — one binary, two labels

The installer detects an existing LogicBoard **before it paints its first
frame** and relabels itself accordingly. There is no separate updater
download: `LogicBoardInstaller.exe` is both.

|                | Fresh machine                | LogicBoard already installed        |
| -------------- | ---------------------------- | ----------------------------------- |
| Heading        | `Welcome to LogicBoard`      | `Update LogicBoard`                 |
| Sub-heading    | `A free, open-source whiteboard…`   | `v3.0.0  →  v3.1.0`                 |
| Button         | `Install LogicBoard`         | `Update LogicBoard`                 |
| Progress title | `Installing LogicBoard…`     | `Updating LogicBoard…`              |
| Done screen    | `All done`                   | `Updated`                           |
| Window title   | `LogicBoard Installer`       | `LogicBoard Updater`                |

Detection (`DetectExistingInstall`) is deliberately two-tiered, because either
signal alone is wrong in exactly the situations where someone is most likely
to be reinstalling:

1. **The HKLM `Uninstall\LogicBoard` key** — the authoritative record, and the
   only place the previously-installed *version* is written down.
2. **A real `LogicBoard.exe` under `%ProgramFiles%\LogicBoard`** — checked at
   the root (older flat installs) and one level down (the versioned folder
   layout). Registry keys outlive a hand-deleted install folder, and a folder
   outlives a hand-cleaned registry.

Either one counts as "installed". If the registry has no version but a
`LogicBoard_V3.0.0` folder exists, the version is recovered from the folder
name. If the installed version equals this build's, the sub-heading reads
`Reinstalling v3.1.0` rather than claiming an upgrade the user can see it
isn't.

**The install pipeline itself is identical either way** — it already wiped the
old version folders, re-pointed every shortcut and rewrote the registry. Only
the wording changes, which is the point: there is one code path to get right,
not two.

---

## Install flow (what the user sees)

1. **UAC prompt** — Windows asks for admin because we write to Program Files
   and the HKLM Uninstall registry.
2. **Welcome screen** — LogicBoard logo, tagline, the GPL-3.0 licence line, and an
   `Install LogicBoard` / `Update LogicBoard` button (see the table above).
3. **Installing screen** — progress bar + live status text as the ZIP payload
   extracts into `C:\Program Files\LogicBoard\`.
4. **Done screen** — checkmark, "All done 🎉", `Close` and `Launch LogicBoard`
   buttons.

Behind the scenes, install ALSO:
- Copies the installer as `Uninstaller.exe` inside the install dir so
  Apps & Features can call it later with `/uninstall`.
- Drops `logo.ico` next to the exe (stable icon reference for shortcuts +
  Apps & Features).
- Creates exactly two shortcuts, both system-wide since we're elevated —
  **one per location, never duplicated**:
  - `%PUBLIC%\Desktop\LogicBoard.lnk`
  - `%ProgramData%\Microsoft\Windows\Start Menu\Programs\LogicBoard\LogicBoard.lnk`
  Both are tagged with the AppUserModelID `InnovaLogiX.LogicBoard` via
  `IPropertyStore`, so Windows groups the taskbar button correctly and any
  pinned icon survives future updates.
- Registers startup **once**, as an `HKCU\...\Run` value named `LogicBoard`
  pointing at `LogicBoard.exe --background`.
- Removes artifacts left by older installers (a per-user desktop copy and a
  Startup-folder shortcut) so upgrading clears the duplicates rather than
  inheriting them.

  > Both of those used to be created deliberately as "belt-and-suspenders".
  > They aren't fallbacks: Explorer merges the per-user and all-users
  > desktops into one view, and Windows enumerates both the Run key and the
  > Startup folder. So each redundant copy showed up as a visible duplicate —
  > two desktop icons, and LogicBoard listed twice in Task Manager → Startup
  > and Settings → Apps → Startup.
- Writes the `HKLM Uninstall\LogicBoard` key with `DisplayName`, `DisplayIcon`,
  `DisplayVersion`, `Publisher`, `InstallLocation`, `EstimatedSize`, and a
  proper `UninstallString`. Shows up cleanly in Apps & Features.
- Writes an `App Paths\LogicBoard.exe` HKLM key so Windows Search / Run
  dialog can find `LogicBoard` by name.

---

## Uninstall flow

Users hit **Uninstall** in Apps & Features → Windows runs
`"%USERPROFILE%\LogicBoard\Uninstaller.exe" /uninstall`.

**The uninstaller asks for administrator rights before it does anything**,
by re-launching itself through the `runas` verb (`ElevateForUninstall`).
Decline the prompt and nothing is removed — you get a one-line message
saying so, because clicking Uninstall in Settings and then seeing nothing
happen reads as a broken uninstaller.

Why it elevates when the install does not: an uninstall has more to remove
than the install put down. A machine that has been through the old Program
Files era still carries `HKLM` `Uninstall` / `App Paths` / `Run` values,
all-users shortcuts, and firewall rules naming the app. Every one of those
delete calls used to fail silently for want of rights — which is how a
machine ended up with LogicBoard still listed in Apps & Features after
being uninstalled.

It is a runtime prompt and not a manifest declaration because the
uninstaller is not a separate binary: it is a copy of the installer, so it
carries the installer's `asInvoker` manifest, and that has to stay
`asInvoker` (see `manifest.xml`). One consequence worth knowing: Apps &
Features shows no UAC shield next to Uninstall, because Windows reads that
from the manifest. The prompt still appears.

### Not necessarily the same user

Elevating means the uninstall may run as a *different account* than the one
that asked for it — a standard user starts it, an administrator types their
credentials. `HKCU` and `SHGetFolderPath` then answer for the
administrator, and cleaning up "the current user" would wipe the wrong
profile while leaving the real one listed forever.

So the unelevated launcher passes its own SID across (`--user-sid:`), and
`ResolveInvokingUser` uses it to reach the right hive under `HKEY_USERS`
and the right shortcut folders (`Explorer\Shell Folders`, which holds
already-expanded paths — `User Shell Folders` would expand `%USERPROFILE%`
against the *administrator's* profile). If that account's folders cannot be
read, the per-user cleanup is skipped rather than aimed at ours.

`test_uninstall_elevation.py` covers this. It extracts the helpers verbatim
from `LogicBoardInstaller.cpp` and compiles them into a console harness, so
it cannot drift from the code:

```
python test_uninstall_elevation.py
```

### What it removes

- Stops any running LogicBoard.
- Removes Desktop / Start Menu / Startup shortcuts, both all-users and the
  invoking user's.
- Removes the `Uninstall` + `App Paths` keys and the `Run` value, under
  both the user hive and `HKLM`.
- Drops the firewall rules naming the install.
- Deletes the installed files, then spawns a hidden `cmd.exe` that waits
  ~2 seconds, deletes `Uninstaller.exe`, and removes the install directory.

Saved boards and settings in `%LOCALAPPDATA%\LogicBoard` are deliberately
left alone.

## Design notes (things you asked about)

**Aero glass UI**  
The window is frameless (`WS_POPUP`) with `DwmSetWindowAttribute` set to
`DWMWA_SYSTEMBACKDROP_TYPE = DWMSBT_TRANSIENTWINDOW` — the acrylic backdrop
Windows 11 22H2+ ships natively. On older Windows 10 / early 11 the code
falls back to the undocumented `SetWindowCompositionAttribute` +
`ACCENT_ENABLE_ACRYLICBLURBEHIND`, which produces the same effect. Corners
are rounded via `DWMWA_WINDOW_CORNER_PREFERENCE = DWMWCP_ROUND`.

The window content is painted by hand with **GDI+** — rounded panels, hairline
strokes, ClearType-hinted text, filled/outlined button styles. No standard
Win32 controls (they'd break the Aero look).

**Single-file EXE**  
`LogicBoard.zip` and `logo.ico` are embedded via `resources.rc` and pulled
out at runtime via `FindResource` / `LockResource`. The ZIP is fed directly
into `mz_zip_reader_init_mem` — never touches the disk until each extracted
file is written.

**Error handling**  
Every install step is wrapped in checks. If the ZIP is missing, corrupt,
extraction fails, Program Files isn't writable, or anything else goes wrong,
the installer switches to a fourth screen (`Screen::Error`) with a friendly
explanation and a single Close button. No partial installs are left behind —
the first thing the pipeline does is clear the install directory except for a
prior `Uninstaller.exe` (which would be locked if it's the one currently
running).

**Compact + maintainable**  
One `.cpp` file, ~900 lines, no framework baggage. Add a screen or tweak a
paint routine and you know exactly where the change goes.

---

## Troubleshooting

**"rc: cannot open logo.ico"** — you forgot to put `logo.ico` in this folder.

**"LNK2019 unresolved external"** — you're missing one of the linker libs.
The build script lists all of them; if you're customizing, make sure
`propsys.lib` is in the link line (it's the one people forget — needed for the
`PROPERTYKEY` shortcut tagging).

**"error C2065: DWMWA_SYSTEMBACKDROP_TYPE"** — you're on an older Windows
SDK. The constants are `#define`d as fallbacks in the .cpp file so this
should compile anyway. If not, update the Windows SDK from the VS installer.

**"error MSB4062: The mt.exe task could not be loaded"** — you're using
MSBuild without the SDK path set. Use the "x64 Native Tools Command Prompt"
launcher instead of a plain cmd.
