"""Strict adapters for MeoZ stock snapshots and published Level-2 cursor pages."""

import json
from dataclasses import replace
from datetime import datetime, time, timezone
from decimal import ROUND_HALF_UP, Decimal, InvalidOperation
from pathlib import Path

from chengchuan.application.errors import MarketError
from chengchuan.application.quote_metrics import number
from chengchuan.domain.market import stock_symbol
from chengchuan.domain.microstructure import (
    BookLevel,
    BookSnapshot,
    OrderEvent,
    OrderTape,
    TradeEvent,
)
from chengchuan.domain.models import SHANGHAI

QUOTE_FIELDS = "symbol,tradedate,time,close,volume,amount," + ",".join(
    f"{key}{i}" for i in range(1, 11) for key in ("bid", "bid_vol", "ask", "ask_vol")
)


def matches_stock(value, symbol):
    raw = str(value).lower()
    if "." in raw:
        digits, exchange = raw.rsplit(".", 1)
        return exchange + digits == symbol
    return raw in (symbol, symbol[-6:])


def record_time(day, value):
    raw = str(value)
    if raw.isdigit() and len(raw) in (10, 13):
        stamp = datetime.fromtimestamp(int(raw) / (1000 if len(raw) == 13 else 1), SHANGHAI)
    elif "-" in raw:
        stamp = datetime.fromisoformat(raw)
        stamp = (
            stamp.astimezone(SHANGHAI)
            if stamp.utcoffset() is not None
            else stamp.replace(tzinfo=SHANGHAI)
        )
    else:
        digits = raw.replace(":", "").replace(".", "")
        digits = digits.zfill(9) if len(digits) > 6 else digits.zfill(6) + "000"
        if len(digits) != 9 or not digits.isdigit():
            raise MarketError("逐笔时间格式无效")
        stamp = datetime.combine(
            day,
            time(int(digits[:2]), int(digits[2:4]), int(digits[4:6]), int(digits[6:]) * 1000),
            SHANGHAI,
        )
    if stamp.date() != day:
        raise MarketError("逐笔日期与请求不一致")
    return stamp


def integer(value):
    # int(float(...)) would lose large order identifiers; never go through float.
    if isinstance(value, bool):
        raise MarketError("逐笔整数无效")
    try:
        result = int(value)
        if str(result) != str(value) or result < 0:
            raise ValueError()
        return result
    except (ValueError, TypeError):
        raise MarketError("逐笔编号或数量不是非负整数") from None


def fen(value):
    try:
        price = Decimal(str(value))
        if not price.is_finite() or price < 0:
            raise ValueError()
        return int((price * 100).quantize(Decimal("1"), rounding=ROUND_HALF_UP))
    except (InvalidOperation, ValueError):
        raise MarketError("逐笔价格无效") from None


def parse_book(row, symbol, day, depth=10, lot_multiplier=1):
    levels = []
    for i in range(1, depth + 1):
        bid, ask = number(row.get("bid" + str(i))), number(row.get("ask" + str(i)))
        bq, aq = number(row.get("bid_vol" + str(i))), number(row.get("ask_vol" + str(i)))
        levels.append(
            BookLevel(
                i,
                bid if bid and bid > 0 else None,
                bq * lot_multiplier if bq is not None and bq >= 0 else None,
                ask if ask and ask > 0 else None,
                aq * lot_multiplier if aq is not None and aq >= 0 else None,
            )
        )
    return BookSnapshot(
        symbol,
        record_time(day, row["time"]),
        tuple(levels),
        "猫头鹰股票五档快照" if depth == 5 else "猫头鹰历史十档",
        notice="五档合计数量，非单笔队列；股票源数量由手转股（已与L2核对）"
        if lot_multiplier == 100
        else "历史快照；集合竞价档位不作连续竞价解释",
        historical=depth == 10,
    )


def fetch_book(client, symbol):
    symbol = stock_symbol(symbol)
    result = client.query("tick", dict(type=1, asset="stock", symbols=symbol[-6:]), max_age=180)
    if len(result.rows) != 1 or not matches_stock(result.rows[0].get("symbol"), symbol):
        raise MarketError("五档快照证券或数量无效")
    row = result.rows[0]
    raw = row.get("time")
    stamp = (
        datetime.fromtimestamp(int(raw) / 1000, SHANGHAI)
        if str(raw).isdigit() and len(str(raw)) == 13
        else datetime.fromisoformat(str(raw)).replace(tzinfo=SHANGHAI)
    )
    return parse_book(row, symbol, stamp.date(), depth=5, lot_multiplier=100)


