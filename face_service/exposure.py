"""Tune the camera for your face: brightness against a bright window, and focus for your desk.

Cameras expose for the whole picture, so a bright window behind you leaves
your face dark – measured on this PC on a morning: face 42 against a scene of
121 (0-255). The NearStream V02AF's driver offers no exposure, gain or
backlight-compensation control, but its brightness control works: raising it
from 124 to 164 lifted the face to 122. So when a face is visible and badly
lit, brightness is stepped towards a well-lit face (at most three steps,
starting from what worked last time in the same light). Nothing changes when
nobody is in view, and every change is undone when the capture closes
(see :class:`face_service.camera.Camera`).

Autofocus can hunt, especially at night. :func:`calibrate_focus` sweeps the
lens while you sit at your desk and picks the position where your face is
sharpest; that position can then be held for every capture.
"""
from __future__ import annotations

import logging
import statistics
from collections.abc import Callable
from dataclasses import dataclass
from typing import Protocol

import cv2
import numpy as np

from .quality import FaceBox, assess, to_gray

log = logging.getLogger(__name__)

BRIGHTNESS = cv2.CAP_PROP_BRIGHTNESS
FACE_TARGET = 115.0
FACE_LOW, FACE_HIGH = 80.0, 165.0
GAIN = 0.5                 # brightness units per grey level of face brightness (measured ~0.5)
MAX_RAISE, MAX_LOWER = 100.0, 60.0
MAX_STEPS = 3
SETTLE_FRAMES = 3          # frames for a brightness change to show
MIN_STEP = 4.0

FOCUS_SWEEP = tuple(range(100, 751, 50))
FOCUS_FINE = 20
FOCUS_DISCARD, FOCUS_MEASURE = 3, 5


class Detector(Protocol):
    def detect_largest(self, bgr: np.ndarray) -> FaceBox | None: ...


class TunableCamera(Protocol):
    def control(self, prop: int) -> float | None: ...
    def original(self, prop: int) -> float | None: ...
    def set_control(self, prop: int, value: float) -> bool: ...
    def read_frame(self) -> np.ndarray: ...


@dataclass
class Exposure:
    frame: np.ndarray | None
    offset: float = 0.0               # brightness relative to the camera's own setting
    face_before: float | None = None
    face_after: float | None = None
    steps: int = 0
    lighting: str = ""

    @property
    def adjusted(self) -> bool:
        return self.steps > 0


def _face(frame: np.ndarray, detector: Detector) -> tuple[float, str] | None:
    box = detector.detect_largest(frame)
    if box is None:
        return None
    quality = assess(to_gray(frame), box)
    return quality.face_brightness, quality.lighting


def expose_for_face(camera: TunableCamera, frame: np.ndarray | None, detector: Detector, *,
                    start_offset: Callable[[str], float | None] | None = None) -> Exposure:
    """Step the camera's brightness until the face in view is well lit.

    ``start_offset(lighting)`` may suggest where to start: what worked before in
    the same kind of light.
    """
    if frame is None or not hasattr(camera, "set_control"):
        return Exposure(frame)
    base = camera.original(BRIGHTNESS)
    seen = _face(frame, detector)
    if base is None or seen is None:
        return Exposure(frame)            # no control, or nobody there to expose for
    face, lighting = seen
    result = Exposure(frame, face_before=face, face_after=face, lighting=lighting)
    start = start_offset(lighting) if start_offset is not None else None
    offset = 0.0
    current = camera.control(BRIGHTNESS)
    if current is not None:
        offset = current - base            # a held camera may already be adjusted
    for step in range(MAX_STEPS):
        if FACE_LOW <= face <= FACE_HIGH:
            break
        wanted = (start if step == 0 and start is not None and not offset
                  else offset + GAIN * (FACE_TARGET - face))
        wanted = max(-MAX_LOWER, min(MAX_RAISE, wanted))
        if abs(wanted - offset) < MIN_STEP or not camera.set_control(BRIGHTNESS, base + wanted):
            break
        offset = wanted
        result.steps += 1
        for _ in range(SETTLE_FRAMES):
            frame = camera.read_frame()
        seen = _face(frame, detector)
        if seen is None:
            break
        face = seen[0]
    result.frame, result.offset, result.face_after = frame, offset, face
    return result


@dataclass
class FocusResult:
    best: int | None
    sharpness: dict[int, float]
    faces_seen: int


def _sharpness(camera: TunableCamera, detector: Detector) -> float | None:
    for _ in range(FOCUS_DISCARD):
        camera.read_frame()               # let the lens move
    values = []
    for _ in range(FOCUS_MEASURE):
        frame = camera.read_frame()
        box = detector.detect_largest(frame)
        if box is not None:
            values.append(assess(to_gray(frame), box).face_sharpness)
    return statistics.median(values) if len(values) >= 3 else None


def calibrate_focus(camera: TunableCamera, detector: Detector) -> FocusResult:
    """Find the lens position where your face is sharpest (you sit as usual)."""
    if not hasattr(camera, "set_control") or not camera.set_control(cv2.CAP_PROP_AUTOFOCUS, 0):
        return FocusResult(None, {}, 0)
    measured: dict[int, float] = {}

    def measure(position: int) -> None:
        if position in measured or not camera.set_control(cv2.CAP_PROP_FOCUS, position):
            return
        if camera.control(cv2.CAP_PROP_FOCUS) != position:
            return                        # outside the lens's range
        value = _sharpness(camera, detector)
        if value is not None:
            measured[position] = value

    for position in FOCUS_SWEEP:
        measure(position)
    if not measured:
        return FocusResult(None, {}, 0)
    coarse = max(measured, key=measured.get)
    for position in range(coarse - 2 * FOCUS_FINE, coarse + 2 * FOCUS_FINE + 1, FOCUS_FINE):
        if position > 0:
            measure(position)
    best = max(measured, key=measured.get)
    log.info("focus calibrated: position %d (sharpness %.0f; %d positions)", best,
             measured[best], len(measured))
    return FocusResult(best, {k: round(v, 1) for k, v in sorted(measured.items())},
                       len(measured))
