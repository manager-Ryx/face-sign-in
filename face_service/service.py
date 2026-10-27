"""FaceService: the background process behind face sign-in and presence checks.

It owns the camera, the recognition model, the face profile, and the user's
DPAPI-protected credential, and answers one JSON request per named-pipe
connection (see :mod:`face_service.pipe`).

Commands used by the Windows sign-in tile:
  {"cmd":"unlock", "selected":bool}      -> credential on a verified face, else a reason
  {"cmd":"unlock_result","attempt_id":..., "accepted":bool, "status":int, "substatus":int}
"selected" (you clicked the tile or pressed Enter) and the Windows status codes
come from tiles of version 0.3 and later; older tiles omit them.

Commands used by the tray app and tools:
  ping, status, insights, reload_config, shutdown,
  presence, check, verify,
  pause_camera / renew_camera / resume_camera   (camera hand-over leases),
  build_enrollment, profile_health, remove_anchors, map_anchor_files,
  learning_undo, learning_reset, threshold_apply, threshold_clear,
  snapshot_restore, camera_list, clear_measurements, low_light_reset, routine_reset,
  auto_lock (the presence monitor is about to lock because you are away),
  calibrate_focus, prepare_model (retry loading the models).
"""
from __future__ import annotations

import logging
import math
import os
import secrets
import shutil
import statistics
import threading
import time
from collections import deque
from collections.abc import Callable
from dataclasses import asdict, dataclass, field, replace
from pathlib import Path
from typing import Any

import cv2

from . import verification
from ._version import __version__
from .adaptation import AdaptationState
from .camera import CameraError, list_cameras
from .camera_manager import CameraManager, CameraSession, CameraUnavailable
from .camera_profile import CameraProfile
from .config import (
    APP_DIR,
    BACKUP_DIR,
    CREDS_PATH,
    ENROLL_DIR,
    LOG_PATH,
    METRICS_DIR,
    PIPE_NAME,
    STATE_DIR,
    Config,
    load_config,
)
from .credentials import load_password
from .detector import FaceDetector
from .embedder import Embedder, SpoofModelUnavailable
from .evaluation import evaluate_threshold
from .evaluation_store import EvaluationStore
from .exposure import calibrate_focus
from .logs import setup_logging
from .metrics import MetricsStore
from .models import provide_bundled_models, recognition_model_present
from .pacing import LockScreenPacer
from .pipe import ClientInfo, PipeServer
from .profile import SOURCE_ENROLLMENT, SOURCE_MANUAL, SOURCE_PASSWORD, SOURCE_PRESENCE
from .quality import assess, to_gray
from .recognizer import IMAGE_SUFFIXES, FrameAnalysis, NotEnrolledError, Outcome, Recognizer
from .routine import ReturnRoutine
from .verification import Verdict, VerifyPolicy
from .win_events import SessionWatcher

log = logging.getLogger(__name__)

# While the sign-in screen is actively requesting scans, background presence
# checks yield the camera to it.
UNLOCK_PRIORITY_GRACE_S = 35.0
ATTEMPT_TTL_S = 120.0
STATUS_DEVICE_TTL_S = 10.0
MAX_STAGING_PHOTOS = 200
# Once a routine is known, continuous scanning after the screen locks or wakes,
# or a face is seen, lasts this long; the routine decides beyond that.
EAGER_WINDOW_S = 120.0
UNLOCK_BUDGET_S = 20.0      # most a sign-in request waits before it may use the camera
CHECK_MODEL_WAIT_S = 20.0
# Lock-screen results that carry no news: counted per lock, not recorded one by one
# (quick looks would otherwise write a record every few seconds all night).
QUIET_REASONS = frozenset({"no_face", "superseded", "display-off", "stopped", "preempted",
                           "starting"})
PACING_REASONS = frozenset({"superseded", "display-off", "stopped", "starting"})
QUIET_COMMANDS = frozenset({"ping", "status", "unlock"})  # unlock: one summary per lock instead
AUTO_LOCK_NOTICE_S = 10.0   # a lock this soon after the presence monitor's notice is its own
# After a password or PIN sign-in: learn from what the sign-in screen saw just before.
PASSWORD_LOOKBACK_S = 120.0
PASSWORD_SAMPLES = 3
PASSWORD_ANCHOR_SLACK = 0.15   # may be this much further from your photos than a match
PRESENCE_LEARN_GAP_S = 600.0   # at most one view learned while you sit, per ten minutes
# Windows rejected the stored password this many times in a row: it is probably out
# of date. Stop replaying it (each attempt counts as a failed sign-in, and work PCs
# may lock the account) until a new one is saved.
PASSWORD_REJECTIONS = 2
# Windows results that mean the stored password (or account name) itself is wrong.
# Anything else - no domain controller, a locked account - says nothing about it.
WRONG_PASSWORD_STATUS = frozenset({
    0xC000006D,  # STATUS_LOGON_FAILURE
    0xC000006A,  # STATUS_WRONG_PASSWORD
    0xC0000064,  # STATUS_NO_SUCH_USER
    0xC0000071,  # STATUS_PASSWORD_EXPIRED
    0xC0000224,  # STATUS_PASSWORD_MUST_CHANGE
})
# A tile selection this soon after the lock is Windows setting up the sign-in
# screen, not you asking to be let back in (Windows+L while facing the camera).
SELECTION_ARMS_AFTER_S = 10.0
SERVICE_MUTEX = "Local\\FaceUnlockService"   # one service per signed-in session
CAUGHT_TEXT = {"instant": "by face straight away", "quick-look": "by face on a quick look",
               "other": "another way"}


@dataclass
class _Attempt:
    created: float
    eval_embedding: Any = None
    eval_lighting: str = "unknown"


@dataclass
class _LockSession:
    started: float
    started_wall: float             # when you left: the lock, or earlier for an automatic lock
    inferred: bool = False          # the service started while the screen was already locked
    automatic: bool = False         # locked by the presence monitor because you were away
    armed: bool = True              # face sign-in allowed (after a manual lock: once you left)
    # Views of a face during this lock (memory only), for learning if you then use a password.
    recent: deque[tuple[float, FrameAnalysis]] = field(default_factory=lambda: deque(maxlen=60))
    face_seen: bool = False
    face_accepted: bool = False
    attempts: int = 0
    first_face_at: float | None = None
    accepted_at: float | None = None
    accepted_mode: str | None = None
    reasons: dict[str, int] = field(default_factory=dict)


def password_rejected(accepted: bool, status: Any, substatus: Any) -> bool:
    """Did Windows turn down the stored password itself (not the network or policy)?"""
    if accepted:
        return False
    codes = [c for c in (substatus, status) if isinstance(c, int) and not isinstance(c, bool)]
    if not codes:
        return True  # older tiles report only "rejected": assume the password
    code = next((c for c in codes if c), 0) & 0xFFFFFFFF
    return code in WRONG_PASSWORD_STATUS


