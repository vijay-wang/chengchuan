import math
import unittest
from dataclasses import replace
from datetime import date, datetime, time, timedelta, timezone

from chengchuan.application.nextday_features import NextDayFeatureExtractor
from chengchuan.domain.calendar import TradingCalendar
from chengchuan.domain.models import SHANGHAI, MarketBar, MarketSeries, MarketSnapshot, SeriesKind


def daily_fixture(count=145):
    days = TradingCalendar().sessions(date(2026, 3, 2), date(2026, 11, 5))[:count]
    bars = []
    prior = 20.0
    for i, day in enumerate(days):
        close = prior * (1 + 0.004 * math.sin(i * 0.7) + 0.001)
        opening = prior * (1 + 0.002 * math.cos(i * 0.4))
        bars.append(
            MarketBar(
                datetime.combine(day, time(15), SHANGHAI),
                close,
                100000 * (1 + 0.25 * math.sin(i * 0.43)),
                None,
                opening,
                max(opening, close) * 1.004,
                min(opening, close) * 0.996,
            )
        )
        prior = close
    series = MarketSeries("sz002318", "合成特征测试", "day", SeriesKind.OHLC, tuple(bars))
    return MarketSnapshot("synthetic-test-only", series)


def minute_fixture(daily, first=75, last=106):
    bars = []
    for i, daily_bar in enumerate(daily.series.bars[first:last], first):
        day = daily_bar.timestamp.date()
        clocks = [time(9 + (30 + n) // 60, (30 + n) % 60) for n in range(121)]
        clocks += [time(13 + n // 60, n % 60) for n in range(121)]
        for j, clock in enumerate(clocks):
            price = daily_bar.open * (1 + 0.00004 * j)
            bars.append(
                MarketBar(
                    datetime.combine(day, clock, SHANGHAI),
                    price,
                    float(100 + i + j % 10),
                    None,
                    price,
                    price * 1.0001,
                    price * 0.9999,
                )
            )
    series = MarketSeries(
        daily.series.symbol, daily.series.name, "min1", SeriesKind.OHLC, tuple(bars)
    )
    return MarketSnapshot("minute-synthetic-test-only", series)


class NextDayFeatureTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.daily = daily_fixture()
        cls.minutes = minute_fixture(cls.daily)
        cls.day = cls.daily.series.bars[100].timestamp.date()
        cls.extractor = NextDayFeatureExtractor()

    def test_bounded_finite_feature_schema_and_explicit_proxy_evidence(self):
        feature = self.extractor.extract(self.daily, self.day)
        self.assertTrue(feature.eligible)
        self.assertEqual(len(feature.values), 63)
        self.assertEqual(len(dict(feature.values)), 63)
        self.assertTrue(all(v is None or math.isfinite(v) for _, v in feature.values))
        self.assertTrue(any("不是实际持仓" in note for note in feature.notes))
        self.assertTrue(any("支撑压力候选" in item for item in feature.evidence))
        self.assertEqual(feature.cutoff_at.time(), time(15, 15))

    def test_close_prefix_is_immune_to_future_data_and_source_revision(self):
        rows = self.daily.series.bars
        changed = replace(
            self.daily,
            revision="future-changed",
            series=replace(
                self.daily.series,
                bars=rows[:101]
                + tuple(
                    replace(b, close=b.close * 4, open=b.open * 4, high=b.high * 4, low=b.low * 4)
                    for b in rows[101:]
                ),
            ),
        )
        self.assertEqual(
            self.extractor.extract(self.daily, self.day), self.extractor.extract(changed, self.day)
        )

    def test_daily_final_bar_and_late_minutes_cannot_leak_into_1435(self):
        rows = self.daily.series.bars
        changed_daily = replace(
            self.daily,
            revision="different",
            series=replace(
                self.daily.series,
                bars=rows[:100]
                + tuple(
                    replace(
                        b,
                        close=b.close * 5,
                        open=b.open * 5,
                        high=b.high * 5,
                        low=b.low * 5,
                        volume_shares=b.volume_shares * 100,
                    )
                    for b in rows[100:]
                ),
            ),
        )
        cutoff = datetime.combine(self.day, time(14, 35), SHANGHAI)
        changed_minutes = replace(
            self.minutes,
            series=replace(
                self.minutes.series,
                bars=tuple(
                    replace(
                        b,
                        close=b.close * 6,
                        open=b.open * 6,
                        high=b.high * 6,
                        low=b.low * 6,
                        volume_shares=b.volume_shares * 100,
                    )
                    if b.timestamp >= cutoff
                    else b
                    for b in self.minutes.series.bars
                ),
            ),
        )
        original = self.extractor.extract(self.daily, self.day, "cut1435", self.minutes)
        changed = self.extractor.extract(changed_daily, self.day, "cut1435", changed_minutes)
        self.assertTrue(original.eligible, original.reasons)
        self.assertEqual(original, changed)
        self.assertEqual(original.cutoff_at, cutoff)
        self.assertEqual(datetime.fromisoformat(original.coverage_at).time(), time(14, 34))
        self.assertTrue(any("缺少逐条接收时间" in n for n in original.notes))

    def test_same_clock_volume_ratio_uses_prior_partial_volume(self):
        feature = self.extractor.extract(self.daily, self.day, "cut1435", self.minutes)
        previous_day = TradingCalendar().shift(self.day, -1)

        def volume(day):
            return sum(
                b.volume_shares
                for b in self.minutes.series.bars
                if b.timestamp.date() == day and b.timestamp.time() < time(14, 35)
            )

        ratio = math.exp(dict(feature.values)["volume_rv1"])
        self.assertAlmostEqual(ratio, volume(self.day) / volume(previous_day))
        daily_volume = self.daily.series.bars[99].volume_shares
        self.assertNotAlmostEqual(ratio, volume(self.day) / daily_volume)

    def test_missing_minutes_rejects_instead_of_using_final_day(self):
        absent = self.extractor.extract(self.daily, self.day, "cut1435")
        self.assertFalse(absent.eligible)
        stamp = datetime.combine(self.day, time(13, 10), SHANGHAI)
        changed = replace(
            self.minutes,
            series=replace(
                self.minutes.series,
                bars=tuple(b for b in self.minutes.series.bars if b.timestamp != stamp),
            ),
        )
        feature = self.extractor.extract(self.daily, self.day, "cut1435", changed)
        self.assertFalse(feature.eligible)
        self.assertTrue(any("不完整" in item for item in feature.reasons))

    def test_missing_previous_same_clock_days_is_explicit(self):
        changed = replace(
            self.minutes,
            series=replace(
                self.minutes.series,
                bars=tuple(b for b in self.minutes.series.bars if b.timestamp.date() >= self.day),
            ),
        )
        feature = self.extractor.extract(self.daily, self.day, "cut1435", changed)
        self.assertFalse(feature.eligible)
        self.assertTrue(any("同钟" in reason for reason in feature.reasons))

    def test_minute_completeness_uses_local_hour_and_local_minute(self):
        shifted = replace(
            self.minutes,
            series=replace(
                self.minutes.series,
                bars=tuple(
                    replace(
                        b,
                        timestamp=b.timestamp.astimezone(timezone(timedelta(hours=5, minutes=30))),
                    )
                    for b in self.minutes.series.bars
                ),
            ),
        )
        original = self.extractor.extract(self.daily, self.day, "cut1435", self.minutes)
        translated = self.extractor.extract(self.daily, self.day, "cut1435", shifted)
        self.assertTrue(translated.eligible, translated.reasons)
        self.assertEqual(original.values, translated.values)
        self.assertEqual(original.levels, translated.levels)

    def test_minute_sampling_and_symbol_mismatch_are_rejected(self):
        sampled = replace(
            self.minutes,
            series=replace(
                self.minutes.series,
                interval="minute",
                kind=SeriesKind.SAMPLED_CLOSE,
                bars=tuple(
                    replace(b, open=None, high=None, low=None) for b in self.minutes.series.bars
                ),
            ),
        )
        with self.assertRaises(ValueError):
            self.extractor.extract(self.daily, self.day, "cut1435", sampled)
        wrong = replace(self.minutes, series=replace(self.minutes.series, symbol="sz300750"))
        with self.assertRaises(ValueError):
            self.extractor.extract(self.daily, self.day, "cut1435", wrong)

    def test_missing_daily_history_and_duplicate_session_rejected(self):
        rows = self.daily.series.bars
        changed = replace(
            self.daily, series=replace(self.daily.series, bars=rows[:99] + rows[100:])
        )
        self.assertFalse(self.extractor.extract(changed, self.day).eligible)
        extra = replace(rows[99], timestamp=rows[99].timestamp + timedelta(minutes=1))
        duplicate = replace(
            self.daily, series=replace(self.daily.series, bars=rows[:100] + (extra,) + rows[100:])
        )
        with self.assertRaises(ValueError):
            self.extractor.extract(duplicate, self.day)

    def test_flat_series_keeps_finite_or_missing_features(self):
        flat = replace(
            self.daily,
            series=replace(
                self.daily.series,
                bars=tuple(
                    replace(b, open=20, high=20, low=20, close=20) for b in self.daily.series.bars
                ),
            ),
        )
        feature = self.extractor.extract(flat, self.day)
        self.assertTrue(feature.eligible)
        self.assertIsNone(dict(feature.values)["price_rsi14_simple"])
        self.assertIsNone(dict(feature.values)["price_close_location"])
        self.assertEqual(dict(feature.values)["chip_cost_distance"], 0)

    def test_market_alignment_and_1435_lag(self):
        benchmark = replace(self.daily.series, symbol="index:sh000001")
        daily = replace(self.daily, benchmark=benchmark)
        close = self.extractor.extract(daily, self.day)
        self.assertAlmostEqual(dict(close.values)["market_correlation20"], 1)
        self.assertAlmostEqual(dict(close.values)["market_relative5"], 0)
        rows = benchmark.bars
        changed = replace(
            daily,
            benchmark=replace(
                benchmark,
                bars=rows[:100]
                + tuple(
                    replace(b, open=b.open * 9, high=b.high * 9, low=b.low * 9, close=b.close * 9)
                    for b in rows[100:]
                ),
            ),
        )
        self.assertEqual(
            self.extractor.extract(daily, self.day, "cut1435", self.minutes),
            self.extractor.extract(changed, self.day, "cut1435", self.minutes),
        )


if __name__ == "__main__":
    unittest.main()
