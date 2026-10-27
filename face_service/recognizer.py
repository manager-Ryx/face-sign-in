"""Recognition: frame analysis, identity matching, enrollment, and learning.

Every frame is classified as one of the :class:`Outcome` values. Image
quality and identity are kept apart: a frame whose face matches the profile
is a match whatever its brightness; a frame that does not match is only
called a *mismatch* when the image was good enough to judge – otherwise it
is *unusable* and comes with guidance. Nothing here ever relaxes the
threshold.

Learning is a two-step transaction. ``stage_learning`` picks at most one
safe frame from a successful attempt and keeps it in memory under a random
attempt id. ``finish_learning`` commits it only when Windows reports that the
face-submitted credential was accepted. Missing, failed, late, or repeated
reports never change the profile.
"""
from __future__ import annotations

import logging
import math
import secrets
import threading
import time
from collections.abc import Callable, Iterable
from dataclasses import dataclass, field
from enum import StrEnum
from pathlib import Path

import cv2
import numpy as np

from .adaptive import LearningCandidate, choose_learning_sample, select_retained
from .config import EMBED_PATH, ENROLL_DIR, LEARNED_PHOTOS_DIR, Config
from .embedder import Embedder
from .metrics import MetricsStore, NullMetrics
from .profile import (
    SOURCE_ENROLLMENT,
    SOURCE_MANUAL,
    SOURCE_UNLOCK,
    FaceProfile,
    lighting_code,
    load_profile,
    save_profile,
    unit,
)
from .quality import ImageQuality, assess, primary_issue, to_gray

log = logging.getLogger(__name__)


def _day_slot(ts: float) -> int:
    local = time.localtime(ts)
    return (local.tm_hour * 3600 + local.tm_min * 60) // SLOT_S

IMAGE_SUFFIXES = {".jpg", ".jpeg", ".png"}
ENROLL_MIN_CONFIDENCE = 0.90
PENDING_TTL_S = 120.0
MAX_PENDING = 8
# Lighting problems a photo may have when you chose to keep it anyway.
LIGHT_ISSUES = frozenset({"too_dark", "too_bright"})
# Frames you could not be recognised in (another sign-in proved it was you):
# poor light is fine, but they still have to look like you.
POOR_LIGHT_MIN_CONFIDENCE = 0.80
POOR_LIGHT_SHARPNESS_FACTOR = 0.25
SLOT_S = 600                 # time-of-day slots of ten minutes
SLOT_FRESH_DAYS = 14.0
RECENT_KEEP_FRACTION = 0.2


class Outcome(StrEnum):
    MATCH = "match"
    UNCERTAIN = "uncertain"
    MISMATCH = "mismatch"
    UNUSABLE = "unusable"
    NO_FACE = "no_face"
    SPOOF = "spoof"
    ERROR = "error"


class NotEnrolledError(RuntimeError):
    """No face profile exists yet."""


@dataclass
class FrameAnalysis:
    outcome: Outcome
    distance: float = 1.0
    anchor_distance: float = 1.0
    quality: ImageQuality | None = None
    face_count: int = 0
    face_confidence: float = 0.0
    is_real: bool = True
    captured_at: float = 0.0
    analyze_ms: float = 0.0
    embedding: np.ndarray | None = field(default=None, repr=False)
    face_crop: np.ndarray | None = field(default=None, repr=False)

    @property
    def match(self) -> bool:
        return self.outcome is Outcome.MATCH

    @property
    def issue(self) -> str | None:
        if self.outcome is Outcome.NO_FACE:
            return "no_face"
        if self.outcome is Outcome.SPOOF:
            return "spoof"
        return primary_issue(self.quality.issues) if self.quality else None


@dataclass
class LearnResult:
    added: int
    reason: str
    sample_ids: list[int] = field(default_factory=list)


@dataclass
class ImageResult:
    name: str
    accepted: bool
    reason: str = ""
    quality: float = 0.0


