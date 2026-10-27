"""Loading the photo-protection (liveness) model, without TensorFlow or PyTorch."""
from __future__ import annotations

import sys
import types
import unittest
from unittest.mock import patch

import _isolation  # noqa: F401
import numpy as np

from face_service.embedder import Embedder, SpoofModelUnavailable


def fake_deepface(build_model) -> dict[str, types.ModuleType]:
    modeling = types.ModuleType("deepface.modules.modeling")
    modeling.build_model = build_model  # type: ignore[attr-defined]
    modules = types.ModuleType("deepface.modules")
    modules.modeling = modeling  # type: ignore[attr-defined]
    modules.detection = types.ModuleType("deepface.modules.detection")  # type: ignore[attr-defined]
    modules.preprocessing = types.ModuleType(  # type: ignore[attr-defined]
        "deepface.modules.preprocessing")
    return {"deepface": types.ModuleType("deepface"), "deepface.modules": modules,
            "deepface.modules.modeling": modeling}


class SpoofModelTests(unittest.TestCase):
    def setUp(self) -> None:
        self.embedder = Embedder()
        self.embedder._client = object()  # the recognition model counts as loaded

    def test_a_missing_model_is_an_error_not_an_empty_frame(self) -> None:
        calls = []

        def offline(task: str, model_name: str):
            calls.append(model_name)
            raise ValueError("weights missing and no internet")

        with patch.dict(sys.modules, fake_deepface(offline)):
            with self.assertRaises(SpoofModelUnavailable):
                self.embedder.detect(np.zeros((8, 8, 3), np.uint8), min_confidence=0.5,
                                     anti_spoofing=True)
            self.assertFalse(self.embedder.load_spoof_model())  # not retried at once
        self.assertEqual(calls, ["Fasnet"])
        self.assertIn("no internet", self.embedder.spoof_error)

    def test_loads_once(self) -> None:
        calls = []
        with patch.dict(sys.modules, fake_deepface(lambda task, model_name: calls.append(task))):
            self.assertTrue(self.embedder.load_spoof_model())
            self.assertTrue(self.embedder.load_spoof_model())
        self.assertEqual(calls, ["spoofing"])
        self.assertIsNone(self.embedder.spoof_error)


if __name__ == "__main__":
    unittest.main()
