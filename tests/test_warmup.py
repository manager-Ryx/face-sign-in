"""Adaptive warm-up: stops once exposure is stable, never runs past its budget."""
from __future__ import annotations

import unittest
from unittest.mock import patch

import _isolation  # noqa: F401
import numpy as np

from face_service.camera import Camera, CameraReadError


class FakeCapture:
    def __init__(self, brightness: list[float | None]):
        self.values = list(brightness)

    def read(self):
        value = self.values.pop(0) if self.values else self.last
        self.last = value
        if value is None:
            return False, None
        return True, np.full((48, 64, 3), value, dtype=np.uint8)

    def release(self) -> None:
        pass


def camera_with(brightness: list[float | None], **kwargs) -> Camera:
    cam = Camera(warmup_frames=kwargs.pop("max_frames", 12), **kwargs)
    cam._cap = FakeCapture(brightness)  # type: ignore[assignment]
    return cam


class WarmupTests(unittest.TestCase):
    def test_already_stable_image_stops_after_three_frames(self) -> None:
        report = camera_with([14, 14, 15, 15, 15]).settle()
        self.assertTrue(report.stable)
        self.assertEqual(report.frames, 3)
        self.assertIsNotNone(report.frame)

    def test_exposure_ramp_waits_until_it_settles(self) -> None:
        ramp = [5, 20, 45, 70, 90, 100, 104, 105, 105, 105]
        report = camera_with(ramp).settle()
        self.assertTrue(report.stable)
        self.assertGreaterEqual(report.frames, 7)
        self.assertLessEqual(report.frames, 10)

    def test_movement_or_flicker_cannot_extend_past_the_frame_budget(self) -> None:
        flicker = [60, 120] * 50
        report = camera_with(flicker, max_frames=8).settle()
        self.assertFalse(report.stable)
        self.assertEqual(report.frames, 8)
        self.assertEqual(report.reason, "frames")

    def test_time_budget_is_enforced(self) -> None:
        clock = iter(np.arange(0, 10, 0.4))
        with patch("face_service.camera.time.perf_counter", side_effect=lambda: next(clock)):
            report = camera_with([60, 120] * 50, max_frames=50).settle(max_s=1.0)
        self.assertFalse(report.stable)
        self.assertEqual(report.reason, "time")

    def test_dark_but_stable_image_does_not_wait_longer(self) -> None:
        # Darkness alone is not a reason to keep capturing: stability is.
        report = camera_with([3, 3, 3, 3, 3]).settle()
        self.assertTrue(report.stable)
        self.assertEqual(report.frames, 3)

    def test_stopped_camera_raises_instead_of_spinning(self) -> None:
        with patch("face_service.camera.time.sleep"), self.assertRaises(CameraReadError):
            camera_with([None] * 20).settle()


if __name__ == "__main__":
    unittest.main()
