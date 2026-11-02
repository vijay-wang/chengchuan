import json
import tempfile
import unittest
from dataclasses import replace
from datetime import datetime, timedelta, timezone
from pathlib import Path
from unittest.mock import Mock, patch

from chengchuan.application.errors import MarketError, StorageError
from chengchuan.application.market import MarketService
from chengchuan.domain.market import Instrument, MarketFrame, Quote, stock_symbol
from chengchuan.domain.models import SHANGHAI, MarketBar, MarketSeries, MarketSnapshot, SeriesKind
from chengchuan.infrastructure.market_cache import SqliteMarketCache, decode_frame, encode_frame
from chengchuan.infrastructure.market_data import TencentMarketProvider, parse_quote, parse_search


def sample_frame(symbol="sz002318", interval="day", revision="one"):
    minute = interval == "minute"
    bars = tuple(
        MarketBar(
            datetime(2026, 11, 2, 9, 30 + i, tzinfo=SHANGHAI),
            10 + i,
            100,
            open=None if minute else 10 + i,
            high=None if minute else 11 + i,
            low=None if minute else 9 + i,
        )
        for i in range(20)
    )
    series = MarketSeries(
        symbol, "样本股票", interval, SeriesKind.SAMPLED_CLOSE if minute else SeriesKind.OHLC, bars
    )
    quote = Quote(Instrument(symbol, "样本股票"), 29, 28, bars[-1].timestamp)
    return MarketFrame(MarketSnapshot(revision, series), quote, datetime.now(timezone.utc))


class MarketTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.cache = SqliteMarketCache(Path(self.temp.name) / "cache.sqlite")
        self.provider = Mock()
        self.frame = sample_frame()
        self.provider.fetch.return_value = self.frame
        self.service = MarketService(self.provider, self.cache)

    def test_stock_code_normalization_rejects_unsupported_markets_and_urls(self):
        for value, expected in [
            ("002318", "sz002318"),
            ("600519", "sh600519"),
            (" SZ000001 ", "sz000001"),
            ("301001", "sz301001"),
        ]:
            self.assertEqual(stock_symbol(value), expected)
        for value in [
            "hk00700",
            "bj920001",
            "sh000001",
            "sz399001",
            "600519&x=1",
            "茅台",
            "999999",
        ]:
            with self.assertRaises(ValueError):
                stock_symbol(value)

    def test_search_filters_indices_funds_and_decodes_chinese(self):
        raw = r'v_hint="sh~600519~\u8d35\u5dde\u8305\u53f0~gzmt~GP-A^sh~000001~指数~szzs~ZS^jj~000001~基金~a~KJ"'
        self.assertEqual(parse_search(raw), (Instrument("sh600519", "贵州茅台", "gzmt"),))
        self.assertEqual(parse_search('v_hint=""'), ())
        with self.assertRaises(ValueError):
            parse_search("not a valid search response")

    def test_quote_matches_symbol_and_preserves_provider_timestamp(self):
        fields = ["", "久立特材", "002318", "16.42", "15.78"] + [""] * 25 + ["20261102161445"]
        quote = parse_quote('v_sz002318="' + "~".join(fields) + '";', "sz002318")
        self.assertAlmostEqual(quote.change_pct, (16.42 / 15.78 - 1) * 100)
        self.assertEqual(quote.timestamp, datetime(2026, 11, 2, 16, 14, 45, tzinfo=SHANGHAI))
        with self.assertRaises(ValueError):
            parse_quote('v_sh600519="' + "~".join(fields) + '";', "sh600519")

    def test_daily_data_converts_lots_and_leaves_amount_missing(self):
        payload = {"data": {"sz002318": {"day": [["2026-11-02", "10", "11", "12", "9", "123"]]}}}
        with patch(
            "chengchuan.infrastructure.market_data.http_text",
            side_effect=[OSError(), json.dumps(payload)],
        ):
            frame = TencentMarketProvider().fetch("002318", "day")
        bar = frame.snapshot.series.bars[0]
        self.assertEqual(bar.volume_shares, 12300)
        self.assertEqual((bar.open, bar.close, bar.high, bar.low), (10, 11, 12, 9))
        self.assertIsNone(bar.amount_yuan)
        self.assertIsNone(frame.quote)
        self.assertIn("最新报价未取得", frame.notice)

    def test_minute_data_sorts_days_and_uses_cumulative_differences(self):
        payload = {
            "data": {
                "sz002318": {
                    "data": [
                        {"date": "20261102", "data": ["0930 10 2 2000", "0931 11 5 5300"]},
                        {"date": "20261030", "data": ["0930 9 1 900", "0931 10 3 2900"]},
                    ]
                }
            }
        }
        with patch(
            "chengchuan.infrastructure.market_data.http_text",
            side_effect=[OSError(), json.dumps(payload)],
        ):
            frame = TencentMarketProvider().fetch("002318", "minute")
        bars = frame.snapshot.series.bars
        self.assertEqual([b.volume_shares for b in bars], [100, 200, 200, 300])
        self.assertEqual([b.amount_yuan for b in bars], [900, 2000, 2000, 3300])
        self.assertTrue(all(b.open is None for b in bars))
        self.assertLess(bars[1].timestamp, bars[2].timestamp)

    def test_cache_roundtrip_preserves_time_units_and_revision(self):
        self.frame = replace(self.frame, sources=("https://example.com/quote",))
        self.cache.save(self.frame)
        restored = self.cache.load("sz002318", "day")
        self.assertEqual(restored, self.frame)
        self.assertEqual(decode_frame(encode_frame(self.frame)), self.frame)
        self.assertIsNone(self.cache.load("sh600519", "day"))

    def test_invalid_provider_response_is_a_recoverable_market_error(self):
        with patch(
            "chengchuan.infrastructure.market_data.http_text", side_effect=[OSError(), "[]"]
        ):
            with self.assertRaises(MarketError):
                TencentMarketProvider().fetch("002318", "day")

    def test_cache_ttl_and_force_refresh(self):
        self.cache.save(self.frame)
        self.assertTrue(self.service.load("002318", "day").cached)
        self.provider.fetch.assert_not_called()
        self.assertFalse(self.service.load("002318", "day", refresh=True).cached)
        self.provider.fetch.assert_called_once_with("sz002318", "day")
        old = replace(self.frame, retrieved_at=self.frame.retrieved_at - timedelta(days=1))
        self.cache.save(old)
        self.service.load("002318", "day")
        self.assertEqual(self.provider.fetch.call_count, 2)

    def test_network_failure_falls_back_without_changing_data_time(self):
        old = replace(self.frame, retrieved_at=self.frame.retrieved_at - timedelta(days=1))
        self.cache.save(old)
        self.provider.fetch.side_effect = MarketError("断网")
        result = self.service.load("002318", "day", refresh=True)
        self.assertTrue(result.cached)
        self.assertEqual(result.retrieved_at, old.retrieved_at)
        self.assertEqual(result.quote.timestamp, old.quote.timestamp)
        self.assertIn("联网失败", result.notice)
        with self.assertRaises(MarketError):
            self.service.load("600519", "day", refresh=True)

    def test_wrong_security_response_is_rejected_and_never_cached(self):
        self.provider.fetch.return_value = sample_frame("sh600519")
        with self.assertRaisesRegex(MarketError, "其他证券"):
            self.service.load("002318", "day", refresh=True)
        self.assertIsNone(self.cache.load("sz002318", "day"))

    def test_search_uses_local_directory_on_failure_and_keeps_initials(self):
        item = Instrument("sz002318", "久立特材", "jltc")
        self.provider.search.return_value = (item,)
        self.assertFalse(self.service.search("久立").cached)
        self.cache.save(self.frame)
        self.provider.search.side_effect = MarketError("断网")
        result = self.service.search("jltc")
        self.assertTrue(result.cached)
        self.assertEqual(result.items[0].symbol, item.symbol)

    def test_cache_failure_keeps_successful_online_response(self):
        with patch.object(self.cache, "save", side_effect=StorageError("磁盘不可写")):
            result = self.service.load("002318", "day", refresh=True)
        self.assertFalse(result.cached)
        self.assertIn("磁盘不可写", result.notice)


if __name__ == "__main__":
    unittest.main()
