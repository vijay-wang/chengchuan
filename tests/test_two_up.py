import importlib.util
import tempfile
import unittest
from dataclasses import replace
from datetime import date, datetime, time, timedelta
from pathlib import Path
from unittest.mock import patch

from test_nextday_features import daily_fixture

from chengchuan.application.forecast import digest
from chengchuan.application.two_up import (
    TwoUpService,
    causal_prices,
    event_state,
    prepare_history,
    split_mature,
)
from chengchuan.application.two_up_learning import fit_signal, wilson
from chengchuan.application.two_up_metrics import summarize_two_up
from chengchuan.domain.models import SHANGHAI
from chengchuan.domain.two_up import (
    ComparableHistory,
    TwoUpConfig,
    TwoUpEvaluation,
    TwoUpPrediction,
    TwoUpSample,
)
from chengchuan.infrastructure.forecast_journal import SqliteForecastJournal
from chengchuan.infrastructure.two_up_reports import write_run


def history_fixture(count=140):
    snapshot = daily_fixture(count)
    # Deliberate synthetic pattern: down, up, up, up; only the second up day qualifies.
    previous = tuple(
        (b.timestamp.date(), b.close / (0.99 if i % 4 == 0 else 1.01))
        for i, b in enumerate(snapshot.series.bars)
    )
    return ComparableHistory(snapshot, previous)


