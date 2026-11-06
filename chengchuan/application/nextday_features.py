"""Point-in-time CPU factors; ownership and investor psychology remain proxies.

Returns and distances are fractions, not percentage points.  The volume profile
allocates each daily bar's volume uniformly across its observed price range and
decays older observations by a fixed 20-session half-life.  It is a reproducible
traded-volume proxy, not an estimate of actual outstanding ownership.
"""

import hashlib
import json
from collections import defaultdict
from datetime import datetime, time, timedelta
from math import exp, isfinite, log, sqrt
from statistics import mean, pstdev

from chengchuan.application.forecast_features import bar_date
from chengchuan.domain.calendar import CalendarUnavailable, TradingCalendar
from chengchuan.domain.models import SHANGHAI, MarketBar, SeriesKind
from chengchuan.domain.nextday import FEATURE_VERSION, MODES, NextDayFeatures

_PROFILE_BINS = 40
_MINUTE_GRID = frozenset(
    [(9, m) for m in range(31, 60)]
    + [(10, m) for m in range(60)]
    + [(11, m) for m in range(30)]
    + [(13, m) for m in range(1, 60)]
    + [(14, m) for m in range(35)]
)
_PROXY_NOTE = (
    "筹码项为60日成交量价格分布代理（40格、20交易日半衰期），不是实际持仓、"
    "真实获利盘或主力账户；心理项仅描述价量行为。"
)


def _bar_payload(bar):
    return (
        bar.timestamp.isoformat(),
        bar.open,
        bar.high,
        bar.low,
        bar.close,
        bar.volume_shares,
        bar.amount_yuan,
    )


def _digest(payload):
    return hashlib.sha256(
        json.dumps(payload, ensure_ascii=False, sort_keys=True, allow_nan=False).encode()
    ).hexdigest()


def _ema(values, period):
    output = [values[0]]
    alpha = 2 / (period + 1)
    for value in values[1:]:
        output.append(output[-1] + alpha * (value - output[-1]))
    return output


def _correlation(a, b):
    ma, mb = mean(a), mean(b)
    va = sum((x - ma) ** 2 for x in a)
    vb = sum((x - mb) ** 2 for x in b)
    cov = sum((x - ma) * (y - mb) for x, y in zip(a, b))
    return (cov / sqrt(va * vb) if va and vb else None, cov / vb if vb else None)


def _minute_groups(snapshot, day, first):
    if snapshot is None:
        return {}
    if snapshot.series.interval != "min1" or snapshot.series.kind != SeriesKind.OHLC:
        raise ValueError("14:35预测只接受真实1分钟OHLC；分时采样或较长周期不能代替")
    groups = defaultdict(list)
    seen = set()
    for bar in snapshot.series.bars:
        stamp = bar.timestamp.astimezone(SHANGHAI)
        clock = stamp.time()
        if not first <= stamp.date() <= day:
            continue
        if not (time(9, 30) <= clock <= time(11, 30) or time(13) <= clock < time(14, 35)):
            continue
        cutoff = datetime.combine(stamp.date(), time(14, 35), SHANGHAI)
        # Safe for either start-labelled or end-labelled one-minute candles.
        if stamp + timedelta(minutes=1) <= cutoff:
            if stamp.second or stamp.microsecond or stamp in seen:
                raise ValueError("1分钟线须为整分钟且无重复，不能混入逐笔或子分钟记录")
            seen.add(stamp)
            groups[stamp.date()].append(bar)
    return dict(groups)


def _complete_minutes(bars):
    clocks = {
        (stamp.hour, stamp.minute) for b in bars for stamp in (b.timestamp.astimezone(SHANGHAI),)
    }
    return _MINUTE_GRID.issubset(clocks) and sum(b.volume_shares for b in bars) > 0


def _partial_bar(day, bars):
    amount = (
        sum(b.amount_yuan for b in bars) if all(b.amount_yuan is not None for b in bars) else None
    )
    return MarketBar(
        datetime.combine(day, time(14, 35), SHANGHAI),
        bars[-1].close,
        sum(b.volume_shares for b in bars),
        amount,
        bars[0].open,
        max(b.high for b in bars),
        min(b.low for b in bars),
    )


