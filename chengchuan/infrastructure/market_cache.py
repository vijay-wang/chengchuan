"""跨进程行情缓存；SQLite事务支持请求被终止时保留上一次完整数据。"""

import json
import sqlite3
from contextlib import contextmanager
from dataclasses import asdict
from datetime import datetime
from pathlib import Path

from chengchuan.application.errors import StorageError
from chengchuan.domain.market import Instrument, MarketFrame, Quote
from chengchuan.domain.models import MarketBar, MarketSeries, MarketSnapshot, SeriesKind


def encode_frame(frame):
    return json.dumps(
        {
            "version": 1,
            "series": asdict(frame.snapshot.series),
            "revision": frame.snapshot.revision,
            "source": frame.snapshot.source,
            "notes": frame.snapshot.quality_notes,
            "retrieved_at": frame.retrieved_at.isoformat(),
            "quote": asdict(frame.quote) if frame.quote else None,
            "notice": frame.notice,
            "sources": frame.sources,
        },
        default=lambda value: value.isoformat(),
        ensure_ascii=False,
        allow_nan=False,
    )


def decode_frame(raw):
    obj = json.loads(raw)
    if obj["version"] != 1:
        raise ValueError("行情缓存版本不受支持")
    series = obj["series"]
    bars = tuple(
        MarketBar(**dict(b, timestamp=datetime.fromisoformat(b["timestamp"])))
        for b in series["bars"]
    )
    series = MarketSeries(
        series["symbol"], series["name"], series["interval"], SeriesKind(series["kind"]), bars
    )
    snapshot = MarketSnapshot(
        obj["revision"], series, quality_notes=tuple(obj["notes"]), source=obj["source"]
    )
    q = obj["quote"]
    quote = (
        Quote(
            Instrument(**q["instrument"]),
            q["price"],
            q["previous_close"],
            datetime.fromisoformat(q["timestamp"]),
        )
        if q
        else None
    )
    return MarketFrame(
        snapshot,
        quote,
        datetime.fromisoformat(obj["retrieved_at"]),
        notice=obj.get("notice", ""),
        sources=tuple(obj.get("sources", ())),
    )


class SqliteMarketCache:
    def __init__(self, path: Path):
        self.path = path
        path.parent.mkdir(parents=True, exist_ok=True)
        with self._connect() as con:
            version = con.execute("PRAGMA user_version").fetchone()[0]
            if version > 1:
                raise StorageError("行情缓存由更高版本创建")
            con.execute("PRAGMA journal_mode=WAL")
            con.execute(
                "CREATE TABLE IF NOT EXISTS quotes (symbol TEXT, interval TEXT, "
                "retrieved TEXT, data TEXT, PRIMARY KEY(symbol,interval))"
            )
            con.execute(
                "CREATE TABLE IF NOT EXISTS instruments (symbol TEXT PRIMARY KEY, "
                "name TEXT NOT NULL, initials TEXT NOT NULL)"
            )
            con.execute("PRAGMA user_version=1")

    @contextmanager
    def _connect(self):
        con = None
        try:
            con = sqlite3.connect(self.path, timeout=1)
            with con:
                yield con
        except (sqlite3.Error, ValueError, KeyError, TypeError) as exc:
            raise StorageError("行情缓存无法读写：" + str(exc)) from exc
        finally:
            if con:
                con.close()

    def load(self, symbol, interval):
        with self._connect() as con:
            row = con.execute(
                "SELECT data FROM quotes WHERE symbol=? AND interval=?", (symbol, interval)
            ).fetchone()
            frame = decode_frame(row[0]) if row else None
            if frame and (
                frame.snapshot.series.symbol != symbol or frame.snapshot.series.interval != interval
            ):
                raise StorageError("行情缓存的证券或周期不一致")
            return frame

    def save(self, frame):
        series = frame.snapshot.series
        with self._connect() as con:
            con.execute(
                "INSERT INTO quotes VALUES (?,?,?,?) ON CONFLICT(symbol,interval) "
                "DO UPDATE SET retrieved=excluded.retrieved, data=excluded.data",
                (
                    series.symbol,
                    series.interval,
                    frame.retrieved_at.isoformat(),
                    encode_frame(frame),
                ),
            )
            con.execute(
                "DELETE FROM quotes WHERE rowid NOT IN (SELECT rowid FROM quotes "
                "ORDER BY retrieved DESC LIMIT 200)"
            )
        self.remember((Instrument(series.symbol, series.name),))

    def remember(self, items):
        with self._connect() as con:
            con.executemany(
                "INSERT INTO instruments VALUES (?,?,?) ON CONFLICT(symbol) "
                "DO UPDATE SET name=excluded.name, initials=CASE WHEN excluded.initials='' "
                "THEN instruments.initials ELSE excluded.initials END",
                [(i.symbol, i.name, i.initials) for i in items],
            )
            con.execute(
                "DELETE FROM instruments WHERE rowid NOT IN "
                "(SELECT rowid FROM instruments ORDER BY rowid DESC LIMIT 5000)"
            )

    def search(self, query):
        with self._connect() as con:
            rows = con.execute(
                "SELECT symbol,name,initials FROM instruments WHERE "
                "instr(symbol,?)>0 OR instr(name,?)>0 OR instr(initials,?)>0 LIMIT 20",
                (query.lower(), query, query.lower()),
            ).fetchall()
        return tuple(Instrument(*row) for row in rows)
