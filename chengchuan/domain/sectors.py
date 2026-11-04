"""Current sector membership with independently dated quote statistics."""

from dataclasses import dataclass
from datetime import date, datetime
from math import isfinite

from chengchuan.domain.market import stock_symbol


@dataclass(frozen=True)
class SectorMember:
    symbol: str
    name: str
    price: float | None
    change_pct: float | None
    main_net_yuan: float | None

    def __post_init__(self):
        if stock_symbol(self.symbol) != self.symbol:
            raise ValueError("板块成员证券身份无效")
        if any(
            v is not None and not isfinite(v)
            for v in (self.price, self.change_pct, self.main_net_yuan)
        ):
            raise ValueError("板块成员行情值无效")


@dataclass(frozen=True)
class SectorSnapshot:
    kind: str
    code: str
    name: str
    day: date
    members: tuple[SectorMember, ...]
    retrieved_at: datetime
    membership_at: datetime
    source: str = "猫爪当前成份＋同日实时选股"
    notice: str = ""
    cached: bool = False
    complete: bool = True

    def __post_init__(self):
        object.__setattr__(self, "members", tuple(self.members))
        if self.kind not in ("hy", "gn") or not self.code.isdigit():
            raise ValueError("板块身份无效")
        if len({m.symbol for m in self.members}) != len(self.members):
            raise ValueError("板块成员重复")
        if any(t.utcoffset() is None for t in (self.retrieved_at, self.membership_at)):
            raise ValueError("板块数据时间须含时区")
