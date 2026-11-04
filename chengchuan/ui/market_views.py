"""Native market heatmap and money-flow views; no vendor or database access."""

from functools import partial

from PySide6.QtCore import QRectF, Qt, Signal
from PySide6.QtGui import QColor, QPainter
from PySide6.QtWidgets import (
    QComboBox,
    QDialog,
    QHBoxLayout,
    QHeaderView,
    QLabel,
    QLineEdit,
    QPushButton,
    QTableWidget,
    QTableWidgetItem,
    QToolTip,
    QVBoxLayout,
    QWidget,
)

from chengchuan.application.market_views import layout_tiles
from chengchuan.domain.models import SHANGHAI
from chengchuan.ui import theme
from chengchuan.ui.market_dashboard import value_text
from chengchuan.ui.tasks import ProcessTaskRunner


class HeatmapCanvas(QWidget):
    open_requested = Signal(str)

    def __init__(self, parent=None):
        super().__init__(parent)
        self.setMinimumSize(600, 350)
        self.setMouseTracking(True)
        self.tiles, self.rects = (), ()
        self.limit = 200

    def set_tiles(self, tiles, limit=200):
        self.tiles, self.limit = tuple(tiles), limit
        self._layout()

    def _layout(self):
        self.rects = layout_tiles(self.tiles, self.width(), self.height(), self.limit)
        self.update()

    def resizeEvent(self, event):
        self._layout()
        super().resizeEvent(event)

    def paintEvent(self, event):
        painter = QPainter(self)
        painter.fillRect(self.rect(), QColor(theme.BACKGROUND))
        for item in self.rects:
            rect = QRectF(item.x + 1, item.y + 1, max(0, item.width - 2), max(0, item.height - 2))
            change = item.tile.change_pct
            base = QColor(
                theme.MUTED if change is None else theme.UP if change >= 0 else theme.DOWN
            )
            intensity = 0.20 if change is None else 0.18 + min(abs(change), 10) / 10 * 0.65
            background = QColor(theme.BACKGROUND)
            color = QColor(
                *(
                    round(background.getRgb()[i] * (1 - intensity) + base.getRgb()[i] * intensity)
                    for i in range(3)
                )
            )
            painter.fillRect(rect, color)
            if rect.width() >= 44 and rect.height() >= 30:
                painter.setPen(QColor("#edf5fc"))
                name = painter.fontMetrics().elidedText(
                    item.tile.name, Qt.TextElideMode.ElideRight, int(rect.width() - 8)
                )
                text = name
                if rect.height() >= 45:
                    text += "\n" + (f"{change:+.2f}%" if change is not None else "—")
                painter.drawText(rect.adjusted(4, 2, -4, -2), Qt.AlignmentFlag.AlignCenter, text)
        if not self.rects:
            painter.setPen(QColor(theme.MUTED))
            painter.drawText(self.rect(), Qt.AlignmentFlag.AlignCenter, "暂无有成交额的匹配标的")

    def _hit(self, point):
        return next(
            (r.tile for r in self.rects if QRectF(r.x, r.y, r.width, r.height).contains(point)),
            None,
        )

    def mouseMoveEvent(self, event):
        tile = self._hit(event.position())
        if tile:
            change = "—" if tile.change_pct is None else f"{tile.change_pct:+.2f}%"
            QToolTip.showText(
                event.globalPosition().toPoint(),
                f"{tile.name} · {tile.code}\n涨跌幅 {change}\n成交额 {value_text(tile.amount_yuan, '元')}",
                self,
            )
        else:
            QToolTip.hideText()
        super().mouseMoveEvent(event)

    def leaveEvent(self, event):
        QToolTip.hideText()
        super().leaveEvent(event)

    def mouseReleaseEvent(self, event):
        tile = self._hit(event.position())
        if event.button() == Qt.MouseButton.LeftButton and tile and tile.symbol:
            self.open_requested.emit(tile.symbol)
        super().mouseReleaseEvent(event)


