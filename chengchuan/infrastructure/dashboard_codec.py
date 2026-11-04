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


def encode_dashboard(value):
    return json.dumps(
        dict(
            version=1,
            kind="details" if isinstance(value, QuoteDetails) else "overview",
            data=asdict(value),
        ),
        ensure_ascii=False,
        allow_nan=False,
        default=lambda x: x.isoformat(),
    )


def decode_dashboard(raw):
    obj = json.loads(raw)
    if obj["version"] != 1:
        raise ValueError("指标缓存版本不受支持")
    row = obj["data"]
    row["retrieved_at"] = datetime.fromisoformat(row["retrieved_at"])
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
