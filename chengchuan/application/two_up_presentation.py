"""Shared plain-text output makes the target, abstention and denominator explicit."""


def pct(value):
    return "—" if value is None else f"{value:.1%}"


def prediction_text(p):
    names = {1: "上涨", -1: "下跌", 0: "持平", None: "不预测"}
    lines = [f"{p.as_of} → 第三天 {p.target or '日历未覆盖'}：{names[p.direction]}", p.reason]
    if len(p.event_returns) == 3:
        lines.append(
            "前日／第一天／第二天涨跌：" + " / ".join(f"{r:+.2f}%" for r in p.event_returns)
        )
    lines.append(f"成熟训练事件 {p.train_count}；历史验证事件 {p.validation_count}")
    if p.validation_summary:
        s = p.validation_summary
        lines += [
            f"验证期成立事件 {s.get('validation_events', p.validation_count)}；有效验证样本 {p.validation_count}",
            f"验证期信号 {s.get('count', 0)} 次，命中 {pct(s.get('accuracy'))}，覆盖 {pct(s.get('coverage'))}",
            "验证期用于选择模型与阈值，不代表独立测试达到80%。",
        ]
    if p.direction is None:
        lines.append("本次没有正式方向；未将内部候选方向当成预测结果。")
    return lines


def summary_text(s):
    lines = [
        s["acceptance"],
        f"成立事件 {s['confirmed_events']}；实际预测 {s['called_events']}；可核对 {s['scored_events']}",
        f"命中率 {pct(s['accuracy'])}；覆盖率 {pct(s['coverage'])}；标签覆盖 {pct(s['label_coverage'])}",
        f"正式预测缺少结果 {s['unresolved_signals']} 次；缺失按未命中计的保守命中率 {pct(s['conservative_accuracy'])}",
        f"同样本始终猜涨 {pct(s['always_up_accuracy'])}；始终猜跌 {pct(s['always_down_accuracy'])}",
        f"条件不成立 {s['not_event_days']} 天；无法确认条件 {s['unknown_condition_days']} 天",
    ]
    if s.get("wilson95"):
        lines.append("命中率95%区间：" + " ～ ".join(pct(v) for v in s["wilson95"]))
    lines.append(
        f"未筛选候选诊断：{s['diagnostic_candidate_count']}次，命中{pct(s['diagnostic_candidate_accuracy'])}；非正式信号"
    )
    return lines
