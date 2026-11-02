from __future__ import annotations

from dataclasses import asdict, dataclass, field
from datetime import datetime
from math import isfinite, log, sqrt
from statistics import mean, pstdev
from typing import Any


@dataclass(frozen=True)
class Bar:
    timestamp: str
    open: float
    high: float
    low: float
    close: float
    volume_shares: float
    amount_yuan: float | None = None

    def __post_init__(self):
        datetime.fromisoformat(self.timestamp)
        values = (self.open, self.high, self.low, self.close, self.volume_shares)
        if not all(isfinite(x) for x in values) or min(values[:4]) <= 0:
            raise ValueError("价格必须为有限正数，成交量必须有限")
        if self.volume_shares < 0 or self.high < max(self.open, self.close, self.low):
            raise ValueError("成交量或OHLC关系无效")
        if self.low > min(self.open, self.close):
            raise ValueError("最低价不得超过开盘价或收盘价")
        if self.amount_yuan is not None and (
            not isfinite(self.amount_yuan) or self.amount_yuan < 0
        ):
            raise ValueError("成交额必须为有限非负数")


@dataclass(frozen=True)
class Flow:
    """Incremental platform order-size flow, never an investor's inventory."""

    timestamp: str
    main_yuan: float
    small_yuan: float
    medium_yuan: float
    large_yuan: float
    super_yuan: float

    def __post_init__(self):
        datetime.fromisoformat(self.timestamp)
        nums = (self.main_yuan, self.small_yuan, self.medium_yuan,
                self.large_yuan, self.super_yuan)
        if not all(isfinite(x) for x in nums):
            raise ValueError("资金分类数据必须有限")
        if abs(self.main_yuan - self.large_yuan - self.super_yuan) > 10:
            raise ValueError("主力分类金额与大单、超大单之和不一致")
        if abs(self.main_yuan + self.small_yuan + self.medium_yuan) > 10:
            raise ValueError("资金分类净额不守恒")


@dataclass
class Dataset:
    symbol: str
    name: str
    interval: str
    bars: list[Bar]
    flows: list[Flow] = field(default_factory=list)
    benchmark: list[Bar] = field(default_factory=list)
    provenance: list[dict[str, Any]] = field(default_factory=list)
    quality_notes: list[str] = field(default_factory=list)
    tape_diagnostics: dict[str, Any] | None = None

    def __post_init__(self):
        if self.interval not in ("day", "minute"):
            raise ValueError("interval 只支持 day 或 minute")
        for rows in (self.bars, self.flows, self.benchmark):
            rows.sort(key=lambda x: x.timestamp)
            stamps = [x.timestamp for x in rows]
            if len(stamps) != len(set(stamps)):
                raise ValueError("数据包含重复时间戳")

    def to_dict(self):
        return asdict(self)

    @classmethod
    def from_dict(cls, obj):
        return cls(**{**obj,
                      "bars": [Bar(**x) for x in obj["bars"]],
                      "flows": [Flow(**x) for x in obj.get("flows", [])],
                      "benchmark": [Bar(**x) for x in obj.get("benchmark", [])]})


def boundary(value: str, end: bool = False) -> str:
    if len(value) == 10:
        value += "T23:59:59" if end else "T00:00:00"
    stamp = datetime.fromisoformat(value)
    if stamp.tzinfo is not None:
        raise ValueError("请输入北京时间，不附带时区偏移")
    return stamp.isoformat(timespec="seconds")


