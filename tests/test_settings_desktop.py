import unittest
from unittest.mock import patch

import test_chart_navigation_desktop as harness
import test_meoz_desktop as base

DESKTOP = harness.DESKTOP
if DESKTOP:
    from PySide6.QtCore import Qt
    from PySide6.QtTest import QTest
    from PySide6.QtWidgets import QDialogButtonBox, QLineEdit


@unittest.skipUnless(DESKTOP, "需要desktop依赖")
class SettingsDesktopTests(unittest.TestCase):
    setUpClass = classmethod(harness.MarketSourceDesktopTests.setUpClass.__func__)
    setUp = harness.MarketSourceDesktopTests.setUp
    wait = harness.MarketSourceDesktopTests.wait

    def open(self):
        QTest.mouseClick(self.window.settings_button, Qt.MouseButton.LeftButton)
        self.app.processEvents()
        self.assertIsNotNone(self.window.settings_dialog)
        return self.window.settings_dialog

    def test_sidebar_settings_navigation_and_cancel_discard_drafts(self):
        w = self.window
        original = w.preferences
        dialog = self.open()
        self.assertEqual(dialog.api_key.text(), "")
        self.assertEqual(dialog.api_key.echoMode(), QLineEdit.EchoMode.Password)
        self.assertIn("已配置", dialog.credential_status.text())
        for i in (1, 2, 0):
            item = dialog.navigation.item(i)
            QTest.mouseClick(
                dialog.navigation.viewport(),
                Qt.MouseButton.LeftButton,
                pos=dialog.navigation.visualItemRect(item).center(),
            )
            self.assertEqual(dialog.pages.currentIndex(), i)
        dialog.refresh_seconds.setValue(60)
        dialog.api_key.setText("nc_" + "a" * 32)
        cleared = []
        dialog.finished.connect(lambda: cleared.append(dialog.api_key.text()))
        dialog.buttons.button(QDialogButtonBox.StandardButton.Cancel).click()
        self.app.processEvents()
        self.assertIsNone(w.settings_dialog)
        self.assertEqual(w.preferences, original)
        self.sources.configure_key.assert_not_called()
        self.assertEqual(cleared, [""])

    def test_save_applies_without_market_reload_and_survives_reopening(self):
        w = self.window
        before = len(w.market.requests)
        dialog = self.open()
        dialog.refresh_seconds.setValue(60)
        dialog.auto_refresh.setChecked(True)
        dialog.show_crosshair.setChecked(False)
        dialog.show_tooltip.setChecked(False)
        dialog.opacity.setValue(45)
        dialog.save_button.click()
        self.app.processEvents()
        self.assertIsNone(w.settings_dialog)
        self.assertEqual(w.refresh_timer.interval(), 60000)
        self.assertTrue(w.auto_refresh.isChecked())
        self.assertFalse(w.chart.cursor.show_crosshair)
        self.assertFalse(w.chart.cursor.show_tooltip)
        self.assertIn("115)", w.chart.cursor.tips[w.chart.price].styleSheet())
        self.assertEqual(len(w.market.requests), before)
        w.chart.cursor.move(w.chart.price.getViewBox().sceneBoundingRect().center(), w.chart.price)
        self.assertFalse(any(line.isVisible() for line in w.chart.cursor.lines))
        self.assertFalse(any(tip.isVisible() for tip in w.chart.cursor.tips.values()))
        reopened = self.open()
        self.assertEqual(reopened.refresh_seconds.value(), 60)
        self.assertFalse(reopened.show_crosshair.isChecked())
        reopened.reject()
        saved = w.preferences
        w.close()
        # Recreate the full window against the same SQLite preferences.
        restored = base.MainWindow(
            base.LegacyJsonRepository(),
            base.AnalysisService(base.LegacyAnalysisEngine()),
            self.workspace,
            market=self.sources.connect("tencent"),
            market_sources=self.sources,
            network_runner_factory=base.LatestTaskRunner,
        )
        try:
            self.assertEqual(restored.preferences, saved)
            self.assertEqual(restored.refresh_timer.interval(), 60000)
            self.assertFalse(restored.chart.cursor.show_tooltip)
        finally:
            restored.close()

    def test_invalid_key_retry_and_text_entry_switch_to_meoz(self):
        w = self.window
        dialog = self.open()
        dialog.api_key.setText("invalid")
        dialog.save_button.click()
        self.assertIn("格式无效", dialog.error.text())
        self.assertEqual(w.source_box.currentData(), "tencent")
        self.sources.configure_key.assert_not_called()
        dialog.api_key.clear()
        key = "nc_" + "a" * 32
        QTest.keyClicks(dialog.api_key, key)
        self.assertEqual(dialog.provider.currentData(), "meoz")
        dialog.save_button.click()
        self.wait("day")
        self.assertEqual(w.source_box.currentData(), "meoz")
        self.sources.configure_key.assert_called_once_with(key)
        self.assertNotIn(key, str(self.workspace.load("preferences")))

    def test_single_settings_window_and_parent_close_clear_pending_secret(self):
        dialog = self.open()
        dialog.api_key.setText("nc_" + "a" * 32)
        self.window.configure_market_key()
        self.assertIs(self.window.settings_dialog, dialog)
        self.assertTrue(dialog.api_key.text())
        cleared = []
        dialog.finished.connect(lambda: cleared.append(dialog.api_key.text()))
        self.window.close()
        self.app.processEvents()
        self.assertEqual(cleared, [""])
        self.sources.configure_key.assert_not_called()

    def test_partial_key_save_failure_clears_secret_and_keeps_dialog_open(self):
        dialog = self.open()
        dialog.api_key.setText("nc_" + "a" * 32)
        with patch.object(self.workspace, "save", side_effect=OSError("disk full")):
            dialog.save_button.click()
        self.assertIn("API Key已保存", dialog.error.text())
        self.assertEqual(dialog.api_key.text(), "")
        self.assertIs(self.window.settings_dialog, dialog)
        self.sources.configure_key.assert_called_once()
        self.assertIsNone(self.workspace.load("preferences").get("api_key"))

    def test_explicit_startup_provider_is_reflected_in_settings(self):
        restored = base.MainWindow(
            base.LegacyJsonRepository(),
            base.AnalysisService(base.LegacyAnalysisEngine()),
            self.workspace,
            market=self.sources.connect("tencent"),
            market_sources=self.sources,
            network_runner_factory=base.LatestTaskRunner,
            initial_provider="tencent",
        )
        try:
            restored.open_settings()
            self.assertEqual(restored.settings_dialog.provider.currentData(), "tencent")
        finally:
            restored.close()
