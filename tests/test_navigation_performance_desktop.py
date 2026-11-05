import os
import tempfile
import threading
import time
import unittest
from dataclasses import replace
from datetime import datetime, timedelta
from functools import partial
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import Mock

import test_market_desktop as harness
from test_market import sample_frame

DESKTOP = harness.DESKTOP

if DESKTOP:
    from PySide6.QtTest import QSignalSpy, QTest
    from PySide6.QtWidgets import QApplication

    from chengchuan.application.company import CompanyService
    from chengchuan.domain.models import (
        SHANGHAI,
        MarketBar,
        MarketSeries,
        MarketSnapshot,
        SeriesKind,
    )
    from chengchuan.ui.chart import MarketChart
    from chengchuan.ui.company import CompanyWorkspace
    from chengchuan.ui.research import ResearchDataView
    from chengchuan.ui.tasks import LatestTaskRunner, ProcessTaskRunner


@unittest.skipUnless(DESKTOP, "需要desktop依赖")
class NavigationPerformanceDesktopTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.app = QApplication.instance() or QApplication([])

    def until(self, predicate):
        deadline = time.monotonic() + 6
        while not predicate():
            self.app.processEvents()
            QTest.qWait(2)
            if time.monotonic() > deadline:
                self.fail("性能交互验证超时")

    def make_window(self):
        folder = tempfile.TemporaryDirectory()
        self.addCleanup(folder.cleanup)
        market = harness.FakeMarket()
        window = harness.MainWindow(
            harness.LegacyJsonRepository(),
            harness.AnalysisService(harness.LegacyAnalysisEngine()),
            harness.SqliteWorkspaceRepository(Path(folder.name) / "workspace.sqlite"),
            market=market,
            network_runner_factory=harness.LatestTaskRunner,
        )
        window.auto_refresh.setChecked(False)
        window.show()
        self.addCleanup(window.close)
        return window, market

    def test_warm_stock_switch_shows_correct_cached_stock_before_network_finishes(self):
        window, market = self.make_window()
        for symbol in ("002318", "600519"):
            window.open_stock(symbol, "day")
            self.until(lambda: window.frame is not None and window.market_runner.active is None)
        old = window.navigation_cache.frames.get(("sz002318", "day", None))
        window.navigation_cache.remember(
            replace(old, retrieved_at=datetime.now(SHANGHAI) - timedelta(days=1))
        )
        market.release = threading.Event()
        window.open_stock("002318", "day")
        self.assertEqual(window.snapshot.series.symbol, "sz002318")
        self.assertTrue(window.frame.cached)
        self.assertIsNotNone(window.market_runner.active)
        self.assertIn("缓存", window.status.text())
        window.open_stock("000001", "day")
        market.release.set()
        self.until(lambda: window.market_runner.active is None)
        self.assertEqual(window.snapshot.series.symbol, "sz000001")

    def test_offline_warm_switch_avoids_worker_and_manual_refresh_still_runs(self):
        window, market = self.make_window()
        market.automatic_updates = False
        for symbol in ("002318", "600519"):
            window.open_stock(symbol, "day")
            self.until(lambda: window.frame is not None and window.market_runner.active is None)
        count = len(market.requests)
        window.open_stock("002318", "day")
        self.assertEqual(window.snapshot.series.symbol, "sz002318")
        self.assertIsNone(window.market_runner.active)
        self.assertEqual(len(market.requests), count)
        window.refresh_market()
        self.until(lambda: window.market_runner.active is None)
        self.assertGreater(len(market.requests), count)
        self.assertTrue(market.requests[-1][-1])

    def test_disk_preview_displays_before_a_slow_refresh_and_does_not_replace_its_result(self):
        from chengchuan.application.market import MarketService

        window, _ = self.make_window()
        release = threading.Event()
        self.addCleanup(release.set)
        old = replace(sample_frame(), retrieved_at=datetime.now(SHANGHAI) - timedelta(days=1))
        new = replace(sample_frame(revision="new"), retrieved_at=datetime.now(SHANGHAI))
        provider = Mock(intervals=("day", "minute"))

        def fetch(*_):
            release.wait(3)
            return new

        provider.fetch.side_effect = fetch
        cache = Mock()
        cache.load.return_value = old
        window.market = MarketService(provider, cache)
        window.open_stock("002318", "day")
        self.until(lambda: window.frame is not None)
        self.assertTrue(window.frame.cached)
        self.assertEqual(window.snapshot.revision, old.snapshot.revision)
        self.assertIsNotNone(window.market_runner.active)
        release.set()
        self.until(lambda: window.market_runner.active is None)
        self.assertEqual(window.snapshot.revision, "new")

    def test_research_module_cache_skips_repeat_requests_and_manual_update_bypasses_it(self):
        from test_research import table

        from chengchuan.domain.research import ResearchBundle
        from chengchuan.ui.research import ResearchWorkspace

        folder = tempfile.TemporaryDirectory()
        self.addCleanup(folder.cleanup)
        calls = []

        def run(request):
            calls.append(request)
            return ResearchBundle(request, (table(request.module, [dict(name="sample", value=1)]),))

        workspace = harness.SqliteWorkspaceRepository(Path(folder.name) / "workspace.sqlite")
        widget = ResearchWorkspace(
            SimpleNamespace(run=run), workspace, runner_factory=LatestTaskRunner
        )
        self.addCleanup(widget.shutdown)
        self.addCleanup(widget.close)
        widget.enter()
        self.until(lambda: widget.bundle is not None and widget.runner.active is None)
        widget.select_module("attention")
        self.until(
            lambda: widget.bundle.request.module == "attention" and widget.runner.active is None
        )
        count = len(calls)
        widget.select_module("rotation")
        self.assertEqual(widget.bundle.request.module, "rotation")
        self.assertEqual(len(calls), count)
        widget.refresh_current()
        self.until(lambda: widget.runner.active is None)
        self.assertGreater(len(calls), count)

    def test_heatmap_categories_reuse_snapshots_and_manual_refresh_bypasses_cache(self):
        from test_market_views import heat_snapshot

        from chengchuan.ui.market_views import HeatmapDialog

        calls = []

        def load(kind, refresh=False):
            calls.append((kind, refresh))
            return heat_snapshot(kind)

        widget = HeatmapDialog(
            SimpleNamespace(supports_heatmap=True, load_heatmap=load),
            runner_factory=LatestTaskRunner,
        )
        self.addCleanup(widget.close)
        widget.load()
        self.until(lambda: widget.snapshot is not None and widget.runner.active is None)
        widget.kind.setCurrentIndex(1)
        self.until(
            lambda: (
                widget.snapshot is not None
                and widget.snapshot.kind == "gn"
                and widget.runner.active is None
            )
        )
        count = len(calls)
        widget.kind.setCurrentIndex(0)
        self.assertEqual(widget.snapshot.kind, "hy")
        self.assertEqual(len(calls), count)
        widget.load(refresh=True)
        self.until(lambda: widget.runner.active is None)
        self.assertTrue(calls[-1][1])

    def test_worker_reuses_process_but_retires_idle_and_never_expires_during_active_work(self):
        runner = ProcessTaskRunner(idle_ms=80, timeout_ms=2000)
        self.addCleanup(runner.shutdown)
        results = []
        runner.ready.connect(results.append)
        runner.submit("first", None, os.getpid)
        self.until(lambda: len(results) == 1)
        pid = results[0]
        runner.submit("second", None, os.getpid)
        self.until(lambda: len(results) == 2)
        self.assertEqual(results, [pid, pid])
        runner.submit("longer-than-idle", None, partial(time.sleep, 0.2))
        self.until(lambda: len(results) == 3)
        self.assertEqual(runner._worker.pid, pid)
        self.until(lambda: runner._worker is None)
        self.assertIsNone(runner.connection)

    def test_worker_can_recover_after_process_crash_and_drop_replaced_work(self):
        runner = ProcessTaskRunner()
        self.addCleanup(runner.shutdown)
        results, errors = [], []
        runner.ready.connect(results.append)
        runner.failed.connect(errors.append)
        runner.submit("crash", None, partial(os._exit, 7))
        self.until(lambda: bool(errors))
        runner.submit("recovered", None, partial(str, "correct"))
        self.until(lambda: bool(results))
        self.assertEqual(results, ["correct"])
        runner.submit("slow", None, partial(time.sleep, 3))
        runner.submit("replacement", None, partial(str, "replacement"))
        self.until(lambda: len(results) == 2)
        self.assertEqual(results[-1], "replacement")

    def test_long_chart_only_builds_visible_candle_chunks_and_volume_rectangles(self):
        bars = tuple(
            MarketBar(
                datetime(2000, 1, 1, tzinfo=SHANGHAI) + timedelta(days=i), 10, 100, 1000, 9, 11, 8
            )
            for i in range(10000)
        )
        snapshot = MarketSnapshot(
            "long", MarketSeries("sz002318", "sample", "day", SeriesKind.OHLC, bars)
        )
        chart = MarketChart()
        chart.resize(1100, 750)
        chart.show()
        self.addCleanup(chart.close)
        chart.set_snapshot(snapshot)
        self.app.processEvents()
        self.assertLessEqual(len(chart._candles.pictures), 3)
        self.assertLess(len(chart._volume_item.opts["x"]), 300)
        chart.price.setXRange(5, 70, padding=0)
        self.app.processEvents()
        old = chart._candles.pictures[0]
        changed = replace(bars[-1], high=12)
        chart.set_snapshot(
            replace(
                snapshot,
                revision="updated",
                series=replace(snapshot.series, bars=(*bars[:-1], changed)),
            ),
            preserve_view=True,
        )
        self.app.processEvents()
        self.assertIs(chart._candles.pictures[0], old)
        chart.price.setXRange(0, 9999, padding=0)
        self.app.processEvents()
        self.assertLessEqual(len(chart._candles.pictures), 64)

    def test_unchanged_virtual_table_keeps_rows_and_selection_without_model_reset(self):
        from test_research import table

        data = table("sample", [dict(name="A", quantity=2), dict(name="B", quantity=1)])
        view = ResearchDataView(data)
        self.addCleanup(view.close)
        view.selectRow(0)
        spy = QSignalSpy(view.model().modelReset)
        view.update_table(replace(data, notice="updated source metadata"))
        self.assertEqual(spy.count(), 0)
        self.assertTrue(view.selectionModel().selectedRows())
        self.assertEqual(view.table.notice, "updated source metadata")

    def test_company_sections_reuse_complete_bundle_and_network_update_bypasses_it(self):
        from test_company import FakeCompanyProvider, company_table

        provider = FakeCompanyProvider(
            {
                "basic": company_table("basic", [dict(symbol="002318", name="sample")]),
                "indicator": company_table(
                    "indicator", [dict(symbol="002318", report_date="20260930", s_fa_eps_basic=1)]
                ),
            }
        )
        widget = CompanyWorkspace(CompanyService(provider), runner_factory=LatestTaskRunner)
        widget.show()
        self.addCleanup(widget.shutdown)
        self.addCleanup(widget.close)
        widget.enter("002318")
        self.until(lambda: widget.bundle is not None and widget.runner.active is None)
        widget.sections.setCurrentRow(1)
        self.until(
            lambda: widget.bundle.request.section == "indicator" and widget.runner.active is None
        )
        count = len(provider.calls)
        widget.sections.setCurrentRow(0)
        self.assertEqual(widget.bundle.request.section, "overview")
        self.assertEqual(len(provider.calls), count)
        self.assertTrue(widget.bundle.tables[0].meta.cached)
        widget.query(True)
        self.until(lambda: widget.runner.active is None)
        self.assertGreater(len(provider.calls), count)
