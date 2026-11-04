"""MeoZ daily history and real minute OHLC shared by twelve chart views."""

import hashlib
import json
from dataclasses import asdict, replace
from datetime import datetime, time, timezone

from chengchuan.application.errors import MarketError
from chengchuan.application.resampling import calendar_bars, minute_bars
from chengchuan.domain.market import Instrument, MarketFrame, Quote, stock_symbol
from chengchuan.domain.models import SHANGHAI, MarketBar, MarketSeries, MarketSnapshot, SeriesKind
from chengchuan.domain.periods import PERIOD_BY_CODE, chart_period
from chengchuan.infrastructure.market_data import TencentMarketProvider
from chengchuan.infrastructure.meoz_client import ApiResult, MeozClient, clean_error

DAILY_FIELDS = "tradedate,symbol,name,open,high,low,close,pre_close,vol,amount"
MINUTE_FIELDS = "tradedate,symbol,trademin,time,open,high,low,close,vol,amount,vwap"


def matches_symbol(value, symbol):
    text = str(value).strip().lower()
    if "." in text:
        code, exchange = text.rsplit(".", 1)
        return exchange in ("sh", "sz") and exchange + code.zfill(6) == symbol
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
                float(row["amount"]) if row.get("amount") is not None else None,
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

    def __init__(self, client=None):
        self.client = client or MeozClient()

    def search(self, query):
        # Name/pinyin search consumes no paid MeoZ calls.
        if self.client.offline:
            raise MarketError("离线模式使用本地已知证券目录")
        return TencentMarketProvider().search(query)

    def fetch(self, symbol, interval):
        try:
            return self._fetch(stock_symbol(symbol), chart_period(interval))
        except (ValueError, KeyError, TypeError, OverflowError) as exc:
            raise MarketError("猫爪行情字段无效或缺失：" + clean_error(exc)) from None

    def _daily(self, symbol, now, freshness, min_fetched):
        archive = self.client.store.daily_history(symbol)
        today = now.strftime("%Y%m%d")
        if archive:
            rows, fetched, checked = archive
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
            result = self.client.query(
                "daily",
                dict(symbols=symbol[2:], startdate=start, enddate=today),
                DAILY_FIELDS,
                max_age=freshness,
                min_fetched=min_fetched,
            )
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

    def _fetch(self, symbol, period):
        now = datetime.now(SHANGHAI)
        after_close = now.time() >= time(15, 15)
        freshness = 180 if now.weekday() < 5 and time(9, 15) <= now.time() < time(15, 15) else 86400
        min_fetched = (
            now.replace(hour=15, minute=15, second=0, microsecond=0).timestamp()
            if after_close
            else None
        )
        daily = self._daily(symbol, now, freshness, min_fetched)
        daily_bars, skipped = parse_rows(daily.rows, symbol)
        if not daily_bars:
            raise MarketError("猫爪尚无该股票有效日线")
        daily_rows = sorted(daily.rows, key=lambda r: str(r["tradedate"]))
        last_day = daily_bars[-1].timestamp.strftime("%Y%m%d")
        last_row = next(r for r in reversed(daily_rows) if str(r["tradedate"]) == last_day)
        name = next((str(r["name"]) for r in reversed(daily_rows) if r.get("name")), symbol[2:])
        results, notes = [daily], ["不复权；成交量由手统一为股，成交额为元。"]
        if skipped:
            notes.append(f"日线有{skipped}条缺少有效价量，未补造记录。")
        vwap, baseline, quote = (), None, None
        if period.daily:
            bars = daily_bars if period.code == "day" else calendar_bars(daily_bars, period.code)
            kind = SeriesKind.OHLC
            if period.code != "day":
                notes.append("由已取得真实日线按自然周期聚合；当前周期结束前末根仍会变化。")
        else:
            if period.code == "minute":
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
                params = dict(symbols=symbol[2:], period="1m")
                if historical:
                    params["tradedate"] = day
                try:
                    result = self.client.query(
                        "minute",
                        params,
                        MINUTE_FIELDS,
                        service="history" if historical else "live",
                        max_age=None if historical else freshness,
                        min_fetched=None if historical else min_fetched,
                    )
                except MarketError:
                    if not self.client.offline or not historical:
                        raise
                    result = self.client.query(
                        "minute", dict(symbols=symbol[2:], period="1m"), MINUTE_FIELDS
                    )
                results.append(result)
                if len(result.rows) >= 6000:
                    raise MarketError("分钟响应达到6000行截断边界，拒绝当成完整数据")
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
        revision = hashlib.sha256(
            json.dumps([asdict(series), vwap, baseline], default=str, sort_keys=True).encode()
        ).hexdigest()
        snapshot = MarketSnapshot(
            revision, series, quality_notes=tuple(notes), source="猫爪／猫头鹰数据"
        )
        usage = self.client.store.usage()
        calls = "、".join(f"{api} {n}/95" for api, n in usage["interfaces"].items())
        notice = f"本机今日请求{usage['total']}次（含失败预留）；{calls}。计数不含其他客户端。"
        notice += " 自动刷新最短复用180秒缓存；盘后确认后当日缓存复用。"
        warnings = tuple(dict.fromkeys(r.notice for r in results if r.notice))
        if warnings:
            notice += " " + "；".join(warnings)
        if not period.daily:
            notice += " 分钟历史最近5交易日，均价线为源提供的当日累计VWAP。"
            if missing_dates:
                notice += " 分钟未发布／无记录：" + "、".join(missing_dates)
        return MarketFrame(
            snapshot,
            quote,
            max(r.fetched_at for r in results).astimezone(timezone.utc),
            cached=all(r.cached for r in results),
            notice=notice,
            sources=tuple(dict.fromkeys(r.url for r in results)),
            vwap_points=vwap,
            baseline_price=baseline,
            provider_name="猫爪／猫头鹰",
        )
