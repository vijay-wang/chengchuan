"""MeoZ HTTP boundary: private credentials, shared persistent cache and conservative quotas."""

import hashlib
import json
import os
import re
import sqlite3
import time
from contextlib import contextmanager
from dataclasses import dataclass
from datetime import datetime, timezone
from pathlib import Path
from urllib.error import HTTPError, URLError
from urllib.request import Request, urlopen

from chengchuan.application.errors import MarketError, StorageError
from chengchuan.domain.models import SHANGHAI

LIVE_URLS = ("https://sz.meoz.cn:6688/api", "https://sh.meoz.cn:6688/api")
HISTORY_URL = "https://hist.meoz.cn:6688/api"
MAX_BYTES = 16_000_000


def private_key():
    value = os.environ.get("MEOZ_API_KEY", "").strip()
    if not value:
        for folder in (".meoz", ".meoz-market"):
            path = Path.home() / folder / "config.json"
            if path.exists():
                try:
                    value = str(json.loads(path.read_text()).get("api_key", "")).strip()
                except (OSError, ValueError, TypeError) as exc:
                    raise MarketError("猫爪本机配置无法读取") from exc
                if value:
                    break
    if not re.fullmatch(r"nc_[A-Za-z0-9_-]{32}", value):
        raise MarketError("请通过猫爪官方Skill配置API Key，或设置MEOZ_API_KEY")
    return value


def configured():
    try:
        private_key()
        return True
    except MarketError:
        return False


def clean_error(message):
    return re.sub(r"nc_[A-Za-z0-9_-]+", "[密钥已隐藏]", str(message))[:240]


@dataclass(frozen=True)
class ApiResult:
    rows: tuple[dict, ...]
    fetched_at: datetime
    cached: bool
    url: str
    notice: str = ""


