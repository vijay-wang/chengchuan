import tempfile
import unittest
from dataclasses import replace
from datetime import datetime
from pathlib import Path

import test_market_views_desktop as harness
from test_microstructure import order, tape, trade

DESKTOP = harness.DESKTOP
if DESKTOP:
    from PySide6.QtCore import Qt

    from chengchuan.domain.microstructure import BookLevel, BookSnapshot
    from chengchuan.domain.models import SHANGHAI
    from chengchuan.domain.sectors import SectorMember, SectorSnapshot
    from chengchuan.infrastructure.workspace import SqliteWorkspaceRepository
    from chengchuan.ui.book_stream import BookStreamRunner


def sample_tape(day):
    source = tape()

    def rebase(event):
        return replace(event, time=event.time.replace(year=day.year, month=day.month, day=day.day))

    book = BookSnapshot(
        source.symbol,
        datetime(day.year, day.month, day.day, 15, tzinfo=SHANGHAI),
        tuple(
            BookLevel(i, 20 - (i - 1) * 0.01, 100, 20.01 + (i - 1) * 0.01, 100)
            for i in range(1, 11)
        ),
        "猫头鹰历史十档",
        historical=True,
    )
    return replace(
        source,
        day=day,
        orders=tuple(rebase(e) for e in source.orders),
        trades=tuple(rebase(e) for e in source.trades),
        quotes=(book,),
    )


class MicroMarket(harness.ViewsMarket):
    supports_sector = True
    supports_microstructure = True
    supports_book_stream = True

    def __init__(self):
        super().__init__()
        self.tape_calls = []
        self.sector_calls = []
        self.cache_calls = []
        self.cached = None

    def load_cached_microstructure(self, symbol, day, max_pages=6):
        from chengchuan.application.errors import MarketError

        self.cache_calls.append((symbol, day))
        if self.cached is None:
            raise MarketError("没有逐笔缓存")
        return replace(self.cached, symbol=symbol)

    def load_sector(self, kind, code, name, refresh=False):
        self.sector_calls.append((kind, code))
        now = datetime(2026, 11, 4, 18, tzinfo=SHANGHAI)
        return SectorSnapshot(
            kind,
            code,
            name,
            now.date(),
            (
                SectorMember("sz002318", "同涨幅低流入", 10, 20, 0),
                SectorMember("sh600519", "同涨幅高流入", 10, 20, 100),
                SectorMember("sz000001", "低涨幅", 10, 10, 1000),
            ),
            now,
            now,
        )

    def load_microstructure(self, symbol, day, max_pages=6):
        self.tape_calls.append((symbol, day, max_pages))
        return replace(sample_tape(day), symbol=symbol)

    def load_order_book(self, symbol):
        day = self.current_day
        return replace(
            sample_tape(day).quotes[0],
            symbol=symbol,
            historical=False,
            levels=sample_tape(day).quotes[0].levels[:5],
        )


