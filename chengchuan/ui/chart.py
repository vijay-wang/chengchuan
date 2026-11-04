from __future__ import annotations

from math import ceil, floor

import pyqtgraph as pg
from PySide6.QtCore import QEvent, QPointF, QRectF, Qt, Signal
from PySide6.QtGui import QColor, QPainter, QPicture
from PySide6.QtWidgets import QGraphicsView, QVBoxLayout, QWidget

from chengchuan.application.chart_tools import locate_date
from chengchuan.domain.models import SHANGHAI, IndexRange, MarketSnapshot, SeriesKind
from chengchuan.domain.periods import chart_period
from chengchuan.ui import theme
from chengchuan.ui.chart_cursor import ChartCursor

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
                fmt = (
                    "%Y"
                    if self.interval == "year"
                    else (
                        "%Y-%m"
                        if self.interval in ("month", "quarter")
                        else ("%m-%d" if chart_period(self.interval).daily else "%m-%d %H:%M")
                    )
                )
                result.append(stamp.strftime(fmt))
            else:
                result.append("")
        return result


class Candles(pg.GraphicsObject):
    """缓存一次绘图指令；不为每根K线创建一个Qt对象。"""

    def __init__(self, bars):
        super().__init__()
        self.bounds = QRectF()
        self.set_bars(bars)

    def set_bars(self, bars):
        self.prepareGeometryChange()
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
        self.update()

    def paint(self, painter, option, widget=None):
        painter.drawPicture(0, 0, self.picture)

    def boundingRect(self):
        return self.bounds


