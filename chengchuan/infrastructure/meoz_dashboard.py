"""MeoZ dashboard queries. Optional statistics never prevent chart display."""

from datetime import datetime, time

from chengchuan.application.errors import MarketError
from chengchuan.application.market_refresh import cache_confirmation, market_checkpoint
from chengchuan.application.quote_metrics import (
    aggregate_breadth,
    base_metrics,
    number,
    volume_ratio,
)
from chengchuan.domain.calendar import CalendarUnavailable
from chengchuan.domain.market import INDEX_CATALOG, INDEX_NAMES, is_index, market_code
from chengchuan.domain.market_dashboard import (
    IndexSummary,
    MarketBreadth,
    MarketOverview,
    QuoteDetails,
    QuoteMetric,
)
from chengchuan.domain.models import SHANGHAI
from chengchuan.infrastructure.meoz_market import (
    INDEX_DAILY_FIELDS,
    index_amount,
    matches_symbol,
    parse_rows,
)

VALUATION_FIELDS = "tradedate,symbol,name,code,close,turnover_rate,turnover_rate_f,volume_ratio,pe_ttm,pb,total_mv,circ_mv"
FLOW_FIELDS = "tradedate,symbol,name,main_net_amount,main_buy_amount,main_sell_amount"
SCREENING_FIELDS = "tradedate,symbol,code,name,pct_chg,main_net_amount,amount,close"
EMOTION_FIELDS = "tradedate,s2,s6,s10,am,mf_main"
INDEX_SYMBOLS = ",".join(symbol[-6:] for symbol, _, _ in INDEX_CATALOG)


def snapshot_time(row):
    raw = str(row.get("servertime") or "")
    day = datetime.strptime(str(row["tradedate"]), "%Y%m%d").date()
    if not raw:
        return None
    try:
        if raw.isdigit() and len(raw) in (10, 13):
            stamp = datetime.fromtimestamp(int(raw) / (1000 if len(raw) == 13 else 1), SHANGHAI)
        elif raw.isdigit() and len(raw) == 14:
            stamp = datetime.strptime(raw, "%Y%m%d%H%M%S").replace(tzinfo=SHANGHAI)
        else:
            stamp = datetime.combine(day, time.fromisoformat(raw), SHANGHAI)
        return stamp if stamp.date() == day else None
    except (ValueError, OverflowError, OSError):
        return None


def cache_policy():
    now = datetime.now(SHANGHAI)
    age = 180 if now.weekday() < 5 and time(9, 15) <= now.time() < time(15, 45) else 86400
    confirmation = cache_confirmation(now)
    minimum = confirmation.timestamp() if confirmation else None
    return now, age, minimum


def latest_query_age(
    client,
    api,
    params,
    fields,
    now,
    age,
    minimum,
    *,
    expected=None,
    min_clock=None,
    symbol=None,
    service="live",
):
    """A late-published previous-day response must not freeze for a full day."""
    if age is not None and age <= 180:
        return age
    try:
        point = market_checkpoint(now)
        expected = expected or point.cutoff.date().strftime("%Y%m%d")
    except CalendarUnavailable:
        return 180
    cached = client.peek(api, params, fields, service=service)
    if (
        cached is None
        or not cached.rows
        or any(str(r.get("tradedate")) != expected for r in cached.rows)
    ):
        return 180
    if minimum is not None and cached.fetched_at.timestamp() < minimum:
        return 180
    if min_clock is not None:
        rows = [r for r in cached.rows if symbol is None or matches_symbol(r.get("symbol"), symbol)]
        clocks = [str(r.get("trademin", "")).zfill(4) for r in rows]
        if (
            not rows
            or any(len(t) != 4 or not t.isdigit() for t in clocks)
            or max(clocks) < min_clock
        ):
            return 180
    if api == "indexdaily" and not point.live:
        if any(
            snapshot_time(r) is not None and snapshot_time(r) < point.cutoff for r in cached.rows
        ):
            return 180
    return age


