"""在线行情的证券、报价和取得状态；时间、价格口径与图表快照分开。"""

import re
from dataclasses import dataclass
from datetime import datetime
from math import isfinite

from chengchuan.domain.models import MarketSnapshot


def stock_symbol(value: str) -> str:
    value = value.strip().lower()
    if re.fullmatch(r"[036]\d{5}", value):
        value = ("sh" if value.startswith("6") else "sz") + value
    if not re.fullmatch(r"(?:sh6\d{5}|sz(?:00[0-3]|30[01])\d{3})", value):
        raise ValueError("请输入沪深A股六位代码，例如002318、600519或sz000001")
    return value


@dataclass(frozen=True)
class Instrument:
    symbol: str
    name: str
    initials: str = ""

    def __post_init__(self):
        if stock_symbol(self.symbol) != self.symbol or not self.name.strip():
            raise ValueError("无效证券信息")


@dataclass(frozen=True)
class Quote:
    instrument: Instrument
    price: float
    previous_close: float
    timestamp: datetime

    def __post_init__(self):
        if any(not isfinite(v) or v <= 0 for v in (self.price, self.previous_close)):
            raise ValueError("报价或昨收无效；证券可能尚无有效行情")
        if self.timestamp.utcoffset() is None:
            raise ValueError("报价时间须包含时区")

    @property
    def change_pct(self):
        return (self.price / self.previous_close - 1) * 100


@dataclass(frozen=True)
class MarketFrame:
    snapshot: MarketSnapshot
    quote: Quote | None
    retrieved_at: datetime
    cached: bool = False
    notice: str = ""
    sources: tuple[str, ...] = ()
    vwap_points: tuple[tuple[datetime, float], ...] = ()
    baseline_price: float | None = None
    provider_name: str = ""

    def __post_init__(self):
        object.__setattr__(self, "sources", tuple(self.sources))
        object.__setattr__(self, "vwap_points", tuple(self.vwap_points))
        if self.baseline_price is not None and (
            not isfinite(self.baseline_price) or self.baseline_price <= 0
        ):
            raise ValueError("图表基准价无效")
        if any(t.utcoffset() is None or not isfinite(v) or v <= 0 for t, v in self.vwap_points):
            raise ValueError("分时均价点无效")
        if self.retrieved_at.utcoffset() is None:
            raise ValueError("行情取得时间须包含时区")
        if self.quote and self.quote.instrument.symbol != self.snapshot.series.symbol:
            raise ValueError("报价与图表的证券不一致")


@dataclass(frozen=True)
class SearchResult:
    items: tuple[Instrument, ...]
    cached: bool = False
    notice: str = ""
