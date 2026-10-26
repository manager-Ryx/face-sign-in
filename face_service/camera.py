"""Pinned-webcam access: strict identity, fast opening, adaptive warm-up.

Only the configured camera may ever be used for recognition. The camera is
identified by its USB device-interface path, which survives Windows changing
camera indices. If that exact device is absent the camera is reported as
missing; if the same camera model appears on a different USB port it is
reported as *moved* so the user can confirm it explicitly. Nothing here ever
falls back to another camera (for example NVIDIA Broadcast or OBS Virtual
Camera).

Measured on the NearStream V02AF: the driver already delivers 640x480 at
30 fps, and re-setting width/height after opening rebuilt the DirectShow
graph and cost about 1.1 s per open, so the capture is used as delivered.
"""
from __future__ import annotations

import logging
import re
import time
from collections.abc import Iterable
from dataclasses import dataclass, field

import cv2
import numpy as np

log = logging.getLogger(__name__)

TARGET_SIZE = (640, 480)
_HARDWARE_RE = re.compile(r"vid_[0-9a-f]{4}&pid_[0-9a-f]{4}(?:&mi_[0-9a-f]{2})?")
_VIRTUAL_HINTS = ("nvidia broadcast", "obs virtual", "obs-camera", "snap camera",
                  "xsplit", "manycam", "virtual")


class CameraError(RuntimeError):
    """A camera problem the user can understand and act on."""

    kind = "error"

    def __init__(self, message: str, *, detail: dict | None = None):
        super().__init__(message)
        self.detail = detail or {}


class CameraMissingError(CameraError):
    kind = "missing"


class CameraAmbiguousError(CameraError):
    kind = "ambiguous"


class CameraMovedError(CameraError):
    kind = "moved"


class CameraBusyError(CameraError):
    kind = "busy"


class CameraReadError(CameraError):
    kind = "read-failed"


def identity_of(path: str) -> str:
    """Backend-independent USB identity: the path before the interface GUID."""
    return path.casefold().split("#{", 1)[0]


def hardware_id_of(path: str) -> str | None:
    """``vid_xxxx&pid_yyyy[&mi_zz]`` – the camera model, independent of USB port."""
    match = _HARDWARE_RE.search(path.casefold())
    return match.group(0) if match else None


@dataclass(frozen=True)
class CameraInfo:
    name: str
    path: str
    index: int
    backend: int

    @property
    def identity(self) -> str:
        return identity_of(self.path)

    @property
    def hardware_id(self) -> str | None:
        return hardware_id_of(self.path)

    @property
    def virtual(self) -> bool:
        lowered = self.name.casefold()
        return self.hardware_id is None or any(h in lowered for h in _VIRTUAL_HINTS)


def enumerate_devices(backend: int) -> list[CameraInfo]:
    from cv2_enumerate_cameras import enumerate_cameras
    return [CameraInfo(c.name or "Camera", c.path or "", int(c.index), backend)
            for c in enumerate_cameras(backend)]


def list_cameras() -> list[CameraInfo]:
    """Cameras visible to DirectShow, one entry per physical device."""
    seen: set[str] = set()
    result = []
    for info in enumerate_devices(cv2.CAP_DSHOW):
        key = info.identity or f"index:{info.index}"
        if key not in seen:
            seen.add(key)
            result.append(info)
    return result


def find_camera(device_id: str) -> CameraInfo | None:
    wanted = identity_of(device_id)
    return next((c for c in list_cameras() if c.identity == wanted), None)


def resolve(device_id: str, legacy_index: int = 0) -> list[tuple[int, int]]:
    """Return ``(index, backend)`` candidates for the pinned camera, DirectShow first."""
    if not device_id:
        return [(legacy_index, cv2.CAP_DSHOW), (legacy_index, cv2.CAP_MSMF)]
    wanted = identity_of(device_id)
    candidates: list[tuple[int, int]] = []
    for backend in (cv2.CAP_DSHOW, cv2.CAP_MSMF):
        matches = [d for d in enumerate_devices(backend) if d.path and d.identity == wanted]
        if len(matches) > 1:
            raise CameraAmbiguousError("Configured webcam identity is ambiguous")
        if matches:
            candidates.append((matches[0].index, backend))
    if candidates:
        return candidates

    model = hardware_id_of(device_id)
    if model:
        same_model = [d for d in list_cameras() if d.hardware_id == model]
        if len(same_model) == 1:
            moved = same_model[0]
            raise CameraMovedError(
                f"{moved.name} is connected to a different USB port; confirm it before use",
                detail={"name": moved.name, "path": moved.path})
        if len(same_model) > 1:
            raise CameraAmbiguousError(
                "Several cameras of the selected model are connected; choose one in Settings")
    raise CameraMissingError("Configured login webcam is disconnected; refusing another camera")


@dataclass
class OpenReport:
    index: int
    backend: int
    open_ms: float
    fps: float | None
    size: tuple[int, int]


@dataclass
class SettleReport:
    frames: int
    ms: float
    stable: bool
    reason: str
    brightness: list[float] = field(default_factory=list)
    frame: np.ndarray | None = field(default=None, repr=False)


