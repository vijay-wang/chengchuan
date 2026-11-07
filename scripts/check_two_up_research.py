"""Frozen train/validation/final experiment on the declared exact-two-up panel."""

import argparse
import hashlib
import json
import time
from dataclasses import asdict
from datetime import date, datetime
from pathlib import Path

from chengchuan.application.forecast import digest
from chengchuan.application.two_up import NOTES, prepare_history
from chengchuan.application.two_up_learning import CANDIDATES, THRESHOLDS, VERSION, fit_signal
from chengchuan.application.two_up_metrics import summarize_two_up
from chengchuan.domain.calendar import TradingCalendar
from chengchuan.domain.forecast import direction
from chengchuan.domain.models import SHANGHAI, MarketSeries, MarketSnapshot, SeriesKind
from chengchuan.domain.two_up import (
    ComparableHistory,
    TwoUpConfig,
    TwoUpEvaluation,
    TwoUpPrediction,
    TwoUpRun,
)
from chengchuan.infrastructure.forecast_journal import SqliteForecastJournal
from chengchuan.infrastructure.meoz_client import MeozStore
from chengchuan.infrastructure.meoz_market import parse_rows
from chengchuan.infrastructure.nextday_inputs import CachedNextDayInputs
from chengchuan.infrastructure.two_up_inputs import HOLDOUT_SYMBOLS, positive_number
from chengchuan.infrastructure.two_up_reports import write_run


def dump(path, value):
    path.write_text(
        json.dumps(value, ensure_ascii=False, default=str, indent=2, allow_nan=False) + "\n",
        encoding="utf-8",
    )


