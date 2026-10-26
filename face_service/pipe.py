"""Named-pipe transport: one JSON request, one JSON reply, every step bounded.

Server: overlapped I/O throughout. The accept loop waits on "client
connected" *or* "stop", so shutdown is immediate; each connection is served
on its own thread with read/write deadlines, so a client that connects and
never writes cannot stall anyone else. The reply is drained by waiting for
the client to close (bounded) rather than an unbounded ``FlushFileBuffers``.

Client: the same deadlines, used by the tray app and tools.
"""
from __future__ import annotations

import json
import logging
import threading
import time
from collections.abc import Callable
from dataclasses import dataclass

import pywintypes
import win32api
import win32con
import win32event
import win32file
import win32pipe
import win32security
import winerror

log = logging.getLogger(__name__)

MAX_MESSAGE = 64 * 1024
SYSTEM_SID = "S-1-5-18"
_FILE_FLAG_FIRST_PIPE_INSTANCE = 0x00080000
_PIPE_REJECT_REMOTE_CLIENTS = 0x00000008


class PipeTimeout(TimeoutError):
    pass


@dataclass(frozen=True)
class ClientInfo:
    pid: int | None
    sid: str | None

    @property
    def is_system(self) -> bool:
        return self.sid == SYSTEM_SID


def _security_attributes() -> win32security.SECURITY_ATTRIBUTES:
    """Only this user and LocalSystem (the sign-in screen) may connect."""
    token = win32security.OpenProcessToken(win32api.GetCurrentProcess(), win32con.TOKEN_QUERY)
    try:
        user_sid = win32security.GetTokenInformation(token, win32security.TokenUser)[0]
    finally:
        token.Close()
    system_sid = win32security.ConvertStringSidToSid(SYSTEM_SID)
    dacl = win32security.ACL()
    access = win32con.GENERIC_READ | win32con.GENERIC_WRITE
    dacl.AddAccessAllowedAce(win32security.ACL_REVISION, access, user_sid)
    dacl.AddAccessAllowedAce(win32security.ACL_REVISION, access, system_sid)
    sd = win32security.SECURITY_DESCRIPTOR()
    sd.SetSecurityDescriptorOwner(user_sid, False)
    sd.SetSecurityDescriptorDacl(True, dacl, False)
    sa = win32security.SECURITY_ATTRIBUTES()
    sa.SECURITY_DESCRIPTOR = sd
    sa.bInheritHandle = 0
    return sa


def _wait(handle, overlapped, deadline: float, stop_event=None) -> int:
    """Wait for an overlapped operation; returns bytes transferred."""
    remaining = max(0, int((deadline - time.monotonic()) * 1000))
    handles = [overlapped.hEvent] + ([stop_event] if stop_event is not None else [])
    result = win32event.WaitForMultipleObjects(handles, False, remaining)
    if result != win32event.WAIT_OBJECT_0:
        win32file.CancelIo(handle)
        try:
            win32file.GetOverlappedResult(handle, overlapped, True)
        except pywintypes.error:
            pass
        raise PipeTimeout("pipe operation timed out" if result == win32event.WAIT_TIMEOUT
                          else "stopped")
    return win32file.GetOverlappedResult(handle, overlapped, False)


def _read_message(handle, deadline: float) -> bytes:
    """Read one message; anything larger than ``MAX_MESSAGE`` is rejected."""
    overlapped = pywintypes.OVERLAPPED()
    overlapped.hEvent = win32event.CreateEvent(None, True, False, None)
    buffer = win32file.AllocateReadBuffer(MAX_MESSAGE)
    try:
        rc, _ = win32file.ReadFile(handle, buffer, overlapped)
        if rc == winerror.ERROR_MORE_DATA:
            raise ValueError("message too large")
        count = _wait(handle, overlapped, deadline)
    except pywintypes.error as exc:
        if exc.winerror == winerror.ERROR_MORE_DATA:
            raise ValueError("message too large") from exc
        raise
    finally:
        win32api.CloseHandle(overlapped.hEvent)
    return bytes(buffer[:count])


