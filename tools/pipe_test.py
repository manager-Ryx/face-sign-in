"""Send one command to the running FaceService and print the reply.

Usage: python -m tools.pipe_test [status|ping|presence|insights|...]

The ``unlock`` command is refused here: its reply contains your Windows
password and is meant only for the sign-in screen.
"""
from __future__ import annotations

import json
import sys

from face_service.config import PIPE_NAME
from face_service.pipe import call


def main() -> None:
    command = sys.argv[1] if len(sys.argv) > 1 else "ping"
    if command == "unlock":
        raise SystemExit("Refusing to request credentials from a command-line tool.")
    reply = call(PIPE_NAME, {"cmd": command}, timeout_s=30.0)
    if reply is None:
        raise SystemExit("FaceService is not responding.")
    print(json.dumps(reply, indent=2))


if __name__ == "__main__":
    main()
