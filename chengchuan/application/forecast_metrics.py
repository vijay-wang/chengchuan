"""诊断统计：分别报告分母、覆盖、简单基线和未校准概率损失。"""

from collections import Counter
from math import log
from random import Random
from statistics import mean


def _ratio(a, b):
    return a / b if b else None


def _block_interval(values):
    if len(values) < 20:
        return None
    rng = Random(20261103)
    replicates = []
    for _ in range(200):
        sample = []
        while len(sample) < len(values):
            i = rng.randrange(len(values))
            sample.extend(values[i : i + 5])
        replicates.append(mean(sample[: len(values)]))
    replicates.sort()
    return (replicates[4], replicates[194])


def summarize(predictions, evaluations):
    actuals = {r.prediction_id: r for r in evaluations}
    output = []
    for group, horizon in sorted({(r.group, r.horizon) for r in predictions}):
        rows = [r for r in predictions if (r.group, r.horizon) == (group, horizon)]
        # Out-of-bound target records are audited but are not forecast opportunities in this test.
        planned = [r for r in rows if actuals[r.record_id].status != "outside_boundary"]
        eligible = [r for r in planned if r.status != "insufficient_input"]
        trained = [r for r in eligible if r.raw_probabilities is not None]
        evaluated = [r for r in planned if actuals[r.record_id].actual_direction is not None]
        scored = [r for r in evaluated if r.direction is not None]
        hits = [int(actuals[r.record_id].correct) for r in scored]
        all_up = [int(actuals[r.record_id].actual_direction == 1) for r in scored]
        classes = Counter(actuals[r.record_id].actual_direction for r in evaluated)
        losses, logloss = [], []
        for r in evaluated:
            if r.raw_probabilities is None:
                continue
            d = actuals[r.record_id].actual_direction
            losses.append(
                sum((p - int(d == k)) ** 2 for p, k in zip(r.raw_probabilities, (-1, 0, 1)))
            )
            logloss.append(-log(r.raw_probabilities[d + 1]))
        calls = {d: [r for r in scored if r.direction == d] for d in (-1, 1)}

        def baseline(name):
            valid = [r for r in scored if getattr(r, name) is not None]
            return (
                mean(int(getattr(r, name) == actuals[r.record_id].actual_direction) for r in valid)
                if valid
                else None
            )

        calibration = []
        for index, lower in enumerate((0, 0.2, 0.4, 0.6, 0.8)):
            selected = [
                r
                for r in evaluated
                if r.raw_probabilities and min(4, int(r.raw_probabilities[2] * 5)) == index
            ]
            calibration.append(
                dict(
                    lower=lower,
                    count=len(selected),
                    mean_raw_up=mean(r.raw_probabilities[2] for r in selected)
                    if selected
                    else None,
                    actual_up=_ratio(
                        sum(actuals[r.record_id].actual_direction == 1 for r in selected),
                        len(selected),
                    ),
                )
            )
        output.append(
            dict(
                group=group,
                horizon=horizon,
                planned=len(planned),
                eligible=len(eligible),
                trained=len(trained),
                predicted=sum(r.direction is not None for r in eligible),
                evaluated=len(evaluated),
                scored=len(scored),
                correct=sum(hits),
                accuracy=mean(hits) if hits else None,
                accuracy_interval=_block_interval(hits),
                always_up_accuracy=mean(all_up) if all_up else None,
                majority_accuracy=baseline("majority_direction"),
                momentum_accuracy=baseline("momentum_direction"),
                paired_up_delta=mean(a - b for a, b in zip(hits, all_up)) if hits else None,
                paired_up_delta_interval=_block_interval([a - b for a, b in zip(hits, all_up)]),
                up_precision=_ratio(
                    sum(actuals[r.record_id].correct for r in calls[1]), len(calls[1])
                ),
                down_precision=_ratio(
                    sum(actuals[r.record_id].correct for r in calls[-1]), len(calls[-1])
                ),
                input_coverage=_ratio(len(eligible), len(planned)),
                forecast_coverage=_ratio(
                    sum(r.direction is not None for r in eligible), len(eligible)
                ),
                label_coverage=_ratio(len(evaluated), len(planned)),
                brier=mean(losses) if losses else None,
                log_loss=mean(logloss) if logloss else None,
                actual_classes={str(k): classes[k] for k in (-1, 0, 1)},
                calibration=calibration,
                status_counts=dict(Counter(r.status for r in rows)),
            )
        )
    # Same-date, same-target comparison avoids gains caused only by different abstention coverage.
    keys = {(r.as_of, r.horizon) for r in predictions}
    by_key = {(r.as_of, r.horizon, r.group): r for r in predictions}
    for summary in output:
        common = []
        groups = sorted({r.group for r in predictions})
        for day, h in sorted(keys):
            if h != summary["horizon"]:
                continue
            panel = [by_key.get((day, h, g)) for g in groups]
            if all(
                r and r.direction is not None and actuals[r.record_id].correct is not None
                for r in panel
            ):
                r = by_key[(day, h, summary["group"])]
                common.append(int(actuals[r.record_id].correct))
        summary["common_samples"] = len(common)
        summary["common_accuracy"] = mean(common) if common else None
    return output
