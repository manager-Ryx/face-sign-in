"""Notification-area icon, its menu, and desktop notifications."""
from __future__ import annotations

from typing import TYPE_CHECKING

from PySide6.QtGui import QAction
from PySide6.QtWidgets import QMenu, QSystemTrayIcon

from . import theme
from .icons import G, glyph_icon, tray_icon
from .status import Overview, summarize

if TYPE_CHECKING:
    from ..app import AppController


class TrayController:
    def __init__(self, controller: AppController):
        self.controller = controller
        self.icon = QSystemTrayIcon(tray_icon("off"))
        self.icon.setToolTip("Face Unlock")
        self.icon.activated.connect(self._activated)
        self.menu = QMenu()
        self._build_menu()
        self.icon.setContextMenu(self.menu)
        self._last_overview: Overview | None = None
        self._notified: set[str] = set()
        controller.status_changed.connect(self._update)
        controller.monitor_changed.connect(lambda _snap: self._update(controller.client.status))
        controller.config_changed.connect(lambda _cfg: self._update(controller.client.status))

    def _action(self, glyph: str, text: str, slot) -> QAction:
        action = QAction(glyph_icon(glyph), text, self.menu)
        action.triggered.connect(slot)
        self.menu.addAction(action)
        return action

    def _build_menu(self) -> None:
        c = self.controller
        self.header = QAction("Face Unlock", self.menu)
        self.header.setEnabled(False)
        self.menu.addAction(self.header)
        self.menu.addSeparator()
        self._action(G.HOME, "Open Face Unlock", lambda: c.show_main())
        self._action(G.SCAN, "Check that I'm here now", lambda: c.check_presence_now(
            lambda reply: self._presence_result(reply)))
        self.pause_menu = QMenu("Pause auto-lock", self.menu)
        self.pause_menu.setIcon(glyph_icon(G.PAUSE))
        for text, seconds in (("For 15 minutes", 900), ("For 1 hour", 3600),
                              ("Until I resume", None)):
            action = QAction(text, self.pause_menu)
            action.triggered.connect(lambda _=False, s=seconds: c.monitor.pause(s))
            self.pause_menu.addAction(action)
        self.pause_action = self.menu.addMenu(self.pause_menu)
        self.resume_action = self._action(G.PLAY, "Resume auto-lock", c.monitor.resume)
        self.menu.addSeparator()
        self._action(G.FACE, "Test face recognition", lambda: c.show_main("face"))
        self._action(G.ADD, "Add my current appearance…", lambda: c.open_setup("append"))
        self._action(G.SETTINGS, "Settings", lambda: c.show_main("settings"))
        self.menu.addSeparator()
        self._action(G.POWER, "Quit Face Unlock…", lambda: c.show_main("quit"))
        self.menu.aboutToShow.connect(lambda: self._update(c.client.status))

    def show(self) -> None:
        self.icon.show()

    def refresh(self) -> None:
        """Rebuild menu icons after a theme change."""
        self.menu.clear()
        self._build_menu()
        self._update(self.controller.client.status)

    def _activated(self, reason: QSystemTrayIcon.ActivationReason) -> None:
        if reason in (QSystemTrayIcon.ActivationReason.Trigger,
                      QSystemTrayIcon.ActivationReason.DoubleClick):
            self.controller.show_main()

    def _update(self, status) -> None:
        c = self.controller
        overview = summarize(status, c.monitor.snapshot(), c.cfg)
        self.icon.setIcon(tray_icon(overview.tray_state))
        self.icon.setToolTip(f"Face Unlock — {overview.tray_text}")
        self.header.setText(overview.tray_text)
        paused = c.monitor.is_paused()
        self.pause_action.setVisible(not paused and c.cfg.presence_enabled)
        self.resume_action.setVisible(paused)
        previous = self._last_overview
        self._last_overview = overview
        if c.cfg.notifications and previous is not None and overview.alert_key and \
                overview.alert_key != previous.alert_key and overview.alert_key not in self._notified:
            self._notified.add(overview.alert_key)
            self.notify(overview.title, overview.message)
        if overview.alert_key is None:
            self._notified.clear()

    def _presence_result(self, reply) -> None:
        if reply is None:
            self.notify("Presence check", "Face Unlock isn't responding right now.")
        elif reply.get("skipped"):
            self.notify("Presence check", "Skipped: the camera is busy with another task.")
        elif reply.get("ok") and reply.get("present"):
            self.notify("Presence check", "You're here. The auto-lock timer has restarted.")
        else:
            from .status import guidance_text
            self.notify("Presence check", "Couldn't confirm it's you. "
                        + guidance_text(reply.get("guidance") or reply.get("reason")))

    def notify(self, title: str, message: str) -> None:
        """Desktop notification (results you asked for are always shown)."""
        if self.controller.cfg.notifications or title == "Presence check":
            self.icon.showMessage(title, message, glyph_icon(G.FACE, theme.current().text, 32),
                                  6000)
