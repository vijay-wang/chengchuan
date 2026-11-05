import os
import tempfile
import threading
import time
import unittest
from dataclasses import replace
from functools import partial
from pathlib import Path

from test_market import sample_frame

os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")

try:
    from PySide6.QtCore import Qt
    from PySide6.QtTest import QTest
    from PySide6.QtWidgets import QApplication

    from chengchuan.application.services import AnalysisService
    from chengchuan.domain.market import Instrument, SearchResult
    from chengchuan.domain.models import IndexRange
    from chengchuan.infrastructure.legacy import LegacyAnalysisEngine, LegacyJsonRepository
    from chengchuan.infrastructure.workspace import SqliteWorkspaceRepository
    from chengchuan.ui.tasks import LatestTaskRunner, ProcessTaskRunner
    from chengchuan.ui.window import MainWindow

    DESKTOP = True
except ImportError:
    DESKTOP = False


class FakeMarket:
    def __init__(self):
        self.requests = []
        self.release = None

    def load(self, symbol, interval, refresh):
        self.requests.append((symbol, interval, refresh))
        if self.release:
            self.release.wait(3)
        return sample_frame(symbol, interval, str(len(self.requests)))

    def search(self, query):
        return SearchResult((Instrument("sh600519", "贵州茅台", "gzmt"),))


