"""Headless research smoke check using existing cache; never opens the work desktop."""

import argparse
import json
import os
import time
from dataclasses import replace
from pathlib import Path

os.environ["QT_QPA_PLATFORM"] = "offscreen"

from PySide6.QtCore import QDate  # noqa: E402
from PySide6.QtTest import QTest  # noqa: E402
from PySide6.QtWidgets import QApplication  # noqa: E402

from chengchuan.application.services import AnalysisService  # noqa: E402
from chengchuan.domain.research import ResearchRequest  # noqa: E402
from chengchuan.infrastructure.legacy import LegacyAnalysisEngine, LegacyJsonRepository  # noqa: E402
from chengchuan.infrastructure.market_sources import ConfiguredMarketSources  # noqa: E402
from chengchuan.infrastructure.workspace import SqliteWorkspaceRepository  # noqa: E402
from chengchuan.ui.window import MainWindow  # noqa: E402


def check(day, output):
    output.mkdir(parents=True, exist_ok=True)
    app = QApplication.instance() or QApplication([])
    sources = ConfiguredMarketSources(output.parent, offline=True)
    market = sources.connect("meoz")
    window = MainWindow(
        LegacyJsonRepository(),
        AnalysisService(LegacyAnalysisEngine()),
        SqliteWorkspaceRepository(output.parent / "smoke-workspace.sqlite"),
        market=market,
        market_sources=sources,
    )
    window.resize(1600, 1000)
    window.auto_refresh.setChecked(False)
    window.show()
    page = window.research_page
    window.pages.setCurrentWidget(page)
    report = []

    def load(request, name, tab=""):
        page.module = request.module
        page.date.setDate(QDate.fromString(request.day, "yyyyMMdd"))
        page.latest.setChecked(request.latest)
        page.symbol.setText(request.symbol)
        page._submit(request)
        deadline = time.monotonic() + 30
        while page.runner.active is not None:
            app.processEvents()
            QTest.qWait(10)
            if time.monotonic() > deadline:
                raise RuntimeError("离屏研究查询超时")
        if page.bundle is None or page.bundle.request != request:
            raise RuntimeError(page.status.text())
        if tab:
            for i in range(page.tabs.count()):
                if page.tabs.widget(i).table.key.startswith(tab):
                    page.tabs.setCurrentIndex(i)
                    break
        for _ in range(12):
            window.repaint()
            app.processEvents()
        if page.current_view().model().rowCount():
            page.current_view().selectRow(0)
            page._describe(page.current_view(), 0)
        app.processEvents()
        window.grab().save(str(output / (name + ".png")))
        report.append(
            dict(
                view=name,
                tables=[
                    dict(key=t.key, rows=len(t.rows), complete=t.meta.complete)
                    for t in page.bundle.tables
                ],
            )
        )
        return page.bundle

    base = ResearchRequest("rotation", day, False)
    try:
        rotation = load(base, "01-rotation")
        code = next(
            str(r["theme_symbol"])
            for t in rotation.tables
            for r in t.records()
            if r.get("theme_symbol")
        )
        load(replace(base, latest=True, code=code, action="detail"), "02-rotation-detail")
        evidence = load(
            replace(base, module="evidence", latest=True, symbol="002318"), "03-evidence", "members"
        )
        theme_id = next(
            str(r["theme_id"]) for t in evidence.tables for r in t.records() if r.get("theme_id")
        )
        load(
            replace(base, module="evidence", latest=True, action="detail", code=theme_id),
            "04-evidence-detail",
            "hierarchy",
        )
        sentiment = load(replace(base, module="sentiment"), "05-sentiment", "pool")
        symbol = next(
            str(r["symbol"]) for t in sentiment.tables for r in t.records() if r.get("symbol")
        )
        load(replace(base, module="sentiment", symbol=symbol, action="detail"), "06-seal", "seal")
        load(replace(base, module="auction", symbol="002318"), "07-auction", "auction_ticks_")
        seats = load(replace(base, module="seats"), "08-seats", "seats")
        seat = next(
            str(r["seat_name"]) for t in seats.tables for r in t.records() if r.get("seat_name")
        )
        load(replace(base, module="seats", action="history", seat=seat), "09-seat-history")
        load(replace(base, module="seats", latest=True, action="aliases"), "10-aliases")
        load(replace(base, module="attention", latest=True, action="compare"), "11-attention")
    finally:
        window.close()
        app.processEvents()
    (output / "summary.json").write_text(json.dumps(report, ensure_ascii=False, indent=2))
    return report


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--day", required=True, help="已缓存交易日YYYYMMDD")
    parser.add_argument(
        "--output", type=Path, default=Path(".chengchuan/research-validation/screens")
    )
    args = parser.parse_args()
    print(json.dumps(check(args.day, args.output), ensure_ascii=False))


if __name__ == "__main__":
    main()
