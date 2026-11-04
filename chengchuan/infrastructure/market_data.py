"""腾讯公开行情适配。协议不提供延迟保证；不推算缺失成交额或分钟OHLC。"""

import hashlib
import json
import re
from dataclasses import asdict
from datetime import datetime, timezone
from urllib.parse import urlencode
from urllib.request import Request, urlopen

from chengchuan.application.errors import MarketError
from chengchuan.application.quote_metrics import base_metrics, number
from chengchuan.domain.market import (
    INDEX_CATALOG,
    Instrument,
    MarketFrame,
    Quote,
    instrument_symbol,
    market_code,
    stock_symbol,
)
from chengchuan.domain.market_dashboard import (
    IndexSummary,
    MarketOverview,
    QuoteDetails,
    QuoteMetric,
)
from chengchuan.domain.models import SHANGHAI, MarketBar, MarketSeries, MarketSnapshot, SeriesKind
from flowradar.data import parse_minute_days


def http_text(url, encoding="utf-8"):
    request = Request(url, headers={"User-Agent": "Mozilla/5.0", "Referer": "https://gu.qq.com/"})
    with urlopen(request, timeout=6) as response:
        raw = response.read(4_000_001)
    if len(raw) > 4_000_000:
        raise MarketError("行情响应超出4MB限制")
    return raw.decode(encoding)


def parse_search(raw):
    match = re.search(r'v_hint="([^"\r\n]*)"', raw)
    if not match:
        raise ValueError("搜索响应格式变化")
    # Only decode JSON's Unicode escapes, never execute a JavaScript response.
    value = json.loads('"' + match[1] + '"')
    items = {}
    for row in value.split("^"):
        fields = row.split("~")
        if len(fields) < 5 or fields[0] not in ("sh", "sz") or fields[4] != "GP-A":
            continue
        try:
            item = Instrument(stock_symbol(fields[0] + fields[1]), fields[2], fields[3])
        except ValueError:
            continue
        items[item.symbol] = item
    return tuple(items.values())[:20]


def parse_quote(raw, symbol):
    match = re.search(r"v_" + re.escape(market_code(symbol)) + r'="([^"\r\n]*)"', raw)
    if not match:
        raise ValueError("没有该证券的有效报价")
    fields = match[1].split("~")
    if len(fields) < 31 or fields[2] != symbol[-6:]:
        raise ValueError("报价格式或证券代码不一致")
    return Quote(
        Instrument(symbol, fields[1]),
        float(fields[3]),
        float(fields[4]),
        datetime.strptime(fields[30], "%Y%m%d%H%M%S").replace(tzinfo=SHANGHAI),
    )


def parse_quote_details(raw, symbol):
    quote = parse_quote(raw, symbol)
    match = re.search(r"v_" + re.escape(market_code(symbol)) + r'="([^"\r\n]*)"', raw)
    fields = match[1].split("~")

    def field(index):
        return number(fields[index]) if len(fields) > index else None

    amount = field(57) if field(57) is not None else field(37)
    row = dict(
        close=quote.price,
        pre_close=quote.previous_close,
        open=field(5),
        high=field(33),
        low=field(34),
        vol=field(6),
        amount=amount * 10000 if amount is not None else None,
    )
    note = f"报价时间 {quote.timestamp:%Y-%m-%d %H:%M:%S}；腾讯公开报价，可能延迟"
    metrics = base_metrics(symbol, row, "腾讯公开报价", note)
    for key, label, unit, index, scale in (
        ("volume_ratio", "量比", "倍", 49, 1),
        ("turnover_rate", "换手率", "%", 38, 1),
        ("pe_ttm", "市盈率TTM", "倍", 39, 1),
        ("pb", "市净率", "倍", 46, 1),
        ("circ_mv", "流通市值", "元", 44, 1e8),
        ("total_mv", "总市值", "元", 45, 1e8),
    ):
        value = field(index)
        metrics.append(
            QuoteMetric(
                key,
                label,
                value * scale if value is not None and value >= 0 else None,
                unit,
                "腾讯公开报价",
                note,
            )
        )
    return QuoteDetails(
        symbol,
        quote.timestamp.astimezone(SHANGHAI).date(),
        tuple(metrics),
        datetime.now(timezone.utc),
    )


