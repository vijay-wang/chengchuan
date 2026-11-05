"""Native market heatmap and money-flow views; no vendor or database access."""

from dataclasses import replace
from functools import partial

from PySide6.QtCore import QEvent, QRectF, Qt, Signal
from PySide6.QtGui import QColor, QFont, QPainter
from PySide6.QtWidgets import (
    QComboBox,
    QHBoxLayout,
    QHeaderView,
    QLabel,
    QLineEdit,
    QPushButton,
    QScrollArea,
    QStackedWidget,
    QTableView,
    QTableWidget,
    QTableWidgetItem,
    QTabWidget,
    QToolTip,
    QVBoxLayout,
    QWidget,
)

from chengchuan.application.market_views import layout_tiles
from chengchuan.application.memory_cache import MemoryCache
from chengchuan.domain.models import SHANGHAI
from chengchuan.domain.sectors import SectorSnapshot
from chengchuan.ui import theme
from chengchuan.ui.market_dashboard import value_text
from chengchuan.ui.microstructure import OrderBandsPanel
from chengchuan.ui.table import ValueTableModel
from chengchuan.ui.tasks import ProcessTaskRunner
from chengchuan.ui.window_geometry import MaximizableDialog


class HeatmapCanvas(QWidget):
    open_requested = Signal(str)
    sector_requested = Signal(str)
    zoom_requested = Signal(int)
    tile_hovered = Signal(str)

    def __init__(self, parent=None):
        super().__init__(parent)
        self.setMinimumSize(360, 240)
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
            if rect.width() >= 32 and rect.height() >= 18:
                painter.save()
                font = QFont(painter.font())
                font.setPointSize(9 if rect.width() < 75 or rect.height() < 40 else 11)
                painter.setFont(font)
                painter.setPen(QColor("#edf5fc"))
                name = painter.fontMetrics().elidedText(
                    item.tile.name, Qt.TextElideMode.ElideRight, int(rect.width() - 8)
                )
                text = name
                if rect.height() >= 34:
                    text += "\n" + (f"{change:+.2f}%" if change is not None else "—")
                painter.drawText(rect.adjusted(4, 2, -4, -2), Qt.AlignmentFlag.AlignCenter, text)
                painter.restore()
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
            self.tile_hovered.emit(
                f"{tile.name} · {tile.code} · {change} · 成交额{value_text(tile.amount_yuan, '元')}"
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
        elif event.button() == Qt.MouseButton.LeftButton and tile:
            self.sector_requested.emit(tile.code)
        super().mouseReleaseEvent(event)

    def wheelEvent(self, event):
        if event.modifiers() & Qt.KeyboardModifier.ControlModifier:
            self.zoom_requested.emit(1 if event.angleDelta().y() > 0 else -1)
            event.accept()
        else:
            event.ignore()


class HeatmapDialog(MaximizableDialog):
    open_requested = Signal(str)

    def __init__(
        self, market, watchlist=(), runner_factory=ProcessTaskRunner, parent=None, cache=None
    ):
        super().__init__(parent)
        self.market, self.watchlist = market, frozenset(watchlist)
        self.cache = cache if cache is not None else MemoryCache(capacity=24, max_weight=40000)
        self.snapshot = None
        self.sector = None
        self._sector_requested = None
        self._zoom = 1.0
        self.setWindowTitle("澄川 · 市场热力图")
        self.setWindowFlags(
            self.windowFlags()
            | Qt.WindowType.WindowMaximizeButtonHint
            | Qt.WindowType.WindowMinimizeButtonHint
        )
        self.setSizeGripEnabled(True)
        self.setMinimumSize(720, 480)
        self.resize(1180, 760)
        self.setAttribute(Qt.WidgetAttribute.WA_DeleteOnClose)
        box = QVBoxLayout(self)
        tools = QHBoxLayout()
        self.back = QPushButton("← 返回热力图")
        self.back.hide()
        self.back.clicked.connect(self._back)
        tools.addWidget(self.back)
        self.maximize_button = QPushButton("最大化／还原")
        self.maximize_button.clicked.connect(
            lambda: self.showNormal() if self.isMaximized() else self.showMaximized()
        )
        tools.addWidget(self.maximize_button)
        self.list_button = QPushButton("列表视图")
        self.list_button.setCheckable(True)
        self.list_button.toggled.connect(self._filter)
        tools.addWidget(self.list_button)
        for text, direction in (("缩小", -1), ("放大", 1), ("适应", 0)):
            button = QPushButton(text)
            button.clicked.connect(lambda checked=False, d=direction: self._change_zoom(d))
            tools.addWidget(button)
        tools.addStretch()
        box.addLayout(tools)
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
        self.map_scroll = QScrollArea()
        self.map_scroll.setWidget(self.canvas)
        self.map_scroll.setWidgetResizable(True)
        self.map_scroll.viewport().installEventFilter(self)
        self.pages = QStackedWidget()
        self.pages.addWidget(self.map_scroll)
        self.list_view = QTableView()
        self.list_model = ValueTableModel(
            ("代码", "名称", "涨跌幅", "成交额"),
            {2: lambda v: f"{v:+.2f}%", 3: lambda v: value_text(v, "元")},
            (2,),
            parent=self,
        )
        self.list_view.setModel(self.list_model)
        self.list_view.horizontalHeader().setSectionResizeMode(QHeaderView.ResizeMode.Stretch)
        self.list_view.doubleClicked.connect(self._list_open)
        self.pages.addWidget(self.list_view)
        self.member_view = QTableView()
        self.member_model = ValueTableModel(
            ("代码", "名称", "最新价", "涨跌幅", "主力净流入"),
            {
                0: lambda v: v[-6:],
                2: lambda v: f"{v:.2f}",
                3: lambda v: f"{v:+.2f}%",
                4: lambda v: value_text(v, "元"),
            },
            (3, 4),
            secondary=4,
            parent=self,
        )
        self.member_view.setModel(self.member_model)
        self.member_view.setSortingEnabled(True)
        self.member_view.horizontalHeader().setSectionResizeMode(QHeaderView.ResizeMode.Stretch)
        self.member_view.horizontalHeader().setSortIndicator(3, Qt.SortOrder.DescendingOrder)
        self.member_view.doubleClicked.connect(
            lambda index: self._activate_stock(self.member_model.rows[index.row()][0])
        )
        self.pages.addWidget(self.member_view)
        box.addWidget(self.pages, 1)
        self.detail = QLabel(
            "Ctrl+滚轮或放大按钮缩放；完整名称可在列表视图查看；点击板块查看成份股。"
        )
        self.detail.setWordWrap(True)
        box.addWidget(self.detail)
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
        self.canvas.open_requested.connect(self._activate_stock)
        self.canvas.sector_requested.connect(self._open_sector)
        self.canvas.zoom_requested.connect(self._change_zoom)
        self.canvas.tile_hovered.connect(self.detail.setText)

    def _activate_stock(self, symbol):
        self.open_requested.emit(symbol)

    def _list_open(self, index):
        row = self.list_model.rows[index.row()]
        if self.snapshot.kind == "stocks":
            tile = next(t for t in self.snapshot.tiles if t.code == row[0])
            self._activate_stock(tile.symbol)
        else:
            self._open_sector(row[0])

    def eventFilter(self, watched, event):
        if getattr(self, "_closing", False):
            return False
        if (
            hasattr(self, "map_scroll")
            and watched is self.map_scroll.viewport()
            and event.type() == QEvent.Type.Resize
        ):
            self._resize_map()
        return super().eventFilter(watched, event)

    def _resize_map(self):
        if getattr(self, "_resizing_map", False):
            return
        self._resizing_map = True
        try:
            self._resize_map_content()
        finally:
            self._resizing_map = False

    def _resize_map_content(self):
        normal = self._zoom == 1.0
        if self.map_scroll.widgetResizable() != normal:
            self.map_scroll.setWidgetResizable(normal)
        policy = (
            Qt.ScrollBarPolicy.ScrollBarAsNeeded if normal else Qt.ScrollBarPolicy.ScrollBarAlwaysOn
        )
        if self.map_scroll.horizontalScrollBarPolicy() != policy:
            self.map_scroll.setHorizontalScrollBarPolicy(policy)
        if self.map_scroll.verticalScrollBarPolicy() != policy:
            self.map_scroll.setVerticalScrollBarPolicy(policy)
        if normal:
            return
        size = self.map_scroll.viewport().size()
        self.canvas.resize(
            max(360, int(size.width() * self._zoom)), max(240, int(size.height() * self._zoom))
        )

    def _change_zoom(self, direction):
        self._zoom = (
            1.0
            if direction == 0
            else min(4.0, max(1.0, self._zoom * (1.3 if direction > 0 else 1 / 1.3)))
        )
        self._resize_map()

    def _open_sector(self, code, refresh=False):
        if self.snapshot is None or self.snapshot.kind == "stocks":
            return
        if getattr(self.market, "supports_sector", False) is not True:
            self.status.setText("当前来源没有板块成份接口。")
            return
        tile = next(t for t in self.snapshot.tiles if t.code == code)
        self._sector_requested = (self.snapshot.kind, code)
        key = ("sector", self.snapshot.kind, code, self.snapshot.day)
        cached = self.cache.get(key, max_age=30) if not refresh else None
        if cached is not None:
            self.runner.cancel()
            self._ready(replace(cached, cached=True), remember=False)
            return
        self.status.setText("正在获取" + tile.name + "成份；复用同日行情和资金缓存…")
        self.runner.submit(
            "sector:" + code,
            None,
            partial(
                self.market.load_sector,
                self.snapshot.kind,
                code,
                tile.name,
                **({"refresh": True} if refresh else {}),
            ),
        )

    def _back(self):
        self.sector = None
        self._sector_requested = None
        self.runner.cancel()
        self.back.hide()
        self.search.clear()
        self._filter()

    def load(self, unused=None, *, refresh=False):
        if self.sector is not None and refresh:
            self._open_sector(self.sector.code, refresh=True)
            return
        self.sector = None
        self._sector_requested = None
        self.back.hide()
        kind = self.kind.currentData()
        self.scope.setEnabled(kind == "stocks")
        if getattr(self.market, "supports_heatmap", False) is not True:
            self.status.setText("热力图需要猫爪数据源；可在主窗口设置中切换。")
            return
        if self.snapshot is None or self.snapshot.kind != kind:
            self.snapshot = None
            self.canvas.set_tiles(())
        cached = self.cache.get(("heatmap", kind), max_age=30) if not refresh else None
        if cached is not None:
            self.runner.cancel()
            self._ready(replace(cached, cached=True), remember=False)
            return
        self.status.setText("正在加载；已有画面保留，优先读取缓存…")
        self.runner.submit(kind, None, partial(self.market.load_heatmap, kind, refresh=refresh))

    def _ready(self, snapshot, remember=True):
        if isinstance(snapshot, SectorSnapshot):
            if (snapshot.kind, snapshot.code) != self._sector_requested:
                return
            self.sector = snapshot
            if remember and snapshot.complete:
                self.cache.put(
                    ("sector", snapshot.kind, snapshot.code, self.snapshot.day),
                    snapshot,
                    len(snapshot.members),
                )
            self.back.show()
            self._filter()
            return
        if snapshot.kind != self.kind.currentData():
            return
        self.snapshot = snapshot
        if remember and snapshot.complete:
            self.cache.put(("heatmap", snapshot.kind), snapshot, len(snapshot.tiles))
        self._filter()

    def _filter(self, unused=None):
        if self.sector is not None:
            query = self.search.text().strip().lower()
            rows = [
                (m.symbol, m.name, m.price, m.change_pct, m.main_net_yuan)
                for m in self.sector.members
                if not query or query in (m.symbol + m.name).lower()
            ]
            self.member_model.set_rows(rows)
            self.member_model.sort(3, Qt.SortOrder.DescendingOrder)
            self.pages.setCurrentWidget(self.member_view)
            self.status.setText(
                f"{self.sector.name} · {self.sector.day:%Y-%m-%d} · {len(rows)}/{len(self.sector.members)}只\n{self.sector.notice}"
            )
            return
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
        self.list_model.set_rows((t.code, t.name, t.change_pct, t.amount_yuan) for t in rows)
        self.pages.setCurrentWidget(
            self.list_view if self.list_button.isChecked() else self.map_scroll
        )
        self._resize_map()
        self.status.setText(
            f"{self.snapshot.source} · {self.snapshot.day:%Y-%m-%d} · 取得于{self.snapshot.retrieved_at.astimezone(SHANGHAI):%H:%M:%S}"
            + (" · 缓存" if self.snapshot.cached else "")
            + f" · 源{len(self.snapshot.tiles)}项，筛选{len(rows)}项，绘制{len(self.canvas.rects)}项\n{self.snapshot.notice}"
        )

    def _failed(self, message):
        self.status.setText("加载未完成，保留已有画面：" + message)

    def closeEvent(self, event):
        self._closing = True
        self.map_scroll.viewport().removeEventFilter(self)
        self.runner.shutdown()
        QToolTip.hideText()
        super().closeEvent(event)

    def reject(self):
        self._closing = True
        self.map_scroll.viewport().removeEventFilter(self)
        self.runner.shutdown()
        super().reject()


class MoneyFlowPanel(QWidget):
    requested = Signal(bool)

    def __init__(self, parent=None):
        super().__init__(parent)
        self.snapshot = None
        outer = QVBoxLayout(self)
        self.pages = QTabWidget()
        outer.addWidget(self.pages)
        public = QWidget()
        box = QVBoxLayout(public)
        self.pages.addTab(public, "公开分类")
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
        self.order_bands = OrderBandsPanel(compact=True)
        self.pages.addTab(self.order_bands, "原始委托分档")
        self.scope.currentIndexChanged.connect(self._scope_changed)
        self.refresh_button.clicked.connect(lambda: self.requested.emit(True))
        self.clear()

    def _scope_changed(self):
        self.snapshot = None
        self.caption.setText("正在读取公开资金分类…")
        self.table.setRowCount(0)
        self.requested.emit(False)

    def clear(self, message="打开本页后读取资金分类；不消耗猫爪高频接口额度。"):
        self.snapshot = None
        self.caption.setText(message)
        self.table.setRowCount(0)
        self.order_bands.clear()

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
