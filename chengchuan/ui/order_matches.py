"""Inline chronological reconstruction, with adjacent original-order cells merged."""

from PySide6.QtCore import Qt
from PySide6.QtGui import QColor, QPen
from PySide6.QtWidgets import (
    QComboBox,
    QHBoxLayout,
    QHeaderView,
    QLabel,
    QPushButton,
    QStyledItemDelegate,
    QTableView,
    QVBoxLayout,
    QWidget,
)

from chengchuan.application.microstructure import execution_spans, select_executions
from chengchuan.domain.microstructure import OrderFilter
from chengchuan.ui import theme
from chengchuan.ui.table import ValueTableModel


class ExecutionModel(ValueTableModel):
    def __init__(self, parent=None):
        super().__init__(("时间", "价格", "成交(手)", "卖单(手)", "买单(手)"), parent=parent)
        self.executions = ()

    def data(self, index, role=Qt.ItemDataRole.DisplayRole):
        if not index.isValid() or index.row() >= len(self.executions):
            return None
        row = self.executions[index.row()]
        column = index.column()
        if role == Qt.ItemDataRole.ToolTipRole and column == 0:
            return f"{row.trade.time:%Y-%m-%d %H:%M:%S.%f}\n成交号 {row.trade.trade_id}"
        if role == Qt.ItemDataRole.TextAlignmentRole:
            return Qt.AlignmentFlag.AlignCenter
        if role == Qt.ItemDataRole.ForegroundRole:
            if column in (3, 4):
                return QColor(theme.DOWN if column == 3 else theme.UP)
            if column == 2:
                return QColor(
                    theme.UP
                    if row.trade.side == "B"
                    else theme.DOWN
                    if row.trade.side == "S"
                    else theme.TEXT
                )
        if role == Qt.ItemDataRole.ToolTipRole and column in (3, 4):
            order = row.sell if column == 3 else row.buy
            if order is None:
                return "对手原单缺失或编号歧义；不按相同价格猜配对。"
            return (
                f"原单{order.exchange_id} · 原价{order.price_fen / 100:.2f}元\n原量{order.original_shares:,}股 · 累计成交{order.executed_shares:,}股"
                + (" · 已撤余单" if order.cancelled else "")
            )
        return super().data(index, role)


class OrderCellDelegate(QStyledItemDelegate):
    def paint(self, painter, option, index):
        if index.column() in (3, 4) and index.data(Qt.ItemDataRole.UserRole) is not None:
            painter.save()
            rect = option.rect.adjusted(2, 2, -2, -2)
            painter.fillRect(rect, QColor("#253142" if index.column() == 3 else "#1b2431"))
            painter.setPen(
                QPen(
                    QColor(theme.DOWN if index.column() == 3 else theme.UP), 1, Qt.PenStyle.DotLine
                )
            )
            painter.drawRect(rect)
            painter.setPen(QColor(theme.DOWN if index.column() == 3 else theme.UP))
            font = painter.font()
            font.setBold(True)
            painter.setFont(font)
            visible = (
                rect.intersected(option.widget.viewport().rect())
                if isinstance(option.widget, QTableView)
                else rect
            )
            painter.drawText(visible, Qt.AlignmentFlag.AlignCenter, index.data())
            painter.restore()
        else:
            super().paint(painter, option, index)


