"""Bounded adjacent chart pages and deterministic merges, without GUI or vendor imports."""

from dataclasses import replace
from datetime import datetime, timedelta

from chengchuan.application.chart_tools import period_key
from chengchuan.application.errors import MarketError
from chengchuan.application.market_payload import market_revision
from chengchuan.domain.models import SHANGHAI
from chengchuan.domain.periods import chart_period

MAX_CHART_BARS = 50000


def bar_key(bar, interval):
    return (
        period_key(bar.timestamp.astimezone(SHANGHAI).date(), interval)
        if chart_period(interval).daily
        else bar.timestamp
    )


def chart_offset(previous, current):
    """Prepending bars must keep the user's dates, cursor and selection at the same place."""
    key = bar_key(previous.bars[0], previous.interval)
    return next((i for i, b in enumerate(current.bars) if bar_key(b, current.interval) == key), 0)


def merge_chart_frames(current, page, newer=True):
    a, b = current.snapshot.series, page.snapshot.series
    if (a.symbol, a.interval, a.kind) != (b.symbol, b.interval, b.kind):
        raise MarketError("补充行情的证券、周期或价量口径不符")
    priority = (a, b) if newer else (b, a)
    by_key = {bar_key(bar, a.interval): bar for s in priority for bar in s.bars}
    if len(by_key) > MAX_CHART_BARS:
        raise MarketError("已达到50000条图表保护上限，请切换较长周期浏览")
    series = replace(a, bars=tuple(sorted(by_key.values(), key=lambda bar: bar.timestamp)))
    base = page if newer else current
    as_of = max(current.as_of, page.as_of) if current.as_of and page.as_of else current.as_of
    if as_of:
        as_of = max(as_of, series.bars[-1].timestamp.astimezone(SHANGHAI).date())
    ordered = (current, page) if newer else (page, current)
    refs = tuple(sorted({d: v for f in ordered for d, v in f.reference_closes}.items()))
    points = {t: v for f in ordered for t, v in f.vwap_points}
    times = {bar.timestamp for bar in series.bars}
    vwap = tuple(sorted((t, v) for t, v in points.items() if t in times))
    baseline = dict(refs).get(series.bars[0].timestamp.astimezone(SHANGHAI).date())
    if baseline is None:
        baseline = current.baseline_price if series.bars[0] == a.bars[0] else page.baseline_price
    revision = market_revision(series, vwap, baseline, refs)
    flows = {f.timestamp: f for frame in ordered for f in frame.snapshot.flows}
    snapshot = replace(
        base.snapshot,
        revision=revision,
        series=series,
        flows=tuple(flows[t] for t in sorted(flows) if t in times),
        quality_notes=tuple(
            dict.fromkeys((*current.snapshot.quality_notes, *page.snapshot.quality_notes))
        ),
    )
    quote = base.quote
    last_time = series.bars[-1].timestamp
    old_quote = quote and (
        quote.timestamp.astimezone(SHANGHAI).date() < last_time.astimezone(SHANGHAI).date()
        if chart_period(series.interval).daily
        else quote.timestamp < last_time
    )
    if old_quote:
        quote = None
    return replace(
        base,
        snapshot=snapshot,
        as_of=as_of,
        quote=quote,
        reference_closes=refs,
        vwap_points=vwap,
        baseline_price=baseline,
        sources=tuple(dict.fromkeys((*current.sources, *page.sources))),
    )


def load_chart_page(market, frame, direction, today=None):
    """Reuse existing date-scoped caches; one page covers up to five trading sessions."""
    if direction not in ("older", "newer"):
        raise ValueError("无效补数方向")
    if not getattr(market, "supports_history", False):
        raise MarketError("当前数据源不支持相邻日期补数")
    series = frame.snapshot.series
    today = today or datetime.now(SHANGHAI).date()
    first, last = (series.bars[i].timestamp.astimezone(SHANGHAI).date() for i in (0, -1))
    advance = {"day": 30, "week": 14, "month": 62, "quarter": 190, "year": 730}.get(
        series.interval, 5
    )
    end = (
        first - timedelta(days=1)
        if direction == "older"
        else min(last + timedelta(days=advance), today)
    )
    if direction == "newer" and (last >= today or frame.as_of is None):
        return None
    interval = "five_day" if series.interval == "minute" else series.interval
    page = market.load(series.symbol, interval, as_of=end)
    if page.snapshot.series.symbol != series.symbol or page.snapshot.series.interval != interval:
        raise MarketError("相邻日期返回其他证券或周期")
    if page.as_of != end:
        raise MarketError("相邻日期返回了错误的截止日期")
    if interval != series.interval:
        page = replace(
            page,
            snapshot=replace(
                page.snapshot, series=replace(page.snapshot.series, interval=series.interval)
            ),
        )
    return page
