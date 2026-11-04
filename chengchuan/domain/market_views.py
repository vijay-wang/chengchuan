"""Heatmap and order-size money-flow records with explicit source and coverage."""

from dataclasses import dataclass
from datetime import date, datetime
from math import isfinite

from chengchuan.domain.market import instrument_symbol


def funds_symbol(value):
    return value if value == "market:shsz" else instrument_symbol(value)


@dataclass(frozen=True)
class HeatTile:
    code: str
    name: str
    change_pct: float | None
    amount_yuan: float | None
    symbol: str | None = None

    def __post_init__(self):
        if not self.code or not self.name:
            raise ValueError("热力图标的身份缺失")
        for value in (self.change_pct, self.amount_yuan):
            if value is not None and not isfinite(value):
                raise ValueError("热力图字段须为有限数值或缺失")
        if self.amount_yuan is not None and self.amount_yuan < 0:
            raise ValueError("成交额不能为负")
        if self.symbol is not None and instrument_symbol(self.symbol) != self.symbol:
            raise ValueError("热力图证券身份无效")


@dataclass(frozen=True)
class HeatmapSnapshot:
    kind: str
    day: date
    tiles: tuple[HeatTile, ...]
    retrieved_at: datetime
    source: str
    notice: str = ""
    cached: bool = False
    complete: bool = True

    def __post_init__(self):
        object.__setattr__(self, "tiles", tuple(self.tiles))
        if self.kind not in ("hy", "gn", "stocks") or self.retrieved_at.utcoffset() is None:
            raise ValueError("热力图类型或时间无效")
        if len({t.code for t in self.tiles}) != len(self.tiles):
            raise ValueError("热力图标的重复")


@dataclass(frozen=True)
class FlowBucket:
    key: str
    label: str
    inflow: float | None
    outflow: float | None
    net: float | None

    def __post_init__(self):
        for value in (self.inflow, self.outflow, self.net):
            if value is not None and not isfinite(value):
                raise ValueError("资金金额须为有限数值或缺失")
        if any(v is not None and v < 0 for v in (self.inflow, self.outflow)):
            raise ValueError("流入流出展示绝对金额")


@dataclass(frozen=True)
class MoneyFlow:
    symbol: str
    name: str
    day: date
    observed_at: datetime
    retrieved_at: datetime
    buckets: tuple[FlowBucket, ...]
    source: str
    scope: str
    notice: str = ""
    cached: bool = False
    complete: bool = True

    def __post_init__(self):
        object.__setattr__(self, "buckets", tuple(self.buckets))
        if funds_symbol(self.symbol) != self.symbol:
            raise ValueError("资金证券身份无效")
        if any(t.utcoffset() is None for t in (self.observed_at, self.retrieved_at)):
            raise ValueError("资金时间须含时区")
        if len({b.key for b in self.buckets}) != len(self.buckets):
            raise ValueError("资金分组重复")
