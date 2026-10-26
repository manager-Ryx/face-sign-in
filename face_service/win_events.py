"""Windows session, display and power events for the current user session.

A hidden window on a background thread receives:

* session changes (lock, unlock, logon, logoff, remote connect/disconnect);
* console display state (on, off, dimmed) – the lock screen turns the
  display off after a minute by default;
* sleep and resume.

The current lock state is also queried directly (``WTSSessionInfoEx``) so
callers get an answer even before the first event arrives.
"""
from __future__ import annotations

import ctypes
import logging
import threading
import time
import uuid
from collections.abc import Callable
from ctypes import wintypes

log = logging.getLogger(__name__)

WM_DESTROY = 0x0002
WM_CLOSE = 0x0010
WM_POWERBROADCAST = 0x0218
WM_WTSSESSION_CHANGE = 0x02B1
PBT_APMSUSPEND = 0x0004
PBT_APMRESUMESUSPEND = 0x0007
PBT_APMRESUMEAUTOMATIC = 0x0012
PBT_POWERSETTINGCHANGE = 0x8013
DEVICE_NOTIFY_WINDOW_HANDLE = 0
NOTIFY_FOR_THIS_SESSION = 0

SESSION_EVENTS = {
    0x1: "console-connect", 0x2: "console-disconnect", 0x3: "remote-connect",
    0x4: "remote-disconnect", 0x5: "logon", 0x6: "logoff", 0x7: "lock", 0x8: "unlock",
}

GUID_CONSOLE_DISPLAY_STATE = uuid.UUID("6fe69556-704a-47a0-8f24-c28d936fda47")
GUID_SESSION_DISPLAY_STATUS = uuid.UUID("2b84c20e-ad23-4ddf-93db-05ffbd7efca5")
DISPLAY_STATES = {0: "off", 1: "on", 2: "dimmed"}

WTS_CURRENT_SERVER_HANDLE = 0
WTS_SESSION_INFO_EX = 25
WTS_SESSIONSTATE_LOCK = 0
WTS_SESSIONSTATE_UNLOCK = 1


class _GUID(ctypes.Structure):
    _fields_ = [("Data1", wintypes.DWORD), ("Data2", wintypes.WORD), ("Data3", wintypes.WORD),
                ("Data4", ctypes.c_ubyte * 8)]

    @classmethod
    def of(cls, value: uuid.UUID) -> _GUID:
        b = value.bytes_le
        return cls(int.from_bytes(b[0:4], "little"), int.from_bytes(b[4:6], "little"),
                   int.from_bytes(b[6:8], "little"), (ctypes.c_ubyte * 8)(*b[8:16]))

    def to_uuid(self) -> uuid.UUID:
        raw = (self.Data1.to_bytes(4, "little") + self.Data2.to_bytes(2, "little")
               + self.Data3.to_bytes(2, "little") + bytes(self.Data4))
        return uuid.UUID(bytes_le=raw)


class _PowerBroadcastSetting(ctypes.Structure):
    _fields_ = [("PowerSetting", _GUID), ("DataLength", wintypes.DWORD),
                ("Data", ctypes.c_ubyte * 4)]


class _WTSInfoExLevel1(ctypes.Structure):
    _fields_ = [("SessionId", wintypes.ULONG), ("SessionState", ctypes.c_int),
                ("SessionFlags", wintypes.LONG), ("WinStationName", wintypes.WCHAR * 33),
                ("UserName", wintypes.WCHAR * 21), ("DomainName", wintypes.WCHAR * 18),
                ("LogonTime", ctypes.c_longlong), ("ConnectTime", ctypes.c_longlong),
                ("DisconnectTime", ctypes.c_longlong), ("LastInputTime", ctypes.c_longlong),
                ("CurrentTime", ctypes.c_longlong)] + [
        (name, wintypes.DWORD) for name in ("IncomingBytes", "OutgoingBytes", "IncomingFrames",
                                            "OutgoingFrames", "IncomingCompressedBytes",
                                            "OutgoingCompressedBytes")]


class _WTSInfoEx(ctypes.Structure):
    _fields_ = [("Level", wintypes.DWORD), ("Data", _WTSInfoExLevel1)]


def current_session_id() -> int:
    session = wintypes.DWORD()
    ctypes.windll.kernel32.ProcessIdToSessionId(ctypes.windll.kernel32.GetCurrentProcessId(),
                                                ctypes.byref(session))
    return int(session.value)


def query_session() -> dict | None:
    """Return ``{"locked": bool | None, "idle_s": float | None}`` for this session."""
    wtsapi = ctypes.windll.wtsapi32
    buffer = ctypes.c_void_p()
    size = wintypes.DWORD()
    ok = wtsapi.WTSQuerySessionInformationW(WTS_CURRENT_SERVER_HANDLE, current_session_id(),
                                            WTS_SESSION_INFO_EX, ctypes.byref(buffer),
                                            ctypes.byref(size))
    if not ok or not buffer.value:
        return None
    try:
        info = ctypes.cast(buffer, ctypes.POINTER(_WTSInfoEx)).contents.Data
        flags = info.SessionFlags
        locked = (True if flags == WTS_SESSIONSTATE_LOCK
                  else False if flags == WTS_SESSIONSTATE_UNLOCK else None)
        idle = None
        if info.LastInputTime and info.CurrentTime:
            idle = max(0.0, (info.CurrentTime - info.LastInputTime) / 1e7)
        return {"locked": locked, "idle_s": idle}
    finally:
        wtsapi.WTSFreeMemory(buffer)


