"""Face Unlock tray application: presence monitor, tray icon, and windows.

Runs at sign-in (scheduled task ``FaceUnlock-Presence``). One instance per
user: starting it again just brings the existing window forward.
"""
from __future__ import annotations

import ctypes
import logging
import os
import sys
import threading
import time
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
    CREDS_PATH,
    EMBED_PATH,
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

from . import system_setup
from .absence import AbsenceModel
from .monitor import PresenceMonitor
from .notices import Notice, Notices
from .ui import theme
from .ui.client import ServiceClient

log = logging.getLogger(__name__)

INSTANCE_KEY = f"FaceUnlockTray-{os.environ.get('USERNAME', 'user')}"
APP_USER_MODEL_ID = "FaceUnlock.App"


SYSTEM_CHECK_S = 30.0
WEEKLY_CHECK_DELAY_MS = 90_000   # after a sign-in, once you've settled in
WATCHDOG_MISSES = 2          # status checks without an answer before restarting the service
WATCHDOG_GAP_S = 300.0       # at most one restart this often


class AppController(QObject):
    config_changed = Signal(object)
    status_changed = Signal(object)
    monitor_changed = Signal(object)
    session_event = Signal(str)
    tile_result = Signal(bool, str)
    notice = Signal(str, str)        # (title, message) from a worker thread, for the GUI thread

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
        self._onboarding = None
        self._system: tuple[float, dict] | None = None
        self.tray = None
        self.tile_result.connect(self._tile_done)
        self._misses = 0
        self._restarted_at = float("-inf")
        self._quitting = False
        self.client.status_changed.connect(self._watchdog)
        self.session_event.connect(self._on_session_event)
        self.notice.connect(self._notify)
        self.notices = Notices(STATE_DIR / "notices.json")
        self.client.status_changed.connect(self._celebrate)
        self._weekly_timer = QTimer(self)
        self._weekly_timer.setSingleShot(True)
        self._weekly_timer.timeout.connect(self._weekly_summary)
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
        QTimer.singleShot(1500, self._first_run)
        self._weekly_timer.start(5 * 60_000)
        if self.cfg.check_for_updates:
            QTimer.singleShot(60_000, self.check_for_updates_quietly)

    def quit(self) -> None:
        """Stop presence checks and the background service, then exit."""
        log.info("quit requested from the app")
        self._quitting = True
        self.monitor.stop()
        self.watcher.stop()

        def stopped(_reply: Any) -> None:
            self.client.shutdown()
            self.app.quit()

        self.client.request({"cmd": "shutdown"}, stopped, timeout_s=3.0)

    def _watchdog(self, status: Any) -> None:
        """If the background service stops answering, start it again (not after Quit)."""
        if status is not None:
            self._misses = 0
            return
        self._misses += 1
        now = time.monotonic()
        if (self._quitting or self._misses < WATCHDOG_MISSES
                or not system_setup.onboarding_done() or now - self._restarted_at < WATCHDOG_GAP_S):
            return
        self._restarted_at = now
        log.warning("the background service isn't answering; starting it again")
        self.start_service()

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

    def onboarding_done(self) -> bool:
        return system_setup.onboarding_done()

    def _first_run(self) -> None:
        """Open first-run setup on a new PC (not where Face Unlock was already set up)."""
        if system_setup.onboarding_done():
            return
        if EMBED_PATH.exists() and CREDS_PATH.exists():
            system_setup.mark_onboarding_done()   # set up before first-run setup existed
            return
        self.open_onboarding()

    def open_onboarding(self) -> None:
        from .ui.onboarding import OnboardingWizard
        if self._onboarding is not None and self._onboarding.isVisible():
            self._onboarding.raise_()
            self._onboarding.activateWindow()
            return
        self._onboarding = OnboardingWizard(self)
        self._onboarding.finished.connect(lambda _code: self.client.refresh_status())
        self._onboarding.show()
        self._onboarding.raise_()
        self._onboarding.activateWindow()

    def system_state(self) -> dict:
        """Quick local checks for the Overview (the registry; cached briefly)."""
        now = time.monotonic()
        if self._system is None or now - self._system[0] > SYSTEM_CHECK_S:
            self._system = (now, {"tile": system_setup.tile_state(),
                                  "hello": system_setup.hello_only_sign_in()})
        return self._system[1]

    def install_tile(self) -> None:
        """Add the sign-in tile; Windows asks for permission, so off the UI thread."""
        threading.Thread(target=lambda: self.tile_result.emit(*system_setup.install_tile()),
                         name="install-tile", daemon=True).start()

    def forget_system_state(self) -> None:
        """Check the registry again next time (a setting was just changed)."""
        self._system = None

    def _tile_done(self, ok: bool, message: str) -> None:
        self._system = None
        if self.tray is not None:
            if ok:
                self.tray.notify("Face sign-in added to the lock screen",
                                 "Try it: press Windows+L and look at your camera.")
            else:
                self.tray.notify("Face sign-in wasn't added", message + " Select to try again.",
                                 "install-tile")
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
        if name == "unlock":
            self._weekly_timer.start(WEEKLY_CHECK_DELAY_MS)

    def capture_window_opened(self, wizard) -> None:
        """A face-capture window opened elsewhere (first-run setup): like the one
        from open_setup, it must give the camera back as soon as Windows locks."""
        self._wizard = wizard

    # ---- fixes: one place for "do the thing that solves this" ----------------------

    def _window(self):
        self.show_main()
        return self._main_window

    def run_fix(self, action: str | None, parent=None, *, from_notification: bool = False) -> None:
        """Open whatever fixes ``action`` (an Overview action), from a button or a
        notification. Anything that asks Windows for permission is confirmed first
        when it comes from a notification."""
        from .ui import dialogs
        if action in (None, "open"):
            self.show_main()
        elif action == "start-service":
            self.start_service()
        elif action == "setup":
            if self.onboarding_done():
                self.open_setup("replace")
            else:
                self.open_onboarding()
        elif action == "install-tile":
            parent = parent or self._window()
            if not from_notification or dialogs.confirm(
                    parent, "Add face sign-in to your lock screen?",
                    "Windows will ask for your permission once. Your PIN and password keep "
                    "working as before.", "Add it", destructive=False):
                self.install_tile()
        elif action == "hello-settings":
            from .ui.guides import HelloOnlyGuide
            HelloOnlyGuide(parent or self._window()).exec()
            self.forget_system_state()
            self.client.refresh_status()
        elif action == "password":
            dialogs.PasswordDialog(self, parent or self._window()).exec()
            self.client.refresh_status()
        elif action == "camera-moved":
            dialogs.confirm_moved_camera(self, parent or self._window())
        elif action in ("camera-settings", "settings"):
            self.show_main("settings")
        elif action == "prepare-model":
            self.client.request({"cmd": "prepare_model"}, lambda _r: self.client.refresh_status())
        elif action == "diagnostics":
            self.open_diagnostics(parent)
        else:
            self.show_main()

    def open_diagnostics(self, parent=None) -> None:
        from .ui.dialogs import DiagnosticsDialog
        DiagnosticsDialog(self, parent or self._window()).exec()

    # ---- cheerful notices -------------------------------------------------------

    def _celebrate(self, status: Any) -> None:
        if not self.cfg.notifications or self.tray is None:
            return
        notice = self.notices.on_status(status, time.time())
        if notice is not None:
            self._show_notice(notice)

    def _weekly_summary(self) -> None:
        if not self.cfg.notifications or not self.notices.weekly_due(time.time()):
            return

        def counted(reply: Any) -> None:
            count = ((reply or {}).get("reliability") or {}).get("face_sign_ins")
            if isinstance(count, int):
                notice = self.notices.weekly(count, time.time())
                if notice is not None:
                    self._show_notice(notice)

        self.client.request({"cmd": "insights", "days": 7}, counted, timeout_s=20.0)

    def _show_notice(self, notice: Notice) -> None:
        if self.tray is not None:
            self.tray.notify(notice.title, notice.message, "open")

    # ---- actions ---------------------------------------------------------------

    def check_presence_now(self, callback=None) -> None:
        self.client.request({"cmd": "presence"}, callback, timeout_s=25.0)

    def start_service(self) -> None:
        """Start the background service (its scheduled task, or the program itself)."""
        try:
            system_setup.start_service()
        except OSError:
            log.exception("could not start the service")
        QTimer.singleShot(4000, self.client.refresh_status)

    def lock_now(self) -> None:
        ctypes.windll.user32.LockWorkStation()

    def open_data_folder(self) -> None:
        os.startfile(str(APP_DIR))  # type: ignore[attr-defined]

    def check_for_updates_quietly(self) -> None:
        from .updater import check_latest, current_version

        def worker() -> None:
            release = check_latest(timeout=8.0)
            if release and release.is_newer_than(current_version()):
                # Qt widgets and pixmaps belong to the GUI thread: hand the notice over.
                self.notice.emit("Update available",
                                 f"Version {release.tag} is available from the project's "
                                 "Releases page.")

        threading.Thread(target=worker, name="update-check", daemon=True).start()

    def _notify(self, title: str, message: str) -> None:
        if self.tray is not None:
            self.tray.notify(title, message)


