"""Prediction enrichment from the existing disk cache only: no HTTP, keys or polling."""

from dataclasses import asdict, replace

from chengchuan.application.forecast import digest
from chengchuan.domain.models import MarketSeries, MarketSnapshot, SeriesKind
from chengchuan.infrastructure.meoz_client import MeozClient, MeozStore
from chengchuan.infrastructure.meoz_market import MINUTE_FIELDS, parse_rows


class CachedNextDayInputs:
    def __init__(self, store=None):
        self.store = store or MeozStore()

    def context(self, snapshot):
        if snapshot.benchmark is not None:
            return snapshot
        symbol = "index:sh000300"
        cached = self.store.daily_history(symbol)
        if not cached:
            return snapshot
        bars, _ = parse_rows(cached[0], symbol)
        # Never attach later benchmark rows even to the predictor's source snapshot.
        bars = tuple(b for b in bars if b.timestamp <= snapshot.series.bars[-1].timestamp)
        if not bars:
            return snapshot
        return replace(
            snapshot,
            benchmark=MarketSeries(symbol, "沪深300（本地历史缓存）", "day", SeriesKind.OHLC, bars),
            quality_notes=(*snapshot.quality_notes, "预测市场背景：本地沪深300日线；历史重建。"),
        )

    def minutes(self, symbol, days):
        """Use exact known minute request keys; missing sessions remain missing."""
        if not days:
            return None
        client = MeozClient(self.store, offline=True)
        allowed = {d.strftime("%Y%m%d") for d in days}
        candidates = []
        latest = client.peek("minute", dict(symbols=symbol[-6:], period="1m"), MINUTE_FIELDS)
        if latest:
            candidates.append(latest)
        for day in days:
            result = client.peek(
                "minute",
                dict(symbols=symbol[-6:], period="1m", tradedate=day.strftime("%Y%m%d")),
                MINUTE_FIELDS,
                service="history",
            )
            if result:
                candidates.append(result)
        rows = {}
        for result in sorted(candidates, key=lambda r: r.fetched_at):
            if len(result.rows) >= 6000:
                continue  # Truncated responses are not complete minute evidence.
            relevant = [r for r in result.rows if str(r.get("tradedate")) in allowed]
            bars, _ = parse_rows(relevant, symbol, intraday=True)
            for bar in bars:
                rows[bar.timestamp] = bar
        if not rows:
            return None
        series = MarketSeries(
            symbol, symbol[-6:], "min1", SeriesKind.OHLC, tuple(rows[key] for key in sorted(rows))
        )
        return MarketSnapshot(
            digest(asdict(series)),
            series,
            quality_notes=("本地真实分钟OHLC历史重建；仅有查询取得时间，无逐条接收时间。",),
            source="猫头鹰本地分钟缓存",
        )
