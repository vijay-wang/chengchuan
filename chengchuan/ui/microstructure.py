"""Order-book, original-order bands and replay views; calculations live in application."""

from math import atan2, degrees

import pyqtgraph as pg
from PySide6.QtCore import Qt, QTime, Signal
from PySide6.QtGui import QColor, QPainter, QPen
from PySide6.QtWidgets import (
    QComboBox,
    QHBoxLayout,
    QHeaderView,
    QLabel,
    QPushButton,
    QScrollArea,
    QSpinBox,
    QTableView,
    QTableWidget,
    QTableWidgetItem,
    QTabWidget,
    QTimeEdit,
    QToolTip,
    QVBoxLayout,
    QWidget,
)

from chengchuan.application.microstructure import order_queue, select_bands
from chengchuan.ui import theme
from chengchuan.ui.market_dashboard import value_text
from chengchuan.ui.order_matches import InlineReconstruction
from chengchuan.ui.order_widgets import COLORS, OrderFilterControls
from chengchuan.ui.table import ValueTableModel
from chengchuan.ui.window_geometry import MaximizableDialog


class PriceVolumeChart(pg.PlotWidget):
    def __init__(self, parent=None):
        super().__init__(parent=parent)
        self.setMinimumHeight(180)
        self.setMenuEnabled(False)
        self.setLabel("left", "成交价格（元）")
        self.setLabel("bottom", "成交量（万股）")
        legend = self.addLegend(labelTextColor=theme.TEXT)
        legend.setBrush(pg.mkBrush(12, 18, 27, 230))
        legend.setPen(pg.mkPen("#293b51"))
        self.items_by_side = []
        for label, color in (("源B", theme.UP), ("源S", theme.DOWN), ("未标方向", theme.MUTED)):
            item = pg.BarGraphItem(
                x0=[], y=[], width=[], height=0.008, brush=color, pen=None, name=label
            )
            self.addItem(item)
            self.items_by_side.append(item)

    def set_profile(self, rows):
        prices = [r.price_fen / 100 for r in rows]
        offset = [0.0] * len(rows)
        for item, key in zip(self.items_by_side, ("buy_shares", "sell_shares", "unknown_shares")):
            values = [getattr(r, key) / 10000 for r in rows]
            item.setOpts(x0=offset, y=prices, width=values)
            offset = [a + b for a, b in zip(offset, values)]
        if rows:
            self.setYRange(min(prices) - 0.02, max(prices) + 0.02, padding=0)
            self.setXRange(0, max(offset) or 1, padding=0.05)


class OrderPie(QWidget):
    def __init__(self, title, parent=None):
        super().__init__(parent)
        self.title = title
        self.bands = ()
        self.ready = False
        self.setMinimumHeight(165)
        self.setMaximumHeight(220)
        self.setMouseTracking(True)

    def set_bands(self, bands, ready):
        self.bands = tuple(bands)
        self.ready = ready
        self.update()

    def paintEvent(self, event):
        from PySide6.QtCore import QRectF

        painter = QPainter(self)
        painter.setRenderHint(QPainter.RenderHint.Antialiasing)
        size = min(self.width() - 12, self.height() - 12)
        rect = QRectF((self.width() - size) / 2, 6, size, size)
        total = sum(b.amount_fen for b in self.bands) if self.ready else 0
        start = 90 * 16
        if total:
            for i, band in enumerate(self.bands):
                angle = band.amount_fen / total * 360 * 16
                painter.setPen(QPen(QColor(theme.BACKGROUND), 1))
                painter.setBrush(QColor(COLORS[i]))
                painter.drawPie(rect, round(start), round(-angle))
                start -= angle
        else:
            painter.setBrush(QColor("#203044"))
            painter.setPen(Qt.PenStyle.NoPen)
            painter.drawEllipse(rect)
        inner = rect.adjusted(size * 0.24, size * 0.24, -size * 0.24, -size * 0.24)
        painter.setBrush(QColor(theme.BACKGROUND))
        painter.setPen(Qt.PenStyle.NoPen)
        painter.drawEllipse(inner)
        painter.setPen(QColor(theme.TEXT))
        caption = (
            self.title + "\n" + (value_text(total / 100, "元") if self.ready else "待完整数据")
        )
        painter.drawText(inner, Qt.AlignmentFlag.AlignCenter, caption)

    def mouseMoveEvent(self, event):
        total = sum(b.amount_fen for b in self.bands) if self.ready else 0
        if total:
            point = event.position()
            angle = (
                degrees(atan2(point.x() - self.width() / 2, self.height() / 2 - point.y())) + 360
            ) % 360
            accumulated = 0
            for band in self.bands:
                percent = band.amount_fen / total * 100
                accumulated += percent * 3.6
                if angle <= accumulated:
                    QToolTip.showText(
                        event.globalPosition().toPoint(),
                        f"{band.label} · {value_text(band.amount_fen / 100, '元')}\n{band.orders:,}笔 · {percent:.2f}%",
                        self,
                    )
                    break
        super().mouseMoveEvent(event)


