"""日线特征；只读到D的内容，不依赖图表、存储或供应商协议。"""

import hashlib
import json
from dataclasses import asdict, replace
from math import log
from statistics import mean, pstdev
from typing import Protocol

from chengchuan.domain.calendar import CalendarUnavailable, TradingCalendar
from chengchuan.domain.forecast import DailyFeatures
from chengchuan.domain.models import SHANGHAI, MarketSnapshot, SeriesKind


class StageProvider(Protocol):
    version: str

    def scores(self, snapshot: MarketSnapshot, window: int) -> tuple[float | None, ...]: ...


def bar_date(bar):
    return bar.timestamp.astimezone(SHANGHAI).date()


def history_at(snapshot, day):
    bars = tuple(b for b in snapshot.series.bars if bar_date(b) <= day)
    if not bars:
        raise ValueError("指定日期之前没有行情")
    series = replace(snapshot.series, bars=bars)
    flows = tuple(f for f in snapshot.flows if f.timestamp.astimezone(SHANGHAI).date() <= day)
    benchmark = snapshot.benchmark
    if benchmark:
        bm = tuple(b for b in benchmark.bars if bar_date(b) <= day)
        benchmark = replace(benchmark, bars=bm) if bm else None
    payload = [asdict(series), [asdict(f) for f in flows], asdict(benchmark) if benchmark else None]
    revision = hashlib.sha256(json.dumps(payload, default=str, sort_keys=True).encode()).hexdigest()
    return replace(snapshot, revision=revision, series=series, flows=flows, benchmark=benchmark)


class DailyFeatureExtractor:
    def __init__(self, stages=None, calendar=None):
        self.stages = stages
        self.calendar = calendar or TradingCalendar()

    def extract(self, snapshot, day):
        if snapshot.series.interval != "day" or snapshot.series.kind != SeriesKind.OHLC:
            raise ValueError("量价预测需要真实日K，请切换日K或导入日线快照")
        prefix = history_at(snapshot, day)
        rows = {bar_date(b): b for b in prefix.series.bars}
        current = rows.get(day)
        if current is None:
            return DailyFeatures(day, prefix.revision, (), False, ("该交易日没有行情",))
        notes = []
        try:
            previous_dates = self.calendar.previous(day, 60)
            if not self.calendar.is_session(day):
                notes.append("基准日不是已核对的交易日")
        except CalendarUnavailable as exc:
            return DailyFeatures(day, prefix.revision, (), False, (str(exc),))
        previous = [rows.get(d) for d in previous_dates]
        if any(b is None or b.volume_shares <= 0 for b in previous):
            notes.append("此前60个交易日存在历史不足、缺失或零成交量")
        if current.volume_shares <= 0:
            notes.append("基准日没有有效成交量")
        values = []

        def add(name, value):
            values.append((name, value))

        for n in (1, 5, 20):
            prior = previous[-n]
            add(f"price_return_{n}", current.close / prior.close - 1 if prior else None)
        for n in (20, 60):
            sample = previous[-n:]
            valid = all(b is not None for b in sample)
            low = min(b.low for b in sample) if valid else None
            high = max(b.high for b in sample) if valid else None
            add(
                f"price_position_{n}",
                (current.close - low) / (high - low) if valid and high > low else None,
            )
        span = current.high - current.low
        add("price_close_location", (current.close - current.low) / span if span else None)
        add(
            "price_upper_wick",
            (current.high - max(current.open, current.close)) / span if span else None,
        )
        add(
            "price_lower_wick",
            (min(current.open, current.close) - current.low) / span if span else None,
        )
        add("price_range", span / current.close)
        add("price_body", current.close / current.open - 1)
        last = previous[-1]
        add("price_gap", current.open / last.close - 1 if last else None)
        sample = previous[-21:]
        add(
            "price_volatility",
            pstdev(log(b.close / a.close) for a, b in zip(sample, sample[1:]))
            if all(b is not None for b in sample)
            else None,
        )
        rv20 = None
        for n in (1, 2, 5, 20):
            sample = previous[-n:]
            base = (
                mean(b.volume_shares for b in sample)
                if all(b and b.volume_shares > 0 for b in sample)
                else None
            )
            ratio = current.volume_shares / base if base else None
            add(f"volume_rv{n}", log(ratio) if ratio and ratio > 0 else None)
            if n == 20:
                rv20 = log(ratio) if ratio and ratio > 0 else None
        ret = current.close / last.close - 1 if last else None
        add(
            "volume_return_interaction",
            ret * rv20 if ret is not None and rv20 is not None else None,
        )
        add(
            "volume_close_interaction",
            ((current.close - current.low) / span - 0.5) * rv20
            if span and rv20 is not None
            else None,
        )
        stage_version = self.stages.version if self.stages else "none"
        for n in (5, 20):
            scores = self.stages.scores(prefix, n) if self.stages and not notes else (None,) * 4
            for i, score in enumerate(scores):
                add(f"stage_{n}_{i}", score / 100 if score is not None else None)
        if not span:
            notes.append("当日高低价相同，收盘位置和影线缺失")
        # Flat price range is a missing feature, not a reason to discard an otherwise valid day.
        eligible = (
            self.calendar.is_session(day)
            and current.volume_shares > 0
            and all(b is not None and b.volume_shares > 0 for b in previous)
        )
        return DailyFeatures(
            day,
            prefix.revision,
            tuple(values),
            eligible,
            tuple(notes),
            f"daily-price-volume-stage-v1/{stage_version}",
        )