def run(collection, folder):
    now = datetime.now(SHANGHAI)
    started = time.perf_counter()
    folder = Path(folder)
    folder.mkdir(parents=True, exist_ok=True)
    output = folder / ("frozen-" + now.strftime("%Y%m%dT%H%M%S%f"))
    output.mkdir()
    collection = Path(collection)
    config = TwoUpConfig()
    calendar = TradingCalendar()
    before = date(2026, 11, 6)
    if now.date() < before:
        raise ValueError("固定测试上界尚未到达")
    code_files = (
        "chengchuan/domain/two_up.py",
        "chengchuan/application/two_up.py",
        "chengchuan/application/two_up_learning.py",
        "chengchuan/application/two_up_metrics.py",
        "chengchuan/application/nextday_features.py",
        "scripts/check_two_up_research.py",
    )
    hashes = {p: hashlib.sha256(Path(p).read_bytes()).hexdigest() for p in code_files}
    manifest = dict(
        task="frozen_final_exact_two_up",
        declared_at=now.isoformat(),
        config=asdict(config),
        model_version=VERSION,
        candidates=CANDIDATES,
        thresholds=THRESHOLDS,
        code_sha256=hashes,
        protocol=json.loads((collection / "protocol.json").read_text()),
        holdout_symbols=HOLDOUT_SYMBOLS,
        training_target_before="2026-04-01",
        validation_origin_from="2026-04-01",
        validation_target_before="2026-07-01",
        test_origin_from="2026-07-01",
        test_target_before="2026-11-06",
        acceptance="point>=80%, scored>=100, distinct target dates>=30, coverage>=10%, Wilson and date-block lower bounds>=80%",
        inputs={
            p.name: hashlib.sha256(p.read_bytes()).hexdigest()
            for p in collection.glob("*-raw.json")
        },
    )
    dump(output / "declaration.json", manifest)
    print(json.dumps(dict(event="frozen", output=str(output))), flush=True)
    store = MeozStore()
    usage = store.usage(now)
    histories = []
    for path in sorted(collection.glob("*-raw.json")):
        obj = json.loads(path.read_text())
        symbol = obj["symbol"]
        raw = obj["rows"]
        bars, _ = parse_rows(raw, symbol)
        if not bars:
            raise ValueError("预先登记的证券缺少数据：" + symbol)
        series = MarketSeries(symbol, obj["name"], "day", SeriesKind.OHLC, bars)
        snapshot = MarketSnapshot(digest(asdict(series)), series, source="冻结猫爪可比昨收日线档案")
        snapshot = CachedNextDayInputs(store).context(snapshot)
        snapshot = MarketSnapshot(
            digest(asdict(snapshot)),
            snapshot.series,
            benchmark=snapshot.benchmark,
            quality_notes=snapshot.quality_notes,
            source=snapshot.source,
        )
        previous = tuple(
            (datetime.strptime(str(r["tradedate"]), "%Y%m%d").date(), v)
            for r in raw
            if (v := positive_number(r.get("pre_close"))) is not None
        )
        histories.append(ComparableHistory(snapshot, previous, now))
    # Persist exact benchmark and price inputs, too; the mutable API cache is never a hidden dependency.
    dump(output / "sources.json", [asdict(h) for h in histories])
    train = []
    validation = []
    validation_events = 0
    for h in histories:
        states, _, samples = prepare_history(h, date(2026, 6, 30), config)
        validation_events += sum(
            state.status not in ("not_event", "event_unknown")
            and day >= date(2026, 4, 1)
            and calendar.shift(day, 1) < date(2026, 7, 1)
            for day, state in states.items()
        )
        train.extend(p for p in samples if p.target < date(2026, 4, 1))
        validation.extend(
            p for p in samples if p.origin >= date(2026, 4, 1) and p.target < date(2026, 7, 1)
        )

    def key(p):
        return (p.origin, p.symbol)

    train, validation = tuple(sorted(train, key=key)), tuple(sorted(validation, key=key))
    fitted = fit_signal(train, validation, config, validation_events=validation_events)
    selected = dict(
        model=fitted.model_name,
        model_id=fitted.model_id,
        threshold=fitted.threshold,
        train_count=len(train),
        validation_count=len(validation),
        validation_summary=fitted.validation_summary,
        candidates=fitted.candidate_results,
    )
    dump(output / "selection-before-final.json", selected)
    print(
        json.dumps(
            dict(
                event="selected_before_final",
                model=fitted.model_name,
                threshold=fitted.threshold,
                train=len(train),
                validation=len(validation),
            )
        ),
        flush=True,
    )
    predictions = []
    truths = {}
    for h in histories:
        symbol = h.snapshot.series.symbol
        if symbol not in HOLDOUT_SYMBOLS:
            continue
        states, features, samples = prepare_history(h, date(2026, 11, 5), config)
        for sample in samples:
            truths[(symbol, sample.origin)] = sample
        for day, state in states.items():
            if day < date(2026, 7, 1):
                continue
            target = calendar.shift(day, 1)
            f = features.get(day)
            revision = f.revision if f else digest(state)
            record = dict(
                record_id=digest([fitted.model_id, symbol, day, revision]),
                symbol=symbol,
                as_of=str(day),
                target=str(target),
                status=state.status,
                reason=state.reason,
                feature_revision=revision,
                event_returns=state.returns,
            )
            if state.status == "confirmed":
                if f is None or not f.eligible:
                    record.update(status="insufficient_input", reason="条件成立，但指标历史不足")
                else:
                    candidate, score = fitted.predict(f.values)
                    called = fitted.threshold is not None and score >= fitted.threshold
                    record.update(
                        status="research_signal"
                        if called
                        else "no_80_evidence"
                        if fitted.threshold is None
                        else "abstained",
                        reason="固定训练／验证选择，最终测试不再调参",
                        direction=candidate if called else None,
                        candidate_direction=candidate,
                        score=score,
                        threshold=fitted.threshold,
                        model_id=fitted.model_id,
                        model_name=fitted.model_name,
                        train_count=len(train),
                        validation_count=len(validation),
                        validation_summary=fitted.validation_summary,
                        training_label_through=max(
                            p.available_at for p in train + validation
                        ).isoformat(),
                    )
            predictions.append(TwoUpPrediction(**record))
    predictions = tuple(predictions)
    manifest.update(selection=selected)
    journal = SqliteForecastJournal(output / "research.sqlite")
    for h in histories:
        journal.predictions(manifest, h.snapshot, ())
    journal.predictions(manifest, histories[0].snapshot, predictions)
    # Scores are revealed only after immutable final-test predictions have been persisted.
    evaluations = []
    for p in predictions:
        sample = truths.get((p.symbol, date.fromisoformat(p.as_of)))
        if date.fromisoformat(p.target) >= before:
            e = TwoUpEvaluation(p.record_id, status="outside_boundary")
        elif p.status in ("not_event", "event_unknown"):
            e = TwoUpEvaluation(p.record_id, status="not_applicable")
        elif sample is None:
            e = TwoUpEvaluation(p.record_id, status="unavailable")
        else:
            actual = direction(sample.return_pct)
            e = TwoUpEvaluation(
                p.record_id,
                sample.return_pct,
                actual,
                p.direction == actual if p.direction is not None else None,
            )
        evaluations.append(e)
    evaluations = tuple(evaluations)
    journal.evaluations(digest(manifest), evaluations)
    summary = summarize_two_up(predictions, evaluations)
    report = TwoUpRun(
        digest(manifest),
        manifest,
        None,
        predictions,
        evaluations,
        (summary,),
        NOTES,
        now.isoformat(),
    )
    paths = write_run(report, output / "final-test")
    result = dict(
        output=str(output),
        selected=selected,
        summary=summary,
        files=paths,
        elapsed_seconds=round(time.perf_counter() - started, 3),
        requests_added=0,
        code_unchanged=all(
            hashlib.sha256(Path(p).read_bytes()).hexdigest() == sha for p, sha in hashes.items()
        ),
    )
    if usage != store.usage(now):
        raise AssertionError("离线模型验证不应消耗API请求")
    dump(output / "result.json", result)
    dump(folder / "latest-result.json", result)
    print(
        json.dumps(
            dict(
                event="completed",
                summary=summary,
                elapsed_seconds=result["elapsed_seconds"],
                code_unchanged=result["code_unchanged"],
            ),
            ensure_ascii=False,
        ),
        flush=True,
    )
    return result


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--collection", type=Path, required=True)
    parser.add_argument("--output", type=Path, default=Path(".chengchuan/two-up-validation"))
    args = parser.parse_args()
    run(args.collection, args.output)
