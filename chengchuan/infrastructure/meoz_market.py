"""MeoZ daily history and real minute OHLC shared by twelve chart views."""

import hashlib
import json
from dataclasses import asdict, replace
from datetime import date, datetime, time, timedelta, timezone

from chengchuan.application.errors import MarketError
from chengchuan.application.market_refresh import cache_confirmation, market_checkpoint
from chengchuan.application.resampling import calendar_bars, minute_bars
from chengchuan.domain.calendar import CalendarUnavailable
from chengchuan.domain.market import (
    INDEX_CATALOG,
    INDEX_NAMES,
    Instrument,
    MarketFrame,
    Quote,
    instrument_symbol,
    is_index,
    market_code,
    stock_symbol,
)
from chengchuan.domain.models import SHANGHAI, MarketBar, MarketSeries, MarketSnapshot, SeriesKind
from chengchuan.domain.periods import PERIOD_BY_CODE, chart_period
from chengchuan.infrastructure.market_data import TencentMarketProvider
from chengchuan.infrastructure.meoz_client import ApiResult, MeozClient, clean_error

DAILY_FIELDS = "tradedate,symbol,name,open,high,low,close,pre_close,vol,amount"
MINUTE_FIELDS = "tradedate,symbol,trademin,time,open,high,low,close,vol,amount,vwap"
INDEX_DAILY_FIELDS = (
    "tradedate,symbol,name,open,high,low,close,pre_close,change,pct_chg,vol,amount,servertime"
)
INDEX_MINUTE_FIELDS = "tradedate,symbol,trademin,time,open,high,low,close,vol,amount"
INDEX_BATCH_SYMBOLS = ",".join(s[-6:] for s, _, _ in INDEX_CATALOG)


def daily_contract(symbol):
    return ("indexdaily", INDEX_DAILY_FIELDS) if is_index(symbol) else ("daily", DAILY_FIELDS)


def index_amount(row):
    # Historical bare-code indexdaily records currently disagree with the
    # documented yuan unit (verified against minute totals). Do not silently
    # interpret their raw amount as yuan or infer a universal conversion.
    if "." not in str(row.get("symbol", "")):
        return None
    return float(row["amount"]) if row.get("amount") is not None else None


def matches_symbol(value, symbol):
    text = str(value).strip().lower()
    if "." in text:
        code, exchange = text.rsplit(".", 1)
        return exchange in ("sh", "sz") and exchange + code.zfill(6) == market_code(symbol)
    if is_index(symbol):
        return text.zfill(6) == symbol[-6:]
    return stock_symbol(text.zfill(6)) == symbol


def parse_rows(rows, symbol, intraday=False):
    bars, skipped = [], 0
    for row in rows:
        if not matches_symbol(row["symbol"], symbol):
            raise MarketError("猫爪响应包含其他证券，拒绝显示")
        if any(row.get(k) is None for k in ("open", "high", "low", "close", "vol")):
            skipped += 1
            continue
        if float(row["close"]) <= 0:
            skipped += 1
            continue
        day = datetime.strptime(str(row["tradedate"]), "%Y%m%d")
        clock = str(row["trademin"]).zfill(4) if intraday else "1500"
        stamp = day.replace(hour=int(clock[:2]), minute=int(clock[2:]), tzinfo=SHANGHAI)
        bars.append(
            MarketBar(
                stamp,
                float(row["close"]),
                float(row["vol"]) * 100,
                index_amount(row)
                if is_index(symbol) and not intraday
                else (float(row["amount"]) if row.get("amount") is not None else None),
                float(row["open"]),
                float(row["high"]),
                float(row["low"]),
            )
        )
    bars.sort(key=lambda b: b.timestamp)
    if len({b.timestamp for b in bars}) != len(bars):
        raise MarketError("猫爪返回重复交易时间，拒绝覆盖数据")
    return tuple(bars), skipped


