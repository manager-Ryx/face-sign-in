"""Guided photo capture for setup: when to take a photo, and what to tell you.

Pure logic (no Qt), so it can be tested without a camera. Photos are taken
automatically when exactly one face is well lit, sharp, a sensible size,
held still, and turned the way the next photo needs: straight on first, then
slightly left, right, up and down, so the profile covers natural head
movement.
"""
from __future__ import annotations

import time
from dataclasses import dataclass, field

import numpy as np

from face_service.quality import FaceBox, ImageQuality, assess, primary_issue

POSES = ("center", "left", "right", "up", "down")
POSE_GUIDANCE = {
    "center": "Look straight at the camera.",
    "left": "Slowly turn your head a little to your left.",
    "right": "Slowly turn your head a little to your right.",
    "up": "Tilt your chin up slightly.",
    "down": "Tilt your chin down slightly.",
}
ISSUE_GUIDANCE = {
    "no_face": "Centre your face in the oval.",
    "multiple_faces": "Make sure only you are in view.",
    "too_dark": "Your face is too dark. Turn on a light or face a window.",
    "backlit": "There's bright light behind you. Face towards the light instead.",
    "too_bright": "There's too much light on your face.",
    "too_far": "Move a little closer.",
    "too_close": "Move back a little.",
    "partial": "Move into the middle of the oval.",
    "blurry": "Hold still for a moment.",
    "tilted": "Keep your head level.",
    "low_contrast": "More light on your face would help.",
}
TURN = 0.13          # yaw ratio change that counts as a slight turn
TILT = 0.08          # pitch ratio change that counts as a slight tilt
TOO_FAR_TURNED = 0.45
STABLE_FRAMES = 3
MIN_GAP_S = 0.6


def targets_for(mode: str) -> dict[str, int]:
    if mode == "append":
        return {"center": 4, "left": 2, "right": 2, "up": 1, "down": 1}
    return {"center": 8, "left": 3, "right": 3, "up": 3, "down": 3}


@dataclass
class Observation:
    guidance: str
    tone: str                 # "good", "issue", "idle"
    capture: bool
    pose: str | None
    box: FaceBox | None
    quality: ImageQuality | None


@dataclass
class CapturePlan:
    targets: dict[str, int]
    counts: dict[str, int] = field(default_factory=lambda: {p: 0 for p in POSES})
    baseline: tuple[float, float] | None = None
    _baseline_samples: list[tuple[float, float]] = field(default_factory=list)
    _last_center: tuple[float, float] | None = None
    _stable: int = 0
    _last_capture: float = 0.0

    @property
    def total(self) -> int:
        return sum(self.targets.values())

    @property
    def taken(self) -> int:
        return sum(min(self.counts[p], self.targets.get(p, 0)) for p in POSES)

    @property
    def done(self) -> bool:
        return all(self.counts[p] >= self.targets.get(p, 0) for p in POSES)

    def next_pose(self) -> str | None:
        # Straight-on photos come first: they define what "straight" means for you.
        for pose in POSES:
            if self.counts[pose] < self.targets.get(pose, 0):
                if pose != "center" and self.baseline is None:
                    return "center"
                return pose
        return None

    def classify(self, box: FaceBox) -> str | None:
        pose = box.pose()
        if pose is None:
            return None
        if self.baseline is None:
            return "center"
        dyaw, dpitch = pose[0] - self.baseline[0], pose[1] - self.baseline[1]
        if abs(dyaw) > TOO_FAR_TURNED:
            return "too-far-turned"
        if dyaw > TURN:
            return "left"
        if dyaw < -TURN:
            return "right"
        if dpitch < -TILT:
            return "up"
        if dpitch > TILT:
            return "down"
        return "center"

    def observe(self, gray: np.ndarray, faces: list[FaceBox],
                now: float | None = None) -> Observation:
        now = time.monotonic() if now is None else now
        wanted = self.next_pose()
        if wanted is None:
            return Observation("All photos taken.", "good", False, None, None, None)
        box = faces[0] if faces else None
        quality = assess(gray, box, face_count=len(faces))
        issue = primary_issue([i for i in quality.issues if i not in ("turned",)])
        if box is None or issue in ISSUE_GUIDANCE:
            self._stable = 0
            self._last_center = None
            return Observation(ISSUE_GUIDANCE.get(issue or "no_face", ""), "issue", False, None,
                               box, quality)
        center = (box.x + box.w / 2, box.y + box.h / 2)
        if self._last_center is not None:
            moved = max(abs(center[0] - self._last_center[0]),
                        abs(center[1] - self._last_center[1])) / gray.shape[1]
            self._stable = self._stable + 1 if moved < 0.025 else 0
        self._last_center = center
        pose = self.classify(box)
        if pose == "too-far-turned":
            return Observation("Not quite so far. Turn back a little.", "issue", False, pose,
                               box, quality)
        if pose != wanted:
            return Observation(POSE_GUIDANCE[wanted], "idle", False, pose, box, quality)
        if self._stable < STABLE_FRAMES:
            return Observation("Hold still…", "good", False, pose, box, quality)
        if now - self._last_capture < MIN_GAP_S:
            return Observation(POSE_GUIDANCE[wanted], "good", False, pose, box, quality)
        return Observation(POSE_GUIDANCE[wanted], "good", True, pose, box, quality)

    def record(self, pose: str, box: FaceBox, now: float | None = None) -> None:
        self.counts[pose] = self.counts.get(pose, 0) + 1
        self._last_capture = time.monotonic() if now is None else now
        self._stable = 0
        if pose == "center" and self.baseline is None:
            measured = box.pose()
            if measured is not None:
                self._baseline_samples.append(measured)
            if len(self._baseline_samples) >= 2:
                yaws, pitches = zip(*self._baseline_samples, strict=True)
                self.baseline = (float(np.median(yaws)), float(np.median(pitches)))