def analyze(data: Dataset, start: str, end: str) -> dict[str, Any]:
    start, end = boundary(start), boundary(end, True)
    if start > end:
        raise ValueError("开始时间不能晚于结束时间")
    bars = [b for b in data.bars if start <= b.timestamp <= end]
    if len(bars) < 2:
        raise ValueError("所选区间至少需要两条有效行情；请扩大范围或补充数据")
    notes = list(data.quality_notes)
    if data.bars[0].timestamp[:10] > start[:10]:
        notes.append("源数据未覆盖请求的开始日期；本报告仅分析实际覆盖区间，不代表完整请求窗口。")
    if data.bars[-1].timestamp[:10] < end[:10]:
        notes.append("源数据未覆盖请求的结束日期；本报告仅分析实际覆盖区间。")
    requested_days = (datetime.fromisoformat(end).date() -
                      datetime.fromisoformat(start).date()).days
    if data.interval == "minute" and requested_days >= 7:
        notes.append("小时级数据覆盖不足一周时，不能替代多周逐笔识别；请核对实际覆盖区间。")
    previous = [b for b in data.bars if b.timestamp < start]
    baseline = previous[-20:] if data.interval == "day" else [
        b for b in previous if b.timestamp[:10] < start[:10]
    ]
    reference = previous[-1].close if previous and data.interval == "day" else bars[0].open
    close = bars[-1].close
    window_return = (close / reference - 1) * 100
    peak = bars[0].close
    drawdown = 0.0
    changes, logs, up_volume = [], [], 0.0
    prev_close = bars[0].open
    for b in bars:
        peak = max(peak, b.close)
        drawdown = min(drawdown, (b.close / peak - 1) * 100)
        changes.append(abs(b.close - prev_close))
        logs.append(log(b.close / prev_close))
        if b.close > prev_close:
            up_volume += b.volume_shares
        prev_close = b.close
    volume = sum(b.volume_shares for b in bars)
    has_amount = all(b.amount_yuan is not None for b in bars)
    amount = sum(b.amount_yuan for b in bars) if has_amount else None
    vwap = amount / volume if amount is not None and volume > 0 else None
    lo, hi = min(b.low for b in bars), max(b.high for b in bars)
    location = (close - lo) / (hi - lo) if hi > lo else .5
    efficiency = abs(close - bars[0].open) / sum(changes) if sum(changes) else 0
    baseline_days = len({b.timestamp[:10] for b in baseline})
    if data.interval == "day":
        expected_volume = mean(b.volume_shares for b in baseline) * len(bars) if baseline else None
    else:
        clock = {}
        for b in baseline:
            clock.setdefault(b.timestamp[11:16], []).append(b.volume_shares)
        matched = [mean(clock[b.timestamp[11:16]]) for b in bars if b.timestamp[11:16] in clock]
        expected_volume = sum(matched) if len(matched) == len(bars) else None
    volume_ratio = volume / expected_volume if expected_volume and expected_volume > 0 else None
    selected_stamps = {b.timestamp for b in bars}
    flow = [f for f in data.flows if f.timestamp in selected_stamps]
    flow_totals = {
        key: sum(getattr(f, key) for f in flow)
        for key in ("main_yuan", "super_yuan", "large_yuan", "medium_yuan", "small_yuan")
    } if flow else None
    coverage = len(flow) / len(bars)
    bm = [b for b in data.benchmark if bars[0].timestamp <= b.timestamp <= bars[-1].timestamp]
    bm_previous = [b for b in data.benchmark if b.timestamp < start]
    if len(bm) >= 2:
        bm_ref = bm_previous[-1].close if bm_previous and data.interval == "day" else bm[0].open
        bm_return = (bm[-1].close / bm_ref - 1) * 100
    else:
        bm_return = None
    relative = window_return - bm_return if bm_return is not None else None
    above_vwap = vwap is not None and close >= vwap
    up_share = up_volume / volume if volume else None
    main_ratio = (flow_totals["main_yuan"] / amount
                  if flow_totals and amount and coverage >= .8 else None)
    metrics = {
        "return_pct": window_return, "last_price": close, "reference_price": reference,
        "reference_definition": "区间前一条日线收盘价" if previous and data.interval == "day" else "区间第一条行情开盘／采样价",
        "volume_shares": volume, "amount_yuan": amount,
        "vwap": vwap, "close_vs_vwap_pct": (close / vwap - 1) * 100 if vwap else None,
        "close_location": location, "close_series_max_drawdown_pct": drawdown,
        "path_efficiency": efficiency, "up_bar_volume_share": up_share,
        "realized_volatility_pct": pstdev(logs) * sqrt(len(logs)) * 100,
        "volume_ratio_to_prior": volume_ratio, "baseline_days": baseline_days,
        "benchmark_return_pct": bm_return, "relative_return_pct": relative,
        "flow_coverage": coverage, "platform_flow_yuan": flow_totals,
        "main_classified_net_to_turnover": main_ratio,
    }
    # Transparent research heuristics. Scores are NOT calibrated probabilities.
    rules = {
        "承接／疑似吸筹": [
            (abs(window_return) <= 3, 15, "区间价格变化较小"),
            (location >= .65, 15, "收盘位于区间偏上部"),
            (above_vwap, 15, "收盘不低于成交均价"),
            (drawdown > -3, 15, "收盘序列回撤较浅"),
            (relative is not None and relative > 0, 15, "跑赢同期基准"),
            (main_ratio is not None and main_ratio > .02, 15, "大额订单分类净买入偏强"),
            (up_share is not None and up_share > .55, 10, "上涨价格区间成交占比较高"),
        ],
        "拉升／趋势上行": [
            (window_return >= 2, 25, "区间价格上涨至少2%"),
            (location >= .8, 20, "收盘接近区间高位"),
            (above_vwap, 15, "收盘高于成交均价"),
            (relative is not None and relative > 1, 20, "跑赢基准超过1个百分点"),
            (volume_ratio is not None and volume_ratio > 1.2, 10, "成交量高于此前可比区间"),
            (efficiency > .35, 10, "价格路径方向性较强"),
        ],
        "卖压／疑似派发": [
            (window_return <= -2, 25, "区间价格下跌至少2%"),
            (location <= .25, 20, "收盘接近区间低位"),
            (vwap is not None and close < vwap, 15, "收盘低于成交均价"),
            (relative is not None and relative < -1, 15, "显著跑输基准"),
            (main_ratio is not None and main_ratio < -.02, 15, "大额订单分类净卖出偏强"),
            (window_return < 0 and volume_ratio is not None and volume_ratio > 1.2, 10, "下跌伴随成交增加"),
        ],
        "回撤承接／洗盘候选": [
            (drawdown <= -1.5, 20, "区间内发生明显回撤"),
            (location >= .7, 25, "回撤后收于区间偏上部"),
            (above_vwap, 20, "最终收盘重回成交均价之上"),
            (relative is not None and relative > 0, 15, "相对基准保持强势"),
            (main_ratio is not None and main_ratio > 0, 10, "大额订单分类未呈净卖出"),
            (window_return > 0, 10, "区间最终上涨"),
        ],
    }
    stages = sorted([
        {"name": name, "evidence_score": sum(weight for ok, weight, _ in items if ok),
         "evidence": [reason for ok, _, reason in items if ok],
         "calibrated_probability": None}
        for name, items in rules.items()
    ], key=lambda x: x["evidence_score"], reverse=True)
    prior_trend = (baseline[-1].close / baseline[0].close - 1) * 100 if len(baseline) >= 2 else None
    for item in stages:
        if item["name"] == "回撤承接／洗盘候选" and not (
            drawdown <= -1.5 and location >= .7 and prior_trend is not None and prior_trend > 3
        ):
            item["evidence_score"] = 0
            item["evidence"] = ["未同时满足此前上行、区间回撤和回收条件，不启用洗盘候选。"]
        if item["name"] == "承接／疑似吸筹" and window_return > 5:
            item["evidence_score"] = min(item["evidence_score"], 50)
            item["evidence"].append("上涨幅度较大，需优先与趋势拉升解释比较。")
    stages.sort(key=lambda x: x["evidence_score"], reverse=True)
    counters = [
        "行情和订单金额分类不揭示真实账户；无法据此认定机构净增持。",
        "指数跟随、多人追涨、回补及被动调仓也可能造成类似价格路径。",
        "吸筹和建仓并非互斥；洗盘含交易动机，实时分析只能给出候选解释。",
    ]
    if coverage < .8:
        notes.append("订单金额分类覆盖不足80%，不使用分类净额强化阶段评分。")
    if baseline_days < 5:
        notes.append("历史可比样本不足5天，成交量比较仅供探索。")
    notes.append("阶段评分为未校准的研究规则分数，不能读作准确率或概率。")
    if data.interval == "day":
        notes.append("当前公开日线使用未复权价格；跨除权除息区间应另行核查公司行为。")
    if data.tape_diagnostics and data.tape_diagnostics.get("three_second_grid_share", 0) > .9:
        notes.append("公开成交明细存在3秒时间格点，不可直接据此识别3秒下单算法。")
    actual_start, actual_end = bars[0].timestamp, bars[-1].timestamp
    return {
        "schema_version": "0.1", "model_version": "heuristic-v0.1-unvalidated",
        "symbol": data.symbol, "name": data.name, "interval": data.interval,
        "requested_window": {"start": start, "end": end},
        "actual_window": {"start": actual_start, "end": actual_end},
        "observations": len(bars), "metrics": metrics, "stages": stages,
        "summary": stages[0]["name"] if stages[0]["evidence_score"] >= 55 else "证据不足／混合交易",
        "institutional_net_purchase_estimate": {
            "low_yuan": None, "high_yuan": None, "status": "not_identifiable",
            "reason": "公开数据缺少投资者或母单标识；尚无经验证的金额估计模型，不能编造机构净买入区间。",
        },
        "execution_model_identification": {
            "status": "insufficient_order_level_data",
            "candidates": ["随机TWAP", "VWAP", "POV", "限价执行", "分散投资者共同交易"],
            "reason": "分钟行情和大小单净额不是某个参与者的成交轨迹，暂不输出算法匹配概率。",
        },
        "counter_hypotheses": counters, "quality_notes": list(dict.fromkeys(notes)),
        "provenance": data.provenance,
        "chart_bars": [asdict(b) for b in bars],
    }


