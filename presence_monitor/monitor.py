"""Walk-away locking: periodically check that you are still at the PC.

The workstation is locked only after an uninterrupted absence of the
configured length. Anything inconclusive – the service being unavailable, a
camera problem, no face profile, the camera lent to the setup window, a
remote session – cancels the absence timer instead of counting towards it,
so a fault can never cause a blind lock. Checks are skipped entirely while
Windows is already locked; the sign-in screen has its own face scanning.

After a face sign-in you also have to be seen at the PC a little later:
checks run every 15 s, and if none from 30 s onwards finds you within
``relock_unconfirmed_s`` (and at least three found you missing), the PC locks
again. A face recognised while you only walked past therefore cannot leave
the PC open for an hour.
"""
from __future__ import annotations

import ctypes
import logging
import threading
import time
from collections.abc import Callable
from dataclasses import dataclass

from face_service import pipe
from face_service.config import PIPE_NAME, Config
from face_service.metrics import MetricsStore, NullMetrics
from face_service.win_events import is_session_locked

from .absence import AbsenceModel
from .remote_session import is_remote_context

log = logging.getLogger(__name__)

CONFIRM_AFTER_S = 30.0       # someone who sat down is still here by then; a passer-by is not
CONFIRM_INTERVAL_S = 15.0
CONFIRM_MIN_MISSES = 3
RELOCK_TRUST_S = 180.0       # signed straight back in after such a re-lock: you were here
SIGN_IN_MATCH_S = 30.0       # a face sign-in this close to the unlock caused it


def _lock_workstation() -> None:
    ctypes.windll.user32.LockWorkStation()


def pipe_call(request: dict, timeout_s: float = 10.0) -> dict | None:
    """Send one request to FaceService; ``None`` if it is unavailable or slow."""
    return pipe.call(PIPE_NAME, request, timeout_s=timeout_s)


@dataclass
class TickSnapshot:
    at: float = 0.0
    result: str = "-"         # present / absent / skipped / error
    reason: str = ""
    guidance: str = ""
    absence_elapsed_s: float = 0.0


@dataclass
class AbsenceTimer:
    """Monotonic timer measuring time since the last successful face match.

    A first absent result starts a fresh timer. A present result moves the
    anchor to that check. Inconclusive results reset the timer so a camera or
    service failure can never cause a blind lock.
    """

    timeout_s: float
    clock: Callable[[], float] = time.monotonic
    _anchor: float | None = None

    def reset(self) -> None:
        self._anchor = None

    def mark_present(self) -> None:
        self._anchor = self.clock()

    def mark_absent(self) -> tuple[float, bool]:
        now = self.clock()
        if self._anchor is None:
            self._anchor = now
        elapsed = max(0.0, now - self._anchor)
        return elapsed, elapsed >= self.timeout_s

    def elapsed(self) -> float:
        if self._anchor is None:
            return 0.0
        return max(0.0, self.clock() - self._anchor)


