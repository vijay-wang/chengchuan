import io
import json
import tempfile
import time
import unittest
from concurrent.futures import ThreadPoolExecutor
from datetime import datetime, timedelta
from pathlib import Path
from unittest.mock import patch
from urllib.error import HTTPError

from chengchuan.application.errors import MarketError
from chengchuan.application.resampling import calendar_bars, minute_bars
from chengchuan.domain.models import SHANGHAI, MarketBar
from chengchuan.domain.periods import PERIODS
from chengchuan.infrastructure.market_cache import decode_frame, encode_frame
from chengchuan.infrastructure.meoz_client import MeozClient, MeozStore, decode_rows
from chengchuan.infrastructure.meoz_market import MeozMarketProvider, parse_rows

NOW = datetime(2026, 11, 4, 16, 30, tzinfo=SHANGHAI)
TEST_KEY = "nc_" + "x" * 32
DAYS = ("20261029", "20261030", "20261102", "20261103", "20261104")


def payload(rows):
    fields = list(rows[0]) if rows else ["symbol"]
    return dict(code=200, data=dict(fields=fields, items=[[r[k] for k in fields] for r in rows]))


def minute_rows(day):
    clocks = list(range(570, 691)) + list(range(781, 901))
    return [
        dict(
            tradedate=day,
            symbol="002318.SZ",
            trademin=f"{m // 60:02}{m % 60:02}",
            time="09:25:00" if m == 570 else f"{m // 60:02}:{m % 60:02}:00",
            open=10,
            high=11,
            low=9,
            close=10,
            vol=2,
            amount=2000,
            vwap=10,
        )
        for m in clocks
    ]


class MeozTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.store = MeozStore(Path(self.temp.name) / "meoz.sqlite")
        self.client = MeozClient(self.store)

    def test_decodes_fields_order_and_rejects_misaligned_columns(self):
        self.assertEqual(
            decode_rows(dict(code=200, data=dict(fields=["vol", "symbol"], items=[[2, "002318"]]))),
            ({"vol": 2, "symbol": "002318"},),
        )
        with self.assertRaisesRegex(MarketError, "列数"):
            decode_rows(dict(code=200, data=dict(fields=["symbol"], items=[[1, 2]])))

    def test_cache_does_not_repeat_http_and_never_saves_credentials(self):
        response = payload([dict(symbol="002318", close=10)])
        with (
            patch("chengchuan.infrastructure.meoz_client.private_key", return_value=TEST_KEY),
            patch(
                "chengchuan.infrastructure.meoz_client.urlopen",
                return_value=io.BytesIO(json.dumps(response).encode()),
            ) as http,
        ):
            first = self.client.query("daily", dict(symbols="002318"))
            second = MeozClient(MeozStore(self.store.path)).query("daily", dict(symbols="002318"))
        self.assertEqual(http.call_count, 1)
        self.assertFalse(first.cached)
        self.assertTrue(second.cached)
        self.assertEqual(first.fetched_at, second.fetched_at)
        self.assertEqual(self.store.usage()["total"], 1)
        with self.store.connect() as con:
            self.assertNotIn(TEST_KEY, str(con.execute("SELECT * FROM responses").fetchall()))
            self.assertEqual(con.execute("SELECT status FROM requests").fetchone()[0], "success")
        with (
            patch(
                "chengchuan.infrastructure.meoz_client.private_key",
                side_effect=AssertionError("offline must not need a key"),
            ),
            patch(
                "chengchuan.infrastructure.meoz_client.urlopen",
                side_effect=AssertionError("offline network"),
            ),
        ):
            offline = MeozClient(self.store, offline=True).query(
                "daily", dict(symbols="002318"), max_age=0
            )
        self.assertEqual(offline.rows, first.rows)
        with patch(
            "chengchuan.infrastructure.meoz_market.TencentMarketProvider",
            side_effect=AssertionError("offline search must not connect"),
        ):
            with self.assertRaisesRegex(MarketError, "离线"):
                MeozMarketProvider(MeozClient(self.store, offline=True)).search("久立")

    def test_403_is_counted_once_and_suppresses_repeated_requests(self):
        error = HTTPError("https://sz.meoz.cn:6688/api", 403, "Forbidden", {}, io.BytesIO())
        with (
            patch("chengchuan.infrastructure.meoz_client.private_key", return_value=TEST_KEY),
            patch("chengchuan.infrastructure.meoz_client.urlopen", side_effect=error) as http,
        ):
            for _ in range(2):
                with self.assertRaises(MarketError):
                    self.client.query("minute", dict(symbols="002318"))
        self.assertEqual(http.call_count, 1)
        self.assertEqual(self.store.usage()["total"], 1)

    def test_failed_refresh_can_reuse_stale_cache_without_changing_time(self):
        response = payload([dict(symbol="002318", close=10)])
        with (
            patch("chengchuan.infrastructure.meoz_client.private_key", return_value=TEST_KEY),
            patch(
                "chengchuan.infrastructure.meoz_client.urlopen",
                return_value=io.BytesIO(json.dumps(response).encode()),
            ),
        ):
            first = self.client.query("daily", dict(symbols="002318"))
        with (
            patch("chengchuan.infrastructure.meoz_client.private_key", return_value=TEST_KEY),
            patch(
                "chengchuan.infrastructure.meoz_client.urlopen",
                side_effect=OSError("network offline"),
            ),
        ):
            later = self.client.query("daily", dict(symbols="002318"), max_age=0)
        self.assertTrue(later.cached)
        self.assertEqual(later.fetched_at, first.fetched_at)
        self.assertIn("更新失败", later.notice)

    def test_atomic_quota_and_global_rate_slots_across_interfaces(self):
        now = time.time()

        def reserve(i):
            return self.store.reserve(str(i), "minute" if i % 2 else "daily", now)

        with ThreadPoolExecutor(max_workers=4) as executor:
            slots = sorted(slot for _, slot in executor.map(reserve, range(4)))
        self.assertTrue(all(b - a >= 0.399 for a, b in zip(slots, slots[1:])))
        self.assertEqual(self.store.usage()["total"], 4)
        limited = MeozStore(self.store.path, interface_limit=2)
        with self.assertRaisesRegex(MarketError, "保护额度"):
            limited.reserve("fifth", "minute", now + 2)
        with self.assertRaisesRegex(MarketError, "正在获取"):
            self.store.reserve("1", "minute", now + 2)

    def test_quota_resets_on_shanghai_midnight(self):
        before = datetime(2026, 11, 4, 23, 59, 59, tzinfo=SHANGHAI)
        self.store.reserve("one", "daily", before.timestamp())
        after = before + timedelta(seconds=2)
        self.store.reserve("two", "daily", after.timestamp())
        self.assertEqual(self.store.usage(before)["total"], 1)
        self.assertEqual(self.store.usage(after)["total"], 1)
        # A rate-limited slot crossing midnight belongs to its scheduled send day.
        other = MeozStore(Path(self.temp.name) / "midnight.sqlite")
        almost_midnight = before.timestamp() + 0.9
        other.reserve("first", "daily", almost_midnight)
        other.reserve("second", "minute", almost_midnight)
        self.assertEqual(other.usage(before)["total"], 1)
        self.assertEqual(other.usage(after)["total"], 1)

    def test_daily_updates_request_only_the_tail_and_preserve_older_history(self):
        old = dict(
            tradedate=DAYS[-2],
            symbol="002318",
            name="测试",
            open=10,
            high=11,
            low=9,
            close=10,
            pre_close=10,
            vol=2,
            amount=2000,
        )
        self.store.save_daily("sz002318", [old], NOW.timestamp() - 86400, DAYS[-2])
        update = dict(old, tradedate=DAYS[-1], close=10.5)
        with (
            patch("chengchuan.infrastructure.meoz_client.private_key", return_value=TEST_KEY),
            patch(
                "chengchuan.infrastructure.meoz_client.urlopen",
                return_value=io.BytesIO(json.dumps(payload([update])).encode()),
            ) as http,
            patch("chengchuan.infrastructure.meoz_market.datetime", wraps=datetime) as clock,
        ):
            clock.now.return_value = NOW
            frame = MeozMarketProvider(self.client).fetch("002318", "day")
        sent = json.loads(http.call_args.args[0].data)
        self.assertEqual(sent["params"]["startdate"], DAYS[-2])
        self.assertEqual(len(frame.snapshot.series.bars), 2)
        self.assertEqual(frame.snapshot.series.bars[0].close, 10)

    def test_reflected_credential_is_never_cached(self):
        reflected = payload([dict(symbol="002318", unexpected=TEST_KEY)])
        with (
            patch("chengchuan.infrastructure.meoz_client.private_key", return_value=TEST_KEY),
            patch(
                "chengchuan.infrastructure.meoz_client.urlopen",
                return_value=io.BytesIO(json.dumps(reflected).encode()),
            ),
        ):
            with self.assertRaisesRegex(MarketError, "敏感认证信息"):
                self.client.query("daily", dict(symbols="002318"))
        with self.store.connect() as con:
            self.assertEqual(con.execute("SELECT COUNT(*) FROM responses").fetchone()[0], 0)
            self.assertNotIn(TEST_KEY, str(con.execute("SELECT * FROM failures").fetchall()))

    def test_opening_bucket_units_real_ohlc_and_duplicate_rejection(self):
        rows = minute_rows(DAYS[-1])
        bars, skipped = parse_rows(rows, "sz002318", intraday=True)
        self.assertEqual(skipped, 0)
        self.assertEqual(bars[0].timestamp.hour, 9)
        self.assertEqual(bars[0].timestamp.minute, 30)
        self.assertEqual(bars[0].volume_shares, 200)
        self.assertEqual(bars[0].amount_yuan, 2000)
        self.assertEqual((bars[0].open, bars[0].high, bars[0].low), (10, 11, 9))
        with self.assertRaisesRegex(MarketError, "重复"):
            parse_rows(rows + [rows[0]], "sz002318", intraday=True)
        with self.assertRaisesRegex(MarketError, "其他证券"):
            parse_rows(rows, "sh600519", intraday=True)

    def test_minute_aggregation_preserves_auction_and_lunch_boundaries(self):
        raw, _ = parse_rows(minute_rows(DAYS[-1]), "sz002318", intraday=True)
        expected = {1: 241, 5: 48, 15: 16, 30: 8, 60: 4}
        for n, count in expected.items():
            bars = minute_bars(raw, n)
            self.assertEqual(len(bars), count)
            self.assertEqual(sum(b.volume_shares for b in bars), sum(b.volume_shares for b in raw))
            self.assertEqual(sum(b.amount_yuan for b in bars), sum(b.amount_yuan for b in raw))
        hours = minute_bars(raw, 60)
        self.assertEqual(
            [b.timestamp.strftime("%H:%M") for b in hours], ["10:30", "11:30", "14:00", "15:00"]
        )
        partial = minute_bars(raw[:-10], 60)
        self.assertEqual(partial[-1].timestamp.strftime("%H:%M"), "14:50")

    def test_calendar_aggregation_uses_iso_weeks_and_preserves_missing_amount(self):
        bars = tuple(
            MarketBar(
                datetime(y, m, d, 15, tzinfo=SHANGHAI),
                11 + i,
                100,
                amount_yuan=None if i == 1 else 1000,
                open=10 + i,
                high=12 + i,
                low=9 + i,
            )
            for i, (y, m, d) in enumerate(((2020, 12, 31), (2021, 1, 1), (2021, 1, 4)))
        )
        weekly = calendar_bars(bars, "week")
        self.assertEqual(len(weekly), 2)
        self.assertEqual(weekly[0].open, 10)
        self.assertEqual(weekly[0].close, 12)
        self.assertIsNone(weekly[0].amount_yuan)
        self.assertEqual(len(calendar_bars(bars, "year")), 2)
        self.assertEqual(len(calendar_bars(bars, "quarter")), 2)

    def test_all_twelve_views_share_seven_http_calls_and_incremental_daily_archive(self):
        daily = [
            dict(
                tradedate=d,
                symbol="002318",
                name="测试",
                open=10,
                high=11,
                low=9,
                close=10,
                pre_close=10,
                vol=482,
                amount=482000,
            )
            for d in DAYS
        ]

        def respond(request, **kwargs):
            body = json.loads(request.data)
            if body["apiname"] == "daily":
                rows = daily
            elif body["apiname"] == "tradecal":
                rows = [dict(cal_date=d, is_open="1") for d in DAYS]
            else:
                rows = minute_rows(body["params"].get("tradedate", DAYS[-1]))
            return io.BytesIO(json.dumps(payload(rows)).encode())

        provider = MeozMarketProvider(self.client)
        with (
            patch("chengchuan.infrastructure.meoz_client.private_key", return_value=TEST_KEY),
            patch("chengchuan.infrastructure.meoz_client.urlopen", side_effect=respond) as http,
            patch("chengchuan.infrastructure.meoz_market.datetime", wraps=datetime) as clock,
        ):
            clock.now.return_value = NOW
            frames = [provider.fetch("002318", p.code) for p in PERIODS]
            count = http.call_count
            for p in PERIODS:
                provider.fetch("002318", p.code)
            self.assertEqual(count, 7)
            self.assertEqual(http.call_count, 7)
            past = provider.fetch_at_date(
                "002318", "minute", datetime.strptime(DAYS[-2], "%Y%m%d").date()
            )
            self.assertEqual(past.as_of.strftime("%Y%m%d"), DAYS[-2])
            self.assertTrue(
                all(b.timestamp.strftime("%Y%m%d") == DAYS[-2] for b in past.snapshot.series.bars)
            )
            weekend = provider.fetch_at_date("002318", "minute", datetime(2026, 10, 31).date())
            self.assertEqual(
                weekend.snapshot.series.bars[-1].timestamp.strftime("%Y%m%d"), "20261030"
            )
            monthly = provider.fetch_at_date("002318", "month", datetime(2026, 11, 3).date())
            self.assertLessEqual(monthly.snapshot.series.bars[-1].timestamp.date(), monthly.as_of)
            self.assertEqual(http.call_count, 7)
        self.assertEqual([f.snapshot.series.interval for f in frames], [p.code for p in PERIODS])
        five = next(f for f in frames if f.snapshot.series.interval == "five_day")
        self.assertEqual(len(five.vwap_points), 1205)
        self.assertEqual(decode_frame(encode_frame(five)), five)
        self.assertEqual(len(self.store.daily_history("sz002318")[0]), 5)


if __name__ == "__main__":
    unittest.main()
