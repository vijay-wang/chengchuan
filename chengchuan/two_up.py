"""Offline exact-two-up research CLI; data collection is a separate explicit action."""

import argparse
import json
from datetime import date
from pathlib import Path

from chengchuan.application.two_up import TwoUpService
from chengchuan.domain.market import stock_symbol
from chengchuan.domain.two_up import TwoUpConfig
from chengchuan.infrastructure.forecast_journal import SqliteForecastJournal
from chengchuan.infrastructure.two_up_inputs import CachedTwoUpInputs
from chengchuan.infrastructure.two_up_reports import write_run


def main(argv=None):
    parser = argparse.ArgumentParser(description="恰好连涨两天，盘后预测第三天；CPU／离线历史验证")
    parser.add_argument("command", choices=("predict", "backtest"))
    parser.add_argument("--symbol", default="002318")
    parser.add_argument("--as-of", type=date.fromisoformat)
    parser.add_argument("--start", type=date.fromisoformat)
    parser.add_argument("--before", type=date.fromisoformat)
    parser.add_argument("--exclude-date", action="append", default=[])
    parser.add_argument("--store", type=Path, default=Path(".chengchuan/two-up/research.sqlite"))
    parser.add_argument("--output", type=Path, default=Path("reports/two_up"))
    args = parser.parse_args(argv)
    try:
        loader = CachedTwoUpInputs()
        h = loader.load(stock_symbol(args.symbol))
        if h is None:
            raise ValueError("本地缺少该股日线和可比昨收，请先在行情页取得数据")
        config = TwoUpConfig(excluded_dates=tuple(args.exclude_date))
        service = TwoUpService(SqliteForecastJournal(args.store), loader.history, loader.pool)
        run = (
            service.predict(h.snapshot, args.as_of, config)
            if args.command == "predict"
            else service.backtest(h.snapshot, args.before, config, args.start)
        )
        print(
            json.dumps(
                dict(files=write_run(run, args.output), note="80%目标以报告为准；拒判不算命中"),
                ensure_ascii=False,
                indent=2,
            )
        )
        return 0
    except (ValueError, OSError, RuntimeError) as exc:
        parser.exit(2, str(exc) + "\n")


if __name__ == "__main__":
    raise SystemExit(main())
