"""Verify real index charts, stock details and cached overview. Network is opt-in."""

import argparse
import json
import time
from datetime import date
from pathlib import Path

from PySide6.QtCore import Qt
from PySide6.QtGui import QFont
from PySide6.QtTest import QTest
from PySide6.QtWidgets import QApplication

from chengchuan.application.services import AnalysisService
from chengchuan.domain.market import INDEX_CATALOG
from chengchuan.domain.periods import PERIODS
from chengchuan.infrastructure.forecast_input import ForecastSnapshotRepository
from chengchuan.infrastructure.legacy import LegacyAnalysisEngine
from chengchuan.infrastructure.market_sources import ConfiguredMarketSources
from chengchuan.infrastructure.meoz_client import MeozStore
from chengchuan.infrastructure.workspace import SqliteWorkspaceRepository
from chengchuan.ui.window import MainWindow


def check(folder, allow_network=False):
    app = QApplication.instance() or QApplication([])
    app.setFont(QFont("Noto Sans CJK SC", 10))
    source = ConfiguredMarketSources(folder, offline=not allow_network)
    before = MeozStore().usage()
    window = MainWindow(
        ForecastSnapshotRepository(),
        AnalysisService(LegacyAnalysisEngine()),
        SqliteWorkspaceRepository(folder / "workspace.sqlite"),
        market=source.connect("meoz"),
        market_sources=source,
    )
    window.resize(1680, 1080)
    window.show()
    window.auto_refresh.setChecked(False)
    observations = []

    def wait(symbol, period, as_of=None):
        deadline = time.monotonic() + 45
        while not (
            window.frame
            and window.snapshot.series.symbol == symbol
            and window.snapshot.series.interval == period
            and window.history_date == as_of
            and window.report
            and all(
                r.active is None
                for r in (
                    window.runner,
                    window.market_runner,
                    window.overview_runner,
                    window.details_runner,
                )
            )
        ):
            app.processEvents()
            QTest.qWait(5)
            if time.monotonic() > deadline:
                raise RuntimeError("指数GUI验收超时：" + window.market_status.text())
        if "volume_ratio" not in window.quote_panel.values:
            raise RuntimeError("详细指标未加载：" + window.quote_panel.notice.text())

    def record(symbol, period, filename=None):
        frame = window.frame
        observations.append(
            dict(
                symbol=symbol,
                period=period,
                records=len(frame.snapshot.series.bars),
                first=frame.snapshot.series.bars[0].timestamp.isoformat(),
                last=frame.snapshot.series.bars[-1].timestamp.isoformat(),
                fields={key: value.text() for key, value in window.quote_panel.values.items()},
                scope=window.quote_panel.breadth_title.text(),
                breadth=window.quote_panel.breadth.text(),
                scope_note=window.quote_panel.scope_note.text(),
                notice=window.quote_panel.notice.text(),
            )
        )
        if filename:
            if not window.grab().save(str(folder / filename)):
                raise RuntimeError("图表截图失败")

    try:
        window.open_stock("002318", "day")
        wait("sz002318", "day")
        record("sz002318", "day", "stock-details.png")
        for position, (symbol, _, _) in enumerate(INDEX_CATALOG):
            QTest.mouseClick(window.index_bar.buttons[symbol], Qt.MouseButton.LeftButton)
            wait(symbol, "day")
            record(symbol, "day", "index-" + symbol[-6:] + "-day.png")
            codes = [p.code for p in PERIODS] if position == 0 else ["day", "minute", "min15"]
            for code in codes:
                window.period.setCurrentIndex(window.period.findData(code))
                wait(symbol, code)
                if any(b.timestamp.date() > date.today() for b in window.snapshot.series.bars):
                    raise RuntimeError("指数图表混入未来记录")
                record(
                    symbol,
                    code,
                    ("index-" + symbol[-6:] + "-" + code + ".png")
                    if code in ("minute", "min15")
                    else None,
                )
            window.period.setCurrentIndex(window.period.findData("day"))
            wait(symbol, "day")
        window.open_stock("index:sh000001", "minute", as_of=date(2026, 11, 3))
        wait("index:sh000001", "minute", date(2026, 11, 3))
        record("index:sh000001", "minute@2026-11-03", "index-historical.png")
        if "2026-11-03" not in window.quote_panel.scope_note.text():
            raise RuntimeError("历史统计日期错误")
        after = MeozStore().usage()
        if not allow_network and before != after:
            raise RuntimeError("离线验收产生了API调用")
        result = dict(
            platform=app.platformName(),
            offline=not allow_network,
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
    parser.add_argument("--output-dir", type=Path, required=True)
    parser.add_argument("--allow-network", action="store_true")
    args = parser.parse_args()
    args.output_dir.mkdir(parents=True, exist_ok=True)
    result = check(args.output_dir, args.allow_network)
    print(
        json.dumps(
            dict(
                platform=result["platform"],
                checks=len(result["observations"]),
                usage_before=result["usage_before"],
                usage_after=result["usage_after"],
            ),
            ensure_ascii=False,
        )
    )


if __name__ == "__main__":
    main()
