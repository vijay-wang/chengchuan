"""Cached minute inspection and amount-band markers; no I/O in pointer callbacks."""

import pyqtgraph as pg
from PySide6.QtCore import QObject, Qt, QTimer, Signal
from PySide6.QtWidgets import (
    QCheckBox,
    QGridLayout,
    QHBoxLayout,
    QLabel,
    QPushButton,
    QVBoxLayout,
    QWidget,
)

from chengchuan.application.microstructure import BAND_LABELS, select_bands
from chengchuan.domain.microstructure import OrderFilter
from chengchuan.domain.models import SHANGHAI
from chengchuan.ui.market_dashboard import value_text
from chengchuan.ui.order_widgets import COLORS


class ChartOrderController(QObject):
    requested = Signal()

    def __init__(self, chart, parent=None):
        super().__init__(parent)
        self.chart = chart
        self.analysis = None
        self.filters = OrderFilter()
        self.minute_bands = {}
        self._hover_index = None
        self._overlay_key = None
        self.marker_spots = []
        self.controls = QWidget()
        layout = QVBoxLayout(self.controls)
        layout.setContentsMargins(0, 0, 0, 0)
        layout.setSpacing(3)
        row = QHBoxLayout()
        self.show_minute = QCheckBox("悬停显示分钟委托分档")
        self.show_marks = QCheckBox("量柱标记")
        self.load = QPushButton("读取逐笔")
        self.load.setToolTip("按需读取当前证券及日期；悬停和筛选本身不会请求API。")
        self.load.clicked.connect(self.requested)
        row.addWidget(self.show_minute)
        row.addWidget(self.show_marks)
        row.addStretch()
        row.addWidget(self.load)
        layout.addLayout(row)
        self.legend = QWidget()
        grid = QGridLayout(self.legend)
        grid.setContentsMargins(0, 0, 0, 0)
        self.band_checks = []
        for i, (label, color) in enumerate(zip(BAND_LABELS, COLORS)):
            box = QCheckBox("■ " + label)
            box.setStyleSheet(f"QCheckBox {{ color: {color}; }}")
            box.setToolTip("买卖任一方向的原单满足该档即标记；与资金分档的统计筛选一致。")
            box.setChecked(i in (1, 2))
            box.toggled.connect(self._options_changed)
            grid.addWidget(box, i // 5, i % 5)
            self.band_checks.append(box)
        self.legend.hide()
        layout.addWidget(self.legend)
        self.hint = QLabel()
        self.hint.setObjectName("eyebrow")
        self.hint.setWordWrap(True)
        layout.addWidget(self.hint)
        self.overlay = QLabel(chart.price.viewport())
        self.overlay.setTextFormat(Qt.TextFormat.RichText)
        self.overlay.setAttribute(Qt.WidgetAttribute.WA_TransparentForMouseEvents)
        self.overlay.setStyleSheet(
            "QLabel { background: rgba(10, 20, 31, 182); color: #e5eff8; border: 1px solid rgba(130,158,184,110); border-radius: 6px; padding: 8px; font-size: 11px; }"
        )
        self.overlay.hide()
        self.markers = pg.ScatterPlotItem(pxMode=True, size=5, symbol="s", pen=None)
        self.markers.setAcceptedMouseButtons(Qt.MouseButton.NoButton)
        self.markers.setZValue(70)
        self.timer = QTimer(self)
        self.timer.setSingleShot(True)
        self.timer.timeout.connect(self._render_markers)
        chart.volume.getViewBox().sigRangeChanged.connect(self._schedule_markers)
        chart.snapshot_changed.connect(self._snapshot_changed)
        chart.cursor.position_changed.connect(self._hover)
        self.show_minute.toggled.connect(self._options_changed)
        self.show_marks.toggled.connect(self._options_changed)
        self._update_hint()

    def set_analysis(self, analysis):
        self.analysis = analysis
        self._reindex()

    def clear(self):
        self.analysis = None
        self._reindex()

    def shutdown(self):
        self.timer.stop()
        self.overlay.hide()

    def set_filters(self, filters):
        self.filters = filters
        self._reindex()

    def _reindex(self):
        self.overlay.hide()
        self._overlay_key = None
        self.minute_bands = (
            {m.minute: select_bands(m.groups, self.filters) for m in self.analysis.minute_groups}
            if self.analysis
            else {}
        )
        self._update_hint()
        self._schedule_markers()
        if self._hover_index is not None:
            self._hover(self._hover_index)

    def _supported(self):
        snapshot = self.chart.snapshot
        return bool(snapshot and snapshot.series.interval in ("minute", "five_day", "min1"))

    def _ready(self):
        return (
            self._supported()
            and self.analysis is not None
            and self.analysis.complete
            and self.analysis.symbol == self.chart.snapshot.series.symbol
        )

    def _update_hint(self):
        if not self._supported():
            text = "分钟分档及标记适用于分时、五日和1分钟K；勾选后启用。"
        elif self.analysis is None:
            text = "先读取对应交易日逐笔；悬停和勾选只计算本地数据。"
        elif not self.analysis.complete:
            text = "逐笔分页未齐：暂不发布分钟分档及标记，避免漏掉撤单。"
        else:
            text = (
                f"{self.analysis.day:%Y-%m-%d} · 原单提交分钟 · 状态截至{self.analysis.cutoff:%H:%M:%S} · "
                f"{'含' if self.filters.include_unfilled else '剔除'}未成交／{'含' if self.filters.include_cancelled else '剔除'}撤单；与资金分档同步"
            )
        self.hint.setText(text)

    def _options_changed(self, unused=None):
        self.legend.setVisible(self.show_marks.isChecked())
        self.chart.volume_headroom = 1.22 if self.show_marks.isChecked() else 1.08
        if self.chart.snapshot is not None:
            self.chart._fit_visible()
        self._update_hint()
        self._schedule_markers()
        self._overlay_key = None
        self._hover(self._hover_index)

    def _snapshot_changed(self, snapshot):
        self._overlay_key = None
        self.overlay.hide()
        self._hover_index = (
            round(self.chart.cursor.price_x.value())
            if snapshot is not None and self.chart.cursor.active_plot is not None
            else None
        )
        self._update_hint()
        self._schedule_markers()
        self._hover(self._hover_index)

    def _schedule_markers(self, *unused):
        if not self.timer.isActive():
            self.timer.start(16)

    def _render_markers(self):
        self.marker_spots = []
        if self._ready() and self.show_marks.isChecked():
            dx, dy = self.chart.volume.getViewBox().viewPixelSize()
            for i, bar in enumerate(self.chart.snapshot.series.bars):
                minute = bar.timestamp.astimezone(SHANGHAI).replace(second=0, microsecond=0)
                bands = self.minute_bands.get(minute)
                if bands is None:
                    continue
                active = [
                    j
                    for j, box in enumerate(self.band_checks)
                    if box.isChecked() and (bands[0][j].orders + bands[1][j].orders) > 0
                ]
                for slot, j in enumerate(active):
                    self.marker_spots.append(
                        dict(
                            pos=(
                                i + (slot - (len(active) - 1) / 2) * 6 * dx,
                                bar.volume_shares / 10000 + 7 * dy,
                            ),
                            brush=COLORS[j],
                            data=(i, j),
                        )
                    )
        if self.chart.snapshot is not None and self.markers.scene() is None:
            self.chart.volume.addItem(self.markers, ignoreBounds=True)
        self.markers.setData(spots=self.marker_spots)

    def _hover(self, index):
        self._hover_index = index
        if index is None or not self.show_minute.isChecked() or not self._ready():
            self.overlay.hide()
            return
        bars = self.chart.snapshot.series.bars
        if not 0 <= index < len(bars):
            self.overlay.hide()
            return
        minute = bars[index].timestamp.astimezone(SHANGHAI).replace(second=0, microsecond=0)
        if minute.date() != self.analysis.day or minute > self.analysis.cutoff:
            self.overlay.hide()
            return
        key = (minute, self.filters)
        if key != self._overlay_key:
            buy, sell = self.minute_bands.get(minute, select_bands((), self.filters))
            rows = []
            for label, color, b, s in zip(BAND_LABELS, COLORS, buy, sell):
                rows.append(
                    f"<tr><td style='color:{color}'>{label}</td><td>{value_text(b.amount_fen / 100, '元')} · {b.orders:,}笔</td><td>{value_text(s.amount_fen / 100, '元')} · {s.orders:,}笔</td></tr>"
                )
            self.overlay.setText(
                f"<b>{minute:%m-%d %H:%M} 原始委托</b>（提交分钟）<br>"
                "<table cellspacing='1'><tr><th>档位</th><th>买方金额／笔数</th><th>卖方金额／笔数</th></tr>"
                + "".join(rows)
                + "</table>"
                + f"<span style='color:#a1b2c3'>状态截至{self.analysis.cutoff:%H:%M:%S}；与资金筛选一致</span>"
            )
            self.overlay.adjustSize()
            self._overlay_key = key
        viewport = self.chart.price.viewport()
        self.overlay.setMaximumWidth(max(150, viewport.width() - 12))
        self.overlay.adjustSize()
        point = (
            self.chart.cursor._last_point
            if self.chart.cursor.active_plot is self.chart.price
            else None
        )
        right = (
            point is not None and self.chart.price.mapFromScene(point).x() < viewport.width() / 2
        )
        self.overlay.move(max(6, viewport.width() - self.overlay.width() - 6) if right else 6, 6)
        self.overlay.show()
        self.overlay.raise_()
