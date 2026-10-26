"""Automatic adjustments: what changed, why, the evidence, and how to undo it.

User preferences live in ``config.toml``. Automatic tuning lives here,
separately, so the two are never confused:

* the tuned threshold, which may only be *stricter* than your setting;
* a ledger of every adjustment (automatic or yours) with its reason,
  evidence, and later outcome;
* probation after an automatic change: if face sign-in success over the
  following lock-screen sessions drops clearly below the rate before the
  change, the change is rolled back automatically;
* snapshots of known-working settings that can be restored in one step.
"""
from __future__ import annotations

import json
import logging
import threading
import time
from collections.abc import Callable
from dataclasses import asdict
from pathlib import Path
from typing import Any

from .config import Config
from .storage import read_state, write_json_atomic

log = logging.getLogger(__name__)

LEDGER_LIMIT = 100
SNAPSHOT_LIMIT = 10
PROBATION_SESSIONS = 12
ROLLBACK_DROP = 0.15    # rate drop (absolute) that triggers a rollback


class AdaptationState:
    def __init__(self, path: Path, *, clock: Callable[[], float] = time.time):
        self.path = path
        self._clock = clock
        self._lock = threading.RLock()
        self._data = self._load()

    # ---- persistence -------------------------------------------------------

    @staticmethod
    def _empty() -> dict[str, Any]:
        return {"version": 1, "threshold": {"tuned": None, "applied_at": None,
                                            "previous": None, "probation": None},
                "ledger": [], "snapshots": [], "next_id": 1}

    def _load(self) -> dict[str, Any]:
        return read_state(self.path, self._empty(), what="adaptation state")

    def _save(self) -> None:
        write_json_atomic(self.path, self._data)

    def snapshot_dict(self) -> dict[str, Any]:
        with self._lock:
            return json.loads(json.dumps(self._data))

    # ---- threshold ---------------------------------------------------------

    def effective_threshold(self, cfg: Config) -> float:
        with self._lock:
            tuned = self._data["threshold"].get("tuned")
        if tuned is None:
            return cfg.threshold
        return min(cfg.threshold, float(tuned))

    def apply_threshold(self, new: float, *, cfg: Config, reason: str, evidence: dict,
                        by: str, session_rate: float | None) -> dict:
        """Make recognition stricter. Refuses anything looser than the user's setting."""
        with self._lock:
            current = self.effective_threshold(cfg)
            if new >= current:
                raise ValueError("automatic tuning may only make recognition stricter")
            entry = self._add_entry("threshold", round(current, 4), round(new, 4),
                                    reason, evidence, by)
            self._data["threshold"] = {
                "tuned": round(new, 4), "applied_at": self._clock(), "previous": current,
                "probation": {"entry": entry["id"], "baseline_rate": session_rate,
                              "successes": 0, "sessions": 0},
            }
            self._save()
            return entry

    def clear_tuned_threshold(self, *, reason: str, by: str) -> dict | None:
        with self._lock:
            tuned = self._data["threshold"].get("tuned")
            if tuned is None:
                return None
            entry = self._add_entry("threshold", tuned, None, reason, {}, by)
            self._mark_active_entries("threshold", "reverted")
            entry["status"] = "active"
            self._data["threshold"] = self._empty()["threshold"]
            self._save()
            return entry

    def record_session(self, *, face_success: bool) -> dict | None:
        """Feed one lock-screen session outcome; may roll back a tuned threshold."""
        with self._lock:
            probation = self._data["threshold"].get("probation")
            if not probation:
                return None
            probation["sessions"] += 1
            probation["successes"] += int(face_success)
            baseline = probation.get("baseline_rate")
            result = None
            if probation["sessions"] >= PROBATION_SESSIONS:
                rate = probation["successes"] / probation["sessions"]
                entry = self._find(probation["entry"])
                if baseline is not None and rate < baseline - ROLLBACK_DROP:
                    previous = self._data["threshold"].get("previous")
                    if entry:
                        entry["status"] = "rolled-back"
                        entry["result"] = (f"Face sign-in success fell from {baseline:.0%} to "
                                           f"{rate:.0%}; restored {previous}")
                    self._data["threshold"] = self._empty()["threshold"]
                    self._add_entry("threshold", entry["new"] if entry else None, previous,
                                    "Automatic rollback: reliability got worse after the change",
                                    {"rate_before": baseline, "rate_after": rate}, "automatic")
                    result = {"rolled_back": True, "rate": rate}
                else:
                    if entry:
                        entry["result"] = (f"Kept: face sign-in success {rate:.0%} over "
                                           f"{probation['sessions']} sessions")
                    self._data["threshold"]["probation"] = None
                    result = {"rolled_back": False, "rate": rate}
            self._save()
            return result

    # ---- ledger ------------------------------------------------------------

    def _add_entry(self, setting: str, old: Any, new: Any, reason: str, evidence: dict,
                   by: str) -> dict:
        entry = {"id": self._data["next_id"], "ts": self._clock(), "setting": setting,
                 "old": old, "new": new, "reason": reason, "evidence": evidence, "by": by,
                 "status": "active", "result": None}
        self._data["next_id"] += 1
        self._data["ledger"].append(entry)
        del self._data["ledger"][:-LEDGER_LIMIT]
        return entry

    def _find(self, entry_id: int) -> dict | None:
        return next((e for e in self._data["ledger"] if e["id"] == entry_id), None)

    def _mark_active_entries(self, setting: str, status: str) -> None:
        for entry in self._data["ledger"]:
            if entry["setting"] == setting and entry["status"] == "active":
                entry["status"] = status

    def note(self, setting: str, old: Any, new: Any, reason: str, *, by: str,
             evidence: dict | None = None) -> dict:
        """Record a change made elsewhere (for example a camera or learning change)."""
        with self._lock:
            entry = self._add_entry(setting, old, new, reason, evidence or {}, by)
            self._save()
            return entry

    def ledger(self, limit: int = 30) -> list[dict]:
        with self._lock:
            return json.loads(json.dumps(self._data["ledger"][-limit:]))[::-1]

    # ---- snapshots ---------------------------------------------------------

    def take_snapshot(self, cfg: Config, label: str) -> dict:
        with self._lock:
            snap = {"ts": self._clock(), "label": label, "config": asdict(cfg),
                    "tuned_threshold": self._data["threshold"].get("tuned")}
            snapshots = self._data["snapshots"]
            if snapshots and snapshots[-1]["config"] == snap["config"] and \
                    snapshots[-1]["tuned_threshold"] == snap["tuned_threshold"]:
                snapshots[-1]["ts"] = snap["ts"]
                snapshots[-1]["label"] = label
            else:
                snapshots.append(snap)
                del snapshots[:-SNAPSHOT_LIMIT]
            self._save()
            return snap

    def snapshots(self) -> list[dict]:
        with self._lock:
            return [{"index": i, "ts": s["ts"], "label": s["label"]}
                    for i, s in enumerate(self._data["snapshots"])][::-1]

    def restore_snapshot(self, index: int) -> Config:
        """Return the snapshot's settings (the caller saves them) and its tuning."""
        with self._lock:
            snap = self._data["snapshots"][index]
            values = {k: v for k, v in snap["config"].items()
                      if k in Config.__dataclass_fields__}
            restored = Config(**values)
            restored.validate()
            self._data["threshold"] = self._empty()["threshold"]
            if snap.get("tuned_threshold") is not None:
                self._data["threshold"]["tuned"] = snap["tuned_threshold"]
            self._add_entry("settings", None, snap["label"],
                            "Restored settings from " + time.strftime(
                                "%d %b %Y %H:%M", time.localtime(snap["ts"])), {}, "you")
            self._save()
            return restored
