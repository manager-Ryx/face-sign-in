"""Pure selection policy for bounded, conservative adaptive enrollment.

The initial, manually enrolled embeddings are anchors and are never evicted.
Adaptive samples are admitted only after identity and quality checks performed
by :mod:`face_service.recognizer`; this module keeps the most useful diverse
subset when the profile reaches its configured cap.
"""
from __future__ import annotations

import numpy as np


def cosine_distances(vector: np.ndarray, matrix: np.ndarray) -> np.ndarray:
    """Return cosine distance from ``vector`` to each row of ``matrix``."""
    if matrix.size == 0:
        return np.empty((0,), dtype=np.float32)
    vector = np.asarray(vector, dtype=np.float32)
    matrix = np.asarray(matrix, dtype=np.float32)
    vector = vector / (np.linalg.norm(vector) + 1e-9)
    matrix = matrix / (np.linalg.norm(matrix, axis=1, keepdims=True) + 1e-9)
    return 1.0 - matrix @ vector


def is_safe_adaptive_candidate(
    embedding: np.ndarray,
    protected: np.ndarray,
    existing: np.ndarray,
    *,
    threshold: float,
    match_margin: float,
    min_diversity: float,
) -> bool:
    """Require a strong anchor match and reject near-duplicate samples.

    Matching against immutable manual anchors prevents profile drift: an
    adaptive sample can never bootstrap another, progressively less similar
    sample into the identity profile.
    """
    if protected.size == 0:
        return False
    anchor_distance = float(np.min(cosine_distances(embedding, protected)))
    if anchor_distance > threshold - match_margin:
        return False
    if existing.size:
        nearest = float(np.min(cosine_distances(embedding, existing)))
        if nearest < min_diversity:
            return False
    return True


def select_diverse_adaptive_indices(
    protected: np.ndarray,
    adaptive: np.ndarray,
    qualities: np.ndarray,
    created_at: np.ndarray,
    limit: int,
) -> list[int]:
    """Select a deterministic, diverse, quality-aware bounded subset."""
    count = len(adaptive)
    if limit <= 0 or count == 0:
        return []
    if count <= limit:
        return list(range(count))

    qualities = np.asarray(qualities, dtype=np.float32)
    created_at = np.asarray(created_at, dtype=np.float64)
    q_min = float(np.min(qualities))
    q_span = max(float(np.max(qualities)) - q_min, 1e-9)
    quality_norm = (qualities - q_min) / q_span
    t_min = float(np.min(created_at))
    t_span = max(float(np.max(created_at)) - t_min, 1e-9)
    recency_norm = (created_at - t_min) / t_span

    selected: list[int] = []
    remaining = set(range(count))
    references = np.asarray(protected, dtype=np.float32)
    while remaining and len(selected) < limit:
        best_index = -1
        best_score = -1.0
        for index in sorted(remaining):
            diversity = (
                float(np.min(cosine_distances(adaptive[index], references)))
                if references.size
                else 1.0
            )
            # Diversity dominates: quality already passed hard admission gates,
            # so it should break ties rather than retain near-duplicates.
            score = (
                0.96 * diversity
                + 0.03 * float(quality_norm[index])
                + 0.01 * float(recency_norm[index])
            )
            if score > best_score:
                best_score = score
                best_index = index
        selected.append(best_index)
        remaining.remove(best_index)
        row = np.asarray(adaptive[best_index], dtype=np.float32)[None, :]
        references = row if not references.size else np.vstack((references, row))
    return selected


def select_rolling_adaptive_indices(
    adaptive: np.ndarray,
    qualities: np.ndarray,
    created_at: np.ndarray,
    *,
    limit: int,
    cutoff: float,
) -> list[int]:
    """Return the non-expired samples retained by a rolling profile.

    Age is the primary policy: expired samples are always removed, then the
    newest ``limit`` samples win. Quality is a deterministic tie-breaker for
    samples captured at exactly the same instant. Diversity is enforced by the
    admission gate, so retention cannot keep an old sample at the expense of a
    newer one and quietly defeat the rolling-window guarantee.

    Returned indices follow their original order so profile persistence is
    stable across repeated loads.
    """
    count = len(adaptive)
    if limit <= 0 or count == 0:
        return []
    qualities = np.asarray(qualities, dtype=np.float32)
    created_at = np.asarray(created_at, dtype=np.float64)
    if len(qualities) != count or len(created_at) != count:
        raise ValueError("adaptive profile metadata length mismatch")

    eligible = [
        index
        for index, timestamp in enumerate(created_at)
        if np.isfinite(timestamp) and float(timestamp) >= cutoff
    ]
    if len(eligible) <= limit:
        return eligible

    # Newest first. A higher-quality frame wins an exact timestamp tie; the
    # original index is the final stable tie-breaker.
    newest = sorted(
        eligible,
        key=lambda index: (
            -float(created_at[index]),
            -float(qualities[index]),
            index,
        ),
    )[:limit]
    return sorted(newest)
