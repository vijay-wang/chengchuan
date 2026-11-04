import json
import unittest
from dataclasses import replace
from datetime import datetime, timedelta
from unittest.mock import Mock

from test_market import sample_frame

from chengchuan.application.market import MarketService
from chengchuan.application.market_refresh import (
    MarketRefreshController,
    cache_confirmation,
    frame_current,
    market_checkpoint,
)
from chengchuan.domain.market import INDEX_CATALOG
from chengchuan.domain.market_dashboard import IndexSummary, MarketOverview, QuoteDetails
from chengchuan.domain.models import SHANGHAI
from chengchuan.infrastructure.dashboard_codec import decode_dashboard, encode_dashboard

NOW = datetime(2026, 11, 4, 20, tzinfo=SHANGHAI)


def closed_frame(interval="day", day=None):
    stamp = (day or NOW).replace(hour=15, minute=0)
    frame = sample_frame(interval=interval)
    bars = tuple(
        replace(b, timestamp=stamp - timedelta(minutes=19 - i))
        for i, b in enumerate(frame.snapshot.series.bars)
    )
    return replace(
        frame,
        snapshot=replace(frame.snapshot, series=replace(frame.snapshot.series, bars=bars)),
        quote=replace(frame.quote, timestamp=stamp),
        retrieved_at=stamp.replace(hour=16),
    )


class RefreshPolicyTests(unittest.TestCase):
    def test_auctions_lunch_and_after_hours_use_explicit_checkpoints(self):
        for h, m in ((9, 15), (9, 25), (10, 0), (13, 0), (14, 57), (15, 20)):
            self.assertTrue(market_checkpoint(NOW.replace(hour=h, minute=m)).live)
        lunch = market_checkpoint(NOW.replace(hour=12))
        self.assertFalse(lunch.live)
        self.assertEqual((lunch.cutoff.hour, lunch.cutoff.minute), (11, 30))
        self.assertEqual(lunch.confirm_after.minute, 33)
        close = market_checkpoint(NOW.replace(hour=15, minute=30))
        self.assertFalse(close.live)
        self.assertEqual(close.confirm_after, NOW.replace(hour=15, minute=45))
        self.assertIsNone(cache_confirmation(NOW.replace(hour=15, minute=40)))

    def test_preopen_weekends_and_holidays_target_previous_session(self):
        self.assertEqual(market_checkpoint(NOW.replace(hour=8)).cutoff.day, 3)
        saturday = datetime(2026, 11, 7, 12, tzinfo=SHANGHAI)
        self.assertEqual(market_checkpoint(saturday).cutoff.day, 6)
        holiday = datetime(2026, 10, 4, 12, tzinfo=SHANGHAI)
        self.assertEqual(market_checkpoint(holiday).cutoff.date().isoformat(), "2026-09-30")
        working_saturday = datetime(2026, 9, 26, 10, tzinfo=SHANGHAI)
        self.assertFalse(market_checkpoint(working_saturday).live)

    def test_daily_nominal_close_and_recently_fetched_old_day_are_not_fresh(self):
        point = market_checkpoint(NOW)
        frame = closed_frame()
        self.assertTrue(frame_current(frame, point, NOW))
        self.assertFalse(
            frame_current(replace(frame, retrieved_at=NOW.replace(hour=14)), point, NOW)
        )
        self.assertFalse(frame_current(closed_frame(day=NOW - timedelta(days=1)), point, NOW))
        self.assertFalse(
            frame_current(
                replace(frame, quote=replace(frame.quote, timestamp=NOW.replace(hour=12))),
                point,
                NOW,
            )
        )

    def test_minutes_must_reach_session_end_even_if_downloaded_later(self):
        frame = closed_frame("minute")
        point = market_checkpoint(NOW)
        self.assertTrue(frame_current(frame, point, NOW))
        bars = frame.snapshot.series.bars[:-1]
        partial = replace(
            frame,
            snapshot=replace(frame.snapshot, series=replace(frame.snapshot.series, bars=bars)),
        )
        self.assertFalse(frame_current(partial, point, NOW))

    def test_chart_details_and_overview_are_checked_independently(self):
        controller = MarketRefreshController()
        frame = closed_frame()
        details = QuoteDetails(frame.snapshot.series.symbol, NOW.date(), (), NOW)
        indices = tuple(
            IndexSummary(s, n, NOW.date(), 100, 99, observed_at=NOW.replace(hour=15))
            for s, n, _ in INDEX_CATALOG
        )
        overview = MarketOverview(indices, (), NOW)
        args = dict(want_details=True, want_overview=True)
        plan = controller.plan(NOW, "stock", frame, details, overview, **args)
        self.assertFalse(any((plan.chart, plan.details, plan.overview)))
        plan = controller.plan(
            NOW, "stock", frame, replace(details, complete=False), overview, **args
        )
        self.assertFalse(plan.chart)
        self.assertTrue(plan.details)
        self.assertFalse(plan.overview)
        controller.reset()
        plan = controller.plan(
            NOW, "stock", frame, details, replace(overview, indices=indices[:-1]), **args
        )
        self.assertTrue(plan.overview)
        self.assertFalse(plan.chart)

    def test_closed_retries_back_off_stop_after_four_and_manual_reset_recovers(self):
        controller = MarketRefreshController()
        self.assertTrue(controller.plan(NOW, "stock", None).chart)
        self.assertFalse(controller.plan(NOW + timedelta(seconds=15), "stock", None).chart)
        for seconds in (180, 480, 1380):
            self.assertTrue(controller.plan(NOW + timedelta(seconds=seconds), "stock", None).chart)
        self.assertFalse(controller.plan(NOW + timedelta(hours=2), "stock", None).chart)
        controller.reset()
        self.assertTrue(controller.plan(NOW + timedelta(hours=2), "stock", None).chart)

    def test_live_interval_does_not_consume_closed_retry_budget(self):
        controller = MarketRefreshController()
        for i in range(20):
            self.assertTrue(
                controller.plan(
                    NOW.replace(hour=10) + timedelta(seconds=i * 15), "stock", None
                ).chart
            )
        self.assertTrue(controller.plan(NOW, "stock", None).chart)

    def test_confirmation_delay_unknown_year_and_naive_time_do_not_guess(self):
        controller = MarketRefreshController()
        self.assertFalse(controller.plan(NOW.replace(hour=15, minute=40), "stock", None).chart)
        plan = controller.plan(NOW.replace(year=2027), "stock", None)
        self.assertFalse(plan.chart)
        self.assertIn("日历", plan.reason)
        with self.assertRaises(ValueError):
            controller.plan(NOW.replace(tzinfo=None), "stock", None)

    def test_old_dashboard_semantics_require_reconfirmation_and_offline_has_no_auto_query(self):
        details = QuoteDetails("sz002318", NOW.date(), (), NOW)
        old = json.loads(encode_dashboard(details))
        old["version"] = 1
        self.assertFalse(decode_dashboard(json.dumps(old)).complete)
        provider = Mock(offline=True, intervals=("day",))
        self.assertFalse(MarketService(provider, Mock()).automatic_updates)
