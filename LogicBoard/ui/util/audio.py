"""
audio.py — the UI click, played by Windows itself.

Usage:
    from ui.util.audio import click_sound
    click_sound.preload()      # once, at startup
    click_sound.play_click()

Why not QSoundEffect
--------------------
It was the only thing in LogicBoard that used QtMultimedia, and QtMultimedia
brings its FFmpeg backend with it: avcodec, avformat, avutil, swscale and
swresample, plus the plugin and Qt6Multimedia itself — about 20 MB of the
installer, to play one 20 KB WAV file.

Windows has played WAV files since 3.1: ``PlaySound`` in winmm.dll, which is
on every Windows install. The click is read into memory once and handed to
``PlaySound`` from there (SND_MEMORY | SND_ASYNC), so a tap costs no disk
read and never blocks the UI thread. A new click cuts the previous one off,
which is what a click should do.

The buffer is held for the life of the process. Python's ``winsound``
refuses SND_MEMORY with SND_ASYNC for exactly that reason — Windows reads
the buffer while the sound plays — so the call goes through ctypes with the
lifetime handled here.

Anywhere other than Windows this is silent, which is the right failure for
a feedback sound.
"""

from __future__ import annotations

import logging
import sys

from .paths import sounds_dir

__all__ = ["AudioManager", "click_sound"]

_SND_ASYNC = 0x0001
_SND_NODEFAULT = 0x0002
_SND_MEMORY = 0x0004


class AudioManager:
    """The app's feedback sounds. One today: the click."""

    def __init__(self) -> None:
        self._buf = None              # ctypes buffer holding the WAV bytes
        self._play = None             # winmm.PlaySoundW
        self._ready = False

    def preload(self) -> None:
        """Read the click into memory. Call once, after startup."""
        if sys.platform != "win32":
            return
        path = sounds_dir() / "click.wav"
        try:
            data = path.read_bytes()
        except OSError:
            logging.warning("Click sound not found at %s", path)
            return
        try:
            import ctypes
            from ctypes import wintypes
            play = ctypes.WinDLL("winmm").PlaySoundW
            play.argtypes = [ctypes.c_void_p, wintypes.HMODULE, wintypes.DWORD]
            play.restype = wintypes.BOOL
            self._buf = ctypes.create_string_buffer(data, len(data))
            self._play = play
            self._ready = True
            logging.info("Click sound ready (%d bytes, winmm).", len(data))
        except Exception as e:                       # noqa: BLE001
            logging.error("Failed to preload click sound: %s", e)
            self._buf = self._play = None

    def play_click(self) -> None:
        """Play the click without blocking; a new one cuts off the last."""
        if self._play is None:
            return
        try:
            import ctypes
            self._play(ctypes.addressof(self._buf), None,
                       _SND_MEMORY | _SND_ASYNC | _SND_NODEFAULT)
        except Exception as e:                       # noqa: BLE001
            logging.debug("Click sound play error: %s", e)

    @property
    def ready(self) -> bool:
        return self._ready


# Module-level singleton.  Call click_sound.preload() during app startup.
click_sound = AudioManager()