def _profile(bars, price):
    low, high = min(b.low for b in bars), max(b.high for b in bars)
    if high == low:
        return (
            dict(
                cost=low,
                profit=float(price >= low),
                overhang=float(price < low),
                concentration=1.0,
                width=0.0,
                poc=low,
            ),
            "全部成交区间相同，成交量价格分布代理集中于单价位。",
        )
    step = (high - low) / _PROFILE_BINS
    centers = [low + (i + 0.5) * step for i in range(_PROFILE_BINS)]
    weights = [0.0] * _PROFILE_BINS
    for age, bar in enumerate(reversed(bars)):
        weight = bar.volume_shares * exp(-log(2) * age / 20)
        if bar.high == bar.low:
            weights[min(_PROFILE_BINS - 1, int((bar.close - low) / step))] += weight
        else:
            for i in range(_PROFILE_BINS):
                overlap = max(
                    0.0, min(bar.high, low + (i + 1) * step) - max(bar.low, low + i * step)
                )
                weights[i] += weight * overlap / (bar.high - bar.low)
    total = sum(weights)
    cost = sum(p * w for p, w in zip(centers, weights)) / total

    def quantile(q):
        cumulative = 0.0
        for p, w in zip(centers, weights):
            cumulative += w
            if cumulative >= q * total:
                return p
        return centers[-1]

    return (
        dict(
            cost=cost,
            profit=sum(w for p, w in zip(centers, weights) if p <= price) / total,
            overhang=sum(w for p, w in zip(centers, weights) if price < p <= price * 1.05) / total,
            concentration=sum(w for p, w in zip(centers, weights) if abs(p / cost - 1) <= 0.02)
            / total,
            width=(quantile(0.8) - quantile(0.2)) / price,
            poc=centers[max(range(_PROFILE_BINS), key=weights.__getitem__)],
        ),
        "",
    )


