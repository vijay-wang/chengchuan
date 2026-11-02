from __future__ import annotations

import csv
import hashlib
import json
from collections import Counter
from datetime import datetime, timedelta, timezone
from pathlib import Path
from urllib.parse import urlencode
from urllib.request import Request, urlopen

from .core import Bar, Dataset, Flow, boundary


class PublicClient:
    def __init__(self, cache_dir: Path, refresh=False):
        self.cache_dir = cache_dir
        self.refresh = refresh
        self.provenance = []
        self.warnings = []
        cache_dir.mkdir(parents=True, exist_ok=True)

    def get(self, url, params):
        full_url = url + "?" + urlencode(params)
        key = hashlib.sha256(full_url.encode()).hexdigest()
        target = self.cache_dir / (key + ".json")
        cache_hit = target.exists() and not self.refresh
        if cache_hit:
            record = json.loads(target.read_text(encoding="utf-8"))
        else:
            request = Request(full_url, headers={
                "User-Agent": "Mozilla/5.0 FlowRadar/0.1",
                "Referer": "https://data.eastmoney.com/",
            })
            with urlopen(request, timeout=20) as response:
                payload = json.load(response)
            record = {"url": full_url, "retrieved_at_utc": datetime.now(timezone.utc).isoformat(),
                      "payload": payload}
            target.write_text(json.dumps(record, ensure_ascii=False), encoding="utf-8")
        self.provenance.append({"url": full_url, "retrieved_at_utc": record["retrieved_at_utc"],
                                "raw_cache": str(target), "sha256": key,
                                "cached": cache_hit})
        return record["payload"]


def instrument(symbol):
    s = symbol.lower().strip()
    if s.startswith(("sh", "sz", "bj")):
        market, code = s[:2], s[2:]
    elif len(s) == 6 and s.isdigit():
        market, code = ("sh" if s[0] in "569" else "sz"), s
    else:
        raise ValueError("证券代码应为002318、sh000001或sh515210等格式")
    if market == "bj" or len(code) != 6 or not code.isdigit():
        raise ValueError("当前公开适配器仅支持沪深市场")
    return market + code, ("1" if market == "sh" else "0") + "." + code


def daily_bars(client, symbol, start, end):
    tcode, secid = instrument(symbol)
    try:
        payload = client.get("https://push2his.eastmoney.com/api/qt/stock/kline/get", {
            "secid": secid, "klt": "101", "fqt": "0", "lmt": "1000",
            "beg": start[:10].replace("-", ""), "end": end[:10].replace("-", ""),
            "fields1": "f1,f2,f3,f4,f5,f6", "fields2": "f51,f52,f53,f54,f55,f56,f57,f58,f59,f60,f61",
        })
        obj = payload.get("data") or {}
        bars = []
        for row in obj.get("klines", []):
            x = row.split(",")
            bars.append(Bar(x[0] + "T15:00:00", float(x[1]), float(x[3]), float(x[4]),
                            float(x[2]), float(x[5]) * 100, float(x[6])))
        if not bars:
            raise ValueError("主日线源未返回行情")
        return obj.get("name", symbol), bars
    except (OSError, ValueError, KeyError, TypeError) as exc:
        client.warnings.append(f"{symbol}主日线接口不可用，已改用腾讯日线；该回退源不含成交额，未用收盘价乘成交量冒充成交额。原因：{exc}")
    payload = client.get("https://web.ifzq.gtimg.cn/appstock/app/fqkline/get", {
        "param": f"{tcode},day,{start[:10]},{end[:10]},1000,",
    })
    obj = (payload.get("data") or {}).get(tcode) or {}
    bars = [Bar(x[0]+"T15:00:00", float(x[1]), float(x[3]), float(x[4]),
                float(x[2]), float(x[5])*100, None) for x in obj.get("day", [])]
    if not bars:
        raise ValueError("两个公开日线源均未返回行情；请核对证券代码、日期或导入本地数据")
    quote = obj.get("qt", {}).get(tcode, [])
    return quote[1] if len(quote) > 1 else symbol, bars


def parse_minute_days(payload, tencent_code):
    obj = (payload.get("data") or {}).get(tencent_code) or {}
    bars = []
    for day in obj.get("data", []):
        date = datetime.strptime(day["date"], "%Y%m%d").date().isoformat()
        previous_volume, previous_amount = 0.0, 0.0
        for raw in day["data"]:
            x = raw.split()
            clock = x[0][:2] + ":" + x[0][2:] + ":00"
            if not ("09:30:00" <= clock <= "11:30:00" or "13:00:00" <= clock <= "15:00:00"):
                continue
            if len(x) < 4:
                raise ValueError("分钟源缺少累计成交额，不进行推算补值")
            price, cumulative_volume, cumulative_amount = float(x[1]), float(x[2]) * 100, float(x[3])
            volume, amount = cumulative_volume - previous_volume, cumulative_amount - previous_amount
            if volume < -.001 or amount < -1:
                raise ValueError("同一天累计成交量／额出现回退，须检查源数据")
            bars.append(Bar(date + "T" + clock, price, price, price, price,
                            max(0.0, volume), max(0.0, amount)))
            previous_volume, previous_amount = cumulative_volume, cumulative_amount
    return bars


