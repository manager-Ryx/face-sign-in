from __future__ import annotations

import unittest
from types import SimpleNamespace
from unittest.mock import patch

import _isolation  # noqa: F401
import cv2

from face_service.camera import (
    Camera,
    CameraAmbiguousError,
    CameraBusyError,
    CameraMissingError,
    CameraMovedError,
    resolve,
)

PINNED = r"\\?\usb#vid_0625&pid_a3a6&mi_02#6&3c4d5e6f&0&0002"
SAME_MODEL_OTHER_PORT = r"\\?\usb#vid_0625&pid_a3a6&mi_02#7&11111111&0&0001"
BROADCAST = r"\\?\root#nvidia_broadcast#0000"


def device(index: int, path: str, name: str = "NearStream V02AF") -> SimpleNamespace:
    return SimpleNamespace(index=index, path=path, name=name)


def enumerate_by_backend(dshow: list, msmf: list):
    return lambda backend: dshow if backend == cv2.CAP_DSHOW else msmf


class CameraIdentityTests(unittest.TestCase):
    def test_reordered_camera_uses_its_new_index_in_each_backend(self) -> None:
        dshow = [device(0, BROADCAST, "NVIDIA Broadcast"), device(2, PINNED + "#{dshow}")]
        msmf = [device(1, PINNED.upper() + "#{msmf}")]
        with patch("cv2_enumerate_cameras.enumerate_cameras",
                   side_effect=enumerate_by_backend(dshow, msmf)):
            self.assertEqual(resolve(PINNED), [(2, cv2.CAP_DSHOW), (1, cv2.CAP_MSMF)])

    def test_missing_camera_never_opens_another_camera(self) -> None:
        with patch("cv2_enumerate_cameras.enumerate_cameras",
                   return_value=[device(0, BROADCAST, "NVIDIA Broadcast")]), \
                patch("face_service.camera.cv2.VideoCapture") as capture:
            with self.assertRaisesRegex(CameraMissingError, "disconnected"):
                Camera(device_id=PINNED).open()
            capture.assert_not_called()

    def test_same_model_on_another_port_is_reported_not_used(self) -> None:
        with patch("cv2_enumerate_cameras.enumerate_cameras",
                   return_value=[device(0, SAME_MODEL_OTHER_PORT)]), \
                patch("face_service.camera.cv2.VideoCapture") as capture:
            with self.assertRaises(CameraMovedError) as raised:
                Camera(device_id=PINNED).open()
            capture.assert_not_called()
        self.assertEqual(raised.exception.detail["path"], SAME_MODEL_OTHER_PORT)

    def test_two_identical_models_on_other_ports_are_ambiguous(self) -> None:
        other = SAME_MODEL_OTHER_PORT.replace("0001", "0003")
        with patch("cv2_enumerate_cameras.enumerate_cameras",
                   return_value=[device(0, SAME_MODEL_OTHER_PORT), device(1, other)]), \
                self.assertRaises(CameraAmbiguousError):
            resolve(PINNED)

    def test_duplicate_identity_is_rejected(self) -> None:
        with patch("cv2_enumerate_cameras.enumerate_cameras",
                   return_value=[device(0, PINNED), device(1, PINNED)]), \
                self.assertRaisesRegex(CameraAmbiguousError, "ambiguous"):
            resolve(PINNED)

    def test_config_is_shared(self) -> None:
        cfg = SimpleNamespace(camera_index=3, camera_warmup_frames=4, camera_device_id=PINNED)
        cam = Camera.from_config(cfg)
        self.assertEqual((cam.index, cam.warmup_frames, cam.device_id), (3, 4, PINNED))

    def test_slow_backend_is_skipped_when_directshow_sees_the_camera(self) -> None:
        dshow = [device(0, PINNED)]
        msmf = [device(0, PINNED)]
        opened = []

        class FakeCapture:
            def __init__(self, index, backend):
                opened.append(backend)

            def isOpened(self):
                return False

            def release(self):
                pass

        with patch("cv2_enumerate_cameras.enumerate_cameras",
                   side_effect=enumerate_by_backend(dshow, msmf)), \
                patch("face_service.camera.cv2.VideoCapture", FakeCapture), \
                patch("face_service.camera.time.sleep"), self.assertRaises(CameraBusyError):
            Camera(device_id=PINNED, open_budget_s=5).open()
        self.assertTrue(opened)
        self.assertTrue(all(b == cv2.CAP_DSHOW for b in opened))


if __name__ == "__main__":
    unittest.main()
