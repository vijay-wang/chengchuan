import os
import unittest
from dataclasses import replace
from datetime import datetime

from test_nextday_desktop import DESKTOP, synthetic_snapshot

from chengchuan.domain.models import SHANGHAI
from chengchuan.domain.two_up import TwoUpPrediction, TwoUpRun

os.environ["QT_QPA_PLATFORM"] = "offscreen"
if DESKTOP:
    from PySide6.QtWidgets import QApplication
    from test_nextday_desktop import ImmediateRunner

    from chengchuan.ui.two_up_panel import TwoUpPanel


def run_fixture():
    p = TwoUpPrediction(
        "one",
        "sz002318",
        "2026-08-28",
        "2026-08-31",
        "no_80_evidence",
        "历史验证未找到80%方案",
        "r",
        (-0.2, 1, 2),
        candidate_direction=1,
        score=0.9,
        validation_summary=dict(count=0, coverage=0, accuracy=None),
    )
    return TwoUpRun("run", {}, None, (p,), (), (), (), datetime.now(SHANGHAI).isoformat())


class Service:
    two_up = True

    def __init__(self):
        self.calls = []

    def latest_date(self, snapshot, *args):
        return snapshot.series.bars[-1].timestamp.date()

    def predict(self, snapshot, day, config, **kwargs):
        self.calls.append((day, config, kwargs))
        return run_fixture()


@unittest.skipUnless(DESKTOP, "需要desktop可选依赖")
class TwoUpDesktopTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.app = QApplication.instance() or QApplication([])

    def setUp(self):
        self.service = Service()
        self.panel = TwoUpPanel(self.service, runner_factory=ImmediateRunner)
        self.addCleanup(self.panel.close)
        self.addCleanup(self.panel.shutdown)
        self.snapshot = synthetic_snapshot(130)
        self.panel.set_context(self.snapshot)

    def test_old_modes_hidden_even_when_parameters_expanded(self):
        form = self.panel.form_scroll.widget().layout()
        self.assertFalse(form.isRowVisible(self.panel.mode))
        self.assertFalse(form.isRowVisible(self.panel.neighbors))
        self.panel.advanced.setChecked(True)
        self.assertFalse(form.isRowVisible(self.panel.mode))
        self.assertFalse(form.isRowVisible(self.panel.neighbors))
        self.assertEqual(self.service.calls, [])
        self.assertFalse(self.panel.follow.isChecked())

    def test_no_threshold_never_shows_candidate_as_formal_up(self):
        self.panel.predict()
        text = self.panel.output.toPlainText()
        self.assertIn("不预测", text)
        self.assertIn("80%", text)
        self.assertNotIn("：上涨", text)
        self.assertEqual(self.service.calls[0][1].target_accuracy, 0.8)
        self.assertNotIn("mode", self.service.calls[0][2])

    def test_stock_and_configuration_changes_cancel_old_result(self):
        self.panel.predict()
        self.assertIsNotNone(self.panel.run)
        self.panel.excluded.setText("2026-06-12")
        self.assertIsNone(self.panel.run)
        self.panel.set_context(
            replace(self.snapshot, series=replace(self.snapshot.series, symbol="sh600519"))
        )
        self.assertIsNone(self.panel.run)


if __name__ == "__main__":
    unittest.main()
