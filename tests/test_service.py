"""FaceService behaviour with a scripted camera and fake recognition model."""
from __future__ import annotations

import shutil
import tempfile
import time
import unittest
from itertools import cycle
from unittest.mock import patch

import _isolation  # noqa: F401
import numpy as np
from helpers import FakeEmbedder, direction, image

from face_service.camera import OpenReport, SettleReport
from face_service.camera_manager import CameraManager
from face_service.config import APP_DIR, EMBED_PATH, STATE_DIR, Config
from face_service.metrics import NullMetrics
from face_service.pipe import ClientInfo
from face_service.profile import FaceProfile, load_profile, save_profile
from face_service.service import FaceService

SYSTEM = ClientInfo(4321, "S-1-5-18")
USER = ClientInfo(1234, "S-1-5-21-1-2-3-1001")
ME, STRANGER, NOBODY = 1, 2, 3
ANGLES = {ME: 20.0, STRANGER: 80.0}           # NOBODY has no face at all
CREDENTIAL = {"u": "me", "p": "secret", "d": "."}


class ScriptedCamera:
    script: list[int] = []
    opened = 0

    def __init__(self, cfg, fps):
        self.fps = fps
        self.frames = cycle([image(k) for k in ScriptedCamera.script])
        self.report = None

    def open(self):
        ScriptedCamera.opened += 1
        self.report = OpenReport(0, 700, 600.0, self.fps, (640, 480))
        return self.report

    def settle(self, **kwargs):
        return SettleReport(3, 80.0, True, "stable", [120.0], next(self.frames))

    def read_frame(self):
        return next(self.frames)

    def set_fps(self, fps):
        self.fps = fps
        return 1.0

    def close(self):
        pass


class Watcher:
    def __init__(self) -> None:
        self.display = "on"
        self.locked = False
        self.handlers = []

    def add_handler(self, handler) -> None:
        self.handlers.append(handler)

    def emit(self, name: str) -> None:
        for handler in self.handlers:
            handler(name, {})


