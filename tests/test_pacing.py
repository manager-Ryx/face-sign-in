"""Lock-screen pacing: camera use while nobody is there."""
from __future__ import annotations

import threading
import time
import unittest

import _isolation  # noqa: F401

from face_service.pacing import LockScreenPacer


class Clock:
    def __init__(self) -> None:
        self.now = 1000.0

    def __call__(self) -> float:
        return self.now


def make(clock: Clock, display: list[str | None] | None = None) -> LockScreenPacer:
    state = display if display is not None else [None]
    return LockScreenPacer(idle_after_s=lambda: 60.0, idle_interval_s=lambda: 10.0,
                           pause_when_display_off=lambda: True,
                           display_state=lambda: state[0], clock=clock)


class PacingTests(unittest.TestCase):
    def test_new_lock_screen_scans_immediately(self) -> None:
        clock = Clock()
        pacer = make(clock)
        ticket = pacer.begin()
        self.assertTrue(ticket.session_started)
        self.assertEqual(pacer.wait_turn(ticket), "go")

    def test_goes_idle_after_a_minute_without_a_face(self) -> None:
        clock = Clock()
        pacer = make(clock)
        for _ in range(13):  # the sign-in tile retries every few seconds
            pacer.begin()
            pacer.end(face_seen=False)
            clock.now += 5
        self.assertEqual(pacer.mode, "idle")
        self.assertFalse(pacer.begin().interactive)

    def test_seeing_a_face_keeps_it_active(self) -> None:
        clock = Clock()
        pacer = make(clock)
        pacer.begin()
        pacer.end(face_seen=True)
        clock.now += 30
        self.assertEqual(pacer.mode, "active")

    def test_learned_routine_keeps_scanning_past_the_window(self) -> None:
        clock = Clock()
        likely = [False]
        pacer = LockScreenPacer(idle_after_s=lambda: 60.0, idle_interval_s=lambda: 10.0,
                                pause_when_display_off=lambda: True, clock=clock,
                                likely_back=lambda: likely[0])
        for _ in range(13):
            pacer.begin()
            pacer.end(face_seen=False)
            clock.now += 5
        self.assertEqual(pacer.mode, "idle")
        likely[0] = True
        self.assertEqual(pacer.mode, "active")

    def test_time_in_each_mode_is_tallied(self) -> None:
        clock = Clock()
        pacer = make(clock)
        for _ in range(25):  # two minutes of retries: one active minute, then idle
            pacer.begin()
            pacer.end(face_seen=False)
            clock.now += 5
        spent = pacer.take_mode_time()
        self.assertAlmostEqual(spent["active"] + spent["idle"], 120.0)
        self.assertAlmostEqual(spent["active"], 65.0)
        self.assertEqual(pacer.take_mode_time(), {})

    def test_after_an_automatic_lock_only_the_first_look_is_immediate(self) -> None:
        clock = Clock()
        pacer = LockScreenPacer(idle_after_s=lambda: 60.0, idle_interval_s=lambda: 10.0,
                                pause_when_display_off=lambda: True, clock=clock,
                                start_window_s=lambda: 0.0)
        ticket = pacer.begin()
        self.assertEqual(pacer.wait_turn(ticket), "go")
        pacer.end(face_seen=False)
        clock.now += 5
        pacer.begin()
        self.assertEqual(pacer.mode, "idle")

    def test_display_off_means_no_scanning(self) -> None:
        clock = Clock()
        display = ["off"]
        pacer = make(clock, display)
        pacer.begin()
        pacer.end(face_seen=False)
        self.assertEqual(pacer.mode, "display-off")

    def test_idle_wait_is_bounded_and_wakes_when_superseded(self) -> None:
        pacer = LockScreenPacer(idle_after_s=lambda: 0.05, idle_interval_s=lambda: 5.0,
                                pause_when_display_off=lambda: True)
        first = pacer.begin()
        pacer.end(face_seen=False)
        time.sleep(0.1)
        waiting = pacer.begin()
        self.assertEqual(pacer.mode, "idle")
        result = []
        thread = threading.Thread(target=lambda: result.append(pacer.wait_turn(waiting)))
        thread.start()
        time.sleep(0.1)
        newer = pacer.begin()  # the tile was re-armed by the user
        thread.join(2)
        self.assertEqual(result, ["superseded"])
        self.assertTrue(newer.interactive)
        self.assertEqual(pacer.wait_turn(newer), "go")
        self.assertNotEqual(first.generation, newer.generation)

    def test_a_tile_that_reports_selection_is_believed(self) -> None:
        clock = Clock()
        pacer = make(clock)
        pacer.begin(False)
        # Overlapping requests are a click only for tiles that don't say (older ones).
        self.assertFalse(pacer.begin(False).interactive)
        self.assertTrue(pacer.begin(None).interactive)
        pacer.end(face_seen=False)
        pacer.end(face_seen=False)
        pacer.end(face_seen=False)
        clock.now += 1
        self.assertTrue(pacer.begin(True).interactive)

    def test_display_turning_on_wakes_a_waiting_request(self) -> None:
        display = ["off"]
        pacer = LockScreenPacer(idle_after_s=lambda: 60.0, idle_interval_s=lambda: 10.0,
                                pause_when_display_off=lambda: True,
                                display_state=lambda: display[0])
        pacer.begin()
        pacer.end(face_seen=False)
        ticket = pacer.begin()
        result = []
        thread = threading.Thread(target=lambda: result.append(pacer.wait_turn(ticket)))
        thread.start()
        time.sleep(0.1)
        display[0] = "on"
        started = time.monotonic()
        pacer.display_changed("on")
        thread.join(2)
        self.assertEqual(result, ["go"])
        self.assertLess(time.monotonic() - started, 1.0)

    def test_stop_releases_waiters(self) -> None:
        display = ["off"]
        pacer = LockScreenPacer(idle_after_s=lambda: 60.0, idle_interval_s=lambda: 10.0,
                                pause_when_display_off=lambda: True,
                                display_state=lambda: display[0])
        pacer.begin()
        pacer.end(face_seen=False)
        ticket = pacer.begin()
        result = []
        thread = threading.Thread(target=lambda: result.append(pacer.wait_turn(ticket)))
        thread.start()
        time.sleep(0.05)
        pacer.stop()
        thread.join(2)
        self.assertEqual(result, ["stopped"])


if __name__ == "__main__":
    unittest.main()
