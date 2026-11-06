import csv
import json
import tempfile
import unittest
from dataclasses import replace
from pathlib import Path

from test_nextday_desktop import sample_run

from chengchuan.domain.nextday import NextDayEvaluation
from chengchuan.infrastructure.nextday_reports import write_run


class NextDayReportsTests(unittest.TestCase):
    def test_reports_preserve_modes_ranges_and_full_evaluation(self):
        run = sample_run("cut1435")
        evaluation = NextDayEvaluation("prediction", 1.5, 1, True, "日内走强", True)
        run = replace(run, evaluations=(evaluation,))
        with tempfile.TemporaryDirectory() as folder:
            files = write_run(run, Path(folder) / "forecast")
            report = json.loads(Path(files["json"]).read_text())
            self.assertEqual(report["predictions"][0]["mode"], "cut1435")
            self.assertIsNone(report["predictions"][0]["calibrated_probabilities"])
            with Path(files["csv"]).open(encoding="utf-8-sig", newline="") as stream:
                row = next(csv.DictReader(stream))
            self.assertEqual(row["actual_return_pct"], "1.5")
            self.assertEqual(json.loads(row["ranges"])[0][0], "close")
            text = Path(files["markdown"]).read_text()
            self.assertIn("14:35尚不知道当日收盘", text)
            self.assertIn("高开回落", text)

    def test_summary_exports_additional_statistics_without_dropping_fields(self):
        run = replace(
            sample_run(),
            features=None,
            summaries=(
                {
                    "group": "full",
                    "scored": 10,
                    "accuracy": 0.6,
                    "paired_up_delta_interval": (-0.1, 0.2),
                    "range_coverage": {"close": 0.75},
                },
            ),
        )
        with tempfile.TemporaryDirectory() as folder:
            files = write_run(run, Path(folder) / "forecast")
            text = Path(files["markdown"]).read_text()
            self.assertIn('"range_coverage"', text)
            self.assertIn("-10.0% ～ 20.0%", text)


if __name__ == "__main__":
    unittest.main()
