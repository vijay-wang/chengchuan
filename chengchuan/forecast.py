"""独立预测研究CLI；不导入Qt，不需要模型API密钥。"""

import argparse
import json
from datetime import date
from pathlib import Path

from chengchuan.application.forecast import ForecastService
from chengchuan.application.forecast_features import DailyFeatureExtractor
from chengchuan.domain.analogue import AnalogueModel
from chengchuan.domain.forecast import ForecastConfig
from chengchuan.infrastructure.forecast_input import load_input, save_input
from chengchuan.infrastructure.forecast_journal import SqliteForecastJournal
from chengchuan.infrastructure.forecast_reports import write_run
from chengchuan.infrastructure.forecast_stage import LegacyStageProvider
from chengchuan.infrastructure.market_data import TencentMarketProvider


def main(argv=None):
    parser = argparse.ArgumentParser(
        description="澄川量价预测研究：方向、逐日验证与真实结果对照（未校准）"
    )
    parser.add_argument("command", choices=("predict", "backtest"))
    parser.add_argument("--symbol", default="002318")
    parser.add_argument("--dataset", type=Path, help="使用已有日线快照，离线运行")
    parser.add_argument("--save-source", type=Path, help="保存本次联网研究输入，供离线复算")
    parser.add_argument("--as-of", type=date.fromisoformat, help="预测基准日D")
    parser.add_argument(
        "--before", type=date.fromisoformat, help="回测目标日期严格小于此日期，默认今天"
    )
    parser.add_argument(
        "--start", type=date.fromisoformat, help="首个被评价的预测基准日，训练仍可使用此前数据"
    )
    parser.add_argument("--neighbors", type=int, default=21)
    parser.add_argument("--min-train", type=int, default=40)
    parser.add_argument("--train-window", type=int, default=160)
    parser.add_argument(
        "--exclude-date", action="append", default=[], help="明确排除的公司行为／异常日期，可重复"
    )
    parser.add_argument("--store", type=Path, default=Path(".chengchuan/forecast/research.sqlite"))
    parser.add_argument("--output", default="reports/forecast_research")
    args = parser.parse_args(argv)
    try:
        frame = None if args.dataset else TencentMarketProvider().fetch(args.symbol, "day")
        if args.dataset:
            snapshot, observed_at, quote_at = load_input(args.dataset)
        else:
            snapshot, observed_at = frame.snapshot, frame.retrieved_at
            quote_at = frame.quote.timestamp if frame.quote else None
            if args.save_source:
                save_input(frame, args.save_source)
        config = ForecastConfig(
            args.neighbors, args.min_train, args.train_window, tuple(args.exclude_date)
        )
        service = ForecastService(
            DailyFeatureExtractor(LegacyStageProvider()),
            AnalogueModel(),
            SqliteForecastJournal(args.store),
        )
        if args.command == "predict":
            run = service.predict(
                snapshot,
                args.as_of,
                config,
                observed_at=observed_at,
                quote_at=quote_at,
            )
        else:
            run = service.backtest(snapshot, args.before, config, args.start)
        paths = write_run(run, args.output)
        print(
            json.dumps(
                dict(
                    experiment=run.experiment_id,
                    files=paths,
                    note="原始价格研究诊断；非独立封存验证，不启用校准概率",
                ),
                ensure_ascii=False,
                indent=2,
            )
        )
        return 0
    except (ValueError, OSError, RuntimeError, KeyError, TypeError) as exc:
        parser.exit(2, "预测研究：" + str(exc) + "\n")


if __name__ == "__main__":
    raise SystemExit(main())
