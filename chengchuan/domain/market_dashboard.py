"""Quote metrics and market-wide summaries, separate from chart periods and models."""

from __future__ import annotations

from dataclasses import dataclass
from datetime import date, datetime
from math import isfinite

from chengchuan.domain.market import INDEX_NAMES, instrument_symbol


@dataclass(frozen=True)
class QuoteMetric:
    key: str
    label: str
    value: float | None
    unit: str = ""
    source: str = ""
    note: str = ""

    def __post_init__(self):
        if self.value is not None and not isfinite(self.value):
            raise ValueError("行情指标必须为有限数值或缺失")


@dataclass(frozen=True)
class QuoteDetails:
    symbol: str
    day: date
    metrics: tuple[QuoteMetric, ...]
    retrieved_at: datetime
    as_of: date | None = None
    cached: bool = False
    notice: str = ""
    breadth: tuple[MarketBreadth, ...] = ()
    complete: bool = True

    def __post_init__(self):
        if instrument_symbol(self.symbol) != self.symbol:
            raise ValueError("行情指标证券身份无效")
        object.__setattr__(self, "metrics", tuple(self.metrics))
        object.__setattr__(self, "breadth", tuple(self.breadth))
        if any(b.day != self.day for b in self.breadth):
            raise ValueError("市场家数与指标日期不一致")
        if len({m.key for m in self.metrics}) != len(self.metrics):
            raise ValueError("行情指标键重复")
        if self.retrieved_at.utcoffset() is None:
            raise ValueError("指标取得时间须含时区")
        if self.as_of is not None and self.day > self.as_of:
            raise ValueError("历史指标来自所选日期之后")


@dataclass(frozen=True)
class IndexSummary:
    symbol: str
    name: str
    day: date
    price: float | None
    previous_close: float | None
    amount_yuan: float | None = None
    observed_at: datetime | None = None

    def __post_init__(self):
        if self.symbol not in INDEX_NAMES:
            raise ValueError("未知指数")
        for value in (self.price, self.previous_close):
            if value is not None and (not isfinite(value) or value <= 0):
                raise ValueError("指数点位无效")
        if self.amount_yuan is not None and (
            not isfinite(self.amount_yuan) or self.amount_yuan < 0
        ):
            raise ValueError("指数成交额无效")
        if self.observed_at is not None and self.observed_at.utcoffset() is None:
            raise ValueError("指数行情时间须含时区")

    @property
    def change_pct(self):
        if self.price is None or self.previous_close is None:
            return None
        return (self.price / self.previous_close - 1) * 100


@dataclass(frozen=True)
class MarketBreadth:
    scope: str
    label: str
    day: date
    up: int
    down: int
    flat: int
    unknown: int
    total: int
    main_net_yuan: float | None
    flow_covered: int | None

    def __post_init__(self):
        if any(
            type(v) is not int or v < 0
            for v in (self.up, self.down, self.flat, self.unknown, self.total)
        ):
            raise ValueError("市场家数无效")
        if self.flow_covered is not None and (
            type(self.flow_covered) is not int or not 0 <= self.flow_covered <= self.total
        ):
            raise ValueError("资金覆盖家数无效")
        if self.up + self.down + self.flat + self.unknown != self.total:
            raise ValueError("市场统计覆盖不一致")
        if self.main_net_yuan is not None and not isfinite(self.main_net_yuan):
            raise ValueError("资金净额无效")


@dataclass(frozen=True)
class MarketOverview:
    indices: tuple[IndexSummary, ...]
    breadth: tuple[MarketBreadth, ...]
    retrieved_at: datetime
    cached: bool = False
    notice: str = ""
    complete: bool = True

    def __post_init__(self):
        object.__setattr__(self, "indices", tuple(self.indices))
        object.__setattr__(self, "breadth", tuple(self.breadth))
        if self.retrieved_at.utcoffset() is None:
            raise ValueError("摘要取得时间须含时区")
        if len({i.symbol for i in self.indices}) != len(self.indices):
            raise ValueError("摘要指数重复")
