"""Progressive multi-frame verification.

The first frame decides the path instead of a fixed frame count:

* no face        -> up to three cheap detector-only looks, then stop;
* clear mismatch -> stop after a second clear mismatch;
* match          -> one more matching frame confirms it;
* uncertain or unusable (dark, blurred, turned away) -> keep looking, up to
  ``max_frames`` (five on the lock screen).

Every analysed frame is reused; nothing restarts. Two matching frames are
required (a one-frame rule exists but stays disabled unless evaluation
justifies it). Frames taken fractions of a second apart are strongly
correlated, so the second match protects against a single glitched frame –
it is not an independent identity check. The identity decision rests on the
threshold.

``evaluate`` is pure (frame outcomes and timestamps only), so the policy can
be unit-tested and replayed against recorded attempts.
"""
from __future__ import annotations

import time
from collections import Counter
from collections.abc import Callable
from dataclasses import dataclass, field
from enum import StrEnum
from typing import Protocol

import numpy as np

from .recognizer import FrameAnalysis, Outcome, Recognizer


class Decision(StrEnum):
    CONTINUE = "continue"
    ACCEPT = "accept"
    REJECT = "reject"


@dataclass(frozen=True)
class Step:
    decision: Decision
    reason: str = ""


CONTINUE = Step(Decision.CONTINUE)


@dataclass(frozen=True)
class VerifyPolicy:
    max_frames: int = 5
    required: int = 2
    min_gap_s: float = 0.1
    max_no_face: int = 3
    max_mismatch: int = 2
    uncertain_band: float = 0.08
    single_frame_distance: float | None = None
    time_budget_s: float = 4.0
    exhaustive: bool = False   # analyse every frame (tests record complete sequences)

    @classmethod
    def for_unlock(cls, cfg) -> VerifyPolicy:
        if cfg.unlock_strategy == "fixed":
            frames = cfg.unlock_verify_frames
            # The original 2-of-3 vote: no early exits, adjacent frames count.
            return cls(max_frames=frames, required=cfg.unlock_verify_required, min_gap_s=0.0,
                       max_no_face=frames, max_mismatch=frames)
        return cls(max_frames=max(cfg.unlock_max_frames, cfg.unlock_verify_required),
                   required=cfg.unlock_verify_required)

    @classmethod
    def for_presence(cls, cfg) -> VerifyPolicy:
        return cls(max_frames=3, required=1, min_gap_s=0.0, time_budget_s=3.0)

    @classmethod
    def for_check(cls, cfg) -> VerifyPolicy:
        frames = max(cfg.verify_frames, cfg.verify_required)
        return cls(max_frames=frames, required=cfg.verify_required, min_gap_s=0.0,
                   max_no_face=frames, max_mismatch=frames, time_budget_s=8.0,
                   exhaustive=True)


@dataclass
class Verdict:
    decision: Decision
    reason: str
    frames: list[FrameAnalysis] = field(default_factory=list)
    counted_matches: int = 0
    elapsed_ms: float = 0.0

    @property
    def accepted(self) -> bool:
        return self.decision is Decision.ACCEPT

    @property
    def best_distance(self) -> float:
        judged = [f.distance for f in self.frames
                  if f.outcome not in (Outcome.NO_FACE, Outcome.SPOOF, Outcome.ERROR)]
        return min(judged) if judged else 1.0

    @property
    def issue(self) -> str | None:
        """The most useful guidance for an attempt that did not succeed."""
        if self.accepted:
            return None
        if self.reason in ("mismatch", "spoof", "uncertain"):
            return self.reason
        issues = Counter(f.issue for f in self.frames if f.issue)
        return issues.most_common(1)[0][0] if issues else self.reason

    def outcome_counts(self) -> dict[str, int]:
        return dict(Counter(f.outcome.value for f in self.frames))


def counted_matches(frames: list[FrameAnalysis], min_gap_s: float) -> int:
    """Matches that are at least ``min_gap_s`` apart."""
    counted, last = 0, -np.inf
    for frame in frames:
        if frame.match and frame.captured_at - last >= min_gap_s:
            counted += 1
            last = frame.captured_at
    return counted


