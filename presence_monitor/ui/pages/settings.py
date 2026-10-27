"""Everyday settings, in plain English."""
from __future__ import annotations

from dataclasses import MISSING, fields
from typing import TYPE_CHECKING, Any

from PySide6.QtWidgets import QComboBox, QDoubleSpinBox, QHBoxLayout, QSpinBox, QWidget

from face_service.config import Config

from ..icons import G
from ..status import duration
from ..widgets import (
    Banner,
    Page,
    SettingRow,
    SettingsGroup,
    ToggleSwitch,
    button,
    subtle_button,
)

DEFAULTS = {f.name: f.default for f in fields(Config) if f.default is not MISSING}


def row_of(widget: QWidget | None) -> SettingRow | None:
    """The setting row a control sits in, if any."""
    while widget is not None and not isinstance(widget, SettingRow):
        widget = widget.parentWidget()
    return widget

if TYPE_CHECKING:
    from ...app import AppController


class Bindings:
    """Keeps controls in sync with the configuration and saves edits.

    Every saved change is confirmed with a tick beside its control, and a setting
    that differs from its default offers "back to the default".
    """

    def __init__(self, controller: AppController, page: Page, banner: Banner):
        self.controller = controller
        self.page = page
        self.banner = banner
        self._sync: list = []
        self._resets: set[str] = set()

    def save(self, source: QWidget | None = None, **changes: Any) -> bool:
        ok, problem = self.controller.update_config(**changes)
        if ok:
            self.banner.hide()
            row = row_of(source)
            if row is not None:
                row.flash_saved()
        else:
            self.banner.show_message("critical", "That setting wasn't saved", problem)
            self.refresh()
        return ok

    def _offer_reset(self, key: str, control: QWidget, current: Any, default_text: str) -> None:
        """Show "back to the default" on the control's row while it isn't the default."""
        row = row_of(control)
        if row is None or key not in DEFAULTS:
            return
        if key not in self._resets:
            self._resets.add(key)
            row.reset_button.clicked.connect(lambda: self.save(control, **{key: DEFAULTS[key]}))
        row.show_reset(None if current == DEFAULTS[key] else default_text)

    def toggle(self, key: str, *, invert: bool = False) -> ToggleSwitch:
        switch = ToggleSwitch()
        switch.toggled.connect(lambda on: self.save(switch, **{key: (not on) if invert else on}))

        def sync(cfg) -> None:
            switch.blockSignals(True)
            switch.setChecked((not getattr(cfg, key)) if invert else bool(getattr(cfg, key)))
            switch.blockSignals(False)
            default = DEFAULTS.get(key)
            self._offer_reset(key, switch, getattr(cfg, key),
                              "On" if bool(default) != invert else "Off")
        self._sync.append(sync)
        return switch

    def choice(self, key: str, options: list[tuple[str, Any]],
               describe=None) -> QComboBox:
        combo = QComboBox()
        combo.setFixedWidth(240)  # one width for every choice, so right edges line up
        default = DEFAULTS.get(key)

        def text_for(text: str, option: Any) -> str:
            # The default is marked, unless the wording already recommends it.
            if option == default and "recommended" not in text:
                return f"{text} (default)"
            return text

        def sync(cfg) -> None:
            value = getattr(cfg, key)
            combo.blockSignals(True)
            combo.clear()
            known = [v for _, v in options]
            for text, option in options:
                combo.addItem(text_for(text, option), option)
            if value not in known:
                combo.addItem(describe(value) if describe else str(value), value)
            combo.setCurrentIndex(combo.findData(value))
            combo.blockSignals(False)
            default_text = next((t for t, o in options if o == default), None)
            if default_text is None and default is not None:
                default_text = describe(default) if describe else str(default)
            self._offer_reset(key, combo, value, default_text or "")
        combo.activated.connect(lambda _i: self.save(combo, **{key: combo.currentData()}))
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
            default = DEFAULTS.get(key)
            if default is not None:
                self._offer_reset(key, spin, value,
                                  f"{default:.{decimals}f}{suffix}" if decimals
                                  else f"{default:g}{suffix}")
        spin.valueChanged.connect(lambda v: self.save(spin, **{key: v}))
        self._sync.append(sync)
        return spin

    def refresh(self) -> None:
        for sync in self._sync:
            sync(self.controller.cfg)


