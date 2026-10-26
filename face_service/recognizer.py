from __future__ import annotations

import logging
import os
import secrets
import tempfile
import time
from dataclasses import dataclass
from pathlib import Path

import cv2
import numpy as np

from .adaptive import (
    cosine_distances,
    is_safe_adaptive_candidate,
    select_rolling_adaptive_indices,
)
from .config import Config, EMBED_PATH, ENROLL_DIR

log = logging.getLogger(__name__)


def _lazy_deepface():
    # Imported lazily because TensorFlow import is slow and heavy.
    from deepface import DeepFace  # type: ignore
    return DeepFace


@dataclass
class FrameAnalysis:
    match: bool
    distance: float
    is_real: bool
    embedding: np.ndarray | None = None
    anchor_distance: float = 1.0
    quality: float = 0.0
    face_count: int = 0
    adaptive_eligible: bool = False


@dataclass
class _PendingAdaptive:
    created_at: float
    embeddings: list[np.ndarray]
    qualities: list[float]


class Recognizer:
    """DeepFace recognizer with immutable anchors and bounded adaptation."""

    _PENDING_TTL_S = 120.0
    _MAX_PENDING_ATTEMPTS = 8

    def __init__(self, cfg: Config):
        self.cfg = cfg
        self._refs: np.ndarray | None = None
        self._protected_count = 0
        self._qualities = np.empty((0,), dtype=np.float32)
        self._created_at = np.empty((0,), dtype=np.float64)
        self._pending: dict[str, _PendingAdaptive] = {}

    # ---------- profile persistence ----------

    @staticmethod
    def _atomic_save_profile(
        embeddings: np.ndarray,
        protected_count: int,
        qualities: np.ndarray,
        created_at: np.ndarray,
    ) -> None:
        EMBED_PATH.parent.mkdir(parents=True, exist_ok=True)
        handle = tempfile.NamedTemporaryFile(
            mode="wb", prefix="embeddings-", suffix=".npz",
            dir=EMBED_PATH.parent, delete=False,
        )
        temp_path = Path(handle.name)
        try:
            with handle:
                np.savez(
                    handle,
                    embeddings=np.asarray(embeddings, dtype=np.float32),
                    protected_count=np.asarray(protected_count, dtype=np.int64),
                    qualities=np.asarray(qualities, dtype=np.float32),
                    created_at=np.asarray(created_at, dtype=np.float64),
                    profile_version=np.asarray(3, dtype=np.int64),
                )
                handle.flush()
                os.fsync(handle.fileno())
            os.replace(temp_path, EMBED_PATH)
        finally:
            if temp_path.exists():
                temp_path.unlink()

    def _install_profile(
        self,
        embeddings: np.ndarray,
        protected_count: int,
        qualities: np.ndarray,
        created_at: np.ndarray,
    ) -> None:
        self._refs = np.asarray(embeddings, dtype=np.float32)
        self._protected_count = max(0, min(int(protected_count), len(self._refs)))
        self._qualities = np.asarray(qualities, dtype=np.float32)
        self._created_at = np.asarray(created_at, dtype=np.float64)

    def _apply_adaptive_retention(self, now: float, *, persist: bool) -> bool:
        """Expire and cap adaptive samples without ever removing anchors."""
        if self._refs is None:
            return False
        protected_count = self._protected_count
        protected = self._refs[:protected_count]
        protected_qualities = self._qualities[:protected_count]
        protected_created = self._created_at[:protected_count]
        adaptive = self._refs[protected_count:]
        adaptive_qualities = self._qualities[protected_count:]
        adaptive_created = self._created_at[protected_count:].copy()

        # Profiles written by an earlier adaptive implementation may lack a
        # usable timestamp. Preserve those samples and begin their rolling age
        # at migration time instead of deleting them unexpectedly.
        invalid = ~np.isfinite(adaptive_created) | (adaptive_created <= 0.0)
        if np.any(invalid):
            adaptive_created[invalid] = now

        retention_seconds = self.cfg.adaptive_retention_days * 24.0 * 60.0 * 60.0
        cutoff = -float("inf") if retention_seconds == 0.0 else now - retention_seconds
        chosen = select_rolling_adaptive_indices(
            adaptive,
            adaptive_qualities,
            adaptive_created,
            limit=self.cfg.adaptive_max_embeddings,
            cutoff=cutoff,
        )

        final_refs = protected.copy()
        final_qualities = protected_qualities.copy()
        final_created = protected_created.copy()
        if chosen:
            final_refs = np.vstack((final_refs, adaptive[chosen]))
            final_qualities = np.concatenate(
                (final_qualities, adaptive_qualities[chosen])
            )
            final_created = np.concatenate((final_created, adaptive_created[chosen]))

        metadata_changed = not np.array_equal(adaptive_created, self._created_at[protected_count:])
        content_changed = len(chosen) != len(adaptive) or chosen != list(range(len(adaptive)))
        changed = metadata_changed or content_changed
        if changed:
            if persist:
                self._atomic_save_profile(
                    final_refs, protected_count, final_qualities, final_created
                )
            self._install_profile(
                final_refs, protected_count, final_qualities, final_created
            )
        return changed

    # ---------- enrollment ----------

    def enroll_from_dir(self, directory: Path = ENROLL_DIR) -> int:
        """Rebuild manual anchors; adaptive samples are intentionally reset."""
        DeepFace = _lazy_deepface()
        directory.mkdir(parents=True, exist_ok=True)
        images = [
            p for p in directory.iterdir()
            if p.suffix.lower() in {".jpg", ".jpeg", ".png"}
        ]
        if not images:
            raise RuntimeError(f"No enroll images in {directory}")

        vecs: list[np.ndarray] = []
        for path in images:
            try:
                reps = DeepFace.represent(
                    img_path=str(path),
                    model_name=self.cfg.model_name,
                    detector_backend=self.cfg.detector_backend,
                    enforce_detection=True,
                    align=True,
                )
                # Manual enrollment also rejects ambiguous multi-face photos.
                if len(reps) == 1:
                    vecs.append(np.asarray(reps[0]["embedding"], dtype=np.float32))
                    log.info("enrolled %s", path.name)
                else:
                    log.warning("skip %s: expected one face, found %d", path.name, len(reps))
            except Exception as exc:
                log.warning("skip %s: %s", path.name, exc)

        if not vecs:
            raise RuntimeError("No unambiguous face found in enroll images")
        embeddings = np.stack(vecs, axis=0)
        now = time.time()
        qualities = np.ones((len(embeddings),), dtype=np.float32)
        created_at = np.full((len(embeddings),), now, dtype=np.float64)
        self._atomic_save_profile(
            embeddings, len(embeddings), qualities, created_at
        )
        self._install_profile(
            embeddings, len(embeddings), qualities, created_at
        )
        self._pending.clear()
        return len(vecs)

    def load(self) -> bool:
        if not EMBED_PATH.exists():
            return False
        with np.load(EMBED_PATH, allow_pickle=False) as data:
            embeddings = np.asarray(data["embeddings"], dtype=np.float32)
            # Legacy profiles contained embeddings only. Treat every legacy
            # embedding as a protected manual anchor, never as evictable data.
            protected_count = (
                int(np.asarray(data["protected_count"]).item())
                if "protected_count" in data.files else len(embeddings)
            )
            qualities = (
                np.asarray(data["qualities"], dtype=np.float32)
                if "qualities" in data.files else np.ones(len(embeddings), dtype=np.float32)
            )
            created_at = (
                np.asarray(data["created_at"], dtype=np.float64)
                if "created_at" in data.files else np.zeros(len(embeddings), dtype=np.float64)
            )
        if len(qualities) != len(embeddings) or len(created_at) != len(embeddings):
            raise RuntimeError("Enrollment profile metadata length mismatch")
        self._install_profile(embeddings, protected_count, qualities, created_at)
        self._apply_adaptive_retention(time.time(), persist=True)
        return True

    def profile_counts(self) -> tuple[int, int]:
        if self._refs is None and not self.load():
            return 0, 0
        assert self._refs is not None
        return self._protected_count, len(self._refs) - self._protected_count

    # ---------- verification ----------

    def _quality(self, bgr: np.ndarray, rep: dict) -> tuple[float, bool]:
        confidence = float(rep.get("face_confidence", 1.0) or 0.0)
        area = rep.get("facial_area") or {}
        x = max(0, int(area.get("x", 0)))
        y = max(0, int(area.get("y", 0)))
        width = max(0, int(area.get("w", 0)))
        height = max(0, int(area.get("h", 0)))
        image_h, image_w = bgr.shape[:2]
        x2, y2 = min(image_w, x + width), min(image_h, y + height)
        face_ratio = (max(0, x2 - x) * max(0, y2 - y)) / max(1, image_w * image_h)
        crop = bgr[y:y2, x:x2]
        blur = (
            float(cv2.Laplacian(cv2.cvtColor(crop, cv2.COLOR_BGR2GRAY), cv2.CV_64F).var())
            if crop.size else 0.0
        )
        quality = (
            0.50 * min(1.0, max(0.0, confidence))
            + 0.25 * min(1.0, face_ratio / 0.12)
            + 0.25 * min(1.0, blur / 120.0)
        )
        eligible = (
            confidence >= self.cfg.adaptive_min_face_confidence
            and face_ratio >= self.cfg.adaptive_min_face_area_ratio
            and blur >= self.cfg.adaptive_min_blur
        )
        return quality, eligible

    def analyze_frame(self, bgr: np.ndarray) -> FrameAnalysis:
        DeepFace = _lazy_deepface()
        if self._refs is None and not self.load():
            raise RuntimeError("No enrollment found. Run enroll first.")
        assert self._refs is not None

        is_real = True
        if self.cfg.anti_spoofing:
            try:
                faces = DeepFace.extract_faces(
                    img_path=bgr,
                    detector_backend=self.cfg.detector_backend,
                    anti_spoofing=True,
                    enforce_detection=True,
                )
                if not faces:
                    return FrameAnalysis(False, 1.0, False)
                is_real = bool(faces[0].get("is_real", True))
                if not is_real:
                    return FrameAnalysis(False, 1.0, False, face_count=len(faces))
            except Exception as exc:
                log.warning("liveness check failed: %s", exc)
                return FrameAnalysis(False, 1.0, False)

        try:
            reps = DeepFace.represent(
                img_path=bgr,
                model_name=self.cfg.model_name,
                detector_backend=self.cfg.detector_backend,
                enforce_detection=True,
                align=True,
            )
        except Exception as exc:
            log.debug("no face: %s", exc)
            return FrameAnalysis(False, 1.0, is_real)
        if not reps:
            return FrameAnalysis(False, 1.0, is_real)

        # Preserve the existing first-face verification behaviour. Adaptive
        # enrollment is stricter and requires exactly one detected face.
        rep = reps[0]
        embedding = np.asarray(rep["embedding"], dtype=np.float32)
        # One vectorized comparison keeps hundreds of rolling samples cheap.
        best = float(np.min(cosine_distances(embedding, self._refs)))
        protected = self._refs[:self._protected_count]
        anchor_distance = (
            float(np.min(cosine_distances(embedding, protected)))
            if len(protected) else 1.0
        )
        quality, quality_ok = self._quality(bgr, rep)
        eligible = (
            len(reps) == 1
            and is_real
            and quality_ok
            and anchor_distance <= self.cfg.threshold - self.cfg.adaptive_match_margin
        )
        return FrameAnalysis(
            best <= self.cfg.threshold,
            best,
            is_real,
            embedding,
            anchor_distance,
            quality,
            len(reps),
            eligible,
        )

    def verify_frame(self, bgr: np.ndarray) -> tuple[bool, float, bool]:
        """Backward-compatible public verification tuple."""
        result = self.analyze_frame(bgr)
        return result.match, result.distance, result.is_real

    # ---------- adaptive enrollment transaction ----------

    def _prune_pending(self) -> None:
        cutoff = time.time() - self._PENDING_TTL_S
        for attempt_id, pending in list(self._pending.items()):
            if pending.created_at < cutoff:
                self._pending.pop(attempt_id, None)
        while len(self._pending) >= self._MAX_PENDING_ATTEMPTS:
            oldest = min(self._pending, key=lambda key: self._pending[key].created_at)
            self._pending.pop(oldest, None)

    def stage_adaptive(self, analyses: list[FrameAnalysis]) -> str | None:
        """Stage, but do not persist, safe candidates for one face attempt."""
        if not self.cfg.adaptive_enrollment or self.cfg.adaptive_samples_per_unlock == 0:
            return None
        if self._refs is None and not self.load():
            return None
        assert self._refs is not None
        protected = self._refs[:self._protected_count]
        working = self._refs.copy()
        embeddings: list[np.ndarray] = []
        qualities: list[float] = []
        for analysis in analyses:
            if not analysis.adaptive_eligible or analysis.embedding is None:
                continue
            candidate = np.asarray(analysis.embedding, dtype=np.float32)
            if not is_safe_adaptive_candidate(
                candidate, protected, working,
                threshold=self.cfg.threshold,
                match_margin=self.cfg.adaptive_match_margin,
                min_diversity=self.cfg.adaptive_min_diversity,
            ):
                continue
            embeddings.append(candidate)
            qualities.append(float(analysis.quality))
            working = np.vstack((working, candidate[None, :]))
            if len(embeddings) >= self.cfg.adaptive_samples_per_unlock:
                break
        if not embeddings:
            return None
        self._prune_pending()
        attempt_id = secrets.token_urlsafe(18)
        self._pending[attempt_id] = _PendingAdaptive(time.time(), embeddings, qualities)
        return attempt_id

    def finish_adaptive(self, attempt_id: str, accepted: bool) -> int:
        """Commit staged samples only after Windows reports authentication success."""
        self._prune_pending()
        pending = self._pending.pop(attempt_id, None)
        if pending is None or not accepted:
            return 0
        if self._refs is None and not self.load():
            return 0
        assert self._refs is not None
        now = time.time()
        retention_changed = self._apply_adaptive_retention(now, persist=False)
        assert self._refs is not None
        protected = self._refs[:self._protected_count]
        refs = self._refs.copy()
        qualities = self._qualities.copy()
        created_at = self._created_at.copy()
        added = 0
        for candidate, quality in zip(pending.embeddings, pending.qualities):
            if not is_safe_adaptive_candidate(
                candidate, protected, refs,
                threshold=self.cfg.threshold,
                match_margin=self.cfg.adaptive_match_margin,
                min_diversity=self.cfg.adaptive_min_diversity,
            ):
                continue
            refs = np.vstack((refs, candidate[None, :]))
            qualities = np.append(qualities, np.float32(quality))
            created_at = np.append(created_at, np.float64(now))
            added += 1
        if not added:
            if retention_changed:
                self._atomic_save_profile(
                    refs, self._protected_count, qualities, created_at
                )
            return 0

        adaptive = refs[self._protected_count:]
        adaptive_qualities = qualities[self._protected_count:]
        adaptive_created = created_at[self._protected_count:]
        retention_seconds = self.cfg.adaptive_retention_days * 24.0 * 60.0 * 60.0
        cutoff = -float("inf") if retention_seconds == 0.0 else now - retention_seconds
        chosen = select_rolling_adaptive_indices(
            adaptive,
            adaptive_qualities,
            adaptive_created,
            limit=self.cfg.adaptive_max_embeddings,
            cutoff=cutoff,
        )
        final_refs = protected.copy()
        final_qualities = qualities[:self._protected_count].copy()
        final_created = created_at[:self._protected_count].copy()
        if chosen:
            final_refs = np.vstack((final_refs, adaptive[chosen]))
            final_qualities = np.concatenate((final_qualities, adaptive_qualities[chosen]))
            final_created = np.concatenate((final_created, adaptive_created[chosen]))
        self._atomic_save_profile(
            final_refs, self._protected_count, final_qualities, final_created
        )
        self._install_profile(
            final_refs, self._protected_count, final_qualities, final_created
        )
        return added