class MeozStore:
    """One store shared by windows/processes. Reservations survive interrupted HTTP requests."""

    def __init__(self, path=None, daily_limit=90000, interface_limit=95):
        self.path = Path(path) if path else Path.home() / ".meoz/chengchuan.sqlite"
        self.daily_limit, self.interface_limit = daily_limit, interface_limit

    @contextmanager
    def connect(self):
        self.path.parent.mkdir(parents=True, exist_ok=True)
        if self.path.exists() and self.path.stat().st_size > 512_000_000:
            raise StorageError("猫爪本地数据档案超过512MB，请先管理本地数据目录")
        con = None
        try:
            for attempt in range(6):
                con = sqlite3.connect(self.path, timeout=5)
                try:
                    if con.execute("PRAGMA journal_mode").fetchone()[0] != "wal":
                        con.execute("PRAGMA journal_mode=WAL")
                    break
                except sqlite3.OperationalError as exc:
                    con.close()
                    con = None
                    if "locked" not in str(exc).lower() or attempt == 5:
                        raise
                    time.sleep(0.02 * 2**attempt)
            if con.execute("PRAGMA user_version").fetchone()[0] > 1:
                raise StorageError("猫爪缓存来自更高版本")
            con.executescript("""
                CREATE TABLE IF NOT EXISTS responses (
                    id TEXT PRIMARY KEY, fetched REAL NOT NULL, data TEXT NOT NULL);
                CREATE TABLE IF NOT EXISTS requests (
                    id INTEGER PRIMARY KEY, day TEXT NOT NULL, api TEXT NOT NULL,
                    reserved REAL NOT NULL, status TEXT NOT NULL);
                CREATE INDEX IF NOT EXISTS requests_day ON requests(day,api);
                CREATE TABLE IF NOT EXISTS leases (id TEXT PRIMARY KEY, expires REAL NOT NULL);
                CREATE TABLE IF NOT EXISTS failures (
                    id TEXT PRIMARY KEY, expires REAL NOT NULL, message TEXT NOT NULL);
                CREATE TABLE IF NOT EXISTS daily_history (
                    symbol TEXT NOT NULL, day TEXT NOT NULL, data TEXT NOT NULL,
                    PRIMARY KEY(symbol,day));
                CREATE TABLE IF NOT EXISTS daily_state (
                    symbol TEXT PRIMARY KEY, fetched REAL NOT NULL, checked_day TEXT NOT NULL);
                PRAGMA user_version=1;
            """)
            with con:
                yield con
        except sqlite3.Error as exc:
            raise StorageError("猫爪缓存／额度账本无法读写：" + clean_error(exc)) from exc
        finally:
            if con is not None:
                con.close()

    def cached(self, key, now, max_age, min_fetched):
        with self.connect() as con:
            row = con.execute("SELECT fetched,data FROM responses WHERE id=?", (key,)).fetchone()
        if not row or (max_age is not None and now - row[0] >= max_age):
            return None
        if min_fetched is not None and row[0] < min_fetched:
            return None
        payload = json.loads(row[1])
        # Empty successful batches can be published later; never freeze them permanently.
        if not (payload.get("data") or {}).get("items") and now - row[0] >= 300:
            return None
        return payload, row[0]

    def reserve(self, key, api, now):
        with self.connect() as con:
            con.execute("BEGIN IMMEDIATE")
            failed = con.execute(
                "SELECT message FROM failures WHERE id=? AND expires>?", (key, now)
            ).fetchone()
            if failed:
                raise MarketError("暂不重复请求：" + failed[0])
            active = con.execute(
                "SELECT id FROM leases WHERE id=? AND expires>?", (key, now)
            ).fetchone()
            if active:
                raise MarketError("相同猫爪数据正在获取，请稍后重试")
            last = con.execute("SELECT MAX(reserved) FROM requests").fetchone()[0]
            slot = max(now, (last or 0) + 0.4)
            if slot - now > 3:
                raise MarketError("猫爪请求队列繁忙，请稍后重试")
            day = datetime.fromtimestamp(slot, SHANGHAI).date().isoformat()
            total = con.execute("SELECT COUNT(*) FROM requests WHERE day=?", (day,)).fetchone()[0]
            count = con.execute(
                "SELECT COUNT(*) FROM requests WHERE day=? AND api=?", (day, api)
            ).fetchone()[0]
            if total >= self.daily_limit or count >= self.interface_limit:
                raise MarketError(
                    f"本机猫爪保护额度已达上限：{api} {count}/{self.interface_limit}；使用已存数据"
                )
            con.execute("INSERT OR REPLACE INTO leases VALUES (?,?)", (key, slot + 35))
            cursor = con.execute(
                "INSERT INTO requests(day,api,reserved,status) VALUES (?,?,?,?)",
                (day, api, slot, "reserved"),
            )
            return cursor.lastrowid, slot

    def finish(self, key, request_id, payload=None, error="", cooldown=60, response_key=None):
        now = time.time()
        with self.connect() as con:
            if payload is not None:
                con.execute(
                    "INSERT OR REPLACE INTO responses VALUES (?,?,?)",
                    (
                        response_key or key,
                        now,
                        json.dumps(payload, ensure_ascii=False, allow_nan=False),
                    ),
                )
                con.execute("DELETE FROM failures WHERE id=?", (key,))
            if error:
                con.execute(
                    "INSERT OR REPLACE INTO failures VALUES (?,?,?)",
                    (key, now + cooldown, clean_error(error)),
                )
            con.execute("DELETE FROM leases WHERE id=?", (key,))
            con.execute(
                "UPDATE requests SET status=? WHERE id=?",
                ("success" if payload is not None else "failed", request_id),
            )
        return now

    def usage(self, now=None):
        day = (now or datetime.now(SHANGHAI)).astimezone(SHANGHAI).date().isoformat()
        with self.connect() as con:
            rows = con.execute(
                "SELECT api,COUNT(*) FROM requests WHERE day=? GROUP BY api", (day,)
            ).fetchall()
        return dict(
            day=day,
            total=sum(n for _, n in rows),
            interfaces=dict(rows),
            daily_limit=self.daily_limit,
            interface_limit=self.interface_limit,
        )

    def daily_history(self, symbol):
        with self.connect() as con:
            state = con.execute(
                "SELECT fetched,checked_day FROM daily_state WHERE symbol=?", (symbol,)
            ).fetchone()
            rows = con.execute(
                "SELECT data FROM daily_history WHERE symbol=? ORDER BY day", (symbol,)
            ).fetchall()
        return (tuple(json.loads(r[0]) for r in rows), *state) if state else None

    def save_daily(self, symbol, rows, fetched, checked_day):
        with self.connect() as con:
            con.executemany(
                "INSERT OR REPLACE INTO daily_history VALUES (?,?,?)",
                [
                    (
                        symbol,
                        str(r["tradedate"]),
                        json.dumps(r, ensure_ascii=False, allow_nan=False),
                    )
                    for r in rows
                ],
            )
            con.execute(
                "INSERT OR REPLACE INTO daily_state VALUES (?,?,?)", (symbol, fetched, checked_day)
            )


def decode_rows(payload):
    if not isinstance(payload, dict) or payload.get("code") != 200:
        raise MarketError("猫爪返回无效业务响应")
    data = payload.get("data")
    if data is None:
        return ()
    fields, items = data.get("fields"), data.get("items")
    if (
        not isinstance(fields, list)
        or not isinstance(items, list)
        or len(set(fields)) != len(fields)
    ):
        raise MarketError("猫爪响应字段格式变化")
    if any(not isinstance(row, list) or len(row) != len(fields) for row in items):
        raise MarketError("猫爪响应列数不一致，拒绝错位解释")
    return tuple(dict(zip(fields, row)) for row in items)


