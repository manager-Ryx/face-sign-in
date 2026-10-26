"""Profile storage and successful-login-only learning."""
from __future__ import annotations

import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

import _isolation  # noqa: F401
import numpy as np
from helpers import DARK, FakeEmbedder, direction, frame

from face_service.adaptive import (
    LearningCandidate,
    choose_learning_sample,
    is_safe_adaptive_candidate,
    select_retained,
)
from face_service.config import Config
from face_service.metrics import NullMetrics
from face_service.profile import (
    SOURCE_UNLOCK,
    FaceProfile,
    load_profile,
    normalize_rows,
    save_profile,
)
from face_service.recognizer import Outcome, Recognizer


def anchors_profile(*angles: float) -> FaceProfile:
    return FaceProfile.build(np.stack([direction(a) for a in angles]),
                             protected_count=len(angles), created_at=np.zeros(len(angles)))


class AdmissionPolicyTests(unittest.TestCase):
    def test_candidate_must_match_an_original_anchor_with_margin(self) -> None:
        protected = direction(0)[None, :]
        self.assertTrue(is_safe_adaptive_candidate(
            direction(25), protected, protected, threshold=0.45, match_margin=0.10,
            min_diversity=0.02))
        self.assertFalse(is_safe_adaptive_candidate(
            direction(55), protected, protected, threshold=0.45, match_margin=0.10,
            min_diversity=0.02))

    def test_near_duplicate_is_rejected(self) -> None:
        protected = direction(0)[None, :]
        existing = np.vstack((protected, direction(25)))
        self.assertFalse(is_safe_adaptive_candidate(
            direction(26), protected, existing, threshold=0.45, match_margin=0.10,
            min_diversity=0.02))

    def test_learned_samples_cannot_validate_each_other(self) -> None:
        # A sample far from every anchor is refused even if it sits right next
        # to an earlier learned sample: no chain of drifting samples.
        profile = anchors_profile(0)
        learned, _ = profile.append(direction(40)[None, :], source=SOURCE_UNLOCK, created_at=1.0,
                                    qualities=np.ones(1), lighting=np.zeros(1, dtype=np.int8))
        candidate = LearningCandidate(direction(60), quality=0.9)
        chosen = choose_learning_sample(
            [candidate], learned.unit_rows[:1], learned.unit_rows, threshold=0.45,
            match_margin=0.10, min_diversity=0.02, min_face_confidence=0.9,
            min_face_ratio=0.04, min_sharpness=40.0)
        self.assertIsNone(chosen)
        self.assertEqual(candidate.rejected, "not close enough to your enrollment")

    def test_best_frame_is_chosen_and_poor_frames_rejected(self) -> None:
        profile = anchors_profile(0)
        blurry = LearningCandidate(direction(20), quality=0.95, sharpness=5.0)
        modest = LearningCandidate(direction(12), quality=0.6)
        better = LearningCandidate(direction(22), quality=0.8)
        two_faces = LearningCandidate(direction(18), quality=0.9, face_count=2)
        chosen = choose_learning_sample(
            [blurry, modest, better, two_faces], profile.unit_rows, profile.unit_rows,
            threshold=0.45, match_margin=0.10, min_diversity=0.02, min_face_confidence=0.9,
            min_face_ratio=0.04, min_sharpness=40.0)
        self.assertIs(chosen, better)
        self.assertEqual(blurry.rejected, "image quality too low")
        self.assertEqual(two_faces.rejected, "more than one face")


