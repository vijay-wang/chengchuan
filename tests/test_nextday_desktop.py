import os
import unittest
from dataclasses import replace
from datetime import date, datetime

from test_forecast import synthetic_snapshot

from chengchuan.domain.models import SHANGHAI
from chengchuan.domain.nextday import NextDayFeatures, NextDayPrediction, NextDayRun

os.environ["QT_QPA_PLATFORM"] = "offscreen"

try:
    from PySide6.QtCore import QDate, QObject, Signal
    from PySide6.QtWidgets import QApplication

    from chengchuan.ui.nextday_panel import NextDayPanel

    DESKTOP = True
except ImportError:
    DESKTOP = False


def sample_run(mode="close", status="research_only", direction=1):
    day = date(2026, 8, 28)
    cutoff = datetime(2026, 8, 28, 15, 15, tzinfo=SHANGHAI)
    if mode == "cut1435":
        cutoff = cutoff.replace(hour=14, minute=35)
    features = NextDayFeatures(
        day,
        cutoff,
        mode,
        "features",
        (("price_return_1", 0.02),),
        status == "research_only",
        reference_price=23,
        levels=(("此前20日低点", 21.5),),
        evidence=("成交分布为筹码代理，不是真实持仓",),
        coverage_at=cutoff.isoformat(),
    )
    prediction = NextDayPrediction(
        "prediction",
        "sz002318",
        str(day),
        cutoff.isoformat(),
        mode,
        "2026-08-31",
        "full",
        status,
        "历史分钟不足" if direction is None else "研究方向",
        "features",
        train_count=80,
        direction=direction,
        ranges=(("close", (-2.0, 0.6, 3.0)),) if direction is not None else (),
        shapes=(("日内走强", 12), ("高开回落", 9)) if direction is not None else (),
    )
    return NextDayRun(
        "experiment",
        {"name": "久立", "symbol": "sz002318", "forecast_mode": mode},
        features,
        (prediction,),
        (),
        (),
        ("原始价格研究，未校准",),
    )


if DESKTOP:

    class ImmediateRunner(QObject):
        ready = Signal(object)
        failed = Signal(str)
        busy = Signal(bool)

        def __init__(self, parent):
            super().__init__(parent)
            self.cancel_count = 0
            self.closed = False
            self.calls = []

        def cancel(self):
            self.cancel_count += 1

        def submit(self, revision, window, operation):
            self.calls.append((revision, window))
            self.busy.emit(True)
            try:
                self.ready.emit(operation())
            except Exception as exc:
                self.failed.emit(str(exc))
            finally:
                self.busy.emit(False)

        def shutdown(self):
            self.closed = True


class StubService:
    next_day = True

    def __init__(self):
        self.calls = []

    def latest_date(self, snapshot, now, observed_at=None, quote_at=None):
        return snapshot.series.bars[-1].timestamp.date()

    def predict(self, snapshot, as_of, config, **kwargs):
        self.calls.append(("predict", as_of, kwargs))
        return sample_run(kwargs["mode"])

    def backtest(self, snapshot, before, config, **kwargs):
        self.calls.append(("backtest", before, kwargs))
        run = sample_run(kwargs["mode"])
        return replace(
            run,
            features=None,
            summaries=(
                {
                    "group": "full",
                    "scored": 80,
                    "accuracy": 0.55,
                    "always_up_accuracy": 0.6,
                    "forecast_coverage": 0.8,
                },
            ),
        )


@unittest.skipUnless(DESKTOP, "需要desktop可选依赖")
class NextDayDesktopTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.app = QApplication.instance() or QApplication([])

    def setUp(self):
        self.snapshot = synthetic_snapshot(130)
        self.service = StubService()
        self.panel = NextDayPanel(self.service, runner_factory=ImmediateRunner)
        self.addCleanup(self.panel.close)
        self.addCleanup(self.panel.shutdown)
        self.panel.set_context(self.snapshot)

    def test_mode_and_dates_forward_to_service_without_auto_training(self):
        self.assertEqual(self.service.calls, [])
        self.assertFalse(self.panel.follow.isChecked())
        self.panel.mode.setCurrentIndex(1)
        self.assertEqual(self.service.calls, [])
        self.panel.predict()
        self.assertEqual(self.service.calls[-1][2]["mode"], "cut1435")
        self.assertIn("14:35尚不知道", self.panel.output.toPlainText())
        self.panel.start.setDate(QDate(2026, 7, 1))
        self.panel.before.setDate(QDate(2026, 11, 3))
        self.panel.backtest()
        self.assertEqual(self.service.calls[-1][1], date(2026, 11, 3))
        self.assertEqual(self.service.calls[-1][2]["start"], date(2026, 7, 1))
        self.assertIn("同样本全猜涨 60.0%", self.panel.output.toPlainText())

    def test_unchanged_context_preserves_result_stock_change_clears(self):
        self.panel.predict()
        run = self.panel.run
        self.panel.set_context(replace(self.snapshot, revision="quote-refresh"))
        self.assertIs(self.panel.run, run)
        other = replace(self.snapshot, series=replace(self.snapshot.series, symbol="sh600519"))
        self.panel.set_context(other)
        self.assertIsNone(self.panel.run)
        self.panel.set_context(None)
        self.assertFalse(self.panel.predict_button.isEnabled())

    def test_insufficient_minute_data_displays_no_prediction(self):
        self.panel._ready(sample_run("cut1435", "insufficient_input", None))
        text = self.panel.output.toPlainText()
        self.assertIn("暂不判断", text)
        self.assertIn("历史分钟不足", text)
        self.assertNotIn("P10 / P50", text)

    def test_presentation_is_plain_text_and_has_levels_and_scenarios(self):
        self.panel._ready(sample_run())
        text = self.panel.output.toPlainText()
        self.assertIn("此前20日低点：21.500", text)
        self.assertIn("高开回落：9例", text)
        self.assertIn("-2.00% / +0.60% / +3.00%", text)
        self.assertNotIn("H2", text)
        self.assertFalse(self.panel.export_button.isEnabled())

    def test_invalid_backtest_boundary_does_not_start_task(self):
        self.panel.start.setDate(QDate(2026, 11, 3))
        self.panel.before.setDate(QDate(2026, 11, 3))
        self.panel.backtest()
        self.assertEqual(self.service.calls, [])
        self.assertIn("须早于", self.panel.status.text())

    def test_excluded_dates_are_validated_before_submitting(self):
        self.panel.excluded.setText("2026-06-12，2026-08-04")
        self.assertEqual(self.panel._config().excluded_dates, ("2026-06-12", "2026-08-04"))
        self.panel.excluded.setText("invalid")
        self.panel.predict()
        self.assertEqual(self.service.calls, [])
        self.assertIn("配置无效", self.panel.status.text())


if __name__ == "__main__":
    unittest.main()