class MeozMarketProvider:
    intervals = tuple(PERIOD_BY_CODE)
    supports_history = True
    supports_overview = True
    supports_details = True
    supports_funds = True
    supports_heatmap = True
    supports_sector = True
    supports_microstructure = True
    supports_book_stream = True

    def __init__(self, client=None):
        self.client = client or MeozClient()

    @property
    def offline(self):
        return self.client.offline

    def fetch_funds(self, symbol):
        if self.client.offline:
            raise MarketError("离线模式只读取已存资金快照")
        from chengchuan.infrastructure.market_views import fetch_public_flow

        return fetch_public_flow(symbol)

    def fetch_heatmap(self, kind):
        from chengchuan.infrastructure.market_views import fetch_heatmap

        return fetch_heatmap(self.client, kind)

    def fetch_sector(self, kind, code, name):
        from chengchuan.infrastructure.sectors import fetch_sector

        return fetch_sector(self.client, kind, code, name)

    def fetch_order_book(self, symbol):
        from chengchuan.infrastructure.microstructure import fetch_book

        return fetch_book(self.client, symbol)

    def fetch_microstructure(self, symbol, day, max_pages=6):
        from chengchuan.infrastructure.microstructure import fetch_tape

        return fetch_tape(self.client, symbol, day, max_pages)

    def stream_order_book(self, symbol, stop):
        if self.client.offline:
            raise MarketError("离线模式不建立实时订阅")
        from chengchuan.infrastructure.meoz_stream import stream_books

        return stream_books(self.client.store, symbol, stop)

    def search(self, query):
        # Name/pinyin search consumes no paid MeoZ calls.
        if self.client.offline:
            raise MarketError("离线模式使用本地已知证券目录")
        return TencentMarketProvider().search(query)

    def fetch(self, symbol, interval):
        try:
            return self._fetch(instrument_symbol(symbol), chart_period(interval))
        except (ValueError, KeyError, TypeError, OverflowError) as exc:
            raise MarketError("猫爪行情字段无效或缺失：" + clean_error(exc)) from None

    def fetch_at_date(self, symbol, interval, as_of):
        if type(as_of) is not date or as_of > datetime.now(SHANGHAI).date():
            raise MarketError("请选择今天或更早的日期")
        try:
            return self._fetch(instrument_symbol(symbol), chart_period(interval), as_of=as_of)
        except (ValueError, KeyError, TypeError, OverflowError) as exc:
            raise MarketError("历史行情字段无效：" + clean_error(exc)) from None

    def _daily_at_date(self, symbol, as_of):
        archive = self.client.store.daily_history(symbol)
        end = as_of.strftime("%Y%m%d")
        latest = max(str(r["tradedate"]) for r in archive[0]) if archive and archive[0] else ""
        complete_after = datetime.combine(as_of, time(15, 15), SHANGHAI).timestamp()
        if (
            archive
            and archive[0]
            and (end < latest or end == latest and archive[1] >= complete_after)
        ):
            rows = tuple(r for r in archive[0] if str(r["tradedate"]) <= end)
            return ApiResult(
                rows,
                datetime.fromtimestamp(archive[1], timezone.utc),
                True,
                "https://sz.meoz.cn:6688/api",
                "复用本地日线定位历史日期",
            )
        result = self._query_daily(symbol, "20000101", end, None, complete_after)
        if any(str(r["tradedate"]) > end for r in result.rows):
            raise MarketError("历史日线返回所选日期之后的数据，拒绝显示")
        parse_rows(result.rows, symbol)
        if result.rows and (not archive or end >= archive[2]):
            self.client.store.save_daily(symbol, result.rows, result.fetched_at.timestamp(), end)
        return result

    def _date_calendar(self, as_of, daily_rows, count=5):
        end = as_of.strftime("%Y%m%d")
        recent = self.client.peek("tradecal", {"recentdays": 6})
        if recent:
            all_days = sorted(str(r["cal_date"]) for r in recent.rows if str(r["is_open"]) == "1")
            covered = [d for d in all_days if d <= end]
            if len(covered) >= count and end <= all_days[-1]:
                return covered[-count:], recent
        try:
            result = self.client.query(
                "tradecal",
                dict(
                    startdate=(as_of - timedelta(days=35)).strftime("%Y%m%d"),
                    enddate=as_of.strftime("%Y%m%d"),
                ),
                max_age=None,
            )
            days = sorted(
                str(r["cal_date"])
                for r in result.rows
                if str(r["is_open"]) == "1" and str(r["cal_date"]) <= end
            )
            if not days:
                raise MarketError("该日期之前暂无可用交易日历")
            return days[-count:], result
        except MarketError:
            if not self.client.offline:
                raise
            days = sorted(str(r["tradedate"]) for r in daily_rows if str(r["tradedate"]) <= end)[
                -count:
            ]
            return days, None

    def _daily(self, symbol, now, freshness, min_fetched):
        archive = self.client.store.daily_history(symbol)
        today = now.strftime("%Y%m%d")
        if archive:
            rows, fetched, checked = archive
            try:
                expected = market_checkpoint(now).cutoff.strftime("%Y%m%d")
                if not rows or max(str(r["tradedate"]) for r in rows) != expected:
                    freshness = min(freshness, 180)
            except CalendarUnavailable:
                freshness = min(freshness, 180)
            fresh = (
                checked == today
                and now.timestamp() - fetched < freshness
                and (min_fetched is None or fetched >= min_fetched)
            )
            if fresh or self.client.offline:
                return ApiResult(
                    rows,
                    datetime.fromtimestamp(fetched, timezone.utc),
                    True,
                    "https://sz.meoz.cn:6688/api",
                    "离线读取本地日线" if self.client.offline else "",
                )
        start = (
            max(str(r["tradedate"]) for r in archive[0]) if archive and archive[0] else "20000101"
        )
        try:
            result = self._query_daily(symbol, start, today, freshness, min_fetched)
            parse_rows(result.rows, symbol)
        except MarketError as exc:
            if not archive:
                raise
            return ApiResult(
                archive[0],
                datetime.fromtimestamp(archive[1], timezone.utc),
                True,
                "https://sz.meoz.cn:6688/api",
                "更新失败，保留本地日线：" + str(exc),
            )
        if result.rows:
            self.client.store.save_daily(symbol, result.rows, result.fetched_at.timestamp(), today)
            rows, fetched, _ = self.client.store.daily_history(symbol)
            return ApiResult(
                rows,
                datetime.fromtimestamp(fetched, timezone.utc),
                result.cached,
                result.url,
                result.notice,
            )
        if archive:
            return ApiResult(
                archive[0],
                datetime.fromtimestamp(archive[1], timezone.utc),
                True,
                result.url,
                "尚无已发布日线更新，保留原日期",
            )
        return result

    def _query_daily(self, symbol, start, end, max_age, min_fetched):
        api, fields = daily_contract(symbol)
        params = dict(symbols=market_code(symbol)[2:], startdate=start, enddate=end)
        if not is_index(symbol):
            return self.client.query(api, params, fields, max_age=max_age, min_fetched=min_fetched)
        if self.client.store.daily_history(symbol):
            recent = self.client.peek(
                "indexdaily", dict(symbols=INDEX_BATCH_SYMBOLS), INDEX_DAILY_FIELDS
            )
            if recent:
                age = datetime.now(timezone.utc).timestamp() - recent.fetched_at.timestamp()
                fresh = (max_age is None or 0 <= age < max_age) and (
                    min_fetched is None or recent.fetched_at.timestamp() >= min_fetched
                )
                rows = tuple(
                    r
                    for r in recent.rows
                    if matches_symbol(r["symbol"], symbol) and start <= str(r["tradedate"]) <= end
                )
                if fresh and rows:
                    return replace(
                        recent, rows=rows, notice=recent.notice or "复用指数摘要更新日线末段"
                    )
        rows, results = [], []
        for page in range(3):
            result = self.client.query(
                api,
                dict(params, limit=6000, offset=page * 6000, order_by="tradedate", order_dir="asc"),
                fields,
                max_age=max_age,
                min_fetched=min_fetched,
            )
            rows.extend(result.rows)
            results.append(result)
            if len(result.rows) < 6000:
                return ApiResult(
                    tuple(rows),
                    max(r.fetched_at for r in results),
                    all(r.cached for r in results),
                    result.url,
                    "；".join(filter(None, (r.notice for r in results))),
                )
        raise MarketError("指数日线超过分页保护范围，未将截断数据作为完整历史")

    def fetch_overview(self):
        from chengchuan.infrastructure.meoz_dashboard import MeozDashboard

        return MeozDashboard(self).overview()

    def fetch_details(self, symbol, as_of=None):
        from chengchuan.infrastructure.meoz_dashboard import MeozDashboard

        return MeozDashboard(self).details(instrument_symbol(symbol), as_of)

    def _fetch(self, symbol, period, as_of=None):
        now = datetime.now(SHANGHAI)
        freshness = 180 if now.weekday() < 5 and time(9, 15) <= now.time() < time(15, 45) else 86400
        confirmation = cache_confirmation(now)
        min_fetched = confirmation.timestamp() if confirmation else None
        daily = (
            self._daily(symbol, now, freshness, min_fetched)
            if as_of is None or as_of == now.date()
            else self._daily_at_date(symbol, as_of)
        )
        daily_bars, skipped = parse_rows(daily.rows, symbol)
        if not daily_bars:
            raise MarketError("所选日期之前尚无该股票有效日线，请选择数据覆盖范围内的日期")
        daily_rows = sorted(daily.rows, key=lambda r: str(r["tradedate"]))
        last_day = daily_bars[-1].timestamp.strftime("%Y%m%d")
        last_row = next(r for r in reversed(daily_rows) if str(r["tradedate"]) == last_day)
        name = next(
            (str(r["name"]) for r in reversed(daily_rows) if r.get("name")),
            INDEX_NAMES.get(symbol, symbol[-6:]),
        )
        results, notes = [daily], ["不复权；成交量由手统一为股，成交额为元。"]
        data_fetched = daily.fetched_at
        if is_index(symbol):
            notes.append("指数点位，非股票价格；指数不启用个股阶段规则或预测。")
            if any(index_amount(r) is None and r.get("amount") is not None for r in daily_rows):
                notes.append(
                    "历史指数日线成交额与文档单位存在差异，未确认记录保持缺失；分钟成交额按元。"
                )
        if skipped:
            notes.append(f"日线有{skipped}条缺少有效价量，未补造记录。")
        vwap, baseline, quote = (), None, None
        if period.daily:
            bars = daily_bars if period.code == "day" else calendar_bars(daily_bars, period.code)
            kind = SeriesKind.OHLC
            if period.code != "day":
                notes.append("由已取得真实日线按自然周期聚合；当前周期结束前末根仍会变化。")
            if is_index(symbol):
                from chengchuan.infrastructure.meoz_dashboard import snapshot_time

                observed = snapshot_time(last_row)
                if observed and last_row.get("pre_close"):
                    quote = Quote(
                        Instrument(symbol, name),
                        float(last_row["close"]),
                        float(last_row["pre_close"]),
                        observed,
                    )
        else:
            if as_of is not None:
                dates, calendar = self._date_calendar(
                    as_of, daily_rows, count=1 if period.code == "minute" else 5
                )
                if calendar:
                    results.append(calendar)
                else:
                    notes.append("离线交易日历未缓存，使用已存日线日期定位。")
                if period.code == "minute":
                    dates = dates[-1:]
            elif period.code == "minute":
                dates = [daily_bars[-1].timestamp.strftime("%Y%m%d")]
            else:
                calendar = self.client.query(
                    "tradecal",
                    {"recentdays": 6},
                    max_age=86400,
                    min_fetched=now.replace(hour=0, minute=0, second=0, microsecond=0).timestamp(),
                )
                results.append(calendar)
                dates = sorted(
                    str(r["cal_date"]) for r in calendar.rows if str(r["is_open"]) == "1"
                )
                dates = [d for d in dates if d <= daily_bars[-1].timestamp.strftime("%Y%m%d")]
                dates = dates[-5:]
            minute_rows = []
            missing_dates = []
            for day in dates:
                historical = day < now.strftime("%Y%m%d")
                params = dict(symbols=market_code(symbol)[2:], period="1m")
                minute_api = "index_minute" if is_index(symbol) else "minute"
                minute_fields = INDEX_MINUTE_FIELDS if is_index(symbol) else MINUTE_FIELDS
                if is_index(symbol):
                    params["symbols"] = INDEX_BATCH_SYMBOLS
                if historical:
                    params["tradedate"] = day
                historical_minimum = (
                    datetime.strptime(day, "%Y%m%d")
                    .replace(hour=15, minute=15, tzinfo=SHANGHAI)
                    .timestamp()
                )
                if confirmation and day == confirmation.strftime("%Y%m%d"):
                    historical_minimum = max(historical_minimum, confirmation.timestamp())
                from chengchuan.infrastructure.meoz_dashboard import latest_query_age

                minute_age = latest_query_age(
                    self.client,
                    minute_api,
                    params,
                    minute_fields,
                    now,
                    None if historical else freshness,
                    historical_minimum if historical else min_fetched,
                    expected=day,
                    min_clock="1500",
                    symbol=symbol,
                    service="history" if historical else "live",
                )
                try:
                    result = self.client.query(
                        minute_api,
                        params,
                        minute_fields,
                        service="history" if historical else "live",
                        max_age=minute_age,
                        min_fetched=historical_minimum if historical else min_fetched,
                    )
                except MarketError:
                    if not self.client.offline or not historical:
                        raise
                    result = self.client.query(
                        minute_api,
                        dict(
                            symbols=INDEX_BATCH_SYMBOLS
                            if is_index(symbol)
                            else market_code(symbol)[2:],
                            period="1m",
                        ),
                        minute_fields,
                    )
                    if any(str(r["tradedate"]) != day for r in result.rows):
                        raise MarketError(f"本地缺少{day}分钟数据，请联网补取") from None
                results.append(result)
                if day == dates[-1]:
                    data_fetched = min(daily.fetched_at, result.fetched_at)
                if len(result.rows) >= 6000:
                    raise MarketError("分钟响应达到6000行截断边界，拒绝当成完整数据")
                if is_index(symbol):
                    if any(
                        not any(matches_symbol(r["symbol"], s) for s, _, _ in INDEX_CATALOG)
                        for r in result.rows
                    ):
                        raise MarketError("指数批次返回未知证券，拒绝拼接")
                    result = replace(
                        result,
                        rows=tuple(r for r in result.rows if matches_symbol(r["symbol"], symbol)),
                    )
                if any(str(r["tradedate"]) != day for r in result.rows):
                    raise MarketError("分钟接口返回日期与请求不一致，拒绝拼接")
                if not result.rows:
                    missing_dates.append(day)
                minute_rows.extend(result.rows)
            raw_bars, dropped = parse_rows(minute_rows, symbol, intraday=True)
            if not raw_bars:
                raise MarketError("所选日期的真实分钟数据尚未发布或没有有效价量")
            if dropped:
                notes.append(f"分钟有{dropped}条缺少真实价量，未补造。")
            if missing_dates:
                notes.append("分钟尚无数据的交易日：" + "、".join(missing_dates))
            notes.append("分钟时间轴采用trademin收盘桶；09:30桶含开盘集合竞价。")
            notes.append("分钟历史默认最近5个市场交易日，切换分钟周期复用相同本地数据。")
            if period.minutes:
                bars = minute_bars(raw_bars, period.minutes)
                kind = SeriesKind.OHLC
                notes.append(
                    "分钟桶按上午／下午独立聚合；缺失分钟不补齐，未完成桶按已取得部分显示。"
                )
            else:
                bars = tuple(replace(b, open=None, high=None, low=None) for b in raw_bars)
                kind = SeriesKind.SAMPLED_CLOSE
                points = []
                for row in minute_rows:
                    if row.get("vwap") is not None and float(row["vwap"]) > 0:
                        day = datetime.strptime(str(row["tradedate"]), "%Y%m%d")
                        clock = str(row["trademin"]).zfill(4)
                        points.append(
                            (
                                day.replace(
                                    hour=int(clock[:2]), minute=int(clock[2:]), tzinfo=SHANGHAI
                                ),
                                float(row["vwap"]),
                            )
                        )
                vwap = tuple(sorted(points))
                baseline = next(
                    (
                        float(r["pre_close"])
                        for r in daily_rows
                        if str(r["tradedate"]) == dates[0] and r.get("pre_close")
                    ),
                    None,
                )
            pre_close = last_row.get("pre_close")
            if pre_close and bars[-1].timestamp.date() == daily_bars[-1].timestamp.date():
                quote = Quote(
                    Instrument(symbol, name), bars[-1].close, float(pre_close), bars[-1].timestamp
                )
        series = MarketSeries(symbol, name, period.code, kind, tuple(bars))
        references = tuple(
            (datetime.strptime(str(r["tradedate"]), "%Y%m%d").date(), float(r["pre_close"]))
            for r in daily_rows
            if r.get("pre_close") is not None and float(r["pre_close"]) > 0
        )
        revision = hashlib.sha256(
            json.dumps(
                [asdict(series), vwap, baseline, references], default=str, sort_keys=True
            ).encode()
        ).hexdigest()
        snapshot = MarketSnapshot(
            revision, series, quality_notes=tuple(notes), source="猫爪／猫头鹰数据"
        )
        usage = self.client.store.usage()
        calls = "、".join(f"{api} {n}/95" for api, n in usage["interfaces"].items())
        notice = f"本次行情取得时本机今日请求{usage['total']}次（含失败预留）；{calls}。最新计数见设置，且不含其他客户端。"
        notice += " 自动刷新最短复用180秒缓存；盘后确认后当日缓存复用。"
        if as_of is not None:
            notice += f" 历史日期{as_of:%Y-%m-%d}；实际图表截至{bars[-1].timestamp:%Y-%m-%d}。"
        warnings = tuple(dict.fromkeys(r.notice for r in results if r.notice))
        if warnings:
            notice += " " + "；".join(warnings)
        if not period.daily:
            notice += (
                " 指数分钟批次共享最近5交易日，未提供指数VWAP。"
                if is_index(symbol)
                else " 分钟历史最近5交易日，均价线为源提供的当日累计VWAP。"
            )
            if missing_dates:
                notice += " 分钟未发布／无记录：" + "、".join(missing_dates)
        return MarketFrame(
            snapshot,
            quote,
            data_fetched.astimezone(timezone.utc),
            cached=all(r.cached for r in results),
            notice=notice,
            sources=tuple(dict.fromkeys(r.url for r in results)),
            vwap_points=vwap,
            baseline_price=baseline,
            provider_name="猫爪／猫头鹰",
            as_of=as_of,
            reference_closes=references,
        )
