import tempfile
import unittest
from dataclasses import replace
from datetime import date, datetime, timedelta, timezone
from pathlib import Path
from unittest.mock import Mock

from test_market import sample_frame

from chengchuan.application.chart_tools import ChartInspector, locate_date
from chengchuan.application.errors import MarketError
from chengchuan.application.market import MarketService
from chengchuan.domain.models import SHANGHAI, MarketBar, MarketSeries, SeriesKind
from chengchuan.infrastructure.market_cache import SqliteMarketCache, decode_frame, encode_frame


def series(interval="day", days=None):
    days = days or (date(2026, 10, 29), date(2026, 10, 30), date(2026, 11, 2), date(2026, 11, 3))
    rows = tuple(
        MarketBar(
            datetime(d.year, d.month, d.day, 15, tzinfo=SHANGHAI),
            10 + i,
            100,
            1000,
            10 + i,
            11 + i,
            9 + i,
        )
        for i, d in enumerate(days)
    )
    return MarketSeries("sz002318", "测试", interval, SeriesKind.OHLC, rows)


class ChartToolsTests(unittest.TestCase):
    def test_weekend_locates_previous_record_without_inventing_a_trading_day(self):
        found = locate_date(series(), date(2026, 10, 31))
        self.assertEqual(found.actual, date(2026, 10, 30))
        self.assertEqual(found.index, 1)
        self.assertIn("无记录", found.caption)
        with self.assertRaises(ValueError):
            locate_date(series(), date(2000, 1, 1))

    def test_week_and_month_use_containing_period_not_preceding_candle(self):
        weekly = series("week", (date(2026, 10, 23), date(2026, 10, 30), date(2026, 11, 6)))
        self.assertEqual(locate_date(weekly, date(2026, 10, 27)).index, 1)
        monthly = series("month", (date(2026, 9, 30), date(2026, 10, 30), date(2026, 11, 3)))
        self.assertEqual(locate_date(monthly, date(2026, 10, 16)).index, 1)

    def test_vendor_previous_close_is_used_across_corporate_action_gaps(self):
        original = series()
        rows = (
            replace(original.bars[0], close=100, open=100, high=101, low=99),
            replace(original.bars[1], close=51, open=50, high=52, low=49),
        )
        s = replace(original, bars=rows)
        inspected = ChartInspector(s, ((date(2026, 10, 30), 50),)).inspect(1)
        self.assertAlmostEqual(inspected.change_pct, 2)
        self.assertIsNone(ChartInspector(s).inspect(0).change_pct)

    def test_intraday_references_reset_per_day(self):
        s = series("min1")
        inspector = ChartInspector(s, ((date(2026, 10, 29), 10), (date(2026, 10, 30), 10.5)))
        self.assertEqual(inspector.inspect(0).change_pct, 0)
        self.assertAlmostEqual(inspector.inspect(1).change_pct, (11 / 10.5 - 1) * 100)

    def test_incomplete_previous_session_does_not_reuse_an_older_close(self):
        s = series("min1")
        rows = (
            s.bars[0],
            replace(s.bars[1], timestamp=s.bars[1].timestamp.replace(hour=14)),
            s.bars[2],
        )
        inspector = ChartInspector(replace(s, bars=rows))
        self.assertAlmostEqual(inspector.inspect(1).change_pct, 10)
        self.assertIsNone(inspector.inspect(2).change_pct)

    def test_historical_frames_and_reference_prices_are_isolated_in_cache(self):
        with tempfile.TemporaryDirectory() as folder:
            cache = SqliteMarketCache(Path(folder) / "market.sqlite")
            latest = sample_frame()
            historical = replace(
                latest,
                as_of=date(2026, 11, 2),
                snapshot=replace(latest.snapshot, revision="past"),
                reference_closes=((date(2026, 11, 2), 9.5),),
            )
            cache.save(latest)
            cache.save(historical)
            self.assertEqual(cache.load("sz002318", "day"), latest)
            self.assertEqual(cache.load("sz002318", "day", historical.as_of), historical)
            self.assertEqual(decode_frame(encode_frame(historical)), historical)
            with self.assertRaisesRegex(ValueError, "之后"):
                replace(latest, as_of=date(2026, 11, 1))

    def test_history_request_rejects_wrong_scope_and_future_before_network(self):
        frame = sample_frame()
        provider = Mock(intervals=("day",), supports_history=True)
        provider.fetch_at_date.return_value = replace(frame, as_of=date(2026, 11, 3))
        cache = Mock()
        cache.load.return_value = None
        service = MarketService(provider, cache)
        with self.assertRaises(MarketError):
            service.load("002318", "day", as_of=date(2026, 11, 2))
        cache.save.assert_not_called()
        with self.assertRaises(ValueError):
            service.load("002318", "day", as_of=datetime.now(SHANGHAI).date() + timedelta(days=1))
        self.assertEqual(provider.fetch_at_date.call_count, 1)

    def test_past_date_cache_does_not_expire_into_repeated_paid_calls(self):
        frame = replace(
            sample_frame(),
            as_of=date(2026, 11, 2),
            retrieved_at=datetime(2026, 11, 2, 16, tzinfo=timezone.utc),
        )
        provider = Mock(intervals=("day",), supports_history=True)
        cache = Mock()
        cache.load.return_value = frame
        result = MarketService(provider, cache).load("002318", "day", as_of=frame.as_of)
        self.assertTrue(result.cached)
        provider.fetch_at_date.assert_not_called()

    def test_intraday_cached_date_is_not_frozen_as_a_complete_past_session(self):
        frame = replace(
            sample_frame(),
            as_of=date(2026, 11, 2),
            retrieved_at=datetime(2026, 11, 2, 6, tzinfo=timezone.utc),
        )
        provider = Mock(intervals=("day",), supports_history=True)
        provider.fetch_at_date.return_value = replace(
            frame, retrieved_at=datetime.now(timezone.utc)
        )
        cache = Mock()
        cache.load.return_value = frame
        MarketService(provider, cache).load("002318", "day", as_of=frame.as_of)
        provider.fetch_at_date.assert_called_once_with("sz002318", "day", frame.as_of)


if __name__ == "__main__":
    unittest.main()
