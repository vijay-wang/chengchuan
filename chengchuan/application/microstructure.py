"""Original-order amount bands, cancel exclusion, and conservative FIFO replay."""

from bisect import bisect_right
from collections import defaultdict
from datetime import datetime, time

from chengchuan.domain.microstructure import (
    AmountBand,
    ExecutionMatch,
    MinuteOrderBands,
    OrderAnalysis,
    OrderBandGroup,
    OrderDaySummary,
    OrderFilter,
    OrderMatchGroup,
    PriceVolume,
    ReconstructedOrder,
)
from chengchuan.domain.models import SHANGHAI

BAND_LIMITS_YUAN = (200000, 500000, 1000000, 2000000, 4000000, 6000000, 10000000, 20000000)
BAND_LABELS = (
    "0–20万",
    "20–50万",
    "50–100万",
    "100–200万",
    "200–400万",
    "400–600万",
    "600–1000万",
    "1000–2000万",
    "2000万以上",
)
TRADE_CODES = frozenset(("0", "0X00", "F", "T"))
CANCEL_CODES = frozenset(("C", "4", "D"))
BAND_LIMITS_FEN = tuple(v * 100 for v in BAND_LIMITS_YUAN)


def summarize_day(analysis):
    return OrderDaySummary(
        analysis.symbol,
        analysis.day,
        analysis.cutoff,
        analysis.complete,
        analysis.minute_groups,
        analysis.notice,
    )


def band_index(amount_fen):
    return bisect_right(BAND_LIMITS_FEN, amount_fen)


def _groups(rows):
    totals = defaultdict(
        lambda: {"B": [[0, 0] for _ in BAND_LABELS], "S": [[0, 0] for _ in BAND_LABELS]}
    )
    for order in rows:
        if order.ambiguous or order.original_fen <= 0:
            continue
        group = totals[(order.executed_shares == 0, order.cancelled)]
        bucket = group[order.side][band_index(order.original_fen)]
        bucket[0] += order.original_fen
        bucket[1] += 1
    return tuple(
        OrderBandGroup(
            unfilled,
            cancelled,
            tuple(AmountBand(label, *v) for label, v in zip(BAND_LABELS, sides["B"])),
            tuple(AmountBand(label, *v) for label, v in zip(BAND_LABELS, sides["S"])),
        )
        for (unfilled, cancelled), sides in sorted(totals.items())
    )


def select_bands(groups, filters=OrderFilter()):
    selected = [
        g
        for g in groups
        if (filters.include_unfilled or not g.unfilled)
        and (filters.include_cancelled or not g.cancelled)
    ]
    return tuple(
        tuple(
            AmountBand(
                label,
                sum(getattr(g, side)[i].amount_fen for g in selected),
                sum(getattr(g, side)[i].orders for g in selected),
            )
            for i, label in enumerate(BAND_LABELS)
        )
        for side in ("buy", "sell")
    )


def match_groups(tape, analysis, selected):
    """Group real execution links by unique opposing original order, never by price guesses."""
    if selected.ambiguous or selected.exchange_id <= 0:
        return ()
    opposite = "S" if selected.side == "B" else "B"
    originals = defaultdict(list)
    for order in analysis.restored:
        if order.side == opposite:
            originals[order.exchange_id].append(order)
    grouped = defaultdict(list)
    for trade in tape.trades:
        if (
            trade.time > analysis.cutoff
            or trade.kind.upper() not in TRADE_CODES
            or trade.price_fen <= 0
            or trade.shares <= 0
        ):
            continue
        anchor_id = trade.bid_id if selected.side == "B" else trade.ask_id
        if anchor_id == selected.exchange_id:
            grouped[trade.ask_id if selected.side == "B" else trade.bid_id].append(trade)
    result = []
    for order_id, trades in grouped.items():
        candidates = originals.get(order_id, ())
        counterpart = (
            candidates[0]
            if order_id > 0 and len(candidates) == 1 and not candidates[0].ambiguous
            else None
        )
        result.append(
            OrderMatchGroup(counterpart, tuple(sorted(trades, key=lambda t: (t.time, t.trade_id))))
        )
    return tuple(sorted(result, key=lambda g: (g.trades[0].time, g.trades[0].trade_id)))


def execution_matches(tape, restored, cutoff):
    unique = {(r.side, r.exchange_id): r for r in restored if not r.ambiguous and r.exchange_id > 0}
    return tuple(
        ExecutionMatch(t, unique.get(("B", t.bid_id)), unique.get(("S", t.ask_id)))
        for t in sorted(tape.trades, key=lambda t: (t.time, t.trade_id))
        if t.time <= cutoff and t.kind.upper() in TRADE_CODES and t.price_fen > 0 and t.shares > 0
    )


def select_executions(analysis, filters, selected=None):
    return tuple(
        row
        for row in analysis.executions
        if (
            filters.include_cancelled
            or not any(o is not None and o.cancelled for o in (row.buy, row.sell))
        )
        and (selected is None or (row.buy if selected.side == "B" else row.sell) is selected)
    )


def execution_spans(rows, side):
    """Only merge adjacent matches. Never reorder executions to join an ID."""
    result = []
    i = 0
    while i < len(rows):
        order = getattr(rows[i], side)
        stop = i + 1
        while order is not None and stop < len(rows) and getattr(rows[stop], side) is order:
            stop += 1
        if order is not None:
            result.append((i, stop - i, order))
        i = stop
    return tuple(result)


