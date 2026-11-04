"""Verify all twelve MeoZ chart periods, using existing cache by default."""

import argparse
import json
import tempfile
import time
from pathlib import Path

from PySide6.QtGui import QFont
from PySide6.QtTest import QTest
from PySide6.QtWidgets import QApplication

from chengchuan.application.services import AnalysisService
from chengchuan.domain.periods import PERIODS
from chengchuan.infrastructure.forecast_input import ForecastSnapshotRepository
from chengchuan.infrastructure.legacy import LegacyAnalysisEngine
from chengchuan.infrastructure.market_sources import ConfiguredMarketSources
from chengchuan.infrastructure.meoz_client import MeozClient
from chengchuan.infrastructure.workspace import SqliteWorkspaceRepository
from chengchuan.ui.window import MainWindow


def check(symbol, folder, allow_network=False):
    app = QApplication.instance() or QApplication([])
    app.setFont(QFont("Noto Sans CJK SC", 10))
    client = MeozClient(offline=not allow_network)
    usage_before = client.store.usage()
    sources = ConfiguredMarketSources(folder, offline=not allow_network)
    window = MainWindow(
        ForecastSnapshotRepository(),
        AnalysisService(LegacyAnalysisEngine()),
        SqliteWorkspaceRepository(folder / "workspace.sqlite"),
        market=sources.connect("meoz"),
        market_sources=sources,
    )
    window.resize(1600, 1000)
    window.show()
    window.auto_refresh.setChecked(False)

    def wait(predicate):
        deadline = time.monotonic() + 40
        while not predicate():
            app.processEvents()
            QTest.qWait(5)
            if time.monotonic() > deadline:
                raise RuntimeError("MeoZ验收超时：" + window.market_status.text())

    observations = []
    try:
        if window.period.count() != 12:
            raise RuntimeError("图表周期未完整注册")
        for period in PERIODS:
            start = time.monotonic()
            window.open_stock(symbol, period.code)
            wait(
                lambda: (
                    window.frame is not None
                    and window.snapshot.series.interval == period.code
                    and window.report is not None
                    and window.runner.active is None
                    and window.market_runner.active is None
                )
            )
            QTest.qWait(80)
            frame = window.frame
            if not window.grab().save(str(folder / (period.code + ".png"))):
                raise RuntimeError("无法保存图表截图")
            observations.append(
                dict(
                    period=period.code,
                    label=period.label,
                    bars=len(frame.snapshot.series.bars),
                    kind=frame.snapshot.series.kind.value,
                    first=frame.snapshot.series.bars[0].timestamp.isoformat(),
                    last=frame.snapshot.series.bars[-1].timestamp.isoformat(),
                    cached=frame.cached,
                    vwap_points=len(frame.vwap_points),
                    seconds=round(time.monotonic() - start, 3),
                )
            )
        window.period.showPopup()
        app.processEvents()
        window.period.view().window().grab().save(str(folder / "period-menu.png"))
        window.period.hidePopup()
        after = client.store.usage()
        if not allow_network and after["total"] != usage_before["total"]:
            raise RuntimeError("离线验收意外产生了API计数")
        result = dict(
            symbol=symbol,
            offline=not allow_network,
            platform=app.platformName(),
            source=window.source_box.currentData(),
            observations=observations,
            usage_before=usage_before,
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
    parser.add_argument("--output-dir", type=Path)
    parser.add_argument(
        "--allow-network", action="store_true", help="允许有本地额度保护的数据请求；默认离线验收"
    )
    args = parser.parse_args()
    folder = args.output_dir or Path(tempfile.mkdtemp(prefix="chengchuan-meoz-check-"))
    folder.mkdir(parents=True, exist_ok=True)
    result = check(args.symbol, folder, args.allow_network)
    print(json.dumps(result, ensure_ascii=False))


if __name__ == "__main__":
    main()
