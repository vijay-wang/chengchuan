"""Real event workflow smoke; never opens a foreground desktop window."""

import json
import os
import time
from datetime import datetime, timedelta
from pathlib import Path

os.environ["QT_QPA_PLATFORM"] = "offscreen"

from PySide6.QtCore import QDate  # noqa: E402
from PySide6.QtTest import QTest  # noqa: E402
from PySide6.QtWidgets import QApplication  # noqa: E402

from chengchuan.application.services import AnalysisService  # noqa: E402
from chengchuan.application.two_up import TwoUpService, prepare_history  # noqa: E402
from chengchuan.domain.models import SHANGHAI  # noqa: E402
from chengchuan.infrastructure.forecast_input import ForecastSnapshotRepository  # noqa: E402
from chengchuan.infrastructure.forecast_journal import SqliteForecastJournal  # noqa: E402
from chengchuan.infrastructure.legacy import LegacyAnalysisEngine  # noqa: E402
from chengchuan.infrastructure.two_up_inputs import CachedTwoUpInputs  # noqa: E402
from chengchuan.infrastructure.two_up_reports import write_run  # noqa: E402
from chengchuan.infrastructure.workspace import SqliteWorkspaceRepository  # noqa: E402
from chengchuan.ui.window import MainWindow  # noqa: E402


def main():
    folder = Path(".chengchuan/two-up-validation/desktop")
    folder.mkdir(parents=True, exist_ok=True)
    app = QApplication.instance() or QApplication([])
    assert app.platformName() == "offscreen"
    inputs = CachedTwoUpInputs()
    h = inputs.load("sz002318")
    service = TwoUpService(
        SqliteForecastJournal(folder / "forecast.sqlite"), inputs.history, inputs.pool
    )
    latest = service.latest_date(h.snapshot, datetime.now(SHANGHAI))
    states, _, _ = prepare_history(h, latest)
    day = max(d for d, s in states.items() if s.status == "confirmed")
    window = MainWindow(
        ForecastSnapshotRepository(),
        AnalysisService(LegacyAnalysisEngine()),
        SqliteWorkspaceRepository(folder / "workspace.sqlite"),
        forecast_service=service,
        forecast_writer=write_run,
    )
    panel = window.forecast_panel

    def wait(predicate):
        deadline = time.monotonic() + 120
        while not predicate():
            app.processEvents()
            QTest.qWait(5)
            if time.monotonic() > deadline:
                raise RuntimeError(panel.status.text())

    try:
        window.resize(1440, 900)
        window.show()
        window._show_snapshot(h.snapshot)
        window.research_tabs.setCurrentWidget(window.forecast_tab)
        panel.asof.setDate(QDate(day.year, day.month, day.day))
        started = time.monotonic()
        panel.predict()
        wait(lambda: panel.run is not None)
        prediction = panel.run
        seconds = time.monotonic() - started
        assert len(prediction.predictions[0].event_returns) == 3
        assert not panel.form_scroll.widget().layout().isRowVisible(panel.mode)
        window.resize(1100, 740)
        QTest.qWait(40)
        window.grab().save(str(folder / "two-up-1100.png"))
        panel.advanced.setChecked(True)
        QTest.qWait(30)
        window.grab().save(str(folder / "two-up-parameters-1100.png"))
        panel.advanced.setChecked(False)
        first = latest - timedelta(days=30)
        before = latest + timedelta(days=1)
        panel.start.setDate(QDate(first.year, first.month, first.day))
        panel.before.setDate(QDate(before.year, before.month, before.day))
        panel.backtest()
        wait(lambda: panel.run is not None)
        summary = panel.run.summaries[0]
        assert summary["confirmed_events"] >= 1
        panel.export_requested(str(folder / "event-backtest"))
        wait(lambda: "已导出" in panel.status.text())
        panel.predict()
        panel.set_context(None)
        assert panel.run is None and panel.runner.active is None
        result = dict(
            platform=app.platformName(),
            as_of=str(day),
            prediction_seconds=round(seconds, 3),
            status=prediction.predictions[0].status,
            summary=summary,
            cancel_and_export=True,
        )
        (folder / "result.json").write_text(
            json.dumps(result, ensure_ascii=False, indent=2), encoding="utf-8"
        )
        print(json.dumps(result, ensure_ascii=False))
    finally:
        window.close()


if __name__ == "__main__":
    main()
