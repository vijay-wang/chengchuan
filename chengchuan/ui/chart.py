from __future__ import annotations

from math import ceil, floor

import pyqtgraph as pg
from PySide6.QtCore import QEvent, QPointF, QRectF, Qt, Signal
from PySide6.QtGui import QColor, QPainter, QPicture
from PySide6.QtWidgets import QVBoxLayout, QWidget

from chengchuan.domain.models import SHANGHAI, IndexRange, MarketSnapshot, SeriesKind
from chengchuan.ui import theme

pg.setConfigOptions(antialias=True, background=theme.BACKGROUND, foreground=theme.MUTED)


class TradingAxis(pg.AxisItem):
    def __init__(self):
        super().__init__(orientation="bottom")
        self.times = []
        self.interval = "day"

    def tickStrings(self, values, scale, spacing):
        result = []
        for x in values:
            i = round(x)
            if abs(x - i) < 0.1 and 0 <= i < len(self.times):
                stamp = self.times[i].astimezone(SHANGHAI)
                result.append(stamp.strftime("%m-%d" if self.interval == "day" else "%m-%d %H:%M"))
            else:
                result.append("")
        return result


class Candles(pg.GraphicsObject):
    """缓存一次绘图指令；不为每根K线创建一个Qt对象。"""

    def __init__(self, bars):
        super().__init__()
        self.picture = QPicture()
        painter = QPainter(self.picture)
        for i, bar in enumerate(bars):
            color = theme.UP if bar.close >= bar.open else theme.DOWN
            pen = pg.mkPen(color, width=1)
            pen.setCosmetic(True)
            painter.setPen(pen)
            painter.setBrush(QColor(color))
            painter.drawLine(QPointF(i, bar.low), QPointF(i, bar.high))
            if bar.open == bar.close:
                painter.drawLine(QPointF(i - 0.3, bar.close), QPointF(i + 0.3, bar.close))
            else:
                painter.drawRect(
                    QRectF(i - 0.29, min(bar.open, bar.close), 0.58, abs(bar.close - bar.open))
                )
        painter.end()
        low, high = min(b.low for b in bars), max(b.high for b in bars)
        self.bounds = QRectF(-0.5, low, len(bars), max(0.01, high - low))

    def paint(self, painter, option, widget=None):
        painter.drawPicture(0, 0, self.picture)

    def boundingRect(self):
        return self.bounds


