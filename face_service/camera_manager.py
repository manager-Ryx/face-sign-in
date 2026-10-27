"""The service's single owner of the pinned camera.

* Exactly one capture is ever open, guarded by one lock.
* Lock-screen unlocks take priority: a waiting unlock asks any running
  presence check to stop at its next frame.
* The enrollment window borrows the camera through an explicit, bounded
  lease that it must renew; if the window disappears the lease simply
  expires. While leased, the service never opens a competing capture.
* Between active lock-screen retries the camera stays open briefly; the hold
  time is learned from the measured gap between retries (bounded by the
  configured maximum) and the camera is released at once after success,
  when scanning goes idle, and after background checks.
"""
from __future__ import annotations

import logging
import math
import threading
import time
from collections import deque
from collections.abc import Callable, Iterator
from contextlib import contextmanager
from dataclasses import replace

from .camera import Camera, CameraError, CameraMovedError, SettleReport, describe_error, find_camera
from .camera_profile import CameraProfile
from .config import Config
from .detector import FaceDetector
from .exposure import FACE_HIGH, FACE_LOW, expose_for_face
from .metrics import MetricsStore

log = logging.getLogger(__name__)

# Quick looks on the lock screen can start the camera every few seconds; one
# start-up sample per interval is plenty for the statistics and the log.
UNLOCK_START_SAMPLE_S = 600.0
HELD_LOG_S = 10.0

MAX_LEASE_S = 600.0
MIN_HOLD_S = 2.0
DEFAULT_HOLD_S = 3.0


class CameraUnavailable(RuntimeError):
    def __init__(self, reason: str, message: str):
        super().__init__(message)
        self.reason = reason


class CameraSession:
    """What a caller gets while it holds the camera."""

    def __init__(self, manager: CameraManager, camera: Camera, purpose: str,
                 settle: SettleReport, lighting: str):
        self.manager = manager
        self.camera = camera
        self.purpose = purpose
        self.settle = settle
        self.lighting = lighting
        self.release_after: float | None = None   # seconds; None = manager decides

    def read_frame(self):
        return self.camera.read_frame()

    @property
    def first_frame(self):
        return self.settle.frame

    def preempted(self) -> bool:
        return self.purpose != "unlock" and self.manager.unlock_waiting()

    def switch_to_low_light(self) -> float | None:
        """Use the low-light frame rate for the rest of this session."""
        fps = self.manager.cfg().camera_low_light_fps
        if not fps or self.camera.fps:
            return None
        cost = self.camera.set_fps(fps)
        self.settle = self.camera.settle(min_frames=2, max_frames=6, max_s=1.0)
        return cost


