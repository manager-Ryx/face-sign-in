"""Cheerful notices, rationed: the first face sign-in, and a weekly summary.

* After the first face sign-in Windows confirms: "Signed in by face in 0.8 s".
  It is shown once, for a sign-in from the last few minutes (never an old one
  rediscovered after a restart).
* At most once a week, and not in the first week: "Face Unlock saved you
  12 PIN entries this week", only when there were a few to celebrate.

Both respect the "Show notifications" setting. What has been shown is kept in
``state/notices.json`` so a restart never repeats it.
"""
from __future__ import annotations

from dataclasses import dataclass
from pathlib import Path

from face_service.storage import read_state, write_json_atomic

FRESH_S = 10 * 60          # a sign-in older than this isn't "just now" any more
WEEK_S = 7 * 86400
WEEKLY_MINIMUM = 3         # fewer than this isn't worth a notification


@dataclass(frozen=True)
class Notice:
    title: str
    message: str


def seconds_text(seconds: float) -> str:
    """0.84 -> "0.8 s", 2.0 -> "2 s", 12.4 -> "12 s"."""
    if seconds < 10:
        text = f"{seconds:.1f}".rstrip("0").rstrip(".")
        return f"{text} s"
    return f"{seconds:.0f} s"


def first_sign_in_notice(seconds: float | None) -> Notice:
    if seconds is not None and 0 < seconds < 120:
        return Notice(f"Signed in by face in {seconds_text(seconds)}",
                      "That was your first face sign-in. Welcome back!")
    return Notice("Signed in by face", "That was your first face sign-in. Welcome back!")


def weekly_notice(count: int) -> Notice:
    return Notice(f"Face Unlock saved you {count} PIN entries this week",
                  "No typing needed: just a look at your camera. Nice!")


class Notices:
    def __init__(self, path: Path):
        self.path = path
        self.state = read_state(path, {"version": 1, "first_seen": None,
                                       "first_sign_in_shown": None, "weekly_at": 0.0},
                                what="notices")

    def _save(self) -> None:
        try:
            write_json_atomic(self.path, self.state)
        except OSError:
            pass  # a notice may then repeat once; nothing else depends on it

    def first_seen(self, now: float) -> float:
        if self.state.get("first_seen") is None:
            self.state["first_seen"] = now
            self._save()
        return float(self.state["first_seen"])

    def on_status(self, status: dict | None, now: float) -> Notice | None:
        """The first-sign-in notice, if this status reports a fresh first one."""
        if not status or self.state.get("first_sign_in_shown"):
            return None
        at = status.get("last_face_sign_in")
        if not isinstance(at, (int, float)) or not 0 <= now - at <= FRESH_S:
            return None
        self.state["first_sign_in_shown"] = at
        self._save()
        return first_sign_in_notice(status.get("last_face_sign_in_s"))

    def weekly_due(self, now: float) -> bool:
        """Time for the weekly summary? (A week since the last, and since first use.)"""
        if now - self.first_seen(now) < WEEK_S:
            return False
        return now - float(self.state.get("weekly_at", 0.0)) >= WEEK_S

    def weekly(self, face_sign_ins: int, now: float) -> Notice | None:
        """Record that this week was summarised; a notice if there's something to say."""
        self.state["weekly_at"] = now
        self._save()
        return weekly_notice(face_sign_ins) if face_sign_ins >= WEEKLY_MINIMUM else None
