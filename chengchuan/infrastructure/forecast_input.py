"""可离线复算的预测输入档案，与旧日线快照兼容。"""

import json
from dataclasses import asdict
from datetime import datetime
from pathlib import Path

from chengchuan.domain.models import (
    MarketBar,
    MarketSeries,
    MarketSnapshot,
    OrderSizeFlow,
    SeriesKind,
)
from chengchuan.infrastructure.legacy import LegacyJsonRepository


def _series(obj):
    if obj is None:
        return None
    bars = tuple(
        MarketBar(**dict(b, timestamp=datetime.fromisoformat(b["timestamp"]))) for b in obj["bars"]
    )
    return MarketSeries(obj["symbol"], obj["name"], obj["interval"], SeriesKind(obj["kind"]), bars)


def decode_snapshot(obj):
    flows = tuple(
        OrderSizeFlow(**dict(f, timestamp=datetime.fromisoformat(f["timestamp"])))
        for f in obj.get("flows", [])
    )
    return MarketSnapshot(
        obj["revision"],
        _series(obj["series"]),
        flows,
        _series(obj.get("benchmark")),
        tuple(obj.get("quality_notes", [])),
        obj.get("source", "研究档案"),
    )


def load_input(path):
    path = Path(path)
    if path.stat().st_size > 32_000_000:
        raise ValueError("单个研究输入不能超过32MB")
    obj = json.loads(path.read_text(encoding="utf-8"))
    if "research_input_version" not in obj:
        return LegacyJsonRepository().load(path), None, None
    if obj["research_input_version"] != 1:
        raise ValueError("研究输入格式版本不受支持")
    return (
        decode_snapshot(obj["snapshot"]),
        datetime.fromisoformat(obj["observed_at"]) if obj.get("observed_at") else None,
        datetime.fromisoformat(obj["quote_at"]) if obj.get("quote_at") else None,
    )


def save_input(frame, path):
    path = Path(path)
    path.parent.mkdir(parents=True, exist_ok=True)
    obj = dict(
        research_input_version=1,
        snapshot=asdict(frame.snapshot),
        observed_at=frame.retrieved_at,
        quote_at=frame.quote.timestamp if frame.quote else None,
        sources=frame.sources,
    )
    temporary = path.with_name(path.name + ".tmp")
    temporary.write_text(
        json.dumps(obj, default=str, ensure_ascii=False, allow_nan=False), encoding="utf-8"
    )
    temporary.replace(path)


class ForecastSnapshotRepository:
    def load(self, path):
        return load_input(path)[0]
