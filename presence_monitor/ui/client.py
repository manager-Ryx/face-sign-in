"""Talking to FaceService without ever blocking the window.

Every request runs on a worker thread with a deadline; the reply comes back
on the GUI thread through a queued signal.
"""
from __future__ import annotations

import logging
from collections.abc import Callable
from typing import Any

from PySide6.QtCore import QObject, QRunnable, QThreadPool, QTimer, Signal

from face_service import pipe
from face_service.config import PIPE_NAME

log = logging.getLogger(__name__)

Reply = dict[str, Any] | None


class _Relay(QObject):
    done = Signal(object, object)   # callback, reply


class _Call(QRunnable):
    def __init__(self, relay: _Relay, request: dict, timeout_s: float,
                 callback: Callable[[Reply], None] | None):
        super().__init__()
        self.relay = relay
        self.request = request
        self.timeout_s = timeout_s
        self.callback = callback

    def run(self) -> None:
        try:
            reply = pipe.call(PIPE_NAME, self.request, timeout_s=self.timeout_s)
        except Exception:  # never let a worker crash silently
            log.exception("service request %s failed", self.request.get("cmd"))
            reply = None
        try:
            self.relay.done.emit(self.callback, reply)
        except RuntimeError:
            pass  # the app is closing and the client is gone; nobody is waiting


class ServiceClient(QObject):
    status_changed = Signal(object)

    def __init__(self, parent: QObject | None = None):
        super().__init__(parent)
        self._pool = QThreadPool(self)
        self._pool.setMaxThreadCount(4)
        self._relay = _Relay()
        self._relay.done.connect(self._deliver)
        self.status: Reply = None
        self._status_pending = False
        self._timer = QTimer(self)
        self._timer.timeout.connect(self.refresh_status)

    def _deliver(self, callback: Callable[[Reply], None] | None, reply: Reply) -> None:
        if callback is not None:
            try:
                callback(reply)
            except Exception:
                log.exception("service reply handler failed")

    def request(self, request: dict, callback: Callable[[Reply], None] | None = None, *,
                timeout_s: float = 10.0) -> None:
        self._pool.start(_Call(self._relay, request, timeout_s, callback))

    def refresh_status(self) -> None:
        if self._status_pending:
            return
        self._status_pending = True

        def done(reply: Reply) -> None:
            self._status_pending = False
            self.status = reply if reply and reply.get("ok") else None
            self.status_changed.emit(self.status)

        self.request({"cmd": "status"}, done, timeout_s=4.0)

    def set_polling(self, interval_ms: int) -> None:
        if self._timer.interval() != interval_ms or not self._timer.isActive():
            self._timer.start(interval_ms)

    def shutdown(self) -> None:
        self._timer.stop()
        self._pool.waitForDone(3000)
