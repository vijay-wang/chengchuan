"""Fixed CPU walk-forward diagnostic on existing local caches; makes no API requests.

Run with ``python -m scripts.check_nextday_research``.  The two instruments,
period, feature groups and model settings are declared before any outcome is
evaluated.  These previously studied instruments are development diagnostics,
not an independent holdout or a representative A-share portfolio.
"""

import argparse
import hashlib
import json
import os
import time
from dataclasses import asdict
from datetime import date, datetime
from pathlib import Path
from unittest.mock import patch

os.environ["QT_QPA_PLATFORM"] = "offscreen"

from chengchuan.application.forecast import digest  # noqa: E402
from chengchuan.application.nextday import NextDayService  # noqa: E402
from chengchuan.domain.models import (  # noqa: E402
    SHANGHAI,
    MarketSeries,
    MarketSnapshot,
    SeriesKind,
)
from chengchuan.domain.nextday import (  # noqa: E402
    FEATURE_VERSION,
    TARGET_VERSION,
    NextDayConfig,
)
from chengchuan.domain.nextday_model import CpuNextDayModel  # noqa: E402
from chengchuan.infrastructure.forecast_journal import SqliteForecastJournal  # noqa: E402
from chengchuan.infrastructure.meoz_client import MeozStore  # noqa: E402
from chengchuan.infrastructure.meoz_market import parse_rows  # noqa: E402
from chengchuan.infrastructure.nextday_inputs import CachedNextDayInputs  # noqa: E402
from chengchuan.infrastructure.nextday_reports import write_run  # noqa: E402

SYMBOLS = (("sz002318", "久立特材"), ("sz300750", "宁德时代"))
START = date(2026, 1, 5)
BEFORE = date(2026, 11, 6)
LATEST = date(2026, 11, 5)
GROUPS = ("price", "volume", "full")
SOURCE_FILES = (
    "chengchuan/domain/nextday.py",
    "chengchuan/domain/nextday_model.py",
    "chengchuan/application/nextday.py",
    "chengchuan/application/nextday_features.py",
    "chengchuan/application/nextday_metrics.py",
    "chengchuan/infrastructure/nextday_inputs.py",
    "scripts/check_nextday_research.py",
)


def _save(path, value):
    path.write_text(
        json.dumps(value, default=str, ensure_ascii=False, indent=2, allow_nan=False) + "\n",
        encoding="utf-8",
    )


def _hashes():
    root = Path(__file__).resolve().parents[1]
    return {name: hashlib.sha256((root / name).read_bytes()).hexdigest() for name in SOURCE_FILES}


