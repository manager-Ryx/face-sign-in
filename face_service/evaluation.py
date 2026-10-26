"""Threshold evaluation from trusted genuine samples.

What the evidence can and cannot show: genuine samples (you, verified)
measure how often *you* would still be recognised at a stricter threshold.
They say nothing about how often *someone else* would be accepted; no
threshold can guarantee every other face is rejected. So this module only
ever recommends making recognition stricter, in small steps, and only when
the evidence is broad enough:

* samples from several days and more than one lighting condition, held out
  from the profile (test checks you run, held-out enrollment photos, and –
  flagged as possibly optimistic – frames from confirmed face sign-ins);
* a conservative (Wilson 95 %) lower bound on per-frame acceptance in every
  lighting condition with enough data;
* when recorded multi-frame checks exist, the actual unlock policy replayed
  on them, which accounts for correlated frames.
"""
from __future__ import annotations

import math
from collections import defaultdict
from dataclasses import dataclass, field

FLOOR = 0.30            # never recommend stricter than this
MAX_STEP = 0.02         # largest single change
GRID_STEP = 0.01
TARGET_FRAME = 0.90     # per-frame acceptance lower bound to keep
TARGET_ATTEMPT = 0.95   # replayed attempt success to keep
MIN_SAMPLES = 60
MIN_DAYS = 5
MIN_LIGHTING = 2
MIN_BUCKET = 15
MIN_SEQUENCES = 10


@dataclass(frozen=True)
class GenuineSample:
    distance: float      # to the current profile, the sample itself excluded
    day: str             # local date, e.g. "2026-10-26"
    lighting: str
    source: str          # "check", "holdout", "unlock"


@dataclass
class Evidence:
    samples: int
    days: int
    lighting: dict[str, int]
    sources: dict[str, int]
    sequences: int

    def missing(self) -> list[str]:
        gaps = []
        if self.samples < MIN_SAMPLES:
            gaps.append(f"{MIN_SAMPLES - self.samples} more face samples")
        if self.days < MIN_DAYS:
            gaps.append(f"samples from {MIN_DAYS - self.days} more day(s)")
        useful = [k for k, n in self.lighting.items() if k != "unknown" and n >= MIN_BUCKET]
        if len(useful) < MIN_LIGHTING:
            gaps.append("samples in another lighting condition")
        return gaps


@dataclass
class ThresholdReport:
    current: float
    baseline: float
    recommended: float
    status: str                       # "insufficient", "keep", "tighten"
    explanation: str
    evidence: Evidence
    acceptance: dict[str, float] = field(default_factory=dict)   # threshold -> lower bound

    def to_dict(self) -> dict:
        return {"current": self.current, "baseline": self.baseline,
                "recommended": self.recommended, "status": self.status,
                "explanation": self.explanation,
                "evidence": {"samples": self.evidence.samples, "days": self.evidence.days,
                             "lighting": self.evidence.lighting,
                             "sources": self.evidence.sources,
                             "sequences": self.evidence.sequences,
                             "missing": self.evidence.missing()},
                "acceptance": self.acceptance}


def wilson_lower(successes: int, total: int, z: float = 1.96) -> float:
    if total == 0:
        return 0.0
    p = successes / total
    denom = 1 + z * z / total
    centre = p + z * z / (2 * total)
    margin = z * math.sqrt(p * (1 - p) / total + z * z / (4 * total * total))
    return max(0.0, (centre - margin) / denom)


def replay_attempt(distances: list[float], threshold: float, *, required: int = 2,
                   max_frames: int = 5) -> bool:
    """Would a recorded check sequence have unlocked at ``threshold``?"""
    return sum(1 for d in distances[:max_frames] if d <= threshold) >= required


def evaluate_threshold(samples: list[GenuineSample], *, current: float, baseline: float,
                       sequences: list[list[float]] | None = None,
                       required: int = 2, max_frames: int = 5) -> ThresholdReport:
    sequences = sequences or []
    by_light: dict[str, list[float]] = defaultdict(list)
    for sample in samples:
        by_light[sample.lighting].append(sample.distance)
    evidence = Evidence(
        samples=len(samples), days=len({s.day for s in samples}),
        lighting={k: len(v) for k, v in sorted(by_light.items())},
        sources={k: sum(1 for s in samples if s.source == k)
                 for k in sorted({s.source for s in samples})},
        sequences=len(sequences))
    distances = [s.distance for s in samples]
    acceptance = {}
    candidates = []
    t = round(current - GRID_STEP, 4)
    while t >= FLOOR - 1e-9:
        candidates.append(round(t, 4))
        t -= GRID_STEP
    for candidate in [current] + candidates:
        accepted = sum(1 for d in distances if d <= candidate)
        acceptance[f"{candidate:.2f}"] = round(wilson_lower(accepted, len(distances)), 4)

    if evidence.missing():
        return ThresholdReport(
            current, baseline, current, "insufficient",
            "Not enough varied evidence yet to judge a stricter setting: needs "
            + ", ".join(evidence.missing()) + ".", evidence, acceptance)

    def acceptable(threshold: float) -> bool:
        for light, values in by_light.items():
            if light == "unknown" or len(values) < MIN_BUCKET:
                continue
            if wilson_lower(sum(1 for d in values if d <= threshold), len(values)) < TARGET_FRAME:
                return False
        overall = wilson_lower(sum(1 for d in distances if d <= threshold), len(distances))
        if overall < TARGET_FRAME:
            return False
        if len(sequences) >= MIN_SEQUENCES:
            replayed = sum(replay_attempt(seq, threshold, required=required,
                                          max_frames=max_frames) for seq in sequences)
            if wilson_lower(replayed, len(sequences)) < TARGET_ATTEMPT:
                return False
        return True

    if not acceptable(current):
        return ThresholdReport(
            current, baseline, current, "keep",
            "Some of your recent test frames fall outside the current setting. Adding your "
            "current appearance usually helps more than changing strictness; the app will "
            "not loosen it on its own.", evidence, acceptance)
    strictest = current
    for candidate in candidates:
        if acceptable(candidate):
            strictest = candidate
        else:
            break
    if strictest >= current:
        return ThresholdReport(
            current, baseline, current, "keep",
            "Your current setting is the strictest one the evidence supports.",
            evidence, acceptance)
    recommended = round(max(strictest, current - MAX_STEP), 4)
    lower = acceptance.get(f"{recommended:.2f}", 0.0)
    return ThresholdReport(
        current, baseline, recommended, "tighten",
        f"At {recommended:.2f}, at least {lower:.0%} of your {evidence.samples} test frames "
        f"(from {evidence.days} days) would still match. This cannot measure how often "
        "someone else would be accepted; it only shows you would still be recognised.",
        evidence, acceptance)
