"""Lightweight YuNet face *detector* (not a recognizer).

Used where identity is not needed: the "any face" presence mode, the
enrollment preview, and quick checks of whether anyone is in view. Fast, and
needs neither TensorFlow nor an enrollment.
"""
from __future__ import annotations

import logging
from pathlib import Path

import cv2
import numpy as np

from .quality import FaceBox

log = logging.getLogger(__name__)

_REPO_ROOT = Path(__file__).resolve().parent.parent
_BUNDLED_MODEL = _REPO_ROOT / "models" / "face_detection_yunet_2023mar.onnx"
_LEGACY_MODEL = Path(
    r"C:\Program Files\facewinunlock-tauri\resources\face_detection_yunet_2023mar.onnx")


def yunet_model_path() -> Path:
    if _BUNDLED_MODEL.exists():
        return _BUNDLED_MODEL
    if _LEGACY_MODEL.exists():
        return _LEGACY_MODEL
    raise FileNotFoundError(
        f"YuNet model not found. Expected at {_BUNDLED_MODEL} or {_LEGACY_MODEL}.")


class FaceDetector:
    """Any-face detector backed by OpenCV's YuNet."""

    def __init__(self, score_threshold: float = 0.7, nms_threshold: float = 0.3,
                 top_k: int = 50):
        self.score_threshold = score_threshold
        self.nms_threshold = nms_threshold
        self.top_k = top_k
        self._impl: cv2.FaceDetectorYN | None = None
        self._size: tuple[int, int] | None = None

    def _ensure(self, width: int, height: int) -> cv2.FaceDetectorYN:
        if self._impl is None:
            self._impl = cv2.FaceDetectorYN.create(
                str(yunet_model_path()), "", (width, height),
                self.score_threshold, self.nms_threshold, self.top_k)
            self._size = (width, height)
        elif self._size != (width, height):
            self._impl.setInputSize((width, height))
            self._size = (width, height)
        return self._impl

    def detect(self, bgr: np.ndarray) -> list[FaceBox]:
        """All faces, largest first, with eye landmarks."""
        height, width = bgr.shape[:2]
        _, faces = self._ensure(width, height).detect(bgr)
        if faces is None:
            return []
        boxes = []
        for row in faces:
            x, y, w, h = (int(v) for v in row[:4])
            # YuNet landmark order: right eye, left eye (from the subject's view), nose tip.
            right_eye = (int(row[4]), int(row[5]))
            left_eye = (int(row[6]), int(row[7]))
            nose = (int(row[8]), int(row[9]))
            boxes.append(FaceBox(x, y, w, h, float(row[-1]), left_eye, right_eye, nose))
        boxes.sort(key=lambda b: b.w * b.h, reverse=True)
        return boxes

    def detect_largest(self, bgr: np.ndarray) -> FaceBox | None:
        faces = self.detect(bgr)
        return faces[0] if faces else None

    def has_face(self, bgr: np.ndarray) -> bool:
        return bool(self.detect(bgr))
