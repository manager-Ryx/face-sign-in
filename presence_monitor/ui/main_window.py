"""Main window: a navigation pane and the app's pages."""
from __future__ import annotations

from typing import TYPE_CHECKING

from PySide6.QtCore import QByteArray, QSettings, QTimer
from PySide6.QtGui import QKeySequence, QShortcut
from PySide6.QtWidgets import (
    QButtonGroup,
    QHBoxLayout,
    QMainWindow,
    QStackedWidget,
    QVBoxLayout,
    QWidget,
)

from face_service.config import STATE_DIR

from .icons import G, app_icon
from .pages.about import AboutPage
from .pages.advanced import AdvancedPage
from .pages.face import FacePage
from .pages.home import HomePage
from .pages.learning import LearningPage
from .pages.settings import SettingsPage
from .status import summarize
from .widgets import NavButton, Pill, fade_in, label

if TYPE_CHECKING:
    from ..app import AppController

PAGES = (
    ("home", G.HOME, "Overview", HomePage),
    ("face", G.FACE, "Your face", FacePage),
    ("learning", G.CHART, "Learning", LearningPage),
    ("settings", G.SETTINGS, "Settings", SettingsPage),
    ("advanced", G.SLIDERS, "Advanced", AdvancedPage),
    ("about", G.INFO, "About and help", AboutPage),
)
NAV_TIPS = {
    "home": "Is face sign-in ready, and quick actions",
    "face": "Your face profile, a recognition test and your stored password",
    "learning": "What Face Unlock has learned and measured, with undo",
    "settings": "Auto-lock, the sign-in screen, camera and notifications",
    "advanced": "Strictness, photo protection, limits and diagnostics",
    "about": "How it works, its limits and privacy",
}
STATE_TIPS = {
    "ready": "Face sign-in and auto-lock are working.",
    "paused": "Auto-lock is paused. Face sign-in still works.",
    "attention": "Something needs your attention: see the Overview.",
    "off": "The background service isn't running, so face sign-in and auto-lock are off.",
}


def window_settings() -> QSettings:
    """Window size and position, kept with Face Unlock's other state (not the registry)."""
    return QSettings(str(STATE_DIR / "window.ini"), QSettings.Format.IniFormat)


