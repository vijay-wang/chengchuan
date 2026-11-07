"""Freeze a fixed two-up research panel, optionally fetch at most three daily batches.

No accuracy, event direction or final-test outcome summaries are calculated here.
All raw files live in an ignored private folder; no API key enters files or output.
"""

import argparse
import hashlib
import json
from dataclasses import asdict
from datetime import date, datetime
from pathlib import Path

from chengchuan.domain.calendar import TradingCalendar
from chengchuan.domain.market import stock_symbol
from chengchuan.domain.models import SHANGHAI
from chengchuan.domain.two_up import EVENT_VERSION, TARGET_VERSION, TwoUpConfig
from chengchuan.infrastructure.meoz_client import MeozClient, MeozStore, clean_error
from chengchuan.infrastructure.meoz_market import DAILY_FIELDS, parse_rows
from chengchuan.infrastructure.two_up_inputs import (
    DEVELOPMENT_SYMBOLS,
    HOLDOUT_SYMBOLS,
    UNIVERSE,
    positive_number,
)

START = date(2025, 1, 1)
END = date(2026, 11, 5)


def _write_new(path, value):
    encoded = json.dumps(value, default=str, ensure_ascii=False, indent=2, allow_nan=False) + "\n"
    with path.open("x", encoding="utf-8") as stream:
        stream.write(encoded)
    return hashlib.sha256(encoded.encode()).hexdigest()


def _symbol(raw):
    text = str(raw).lower()
    if "." in text:
        code, exchange = text.split(".")
        symbol = exchange + code
        if stock_symbol(code) != symbol:
            raise ValueError("证券代码交易所后缀不一致")
        return symbol
    return stock_symbol(text)


def validate_batch(rows, symbols):
    if len(rows) >= 6000:
        raise ValueError("响应达到6000行截断边界，拒绝作为完整研究输入")
    allowed = set(symbols)
    groups = {symbol: [] for symbol in symbols}
    seen = set()
    expected = set(TradingCalendar().sessions(START, END))
    for row in rows:
        symbol = _symbol(row["symbol"])
        if symbol not in allowed:
            raise ValueError("响应包含未请求的证券")
        day = datetime.strptime(str(row["tradedate"]), "%Y%m%d").date()
        if day not in expected:
            raise ValueError("响应日期不在已声明的交易日范围内")
        identity = symbol, day
        if identity in seen:
            raise ValueError("响应包含重复证券日期")
        seen.add(identity)
        groups[symbol].append(row)
    checks = []
    for symbol, values in groups.items():
        values.sort(key=lambda row: str(row["tradedate"]))
        bars, skipped = parse_rows(values, symbol)
        present = {b.timestamp.date() for b in bars}
        checks.append(
            dict(
                symbol=symbol,
                rows=len(values),
                valid_ohlc=len(bars),
                skipped_ohlc=skipped,
                expected_market_sessions=len(expected),
                missing_sessions=[d.isoformat() for d in sorted(expected - present)],
                missing_comparable_close=sum(
                    positive_number(row.get("pre_close")) is None for row in values
                ),
            )
        )
    return groups, checks


