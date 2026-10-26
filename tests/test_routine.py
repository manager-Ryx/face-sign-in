"""Learning when you come back to a locked PC, so scanning is continuous when it pays off."""
from __future__ import annotations

import json
import random
import shutil
import tempfile
import time
import unittest
from pathlib import Path

import _isolation  # noqa: F401
import numpy as np

from face_service.routine import ReturnRoutine

HOUR = 3600.0
MINUTE = 60.0


def at(day: int, hour: float) -> float:
    """Local time ``hour`` o'clock on ``day`` days after Monday 1 June 2026 (no DST change)."""
    return time.mktime((2026, 6, 1 + day, 0, 0, 0, 0, 0, -1)) + hour * HOUR


def office_routine(days: int, *, arrive: float = 7.75, seed: int = 7) -> list[tuple[float, float]]:
    """Locks of a regular routine: breaks, lunch, evenings away, slower weekends."""
    rng = random.Random(seed)
    present: list[tuple[float, float]] = []
    for day in range(days):
        if day % 7 < 5:
            start = arrive + rng.uniform(-0.25, 0.3)
            first = 10.5 + rng.uniform(-0.3, 0.3)
            lunch = 12.5 + rng.uniform(-0.2, 0.2)
            second = 15.0 + rng.uniform(-0.3, 0.3)
            spans = [(start, first), (first + rng.uniform(3, 10) / 60, lunch),
                     (lunch + rng.uniform(40, 55) / 60, second),
                     (second + rng.uniform(3, 10) / 60, 18.0 + rng.uniform(-0.5, 0.5))]
        else:
            spans = [(10.0 + rng.uniform(-0.5, 0.5), 13.0 + rng.uniform(-0.3, 0.3)),
                     (16.0 + rng.uniform(-0.5, 0.5), 22.0 + rng.uniform(-0.5, 0.5))]
        present += [(at(day, a), at(day, b)) for a, b in spans]
    return [(present[i][1], present[i + 1][0]) for i in range(len(present) - 1)]


class RoutineTests(unittest.TestCase):
    def setUp(self) -> None:
        self.folder = Path(tempfile.mkdtemp())
        self.path = self.folder / "routine.json"
        self.now = [at(0, 0)]
        self.routine = ReturnRoutine(self.path, clock=lambda: self.now[0])

    def tearDown(self) -> None:
        shutil.rmtree(self.folder, ignore_errors=True)

    def feed(self, locks: list[tuple[float, float]], routine: ReturnRoutine | None = None) -> None:
        for locked, returned in locks:
            self.now[0] = returned
            (routine or self.routine).observe(locked, returned)

    # ---- before enough is known --------------------------------------------

    def test_no_schedule_until_enough_is_known(self) -> None:
        self.feed(office_routine(2))  # 8 locks over 2 days
        self.assertIsNone(self.routine.schedule())
        self.assertFalse(self.routine.likely_back(at(2, 7.75), at(1, 18.0)))
        summary = self.routine.summary()
        self.assertFalse(summary["learned"])
        self.assertEqual(summary["days"], 2)

    def test_short_locks_are_not_absences(self) -> None:
        self.assertFalse(self.routine.observe(at(0, 9), at(0, 9) + 5))
        self.assertEqual(self.routine.summary()["locks"], 0)

    # ---- what it learns -------------------------------------------------------

    def test_learns_when_you_usually_arrive(self) -> None:
        self.feed(office_routine(21))
        self.assertIsNotNone(self.routine.schedule())
        evening = at(21, 18.0)  # Monday evening lock, back on Tuesday morning
        self.assertTrue(self.routine.likely_back(at(22, 7.75), evening))
        self.assertFalse(self.routine.likely_back(at(22, 3.0), evening))
        self.assertFalse(self.routine.likely_back(at(21, 21.0), evening))
        weekday = self.routine.summary()["usual_returns"]["weekday"]
        self.assertTrue(any(a <= 7.75 * 60 < b for a, b in weekday), weekday)

    def test_scans_right_after_you_leave_but_not_all_evening(self) -> None:
        self.feed(office_routine(21))
        afternoon = at(22, 15.0)
        self.assertTrue(self.routine.likely_back(afternoon + 2 * MINUTE, afternoon))
        evening = at(22, 18.0)
        self.assertFalse(self.routine.likely_back(evening + HOUR, evening))

    def test_most_returns_are_caught_while_scanning_with_the_camera_mostly_off(self) -> None:
        locks = office_routine(35)
        split = next(i for i, (locked, _) in enumerate(locks) if locked >= at(28, 0))
        self.feed(locks[:split])
        caught = scanning = locked_minutes = 0
        test = locks[split:]
        for locked, returned in test:
            caught += self.routine.likely_back(returned, locked)
            for t in np.arange(locked, returned, MINUTE):
                scanning += self.routine.likely_back(float(t), locked)
                locked_minutes += 1
        self.assertGreaterEqual(caught / len(test), 0.85)
        self.assertLess(scanning / locked_minutes, 0.35)

    def test_follows_a_changed_routine(self) -> None:
        self.feed(office_routine(21, arrive=7.75))
        later = [(a + 21 * 86400, b + 21 * 86400) for a, b in
                 office_routine(42, arrive=9.5, seed=11)]
        self.feed(later)
        evening = at(63, 18.0)
        self.assertTrue(self.routine.likely_back(at(64, 9.5), evening))
        self.assertFalse(self.routine.likely_back(at(64, 7.6), evening))

    # ---- storage --------------------------------------------------------------

    def test_survives_a_restart(self) -> None:
        self.feed(office_routine(21))
        again = ReturnRoutine(self.path, clock=lambda: self.now[0])
        before, after = self.routine.schedule(), again.schedule()
        self.assertIsNotNone(after)
        for day in ("weekday", "weekend"):
            self.assertTrue(np.array_equal(before.time_of_day[day], after.time_of_day[day]))

    def test_keeps_counts_not_a_history(self) -> None:
        self.feed(office_routine(21))
        stored = json.loads(self.path.read_text(encoding="utf-8"))
        self.assertEqual(set(stored), {"version", "updated", "locks", "days", "last_day",
                                       "since_lock", "time_of_day"})
        self.assertEqual(len(stored["time_of_day"]["weekday"]["returns"]), 48)

    def test_damaged_file_starts_afresh(self) -> None:
        self.path.write_text('{"version": 1, "since_lock": {"weekday-morning": 3}}',
                             encoding="utf-8")
        fresh = ReturnRoutine(self.path, clock=lambda: self.now[0])
        self.assertIsNone(fresh.schedule())
        self.assertEqual(fresh.summary()["locks"], 0)

    def test_forget(self) -> None:
        self.feed(office_routine(21))
        self.routine.reset()
        self.assertIsNone(self.routine.schedule())
        self.assertFalse(self.path.exists())

    def test_stale_knowledge_stops_driving_the_camera(self) -> None:
        self.feed(office_routine(14))
        self.assertIsNotNone(self.routine.schedule())
        self.now[0] += 120 * 86400  # four months without a single lock
        self.assertIsNone(ReturnRoutine(self.path, clock=lambda: self.now[0]).schedule())


if __name__ == "__main__":
    unittest.main()