class MeozClient:
    def __init__(self, store=None, offline=False):
        self.store = store or MeozStore()
        self.offline = offline

    def peek(self, api, params, fields="", service="live"):
        """Read a previously stored response without credentials, reservations or HTTP."""
        body = dict(apiname=api, params=params)
        if fields:
            body["fields"] = fields
        cache_key = hashlib.sha256(json.dumps([service, body], sort_keys=True).encode()).hexdigest()
        cached = self.store.cached(cache_key, time.time(), None, None)
        if cached is None:
            return None
        payload, fetched = cached
        return ApiResult(
            decode_rows(payload),
            datetime.fromtimestamp(fetched, timezone.utc),
            True,
            LIVE_URLS[0] if service == "live" else HISTORY_URL,
        )

    def query(self, api, params, fields="", service="live", max_age=180, min_fetched=None):
        if service not in ("live", "history"):
            raise ValueError("猫爪服务类型无效")
        url = LIVE_URLS[0] if service == "live" else HISTORY_URL
        body = dict(apiname=api, params=params)
        if fields:
            body["fields"] = fields
        cache_key = hashlib.sha256(json.dumps([service, body], sort_keys=True).encode()).hexdigest()
        now = time.time()
        cached = self.store.cached(cache_key, now, max_age, min_fetched)
        stale = cached or self.store.cached(cache_key, now, None, None)
        if self.offline:
            if not stale:
                raise MarketError("离线缓存缺少此接口／日期的数据")
            payload, fetched = stale
            return ApiResult(
                decode_rows(payload),
                datetime.fromtimestamp(fetched, timezone.utc),
                True,
                url,
                "离线读取本地已存数据",
            )
        if cached:
            payload, fetched = cached
            return ApiResult(
                decode_rows(payload), datetime.fromtimestamp(fetched, timezone.utc), True, url
            )
        key_value = private_key()
        # Failure suppression changes when a user changes credentials; cached public data is reusable.
        failure_key = cache_key + hashlib.sha256(key_value.encode()).hexdigest()[:12]
        try:
            request_id, slot = self.store.reserve(failure_key, api, now)
        except MarketError as exc:
            if stale:
                payload, fetched = stale
                return ApiResult(
                    decode_rows(payload),
                    datetime.fromtimestamp(fetched, timezone.utc),
                    True,
                    url,
                    clean_error(exc),
                )
            raise
        if slot > now:
            time.sleep(slot - now)
        body["apikey"] = key_value
        request = Request(
            url,
            data=json.dumps(body).encode(),
            headers={"Content-Type": "application/json", "User-Agent": "Chengchuan/0.4"},
        )
        cooldown = 60
        try:
            with urlopen(request, timeout=12) as response:
                raw = response.read(MAX_BYTES + 1)
            if len(raw) > MAX_BYTES:
                raise MarketError("猫爪响应超过16MB限制")
            payload = json.loads(raw)
            if payload.get("code") != 200:
                code = payload.get("code")
                cooldown = 3600 if code in (401, 403) else 60
                raise MarketError(
                    f"猫爪接口{api}：{code} " + clean_error(payload.get("message", "请求失败"))
                )
            rows = decode_rows(payload)
            # A server is never allowed to reflect a credential into our public cache.
            if key_value in raw.decode("utf-8", errors="replace"):
                raise MarketError("猫爪响应包含敏感认证信息，拒绝缓存")
        except HTTPError as exc:
            retry_after = exc.headers.get("Retry-After", "60")
            seconds = int(retry_after) if retry_after.isdigit() else 60
            cooldown = 3600 if exc.code in (401, 403) else max(60, min(3600, seconds))
            message = f"猫爪接口{api}：HTTP {exc.code}；请检查套餐、额度或稍后重试"
            exc.close()
            self.store.finish(failure_key, request_id, error=message, cooldown=cooldown)
            if stale:
                payload, fetched = stale
                return ApiResult(
                    decode_rows(payload),
                    datetime.fromtimestamp(fetched, timezone.utc),
                    True,
                    url,
                    message,
                )
            raise MarketError(message) from None
        except (OSError, URLError, ValueError, TypeError, AttributeError, MarketError) as exc:
            message = clean_error(exc)
            self.store.finish(failure_key, request_id, error=message, cooldown=cooldown)
            if stale:
                payload, fetched = stale
                return ApiResult(
                    decode_rows(payload),
                    datetime.fromtimestamp(fetched, timezone.utc),
                    True,
                    url,
                    "更新失败，保留原数据：" + message,
                )
            raise MarketError(message) from None
        # The lease/failure key is credential-specific; the response key contains no credential.
        fetched = self.store.finish(
            failure_key, request_id, payload=payload, response_key=cache_key
        )
        return ApiResult(rows, datetime.fromtimestamp(fetched, timezone.utc), False, url)