class NextDayFeatureExtractor:
    def __init__(self, calendar=None):
        self.calendar = calendar or TradingCalendar()

    def extract(self, snapshot, day, mode="close", minute_snapshot=None):
        if mode not in MODES:
            raise ValueError("预测时点须为close或cut1435")
        if snapshot.series.interval != "day" or snapshot.series.kind != SeriesKind.OHLC:
            raise ValueError("下一交易日预测需要真实日K")
        if minute_snapshot and minute_snapshot.series.symbol != snapshot.series.symbol:
            raise ValueError("分钟数据与日线证券不一致")
        cutoff = datetime.combine(day, time(15, 15) if mode == "close" else time(14, 35), SHANGHAI)
        rows = {}
        for bar in snapshot.series.bars:
            d = bar_date(bar)
            if d > day or (mode == "cut1435" and d == day):
                continue
            if d in rows:
                raise ValueError("日线包含重复交易日")
            rows[d] = bar
        reasons, notes = [], [_PROXY_NOTE]
        try:
            previous_dates = self.calendar.previous(day, 60)
            if not self.calendar.is_session(day):
                reasons.append("基准日不是已核对的交易日")
        except CalendarUnavailable as exc:
            revision = _digest([FEATURE_VERSION, mode, day.isoformat(), str(exc)])
            return NextDayFeatures(day, cutoff, mode, revision, (), False, (str(exc),))
        previous = [rows.get(d) for d in previous_dates]
        if any(b is None or b.volume_shares <= 0 for b in previous):
            reasons.append("此前60个交易日存在历史不足、缺失或零成交量")
        minute_groups = {}
        current = rows.get(day) if mode == "close" else None
        if mode == "cut1435":
            minute_groups = _minute_groups(minute_snapshot, day, previous_dates[-20])
            minute_current = minute_groups.get(day, ())
            if not _complete_minutes(minute_current):
                reasons.append("14:35前真实分钟行情缺失或不完整，不能用最终日线回填")
            else:
                current = _partial_bar(day, minute_current)
            missing = [
                d for d in previous_dates[-5:] if not _complete_minutes(minute_groups.get(d, ()))
            ]
            if missing:
                reasons.append("此前5个交易日的同钟分钟行情不足，不能计算可比量比")
            notes.append(
                "14:35历史重建：仅用标时早于14:35且保守结束时刻不晚于截止点的1分钟线；"
                "缺少逐条接收时间，不宣称复现当时已收到的数据。"
            )
            notes.append("D日指标使用14:35截面；相对D日最终收盘的次日涨跌标签由评价侧事后计算。")
        if current is None:
            reasons.append("基准时点没有有效行情")
        elif current.volume_shares <= 0:
            reasons.append("基准时点没有有效成交量")
        benchmark_rows = {}
        if snapshot.benchmark:
            if snapshot.benchmark.interval != "day" or snapshot.benchmark.kind != SeriesKind.OHLC:
                notes.append("基准指数不是完整日K，市场特征留空。")
            else:
                for bar in snapshot.benchmark.bars:
                    d = bar_date(bar)
                    if previous_dates[0] <= d <= day and not (mode == "cut1435" and d == day):
                        if d in benchmark_rows:
                            raise ValueError("基准指数日线包含重复交易日")
                        benchmark_rows[d] = bar
        revision = _digest(
            [
                FEATURE_VERSION,
                snapshot.series.symbol,
                mode,
                day.isoformat(),
                [_bar_payload(b) if b else None for b in previous],
                _bar_payload(current) if current else None,
                [
                    (d.isoformat(), [_bar_payload(b) for b in bars])
                    for d, bars in sorted(minute_groups.items())
                ],
                [(d.isoformat(), _bar_payload(b)) for d, b in sorted(benchmark_rows.items())],
            ]
        )
        coverage = (
            minute_groups[day][-1].timestamp.isoformat()
            if mode == "cut1435" and minute_groups.get(day)
            else (current.timestamp.isoformat() if current else None)
        )
        if reasons:
            return NextDayFeatures(
                day,
                cutoff,
                mode,
                revision,
                (),
                False,
                tuple(reasons),
                tuple(notes),
                current.close if current else None,
                coverage_at=coverage,
            )
        return self._calculate(
            day,
            cutoff,
            mode,
            revision,
            previous,
            current,
            previous_dates,
            minute_groups,
            benchmark_rows,
            notes,
            coverage,
        )

    def _calculate(
        self,
        day,
        cutoff,
        mode,
        revision,
        previous,
        current,
        previous_dates,
        minute_groups,
        benchmark_rows,
        notes,
        coverage,
    ):
        bars = previous + [current]
        closes = [b.close for b in bars]
        last, price = previous[-1], current.close
        values, levels, evidence = [], [], []

        def add(name, value):
            values.append((name, float(value) if value is not None and isfinite(value) else None))

        for n in (1, 2, 5, 10, 20, 60):
            add(f"price_return_{n}", price / previous[-n].close - 1)
        for n in (5, 10, 20, 60):
            add(f"price_ma_distance_{n}", price / mean(closes[-n:]) - 1)
        for n in (5, 20):
            add(f"price_ma_slope_{n}", mean(closes[-n:]) / mean(closes[-n - 5 : -5]) - 1)
        changes = [b - a for a, b in zip(closes[-15:], closes[-14:])]
        gains, losses = sum(max(0, c) for c in changes), sum(max(0, -c) for c in changes)
        rsi = gains / (gains + losses) if gains + losses else None
        add("price_rsi14_simple", rsi)
        fast, slow = _ema(closes, 12), _ema(closes, 26)
        macd = [a - b for a, b in zip(fast, slow)]
        add("price_macd", macd[-1] / price)
        add("price_macd_histogram", (macd[-1] - _ema(macd, 9)[-1]) / price)
        atr = mean(
            max(b.high - b.low, abs(b.high - a.close), abs(b.low - a.close))
            for a, b in zip(bars[-15:], bars[-14:])
        )
        add("price_atr14", atr / price)
        returns = [log(b / a) for a, b in zip(closes[-21:], closes[-20:])]
        add("price_volatility20", pstdev(returns))
        std, average = pstdev(closes[-20:]), mean(closes[-20:])
        add("price_bollinger_z", (price - average) / std if std else None)
        add("price_bollinger_width", 4 * std / average)
        span, ret = current.high - current.low, price / last.close - 1
        location = (price - current.low) / span if span else None
        upper = (current.high - max(current.open, price)) / span if span else None
        lower = (min(current.open, price) - current.low) / span if span else None
        for name, value in (
            ("range", span / price),
            ("body", price / current.open - 1),
            ("gap", current.open / last.close - 1),
            ("close_location", location),
            ("upper_wick", upper),
            ("lower_wick", lower),
        ):
            add("price_" + name, value)
        volume_previous = [b.volume_shares for b in previous]
        if mode == "cut1435":
            volume_previous = [
                sum(b.volume_shares for b in minute_groups.get(d, ()))
                if _complete_minutes(minute_groups.get(d, ()))
                else None
                for d in previous_dates
            ]
        rv = {}
        for n in (1, 2, 5, 20):
            sample = volume_previous[-n:]
            ratio = (
                current.volume_shares / mean(sample)
                if all(v is not None and v > 0 for v in sample)
                else None
            )
            rv[n] = log(ratio) if ratio else None
            add(f"volume_rv{n}", rv[n])
        valid20 = all(v is not None and v > 0 for v in volume_previous[-20:])
        add(
            "volume_recent_trend",
            log(mean(volume_previous[-5:]) / mean(volume_previous[-20:])) if valid20 else None,
        )
        add(
            "volume_signed_imbalance20",
            sum(
                (1 if b.close > a.close else -1 if b.close < a.close else 0) * b.volume_shares
                for a, b in zip(previous[-21:], previous[-20:])
            )
            / sum(b.volume_shares for b in previous[-20:]),
        )
        add("volume_return_interaction", ret * rv[5] if rv[5] is not None else None)
        add(
            "volume_location_interaction",
            (location - 0.5) * rv[5] if location is not None and rv[5] is not None else None,
        )
        support, resistance = {}, {}
        for n in (20, 60):
            support[n], resistance[n] = (
                min(b.low for b in previous[-n:]),
                max(b.high for b in previous[-n:]),
            )
            levels.extend(
                ((f"此前{n}日区间低点", support[n]), (f"此前{n}日区间高点", resistance[n]))
            )
            add(f"structure_support_distance_{n}", (price - support[n]) / atr if atr else None)
            add(
                f"structure_resistance_distance_{n}", (resistance[n] - price) / atr if atr else None
            )
            add(
                f"price_position_{n}",
                (price - support[n]) / (resistance[n] - support[n])
                if resistance[n] > support[n]
                else None,
            )
        tolerance = max(price * 0.003, atr * 0.2)
        add(
            "structure_support_touches",
            sum(abs(b.low - support[20]) <= tolerance for b in previous[-20:]) / 20,
        )
        add(
            "structure_resistance_touches",
            sum(abs(b.high - resistance[20]) <= tolerance for b in previous[-20:]) / 20,
        )
        add("structure_breakout", (price - resistance[20]) / atr if atr else None)
        add(
            "structure_failed_breakout",
            (current.high - resistance[20]) / atr
            if current.high > resistance[20] and price < resistance[20] and atr
            else 0.0,
        )
        # A partial-day profile must not combine current partial volume with full-day weights.
        profile_bars = previous[-60:] if mode == "cut1435" else bars[-60:]
        profile, note = _profile(profile_bars, price)
        if note:
            notes.append(note)
        for name, value in (
            ("cost_distance", price / profile["cost"] - 1),
            ("profit_volume_share", profile["profit"]),
            ("near_overhang_share", profile["overhang"]),
            ("concentration", profile["concentration"]),
            ("width", profile["width"]),
            ("poc_distance", price / profile["poc"] - 1),
        ):
            add("chip_" + name, value)
        levels.extend(
            (("成交量价格代理均值", profile["cost"]), ("成交量价格代理峰值", profile["poc"]))
        )
        directions = [1 if b > a else -1 if b < a else 0 for a, b in zip(closes, closes[1:])]
        streak = 0
        for direction in reversed(directions):
            if direction != directions[-1] or direction == 0:
                break
            streak += direction
        add("behavior_streak", streak / 10)
        add("behavior_up_fraction10", sum(d > 0 for d in directions[-10:]) / 10)
        add("behavior_rejection", lower * max(0, -ret) if lower is not None else None)
        add(
            "behavior_exhaustion",
            upper * max(0, ret) * max(0, rv[5])
            if upper is not None and rv[5] is not None
            else None,
        )
        add("behavior_drawdown60", price / resistance[60] - 1)
        add("behavior_rebound60", price / support[60] - 1)
        market_days = previous_dates[-21:] if mode == "cut1435" else previous_dates[-20:] + (day,)
        aligned = [benchmark_rows.get(d) for d in market_days]
        market_ok = all(b is not None and b.volume_shares > 0 for b in aligned)
        if market_ok:
            market_closes = [b.close for b in aligned]
            mr1, mr5 = (
                market_closes[-1] / market_closes[-2] - 1,
                market_closes[-1] / market_closes[-6] - 1,
            )
            stock_closes = closes[-22:-1] if mode == "cut1435" else closes[-21:]
            bm_returns = [log(b / a) for a, b in zip(market_closes, market_closes[1:])]
            stock_returns = [log(b / a) for a, b in zip(stock_closes, stock_closes[1:])]
            corr, beta = _correlation(stock_returns, bm_returns)
            relative = stock_closes[-1] / stock_closes[-6] - 1 - mr5
        else:
            mr1 = mr5 = corr = beta = relative = None
            notes.append("缺少按交易日期对齐的21日基准指数，市场因子留空；不回填当前指数资料。")
        for name, value in (
            ("return1", mr1),
            ("return5", mr5),
            ("relative5", relative),
            ("correlation20", corr),
            ("beta20", beta),
        ):
            add("market_" + name, value)
        intraday = minute_groups.get(day, ()) if mode == "cut1435" else ()
        if intraday:
            latest = intraday[-30:]
            earlier = intraday[-60:-30]
            recent_ratio = (
                sum(b.volume_shares for b in latest) / sum(b.volume_shares for b in earlier)
                if sum(b.volume_shares for b in earlier)
                else None
            )
            intraday_return = latest[-1].close / latest[0].open - 1
            proxy_vwap = (
                sum((b.high + b.low + b.close) / 3 * b.volume_shares for b in intraday)
                / current.volume_shares
            )
            intraday_distance = price / proxy_vwap - 1
        else:
            recent_ratio = intraday_return = intraday_distance = None
        add("intraday_recent_volume_ratio", recent_ratio)
        add("intraday_last30_return", intraday_return)
        add("intraday_vwap_proxy_distance", intraday_distance)
        clock_name = "14:35同钟" if mode == "cut1435" else "全天"
        evidence.extend(
            (
                f"{clock_name}成交量较此前5日均量为{exp(rv[5]):.2f}倍；价格较前收{ret:+.2%}。",
                f"此前20日区间为{support[20]:.3f}至{resistance[20]:.3f}，仅作为支撑压力候选位置。",
                f"成交量价格分布代理均值{profile['cost']:.3f}，低于当前价的历史成交权重{profile['profit']:.1%}。",
            )
        )
        if mode == "cut1435":
            notes.append("市场因子及筹码代理只使用D之前完整日线；20日同钟量比缺历史时留空。")
        notes.append("RSI为14期简单涨跌均值；MACD以61根窗口首价初始化；未复权会受公司行为影响。")
        return NextDayFeatures(
            day,
            cutoff,
            mode,
            revision,
            tuple(values),
            True,
            (),
            tuple(notes),
            price,
            tuple(levels),
            tuple(evidence),
            coverage,
        )
