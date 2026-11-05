"""Scalar market payloads: preserve the wire/hash format without recursive deep copies."""

import hashlib
import json


def series_payload(series):
    return dict(
        symbol=series.symbol,
        name=series.name,
        interval=series.interval,
        kind=series.kind,
        bars=tuple(
            dict(
                timestamp=b.timestamp,
                close=b.close,
                volume_shares=b.volume_shares,
                amount_yuan=b.amount_yuan,
                open=b.open,
                high=b.high,
                low=b.low,
            )
            for b in series.bars
        ),
    )


def market_revision(series, vwap=(), baseline=None, references=()):
    return hashlib.sha256(
        json.dumps(
            [series_payload(series), vwap, baseline, references], default=str, sort_keys=True
        ).encode()
    ).hexdigest()
