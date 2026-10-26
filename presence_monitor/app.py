"""Face Unlock tray application: presence monitor, tray icon, and windows.

Runs at sign-in (scheduled task ``FaceUnlock-Presence``). One instance per
user: starting it again just brings the existing window forward.
"""
from __future__ import annotations

import ctypes
import logging
import os
import subprocess
import sys
import threading
from dataclasses import asdict, replace
from typing import Any

from PySide6.QtCore import (
    QFileSystemWatcher,
    QLockFile,
    QObject,
    QStandardPaths,
    Qt,
    QTimer,
    Signal,
)
from PySide6.QtGui import QGuiApplication
from PySide6.QtNetwork import QLocalServer, QLocalSocket
from PySide6.QtWidgets import QApplication

from face_service.config import (
    APP_DIR,
    CONFIG_PATH,
    METRICS_DIR,
    PRESENCE_LOG_PATH,
    STATE_DIR,
    Config,
    LoadResult,
    load_config,
    save_config,
)
from face_service.logs import setup_logging
from face_service.metrics import MetricsStore
from face_service.win_events import SessionWatcher

from .absence import AbsenceModel
from .monitor import PresenceMonitor
from .ui import theme
from .ui.client import ServiceClient

log = logging.getLogger(__name__)

INSTANCE_KEY = f"FaceUnlockTray-{os.environ.get('USERNAME', 'user')}"


class AppController(QObject):
    config_changed = Signal(object)
    status_changed = Signal(object)
    monitor_changed = Signal(object)
    session_event = Signal(str)

    def __init__(self, app: QApplication):
        super().__init__()
        self.app = app
        result = load_config()
        self.cfg: Config = result.config
        self.config_problems = result.problems
        self.tokens = theme.apply_theme(app, self.cfg.theme)
        self.metrics = MetricsStore(METRICS_DIR / "presence.jsonl",
                                    retention_days=self.cfg.metrics_retention_days)
        self.absence = AbsenceModel(STATE_DIR / "absence.json")
        self.monitor = PresenceMonitor(self.cfg, absence=self.absence, metrics=self.metrics)
        self.client = ServiceClient(self)
        self.client.status_changed.connect(self.status_changed)
        self.watcher = SessionWatcher(self._on_session_event_thread)
        self.watcher.add_handler(self.monitor.on_session_event)
        self._monitor_timer = QTimer(self)
        self._monitor_timer.timeout.connect(
            lambda: self.monitor_changed.emit(self.monitor.snapshot()))
        self._main_window = None
        self._wizard = None
        self.tray = None
        self.session_event.connect(self._on_session_event)
        hints = QGuiApplication.styleHints()
        hints.colorSchemeChanged.connect(self._on_system_scheme)
        # Settings edited outside the app (by hand or a tool) are picked up, so a
        # later change made here can never overwrite them with stale values.
        self._config_watch = QFileSystemWatcher(self)
        if APP_DIR.is_dir():
            self._config_watch.addPath(str(APP_DIR))
        self._config_watch.fileChanged.connect(lambda _path: self._config_reload.start())
        self._config_watch.directoryChanged.connect(lambda _path: self._config_reload.start())
        self._config_reload = QTimer(self)
        self._config_reload.setSingleShot(True)
        self._config_reload.setInterval(400)
        self._config_reload.timeout.connect(self._reload_if_changed)
        self._watch_config_file()

    # ---- lifecycle -----------------------------------------------------------

    def start(self) -> None:
        from .ui.tray import TrayController
        threading.Thread(target=self.monitor.run, name="presence-monitor", daemon=True).start()
        self.watcher.start()
        self.tray = TrayController(self)
        self.tray.show()
        self._monitor_timer.start(2000)
        self.client.set_polling(15000)
        self.client.refresh_status()
        if self.cfg.check_for_updates:
            QTimer.singleShot(60_000, self.check_for_updates_quietly)

    def quit(self) -> None:
        """Stop presence checks and the background service, then exit."""
        log.info("quit requested from the app")
        self.monitor.stop()
        self.watcher.stop()

        def stopped(_reply: Any) -> None:
            self.client.shutdown()
            self.app.quit()

        self.client.request({"cmd": "shutdown"}, stopped, timeout_s=3.0)

    # ---- windows ---------------------------------------------------------------

    def show_main(self, page: str | None = None) -> None:
        from .ui.main_window import MainWindow
        if self._main_window is None:
            self._main_window = MainWindow(self)
        self._main_window.open_page(page)

    def main_window_visible(self, visible: bool) -> None:
        self.client.set_polling(3000 if visible else 15000)
        if visible:
            self.client.refresh_status()

    def open_setup(self, mode: str = "append") -> None:
        from .ui.enroll import EnrollWizard
        if self._wizard is not None and self._wizard.isVisible():
            self._wizard.raise_()
            self._wizard.activateWindow()
            return
        parent = self._main_window if self._main_window and self._main_window.isVisible() else None
        self._wizard = EnrollWizard(self, mode, parent)
        self._wizard.finished.connect(lambda _code: self.client.refresh_status())
        self._wizard.show()

    # ---- settings ------------------------------------------------------------

    def update_config(self, **changes: Any) -> tuple[bool, str]:
        """Validate, save, and apply a settings change."""
        candidate = replace(self.cfg, **changes)
        problems = candidate.problems()
        if problems:
            return False, problems[0]
        try:
            save_config(candidate)
        except (OSError, ValueError) as exc:
            log.exception("settings not saved")
            return False, str(exc)
        old = self.cfg
        self.cfg = candidate
        self.config_problems = []
        self.monitor.reload_config(candidate)
        self.client.request({"cmd": "reload_config"}, lambda _r: self.client.refresh_status())
        if old.theme != candidate.theme:
            self.apply_appearance()
        self.config_changed.emit(candidate)
        log.info("settings changed: %s", ", ".join(
            f"{k}={v}" for k, v in changes.items() if asdict(old).get(k) != v))
        return True, ""

    def reload_from_disk(self, result: LoadResult | None = None) -> None:
        result = result or load_config()
        self.cfg = result.config
        self.config_problems = result.problems
        self.monitor.reload_config(self.cfg)
        self.apply_appearance()
        self.config_changed.emit(self.cfg)

    def _watch_config_file(self) -> None:
        # Saving replaces the file, which can drop it from the watch list.
        if CONFIG_PATH.exists() and str(CONFIG_PATH) not in self._config_watch.files():
            self._config_watch.addPath(str(CONFIG_PATH))

    def _reload_if_changed(self) -> None:
        self._watch_config_file()
        result = load_config()
        if asdict(result.config) == asdict(self.cfg):
            return  # our own save, or nothing that matters
        log.info("settings changed outside the app; reloading")
        self.reload_from_disk(result)
        self.client.request({"cmd": "reload_config"}, lambda _r: self.client.refresh_status())

    def apply_appearance(self) -> None:
        self.tokens = theme.apply_theme(self.app, self.cfg.theme)
        for widget in self.app.allWidgets():
            refresh = getattr(widget, "refresh_theme", None)
            if callable(refresh):
                refresh()
            widget.update()
        if self.tray is not None:
            self.tray.refresh()

    def _on_system_scheme(self, _scheme: Qt.ColorScheme) -> None:
        if self.cfg.theme == "system":
            QTimer.singleShot(0, self.apply_appearance)

    # ---- session events (arrive on the watcher thread) -------------------------

    def _on_session_event_thread(self, name: str, detail: dict) -> None:
        self.metrics.record("session_event", event=name)
        self.session_event.emit(name)

    def _on_session_event(self, name: str) -> None:
        if name == "lock" and self._wizard is not None and self._wizard.isVisible():
            self._wizard.pause_for_lock()

    # ---- actions ---------------------------------------------------------------

    def check_presence_now(self, callback=None) -> None:
        self.client.request({"cmd": "presence"}, callback, timeout_s=25.0)

    def start_service(self) -> None:
        """Start the background service through its scheduled task."""
        try:
            subprocess.Popen(["schtasks", "/Run", "/TN", "FaceUnlock-Service"],
                             creationflags=subprocess.CREATE_NO_WINDOW)
        except OSError:
            log.exception("could not start the service task")
        QTimer.singleShot(4000, self.client.refresh_status)

    def lock_now(self) -> None:
        ctypes.windll.user32.LockWorkStation()

    def open_data_folder(self) -> None:
        os.startfile(str(APP_DIR))  # type: ignore[attr-defined]

    def check_for_updates_quietly(self) -> None:
        from .updater import check_latest, current_version

        def worker() -> None:
            release = check_latest(timeout=8.0)
            if release and release.is_newer_than(current_version()) and self.tray:
                self.tray.notify("Update available",
                                 f"Version {release.tag} is available from the original project. "
                                 "Installing it would replace this customised version.")

        threading.Thread(target=worker, name="update-check", daemon=True).start()


