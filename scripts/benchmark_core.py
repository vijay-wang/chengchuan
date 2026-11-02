"""小样本内存分析基准；不代表大规模图表帧时或网络性能。"""

import argparse
import json
import os
import platform
from datetime import datetime, timezone
from math import ceil
from pathlib import Path
from statistics import median
from time import perf_counter

from chengchuan.application.services import AnalysisService
from chengchuan.domain.models import IndexRange
from chengchuan.infrastructure.legacy import LegacyAnalysisEngine, LegacyJsonRepository


def timings(function, repeat):
    values = []
    for _ in range(repeat):
        start = perf_counter()
        function()
        values.append((perf_counter() - start) * 1000)
    return {
        "p50_ms": round(median(values), 4),
        "p95_ms": round(sorted(values)[ceil(0.95 * len(values)) - 1], 4),
        "max_ms": round(max(values), 4),
    }


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--repeat", type=int, default=50)
    parser.add_argument("--output", type=Path)
    args = parser.parse_args()
    if args.repeat < 5:
        parser.error("repeat至少为5")
    root = Path(__file__).resolve().parents[1]
    results = []
    for file, count in (
        ("jiuli_20261019_1102.dataset.json", 11),
        ("jiuli_20261102_full_day.dataset.json", 242),
    ):
        snapshot = LegacyJsonRepository().load(root / "reports" / file)
        selection = IndexRange(len(snapshot.series.bars) - count, len(snapshot.series.bars))
        engine = LegacyAnalysisEngine()
        service = AnalysisService(engine)
        service.run(snapshot, selection)
        results.append(
            {
                "file": file,
                "loaded_bars": len(snapshot.series.bars),
                "selected_bars": count,
                "uncached_analysis": timings(
                    lambda: engine.analyze(snapshot, selection), args.repeat
                ),
                "cached_use_case": timings(lambda: service.run(snapshot, selection), args.repeat),
            }
        )
    output = {
        "measured_at_utc": datetime.now(timezone.utc).isoformat(),
        "environment": {
            "python": platform.python_version(),
            "system": platform.system(),
            "machine": platform.machine(),
            "logical_cpus": os.cpu_count(),
        },
        "repeat": args.repeat,
        "results": results,
        "scope": "本机、小规模已保存真实快照、内存分析；不包含网络、图表绘制、逐笔数据或长时间运行。",
    }
    encoded = json.dumps(output, ensure_ascii=False, indent=2) + "\n"
    if args.output:
        args.output.parent.mkdir(parents=True, exist_ok=True)
        args.output.write_text(encoded, encoding="utf-8")
    print(encoded)


if __name__ == "__main__":
    main()
