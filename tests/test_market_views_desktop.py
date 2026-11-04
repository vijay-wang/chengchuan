import tempfile
import threading
import unittest
from dataclasses import replace
from pathlib import Path
from unittest.mock import patch

import test_market_desktop as harness
from test_market import sample_frame
from test_market_views import NOW, flow_payload, heat_snapshot

DESKTOP = harness.DESKTOP
if DESKTOP:
    from PySide6.QtCore import Qt
    from PySide6.QtTest import QTest

    from chengchuan.domain.market_dashboard import QuoteDetails, QuoteMetric
    from chengchuan.infrastructure.market_views import parse_public_flow
    from chengchuan.infrastructure.workspace import SqliteWorkspaceRepository


class ViewsMarket(harness.FakeMarket):
    supports_heatmap = True
    supports_funds = True
    supports_details = True

    def __init__(self):
        super().__init__()
        self.funds_calls = []
        self.heat_calls = []
        self.funds_release = None

    def load_details(self, symbol, as_of=None, refresh=False):
        return QuoteDetails(
            symbol,
            sample_frame().snapshot.series.bars[-1].timestamp.date(),
            (
                QuoteMetric("volume_ratio", "量比", 0, "倍"),
                QuoteMetric("turnover_rate", "换手率", 1, "%"),
            ),
            NOW,
        )

    def load_heatmap(self, kind, refresh=False):
        self.heat_calls.append((kind, refresh))
        return heat_snapshot(kind)

    def load_funds(self, symbol, refresh=False):
        self.funds_calls.append((symbol, refresh))
        release = self.funds_release
        if release:
            release.wait(3)
        if symbol == "market:shsz":
            payload = flow_payload("000001", 1)
            payload["data"]["diff"] += flow_payload("399001", 0)["data"]["diff"]
        else:
            payload = flow_payload(symbol[-6:], 1 if symbol.startswith("sh") else 0)
        return parse_public_flow(payload, symbol, NOW)