def minute_bars(client, symbol):
    tcode, _ = instrument(symbol)
    payload = client.get("https://web.ifzq.gtimg.cn/appstock/app/day/query", {"code": tcode})
    bars = parse_minute_days(payload, tcode)
    if not bars:
        raise ValueError("公开分钟源没有返回行情")
    quote = (payload.get("data") or {}).get(tcode, {}).get("qt", {}).get(tcode, [])
    return quote[1] if len(quote) > 1 else symbol, bars


def parse_flows(payload, cumulative=False):
    rows, previous, last_day = [], [0.0] * 5, None
    for raw in (payload.get("data") or {}).get("klines", []):
        x = raw.split(",")
        timestamp = x[0].replace(" ", "T")
        if len(timestamp) == 10:
            timestamp += "T15:00:00"
        elif len(timestamp) == 16:
            timestamp += ":00"
        values = [float(v) for v in x[1:6]]
        if cumulative:
            if timestamp[:10] != last_day:
                previous = [0.0] * 5
            increments = [a-b for a, b in zip(values, previous)]
            previous, last_day = values, timestamp[:10]
        else:
            increments = values
        rows.append(Flow(timestamp, *increments))
    return rows


def flow_rows(client, symbol, interval):
    _, secid = instrument(symbol)
    url = ("https://push2.eastmoney.com/api/qt/stock/fflow/kline/get" if interval == "minute"
           else "https://push2his.eastmoney.com/api/qt/stock/fflow/daykline/get")
    payload = client.get(url, {
        "secid": secid, "lmt": "0" if interval == "minute" else "1000",
        "klt": "1" if interval == "minute" else "101",
        "fields1": "f1,f2,f3,f7", "fields2": "f51,f52,f53,f54,f55,f56",
    })
    return parse_flows(payload, cumulative=interval == "minute")


def tape_diagnostics(payload):
    rows = [(x.split(",")[0]) for x in (payload.get("data") or {}).get("details", [])]
    rows = [t for t in rows if "09:30:00" <= t <= "11:30:00" or "13:00:00" <= t < "14:57:00"]
    if len(rows) < 2:
        return None
    seconds = [sum(int(v)*k for v, k in zip(t.split(":"), [3600, 60, 1])) for t in rows]
    gaps = [b-a for a, b in zip(seconds, seconds[1:]) if 0 < b-a < 100]
    return {"records": len(rows), "three_second_grid_share": sum(s % 3 == 0 for s in seconds) / len(seconds),
            "top_gaps_seconds": Counter(gaps).most_common(5),
            "original_order_ids_available": False,
            "interpretation": "采样和发布格点不能当作真实下单节奏；此明细不支持母单还原。"}


def fetch_public(symbol, start, end, interval, cache_dir, refresh=False, benchmark="sh000001"):
    start, end = boundary(start), boundary(end, True)
    tcode, secid = instrument(symbol)
    client = PublicClient(Path(cache_dir), refresh)
    notes = []
    if interval == "day":
        expanded = (datetime.fromisoformat(start) - timedelta(days=90)).date().isoformat()
        name, bars = daily_bars(client, symbol, expanded, end)
    else:
        name, bars = minute_bars(client, symbol)
        notes.append("免费分钟接口只返回近期少数交易日；数周／数月的小时级研究需要另接历史分钟数据。")
        notes.append("分钟OHLC为分钟末采样价格，不代表该分钟内真实最高最低价。")
    try:
        flows = flow_rows(client, symbol, interval)
    except Exception as exc:
        flows = []
        notes.append("订单分类数据获取失败：" + str(exc))
    bm = []
    if benchmark:
        try:
            if interval == "day":
                _, bm = daily_bars(client, benchmark, expanded, end)
            else:
                _, bm = minute_bars(client, benchmark)
        except Exception as exc:
            notes.append("基准获取失败：" + str(exc))
    diagnostic = None
    if interval == "minute":
        try:
            obj = client.get("https://push2.eastmoney.com/api/qt/stock/details/get", {
                "secid": secid, "fields1": "f1,f2,f3,f4", "fields2": "f51,f52,f53,f54,f55",
                "pos": "-100000", "iscca": "1",
            })
            diagnostic = tape_diagnostics(obj)
        except Exception as exc:
            notes.append("公开成交明细诊断未取得：" + str(exc))
    notes.extend(client.warnings)
    data = Dataset(tcode, name, interval, bars, flows, bm, client.provenance, notes, diagnostic)
    if not any(start <= b.timestamp <= end for b in bars):
        available = (min(b.timestamp for b in bars), max(b.timestamp for b in bars))
        raise ValueError(f"所请求区间不在数据覆盖范围，当前可用：{available[0]} 至 {available[1]}")
    return data


def read_csv(path, symbol, interval):
    bars = []
    with Path(path).open(encoding="utf-8-sig", newline="") as stream:
        for x in csv.DictReader(stream):
            stamp = boundary(x["timestamp"], end=interval == "day")
            bars.append(Bar(stamp, *(float(x[k]) for k in ("open", "high", "low", "close", "volume_shares")),
                            float(x["amount_yuan"]) if x.get("amount_yuan") else None))
    return Dataset(symbol, symbol, interval, bars,
                   provenance=[{"source": "CSV", "path": str(Path(path).resolve())}],
                   quality_notes=["CSV价格、成交量必须使用同一口径；volume_shares单位为股、amount_yuan单位为元。"])
