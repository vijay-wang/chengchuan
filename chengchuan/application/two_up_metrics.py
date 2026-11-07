"""An 80% target cannot be met by excluding failed or uncalled qualifying events."""

from collections import Counter
from random import Random
from statistics import mean

from chengchuan.application.two_up_learning import wilson


def _rate(a, b):
    return a / b if b else None


def summarize_two_up(predictions, evaluations):
    outcomes = {e.prediction_id: e for e in evaluations}
    within = [p for p in predictions if outcomes[p.record_id].status != "outside_boundary"]
    unknown = [p for p in within if p.status == "event_unknown"]
    events = [p for p in within if p.status not in ("not_event", "event_unknown")]
    called = [p for p in events if p.direction is not None]
    scored = [p for p in called if outcomes[p.record_id].correct is not None]
    actual = [p for p in events if outcomes[p.record_id].actual_direction is not None]
    correct = sum(outcomes[p.record_id].correct for p in scored)
    accuracy = _rate(correct, len(scored))
    coverage = _rate(len(called), len(events))
    interval = wilson(correct, len(scored))
    dates = sorted({p.target for p in scored})
    # Day blocks keep simultaneous stocks together; consecutive target-day blocks retain dependence.
    blocks = {
        d: [int(outcomes[p.record_id].correct) for p in scored if p.target == d] for d in dates
    }
    bootstrap = None
    if len(dates) >= 30:
        rng = Random(20261106)
        means = []
        for _ in range(300):
            drawn = []
            while len(drawn) < len(dates):
                i = rng.randrange(len(dates))
                drawn.extend(dates[i : i + 5])
            means.append(mean(hit for d in drawn[: len(dates)] for hit in blocks[d]))
        means.sort()
        bootstrap = (means[7], means[292])
    diagnostic = [p for p in actual if p.candidate_direction is not None]
    rows = []
    for month in sorted({p.as_of[:7] for p in events}):
        items = [p for p in events if p.as_of.startswith(month)]
        evaluated = [p for p in scored if p.as_of.startswith(month)]
        rows.append(
            dict(
                month=month,
                events=len(items),
                scored=len(evaluated),
                accuracy=_rate(
                    sum(outcomes[p.record_id].correct for p in evaluated), len(evaluated)
                ),
                coverage=_rate(sum(p.direction is not None for p in items), len(items)),
            )
        )
    by_stock = []
    for symbol in sorted({p.symbol for p in events}):
        items = [p for p in events if p.symbol == symbol]
        evaluated = [p for p in scored if p.symbol == symbol]
        by_stock.append(
            dict(
                symbol=symbol,
                events=len(items),
                scored=len(evaluated),
                accuracy=_rate(
                    sum(outcomes[p.record_id].correct for p in evaluated), len(evaluated)
                ),
                coverage=_rate(sum(p.direction is not None for p in items), len(items)),
            )
        )
    point = accuracy is not None and accuracy >= 0.8
    reliable = bool(
        point
        and len(scored) >= 100
        and len(scored) == len(called)
        and len(dates) >= 30
        and coverage >= 0.1
        and interval[0] >= 0.8
        and bootstrap
        and bootstrap[0] >= 0.8
    )
    return dict(
        candidate_days=len(within),
        confirmed_events=len(events),
        unknown_condition_days=len(unknown),
        not_event_days=sum(p.status == "not_event" for p in within),
        trained_events=sum(p.model_id is not None for p in events),
        called_events=len(called),
        scored_events=len(scored),
        correct=correct,
        accuracy=accuracy,
        coverage=coverage,
        label_coverage=_rate(len(actual), len(events)),
        called_label_coverage=_rate(len(scored), len(called)),
        unresolved_signals=len(called) - len(scored),
        conservative_accuracy=_rate(correct, len(called)),
        wilson95=interval,
        day_block95=bootstrap,
        unique_target_dates=len(dates),
        target_accuracy=0.8,
        point_target_met=point,
        reliability_gate_passed=reliable,
        acceptance="达到预设统计门槛，仍需前瞻验证" if reliable else "80%目标未达到或证据不足",
        always_up_accuracy=_rate(
            sum(outcomes[p.record_id].actual_direction == 1 for p in scored), len(scored)
        ),
        always_down_accuracy=_rate(
            sum(outcomes[p.record_id].actual_direction == -1 for p in scored), len(scored)
        ),
        diagnostic_candidate_accuracy=_rate(
            sum(
                outcomes[p.record_id].actual_direction == p.candidate_direction for p in diagnostic
            ),
            len(diagnostic),
        ),
        diagnostic_candidate_count=len(diagnostic),
        actual_classes=dict(Counter(outcomes[p.record_id].actual_direction for p in actual)),
        status_counts=dict(Counter(p.status for p in within)),
        monthly=rows,
        by_stock=by_stock,
    )
