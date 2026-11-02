from __future__ import annotations

import argparse
import json
import sys
from datetime import datetime, timedelta
from pathlib import Path
from zoneinfo import ZoneInfo

from .core import Dataset, analyze, boundary, walk_forward
from .data import fetch_public, read_csv
from .reports import write_report


def main(argv=None):
    parser = argparse.ArgumentParser(description="FlowRadar：公开数据资金行为研究原型（不连接交易账户）")
    parser.add_argument("command", choices=["analyze", "evaluate"])
    parser.add_argument("--symbol", default="002318", help="如002318、sh000001、sh515210")
    parser.add_argument("--interval", choices=["day", "minute"], default="day")
    parser.add_argument("--start", help="北京时间日期或时间，如2026-11-02T13:00")
    parser.add_argument("--end", help="北京时间日期或时间")
    parser.add_argument("--benchmark", default="sh000001", help="默认上证指数；空字符串禁用")
    parser.add_argument("--dataset", type=Path, help="使用先前保存的标准数据JSON，离线复算")
    parser.add_argument("--csv", type=Path, help="导入OHLCV CSV；成交量单位为股")
    parser.add_argument("--cache-dir", type=Path, default=Path(".flowradar/cache"))
    parser.add_argument("--refresh", action="store_true", help="重新请求公开接口，避免复用旧快照")
    parser.add_argument("--output", default="reports/flowradar", help="输出文件前缀")
    parser.add_argument("--lookback", type=int, default=20, help="历史观察评估的回看日数")
    parser.add_argument("--horizon", type=int, default=5, help="历史观察评估的后续日数")
    args = parser.parse_args(argv)
    today = datetime.now(ZoneInfo("Asia/Shanghai")).date()
    end = args.end or today.isoformat()
    start = args.start or (today if args.interval == "minute" else today-timedelta(days=30)).isoformat()
    try:
        if args.dataset and args.csv:
            raise ValueError("--dataset 与 --csv 不可同时使用")
        if args.dataset:
            data = Dataset.from_dict(json.loads(args.dataset.read_text(encoding="utf-8")))
        elif args.csv:
            data = read_csv(args.csv, args.symbol, args.interval)
        else:
            data = fetch_public(args.symbol, start, end, args.interval,
                                args.cache_dir, args.refresh, args.benchmark)
        output = Path(args.output)
        output.parent.mkdir(parents=True, exist_ok=True)
        dataset_path = Path(str(output) + ".dataset.json")
        dataset_path.write_text(json.dumps(data.to_dict(), ensure_ascii=False, indent=2, allow_nan=False), encoding="utf-8")
        if args.command == "evaluate":
            result = walk_forward(data, args.lookback, args.horizon)
            a, b = boundary(start), boundary(end, True)
            result["rows"] = [r for r in result["rows"] if a <= r["as_of"] <= b]
            # Recompute groups after restricting evaluation dates.
            from statistics import mean
            result["groups"] = []
            for name in sorted({r["candidate"] for r in result["rows"]}):
                vals = [r["forward_return_pct"] for r in result["rows"] if r["candidate"] == name]
                result["groups"].append({"candidate": name, "samples": len(vals),
                                         "mean_forward_return_pct": mean(vals),
                                         "positive_return_share": sum(v > 0 for v in vals)/len(vals)})
            path = Path(str(output) + ".evaluation.json")
            path.write_text(json.dumps(result, ensure_ascii=False, indent=2, allow_nan=False), encoding="utf-8")
            print(json.dumps({"evaluation": str(path.resolve()), "observations": len(result["rows"]),
                              "note": "后续价格观察，不是主力识别准确率或策略收益率。"}, ensure_ascii=False, indent=2))
        else:
            report = analyze(data, start, end)
            paths = write_report(report, output)
            print(json.dumps({"summary": report["summary"], "actual_window": report["actual_window"],
                              "institutional_amount": "无法从当前公开数据识别", "files": paths,
                              "dataset": str(dataset_path.resolve())}, ensure_ascii=False, indent=2))
        return 0
    except (ValueError, OSError, KeyError, TypeError) as exc:
        print("FlowRadar：" + str(exc), file=sys.stderr)
        return 2
