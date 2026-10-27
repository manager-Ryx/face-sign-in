"""Visual feedback and the features around it: sign-in timing, lighting hints,
diagnostics (content and masking), rationed notices, notification clicks, busy
buttons, settings search and reset, and setup's "waiting for" reasons."""
from __future__ import annotations

import os
import tempfile
import unittest
import zipfile
from dataclasses import replace
from pathlib import Path
from unittest.mock import patch

import _isolation  # noqa: F401

os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")

from PySide6.QtWidgets import QApplication  # noqa: E402

from face_service.config import APP_DIR, CREDS_PATH, LOG_PATH, Config  # noqa: E402
from face_service.quality import ImageQuality  # noqa: E402
from face_service.service import sign_in_seconds  # noqa: E402
from presence_monitor import diagnostics  # noqa: E402
from presence_monitor.notices import (  # noqa: E402
    FRESH_S,
    WEEK_S,
    Notices,
    seconds_text,
)
from presence_monitor.ui.capture import lighting  # noqa: E402
from tools.ui_preview import STATUS, DemoController  # noqa: E402

APP = QApplication.instance() or QApplication([])


class SignInTimingTests(unittest.TestCase):
    def test_measured_from_waking_the_screen(self) -> None:
        self.assertEqual(sign_in_seconds(100.8, woke_at=100.0, first_face_at=95.0), 0.8)

    def test_an_old_wake_counts_from_the_face_instead(self) -> None:
        self.assertEqual(sign_in_seconds(500.0, woke_at=100.0, first_face_at=498.5), 1.5)

    def test_nothing_to_measure(self) -> None:
        self.assertIsNone(sign_in_seconds(None, 1.0, 1.0))
        self.assertIsNone(sign_in_seconds(10.0, None, None))


class LightingTests(unittest.TestCase):
    def reading(self, face: float, issues: tuple[str, ...] = (), frame: float = 100.0):
        return lighting(ImageQuality(frame, face_brightness=face, issues=issues))

    def test_friendly_hints(self) -> None:
        self.assertEqual(lighting(None).tone, "idle")
        self.assertIn("Looking", lighting(ImageQuality(90.0, issues=("no_face",))).hint)
        self.assertEqual(self.reading(25.0, ("too_dark",)).tone, "critical")
        self.assertIn("behind you", self.reading(60.0, ("backlit",)).hint)
        self.assertIn("bright on your face", self.reading(230.0, ("too_bright",)).hint)
        dim = self.reading(60.0)
        self.assertEqual((dim.tone, dim.hint), ("caution",
                                                "A bit dark: turn on a lamp or face a window."))
        good = self.reading(130.0)
        self.assertEqual(good.tone, "good")
        self.assertAlmostEqual(good.level, 130.0 / 255.0)


