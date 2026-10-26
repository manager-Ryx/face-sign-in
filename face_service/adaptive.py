"""Pure policies for bounded, conservative learning.

Admission: a learned sample must match one of the *original enrollment*
anchors with a safety margin below the unlock threshold. Matching against
immutable anchors (never against other learned samples) prevents a chain in
which progressively less similar samples validate each other.

Selection: from one confirmed sign-in, at most one frame is chosen – the
most useful one, balancing image quality against novelty – and frames nearly
identical to stored samples are rejected.

Retention: when the learned part of the profile exceeds its cap, samples
past the optional age limit go first; the newest samples are always kept so
the profile follows gradual changes; beyond that the most redundant sample
(closest to an anchor or another learned sample) is evicted, so diverse
appearances such as glasses on/off survive.
"""
from __future__ import annotations

import math
from dataclasses import dataclass, field

import numpy as np

from .profile import normalize_rows, unit

NOVELTY_SCALE = 0.15  # novelty beyond this distance earns no extra credit


def cosine_distances(vector: np.ndarray, matrix: np.ndarray) -> np.ndarray:
    """Cosine distance from ``vector`` to each row of ``matrix``."""
    matrix = np.asarray(matrix, dtype=np.float32)
    if matrix.size == 0:
        return np.empty((0,), dtype=np.float32)
    return 1.0 - normalize_rows(matrix) @ unit(vector)


def is_safe_adaptive_candidate(embedding: np.ndarray, protected: np.ndarray,
                               existing: np.ndarray, *, threshold: float,
                               match_margin: float, min_diversity: float) -> bool:
    """Require a strong anchor match and reject near-duplicate samples."""
    if np.asarray(protected).size == 0:
        return False
    if float(np.min(cosine_distances(embedding, protected))) > threshold - match_margin:
        return False
    existing = np.asarray(existing)
    return not (existing.size and float(np.min(cosine_distances(embedding, existing)))
                < min_diversity)


@dataclass
class LearningCandidate:
    """One frame from a successful attempt that could become a learned sample."""

    embedding: np.ndarray = field(repr=False)
    quality: float                 # 0..1 image usefulness (see quality.ImageQuality.score)
    lighting: str = "unknown"
    face_confidence: float = 1.0
    face_ratio: float = 1.0
    sharpness: float = 1000.0
    is_real: bool = True
    face_count: int = 1
    # Filled in by choose_learning_sample:
    anchor_distance: float = math.nan
    novelty: float = math.nan
    utility: float = math.nan
    rejected: str | None = None


def choose_learning_sample(candidates: list[LearningCandidate], anchors_unit: np.ndarray,
                           all_unit: np.ndarray, *, threshold: float, match_margin: float,
                           min_diversity: float, min_face_confidence: float,
                           min_face_ratio: float, min_sharpness: float
                           ) -> LearningCandidate | None:
    """Pick the single most useful safe sample, or ``None``.

    Every candidate is annotated with its anchor distance, novelty, utility
    and – when rejected – the reason, so the decision can be explained.
    """
    best: LearningCandidate | None = None
    for candidate in candidates:
        vector = unit(candidate.embedding)
        if candidate.face_count != 1:
            candidate.rejected = "more than one face"
            continue
        if not candidate.is_real:
            candidate.rejected = "liveness check failed"
            continue
        if (candidate.face_confidence < min_face_confidence
                or candidate.face_ratio < min_face_ratio
                or candidate.sharpness < min_sharpness
                or candidate.quality <= 0.0):
            candidate.rejected = "image quality too low"
            continue
        if anchors_unit.size == 0:
            candidate.rejected = "no enrollment"
            continue
        candidate.anchor_distance = float(np.min(1.0 - anchors_unit @ vector))
        if candidate.anchor_distance > threshold - match_margin:
            candidate.rejected = "not close enough to your enrollment"
            continue
        candidate.novelty = float(np.min(1.0 - all_unit @ vector)) if all_unit.size else 1.0
        if candidate.novelty < min_diversity:
            candidate.rejected = "nearly identical to a stored sample"
            continue
        candidate.utility = round(0.6 * candidate.quality
                                  + 0.4 * min(1.0, candidate.novelty / NOVELTY_SCALE), 4)
        if best is None or candidate.utility > best.utility:
            best = candidate
    return best


def select_retained(learned: np.ndarray, anchors: np.ndarray, created_at: np.ndarray,
                    qualities: np.ndarray, *, limit: int, cutoff: float = -math.inf,
                    recent_keep: int = 0) -> list[int]:
    """Indices (ascending) of learned samples to keep under ``limit``."""
    count = len(learned)
    created_at = np.asarray(created_at, dtype=np.float64)
    qualities = np.asarray(qualities, dtype=np.float32)
    if len(created_at) != count or len(qualities) != count:
        raise ValueError("learned-sample metadata length mismatch")
    keep = [i for i in range(count) if np.isfinite(created_at[i]) and created_at[i] >= cutoff]
    if limit <= 0:
        return []
    if len(keep) <= limit:
        return keep

    newest_first = sorted(keep, key=lambda i: (-created_at[i], -qualities[i], i))
    pinned = set(newest_first[:max(0, min(recent_keep, limit))])
    rows = normalize_rows(learned)
    anchor_rows = normalize_rows(anchors) if np.asarray(anchors).size else rows[:0]
    to_anchor = ((1.0 - rows @ anchor_rows.T).min(axis=1) if len(anchor_rows)
                 else np.full(count, np.inf))
    pairwise = 1.0 - rows @ rows.T
    np.fill_diagonal(pairwise, np.inf)
    alive = np.zeros(count, dtype=bool)
    alive[keep] = True
    pairwise[:, ~alive] = np.inf

    # Nearest stored neighbour of every live sample, updated incrementally so
    # large reductions (for example a lowered cap) stay fast.
    nearest_idx = pairwise.argmin(axis=1)
    nearest = np.minimum(to_anchor, pairwise[np.arange(count), nearest_idx])
    remaining = int(alive.sum())
    while remaining > limit:
        candidates = [i for i in np.flatnonzero(alive) if i not in pinned]
        if not candidates:
            candidates = list(np.flatnonzero(alive))
        victim = min(candidates, key=lambda i: (round(float(nearest[i]), 4),
                                                float(created_at[i]), float(qualities[i]), i))
        alive[victim] = False
        remaining -= 1
        pairwise[:, victim] = np.inf
        stale = np.flatnonzero(alive & (nearest_idx == victim))
        for i in stale:
            nearest_idx[i] = int(pairwise[i].argmin())
            nearest[i] = min(to_anchor[i], pairwise[i, nearest_idx[i]])
    return [int(i) for i in np.flatnonzero(alive)]
