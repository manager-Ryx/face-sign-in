import multiprocessing
import os


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


if __name__ == "__main__":
    # Absolute imports so this file also works as a frozen (PyInstaller) entry
    # point; freeze_support stops frozen children from re-running main.
    multiprocessing.freeze_support()
    _configure_threads()
    from face_service.service import main
    main()
