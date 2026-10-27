"""Interface: plain-English summaries, capture rules, and headless rendering."""
from __future__ import annotations

import os
import tempfile
import unittest
from copy import deepcopy
from dataclasses import replace
from pathlib import Path

import _isolation  # noqa: F401

os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")

import numpy as np  # noqa: E402
from PySide6.QtWidgets import QApplication  # noqa: E402

from face_service.config import Config  # noqa: E402
from face_service.quality import FaceBox  # noqa: E402
from presence_monitor.ui.capture import CapturePlan, targets_for  # noqa: E402
from presence_monitor.ui.status import duration, guidance_text, summarize, when  # noqa: E402
from tools.ui_preview import STATUS, DemoController, render  # noqa: E402

APP = QApplication.instance() or QApplication([])
SNAPSHOT = {"paused": False, "last_result": "present", "last_at": 0, "absence_elapsed_s": 0}


def status(**changes) -> dict:
    data = deepcopy(STATUS)
    for key, value in changes.items():
        data[key] = value
    return data


class SummaryTests(unittest.TestCase):
    cfg = Config(presence_absence_timeout_s=3600)

    def test_ready(self) -> None:
        overview = summarize(status(), SNAPSHOT, self.cfg)
        self.assertEqual((overview.tone, overview.title), ("success", "Face sign-in is ready"))
        self.assertIn("Photo-spoof protection is off", overview.message)
        self.assertEqual(overview.autolock.value, "Locks after about 1 hour")

    def test_service_not_running(self) -> None:
        overview = summarize(None, SNAPSHOT, self.cfg)
        self.assertEqual((overview.action, overview.tray_state), ("start-service", "off"))

    def test_not_enrolled_comes_first(self) -> None:
        overview = summarize(status(profile={"anchors": 0, "learned": 0}), SNAPSHOT, self.cfg)
        self.assertEqual(overview.action, "setup")

    def test_moved_camera_needs_confirmation(self) -> None:
        camera = deepcopy(STATUS["camera"])
        camera["device"] = {"state": "moved", "name": "NearStream V02AF", "path": "x"}
        overview = summarize(status(camera=camera), SNAPSHOT, self.cfg)
        self.assertEqual((overview.action, overview.alert_key), ("camera-moved", "camera-moved"))

    def test_missing_camera_is_critical(self) -> None:
        camera = deepcopy(STATUS["camera"])
        camera["device"] = {"state": "missing", "name": "NearStream V02AF"}
        overview = summarize(status(camera=camera), SNAPSHOT, self.cfg)
        self.assertEqual(overview.tone, "critical")
        self.assertIn("won't switch to another camera", overview.camera.detail)

    def test_missing_password(self) -> None:
        overview = summarize(status(credentials=False), SNAPSHOT, self.cfg)
        self.assertEqual(overview.action, "password")

    def test_a_face_model_that_cannot_be_prepared_offers_a_retry(self) -> None:
        recognition = dict(STATUS["recognition"], model_error="url unreachable")
        overview = summarize(status(not_ready=["model-unavailable"], recognition=recognition),
                             SNAPSHOT, self.cfg)
        self.assertEqual(overview.action, "prepare-model")
        self.assertIn("url unreachable", overview.message)

    def test_photo_protection_that_cannot_start_is_explained(self) -> None:
        overview = summarize(status(not_ready=["anti-spoofing-unavailable"]), SNAPSHOT, self.cfg)
        self.assertEqual((overview.action, overview.alert_key),
                         ("settings", "anti-spoofing-unavailable"))

    def test_paused_auto_lock(self) -> None:
        overview = summarize(status(), {**SNAPSHOT, "paused": True}, self.cfg)
        self.assertEqual(overview.tray_state, "paused")
        self.assertEqual(overview.autolock.pill, "Paused")

    def test_wording(self) -> None:
        self.assertEqual(duration(60), "1 minute")
        self.assertEqual(duration(5400), "1.5 hours")
        self.assertEqual(duration(45), "45 seconds")
        self.assertIn("too dark", guidance_text("too_dark"))
        self.assertEqual(when(None), "never")