class OrderBandsPanel(QWidget):
    requested = Signal()
    filters_changed = Signal(object)

    def __init__(self, compact=False, parent=None):
        super().__init__(parent)
        self.analysis = None
        outer = QVBoxLayout(self)
        self.load_button = QPushButton("读取猫爪原始委托／补齐缓存")
        self.load_button.clicked.connect(self.requested)
        outer.addWidget(self.load_button)
        self.caption = QLabel("原始委托金额分档；整张撤销委托剔除。")
        self.caption.setWordWrap(True)
        outer.addWidget(self.caption)
        self.filters = OrderFilterControls()
        self.filters.changed.connect(self._filters_changed)
        outer.addWidget(self.filters)
        scroll = QScrollArea()
        scroll.setWidgetResizable(True)
        content = QWidget()
        layout = QVBoxLayout(content) if compact else QHBoxLayout(content)
        self.tables, self.pies = [], []
        for side in ("买入委托", "卖出委托"):
            section = QVBoxLayout()
            section.addWidget(QLabel(side))
            pie = OrderPie(side)
            section.addWidget(pie)
            table = QTableWidget(9, 4)
            table.setHorizontalHeaderLabels(("原单区间", "委托金额", "笔数", "占比"))
            table.verticalHeader().hide()
            table.setEditTriggers(QTableWidget.EditTrigger.NoEditTriggers)
            table.horizontalHeader().setSectionResizeMode(QHeaderView.ResizeMode.Stretch)
            table.verticalHeader().setDefaultSectionSize(25)
            table.setMinimumHeight(265)
            section.addWidget(table)
            layout.addLayout(section)
            self.tables.append(table)
            self.pies.append(pie)
        scroll.setWidget(content)
        outer.addWidget(scroll, 1)
        self.note = QLabel("按原始价格×原始数量，每张委托仅计一笔；非实际成交流入流出。")
        self.note.setWordWrap(True)
        self.note.setObjectName("eyebrow")
        outer.addWidget(self.note)
        self.clear()

    def clear(self, message="尚未读取逐笔数据；仅按需查询，不自动反复下载。"):
        from chengchuan.application.microstructure import BAND_LABELS

        self.analysis = None
        self.caption.setText(message)
        for table, pie in zip(self.tables, self.pies):
            pie.set_bands((), False)
            for i, label in enumerate(BAND_LABELS):
                for j, text in enumerate((label, "—", "—", "—")):
                    table.setItem(i, j, QTableWidgetItem(text))

    def render(self, analysis):
        self.analysis = analysis
        self._draw()

    def set_filters(self, filters):
        self.filters.set_value(filters)
        if self.analysis is not None:
            self._draw()

    def _filters_changed(self, filters):
        if self.analysis is not None:
            self._draw()
        self.filters_changed.emit(filters)

    def _draw(self):
        analysis = self.analysis
        filters = self.filters.value()
        self.caption.setText(
            f"{analysis.symbol[-6:]} · {analysis.day:%Y-%m-%d} · 截至{analysis.cutoff:%H:%M:%S}\n"
            f"已撤销{analysis.cancelled_orders:,}笔（{'允许统计' if filters.include_cancelled else '剔除'}）／全体歧义{analysis.all_ambiguous_orders:,}笔／全体无价{analysis.all_unpriced_orders:,}笔"
        )
        selected = select_bands(analysis.band_groups, filters)
        for table, pie, bands in zip(self.tables, self.pies, selected):
            total = sum(b.amount_fen for b in bands)
            for i, band in enumerate(bands):
                texts = (
                    band.label,
                    value_text(band.amount_fen / 100, "元"),
                    f"{band.orders:,}",
                    f"{band.amount_fen / total * 100:.2f}%" if total else "0.00%",
                )
                for j, text in enumerate(texts):
                    table.item(i, j).setText(text if analysis.complete or j == 0 else "—")
                    if j == 0:
                        table.item(i, j).setForeground(QColor(COLORS[i]))
            pie.set_bands(bands, analysis.complete)
        self.note.setText(
            "按原始价格×原始数量，每张只计一笔；部分成交仍按整张原单金额。"
            + ("包含" if filters.include_unfilled else "剔除")
            + "成交股数为0的委托；"
            + ("包含" if filters.include_cancelled else "剔除")
            + "整张已撤销原单（含部分成交后撤余）。"
            + "无法唯一还原和无原价委托始终不计；非实际成交流入流出。"
            + ("分页未齐，最终金额及饼图不发布。" if not analysis.complete else "")
        )