def _activate_existing(message: bytes = b"show") -> bool:
    socket = QLocalSocket()
    socket.connectToServer(INSTANCE_KEY)
    if socket.waitForConnected(500):
        socket.write(message)
        socket.waitForBytesWritten(500)
        socket.disconnectFromServer()
        return True
    return False


def _set_app_id() -> None:
    """Show Face Unlock's own icon in the taskbar rather than Python's.

    Without an explicit app ID, Windows groups the windows under pythonw.exe
    and uses its icon."""
    try:
        ctypes.windll.shell32.SetCurrentProcessExplicitAppUserModelID(APP_USER_MODEL_ID)
    except (AttributeError, OSError):
        log.debug("could not set the taskbar app ID")


def main() -> None:
    setup_logging(PRESENCE_LOG_PATH, console=False)
    _set_app_id()
    QGuiApplication.setHighDpiScaleFactorRoundingPolicy(
        Qt.HighDpiScaleFactorRoundingPolicy.PassThrough)
    app = QApplication(sys.argv)
    app.setApplicationName("Face Unlock")
    app.setOrganizationName("Face Unlock")
    app.setQuitOnLastWindowClosed(False)
    lock_dir = QStandardPaths.writableLocation(QStandardPaths.StandardLocation.TempLocation)
    instance_lock = QLockFile(os.path.join(lock_dir, INSTANCE_KEY + ".lock"))
    if not instance_lock.tryLock(100):
        if _activate_existing(b"welcome" if "--welcome" in sys.argv else b"show"):
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
            def received() -> None:
                if bytes(connection.readAll().data()).startswith(b"welcome")                         and not controller.onboarding_done():
                    controller.open_onboarding()
                else:
                    controller.show_main()
            connection.readyRead.connect(received)

    server.newConnection.connect(on_connection)
    controller.start()
    if "--welcome" in sys.argv:
        controller.open_onboarding()
    elif "--show" in sys.argv or controller.config_problems:
        controller.show_main()
    code = app.exec()
    instance_lock.unlock()
    sys.exit(code)
