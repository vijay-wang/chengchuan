"""Aggregate actual OHLC, preserving lunch/session boundaries and missing amounts."""

from collections import defaultdict
from datetime import time

from chengchuan.domain.models import SHANGHAI, MarketBar


def combine(rows):
    if not rows or any(b.open is None for b in rows):
        raise ValueError("合成K线需要真实OHLC，不能使用分钟末采样价")
    return MarketBar(
        rows[-1].timestamp,
        rows[-1].close,
        sum(b.volume_shares for b in rows),
        sum(b.amount_yuan for b in rows) if all(b.amount_yuan is not None for b in rows) else None,
        rows[0].open,
        max(b.high for b in rows),
        min(b.low for b in rows),
    )


def calendar_bars(bars, period):
    if period not in ("week", "month", "quarter", "year"):
        raise ValueError("自然周期聚合仅支持周、月、季、年")
    groups = defaultdict(list)
    for bar in bars:
        day = bar.timestamp.astimezone(SHANGHAI).date()
        key = {
            "week": day.isocalendar()[:2],
            "month": (day.year, day.month),
            "quarter": (day.year, (day.month - 1) // 3),
            "year": (day.year,),
        }[period]
        groups[key].append(bar)
    return tuple(combine(rows) for rows in groups.values())


def minute_bucket(stamp, minutes):
    local = stamp.astimezone(SHANGHAI)
    clock = local.time().replace(tzinfo=None)
    if time(9, 30) <= clock <= time(11, 30):
        session, offset = "am", local.hour * 60 + local.minute - 570
    elif time(13, 1) <= clock <= time(15):
        session, offset = "pm", local.hour * 60 + local.minute - 780
    else:
        raise ValueError("分钟数据包含连续交易时段外的时间，需核对接口口径")
    # 09:30 includes the opening auction and joins the first continuous-session bucket.
    return local.date(), session, (max(1, offset) - 1) // minutes


def minute_bars(bars, minutes):
    if minutes not in (1, 5, 15, 30, 60):
        raise ValueError("分钟周期不支持")
    if any(b.open is None for b in bars):
        raise ValueError("分钟K线必须使用真实OHLC")
    if minutes == 1:
        return tuple(bars)
    groups = defaultdict(list)
    for bar in bars:
        groups[minute_bucket(bar.timestamp, minutes)].append(bar)
    # The last observation time is retained; a partial bucket never claims a future close time.
    return tuple(combine(rows) for rows in groups.values())