def dominant_failure(frames: list[FrameAnalysis]) -> str:
    outcomes = Counter(f.outcome for f in frames)
    if outcomes[Outcome.SPOOF]:
        return "spoof"
    if outcomes[Outcome.UNCERTAIN] or outcomes[Outcome.MATCH]:
        return "uncertain"
    if outcomes[Outcome.MISMATCH]:
        return "mismatch"
    if outcomes[Outcome.UNUSABLE]:
        return "unusable"
    if outcomes[Outcome.ERROR]:
        return "error"
    return "no_face"


def evaluate(frames: list[FrameAnalysis], policy: VerifyPolicy) -> Step:
    """Decide after the latest frame: continue, accept, or reject with a reason."""
    counted = counted_matches(frames, policy.min_gap_s)
    if policy.exhaustive:
        if len(frames) < policy.max_frames:
            return CONTINUE
        if counted >= policy.required:
            return Step(Decision.ACCEPT, "match")
        return Step(Decision.REJECT, dominant_failure(frames))
    if counted >= policy.required:
        return Step(Decision.ACCEPT, "match")
    if policy.single_frame_distance is not None and any(
            f.match and f.distance <= policy.single_frame_distance for f in frames):
        return Step(Decision.ACCEPT, "match")
    if not frames:
        return CONTINUE
    if any(f.outcome is Outcome.SPOOF for f in frames):
        return Step(Decision.REJECT, "spoof")
    tail_no_face = 0
    for frame in reversed(frames):
        if frame.outcome is not Outcome.NO_FACE:
            break
        tail_no_face += 1
    if tail_no_face >= policy.max_no_face:
        return Step(Decision.REJECT, "no_face")
    mismatches = sum(1 for f in frames if f.outcome is Outcome.MISMATCH)
    if counted == 0 and mismatches >= policy.max_mismatch:
        return Step(Decision.REJECT, "mismatch")
    remaining = policy.max_frames - len(frames)
    if counted + remaining < policy.required:
        return Step(Decision.REJECT, dominant_failure(frames))
    return CONTINUE


class FrameSource(Protocol):
    def read_frame(self) -> np.ndarray: ...


def run(source: FrameSource, recognizer: Recognizer, policy: VerifyPolicy, *,
        threshold: float, first_frame: np.ndarray | None = None,
        preempted: Callable[[], bool] = lambda: False, keep_crops: bool = False,
        on_frame: Callable[[FrameAnalysis], None] | None = None,
        clock: Callable[[], float] = time.monotonic,
        sleep: Callable[[float], None] = time.sleep) -> Verdict:
    """Read and analyse frames until the policy reaches a decision."""
    started = clock()
    deadline = started + policy.time_budget_s
    frames: list[FrameAnalysis] = []
    pending = first_frame

    def verdict(decision: Decision, reason: str) -> Verdict:
        return Verdict(decision, reason, frames, counted_matches(frames, policy.min_gap_s),
                       (clock() - started) * 1000.0)

    while True:
        if preempted():
            return verdict(Decision.REJECT, "preempted")
        if frames and clock() > deadline:
            return verdict(Decision.REJECT, dominant_failure(frames))
        if pending is not None:
            frame, pending = pending, None
        else:
            if frames and frames[-1].match:
                # A confirming frame must be a separate moment, not the next
                # buffered frame (analysis is usually slower than this anyway).
                wait = policy.min_gap_s - (clock() - frames[-1].captured_at)
                if wait > 0:
                    sleep(wait)
            frame = source.read_frame()
        analysis = recognizer.analyze(frame, captured_at=clock(), threshold=threshold,
                                      uncertain_band=policy.uncertain_band, keep_crop=keep_crops)
        frames.append(analysis)
        if on_frame is not None:
            on_frame(analysis)
        step = evaluate(frames, policy)
        if step.decision is not Decision.CONTINUE:
            return verdict(step.decision, step.reason)
