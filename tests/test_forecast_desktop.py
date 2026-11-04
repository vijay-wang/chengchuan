import os
import tempfile
import time
import unittest
from dataclasses import replace
from datetime import date
from pathlib import Path

from test_forecast import synthetic_snapshot

os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")

try:
    from PySide6.QtCore import QDate
    from PySide6.QtTest import QTest
    from PySide6.QtWidgets import QApplication

    from chengchuan.application.forecast import ForecastService
    from chengchuan.application.forecast_features import DailyFeatureExtractor, bar_date
    from chengchuan.application.services import AnalysisService
    from chengchuan.domain.analogue import AnalogueModel
    from chengchuan.infrastructure.forecast_input import ForecastSnapshotRepository
    from chengchuan.infrastructure.forecast_journal import SqliteForecastJournal
    from chengchuan.infrastructure.forecast_reports import write_run
    from chengchuan.infrastructure.forecast_stage import LegacyStageProvider
    from chengchuan.infrastructure.legacy import LegacyAnalysisEngine
    from chengchuan.infrastructure.workspace import SqliteWorkspaceRepository
    from chengchuan.ui.forecast_panel import ForecastPanel
    from chengchuan.ui.window import MainWindow

    DESKTOP = True
except ImportError:
    DESKTOP = False


@unittest.skipUnless(DESKTOP, "需要desktop可选依赖")
class ForecastDesktopTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.app = QApplication.instance() or QApplication([])

    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.root = Path(self.temp.name)
        self.snapshot = synthetic_snapshot(130)
        self.service = ForecastService(
            DailyFeatureExtractor(LegacyStageProvider()),
            AnalogueModel(),
            SqliteForecastJournal(self.root / "forecast.sqlite"),
        )
        self.panel = ForecastPanel(self.service, report_writer=write_run)
        self.addCleanup(self.panel.shutdown)
        self.addCleanup(self.panel.close)
        self.panel.show()
        self.panel.set_context(self.snapshot)

    def wait_until(self, predicate, timeout=15):
        deadline = time.monotonic() + timeout
        while not predicate() and time.monotonic() < deadline:
            self.app.processEvents()
            QTest.qWait(5)
        self.assertTrue(predicate(), self.panel.status.text())

    def test_process_prediction_backtest_and_background_export(self):
        self.panel.predict_button.click()
        self.wait_until(lambda: self.panel.run is not None)
        self.assertEqual(len(self.panel.run.predictions), 2)
        self.assertTrue(all(p.model_id for p in self.panel.run.predictions))
        self.assertIn("较前2日均量", self.panel.output.toPlainText())
        self.assertTrue(all(p.calibrated_probabilities is None for p in self.panel.run.predictions))
        self.panel.before.setDate(QDate(2026, 11, 3))
        self.panel.validate_button.click()
        self.wait_until(lambda: self.panel.run is not None)
        run = self.panel.run
        self.assertEqual(len(run.summaries), 6)
        self.assertGreater(self.panel.table.rowCount(), 0)
        self.assertIn("同样本全猜涨", self.panel.output.toPlainText())
        self.panel.export_requested(str(self.root / "report"))
        self.wait_until(lambda: "已导出" in self.panel.status.text())
        self.assertIs(self.panel.run, run)
        for suffix in ("json", "md", "csv"):
            self.assertGreater((self.root / f"report.{suffix}").stat().st_size, 100)

    def test_quote_refresh_preserves_result_but_stock_switch_clears_it(self):
        self.panel.predict()
        self.wait_until(lambda: self.panel.run is not None)
        run = self.panel.run
        refreshed = replace(self.snapshot, revision="quote-refresh")
        self.panel.set_context(refreshed)
        self.assertIs(self.panel.run, run)
        self.panel.set_selection_date(bar_date(self.snapshot.series.bars[-5]))
        self.panel.follow.setChecked(True)
        self.assertEqual(self.panel.asof.date().toPython(), bar_date(self.snapshot.series.bars[-5]))
        self.assertIsNone(self.panel.run)
        changed = replace(self.snapshot, series=replace(self.snapshot.series, symbol="sh600519"))
        self.panel.set_context(changed)
        self.assertIsNone(self.panel.run)
        self.assertEqual(self.panel.table.rowCount(), 0)
        self.panel.set_context(None)
        self.assertFalse(self.panel.predict_button.isEnabled())

    def test_main_window_has_prediction_tab_and_closes_worker(self):
        window = MainWindow(
            ForecastSnapshotRepository(),
            AnalysisService(LegacyAnalysisEngine()),
            SqliteWorkspaceRepository(self.root / "workspace.sqlite"),
            forecast_service=self.service,
            forecast_writer=write_run,
        )
        try:
            window.show()
            window._show_snapshot(self.snapshot)
            self.assertIsNotNone(window.forecast_panel)
            self.assertTrue(window.forecast_panel.predict_button.isEnabled())
            window.forecast_panel.set_selection_date(date(2026, 8, 10))
            window.forecast_panel.predict()
            started = time.monotonic()
            window.close()
            self.assertLess(time.monotonic() - started, 1)
            self.assertIsNone(window.forecast_panel.runner.active)
        finally:
            window.close()


if __name__ == "__main__":
    unittest.main()