class InlineReconstruction(QWidget):
    PAGE_SIZE = 300

    def __init__(self, parent=None):
        super().__init__(parent)
        self.analysis = None
        self.filters = OrderFilter(True, True)
        self.selected = None
        self.executions = ()
        self.page = 0
        layout = QVBoxLayout(self)
        layout.setContentsMargins(0, 0, 0, 0)
        tools = QHBoxLayout()
        self.ordering = QComboBox()
        self.ordering.addItems(("最新在前", "最早在前"))
        self.ordering.currentIndexChanged.connect(self._rebuild)
        self.unit = QComboBox()
        self.unit.addItems(("手（100股）", "股"))
        self.unit.currentIndexChanged.connect(self._render)
        self.all_button = QPushButton("全部成交")
        self.all_button.clicked.connect(self.show_all)
        tools.addWidget(self.ordering)
        tools.addWidget(self.unit)
        tools.addWidget(self.all_button)
        layout.addLayout(tools)
        self.table = QTableView()
        self.model = ExecutionModel(self.table)
        self.table.setModel(self.model)
        self.table.setItemDelegate(OrderCellDelegate(self.table))
        self.table.setShowGrid(False)
        self.table.setAlternatingRowColors(True)
        self.table.setSelectionBehavior(QTableView.SelectionBehavior.SelectRows)
        self.table.verticalHeader().hide()
        self.table.verticalHeader().setDefaultSectionSize(32)
        self.table.horizontalHeader().setSectionResizeMode(QHeaderView.ResizeMode.Stretch)
        layout.addWidget(self.table, 1)
        footer = QHBoxLayout()
        self.previous, self.next = QPushButton("上一页"), QPushButton("下一页")
        self.caption = QLabel()
        self.caption.setWordWrap(True)
        self.previous.clicked.connect(lambda: self._move(-1))
        self.next.clicked.connect(lambda: self._move(1))
        footer.addWidget(self.previous)
        footer.addWidget(self.caption, 1)
        footer.addWidget(self.next)
        layout.addLayout(footer)
        self.clear()

    def clear(self):
        self.analysis = self.selected = None
        self.executions = ()
        self.page = 0
        self._render()

    def set_analysis(self, analysis, filters):
        self.analysis, self.filters, self.selected = analysis, filters, None
        self._rebuild()

    def set_filters(self, filters):
        if filters == self.filters:
            return
        self.filters = filters
        self._rebuild()

    def show_order(self, order):
        self.selected = order
        self._rebuild()

    def show_all(self):
        self.selected = None
        self._rebuild()

    def _rebuild(self, unused=None):
        self.page = 0
        self.executions = (
            select_executions(self.analysis, self.filters, self.selected) if self.analysis else ()
        )
        if self.ordering.currentIndex() == 0:
            self.executions = tuple(reversed(self.executions))
        self._render()

    def _move(self, delta):
        self.page += delta
        self._render()

    def _render(self, unused=None):
        start = self.page * self.PAGE_SIZE
        rows = self.executions[start : start + self.PAGE_SIZE]
        hands = self.unit.currentIndex() == 0
        divisor = 100 if hands else 1
        self.model.headers = (
            "时间",
            "价格",
            "成交(手)" if hands else "成交(股)",
            "卖单(手)" if hands else "卖单(股)",
            "买单(手)" if hands else "买单(股)",
        )
        self.table.clearSpans()
        self.model.executions = rows
        self.model.formats = {
            1: lambda v: f"{v:.2f}",
            **{
                i: lambda v: f"{v:,.2f}".rstrip("0").rstrip(".") if hands else f"{v:,}"
                for i in (2, 3, 4)
            },
        }
        self.model.set_rows(
            (
                r.trade.time.strftime("%H:%M:%S"),
                r.trade.price_fen / 100,
                r.trade.shares / divisor if hands else r.trade.shares,
                r.sell.original_shares / divisor
                if r.sell and hands
                else r.sell.original_shares
                if r.sell
                else None,
                r.buy.original_shares / divisor
                if r.buy and hands
                else r.buy.original_shares
                if r.buy
                else None,
            )
            for r in rows
        )
        for side, column in (("sell", 3), ("buy", 4)):
            for first, count, order in execution_spans(rows, side):
                if count > 1:
                    self.table.setSpan(first, column, count, 1)
        self.previous.setEnabled(self.page > 0)
        self.next.setEnabled(start + len(rows) < len(self.executions))
        self.all_button.setEnabled(self.selected is not None)
        prefix = f"原单{self.selected.exchange_id} · " if self.selected else ""
        self.caption.setText(
            prefix
            + (
                f"{start + 1}—{start + len(rows)}／{len(self.executions):,}笔 · 连续同号合并"
                if rows
                else "暂无符合筛选条件的实际成交"
            )
            + (" · 分页未齐，仅已载入样本" if self.analysis and not self.analysis.complete else "")
        )
