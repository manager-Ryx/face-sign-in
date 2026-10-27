"""Small sound effects: a soft shutter click per photo and a chime when setup is done.

Qt's multimedia module isn't part of the slim PySide6 we ship, so this plays
the bundled WAV files (``assets/sounds``, made by ``tools.make_sounds``) with
Windows' own ``winsound``, asynchronously, so it never holds up the window.
The "Play a sound for each photo" setting (``capture_sounds``) mutes them.
"""
from __future__ import annotations

import logging
import os
from pathlib import Path

log = logging.getLogger(__name__)

SOUNDS_DIR = Path(__file__).resolve().parent / "assets" / "sounds"
SHUTTER = "shutter"
DONE = "done"

enabled = os.environ.get("FACE_UNLOCK_STILL") != "1"   # silent in tests and renders


def sound_file(name: str, folder: Path = SOUNDS_DIR) -> Path | None:
    path = folder / f"{name}.wav"
    return path if path.is_file() else None


def play(name: str) -> bool:
    """Play a bundled sound without waiting; False if it couldn't (never raises)."""
    path = sound_file(name)
    if not enabled or path is None:
        return False
    try:
        import winsound
        winsound.PlaySound(str(path), winsound.SND_FILENAME | winsound.SND_ASYNC
                           | winsound.SND_NODEFAULT)
        return True
    except (ImportError, RuntimeError, OSError):
        log.debug("sound %s not played", name, exc_info=True)
        return False