class FaceService:
    def __init__(self, cfg: Config, *, problems: list[str] | None = None,
                 metrics: MetricsStore | None = None, embedder: Embedder | None = None,
                 camera_manager: CameraManager | None = None,
                 watcher: SessionWatcher | None = None,
                 clock: Callable[[], float] = time.monotonic,
                 wall_clock: Callable[[], float] = time.time):
        self.cfg = cfg
        self.config_problems = list(problems or [])
        self._clock = clock
        self._wall_clock = wall_clock
        self._started = time.time()
        self.metrics = metrics or MetricsStore(METRICS_DIR / "service.jsonl",
                                               retention_days=cfg.metrics_retention_days)
        self.adaptation = AdaptationState(STATE_DIR / "adaptation.json")
        self.recognizer = Recognizer(
            cfg, embedder=embedder, metrics=self.metrics,
            threshold=lambda: self.adaptation.effective_threshold(self.cfg))
        self.camera_profile = CameraProfile(STATE_DIR / "camera_profile.json")
        self.camera = camera_manager or CameraManager(lambda: self.cfg, self.camera_profile,
                                                      self.metrics)
        self.evaluation = EvaluationStore(STATE_DIR / "evaluation.npz")
        self.routine = ReturnRoutine(STATE_DIR / "routine.json", clock=wall_clock)
        self.watcher = watcher
        self.pacer = LockScreenPacer(
            idle_after_s=self._eager_window,
            idle_interval_s=lambda: self.cfg.unlock_idle_interval_s,
            pause_when_display_off=lambda: self.cfg.unlock_pause_when_display_off,
            display_state=lambda: self.watcher.display if self.watcher else None,
            clock=clock, likely_back=self._likely_back, start_window_s=self._start_window)
        self.detector = FaceDetector()
        self.server: PipeServer | None = None
        self._unlock_priority_until = 0.0
        self._attempts: dict[str, _Attempt] = {}
        self._attempts_lock = threading.Lock()
        self._lock_session: _LockSession | None = None
        self._session_lock = threading.Lock()
        self._auto_lock_notice: tuple[float, float] | None = None  # (when, seconds away)
        self._face_sign_in_at: float | None = None                 # wall clock
        self._presence_learned_at = float("-inf")
        self._rejections = 0
        self._rejected_password: float | None = None   # mtime of the stored password then
        self._model_error: str | None = None   # why the recognition model couldn't load
        self._preparing = threading.Lock()
        self._caller_seen: tuple[str | None, bool] | None = None
        self._device_status: tuple[float, dict] | None = None
        self._ready_reason = "starting"
        self._warm = threading.Event()
        self._warm.set()  # cleared by start_warmup() until the model is loaded
        self._stopping = threading.Event()
        if watcher is not None:
            watcher.add_handler(self._on_session_event)

    # ---- session events ----------------------------------------------------

    def _on_session_event(self, name: str, detail: dict) -> None:
        self.metrics.record("session_event", event=name, **{k: v for k, v in detail.items()
                                                            if isinstance(v, (str, int))})
        if name.startswith("display-"):
            state = name.split("-", 1)[1]
            if state == "on":
                self._arm("woke the screen")
            self.pacer.display_changed(state)
        elif name == "lock":
            self._open_lock_session()
            self.pacer.take_mode_time()  # start this lock's camera-time tally afresh
            self.camera.release_now()
        elif name == "unlock":
            self._unlock_priority_until = 0.0  # the sign-in screen is gone: camera is free
            self._close_lock_session()

    def _open_lock_session(self) -> None:
        now, wall = self._clock(), self._wall_clock()
        current = self._lock_session
        if current is not None and not current.inferred and now - current.started < 5.0:
            return  # the same lock reported twice
        notice, self._auto_lock_notice = self._auto_lock_notice, None
        automatic = notice is not None and now - notice[0] <= AUTO_LOCK_NOTICE_S
        session = _LockSession(now, wall - (notice[1] if automatic and notice else 0.0),
                               automatic=automatic,
                               armed=automatic or not self.cfg.unlock_wait_after_manual_lock)
        with self._session_lock:
            self._lock_session = session
            self._caller_seen = None
        if not session.armed:
            log.info("locked by you: face sign-in waits until you have stepped away")

    def _arm(self, why: str) -> None:
        with self._session_lock:
            session = self._lock_session
            if session is None or session.armed:
                return
            session.armed = True
        log.info("face sign-in ready again: you %s", why)

    def _close_lock_session(self) -> None:
        with self._session_lock:
            session, self._lock_session = self._lock_session, None
        spent = self.pacer.take_mode_time()
        if session is None:
            return
        away_s = self._clock() - session.started
        time_to_unlock = (session.accepted_at - session.first_face_at
                          if session.accepted_at and session.first_face_at else None)
        if session.face_accepted:
            caught = "instant" if session.accepted_mode == "active" else "quick-look"
        else:
            caught = "other"
        self.metrics.record(
            "lock_session", duration_s=away_s, face_seen=session.face_seen,
            face_accepted=session.face_accepted, attempts=session.attempts,
            time_to_unlock_s=time_to_unlock, caught=caught,
            scanning_s=round(spent.get("active", 0.0), 1),
            quick_look_s=round(spent.get("idle", 0.0), 1),
            screen_off_s=round(spent.get("display-off", 0.0), 1),
            top_reason=max(session.reasons, key=session.reasons.get) if session.reasons else "")
        log.info("lock screen: away %.0f min, %d look(s), signed in %s", away_s / 60.0,
                 session.attempts, CAUGHT_TEXT[caught])
        if not session.face_accepted:
            self._learn_after_password(session)
        if self.cfg.unlock_learn_routine and not session.inferred:
            known = self.routine.schedule() is not None
            self.routine.observe(session.started_wall, self._wall_clock())
            if not known and self.routine.schedule() is not None:
                self.adaptation.note("routine", None, "learned",
                                     "The sign-in screen now scans when you usually come back",
                                     by="automatic")
        if session.face_seen:
            result = self.adaptation.record_session(face_success=session.face_accepted)
            if result and result.get("rolled_back"):
                log.warning("automatic threshold change rolled back (rate %.0f%%)",
                            result["rate"] * 100)

    def _note_attempt(self, verdict: Verdict | None, reason: str, mode: str,
                      looked_at: float | None = None) -> None:
        """Tally one sign-in screen request; ``looked_at`` is when its camera look began."""
        with self._session_lock:
            session = self._lock_session
            if session is None or reason in PACING_REASONS:
                return
            session.attempts += 1
            session.reasons[reason] = session.reasons.get(reason, 0) + 1
            if verdict and any(f.outcome is not Outcome.NO_FACE for f in verdict.frames):
                session.face_seen = True
                if session.first_face_at is None:
                    session.first_face_at = looked_at if looked_at is not None else self._clock()
                if reason == "match" and session.accepted_at is None:
                    session.accepted_at = self._clock()
                    session.accepted_mode = mode

    def _password_out_of_date(self) -> bool:
        """Windows kept rejecting the stored password, and no new one has been saved."""
        if self._rejected_password is None:
            return False
        try:
            if CREDS_PATH.stat().st_mtime != self._rejected_password:
                self._rejected_password, self._rejections = None, 0   # a new password
                return False
        except OSError:
            return False
        return True

    def _note_password_result(self, accepted: bool, rejected: bool = True) -> None:
        if accepted:
            self._rejections = 0
            return
        if not rejected:
            return  # failed for another reason (offline domain PC, locked account)
        self._rejections += 1
        if self._rejections >= PASSWORD_REJECTIONS and self._rejected_password is None:
            try:
                self._rejected_password = CREDS_PATH.stat().st_mtime
            except OSError:
                return
            log.warning("Windows rejected the stored password %d times; face sign-in paused "
                        "until it is updated", self._rejections)

    def _keep_for_learning(self, verdict: Verdict) -> None:
        """Remember this lock's views of a face, in case you then sign in another way."""
        views = [f for f in verdict.frames if f.embedding is not None and f.face_count == 1]
        if not views:
            return
        now = self._wall_clock()
        with self._session_lock:
            if self._lock_session is not None:
                self._lock_session.recent.extend((now, view) for view in views)

    def _learn_after_password(self, session: _LockSession) -> None:
        """You signed in with your password or PIN, so the face the sign-in screen saw
        just before was you, even though it couldn't recognise you (say, a bright window
        behind you). Learn a few distinct views of it."""
        cfg = self.cfg
        if not (cfg.adaptive_enrollment and cfg.adaptive_learn_after_password) or session.inferred:
            return
        cutoff = self._wall_clock() - PASSWORD_LOOKBACK_S
        views = [view for at, view in session.recent if at >= cutoff]
        if not views:
            return
        result = self.recognizer.learn_now(
            views, source=SOURCE_PASSWORD, limit=PASSWORD_SAMPLES,
            max_anchor_distance=self.threshold + PASSWORD_ANCHOR_SLACK, poor_light=True)
        log.info("after a sign-in without your face: learned %d view(s) (%s)", result.added,
                 result.reason)
        if result.added:
            self.adaptation.note("learning", None, f"{result.added} view(s)",
                                 "Learned how you looked on the sign-in screen before you used "
                                 "your password", by="automatic")

    def _learn_while_present(self, verdict: Verdict) -> None:
        """While you sit here, fill in times of day that have no learned view yet."""
        cfg = self.cfg
        now = self._wall_clock()
        if (not (cfg.adaptive_enrollment and cfg.adaptive_learn_while_present)
                or now - self._presence_learned_at < PRESENCE_LEARN_GAP_S
                or self.recognizer.slot_recently_learned(now)):
            return
        views = [f for f in verdict.frames if f.match and f.embedding is not None]
        result = self.recognizer.learn_now(views, source=SOURCE_PRESENCE, limit=1,
                                           max_anchor_distance=self.threshold)
        if result.added:
            self._presence_learned_at = now
            log.info("learned a view of you for this time of day")

    def _ensure_lock_session(self) -> None:
        """Time the rest of a lock that began before the service started."""
        if self._lock_session is not None or self.watcher is None or not self.watcher.locked:
            return
        with self._session_lock:
            if self._lock_session is None:
                self._lock_session = _LockSession(self._clock(), self._wall_clock(),
                                                  inferred=True)

    # ---- lock-screen routine -------------------------------------------------

    def _routine_in_use(self) -> bool:
        return self.cfg.unlock_learn_routine and self.routine.schedule() is not None

    def _eager_window(self) -> float:
        """Continuous scanning after the screen locks or wakes, or a face is seen."""
        if self._routine_in_use():
            return min(EAGER_WINDOW_S, self.cfg.unlock_idle_after_s)
        return self.cfg.unlock_idle_after_s

    def _start_window(self) -> float:
        """Continuous scanning when the lock screen appears. None after an automatic
        lock: by then you have already been away for a while."""
        session = self._lock_session
        if session is not None and session.automatic:
            return 0.0
        return self._eager_window()

    def _likely_back(self) -> bool:
        """The learned routine says you're probably back soon: keep scanning."""
        if not self.cfg.unlock_learn_routine:
            return False
        session = self._lock_session
        locked_at = None if session is None or session.inferred else session.started_wall
        return self.routine.likely_back(self._wall_clock(), locked_at)

    def _model_ready(self, wait_s: float) -> bool:
        """Wait (bounded) for start-up loading, so the camera never waits on the model."""
        if not self._warm.wait(max(0.0, wait_s)):
            return False
        self.recognizer.embedder.load()  # no-op unless the start-up warm-up is turned off
        return True

    # ---- helpers -----------------------------------------------------------

    @property
    def threshold(self) -> float:
        return self.adaptation.effective_threshold(self.cfg)

    def _unlock_priority_active(self) -> bool:
        return self._clock() < self._unlock_priority_until or self.camera.unlock_waiting()

    def _credentials_caller_ok(self, client: ClientInfo) -> bool:
        mode = self.cfg.unlock_client_check
        if mode == "off":
            return True
        identity = (client.sid, client.is_system)
        with self._session_lock:
            first, self._caller_seen = identity != self._caller_seen, identity
        if first or not client.is_system:  # once per lock, and every other caller
            self.metrics.record("unlock_caller", system=client.is_system,
                                identified=client.sid is not None)
        if client.is_system:
            return True
        log.warning("credential request from a non-system caller (pid=%s)", client.pid)
        return mode != "enforce"

    def _observe_lighting(self, verdict: Verdict) -> str | None:
        face = next((f for f in verdict.frames if f.quality and f.outcome is not Outcome.NO_FACE),
                    None)
        if face is None or face.quality is None:
            return None
        self.camera_profile.observe_lighting(face.quality.lighting, face.quality.face_brightness)
        return face.quality.lighting

    @staticmethod
    def _frame_summary(verdict: Verdict) -> list[dict]:
        return [{"outcome": f.outcome.value,
                 "distance": round(f.distance, 4) if f.outcome not in (
                     Outcome.NO_FACE, Outcome.SPOOF) else None,
                 "issue": f.issue,
                 "face_brightness": round(f.quality.face_brightness, 1)
                 if f.quality and math.isfinite(f.quality.face_brightness) else None}
                for f in verdict.frames]

    def _record_attempt(self, kind: str, verdict: Verdict | None, *, reason: str,
                        session: CameraSession | None = None, extra: dict | None = None) -> None:
        data: dict[str, Any] = {"reason": reason}
        if verdict is not None:
            data.update(frames=len(verdict.frames), matches=verdict.counted_matches,
                        best=round(verdict.best_distance, 4), verify_ms=verdict.elapsed_ms,
                        issue=verdict.issue or "",
                        outcomes=[f.outcome.value for f in verdict.frames])
            face = next((f for f in verdict.frames if f.quality and f.face_count), None)
            if face is not None and face.quality is not None:
                data.update(face_brightness=face.quality.face_brightness,
                            lighting=face.quality.lighting)
        if session is not None:
            data.update(settle_ms=session.settle.ms, settle_frames=session.settle.frames,
                        fps=session.camera.fps or 0)
        data["threshold"] = self.threshold
        if extra:
            data.update(extra)
        self.metrics.record(kind, **data)

    def _verify(self, session: CameraSession, policy: VerifyPolicy, *,
                preempted: Callable[[], bool] | None = None) -> Verdict:
        verdict = verification.run(
            session, self.recognizer, policy, threshold=self.threshold,
            first_frame=session.first_frame, preempted=preempted or session.preempted,
            keep_crops=self.cfg.adaptive_keep_photos)
        lighting = self._observe_lighting(verdict)
        # In dim light a lower frame rate lets the camera expose longer. Use
        # it within this attempt only once it has proven useful here.
        dark = verdict.issue in ("too_dark", "no_face", "unusable", "uncertain")
        if (not verdict.accepted and dark and lighting in ("dim", "dark", "backlit")
                and self.cfg.camera_low_light_fps and not session.camera.fps
                and self.camera_profile.low_light_state() == "enabled"):
            session.switch_to_low_light()
            retry = verification.run(
                session, self.recognizer, policy, threshold=self.threshold,
                first_frame=session.first_frame, preempted=preempted or session.preempted,
                keep_crops=self.cfg.adaptive_keep_photos)
            retry.frames = verdict.frames + retry.frames
            verdict = retry
        return verdict

    def _trial_low_light(self, session: CameraSession, verdict: Verdict) -> None:
        """Background-only experiment: does a lower frame rate brighten your face?"""
        lighting = self._observe_lighting(verdict)
        if not (lighting and self.cfg.camera_low_light_fps
                and self.camera_profile.should_trial_low_light(lighting)
                and not session.camera.fps):
            return
        before = [f.quality.face_brightness for f in verdict.frames
                  if f.quality and math.isfinite(f.quality.face_brightness)]
        if not before:
            return
        cost = session.switch_to_low_light()
        if cost is None:
            return
        after = []
        for _ in range(4):
            frame = session.read_frame()
            box = self.detector.detect_largest(frame)
            if box is not None:
                quality = assess(to_gray(frame), box)
                if math.isfinite(quality.face_brightness):
                    after.append(quality.face_brightness)
        if after:
            self.camera_profile.observe_fps_switch(statistics.median(before),
                                                   statistics.median(after), cost)
            self.metrics.record("low_light_trial", before=statistics.median(before),
                                after=statistics.median(after), cost_ms=cost)

    # ---- core commands ------------------------------------------------------

    def unlock(self, client: ClientInfo, selected: Any = None) -> dict:
        if not self._credentials_caller_ok(client):
            return {"ok": False, "reason": "caller-not-allowed"}
        started = self._clock()
        self._unlock_priority_until = started + UNLOCK_PRIORITY_GRACE_S
        self._ensure_lock_session()
        ticket = self.pacer.begin(selected if isinstance(selected, bool) else None)
        face_seen = False
        verdict: Verdict | None = None
        session_ref: CameraSession | None = None
        mode = "none"
        reason = "error"
        looked_at: float | None = None
        try:
            turn = self.pacer.wait_turn(ticket)
            if turn != "go":
                reason = turn
                return {"ok": False, "reason": turn}
            self.recognizer.require_profile()
            if not self._model_ready(UNLOCK_BUDGET_S - (self._clock() - started)):
                reason = "starting"
                return {"ok": False, "reason": "starting"}
            mode = self.pacer.mode
            policy = VerifyPolicy.for_unlock(self.cfg)
            if mode == "idle":
                # A quick look: one frame; a full attempt only if someone is there.
                policy = replace(policy, max_no_face=1)
            if ticket.interactive:
                # You selected the tile: look longer and at more frames (never more loosely).
                policy = replace(policy, max_frames=max(policy.max_frames, 8),
                                 max_no_face=max(policy.max_no_face, 5),
                                 time_budget_s=max(policy.time_budget_s, 6.0))
            looked_at = self._clock()
            with self.camera.session("unlock", wait_s=8.0) as session:
                session_ref = session
                verdict = self._verify(session, policy, preempted=lambda: (
                    self.pacer.generation != ticket.generation))
                face_seen = any(f.outcome is not Outcome.NO_FACE for f in verdict.frames)
                session.release_after = (0.0 if verdict.accepted or mode != "active"
                                         else self.camera.hold_seconds())
            self._keep_for_learning(verdict)
            armed = self._armed_after_look(face_seen, ticket.interactive)
            reason = verdict.reason
            if not verdict.accepted:
                return {"ok": False, "reason": verdict.reason, "guidance": verdict.issue,
                        "distance": round(verdict.best_distance, 4)}
            if not armed:
                reason = "just-locked"
                return {"ok": False, "reason": "just-locked"}
            if self._password_out_of_date():
                reason = "password-rejected"
                return {"ok": False, "reason": "password-rejected"}
            credential = load_password()
            if not credential:
                reason = "no-credentials"
                return {"ok": False, "reason": "no-credentials"}
            attempt_id = secrets.token_urlsafe(18)
            self.recognizer.stage_learning(verdict.frames, attempt_id=attempt_id)
            self._remember_attempt(attempt_id, verdict)
            return {"ok": True, "username": credential["u"], "password": credential["p"],
                    "domain": credential.get("d", "."), "attempt_id": attempt_id}
        except NotEnrolledError:
            reason = "not-enrolled"
            return {"ok": False, "reason": "not-enrolled"}
        except SpoofModelUnavailable:
            reason = "anti-spoofing-unavailable"
            return {"ok": False, "reason": reason}
        except CameraUnavailable as exc:
            reason = exc.reason
            return {"ok": False, "reason": exc.reason}
        except CameraError as exc:
            reason = f"camera-{exc.kind}"
            return {"ok": False, "reason": reason, "message": str(exc)}
        finally:
            self.pacer.end(face_seen=face_seen)
            self._note_attempt(verdict, reason, mode, looked_at)
            quiet = reason == "just-locked" or (reason in QUIET_REASONS and not face_seen)
            if not quiet:
                self._record_attempt("unlock_attempt", verdict, reason=reason,
                                     session=session_ref,
                                     extra={"mode": mode, "caller_system": client.is_system,
                                            "interactive": ticket.interactive})

    def _armed_after_look(self, face_seen: bool, interactive: bool) -> bool:
        """May this look sign you in? After you lock the PC yourself, not until a
        look finds nobody there (you stepped away) or you select the tile."""
        with self._session_lock:
            session = self._lock_session
            if session is None or session.armed:
                return True
            selected = (interactive
                        and self._clock() - session.started >= SELECTION_ARMS_AFTER_S)
            if face_seen and not selected:
                return False
            session.armed = True
        log.info("face sign-in ready again: %s",
                 "you selected the tile" if interactive else "you stepped away")
        return True

    def _remember_attempt(self, attempt_id: str, verdict: Verdict) -> None:
        # Keep one spare matching frame (not the learned one) as evaluation data.
        spare = [f for f in verdict.frames if f.match and f.embedding is not None]
        eval_frame = spare[-1] if len(spare) >= 2 else None
        now = self._clock()
        with self._attempts_lock:
            for key in [k for k, v in self._attempts.items() if now - v.created > ATTEMPT_TTL_S]:
                self._attempts.pop(key, None)
            self._attempts[attempt_id] = _Attempt(
                now, eval_frame.embedding if eval_frame else None,
                eval_frame.quality.lighting if eval_frame and eval_frame.quality else "unknown")

    def unlock_result(self, attempt_id: Any, accepted: Any, status: Any = None,
                      substatus: Any = None) -> dict:
        if not isinstance(attempt_id, str) or not isinstance(accepted, bool):
            return {"ok": False, "reason": "invalid-unlock-result"}
        with self._attempts_lock:
            attempt = self._attempts.pop(attempt_id, None)
        result = self.recognizer.finish_learning(attempt_id, accepted)
        if attempt is None:
            log.info("unlock result for an unknown, expired, or repeated attempt ignored")
            return {"ok": True, "added": 0}
        self._note_password_result(accepted, password_rejected(accepted, status, substatus))
        if accepted:
            self._face_sign_in_at = self._wall_clock()
            with self._session_lock:
                if self._lock_session is not None:
                    self._lock_session.face_accepted = True
            if attempt.eval_embedding is not None:
                self.evaluation.add([attempt.eval_embedding], source="unlock",
                                    lighting=[attempt.eval_lighting],
                                    session=secrets.token_hex(6))
        self.metrics.record("unlock_result", accepted=accepted, added=result.added,
                            reason=result.reason)
        log.info("face sign-in result accepted=%s learned=%d (%s)", accepted, result.added,
                 result.reason)
        return {"ok": True, "added": result.added}

    def presence(self) -> dict:
        mode = self.cfg.presence_mode
        if self.camera.leased_to():
            return {"ok": True, "present": True, "skipped": "camera-leased", "mode": mode}
        if self._unlock_priority_active():
            return {"ok": True, "present": True, "skipped": "unlock-active", "mode": mode}
        if (self.cfg.presence_pause_while_locked and self.watcher is not None
                and self.watcher.locked):
            return {"ok": True, "present": True, "skipped": "locked", "mode": mode}
        verdict: Verdict | None = None
        try:
            if mode != "detection":
                self.recognizer.require_profile()  # before the camera turns on
                if not self._model_ready(0.0):  # never hold the camera while the model loads
                    return {"ok": True, "present": True, "skipped": "starting", "mode": mode}
            with self.camera.session("presence", wait_s=2.0) as session:
                if mode == "detection":
                    present = self._detect_any_face(session)
                    self.metrics.record("presence_check", reason="face" if present
                                        else "no_face", mode=mode)
                    return {"ok": True, "present": present, "mode": mode,
                            "face_sign_in_at": self._face_sign_in_at}
                verdict = self._verify(session, VerifyPolicy.for_presence(self.cfg))
                if verdict.reason == "preempted":
                    return {"ok": True, "present": True, "skipped": "unlock-active", "mode": mode}
                self._record_attempt("presence_check", verdict, reason=verdict.reason,
                                     session=session)
                self._trial_low_light(session, verdict)
            if verdict.accepted:
                self._learn_while_present(verdict)
        except NotEnrolledError:
            return {"ok": False, "reason": "not-enrolled"}
        except SpoofModelUnavailable:
            return {"ok": False, "reason": "anti-spoofing-unavailable"}  # never "away"
        except CameraUnavailable as exc:
            return {"ok": True, "present": True, "skipped": exc.reason, "mode": mode}
        except CameraError as exc:
            return {"ok": False, "reason": f"camera-{exc.kind}", "message": str(exc)}
        return {"ok": True, "present": verdict.accepted, "mode": mode, "outcome": verdict.reason,
                "guidance": verdict.issue, "distance": round(verdict.best_distance, 4),
                "face_sign_in_at": self._face_sign_in_at}

    def _detect_any_face(self, session: CameraSession) -> bool:
        frame = session.first_frame
        for _ in range(5):
            if frame is not None and self.detector.has_face(frame):
                return True
            frame = session.read_frame()
        return False

    def check(self, *, record: bool) -> dict:
        """A recognition test the signed-in user runs from the app."""
        try:
            self.recognizer.require_profile()
            if not self._model_ready(CHECK_MODEL_WAIT_S):
                return {"ok": False, "reason": "starting",
                        "message": "Face Unlock is still starting. Try again in a moment."}
            with self.camera.session("check", wait_s=5.0) as session:
                verdict = self._verify(session, VerifyPolicy.for_check(self.cfg),
                                       preempted=lambda: False)
                open_ms = (session.camera.report.open_ms if session.camera.report else 0.0)
        except NotEnrolledError:
            return {"ok": False, "reason": "not-enrolled"}
        except CameraUnavailable as exc:
            return {"ok": False, "reason": exc.reason, "message": str(exc)}
        except CameraError as exc:
            return {"ok": False, "reason": f"camera-{exc.kind}", "message": str(exc)}
        faces = [f for f in verdict.frames if f.embedding is not None and f.quality
                 and f.quality.usable]
        if record and faces:
            self.evaluation.add([f.embedding for f in faces], source="check",
                                lighting=[f.quality.lighting for f in faces],  # type: ignore[union-attr]
                                session=secrets.token_hex(6))
        self._record_attempt("check", verdict, reason=verdict.reason)
        return {"ok": True, "accepted": verdict.accepted, "reason": verdict.reason,
                "guidance": verdict.issue, "threshold": round(self.threshold, 4),
                "frames": self._frame_summary(verdict), "verify_ms": round(verdict.elapsed_ms),
                "camera_open_ms": round(open_ms), "recorded": bool(record and faces)}

    # ---- enrollment ----------------------------------------------------------

    def _staging_dir(self, name: Any) -> Path:
        if not isinstance(name, str) or not name or "/" in name or "\\" in name or ".." in name:
            raise ValueError("invalid staging folder")
        path = APP_DIR / "staging" / name
        if not path.is_dir():
            raise ValueError("staging folder not found")
        return path

    def build_enrollment(self, request: dict) -> dict:
        mode = request.get("mode", "replace")
        dry_run = bool(request.get("dry_run", False))
        poor_light = request.get("allow_poor_light") is True  # you chose to keep such photos
        staging = request.get("staging")
        if staging:
            folder = self._staging_dir(staging)
        else:
            folder = ENROLL_DIR
        photos = sorted(p for p in folder.iterdir() if p.suffix.lower() in IMAGE_SUFFIXES)
        if not photos:
            return {"ok": False, "reason": "no-photos"}
        if len(photos) > MAX_STAGING_PHOTOS:
            return {"ok": False, "reason": "too-many-photos"}
        source = SOURCE_MANUAL if mode == "append" else SOURCE_ENROLLMENT
        holdout = 0 if dry_run or not staging else 7
        try:
            report = self.recognizer.build_profile(photos, mode=mode, source=source,
                                                   dry_run=dry_run, holdout_every=holdout,
                                                   allow_poor_light=poor_light)
        except ValueError as exc:
            return {"ok": False, "reason": "no-usable-face", "message": str(exc)}
        if not dry_run and staging:
            self._commit_staging(folder, mode, {r.name for r in report.results if r.accepted})
            if report.holdout:
                self.evaluation.add([e for e, _ in report.holdout], source="holdout",
                                    lighting=[light for _, light in report.holdout],
                                    session=secrets.token_hex(6))
            self.adaptation.note("enrollment", None, f"{mode}: {report.accepted} photos",
                                 "You saved a new face profile" if mode == "replace"
                                 else "You added your current appearance", by="you")
        return {"ok": True, "mode": mode, "dry_run": dry_run, "accepted": report.accepted,
                "rejected": report.rejected, "anchors": report.anchors,
                "learned": report.learned, "consistency": report.consistency,
                "results": [asdict(r) for r in report.results][:MAX_STAGING_PHOTOS]}

    def _commit_staging(self, folder: Path, mode: str, accepted: set[str]) -> None:
        stamp = time.strftime("%Y%m%d-%H%M%S")
        ENROLL_DIR.mkdir(parents=True, exist_ok=True)
        if mode == "replace":
            backup = BACKUP_DIR / f"enroll-{stamp}"
            backup.mkdir(parents=True, exist_ok=True)
            for old in ENROLL_DIR.iterdir():
                if old.is_file():
                    shutil.move(str(old), backup / old.name)
        for photo in folder.iterdir():
            if photo.name in accepted:
                shutil.move(str(photo), ENROLL_DIR / photo.name)
        shutil.rmtree(folder, ignore_errors=True)

    # ---- status & insights ----------------------------------------------------

    def _device(self) -> dict:
        now = self._clock()
        cached = self._device_status
        if cached is None or now - cached[0] > STATUS_DEVICE_TTL_S:
            cached = (now, self.camera.probe_device())
            self._device_status = cached
        return cached[1]

    def status(self) -> dict:
        anchors, learned = self.recognizer.profile_counts()
        device = self._device()
        camera = {"device": device, "activity": self.camera.status()}
        reasons = []
        if anchors == 0:
            reasons.append("not-enrolled")
        if not CREDS_PATH.exists():
            reasons.append("no-credentials")
        elif self._password_out_of_date():
            reasons.append("password-rejected")
        if device.get("state") != "ready":
            reasons.append(f"camera-{device.get('state')}")
        if self._model_error and not self.recognizer.embedder.loaded:
            reasons.append("model-unavailable")
        if self.cfg.anti_spoofing and self.recognizer.embedder.spoof_error:
            reasons.append("anti-spoofing-unavailable")
        ready = not reasons
        profile = self.recognizer.profile
        last_learned = None
        if profile is not None and profile.learned_count:
            last_learned = float(profile.created_at[profile.protected_count:].max())
        return {
            "ok": True, "version": __version__, "pid": os.getpid(),
            "uptime_s": round(time.time() - self._started, 1),
            "ready": ready, "not_ready": reasons, "warming": self._ready_reason,
            "config_problems": self.config_problems,
            "profile": {"anchors": anchors, "learned": learned},
            "camera": camera,
            "recognition": {"threshold": self.cfg.threshold,
                            "effective": round(self.threshold, 4),
                            "anti_spoofing": self.cfg.anti_spoofing,
                            "model": self.cfg.model_name, "strategy": self.cfg.unlock_strategy,
                            "model_loaded": self.recognizer.embedder.loaded,
                            "model_present": recognition_model_present(),
                            "model_error": self._model_error,
                            "compiled": self.recognizer.embedder.compiled_ok},
            "learning": {"enabled": self.cfg.adaptive_enrollment, "last": last_learned},
            "credentials": CREDS_PATH.exists(),
            "lock_screen": {"mode": self.pacer.mode,
                            "display": self.watcher.display if self.watcher else None,
                            "locked": self.watcher.locked if self.watcher else None,
                            "routine": ("off" if not self.cfg.unlock_learn_routine
                                        else "in-use" if self._routine_in_use()
                                        else "learning"),
                            "waiting_for_you_to_leave": bool(
                                self._lock_session and not self._lock_session.armed)},
            "last_face_sign_in": self._face_sign_in_at,
        }

    def insights(self, days: float = 30.0) -> dict:
        from .insights import build_insights
        return build_insights(self, days=days)

    # ---- request dispatch ----------------------------------------------------

    def handle(self, request: dict, client: ClientInfo) -> dict:
        cmd = request.get("cmd")
        log.log(logging.DEBUG if cmd in QUIET_COMMANDS else logging.INFO, "request cmd=%s", cmd)
        handler = self._handlers().get(cmd)
        if handler is None:
            return {"ok": False, "reason": "unknown-command"}
        return handler(request, client)

    def _handlers(self) -> dict[str, Callable[[dict, ClientInfo], dict]]:
        return {
            "ping": lambda r, c: {"ok": True, "pong": True},
            "status": lambda r, c: self.status(),
            "insights": lambda r, c: {"ok": True, **self.insights(float(r.get("days", 30)))},
            "reload_config": lambda r, c: self.reload_config(),
            "shutdown": lambda r, c: self.shutdown(),
            "unlock": lambda r, c: self.unlock(c, r.get("selected")),
            "unlock_result": lambda r, c: self.unlock_result(
                r.get("attempt_id"), r.get("accepted"), r.get("status"), r.get("substatus")),
            "presence": lambda r, c: self.presence(),
            "check": lambda r, c: self.check(record=bool(r.get("record", True))),
            "verify": lambda r, c: self._legacy_verify(),
            "pause_camera": lambda r, c: {"ok": True, "lease_s": self.camera.lease(
                str(r.get("owner", "enrollment")), float(r.get("seconds", 120)))},
            "renew_camera": lambda r, c: {"ok": self.camera.renew(
                str(r.get("owner", "enrollment")), float(r.get("seconds", 120)))},
            "resume_camera": lambda r, c: {"ok": True, "ended": self.camera.end_lease(
                r.get("owner"))},
            "build_enrollment": lambda r, c: self.build_enrollment(r),
            "profile_health": lambda r, c: {"ok": True, **self.recognizer.profile_health()},
            "map_anchor_files": lambda r, c: {"ok": True,
                                              "mapped": self.recognizer.map_anchor_files()},
            "remove_anchors": lambda r, c: self._remove_anchors(r),
            "learning_undo": lambda r, c: self._learning_undo(r),
            "learning_reset": lambda r, c: self._learning_reset(),
            "threshold_apply": lambda r, c: self._threshold_apply(r),
            "threshold_clear": lambda r, c: self._threshold_clear(),
            "snapshot_take": lambda r, c: {"ok": True, "snapshot": self.adaptation.take_snapshot(
                self.cfg, str(r.get("label", "Saved by you"))[:80])},
            "snapshot_restore": lambda r, c: self._snapshot_restore(r),
            "camera_list": lambda r, c: self._camera_list(),
            "clear_measurements": lambda r, c: self._clear_measurements(),
            "low_light_reset": lambda r, c: self._low_light_reset(),
            "routine_reset": lambda r, c: self._routine_reset(),
            "auto_lock": lambda r, c: self._expect_auto_lock(r.get("away_s"),
                                                             r.get("cancel") is True),
            "calibrate_focus": lambda r, c: self._calibrate_focus(),
            "prepare_model": lambda r, c: self._prepare_model(),
        }

    def _legacy_verify(self) -> dict:
        result = self.check(record=False)
        if not result.get("ok"):
            return result
        distances = [f["distance"] for f in result["frames"] if f["distance"] is not None]
        return {"ok": True, "match": result["accepted"],
                "distance": min(distances) if distances else 1.0, "real": True,
                "frames": len(result["frames"]), "verify_ms": result["verify_ms"]}

    def _remove_anchors(self, request: dict) -> dict:
        indices = request.get("indices")
        if not isinstance(indices, list) or not all(isinstance(i, int) for i in indices):
            return {"ok": False, "reason": "invalid-indices"}
        removed = self.recognizer.remove_anchors(indices)
        files = [name for name in removed if name]
        if files:
            # Keep the photos (out of the enrollment folder) so the removal can be undone.
            backup = BACKUP_DIR / f"removed-photos-{time.strftime('%Y%m%d-%H%M%S')}"
            backup.mkdir(parents=True, exist_ok=True)
            for name in files:
                source = ENROLL_DIR / name
                if source.exists():
                    shutil.move(str(source), backup / name)
        if removed:
            self.adaptation.note("enrollment", None, f"removed {len(removed)} photo(s)",
                                 "You removed reference photos", by="you")
        return {"ok": True, "removed": len(removed)}

    def _learning_undo(self, request: dict) -> dict:
        since = request.get("since")
        ids = request.get("ids")
        removed = self.recognizer.undo_learning(
            since=float(since) if isinstance(since, (int, float)) else None,
            sample_ids=ids if isinstance(ids, list) else None)
        if removed:
            self.adaptation.note("learning", None, f"undid {removed} sample(s)",
                                 "You undid recent learning", by="you")
        return {"ok": True, "removed": removed}

    def _learning_reset(self) -> dict:
        removed = self.recognizer.reset_learning()
        self.adaptation.note("learning", None, "reset",
                             f"You reset learned data ({removed} samples); original enrollment kept",
                             by="you")
        return {"ok": True, "removed": removed}

    def _threshold_report(self):
        profile = self.recognizer.profile
        if profile is None:
            return None
        return evaluate_threshold(
            self.evaluation.samples(profile), current=self.threshold,
            baseline=self.cfg.threshold, sequences=self.evaluation.sequences(profile),
            required=self.cfg.unlock_verify_required, max_frames=self.cfg.unlock_max_frames)

    def _session_rate(self) -> float | None:
        sessions = [e for e in self.metrics.events({"lock_session"}) if e.get("face_seen")][-30:]
        if len(sessions) < 5:
            return None
        return sum(1 for e in sessions if e.get("face_accepted")) / len(sessions)

    def _threshold_apply(self, request: dict, *, by: str = "you") -> dict:
        report = self._threshold_report()
        if report is None or report.status != "tighten":
            return {"ok": False, "reason": "no-recommendation"}
        value = request.get("value", report.recommended)
        if not isinstance(value, (int, float)) or abs(value - report.recommended) > 1e-9:
            return {"ok": False, "reason": "stale-recommendation"}
        self.adaptation.take_snapshot(self.cfg, "Before a stricter recognition setting")
        entry = self.adaptation.apply_threshold(
            float(value), cfg=self.cfg, reason=report.explanation,
            evidence=report.to_dict()["evidence"], by=by, session_rate=self._session_rate())
        return {"ok": True, "entry": entry}

    def _threshold_clear(self) -> dict:
        entry = self.adaptation.clear_tuned_threshold(reason="You restored your own setting",
                                                      by="you")
        return {"ok": True, "entry": entry}

    def maybe_auto_tighten(self) -> None:
        """Opt-in: apply a stricter threshold when evidence supports it."""
        if not self.cfg.auto_tighten_threshold:
            return
        state = self.adaptation.snapshot_dict()["threshold"]
        if state.get("probation"):
            return
        report = self._threshold_report()
        if report is not None and report.status == "tighten":
            self._threshold_apply({"value": report.recommended}, by="automatic")

    def _snapshot_restore(self, request: dict) -> dict:
        """Return a saved configuration (the app saves it) and restore its tuning."""
        index = request.get("index")
        if not isinstance(index, int):
            return {"ok": False, "reason": "invalid-index"}
        try:
            restored = self.adaptation.restore_snapshot(index)
        except (IndexError, ValueError) as exc:
            return {"ok": False, "reason": "invalid-snapshot", "message": str(exc)}
        return {"ok": True, "config": asdict(restored)}

    def _camera_list(self) -> dict:
        try:
            cameras = list_cameras()
        except Exception as exc:
            return {"ok": False, "reason": "enumeration-failed", "message": str(exc)}
        return {"ok": True, "cameras": [
            {"name": c.name, "path": c.path, "virtual": c.virtual,
             "selected": bool(self.cfg.camera_device_id) and c.identity ==
             self.cfg.camera_device_id.casefold().split("#{", 1)[0]} for c in cameras]}

    def _clear_measurements(self) -> dict:
        removed = self.metrics.clear()
        self.evaluation.clear()
        self.adaptation.note("measurements", None, "cleared",
                             "You cleared local measurements", by="you")
        return {"ok": True, "removed": removed}

    def _low_light_reset(self) -> dict:
        self.camera_profile.reset()
        self.adaptation.note("camera", None, "re-measure",
                             "You asked the app to re-measure camera behaviour", by="you")
        return {"ok": True}

    def _calibrate_focus(self) -> dict:
        """Sweep the lens while you sit as usual; the app saves the sharpest position."""
        try:
            with self.camera.session("check", wait_s=5.0) as session:
                result = calibrate_focus(session.camera, self.detector)
        except CameraUnavailable as exc:
            return {"ok": False, "reason": exc.reason, "message": str(exc)}
        except CameraError as exc:
            return {"ok": False, "reason": f"camera-{exc.kind}", "message": str(exc)}
        if result.best is None:
            return {"ok": False, "reason": "no-face",
                    "message": "Your face wasn't clear enough to judge the focus. Sit as you "
                               "normally do, facing the camera, and try again."}
        return {"ok": True, "position": result.best, "positions": result.faces_seen}

    def _expect_auto_lock(self, away_s: Any, cancel: bool = False) -> dict:
        """The presence monitor is about to lock because you are away: the next lock
        is not yours, so face sign-in may welcome you back straight away."""
        if cancel:  # Windows refused to lock
            self._auto_lock_notice = None
            return {"ok": True}
        valid = isinstance(away_s, (int, float)) and 0 <= away_s <= 7 * 86400
        self._auto_lock_notice = (self._clock(), float(away_s) if valid else 0.0)
        return {"ok": True}

    def _routine_reset(self) -> dict:
        self.routine.reset()
        self.adaptation.note("routine", None, "forgotten",
                             "You asked the app to forget when you usually come back", by="you")
        return {"ok": True}

    # ---- lifecycle -----------------------------------------------------------

    def reload_config(self) -> dict:
        result = load_config()
        if result.source == "defaults" and result.problems:
            return {"ok": False, "reason": "invalid-config", "problems": result.problems}
        old = self.cfg
        if asdict(old) != asdict(result.config):
            self.adaptation.take_snapshot(old, "Before your settings change")
        self.cfg = result.config
        self.recognizer.cfg = result.config
        self.config_problems = result.problems
        if (old.camera_device_id != self.cfg.camera_device_id
                or old.camera_index != self.cfg.camera_index
                or old.persistent_camera != self.cfg.persistent_camera
                or old.camera_focus != self.cfg.camera_focus
                or old.camera_face_exposure != self.cfg.camera_face_exposure):
            self.camera.release_now()
            self._device_status = None
        log.info("configuration reloaded (%d problem(s))", len(result.problems))
        return {"ok": True, "problems": result.problems}

    def shutdown(self) -> dict:
        log.info("shutdown requested")
        self._stopping.set()
        self.pacer.stop()
        if self.server is not None:
            self.server.stop()
        return {"ok": True, "shutting_down": True}

    def start_warmup(self) -> threading.Thread:
        """Load in the background; requests that need the camera wait for it (bounded)."""
        self._warm.clear()
        thread = threading.Thread(target=self.warmup, name="warmup", daemon=True)
        thread.start()
        return thread

    def warmup(self) -> None:
        """Load the profile and model so the first sign-in is fast."""
        self._ready_reason = "loading"
        try:
            provide_bundled_models()  # a new PC: use the shipped model, no download
            try:
                loaded = self.recognizer.load()
                log.info("face profile loaded: %s", loaded)
            except Exception:
                log.exception("face profile could not be loaded")
            if self.cfg.warmup_on_start:
                self._warm_model()
                if self.cfg.anti_spoofing:
                    self.recognizer.embedder.load_spoof_model()  # failures show on the Overview
            self._device()
        finally:
            self._ready_reason = "ready"
            self._warm.set()

    def _warm_model(self) -> None:
        started = time.perf_counter()
        try:
            self.recognizer.embedder.load()
            # Compare the compiled model with DeepFace on a stored photo
            # (the first few may contain no detectable face).
            for sample in sorted(ENROLL_DIR.glob("*.jpg"))[:12]:
                image = cv2.imread(str(sample))
                distance = (self.recognizer.embedder.self_check(image)
                            if image is not None else None)
                if distance is not None:
                    log.info("embedding self-check: max difference %.2e (%s)", distance,
                             "ok" if self.recognizer.embedder.compiled_ok else "fallback")
                    break
            else:
                log.info("embedding self-check skipped: no usable stored photo")
            log.info("model ready in %.1f s", time.perf_counter() - started)
            self._model_error = None
        except Exception as exc:
            log.exception("model warm-up failed")
            lines = str(exc).strip().splitlines()
            self._model_error = (lines[-1] if lines else type(exc).__name__)[:200]

    def _prepare_model(self) -> dict:
        """Try loading the models again (after a failed download, say), in the background."""
        if self.recognizer.embedder.loaded:
            return {"ok": True, "loaded": True}
        if not self._preparing.acquire(blocking=False):
            return {"ok": True, "started": False}  # already trying

        def run() -> None:
            try:
                self._warm_model()
                if self.cfg.anti_spoofing:
                    self.recognizer.embedder.load_spoof_model()
            finally:
                self._preparing.release()

        threading.Thread(target=run, name="prepare-model", daemon=True).start()
        return {"ok": True, "started": True}


