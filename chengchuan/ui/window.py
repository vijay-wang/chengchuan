from __future__ import annotations

from dataclasses import replace
from datetime import datetime
from functools import partial
from pathlib import Path

from PySide6.QtCore import Qt, QTimer
from PySide6.QtGui import QKeySequence, QShortcut
from PySide6.QtWidgets import (
    QCheckBox,
    QComboBox,
    QFileDialog,
    QFrame,
    QGridLayout,
    QHBoxLayout,
    QLabel,
    QLayout,
    QLineEdit,
    QListWidget,
    QListWidgetItem,
    QMainWindow,
    QProgressBar,
    QPushButton,
    QScrollArea,
    QSizePolicy,
    QSplitter,
    QTabWidget,
    QVBoxLayout,
    QWidget,
)

from chengchuan import APP_NAME, __version__
from chengchuan.application.errors import StorageError
from chengchuan.application.market_refresh import MarketRefreshController, market_checkpoint
from chengchuan.application.microstructure import analyze_orders
from chengchuan.application.quote_metrics import details_from_frame
from chengchuan.application.settings import SettingsSaveError, SettingsService
from chengchuan.domain.calendar import CalendarUnavailable
from chengchuan.domain.market import Instrument, instrument_symbol, is_index
from chengchuan.domain.models import SHANGHAI, IndexRange, SeriesKind
from chengchuan.domain.periods import PERIODS, chart_period
from chengchuan.ui import theme
from chengchuan.ui.book_stream import BookStreamRunner
from chengchuan.ui.chart import MarketChart
from chengchuan.ui.chart_orders import ChartOrderController
from chengchuan.ui.date_navigator import DateNavigator
from chengchuan.ui.forecast_panel import ForecastPanel
from chengchuan.ui.market import MarketBrowser
from chengchuan.ui.market_dashboard import IndexOverviewBar, QuoteDetailsPanel
from chengchuan.ui.market_views import HeatmapDialog, MoneyFlowPanel
from chengchuan.ui.microstructure import MicrostructureDialog, MicrostructurePanel
from chengchuan.ui.settings_dialog import SettingsDialog
from chengchuan.ui.tasks import LatestTaskRunner, ProcessTaskRunner


def label(text="", name=None, wrap=False):
    widget = QLabel(text)
    if name:
        widget.setObjectName(name)
    widget.setWordWrap(wrap)
    return widget


def format_metric(metric):
    v = metric.value
    if v is None:
        return metric.missing_text
    if metric.unit == "元":
        return f"{v / 1e8:,.2f} 亿" if abs(v) >= 1e8 else f"{v / 1e4:,.2f} 万"
    if metric.unit == "股":
        return f"{v / 1e4:,.2f} 万股"
    if metric.unit == "%":
        return f"{v:+.2f}%"
    if metric.unit == "百分点":
        return f"{v:+.2f} pp"
    return f"{v:.3f}"


def empty_layout(layout):
    while layout.count():
        item = layout.takeAt(0)
        if item.widget():
            item.widget().deleteLater()
        elif item.layout():
            child = item.layout()
            empty_layout(child)
            child.deleteLater()


