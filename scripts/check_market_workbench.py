"""联网实机验收：搜索、切股、日K/分时、定时刷新、自选持久化和关闭。"""

import argparse
import json
import tempfile
import time
from pathlib import Path

from PySide6.QtCore import Qt
from PySide6.QtGui import QFont
from PySide6.QtTest import QTest
from PySide6.QtWidgets import QApplication, QFrame

from chengchuan.application.market import MarketService
from chengchuan.application.services import AnalysisService
from chengchuan.infrastructure.legacy import LegacyAnalysisEngine, LegacyJsonRepository
from chengchuan.infrastructure.market_cache import SqliteMarketCache
from chengchuan.infrastructure.market_data import TencentMarketProvider
from chengchuan.infrastructure.workspace import SqliteWorkspaceRepository
from chengchuan.ui.window import MainWindow


def check(output_dir, wait_refresh=False):
    app = QApplication.instance() or QApplication([])
    font = QFont("Noto Sans CJK SC", 10)
    app.setFont(font)
    workspace = SqliteWorkspaceRepository(output_dir / "workspace.sqlite")
    market = MarketService(TencentMarketProvider(), SqliteMarketCache(output_dir / "market.sqlite"))
    window = MainWindow(
        LegacyJsonRepository(), AnalysisService(LegacyAnalysisEngine()), workspace, market=market
    )
    window.show()

    def wait(predicate):
        deadline = time.monotonic() + 25
        while not predicate():
            app.processEvents()
            QTest.qWait(5)
            if time.monotonic() > deadline:
                raise RuntimeError("验收超时：" + window.market_status.text())

    def opened(symbol, interval):
        return (
            window.frame is not None
            and window.snapshot.series.symbol == symbol
            and window.snapshot.series.interval == interval
            and window.report is not None
            and window.market_runner.active is None
            and window.runner.active is None
        )

    observations = []
    try:
        window.browser.search.setText("茅台")
        QTest.keyClick(window.browser.search, Qt.Key.Key_Return)
        wait(lambda: window.browser.results.count() > 0)
        item = window.browser.results.item(0)
        QTest.mouseClick(
            window.browser.results.viewport(),
            Qt.MouseButton.LeftButton,
            pos=window.browser.results.visualItemRect(item).center(),
        )
        wait(lambda: opened("sh600519", "day"))
        window._add_current_stock()
        for symbol in ("sz002318", "sh600519", "sz000001"):
            for interval in ("day", "minute"):
                window.open_stock(symbol, interval, refresh=True)
                wait(lambda: opened(symbol, interval))
                frame = window.frame
                if frame.cached or not frame.quote:
                    raise RuntimeError("联网验收未取得有效新报价：" + window.market_status.text())
                observations.append(
                    {
                        "symbol": symbol,
                        "interval": interval,
                        "bars": len(frame.snapshot.series.bars),
                        "quote_time": frame.quote.timestamp.isoformat(),
                        "retrieved_at": frame.retrieved_at.isoformat(),
                    }
                )
                QTest.qWait(80)
                window.grab().save(str(output_dir / f"{symbol}-{interval}.png"))
                cards = [w for w in window.findChildren(QFrame) if w.objectName() == "metric"]
                if len(cards) < 6 or any(card.height() < 50 for card in cards):
                    raise RuntimeError("分析面板的指标卡片被挤压")
        if wait_refresh:
            previous = window.frame.retrieved_at
            wait(
                lambda: window.frame.retrieved_at > previous and window.market_runner.active is None
            )
        for symbol in ("sh600519", "sz002318", "sz000001"):
            window.open_stock(symbol, "day", refresh=True)
        wait(lambda: opened("sz000001", "day"))
        started = time.monotonic()
        window.close()
        close_ms = round((time.monotonic() - started) * 1000, 2)
        restored = MainWindow(
            LegacyJsonRepository(),
            AnalysisService(LegacyAnalysisEngine()),
            workspace,
            market=market,
        )
        try:
            saved = [
                restored.browser.watchlist.item(i).data(Qt.ItemDataRole.UserRole).symbol
                for i in range(restored.browser.watchlist.count())
            ]
            if "sh600519" not in saved:
                raise RuntimeError("自选持久化复验失败")
        finally:
            restored.close()
        result = {
            "platform": app.platformName(),
            "observations": observations,
            "search": "茅台 → sh600519",
            "automatic_refresh_checked": wait_refresh,
            "rapid_switch_final": "sz000001",
            "watchlist_restored": saved,
            "close_ms": close_ms,
            "artifacts": str(output_dir),
        }
        (output_dir / "result.json").write_text(
            json.dumps(result, ensure_ascii=False, indent=2), encoding="utf-8"
        )
        return result
    finally:
        window.close()


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--output-dir", type=Path)
    parser.add_argument("--wait-refresh", action="store_true")
    args = parser.parse_args()
    folder = args.output_dir or Path(tempfile.mkdtemp(prefix="chengchuan-market-check-"))
    folder.mkdir(parents=True, exist_ok=True)
    print(json.dumps(check(folder, args.wait_refresh), ensure_ascii=False), flush=True)


if __name__ == "__main__":
    main()
