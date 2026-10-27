"""Model files: use the ones shipped with the app, so a new PC works offline.

DeepFace loads ArcFace's weights (and its YuNet detector) from
``%USERPROFILE%\\.deepface\\weights`` and downloads them – about 130 MB –
when they are missing. The installer bundles them in ``models``; before the
service first needs them, any that are missing are copied into place.
"""
from __future__ import annotations

import logging
import os
import shutil
import sys
from pathlib import Path

log = logging.getLogger(__name__)

ARCFACE_WEIGHTS = "arcface_weights.h5"
YUNET = "face_detection_yunet_2023mar.onnx"
MIN_ARCFACE_BYTES = 130_000_000


def bundle_dir() -> Path:
    """Where the app's own model files live (also inside a packaged build)."""
    root = getattr(sys, "_MEIPASS", None)
    return Path(root) / "models" if root else Path(__file__).resolve().parent.parent / "models"


def deepface_weights_dir() -> Path:
    home = os.environ.get("DEEPFACE_HOME") or str(Path.home())
    return Path(home) / ".deepface" / "weights"


def _bundled_files() -> list[Path]:
    base = bundle_dir()
    files = [base / YUNET]
    weights = base / "weights"
    if weights.is_dir():
        files += [p for p in weights.iterdir() if p.is_file()]
    return [p for p in files if p.is_file()]


def provide_bundled_models() -> list[str]:
    """Copy shipped model files DeepFace doesn't have yet; returns their names."""
    target = deepface_weights_dir()
    copied = []
    for source in _bundled_files():
        destination = target / source.name
        if destination.exists() and destination.stat().st_size == source.stat().st_size:
            continue
        try:
            target.mkdir(parents=True, exist_ok=True)
            partial = destination.with_suffix(destination.suffix + ".partial")
            shutil.copyfile(source, partial)
            os.replace(partial, destination)
            copied.append(source.name)
        except OSError as exc:
            log.warning("could not provide %s: %s", source.name, exc)
    if copied:
        log.info("provided bundled model files: %s", ", ".join(copied))
    return copied


def recognition_model_present() -> bool:
    """Is ArcFace available without downloading it?"""
    weights = deepface_weights_dir() / ARCFACE_WEIGHTS
    return weights.is_file() and weights.stat().st_size >= MIN_ARCFACE_BYTES
