"""Shared, deterministic quote arithmetic; no access to prices after the requested date."""

from math import isfinite
from statistics import mean

from chengchuan.domain.market import is_index
from chengchuan.domain.market_dashboard import MarketBreadth, QuoteDetails, QuoteMetric
from chengchuan.domain.models import SHANGHAI


def number(value):
    if isinstance(value, bool):
        return None
    if value in (None, "", "-", "--"):
        return None
    try:
        result = float(value)
    except (ValueError, TypeError):
        return None
    return result if isfinite(result) else None


def details_from_frame(frame):
    series = frame.snapshot.series
    bar = series.bars[-1]
    day = bar.timestamp.astimezone(SHANGHAI).date()
    quote = (
        frame.quote
        if frame.quote and frame.quote.timestamp.astimezone(SHANGHAI).date() == day
        else None
    )
    row = dict(
        close=quote.price if quote else bar.close,
        pre_close=quote.previous_close if quote else dict(frame.reference_closes).get(day),
    )
    if series.interval == "day":
        row.update(
            open=bar.open,
            high=bar.high,
            low=bar.low,
            vol=bar.volume_shares / 100,
            amount=bar.amount_yuan,
        )
    metrics = base_metrics(
        series.symbol,
        row,
        frame.provider_name or frame.snapshot.source,
        "仅图表中可确认字段；其他周期的高低量额不冒充当日指标",
    )
    return QuoteDetails(
        series.symbol, day, tuple(metrics), frame.retrieved_at, frame.as_of, frame.cached
    )


def base_metrics(symbol, row, source, note=""):
    point = "点" if is_index(symbol) else "元"
    fields = [
        ("close", "最新点位" if is_index(symbol) else "最新价", point),
        ("pre_close", "昨收", point),
        ("open", "今开", point),
        ("high", "最高", point),
        ("low", "最低", point),
    ]
    result = []
    for key, label, unit in fields:
        value = number(row.get(key))
        result.append(
            QuoteMetric(
                key, label, value if value is not None and value > 0 else None, unit, source, note
            )
        )
    previous = number(row.get("pre_close"))
    price = number(row.get("close"))
    high, low = number(row.get("high")), number(row.get("low"))
    previous = previous if previous is not None and previous > 0 else None
    price = price if price is not None and price > 0 else None
    change = price - previous if price is not None and previous else None
    percent = change / previous * 100 if change is not None else None
    amplitude = (
        (high - low) / previous * 100
        if high is not None and low is not None and high >= low > 0 and previous
        else None
    )
    volume = number(row.get("vol"))
    if volume is not None and volume < 0:
        volume = None
    amount = number(row.get("amount"))
    if amount is not None and amount < 0:
        amount = None
    result.extend(
        (
            QuoteMetric("change", "涨跌额", change, point, source, note),
            QuoteMetric("pct_chg", "涨跌幅", percent, "%", source, note),
            QuoteMetric(
                "amplitude", "振幅", amplitude, "%", source, "(最高－最低)/昨收×100；" + note
            ),
            QuoteMetric(
                "volume", "成交量", volume * 100 if volume is not None else None, "股", source, note
            ),
            QuoteMetric("amount", "成交额", amount, "元", source, note),
        )
    )
    return result


def trading_minutes(stamp):
    local = stamp.astimezone(SHANGHAI)
    minutes = local.hour * 60 + local.minute + local.second / 60
    return max(0, min(120, minutes - 570)) + max(0, min(120, minutes - 780))


def volume_ratio(bars, stamp, completed=False):
    """Current per-minute volume / preceding five sessions' per-minute volume."""
    if len(bars) < 6:
        return None
    current = bars[-1]
    previous = bars[-6:-1]
    if any(b.volume_shares <= 0 for b in previous):
        return None
    if stamp is None and not completed:
        return None
    elapsed = 240 if completed else trading_minutes(stamp)
    if elapsed <= 0:
        return None
    return current.volume_shares / elapsed / (mean(b.volume_shares for b in previous) / 240)


SCOPE_LABELS = {
    "all": "全市场A股（源范围）",
    "sh": "沪市A股",
    "sz": "深市A股",
    "growth": "创业板A股",
    "star": "科创板A股",
}


def aggregate_breadth(rows, day):
    groups = {key: [] for key in SCOPE_LABELS}
    seen = set()
    for row in rows:
        if str(row.get("tradedate")) != day.strftime("%Y%m%d"):
            raise ValueError("市场统计混入其他日期")
        code = str(row.get("code") or row.get("symbol") or "").upper()
        digits = code.split(".")[0]
        if not digits.isdigit() or len(digits) != 6 or digits in seen:
            raise ValueError("市场统计包含错位或重复证券")
        seen.add(digits)
        # Screening's universe is A shares. Preserve its declared universe and
        # report unknown prices/partial money coverage instead of zero filling.
        groups["all"].append(row)
        if code.endswith(".SH") or digits.startswith("6"):
            groups["sh"].append(row)
            if digits.startswith(("688", "689")):
                groups["star"].append(row)
        elif code.endswith(".SZ") or digits.startswith(("00", "30")):
            groups["sz"].append(row)
            if digits.startswith("30"):
                groups["growth"].append(row)
    output = []
    for scope, items in groups.items():
        if not items:
            continue
        values = [number(r.get("pct_chg")) for r in items]
        flows = [number(r.get("main_net_amount")) for r in items]
        known = [f for f in flows if f is not None]
        output.append(
            MarketBreadth(
                scope,
                SCOPE_LABELS[scope],
                day,
                sum(v is not None and v > 0 for v in values),
                sum(v is not None and v < 0 for v in values),
                sum(v == 0 for v in values),
                sum(v is None for v in values),
                len(items),
                sum(known) if known else None,
                len(known),
            )
        )
    return tuple(output)
