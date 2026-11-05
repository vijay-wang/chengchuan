import tempfile
import unittest
from dataclasses import replace
from datetime import date, datetime, timedelta, timezone
from pathlib import Path
from unittest.mock import Mock

from test_market import sample_frame

from chengchuan.application.chart_history import chart_offset, load_chart_page, merge_chart_frames
from chengchuan.application.chart_tools import ChartInspector, locate_date
from chengchuan.application.errors import MarketError
from chengchuan.application.market import MarketService
from chengchuan.application.startup import startup_target
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
    def test_startup_restores_last_stock_and_period_without_date_or_selection(self):
        state = dict(
            symbol="index:sh000001",
            last_stock_symbol="600519",
            last_stock_interval="min15",
            selection=[1, 4],
            history_date="20261001",
        )
        self.assertEqual(startup_target(state, ("day", "min15")), ("sh600519", "min15"))
        self.assertEqual(startup_target(state, ("day",)), ("sh600519", "day"))
        self.assertEqual(startup_target(dict(symbol="invalid"), ("day",)), ("sz002318", "day"))
        self.assertEqual(
            startup_target(state, ("day",), "index:sh000001", "day"), ("index:sh000001", "day")
        )
        with self.assertRaises(ValueError):
            startup_target(state, ("day",), interval="min15")

    def test_merge_adds_adjacent_bars_and_preserves_latest_quote_and_historical_cutoff(self):
        base = sample_frame(interval="minute")
        source = base.snapshot.series
        older = replace(source.bars[0], timestamp=source.bars[0].timestamp - timedelta(days=2))
        page = replace(
            base,
            snapshot=replace(base.snapshot, series=replace(source, bars=(older,))),
            as_of=older.timestamp.date(),
        )
        merged = merge_chart_frames(base, page, newer=False)
        self.assertEqual(len(merged.snapshot.series.bars), len(source.bars) + 1)
        self.assertEqual(merged.quote, base.quote)
        self.assertIsNone(merged.as_of)
        self.assertEqual(chart_offset(source, merged.snapshot.series), 1)
        self.assertEqual(
            merge_chart_frames(merged, page, newer=False).snapshot.revision,
            merged.snapshot.revision,
        )
        with self.assertRaises(MarketError):
            merge_chart_frames(
                base,
                replace(
                    page,
                    quote=None,
                    snapshot=replace(
                        page.snapshot, series=replace(page.snapshot.series, symbol="sh600519")
                    ),
                ),
            )

    def test_week_merge_replaces_partial_same_period_instead_of_creating_two_candles(self):
        base = sample_frame()
        old_series = series("week", (date(2026, 10, 30), date(2026, 11, 3)))
        new_series = series("week", (date(2026, 10, 30), date(2026, 11, 5)))
        a = replace(
            base,
            quote=None,
            as_of=date(2026, 11, 3),
            snapshot=replace(base.snapshot, series=old_series),
        )
        b = replace(
            base,
            quote=None,
            as_of=date(2026, 11, 5),
            snapshot=replace(base.snapshot, series=new_series),
        )
        merged = merge_chart_frames(a, b)
        self.assertEqual(len(merged.snapshot.series.bars), 2)
        self.assertEqual(merged.snapshot.series.bars[-1].timestamp.date(), date(2026, 11, 5))
        self.assertEqual(merged.as_of, date(2026, 11, 5))

    def test_partial_daily_bar_keeps_its_same_day_intraday_quote_when_history_is_added(self):
        base = sample_frame()
        daily = series("day", (date(2026, 10, 30), date(2026, 11, 2)))
        quote = replace(base.quote, timestamp=datetime(2026, 11, 2, 14, tzinfo=SHANGHAI))
        current = replace(base, snapshot=replace(base.snapshot, series=daily), quote=quote)
        older = series("day", (date(2026, 10, 29),))
        page = replace(
            base,
            snapshot=replace(base.snapshot, series=older),
            quote=None,
            as_of=date(2026, 10, 29),
        )
        self.assertEqual(merge_chart_frames(current, page, newer=False).quote, quote)

    def test_adjacent_minute_page_uses_five_day_cache_without_changing_requested_chart(self):
        base = sample_frame("sz002318", "minute")
        end = base.snapshot.series.bars[0].timestamp.date() - timedelta(days=1)
        bar = replace(
            base.snapshot.series.bars[0],
            timestamp=base.snapshot.series.bars[0].timestamp - timedelta(days=2),
        )
        page = replace(
            base,
            quote=None,
            as_of=end,
            snapshot=replace(
                base.snapshot,
                series=replace(base.snapshot.series, interval="five_day", bars=(bar,)),
            ),
        )
        market = Mock(supports_history=True)
        market.load.return_value = page
        result = load_chart_page(market, base, "older")
        market.load.assert_called_once_with("sz002318", "five_day", as_of=end)
        self.assertEqual(result.snapshot.series.interval, "minute")
        self.assertEqual(result.snapshot.series.bars, (bar,))
        market.load.reset_mock()
        self.assertIsNone(load_chart_page(market, base, "newer"))
        market.load.assert_not_called()

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
