"""ArcFace face embeddings with DeepFace-identical preprocessing.

Detection, alignment and preprocessing call DeepFace's own functions exactly
as ``DeepFace.represent`` does, so embeddings match the existing enrollment
bit for bit. Only the network call differs: it is compiled once with
``tf.function`` instead of being re-interpreted layer by layer on every
frame. Measured on this PC (4 threads): 87 ms per face compiled versus
242 ms eager, with a maximum element difference of 4e-7. A self-check
against ``DeepFace.represent`` runs at start-up and falls back to DeepFace's
own call if the outputs ever disagree.
"""
from __future__ import annotations

import logging
import os
import threading
import time
from dataclasses import dataclass
from typing import Any

import numpy as np

from .quality import FaceBox

log = logging.getLogger(__name__)

PARITY_TOLERANCE = 1e-4  # cosine distance
SPOOF_RETRY_S = 60.0     # a failed liveness-model load (e.g. offline) is retried this often


class SpoofModelUnavailable(RuntimeError):
    """Photo protection is on, but its liveness model could not be loaded."""


@dataclass
class DetectedFace:
    box: FaceBox
    tensor: np.ndarray              # preprocessed (1, H, W, 3) network input
    is_real: bool | None = None
    spoof_score: float | None = None

    @property
    def area(self) -> int:
        return self.box.w * self.box.h


def cosine_distance(a: np.ndarray, b: np.ndarray) -> float:
    a = np.asarray(a, dtype=np.float64).ravel()
    b = np.asarray(b, dtype=np.float64).ravel()
    return float(1.0 - a @ b / ((np.linalg.norm(a) * np.linalg.norm(b)) + 1e-12))


