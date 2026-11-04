"""Virtual matched-order cells: one original can span many counterpart cells."""

from PySide6.QtCore import Qt
from PySide6.QtGui import QColor
from PySide6.QtWidgets import QLabel, QVBoxLayout

from chengchuan.application.microstructure import match_groups
from chengchuan.ui import theme
from chengchuan.ui.table import ValueTableModel
from chengchuan.ui.window_geometry import MaximizableDialog


def order_text(order):
    if order is None:
        return "对手原单缺失／编号歧义\n仅展示可确认的实际成交"
    return (
        f"原单 {order.exchange_id}\n委托 {order.price_fen / 100:.2f}元 × {order.original_shares:,}股\n"
        f"累计成交 {order.executed_shares:,}股" + (" · 已撤余单" if order.cancelled else "")
    )


class MatchTableModel(ValueTableModel):
    def data(self, index, role=Qt.ItemDataRole.DisplayRole):
        if index.isValid():
            if role == Qt.ItemDataRole.TextAlignmentRole:
                return Qt.AlignmentFlag.AlignLeft | Qt.AlignmentFlag.AlignTop
            if role == Qt.ItemDataRole.ForegroundRole and index.column() != 1:
                return QColor(theme.UP if index.column() == 0 else theme.DOWN)
        return super().data(index, role)


class OrderMatchDialog(MaximizableDialog):
    PAGE_SIZE = 300

    def __init__(self, tape, analysis, order, parent=None):
        super().__init__(parent)
        from PySide6.QtWidgets import QHBoxLayout, QHeaderView, QPushButton, QTableView

        self.setAttribute(Qt.WidgetAttribute.WA_DeleteOnClose)
        self.setWindowTitle(f"澄川 · 委托 {order.exchange_id} 买卖成交配对")
        self.resize(1100, 700)
        self.order = order
        self.groups = match_groups(tape, analysis, order)
        self.executions = tuple((g, t) for g in self.groups for t in g.trades)
        self.page = 0
        layout = QVBoxLayout(self)
        average = "—" if order.average_price is None else f"{order.average_price:.4f}元"
        title = QLabel(
            f"{'买' if order.side == 'B' else '卖'}单 {order.exchange_id} · 成交均价 {average} · "
            f"共 {len(self.executions):,} 笔实际成交\n"
            "左右格按原始委托号合并；中间为每笔实际成交价格与股数。对手原单累计量可能包含与其他单的成交。"
        )
        title.setWordWrap(True)
        layout.addWidget(title)
        self.table = QTableView()
        self.model = MatchTableModel(
            ("买方原始委托", "实际成交", "卖方原始委托"), parent=self.table
        )
        self.table.setModel(self.model)
        self.table.horizontalHeader().setSectionResizeMode(QHeaderView.ResizeMode.Stretch)
        self.table.verticalHeader().setDefaultSectionSize(90)
        self.table.setAlternatingRowColors(True)
        self.table.setWordWrap(True)
        layout.addWidget(self.table, 1)
        footer = QHBoxLayout()
        self.previous = QPushButton("上一页")
        self.next = QPushButton("下一页")
        self.caption = QLabel()
        self.previous.clicked.connect(lambda: self._move(-1))
        self.next.clicked.connect(lambda: self._move(1))
        footer.addWidget(self.previous)
        footer.addWidget(self.caption, 1)
        footer.addWidget(self.next)
        layout.addLayout(footer)
        self._render()

    def _move(self, delta):
        self.page += delta
        self._render()

    def _render(self):
        start = self.page * self.PAGE_SIZE
        executions = self.executions[start : start + self.PAGE_SIZE]
        rows = []
        for group, trade in executions:
            left, right = (
                (self.order, group.counterpart)
                if self.order.side == "B"
                else (group.counterpart, self.order)
            )
            rows.append(
                (
                    order_text(left),
                    f"{trade.time:%H:%M:%S.%f}"[:-3]
                    + f"\n{trade.price_fen / 100:.2f}元 × {trade.shares:,}股\n成交号 {trade.trade_id}",
                    order_text(right),
                )
            )
        self.table.clearSpans()
        self.model.set_rows(rows)
        if rows:
            anchor_column = 0 if self.order.side == "B" else 2
            if len(rows) > 1:
                self.table.setSpan(0, anchor_column, len(rows), 1)
            i = 0
            while i < len(rows):
                end = i + 1
                while end < len(rows) and executions[end][0] is executions[i][0]:
                    end += 1
                if end - i > 1:
                    self.table.setSpan(i, 2 - anchor_column, end - i, 1)
                i = end
        self.previous.setEnabled(self.page > 0)
        self.next.setEnabled(start + len(rows) < len(self.executions))
        self.caption.setText(
            f"第{self.page + 1}页 · 显示 {start + 1 if rows else 0}—{start + len(rows)}／{len(self.executions):,}笔；跨页原单继续展示"
            if rows
            else "截至回放时点没有可确认的实际成交；撤单不绘为成交。"
        )
