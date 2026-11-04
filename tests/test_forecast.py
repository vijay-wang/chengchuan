import json
import math
import sqlite3
import tempfile
import unittest
from dataclasses import asdict, replace
from datetime import date, datetime
from pathlib import Path

from chengchuan.application.errors import StorageError
from chengchuan.application.forecast import ForecastService, completed_session, digest
from chengchuan.application.forecast_features import DailyFeatureExtractor, bar_date
from chengchuan.domain.analogue import AnalogueModel
from chengchuan.domain.calendar import CalendarUnavailable, TradingCalendar
from chengchuan.domain.forecast import ForecastConfig
from chengchuan.domain.models import SHANGHAI, MarketBar, MarketSeries, MarketSnapshot, SeriesKind
from chengchuan.infrastructure.forecast_input import decode_snapshot
from chengchuan.infrastructure.forecast_journal import SqliteForecastJournal
from chengchuan.infrastructure.forecast_reports import write_run
from chengchuan.infrastructure.forecast_stage import LegacyStageProvider

NOW = datetime(2026, 11, 3, 17, tzinfo=SHANGHAI)


def synthetic_snapshot(count=150):
    """仅用于正确性测试，不作为市场准确率证据。"""
    calendar = TradingCalendar()
    days = calendar.sessions(date(2026, 3, 2), date(2026, 11, 3))[:count]
    bars = []
    prior = 100.0
    for i, day in enumerate(days):
        close = prior * (1 + 0.004 * math.sin(i * 0.7) + 0.001)
        opening = prior * (1 + 0.001 * math.cos(i * 0.4))
        bars.append(
            MarketBar(
                datetime.combine(day, datetime.min.time(), SHANGHAI).replace(hour=15),
                close,
                100000 * (1 + 0.3 * math.sin(i * 0.43)),
                None,
                opening,
                max(opening, close) * 1.004,
                min(opening, close) * 0.996,
            )
        )
        prior = close
    series = MarketSeries("sz002318", "合成测试", "day", SeriesKind.OHLC, tuple(bars))
    return MarketSnapshot(digest(asdict(series)), series)


def service(journal=None):
    return ForecastService(DailyFeatureExtractor(), AnalogueModel(), journal)


class ForecastTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.snapshot = synthetic_snapshot()
        cls.config = ForecastConfig(neighbors=5, min_train=10, train_window=60)

    def test_calendar_weekend_and_official_holidays(self):
        calendar = TradingCalendar()
        self.assertEqual(calendar.shift(date(2026, 10, 30), 1), date(2026, 11, 2))
        self.assertEqual(calendar.shift(date(2026, 2, 13), 1), date(2026, 2, 24))
        self.assertEqual(calendar.shift(date(2025, 1, 27), 1), date(2025, 2, 5))
        with self.assertRaises(CalendarUnavailable):
            calendar.shift(date(2026, 12, 31), 1)

    def test_ratios_exclude_current_day_and_keep_breakout_values(self):
        snapshot = self.snapshot
        day = bar_date(snapshot.series.bars[100])
        f = DailyFeatureExtractor().extract(snapshot, day)
        values = dict(f.values)
        bars = snapshot.series.bars
        self.assertAlmostEqual(
            math.exp(values["volume_rv1"]), bars[100].volume_shares / bars[99].volume_shares
        )
        self.assertAlmostEqual(
            math.exp(values["volume_rv2"]),
            bars[100].volume_shares / ((bars[98].volume_shares + bars[99].volume_shares) / 2),
        )
        query = replace(bars[100], close=500, open=499, high=501, low=498)
        changed = replace(
            snapshot, series=replace(snapshot.series, bars=bars[:100] + (query,) + bars[101:])
        )
        value = dict(DailyFeatureExtractor().extract(changed, day).values)["price_position_60"]
        self.assertGreater(value, 1)

    def test_zero_volume_and_missing_past_session_are_not_enormous_ratios(self):
        bars = list(self.snapshot.series.bars)
        day = bar_date(bars[100])
        bars[99] = replace(bars[99], volume_shares=0)
        changed = replace(self.snapshot, series=replace(self.snapshot.series, bars=tuple(bars)))
        f = DailyFeatureExtractor().extract(changed, day)
        self.assertIsNone(dict(f.values)["volume_rv1"])
        self.assertFalse(f.eligible)

    def test_flat_range_fields_are_missing_not_midpoint(self):
        bars = self.snapshot.series.bars
        b = bars[100]
        flat = replace(b, open=b.close, high=b.close, low=b.close)
        changed = replace(
            self.snapshot,
            series=replace(self.snapshot.series, bars=bars[:100] + (flat,) + bars[101:]),
        )
        f = DailyFeatureExtractor().extract(changed, bar_date(b))
        self.assertIsNone(dict(f.values)["price_close_location"])
        self.assertTrue(f.eligible)

    def test_future_prices_and_labels_cannot_change_past_prediction(self):
        bars = self.snapshot.series.bars
        day = bar_date(bars[100])
        changed_bars = bars[:101] + tuple(
            replace(b, close=b.close * 4, open=b.open * 4, high=b.high * 4, low=b.low * 4)
            for b in bars[101:]
        )
        changed = replace(
            self.snapshot,
            revision="future-changed",
            series=replace(self.snapshot.series, bars=changed_bars),
        )
        engine = ForecastService(DailyFeatureExtractor(LegacyStageProvider()), AnalogueModel())
        a = engine.predict(self.snapshot, day, self.config, now=NOW)
        b = engine.predict(changed, day, self.config, now=NOW)
        self.assertEqual(a.features, b.features)
        self.assertEqual(a.predictions, b.predictions)
        for p in a.predictions:
            self.assertLessEqual(date.fromisoformat(p.training_label_through), day)

    def test_long_chart_history_does_not_break_existing_research_calendar(self):
        old = replace(
            self.snapshot.series.bars[0], timestamp=datetime(2010, 1, 4, 15, tzinfo=SHANGHAI)
        )
        extended = replace(
            self.snapshot,
            revision="long-chart",
            series=replace(self.snapshot.series, bars=(old,) + self.snapshot.series.bars),
        )
        day = bar_date(self.snapshot.series.bars[100])
        a = service().predict(self.snapshot, day, self.config, now=NOW)
        b = service().predict(extended, day, self.config, now=NOW)
        self.assertEqual(a.features, b.features)
        self.assertEqual(a.predictions, b.predictions)

    def test_nominal_1500_does_not_confirm_a_live_day(self):
        day = date(2026, 11, 3)
        noon = datetime(2026, 11, 3, 13, tzinfo=SHANGHAI)
        self.assertFalse(completed_session(day, noon))
        self.assertFalse(completed_session(day, NOW))
        quote = datetime(2026, 11, 3, 15, tzinfo=SHANGHAI)
        self.assertTrue(completed_session(day, NOW, NOW, quote))
        with self.assertRaises(ValueError):
            completed_session(day, datetime(2026, 11, 3, 17))

    def test_too_little_history_does_not_invent_prediction_or_probability(self):
        snapshot = synthetic_snapshot(65)
        run = service().predict(snapshot, config=self.config, now=NOW)
        self.assertTrue(all(p.direction is None for p in run.predictions))
        self.assertTrue(all(p.calibrated_probabilities is None for p in run.predictions))
        self.assertTrue(all(p.status == "insufficient_training" for p in run.predictions))

    def test_target_date_boundary_and_past_label_training(self):
        snapshot = synthetic_snapshot(180)
        cutoff = bar_date(snapshot.series.bars[-4])
        run = service().backtest(snapshot, cutoff, self.config, now=NOW)
        by_id = {p.record_id: p for p in run.predictions}
        for e in run.evaluations:
            p = by_id[e.prediction_id]
            if e.actual_direction is not None:
                self.assertLess(date.fromisoformat(p.target), cutoff)
            if p.training_label_through:
                self.assertLessEqual(p.training_label_through, p.as_of)
        self.assertTrue(any(e.status == "outside_boundary" for e in run.evaluations))

    def test_missing_target_is_not_shifted_to_next_observation(self):
        bars = self.snapshot.series.bars
        origin = bar_date(bars[100])
        missing = bar_date(bars[101])
        changed = replace(
            self.snapshot, series=replace(self.snapshot.series, bars=bars[:101] + bars[102:])
        )
        run = service().backtest(
            changed, date(2026, 11, 3), self.config, start=origin, groups=("C",), now=NOW
        )
        p = next(p for p in run.predictions if p.as_of == str(origin) and p.horizon == 1)
        e = next(e for e in run.evaluations if e.prediction_id == p.record_id)
        self.assertEqual(p.target, str(missing))
        self.assertIsNone(e.actual_return_pct)
        self.assertEqual(e.status, "unavailable")
        self.assertTrue(
            any(
                p.as_of == str(missing) and p.status == "insufficient_input"
                for p in run.predictions
            )
        )

    def test_summary_denominators_calibration_bins_and_common_samples(self):
        run = service().backtest(self.snapshot, date(2026, 11, 3), self.config, now=NOW)
        actual = {e.prediction_id: e for e in run.evaluations}
        for s in run.summaries:
            self.assertAlmostEqual(s["accuracy"], s["correct"] / s["scored"])
            self.assertAlmostEqual(s["forecast_coverage"], s["predicted"] / s["eligible"])
            count = sum(
                p.raw_probabilities is not None and actual[p.record_id].actual_direction is not None
                for p in run.predictions
                if (p.group, p.horizon) == (s["group"], s["horizon"])
            )
            self.assertEqual(sum(b["count"] for b in s["calibration"]), count)
            self.assertLessEqual(s["common_samples"], s["scored"])

    def test_predictions_committed_before_evaluation(self):
        class Journal:
            def __init__(self):
                self.events = []

            def predictions(self, *args):
                self.events.append("predictions")

            def evaluations(self, *args):
                self.events.append("evaluations")

        journal = Journal()
        service(journal).backtest(
            self.snapshot, date(2026, 11, 3), self.config, groups=("C",), now=NOW
        )
        self.assertEqual(journal.events, ["predictions", "evaluations"])

    def test_journal_is_idempotent_and_rejects_overwriting_prediction(self):
        with tempfile.TemporaryDirectory() as folder:
            path = Path(folder) / "research.sqlite"
            journal = SqliteForecastJournal(path)
            engine = service(journal)
            run = engine.predict(self.snapshot, config=self.config, now=NOW)
            engine.predict(self.snapshot, config=self.config, now=NOW)
            with sqlite3.connect(path) as con:
                self.assertEqual(con.execute("select count(*) from predictions").fetchone()[0], 2)
            changed = (replace(run.predictions[0], direction=0),) + run.predictions[1:]
            with self.assertRaises(StorageError):
                journal.predictions(run.manifest, self.snapshot, changed)

    def test_input_roundtrip_and_report_exports(self):
        restored = decode_snapshot(json.loads(json.dumps(asdict(self.snapshot), default=str)))
        self.assertEqual(restored, self.snapshot)
        run = service().predict(self.snapshot, config=self.config, now=NOW)
        with tempfile.TemporaryDirectory() as folder:
            paths = write_run(run, Path(folder) / "report")
            self.assertTrue(all(Path(path).exists() for path in paths.values()))
            output = json.loads(Path(paths["json"]).read_text())
            self.assertIsNone(output["predictions"][0]["calibrated_probabilities"])

    def test_explicit_excluded_dates_and_invalid_configuration(self):
        day = bar_date(self.snapshot.series.bars[-1])
        config = replace(self.config, excluded_dates=(str(day),))
        run = service().predict(self.snapshot, day, config, now=NOW)
        self.assertTrue(all(p.status == "insufficient_input" for p in run.predictions))
        with self.assertRaises(ValueError):
            ForecastConfig(neighbors=50, min_train=10)


if __name__ == "__main__":
    unittest.main()
