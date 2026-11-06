import json
import sqlite3
import tempfile
import unittest
from dataclasses import replace
from datetime import date, datetime, time
from pathlib import Path

from test_nextday_features import daily_fixture, minute_fixture

from chengchuan.application.nextday import NextDayService, cutoff
from chengchuan.application.nextday_metrics import summarize
from chengchuan.domain.models import SHANGHAI
from chengchuan.domain.nextday import NextDayConfig
from chengchuan.infrastructure.forecast_journal import SqliteForecastJournal

NOW = datetime(2026, 11, 6, 17, tzinfo=SHANGHAI)


class NextDayServiceTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.daily = daily_fixture(125)
        cls.minutes = minute_fixture(cls.daily, 45, 125)
        cls.config = NextDayConfig(neighbors=5, min_train=10, train_window=30)
        cls.day = cls.daily.series.bars[100].timestamp.date()
        cls.service = NextDayService()

    def test_next_session_only_and_future_data_immunity(self):
        a = self.service.predict(self.daily, self.day, self.config, now=NOW)
        rows = self.daily.series.bars
        changed = replace(
            self.daily,
            revision="future-revised",
            series=replace(
                self.daily.series,
                bars=rows[:101]
                + tuple(
                    replace(b, open=b.open * 2, high=b.high * 2, low=b.low * 2, close=b.close * 2)
                    for b in rows[101:]
                ),
            ),
        )
        b = self.service.predict(changed, self.day, self.config, now=NOW)
        self.assertEqual(a.predictions, b.predictions)
        self.assertEqual(len(a.predictions), 1)
        self.assertEqual(a.predictions[0].target, str(self.service.calendar.shift(self.day, 1)))
        self.assertIsNone(a.predictions[0].calibrated_probabilities)

    def test_1435_excludes_today_final_from_model_and_label_maturity(self):
        a = self.service.predict(
            self.daily, self.day, self.config, now=NOW, mode="cut1435", minute_snapshot=self.minutes
        )
        rows = self.daily.series.bars
        changed = replace(
            self.daily,
            revision="final-changed",
            series=replace(
                self.daily.series,
                bars=rows[:100]
                + tuple(
                    replace(
                        b,
                        open=b.open * 2,
                        high=b.high * 2,
                        low=b.low * 2,
                        close=b.close * 2,
                        volume_shares=b.volume_shares * 20,
                    )
                    for b in rows[100:]
                ),
            ),
        )
        later = replace(
            self.minutes,
            revision="late-changed",
            series=replace(
                self.minutes.series,
                bars=tuple(
                    replace(b, open=b.open * 3, high=b.high * 3, low=b.low * 3, close=b.close * 3)
                    if b.timestamp >= cutoff(self.day, "cut1435")
                    else b
                    for b in self.minutes.series.bars
                ),
            ),
        )
        b = self.service.predict(
            changed, self.day, self.config, now=NOW, mode="cut1435", minute_snapshot=later
        )
        self.assertEqual(a.features, b.features)
        self.assertEqual(a.predictions, b.predictions)
        prediction = a.predictions[0]
        self.assertTrue(prediction.model_id)
        self.assertLess(datetime.fromisoformat(prediction.training_label_through).date(), self.day)
        self.assertLess(
            datetime.fromisoformat(prediction.training_label_through), cutoff(self.day, "cut1435")
        )

    def test_no_minute_data_is_explicitly_unavailable(self):
        run = self.service.predict(self.daily, self.day, self.config, now=NOW, mode="cut1435")
        self.assertEqual(run.predictions[0].status, "insufficient_input")
        self.assertIsNone(run.predictions[0].model_id)

    def test_current_completion_guard_and_modes(self):
        now = datetime.combine(self.day, time(14, 34), SHANGHAI)
        with self.assertRaises(ValueError):
            self.service.predict(self.daily, self.day, now=now, mode="cut1435")
        with self.assertRaises(ValueError):
            self.service.predict(self.daily, self.day, now=now.replace(hour=17), mode="close")
        with self.assertRaises(ValueError):
            self.service.predict(self.daily, self.day, now=NOW, mode="arbitrary")

    def test_missing_target_and_excluded_dates_are_not_shifted(self):
        rows = self.daily.series.bars
        missing_day = rows[101].timestamp.date()
        changed = replace(
            self.daily, series=replace(self.daily.series, bars=rows[:101] + rows[102:])
        )
        run = self.service.backtest(
            changed, rows[104].timestamp.date(), self.config, self.day, groups=("full",), now=NOW
        )
        prediction = run.predictions[0]
        self.assertEqual(prediction.target, str(missing_day))
        self.assertEqual(run.evaluations[0].status, "unavailable")
        excluded = replace(self.config, excluded_dates=(str(missing_day),))
        run = self.service.backtest(
            self.daily, rows[104].timestamp.date(), excluded, self.day, groups=("full",), now=NOW
        )
        self.assertIsNone(run.evaluations[0].actual_direction)
        self.assertIn("排除", run.evaluations[0].reason)

    def test_strict_boundary_and_archive_before_evaluation(self):
        class Journal:
            def __init__(self):
                self.events = []

            def predictions(self, *args):
                self.events.append("predictions")

            def evaluations(self, *args):
                self.events.append("evaluations")

        journal = Journal()
        before = self.daily.series.bars[110].timestamp.date()
        run = NextDayService(journal=journal).backtest(
            self.daily, before, self.config, self.day, groups=("full",), now=NOW
        )
        self.assertEqual(journal.events, ["predictions", "evaluations"])
        by_id = {p.record_id: p for p in run.predictions}
        for evaluation in run.evaluations:
            prediction = by_id[evaluation.prediction_id]
            if evaluation.actual_direction is not None:
                self.assertLess(date.fromisoformat(prediction.target), before)
            if prediction.training_label_through:
                self.assertLessEqual(prediction.training_label_through, prediction.cutoff_at)
        self.assertEqual(run.evaluations[-1].status, "outside_boundary")

    def test_archive_keeps_both_cutoffs_and_actual_minute_source(self):
        with tempfile.TemporaryDirectory() as folder:
            path = Path(folder) / "research.sqlite"
            service = NextDayService(journal=SqliteForecastJournal(path))
            close = service.predict(self.daily, self.day, self.config, now=NOW)
            early = service.predict(
                self.daily,
                self.day,
                self.config,
                now=NOW,
                mode="cut1435",
                minute_snapshot=self.minutes,
            )
            again = service.predict(self.daily, self.day, self.config, now=NOW)
            self.assertEqual(close.predictions, again.predictions)
            self.assertNotEqual(close.predictions[0].record_id, early.predictions[0].record_id)
            with sqlite3.connect(path) as con:
                self.assertEqual(con.execute("select count(*) from predictions").fetchone()[0], 2)
                sources = [json.loads(row[0]) for row in con.execute("select data from snapshots")]
            self.assertTrue(any(s["series"]["interval"] == "min1" for s in sources))

    def test_summary_matches_same_calls_and_discloses_abstentions(self):
        before = self.daily.series.bars[113].timestamp.date()
        run = self.service.backtest(self.daily, before, self.config, self.day, now=NOW)
        actuals = {e.prediction_id: e for e in run.evaluations}
        self.assertEqual(tuple(summarize(run.predictions, run.evaluations)), run.summaries)
        for summary in run.summaries:
            scored = [
                p
                for p in run.predictions
                if p.group == summary["group"] and actuals[p.record_id].correct is not None
            ]
            self.assertEqual(summary["scored"], len(scored))
            if scored:
                self.assertEqual(
                    summary["accuracy"],
                    sum(actuals[p.record_id].correct for p in scored) / len(scored),
                )
                self.assertEqual(
                    summary["always_up_accuracy"],
                    sum(actuals[p.record_id].actual_direction == 1 for p in scored) / len(scored),
                )
            self.assertLessEqual(summary["common_samples"], summary["scored"])
            self.assertFalse(summary["independent_holdout_passed"])

    def test_series_and_range_validation(self):
        index = replace(self.daily, series=replace(self.daily.series, symbol="index:sh000300"))
        with self.assertRaises(ValueError):
            self.service.predict(index, self.day, now=NOW)
        with self.assertRaises(ValueError):
            self.service.backtest(self.daily, date(2027, 1, 1), now=NOW)
        with self.assertRaises(ValueError):
            self.service.backtest(self.daily, now=NOW, groups=("full", "full"))


if __name__ == "__main__":
    unittest.main()