def analyze_orders(tape, cutoff=None):
    cutoff = cutoff or datetime.combine(tape.day, time(23, 59, 59, 999999), SHANGHAI)
    if cutoff.utcoffset() is None or cutoff.astimezone(SHANGHAI).date() != tape.day:
        raise ValueError("逐笔分析截止时点不属于数据日期")
    records = defaultdict(list)
    cancelled = set()
    cancelled_any = set()
    uncertain = set()
    unknown, unmatched = 0, 0
    for event in tape.orders:
        if event.time > cutoff:
            continue
        kind = event.kind.upper()
        if kind == "D":
            if event.exchange_id > 0:
                if event.side in ("B", "S"):
                    cancelled.add((event.side, event.exchange_id))
                else:
                    cancelled_any.add(event.exchange_id)
            continue
        if kind not in ("A", "0", "1", "U") or event.side not in ("B", "S"):
            unknown += 1
            continue
        records[(event.side, event.exchange_id)].append(event)
    fills = defaultdict(lambda: [0, 0, 0])
    price_volume = defaultdict(lambda: [0, 0, 0])
    for event in tape.trades:
        if event.time > cutoff:
            continue
        kind = event.kind.upper()
        refs = (("B", event.bid_id), ("S", event.ask_id))
        if kind in CANCEL_CODES:
            for side, order_id in refs:
                if order_id > 0:
                    cancelled.add((side, order_id))
            continue
        if kind not in TRADE_CODES or event.price_fen <= 0 or event.shares <= 0:
            unknown += 1
            # Unknown events are ambiguity, not confirmed cancels. Even opting
            # into cancelled orders must never reintroduce these into statistics.
            for side, order_id in refs:
                if order_id > 0:
                    uncertain.add((side, order_id))
            continue
        bucket = price_volume[event.price_fen]
        bucket[0 if event.side == "B" else 1 if event.side == "S" else 2] += event.shares
        for side, order_id in refs:
            if order_id <= 0 or len(records.get((side, order_id), ())) != 1:
                unmatched += 1
                continue
            aggregate = fills[(side, order_id)]
            aggregate[0] += event.shares
            aggregate[1] += event.price_fen * event.shares
            aggregate[2] += 1
    restored = []
    for (side, order_id), events in records.items():
        ambiguous = order_id <= 0 or len(events) != 1 or (side, order_id) in uncertain
        for event in events:
            executed, amount, count = fills[(side, order_id)] if not ambiguous else (0, 0, 0)
            impossible = executed > event.shares
            restored.append(
                ReconstructedOrder(
                    event.time,
                    order_id,
                    side,
                    event.price_fen,
                    event.shares,
                    executed,
                    amount,
                    count,
                    (side, order_id) in cancelled or order_id in cancelled_any,
                    ambiguous or impossible,
                    event.kind,
                )
            )
    restored.sort(key=lambda r: (r.time, r.side, r.exchange_id))
    totals = {"B": [[0, 0] for _ in BAND_LABELS], "S": [[0, 0] for _ in BAND_LABELS]}
    cancelled_count = ambiguous_count = unpriced = 0
    for order in restored:
        if order.cancelled:
            cancelled_count += 1
            continue
        if order.ambiguous:
            ambiguous_count += 1
            continue
        if order.original_fen <= 0:
            unpriced += 1
            continue
        index = band_index(order.original_fen)
        totals[order.side][index][0] += order.original_fen
        totals[order.side][index][1] += 1
    complete = tape.orders_complete and tape.trades_complete
    note = (
        "按每张原始委托的价格×原始数量分档，含未成交的有效委托，非实际成交资金。"
        "发生撤销（含部分成交后撤销）的整张委托剔除；0号、重复号、超额匹配及无可定价委托不纳入。"
        "区间左闭右开，2000万及以上归最后档；买卖各自合计作为饼图分母。"
    )
    if not complete:
        note = "分页未齐，尚不能完整排除后续撤单；金额及饼图暂不发布。" + note
    minutes = defaultdict(list)
    for order in restored:
        minutes[order.time.astimezone(SHANGHAI).replace(second=0, microsecond=0)].append(order)
    return OrderAnalysis(
        tape.symbol,
        tape.day,
        cutoff,
        tuple(AmountBand(label, *values) for label, values in zip(BAND_LABELS, totals["B"])),
        tuple(AmountBand(label, *values) for label, values in zip(BAND_LABELS, totals["S"])),
        tuple(restored),
        tuple(
            PriceVolume(price, *values)
            for price, values in sorted(price_volume.items(), reverse=True)
        ),
        complete,
        cancelled_count,
        ambiguous_count,
        unknown,
        unmatched,
        unpriced,
        note,
        _groups(restored),
        tuple(MinuteOrderBands(minute, _groups(rows)) for minute, rows in sorted(minutes.items())),
        sum(r.ambiguous for r in restored),
        sum(not r.ambiguous and r.original_fen <= 0 for r in restored),
        execution_matches(tape, restored, cutoff),
    )


def order_queue(analysis, side, price_fen, limit=50):
    if side not in ("B", "S") or not 1 <= limit <= 1000:
        raise ValueError("队列方向或数量无效")
    rows = [
        r
        for r in analysis.restored
        if r.side == side
        and r.price_fen == price_fen
        and r.remaining_shares > 0
        and r.order_kind in ("A", "0")
    ]
    # Source lacks a channel/complete cross-feed sequence. Same-time ordering is
    # deterministic, not claimed to be the exchange's exact queue priority.
    return tuple(sorted(rows, key=lambda r: (r.time, r.exchange_id))[:limit])
