import math
import unittest
from dataclasses import replace
from datetime import date, datetime, time, timedelta

from chengchuan.domain.models import SHANGHAI
from chengchuan.domain.nextday import NextDayConfig, NextDaySample
from chengchuan.domain.nextday_model import CpuNextDayModel


def samples(count=80):
    rows = []
    for index in range(count):
        origin = date(2025, 1, 2) + timedelta(days=index)
        target = origin + timedelta(days=1)
        factor = math.sin(index * 0.67)
        ret = 1.7 * factor + 0.1 * math.cos(index * 1.73)
        opening = 0.3 * ret
        rows.append(
            NextDaySample(
                origin,
                target,
                datetime.combine(target, time(15, 15), SHANGHAI),
                str(index),
                (
                    ("price_trend", factor),
                    ("price_oscillator", math.cos(index * 0.32)),
                    ("volume_ratio", math.sin(index * 0.91)),
                    ("chip_cost", math.cos(index * 0.52)),
                    ("market_missing", None),
                ),
                ret,
                opening,
                max(ret, opening) + 0.4,
                min(ret, opening) - 0.4,
                "日内走强" if ret > opening else "日内走弱",
            )
        )
    return tuple(rows)


class NextDayModelTests(unittest.TestCase):
    def setUp(self):
        self.training = samples()
        self.query = (
            ("price_trend", 0.9),
            ("price_oscillator", 0.1),
            ("volume_ratio", 0.4),
            ("chip_cost", 0.2),
            ("market_missing", None),
        )
        self.config = NextDayConfig(neighbors=11, min_train=40, train_window=100)
        self.model = CpuNextDayModel()

    def test_deterministic_scores_are_finite_and_ranges_match_neighbor_labels(self):
        first = self.model.predict(self.query, self.training, self.config)
        second = self.model.predict(self.query, self.training, self.config)
        self.assertEqual(first, second)
        self.assertAlmostEqual(sum(first.scores), 1)
        self.assertTrue(all(0 < value < 1 for value in first.scores))
        self.assertEqual(len(first.neighbors), self.config.neighbors)
        self.assertEqual(sum(count for _, count in first.shapes), self.config.neighbors)
        selected = [p for p in self.training if p.origin.isoformat() in first.neighbors]
        for name, (lower, center, upper) in first.ranges:
            actual = sorted(getattr(p, name + "_return") for p in selected)
            self.assertEqual(center, actual[len(actual) // 2])
            self.assertLessEqual(actual[0], lower)
            self.assertLessEqual(lower, center)
            self.assertLessEqual(center, upper)
            self.assertLessEqual(upper, actual[-1])
        self.assertNotIn("market_missing", dict(first.contributions))

    def test_positive_and_negative_synthetic_relationship(self):
        # Correctness fixture, not evidence of market prediction accuracy.
        positive = self.model.predict(self.query, self.training, self.config)
        negative_query = (("price_trend", -0.9),) + self.query[1:]
        negative = self.model.predict(negative_query, self.training, self.config)
        self.assertGreater(positive.scores[2], negative.scores[2])

    def test_scaling_and_offsets_do_not_change_model_outputs(self):
        query = tuple(
            (name, value * 100 + 25 if value is not None else None) for name, value in self.query
        )
        training = tuple(
            replace(
                p,
                values=tuple(
                    (name, value * 100 + 25 if value is not None else None)
                    for name, value in p.values
                ),
            )
            for p in self.training
        )
        original = self.model.predict(self.query, self.training, self.config)
        changed = self.model.predict(query, training, self.config)
        self.assertEqual(original.neighbors, changed.neighbors)
        self.assertEqual(original.direction, changed.direction)
        for a, b in zip(original.scores, changed.scores):
            self.assertAlmostEqual(a, b)

    def test_repeating_whole_family_does_not_increase_its_weight(self):
        def repeat_family(values):
            return values + tuple(
                (name + "_copy", value) for name, value in values if name.startswith("price_")
            )

        original = self.model.predict(self.query, self.training, self.config)
        changed = self.model.predict(
            repeat_family(self.query),
            tuple(replace(p, values=repeat_family(p.values)) for p in self.training),
            self.config,
        )
        self.assertEqual(original.neighbors, changed.neighbors)
        for a, b in zip(original.scores, changed.scores):
            self.assertAlmostEqual(a, b)

    def test_missing_query_family_is_not_an_imputed_signal(self):
        query = tuple(
            (name, None if name.startswith("chip_") else value) for name, value in self.query
        )
        a = self.model.predict(query, self.training, self.config)
        changed = tuple(
            replace(
                p,
                values=tuple(
                    (name, 5000 + index if name.startswith("chip_") else value)
                    for name, value in p.values
                ),
            )
            for index, p in enumerate(self.training)
        )
        b = self.model.predict(query, changed, self.config)
        self.assertEqual(a.neighbors, b.neighbors)
        self.assertEqual(a.scores, b.scores)
        self.assertNotIn("chip_cost", dict(a.contributions))

    def test_sparse_and_constant_columns_are_excluded(self):
        training = tuple(
            replace(
                p,
                values=p.values
                + (("behavior_sparse", index if index < 3 else None), ("behavior_constant", 10)),
            )
            for index, p in enumerate(self.training)
        )
        a = self.model.predict(self.query, self.training, self.config)
        b = self.model.predict(
            self.query + (("behavior_sparse", 3), ("behavior_constant", 20)), training, self.config
        )
        self.assertEqual(a.scores, b.scores)
        self.assertEqual(a.neighbors, b.neighbors)

    def test_all_missing_features_abstain_without_fabricated_neighbors(self):
        result = self.model.predict(
            tuple((name, None) for name, _ in self.query), self.training, self.config
        )
        self.assertIsNone(result.direction)
        self.assertEqual(result.neighbors, ())
        self.assertEqual(result.ranges, ())

    def test_high_margin_abstains(self):
        query = tuple((name, 0 if value is not None else None) for name, value in self.query)
        result = self.model.predict(query, self.training, replace(self.config, vote_margin=0.5))
        self.assertIsNone(result.direction)

    def test_local_analogues_and_global_model_disagreement_abstains(self):
        training = []
        for index, point in enumerate(samples(240)):
            value = (index - 120) / 40
            ret = 1 if value > 0 else -1
            if 1.35 < value < 1.65:
                ret = -1
            training.append(
                replace(
                    point,
                    values=(("price_signal", value),),
                    close_return=ret,
                    open_return=0,
                    high_return=2,
                    low_return=-2,
                )
            )
        result = self.model.predict(
            (("price_signal", 1.5),),
            training,
            NextDayConfig(neighbors=5, min_train=40, train_window=240),
        )
        self.assertLess(dict(result.components)["knn"], 0.2)
        self.assertGreater(dict(result.components)["logistic"], 0.6)
        self.assertIsNone(result.direction)
        self.assertIn("分歧", result.reason)

    def test_flat_labels_are_retained_as_flat(self):
        training = tuple(
            replace(
                p, close_return=0, open_return=0, high_return=0.3, low_return=-0.3, shape="窄幅收盘"
            )
            for p in self.training
        )
        result = self.model.predict(self.query, training, self.config)
        self.assertEqual(result.direction, 0)
        self.assertGreater(result.scores[1], 0.8)

    def test_input_validation_rejects_silent_truncation_and_invalid_labels(self):
        bad_inputs = [
            (self.query[::-1], self.training),
            (self.query[:-1], self.training),
            (self.query + (self.query[0],), self.training),
            ((("price_trend", float("nan")),) + self.query[1:], self.training),
            ((("price_trend", True),) + self.query[1:], self.training),
            (self.query, self.training[:10]),
            (self.query, self.training[::-1]),
            (self.query, (self.training[0],) + self.training),
            (self.query, (replace(self.training[0], high_return=-99),) + self.training[1:]),
            (
                self.query,
                (replace(self.training[0], close_return=float("inf")),) + self.training[1:],
            ),
            (
                self.query,
                (replace(self.training[0], available_at=datetime(2025, 1, 3, 15)),)
                + self.training[1:],
            ),
        ]
        for query, training in bad_inputs:
            with self.subTest(query=query, count=len(training)):
                with self.assertRaises(ValueError):
                    self.model.predict(query, training, self.config)


if __name__ == "__main__":
    unittest.main()
