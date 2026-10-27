"""Turns service status, monitor state and settings into plain-English summaries."""
from __future__ import annotations

import time
from dataclasses import dataclass, field
from typing import Any

from face_service.config import Config

GUIDANCE = {
    "no_face": "Look at the camera.",
    "too_dark": "Your face is too dark. Turn on a light or face the screen.",
    "backlit": "There's bright light behind you. Face towards a light instead.",
    "too_bright": "There's too much light on your face.",
    "blurry": "Hold still for a moment.",
    "too_far": "Move a little closer.",
    "too_close": "Move back a little.",
    "turned": "Face the camera directly.",
    "tilted": "Keep your head level.",
    "partial": "Move into the middle of the camera's view.",
    "multiple_faces": "More than one face is visible.",
    "low_contrast": "The image is flat. More light would help.",
    "mismatch": "This face doesn't match your profile.",
    "uncertain": "Almost there. Keep looking at the camera.",
    "unusable": "The image wasn't clear enough to judge.",
    "spoof": "The liveness check didn't pass.",
    "camera-busy": "The camera is busy right now.",
    "camera-leased": "The camera is being used to set up face recognition.",
    "not-enrolled": "Set up face recognition first.",
    "display-off": "The screen was off, so the camera stayed off.",
    "superseded": "A newer scan replaced this one.",
}

REASONS = {
    "match": "Recognised you", "no_face": "No one in view", "mismatch": "Not you",
    "uncertain": "Not certain", "unusable": "Image unclear", "spoof": "Liveness failed",
    "superseded": "Restarted", "display-off": "Screen off, camera idle",
    "camera-leased": "Camera lent to setup", "camera-busy": "Camera busy",
    "not-enrolled": "Not set up", "no-credentials": "No stored password",
    "caller-not-allowed": "Blocked caller", "stopped": "Service stopped", "error": "Error",
    "timeout": "Took too long", "preempted": "Interrupted", "starting": "Still starting",
    "just-locked": "Locked by you – waits until you step away",
    "password-rejected": "Saved password out of date",
    "anti-spoofing-unavailable": "Photo protection unavailable",
    "model-unavailable": "Face model unavailable",
}


def guidance_text(issue: str | None) -> str:
    return GUIDANCE.get(issue or "", "")


def reason_text(reason: str | None) -> str:
    if not reason:
        return ""
    if reason.startswith("camera-") and reason not in REASONS:
        return "Camera problem"
    return REASONS.get(reason, reason.replace("-", " ").replace("_", " ").capitalize())


def duration(seconds: float) -> str:
    """Plain-English length of time: "45 seconds", "1 minute", "1.5 hours"."""
    seconds = max(0.0, float(seconds))
    if seconds < 60:
        return f"{seconds:.0f} second{'s' if round(seconds) != 1 else ''}"
    minutes = round(seconds / 60, 1)
    if minutes < 60:
        return f"{minutes:g} minute{'s' if minutes != 1 else ''}"
    hours = round(minutes / 60, 1)
    return f"{hours:g} hour{'s' if hours != 1 else ''}"


def sentence(text: str) -> str:
    """Capitalise the first letter only ("yesterday at 9:00" -> "Yesterday at 9:00")."""
    return text[:1].upper() + text[1:]


def ago(ts: float | None) -> str:
    if not ts:
        return "never"
    delta = time.time() - ts
    if delta < 45:
        return "just now"
    if delta < 3600:
        return f"{delta / 60:.0f} min ago"
    if delta < 6 * 3600:
        return f"{delta / 3600:.0f} h ago"
    return when(ts)


def when(ts: float | None) -> str:
    if not ts:
        return "never"
    local = time.localtime(ts)
    today = time.localtime()
    clock = time.strftime("%H:%M", local)
    if local.tm_yday == today.tm_yday and local.tm_year == today.tm_year:
        return f"today at {clock}"
    if (time.mktime(today) - ts) < 2 * 86400 and today.tm_yday - local.tm_yday == 1:
        return f"yesterday at {clock}"
    return time.strftime("%d %b %Y, %H:%M", local).lstrip("0")


@dataclass
class Tile:
    value: str
    detail: str
    pill: str
    tone: str


