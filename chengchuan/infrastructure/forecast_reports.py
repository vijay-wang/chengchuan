import csv
import json
from dataclasses import asdict
from pathlib import Path
from tempfile import TemporaryDirectory


def pct(value):
    return "—" if value is None else f"{value * 100:.1f}%"


def markdown(run):
    m = run.manifest
    rows = [
        f"# {m['name']}（{m['symbol']}）预测研究报告",
        "",
        "**工程诊断：未经独立封存验证、未校准；不能视作已验证的未来胜率。**",
        "",
        f"实验：`{run.experiment_id}`；模式：`{m['mode']}`；目标：`{m['target_version']}`。",
        f"本次实际执行时间：{run.executed_at}；历史逻辑截止点见基准日／评估上界。",
        "",
    ]
    if m.get("before"):
        rows += [f"评估目标日期严格早于 {m['before']}，按交易所日历确定H1/H2。", ""]
    if run.summaries:
        rows += [
            "| 组别 | 目标 | 可评价方向数 | 命中率 | 同样本全猜涨 | 训练多数类 | 沿用今日方向 | 预测覆盖率 | A/B/C共同样本命中率 |",
            "|---|---|---:|---:|---:|---:|---:|---:|---:|",
        ]
        for s in run.summaries:
            rows.append(
                f"| {s['group']} | H{s['horizon']} | {s['scored']} | {pct(s['accuracy'])} | "
                f"{pct(s['always_up_accuracy'])} | {pct(s['majority_accuracy'])} | "
                f"{pct(s['momentum_accuracy'])} | {pct(s['forecast_coverage'])} | {pct(s['common_accuracy'])} |"
            )
        rows += [
            "",
            "A为价格结构，B增加量价，C增加固定5/20日阶段评分。基线与对应模型使用相同可评价记录。",
            "",
            "完整JSON保留输入／标签覆盖、状态数量、原始概率损失、校准分组和探索性日期块区间。",
            "探索性区间按时间排序的有效记录分块（每块最多5条），200次重采样；缺口可能跨越，不是严格交易日块或跨股票验证。样本少于20时不输出。",
            "",
        ]
    else:
        rows += [
            "| 基准日 | 目标日期 | 目标 | 状态 | 研究方向 | 训练样本 | 原因 |",
            "|---|---|---|---|---|---:|---|",
        ]
        for p in run.predictions:
            name = {1: "上涨", 0: "不变", -1: "下跌", None: "不预测"}[p.direction]
            rows.append(
                f"| {p.as_of} | {p.target or '日历未覆盖'} | H{p.horizon} | {p.status} | {name} | {p.train_count} | {p.reason} |"
            )
    rows += ["", "## 数据与能力边界", ""]
    rows += [f"- {note}" for note in run.notes]
    rows += [
        "",
        f"来源快照版本：`{m['snapshot_revision']}`。模型：`{m['model_version']}`。",
        "公共校准概率未启用；内部raw_probabilities为平滑后的近邻类别频率，仅供实验评分。",
        "",
    ]
    return "\n".join(rows)


def write_run(run, prefix):
    prefix = Path(prefix)
    prefix.parent.mkdir(parents=True, exist_ok=True)
    paths = {
        kind: Path(str(prefix) + suffix)
        for kind, suffix in (("json", ".json"), ("markdown", ".md"), ("csv", ".csv"))
    }
    # Build complete files before replacing exports. A cancelled process cannot truncate an old file.
    with TemporaryDirectory(prefix=".forecast-export-", dir=prefix.parent) as folder:
        staged = {kind: Path(folder) / path.name for kind, path in paths.items()}
        _write_files(run, staged)
        for kind, path in paths.items():
            staged[kind].replace(path)
    return {key: str(path.resolve()) for key, path in paths.items()}


def _write_files(run, paths):
    paths["json"].write_text(
        json.dumps(asdict(run), default=str, ensure_ascii=False, indent=2, allow_nan=False) + "\n",
        encoding="utf-8",
    )
    paths["markdown"].write_text(markdown(run), encoding="utf-8")
    evaluations = {r.prediction_id: r for r in run.evaluations}
    fields = [
        "symbol",
        "as_of",
        "target",
        "horizon",
        "group",
        "status",
        "direction",
        "train_count",
        "training_label_through",
        "actual_direction",
        "actual_return_pct",
        "correct",
        "reason",
        "evaluation_reason",
        "feature_revision",
        "model_id",
        "record_id",
    ]
    with paths["csv"].open("w", encoding="utf-8-sig", newline="") as stream:
        writer = csv.DictWriter(stream, fieldnames=fields)
        writer.writeheader()
        for p in run.predictions:
            row = {key: getattr(p, key) for key in fields if hasattr(p, key)}
            e = evaluations.get(p.record_id)
            if e:
                row.update(
                    actual_direction=e.actual_direction,
                    actual_return_pct=e.actual_return_pct,
                    correct=e.correct,
                    evaluation_reason=e.reason,
                )
            writer.writerow(row)