def load_pages(client, api, symbol, day, max_pages, fields=""):
    result_rows = []
    cursor = None
    seen = set()
    fetched = []
    notice = []
    complete = False
    for _ in range(max_pages):
        params = dict(
            symbol=symbol[-6:], tradedate=day.strftime("%Y%m%d"), format="json", page_size=10000
        )
        if cursor:
            params["cursor"] = cursor
        result = client.query(api, params, fields, service="history", max_age=None)
        if result.page is None or result.page.get("tradedate") not in (
            None,
            day.strftime("%Y%m%d"),
        ):
            raise MarketError("逐笔接口缺少可验证分页信息或日期错位")
        if len(result.rows) > 10000:
            raise MarketError("逐笔分页超过10000条边界")
        for row in result.rows:
            if not matches_stock(row.get("symbol"), symbol) or str(
                row.get("tradedate")
            ) != day.strftime("%Y%m%d"):
                raise MarketError("逐笔页混入其他证券或日期")
        result_rows.extend(result.rows)
        fetched.append(result.fetched_at)
        if result.notice:
            notice.append(result.notice)
        if not result.page["has_more"]:
            complete = True
            break
        cursor = result.page["next_cursor"]
        if cursor in seen or not result.rows:
            raise MarketError("逐笔游标重复或空页仍声称有后续")
        seen.add(cursor)
    return result_rows, complete, max(fetched), tuple(notice)


def fetch_tape(client, symbol, day, max_pages=6, quote_optional=False, include_quotes=True):
    symbol = stock_symbol(symbol)
    if not 1 <= max_pages <= 8:
        raise ValueError("逐笔每接口最多8页")
    orders, oc, ot, on = load_pages(client, "level2_order_history", symbol, day, max_pages)
    trades, tc, tt, tn = load_pages(client, "level2_trade_history", symbol, day, max_pages)
    quotes, qc, qt, qn = [], False, max(ot, tt), ("未载入十档；不影响完整委托及撤单统计",)
    if include_quotes:
        try:
            quotes, qc, qt, qn = load_pages(
                client, "level2_quote_history", symbol, day, 1, QUOTE_FIELDS
            )
        except MarketError:
            if not quote_optional or getattr(client, "offline", False) is not True:
                raise
    notice = "；".join(dict.fromkeys((*on, *tn, *qn)))
    if not oc or not tc:
        notice += "；达到本次有界分页上限，覆盖未完整；不会发布完整资金分档。"
    if include_quotes and not qc:
        notice += "；十档快照覆盖未完整；盘口回放只覆盖已载入时间，不影响完整委托／撤单的分档。"
    return parse_tape(symbol, day, orders, trades, quotes, oc, tc, qc, max(ot, tt, qt), notice)


def parse_tape(symbol, day, orders, trades, quotes, oc, tc, qc, retrieved_at, notice=""):
    for row in (*orders, *trades, *quotes):
        if not matches_stock(row.get("symbol"), symbol) or str(
            row.get("tradedate")
        ) != day.strftime("%Y%m%d"):
            raise MarketError("逐笔导入混入其他证券或日期")
    parsed_orders = tuple(
        OrderEvent(
            record_time(day, r["time"]),
            integer(r["exchange_order_id"]),
            integer(r["order_id"]),
            str(r["order_type"]),
            str(r["order_code"]).upper(),
            fen(r["price"]),
            integer(r["volume"]),
        )
        for r in orders
    )
    parsed_trades = tuple(
        TradeEvent(
            record_time(day, r["time"]),
            integer(r["trade_id"]),
            str(r["trade_code"]),
            str(r["bs_flag"]).upper(),
            integer(r["bid_order_id"]),
            integer(r["ask_order_id"]),
            fen(r["price"]),
            integer(r["volume"]),
        )
        for r in trades
    )
    parsed_quotes = tuple(parse_book(r, symbol, day) for r in quotes)
    return OrderTape(
        symbol,
        day,
        parsed_orders,
        parsed_trades,
        parsed_quotes,
        oc,
        tc,
        qc,
        retrieved_at,
        notice,
    )


class JsonOrderTapeRepository:
    def load(self, path):
        path = Path(path)
        with path.open("rb") as stream:
            raw = stream.read(64_000_001)
        if len(raw) > 64_000_000:
            raise MarketError("逐笔JSON超过64MB上限，请拆分或使用专用数据导入")
        data = json.loads(raw)
        if not isinstance(data, dict) or data.get("version") != 1:
            raise MarketError("逐笔JSON格式应为version=1的orders/trades/quotes数据包")
        symbol = stock_symbol(data["symbol"])
        day = datetime.strptime(data["day"], "%Y-%m-%d").date()
        records = [data.get(key, []) for key in ("orders", "trades", "quotes")]
        if (
            any(
                not isinstance(rows, list) or any(not isinstance(r, dict) for r in rows)
                for rows in records
            )
            or sum(map(len, records)) > 200000
        ):
            raise MarketError("逐笔JSON结构无效或超过20万条边界")
        coverage = data.get("coverage") or {}
        tape = parse_tape(
            symbol,
            day,
            *records,
            *(coverage.get(key) is True for key in ("orders", "trades", "quotes")),
            datetime.now(timezone.utc),
            "来自本地JSON；覆盖完整性依据文件coverage声明，非服务端独立核验。",
        )
        return replace(
            tape,
            source="本地逐笔JSON",
            quotes=tuple(replace(q, source="本地JSON十档回放") for q in tape.quotes),
        )
