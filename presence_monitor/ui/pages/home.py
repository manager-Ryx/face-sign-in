"""Overview: is face sign-in ready, what the camera is doing, quick actions."""
from __future__ import annotations

from typing import TYPE_CHECKING

from PySide6.QtCore import Qt
from PySide6.QtWidgets import QGridLayout, QHBoxLayout, QMenu, QVBoxLayout, QWidget

from ..icons import G, GlyphLabel
from ..status import Tile, guidance_text, summarize
from ..widgets import (
    Card,
    Page,
    Pill,
    ProgressRing,
    accent_button,
    button,
    hbox,
    label,
    subtle_button,
)

if TYPE_CHECKING:
    from ...app import AppController


class StatusTile(Card):
    def __init__(self, glyph: str, heading: str):
        super().__init__(padding=(18, 16), spacing=6)
        top = QHBoxLayout()
        top.setSpacing(10)
        self.icon = GlyphLabel(glyph, 18, tone="accent")
        top.addWidget(self.icon)
        top.addWidget(label(heading, "Secondary"), 1)
        self.pill = Pill()
        top.addWidget(self.pill)
        self.body.addLayout(top)
        self.value = label("", "CardTitle", wrap=True)
        self.detail = label("", "Secondary", wrap=True)
        self.body.addWidget(self.value)
        self.body.addWidget(self.detail)
        self.body.addStretch(1)

    def show_tile(self, tile: Tile) -> None:
        self.value.setText(tile.value)
        self.detail.setText(tile.detail)
        self.pill.set_state(tile.pill, tile.tone if tile.tone != "accent" else "accent")
        self.pill.setVisible(bool(tile.pill))
        self.setAccessibleName(f"{tile.value}. {tile.detail}")


