"""Named-pipe transport on a private test pipe (never the real service pipe)."""
from __future__ import annotations

import os
import threading
import time
import unittest

import _isolation  # noqa: F401
import win32file

from face_service import pipe


class PipeTests(unittest.TestCase):
    def setUp(self) -> None:
        self.name = rf"\\.\pipe\FaceUnlockTest-{os.getpid()}-{id(self)}"
        self.server = pipe.PipeServer(self.name, self.handle, read_timeout_s=1.0,
                                      write_timeout_s=1.0)
        self.thread = threading.Thread(target=self.server.serve_forever, daemon=True)
        self.thread.start()
        time.sleep(0.1)

    def tearDown(self) -> None:
        self.server.stop()
        self.thread.join(3)

    @staticmethod
    def handle(request: dict, client: pipe.ClientInfo) -> dict:
        if request.get("cmd") == "slow":
            time.sleep(0.6)
        if request.get("cmd") == "boom":
            raise RuntimeError("bad")
        return {"ok": True, "echo": request.get("cmd"), "system": client.is_system,
                "pid": client.pid}

    def test_round_trip_identifies_the_caller(self) -> None:
        reply = pipe.call(self.name, {"cmd": "ping"}, timeout_s=3)
        self.assertEqual(reply["echo"], "ping")
        self.assertFalse(reply["system"])
        self.assertEqual(reply["pid"], os.getpid())

    def test_a_silent_client_cannot_block_others(self) -> None:
        stalled = win32file.CreateFile(self.name, win32file.GENERIC_READ |
                                       win32file.GENERIC_WRITE, 0, None,
                                       win32file.OPEN_EXISTING, 0, None)
        try:
            started = time.monotonic()
            self.assertEqual(pipe.call(self.name, {"cmd": "x"}, timeout_s=3)["echo"], "x")
            self.assertLess(time.monotonic() - started, 0.5)
        finally:
            win32file.CloseHandle(stalled)

    def test_requests_run_in_parallel(self) -> None:
        replies = []
        threads = [threading.Thread(target=lambda: replies.append(
            pipe.call(self.name, {"cmd": "slow"}, timeout_s=5))) for _ in range(3)]
        started = time.monotonic()
        for t in threads:
            t.start()
        for t in threads:
            t.join()
        self.assertEqual(len([r for r in replies if r]), 3)
        self.assertLess(time.monotonic() - started, 1.5)

    def test_client_timeout_is_honoured(self) -> None:
        started = time.monotonic()
        self.assertIsNone(pipe.call(self.name, {"cmd": "slow"}, timeout_s=0.2))
        self.assertLess(time.monotonic() - started, 0.5)

    def test_handler_errors_become_replies(self) -> None:
        reply = pipe.call(self.name, {"cmd": "boom"}, timeout_s=3)
        self.assertEqual(reply["reason"], "internal-error")

    def test_stop_is_immediate(self) -> None:
        started = time.monotonic()
        self.server.stop()
        self.thread.join(2)
        self.assertFalse(self.thread.is_alive())
        self.assertLess(time.monotonic() - started, 0.5)

    def test_unavailable_pipe(self) -> None:
        self.assertIsNone(pipe.call(r"\\.\pipe\FaceUnlock-missing-test", {"cmd": "x"},
                                    timeout_s=0.3))


if __name__ == "__main__":
    unittest.main()