def is_session_locked() -> bool | None:
    try:
        info = query_session()
    except OSError:
        return None
    return None if info is None else info["locked"]


EventHandler = Callable[[str, dict], None]


class SessionWatcher:
    """Background listener; thread-safe state plus optional callbacks."""

    def __init__(self, on_event: EventHandler | None = None):
        self._handlers: list[EventHandler] = [on_event] if on_event else []
        self._lock = threading.Condition()
        self._thread: threading.Thread | None = None
        self._hwnd = None
        self.locked: bool | None = is_session_locked()
        self.display: str | None = None   # "on", "off", "dimmed", or unknown
        self.changed_at = time.monotonic()
        self.available = False

    def add_handler(self, handler: EventHandler) -> None:
        self._handlers.append(handler)

    def start(self) -> None:
        if self._thread is None:
            self._thread = threading.Thread(target=self._run, name="session-events", daemon=True)
            self._thread.start()

    def stop(self) -> None:
        hwnd = self._hwnd
        if hwnd:
            try:
                import win32gui
                win32gui.PostMessage(hwnd, WM_CLOSE, 0, 0)
            except Exception:
                pass

    def wait_for_change(self, timeout: float) -> None:
        with self._lock:
            self._lock.wait(timeout)

    def _emit(self, name: str, **detail) -> None:
        with self._lock:
            if name in ("lock", "unlock"):
                self.locked = name == "lock"
            elif name.startswith("display-"):
                self.display = name.split("-", 1)[1]
            self.changed_at = time.monotonic()
            self._lock.notify_all()
        log.info("session event: %s %s", name, detail or "")
        for handler in list(self._handlers):
            try:
                handler(name, detail)
            except Exception:
                log.exception("session event handler failed")

    def _run(self) -> None:
        try:
            import win32api
            import win32gui
            import win32ts
        except ImportError:
            log.warning("pywin32 unavailable; session events disabled")
            return
        user32 = ctypes.windll.user32
        user32.RegisterPowerSettingNotification.restype = ctypes.c_void_p
        user32.RegisterPowerSettingNotification.argtypes = [
            wintypes.HANDLE, ctypes.POINTER(_GUID), wintypes.DWORD]

        def wndproc(hwnd, msg, wparam, lparam):
            if msg == WM_WTSSESSION_CHANGE:
                name = SESSION_EVENTS.get(wparam)
                if name:
                    self._emit(name)
                return 0
            if msg == WM_POWERBROADCAST:
                if wparam == PBT_POWERSETTINGCHANGE and lparam:
                    setting = ctypes.cast(lparam, ctypes.POINTER(_PowerBroadcastSetting)).contents
                    guid = setting.PowerSetting.to_uuid()
                    if guid in (GUID_CONSOLE_DISPLAY_STATE, GUID_SESSION_DISPLAY_STATUS):
                        state = DISPLAY_STATES.get(setting.Data[0])
                        if state and state != self.display:
                            self._emit(f"display-{state}",
                                       source="console" if guid == GUID_CONSOLE_DISPLAY_STATE
                                       else "session")
                elif wparam == PBT_APMSUSPEND:
                    self._emit("sleep")
                elif wparam in (PBT_APMRESUMESUSPEND, PBT_APMRESUMEAUTOMATIC):
                    self._emit("resume")
                return 1
            if msg == WM_CLOSE:
                win32gui.DestroyWindow(hwnd)
                return 0
            if msg == WM_DESTROY:
                win32gui.PostQuitMessage(0)
                return 0
            return win32gui.DefWindowProc(hwnd, msg, wparam, lparam)

        cls = win32gui.WNDCLASS()
        cls.hInstance = win32api.GetModuleHandle(None)
        cls.lpszClassName = f"FaceUnlockSessionEvents{id(self)}"
        cls.lpfnWndProc = wndproc
        try:
            atom = win32gui.RegisterClass(cls)
            hwnd = win32gui.CreateWindowEx(0, atom, "FaceUnlock events", 0, 0, 0, 0, 0,
                                           0, 0, cls.hInstance, None)
        except Exception:
            log.exception("could not create the session event window")
            return
        self._hwnd = hwnd
        try:
            win32ts.WTSRegisterSessionNotification(hwnd, NOTIFY_FOR_THIS_SESSION)
        except Exception:
            log.exception("session notifications unavailable")
        handles = []
        for guid in (GUID_CONSOLE_DISPLAY_STATE, GUID_SESSION_DISPLAY_STATUS):
            handle = user32.RegisterPowerSettingNotification(
                hwnd, ctypes.byref(_GUID.of(guid)), DEVICE_NOTIFY_WINDOW_HANDLE)
            if handle:
                handles.append(handle)
        self.available = True
        try:
            win32gui.PumpMessages()
        finally:
            self.available = False
            for handle in handles:
                user32.UnregisterPowerSettingNotification(ctypes.c_void_p(handle))
            try:
                win32ts.WTSUnRegisterSessionNotification(hwnd)
            except Exception:
                pass
