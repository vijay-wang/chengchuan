"""Company dossier workspace with lazy sections, shared virtual tables and local curves."""

from datetime import datetime
from functools import partial

import pyqtgraph as pg
from PySide6.QtCore import QDate, Qt, QUrl, Signal
from PySide6.QtGui import QDesktopServices, QTextOption
from PySide6.QtWidgets import (
    QCheckBox,
    QComboBox,
    QDateEdit,
    QFileDialog,
    QFrame,
    QGridLayout,
    QHBoxLayout,
    QLabel,
    QLineEdit,
    QListWidget,
    QListWidgetItem,
    QPushButton,
    QSplitter,
    QTabWidget,
    QTextBrowser,
    QVBoxLayout,
    QWidget,
)

from chengchuan.application.company import (
    IDENTITY_FIELDS,
    company_series,
    export_company,
    external_url,
    statement_matrix,
)
from chengchuan.application.research import numeric, research_stock_symbol
from chengchuan.domain.company import COMPANY_SECTIONS, CompanyRequest
from chengchuan.domain.models import SHANGHAI
from chengchuan.ui import theme
from chengchuan.ui.evidence import evidence_text
from chengchuan.ui.research import COLORS, ResearchDataView
from chengchuan.ui.tasks import ProcessTaskRunner


def text_label(text="", name="muted"):
    label = QLabel(text)
    label.setObjectName(name)
    label.setWordWrap(True)
    return label


class ReportAxis(pg.AxisItem):
    def __init__(self):
        super().__init__(orientation="bottom")
        self.dates = ()

    def tickStrings(self, values, scale, spacing):
        return [
            self.dates[round(v)]
            if abs(round(v) - v) < 0.12 and 0 <= round(v) < len(self.dates)
            else ""
            for v in values
        ]


