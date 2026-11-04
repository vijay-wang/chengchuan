"""Exchange order records and explicitly covered order-size statistics."""

from dataclasses import dataclass
from datetime import date, datetime

from chengchuan.domain.market import stock_symbol


@dataclass(frozen=True)
class OrderEvent:
    time: datetime
    exchange_id: int
    source_id: int
    kind: str
    side: str
    price_fen: int
    shares: int


@dataclass(frozen=True)
class TradeEvent:
    time: datetime
    trade_id: int
    kind: str
    side: str
    bid_id: int
    ask_id: int
    price_fen: int
    shares: int


@dataclass(frozen=True)
class BookLevel:
    level: int
    bid_price: float | None
    bid_quantity: float | None
    ask_price: float | None
    ask_quantity: float | None


@dataclass(frozen=True)
class BookSnapshot:
    symbol: str
    time: datetime
    levels: tuple[BookLevel, ...]
    source: str
    quantity_unit: str = "股"
    notice: str = ""
    historical: bool = False


@dataclass(frozen=True)
class OrderTape:
    symbol: str
    day: date
    orders: tuple[OrderEvent, ...]
    trades: tuple[TradeEvent, ...]
    quotes: tuple[BookSnapshot, ...]
    orders_complete: bool
    trades_complete: bool
    quotes_complete: bool
    retrieved_at: datetime
    notice: str = ""
    source: str = "猫头鹰Level-2已发布历史"

    def __post_init__(self):
        for key in ("orders", "trades", "quotes"):
            object.__setattr__(self, key, tuple(getattr(self, key)))
        if stock_symbol(self.symbol) != self.symbol:
            raise ValueError("逐笔证券身份无效")
        if sum(len(v) for v in (self.orders, self.trades, self.quotes)) > 200000:
            raise ValueError("逐笔快照超过20万条边界")
        if any(
            type(v) is not bool
            for v in (self.orders_complete, self.trades_complete, self.quotes_complete)
        ):
            raise ValueError("逐笔覆盖标志无效")
        if any(q.symbol != self.symbol for q in self.quotes):
            raise ValueError("逐笔盘口混入其他证券")
        for events in (self.orders, self.trades, self.quotes):
            if any(e.time.utcoffset() is None or e.time.date() != self.day for e in events):
                raise ValueError("逐笔记录混入其他日期或缺少时区")
        if self.retrieved_at.utcoffset() is None:
            raise ValueError("逐笔取得时间须含时区")


@dataclass(frozen=True)
class ReconstructedOrder:
    time: datetime
    exchange_id: int
    side: str
    price_fen: int
    original_shares: int
    executed_shares: int
    executed_fen: int
    trade_count: int
    cancelled: bool
    ambiguous: bool = False
    order_kind: str = ""

    @property
    def original_fen(self):
        return self.price_fen * self.original_shares

    @property
    def remaining_shares(self):
        return (
            0
            if self.cancelled or self.ambiguous
            else max(0, self.original_shares - self.executed_shares)
        )


@dataclass(frozen=True)
class AmountBand:
    label: str
    amount_fen: int
    orders: int


@dataclass(frozen=True)
class PriceVolume:
    price_fen: int
    buy_shares: int
    sell_shares: int
    unknown_shares: int


@dataclass(frozen=True)
class OrderAnalysis:
    symbol: str
    day: date
    cutoff: datetime
    buy: tuple[AmountBand, ...]
    sell: tuple[AmountBand, ...]
    restored: tuple[ReconstructedOrder, ...]
    price_volume: tuple[PriceVolume, ...]
    complete: bool
    cancelled_orders: int
    ambiguous_orders: int
    unknown_events: int
    unmatched_trade_refs: int
    unpriced_orders: int
    notice: str
