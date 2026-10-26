"""When you usually come back to a locked PC – used to time lock-screen scanning.

The sign-in screen either scans continuously, so you are signed in the moment
you sit down, or takes a quick look every few seconds with the camera off in
between, so you may wait a few seconds. Scanning continuously all the time
would keep the camera light on for hours; quick looks alone add a short wait
to every return. This model spends continuous scanning where it pays off:
when you are likely to be back soon.

What is kept: each lock – from locking to signing in again – is folded into
counts: how many returns, and how many locked hours, fell into each band of
"time since locking" (separately for weekday and weekend nights, mornings,
afternoons and evenings, by when you locked), and into each half hour of a
weekday or weekend day. No list of lock times is stored. The counts halve
every three weeks, so the model follows changes in your routine.

The estimate: a band's return rate is its returns per locked hour, damped
towards your average over all locks (time since locking) or towards zero
(time of day) while it rests on little data. The camera scans continuously
while – judging by how long you have been away (for the first six hours), or by
the time of day – the chance that you are back within the next 15 minutes is at
least 10 %.

Until about ten locks over five days are known there is no schedule, and
the user's fixed setting applies.
"""
from __future__ import annotations

import logging
import math
import threading
import time
from collections.abc import Callable
from dataclasses import dataclass
from pathlib import Path
from typing import Any

import numpy as np

from .storage import read_state, write_json_atomic

log = logging.getLogger(__name__)

# Bands of minutes since locking; an absence is followed for two days at most.
EDGES_MIN = np.array([0, 2, 5, 10, 15, 20, 30, 45, 60, 90, 120, 180, 240, 360, 480, 720, 1080,
                      1440, 2880])
BANDS = len(EDGES_MIN) - 1
PARTS = ("night", "morning", "afternoon", "evening")  # 00-06, 06-12, 12-18, 18-24
DAY_TYPES = ("weekday", "weekend")
GROUPS = tuple(f"{day}-{part}" for day in DAY_TYPES for part in PARTS)
SLOT_MIN = 30
SLOTS = 24 * 60 // SLOT_MIN

MIN_ABSENCE_S = 20.0          # a shorter lock isn't an absence
SINCE_LOCK_LIMIT_MIN = 360    # beyond this, the time of day predicts returns better
MAX_CLOCK_ABSENCE_H = 36.0    # longer ones (holidays) don't shape the time-of-day pattern
HORIZON_MIN = 15
LIKELY = 0.10                 # scan continuously from this chance of a return within the horizon
HALF_LIFE_DAYS = 21.0
PRIOR_H = 1.0                 # pseudo-hours that damp sparsely observed bands
MIN_LOCKS = 10.0
MIN_DAYS = 5


def _day_type(local: time.struct_time) -> str:
    return "weekend" if local.tm_wday >= 5 else "weekday"


def group_of(ts: float) -> str:
    """The time-of-lock group of a lock at ``ts``, e.g. ``"weekday-evening"``."""
    local = time.localtime(ts)
    return f"{_day_type(local)}-{PARTS[local.tm_hour // 6]}"


def _neighbours(values: np.ndarray) -> np.ndarray:
    """Each half hour together with the one before and after (wrapping at midnight)."""
    return values + np.roll(values, 1) + np.roll(values, -1)


def _chance_soon(rate_per_min: np.ndarray, *, circular: bool) -> np.ndarray:
    """Chance of a return within the horizon, starting at each minute."""
    tail = rate_per_min[:HORIZON_MIN] if circular else np.full(HORIZON_MIN, rate_per_min[-1])
    cumulative = np.concatenate(([0.0], np.cumsum(np.concatenate((rate_per_min, tail)))))
    count = len(rate_per_min)
    return 1.0 - np.exp(-(cumulative[HORIZON_MIN:HORIZON_MIN + count] - cumulative[:count]))


def _ranges(active: np.ndarray) -> list[list[int]]:
    """Runs of ``True`` as ``[start, end)`` index pairs."""
    padded = np.concatenate(([False], active, [False]))
    edges = np.flatnonzero(padded[1:] != padded[:-1])
    return [[int(start), int(end)] for start, end in zip(edges[::2], edges[1::2], strict=True)]


