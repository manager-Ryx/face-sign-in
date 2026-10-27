import os
import sys


def _smoke_test(report: str) -> int:
    """Check a packaged build can start: import everything the app and its setup
    windows need, and build its windows off screen. Writes a JSON report; never
    raises, so a windowed build can't stop on an error dialog."""
    import json
    import traceback

    result: dict = {"program": "face_unlock_tray", "ok": False}
    try:
        os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")
        import cv2
        import numpy
        from PySide6.QtWidgets import QApplication

        from face_service._version import __version__
        from presence_monitor import app
        from presence_monitor.ui import dialogs, enroll, main_window, onboarding  # noqa: F401
        qt = QApplication.instance() or QApplication([])
        controller = app.AppController(qt)
        windows = [main_window.MainWindow(controller), onboarding.OnboardingWizard(controller)]
        result.update(version=__version__, numpy=numpy.__version__, opencv=cv2.__version__,
                      windows=len(windows), ok=True)
    except BaseException:  # noqa: BLE001 - report everything, including SystemExit
        result["error"] = traceback.format_exc()
    with open(report, "w", encoding="utf-8") as handle:
        json.dump(result, handle, indent=1)
    return 0 if result["ok"] else 1


if __name__ == "__main__":
    # A windowed (packaged) program has no console; give anything that prints
    # somewhere harmless to write.
    for name in ("stdout", "stderr"):
        if getattr(sys, name) is None:
            setattr(sys, name, open(os.devnull, "w", encoding="utf-8"))  # noqa: SIM115
    # numpy's BLAS would otherwise reserve a buffer per CPU core (hundreds of MB
    # of committed memory) for the tray's tiny arrays.
    os.environ.setdefault("OPENBLAS_NUM_THREADS", "1")
    if len(sys.argv) == 3 and sys.argv[1] == "--smoke-test":
        sys.exit(_smoke_test(sys.argv[2]))
    # Absolute import so this also works as a frozen (PyInstaller) entry point.
    from presence_monitor.app import main
    main()