class HomePage(Page):
    def __init__(self, controller: AppController):
        super().__init__("Overview")
        self.controller = controller

        hero = Card(padding=(24, 20), spacing=8)
        row = QHBoxLayout()
        row.setSpacing(18)
        self.hero_icon = GlyphLabel(G.DONE, 34, tone="success")
        row.addWidget(self.hero_icon, 0, Qt.AlignmentFlag.AlignTop)
        text = QVBoxLayout()
        text.setSpacing(4)
        self.hero_title = label("", "HeroTitle", wrap=True)
        self.hero_message = label("", "Body2", wrap=True)
        text.addWidget(self.hero_title)
        text.addWidget(self.hero_message)
        row.addLayout(text, 1)
        self.hero_action = accent_button("", self._hero_action)
        row.addWidget(self.hero_action, 0, Qt.AlignmentFlag.AlignVCenter)
        hero.body.addLayout(row)
        self.add(hero)
        self._action: str | None = None

        grid = QGridLayout()
        grid.setSpacing(12)
        self.camera = StatusTile(G.CAMERA, "Camera")
        self.recognition = StatusTile(G.FACE, "Face recognition")
        self.autolock = StatusTile(G.LOCK, "Auto-lock when you leave")
        self.learning = StatusTile(G.CHART, "Learning")
        for index, tile in enumerate((self.camera, self.recognition, self.autolock,
                                      self.learning)):
            grid.addWidget(tile, index // 2, index % 2)
        grid_host = QWidget()
        grid_host.setObjectName("Transparent")
        grid_host.setLayout(grid)
        grid.setContentsMargins(0, 0, 0, 0)
        self.add(grid_host)

        self.section("Quick actions")
        actions = Card(padding=(16, 14))
        self.check_button = button("Check that I'm here", self._check_presence)
        self.test_button = button("Test face recognition", lambda: controller.show_main("face"))
        self.pause_button = button("Pause auto-lock")
        pause_menu = QMenu(self.pause_button)
        for text, seconds in (("For 15 minutes", 900), ("For 1 hour", 3600),
                              ("Until I resume", None)):
            pause_menu.addAction(text, lambda s=seconds: self._pause(s))
        self.pause_button.setMenu(pause_menu)
        self.resume_button = accent_button("Resume auto-lock", self._resume)
        self.lock_button = subtle_button("Lock now", controller.lock_now)
        actions.body.addLayout(hbox(self.check_button, self.test_button, self.pause_button,
                                    self.resume_button, None, self.lock_button, stretch_at=5))
        self.busy = ProgressRing(18)
        self.busy.hide()
        self.result = label("", "Secondary", wrap=True)
        self.result_row = QWidget()
        self.result_row.setObjectName("Transparent")
        self.result_row.setLayout(hbox(self.busy, self.result, stretch_at=2))
        self.result_row.hide()
        actions.body.addWidget(self.result_row)
        self.add(actions)

        self.section("Good to know")
        notes = Card(padding=(20, 14), spacing=6)
        for glyph, text in (
                (G.POWER, "After Windows restarts, sign in once with your PIN or password; "
                          "face sign-in works from the next lock screen."),
                (G.KEY, "Your PIN and password always keep working on the sign-in screen."),
                (G.EYE_OFF, "Photos and measurements stay on this PC. Nothing is uploaded.")):
            notes.body.addLayout(hbox(GlyphLabel(glyph, 16, tone="muted"),
                                      label(text, "Body2", wrap=True), spacing=10))
        self.add(notes)
        self.finish()

        controller.status_changed.connect(lambda _s: self.refresh())
        controller.monitor_changed.connect(lambda _s: self.refresh())
        controller.config_changed.connect(lambda _c: self.refresh())

    def refresh(self) -> None:
        c = self.controller
        overview = summarize(c.client.status, c.monitor.snapshot(), c.cfg, c.system_state())
        glyph = {"success": G.DONE, "caution": G.WARNING, "critical": G.ERROR}.get(
            overview.tone, G.INFO)
        self.hero_icon.set_glyph(glyph, overview.tone)
        self.hero_title.setText(overview.title)
        self.hero_message.setText(overview.message)
        self._action = overview.action
        self.hero_action.setVisible(bool(overview.action))
        self.hero_action.setText(overview.action_text or "")
        self.camera.show_tile(overview.camera)
        self.recognition.show_tile(overview.recognition)
        self.autolock.show_tile(overview.autolock)
        self.learning.show_tile(overview.learning)
        paused = c.monitor.is_paused()
        self.pause_button.setVisible(c.cfg.presence_enabled and not paused)
        self.resume_button.setVisible(paused)
        self.check_button.setEnabled(c.cfg.presence_enabled)

    def _hero_action(self) -> None:
        c = self.controller
        if self._action == "start-service":
            c.start_service()
        elif self._action == "setup":
            if c.onboarding_done():
                c.open_setup("replace")
            else:
                c.open_onboarding()
        elif self._action == "install-tile":
            c.install_tile()
        elif self._action == "hello-settings":
            import os
            os.startfile("ms-settings:signinoptions")  # type: ignore[attr-defined]
        elif self._action == "password":
            from ..dialogs import PasswordDialog
            PasswordDialog(c, self).exec()
            c.client.refresh_status()
        elif self._action == "camera-moved":
            from ..dialogs import confirm_moved_camera
            confirm_moved_camera(c, self)
        elif self._action in ("camera-settings", "settings"):
            c.show_main("settings")

    def _check_presence(self) -> None:
        self.result_row.show()
        self.busy.show()
        self.result.setText("Looking for you…")
        self.check_button.setEnabled(False)

        def done(reply) -> None:
            self.busy.hide()
            self.check_button.setEnabled(True)
            if reply is None:
                self.result.setText("Face Unlock isn't responding right now.")
            elif reply.get("skipped"):
                self.result.setText("Skipped: the camera is busy with another task.")
            elif reply.get("ok") and reply.get("present"):
                self.result.setText("You're here. The auto-lock timer has restarted.")
                self.controller.monitor.check_soon()
            else:
                self.result.setText("Couldn't confirm it's you. " + guidance_text(
                    reply.get("guidance") or reply.get("reason")))

        self.controller.check_presence_now(done)

    def _pause(self, seconds: float | None) -> None:
        self.controller.monitor.pause(seconds)
        self.refresh()

    def _resume(self) -> None:
        self.controller.monitor.resume()
        self.refresh()
