"""行情用例：有时效的缓存、离线回退和可替换数据源。"""

from dataclasses import replace
from datetime import date, datetime, time, timezone
from typing import Protocol

from chengchuan.application.errors import MarketError, StorageError
from chengchuan.application.settings import SettingsInfo
from chengchuan.domain.market import Instrument, MarketFrame, SearchResult, stock_symbol
from chengchuan.domain.models import SHANGHAI
from chengchuan.domain.periods import chart_period


class MarketProvider(Protocol):
    def search(self, query: str) -> tuple[Instrument, ...]: ...

    def fetch(self, symbol: str, interval: str) -> MarketFrame: ...


class MarketCache(Protocol):
    def load(self, symbol: str, interval: str, as_of: date | None = None) -> MarketFrame | None: ...

    def save(self, frame: MarketFrame) -> None: ...

    def search(self, query: str) -> tuple[Instrument, ...]: ...

    def remember(self, items: tuple[Instrument, ...]) -> None: ...


class MarketService:
    def __init__(
        self,
        provider: MarketProvider,
        cache: MarketCache,
        ttl_seconds=15,
        source_id="",
        source_label="",
        connection_notice="",
        offline=False,
    ):
        self.provider, self.cache, self.ttl_seconds = provider, cache, ttl_seconds
        self.source_id, self.source_label = source_id, source_label
        self.connection_notice = connection_notice
        self.offline = offline
        intervals = getattr(provider, "intervals", None)
        self.intervals = (
            tuple(intervals) if isinstance(intervals, (tuple, list)) else ("day", "minute")
        )
        self.supports_history = getattr(provider, "supports_history", False) is True

    def search(self, query: str) -> SearchResult:
        query = query.strip()[:40]
        if not query:
            return SearchResult(())
        if self.offline:
            return SearchResult(self.cache.search(query), True, "离线模式使用本地已知证券目录")
        try:
            items = self.provider.search(query)
        except MarketError as exc:
            return SearchResult(self.cache.search(query), True, str(exc))
        try:
            self.cache.remember(items)
        except StorageError:
            return SearchResult(items, notice="搜索成功，但本地目录未能保存")
        return SearchResult(items)

    def load(self, symbol: str, interval: str, refresh=False, as_of=None) -> MarketFrame:
        symbol = stock_symbol(symbol)
        chart_period(interval)
        if interval not in self.intervals:
            raise ValueError("当前数据源不支持此周期；完整周期需配置猫爪数据")
        if as_of is not None:
            if type(as_of) is not date or as_of > datetime.now(SHANGHAI).date():
                raise ValueError("请选择今天或更早的日期")
            if not self.supports_history:
                raise MarketError("当前源不支持补取指定日期，请使用已有数据或猫爪数据")
        cache_notice = ""
        try:
            cached = (
                self.cache.load(symbol, interval)
                if as_of is None
                else self.cache.load(symbol, interval, as_of)
            )
        except StorageError as exc:
            cached, cache_notice = None, str(exc)
        now = datetime.now(timezone.utc)
        if self.offline:
            if cached:
                return replace(cached, cached=True, notice="离线模式使用本地缓存")
            raise MarketError("离线模式缺少该股票和周期的本地缓存")
        if (
            cached
            and not refresh
            and (
                as_of is not None
                and as_of < now.astimezone(SHANGHAI).date()
                and cached.retrieved_at >= datetime.combine(as_of, time(15, 15), SHANGHAI)
                or 0 <= (now - cached.retrieved_at).total_seconds() < self.ttl_seconds
            )
        ):
            return replace(
                cached,
                cached=True,
                notice="所选历史日期的本地缓存" if as_of is not None else "最近取得的本地缓存",
            )
        try:
            frame = (
                self.provider.fetch(symbol, interval)
                if as_of is None
                else self.provider.fetch_at_date(symbol, interval, as_of)
            )
            if (
                frame.snapshot.series.symbol != symbol
                or frame.snapshot.series.interval != interval
                or frame.as_of != as_of
            ):
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


class MarketSources(Protocol):
    choices: tuple[tuple[str, str], ...]

    def connect(self, source: str) -> MarketService: ...

    def configure_key(self, value: str) -> None: ...

    def settings_info(self) -> SettingsInfo: ...
