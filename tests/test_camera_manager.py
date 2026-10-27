"""Camera hand-over, priority, hold, and release."""
from __future__ import annotations

import tempfile
import threading
import time
import unittest
from pathlib import Path

import _isolation  # noqa: F401
import numpy as np

from face_service.camera import CameraBusyError, OpenReport, SettleReport
from face_service.camera_manager import CameraManager, CameraUnavailable
from face_service.camera_profile import CameraProfile
from face_service.config import Config
from face_service.metrics import NullMetrics


class FakeCamera:
    instances: list[FakeCamera] = []

    def __init__(self, cfg, fps, fail: Exception | None = None):
        self.fps = fps
        self.opened = False
        self.closed = False
        self.fail = fail
        self.report = None
        FakeCamera.instances.append(self)

    def open(self):
        if self.fail:
            raise self.fail
        self.opened = True
        self.report = OpenReport(0, 700, 640.0, self.fps, (640, 480))
        return self.report

    def settle(self, **kwargs):
        return SettleReport(3, 90.0, True, "stable", [50, 50, 50],
                            np.zeros((480, 640, 3), np.uint8))

    def read_frame(self):
        return np.zeros((480, 640, 3), np.uint8)

    def close(self):
        self.closed = True
        if getattr(self, "fail_close", None):
            raise self.fail_close


class CameraManagerTests(unittest.TestCase):
    def setUp(self) -> None:
        FakeCamera.instances = []
        self.dir = tempfile.TemporaryDirectory()
        self.cfg = Config(unlock_camera_hold_s=15.0)
        self.profile = CameraProfile(Path(self.dir.name) / "camera.json")
        self.manager = CameraManager(lambda: self.cfg, self.profile, NullMetrics(),
                                     camera_factory=FakeCamera)

    def tearDown(self) -> None:
        self.manager.close()
        self.dir.cleanup()

    def test_a_camera_that_fails_to_close_does_not_keep_it_locked(self) -> None:
        with self.manager.session("presence") as session:
            session.camera.fail_close = RuntimeError("device unplugged")
        with self.manager.session("presence", wait_s=0.5):
            pass  # would raise camera-busy if the first session had kept the lock

    def test_background_check_releases_the_camera_immediately(self) -> None:
        with self.manager.session("presence"):
            pass
        self.assertTrue(FakeCamera.instances[0].closed)
        self.assertEqual(self.manager.status()["state"], "idle")

    def test_lock_screen_retry_keeps_the_camera_briefly_then_releases(self) -> None:
        with self.manager.session("unlock") as session:
            session.release_after = 0.1
        camera = FakeCamera.instances[0]
        self.assertFalse(camera.closed)
        with self.manager.session("unlock"):
            pass  # reuses the open camera
        self.assertEqual(len(FakeCamera.instances), 1)
        time.sleep(0.05)
        self.assertTrue(camera.closed)

    def test_hold_timer_releases_when_retries_stop(self) -> None:
        with self.manager.session("unlock") as session:
            session.release_after = 0.1
        time.sleep(0.3)
        self.assertTrue(FakeCamera.instances[0].closed)

    def test_lease_blocks_every_capture_until_ended(self) -> None:
        self.manager.lease("enrollment", 60)
        with self.assertRaises(CameraUnavailable) as raised, self.manager.session("unlock"):
            pass
        self.assertEqual(raised.exception.reason, "camera-leased")
        self.assertEqual(FakeCamera.instances, [])
        self.assertTrue(self.manager.end_lease("enrollment"))
        with self.manager.session("unlock"):
            pass
        self.assertEqual(len(FakeCamera.instances), 1)

    def test_lease_is_bounded_and_expires(self) -> None:
        clock = [100.0]
        manager = CameraManager(lambda: self.cfg, self.profile, NullMetrics(),
                                camera_factory=FakeCamera, clock=lambda: clock[0])
        self.assertEqual(manager.lease("enrollment", 1e12), 600.0)
        clock[0] += 601
        self.assertIsNone(manager.leased_to())

    def test_lease_renewal_requires_the_same_owner(self) -> None:
        self.manager.lease("enrollment", 60)
        self.assertTrue(self.manager.renew("enrollment", 60))
        self.assertFalse(self.manager.renew("someone-else", 60))

    def test_taking_a_lease_closes_a_held_camera(self) -> None:
        with self.manager.session("unlock") as session:
            session.release_after = 10
        self.manager.lease("enrollment", 30)
        self.assertTrue(FakeCamera.instances[0].closed)

    def test_waiting_unlock_preempts_a_presence_check(self) -> None:
        preempted = []
        entered = threading.Event()

        def presence() -> None:
            with self.manager.session("presence") as session:
                entered.set()
                for _ in range(100):
                    if session.preempted():
                        preempted.append(True)
                        return
                    time.sleep(0.01)

        worker = threading.Thread(target=presence)
        worker.start()
        entered.wait(2)
        with self.manager.session("unlock") as session:
            self.assertFalse(session.preempted())
        worker.join(2)
        self.assertEqual(preempted, [True])

    def test_open_failure_is_reported_and_nothing_is_left_open(self) -> None:
        manager = CameraManager(
            lambda: self.cfg, self.profile, NullMetrics(),
            camera_factory=lambda cfg, fps: FakeCamera(cfg, fps, CameraBusyError("in use")))
        with self.assertRaises(CameraBusyError), manager.session("unlock"):
            pass
        status = manager.status()
        self.assertEqual(status["last_error"]["state"], "busy")
        self.assertEqual(status["state"], "idle")

    def test_hold_time_is_learned_from_retry_gaps(self) -> None:
        clock = [0.0]
        manager = CameraManager(lambda: self.cfg, self.profile, NullMetrics(),
                                camera_factory=FakeCamera, clock=lambda: clock[0])
        for _ in range(8):
            with manager.session("unlock"):
                clock[0] += 0.5
            clock[0] += 1.2  # the sign-in tile retries about a second later
        self.assertAlmostEqual(manager.hold_seconds(), 2.2, places=1)
        manager.close()

    def test_low_light_rate_is_used_only_once_proven(self) -> None:
        self.profile.observe_lighting("dim", 45.0)
        with self.manager.session("unlock"):
            pass
        self.assertIsNone(FakeCamera.instances[-1].fps)
        for _ in range(3):
            self.profile.observe_fps_switch(45.0, 80.0, 1300.0)
        self.assertEqual(self.profile.low_light_state(), "enabled")
        with self.manager.session("unlock"):
            pass
        self.assertEqual(FakeCamera.instances[-1].fps, self.cfg.camera_low_light_fps)

    def test_low_light_is_disabled_when_it_does_not_help(self) -> None:
        for _ in range(3):
            self.profile.observe_fps_switch(45.0, 47.0, 1300.0)
        self.assertEqual(self.profile.low_light_state(), "disabled")
        self.assertIn("1.04x", self.profile.summary()["low_light"]["reason"])


if __name__ == "__main__":
    unittest.main()