def _write_message(handle, payload: bytes, deadline: float) -> None:
    overlapped = pywintypes.OVERLAPPED()
    overlapped.hEvent = win32event.CreateEvent(None, True, False, None)
    try:
        win32file.WriteFile(handle, payload, overlapped)
        if _wait(handle, overlapped, deadline) != len(payload):
            raise OSError("short pipe write")
    finally:
        win32api.CloseHandle(overlapped.hEvent)


Handler = Callable[[dict, ClientInfo], dict]


class PipeServer:
    def __init__(self, name: str, handler: Handler, *, read_timeout_s: float = 5.0,
                 write_timeout_s: float = 5.0):
        self.name = name
        self.handler = handler
        self.read_timeout_s = read_timeout_s
        self.write_timeout_s = write_timeout_s
        self._stop = win32event.CreateEvent(None, True, False, None)

    def stop(self) -> None:
        win32event.SetEvent(self._stop)

    @property
    def stopping(self) -> bool:
        return win32event.WaitForSingleObject(self._stop, 0) == win32event.WAIT_OBJECT_0

    def serve_forever(self) -> None:
        sa = _security_attributes()
        first = True
        while not self.stopping:
            try:
                flags = win32pipe.PIPE_ACCESS_DUPLEX | win32file.FILE_FLAG_OVERLAPPED
                if first:
                    flags |= _FILE_FLAG_FIRST_PIPE_INSTANCE
                handle = win32pipe.CreateNamedPipe(
                    self.name, flags,
                    win32pipe.PIPE_TYPE_MESSAGE | win32pipe.PIPE_READMODE_MESSAGE
                    | win32pipe.PIPE_WAIT | _PIPE_REJECT_REMOTE_CLIENTS,
                    win32pipe.PIPE_UNLIMITED_INSTANCES, MAX_MESSAGE, MAX_MESSAGE, 0, sa)
            except pywintypes.error as exc:
                if first and exc.winerror == winerror.ERROR_ACCESS_DENIED:
                    # The pipe still exists: usually the previous service is
                    # still shutting down. Wait for it rather than sharing the
                    # name with a process we don't know.
                    log.warning("%s is still owned by another process; waiting", self.name)
                    win32event.WaitForSingleObject(self._stop, 2000)
                    continue
                log.exception("could not create pipe instance")
                win32event.WaitForSingleObject(self._stop, 500)
                continue
            first = False
            if not self._accept(handle):
                break

    def _accept(self, handle) -> bool:
        """Wait for a client (or stop); hand the connection to a worker thread."""
        overlapped = pywintypes.OVERLAPPED()
        overlapped.hEvent = win32event.CreateEvent(None, True, False, None)
        handed_off = False
        try:
            try:
                rc = win32pipe.ConnectNamedPipe(handle, overlapped)
            except pywintypes.error as exc:
                if exc.winerror != winerror.ERROR_PIPE_CONNECTED:
                    log.warning("pipe accept failed: %s", exc)
                    return not self.stopping
                rc = winerror.ERROR_PIPE_CONNECTED
            if rc == winerror.ERROR_IO_PENDING:
                result = win32event.WaitForMultipleObjects(
                    [overlapped.hEvent, self._stop], False, win32event.INFINITE)
                if result != win32event.WAIT_OBJECT_0:
                    win32file.CancelIo(handle)
                    return False
            threading.Thread(target=self._serve_client, args=(handle,), name="pipe-client",
                             daemon=True).start()
            handed_off = True
            return True
        finally:
            win32api.CloseHandle(overlapped.hEvent)
            if not handed_off:
                win32file.CloseHandle(handle)

    def _client_info(self, handle) -> ClientInfo:
        pid = None
        sid = None
        try:
            pid = int(win32pipe.GetNamedPipeClientProcessId(handle))
        except (pywintypes.error, AttributeError):
            pass
        try:
            win32security.ImpersonateNamedPipeClient(handle)
            try:
                token = win32security.OpenThreadToken(win32api.GetCurrentThread(),
                                                      win32con.TOKEN_QUERY, True)
                try:
                    user = win32security.GetTokenInformation(token, win32security.TokenUser)[0]
                    sid = win32security.ConvertSidToStringSid(user)
                finally:
                    token.Close()
            finally:
                win32security.RevertToSelf()
        except Exception as exc:  # identification is best-effort
            log.debug("could not identify pipe client: %s", exc)
        return ClientInfo(pid, sid)

    def _serve_client(self, handle) -> None:
        try:
            try:
                raw = _read_message(handle, time.monotonic() + self.read_timeout_s)
                request = json.loads(raw.decode("utf-8"))
                if not isinstance(request, dict):
                    raise ValueError("request must be an object")
            except (PipeTimeout, ValueError, UnicodeDecodeError, pywintypes.error) as exc:
                log.info("rejected pipe request: %s", exc)
                return
            client = self._client_info(handle)
            try:
                response = self.handler(request, client)
            except Exception as exc:
                log.exception("pipe handler failed")
                response = {"ok": False, "reason": "internal-error", "message": str(exc)[:200]}
            payload = (json.dumps(response, ensure_ascii=False) + "\n").encode("utf-8")
            try:
                _write_message(handle, payload, time.monotonic() + self.write_timeout_s)
                # Wait (bounded) for the client to read the reply and hang up.
                try:
                    _read_message(handle, time.monotonic() + self.write_timeout_s)
                except (PipeTimeout, pywintypes.error, ValueError):
                    pass
            except (PipeTimeout, pywintypes.error) as exc:
                code = getattr(exc, "winerror", None)
                if code in (winerror.ERROR_BROKEN_PIPE, winerror.ERROR_NO_DATA):
                    log.info("client closed the pipe before the reply (cancelled)")
                else:
                    log.info("reply not delivered: %s", exc)
        finally:
            try:
                win32pipe.DisconnectNamedPipe(handle)
            except pywintypes.error:
                pass
            win32file.CloseHandle(handle)