class RetentionTests(unittest.TestCase):
    def test_age_expiry_then_cap(self) -> None:
        learned = np.stack([direction(a) for a in (5, 10, 15, 20)])
        keep = select_retained(learned, direction(0)[None, :], np.asarray([10., 20., 30., 40.]),
                               np.ones(4), limit=2, cutoff=15.0)
        self.assertEqual(keep, [2, 3])

    def test_redundant_sample_is_evicted_before_a_diverse_one(self) -> None:
        # 2 and 2.5 degrees are near-duplicates; 30 and -30 are distinct looks.
        learned = np.stack([direction(a) for a in (30, 2, 2.5, -30)])
        keep = select_retained(learned, direction(0)[None, :], np.asarray([1., 2., 3., 4.]),
                               np.ones(4), limit=3, recent_keep=1)
        self.assertEqual(keep, [0, 2, 3])

    def test_newest_samples_are_always_kept(self) -> None:
        learned = np.stack([direction(a) for a in (30, -30, 1)])
        keep = select_retained(learned, direction(0)[None, :], np.asarray([1., 2., 3.]),
                               np.ones(3), limit=2, recent_keep=1)
        self.assertIn(2, keep)  # newest, despite being redundant with the anchor

    def test_large_reduction_is_fast_and_bounded(self) -> None:
        rng = np.random.default_rng(1)
        learned = rng.normal(size=(800, 64)).astype(np.float32)
        keep = select_retained(learned, rng.normal(size=(30, 64)), np.arange(800.0),
                               np.ones(800), limit=100, recent_keep=20)
        self.assertEqual(len(keep), 100)
        self.assertTrue(set(range(780, 800)) <= set(keep))


class ProfileStorageTests(unittest.TestCase):
    def setUp(self) -> None:
        self.dir = tempfile.TemporaryDirectory()
        self.path = Path(self.dir.name) / "embeddings.npz"

    def tearDown(self) -> None:
        self.dir.cleanup()

    def test_round_trip_and_atomic_write(self) -> None:
        profile = anchors_profile(0, 10).with_anchor_files(("a.jpg", "b.jpg"))
        profile, ids = profile.append(direction(20)[None, :], source=SOURCE_UNLOCK,
                                      created_at=5.0, qualities=np.asarray([0.8]),
                                      lighting=np.asarray([2], dtype=np.int8))
        save_profile(profile, self.path)
        loaded = load_profile(self.path)
        assert loaded is not None
        self.assertEqual(loaded.protected_count, 2)
        self.assertEqual(loaded.learned_count, 1)
        self.assertEqual(loaded.anchor_files, ("a.jpg", "b.jpg"))
        self.assertEqual(int(loaded.sample_ids[-1]), ids[0])
        self.assertEqual(list(self.path.parent.glob("embeddings-*.npz")), [])

    def test_legacy_profile_is_loaded_as_all_protected(self) -> None:
        np.savez(self.path, embeddings=np.vstack((direction(0), direction(10))))
        loaded = load_profile(self.path)
        assert loaded is not None
        self.assertEqual((loaded.protected_count, loaded.learned_count), (2, 0))

    def test_version_three_profile_migrates(self) -> None:
        np.savez(self.path, embeddings=np.vstack((direction(0), direction(10), direction(20))),
                 protected_count=np.asarray(2), qualities=np.ones(3, dtype=np.float32),
                 created_at=np.asarray([0.0, 0.0, 99.0]), profile_version=np.asarray(3))
        loaded = load_profile(self.path)
        assert loaded is not None
        self.assertEqual(loaded.learned_count, 1)
        self.assertEqual(list(loaded.sample_ids), [1, 2, 3])

    def test_anchors_cannot_be_removed_by_id(self) -> None:
        profile = anchors_profile(0, 10)
        trimmed = profile.without_ids({1, 2})
        self.assertEqual(trimmed.protected_count, 2)
        np.testing.assert_allclose(normalize_rows(trimmed.embeddings), profile.unit_rows)


