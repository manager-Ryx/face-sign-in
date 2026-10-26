from __future__ import annotations

import unittest

import _isolation  # noqa: F401
import numpy as np

from face_service.quality import FaceBox, assess, primary_issue

FRONTAL = dict(left_eye=(360, 200), right_eye=(260, 200))


def textured(brightness: float, *, noise: float = 30.0, size=(480, 640)) -> np.ndarray:
    rng = np.random.default_rng(0)
    img = rng.normal(brightness, noise, size=size)
    return np.clip(img, 0, 255).astype(np.uint8)


class QualityTests(unittest.TestCase):
    def test_good_frontal_face_is_usable(self) -> None:
        q = assess(textured(120), FaceBox(200, 100, 220, 260, 0.95, **FRONTAL))
        self.assertTrue(q.usable)
        self.assertEqual(q.issues, ())
        self.assertEqual(q.lighting, "bright")
        self.assertGreater(q.score(), 0.5)

    def test_dark_face_is_unusable_with_guidance(self) -> None:
        q = assess(textured(15, noise=4), FaceBox(200, 100, 220, 260, 0.9, **FRONTAL))
        self.assertIn("too_dark", q.issues)
        self.assertFalse(q.usable)
        self.assertEqual(primary_issue(q.issues), "too_dark")
        self.assertEqual(q.lighting, "dark")

    def test_dim_face_is_still_usable_but_classified_dim(self) -> None:
        q = assess(textured(55), FaceBox(200, 100, 220, 260, 0.9, **FRONTAL))
        self.assertTrue(q.usable)
        self.assertEqual(q.lighting, "dim")

    def test_small_face_asks_to_move_closer(self) -> None:
        q = assess(textured(120), FaceBox(300, 200, 40, 50, 0.9))
        self.assertIn("too_far", q.issues)
        self.assertFalse(q.usable)

    def test_backlit_face_is_detected_from_face_region_not_frame(self) -> None:
        gray = textured(200)
        gray[100:360, 200:420] = textured(45, size=(260, 220))
        q = assess(gray, FaceBox(200, 100, 220, 260, 0.9, **FRONTAL))
        self.assertIn("backlit", q.issues)
        self.assertEqual(q.lighting, "backlit")

    def test_blurred_face_is_flagged(self) -> None:
        q = assess(np.full((480, 640), 120, dtype=np.uint8),
                   FaceBox(200, 100, 220, 260, 0.9, **FRONTAL))
        self.assertIn("blurry", q.issues)

    def test_face_cut_off_at_edge(self) -> None:
        q = assess(textured(120), FaceBox(0, 100, 220, 260, 0.9, **FRONTAL))
        self.assertIn("partial", q.issues)

    def test_turned_head_from_eye_spacing(self) -> None:
        q = assess(textured(120), FaceBox(200, 100, 220, 260, 0.9, left_eye=(320, 200),
                                          right_eye=(290, 200)))
        self.assertIn("turned", q.issues)

    def test_no_face(self) -> None:
        q = assess(textured(120), None)
        self.assertEqual(q.issues, ("no_face",))
        self.assertFalse(q.usable)


if __name__ == "__main__":
    unittest.main()