@unittest.skipUnless(DESKTOP, "需要desktop可选依赖")
class OnlineDesktopTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.app = QApplication.instance() or QApplication([])

    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.workspace = SqliteWorkspaceRepository(Path(self.temp.name) / "state.sqlite")
        self.market = FakeMarket()
        self.window = self.make_window()
        self.addCleanup(self.window.close)

    def make_window(self):
        window = MainWindow(
            LegacyJsonRepository(),
            AnalysisService(LegacyAnalysisEngine()),
            self.workspace,
            market=self.market,
            network_runner_factory=LatestTaskRunner,
        )
        window.show()
        return window

    def wait_until(self, predicate, timeout=5):
        deadline = time.monotonic() + timeout
        while not predicate() and time.monotonic() < deadline:
            self.app.processEvents()
            QTest.qWait(2)
        self.assertTrue(predicate(), "Qt操作超时")

    def opened(self):
        return (
            self.window.snapshot is not None
            and self.window.market_runner.active is None
            and self.window.runner.active is None
        )

    def test_switch_clears_old_stock_immediately_and_discards_late_results(self):
        window = self.window
        window.open_stock("002318")
        self.wait_until(self.opened)
        self.assertEqual(window.snapshot.series.symbol, "sz002318")
        self.market.release = threading.Event()
        window.open_stock("600519")
        self.assertIsNone(window.snapshot)
        self.assertIsNone(window.report)
        self.assertIsNone(window.chart.snapshot)
        self.assertEqual(window.quote.text(), "—")
        window.open_stock("000001")
        self.market.release.set()
        self.wait_until(self.opened)
        self.assertEqual(window.snapshot.series.symbol, "sz000001")
        self.assertIsNone(window.selection)
        self.assertIsNone(window.report)
        self.assertEqual(window.frame.quote.instrument.symbol, "sz000001")

    def test_refresh_preserves_view_selection_and_escape_state(self):
        window = self.window
        window.open_stock("002318")
        self.wait_until(self.opened)
        window.chart.price.setXRange(3, 12, padding=0)
        window.chart.set_selection(IndexRange(5, 10))
        self.wait_until(self.opened)
        window.refresh_market()
        self.wait_until(self.opened)
        self.assertEqual(window.selection, IndexRange(5, 10))
        view = window.chart.price.getViewBox().viewRange()[0]
        self.assertAlmostEqual(view[0], 3)
        self.assertAlmostEqual(view[1], 12)
        window.clear_selection()
        window.refresh_market()
        self.wait_until(lambda: window.market_runner.active is None)
        self.assertIsNone(window.selection)
        self.assertFalse(window.chart.region.isVisible())

    def test_search_click_and_code_enter_open_stock(self):
        browser = self.window.browser
        browser.search.setText("茅台")
        QTest.keyClick(browser.search, Qt.Key.Key_Return)
        self.wait_until(lambda: browser.results.count() == 1)
        item = browser.results.item(0)
        QTest.mouseClick(
            browser.results.viewport(),
            Qt.MouseButton.LeftButton,
            pos=browser.results.visualItemRect(item).center(),
        )
        self.wait_until(self.opened)
        self.assertEqual(self.window.online_symbol, "sh600519")
        browser.search.setText("000001")
        QTest.keyClick(browser.search, Qt.Key.Key_Return)
        self.wait_until(self.opened)
        self.assertEqual(self.window.online_symbol, "sz000001")

    def test_watchlist_and_refresh_setting_survive_restart(self):
        window = self.window
        window.open_stock("002318")
        self.wait_until(self.opened)
        window._add_current_stock()
        window._add_current_stock()
        self.assertEqual(window.browser.watchlist.count(), 1)
        window.auto_refresh.setChecked(False)
        window.close()
        restored = self.make_window()
        try:
            self.assertEqual(restored.browser.watchlist.count(), 1)
            self.assertFalse(restored.auto_refresh.isChecked())
            self.assertEqual(self.workspace.load("main")["symbol"], "sz002318")
            restored.browser.watchlist.setCurrentRow(0)
            restored.browser.remove_selected()
            self.assertEqual(self.workspace.load("watchlist")["items"], [])
        finally:
            restored.close()

    def test_period_timer_and_cache_status(self):
        window = self.window
        window.open_stock("002318")
        self.wait_until(self.opened)
        before = len(self.market.requests)
        window.auto_refresh.setChecked(False)
        window._auto_refresh()
        self.assertEqual(len(self.market.requests), before)
        window.auto_refresh.setChecked(True)
        window._auto_refresh()
        self.wait_until(
            lambda: len(self.market.requests) > before and window.market_runner.active is None
        )
        window.period.setCurrentIndex(1)
        self.wait_until(self.opened)
        self.assertEqual(window.snapshot.series.interval, "minute")
        cached = replace(window.frame, cached=True, notice="联网失败")
        window._market_ready(cached)
        self.assertIn("历史缓存", window.market_status.text())
        self.assertIn("联网失败", window.market_status.text())
        self.assertIn("报价时间", window.market_status.text())
        self.assertNotIn("已联网", window.market_status.text())

    def test_network_process_replacement_timeout_and_bounded_shutdown(self):
        runner = ProcessTaskRunner()
        self.addCleanup(runner.shutdown)
        results, errors = [], []
        runner.ready.connect(results.append)
        runner.failed.connect(errors.append)
        runner.submit("slow", None, partial(time.sleep, 30))
        runner.submit("latest", None, partial(str, "current"))
        self.wait_until(lambda: bool(results))
        self.assertEqual(results, ["current"])
        self.assertEqual(errors, [])
        with self.assertLogs("chengchuan.ui.tasks", level="ERROR"):
            runner.submit("broken", None, partial(int, "bad-number"))
            self.wait_until(lambda: bool(errors))
        self.assertIn("invalid literal", errors[0])
        errors.clear()
        runner.timeout_ms = 100
        runner.submit("timeout", None, partial(time.sleep, 30))
        self.wait_until(lambda: bool(errors))
        self.assertIn("超时", errors[0])
        runner.submit("close", None, partial(time.sleep, 30))
        start = time.monotonic()
        runner.shutdown()
        self.assertLess(time.monotonic() - start, 1)
        self.assertIsNone(runner.active)

    def test_window_close_terminates_search_and_market_processes(self):
        window = MainWindow(
            LegacyJsonRepository(),
            AnalysisService(LegacyAnalysisEngine()),
            self.workspace,
            market=self.market,
        )
        window.market_runner.submit("slow", None, partial(time.sleep, 30))
        window.browser.runner.submit("slow-search", None, partial(time.sleep, 30))
        started = time.monotonic()
        window.close()
        self.assertLess(time.monotonic() - started, 1)
        self.assertIsNone(window.market_runner.active)
        self.assertIsNone(window.browser.runner.active)


if __name__ == "__main__":
    unittest.main()
