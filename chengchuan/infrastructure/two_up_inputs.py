"""Comparable daily prices from verified local MeoZ records, without implicit HTTP."""

from dataclasses import asdict, replace
from datetime import date, datetime, timezone
from math import isclose, isfinite

from chengchuan.application.forecast import digest
from chengchuan.domain.models import SHANGHAI, MarketSeries, MarketSnapshot, SeriesKind
from chengchuan.domain.two_up import ComparableHistory
from chengchuan.infrastructure.meoz_client import MeozStore
from chengchuan.infrastructure.meoz_market import matches_symbol, parse_rows
from chengchuan.infrastructure.nextday_inputs import CachedNextDayInputs

# Fixed before looking at the new task's outcomes. These familiar listed stocks
# are a convenience research panel, not a representative or survivorship-free universe.
UNIVERSE = (
    ("sz002318", "久立特材"),
    ("sz300750", "宁德时代"),
    ("sh600519", "贵州茅台"),
    ("sz000333", "美的集团"),
    ("sh600036", "招商银行"),
    ("sh601318", "中国平安"),
    ("sh600900", "长江电力"),
    ("sh601899", "紫金矿业"),
    ("sz002475", "立讯精密"),
    ("sz300308", "中际旭创"),
    ("sh600030", "中信证券"),
    ("sz000651", "格力电器"),
)
DEVELOPMENT_SYMBOLS = tuple(symbol for symbol, _ in UNIVERSE[:2])
HOLDOUT_SYMBOLS = tuple(symbol for symbol, _ in UNIVERSE[2:])
SOURCE_START = date(2025, 1, 1)
BASIS = "meoz-daily-pre-close-1.0.52"


def positive_number(value):
    if isinstance(value, bool):
        return None
    try:
        result = float(value)
    except (TypeError, ValueError, OverflowError):
        return None
    return result if isfinite(result) and result > 0 else None


def _raw_matches(row, bar):
    return all(
        (value := positive_number(row.get(name))) is not None
        and isclose(value, getattr(bar, name), rel_tol=1e-10, abs_tol=1e-8)
        for name in ("open", "high", "low", "close")
    )


class CachedTwoUpInputs:
    """Join pre_close only to matching unadjusted candles, never guess missing bases."""

    def __init__(self, store=None):
        self.store = store or MeozStore()

    def history(self, snapshot):
        if snapshot.series.interval != "day" or snapshot.series.kind != SeriesKind.OHLC:
            raise ValueError("连涨两日预测需要真实日K")
        source = self.store.daily_history(snapshot.series.symbol)
        notes, previous = [], []
        observed = None
        if source:
            observed = datetime.fromtimestamp(source[1], timezone.utc)
            rows = {}
            cutoff = snapshot.series.bars[-1].timestamp.astimezone(SHANGHAI).strftime("%Y%m%d")
            for row in source[0]:
                if str(row["tradedate"]) > cutoff:
                    continue
                if not matches_symbol(row.get("symbol", ""), snapshot.series.symbol):
                    raise ValueError("可比昨收缓存包含其他证券，拒绝混用")
                day = datetime.strptime(str(row["tradedate"]), "%Y%m%d").date()
                if day in rows:
                    raise ValueError("可比昨收缓存存在重复日期")
                rows[day] = row
            mismatch, missing = 0, 0
            for bar in snapshot.series.bars:
                day = bar.timestamp.astimezone(SHANGHAI).date()
                row = rows.get(day)
                value = positive_number(row.get("pre_close")) if row else None
                if row and not _raw_matches(row, bar):
                    mismatch += 1
                elif value is None:
                    missing += 1
                else:
                    previous.append((day, value))
            if mismatch:
                notes.append(f"{mismatch}日价格与猫爪原始日线不一致，未混入其可比昨收。")
            if missing:
                notes.append(f"{missing}日缺少有效可比昨收，事件和标签保留未知。")
        else:
            notes.append("本地缺少猫爪可比昨收数据，不以相邻日原始收盘替代。")
        notes.append("涨跌基准来自猫爪日线pre_close可比昨收；历史重建，非当时接收档案。")
        snapshot = CachedNextDayInputs(self.store).context(snapshot)
        snapshot = replace(snapshot, quality_notes=(*snapshot.quality_notes, *notes))
        payload = asdict(snapshot)
        payload.pop("revision")
        snapshot = replace(snapshot, revision=digest(payload))
        return ComparableHistory(snapshot, tuple(previous), observed, BASIS)

    def load(self, symbol):
        """Read one cached stock, including stocks outside the fixed training panel."""
        source = self.store.daily_history(symbol)
        if not source:
            return None
        raw = tuple(r for r in source[0] if str(r["tradedate"]) >= SOURCE_START.strftime("%Y%m%d"))
        bars, skipped = parse_rows(raw, symbol)
        if not bars:
            return None
        names = dict(UNIVERSE)
        name = next(
            (str(r["name"]) for r in reversed(raw) if r.get("name")), names.get(symbol, symbol)
        )
        series = MarketSeries(symbol, name, "day", SeriesKind.OHLC, bars)
        snapshot = MarketSnapshot(
            digest(asdict(series)),
            series,
            quality_notes=((f"原始行有{skipped}条无法形成有效OHLC。",) if skipped else ()),
            source="猫爪本地日线缓存（可比昨收研究）",
        )
        return self.history(snapshot)

    def pool(self):
        return tuple(
            history for symbol, _ in UNIVERSE if (history := self.load(symbol)) is not None
        )