class MeozDashboard:
    def __init__(self, provider):
        self.provider = provider
        self.client = provider.client

    def overview(self):
        now, age, minimum = cache_policy()
        params = dict(symbols=INDEX_SYMBOLS)
        age = latest_query_age(
            self.client, "indexdaily", params, INDEX_DAILY_FIELDS, now, age, minimum
        )
        result = self.client.query(
            "indexdaily",
            params,
            INDEX_DAILY_FIELDS,
            max_age=age,
            min_fetched=minimum,
        )
        if not result.rows:
            raise MarketError("指数摘要暂未发布")
        indices = []
        seen = set()
        for row in result.rows:
            symbol = next(
                (s for s, _, _ in INDEX_CATALOG if matches_symbol(row["symbol"], s)), None
            )
            if symbol is None or symbol in seen:
                raise MarketError("指数摘要返回未知或重复证券")
            seen.add(symbol)
            day = datetime.strptime(str(row["tradedate"]), "%Y%m%d").date()
            indices.append(
                IndexSummary(
                    symbol,
                    str(row.get("name") or INDEX_NAMES[symbol]),
                    day,
                    number(row.get("close")),
                    number(row.get("pre_close")),
                    index_amount(row),
                    snapshot_time(row),
                )
            )
        day = max(i.day for i in indices)
        breadth, extra, warnings = self._breadth(day, age, minimum)
        results = [result, *extra]
        warnings.append(result.notice)
        return MarketOverview(
            tuple(indices),
            breadth,
            max(r.fetched_at for r in results),
            all(r.cached for r in results),
            "；".join(dict.fromkeys(filter(None, warnings))),
            complete=len(indices) == len(INDEX_CATALOG)
            and len(breadth) > 1
            and (minimum is None or all(r.fetched_at.timestamp() >= minimum for r in results)),
        )

    def _breadth(self, day, age, minimum):
        results, warnings, breadth = [], [], ()
        historical = day < datetime.now(SHANGHAI).date()
        if historical:
            age = None
            minimum = max(minimum or 0, datetime.combine(day, time(15, 15), SHANGHAI).timestamp())
        try:
            market = self.client.query(
                "screening",
                dict(tradedate=day.strftime("%Y%m%d")),
                SCREENING_FIELDS,
                max_age=age,
                min_fetched=minimum,
            )
            if len(market.rows) >= 6000:
                raise MarketError("全市场响应达到6000行边界，拒绝将截断样本作为全市场")
            if historical and market.fetched_at.timestamp() < minimum:
                raise MarketError("历史市场统计仅缓存了盘中值，未确认收盘结果")
            breadth = aggregate_breadth(market.rows, day)
            if not breadth:
                raise MarketError("暂无市场涨跌统计")
            results.append(market)
            warnings.append(market.notice)
        except (MarketError, ValueError) as error:
            warnings.append("市场分组统计暂缺：" + str(error))
            try:
                market = self.client.query(
                    "emoindic_daily",
                    dict(tradedate=day.strftime("%Y%m%d")),
                    EMOTION_FIELDS,
                    max_age=age,
                    min_fetched=minimum,
                )
                rows = [r for r in market.rows if str(r.get("tradedate")) == day.strftime("%Y%m%d")]
                if historical and market.fetched_at.timestamp() < minimum:
                    raise MarketError("历史汇总仅缓存了盘中值，未确认收盘结果")
                if len(rows) == 1 and all(
                    number(rows[0].get(k)) is not None for k in ("s2", "s6", "s10")
                ):
                    row = rows[0]
                    if any(not float(row[k]).is_integer() for k in ("s2", "s6", "s10")):
                        raise MarketError("市场家数包含非整数值")
                    up, down, flat = (int(row[k]) for k in ("s2", "s6", "s10"))
                    breadth = (
                        MarketBreadth(
                            "all",
                            "全市场（情绪源汇总）",
                            day,
                            up,
                            down,
                            flat,
                            0,
                            up + down + flat,
                            number(row.get("mf_main")),
                            None,
                        ),
                    )
                    results.append(market)
            except (MarketError, ValueError, TypeError, KeyError) as error:
                warnings.append("全市场汇总暂缺：" + str(error))
        return breadth, results, warnings

    def details(self, symbol, as_of=None):
        now, age, minimum = cache_policy()
        daily = (
            self.provider._daily(symbol, now, age, minimum)
            if as_of is None or as_of == now.date()
            else self.provider._daily_at_date(symbol, as_of)
        )
        bars, _ = parse_rows(daily.rows, symbol)
        if not bars:
            raise MarketError("没有该日期的行情摘要")
        day = bars[-1].timestamp.astimezone(SHANGHAI).date()
        if as_of is not None and day < now.date():
            minimum = None
        rows = [r for r in daily.rows if str(r["tradedate"]) == day.strftime("%Y%m%d")]
        if len(rows) != 1:
            raise MarketError("日行情摘要日期重复")
        row = dict(rows[0])
        if is_index(symbol):
            row["amount"] = index_amount(row)
        note = f"数据日期 {day:%Y-%m-%d}；取得于 {daily.fetched_at.astimezone(SHANGHAI):%Y-%m-%d %H:%M:%S}"
        metrics = base_metrics(
            symbol, row, "猫爪指数日线" if is_index(symbol) else "猫爪日线", note
        )
        results = [daily]
        warnings = [daily.notice]
        completed = (
            day < now.date()
            or day == now.date()
            and now.time() >= time(15, 15)
            and daily.fetched_at.astimezone(SHANGHAI)
            >= now.replace(hour=15, minute=15, second=0, microsecond=0)
        )
        breadth = ()
        complete = True
        if is_index(symbol):
            observed = snapshot_time(row)
            if day == now.date() and observed is not None:
                completed = completed and observed.time() >= time(15)
            metrics.append(
                QuoteMetric(
                    "volume_ratio",
                    "量比（计算）",
                    volume_ratio(bars, observed, completed),
                    "倍",
                    "指数日线计算",
                    "当前每分钟量/前5个已取得交易日每分钟均量；不足5日前序或无有效时点则缺失；"
                    + note,
                )
            )
            metrics.append(
                QuoteMetric(
                    "turnover_rate",
                    "换手率",
                    None,
                    "%",
                    note="当前指数接口未提供换手率，不套用个股股本计算",
                )
            )
            breadth, extra, issues = self._breadth(day, age, minimum)
            results.extend(extra)
            warnings.extend(issues)
            complete = len(breadth) > 1
            if row.get("amount") is None:
                warnings.append(
                    "历史指数日线成交额单位存在差异，该字段暂不显示；分钟成交额按元保留"
                )
        else:
            valuation = self._optional_row(
                "valuation",
                symbol,
                day,
                VALUATION_FIELDS,
                results,
                warnings,
                age,
                minimum,
            )
            complete = bool(valuation)
            valuation_note = valuation.get("_provenance", "")
            for key, label, unit in (
                ("volume_ratio", "量比", "倍"),
                ("turnover_rate", "换手率", "%"),
                ("turnover_rate_f", "自由流通换手", "%"),
                ("pe_ttm", "市盈率TTM", "倍"),
                ("pb", "市净率", "倍"),
                ("total_mv", "总市值", "元"),
                ("circ_mv", "流通市值", "元"),
            ):
                metrics.append(
                    QuoteMetric(
                        key,
                        label,
                        number(valuation.get(key)),
                        unit,
                        "猫爪估值指标",
                        "同交易日来源值；换手率与自由流通换手率分开；" + valuation_note
                        if valuation
                        else "数据源无可用值或当前权限未提供",
                    )
                )
            flow = self._optional_row(
                "fundflow_kp", symbol, day, FLOW_FIELDS, results, warnings, age, minimum
            )
            complete = complete and bool(flow)
            flow_note = flow.get("_provenance", "")
            for key, field, label in (
                ("main_net", "main_net_amount", "主力净额"),
                ("main_in", "main_buy_amount", "主力买入"),
                ("main_out", "main_sell_amount", "主力卖出"),
            ):
                value = number(flow.get(field))
                metrics.append(
                    QuoteMetric(
                        key,
                        label,
                        abs(value) if key == "main_out" and value is not None else value,
                        "元",
                        "猫爪KPL资金分类",
                        "源主力买卖分类，非真实机构持仓；卖出额显示绝对值；" + flow_note
                        if flow
                        else "该日期暂无资金值或权限未提供",
                    )
                )
        confirmation = cache_confirmation(now)
        if as_of is None and confirmation and day == confirmation.date():
            complete = complete and all(r.fetched_at >= confirmation for r in results)
        return QuoteDetails(
            symbol,
            day,
            tuple(metrics),
            max(r.fetched_at for r in results),
            as_of,
            all(r.cached for r in results),
            "；".join(dict.fromkeys(filter(None, warnings))),
            breadth,
            complete,
        )

    def _optional_row(self, api, symbol, day, fields, results, warnings, age, minimum):
        try:
            historical = day < datetime.now(SHANGHAI).date()
            if historical:
                minimum = max(
                    minimum or 0, datetime.combine(day, time(15, 15), SHANGHAI).timestamp()
                )
            result = self.client.query(
                api,
                dict(symbols=market_code(symbol)[2:], tradedate=day.strftime("%Y%m%d")),
                fields,
                max_age=None if historical else age,
                min_fetched=minimum,
            )
            if historical and result.fetched_at.timestamp() < minimum:
                raise MarketError("历史指标仅缓存了盘中值，未确认收盘结果")
            if len(result.rows) > 1 or any(
                not matches_symbol(r.get("code") or r.get("symbol"), symbol)
                or str(r.get("tradedate")) != day.strftime("%Y%m%d")
                for r in result.rows
            ):
                raise MarketError("指标返回其他证券或日期，已拒绝使用")
            results.append(result)
            warnings.append(result.notice)
            return (
                dict(
                    result.rows[0],
                    _provenance=f"数据日期 {day:%Y-%m-%d}；取得于 {result.fetched_at.astimezone(SHANGHAI):%Y-%m-%d %H:%M:%S}；{result.notice}",
                )
                if result.rows
                else {}
            )
        except (MarketError, ValueError, TypeError, KeyError, OverflowError) as error:
            warnings.append(api + "暂缺：" + str(error))
            return {}
