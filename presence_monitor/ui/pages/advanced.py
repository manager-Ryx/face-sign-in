"""Advanced settings: security, performance, and maintenance."""
from __future__ import annotations

from typing import TYPE_CHECKING

from face_service.config import APP_DIR, Config

from ..icons import G
from ..widgets import Banner, Page, SettingRow, SettingsGroup, button, label
from .settings import Bindings

if TYPE_CHECKING:
    from ...app import AppController


class AdvancedPage(Page):
    def __init__(self, controller: AppController):
        super().__init__("Advanced", "These settings affect security and reliability. The "
                         "defaults are recommended and marked \u201c(default)\u201d.")
        self.controller = controller
        self.add_search("Find an advanced setting")
        self.banner = self.add(Banner())
        b = self.bindings = Bindings(controller, self, self.banner)

        self.section("Recognition")
        group = self.add(SettingsGroup())
        self.threshold = b.number("threshold", 0.20, 0.68, step=0.01, decimals=2)
        self.threshold.valueChanged.disconnect()
        self.threshold.valueChanged.connect(self._threshold_changed)
        group.add(SettingRow(G.SHIELD, "Strictest match allowed",
                             "How closely the camera image must match your profile. Lower is "
                             "stricter: fewer chances for someone else, more chances you'll be "
                             "asked for your PIN. Automatic tuning may only go lower.",
                             self.threshold,
                             explain="Each camera image becomes a face signature; this is the "
                             "largest difference from your profile still counted as you. At "
                             "0.40 a close match is needed; at 0.60 a looser one is accepted. "
                             "The Learning page shows how a stricter value would have worked "
                             "for you before you change it."))
        group.add(SettingRow(G.EYE_OFF, "Check for photos and screens (anti-spoofing)",
                             "Tries to reject a printed photo or a phone screen held up to the "
                             "camera. It adds processing time to each frame and can reject you in "
                             "poor light. When off, a good photo of you could unlock your PC.",
                             b.toggle("anti_spoofing"),
                             explain="A small extra model looks for the tell-tale signs of a "
                             "print or a screen (flat texture, moiré, glare). An ordinary "
                             "webcam can't measure depth, so it can't be perfect, but it "
                             "stops the easy tricks."))
        group.add(SettingRow(G.SCAN, "Sign-in screen frame strategy",
                             "Progressive looks at one frame first and checks more only when "
                             "needed. Fixed always checks three frames (the previous behaviour). "
                             "Both need two matching frames.",
                             explain="Progressive signs you in sooner when the first frame is "
                             "a clear match. Security is the same: two frames must match "
                             "either way.",
                             control=b.choice("unlock_strategy", [("Progressive (recommended)",
                                                           "progressive"),
                                                          ("Fixed, 2 of 3", "fixed")])))
        group.add(SettingRow(G.PERSON, "Face detection sensitivity",
                             "Lowest detector confidence accepted for live frames. Lower helps "
                             "in dim light; it doesn't change how closely a face must match.",
                             b.number("detector_min_confidence", 0.5, 0.95, step=0.05,
                                      decimals=2)))

        self.section("Performance")
        group = self.add(SettingsGroup())
        group.add(SettingRow(G.GAUGE, "Processor threads for recognition",
                             "More threads finish each frame sooner (measured: 1 thread 330 ms, "
                             "4 threads about 90 ms). Takes effect after Face Unlock restarts.",
                             b.number("inference_threads", 1, 8)))
        group.add(SettingRow(G.MOON, "Low-light frame rate",
                             "In dim light a lower frame rate lets the camera expose longer. The "
                             "app turns this on only after measuring that it helps your camera.",
                             b.choice("camera_low_light_fps", [("Off", 0.0), ("10 per second", 10.0),
                                                               ("15 per second", 15.0),
                                                               ("20 per second", 20.0)])))
        group.add(SettingRow(G.STOPWATCH, "Longest camera warm-up",
                             "The camera is used as soon as its image is stable; this is only the "
                             "upper limit.",
                             b.number("camera_warmup_max_s", 0.3, 6.0, step=0.1, decimals=1,
                                      suffix=" s")))

        self.section("Learning limits")
        group = self.add(SettingsGroup())
        group.add(SettingRow(G.CHART, "Most learned samples kept",
                             "Your original reference photos are kept in addition to these.",
                             b.number("adaptive_max_embeddings", 10, 2000, step=10)))
        group.add(SettingRow(G.HISTORY, "Forget learned samples after",
                             "0 keeps them until the limit above is reached.",
                             b.number("adaptive_retention_days", 0, 3650, decimals=0,
                                      suffix=" days")))
        group.add(SettingRow(G.ADD, "Samples learned per sign-in",
                             "One is recommended: the single most useful frame.",
                             b.choice("adaptive_samples_per_unlock", [("1 (recommended)", 1),
                                                                      ("2", 2)])))

        self.section("Security")
        group = self.add(SettingsGroup())
        group.add(SettingRow(G.KEY, "Who may ask for your password",
                             "The background service hands your password only to the Windows "
                             "sign-in screen. \"Record\" logs any other program that asks; "
                             "\"Only the sign-in screen\" refuses them.",
                             explain="Windows' sign-in screen runs as SYSTEM, so Face Unlock "
                             "can tell it apart from an ordinary program. \"Only the sign-in "
                             "screen\" is the safest; \"Record\" is for troubleshooting.",
                             control=b.choice("unlock_client_check",
                                      [("Record other callers", "log"),
                                       ("Only the sign-in screen", "enforce"), ("Off", "off")])))
        group.add(SettingRow(G.LOCK, "Don't sign me straight back in after I lock",
                             "After you lock the PC yourself (for example with Windows+L), face "
                             "sign-in waits until you've stepped out of view. Waking the screen "
                             "or selecting the Face Unlock tile signs you in at once.",
                             b.toggle("unlock_wait_after_manual_lock")))
        group.add(SettingRow(G.PERSON, "If I don't stay after a face sign-in",
                             "For example when the camera recognised you walking past. Works "
                             "while \"Lock my PC when I'm away\" is on.",
                             b.choice("relock_unconfirmed_s",
                                      [("Lock again after 1 minute", 60),
                                       ("Lock again after 2 minutes", 120),
                                       ("Lock again after 5 minutes", 300),
                                       ("Don't lock again", 0)],
                                      lambda s: f"Lock again after {s} seconds" if s
                                      else "Don't lock again")))

        self.section("Data and maintenance")
        group = self.add(SettingsGroup())
        group.add(SettingRow(G.HISTORY, "Keep measurements for",
                             "Used for the Learning page. Never includes images.",
                             b.number("metrics_retention_days", 7, 365, suffix=" days")))
        group.add(SettingRow(G.FOLDER, "Data folder", str(APP_DIR),
                             button("Open", controller.open_data_folder)))
        group.add(SettingRow(G.SYNC, "Check for updates when Face Unlock starts",
                             "Looks for releases of the original open-source project. Installing "
                             "one would replace this customised version.",
                             b.toggle("check_for_updates")))
        reset = SettingRow(G.UNDO, "Restore default settings",
                           "Your face profile and stored password are kept.",
                           button("Restore defaults…", self._restore_defaults))
        group.add(reset)
        diagnostics = button("Copy diagnostics…", lambda: controller.run_fix("diagnostics", self))
        diagnostics.setToolTip("A report for getting help: versions, checks, settings and the "
                               "end of the logs. Never your password, face data or photos.")
        group.add(SettingRow(G.COPY, "Copy diagnostics for support",
                             "Copies a report to the clipboard and saves it as a zip in "
                             "Downloads. Nothing private is included.", diagnostics))
        self.add(label("Changes take effect straight away unless noted.", "Caption"))
        self.finish()
        controller.config_changed.connect(lambda _c: self.refresh())

    def refresh(self) -> None:
        self.bindings.refresh()

    def _threshold_changed(self, value: float) -> None:
        from ..dialogs import confirm
        current = self.controller.cfg.threshold
        if value > current and not confirm(
                self, "Make recognition less strict?",
                f"At {value:.2f}, faces that are less similar to yours are accepted. This makes it "
                "easier for someone who looks like you to sign in.", "Make less strict"):
            self.refresh()
            return
        self.bindings.save(self.threshold, threshold=round(value, 2))

    def _restore_defaults(self) -> None:
        from dataclasses import asdict

        from ..dialogs import confirm
        if not confirm(self, "Restore default settings?",
                       "All settings return to their defaults. Your face profile, learned "
                       "samples and stored password are kept.", "Restore defaults"):
            return
        previous = asdict(self.controller.cfg)
        keep = {"camera_device_id": self.controller.cfg.camera_device_id,
                "language": self.controller.cfg.language}
        defaults = {**asdict(Config()), **keep}
        ok, problem = self.controller.update_config(**defaults)
        if not ok:
            self.banner.show_message("critical", "Defaults weren't restored", problem)
            return
        from ..widgets import show_toast
        show_toast(self, "Default settings restored", action=(
            "Undo", lambda: self.controller.update_config(**previous)), ms=8000)
