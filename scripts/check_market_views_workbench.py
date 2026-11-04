"""Native feature check with real cached data; paid network access is explicit."""

import argparse
import json
import time
from pathlib import Path

import numpy as np
from PySide6.QtCore import QPoint, QRect
from PySide6.QtGui import QImage
from PySide6.QtTest import QTest
from PySide6.QtWidgets import QApplication

from chengchuan.application.services import AnalysisService
from chengchuan.infrastructure.legacy import LegacyAnalysisEngine, LegacyJsonRepository
from chengchuan.infrastructure.market_sources import ConfiguredMarketSources
from chengchuan.infrastructure.meoz_client import MeozStore
from chengchuan.infrastructure.workspace import SqliteWorkspaceRepository
from chengchuan.ui.window import MainWindow


def refresh_pixels(window, folder):
    """Repaint an identical real cached chart, without any data-source requests."""
    if QApplication.platformName() != "xcb":
        return None
    window.raise_()
    window.activateWindow()
    if not QTest.qWaitForWindowActive(window, 1500):
        raise RuntimeError("原生刷新检查需要活动窗口")
    chart = window.chart
    rect = QRect(chart.mapTo(window, QPoint(0, 0)), chart.size())

    def capture():
        shot = window.screen().grabWindow(window.winId()).copy(rect)
        pixels = shot.toImage().convertToFormat(QImage.Format.Format_RGBA8888)
        return shot, np.frombuffer(pixels.constBits(), dtype=np.uint8).copy()

    QTest.qWait(80)
    before, baseline = capture()
    before.save(str(folder / "refresh-before.png"))
    ids = tuple(id(i) for plot in (chart.price, chart.volume) for i in plot.getPlotItem().items)
    difference = 0
    for _ in range(30):
        chart.set_snapshot(
            window.snapshot,
            preserve_view=True,
            vwap_points=window.frame.vwap_points,
            baseline_price=window.frame.baseline_price,
            reference_closes=window._chart_references(),
        )
        QTest.qWait(5)
        QApplication.processEvents()
        _, current = capture()
        difference = max(difference, int(np.count_nonzero(baseline != current)))
        if (
            tuple(id(i) for plot in (chart.price, chart.volume) for i in plot.getPlotItem().items)
            != ids
        ):
            raise RuntimeError("同证券刷新不应替换或累加图元")
    after, _ = capture()
    after.save(str(folder / "refresh-after.png"))
    if difference:
        raise RuntimeError(f"相同图表刷新存在像素抖动：{difference}")
    return dict(updates=30, max_changed_channels=difference, reused_items=len(ids))


def check(folder, allow_network=False):
    app = QApplication.instance() or QApplication([])
    sources = ConfiguredMarketSources(folder, offline=not allow_network)
    before = MeozStore().usage()
    window = MainWindow(
        LegacyJsonRepository(),
        AnalysisService(LegacyAnalysisEngine()),
        SqliteWorkspaceRepository(folder / "workspace.sqlite"),
        market=sources.connect("meoz"),
        market_sources=sources,
    )
    window.resize(1780, 1100)
    window.show()
    window.auto_refresh.setChecked(False)

    def wait(predicate):
        end = time.monotonic() + 40
        while not predicate():
            app.processEvents()
            QTest.qWait(5)
            if time.monotonic() > end:
                raise RuntimeError("验收超时：" + window.market_status.text())

    def idle():
        return all(
            r.active is None
            for r in (
                window.runner,
                window.market_runner,
                window.overview_runner,
                window.details_runner,
                window.funds_runner,
            )
        )

    observations = []
    try:
        window.open_stock("002318")
        wait(lambda: window.frame is not None and idle())
        repaint = refresh_pixels(window, folder)
        window.research_tabs.setCurrentWidget(window.funds_panel)
        wait(lambda: window.funds_panel.snapshot is not None and idle())
        for scope in (0, 1):
            window.funds_panel.scope.setCurrentIndex(scope)
            wait(lambda: window.funds_panel.snapshot is not None and idle())
            flow = window.funds_panel.snapshot
            if not window.grab().save(
                str(folder / ("funds-stock.png" if scope == 0 else "funds-market.png"))
            ):
                raise RuntimeError("资金截图保存失败")
            observations.append(
                dict(
                    kind="funds",
                    symbol=flow.symbol,
                    day=flow.day.isoformat(),
                    source=flow.source,
                    buckets=[
                        dict(key=b.key, inflow=b.inflow, outflow=b.outflow, net=b.net)
                        for b in flow.buckets
                    ],
                )
            )
        window.open_heatmap()
        dialog = window.heatmap_dialog
        for kind in ("hy", "gn", "stocks"):
            dialog.kind.setCurrentIndex(dialog.kind.findData(kind))
            wait(
                lambda: (
                    dialog.snapshot is not None
                    and dialog.snapshot.kind == kind
                    and dialog.runner.active is None
                )
            )
            if not dialog.canvas.rects:
                raise RuntimeError("热力图没有可绘制的真实成交额")
            if not dialog.grab().save(str(folder / ("heatmap-" + kind + ".png"))):
                raise RuntimeError("热力图截图保存失败")
            observations.append(
                dict(
                    kind=kind,
                    day=dialog.snapshot.day.isoformat(),
                    records=len(dialog.snapshot.tiles),
                    drawn=len(dialog.canvas.rects),
                )
            )
        dialog.close()
        after = MeozStore().usage()
        if not allow_network and before != after:
            raise RuntimeError("离线验收不应产生付费调用")
        result = dict(
            platform=app.platformName(),
            offline=not allow_network,
            before=before,
            after=after,
            observations=observations,
            repaint=repaint,
        )
        (folder / "result.json").write_text(
            json.dumps(result, ensure_ascii=False, indent=2), encoding="utf-8"
        )
        return result
    finally:
        window.close()
        app.processEvents()


if __name__ == "__main__":
    parser = argparse.ArgumentParser()
    parser.add_argument("--output-dir", type=Path, required=True)
    parser.add_argument("--allow-network", action="store_true")
    args = parser.parse_args()
    args.output_dir.mkdir(parents=True, exist_ok=True)
    result = check(args.output_dir, args.allow_network)
    print(
        json.dumps(
            dict(
                platform=result["platform"],
                offline=result["offline"],
                checks=len(result["observations"]),
                added_api_calls=result["after"]["total"] - result["before"]["total"],
            ),
            ensure_ascii=False,
        )
    )
