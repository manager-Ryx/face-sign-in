"""Detect remote-access sessions so the presence monitor never locks during one.

Two tiers:

1. ``ALWAYS_REMOTE_PROCS`` – processes that exist only while a remote session
   is connected (for example ``TeamViewer_Desktop.exe`` or Quick Assist).
2. ``CONNECTION_CHECKED_PROCS`` – helpers that run all the time and only mean
   "remote session active" while they hold an established TCP connection to
   another machine (AnyDesk, RustDesk, Parsec, ...).

Connections are read from the system-wide table once per check, which also
works for helpers running under another account (their per-process
connection lists would be access-denied). An RDP session is detected with
``GetSystemMetrics(SM_REMOTESESSION)``.
"""
from __future__ import annotations

import ipaddress
import logging

try:
    import psutil
except ImportError:  # pragma: no cover - psutil is a declared dependency
    psutil = None  # type: ignore[assignment]

import win32api

log = logging.getLogger(__name__)

ALWAYS_REMOTE_PROCS = {
    "teamviewer_desktop.exe",   # TeamViewer spawns this on connect
    "remoting_desktop.exe",     # Chrome Remote Desktop (per connection)
    "quickassist.exe",          # Microsoft Quick Assist
    "msra.exe",                 # Windows Remote Assistance
}

CONNECTION_CHECKED_PROCS = {
    "ultraviewer_desktop.exe", "ultraviewer_service.exe", "anydesk.exe", "rustdesk.exe",
    "remoting_host.exe", "parsecd.exe", "sunshine.exe", "srserver.exe", "teamviewer.exe",
}

SM_REMOTESESSION = 0x1000


def _is_external(address: str) -> bool:
    try:
        ip = ipaddress.ip_address(address)
    except ValueError:
        return False
    return not (ip.is_loopback or ip.is_unspecified or ip.is_link_local)


def is_rdp_session() -> bool:
    try:
        return bool(win32api.GetSystemMetrics(SM_REMOTESESSION))
    except Exception as exc:
        log.debug("GetSystemMetrics failed: %s", exc)
        return False


def _connected_pids() -> set[int]:
    """Process ids holding an established TCP connection to another machine."""
    try:
        return {c.pid for c in psutil.net_connections(kind="tcp")
                if c.pid and c.status == psutil.CONN_ESTABLISHED and c.raddr
                and _is_external(c.raddr.ip)}
    except (psutil.AccessDenied, OSError) as exc:
        log.debug("connection table unavailable: %s", exc)
        return set()


def active_remote_tools() -> list[str]:
    if psutil is None:
        return []
    found: list[str] = []
    connected: set[int] | None = None
    for process in psutil.process_iter(attrs=["name", "pid"]):
        name = (process.info.get("name") or "").lower()
        if name in ALWAYS_REMOTE_PROCS:
            found.append(name)
        elif name in CONNECTION_CHECKED_PROCS:
            if connected is None:
                connected = _connected_pids()
            if process.info.get("pid") in connected:
                found.append(f"{name}(connected)")
    return found


def is_remote_context() -> tuple[bool, str]:
    if is_rdp_session():
        return True, "rdp"
    tools = active_remote_tools()
    if tools:
        return True, "remote-tool:" + ",".join(sorted(set(tools)))
    return False, ""
