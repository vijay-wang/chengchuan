"""预测研究用例：截至D训练，先存预测再揭晓目标；不自动启用校准概率。"""

import hashlib
import json
from dataclasses import asdict, dataclass, field, replace
from datetime import date, datetime, time, timezone
from typing import Protocol

from chengchuan.application.forecast_features import bar_date, history_at
from chengchuan.application.forecast_metrics import summarize
from chengchuan.domain.calendar import CalendarUnavailable
from chengchuan.domain.forecast import (
    FEATURE_VERSION,
    GROUPS,
    TARGET_VERSION,
    Evaluation,
    ForecastConfig,
    Prediction,
    TrainingPoint,
    direction,
)
from chengchuan.domain.models import SHANGHAI

WARNINGS = (
    "研究模型尚未完成独立封存测试与概率校准，方向不是收益承诺。",
    "目标为原始收盘价变化，尚未系统核对除权除息／股本变化；仅作工程诊断。",
    "当前历史为事后取得的数据重建，不能声称完整复现当时版本。",
    "历史命中率不等于未来准确率，也不等于机构身份识别或可交易收益。",
)


class ForecastJournal(Protocol):
    def predictions(self, manifest, snapshot, rows): ...
    def evaluations(self, experiment_id, rows): ...


@dataclass(frozen=True)
class ForecastRun:
    experiment_id: str
    manifest: dict
    features: object
    predictions: tuple[Prediction, ...]
    evaluations: tuple[Evaluation, ...]
    summaries: tuple[dict, ...]
    notes: tuple[str, ...] = WARNINGS
    executed_at: str = field(default_factory=lambda: datetime.now(timezone.utc).isoformat())


def digest(value):
    return hashlib.sha256(
        json.dumps(value, default=str, sort_keys=True, allow_nan=False).encode()
    ).hexdigest()


def completed_session(day, now, observed_at=None, quote_at=None):
    if now.utcoffset() is None:
        raise ValueError("预测截止时间须包含时区")
    local = now.astimezone(SHANGHAI)
    if day < local.date():
        return True
    if day > local.date() or local.time() < time(15, 15):
        return False
    # A nominal 15:00 candle timestamp alone cannot prove the session is complete.
    if observed_at is None or quote_at is None:
        return False
    if observed_at.utcoffset() is None or quote_at.utcoffset() is None:
        raise ValueError("来源观察时间须包含时区")
    observed, quote = observed_at.astimezone(SHANGHAI), quote_at.astimezone(SHANGHAI)
    return (
        observed_at <= now
        and quote_at <= now
        and observed.date() == day
        and observed.time() >= time(15, 15)
        and quote.date() == day
        and quote.time() >= time(15)
    )


