"""使用已保存真实日线验收预测／回测／导出和桌面截图，不依赖网络。"""

import argparse
import json
import tempfile
import time
from datetime import date
from pathlib import Path

from PySide6.QtGui import QFont
from PySide6.QtTest import QTest
from PySide6.QtWidgets import QApplication

from chengchuan.application.forecast import ForecastService
from chengchuan.application.forecast_features import DailyFeatureExtractor
from chengchuan.application.services import AnalysisService
from chengchuan.domain.analogue import AnalogueModel
from chengchuan.infrastructure.forecast_input import ForecastSnapshotRepository, load_input
from chengchuan.infrastructure.forecast_journal import SqliteForecastJournal
from chengchuan.infrastructure.forecast_reports import write_run
from chengchuan.infrastructure.forecast_stage import LegacyStageProvider
from chengchuan.infrastructure.legacy import LegacyAnalysisEngine
from chengchuan.infrastructure.workspace import SqliteWorkspaceRepository
from chengchuan.ui.window import MainWindow


def check(dataset, output_dir, before):
    app = QApplication.instance() or QApplication([])
    app.setFont(QFont("Noto Sans CJK SC", 10))
    window = MainWindow(
        ForecastSnapshotRepository(),
        AnalysisService(LegacyAnalysisEngine()),
        SqliteWorkspaceRepository(output_dir / "workspace.sqlite"),
        forecast_service=ForecastService(
            DailyFeatureExtractor(LegacyStageProvider()),
            AnalogueModel(),
            SqliteForecastJournal(output_dir / "forecast.sqlite"),
        ),
        forecast_writer=write_run,
    )
    window.resize(1600, 1000)
    window.show()
    panel = window.forecast_panel

    def wait(predicate):
        deadline = time.monotonic() + 40
        while not predicate():
            app.processEvents()
            QTest.qWait(5)
            if time.monotonic() > deadline:
                raise RuntimeError("验收超时：" + panel.status.text())

    try:
        snapshot, observed, quote = load_input(dataset)
        window._show_snapshot(snapshot)
        panel.set_context(snapshot, observed, quote)
        window.research_tabs.setCurrentWidget(window.forecast_tab)
        panel.predict_button.click()
        wait(lambda: panel.run is not None)
        prediction = panel.run
        if len(prediction.predictions) != 2:
            raise RuntimeError("H1/H2预测不完整")
        QTest.qWait(100)
        if not window.grab().save(str(output_dir / "prediction.png")):
            raise RuntimeError("无法保存预测界面")
        panel.before.setDate(before)
        started = time.monotonic()
        panel.validate_button.click()
        wait(lambda: panel.run is not None)
        run = panel.run
        elapsed = time.monotonic() - started
        if len(run.summaries) != 6 or panel.table.rowCount() == 0:
            raise RuntimeError("逐日回测结果不完整")
        panel.export_requested(str(output_dir / "backtest"))
        wait(lambda: "已导出" in panel.status.text())
        QTest.qWait(100)
        if not window.grab().save(str(output_dir / "backtest.png")):
            raise RuntimeError("无法保存回测界面")
        result = dict(
            symbol=snapshot.series.symbol,
            bars=len(snapshot.series.bars),
            before=before.isoformat(),
            platform=app.platformName(),
            backtest_seconds=round(elapsed, 3),
            displayed_rows=panel.table.rowCount(),
            predictions=[
                dict(as_of=p.as_of, target=p.target, status=p.status)
                for p in prediction.predictions
            ],
            summaries=run.summaries,
            export_preserved_run=panel.run is run,
        )
        (output_dir / "result.json").write_text(
            json.dumps(result, ensure_ascii=False, indent=2), encoding="utf-8"
        )
        return result
    finally:
        window.close()


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--dataset", type=Path, required=True)
    parser.add_argument("--output-dir", type=Path)
    parser.add_argument("--before", type=date.fromisoformat, default=date.today())
    args = parser.parse_args()
    folder = args.output_dir or Path(tempfile.mkdtemp(prefix="chengchuan-forecast-check-"))
    folder.mkdir(parents=True, exist_ok=True)
    result = check(args.dataset, folder, args.before)
    print(json.dumps({k: v for k, v in result.items() if k != "summaries"}, ensure_ascii=False))


if __name__ == "__main__":
    main()
