"""Bounded annotation book, using the existing workspace port for persistence."""

from datetime import datetime
from uuid import uuid4

from chengchuan.application.errors import StorageError
from chengchuan.domain.drawings import Drawing, DrawingPoint


class DrawingBook:
    max_per_chart = 100
    max_total = 2000

    def __init__(self, workspace=None):
        self.workspace = workspace
        self.items = {}
        self.dirty = False
        self.notice = ""
        self.read_only = False
        try:
            state = workspace.load("chart_drawings") if workspace else {}
            if not state:
                return
            if not isinstance(state, dict) or state.get("schema") != 1:
                self.read_only = True
                self.notice = "画线档案版本不受支持，未修改原档案"
                return
            rows = state.get("items", [])
            if not isinstance(rows, list) or len(rows) > self.max_total:
                raise ValueError("画线档案超过数量限制或格式错误")
            skipped = 0
            for row in rows:
                try:
                    drawing = Drawing(
                        row["id"],
                        row["symbol"],
                        row["interval"],
                        row["tool"],
                        tuple(
                            DrawingPoint(datetime.fromisoformat(p["timestamp"]), p["price"])
                            for p in row["points"]
                        ),
                    )
                    if drawing.id in self.items:
                        raise ValueError("重复画线标识")
                    self.put(drawing)
                except (KeyError, TypeError, ValueError, AttributeError, OverflowError):
                    skipped += 1
            self.dirty = False
            if skipped:
                self.notice = f"有{skipped}条无效画线未载入"
        except (StorageError, OSError, ValueError, TypeError) as exc:
            self.read_only = True
            self.notice = "画线档案未载入：" + str(exc)

    def for_chart(self, context):
        return tuple(d for d in self.items.values() if d.context == context)

    def put(self, drawing):
        if self.read_only:
            raise ValueError(self.notice)
        if drawing.id not in self.items:
            if (
                len(self.items) >= self.max_total
                or len(self.for_chart(drawing.context)) >= self.max_per_chart
            ):
                raise ValueError("画线数量已达上限，请删除部分画线后继续")
        self.items[drawing.id] = drawing
        self.dirty = True

    def add(self, symbol, interval, tool, points):
        drawing = Drawing(uuid4().hex, symbol, interval, tool, tuple(points))
        self.put(drawing)
        return drawing

    def remove(self, ids):
        if self.read_only:
            raise ValueError(self.notice)
        for identity in ids:
            if identity in self.items:
                del self.items[identity]
                self.dirty = True

    def flush(self):
        if not self.dirty:
            return
        state = {
            "schema": 1,
            "items": [
                {
                    "id": d.id,
                    "symbol": d.symbol,
                    "interval": d.interval,
                    "tool": d.tool,
                    "points": [
                        {"timestamp": p.timestamp.isoformat(), "price": p.price} for p in d.points
                    ],
                }
                for d in self.items.values()
            ],
        }
        if self.workspace:
            self.workspace.save("chart_drawings", state)
        self.dirty = False
