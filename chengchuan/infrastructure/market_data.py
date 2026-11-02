"""腾讯公开行情适配。协议不提供延迟保证；不推算缺失成交额或分钟OHLC。"""

import hashlib
import json
import re
from dataclasses import asdict
from datetime import datetime, timezone
from urllib.parse import urlencode
from urllib.request import Request, urlopen

from chengchuan.application.errors import MarketError
from chengchuan.domain.market import Instrument, MarketFrame, Quote, stock_symbol
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
    match = re.search(r"v_" + re.escape(symbol) + r'="([^"\r\n]*)"', raw)
    if not match:
        raise ValueError("没有该证券的有效报价")
    fields = match[1].split("~")
    if len(fields) < 31 or fields[2] != symbol[2:]:
        raise ValueError("报价格式或证券代码不一致")
    return Quote(
        Instrument(symbol, fields[1]),
        float(fields[3]),
        float(fields[4]),
        datetime.strptime(fields[30], "%Y%m%d%H%M%S").replace(tzinfo=SHANGHAI),
    )


class TencentMarketProvider:
    def search(self, query):
        try:
            url = "https://smartbox.gtimg.cn/s3/?" + urlencode({"q": query, "t": "all"})
            return parse_search(http_text(url, "gb18030"))
        except (OSError, ValueError, KeyError, TypeError, IndexError, AttributeError) as exc:
            raise MarketError("股票搜索暂不可用，请输入六位代码重试") from exc

    def fetch(self, symbol, interval):
        symbol = stock_symbol(symbol)
        if interval not in ("day", "minute"):
            raise ValueError("不支持的行情周期")
        quote, warning = None, ""
        try:
            quote = parse_quote(http_text("https://qt.gtimg.cn/q=" + symbol, "gb18030"), symbol)
        except (OSError, ValueError, KeyError, TypeError, IndexError, AttributeError) as exc:
            warning = "最新报价未取得；图表末价不代表当前报价"
            quote_error = exc
        try:
            if interval == "day":
                url = "https://web.ifzq.gtimg.cn/appstock/app/fqkline/get?" + urlencode(
                    {"param": f"{symbol},day,,,320,"}
                )
            else:
                url = "https://web.ifzq.gtimg.cn/appstock/app/day/query?" + urlencode(
                    {"code": symbol}
                )
            payload = json.loads(http_text(url))
            obj = payload.get("data", {}).get(symbol, {})
            name = (
                quote.instrument.name if quote else obj.get("qt", {}).get(symbol, ["", symbol])[1]
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
                rows = sorted(parse_minute_days(payload, symbol), key=lambda b: b.timestamp)
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
                sources=("https://qt.gtimg.cn/q=" + symbol, url),
            )
        except (OSError, ValueError, KeyError, TypeError, IndexError, AttributeError) as exc:
            detail = "请检查网络或证券代码，稍后刷新"
            if quote is None and warning:
                detail += "；报价源也未能连接"
            raise MarketError("行情暂不可用：" + detail) from (
                quote_error if quote is None else exc
            )
