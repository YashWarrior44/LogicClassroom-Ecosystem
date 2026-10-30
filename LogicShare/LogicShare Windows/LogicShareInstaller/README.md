# LogicShare Installer

Single-file Windows installer for LogicShare 😎🔥. Admin elevation, ZIP payload
baked into the EXE, full Apps & Features registration, a working uninstaller,
and an in-app updater that downloads and runs this same installer.

Derived from `LogicBoard/LogicBoardInstaller` — see **Keeping the two in sync**
at the bottom.

---

## What's in this folder

```
LogicShareInstaller/
├── LogicShareInstaller.cpp   ← installer source (GENERATED — see below)
├── resources.rc              ← embeds LogicShare.zip + logo.ico into the EXE
├── manifest.xml              ← admin elevation + DPI awareness declaration
├── miniz.h / miniz.c         ← ZIP decompression (from richgel999/miniz)
├── build.bat                 ← compile + link
├── _build_with_vcvars.bat    ← build.bat wrapped in the MSVC environment
├── logo.ico                  ← the LogicShare icon
├── LogicShare.zip            ← payload, produced by package_installer.py
└── README.md                 ← this file
```

---

## Build

From the parent folder, one command does everything:

```
python package_installer.py
```

That runs `package.py` (PyInstaller `--onedir`) → zips `dist/LogicShare/` into
`LogicShareInstaller/LogicShare.zip` → links `LogicShareInstaller.exe`.

Add `--skip-app` to reuse the existing `dist/` build when you have only touched
the installer's C++, or `--skip-installer` to stop after producing the ZIP.

Building the installer alone, if the ZIP is already in place, is just
`_build_with_vcvars.bat`.

> **Why the wrapper scripts?** The project lives under a path containing an
> emoji (`Source Code📜`). `cmd.exe` resolves relative command names against an
> OEM-codepage copy of the current directory, and that conversion fails on the
> emoji — so `call build.bat` reports "not recognized" while `dir` can see the
> file perfectly well. `package_installer.py` sidesteps it with a temporary
> ASCII directory junction; `_build_with_vcvars.bat` does it by setting
> codepage 65001 and calling everything by absolute path.

---

## Install vs. update — one binary, two labels

Same scheme as the LogicBoard installer. The binary detects an existing
LogicShare **before painting its first frame** and relabels itself:

|                | Fresh machine                | LogicShare already installed        |
| -------------- | ---------------------------- | ----------------------------------- |
| Heading        | `Welcome to LogicShare`      | `Update LogicShare`                 |
| Sub-heading    | `Free, open-source file sharing…`   | `v1.0.0  →  v1.1.0`                 |
| Button         | `Install LogicShare`         | `Update LogicShare`                 |
| Progress title | `Installing LogicShare…`     | `Updating LogicShare…`              |
| Done screen    | `All done`                   | `Updated`                           |

Detection reads the HKLM `Uninstall\LogicShare` key **and** cross-checks for a
real `LogicShare.exe` under `%ProgramFiles%\LogicShare` (at the root, and one
level down in the versioned folder). Either signal counts — registry keys
outlive a hand-deleted folder, and a folder outlives a hand-cleaned registry.
Installing the version that's already there says `Reinstalling v1.0.0`, not
"upgrade".

The install pipeline is identical either way. Only the wording changes.

---

## What install actually does

```
C:\Program Files\LogicShare\           <- install root
    Uninstaller.exe                    <- survives every update
    logo.ico
    LogicShare_V1.0.0\                 <- this build
        LogicShare.exe
        _internal\
```

1. **Terminates any running `LogicShare.exe`.** Not optional: a live copy holds
   its own exe and every DLL under `_internal` open, and Windows will not let
   anyone overwrite a mapped image. Without this the extract fails partway and
   the whole install rolls back.
2. Wipes the install root except `Uninstaller.exe`, then extracts the payload
   into the versioned folder.
3. Verifies `LogicShare.exe` actually landed where every shortcut is about to
   point — and if it didn't, cleans up rather than leaving a broken install.
4. Drops `logo.ico` at the root and self-copies as `Uninstaller.exe`, stripping
   the ~67 MB ZIP resource from that copy so it doesn't double the footprint.
