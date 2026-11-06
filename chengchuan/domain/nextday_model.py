"""Deterministic CPU ensemble; all transforms are fitted on supplied mature samples.

The fixed blend is 50% family-balanced nearest neighbours, 40% L2 logistic
regression and 10% training class frequency.  Its scores are diagnostic model
scores, NOT calibrated probabilities.  OHLC quantiles describe the selected
historical neighbours and do not form a calibrated future price interval.
"""

import hashlib
import json
from collections import Counter, defaultdict
from dataclasses import asdict
from heapq import nsmallest
from math import exp, isfinite, log, sqrt
from statistics import median, pstdev

from chengchuan.domain.nextday import NextDayEstimate

_RIDGE = 0.12
_ITERATIONS = 48
_CLIP = 5.0
_BLEND = (0.5, 0.4, 0.1)
_MAX_FEATURES = 128


def _quantile(values, level):
    ordered = sorted(values)
    location = (len(ordered) - 1) * level
    lower = int(location)
    fraction = location - lower
    upper = min(lower + 1, len(ordered) - 1)
    return ordered[lower] * (1 - fraction) + ordered[upper] * fraction


def _number(value):
    return isinstance(value, (int, float)) and not isinstance(value, bool) and isfinite(value)


def _validate(query, training, config):
    if not config.min_train <= len(training) <= config.train_window:
        raise ValueError("模型训练样本须满足最小样本量和训练窗口限制")
    names = tuple(name for name, _ in query)
    if not names or len(names) > _MAX_FEATURES or len(set(names)) != len(names):
        raise ValueError("特征须为1至128个唯一名称")
    if any(not isinstance(name, str) or not name or "_" not in name for name in names):
        raise ValueError("特征名称须包含特征族前缀")
    last_origin = None
    for values in (query, *(point.values for point in training)):
        if tuple(name for name, _ in values) != names:
            raise ValueError("训练与查询的特征名称、顺序必须一致")
        if any(value is not None and not _number(value) for _, value in values):
            raise ValueError("特征只接受有限数值或明确缺失值")
    for point in training:
        if last_origin is not None and point.origin <= last_origin:
            raise ValueError("训练样本须按基准日严格递增，不能重复")
        if point.target <= point.origin or point.available_at.utcoffset() is None:
            raise ValueError("训练标签日期或可获得时间无效")
        last_origin = point.origin
        returns = (point.open_return, point.high_return, point.low_return, point.close_return)
        if any(not _number(value) or value <= -100 for value in returns):
            raise ValueError("OHLC标签须为相对基准收盘的有限百分比收益")
        if point.high_return < max(point.open_return, point.close_return) or point.low_return > min(
            point.open_return, point.close_return
        ):
            raise ValueError("OHLC标签高低价顺序无效")
        if not isinstance(point.shape, str) or not point.shape:
            raise ValueError("走势标签不能为空")
    return names