def box(nose_x: float, nose_y: float = 300.0) -> FaceBox:
    return FaceBox(220, 120, 200, 240, 0.95, left_eye=(370, 240), right_eye=(270, 240),
                   nose=(int(nose_x), int(nose_y)))


def textured(brightness: float) -> np.ndarray:
    rng = np.random.default_rng(0)
    return np.clip(rng.normal(brightness, 30, (480, 640)), 0, 255).astype(np.uint8)


class CapturePlanTests(unittest.TestCase):
    def capture_ready(self, plan: CapturePlan, face: FaceBox, gray: np.ndarray,
                      start: float) -> bool:
        result = None
        for step in range(6):
            result = plan.observe(gray, [face], now=start + step * 0.2)
        return bool(result and result.capture)

    def test_straight_on_photos_come_first_and_set_the_baseline(self) -> None:
        plan = CapturePlan(targets_for("append"))
        self.assertEqual(plan.next_pose(), "center")
        gray = textured(120)
        self.assertTrue(self.capture_ready(plan, box(320), gray, 0))
        plan.record("center", box(320), now=1)
        self.assertTrue(self.capture_ready(plan, box(320), gray, 2))
        plan.record("center", box(320), now=3)
        self.assertIsNotNone(plan.baseline)

    def test_turn_is_classified_relative_to_your_straight_pose(self) -> None:
        plan = CapturePlan(targets_for("replace"))
        plan.baseline = (0.5, 0.6)  # this person's nose sits slightly right when facing ahead
        self.assertEqual(plan.classify(box(320 + 50)), "center")
        self.assertEqual(plan.classify(box(320 + 70)), "left")
        self.assertEqual(plan.classify(box(320 + 30)), "right")

    def test_dark_face_is_not_captured_and_gets_guidance(self) -> None:
        plan = CapturePlan(targets_for("append"))
        observation = plan.observe(textured(12), [box(320)], now=0)
        self.assertFalse(observation.capture)
        self.assertIn("too dark", observation.guidance)

    def test_two_faces_are_never_captured(self) -> None:
        plan = CapturePlan(targets_for("append"))
        observation = plan.observe(textured(120), [box(320), box(330)], now=0)
        self.assertFalse(observation.capture)
        self.assertIn("only you", observation.guidance)

    def test_must_hold_still_before_a_capture(self) -> None:
        plan = CapturePlan(targets_for("append"))
        first = plan.observe(textured(120), [box(320)], now=0)
        self.assertFalse(first.capture)
        self.assertEqual(first.guidance, "Hold still…")

    def test_plan_completes(self) -> None:
        plan = CapturePlan({"center": 1, "left": 0, "right": 0, "up": 0, "down": 0})
        plan.record("center", box(320))
        self.assertTrue(plan.done)
        self.assertEqual(plan.taken, plan.total)


class CaptureFailSafeTests(unittest.TestCase):
    def straight_on_done(self) -> CapturePlan:
        plan = CapturePlan({"center": 2, "left": 0, "right": 0, "up": 0, "down": 1})
        plan.take_anyway(box(320), now=0)
        plan.take_anyway(box(320), now=1)
        return plan

    def test_take_photo_now_counts_for_the_angle_asked_for(self) -> None:
        plan = self.straight_on_done()
        self.assertEqual(plan.next_pose(), "down")
        self.assertEqual(plan.take_anyway(box(320), now=2), "down")
        self.assertTrue(plan.done)
        self.assertEqual(plan.manual, 3)

    def test_an_angle_can_be_skipped_but_not_straight_on(self) -> None:
        plan = CapturePlan({"center": 2, "left": 0, "right": 0, "up": 0, "down": 1})
        self.assertIsNone(plan.skip())
        plan = self.straight_on_done()
        self.assertEqual(plan.skip(), "down")
        self.assertTrue(plan.done)

    def test_a_smaller_tilt_is_enough_after_being_stuck(self) -> None:
        plan = self.straight_on_done()
        slight = box(320, 305)  # less chin-down than normally needed
        self.assertNotEqual(plan.observe(textured(120), [slight], now=2).pose, "down")
        late = plan.observe(textured(120), [slight], now=11)
        self.assertTrue(late.stuck)
        self.assertEqual(late.pose, "down")


