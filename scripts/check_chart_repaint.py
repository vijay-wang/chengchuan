"""Compare actual screen pixels before and after bottom-edge cursor sweeps (X11)."""

import argparse
import json
from pathlib import Path

import numpy as np
import pyqtgraph as pg
from PySide6.QtCore import QEvent, QPointF, Qt
from PySide6.QtGui import QImage, QMouseEvent
from PySide6.QtTest import QTest
from PySide6.QtWidgets import QApplication

from chengchuan.infrastructure.legacy import LegacyJsonRepository
from chengchuan.ui.chart import MarketChart


def pixels(pixmap):
    image = pixmap.toImage().convertToFormat(QImage.Format.Format_RGBA8888)
    return (
        np.frombuffer(image.constBits(), dtype=np.uint8)
        .reshape(image.height(), image.width(), 4)
        .copy()
    )


def check(folder):
    app = QApplication.instance() or QApplication([])
    if app.platformName() != "xcb":
        raise RuntimeError("残影检查需xcb原生窗口，grab()会强制重画而掩盖残影")
    pg.setConfigOption("mouseRateLimit", 0)
    chart = MarketChart()
    chart.resize(1000, 740)
    chart.show()
    chart.raise_()
    chart.activateWindow()
    if not QTest.qWaitForWindowActive(chart, 2000):
        raise RuntimeError("原生窗口未激活，不能用后台窗口的屏幕像素作验证")
    snapshot = LegacyJsonRepository().load(
        Path(__file__).resolve().parents[1] / "reports/jiuli_20261102_full_day.dataset.json"
    )
    chart.set_snapshot(snapshot)
    chart.clear_selection()
    chart.price.scene().sigMouseMoved.connect(lambda _: None)
    QTest.qWait(200)

    def capture(name):
        # QWidget.grab/render would repaint everything and conceal stale backing pixels.
        shot = chart.screen().grabWindow(chart.winId())
        if shot.isNull():
            raise RuntimeError("无法读取原生窗口像素")
        shot.save(str(folder / name))
        return pixels(shot)

    def move(plot, point):
        local = plot.mapFromScene(point)
        event = QMouseEvent(
            QEvent.Type.MouseMove,
            QPointF(local),
            QPointF(plot.viewport().mapToGlobal(local)),
            Qt.MouseButton.NoButton,
            Qt.MouseButton.NoButton,
            Qt.KeyboardModifier.NoModifier,
        )
        app.sendEvent(plot.viewport(), event)
        app.processEvents()
        QTest.qWait(2)

    try:
        baseline = capture("before.png")
        checkpoints = []

        def compare(name):
            swept = capture(name + "-sweep.png")
            for pane in (chart.price, chart.volume):
                pane.viewport().update()
            QTest.qWait(50)
            clean = capture(name + "-redraw.png")
            checkpoints.append(
                dict(stage=name, stale_pixels=int(np.any(swept != clean, axis=2).sum()))
            )

        for label, plot in (("price", chart.price), ("volume", chart.volume)):
            rect = plot.getViewBox().sceneBoundingRect()
            for number, edge in enumerate((rect.top() + 2, rect.bottom() - 2, rect.bottom() + 5)):
                for i in range(120):
                    move(
                        plot,
                        QPointF(
                            rect.left() + rect.width() * (0.02 + 0.96 * i / 119),
                            edge - (i % 3) * 0.25,
                        ),
                    )
                QTest.qWait(50)
                compare(label + "-" + str(number))
            app.sendEvent(plot.viewport(), QEvent(QEvent.Type.Leave))
            app.processEvents()
        QTest.qWait(100)
        swept = capture("after-sweep.png")
        for plot in (chart.price, chart.volume):
            plot.viewport().update()
        QTest.qWait(100)
        clean = capture("after-full-redraw.png")
        result = dict(
            platform=app.platformName(),
            moves=720,
            stale_pixels=int(np.any(swept != clean, axis=2).sum())
            + sum(c["stale_pixels"] for c in checkpoints),
            baseline_difference=int(np.any(baseline != clean, axis=2).sum()),
            checkpoints=checkpoints,
        )
        (folder / "result.json").write_text(json.dumps(result, indent=2), encoding="utf-8")
        return result
    finally:
        chart.close()


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--output-dir", type=Path, required=True)
    args = parser.parse_args()
    args.output_dir.mkdir(parents=True, exist_ok=True)
    result = check(args.output_dir)
    print(json.dumps(result))
    if result["stale_pixels"]:
        raise SystemExit(1)


if __name__ == "__main__":
    main()
