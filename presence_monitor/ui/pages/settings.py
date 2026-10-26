"""Everyday settings, in plain English."""
from __future__ import annotations

from typing import TYPE_CHECKING, Any

from PySide6.QtWidgets import QComboBox, QDoubleSpinBox, QSpinBox

from ..icons import G
from ..status import duration
from ..widgets import Banner, Page, SettingRow, SettingsGroup, ToggleSwitch, button

if TYPE_CHECKING:
    from ...app import AppController


class Bindings:
    """Keeps controls in sync with the configuration and saves edits."""

    def __init__(self, controller: AppController, page: Page, banner: Banner):
        self.controller = controller
        self.page = page
        self.banner = banner
        self._sync: list = []

    def save(self, **changes: Any) -> None:
        ok, problem = self.controller.update_config(**changes)
        if ok:
            self.banner.hide()
        else:
            self.banner.show_message("critical", "That setting wasn't saved", problem)
            self.refresh()

    def toggle(self, key: str, *, invert: bool = False) -> ToggleSwitch:
        switch = ToggleSwitch()
        switch.toggled.connect(lambda on: self.save(**{key: (not on) if invert else on}))

        def sync(cfg) -> None:
            switch.blockSignals(True)
            switch.setChecked((not getattr(cfg, key)) if invert else bool(getattr(cfg, key)))
            switch.blockSignals(False)
        self._sync.append(sync)
        return switch

    def choice(self, key: str, options: list[tuple[str, Any]],
               describe=None) -> QComboBox:
        combo = QComboBox()
        combo.setFixedWidth(240)  # one width for every choice, so right edges line up

        def sync(cfg) -> None:
            value = getattr(cfg, key)
            combo.blockSignals(True)
            combo.clear()
            known = [v for _, v in options]
            for text, option in options:
                combo.addItem(text, option)
            if value not in known:
                combo.addItem(describe(value) if describe else str(value), value)
            combo.setCurrentIndex(combo.findData(value))
            combo.blockSignals(False)
        combo.activated.connect(lambda _i: self.save(**{key: combo.currentData()}))
        self._sync.append(sync)
        return combo

    def number(self, key: str, minimum: float, maximum: float, *, step: float = 1,
               decimals: int = 0, suffix: str = "") -> QSpinBox | QDoubleSpinBox:
        spin: QSpinBox | QDoubleSpinBox
        if decimals:
            spin = QDoubleSpinBox()
            spin.setDecimals(decimals)
        else:
            spin = QSpinBox()
        spin.setRange(minimum, maximum)
        spin.setSingleStep(step)
        spin.setSuffix(suffix)
        spin.setFixedWidth(140)
        spin.setKeyboardTracking(False)

        def sync(cfg) -> None:
            value = getattr(cfg, key)
            spin.blockSignals(True)
            spin.setValue(float(value) if decimals else int(round(value)))
            spin.blockSignals(False)
        spin.valueChanged.connect(lambda v: self.save(**{key: v}))
        self._sync.append(sync)
        return spin

    def refresh(self) -> None:
        for sync in self._sync:
            sync(self.controller.cfg)


