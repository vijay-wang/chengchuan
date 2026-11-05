"""Append-only, size-bounded local research evidence. Does not contain credentials."""

import hashlib
import json
import sqlite3
from contextlib import contextmanager
from dataclasses import asdict, replace
from datetime import datetime
from pathlib import Path

from chengchuan.application.errors import StorageError
from chengchuan.domain.research import ResearchColumn, ResearchMeta, ResearchTable


def encode_table(table):
    data = asdict(table)
    for field in ("fetched_at", "first_seen_at", "observed_at"):
        value = data["meta"][field]
        data["meta"][field] = value.isoformat() if value else None
    return json.dumps(data, ensure_ascii=False, allow_nan=False)


def decode_table(data):
    obj = json.loads(data)
    meta = obj["meta"]
    for field in ("fetched_at", "first_seen_at", "observed_at"):
        meta[field] = datetime.fromisoformat(meta[field]) if meta.get(field) else None
    return ResearchTable(
        obj["key"],
        obj["title"],
        tuple(ResearchColumn(**c) for c in obj["columns"]),
        tuple(tuple(r) for r in obj["rows"]),
        ResearchMeta(**meta),
        obj["notice"],
        obj["document"],
    )


class SqliteResearchArchive:
    def __init__(self, path):
        self.path = Path(path)

    @contextmanager
    def connect(self):
        self.path.parent.mkdir(parents=True, exist_ok=True)
        con = None
        try:
            if self.path.exists() and self.path.stat().st_size > 256_000_000:
                raise StorageError("研究档案已达256MB，请导出并管理本地档案")
            con = sqlite3.connect(self.path, timeout=5)
            if con.execute("PRAGMA journal_mode").fetchone()[0] != "wal":
                con.execute("PRAGMA journal_mode=WAL")
            version = con.execute("PRAGMA user_version").fetchone()[0]
            if version > 2:
                raise StorageError("研究档案来自更高版本")
            if version < 2:
                con.executescript("""
                CREATE TABLE IF NOT EXISTS snapshots (
                    scope TEXT NOT NULL, revision TEXT NOT NULL,
                    seen REAL NOT NULL, business_day TEXT NOT NULL, payload TEXT NOT NULL,
                    PRIMARY KEY(scope, revision));
                CREATE INDEX IF NOT EXISTS snapshots_seen ON snapshots(scope,seen);
                CREATE TABLE IF NOT EXISTS observations (
                    scope TEXT NOT NULL, seen REAL NOT NULL, revision TEXT NOT NULL,
                    PRIMARY KEY(scope,seen));
                """)
                con.execute(
                    "INSERT OR IGNORE INTO observations SELECT scope,seen,revision FROM snapshots"
                )
                con.execute("PRAGMA user_version=2")
            with con:
                yield con
        except sqlite3.Error as exc:
            raise StorageError("研究档案无法读写") from exc
        finally:
            if con is not None:
                con.close()

    def append(self, scope, table, observed_at):
        # Local cache status/fetch time do not create artificial source revisions.
        content = [
            table.key,
            table.meta.source,
            table.meta.source_url,
            [
                (c.key, c.label, c.unit) + ((c.description,) if c.description else ())
                for c in table.columns
            ],
            table.rows,
            table.document,
            table.meta.business_day,
            table.meta.source_updated_at,
            table.meta.published_at,
            table.meta.coverage,
            table.meta.complete,
        ]
        revision = hashlib.sha256(json.dumps(content, ensure_ascii=False).encode()).hexdigest()
        with self.connect() as con:
            row = con.execute(
                "SELECT seen FROM snapshots WHERE scope=? AND revision=?", (scope, revision)
            ).fetchone()
            first = datetime.fromtimestamp(row[0], observed_at.tzinfo) if row else observed_at
            table = replace(
                table,
                meta=replace(
                    table.meta, revision=revision, first_seen_at=first, observed_at=observed_at
                ),
            )
            if row is None:
                con.execute(
                    "INSERT INTO snapshots VALUES (?,?,?,?,?)",
                    (
                        scope,
                        revision,
                        first.timestamp(),
                        table.meta.business_day,
                        encode_table(table),
                    ),
                )
            con.execute(
                "INSERT OR IGNORE INTO observations VALUES (?,?,?)",
                (scope, observed_at.timestamp(), revision),
            )
        return table

    def before(self, scope, cutoff, business_day="", exclude_revision=""):
        with self.connect() as con:
            row = con.execute(
                "SELECT s.payload,o.seen FROM observations o JOIN snapshots s "
                "ON s.scope=o.scope AND s.revision=o.revision "
                "WHERE o.scope=? AND o.seen<=? AND o.revision<>? "
                "AND (?='' OR s.business_day=?) ORDER BY o.seen DESC,o.rowid DESC LIMIT 1",
                (scope, cutoff.timestamp(), exclude_revision, business_day, business_day),
            ).fetchone()
        if not row:
            return None
        table = decode_table(row[0])
        return replace(
            table,
            meta=replace(table.meta, observed_at=datetime.fromtimestamp(row[1], cutoff.tzinfo)),
        )
