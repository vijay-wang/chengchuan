import json
import math
import tempfile
import unittest
from datetime import datetime, timedelta
from pathlib import Path

from flowradar.core import Bar, Dataset, Flow, analyze, boundary, walk_forward
from flowradar.data import daily_bars, parse_flows, parse_minute_days, tape_diagnostics
from flowradar.reports import write_report


def bar(timestamp, price, volume=100, amount=None):
    return Bar(timestamp, price, price, price, price, volume, amount)


class FlowRadarTests(unittest.TestCase):
    def test_rejects_nonfinite_prices_and_negative_volume(self):
        for price, volume in [(math.nan, 100), (math.inf, 100), (0, 100), (10, -1)]:
            with self.assertRaises(ValueError):
                bar("2026-11-02T13:00:00", price, volume)

    def test_cumulative_minute_units_and_lunch(self):
        payload = {"data": {"sz002318": {"data": [{"date": "20261102", "data": [
            "0930 10 10 10000", "1130 10 100 100000",
            "1300 10 100 100000", "1301 10 110 110000", "1509 10 111 111000",
        ]}]}}}
        bars = parse_minute_days(payload, "sz002318")
        self.assertEqual([b.volume_shares for b in bars], [1000, 9000, 0, 1000])
        self.assertEqual(sum(b.amount_yuan for b in bars), 110000)
        self.assertEqual(len(bars), 4)

    def test_rejects_cumulative_volume_reset(self):
        payload = {"data": {"sz002318": {"data": [{"date": "20261102", "data": [
            "0930 10 10 10000", "0931 10 9 9000",
        ]}]}}}
        with self.assertRaises(ValueError):
            parse_minute_days(payload, "sz002318")

    def test_flow_net_is_incremented_before_window_filter(self):
        p = {"data": {"klines": [
            "2026-11-02 11:30,100,-80,-20,60,40",
            "2026-11-02 13:00,100,-80,-20,60,40",
            "2026-11-02 13:01,120,-100,-20,70,50",
        ]}}
        flows = parse_flows(p, cumulative=True)
        data = Dataset("002318", "test", "minute", [
            bar("2026-11-02T13:00:00", 10), bar("2026-11-02T13:01:00", 10.1),
        ], flows)
        r = analyze(data, "2026-11-02T13:00", "2026-11-02T13:01")
        self.assertEqual(r["metrics"]["platform_flow_yuan"]["main_yuan"], 20)

    def test_flow_conservation_rejects_bad_mapping(self):
        with self.assertRaises(ValueError):
            Flow("2026-11-02T15:00:00", 1000, 1000, 1000, 1000, 1000)

    def test_future_data_do_not_change_current_features(self):
        bars = [bar("2026-10-30T15:00:00", 10, 100, 1000),
                bar("2026-11-02T15:00:00", 11, 100, 1100),
                bar("2026-11-03T15:00:00", 11.5, 100, 1150)]
        first = analyze(Dataset("x", "x", "day", bars.copy()), "2026-11-02", "2026-11-03")
        later = analyze(Dataset("x", "x", "day", bars+[bar("2026-11-04T15:00:00", 500, 1e10, 5e12)]),
                        "2026-11-02", "2026-11-03")
        self.assertEqual(first["metrics"], later["metrics"])
        self.assertEqual(first["stages"], later["stages"])

    def test_missing_amount_and_flow_are_not_zero_estimates(self):
        d = Dataset("x", "x", "minute", [bar("2026-11-02T13:00:00", 10),
                                          bar("2026-11-02T13:01:00", 11)])
        r = analyze(d, "2026-11-02", "2026-11-02")
        self.assertIsNone(r["metrics"]["vwap"])
        self.assertIsNone(r["metrics"]["amount_yuan"])
        self.assertIsNone(r["metrics"]["platform_flow_yuan"])
        self.assertIsNone(r["institutional_net_purchase_estimate"]["low_yuan"])
        self.assertIsNone(r["stages"][0]["calibrated_probability"])

    def test_volume_weighted_price_uses_shares_not_hands(self):
        d = Dataset("x", "x", "minute", [bar("2026-11-02T13:00:00", 10, 100, 1000),
                                          bar("2026-11-02T13:01:00", 20, 300, 6000)])
        r = analyze(d, "2026-11-02", "2026-11-02")
        self.assertEqual(r["metrics"]["vwap"], 17.5)

    def test_duplicate_timestamps_rejected(self):
        with self.assertRaises(ValueError):
            Dataset("x", "x", "day", [bar("2026-11-02T15:00:00", 10)]*2)

    def test_provider_sampling_is_reported_as_data_property(self):
        p = {"data": {"details": ["13:00:00,10,1,1,2", "13:00:03,10,2,1,1",
                                    "13:00:06,10,1,1,2", "13:00:12,10,1,1,2"]}}
        result = tape_diagnostics(p)
        self.assertEqual(result["three_second_grid_share"], 1)
        self.assertFalse(result["original_order_ids_available"])

    def test_timezone_and_inverted_window_validation(self):
        with self.assertRaises(ValueError):
            boundary("2026-11-02T13:00:00+08:00")
        d = Dataset("x", "x", "day", [bar("2026-11-02T15:00:00", 10), bar("2026-11-03T15:00:00", 11)])
        with self.assertRaises(ValueError):
            analyze(d, "2026-11-04", "2026-11-01")

    def test_reports_escape_script_in_untrusted_inputs(self):
        name = "</script><img src=x onerror=alert(1)>"
        d = Dataset("x", name, "day", [bar("2026-11-02T15:00:00", 10), bar("2026-11-03T15:00:00", 11)])
        r = analyze(d, "2026-11-02", "2026-11-03")
        with tempfile.TemporaryDirectory() as folder:
            paths = write_report(r, Path(folder)/"report")
            self.assertNotIn(name, Path(paths["html"]).read_text())
            self.assertEqual(json.loads(Path(paths["json"]).read_text())["name"], name)

    def test_roundtrip_data_keeps_amount_units(self):
        d = Dataset("x", "x", "day", [bar("2026-11-02T15:00:00", 10, 10000, 100000)])
        restored = Dataset.from_dict(d.to_dict())
        self.assertEqual(restored.bars[0].amount_yuan, 100000)

    def test_daily_fallback_does_not_fabricate_amount(self):
        class Client:
            warnings = []
            def get(self, url, params):
                if "eastmoney" in url:
                    raise OSError("temporary failure")
                return {"data": {"sz002318": {"day": [
                    ["2026-11-02", "15.78", "16.42", "16.49", "15.78", "111897"]
                ], "qt": {"sz002318": ["51", "久立特材"]}}}}
        client = Client()
        name, bars = daily_bars(client, "002318", "2026-11-02", "2026-11-02")
        self.assertEqual(name, "久立特材")
        self.assertEqual(bars[0].volume_shares, 11189700)
        self.assertIsNone(bars[0].amount_yuan)
        self.assertTrue(client.warnings)

    def test_walk_forward_labels_are_only_future_price_outcomes(self):
        start = datetime(2026, 10, 1, 15)
        bars = [bar((start+timedelta(days=i)).isoformat(), 10+i, 100, (10+i)*100) for i in range(10)]
        r = walk_forward(Dataset("x", "x", "day", bars), 3, 2)
        self.assertEqual(len(r["rows"]), 6)
        self.assertAlmostEqual(r["rows"][0]["forward_return_pct"], (14/12-1)*100)
        self.assertIn("不是主力识别准确率", "".join(r["limitations"]))

    def test_rising_prices_without_drawdown_do_not_enable_wash_label(self):
        bars = [bar("2026-11-02T13:00:00", 10, 100, 1000),
                bar("2026-11-02T13:01:00", 11, 100, 1100)]
        r = analyze(Dataset("x", "x", "minute", bars), "2026-11-02", "2026-11-02")
        wash = next(x for x in r["stages"] if "洗盘" in x["name"])
        self.assertEqual(wash["evidence_score"], 0)

    def test_rising_volume_does_not_claim_falling_volume_evidence(self):
        bars = [bar("2026-10-30T15:00:00", 10),
                bar("2026-11-02T15:00:00", 11, 1000),
                bar("2026-11-03T15:00:00", 12, 1000)]
        r = analyze(Dataset("x", "x", "day", bars), "2026-11-02", "2026-11-03")
        selling = next(x for x in r["stages"] if "卖压" in x["name"])
        self.assertNotIn("下跌伴随成交增加", selling["evidence"])

    def test_missing_requested_dates_are_disclosed(self):
        bars = [bar("2026-11-02T13:00:00", 10), bar("2026-11-02T14:00:00", 11)]
        r = analyze(Dataset("x", "x", "minute", bars), "2026-10-19", "2026-11-02")
        self.assertTrue(any("未覆盖请求的开始日期" in note for note in r["quality_notes"]))


if __name__ == "__main__":
    unittest.main()
