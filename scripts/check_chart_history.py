"""Headless real chart paging and restart check; no foreground windows or external links."""

import argparse
import json
import os
import time
from datetime import date
from pathlib import Path

os.environ["QT_QPA_PLATFORM"] = "offscreen"

from PySide6.QtCore import QPoint, QPointF, Qt  # noqa: E402
from PySide6.QtGui import QWheelEvent  # noqa: E402
from PySide6.QtTest import QTest  # noqa: E402
from PySide6.QtWidgets import QApplication  # noqa: E402

from chengchuan.application.services import AnalysisService  # noqa: E402
from chengchuan.infrastructure.legacy import LegacyAnalysisEngine, LegacyJsonRepository  # noqa: E402
from chengchuan.infrastructure.market_sources import ConfiguredMarketSources  # noqa: E402
from chengchuan.infrastructure.meoz_client import MeozStore  # noqa: E402
from chengchuan.infrastructure.workspace import SqliteWorkspaceRepository  # noqa: E402
from chengchuan.ui.window import MainWindow  # noqa: E402


def check(folder, day, allow_network=False):
    folder.mkdir(parents=True, exist_ok=True)
    app = QApplication.instance() or QApplication([])
    assert app.platformName() == "offscreen"
    sources = ConfiguredMarketSources(folder, offline=not allow_network)
    workspace = SqliteWorkspaceRepository(folder / "workspace.sqlite")
    before = MeozStore().usage()

    def make_window():
        w = MainWindow(
            LegacyJsonRepository(),
            AnalysisService(LegacyAnalysisEngine()),
            workspace,
            market=sources.connect("meoz"),
            market_sources=sources,
        )
        w.auto_refresh.setChecked(False)
        w._overview_requested = True
        w.resize(1600, 1000)
        w.show()
        return w

    def wait(predicate):
        deadline = time.monotonic() + 60
        while not predicate():
            app.processEvents()
            QTest.qWait(5)
            if time.monotonic() > deadline:
                raise RuntimeError("离屏验收超时")

    window = make_window()
    results = []
    try:
        for interval in ("minute", "five_day", "min5"):
            window.open_stock("002318", interval, as_of=day)
            wait(lambda: window.frame is not None and window.market_runner.active is None)
            assert window.selection is None and not window.chart.region.isVisible()
            old = window.snapshot.series.bars
            plot = window.chart.price if interval != "five_day" else window.chart.volume
            point = plot.mapFromScene(plot.getViewBox().sceneBoundingRect().center())
            event = QWheelEvent(
                QPointF(point),
                QPointF(plot.viewport().mapToGlobal(point)),
                QPoint(),
                QPoint(0, -120),
                Qt.MouseButton.NoButton,
                Qt.KeyboardModifier.NoModifier,
                Qt.ScrollPhase.NoScrollPhase,
                False,
            )
            app.sendEvent(plot.viewport(), event)
            app.processEvents()
            controller = window.chart_history
            wait(
                lambda: (
                    controller.active is None
                    and controller.runner.active is None
                    and not controller.timer.isActive()
                )
            )
            bars = window.snapshot.series.bars
            assert len(bars) > len(old), window.status.text()
            assert bars[0].timestamp < old[0].timestamp
            assert len({b.timestamp for b in bars}) == len(bars)
            ranges = [
                p.getViewBox().viewRange()[0] for p in (window.chart.price, window.chart.volume)
            ]
            assert all(a >= -0.50001 and b <= len(bars) - 0.49999 for a, b in ranges), ranges
            assert window.selection is None
            window.grab().save(str(folder / f"expanded-{interval}.png"))
            results.append(
                dict(
                    interval=interval,
                    before=len(old),
                    after=len(bars),
                    first=str(bars[0].timestamp),
                    last=str(bars[-1].timestamp),
                    ranges=ranges,
                )
            )
        window.close()
        window = make_window()
        window.restore_last_stock()
        wait(lambda: window.frame is not None and window.market_runner.active is None)
        assert window.online_symbol == "sz002318" and window.online_interval == "min5"
        assert window.history_date is None and window.selection is None
        results.append(
            dict(
                restart_symbol=window.online_symbol,
                restart_interval=window.online_interval,
                latest_mode=window.history_date is None,
                selection=window.selection,
            )
        )
    finally:
        window.close()
        app.processEvents()
    after = MeozStore().usage()
    if not allow_network:
        assert before == after
    report = dict(
        platform=app.platformName(), checks=results, requests_added=after["total"] - before["total"]
    )
    (folder / "result.json").write_text(json.dumps(report, ensure_ascii=False, indent=2))
    return report


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--as-of", required=True, type=date.fromisoformat)
    parser.add_argument("--allow-network", action="store_true")
    parser.add_argument("--output", type=Path, default=Path(".chengchuan/chart-history-validation"))
    args = parser.parse_args()
    print(json.dumps(check(args.output, args.as_of, args.allow_network), ensure_ascii=False))
