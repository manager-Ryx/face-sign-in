"""Named pipe server exposing face verification + credential retrieval.

Protocol: line-delimited JSON.  Client sends {"cmd": "...", ...}, server replies
with a single JSON line and closes the connection.

Commands:
  {"cmd":"ping"}
      -> {"ok":true,"pong":true}
  {"cmd":"verify"}
      -> {"ok":true,"match":bool,"distance":float,"real":bool}
  {"cmd":"unlock"}             # verify + return credentials on success
      -> {"ok":true,"username":"...","password":"...","domain":"..."}  (on match)
      -> {"ok":false,"reason":"..."}
  {"cmd":"unlock_result","attempt_id":"...","accepted":true|false}
      -> {"ok":true,"added":int}  # commit/discard staged adaptive samples
  {"cmd":"presence"}           # single-frame presence probe
      -> {"ok":true,"present":bool,"real":bool,"mode":"recognition|detection"}
  {"cmd":"status"}             # service metadata for GUI
      -> {"ok":true,"uptime_s":float,"config":{...},"enrollment":bool}
  {"cmd":"reload_config"}      # re-read config.toml from disk
      -> {"ok":true,"config":{...}}
  {"cmd":"pause_camera","seconds":120}   # release webcam for N seconds so
                                          # the enrollment GUI can own it
      -> {"ok":true,"paused_until":float}
  {"cmd":"resume_camera"}                 # clear the camera lease early
      -> {"ok":true}
  {"cmd":"build_enrollment"}              # (re)compute embeddings from ENROLL_DIR
      -> {"ok":true,"count":int} | {"ok":false,"reason":str}
  {"cmd":"shutdown"}           # stop the service cleanly (tray Quit uses this)
      -> {"ok":true,"shutting_down":true}
"""
from __future__ import annotations
import json
import logging
import threading
import time
from dataclasses import asdict
from typing import Callable

import pywintypes  # type: ignore
import win32api  # type: ignore
import win32con  # type: ignore
import win32file  # type: ignore
import win32pipe  # type: ignore
import win32security  # type: ignore


def win32api_get_last_error() -> int:
    return win32api.GetLastError()

from .camera import Camera
from .config import Config, LOG_PATH, PIPE_NAME
from .credentials import load_password
from .detector import FaceDetector
from .recognizer import FrameAnalysis, Recognizer

log = logging.getLogger(__name__)

# While LogonUI is actively polling for a face, its unlock request has priority
# over the low-frequency walk-away monitor.  A presence camera scan can take
# long enough to make the Credential Provider abandon its pipe response.  The
# provider refreshes this window on every unlock request; it expires shortly
# after LogonUI stops polling so normal presence monitoring resumes.
UNLOCK_PRIORITY_GRACE_S = 35.0


def _build_pipe_sa() -> win32security.SECURITY_ATTRIBUTES:
    """Allow only this user and LocalSystem to connect to the pipe.

    FaceService runs as the enrolled user while the credential provider runs
    inside LogonUI as LocalSystem. No other local account needs access.
    """
    token = win32security.OpenProcessToken(
        win32api.GetCurrentProcess(), win32con.TOKEN_QUERY
    )
    try:
        user_sid = win32security.GetTokenInformation(
            token, win32security.TokenUser
        )[0]
    finally:
        token.Close()
    system_sid = win32security.ConvertStringSidToSid("S-1-5-18")

    dacl = win32security.ACL()
    access = win32con.GENERIC_READ | win32con.GENERIC_WRITE
    dacl.AddAccessAllowedAce(win32security.ACL_REVISION, access, user_sid)
    dacl.AddAccessAllowedAce(win32security.ACL_REVISION, access, system_sid)

    sd = win32security.SECURITY_DESCRIPTOR()
    sd.SetSecurityDescriptorOwner(user_sid, False)
    sd.SetSecurityDescriptorDacl(True, dacl, False)
    sa = win32security.SECURITY_ATTRIBUTES()
    sa.SECURITY_DESCRIPTOR = sd
    sa.bInheritHandle = 0
    return sa


