"""Learned, bounded start-up estimates for the pinned camera.

The service records how long the camera takes to open and settle in each
lighting condition, how lighting varies by hour of day, and whether asking
for a lower frame rate actually brightens a dim face on this camera. These
are *starting estimates only*: every capture is still checked frame by frame.

Low-light mode is evidence-gated. It starts in a "trial" state; each switch
to the lower frame rate measures the face-brightness gain. After three
trials it is enabled only if the median gain is at least 25 %, otherwise it
is disabled with the measured reason. Users can reset it at any time.
"""
from __future__ import annotations

import json
import logging
import math
import statistics
import threading
import time
from collections.abc import Callable
from pathlib import Path
from typing import Any

from .storage import read_state, write_json_atomic

log = logging.getLogger(__name__)

LIGHTING = ("bright", "dim", "dark", "backlit")
RECENT_S = 20 * 60          # an observation this recent predicts current lighting
EWMA = 0.3
MIN_TRIALS = 3
MIN_GAIN = 1.25
MAX_TRIALS_KEPT = 12
SAVE_INTERVAL_S = 60.0


def _ewma(old: float | None, new: float) -> float:
    return new if old is None or not math.isfinite(old) else (1 - EWMA) * old + EWMA * new


class CameraProfile:
    """Persistent per-camera estimates (``state/camera_profile.json``)."""

    def __init__(self, path: Path, *, clock: Callable[[], float] = time.time):
        self.path = path
        self._clock = clock
        self._lock = threading.Lock()
        self._data: dict[str, Any] = self._load()
        self._dirty = False
        self._saved_at = -SAVE_INTERVAL_S

    # ---- persistence -------------------------------------------------------

    def _empty(self) -> dict[str, Any]:
        return {"version": 1, "buckets": {}, "hours": {}, "last": None,
                "low_light": {"state": "trial", "gains": [], "switch_ms": None,
                              "reason": "Not measured yet"}}

    def _load(self) -> dict[str, Any]:
        return read_state(self.path, self._empty(), what="camera profile")

    def _save(self) -> None:
        self._dirty = False
        self._saved_at = time.monotonic()
        try:
            write_json_atomic(self.path, self._data)
        except OSError as exc:
            log.warning("camera profile not saved: %s", exc)

    def _changed(self) -> None:
        """Save at most once a minute: quick looks on the lock screen open the
        camera every few seconds, and each start updates these estimates."""
        if time.monotonic() - self._saved_at >= SAVE_INTERVAL_S:
            self._save()
        elif not self._dirty:
            self._dirty = True
            timer = threading.Timer(SAVE_INTERVAL_S, self.flush)
            timer.daemon = True
            timer.start()

    def flush(self) -> None:
        """Write pending changes now (also called at shutdown)."""
        with self._lock:
            if self._dirty:
                self._save()

    def reset(self) -> None:
        with self._lock:
            self._data = self._empty()
            self._save()

    # ---- observations ------------------------------------------------------

    def observe_lighting(self, lighting: str, face_brightness: float | None) -> None:
        if lighting not in LIGHTING:
            return
        now = self._clock()
        hour = str(time.localtime(now).tm_hour)
        with self._lock:
            self._data["last"] = {"ts": now, "lighting": lighting,
                                  "face": None if face_brightness is None
                                  or not math.isfinite(face_brightness)
                                  else round(face_brightness, 1)}
            counts = self._data["hours"].setdefault(hour, {})
            counts[lighting] = min(10_000, counts.get(lighting, 0) + 1)
            self._changed()

    def observe_start(self, lighting: str, *, fps: float | None, open_ms: float,
                      settle_ms: float, settle_frames: int, stable: bool) -> None:
        if lighting not in LIGHTING:
            return
        key = f"{lighting}@{int(fps) if fps else 'default'}"
        with self._lock:
            bucket = self._data["buckets"].setdefault(key, {"n": 0})
            bucket["n"] = min(100_000, bucket["n"] + 1)
            bucket["open_ms"] = round(_ewma(bucket.get("open_ms"), open_ms), 1)
            bucket["settle_ms"] = round(_ewma(bucket.get("settle_ms"), settle_ms), 1)
            bucket["settle_frames"] = round(_ewma(bucket.get("settle_frames"), settle_frames), 2)
            bucket["unstable"] = min(100_000, bucket.get("unstable", 0) + (0 if stable else 1))
            self._changed()

    def observe_fps_switch(self, before: float, after: float, cost_ms: float) -> None:
        """Record the brightness gain from switching to the low-light frame rate."""
        if not (math.isfinite(before) and math.isfinite(after)) or before <= 1.0:
            return
        gain = after / before
        with self._lock:
            low = self._data["low_light"]
            gains = (low.get("gains") or [])[-(MAX_TRIALS_KEPT - 1):] + [round(gain, 3)]
            low["gains"] = gains
            low["switch_ms"] = round(_ewma(low.get("switch_ms"), cost_ms), 1)
            if len(gains) >= MIN_TRIALS:
                median = statistics.median(gains)
                if median >= MIN_GAIN:
                    low["state"] = "enabled"
                    low["reason"] = (f"A lower frame rate made your face {median:.1f}x "
                                     f"brighter in {len(gains)} trials")
                else:
                    low["state"] = "disabled"
                    low["reason"] = (f"A lower frame rate only changed brightness by "
                                     f"{median:.2f}x in {len(gains)} trials")
            self._save()

    # ---- predictions -------------------------------------------------------

    def predict_lighting(self) -> str:
        now = self._clock()
        with self._lock:
            last = self._data.get("last")
            if last and now - float(last.get("ts", 0)) <= RECENT_S:
                return str(last.get("lighting", "bright"))
            counts = self._data["hours"].get(str(time.localtime(now).tm_hour))
        if counts:
            return max(counts, key=counts.get)
        return "bright"

    def low_light_state(self) -> str:
        with self._lock:
            return str(self._data["low_light"].get("state", "trial"))

    def wants_low_light(self, lighting: str) -> bool:
        """Open directly at the low-light rate only once it has proven useful."""
        return lighting in ("dim", "dark", "backlit") and self.low_light_state() == "enabled"

    def should_trial_low_light(self, lighting: str) -> bool:
        return lighting in ("dim", "dark", "backlit") and self.low_light_state() == "trial"

    def warmup_budget(self, lighting: str, fps: float | None, *, max_frames: int,
                      max_s: float) -> tuple[int, float]:
        """Warm-up limits: learned settle time plus margin, never above the config."""
        key = f"{lighting}@{int(fps) if fps else 'default'}"
        with self._lock:
            bucket = self._data["buckets"].get(key)
        if not bucket or bucket.get("n", 0) < 5:
            return max_frames, max_s
        frames = min(max_frames, max(3, math.ceil(bucket["settle_frames"] * 2 + 2)))
        seconds = min(max_s, max(0.3, bucket["settle_ms"] / 1000.0 * 2 + 0.2))
        return frames, seconds

    def summary(self) -> dict[str, Any]:
        with self._lock:
            return json.loads(json.dumps(self._data))
