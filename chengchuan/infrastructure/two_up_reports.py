"""Portable event research output; old next-day archives remain untouched."""

import csv
import json
from dataclasses import asdict
from pathlib import Path
from tempfile import TemporaryDirectory

from chengchuan.application.two_up_presentation import prediction_text, summary_text


def write_run(run, prefix):
    prefix = Path(prefix)
    prefix.parent.mkdir(parents=True, exist_ok=True)
    paths = {
        k: Path(str(prefix) + ext)
        for k, ext in (("json", ".json"), ("markdown", ".md"), ("csv", ".csv"))
    }
    with TemporaryDirectory(prefix=".two-up-export-", dir=prefix.parent) as folder:
        staged = {k: Path(folder) / p.name for k, p in paths.items()}
        staged["json"].write_text(
            json.dumps(asdict(run), default=str, ensure_ascii=False, indent=2, allow_nan=False)
            + "\n",
            encoding="utf-8",
        )
        lines = ["# 恰好连涨两天后的第三天预测", "", "80%为验收目标，不是未经验证的承诺。", ""]
        for s in run.summaries:
            lines += summary_text(s) + [""]
        if not run.summaries:
            for p in run.predictions:
                lines += prediction_text(p) + [""]
        lines += [
            *run.notes,
            "",
            "```json",
            json.dumps(run.manifest, ensure_ascii=False, default=str, indent=2),
            "```",
            "",
        ]
        staged["markdown"].write_text("\n".join(lines), encoding="utf-8")
        outcomes = {e.prediction_id: e for e in run.evaluations}
        fields = (
            list(asdict(run.predictions[0]))
            if run.predictions
            else ["record_id", "symbol", "as_of", "target", "status"]
        )
        extra = [
            "actual_return_pct",
            "actual_direction",
            "correct",
            "evaluation_status",
            "evaluation_reason",
        ]
        with staged["csv"].open("w", encoding="utf-8-sig", newline="") as stream:
            writer = csv.DictWriter(stream, fieldnames=fields + extra)
            writer.writeheader()
            for p in run.predictions:
                row = asdict(p)
                e = outcomes.get(p.record_id)
                if e:
                    row.update(
                        actual_return_pct=e.actual_return_pct,
                        actual_direction=e.actual_direction,
                        correct=e.correct,
                        evaluation_status=e.status,
                        evaluation_reason=e.reason,
                    )
                for k, v in row.items():
                    if isinstance(v, (tuple, list, dict)):
                        row[k] = json.dumps(v, ensure_ascii=False, allow_nan=False)
                writer.writerow(row)
        for k, path in paths.items():
            staged[k].replace(path)
    return {k: str(p.resolve()) for k, p in paths.items()}