class ForecastService:
    def __init__(self, extractor, model, journal=None):
        self.extractor, self.model, self.journal = extractor, model, journal
        self.calendar = extractor.calendar

    def _research_input(self, snapshot):
        """Long chart history may exceed the calendar checked by the existing research model."""
        first = self.calendar.first_supported
        bars = tuple(b for b in snapshot.series.bars if bar_date(b) >= first)
        if not bars:
            raise ValueError("行情不在已核对的预测交易日历范围内")
        if len(bars) == len(snapshot.series.bars):
            return snapshot
        return replace(
            snapshot,
            series=replace(snapshot.series, bars=bars),
            flows=tuple(
                f for f in snapshot.flows if f.timestamp.astimezone(SHANGHAI).date() >= first
            ),
        )

    def latest_date(self, snapshot, now, observed_at=None, quote_at=None):
        for b in reversed(snapshot.series.bars):
            day = bar_date(b)
            if completed_session(day, now, observed_at, quote_at):
                return day
        raise ValueError("没有已完成的日线")

    def _features(self, snapshot, until):
        if len(snapshot.series.bars) > 2000:
            raise ValueError("当前研究任务最多2000条日线，请缩小范围或使用分批研究数据")
        dates = [bar_date(b) for b in snapshot.series.bars]
        if len(dates) != len(set(dates)):
            raise ValueError("日线含重复交易日，不能重复计算样本")
        return {
            day: self.extractor.extract(snapshot, day)
            for day in self.calendar.sessions(min(dates), until)
        }

    def _label(self, bars, origin, horizon, config):
        try:
            target = self.calendar.shift(origin, horizon)
        except CalendarUnavailable as exc:
            return None, None, str(exc)
        a, b = bars.get(origin), bars.get(target)
        if not a or not b or a.volume_shares <= 0 or b.volume_shares <= 0:
            return target, None, "基准或目标日缺失／无有效成交，不顺延目标日期"
        if any(origin <= date.fromisoformat(d) <= target for d in config.excluded_dates):
            return target, None, "目标跨度包含明确排除的公司行为／异常日期"
        return target, (b.close / a.close - 1) * 100, ""

    def _predict(self, snapshot, features, day, horizon, group, config):
        feature = features[day]
        prefix = snapshot
        if bar_date(prefix.series.bars[-1]) > day or any(d > day for d in features):
            raise ValueError("预测输入包含D之后的信息，已拒绝")
        history_bars = {bar_date(b): b for b in prefix.series.bars}
        try:
            target = self.calendar.shift(day, horizon)
            status, reason = "research_only", "未经独立验证及校准"
        except CalendarUnavailable as exc:
            target, status, reason = None, "calendar_unavailable", str(exc)
        record = dict(
            record_id=digest(
                [
                    snapshot.series.symbol,
                    day,
                    horizon,
                    group,
                    feature.revision,
                    feature.version,
                    asdict(config),
                    self.model.version,
                ]
            ),
            symbol=snapshot.series.symbol,
            as_of=day.isoformat(),
            target=target.isoformat() if target else None,
            horizon=horizon,
            group=group,
            status=status,
            reason=reason,
            feature_revision=feature.revision,
        )
        if not feature.eligible or day.isoformat() in config.excluded_dates:
            return Prediction(
                **{
                    **record,
                    "status": "insufficient_input",
                    "reason": "；".join(feature.reasons) or "基准日属于明确排除的日期",
                }
            )
        if target is None:
            return Prediction(**record)
        training = []
        for earlier, f in features.items():
            if earlier >= day or not f.eligible:
                continue
            label_day, ret, _ = self._label(history_bars, earlier, horizon, config)
            if label_day is not None and label_day <= day and ret is not None:
                training.append(TrainingPoint(earlier, label_day, f.revision, f.vector(group), ret))
        training = tuple(training[-config.train_window :])
        if len(training) < config.min_train:
            return Prediction(
                **{
                    **record,
                    "status": "insufficient_training",
                    "reason": f"训练样本{len(training)}，至少需要{config.min_train}",
                },
                train_count=len(training),
            )
        output = self.model.predict(feature.vector(group), training, config)
        counts = {d: sum(direction(p.return_pct) == d for p in training) for d in (-1, 0, 1)}
        majority = max((-1, 0, 1), key=lambda d: counts[d])
        bars = prefix.series.bars
        return Prediction(
            **{
                **record,
                "status": "research_only" if output.direction is not None else "abstained",
                "reason": "研究方向，非校准概率"
                if output.direction is not None
                else "相似日类别并列，不给方向",
            },
            model_id=output.model_id,
            direction=output.direction,
            raw_probabilities=output.raw_probabilities,
            neighbor_counts=output.neighbor_counts,
            neighbor_mean_return=output.neighbor_mean_return,
            train_count=output.train_count,
            training_label_through=max(p.target for p in training).isoformat(),
            majority_direction=majority,
            momentum_direction=direction(bars[-1].close / bars[-2].close - 1),
            neighbor_dates=output.neighbor_dates,
        )

    def _manifest(self, snapshot, mode, config, **kwargs):
        return dict(
            schema_version=1,
            mode=mode,
            symbol=snapshot.series.symbol,
            name=snapshot.series.name,
            snapshot_revision=snapshot.revision,
            target_version=TARGET_VERSION,
            model_version=self.model.version,
            feature_version=FEATURE_VERSION,
            stage_version=getattr(self.extractor.stages, "version", "none"),
            calendar_version=self.calendar.version,
            config=asdict(config),
            historical_grade="reconstructed",
            public_probability_status="not_enabled",
            **kwargs,
        )

    def predict(self, snapshot, as_of=None, config=None, now=None, observed_at=None, quote_at=None):
        now, config = now or datetime.now(SHANGHAI), config or ForecastConfig()
        day = as_of or self.latest_date(snapshot, now, observed_at, quote_at)
        if not completed_session(day, now, observed_at, quote_at):
            raise ValueError("该日线尚未完成／来源完成状态未确认，请选择已完成历史交易日")
        research = self._research_input(snapshot)
        features = self._features(research, day)
        if day not in features:
            raise ValueError("指定基准日不在日线数据中")
        manifest = self._manifest(
            snapshot,
            "predict",
            config,
            as_of=day.isoformat(),
            source_observed_at=observed_at.isoformat() if observed_at else None,
            source_quote_at=quote_at.isoformat() if quote_at else None,
            completion_policy="historical-date-or-1515-observed-and-1500-quote-v1",
        )
        experiment_id = digest(manifest)
        prefix = history_at(research, day)
        rows = tuple(self._predict(prefix, features, day, h, "C", config) for h in (1, 2))
        if self.journal:
            self.journal.predictions(manifest, snapshot, rows)
        return ForecastRun(experiment_id, manifest, features[day], rows, (), ())

    def backtest(self, snapshot, before=None, config=None, start=None, groups=GROUPS, now=None):
        now, config = now or datetime.now(SHANGHAI), config or ForecastConfig()
        before = before or now.astimezone(SHANGHAI).date()
        if before > now.astimezone(SHANGHAI).date():
            raise ValueError("严格上界不能晚于今天，避免将未确认的当前日线作为真实标签")
        if not groups or any(g not in GROUPS for g in groups):
            raise ValueError("研究组须为A、B或C")
        days = [bar_date(b) for b in snapshot.series.bars if bar_date(b) < before]
        if not days:
            raise ValueError("评估上界之前没有数据")
        research = self._research_input(snapshot)
        features = self._features(research, max(days))
        days = sorted(features)
        manifest = self._manifest(
            snapshot,
            "walk_forward",
            config,
            before=before.isoformat(),
            start=start.isoformat() if start else None,
            groups=list(groups),
        )
        experiment_id = digest(manifest)
        predictions = []
        for day in days:
            if start and day < start:
                continue
            prefix = history_at(research, day)
            available = {d: f for d, f in features.items() if d <= day}
            for h in (1, 2):
                for group in groups:
                    predictions.append(self._predict(prefix, available, day, h, group, config))
        predictions = tuple(predictions)
        # Prediction records are committed before future outcome evaluation.
        if self.journal:
            self.journal.predictions(manifest, snapshot, predictions)
        evaluations = []
        evaluation_bars = {bar_date(b): b for b in snapshot.series.bars}
        for row in predictions:
            target = date.fromisoformat(row.target) if row.target else None
            if target is None or target >= before:
                evaluations.append(
                    Evaluation(
                        row.record_id,
                        None,
                        None,
                        None,
                        "目标日期未严格早于评估上界／日历不可用",
                        "outside_boundary",
                    )
                )
                continue
            _, ret, reason = self._label(
                evaluation_bars, date.fromisoformat(row.as_of), row.horizon, config
            )
            actual = direction(ret) if ret is not None else None
            evaluations.append(
                Evaluation(
                    row.record_id,
                    ret,
                    actual,
                    row.direction == actual
                    if actual is not None and row.direction is not None
                    else None,
                    reason,
                    "evaluated" if actual is not None else "unavailable",
                )
            )
        evaluations = tuple(evaluations)
        if self.journal:
            self.journal.evaluations(experiment_id, evaluations)
        return ForecastRun(
            experiment_id,
            manifest,
            None,
            predictions,
            evaluations,
            tuple(summarize(predictions, evaluations)),
        )