class CameraManager:
    def __init__(self, cfg: Callable[[], Config], profile: CameraProfile,
                 metrics: MetricsStore, *, camera_factory: Callable[..., Camera] | None = None,
                 clock: Callable[[], float] = time.monotonic):
        self.cfg = cfg
        self.profile = profile
        self.metrics = metrics
        self._factory = camera_factory or (lambda c, fps: Camera.from_config(c, fps=fps))
        self._clock = clock
        self._lock = threading.Lock()
        self._state = threading.Condition()
        self._camera: Camera | None = None
        self._release_timer: threading.Timer | None = None
        self._release_token = 0
        self._unlock_waiters = 0
        self._lease_owner: str | None = None
        self._lease_until = 0.0
        self._in_use_by: str | None = None
        self._last_error: dict | None = None
        self._last_open_ms: float | None = None
        self._unlock_gaps: deque[float] = deque(maxlen=40)
        self._last_unlock_end: float | None = None
        self._last_unlock_sample = -UNLOCK_START_SAMPLE_S
        self._opened_for = ""
        self._opened_at = 0.0
        self._detector: FaceDetector | None = None

    # ---- status ------------------------------------------------------------

    def status(self) -> dict:
        """What the camera is doing right now (not whether it is connected)."""
        with self._state:
            if self._lease_active_locked():
                activity = {"state": "leased", "owner": self._lease_owner,
                            "lease_s": round(self._lease_until - self._clock(), 1)}
            elif self._in_use_by:
                activity = {"state": "in-use", "owner": self._in_use_by}
            elif self._camera is not None:
                activity = {"state": "open"}
            else:
                activity = {"state": "idle"}
            activity.update(hold_s=round(self.hold_seconds(), 1),
                            low_light=self.profile.low_light_state(),
                            last_open_ms=self._last_open_ms, last_error=self._last_error)
        return activity

    def probe_device(self) -> dict:
        """Whether the pinned camera is connected, checked without opening it."""
        cfg = self.cfg()
        if not cfg.camera_device_id:
            return {"state": "unpinned", "message": "No camera selected yet."}
        try:
            from .camera import resolve
            resolve(cfg.camera_device_id, cfg.camera_index)  # raises a typed error
            info = find_camera(cfg.camera_device_id)
            return {"state": "ready", "name": info.name if info else "Camera",
                    "message": "Connected"}
        except CameraError as exc:
            return describe_error(exc)
        except Exception as exc:  # enumeration failures must not break status
            return {"state": "error", "message": str(exc)}

    # ---- leases (enrollment window) -----------------------------------------

    def _lease_active_locked(self) -> bool:
        if self._lease_owner and self._clock() >= self._lease_until:
            log.info("camera lease for %s expired", self._lease_owner)
            self._lease_owner = None
        return self._lease_owner is not None

    def lease(self, owner: str, seconds: float) -> float:
        seconds = max(5.0, min(MAX_LEASE_S, float(seconds)))
        with self._lock:            # waits for any capture in progress
            self._cancel_release_timer()
            self._close_camera()
            with self._state:
                self._lease_owner = owner
                self._lease_until = self._clock() + seconds
        self.metrics.record("camera_lease", owner=owner, seconds=seconds)
        return seconds

    def renew(self, owner: str, seconds: float) -> bool:
        with self._state:
            if not self._lease_active_locked() or self._lease_owner != owner:
                return False
            self._lease_until = self._clock() + max(5.0, min(MAX_LEASE_S, float(seconds)))
            return True

    def end_lease(self, owner: str | None = None) -> bool:
        with self._state:
            if self._lease_owner is None or (owner and owner != self._lease_owner):
                return False
            self._lease_owner = None
            self._lease_until = 0.0
            self._state.notify_all()
        self.metrics.record("camera_lease_end", owner=owner or "")
        return True

    def leased_to(self) -> str | None:
        with self._state:
            return self._lease_owner if self._lease_active_locked() else None

    # ---- priority ----------------------------------------------------------

    def unlock_waiting(self) -> bool:
        with self._state:
            return self._unlock_waiters > 0

    # ---- hold policy -------------------------------------------------------

    def hold_seconds(self) -> float:
        """Keep the camera open across lock-screen retries, based on measured gaps."""
        limit = self.cfg().unlock_camera_hold_s
        if limit <= 0:
            return 0.0
        gaps = sorted(self._unlock_gaps)
        if len(gaps) < 5:
            return min(limit, DEFAULT_HOLD_S)
        p90 = gaps[min(len(gaps) - 1, math.ceil(0.9 * len(gaps)) - 1)]
        return max(MIN_HOLD_S, min(limit, p90 + 1.0))

    def _cancel_release_timer(self) -> None:
        self._release_token += 1
        timer, self._release_timer = self._release_timer, None
        if timer is not None:
            timer.cancel()

    def _schedule_release(self, delay: float) -> None:
        self._cancel_release_timer()
        token = self._release_token

        def release() -> None:
            with self._lock:
                if token == self._release_token:
                    self._close_camera()

        self._release_timer = threading.Timer(delay, release)
        self._release_timer.daemon = True
        self._release_timer.start()

    def _close_camera(self) -> None:
        camera, self._camera = self._camera, None
        if camera is not None:
            try:
                camera.close()
            except Exception as exc:  # noqa: BLE001 - e.g. cv2.error from an unplugged camera
                log.warning("camera did not close cleanly: %s", exc)
            held = self._clock() - self._opened_at
            quick_look = self._opened_for == "unlock" and held < HELD_LOG_S
            log.log(logging.DEBUG if quick_look else logging.INFO,
                    "camera released after %.1f s", held)

    def release_now(self) -> None:
        # Called from the session-event thread: never wait long behind a session
        # in progress, which closes or hands back the camera itself when it ends.
        if not self._lock.acquire(timeout=1.0):
            return
        try:
            self._cancel_release_timer()
            self._close_camera()
        finally:
            self._lock.release()

    # ---- sessions ----------------------------------------------------------

    def _acquire(self, purpose: str, wait_s: float) -> None:
        is_unlock = purpose == "unlock"
        if is_unlock:
            with self._state:
                self._unlock_waiters += 1
                if self._last_unlock_end is not None:
                    gap = self._clock() - self._last_unlock_end
                    if gap < 30.0:  # retries within one lock-screen session
                        self._unlock_gaps.append(gap)
        try:
            if not self._lock.acquire(timeout=wait_s):
                raise CameraUnavailable("camera-busy", "The camera is busy with another check")
        finally:
            if is_unlock:
                with self._state:
                    self._unlock_waiters -= 1
        with self._state:
            leased = self._lease_active_locked()
        if leased:
            self._lock.release()
            raise CameraUnavailable("camera-leased",
                                    "The camera is being used to set up face recognition")

    @contextmanager
    def session(self, purpose: str, *, wait_s: float = 10.0) -> Iterator[CameraSession]:
        """Hold the camera for one unlock attempt, presence check, or test."""
        self._acquire(purpose, wait_s)
        session: CameraSession | None = None
        try:
            self._cancel_release_timer()
            with self._state:
                self._in_use_by = purpose
            session = self._open_session(purpose)
            yield session
        finally:
            try:
                hold = session.release_after if session and session.release_after else 0.0
                if purpose == "unlock":
                    self._last_unlock_end = self._clock()
                if not self.cfg().persistent_camera:
                    if hold > 0 and self._camera is not None:
                        self._schedule_release(hold)
                    else:
                        self._close_camera()
            finally:
                with self._state:
                    self._in_use_by = None
                self._lock.release()

    def _open_session(self, purpose: str) -> CameraSession:
        cfg = self.cfg()
        lighting = self.profile.predict_lighting()
        reused = self._camera is not None
        if self._camera is None:
            fps = cfg.camera_low_light_fps if (
                cfg.camera_low_light_fps and self.profile.wants_low_light(lighting)) else None
            camera = self._factory(cfg, fps)
            try:
                report = camera.open()
            except CameraError as exc:
                self._fail(exc, purpose)
                raise
            self._camera = camera
            open_ms = report.open_ms
            if cfg.camera_focus and hasattr(camera, "fix_focus")                     and not camera.fix_focus(cfg.camera_focus):
                log.warning("the camera did not accept focus position %d", cfg.camera_focus)
        else:
            camera = self._camera
            open_ms = 0.0
        max_frames, max_s = self.profile.warmup_budget(
            lighting, camera.fps, max_frames=cfg.camera_warmup_frames,
            max_s=cfg.camera_warmup_max_s)
        try:
            settle = camera.settle(min_frames=1 if reused else 3, max_frames=max_frames,
                                   max_s=max_s)
        except CameraError as exc:
            self._close_camera()
            self._fail(exc, purpose)
            raise
        settle = self._expose_for_face(camera, settle, purpose)
        if not reused:
            self._opened_for, self._opened_at = purpose, self._clock()
            self.profile.observe_start(lighting, fps=camera.fps, open_ms=open_ms,
                                       settle_ms=settle.ms, settle_frames=settle.frames,
                                       stable=settle.stable)
            sample = (purpose != "unlock"
                      or self._opened_at - self._last_unlock_sample >= UNLOCK_START_SAMPLE_S)
            if sample:
                if purpose == "unlock":
                    self._last_unlock_sample = self._opened_at
                self.metrics.record("camera_start", purpose=purpose, open_ms=open_ms,
                                    settle_ms=settle.ms, settle_frames=settle.frames,
                                    stable=settle.stable, fps=camera.fps or 0,
                                    predicted=lighting)
            log.log(logging.INFO if sample else logging.DEBUG,
                    "camera started for %s: open %.0f ms, settled in %.0f ms (%d frames)",
                    purpose, open_ms, settle.ms, settle.frames)
        with self._state:
            self._last_error = None
            if not reused:
                self._last_open_ms = round(open_ms, 1)
        return CameraSession(self, camera, purpose, settle, lighting)

    def _expose_for_face(self, camera: Camera, settle: SettleReport,
                         purpose: str) -> SettleReport:
        """With a badly lit face in view (a bright window behind you), raise or lower
        brightness for this capture; what worked is remembered per kind of light."""
        if not self.cfg().camera_face_exposure or not hasattr(camera, "set_control"):
            return settle
        if self._detector is None:
            self._detector = FaceDetector()
        result = expose_for_face(camera, settle.frame, self._detector,
                                 start_offset=self.profile.face_offset)
        if (result.lighting and result.face_after is not None
                and FACE_LOW <= result.face_after <= FACE_HIGH):
            self.profile.observe_face_offset(result.lighting, result.offset)
        if result.reverted:
            log.info("face exposure for %s: no better than the camera's own setting (face %.0f); "
                     "left as it was", purpose, result.face_before)
        if not result.adjusted:
            return settle
        log.info("face exposure for %s: brightness %+.0f, face %.0f -> %.0f (%s)", purpose,
                 result.offset, result.face_before, result.face_after, result.lighting)
        self.metrics.record("face_exposure", purpose=purpose, offset=result.offset,
                            before=result.face_before, after=result.face_after,
                            steps=result.steps, lighting=result.lighting)
        return replace(settle, frame=result.frame)

    def _fail(self, exc: CameraError, purpose: str) -> None:
        with self._state:
            self._last_error = {**describe_error(exc), "at": time.time()}
        self.metrics.record("camera_error", error=exc.kind, purpose=purpose,
                            message=str(exc)[:160])
        if isinstance(exc, CameraMovedError):
            log.warning("camera moved: %s", exc)
        else:
            log.warning("camera problem (%s): %s", exc.kind, exc)

    def close(self) -> None:
        self.release_now()
