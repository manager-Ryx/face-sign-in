"""Camera tuning for the face: brightness against a bright window, focus for the desk."""
from __future__ import annotations

import unittest

import _isolation  # noqa: F401
import cv2
import numpy as np

from face_service.camera import CONTROLS_MARKER, Camera, _undo_leftover_changes
from face_service.exposure import FACE_HIGH, FACE_LOW, calibrate_focus, expose_for_face
from face_service.quality import FaceBox

BRIGHTNESS, AUTOFOCUS, FOCUS = (cv2.CAP_PROP_BRIGHTNESS, cv2.CAP_PROP_AUTOFOCUS,
                                cv2.CAP_PROP_FOCUS)
BOX = FaceBox(200, 120, 220, 260, 0.95, (360, 200), (260, 200))


class Detector:
    def __init__(self, present: bool = True) -> None:
        self.present = present

    def detect_largest(self, bgr):
        return BOX if self.present else None


class FakeCamera:
    """Face brightness rises two grey levels per brightness unit, as measured."""

    def __init__(self, face: float = 42.0, *, brightness: bool = True, sharpest: int = 300):
        self.values: dict[int, float] = {AUTOFOCUS: 1.0, FOCUS: 100.0}
        if brightness:
            self.values[BRIGHTNESS] = 124.0
        self.originals: dict[int, float] = {}
        self.face, self.sharpest = face, sharpest
        self.rng = np.random.default_rng(1)

    def control(self, prop):
        return self.values.get(prop)

    def original(self, prop):
        return self.originals.get(prop, self.control(prop))

    def set_control(self, prop, value):
        if prop not in self.values or (prop == FOCUS and value < 120):
            return False
        self.originals.setdefault(prop, self.values[prop])
        self.values[prop] = float(value)
        return True

    def read_frame(self):
        offset = self.values.get(BRIGHTNESS, 124.0) - 124.0
        face = float(np.clip(self.face + 2 * offset, 0, 255))
        frame = np.full((480, 640, 3), 200, dtype=np.uint8)   # a bright window
        blur = abs(self.values[FOCUS] - self.sharpest) / 120.0
        texture = self.rng.normal(0, 40 * np.exp(-blur * blur) + 1, (260, 220, 1))
        frame[120:380, 200:420] = np.clip(face + texture, 0, 255).astype(np.uint8)
        return frame


class ExposureTests(unittest.TestCase):
    def test_a_backlit_face_is_brought_into_the_light(self) -> None:
        camera = FakeCamera(face=42)
        result = expose_for_face(camera, camera.read_frame(), Detector())
        self.assertTrue(result.adjusted)
        self.assertGreater(result.offset, 0)
        self.assertTrue(FACE_LOW <= result.face_after <= FACE_HIGH, result.face_after)
        self.assertLessEqual(result.steps, 3)

    def test_a_face_in_glare_is_toned_down(self) -> None:
        camera = FakeCamera(face=235)
        result = expose_for_face(camera, camera.read_frame(), Detector())
        self.assertLess(result.offset, 0)
        self.assertTrue(FACE_LOW <= result.face_after <= FACE_HIGH, result.face_after)

    def test_nothing_changes_when_nobody_is_there(self) -> None:
        camera = FakeCamera(face=42)
        result = expose_for_face(camera, camera.read_frame(), Detector(present=False))
        self.assertFalse(result.adjusted)
        self.assertEqual(camera.originals, {})

    def test_what_worked_before_in_this_light_is_tried_first(self) -> None:
        camera = FakeCamera(face=42)
        result = expose_for_face(camera, camera.read_frame(), Detector(),
                                 start_offset=lambda lighting: 40.0)
        self.assertEqual(result.steps, 1)
        self.assertEqual(result.offset, 40.0)

    def test_a_camera_without_a_brightness_control_is_left_alone(self) -> None:
        camera = FakeCamera(face=42, brightness=False)
        self.assertFalse(expose_for_face(camera, camera.read_frame(), Detector()).adjusted)


class FocusTests(unittest.TestCase):
    def test_finds_the_sharpest_lens_position_for_your_face(self) -> None:
        camera = FakeCamera(face=120, sharpest=300)
        result = calibrate_focus(camera, Detector())
        self.assertIsNotNone(result.best)
        self.assertLessEqual(abs(result.best - 300), 20)
        self.assertNotIn(100, result.sharpness)  # outside this lens's range

    def test_no_face_means_no_answer(self) -> None:
        self.assertIsNone(calibrate_focus(FakeCamera(), Detector(present=False)).best)


class FakeCapture:
    def __init__(self, **values: float) -> None:
        self.values = {BRIGHTNESS: 124.0, AUTOFOCUS: 1.0, FOCUS: 100.0, **values}

    def get(self, prop):
        return self.values.get(prop, -1.0)

    def set(self, prop, value):
        if prop not in self.values:
            return False
        self.values[prop] = value
        return True

    def release(self) -> None:
        pass


class ControlRestoreTests(unittest.TestCase):
    def tearDown(self) -> None:
        CONTROLS_MARKER.unlink(missing_ok=True)

    def test_every_change_is_undone_when_the_capture_closes(self) -> None:
        camera, capture = Camera(), FakeCapture()
        camera._cap = capture
        self.assertTrue(camera.set_control(BRIGHTNESS, 160))
        self.assertTrue(camera.fix_focus(250))
        self.assertTrue(CONTROLS_MARKER.exists())
        camera.close()
        self.assertEqual(capture.values, {BRIGHTNESS: 124.0, AUTOFOCUS: 1.0, FOCUS: 100.0})
        self.assertFalse(CONTROLS_MARKER.exists())

    def test_changes_left_by_a_crash_are_undone_by_the_next_capture(self) -> None:
        camera = Camera()
        camera._cap = FakeCapture()
        camera.set_control(BRIGHTNESS, 180)        # ...and the process dies here
        reopened = FakeCapture()
        reopened.values[BRIGHTNESS] = 180.0       # the camera kept the change
        _undo_leftover_changes(reopened)
        self.assertEqual(reopened.values[BRIGHTNESS], 124.0)
        self.assertFalse(CONTROLS_MARKER.exists())


if __name__ == "__main__":
    unittest.main()