def walk_forward(data: Dataset, lookback: int = 20, horizon: int = 5):
    """Outcome audit, not a test of hidden investor identities."""
    if data.interval != "day" or lookback < 2 or horizon < 1:
        raise ValueError("历史观察评估需要日线、lookback>=2、horizon>=1")
    rows = []
    for i in range(lookback - 1, len(data.bars) - horizon):
        # Analysis filters all features/baselines to timestamps <= as_of.
        report = analyze(data, data.bars[i-lookback+1].timestamp, data.bars[i].timestamp)
        forward = (data.bars[i+horizon].close / data.bars[i].close - 1) * 100
        rows.append({"as_of": data.bars[i].timestamp, "candidate": report["summary"],
                     "score": report["stages"][0]["evidence_score"],
                     "forward_return_pct": forward})
    groups = []
    for name in sorted({r["candidate"] for r in rows}):
        values = [r["forward_return_pct"] for r in rows if r["candidate"] == name]
        groups.append({"candidate": name, "samples": len(values),
                       "mean_forward_return_pct": mean(values),
                       "positive_return_share": sum(v > 0 for v in values) / len(values)})
    return {"lookback_bars": lookback, "forward_bars": horizon, "rows": rows, "groups": groups,
            "limitations": ["评估的是后续价格表现，不是主力识别准确率。",
                            "滚动区间和未来收益相互重叠，样本不能视作独立。",
                            "未建立交易策略，不输出可交易收益率或收益承诺。"]}