class MarketChart(QWidget):
    range_selected = Signal(object)
    user_range_selected = Signal()
    hover_text = Signal(str)
    snapshot_changed = Signal(object)

    def __init__(self, parent=None):
        super().__init__(parent)
        self.snapshot = None
        self._vwap = {}
        self._baseline = None
        self._drag_start = None
        self._updating = False
        self.volume_headroom = 1.08
        self.axis = TradingAxis()
        self.price = pg.PlotWidget()
        self.volume = pg.PlotWidget(axisItems={"bottom": self.axis})
        self.volume.setXLink(self.price)
        self.price.hideAxis("bottom")
        self.volume.setMaximumHeight(160)
        self.volume.setMinimumHeight(80)
        self.price.setMinimumHeight(240)
        self.price.showGrid(x=True, y=True, alpha=0.13)
        self.volume.showGrid(y=True, alpha=0.1)
        for plot in (self.price, self.volume):
            # InfiniteLine's clipped bounds + translucent child widgets can leave
            # stale pixels at view/axis edges with Qt's minimal dirty rectangles.
            # Coalesce complete plot repaints; cached candles and analysis stay intact.
            plot.setViewportUpdateMode(QGraphicsView.ViewportUpdateMode.FullViewportUpdate)
            plot.hideAxis("left")
            plot.showAxis("right")
            plot.getAxis("right").setWidth(66)
            plot.setMenuEnabled(False)
            plot.setMouseEnabled(x=True, y=False)
            plot.viewport().installEventFilter(self)
        self.price.scene().sigMouseMoved.connect(self._hover)
        self.volume.scene().sigMouseMoved.connect(self._hover_volume)
        self.price.getViewBox().sigXRangeChanged.connect(self._fit_visible)
        self.cursor = ChartCursor(self.price, self.volume, self)
        self.cursor.text.connect(self.hover_text.emit)
        self.cross_x, self.cross_y = self.cursor.price_x, self.cursor.price_y
        self.date_markers = tuple(
            pg.InfiniteLine(angle=90, pen=pg.mkPen("#c8aa69", style=Qt.PenStyle.DotLine))
            for _ in range(2)
        )
        for marker in self.date_markers:
            marker.setAcceptedMouseButtons(Qt.MouseButton.NoButton)
            marker.setZValue(50)
            marker.hide()
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

    def reset(self):
        self.cursor.hide()
        self.cursor.inspector = None
        self.snapshot = None
        self._drag_start = None
        self.price.clear()
        self.volume.clear()
        self.axis.times = []
        self.axis.picture = None
        self.axis.update()
        self.snapshot_changed.emit(None)

    def set_snapshot(
        self,
        snapshot: MarketSnapshot,
        preserve_view=False,
        vwap_points=(),
        baseline_price=None,
        reference_closes=(),
    ):
        self.setUpdatesEnabled(False)
        try:
            self._update_snapshot(
                snapshot, preserve_view, vwap_points, baseline_price, reference_closes
            )
        finally:
            self.setUpdatesEnabled(True)

    def _update_snapshot(
        self, snapshot, preserve_view, vwap_points, baseline_price, reference_closes
    ):
        previous = self.snapshot.series if self.snapshot else None
        incremental = bool(
            preserve_view
            and previous
            and (previous.symbol, previous.interval, previous.kind)
            == (snapshot.series.symbol, snapshot.series.interval, snapshot.series.kind)
        )
        old_range = self.price.getViewBox().viewRange()[0] if preserve_view else None
        self._updating = True
        if not incremental:
            self.cursor.hide()
            self.cursor.inspector = None
        self.snapshot = snapshot
        self._vwap, self._baseline = dict(vwap_points), baseline_price
        self._drag_start = None
        bars = snapshot.series.bars
        if not incremental:
            self.price.clear()
            self.volume.clear()
            self._day_lines = []
            self._day_positions = ()
        self.axis.times = [b.timestamp for b in bars]
        self.axis.interval = snapshot.series.interval
        self.axis.setTicks(None)
        self.axis.picture = None
        self.axis.update()
        if snapshot.series.kind == SeriesKind.OHLC:
            if incremental:
                self._candles.set_bars(bars)
            else:
                self._candles = Candles(bars)
                self.price.addItem(self._candles)
        else:
            xs, prices, averages = [], [], []
            previous_day = None
            for i, bar in enumerate(bars):
                day = bar.timestamp.astimezone(SHANGHAI).date()
                if previous_day is not None and day != previous_day:
                    xs.append(i - 0.5)
                    prices.append(float("nan"))
                    averages.append(float("nan"))
                xs.append(i)
                prices.append(bar.close)
                averages.append(self._vwap.get(bar.timestamp, float("nan")))
                previous_day = day
            if not incremental:
                self._price_line = self.price.plot(
                    pen=pg.mkPen(theme.ACCENT, width=1.7), connect="finite"
                )
                self._vwap_line = self.price.plot(
                    pen=pg.mkPen("#e2bb66", width=1.3), connect="finite"
                )
                self._baseline_line = pg.InfiniteLine(
                    angle=0, pen=pg.mkPen("#65758b", style=Qt.PenStyle.DashLine)
                )
                self.price.addItem(self._baseline_line)
            self._price_line.setData(xs, prices)
            self._vwap_line.setData(xs, averages)
            self._vwap_line.setVisible(bool(vwap_points))
            self._baseline_line.setVisible(baseline_price is not None)
            if baseline_price is not None:
                self._baseline_line.setPos(baseline_price)
        if snapshot.series.interval == "five_day":
            spans = {}
            for i, bar in enumerate(bars):
                day = bar.timestamp.astimezone(SHANGHAI).date()
                spans.setdefault(day, []).append(i)
            self.axis.setTicks(
                [
                    [
                        (sum((indices[0], indices[-1])) / 2, day.strftime("%m-%d"))
                        for day, indices in spans.items()
                    ]
                ]
            )
            positions = tuple(indices[0] - 0.5 for indices in list(spans.values())[1:])
            if positions != self._day_positions:
                for plot, line in self._day_lines:
                    plot.removeItem(line)
                self._day_lines = []
                for position in positions:
                    for plot in (self.price, self.volume):
                        line = pg.InfiniteLine(
                            pos=position,
                            angle=90,
                            pen=pg.mkPen("#283d50", style=Qt.PenStyle.DashLine),
                        )
                        plot.addItem(line)
                        self._day_lines.append((plot, line))
                self._day_positions = positions
        colors = [
            pg.mkBrush(
                theme.UP
                if b.close >= (b.open if b.open is not None else bars[max(0, i - 1)].close)
                else theme.DOWN
            )
            for i, b in enumerate(bars)
        ]
        if not incremental:
            self._volume_item = pg.BarGraphItem(x=[], height=[], width=0.58, pen=None)
            self.volume.addItem(self._volume_item)
        self._volume_item.setOpts(
            x=list(range(len(bars))), height=[b.volume_shares / 10000 for b in bars], brushes=colors
        )
        self.volume.setLabel("right", "万股")
        if not incremental:
            self.price.addItem(self.region)
            self.volume.addItem(self.volume_region)
        self.cursor.attach(
            snapshot.series, reference_closes, baseline_price, vwap_points, preserve=incremental
        )
        for plot, marker in zip((self.price, self.volume), self.date_markers):
            if not incremental:
                plot.addItem(marker, ignoreBounds=True)
                marker.hide()
        self.region.setBounds((-0.5, len(bars) - 0.5))
        visible = (
            90
            if chart_period(snapshot.series.interval).daily
            else (len(bars) if snapshot.series.interval == "five_day" else 242)
        )
        if old_range is None:
            self.price.setXRange(max(-1, len(bars) - visible), len(bars), padding=0.02)
        else:
            self.price.setXRange(*old_range, padding=0)
        self._fit_visible()
        self._updating = False
        self.snapshot_changed.emit(snapshot)

    def focus_date(self, day):
        if self.snapshot is None:
            raise ValueError("请先打开行情")
        location = locate_date(self.snapshot.series, day)
        self.cursor.hide()
        left, right = self.price.getViewBox().viewRange()[0]
        count = len(self.snapshot.series.bars)
        width = max(3, min(right - left, count))
        start = max(-0.5, min(location.index - width / 2, count - width + 0.5))
        self.price.setXRange(start, start + width, padding=0)
        for marker in self.date_markers:
            marker.setPos(location.index)
            marker.show()
        return location

    def show_latest(self):
        if self.snapshot is None:
            return
        count = len(self.snapshot.series.bars)
        period = self.snapshot.series.interval
        visible = 90 if chart_period(period).daily else (count if period == "five_day" else 242)
        self.cursor.hide()
        for marker in self.date_markers:
            marker.hide()
        self.price.setXRange(max(-1, count - visible), count, padding=0.02)

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
        extra = [self._vwap[b.timestamp] for b in bars if b.timestamp in self._vwap]
        if self.snapshot.series.kind == SeriesKind.SAMPLED_CLOSE and self._baseline:
            extra.append(self._baseline)
        if extra:
            low, high = min(low, min(extra)), max(high, max(extra))
        padding = max((high - low) * 0.08, 0.01)
        self.price.setYRange(low - padding, high + padding, padding=0)
        self.volume.setYRange(
            0,
            max(0.01, max(b.volume_shares for b in bars) / 10000) * self.volume_headroom,
            padding=0,
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
            self.user_range_selected.emit()

    def eventFilter(self, watched, event):
        if event.type() in (QEvent.Type.Leave, QEvent.Type.WindowDeactivate):
            plot = self.price if watched is self.price.viewport() else self.volume
            self.cursor.hide(plot)
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
                self.user_range_selected.emit()
                return True
        return super().eventFilter(watched, event)

    def _hover(self, point):
        self.cursor.move(point, self.price)

    def _hover_volume(self, point):
        self.cursor.move(point, self.volume)
