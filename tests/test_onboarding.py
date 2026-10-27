"""First-run setup: readiness checks, the wizard, and shipped model files."""
from __future__ import annotations

import os
import re
import shutil
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

import _isolation  # noqa: F401

os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")

from PySide6.QtWidgets import QApplication  # noqa: E402

from face_service import models  # noqa: E402
from face_service.config import CREDS_PATH, EMBED_PATH  # noqa: E402
from presence_monitor import system_setup  # noqa: E402
from presence_monitor.system_setup import TileState  # noqa: E402
from presence_monitor.ui.onboarding import (  # noqa: E402
    OnboardingWizard,
    SystemChecks,
    preference_changes,
)
from tools.ui_preview import STATUS, DemoController  # noqa: E402

APP = QApplication.instance() or QApplication([])
REPO = Path(__file__).resolve().parent.parent


def checks(*, tile: bool = True) -> SystemChecks:
    return SystemChecks(tile_state=lambda: TileState(tile), install_tile=lambda: (True, ""),
                        autostart_state=lambda: "tasks", enable_autostart=lambda: (True, ""),
                        hello_only=lambda: False, start_service=lambda: None)


class SystemSetupTests(unittest.TestCase):
    def test_tile_id_matches_the_credential_provider(self) -> None:
        source = (REPO / "credential_provider" / "guid.h").read_text(encoding="utf-8")
        self.assertIn(system_setup.TILE_CLSID.strip("{}"), source.upper())

    def test_a_registered_tile_without_its_program_file_is_not_installed(self) -> None:
        values = {rf"{system_setup.PROVIDERS_KEY}\{system_setup.TILE_CLSID}": "Face Unlock",
                  rf"{system_setup.CLSID_KEY}\{system_setup.TILE_CLSID}\InprocServer32":
                      r"C:\nowhere\FaceCredentialProvider.dll"}
        with patch.object(system_setup, "_read_default", lambda _root, path: values.get(path)):
            state = system_setup.tile_state()
        self.assertFalse(state.installed)
        self.assertIn("missing", state.problem)

    def test_start_at_sign_in_needs_the_installed_app(self) -> None:
        ok, message = system_setup.enable_autostart()   # a developer copy
        self.assertFalse(ok)
        self.assertIn("register_tasks", message)

    def test_declining_the_windows_permission_prompt_changes_nothing(self) -> None:
        import pywintypes
        from win32com.shell import shell

        def declined(**_kwargs):
            raise pywintypes.error(1223, "ShellExecuteEx", "The operation was cancelled.")

        with patch.object(shell, "ShellExecuteEx", declined):
            ok, message = system_setup.run_elevated("regsvr32.exe", "/s x.dll")
        self.assertFalse(ok)
        self.assertIn("permission", message)


class BundledModelTests(unittest.TestCase):
    def test_shipped_models_are_put_where_deepface_looks(self) -> None:
        bundle, home = Path(tempfile.mkdtemp()), Path(tempfile.mkdtemp())
        self.addCleanup(shutil.rmtree, bundle, True)
        self.addCleanup(shutil.rmtree, home, True)
        (bundle / "weights").mkdir()
        (bundle / "weights" / models.ARCFACE_WEIGHTS).write_bytes(b"w" * 64)
        (bundle / models.YUNET).write_bytes(b"y" * 32)
        with patch.object(models, "bundle_dir", lambda: bundle), \
                patch.dict(os.environ, {"DEEPFACE_HOME": str(home)}):
            self.assertEqual(sorted(models.provide_bundled_models()),
                             sorted([models.ARCFACE_WEIGHTS, models.YUNET]))
            self.assertEqual(models.provide_bundled_models(), [])  # already there
            self.assertTrue((home / ".deepface" / "weights" / models.YUNET).is_file())


class OnboardingTests(unittest.TestCase):
    def setUp(self) -> None:
        system_setup.ONBOARDING_PATH.unlink(missing_ok=True)
        self.controller = DemoController(APP, "light")

    def wizard(self, **kwargs) -> OnboardingWizard:
        wizard = OnboardingWizard(self.controller, None, checks=checks(**kwargs))  # type: ignore[arg-type]
        wizard.timer.stop()
        return wizard

    def test_nothing_continues_until_you_agree(self) -> None:
        wizard = self.wizard()
        self.assertFalse(wizard.next.isEnabled())
        wizard.consent.setChecked(True)
        self.assertTrue(wizard.next.isEnabled())

    def test_getting_ready_waits_for_the_service_and_camera(self) -> None:
        wizard = self.wizard(tile=False)
        wizard._local = {"tile": TileState(False), "hello": False, "autostart": "tasks"}
        wizard._go(1)
        wizard.timer.stop()
        self.assertTrue(wizard.next.isEnabled())       # demo service, model and camera ready
        self.assertFalse(wizard.row_tile.ok)            # optional: offered, not required
        self.assertEqual(wizard.row_tile.action.text(), "Install")
        self.controller.client.status = None
        wizard._refresh()
        self.assertFalse(wizard.next.isEnabled())

    def test_preferences_become_settings(self) -> None:
        changes = preference_changes(auto_lock=True, lock_after_s=900, scanning="battery",
                                     learning=False, notifications=True)
        self.assertEqual(changes["presence_absence_timeout_s"], 900)
        self.assertEqual(changes["presence_interval_s"], 180)
        self.assertEqual(changes["unlock_idle_interval_s"], 10.0)
        self.assertTrue(changes["unlock_pause_when_display_off"])
        self.assertFalse(changes["adaptive_learn_after_password"])

    def test_finishing_records_that_setup_is_done(self) -> None:
        wizard = self.wizard()
        wizard._go(5)
        wizard._next()
        self.assertTrue(system_setup.onboarding_done())

    def test_a_pc_set_up_before_is_not_asked_again(self) -> None:
        from presence_monitor.app import AppController
        EMBED_PATH.parent.mkdir(parents=True, exist_ok=True)
        for path in (EMBED_PATH, CREDS_PATH):
            if not path.exists():
                path.write_bytes(b"x")
                self.addCleanup(path.unlink)
        controller = AppController(APP)
        with patch.object(controller, "open_onboarding") as opened:
            controller._first_run()
        opened.assert_not_called()
        self.assertTrue(system_setup.onboarding_done())

    def test_a_stopped_service_is_started_again_but_not_after_quit(self) -> None:
        from presence_monitor.app import AppController
        system_setup.mark_onboarding_done()
        controller = AppController(APP)
        with patch.object(controller, "start_service") as start:
            controller._watchdog(None)
            start.assert_not_called()          # one missed answer can be a hiccup
            controller._watchdog(None)
            start.assert_called_once()
            controller._watchdog(None)
            start.assert_called_once()          # not again within five minutes
        controller._quitting, controller._restarted_at = True, float("-inf")
        with patch.object(controller, "start_service") as start:
            controller._watchdog(None)
            start.assert_not_called()

    def test_the_overview_offers_to_add_the_tile(self) -> None:
        from presence_monitor.ui.status import summarize
        overview = summarize(dict(STATUS, credentials=True), {}, self.controller.cfg,
                             {"tile": TileState(False), "hello": False})
        self.assertEqual(overview.action, "install-tile")
        self.assertTrue(re.search("lock screen", overview.title))


if __name__ == "__main__":
    unittest.main()
