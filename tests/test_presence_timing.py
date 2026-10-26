from __future__ import annotations

import unittest
from unittest.mock import patch

import _isolation  # noqa: F401

from face_service.config import Config
from presence_monitor.monitor import AbsenceTimer, PresenceMonitor

PRESENT = {"ok": True, "present": True, "mode": "recognition"}
ABSENT = {"ok": True, "present": False, "mode": "recognition", "outcome": "no_face"}


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
            self.assertEqual(timer.mark_absent(), (second, False))
        clock.now = 600
        self.assertEqual(timer.mark_absent(), (600, True))

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
        timer.mark_absent()
        timer.reset()
        clock.now = 600
        self.assertEqual(timer.mark_absent(), (0, False))


class PresenceMonitorTests(unittest.TestCase):
    def setUp(self) -> None:
        self.cfg = Config(presence_interval_s=120, presence_absence_timeout_s=600)
        self.locked = [False]
        self.wall = FakeClock(1_000_000.0)
        self.monitor = PresenceMonitor(self.cfg, locked=lambda: self.locked[0], clock=self.wall)
        self.clock = FakeClock()
        self.monitor._absence.clock = self.clock
        self.remote = patch("presence_monitor.monitor.is_remote_context",
                            return_value=(False, ""))
        self.remote.start()

    def tearDown(self) -> None:
        self.remote.stop()

    def run_ticks(self, responses: list, times: list[float]):
        replies = iter(responses)
        with patch("presence_monitor.monitor.pipe_call", side_effect=lambda *a, **k: next(replies)), \
                patch("presence_monitor.monitor._lock_workstation") as lock:
            for second in times:
                self.clock.now = second
                self.monitor._tick()
        return lock

    def test_120_second_checks_lock_once_at_ten_minutes(self) -> None:
        lock = self.run_ticks([PRESENT] + [ABSENT] * 5, [0, 120, 240, 360, 480, 600])
        lock.assert_called_once_with()

    def test_service_error_cancels_pending_absence_for_safety(self) -> None:
        lock = self.run_ticks([PRESENT, ABSENT, None, ABSENT, ABSENT], [0, 480, 600, 720, 1200])
        lock.assert_not_called()

    def test_missing_profile_never_counts_as_absence(self) -> None:
        not_ready = {"ok": False, "reason": "not-enrolled"}
        lock = self.run_ticks([PRESENT] + [not_ready] * 6, [0, 120, 240, 360, 480, 600, 720])
        lock.assert_not_called()
        self.assertEqual(self.monitor.snapshot()["last_result"], "error")

    def test_skipped_checks_are_inconclusive(self) -> None:
        skipped = {"ok": True, "present": True, "skipped": "camera-leased"}
        lock = self.run_ticks([PRESENT, ABSENT, ABSENT, skipped, ABSENT, ABSENT],
                              [0, 120, 480, 540, 600, 700])
        lock.assert_not_called()

    def test_no_checks_while_windows_is_locked(self) -> None:
        self.locked[0] = True
        with patch("presence_monitor.monitor.pipe_call") as call:
            self.monitor._tick()
        call.assert_not_called()
        self.assertEqual(self.monitor.snapshot()["last_reason"], "locked")

    def test_timed_pause_expires(self) -> None:
        self.monitor.pause(600)
        self.assertTrue(self.monitor.is_paused())
        self.wall.now += 601
        self.assertFalse(self.monitor.is_paused())

    def test_unlock_event_restarts_the_absence_window(self) -> None:
        self.run_ticks([PRESENT, ABSENT], [0, 480])
        self.monitor.on_session_event("unlock", {})
        lock = self.run_ticks([ABSENT, ABSENT], [600, 700])
        lock.assert_not_called()


if __name__ == "__main__":
    unittest.main()
