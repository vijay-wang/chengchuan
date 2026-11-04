"""Clickable index overview and quote details, without vendor access or calculations."""

from PySide6.QtCore import Qt, Signal
from PySide6.QtWidgets import (
    QFrame,
    QGridLayout,
    QHBoxLayout,
    QLabel,
    QPushButton,
    QScrollArea,
    QVBoxLayout,
    QWidget,
)

from chengchuan.domain.market import INDEX_CATALOG, is_index
from chengchuan.domain.models import SHANGHAI
from chengchuan.ui import theme


def value_text(value, unit=""):
    if value is None:
        return "—"
    if unit in ("元", "股"):
        if abs(value) >= 1e8:
            return f"{value / 1e8:,.2f}亿" + unit
        if abs(value) >= 1e4:
            return f"{value / 1e4:,.2f}万" + unit
    if unit == "家":
        return f"{value:,.0f}家"
    return f"{value:,.2f}" + unit


class IndexOverviewBar(QWidget):
    open_requested = Signal(str)

    def __init__(self, parent=None):
        super().__init__(parent)
        self.setObjectName("indexOverview")
        box = QVBoxLayout(self)
        box.setContentsMargins(18, 8, 18, 9)
        box.setSpacing(5)
        header = QHBoxLayout()
        title = QLabel("市场概览")
        title.setObjectName("eyebrow")
        header.addWidget(title)
        self.status = QLabel("点击指数切换到最新图表")
        self.status.setObjectName("eyebrow")
        header.addWidget(self.status, 1, Qt.AlignmentFlag.AlignRight)
        box.addLayout(header)
        row = QHBoxLayout()
        row.setSpacing(8)
        self.buttons, self.prices, self.changes = {}, {}, {}
        for symbol, name, _ in INDEX_CATALOG:
            button = QPushButton()
            button.setObjectName("indexCard")
            button.setCheckable(True)
            button.setMinimumHeight(53)
            button.setToolTip("点击查看最新" + name + "；沿用当前图表周期")
            card = QVBoxLayout(button)
            card.setContentsMargins(10, 5, 10, 5)
            card.setSpacing(2)
            label = QLabel(name)
            label.setObjectName("eyebrow")
            line = QHBoxLayout()
            price, change = QLabel("—"), QLabel("—")
            price.setStyleSheet("font-size: 16px; font-weight: 600;")
            change.setStyleSheet("font-size: 12px;")
            for widget in (label, price, change):
                widget.setAttribute(Qt.WidgetAttribute.WA_TransparentForMouseEvents)
            line.addWidget(price)
            line.addStretch()
            line.addWidget(change)
            card.addWidget(label)
            card.addLayout(line)
            button.clicked.connect(
                lambda checked=False, symbol=symbol: self.open_requested.emit(symbol)
            )
            row.addWidget(button, 1)
            self.buttons[symbol], self.prices[symbol], self.changes[symbol] = button, price, change
        box.addLayout(row)

    def select(self, symbol):
        for code, button in self.buttons.items():
            button.setChecked(code == symbol)

    def reset(self):
        for label in (*self.prices.values(), *self.changes.values()):
            label.setText("—")
        self.status.setText("正在获取指数摘要…")

    def render(self, overview):
        for symbol in self.buttons:
            item = next((i for i in overview.indices if i.symbol == symbol), None)
            self.prices[symbol].setText(value_text(item.price) if item else "—")
            change = item.change_pct if item else None
            self.changes[symbol].setText(f"{change:+.2f}%" if change is not None else "—")
            color = theme.MUTED if change is None else theme.UP if change >= 0 else theme.DOWN
            self.prices[symbol].setStyleSheet(f"font-size:16px;font-weight:600;color:{color};")
            self.changes[symbol].setStyleSheet(f"font-size:12px;color:{color};")
            if item:
                self.buttons[symbol].setToolTip(
                    f"{item.name} · {item.day:%Y-%m-%d}\n成交额 {value_text(item.amount_yuan, '元')}\n点击查看最新图表"
                )
        days = sorted({i.day.isoformat() for i in overview.indices})
        all_a = next((b for b in overview.breadth if b.scope == "all"), None)
        text = "最新概览 · " + (" / ".join(days))
        if all_a:
            text += f" · 全市场 ↑{all_a.up:,} ↓{all_a.down:,} 平{all_a.flat:,}"
        if overview.cached:
            text += " · 缓存"
        self.status.setText(text)
        self.status.setToolTip(overview.notice)

    def failed(self, message):
        self.status.setText("摘要更新未完成；可点击指数查看图表")
        self.status.setToolTip(message)


