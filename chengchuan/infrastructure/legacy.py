"""旧原型的边界适配；其数据类型不会进入新UI或应用层。"""

import hashlib
import json
from datetime import datetime
from pathlib import Path

from chengchuan.domain.market import is_index
from chengchuan.domain.models import (
    SHANGHAI,
    AnalysisReport,
    Finding,
    IndexRange,
    MarketBar,
    MarketSeries,
    MarketSnapshot,
    Metric,
    OrderSizeFlow,
    SeriesKind,
)
from flowradar.core import Bar, Dataset, Flow, analyze


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


class LegacyAnalysisEngine:
    version = "flowradar-heuristic-v0.1-unvalidated/adapter-2"

    def analyze(self, snapshot: MarketSnapshot, selection: IndexRange) -> AnalysisReport:
        series = snapshot.series
        if is_index(series.symbol) or series.interval not in ("day", "minute"):
            return self._period_statistics(snapshot, selection)
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
        data = Dataset(
            series.symbol,
            series.name,
            series.interval,
            _legacy_bars(series),
            flows,
            _legacy_bars(snapshot.benchmark),
            quality_notes=list(snapshot.quality_notes),
        )
        selected = data.bars[selection.start : selection.stop]
        result = analyze(data, selected[0].timestamp, selected[-1].timestamp)
        m = result["metrics"]
        metrics = (
            Metric("return", "区间涨跌", m["return_pct"], "%"),
            Metric("amount", "区间成交额", m["amount_yuan"], "元"),
            Metric("vwap", "区间成交均价", m["vwap"], "价格"),
            Metric("relative", "相对基准", m["relative_return_pct"], "百分点"),
            Metric("volume", "区间成交量", m["volume_shares"], "股"),
            Metric("institutional", "机构净买入", None, "元", "无法识别"),
        )
        findings = tuple(
            Finding(s["name"], s["evidence_score"], tuple(s["evidence"])) for s in result["stages"]
        )
        return AnalysisReport(
            snapshot.revision,
            selection,
            result["summary"],
            metrics,
            findings,
            tuple(result["quality_notes"]),
            self.version,
        )

    def _period_statistics(self, snapshot, selection):
        bars = snapshot.series.bars[selection.start : selection.stop]
        previous = snapshot.series.bars[selection.start - 1] if selection.start else None
        reference = previous.close if previous else (bars[0].open or bars[0].close)
        volume = sum(b.volume_shares for b in bars)
        amount = (
            sum(b.amount_yuan for b in bars)
            if all(b.amount_yuan is not None for b in bars)
            else None
        )
        metrics = (
            Metric("return", "区间涨跌", (bars[-1].close / reference - 1) * 100, "%"),
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
            Metric("relative", "相对基准", None, "百分点"),
            Metric("volume", "区间成交量", volume, "股"),
            Metric("institutional", "机构净买入", None, "元", "无法识别"),
        )
        return AnalysisReport(
            snapshot.revision,
            selection,
            "指数展示区间基础统计，未套用个股阶段规则。"
            if is_index(snapshot.series.symbol)
            else "当前周期展示区间基础统计；阶段规则尚未在此周期核验。",
            metrics,
            (),
            snapshot.quality_notes
            + ("区间涨跌以选区前一条收盘价为基准；无前序时使用首条开盘／分时价。",),
            self.version,
        )
