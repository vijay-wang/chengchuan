"""Matched-sample diagnostics, coverage, OHLC scenario errors and date-block intervals."""

from collections import Counter
from random import Random
from statistics import mean


def ratio(a, b):
    return a / b if b else None


def block_interval(sequence):
    """Five-origin blocks preserve abstention gaps; exploratory, single-symbol only."""
    if sum(v is not None for v in sequence) < 20:
        return None
    rng = Random(20261106)
    replicates = []
    for _ in range(300):
        sample = []
        while len(sample) < len(sequence):
            start = rng.randrange(len(sequence))
            sample.extend(sequence[start : start + 5])
        values = [v for v in sample[: len(sequence)] if v is not None]
        if values:
            replicates.append(mean(values))
    replicates.sort()
    return (replicates[int(len(replicates) * 0.025)], replicates[int(len(replicates) * 0.975)])


def summarize(predictions, evaluations):
    actuals = {e.prediction_id: e for e in evaluations}
    groups = sorted({p.group for p in predictions})
    rows_by_key = {(p.as_of, p.group): p for p in predictions}
    common = {
        day
        for day, _ in rows_by_key
        if all(
            (row := rows_by_key.get((day, group))) is not None
            and row.direction is not None
            and actuals[row.record_id].correct is not None
            for group in groups
        )
    }
    output = []
    for group in groups:
        rows = sorted((p for p in predictions if p.group == group), key=lambda p: p.as_of)
        planned = [p for p in rows if actuals[p.record_id].status != "outside_boundary"]
        eligible = [
            p for p in planned if p.status not in ("insufficient_input", "calendar_unavailable")
        ]
        trained = [p for p in planned if p.scores is not None]
        evaluated = [p for p in planned if actuals[p.record_id].actual_direction is not None]
        scored = [p for p in evaluated if p.direction is not None]
        trained_actual = [p for p in evaluated if p.scores is not None]
        hit = {p.record_id: int(actuals[p.record_id].correct) for p in scored}
        delta = {
            p.record_id: hit[p.record_id] - int(actuals[p.record_id].actual_direction == 1)
            for p in scored
        }

        def accuracy(name):
            valid = [p for p in scored if getattr(p, name) is not None]
            return ratio(
                sum(getattr(p, name) == actuals[p.record_id].actual_direction for p in valid),
                len(valid),
            )

        recalls, opportunity_recalls, classes = [], [], {}
        for d in (-1, 0, 1):
            called = [p for p in scored if p.direction == d]
            actual = [p for p in scored if actuals[p.record_id].actual_direction == d]
            evaluated_actual = [p for p in evaluated if actuals[p.record_id].actual_direction == d]
            recall = ratio(sum(hit[p.record_id] for p in actual), len(actual))
            opportunity_recall = ratio(sum(hit[p.record_id] for p in actual), len(evaluated_actual))
            classes[str(d)] = dict(
                called=len(called),
                actual=len(actual),
                evaluated_actual=len(evaluated_actual),
                precision=ratio(sum(hit[p.record_id] for p in called), len(called)),
                recall=recall,
                prediction_coverage=ratio(len(actual), len(evaluated_actual)),
                abstention_as_miss_recall=opportunity_recall,
            )
            if recall is not None:
                recalls.append(recall)
            if opportunity_recall is not None:
                opportunity_recalls.append(opportunity_recall)
        errors = {}
        for name in ("open", "high", "low", "close"):
            comparable = [
                (dict(p.ranges)[name], dict(actuals[p.record_id].actual_ohlc)[name])
                for p in trained_actual
                if name in dict(p.ranges)
            ]
            errors[name] = dict(
                count=len(comparable),
                median_mae_pct=mean(abs(q[1] - actual) for q, actual in comparable)
                if comparable
                else None,
                q10_q90_coverage=mean(q[0] <= actual <= q[2] for q, actual in comparable)
                if comparable
                else None,
                mean_width_pct=mean(q[2] - q[0] for q, _ in comparable) if comparable else None,
            )
        months = []
        for month in sorted({p.as_of[:7] for p in planned}):
            opportunities = [p for p in planned if p.as_of.startswith(month)]
            chosen = [p for p in scored if p.as_of.startswith(month)]
            months.append(
                dict(
                    month=month,
                    planned=len(opportunities),
                    scored=len(chosen),
                    accuracy=ratio(sum(hit[p.record_id] for p in chosen), len(chosen)),
                    coverage=ratio(len(chosen), len(opportunities)),
                )
            )
        common_rows = [p for p in scored if p.as_of in common]
        shape_rows = [p for p in trained_actual if actuals[p.record_id].shape_correct is not None]
        summary = dict(
            group=group,
            planned=len(planned),
            eligible=len(eligible),
            trained=len(trained),
            predicted=sum(p.direction is not None for p in planned),
            evaluated=len(evaluated),
            scored=len(scored),
            correct=sum(hit.values()),
            accuracy=ratio(sum(hit.values()), len(scored)),
            accuracy_interval=block_interval([hit.get(p.record_id) for p in planned]),
            always_up_accuracy=ratio(
                sum(actuals[p.record_id].actual_direction == 1 for p in scored), len(scored)
            ),
            majority_accuracy=accuracy("majority_direction"),
            momentum_accuracy=accuracy("momentum_direction"),
            paired_up_delta=mean(delta.values()) if delta else None,
            paired_up_delta_interval=block_interval([delta.get(p.record_id) for p in planned]),
            forecast_coverage=ratio(sum(p.direction is not None for p in eligible), len(eligible)),
            opportunity_coverage=ratio(len(scored), len(planned)),
            input_coverage=ratio(len(eligible), len(planned)),
            label_coverage=ratio(len(evaluated), len(planned)),
            common_samples=len(common_rows),
            common_accuracy=ratio(sum(hit[p.record_id] for p in common_rows), len(common_rows)),
            balanced_accuracy=mean(recalls) if recalls else None,
            # Compatibility alias above is selective: it excludes abstentions.
            selective_balanced_accuracy=mean(recalls) if recalls else None,
            abstention_as_miss_balanced_recall=mean(opportunity_recalls)
            if opportunity_recalls
            else None,
            evaluated_class_counts={d: values["evaluated_actual"] for d, values in classes.items()},
            class_prediction_coverage={
                d: values["prediction_coverage"] for d, values in classes.items()
            },
            classes=classes,
            all_trained_always_up_accuracy=ratio(
                sum(actuals[p.record_id].actual_direction == 1 for p in trained_actual),
                len(trained_actual),
            ),
            shape_accuracy=ratio(
                sum(actuals[p.record_id].shape_correct for p in shape_rows), len(shape_rows)
            ),
            shape_count=len(shape_rows),
            ohlc_errors=errors,
            monthly=months,
            status_counts=dict(Counter(p.status for p in rows)),
            independent_holdout_passed=False,
        )
        output.append(summary)
    return output
