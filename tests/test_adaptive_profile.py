from __future__ import annotations

import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

import numpy as np

from face_service.adaptive import (
    is_safe_adaptive_candidate,
    select_diverse_adaptive_indices,
    select_rolling_adaptive_indices,
)
from face_service.config import Config
from face_service.recognizer import FrameAnalysis, Recognizer


def direction(degrees: float) -> np.ndarray:
    radians = np.deg2rad(degrees)
    return np.asarray([np.cos(radians), np.sin(radians), 0.0], dtype=np.float32)


class AdaptivePolicyTests(unittest.TestCase):
    def test_candidate_must_match_immutable_anchor_with_margin(self) -> None:
        protected = direction(0)[None, :]
        existing = protected.copy()
        self.assertTrue(
            is_safe_adaptive_candidate(
                direction(25), protected, existing,
                threshold=0.45, match_margin=0.10, min_diversity=0.02,
            )
        )
        self.assertFalse(
            is_safe_adaptive_candidate(
                direction(55), protected, existing,
                threshold=0.45, match_margin=0.10, min_diversity=0.02,
            )
        )

    def test_near_duplicate_is_rejected(self) -> None:
        protected = direction(0)[None, :]
        existing = np.vstack((protected, direction(25)))
        self.assertFalse(
            is_safe_adaptive_candidate(
                direction(26), protected, existing,
                threshold=0.45, match_margin=0.10, min_diversity=0.02,
            )
        )

    def test_selection_is_bounded_and_keeps_diverse_samples(self) -> None:
        protected = direction(0)[None, :]
        adaptive = np.vstack((direction(5), direction(20), direction(-20), direction(35)))
        chosen = select_diverse_adaptive_indices(
            protected,
            adaptive,
            np.asarray([1.0, 0.8, 0.8, 0.7]),
            np.asarray([1.0, 2.0, 3.0, 4.0]),
            2,
        )
        self.assertEqual(len(chosen), 2)
        self.assertIn(3, chosen)  # widest useful appearance variation
        self.assertNotIn(0, chosen)  # near-anchor duplicate is least useful

    def test_rolling_selection_expires_then_removes_oldest_first(self) -> None:
        adaptive = np.vstack(
            (direction(5), direction(10), direction(15), direction(20))
        )
        chosen = select_rolling_adaptive_indices(
            adaptive,
            np.asarray([0.9, 0.8, 0.7, 0.6]),
            np.asarray([10.0, 20.0, 30.0, 40.0]),
            limit=2,
            cutoff=15.0,
        )
        self.assertEqual(chosen, [2, 3])

    def test_rolling_selection_uses_quality_for_timestamp_ties(self) -> None:
        adaptive = np.vstack((direction(5), direction(10), direction(15)))
        chosen = select_rolling_adaptive_indices(
            adaptive,
            np.asarray([0.7, 0.9, 0.8]),
            np.asarray([50.0, 50.0, 50.0]),
            limit=2,
            cutoff=0.0,
        )
        self.assertEqual(chosen, [1, 2])


