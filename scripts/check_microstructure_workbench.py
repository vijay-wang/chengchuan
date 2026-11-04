"""Actual sector and Level-2 views; defaults to existing local raw caches."""

import argparse
import json
import time
from pathlib import Path

from PySide6.QtTest import QTest
from PySide6.QtWidgets import QApplication

from chengchuan.application.services import AnalysisService
from chengchuan.infrastructure.legacy import LegacyAnalysisEngine, LegacyJsonRepository
from chengchuan.infrastructure.market_sources import ConfiguredMarketSources
from chengchuan.infrastructure.meoz_client import MeozStore
from chengchuan.infrastructure.workspace import SqliteWorkspaceRepository
from chengchuan.ui.window import MainWindow


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
    window.resize(1680, 1000)
    window.show()
    window.auto_refresh.setChecked(False)

    def wait(predicate):
        deadline = time.monotonic() + 120
        while not predicate():
            app.processEvents()
            QTest.qWait(5)
            if time.monotonic() > deadline:
                raise RuntimeError("验收超时：" + window.micro_panel.status.text())

    def capture(widget, name):
        app.processEvents()
        QTest.qWait(40)
        if not widget.grab().save(str(folder / name)):
            raise RuntimeError("截图保存失败")

    try:
        window.open_stock("002318")
        wait(
            lambda: (
                window.frame is not None
                and window.runner.active is None
                and window.market_runner.active is None
                and window.details_runner.active is None
                and window.overview_runner.active is None
            )
        )
        window.open_heatmap()
        heat = window.heatmap_dialog
        wait(lambda: heat.snapshot is not None)
        heat.showMaximized()
        wait(lambda: heat.width() >= heat.screen().availableGeometry().width() * 0.9)
        capture(heat, "heatmap-maximized.png")
        heat._open_sector("880491")
        wait(lambda: heat.sector is not None)
        names = [(m.symbol, m.change_pct, m.main_net_yuan) for m in heat.sector.members[:3]]
        if names[0][0] != "sh688072":
            raise RuntimeError("半导体同涨幅的净流入次序与已核对数据不符")
        capture(heat, "sector-members.png")
        heat.close()
        window.research_tabs.setCurrentWidget(window.micro_panel)
        window._request_l2_tape(6)
        wait(
            lambda: (
                window._l2_analysis is not None
                and window.l2_runner.active is None
                and window.l2_analysis_runner.active is None
            )
        )
        tape, analysis = window._l2_tape, window._l2_analysis
        if not analysis.complete:
            raise RuntimeError("本次样本应已完成全部委托／成交分页")
        counts = sum(b.orders for b in (*analysis.buy, *analysis.sell))
        if (
            counts
            + analysis.cancelled_orders
            + analysis.ambiguous_orders
            + analysis.unpriced_orders
            != len(analysis.restored)
        ):
            raise RuntimeError("委托笔数分解不守恒")
        window._expand_micro()
        full = window.micro_dialog
        full.showMaximized()
        wait(lambda: full.width() >= full.screen().availableGeometry().width() * 0.9)
        full.panel.tabs.setCurrentWidget(full.panel.bands)
        capture(full, "original-order-bands.png")
        full.panel.tabs.setCurrentIndex(0)
        full.panel.depth.setCurrentIndex(full.panel.depth.findData(10))
        capture(full, "historical-book-queue.png")
        full.panel.tabs.setCurrentWidget(full.panel.profile_page)
        capture(full, "price-volume.png")
        full.panel.tabs.setCurrentWidget(full.panel.restored_page)
        capture(full, "restored-orders.png")
        full.hide()
        window.funds_panel.pages.setCurrentIndex(1)
        window.research_tabs.setCurrentWidget(window.funds_panel)
        capture(window, "right-order-bands.png")
        window.research_tabs.setCurrentWidget(window.micro_panel)
        window._request_book()
        wait(
            lambda: (
                window.book_runner.active is None
                and window.micro_panel.book is not None
                and not window.micro_panel.book.historical
            )
        )
        capture(window, "five-level-snapshot.png")
        after = MeozStore().usage()
        if not allow_network and after != before:
            raise RuntimeError("离线回放不应增加付费接口调用")
        report = dict(
            platform=app.platformName(),
            offline=not allow_network,
            before=before,
            after=after,
            sector_top=names,
            orders=len(tape.orders),
            trade_records=len(tape.trades),
            quotes=len(tape.quotes),
            cancelled=analysis.cancelled_orders,
            ambiguous=analysis.ambiguous_orders,
            unpriced=analysis.unpriced_orders,
            unmatched_trade_refs=analysis.unmatched_trade_refs,
            valid_buy=sum(b.orders for b in analysis.buy),
            valid_sell=sum(b.orders for b in analysis.sell),
            buy_fen=sum(b.amount_fen for b in analysis.buy),
            sell_fen=sum(b.amount_fen for b in analysis.sell),
            price_volume_shares=sum(
                p.buy_shares + p.sell_shares + p.unknown_shares for p in analysis.price_volume
            ),
        )
        (folder / "result.json").write_text(
            json.dumps(report, ensure_ascii=False, indent=2), encoding="utf-8"
        )
        print(
            json.dumps(
                dict(
                    platform=report["platform"],
                    offline=report["offline"],
                    added_api_calls=after["total"] - before["total"],
                    orders=report["orders"],
                    cancelled=report["cancelled"],
                ),
                ensure_ascii=False,
            )
        )
    finally:
        window.close()
        app.processEvents()


if __name__ == "__main__":
    parser = argparse.ArgumentParser()
    parser.add_argument("--output-dir", type=Path, required=True)
    parser.add_argument("--allow-network", action="store_true")
    args = parser.parse_args()
    args.output_dir.mkdir(parents=True, exist_ok=True)
    check(args.output_dir, args.allow_network)
