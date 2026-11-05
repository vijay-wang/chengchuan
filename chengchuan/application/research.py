"""Research workflows and transparent calculations, independent of Qt and vendors."""

import json
import math
import re
from dataclasses import replace
from datetime import datetime, timedelta
from html.parser import HTMLParser
from typing import Protocol

from chengchuan.application.errors import MarketError, StorageError
from chengchuan.domain.calendar import TradingCalendar
from chengchuan.domain.market import stock_symbol
from chengchuan.domain.models import SHANGHAI
from chengchuan.domain.research import (
    ResearchBundle,
    ResearchColumn,
    ResearchMeta,
    ResearchRequest,
    ResearchTable,
    ThemeIdentity,
)

MODULES = (
    ("rotation", "板块轮动"),
    ("evidence", "题材证据"),
    ("sentiment", "情绪封单"),
    ("auction", "集合竞价"),
    ("seats", "龙虎榜席位"),
    ("attention", "热度监管"),
)
HOT_PLATFORMS = (
    ("tdx&resou", "通达信"),
    ("xq&resou", "雪球"),
    ("dfcf&renqi", "东方财富"),
    ("ths&normal", "同花顺"),
)


class ResearchProvider(Protocol):
    def fetch(self, key, params, day, latest=True) -> ResearchTable: ...
    def previous(self, key, params, current) -> ResearchTable | None: ...
    def quotes(self, symbols, day) -> ResearchTable: ...


def numeric(value):
    if isinstance(value, bool):
        return None
    try:
        value = float(value)
        return value if math.isfinite(value) else None
    except (TypeError, ValueError):
        return None


def research_stock_symbol(value):
    text = str(value).strip().lower()
    match = re.fullmatch(r"(\d{6})\.(sh|sz|bj)", text)
    if match:
        text = match[2] + match[1]
    return stock_symbol(text)


def clock_seconds(value):
    """HHMM, HHMMSS, ISO/local datetime and Unix seconds/milliseconds."""
    text = str(value or "")
    try:
        if text.isdigit() and len(text) in (10, 13):
            stamp = datetime.fromtimestamp(int(text) / (1000 if len(text) == 13 else 1), SHANGHAI)
            return stamp.hour * 3600 + stamp.minute * 60 + stamp.second
        if "T" in text or " " in text:
            text = text.replace("T", " ").split(" ")[-1].split("+")[0]
        text = text.split(".")[0].replace(":", "")
        if text.isdigit() and len(text) in (4, 6):
            h, m = int(text[:2]), int(text[2:4])
            s = int(text[4:]) if len(text) == 6 else 0
            if h < 24 and m < 60 and s < 60:
                return h * 3600 + m * 60 + s
    except (ValueError, OverflowError, OSError):
        pass
    return None