@dataclass
class Overview:
    tone: str
    title: str
    message: str
    action: str | None
    action_text: str | None
    tray_state: str
    tray_text: str
    alert_key: str | None
    camera: Tile = field(default_factory=lambda: Tile("", "", "", "neutral"))
    recognition: Tile = field(default_factory=lambda: Tile("", "", "", "neutral"))
    autolock: Tile = field(default_factory=lambda: Tile("", "", "", "neutral"))
    learning: Tile = field(default_factory=lambda: Tile("", "", "", "neutral"))


def _autolock_tile(snapshot: dict, cfg: Config) -> Tile:
    timeout = duration(cfg.effective_presence_absence_timeout_s)
    if not cfg.presence_enabled:
        return Tile("Off", "Your PC won't lock itself when you leave.", "Off", "neutral")
    if snapshot.get("paused"):
        until = snapshot.get("paused_until")
        text = f"Paused until {time.strftime('%H:%M', time.localtime(until))}" if until \
            else "Paused until you resume"
        return Tile(text, f"Normally locks after about {timeout} away.", "Paused", "caution")
    last = snapshot.get("last_result")
    when_text = ago(snapshot.get("last_at"))
    detail = {
        "present": f"Saw you {when_text}.",
        "absent": f"Haven't seen you for about {duration(snapshot.get('absence_elapsed_s', 0))}.",
        "skipped": f"Last check skipped {when_text} ({reason_text(snapshot.get('last_reason'))}).",
        "error": f"Last check {when_text} couldn't run ({reason_text(snapshot.get('last_reason'))}).",
    }.get(last, f"Checks every {duration(cfg.presence_interval_s)}.")
    return Tile(f"Locks after about {timeout}", detail, "On", "success")


