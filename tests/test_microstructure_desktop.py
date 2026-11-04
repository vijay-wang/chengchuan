import tempfile
import unittest
from dataclasses import replace
from datetime import datetime
from pathlib import Path

import test_market_views_desktop as harness
from test_microstructure import tape

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
