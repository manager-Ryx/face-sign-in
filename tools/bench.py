"""Time recognition checks through the running FaceService.

Usage: python -m tools.bench [N] [--record]

Each run opens the camera, waits for a stable image, and analyses five
frames. Prints per-run timing and outcome; nothing is learned, and runs are
only kept as evaluation data with ``--record``.
"""
from __future__ import annotations

import statistics
import sys
import time

from face_service.config import PIPE_NAME
from face_service.pipe import call


def main() -> None:
    args = [a for a in sys.argv[1:] if not a.startswith("--")]
    runs = int(args[0]) if args else 3
    record = "--record" in sys.argv
    totals, opens, analyses = [], [], []
    for index in range(runs):
        started = time.perf_counter()
        reply = call(PIPE_NAME, {"cmd": "check", "record": record}, timeout_s=60.0)
        elapsed = time.perf_counter() - started
        if not reply or not reply.get("ok"):
            print(f"run {index + 1}: failed ({(reply or {}).get('reason', 'no reply')})")
            continue
        totals.append(elapsed)
        opens.append(reply["camera_open_ms"] / 1000)
        analyses.append(reply["verify_ms"] / 1000)
        distances = [f["distance"] for f in reply["frames"] if f["distance"] is not None]
        print(f"run {index + 1}: {elapsed:.2f}s total (camera {opens[-1]:.2f}s, analysis "
              f"{analyses[-1]:.2f}s) accepted={reply['accepted']} reason={reply['reason']} "
              f"best={min(distances) if distances else '-'}")
        time.sleep(1.0)
    if totals:
        print(f"median: {statistics.median(totals):.2f}s total, "
              f"{statistics.median(opens):.2f}s camera, {statistics.median(analyses):.2f}s analysis")


if __name__ == "__main__":
    main()
