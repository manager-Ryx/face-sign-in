"""Generate the app's small sound effects (synthesised here, so they're licence-free).

    python -m tools.make_sounds        # writes presence_monitor/ui/assets/sounds/*.wav

* ``shutter.wav``: a soft two-part camera click, played for each photo in face setup.
* ``done.wav``: a gentle rising two-note chime when every photo has been taken.

Both are quiet, short, 16-bit mono WAV files that ``winsound`` can play.
"""
from __future__ import annotations

import wave
from pathlib import Path

import numpy as np

RATE = 22050
OUT = Path(__file__).resolve().parent.parent / "presence_monitor" / "ui" / "assets" / "sounds"


def _envelope(n: int, attack: int, decay: float) -> np.ndarray:
    env = np.exp(-np.arange(n) / (decay * RATE))
    env[:attack] *= np.linspace(0.0, 1.0, attack)
    return env


def _lowpass(signal: np.ndarray, amount: float) -> np.ndarray:
    out = np.empty_like(signal)
    last = 0.0
    for index, value in enumerate(signal):
        last += amount * (value - last)
        out[index] = last
    return out


def shutter() -> np.ndarray:
    rng = np.random.default_rng(7)   # fixed: the same file every time
    total = np.zeros(int(0.12 * RATE))
    for start_s, level, decay in ((0.0, 1.0, 0.006), (0.045, 0.7, 0.009)):
        n = int(0.05 * RATE)
        click = _lowpass(rng.standard_normal(n), 0.35) * _envelope(n, 20, decay) * level
        thump = np.sin(2 * np.pi * 900 * np.arange(n) / RATE) * _envelope(n, 10, 0.004) * 0.4
        start = int(start_s * RATE)
        total[start:start + n] += click + thump
    return total


def done() -> np.ndarray:
    total = np.zeros(int(0.6 * RATE))
    for start_s, freq in ((0.0, 1046.5), (0.11, 1568.0)):   # C6, then G6
        n = int(0.45 * RATE)
        t = np.arange(n) / RATE
        tone = (np.sin(2 * np.pi * freq * t) + 0.25 * np.sin(2 * np.pi * 2 * freq * t)) \
            * _envelope(n, 60, 0.12)
        start = int(start_s * RATE)
        total[start:start + n] += tone
    return total


def write(path: Path, samples: np.ndarray, peak: float) -> None:
    samples = samples / max(1e-9, float(np.abs(samples).max())) * peak
    data = (samples * 32767).astype("<i2").tobytes()
    with wave.open(str(path), "wb") as out:
        out.setnchannels(1)
        out.setsampwidth(2)
        out.setframerate(RATE)
        out.writeframes(data)


def main() -> None:
    OUT.mkdir(parents=True, exist_ok=True)
    write(OUT / "shutter.wav", shutter(), 0.30)
    write(OUT / "done.wav", done(), 0.22)
    for path in sorted(OUT.glob("*.wav")):
        print(path, path.stat().st_size, "bytes")


if __name__ == "__main__":
    main()
