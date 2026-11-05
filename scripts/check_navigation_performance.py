"""Offscreen navigation timings against real local data; never opens a foreground window."""

import argparse
import json
import os
import statistics
import time
from datetime import date
from pathlib import Path

os.environ["QT_QPA_PLATFORM"] = "offscreen"

from PySide6.QtCore import QEvent, QObject  # noqa: E402
from PySide6.QtTest import QTest  # noqa: E402
from PySide6.QtWidgets import QApplication  # noqa: E402

from chengchuan import __file__ as package_path  # noqa: E402
from chengchuan import __version__  # noqa: E402
from chengchuan.application.services import AnalysisService  # noqa: E402
from chengchuan.infrastructure.legacy import LegacyAnalysisEngine, LegacyJsonRepository  # noqa: E402
from chengchuan.infrastructure.market_sources import ConfiguredMarketSources  # noqa: E402
from chengchuan.infrastructure.meoz_client import MeozStore  # noqa: E402
from chengchuan.infrastructure.workspace import SqliteWorkspaceRepository  # noqa: E402
from chengchuan.ui.window import MainWindow  # noqa: E402


class PaintCounter(QObject):
    def __init__(self, viewport):
        super().__init__(viewport)
        self.count = 0
        viewport.installEventFilter(self)

    def eventFilter(self, watched, event):
        if event.type() == QEvent.Type.Paint:
            self.count += 1
        return False


def check(folder, label, day, rounds=2):
    folder.mkdir(parents=True, exist_ok=True)
    app = QApplication.instance() or QApplication([])
    assert app.platformName() == "offscreen"
    sources = ConfiguredMarketSources(folder, offline=True)
    service = sources.connect("meoz")
    cases = [
        ("sz002318", p)
        for p in (
            "day",
            "minute",
            "five_day",
            "week",
            "month",
            "quarter",
            "year",
            "min1",
            "min5",
            "min15",
            "min30",
            "min60",
        )
    ]
    cases += [("index:sh000001", p) for p in ("day", "minute", "five_day")]
    cases += [("sz300750", "day")]
    cases += [
        (symbol, "day")
        for symbol in (
            "index:sh000300",
            "index:sh000688",
            "index:sh000905",
            "index:sz399001",
            "index:sz399006",
        )
    ]
    before = MeozStore().usage()
    usable, skipped = [], []
    # Both versions start with identical disk caches; no HTTP or front-end objects yet.
    for symbol, interval in cases:
        try:
            service.load(symbol, interval, as_of=day)
            usable.append((symbol, interval))
        except Exception as exc:
            skipped.append(dict(symbol=symbol, interval=interval, reason=str(exc)))
    window = MainWindow(
        LegacyJsonRepository(),
        AnalysisService(LegacyAnalysisEngine()),
        SqliteWorkspaceRepository(folder / "workspace.sqlite"),
        market=service,
        market_sources=sources,
    )
    window.auto_refresh.setChecked(False)
    window._overview_requested = True
    window.resize(1600, 1000)
    window.show()
    app.processEvents()
    paints = PaintCounter(window.chart.price.viewport())
    rows = []
    try:
        for round_index in range(rounds):
            for symbol, interval in usable:
                count = paints.count
                start = time.perf_counter()
                window.open_stock(symbol, interval, as_of=day)
                first = None
                deadline = time.monotonic() + 30
                while True:
                    app.processEvents()
                    QTest.qWait(1)
                    if (
                        window.frame is not None
                        and window.frame.snapshot.series.symbol == symbol
                        and window.frame.snapshot.series.interval == interval
                        and window.history_date == day
                        and paints.count > count
                        and first is None
                    ):
                        first = time.perf_counter()
                    if (
                        first is not None
                        and window.market_runner.active is None
                        and window.details_runner.active is None
                    ):
                        break
                    if time.monotonic() > deadline:
                        raise RuntimeError(
                            "Performance check timed out: " + window.market_status.text()
                        )
                rows.append(
                    dict(
                        round=round_index + 1,
                        symbol=symbol,
                        interval=interval,
                        bars=len(window.snapshot.series.bars),
                        first_paint_ms=round((first - start) * 1000, 2),
                        settled_ms=round((time.perf_counter() - start) * 1000, 2),
                    )
                )
    finally:
        window.close()
        app.processEvents()
    after = MeozStore().usage()
    assert before == after, "Offline check must not consume API requests"

    def summary(items):
        values = sorted(r["first_paint_ms"] for r in items)
        return (
            dict(
                count=len(values),
                median_ms=round(statistics.median(values), 2),
                p95_ms=values[min(len(values) - 1, int(len(values) * 0.95))],
                max_ms=max(values),
            )
            if values
            else {}
        )

    report = dict(
        version=__version__,
        package_path=package_path,
        label=label,
        platform=app.platformName(),
        as_of=day.isoformat(),
        requests_added=0,
        cold=summary([r for r in rows if r["round"] == 1]),
        warm=summary([r for r in rows if r["round"] > 1]),
        rows=rows,
        skipped=skipped,
    )
    (folder / (label + ".json")).write_text(json.dumps(report, ensure_ascii=False, indent=2))
    return {k: v for k, v in report.items() if k not in ("rows",)}


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--label", required=True)
    parser.add_argument("--as-of", type=date.fromisoformat, default=date(2026, 11, 4))
    parser.add_argument("--rounds", type=int, default=2)
    parser.add_argument("--output", type=Path, default=Path(".chengchuan/performance-validation"))
    args = parser.parse_args()
    print(
        json.dumps(check(args.output, args.label, args.as_of, args.rounds), ensure_ascii=False),
        flush=True,
    )