def _single_instance(wait_s: float = 15.0, name: str = SERVICE_MUTEX) -> Any:
    """The service's instance lock. One that is still shutting down (a quick stop
    and start) is waited for, briefly, instead of being taken for a running one."""
    import win32api
    import win32event
    import winerror
    deadline = time.monotonic() + wait_s
    while True:
        mutex = win32event.CreateMutex(None, False, name)
        if win32api.GetLastError() != winerror.ERROR_ALREADY_EXISTS:
            return mutex
        mutex.Close()
        if time.monotonic() >= deadline:
            return None
        time.sleep(0.5)


def main() -> None:
    setup_logging(LOG_PATH)
    result = load_config()
    for problem in result.problems:
        log.warning("config: %s", problem)
    mutex = _single_instance()
    if mutex is None:
        log.warning("another FaceService instance is still running after 15 s; exiting")
        return
    watcher = SessionWatcher()
    service = FaceService(result.config, problems=result.problems, watcher=watcher)
    watcher.start()
    server = PipeServer(PIPE_NAME, service.handle)
    service.server = server
    log.info("FaceService %s starting; pipe=%s", __version__, PIPE_NAME)
    service.start_warmup()

    def housekeeping() -> None:
        service.adaptation.take_snapshot(service.cfg, "Settings when the service started")
        while not service._stopping.wait(3600):
            try:
                service.maybe_auto_tighten()
            except Exception:
                log.exception("housekeeping failed")

    threading.Thread(target=housekeeping, name="housekeeping", daemon=True).start()
    try:
        server.serve_forever()
    except KeyboardInterrupt:
        pass
    finally:
        service.pacer.stop()
        service.camera.close()
        service.camera_profile.flush()
        watcher.stop()
        log.info("FaceService stopped")


if __name__ == "__main__":
    main()
