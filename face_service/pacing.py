"""Lock-screen pacing: how eagerly unlock requests may use the camera.

The sign-in tile asks for a scan again whenever the previous one fails, for
as long as the lock screen is up – measured at one request about every six
seconds, for 25 hours in one case, with the camera kept on throughout. The
tile cannot be changed from here, but the service decides how long each
request takes, so it paces the camera instead:

* active  – the screen just locked or woke, a face was seen recently, or the
            learned routine says you are likely to be back soon: scan at once;
* idle    – otherwise: wait so quick checks happen at most every
            ``unlock_idle_interval_s``, camera off in between;
* display off – the lock screen turned the display off and the user chose to
            pause then: do not scan at all until it wakes (bounded so the
            tile's 30 s request timeout is never reached).

A request that arrives while another is still waiting means the tile was
re-armed by the user (tile click or Enter); it wakes the waiter, which
returns "superseded", and the new request scans immediately.
"""
from __future__ import annotations

import threading
import time
from collections.abc import Callable
from dataclasses import dataclass

SESSION_GAP_S = 30.0      # a request after this long starts a new lock-screen session
MAX_WAIT_S = 15.0         # keep every request well inside the tile's 30 s timeout


@dataclass(frozen=True)
class Ticket:
    generation: int
    session_started: bool
    interactive: bool


class LockScreenPacer:
    def __init__(self, idle_after_s: Callable[[], float], idle_interval_s: Callable[[], float],
                 pause_when_display_off: Callable[[], bool],
                 display_state: Callable[[], str | None] = lambda: None,
                 clock: Callable[[], float] = time.monotonic,
                 likely_back: Callable[[], bool] = lambda: False):
        """``idle_after_s`` is how long scanning stays continuous after the screen
        locks or wakes, or a face is seen; ``likely_back`` (the learned routine)
        keeps it continuous beyond that. Neither may call back into the pacer."""
        self._idle_after_s = idle_after_s
        self._idle_interval_s = idle_interval_s
        self._pause_when_display_off = pause_when_display_off
        self._display_state = display_state
        self._clock = clock
        self._likely_back = likely_back
        self._cond = threading.Condition()
        self._generation = 0
        self._in_flight = 0
        self._last_request = -1e18
        self._last_scan_end = -1e18
        self._last_face = -1e18
        self._active_until = -1e18
        self._stopped = False
        self._last_mode = "none"
        self._mode_time: dict[str, float] = {}

    # ---- state -------------------------------------------------------------

    @property
    def generation(self) -> int:
        """Increments with every request; a scan whose ticket is older is stale."""
        return self._generation

    @property
    def mode(self) -> str:
        now = self._clock()
        with self._cond:
            return self._mode_locked(now)

    def _mode_locked(self, now: float) -> str:
        if now - self._last_request > SESSION_GAP_S and self._in_flight == 0:
            return "none"
        if self._display_paused():
            return "display-off"
        if (now <= self._active_until or now - self._last_face <= self._idle_after_s()
                or self._likely_back()):
            return "active"
        return "idle"

    def _display_paused(self) -> bool:
        return self._pause_when_display_off() and self._display_state() == "off"

    # ---- request lifecycle -------------------------------------------------

    def begin(self) -> Ticket:
        now = self._clock()
        with self._cond:
            session_started = now - self._last_request > SESSION_GAP_S and self._in_flight == 0
            interactive = self._in_flight > 0
            if not session_started:
                # Time between requests, attributed to the mode in force before.
                self._mode_time[self._last_mode] = (self._mode_time.get(self._last_mode, 0.0)
                                                    + now - self._last_request)
            if session_started or interactive:
                # A fresh lock screen or an explicit re-arm: scan right away.
                self._active_until = now + self._idle_after_s()
            self._generation += 1
            self._in_flight += 1
            self._last_request = now
            self._last_mode = self._mode_locked(now)
            self._cond.notify_all()
            return Ticket(self._generation, session_started, interactive)

    def take_mode_time(self) -> dict[str, float]:
        """Seconds spent in each mode since the previous call."""
        with self._cond:
            spent, self._mode_time = self._mode_time, {}
            return spent

    def end(self, *, face_seen: bool) -> None:
        now = self._clock()
        with self._cond:
            self._in_flight = max(0, self._in_flight - 1)
            self._last_scan_end = now
            if face_seen:
                self._last_face = now

    def display_changed(self, state: str) -> None:
        """A display that turns back on means someone woke the PC: be eager."""
        with self._cond:
            if state == "on":
                self._active_until = self._clock() + self._idle_after_s()
            self._cond.notify_all()

    def stop(self) -> None:
        with self._cond:
            self._stopped = True
            self._cond.notify_all()

    def wait_turn(self, ticket: Ticket) -> str:
        """Block until this request may scan.

        Returns ``"go"``, ``"superseded"`` (a newer request arrived),
        ``"display-off"`` (still off at the deadline), or ``"stopped"``.
        """
        deadline = self._clock() + MAX_WAIT_S
        with self._cond:
            while True:
                if self._stopped:
                    return "stopped"
                if ticket.generation != self._generation:
                    return "superseded"
                now = self._clock()
                mode = self._mode_locked(now)
                if mode in ("active", "none"):
                    return "go"
                if mode == "display-off":
                    wake_at = deadline
                else:
                    wake_at = min(deadline, self._last_scan_end + self._idle_interval_s())
                if now >= wake_at:
                    return "display-off" if mode == "display-off" else "go"
                self._cond.wait(wake_at - now)
