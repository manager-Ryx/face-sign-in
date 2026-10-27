""""Copy diagnostics": one plain-text report for getting help, without anything private.

It holds what a helper needs: the app and Windows versions, the same checks as
``tools/check_install.ps1``, the service's ``status`` reply, settings, a
summary of measurements and the end of both logs. It never holds the stored
password, face data or photos (none of those files are opened), and the
account name, email address, PC name and camera device path are masked.
"""
from __future__ import annotations

import json
import os
import platform
import re
import statistics
import sys
import time
import zipfile
from collections import Counter
from dataclasses import asdict, dataclass, field
from pathlib import Path

from face_service._version import __version__
from face_service.config import LOG_PATH, METRICS_DIR, PRESENCE_LOG_PATH, Config
from face_service.metrics import MetricsStore

LOG_LINES = 300
METRIC_DAYS = 7

# What the report says it contains, and what it never contains (shown before copying).
INCLUDED = (
    "App and Windows versions",
    "The same checks as the installer's check script",
    "Face Unlock's status (no password, no face data)",
    "Your settings (the camera's device path masked)",
    "A summary of the last week's measurements",
    f"The last {LOG_LINES} lines of both logs",
)
NEVER = (
    "Your Windows password",
    "Your face data, reference photos or learned photos",
    "Your account name, email address or PC name (they're masked)",
)

DEVICE_PATH = re.compile(r"\\\\\?\\[^\s\"',;]+", re.IGNORECASE)
EMAIL = re.compile(r"[A-Za-z0-9._%+-]+@[A-Za-z0-9.-]+\.[A-Za-z]{2,}")
USER_FOLDER = re.compile(r"([A-Za-z]:\\Users\\)[^\\/\s\"']+", re.IGNORECASE)


@dataclass
class Identities:
    """Personal strings to mask wherever they appear."""
    user: str = ""
    domain: str = ""
    computer: str = ""
    email: str = ""
    camera_paths: list[str] = field(default_factory=list)

    @classmethod
    def from_environment(cls, *, email: str | None = None,
                         camera_paths: list[str] | None = None) -> Identities:
        return cls(os.environ.get("USERNAME", ""), os.environ.get("USERDOMAIN", ""),
                   os.environ.get("COMPUTERNAME", ""), email or "",
                   [p for p in camera_paths or [] if p])


def redact(text: str, who: Identities) -> str:
    """Mask camera device paths, email addresses, user folders and names."""
    for path in sorted(who.camera_paths, key=len, reverse=True):
        text = re.sub(re.escape(path), "<camera device>", text, flags=re.IGNORECASE)
    text = DEVICE_PATH.sub("<device path>", text)
    if who.email:
        text = re.sub(re.escape(who.email), "<email>", text, flags=re.IGNORECASE)
    text = EMAIL.sub("<email>", text)
    text = USER_FOLDER.sub(r"\1<user>", text)
    names = [(who.user, "<user>"), (who.domain, "<domain>"), (who.computer, "<pc>")]
    for name, mask in sorted(names, key=lambda item: len(item[0]), reverse=True):
        # Whole words only, and never very short names that would mask ordinary words.
        if len(name) >= 3:
            text = re.sub(rf"(?<![\w-]){re.escape(name)}(?![\w-])", mask, text,
                          flags=re.IGNORECASE)
    return text


def tail(path: Path, lines: int = LOG_LINES) -> str:
    """The last ``lines`` lines of a text file ("" if it doesn't exist)."""
    try:
        with path.open("rb") as handle:
            handle.seek(0, os.SEEK_END)
            size = handle.tell()
            handle.seek(max(0, size - 400 * lines))
            data = handle.read().decode("utf-8", errors="replace")
    except OSError:
        return ""
    return "\n".join(data.splitlines()[-lines:])


def metrics_summary(events: list[dict]) -> list[str]:
    """Counts per kind of measurement, and how face sign-ins went."""
    if not events:
        return ["No measurements in this period."]
    kinds = Counter(e.get("kind", "?") for e in events)
    lines = [f"{kind}: {count}" for kind, count in sorted(kinds.items())]
    sessions = [e for e in events if e.get("kind") == "lock_session"]
    if sessions:
        face = [e for e in sessions if e.get("face_accepted")]
        times = [e["sign_in_s"] for e in face if isinstance(e.get("sign_in_s"), (int, float))]
        lines.append(f"Locks: {len(sessions)}, signed in by face: {len(face)}"
                     + (f", median time {statistics.median(times):.1f} s" if times else ""))
        reasons = Counter(e.get("top_reason") for e in sessions if e.get("top_reason"))
        if reasons:
            lines.append("Most common lock-screen outcome: "
                         + ", ".join(f"{r} ({n})" for r, n in reasons.most_common(4)))
    return lines


def windows_version() -> str:
    try:
        build = sys.getwindowsversion()
        return f"Windows {platform.release()} (build {build.build}), {platform.machine()}"
    except AttributeError:
        return platform.platform()


def settings_lines(cfg: Config) -> list[str]:
    values = asdict(cfg)
    if values.get("camera_device_id"):
        values["camera_device_id"] = "<camera device>"
    return [f"{key} = {value!r}" for key, value in sorted(values.items())]


