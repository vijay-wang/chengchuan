import os
import unittest
from dataclasses import replace
from datetime import timedelta

os.environ["QT_QPA_PLATFORM"] = "offscreen"

import test_chart_history_desktop as chart_harness
import test_meoz_desktop as harness

DESKTOP = harness.DESKTOP
if DESKTOP:
    from PySide6.QtCore import QPointF, Qt
    from PySide6.QtTest import QTest

    from chengchuan.application.drawings import DrawingBook
    from chengchuan.domain.drawings import price_difference
    from chengchuan.domain.models import SHANGHAI


@unittest.skipUnless(DESKTOP, "需要desktop依赖")
class DrawingDesktopTests(unittest.TestCase):
    setUpClass = classmethod(chart_harness.ChartHistoryDesktopTests.setUpClass.__func__)
    setUp = chart_harness.ChartHistoryDesktopTests.setUp
    make_window = chart_harness.ChartHistoryDesktopTests.make_window
    until = chart_harness.ChartHistoryDesktopTests.until
    opened = chart_harness.ChartHistoryDesktopTests.opened

    def open_day(self):
        self.window.open_stock("002318", "day")
        self.opened()
        self.chart = self.window.chart
        self.tools = self.chart.drawings

    def at(self, x, y, plot=None):
        plot = plot or self.chart.price
        return plot.mapFromScene(plot.getViewBox().mapViewToScene(QPointF(x, y)))

    def click(self, x, y):
        QTest.mouseClick(self.chart.price.viewport(), Qt.MouseButton.LeftButton, pos=self.at(x, y))
        self.app.processEvents()

    def tool(self, name):
        self.tools.combo.setCurrentIndex(self.tools.combo.findData(name))

    def test_price_pair_drag_updates_signed_label_and_persists(self):
        self.open_day()
        bounds = self.chart.price.getViewBox().viewRange()[1]
        self.tool("price_range")
        self.click(3, 9.6)
        QTest.mouseMove(self.chart.price.viewport(), self.at(6, 10.6))
        self.click(6, 10.6)
        self.assertEqual(len(self.tools.visuals), 1)
        visual = next(iter(self.tools.visuals.values()))
        a, b = visual.record.points
        self.assertAlmostEqual(a.price, 9.6, delta=0.02)
        self.assertAlmostEqual(b.price, 10.6, delta=0.02)
        delta, percent = price_difference(a.price, b.price)
        self.assertIn(f"{delta:+.3f}", visual.text.toPlainText())
        self.assertIn(f"{percent:+.2f}%", visual.text.toPlainText())
        # Actual mouse drag crosses the base line; the A denominator must not change.
        start, stop = self.at(9, b.price), self.at(9, 9.3)
        QTest.mousePress(self.chart.price.viewport(), Qt.MouseButton.LeftButton, pos=start)
        QTest.mouseMove(self.chart.price.viewport(), stop, 30)
        QTest.mouseRelease(self.chart.price.viewport(), Qt.MouseButton.LeftButton, pos=stop)
        self.app.processEvents()
        current = self.tools.book.items[visual.record.id]
        self.assertLess(current.points[1].price, current.points[0].price)
        self.assertIn("-", visual.text.toPlainText())
        self.assertEqual(self.chart.price.getViewBox().viewRange()[1], bounds)
        self.tools.flush()
        restored = DrawingBook(self.workspace)
        self.assertEqual(restored.items[current.id], current)

    def test_all_tools_delete_cancel_and_timestamp_positions_after_prepend(self):
        self.open_day()
        for tool in ("horizontal", "vertical", "trend", "rectangle"):
            self.tool(tool)
            self.click(3, 9.7)
            if tool in ("trend", "rectangle"):
                self.click(8, 10.4)
        self.assertEqual(len(self.tools.visuals), 4)
        trend = next(v for v in self.tools.visuals.values() if v.record.tool == "trend")
        old_points = trend.record.points
        original = self.chart.snapshot
        first = original.series.bars[0]
        expanded = replace(
            original,
            revision="prepended",
            series=replace(
                original.series,
                bars=(replace(first, timestamp=first.timestamp - timedelta(days=3)),)
                + original.series.bars,
            ),
        )
        self.chart.set_snapshot(expanded, preserve_view=True)
        self.assertIs(self.tools.visuals[trend.record.id], trend)
        self.assertEqual(trend.record.points, old_points)
        actual = [trend.roi.mapToParent(p).x() for p in trend.roi.listPoints()]
        self.assertEqual(actual, [4, 9])
        self.tool("price_range")
        self.click(5, 9.8)
        QTest.keyClick(self.window, Qt.Key.Key_Escape)
        self.assertEqual(self.tools.tool, "browse")
        self.assertIsNone(self.tools.first)
        self.assertEqual(len(self.tools.visuals), 4)
        self.tools.select(trend.record.id)
        self.chart.price.setFocus()
        QTest.keyClick(self.chart.price.viewport(), Qt.Key.Key_Delete)
        self.assertNotIn(trend.record.id, self.tools.book.items)
        self.assertEqual(len(self.tools.visuals), 3)

    def test_roi_handles_edit_points_and_ctrl_selection_takes_priority_over_drawing(self):
        self.open_day()
        for tool in ("trend", "rectangle"):
            self.tool(tool)
            self.click(3, 9.6)
            self.click(8, 10.5)
            visual = next(iter(self.tools.visuals.values()))
            original = visual.record
            handle = visual.roi.getHandles()[0]
            start = self.chart.price.mapFromScene(handle.scenePos())
            stop = start + self.at(5, 10.2) - self.at(3, 10.5)
            QTest.mousePress(self.chart.price.viewport(), Qt.MouseButton.LeftButton, pos=start)
            QTest.mouseMove(self.chart.price.viewport(), stop, 30)
            QTest.mouseRelease(self.chart.price.viewport(), Qt.MouseButton.LeftButton, pos=stop)
            self.app.processEvents()
            self.assertNotEqual(self.tools.book.items[original.id].points, original.points)
            self.tools.clear_current()
        self.tool("horizontal")
        QTest.mousePress(
            self.chart.price.viewport(),
            Qt.MouseButton.LeftButton,
            Qt.KeyboardModifier.ControlModifier,
            self.at(3, 10),
        )
        QTest.mouseMove(self.chart.price.viewport(), self.at(7, 10))
        QTest.mouseRelease(
            self.chart.price.viewport(),
            Qt.MouseButton.LeftButton,
            Qt.KeyboardModifier.ControlModifier,
            self.at(7, 10),
        )
        self.opened()
        self.assertEqual(len(self.tools.visuals), 0)
        self.assertIsNotNone(self.window.selection)

    def test_switch_period_and_stock_does_not_lose_or_clear_other_chart_drawings(self):
        self.open_day()
        self.tool("horizontal")
        self.click(4, 9.9)
        original = next(iter(self.tools.book.items))
        self.window.open_stock("600519", "day")
        self.opened()
        self.assertEqual(len(self.tools.visuals), 0)
        self.tool("horizontal")
        self.click(4, 10.1)
        self.tools.clear_current()
        self.assertEqual(set(self.tools.book.items), {original})
        self.window.open_stock("002318", "day")
        self.opened()
        self.assertEqual(set(self.tools.visuals), {original})
        self.window.refresh_market()
        self.opened()
        self.assertEqual(set(self.tools.visuals), {original})

    def test_double_click_day_loads_exact_historical_intraday_and_volume_does_not(self):
        self.open_day()
        day = self.chart.snapshot.series.bars[6].timestamp.astimezone(SHANGHAI).date()
        QTest.mouseDClick(
            self.chart.volume.viewport(),
            Qt.MouseButton.LeftButton,
            pos=self.at(6, 0.005, self.chart.volume),
        )
        self.app.processEvents()
        self.assertEqual(self.window.online_interval, "day")
        self.tool("price_range")
        QTest.mouseDClick(
            self.chart.price.viewport(), Qt.MouseButton.LeftButton, pos=self.at(6, 10)
        )
        self.app.processEvents()
        self.assertEqual(self.window.online_interval, "day")
        self.tools.cancel()
        QTest.mouseDClick(
            self.chart.price.viewport(), Qt.MouseButton.LeftButton, pos=self.at(6, 10)
        )
        self.opened()
        self.assertEqual(self.window.online_interval, "minute")
        self.assertEqual(self.window.history_date, day)
        self.assertEqual(self.window.period.currentData(), "minute")
        self.assertEqual(
            {b.timestamp.astimezone(SHANGHAI).date() for b in self.window.snapshot.series.bars},
            {day},
        )

    def test_unsupported_history_source_keeps_chart_and_shows_action(self):
        self.open_day()
        self.market.supports_history = False
        current = self.window.snapshot
        QTest.mouseDClick(
            self.chart.price.viewport(), Qt.MouseButton.LeftButton, pos=self.at(6, 10)
        )
        self.app.processEvents()
        self.assertIs(self.window.snapshot, current)
        self.assertIn("历史分时", self.window.status.text())


if __name__ == "__main__":
    unittest.main()