class DiagnosticsTests(unittest.TestCase):
    who = diagnostics.Identities(user="ada", domain="ADA-LAPTOP", computer="ADA-LAPTOP",
                                 email="ada@example.org",
                                 camera_paths=[r"\\?\usb#vid_046d&pid_085e#abc#{65e8773d}"])

    def test_personal_details_are_masked(self) -> None:
        text = (r"user ada signed in on ADA-LAPTOP as ada@example.org from C:\Users\ada\x "
                r"camera \\?\usb#vid_046d&pid_085e#abc#{65e8773d}\global and "
                r"\\?\usb#vid_1&pid_2#other, also bob@elsewhere.net; adapter stays")
        masked = diagnostics.redact(text, self.who)
        for secret in ("ada@example.org", "ADA-LAPTOP", r"C:\Users\ada", "vid_046d", "vid_1",
                       "bob@elsewhere.net", " ada "):
            self.assertNotIn(secret, masked)
        self.assertIn("adapter stays", masked)   # whole words only
        self.assertIn("<camera device>", masked)

    def test_short_names_are_left_alone(self) -> None:
        masked = diagnostics.redact("a b c", diagnostics.Identities(user="a"))
        self.assertEqual(masked, "a b c")

    def test_report_has_checks_status_settings_and_logs(self) -> None:
        cfg = replace(Config(), camera_device_id=self.who.camera_paths[0])
        report = diagnostics.build_report(
            status=STATUS, system={"tile": None, "hello": False, "autostart": "tasks"}, cfg=cfg,
            events=[{"kind": "lock_session", "ts": 1.0, "face_accepted": True, "sign_in_s": 0.9}],
            service_log="service line for ada", presence_log="", who=self.who, now=0.0)
        for part in ("== Checks ==", "[TODO] Lock-screen tile", "[OK  ] Stored password",
                     "== Service status ==", "camera_device_id = '<camera device>'",
                     "signed in by face: 1, median time 0.9 s", "service line for <user>",
                     "Never included"):
            self.assertIn(part, report)
        self.assertNotIn("vid_046d", report)

    def test_never_reads_the_password_or_face_files(self) -> None:
        APP_DIR.mkdir(parents=True, exist_ok=True)
        CREDS_PATH.write_text("SECRET-PASSWORD-MARKER")
        LOG_PATH.write_text("\n".join(f"line {i}" for i in range(500)) + "\n")
        opened: list[str] = []
        real_open = Path.open

        def watch(path, *args, **kwargs):
            opened.append(Path(path).name)
            return real_open(path, *args, **kwargs)

        try:
            with patch.object(Path, "open", watch):
                report = diagnostics.collect(status=None, system={}, cfg=Config(),
                                             who=diagnostics.Identities())
        finally:
            CREDS_PATH.unlink(missing_ok=True)
            LOG_PATH.unlink(missing_ok=True)
        self.assertNotIn("SECRET-PASSWORD-MARKER", report)
        self.assertNotIn(CREDS_PATH.name, opened)
        self.assertNotIn("embeddings.npz", opened)
        self.assertIn("line 499", report)
        self.assertNotIn("line 199\n", report)  # only the last 300 lines

    def test_zip_holds_the_report(self) -> None:
        with tempfile.TemporaryDirectory() as folder:
            path = diagnostics.save_zip("hello\n", Path(folder), now=0.0)
            with zipfile.ZipFile(path) as archive:
                self.assertEqual(archive.read("face-unlock-diagnostics.txt"), b"hello\n")


class NoticeTests(unittest.TestCase):
    def setUp(self) -> None:
        self.folder = tempfile.TemporaryDirectory()
        self.path = Path(self.folder.name) / "notices.json"

    def tearDown(self) -> None:
        self.folder.cleanup()

    def test_first_sign_in_is_celebrated_once(self) -> None:
        now = 1_000_000.0
        notices = Notices(self.path)
        status = {"last_face_sign_in": now - 5, "last_face_sign_in_s": 0.84}
        notice = notices.on_status(status, now)
        self.assertEqual(notice.title, "Signed in by face in 0.8 s")
        self.assertIsNone(notices.on_status(status, now + 1))
        self.assertIsNone(Notices(self.path).on_status(status, now + 2))   # after a restart

    def test_an_old_sign_in_is_not_news(self) -> None:
        now = 1_000_000.0
        notices = Notices(self.path)
        self.assertIsNone(notices.on_status({"last_face_sign_in": now - FRESH_S - 1}, now))
        self.assertIsNone(notices.on_status({"last_face_sign_in": None}, now))
        self.assertIsNone(notices.on_status(None, now))

    def test_weekly_summary_waits_a_week_and_needs_a_few(self) -> None:
        start = 1_000_000.0
        notices = Notices(self.path)
        self.assertFalse(notices.weekly_due(start))              # first week: nothing
        self.assertTrue(notices.weekly_due(start + WEEK_S))
        self.assertIsNone(notices.weekly(2, start + WEEK_S))     # too few to mention
        self.assertFalse(notices.weekly_due(start + WEEK_S + 3600))
        later = start + 2 * WEEK_S
        self.assertEqual(notices.weekly(12, later).title,
                         "Face Unlock saved you 12 PIN entries this week")

    def test_seconds_text(self) -> None:
        self.assertEqual([seconds_text(v) for v in (0.84, 2.0, 12.4)], ["0.8 s", "2 s", "12 s"])


