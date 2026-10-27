"""Overview: is face sign-in ready, what the camera is doing, quick actions."""
from __future__ import annotations

from typing import TYPE_CHECKING

from PySide6.QtCore import Qt, QTimer
from PySide6.QtWidgets import QGridLayout, QHBoxLayout, QMenu, QVBoxLayout, QWidget

from ..icons import G, GlyphLabel
from ..status import Tile, guidance_text, summarize
from ..widgets import (
    Card,
    Page,
    Pill,
    StatusLine,
    accent_button,
    button,
    fade_in,
    hbox,
    label,
    show_toast,
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
        self.pill.set_state(tile.pill, tile.tone, tile.detail)
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
        # When something's wrong, a report for whoever helps is one click away.
        self.hero_help = subtle_button("Copy diagnostics",
                                       lambda: controller.run_fix("diagnostics", self))
        self.hero_help.setToolTip("Copy a report for getting help (never your password, face "
                                  "data or photos)")
        buttons = QVBoxLayout()
        buttons.setSpacing(4)
        buttons.addWidget(self.hero_action)
        buttons.addWidget(self.hero_help)
        row.addLayout(buttons)
        hero.body.addLayout(row)
        self._hero_title = ""
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
        self.check_button.setToolTip("Looks for you with the camera now and restarts the "
                                     "auto-lock timer")
        self.test_button = button("Test face recognition", lambda: controller.show_main("face"))
        self.test_button.setToolTip("Shows how closely the camera image matches your face "
                                    "(Your face page)")
        self.pause_button = button("Pause auto-lock")
        self.pause_button.setToolTip("Stop locking when you step away, for a while")
        pause_menu = QMenu(self.pause_button)
        for text, seconds in (("For 15 minutes", 900), ("For 1 hour", 3600),
                              ("Until I resume", None)):
            pause_menu.addAction(text, lambda s=seconds: self._pause(s))
        self.pause_button.setMenu(pause_menu)
        self.resume_button = accent_button("Resume auto-lock", self._resume)
        self.lock_button = subtle_button("Lock now", controller.lock_now)
        self.lock_button.setToolTip("Lock Windows straight away, like Windows+L")
        actions.body.addLayout(hbox(self.check_button, self.test_button, self.pause_button,
                                    self.resume_button, None, self.lock_button, stretch_at=5))
        self.result = StatusLine()
        actions.body.addWidget(self.result)
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
        if overview.title != self._hero_title:
            if self._hero_title:
                fade_in(self.hero_icon, 260)   # the state changed: let the eye notice
            self._hero_title = overview.title
        self.hero_title.setText(overview.title)
        self.hero_message.setText(overview.message)
        if overview.action != self._action:
            self.hero_action.set_busy(False)
        self._action = overview.action
        self.hero_action.setVisible(bool(overview.action))
        self.hero_action.setText(overview.action_text or "")
        self.hero_help.setVisible(overview.tone != "success")
        self.camera.show_tile(overview.camera)
        self.recognition.show_tile(overview.recognition)
        self.autolock.show_tile(overview.autolock)
        self.learning.show_tile(overview.learning)
        paused = c.monitor.is_paused()
        self.pause_button.setVisible(c.cfg.presence_enabled and not paused)
        self.resume_button.setVisible(paused)
        self.check_button.setEnabled(c.cfg.presence_enabled)
        self.check_button.setToolTip(
            "Looks for you with the camera now and restarts the auto-lock timer"
            if c.cfg.presence_enabled else "Auto-lock is off in Settings, so there's nothing to "
            "check")

    def _hero_action(self) -> None:
        busy = {"start-service": "Starting…", "install-tile": "Waiting for Windows…",
                "prepare-model": "Trying again…"}.get(self._action or "")
        if busy:
            self.hero_action.set_busy(True, busy)   # until the status changes...
            QTimer.singleShot(45_000, lambda: self.hero_action.set_busy(False))  # ...or not
        self.controller.run_fix(self._action, self)

    def _check_presence(self) -> None:
        self.result.working("Looking for you…")
        self.check_button.set_busy(True, "Looking…")

        def done(reply) -> None:
            self.check_button.set_busy(False)
            if reply is None:
                self.result.done("critical", "Face Unlock isn't responding right now. Try again "
                                 "in a moment.")
            elif reply.get("skipped"):
                self.result.done("info", "Skipped: the camera is busy with another task. Try "
                                 "again in a moment.")
            elif reply.get("ok") and reply.get("present"):
                self.result.done("success", "You're here. The auto-lock timer has restarted.")
                self.controller.monitor.check_soon()
            else:
                self.result.done("caution", "Couldn't confirm it's you. " + (guidance_text(
                    reply.get("guidance") or reply.get("reason")) or "Look at the camera and "
                    "try again."))

        self.controller.check_presence_now(done)

    def _pause(self, seconds: float | None) -> None:
        self.controller.monitor.pause(seconds)
        self.refresh()
        text = {900: "for 15 minutes", 3600: "for an hour"}.get(seconds or 0, "until you resume")
        show_toast(self, f"Auto-lock paused {text}", "info",
                   action=("Undo", self._resume), ms=6000)

    def _resume(self) -> None:
        self.controller.monitor.resume()
        self.refresh()
        show_toast(self, "Auto-lock is back on")
