"""Bounded local measurements: scalars only, rotated, expired, clearable."""
from __future__ import annotations

import json
import tempfile
import time
import unittest
from pathlib import Path

import _isolation  # noqa: F401
import numpy as np

from face_service.metrics import MetricsStore


class MetricsTests(unittest.TestCase):
    def setUp(self) -> None:
        self.dir = tempfile.TemporaryDirectory()
        self.path = Path(self.dir.name) / "events.jsonl"

    def tearDown(self) -> None:
        self.dir.cleanup()

    def test_records_scalars_only(self) -> None:
        store = MetricsStore(self.path)
        event = store.record("x", a=1, b=0.123456789, c=np.float32(2.5),
                             vector=np.zeros(512), text="ok")
        self.assertNotIn("vector", event)
        self.assertEqual(event["b"], 0.1235)
        self.assertEqual(event["c"], 2.5)
        line = json.loads(self.path.read_text(encoding="utf-8").strip())
        self.assertEqual(line["kind"], "x")

    def test_rotation_keeps_files_bounded(self) -> None:
        store = MetricsStore(self.path, max_bytes=2_000, backups=2)
        for i in range(400):
            store.record("tick", i=i, padding="x" * 20)
        files = list(Path(self.dir.name).glob("events.jsonl*"))
        self.assertLessEqual(len(files), 3)
        self.assertTrue(all(f.stat().st_size <= 2_100 for f in files))
        reloaded = MetricsStore(self.path, max_bytes=2_000, backups=2)
        self.assertEqual(reloaded.events()[-1]["i"], 399)

    def test_old_rotated_files_expire(self) -> None:
        clock = [time.time()]
        store = MetricsStore(self.path, max_bytes=500, backups=3, retention_days=1,
                             clock=lambda: clock[0])
        for i in range(30):
            store.record("tick", i=i, pad="y" * 30)
        clock[0] += 3 * 86400
        for i in range(30):
            store.record("tick", i=i, pad="y" * 30)
        self.assertLessEqual(len(list(Path(self.dir.name).glob("events.jsonl.*"))), 3)

    def test_clear_by_kind(self) -> None:
        store = MetricsStore(self.path)
        store.record("keep")
        store.record("drop")
        self.assertEqual(store.clear(["drop"]), 1)
        self.assertEqual([e["kind"] for e in MetricsStore(self.path).events()], ["keep"])

    def test_corrupt_lines_are_skipped(self) -> None:
        self.path.write_text('{"ts": 1, "kind": "ok"}\nnot json\n{"kind": "no ts"}\n',
                             encoding="utf-8")
        self.assertEqual(len(MetricsStore(self.path).events()), 1)


if __name__ == "__main__":
    unittest.main()
