"""在独立进程中持续发送Qt鼠标事件，覆盖悬停、点击、选区和快照切换。"""

import argparse
import json
import sys
import tempfile
import time
from pathlib import Path

import pyqtgraph
from PySide6.QtCore import QPointF, Qt, qVersion
from PySide6.QtGui import QMouseEvent
from PySide6.QtTest import QTest
from PySide6.QtWidgets import QApplication

from chengchuan.application.services import AnalysisService
from chengchuan.domain.models import IndexRange
from chengchuan.infrastructure.legacy import LegacyAnalysisEngine, LegacyJsonRepository
from chengchuan.infrastructure.workspace import SqliteWorkspaceRepository
from chengchuan.ui.window import MainWindow

ROOT = Path(__file__).resolve().parents[1]


def wait_until(app, predicate):
    deadline = time.monotonic() + 10
    while not predicate():
        app.processEvents()
        QTest.qWait(1)
        if time.monotonic() > deadline:
            raise RuntimeError("桌面任务超时")


def check(cycles, moves):
    app = QApplication.instance() or QApplication([])
    # Exercise every injected event; the normal UI caps hover processing at 100 Hz.
    pyqtgraph.setConfigOption("mouseRateLimit", 0)
    hover_messages = []
    with tempfile.TemporaryDirectory(prefix="chengchuan-stability-") as folder:
        window = MainWindow(
            LegacyJsonRepository(),
            AnalysisService(LegacyAnalysisEngine()),
            SqliteWorkspaceRepository(Path(folder) / "state.sqlite"),
        )
        window.chart.hover_text.connect(hover_messages.append)
        window.show()
        app.processEvents()
        try:
            for cycle in range(cycles):
                for dataset in (
                    "jiuli_20261019_1102.dataset.json",
                    "jiuli_20261102_full_day.dataset.json",
                ):
                    window.open_snapshot(ROOT / "reports" / dataset)
                    wait_until(
                        app, lambda: window.snapshot is not None and window.runner.active is None
                    )
                    chart = window.chart
                    before = len(hover_messages)
                    for i in range(moves):
                        view = chart.price
                        rect = view.getViewBox().sceneBoundingRect()
                        scene_pos = QPointF(
                            rect.left() + rect.width() * (0.1 + (i * 17 % 80) / 100),
                            rect.top() + rect.height() * (0.1 + (i * 31 % 80) / 100),
                        )
                        point = view.mapFromScene(scene_pos)
                        event = QMouseEvent(
                            QMouseEvent.Type.MouseMove,
                            QPointF(point),
                            QPointF(view.viewport().mapToGlobal(point)),
                            Qt.MouseButton.NoButton,
                            Qt.MouseButton.NoButton,
                            Qt.KeyboardModifier.NoModifier,
                        )
                        app.sendEvent(view.viewport(), event)
                        if i % 25 == 0:
                            QTest.mouseClick(view.viewport(), Qt.MouseButton.LeftButton, pos=point)
                        app.processEvents()
                    if len(hover_messages) - before < moves:
                        raise RuntimeError("鼠标事件未覆盖到行情悬停处理")

                    view = chart.price
                    vb = view.getViewBox()
                    x, y = (sum(bounds) / 2 for bounds in vb.viewRange())
                    start = round(x) - 5
                    a = view.mapFromScene(vb.mapViewToScene(QPointF(start, y)))
                    b = view.mapFromScene(vb.mapViewToScene(QPointF(start + 8, y)))
                    QTest.mousePress(
                        view.viewport(),
                        Qt.MouseButton.LeftButton,
                        Qt.KeyboardModifier.ControlModifier,
                        a,
                    )
                    QTest.mouseMove(view.viewport(), b)
                    QTest.mouseRelease(
                        view.viewport(),
                        Qt.MouseButton.LeftButton,
                        Qt.KeyboardModifier.ControlModifier,
                        b,
                    )
                    wait_until(
                        app, lambda: window.report is not None and window.runner.active is None
                    )
                    if window.report.selection != IndexRange(start, start + 9):
                        raise RuntimeError("连续交互后选区分析不一致")
                    QTest.keyClick(window, Qt.Key.Key_Escape)
                    app.processEvents()
                    if window.selection is not None:
                        raise RuntimeError("Esc未清除选区")
                    window.resize(1440 + 20 * (cycle % 2), 920 + 20 * (cycle % 2))
                    app.processEvents()
        finally:
            window.close()
            app.processEvents()
    return {
        "python": sys.version.split()[0],
        "qt": qVersion(),
        "pyqtgraph": pyqtgraph.__version__,
        "platform": app.platformName(),
        "snapshots_loaded": cycles * 2,
        "mouse_moves": cycles * 2 * moves,
        "hover_updates": len(hover_messages),
    }


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--cycles", type=int, default=4)
    parser.add_argument("--moves", type=int, default=500)
    args = parser.parse_args()
    if args.cycles < 1 or args.moves < 1:
        parser.error("cycles和moves必须为正数")
    print(json.dumps(check(args.cycles, args.moves), ensure_ascii=False), flush=True)


if __name__ == "__main__":
    main()
