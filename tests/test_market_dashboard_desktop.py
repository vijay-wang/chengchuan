import tempfile
import threading
import time
import unittest
from dataclasses import replace
from datetime import date, datetime
from pathlib import Path

import test_chart_navigation_desktop as navigation
import test_meoz_desktop as harness

from chengchuan.domain.market import INDEX_CATALOG, is_index
from chengchuan.domain.market_dashboard import (
    IndexSummary,
    MarketBreadth,
    MarketOverview,
    QuoteDetails,
    QuoteMetric,
)
from chengchuan.domain.models import SHANGHAI

DESKTOP = harness.DESKTOP
NOW = datetime(2026, 11, 4, 18, tzinfo=SHANGHAI)
if DESKTOP:
    from PySide6.QtCore import QPointF, Qt
    from PySide6.QtTest import QTest


class DashboardMarket(navigation.HistoryMarket):
    supports_overview = True
    supports_details = True

    def __init__(self):
        super().__init__()
        self.overview_calls = 0
        self.detail_calls = []
        self.details_release = None

    def load_overview(self, refresh=False):
        self.overview_calls += 1
        return MarketOverview(
            tuple(
                IndexSummary(s, n, NOW.date(), 3000, 2990, 1000, NOW) for s, n, _ in INDEX_CATALOG
            ),
            (),
            NOW,
        )

    def load(self, symbol, interval, refresh=False, as_of=None):
        self.requests.append((symbol, interval, refresh, as_of))
        frame = harness.MultiPeriodMarket.load(self, "sz002318", interval, refresh)
        bars = tuple(
            b for b in frame.snapshot.series.bars if as_of is None or b.timestamp.date() <= as_of
        )
        series = replace(
            frame.snapshot.series,
            symbol=symbol,
            name="测试指数" if is_index(symbol) else "测试股票",
            bars=bars,
        )
        days = {b.timestamp.date() for b in bars}
        return replace(
            frame,
            as_of=as_of,
            snapshot=replace(
                frame.snapshot, series=series, revision=f"{symbol}:{interval}:{as_of}"
            ),
            vwap_points=tuple((t, v) for t, v in frame.vwap_points if t.date() in days),
        )

    def load_details(self, symbol, as_of=None, refresh=False):
        self.detail_calls.append((symbol, as_of))
        if self.details_release:
            self.details_release.wait(3)
        day = as_of or NOW.date()
        metrics = (
            QuoteMetric(
                "close",
                "最新点位" if is_index(symbol) else "最新价",
                3000 if is_index(symbol) else 16,
                "点" if is_index(symbol) else "元",
            ),
            QuoteMetric(
                "pre_close",
                "昨收",
                2990 if is_index(symbol) else 16.5,
                "点" if is_index(symbol) else "元",
            ),
            QuoteMetric("volume_ratio", "量比", 1.2, "倍"),
            QuoteMetric("turnover_rate", "换手率", None if is_index(symbol) else 0, "%"),
        )
        breadth = (
            (MarketBreadth("all", "全市场A股（源范围）", day, 1, 1, 0, 1, 3, None, 0),)
            if is_index(symbol)
            else ()
        )
        return QuoteDetails(symbol, day, metrics, NOW, as_of, breadth=breadth)


