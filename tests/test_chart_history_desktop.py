import tempfile
import threading
import time
import unittest
from dataclasses import replace
from datetime import date, datetime, timedelta, timezone
from pathlib import Path

import test_meoz_desktop as harness

DESKTOP = harness.DESKTOP

if DESKTOP:
    from PySide6.QtCore import QPoint, QPointF, Qt
    from PySide6.QtGui import QWheelEvent
    from PySide6.QtTest import QTest

    from chengchuan.application.resampling import calendar_bars, minute_bars
    from chengchuan.domain.market import MarketFrame
    from chengchuan.domain.models import (
        SHANGHAI,
        IndexRange,
        MarketBar,
        MarketSeries,
        MarketSnapshot,
        SeriesKind,
    )
    from chengchuan.domain.periods import chart_period


class PagingMarket(harness.MultiPeriodMarket):
    supports_history = True

    def __init__(self):
        self.requests = []
        self.release = None
        self.started = threading.Event()

    def load(self, symbol, interval, refresh=False, as_of=None):
        self.requests.append((symbol, interval, refresh, as_of))
        if self.release and as_of and as_of < date(2026, 10, 30):
            self.started.set()
            self.release.wait(3)
        end = as_of or date(2026, 11, 5)
        days = [date(2026, 10, 1) + timedelta(days=i) for i in range(36)]
        days = [d for d in days if d <= end and d.weekday() < 5]
        period = chart_period(interval)
        if not period.daily:
            days = days[-(1 if interval == "minute" else 5) :]
        bars = tuple(
            MarketBar(
                datetime.combine(day, datetime.min.time(), SHANGHAI)
                + timedelta(hours=9, minutes=30 + i),
                10 + i / 1000,
                100,
                1000,
                10,
                11,
                9,
            )
            for day in days
            for i in (range(120) if not period.daily else (0,))
        )
        if period.daily and interval != "day":
            bars = calendar_bars(bars, interval)
        if period.minutes:
            bars = minute_bars(bars, period.minutes)
        kind = SeriesKind.OHLC if period.daily or period.minutes else SeriesKind.SAMPLED_CLOSE
        if kind == SeriesKind.SAMPLED_CLOSE:
            bars = tuple(replace(b, open=None, high=None, low=None) for b in bars)
        series = MarketSeries(symbol, "测试股票", interval, kind, bars)
        return MarketFrame(
            MarketSnapshot(f"{symbol}:{interval}:{end}", series),
            None,
            datetime.now(timezone.utc),
            as_of=as_of,
            reference_closes=tuple((d, 10) for d in days),
            vwap_points=tuple((b.timestamp, 10) for b in bars)
            if kind == SeriesKind.SAMPLED_CLOSE
            else (),
        )


