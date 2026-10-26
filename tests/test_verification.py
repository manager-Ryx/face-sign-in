"""Progressive frame selection and bounded attempts."""
from __future__ import annotations

import unittest
from types import SimpleNamespace

import _isolation  # noqa: F401
from helpers import DARK, frame

from face_service.config import Config
from face_service.recognizer import Outcome
from face_service.verification import Decision, VerifyPolicy, evaluate, run

M, U, X, N, B = Outcome.MATCH, Outcome.UNCERTAIN, Outcome.MISMATCH, Outcome.NO_FACE, Outcome.UNUSABLE
PROGRESSIVE = VerifyPolicy.for_unlock(Config())
FIXED = VerifyPolicy.for_unlock(Config(unlock_strategy="fixed"))


def frames(*outcomes: Outcome, gap: float = 0.15) -> list:
    return [frame(o, 0.3 if o is M else 0.55 if o is U else 0.8, i * gap,
                  quality=DARK if o is B else frame(M).quality)
            for i, o in enumerate(outcomes)]


class ProgressivePolicyTests(unittest.TestCase):
    def decide(self, *outcomes: Outcome, policy: VerifyPolicy = PROGRESSIVE, gap: float = 0.15):
        return evaluate(frames(*outcomes, gap=gap), policy)

    def test_two_matches_unlock_after_two_frames(self) -> None:
        step = self.decide(M, M)
        self.assertEqual((step.decision, step.reason), (Decision.ACCEPT, "match"))

    def test_one_frame_alone_never_unlocks_by_default(self) -> None:
        self.assertEqual(self.decide(M).decision, Decision.CONTINUE)

    def test_adjacent_frames_do_not_count_as_two_confirmations(self) -> None:
        self.assertEqual(self.decide(M, M, gap=0.03).decision, Decision.CONTINUE)

    def test_nobody_there_stops_after_three_cheap_frames(self) -> None:
        step = self.decide(N, N, N)
        self.assertEqual((step.decision, step.reason), (Decision.REJECT, "no_face"))

    def test_clear_mismatch_stops_after_two_frames(self) -> None:
        step = self.decide(X, X)
        self.assertEqual((step.decision, step.reason), (Decision.REJECT, "mismatch"))

    def test_uncertain_expands_up_to_five(self) -> None:
        for count in range(1, 4):
            self.assertEqual(self.decide(*([U] * count)).decision, Decision.CONTINUE)
        step = self.decide(U, U, U, U)
        self.assertEqual((step.decision, step.reason), (Decision.REJECT, "uncertain"))

    def test_uncertain_then_two_matches_unlocks_within_five(self) -> None:
        self.assertEqual(self.decide(U, M, U, M).decision, Decision.ACCEPT)

    def test_dark_frames_are_unusable_not_mismatches(self) -> None:
        step = self.decide(B, B, B, B, B)
        self.assertEqual((step.decision, step.reason), (Decision.REJECT, "unusable"))

    def test_rejects_once_success_becomes_impossible(self) -> None:
        step = self.decide(U, U, U, U)  # one frame left, two matches needed
        self.assertEqual(step.decision, Decision.REJECT)

    def test_spoof_rejects_immediately(self) -> None:
        step = evaluate([frame(Outcome.SPOOF)], PROGRESSIVE)
        self.assertEqual((step.decision, step.reason), (Decision.REJECT, "spoof"))

    def test_fixed_policy_reproduces_two_of_three(self) -> None:
        self.assertEqual(self.decide(M, M, policy=FIXED, gap=0.0).decision, Decision.ACCEPT)
        self.assertEqual(self.decide(M, X, M, policy=FIXED, gap=0.0).decision, Decision.ACCEPT)
        self.assertEqual(self.decide(N, policy=FIXED).decision, Decision.CONTINUE)
        # Two matches can no longer be reached, so it stops without a third frame.
        self.assertEqual(self.decide(N, X, policy=FIXED).decision, Decision.REJECT)

    def test_single_frame_rule_only_when_enabled(self) -> None:
        strict = VerifyPolicy(single_frame_distance=0.2)
        self.assertEqual(evaluate([frame(M, 0.15)], strict).decision, Decision.ACCEPT)
        self.assertEqual(evaluate([frame(M, 0.25)], strict).decision, Decision.CONTINUE)


class FakeSource:
    def __init__(self, outcomes: list[Outcome]):
        self.outcomes = outcomes
        self.reads = 0

    def read_frame(self):
        self.reads += 1
        return self.reads


class FakeRecognizer:
    def __init__(self, outcomes: list[Outcome]):
        self.outcomes = list(outcomes)
        self.calls = 0

    def analyze(self, image, *, captured_at, threshold, uncertain_band, keep_crop):
        outcome = self.outcomes[min(self.calls, len(self.outcomes) - 1)]
        self.calls += 1
        return frame(outcome, 0.3, captured_at)


class RunnerTests(unittest.TestCase):
    def clock(self):
        state = SimpleNamespace(t=0.0)

        def tick() -> float:
            state.t += 0.12
            return state.t
        return tick

    def test_first_frame_is_reused_and_attempt_is_bounded(self) -> None:
        source = FakeSource([])
        recognizer = FakeRecognizer([M, M])
        verdict = run(source, recognizer, PROGRESSIVE, threshold=0.5, first_frame="first",
                      clock=self.clock())
        self.assertTrue(verdict.accepted)
        self.assertEqual(source.reads, 1)   # the warm-up frame was reused
        self.assertEqual(len(verdict.frames), 2)

    def test_never_reads_more_than_the_maximum(self) -> None:
        source = FakeSource([])
        verdict = run(source, FakeRecognizer([U]), PROGRESSIVE, threshold=0.5,
                      clock=self.clock())
        self.assertFalse(verdict.accepted)
        self.assertLessEqual(len(verdict.frames), PROGRESSIVE.max_frames)

    def test_preemption_stops_promptly(self) -> None:
        checks = iter([False, True])
        recognizer = FakeRecognizer([U])
        verdict = run(FakeSource([]), recognizer, PROGRESSIVE, threshold=0.5,
                      preempted=lambda: next(checks, True), clock=self.clock())
        self.assertEqual(verdict.reason, "preempted")
        self.assertEqual(recognizer.calls, 1)

    def test_time_budget_ends_a_slow_attempt(self) -> None:
        policy = VerifyPolicy(max_frames=50, time_budget_s=0.5)
        verdict = run(FakeSource([]), FakeRecognizer([U]), policy, threshold=0.5,
                      clock=self.clock())
        self.assertFalse(verdict.accepted)
        self.assertLess(len(verdict.frames), 10)


if __name__ == "__main__":
    unittest.main()
