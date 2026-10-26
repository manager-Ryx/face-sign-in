"""Face-region image quality: measurements, issues, and plain-English guidance.

Quality is assessed separately from identity. A dark or blurry frame is
reported as *unusable* (with guidance) instead of counting as a mismatch,
and it never loosens the identity threshold. The numeric bands below come
from measurements on the NearStream V02AF in evening light (face-region
brightness about 45-55 at 30 fps and 70-100 at 10-15 fps).
"""
from __future__ import annotations

import math
from dataclasses import dataclass, field

import cv2
import numpy as np

# Face-region mean luma bands (0-255).
TOO_DARK = 30.0
DARK = 40.0
DIM = 80.0
TOO_BRIGHT = 215.0
LOW_CONTRAST = 10.0
BLURRY = 12.0
TOO_FAR = 0.025       # face area / frame area
TOO_CLOSE = 0.40
TILTED_DEG = 25.0
TURNED_EYE_RATIO = 0.20
BACKLIGHT_GAP = 40.0

# Issues that make a non-matching frame inconclusive rather than a mismatch.
BLOCKING = frozenset({"too_dark", "too_bright", "blurry", "too_far", "turned", "partial"})

GUIDANCE_PRIORITY = (
    "no_face", "multiple_faces", "too_dark", "backlit", "too_bright", "partial",
    "too_far", "too_close", "turned", "tilted", "blurry", "low_contrast",
)


@dataclass(frozen=True)
class FaceBox:
    x: int
    y: int
    w: int
    h: int
    confidence: float = 1.0
    left_eye: tuple[int, int] | None = None
    right_eye: tuple[int, int] | None = None
    nose: tuple[int, int] | None = None

    def pose(self) -> tuple[float, float] | None:
        """Head-turn and head-tilt ratios from landmarks (0, 0 ≈ facing the camera).

        Positive yaw: nose towards image-right (your left, mirrored preview);
        positive pitch: nose low relative to the eyes (looking down).
        """
        if self.left_eye is None or self.right_eye is None or self.nose is None:
            return None
        mid_x = (self.left_eye[0] + self.right_eye[0]) / 2.0
        mid_y = (self.left_eye[1] + self.right_eye[1]) / 2.0
        span = math.hypot(self.left_eye[0] - self.right_eye[0],
                          self.left_eye[1] - self.right_eye[1])
        if span < 1:
            return None
        return (self.nose[0] - mid_x) / span, (self.nose[1] - mid_y) / span


@dataclass(frozen=True)
class ImageQuality:
    frame_brightness: float
    face_brightness: float = math.nan
    face_contrast: float = math.nan
    face_sharpness: float = math.nan
    face_ratio: float = 0.0
    roll_deg: float = 0.0
    eye_ratio: float = math.nan
    issues: tuple[str, ...] = field(default_factory=tuple)

    @property
    def usable(self) -> bool:
        return not BLOCKING.intersection(self.issues) and "no_face" not in self.issues

    @property
    def lighting(self) -> str:
        """Coarse lighting bucket used for learned camera estimates."""
        if "backlit" in self.issues:
            return "backlit"
        level = self.face_brightness if math.isfinite(self.face_brightness) else (
            self.frame_brightness * 2.0)
        if level < DARK:
            return "dark"
        if level < DIM:
            return "dim"
        return "bright"

    def score(self) -> float:
        """0..1 usefulness of this frame as a learning sample."""
        if not self.usable:
            return 0.0
        exposure = 1.0 - min(1.0, abs(self.face_brightness - 125.0) / 125.0)
        size = min(1.0, self.face_ratio / 0.12)
        sharp = min(1.0, self.face_sharpness / 120.0)
        frontal = min(1.0, self.eye_ratio / 0.35) if math.isfinite(self.eye_ratio) else 0.5
        return round(0.30 * exposure + 0.25 * size + 0.25 * sharp + 0.20 * frontal, 4)


def to_gray(bgr: np.ndarray) -> np.ndarray:
    return cv2.cvtColor(bgr, cv2.COLOR_BGR2GRAY) if bgr.ndim == 3 else bgr


def assess(gray: np.ndarray, box: FaceBox | None, *, face_count: int = 1) -> ImageQuality:
    """Measure frame and face-region quality and list any issues."""
    frame_brightness = float(gray.mean())
    if box is None or box.w <= 0 or box.h <= 0:
        return ImageQuality(frame_brightness, issues=("no_face",))

    height, width = gray.shape[:2]
    x0, y0 = max(0, box.x), max(0, box.y)
    x1, y1 = min(width, box.x + box.w), min(height, box.y + box.h)
    crop = gray[y0:y1, x0:x1]
    if crop.size == 0:
        return ImageQuality(frame_brightness, issues=("no_face",))

    face_brightness = float(crop.mean())
    face_contrast = float(crop.std())
    face_sharpness = float(cv2.Laplacian(crop, cv2.CV_64F).var())
    face_ratio = (x1 - x0) * (y1 - y0) / float(width * height)

    roll = 0.0
    eye_ratio = math.nan
    if box.left_eye is not None and box.right_eye is not None:
        dx = box.left_eye[0] - box.right_eye[0]
        dy = box.left_eye[1] - box.right_eye[1]
        distance = math.hypot(dx, dy)
        if distance > 0:
            roll = math.degrees(math.atan2(dy, dx))
            # Eye order depends on the detector's convention; fold to [-90, 90].
            if roll > 90:
                roll -= 180
            elif roll < -90:
                roll += 180
            eye_ratio = distance / max(1.0, float(box.w))

    issues: list[str] = []
    if face_count > 1:
        issues.append("multiple_faces")
    if face_brightness < TOO_DARK:
        issues.append("too_dark")
    elif face_brightness > TOO_BRIGHT:
        issues.append("too_bright")
    if frame_brightness - face_brightness > BACKLIGHT_GAP and face_brightness < DIM:
        issues.append("backlit")
    if face_contrast < LOW_CONTRAST:
        issues.append("low_contrast")
    if face_sharpness < BLURRY:
        issues.append("blurry")
    if face_ratio < TOO_FAR:
        issues.append("too_far")
    elif face_ratio > TOO_CLOSE:
        issues.append("too_close")
    if abs(roll) > TILTED_DEG:
        issues.append("tilted")
    if math.isfinite(eye_ratio) and eye_ratio < TURNED_EYE_RATIO:
        issues.append("turned")
    if box.x <= 0 or box.y <= 0 or box.x + box.w >= width or box.y + box.h >= height:
        issues.append("partial")
    return ImageQuality(frame_brightness, face_brightness, face_contrast, face_sharpness,
                        face_ratio, roll, eye_ratio, tuple(issues))


def primary_issue(issues: tuple[str, ...] | list[str]) -> str | None:
    """The single most useful thing to tell the user, if anything."""
    for issue in GUIDANCE_PRIORITY:
        if issue in issues:
            return issue
    return None