def _prepare(query, training):
    """Drop query-missing/sparse/constant columns, then robustly scale past data.

    Each active family has unit total feature weight.  Missing training values
    become the training median for the linear fit; KNN instead uses an explicit
    missing-distance penalty.  No future rows or query values fit the scaler.
    """
    selected = []
    minimum = max(3, len(training) // 3)
    for index, (name, value) in enumerate(query):
        column = [point.values[index][1] for point in training]
        present = [entry for entry in column if entry is not None]
        if value is None or len(present) < minimum:
            continue
        center = median(present)
        scale = (_quantile(present, 0.75) - _quantile(present, 0.25)) / 1.349
        if scale <= 1e-12:
            scale = pstdev(present)
        if scale <= 1e-12:
            continue
        selected.append((index, name, center, scale))
    counts = Counter(name.split("_", 1)[0] for _, name, _, _ in selected)
    weights = tuple(1 / counts[name.split("_", 1)[0]] for _, name, _, _ in selected)

    def scale_row(values):
        return tuple(
            None
            if values[index][1] is None
            else max(-_CLIP, min(_CLIP, (values[index][1] - center) / scale))
            for index, _, center, scale in selected
        )

    return (
        tuple(name for _, name, _, _ in selected),
        weights,
        scale_row(query),
        tuple(scale_row(point.values) for point in training),
    )


def _logistic(rows, query, training, weights):
    """48 fixed batch steps for mean binary CE + 0.12/2 * ||w||².

    Flat labels are not arbitrarily assigned up/down.  Their smoothed training
    frequency is retained as a separate third score.  Intercept is unpenalised;
    the deterministic step uses a data-only curvature bound.
    """
    roots = tuple(sqrt(weight) for weight in weights)
    filled = [tuple((value or 0.0) * root for value, root in zip(row, roots)) for row in rows]
    query = tuple(value * root for value, root in zip(query, roots))
    samples = [(row, int(p.close_return > 0)) for row, p in zip(filled, training) if p.close_return]
    up = sum(label for _, label in samples)
    down = len(samples) - up
    intercept = log((up + 1) / (down + 1))
    coefficients = [0.0] * len(query)
    if samples:
        count = len(samples)
        curvature = 0.25 * sum(1 + sum(x * x for x in row) for row, _ in samples) / count
        step = min(1.0, 1 / (curvature + _RIDGE))
        columns = tuple(zip(*(row for row, _ in samples)))
        for _ in range(_ITERATIONS):
            residuals = []
            for row, label in samples:
                score = intercept + sum(w * x for w, x in zip(coefficients, row))
                estimate = 1 / (1 + exp(-max(-30.0, min(30.0, score))))
                residuals.append(estimate - label)
            intercept -= step * sum(residuals) / count
            coefficients = [
                weight
                - step * (sum(r * x for r, x in zip(residuals, col)) / count + _RIDGE * weight)
                for weight, col in zip(coefficients, columns)
            ]
    contributions = tuple(weight * value for weight, value in zip(coefficients, query))
    score = intercept + sum(contributions)
    conditional_up = 1 / (1 + exp(-max(-30.0, min(30.0, score))))
    flat = (len(training) - len(samples) + 1) / (len(training) + 3)
    return ((1 - flat) * (1 - conditional_up), flat, (1 - flat) * conditional_up), contributions


class CpuNextDayModel:
    version = "cpu-family-knn-logistic-v1-uncalibrated"

    def predict(self, query, training, config):
        query, training = tuple(query), tuple(training)
        names = _validate(query, training, config)
        active, weights, scaled_query, rows = _prepare(query, training)
        model_id = hashlib.sha256(
            json.dumps(
                [self.version, asdict(config), names, active, [asdict(p) for p in training]],
                default=str,
                sort_keys=True,
                allow_nan=False,
            ).encode()
        ).hexdigest()
        totals = tuple(
            sum((p.close_return > 0) - (p.close_return < 0) == d for p in training)
            for d in (-1, 0, 1)
        )
        base = tuple((count + 1) / (len(training) + 3) for count in totals)
        if not active:
            return NextDayEstimate(
                model_id,
                None,
                base,
                (("base", base[2]),),
                (),
                (),
                (),
                (),
                "查询与历史没有覆盖足够、可区分的共同特征，不给方向",
            )

        def distance(item):
            index, row = item
            families = defaultdict(list)
            for name, a, b in zip(active, scaled_query, row):
                families[name.split("_", 1)[0]].append(
                    9.0 if b is None else min(6.0, abs(a - b)) ** 2
                )
            return sum(sum(parts) / len(parts) for parts in families.values()), training[
                index
            ].origin

        nearest = nsmallest(config.neighbors, enumerate(rows), key=distance)
        points = [training[index] for index, _ in nearest]
        counts = tuple(
            sum((p.close_return > 0) - (p.close_return < 0) == d for p in points)
            for d in (-1, 0, 1)
        )
        analogue = tuple((count + 1) / (len(points) + 3) for count in counts)
        linear, contributions = _logistic(rows, scaled_query, training, weights)
        scores = tuple(
            sum(blend * model[i] for blend, model in zip(_BLEND, (analogue, linear, base)))
            for i in range(3)
        )
        order = sorted(range(3), key=lambda i: (-scores[i], i))
        winner, runner_up = order[:2]
        knn_signed, linear_signed = analogue[2] - analogue[0], linear[2] - linear[0]
        conflict = (
            knn_signed * linear_signed < 0
            and min(abs(knn_signed), abs(linear_signed)) >= config.vote_margin
        )
        if scores[winner] - scores[runner_up] <= config.vote_margin:
            direction, reason = None, "融合得分差距不足，不给方向"
        elif conflict:
            direction, reason = None, "相似日与线性模型方向分歧，不给方向"
        else:
            direction, reason = winner - 1, "CPU融合研究方向；得分未经概率校准"
        if len(active) < len(names):
            reason += f"；使用{len(active)}/{len(names)}个有覆盖且非恒定特征"
        ranges = tuple(
            (
                name,
                tuple(
                    _quantile([getattr(p, name + "_return") for p in points], q)
                    for q in (0.1, 0.5, 0.9)
                ),
            )
            for name in ("open", "high", "low", "close")
        )
        shapes = tuple(
            sorted(Counter(p.shape for p in points).items(), key=lambda item: (-item[1], item[0]))
        )
        ranked = sorted(zip(active, contributions), key=lambda item: (-abs(item[1]), item[0]))[:8]
        return NextDayEstimate(
            model_id,
            direction,
            scores,
            (("knn", analogue[2]), ("logistic", linear[2]), ("base", base[2])),
            ranges,
            shapes,
            tuple(p.origin.isoformat() for p in points),
            tuple(ranked),
            reason,
        )
