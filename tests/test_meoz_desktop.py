import os
import tempfile
import time
import unittest
from dataclasses import replace
from datetime import datetime, timezone
from pathlib import Path

from test_meoz import DAYS, minute_rows

os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")

try:
    from PySide6.QtTest import QTest
    from PySide6.QtWidgets import QApplication

    from chengchuan.application.resampling import calendar_bars, minute_bars
    from chengchuan.application.services import AnalysisService
    from chengchuan.domain.market import MarketFrame
    from chengchuan.domain.models import (
        SHANGHAI,
        IndexRange,
        MarketBar,
        MarketSeries,
        MarketSnapshot,
        SeriesKind,
    )
    from chengchuan.domain.periods import PERIODS, chart_period
    from chengchuan.infrastructure.legacy import LegacyAnalysisEngine, LegacyJsonRepository
    from chengchuan.infrastructure.meoz_market import parse_rows
    from chengchuan.infrastructure.workspace import SqliteWorkspaceRepository
    from chengchuan.ui.tasks import LatestTaskRunner
    from chengchuan.ui.window import MainWindow

    DESKTOP = True
except ImportError:
    DESKTOP = False


class MultiPeriodMarket:
    intervals = tuple(p.code for p in PERIODS)

    def load(self, symbol, interval, refresh=False):
        period = chart_period(interval)
        vwap = ()
        if period.daily:
            bars = tuple(
                MarketBar(datetime(y, 11, 2, 15, tzinfo=SHANGHAI), 10, 200, 2000, 9, 11, 8)
                for y in (2024, 2025, 2026)
            )
            bars = bars if interval == "day" else calendar_bars(bars, interval)
            kind = SeriesKind.OHLC
        else:
            rows = [
                r for d in (DAYS[-1:] if interval == "minute" else DAYS) for r in minute_rows(d)
            ]
            bars, _ = parse_rows(rows, symbol, intraday=True)
            if period.minutes:
                bars = minute_bars(bars, period.minutes)
                kind = SeriesKind.OHLC
            else:
                bars = tuple(replace(b, open=None, high=None, low=None) for b in bars)
                vwap = tuple((b.timestamp, 10) for b in bars)
                kind = SeriesKind.SAMPLED_CLOSE
        series = MarketSeries(symbol, "测试股票", interval, kind, bars)
        return MarketFrame(
            MarketSnapshot(interval, series),
            None,
            datetime.now(timezone.utc),
            provider_name="测试数据源",
            vwap_points=vwap,
        )


@unittest.skipUnless(DESKTOP, "需要desktop依赖")
class MultiPeriodDesktopTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.app = QApplication.instance() or QApplication([])

    def setUp(self):
        temp = tempfile.TemporaryDirectory()
        self.addCleanup(temp.cleanup)
        self.window = MainWindow(
            LegacyJsonRepository(),
            AnalysisService(LegacyAnalysisEngine()),
            SqliteWorkspaceRepository(Path(temp.name) / "workspace.sqlite"),
            market=MultiPeriodMarket(),
            network_runner_factory=LatestTaskRunner,
        )
        self.addCleanup(self.window.close)
        self.window.show()
        self.window.auto_refresh.setChecked(False)

    def wait(self, interval):
        deadline = time.monotonic() + 5
        w = self.window
        while time.monotonic() < deadline:
            self.app.processEvents()
            QTest.qWait(2)
            if (
                w.snapshot
                and w.snapshot.series.interval == interval
                and w.report
                and w.runner.active is None
                and w.market_runner.active is None
            ):
                return
        self.fail("图表切换超时：" + w.market_status.text())

    def test_all_twelve_periods_switch_render_and_analyse_without_day_rule_reuse(self):
        w = self.window
        self.assertEqual(w.period.count(), 12)
        w.open_stock("002318", "day")
        self.wait("day")
        for p in PERIODS:
            w.period.setCurrentIndex(w.period.findData(p.code))
            self.wait(p.code)
            self.assertEqual(w.chart.snapshot.series.interval, p.code)
            self.assertIn(p.label, w.kind_label.text())
            self.assertIn("测试数据源", w.market_status.text())
            if p.code not in ("day", "minute"):
                self.assertEqual(w.report.findings, ())
        w.chart.set_selection(IndexRange(0, 3))
        self.wait("min60")
        self.assertEqual(w.selection, IndexRange(0, 3))

    def test_five_day_ticks_and_vwap_are_cleared_when_switching_to_candles(self):
        w = self.window
        w.open_stock("002318", "five_day")
        self.wait("five_day")
        self.assertEqual(len(w.chart._vwap), 1205)
        self.assertIsNotNone(w.chart.axis._tickLevels)
        w.open_stock("002318", "min5")
        self.wait("min5")
        self.assertEqual(w.chart._vwap, {})
        self.assertIsNone(w.chart.axis._tickLevels)
        self.assertEqual(w.snapshot.series.kind, SeriesKind.OHLC)


if __name__ == "__main__":
    unittest.main()
