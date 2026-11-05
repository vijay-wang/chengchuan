"""Always headless: real multi-day overlays, reuse and chart-edge acceptance."""

import argparse
import json
import os
import time
from collections import Counter
from datetime import date
from pathlib import Path

os.environ["QT_QPA_PLATFORM"] = "offscreen"

from PySide6.QtCore import QEvent, QPointF, Qt
from PySide6.QtGui import QMouseEvent
from PySide6.QtTest import QTest
from PySide6.QtWidgets import QApplication

from chengchuan.application.services import AnalysisService
from chengchuan.infrastructure.legacy import LegacyAnalysisEngine, LegacyJsonRepository
from chengchuan.infrastructure.market_sources import ConfiguredMarketSources
from chengchuan.infrastructure.meoz_client import MeozStore
from chengchuan.infrastructure.workspace import SqliteWorkspaceRepository
from chengchuan.ui.window import MainWindow


def check(folder, day, allow_network=False):
    folder.mkdir(parents=True, exist_ok=True)
    app = QApplication.instance() or QApplication([])
    assert app.platformName() == "offscreen"
    before = MeozStore().usage()
    sources = ConfiguredMarketSources(folder, offline=not allow_network)
    window = MainWindow(
        LegacyJsonRepository(),
        AnalysisService(LegacyAnalysisEngine()),
        SqliteWorkspaceRepository(folder / "workspace.sqlite"),
        market=sources.connect("meoz"),
        market_sources=sources,
    )
    window.resize(1680, 1050)
    window.show()
    window.auto_refresh.setChecked(False)
    window._overview_requested = True

    def wait(predicate):
        deadline = time.monotonic() + 60
        while not predicate():
            app.processEvents()
            QTest.qWait(5)
            if time.monotonic() > deadline:
                raise RuntimeError(
                    "后台验收超时："
                    + window.chart_orders.hint.text()
                    + repr(window.chart_orders.day_status)
                )

    def ready():
        c = window.chart_orders
        return (
            window.frame is not None
            and window.market_runner.active is None
            and all(c._ready(d) for d in c.required_days())
        )

    try:
        window.open_stock("002318", "minute", as_of=day)
        wait(lambda: window.frame is not None and window.market_runner.active is None)
        c = window.chart_orders
        c.show_minute.setChecked(True)
        c.show_marks.setChecked(True)
        wait(ready)
        previous = c.day_summaries[day]
        window.open_stock("002318", "five_day", as_of=day)
        wait(lambda: window.frame is not None and len(c.required_days()) == 5)
        wait(ready)
        assert c.day_summaries[day] is previous
        window.chart.clear_selection()
        c._render_markers()
        bars = window.chart.snapshot.series.bars
        marked = Counter(bars[s["data"][0]].timestamp.date() for s in c.marker_spots)
        assert set(marked) == set(c.required_days()), marked
        for target in c.required_days():
            index = next(
                s["data"][0]
                for s in c.marker_spots
                if bars[s["data"][0]].timestamp.date() == target
            )
            low, high = window.chart.price.getViewBox().viewRange()[1]
            point = window.chart.price.getViewBox().mapViewToScene(QPointF(index, (low + high) / 2))
            local = window.chart.price.mapFromScene(point)
            app.sendEvent(
                window.chart.price.viewport(),
                QMouseEvent(
                    QEvent.Type.MouseMove,
                    QPointF(local),
                    QPointF(window.chart.price.viewport().mapToGlobal(local)),
                    Qt.MouseButton.NoButton,
                    Qt.MouseButton.NoButton,
                    Qt.KeyboardModifier.NoModifier,
                ),
            )
            app.processEvents()
            assert c.overlay.isVisible() and target.strftime("%m-%d") in c.overlay.text()
            assert "买方金额" in c.overlay.text()
            window.grab().save(str(folder / f"five-day-{target}.png"))
        window.chart.price.setXRange(-100000, 100000, padding=0)
        app.processEvents()
        bounds = [
            plot.getViewBox().viewRange()[0] for plot in (window.chart.price, window.chart.volume)
        ]
        for left, right in bounds:
            assert left >= -0.50001 and right <= len(bars) - 0.49999, bounds
        window.grab().save(str(folder / "five-day-multiday.png"))
        retained = dict(c.day_summaries)
        window.open_stock("002318", "minute", as_of=day)
        wait(ready)
        window.open_stock("002318", "five_day", as_of=day)
        wait(lambda: ready() and len(c.required_days()) == 5)
        assert all(c.day_summaries[d] is retained[d] for d in c.required_days())
        after = MeozStore().usage()
        if not allow_network:
            assert before == after
        report = dict(
            platform=app.platformName(),
            days=list(map(str, c.required_days())),
            bars=len(bars),
            color_blocks_by_day={str(d): n for d, n in sorted(marked.items())},
            marked_minutes_by_day={
                str(d): len(
                    {
                        s["data"][0]
                        for s in c.marker_spots
                        if bars[s["data"][0]].timestamp.date() == d
                    }
                )
                for d in c.required_days()
            },
            view_bounds=bounds,
            before=before,
            after=after,
        )
        (folder / "result.json").write_text(
            json.dumps(report, ensure_ascii=False, indent=2), encoding="utf-8"
        )
        print(json.dumps(report, ensure_ascii=False))
    finally:
        window.close()
        app.processEvents()


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument("--output-dir", type=Path, default=Path(".chengchuan/multiday-headless"))
    parser.add_argument("--date", type=date.fromisoformat, default=date(2026, 11, 4))
    parser.add_argument("--allow-network", action="store_true")
    args = parser.parse_args()
    check(args.output_dir, args.date, args.allow_network)


if __name__ == "__main__":
    main()
