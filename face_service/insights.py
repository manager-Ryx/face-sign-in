"""Summaries for the "Learning and adaptation" page.

Everything here is derived from local measurements; it states sample counts
alongside every rate so small numbers are not over-interpreted.
"""
from __future__ import annotations

import statistics
import time
from collections import Counter, defaultdict
from typing import TYPE_CHECKING, Any

from .profile import LIGHTING_CODES

if TYPE_CHECKING:
    from .service import FaceService

MAX_ITEMS = 20


def _median(values: list[float]) -> float | None:
    values = [v for v in values if isinstance(v, (int, float))]
    return round(statistics.median(values), 1) if values else None


def _weekly_trend(events: list[dict], field: str) -> list[dict]:
    weeks: dict[str, list[float]] = defaultdict(list)
    for event in events:
        value = event.get(field)
        if isinstance(value, (int, float)):
            weeks[time.strftime("%Y-W%W", time.localtime(event["ts"]))].append(value)
    return [{"week": week, "median": round(statistics.median(values), 3), "n": len(values)}
            for week, values in sorted(weeks.items())][-8:]


def build_insights(service: FaceService, *, days: float = 30.0) -> dict[str, Any]:
    now = time.time()
    events = service.metrics.events(since=now - days * 86400.0)
    by_kind: dict[str, list[dict]] = defaultdict(list)
    for event in events:
        by_kind[event["kind"]].append(event)

    sessions = [e for e in by_kind["lock_session"] if e.get("face_seen")][-30:]
    unlocked = [e for e in sessions if e.get("face_accepted")]
    reliability = {
        "sessions": len(sessions), "face_unlocked": len(unlocked),
        "rate": round(len(unlocked) / len(sessions), 3) if sessions else None,
        "median_time_s": _median([e.get("time_to_unlock_s") for e in unlocked]),
        "recent": [{"at": e["ts"], "face": bool(e.get("face_accepted")),
                    "reason": e.get("top_reason", "")} for e in sessions[-10:]][::-1],
    }

    attempts = by_kind["unlock_attempt"]
    matched = [e for e in attempts if e.get("reason") == "match"]
    attempt_summary = {
        "total": len(attempts),
        "by_reason": dict(Counter(e.get("reason", "?") for e in attempts).most_common()),
        "median_verify_ms": _median([e.get("verify_ms") for e in matched]),
        "median_frames": _median([e.get("frames") for e in matched]),
        "distance_trend": _weekly_trend(matched, "best"),
    }

    starts = [e for e in by_kind["camera_start"] if not e.get("reused")]
    camera_rows = defaultdict(list)
    for event in starts:
        camera_rows[(event.get("predicted", "?"), int(event.get("fps") or 0))].append(event)
    camera = {
        "starts": len(starts),
        "by_condition": [
            {"lighting": light, "fps": fps or "default", "n": len(rows),
             "open_ms": _median([r.get("open_ms") for r in rows]),
             "settle_ms": _median([r.get("settle_ms") for r in rows]),
             "settle_frames": _median([r.get("settle_frames") for r in rows])}
            for (light, fps), rows in sorted(camera_rows.items())],
        "low_light": service.camera_profile.summary().get("low_light"),
        "hold_s": round(service.camera.hold_seconds(), 1),
        "errors": dict(Counter(e.get("error", "?") for e in by_kind["camera_error"])),
    }

    presence = by_kind["presence_check"]
    presence_summary = {
        "total": len(presence),
        "by_reason": dict(Counter(e.get("reason", "?") for e in presence).most_common()),
        "distance_trend": _weekly_trend([e for e in presence if e.get("reason") == "match"],
                                        "best"),
    }

    recognizer = service.recognizer
    anchors, learned = recognizer.profile_counts()
    timeline = recognizer.learned_samples(MAX_ITEMS)
    for item in timeline:
        item["lighting"] = LIGHTING_CODES[item["lighting"]] if 0 <= item["lighting"] < len(
            LIGHTING_CODES) else "unknown"
    learning = {
        "enabled": service.cfg.adaptive_enrollment, "anchors": anchors, "learned": learned,
        "cap": service.cfg.adaptive_max_embeddings, "timeline": timeline,
        "skipped": dict(Counter(e.get("reason", "?") for e in by_kind["learning_skipped"])
                        .most_common(5)),
        "results": dict(Counter(e.get("reason", "?") for e in by_kind["learning_result"])
                        .most_common(5)),
        "keep_photos": service.cfg.adaptive_keep_photos,
    }

    caught = Counter(e.get("caught") for e in by_kind["lock_session"] if e.get("caught"))
    scanning = sum(e.get("scanning_s") or 0.0 for e in by_kind["lock_session"])
    quick = sum(e.get("quick_look_s") or 0.0 for e in by_kind["lock_session"])
    routine = {
        **service.routine.summary(), "enabled": service.cfg.unlock_learn_routine,
        "caught": {key: caught.get(key, 0) for key in ("instant", "quick-look", "other")},
        "scanning_share": round(scanning / (scanning + quick), 3) if scanning + quick else None,
        "fixed_window_s": service.cfg.unlock_idle_after_s,
        "interval_s": service.cfg.unlock_idle_interval_s,
    }

    report = service._threshold_report()
    advice = []
    trend = attempt_summary["distance_trend"]
    if len(trend) >= 3 and trend[-1]["median"] - trend[0]["median"] > 0.05:
        advice.append("Matches have been getting weaker over recent weeks. Adding your current "
                      "appearance usually helps.")
    if reliability["rate"] is not None and reliability["sessions"] >= 5 and reliability["rate"] < 0.7:
        advice.append("Face sign-in often needed another method recently. Run a recognition "
                      "test to see what is getting in the way.")
    return {
        "days": days, "reliability": reliability, "attempts": attempt_summary,
        "camera": camera, "presence": presence_summary, "learning": learning,
        "routine": routine,
        "threshold": report.to_dict() if report else None,
        "auto_tighten": service.cfg.auto_tighten_threshold,
        "ledger": service.adaptation.ledger(MAX_ITEMS),
        "snapshots": service.adaptation.snapshots(),
        "evaluation": {"samples": len(service.evaluation)},
        "advice": advice,
    }