5. Creates exactly two shortcuts, both all-users, both tagged with the
   AppUserModelID `InnovaLogiX.LogicShare`:
   - `%PUBLIC%\Desktop\LogicShare.lnk`
   - `%ProgramData%\…\Start Menu\Programs\LogicShare\LogicShare.lnk`
6. Writes the HKLM `Uninstall\LogicShare` key (DisplayName, DisplayVersion,
   Publisher, InstallLocation, EstimatedSize, UninstallString) and an
   `App Paths\LogicShare.exe` key so Windows Search finds it.

### Deliberately different from LogicBoard: no startup entry

LogicBoard registers itself under `HKCU\…\Run` with `--background`, because a
classroom board has to be listening for incoming files from the moment it
boots. LogicShare is the *sending* half of that conversation: it runs on a
teacher's own laptop and only does anything while someone is actively sending
something. An app with nothing to wait for has no business launching itself at
every login, so there is no Run value here.

The **uninstaller still deletes** that value — cleaning up after any build that
did set it costs one API call and means nobody is left with an orphaned startup
entry.

---

## How this fits the in-app updater

LogicShare updates by **downloading and running this installer**, not by
unpacking files over itself.

```
LogicShare  ──▶  api.github.com/repos/YashWarrior44/LogicClassroom-Ecosystem/releases  (newest LogicShare-v* tag)
            ──▶  picks the .exe asset  ──▶  %LOCALAPPDATA%\LogicShare\Updates\
            ──▶  ShellExecute "runas"  ──▶  LogicShareInstaller.exe takes over
```

A self-extracting updater would have to reimplement — correctly — everything
this installer already does: close the running copy, lay files into a versioned
folder, repoint the shortcuts, rewrite the Apps & Features entry so the version
shown there isn't a lie, update App Paths, delete the old version. Each is a
separate chance for an updated install to end up subtly wrong. Handing the job
back to the installer means the update path and the install path are the *same*
code path.

### Release checklist

Three version numbers must agree, or the updater misbehaves — an app that
under-reports offers an "update" to the build already installed; one that
over-reports never sees a real one:

| Where                                | What                                  |
| ------------------------------------ | ------------------------------------- |
| `LogicShare.py`                      | `APP_VERSION = "1.0.0"`               |
| `LogicShareInstaller/resources.rc`   | `FILEVERSION` / `PRODUCTVERSION` / the two version strings |
| `LogicShareInstaller.cpp`            | `APP_VERSION_TEXT` (set by the derive script) |
| The GitHub release                   | tag `v1.0.0`, with the built `LogicShareInstaller.exe` attached as an asset |

The tag parser is tolerant — `v1.2.3`, `V1.2.3`, `1.2.3`, `v1.2.3-beta.1`,
`LogicShare v1.2.3` and `v1.2` all work — but the *asset* must be a `.exe`.
A release with only a `.zip` attached reports "no installer attached yet" and
offers nothing, by design: the updater runs what it downloads.

---

## Uninstall

Apps & Features runs `"C:\Program Files\LogicShare\Uninstaller.exe" /uninstall`,
which shows the same styled window and then: stops LogicShare, removes both
shortcuts and the Start Menu folder, deletes the Uninstall / App Paths / Run
registry entries from both hives, wipes the install directory, and spawns a
hidden `cmd` that deletes `Uninstaller.exe` and the now-empty folder a couple of
seconds later.

**The user's data is left alone.** The profile, announcement history and log
live in `%LOCALAPPDATA%\LogicShare`, not in Program Files, so uninstalling —
and, more importantly, *updating* — never touches them.

---

## Keeping the two in sync

`LogicShareInstaller.cpp` is **generated** from
`LogicBoard/LogicBoardInstaller/LogicBoardInstaller.cpp`. The two installers are
one program with two product identities, so a fix to the install pipeline, the
update detection or the uninstaller belongs in the LogicBoard source and gets
re-derived here — rather than being applied twice and drifting the second time.

The derive script does the mechanical identity rename (names, GUID-ish tokens,
resource ids, registry paths) and applies the handful of edits that are *not*
mechanical: the Welcome-screen copy, the feature list, and dropping the startup
registration described above.

If you edit `LogicShareInstaller.cpp` by hand, expect the next derive to
overwrite it. Put the change in the LogicBoard source, or in the derive script's
edit list.
