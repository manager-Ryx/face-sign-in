from __future__ import annotations

import unittest
from unittest.mock import patch

from face_service.config import Config
from presence_monitor.monitor import AbsenceTimer, PresenceMonitor


class FakeClock:
    def __init__(self, now: float = 0.0):
        self.now = now

    def __call__(self) -> float:
        return self.now


class AbsenceTimerTests(unittest.TestCase):
    def test_does_not_fire_before_timeout_and_fires_at_timeout(self) -> None:
        clock = FakeClock()
        timer = AbsenceTimer(600, clock)

        timer.mark_present()
        for second in (120, 240, 360, 480):
            clock.now = second
            elapsed, expired = timer.mark_absent()
            self.assertEqual(elapsed, second)
            self.assertFalse(expired)

        clock.now = 600
        elapsed, expired = timer.mark_absent()
        self.assertEqual(elapsed, 600)
        self.assertTrue(expired)

    def test_present_match_restarts_elapsed_time(self) -> None:
        clock = FakeClock()
        timer = AbsenceTimer(600, clock)

        timer.mark_present()
        clock.now = 480
        self.assertEqual(timer.mark_absent(), (480, False))
        timer.mark_present()
        clock.now = 960
        self.assertEqual(timer.mark_absent(), (480, False))
        clock.now = 1080
        self.assertEqual(timer.mark_absent(), (600, True))

    def test_reset_requires_a_new_full_absence_window(self) -> None:
        clock = FakeClock()
        timer = AbsenceTimer(600, clock)

        timer.mark_present()
        clock.now = 480
        self.assertEqual(timer.mark_absent(), (480, False))
        timer.reset()
        clock.now = 600
        self.assertEqual(timer.mark_absent(), (0, False))


class PresenceMonitorPolicyTests(unittest.TestCase):
    def setUp(self) -> None:
        self.cfg = Config(
            presence_interval_s=120,
            presence_absence_timeout_s=600,
            presence_mode="recognition",
        )
        self.monitor = PresenceMonitor(self.cfg)
        self.clock = FakeClock()
        self.monitor._absence.clock = self.clock

    def test_120_second_probes_lock_once_at_ten_minutes(self) -> None:
        responses = iter(
            [{"ok": True, "present": True, "real": True, "mode": "recognition"}]
            + [
                {"ok": True, "present": False, "real": False, "mode": "recognition"}
                for _ in range(5)
            ]
        )
        with (
            patch("presence_monitor.monitor.is_remote_context", return_value=(False, "")),
            patch("presence_monitor.monitor.pipe_call", side_effect=lambda *a, **k: next(responses)),
            patch("presence_monitor.monitor._lock_workstation") as lock,
        ):
            self.monitor._tick()
            for second in (120, 240, 360, 480):
                self.clock.now = second
                self.monitor._tick()
                lock.assert_not_called()

            self.clock.now = 600
            self.monitor._tick()
            lock.assert_called_once_with()

    def test_service_error_cancels_pending_absence_for_safety(self) -> None:
        responses = iter(
            [
                {"ok": True, "present": True, "real": True, "mode": "recognition"},
                {"ok": True, "present": False, "real": False, "mode": "recognition"},
                None,
                {"ok": True, "present": False, "real": False, "mode": "recognition"},
                {"ok": True, "present": False, "real": False, "mode": "recognition"},
            ]
        )
        with (
            patch("presence_monitor.monitor.is_remote_context", return_value=(False, "")),
            patch("presence_monitor.monitor.pipe_call", side_effect=lambda *a, **k: next(responses)),
            patch("presence_monitor.monitor._lock_workstation") as lock,
        ):
            self.monitor._tick()
            self.clock.now = 480
            self.monitor._tick()
            self.clock.now = 600
            self.monitor._tick()
            self.clock.now = 720
            self.monitor._tick()
            self.clock.now = 1200
            self.monitor._tick()
            lock.assert_not_called()


class ConfigCompatibilityTests(unittest.TestCase):
    def test_legacy_strikes_are_only_the_zero_timeout_fallback(self) -> None:
        legacy = Config(presence_interval_s=60, presence_absent_strikes=3)
        explicit = Config(
            presence_interval_s=120,
            presence_absent_strikes=99,
            presence_absence_timeout_s=600,
        )
        self.assertEqual(legacy.effective_presence_absence_timeout_s, 180)
        self.assertEqual(explicit.effective_presence_absence_timeout_s, 600)


if __name__ == "__main__":
    unittest.main()
