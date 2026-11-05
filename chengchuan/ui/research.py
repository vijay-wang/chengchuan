"""Embedded market research workspace; virtual tables and cancellable network tasks."""

from dataclasses import replace
from functools import partial

import pyqtgraph as pg
from PySide6.QtCore import QDate, Qt, QTime, Signal
from PySide6.QtGui import QTextOption
from PySide6.QtWidgets import (
    QAbstractItemView,
    QButtonGroup,
    QCheckBox,
    QComboBox,
    QDateEdit,
    QFileDialog,
    QHBoxLayout,
    QHeaderView,
    QLabel,
    QLineEdit,
    QPushButton,
    QSplitter,
    QTableView,
    QTabWidget,
    QTextBrowser,
    QTimeEdit,
    QVBoxLayout,
    QWidget,
)

from chengchuan.application.errors import StorageError
from chengchuan.application.research import MODULES, clock_seconds, export_research, numeric
from chengchuan.domain.research import ResearchRequest
from chengchuan.ui import theme
from chengchuan.ui.evidence import evidence_text
from chengchuan.ui.table import ValueTableModel
from chengchuan.ui.tasks import ProcessTaskRunner

COLORS = (theme.ACCENT, "#8daaf2", "#efb778", "#db8ac8", "#84c8ee")
METRICS = {
    "pct_chg": "涨跌幅（%）",
    "strength": "供应商强度",
    "main_net_amount": "累计净额（元）",
    "net_5m": "5分钟净额变化（元）",
    "net_15m": "15分钟净额变化（元）",
    "fd_amount": "封单额（元）",
    "m_price": "撮合价（元）",
    "auc_vol": "匹配量（股）",
    "um_vol": "未匹配量（股）",
    "um_signed_shares": "未匹配量：买正卖负（股）",
    "auc_amt": "匹配金额（元）",
}


def _label(text, name="muted"):
    widget = QLabel(text)
    widget.setObjectName(name)
    widget.setWordWrap(True)
    return widget


def _display(value):
    if isinstance(value, bool):
        return "是" if value else "否"
    if isinstance(value, float):
        return f"{value:,.3f}".rstrip("0").rstrip(".")
    return str(value)


def _display_column(key, value, table_key=""):
    labels = {
        "side": {0: "买入榜", 1: "卖出榜"},
        "um_side": {"b": "买方", "s": "卖方"},
        "type": {
            "u": "涨停",
            "d": "跌停",
            "ub": "涨停炸板",
            "db": "跌停炸板",
            "bu": "大涨",
            "bd": "大跌",
        },
        "status": {"triggered": "已触发（计算）", "reachable": "未触发（计算）"},
    }
    enum_scope = (
        key == "side"
        and (table_key == "seats" or table_key.startswith("seat_"))
        or key == "um_side"
        and table_key.startswith("auction_ticks")
        or key == "type"
        and table_key in ("pool", "yesterday")
        or key == "status"
        and table_key == "anomaly"
    )
    if enum_scope and value in labels.get(key, {}):
        return labels[key][value]
    text = str(value)
    if (
        key
        in (
            "tradedate",
            "source_day",
            "report_date",
            "announce_date",
            "declareDate",
            "endDate",
            "m_timetag",
            "m_anntime",
        )
        and text.isdigit()
        and len(text) == 8
    ):
        return f"{text[:4]}-{text[4:6]}-{text[6:]}"
    if key in ("trademin", "window_start", "window_end") and text.isdigit() and len(text) == 4:
        return text[:2] + ":" + text[2:]
    return _display(value)


class ClockAxis(pg.AxisItem):
    def tickStrings(self, values, scale, spacing):
        return [
            f"{int(v) // 3600:02}:{int(v) % 3600 // 60:02}" if 0 <= v < 86400 else ""
            for v in values
        ]


