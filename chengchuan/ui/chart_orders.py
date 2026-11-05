"""Cached minute inspection and amount-band markers; no I/O in pointer callbacks."""

from html import escape

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

from chengchuan.application.microstructure import BAND_LABELS, select_bands, summarize_day
from chengchuan.domain.microstructure import OrderFilter
from chengchuan.domain.models import SHANGHAI
from chengchuan.ui.market_dashboard import value_text
from chengchuan.ui.order_widgets import COLORS


class ChartOrderController(QObject):
    requested = Signal()
    cache_requested = Signal()

    def __init__(self, chart, parent=None):
        super().__init__(parent)
        self.chart = chart
        self.analysis = None
        self.day_summaries = {}
        self.day_status = {}
        self.data_status = ""
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
        self.load = QPushButton("补齐图中逐笔")
        self.load.setToolTip("逐日读取图中原单及成交，复用缓存；可重试缺失日期。")
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
        self.markers = pg.BarGraphItem(x0=[], y0=[], width=[], height=[], brushes=[], pen=None)
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
        self.day_summaries[analysis.day] = summarize_day(analysis)
        self.day_status.pop(analysis.day, None)
        self.data_status = ""
        self._reindex()

    def set_day_summary(self, summary):
        self.day_summaries[summary.day] = summary
        self.day_status.pop(summary.day, None)
        if self.analysis is None or summary.day >= self.analysis.day:
            self.analysis = summary
        self._reindex()

    def restore_days(self, summaries):
        if not summaries:
            return
        self.day_summaries.update(summaries)
        self.day_summaries = dict(sorted(self.day_summaries.items())[-20:])
        self._reindex()

    def required_days(self):
        return (
            tuple(
                sorted(
                    {
                        b.timestamp.astimezone(SHANGHAI).date()
                        for b in self.chart.snapshot.series.bars
                    }
                )
            )
            if self._supported()
            else ()
        )

    def set_day_status(self, day, message):
        self.day_status[day] = message
        self._update_hint()
        self._hover(self._hover_index)

    def set_data_status(self, message):
        self.data_status = message
        self._overlay_key = None
        self._update_hint()
        self._hover(self._hover_index)

    def clear(self):
        self.analysis = None
        self.day_summaries.clear()
        self.day_status.clear()
        self.data_status = ""
        self._hover_index = None
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
        self.minute_bands = {
            m.minute: select_bands(m.groups, self.filters)
            for summary in self.day_summaries.values()
            if summary.complete
            for m in summary.minute_groups
        }
        self._update_hint()
        self._schedule_markers()
        if self._hover_index is not None:
            self._hover(self._hover_index)

    def _supported(self):
        snapshot = self.chart.snapshot
        return bool(snapshot and snapshot.series.interval in ("minute", "five_day", "min1"))

    def _ready(self, day=None):
        if not self._supported():
            return False
        days = (day,) if day is not None else self.required_days()
        return any(
            d in self.day_summaries
            and self.day_summaries[d].complete
            and self.day_summaries[d].symbol == self.chart.snapshot.series.symbol
            for d in days
        )

    def _update_hint(self):
        if not self._supported():
            text = "分钟分档及标记适用于分时、五日和1分钟K；勾选后启用。"
        elif not self.day_summaries:
            status = next(reversed(self.day_status.values())) if self.day_status else ""
            text = self.data_status or status or "尚未加载逐笔：勾选后将逐日加载图中数据。"
        else:
            required = self.required_days()
            ready = sum(self._ready(day) for day in required)
            text = (
                f"逐笔覆盖{ready}/{len(required)}天 · 按各日原单提交分钟，状态截至各日回放时点 · "
                + f"{'含' if self.filters.include_unfilled else '剔除'}未成交／{'含' if self.filters.include_cancelled else '剔除'}撤单"
            )
            if any(
                d in self.day_summaries and not self.day_summaries[d].complete for d in required
            ):
                text += " · 部分日期分页未齐"
            if self.show_marks.isChecked():
                count = len({spot["data"][0] for spot in self.marker_spots})
                text += (
                    f" · 已标记{count}个分钟"
                    if count
                    else " · 所选档位暂无匹配原单，或正在更新标记"
                )
        if self.hint.text() != text:
            self.hint.setText(text)
        style = "color: #c8af73;" if not self._ready() else ""
        if self.hint.styleSheet() != style:
            self.hint.setStyleSheet(style)

    def _options_changed(self, unused=None):
        self.legend.setVisible(self.show_marks.isChecked())
        self.chart.volume_headroom = 1.22 if self.show_marks.isChecked() else 1.08
        if self.chart.snapshot is not None:
            self.chart._fit_visible()
        self._update_hint()
        self._schedule_markers()
        self._overlay_key = None
        self._hover(self._hover_index)
        self._request_cached()
        if not self.show_minute.isChecked() and not self.show_marks.isChecked():
            self.cache_requested.emit()

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
        self._request_cached()

    def _request_cached(self):
        if (
            self._supported()
            and any(not self._ready(day) for day in self.required_days())
            and (self.show_minute.isChecked() or self.show_marks.isChecked())
        ):
            self.cache_requested.emit()

    def _schedule_markers(self, *unused):
        if not self.timer.isActive():
            self.timer.start(16)

    def _render_markers(self):
        self.marker_spots = []
        if self._ready() and self.show_marks.isChecked():
            dy = self.chart.volume.getViewBox().viewPixelSize()[1]
            rail_y = self.chart.volume.getViewBox().viewRange()[1][1] - 8 * dy
            for i, bar in enumerate(self.chart.snapshot.series.bars):
                minute = bar.timestamp.astimezone(SHANGHAI).replace(second=0, microsecond=0)
                if not self._ready(minute.date()):
                    continue
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
                                i - 0.42 + (slot + 0.5) * 0.84 / len(active),
                                rail_y,
                            ),
                            size=(0.84 / len(active), 8 * dy),
                            brush=COLORS[j],
                            data=(i, j),
                        )
                    )
        if self.chart.snapshot is not None and self.markers.scene() is None:
            self.chart.volume.addItem(self.markers, ignoreBounds=True)
        self.markers.setOpts(
            x0=[s["pos"][0] - s["size"][0] / 2 for s in self.marker_spots],
            y0=[s["pos"][1] - s["size"][1] / 2 for s in self.marker_spots],
            width=[s["size"][0] for s in self.marker_spots],
            height=[s["size"][1] for s in self.marker_spots],
            brushes=[pg.mkBrush(s["brush"]) for s in self.marker_spots],
        )
        self._update_hint()

    def _hover(self, index):
        self._hover_index = index
        if index is None or not self.show_minute.isChecked() or not self._supported():
            self.overlay.hide()
            return
        bars = self.chart.snapshot.series.bars
        if not 0 <= index < len(bars):
            self.overlay.hide()
            return
        minute = bars[index].timestamp.astimezone(SHANGHAI).replace(second=0, microsecond=0)
        summary = self.day_summaries.get(minute.date())
        if not self._ready(minute.date()):
            message = self.day_status.get(minute.date()) or (
                "该日逐笔分页未齐，暂不发布金额"
                if summary
                else self.data_status or "该日逐笔尚未加载，正在补取；可点击读取逐笔重试。"
            )
            self.overlay.setText(f"<b>{minute:%m-%d %H:%M} 原始委托</b><br>{escape(message)}")
            self._place_overlay()
            return
        if minute > summary.cutoff:
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
                + f"<span style='color:#a1b2c3'>状态截至{summary.cutoff:%H:%M:%S}；与资金筛选一致</span>"
            )
            self.overlay.adjustSize()
            self._overlay_key = key
        self._place_overlay()

    def _place_overlay(self):
        viewport = self.chart.price.viewport()
        small = viewport.height() < 250
        style = (
            "QLabel { background: rgba(10, 20, 31, 182); color: #e5eff8; "
            "border: 1px solid rgba(130,158,184,110); border-radius: 6px; "
            + ("padding: 5px; font-size: 10px; }" if small else "padding: 8px; font-size: 11px; }")
        )
        if self.overlay.styleSheet() != style:
            self.overlay.setStyleSheet(style)
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
