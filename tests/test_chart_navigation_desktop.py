import time
import unittest
from dataclasses import replace
from datetime import date

import test_meoz_desktop as harness

DESKTOP = harness.DESKTOP

if DESKTOP:
    from PySide6.QtCore import QEvent, QPointF, Qt
    from PySide6.QtTest import QTest
    from PySide6.QtWidgets import QTableView

    from chengchuan.domain.models import SHANGHAI


class HistoryMarket(harness.MultiPeriodMarket):
    supports_history = True

    def __init__(self):
        self.requests = []

    def load(self, symbol, interval, refresh=False, as_of=None):
        self.requests.append((symbol, interval, refresh, as_of))
        frame = super().load(symbol, interval, refresh)
        bars = tuple(
            b
            for b in frame.snapshot.series.bars
            if as_of is None or b.timestamp.astimezone(SHANGHAI).date() <= as_of
        )
        series = replace(frame.snapshot.series, bars=bars)
        days = {b.timestamp.astimezone(SHANGHAI).date() for b in bars}
        return replace(
            frame,
            as_of=as_of,
            snapshot=replace(frame.snapshot, series=series, revision=f"{interval}:{as_of}"),
            vwap_points=tuple((t, p) for t, p in frame.vwap_points if t.date() in days),
            reference_closes=tuple((d, 10.2) for d in sorted(days)),
        )


@unittest.skipUnless(DESKTOP, "需要desktop依赖")
class ChartNavigationDesktopTests(unittest.TestCase):
    setUpClass = classmethod(harness.MultiPeriodDesktopTests.setUpClass.__func__)
    wait = harness.MultiPeriodDesktopTests.wait

    def setUp(self):
        harness.MultiPeriodDesktopTests.setUp(self)
        self.market = HistoryMarket()
        self.window.market = self.market
        self.window.open_stock("002318", "five_day")
        self.wait("five_day")

    def wait_date(self, target, interval="five_day"):
        deadline = time.monotonic() + 5
        while time.monotonic() < deadline:
            self.app.processEvents()
            QTest.qWait(2)
            w = self.window
            if (
                w.frame
                and w.frame.as_of == target
                and w.snapshot.series.interval == interval
                and w.market_runner.active is None
            ):
                self.wait(interval)
                return
        self.fail("历史日期切换超时")

    def test_calendar_native_click_and_history_refresh_scope(self):
        w = self.window
        calls = len(self.market.requests)
        QTest.mouseClick(w.date_nav.button, Qt.MouseButton.LeftButton)
        self.app.processEvents()
        self.assertTrue(w.date_nav.popup.isVisible())
        calendar = w.date_nav.calendar
        calendar.showPreviousMonth()
        calendar.setCurrentPage(2026, 11)
        self.app.processEvents()
        self.assertEqual(len(self.market.requests), calls)
        table = calendar.findChild(QTableView, "qt_calendar_calendarview")
        # Monday first, November 1 is Sunday; November 3 is the second row Tuesday.
        for row in range(table.model().rowCount()):
            index = table.model().index(row, 1)
            if index.data() == 3:
                QTest.mouseClick(
                    table.viewport(),
                    Qt.MouseButton.LeftButton,
                    pos=table.visualRect(index).center(),
                )
                break
        self.wait_date(date(2026, 11, 3))
        self.assertFalse(w.date_nav.popup.isVisible())
        self.assertFalse(w.auto_refresh.isEnabled())
        calls = len(self.market.requests)
        w.auto_refresh.setChecked(True)
        w._auto_refresh()
        self.app.processEvents()
        self.assertEqual(len(self.market.requests), calls)
        w.refresh_market()
        self.wait_date(date(2026, 11, 3))
        self.assertEqual(self.market.requests[-1][2:], (True, date(2026, 11, 3)))
        w.period.setCurrentIndex(w.period.findData("min5"))
        self.wait_date(date(2026, 11, 3), "min5")
        # A delayed response for a different date must never replace the chart.
        current = w.frame
        w._market_ready(self.market.load("sz002318", "min5", as_of=date(2026, 11, 2)))
        self.assertIs(w.frame, current)
        QTest.mouseClick(w.date_nav.latest, Qt.MouseButton.LeftButton)
        self.wait_date(None, "min5")
        self.assertIsNone(w.history_date)
        self.assertTrue(w.auto_refresh.isEnabled())

    def move_to(self, plot, x_fraction=0.5, y_fraction=0.5):
        rect = plot.getViewBox().sceneBoundingRect()
        point = plot.mapFromScene(
            QPointF(
                rect.left() + rect.width() * x_fraction, rect.top() + rect.height() * y_fraction
            )
        )
        QTest.mouseMove(plot.viewport(), point)
        QTest.qWait(15)
        self.app.processEvents()
        return point

    def test_translucent_crosshair_tracks_both_panes_and_leaves_cleanly(self):
        chart = self.window.chart
        self.move_to(chart.price)
        cursor = chart.cursor
        tip = cursor.tips[chart.price]
        self.assertTrue(cursor.price_x.isVisible())
        self.assertTrue(cursor.price_y.isVisible())
        self.assertTrue(cursor.volume_x.isVisible())
        self.assertIn("2026-", tip.text())
        self.assertIn(":00", tip.text())
        self.assertIn("-1.96%", tip.text())
        self.assertIn("较当日昨收", tip.text())
        self.assertIn("光标价", tip.text())
        self.assertTrue(tip.testAttribute(Qt.WidgetAttribute.WA_TransparentForMouseEvents))
        for x, y in ((0.02, 0.02), (0.98, 0.02), (0.98, 0.98), (0.02, 0.98)):
            self.move_to(chart.price, x, y)
            self.assertTrue(tip.isVisible())
            self.assertTrue(chart.price.viewport().rect().contains(tip.geometry()))
        self.move_to(chart.volume)
        self.assertFalse(cursor.price_y.isVisible())
        self.assertTrue(cursor.volume_y.isVisible())
        self.assertEqual(cursor.price_x.value(), cursor.volume_x.value())
        self.assertFalse(tip.isVisible())
        self.assertIn("成交量", cursor.tips[chart.volume].text())
        self.app.sendEvent(chart.volume.viewport(), QEvent(QEvent.Type.Leave))
        self.assertFalse(any(line.isVisible() for line in cursor.lines))
        self.assertFalse(any(t.isVisible() for t in cursor.tips.values()))

    def test_local_navigation_does_not_make_requests_or_relabel_out_of_range(self):
        w = self.window
        w.online_symbol = None
        calls = len(self.market.requests)
        w.jump_to_date(date(2026, 10, 31))
        self.assertEqual(w.history_date, date(2026, 10, 31))
        self.assertIn("2026-10-30", w.date_nav.hint.text())
        self.assertTrue(w.chart.date_markers[0].isVisible())
        w.jump_to_date(date(2000, 1, 1))
        self.assertIn("超出", w.date_nav.hint.text())
        self.assertEqual(w.history_date, date(2026, 10, 31))
        w.return_to_latest()
        self.assertIsNone(w.history_date)
        self.assertFalse(w.chart.date_markers[0].isVisible())
        self.assertEqual(len(self.market.requests), calls)