class ServiceTests(unittest.TestCase):
    def setUp(self) -> None:
        # Never let a test touch the real data folder.
        assert str(APP_DIR).startswith(tempfile.gettempdir()), APP_DIR
        shutil.rmtree(APP_DIR, ignore_errors=True)
        APP_DIR.mkdir(parents=True)
        save_profile(FaceProfile.build(np.stack([direction(0), direction(5)]), protected_count=2,
                                       created_at=np.zeros(2)), EMBED_PATH)
        self.cfg = Config(threshold=0.45, adaptive_enrollment=True, unlock_client_check="log")
        self.watcher = Watcher()
        self.service = FaceService(self.cfg, metrics=NullMetrics(),
                                   embedder=FakeEmbedder(ANGLES), watcher=self.watcher)
        self.service.camera = CameraManager(lambda: self.service.cfg, self.service.camera_profile,
                                            self.service.metrics, camera_factory=ScriptedCamera)
        self.service.recognizer.load()
        ScriptedCamera.opened = 0
        self.creds = patch("face_service.service.load_password", return_value=dict(CREDENTIAL))
        self.creds.start()

    def tearDown(self) -> None:
        self.creds.stop()
        self.service.camera.close()

    def unlock(self, script: list[int], client: ClientInfo = SYSTEM) -> dict:
        ScriptedCamera.script = script
        return self.service.handle({"cmd": "unlock"}, client)

    # ---- unlock ------------------------------------------------------------

    def test_credentials_only_after_two_matching_frames(self) -> None:
        response = self.unlock([ME])
        self.assertTrue(response["ok"])
        self.assertEqual(list(response)[:2], ["ok", "username"])  # provider parses "ok" first
        self.assertIn("attempt_id", response)

    def test_stranger_is_refused_without_credentials(self) -> None:
        response = self.unlock([STRANGER])
        self.assertFalse(response["ok"])
        self.assertEqual(response["reason"], "mismatch")
        self.assertNotIn("password", response)

    def test_nobody_in_front_of_the_camera(self) -> None:
        response = self.unlock([NOBODY])
        self.assertEqual(response["reason"], "no_face")
        self.assertNotIn("password", response)

    def test_learning_commits_once_and_only_on_windows_confirmation(self) -> None:
        attempt = self.unlock([ME])["attempt_id"]
        self.assertEqual(load_profile(EMBED_PATH).learned_count, 0)
        first = self.service.handle({"cmd": "unlock_result", "attempt_id": attempt,
                                     "accepted": True}, SYSTEM)
        again = self.service.handle({"cmd": "unlock_result", "attempt_id": attempt,
                                     "accepted": True}, SYSTEM)
        self.assertEqual((first["added"], again["added"]), (1, 0))
        self.assertEqual(load_profile(EMBED_PATH).learned_count, 1)

    def test_rejected_sign_in_learns_nothing(self) -> None:
        attempt = self.unlock([ME])["attempt_id"]
        self.service.handle({"cmd": "unlock_result", "attempt_id": attempt,
                             "accepted": False}, SYSTEM)
        self.assertEqual(load_profile(EMBED_PATH).learned_count, 0)

    def test_malformed_result_is_rejected(self) -> None:
        response = self.service.handle({"cmd": "unlock_result", "attempt_id": 5,
                                        "accepted": "yes"}, SYSTEM)
        self.assertFalse(response["ok"])

    def test_enforced_caller_check_refuses_other_programs(self) -> None:
        self.service.cfg = Config(unlock_client_check="enforce")
        self.assertEqual(self.unlock([ME], USER)["reason"], "caller-not-allowed")
        self.assertTrue(self.unlock([ME], SYSTEM)["ok"])

    def test_logging_caller_check_still_allows(self) -> None:
        self.assertTrue(self.unlock([ME], USER)["ok"])

    def test_missing_credentials(self) -> None:
        with patch("face_service.service.load_password", return_value=None):
            self.assertEqual(self.unlock([ME])["reason"], "no-credentials")

    def test_camera_is_released_after_a_successful_unlock(self) -> None:
        self.unlock([ME])
        self.assertEqual(self.service.camera.status()["state"], "idle")

    def test_display_off_does_not_use_the_camera(self) -> None:
        self.watcher.display = "off"
        with patch("face_service.pacing.MAX_WAIT_S", 0.2):
            response = self.unlock([ME])
        self.assertEqual(response["reason"], "display-off")
        self.assertEqual(ScriptedCamera.opened, 0)

    def test_lock_session_outcomes_are_recorded(self) -> None:
        self.watcher.emit("lock")
        attempt = self.unlock([ME])["attempt_id"]
        self.service.handle({"cmd": "unlock_result", "attempt_id": attempt, "accepted": True},
                            SYSTEM)
        self.watcher.emit("unlock")
        sessions = self.service.metrics.events({"lock_session"})
        self.assertEqual(len(sessions), 1)
        self.assertTrue(sessions[0]["face_accepted"])

    def test_empty_lock_screen_looks_are_counted_not_recorded(self) -> None:
        for _ in range(3):
            self.unlock([NOBODY])
        self.assertEqual(self.service.metrics.events({"unlock_attempt"}), [])
        self.service.camera.release_now()  # the held camera would keep the empty scene
        self.unlock([ME])
        self.assertEqual(len(self.service.metrics.events({"unlock_attempt"})), 1)

    def test_camera_waits_for_the_model_at_start_up(self) -> None:
        self.service._warm.clear()  # the model is still loading
        ScriptedCamera.script = [ME]
        self.assertEqual(self.service.handle({"cmd": "presence"}, USER)["skipped"], "starting")
        with patch("face_service.service.UNLOCK_BUDGET_S", 0.05):
            self.assertEqual(self.unlock([ME])["reason"], "starting")
        self.assertEqual(ScriptedCamera.opened, 0)
        self.service._warm.set()
        self.assertTrue(self.unlock([ME])["ok"])

    # ---- routine ---------------------------------------------------------------

    def lock_and_return(self, minutes: float) -> None:
        wall = [time.time()]
        self.service._wall_clock = lambda: wall[0]
        self.watcher.emit("lock")
        wall[0] += minutes * 60
        attempt = self.unlock([ME])["attempt_id"]
        self.service.handle({"cmd": "unlock_result", "attempt_id": attempt, "accepted": True},
                            SYSTEM)
        self.watcher.emit("unlock")

    def test_each_lock_teaches_the_routine(self) -> None:
        self.lock_and_return(10)
        self.assertEqual(self.service.routine.summary()["locks"], 1.0)
        self.assertEqual(self.service.metrics.events({"lock_session"})[-1]["caught"], "instant")

    def test_time_to_sign_in_counts_from_when_the_camera_starts_looking(self) -> None:
        ticks = iter(range(1000))
        self.service._clock = lambda: next(ticks) * 0.5  # every reading is half a second on
        self.lock_and_return(10)
        self.assertGreater(self.service.metrics.events({"lock_session"})[-1]["time_to_unlock_s"],
                           0.0)

    def test_routine_learning_can_be_turned_off(self) -> None:
        self.service.cfg = Config(unlock_learn_routine=False)
        self.lock_and_return(10)
        self.assertEqual(self.service.routine.summary()["locks"], 0)

    def test_known_routine_replaces_the_fixed_window(self) -> None:
        class Known:
            def schedule(self):
                return object()

            def likely_back(self, now, locked_at):
                return True

        self.service.routine = Known()  # type: ignore[assignment]
        self.service.cfg = Config(unlock_idle_after_s=1800)
        self.assertEqual(self.service._eager_window(), 120.0)
        self.assertTrue(self.service._likely_back())
        self.service.cfg = Config(unlock_idle_after_s=1800, unlock_learn_routine=False)
        self.assertEqual(self.service._eager_window(), 1800.0)
        self.assertFalse(self.service._likely_back())

    def test_forgetting_the_routine(self) -> None:
        self.lock_and_return(10)
        self.assertTrue(self.service.handle({"cmd": "routine_reset"}, USER)["ok"])
        self.assertEqual(self.service.routine.summary()["locks"], 0)

    # ---- presence ------------------------------------------------------------

    def test_presence_check_never_learns(self) -> None:
        ScriptedCamera.script = [ME]
        response = self.service.handle({"cmd": "presence"}, USER)
        self.assertTrue(response["present"])
        self.assertEqual(load_profile(EMBED_PATH).learned_count, 0)

    def test_missing_profile_is_an_error_not_absence(self) -> None:
        EMBED_PATH.unlink()
        self.service.recognizer._profile = None
        ScriptedCamera.script = [ME]
        response = self.service.handle({"cmd": "presence"}, USER)
        self.assertFalse(response["ok"])
        self.assertEqual(response["reason"], "not-enrolled")

    def test_presence_is_skipped_while_the_camera_is_lent_out(self) -> None:
        self.service.handle({"cmd": "pause_camera", "owner": "enrollment", "seconds": 60}, USER)
        response = self.service.handle({"cmd": "presence"}, USER)
        self.assertEqual(response["skipped"], "camera-leased")
        self.assertEqual(ScriptedCamera.opened, 0)

    def test_presence_is_skipped_while_locked(self) -> None:
        self.watcher.locked = True
        self.assertEqual(self.service.handle({"cmd": "presence"}, USER)["skipped"], "locked")

    def test_presence_yields_to_an_active_sign_in_screen(self) -> None:
        self.unlock([NOBODY])
        response = self.service.handle({"cmd": "presence"}, USER)
        self.assertEqual(response["skipped"], "unlock-active")

    # ---- management ----------------------------------------------------------

    def test_status_reports_why_face_sign_in_is_not_ready(self) -> None:
        status = self.service.handle({"cmd": "status"}, USER)
        self.assertFalse(status["ready"])
        self.assertIn("no-credentials", status["not_ready"])
        self.assertIn("camera-unpinned", status["not_ready"])
        self.assertEqual(status["profile"], {"anchors": 2, "learned": 0})

    def test_staging_folder_cannot_escape_the_data_folder(self) -> None:
        for name in ("..", "../x", "a\\b", ""):
            with self.assertRaises(ValueError):
                self.service._staging_dir(name)

    def test_reset_learning_keeps_enrollment(self) -> None:
        attempt = self.unlock([ME])["attempt_id"]
        self.service.handle({"cmd": "unlock_result", "attempt_id": attempt, "accepted": True},
                            SYSTEM)
        response = self.service.handle({"cmd": "learning_reset"}, USER)
        self.assertEqual(response["removed"], 1)
        self.assertEqual(load_profile(EMBED_PATH).protected_count, 2)

    def test_insights_have_no_biometric_data(self) -> None:
        self.unlock([ME])
        insights = self.service.handle({"cmd": "insights"}, USER)
        self.assertTrue(insights["ok"])
        text = repr(insights)
        self.assertNotIn("embedding", text)
        self.assertNotIn("secret", text)

    def test_unknown_command(self) -> None:
        self.assertEqual(self.service.handle({"cmd": "nope"}, USER)["reason"], "unknown-command")

    def test_state_lives_in_the_data_folder(self) -> None:
        self.assertTrue(str(STATE_DIR).startswith(str(APP_DIR)))


if __name__ == "__main__":
    unittest.main()