@unittest.skipUnless(DESKTOP, "需要desktop依赖")
class MarketViewsDesktopTests(unittest.TestCase):
    setUpClass = classmethod(harness.OnlineDesktopTests.setUpClass.__func__)
    wait_until = harness.OnlineDesktopTests.wait_until
    make_window = harness.OnlineDesktopTests.make_window
    opened = harness.OnlineDesktopTests.opened

    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.workspace = SqliteWorkspaceRepository(Path(self.temp.name) / "state.sqlite")
        self.market = ViewsMarket()
        self.window = self.make_window()
        self.addCleanup(self.window.close)
        self.window.auto_refresh.setChecked(False)
        self.window.open_stock("002318")
        self.wait_until(lambda: self.opened() and self.window.details_runner.active is None)

    def test_refresh_retains_layout_plot_objects_range_selection_and_full_quote_cells(self):
        w = self.window
        original_status = w.market_status.text()
        original_geometry = w.chart.geometry()
        candle, volume = w.chart._candles, w.chart._volume_item
        cell = w.quote_panel.values["volume_ratio"]
        visible = w.chart.price.getViewBox().viewRange()[0]
        selection = w.selection
        self.market.release = threading.Event()
        w.refresh_market()
        self.app.processEvents()
        self.assertEqual(w.market_status.text(), original_status)
        self.assertEqual(w.chart.geometry(), original_geometry)
        self.assertTrue(w.chart.isEnabled())
        self.market.release.set()
        self.wait_until(lambda: self.opened() and w.details_runner.active is None)
        changed = replace(
            w.frame,
            snapshot=replace(
                w.frame.snapshot,
                revision="changed",
                series=replace(
                    w.frame.snapshot.series,
                    bars=(
                        *w.frame.snapshot.series.bars[:-1],
                        replace(w.frame.snapshot.series.bars[-1], close=29.5),
                    ),
                ),
            ),
        )
        with (
            patch.object(w.chart.price, "clear", wraps=w.chart.price.clear) as clear_price,
            patch.object(w.chart.volume, "clear", wraps=w.chart.volume.clear) as clear_volume,
        ):
            w._market_ready(changed)
            self.wait_until(lambda: self.opened() and w.details_runner.active is None)
        clear_price.assert_not_called()
        clear_volume.assert_not_called()
        self.assertIs(w.chart._candles, candle)
        self.assertIs(w.chart._volume_item, volume)
        self.assertIs(w.quote_panel.values["volume_ratio"], cell)
        self.assertEqual(w.selection, selection)
        for actual, expected in zip(w.chart.price.getViewBox().viewRange()[0], visible):
            self.assertAlmostEqual(actual, expected)

    def test_heatmap_kind_filter_and_stock_click_use_native_view_without_network(self):
        w = self.window
        QTest.mouseClick(w.index_bar.heatmap_button, Qt.MouseButton.LeftButton)
        dialog = w.heatmap_dialog
        self.wait_until(lambda: dialog.snapshot is not None)
        self.assertEqual(dialog.snapshot.kind, "hy")
        self.assertEqual(len(dialog.canvas.rects), 5)
        for kind in ("gn", "stocks"):
            dialog.kind.setCurrentIndex(dialog.kind.findData(kind))
            self.wait_until(lambda: dialog.snapshot is not None and dialog.snapshot.kind == kind)
        calls = len(self.market.heat_calls)
        dialog.search.setText("行业1")
        self.app.processEvents()
        self.assertEqual(len(dialog.canvas.rects), 1)
        self.assertEqual(len(self.market.heat_calls), calls)
        dialog.scope.setCurrentIndex(1)
        self.assertEqual(len(dialog.canvas.rects), 0)
        dialog.scope.setCurrentIndex(0)
        dialog.canvas.open_requested.emit("sh600519")
        self.wait_until(lambda: w.online_symbol == "sh600519" and self.opened())
        dialog.close()
        self.app.processEvents()
        self.assertIsNone(w.heatmap_dialog)

    def test_money_flow_is_lazy_and_switches_between_security_and_market(self):
        w = self.window
        self.assertEqual(self.market.funds_calls, [])
        w.research_tabs.setCurrentWidget(w.funds_panel)
        self.wait_until(lambda: w.funds_panel.snapshot is not None)
        self.assertEqual(w.funds_panel.snapshot.symbol, "sz002318")
        self.assertEqual(w.funds_panel.table.rowCount(), 6)
        self.assertEqual(w.funds_panel.table.item(5, 1).text(), "0.00元")
        self.assertIn("非真实账户", w.funds_panel.notice.text())
        w.funds_panel.scope.setCurrentIndex(1)
        self.wait_until(
            lambda: (
                w.funds_panel.snapshot is not None
                and w.funds_panel.snapshot.symbol == "market:shsz"
            )
        )
        self.assertEqual(w.funds_panel.snapshot.buckets[0].inflow, 60)

    def test_late_funds_cannot_overwrite_new_stock_and_close_cancels_dialog(self):
        w = self.window
        self.market.funds_release = threading.Event()
        w.research_tabs.setCurrentWidget(w.funds_panel)
        self.wait_until(lambda: bool(self.market.funds_calls))
        w.open_stock("600519")
        self.market.funds_release.set()
        self.wait_until(lambda: self.opened() and w.funds_runner.active is None)
        self.assertIsNone(w.funds_panel.snapshot)
        w.research_tabs.setCurrentWidget(w.funds_panel)
        self.wait_until(lambda: w.funds_panel.snapshot is not None)
        self.assertEqual(w.funds_panel.snapshot.symbol, "sh600519")
        w.open_heatmap()
        self.wait_until(lambda: w.heatmap_dialog.snapshot is not None)
        runner = w.heatmap_dialog.runner
        w.close()
        self.assertTrue(runner.closed)