@unittest.skipUnless(DESKTOP, "需要desktop依赖")
class MicrostructureDesktopTests(unittest.TestCase):
    setUpClass = classmethod(harness.MarketViewsDesktopTests.setUpClass.__func__)
    wait_until = harness.MarketViewsDesktopTests.wait_until
    make_window = harness.MarketViewsDesktopTests.make_window
    opened = harness.MarketViewsDesktopTests.opened

    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.workspace = SqliteWorkspaceRepository(Path(self.temp.name) / "state.sqlite")
        self.market = MicroMarket()
        self.window = self.make_window()
        self.addCleanup(self.window.close)
        self.window.auto_refresh.setChecked(False)
        self.window.open_stock("002318")
        self.wait_until(lambda: self.opened() and self.window.details_runner.active is None)
        self.day = self.window.frame.snapshot.series.bars[-1].timestamp.date()
        self.market.current_day = self.day

    def test_heatmap_maximize_zoom_and_full_list_remain_available(self):
        w = self.window
        w.open_heatmap()
        dialog = w.heatmap_dialog
        self.wait_until(lambda: dialog.snapshot is not None)
        self.assertTrue(dialog.windowFlags() & Qt.WindowType.WindowMaximizeButtonHint)
        dialog.maximize_button.click()
        self.app.processEvents()
        self.assertTrue(dialog.isMaximized())
        self.wait_until(lambda: dialog.width() >= dialog.screen().availableGeometry().width() * 0.9)
        before = dialog.canvas.width()
        dialog._change_zoom(1)
        self.app.processEvents()
        self.assertGreater(dialog.canvas.width(), before)
        dialog.list_button.setChecked(True)
        self.assertIs(dialog.pages.currentWidget(), dialog.list_view)
        self.assertEqual(dialog.list_model.rowCount(), 5)
        dialog.close()
        self.app.processEvents()
        self.assertIsNone(w.heatmap_dialog)

    def test_sector_drilldown_ranks_change_then_cash_and_returns_without_requery(self):
        w = self.window
        w.open_heatmap()
        dialog = w.heatmap_dialog
        self.wait_until(lambda: dialog.snapshot is not None)
        dialog.canvas.sector_requested.emit("1")
        self.wait_until(lambda: dialog.sector is not None)
        self.assertIs(dialog.pages.currentWidget(), dialog.member_view)
        self.assertEqual(
            [r[0] for r in dialog.member_model.rows], ["sh600519", "sz002318", "sz000001"]
        )
        calls = len(self.market.heat_calls)
        dialog.search.setText("高流入")
        self.assertEqual(dialog.member_model.rowCount(), 1)
        dialog.back.click()
        self.assertIsNone(dialog.sector)
        self.assertEqual(len(self.market.heat_calls), calls)

    def test_original_order_bands_have_two_tables_counts_and_pies_from_one_tape(self):
        w = self.window
        self.assertEqual(self.market.tape_calls, [])
        w.funds_panel.order_bands.load_button.click()
        self.wait_until(lambda: w._l2_analysis is not None)
        panel = w.funds_panel.order_bands
        self.assertEqual(panel.tables[0].item(3, 2).text(), "1")
        self.assertEqual(panel.tables[0].item(3, 1).text(), "120.00万元")
        self.assertTrue(panel.pies[0].ready)
        self.assertEqual(len(self.market.tape_calls), 1)
        self.assertEqual(w.micro_panel.order_model.rowCount(), 4)
        self.assertEqual(w.micro_panel.restored_model.rowCount(), 4)

    def test_table_alternate_rows_use_dark_palette(self):
        from PySide6.QtGui import QPalette

        w = self.window
        w._l2_ready(sample_tape(self.day))
        self.wait_until(lambda: w._l2_analysis is not None)
        w._expand_micro()
        panel = w.micro_dialog.panel
        panel.tabs.setCurrentWidget(panel.restored_page)
        self.app.processEvents()
        palette = panel.restored_table.palette()
        self.assertEqual(palette.color(QPalette.ColorRole.Base).name(), "#101a27")
        self.assertEqual(palette.color(QPalette.ColorRole.AlternateBase).name(), "#152335")
        image = panel.restored_table.viewport().grab().toImage()
        for row in (0, 1):
            y = (
                panel.restored_table.rowViewportPosition(row)
                + panel.restored_table.rowHeight(row)
                - 7
            )
            self.assertLess(image.pixelColor(8, y).lightness(), 100)

    def test_band_filters_sync_across_views_without_data_requests(self):
        w = self.window
        source = sample_tape(self.day)
        source = replace(
            source, trades=(*source.trades, replace(source.trades[0], kind="C", bid_id=1, ask_id=0))
        )
        w._l2_ready(source)
        self.wait_until(lambda: w._l2_analysis is not None)
        w._expand_micro()
        controls = w.funds_panel.order_bands.filters
        controls.cancelled.setChecked(True)
        controls.unfilled.setChecked(False)
        self.assertTrue(w.micro_dialog.panel.bands.filters.cancelled.isChecked())
        self.assertFalse(w.micro_panel.bands.filters.unfilled.isChecked())
        self.assertEqual(w.chart_orders.filters, controls.value())
        self.assertEqual(sum(b.orders for b in w.funds_panel.order_bands.pies[0].bands), 1)
        self.assertEqual(self.market.tape_calls, [])

    def test_filtered_row_opens_correct_original_and_merges_three_execution_cells(self):
        w = self.window
        source = sample_tape(self.day)
        source = replace(
            source,
            orders=(
                replace(order(5, shares=300), time=source.orders[0].time),
                replace(order(1, shares=300), time=source.orders[0].time),
                *(
                    replace(order(i, "S", shares=100), time=source.orders[0].time)
                    for i in (10, 11, 12)
                ),
            ),
            trades=tuple(
                replace(trade(i + 1, bid=1, ask=10 + i, shares=100), time=source.trades[i].time)
                for i in range(3)
            ),
        )
        w._l2_ready(source)
        self.wait_until(lambda: w._l2_analysis is not None)
        panel = w.micro_panel
        panel.restored_filters.unfilled.setChecked(False)
        self.assertEqual(panel.restored_model.rowCount(), 4)
        panel._show_fills(panel.restored_model.index(0, 0))
        view = panel.inline_matches
        self.assertIs(panel.restored_views.currentWidget(), view)
        self.assertEqual(view.selected.exchange_id, 1)
        self.assertEqual(view.model.rowCount(), 3)
        self.assertEqual(view.table.rowSpan(0, 4), 3)
        self.assertEqual(view.table.rowSpan(0, 3), 1)
        self.assertEqual(view.model.index(0, 1).data(), "20.00")
        self.assertEqual(view.model.index(0, 2).data(), "1")

    def test_sell_anchor_merges_right_column_and_pagination_has_bounded_rows(self):
        from chengchuan.application.microstructure import analyze_orders
        from chengchuan.ui.order_matches import InlineReconstruction

        source = tape(
            orders=[order(10, "S", shares=30100), order(1, shares=30100)],
            trades=[trade(i + 1, bid=1, ask=10, shares=100) for i in range(301)],
        )
        a = analyze_orders(source)
        view = InlineReconstruction(self.window)
        view.set_analysis(a, self.window.micro_panel.restored_filters.value())
        self.assertEqual(view.model.rowCount(), 300)
        self.assertEqual(view.table.rowSpan(0, 4), 300)
        self.assertEqual(view.table.rowSpan(0, 3), 300)
        view.next.click()
        self.assertEqual(view.model.rowCount(), 1)
        self.assertFalse(view.next.isEnabled())
        self.assertTrue(view.previous.isEnabled())
        view.previous.click()
        self.assertEqual(view.table.rowSpan(0, 3), 300)

    def test_parent_close_stops_chart_timer_and_hides_inline_reconstruction(self):
        w = self.window
        w._l2_ready(sample_tape(self.day))
        self.wait_until(lambda: w._l2_analysis is not None)
        w._expand_micro()
        panel = w.micro_dialog.panel
        panel._show_fills(panel.restored_model.index(0, 0))
        w.chart_orders._schedule_markers()
        w.close()
        self.assertFalse(panel.inline_matches.isVisible())
        self.assertFalse(w.chart_orders.timer.isActive())

    def test_minute_hover_and_multicolor_marks_are_local_and_clear_on_switch(self):
        from PySide6.QtCore import QPointF

        from chengchuan.application.microstructure import analyze_orders

        w = self.window
        w.open_stock("002318", "minute")
        self.wait_until(self.opened)
        series = w.chart.snapshot.series
        stamp = series.bars[0].timestamp.replace(second=0, microsecond=0)
        source = replace(
            tape(orders=[order(1, shares=10000), order(2, shares=25000)], trades=[]),
            day=stamp.date(),
            orders=(
                replace(order(1, shares=10000), time=stamp),
                replace(order(2, shares=25000), time=stamp),
            ),
            retrieved_at=stamp,
        )
        controller = w.chart_orders
        controller.set_analysis(analyze_orders(source))
        controller.show_minute.setChecked(True)
        controller.show_marks.setChecked(True)
        controller._render_markers()
        self.assertEqual([s["data"] for s in controller.marker_spots], [(0, 1), (0, 2)])
        self.assertLess(controller.marker_spots[0]["pos"][0], controller.marker_spots[1]["pos"][0])
        point = w.chart.price.getViewBox().mapViewToScene(QPointF(0, series.bars[0].close))
        w.chart.cursor.move(point, w.chart.price)
        self.assertTrue(controller.overlay.isVisible())
        self.assertIn("20–50万", controller.overlay.text())
        self.assertIn("50–100万", controller.overlay.text())
        controller.show_minute.setChecked(False)
        self.assertFalse(controller.overlay.isVisible())
        controller.show_minute.setChecked(True)
        controller.clear()
        controller._render_markers()
        self.assertFalse(controller.overlay.isVisible())
        self.assertEqual(controller.marker_spots, [])
        self.assertEqual(self.market.tape_calls, [])

    def test_read_daily_then_choose_minute_preserves_data_and_real_mouse_hover(self):
        from PySide6.QtCore import QEvent, QPointF
        from PySide6.QtGui import QMouseEvent

        w = self.window
        w._request_l2_tape()
        self.wait_until(lambda: w._l2_analysis is not None)
        analysis, source = w._l2_analysis, w._l2_tape
        w.period.setCurrentIndex(w.period.findData("minute"))
        self.wait_until(lambda: self.opened() and w.chart.snapshot.series.interval == "minute")
        self.assertIs(w._l2_analysis, analysis)
        self.assertIs(w._l2_tape, source)
        c = w.chart_orders
        c.show_minute.setChecked(True)
        c.show_marks.setChecked(True)
        c.band_checks[3].setChecked(True)
        self.wait_until(lambda: bool(c.marker_spots))
        bounds = w.chart.price.getViewBox().viewRange()[1]
        point = w.chart.price.getViewBox().mapViewToScene(QPointF(0, sum(bounds) / 2))
        local = w.chart.price.mapFromScene(point)
        self.app.sendEvent(
            w.chart.price.viewport(),
            QMouseEvent(
                QEvent.Type.MouseMove,
                QPointF(local),
                QPointF(w.chart.price.viewport().mapToGlobal(local)),
                Qt.MouseButton.NoButton,
                Qt.MouseButton.NoButton,
                Qt.KeyboardModifier.NoModifier,
            ),
        )
        self.wait_until(lambda: c.overlay.isVisible())
        self.assertIn("100–200万", c.overlay.text())
        self.assertEqual(len(self.market.tape_calls), 1)
        self.assertEqual(self.market.cache_calls, [])

    def test_enable_annotations_loads_local_cache_once_without_paid_query(self):
        w = self.window
        self.market.cached = sample_tape(self.day)
        w.open_stock("002318", "minute")
        self.wait_until(self.opened)
        c = w.chart_orders
        c.show_marks.setChecked(True)
        c.band_checks[3].setChecked(True)
        c.show_minute.setChecked(True)
        self.wait_until(lambda: w._l2_analysis is not None and bool(c.marker_spots))
        for _ in range(3):
            c.show_marks.setChecked(False)
            c.show_marks.setChecked(True)
        self.assertEqual(len(self.market.cache_calls), 1)
        self.assertEqual(self.market.tape_calls, [])

    def test_next_trading_day_refresh_discards_previous_day_orders(self):
        from datetime import timedelta

        w = self.window
        w._l2_ready(sample_tape(self.day))
        self.wait_until(lambda: w._l2_analysis is not None)
        frame = w.frame
        bars = tuple(
            replace(b, timestamp=b.timestamp + timedelta(days=1))
            for b in frame.snapshot.series.bars
        )
        next_frame = replace(
            frame,
            snapshot=replace(
                frame.snapshot,
                revision="next-day",
                series=replace(frame.snapshot.series, bars=bars),
            ),
        )
        w._market_ready(next_frame)
        self.assertIsNone(w._l2_analysis)
        self.assertIsNone(w.chart_orders.analysis)

    def test_missing_cache_explains_reason_in_hover_without_retries(self):
        w = self.window
        w.open_stock("002318", "minute")
        self.wait_until(self.opened)
        c = w.chart_orders
        c.show_minute.setChecked(True)
        self.wait_until(lambda: w.l2_runner.active is None and bool(self.market.cache_calls))
        c._hover(0)
        self.assertTrue(c.overlay.isVisible())
        self.assertIn("读取逐笔", c.overlay.text())
        self.assertNotIn("0笔", c.overlay.text())
        for _ in range(3):
            c.show_marks.setChecked(True)
            c.show_marks.setChecked(False)
        self.assertEqual(len(self.market.cache_calls), 1)
        self.assertEqual(self.market.tape_calls, [])

    def test_marker_rail_is_painted_at_volume_top_with_nonoverlapping_colors(self):
        from PySide6.QtGui import QColor

        w = self.window
        self.market.cached = sample_tape(self.day)
        w.open_stock("002318", "minute")
        self.wait_until(self.opened)
        c = w.chart_orders
        c.show_marks.setChecked(True)
        c.band_checks[0].setChecked(True)
        c.band_checks[3].setChecked(True)
        self.wait_until(lambda: w._l2_analysis is not None and len(c.marker_spots) >= 2)
        self.app.processEvents()
        shots = w.chart.volume.viewport().grab().toImage()
        from PySide6.QtCore import QPointF

        for spot in c.marker_spots:
            p = w.chart.volume.mapFromScene(
                w.chart.volume.getViewBox().mapViewToScene(QPointF(*spot["pos"]))
            )
            color = shots.pixelColor(p)
            expected = QColor(spot["brush"])
            self.assertLess(
                sum(abs(a - b) for a, b in zip(color.getRgb()[:3], expected.getRgb()[:3])), 25
            )
        first, second = c.marker_spots[:2]
        self.assertEqual(first["pos"][1], second["pos"][1])
        self.assertLessEqual(
            first["pos"][0] + first["size"][0] / 2, second["pos"][0] - second["size"][0] / 2 + 1e-9
        )

    def test_partial_data_disables_minute_overlay_and_markers(self):
        from chengchuan.application.microstructure import analyze_orders

        w = self.window
        w.open_stock("002318", "minute")
        self.wait_until(self.opened)
        source = replace(sample_tape(self.day), trades_complete=False)
        controller = w.chart_orders
        controller.set_analysis(analyze_orders(source))
        controller.show_minute.setChecked(True)
        controller.show_marks.setChecked(True)
        controller._render_markers()
        controller._hover(0)
        self.assertTrue(controller.overlay.isVisible())
        self.assertIn("分页未齐", controller.overlay.text())
        self.assertNotIn("0笔", controller.overlay.text())
        self.assertEqual(controller.marker_spots, [])
        self.assertIn("分页未齐", controller.hint.text())

    def test_incomplete_tape_suppresses_pie_and_final_values(self):
        w = self.window
        partial = replace(sample_tape(self.day), orders_complete=False)
        w._l2_ready(partial)
        self.wait_until(lambda: w._l2_analysis is not None)
        self.assertFalse(w.funds_panel.order_bands.pies[0].ready)
        self.assertEqual(w.funds_panel.order_bands.tables[0].item(3, 1).text(), "—")

    def test_live_five_levels_do_not_display_historical_order_queue(self):
        w = self.window
        w._l2_ready(sample_tape(self.day))
        self.wait_until(lambda: w._l2_analysis is not None)
        w._request_book()
        self.wait_until(
            lambda: (
                w.book_runner.active is None
                and w.micro_panel.book is not None
                and not w.micro_panel.book.historical
            )
        )
        self.assertEqual(w.micro_panel.queue_model.rowCount(), 0)
        index = w.micro_panel.depth.findData(10)
        self.assertFalse(w.micro_panel.depth.model().item(index).isEnabled())

    def test_replay_recomputes_locally_and_stale_tape_cannot_override_new_symbol(self):
        w = self.window
        w._request_l2_tape(6)
        self.wait_until(lambda: w._l2_analysis is not None)
        calls = len(self.market.tape_calls)
        w._replay_l2(datetime.strptime("09:30:00", "%H:%M:%S").time())
        self.wait_until(lambda: w._l2_analysis.cutoff.time().isoformat() == "09:30:00")
        self.assertEqual(w._l2_analysis.restored[0].executed_shares, 0)
        self.assertEqual(len(self.market.tape_calls), calls)
        old = w._l2_tape
        w.open_stock("600519")
        self.wait_until(self.opened)
        w._l2_ready(old)
        self.assertIsNone(w._l2_tape)

    def test_latest_value_stream_buffer_coalesces_and_shutdown_has_no_late_result(self):
        runner = BookStreamRunner()
        self.addCleanup(runner.shutdown)
        results = []
        runner.ready.connect(results.append)
        book = sample_tape(self.day).quotes[0]
        runner.start(lambda stop: iter([book] * 10000))
        self.wait_until(lambda: bool(results))
        self.assertLessEqual(len(results), 2)
        self.assertFalse(runner.timer.isActive())
        runner.shutdown()
        count = len(results)
        self.app.processEvents()
        self.assertEqual(len(results), count)

    def test_expanded_workbench_reuses_same_analysis_and_closes_with_parent(self):
        w = self.window
        w._l2_ready(sample_tape(self.day))
        self.wait_until(lambda: w._l2_analysis is not None)
        w._expand_micro()
        self.assertIs(w.micro_dialog.panel.analysis, w._l2_analysis)
        self.assertTrue(w.micro_dialog.windowFlags() & Qt.WindowType.WindowMaximizeButtonHint)
        w.close()
        self.assertFalse(w.micro_dialog.isVisible())
