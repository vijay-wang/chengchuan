"""独立研究档案：事务写入，已发出的预测不可覆盖，重复运行不重复计数。"""

import json
import sqlite3
from contextlib import contextmanager
from dataclasses import asdict
from datetime import datetime, timezone
from pathlib import Path

from chengchuan.application.errors import StorageError
from chengchuan.application.forecast import digest


def encode(value):
    return json.dumps(value, default=str, ensure_ascii=False, sort_keys=True, allow_nan=False)


class SqliteForecastJournal:
    def __init__(self, path: Path):
        self.path = Path(path)

    @contextmanager
    def _connect(self):
        self.path.parent.mkdir(parents=True, exist_ok=True)
        if self.path.exists() and self.path.stat().st_size > 512_000_000:
            raise StorageError("研究档案超过512MB，请指定新的研究目录；不会自动删除旧预测")
        con = None
        try:
            con = sqlite3.connect(self.path, timeout=5)
            con.execute("PRAGMA foreign_keys=ON")
            con.execute("PRAGMA journal_mode=WAL")
            if con.execute("PRAGMA user_version").fetchone()[0] > 1:
                raise StorageError("研究档案来自更高版本")
            con.executescript("""
                CREATE TABLE IF NOT EXISTS snapshots (
                    revision TEXT PRIMARY KEY, content_digest TEXT NOT NULL, data TEXT NOT NULL);
                CREATE TABLE IF NOT EXISTS experiments (
                    id TEXT PRIMARY KEY, manifest TEXT NOT NULL, created_at TEXT NOT NULL);
                CREATE TABLE IF NOT EXISTS predictions (
                    experiment_id TEXT NOT NULL, id TEXT NOT NULL, data TEXT NOT NULL,
                    PRIMARY KEY(experiment_id,id),
                    FOREIGN KEY(experiment_id) REFERENCES experiments(id));
                CREATE TABLE IF NOT EXISTS evaluations (
                    experiment_id TEXT NOT NULL, prediction_id TEXT NOT NULL, data TEXT NOT NULL,
                    PRIMARY KEY(experiment_id,prediction_id),
                    FOREIGN KEY(experiment_id,prediction_id) REFERENCES predictions(experiment_id,id));
                PRAGMA user_version=1;
            """)
            with con:
                yield con
        except sqlite3.Error as exc:
            raise StorageError("研究档案无法读写：" + str(exc)) from exc
        finally:
            if con is not None:
                con.close()

    def predictions(self, manifest, snapshot, rows):
        experiment_id = digest(manifest)
        data = encode(asdict(snapshot))
        with self._connect() as con:
            prior_snapshot = con.execute(
                "SELECT content_digest FROM snapshots WHERE revision=?", (snapshot.revision,)
            ).fetchone()
            if prior_snapshot and prior_snapshot[0] != digest(asdict(snapshot)):
                raise StorageError("同一快照版本对应不同内容，拒绝覆盖研究档案")
            con.execute(
                "INSERT OR IGNORE INTO snapshots VALUES (?,?,?)",
                (snapshot.revision, digest(asdict(snapshot)), data),
            )
            con.execute(
                "INSERT OR IGNORE INTO experiments VALUES (?,?,?)",
                (experiment_id, encode(manifest), datetime.now(timezone.utc).isoformat()),
            )
            for row in rows:
                encoded = encode(asdict(row))
                prior = con.execute(
                    "SELECT data FROM predictions WHERE experiment_id=? AND id=?",
                    (experiment_id, row.record_id),
                ).fetchone()
                if prior and prior[0] != encoded:
                    raise StorageError("相同实验和预测标识的内容冲突，拒绝覆盖已保存预测")
                con.execute(
                    "INSERT OR IGNORE INTO predictions VALUES (?,?,?)",
                    (experiment_id, row.record_id, encoded),
                )

    def evaluations(self, experiment_id, rows):
        with self._connect() as con:
            for row in rows:
                encoded = encode(asdict(row))
                prior = con.execute(
                    "SELECT data FROM evaluations WHERE experiment_id=? AND prediction_id=?",
                    (experiment_id, row.prediction_id),
                ).fetchone()
                if prior and prior[0] != encoded:
                    raise StorageError("评价内容已变化，请创建新的数据版本实验，不覆盖旧评价")
                con.execute(
                    "INSERT OR IGNORE INTO evaluations VALUES (?,?,?)",
                    (experiment_id, row.prediction_id, encoded),
                )
