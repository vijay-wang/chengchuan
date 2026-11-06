"""Portable next-session research reports with full audit fields and no Qt dependency."""

import csv
import json
from dataclasses import asdict
from pathlib import Path
from tempfile import TemporaryDirectory

from chengchuan.application.nextday_presentation import (
    MODE_NAMES,
    prediction_text,
    summary_text,
)


def markdown(run):
    m = run.manifest
    lines = [
        f"# {m.get('name', '')}（{m.get('symbol', '')}）下一交易日预测研究",
        "",
        "CPU研究模型；模型评分与历史频率均未校准为未来概率。",
        "",
        f"实验：`{run.experiment_id}`；执行时间：{run.executed_at}。",
        f"模式：{MODE_NAMES.get(m.get('forecast_mode'), m.get('forecast_mode', m.get('mode', '')))}。",
        "下一交易日按交易日历确定。14:35特征不包含当日最终收盘；"
        "两种模式均评价下一交易日收盘相对基准日最终收盘的方向。",
        "",
    ]
    if m.get("before"):
        lines += [f"目标日期严格早于 {m['before']}。", ""]
    if run.features:
        f = run.features
        lines += [f"实际输入覆盖：{f.coverage_at or '—'}。", "", *f.evidence]
        if f.levels:
            lines += ["", "## 历史结构参考价", ""]
            lines += [f"- {name}：{price:.3f}" for name, price in f.levels]
        lines += ["", *f.notes, ""]
    if run.summaries:
        lines += ["## 逐日样本外诊断", ""]
        for summary in run.summaries:
            lines += summary_text(summary) + [""]
        lines += [
            "各组与基线使用同样本比较。完整统计如下，包含未预测和不可评价记录的分母：",
            "",
            "```json",
            json.dumps(run.summaries, ensure_ascii=False, indent=2, allow_nan=False),
            "```",
            "",
        ]
    else:
        for prediction in run.predictions:
            lines += prediction_text(prediction) + [""]
    lines += ["## 数据与验证边界", "", *[f"- {note}" for note in run.notes], ""]
    lines += [
        "## 实验清单",
        "",
        "```json",
        json.dumps(m, default=str, ensure_ascii=False, indent=2, allow_nan=False),
        "```",
        "",
    ]
    return "\n".join(lines)


def write_run(run, prefix):
    prefix = Path(prefix)
    prefix.parent.mkdir(parents=True, exist_ok=True)
    paths = {
        k: Path(str(prefix) + s)
        for k, s in (("json", ".json"), ("markdown", ".md"), ("csv", ".csv"))
    }
    with TemporaryDirectory(prefix=".nextday-export-", dir=prefix.parent) as folder:
        staged = {key: Path(folder) / path.name for key, path in paths.items()}
        staged["json"].write_text(
            json.dumps(asdict(run), default=str, ensure_ascii=False, indent=2, allow_nan=False)
            + "\n",
            encoding="utf-8",
        )
        staged["markdown"].write_text(markdown(run), encoding="utf-8")
        evaluations = {row.prediction_id: row for row in run.evaluations}
        fields = (
            list(asdict(run.predictions[0]))
            if run.predictions
            else ["record_id", "symbol", "as_of", "mode", "target", "status"]
        )
        evaluation_fields = [
            "actual_return_pct",
            "actual_direction",
            "correct",
            "actual_shape",
            "shape_correct",
            "actual_ohlc",
            "evaluation_status",
            "evaluation_reason",
        ]
        with staged["csv"].open("w", encoding="utf-8-sig", newline="") as stream:
            writer = csv.DictWriter(stream, fieldnames=fields + evaluation_fields)
            writer.writeheader()
            for prediction in run.predictions:
                row = asdict(prediction)
                evaluation = evaluations.get(prediction.record_id)
                if evaluation:
                    row.update(
                        {
                            k: getattr(evaluation, k)
                            for k in evaluation_fields
                            if hasattr(evaluation, k)
                        }
                    )
                    row.update(
                        evaluation_status=evaluation.status, evaluation_reason=evaluation.reason
                    )
                for key, value in row.items():
                    if isinstance(value, (tuple, list, dict)):
                        row[key] = json.dumps(value, ensure_ascii=False, allow_nan=False)
                writer.writerow(row)
        for key, destination in paths.items():
            staged[key].replace(destination)
    return {key: str(path.resolve()) for key, path in paths.items()}