class MarketChart(QWidget):
    range_selected = Signal(object)
    hover_text = Signal(str)

    def __init__(self, parent=None):
        super().__init__(parent)
        self.snapshot = None
        self._drag_start = None
        self._updating = False
        self.axis = TradingAxis()
        self.price = pg.PlotWidget()
        self.volume = pg.PlotWidget(axisItems={"bottom": self.axis})
        self.volume.setXLink(self.price)
        self.price.hideAxis("bottom")
        self.volume.setMaximumHeight(160)
        self.price.setMinimumHeight(280)
        self.price.showGrid(x=True, y=True, alpha=0.13)
        self.volume.showGrid(y=True, alpha=0.1)
        for plot in (self.price, self.volume):
            plot.hideAxis("left")
            plot.showAxis("right")
            plot.getAxis("right").setWidth(66)
            plot.setMenuEnabled(False)
            plot.setMouseEnabled(x=True, y=False)
            plot.viewport().installEventFilter(self)
        self.price.scene().sigMouseMoved.connect(self._hover)
        self.price.getViewBox().sigXRangeChanged.connect(self._fit_visible)
        self.cross_x = pg.InfiniteLine(
            angle=90, pen=pg.mkPen("#42576e", style=Qt.PenStyle.DashLine)
        )
        self.cross_y = pg.InfiniteLine(angle=0, pen=pg.mkPen("#42576e", style=Qt.PenStyle.DashLine))
        self.region = pg.LinearRegionItem(
            brush=pg.mkBrush(76, 155, 157, 28), pen=pg.mkPen("#488d91"), swapMode="sort"
        )
        self.volume_region = pg.LinearRegionItem(
            brush=pg.mkBrush(76, 155, 157, 28), pen=pg.mkPen("#488d91"), movable=False
        )
        self.region.sigRegionChangeFinished.connect(self._region_finished)
        layout = QVBoxLayout(self)
        layout.setContentsMargins(0, 0, 0, 0)
        layout.setSpacing(0)
        layout.addWidget(self.price, 4)
        layout.addWidget(self.volume, 1)

    def set_snapshot(self, snapshot: MarketSnapshot):
        self._updating = True
        self.snapshot = snapshot
        self._drag_start = None
        bars = snapshot.series.bars
        self.price.clear()
        self.volume.clear()
        self.axis.times = [b.timestamp for b in bars]
        self.axis.interval = snapshot.series.interval
        self.axis.picture = None
        self.axis.update()
        if snapshot.series.kind == SeriesKind.OHLC:
            self.price.addItem(Candles(bars))
        else:
            self.price.plot(
                range(len(bars)), [b.close for b in bars], pen=pg.mkPen(theme.ACCENT, width=1.7)
            )
        colors = [
            pg.mkBrush(
                theme.UP
                if b.close >= (b.open if b.open is not None else bars[max(0, i - 1)].close)
                else theme.DOWN
            )
            for i, b in enumerate(bars)
        ]
        self.volume.addItem(
            pg.BarGraphItem(
                x=list(range(len(bars))),
                height=[b.volume_shares / 10000 for b in bars],
                width=0.58,
                brushes=colors,
                pen=None,
            )
        )
        self.volume.setLabel("right", "万股")
        self.price.addItem(self.region)
        self.volume.addItem(self.volume_region)
        self.price.addItem(self.cross_x, ignoreBounds=True)
        self.price.addItem(self.cross_y, ignoreBounds=True)
        self.region.setBounds((-0.5, len(bars) - 0.5))
        visible = 90 if snapshot.series.interval == "day" else 242
        self.price.setXRange(max(-1, len(bars) - visible), len(bars), padding=0.02)
        self._fit_visible()
        self._updating = False

    def _fit_visible(self, *args):
        if self.snapshot is None:
            return
        left, right = self.price.getViewBox().viewRange()[0]
        all_bars = self.snapshot.series.bars
        start = max(0, min(len(all_bars) - 1, floor(left)))
        stop = max(start + 1, min(len(all_bars), ceil(right) + 1))
        bars = all_bars[start:stop]
        low = min(b.low if b.low is not None else b.close for b in bars)
        high = max(b.high if b.high is not None else b.close for b in bars)
        padding = max((high - low) * 0.08, 0.01)
        self.price.setYRange(low - padding, high + padding, padding=0)
        self.volume.setYRange(
            0, max(0.01, max(b.volume_shares for b in bars) / 10000) * 1.08, padding=0
        )

    def set_selection(self, selection: IndexRange, notify=True):
        self._updating = True
        region = (selection.start - 0.45, selection.stop - 0.55)
        self.region.setRegion(region)
        self.volume_region.setRegion(region)
        self.region.show()
        self.volume_region.show()
        self._updating = False
        if notify:
            self.range_selected.emit(selection)

    def clear_selection(self):
        self._drag_start = None
        self.region.hide()
        self.volume_region.hide()

    def _region_finished(self):
        if not self._updating and self.snapshot:
            selection = IndexRange.from_drag(
                *self.region.getRegion(), len(self.snapshot.series.bars)
            )
            self.set_selection(selection)

    def eventFilter(self, watched, event):
        if self.snapshot is None:
            return super().eventFilter(watched, event)
        kind = event.type()
        if kind in (QEvent.Type.FocusOut, QEvent.Type.WindowDeactivate):
            self._drag_start = None
        if kind not in (
            QEvent.Type.MouseButtonPress,
            QEvent.Type.MouseMove,
            QEvent.Type.MouseButtonRelease,
        ):
            return super().eventFilter(watched, event)
        plot = self.price if watched is self.price.viewport() else self.volume
        point = plot.mapToScene(event.position().toPoint())
        vb = plot.getViewBox()
        if kind == QEvent.Type.MouseButtonPress and (
            event.button() == Qt.MouseButton.LeftButton
            and event.modifiers() & Qt.KeyboardModifier.ControlModifier
            and vb.sceneBoundingRect().contains(point)
        ):
            self._drag_start = vb.mapSceneToView(point).x()
            return True
        if self._drag_start is not None:
            selection = IndexRange.from_drag(
                self._drag_start, vb.mapSceneToView(point).x(), len(self.snapshot.series.bars)
            )
            if kind == QEvent.Type.MouseMove:
                self.set_selection(selection, notify=False)
                return True
            if kind == QEvent.Type.MouseButtonRelease:
                self._drag_start = None
                self.set_selection(selection)
                return True
        return super().eventFilter(watched, event)

    def _hover(self, point):
        if not self.snapshot or not self.price.getViewBox().sceneBoundingRect().contains(point):
            return
        pos = self.price.getViewBox().mapSceneToView(point)
        i = max(0, min(len(self.snapshot.series.bars) - 1, round(pos.x())))
        bar = self.snapshot.series.bars[i]
        self.cross_x.setPos(i)
        self.cross_y.setPos(pos.y())
        stamp = bar.timestamp.astimezone(SHANGHAI).strftime("%Y-%m-%d %H:%M")
        if bar.open is not None:
            prices = (
                f"开 {bar.open:.2f}   高 {bar.high:.2f}   低 {bar.low:.2f}   收 {bar.close:.2f}"
            )
        else:
            prices = f"采样价 {bar.close:.2f}"
        self.hover_text.emit(f"{stamp}   {prices}   量 {bar.volume_shares / 10000:,.2f}万股")
