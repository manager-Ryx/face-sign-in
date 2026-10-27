"""Fetching the recognition model: progress, checksum, nothing half-written."""
from __future__ import annotations

import hashlib
import os
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

import _isolation  # noqa: F401

from face_service import models


class DownloadTests(unittest.TestCase):
    def setUp(self) -> None:
        self.folder = tempfile.TemporaryDirectory()
        root = Path(self.folder.name)
        self.source = root / "served.h5"
        self.source.write_bytes(os.urandom(3 << 20))  # 3 MB: several progress steps
        self.sha = hashlib.sha256(self.source.read_bytes()).hexdigest()
        self.env = patch.dict(os.environ, {"DEEPFACE_HOME": str(root / "home")})
        self.env.start()

    def tearDown(self) -> None:
        self.env.stop()
        self.folder.cleanup()

    def target(self) -> Path:
        return models.deepface_weights_dir() / models.ARCFACE_WEIGHTS

    def test_downloads_with_progress(self) -> None:
        seen = []
        fetched = models.download_recognition_model(lambda done, total: seen.append((done, total)),
                                                    url=self.source.as_uri(), sha256=self.sha)
        self.assertTrue(fetched)
        self.assertEqual(self.target().read_bytes(), self.source.read_bytes())
        self.assertGreaterEqual(len(seen), 3)
        self.assertEqual(seen[-1], (3 << 20, 3 << 20))

    def test_a_file_that_fails_its_checksum_is_never_kept(self) -> None:
        with self.assertRaises(ValueError):
            models.download_recognition_model(url=self.source.as_uri(), sha256="0" * 64)
        self.assertFalse(self.target().exists())
        self.assertEqual(list(self.target().parent.iterdir()), [])  # no partial file

    def test_nothing_to_do_when_the_model_is_there(self) -> None:
        with patch.object(models, "recognition_model_present", return_value=True):
            self.assertFalse(models.download_recognition_model(url="http://unused.invalid/x"))


if __name__ == "__main__":
    unittest.main()