class TencentMarketProvider:
    supports_overview = True
    supports_details = True

    def fetch_overview(self):
        raw = http_text(
            "https://qt.gtimg.cn/q=" + ",".join(market_code(s) for s, _, _ in INDEX_CATALOG),
            "gb18030",
        )
        indices = []
        issues = []
        for symbol, name, _ in INDEX_CATALOG:
            try:
                quote = parse_quote(raw, symbol)
                details = parse_quote_details(raw, symbol)
                amount = next(m.value for m in details.metrics if m.key == "amount")
                indices.append(
                    IndexSummary(
                        symbol,
                        name,
                        quote.timestamp.astimezone(SHANGHAI).date(),
                        quote.price,
                        quote.previous_close,
                        amount,
                        quote.timestamp,
                    )
                )
            except (ValueError, TypeError, KeyError):
                issues.append(name + "暂无报价")
        if not indices:
            raise MarketError("指数摘要暂不可用")
        return MarketOverview(
            tuple(indices), (), datetime.now(timezone.utc), notice="；".join(issues)
        )

    def fetch_details(self, symbol, as_of=None):
        symbol = instrument_symbol(symbol)
        if as_of is not None:
            raise MarketError("腾讯基础源不支持补取历史指标，请使用猫爪历史数据")
        try:
            return parse_quote_details(
                http_text("https://qt.gtimg.cn/q=" + market_code(symbol), "gb18030"), symbol
            )
        except (OSError, ValueError, TypeError, KeyError, IndexError):
            raise MarketError("详细行情暂不可用") from None

    def search(self, query):
        try:
            url = "https://smartbox.gtimg.cn/s3/?" + urlencode({"q": query, "t": "all"})
            return parse_search(http_text(url, "gb18030"))
        except (OSError, ValueError, KeyError, TypeError, IndexError, AttributeError) as exc:
            raise MarketError("股票搜索暂不可用，请输入六位代码重试") from exc

    def fetch(self, symbol, interval):
        symbol = instrument_symbol(symbol)
        raw_symbol = market_code(symbol)
        if interval not in ("day", "minute"):
            raise ValueError("不支持的行情周期")
        quote, warning = None, ""
        try:
            quote = parse_quote(http_text("https://qt.gtimg.cn/q=" + raw_symbol, "gb18030"), symbol)
        except (OSError, ValueError, KeyError, TypeError, IndexError, AttributeError) as exc:
            warning = "最新报价未取得；图表末价不代表当前报价"
            quote_error = exc
        try:
            if interval == "day":
                url = "https://web.ifzq.gtimg.cn/appstock/app/fqkline/get?" + urlencode(
                    {"param": f"{raw_symbol},day,,,320,"}
                )
            else:
                url = "https://web.ifzq.gtimg.cn/appstock/app/day/query?" + urlencode(
                    {"code": raw_symbol}
                )
            payload = json.loads(http_text(url))
            obj = payload.get("data", {}).get(raw_symbol, {})
            name = (
                quote.instrument.name
                if quote
                else obj.get("qt", {}).get(raw_symbol, ["", raw_symbol])[1]
            )
            if interval == "day":
                bars = tuple(
                    MarketBar(
                        datetime.fromisoformat(row[0] + "T15:00:00").replace(tzinfo=SHANGHAI),
                        float(row[2]),
                        float(row[5]) * 100,
                        open=float(row[1]),
                        high=float(row[3]),
                        low=float(row[4]),
                    )
                    for row in obj.get("day", [])
                )
                notes = (
                    "日K为不复权价格；当前交易日尚未收盘时最后一根仍会变化。",
                    "此日K源未提供成交额，不计算成交额和VWAP。",
                )
            else:
                # The provider lists newest days first; cumulative values reset per day.
                rows = sorted(parse_minute_days(payload, raw_symbol), key=lambda b: b.timestamp)
                bars = tuple(
                    MarketBar(
                        datetime.fromisoformat(b.timestamp).replace(tzinfo=SHANGHAI),
                        b.close,
                        b.volume_shares,
                        b.amount_yuan,
                    )
                    for b in rows
                )
                notes = ("分时为分钟末采样价，不是真实分钟OHLC；仅覆盖源提供的近期交易日。",)
            series = MarketSeries(
                symbol,
                name,
                interval,
                SeriesKind.OHLC if interval == "day" else SeriesKind.SAMPLED_CLOSE,
                bars,
            )
            revision = hashlib.sha256(
                json.dumps(asdict(series), default=str, sort_keys=True, allow_nan=False).encode()
            ).hexdigest()
            snapshot = MarketSnapshot(
                revision,
                series,
                quality_notes=notes + ("腾讯公开行情，可能延迟；未获取基准和订单金额分类。",),
                source="腾讯财经 · 公开行情",
            )
            return MarketFrame(
                snapshot,
                quote,
                datetime.now(timezone.utc),
                notice=warning,
                sources=("https://qt.gtimg.cn/q=" + raw_symbol, url),
            )
        except (OSError, ValueError, KeyError, TypeError, IndexError, AttributeError) as exc:
            detail = "请检查网络或证券代码，稍后刷新"
            if quote is None and warning:
                detail += "；报价源也未能连接"
            raise MarketError("行情暂不可用：" + detail) from (
                quote_error if quote is None else exc
            )