def interval_changes(table):
    """Difference cumulative net amounts only across complete adjacent 5-minute buckets."""
    records = sorted(
        table.records(), key=lambda r: (str(r.get("tradedate")), str(r.get("trademin")))
    )
    by_time, result = {}, []
    for row in records:
        day, code = str(row.get("tradedate")), str(row.get("theme_symbol"))
        clock = clock_seconds(row.get("trademin"))
        if clock is None:
            continue
        current = numeric(row.get("main_net_amount"))
        new = dict(row)
        for minutes in (5, 15):
            keys = [(day, code, clock - i * 300) for i in range(1, minutes // 5 + 1)]
            previous = by_time.get(keys[-1])
            valid = table.meta.complete and all(k in by_time for k in keys)
            old = numeric(previous.get("main_net_amount")) if previous else None
            new[f"net_{minutes}m"] = (
                current - old if valid and current is not None and old is not None else None
            )
        before = by_time.get((day, code, clock - 300))
        now_pct = numeric(row.get("pct_chg"))
        old_pct = numeric(before.get("pct_chg")) if before else None
        delta = new["net_5m"]
        new["condition"] = (
            ("涨幅上升" if now_pct > old_pct else "涨幅回落" if now_pct < old_pct else "涨幅持平")
            + ("／净额增加" if delta > 0 else "／净额减少" if delta < 0 else "／净额不变")
            if now_pct is not None and old_pct is not None and delta is not None
            else None
        )
        result.append(new)
        by_time[(day, code, clock)] = row
    columns = (
        *table.columns,
        ResearchColumn("net_5m", "连续5分钟净额变化", "元"),
        ResearchColumn("net_15m", "连续15分钟净额变化", "元"),
        ResearchColumn("condition", "相邻完整桶事实"),
    )
    return table.with_records(result, columns=columns)


def member_rows(table, symbol="", leaders=False):
    """Filter after expanding original rank groups; preserve tied ranks."""
    result, truncated = [], False
    for row in table.records():
        groups = json.loads(row.get("symbols") or "[]")
        if not isinstance(groups, list):
            continue
        for rank, group in enumerate(groups, 1):
            tied = group if isinstance(group, list) else [group]
            for raw in tied:
                try:
                    code = research_stock_symbol(raw)[-6:]
                except ValueError:
                    continue
                if symbol and code != symbol:
                    continue
                result.append(
                    dict(
                        theme_symbol=row.get("theme_symbol"),
                        symbol=code,
                        rank=rank if leaders else None,
                        tied=len(tied) > 1 if leaders else None,
                    )
                )
                if len(result) >= 20000:
                    truncated = True
                    break
            if truncated:
                break
        if truncated:
            break
    output = table.with_records(
        result,
        columns=(
            ResearchColumn("theme_symbol", "精选板块代码"),
            ResearchColumn("symbol", "股票代码"),
            ResearchColumn("rank", "原始收盘排名"),
            ResearchColumn("tied", "并列"),
        ),
    )
    if truncated:
        output = replace(
            output,
            meta=replace(output.meta, complete=False),
            notice=output.notice + "；展开达到20000行上限，请指定股票缩小范围",
        )
    return output


def with_theme_names(table, names):
    return table.with_records(
        [dict(r, theme_name=names.get(r.get("theme_symbol"))) for r in table.records()],
        columns=(ResearchColumn("theme_name", "精选一级名称"), *table.columns),
    )


class _PlainHTML(HTMLParser):
    def __init__(self):
        super().__init__()
        self.parts, self.hidden = [], 0

    def handle_starttag(self, tag, attrs):
        if tag in ("script", "style"):
            self.hidden += 1
        elif tag in ("p", "div", "br", "li", "tr", "h1", "h2", "h3"):
            self.parts.append("\n")

    def handle_endtag(self, tag):
        if tag in ("script", "style"):
            self.hidden = max(0, self.hidden - 1)

    def handle_data(self, data):
        if not self.hidden:
            self.parts.append(data)


def plain_html(value):
    parser = _PlainHTML()
    parser.feed(str(value or "")[:300_000])
    return "".join(parser.parts).strip()


def detail_tables(table):
    doc = json.loads(table.document or "{}")
    text = "\n\n".join(
        plain_html(doc.get(k))
        for k in ("name", "brief_intro", "desc", "introduction")
        if doc.get(k)
    )
    evidence = replace(table, document=text, title="三方题材原文（非公司披露）")
    rows = [
        dict(symbol=str(r.get("symbol") or ""), name=r.get("name"))
        for r in doc.get("stock_list") or ()
        if isinstance(r, dict)
    ]
    stocks = table.with_records(
        rows,
        columns=(ResearchColumn("symbol", "股票代码"), ResearchColumn("name", "名称")),
        key="library_stocks",
        title="题材库关联股票",
    )
    hierarchy = []

    def walk(value, path="", depth=0):
        if depth > 12 or len(hierarchy) >= 12000:
            return
        if isinstance(value, list):
            for item in value:
                walk(item, path, depth + 1)
        elif isinstance(value, dict):
            # The source pairs Level1 and Level2 as sibling keys in each tree branch.
            # Preserve their relationship instead of flattening them into unrelated roots.
            if isinstance(value.get("Level1"), dict):
                parent = value["Level1"]
                walk(parent, path, depth + 1)
                parent_name = str(parent.get("Name") or parent.get("name") or "")
                parent_path = path + (" / " if path and parent_name else "") + parent_name
                walk(value.get("Level2") or [], parent_path, depth + 1)
                return
            name = str(value.get("Name") or value.get("name") or "")
            node_path = path + (" / " if path and name else "") + name
            if name:
                hierarchy.append(
                    dict(
                        path=node_path,
                        theme_id=str(value.get("ID") or ""),
                        name=name,
                        symbol=None,
                        stock_name=None,
                        reason=None,
                    )
                )
            for stock in value.get("Stocks") or ():
                if len(hierarchy) >= 12000:
                    break
                if not isinstance(stock, dict):
                    continue
                raw = str(stock.get("StockID") or stock.get("symbol") or "")
                try:
                    symbol = research_stock_symbol(raw)[-6:]
                except ValueError:
                    symbol = None
                hierarchy.append(
                    dict(
                        path=node_path,
                        theme_id=str(value.get("ID") or ""),
                        name=name,
                        symbol=symbol,
                        stock_name=stock.get("prod_name") or stock.get("name"),
                        reason=plain_html(stock.get("Reason") or stock.get("reason")),
                    )
                )
            for key, child in value.items():
                if key not in ("Stocks", "raw_payload") and isinstance(child, (dict, list)):
                    walk(child, node_path, depth + 1)

    walk(doc.get("table") or {})
    tree = table.with_records(
        hierarchy,
        key="hierarchy",
        title="题材层级与股票关联原文",
        columns=(
            ResearchColumn("path", "源内层级路径"),
            ResearchColumn("theme_id", "题材库节点ID"),
            ResearchColumn("name", "节点名称"),
            ResearchColumn("symbol", "股票代码"),
            ResearchColumn("stock_name", "股票名称"),
            ResearchColumn("reason", "源内关联说明"),
        ),
    )
    tree = replace(tree, document="层级来自源内table；只在同一来源内关联，不与精选代码合并。")
    if len(hierarchy) >= 12000:
        tree = replace(
            tree, meta=replace(tree.meta, complete=False), notice="层级达到12000行保护边界"
        )
    return evidence, stocks, tree


def auction_shares(table):
    records = []
    for row in table.records():
        new = dict(row)
        for key in ("auc_vol", "um_vol"):
            if key in new:
                v = numeric(new[key])
                new[key] = v * 100 if v is not None else None
        if "um_side" in row:
            size = numeric(new.get("um_vol"))
            new["um_signed_shares"] = (
                (size if row["um_side"] == "b" else -size if row["um_side"] == "s" else None)
                if size is not None
                else None
            )
        records.append(new)
    columns = tuple(
        replace(c, label=c.label.replace("（手）", "（股）"), unit="股")
        if c.key in ("auc_vol", "um_vol")
        else c
        for c in table.columns
    )
    if any(c.key == "um_side" for c in columns):
        columns += (ResearchColumn("um_signed_shares", "未匹配买正卖负（股）", "股"),)
    return table.with_records(records, columns=columns)


class SpecialtyResearchService:
    def __init__(self, provider: ResearchProvider):
        self.provider = provider

    def run(self, request: ResearchRequest):
        original_request = request
        if request.cutoff and (
            not re.fullmatch(r"\d{6}", request.cutoff) or clock_seconds(request.cutoff) is None
        ):
            raise ValueError("曲线截至时间格式无效")
        day = datetime.strptime(request.day, "%Y%m%d").date()
        if day > datetime.now(SHANGHAI).date():
            raise ValueError("不能查询未来交易日")
        if request.latest:
            today = datetime.now(SHANGHAI).date()
            calendar = TradingCalendar()
            day = today if calendar.is_session(today) else calendar.shift(today, -1)
        context_day = day.strftime("%Y%m%d")
        if request.symbol:
            request = replace(request, symbol=research_stock_symbol(request.symbol)[-6:])
        if request.module not in dict(MODULES) or request.level not in ("parent", "child"):
            raise ValueError("不支持的研究模块或题材层级")
        tables, notes = [], ["历史数据为当前取得的重建结果；首次采集时间可查，不代表当时已发布。"]

        def fetch(key, params=None, latest=None):
            try:
                table = self.provider.fetch(
                    key, params or {}, request.day, request.latest if latest is None else latest
                )
            except (MarketError, StorageError) as exc:
                message = str(exc)
                describe = getattr(self.provider, "describe", None)
                title, url, source = describe(key) if callable(describe) else (key, "", "数据源")
                state = (
                    "接口迁移／停用"
                    if "410" in message
                    else "无权限／额度"
                    if "403" in message
                    else "暂无快照"
                    if "1002" in message
                    else "不可用"
                )
                table = ResearchTable(
                    key,
                    title + " · " + state,
                    (ResearchColumn("status", "状态"), ResearchColumn("message", "说明")),
                    ((state, message),),
                    ResearchMeta(source, url, "", "", "", datetime.now(SHANGHAI), complete=False),
                    message,
                )
            tables.append(table)
            return table

        dated = {} if request.latest else dict(tradedate=request.day)
        module = request.module
        if module == "rotation":
            if request.action == "detail":
                identity = ThemeIdentity("meoz.jx", request.level, request.code)
                if not re.fullmatch(r"[A-Za-z0-9_-]{1,40}", identity.code):
                    raise ValueError("板块代码格式无效")
                params = dict(theme_symbols=identity.code, level=identity.level)
                if request.level == "parent":
                    minute = fetch("minute", dict(**dated, **params))
                    if "trademin" in {c.key for c in minute.columns}:
                        tables[-1] = interval_changes(minute)
                fetch("flow", params)
                member = fetch("members", params)
                if "symbols" in {c.key for c in member.columns}:
                    expanded = member_rows(member)
                    tables[-1] = expanded
                    symbols = tuple(dict.fromkeys(r["symbol"] for r in expanded.records()))
                    if symbols:
                        try:
                            dated_tables = [t for t in tables if t.key in ("minute", "flow")]
                            days = [
                                t.meta.business_day
                                for t in dated_tables
                                if re.fullmatch(r"\d{8}", t.meta.business_day)
                            ]
                            tables.append(
                                self.provider.quotes(symbols, days[0] if days else context_day)
                            )
                        except (MarketError, StorageError) as exc:
                            notes.append("成份行情不可用：" + str(exc))
                notes.append(
                    "累计净额可正可负；5／15分钟变化仅在同日连续完整桶上计算，午休及缺口不连接。"
                )
            else:
                fetch("rotation", dict(**dated, level=request.level))
            notes.append("精选来源代码不与行业／概念代码合并；板块有重叠，资金不能简单相加。")
        elif module == "evidence":
            if request.action == "detail":
                if not request.code or len(request.code) > 40:
                    raise ValueError("请选择题材库ID")
                detail = fetch("detail", dict(theme_id=request.code))
                if detail.document:
                    tables[-1:] = detail_tables(detail)
            elif request.action == "reason":
                if request.source not in ("xgb", "jygs"):
                    raise ValueError("请选择选股宝／韭菜公社来源")
                params = dict(**dated, source=request.source)
                if request.code:
                    params["symbols"] = request.code
                fetch("reason", params)
            else:
                fetch("library")
                names_table = fetch("rotation", dict(**dated, level="parent"))
                names = {r.get("theme_symbol"): r.get("theme_name") for r in names_table.records()}
                tables.pop()
                for key, tag in (
                    ("members", None),
                    ("authentic", "authentic"),
                    ("leaders", "long"),
                ):
                    params = dict(level="parent")
                    if tag:
                        params["tag"] = tag
                    if key == "leaders":
                        params.update(dated)
                    table = fetch(key, params)
                    if "symbols" in {c.key for c in table.columns}:
                        tables[-1] = with_theme_names(
                            member_rows(table, request.symbol, key == "leaders"), names
                        )
            notes.extend(
                (
                    "题材库与正宗标签只提供当前截面；历史模式只读所选日期之前本机已采集的版本。",
                    "龙排名按完整组展开后过滤，保留原始名次与并列；通常15:40发布，并非盘中排行。",
                    "题材关联和三方说明不能证明订单、收入或业务占比。",
                )
            )
        elif module == "sentiment":
            if request.action == "detail":
                if not request.symbol:
                    raise ValueError("请先填写或双击股票")
                fetch("events", dict(**dated, symbols=request.symbol))
                events = tables[-1]
                if "event" in {c.key for c in events.columns}:
                    labels = {
                        1: "冲击涨停",
                        2: "连续涨停封板60秒",
                        7: "冲击跌停",
                        8: "连续跌停封板60秒",
                    }
                    tables[-1] = events.with_records(
                        [
                            dict(r, event_label=labels.get(r.get("event"), "保留源编号"))
                            for r in events.records()
                        ],
                        columns=(*events.columns, ResearchColumn("event_label", "已核对事件说明")),
                    )
                fetch("seal", dict(**dated, symbol=request.symbol))
                notes.append(
                    "封单快照仅涨停侧；封单减少不是已确认撤单；12秒减少比例分母为当前封单额。"
                )
            else:
                fetch("emotion", dated)
                fetch("pool", dict(**dated, type=["u", "d", "ub", "db"]))
                fetch("yesterday", dict(**dated, type="u"))
                notes.append(
                    "昨日池使用查询日参数；今日表现为查询日字段。涨停情绪含供应商ST等筛选口径。"
                )
        elif module == "auction":
            if request.action == "themes":
                fetch("theme_auction", dated)
            else:
                if not request.symbol:
                    raise ValueError("请填写竞价研究股票代码")
                end = day.strftime("%Y%m%d")
                start = (day - timedelta(days=16)).strftime("%Y%m%d")
                summary = fetch(
                    "auction", dict(symbols=request.symbol, startdate=start, enddate=end), True
                )
                if "auc_vol" in {c.key for c in summary.columns}:
                    summary = auction_shares(summary)
                    dates = sorted(
                        {
                            str(r.get("tradedate"))
                            for r in summary.records()
                            if str(r.get("tradedate")) <= end
                        },
                        reverse=True,
                    )[:2]
                    if not dates or dates[0] != end:
                        notes.append("所选日期无竞价指标；前一可用日仅作参考，不冒充当日或昨日。")
                    tables[-1] = summary.with_records(
                        [r for r in summary.records() if str(r.get("tradedate")) in dates]
                    )
                # Each source remains usable even if the daily summary is missing.
                previous = TradingCalendar().shift(day, -1).strftime("%Y%m%d")
                for date in (end, previous):
                    ticks = fetch("auction_ticks", dict(tradedate=date, symbols=request.symbol))
                    if "auc_vol" in {c.key for c in ticks.columns}:
                        ticks = auction_shares(ticks)
                    tables[-1] = replace(
                        ticks, key="auction_ticks_" + date, title=date + " · 竞价快照"
                    )
                fetch(
                    "auction_last",
                    dict(
                        **dated,
                        symbols=request.symbol,
                        asset="stock",
                        trademin="0925",
                        side="before",
                    ),
                )
            notes.extend(
                (
                    "09:25前撮合价／量为竞价指示，之后为开盘价／实际匹配量；竞价量统一为股，最后盘口快照档量按手标示。",
                    "未匹配方向未知保持空值；开盘抢筹幅度相对09:24最后有效价。板块竞价约09:28更新。",
                )
            )
        elif module == "seats":
            if request.action == "aliases":
                fetch("aliases")
            elif request.action == "history":
                if not request.seat:
                    raise ValueError("请双击一个席位查看历史")
                dates = (*TradingCalendar().previous(day, 4), day)
                for session in dates:
                    table = fetch("seats", dict(tradedate=session.strftime("%Y%m%d")), True)
                    if "seat_name" in {c.key for c in table.columns}:
                        tables[-1] = table.with_records(
                            [r for r in table.records() if r.get("seat_name") == request.seat],
                            key="seat_" + session.strftime("%Y%m%d"),
                            title=session.isoformat() + " · 席位记录",
                        )
            else:
                params = dict(dated)
                if request.symbol:
                    params["symbols"] = request.symbol
                fetch("lhb", params)
                fetch("seats", params)
            notes.extend(
                (
                    "买入榜0／卖出榜1；不同上榜原因和方向保留原披露，不重复合计净仓。",
                    "席位不等同唯一账户，净买入不等同持仓变化；无发布时间的记录仅视作盘后披露。",
                )
            )
        elif module == "attention":
            if request.action == "compare":
                observed, source_tables = {}, []
                for platform, label in HOT_PLATFORMS:
                    key = "hot_live" if request.latest else "hot_day"
                    current = fetch(key, dict(type=platform, **dated))
                    source_tables.append(current)
                    tables[-1] = replace(current, key=key + "_" + platform, title=label + " · 热度")
                    for row in current.records():
                        if row.get("s"):
                            observed.setdefault(str(row["s"]), []).append(label)
                records = [
                    dict(symbol=s, platform_count=len(platforms), platforms="、".join(platforms))
                    for s, platforms in observed.items()
                    if len(platforms) >= 2
                ]
                records.sort(key=lambda r: (-r["platform_count"], r["symbol"]))
                template = source_tables[0]
                comparison = template.with_records(
                    records,
                    key="coappearance",
                    title="多平台共现（本机计算）",
                    columns=(
                        ResearchColumn("symbol", "股票代码"),
                        ResearchColumn("platform_count", "出现平台数"),
                        ResearchColumn("platforms", "平台"),
                    ),
                )
                comparison = replace(
                    comparison,
                    meta=replace(
                        template.meta,
                        source="本机计算／四平台热度",
                        source_updated_at="",
                        published_at="",
                        revision="",
                        complete=all(t.meta.complete for t in source_tables),
                    ),
                    notice="按各来源已取得名单交集计算；源时间可能不同，非同一时刻的全市场热度排名。",
                )
                tables.insert(0, comparison)
                notes.append(
                    "共现以各来源自身名单为样本；缺失平台不当作未上榜，各平台原始表及时间单独保留。"
                )
                return finish_bundle(original_request, tables, notes)
            params = dict(type=request.platform)
            params.update(dated)
            hot = fetch("hot_live" if request.latest else "hot_day", params)
            if request.latest and "s" in {c.key for c in hot.columns}:
                try:
                    previous = self.provider.previous("hot_live", params, hot)
                except StorageError:
                    previous = None
                old = {r.get("s") for r in previous.records()} if previous else None
                records = [
                    dict(
                        r,
                        observed_status=(
                            "首次采集，无法比较"
                            if old is None
                            else "仍在名单"
                            if r.get("s") in old
                            else "新进入本机观察"
                        ),
                    )
                    for r in hot.records()
                ]
                tables[-1] = hot.with_records(
                    records,
                    columns=(*hot.columns, ResearchColumn("observed_status", "相对上个本机版本")),
                )
            elif not request.latest and "rank" in {c.key for c in hot.columns}:
                previous_day = TradingCalendar().shift(day, -1).strftime("%Y%m%d")
                previous = fetch("hot_day", dict(type=request.platform, tradedate=previous_day))
                tables[-1] = replace(previous, key="hot_previous", title=previous_day + " · 前日榜")
                ranks = {r.get("s"): r.get("rank") for r in previous.records()}
                records = []
                for row in hot.records():
                    prior, current = numeric(ranks.get(row.get("s"))), numeric(row.get("rank"))
                    records.append(
                        dict(
                            row,
                            rank_change=prior - current
                            if prior is not None and current is not None
                            else None,
                        )
                    )
                tables[0] = hot.with_records(
                    records,
                    columns=(
                        *hot.columns,
                        ResearchColumn("rank_change", "较前一交易日排名变化（正为上升）"),
                    ),
                )
            fetch("wind", dated)
            stock_params = dict(symbols=request.symbol) if request.symbol else {}
            fetch("monitor", dict(**stock_params, enddate=context_day))
            fetch("anomaly", dict(**stock_params, **dated))
            notes.extend(
                (
                    "最新热搜仅名单／涨幅，无官方rank；新进入只相对本机已采集版本。日度模式才展示排名。",
                    "监管记录与供应商异动候选分别列出；候选未来触发价假设基准指数涨幅为0，非交易所认定。",
                    "最强风口最多30只，为供应商榜单；无记录不等于不存在监管事项。",
                )
            )
        return finish_bundle(original_request, tables, notes)


def finish_bundle(request, tables, notes):
    if request.cutoff:
        cutoff = clock_seconds(request.cutoff)
        filtered = []
        for table in tables:
            keys = {c.key for c in table.columns}
            if keys.intersection(("time", "trademin")):
                rows = [
                    r
                    for r in table.records()
                    if (
                        clock_seconds(r.get("time") or r.get("trademin")) is not None
                        and clock_seconds(r.get("time") or r.get("trademin")) <= cutoff
                    )
                ]
                table = replace(
                    table.with_records(rows),
                    notice=table.notice
                    + f"；仅保留曲线时间≤{request.cutoff[:2]}:{request.cutoff[2:4]}。",
                )
            filtered.append(table)
        tables = filtered
        notes.append("曲线按时点过滤；日度汇总／榜单另列，不能视为该时点已经可得的回测样本。")
    return ResearchBundle(request, tuple(tables), tuple(notes))


def export_research(bundle, path):
    from chengchuan.application.evidence_export import export_bundle

    export_bundle(bundle, path, dict(MODULES)[bundle.request.module])
