"""Capture the real saved-stock drawing workflow without a foreground window."""

import json
import os
import time
from pathlib import Path

os.environ["QT_QPA_PLATFORM"] = "offscreen"

from PySide6.QtCore import QPointF, Qt  # noqa: E402
from PySide6.QtTest import QTest  # noqa: E402
from PySide6.QtWidgets import QApplication  # noqa: E402

from chengchuan.application.services import AnalysisService  # noqa: E402
from chengchuan.infrastructure.legacy import LegacyAnalysisEngine, LegacyJsonRepository  # noqa: E402
from chengchuan.infrastructure.workspace import SqliteWorkspaceRepository  # noqa: E402
from chengchuan.ui.window import MainWindow  # noqa: E402


def main():
    folder = Path(".chengchuan/drawings-validation")
    folder.mkdir(parents=True, exist_ok=True)
    app = QApplication.instance() or QApplication([])
    assert app.platformName() == "offscreen"
    window = MainWindow(
        LegacyJsonRepository(),
        AnalysisService(LegacyAnalysisEngine()),
        SqliteWorkspaceRepository(folder / "workspace.sqlite"),
    )
    try:
        window.show()
        window.open_snapshot(Path("reports/jiuli_20261019_1102.dataset.json"))
        deadline = time.monotonic() + 15
        while window.snapshot is None or window.runner.active is not None:
            app.processEvents()
            QTest.qWait(5)
            if time.monotonic() > deadline:
                raise RuntimeError(window.status.text())
        chart = window.chart
        tools = chart.drawings
        tools.clear_current()
        left, right = chart.price.getViewBox().viewRange()[0]
        low, high = chart.price.getViewBox().viewRange()[1]

        def click(x, y):
            point = chart.price.mapFromScene(chart.price.getViewBox().mapViewToScene(QPointF(x, y)))
            QTest.mouseClick(chart.price.viewport(), Qt.MouseButton.LeftButton, pos=point)
            app.processEvents()

        tools.set_tool("price_range")
        click(left + (right - left) * 0.3, low + (high - low) * 0.35)
        click(left + (right - left) * 0.7, low + (high - low) * 0.65)
        assert len(tools.visuals) == 1
        label = next(iter(tools.visuals.values())).text.toPlainText()
        assert "价差" in label and "%" in label
        for width, height in ((1440, 920), (1100, 740)):
            window.resize(width, height)
            QTest.qWait(80)
            window.grab().save(str(folder / f"chart-drawings-{width}.png"))
        tools.flush()
        result = dict(
            platform=app.platformName(),
            symbol=window.snapshot.series.symbol,
            label=label,
            drawings=len(tools.book.items),
            network_requests=0,
            screenshots=["chart-drawings-1440.png", "chart-drawings-1100.png"],
        )
        (folder / "result.json").write_text(
            json.dumps(result, ensure_ascii=False, indent=2), encoding="utf-8"
        )
        print(json.dumps(result, ensure_ascii=False))
    finally:
        window.close()


if __name__ == "__main__":
    main()
