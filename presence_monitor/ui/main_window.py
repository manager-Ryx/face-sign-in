"""Main window: a navigation pane and the app's pages."""
from __future__ import annotations

from typing import TYPE_CHECKING

from PySide6.QtCore import QTimer
from PySide6.QtGui import QKeySequence, QShortcut
from PySide6.QtWidgets import (
    QButtonGroup,
    QHBoxLayout,
    QMainWindow,
    QStackedWidget,
    QVBoxLayout,
    QWidget,
)

from .icons import G, app_icon
from .pages.about import AboutPage
from .pages.advanced import AdvancedPage
from .pages.face import FacePage
from .pages.home import HomePage
from .pages.learning import LearningPage
from .pages.settings import SettingsPage
from .status import summarize
from .widgets import NavButton, Pill, label

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
        self.stack.setCurrentWidget(page)
        self.buttons[key].setChecked(True)
        page.refresh()
        self._show()

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

    def _update_state(self) -> None:
        c = self.controller
        overview = summarize(c.client.status, c.monitor.snapshot(), c.cfg)
        self.state_pill.set_state(overview.tray_text, {"ready": "success", "paused": "caution",
                                                       "attention": "caution",
                                                       "off": "critical"}[overview.tray_state])

    def refresh_theme(self) -> None:
        for page in self.pages.values():
            page.refresh()

    def showEvent(self, event) -> None:  # noqa: N802 - Qt API
        self.controller.main_window_visible(True)
        super().showEvent(event)

    def hideEvent(self, event) -> None:  # noqa: N802 - Qt API
        self.controller.main_window_visible(False)
        super().hideEvent(event)

    def closeEvent(self, event) -> None:  # noqa: N802 - Qt API
        # Closing the window keeps Face Unlock running in the notification area.
        event.ignore()
        self.hide()