class MainWindow(QMainWindow):
    def __init__(
        self,
        snapshots,
        analyzer,
        workspace,
        catalog=(),
        market=None,
        network_runner_factory=ProcessTaskRunner,
        forecast_service=None,
        forecast_writer=None,
        market_sources=None,
        settings_service=None,
        initial_provider=None,
    ):
        super().__init__()
        self.snapshots, self.analyzer, self.workspace = snapshots, analyzer, workspace
        self.snapshot = None
        self.selection = None
        self.report = None
        self._loading = False
        self.market = market
        self.market_sources = market_sources
        state = self.workspace.load("main")
        self.settings_service = settings_service or SettingsService(workspace, market_sources)
        self.preferences = self.settings_service.load(state)
        if initial_provider is not None:
            self.preferences = replace(self.preferences, provider=initial_provider)
        self.settings_dialog = None
        self.heatmap_dialog = None
        self.micro_dialog = None
        self._l2_tape = self._l2_analysis = None
        self._l2_requested = None
        self._pending_l2 = None
        self._pending_chart_days = {}
        self._chart_day_queue = []
        self._chart_day_active = None
        self._chart_day_attempts = set()
        self._chart_cache_attempted = None
        self._l2_loading_kind = None
        self.forecast_service, self.forecast_writer = forecast_service, forecast_writer
        self.forecast_panel = None
        self.network_runner_factory = network_runner_factory
        self.online_symbol = None
        self.online_interval = "day"
        self.frame = None
        self.history_date = None
        self.current_path = None
        self.market_runner = network_runner_factory(self) if market else None
        self.overview_runner = network_runner_factory(self) if market else None
        self.details_runner = network_runner_factory(self) if market else None
        self.funds_runner = network_runner_factory(self) if market else None
        self.l2_runner = network_runner_factory(self) if market else None
        self.book_runner = network_runner_factory(self) if market else None
        self.chart_days_runner = network_runner_factory(self) if market else None
        if self.chart_days_runner:
            self.chart_days_runner.timeout_ms = 180000
            self.chart_days_runner.ready.connect(self._chart_day_ready)
            self.chart_days_runner.failed.connect(self._chart_day_failed)
        if self.l2_runner:
            self.l2_runner.timeout_ms = 180000
            self.l2_runner.ready.connect(self._l2_ready)
            self.l2_runner.failed.connect(self._l2_failed)
            self.book_runner.ready.connect(self._book_ready)
            self.book_runner.failed.connect(self._micro_error)
        self.l2_analysis_runner = LatestTaskRunner(self)
        self.l2_analysis_runner.ready.connect(self._l2_analysis_ready)
        self.l2_analysis_runner.failed.connect(self._l2_failed)
        self.book_stream = BookStreamRunner(self)
        self.book_stream.ready.connect(self._book_ready)
        self.book_stream.status.connect(self._micro_status)
        self.book_stream.finished.connect(lambda: self._toggle_book_stream(False))
        self._overview_requested = False
        self._details_refresh = False
        self._refresh_controller = MarketRefreshController()
        self._latest_details = self._latest_overview = None
        self._funds_requested_symbol = None
        if self.market_runner:
            self.market_runner.ready.connect(self._market_ready)
            self.market_runner.failed.connect(self._market_failed)
            self.overview_runner.ready.connect(self._overview_ready)
            self.overview_runner.failed.connect(self._overview_failed)
            self.details_runner.ready.connect(self._details_ready)
            self.details_runner.failed.connect(self._details_failed)
            self.funds_runner.ready.connect(self._funds_ready)
            self.funds_runner.failed.connect(self._funds_failed)
        self.runner = LatestTaskRunner(self)
        self.runner.ready.connect(self._task_ready)
        self.runner.failed.connect(self._task_failed)
        self.runner.busy.connect(self._busy)
        self.setWindowTitle(f"{APP_NAME} Chengchuan · 股票研究工作台")
        self.resize(1440, 920)
        self.setMinimumSize(1100, 740)
        self.setStyleSheet(theme.STYLESHEET)
        root = QWidget()
        self.setCentralWidget(root)
        outer = QVBoxLayout(root)
        outer.setContentsMargins(0, 0, 0, 0)
        outer.setSpacing(0)
        self.index_bar = IndexOverviewBar() if market else None
        if self.index_bar:
            self.index_bar.open_requested.connect(self.open_stock)
            self.index_bar.heatmap_requested.connect(self.open_heatmap)
            outer.addWidget(self.index_bar)
        self.splitter = QSplitter(Qt.Orientation.Horizontal)
        outer.addWidget(self.splitter, 1)
        self._build_sidebar(catalog)
        self._build_chart_area()
        self._build_inspector()
        self.splitter.setSizes([220, 865, 355])
        self.splitter.setCollapsible(0, False)
        self.splitter.setCollapsible(1, False)
        self.splitter.setCollapsible(2, False)
        self.status = label("本地研究预览 · 导入行情快照开始分析", "status")
        outer.addWidget(self.status)
        QShortcut(QKeySequence("Escape"), self, activated=self.clear_selection)
        QShortcut(QKeySequence("Ctrl+O"), self, activated=self.choose_snapshot)
        QShortcut(QKeySequence("Ctrl+,"), self, activated=self.open_settings)
        if state.get("splitter") and len(state["splitter"]) == 3:
            self.splitter.setSizes(state["splitter"])
        self._saved_state = state
        self.refresh_timer = QTimer(self)
        self.refresh_timer.setInterval(self.preferences.refresh_seconds * 1000)
        self.refresh_timer.timeout.connect(self._auto_refresh)
        self.refresh_timer.start()
        QShortcut(QKeySequence("F5"), self, activated=self.refresh_market)
        if market:
            self.auto_refresh.setChecked(self.preferences.auto_refresh)
            self.auto_refresh.toggled.connect(self._auto_refresh_preference_changed)
        self._apply_display_preferences()

    def _build_sidebar(self, catalog):
        sidebar = QWidget()
        sidebar.setObjectName("sidebar")
        sidebar.setMinimumWidth(220)
        sidebar.setMaximumWidth(330)
        layout = QVBoxLayout(sidebar)
        layout.setContentsMargins(18, 24, 18, 20)
        layout.setSpacing(12)
        layout.addWidget(label(APP_NAME, "brand"))
        layout.addWidget(label("CHENGCHUAN", "brandEnglish"))
        layout.addSpacing(8)
        layout.addWidget(label("研究工作台", "sectionTitle"))
        if self.market:
            self.browser = MarketBrowser(
                self.market, self.workspace, self.network_runner_factory, self
            )
            self.browser.open_requested.connect(self.open_stock)
            layout.addWidget(self.browser, 1)
        layout.addWidget(label("本地行情快照", "eyebrow"))
        self.search = QLineEdit()
        self.search.setPlaceholderText("过滤本地快照")
        self.search.textChanged.connect(self._filter_catalog)
        layout.addWidget(self.search)
        self.library = QListWidget()
        if self.market:
            self.search.hide()
            self.library.setMaximumHeight(115)
        self.library.itemClicked.connect(
            lambda item: self.open_snapshot(Path(item.data(Qt.ItemDataRole.UserRole)))
        )
        for title, path in catalog:
            item = QListWidgetItem(title)
            item.setData(Qt.ItemDataRole.UserRole, str(path))
            self.library.addItem(item)
        layout.addWidget(self.library, 0 if self.market else 1)
        button = QPushButton("＋  导入行情快照")
        button.clicked.connect(self.choose_snapshot)
        layout.addWidget(button)
        self.settings_button = QPushButton("设置  Ctrl+,")
        self.settings_button.clicked.connect(lambda: self.open_settings())
        layout.addWidget(self.settings_button)
        layout.addSpacing(15)
        layout.addWidget(label("在价格中发现线索\n让每个判断都有依据", "muted", True))
        layout.addSpacing(8)
        layout.addWidget(label(f"v{__version__}  ·  桌面基础版", "eyebrow"))
        self.splitter.addWidget(sidebar)

    def _build_chart_area(self):
        center = QWidget()
        center.setMinimumWidth(530)
        layout = QVBoxLayout(center)
        layout.setContentsMargins(24, 24, 18, 16)
        layout.setSpacing(15)
        heading = QHBoxLayout()
        info = QVBoxLayout()
        self.stock_title = label("行情研究", "stockTitle")
        self.stock_subtitle = label("导入已保存的日线或分钟快照", "muted")
        info.addWidget(self.stock_title)
        info.addWidget(self.stock_subtitle)
        heading.addLayout(info)
        heading.addStretch()
        quote = QVBoxLayout()
        self.quote = label("—", "quote")
        quote.addWidget(self.quote, alignment=Qt.AlignmentFlag.AlignRight)
        self.quote_caption = label("快照末价", "eyebrow")
        quote.addWidget(self.quote_caption, alignment=Qt.AlignmentFlag.AlignRight)
        heading.addLayout(quote)
        layout.addLayout(heading)
        tools = QHBoxLayout()
        self.kind_label = label("日K / 分时", "summary")
        tools.addWidget(self.kind_label)
        tools.addSpacing(12)
        tools.addWidget(label("Ctrl + 拖动选择区间", "muted"))
        tools.addStretch()
        clear = QPushButton("清除选区  Esc")
        clear.clicked.connect(self.clear_selection)
        tools.addWidget(clear)
        layout.addLayout(tools)
        if self.market:
            source_row = QHBoxLayout()
            source_row.addWidget(label("数据源", "muted"))
            if self.market_sources:
                self.source_box = QComboBox()
                for code, title in self.market_sources.choices:
                    self.source_box.addItem(title, code)
                self.source_box.setCurrentIndex(self.source_box.findData(self.market.source_id))
                self.source_box.currentIndexChanged.connect(self._source_changed)
                source_row.addWidget(self.source_box)
                self.key_button = QPushButton("配置猫爪")
                self.key_button.clicked.connect(self.configure_market_key)
                source_row.addWidget(self.key_button)
            self.source_hint = label(getattr(self.market, "connection_notice", ""), "eyebrow", True)
            source_row.addWidget(self.source_hint, 1)
            layout.addLayout(source_row)
            controls = QHBoxLayout()
            self.period = QComboBox()
            self._set_period_capabilities()
            self.period.setEnabled(False)
            self.period.currentIndexChanged.connect(self._period_changed)
            controls.addWidget(self.period)
            self.refresh_button = QPushButton("刷新 F5")
            self.refresh_button.clicked.connect(self.refresh_market)
            self.refresh_button.setEnabled(False)
            controls.addWidget(self.refresh_button)
            self.auto_refresh = QCheckBox("每15秒刷新")
            self.auto_refresh.setChecked(True)
            self.auto_refresh.setEnabled(False)
            controls.addWidget(self.auto_refresh)
            controls.addStretch()
            self.watch_button = QPushButton("☆ 加入自选")
            self.watch_button.clicked.connect(self._add_current_stock)
            self.watch_button.setEnabled(False)
            controls.addWidget(self.watch_button)
            layout.addLayout(controls)
        self.market_status = label("本地历史快照 · 不自动更新", "eyebrow", True)
        layout.addWidget(self.market_status)
        self.ohlc = label("鼠标悬停查看行情", "eyebrow")
        self.ohlc.setSizePolicy(QSizePolicy.Policy.Ignored, QSizePolicy.Policy.Preferred)
        layout.addWidget(self.ohlc)
        self.chart = MarketChart()
        self.chart.range_selected.connect(self.analyze_selection)
        self.chart.user_range_selected.connect(
            lambda: self.research_tabs.setCurrentWidget(self.analysis_view)
        )
        self.chart.hover_text.connect(self.ohlc.setText)
        layout.addWidget(self.chart, 1)
        self.date_nav = DateNavigator()
        self.date_nav.requested.connect(self.jump_to_date)
        self.date_nav.latest_requested.connect(self.return_to_latest)
        layout.addWidget(self.date_nav)
        self.chart_orders = ChartOrderController(self.chart, self)
        self.chart_orders.requested.connect(self._reload_chart_orders)
        self.chart_orders.cache_requested.connect(self._ensure_chart_orders)
        layout.addWidget(self.chart_orders.controls)
        self.coverage = label("", "eyebrow", True)
        layout.addWidget(self.coverage)
        self.splitter.addWidget(center)

    def _set_period_capabilities(self):
        previous = self.period.currentData()
        supported = getattr(self.market, "intervals", ("day", "minute"))
        self.period.blockSignals(True)
        self.period.clear()
        for p in PERIODS:
            self.period.addItem(p.label, p.code)
            item = self.period.model().item(self.period.count() - 1)
            item.setEnabled(p.code in supported)
            if p.code not in supported:
                item.setToolTip("当前源不支持此周期，请切换猫爪并配置本机API Key")
        self.period.setMaxVisibleItems(len(PERIODS))
        self.period.setCurrentIndex(
            self.period.findData(previous if previous in supported else "day")
        )
        self.period.blockSignals(False)

    def _source_changed(self):
        try:
            market = self.market_sources.connect(self.source_box.currentData())
        except (OSError, ValueError, StorageError):
            self.source_hint.setText("数据源未能切换，请检查本机配置文件")
            self.source_box.blockSignals(True)
            self.source_box.setCurrentIndex(self.source_box.findData(self.market.source_id))
            self.source_box.blockSignals(False)
            return
        self._install_market(market)
        self.preferences = replace(self.preferences, provider=self.source_box.currentData())
        self._persist_preferences()

    def _install_market(self, market):
        self._clear_micro()
        symbol, interval, day = self.online_symbol, self.online_interval, self.history_date
        self.market_runner.cancel()
        self.overview_runner.cancel()
        self.details_runner.cancel()
        self.funds_runner.cancel()
        if self.heatmap_dialog:
            self.heatmap_dialog.close()
        self._overview_requested = False
        self._refresh_controller.reset()
        self._latest_details = self._latest_overview = None
        self.index_bar.reset()
        self.browser.set_market(market)
        self.market = market
        self.source_box.blockSignals(True)
        self.source_box.setCurrentIndex(self.source_box.findData(market.source_id))
        self.source_box.blockSignals(False)
        self.source_hint.setText(market.connection_notice)
        self._set_period_capabilities()
        if symbol:
            self.online_symbol = None
            self.frame = None
            self.open_stock(
                symbol,
                interval if interval in market.intervals else "day",
                as_of=day if market.supports_history else None,
            )

    def configure_market_key(self):
        self.open_settings("data", prefer_meoz=True)

    def open_settings(self, page="data", prefer_meoz=False):
        if self.settings_dialog is not None:
            self.settings_dialog.select_page(page)
            self.settings_dialog.raise_()
            self.settings_dialog.activateWindow()
            return
        values = replace(self.preferences, provider="meoz") if prefer_meoz else self.preferences
        dialog = SettingsDialog(
            values,
            self.settings_service.info(),
            self._save_preferences,
            self.settings_service.info,
            bool(self.market_sources),
            self,
        )
        self.settings_dialog = dialog
        dialog.finished.connect(self._settings_closed)
        dialog.select_page(page)
        dialog.open()

    def _settings_closed(self):
        self.settings_dialog = None

    def _save_preferences(self, settings, api_key=""):
        self.settings_service.save(settings, api_key)
        self.preferences = settings
        self.refresh_timer.setInterval(settings.refresh_seconds * 1000)
        if self.market:
            self.auto_refresh.blockSignals(True)
            self.auto_refresh.setChecked(settings.auto_refresh)
            self.auto_refresh.blockSignals(False)
        self._apply_display_preferences()
        if self.market_sources:
            try:
                market = self.market_sources.connect(settings.provider)
            except (OSError, ValueError, StorageError):
                raise SettingsSaveError(
                    "设置已保存，但数据源暂未切换，请重试", bool(api_key.strip())
                ) from None
            if market.source_id != self.market.source_id or api_key.strip():
                self._install_market(market)
        self.status.setText("设置已保存并生效")

    def _apply_display_preferences(self):
        settings = self.preferences
        if self.market:
            self.auto_refresh.setText(f"每{settings.refresh_seconds}秒检查更新")
            self.auto_refresh.setToolTip(
                "交易窗口按间隔检查；闭市仅补齐落后缓存，齐全后暂停；F5可手动刷新"
            )
        self.chart.cursor.configure(
            settings.show_crosshair, settings.show_tooltip, settings.tooltip_opacity
        )

    def _auto_refresh_preference_changed(self, checked):
        self._refresh_controller.reset()
        self.preferences = replace(self.preferences, auto_refresh=checked)
        self._persist_preferences()

    def _persist_preferences(self):
        try:
            self.settings_service.save(self.preferences)
        except SettingsSaveError:
            self.status.setText("当前操作已生效，但偏好设置未能保存")

    def _build_inspector(self):
        inspector = QWidget()
        inspector.setObjectName("inspector")
        inspector.setMinimumWidth(300)
        inspector.setMaximumWidth(450)
        shell = QVBoxLayout(inspector)
        shell.setContentsMargins(0, 0, 0, 0)
        analysis_view = QWidget()
        layout = QVBoxLayout(analysis_view)
        layout.setContentsMargins(20, 24, 18, 20)
        layout.setSpacing(13)
        layout.addWidget(label("区间研究", "sectionTitle"))
        self.selected_window = label("选择一段走势，开始研究", "muted", True)
        layout.addWidget(self.selected_window)
        self.summary = label("等待选择", "summary", True)
        layout.addWidget(self.summary)
        scroll = QScrollArea()
        scroll.setWidgetResizable(True)
        content = QWidget()
        self.results = QVBoxLayout(content)
        self.results.setSizeConstraint(QLayout.SizeConstraint.SetMinAndMaxSize)
        self.results.setContentsMargins(0, 0, 4, 0)
        self.results.setSpacing(12)
        self.results.addStretch()
        scroll.setWidget(content)
        layout.addWidget(scroll, 1)
        layout.addWidget(
            label("阶段为候选解释。规则分数尚未校准，\n不代表概率或真实机构持仓。", "notice", True)
        )
        self.analysis_view = analysis_view
        self.research_tabs = QTabWidget()
        self.quote_panel = QuoteDetailsPanel()
        self.research_tabs.addTab(self.quote_panel, "行情")
        self.research_tabs.addTab(analysis_view, "区间分析")
        self.funds_panel = MoneyFlowPanel()
        self.research_tabs.addTab(self.funds_panel, "资金")
        self.funds_panel.requested.connect(self._request_funds)
        self.funds_panel.order_bands.requested.connect(lambda: self._request_l2_tape(6))
        self.funds_panel.order_bands.filters_changed.connect(self._band_filters_changed)
        self.micro_panel = MicrostructurePanel()
        self._wire_micro(self.micro_panel)
        self.research_tabs.addTab(self.micro_panel, "盘口逐笔")
        self.research_tabs.currentChanged.connect(self._research_tab_changed)
        if self.forecast_service:
            self.forecast_panel = ForecastPanel(
                self.forecast_service, report_writer=self.forecast_writer
            )
            forecast_view = QWidget()
            self.forecast_tab = forecast_view
            forecast_layout = QVBoxLayout(forecast_view)
            forecast_layout.setContentsMargins(14, 10, 14, 12)
            forecast_layout.addWidget(self.forecast_panel)
            self.research_tabs.addTab(forecast_view, "量价预测")
        shell.addWidget(self.research_tabs)
        self.splitter.addWidget(inspector)

    def _filter_catalog(self, text):
        for i in range(self.library.count()):
            item = self.library.item(i)
            item.setHidden(text.lower() not in item.text().lower())

    def choose_snapshot(self):
        path, _ = QFileDialog.getOpenFileName(self, "打开行情快照", "", "行情快照 (*.json)")
        if path:
            self.open_snapshot(Path(path))

    def open_snapshot(self, path: Path):
        if self.market_runner:
            self.market_runner.cancel()
            self.overview_runner.cancel()
            self.details_runner.cancel()
            self.funds_runner.cancel()
        self.online_symbol = None
        self.history_date = None
        self.frame = None
        self.research_tabs.setCurrentWidget(self.analysis_view)
        self._reset_display()
        self.market_status.setText("本地历史快照 · 不自动更新")
        if self.market:
            self.period.setEnabled(False)
            self.refresh_button.setEnabled(False)
            self.auto_refresh.setEnabled(False)
        self._loading = True
        self.chart.setEnabled(False)
        self.status.setText("正在打开行情快照…")
        self.runner.submit(str(path), None, lambda: ("snapshot", self.snapshots.load(path), path))

    def _task_ready(self, payload):
        if payload[0] == "snapshot":
            _, snapshot, self.current_path = payload
            self._show_snapshot(snapshot)
        else:
            self.report = payload[1]
            self.render_report(self.report)

    def _reset_display(self):
        self._clear_micro()
        self.runner.cancel()
        self.snapshot = self.selection = self.report = None
        if self.forecast_panel:
            self.forecast_panel.set_context(None)
        self.chart.reset()
        self.quote_panel.clear()
        self.funds_panel.clear()
        if self.details_runner:
            self.details_runner.cancel()
        self.date_nav.set_context(
            bool(self.online_symbol),
            self.history_date,
            historical=self.history_date is not None,
            hint="正在加载所选日期…" if self.history_date else "打开行情后选择日期",
        )
        self.chart.setEnabled(False)
        self.stock_title.setText("正在打开股票…")
        self.quote.setText("—")
        self.quote.setStyleSheet("")
        self.quote_caption.setText("等待行情")
        self.ohlc.setText("鼠标悬停查看行情")
        self.coverage.setText("")
        self.selected_window.setText("等待行情")
        self.summary.setText("等待行情")
        empty_layout(self.results)
        self.results.addStretch()
        if self.market:
            self.watch_button.setEnabled(False)

    def _show_snapshot(self, snapshot, preserve=False):
        old_snapshot, old_selection = self.snapshot, self.selection
        unchanged = preserve and old_snapshot and old_snapshot.revision == snapshot.revision
        self.snapshot = snapshot
        if self.forecast_panel:
            self.forecast_panel.set_context(
                None if is_index(snapshot.series.symbol) else snapshot,
                self.frame.retrieved_at if self.frame else None,
                self.frame.quote.timestamp if self.frame and self.frame.quote else None,
            )
            if is_index(snapshot.series.symbol):
                self.forecast_panel.status.setText(
                    "当前指数提供图表与区间统计；预测研究暂未覆盖指数。"
                )
        self._loading = False
        self.chart.setEnabled(True)
        series, bars = snapshot.series, snapshot.series.bars
        self.stock_title.setText(f"{series.name}  {series.symbol[-6:]}")
        self.stock_subtitle.setText(
            ("沪深指数 / 市场研究" if is_index(series.symbol) else "沪深A股 / 公开行情研究")
            if self.online_symbol
            else "本地快照 / 沪深市场研究"
        )
        self.quote.setText(f"{bars[-1].close:.2f}")
        self.quote_caption.setText("图表末价")
        period = chart_period(series.interval)
        detail = (
            "指数点位"
            if is_index(series.symbol)
            else "不复权"
            if period.daily
            else ("真实分钟OHLC" if series.kind == SeriesKind.OHLC else "分钟价")
        )
        self.kind_label.setText(f"{period.label} · {detail}")
        self.kind_label.setToolTip("\n".join(snapshot.quality_notes))
        self.coverage.setText(
            f"{snapshot.source}  ·  {bars[0].timestamp:%Y-%m-%d} — "
            f"{bars[-1].timestamp:%Y-%m-%d}  ·  {len(bars):,} 条记录"
        )
        self.date_nav.set_context(
            True,
            self.history_date or bars[-1].timestamp.astimezone(SHANGHAI).date(),
            historical=self.history_date is not None,
            hint="历史浏览 · 自动更新暂停" if self.history_date else "点击日期弹出日历",
        )
        if unchanged:
            return
        if self.frame and (
            self._latest_details is None
            or self._latest_details.symbol != series.symbol
            or self._latest_details.day != bars[-1].timestamp.astimezone(SHANGHAI).date()
        ):
            self.quote_panel.render(details_from_frame(self.frame))
        self.runner.cancel()
        self.chart.set_snapshot(
            snapshot,
            preserve_view=preserve,
            vwap_points=self.frame.vwap_points if self.frame else (),
            baseline_price=self.frame.baseline_price if self.frame else None,
            reference_closes=self._chart_references(),
        )
        self.report = None
        if preserve and old_selection is None:
            self.chart.clear_selection()
            return
        count = min(11 if period.daily else 121, len(bars))
        selection = IndexRange(len(bars) - count, len(bars))
        if preserve and old_snapshot and old_selection:
            indices = {b.timestamp: i for i, b in enumerate(bars)}
            first = old_snapshot.series.bars[old_selection.start].timestamp
            last = old_snapshot.series.bars[old_selection.stop - 1].timestamp
            if first in indices and last in indices:
                selection = IndexRange(indices[first], indices[last] + 1)
        elif self._saved_state.get("revision") == snapshot.revision:
            try:
                restored = IndexRange(*self._saved_state["selection"])
                if restored.stop <= len(bars):
                    selection = restored
            except (KeyError, ValueError, TypeError):
                pass
        self.chart.set_selection(selection)

    def _chart_references(self):
        if self.frame is None:
            return ()
        references = dict(self.frame.reference_closes)
        if self.frame.quote:
            day = self.frame.quote.timestamp.astimezone(SHANGHAI).date()
            references.setdefault(day, self.frame.quote.previous_close)
        return tuple(sorted(references.items()))

    def open_stock(self, symbol, interval=None, refresh=False, as_of=None, automatic=False):
        if self.market is None:
            return
        try:
            symbol = instrument_symbol(symbol)
        except ValueError as exc:
            self.status.setText(str(exc))
            return
        interval = interval or self.period.currentData()
        retained_days = (
            dict(self.chart_orders.day_summaries) if symbol == self.online_symbol else {}
        )
        retained = (
            (self._l2_tape, self._l2_analysis)
            if symbol == self.online_symbol
            and self._l2_tape is not None
            and (as_of is None or as_of == self._l2_tape.day)
            else None
        )
        if not automatic:
            self._refresh_controller.reset()
        if symbol != self.online_symbol:
            self._latest_details = None
            self.research_tabs.setCurrentWidget(self.quote_panel)
        switching = (symbol, interval, as_of) != (
            self.online_symbol,
            self.online_interval,
            self.history_date,
        )
        self.online_symbol, self.online_interval = symbol, interval
        self._details_refresh = refresh
        self.index_bar.select(symbol)
        self.history_date = as_of
        self.current_path = None
        self.period.setEnabled(True)
        self.refresh_button.setEnabled(True)
        self.auto_refresh.setEnabled(as_of is None)
        self.period.blockSignals(True)
        self.period.setCurrentIndex(self.period.findData(interval))
        self.period.blockSignals(False)
        if switching or self.snapshot is None:
            self.funds_runner.cancel()
            self.frame = None
            self._reset_display()
            self._pending_l2 = retained
            self._pending_chart_days = retained_days
            self._loading = True
            self.stock_title.setText(f"{symbol[2:]} · 正在获取行情")
        if switching or self.frame is None:
            self.market_status.setText("正在连接行情源…")
        else:
            # Keep the multiline source block and chart geometry while loading.
            self.status.setText("正在刷新行情…")
        self.date_nav.set_context(
            True,
            as_of,
            historical=as_of is not None,
            hint="正在加载所选日期…" if as_of else "正在加载最新行情…",
        )
        task = (
            partial(self.market.load, symbol, interval, refresh)
            if as_of is None
            else partial(self.market.load, symbol, interval, refresh, as_of=as_of)
        )
        self.market_runner.submit(
            symbol + interval + (as_of.isoformat() if as_of else ""), None, task
        )
        if not automatic and (not self._overview_requested or refresh and as_of is None):
            self._request_overview(refresh)

    def _market_ready(self, frame):
        series = frame.snapshot.series
        if (series.symbol, series.interval, frame.as_of) != (
            self.online_symbol,
            self.online_interval,
            self.history_date,
        ):
            return
        preserve = self.frame is not None
        day = series.bars[-1].timestamp.astimezone(SHANGHAI).date()
        if self._l2_tape is not None and (self._l2_tape.symbol, self._l2_tape.day) != (
            series.symbol,
            day,
        ):
            remembered = (
                dict(self.chart_orders.day_summaries)
                if self._l2_tape.symbol == series.symbol
                else {}
            )
            self._clear_micro()
            self._pending_chart_days = remembered
        self.frame = frame
        self.chart_orders.restore_days(
            {
                d: value
                for d, value in self._pending_chart_days.items()
                if value.symbol == series.symbol
            }
        )
        self._pending_chart_days = {}
        if self._pending_l2 is not None:
            tape, analysis = self._pending_l2
            self._pending_l2 = None
            if tape.symbol == series.symbol and tape.day == day:
                self._l2_tape = tape
                if analysis is not None:
                    self._l2_analysis_ready(analysis)
                else:
                    self._replay_l2(self.micro_panel.cutoff.time().toPython())
        self._show_snapshot(frame.snapshot, preserve)
        self.watch_button.setEnabled(True)
        self._frame_status()
        self._request_details(self._details_refresh)
        if (
            self.research_tabs.currentWidget() is self.funds_panel
            and self.funds_panel.pages.currentIndex() == 0
        ):
            self._request_funds(self._details_refresh)
        if self.history_date:
            self._focus_history_date(self.history_date)

    def _frame_status(self):
        frame = self.frame
        if frame is None:
            return
        quote = frame.quote
        if quote:
            self.quote.setText(f"{quote.price:.2f}")
            self.quote.setStyleSheet(f"color: {theme.UP if quote.change_pct >= 0 else theme.DOWN};")
            self.quote_caption.setText(
                f"{'较当日昨收' if self.history_date else '较昨收'} {quote.change_pct:+.2f}%"
            )
            quote_time = quote.timestamp.astimezone(SHANGHAI).strftime("%m-%d %H:%M:%S")
        else:
            quote_time = "未取得"
            self.quote_caption.setText(
                "历史图表末价" if self.history_date else "图表末价 · 未取得最新报价"
            )
        bar = frame.snapshot.series.bars[-1]
        chart_time = bar.timestamp.astimezone(SHANGHAI)
        # Daily bars are labelled at the session close, even while still forming.
        chart_label = chart_time.strftime(
            "%Y-%m-%d" if chart_period(self.online_interval).daily else "%m-%d %H:%M"
        )
        now = datetime.now(SHANGHAI)
        trading_hours = now.weekday() < 5 and (
            "09:30" <= now.strftime("%H:%M") <= "11:30"
            or "13:00" <= now.strftime("%H:%M") <= "15:00"
        )
        stale = quote is None or (now - quote.timestamp.astimezone(SHANGHAI)).total_seconds() > 180
        mode = (
            "历史缓存"
            if frame.cached
            else ("已取得行情" if quote is None else ("报价时间较早" if stale else "已联网"))
        )
        session = "交易时段（节假日以数据源为准）" if trading_hours else "非连续竞价时段"
        if self.history_date:
            mode = "历史缓存" if frame.cached else "历史行情"
            session = "按所选日期截断"
        text = (
            f"{mode} · {frame.provider_name or '腾讯财经'} · {session}\n报价时间 {quote_time} · 图表截至 {chart_label} · "
            f"取得于 {frame.retrieved_at.astimezone(SHANGHAI):%m-%d %H:%M:%S} · 北京时间 · 行情可能延迟"
        )
        if frame.notice:
            text += "\n" + frame.notice
        if self.history_date:
            text = f"历史日期 {self.history_date:%Y-%m-%d} · 自动更新暂停\n" + text
        self.market_status.setText(text)
        self.market_status.setToolTip("\n".join(frame.sources))

    def _market_failed(self, error):
        self._loading = False
        if self.frame:
            self._frame_status()
            self.market_status.setText(
                self.market_status.text() + "\n刷新失败，保留上次行情：" + error
            )
        else:
            self.summary.setText("行情未取得")
            self.market_status.setText("连接失败：" + error + " · 可按F5重试或导入本地快照")
        self.status.setText("行情请求未完成：" + error)
        self.date_nav.hint.setText("所选日期未加载：" + error)

    def refresh_market(self):
        if self.online_symbol:
            if self.history_date and not getattr(self.market, "supports_history", False):
                self.date_nav.hint.setText("当前源只定位已加载历史，返回最新后可刷新")
                return
            self.open_stock(
                self.online_symbol, self.online_interval, refresh=True, as_of=self.history_date
            )

    def _auto_refresh(self):
        if (
            self.market
            and self.online_symbol
            and self.auto_refresh.isChecked()
            and self.history_date is None
            and getattr(self.market, "offline", False) is not True
            and getattr(self.market, "automatic_updates", True) is not False
            and all(
                r.active is None
                for r in (self.market_runner, self.details_runner, self.overview_runner)
            )
        ):
            plan = self._refresh_controller.plan(
                datetime.now(SHANGHAI),
                (getattr(self.market, "source_id", ""), self.online_symbol, self.online_interval),
                self.frame,
                self._latest_details,
                self._latest_overview,
                self.funds_panel.snapshot,
                want_details=getattr(self.market, "supports_details", False) is True,
                want_overview=getattr(self.market, "supports_overview", False) is True,
                want_funds=self.research_tabs.currentWidget() is self.funds_panel
                and self.funds_panel.pages.currentIndex() == 0
                and getattr(self.market, "supports_funds", False) is True,
            )
            self.auto_refresh.setToolTip(
                plan.reason + "；F5仍可手动刷新，付费缓存与额度保护继续生效"
            )
            if plan.chart:
                self.open_stock(
                    self.online_symbol, self.online_interval, refresh=plan.force, automatic=True
                )
            elif plan.details:
                self._request_details(plan.force)
            if plan.overview:
                self._request_overview(plan.force)
            if plan.funds and not plan.chart:
                self._request_funds(plan.force)

    def open_heatmap(self):
        if self.heatmap_dialog is None:
            symbols = tuple(
                self.browser.watchlist.item(i).data(Qt.ItemDataRole.UserRole).symbol
                for i in range(self.browser.watchlist.count())
            )
            self.heatmap_dialog = HeatmapDialog(
                self.market, symbols, self.network_runner_factory, self
            )
            self.heatmap_dialog.finished.connect(self._heatmap_closed)
            self.heatmap_dialog.open_requested.connect(self.open_stock)
            self.heatmap_dialog.load()
        self.heatmap_dialog.show()
        self.heatmap_dialog.raise_()

    def _heatmap_closed(self, unused=None):
        self.heatmap_dialog = None

    def _research_tab_changed(self, unused=None):
        if (
            self.research_tabs.currentWidget() is self.funds_panel
            and self.funds_panel.pages.currentIndex() == 0
        ):
            self._request_funds()

    def _wire_micro(self, panel):
        panel.snapshot_requested.connect(self._request_book)
        panel.tape_requested.connect(self._request_l2_tape)
        panel.stream_requested.connect(self._toggle_book_stream)
        panel.replay_requested.connect(self._replay_l2)
        panel.import_requested.connect(self._import_l2)
        panel.expand_requested.connect(self._expand_micro)
        panel.bands.filters_changed.connect(self._band_filters_changed)
        panel.restored_filters_changed.connect(self._restored_filters_changed)

    def _band_filters_changed(self, filters):
        self.funds_panel.order_bands.set_filters(filters)
        for panel in self._micro_panels():
            panel.bands.set_filters(filters)
        self.chart_orders.set_filters(filters)

    def _restored_filters_changed(self, filters):
        for panel in self._micro_panels():
            panel.set_restored_filters(filters)

    def _micro_panels(self):
        return (
            (self.micro_panel, self.micro_dialog.panel)
            if self.micro_dialog
            else (self.micro_panel,)
        )

    def _micro_context(self):
        if self.frame is None or not self.online_symbol or is_index(self.online_symbol):
            raise ValueError("盘口及原始委托分档适用于已打开的沪深个股；不将指数当作个股查询")
        return self.online_symbol, self.frame.snapshot.series.bars[-1].timestamp.astimezone(
            SHANGHAI
        ).date()

    def _micro_status(self, message):
        for panel in self._micro_panels():
            panel.status.setText(message)

    def _micro_error(self, message):
        self._micro_status("读取／分析未完成，已有数据保留：" + message)
        self.funds_panel.order_bands.caption.setText("读取／分析未完成：" + message)

    def _clear_micro(self):
        self.book_stream.stop()
        if self.l2_runner:
            self.l2_runner.cancel()
            self.book_runner.cancel()
        self.l2_analysis_runner.cancel()
        if self.chart_days_runner:
            self.chart_days_runner.cancel()
        self._chart_day_queue = []
        self._chart_day_active = None
        self._chart_day_attempts.clear()
        self._pending_chart_days = {}
        self._l2_tape = self._l2_analysis = self._l2_requested = None
        self._pending_l2 = None
        self._l2_loading_kind = None
        self.chart_orders.clear()
        if hasattr(self, "micro_panel"):
            for panel in self._micro_panels():
                panel.clear()

    def _ensure_chart_orders(self):
        if not (
            self.chart_orders.show_minute.isChecked() or self.chart_orders.show_marks.isChecked()
        ):
            pending = set(self._chart_day_queue)
            if self._chart_day_active is not None:
                pending.add(self._chart_day_active)
            self._chart_day_attempts.difference_update(pending)
            self._chart_day_queue = []
            self._chart_day_active = None
            if self.chart_days_runner:
                self.chart_days_runner.cancel()
            return
        try:
            symbol, day = self._micro_context()
        except ValueError:
            return
        loader = getattr(self.market, "load_chart_order_summary", None)
        if callable(loader):
            if self.l2_runner.active is not None or self.l2_analysis_runner.active is not None:
                return
            for target in reversed(self.chart_orders.required_days()):
                key = (getattr(self.market, "source_id", ""), symbol, target)
                if self.chart_orders._ready(target) or key in self._chart_day_attempts:
                    continue
                self._chart_day_attempts.add(key)
                self._chart_day_queue.append(key)
            self._next_chart_day()
            return
        if (
            self._l2_analysis is not None
            or self.l2_runner.active is not None
            or self.l2_analysis_runner.active is not None
        ):
            return
        key = (getattr(self.market, "source_id", ""), symbol, day)
        if key == self._chart_cache_attempted:
            return
        self._chart_cache_attempted = key
        cached = getattr(self.market, "load_cached_microstructure", None)
        if not callable(cached):
            self.chart_orders.set_data_status(
                "没有该日逐笔缓存；请点击“读取逐笔”或导入对应日期JSON。"
            )
            return
        self._l2_loading_kind = "cache"
        self._l2_requested = (symbol, day)
        self.chart_orders.set_data_status(f"正在读取{day:%Y-%m-%d}本地逐笔缓存，不访问付费接口…")
        self.l2_runner.submit("cached:" + symbol + str(day), None, partial(cached, symbol, day, 6))

    def _next_chart_day(self):
        if (
            self._chart_day_active is not None
            or not self._chart_day_queue
            or self.chart_days_runner is None
        ):
            return
        key = self._chart_day_queue.pop(0)
        source, symbol, day = key
        self._chart_day_active = key
        self.chart_orders.set_day_status(day, f"正在加载{day:%Y-%m-%d}原始逐笔…")
        self.chart_days_runner.submit(
            str(key), None, partial(self.market.load_chart_order_summary, symbol, day, 8)
        )

    def _chart_day_ready(self, summary):
        key, self._chart_day_active = self._chart_day_active, None
        if key is None:
            return
        if (summary.symbol, summary.day) == key[
            1:
        ] and summary.day in self.chart_orders.required_days():
            manual = self._l2_analysis
            if (
                manual is not None
                and (manual.symbol, manual.day) == (summary.symbol, summary.day)
                and manual.cutoff < summary.cutoff.replace(microsecond=0)
            ):
                self.chart_orders.set_analysis(manual)
            else:
                self.chart_orders.set_day_summary(summary)
        else:
            self.chart_orders.set_day_status(key[2], "返回的证券或日期不符，已拒绝")
        self._next_chart_day()

    def _chart_day_failed(self, message):
        key, self._chart_day_active = self._chart_day_active, None
        if key is None:
            return
        self.chart_orders.set_day_status(
            key[2], f"{key[2]:%Y-%m-%d}逐笔不可用：{message}；可点击补齐重试。"
        )
        self._next_chart_day()

    def _reload_chart_orders(self):
        if callable(getattr(self.market, "load_chart_order_summary", None)):
            days = set(self.chart_orders.required_days())
            busy = set(self._chart_day_queue)
            if self._chart_day_active is not None:
                busy.add(self._chart_day_active)
            self._chart_day_attempts = {
                k
                for k in self._chart_day_attempts
                if k in busy or k[2] not in days or self.chart_orders._ready(k[2])
            }
            self._ensure_chart_orders()
        else:
            self._request_l2_tape(6)

    def _l2_failed(self, message):
        cached = self._l2_loading_kind == "cache"
        self._l2_loading_kind = None
        self.chart_orders.set_data_status(
            "本地没有完整逐笔缓存；请点“读取逐笔”或导入JSON。当日历史尚未发布时可选择已发布日期。"
            if cached
            else "逐笔读取／分析未完成：" + message
        )
        if not cached:
            self._micro_error(message)

    def _request_book(self):
        try:
            symbol, day = self._micro_context()
        except ValueError as error:
            self._micro_error(str(error))
            return
        self.book_runner.submit(symbol, None, partial(self.market.load_order_book, symbol))
        self._micro_status("正在读取五档快照；数量将标明单位，优先使用缓存。")

    def _book_ready(self, book):
        try:
            symbol, day = self._micro_context()
        except ValueError:
            return
        if book.symbol != symbol or book.time.date() != day:
            self._micro_status("快照证券／日期与当前图表不符，已拒绝；历史盘口请读取已发布逐笔。")
            return
        for panel in self._micro_panels():
            panel.render_book(book)

    def _toggle_book_stream(self, enabled):
        self.book_stream.stop()
        if enabled:
            try:
                symbol, day = self._micro_context()
                if (
                    self.history_date is not None
                    or not market_checkpoint(datetime.now(SHANGHAI)).live
                ):
                    raise ValueError("历史浏览或休市中，不启动实时订阅；可读取快照或已发布逐笔。")
                if getattr(self.market, "supports_book_stream", False) is not True:
                    raise ValueError("当前来源不支持五档订阅")
            except (ValueError, CalendarUnavailable) as error:
                self._micro_status(str(error))
                enabled = False
            else:
                self.book_stream.start(partial(self.market.stream_order_book, symbol))
        for panel in self._micro_panels():
            panel.stream_button.blockSignals(True)
            panel.stream_button.setChecked(enabled)
            panel.stream_button.blockSignals(False)

    def _request_l2_tape(self, max_pages=6):
        try:
            symbol, day = self._micro_context()
        except ValueError as error:
            self._micro_error(str(error))
            return
        if getattr(self.market, "supports_microstructure", False) is not True:
            self._micro_error("逐笔需切换猫爪数据源，或导入本地JSON")
            return
        self._toggle_book_stream(False)
        self._l2_loading_kind = "network"
        self._l2_requested = (symbol, day)
        self.chart_orders.set_data_status(
            f"正在读取{day:%Y-%m-%d}逐笔；完成后自动显示分钟分档及标记。"
        )
        self._micro_status(
            f"读取{day:%Y-%m-%d}已发布原始委托、成交及十档；每类最多{max_pages}页，本地优先，数据不齐不发布饼图。"
        )
        self.l2_runner.submit(
            symbol + str(day),
            None,
            partial(self.market.load_microstructure, symbol, day, max_pages),
        )

    def _l2_ready(self, tape):
        try:
            expected = self._micro_context()
        except ValueError:
            return
        if (tape.symbol, tape.day) != expected:
            self._micro_error("逐笔证券／日期与当前图表不符；请先切到对应日期")
            return
        self._l2_tape = tape
        self._l2_loading_kind = None
        self.chart_orders.set_data_status("逐笔已读入，正在本地还原及建立分钟索引…")
        self._replay_l2(self.micro_panel.cutoff.time().toPython())

    def _replay_l2(self, clock):
        if self._l2_tape is None:
            self._micro_error("请先读取或导入对应日期的逐笔数据")
            return
        self._toggle_book_stream(False)
        cutoff = datetime.combine(self._l2_tape.day, clock, SHANGHAI)
        self.l2_analysis_runner.submit(
            str(cutoff), None, partial(analyze_orders, self._l2_tape, cutoff)
        )

    def _l2_analysis_ready(self, analysis):
        if self._l2_tape is None or (analysis.symbol, analysis.day) != (
            self._l2_tape.symbol,
            self._l2_tape.day,
        ):
            return
        self._l2_analysis = analysis
        for panel in self._micro_panels():
            panel.render_analysis(self._l2_tape, analysis)
        self.funds_panel.order_bands.render(analysis)
        self.chart_orders.set_analysis(analysis)
        self._ensure_chart_orders()

    def _import_l2(self):
        try:
            self._micro_context()
        except ValueError as error:
            self._micro_error(str(error))
            return
        path, _ = QFileDialog.getOpenFileName(self, "导入逐笔JSON数据包", "", "逐笔数据包 (*.json)")
        if path:
            self.l2_runner.submit(
                "file:" + path, None, partial(self.market.load_microstructure_file, Path(path))
            )

    def _expand_micro(self):
        if self.micro_dialog is None:
            self.micro_dialog = MicrostructureDialog(self)
            self._wire_micro(self.micro_dialog.panel)
            self.micro_dialog.panel.bands.set_filters(self.funds_panel.order_bands.filters.value())
            self.micro_dialog.panel.set_restored_filters(self.micro_panel.restored_filters.value())
        if self._l2_tape and self._l2_analysis:
            self.micro_dialog.panel.render_analysis(self._l2_tape, self._l2_analysis)
        elif self.micro_panel.book:
            self.micro_dialog.panel.render_book(self.micro_panel.book)
        self.micro_dialog.show()
        self.micro_dialog.raise_()

    def _request_funds(self, refresh=False):
        if self.market is None or self.frame is None or self.history_date is not None:
            self.funds_panel.clear("当前资金页提供最新快照；历史日期和本地快照暂未接入。")
            return
        if getattr(self.market, "supports_funds", False) is not True:
            self.funds_panel.clear("当前数据源未提供资金分类。")
            return
        symbol = "market:shsz" if self.funds_panel.scope.currentIndex() else self.online_symbol
        if (
            self.funds_runner.active is not None
            and not refresh
            and symbol == self._funds_requested_symbol
        ):
            return
        self._funds_requested_symbol = symbol
        self.funds_runner.submit(
            symbol, None, partial(self.market.load_funds, symbol, refresh=refresh)
        )

    def _funds_ready(self, snapshot):
        expected = "market:shsz" if self.funds_panel.scope.currentIndex() else self.online_symbol
        if self.frame is not None and self.history_date is None and snapshot.symbol == expected:
            self.funds_panel.render(snapshot)

    def _funds_failed(self, message):
        self.funds_panel.caption.setText("资金更新未完成，已有数据保留：" + message)

    def _request_overview(self, refresh=False):
        if (
            getattr(self.market, "supports_overview", False) is not True
            or self.overview_runner.active is not None
        ):
            return
        self._overview_requested = True
        self.overview_runner.submit(
            "market-overview", None, partial(self.market.load_overview, refresh=refresh)
        )

    def _overview_ready(self, overview):
        self._latest_overview = overview
        self.index_bar.render(overview)

    def _overview_failed(self, message):
        self.index_bar.failed(message)

    def _request_details(self, refresh=False):
        if self.frame is None or getattr(self.market, "supports_details", False) is not True:
            return
        symbol, day = self.online_symbol, self.history_date
        self.details_runner.submit(
            symbol + "@" + str(day),
            None,
            partial(self.market.load_details, symbol, as_of=day, refresh=refresh),
        )

    def _details_ready(self, details):
        if self.frame is not None and (details.symbol, details.as_of) == (
            self.online_symbol,
            self.history_date,
        ):
            self._latest_details = details
            self.quote_panel.render(details)

    def _details_failed(self, message):
        self.quote_panel.notice.setText("详细指标更新未完成：" + message)

    def _period_changed(self):
        if self.online_symbol:
            if self.period.currentData() not in getattr(
                self.market, "intervals", ("day", "minute")
            ):
                self.period.blockSignals(True)
                self.period.setCurrentIndex(self.period.findData(self.online_interval))
                self.period.blockSignals(False)
                self.source_hint.setText("当前源不支持此周期，请切换猫爪并配置本机API Key")
                return
            day = self.history_date if getattr(self.market, "supports_history", False) else None
            self.open_stock(self.online_symbol, self.period.currentData(), as_of=day)

    def _focus_history_date(self, day):
        try:
            location = self.chart.focus_date(day)
            message = location.caption
        except ValueError:
            # A non-trading target after the last available record is explicitly reported.
            last = self.snapshot.series.bars[-1].timestamp.astimezone(SHANGHAI).date()
            self.chart.focus_date(last)
            message = f"所选日无有效记录，实际数据截至 {last:%Y-%m-%d}"
        self.date_nav.set_context(True, day, historical=True, hint=message + " · 自动更新暂停")

    def jump_to_date(self, day):
        if self.online_symbol and getattr(self.market, "supports_history", False):
            self.open_stock(self.online_symbol, self.online_interval, as_of=day)
            return
        try:
            location = self.chart.focus_date(day)
        except ValueError as exc:
            self.date_nav.hint.setText(str(exc) + "；可使用猫爪补取或导入更多数据")
            return
        self.history_date = day
        if self.market_runner:
            self.market_runner.cancel()
            self.auto_refresh.setEnabled(False)
        self.date_nav.set_context(True, day, historical=True, hint=location.caption + " · 历史浏览")

    def return_to_latest(self):
        if self.online_symbol:
            self.open_stock(self.online_symbol, self.online_interval)
        elif self.snapshot:
            self.history_date = None
            self.chart.show_latest()
            day = self.snapshot.series.bars[-1].timestamp.astimezone(SHANGHAI).date()
            self.date_nav.set_context(True, day, hint="本地快照 · 已返回末尾")

    def _add_current_stock(self):
        if self.snapshot and self.online_symbol:
            series = self.snapshot.series
            self.browser.add_stock(Instrument(series.symbol, series.name))

    def analyze_selection(self, selection):
        if self.snapshot is None or self._loading:
            return
        self.selection = selection
        bars = self.snapshot.series.bars
        if self.forecast_panel and self.snapshot.series.interval == "day":
            self.forecast_panel.set_selection_date(
                bars[selection.stop - 1].timestamp.astimezone(SHANGHAI).date()
            )
        a, b = (
            bars[i].timestamp.astimezone(SHANGHAI) for i in (selection.start, selection.stop - 1)
        )
        self.selected_window.setText(
            f"{a:%Y-%m-%d %H:%M}\n{b:%Y-%m-%d %H:%M}  ·  {selection.count} 条记录"
        )
        self.report = None
        empty_layout(self.results)
        self.results.addStretch()
        if selection.count < 2:
            self.runner.cancel()
            self.summary.setText("请至少选择两条行情")
            return
        self.summary.setText("正在计算…")
        snapshot = self.snapshot
        self.runner.submit(
            snapshot.revision,
            selection,
            lambda: ("analysis", self.analyzer.run(snapshot, selection)),
        )

    def render_report(self, report):
        self.summary.setText(report.summary)
        empty_layout(self.results)
        grid = QGridLayout()
        grid.setSpacing(9)
        for i, metric in enumerate(report.metrics):
            card = QFrame()
            card.setObjectName("metric")
            box = QVBoxLayout(card)
            box.setContentsMargins(11, 9, 8, 9)
            box.setSpacing(4)
            box.addWidget(label(metric.label, "eyebrow"))
            value = label(format_metric(metric), "metricValue")
            if metric.key in ("return", "relative") and metric.value is not None:
                value.setStyleSheet(f"color: {theme.UP if metric.value >= 0 else theme.DOWN};")
            if metric.value is None:
                value.setStyleSheet("color: #8193a8; font-size: 15px;")
            box.addWidget(value)
            grid.addWidget(card, i // 2, i % 2)
        self.results.addLayout(grid)
        self.results.addWidget(label("阶段候选与支持证据", "eyebrow"))
        for finding in report.findings:
            frame = QFrame()
            frame.setObjectName("finding")
            box = QVBoxLayout(frame)
            box.setContentsMargins(11, 10, 11, 10)
            box.setSpacing(7)
            heading = QHBoxLayout()
            heading.addWidget(label(finding.label))
            heading.addStretch()
            heading.addWidget(label(f"{finding.evidence_score}", "brandEnglish"))
            box.addLayout(heading)
            progress = QProgressBar()
            progress.setRange(0, 100)
            progress.setValue(finding.evidence_score)
            progress.setTextVisible(False)
            box.addWidget(progress)
            if finding.evidence:
                box.addWidget(label("；".join(finding.evidence[:2]), "muted", True))
            self.results.addWidget(frame)
        self.results.addWidget(label("数据与分析口径", "eyebrow"))
        for note in report.notes:
            self.results.addWidget(label("· " + note, "muted", True))
        self.results.addStretch()
        self.status.setText("分析完成  ·  基于当前图表数据  ·  拖动选区边界可重新计算")

    def _task_failed(self, error):
        self._loading = False
        self.chart.setEnabled(True)
        self.summary.setText("本次操作未完成")
        self.status.setText("无法完成操作：" + error[:180])

    def _busy(self, busy):
        if busy and not self._loading:
            self.status.setText("正在分析所选区间…")

    def clear_selection(self):
        if self._loading:
            return
        self.runner.cancel()
        self.chart.clear_selection()
        self.selection = self.report = None
        self.summary.setText("等待选择")
        self.selected_window.setText("按住 Ctrl 拖动，选择一段走势")
        empty_layout(self.results)
        self.results.addStretch()
        self.status.setText("选区已清除  ·  当前行情保持不变")

    def closeEvent(self, event):
        self.book_stream.shutdown()
        self.l2_analysis_runner.shutdown()
        self.chart_orders.shutdown()
        if self.micro_dialog:
            self.micro_dialog.close()
        if self.settings_dialog is not None:
            self.settings_dialog.reject()
        self.refresh_timer.stop()
        if self.heatmap_dialog:
            self.heatmap_dialog.close()
        if self.forecast_panel:
            self.forecast_panel.shutdown()
        if self.market:
            self.browser.shutdown()
            self.market_runner.shutdown()
            self.overview_runner.shutdown()
            self.details_runner.shutdown()
            self.funds_runner.shutdown()
            self.l2_runner.shutdown()
            self.book_runner.shutdown()
            self.chart_days_runner.shutdown()
        self.runner.shutdown()
        state = {"splitter": self.splitter.sizes()}
        if self.market:
            state.update(
                symbol=self.online_symbol,
                interval=self.online_interval,
                auto_refresh=self.auto_refresh.isChecked(),
            )
            if self.market_sources:
                state["provider"] = self.source_box.currentData()
        if self.snapshot and self.selection:
            state.update(
                revision=self.snapshot.revision,
                selection=[self.selection.start, self.selection.stop],
            )
        try:
            self.workspace.save("main", state)
        except (OSError, ValueError, StorageError) as exc:
            self.status.setText("工作区保存失败：" + str(exc))
        super().closeEvent(event)
