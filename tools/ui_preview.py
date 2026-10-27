"""Render every screen of the app with sample data to PNG files.

Nothing is shown on screen and nothing touches the real service, camera, or
settings. Used for design review, documentation, and the UI smoke test.

Usage:
    python -m tools.ui_preview OUTPUT_DIR [--theme light|dark|both] [--scale 1.0]
"""
from __future__ import annotations

import argparse
import os
import sys
import tempfile
import time
from dataclasses import replace
from pathlib import Path
from typing import Any

if "FACE_UNLOCK_HOME" not in os.environ:
    os.environ["FACE_UNLOCK_HOME"] = tempfile.mkdtemp(prefix="face-unlock-preview-")

from PySide6.QtCore import QObject, QTimer, Signal  # noqa: E402
from PySide6.QtGui import QColor, QPixmap  # noqa: E402
from PySide6.QtWidgets import QApplication  # noqa: E402

from face_service.config import STATE_DIR, Config  # noqa: E402
from face_service.metrics import NullMetrics  # noqa: E402
from presence_monitor.absence import AbsenceModel  # noqa: E402
from presence_monitor.monitor import PresenceMonitor  # noqa: E402

NOW = time.time()

STATUS: dict[str, Any] = {
    "ok": True, "version": "0.2.0", "pid": 1, "uptime_s": 5400.0, "ready": True,
    "not_ready": [], "warming": "ready", "config_problems": [],
    "profile": {"anchors": 64, "learned": 12},
    "camera": {"device": {"state": "ready", "name": "NearStream V02AF", "message": "Connected"},
               "activity": {"state": "idle", "hold_s": 2.2, "low_light": "enabled",
                            "last_open_ms": 640.0, "last_error": None}},
    "recognition": {"threshold": 0.5, "effective": 0.5, "anti_spoofing": False,
                    "model": "ArcFace", "strategy": "progressive", "model_loaded": True,
                    "compiled": True},
    "learning": {"enabled": True, "last": NOW - 3600 * 3},
    "credentials": True,
    "lock_screen": {"mode": "none", "display": "on", "locked": False, "routine": "in-use"},
}

INSIGHTS: dict[str, Any] = {
    "ok": True, "days": 30,
    "reliability": {"sessions": 22, "face_unlocked": 20, "rate": 0.91, "median_time_s": 1.4,
                    "recent": [{"at": NOW - 3600 * h, "face": h != 30, "reason": "match"
                                if h != 30 else "unusable"} for h in (3, 9, 26, 30, 50, 74)]},
    "attempts": {"total": 310, "by_reason": {"no_face": 240, "match": 41, "uncertain": 18,
                                             "unusable": 11}, "median_verify_ms": 320.0,
                 "median_frames": 2.0, "distance_trend": []},
    "camera": {"starts": 96, "by_condition": [
        {"lighting": "bright", "fps": "default", "n": 51, "open_ms": 642.0, "settle_ms": 96.0,
         "settle_frames": 3.0},
        {"lighting": "dim", "fps": 15, "n": 33, "open_ms": 1910.0, "settle_ms": 201.0,
         "settle_frames": 3.0}],
        "low_light": {"state": "enabled", "reason": "A lower frame rate made your face 1.6x "
                      "brighter in 3 trials", "gains": [1.6, 1.5, 1.7]},
        "hold_s": 2.2, "errors": {}},
    "presence": {"total": 400, "by_reason": {"match": 330, "no_face": 70}, "distance_trend": []},
    "learning": {"enabled": True, "anchors": 64, "learned": 12, "cap": 300, "keep_photos": False,
                 "timeline": [{"id": 70 + i, "at": NOW - 3600 * (3 + 20 * i),
                               "quality": 0.83 - i * 0.02, "source": 1,
                               "lighting": ("bright", "dim", "bright", "backlit")[i % 4]}
                              for i in range(6)],
                 "skipped": {"nearly identical to a stored sample": 9,
                             "image quality too low": 3}, "results": {"learned": 12}},
    "threshold": {"current": 0.5, "baseline": 0.5, "recommended": 0.48, "status": "tighten",
                  "explanation": "At 0.48, at least 93% of your 140 test frames (from 9 days) "
                  "would still match. This cannot measure how often someone else would be "
                  "accepted; it only shows you would still be recognised.",
                  "evidence": {"samples": 140, "days": 9, "lighting": {"bright": 90, "dim": 50},
                               "sources": {"check": 120, "unlock": 20}, "sequences": 24,
                               "missing": []}, "acceptance": {}},
    "auto_tighten": False,
    "ledger": [
        {"id": 3, "ts": NOW - 86400 * 2, "setting": "camera", "old": None, "new": "on",
         "reason": "Low-light mode turned on: a lower frame rate made your face 1.6x brighter",
         "evidence": {}, "by": "automatic", "status": "active", "result": None},
        {"id": 2, "ts": NOW - 86400 * 4, "setting": "enrollment", "old": None,
         "new": "append: 10 photos", "reason": "You added your current appearance",
         "evidence": {}, "by": "you", "status": "active", "result": None}],
    "snapshots": [{"index": 0, "ts": NOW - 86400 * 4, "label": "Before your settings change"}],
    "evaluation": {"samples": 140}, "advice": [],
    "routine": {"learned": True, "locks": 41.3, "days": 16, "needs": {"locks": 10, "days": 5},
                "enabled": True,
                "after_leaving": {"weekday-morning": [[0, 14]], "weekday-afternoon": [[0, 22],
                                                                                    [40, 75]],
                                  "weekday-evening": [[0, 9]], "weekend-afternoon": [[0, 18]]},
                "usual_returns": {"weekday": [[440, 525], [745, 800]], "weekend": [[570, 640]]},
                "caught": {"instant": 19, "quick-look": 2, "other": 1}, "scanning_share": 0.31,
                "fixed_window_s": 1800.0, "interval_s": 3.0},
}