class Embedder:
    """Thread-safe wrapper around DeepFace's detector and recognition model."""

    def __init__(self, model_name: str = "ArcFace", detector_backend: str = "yunet",
                 threads: int = 4):
        self.model_name = model_name
        self.detector_backend = detector_backend
        self.threads = threads
        self._lock = threading.RLock()
        self._client: Any = None
        self._compiled: Any = None
        self._input_shape: tuple[int, int] = (112, 112)
        self.compiled_ok: bool | None = None
        self._spoof_ready = False
        self._spoof_failed_at: float | None = None
        self.spoof_error: str | None = None

    # ---- set-up ------------------------------------------------------------

    def load(self) -> None:
        """Build the model and compile its forward pass (idempotent)."""
        with self._lock:
            if self._client is not None:
                return
            import tensorflow as tf
            from deepface.modules import modeling

            try:
                tf.config.threading.set_intra_op_parallelism_threads(self.threads)
                tf.config.threading.set_inter_op_parallelism_threads(1)
            except RuntimeError:
                pass  # TensorFlow was already initialised by the host process
            client = modeling.build_model(task="facial_recognition", model_name=self.model_name)
            keras_model = client.model
            height, width = client.input_shape
            self._input_shape = (int(height), int(width))
            signature = [tf.TensorSpec([None, self._input_shape[0], self._input_shape[1], 3],
                                       tf.float32)]
            self._compiled = tf.function(lambda x: keras_model(x, training=False),
                                         input_signature=signature)
            self._client = client

    @property
    def loaded(self) -> bool:
        return self._client is not None

    def load_spoof_model(self) -> bool:
        """Build DeepFace's liveness model (idempotent).

        Returns False, with the reason in ``spoof_error``, when it can't be
        loaded (PyTorch missing, or its weights absent while offline). A
        failure is retried at most once a minute, so frames aren't held up by
        repeated downloads.
        """
        with self._lock:
            if self._spoof_ready:
                return True
            now = time.monotonic()
            if self._spoof_failed_at is not None and now - self._spoof_failed_at < SPOOF_RETRY_S:
                return False
            try:
                from deepface.modules import modeling

                modeling.build_model(task="spoofing", model_name="Fasnet")
            except Exception as exc:  # noqa: BLE001 - ImportError, ValueError, network errors
                self._spoof_failed_at, self.spoof_error = now, str(exc) or type(exc).__name__
                log.error("photo protection model unavailable: %s", self.spoof_error)
                return False
            self._spoof_ready, self._spoof_failed_at, self.spoof_error = True, None, None
            return True

    def self_check(self, bgr: np.ndarray) -> float | None:
        """Compare against ``DeepFace.represent``; disable compilation on mismatch."""
        from deepface import DeepFace

        self.load()
        with self._lock:
            try:
                reference = DeepFace.represent(
                    img_path=bgr, model_name=self.model_name,
                    detector_backend=self.detector_backend, enforce_detection=True, align=True)
            except Exception as exc:  # no face in the sample image
                log.info("embedding self-check skipped: %s", exc)
                return None
            faces = self.detect(bgr, min_confidence=0.9)
            if not faces or not reference:
                return None
            ours = self.embed([faces[0]])[0]
            distance = cosine_distance(ours, np.asarray(reference[0]["embedding"]))
            self.compiled_ok = distance <= PARITY_TOLERANCE
            if not self.compiled_ok:
                log.warning("compiled embeddings differ from DeepFace (%.2e); using DeepFace's "
                            "own forward pass", distance)
            return distance

    # ---- inference ---------------------------------------------------------

    def detect(self, bgr: np.ndarray, *, min_confidence: float,
               anti_spoofing: bool = False) -> list[DetectedFace]:
        """Detect and align faces exactly as ``DeepFace.represent`` would."""
        from deepface.modules import detection, preprocessing

        self.load()
        if anti_spoofing and not self.load_spoof_model():
            # Never report "no face" for a model that isn't there: that would
            # read as absence, and every sign-in would quietly fail.
            raise SpoofModelUnavailable(self.spoof_error or "liveness model unavailable")
        with self._lock:
            # DeepFace's YuNet wrapper reads its score threshold from the
            # environment on every call; all inference is serialised by
            # this lock, so setting it here is race-free.
            os.environ["yunet_score_threshold"] = f"{min_confidence:.3f}"  # noqa: SIM112
            try:
                objs = detection.extract_faces(
                    img_path=bgr, detector_backend=self.detector_backend, grayscale=False,
                    enforce_detection=True, align=True, expand_percentage=0,
                    anti_spoofing=anti_spoofing)
            except ValueError:
                return []  # DeepFace signals "no face" with ValueError subclasses
        faces = []
        for obj in objs:
            area = obj["facial_area"]
            box = FaceBox(int(area["x"]), int(area["y"]), int(area["w"]), int(area["h"]),
                          float(obj.get("confidence") or 0.0),
                          _point(area.get("left_eye")), _point(area.get("right_eye")))
            img = obj["face"][:, :, ::-1]  # RGB -> BGR, as represent() does
            img = preprocessing.resize_image(
                img=img, target_size=(self._input_shape[1], self._input_shape[0]))
            img = preprocessing.normalize_input(img=img, normalization="base")
            faces.append(DetectedFace(box, np.asarray(img, dtype=np.float32),
                                      obj.get("is_real"), obj.get("antispoof_score")))
        faces.sort(key=lambda face: face.area, reverse=True)
        return faces

    def embed(self, faces: list[DetectedFace]) -> np.ndarray:
        """Return one embedding row per face (same values as DeepFace)."""
        if not faces:
            return np.empty((0, 512), dtype=np.float32)
        self.load()
        batch = np.concatenate([face.tensor for face in faces], axis=0)
        with self._lock:
            if self.compiled_ok is False:
                out = self._client.forward(batch)
            else:
                out = self._compiled(batch).numpy()
        return np.atleast_2d(np.asarray(out, dtype=np.float32))


def _point(value: Any) -> tuple[int, int] | None:
    if value is None:
        return None
    return int(value[0]), int(value[1])
