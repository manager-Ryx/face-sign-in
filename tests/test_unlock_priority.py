from __future__ import annotations

import unittest
from unittest.mock import patch

from face_service.config import Config
from face_service.service import FaceService, UNLOCK_PRIORITY_GRACE_S


class UnlockPriorityTests(unittest.TestCase):
    def setUp(self) -> None:
        self.service = FaceService(Config(presence_mode="recognition"))

    def test_unlock_request_starts_priority_window(self) -> None:
        with (
            patch("face_service.service.time.monotonic", return_value=100.0),
            patch.object(
                self.service,
                "_capture_and_verify",
                return_value=(False, 1.0, False, []),
            ),
        ):
            response = self.service._handle({"cmd": "unlock"})

        self.assertFalse(response["ok"])
        self.assertEqual(
            self.service._unlock_priority_until,
            100.0 + UNLOCK_PRIORITY_GRACE_S,
        )

    def test_presence_scan_is_skipped_during_unlock_priority_window(self) -> None:
        self.service._unlock_priority_until = 135.0
        with (
            patch("face_service.service.time.monotonic", return_value=120.0),
            patch.object(self.service, "_presence_probe") as probe,
        ):
            response = self.service._handle({"cmd": "presence"})

        probe.assert_not_called()
        self.assertTrue(response["present"])
        self.assertEqual(response["skipped"], "unlock-active")

    def test_presence_scan_resumes_after_priority_window(self) -> None:
        self.service._unlock_priority_until = 135.0
        with (
            patch("face_service.service.time.monotonic", return_value=135.0),
            patch.object(
                self.service, "_presence_probe", return_value=(False, False)
            ) as probe,
        ):
            response = self.service._handle({"cmd": "presence"})

        probe.assert_called_once_with()
        self.assertFalse(response["present"])
        self.assertNotIn("skipped", response)


if __name__ == "__main__":
    unittest.main()
