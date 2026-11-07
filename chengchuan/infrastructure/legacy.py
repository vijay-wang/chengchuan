"""旧原型的边界适配；其数据类型不会进入新UI或应用层。"""

import hashlib
import json
from datetime import datetime
from pathlib import Path

from chengchuan.domain.market import is_index
from chengchuan.domain.models import (
    SHANGHAI,
    AnalysisReport,
    IndexRange,
    MarketBar,
    MarketSeries,
    MarketSnapshot,
    Metric,
    OrderSizeFlow,
    SeriesKind,
)
from flowradar.core import Bar, Dataset, Flow


def _stamp(value: str):
    parsed = datetime.fromisoformat(value)
    return parsed.replace(tzinfo=SHANGHAI) if parsed.tzinfo is None else parsed.astimezone(SHANGHAI)


def _series(symbol, name, interval, bars):
    # The legacy minute adapter has sampled closes, never true minute OHLC.
    kind = SeriesKind.OHLC if interval == "day" else SeriesKind.SAMPLED_CLOSE
    rows = tuple(
        MarketBar(
            timestamp=_stamp(b.timestamp),
            close=b.close,
            volume_shares=b.volume_shares,
            amount_yuan=b.amount_yuan,
            open=b.open if kind == SeriesKind.OHLC else None,
            high=b.high if kind == SeriesKind.OHLC else None,
            low=b.low if kind == SeriesKind.OHLC else None,
        )
        for b in bars
    )
    return MarketSeries(symbol, name, interval, kind, rows)


class LegacyJsonRepository:
    def load(self, path: Path) -> MarketSnapshot:
        if path.stat().st_size > 32_000_000:
            raise ValueError("基础版单个快照限制为32MB；更大数据需要分区查询适配器")
        raw = path.read_bytes()
        data = Dataset.from_dict(json.loads(raw))
        series = _series(data.symbol, data.name, data.interval, data.bars)
        benchmark = (
            _series("benchmark", "比较基准", data.interval, data.benchmark)
            if data.benchmark
            else None
        )
        flows = tuple(
            OrderSizeFlow(
                _stamp(f.timestamp),
                f.main_yuan,
                f.small_yuan,
                f.medium_yuan,
                f.large_yuan,
                f.super_yuan,
            )
            for f in data.flows
        )
        return MarketSnapshot(
            hashlib.sha256(raw).hexdigest(),
            series,
            flows,
            benchmark,
            tuple(data.quality_notes),
            "已保存行情快照",
        )


def _legacy_bars(series):
    if series is None:
        return []
    return [
        Bar(
            b.timestamp.astimezone(SHANGHAI).replace(tzinfo=None).isoformat(),
            b.open if b.open is not None else b.close,
            b.high if b.high is not None else b.close,
            b.low if b.low is not None else b.close,
            b.close,
            b.volume_shares,
            b.amount_yuan,
        )
        for b in series.bars
    ]


def legacy_dataset(snapshot):
    """Convert an explicit legacy research input; interval statistics do not use it."""
    series = snapshot.series
    flows = [
        Flow(
            f.timestamp.astimezone(SHANGHAI).replace(tzinfo=None).isoformat(),
            f.main_yuan,
            f.small_yuan,
            f.medium_yuan,
            f.large_yuan,
            f.super_yuan,
        )
        for f in snapshot.flows
    ]
    return Dataset(
        series.symbol,
        series.name,
        series.interval,
        _legacy_bars(series),
        flows,
        _legacy_bars(snapshot.benchmark),
        quality_notes=list(snapshot.quality_notes),
    )


class LegacyAnalysisEngine:
    version = "interval-statistics-v1/adapter-3"

    def analyze(self, snapshot: MarketSnapshot, selection: IndexRange) -> AnalysisReport:
        bars = snapshot.series.bars[selection.start : selection.stop]
        previous = snapshot.series.bars[selection.start - 1] if selection.start else None
        use_previous = previous is not None and (
            snapshot.series.interval != "minute" or is_index(snapshot.series.symbol)
        )
        reference = previous.close if use_previous else (bars[0].open or bars[0].close)
        window_return = (bars[-1].close / reference - 1) * 100
        volume = sum(b.volume_shares for b in bars)
        amount = (
            sum(b.amount_yuan for b in bars)
            if all(b.amount_yuan is not None for b in bars)
            else None
        )
        metrics = (
            Metric("return", "区间涨跌", window_return, "%"),
            Metric("amount", "区间成交额", amount, "元"),
            Metric(
                "vwap",
                "区间成交均价",
                amount / volume
                if amount is not None and volume and not is_index(snapshot.series.symbol)
                else None,
                "价格",
                "指数不适用" if is_index(snapshot.series.symbol) else "缺少数据",
            ),
            Metric(
                "relative",
                "相对基准",
                self._relative_return(snapshot, bars, window_return),
                "百分点",
            ),
            Metric("volume", "区间成交量", volume, "股"),
            Metric("institutional", "机构净买入", None, "元", "无法识别"),
        )
        return AnalysisReport(
            snapshot.revision,
            selection,
            f"区间基础统计 · {len(bars)} 条记录",
            metrics,
            (),
            snapshot.quality_notes
            + (
                "区间涨跌以选区前一条收盘价为基准。"
                if use_previous
                else "区间涨跌以首条开盘／分时价为基准。",
            ),
            self.version,
        )

    @staticmethod
    def _relative_return(snapshot, bars, window_return):
        if (
            is_index(snapshot.series.symbol)
            or snapshot.series.interval not in ("day", "minute")
            or snapshot.benchmark is None
        ):
            return None
        benchmark = snapshot.benchmark.bars
        selected = tuple(
            b for b in benchmark if bars[0].timestamp <= b.timestamp <= bars[-1].timestamp
        )
        if len(selected) < 2:
            return None
        previous = tuple(b for b in benchmark if b.timestamp < bars[0].timestamp)
        reference = (
            previous[-1].close
            if previous and snapshot.series.interval == "day"
            else (selected[0].open or selected[0].close)
        )
        return window_return - (selected[-1].close / reference - 1) * 100
