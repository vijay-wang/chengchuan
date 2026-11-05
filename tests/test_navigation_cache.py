import hashlib
import json
import tempfile
import unittest
from dataclasses import asdict, replace
from datetime import date, datetime, timedelta
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import Mock, patch

from test_market import sample_frame

from chengchuan.application.market import MarketService
from chengchuan.application.market_payload import market_revision, series_payload
from chengchuan.application.memory_cache import MemoryCache
from chengchuan.application.navigation import NavigationCache, chart_fresh, derive_frame
from chengchuan.domain.market import MarketFrame
from chengchuan.domain.models import SHANGHAI, MarketBar, MarketSeries, MarketSnapshot, SeriesKind
from chengchuan.infrastructure.meoz_client import MeozStore


class NavigationCacheTests(unittest.TestCase):
    def test_disk_preview_does_not_fetch_and_rejects_wrong_identity_and_future_scope(self):
        frame = sample_frame()
        provider = Mock(intervals=("day", "week"), supports_history=True)
        cache = Mock()
        cache.load.return_value = frame
        service = MarketService(provider, cache)
        preview = service.cached_frame("002318", "day")
        self.assertTrue(preview.cached)
        self.assertEqual(preview.retrieved_at, frame.retrieved_at)
        provider.fetch.assert_not_called()
        provider.fetch_at_date.assert_not_called()
        self.assertIsNone(service.cached_frame("600519", "day"))
        cache.load.reset_mock()
        self.assertIsNone(
            service.cached_frame("002318", "day", datetime.now(SHANGHAI).date() + timedelta(days=1))
        )
        cache.load.assert_not_called()

    def test_weighted_lru_age_eviction_and_replacing_oversized_entry(self):
        cache = MemoryCache(capacity=2, max_weight=5)
        with patch("chengchuan.application.memory_cache.monotonic", return_value=10):
            cache.put("a", 1, 2)
            cache.put("b", 2, 2)
            self.assertEqual(cache.get("a"), 1)
            cache.put("c", 3, 2)
            self.assertIsNone(cache.get("b"))
            cache.put("a", 9, 10)
            self.assertIsNone(cache.get("a"))
            self.assertEqual(cache.weight, 2)
        with patch("chengchuan.application.memory_cache.monotonic", return_value=20):
            self.assertIsNone(cache.get("c", max_age=5))
            self.assertEqual(cache.weight, 0)

    def test_scalar_payload_and_content_revision_match_legacy_format_exactly(self):
        frame = sample_frame(interval="minute")
        series = frame.snapshot.series
        old = json.dumps(
            [asdict(series), frame.vwap_points, frame.baseline_price, frame.reference_closes],
            default=str,
            sort_keys=True,
        )
        self.assertEqual(series_payload(series), asdict(series))
        self.assertEqual(
            market_revision(
                series, frame.vwap_points, frame.baseline_price, frame.reference_closes
            ),
            hashlib.sha256(old.encode()).hexdigest(),
        )

    def test_daily_conversion_preserves_real_ohlc_amounts_and_incomplete_period(self):
        base = sample_frame()
        bars = tuple(
            MarketBar(datetime(2026, 11, d, 15, tzinfo=SHANGHAI), close, 100, amount, 10, high, low)
            for d, close, high, low, amount in (
                (2, 11, 12, 9, 1000),
                (3, 9, 11, 8, None),
                (4, 12, 13, 9, 1200),
            )
        )
        base = replace(
            base,
            quote=None,
            snapshot=replace(base.snapshot, series=replace(base.snapshot.series, bars=bars)),
        )
        derived = derive_frame(base, "week")
        self.assertEqual(len(derived.snapshot.series.bars), 1)
        bar = derived.snapshot.series.bars[0]
        self.assertEqual(
            (bar.open, bar.high, bar.low, bar.close, bar.volume_shares, bar.amount_yuan),
            (10, 13, 8, 12, 300, None),
        )
        self.assertEqual(bar.timestamp, bars[-1].timestamp)
        self.assertEqual(derived.retrieved_at, base.retrieved_at)

    def test_minute_ohlc_can_aggregate_but_sampled_close_cannot_invent_candles(self):
        base = sample_frame(interval="minute")
        bars = tuple(
            MarketBar(datetime(2026, 11, 4, 9, m, tzinfo=SHANGHAI), close, 100, 1000, 10, high, low)
            for m, close, high, low in ((31, 11, 12, 9), (35, 9, 11, 8), (36, 12, 13, 9))
        )
        raw = replace(
            base,
            quote=None,
            snapshot=replace(
                base.snapshot,
                series=MarketSeries("sz002318", "sample", "min1", SeriesKind.OHLC, bars),
            ),
        )
        five = derive_frame(raw, "min5").snapshot.series.bars
        self.assertEqual(len(five), 2)
        self.assertEqual(
            (five[0].open, five[0].high, five[0].low, five[0].close, five[0].volume_shares),
            (10, 12, 8, 9, 200),
        )
        self.assertIsNone(derive_frame(base, "min5"))
        self.assertIsNone(derive_frame(base, "five_day"))

    def test_intraday_from_five_day_keeps_only_last_day_and_its_vwap_and_previous_close(self):
        bars = tuple(MarketBar(datetime(2026, 11, d, 10, tzinfo=SHANGHAI), 10, 100) for d in (3, 4))
        series = MarketSeries("sz002318", "sample", "five_day", SeriesKind.SAMPLED_CLOSE, bars)
        frame = MarketFrame(
            MarketSnapshot("five", series),
            None,
            datetime.now(SHANGHAI),
            vwap_points=tuple((b.timestamp, 10.1) for b in bars),
            reference_closes=((date(2026, 11, 3), 9), (date(2026, 11, 4), 9.8)),
        )
        result = derive_frame(frame, "minute")
        self.assertEqual(result.snapshot.series.bars, (bars[-1],))
        self.assertEqual(result.vwap_points, ((bars[-1].timestamp, 10.1),))
        self.assertEqual(result.baseline_price, 9.8)

    def test_navigation_identity_date_and_cache_reset_are_isolated(self):
        cache = NavigationCache()
        frame = sample_frame()
        cache.remember(frame)
        self.assertIsNotNone(cache.preview("sz002318", "day", None))
        self.assertIsNone(cache.preview("sh600519", "day", None))
        self.assertIsNone(cache.preview("sz002318", "day", date(2026, 11, 2)))
        cache.details.put("details", 1)
        cache.panels.put("heatmap", 2)
        cache.clear()
        self.assertIsNone(cache.preview("sz002318", "day", None))
        self.assertIsNone(cache.details.get("details"))
        self.assertIsNone(cache.panels.get("heatmap"))

    def test_stale_latest_preview_is_never_considered_current_and_complete_history_is_reusable(
        self,
    ):
        now = datetime(2026, 11, 6, 10, tzinfo=SHANGHAI)
        frame = replace(sample_frame(), retrieved_at=now - timedelta(days=1))
        market = SimpleNamespace(automatic_updates=True, ttl_seconds=180)
        self.assertFalse(chart_fresh(frame, market, now))
        fresh = replace(frame, retrieved_at=now)
        with patch(
            "chengchuan.application.navigation.market_checkpoint",
            return_value=SimpleNamespace(cutoff=now),
        ):
            self.assertFalse(chart_fresh(fresh, market, now))
        historical = replace(frame, as_of=date(2026, 11, 2))
        self.assertTrue(chart_fresh(historical, market, now))
        self.assertTrue(chart_fresh(frame, SimpleNamespace(automatic_updates=False), now))

    def test_initialized_raw_store_reads_do_not_rewrite_schema_or_take_schema_write_locks(self):
        import sqlite3

        with tempfile.TemporaryDirectory() as folder:
            store = MeozStore(Path(folder) / "cache.sqlite")
            store.usage()
            queries = []
            original = sqlite3.connect

            def traced(*args, **kwargs):
                con = original(*args, **kwargs)
                con.set_trace_callback(queries.append)
                return con

            with patch("chengchuan.infrastructure.meoz_client.sqlite3.connect", side_effect=traced):
                store.usage()
            self.assertFalse(any(q.upper().startswith("CREATE") for q in queries))
            self.assertFalse(any("USER_VERSION=" in q.upper() for q in queries))
