"""Linked crosshairs with non-interactive, translucent viewport overlays."""

import pyqtgraph as pg
from PySide6.QtCore import QObject, Qt, Signal
from PySide6.QtWidgets import QLabel

from chengchuan.application.chart_tools import ChartInspector
from chengchuan.domain.market import is_index
from chengchuan.domain.models import SHANGHAI
from chengchuan.ui import theme


class ChartCursor(QObject):
    text = Signal(str)
    position_changed = Signal(object)

    def __init__(self, price, volume, parent=None):
        super().__init__(parent)
        self.price, self.volume = price, volume
        self.inspector = None
        self.vwap = {}
        self.active_plot = None
        self.show_crosshair, self.show_tooltip = True, True
        pen = pg.mkPen("#90a6bd", width=1, style=Qt.PenStyle.DashLine)
        self.price_x, self.price_y, self.volume_x, self.volume_y = (
            pg.InfiniteLine(angle=angle, pen=pen) for angle in (90, 0, 90, 0)
        )
        for line in self.lines:
            line.setZValue(100)
            line.setAcceptedMouseButtons(Qt.MouseButton.NoButton)
            line.hide()
        self.tips = {}
        for plot in (price, volume):
            plot.setMouseTracking(True)
            plot.viewport().setMouseTracking(True)
            tip = QLabel(plot.viewport())
            tip.setTextFormat(Qt.TextFormat.RichText)
            tip.setAttribute(Qt.WidgetAttribute.WA_TransparentForMouseEvents, True)
            tip.setStyleSheet(
                "QLabel { background: rgba(10, 20, 31, 160); color: #e5eff8; border: 1px solid rgba(130, 158, 184, 100); border-radius: 6px; padding: 7px 9px; font-size: 12px; }"
            )
            tip.hide()
            self.tips[plot] = tip

    def configure(self, show_crosshair=True, show_tooltip=True, opacity=63):
        self.hide()
        self.show_crosshair, self.show_tooltip = show_crosshair, show_tooltip
        alpha = round(255 * opacity / 100)
        for tip in self.tips.values():
            tip.setStyleSheet(
                f"QLabel {{ background: rgba(10, 20, 31, {alpha}); color: #e5eff8; "
                "border: 1px solid rgba(130, 158, 184, 100); border-radius: 6px; "
                "padding: 7px 9px; font-size: 12px; }"
            )

    @property
    def lines(self):
        return self.price_x, self.price_y, self.volume_x, self.volume_y

    def attach(
        self, series, reference_closes=(), baseline_price=None, vwap_points=(), preserve=False
    ):
        self.inspector = ChartInspector(series, reference_closes, baseline_price)
        self.vwap = dict(vwap_points)
        for plot, lines in (
            (self.price, (self.price_x, self.price_y)),
            (self.volume, (self.volume_x, self.volume_y)),
        ):
            for line in lines:
                if line.scene() is None:
                    plot.addItem(line, ignoreBounds=True)
        if preserve and self.active_plot is not None:
            self.move(self._last_point, self.active_plot)
        else:
            self.hide()

    def hide(self, plot=None):
        if plot is not None and self.active_plot is not plot:
            return
        was_active = self.active_plot is not None
        for line in self.lines:
            line.hide()
        for tip in self.tips.values():
            tip.hide()
        self.active_plot = None
        if was_active:
            self.position_changed.emit(None)
            self.price.viewport().update()
            self.volume.viewport().update()

    def move(self, point, plot):
        if self.inspector is None or not plot.getViewBox().sceneBoundingRect().contains(point):
            self.hide(plot)
            return
        pos = plot.getViewBox().mapSceneToView(point)
        self._last_point = point
        bars = self.inspector.series.bars
        if not -0.5 <= pos.x() <= len(bars) - 0.5:
            self.hide(plot)
            return
        index = max(0, min(len(bars) - 1, int(pos.x() + 0.5)))
        bar = bars[index]
        inspected = self.inspector.inspect(index)
        self.active_plot = plot
        for other_plot, other_tip in self.tips.items():
            if other_plot is not plot:
                other_tip.hide()
        self.price_x.setPos(index)
        self.volume_x.setPos(index)
        self.price_x.setVisible(self.show_crosshair)
        self.volume_x.setVisible(self.show_crosshair)
        horizontal = self.price_y if plot is self.price else self.volume_y
        (self.volume_y if plot is self.price else self.price_y).hide()
        horizontal.setPos(pos.y())
        horizontal.setVisible(self.show_crosshair)
        change = "—" if inspected.change_pct is None else f"{inspected.change_pct:+.2f}%"
        color = (
            theme.MUTED
            if inspected.change_pct is None
            else (theme.UP if inspected.change_pct >= 0 else theme.DOWN)
        )
        stamp = bar.timestamp.astimezone(SHANGHAI).strftime("%Y-%m-%d %H:%M:%S")
        name = "收盘价" if bar.open is not None else "分时价"
        index_asset = is_index(self.inspector.series.symbol)
        if index_asset:
            name = "收盘点位" if bar.open is not None else "分时点位"
        details = (
            f"{'光标点位' if index_asset else '光标价'} {pos.y():.2f}"
            if plot is self.price
            else f"该条成交量 {bar.volume_shares / 10000:,.2f}万股"
        )
        average = f" · 均价 {self.vwap[bar.timestamp]:.3f}" if bar.timestamp in self.vwap else ""
        tip = self.tips[plot]
        tip.setText(
            f"<b>{stamp}</b><br>{name} <b>{bar.close:.2f}</b>　<span style='color:{color}'>{change}</span><br><span style='color:#a1b2c3'>{inspected.reference}{average}<br>{details}</span>"
        )
        tip.adjustSize()
        at = plot.mapFromScene(point)
        viewport = plot.viewport()
        x, y = at.x() + 16, at.y() - tip.height() - 12
        if x + tip.width() > viewport.width() - 6:
            x = at.x() - tip.width() - 16
        if y < 6:
            y = at.y() + 16
        tip.move(
            max(6, min(x, viewport.width() - tip.width() - 6)),
            max(6, min(y, viewport.height() - tip.height() - 6)),
        )
        tip.setVisible(self.show_tooltip)
        tip.raise_()
        # Request both linked viewports, including the old crosshair/tooltip area.
        # update() is asynchronous and Qt merges multiple pending mouse events.
        self.price.viewport().update()
        self.volume.viewport().update()
        prices = (
            f"开 {bar.open:.2f}   高 {bar.high:.2f}   低 {bar.low:.2f}   收 {bar.close:.2f}"
            if bar.open is not None
            else f"分时价 {bar.close:.2f}"
        )
        self.text.emit(
            f"{stamp}   {prices}   涨跌 {change}   量 {bar.volume_shares / 10000:,.2f}万股"
        )
        self.position_changed.emit(index)