def collect(folder, fetch=False, store=None, client=None):
    store = store or MeozStore()
    client = client or MeozClient(store=store, offline=not fetch)
    folder = Path(folder)
    folder.mkdir(parents=True, exist_ok=True)
    now = datetime.now(SHANGHAI)
    run_dir = folder / ("collection-" + now.strftime("%Y%m%dT%H%M%S%f"))
    run_dir.mkdir()
    protocol = dict(
        protocol_version=1,
        declared_at=now.isoformat(),
        event_version=EVENT_VERSION,
        target_version=TARGET_VERSION,
        universe=UNIVERSE,
        development_only_symbols=DEVELOPMENT_SYMBOLS,
        final_holdout_symbols=HOLDOUT_SYMBOLS,
        source_start=START.isoformat(),
        source_end=END.isoformat(),
        training_target_before="2026-04-01",
        validation_origin_from="2026-04-01",
        validation_target_before="2026-07-01",
        final_origin_from="2026-07-01",
        final_target_before="2026-11-06",
        config=asdict(TwoUpConfig()),
        candidates=[
            "logistic_C0.1",
            "logistic_C1",
            "hist_gradient_depth2",
            "hist_gradient_depth3",
            "extra_trees_depth5",
            "majority",
        ],
        threshold_candidates=[0.55, 0.60, 0.65, 0.70, 0.75, 0.80, 0.85, 0.90],
        source_contract=dict(
            operation_id="reference_stock_daily",
            api="daily",
            service="live",
            document_version="1.0.52",
            url="https://meoz.cn/docs/query/stock/daily",
            previous_close="前复权可比昨收价",
        ),
        max_query_batches=3,
        max_symbols_per_batch=4,
        fetch=fetch,
        selection_note="预先固定的存续熟悉股票研究样本，非全A股代表或无幸存偏差样本",
        leakage_policy="同一日期所有股票同分区；最终新十股得分不用于候选或阈值选择",
        source_status="事后重建，无逐条原始接收时间；此脚本不查看事件结果或模型分数",
    )
    protocol_hash = _write_new(run_dir / "protocol.json", protocol)
    print(
        json.dumps(
            dict(event="frozen", directory=str(run_dir.resolve()), protocol_sha256=protocol_hash),
            ensure_ascii=False,
        ),
        flush=True,
    )
    before = store.usage(now)
    checks, batches = [], []
    error = None
    if fetch:
        try:
            for index in range(0, len(UNIVERSE), 4):
                symbols = tuple(symbol for symbol, _ in UNIVERSE[index : index + 4])
                result = client.query(
                    "daily",
                    dict(
                        symbols=",".join(symbol[-6:] for symbol in symbols),
                        startdate=START.strftime("%Y%m%d"),
                        enddate=END.strftime("%Y%m%d"),
                    ),
                    DAILY_FIELDS,
                    service="live",
                    max_age=None,
                )
                grouped, quality = validate_batch(result.rows, symbols)
                # Write the exact response before touching the mutable shared cache.
                name = f"batch-{index // 4 + 1}.json"
                sha = _write_new(
                    run_dir / name,
                    dict(
                        symbols=symbols,
                        fetched_at=result.fetched_at,
                        source=result.url,
                        rows=result.rows,
                    ),
                )
                batches.append(
                    dict(file=name, sha256=sha, rows=len(result.rows), cached=result.cached)
                )
                checks.extend(quality)
                for symbol, rows in grouped.items():
                    if not rows:
                        continue
                    existing = store.daily_history(symbol)
                    # Preserve newer cache-observation metadata when this bounded historical
                    # query did not observe the cache's latest date.
                    newer = (
                        existing
                        and existing[0]
                        and max(str(r["tradedate"]) for r in existing[0]) > END.strftime("%Y%m%d")
                    )
                    fetched = existing[1] if newer else result.fetched_at.timestamp()
                    checked = existing[2] if newer else END.strftime("%Y%m%d")
                    store.save_daily(symbol, rows, fetched, checked)
                print(
                    json.dumps(
                        dict(
                            event="batch_saved",
                            batch=index // 4 + 1,
                            rows=len(result.rows),
                            cached=result.cached,
                        )
                    ),
                    flush=True,
                )
        except (ValueError, OSError, RuntimeError) as exc:
            error = clean_error(exc)
    snapshots = []
    for symbol, name in UNIVERSE:
        source = store.daily_history(symbol)
        rows = (
            tuple(
                r
                for r in source[0]
                if START.strftime("%Y%m%d") <= str(r["tradedate"]) <= END.strftime("%Y%m%d")
            )
            if source
            else ()
        )
        _, quality = validate_batch(rows, (symbol,))
        sha = _write_new(
            run_dir / f"{symbol}-raw.json",
            dict(
                symbol=symbol,
                name=name,
                observed_at=datetime.fromtimestamp(source[1], SHANGHAI).isoformat()
                if source
                else None,
                rows=rows,
            ),
        )
        snapshots.append(
            dict(
                symbol=symbol,
                name=name,
                file=f"{symbol}-raw.json",
                sha256=sha,
                **{k: v for k, v in quality[0].items() if k != "symbol"},
            )
        )
    after = store.usage(now)
    added = after["total"] - before["total"]
    if added > 3:
        raise RuntimeError("研究数据收集超出已声明的三次请求预算")
    result = dict(
        protocol_sha256=protocol_hash,
        directory=str(run_dir.resolve()),
        requests_added=added,
        batches=batches,
        snapshots=snapshots,
        checks=checks,
        error=error,
        outcomes_evaluated=False,
    )
    _write_new(run_dir / "collection-summary.json", result)
    print(
        json.dumps(
            dict(
                event="complete",
                directory=str(run_dir.resolve()),
                requests_added=added,
                symbols=len(snapshots),
                rows=sum(r["rows"] for r in snapshots),
                error=error,
            ),
            ensure_ascii=False,
        ),
        flush=True,
    )
    return result


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--output", type=Path, default=Path(".chengchuan/two-up-validation"))
    parser.add_argument("--fetch", action="store_true", help="显式启用最多三批已核对日线查询")
    args = parser.parse_args()
    result = collect(args.output, args.fetch)
    return 2 if result["error"] else 0


if __name__ == "__main__":
    raise SystemExit(main())
