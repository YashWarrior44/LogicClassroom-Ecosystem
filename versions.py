"""
versions.py — the ONE place a version number is written by hand.

Every other version literal in this ecosystem is derived from the values
below by ``sync_versions.py``. Bump a number here, run that, done.

WHY THIS FILE EXISTS
====================
A version was written out by hand in eight places across four languages:

    LogicBoard/ui/util/version.py                     APP_VERSION
    LogicBoard/LogicBoardInstaller/*.cpp              APP_VERSION_TEXT
    LogicBoard/LogicBoardInstaller/resources.rc       FILEVERSION + strings
    LogicShare Windows/LogicShare.py                  APP_VERSION
    LogicShare Windows/LogicShareInstaller/*.cpp      APP_VERSION_TEXT
    LogicShare Windows/LogicShareInstaller/*.rc       FILEVERSION + strings
    LogicShare Mobile/pubspec.yaml                    version
    LogicShare Mobile/lib/main.dart                   kAppVersion

They drifted, which is what you would expect and what actually happened:
the LogicShare installer's derive script was still writing 1.0.0 into a
tree whose app said 1.1.0. The resources.rc files even carry a comment
telling the reader to keep them in step by hand.

WHY NOT JUST IMPORT THIS FILE EVERYWHERE
========================================
Because the shipped products cannot see it. LogicBoard is frozen by
PyInstaller from LogicBoard/ alone, the installers are C++ compiled from
their own folder, and the Android app is an APK built from
LogicShare Mobile/. Each one has to carry a literal of its own.

So the literal stays, and stops being hand-maintained: ``sync_versions.py``
writes it, and ``sync_versions.py --check`` fails if any copy has drifted.
That is the part that makes this a single source rather than a ninth place
to forget.

THE TWO PRODUCTS VERSION SEPARATELY
===================================
LogicBoard and LogicShare are different products on different release
cadences, so they get a number each. LogicShare Windows and LogicShare
Android are ONE product on two platforms and always ship together, so they
share theirs — a teacher pairing a phone to a board should not have to
reason about two different LogicShare versions.
"""

# ── the numbers ──────────────────────────────────────────────────────────
LOGICBOARD = "3.1.3"

# Windows and Android both. See the note above.
LOGICSHARE = "1.1.2"

# Android's versionCode: an integer that must INCREASE on every build the
# Play Store or a device sees, independently of the human-facing version.
# Downgrading it makes Android refuse the install with a message that says
# nothing useful, so it only ever goes up.
# 4: the Kotlin rewrite, replacing the Flutter build (3). It installs over
# 3 in place because it keeps the package name and signing key.
# 5: 1.1.2 — update consent (download, then ask before installing).
LOGICSHARE_ANDROID_BUILD = 5


__all__ = ["LOGICBOARD", "LOGICSHARE", "LOGICSHARE_ANDROID_BUILD"]