@dataclass
class BuildReport:
    accepted: int
    rejected: int
    results: list[ImageResult]
    anchors: int
    learned: int
    consistency: float | None = None
    holdout: list[tuple[np.ndarray, str]] = field(default_factory=list, repr=False)


@dataclass
class _Pending:
    created: float
    attempt: str
    candidates: list[LearningCandidate]
    photos: list[np.ndarray | None]


class Recognizer:
    """Identity matching against an immutable, atomically swapped profile."""

    def __init__(self, cfg: Config, *, embedder: Embedder | None = None,
                 profile_path: Path | None = None, metrics: MetricsStore | None = None,
                 threshold: Callable[[], float] | None = None,
                 clock: Callable[[], float] = time.time,
                 monotonic: Callable[[], float] = time.monotonic):
        self.cfg = cfg
        self.embedder = embedder or Embedder(cfg.model_name, cfg.detector_backend,
                                             cfg.inference_threads)
        self.profile_path = profile_path or EMBED_PATH
        self.metrics = metrics or NullMetrics()
        self._threshold = threshold or (lambda: self.cfg.threshold)
        self._clock = clock
        self._monotonic = monotonic
        self._lock = threading.RLock()
        self._profile: FaceProfile | None = None
        self._pending: dict[str, _Pending] = {}
        self._skipped: dict[str, tuple[float, str]] = {}  # attempt -> (when, why nothing staged)

    # ---- profile -----------------------------------------------------------

    @property
    def profile(self) -> FaceProfile | None:
        return self._profile

    @property
    def threshold(self) -> float:
        return float(self._threshold())

    def load(self) -> bool:
        with self._lock:
            profile = load_profile(self.profile_path)
            if profile is None:
                self._profile = None
                return False
            self._profile = profile
            self._apply_retention_locked(persist=True)
            return True

    def require_profile(self) -> FaceProfile:
        profile = self._profile
        if profile is None and self.load():
            profile = self._profile
        if profile is None:
            raise NotEnrolledError("No face profile yet. Set up face recognition first.")
        return profile

    def profile_counts(self) -> tuple[int, int]:
        profile = self._profile
        if profile is None and self.load():
            profile = self._profile
        return (profile.protected_count, profile.learned_count) if profile else (0, 0)

    def _install_locked(self, profile: FaceProfile, *, persist: bool) -> None:
        if persist:
            save_profile(profile, self.profile_path)
        self._profile = profile

    def _apply_retention_locked(self, *, persist: bool) -> int:
        profile = self._profile
        if profile is None or profile.learned_count == 0:
            return 0
        protected = profile.protected_count
        days = self.cfg.adaptive_retention_days
        cutoff = -math.inf if days <= 0 else self._clock() - days * 86400.0
        created = profile.created_at[protected:].copy()
        # Earlier versions could write samples without a usable timestamp;
        # start their age now instead of deleting them unexpectedly.
        created[~np.isfinite(created) | (created <= 0)] = self._clock()
        limit = self.cfg.adaptive_max_embeddings
        keep = select_retained(
            profile.embeddings[protected:], profile.embeddings[:protected], created,
            profile.qualities[protected:], limit=limit, cutoff=cutoff,
            recent_keep=max(1, int(limit * RECENT_KEEP_FRACTION)))
        timestamps_changed = not np.array_equal(created, profile.created_at[protected:])
        if len(keep) == profile.learned_count and not timestamps_changed:
            return 0
        refreshed = FaceProfile.build(
            profile.embeddings, protected_count=protected, sample_ids=profile.sample_ids,
            sources=profile.sources,
            created_at=np.concatenate((profile.created_at[:protected], created)),
            qualities=profile.qualities, lighting=profile.lighting,
            anchor_files=profile.anchor_files, next_id=profile.next_id)
        trimmed = refreshed.keep_learned(keep)
        removed = profile.learned_count - trimmed.learned_count
        self._install_locked(trimmed, persist=persist)
        if removed:
            self.metrics.record("learned_evicted", count=removed, kept=trimmed.learned_count)
        return removed

    # ---- analysis ----------------------------------------------------------

    def analyze(self, bgr: np.ndarray, *, captured_at: float | None = None,
                threshold: float | None = None, uncertain_band: float = 0.08,
                keep_crop: bool = False) -> FrameAnalysis:
        """Detect, assess, and match one frame."""
        started = time.perf_counter()
        profile = self.require_profile()
        threshold = self.threshold if threshold is None else threshold
        captured_at = self._monotonic() if captured_at is None else captured_at
        gray = to_gray(bgr)
        faces = self.embedder.detect(bgr, min_confidence=self.cfg.detector_min_confidence,
                                     anti_spoofing=self.cfg.anti_spoofing)
        if not faces:
            return FrameAnalysis(Outcome.NO_FACE, quality=assess(gray, None),
                                 captured_at=captured_at,
                                 analyze_ms=(time.perf_counter() - started) * 1000.0)
        face = faces[0]
        quality = assess(gray, face.box, face_count=len(faces))
        common = dict(quality=quality, face_count=len(faces),
                      face_confidence=face.box.confidence, captured_at=captured_at)
        if self.cfg.anti_spoofing and face.is_real is False:
            return FrameAnalysis(Outcome.SPOOF, is_real=False, **common,
                                 analyze_ms=(time.perf_counter() - started) * 1000.0)
        embedding = self.embedder.embed([face])[0]
        distances = profile.distances(embedding)
        best = float(distances.min()) if distances.size else 1.0
        anchor = (float(distances[: profile.protected_count].min())
                  if profile.protected_count else 1.0)
        if best <= threshold:
            outcome = Outcome.MATCH
        elif not quality.usable:
            outcome = Outcome.UNUSABLE
        elif best <= threshold + uncertain_band:
            outcome = Outcome.UNCERTAIN
        else:
            outcome = Outcome.MISMATCH
        crop = None
        if keep_crop and outcome is Outcome.MATCH:
            b = face.box
            crop = bgr[max(0, b.y): b.y + b.h, max(0, b.x): b.x + b.w].copy()
        return FrameAnalysis(outcome, best, anchor, embedding=embedding, face_crop=crop,
                             analyze_ms=(time.perf_counter() - started) * 1000.0, **common)

    # ---- enrollment --------------------------------------------------------

    def _embed_images(self, paths: Iterable[Path], *, allow_poor_light: bool = False
                      ) -> tuple[list[np.ndarray], list[str], list[float], list[str], list[ImageResult]]:
        embeddings, names, qualities, lighting, results = [], [], [], [], []
        for path in paths:
            image = cv2.imread(str(path))
            if image is None:
                results.append(ImageResult(path.name, False, "could not be read"))
                continue
            faces = self.embedder.detect(image, min_confidence=ENROLL_MIN_CONFIDENCE)
            if len(faces) != 1:
                reason = "no clear face" if not faces else "more than one face"
                results.append(ImageResult(path.name, False, reason))
                continue
            quality = assess(to_gray(image), faces[0].box, face_count=1)
            issues = [i for i in quality.issues if not (allow_poor_light and i in LIGHT_ISSUES)]
            if not quality.usable and (not allow_poor_light or primary_issue(issues)):
                issue = primary_issue(issues) or "image quality too low"
                results.append(ImageResult(path.name, False, issue.replace("_", " ")))
                continue
            embeddings.append(self.embedder.embed(faces)[0])
            names.append(path.name)
            qualities.append(max(quality.score(), 0.05))
            lighting.append(quality.lighting)
            results.append(ImageResult(path.name, True, quality=quality.score()))
        return embeddings, names, qualities, lighting, results

    def build_profile(self, paths: list[Path], *, mode: str = "replace",
                      source: int = SOURCE_ENROLLMENT, dry_run: bool = False,
                      holdout_every: int = 0, allow_poor_light: bool = False) -> BuildReport:
        """Create (``replace``) or extend (``append``) the original enrollment.

        Replacing starts a fresh profile and discards learned samples, which
        were only valid relative to the old enrollment. Appending keeps both.
        With ``holdout_every=N``, every Nth usable photo is kept aside as an
        evaluation sample instead of becoming part of the profile.
        """
        if mode not in ("replace", "append"):
            raise ValueError("mode must be 'replace' or 'append'")
        embeddings, names, qualities, lighting, results = self._embed_images(
            paths, allow_poor_light=allow_poor_light)
        if not embeddings:
            raise ValueError("No usable face was found in the photos")
        holdout: list[tuple[np.ndarray, str]] = []
        if holdout_every > 1 and len(embeddings) >= 2 * holdout_every:
            keep = [i for i in range(len(embeddings)) if (i + 1) % holdout_every]
            holdout = [(embeddings[i], lighting[i]) for i in range(len(embeddings))
                       if not (i + 1) % holdout_every]
            embeddings = [embeddings[i] for i in keep]
            names = [names[i] for i in keep]
            qualities = [qualities[i] for i in keep]
            lighting = [lighting[i] for i in keep]
        new = np.stack(embeddings)
        now = self._clock()
        with self._lock:
            current = self._profile if mode == "append" else None
            if mode == "append" and current is None:
                current = load_profile(self.profile_path)
            consistency = None
            if current is not None and current.protected_count:
                anchors_unit = current.unit_rows[: current.protected_count]
                nearest = [float(np.min(1.0 - anchors_unit @ unit(e))) for e in new]
                consistency = float(np.median(nearest))
            if current is None:
                profile = FaceProfile.build(
                    new, protected_count=len(new),
                    sources=np.full(len(new), source, dtype=np.int8),
                    created_at=np.full(len(new), now),
                    qualities=np.asarray(qualities, dtype=np.float32),
                    lighting=np.asarray([lighting_code(x) for x in lighting], dtype=np.int8),
                    anchor_files=tuple(names))
            else:
                p = current.protected_count
                new_ids = np.arange(current.next_id, current.next_id + len(new), dtype=np.int64)
                files = (list(current.anchor_files) or [""] * p) + names
                profile = FaceProfile.build(
                    np.vstack((current.embeddings[:p], new, current.embeddings[p:])),
                    protected_count=p + len(new),
                    sample_ids=np.concatenate((current.sample_ids[:p], new_ids,
                                               current.sample_ids[p:])),
                    sources=np.concatenate((current.sources[:p],
                                            np.full(len(new), source, dtype=np.int8),
                                            current.sources[p:])),
                    created_at=np.concatenate((current.created_at[:p], np.full(len(new), now),
                                               current.created_at[p:])),
                    qualities=np.concatenate((current.qualities[:p],
                                              np.asarray(qualities, dtype=np.float32),
                                              current.qualities[p:])),
                    lighting=np.concatenate((current.lighting[:p],
                                             np.asarray([lighting_code(x) for x in lighting],
                                                        dtype=np.int8),
                                             current.lighting[p:])),
                    anchor_files=tuple(files), next_id=current.next_id + len(new))
            report = BuildReport(len(new), sum(1 for r in results if not r.accepted), results,
                                 profile.protected_count, profile.learned_count, consistency,
                                 holdout)
            if not dry_run:
                self._install_locked(profile, persist=True)
                self._pending.clear()
                self._skipped.clear()
                self.metrics.record("profile_built", mode=mode, accepted=report.accepted,
                                    rejected=report.rejected, anchors=report.anchors,
                                    learned=report.learned, consistency=consistency)
            return report

    def enroll_from_dir(self, directory: Path = ENROLL_DIR) -> int:
        """Rebuild the enrollment from every photo in ``directory``."""
        directory.mkdir(parents=True, exist_ok=True)
        paths = sorted(p for p in directory.iterdir() if p.suffix.lower() in IMAGE_SUFFIXES)
        if not paths:
            raise ValueError(f"No enrollment photos in {directory}")
        return self.build_profile(paths, mode="replace").accepted

    def map_anchor_files(self, directory: Path = ENROLL_DIR) -> int:
        """Recover which enrollment photo produced each anchor (legacy profiles)."""
        profile = self.require_profile()
        if profile.anchor_files and all(profile.anchor_files):
            return len(profile.anchor_files)
        paths = sorted(p for p in directory.iterdir() if p.suffix.lower() in IMAGE_SUFFIXES)
        names = [""] * profile.protected_count
        anchors_unit = profile.unit_rows[: profile.protected_count]
        for path in paths:
            image = cv2.imread(str(path))
            if image is None:
                continue
            faces = self.embedder.detect(image, min_confidence=ENROLL_MIN_CONFIDENCE)
            if len(faces) != 1:
                continue
            distances = 1.0 - anchors_unit @ unit(self.embedder.embed(faces)[0])
            index = int(np.argmin(distances))
            if distances[index] < 1e-3 and not names[index]:
                names[index] = path.name
        with self._lock:
            if self._profile is profile:
                self._install_locked(profile.with_anchor_files(tuple(names)), persist=True)
        return sum(1 for n in names if n)

    def profile_health(self) -> dict:
        """Flag original photos that look unlike the rest (possible poor captures)."""
        profile = self.require_profile()
        p = profile.protected_count
        if p < 4:
            return {"anchors": p, "outliers": []}
        anchors = profile.unit_rows[:p]
        pairwise = 1.0 - anchors @ anchors.T
        np.fill_diagonal(pairwise, np.nan)
        typical = np.nanmedian(pairwise, axis=1)
        q1, q3 = np.percentile(typical, [25, 75])
        limit = max(0.55, q3 + 1.5 * (q3 - q1))
        outliers = [{"index": int(i), "file": profile.anchor_files[i] if i < len(
            profile.anchor_files) else "", "typical_distance": round(float(typical[i]), 3)}
            for i in np.flatnonzero(typical > limit)]
        return {"anchors": p, "median_distance": round(float(np.median(typical)), 3),
                "outlier_limit": round(float(limit), 3), "outliers": outliers}

    def remove_anchors(self, indices: Iterable[int]) -> list[str]:
        """Remove original photos the user chose to drop; returns their file names."""
        with self._lock:
            profile = self.require_profile()
            drop = {i for i in indices if 0 <= i < profile.protected_count}
            if not drop or len(drop) >= profile.protected_count:
                return []
            removed_files = [profile.anchor_files[i] for i in sorted(drop)
                             if i < len(profile.anchor_files) and profile.anchor_files[i]]
            keep_rows = [i for i in range(profile.size) if i not in drop]
            index = np.asarray(keep_rows)
            files = [f for i, f in enumerate(profile.anchor_files) if i not in drop]
            trimmed = FaceProfile.build(
                profile.embeddings[index], protected_count=profile.protected_count - len(drop),
                sample_ids=profile.sample_ids[index], sources=profile.sources[index],
                created_at=profile.created_at[index], qualities=profile.qualities[index],
                lighting=profile.lighting[index], anchor_files=tuple(files),
                next_id=profile.next_id)
            self._install_locked(trimmed, persist=True)
            self.metrics.record("anchors_removed", count=len(drop))
            return removed_files or [""] * len(drop)

    # ---- learning ----------------------------------------------------------

    def _prune_pending_locked(self) -> None:
        cutoff = self._monotonic() - PENDING_TTL_S
        for attempt, pending in list(self._pending.items()):
            if pending.created < cutoff:
                self._pending.pop(attempt, None)
                self.metrics.record("learning_expired")
        while len(self._pending) >= MAX_PENDING:
            oldest = min(self._pending, key=lambda key: self._pending[key].created)
            self._pending.pop(oldest, None)
        for attempt, (created, _why) in list(self._skipped.items()):
            if created < cutoff or len(self._skipped) > MAX_PENDING:
                self._skipped.pop(attempt, None)

    def _skip(self, attempt_id: str | None, why: str) -> None:
        """Remember why nothing was staged, so the sign-in result can say so."""
        if attempt_id is not None:
            with self._lock:
                self._prune_pending_locked()
                self._skipped[attempt_id] = (self._monotonic(), why)

    def stage_learning(self, analyses: list[FrameAnalysis], *,
                       attempt_id: str | None = None) -> str | None:
        """Pick the best safe frame of a successful attempt; nothing is saved yet."""
        cfg = self.cfg
        if not cfg.adaptive_enrollment or cfg.adaptive_samples_per_unlock <= 0:
            return self._skip(attempt_id, "learning is off")
        profile = self._profile
        if profile is None:
            return self._skip(attempt_id, "no face profile")
        candidates = [
            LearningCandidate(
                a.embedding, a.quality.score() if a.quality else 0.0,
                a.quality.lighting if a.quality else "unknown", a.face_confidence,
                a.quality.face_ratio if a.quality else 0.0,
                a.quality.face_sharpness if a.quality else 0.0, a.is_real, a.face_count)
            for a in analyses if a.match and a.embedding is not None]
        chosen, photos = self._choose(candidates, profile, analyses)
        if not chosen:
            reasons = [c.rejected for c in candidates if c.rejected]
            reason = max(set(reasons), key=reasons.count) if reasons else "no matching frame"
            self.metrics.record("learning_skipped", reason=reason, frames=len(candidates))
            return self._skip(attempt_id, reason)
        with self._lock:
            self._prune_pending_locked()
            attempt = attempt_id or secrets.token_urlsafe(18)
            self._pending[attempt] = _Pending(self._monotonic(), attempt, chosen, photos)
        self.metrics.record("learning_staged", quality=chosen[0].quality,
                            novelty=chosen[0].novelty, anchor_distance=chosen[0].anchor_distance)
        return attempt

    def _choose(self, candidates: list[LearningCandidate], profile: FaceProfile,
                analyses: list[FrameAnalysis]
                ) -> tuple[list[LearningCandidate], list[np.ndarray | None]]:
        cfg = self.cfg
        anchors = profile.unit_rows[: profile.protected_count]
        stored = profile.unit_rows
        chosen: list[LearningCandidate] = []
        photos: list[np.ndarray | None] = []
        crops = {id(a.embedding): a.face_crop for a in analyses if a.embedding is not None}
        remaining = list(candidates)
        for _ in range(cfg.adaptive_samples_per_unlock):
            best = choose_learning_sample(
                remaining, anchors, stored, threshold=self.threshold,
                match_margin=cfg.adaptive_match_margin, min_diversity=cfg.adaptive_min_diversity,
                min_face_confidence=cfg.adaptive_min_face_confidence,
                min_face_ratio=cfg.adaptive_min_face_area_ratio,
                min_sharpness=cfg.adaptive_min_blur)
            if best is None:
                break
            chosen.append(best)
            photos.append(crops.get(id(best.embedding)) if cfg.adaptive_keep_photos else None)
            stored = np.vstack((stored, unit(best.embedding)[None, :]))
            remaining = [c for c in remaining if c is not best]
        return chosen, photos

    def finish_learning(self, attempt: str, accepted: bool) -> LearnResult:
        """Commit a staged sample only after Windows confirmed the sign-in."""
        with self._lock:
            self._prune_pending_locked()
            pending = self._pending.pop(attempt, None)
            if pending is None:
                skipped = self._skipped.pop(attempt, None)
                why = (f"nothing to learn: {skipped[1]}" if skipped
                       else "unknown, late, or repeated report")
                self.metrics.record("learning_result", accepted=accepted, added=0, reason=why)
                return LearnResult(0, why)
            if not accepted:
                self.metrics.record("learning_result", accepted=False, added=0,
                                    reason="Windows did not accept the sign-in")
                return LearnResult(0, "Windows did not accept the sign-in")
            profile = self._profile
            if profile is None:
                return LearnResult(0, "no profile")
            # The profile may have changed since staging; re-check safety.
            survivors, photos = [], []
            for candidate, photo in zip(pending.candidates, pending.photos, strict=True):
                check = choose_learning_sample(
                    [LearningCandidate(candidate.embedding, candidate.quality,
                                       candidate.lighting, candidate.face_confidence,
                                       candidate.face_ratio, candidate.sharpness,
                                       candidate.is_real, candidate.face_count)],
                    profile.unit_rows[: profile.protected_count], profile.unit_rows,
                    threshold=self.threshold, match_margin=self.cfg.adaptive_match_margin,
                    min_diversity=self.cfg.adaptive_min_diversity,
                    min_face_confidence=self.cfg.adaptive_min_face_confidence,
                    min_face_ratio=self.cfg.adaptive_min_face_area_ratio,
                    min_sharpness=self.cfg.adaptive_min_blur)
                if check is not None:
                    survivors.append(check)
                    photos.append(photo)
            if not survivors:
                self.metrics.record("learning_result", accepted=True, added=0,
                                    reason="no longer distinct from stored samples")
                return LearnResult(0, "no longer distinct from stored samples")
            return self._commit_locked(profile, survivors, photos, SOURCE_UNLOCK)

    def _commit_locked(self, profile: FaceProfile, chosen: list[LearningCandidate],
                       photos: list[np.ndarray | None], source: int) -> LearnResult:
        updated, ids = profile.append(
            np.stack([c.embedding for c in chosen]), source=source,
            created_at=self._clock(),
            qualities=np.asarray([c.quality for c in chosen], dtype=np.float32),
            lighting=np.asarray([lighting_code(c.lighting) for c in chosen], dtype=np.int8))
        self._install_locked(updated, persist=False)
        self._apply_retention_locked(persist=False)
        save_profile(self._profile, self.profile_path)  # type: ignore[arg-type]
        for candidate, sample_id in zip(chosen, ids, strict=True):
            self.metrics.record(
                "learned_sample", sample_id=sample_id, quality=candidate.quality,
                anchor_distance=candidate.anchor_distance, novelty=candidate.novelty,
                lighting=candidate.lighting, source=source)
        for photo, sample_id in zip(photos, ids, strict=True):
            if photo is not None:
                self._save_photo(photo, sample_id)
        self.metrics.record("learning_result", accepted=True, added=len(ids), reason="learned")
        return LearnResult(len(ids), "learned", ids)

    def learn_now(self, analyses: list[FrameAnalysis], *, source: int, limit: int,
                  max_anchor_distance: float, poor_light: bool = False) -> LearnResult:
        """Learn up to ``limit`` distinct views at once, when it is known to be you.

        Used after a password or PIN sign-in (``poor_light``: the views the
        sign-in screen could not recognise) and while you sit at the PC.
        Every view must still be a single face within ``max_anchor_distance``
        of your *original* photos, so learned samples never drift away from
        you, and nearly identical views are skipped.
        """
        cfg = self.cfg
        if not cfg.adaptive_enrollment:
            return LearnResult(0, "learning is off")
        sharpness = cfg.adaptive_min_blur * (POOR_LIGHT_SHARPNESS_FACTOR if poor_light else 1.0)
        confidence = (min(cfg.adaptive_min_face_confidence, POOR_LIGHT_MIN_CONFIDENCE)
                      if poor_light else cfg.adaptive_min_face_confidence)
        candidates = [
            LearningCandidate(
                a.embedding, max(a.quality.score() if a.quality else 0.0,
                                 0.05 if poor_light else 0.0),
                a.quality.lighting if a.quality else "unknown", a.face_confidence,
                a.quality.face_ratio if a.quality else 0.0,
                a.quality.face_sharpness if a.quality else 0.0, a.is_real, a.face_count)
            for a in analyses if a.embedding is not None]
        with self._lock:
            profile = self._profile
            if profile is None or not candidates:
                return LearnResult(0, "nothing to learn")
            anchors = profile.unit_rows[: profile.protected_count]
            stored = profile.unit_rows
            chosen: list[LearningCandidate] = []
            remaining = list(candidates)
            while remaining and len(chosen) < limit:
                best = choose_learning_sample(
                    remaining, anchors, stored, threshold=max_anchor_distance, match_margin=0.0,
                    min_diversity=cfg.adaptive_min_diversity, min_face_confidence=confidence,
                    min_face_ratio=cfg.adaptive_min_face_area_ratio, min_sharpness=sharpness)
                if best is None:
                    break
                chosen.append(best)
                stored = np.vstack((stored, unit(best.embedding)[None, :]))
                remaining = [c for c in remaining if c is not best]
            if not chosen:
                reasons = [c.rejected for c in candidates if c.rejected]
                why = max(set(reasons), key=reasons.count) if reasons else "nothing to learn"
                self.metrics.record("learning_skipped", reason=why, frames=len(candidates),
                                    source=source)
                return LearnResult(0, why)
            return self._commit_locked(profile, chosen, [None] * len(chosen), source)

    def slot_recently_learned(self, ts: float) -> bool:
        """Was a view learned in this ten-minute slot of the day within two weeks?"""
        profile = self._profile
        if profile is None or not profile.learned_count:
            return False
        created = profile.created_at[profile.protected_count:]
        recent = created[created >= ts - SLOT_FRESH_DAYS * 86400.0]
        slot = _day_slot(ts)
        return any(_day_slot(float(t)) == slot for t in recent)

    def _save_photo(self, crop: np.ndarray, sample_id: int) -> None:
        try:
            LEARNED_PHOTOS_DIR.mkdir(parents=True, exist_ok=True)
            cv2.imwrite(str(LEARNED_PHOTOS_DIR / f"sample-{sample_id}.jpg"), crop)
            photos = sorted(LEARNED_PHOTOS_DIR.glob("sample-*.jpg"),
                            key=lambda p: p.stat().st_mtime)
            for old in photos[: max(0, len(photos) - self.cfg.adaptive_photo_limit)]:
                old.unlink(missing_ok=True)
        except OSError as exc:
            log.warning("learned photo not saved: %s", exc)

    def undo_learning(self, *, since: float | None = None,
                      sample_ids: Iterable[int] | None = None) -> int:
        """Remove learned samples by id or learned after ``since``."""
        with self._lock:
            profile = self.require_profile()
            p = profile.protected_count
            learned = range(p, profile.size)
            if sample_ids is not None:
                targets = {int(i) for i in sample_ids}
            elif since is not None:
                targets = {int(profile.sample_ids[i]) for i in learned
                           if profile.created_at[i] >= since and profile.sources[i] != SOURCE_MANUAL}
            else:
                return 0
            updated = profile.without_ids(targets)
            removed = profile.size - updated.size
            if removed:
                self._install_locked(updated, persist=True)
                self.metrics.record("learning_undone", count=removed)
            return removed

    def reset_learning(self) -> int:
        """Remove every learned sample; the original enrollment is untouched."""
        with self._lock:
            profile = self.require_profile()
            removed = profile.learned_count
            if removed:
                self._install_locked(profile.keep_learned([]), persist=True)
                self.metrics.record("learning_reset", count=removed)
            for photo in LEARNED_PHOTOS_DIR.glob("sample-*.jpg"):
                photo.unlink(missing_ok=True)
            self._pending.clear()
            self._skipped.clear()
            return removed

    def learned_samples(self, limit: int = 50) -> list[dict]:
        """Newest learned samples, without any biometric data."""
        profile = self._profile
        if profile is None:
            return []
        rows = range(profile.protected_count, profile.size)
        items = [{"id": int(profile.sample_ids[i]), "at": float(profile.created_at[i]),
                  "quality": round(float(profile.qualities[i]), 3),
                  "source": int(profile.sources[i]),
                  "lighting": int(profile.lighting[i])} for i in rows]
        items.sort(key=lambda item: item["at"], reverse=True)
        return items[:limit]


__all__ = ["Outcome", "FrameAnalysis", "Recognizer", "NotEnrolledError", "LearnResult",
           "BuildReport", "ImageResult", "SOURCE_MANUAL", "SOURCE_ENROLLMENT"]
