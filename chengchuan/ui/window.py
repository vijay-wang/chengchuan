from __future__ import annotations

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
    QSplitter,
    QTabWidget,
    QVBoxLayout,
    QWidget,
)

from chengchuan import APP_NAME, __version__
from chengchuan.application.errors import StorageError
from chengchuan.domain.market import Instrument, stock_symbol
from chengchuan.domain.models import SHANGHAI, IndexRange, SeriesKind
from chengchuan.ui import theme
from chengchuan.ui.chart import MarketChart
from chengchuan.ui.forecast_panel import ForecastPanel
from chengchuan.ui.market import MarketBrowser
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
    ):
        super().__init__()
        self.snapshots, self.analyzer, self.workspace = snapshots, analyzer, workspace
        self.snapshot = None
        self.selection = None
        self.report = None
        self._loading = False
        self.market = market
        self.forecast_service, self.forecast_writer = forecast_service, forecast_writer
        self.forecast_panel = None
        self.network_runner_factory = network_runner_factory
        self.online_symbol = None
        self.online_interval = "day"
        self.frame = None
        self.current_path = None
        self.market_runner = network_runner_factory(self) if market else None
        if self.market_runner:
            self.market_runner.ready.connect(self._market_ready)
            self.market_runner.failed.connect(self._market_failed)
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
        state = self.workspace.load("main")
        if state.get("splitter") and len(state["splitter"]) == 3:
            self.splitter.setSizes(state["splitter"])
        self._saved_state = state
        self.refresh_timer = QTimer(self)
        self.refresh_timer.setInterval(15_000)
        self.refresh_timer.timeout.connect(self._auto_refresh)
        self.refresh_timer.start()
        QShortcut(QKeySequence("F5"), self, activated=self.refresh_market)
        if market:
            self.auto_refresh.setChecked(bool(state.get("auto_refresh", True)))

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
            controls = QHBoxLayout()
            self.period = QComboBox()
            self.period.addItem("日K · 不复权", "day")
            self.period.addItem("分时 · 分钟采样", "minute")
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
        layout.addWidget(self.ohlc)
        self.chart = MarketChart()
        self.chart.range_selected.connect(self.analyze_selection)
        self.chart.hover_text.connect(self.ohlc.setText)
        layout.addWidget(self.chart, 1)
        self.coverage = label("", "eyebrow", True)
        layout.addWidget(self.coverage)
        self.splitter.addWidget(center)

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
        if self.forecast_service:
            self.research_tabs = QTabWidget()
            self.forecast_panel = ForecastPanel(
                self.forecast_service, report_writer=self.forecast_writer
            )
            self.research_tabs.addTab(analysis_view, "区间分析")
            forecast_view = QWidget()
            forecast_layout = QVBoxLayout(forecast_view)
            forecast_layout.setContentsMargins(14, 10, 14, 12)
            forecast_layout.addWidget(self.forecast_panel)
            self.research_tabs.addTab(forecast_view, "量价预测")
            shell.addWidget(self.research_tabs)
        else:
            shell.addWidget(analysis_view)
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
        self.online_symbol = None
        self.frame = None
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
        self.runner.cancel()
        self.snapshot = self.selection = self.report = None
        if self.forecast_panel:
            self.forecast_panel.set_context(None)
        self.chart.reset()
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
                snapshot,
                self.frame.retrieved_at if self.frame else None,
                self.frame.quote.timestamp if self.frame and self.frame.quote else None,
            )
        self._loading = False
        self.chart.setEnabled(True)
        series, bars = snapshot.series, snapshot.series.bars
        self.stock_title.setText(f"{series.name}  {series.symbol[-6:]}")
        self.stock_subtitle.setText(
            "沪深A股 / 公开行情研究" if self.online_symbol else "本地快照 / 沪深市场研究"
        )
        self.quote.setText(f"{bars[-1].close:.2f}")
        self.quote_caption.setText("图表末价")
        self.kind_label.setText(
            "日K · 不复权" if series.kind == SeriesKind.OHLC else "分时 · 分钟采样"
        )
        self.coverage.setText(
            f"{snapshot.source}  ·  {bars[0].timestamp:%Y-%m-%d} — "
            f"{bars[-1].timestamp:%Y-%m-%d}  ·  {len(bars):,} 条记录"
        )
        if unchanged:
            return
        self.runner.cancel()
        self.chart.set_snapshot(snapshot, preserve_view=preserve)
        self.report = None
        if preserve and old_selection is None:
            self.chart.clear_selection()
            return
        count = min(11 if series.interval == "day" else 121, len(bars))
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

    def open_stock(self, symbol, interval=None, refresh=False):
        if self.market is None:
            return
        try:
            symbol = stock_symbol(symbol)
        except ValueError as exc:
            self.status.setText(str(exc))
            return
        interval = interval or self.period.currentData()
        switching = (symbol, interval) != (self.online_symbol, self.online_interval)
        self.online_symbol, self.online_interval = symbol, interval
        self.current_path = None
        self.period.setEnabled(True)
        self.refresh_button.setEnabled(True)
        self.auto_refresh.setEnabled(True)
        self.period.blockSignals(True)
        self.period.setCurrentIndex(self.period.findData(interval))
        self.period.blockSignals(False)
        if switching or self.snapshot is None:
            self.frame = None
            self._reset_display()
            self._loading = True
            self.stock_title.setText(f"{symbol[2:]} · 正在获取行情")
        self.market_status.setText("正在连接行情源…" if switching else "正在刷新行情…")
        self.market_runner.submit(
            symbol + interval, None, partial(self.market.load, symbol, interval, refresh)
        )

    def _market_ready(self, frame):
        series = frame.snapshot.series
        if (series.symbol, series.interval) != (self.online_symbol, self.online_interval):
            return
        preserve = self.frame is not None
        self.frame = frame
        self._show_snapshot(frame.snapshot, preserve)
        self.watch_button.setEnabled(True)
        self._frame_status()

    def _frame_status(self):
        frame = self.frame
        if frame is None:
            return
        quote = frame.quote
        if quote:
            self.quote.setText(f"{quote.price:.2f}")
            self.quote.setStyleSheet(f"color: {theme.UP if quote.change_pct >= 0 else theme.DOWN};")
            self.quote_caption.setText(f"较昨收 {quote.change_pct:+.2f}%")
            quote_time = quote.timestamp.astimezone(SHANGHAI).strftime("%m-%d %H:%M:%S")
        else:
            quote_time = "未取得"
            self.quote_caption.setText("图表末价 · 未取得最新报价")
        bar = frame.snapshot.series.bars[-1]
        chart_time = bar.timestamp.astimezone(SHANGHAI)
        # Daily bars are labelled at the session close, even while still forming.
        chart_label = chart_time.strftime(
            "%Y-%m-%d" if self.online_interval == "day" else "%m-%d %H:%M"
        )
        now = datetime.now(SHANGHAI)
        trading_hours = now.weekday() < 5 and (
            "09:30" <= now.strftime("%H:%M") <= "11:30"
            or "13:00" <= now.strftime("%H:%M") <= "15:00"
        )
        stale = quote is None or (now - quote.timestamp.astimezone(SHANGHAI)).total_seconds() > 180
        mode = "历史缓存" if frame.cached else ("报价时间较早" if stale else "已联网")
        session = "交易时段（节假日以数据源为准）" if trading_hours else "非连续竞价时段"
        text = (
            f"{mode} · 腾讯财经 · {session}\n报价时间 {quote_time} · 图表截至 {chart_label} · "
            f"取得于 {frame.retrieved_at.astimezone(SHANGHAI):%m-%d %H:%M:%S} · 北京时间 · 公开行情可能延迟"
        )
        if frame.notice:
            text += "\n" + frame.notice
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

    def refresh_market(self):
        if self.online_symbol:
            self.open_stock(self.online_symbol, self.online_interval, refresh=True)

    def _auto_refresh(self):
        if (
            self.market
            and self.online_symbol
            and self.auto_refresh.isChecked()
            and self.market_runner.active is None
        ):
            self.refresh_market()

    def _period_changed(self):
        if self.online_symbol:
            self.open_stock(self.online_symbol, self.period.currentData())

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
        self.refresh_timer.stop()
        if self.forecast_panel:
            self.forecast_panel.shutdown()
        if self.market:
            self.browser.shutdown()
            self.market_runner.shutdown()
        self.runner.shutdown()
        state = {"splitter": self.splitter.sizes()}
        if self.market:
            state.update(
                symbol=self.online_symbol,
                interval=self.online_interval,
                auto_refresh=self.auto_refresh.isChecked(),
            )
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