class TwoUpTests(unittest.TestCase):
    def setUp(self):
        self.history = history_fixture()
        self.days = [b.timestamp.date() for b in self.history.snapshot.series.bars]

    def test_exactly_two_requires_non_up_predecessor_and_three_rising_rejected(self):
        self.assertEqual(event_state(self.history, self.days[102]).status, "confirmed")
        self.assertEqual(event_state(self.history, self.days[103]).status, "not_event")
        previous = dict(self.history.previous_closes)
        previous[self.days[100]] = self.history.snapshot.series.bars[100].close
        flat = replace(self.history, previous_closes=tuple(previous.items()))
        self.assertEqual(event_state(flat, self.days[102]).status, "confirmed")

    def test_gap_and_missing_comparable_are_unknown_not_non_event(self):
        snapshot = self.history.snapshot
        bars = snapshot.series.bars
        missing = replace(
            self.history,
            snapshot=replace(
                snapshot, series=replace(snapshot.series, bars=bars[:100] + bars[101:])
            ),
        )
        self.assertEqual(event_state(missing, self.days[102]).status, "event_unknown")
        missing = replace(
            self.history,
            previous_closes=tuple(
                (d, p) for d, p in self.history.previous_closes if d != self.days[101]
            ),
        )
        self.assertEqual(event_state(missing, self.days[102]).status, "event_unknown")

    def test_adjustment_jump_does_not_create_false_down_day_or_future_scaling(self):
        h = self.history
        bars = h.snapshot.series.bars
        day = self.days[102]
        scaled = tuple(
            replace(b, open=b.open * 0.5, high=b.high * 0.5, low=b.low * 0.5, close=b.close * 0.5)
            if i >= 101
            else b
            for i, b in enumerate(bars)
        )
        previous = tuple((d, p * 0.5 if d >= self.days[101] else p) for d, p in h.previous_closes)
        changed = replace(
            h,
            snapshot=replace(h.snapshot, series=replace(h.snapshot.series, bars=scaled)),
            previous_closes=previous,
        )
        self.assertEqual(event_state(changed, day).status, "confirmed")
        a = causal_prices(h, day)
        b = causal_prices(changed, day)
        for x, y in zip(a.series.bars, b.series.bars):
            self.assertAlmostEqual(x.close, y.close)
        # Future comparable price changes must not alter an earlier feature.
        later = replace(
            changed,
            previous_closes=tuple(
                (d, p * 0.01 if d > day else p) for d, p in changed.previous_closes
            ),
        )
        self.assertEqual(causal_prices(changed, day), causal_prices(later, day))

    def test_future_labels_do_not_change_query_or_formal_prediction(self):
        h = self.history
        day = self.days[102]
        config = TwoUpConfig(min_train=3, min_validation=3, min_signals=3)

        class Fit:
            threshold = None
            model_id = "fake"
            model_name = "test"
            validation_summary = {"status": "no_80_evidence"}

            def predict(self, values):
                return 1, 0.9

        seen = []

        def fit(train, validation, config, **kwargs):
            self.assertGreaterEqual(kwargs["validation_events"], len(validation))
            seen.append((train, validation))
            return Fit()

        def engine(h):
            return TwoUpService(input_loader=lambda _: h)

        now = datetime(2026, 11, 6, 18, tzinfo=SHANGHAI)
        with patch("chengchuan.application.two_up.fit_signal", side_effect=fit):
            a = engine(h).predict(h.snapshot, day, config, now=now)
            future = replace(
                h, previous_closes=tuple((d, p * 5 if d > day else p) for d, p in h.previous_closes)
            )
            b = engine(future).predict(h.snapshot, day, config, now=now)
        self.assertEqual(a.predictions, b.predictions)
        self.assertIsNone(a.predictions[0].direction)
        self.assertEqual(a.predictions[0].candidate_direction, 1)
        for train, validation in seen:
            self.assertLess(max(p.target for p in train), min(p.origin for p in validation))
            self.assertTrue(
                all(
                    p.available_at <= datetime.combine(day, time(15, 15), SHANGHAI)
                    for p in train + validation
                )
            )

    def test_before_close_event_cannot_be_confirmed_by_final_daily_bar(self):
        day = self.days[102]
        with self.assertRaises(ValueError):
            TwoUpService(input_loader=lambda _: self.history).predict(
                self.history.snapshot, day, now=datetime.combine(day, time(14, 35), SHANGHAI)
            )

    def test_background_intraday_cache_cannot_supply_a_completed_label(self):
        target = self.days[103]
        h = replace(self.history, observed_at=datetime.combine(target, time(14), SHANGHAI))
        states, _, samples = prepare_history(h, target)
        self.assertEqual(states[target].status, "event_unknown")
        self.assertFalse(any(p.target == target for p in samples))
        self.assertTrue(any(p.target < target for p in samples))
        closed = replace(h, observed_at=datetime.combine(target, time(16), SHANGHAI))
        _, _, samples = prepare_history(closed, target)
        self.assertTrue(any(p.target == target for p in samples))

    def test_completing_background_cache_does_not_conflict_with_archived_prediction(self):
        day = self.days[102]
        h = self.history
        background = replace(
            h,
            snapshot=replace(
                h.snapshot,
                revision="background",
                series=replace(h.snapshot.series, symbol="sh600519"),
            ),
            previous_closes=tuple(
                (b.timestamp.date(), b.close / (0.99 if (i + 1) % 4 == 0 else 1.01))
                for i, b in enumerate(h.snapshot.series.bars)
            ),
            observed_at=datetime.combine(day, time(14), SHANGHAI),
        )
        now = datetime.combine(day, time(18), SHANGHAI)
        with tempfile.TemporaryDirectory() as folder:
            service = TwoUpService(
                SqliteForecastJournal(Path(folder) / "test.sqlite"),
                lambda _: h,
                lambda: (background,),
            )

            def predict():
                return service.predict(
                    h.snapshot,
                    day,
                    now=now,
                    observed_at=now,
                    quote_at=datetime.combine(day, time(15), SHANGHAI),
                )

            a = predict()
            background = replace(background, observed_at=datetime.combine(day, time(16), SHANGHAI))
            b = predict()
            self.assertEqual(a.predictions[0].status, "insufficient_training")
            self.assertNotEqual(a.experiment_id, b.experiment_id)
            self.assertNotEqual(a.predictions[0].record_id, b.predictions[0].record_id)
            self.assertNotEqual(
                a.manifest["inputs"]["sh600519"]["observed_at"],
                b.manifest["inputs"]["sh600519"]["observed_at"],
            )

    def test_comparable_target_label_not_raw_close_and_report_keeps_refusals(self):
        day = self.days[102]
        events, features, samples = prepare_history(self.history, self.days[106])
        sample = next(p for p in samples if p.origin == day)
        self.assertAlmostEqual(sample.return_pct, 1)
        train, validation = split_mature(samples, self.days[106], TwoUpConfig())
        self.assertTrue(all(p.target < min(q.origin for q in validation) for p in train))
        with tempfile.TemporaryDirectory() as folder:
            service = TwoUpService(
                SqliteForecastJournal(Path(folder) / "test.sqlite"), lambda _: self.history
            )
            run = service.backtest(
                self.history.snapshot,
                self.days[107],
                start=day,
                now=datetime(2026, 11, 6, 18, tzinfo=SHANGHAI),
            )
            self.assertIn(
                "observed_at", run.manifest["inputs"][self.history.snapshot.series.symbol]
            )
            self.assertEqual(run.summaries[0]["confirmed_events"], 1)
            self.assertEqual(run.summaries[0]["coverage"], 0)
            self.assertFalse(run.summaries[0]["point_target_met"])
            files = write_run(run, Path(folder) / "report")
            self.assertTrue(all(Path(p).exists() for p in files.values()))

    def test_metrics_exclude_non_events_but_keep_all_qualifying_abstentions(self):
        statuses = (
            "not_event",
            "event_unknown",
            "insufficient_training",
            "no_80_evidence",
            "research_signal",
        )
        predictions = tuple(
            TwoUpPrediction(
                str(i),
                "sz002318",
                f"2026-03-{i + 1:02d}",
                f"2026-03-{i + 2:02d}",
                s,
                "",
                "r",
                direction=1 if i == 4 else None,
            )
            for i, s in enumerate(statuses)
        )
        evaluations = tuple(
            TwoUpEvaluation(str(i), 1, 1, True if i == 4 else None) for i in range(5)
        )
        s = summarize_two_up(predictions, evaluations)
        self.assertEqual(s["confirmed_events"], 3)
        self.assertEqual(s["coverage"], 1 / 3)
        self.assertEqual(s["accuracy"], 1)
        self.assertTrue(s["point_target_met"])
        self.assertFalse(s["reliability_gate_passed"])
        self.assertLess(wilson(80, 100)[0], 0.8)

    def test_unresolved_signals_cannot_pass_acceptance_using_only_available_hits(self):
        predictions, evaluations = [], []
        for i in range(1000):
            day = date(2026, 1, 2) + timedelta(days=i % 100)
            predictions.append(
                TwoUpPrediction(
                    str(i),
                    "sz002318",
                    str(day),
                    str(day + timedelta(days=1)),
                    "research_signal",
                    "",
                    "r",
                    direction=1,
                )
            )
            evaluations.append(
                TwoUpEvaluation(str(i), 1, 1, True)
                if i < 100
                else TwoUpEvaluation(str(i), status="unavailable")
            )
        s = summarize_two_up(predictions, evaluations)
        self.assertEqual(s["accuracy"], 1)
        self.assertEqual(s["called_label_coverage"], 0.1)
        self.assertEqual(s["unresolved_signals"], 900)
        self.assertEqual(s["conservative_accuracy"], 0.1)
        self.assertFalse(s["reliability_gate_passed"])


