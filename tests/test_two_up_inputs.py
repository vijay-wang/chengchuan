import json
import tempfile
import time
import unittest
from dataclasses import replace
from datetime import datetime, timezone
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import patch

from test_nextday_features import daily_fixture

from chengchuan.infrastructure.meoz_client import MeozStore
from chengchuan.infrastructure.two_up_inputs import UNIVERSE, CachedTwoUpInputs

try:
    from scripts.collect_two_up_data import collect, validate_batch
except ModuleNotFoundError:
    collect = validate_batch = None


def raw_rows(snapshot):
    return [
        dict(
            symbol=snapshot.series.symbol[-6:],
            name=snapshot.series.name,
            tradedate=b.timestamp.strftime("%Y%m%d"),
            open=b.open,
            high=b.high,
            low=b.low,
            close=b.close,
            pre_close=b.close / 1.02,
            vol=b.volume_shares / 100,
            amount=b.amount_yuan,
        )
        for b in snapshot.series.bars
    ]


class TwoUpInputsTests(unittest.TestCase):
    def test_no_cache_or_credentials_does_not_guess_comparable_close(self):
        with tempfile.TemporaryDirectory() as folder:
            store = MeozStore(Path(folder) / "cache.sqlite")
            snapshot = daily_fixture(4)
            with patch(
                "chengchuan.infrastructure.meoz_client.private_key",
                side_effect=AssertionError("no auth"),
            ):
                history = CachedTwoUpInputs(store).history(snapshot)
            self.assertEqual(history.previous_closes, ())
            self.assertTrue(any("不以相邻日" in note for note in history.snapshot.quality_notes))
            self.assertEqual(store.usage()["total"], 0)

    def test_only_matching_prices_join_and_future_rows_do_not_leak(self):
        with tempfile.TemporaryDirectory() as folder:
            store = MeozStore(Path(folder) / "cache.sqlite")
            full = daily_fixture(6)
            rows = raw_rows(full)
            rows[1]["high"] += 2
            rows[2]["pre_close"] = None
            store.save_daily(full.series.symbol, rows, time.time(), "20260309")
            snapshot = replace(full, series=replace(full.series, bars=full.series.bars[:4]))
            history = CachedTwoUpInputs(store).history(snapshot)
            self.assertEqual(
                [d for d, _ in history.previous_closes],
                [full.series.bars[i].timestamp.date() for i in (0, 3)],
            )
            self.assertTrue(any("不一致" in note for note in history.snapshot.quality_notes))
            self.assertTrue(
                all(
                    d <= snapshot.series.bars[-1].timestamp.date()
                    for d, _ in history.previous_closes
                )
            )

    def test_invalid_bases_and_adjusted_input_do_not_get_raw_preclose(self):
        with tempfile.TemporaryDirectory() as folder:
            store = MeozStore(Path(folder) / "cache.sqlite")
            snapshot = daily_fixture(5)
            rows = raw_rows(snapshot)
            for row, value in zip(rows, (0, -1, None, "NaN", True)):
                row["pre_close"] = value
            store.save_daily(snapshot.series.symbol, rows, time.time(), "20260306")
            inputs = CachedTwoUpInputs(store)
            self.assertFalse(inputs.history(snapshot).previous_closes)
            valid = raw_rows(snapshot)
            store.save_daily(snapshot.series.symbol, valid, time.time(), "20260306")
            adjusted = replace(
                snapshot,
                series=replace(
                    snapshot.series,
                    bars=tuple(
                        replace(
                            b, open=b.open * 2, high=b.high * 2, low=b.low * 2, close=b.close * 2
                        )
                        for b in snapshot.series.bars
                    ),
                ),
            )
            self.assertFalse(inputs.history(adjusted).previous_closes)

    def test_pool_is_fixed_order_and_load_supports_other_cached_stocks(self):
        with tempfile.TemporaryDirectory() as folder:
            store = MeozStore(Path(folder) / "cache.sqlite")
            snapshot = daily_fixture(4)
            for symbol in ("sz000651", "sz002318", "sh600000"):
                new = replace(snapshot, series=replace(snapshot.series, symbol=symbol))
                store.save_daily(symbol, raw_rows(new), time.time(), "20260305")
            inputs = CachedTwoUpInputs(store)
            self.assertEqual(
                tuple(h.snapshot.series.symbol for h in inputs.pool()), ("sz002318", "sz000651")
            )
            self.assertIsNotNone(inputs.load("sh600000"))
            self.assertIsNone(inputs.load("sz300750"))

    @unittest.skipUnless(validate_batch, "需要源码中的采集脚本")
    def test_batch_validation_rejects_extra_dates_symbols_and_duplicates(self):
        row = raw_rows(daily_fixture(1))[0]
        groups, quality = validate_batch((row,), ("sz002318",))
        self.assertEqual(len(groups["sz002318"]), 1)
        self.assertGreater(len(quality[0]["missing_sessions"]), 0)
        for rows in ((row, row), (dict(row, symbol="600519"),), (dict(row, tradedate="20261106"),)):
            with self.assertRaises(ValueError):
                validate_batch(rows, ("sz002318",))

    @unittest.skipUnless(collect, "需要源码中的采集脚本")
    def test_collection_freezes_before_three_batches_and_keeps_old_rows(self):
        with tempfile.TemporaryDirectory() as folder:
            folder = Path(folder)
            store = MeozStore(folder / "cache.sqlite")
            output = folder / "collected"
            calls = []
            template = raw_rows(daily_fixture(1))[0]
            old = dict(template, tradedate="20240102")
            store.save_daily("sz002318", (old,), time.time(), "20240102")

            class Client:
                def query(self, api, params, fields, **kwargs):
                    self.frozen = list(output.glob("collection-*/protocol.json"))
                    if not self.frozen:
                        raise AssertionError("protocol must be written before querying")
                    calls.append((api, params, fields, kwargs))
                    return SimpleNamespace(
                        rows=tuple(
                            dict(template, symbol=code) for code in params["symbols"].split(",")
                        ),
                        fetched_at=datetime.now(timezone.utc),
                        url="https://sz.meoz.cn:6688/api",
                        cached=False,
                    )

            result = collect(output, fetch=True, store=store, client=Client())
            self.assertEqual(len(calls), 3)
            self.assertTrue(all(call[3]["service"] == "live" for call in calls))
            self.assertTrue(all("apikey" not in call[1] for call in calls))
            self.assertEqual(len(result["snapshots"]), len(UNIVERSE))
            self.assertEqual(len(store.daily_history("sz002318")[0]), 2)
            protocol = json.loads((Path(result["directory"]) / "protocol.json").read_text())
            self.assertEqual(len(protocol["final_holdout_symbols"]), 10)
            self.assertFalse(result["outcomes_evaluated"])
            with self.assertRaises(FileExistsError):
                (Path(result["directory"]) / "protocol.json").open("x")


if __name__ == "__main__":
    unittest.main()
