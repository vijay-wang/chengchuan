"""Timestamp-anchored chart annotations, independent of plotting and storage."""

from dataclasses import dataclass
from datetime import datetime
from math import isfinite

from chengchuan.domain.periods import PERIOD_BY_CODE

TOOLS = {"horizontal": 1, "vertical": 1, "trend": 2, "rectangle": 2, "price_range": 2}


@dataclass(frozen=True)
class DrawingPoint:
    timestamp: datetime
    price: float

    def __post_init__(self):
        if self.timestamp.utcoffset() is None:
            raise ValueError("画线时间需要时区")
        if isinstance(self.price, bool) or not isfinite(self.price) or self.price <= 0:
            raise ValueError("画线价格必须为有限正数")


@dataclass(frozen=True)
class Drawing:
    id: str
    symbol: str
    interval: str
    tool: str
    points: tuple[DrawingPoint, ...]

    def __post_init__(self):
        object.__setattr__(self, "points", tuple(self.points))
        if not self.id or len(self.id) > 80 or not self.symbol or len(self.symbol) > 80:
            raise ValueError("画线标识或证券无效")
        if self.interval not in PERIOD_BY_CODE or self.tool not in TOOLS:
            raise ValueError("不支持的画线周期或工具")
        if len(self.points) != TOOLS[self.tool]:
            raise ValueError("画线锚点数量不匹配")

    @property
    def context(self):
        return self.symbol, self.interval


def price_difference(first, second):
    """Signed target-minus-base difference; the first price remains the denominator."""
    for value in (first, second):
        if isinstance(value, bool) or not isfinite(value) or value <= 0:
            raise ValueError("测量价格必须为有限正数")
    delta = second - first
    return delta, delta / first * 100