def _daily_ranges(active: np.ndarray) -> list[list[int]]:
    """Like :func:`_ranges`, joining a run that crosses midnight into one."""
    runs = _ranges(active)
    if len(runs) > 1 and runs[0][0] == 0 and runs[-1][1] == len(active):
        runs[-1] = [runs[-1][0], runs[0][1] + len(active)]
        runs = runs[1:]
    return runs


@dataclass(frozen=True)
class Schedule:
    """When continuous scanning is worthwhile, precomputed minute by minute."""

    since_lock: dict[str, np.ndarray]   # group -> per minute since locking (last value repeats)
    time_of_day: dict[str, np.ndarray]  # day type -> per minute of the day

    def likely_back(self, now: float, locked_at: float | None) -> bool:
        local = time.localtime(now)
        if self.time_of_day[_day_type(local)][local.tm_hour * 60 + local.tm_min]:
            return True
        if locked_at is None or now < locked_at:
            return False
        active = self.since_lock[group_of(locked_at)]
        return bool(active[min(int((now - locked_at) // 60), len(active) - 1)])


class ReturnRoutine:
    """Faded counts of locks and returns (``state/routine.json``) and their schedule."""

    def __init__(self, path: Path, *, clock: Callable[[], float] = time.time):
        self.path = path
        self._clock = clock
        self._lock = threading.Lock()
        self._data = self._load()
        self._schedule: Schedule | None = None
        self._rebuild()

    # ---- persistence -------------------------------------------------------

    @staticmethod
    def _empty() -> dict[str, Any]:
        return {"version": 1, "updated": None, "locks": 0.0, "days": 0, "last_day": None,
                "since_lock": {group: {"returns": np.zeros(BANDS), "hours": np.zeros(BANDS)}
                               for group in GROUPS},
                "time_of_day": {day: {"returns": np.zeros(SLOTS), "hours": np.zeros(SLOTS)}
                                for day in DAY_TYPES}}

    def _load(self) -> dict[str, Any]:
        data = read_state(self.path, self._empty(), what="routine")
        try:
            for table, keys, size in (("since_lock", GROUPS, BANDS),
                                      ("time_of_day", DAY_TYPES, SLOTS)):
                data[table] = {key: {name: self._counts(data[table][key][name], size)
                                     for name in ("returns", "hours")} for key in keys}
            data["locks"] = float(data["locks"])
            data["days"] = int(data["days"])
            if data["updated"] is not None:
                data["updated"] = float(data["updated"])
        except (KeyError, TypeError, ValueError) as exc:
            log.warning("routine data damaged, starting fresh: %s", exc)
            data = self._empty()
        return data

    @staticmethod
    def _counts(values: Any, size: int) -> np.ndarray:
        array = np.asarray(values, dtype=float)
        if array.shape != (size,) or not np.all(np.isfinite(array)) or np.any(array < 0):
            raise ValueError("unexpected counts")
        return array

    def _save(self) -> None:
        data = dict(self._data)
        for table in ("since_lock", "time_of_day"):
            data[table] = {key: {name: [round(float(v), 4) for v in values]
                                 for name, values in counts.items()}
                           for key, counts in self._data[table].items()}
        try:
            write_json_atomic(self.path, data)
        except OSError as exc:
            log.warning("routine not saved: %s", exc)

    def reset(self) -> None:
        with self._lock:
            self._data = self._empty()
            self._schedule = None
            self.path.unlink(missing_ok=True)

    # ---- learning ------------------------------------------------------------

    def observe(self, locked_at: float, returned_at: float) -> bool:
        """Fold one lock, from locking to signing in again, into the counts."""
        away_s = returned_at - locked_at
        if not (math.isfinite(away_s) and away_s >= MIN_ABSENCE_S):
            return False
        with self._lock:
            weight = self._fade_to(returned_at)
            self._add_since_lock(group_of(locked_at), away_s / 60.0, weight)
            if away_s <= MAX_CLOCK_ABSENCE_H * 3600.0:
                self._add_time_of_day(locked_at, returned_at, weight)
            data = self._data
            data["locks"] += weight
            day = time.strftime("%Y-%m-%d", time.localtime(returned_at))
            if day != data["last_day"]:
                data["days"] += 1
                data["last_day"] = day
            self._save()
            self._rebuild()
        return True

    def _fade_to(self, ts: float) -> float:
        """Fade the counts to ``ts``; return the weight of an observation made then."""
        updated = self._data["updated"]
        if updated is not None and ts < updated:
            return 0.5 ** ((updated - ts) / 86400.0 / HALF_LIFE_DAYS)
        if updated is not None:
            factor = 0.5 ** ((ts - updated) / 86400.0 / HALF_LIFE_DAYS)
            for table in ("since_lock", "time_of_day"):
                for counts in self._data[table].values():
                    counts["returns"] *= factor
                    counts["hours"] *= factor
            self._data["locks"] *= factor
        self._data["updated"] = ts
        return 1.0

    def _add_since_lock(self, group: str, minutes: float, weight: float) -> None:
        counts = self._data["since_lock"][group]
        lower, upper = EDGES_MIN[:-1], EDGES_MIN[1:]
        counts["hours"] += weight * np.clip(np.minimum(minutes, upper) - lower, 0.0, None) / 60.0
        if minutes < EDGES_MIN[-1]:
            counts["returns"][np.searchsorted(EDGES_MIN, minutes, side="right") - 1] += weight

    def _add_time_of_day(self, start: float, end: float, weight: float) -> None:
        table = self._data["time_of_day"]
        t = start
        while t < end:
            local = time.localtime(t)
            minute = local.tm_hour * 60 + local.tm_min + local.tm_sec / 60.0
            slot = int(minute // SLOT_MIN)
            step = min(end, max(t + 1.0, t + ((slot + 1) * SLOT_MIN - minute) * 60.0))
            table[_day_type(local)]["hours"][slot] += weight * (step - t) / 3600.0
            t = step
        local = time.localtime(end)
        table[_day_type(local)]["returns"][(local.tm_hour * 60 + local.tm_min) // SLOT_MIN] += weight

    # ---- schedule ------------------------------------------------------------

    def _fade_now(self) -> float:
        updated = self._data["updated"]
        if updated is None:
            return 1.0
        return 0.5 ** (max(0.0, self._clock() - updated) / 86400.0 / HALF_LIFE_DAYS)

    def _rebuild(self) -> None:
        data = self._data
        fade = self._fade_now()
        if data["locks"] * fade < MIN_LOCKS or data["days"] < MIN_DAYS:
            self._schedule = None
            return
        groups = data["since_lock"].values()
        overall = (sum(c["returns"] for c in groups) * fade
                   / (sum(c["hours"] for c in groups) * fade + PRIOR_H))
        widths = np.diff(EDGES_MIN)
        since_lock = {}
        for group, counts in data["since_lock"].items():
            rate = ((counts["returns"] * fade + overall * PRIOR_H)
                    / (counts["hours"] * fade + PRIOR_H))
            active = _chance_soon(np.repeat(rate / 60.0, widths), circular=False) >= LIKELY
            active[SINCE_LOCK_LIMIT_MIN:] = False
            since_lock[group] = active
        time_of_day = {}
        for day, counts in data["time_of_day"].items():
            rate = (_neighbours(counts["returns"]) * fade
                    / (_neighbours(counts["hours"]) * fade + PRIOR_H))
            time_of_day[day] = _chance_soon(np.repeat(rate / 60.0, SLOT_MIN),
                                            circular=True) >= LIKELY
        self._schedule = Schedule(since_lock, time_of_day)

    def schedule(self) -> Schedule | None:
        """The current schedule, or ``None`` while there is too little to go on."""
        return self._schedule

    def likely_back(self, now: float, locked_at: float | None) -> bool:
        schedule = self._schedule
        return schedule is not None and schedule.likely_back(now, locked_at)

    def summary(self) -> dict[str, Any]:
        """What has been learned, in minutes, for the Learning page."""
        with self._lock:
            data, schedule = self._data, self._schedule
            info: dict[str, Any] = {
                "learned": schedule is not None,
                "locks": round(data["locks"] * self._fade_now(), 1), "days": data["days"],
                "needs": {"locks": MIN_LOCKS, "days": MIN_DAYS}}
            if schedule is not None:
                info["after_leaving"] = {
                    group: _ranges(active[:24 * 60])[:3]
                    for group, active in schedule.since_lock.items()
                    if data["since_lock"][group]["hours"].sum() > 0}
                info["usual_returns"] = {day: _daily_ranges(active)
                                         for day, active in schedule.time_of_day.items()}
        return info