class SettingRowTests(unittest.TestCase):
    def test_a_description_set_later_appears_inside_the_row(self) -> None:
        from presence_monitor.ui.widgets import SettingRow
        row = SettingRow(None, "Focus")
        row.set_description("Automatic.")
        self.assertIs(row.description.window(), row.window())
        self.assertFalse(row.description.isWindow())


class SetupWizardTests(unittest.TestCase):
    def test_cancel_closes_the_setup_window(self) -> None:
        from presence_monitor.ui.enroll import EnrollWizard
        wizard = EnrollWizard(DemoController(APP, "light"), "append", None)  # type: ignore[arg-type]
        wizard.show()
        APP.processEvents()
        wizard.cancel.click()
        APP.processEvents()
        self.assertFalse(wizard.isVisible())


    def test_unknown_service_status_never_turns_add_into_replace(self) -> None:
        from presence_monitor.ui.enroll import EnrollWizard
        controller = DemoController(APP, "light")
        controller.client.status = None  # the first status reply hasn't arrived yet
        wizard = EnrollWizard(controller, "append", None)  # type: ignore[arg-type]
        self.assertEqual(wizard.mode, "append")
        controller.client.status = status(profile={"anchors": 0, "learned": 0})
        self.assertEqual(EnrollWizard(controller, "append", None).mode,  # type: ignore[arg-type]
                         "replace")


class RoutineTextTests(unittest.TestCase):
    def test_learned_routine_in_plain_english(self) -> None:
        from presence_monitor.ui.pages.learning import describe_routine, routine_outcome
        from tools.ui_preview import INSIGHTS
        info = INSIGHTS["routine"]
        text = describe_routine(info, 3.0)
        self.assertIn("weekdays 07:20–08:45, 12:25–13:20", text)
        self.assertIn("mornings 14 min", text)
        self.assertIn("quick look every 3 seconds", text)
        self.assertIn("19 of 21 face sign-ins", routine_outcome(info))


class ConfigWatchTests(unittest.TestCase):
    def test_settings_changed_elsewhere_are_picked_up(self) -> None:
        from face_service.config import save_config
        from presence_monitor.app import AppController
        save_config(Config())
        controller = AppController(APP)
        sent: list[str] = []
        controller.client.request = lambda request, *a, **k: sent.append(request["cmd"])
        save_config(replace(controller.cfg, unlock_idle_interval_s=3.0))
        controller._reload_if_changed()
        self.assertEqual(controller.cfg.unlock_idle_interval_s, 3.0)
        self.assertEqual(sent, ["reload_config"])
        controller._reload_if_changed()  # unchanged: nothing more to do
        self.assertEqual(sent, ["reload_config"])


def drew_something(path: Path) -> bool:
    """A real screen has text, icons and controls: many distinct colours."""
    from PySide6.QtGui import QImage
    image = QImage(str(path)).convertToFormat(QImage.Format.Format_ARGB32)
    pixels = np.frombuffer(image.constBits(), np.uint32, image.width() * image.height())
    return len(np.unique(pixels)) > 4   # a blank window has one colour


class RenderingTests(unittest.TestCase):
    def test_every_screen_renders_in_both_themes(self) -> None:
        with tempfile.TemporaryDirectory() as folder:
            for theme_name in ("light", "dark"):
                written = render(Path(folder), theme_name, 1.0)
                self.assertGreaterEqual(len(written), 13)
                blank = [p.name for p in written if not drew_something(p)]
                self.assertEqual(blank, [])

    def test_switch_labels_follow_settings_loaded_later(self) -> None:
        from presence_monitor.ui.pages.settings import SettingsPage
        from presence_monitor.ui.widgets import SettingRow, ToggleSwitch
        controller = DemoController(APP, "light")
        controller.cfg = replace(controller.cfg, presence_enabled=True)
        page = SettingsPage(controller)  # type: ignore[arg-type]
        page.refresh()
        row = page.findChildren(SettingRow)[0]
        switch = row.findChild(ToggleSwitch)
        self.assertTrue(switch.isChecked())
        states = [w.text() for w in row.findChildren(type(row.title)) if w.text() in ("On", "Off")]
        self.assertEqual(states, ["On"])


if __name__ == "__main__":
    unittest.main()
