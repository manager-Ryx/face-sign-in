from __future__ import annotations

import tempfile
import unittest
from pathlib import Path

import _isolation  # noqa: F401

from face_service.config import Config, Kind, load_config, save_config, settings_specs


class ConfigLoadingTests(unittest.TestCase):
    def setUp(self) -> None:
        self.dir = tempfile.TemporaryDirectory()
        self.path = Path(self.dir.name) / "config.toml"
        self.last_good = Path(self.dir.name) / "config.last-good.toml"

    def tearDown(self) -> None:
        self.dir.cleanup()

    def write(self, text: str) -> None:
        self.path.write_text(text, encoding="utf-8")

    def test_valid_file_loads_without_problems(self) -> None:
        self.write("threshold = 0.5\npresence_interval_s = 300\npersistent_camera = false\n")
        result = load_config(self.path, self.last_good)
        self.assertEqual(result.problems, [])
        self.assertEqual(result.config.threshold, 0.5)

    def test_out_of_range_threshold_cannot_weaken_recognition(self) -> None:
        self.write("threshold = 1.9\n")
        result = load_config(self.path, self.last_good)
        self.assertEqual(result.config.threshold, Config().threshold)
        self.assertTrue(any("threshold" in p for p in result.problems))

    def test_wrong_type_falls_back_and_is_reported(self) -> None:
        self.write('anti_spoofing = "yes"\nthreshold = "0.4"\n')
        result = load_config(self.path, self.last_good)
        self.assertEqual(result.config.anti_spoofing, Config().anti_spoofing)
        self.assertEqual(result.config.threshold, Config().threshold)
        self.assertEqual(len(result.problems), 2)

    def test_invalid_value_falls_back_to_last_working_settings(self) -> None:
        save_config(Config(threshold=0.42), self.path, self.last_good)
        self.write("threshold = 5.0\n")
        result = load_config(self.path, self.last_good)
        self.assertEqual(result.config.threshold, 0.42)

    def test_inconsistent_group_falls_back_together(self) -> None:
        self.write("unlock_verify_frames = 2\nunlock_verify_required = 3\n")
        result = load_config(self.path, self.last_good)
        cfg = result.config
        self.assertLessEqual(cfg.unlock_verify_required, cfg.unlock_verify_frames)

    def test_unparseable_file_uses_last_working_settings(self) -> None:
        save_config(Config(presence_interval_s=120), self.path, self.last_good)
        self.write("threshold = = 0.4\n")
        result = load_config(self.path, self.last_good)
        self.assertEqual(result.source, "defaults")
        self.assertEqual(result.config.presence_interval_s, 120)

    def test_retired_learning_from_failed_attempts_is_ignored_quietly(self) -> None:
        self.write("adaptive_pre_auth_enabled = true\nadaptive_pre_auth_ttl_s = 45.0\n")
        result = load_config(self.path, self.last_good)
        self.assertEqual(result.problems, [])
        self.assertFalse(hasattr(result.config, "adaptive_pre_auth_enabled"))

    def test_unknown_key_is_reported_not_fatal(self) -> None:
        self.write("thresold = 0.3\n")
        result = load_config(self.path, self.last_good)
        self.assertTrue(any("thresold" in p for p in result.problems))

    def test_save_round_trip(self) -> None:
        cfg = Config(threshold=0.47, presence_interval_s=600, theme="dark")
        save_config(cfg, self.path, self.last_good)
        self.assertEqual(load_config(self.path, self.last_good).config, cfg)
        self.assertTrue(self.last_good.exists())

    def test_save_refuses_invalid_values(self) -> None:
        with self.assertRaises(ValueError):
            save_config(Config(threshold=0.9), self.path, self.last_good)
        self.assertFalse(self.path.exists())


class ConfigRulesTests(unittest.TestCase):
    def test_every_setting_is_classified(self) -> None:
        specs = settings_specs()
        self.assertTrue(all(isinstance(s.kind, Kind) and s.summary for s in specs.values()))
        self.assertEqual(specs["threshold"].kind, Kind.SAFEGUARD)
        self.assertEqual(specs["presence_absence_timeout_s"].kind, Kind.PREFERENCE)

    def test_unlock_vote_must_be_bounded(self) -> None:
        with self.assertRaises(ValueError):
            Config(unlock_verify_frames=2, unlock_verify_required=3).validate()

    def test_learning_margin_must_be_below_threshold(self) -> None:
        with self.assertRaises(ValueError):
            Config(threshold=0.20, adaptive_match_margin=0.20).validate()

    def test_retention_days_cannot_be_negative(self) -> None:
        with self.assertRaises(ValueError):
            Config(adaptive_retention_days=-1.0).validate()

    def test_legacy_strikes_are_only_the_zero_timeout_fallback(self) -> None:
        legacy = Config(presence_interval_s=60, presence_absent_strikes=3)
        explicit = Config(presence_interval_s=120, presence_absent_strikes=99,
                          presence_absence_timeout_s=600)
        self.assertEqual(legacy.effective_presence_absence_timeout_s, 180)
        self.assertEqual(explicit.effective_presence_absence_timeout_s, 600)

    def test_safe_defaults(self) -> None:
        cfg = Config()
        self.assertFalse(cfg.persistent_camera)
        self.assertEqual(cfg.adaptive_samples_per_unlock, 1)
        self.assertFalse(cfg.adaptive_keep_photos)
        self.assertFalse(cfg.auto_tighten_threshold)
        self.assertFalse(cfg.absence_learning)
        self.assertFalse(cfg.adaptive_lock)


if __name__ == "__main__":
    unittest.main()