class CompanyWorkspace(QWidget):
    back_requested = Signal()
    stock_requested = Signal(str, str, str)
    theme_requested = Signal(str)

    def __init__(self, service=None, runner_factory=None, open_url=None, parent=None):
        super().__init__(parent)
        self.service, self.bundle, self.active_request = service, None, None
        self.views, self.cards, self.names = {}, [], {}
        self.section = "overview"
        self.url_opener = open_url or QDesktopServices.openUrl
        self.runner = (
            runner_factory(self)
            if runner_factory
            else ProcessTaskRunner(self, timeout_ms=120000, task_label="公司资料")
        )
        self.runner.ready.connect(self._ready)
        self.runner.failed.connect(
            lambda message: self.status.setText("查询失败，保留已有资料：" + message)
        )
        self.runner.busy.connect(
            lambda busy: self.query_button.setText("查询中…" if busy else "查询／读取缓存")
        )
        layout = QVBoxLayout(self)
        layout.setContentsMargins(22, 18, 22, 12)
        heading = QHBoxLayout()
        back = QPushButton("‹ 返回行情")
        back.clicked.connect(self._back)
        heading.addWidget(back)
        self.heading = text_label("公司资料", "stockTitle")
        heading.addWidget(self.heading, 1)
        market = QPushButton("查看行情")
        market.clicked.connect(self._open_market)
        heading.addWidget(market)
        evidence = QPushButton("题材证据")
        evidence.clicked.connect(self._open_theme)
        heading.addWidget(evidence)
        self.export_button = QPushButton("导出公司研究卡")
        self.export_button.clicked.connect(self._export)
        heading.addWidget(self.export_button)
        layout.addLayout(heading)
        controls = QHBoxLayout()
        self.symbol_box = QLineEdit()
        self.symbol_box.setPlaceholderText("沪深股票代码，例如002318")
        self.symbol_box.setMaximumWidth(230)
        controls.addWidget(self.symbol_box)
        controls.addWidget(text_label("截至披露日"))
        today = datetime.now(SHANGHAI).date()
        shanghai_today = QDate(today.year, today.month, today.day)
        self.date_box = QDateEdit(shanghai_today)
        self.date_box.setCalendarPopup(True)
        self.date_box.setDisplayFormat("yyyy-MM-dd")
        self.date_box.setMaximumDate(shanghai_today)
        controls.addWidget(self.date_box)
        self.years_box = QComboBox()
        for years in (1, 3, 5):
            self.years_box.addItem(f"近{years}个自然年", years)
        self.years_box.setCurrentIndex(1)
        controls.addWidget(self.years_box)
        self.period_box = QComboBox()
        self.period_box.addItem("全部报告期", "")
        year = datetime.now(SHANGHAI).year
        for y in range(year, year - 6, -1):
            for md, title in (
                ("1231", "年报"),
                ("0930", "三季报"),
                ("0630", "中报"),
                ("0331", "一季报"),
            ):
                self.period_box.addItem(f"{y} · {title}", str(y) + md)
        controls.addWidget(self.period_box)
        self.versions_box = QCheckBox("全部修订")
        self.versions_box.setToolTip("保留所有已披露版本及并列标志，不把多版本选成唯一最新值。")
        controls.addWidget(self.versions_box)
        self.query_button = QPushButton("查询／读取缓存")
        self.query_button.setObjectName("primary")
        self.query_button.clicked.connect(lambda: self.query())
        controls.addWidget(self.query_button)
        self.network_button = QPushButton("联网更新")
        self.network_button.setToolTip(
            "主动尝试更新本分类；普通查询优先使用缓存，离线模式仍不会联网。"
        )
        self.network_button.clicked.connect(lambda: self.query(True))
        controls.addWidget(self.network_button)
        layout.addLayout(controls)
        body = QHBoxLayout()
        self.sections = QListWidget()
        self.sections.setObjectName("companySections")
        self.sections.setFixedWidth(135)
        for key, title in COMPANY_SECTIONS:
            item = QListWidgetItem(title)
            item.setData(Qt.ItemDataRole.UserRole, key)
            self.sections.addItem(item)
        self.sections.setCurrentRow(0)
        self.sections.currentRowChanged.connect(self._select_section)
        body.addWidget(self.sections)
        content = QVBoxLayout()
        actions = QHBoxLayout()
        self.filter_box = QLineEdit()
        self.filter_box.setPlaceholderText("过滤当前表中的名称、科目或正文…")
        self.filter_box.textChanged.connect(self._filter)
        actions.addWidget(self.filter_box, 1)
        self.matrix_box = QCheckBox("报表横向对比")
        self.matrix_box.setChecked(True)
        self.matrix_box.toggled.connect(lambda _: self._show_tables())
        actions.addWidget(self.matrix_box)
        self.hide_empty_box = QCheckBox("隐藏全空科目")
        self.hide_empty_box.setChecked(True)
        self.hide_empty_box.toggled.connect(lambda _: self._show_tables())
        self.matrix_box.toggled.connect(self.hide_empty_box.setEnabled)
        actions.addWidget(self.hide_empty_box)
        self.link_button = QPushButton("打开所选公告原文")
        self.link_button.clicked.connect(self._open_link)
        self.link_button.setEnabled(False)
        actions.addWidget(self.link_button)
        content.addLayout(actions)
        self.card_area = QWidget()
        self.card_grid = QGridLayout(self.card_area)
        self.card_grid.setContentsMargins(0, 0, 0, 0)
        content.addWidget(self.card_area)
        splitter = QSplitter(Qt.Orientation.Horizontal)
        table_area = QWidget()
        table_layout = QVBoxLayout(table_area)
        table_layout.setContentsMargins(0, 0, 0, 0)
        self.tabs = QTabWidget()
        self.tabs.setDocumentMode(True)
        self.tabs.currentChanged.connect(self._tab_changed)
        self.data_splitter = QSplitter(Qt.Orientation.Vertical)
        self.data_splitter.addWidget(self.tabs)
        table_layout.addWidget(self.data_splitter, 1)
        self.curve_area = QWidget()
        curve_layout = QVBoxLayout(self.curve_area)
        curve_layout.setContentsMargins(0, 0, 0, 0)
        curve_controls = QHBoxLayout()
        curve_controls.addWidget(text_label("报告期原值"))
        self.field_box = QComboBox()
        self.field_box.currentIndexChanged.connect(self._plot)
        curve_controls.addWidget(self.field_box, 1)
        self.scope_box = QComboBox()
        self.scope_box.setMaximumWidth(240)
        self.scope_box.currentIndexChanged.connect(self._plot)
        curve_controls.addWidget(self.scope_box, 1)
        self.curve_note = text_label("")
        curve_controls.addWidget(self.curve_note, 2)
        curve_layout.addLayout(curve_controls)
        self.axis = ReportAxis()
        self.plot = pg.PlotWidget(axisItems={"bottom": self.axis})
        self.plot.setBackground(theme.BACKGROUND)
        self.plot.showGrid(x=True, y=True, alpha=0.14)
        self.plot.addLegend(offset=(8, 4))
        self.plot.setMinimumHeight(175)
        self.curves = []
        curve_layout.addWidget(self.plot)
        self.data_splitter.addWidget(self.curve_area)
        self.data_splitter.setChildrenCollapsible(False)
        self.data_splitter.setSizes([440, 220])
        splitter.addWidget(table_area)
        self.evidence = QTextBrowser()
        self.evidence.setMinimumWidth(245)
        self.evidence.setWordWrapMode(QTextOption.WrapMode.WrapAtWordBoundaryOrAnywhere)
        self.evidence.setOpenExternalLinks(False)
        splitter.addWidget(self.evidence)
        splitter.setSizes([950, 280])
        content.addWidget(splitter, 1)
        body.addLayout(content, 1)
        layout.addLayout(body, 1)
        self.status = text_label("输入股票后查询；资料按分类读取，不启动后台轮询。")
        layout.addWidget(self.status)
        self.symbol_box.returnPressed.connect(lambda: self.query())
        self.symbol_box.textEdited.connect(self._changed)
        self.date_box.dateChanged.connect(self._changed)
        self.years_box.currentIndexChanged.connect(self._changed)
        self.period_box.currentIndexChanged.connect(self._changed)
        self.versions_box.toggled.connect(self._changed)
        self.set_service(service)

    def set_service(self, service):
        self.runner.cancel()
        self.names.clear()
        self.service, self.bundle, self.active_request = service, None, None
        self.tabs.clear()
        for view in self.views.values():
            view.deleteLater()
        self.views.clear()
        self.query_button.setEnabled(service is not None)
        self.network_button.setEnabled(service is not None)
        self.export_button.setEnabled(False)
        self.curve_area.hide()
        self.matrix_box.hide()
        self.hide_empty_box.hide()
        self.link_button.hide()
        self._show_facts(())
        self.heading.setText("公司资料")
        self.evidence.setPlainText("按分类查询；点击字段表头可排序，悬停表头查看源字段与口径。")
        self.status.setText(
            "资料服务已就绪；按分类手动读取，优先复用缓存。"
            if service
            else "公司资料需要猫爪数据源，请在设置中配置API Key并切换到猫爪。"
        )

    def enter(self, symbol=""):
        if symbol:
            try:
                normalized = research_stock_symbol(symbol)[-6:]
            except ValueError:
                self.status.setText("公司资料目前支持沪深A股")
                return
            if self.bundle and research_stock_symbol(self.bundle.request.symbol)[-6:] == normalized:
                return
            self.symbol_box.setText(normalized)
            self.sections.blockSignals(True)
            self.sections.setCurrentRow(0)
            self.sections.blockSignals(False)
            self.section = "overview"
            self.query()

    def _select_section(self, index):
        if index < 0:
            return
        self.section = self.sections.item(index).data(Qt.ItemDataRole.UserRole)
        if self.symbol_box.text().strip() and self.service:
            self.query()

    def _changed(self, *_):
        self.runner.cancel()
        self.active_request = None
        if self.service:
            self.status.setText("条件已变更，点击查询更新；当前内容仍属于上次查询。")

    def query(self, refresh=False):
        if not self.service:
            return
        request = CompanyRequest(
            self.symbol_box.text().strip(),
            self.date_box.date().toString("yyyyMMdd"),
            self.section,
            self.years_box.currentData(),
            self.period_box.currentData(),
            self.versions_box.isChecked(),
            refresh,
        )
        self.active_request = request
        self.status.setText("正在后台读取本分类；已有资料保留，切条件可取消。")
        self.runner.submit(str(request), None, partial(self.service.run, request))

    def _ready(self, bundle):
        if bundle.request != self.active_request:
            return
        self.render_bundle(bundle)

    def render_bundle(self, bundle):
        self.bundle = bundle
        symbol = research_stock_symbol(bundle.request.symbol)[-6:]
        for table in bundle.tables:
            if table.key == "basic" and table.rows:
                name = table.records()[0].get("name")
                if name:
                    self.names[symbol] = str(name)
        self.heading.setText(f"{symbol}  {self.names.get(symbol, '')} · 公司资料")
        self.matrix_box.setVisible(bundle.request.section == "statements")
        self.hide_empty_box.setVisible(bundle.request.section == "statements")
        self.link_button.setVisible(bundle.request.section == "announcements")
        self.export_button.setEnabled(True)
        self._show_facts(bundle.facts)
        self._show_tables()
        self.status.setText(" · ".join(bundle.notes))

    def _show_facts(self, facts):
        while self.card_grid.count():
            item = self.card_grid.takeAt(0)
            item.widget().deleteLater()
        self.cards = []
        for i, fact in enumerate(facts):
            frame = QFrame()
            frame.setObjectName("metric")
            box = QVBoxLayout(frame)
            box.setContentsMargins(10, 7, 10, 7)
            box.addWidget(text_label(fact.label, "eyebrow"))
            value = fact.value
            if isinstance(value, (int, float)):
                text = (
                    f"{value / 1e8:,.2f} 亿"
                    if fact.unit == "元" and abs(value) >= 1e8
                    else f"{value:,.3f}".rstrip("0").rstrip(".")
                )
            else:
                text = str(value) if value is not None else "源未提供"
            box.addWidget(text_label(text, "metricValue"))
            frame.setToolTip("来源表：" + fact.source_key + "；单位：" + (fact.unit or "不适用"))
            self.card_grid.addWidget(frame, i // 4, i % 4)
            self.cards.append(frame)
        self.card_area.setVisible(bool(facts))

    def current_view(self):
        return self.tabs.currentWidget()

    def _show_tables(self):
        if not self.bundle:
            return
        selected = self.current_view().table.key if self.current_view() else ""
        self.tabs.blockSignals(True)
        self.tabs.clear()
        keys = set()
        for original in self.bundle.tables:
            table = (
                statement_matrix(original, hide_empty=self.hide_empty_box.isChecked())
                if self.matrix_box.isChecked() and original.key in ("income", "balance", "cashflow")
                else original
            )
            keys.add(table.key)
            view = self.views.get(table.key)
            if view is None:
                view = ResearchDataView(table, natural_order=True)
                view.clicked.connect(lambda index, v=view: self._describe(v, index.row()))
                view.doubleClicked.connect(lambda index, v=view: self._activate(v, index.row()))
                self.views[table.key] = view
            view.update_table(table, self.filter_box.text())
            if table.columns and table.columns[0].key == "item":
                view.setColumnWidth(0, 255)
                view.setColumnWidth(1, 190)
                view.setColumnWidth(2, 100)
            index = self.tabs.addTab(view, table.title)
            if table.key == selected:
                self.tabs.setCurrentIndex(index)
        for key in tuple(self.views):
            if key not in keys:
                self.views.pop(key).deleteLater()
        self.tabs.blockSignals(False)
        self._tab_changed()

    def _raw_table(self):
        view = self.current_view()
        return (
            next((t for t in self.bundle.tables if view and t.key == view.table.key), None)
            if self.bundle
            else None
        )

    def _tab_changed(self, *_):
        table = self._raw_table()
        self.field_box.blockSignals(True)
        previous = self.field_box.currentData()
        key = table.key if table else None
        if getattr(self, "_metric_table_key", None) != key:
            previous = None
        self._metric_table_key = key
        self.field_box.clear()
        if table and any(c.key == "report_date" for c in table.columns):
            records = table.records()
            for col in table.columns:
                if (
                    col.key not in IDENTITY_FIELDS
                    and any(numeric(r.get(col.key)) is not None for r in records)
                    and not col.description.startswith("源新增字段")
                ):
                    self.field_box.addItem(col.caption, col.key)
        preferred = previous or {
            "indicator": "s_fa_eps_basic",
            "income": "revenue_inc",
            "balance": "tot_assets",
            "cashflow": "net_cash_flows_oper_act",
            "holder_number": "shareholder",
        }.get(table.key if table else "")
        i = self.field_box.findData(preferred)
        if i >= 0:
            self.field_box.setCurrentIndex(i)
        self.field_box.blockSignals(False)
        self.curve_area.setVisible(self.field_box.count() > 0)
        self._describe(self.current_view())
        self._plot()

    def _describe(self, view, row=None):
        if view:
            record = view.record(row) if row is not None else None
            self.evidence.setPlainText(evidence_text(view.table, record))
            self.link_button.setEnabled(bool(record and external_url(record.get("content_url"))))

    def _filter(self, text):
        view = self.current_view()
        if view:
            view.update_table(view.table, text)

    def _plot(self, *_):
        if not hasattr(self, "plot"):
            return
        table = self._raw_table()
        series = (
            company_series(table, self.field_box.currentData(), self.bundle.request.all_versions)
            if table and self.bundle
            else ()
        )
        choices = [title for title, _ in series]
        if [self.scope_box.itemData(i) for i in range(1, self.scope_box.count())] != choices:
            old = self.scope_box.currentData()
            self.scope_box.blockSignals(True)
            self.scope_box.clear()
            self.scope_box.addItem("全部股东／口径", None)
            for title in choices:
                self.scope_box.addItem(title, title)
            index = self.scope_box.findData(old)
            self.scope_box.setCurrentIndex(1 if old is None and len(choices) > 3 else max(0, index))
            self.scope_box.blockSignals(False)
        selected = self.scope_box.currentData()
        self.scope_box.setToolTip(str(selected or "显示所有已确认可绘制的口径"))
        if selected:
            series = tuple(s for s in series if s[0] == selected)
        self.axis.dates = tuple(sorted({day for _, points in series for day, _ in points}))
        self.axis.picture = None
        self.axis.update()
        lookup = {day: i for i, day in enumerate(self.axis.dates)}
        while len(self.curves) < min(8, len(series)):
            self.curves.append(
                self.plot.plot(
                    pen=pg.mkPen(COLORS[len(self.curves) % len(COLORS)], width=1.5),
                    symbol="o",
                    symbolSize=4,
                )
            )
        legend = self.plot.plotItem.legend
        legend.clear()
        legend.setVisible(len(series) > 1)
        for i, curve in enumerate(self.curves):
            if i < min(8, len(series)):
                title, points = series[i]
                curve.setData([lookup[d] for d, _ in points], [v for _, v in points])
                legend.addItem(curve, title)
            else:
                curve.setData([], [])
        self.plot.enableAutoRange()
        self.curve_note.setText(
            "源报表原值，可能为累计；并列版本不连线。"
            + ("仅绘制前8个口径。" if len(series) > 8 else "")
        )

    def _activate(self, view, row):
        record = view.record(row)
        if external_url(record.get("content_url")):
            self._open_link(record)

    def _open_link(self, record=None):
        if not isinstance(record, dict):
            view = self.current_view()
            rows = view.selectionModel().selectedRows() if view else ()
            record = view.record(rows[0].row()) if rows else {}
        value = external_url(record.get("content_url"))
        if value:
            self.url_opener(QUrl(value))

    def _open_market(self):
        request = self.bundle.request if self.bundle else self.active_request
        if request:
            self.stock_requested.emit(request.symbol, request.as_of, "")

    def _open_theme(self):
        request = self.bundle.request if self.bundle else self.active_request
        if request:
            self.theme_requested.emit(request.symbol)

    def _export(self):
        if self.bundle:
            path, _ = QFileDialog.getSaveFileName(
                self,
                "导出公司资料",
                self.bundle.request.symbol + "-公司资料.md",
                "Markdown (*.md);;JSON (*.json)",
            )
            if path:
                try:
                    export_company(self.bundle, path)
                    self.status.setText("已导出来源、披露日期、版本、字段口径及原始资料。")
                except (OSError, ValueError) as exc:
                    self.status.setText("导出失败：" + str(exc))

    def _back(self):
        self.runner.cancel()
        self.back_requested.emit()

    def hideEvent(self, event):
        if hasattr(self, "runner"):
            self.runner.cancel()
        super().hideEvent(event)

    def shutdown(self):
        self.runner.shutdown()
