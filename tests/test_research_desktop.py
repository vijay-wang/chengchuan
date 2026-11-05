import os
import subprocess
import sys
import tempfile
import threading
import time
import unittest
from pathlib import Path

from test_research import FakeResearchProvider, table

os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")

try:
    from PySide6.QtCore import Qt
    from PySide6.QtTest import QTest
    from PySide6.QtWidgets import QApplication

    from chengchuan.application.research import SpecialtyResearchService
    from chengchuan.domain.research import ResearchRequest
    from chengchuan.infrastructure.workspace import SqliteWorkspaceRepository
    from chengchuan.ui import theme
    from chengchuan.ui.research import ResearchWorkspace
    from chengchuan.ui.tasks import LatestTaskRunner

    DESKTOP = True
except ImportError:
    DESKTOP = False


@unittest.skipUnless(DESKTOP, "需要desktop依赖")
class ResearchDesktopTests(unittest.TestCase):
    def test_native_repaint_and_shutdown_survive_in_independent_process(self):
        code = """
from PySide6.QtWidgets import QApplication
from chengchuan.ui.research import ResearchWorkspace
app = QApplication([])
for i in range(12):
    widget = ResearchWorkspace()
    widget.resize(1200 + i, 800 + i)
    widget.show()
    widget.curve_area.show()
    for j in range(10):
        widget.repaint()
        app.processEvents()
    widget.shutdown()
    widget.close()
    widget.deleteLater()
    app.processEvents()
print("research native repaint ok")
"""
        result = subprocess.run(
            [sys.executable, "-X", "faulthandler", "-c", code],
            capture_output=True,
            text=True,
            timeout=30,
        )
        self.assertEqual(result.returncode, 0, result.stdout + result.stderr)

    @classmethod
    def setUpClass(cls):
        cls.app = QApplication.instance() or QApplication([])

    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.workspace = SqliteWorkspaceRepository(Path(self.temp.name) / "workspace.sqlite")
        self.provider = FakeResearchProvider(
            {
                "rotation": table(
                    "rotation",
                    [
                        dict(
                            theme_symbol="801165k",
                            theme_name="样本板块",
                            level="parent",
                            pct_chg=-1,
                        )
                    ],
                ),
                "minute": table(
                    "minute",
                    [
                        dict(
                            tradedate="20261104",
                            theme_symbol="801165k",
                            trademin=t,
                            pct_chg=p,
                            main_net_amount=n,
                        )
                        for t, p, n in (("0930", -2, -100), ("0935", -1, 20), ("0940", 1, 50))
                    ],
                ),
                "flow": table("flow", [dict(trademin="0935", main_net_amount=-50)]),
                "members": table("members", [dict(theme_symbol="801165k", symbols='["002318"]')]),
                "library": table("library", [dict(theme_id="25", name="题材A")]),
                "events": table(
                    "events",
                    [dict(symbol="002318", tradedate="20261104", servertime="09:31:15", event=2)],
                ),
                "hot_live": table("hot_live", [dict(s="002318", n="久立", pc=0)]),
            }
        )
        self.widget = ResearchWorkspace(
            SpecialtyResearchService(self.provider), self.workspace, runner_factory=LatestTaskRunner
        )
        self.widget.setStyleSheet(theme.STYLESHEET)
        self.widget.resize(1400, 850)
        self.widget.show()
        self.addCleanup(self.widget.close)
        self.addCleanup(self.widget.shutdown)

    def wait_until(self, predicate):
        end = time.monotonic() + 4
        while time.monotonic() < end:
            self.app.processEvents()
            if predicate():
                return
            QTest.qWait(10)
        self.fail("研究界面后台任务未完成")

    def loaded(self, module="rotation", action="overview"):
        self.wait_until(
            lambda: (
                self.widget.bundle is not None
                and self.widget.bundle.request.module == module
                and self.widget.bundle.request.action == action
                and self.widget.runner.active is None
            )
        )

    def open_detail(self):
        self.widget.enter("002318")
        self.loaded()
        view = self.widget.current_view()
        view.doubleClicked.emit(view.model().index(0, 0))
        self.loaded(action="detail")

    def test_no_startup_polling_and_real_navigation_back_without_fetch(self):
        w = self.widget
        self.assertEqual(self.provider.calls, [])
        self.open_detail()
        self.assertEqual(w.bundle.request.code, "801165k")
        self.assertIn("net_5m", {c.key for c in w.current_view().table.columns})
        calls = len(self.provider.calls)
        QTest.mouseClick(w.previous, Qt.MouseButton.LeftButton)
        self.app.processEvents()
        self.assertEqual(w.bundle.request.action, "overview")
        self.assertEqual(len(self.provider.calls), calls)
        QTest.mouseClick(w.nav_buttons["sentiment"], Qt.MouseButton.LeftButton)
        self.loaded("sentiment")

    def test_refresh_reuses_table_model_and_plot_objects_filter_and_drag_do_not_query(self):
        self.open_detail()
        w = self.widget
        view, model, curve = w.current_view(), w.current_view().model(), w.curves[0]
        before = len(self.provider.calls)
        w.filter.setText("0935")
        w.region.setRegion((34200, 34500))
        w.range_toggle.setChecked(True)
        self.app.processEvents()
        self.assertEqual(len(self.provider.calls), before)
        self.assertEqual(view.model().rowCount(), 1)
        w.refresh_current()
        self.loaded(action="detail")
        self.assertIs(w.current_view(), view)
        self.assertIs(w.current_view().model(), model)
        self.assertIs(w.curves[0], curve)

    def test_module_switch_discards_late_old_task_and_source_change_clears_results(self):
        release, started = threading.Event(), threading.Event()
        original = self.provider.fetch

        def fetch(key, *args, **kwargs):
            if key == "rotation":
                started.set()
                release.wait(2)
            return original(key, *args, **kwargs)

        self.provider.fetch = fetch
        w = self.widget
        w.enter()
        self.wait_until(started.is_set)
        w.select_module("sentiment")
        release.set()
        self.loaded("sentiment")
        self.assertNotIn("rotation", w.views)
        w.set_service(None)
        self.app.processEvents()
        self.assertIsNone(w.bundle)
        self.assertEqual(w.tabs.count(), 0)
        self.assertFalse(w.refresh.isEnabled())
        w.set_service(SpecialtyResearchService(self.provider))
        self.assertTrue(w.refresh.isEnabled())
        self.assertIn("已就绪", w.status.text())
        self.assertIsNone(w.bundle)

    def test_event_opens_its_stock_date_time_but_theme_id_never_opens_stock(self):
        w = self.widget
        received = []
        w.stock_requested.connect(lambda *args: received.append(args))
        w.active_request = ResearchRequest(
            "sentiment", "20261104", False, "002318", action="detail"
        )
        w.render(SpecialtyResearchService(self.provider).run(w.active_request))
        view = w.current_view()
        view.doubleClicked.emit(view.model().index(0, 0))
        self.assertEqual(received, [("002318", "20261104", "09:31:15")])
        w._emit_stock(dict(theme_id="25"))
        self.assertEqual(len(received), 1)

    def test_pinned_observation_restores_source_level_and_original_theme_id(self):
        self.open_detail()
        w = self.widget
        w._pin()
        self.assertEqual(w.pins.count(), 2)
        w.level.setCurrentIndex(1)
        w._load_pin(1)
        self.loaded(action="detail")
        self.assertEqual(w.bundle.request.code, "801165k")
        self.assertEqual(w.bundle.request.level, "parent")
        self.assertEqual(self.workspace.load("research_pins")["items"][0]["code"], "801165k")

    def test_platform_comparison_does_not_overwrite_tables_with_same_api_key(self):
        w = self.widget
        w.select_module("attention")
        self.loaded("attention")
        QTest.mouseClick(w.extra, Qt.MouseButton.LeftButton)
        self.loaded("attention", "compare")
        keys = [t.key for t in w.bundle.tables]
        self.assertEqual(len(keys), len(set(keys)))
        self.assertEqual(w.current_view().table.key, "coappearance")
        self.assertEqual(w.current_view().table.records()[0]["platform_count"], 4)

    def test_normalized_stock_input_does_not_discard_matching_result(self):
        self.widget.symbol.setText("sz002318")
        self.widget.select_module("evidence")
        self.loaded("evidence")
        self.assertEqual(self.widget.bundle.request.symbol, "sz002318")


if __name__ == "__main__":
    unittest.main()
