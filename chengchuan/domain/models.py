from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime
from enum import Enum
from math import floor, isfinite
from zoneinfo import ZoneInfo

SHANGHAI = ZoneInfo("Asia/Shanghai")


class SeriesKind(str, Enum):
    OHLC = "ohlc"
    SAMPLED_CLOSE = "sampled_close"


@dataclass(frozen=True)
class MarketBar:
    timestamp: datetime
    close: float
    volume_shares: float
    amount_yuan: float | None = None
    open: float | None = None
    high: float | None = None
    low: float | None = None

    def __post_init__(self):
        if self.timestamp.tzinfo is None or self.timestamp.utcoffset() is None:
            raise ValueError("行情时间必须包含时区")
        if not isfinite(self.close) or self.close <= 0:
            raise ValueError("收盘／采样价必须是有限正数")
        if not isfinite(self.volume_shares) or self.volume_shares < 0:
            raise ValueError("成交量必须是有限非负数，单位股")
        if self.amount_yuan is not None and (
            not isfinite(self.amount_yuan) or self.amount_yuan < 0
        ):
            raise ValueError("成交额必须是有限非负数，单位元")
        ohl = (self.open, self.high, self.low)
        if any(x is not None for x in ohl):
            if not all(x is not None and isfinite(x) and x > 0 for x in ohl):
                raise ValueError("OHLC须完整且为有限正数")
            if self.low > min(self.open, self.close) or self.high < max(self.open, self.close):
                raise ValueError("OHLC价格关系错误")


@dataclass(frozen=True)
class OrderSizeFlow:
    timestamp: datetime
    main_yuan: float
    small_yuan: float
    medium_yuan: float
    large_yuan: float
    super_yuan: float

    def __post_init__(self):
        if self.timestamp.tzinfo is None or self.timestamp.utcoffset() is None:
            raise ValueError("资金分类时间必须包含时区")
        values = (
            self.main_yuan,
            self.small_yuan,
            self.medium_yuan,
            self.large_yuan,
            self.super_yuan,
        )
        if not all(isfinite(x) for x in values):
            raise ValueError("资金分类必须是有限数值")
        if abs(self.main_yuan - self.large_yuan - self.super_yuan) > 10:
            raise ValueError("大额订单分类不守恒")
        if abs(self.main_yuan + self.small_yuan + self.medium_yuan) > 10:
            raise ValueError("全部订单分类净额不守恒")


@dataclass(frozen=True)
class MarketSeries:
    symbol: str
    name: str
    interval: str
    kind: SeriesKind
    bars: tuple[MarketBar, ...]

    def __post_init__(self):
        object.__setattr__(self, "bars", tuple(self.bars))
        if not isinstance(self.kind, SeriesKind):
            raise ValueError("行情序列类型须明确指定")
        if not self.bars:
            raise ValueError("行情序列为空")
        if self.interval not in ("day", "minute"):
            raise ValueError("当前仅支持日线及分钟采样")
        times = [b.timestamp for b in self.bars]
        if any(a >= b for a, b in zip(times, times[1:])):
            raise ValueError("行情须按时间严格递增，不允许重复")
        if self.kind == SeriesKind.OHLC and any(b.open is None for b in self.bars):
            raise ValueError("蜡烛图序列必须包含真实OHLC")
        if self.kind == SeriesKind.SAMPLED_CLOSE and any(b.open is not None for b in self.bars):
            raise ValueError("采样价序列不得冒充OHLC")


@dataclass(frozen=True)
class MarketSnapshot:
    revision: str
    series: MarketSeries
    flows: tuple[OrderSizeFlow, ...] = ()
    benchmark: MarketSeries | None = None
    quality_notes: tuple[str, ...] = ()
    source: str = "本地快照"

    def __post_init__(self):
        object.__setattr__(self, "flows", tuple(self.flows))
        object.__setattr__(self, "quality_notes", tuple(self.quality_notes))
        if not self.revision:
            raise ValueError("快照必须有数据版本")


@dataclass(frozen=True)
class IndexRange:
    """交易记录索引，左闭右开；不以自然时间空档估算位置。"""

    start: int
    stop: int

    def __post_init__(self):
        if type(self.start) is not int or type(self.stop) is not int:
            raise ValueError("选区索引必须为整数")
        if self.start < 0 or self.stop <= self.start:
            raise ValueError("选区必须为非空的左闭右开索引范围")

    @classmethod
    def from_drag(cls, a: float, b: float, count: int):
        if count < 1 or not all(isfinite(x) for x in (a, b)):
            raise ValueError("无效拖选坐标")
        left, right = sorted(max(0, min(count - 1, floor(x + 0.5))) for x in (a, b))
        return cls(left, right + 1)

    @property
    def count(self):
        return self.stop - self.start


@dataclass(frozen=True)
class Metric:
    key: str
    label: str
    value: float | None
    unit: str
    missing_text: str = "缺少数据"


@dataclass(frozen=True)
class Finding:
    label: str
    evidence_score: int
    evidence: tuple[str, ...]


@dataclass(frozen=True)
class AnalysisReport:
    snapshot_revision: str
    selection: IndexRange
    summary: str
    metrics: tuple[Metric, ...]
    findings: tuple[Finding, ...]
    notes: tuple[str, ...]
    model_version: str
