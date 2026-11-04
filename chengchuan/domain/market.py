"""在线行情的证券、报价和取得状态；时间、价格口径与图表快照分开。"""

import re
from dataclasses import dataclass
from datetime import date, datetime
from math import isfinite

from chengchuan.domain.models import SHANGHAI, MarketSnapshot


def stock_symbol(value: str) -> str:
    value = value.strip().lower()
    if re.fullmatch(r"[036]\d{5}", value):
        value = ("sh" if value.startswith("6") else "sz") + value
    if not re.fullmatch(r"(?:sh6\d{5}|sz(?:00[0-3]|30[01])\d{3})", value):
        raise ValueError("请输入沪深A股六位代码，例如002318、600519或sz000001")
    return value


# Index identities are explicit: bare 000001 remains 平安银行, never 上证指数.
INDEX_CATALOG = (
    ("index:sh000001", "上证指数", "上证,沪指,szzs"),
    ("index:sz399001", "深证成指", "深证,深成指,szcz"),
    ("index:sz399006", "创业板指", "创业板,创业,cybz"),
    ("index:sh000300", "沪深300", "沪深,hs300"),
    ("index:sh000688", "科创50", "科创,kc50"),
    ("index:sh000905", "中证500", "中证,zz500"),
)
INDEX_NAMES = {symbol: name for symbol, name, _ in INDEX_CATALOG}


def is_index(symbol):
    return symbol.startswith("index:")


def instrument_symbol(value):
    text = value.strip().lower()
    candidate = text if text.startswith("index:") else "index:" + text
    if candidate in INDEX_NAMES:
        return candidate
    return stock_symbol(text)


def market_code(symbol):
    return symbol.removeprefix("index:")


def index_matches(query):
    query = query.strip().lower()
    return tuple(
        Instrument(symbol, name, aliases)
        for symbol, name, aliases in INDEX_CATALOG
        if query and query in (symbol + name + aliases).lower()
    )


@dataclass(frozen=True)
class Instrument:
    symbol: str
    name: str
    initials: str = ""

    def __post_init__(self):
        if instrument_symbol(self.symbol) != self.symbol or not self.name.strip():
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
    as_of: date | None = None
    reference_closes: tuple[tuple[date, float], ...] = ()

    def __post_init__(self):
        object.__setattr__(self, "sources", tuple(self.sources))
        object.__setattr__(self, "vwap_points", tuple(self.vwap_points))
        object.__setattr__(self, "reference_closes", tuple(self.reference_closes))
        if self.as_of is not None and type(self.as_of) is not date:
            raise ValueError("历史日期必须为交易日期")
        if self.as_of is not None and any(
            b.timestamp.astimezone(SHANGHAI).date() > self.as_of for b in self.snapshot.series.bars
        ):
            raise ValueError("历史图表包含所选日期之后的数据")
        if any(type(d) is not date or not isfinite(v) or v <= 0 for d, v in self.reference_closes):
            raise ValueError("昨收价参考数据无效")
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
