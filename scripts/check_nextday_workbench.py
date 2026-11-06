"""Offline desktop smoke for next-day research; always forces offscreen Qt."""

import argparse
import json
import os
import time
from datetime import timedelta
from pathlib import Path

os.environ["QT_QPA_PLATFORM"] = "offscreen"

from PySide6.QtCore import QDate  # noqa: E402
from PySide6.QtGui import QFont  # noqa: E402
from PySide6.QtTest import QTest  # noqa: E402
from PySide6.QtWidgets import QApplication  # noqa: E402

from chengchuan.application.nextday import NextDayService  # noqa: E402
from chengchuan.application.services import AnalysisService  # noqa: E402
from chengchuan.infrastructure.forecast_input import (  # noqa: E402
    ForecastSnapshotRepository,
    load_input,
)
from chengchuan.infrastructure.forecast_journal import SqliteForecastJournal  # noqa: E402
from chengchuan.infrastructure.legacy import LegacyAnalysisEngine  # noqa: E402
from chengchuan.infrastructure.nextday_reports import write_run  # noqa: E402
from chengchuan.infrastructure.workspace import SqliteWorkspaceRepository  # noqa: E402
from chengchuan.ui.window import MainWindow  # noqa: E402


def check(dataset, output):
    output.mkdir(parents=True, exist_ok=True)
    app = QApplication.instance() or QApplication([])
    if app.platformName() != "offscreen":
        raise RuntimeError("验收仅可使用离屏Qt")
    app.setFont(QFont("Noto Sans CJK SC", 10))
    snapshot, observed, quote = load_input(dataset)
    window = MainWindow(
        ForecastSnapshotRepository(),
        AnalysisService(LegacyAnalysisEngine()),
        SqliteWorkspaceRepository(output / "workspace.sqlite"),
        forecast_service=NextDayService(journal=SqliteForecastJournal(output / "forecast.sqlite")),
        forecast_writer=write_run,
    )
    panel = window.forecast_panel

    def wait(predicate, seconds=180):
        deadline = time.monotonic() + seconds
        while not predicate():
            app.processEvents()
            QTest.qWait(5)
            if time.monotonic() > deadline:
                raise RuntimeError("验收超时：" + panel.status.text())

    try:
        window.resize(1440, 900)
        window.show()
        window._show_snapshot(snapshot)
        panel.set_context(snapshot, observed, quote)
        window.research_tabs.setCurrentWidget(window.forecast_tab)
        started = time.monotonic()
        panel.predict()
        wait(lambda: panel.run is not None)
        prediction = panel.run
        prediction_seconds = time.monotonic() - started
        if len(prediction.predictions) != 1 or prediction.predictions[0].mode != "close":
            raise RuntimeError("预测目标不是盘后下一交易日")
        if not prediction.predictions[0].model_id:
            raise RuntimeError("样本未产生模型输出：" + prediction.predictions[0].reason)
        if window.selection is not None:
            raise RuntimeError("预测不应创建默认图表选区")
        QTest.qWait(50)
        window.grab().save(str(output / "nextday-1440.png"))
        window.resize(1100, 740)
        QTest.qWait(50)
        window.grab().save(str(output / "nextday-1100.png"))
        panel.advanced.setChecked(True)
        QTest.qWait(50)
        window.grab().save(str(output / "nextday-advanced-1100.png"))
        if any(widget.height() < widget.minimumHeight() for widget in panel._advanced_fields):
            raise RuntimeError("小窗口参数控件被压缩裁切")
        panel.advanced.setChecked(False)
        last = snapshot.series.bars[-1].timestamp.date()
        start = last - timedelta(days=14)
        before = last + timedelta(days=1)
        panel.start.setDate(QDate(start.year, start.month, start.day))
        panel.before.setDate(QDate(before.year, before.month, before.day))
        panel.backtest()
        wait(lambda: panel.run is not None)
        backtest = panel.run
        if len(backtest.summaries) != 3 or not panel.table.rowCount():
            raise RuntimeError("三组验证或对照表缺失")
        panel.export_requested(str(output / "desktop-backtest"))
        wait(lambda: "已导出" in panel.status.text())
        if panel.run is not backtest:
            raise RuntimeError("导出破坏了研究结果")
        panel.predict()
        panel.mode.setCurrentIndex(1)
        if panel.runner.active is not None or panel.run is not None:
            raise RuntimeError("切换时点未取消旧任务")
        panel.predict()
        wait(lambda: panel.run is not None)
        if panel.run.predictions[0].direction is not None:
            raise RuntimeError("没有分钟输入的14:35不应给方向")
        result = dict(
            symbol=snapshot.series.symbol,
            bars=len(snapshot.series.bars),
            platform=app.platformName(),
            prediction_seconds=round(prediction_seconds, 3),
            nextday_status=prediction.predictions[0].status,
            nextday_direction=prediction.predictions[0].direction,
            backtest_groups=len(backtest.summaries),
            minute_status=panel.run.predictions[0].status,
            export_files=[
                str(output / ("desktop-backtest." + ext)) for ext in ("json", "md", "csv")
            ],
            cancellation_and_reselection=True,
        )
        (output / "desktop-result.json").write_text(
            json.dumps(result, ensure_ascii=False, indent=2), encoding="utf-8"
        )
        return result
    finally:
        window.close()


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--dataset", type=Path, required=True)
    parser.add_argument("--output", type=Path, default=Path(".chengchuan/nextday-validation"))
    args = parser.parse_args()
    print(json.dumps(check(args.dataset, args.output), ensure_ascii=False))


if __name__ == "__main__":
    main()
