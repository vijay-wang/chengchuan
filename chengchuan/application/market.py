"""行情用例：有时效的缓存、离线回退和可替换数据源。"""

from dataclasses import replace
from datetime import datetime, timezone
from typing import Protocol

from chengchuan.application.errors import MarketError, StorageError
from chengchuan.domain.market import Instrument, MarketFrame, SearchResult, stock_symbol
from chengchuan.domain.periods import chart_period


class MarketProvider(Protocol):
    def search(self, query: str) -> tuple[Instrument, ...]: ...

    def fetch(self, symbol: str, interval: str) -> MarketFrame: ...


class MarketCache(Protocol):
    def load(self, symbol: str, interval: str) -> MarketFrame | None: ...

    def save(self, frame: MarketFrame) -> None: ...

    def search(self, query: str) -> tuple[Instrument, ...]: ...

    def remember(self, items: tuple[Instrument, ...]) -> None: ...


class MarketService:
    def __init__(self, provider: MarketProvider, cache: MarketCache, ttl_seconds=15):
        self.provider, self.cache, self.ttl_seconds = provider, cache, ttl_seconds
        intervals = getattr(provider, "intervals", None)
        self.intervals = (
            tuple(intervals) if isinstance(intervals, (tuple, list)) else ("day", "minute")
        )

    def search(self, query: str) -> SearchResult:
        query = query.strip()[:40]
        if not query:
            return SearchResult(())
        try:
            items = self.provider.search(query)
        except MarketError as exc:
            return SearchResult(self.cache.search(query), True, str(exc))
        try:
            self.cache.remember(items)
        except StorageError:
            return SearchResult(items, notice="搜索成功，但本地目录未能保存")
        return SearchResult(items)

    def load(self, symbol: str, interval: str, refresh=False) -> MarketFrame:
        symbol = stock_symbol(symbol)
        chart_period(interval)
        if interval not in self.intervals:
            raise ValueError("当前数据源不支持此周期；完整周期需配置猫爪数据")
        cache_notice = ""
        try:
            cached = self.cache.load(symbol, interval)
        except StorageError as exc:
            cached, cache_notice = None, str(exc)
        now = datetime.now(timezone.utc)
        if (
            cached
            and not refresh
            and 0 <= (now - cached.retrieved_at).total_seconds() < self.ttl_seconds
        ):
            return replace(cached, cached=True, notice="最近取得的本地缓存")
        try:
            frame = self.provider.fetch(symbol, interval)
            if frame.snapshot.series.symbol != symbol or frame.snapshot.series.interval != interval:
                raise MarketError("数据源返回了其他证券或周期，已拒绝显示")
        except MarketError as exc:
            if cached:
                return replace(cached, cached=True, notice="联网失败，显示历史缓存：" + str(exc))
            raise
        try:
            self.cache.save(frame)
        except StorageError as exc:
            cache_notice = str(exc)
        return replace(frame, notice="；".join(filter(None, [frame.notice, cache_notice])))
