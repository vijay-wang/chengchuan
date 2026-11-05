"""In-memory chart previews and lossless period conversions. No disk or network I/O."""

from dataclasses import replace
from datetime import datetime, time

from chengchuan.application.market_payload import market_revision
from chengchuan.application.market_refresh import market_checkpoint
from chengchuan.application.memory_cache import MemoryCache
from chengchuan.application.resampling import calendar_bars, minute_bars
from chengchuan.domain.calendar import CalendarUnavailable
from chengchuan.domain.models import SHANGHAI, SeriesKind
from chengchuan.domain.periods import chart_period


def chart_fresh(frame, market, now=None):
    now = now or datetime.now(SHANGHAI)
    if (
        getattr(market, "automatic_updates", True) is False
        or getattr(market, "offline", False) is True
    ):
        return True
    if (
        frame.as_of is not None
        and frame.as_of < now.date()
        and frame.retrieved_at >= datetime.combine(frame.as_of, time(15, 15), SHANGHAI)
    ):
        return True
    if not 0 <= (now - frame.retrieved_at).total_seconds() < getattr(market, "ttl_seconds", 15):
        return False
    if frame.as_of is None:
        try:
            expected = market_checkpoint(now).cutoff.date()
            if frame.snapshot.series.bars[-1].timestamp.astimezone(SHANGHAI).date() < expected:
                return False
        except CalendarUnavailable:
            pass
    return True


def derive_frame(frame, interval):
    series = frame.snapshot.series
    period = chart_period(interval)
    vwap, baseline = (), frame.baseline_price
    if series.interval == "day" and period.daily and interval != "day":
        bars = calendar_bars(series.bars, interval)
        kind = SeriesKind.OHLC
    elif series.interval == "min1" and period.minutes and series.kind == SeriesKind.OHLC:
        bars = minute_bars(series.bars, period.minutes)
        kind = SeriesKind.OHLC
    elif series.interval == "five_day" and interval == "minute":
        day = series.bars[-1].timestamp.astimezone(SHANGHAI).date()
        bars = tuple(b for b in series.bars if b.timestamp.astimezone(SHANGHAI).date() == day)
        vwap = tuple((t, v) for t, v in frame.vwap_points if t.astimezone(SHANGHAI).date() == day)
        baseline = dict(frame.reference_closes).get(day)
        kind = SeriesKind.SAMPLED_CLOSE
    else:
        return None
    converted = replace(series, interval=interval, kind=kind, bars=bars)
    return replace(
        frame,
        snapshot=replace(
            frame.snapshot,
            series=converted,
            revision=market_revision(converted, vwap, baseline, frame.reference_closes),
        ),
        vwap_points=vwap,
        baseline_price=baseline,
        cached=True,
        notice=frame.notice + "；由已取得同证券、同日期数据本地转换周期。",
    )


class NavigationCache:
    def __init__(self):
        self.frames = MemoryCache(capacity=48, max_weight=120000)
        self.details = MemoryCache(capacity=80)
        self.panels = MemoryCache(capacity=24, max_weight=40000)

    def clear(self):
        self.frames.clear()
        self.details.clear()
        self.panels.clear()

    def remember(self, frame):
        series = frame.snapshot.series
        self.frames.put((series.symbol, series.interval, frame.as_of), frame, len(series.bars))

    def preview(self, symbol, interval, as_of):
        frame = self.frames.get((symbol, interval, as_of))
        if frame is None:
            for source in ("day", "min1", "five_day"):
                base = self.frames.get((symbol, source, as_of))
                if base is not None and len(base.snapshot.series.bars) <= 12000:
                    frame = derive_frame(base, interval)
                    if frame is not None:
                        self.remember(frame)
                        break
        if frame is not None:
            return replace(
                frame, cached=True, notice=frame.notice + "；会话缓存，显示原数据日期与取得时间。"
            )
        return None
