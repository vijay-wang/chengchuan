from __future__ import annotations

from pathlib import Path

from PySide6.QtCore import Qt
from PySide6.QtGui import QKeySequence, QShortcut
from PySide6.QtWidgets import (
    QFileDialog,
    QFrame,
    QGridLayout,
    QHBoxLayout,
    QLabel,
    QLineEdit,
    QListWidget,
    QListWidgetItem,
    QMainWindow,
    QProgressBar,
    QPushButton,
    QScrollArea,
    QSplitter,
    QVBoxLayout,
    QWidget,
)

from chengchuan import APP_NAME, __version__
from chengchuan.application.errors import StorageError
from chengchuan.domain.models import SHANGHAI, IndexRange, SeriesKind
from chengchuan.ui import theme
from chengchuan.ui.chart import MarketChart
from chengchuan.ui.tasks import LatestTaskRunner


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
    def __init__(self, snapshots, analyzer, workspace, catalog=()):
        super().__init__()
        self.snapshots, self.analyzer, self.workspace = snapshots, analyzer, workspace
        self.snapshot = None
        self.selection = None
        self.report = None
        self._loading = False
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

    def _build_sidebar(self, catalog):
        sidebar = QWidget()
        sidebar.setObjectName("sidebar")
        sidebar.setMinimumWidth(190)
        sidebar.setMaximumWidth(330)
        layout = QVBoxLayout(sidebar)
        layout.setContentsMargins(18, 24, 18, 20)
        layout.setSpacing(12)
        layout.addWidget(label(APP_NAME, "brand"))
        layout.addWidget(label("CHENGCHUAN", "brandEnglish"))
        layout.addSpacing(25)
        layout.addWidget(label("研究工作台", "sectionTitle"))
        layout.addWidget(label("本地行情快照", "eyebrow"))
        self.search = QLineEdit()
        self.search.setPlaceholderText("搜索名称 / 代码")
        self.search.textChanged.connect(self._filter_catalog)
        layout.addWidget(self.search)
        self.library = QListWidget()
        self.library.itemClicked.connect(
            lambda item: self.open_snapshot(Path(item.data(Qt.ItemDataRole.UserRole)))
        )
        for title, path in catalog:
            item = QListWidgetItem(title)
            item.setData(Qt.ItemDataRole.UserRole, str(path))
            self.library.addItem(item)
        layout.addWidget(self.library, 1)
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
        quote.addWidget(label("快照末价", "eyebrow"), alignment=Qt.AlignmentFlag.AlignRight)
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
        layout = QVBoxLayout(inspector)
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
        self.results.setContentsMargins(0, 0, 4, 0)
        self.results.setSpacing(12)
        self.results.addStretch()
        scroll.setWidget(content)
        layout.addWidget(scroll, 1)
        layout.addWidget(
            label("阶段为候选解释。规则分数尚未校准，\n不代表概率或真实机构持仓。", "notice", True)
        )
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
        self._loading = True
        self.chart.setEnabled(False)
        self.status.setText("正在打开行情快照…")
        self.runner.submit(str(path), None, lambda: ("snapshot", self.snapshots.load(path), path))

    def _task_ready(self, payload):
        if payload[0] == "snapshot":
            _, self.snapshot, self.current_path = payload
            self._loading = False
            self.chart.setEnabled(True)
            series = self.snapshot.series
            bars = series.bars
            self.stock_title.setText(f"{series.name}  {series.symbol[-6:]}")
            self.stock_subtitle.setText("本地快照  /  沪深市场研究")
            self.quote.setText(f"{bars[-1].close:.2f}")
            self.kind_label.setText(
                "日K · 不复权" if series.kind == SeriesKind.OHLC else "分时 · 分钟采样"
            )
            self.coverage.setText(
                f"{self.snapshot.source}  ·  "
                f"{bars[0].timestamp:%Y-%m-%d} — {bars[-1].timestamp:%Y-%m-%d}"
                f"  ·  {len(bars):,} 条记录"
            )
            self.chart.set_snapshot(self.snapshot)
            self.report = None
            count = min(11 if series.interval == "day" else 121, len(bars))
            selection = IndexRange(len(bars) - count, len(bars))
            saved = self._saved_state
            if saved.get("revision") == self.snapshot.revision:
                try:
                    restored = IndexRange(*saved["selection"])
                    if restored.stop <= len(bars):
                        selection = restored
                except (KeyError, ValueError, TypeError):
                    pass
            self.chart.set_selection(selection)
        else:
            self.report = payload[1]
            self.render_report(self.report)

    def analyze_selection(self, selection):
        if self.snapshot is None or self._loading:
            return
        self.selection = selection
        bars = self.snapshot.series.bars
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
        self.status.setText("分析完成  ·  本地行情快照  ·  拖动选区边界可重新计算")

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
        self.runner.shutdown()
        state = {"splitter": self.splitter.sizes()}
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