class SettingsPage(Page):
    def __init__(self, controller: AppController):
        super().__init__("Settings")
        self.controller = controller
        self.banner = self.add(Banner())
        b = self.bindings = Bindings(controller, self, self.banner)

        self.section("Auto-lock when you leave")
        group = self.add(SettingsGroup())
        group.add(SettingRow(G.LOCK, "Lock my PC when I'm away",
                             "Your PC locks after you've been away for a while. Running apps "
                             "keep running.", b.toggle("presence_enabled")))
        group.add(SettingRow(G.CLOCK, "Lock after about",
                             "Measured from the last time your face was seen.",
                             b.choice("presence_absence_timeout_s",
                                      [(duration(s), s) for s in (900, 1800, 2700, 3600, 5400,
                                                                  7200, 10800)],
                                      lambda s: duration(s))))
        group.add(SettingRow(G.SCAN, "Check that I'm here every",
                             "Each check turns the camera on for about a second, then off "
                             "again. Locking can happen up to one interval later than the time "
                             "above.",
                             b.choice("presence_interval_s",
                                      [(duration(s), s) for s in (60, 120, 300, 600)],
                                      lambda s: duration(s))))
        group.add(SettingRow(G.PERSON, "Who counts as being here",
                             "\"Only me\" also locks when someone else sits at your PC.",
                             b.choice("presence_mode", [("Only me", "recognition"),
                                                        ("Anyone in front of the camera",
                                                         "detection")])))
        group.add(SettingRow(G.PAUSE, "Skip checks while Windows is locked",
                             "The sign-in screen already looks for you, so the extra checks "
                             "aren't needed.", b.toggle("presence_pause_while_locked")))

        self.section("Sign-in screen")
        group = self.add(SettingsGroup())
        group.add(SettingRow(G.CALENDAR_CLOCK, "Learn when I usually come back",
                             "Scans continuously when you're likely to be back soon – right after "
                             "you leave, and around the times you usually return – and takes "
                             "quick looks the rest of the time. Uses only when you lock and sign "
                             "in, kept as counts on this PC.", b.toggle("unlock_learn_routine")))
        group.add(SettingRow(G.SCAN, "Keep looking for me for",
                             "The camera scans continuously this long after you leave, so you're "
                             "signed in the moment you sit down; after that it takes quick looks. "
                             "With learning on, this applies until your routine is known (about "
                             "a week of normal use).",
                             b.choice("unlock_idle_after_s",
                                      [(duration(s), float(s)) for s in
                                       (60, 300, 900, 1800, 3600, 7200)],
                                      lambda s: duration(s))))
        group.add(SettingRow(G.STOPWATCH, "Then take a quick look every",
                             "Pressing a key or clicking the Face Unlock tile always starts a "
                             "scan straight away.",
                             b.choice("unlock_idle_interval_s",
                                      [(f"{s} seconds", float(s)) for s in (3, 5, 10, 15, 20)],
                                      lambda s: f"{s:g} seconds")))
        group.add(SettingRow(G.MOON, "Keep the camera off while the screen is off",
                             "When the lock screen turns the display off, the camera stays off "
                             "until the screen wakes.", b.toggle("unlock_pause_when_display_off")))

        self.section("Learning")
        group = self.add(SettingsGroup())
        group.add(SettingRow(G.BULB, "Learn from successful face sign-ins",
                             "Keeps up with gradual changes such as a beard or new glasses. "
                             "Only sign-ins Windows has confirmed are used.",
                             b.toggle("adaptive_enrollment")))
        group.add(SettingRow(G.FOLDER, "Also keep a small photo of each learned sample",
                             "Off: only a numeric face signature is stored, never a photo. On: "
                             "a cropped face photo is kept too (up to the limit in Advanced), "
                             "so you can see what was learned.",
                             b.toggle("adaptive_keep_photos")))

        self.section("Camera")
        group = self.add(SettingsGroup())
        self.camera_row = SettingRow(G.CAMERA, "Camera", "", button("Choose camera…",
                                                                     self._choose_camera))
        group.add(self.camera_row)

        self.section("Appearance and notifications")
        group = self.add(SettingsGroup())
        group.add(SettingRow(G.SUN, "Theme", "",
                             b.choice("theme", [("Use my Windows setting", "system"),
                                                ("Light", "light"), ("Dark", "dark")])))
        group.add(SettingRow(G.INFO, "Show notifications",
                             "For example when your camera is disconnected.",
                             b.toggle("notifications")))
        self.finish()
        controller.config_changed.connect(lambda _c: self.refresh())
        controller.status_changed.connect(lambda _s: self._camera_text())

    def refresh(self) -> None:
        self.bindings.refresh()
        self._camera_text()

    def _camera_text(self) -> None:
        status = self.controller.client.status or {}
        device = (status.get("camera") or {}).get("device") or {}
        name = device.get("name") or "Not chosen"
        state = {"ready": "Connected", "missing": "Not connected", "moved": "On a different USB port",
                 "ambiguous": "Several matching cameras", "unpinned": "No camera chosen"}.get(
            device.get("state"), device.get("message", "Unknown"))
        self.camera_row.title.setText(f"Camera: {name}")
        self.camera_row.set_description(
            f"{state}. Only this camera is used; Face Unlock never switches to another one "
            "(such as a virtual camera) on its own.")

    def _choose_camera(self) -> None:
        from ..dialogs import CameraDialog
        CameraDialog(self.controller, self).exec()