class MainWindow(QMainWindow):
    def __init__(self, controller: AppController):
        super().__init__()
        self.controller = controller
        self.setWindowTitle("Face Unlock")
        self.setWindowIcon(app_icon())
        self.resize(1120, 780)
        self.setMinimumSize(880, 600)

        root = QWidget()
        root.setObjectName("AppRoot")
        layout = QHBoxLayout(root)
        layout.setContentsMargins(0, 0, 0, 0)
        layout.setSpacing(0)

        nav = QWidget()
        nav.setObjectName("Transparent")
        nav.setFixedWidth(250)
        nav_layout = QVBoxLayout(nav)
        nav_layout.setContentsMargins(8, 14, 8, 14)
        nav_layout.setSpacing(2)
        brand = QHBoxLayout()
        brand.setContentsMargins(12, 0, 0, 12)
        logo = label("")
        logo.setPixmap(app_icon().pixmap(22, 22))
        brand.addWidget(logo)
        brand.addSpacing(8)
        brand.addWidget(label("Face Unlock", "CardTitle"))
        brand.addStretch(1)
        nav_layout.addLayout(brand)

        self.stack = QStackedWidget()
        self.group = QButtonGroup(self)
        self.pages: dict[str, QWidget] = {}
        self.buttons: dict[str, NavButton] = {}
        for index, (key, glyph, text, factory) in enumerate(PAGES):
            page = factory(controller)
            self.pages[key] = page
            self.stack.addWidget(page)
            nav_button = NavButton(glyph, text)
            nav_button.setToolTip(f"{NAV_TIPS[key]} (Ctrl+{index + 1})")
            nav_button.clicked.connect(lambda _=False, k=key: self.open_page(k))
            self.group.addButton(nav_button, index)
            self.buttons[key] = nav_button
            if key == "about":
                nav_layout.addStretch(1)
            nav_layout.addWidget(nav_button)
            QShortcut(QKeySequence(f"Ctrl+{index + 1}"), self,
                      activated=lambda k=key: self.open_page(k))
        self.state_pill = Pill()
        state_row = QHBoxLayout()
        state_row.setContentsMargins(16, 10, 0, 0)
        state_row.addWidget(self.state_pill)
        state_row.addStretch(1)
        nav_layout.addLayout(state_row)

        layout.addWidget(nav)
        layout.addWidget(self.stack, 1)
        self.setCentralWidget(root)
        QShortcut(QKeySequence("F5"), self, activated=self._refresh_current)
        QShortcut(QKeySequence.StandardKey.Find, self, activated=self._find)
        QShortcut(QKeySequence("Ctrl+,"), self, activated=lambda: self.open_page("settings"))
        self._restore_geometry()
        controller.status_changed.connect(lambda _s: self._update_state())
        controller.monitor_changed.connect(lambda _s: self._update_state())
        self._update_state()

    def open_page(self, key: str | None) -> None:
        if key == "quit":
            self._show()
            from .dialogs import confirm_quit
            QTimer.singleShot(0, lambda: confirm_quit(self.controller, self))
            return
        if key not in self.pages:
            current = self.stack.currentWidget()
            key = next((k for k, p in self.pages.items() if p is current), "home")
        page = self.pages[key]
        changed = page is not self.stack.currentWidget()
        self.stack.setCurrentWidget(page)
        self.buttons[key].setChecked(True)
        page.refresh()
        self._show()
        if changed:
            fade_in(page, 140)

    def _show(self) -> None:
        if self.isMinimized():
            self.showNormal()
        self.show()
        self.raise_()
        self.activateWindow()

    def _refresh_current(self) -> None:
        self.controller.client.refresh_status()
        page = self.stack.currentWidget()
        if hasattr(page, "load"):
            page.load()
        page.refresh()

    def _find(self) -> None:
        """Ctrl+F: the current page's search box (Settings, if this page has none)."""
        page = self.stack.currentWidget()
        if getattr(page, "search", None) is None:
            self.open_page("settings")
            page = self.pages["settings"]
        page.search.setFocus()
        page.search.selectAll()

    def _update_state(self) -> None:
        c = self.controller
        overview = summarize(c.client.status, c.monitor.snapshot(), c.cfg, c.system_state())
        self.state_pill.set_state(overview.tray_text, {"ready": "success", "paused": "caution",
                                                       "attention": "caution",
                                                       "off": "critical"}[overview.tray_state],
                                  STATE_TIPS[overview.tray_state])

    def _restore_geometry(self) -> None:
        saved = window_settings().value("main/geometry")
        if isinstance(saved, QByteArray) and not saved.isEmpty():
            self.restoreGeometry(saved)

    def _save_geometry(self) -> None:
        try:
            STATE_DIR.mkdir(parents=True, exist_ok=True)
            settings = window_settings()
            settings.setValue("main/geometry", self.saveGeometry())
            settings.sync()
        except OSError:
            pass  # remembering the size is a comfort, never a reason to fail

    def refresh_theme(self) -> None:
        for page in self.pages.values():
            page.refresh()

    def showEvent(self, event) -> None:  # noqa: N802 - Qt API
        self.controller.main_window_visible(True)
        super().showEvent(event)

    def hideEvent(self, event) -> None:  # noqa: N802 - Qt API
        self._save_geometry()
        self.controller.main_window_visible(False)
        super().hideEvent(event)

    def closeEvent(self, event) -> None:  # noqa: N802 - Qt API
        # Closing the window keeps Face Unlock running in the notification area.
        event.ignore()
        self.hide()
