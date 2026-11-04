"""Versioned JSON for dashboard records; no credentials or executable objects."""

import json
from dataclasses import asdict
from datetime import date, datetime

from chengchuan.domain.market_dashboard import (
    IndexSummary,
    MarketBreadth,
    MarketOverview,
    QuoteDetails,
    QuoteMetric,
)
from chengchuan.domain.market_views import FlowBucket, HeatmapSnapshot, HeatTile, MoneyFlow


def encode_dashboard(value):
    return json.dumps(
        dict(
            version=3,
            kind={
                QuoteDetails: "details",
                MarketOverview: "overview",
                HeatmapSnapshot: "heatmap",
                MoneyFlow: "funds",
            }[type(value)],
            data=asdict(value),
        ),
        ensure_ascii=False,
        allow_nan=False,
        default=lambda x: x.isoformat(),
    )


def decode_dashboard(raw):
    obj = json.loads(raw)
    if obj["version"] not in (1, 2, 3):
        raise ValueError("指标缓存版本不受支持")
    row = obj["data"]
    if obj["version"] == 1:
        # Old records did not confirm all constituent response acquisition times.
        row["complete"] = False
    row["retrieved_at"] = datetime.fromisoformat(row["retrieved_at"])
    if obj["kind"] in ("heatmap", "funds"):
        row["day"] = date.fromisoformat(row["day"])
        if obj["kind"] == "heatmap":
            row["tiles"] = tuple(HeatTile(**t) for t in row["tiles"])
            return HeatmapSnapshot(**row)
        row["observed_at"] = datetime.fromisoformat(row["observed_at"])
        row["buckets"] = tuple(FlowBucket(**b) for b in row["buckets"])
        return MoneyFlow(**row)
    row["breadth"] = tuple(
        MarketBreadth(**dict(b, day=date.fromisoformat(b["day"]))) for b in row.get("breadth", ())
    )
    if obj["kind"] == "details":
        row["day"] = date.fromisoformat(row["day"])
        row["as_of"] = date.fromisoformat(row["as_of"]) if row.get("as_of") else None
        row["metrics"] = tuple(QuoteMetric(**m) for m in row["metrics"])
        return QuoteDetails(**row)
    if obj["kind"] != "overview":
        raise ValueError("指标缓存类型不受支持")
    row["indices"] = tuple(
        IndexSummary(
            **dict(
                i,
                day=date.fromisoformat(i["day"]),
                observed_at=datetime.fromisoformat(i["observed_at"])
                if i.get("observed_at")
                else None,
            )
        )
        for i in row["indices"]
    )
    return MarketOverview(**row)