class ResearchDataView(QTableView):
    def __init__(self, table, parent=None, natural_order=False):
        super().__init__(parent)
        self.natural_order = natural_order
        document_layout = QVBoxLayout(self.viewport())
        document_layout.setContentsMargins(0, 0, 0, 0)
        self.document_view = QTextBrowser()
        self.document_view.setOpenExternalLinks(False)
        self.document_view.setWordWrapMode(QTextOption.WrapMode.WrapAtWordBoundaryOrAnywhere)
        self.document_view.setHorizontalScrollBarPolicy(Qt.ScrollBarPolicy.ScrollBarAlwaysOff)
        document_layout.addWidget(self.document_view)
        self.setAlternatingRowColors(True)
        self.setSelectionBehavior(QAbstractItemView.SelectionBehavior.SelectRows)
        self.setSelectionMode(QAbstractItemView.SelectionMode.SingleSelection)
        self.setEditTriggers(QAbstractItemView.EditTrigger.NoEditTriggers)
        self.verticalHeader().hide()
        self.horizontalHeader().setSectionResizeMode(QHeaderView.ResizeMode.Interactive)
        self.horizontalHeader().setStretchLastSection(True)
        self.setSortingEnabled(True)
        self.update_table(table)

    def update_table(self, table, text=""):
        self.table = table
        self.document_view.setPlainText(table.document)
        self.document_view.setVisible(bool(table.document) and not table.columns)
        signature = tuple((c.key, c.label, c.unit, c.description) for c in table.columns)
        new_model = getattr(self, "signature", None) != signature
        if new_model:
            old = self.model()
            colors = [
                i
                for i, c in enumerate(table.columns)
                if c.key in ("pct_chg", "pc", "main_net_amount", "net_buy", "net_amount")
            ]
            model = ValueTableModel(
                [c.caption for c in table.columns],
                formats={
                    i: partial(_display_column, c.key, table_key=table.key)
                    for i, c in enumerate(table.columns)
                },
                color_columns=colors,
                parent=self,
                header_tips={
                    i: c.key + "\n" + (c.description or c.caption)
                    for i, c in enumerate(table.columns)
                },
            )
            self.setModel(model)
            if self.natural_order:
                self.horizontalHeader().setSortIndicator(-1, Qt.SortOrder.AscendingOrder)
            if old:
                old.deleteLater()
            self.signature = signature
            for i, c in enumerate(table.columns):
                self.setColumnWidth(i, 230 if c.key in ("reason", "seat_name", "message") else 135)
        needle = text.strip().casefold()
        self.model().set_rows(
            r for r in table.rows if not needle or any(needle in str(v).casefold() for v in r)
        )
        # Stable, numeric sort. Default constituent ranking matches the existing heatmap drill-down.
        keys = [c.key for c in table.columns]
        if new_model and "pct_chg" in keys and table.key in ("rotation", "stocks"):
            if "main_net_amount" in keys:
                self.model().secondary = keys.index("main_net_amount")
            self.sortByColumn(keys.index("pct_chg"), Qt.SortOrder.DescendingOrder)
        else:
            self.model().sort(
                self.horizontalHeader().sortIndicatorSection(),
                self.horizontalHeader().sortIndicatorOrder(),
            )

    def record(self, row):
        if 0 <= row < len(self.model().rows):
            return dict(zip((c.key for c in self.table.columns), self.model().rows[row]))
        return {}


