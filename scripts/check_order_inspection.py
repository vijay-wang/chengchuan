"""Offline native acceptance of order filters, matched cells and minute annotations."""

import argparse
import json
import time
from datetime import date
from pathlib import Path

from PySide6.QtCore import QEvent, QPointF, Qt
from PySide6.QtGui import QMouseEvent
from PySide6.QtTest import QTest
from PySide6.QtWidgets import QApplication

from chengchuan.application.microstructure import select_bands
from chengchuan.application.services import AnalysisService
from chengchuan.domain.microstructure import OrderFilter
from chengchuan.infrastructure.legacy import LegacyAnalysisEngine, LegacyJsonRepository
from chengchuan.infrastructure.market_sources import ConfiguredMarketSources
from chengchuan.infrastructure.meoz_client import MeozStore
from chengchuan.infrastructure.workspace import SqliteWorkspaceRepository
from chengchuan.ui.window import MainWindow


def check(folder, symbol, day):
    folder.mkdir(parents=True, exist_ok=True)
    app = QApplication.instance() or QApplication([])
    before = MeozStore().usage()
    sources = ConfiguredMarketSources(folder, offline=True)
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

    def wait(predicate):
        deadline = time.monotonic() + 30
        while not predicate():
            app.processEvents()
            QTest.qWait(5)
            if time.monotonic() > deadline:
                raise RuntimeError("离线验收超时：" + window.market_status.text())

    def capture(widget, name):
        app.processEvents()
        QTest.qWait(40)
        if not widget.grab().save(str(folder / name)):
            raise RuntimeError("截图保存失败")

    try:
        window.open_stock(symbol, "day", as_of=day)
        wait(lambda: window.frame is not None and window.market_runner.active is None)
        start = time.perf_counter()
        window._request_l2_tape()
        wait(lambda: window._l2_analysis is not None and window.l2_analysis_runner.active is None)
        elapsed = time.perf_counter() - start
        analysis = window._l2_analysis
        window.period.setCurrentIndex(window.period.findData("minute"))
        wait(lambda: window.frame is not None and window.market_runner.active is None)
        if window._l2_analysis is not analysis:
            raise RuntimeError("同股同日切换周期不得丢失已加载逐笔")
        window.research_tabs.setCurrentWidget(window.micro_panel)
        window.micro_panel.tabs.setCurrentWidget(window.micro_panel.restored_page)
        capture(window, "right-inline-reconstruction.png")
        window._expand_micro()
        panel = window.micro_dialog.panel
        panel.tabs.setCurrentWidget(panel.restored_page)
        capture(window.micro_dialog, "inline-reconstruction.png")
        panel.restored_views.setCurrentIndex(1)
        capture(window.micro_dialog, "restored-dark-table.png")
        panel.restored_filters.unfilled.setChecked(False)
        panel.restored_filters.cancelled.setChecked(False)
        capture(window.micro_dialog, "restored-filtered.png")
        selected = next(
            i for i, o in enumerate(panel._visible_restored) if o.side == "B" and o.trade_count == 3
        )
        panel._show_fills(panel.restored_model.index(selected, 0))
        capture(window.micro_dialog, "matched-order-cells.png")
        window.micro_dialog.hide()
        window.chart.clear_selection()
        window._clear_micro()
        controller = window.chart_orders
        controller.show_minute.setChecked(True)
        controller.show_marks.setChecked(True)
        wait(lambda: window._l2_analysis is not None and window.l2_analysis_runner.active is None)
        tape, analysis = window._l2_tape, window._l2_analysis
        app.processEvents()
        controller._render_markers()
        if not controller.marker_spots:
            raise RuntimeError("真实样本未产生已勾选档位的分钟标记")
        xbounds = window.chart.price.getViewBox().viewRange()[0]
        visible = [
            s["data"][0]
            for s in controller.marker_spots
            if xbounds[0] + 0.5 < s["pos"][0] < xbounds[1] - 0.5
        ]
        index = max(set(visible), key=visible.count)
        bounds = window.chart.price.getViewBox().viewRange()[1]
        point = window.chart.price.getViewBox().mapViewToScene(QPointF(index, sum(bounds) / 2))
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
        if not controller.overlay.isVisible():
            raise RuntimeError(
                "分钟浮层未显示："
                + repr(
                    dict(
                        index=index,
                        point=(point.x(), point.y()),
                        view=window.chart.price.getViewBox().viewRange(),
                        contains=window.chart.price.getViewBox()
                        .sceneBoundingRect()
                        .contains(point),
                        active=window.chart.cursor.active_plot is window.chart.price,
                        selected=controller._hover_index,
                        ready=controller._ready(),
                        parent_visible=window.chart.price.viewport().isVisible(),
                        minute=str(window.chart.snapshot.series.bars[index].timestamp),
                        cutoff=str(analysis.cutoff),
                    )
                )
            )
        capture(window, "minute-bands-and-markers.png")
        start = time.perf_counter()
        for _ in range(1000):
            controller._hover(index)
        hover_ms = (time.perf_counter() - start) * 1000 / 1000
        totals = {}
        for unfilled in (False, True):
            for cancelled in (False, True):
                filters = OrderFilter(unfilled, cancelled)
                bands = select_bands(analysis.band_groups, filters)
                totals[f"unfilled={unfilled},cancelled={cancelled}"] = [
                    dict(
                        amount_fen=sum(b.amount_fen for b in side),
                        orders=sum(b.orders for b in side),
                    )
                    for side in bands
                ]
                window._band_filters_changed(filters)
                controller._render_markers()
        after = MeozStore().usage()
        if before != after:
            raise RuntimeError("本轮全部应为离线，本机API计数不应增加")
        result = dict(
            platform=app.platformName(),
            symbol=tape.symbol,
            day=str(day),
            orders=len(tape.orders),
            load_and_analysis_seconds=elapsed,
            minutes=len(analysis.minute_groups),
            hover_cached_ms=hover_ms,
            totals=totals,
            before=before,
            after=after,
        )
        (folder / "result.json").write_text(
            json.dumps(result, ensure_ascii=False, indent=2), encoding="utf-8"
        )
        print(json.dumps(result, ensure_ascii=False))
    finally:
        window.close()
        app.processEvents()


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument("--output-dir", type=Path, default=Path(".chengchuan/order-inspection"))
    parser.add_argument("--symbol", default="002318")
    parser.add_argument("--date", type=date.fromisoformat, default=date(2026, 11, 4))
    args = parser.parse_args()
    check(args.output_dir, args.symbol, args.date)


if __name__ == "__main__":
    main()