class Camera:
    """One open capture of the pinned camera."""

    _MAX_READ_FAILURES = 6

    def __init__(self, index: int = 0, warmup_frames: int = 12, device_id: str = "", *,
                 fps: float | None = None, warmup_max_s: float = 2.0,
                 open_budget_s: float = 8.0, allow_slow_backend: bool = False):
        self.device_id = device_id
        self.index = index
        self.warmup_frames = warmup_frames
        self.warmup_max_s = warmup_max_s
        self.fps = fps
        self.open_budget_s = open_budget_s
        self.allow_slow_backend = allow_slow_backend
        self._cap: cv2.VideoCapture | None = None
        self._report: OpenReport | None = None
        self._read_failures = 0

    @classmethod
    def from_config(cls, cfg, *, fps: float | None = None, **kwargs) -> Camera:
        return cls(cfg.camera_index, cfg.camera_warmup_frames, cfg.camera_device_id,
                   fps=fps, warmup_max_s=getattr(cfg, "camera_warmup_max_s", 2.0), **kwargs)

    @property
    def is_open(self) -> bool:
        return self._cap is not None

    @property
    def report(self) -> OpenReport | None:
        return self._report

    def open(self) -> OpenReport:
        if self._cap is not None and self._report is not None:
            return self._report
        started = time.perf_counter()
        deadline = started + self.open_budget_s
        candidates = resolve(self.device_id, self.index)
        # MSMF took 12-17 s to open this camera; only try it when DirectShow
        # cannot see the device, or when a slow interactive open is acceptable.
        has_dshow = any(backend == cv2.CAP_DSHOW for _, backend in candidates)
        if has_dshow and not self.allow_slow_backend:
            candidates = [c for c in candidates if c[1] == cv2.CAP_DSHOW]
        problem = "no frames"
        for _ in range(2):
            for index, backend in candidates:
                if time.perf_counter() > deadline:
                    break
                cap = cv2.VideoCapture(index, backend)
                if not cap.isOpened():
                    cap.release()
                    problem = "the device could not be opened"
                    continue
                if self.fps:
                    cap.set(cv2.CAP_PROP_FPS, float(self.fps))
                frame = None
                for _ in range(10):
                    ok, frame = cap.read()
                    if ok and frame is not None:
                        break
                    frame = None
                    time.sleep(0.05)
                if frame is None:
                    cap.release()
                    problem = "the device delivered no frames (it may be in use by another app)"
                    continue
                height, width = frame.shape[:2]
                if width * height > TARGET_SIZE[0] * TARGET_SIZE[1]:
                    cap.set(cv2.CAP_PROP_FRAME_WIDTH, TARGET_SIZE[0])
                    cap.set(cv2.CAP_PROP_FRAME_HEIGHT, TARGET_SIZE[1])
                    ok, resized = cap.read()
                    if ok and resized is not None:
                        frame = resized
                        height, width = frame.shape[:2]
                self._cap = cap
                self._read_failures = 0
                self._report = OpenReport(index, backend,
                                          (time.perf_counter() - started) * 1000.0,
                                          self.fps, (width, height))
                log.debug("camera opened index=%s backend=%s fps=%s in %.0f ms",
                         index, backend, self.fps or "default", self._report.open_ms)
                return self._report
            if time.perf_counter() + 0.5 > deadline:
                break
            time.sleep(0.5)  # give the driver a moment to release a stale handle
        raise CameraBusyError(f"The camera could not be started: {problem}")

    def set_fps(self, fps: float | None) -> float:
        """Change the requested frame rate on the open capture; returns cost in ms."""
        if self._cap is None:
            raise CameraReadError("Camera not opened")
        started = time.perf_counter()
        self._cap.set(cv2.CAP_PROP_FPS, float(fps or 30.0))
        self.fps = fps
        self.read()
        cost = (time.perf_counter() - started) * 1000.0
        if self._report is not None:
            self._report.fps = fps
        return cost

    def read(self) -> np.ndarray | None:
        if self._cap is None:
            raise CameraReadError("Camera not opened")
        ok, frame = self._cap.read()
        if not ok or frame is None:
            self._read_failures += 1
            return None
        self._read_failures = 0
        return frame

    def read_frame(self) -> np.ndarray:
        """Read one frame, tolerating brief hiccups; raise if frames stop."""
        for _ in range(self._MAX_READ_FAILURES):
            frame = self.read()
            if frame is not None:
                return frame
            time.sleep(0.03)
        raise CameraReadError("The camera stopped delivering frames")

    def settle(self, *, min_frames: int = 3, max_frames: int | None = None,
               max_s: float | None = None, tolerance: float = 2.0) -> SettleReport:
        """Read until exposure is stable or the budget is spent.

        Stability is judged on whole-frame brightness, which follows the
        camera's exposure; a moving head barely changes it, so movement
        cannot keep extending the warm-up. The budget always ends it.
        """
        max_frames = max(min_frames, max_frames or self.warmup_frames)
        max_s = max_s if max_s is not None else self.warmup_max_s
        started = time.perf_counter()
        trace: list[float] = []
        frame = None
        while len(trace) < max_frames and time.perf_counter() - started < max_s:
            frame = self.read_frame()
            trace.append(float(frame.mean()))
            if len(trace) >= min_frames:
                window = trace[-3:]
                spread = max(window) - min(window)
                if spread <= max(tolerance, 0.03 * window[-1]):
                    return SettleReport(len(trace), (time.perf_counter() - started) * 1000.0,
                                        True, "stable", [round(v, 1) for v in trace[-8:]], frame)
        reason = "frames" if len(trace) >= max_frames else "time"
        return SettleReport(len(trace), (time.perf_counter() - started) * 1000.0, False,
                            reason, [round(v, 1) for v in trace[-8:]], frame)

    def close(self) -> None:
        cap, self._cap = self._cap, None
        self._report = None
        if cap is not None:
            cap.release()

    def __enter__(self) -> Camera:
        self.open()
        return self

    def __exit__(self, *exc) -> None:
        self.close()


def describe_error(exc: BaseException) -> dict:
    """Serializable camera status for the UI."""
    if isinstance(exc, CameraError):
        return {"state": exc.kind, "message": str(exc), **exc.detail}
    return {"state": "error", "message": str(exc)}


def visible_virtual_cameras(cameras: Iterable[CameraInfo]) -> list[str]:
    return [c.name for c in cameras if c.virtual]
