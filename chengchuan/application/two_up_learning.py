"""Small frozen CPU candidate grid; selection and abstention use validation only."""

from collections import Counter
from dataclasses import asdict, dataclass
from math import isfinite, sqrt

from chengchuan.application.forecast import digest
from chengchuan.domain.forecast import direction

VERSION = "exact-two-up-cpu-selection-v2"
THRESHOLDS = (0.55, 0.60, 0.65, 0.70, 0.75, 0.80, 0.85, 0.90)
CANDIDATES = (
    "logistic_01",
    "logistic_1",
    "boost_depth2",
    "boost_depth3",
    "extra_trees",
    "majority",
)


def wilson(correct, count):
    if not count:
        return None
    z, p = 1.959963984540054, correct / count
    denominator = 1 + z * z / count
    center = (p + z * z / (2 * count)) / denominator
    half = z * sqrt(p * (1 - p) / count + z * z / (4 * count * count)) / denominator
    return (max(0.0, center - half), min(1.0, center + half))


def _matrix(rows, names):
    result = []
    for row in rows:
        values = row.values if hasattr(row, "values") else row
        if tuple(k for k, _ in values) != names:
            raise ValueError("训练与预测的特征名称／顺序不一致")
        if any(
            v is not None
            and (isinstance(v, bool) or not isinstance(v, (int, float)) or not isfinite(v))
            for _, v in values
        ):
            raise ValueError("特征包含非法数值")
        result.append([float("nan") if v is None else v for _, v in values])
    return result


def _models():
    try:
        from sklearn.dummy import DummyClassifier
        from sklearn.ensemble import ExtraTreesClassifier, HistGradientBoostingClassifier
        from sklearn.impute import SimpleImputer
        from sklearn.linear_model import LogisticRegression
        from sklearn.pipeline import make_pipeline
        from sklearn.preprocessing import StandardScaler
    except ImportError as exc:
        raise ValueError(
            "连涨两天模型需要CPU依赖，请安装 pip install '.[prediction]'；未使用GPU"
        ) from exc
    seed = 20261106
    return (
        make_pipeline(
            SimpleImputer(keep_empty_features=True),
            StandardScaler(),
            LogisticRegression(C=0.1, max_iter=400, random_state=seed),
        ),
        make_pipeline(
            SimpleImputer(keep_empty_features=True),
            StandardScaler(),
            LogisticRegression(C=1, max_iter=400, random_state=seed),
        ),
        HistGradientBoostingClassifier(
            max_depth=2,
            max_iter=100,
            min_samples_leaf=20,
            l2_regularization=1,
            early_stopping=False,
            random_state=seed,
        ),
        HistGradientBoostingClassifier(
            max_depth=3,
            max_iter=100,
            min_samples_leaf=20,
            l2_regularization=1,
            early_stopping=False,
            random_state=seed,
        ),
        make_pipeline(
            SimpleImputer(keep_empty_features=True),
            ExtraTreesClassifier(
                n_estimators=200, max_depth=5, min_samples_leaf=15, n_jobs=2, random_state=seed
            ),
        ),
        make_pipeline(SimpleImputer(keep_empty_features=True), DummyClassifier(strategy="prior")),
    )


@dataclass
class FittedSignal:
    model: object
    names: tuple[str, ...]
    model_name: str
    model_id: str
    threshold: float | None
    train_count: int
    validation_count: int
    validation_summary: dict
    candidate_results: tuple[dict, ...]

    def predict_many(self, values):
        from threadpoolctl import threadpool_limits

        matrix = _matrix(values, self.names)
        if not matrix:
            return ()
        with threadpool_limits(limits=2):
            scores = self.model.predict_proba(matrix)
        classes = self.model.classes_
        return tuple((int(classes[int(row.argmax())]), float(row.max())) for row in scores)

    def predict(self, values):
        return self.predict_many((values,))[0]