@unittest.skipUnless(importlib.util.find_spec("sklearn"), "需要prediction可选CPU依赖")
class TwoUpLearningTests(unittest.TestCase):
    @staticmethod
    def samples(first, count):
        out = []
        for i in range(count):
            day = first + timedelta(days=i * 2)
            target = day + timedelta(days=1)
            label = 1 if i % 2 else -1
            out.append(
                TwoUpSample(
                    "sz002318",
                    day,
                    target,
                    datetime.combine(target, time(15, 15), SHANGHAI),
                    digest(i),
                    (("price_signal", label), ("volume_missing", None)),
                    float(label),
                )
            )
        return tuple(out)

    def test_finite_grid_gate_and_deterministic_validation_only(self):
        train = self.samples(date(2025, 1, 2), 90)
        validation = self.samples(date(2026, 1, 2), 45)
        config = TwoUpConfig()
        fitted = fit_signal(train, validation, config)
        self.assertEqual(len(fitted.candidate_results), 6)
        self.assertIsNotNone(fitted.threshold)
        self.assertEqual(fitted.predict((("price_signal", 1), ("volume_missing", None)))[0], 1)
        self.assertFalse(fitted.validation_summary["heldout_verified"])
        with self.assertRaises(ValueError):
            fit_signal(train, train[:45], config)
        with self.assertRaises(ValueError):
            fitted.predict((("renamed_signal", 1), ("volume_missing", None)))

    def test_no_80_validation_evidence_never_emits_gate(self):
        train = self.samples(date(2025, 1, 2), 90)
        val = self.samples(date(2026, 1, 2), 45)
        # Contradictory validation: every available feature is identical across opposite labels.
        val = tuple(replace(p, values=(("price_signal", 0), ("volume_missing", None))) for p in val)
        fit = fit_signal(train, val, TwoUpConfig())
        self.assertIsNone(fit.threshold)
        self.assertEqual(fit.validation_summary["status"], "no_80_evidence")

    def test_validation_coverage_includes_qualifying_events_with_missing_inputs(self):
        train = self.samples(date(2025, 1, 2), 90)
        val = self.samples(date(2026, 1, 2), 45)
        fit = fit_signal(train, val, TwoUpConfig(), validation_events=500)
        self.assertIsNone(fit.threshold)
        self.assertEqual(fit.validation_summary["validation_events"], 500)
        self.assertTrue(
            all(t["coverage"] <= 0.09 for c in fit.candidate_results for t in c["thresholds"])
        )


if __name__ == "__main__":
    unittest.main()
