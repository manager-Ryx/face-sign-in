from __future__ import annotations
import time
import logging
import cv2
import numpy as np


class Camera:
    """Thin wrapper over cv2.VideoCapture with open/close safety."""

    def __init__(self, index: int = 0, warmup_frames: int = 10, device_id: str = ""):
        self.device_id = device_id
        self.index = index
        self.warmup_frames = warmup_frames
        self._cap: cv2.VideoCapture | None = None

    @classmethod
    def from_config(cls, cfg):
        return cls(cfg.camera_index, cfg.camera_warmup_frames, cfg.camera_device_id)

    def _candidates(self):
        if not self.device_id:
            return [(self.index, b) for b in (cv2.CAP_DSHOW, cv2.CAP_MSMF, cv2.CAP_ANY)]
        from cv2_enumerate_cameras import enumerate_cameras
        # Device interface GUIDs differ by backend; the USB identity prefix does not.
        wanted = self.device_id.casefold().split("#{", 1)[0]
        candidates = []
        for backend in (cv2.CAP_DSHOW, cv2.CAP_MSMF):
            matches = [c for c in enumerate_cameras(backend)
                       if c.path and c.path.casefold().split("#{", 1)[0] == wanted]
            if len(matches) > 1:
                raise RuntimeError("Configured webcam identity is ambiguous")
            if matches:
                candidates.append((matches[0].index, backend))
        if not candidates:
            raise RuntimeError("Configured login webcam is disconnected; refusing another camera")
        return candidates

    def open(self) -> None:
        if self._cap is not None:
            return
        last_err: str | None = None
        # Three attempts per backend, because Windows webcam drivers often
        # report ``isOpened=True`` from a handle the previous process (or
        # a killed enroll wizard) didn't release cleanly. Releasing the
        # zombie capture + waiting a beat is enough for DirectShow to
        # hand the real device back.
        for attempt in range(3):
            for index, backend in self._candidates():
                cap = cv2.VideoCapture(index, backend)
                cap.set(cv2.CAP_PROP_FRAME_WIDTH, 640)
                cap.set(cv2.CAP_PROP_FRAME_HEIGHT, 480)
                cap.set(cv2.CAP_PROP_BUFFERSIZE, 1)
                ok = False
                for _ in range(5):
                    ret, _ = cap.read()
                    if ret:
                        ok = True
                        break
                    time.sleep(0.1)
                if ok:
                    logging.getLogger(__name__).info("Opened login webcam identity=%s index=%s backend=%s", self.device_id or "legacy", index, backend)
                    self._cap = cap
                    for _ in range(self.warmup_frames):
                        cap.read()
                        time.sleep(0.03)
                    return
                last_err = (f"attempt={attempt} backend={backend} "
                            f"isOpened={cap.isOpened()}")
                cap.release()
            if attempt < 2:
                time.sleep(1.0)  # let the driver flush stuck handles
        raise RuntimeError(f"Cannot open camera index {self.index} ({last_err})")

    def close(self) -> None:
        if self._cap is not None:
            self._cap.release()
            self._cap = None

    def read(self) -> np.ndarray | None:
        assert self._cap is not None, "Camera not opened"
        ok, frame = self._cap.read()
        return frame if ok else None

    def __enter__(self):
        self.open()
        return self

    def __exit__(self, *exc):
        self.close()
