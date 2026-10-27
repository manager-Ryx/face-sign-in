"""Shared test doubles: synthetic embeddings, frames, cameras and recognizers."""
from __future__ import annotations

from dataclasses import dataclass

import _isolation  # noqa: F401  (must come first)
import numpy as np

from face_service.quality import FaceBox, ImageQuality
from face_service.recognizer import FrameAnalysis, Outcome

DIM = 8


def direction(degrees: float, dim: int = DIM) -> np.ndarray:
    """Unit vector at ``degrees`` in the first plane; cosine distance = 1 - cos(angle)."""
    radians = np.deg2rad(degrees)
    vector = np.zeros(dim, dtype=np.float32)
    vector[0], vector[1] = np.cos(radians), np.sin(radians)
    return vector


def distance_for(degrees: float) -> float:
    return float(1.0 - np.cos(np.deg2rad(degrees)))


GOOD = ImageQuality(frame_brightness=110.0, face_brightness=120.0, face_contrast=40.0,
                    face_sharpness=150.0, face_ratio=0.09, roll_deg=0.0, eye_ratio=0.38,
                    issues=())
DARK = ImageQuality(frame_brightness=15.0, face_brightness=20.0, face_contrast=8.0,
                    face_sharpness=8.0, face_ratio=0.09, roll_deg=0.0, eye_ratio=0.38,
                    issues=("too_dark", "low_contrast", "blurry"))


def frame(outcome: Outcome, distance: float = 0.3, t: float = 0.0, *,
          quality: ImageQuality | None = GOOD, embedding: np.ndarray | None = None,
          face_count: int = 1) -> FrameAnalysis:
    if outcome is Outcome.NO_FACE:
        return FrameAnalysis(outcome, quality=ImageQuality(20.0, issues=("no_face",)),
                             captured_at=t)
    return FrameAnalysis(outcome, distance, distance, quality=quality, face_count=face_count,
                         face_confidence=0.95, captured_at=t, embedding=embedding)


@dataclass
class FakeBox:
    box: FaceBox


class FakeEmbedder:
    """Maps a frame's first pixel value to a direction, so tests choose identities."""

    loaded = True
    compiled_ok = True

    def __init__(self, angles: dict[int, float] | None = None, faces: int = 1):
        self.angles = angles or {}
        self.faces = faces
        self.spoof_available = True
        self.spoof_error: str | None = None

    def load(self) -> None:
        pass

    def load_spoof_model(self) -> bool:
        self.spoof_error = None if self.spoof_available else "offline"
        return self.spoof_available

    def detect(self, bgr, *, min_confidence: float, anti_spoofing: bool = False):
        from face_service.embedder import DetectedFace, SpoofModelUnavailable
        if anti_spoofing and not self.load_spoof_model():
            raise SpoofModelUnavailable(self.spoof_error)
        key = int(bgr[0, 0, 0])
        if key not in self.angles or self.faces == 0:
            return []
        box = FaceBox(200, 120, 220, 260, 0.95, (360, 200), (260, 200))
        return [DetectedFace(box, np.full((1, 2), key, dtype=np.float32), True, 1.0)
                for _ in range(self.faces)]

    def embed(self, faces):
        return np.stack([direction(self.angles[int(f.tensor[0, 0])]) for f in faces])


def image(key: int, brightness: int = 120) -> np.ndarray:
    """A 640x480 frame whose first pixel encodes ``key`` for FakeEmbedder."""
    img = np.full((480, 640, 3), brightness, dtype=np.uint8)
    rng = np.random.default_rng(key)
    img[120:380, 200:420] = rng.integers(60, 200, size=(260, 220, 3), dtype=np.uint8)
    img[0, 0] = key
    return img
