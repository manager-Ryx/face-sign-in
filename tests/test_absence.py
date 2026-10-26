"""Optional away-time statistics: counts only, cautious recommendations."""
from __future__ import annotations

import json
import tempfile
import time
import unittest
from pathlib import Path

import _isolation  # noqa: F401

from presence_monitor.absence import MIN_DAYS, AbsenceModel


def at(day: int, hour: int, minute: int = 0) -> float:
    # 2026-10-05 is a Monday.
    return time.mktime((2026, 10, 5 + day, hour, minute, 0, 0, 0, -1))


class AbsenceTests(unittest.TestCase):
    def setUp(self) -> None:
        self.dir = tempfile.TemporaryDirectory()
        self.model = AbsenceModel(Path(self.dir.name) / "absence.json")

    def tearDown(self) -> None:
        self.dir.cleanup()

    def evening_away(self, day: int, hours: float) -> None:
        self.model.observe_present(at(day, 17, 55))
        self.model.observe_lock(at(day, 18))
        self.model.observe_unlock(at(day, 18) + hours * 3600)

    def test_stores_only_counts(self) -> None:
        self.evening_away(0, 3)
        raw = json.loads(self.model.path.read_text(encoding="utf-8"))
        self.assertIsNone(raw["open"])
        self.assertNotIn("episodes", raw)
        self.assertEqual(sum(raw["buckets"]["weekday"]["18"]), 1)

    def test_no_recommendation_without_two_weeks(self) -> None:
        for day in range(5):
            self.evening_away(day, 4)
        self.assertEqual(self.model.recommendations(), [])

    def test_recommends_shorter_timeout_when_absences_are_reliably_long(self) -> None:
        weekdays = [d for d in range(28) if (d % 7) < 5]
        for day in weekdays[:MIN_DAYS + 2]:
            self.evening_away(day, 3)
        recs = self.model.recommendations()
        self.assertEqual([(r.day_type, r.hour) for r in recs], [("weekday", 18)])
        self.assertIn("lasted over 2 hours", recs[0].text)
        timeout, rule = self.model.timeout_for(at(1, 18, 10), base_timeout_s=3600,
                                               minimum_s=900)
        self.assertEqual(timeout, 900)
        self.assertIn("Shorter timeout", rule)

    def test_adaptive_timeout_never_exceeds_the_users_setting(self) -> None:
        weekdays = [d for d in range(28) if (d % 7) < 5]
        for day in weekdays[:MIN_DAYS + 2]:
            self.evening_away(day, 3)
        timeout, _ = self.model.timeout_for(at(1, 18), base_timeout_s=600, minimum_s=900)
        self.assertLessEqual(timeout, 600)
        timeout, _ = self.model.timeout_for(at(1, 9), base_timeout_s=3600, minimum_s=900)
        self.assertEqual(timeout, 3600)

    def test_mixed_short_and_long_absences_do_not_recommend(self) -> None:
        weekdays = [d for d in range(28) if (d % 7) < 5]
        for i, day in enumerate(weekdays[:MIN_DAYS + 2]):
            self.evening_away(day, 3 if i % 2 else 0.2)
        self.assertEqual(self.model.recommendations(), [])

    def test_unobserved_period_is_discarded(self) -> None:
        self.model.observe_present(at(0, 10))
        self.model.observe_absent(at(0, 10, 5))
        self.model.discard_open("remote session")
        self.model.observe_present(at(0, 15))
        self.assertEqual(self.model.summary()["buckets"], {})


if __name__ == "__main__":
    unittest.main()