class LearningTransactionTests(unittest.TestCase):
    def setUp(self) -> None:
        self.dir = tempfile.TemporaryDirectory()
        self.path = Path(self.dir.name) / "embeddings.npz"
        self.cfg = Config(threshold=0.45, adaptive_enrollment=True, adaptive_max_embeddings=3,
                          adaptive_min_diversity=0.02, adaptive_min_blur=40.0)
        self.metrics = NullMetrics()
        self.recognizer = Recognizer(self.cfg, embedder=FakeEmbedder(),
                                     profile_path=self.path, metrics=self.metrics)
        save_profile(anchors_profile(0), self.path)
        self.recognizer.load()

    def tearDown(self) -> None:
        self.dir.cleanup()

    def success(self, *angles: float) -> list:
        return [frame(Outcome.MATCH, 0.1, i * 0.2, embedding=direction(a))
                for i, a in enumerate(angles)]

    def test_only_one_sample_per_unlock_and_only_after_confirmation(self) -> None:
        attempt = self.recognizer.stage_learning(self.success(20, 25, -20))
        self.assertIsNotNone(attempt)
        self.assertEqual(self.recognizer.profile_counts(), (1, 0))  # nothing saved yet
        result = self.recognizer.finish_learning(attempt or "", True)
        self.assertEqual(result.added, 1)
        self.assertEqual(self.recognizer.profile_counts(), (1, 1))
        self.assertEqual(load_profile(self.path).learned_count, 1)  # type: ignore[union-attr]

    def test_failed_windows_sign_in_never_changes_the_profile(self) -> None:
        attempt = self.recognizer.stage_learning(self.success(20))
        self.assertEqual(self.recognizer.finish_learning(attempt or "", False).added, 0)
        self.assertEqual(self.recognizer.profile_counts(), (1, 0))

    def test_repeated_report_cannot_add_twice(self) -> None:
        attempt = self.recognizer.stage_learning(self.success(20))
        self.assertEqual(self.recognizer.finish_learning(attempt or "", True).added, 1)
        self.assertEqual(self.recognizer.finish_learning(attempt or "", True).added, 0)
        self.assertEqual(self.recognizer.profile_counts(), (1, 1))

    def test_missing_report_expires_without_change(self) -> None:
        clock = [1000.0]
        self.recognizer._monotonic = lambda: clock[0]
        attempt = self.recognizer.stage_learning(self.success(20))
        clock[0] += 121.0
        self.assertEqual(self.recognizer.finish_learning(attempt or "", True).added, 0)
        self.assertEqual(self.recognizer.profile_counts(), (1, 0))

    def test_unknown_attempt_is_ignored(self) -> None:
        self.assertEqual(self.recognizer.finish_learning("forged", True).added, 0)

    def test_failed_and_unusable_frames_never_stage(self) -> None:
        frames = [frame(Outcome.UNCERTAIN, 0.5, embedding=direction(30)),
                  frame(Outcome.UNUSABLE, 0.6, quality=DARK, embedding=direction(31))]
        self.assertIsNone(self.recognizer.stage_learning(frames))

    def test_dark_matching_frame_is_not_learned(self) -> None:
        frames = [frame(Outcome.MATCH, 0.2, quality=DARK, embedding=direction(20))]
        self.assertIsNone(self.recognizer.stage_learning(frames))

    def test_learning_disabled_stages_nothing(self) -> None:
        self.cfg.adaptive_enrollment = False
        self.assertIsNone(self.recognizer.stage_learning(self.success(20)))

    def test_cap_is_enforced_and_anchor_retained(self) -> None:
        for base in (20, -20, 40, -40):
            attempt = self.recognizer.stage_learning(self.success(base))
            self.recognizer.finish_learning(attempt or "", True)
        anchors, learned = self.recognizer.profile_counts()
        self.assertEqual((anchors, learned), (1, 3))
        np.testing.assert_allclose(self.recognizer.profile.unit_rows[0], direction(0), atol=1e-6)

    def test_undo_and_reset_keep_original_enrollment(self) -> None:
        for base in (20, -20):
            attempt = self.recognizer.stage_learning(self.success(base))
            self.recognizer.finish_learning(attempt or "", True)
        ids = [s["id"] for s in self.recognizer.learned_samples()]
        self.assertEqual(self.recognizer.undo_learning(sample_ids=ids[:1]), 1)
        self.assertEqual(self.recognizer.reset_learning(), 1)
        self.assertEqual(self.recognizer.profile_counts(), (1, 0))

    def test_profile_change_between_stage_and_commit_is_rechecked(self) -> None:
        first = self.recognizer.stage_learning(self.success(20))
        second = self.recognizer.stage_learning(self.success(20.5))
        self.assertEqual(self.recognizer.finish_learning(first or "", True).added, 1)
        # The second staged sample is now a near-duplicate of the first.
        self.assertEqual(self.recognizer.finish_learning(second or "", True).added, 0)

    def test_retention_on_load_never_removes_anchors(self) -> None:
        now = 2_000_000.0
        profile = FaceProfile.build(
            np.stack([direction(0), direction(10), direction(20)]), protected_count=1,
            created_at=np.asarray([0.0, now - 15 * 86400, now - 10 * 86400]))
        save_profile(profile, self.path)
        self.cfg.adaptive_retention_days = 14.0
        other = Recognizer(self.cfg, embedder=FakeEmbedder(), profile_path=self.path,
                           clock=lambda: now)
        self.assertTrue(other.load())
        self.assertEqual(other.profile_counts(), (1, 1))

    def test_missing_timestamps_are_migrated_not_deleted(self) -> None:
        profile = FaceProfile.build(np.stack([direction(0), direction(10)]), protected_count=1)
        save_profile(profile, self.path)
        self.cfg.adaptive_retention_days = 14.0
        other = Recognizer(self.cfg, embedder=FakeEmbedder(), profile_path=self.path,
                           clock=lambda: 2_000_000.0)
        other.load()
        self.assertEqual(other.profile_counts(), (1, 1))
        self.assertEqual(float(other.profile.created_at[1]), 2_000_000.0)

    def test_keep_photos_is_off_by_default(self) -> None:
        with patch("face_service.recognizer.cv2.imwrite") as write:
            attempt = self.recognizer.stage_learning(self.success(20))
            self.recognizer.finish_learning(attempt or "", True)
            write.assert_not_called()