class AdaptiveTransactionTests(unittest.TestCase):
    def setUp(self) -> None:
        self.temp = tempfile.TemporaryDirectory()
        self.profile_path = Path(self.temp.name) / "embeddings.npz"
        self.path_patch = patch("face_service.recognizer.EMBED_PATH", self.profile_path)
        self.path_patch.start()
        self.cfg = Config(
            threshold=0.45,
            adaptive_enrollment=True,
            adaptive_samples_per_unlock=2,
            adaptive_max_embeddings=3,
            adaptive_retention_days=14.0,
            adaptive_match_margin=0.10,
            adaptive_min_diversity=0.02,
        )
        self.recognizer = Recognizer(self.cfg)
        anchors = direction(0)[None, :]
        qualities = np.ones(1, dtype=np.float32)
        created = np.zeros(1, dtype=np.float64)
        self.recognizer._atomic_save_profile(anchors, 1, qualities, created)
        self.recognizer._install_profile(anchors, 1, qualities, created)

    def tearDown(self) -> None:
        self.path_patch.stop()
        self.temp.cleanup()

    @staticmethod
    def analysis(angle: float, quality: float = 0.9) -> FrameAnalysis:
        return FrameAnalysis(
            match=True,
            distance=0.1,
            is_real=True,
            embedding=direction(angle),
            anchor_distance=0.1,
            quality=quality,
            face_count=1,
            adaptive_eligible=True,
        )

    def test_failed_windows_authentication_never_changes_profile(self) -> None:
        attempt = self.recognizer.stage_adaptive([self.analysis(25)])
        self.assertIsNotNone(attempt)
        self.assertEqual(self.recognizer.finish_adaptive(attempt or "", False), 0)
        self.assertEqual(self.recognizer.profile_counts(), (1, 0))

    def test_success_is_atomic_capped_and_manual_anchor_is_retained(self) -> None:
        for angle in (20, -20, 35):
            attempt = self.recognizer.stage_adaptive([self.analysis(angle)])
            self.assertIsNotNone(attempt)
            self.assertEqual(self.recognizer.finish_adaptive(attempt or "", True), 1)

        protected, adaptive = self.recognizer.profile_counts()
        self.assertEqual((protected, adaptive), (1, 3))
        with np.load(self.profile_path, allow_pickle=False) as profile:
            self.assertEqual(int(profile["protected_count"]), 1)
            np.testing.assert_allclose(profile["embeddings"][0], direction(0))
        self.assertEqual(list(self.profile_path.parent.glob("embeddings-*.npz")), [])

    def test_legacy_profile_is_loaded_as_all_protected(self) -> None:
        np.savez(self.profile_path, embeddings=np.vstack((direction(0), direction(10))))
        other = Recognizer(self.cfg)
        self.assertTrue(other.load())
        self.assertEqual(other.profile_counts(), (2, 0))

    def test_load_expires_adaptive_samples_but_never_manual_anchors(self) -> None:
        now = 2_000_000.0
        embeddings = np.vstack((direction(0), direction(10), direction(20)))
        qualities = np.ones(3, dtype=np.float32)
        created = np.asarray(
            [0.0, now - 15 * 86400.0, now - 10 * 86400.0], dtype=np.float64
        )
        self.recognizer._atomic_save_profile(embeddings, 1, qualities, created)

        other = Recognizer(self.cfg)
        with patch("face_service.recognizer.time.time", return_value=now):
            self.assertTrue(other.load())
        self.assertEqual(other.profile_counts(), (1, 1))
        with np.load(self.profile_path, allow_pickle=False) as profile:
            np.testing.assert_allclose(profile["embeddings"][0], direction(0))
            np.testing.assert_allclose(profile["embeddings"][1], direction(20))

    def test_missing_adaptive_timestamp_is_migrated_not_deleted(self) -> None:
        now = 2_000_000.0
        embeddings = np.vstack((direction(0), direction(10)))
        np.savez(
            self.profile_path,
            embeddings=embeddings,
            protected_count=np.asarray(1, dtype=np.int64),
            qualities=np.ones(2, dtype=np.float32),
        )
        other = Recognizer(self.cfg)
        with patch("face_service.recognizer.time.time", return_value=now):
            self.assertTrue(other.load())
        self.assertEqual(other.profile_counts(), (1, 1))
        with np.load(self.profile_path, allow_pickle=False) as profile:
            self.assertEqual(float(profile["created_at"][1]), now)

    def test_load_cap_keeps_newest_adaptive_samples(self) -> None:
        now = 2_000_000.0
        self.cfg.adaptive_max_embeddings = 2
        embeddings = np.vstack(
            (direction(0), direction(10), direction(20), direction(30))
        )
        qualities = np.ones(4, dtype=np.float32)
        created = np.asarray([0.0, now - 30.0, now - 20.0, now - 10.0])
        self.recognizer._atomic_save_profile(embeddings, 1, qualities, created)
        other = Recognizer(self.cfg)
        with patch("face_service.recognizer.time.time", return_value=now):
            self.assertTrue(other.load())
        self.assertEqual(other.profile_counts(), (1, 2))
        with np.load(self.profile_path, allow_pickle=False) as profile:
            np.testing.assert_allclose(profile["embeddings"][1], direction(20))
            np.testing.assert_allclose(profile["embeddings"][2], direction(30))


class AdaptiveConfigTests(unittest.TestCase):
    def test_unlock_vote_must_be_bounded(self) -> None:
        with self.assertRaises(ValueError):
            Config(unlock_verify_frames=2, unlock_verify_required=3).validate()

    def test_adaptive_margin_must_be_below_identity_threshold(self) -> None:
        with self.assertRaises(ValueError):
            Config(threshold=0.20, adaptive_match_margin=0.20).validate()

    def test_adaptive_retention_days_cannot_be_negative(self) -> None:
        with self.assertRaises(ValueError):
            Config(adaptive_retention_days=-1.0).validate()

    def test_pre_auth_buffer_cap_must_be_non_negative(self) -> None:
        with self.assertRaises(ValueError):
            Config(adaptive_pre_auth_max_frames=-1).validate()

    def test_pre_auth_ttl_is_bounded(self) -> None:
        with self.assertRaises(ValueError):
            Config(adaptive_pre_auth_ttl_s=301.0).validate()


if __name__ == "__main__":
    unittest.main()