CHECK = {"ok": True, "accepted": True, "reason": "match", "guidance": None, "threshold": 0.5,
         "frames": [{"outcome": o, "distance": d, "issue": None, "face_brightness": 96.0}
                    for o, d in (("match", 0.31), ("match", 0.29), ("uncertain", 0.53),
                                 ("match", 0.33), ("no_face", None))],
         "verify_ms": 610, "camera_open_ms": 640, "recorded": True}


class DemoClient(QObject):
    status_changed = Signal(object)

    def __init__(self, status: dict | None):
        super().__init__()
        self.status = status

    def request(self, request: dict, callback=None, *, timeout_s: float = 10.0) -> None:
        reply = {"insights": INSIGHTS, "check": CHECK}.get(request.get("cmd"), {"ok": True})
        if callback is not None:
            QTimer.singleShot(0, lambda: callback(reply))

    def refresh_status(self) -> None:
        self.status_changed.emit(self.status)

    def set_polling(self, interval_ms: int) -> None:
        pass

    def shutdown(self) -> None:
        pass


class DemoController(QObject):
    config_changed = Signal(object)
    status_changed = Signal(object)
    monitor_changed = Signal(object)
    session_event = Signal(str)

    def __init__(self, app: QApplication, theme_name: str, status: dict | None = STATUS):
        super().__init__()
        from presence_monitor.ui import theme
        self.app = app
        self.cfg = replace(Config(), threshold=0.5, presence_interval_s=300,
                           presence_absence_timeout_s=3600, adaptive_enrollment=True,
                           anti_spoofing=False, theme=theme_name, absence_learning=True,
                           unlock_idle_after_s=1800.0, unlock_idle_interval_s=3.0,
                           unlock_pause_when_display_off=False,
                           camera_device_id=r"\\?\usb#vid_0625&pid_a3a6&mi_02#6&3c4d5e6f&0&0002")
        self.config_problems: list[str] = []
        self.tokens = theme.apply_theme(app, theme_name)
        self.metrics = NullMetrics()
        self.absence = AbsenceModel(STATE_DIR / "absence-demo.json")
        self.monitor = PresenceMonitor(self.cfg, locked=lambda: False)
        self.monitor._last.at = NOW - 120
        self.monitor._last.result = "present"
        self.client = DemoClient(status)
        self.client.status_changed.connect(self.status_changed)

    def update_config(self, **changes: Any) -> tuple[bool, str]:
        self.cfg = replace(self.cfg, **changes)
        self.config_changed.emit(self.cfg)
        return True, ""

    def __getattr__(self, name: str):  # actions are no-ops in the preview
        return lambda *args, **kwargs: None