def check_lines(status: dict | None, system: dict) -> list[str]:
    """The same checks as tools/check_install.ps1, from what the app already knows."""
    def line(ok: bool, name: str, detail: str) -> str:
        return f"[{'OK  ' if ok else 'TODO'}] {name}: {detail}"

    tile = system.get("tile")
    autostart = system.get("autostart", "unknown")
    lines = [
        line(autostart != "off", "Starts at sign-in", str(autostart)),
        line(status is not None, "Background service",
             "running" if status is not None else "not answering"),
        line(True, "Tray app", "running (it made this report)"),
        line(bool(tile and tile.installed), "Lock-screen tile",
             "installed" if tile and tile.installed else
             (tile.problem if tile and tile.problem else "not installed")),
        line(not system.get("hello"), "Windows Hello-only sign-in",
             "on: it hides the Face Unlock tile" if system.get("hello") else "off"),
    ]
    if status is not None:
        profile = status.get("profile") or {}
        device = (status.get("camera") or {}).get("device") or {}
        recognition = status.get("recognition") or {}
        lines += [
            line(bool(profile.get("anchors")), "Face profile",
                 f"{profile.get('anchors', 0)} reference photos, "
                 f"{profile.get('learned', 0)} learned"),
            line(bool(status.get("credentials")), "Stored password",
                 "stored (encrypted)" if status.get("credentials") else "not stored"),
            line(device.get("state") == "ready", "Camera",
                 f"{device.get('name', '?')} ({device.get('state', '?')})"),
            line(bool(recognition.get("model_present")), "Recognition model",
                 "downloaded" if recognition.get("model_present") else "not downloaded yet"),
        ]
        for reason in status.get("not_ready") or []:
            lines.append(f"Service reports: {reason}")
    return lines


def build_report(*, status: dict | None, system: dict, cfg: Config, events: list[dict],
                 service_log: str, presence_log: str, who: Identities,
                 now: float | None = None) -> str:
    now = time.time() if now is None else now
    status_text = json.dumps(status, indent=2, sort_keys=True, default=str) if status \
        else "No answer from the background service."
    parts = [
        "Face Unlock diagnostics",
        f"Created {time.strftime('%Y-%m-%d %H:%M', time.localtime(now))}",
        f"Face Unlock {__version__}{' (installed)' if getattr(sys, 'frozen', False) else ''}"
        f" · {windows_version()} · Python {platform.python_version()}",
        "Never included: the stored password, face data or photos.",
        "", "== Checks ==", *check_lines(status, system),
        "", "== Service status ==", status_text,
        "", "== Settings ==", *settings_lines(cfg),
        "", f"== Measurements, last {METRIC_DAYS} days ==", *metrics_summary(events),
        "", f"== service.log (last {LOG_LINES} lines) ==", service_log or "(empty)",
        "", f"== presence.log (last {LOG_LINES} lines) ==", presence_log or "(empty)",
    ]
    return redact("\n".join(parts) + "\n", who)


def collect(*, status: dict | None, system: dict, cfg: Config, who: Identities,
            now: float | None = None) -> str:
    """Read the logs and measurements (never the password or face files) and build it."""
    now = time.time() if now is None else now
    since = now - METRIC_DAYS * 86400
    events: list[dict] = []
    for name in ("service.jsonl", "presence.jsonl"):
        path = METRICS_DIR / name
        if path.exists():
            events += MetricsStore(path).events(since=since, from_disk=True)
    return build_report(status=status, system=system, cfg=cfg, events=events,
                        service_log=tail(LOG_PATH), presence_log=tail(PRESENCE_LOG_PATH),
                        who=who, now=now)


def downloads_folder() -> Path:
    """The Downloads folder (Windows knows if it was moved), or the home folder."""
    try:
        import ctypes
        from ctypes import wintypes
        from uuid import UUID

        class Guid(ctypes.Structure):
            _fields_ = [("data", ctypes.c_byte * 16)]

        guid = Guid()
        guid.data[:] = UUID("374DE290-123F-4565-9164-39C4925E467B").bytes_le
        out = wintypes.LPWSTR()
        if ctypes.windll.shell32.SHGetKnownFolderPath(ctypes.byref(guid), 0, None,
                                                      ctypes.byref(out)) == 0:
            path = Path(out.value)
            ctypes.windll.ole32.CoTaskMemFree(out)
            if path.is_dir():
                return path
    except (AttributeError, OSError, ValueError):
        pass
    downloads = Path.home() / "Downloads"
    return downloads if downloads.is_dir() else Path.home()


def save_zip(report: str, folder: Path, now: float | None = None) -> Path:
    """Write the report as a zip in ``folder``; returns its path."""
    stamp = time.strftime("%Y%m%d-%H%M%S", time.localtime(time.time() if now is None else now))
    path = folder / f"face-unlock-diagnostics-{stamp}.zip"
    with zipfile.ZipFile(path, "w", compression=zipfile.ZIP_DEFLATED) as archive:
        archive.writestr("face-unlock-diagnostics.txt", report)
    return path