class QuoteDetailsPanel(QWidget):
    def __init__(self, parent=None):
        super().__init__(parent)
        shell = QVBoxLayout(self)
        shell.setContentsMargins(0, 0, 0, 0)
        scroll = QScrollArea()
        scroll.setWidgetResizable(True)
        self.content = QWidget()
        self.box = QVBoxLayout(self.content)
        self.box.setContentsMargins(16, 18, 16, 18)
        self.box.setSpacing(14)
        self.caption = QLabel("打开证券后查看当日行情")
        self.caption.setObjectName("muted")
        self.caption.setWordWrap(True)
        self.box.addWidget(self.caption)
        self.grid_widget = QWidget()
        self.grid = QGridLayout(self.grid_widget)
        self.grid.setContentsMargins(0, 0, 0, 0)
        self.grid.setHorizontalSpacing(12)
        self.grid.setVerticalSpacing(12)
        self.box.addWidget(self.grid_widget)
        self.breadth_title = QLabel("市场概况")
        self.breadth_title.setObjectName("sectionTitle")
        self.box.addWidget(self.breadth_title)
        self.breadth = QLabel("")
        self.breadth.setWordWrap(True)
        self.box.addWidget(self.breadth)
        self.scope_note = QLabel("")
        self.scope_note.setObjectName("eyebrow")
        self.scope_note.setWordWrap(True)
        self.box.addWidget(self.scope_note)
        self.notice = QLabel("")
        self.notice.setObjectName("eyebrow")
        self.notice.setWordWrap(True)
        self.box.addWidget(self.notice)
        self.box.addStretch()
        scroll.setWidget(self.content)
        shell.addWidget(scroll)
        self.values = {}
        self._keys = ()
        self.clear()

    def clear(self, message="正在加载当日指标…"):
        self.caption.setText(message)
        for label in self.values.values():
            label.setText("—")
        self.notice.clear()
        self._show_breadth(None)

    def render(self, details):
        keys = tuple((m.key, m.label) for m in details.metrics)
        if keys != self._keys:
            while self.grid.count():
                item = self.grid.takeAt(0)
                if item.widget():
                    item.widget().deleteLater()
            self.values = {}
            self._keys = keys
            for i, metric in enumerate(details.metrics):
                cell = QFrame()
                cell.setObjectName("quoteCell")
                layout = QVBoxLayout(cell)
                layout.setContentsMargins(9, 8, 9, 8)
                layout.setSpacing(4)
                title = QLabel(metric.label)
                title.setObjectName("eyebrow")
                value = QLabel("—")
                value.setTextInteractionFlags(Qt.TextInteractionFlag.TextSelectableByMouse)
                value.setStyleSheet("font-size:14px; font-weight:600;")
                layout.addWidget(title)
                layout.addWidget(value)
                self.grid.addWidget(cell, i // 2, i % 2)
                self.values[metric.key] = value
        for metric in details.metrics:
            label = self.values[metric.key]
            text = value_text(metric.value, metric.unit)
            if metric.key in ("change", "pct_chg", "main_net") and metric.value is not None:
                text = ("+" if metric.value >= 0 else "") + text
                label.setStyleSheet(
                    "font-size:14px;font-weight:600;color:"
                    + (theme.UP if metric.value >= 0 else theme.DOWN)
                    + ";"
                )
            else:
                label.setStyleSheet("font-size:14px;font-weight:600;")
            label.setText(text)
            label.setToolTip(metric.source + "\n" + metric.note)
        self.caption.setText(
            f"当日行情 · {details.day:%Y-%m-%d}\n指标取得 {details.retrieved_at.astimezone(SHANGHAI):%m-%d %H:%M:%S}"
            + (" · 缓存" if details.cached else "")
        )
        self.notice.setText(details.notice)
        self._show_breadth(details)

    def _show_breadth(self, details):
        enabled = details is not None and is_index(details.symbol)
        for widget in (self.breadth_title, self.breadth, self.scope_note):
            widget.setVisible(enabled)
        if not enabled:
            return
        scope = {
            "index:sh000001": "sh",
            "index:sz399001": "sz",
            "index:sz399006": "growth",
            "index:sh000688": "star",
        }.get(details.symbol, "all")
        data = next((b for b in details.breadth if b.scope == scope), None) or next(
            (b for b in details.breadth if b.scope == "all"), None
        )
        if data is None:
            self.breadth_title.setText("市场概况")
            self.breadth.setText("涨跌家数／资金统计暂缺")
            self.scope_note.setText("当前源未提供对应日期市场统计。")
            return
        self.breadth_title.setText(data.label)
        net = value_text(data.main_net_yuan, "元")
        direction = (
            "净额"
            if data.main_net_yuan is None
            else "净流入"
            if data.main_net_yuan >= 0
            else "净流出"
        )
        self.breadth.setText(
            f"上涨 {data.up:,}家　下跌 {data.down:,}家\n平盘 {data.flat:,}家　无报价 {data.unknown:,}家\n主力{direction} {net}"
        )
        covered = (
            f"{data.flow_covered:,}/{data.total:,}家"
            if data.flow_covered is not None
            else "源汇总，未提供逐股覆盖"
        )
        self.scope_note.setText(
            f"{data.day:%Y-%m-%d} · 资金覆盖 {covered}\n市场范围统计，并非该指数成分股统计；资金为来源分类，非真实机构持仓。"
        )