def check(folder):
    folder = Path(folder)
    folder.mkdir(parents=True, exist_ok=True)
    config = NextDayConfig(neighbors=21, min_train=60, train_window=240, vote_margin=0.08)
    now = datetime.now(SHANGHAI)
    if now.date() < BEFORE:
        raise ValueError("本诊断的固定评估上界尚未到达")
    frozen = dict(
        declared_at=now.isoformat(),
        symbols=SYMBOLS,
        start=START.isoformat(),
        before=BEFORE.isoformat(),
        latest_prediction_origin=LATEST.isoformat(),
        groups=GROUPS,
        config=asdict(config),
        model_version=CpuNextDayModel.version,
        feature_version=FEATURE_VERSION,
        target_version=TARGET_VERSION,
        code_sha256=_hashes(),
        compute="CPU/stdlib",
        data="existing local cache; no API; reconstructed historical raw prices",
        experiment_grade="development_walk_forward_not_independent_holdout",
        excluded_dates_policy="No newly selected exclusions after observing scores; raw-price caveat retained",
    )
    run_dir = folder / ("run-" + now.strftime("%Y%m%dT%H%M%S%f"))
    run_dir.mkdir()
    _save(run_dir / "frozen-declaration.json", frozen)
    print(
        json.dumps(dict(event="frozen", path=str(run_dir.resolve())), ensure_ascii=False),
        flush=True,
    )
    store = MeozStore()
    usage_before = store.usage(now)
    cached = CachedNextDayInputs(store)
    engine = NextDayService(
        journal=SqliteForecastJournal(folder / "research.sqlite"),
        minute_loader=cached.minutes,
        context_loader=cached.context,
    )
    result = dict(
        frozen=frozen,
        run_directory=str(run_dir.resolve()),
        stocks=[],
        skipped=[],
        compute="CPU/stdlib",
        requests_added=None,
    )
    started = time.perf_counter()
    # Guard future accidental networking as well as asserting shared API usage.
    with patch("socket.create_connection", side_effect=RuntimeError("离线验证禁止网络连接")):
        for symbol, name in SYMBOLS:
            source = store.daily_history(symbol)
            if not source:
                result["skipped"].append(dict(symbol=symbol, reason="本地日线缓存缺失"))
                continue
            bars, rejected = parse_rows(source[0], symbol)
            bars = tuple(b for b in bars if b.timestamp.date() <= LATEST)
            if not bars:
                result["skipped"].append(dict(symbol=symbol, reason="固定上界之前无有效日线"))
                continue
            series = MarketSeries(symbol, name, "day", SeriesKind.OHLC, bars)
            snapshot = MarketSnapshot(
                digest(asdict(series)),
                series,
                quality_notes=("不复权日线；事后历史重建；未系统排除除权除息。",),
                source="猫爪本地日线缓存（离线诊断）",
            )
            record = dict(
                symbol=symbol,
                name=name,
                source_rows=len(bars),
                rejected_rows=rejected,
                first=bars[0].timestamp.date().isoformat(),
                last=bars[-1].timestamp.date().isoformat(),
                modes={},
            )
            for mode in ("close", "cut1435"):
                print(json.dumps(dict(event="backtest", symbol=symbol, mode=mode)), flush=True)
                mode_start = time.perf_counter()
                run = engine.backtest(
                    snapshot,
                    before=BEFORE,
                    config=config,
                    start=START,
                    groups=GROUPS,
                    now=now,
                    mode=mode,
                )
                paths = write_run(run, run_dir / f"{symbol}-{mode}-backtest")
                latest = engine.predict(snapshot, as_of=LATEST, config=config, now=now, mode=mode)
                latest_paths = write_run(latest, run_dir / f"{symbol}-{mode}-latest")
                record["modes"][mode] = dict(
                    elapsed_seconds=round(time.perf_counter() - mode_start, 3),
                    experiment_id=run.experiment_id,
                    manifest=run.manifest,
                    summaries=run.summaries,
                    latest=asdict(latest.predictions[0]),
                    latest_features=asdict(latest.features),
                    reports=paths,
                    latest_reports=latest_paths,
                )
                brief = [
                    {
                        k: s[k]
                        for k in (
                            "group",
                            "scored",
                            "accuracy",
                            "always_up_accuracy",
                            "opportunity_coverage",
                        )
                    }
                    for s in run.summaries
                ]
                print(
                    json.dumps(
                        dict(event="completed", symbol=symbol, mode=mode, summaries=brief),
                        ensure_ascii=False,
                    ),
                    flush=True,
                )
            result["stocks"].append(record)
            _save(run_dir / "partial-summary.json", result)
    usage_after = store.usage(now)
    if usage_before != usage_after:
        raise AssertionError("离线预测验证不应消耗API次数")
    result.update(
        elapsed_seconds=round(time.perf_counter() - started, 3),
        requests_added=0,
        code_unchanged_since_declaration=_hashes() == frozen["code_sha256"],
        code_sha256_after=_hashes(),
    )
    _save(run_dir / "summary.json", result)
    _save(folder / "latest-summary.json", result)
    return dict(
        summary=str((run_dir / "summary.json").resolve()),
        elapsed_seconds=result["elapsed_seconds"],
        requests_added=0,
        stocks=len(result["stocks"]),
        skipped=result["skipped"],
        code_unchanged_since_declaration=result["code_unchanged_since_declaration"],
    )


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--output", type=Path, default=Path(".chengchuan/nextday-validation"))
    args = parser.parse_args()
    print(json.dumps(check(args.output), ensure_ascii=False, indent=2), flush=True)
