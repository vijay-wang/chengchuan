"""Offline company workspace check: force offscreen and never open external documents."""

import argparse
import json
import os
import time
from pathlib import Path

os.environ["QT_QPA_PLATFORM"] = "offscreen"

from PySide6.QtCore import QDate  # noqa: E402
from PySide6.QtTest import QTest  # noqa: E402
from PySide6.QtWidgets import QApplication  # noqa: E402

from chengchuan.application.services import AnalysisService  # noqa: E402
from chengchuan.domain.company import COMPANY_SECTIONS  # noqa: E402
from chengchuan.infrastructure.legacy import LegacyAnalysisEngine, LegacyJsonRepository  # noqa: E402
from chengchuan.infrastructure.market_sources import ConfiguredMarketSources  # noqa: E402
from chengchuan.infrastructure.workspace import SqliteWorkspaceRepository  # noqa: E402
from chengchuan.ui.window import MainWindow  # noqa: E402


def check(symbol, as_of, folder):
    folder.mkdir(parents=True, exist_ok=True)
    app = QApplication.instance() or QApplication([])
    sources = ConfiguredMarketSources(folder.parent, offline=True)
    window = MainWindow(
        LegacyJsonRepository(),
        AnalysisService(LegacyAnalysisEngine()),
        SqliteWorkspaceRepository(folder.parent / "smoke-workspace.sqlite"),
        market=sources.connect("meoz"),
        market_sources=sources,
    )
    window.auto_refresh.setChecked(False)
    window.resize(1600, 1000)
    window.show()
    page = window.company_page
    page.url_opener = lambda _: False
    window.pages.setCurrentWidget(page)
    page.symbol_box.setText(symbol)
    page.date_box.setDate(QDate.fromString(as_of, "yyyyMMdd"))
    report = []
    try:
        for index, (key, _) in enumerate(COMPANY_SECTIONS):
            page.sections.blockSignals(True)
            page.sections.setCurrentRow(index)
            page.sections.blockSignals(False)
            page.section = key
            page.query()
            deadline = time.monotonic() + 30
            while page.runner.active is not None:
                app.processEvents()
                QTest.qWait(5)
                if time.monotonic() > deadline:
                    raise RuntimeError("公司资料离屏查询超时")
            if page.bundle is None or page.bundle.request.section != key:
                raise RuntimeError(page.status.text())
            if key == "holders":
                page.tabs.setCurrentIndex(1)
            for _ in range(4):
                app.processEvents()
                window.repaint()
            view = page.current_view()
            if view and view.model().rowCount():
                view.selectRow(0)
                page._describe(view, 0)
            app.processEvents()
            window.grab().save(str(folder / f"{index + 1:02}-{key}.png"))
            report.append(
                dict(
                    section=key,
                    tables=[
                        dict(key=t.key, rows=len(t.rows), complete=t.meta.complete)
                        for t in page.bundle.tables
                    ],
                )
            )
        window.resize(1100, 740)
        app.processEvents()
        window.grab().save(str(folder / "09-compact.png"))
        report.append(
            dict(compact_requested=[1100, 740], compact_actual=[window.width(), window.height()])
        )
    finally:
        window.close()
        app.processEvents()
    (folder / "summary.json").write_text(json.dumps(report, ensure_ascii=False, indent=2))
    return report


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--symbol", default="002318")
    parser.add_argument("--as-of", required=True)
    parser.add_argument(
        "--output", type=Path, default=Path(".chengchuan/company-validation/screens")
    )
    args = parser.parse_args()
    print(json.dumps(check(args.symbol, args.as_of, args.output), ensure_ascii=False))
