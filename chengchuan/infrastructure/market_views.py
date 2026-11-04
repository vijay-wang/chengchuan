"""Verified MeoZ heatmaps and Eastmoney public order-size cash-flow snapshots."""

import json
from datetime import datetime, timezone
from urllib.parse import urlencode

from chengchuan.application.errors import MarketError
from chengchuan.application.market_refresh import observed_current
from chengchuan.application.quote_metrics import number
from chengchuan.domain.market import INDEX_NAMES, instrument_symbol, is_index, market_code
from chengchuan.domain.market_views import (
    FlowBucket,
    HeatmapSnapshot,
    HeatTile,
    MoneyFlow,
    funds_symbol,
)
from chengchuan.domain.models import SHANGHAI
from chengchuan.infrastructure.market_data import http_text

PUBLIC_FLOW_FIELDS = "f12,f13,f14,f124,f62,f64,f65,f66,f70,f71,f72,f76,f77,f78,f82,f83,f84"


def positive(value):
    result = number(value)
    return result if result is not None and result >= 0 else None


def total(values):
    return sum(values) if all(v is not None for v in values) else None


def parse_public_flow(payload, symbol, retrieved_at):
    symbol = funds_symbol(symbol)
    if not isinstance(payload, dict):
        raise MarketError("公开资金响应结构变化")
    data = payload.get("data") if isinstance(payload, dict) else None
    rows = data.get("diff") if isinstance(data, dict) else None
    if payload.get("rc") != 0 or not isinstance(rows, list):
        raise MarketError("公开资金响应结构变化")
    expected = {"1.000001", "0.399001"} if symbol == "market:shsz" else {public_secid(symbol)}
    seen, stamps, parsed = set(), [], []
    for row in rows:
        identity = str(row.get("f13")) + "." + str(row.get("f12"))
        if identity not in expected or identity in seen:
            raise MarketError("公开资金返回其他证券或重复数据")
        seen.add(identity)
        stamp = number(row.get("f124"))
        if stamp is None or stamp <= 0:
            raise MarketError("公开资金缺少有效数据时间")
        try:
            stamps.append(datetime.fromtimestamp(stamp, SHANGHAI))
        except (ValueError, OverflowError, OSError):
            raise MarketError("公开资金时间无效") from None
        buckets = []
        for key, label, incoming, outgoing, net in (
            ("super", "超大单", "f64", "f65", "f66"),
            ("large", "大单", "f70", "f71", "f72"),
            ("medium", "中单", "f76", "f77", "f78"),
            ("small", "小单", "f82", "f83", "f84"),
        ):
            buckets.append(
                FlowBucket(
                    key,
                    label,
                    positive(row.get(incoming)),
                    positive(row.get(outgoing)),
                    number(row.get(net)),
                )
            )
        by_key = {b.key: b for b in buckets}
        main, other = (
            (by_key[k] for k in ("super", "large")),
            (by_key[k] for k in ("medium", "small")),
        )
        main, other = tuple(main), tuple(other)
        buckets[:0] = [
            FlowBucket(
                "main",
                "主力（大单＋超大单）",
                total(tuple(b.inflow for b in main)),
                total(tuple(b.outflow for b in main)),
                number(row.get("f62")),
            ),
            FlowBucket(
                "retail",
                "散户参考（中小单）",
                total(tuple(b.inflow for b in other)),
                total(tuple(b.outflow for b in other)),
                total(tuple(b.net for b in other)),
            ),
        ]
        parsed.append(buckets)
    if seen != expected or len({s.date() for s in stamps}) != 1:
        raise MarketError("公开资金证券覆盖不全或日期不一致")
    buckets = tuple(
        FlowBucket(
            b.key,
            b.label,
            total(tuple(r[i].inflow for r in parsed)),
            total(tuple(r[i].outflow for r in parsed)),
            total(tuple(r[i].net for r in parsed)),
        )
        for i, b in enumerate(parsed[0])
    )
    name = (
        "沪深大盘"
        if symbol == "market:shsz"
        else str(rows[0].get("f14") or INDEX_NAMES.get(symbol, symbol[-6:]))
    )
    scope = (
        "上证与深证来源范围相加"
        if symbol == "market:shsz"
        else name + "（来源关联范围）"
        if is_index(symbol)
        else "当前个股"
    )
    return MoneyFlow(
        symbol,
        name,
        stamps[0].date(),
        min(stamps),
        retrieved_at,
        buckets,
        "东方财富公开资金分类",
        scope,
        "按成交规模分类，非真实账户身份；主力=大单+超大单，散户参考=中单+小单。"
        "流入流出为分类买卖金额，净额使用源统计；可能存在舍入差。指数范围未独立复核成分名单。",
        complete=observed_current(stamps, retrieved_at)
        and all(
            b.inflow is not None and b.outflow is not None and b.net is not None for b in buckets
        ),
    )


