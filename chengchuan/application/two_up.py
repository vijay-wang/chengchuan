"""Exactly-two-up event research, with causal comparable prices and dated labels."""

from dataclasses import asdict, replace
from datetime import date, datetime
from math import isfinite

from chengchuan.application.forecast import completed_session, digest
from chengchuan.application.forecast_features import bar_date
from chengchuan.application.nextday import cutoff
from chengchuan.application.nextday_features import NextDayFeatureExtractor
from chengchuan.application.two_up_learning import VERSION, fit_signal
from chengchuan.application.two_up_metrics import summarize_two_up
from chengchuan.domain.calendar import CalendarUnavailable, TradingCalendar
from chengchuan.domain.forecast import direction
from chengchuan.domain.market import is_index
from chengchuan.domain.models import SHANGHAI, SeriesKind
from chengchuan.domain.two_up import (
    EVENT_VERSION,
    TARGET_VERSION,
    EventState,
    TwoUpConfig,
    TwoUpEvaluation,
    TwoUpPrediction,
    TwoUpRun,
    TwoUpSample,
)

NOTES = (
    "条件：前一天收跌或收平，随后恰好两天收涨；仅第二天盘后预测第三个交易日。",
    "每日涨跌使用来源文档定义的可比昨收价；第三日可比昨收仅进入事后标签。",
    "80%是验收目标，验证集选择不等于独立测试或未来达到80%；拒判仍计入事件覆盖分母。",
    "CPU候选与阈值只用更早训练／验证数据选择；原始评分不是校准概率。",
    "历史数据为事后重建；筹码及心理仍是量价代理，现金分红不随意调整原成交量。",
)


def closed_at_source(history, day, bar):
    # Explicit undated research archives are historical reconstructions. The live
    # cache adapter always supplies observed_at; its intraday rows must not mature
    # just because the wall clock has passed the nominal candle closing time.
    observed = history.observed_at
    return observed is None or completed_session(day, observed, observed, bar.timestamp)


def event_state(history, day, config=None, calendar=None):
    calendar, config = calendar or TradingCalendar(), config or TwoUpConfig()
    try:
        dates = calendar.previous(day, 3) + (day,)
        if not calendar.is_session(day):
            return EventState(day, "event_unknown", "不是已核对交易日")
    except CalendarUnavailable as exc:
        return EventState(day, "event_unknown", str(exc))
    bars = {bar_date(b): b for b in history.snapshot.series.bars if bar_date(b) <= day}
    previous = dict(history.previous_closes)
    if any(d not in bars or bars[d].volume_shares <= 0 for d in dates):
        return EventState(
            day, "event_unknown", "连续四个交易日行情缺失或无成交，不能跨缺口识别连涨"
        )
    if not closed_at_source(history, day, bars[day]):
        return EventState(
            day, "event_unknown", "来源缓存尚未确认指定日收盘，不能用盘中日线确认条件"
        )
    if any(d not in previous or not isfinite(previous[d]) or previous[d] <= 0 for d in dates[1:]):
        return EventState(day, "event_unknown", "缺少有效可比昨收价，不能用原始价格断点猜测连涨")
    returns = tuple((bars[d].close / previous[d] - 1) * 100 for d in dates[1:])
    if not (returns[0] <= 0 and returns[1] > 0 and returns[2] > 0):
        return EventState(day, "not_event", "不满足前日不涨、随后恰好两天收涨", returns)
    if any(d.isoformat() in config.excluded_dates for d in dates):
        return EventState(day, "excluded_event", "条件已成立，但条件跨度包含用户排除日期", returns)
    return EventState(day, "confirmed", "恰好连涨两天已在收盘后确认", returns)


