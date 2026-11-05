import os
import subprocess
import sys
import threading
import time
import unittest

from test_company import FakeCompanyProvider, company_table

os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")

try:
    from PySide6.QtCore import Qt
    from PySide6.QtTest import QTest
    from PySide6.QtWidgets import QApplication

    from chengchuan.application.company import CompanyService
    from chengchuan.ui import theme
    from chengchuan.ui.company import CompanyWorkspace
    from chengchuan.ui.tasks import LatestTaskRunner

    DESKTOP = True
except ImportError:
    DESKTOP = False


@unittest.skipUnless(DESKTOP, "需要desktop依赖")
class CompanyDesktopTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.app = QApplication.instance() or QApplication([])

    def setUp(self):
        self.opened_urls = []
        self.provider = FakeCompanyProvider(
            {
                "basic": company_table(
                    "basic", [dict(symbol="002318", name="久立特材", area="浙江")]
                ),
                "valuation": company_table(
                    "valuation",
                    [dict(symbol="002318", tradedate="20261104", pe_ttm=0, total_mv=1e9)],
                ),
                "indicator": company_table(
                    "indicator",
                    [
                        dict(
                            symbol="002318",
                            report_date=d,
                            announce_date="20261027",
                            s_fa_eps_basic=value,
                        )
                        for d, value in (("20260630", -0.2), ("20260930", 0))
                    ],
                ),
                "income": company_table(
                    "income", [dict(symbol="002318", report_date="20260930", revenue_inc=0)]
                ),
                "announcements": company_table(
                    "announcements",
                    [
                        dict(
                            symbol="002318",
                            event_date="20261027",
                            title="报告",
                            content_url="https://example.test/a.pdf",
                        )
                    ],
                ),
            }
        )
        self.widget = CompanyWorkspace(
            CompanyService(self.provider),
            runner_factory=LatestTaskRunner,
            open_url=lambda url: self.opened_urls.append(url.toString()),
        )
        self.widget.setStyleSheet(theme.STYLESHEET)
        self.widget.resize(1500, 900)
        self.widget.show()
        self.addCleanup(self.widget.close)
        self.addCleanup(self.widget.shutdown)

    def wait(self, predicate):
        end = time.monotonic() + 4
        while time.monotonic() < end:
            self.app.processEvents()
            if predicate():
                return
            QTest.qWait(5)
        self.fail("公司资料后台任务未完成")

    def loaded(self, section="overview"):
        self.wait(
            lambda: (
                self.widget.bundle is not None
                and self.widget.bundle.request.section == section
                and self.widget.runner.active is None
            )
        )

    def test_constructor_is_lazy_and_sections_only_load_the_selected_sources(self):
        w = self.widget
        self.assertEqual(self.provider.calls, [])
        w.enter("002318")
        self.loaded()
        self.assertEqual([k for k, _, _ in self.provider.calls], ["basic", "valuation"])
        self.assertIn("久立特材", w.heading.text())
        w.sections.setCurrentRow(7)
        self.loaded("qa")
        self.assertEqual(self.provider.calls[-1][0], "qa")

    def test_matrix_switch_and_filter_do_not_query_and_raw_bundle_is_unchanged(self):
        w = self.widget
        w.symbol_box.setText("002318")
        w.sections.setCurrentRow(2)
        self.loaded("statements")
        raw = w.bundle.tables[0]
        self.assertEqual(w.current_view().table.columns[0].key, "item")
        before = len(self.provider.calls)
        w.matrix_box.setChecked(False)
        self.assertEqual(w.current_view().table.columns[0].key, "symbol")
        w.filter_box.setText("no matching value")
        self.assertEqual(w.current_view().model().rowCount(), 0)
        self.assertEqual(len(self.provider.calls), before)
        self.assertEqual(raw.records()[0]["revenue_inc"], 0)

    def test_company_fields_do_not_use_stock_pool_enum_labels_and_sort_can_restore_order(self):
        from chengchuan.ui.research import ResearchDataView

        data = company_table("holders", [dict(name="B", type="u"), dict(name="A", type="d")])
        view = ResearchDataView(data, natural_order=True)
        try:
            self.assertEqual(view.model().data(view.model().index(0, 1)), "u")
            self.assertEqual(view.record(0)["name"], "B")
            view.sortByColumn(0, Qt.SortOrder.AscendingOrder)
            self.assertEqual(view.record(0)["name"], "A")
            view.sortByColumn(-1, Qt.SortOrder.AscendingOrder)
            self.assertEqual(view.record(0)["name"], "B")
        finally:
            view.close()

    def test_refresh_reuses_view_model_and_manual_network_update_sets_force(self):
        w = self.widget
        w.enter("002318")
        self.loaded()
        view, model = w.current_view(), w.current_view().model()
        w.query(True)
        self.loaded()
        self.assertIs(w.current_view(), view)
        self.assertIs(w.current_view().model(), model)
        self.assertTrue(all(force for _, _, force in self.provider.calls[-2:]))

    def test_field_change_discards_a_late_response_and_new_stock_does_not_keep_old_name(self):
        w = self.widget
        w.enter("002318")
        self.loaded()
        old = w.bundle
        w.active_request = None
        w._ready(old)
        self.assertIs(w.bundle, old)
        started, release = threading.Event(), threading.Event()
        original = self.provider.fetch

        def fetch(key, params, refresh=False):
            if key == "indicator":
                started.set()
                release.wait(2)
            return original(key, params, refresh)

        self.provider.fetch = fetch
        w.sections.setCurrentRow(1)
        self.wait(started.is_set)
        w.symbol_box.setText("600519")
        w._changed()
        w.sections.setCurrentRow(7)
        release.set()
        self.loaded("qa")
        self.assertEqual(w.bundle.request.symbol, "600519")
        self.assertNotIn("久立特材", w.heading.text())

    def test_declared_historical_data_does_not_open_browser_until_explicit_click(self):
        w = self.widget
        w.symbol_box.setText("002318")
        w.sections.setCurrentRow(6)
        self.loaded("announcements")
        view = w.current_view()
        view.selectRow(0)
        w._describe(view, 0)
        self.assertEqual(self.opened_urls, [])
        QTest.mouseClick(w.link_button, Qt.MouseButton.LeftButton)
        self.assertEqual(self.opened_urls, ["https://example.test/a.pdf"])
        w._open_link(dict(content_url="javascript:alert(1)"))
        self.assertEqual(len(self.opened_urls), 1)

    def test_source_reset_clears_company_data_and_preserves_readiness_state(self):
        w = self.widget
        w.enter("002318")
        self.loaded()
        w.set_service(None)
        self.assertIsNone(w.bundle)
        self.assertEqual(w.tabs.count(), 0)
        self.assertFalse(w.query_button.isEnabled())
        w.set_service(CompanyService(self.provider))
        self.assertTrue(w.query_button.isEnabled())
        self.assertIn("就绪", w.status.text())

    def test_negative_and_zero_metric_points_have_a_curve_and_local_scope(self):
        w = self.widget
        w.symbol_box.setText("002318")
        w.sections.setCurrentRow(1)
        self.loaded("indicator")
        self.assertTrue(w.curves)
        x, y = w.curves[0].getData()
        self.assertEqual(list(y), [-0.2, 0])
        before = len(self.provider.calls)
        w.scope_box.setCurrentIndex(1)
        self.app.processEvents()
        self.assertEqual(len(self.provider.calls), before)

    def test_native_repaint_and_close_in_separate_process(self):
        code = """
from PySide6.QtWidgets import QApplication
from chengchuan.ui.company import CompanyWorkspace
app=QApplication([])
for i in range(8):
    w=CompanyWorkspace()
    w.resize(1200+i,800+i)
    w.show()
    w.curve_area.show()
    for j in range(10):
        w.repaint()
        app.processEvents()
    w.shutdown()
    w.close()
    w.deleteLater()
    app.processEvents()
"""
        result = subprocess.run(
            [sys.executable, "-X", "faulthandler", "-c", code],
            capture_output=True,
            text=True,
            timeout=30,
        )
        self.assertEqual(result.returncode, 0, result.stdout + result.stderr)

    def test_main_window_company_entry_preserves_the_existing_market_chart(self):
        from pathlib import Path
        from tempfile import TemporaryDirectory

        import test_market_desktop as harness

        from chengchuan.application.services import AnalysisService
        from chengchuan.infrastructure.legacy import LegacyAnalysisEngine, LegacyJsonRepository
        from chengchuan.infrastructure.workspace import SqliteWorkspaceRepository
        from chengchuan.ui.window import MainWindow

        with TemporaryDirectory() as folder:
            market = harness.FakeMarket()
            market.company_service = CompanyService(self.provider)
            window = MainWindow(
                LegacyJsonRepository(),
                AnalysisService(LegacyAnalysisEngine()),
                SqliteWorkspaceRepository(Path(folder) / "workspace.sqlite"),
                market=market,
                network_runner_factory=LatestTaskRunner,
            )
            window.show()
            try:
                window.auto_refresh.setChecked(False)
                window.open_stock("002318")
                self.wait(lambda: window.frame is not None and window.market_runner.active is None)
                snapshot = window.chart.snapshot
                window.open_company()
                self.wait(
                    lambda: (
                        window.company_page.bundle is not None
                        and window.company_page.runner.active is None
                    )
                )
                self.assertIs(window.pages.currentWidget(), window.company_page)
                self.assertIs(window.chart.snapshot, snapshot)
                self.assertEqual(window.company_page.bundle.request.symbol, "002318")
                window.company_page._back()
                self.assertIs(window.pages.currentWidget(), window.splitter)
                self.assertIs(window.chart.snapshot, snapshot)
            finally:
                window.close()


if __name__ == "__main__":
    unittest.main()