def _activate_existing() -> bool:
    socket = QLocalSocket()
    socket.connectToServer(INSTANCE_KEY)
    if socket.waitForConnected(500):
        socket.write(b"show")
        socket.waitForBytesWritten(500)
        socket.disconnectFromServer()
        return True
    return False


def main() -> None:
    setup_logging(PRESENCE_LOG_PATH, console=False)
    QGuiApplication.setHighDpiScaleFactorRoundingPolicy(
        Qt.HighDpiScaleFactorRoundingPolicy.PassThrough)
    app = QApplication(sys.argv)
    app.setApplicationName("Face Unlock")
    app.setOrganizationName("Face Unlock")
    app.setQuitOnLastWindowClosed(False)
    lock_dir = QStandardPaths.writableLocation(QStandardPaths.StandardLocation.TempLocation)
    instance_lock = QLockFile(os.path.join(lock_dir, INSTANCE_KEY + ".lock"))
    if not instance_lock.tryLock(100):
        if _activate_existing():
            return
        instance_lock.removeStaleLockFile()
        if not instance_lock.tryLock(100):
            return
    controller = AppController(app)
    from .ui.icons import app_icon
    app.setWindowIcon(app_icon())
    server = QLocalServer()
    QLocalServer.removeServer(INSTANCE_KEY)
    server.listen(INSTANCE_KEY)

    def on_connection() -> None:
        connection = server.nextPendingConnection()
        if connection is not None:
            connection.readyRead.connect(lambda: controller.show_main())

    server.newConnection.connect(on_connection)
    controller.start()
    if "--show" in sys.argv or controller.config_problems:
        controller.show_main()
    code = app.exec()
    instance_lock.unlock()
    sys.exit(code)