def causal_prices(history, until):
    """Forward chain only: a future adjustment never rescales an earlier feature."""
    source = history.snapshot
    if (
        source.series.interval != "day"
        or source.series.kind != SeriesKind.OHLC
        or is_index(source.series.symbol)
    ):
        raise ValueError("连涨两天预测仅接受个股真实日K")
    previous = dict(history.previous_closes)
    rows = []
    last = None
    for b in source.series.bars:
        d = bar_date(b)
        if d > until:
            break
        comparable = previous.get(d)
        if comparable is None or not isfinite(comparable) or comparable <= 0:
            last = None
            continue
        price = b.close if last is None else last * b.close / comparable
        factor = price / b.close
        opening = b.open * factor
        rows.append(
            replace(
                b,
                close=price,
                open=opening,
                high=max(b.high * factor, price, opening),
                low=min(b.low * factor, price, opening),
            )
        )
        last = price
    if not rows:
        return None
    series = replace(source.series, bars=tuple(rows))
    benchmark = source.benchmark
    if benchmark:
        kept = tuple(b for b in benchmark.bars if bar_date(b) <= until)
        benchmark = replace(benchmark, bars=kept) if kept else None
    return replace(
        source,
        revision=digest([asdict(series), asdict(benchmark) if benchmark else None]),
        series=series,
        benchmark=benchmark,
        flows=(),
        source="按可比昨收向前链接的因果价格历史",
    )


def prepare_history(history, until, config=None):
    config = config or TwoUpConfig()
    calendar = TradingCalendar()
    extractor = NextDayFeatureExtractor(calendar)
    snapshot = history.snapshot
    dates = [
        bar_date(b)
        for b in snapshot.series.bars
        if calendar.first_supported <= bar_date(b) <= until
    ]
    if len(dates) != len(set(dates)):
        raise ValueError("日线包含重复交易日")
    if not dates:
        return {}, {}, ()
    if len(dates) > 1000:
        raise ValueError("单次事件研究最多1000个已核对交易日")
    adjusted = causal_prices(history, until)
    states, features, samples = {}, {}, []
    raw = {bar_date(b): b for b in snapshot.series.bars if bar_date(b) <= until}
    previous = dict(history.previous_closes)
    for day in calendar.sessions(min(dates), until):
        state = event_state(history, day, config, calendar)
        states[day] = state
        if state.status != "confirmed" or adjusted is None:
            continue
        f = extractor.extract(adjusted, day)
        # Add event-specific acceleration and the non-up preceding session, all known at D close.
        values = (
            f.values
            + tuple(
                zip(
                    (
                        "event_prior_return",
                        "event_first_return",
                        "event_second_return",
                        "event_acceleration",
                    ),
                    (*state.returns, state.returns[2] - state.returns[1]),
                )
            )
            if f.eligible
            else f.values
        )
        f = replace(
            f,
            values=values,
            revision=digest([EVENT_VERSION, f.revision, state.returns]),
            version=f.version + "/" + EVENT_VERSION,
        )
        features[day] = f
        if not f.eligible:
            continue
        try:
            target = calendar.shift(day, 1)
        except CalendarUnavailable:
            continue
        b = raw.get(target)
        base = previous.get(target)
        if b is None or b.volume_shares <= 0 or base is None or not isfinite(base) or base <= 0:
            continue
        if not closed_at_source(history, target, b):
            continue
        if any(day <= date.fromisoformat(d) <= target for d in config.excluded_dates):
            continue
        samples.append(
            TwoUpSample(
                snapshot.series.symbol,
                day,
                target,
                cutoff(target, "close"),
                f.revision,
                f.values,
                (b.close / base - 1) * 100,
            )
        )
    return states, features, tuple(samples)


def split_mature(samples, day, config):
    available = tuple(
        sorted(
            (p for p in samples if p.origin < day and p.available_at <= cutoff(day, "close")),
            key=lambda p: (p.origin, p.symbol),
        )
    )
    origins = sorted({p.origin for p in available})
    if len(origins) < 2:
        return (), ()
    split = origins[min(len(origins) - 1, int(len(origins) * 0.7))]
    training = tuple(p for p in available if p.target < split)
    validation = tuple(p for p in available if p.origin >= split)
    return training, validation


