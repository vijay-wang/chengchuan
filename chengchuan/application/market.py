"""行情用例：有时效的缓存、离线回退和可替换数据源。"""

from dataclasses import replace
from datetime import date, datetime, time, timezone
from typing import Protocol

from chengchuan.application.errors import MarketError, StorageError
from chengchuan.application.market_refresh import cache_confirmation
from chengchuan.application.settings import SettingsInfo
from chengchuan.domain.market import (
    Instrument,
    MarketFrame,
    SearchResult,
    index_matches,
    instrument_symbol,
    stock_symbol,
)
from chengchuan.domain.market_dashboard import MarketOverview, QuoteDetails
from chengchuan.domain.market_views import HeatmapSnapshot, MoneyFlow, funds_symbol
from chengchuan.domain.microstructure import OrderDaySummary
from chengchuan.domain.models import SHANGHAI
from chengchuan.domain.periods import chart_period
from chengchuan.domain.sectors import SectorSnapshot


class MarketProvider(Protocol):
    def search(self, query: str) -> tuple[Instrument, ...]: ...

    def fetch(self, symbol: str, interval: str) -> MarketFrame: ...


class MarketCache(Protocol):
    def load(self, symbol: str, interval: str, as_of: date | None = None) -> MarketFrame | None: ...

    def save(self, frame: MarketFrame) -> None: ...

    def search(self, query: str) -> tuple[Instrument, ...]: ...

    def remember(self, items: tuple[Instrument, ...]) -> None: ...

    def load_dashboard(
        self, kind: str, key: str
    ) -> MarketOverview | QuoteDetails | HeatmapSnapshot | MoneyFlow | SectorSnapshot | None: ...

    def save_dashboard(
        self,
        kind: str,
        key: str,
        value: MarketOverview | QuoteDetails | HeatmapSnapshot | MoneyFlow | SectorSnapshot,
    ) -> None: ...


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
        tape_repository=None,
    ):
        self.provider, self.cache, self.ttl_seconds = provider, cache, ttl_seconds
        self.source_id, self.source_label = source_id, source_label
        self.connection_notice = connection_notice
        self.offline = offline
        self.tape_repository = tape_repository
        self.automatic_updates = not offline and getattr(provider, "offline", False) is not True
        intervals = getattr(provider, "intervals", None)
        self.intervals = (
            tuple(intervals) if isinstance(intervals, (tuple, list)) else ("day", "minute")
        )
        self.supports_history = getattr(provider, "supports_history", False) is True
        self.supports_overview = getattr(provider, "supports_overview", False) is True
        self.supports_details = getattr(provider, "supports_details", False) is True
        self.supports_heatmap = getattr(provider, "supports_heatmap", False) is True
        self.supports_funds = getattr(provider, "supports_funds", False) is True
        self.supports_sector = getattr(provider, "supports_sector", False) is True
        self.supports_microstructure = getattr(provider, "supports_microstructure", False) is True
        self.supports_book_stream = getattr(provider, "supports_book_stream", False) is True

    def search(self, query: str) -> SearchResult:
        query = query.strip()[:40]
        if not query:
            return SearchResult(())
        if self.offline:
            items = {i.symbol: i for i in (*index_matches(query), *self.cache.search(query))}
            return SearchResult(tuple(items.values()), True, "离线模式使用本地已知证券目录")
        try:
            items = self.provider.search(query)
        except MarketError as exc:
            items = {i.symbol: i for i in (*index_matches(query), *self.cache.search(query))}
            return SearchResult(tuple(items.values()), True, str(exc))
        try:
            self.cache.remember(items)
        except StorageError:
            return SearchResult(items, notice="搜索成功，但本地目录未能保存")
        combined = {i.symbol: i for i in (*index_matches(query), *items)}
        return SearchResult(tuple(combined.values()))

    def load(self, symbol: str, interval: str, refresh=False, as_of=None) -> MarketFrame:
        symbol = instrument_symbol(symbol)
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

    def load_overview(self, refresh=False):
        if not self.supports_overview:
            raise MarketError("当前源未提供指数摘要")
        return self._dashboard(
            "overview", "latest", MarketOverview, self.provider.fetch_overview, refresh
        )

    def load_details(self, symbol, as_of=None, refresh=False):
        symbol = instrument_symbol(symbol)
        if as_of is not None and (type(as_of) is not date or as_of > datetime.now(SHANGHAI).date()):
            raise ValueError("请选择今天或更早的日期")
        if not self.supports_details:
            raise MarketError("当前源未提供详细行情指标")
        key = symbol + "@" + (as_of.isoformat() if as_of else "latest")

        def fetch():
            value = self.provider.fetch_details(symbol, as_of)
            if value.symbol != symbol or value.as_of != as_of:
                raise MarketError("指标返回其他证券或日期，已拒绝使用")
            return value

        return self._dashboard("details", key, QuoteDetails, fetch, refresh, as_of)

    def _dashboard(self, kind, key, expected, fetch, refresh, as_of=None):
        cached = None
        try:
            candidate = self.cache.load_dashboard(kind, key)
            if isinstance(candidate, expected):
                if (
                    kind == "funds"
                    and candidate.symbol != key
                    or kind == "heatmap"
                    and candidate.kind != key
                ):
                    raise StorageError("市场视图缓存身份不一致")
                if kind == "sector" and candidate.kind + ":" + candidate.code != key:
                    raise StorageError("板块缓存身份不一致")
                if (
                    kind == "details"
                    and candidate.symbol
                    + "@"
                    + (candidate.as_of.isoformat() if candidate.as_of else "latest")
                    != key
                ):
                    raise StorageError("指标缓存身份不一致")
                cached = candidate
        except StorageError:
            pass
        now = datetime.now(timezone.utc)
        ttl = self.ttl_seconds if cached is None or cached.complete else min(self.ttl_seconds, 15)
        closed = cache_confirmation(now)
        fresh = cached and (
            0 <= (now - cached.retrieved_at).total_seconds() < ttl
            or cached.complete
            and closed is not None
            and closed <= cached.retrieved_at <= now
            or as_of is not None
            and cached.complete
            and as_of < now.astimezone(SHANGHAI).date()
            and cached.retrieved_at >= datetime.combine(as_of, time(15, 15), SHANGHAI)
        )
        if cached and (self.offline or fresh and not refresh):
            return replace(cached, cached=True)
        if self.offline:
            raise MarketError("离线模式缺少本地摘要／指标缓存")
        try:
            value = fetch()
            if not isinstance(value, expected):
                raise MarketError("指标返回类型不匹配")
        except (
            MarketError,
            StorageError,
            OSError,
            ValueError,
            TypeError,
            KeyError,
            OverflowError,
        ) as error:
            if cached:
                return replace(cached, cached=True, notice="刷新失败，保留原指标：" + str(error))
            raise MarketError("摘要／指标暂不可用：" + str(error)) from None
        try:
            self.cache.save_dashboard(kind, key, value)
        except StorageError:
            return replace(value, notice=value.notice + "；指标缓存未能保存")
        return value

    def load_heatmap(self, kind="hy", refresh=False):
        if not self.supports_heatmap or kind not in ("hy", "gn", "stocks"):
            raise MarketError("行业／个股热力图需要猫爪数据源")

        def fetch():
            value = self.provider.fetch_heatmap(kind)
            if value.kind != kind:
                raise MarketError("热力图类型与请求不一致")
            return value

        return self._dashboard("heatmap", kind, HeatmapSnapshot, fetch, refresh)

    def load_funds(self, symbol, refresh=False):
        if not self.supports_funds:
            raise MarketError("当前来源未配置资金分类查询")
        symbol = funds_symbol(symbol)

        def fetch():
            value = self.provider.fetch_funds(symbol)
            if value.symbol != symbol:
                raise MarketError("资金返回其他证券")
            return value

        return self._dashboard("funds", symbol, MoneyFlow, fetch, refresh)

    def load_sector(self, kind, code, name, refresh=False):
        if not self.supports_sector or kind not in ("hy", "gn") or not str(code).isdigit():
            raise MarketError("当前源未提供该板块成份")

        def fetch():
            value = self.provider.fetch_sector(kind, code, name)
            if value.kind != kind or value.code != code:
                raise MarketError("板块成份返回了其他板块")
            return value

        return self._dashboard("sector", kind + ":" + code, SectorSnapshot, fetch, refresh)

    def load_order_book(self, symbol):
        if not self.supports_microstructure:
            raise MarketError("五档及逐笔数据需要猫爪来源")
        return self.provider.fetch_order_book(stock_symbol(symbol))

    def load_microstructure(self, symbol, day, max_pages=6):
        if (
            not self.supports_microstructure
            or type(day) is not date
            or day > datetime.now(SHANGHAI).date()
        ):
            raise MarketError("请选择可查询的个股与已发布交易日")
        return self.provider.fetch_microstructure(stock_symbol(symbol), day, max_pages)

    def stream_order_book(self, symbol, stop):
        if not self.supports_book_stream or not self.automatic_updates:
            raise MarketError("当前来源不支持在线五档订阅")
        return self.provider.stream_order_book(stock_symbol(symbol), stop)

    def load_cached_microstructure(self, symbol, day, max_pages=6):
        fetch = getattr(self.provider, "fetch_cached_microstructure", None)
        if not self.supports_microstructure or not callable(fetch) or type(day) is not date:
            raise MarketError("当前来源没有该日逐笔缓存")
        return fetch(stock_symbol(symbol), day, max_pages)

    def load_chart_order_summary(self, symbol, day, max_pages=8):
        fetch = getattr(self.provider, "fetch_chart_order_summary", None)
        if not self.supports_microstructure or not callable(fetch) or type(day) is not date:
            raise MarketError("当前来源不提供分钟原单统计")
        symbol = stock_symbol(symbol)
        value = fetch(symbol, day, max_pages)
        if not isinstance(value, OrderDaySummary) or (value.symbol, value.day) != (symbol, day):
            raise MarketError("图中逐笔统计返回了其他证券或日期")
        return value

    def load_microstructure_file(self, path):
        if self.tape_repository is None:
            raise MarketError("当前环境未配置逐笔文件导入")
        return self.tape_repository.load(path)


class MarketSources(Protocol):
    choices: tuple[tuple[str, str], ...]

    def connect(self, source: str) -> MarketService: ...

    def configure_key(self, value: str) -> None: ...

    def settings_info(self) -> SettingsInfo: ...