@unittest.skipUnless(DESKTOP, "需要desktop依赖")
class MarketDashboardDesktopTests(unittest.TestCase):
    setUpClass = classmethod(harness.MultiPeriodDesktopTests.setUpClass.__func__)

    def setUp(self):
        temp = tempfile.TemporaryDirectory()
        self.addCleanup(temp.cleanup)
        self.market = DashboardMarket()
        self.window = harness.MainWindow(
            harness.LegacyJsonRepository(),
            harness.AnalysisService(harness.LegacyAnalysisEngine()),
            harness.SqliteWorkspaceRepository(Path(temp.name) / "state.sqlite"),
            market=self.market,
            network_runner_factory=harness.LatestTaskRunner,
        )
        self.addCleanup(self.window.close)
        self.window.show()
        if self.app.platformName() == "xcb":
            self.window.raise_()
            self.window.activateWindow()
            self.assertTrue(QTest.qWaitForWindowActive(self.window, 1000))
        self.window.auto_refresh.setChecked(False)
        self.window.open_stock("000001", "minute")
        self.wait("sz000001", "minute")

    def until(self, predicate):
        deadline = time.monotonic() + 6
        while not predicate():
            self.app.processEvents()
            QTest.qWait(2)
            if time.monotonic() > deadline:
                self.fail("摘要／指标GUI超时")

    def wait(self, symbol, interval):
        w = self.window
        self.until(
            lambda: (
                w.frame
                and w.snapshot.series.symbol == symbol
                and w.snapshot.series.interval == interval
                and w.report
                and w.runner.active is None
                and w.market_runner.active is None
                and w.details_runner.active is None
                and w.overview_runner.active is None
            )
        )

    def test_index_summary_click_keeps_period_and_never_opens_bank(self):
        w = self.window
        self.assertEqual(len(w.index_bar.buttons), 6)
        self.assertEqual(w.index_bar.prices["index:sh000001"].text(), "3,000.00")
        QTest.mouseClick(w.index_bar.buttons["index:sh000001"], Qt.MouseButton.LeftButton)
        self.wait("index:sh000001", "minute")
        self.assertEqual(w.quote_panel.values["pre_close"].text(), "2,990.00点")
        self.assertTrue(w.index_bar.buttons["index:sh000001"].isChecked())
        for interval in ("min15", "week", "quarter"):
            w.period.setCurrentIndex(w.period.findData(interval))
            self.wait("index:sh000001", interval)
            self.assertEqual(w.quote_panel.values["volume_ratio"].text(), "1.20倍")
        w.open_stock("000001", "day")
        self.wait("sz000001", "day")
        self.assertEqual(w.quote_panel.values["turnover_rate"].text(), "0.00%")

    def test_late_index_details_cannot_overwrite_new_stock(self):
        w = self.window
        release = threading.Event()
        self.market.details_release = release
        self.addCleanup(release.set)
        w.open_stock("index:sh000001", "day")
        self.until(lambda: ("index:sh000001", None) in self.market.detail_calls)
        w.open_stock("000001", "day")
        release.set()
        self.wait("sz000001", "day")
        self.assertEqual(w.quote_panel.values["close"].text(), "16.00元")
        self.assertEqual(w.snapshot.series.symbol, "sz000001")

    def test_index_scope_missing_values_and_ctrl_drag_return_to_analysis(self):
        w = self.window
        w.open_stock("index:sh000001", "min15")
        self.wait("index:sh000001", "min15")
        self.assertEqual(w.quote_panel.values["turnover_rate"].text(), "—")
        self.assertIn("并非该指数成分股", w.quote_panel.scope_note.text())
        self.assertIn("主力净额 —", w.quote_panel.breadth.text())
        self.assertIs(w.research_tabs.currentWidget(), w.quote_panel)
        view = w.chart.price
        vb = view.getViewBox()
        y = sum(vb.viewRange()[1]) / 2
        start = view.mapFromScene(vb.mapViewToScene(QPointF(10, y)))
        end = view.mapFromScene(vb.mapViewToScene(QPointF(16, y)))
        QTest.mousePress(
            view.viewport(), Qt.MouseButton.LeftButton, Qt.KeyboardModifier.ControlModifier, start
        )
        QTest.mouseMove(view.viewport(), end)
        QTest.mouseRelease(
            view.viewport(), Qt.MouseButton.LeftButton, Qt.KeyboardModifier.ControlModifier, end
        )
        self.wait("index:sh000001", "min15")
        self.assertIs(w.research_tabs.currentWidget(), w.analysis_view)
        self.assertEqual(w.report.findings, ())
        self.assertIsNone(next(m.value for m in w.report.metrics if m.key == "vwap"))

    def test_historical_metrics_keep_their_date_and_local_import_cancels_online_context(self):
        w = self.window
        w.open_stock("index:sh000001", "min15")
        self.wait("index:sh000001", "min15")
        w.jump_to_date(date(2026, 11, 3))
        self.wait("index:sh000001", "min15")
        self.assertIn("2026-11-03", w.quote_panel.caption.text())
        self.assertIn("2026-11-03", w.quote_panel.scope_note.text())
        self.assertIn("2026-11-04", w.index_bar.status.text())
        calls = self.market.overview_calls
        w.refresh_market()
        self.wait("index:sh000001", "min15")
        self.assertEqual(self.market.overview_calls, calls)
        root = Path(__file__).resolve().parents[1]
        w.open_snapshot(root / "reports/jiuli_20261102_full_day.dataset.json")
        self.until(lambda: w.snapshot and w.runner.active is None)
        self.assertIsNone(w.online_symbol)
        self.assertIs(w.research_tabs.currentWidget(), w.analysis_view)
        self.assertTrue(all(label.text() == "—" for label in w.quote_panel.values.values()))
