"""Plain-text next-session research presentation shared by desktop and exports."""

MODE_NAMES = {"close": "盘后", "cut1435": "14:35"}
DIRECTION_NAMES = {1: "上涨倾向", -1: "下跌倾向", 0: "不变倾向", None: "暂不判断"}
GROUP_NAMES = {"price": "价格", "volume": "价格＋成交量", "full": "多因子"}
RANGE_NAMES = {"open": "开盘", "high": "最高", "low": "最低", "close": "收盘"}
MODEL_NAMES = {"knn": "相似日", "logistic": "正则化线性模型", "base": "历史涨跌基线"}
FEATURE_NAMES = {
    "price_rsi14_simple": "14期相对强弱",
    "price_macd": "MACD趋势",
    "price_macd_histogram": "MACD动量",
    "price_atr14": "14期真实波幅",
    "price_volatility20": "20日波动率",
    "price_bollinger_z": "布林带位置",
    "price_bollinger_width": "布林带宽度",
    "price_range": "日内振幅",
    "price_body": "K线实体",
    "price_gap": "开盘缺口",
    "price_close_location": "收盘位置",
    "price_upper_wick": "上影线",
    "price_lower_wick": "下影线",
    "volume_recent_trend": "近期量能趋势",
    "volume_signed_imbalance20": "20日涨跌成交量差",
    "volume_return_interaction": "涨跌与量比交互",
    "volume_location_interaction": "收盘位置与量比交互",
    "structure_support_touches": "低点附近触及次数",
    "structure_resistance_touches": "高点附近触及次数",
    "structure_breakout": "突破幅度",
    "structure_failed_breakout": "突破后回落幅度",
    "chip_cost_distance": "成交成本代理距离",
    "chip_profit_volume_share": "低于现价成交权重",
    "chip_near_overhang_share": "现价上方成交权重",
    "chip_concentration": "成交价格集中度",
    "chip_width": "成交分布宽度",
    "chip_poc_distance": "成交峰值距离",
    "behavior_streak": "连续涨跌",
    "behavior_up_fraction10": "10日上涨比例",
    "behavior_rejection": "下影反弹代理",
    "behavior_exhaustion": "放量上影代理",
    "behavior_drawdown60": "60日高点回撤",
    "behavior_rebound60": "60日低点反弹",
    "market_return1": "大盘单日涨跌",
    "market_return5": "大盘5日涨跌",
    "market_relative5": "相对大盘5日强弱",
    "market_correlation20": "20日市场相关性",
    "market_beta20": "20日市场敏感度",
    "intraday_recent_volume_ratio": "最近30分钟量比",
    "intraday_last30_return": "最近30分钟涨跌",
    "intraday_vwap_proxy_distance": "分时成交均价代理距离",
}


def feature_name(name):
    for prefix, label in (
        ("price_return_", "日涨跌"),
        ("price_ma_distance_", "日均线距离"),
        ("price_ma_slope_", "日均线斜率"),
        ("price_position_", "日价格位置"),
        ("volume_rv", "日量比"),
        ("structure_support_distance_", "日低点距离"),
        ("structure_resistance_distance_", "日高点距离"),
    ):
        if name.startswith(prefix) and name[len(prefix) :].isdigit():
            return name[len(prefix) :] + label
    return FEATURE_NAMES.get(name, name)


def pct(value):
    return "—" if value is None else f"{value:.1%}"


def interval(value):
    return "—" if value is None else " ～ ".join(pct(v) for v in value)


def prediction_text(prediction):
    """Plain text shared by the desktop and exported report; API strings cannot inject HTML."""
    p = prediction
    lines = [
        f"{MODE_NAMES[p.mode]} → {p.target or '日历未覆盖'}：{DIRECTION_NAMES[p.direction]}",
        p.reason,
        f"逻辑截止：{p.cutoff_at}；训练样本：{p.train_count}",
        f"训练标签可得时点上限：{p.training_label_through or '—'}",
    ]
    if p.components:
        lines += ["", "模型上涨评分（未校准，不是未来上涨概率）："]
        lines += [f"  {MODEL_NAMES.get(name, name)}：{score:.3f}" for name, score in p.components]
    if p.ranges:
        lines += [
            "",
            "相似日次日价格变化分布：P10 / P50 / P90",
            "以下均相对基准日最终收盘，单位%；不是价格保证区间。",
        ]
        if p.mode == "cut1435":
            lines.append("14:35尚不知道当日收盘，不将这些涨跌幅换算为次日绝对价格。")
        for name, values in p.ranges:
            lines.append(
                f"  {RANGE_NAMES.get(name, name)}：" + " / ".join(f"{v:+.2f}%" for v in values)
            )
    if p.shapes:
        total = sum(count for _, count in p.shapes)
        lines += ["", f"相似日次日形态（共{total}例；不代表精确分时路径）："]
        lines += [f"  {shape}：{count}例" for shape, count in p.shapes]
    if p.contributions:
        lines += ["", "线性模型贡献（数值符号为该模型方向，不代表因果）："]
        lines += [f"  {feature_name(name)}：{value:+.3f}" for name, value in p.contributions]
    return lines


def summary_text(summary):
    s = summary
    lines = [
        f"{GROUP_NAMES.get(s['group'], s['group'])}：{s.get('scored', 0)}条可评价方向",
        f"命中 {pct(s.get('accuracy'))}；探索性区间 {interval(s.get('accuracy_interval'))}",
        f"同样本全猜涨 {pct(s.get('always_up_accuracy'))}；"
        f"历史多数类 {pct(s.get('majority_accuracy'))}；"
        f"沿用今日方向 {pct(s.get('momentum_accuracy'))}",
        f"输入覆盖 {pct(s.get('input_coverage'))}；"
        f"预测覆盖 {pct(s.get('forecast_coverage'))}；"
        f"标签覆盖 {pct(s.get('label_coverage'))}",
        f"共同样本 {s.get('common_samples', 0)}条，命中 {pct(s.get('common_accuracy'))}",
        f"相对全猜涨差值 {pct(s.get('paired_up_delta'))}；"
        f"区间 {interval(s.get('paired_up_delta_interval'))}",
    ]
    if "shape_accuracy" in s:
        lines.append(f"相似日多数形态命中 {pct(s.get('shape_accuracy'))}")
    return lines