class SettingsPage(Page):
    def __init__(self, controller: AppController):
        super().__init__("Settings", "Changes are saved as you make them. A tick shows each "
                         "one was saved; \u21b6 puts a setting back to its default.")
        self.controller = controller
        self.add_search()
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
                                      lambda s: duration(s) if s else "After missed checks")))
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
                                                         "detection")]),
                             explain="\"Only me\" recognises your face at each check, so "
                             "the PC also locks if someone else sits down. \"Anyone\" only "
                             "looks for a face: a little faster and more forgiving in poor "
                             "light, but anyone keeps it unlocked."))
        group.add(SettingRow(G.PAUSE, "Skip checks while Windows is locked",
                             "The sign-in screen already looks for you, so the extra checks "
                             "aren't needed.", b.toggle("presence_pause_while_locked")))

        self.section("Sign-in screen")
        group = self.add(SettingsGroup())
        group.add(SettingRow(G.CALENDAR_CLOCK, "Learn when I usually come back",
                             "Scans continuously when you're likely to be back soon – right after "
                             "you leave, and around the times you usually return – and takes "
                             "quick looks the rest of the time. Uses only when you lock and sign "
                             "in, kept as counts on this PC.", b.toggle("unlock_learn_routine"),
                             explain="It counts when you lock and come back (for example "
                             "weekday lunchtimes), never what you do. The camera then scans "
                             "continuously only when you're likely to be back, which saves "
                             "power and keeps the camera light off the rest of the time."))
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
        group.add(SettingRow(G.SUN, "Light my face properly",
                             "With a bright window behind you, the camera exposes for the window "
                             "and your face ends up dark. This raises the camera's brightness "
                             "for your face, and puts it back as soon as the camera is released.",
                             b.toggle("camera_face_exposure")))
        focus_controls = QWidget()
        focus_controls.setObjectName("Transparent")
        focus_layout = QHBoxLayout(focus_controls)
        focus_layout.setContentsMargins(0, 0, 0, 0)
        focus_layout.setSpacing(8)
        self.focus_auto = subtle_button("Use automatic focus",
                                        lambda: self.bindings.save(self.focus_auto,
                                                                   camera_focus=0))
        self.focus_set = button("Set focus for my desk", self._calibrate_focus)
        focus_layout.addWidget(self.focus_auto)
        focus_layout.addWidget(self.focus_set)
        self.focus_row = SettingRow(G.SCAN, "Focus", "", focus_controls)
        group.add(self.focus_row)

        self.section("Appearance and notifications")
        group = self.add(SettingsGroup())
        group.add(SettingRow(G.SUN, "Theme", "",
                             b.choice("theme", [("Use my Windows setting", "system"),
                                                ("Light", "light"), ("Dark", "dark")])))
        group.add(SettingRow(G.INFO, "Show notifications",
                             "For example when your camera is disconnected. Selecting a "
                             "notification opens what fixes it.",
                             b.toggle("notifications")))
        group.add(SettingRow(G.VOLUME, "Play a sound for each photo",
                             "A soft shutter click while face setup takes your photos, and a "
                             "chime when it's done.", b.toggle("capture_sounds")))
        self.finish()
        controller.config_changed.connect(lambda _c: self.refresh())
        controller.status_changed.connect(lambda _s: self._camera_text())

    def refresh(self) -> None:
        self.bindings.refresh()
        self._camera_text()
        self._focus_text()

    def _focus_text(self) -> None:
        position = self.controller.cfg.camera_focus
        self.focus_auto.setVisible(bool(position))
        self.focus_row.set_description(
            f"Fixed for your desk (lens position {position}). Automatic focus can hunt in low "
            "light; a fixed focus doesn't." if position else
            "Automatic. If the picture is often soft, especially at night, sit as you normally "
            "do and select \u201cSet focus for my desk\u201d (about 10 seconds).")

    def _calibrate_focus(self) -> None:
        self.focus_set.set_busy(True, "Measuring\u2026")
        self.focus_row.set_description("Measuring \u2013 sit as you normally do and look at the "
                                       "screen for about 10 seconds\u2026")

        def done(reply) -> None:
            self.focus_set.set_busy(False)
            if reply and reply.get("ok"):
                self.bindings.save(self.focus_set, camera_focus=int(reply["position"]))
            else:
                self.banner.show_message("caution", "Focus not set", (reply or {}).get(
                    "message") or "Face Unlock isn't responding.")
            self._focus_text()

        self.controller.client.request({"cmd": "calibrate_focus"}, done, timeout_s=45.0)

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