def public_secid(symbol):
    code = market_code(instrument_symbol(symbol))
    return ("1." if code.startswith("sh") else "0.") + code[-6:]


def fetch_public_flow(symbol):
    symbol = funds_symbol(symbol)
    secids = "1.000001,0.399001" if symbol == "market:shsz" else public_secid(symbol)
    url = "https://push2.eastmoney.com/api/qt/ulist.np/get?" + urlencode(
        dict(fltt=2, secids=secids, fields=PUBLIC_FLOW_FIELDS)
    )
    try:
        return parse_public_flow(json.loads(http_text(url)), symbol, datetime.now(timezone.utc))
    except (ValueError, TypeError, KeyError, OSError) as error:
        raise MarketError("公开资金暂不可用：" + str(error)) from None


def fetch_heatmap(client, kind):
    from chengchuan.infrastructure.meoz_dashboard import (
        SCREENING_FIELDS,
        cache_policy,
        latest_query_age,
    )

    if kind not in ("hy", "gn", "stocks"):
        raise ValueError("不支持的热力图类型")
    now, age, minimum = cache_policy()
    if kind == "stocks":
        # Share the exact full-market screening request with index breadth.
        from chengchuan.application.market_refresh import market_checkpoint
        from chengchuan.domain.calendar import CalendarUnavailable

        try:
            point = market_checkpoint(now)
            day = now.date() if point.live else point.cutoff.date()
        except CalendarUnavailable:
            raise MarketError("交易日历待更新，暂不能自动确定个股热力图日期") from None
        result = client.query(
            "screening",
            dict(tradedate=day.strftime("%Y%m%d")),
            SCREENING_FIELDS,
            max_age=age,
            min_fetched=minimum,
        )
    else:
        fields = "tradedate,symbol,name,type,pct_chg,amount"
        age = latest_query_age(client, "theme_daily", dict(type=kind), fields, now, age, minimum)
        result = client.query(
            "theme_daily",
            dict(type=kind),
            fields,
            max_age=age,
            min_fetched=minimum,
        )
    if not result.rows or len(result.rows) >= 6000:
        raise MarketError("热力图暂无完整数据，空响应或达到截断边界")
    dates = {str(r.get("tradedate")) for r in result.rows}
    if len(dates) != 1:
        raise MarketError("热力图混入不同交易日")
    day = datetime.strptime(dates.pop(), "%Y%m%d").date()
    tiles = []
    for row in result.rows:
        if kind != "stocks" and row.get("type") != kind:
            raise MarketError("热力图返回其他板块类型")
        symbol = None
        if kind == "stocks":
            try:
                symbol = instrument_symbol(str(row.get("code") or row["symbol"]).split(".")[0])
            except ValueError:
                continue  # This workbench only opens SH/SZ A shares.
        tiles.append(
            HeatTile(
                str(row["symbol"]),
                str(row.get("name") or row["symbol"]),
                number(row.get("pct_chg")),
                positive(row.get("amount")),
                symbol,
            )
        )
    notice = result.notice + (
        f"；仅展示支持的沪深A股，源{len(result.rows)}条、接纳{len(tiles)}条"
        if kind == "stocks"
        else "；板块可能存在层级或成分交叠，面积不代表互斥市场份额"
    )
    return HeatmapSnapshot(
        kind,
        day,
        tuple(tiles),
        result.fetched_at,
        "猫爪实时选股" if kind == "stocks" else "猫爪板块日线",
        notice,
        result.cached,
        complete=minimum is None or result.fetched_at.timestamp() >= minimum,
    )