@unittest.skipUnless(DESKTOP, "需要desktop依赖")
class ChartHistoryDesktopTests(unittest.TestCase):
    setUpClass = classmethod(harness.MultiPeriodDesktopTests.setUpClass.__func__)

    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.workspace = harness.SqliteWorkspaceRepository(
            Path(self.temp.name) / "workspace.sqlite"
        )
        self.market = PagingMarket()
        self.window = self.make_window()
        self.addCleanup(self.window.close)

    def make_window(self):
        window = harness.MainWindow(
            harness.LegacyJsonRepository(),
            harness.AnalysisService(harness.LegacyAnalysisEngine()),
            self.workspace,
            market=self.market,
            network_runner_factory=harness.LatestTaskRunner,
        )
        window.resize(1440, 920)
        window.show()
        window.auto_refresh.setChecked(False)
        return window

    def until(self, predicate):
        end = time.monotonic() + 6
        while not predicate():
            self.app.processEvents()
            QTest.qWait(5)
            if time.monotonic() > end:
                self.fail("图表补数超时：" + self.window.status.text())

    def opened(self):
        self.until(
            lambda: (
                self.window.frame is not None
                and self.window.market_runner.active is None
                and self.window.runner.active is None
            )
        )

    def settled(self):
        c = self.window.chart_history
        self.until(lambda: c.active is None and c.runner.active is None and not c.timer.isActive())

    def wheel(self, plot, delta=-120):
        rect = plot.getViewBox().sceneBoundingRect()
        local = plot.mapFromScene(rect.center())
        event = QWheelEvent(
            QPointF(local),
            QPointF(plot.viewport().mapToGlobal(local)),
            QPoint(),
            QPoint(0, delta),
            Qt.MouseButton.NoButton,
            Qt.KeyboardModifier.NoModifier,
            Qt.ScrollPhase.NoScrollPhase,
            False,
        )
        self.app.sendEvent(plot.viewport(), event)
        self.app.processEvents()

    def test_restart_returns_last_stock_latest_date_and_discards_saved_selection(self):
        w = self.window
        w.open_stock("600519", "five_day", as_of=date(2026, 11, 2))
        self.opened()
        self.assertIsNone(w.selection)
        self.assertFalse(w.chart.region.isVisible())
        w.chart.set_selection(IndexRange(3, 9))
        self.opened()
        w.open_stock("index:sh000001", "day")
        self.opened()
        w.close()
        state = self.workspace.load("main")
        self.assertNotIn("selection", state)
        state.update(selection=[3, 9], revision="old", history_date="20261102")
        self.workspace.save("main", state)
        self.window = self.make_window()
        self.addCleanup(self.window.close)
        self.window.restore_last_stock()
        self.opened()
        self.assertEqual(self.market.requests[-1], ("sh600519", "five_day", True, None))
        self.assertEqual(self.window.snapshot.series.bars[-1].timestamp.date(), date(2026, 11, 5))
        self.assertIsNone(self.window.history_date)
        self.assertIsNone(self.window.selection)
        self.assertIsNone(self.window.report)
        self.assertFalse(self.window.chart.region.isVisible())

    def test_wheel_in_price_and_volume_loads_history_and_preserves_manual_selection(self):
        for interval in ("minute", "five_day", "min5"):
            w = self.window
            w.open_stock("002318", interval)
            self.opened()
            bars = w.snapshot.series.bars
            w.chart.set_selection(IndexRange(4, 10))
            self.opened()
            selected = (bars[4].timestamp, bars[9].timestamp)
            before = len(bars)
            plot = w.chart.volume if interval == "five_day" else w.chart.price
            self.wheel(plot)
            self.until(lambda: len(w.snapshot.series.bars) > before)
            self.settled()
            self.opened()
            bars = w.snapshot.series.bars
            self.assertEqual(
                (bars[w.selection.start].timestamp, bars[w.selection.stop - 1].timestamp), selected
            )
            for pane in (w.chart.price, w.chart.volume):
                left, right = pane.getViewBox().viewRange()[0]
                self.assertGreaterEqual(left, -0.50001)
                self.assertLessEqual(right, len(bars) - 0.49999)
            total = len(bars)
            w.refresh_market()
            self.opened()
            self.assertEqual(len(w.snapshot.series.bars), total)

    def test_historical_chart_can_extend_both_sides_but_never_after_today(self):
        w = self.window
        w.open_stock("002318", "five_day", as_of=date(2026, 11, 2))
        self.opened()
        old = w.snapshot.series.bars
        w.chart.history_requested.emit((-150, len(old) + 250))
        self.settled()
        current = w.snapshot.series.bars
        self.assertLess(current[0].timestamp, old[0].timestamp)
        self.assertGreater(current[-1].timestamp, old[-1].timestamp)
        self.assertLessEqual(current[-1].timestamp.date(), datetime.now(SHANGHAI).date())
        self.assertEqual(w.history_date, w.frame.as_of)
        self.assertIsNone(w.selection)

    def test_programmatic_fit_does_not_fetch_and_late_history_is_discarded_on_stock_switch(self):
        w = self.window
        w.open_stock("002318", "five_day")
        self.opened()
        before = len(self.market.requests)
        for _ in range(20):
            w.chart.price.setXRange(-100000, 100000, padding=0)
            w.chart._fit_visible()
            self.app.processEvents()
        QTest.qWait(230)
        self.app.processEvents()
        self.assertEqual(len(self.market.requests), before)
        self.market.release = threading.Event()
        w.chart.history_requested.emit((-500, len(w.snapshot.series.bars)))
        self.until(self.market.started.is_set)
        w.open_stock("600519", "minute")
        self.opened()
        self.market.release.set()
        self.settled()
        self.assertEqual(w.snapshot.series.symbol, "sh600519")
        self.assertEqual(w.snapshot.series.interval, "minute")
        self.assertIsNone(w.selection)

    def test_zoom_back_in_cancels_a_pending_history_page(self):
        w = self.window
        w.open_stock("002318", "five_day")
        self.opened()
        original = w.frame
        self.market.release = threading.Event()
        self.wheel(w.chart.price)
        self.until(self.market.started.is_set)
        self.wheel(w.chart.price, delta=120)
        self.market.release.set()
        self.settled()
        self.assertIs(w.frame, original)
        self.assertFalse(w.chart_history.expanded)
        self.assertIsNone(w.chart_history.desired)

    def test_local_boundaries_keep_both_panes_filled_for_all_daily_periods(self):
        w = self.window
        for interval in ("day", "week", "month", "quarter", "year"):
            w.open_stock("002318", interval)
            self.opened()
            self.wheel(w.chart.price)
            self.settled()
            count = len(w.snapshot.series.bars)
            for pane in (w.chart.price, w.chart.volume):
                left, right = pane.getViewBox().viewRange()[0]
                self.assertGreaterEqual(left, -0.50001)
                self.assertLessEqual(right, count - 0.49999)
            self.assertIsNone(w.selection)
