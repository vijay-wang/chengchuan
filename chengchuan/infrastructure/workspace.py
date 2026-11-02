import json
import sqlite3
from contextlib import contextmanager
from pathlib import Path

from chengchuan.application.errors import StorageError


class SqliteWorkspaceRepository:
    """每次操作独立连接；事务提交工作区，避免跨线程共享连接。"""

    def __init__(self, path: Path):
        self.path = path
        path.parent.mkdir(parents=True, exist_ok=True)
        with self._connect() as con:
            version = con.execute("PRAGMA user_version").fetchone()[0]
            if version > 1:
                raise ValueError("工作区由更高版本创建，请升级软件后打开")
            con.execute("PRAGMA journal_mode=WAL")
            con.execute(
                "CREATE TABLE IF NOT EXISTS workspaces (name TEXT PRIMARY KEY, state TEXT NOT NULL)"
            )
            con.execute("PRAGMA user_version=1")

    @contextmanager
    def _connect(self):
        con = None
        try:
            con = sqlite3.connect(self.path, timeout=3)
            with con:
                yield con
        except sqlite3.Error as exc:
            raise StorageError("无法读写本地工作区：" + str(exc)) from exc
        finally:
            if con is not None:
                con.close()

    def load(self, name):
        with self._connect() as con:
            row = con.execute("SELECT state FROM workspaces WHERE name=?", (name,)).fetchone()
        return json.loads(row[0]) if row else {}

    def save(self, name, state):
        encoded = json.dumps(state, ensure_ascii=False, allow_nan=False)
        with self._connect() as con:
            con.execute(
                "INSERT INTO workspaces(name,state) VALUES (?,?) "
                "ON CONFLICT(name) DO UPDATE SET state=excluded.state",
                (name, encoded),
            )
