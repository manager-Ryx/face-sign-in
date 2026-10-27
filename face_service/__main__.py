import multiprocessing
import os
import sys


def _give_output_somewhere() -> None:
    """A windowed (packaged) program has no console: sys.stdout and sys.stderr are
    None, and anything that prints – such as the download progress bar DeepFace
    shows while fetching a model – fails. Send it nowhere instead."""
    for name in ("stdout", "stderr"):
        if getattr(sys, name) is None:
            setattr(sys, name, open(os.devnull, "w", encoding="utf-8"))  # noqa: SIM115


def _configure_threads() -> None:
    """Size TensorFlow's thread pools before it is imported anywhere."""
    # numpy's BLAS would otherwise reserve a buffer per CPU core (about 790 MB
    # of committed memory on a 28-core PC) for arrays that never need threads.
    os.environ.setdefault("OPENBLAS_NUM_THREADS", "1")
    try:
        from face_service.config import load_config
        threads = load_config().config.inference_threads
    except Exception:
        threads = 4
    for name in ("OMP_NUM_THREADS", "TF_NUM_INTRAOP_THREADS"):
        os.environ.setdefault(name, str(threads))
    os.environ.setdefault("TF_NUM_INTEROP_THREADS", "1")
    # oneDNN kernels: 96 vs 110 ms per face, but 1017 vs 496 MB committed and
    # 771 vs 508 MB in use for this model; outputs differ by < 2e-7 (cosine).
    os.environ.setdefault("TF_ENABLE_ONEDNN_OPTS", "0")
    os.environ.setdefault("CUDA_VISIBLE_DEVICES", "-1")  # CPU only
    os.environ.setdefault("TF_CPP_MIN_LOG_LEVEL", "2")


def _smoke_test(report: str) -> int:
    """Check a packaged build can do its job: load both models (downloading
    whatever the model folder lacks) and run the face detector. Writes a JSON
    report; never raises, so a windowed build can't stop on an error dialog."""
    import json
    import time
    import traceback

    result: dict = {"program": "face_service", "ok": False}
    started = time.perf_counter()
    try:
        import numpy as np

        from face_service._version import __version__
        from face_service.embedder import Embedder
        from face_service.models import provide_bundled_models, recognition_model_present
        result["version"] = __version__
        result["provided"] = provide_bundled_models()
        embedder = Embedder()
        embedder.load()
        result["recognition_model"] = recognition_model_present()
        result["photo_protection"] = embedder.load_spoof_model()
        result["photo_protection_error"] = embedder.spoof_error
        result["faces_in_blank_frame"] = len(
            embedder.detect(np.zeros((480, 640, 3), np.uint8), min_confidence=0.9))
        result["ok"] = bool(result["recognition_model"] and result["photo_protection"])
    except BaseException:  # noqa: BLE001 - report everything, including SystemExit
        result["error"] = traceback.format_exc()
    result["seconds"] = round(time.perf_counter() - started, 1)
    with open(report, "w", encoding="utf-8") as handle:
        json.dump(result, handle, indent=1)
    return 0 if result["ok"] else 1


if __name__ == "__main__":
    # Absolute imports so this file also works as a frozen (PyInstaller) entry
    # point; freeze_support stops frozen children from re-running main.
    multiprocessing.freeze_support()
    _give_output_somewhere()
    _configure_threads()
    if len(sys.argv) == 3 and sys.argv[1] == "--smoke-test":
        sys.exit(_smoke_test(sys.argv[2]))
    from face_service.service import main
    main()
