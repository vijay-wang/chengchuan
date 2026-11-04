from datetime import datetime, time

from chengchuan.application.errors import MarketError
from chengchuan.application.quote_metrics import number
from chengchuan.application.sectors import rank_members
from chengchuan.domain.market import stock_symbol
from chengchuan.domain.models import SHANGHAI
from chengchuan.domain.sectors import SectorMember, SectorSnapshot
from chengchuan.infrastructure.meoz_dashboard import SCREENING_FIELDS, cache_policy


def fetch_sector(client, kind, code, name):
    if kind not in ("hy", "gn") or not str(code).isdigit():
        raise ValueError("板块类型或代码无效")
    now, age, minimum = cache_policy()
    members = client.query(
        "theme_members",
        dict(type=kind),
        max_age=86400,
        min_fetched=datetime.combine(now.date(), time(), SHANGHAI).timestamp(),
    )
    rows = [r for r in members.rows if str(r.get("theme_symbol")) == code]
    if len(rows) != 1 or not isinstance(rows[0].get("symbols"), list):
        raise MarketError("暂无该板块完整成份，不能用名称模糊匹配替代")
    symbols, unsupported = set(), 0
    for value in rows[0]["symbols"]:
        try:
            symbols.add(stock_symbol(str(value)))
        except ValueError:
            unsupported += 1
    from chengchuan.application.market_refresh import market_checkpoint

    point = market_checkpoint(now)
    day = point.cutoff.date()
    quotes = client.query(
        "screening",
        dict(tradedate=day.strftime("%Y%m%d")),
        SCREENING_FIELDS,
        max_age=age,
        min_fetched=minimum,
    )
    if len(quotes.rows) >= 6000:
        raise MarketError("筛选响应达到截断边界，拒绝不完整排名")
    by_symbol = {}
    for row in quotes.rows:
        if str(row.get("tradedate")) != day.strftime("%Y%m%d"):
            raise MarketError("板块成员行情混入其他日期")
        try:
            symbol = stock_symbol(str(row["symbol"]))
        except ValueError:
            continue
        if symbol in by_symbol:
            raise MarketError("板块行情证券重复")
        by_symbol[symbol] = row
    result = []
    for symbol in symbols:
        row = by_symbol.get(symbol, {})
        price = number(row.get("close"))
        result.append(
            SectorMember(
                symbol,
                str(row.get("name") or symbol[-6:]),
                price if price and price > 0 else None,
                number(row.get("pct_chg")),
                number(row.get("main_net_amount")),
            )
        )
    return SectorSnapshot(
        kind,
        code,
        name,
        day,
        rank_members(result),
        quotes.fetched_at,
        members.fetched_at,
        notice=f"当前成份，非历史成份；主排序涨跌幅降序，同涨幅按主力净流入降序。支持{len(symbols)}只，其他市场{unsupported}只未列入。",
        cached=members.cached and quotes.cached,
        complete=minimum is None or quotes.fetched_at.timestamp() >= minimum,
    )
