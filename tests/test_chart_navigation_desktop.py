import tempfile
import time
import unittest
from dataclasses import replace
from datetime import date
from pathlib import Path
from unittest.mock import Mock, patch

import test_meoz_desktop as harness

DESKTOP = harness.DESKTOP

if DESKTOP:
    from PySide6.QtCore import QEvent, QObject, QPointF, Qt
    from PySide6.QtGui import QMouseEvent
    from PySide6.QtTest import QTest
    from PySide6.QtWidgets import QLineEdit, QTableView

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
        if self.app.platformName() == "xcb":
            self.window.raise_()
            self.window.activateWindow()
            self.assertTrue(QTest.qWaitForWindowActive(self.window, 1000))
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
        # Remote X11 cursor warping need not dispatch a native movement event.
        # Exercise the actual Qt viewport/scene path deterministically instead.
        event = QMouseEvent(
            QEvent.Type.MouseMove,
            QPointF(point),
            QPointF(plot.viewport().mapToGlobal(point)),
            Qt.MouseButton.NoButton,
            Qt.MouseButton.NoButton,
            Qt.KeyboardModifier.NoModifier,
        )
        self.app.sendEvent(plot.viewport(), event)
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

    def test_bottom_edge_sweeps_and_leave_repaint_both_linked_viewports(self):
        class PaintRecorder(QObject):
            def __init__(self, viewport):
                super().__init__(viewport)
                self.rects = []
                viewport.installEventFilter(self)

            def eventFilter(self, watched, event):
                if event.type() == QEvent.Type.Paint:
                    self.rects.append(event.rect())
                return False

        chart = self.window.chart
        plots = (chart.price, chart.volume)
        recorders = [PaintRecorder(plot.viewport()) for plot in plots]
        for plot in plots:
            for i in range(20):
                self.move_to(plot, 0.05 + 0.9 * i / 19, 0.98)
            self.app.sendEvent(plot.viewport(), QEvent(QEvent.Type.Leave))
            self.app.processEvents()
            QTest.qWait(20)
        self.assertFalse(any(line.isVisible() for line in chart.cursor.lines))
        self.assertFalse(any(tip.isVisible() for tip in chart.cursor.tips.values()))
        for plot, recorder in zip(plots, recorders):
            self.assertTrue(
                any(r.contains(plot.viewport().rect()) for r in recorder.rects),
                "旧光标与底部边缘必须得到完整重绘",
            )


class FakeSources:
    choices = (("meoz", "猫爪 · 12周期"), ("tencent", "腾讯 · 基础行情"))

    def __init__(self):
        self.markets = {}
        self.configure_key = Mock()
        for code in ("tencent", "meoz"):
            market = HistoryMarket()
            market.source_id = code
            market.supports_history = code == "meoz"
            market.connection_notice = (
                "本机未配置猫爪，请配置" if code == "meoz" else "仅分时／日K，请切换猫爪"
            )
            if code == "tencent":
                market.intervals = ("day", "minute")
            self.markets[code] = market

    def connect(self, code):
        return self.markets[code]


@unittest.skipUnless(DESKTOP, "需要desktop依赖")
class MarketSourceDesktopTests(unittest.TestCase):
    setUpClass = classmethod(harness.MultiPeriodDesktopTests.setUpClass.__func__)
    wait = harness.MultiPeriodDesktopTests.wait

    def setUp(self):
        temp = tempfile.TemporaryDirectory()
        self.addCleanup(temp.cleanup)
        self.sources = FakeSources()
        self.workspace = harness.SqliteWorkspaceRepository(Path(temp.name) / "state.sqlite")
        self.window = harness.MainWindow(
            harness.LegacyJsonRepository(),
            harness.AnalysisService(harness.LegacyAnalysisEngine()),
            self.workspace,
            market=self.sources.connect("tencent"),
            market_sources=self.sources,
            network_runner_factory=harness.LatestTaskRunner,
        )
        self.addCleanup(self.window.close)
        self.window.show()
        if self.app.platformName() == "xcb":
            self.window.raise_()
            self.window.activateWindow()
            self.assertTrue(QTest.qWaitForWindowActive(self.window, 1000))
        self.window.auto_refresh.setChecked(False)
        self.window.open_stock("002318", "day")
        self.wait("day")

    def test_source_switch_unlocks_all_periods_and_keeps_cache_and_browser_context_separate(self):
        w = self.window
        self.assertEqual(w.period.count(), 12)
        self.assertIn("仅分时／日K", w.source_hint.text())
        item = w.period.model().item(w.period.findData("min15"))
        self.assertFalse(item.isEnabled())
        self.assertIn("切换猫爪", item.toolTip())
        before = len(w.market.requests)
        w.period.setCurrentIndex(w.period.findData("min15"))
        self.assertEqual(w.period.currentData(), "day")
        self.assertEqual(len(w.market.requests), before)
        w.source_box.setCurrentIndex(w.source_box.findData("meoz"))
        self.wait("day")
        self.assertIs(w.browser.market, w.market)
        self.assertTrue(all(w.period.model().item(i).isEnabled() for i in range(12)))
        for interval in ("five_day", "week", "year", "min15", "min60"):
            w.period.setCurrentIndex(w.period.findData(interval))
            self.wait(interval)
            self.assertEqual(w.snapshot.series.interval, interval)
        w.source_box.setCurrentIndex(w.source_box.findData("tencent"))
        self.wait("day")
        self.assertEqual(w.period.currentData(), "day")
        self.assertIs(w.browser.market, self.sources.markets["tencent"])
        w.close()
        self.assertEqual(self.workspace.load("main")["provider"], "tencent")

    def test_password_configuration_cancel_failure_and_success(self):
        w = self.window
        with patch("chengchuan.ui.window.QInputDialog.getText", return_value=("", False)):
            w.configure_market_key()
        self.sources.configure_key.assert_not_called()
        value = "nc_" + "a" * 32
        self.sources.configure_key.side_effect = ValueError("invalid")
        with patch(
            "chengchuan.ui.window.QInputDialog.getText", return_value=(value, True)
        ) as dialog:
            w.configure_market_key()
        self.assertEqual(dialog.call_args.args[-1], QLineEdit.EchoMode.Password)
        self.assertEqual(w.source_box.currentData(), "tencent")
        self.assertIn("保存失败", w.source_hint.text())
        self.assertNotIn(value, w.source_hint.text())
        self.sources.configure_key.side_effect = None
        with patch("chengchuan.ui.window.QInputDialog.getText", return_value=(value, True)):
            w.configure_market_key()
        self.wait("day")
        self.sources.configure_key.assert_called_with(value)
        self.assertEqual(w.source_box.currentData(), "meoz")
