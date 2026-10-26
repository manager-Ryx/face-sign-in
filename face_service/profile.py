"""Face profile storage: protected enrollment anchors plus learned samples.

A profile is immutable once built; every change produces a new profile that
is written atomically and then swapped in, so readers on other threads never
see a half-updated mixture of arrays.

Rows ``[0, protected_count)`` are the original enrollment ("anchors") and
are never evicted. Later rows are samples learned from confirmed sign-ins or
added explicitly by the user. Each row carries an id, its source, a
timestamp, a quality score and a lighting bucket so learning can be
explained, undone, and evaluated.

File format history: v1 (embeddings only), v3 (+ protected count, quality,
timestamps), v4 (+ ids, sources, lighting, anchor file names). Older files
load unchanged; legacy rows are treated as protected anchors.
"""
from __future__ import annotations

import logging
import os
import tempfile
import time
from dataclasses import dataclass, replace
from pathlib import Path

import numpy as np

log = logging.getLogger(__name__)

PROFILE_VERSION = 4

SOURCE_ENROLLMENT = 0
SOURCE_UNLOCK = 1
SOURCE_MANUAL = 2
SOURCE_NAMES = {SOURCE_ENROLLMENT: "enrollment", SOURCE_UNLOCK: "face sign-in",
                SOURCE_MANUAL: "added by you"}

LIGHTING_CODES = ("unknown", "bright", "dim", "dark", "backlit")


def lighting_code(name: str | None) -> int:
    return LIGHTING_CODES.index(name) if name in LIGHTING_CODES else 0


def normalize_rows(matrix: np.ndarray) -> np.ndarray:
    matrix = np.asarray(matrix, dtype=np.float32)
    if matrix.size == 0:
        return matrix.reshape(0, matrix.shape[-1] if matrix.ndim == 2 else 0)
    norms = np.linalg.norm(matrix, axis=1, keepdims=True)
    return matrix / np.maximum(norms, 1e-9)


def unit(vector: np.ndarray) -> np.ndarray:
    vector = np.asarray(vector, dtype=np.float32).ravel()
    return vector / max(float(np.linalg.norm(vector)), 1e-9)


@dataclass(frozen=True)
class FaceProfile:
    embeddings: np.ndarray
    unit_rows: np.ndarray
    protected_count: int
    sample_ids: np.ndarray
    sources: np.ndarray
    created_at: np.ndarray
    qualities: np.ndarray
    lighting: np.ndarray
    anchor_files: tuple[str, ...]
    next_id: int

    # ---- construction ------------------------------------------------------

    @classmethod
    def build(cls, embeddings: np.ndarray, *, protected_count: int,
              sample_ids: np.ndarray | None = None, sources: np.ndarray | None = None,
              created_at: np.ndarray | None = None, qualities: np.ndarray | None = None,
              lighting: np.ndarray | None = None, anchor_files: tuple[str, ...] = (),
              next_id: int | None = None) -> FaceProfile:
        embeddings = np.asarray(embeddings, dtype=np.float32)
        if embeddings.ndim != 2:
            raise ValueError("profile embeddings must be a 2-D array")
        count = len(embeddings)
        protected = max(0, min(int(protected_count), count))
        ids = (np.arange(1, count + 1, dtype=np.int64) if sample_ids is None
               else np.asarray(sample_ids, dtype=np.int64))
        src = (np.where(np.arange(count) < protected, SOURCE_ENROLLMENT,
                        SOURCE_UNLOCK).astype(np.int8)
               if sources is None else np.asarray(sources, dtype=np.int8))
        created = (np.zeros(count, dtype=np.float64) if created_at is None
                   else np.asarray(created_at, dtype=np.float64))
        quality = (np.ones(count, dtype=np.float32) if qualities is None
                   else np.asarray(qualities, dtype=np.float32))
        light = (np.zeros(count, dtype=np.int8) if lighting is None
                 else np.asarray(lighting, dtype=np.int8))
        for name, column in (("sample_ids", ids), ("sources", src), ("created_at", created),
                             ("qualities", quality), ("lighting", light)):
            if len(column) != count:
                raise ValueError(f"profile metadata length mismatch: {name}")
        if len(np.unique(ids)) != count:
            raise ValueError("profile sample ids must be unique")
        files = tuple(anchor_files)[:protected]
        following = int(ids.max()) + 1 if count else 1
        return cls(embeddings, normalize_rows(embeddings), protected, ids, src, created,
                   quality, light, files, max(following, int(next_id or 0)))

    # ---- queries -----------------------------------------------------------

    @property
    def size(self) -> int:
        return len(self.embeddings)

    @property
    def learned_count(self) -> int:
        return self.size - self.protected_count

    @property
    def dim(self) -> int:
        return int(self.embeddings.shape[1]) if self.embeddings.ndim == 2 else 0

    def distances(self, embedding: np.ndarray) -> np.ndarray:
        """Cosine distance from ``embedding`` to every row."""
        if self.size == 0:
            return np.empty((0,), dtype=np.float32)
        return 1.0 - self.unit_rows @ unit(embedding)

    def anchor_distances(self, embedding: np.ndarray) -> np.ndarray:
        return self.distances(embedding)[: self.protected_count]

    # ---- changes (each returns a new profile) ------------------------------

    def append(self, embeddings: np.ndarray, *, source: int, created_at: float,
               qualities: np.ndarray, lighting: np.ndarray) -> tuple[FaceProfile, list[int]]:
        embeddings = np.atleast_2d(np.asarray(embeddings, dtype=np.float32))
        count = len(embeddings)
        ids = np.arange(self.next_id, self.next_id + count, dtype=np.int64)
        profile = FaceProfile.build(
            np.vstack((self.embeddings, embeddings)) if self.size else embeddings,
            protected_count=self.protected_count,
            sample_ids=np.concatenate((self.sample_ids, ids)),
            sources=np.concatenate((self.sources, np.full(count, source, dtype=np.int8))),
            created_at=np.concatenate((self.created_at, np.full(count, created_at))),
            qualities=np.concatenate((self.qualities, np.asarray(qualities, dtype=np.float32))),
            lighting=np.concatenate((self.lighting, np.asarray(lighting, dtype=np.int8))),
            anchor_files=self.anchor_files, next_id=self.next_id + count)
        return profile, [int(i) for i in ids]

    def keep_learned(self, learned_indices: list[int]) -> FaceProfile:
        """Keep all anchors plus the given learned rows (indices into the learned part)."""
        rows = list(range(self.protected_count)) + [
            self.protected_count + i for i in sorted(learned_indices)]
        return self._subset(rows)

    def without_ids(self, ids: set[int]) -> FaceProfile:
        """Remove learned rows by id. Anchors can never be removed this way."""
        rows = [i for i in range(self.size)
                if i < self.protected_count or int(self.sample_ids[i]) not in ids]
        return self._subset(rows)

    def _subset(self, rows: list[int]) -> FaceProfile:
        index = np.asarray(rows, dtype=np.int64)
        return FaceProfile.build(
            self.embeddings[index] if len(index) else self.embeddings[:0],
            protected_count=self.protected_count, sample_ids=self.sample_ids[index],
            sources=self.sources[index], created_at=self.created_at[index],
            qualities=self.qualities[index], lighting=self.lighting[index],
            anchor_files=self.anchor_files, next_id=self.next_id)

    def with_anchor_files(self, files: tuple[str, ...]) -> FaceProfile:
        return replace(self, anchor_files=tuple(files)[: self.protected_count])


