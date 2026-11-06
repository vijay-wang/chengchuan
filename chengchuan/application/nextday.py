"""Next-session research with cutoff isolation and mature-label walk-forward validation."""

from dataclasses import asdict, replace
from datetime import date, datetime, time

from chengchuan.application.forecast import completed_session, digest
from chengchuan.application.forecast_features import bar_date
from chengchuan.domain.calendar import CalendarUnavailable, TradingCalendar
from chengchuan.domain.forecast import direction
from chengchuan.domain.market import is_index
from chengchuan.domain.models import SHANGHAI, SeriesKind
from chengchuan.domain.nextday import (
    FEATURE_VERSION,
    GROUPS,
    MODES,
    TARGET_VERSION,
    NextDayConfig,
    NextDayEvaluation,
    NextDayPrediction,
    NextDayRun,
    NextDaySample,
)

NOTES = (
    "CPU研究模型；未完成独立封存验证，内部评分不是校准后的未来胜率。",
    "筹码为历史成交价量分布代理，行为为量价代理；不识别账户、真实持仓或投资者心理。",
    "使用事后取得的历史数据重建；缺少原始接收时间，不能证明当时已可取得。",
    "涨跌与OHLC区间相对D日最终收盘；14:35的输入与模型不读取该收盘价。",
    "原始价格诊断，尚未系统复权；请排除已知除权除息日期。支撑压力不是必然转折点。",
    "OHLC与形态是历史相似日的情景分布，不能推断盘中先高后低或精确分时路径。",
)


def cutoff(day, mode):
    if mode not in MODES:
        raise ValueError("预测时点须为close或cut1435")
    return datetime.combine(day, time(15, 15) if mode == "close" else time(14, 35), SHANGHAI)


def shape_label(origin, target):
    """Only OHLC relationships, never an invented high/low chronological path."""
    gap = (target.open / origin.close - 1) * 100
    body = (target.close / target.open - 1) * 100
    if gap > 0.3 and body < -0.3:
        return "高开收低于开盘"
    if gap < -0.3 and body > 0.3:
        return "低开收高于开盘"
    if body > 0.3:
        return "收高于开盘"
    if body < -0.3:
        return "收低于开盘"
    return "收盘接近开盘"