class TwoUpService:
    two_up = True
    next_day = True

    def __init__(self, journal=None, input_loader=None, pool_loader=None):
        self.journal = journal
        self.input_loader = input_loader
        self.pool_loader = pool_loader
        self.calendar = TradingCalendar()

    def latest_date(self, snapshot, now, observed_at=None, quote_at=None):
        for b in reversed(snapshot.series.bars):
            if completed_session(bar_date(b), now, observed_at, quote_at):
                return bar_date(b)
        raise ValueError("没有确认完成的第二天日线")

    def _inputs(self, snapshot, until, config):
        if self.input_loader is None:
            raise ValueError("需要由启动入口提供可比昨收数据适配器")
        current = self.input_loader(snapshot)
        pool = self.pool_loader() if self.pool_loader else ()
        histories = {h.snapshot.series.symbol: h for h in pool}
        histories[snapshot.series.symbol] = current
        if len(histories) > 40:
            raise ValueError("单次CPU事件研究最多40只背景股票")
        prepared = {symbol: prepare_history(h, until, config) for symbol, h in histories.items()}
        samples = tuple(p for _, _, rows in prepared.values() for p in rows)
        event_days = tuple(
            (symbol, day)
            for symbol, (states, _, _) in prepared.items()
            for day, state in states.items()
            if state.status not in ("event_unknown", "not_event")
        )
        return current, histories, prepared[snapshot.series.symbol], samples, event_days

    def _predict(self, symbol, day, state, feature, samples, config, event_days=()):
        try:
            target = self.calendar.shift(day, 1).isoformat()
        except CalendarUnavailable:
            target = None
        revision = feature.revision if feature else digest([EVENT_VERSION, state])
        identity = [EVENT_VERSION, TARGET_VERSION, symbol, day, revision, asdict(config), VERSION]
        record = dict(
            record_id=digest(identity),
            symbol=symbol,
            as_of=str(day),
            target=target,
            status=state.status,
            reason=state.reason,
            feature_revision=revision,
            event_returns=state.returns,
        )
        if state.status != "confirmed":
            return TwoUpPrediction(**record)
        if not target or feature is None or not feature.eligible:
            return TwoUpPrediction(
                **{
                    **record,
                    "status": "insufficient_input",
                    "reason": "；".join(feature.reasons) if feature else "有效历史或交易日历不足",
                }
            )
        training, validation = split_mature(samples, day, config)
        record["record_id"] = digest(
            [*identity, [asdict(p) for p in training], [asdict(p) for p in validation]]
        )
        counts = dict(train_count=len(training), validation_count=len(validation))
        if len(training) < config.min_train or len(validation) < config.min_validation:
            return TwoUpPrediction(
                **{
                    **record,
                    "status": "insufficient_training",
                    "reason": f"成熟事件训练／验证样本{len(training)}/{len(validation)}，要求{config.min_train}/{config.min_validation}",
                },
                **counts,
            )
        first_validation = min(p.origin for p in validation)
        validation_events = sum(first_validation <= d < day for _, d in event_days)
        fitted = fit_signal(
            training, validation, config, validation_events=validation_events or len(validation)
        )
        candidate, score = fitted.predict(feature.values)
        called = fitted.threshold is not None and score >= fitted.threshold
        status = (
            "research_signal"
            if called
            else "no_80_evidence"
            if fitted.threshold is None
            else "abstained"
        )
        reason = (
            "通过历史验证门槛的研究信号，尚非未来80%保证"
            if called
            else "历史验证未找到满足80%及样本／覆盖要求的方案"
            if fitted.threshold is None
            else "本次信号低于预先选择的阈值，不预测"
        )
        return TwoUpPrediction(
            **{
                **record,
                "record_id": digest([*identity, fitted.model_id]),
                "status": status,
                "reason": reason,
            },
            **counts,
            direction=candidate if called else None,
            candidate_direction=candidate,
            score=score,
            threshold=fitted.threshold,
            model_id=fitted.model_id,
            model_name=fitted.model_name,
            training_label_through=max(p.available_at for p in training + validation).isoformat(),
            validation_summary=fitted.validation_summary,
        )

    def _save(self, histories, rows, manifest):
        if not self.journal:
            return
        # Actual comparable closes are part of the manifest; raw snapshots are immutable source evidence.
        for h in histories.values():
            self.journal.predictions(manifest, h.snapshot, ())
        first = next(iter(histories.values())).snapshot
        self.journal.predictions(manifest, first, rows)

    def _manifest(self, histories, config, task, **extra):
        return dict(
            schema_version=3,
            task=task,
            event_version=EVENT_VERSION,
            target_version=TARGET_VERSION,
            model_version=VERSION,
            compute="CPU",
            config=asdict(config),
            historical_grade="reconstructed",
            inputs={
                s: dict(
                    revision=h.snapshot.revision,
                    basis=h.basis,
                    observed_at=h.observed_at.isoformat() if h.observed_at else None,
                    previous_closes=[(str(d), v) for d, v in h.previous_closes],
                )
                for s, h in histories.items()
            },
            **extra,
        )

    def predict(self, snapshot, as_of=None, config=None, now=None, observed_at=None, quote_at=None):
        config = config or TwoUpConfig()
        now = now or datetime.now(SHANGHAI)
        day = as_of or self.latest_date(snapshot, now, observed_at, quote_at)
        if not completed_session(day, now, observed_at, quote_at):
            raise ValueError("恰好两连涨必须第二天收盘确认；不提供14:35替代预测")
        current, histories, (states, features, _), samples, event_days = self._inputs(
            snapshot, day, config
        )
        state = states.get(
            day, EventState(day, "event_unknown", "指定日期不是有数据的已核对交易日")
        )
        feature = features.get(day)
        row = self._predict(
            snapshot.series.symbol, day, state, feature, samples, config, event_days
        )
        manifest = self._manifest(
            histories,
            config,
            "predict",
            symbol=snapshot.series.symbol,
            name=snapshot.series.name,
            as_of=str(day),
        )
        self._save(histories, (row,), manifest)
        if feature and feature.reference_price:
            raw = next((b.close for b in snapshot.series.bars if bar_date(b) == day), None)
            if raw:
                scale = raw / feature.reference_price
                feature = replace(
                    feature,
                    reference_price=raw,
                    levels=tuple((k, v * scale) for k, v in feature.levels),
                )
        return TwoUpRun(digest(manifest), manifest, feature, (row,), (), (), NOTES, now.isoformat())

    def backtest(self, snapshot, before=None, config=None, start=None, now=None):
        config = config or TwoUpConfig()
        now = now or datetime.now(SHANGHAI)
        if now.utcoffset() is None:
            raise ValueError("截止时间需要时区")
        before = before or now.astimezone(SHANGHAI).date()
        if before > now.astimezone(SHANGHAI).date() or start and start >= before:
            raise ValueError("回测日期范围无效，目标须严格早于评估上界")
        dates = [bar_date(b) for b in snapshot.series.bars if bar_date(b) < before]
        if not dates:
            raise ValueError("上界之前没有数据")
        current, histories, (states, features, _), samples, event_days = self._inputs(
            snapshot, max(dates), config
        )
        rows = tuple(
            self._predict(
                snapshot.series.symbol, d, s, features.get(d), samples, config, event_days
            )
            for d, s in states.items()
            if start is None or d >= start
        )
        manifest = self._manifest(
            histories,
            config,
            "walk_forward",
            symbol=snapshot.series.symbol,
            name=snapshot.series.name,
            before=str(before),
            start=str(start) if start else None,
        )
        self._save(histories, rows, manifest)
        evaluations = []
        raw = {bar_date(b): b for b in current.snapshot.series.bars}
        previous = dict(current.previous_closes)
        for row in rows:
            if row.target is None or date.fromisoformat(row.target) >= before:
                evaluation = TwoUpEvaluation(
                    row.record_id, status="outside_boundary", reason="目标未严格早于上界"
                )
            elif row.status in ("not_event", "event_unknown"):
                evaluation = TwoUpEvaluation(
                    row.record_id, status="not_applicable", reason=row.reason
                )
            else:
                target = date.fromisoformat(row.target)
                b = raw.get(target)
                base = previous.get(target)
                excluded = any(
                    date.fromisoformat(row.as_of) <= date.fromisoformat(d) <= target
                    for d in config.excluded_dates
                )
                if (
                    not b
                    or b.volume_shares <= 0
                    or not closed_at_source(current, target, b)
                    or base is None
                    or not isfinite(base)
                    or base <= 0
                    or excluded
                ):
                    evaluation = TwoUpEvaluation(
                        row.record_id,
                        status="unavailable",
                        reason="目标缺失、无有效成交或明确排除，不顺延",
                    )
                else:
                    ret = (b.close / base - 1) * 100
                    actual = direction(ret)
                    evaluation = TwoUpEvaluation(
                        row.record_id,
                        ret,
                        actual,
                        row.direction == actual if row.direction is not None else None,
                    )
            evaluations.append(evaluation)
        evaluations = tuple(evaluations)
        if self.journal:
            self.journal.evaluations(digest(manifest), evaluations)
        summary = summarize_two_up(rows, evaluations)
        return TwoUpRun(
            digest(manifest), manifest, None, rows, evaluations, (summary,), NOTES, now.isoformat()
        )
