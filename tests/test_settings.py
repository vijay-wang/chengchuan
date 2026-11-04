import tempfile
import unittest
from dataclasses import replace
from pathlib import Path
from unittest.mock import Mock

from chengchuan.application.settings import AppSettings, SettingsSaveError, SettingsService
from chengchuan.infrastructure.workspace import SqliteWorkspaceRepository


class SettingsTests(unittest.TestCase):
    def setUp(self):
        folder = tempfile.TemporaryDirectory()
        self.addCleanup(folder.cleanup)
        self.repo = SqliteWorkspaceRepository(Path(folder.name) / "state.sqlite")
        self.sources = Mock()
        self.service = SettingsService(self.repo, self.sources)

    def test_old_workspace_migrates_without_write_and_roundtrips_preferences(self):
        migrated = self.service.load({"provider": "tencent", "auto_refresh": False})
        self.assertEqual(migrated.provider, "tencent")
        self.assertFalse(migrated.auto_refresh)
        self.assertEqual(self.repo.load("preferences"), {})
        settings = replace(migrated, refresh_seconds=60, show_crosshair=False, tooltip_opacity=45)
        self.service.save(settings)
        self.assertEqual(self.service.load(), settings)
        self.sources.configure_key.assert_not_called()

    def test_settings_bounds_and_future_versions_are_rejected(self):
        for values in (
            {"refresh_seconds": 0},
            {"refresh_seconds": True},
            {"show_tooltip": 1},
            {"tooltip_opacity": 100},
            {"provider": "unknown"},
        ):
            with self.assertRaises(ValueError):
                AppSettings(**values)
        self.repo.save("preferences", {"version": 99})
        with self.assertRaisesRegex(ValueError, "其他版本"):
            self.service.load()
        self.assertEqual(self.repo.load("preferences"), {"version": 99})

    def test_blank_key_preserves_credential_and_new_key_never_enters_workspace(self):
        settings = AppSettings(provider="meoz")
        self.service.save(settings, "  ")
        self.sources.configure_key.assert_not_called()
        key = "nc_" + "a" * 32
        self.service.save(settings, key)
        self.sources.configure_key.assert_called_once_with(key)
        stored = self.repo.load("preferences")
        self.assertEqual(
            set(stored),
            {
                "version",
                "provider",
                "auto_refresh",
                "refresh_seconds",
                "show_crosshair",
                "show_tooltip",
                "tooltip_opacity",
            },
        )
        self.assertNotIn(key, str(stored))

    def test_invalid_key_has_no_partial_writes(self):
        original = AppSettings()
        self.service.save(original)
        with self.assertRaisesRegex(SettingsSaveError, "格式无效"):
            self.service.save(replace(original, refresh_seconds=60), "invalid")
        self.sources.configure_key.assert_not_called()
        self.assertEqual(self.service.load(), original)

    def test_credential_failure_does_not_save_preferences_or_echo_secret(self):
        key = "nc_" + "a" * 32
        self.sources.configure_key.side_effect = OSError(key)
        with self.assertRaises(SettingsSaveError) as raised:
            self.service.save(AppSettings(), key)
        self.assertNotIn(key, str(raised.exception))
        self.assertFalse(raised.exception.key_saved)
        self.assertEqual(self.repo.load("preferences"), {})

    def test_workspace_failure_after_key_save_is_reported_as_partial(self):
        repo = Mock()
        repo.save.side_effect = OSError("disk full")
        service = SettingsService(repo, self.sources)
        with self.assertRaises(SettingsSaveError) as raised:
            service.save(AppSettings(), "nc_" + "a" * 32)
        self.assertTrue(raised.exception.key_saved)
        self.assertIn("API Key已保存", str(raised.exception))
        self.sources.configure_key.assert_called_once()

    def test_missing_optional_source_status_has_a_recoverable_message(self):
        self.sources.settings_info.side_effect = OSError("private details")
        self.assertIn("暂不可读取", self.service.info().credential_status)
        self.assertNotIn("private details", self.service.info().credential_status)

    def test_non_object_preferences_are_reported_without_overwrite(self):
        self.repo.save("preferences", [])
        with self.assertRaisesRegex(ValueError, "设置内容无效"):
            self.service.load()
        self.assertEqual(self.repo.load("preferences"), [])
