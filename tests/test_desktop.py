import os
import subprocess
import sys
import tempfile
import textwrap
import threading
import time
import unittest
from pathlib import Path

os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")

try:
    from PySide6.QtCore import QPointF, Qt
    from PySide6.QtTest import QTest
    from PySide6.QtWidgets import QApplication

    from chengchuan.application.services import AnalysisService
    from chengchuan.domain.models import IndexRange
    from chengchuan.infrastructure.legacy import LegacyAnalysisEngine, LegacyJsonRepository
    from chengchuan.infrastructure.workspace import SqliteWorkspaceRepository
    from chengchuan.ui.tasks import LatestTaskRunner
    from chengchuan.ui.window import MainWindow

    DESKTOP = True
except ImportError:
    DESKTOP = False

ROOT = Path(__file__).resolve().parents[1]


@unittest.skipUnless(DESKTOP, "需要desktop可选依赖")
class DesktopStabilityTests(unittest.TestCase):
    def run_probe(self, *args):
        # Native binding failures abort the interpreter; also check normal child teardown.
        result = subprocess.run(
            [sys.executable, "-X", "faulthandler", *args],
            cwd=ROOT,
            capture_output=True,
            text=True,
            timeout=60,
        )
        self.assertEqual(result.returncode, 0, result.stdout + result.stderr)
        return result.stdout

    def test_qt_void_calls_preserve_none_references(self):
        self.run_probe(
            "-c",
            textwrap.dedent("""
                import sys
                from PySide6.QtCore import QObject

                obj = QObject()
                for _ in range(20):
                    obj.setObjectName('warmup')
                before = sys.getrefcount(None)
                for _ in range(1000):
                    obj.setObjectName('probe')
                delta = sys.getrefcount(None) - before
                assert delta >= -5, f'Qt void calls changed None refcount by {delta}'
            """),
        )

    def test_repeated_chart_mouse_events_and_snapshot_switches(self):
        output = self.run_probe(
            "-m", "scripts.check_desktop_stability", "--cycles", "2", "--moves", "250"
        )
        self.assertIn('"mouse_moves": 1000', output)


@unittest.skipUnless(DESKTOP, "需要desktop可选依赖")
class DesktopTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.app = QApplication.instance() or QApplication([])

    def wait_until(self, predicate, timeout=5):
        deadline = time.monotonic() + timeout
        while not predicate() and time.monotonic() < deadline:
            self.app.processEvents()
            QTest.qWait(5)
        self.assertTrue(predicate(), "GUI任务超时")

    def test_queue_coalesces_and_discards_late_results(self):
        runner = LatestTaskRunner()
        release = threading.Event()
        calls, received = [], []
        runner.ready.connect(received.append)

        def first():
            calls.append(1)
            release.wait(2)
            return 1

        def second():
            calls.append(2)
            return 2

        def third():
            calls.append(3)
            return 3

        runner.submit("one", None, first)
        runner.submit("two", None, second)
        runner.submit("three", None, third)
        release.set()
        self.wait_until(lambda: runner.active is None)
        self.assertEqual(calls, [1, 3])
        self.assertEqual(received, [3])
        runner.shutdown()

    def test_failed_task_does_not_wedge_runner(self):
        runner = LatestTaskRunner()
        errors, received = [], []
        runner.failed.connect(errors.append)
        runner.ready.connect(received.append)

        def broken():
            raise ValueError("测试失败")

        with self.assertLogs("chengchuan.ui.tasks", "ERROR"):
            runner.submit("broken", None, broken)
            self.wait_until(lambda: runner.active is None)
        self.assertEqual(errors, ["测试失败"])
        runner.submit("next", None, lambda: 42)
        self.wait_until(lambda: runner.active is None)
        self.assertEqual(received, [42])
        runner.shutdown()

    def test_actual_ctrl_drag_recalculates_selected_bars(self):
        with tempfile.TemporaryDirectory() as folder:
            window = MainWindow(
                LegacyJsonRepository(),
                AnalysisService(LegacyAnalysisEngine()),
                SqliteWorkspaceRepository(Path(folder) / "state.sqlite"),
            )
            window.show()
            try:
                window.open_snapshot(ROOT / "reports/jiuli_20261019_1102.dataset.json")
                self.wait_until(lambda: window.report is not None and window.runner.active is None)
                chart = window.chart
                view = chart.price
                vb = view.getViewBox()
                y = sum(vb.viewRange()[1]) / 2
                a = view.mapFromScene(vb.mapViewToScene(QPointF(10, y)))
                b = view.mapFromScene(vb.mapViewToScene(QPointF(20, y)))
                QTest.mousePress(
                    view.viewport(),
                    Qt.MouseButton.LeftButton,
                    Qt.KeyboardModifier.ControlModifier,
                    a,
                )
                QTest.mouseMove(view.viewport(), b, 20)
                QTest.mouseRelease(
                    view.viewport(),
                    Qt.MouseButton.LeftButton,
                    Qt.KeyboardModifier.ControlModifier,
                    b,
                )
                self.wait_until(lambda: window.report is not None and window.runner.active is None)
                self.assertEqual(window.selection, IndexRange(10, 21))
                self.assertEqual(window.report.selection, IndexRange(10, 21))
                QTest.keyClick(window, Qt.Key.Key_Escape)
                self.app.processEvents()
                self.assertIsNone(window.selection)
            finally:
                window.close()
                self.app.processEvents()

    def test_intraday_y_axis_uses_visible_prices_instead_of_all_history(self):
        with tempfile.TemporaryDirectory() as folder:
            window = MainWindow(
                LegacyJsonRepository(),
                AnalysisService(LegacyAnalysisEngine()),
                SqliteWorkspaceRepository(Path(folder) / "state.sqlite"),
            )
            window.show()
            try:
                window.open_snapshot(ROOT / "reports/jiuli_20261102_full_day.dataset.json")
                self.wait_until(lambda: window.report is not None and window.runner.active is None)
                self.assertLess(window.chart.price.getViewBox().viewRange()[1][1], 17)
            finally:
                window.close()
                self.app.processEvents()


if __name__ == "__main__":
    unittest.main()