class FeedbackWidgetTests(unittest.TestCase):
    def test_busy_button_restores_itself(self) -> None:
        from presence_monitor.ui.widgets import accent_button
        button = accent_button("Check")
        button.set_busy(True, "Checking…")
        self.assertEqual(button.text(), "Checking…")
        self.assertFalse(button.isEnabled())
        button.setText("Check again")     # a label for afterwards
        button.setEnabled(True)
        self.assertEqual(button.text(), "Checking…")
        button.set_busy(False)
        self.assertEqual(button.text(), "Check again")
        self.assertTrue(button.isEnabled())

    def test_settings_search_and_reset(self) -> None:
        from presence_monitor.ui.pages.settings import SettingsPage
        from presence_monitor.ui.widgets import SettingRow
        controller = DemoController(APP, "light")
        page = SettingsPage(controller)  # type: ignore[arg-type]
        page.refresh()
        self.assertGreater(page.filter_settings("camera"), 0)
        self.assertEqual(page.filter_settings("zebra"), 0)
        self.assertFalse(page.no_results.isHidden())
        page.filter_settings("")
        self.assertTrue(page.no_results.isHidden())
        # The demo's 30-minute "keep looking" isn't the default: reset is offered.
        row = next(r for r in page.findChildren(SettingRow)
                   if r.title.text() == "Keep looking for me for")
        self.assertFalse(row.reset_button.isHidden())
        row.reset_button.click()
        self.assertEqual(controller.cfg.unlock_idle_after_s, Config().unlock_idle_after_s)
        self.assertTrue(row.reset_button.isHidden())
        self.assertFalse(row.saved_mark.isHidden())   # the tick confirming the save

    def test_password_boxes_say_when_they_match(self) -> None:
        from presence_monitor.ui.dialogs import PasswordDialog
        with patch("presence_monitor.ui.dialogs.microsoft_account", return_value=None):
            dialog = PasswordDialog(DemoController(APP, "light"), None)  # type: ignore[arg-type]
        self.assertFalse(dialog.accept_button.isEnabled())
        dialog.password.setText("secret")
        dialog.repeat.setText("sec")          # still typing: no complaint yet
        self.assertTrue(dialog.match.isHidden())
        dialog.repeat.setText("secrex")
        self.assertIn("don't match", dialog.match.text.text())
        dialog.repeat.setText("secret")
        self.assertTrue(dialog.accept_button.isEnabled())


class SetupReasonTests(unittest.TestCase):
    def test_continue_says_what_it_is_waiting_for(self) -> None:
        from presence_monitor.system_setup import TileState
        from presence_monitor.ui.onboarding import OnboardingWizard, SystemChecks
        checks = SystemChecks(tile_state=lambda: TileState(True), install_tile=lambda: (True, ""),
                              autostart_state=lambda: "tasks",
                              enable_autostart=lambda: (True, ""), hello_only=lambda: False,
                              start_service=lambda: None)
        controller = DemoController(APP, "light")
        wizard = OnboardingWizard(controller, None, checks=checks)  # type: ignore[arg-type]
        wizard.timer.stop()
        self.assertEqual(wizard.waiting.text(), "Tick the box above to continue")
        wizard.consent.setChecked(True)
        self.assertEqual(wizard.waiting.text(), "")
        controller.client.status = None
        wizard._go(1)
        wizard.timer.stop()
        self.assertEqual(wizard.waiting.text(), "Waiting for the background service, the face "
                                                "model and the camera")
        self.assertFalse(wizard.next.isEnabled())


class NotificationClickTests(unittest.TestCase):
    def test_selecting_a_notification_opens_its_fix(self) -> None:
        from presence_monitor.ui.tray import TrayController
        fixes: list[tuple] = []

        class Controller(DemoController):
            def run_fix(self, action, parent=None, *, from_notification=False):
                fixes.append((action, from_notification))

            def system_state(self):
                return {"tile": None, "hello": False}

        controller = Controller(APP, "light")
        tray = TrayController(controller)  # type: ignore[arg-type]
        tray._update(STATUS)
        rejected = dict(STATUS, not_ready=["password-rejected"])
        with patch.object(tray.icon, "showMessage") as shown:
            tray._update(rejected)
        self.assertIn("Select to store your new password", shown.call_args.args[1])
        tray._message_clicked()
        self.assertEqual(fixes, [("password", True)])
        tray._message_clicked()                       # a later click just opens the app
        self.assertEqual(fixes[-1], ("open", True))


if __name__ == "__main__":
    unittest.main()
