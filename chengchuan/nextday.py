"""Offline CPU next-session prediction CLI, with no desktop or GPU dependency."""

import argparse
import json
from datetime import date
from pathlib import Path

from chengchuan.application.nextday import NextDayService
from chengchuan.domain.nextday import NextDayConfig
from chengchuan.infrastructure.forecast_input import load_input
from chengchuan.infrastructure.forecast_journal import SqliteForecastJournal
from chengchuan.infrastructure.nextday_reports import write_run


def main(argv=None):
    parser = argparse.ArgumentParser(description="澄川下一交易日CPU研究：离线预测与逐日验证")
    parser.add_argument("command", choices=("predict", "backtest"))
    parser.add_argument("--dataset", required=True, type=Path, help="真实日线快照")
    parser.add_argument("--minute-dataset", type=Path, help="14:35模式使用的真实1分钟OHLC快照")
    parser.add_argument("--mode", choices=("close", "cut1435"), default="close")
    parser.add_argument("--as-of", type=date.fromisoformat, help="预测基准日D")
    parser.add_argument("--before", type=date.fromisoformat, help="回测目标日期严格早于此日期")
    parser.add_argument(
        "--start", type=date.fromisoformat, help="回测起始基准日；训练可使用更早历史"
    )
    parser.add_argument("--neighbors", type=int, default=21)
    parser.add_argument("--min-train", type=int, default=60)
    parser.add_argument("--train-window", type=int, default=240)
    parser.add_argument(
        "--exclude-date", action="append", default=[], help="排除公司行为／异常日期，可重复"
    )
    parser.add_argument("--store", type=Path, default=Path(".chengchuan/forecast/nextday.sqlite"))
    parser.add_argument("--output", type=Path, default=Path("reports/nextday_research"))
    args = parser.parse_args(argv)
    try:
        snapshot, observed_at, quote_at = load_input(args.dataset)
        minute = load_input(args.minute_dataset)[0] if args.minute_dataset else None
        config = NextDayConfig(
            args.neighbors, args.min_train, args.train_window, tuple(args.exclude_date)
        )
        service = NextDayService(journal=SqliteForecastJournal(args.store))
        if args.command == "predict":
            run = service.predict(
                snapshot,
                args.as_of,
                config,
                observed_at=observed_at,
                quote_at=quote_at,
                mode=args.mode,
                minute_snapshot=minute,
            )
        else:
            run = service.backtest(
                snapshot,
                args.before,
                config,
                start=args.start,
                mode=args.mode,
                minute_snapshot=minute,
            )
        print(
            json.dumps(
                {
                    "experiment": run.experiment_id,
                    "files": write_run(run, args.output),
                    "note": "CPU历史研究；评分未校准，完整覆盖率和基线见报告",
                },
                ensure_ascii=False,
                indent=2,
            )
        )
        return 0
    except (ValueError, OSError, RuntimeError, KeyError, TypeError) as exc:
        parser.exit(2, "下一交易日研究：" + str(exc) + "\n")


if __name__ == "__main__":
    raise SystemExit(main())