class PresenceMonitor:
    def __init__(self, cfg: Config, *, absence: AbsenceModel | None = None,
                 metrics: MetricsStore | None = None,
                 locked: Callable[[], bool | None] = is_session_locked,
                 clock: Callable[[], float] = time.time):
        self.cfg = cfg
        self.absence = absence
        self.metrics = metrics or NullMetrics()
        self._locked = locked
        self._clock = clock
        self._stop = threading.Event()
        self._wake = threading.Event()
        self._paused_until: float | None = None   # inf = until resumed
        self._strikes = 0
        self._absence = AbsenceTimer(cfg.effective_presence_absence_timeout_s)
        self._last = TickSnapshot()
        self._lock_count = 0
        self._self_locked_at = 0.0
        self._timeout_rule = "Your normal lock timeout."
        self._state_lock = threading.Lock()
        self._unlocked_at: float | None = None   # a sign-in still to be confirmed
        self._confirm_misses = 0
        self._relocked_at = float("-inf")

    # ---- control -----------------------------------------------------------

    def pause(self, seconds: float | None = None) -> None:
        """Pause checks for ``seconds`` (``None`` = until resumed)."""
        with self._state_lock:
            self._paused_until = float("inf") if seconds is None else self._clock() + seconds
        self._reset_absence()
        log.info("presence checks paused %s", "until resumed" if seconds is None
                 else f"for {seconds / 60:.0f} min")

    def resume(self) -> None:
        with self._state_lock:
            self._paused_until = None
        self._reset_absence()
        log.info("presence checks resumed")
        self._wake.set()

    def is_paused(self) -> bool:
        with self._state_lock:
            until = self._paused_until
            if until is not None and self._clock() >= until:
                self._paused_until = None
                until = None
        return until is not None

    @property
    def paused_until(self) -> float | None:
        with self._state_lock:
            return self._paused_until

    def stop(self) -> None:
        self._stop.set()
        self._wake.set()

    def check_soon(self) -> None:
        """Run the next check now instead of waiting for the interval."""
        self._wake.set()

    def reload_config(self, cfg: Config) -> None:
        self.cfg = cfg
        self._absence = AbsenceTimer(cfg.effective_presence_absence_timeout_s,
                                     self._absence.clock)
        self._strikes = 0
        log.info("presence monitor settings: every %ss, lock after %ss, mode=%s",
                 cfg.presence_interval_s, cfg.effective_presence_absence_timeout_s,
                 cfg.presence_mode)

    # ---- state -------------------------------------------------------------

    def _timeout(self) -> int:
        base = self.cfg.effective_presence_absence_timeout_s
        if self.cfg.adaptive_lock and self.absence is not None:
            timeout, rule = self.absence.timeout_for(
                self._clock(), base_timeout_s=base,
                minimum_s=self.cfg.adaptive_lock_min_timeout_s)
            self._timeout_rule = rule
            return timeout
        self._timeout_rule = "Your normal lock timeout."
        return base

    def snapshot(self) -> dict:
        with self._state_lock:
            last = self._last
            paused_until = self._paused_until
        return {
            "paused": self.is_paused(),
            "paused_until": None if paused_until in (None, float("inf")) else paused_until,
            "strikes": self._strikes,
            "absence_elapsed_s": last.absence_elapsed_s,
            "absence_timeout_s": self._absence.timeout_s,
            "timeout_rule": self._timeout_rule,
            "lock_count": self._lock_count,
            "last_at": last.at,
            "last_result": last.result,
            "last_reason": last.reason,
            "last_guidance": last.guidance,
            "interval_s": self.cfg.presence_interval_s,
            "mode": self.cfg.presence_mode,
            "confirming": self._confirm_active(),
        }

    def _set_last(self, result: str, reason: str = "", guidance: str = "") -> None:
        with self._state_lock:
            self._last = TickSnapshot(self._clock(), result, reason, guidance or "",
                                      self._absence.elapsed())
        self.metrics.record("presence", result=result, reason=reason,
                            elapsed_s=round(self._absence.elapsed(), 1))

    def _reset_absence(self) -> None:
        self._strikes = 0
        self._absence.reset()

    def _learning(self) -> AbsenceModel | None:
        return self.absence if self.cfg.absence_learning else None

    # ---- session events (from the app's SessionWatcher) ---------------------

    def on_session_event(self, name: str, detail: dict) -> None:
        learner = self._learning()
        if name == "lock":
            self._reset_absence()
            self._end_confirmation()
            automatic = self._clock() - self._self_locked_at < 30
            if learner and not automatic:
                learner.observe_lock()
        elif name in ("unlock", "logon"):
            self._reset_absence()
            if learner:
                learner.observe_unlock()
            if name == "unlock":
                with self._state_lock:
                    self._unlocked_at = self._clock()
                    self._confirm_misses = 0
            self.check_soon()
        elif name in ("sleep", "remote-connect", "console-disconnect"):
            self._reset_absence()
            if learner and name != "sleep":
                learner.discard_open(name)

    # ---- confirming a face sign-in -------------------------------------------

    def _confirm_active(self) -> bool:
        with self._state_lock:
            unlocked_at = self._unlocked_at
        window = self.cfg.relock_unconfirmed_s
        return (unlocked_at is not None and window > 0
                and self._clock() - unlocked_at <= window + 3 * CONFIRM_INTERVAL_S)

    def _end_confirmation(self) -> None:
        with self._state_lock:
            self._unlocked_at = None
            self._confirm_misses = 0

    def _confirming(self, response: dict) -> bool:
        """Does this check count towards confirming the last sign-in?"""
        with self._state_lock:
            unlocked_at = self._unlocked_at
        if unlocked_at is None:
            return False
        signed_in = response.get("face_sign_in_at")
        by_face = (isinstance(signed_in, (int, float))
                   and abs(unlocked_at - signed_in) <= SIGN_IN_MATCH_S)
        if (not self._confirm_active() or not by_face
                or unlocked_at - self._relocked_at < RELOCK_TRUST_S):
            self._end_confirmation()  # not a face sign-in, or nothing left to confirm
            return False
        return self._clock() - unlocked_at >= CONFIRM_AFTER_S

    def _lock(self, away_s: float, timeout: float, cause: str) -> None:
        self._reset_absence()
        self._lock_count += 1
        self._self_locked_at = self._clock()
        self.metrics.record("auto_lock", elapsed_s=round(away_s, 1), timeout_s=timeout,
                            cause=cause)
        # The lock is ours, not yours: the service may welcome you back at once.
        pipe_call({"cmd": "auto_lock", "away_s": round(away_s, 1)}, timeout_s=2.0)
        _lock_workstation()

    # ---- the check -----------------------------------------------------------

    def _tick(self) -> None:
        learner = self._learning()
        if not self.cfg.presence_enabled:
            self._reset_absence()
            self._set_last("skipped", "off")
            return
        if self.is_paused():
            self._reset_absence()
            self._set_last("skipped", "paused")
            return
        if self.cfg.presence_pause_while_locked and self._locked():
            self._reset_absence()
            self._set_last("skipped", "locked")
            return
        remote, why = is_remote_context()
        if remote:
            log.info("check skipped: remote session (%s)", why)
            self._reset_absence()
            if learner:
                learner.discard_open("remote session")
            self._set_last("skipped", "remote")
            return

        response = pipe_call({"cmd": "presence"}, timeout_s=25.0)
        if response is None:
            log.warning("presence check: service unavailable; not counting as absence")
            self._reset_absence()
            self._set_last("error", "service-unavailable")
            return
        if not response.get("ok"):
            reason = str(response.get("reason", "check-failed"))
            log.warning("presence check failed: %s", reason)
            self._reset_absence()
            if learner and reason.startswith("camera"):
                learner.discard_open("camera problem")
            self._set_last("error", reason)
            return
        if response.get("skipped"):
            self._reset_absence()
            self._set_last("skipped", str(response["skipped"]))
            return

        present = bool(response.get("present"))
        guidance = str(response.get("guidance") or "")
        confirming = self._confirming(response)
        if present:
            self._strikes = 0
            self._absence.mark_present()
            if learner:
                learner.observe_present()
            self._set_last("present", str(response.get("outcome", "match")))
            if confirming:
                log.info("face sign-in confirmed: you are at the PC")
                self._end_confirmation()
            return

        self._strikes += 1
        elapsed, _ = self._absence.mark_absent()
        timeout = self._timeout()
        if learner:
            learner.observe_absent()
        self._set_last("absent", str(response.get("outcome", "")), guidance)
        if confirming:
            with self._state_lock:
                self._confirm_misses += 1
                misses, unlocked_at = self._confirm_misses, self._unlocked_at
            since = self._clock() - (unlocked_at or self._clock())
            if since >= self.cfg.relock_unconfirmed_s and misses >= CONFIRM_MIN_MISSES:
                log.warning("signed in by face %.0f s ago but not seen at the PC since; "
                            "locking again", since)
                self._end_confirmation()
                self._relocked_at = self._clock()
                self._lock(since, self.cfg.relock_unconfirmed_s, "not-seen-after-sign-in")
                return
        if elapsed >= timeout:
            log.warning("no face for about %.0f min; locking the workstation", elapsed / 60)
            self._lock(elapsed, timeout, "away")

    def run(self) -> None:
        log.info("presence checks every %ss; lock after about %ss; mode=%s",
                 self.cfg.presence_interval_s, self.cfg.effective_presence_absence_timeout_s,
                 self.cfg.presence_mode)
        next_at = time.monotonic()
        while not self._stop.is_set():
            try:
                self._tick()
            except Exception:
                log.exception("presence check crashed; not counting as absence")
                self._reset_absence()
                self._set_last("error", "exception")
            # A fixed cadence: slow checks never cause a catch-up burst.
            interval = (CONFIRM_INTERVAL_S if self._confirm_active()
                        else float(self.cfg.presence_interval_s))
            next_at += interval
            now = time.monotonic()
            if next_at <= now:
                next_at = now + interval
            self._wake.wait(next_at - now)
            if self._wake.is_set():
                self._wake.clear()
                next_at = time.monotonic()
