"""Private transient tickets and a single stock five-level WebSocket stream."""

import hashlib
import json
import logging
import time
from datetime import datetime
from urllib.parse import urlsplit
from urllib.request import Request, urlopen

from chengchuan.application.errors import MarketError
from chengchuan.application.quote_metrics import number
from chengchuan.domain.market import stock_symbol
from chengchuan.domain.microstructure import BookLevel, BookSnapshot
from chengchuan.domain.models import SHANGHAI
from chengchuan.infrastructure.meoz_client import LIVE_URLS, private_key

STREAM_FIELDS = (
    "symbol",
    "time",
    "lastPrice",
    "volume",
    "amount",
    "bidPrice",
    "askPrice",
    "bidVol",
    "askVol",
    "send_ts_ms",
)


def request_ticket(store, symbol):
    symbol = stock_symbol(symbol)
    body = dict(
        channel="tick_stream",
        stream_version="tick_v1",
        symbols=[symbol[-6:] + (".SH" if symbol.startswith("sh") else ".SZ")],
        ttl_seconds=120,
    )
    key = private_key()
    lease = (
        "ticket:"
        + hashlib.sha256(json.dumps(body, sort_keys=True).encode() + key.encode()).hexdigest()
    )
    reserved, slot = store.reserve(lease, "tick_stream_ticket", time.time())
    if slot > time.time():
        time.sleep(slot - time.time())
    body["apikey"] = key
    endpoint = LIVE_URLS[0].removesuffix("/api") + "/api/v2/realtime/ws-ticket"
    try:
        with urlopen(
            Request(
                endpoint,
                data=json.dumps(body).encode(),
                headers={"Content-Type": "application/json", "User-Agent": "Chengchuan/0.9"},
            ),
            timeout=6,
        ) as response:
            raw = response.read(65537)
        if len(raw) > 65536:
            raise MarketError("订阅票据响应过大")
        result = json.loads(raw)
        if result.get("code") != 200:
            raise MarketError("订阅权益不足或票据暂不可用")
        data = result.get("data") or {}
        parsed = urlsplit(data.get("url", ""))
        if (
            parsed.scheme != "wss"
            or parsed.hostname not in ("meoz.cn", "sz.meoz.cn", "sh.meoz.cn", "t.meoz.cn")
            or data.get("stream_version") != "tick_v1"
            or not parsed.query
        ):
            raise MarketError("订阅返回了非官方WSS地址或版本")
        if key in raw.decode("utf-8", errors="replace"):
            raise MarketError("票据响应反射凭据，已拒绝")
    except Exception:
        store.finish(lease, reserved, error="订阅票据取得失败，请检查权益或稍后重试", cooldown=180)
        raise MarketError("订阅票据取得失败，请检查权益或稍后重试") from None
    # The URL is an ephemeral credential. Never put it in cache, logs or status.
    store.finish(lease, reserved)
    return data


def parse_stream_book(row, fields, symbol):
    if len(row) != len(fields) or not set(STREAM_FIELDS).issubset(fields):
        raise MarketError("实时五档字段契约变化")
    data = dict(zip(fields, row))
    expected = symbol[-6:] + (".SH" if symbol.startswith("sh") else ".SZ")
    if data.get("symbol") != expected:
        raise MarketError("实时五档返回其他证券")
    stamp = number(data.get("time"))
    if stamp is None:
        raise MarketError("实时五档缺少源时间")
    levels = []
    for name in ("bidPrice", "askPrice", "bidVol", "askVol"):
        if not isinstance(data.get(name), list) or len(data[name]) > 5:
            raise MarketError("实时五档数组格式无效")
    for i in range(5):

        def at(name):
            values = data[name]
            value = number(values[i]) if i < len(values) else None
            return value if value is not None and value >= 0 else None

        bid, ask = at("bidPrice"), at("askPrice")
        levels.append(BookLevel(i + 1, bid or None, at("bidVol"), ask or None, at("askVol")))
    return BookSnapshot(
        symbol,
        datetime.fromtimestamp(stamp / 1000, SHANGHAI),
        tuple(levels),
        "猫头鹰tick_v1五档增量",
        quantity_unit="源数量",
        notice="WS委托量单位尚未独立核定，原值显示；五档合计，非单笔队列。",
    )


def stream_books(store, symbol, stop):
    from websockets.exceptions import InvalidStatus
    from websockets.sync.client import connect

    symbol = stock_symbol(symbol)
    yield "正在取得实时五档连接票据…"
    ticket = request_ticket(store, symbol)
    if stop.is_set():
        return
    logger = logging.getLogger("chengchuan.private_stream")
    logger.addHandler(logging.NullHandler())
    logger.propagate = False
    try:
        with connect(
            ticket["url"],
            open_timeout=6,
            close_timeout=1,
            ping_interval=20,
            ping_timeout=20,
            max_size=2_000_000,
            max_queue=16,
            logger=logger,
        ) as socket:
            yield "WSS已连接，等待订阅确认；休市可能没有新行情"
            fields = None
            while not stop.is_set():
                try:
                    raw = socket.recv(timeout=0.5)
                except TimeoutError:
                    continue
                message = json.loads(raw)
                if not isinstance(message, dict):
                    raise MarketError("实时消息结构无效")
                if (
                    message.get("error")
                    or message.get("op") in ("error", "err")
                    or message.get("t") in ("error", "err")
                ):
                    raise MarketError("实时订阅被拒绝，请检查权益及订阅上限")
                nested = message.get("data")
                candidate = message.get("fields") or (
                    nested.get("fields") if isinstance(nested, dict) else None
                )
                if candidate:
                    if not isinstance(candidate, list) or not set(STREAM_FIELDS).issubset(
                        candidate
                    ):
                        raise MarketError("实时字段确认不匹配")
                    fields = tuple(candidate)
                    yield "实时五档订阅已确认"
                rows = message.get("i")
                if rows:
                    if fields is None:
                        raise MarketError("实时推送先于字段确认，已暂停")
                    if not isinstance(rows, list) or len(rows) > 10000:
                        raise MarketError("实时增量超过批次边界")
                    for row in rows:
                        yield parse_stream_book(row, fields, symbol)
    except InvalidStatus as error:
        if not stop.is_set():
            raise MarketError(
                f"实时五档服务HTTP {error.response.status_code}；保留快照，可稍后手动重连"
            ) from None
    except Exception:
        if not stop.is_set():
            raise MarketError("实时五档连接中断；已有快照保留，可手动重连") from None