class NextDayService:
    next_day = True

    def __init__(
        self, extractor=None, model=None, journal=None, minute_loader=None, context_loader=None
    ):
        from chengchuan.application.nextday_features import NextDayFeatureExtractor
        from chengchuan.domain.nextday_model import CpuNextDayModel

        self.calendar = getattr(extractor, "calendar", None) or TradingCalendar()
        self.extractor = extractor or NextDayFeatureExtractor(self.calendar)
        self.model = model or CpuNextDayModel()
        self.journal = journal
        self.minute_loader = minute_loader
        self.context_loader = context_loader

    def latest_date(self, snapshot, now, observed_at=None, quote_at=None):
        for bar in reversed(snapshot.series.bars):
            day = bar_date(bar)
            if completed_session(day, now, observed_at, quote_at):
                return day
        raise ValueError("没有确认完成的日线")

    def _prepare(self, snapshot, until, mode, minute_snapshot):
        cutoff(until, mode)
        if is_index(snapshot.series.symbol):
            raise ValueError("当前次日模型仅研究个股，指数可作为市场背景")
        if snapshot.series.interval != "day" or snapshot.series.kind != SeriesKind.OHLC:
            raise ValueError("预测需要真实日K；分钟数据单独提供，不以分时采样替代")
        dates = [bar_date(b) for b in snapshot.series.bars]
        if len(set(dates)) != len(dates):
            raise ValueError("日K存在重复日期")
        bars = tuple(
            b for b in snapshot.series.bars if self.calendar.first_supported <= bar_date(b) <= until
        )
        if not bars:
            raise ValueError("指定日期之前没有已核对交易日历内的行情")
        if len(bars) > 1000:
            raise ValueError("单次研究最多1000个交易日，请分批处理")
        research = replace(snapshot, series=replace(snapshot.series, bars=bars))
        if self.context_loader:
            research = self.context_loader(research)
        research = replace(
            research,
            revision=digest(
                [
                    asdict(research.series),
                    asdict(research.benchmark) if research.benchmark else None,
                    [asdict(f) for f in research.flows],
                    research.quality_notes,
                    research.source,
                ]
            ),
        )
        days = self.calendar.sessions(bar_date(bars[0]), until)
        if mode == "cut1435" and minute_snapshot is None and self.minute_loader:
            minute_snapshot = self.minute_loader(snapshot.series.symbol, days)
        if minute_snapshot and minute_snapshot.series.symbol != snapshot.series.symbol:
            raise ValueError("分钟数据与日线证券不一致")
        features = {
            d: self.extractor.extract(research, d, mode=mode, minute_snapshot=minute_snapshot)
            for d in days
        }
        return research, features, minute_snapshot

    def _save(self, manifest, research, minutes, rows):
        if self.journal:
            # Save the actual enriched inputs, including minute OHLC, for offline reconstruction.
            if minutes:
                self.journal.predictions(manifest, minutes, ())
            self.journal.predictions(manifest, research, rows)

    def _sample(self, bars, feature, config, available_by, group):
        origin = feature.as_of
        try:
            target = self.calendar.shift(origin, 1)
        except CalendarUnavailable as exc:
            return None, str(exc)
        # D-1 -> D is NOT a mature training label at D 14:35.
        available_at = cutoff(target, "close")
        if available_at > available_by:
            return None, "目标标签尚未成熟"
        if any(origin <= date.fromisoformat(d) <= target for d in config.excluded_dates):
            return None, "标签跨度包含排除的公司行为／异常日期"
        a, b = bars.get(origin), bars.get(target)
        if a is None or b is None or a.volume_shares <= 0 or b.volume_shares <= 0:
            return None, "基准或目标日缺失／无有效成交，不顺延目标日期"
        returns = [
            (getattr(b, field) / a.close - 1) * 100 for field in ("close", "open", "high", "low")
        ]
        return NextDaySample(
            origin,
            target,
            available_at,
            feature.revision,
            feature.selected(group),
            *returns,
            shape_label(a, b),
        ), ""

    def _predict(self, research, features, day, mode, group, config):
        feature = features[day]
        logical_cutoff = cutoff(day, mode)
        # Only matured daily rows reach the label builder. The model never sees research snapshots.
        bars = {
            bar_date(b): b
            for b in research.series.bars
            if cutoff(bar_date(b), "close") <= logical_cutoff
        }
        try:
            target = self.calendar.shift(day, 1).isoformat()
        except CalendarUnavailable:
            target = None
        record = dict(
            record_id=digest(
                [
                    research.series.symbol,
                    day,
                    logical_cutoff,
                    mode,
                    group,
                    feature.revision,
                    feature.version,
                    TARGET_VERSION,
                    self.model.version,
                    asdict(config),
                ]
            ),
            symbol=research.series.symbol,
            as_of=day.isoformat(),
            cutoff_at=logical_cutoff.isoformat(),
            mode=mode,
            target=target,
            group=group,
            status="insufficient_input",
            reason="",
            feature_revision=feature.revision,
            reference_price=feature.reference_price,
        )
        if not feature.eligible or day.isoformat() in config.excluded_dates:
            return NextDayPrediction(
                **{**record, "reason": "；".join(feature.reasons) or "基准日已排除"}
            )
        if target is None:
            return NextDayPrediction(
                **{**record, "status": "calendar_unavailable", "reason": "下一交易日超出已核对日历"}
            )
        training = []
        for earlier, f in features.items():
            if earlier >= day or not f.eligible:
                continue
            sample, _ = self._sample(bars, f, config, logical_cutoff, group)
            if sample:
                training.append(sample)
        training = tuple(training[-config.train_window :])
        if len(training) < config.min_train:
            return NextDayPrediction(
                **{
                    **record,
                    "status": "insufficient_training",
                    "reason": f"{mode}已成熟训练样本{len(training)}，至少需要{config.min_train}；不使用其他时点代替",
                    "train_count": len(training),
                }
            )
        estimate = self.model.predict(feature.selected(group), training, config)
        counts = {d: sum(direction(p.close_return) == d for p in training) for d in (-1, 0, 1)}
        majority = max(counts, key=counts.get)
        previous = bars.get(self.calendar.shift(day, -1))
        momentum = (
            direction(feature.reference_price / previous.close - 1)
            if previous and feature.reference_price
            else None
        )
        return NextDayPrediction(
            **{
                **record,
                "status": "research_only" if estimate.direction is not None else "abstained",
                "reason": estimate.reason or "CPU多因子研究；未校准",
            },
            train_count=len(training),
            training_label_through=max(p.available_at for p in training).isoformat(),
            direction=estimate.direction,
            model_id=estimate.model_id,
            scores=estimate.scores,
            components=estimate.components,
            ranges=estimate.ranges,
            shapes=estimate.shapes,
            neighbors=estimate.neighbors,
            contributions=estimate.contributions,
            majority_direction=majority,
            momentum_direction=momentum,
        )

    def _manifest(self, snapshot, features, minute_snapshot, config, mode, task, **kwargs):
        return dict(
            schema_version=2,
            task=task,
            mode=mode,
            symbol=snapshot.series.symbol,
            name=snapshot.series.name,
            snapshot_revision=snapshot.revision,
            feature_version=FEATURE_VERSION,
            model_version=self.model.version,
            target_version=TARGET_VERSION,
            calendar_version=self.calendar.version,
            config=asdict(config),
            historical_grade="reconstructed_no_receipt_timestamps",
            public_probability_status="not_enabled",
            compute="CPU/stdlib",
            feature_digest=digest([(str(d), f.revision) for d, f in features.items()]),
            minute_revision=minute_snapshot.revision if minute_snapshot else None,
            **kwargs,
        )

    def predict(
        self,
        snapshot,
        as_of=None,
        config=None,
        now=None,
        observed_at=None,
        quote_at=None,
        mode="close",
        minute_snapshot=None,
    ):
        config, now = config or NextDayConfig(), now or datetime.now(SHANGHAI)
        if now.utcoffset() is None:
            raise ValueError("截止时间需要时区")
        day = as_of or (
            self.latest_date(snapshot, now, observed_at, quote_at)
            if mode == "close"
            else bar_date(snapshot.series.bars[-1])
        )
        if cutoff(day, mode) > now:
            raise ValueError("尚未到达所选预测时点")
        if mode == "close" and not completed_session(day, now, observed_at, quote_at):
            raise ValueError("当日日线完成状态未确认，需盘后来源观察时间和收盘报价")
        research, features, minutes = self._prepare(snapshot, day, mode, minute_snapshot)
        if day not in features:
            raise ValueError("基准日不是已核对交易日")
        manifest = self._manifest(
            snapshot,
            features,
            minutes,
            config,
            mode,
            "predict",
            as_of=day.isoformat(),
            source_observed_at=str(observed_at) if observed_at else None,
            source_quote_at=str(quote_at) if quote_at else None,
            research_revision=research.revision,
        )
        rows = (self._predict(research, features, day, mode, "full", config),)
        self._save(manifest, research, minutes, rows)
        return NextDayRun(digest(manifest), manifest, features[day], rows, (), (), NOTES)

    def backtest(
        self,
        snapshot,
        before=None,
        config=None,
        start=None,
        groups=GROUPS,
        now=None,
        mode="close",
        minute_snapshot=None,
    ):
        from chengchuan.application.nextday_metrics import summarize

        config, now = config or NextDayConfig(), now or datetime.now(SHANGHAI)
        if now.utcoffset() is None:
            raise ValueError("截止时间需要时区")
        before = before or now.astimezone(SHANGHAI).date()
        if before > now.astimezone(SHANGHAI).date():
            raise ValueError("回测目标上界不能晚于今天")
        if not groups or len(set(groups)) != len(groups) or any(g not in GROUPS for g in groups):
            raise ValueError("研究组须为不重复的price、volume、full")
        days = [bar_date(b) for b in snapshot.series.bars if bar_date(b) < before]
        if not days or start and start >= before:
            raise ValueError("回测日期范围没有有效数据")
        research, features, minutes = self._prepare(snapshot, max(days), mode, minute_snapshot)
        rows = tuple(
            self._predict(research, features, day, mode, group, config)
            for day in features
            if start is None or day >= start
            for group in groups
        )
        manifest = self._manifest(
            snapshot,
            features,
            minutes,
            config,
            mode,
            "walk_forward",
            before=before.isoformat(),
            start=str(start) if start else None,
            groups=list(groups),
            research_revision=research.revision,
        )
        experiment_id = digest(manifest)
        self._save(manifest, research, minutes, rows)
        bars = {bar_date(b): b for b in research.series.bars}
        evaluations = []
        for row in rows:
            if row.target is None or date.fromisoformat(row.target) >= before:
                evaluations.append(
                    NextDayEvaluation(
                        row.record_id,
                        reason="目标未严格早于评估上界／日历不可用",
                        status="outside_boundary",
                    )
                )
                continue
            sample, reason = self._sample(
                bars, features[date.fromisoformat(row.as_of)], config, now, row.group
            )
            if sample is None:
                evaluations.append(
                    NextDayEvaluation(row.record_id, reason=reason, status="unavailable")
                )
                continue
            actual = direction(sample.close_return)
            shape = max(row.shapes, key=lambda x: x[1])[0] if row.shapes else None
            evaluations.append(
                NextDayEvaluation(
                    row.record_id,
                    sample.close_return,
                    actual,
                    row.direction == actual if row.direction is not None else None,
                    sample.shape,
                    shape == sample.shape if shape else None,
                    tuple(
                        (name, getattr(sample, name + "_return"))
                        for name in ("open", "high", "low", "close")
                    ),
                )
            )
        evaluations = tuple(evaluations)
        if self.journal:
            self.journal.evaluations(experiment_id, evaluations)
        return NextDayRun(
            experiment_id,
            manifest,
            None,
            rows,
            evaluations,
            tuple(summarize(rows, evaluations)),
            NOTES,
        )