def value_table(headers, formats=None, colors=()):
    table = QTableView()
    model = ValueTableModel(headers, formats, colors, parent=table)
    table.setModel(model)
    table.horizontalHeader().setSectionResizeMode(QHeaderView.ResizeMode.ResizeToContents)
    table.horizontalHeader().setStretchLastSection(True)
    table.setSelectionBehavior(QTableView.SelectionBehavior.SelectRows)
    table.setAlternatingRowColors(True)
    return table, model


class MicrostructurePanel(QWidget):
    snapshot_requested = Signal()
    tape_requested = Signal(int)
    stream_requested = Signal(bool)
    replay_requested = Signal(object)
    import_requested = Signal()
    expand_requested = Signal()
    restored_filters_changed = Signal(object)

    def __init__(self, expanded=False, parent=None):
        super().__init__(parent)
        self.tape = None
        self.analysis = None
        self.book = None
        self.symbol = None
        self.streaming = False
        self._visible_restored = ()
        box = QVBoxLayout(self)
        top = QHBoxLayout()
        self.snapshot_button = QPushButton("读取五档")
        self.snapshot_button.clicked.connect(self.snapshot_requested)
        top.addWidget(self.snapshot_button)
        self.stream_button = QPushButton("连接实时")
        self.stream_button.setCheckable(True)
        self.stream_button.toggled.connect(self.stream_requested)
        top.addWidget(self.stream_button)
        if not expanded:
            expand = QPushButton("展开工作台")
            expand.clicked.connect(self.expand_requested)
            top.addWidget(expand)
        box.addLayout(top)
        self.status = QLabel("五档为合计挂单；真实逐笔委托／十档当前由已发布历史回放提供。")
        self.status.setWordWrap(True)
        box.addWidget(self.status)
        tools = QHBoxLayout()
        self.load_button = QPushButton("读取逐笔")
        self.load_button.clicked.connect(lambda: self.tape_requested.emit(self.pages.currentData()))
        tools.addWidget(self.load_button)
        self.pages = QComboBox()
        self.pages.addItem("每类最多6页", 6)
        self.pages.addItem("每类最多8页", 8)
        tools.addWidget(self.pages)
        import_button = QPushButton("导入JSON")
        import_button.clicked.connect(self.import_requested)
        tools.addWidget(import_button)
        box.addLayout(tools)
        replay = QHBoxLayout()
        self.cutoff = QTimeEdit(QTime(23, 59, 59))
        self.cutoff.setDisplayFormat("HH:mm:ss")
        replay.addWidget(self.cutoff)
        play = QPushButton("按时点回放")
        play.clicked.connect(lambda: self.replay_requested.emit(self.cutoff.time().toPython()))
        replay.addWidget(play)
        box.addLayout(replay)
        self.tabs = QTabWidget()
        box.addWidget(self.tabs, 1)
        book_page = QWidget()
        book_box = QVBoxLayout(book_page)
        choices = QHBoxLayout()
        self.depth = QComboBox()
        for count in (1, 5, 10):
            self.depth.addItem(f"{count}档", count)
        self.depth.setCurrentIndex(1)
        choices.addWidget(self.depth)
        self.queue_side = QComboBox()
        self.queue_side.addItems(("买盘队列", "卖盘队列"))
        choices.addWidget(self.queue_side)
        self.queue_level = QSpinBox()
        self.queue_level.setRange(1, 5)
        self.queue_level.setPrefix("第")
        self.queue_level.setSuffix("档")
        choices.addWidget(self.queue_level)
        book_box.addLayout(choices)
        self.book_label = QLabel("暂无盘口快照")
        self.book_label.setWordWrap(True)
        book_box.addWidget(self.book_label)
        self.book_table, self.book_model = value_table(
            ("档位", "买价", "买量", "卖价", "卖量"),
            {
                1: lambda v: f"{v:.2f}",
                2: lambda v: f"{v:,.0f}",
                3: lambda v: f"{v:.2f}",
                4: lambda v: f"{v:,.0f}",
            },
        )
        book_box.addWidget(self.book_table, 1)
        self.queue_note = QLabel("实时快照没有逐笔队列；历史载入后显示可还原的限价委托样本。")
        self.queue_note.setWordWrap(True)
        book_box.addWidget(self.queue_note)
        self.queue_table, self.queue_model = value_table(
            ("源时间", "委托号", "剩余股数", "价格", "剩余金额"),
            {2: lambda v: f"{v:,}", 3: lambda v: f"{v:.2f}", 4: lambda v: value_text(v, "元")},
        )
        book_box.addWidget(self.queue_table, 1)
        self.tabs.addTab(book_page, "盘口／队列")
        self.order_table, self.order_model = value_table(
            ("时间", "原编号", "交易所委托号", "类型", "方向", "价格", "原始股数", "原始金额"),
            {5: lambda v: f"{v:.2f}", 6: lambda v: f"{v:,}", 7: lambda v: value_text(v, "元")},
        )
        self.tabs.addTab(self.order_table, "逐笔委托")
        self.restored_table, self.restored_model = value_table(
            (
                "原时间",
                "委托号",
                "方向",
                "原始价格",
                "成交均价",
                "原始金额",
                "原始股数",
                "成交股数",
                "成交笔数",
                "状态",
            ),
            {3: lambda v: f"{v:.2f}", 4: lambda v: f"{v:.4f}", 5: lambda v: value_text(v, "元")},
        )
        self.restored_page = QWidget()
        restored_box = QVBoxLayout(self.restored_page)
        self.restored_filters = OrderFilterControls("显示", include_cancelled=True)
        self.restored_filters.unfilled.setToolTip(
            "作用于原单列表；成交还原只显示实际成交，不虚构未成交记录。"
        )
        self.restored_filters.changed.connect(self._restored_filter_changed)
        restored_box.addWidget(self.restored_filters)
        self.restored_views = QTabWidget()
        self.inline_matches = InlineReconstruction()
        self.restored_views.addTab(self.inline_matches, "成交还原")
        original_page = QWidget()
        original_box = QVBoxLayout(original_page)
        original_box.addWidget(QLabel("双击原单在本页定位对应成交；未成交开关用于原单列表。"))
        original_box.addWidget(self.restored_table, 1)
        self.restored_views.addTab(original_page, "原单列表")
        restored_box.addWidget(self.restored_views, 1)
        self.tabs.addTab(self.restored_page, "逐笔还原")
        self.profile_table, self.profile_model = value_table(
            ("成交价", "源B量", "源S量", "未标方向量", "总量"), {0: lambda v: f"{v:.2f}"}
        )
        profile = QWidget()
        self.profile_page = profile
        profile_box = QVBoxLayout(profile)
        self.profile_chart = PriceVolumeChart()
        profile_box.addWidget(self.profile_chart, 1)
        profile_box.addWidget(self.profile_table, 1)
        self.tabs.addTab(profile, "量价分布")
        self.bands = OrderBandsPanel(compact=not expanded)
        self.bands.requested.connect(lambda: self.tape_requested.emit(self.pages.currentData()))
        self.tabs.addTab(self.bands, "原始委托分档")
        self.depth.currentIndexChanged.connect(self._render_book)
        self.queue_side.currentIndexChanged.connect(self._render_queue)
        self.queue_level.valueChanged.connect(self._render_queue)
        self.restored_table.doubleClicked.connect(self._show_fills)

    def clear(self, message="选择个股后按需读取；不会自动下载整日逐笔。"):
        self.tape = self.analysis = self.book = None
        self._restored_key = None
        self._visible_restored = ()
        self.inline_matches.clear()
        self.status.setText(message)
        for model in (
            self.book_model,
            self.queue_model,
            self.order_model,
            self.restored_model,
            self.profile_model,
        ):
            model.set_rows(())
        self.bands.clear()
        self.profile_chart.set_profile(())
        self.book_label.setText("暂无盘口快照")
        self.stream_button.blockSignals(True)
        self.stream_button.setChecked(False)
        self.stream_button.blockSignals(False)

    def render_book(self, book):
        self.book = book
        self.book_label.setText(
            f"{book.source} · {book.time:%Y-%m-%d %H:%M:%S} · 数量单位：{book.quantity_unit}"
        )
        self.status.setText(
            f"{book.symbol[-6:]} · {book.time:%Y-%m-%d %H:%M:%S.%f}\n{book.source} · 数量单位：{book.quantity_unit}\n{book.notice}"
        )
        self.depth.model().item(self.depth.findData(10)).setEnabled(len(book.levels) >= 10)
        if self.depth.currentData() > len(book.levels):
            self.depth.setCurrentIndex(self.depth.findData(5))
        self._render_book()

    def _render_book(self, unused=None):
        if self.book is None:
            return
        depth = min(self.depth.currentData(), len(self.book.levels))
        self.queue_level.setMaximum(depth)
        self.book_model.set_rows(
            (b.level, b.bid_price, b.bid_quantity, b.ask_price, b.ask_quantity)
            for b in self.book.levels[:depth]
        )
        self._render_queue()

    def render_analysis(self, tape, analysis):
        if self.tape is tape and self.analysis is analysis:
            return
        self.tape, self.analysis = tape, analysis
        self.inline_matches.set_analysis(analysis, self.restored_filters.value())
        self.bands.render(analysis)
        self.order_model.set_rows(
            (
                e.time.strftime("%H:%M:%S.%f")[:-3],
                e.source_id,
                e.exchange_id,
                e.kind,
                e.side,
                e.price_fen / 100,
                e.shares,
                e.price_fen * e.shares / 100,
            )
            for e in tape.orders
            if e.time <= analysis.cutoff
        )
        self._render_restored()
        self.profile_model.set_rows(
            (
                r.price_fen / 100,
                r.buy_shares,
                r.sell_shares,
                r.unknown_shares,
                r.buy_shares + r.sell_shares + r.unknown_shares,
            )
            for r in analysis.price_volume
        )
        self.profile_chart.set_profile(analysis.price_volume)
        quotes = [q for q in tape.quotes if q.time <= analysis.cutoff]
        if quotes:
            self.render_book(quotes[-1])
        self.status.setText(
            f"{tape.symbol[-6:]} · {tape.day:%Y-%m-%d} · 委托{len(tape.orders):,}／成交及撤单{len(tape.trades):,}／快照{len(tape.quotes):,}\n"
            f"截止{analysis.cutoff:%H:%M:%S} · 成交关联缺失{analysis.unmatched_trade_refs:,}次 · 未知／阶段事件{analysis.unknown_events:,}\n{tape.notice}"
        )
        self._render_queue()

    def set_restored_filters(self, filters):
        self.restored_filters.set_value(filters)
        self._render_restored()

    def _restored_filter_changed(self, filters):
        self._render_restored()
        self.restored_filters_changed.emit(filters)

    def _render_restored(self):
        filters = self.restored_filters.value()
        key = (id(self.analysis), filters)
        if getattr(self, "_restored_key", None) == key:
            return
        self._restored_key = key
        self.inline_matches.set_filters(filters)
        self._visible_restored = (
            tuple(r for r in self.analysis.restored if filters.accepts(r)) if self.analysis else ()
        )
        self.restored_model.set_rows(
            (
                r.time.strftime("%H:%M:%S.%f")[:-3],
                r.exchange_id,
                r.side,
                r.price_fen / 100,
                r.average_price,
                r.original_fen / 100,
                r.original_shares,
                r.executed_shares,
                r.trade_count,
                "已撤销"
                if r.cancelled
                else "编号歧义（剔除）"
                if r.ambiguous
                else "全部成交"
                if r.executed_shares >= r.original_shares
                else "部分成交"
                if r.executed_shares
                else "未成交",
            )
            for r in self._visible_restored
        )

    def _render_queue(self, unused=None):
        self.queue_model.set_rows(())
        if self.book is None or self.analysis is None or self.tape is None:
            return
        if not self.book.historical or self.book.time.date() != self.analysis.day:
            self.queue_note.setText(
                "实时五档只有合计价量，没有单笔队列；不能将历史委托贴到实时盘口。"
            )
            return
        level = self.book.levels[min(self.queue_level.value(), len(self.book.levels)) - 1]
        buy = self.queue_side.currentIndex() == 0
        price = level.bid_price if buy else level.ask_price
        if price is None:
            return
        rows = order_queue(self.analysis, "B" if buy else "S", round(price * 100))
        self.queue_model.set_rows(
            (
                r.time.strftime("%H:%M:%S.%f")[:-3],
                r.exchange_id,
                r.remaining_shares,
                r.price_fen / 100,
                r.remaining_shares * r.price_fen / 100,
            )
            for r in rows
        )
        self.queue_note.setText(
            "可还原限价委托样本，最多50笔；按源时间／编号排序。源缺通道及全序号，不能宣称完整交易所排队优先级。"
        )

    def _show_fills(self, index):
        if self.tape is None or self.analysis is None:
            return
        if not index.isValid() or not 0 <= index.row() < len(self._visible_restored):
            return
        order = self._visible_restored[index.row()]
        if order.ambiguous or order.exchange_id <= 0:
            self.status.setText("该委托编号缺失或不唯一，不能展示已确认的成交关联。")
            return
        self.inline_matches.show_order(order)
        self.restored_views.setCurrentWidget(self.inline_matches)


class MicrostructureDialog(MaximizableDialog):
    def __init__(self, parent=None):
        super().__init__(parent)
        self.setWindowTitle("澄川 · 盘口与逐笔工作台")
        self.setWindowFlags(
            self.windowFlags()
            | Qt.WindowType.WindowMaximizeButtonHint
            | Qt.WindowType.WindowMinimizeButtonHint
        )
        self.resize(1280, 850)
        self.setSizeGripEnabled(True)
        box = QVBoxLayout(self)
        self.panel = MicrostructurePanel(expanded=True)
        box.addWidget(self.panel)