def summarize(status: dict | None, snapshot: dict, cfg: Config,
              system: dict | None = None) -> Overview:
    """``system``: local checks ({"tile": TileState, "hello": bool}) when known."""
    if status is None:
        return Overview(
            "critical", "Face Unlock isn't running",
            "Face sign-in and auto-lock are unavailable until the background service starts.",
            "start-service", "Start it", "off", "Not running", "service-down",
            Tile("Unknown", "The service isn't responding.", "Unavailable", "neutral"),
            Tile("Unavailable", "", "Unavailable", "neutral"), _autolock_tile(snapshot, cfg),
            Tile("Unknown", "", "", "neutral"))

    device = (status.get("camera") or {}).get("device") or {}
    activity = (status.get("camera") or {}).get("activity") or {}
    profile = status.get("profile") or {}
    name = device.get("name") or "Your camera"
    state = device.get("state")
    owner = activity.get("owner")
    in_use = {"unlock": "In use by the sign-in screen", "presence": "In use: checking you're here",
              "check": "In use: recognition test"}.get(owner or "", "")
    if activity.get("state") == "leased":
        in_use = "Lent to face setup"
    camera_detail = in_use or "Not in use. The camera light stays off between checks."
    camera = {
        "ready": Tile(name, camera_detail, "In use" if in_use else "Connected",
                      "accent" if in_use else "success"),
        "missing": Tile(name, "Not connected. Face Unlock won't switch to another camera.",
                        "Disconnected", "critical"),
        "moved": Tile(device.get("name") or name, "Connected to a different USB port.",
                      "Needs confirmation", "caution"),
        "ambiguous": Tile("Several matching cameras", "Choose one in Settings.", "Choose",
                          "caution"),
        "unpinned": Tile("No camera chosen", "Choose your webcam in Settings.", "Choose", "caution"),
    }.get(state, Tile(name, device.get("message", "Camera problem"), "Problem", "critical"))
    if activity.get("last_error") and state == "ready":
        camera.detail = "Last start-up failed: " + str(activity["last_error"].get("message", ""))

    anchors, learned = profile.get("anchors", 0), profile.get("learned", 0)
    recognition = (Tile("Ready", f"{anchors} reference photos, {learned} learned from sign-ins",
                        "Ready", "success") if anchors
                   else Tile("Not set up", "Face Unlock doesn't know your face yet.",
                             "Needs setup", "caution"))
    learn = status.get("learning") or {}
    learning = (Tile("On", ("Last learned " + when(learn.get("last"))) if learn.get("last")
                     else "Nothing learned yet.", "On", "success") if learn.get("enabled")
                else Tile("Paused", "Successful sign-ins aren't being learned from.", "Paused",
                          "neutral"))
    autolock = _autolock_tile(snapshot, cfg)
    paused = bool(snapshot.get("paused"))
    tray_ok = "paused" if paused else "ready"

    def out(tone: str, title: str, message: str, action: str | None, action_text: str | None,
            tray_state: str, tray_text: str, alert: str | None) -> Overview:
        return Overview(tone, title, message, action, action_text, tray_state, tray_text, alert,
                        camera, recognition, autolock, learning)

    if not anchors:
        return out("caution", "Set up face recognition",
                   "Face Unlock doesn't know your face yet. Setting up takes about a minute.",
                   "setup", "Set up", "attention", "Needs setup", "not-enrolled")
    if state == "moved":
        return out("caution", "Your camera is on a different USB port",
                   f"{device.get('name') or 'Your camera'} is now connected through a different "
                   "USB port. Confirm it's the same camera to keep using it.",
                   "camera-moved", "Use this camera", "attention", "Camera needs confirmation",
                   "camera-moved")
    if state in ("missing", "ambiguous", "unpinned", "error"):
        return out("critical" if state in ("missing", "error") else "caution",
                   {"missing": "Your camera isn't connected",
                    "ambiguous": "More than one matching camera",
                    "unpinned": "Choose a camera"}.get(state, "Camera problem"),
                   camera.detail, "camera-settings", "Camera settings", "attention",
                   camera.pill, f"camera-{state}")
    if not status.get("credentials"):
        return out("caution", "Store your Windows password",
                   "Face sign-in needs your Windows password to sign you in. It's stored "
                   "encrypted so only your Windows account can read it.",
                   "password", "Store password", "attention", "Needs your password",
                   "no-credentials")
    if "password-rejected" in (status.get("not_ready") or []):
        return out("caution", "Windows didn't accept your saved password",
                   "It has probably changed. Store your current Windows password so face "
                   "sign-in can work again – until then it won't try, so Windows doesn't count "
                   "failed sign-ins.", "password", "Update password", "attention",
                   "Needs your new password", "password-rejected")
    if "model-unavailable" in (status.get("not_ready") or []):
        error = (status.get("recognition") or {}).get("model_error") or "unknown error"
        return out("caution", "The face model couldn't be prepared",
                   f"It downloads once (about 130 MB) and needs the internet ({error}). "
                   "Face sign-in waits until it's ready.", "prepare-model", "Try again",
                   "attention", "Face model unavailable", "model-unavailable")
    if "anti-spoofing-unavailable" in (status.get("not_ready") or []):
        return out("caution", "Photo protection couldn't start",
                   "Its small model downloads once (this needs the internet), or PyTorch is "
                   "missing. Face sign-in waits until it works. You can turn photo protection "
                   "off in Settings.", "settings", "Open Settings", "attention",
                   "Photo protection unavailable", "anti-spoofing-unavailable")
    system = system or {}
    if system.get("hello"):
        return out("caution", "Windows only allows Windows Hello sign-in",
                   "With this on, Windows hides Face Unlock on the lock screen. Turn off "
                   "\u201cFor improved security, only allow Windows Hello sign-in\u201d in "
                   "Sign-in options.", "hello-settings", "Open Sign-in options", "attention",
                   "Hidden by Windows", "hello-only")
    tile = system.get("tile")
    if tile is not None and not tile.installed:
        return out("caution", "Face sign-in isn't on your lock screen yet",
                   "Add the Face Unlock sign-in tile. Windows will ask for your permission "
                   "once. Auto-lock works without it.", "install-tile", "Add to lock screen",
                   "attention", "Not on the lock screen", "tile-missing")
    problems = status.get("config_problems") or []
    if problems:
        return out("caution", "Some settings were reset to safe values", problems[0],
                   "settings", "Review settings", tray_ok, "Ready (settings reset)", None)
    message = "Look at your camera on the lock screen to sign in."
    if not status.get("recognition", {}).get("anti_spoofing"):
        message += " Photo-spoof protection is off."
    return out("success", "Face sign-in is ready", message, None, None, tray_ok,
               "Paused auto-lock" if paused else "Ready", None)


def fmt_ms(value: Any) -> str:
    if not isinstance(value, (int, float)):
        return "—"
    return f"{value / 1000:.1f} s" if value >= 1000 else f"{value:.0f} ms"
