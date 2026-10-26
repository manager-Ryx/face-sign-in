"""Optional, approximate away-time statistics (off unless you turn it on).

Only coarse counts are stored: for each kind of day (weekday or weekend) and
hour of day, how many times you were away for less than 10 minutes, 10-30
minutes, and so on. No timestamps, images, or activity histories are kept
apart from the single absence currently in progress.

Accuracy limits: presence is checked periodically (every few minutes), so the
start of an absence noticed by the camera is only known to within one check
interval. Absences that begin with you locking the PC, or end with you
unlocking it, are timed exactly. Remote sessions, long gaps in observation,
and camera failures discard the absence in progress rather than guess.

Recommendations need at least two weeks of observation and enough absences
in a time slot, and always state how many absences they rest on. Adaptive
locking (separately opt-in) may only shorten the timeout, never exceed your
own setting, and never go below its configured minimum.
"""
from __future__ import annotations

import json
import logging
import math
import threading
import time
from collections.abc import Callable
from dataclasses import dataclass
from pathlib import Path
from typing import Any

from face_service.storage import read_state, write_json_atomic

log = logging.getLogger(__name__)

BINS_MIN = (10, 30, 60, 120, 240)            # upper edges in minutes; last bin is open
BIN_LABELS = ("under 10 min", "10-30 min", "30-60 min", "1-2 h", "2-4 h", "over 4 h")
LONG_FROM_BIN = 4                            # bins >= this index count as "gone for hours"
MIN_DAYS = 14
MIN_EPISODES = 8
LONG_SHARE = 0.75
MIN_EPISODE_S = 120.0                        # shorter blips are ignored


def _bin(minutes: float) -> int:
    for index, edge in enumerate(BINS_MIN):
        if minutes < edge:
            return index
    return len(BINS_MIN)


def _day_type(ts: float) -> str:
    return "weekend" if time.localtime(ts).tm_wday >= 5 else "weekday"


def _wilson_lower(successes: int, total: int, z: float = 1.96) -> float:
    if total == 0:
        return 0.0
    p = successes / total
    denom = 1 + z * z / total
    centre = p + z * z / (2 * total)
    margin = z * math.sqrt(p * (1 - p) / total + z * z / (4 * total * total))
    return max(0.0, (centre - margin) / denom)


@dataclass
class Recommendation:
    day_type: str
    hour: int
    episodes: int
    long_share: float
    confidence: float
    text: str


class AbsenceModel:
    def __init__(self, path: Path, *, clock: Callable[[], float] = time.time):
        self.path = path
        self._clock = clock
        self._lock = threading.Lock()
        self._data = self._load()

    @staticmethod
    def _empty() -> dict[str, Any]:
        return {"version": 1, "days": [], "buckets": {}, "open": None, "last_seen": None}

    def _load(self) -> dict[str, Any]:
        return read_state(self.path, self._empty(), what="absence statistics")

    def _save(self) -> None:
        try:
            write_json_atomic(self.path, self._data)
        except OSError as exc:
            log.warning("absence statistics not saved: %s", exc)

    def reset(self) -> None:
        with self._lock:
            self._data = self._empty()
            self.path.unlink(missing_ok=True)

    # ---- observations ------------------------------------------------------

    def _mark_day(self, ts: float) -> None:
        day = time.strftime("%Y-%m-%d", time.localtime(ts))
        days = self._data["days"]
        if day not in days:
            days.append(day)
            del days[:-120]  # only the count of recent observed days matters

    def observe_present(self, ts: float | None = None) -> None:
        ts = self._clock() if ts is None else ts
        with self._lock:
            self._close(ts)
            self._data["last_seen"] = ts
            self._mark_day(ts)
            self._save()

    def observe_absent(self, ts: float | None = None) -> None:
        ts = self._clock() if ts is None else ts
        with self._lock:
            if self._data["open"] is None and self._data["last_seen"] is not None:
                self._data["open"] = {"start": self._data["last_seen"], "exact": False}
                self._save()

    def observe_lock(self, ts: float | None = None) -> None:
        ts = self._clock() if ts is None else ts
        with self._lock:
            if self._data["open"] is None:
                self._data["open"] = {"start": ts, "exact": True}
                self._save()

    def observe_unlock(self, ts: float | None = None) -> None:
        ts = self._clock() if ts is None else ts
        with self._lock:
            self._close(ts)
            self._data["last_seen"] = ts
            self._mark_day(ts)
            self._save()

    def discard_open(self, reason: str) -> None:
        """Unobserved period (remote session, camera failure): do not guess."""
        with self._lock:
            if self._data["open"] is not None:
                log.info("absence in progress discarded: %s", reason)
                self._data["open"] = None
                self._save()

    def _close(self, end: float) -> None:
        episode = self._data["open"]
        self._data["open"] = None
        if not episode:
            return
        duration = end - float(episode["start"])
        if duration < MIN_EPISODE_S or duration > 7 * 86400:
            return
        start = float(episode["start"])
        day_type = _day_type(start)
        hour = str(time.localtime(start).tm_hour)
        bucket = self._data["buckets"].setdefault(day_type, {}).setdefault(
            hour, [0] * (len(BINS_MIN) + 1))
        bucket[_bin(duration / 60.0)] += 1

    # ---- conclusions -------------------------------------------------------

    @property
    def observed_days(self) -> int:
        with self._lock:
            return len(self._data["days"])

    def summary(self) -> dict[str, Any]:
        with self._lock:
            return {"observed_days": len(self._data["days"]),
                    "buckets": json.loads(json.dumps(self._data["buckets"])),
                    "bins": list(BIN_LABELS),
                    "in_progress": self._data["open"] is not None}

    def recommendations(self) -> list[Recommendation]:
        with self._lock:
            if len(self._data["days"]) < MIN_DAYS:
                return []
            result = []
            for day_type, hours in self._data["buckets"].items():
                for hour, counts in hours.items():
                    total = sum(counts)
                    if total < MIN_EPISODES:
                        continue
                    long = sum(counts[LONG_FROM_BIN:])
                    share = long / total
                    confidence = _wilson_lower(long, total)
                    if share >= LONG_SHARE and confidence >= 0.5:
                        result.append(Recommendation(
                            day_type, int(hour), total, share, confidence,
                            f"On {day_type}s, {long} of {total} absences that started around "
                            f"{int(hour):02d}:00 lasted over 2 hours. A shorter lock timeout "
                            "at that time would lock sooner without affecting short breaks."))
            return sorted(result, key=lambda r: (r.day_type, r.hour))

    def timeout_for(self, ts: float, *, base_timeout_s: int, minimum_s: int) -> tuple[int, str]:
        """Adaptive timeout: never longer than ``base_timeout_s``."""
        floor = min(base_timeout_s, max(300, minimum_s))
        hour = time.localtime(ts).tm_hour
        day_type = _day_type(ts)
        for rec in self.recommendations():
            if rec.day_type == day_type and rec.hour == hour:
                return floor, (f"Shorter timeout ({floor // 60} min): on {day_type}s you are "
                               f"usually away for hours when you leave around {hour:02d}:00 "
                               f"({rec.episodes} absences observed).")
        return base_timeout_s, "Your normal lock timeout."
