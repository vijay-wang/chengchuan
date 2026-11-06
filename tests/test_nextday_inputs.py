import hashlib
import json
import tempfile
import time
import unittest
from datetime import date
from pathlib import Path
from unittest.mock import patch

from test_nextday_features import daily_fixture

from chengchuan.infrastructure.meoz_client import MeozStore
from chengchuan.infrastructure.meoz_market import MINUTE_FIELDS
from chengchuan.infrastructure.nextday_inputs import CachedNextDayInputs


class CachedNextDayInputsTests(unittest.TestCase):
    def test_missing_cache_makes_no_requests_or_credentials_lookup(self):
        with tempfile.TemporaryDirectory() as folder:
            store = MeozStore(Path(folder) / "cache.sqlite")
            inputs = CachedNextDayInputs(store)
            with (
                patch(
                    "chengchuan.infrastructure.meoz_client.private_key",
                    side_effect=AssertionError("no auth"),
                ),
                patch(
                    "chengchuan.infrastructure.meoz_client.MeozClient.query",
                    side_effect=AssertionError("no HTTP"),
                ),
            ):
                self.assertIsNone(inputs.minutes("sz002318", (date(2026, 11, 5),)))
                snapshot = daily_fixture()
                self.assertIs(inputs.context(snapshot), snapshot)
            self.assertEqual(store.usage()["total"], 0)

    def test_explicit_minute_cache_identity_and_date_filter(self):
        with tempfile.TemporaryDirectory() as folder:
            store = MeozStore(Path(folder) / "cache.sqlite")
            fields = MINUTE_FIELDS.split(",")
            data = dict(
                tradedate="20261105",
                symbol="002318",
                trademin="1434",
                time=None,
                open=20,
                high=21,
                low=19,
                close=20,
                vol=10,
                amount=20000,
                vwap=None,
            )
            payload = dict(code=200, data=dict(fields=fields, items=[[data[k] for k in fields]]))
            body = dict(
                apiname="minute",
                params=dict(symbols="002318", period="1m", tradedate="20261105"),
                fields=MINUTE_FIELDS,
            )
            key = hashlib.sha256(json.dumps(["history", body], sort_keys=True).encode()).hexdigest()
            with store.connect() as con:
                con.execute(
                    "insert into responses values(?,?,?)", (key, time.time(), json.dumps(payload))
                )
            inputs = CachedNextDayInputs(store)
            snapshot = inputs.minutes("sz002318", (date(2026, 11, 5),))
            self.assertEqual(snapshot.series.interval, "min1")
            self.assertEqual(snapshot.series.bars[0].volume_shares, 1000)
            self.assertEqual(snapshot.series.bars[0].timestamp.minute, 34)
            self.assertIsNone(inputs.minutes("sz002318", (date(2026, 11, 4),)))
            self.assertEqual(store.usage()["total"], 0)

    def test_benchmark_is_clipped_to_input_date_and_preserves_explicit_benchmark(self):
        with tempfile.TemporaryDirectory() as folder:
            store = MeozStore(Path(folder) / "cache.sqlite")
            snapshot = daily_fixture(100)
            last = snapshot.series.bars[-1].timestamp.strftime("%Y%m%d")
            rows = [
                dict(
                    tradedate=d,
                    symbol="000300.SH",
                    open=3000,
                    high=3100,
                    low=2900,
                    close=3050,
                    vol=100,
                    amount=30000,
                )
                for d in (last, "20261105")
            ]
            store.save_daily("index:sh000300", rows, time.time(), "20261105")
            inputs = CachedNextDayInputs(store)
            enriched = inputs.context(snapshot)
            self.assertEqual(len(enriched.benchmark.bars), 1)
            self.assertLessEqual(
                enriched.benchmark.bars[-1].timestamp, snapshot.series.bars[-1].timestamp
            )
            self.assertIs(inputs.context(enriched), enriched)


if __name__ == "__main__":
    unittest.main()
