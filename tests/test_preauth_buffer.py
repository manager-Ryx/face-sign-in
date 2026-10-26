from __future__ import annotations

import unittest
from unittest.mock import patch

import numpy as np

from face_service.config import Config
from face_service.recognizer import FrameAnalysis
from face_service.service import FaceService


def analysis() -> FrameAnalysis:
    return FrameAnalysis(
        match=True,
        distance=0.20,
        is_real=True,
        embedding=np.asarray([1.0, 0.0, 0.0], dtype=np.float32),
        anchor_distance=0.20,
        quality=0.9,
        face_count=1,
        adaptive_eligible=True,
    )


class PreAuthBufferTests(unittest.TestCase):
    def make_service(self) -> FaceService:
        return FaceService(
            Config(
                adaptive_enrollment=True,
                adaptive_pre_auth_enabled=True,
                adaptive_pre_auth_max_frames=2,
                adaptive_pre_auth_ttl_s=45.0,
            )
        )

    def test_failed_vote_candidates_are_bounded_and_consumed_on_success(self) -> None:
        service = self.make_service()
        first, second, third = analysis(), analysis(), analysis()
        with patch("face_service.service.time.monotonic", return_value=100.0):
            service._remember_preauth_candidates([first, second, third])
            self.assertEqual(len(service._preauth_candidates), 2)
            pool = service._consume_preauth_candidates([analysis()])

        self.assertEqual(len(pool), 3)
        self.assertEqual(service._preauth_candidates, [])

    def test_expired_candidates_are_discarded(self) -> None:
        service = self.make_service()
        with patch("face_service.service.time.monotonic", return_value=100.0):
            service._remember_preauth_candidates([analysis()])
        with patch("face_service.service.time.monotonic", return_value=146.0):
            service._prune_preauth_candidates()
        self.assertEqual(service._preauth_candidates, [])

    def test_disabled_buffer_never_retains_candidates(self) -> None:
        service = FaceService(
            Config(
                adaptive_enrollment=True,
                adaptive_pre_auth_enabled=False,
            )
        )
        service._remember_preauth_candidates([analysis()])
        self.assertEqual(service._preauth_candidates, [])


if __name__ == "__main__":
    unittest.main()