def render(output: Path, theme_name: str, scale: float) -> list[Path]:
    from presence_monitor.ui.enroll import EnrollWizard
    from presence_monitor.ui.main_window import PAGES, MainWindow
    app = QApplication.instance() or QApplication(sys.argv)
    controller = DemoController(app, theme_name)
    window = MainWindow(controller)  # type: ignore[arg-type]
    window.resize(round(1120 * scale), round(820 * scale))
    written = []
    for key, *_ in PAGES:
        window.stack.setCurrentWidget(window.pages[key])
        window.buttons[key].setChecked(True)
        page = window.pages[key]
        if hasattr(page, "load"):
            page.load()
        page.refresh()
        if key == "face":
            page._run_test()
        for _ in range(5):
            app.processEvents()
        path = output / f"{theme_name}-{key}.png"
        window.grab().save(str(path))
        written.append(path)
        content = page.widget()
        if content is not None and content.sizeHint().height() > page.viewport().height():
            content.resize(page.viewport().width(), content.sizeHint().height())
            full = output / f"{theme_name}-{key}-full.png"
            image = QPixmap(content.size())
            image.fill(QColor(controller.tokens.window))  # the page itself is transparent
            content.render(image)
            image.save(str(full))
            written.append(full)
    wizard = EnrollWizard(controller, "append", None)  # type: ignore[arg-type]
    wizard.resize(800, 760)
    for _ in range(3):
        app.processEvents()
    path = output / f"{theme_name}-setup.png"
    wizard.grab().save(str(path))
    written.append(path)
    written += _render_onboarding(app, controller, output, theme_name)
    return written


def _render_onboarding(app, controller, output: Path, theme_name: str) -> list[Path]:
    """Every first-run step, with simulated PC checks (nothing on this PC is touched)."""
    from unittest.mock import patch

    from presence_monitor.system_setup import TileState
    from presence_monitor.ui.onboarding import STEPS, OnboardingWizard, SystemChecks
    checks = SystemChecks(tile_state=lambda: TileState(False, problem="not installed"),
                          install_tile=lambda: (True, ""), autostart_state=lambda: "tasks",
                          enable_autostart=lambda: (True, ""), hello_only=lambda: False,
                          start_service=lambda: None)
    # A made-up account, so renders never show the real one.
    with patch("presence_monitor.ui.onboarding.microsoft_account",
               return_value="you@example.com"):
        wizard = OnboardingWizard(controller, None, checks=checks)  # type: ignore[arg-type]
    wizard.resize(820, 700)
    wizard._local = {"tile": checks.tile_state(), "hello": False, "autostart": "tasks"}
    written = []
    for index in range(len(STEPS)):
        wizard._go(index)
        wizard.timer.stop()
        for _ in range(3):
            app.processEvents()
        path = output / f"{theme_name}-welcome-{index + 1}.png"
        wizard.grab().save(str(path))
        written.append(path)
    # The model downloading, as it looks on the Get ready step.
    from copy import deepcopy
    original = controller.client.status
    downloading = deepcopy(original)
    downloading["recognition"].update(model_loaded=False,
                                      download={"done": 46_000_000, "total": 137_000_000})
    controller.client.status = downloading
    wizard._go(1)
    wizard.timer.stop()
    wizard._refresh_ready()
    for _ in range(3):
        app.processEvents()
    path = output / f"{theme_name}-welcome-2-downloading.png"
    wizard.grab().save(str(path))
    written.append(path)
    controller.client.status = original
    written += _render_guides(app, output, theme_name)
    return written


def _render_guides(app, output: Path, theme_name: str) -> list[Path]:
    """The how-to guides, waiting and done (no Windows setting is read)."""
    from presence_monitor.ui.guides import HelloOnlyGuide
    written = []
    for state, hello_only in (("", True), ("-done", False)):
        guide = HelloOnlyGuide(None, hello_only=lambda value=hello_only: value)
        guide.timer.stop()
        guide.adjustSize()
        for _ in range(3):
            app.processEvents()
        path = output / f"{theme_name}-guide-hello-only{state}.png"
        guide.grab().save(str(path))
        written.append(path)
        guide.done(0)
    return written


def main() -> None:
    parser = argparse.ArgumentParser()
    parser.add_argument("output", type=Path)
    parser.add_argument("--theme", default="both", choices=("light", "dark", "both"))
    parser.add_argument("--scale", type=float, default=1.0)
    args = parser.parse_args()
    args.output.mkdir(parents=True, exist_ok=True)
    themes = ("light", "dark") if args.theme == "both" else (args.theme,)
    for name in themes:
        for path in render(args.output, name, args.scale):
            print(path)


if __name__ == "__main__":
    main()