class FaceService:
    def __init__(self, cfg: Config):
        self.cfg = cfg
        self.recog = Recognizer(cfg)
        self.detector = FaceDetector()
        self._stop = threading.Event()
        self._cam_lock = threading.Lock()
        self._cam: Camera | None = None  # kept open when persistent_camera=True
        self._unlock_camera_timer: threading.Timer | None = None
        self._unlock_camera_expires_at = 0.0
        self._started_at = time.time()
        # Keep the pipe accept loop responsive while camera/model work runs.
        # The count lets a concurrent presence probe yield the camera as soon
        # as LogonUI starts an unlock request.
        self._unlock_state_lock = threading.Lock()
        self._unlock_request_count = 0
        self._request_threads: set[threading.Thread] = set()
        self._request_threads_lock = threading.Lock()
        # When the enrollment wizard is running it needs exclusive access to
        # the webcam. ``_camera_paused_until`` holds an epoch timestamp: probes
        # and verify calls short-circuit until that time passes. The service
        # also releases the persistent camera so the tray can open it.
        self._camera_paused_until: float = 0.0
        self._unlock_priority_until: float = 0.0
        # Safe candidates from recent failed unlock votes live only in memory.
        # They are eligible for persistence only if a later face attempt is
        # accepted by Windows, and are bounded by the config TTL/cap.
        self._preauth_candidates: list[tuple[float, FrameAnalysis]] = []

    def _begin_unlock_request(self) -> None:
        with self._unlock_state_lock:
            self._unlock_request_count += 1

    def _end_unlock_request(self) -> None:
        with self._unlock_state_lock:
            self._unlock_request_count = max(0, self._unlock_request_count - 1)

    def _unlock_priority_active(self) -> bool:
        with self._unlock_state_lock:
            active = self._unlock_request_count > 0
        return active or time.monotonic() < self._unlock_priority_until

    def _prune_preauth_candidates(self) -> None:
        """Drop expired/disabled pre-auth candidates."""
        if (
            not self.cfg.adaptive_enrollment
            or not self.cfg.adaptive_pre_auth_enabled
            or self.cfg.adaptive_pre_auth_max_frames <= 0
            or self.cfg.adaptive_pre_auth_ttl_s <= 0
        ):
            self._preauth_candidates.clear()
            return
        cutoff = time.monotonic() - self.cfg.adaptive_pre_auth_ttl_s
        self._preauth_candidates = [
            item for item in self._preauth_candidates if item[0] >= cutoff
        ]
        if len(self._preauth_candidates) > self.cfg.adaptive_pre_auth_max_frames:
            self._preauth_candidates = self._preauth_candidates[
                -self.cfg.adaptive_pre_auth_max_frames :
            ]

    def _remember_preauth_candidates(self, analyses: list[FrameAnalysis]) -> None:
        """Retain only safe in-memory candidates from a failed vote."""
        self._prune_preauth_candidates()
        if not analyses:
            return
        if (
            not self.cfg.adaptive_enrollment
            or not self.cfg.adaptive_pre_auth_enabled
        ):
            return
        now = time.monotonic()
        self._preauth_candidates.extend((now, analysis) for analysis in analyses)
        self._prune_preauth_candidates()

    def _consume_preauth_candidates(
        self, current: list[FrameAnalysis]
    ) -> list[FrameAnalysis]:
        """Combine recent failed-vote candidates with the successful vote."""
        self._prune_preauth_candidates()
        previous = [analysis for _created, analysis in self._preauth_candidates]
        self._preauth_candidates.clear()
        return previous + current

    def _camera_leased_out(self) -> bool:
        return time.time() < self._camera_paused_until

    def _get_camera(self) -> Camera:
        """Return the shared persistent camera, opening it if needed."""
        if self._cam is None:
            self._cam = Camera.from_config(self.cfg)
            self._cam.open()
        return self._cam

    def _release_camera(self) -> None:
        if self._cam is not None:
            self._cam.close()
            self._cam = None

    def _cancel_unlock_camera_release(self) -> None:
        timer = self._unlock_camera_timer
        self._unlock_camera_timer = None
        self._unlock_camera_expires_at = 0.0
        if timer is not None:
            timer.cancel()

    def _schedule_unlock_camera_release(self) -> None:
        """Keep the camera warm only across active lock-screen retries."""
        if self.cfg.persistent_camera:
            return
        self._cancel_unlock_camera_release()
        hold_s = self.cfg.unlock_camera_hold_s
        if hold_s <= 0:
            self._release_camera()
            return
        self._unlock_camera_expires_at = time.monotonic() + hold_s

        def release_if_idle() -> None:
            with self._cam_lock:
                if time.monotonic() >= self._unlock_camera_expires_at:
                    self._release_camera()
                    self._unlock_camera_timer = None
                    self._unlock_camera_expires_at = 0.0

        self._unlock_camera_timer = threading.Timer(hold_s + 0.05, release_if_idle)
        self._unlock_camera_timer.daemon = True
        self._unlock_camera_timer.start()

    # ---------- core ops ----------

    def _capture_and_verify(
        self, *, for_unlock: bool = False
    ) -> tuple[bool, float, bool, list]:
        if self._camera_leased_out():
            log.info("verify skipped: camera leased out to enrollment")
            return False, 1.0, False, []
        matches = 0
        best = 1.0
        real_seen = False
        adaptive_candidates = []
        frames = self.cfg.unlock_verify_frames if for_unlock else self.cfg.verify_frames
        required = self.cfg.unlock_verify_required if for_unlock else self.cfg.verify_required
        with self._cam_lock:
            # Unlock retries share a short-lived warm camera even when normal
            # presence probes release it after every request.
            warm_unlock = for_unlock and self.cfg.unlock_camera_hold_s > 0
            cam = self._get_camera() if (self.cfg.persistent_camera or warm_unlock) else Camera.from_config(self.cfg)
            if not self.cfg.persistent_camera and not warm_unlock:
                cam.open()
            try:
                # drain stale buffered frames
                for _ in range(2):
                    cam.read()
                for _ in range(frames):
                    frame = cam.read()
                    if frame is None:
                        continue
                    try:
                        analysis = self.recog.analyze_frame(frame)
                    except Exception as e:
                        log.warning("verify error: %s", e)
                        continue
                    ok, dist, real = analysis.match, analysis.distance, analysis.is_real
                    real_seen = real_seen or real
                    if dist < best:
                        best = dist
                    if for_unlock and analysis.adaptive_eligible:
                        adaptive_candidates.append(analysis)
                    if ok:
                        matches += 1
                        if matches >= required:
                            return True, best, True, adaptive_candidates
            finally:
                if warm_unlock:
                    self._schedule_unlock_camera_release()
                elif not self.cfg.persistent_camera:
                    cam.close()
        return False, best, real_seen, adaptive_candidates

    def _presence_probe(self) -> tuple[bool, bool]:
        """(present, real) — semantics depend on config.presence_mode.

        recognition: ``present`` = enrolled face detected AND passes anti-spoofing.
        detection:   ``present`` = *any* face detected by YuNet. ``real`` is
                     reported as True (anti-spoofing not evaluated).
        """
        if self._camera_leased_out():
            log.info("presence probe skipped: camera leased out to enrollment")
            # Return (True, True) so the monitor doesn't rack up strikes
            # while the user is enrolling their face.
            return True, True
        if self.cfg.presence_mode == "detection":
            return self._presence_probe_detection()
        return self._presence_probe_recognition()

    def _presence_probe_recognition(self) -> tuple[bool, bool]:
        if self._unlock_priority_active():
            return True, True
        with self._cam_lock:
            if self._unlock_priority_active():
                return True, True
            cam = self._get_camera() if self.cfg.persistent_camera else Camera.from_config(self.cfg)
            if not self.cfg.persistent_camera:
                cam.open()
            try:
                for _ in range(2):
                    if self._unlock_priority_active():
                        return True, True
                    cam.read()
                for _ in range(3):
                    if self._unlock_priority_active():
                        return True, True
                    frame = cam.read()
                    if frame is None:
                        continue
                    try:
                        ok, _dist, real = self.recog.verify_frame(frame)
                    except Exception:
                        continue
                    if ok:
                        return True, real
            finally:
                if not self.cfg.persistent_camera:
                    cam.close()
        return False, False

    def _presence_probe_detection(self) -> tuple[bool, bool]:
        if self._unlock_priority_active():
            return True, True
        with self._cam_lock:
            if self._unlock_priority_active():
                return True, True
            cam = self._get_camera() if self.cfg.persistent_camera else Camera.from_config(self.cfg)
            if not self.cfg.persistent_camera:
                cam.open()
            try:
                for _ in range(2):
                    if self._unlock_priority_active():
                        return True, True
                    cam.read()
                for _ in range(5):
                    if self._unlock_priority_active():
                        return True, True
                    frame = cam.read()
                    if frame is None:
                        continue
                    try:
                        if self.detector.has_face(frame):
                            return True, True
                    except Exception as e:
                        log.warning("detector error: %s", e)
                        continue
            finally:
                if not self.cfg.persistent_camera:
                    cam.close()
        return False, True

    # ---------- pipe ----------

    def _status(self) -> dict:
        from .config import EMBED_PATH
        protected_count, adaptive_count = self.recog.profile_counts()
        return {
            "ok": True,
            "uptime_s": time.time() - self._started_at,
            "config": asdict(self.cfg),
            "enrollment": EMBED_PATH.exists(),
            "profile": {
                "protected": protected_count,
                "adaptive": adaptive_count,
            },
        }

    def _reload_config(self) -> dict:
        new_cfg = Config.load()
        try:
            new_cfg.validate()
        except ValueError as e:
            return {"ok": False, "reason": f"invalid-config: {e}"}
        old_device_id = self.cfg.camera_device_id
        old_index = self.cfg.camera_index
        old_persistent = self.cfg.persistent_camera
        self.cfg = new_cfg
        self.recog.cfg = new_cfg
        # Reset camera if camera-affecting settings changed
        if (new_cfg.camera_device_id != old_device_id or new_cfg.camera_index != old_index
                or new_cfg.persistent_camera != old_persistent):
            with self._cam_lock:
                self._cancel_unlock_camera_release()
                self._release_camera()
        return {"ok": True, "config": asdict(new_cfg)}

    def _handle(self, req: dict) -> dict:
        cmd = req.get("cmd")
        if cmd == "ping":
            return {"ok": True, "pong": True}

        if cmd == "status":
            return self._status()

        if cmd == "reload_config":
            return self._reload_config()

        if cmd == "shutdown":
            log.info("shutdown requested via pipe")
            self._stop.set()
            return {"ok": True, "shutting_down": True}

        if cmd == "pause_camera":
            # Release the webcam and ignore probe/verify for the requested
            # number of seconds so the enrollment wizard can own it.
            seconds = float(req.get("seconds", 120))
            self._camera_paused_until = time.time() + max(5.0, seconds)
            with self._cam_lock:
                self._cancel_unlock_camera_release()
                self._release_camera()
            log.info("camera leased out for %.0fs (enrollment)", seconds)
            return {"ok": True, "paused_until": self._camera_paused_until}

        if cmd == "resume_camera":
            was = self._camera_paused_until
            self._camera_paused_until = 0.0
            log.info("camera lease cleared (was until %s)", was)
            return {"ok": True}

        if cmd == "build_enrollment":
            try:
                from .config import ENROLL_DIR
                n = self.recog.enroll_from_dir(ENROLL_DIR)
                return {"ok": True, "count": n}
            except Exception as e:
                log.exception("build_enrollment failed")
                return {"ok": False, "reason": str(e)}

        if cmd == "verify":
            ok, dist, real, _candidates = self._capture_and_verify()
            return {"ok": True, "match": ok, "distance": dist, "real": real}

        if cmd == "presence":
            if self._unlock_priority_active():
                log.info("presence probe skipped: lock-screen unlock is active")
                return {
                    "ok": True,
                    "present": True,
                    "real": True,
                    "mode": self.cfg.presence_mode,
                    "skipped": "unlock-active",
                }
            present, real = self._presence_probe()
            return {"ok": True, "present": present, "real": real, "mode": self.cfg.presence_mode}

        if cmd == "unlock":
            self._begin_unlock_request()
            try:
                self._unlock_priority_until = (
                    time.monotonic() + UNLOCK_PRIORITY_GRACE_S
                )
                self._prune_preauth_candidates()
                ok, dist, real, candidates = self._capture_and_verify(for_unlock=True)
                if not ok:
                    # A failed 2-of-3 vote can still contain a safe near-anchor
                    # frame. Keep only those embeddings in memory for a short
                    # window; nothing is persisted until a later face unlock is
                    # accepted by Windows.
                    self._remember_preauth_candidates(candidates)
                    return {"ok": False, "reason": "no-match", "distance": dist, "real": real}
                creds = load_password()
                if not creds:
                    self._preauth_candidates.clear()
                    return {"ok": False, "reason": "no-credentials"}
                candidate_pool = self._consume_preauth_candidates(candidates)
                attempt_id = self.recog.stage_adaptive(candidate_pool)
                response = {
                    "ok": True,
                    "username": creds["u"],
                    "password": creds["p"],
                    "domain": creds.get("d", "."),
                }
                if attempt_id:
                    response["attempt_id"] = attempt_id
                return response
            finally:
                self._end_unlock_request()

        if cmd == "unlock_result":
            attempt_id = req.get("attempt_id")
            accepted = req.get("accepted")
            if not isinstance(attempt_id, str) or not isinstance(accepted, bool):
                return {"ok": False, "reason": "invalid-unlock-result"}
            added = self.recog.finish_adaptive(attempt_id, accepted)
            log.info("adaptive unlock result accepted=%s added=%d", accepted, added)
            return {"ok": True, "added": added}

        return {"ok": False, "reason": "unknown-command"}

    def _handle_connection(self, handle, req: dict) -> None:
        """Handle one parsed request without blocking the pipe accept loop."""
        try:
            try:
                resp = self._handle(req)
            except Exception as e:
                log.exception("handler error")
                resp = {"ok": False, "reason": f"exception: {e}"}
            try:
                win32file.WriteFile(
                    handle,
                    (json.dumps(resp, ensure_ascii=False) + "\n").encode("utf-8"),
                )
                try:
                    win32file.FlushFileBuffers(handle)
                except pywintypes.error:
                    pass
            except pywintypes.error as e:
                # LogonUI closes a cancelled request when the user presses
                # Enter or changes tiles. That is an expected cancellation.
                error_code = getattr(e, "winerror", None)
                if error_code is None and getattr(e, "args", None):
                    error_code = e.args[0]
                if error_code in (109, 232):
                    log.info("client closed request pipe (cancelled)")
                else:
                    raise
        finally:
            try:
                win32pipe.DisconnectNamedPipe(handle)
            except pywintypes.error:
                pass
            win32file.CloseHandle(handle)
            with self._request_threads_lock:
                self._request_threads.discard(threading.current_thread())

    def _serve_one(self) -> None:
        """Accept one request and hand slow work to a daemon worker."""
        sa = _build_pipe_sa()
        handle = win32pipe.CreateNamedPipe(
            PIPE_NAME,
            win32pipe.PIPE_ACCESS_DUPLEX,
            win32pipe.PIPE_TYPE_MESSAGE | win32pipe.PIPE_READMODE_MESSAGE | win32pipe.PIPE_WAIT,
            win32pipe.PIPE_UNLIMITED_INSTANCES,
            65536, 65536, 0, sa,
        )
        handed_off = False
        try:
            win32pipe.ConnectNamedPipe(handle, None)
            _hr, data = win32file.ReadFile(handle, 65536)
            req = json.loads(data.decode("utf-8"))
            log.info("request cmd=%s", req.get("cmd"))
            worker = threading.Thread(
                target=self._handle_connection,
                args=(handle, req),
                name=f"FaceService-{req.get('cmd', 'request')}",
                daemon=True,
            )
            with self._request_threads_lock:
                self._request_threads.add(worker)
            worker.start()
            handed_off = True
        finally:
            if not handed_off:
                try:
                    win32pipe.DisconnectNamedPipe(handle)
                except pywintypes.error:
                    pass
                win32file.CloseHandle(handle)

    def _warmup(self) -> None:
        """Load enrollment, preload heavy models so the first real call is fast."""
        import numpy as np
        try:
            self.recog.load()
            log.info("enrollment loaded: %s", self.recog._refs is not None)
        except Exception as e:
            log.warning("enrollment load: %s", e)

        # Force-load ArcFace + detector + MiniFASNet with a dummy image.
        # Using one of our enrolled photos guarantees a face is present.
        try:
            from .config import ENROLL_DIR
            import cv2
            enroll_imgs = list(ENROLL_DIR.glob("*.jpg")) + list(ENROLL_DIR.glob("*.png"))
            if enroll_imgs:
                img = cv2.imread(str(enroll_imgs[0]))
                if img is not None:
                    t0 = time.time()
                    self.recog.verify_frame(img)
                    log.info("model warmup ok in %.2fs", time.time() - t0)
        except Exception as e:
            log.warning("model warmup failed: %s", e)

        # Also warm up the camera (open + close if not persistent)
        try:
            cam = self._get_camera()
            cam.read()
            log.info("camera warmup ok (persistent=%s)", self.cfg.persistent_camera)
            if not self.cfg.persistent_camera:
                self._release_camera()
        except Exception as e:
            log.warning("camera warmup failed: %s", e)

    def serve_forever(self) -> None:
        import win32event  # type: ignore
        import winerror    # type: ignore
        # Single-instance guard: if another FaceService is already serving
        # this pipe, bail out cleanly instead of competing for connections.
        try:
            mutex = win32event.CreateMutex(None, False, "Local\\FaceUnlockService")
            if win32api_get_last_error() == winerror.ERROR_ALREADY_EXISTS:
                log.warning("another FaceService instance already running; exiting")
                return
        except Exception as e:
            log.warning("mutex check failed, continuing: %s", e)

        log.info("FaceService starting; pipe=%s", PIPE_NAME)
        if self.cfg.warmup_on_start:
            self._warmup()

        while not self._stop.is_set():
            try:
                self._serve_one()
            except Exception:
                log.exception("pipe error")
                time.sleep(0.5)

        log.info("FaceService stopped")
        with self._cam_lock:
            self._cancel_unlock_camera_release()
            self._release_camera()

    def stop(self) -> None:
        self._stop.set()


def _setup_logging() -> None:
    LOG_PATH.parent.mkdir(parents=True, exist_ok=True)
    logging.basicConfig(
        level=logging.INFO,
        format="%(asctime)s %(levelname)s %(name)s: %(message)s",
        handlers=[logging.FileHandler(LOG_PATH, encoding="utf-8"), logging.StreamHandler()],
    )


def main() -> None:
    _setup_logging()
    cfg = Config.load()
    svc = FaceService(cfg)
    try:
        svc.serve_forever()
    except KeyboardInterrupt:
        svc.stop()


if __name__ == "__main__":
    main()