def fit_signal(training, validation, config, validation_events=None):
    if len(training) < config.min_train or len(validation) < config.min_validation:
        raise ValueError(
            f"事件训练／验证样本不足：{len(training)}/{len(validation)}，要求{config.min_train}/{config.min_validation}"
        )
    validation_events = len(validation) if validation_events is None else validation_events
    if type(validation_events) is not int or validation_events < len(validation):
        raise ValueError("验证覆盖分母必须包含全部有标签样本及其他已成立事件")
    first_validation = min(p.origin for p in validation)
    if max(p.target for p in training) >= first_validation:
        raise ValueError("训练标签跨入验证起点，须按交易日期清理边界")
    if any(
        p.available_at.utcoffset() is None or p.available_at.date() >= first_validation
        for p in training
    ):
        raise ValueError("训练标签未在验证起点前成熟")
    combined = tuple(training) + tuple(validation)
    if len({(p.symbol, p.origin) for p in combined}) != len(combined):
        raise ValueError("事件样本重复或训练与验证交叉")
    if any(
        not isfinite(p.return_pct) or p.return_pct <= -100 or p.target <= p.origin for p in combined
    ):
        raise ValueError("第三日标签无效")
    names = tuple(k for k, _ in training[0].values)
    if not names or len(names) != len(set(names)) or len(names) > 128:
        raise ValueError("特征名称必须唯一且数量为1至128")
    x, xv = _matrix(training, names), _matrix(validation, names)
    y, yv = (
        [direction(p.return_pct) for p in training],
        [direction(p.return_pct) for p in validation],
    )
    models = _models()
    from threadpoolctl import threadpool_limits

    # A one-class early history cannot fit logistic; report it instead of inventing certainty.
    if len(set(y)) < 2:
        raise ValueError("训练事件只有一种结果，无法验证涨跌区分能力")
    results, fitted, eligible = [], [], []
    for order, (name, model) in enumerate(zip(CANDIDATES, models)):
        with threadpool_limits(limits=2):
            model.fit(x, y)
            raw = model.predict_proba(xv)
        calls = [(int(model.classes_[int(row.argmax())]), float(row.max())) for row in raw]
        hits = sum(d == actual for (d, _), actual in zip(calls, yv))
        options = []
        for threshold in THRESHOLDS:
            chosen = [(d == actual) for (d, score), actual in zip(calls, yv) if score >= threshold]
            count, correct = len(chosen), sum(chosen)
            item = dict(
                threshold=threshold,
                count=count,
                correct=correct,
                coverage=count / validation_events,
                eligible_sample_coverage=count / len(yv),
                accuracy=correct / count if count else None,
                wilson95=wilson(correct, count),
            )
            options.append(item)
            if (
                count >= config.min_signals
                and count / validation_events >= config.min_coverage
                and correct / count >= config.target_accuracy
            ):
                eligible.append((count, item["wilson95"][0], -order, -threshold, order, item))
        results.append(
            dict(
                model=name,
                all_accuracy=hits / len(yv),
                validation_count=len(yv),
                thresholds=options,
            )
        )
        fitted.append(model)
    if eligible:
        _, _, _, _, index, selection = max(eligible, key=lambda r: r[:4])
        threshold = selection["threshold"]
        summary = {**selection, "status": "validation_gate_only", "heldout_verified": False}
    else:
        index = max(range(len(results)), key=lambda i: (results[i]["all_accuracy"], -i))
        threshold = None
        summary = dict(
            status="no_80_evidence",
            threshold=None,
            count=0,
            accuracy=None,
            coverage=0,
            heldout_verified=False,
            reason="预先固定的候选模型和阈值均未通过验证集80%及样本／覆盖门槛",
        )
    summary.update(
        validation_count=len(validation),
        validation_events=validation_events,
        usable_sample_coverage=len(validation) / validation_events,
        class_counts=dict(Counter(yv)),
    )
    model_id = digest(
        [
            VERSION,
            asdict(config),
            names,
            [asdict(p) for p in training],
            [asdict(p) for p in validation],
            CANDIDATES[index],
            threshold,
            validation_events,
        ]
    )
    return FittedSignal(
        fitted[index],
        names,
        CANDIDATES[index],
        model_id,
        threshold,
        len(training),
        len(validation),
        summary,
        tuple(results),
    )