def load_profile(path: Path) -> FaceProfile | None:
    if not path.exists():
        return None
    with np.load(path, allow_pickle=False) as data:
        names = set(data.files)
        embeddings = np.asarray(data["embeddings"], dtype=np.float32)
        count = len(embeddings)
        protected = (int(np.asarray(data["protected_count"]).item())
                     if "protected_count" in names else count)

        def column(name: str):
            return np.asarray(data[name]) if name in names else None

        created = column("created_at")
        if created is not None:
            created = created.astype(np.float64)
        files = tuple(str(f) for f in data["anchor_files"]) if "anchor_files" in names else ()
        next_id = int(np.asarray(data["next_id"]).item()) if "next_id" in names else None
        return FaceProfile.build(
            embeddings, protected_count=protected, sample_ids=column("sample_ids"),
            sources=column("sources"), created_at=created, qualities=column("qualities"),
            lighting=column("lighting"), anchor_files=files, next_id=next_id)


def save_profile(profile: FaceProfile, path: Path) -> None:
    """Write ``profile`` atomically (temp file, fsync, replace)."""
    path.parent.mkdir(parents=True, exist_ok=True)
    fd, name = tempfile.mkstemp(dir=path.parent, prefix="embeddings-", suffix=".npz")
    temp = Path(name)
    try:
        with os.fdopen(fd, "wb") as handle:
            np.savez(
                handle,
                embeddings=profile.embeddings,
                protected_count=np.asarray(profile.protected_count, dtype=np.int64),
                qualities=profile.qualities,
                created_at=profile.created_at,
                sample_ids=profile.sample_ids,
                sources=profile.sources,
                lighting=profile.lighting,
                anchor_files=np.asarray(profile.anchor_files, dtype=np.str_),
                next_id=np.asarray(profile.next_id, dtype=np.int64),
                profile_version=np.asarray(PROFILE_VERSION, dtype=np.int64),
            )
            handle.flush()
            os.fsync(handle.fileno())
        for attempt in range(5):
            try:
                os.replace(temp, path)
                break
            except PermissionError:
                # Another process (antivirus, backup) may briefly hold the file.
                if attempt == 4:
                    raise
                time.sleep(0.1 * (attempt + 1))
    finally:
        if temp.exists():
            temp.unlink()
