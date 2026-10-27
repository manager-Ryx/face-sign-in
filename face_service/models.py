"""Model files: use the ones shipped with the app, so a new PC works offline.

DeepFace loads ArcFace's weights (and its YuNet detector) from
``%USERPROFILE%\\.deepface\\weights`` and downloads them – about 130 MB –
when they are missing. The installer bundles them in ``models``; before the
service first needs them, any that are missing are copied into place.
"""
from __future__ import annotations

import hashlib
import logging
import os
import shutil
import sys
import urllib.request
from collections.abc import Callable
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


# The file DeepFace 0.0.100 itself would fetch, and its SHA-256: a download that
# doesn't match (cut short, altered on the way) is never used.
ARCFACE_URL = "https://github.com/serengil/deepface_models/releases/download/v1.0/arcface_weights.h5"
ARCFACE_SHA256 = "6336979c0c602cae08d1122a66f4dfb862d059bbcd8ef80306aef2b2249b0c93"


def download_recognition_model(progress: Callable[[int, int], None] | None = None, *,
                               url: str = ARCFACE_URL, sha256: str = ARCFACE_SHA256,
                               timeout_s: float = 60.0) -> bool:
    """Fetch ArcFace's weights where DeepFace looks for them, reporting
    ``progress(done_bytes, total_bytes)`` as it goes (total is 0 if unknown).

    Returns False if the model was already there. Raises on a failed download
    or a file that doesn't match its checksum; nothing partial is left behind.
    """
    if recognition_model_present():
        return False
    from ._version import __version__
    target = deepface_weights_dir() / ARCFACE_WEIGHTS
    target.parent.mkdir(parents=True, exist_ok=True)
    partial = target.with_suffix(target.suffix + ".partial")
    request = urllib.request.Request(url, headers={"User-Agent": f"face-unlock/{__version__}"})
    digest = hashlib.sha256()
    try:
        with urllib.request.urlopen(request, timeout=timeout_s) as response, \
                open(partial, "wb") as out:
            total = int(response.headers.get("Content-Length") or 0)
            done = 0
            while chunk := response.read(1 << 20):
                out.write(chunk)
                digest.update(chunk)
                done += len(chunk)
                if progress is not None:
                    progress(done, total)
        if digest.hexdigest() != sha256:
            raise ValueError("the downloaded face model didn't match its checksum")
        os.replace(partial, target)
    finally:
        partial.unlink(missing_ok=True)
    log.info("face model downloaded (%d MB)", target.stat().st_size // 1_000_000)
    return True
