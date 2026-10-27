"""Threshold evaluation, bounded adjustments, rollback, and snapshots."""
from __future__ import annotations

import tempfile
import unittest
from pathlib import Path

import _isolation  # noqa: F401
import numpy as np

from face_service.adaptation import PROBATION_SESSIONS, AdaptationState
from face_service.config import Config
from face_service.evaluation import FLOOR, MAX_STEP, GenuineSample, evaluate_threshold, wilson_lower


def samples(distances, *, days: int = 6, lighting=("bright", "dim")) -> list[GenuineSample]:
    out = []
    for i, d in enumerate(distances):
        out.append(GenuineSample(float(d), f"2026-10-{1 + i % days:02d}",
                                 lighting[i % len(lighting)], "check"))
    return out


class EvaluationStoreTests(unittest.TestCase):
    def test_a_damaged_file_starts_fresh_instead_of_stopping_the_service(self) -> None:
        import tempfile
        from pathlib import Path

        from face_service.evaluation_store import EvaluationStore
        with tempfile.TemporaryDirectory() as folder:
            path = Path(folder) / "evaluation.npz"
            for damage in (b"", b"PK\x03\x04 cut short"):
                path.write_bytes(damage)
                self.assertEqual(len(EvaluationStore(path)), 0)


class EvaluationTests(unittest.TestCase):
    def test_wilson_lower_bound_is_conservative(self) -> None:
        self.assertLess(wilson_lower(10, 10), 1.0)
        self.assertAlmostEqual(wilson_lower(0, 0), 0.0)
        self.assertGreater(wilson_lower(95, 100), 0.88)

    def test_insufficient_evidence_never_recommends_a_change(self) -> None:
        report = evaluate_threshold(samples([0.2] * 20, days=2), current=0.5, baseline=0.5)
        self.assertEqual(report.status, "insufficient")
        self.assertEqual(report.recommended, 0.5)
        self.assertTrue(report.evidence.missing())

    def test_single_lighting_condition_is_not_enough(self) -> None:
        report = evaluate_threshold(samples([0.2] * 80, lighting=("bright",)), current=0.5,
                                    baseline=0.5)
        self.assertEqual(report.status, "insufficient")

    def test_strong_evidence_tightens_by_one_small_step_only(self) -> None:
        rng = np.random.default_rng(0)
        report = evaluate_threshold(samples(rng.uniform(0.15, 0.30, 120)), current=0.5,
                                    baseline=0.5)
        self.assertEqual(report.status, "tighten")
        self.assertAlmostEqual(report.recommended, 0.5 - MAX_STEP)
        self.assertIn("cannot measure how often someone else", report.explanation)

    def test_borderline_evidence_keeps_the_current_setting(self) -> None:
        # Many genuine frames sit just inside the current setting.
        rng = np.random.default_rng(1)
        report = evaluate_threshold(samples(rng.uniform(0.44, 0.50, 120)), current=0.5,
                                    baseline=0.5)
        self.assertEqual(report.status, "keep")

    def test_poor_recognition_is_never_answered_by_loosening(self) -> None:
        rng = np.random.default_rng(2)
        report = evaluate_threshold(samples(rng.uniform(0.40, 0.70, 120)), current=0.5,
                                    baseline=0.5)
        self.assertEqual(report.status, "keep")
        self.assertLessEqual(report.recommended, 0.5)
        self.assertIn("current appearance", report.explanation)

    def test_never_below_the_floor(self) -> None:
        report = evaluate_threshold(samples([0.05] * 120), current=FLOOR + 0.01, baseline=0.5)
        self.assertGreaterEqual(report.recommended, FLOOR)

    def test_replayed_attempts_must_also_succeed(self) -> None:
        rng = np.random.default_rng(3)
        frames = samples(rng.uniform(0.15, 0.30, 120))
        # Recorded checks where only one frame in five would match at 0.48.
        sequences = [[0.2, 0.49, 0.49, 0.49, 0.49]] * 12
        report = evaluate_threshold(frames, current=0.5, baseline=0.5, sequences=sequences)
        self.assertEqual(report.status, "keep")


class AdaptationStateTests(unittest.TestCase):
    def setUp(self) -> None:
        self.dir = tempfile.TemporaryDirectory()
        self.state = AdaptationState(Path(self.dir.name) / "adaptation.json")
        self.cfg = Config(threshold=0.5)

    def tearDown(self) -> None:
        self.dir.cleanup()

    def test_only_stricter_values_can_be_applied(self) -> None:
        with self.assertRaises(ValueError):
            self.state.apply_threshold(0.55, cfg=self.cfg, reason="x", evidence={}, by="automatic",
                                       session_rate=0.9)
        self.state.apply_threshold(0.48, cfg=self.cfg, reason="x", evidence={}, by="automatic",
                                   session_rate=0.9)
        self.assertEqual(self.state.effective_threshold(self.cfg), 0.48)

    def test_user_setting_always_caps_the_effective_value(self) -> None:
        self.state.apply_threshold(0.48, cfg=self.cfg, reason="x", evidence={}, by="automatic",
                                   session_rate=0.9)
        self.assertEqual(self.state.effective_threshold(Config(threshold=0.45)), 0.45)

    def test_worse_reliability_rolls_back_automatically(self) -> None:
        self.state.apply_threshold(0.48, cfg=self.cfg, reason="x", evidence={}, by="automatic",
                                   session_rate=0.95)
        result = None
        for i in range(PROBATION_SESSIONS):
            result = self.state.record_session(face_success=i % 2 == 0)
        self.assertTrue(result and result["rolled_back"])
        self.assertEqual(self.state.effective_threshold(self.cfg), 0.5)
        statuses = [e["status"] for e in self.state.ledger()]
        self.assertIn("rolled-back", statuses)

    def test_good_reliability_keeps_the_change(self) -> None:
        self.state.apply_threshold(0.48, cfg=self.cfg, reason="x", evidence={}, by="automatic",
                                   session_rate=0.9)
        result = None
        for _ in range(PROBATION_SESSIONS):
            result = self.state.record_session(face_success=True)
        self.assertFalse(result["rolled_back"])
        self.assertEqual(self.state.effective_threshold(self.cfg), 0.48)

    def test_snapshot_restore(self) -> None:
        self.state.take_snapshot(Config(presence_interval_s=120), "before")
        restored = self.state.restore_snapshot(0)
        self.assertEqual(restored.presence_interval_s, 120)
        self.assertEqual(self.state.ledger()[0]["setting"], "settings")

    def test_state_survives_restart(self) -> None:
        self.state.apply_threshold(0.48, cfg=self.cfg, reason="x", evidence={}, by="you",
                                   session_rate=None)
        again = AdaptationState(self.state.path)
        self.assertEqual(again.effective_threshold(self.cfg), 0.48)

    def test_clearing_returns_to_the_users_setting(self) -> None:
        self.state.apply_threshold(0.48, cfg=self.cfg, reason="x", evidence={}, by="you",
                                   session_rate=None)
        self.state.clear_tuned_threshold(reason="undo", by="you")
        self.assertEqual(self.state.effective_threshold(self.cfg), 0.5)


if __name__ == "__main__":
    unittest.main()