def call(name: str, request: dict, *, timeout_s: float = 10.0) -> dict | None:
    """Send one request; return the reply, or ``None`` if unavailable/timed out."""
    deadline = time.monotonic() + timeout_s
    handle = None
    while handle is None:
        try:
            handle = win32file.CreateFile(
                name, win32file.GENERIC_READ | win32file.GENERIC_WRITE, 0, None,
                win32file.OPEN_EXISTING, win32file.FILE_FLAG_OVERLAPPED, None)
        except pywintypes.error as exc:
            remaining = deadline - time.monotonic()
            if remaining <= 0:
                return None
            if exc.winerror == winerror.ERROR_PIPE_BUSY:
                try:
                    win32pipe.WaitNamedPipe(name, int(min(remaining, 0.25) * 1000))
                except pywintypes.error:
                    pass
            elif exc.winerror == winerror.ERROR_FILE_NOT_FOUND:
                time.sleep(min(remaining, 0.2))
            else:
                log.debug("pipe connect failed: %s", exc)
                return None
    try:
        win32pipe.SetNamedPipeHandleState(handle, win32pipe.PIPE_READMODE_MESSAGE, None, None)
        _write_message(handle, (json.dumps(request) + "\n").encode("utf-8"), deadline)
        raw = _read_message(handle, deadline)
        return json.loads(raw.decode("utf-8").strip())
    except (PipeTimeout, pywintypes.error, ValueError, OSError) as exc:
        log.debug("pipe call %s failed: %s", request.get("cmd"), exc)
        return None
    finally:
        win32file.CloseHandle(handle)
