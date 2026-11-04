"""Use cached real data to verify date navigation and translucent chart overlays, offline."""

import argparse
import json
import time
from datetime import date
from pathlib import Path

from PySide6.QtCore import QPointF, Qt
from PySide6.QtGui import QFont
from PySide6.QtTest import QTest
from PySide6.QtWidgets import QApplication

from chengchuan.application.market import MarketService
from chengchuan.application.services import AnalysisService
from chengchuan.infrastructure.forecast_input import ForecastSnapshotRepository
from chengchuan.infrastructure.legacy import LegacyAnalysisEngine
from chengchuan.infrastructure.market_cache import SqliteMarketCache
from chengchuan.infrastructure.meoz_client import MeozClient
from chengchuan.infrastructure.meoz_market import MeozMarketProvider
from chengchuan.infrastructure.workspace import SqliteWorkspaceRepository
from chengchuan.ui.window import MainWindow


def check(symbol, target, folder):
    app = QApplication.instance() or QApplication([])
    app.setFont(QFont("Noto Sans CJK SC", 10))
    client = MeozClient(offline=True)
    before = client.store.usage()
    window = MainWindow(
        ForecastSnapshotRepository(),
        AnalysisService(LegacyAnalysisEngine()),
        SqliteWorkspaceRepository(folder / "workspace.sqlite"),
        market=MarketService(
            MeozMarketProvider(client), SqliteMarketCache(folder / "market.sqlite"), ttl_seconds=180
        ),
    )
    window.resize(1600, 1000)
    window.show()
    window.auto_refresh.setChecked(False)

    def wait(day, interval):
        deadline = time.monotonic() + 40
        while not (
            window.frame
            and window.frame.as_of == day
            and window.snapshot.series.interval == interval
            and window.report
            and window.runner.active is None
            and window.market_runner.active is None
        ):
            app.processEvents()
            QTest.qWait(5)
            if time.monotonic() > deadline:
                raise RuntimeError("历史图表验收超时：" + window.market_status.text())

    def hover(plot, filename):
        QTest.mouseMove(window.date_nav.button)
        QTest.qWait(15)
        rect = plot.getViewBox().sceneBoundingRect()
        point = plot.mapFromScene(
            QPointF(rect.left() + rect.width() * 0.62, rect.top() + rect.height() * 0.38)
        )
        QTest.mouseMove(plot.viewport(), point)
        QTest.qWait(80)
        app.processEvents()
        tip = window.chart.cursor.tips[plot]
        if not tip.isVisible() or not plot.viewport().rect().contains(tip.geometry()):
            raise RuntimeError("浮窗不可见或超出边界")
        window.grab().save(str(folder / filename))
        return tip.text()

    observations = []
    try:
        window.open_stock(symbol, "minute")
        wait(None, "minute")
        for interval in ("minute", "day", "week"):
            window.open_stock(symbol, interval, as_of=target)
            wait(target, interval)
            if any(b.timestamp.date() > target for b in window.snapshot.series.bars):
                raise RuntimeError("历史图表混入未来数据")
            if window.auto_refresh.isEnabled():
                raise RuntimeError("历史浏览未暂停自动更新")
            observations.append(
                dict(
                    period=interval,
                    as_of=str(window.frame.as_of),
                    last=window.snapshot.series.bars[-1].timestamp.isoformat(),
                    bars=len(window.snapshot.series.bars),
                    tooltip=hover(window.chart.price, interval + "-hover.png"),
                )
            )
            if interval == "minute":
                hover(window.chart.volume, "volume-hover.png")
                QTest.mouseClick(window.date_nav.button, Qt.MouseButton.LeftButton)
                app.processEvents()
                if not window.date_nav.popup.isVisible():
                    raise RuntimeError("日历未弹出")
                window.date_nav.popup.grab().save(str(folder / "calendar.png"))
                window.date_nav.popup.hide()
        window.return_to_latest()
        wait(None, "week")
        if not window.auto_refresh.isEnabled():
            raise RuntimeError("返回最新未恢复更新控件")
        after = client.store.usage()
        if before != after:
            raise RuntimeError("离线验收意外产生付费请求")
        result = dict(
            symbol=symbol,
            date=str(target),
            platform=app.platformName(),
            observations=observations,
            usage_before=before,
            usage_after=after,
        )
        (folder / "result.json").write_text(
            json.dumps(result, ensure_ascii=False, indent=2), encoding="utf-8"
        )
        return result
    finally:
        window.close()


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--symbol", default="002318")
    parser.add_argument("--date", type=date.fromisoformat, required=True)
    parser.add_argument("--output-dir", type=Path, required=True)
    args = parser.parse_args()
    args.output_dir.mkdir(parents=True, exist_ok=True)
    print(json.dumps(check(args.symbol, args.date, args.output_dir), ensure_ascii=False))


if __name__ == "__main__":
    main()
