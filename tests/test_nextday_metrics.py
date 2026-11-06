import unittest

from chengchuan.application.nextday_metrics import summarize
from chengchuan.domain.nextday import NextDayEvaluation, NextDayPrediction


def diagnostic(index, predicted, actual, status="evaluated"):
    prediction = NextDayPrediction(
        record_id=str(index),
        symbol="sz002318",
        as_of=f"2026-03-{index:02d}",
        cutoff_at=f"2026-03-{index:02d}T15:15:00+08:00",
        mode="close",
        target=f"2026-03-{index + 1:02d}",
        group="full",
        status="research_only" if predicted is not None else "abstained",
        reason="test",
        feature_revision=str(index),
        direction=predicted,
    )
    evaluation = NextDayEvaluation(
        prediction_id=str(index),
        actual_return_pct=actual,
        actual_direction=actual,
        correct=predicted == actual if predicted is not None and actual is not None else None,
        status=status,
    )
    return prediction, evaluation


class NextDayMetricsTests(unittest.TestCase):
    def test_rejecting_every_down_day_does_not_hide_down_coverage(self):
        rows = [diagnostic(1, 1, 1), diagnostic(2, None, -1)]
        result = summarize(tuple(p for p, _ in rows), tuple(e for _, e in rows))[0]
        self.assertEqual(result["selective_balanced_accuracy"], 1)
        self.assertEqual(result["balanced_accuracy"], result["selective_balanced_accuracy"])
        self.assertEqual(result["evaluated_class_counts"], {"-1": 1, "0": 0, "1": 1})
        self.assertEqual(result["class_prediction_coverage"], {"-1": 0, "0": None, "1": 1})
        self.assertEqual(result["classes"]["-1"]["actual"], 0)
        self.assertEqual(result["classes"]["-1"]["evaluated_actual"], 1)
        self.assertEqual(result["classes"]["-1"]["abstention_as_miss_recall"], 0)
        self.assertEqual(result["abstention_as_miss_balanced_recall"], 0.5)

    def test_wrong_directions_are_covered_but_not_hits(self):
        rows = [diagnostic(1, -1, 1), diagnostic(2, 1, -1), diagnostic(3, None, 0)]
        result = summarize(tuple(p for p, _ in rows), tuple(e for _, e in rows))[0]
        self.assertEqual(result["class_prediction_coverage"], {"-1": 1, "0": 0, "1": 1})
        self.assertEqual(result["abstention_as_miss_balanced_recall"], 0)
        self.assertEqual(result["selective_balanced_accuracy"], 0)

    def test_missing_and_outside_boundary_labels_do_not_enter_class_denominators(self):
        rows = [
            diagnostic(1, 1, 1),
            diagnostic(2, -1, None, status="unavailable"),
            diagnostic(3, None, None, status="outside_boundary"),
        ]
        result = summarize(tuple(p for p, _ in rows), tuple(e for _, e in rows))[0]
        self.assertEqual(result["planned"], 2)
        self.assertEqual(result["evaluated_class_counts"], {"-1": 0, "0": 0, "1": 1})
        self.assertEqual(result["abstention_as_miss_balanced_recall"], 1)

    def test_all_abstained_reports_zero_recall_and_no_selective_accuracy(self):
        rows = [diagnostic(1, None, 1), diagnostic(2, None, -1)]
        result = summarize(tuple(p for p, _ in rows), tuple(e for _, e in rows))[0]
        self.assertIsNone(result["selective_balanced_accuracy"])
        self.assertEqual(result["abstention_as_miss_balanced_recall"], 0)


if __name__ == "__main__":
    unittest.main()