class ResearchWorkspace(QWidget):
    back_requested = Signal()
    stock_requested = Signal(str, str, str)

    def __init__(self, service=None, workspace=None, runner_factory=None, parent=None):
        super().__init__(parent)
        self.service, self.workspace = service, workspace
        self.module, self.bundle, self.active_request = "rotation", None, None
        self.views, self.history = {}, []
        self._context = 0
        self.runner = (
            runner_factory(self)
            if runner_factory
            else ProcessTaskRunner(self, timeout_ms=120000, task_label="市场研究")
        )
        self.runner.ready.connect(self._ready)
        self.runner.failed.connect(self._failed)
        self.runner.busy.connect(self._busy)
        root = QVBoxLayout(self)
        root.setContentsMargins(22, 18, 22, 12)
        header = QHBoxLayout()
        back = QPushButton("‹  返回行情")
        back.clicked.connect(self.back_requested)
        header.addWidget(back)
        header.addWidget(_label("市场研究", "stockTitle"))
        header.addStretch()
        self.pins = QComboBox()
        self.pins.setMaximumWidth(220)
        self.pins.activated.connect(self._load_pin)
        header.addWidget(self.pins)
        self.pin = QPushButton("☆ 固定当前观察")
        self.pin.clicked.connect(self._pin)
        header.addWidget(self.pin)
        self.export = QPushButton("导出研究卡")
        self.export.clicked.connect(self._export)
        header.addWidget(self.export)
        root.addLayout(header)
        nav = QHBoxLayout()
        self.nav = QButtonGroup(self)
        self.nav_buttons = {}
        for key, title in MODULES:
            button = QPushButton(title)
            button.setCheckable(True)
            button.setChecked(key == self.module)
            self.nav.addButton(button)
            button.clicked.connect(partial(self.select_module, key))
            self.nav_buttons[key] = button
            nav.addWidget(button)
        root.addLayout(nav)
        controls = QHBoxLayout()
        self.symbol = QLineEdit()
        self.symbol.setPlaceholderText("股票代码（留空看全市场）")
        self.symbol.setMaximumWidth(220)
        controls.addWidget(self.symbol)
        self.date = QDateEdit(QDate.currentDate())
        self.date.setCalendarPopup(True)
        self.date.setDisplayFormat("yyyy-MM-dd")
        self.date.setMaximumDate(QDate.currentDate())
        self.date.dateChanged.connect(lambda _: self.latest.setChecked(False))
        controls.addWidget(self.date)
        self.latest = QCheckBox("最新可用")
        self.latest.setChecked(True)
        self.latest.setToolTip(
            "勾选读取源最新业务日；取消按日查询。仅最新接口改读当日之前已采集的本机版本。"
        )
        controls.addWidget(self.latest)
        self.level = QComboBox()
        self.level.addItem("精选一级", "parent")
        self.level.addItem("精选二级", "child")
        controls.addWidget(self.level)
        self.platform = QComboBox()
        for title, code in (
            ("通达信热搜", "tdx&resou"),
            ("雪球热搜", "xq&resou"),
            ("东方财富人气", "dfcf&renqi"),
            ("同花顺热股", "ths&normal"),
        ):
            self.platform.addItem(title, code)
        controls.addWidget(self.platform)
        self.refresh = QPushButton("查询／刷新")
        self.refresh.setObjectName("primary")
        self.refresh.clicked.connect(self.refresh_current)
        self.symbol.returnPressed.connect(self.refresh_current)
        controls.addWidget(self.refresh)
        controls.addStretch()
        root.addLayout(controls)
        actions = QHBoxLayout()
        self.previous = QPushButton("‹ 上一研究视图")
        self.previous.clicked.connect(self._previous)
        actions.addWidget(self.previous)
        self.stock_open = QPushButton("打开所选股票行情")
        self.stock_open.clicked.connect(self._open_selected_stock)
        actions.addWidget(self.stock_open)
        self.extra = QPushButton()
        self.extra.clicked.connect(self._extra)
        actions.addWidget(self.extra)
        self.source = QComboBox()
        self.source.addItem("选股宝源内代码", "xgb")
        self.source.addItem("韭菜公社源内代码", "jygs")
        actions.addWidget(self.source)
        self.reason_code = QLineEdit()
        self.reason_code.setPlaceholderText("源内板块代码；留空看该来源列表")
        actions.addWidget(self.reason_code)
        self.reason = QPushButton("查三方说明")
        self.reason.clicked.connect(self._reason)
        actions.addWidget(self.reason)
        self.filter = QLineEdit()
        self.filter.setPlaceholderText("本地过滤当前表…")
        self.filter.textChanged.connect(self._filter)
        actions.addWidget(self.filter, 1)
        root.addLayout(actions)
        split = QSplitter(Qt.Orientation.Horizontal)
        left = QWidget()
        data_layout = QVBoxLayout(left)
        data_layout.setContentsMargins(0, 0, 0, 0)
        self.tabs = QTabWidget()
        self.tabs.setDocumentMode(True)
        self.tabs.currentChanged.connect(self._tab_changed)
        data_layout.addWidget(self.tabs, 3)
        curve_controls = QHBoxLayout()
        self.metric_box = QComboBox()
        self.metric_box.currentIndexChanged.connect(self._plot)
        curve_controls.addWidget(_label("研究曲线"))
        curve_controls.addWidget(self.metric_box)
        self.cutoff_toggle = QCheckBox("曲线截至")
        self.cutoff_time = QTimeEdit(QTime(15, 0))
        self.cutoff_time.setDisplayFormat("HH:mm")
        self.cutoff_time.setEnabled(False)
        self.cutoff_toggle.toggled.connect(self.cutoff_time.setEnabled)
        self.cutoff_toggle.setToolTip(
            "点击查询生效；只过滤曲线时间，不代表该时点已发布的回测数据。"
        )
        curve_controls.addWidget(self.cutoff_toggle)
        curve_controls.addWidget(self.cutoff_time)
        self.range_toggle = QCheckBox("拖选区间统计")
        self.range_toggle.toggled.connect(lambda checked: self.region.setVisible(checked))
        curve_controls.addWidget(self.range_toggle)
        self.range_summary = _label("")
        curve_controls.addWidget(self.range_summary, 1)
        self.curve_area = QWidget()
        curve_layout = QVBoxLayout(self.curve_area)
        curve_layout.setContentsMargins(0, 0, 0, 0)
        curve_layout.addLayout(curve_controls)
        self.plot = pg.PlotWidget(axisItems={"bottom": ClockAxis(orientation="bottom")})
        self.plot.setBackground(theme.BACKGROUND)
        self.plot.getAxis("bottom").setTickSpacing(major=1800, minor=600)
        self.plot.showGrid(x=True, y=True, alpha=0.16)
        self.plot.addLegend(offset=(8, 5))
        self.plot.setMinimumHeight(200)
        self.curves = []
        self.region = pg.LinearRegionItem((34200, 36000), brush=(90, 215, 191, 25))
        self.plot.addItem(self.region)
        self.region.hide()
        self.region.sigRegionChanged.connect(self._range_changed)
        curve_layout.addWidget(self.plot, 1)
        data_layout.addWidget(self.curve_area, 2)
        split.addWidget(left)
        self.evidence = QTextBrowser()
        self.evidence.setOpenExternalLinks(False)
        self.evidence.setWordWrapMode(QTextOption.WrapMode.WrapAtWordBoundaryOrAnywhere)
        self.evidence.setHorizontalScrollBarPolicy(Qt.ScrollBarPolicy.ScrollBarAlwaysOff)
        self.evidence.setMinimumWidth(270)
        split.addWidget(self.evidence)
        split.setSizes([950, 330])
        root.addWidget(split, 1)
        self.status = _label(
            "点击查询加载；不启动后台轮询。表头可排序，双击下钻，打开行情保留研究视图。"
        )
        root.addWidget(self.status)
        self.symbol.textEdited.connect(self._controls_changed)
        self.date.dateChanged.connect(self._controls_changed)
        self.latest.toggled.connect(self._controls_changed)
        self.level.currentIndexChanged.connect(self._controls_changed)
        self.platform.currentIndexChanged.connect(self._controls_changed)
        self.cutoff_toggle.toggled.connect(self._controls_changed)
        self.cutoff_time.timeChanged.connect(self._controls_changed)
        self._configure_controls()
        self.set_service(service)
        self._reload_pins()

    def set_service(self, service):
        self.runner.cancel()
        self._context += 1
        self.service, self.bundle, self.active_request = service, None, None
        self.history.clear()
        self.tabs.clear()
        for view in self.views.values():
            view.deleteLater()
        self.views.clear()
        self.refresh.setEnabled(service is not None)
        self.export.setEnabled(False)
        self.pin.setEnabled(False)
        self.evidence.setPlainText("选择模块后查询；鼠标移动和本地筛选不会访问API。")
        self._plot()
        if service is None:
            self.status.setText("特色研究需要猫爪数据源；请在设置中配置API Key并切换到猫爪。")
        else:
            self.status.setText("研究数据源已就绪；点击查询加载，不启动后台轮询。")

    def enter(self, symbol=""):
        if symbol and self.bundle is None:
            self.symbol.setText(symbol[-6:])
        if self.bundle is None and self.service:
            self.refresh_current()

    def open_evidence(self, symbol=""):
        if symbol:
            self.symbol.setText(symbol[-6:])
        self.module = "evidence"
        self.nav_buttons[self.module].setChecked(True)
        self._configure_controls()
        self._submit(self._request())

    def _request(self, action="overview", code="", seat=""):
        return ResearchRequest(
            self.module,
            self.date.date().toString("yyyyMMdd"),
            self.latest.isChecked(),
            self.symbol.text().strip(),
            self.level.currentData(),
            code,
            action,
            self.platform.currentData(),
            self.source.currentData(),
            seat,
            self.cutoff_time.time().toString("HHmmss") if self.cutoff_toggle.isChecked() else "",
        )

    def select_module(self, key, *_):
        if key == self.module and self.bundle:
            return
        self.runner.cancel()
        self._context += 1
        self.module = key
        self.nav_buttons[key].setChecked(True)
        self.active_request = None
        self._configure_controls()
        if self.service:
            self._submit(self._request())

    def _configure_controls(self):
        self.level.setVisible(self.module == "rotation")
        self.platform.setVisible(self.module == "attention")
        for widget in (self.source, self.reason_code, self.reason):
            widget.setVisible(self.module == "evidence")
        self.extra.setVisible(self.module in ("auction", "seats", "sentiment", "attention"))
        self.extra.setText(
            {
                "auction": "板块竞价分组",
                "seats": "公开关联标签",
                "sentiment": "查看该股封单时间线",
                "attention": "四平台共现对比",
            }.get(self.module, "")
        )

    def refresh_current(self):
        request = self._request()
        old = self.active_request
        if old and old.module == request.module and old.level == request.level:
            request = replace(request, action=old.action, code=old.code, seat=old.seat)
        self._submit(request)

    def _controls_changed(self, *_):
        if self.service is not None:
            self.runner.cancel()
            self.status.setText("查询条件已改变；点击查询更新。当前表格仍为上次研究结果。")

    def _submit(self, request):
        if not self.service:
            return
        self.runner.cancel()
        self._context += 1
        self.active_request = request
        self.status.setText("正在后台查询；已有表格保持显示，可切换模块取消。")
        self.runner.submit(str(self._context), None, partial(self.service.run, request))

    def _ready(self, bundle):
        if bundle.request != self.active_request:
            return
        if self.bundle and self.bundle.request != bundle.request:
            self.history.append(self.bundle)
            self.history = self.history[-8:]
        self.render(bundle)

    def render(self, bundle):
        self.bundle = bundle
        self.module = bundle.request.module
        self.nav_buttons[self.module].setChecked(True)
        self._configure_controls()
        selected = self.current_view().table.key if self.current_view() else ""
        keys = {t.key for t in bundle.tables}
        self.tabs.blockSignals(True)
        for i in reversed(range(self.tabs.count())):
            if self.tabs.widget(i).table.key not in keys:
                self.tabs.removeTab(i)
        for table in bundle.tables:
            view = self.views.get(table.key)
            if view is None:
                view = ResearchDataView(table)
                view.doubleClicked.connect(partial(self._activate, view))
                view.clicked.connect(lambda index, v=view: self._describe(v, index.row()))
                self.views[table.key] = view
            else:
                view.update_table(table, self.filter.text())
            i = self.tabs.indexOf(view)
            if i < 0:
                i = self.tabs.addTab(view, table.title)
            self.tabs.setTabText(i, table.title)
            self.tabs.setTabToolTip(i, table.notice or table.meta.coverage)
            if table.key == selected:
                self.tabs.setCurrentIndex(i)
        # Bound hidden views across date/stock changes.
        for key in tuple(self.views):
            if key not in keys:
                self.views.pop(key).deleteLater()
        self.tabs.blockSignals(False)
        self.export.setEnabled(True)
        self.pin.setEnabled(True)
        self.previous.setEnabled(bool(self.history))
        self.status.setText(" · ".join(bundle.notes))
        self._tab_changed()

    def current_view(self):
        return self.tabs.currentWidget()

    def _tab_changed(self, *_):
        view = self.current_view()
        self.metric_box.blockSignals(True)
        previous = self.metric_box.currentData()
        self.metric_box.clear()
        if view:
            keys = {c.key for c in view.table.columns}
            for key, label in METRICS.items():
                if key in keys:
                    self.metric_box.addItem(label, key)
        index = self.metric_box.findData(previous)
        if index >= 0:
            self.metric_box.setCurrentIndex(index)
        self.metric_box.blockSignals(False)
        keys = {c.key for c in view.table.columns} if view else set()
        self.curve_area.setVisible(
            bool(keys.intersection(("time", "trademin"))) and self.metric_box.count() > 0
        )
        self._describe(view)
        self._plot()

    def _describe(self, view, row=None):
        if not view:
            return
        record = view.record(row) if row is not None else None
        self.evidence.setPlainText(evidence_text(view.table, record))
        self.evidence.horizontalScrollBar().setValue(0)
        self.evidence.verticalScrollBar().setValue(0)

    def _filter(self, text):
        view = self.current_view()
        if view:
            view.update_table(view.table, text)

    def _activate(self, view, index):
        row = view.record(index.row())
        key = view.table.key
        if key == "rotation":
            self._submit(
                replace(
                    self._request("detail", str(row.get("theme_symbol") or "")),
                    level=str(row.get("level") or self.level.currentData()),
                )
            )
        elif key == "library":
            self._submit(self._request("detail", str(row.get("theme_id") or "")))
        elif key in ("pool", "yesterday"):
            self.symbol.setText(str(row.get("symbol") or ""))
            self._submit(self._request("detail"))
        elif key == "seats" or key.startswith("seat_"):
            if row.get("tradedate"):
                self.date.setDate(QDate.fromString(str(row["tradedate"]), "yyyyMMdd"))
                self.latest.setChecked(False)
            self._submit(self._request("history", seat=str(row.get("seat_name") or "")))
        else:
            self._emit_stock(row)

    def _emit_stock(self, row):
        if self.current_view() and self.current_view().table.key == "reason":
            self.status.setText("此表symbol是三方板块代码，不能交给股票接口。可在右侧查看说明。")
            return
        symbol = str(row.get("symbol") or row.get("s") or "")
        if not symbol:
            self.status.setText("此行没有股票代码；题材代码只用于题材研究。")
            return
        view = self.current_view()
        source_day = view.table.meta.business_day if view else ""
        if len(source_day) != 8 or not source_day.isdigit():
            source_day = self.bundle.request.day if self.bundle else ""
        day = str(row.get("tradedate") or source_day)
        clock = str(row.get("servertime") or row.get("time") or "")
        self.stock_requested.emit(symbol, day, clock)

    def _open_selected_stock(self):
        view = self.current_view()
        indices = view.selectionModel().selectedRows() if view else ()
        if indices:
            self._emit_stock(view.record(indices[0].row()))
        elif self.symbol.text().strip():
            self._emit_stock(dict(symbol=self.symbol.text().strip()))

    def _extra(self):
        self._submit(
            self._request(
                {
                    "auction": "themes",
                    "seats": "aliases",
                    "sentiment": "detail",
                    "attention": "compare",
                }[self.module]
            )
        )

    def _reason(self):
        self._submit(self._request("reason", self.reason_code.text().strip()))

    def _previous(self):
        if not self.history:
            return
        self.runner.cancel()
        bundle = self.history.pop()
        self.active_request = bundle.request
        self.symbol.setText(bundle.request.symbol)
        self.date.setDate(QDate.fromString(bundle.request.day, "yyyyMMdd"))
        self.latest.setChecked(bundle.request.latest)
        self.level.setCurrentIndex(self.level.findData(bundle.request.level))
        self.platform.setCurrentIndex(self.platform.findData(bundle.request.platform))
        self.cutoff_toggle.setChecked(bool(bundle.request.cutoff))
        if bundle.request.cutoff:
            self.cutoff_time.setTime(QTime.fromString(bundle.request.cutoff, "HHmmss"))
        self.render(bundle)

    def _plot(self, *_):
        if not hasattr(self, "plot"):
            return
        view, metric = self.current_view(), self.metric_box.currentData()
        request = self.bundle.request if self.bundle else None
        signature = (request, view.table.key if view else None, metric)
        preserve = getattr(self, "_plot_signature", None) == signature
        old_range = self.plot.getViewBox().viewRange()
        self._plot_signature = signature
        source_tables = [view.table] if view else []
        if view and view.table.key.startswith("auction_ticks_") and self.bundle:
            source_tables = [t for t in self.bundle.tables if t.key.startswith("auction_ticks_")]
        series = []
        for table in source_tables:
            points = []
            for row in table.records():
                clock = clock_seconds(row.get("time") or row.get("trademin"))
                value = numeric(row.get(metric))
                if clock is not None and value is not None:
                    points.append((clock, value))
            if points:
                # NaNs split missing minute buckets and the lunch gap; no invented connecting data.
                points.sort()
                x, y, previous = [], [], None
                for clock, value in points:
                    if previous is not None and clock - previous > (
                        300 if table.key == "minute" else 120
                    ):
                        x.append(clock)
                        y.append(float("nan"))
                    x.append(clock)
                    y.append(value)
                    previous = clock
                series.append((table.title, x, y))
        while len(self.curves) < len(series):
            self.curves.append(
                self.plot.plot(pen=pg.mkPen(COLORS[len(self.curves) % len(COLORS)], width=1.6))
            )
        if series:
            clocks = [x for _, xs, _ in series for x in xs]
            self.plot.setLimits(
                xMin=min(clocks) - 30,
                xMax=max(clocks) + 30,
                maxXRange=max(clocks) - min(clocks) + 60,
                minXRange=30,
            )
        legend = self.plot.plotItem.legend
        legend.clear()
        for i, curve in enumerate(self.curves):
            if i < len(series):
                title, x, y = series[i]
                curve.setData(x, y, connect="finite")
                legend.addItem(curve, title)
            else:
                curve.setData([], [])
        self.plot.setTitle(
            METRICS.get(metric, "选择有时间字段的表格查看曲线"), color=theme.MUTED, size="11pt"
        )
        if preserve and series:
            self.plot.setRange(xRange=old_range[0], yRange=old_range[1], padding=0)
        else:
            self.plot.enableAutoRange()
        self._range_changed()

    def _range_changed(self):
        view = self.current_view()
        if not view:
            self.range_summary.setText("")
            return
        lo, hi = sorted(self.region.getRegion())
        metric = self.metric_box.currentData()
        points = sorted(
            (clock_seconds(r.get("time") or r.get("trademin")), numeric(r.get(metric)))
            for r in view.table.records()
            if clock_seconds(r.get("time") or r.get("trademin")) is not None
            and numeric(r.get(metric)) is not None
        )
        points = [(x, y) for x, y in points if lo <= x <= hi]
        self.range_summary.setText(
            f"{len(points)}个已知点；端点差 {points[-1][1] - points[0][1]:+,.2f}"
            if len(points) >= 2
            else "区间内至少需要两个已知点"
        )

    def _pin(self):
        if not self.workspace or not self.bundle:
            return
        try:
            pins = self.workspace.load("research_pins") or {}
            from dataclasses import asdict

            records = pins.get("items", [])
            record = asdict(self.bundle.request)
            if record not in records:
                records.append(record)
            self.workspace.save("research_pins", {"items": records[-100:]})
            self._reload_pins()
            self.status.setText("已固定到本机研究观察（最多100项），不触发后台订阅。")
        except (OSError, ValueError, StorageError) as exc:
            self.status.setText("观察保存失败：" + str(exc))

    def _reload_pins(self):
        self.pins.clear()
        self.pins.addItem("已固定的研究观察…", None)
        if self.workspace:
            try:
                records = (self.workspace.load("research_pins") or {}).get("items", [])
                for record in records[-100:]:
                    title = dict(MODULES).get(record.get("module"), "研究")
                    self.pins.addItem(
                        f"{title} · {record.get('code') or record.get('symbol') or '市场'}", record
                    )
            except (StorageError, ValueError, TypeError):
                self.status.setText("本机固定观察暂不可读")

    def _load_pin(self, index):
        record = self.pins.itemData(index)
        if not record:
            return
        try:
            request = ResearchRequest(**record)
            if request.module not in self.nav_buttons:
                raise ValueError("不支持的观察模块")
            self.module = request.module
            self.nav_buttons[self.module].setChecked(True)
            self._configure_controls()
            self.symbol.setText(request.symbol)
            self.date.setDate(QDate.fromString(request.day, "yyyyMMdd"))
            self.latest.setChecked(request.latest)
            self.level.setCurrentIndex(self.level.findData(request.level))
            self.platform.setCurrentIndex(self.platform.findData(request.platform))
            self.cutoff_toggle.setChecked(bool(request.cutoff))
            if request.cutoff:
                self.cutoff_time.setTime(QTime.fromString(request.cutoff, "HHmmss"))
            self._submit(request)
        except (TypeError, KeyError, ValueError):
            self.status.setText("该观察配置不可读，请重新固定")

    def _export(self):
        if not self.bundle:
            return
        path, _ = QFileDialog.getSaveFileName(
            self, "导出研究卡", "澄川研究卡.md", "Markdown (*.md);;JSON (*.json)"
        )
        if path:
            try:
                export_research(self.bundle, path)
                self.status.setText("研究卡已导出，包含来源、单位、采集时间和覆盖说明。")
            except (OSError, ValueError) as exc:
                self.status.setText("导出失败：" + str(exc))

    def _busy(self, busy):
        self.refresh.setText("查询中…可再次刷新" if busy else "查询／刷新")

    def _failed(self, message):
        self.status.setText("查询失败，保留已有研究视图：" + message)

    def shutdown(self):
        self.runner.shutdown()