class ImageEnrollmentTests(unittest.TestCase):
    def setUp(self) -> None:
        self.dir = tempfile.TemporaryDirectory()
        self.root = Path(self.dir.name)
        self.path = self.root / "embeddings.npz"

    def tearDown(self) -> None:
        self.dir.cleanup()

    def photos(self, keys: list[int]) -> list[Path]:
        import cv2
        from helpers import image
        paths = []
        for key in keys:
            path = self.root / f"p{key}.png"
            cv2.imwrite(str(path), image(key))
            paths.append(path)
        return paths

    def test_replace_then_append_keeps_learned_samples(self) -> None:
        embedder = FakeEmbedder({k: float(k) for k in range(1, 40)})
        recognizer = Recognizer(Config(adaptive_enrollment=True), embedder=embedder,
                                profile_path=self.path)
        report = recognizer.build_profile(self.photos([1, 2, 3]), mode="replace")
        self.assertEqual((report.accepted, report.anchors, report.learned), (3, 3, 0))
        profile = recognizer.profile
        learned, _ = profile.append(direction(8)[None, :], source=SOURCE_UNLOCK, created_at=1.0,
                                    qualities=np.ones(1), lighting=np.zeros(1, dtype=np.int8))
        recognizer._profile = learned
        report = recognizer.build_profile(self.photos([4, 5]), mode="append")
        self.assertEqual((report.anchors, report.learned), (5, 1))
        self.assertLess(report.consistency, 0.05)

    def test_dry_run_saves_nothing(self) -> None:
        embedder = FakeEmbedder({1: 1.0})
        recognizer = Recognizer(Config(), embedder=embedder, profile_path=self.path)
        recognizer.build_profile(self.photos([1]), mode="replace", dry_run=True)
        self.assertFalse(self.path.exists())

    def test_photo_without_a_face_is_reported(self) -> None:
        embedder = FakeEmbedder({1: 1.0})
        recognizer = Recognizer(Config(), embedder=embedder, profile_path=self.path)
        report = recognizer.build_profile(self.photos([1, 99]), mode="replace")
        reasons = {r.name: r.reason for r in report.results if not r.accepted}
        self.assertEqual(reasons, {"p99.png": "no clear face"})

    def test_holdout_photos_are_not_in_the_profile(self) -> None:
        embedder = FakeEmbedder({k: float(k) for k in range(1, 30)})
        recognizer = Recognizer(Config(), embedder=embedder, profile_path=self.path)
        report = recognizer.build_profile(self.photos(list(range(1, 15))), mode="replace",
                                          holdout_every=7)
        self.assertEqual(len(report.holdout), 2)
        self.assertEqual(report.anchors, 12)


if __name__ == "__main__":
    unittest.main()