class HeatmapDialog(QDialog):
    open_requested = Signal(str)

    def __init__(self, market, watchlist=(), runner_factory=ProcessTaskRunner, parent=None):
        super().__init__(parent)
        self.market, self.watchlist = market, frozenset(watchlist)
        self.snapshot = None
        self.setWindowTitle("澄川 · 市场热力图")
        self.resize(1180, 760)
        self.setAttribute(Qt.WidgetAttribute.WA_DeleteOnClose)
        box = QVBoxLayout(self)
        row = QHBoxLayout()
        self.kind = QComboBox()
        for label, code in (("行业板块", "hy"), ("概念板块", "gn"), ("沪深个股", "stocks")):
            self.kind.addItem(label, code)
        row.addWidget(self.kind)
        self.scope = QComboBox()
        self.scope.addItems(("全部", "仅自选个股"))
        self.scope.setEnabled(False)
        row.addWidget(self.scope)
        self.search = QLineEdit()
        self.search.setPlaceholderText("筛选名称／代码（本地筛选，不请求API）")
        row.addWidget(self.search, 1)
        self.limit = QComboBox()
        for count in (100, 200, 500):
            self.limit.addItem(f"成交额前{count}", count)
        self.limit.setCurrentIndex(1)
        row.addWidget(self.limit)
        self.refresh_button = QPushButton("刷新")
        row.addWidget(self.refresh_button)
        box.addLayout(row)
        self.status = QLabel("正在读取热力图…")
        self.status.setWordWrap(True)
        box.addWidget(self.status)
        self.canvas = HeatmapCanvas()
        box.addWidget(self.canvas, 1)
        legend = QLabel(
            "面积＝所示标的成交额；红涨绿跌，灰色缺失。板块可能交叠，不代表互斥市场份额；点击个股可切换主图。"
        )
        legend.setWordWrap(True)
        legend.setObjectName("eyebrow")
        box.addWidget(legend)
        self.runner = runner_factory(self)
        self.runner.ready.connect(self._ready)
        self.runner.failed.connect(self._failed)
        self.kind.currentIndexChanged.connect(self.load)
        self.refresh_button.clicked.connect(lambda: self.load(refresh=True))
        self.scope.currentIndexChanged.connect(self._filter)
        self.search.textChanged.connect(self._filter)
        self.limit.currentIndexChanged.connect(self._filter)
        self.canvas.open_requested.connect(self.open_requested)

    def load(self, unused=None, *, refresh=False):
        kind = self.kind.currentData()
        self.scope.setEnabled(kind == "stocks")
        if getattr(self.market, "supports_heatmap", False) is not True:
            self.status.setText("热力图需要猫爪数据源；可在主窗口设置中切换。")
            return
        if self.snapshot is None or self.snapshot.kind != kind:
            self.snapshot = None
            self.canvas.set_tiles(())
        self.status.setText("正在加载；已有画面保留，优先读取缓存…")
        self.runner.submit(kind, None, partial(self.market.load_heatmap, kind, refresh=refresh))

    def _ready(self, snapshot):
        if snapshot.kind != self.kind.currentData():
            return
        self.snapshot = snapshot
        self._filter()

    def _filter(self, unused=None):
        if self.snapshot is None:
            return
        query = self.search.text().strip().lower()
        rows = tuple(
            t
            for t in self.snapshot.tiles
            if (not query or query in (t.code + t.name).lower())
            and (
                self.snapshot.kind != "stocks"
                or self.scope.currentIndex() == 0
                or t.symbol in self.watchlist
            )
        )
        self.canvas.set_tiles(rows, self.limit.currentData())
        self.status.setText(
            f"{self.snapshot.source} · {self.snapshot.day:%Y-%m-%d} · 取得于{self.snapshot.retrieved_at.astimezone(SHANGHAI):%H:%M:%S}"
            + (" · 缓存" if self.snapshot.cached else "")
            + f" · 源{len(self.snapshot.tiles)}项，筛选{len(rows)}项，绘制{len(self.canvas.rects)}项\n{self.snapshot.notice}"
        )

    def _failed(self, message):
        self.status.setText("加载未完成，保留已有画面：" + message)

    def closeEvent(self, event):
        self.runner.shutdown()
        QToolTip.hideText()
        super().closeEvent(event)

    def reject(self):
        self.runner.shutdown()
        super().reject()


class MoneyFlowPanel(QWidget):
    requested = Signal(bool)

    def __init__(self, parent=None):
        super().__init__(parent)
        self.snapshot = None
        box = QVBoxLayout(self)
        row = QHBoxLayout()
        self.scope = QComboBox()
        self.scope.addItems(("当前证券", "沪深大盘"))
        row.addWidget(self.scope, 1)
        self.refresh_button = QPushButton("刷新资金")
        row.addWidget(self.refresh_button)
        box.addLayout(row)
        self.caption = QLabel()
        self.caption.setWordWrap(True)
        box.addWidget(self.caption)
        self.table = QTableWidget(0, 4)
        self.table.setHorizontalHeaderLabels(("分类", "流入", "流出", "净额"))
        self.table.verticalHeader().hide()
        self.table.horizontalHeader().setSectionResizeMode(QHeaderView.ResizeMode.Stretch)
        self.table.setEditTriggers(QTableWidget.EditTrigger.NoEditTriggers)
        self.table.setSelectionMode(QTableWidget.SelectionMode.NoSelection)
        self.table.setMinimumHeight(285)
        box.addWidget(self.table)
        self.notice = QLabel("按成交规模分类，不能识别真实机构或散户账户。")
        self.notice.setWordWrap(True)
        self.notice.setObjectName("eyebrow")
        box.addWidget(self.notice)
        box.addStretch()
        self.scope.currentIndexChanged.connect(self._scope_changed)
        self.refresh_button.clicked.connect(lambda: self.requested.emit(True))
        self.clear()

    def _scope_changed(self):
        self.clear()
        self.requested.emit(False)

    def clear(self, message="打开本页后读取资金分类；不消耗猫爪高频接口额度。"):
        self.snapshot = None
        self.caption.setText(message)
        self.table.setRowCount(0)

    def render(self, snapshot):
        self.snapshot = snapshot
        self.caption.setText(
            f"{snapshot.name} · {snapshot.day:%Y-%m-%d}\n{snapshot.source} · {snapshot.scope}\n数据{snapshot.observed_at.astimezone(SHANGHAI):%H:%M:%S} · 取得{snapshot.retrieved_at.astimezone(SHANGHAI):%H:%M:%S}"
            + (" · 缓存" if snapshot.cached else "")
        )
        self.table.setRowCount(len(snapshot.buckets))
        for i, bucket in enumerate(snapshot.buckets):
            for j, text in enumerate(
                (
                    bucket.label.split("（")[0],
                    value_text(bucket.inflow, "元"),
                    value_text(bucket.outflow, "元"),
                    value_text(bucket.net, "元"),
                )
            ):
                item = self.table.item(i, j)
                if item is None:
                    item = QTableWidgetItem()
                    self.table.setItem(i, j, item)
                item.setText(text)
                item.setToolTip(bucket.label + " · " + snapshot.source)
                if j == 3:
                    item.setForeground(
                        QColor(
                            theme.MUTED
                            if bucket.net is None
                            else theme.UP
                            if bucket.net >= 0
                            else theme.DOWN
                        )
                    )
        self.notice.setText(snapshot.notice)
