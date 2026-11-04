"""独立预测研究视图；可终止进程执行，模型统计不冒充校准概率。"""

from datetime import datetime
from functools import partial
from math import exp

from PySide6.QtCore import QDate
from PySide6.QtWidgets import (
    QCheckBox,
    QDateEdit,
    QFileDialog,
    QFormLayout,
    QHBoxLayout,
    QLabel,
    QPushButton,
    QSpinBox,
    QTableWidget,
    QTableWidgetItem,
    QTextBrowser,
    QVBoxLayout,
    QWidget,
)

from chengchuan.application.forecast_features import history_at
from chengchuan.domain.forecast import ForecastConfig
from chengchuan.domain.models import SHANGHAI
from chengchuan.ui.tasks import ProcessTaskRunner


class ForecastPanel(QWidget):
    def __init__(self, service, parent=None, runner_factory=None, report_writer=None):
        super().__init__(parent)
        self.service = service
        self.report_writer = report_writer
        self._context_key = None
        self.snapshot = self.run = None
        self._selection_day = None
        self.observed_at = self.quote_at = None
        self.runner = (
            runner_factory(self)
            if runner_factory
            else ProcessTaskRunner(self, timeout_ms=120000, task_label="预测研究")
        )
        self.runner.ready.connect(self._ready)
        self.runner.failed.connect(self._failed)
        self.runner.busy.connect(self._busy)
        layout = QVBoxLayout(self)
        layout.setContentsMargins(0, 8, 0, 0)
        layout.setSpacing(10)
        self.notice = QLabel(
            "量价与阶段融合 · 研究模型尚未校准\n原始收盘价口径，除权除息尚未系统核验"
        )
        self.notice.setObjectName("notice")
        self.notice.setWordWrap(True)
        layout.addWidget(self.notice)
        form = QFormLayout()
        self.asof = QDateEdit(QDate.currentDate())
        self.asof.setCalendarPopup(True)
        self.asof.setDisplayFormat("yyyy-MM-dd")
        self.before = QDateEdit(QDate.currentDate())
        self.before.setCalendarPopup(True)
        self.before.setDisplayFormat("yyyy-MM-dd")
        self.follow = QCheckBox("基准日跟随Ctrl选区末日")
        self.follow.toggled.connect(self._follow_selection)
        self.neighbors = QSpinBox()
        self.neighbors.setRange(3, 40)
        self.neighbors.setValue(21)
        form.addRow("预测基准日D", self.asof)
        form.addRow("回测目标早于", self.before)
        form.addRow("相似日数量", self.neighbors)
        layout.addLayout(form)
        layout.addWidget(self.follow)
        buttons = QHBoxLayout()
        self.predict_button = QPushButton("计算方向")
        self.validate_button = QPushButton("逐日验证")
        self.cancel_button = QPushButton("取消")
        self.predict_button.clicked.connect(self.predict)
        self.validate_button.clicked.connect(self.backtest)
        self.cancel_button.clicked.connect(self.cancel)
        for b in (self.predict_button, self.validate_button, self.cancel_button):
            buttons.addWidget(b)
        layout.addLayout(buttons)
        self.status = QLabel("打开日K后，计算研究方向或验证历史表现")
        self.status.setWordWrap(True)
        layout.addWidget(self.status)
        self.output = QTextBrowser()
        self.output.setMinimumHeight(150)
        self.output.setOpenExternalLinks(False)
        layout.addWidget(self.output, 2)
        self.table = QTableWidget(0, 5)
        self.table.setHorizontalHeaderLabels(["D", "目标", "预测", "真实", "命中"])
        self.table.setEditTriggers(QTableWidget.EditTrigger.NoEditTriggers)
        self.table.setMaximumHeight(210)
        self.table.setToolTip("展示C组最近40条；完整A/B/C记录可导出")
        layout.addWidget(self.table, 1)
        self.export_button = QPushButton("导出报告与完整对照表")
        self.export_button.setEnabled(False)
        self.export_button.setVisible(report_writer is not None)
        self.export_button.clicked.connect(self.export)
        layout.addWidget(self.export_button)
        for widget in (self.asof, self.before, self.neighbors):
            signal = widget.dateChanged if isinstance(widget, QDateEdit) else widget.valueChanged
            signal.connect(self._invalidate)
        self._set_enabled(False)

    def _set_enabled(self, enabled):
        self.predict_button.setEnabled(enabled)
        self.validate_button.setEnabled(enabled)

    def set_context(self, snapshot, observed_at=None, quote_at=None):
        enabled = snapshot is not None and snapshot.series.interval == "day"
        key = None
        if enabled:
            try:
                closed = self.service.latest_date(
                    snapshot, datetime.now(SHANGHAI), observed_at, quote_at
                )
                key = (snapshot.series.symbol, history_at(snapshot, closed).revision)
            except ValueError:
                key = (snapshot.series.symbol, "no_completed_session")
        if self.snapshot and snapshot and key == self._context_key:
            self.snapshot = snapshot
            self.observed_at, self.quote_at = observed_at, quote_at
            return
        self._invalidate()
        self._selection_day = None
        self._context_key = key
        self.snapshot = snapshot
        self.observed_at, self.quote_at = observed_at, quote_at
        self._set_enabled(enabled)
        if not enabled:
            self.status.setText("切换到真实日K后使用预测研究；分钟采样不生成分钟K线特征")
            return
        try:
            day = self.service.latest_date(snapshot, datetime.now(SHANGHAI), observed_at, quote_at)
            self.asof.setDate(QDate(day.year, day.month, day.day))
        except ValueError as exc:
            self.status.setText(str(exc))
        else:
            self.status.setText(
                f"{snapshot.series.name} · 日线{len(snapshot.series.bars)}条；按按钮运行，不随行情刷新训练"
            )

    def set_selection_date(self, day):
        self._selection_day = day
        if self.follow.isChecked():
            self.asof.setDate(QDate(day.year, day.month, day.day))

    def _follow_selection(self, enabled):
        if enabled and self._selection_day:
            self.set_selection_date(self._selection_day)

    def _invalidate(self, *args):
        self.runner.cancel()
        self.run = None
        self.export_button.setEnabled(False)
        self.output.clear()
        self.table.setRowCount(0)
        self.status.setText("参数已更新，按按钮重新计算")

    def _config(self):
        return ForecastConfig(neighbors=self.neighbors.value())

    def predict(self):
        if not self.snapshot:
            return
        self._invalidate()
        self.status.setText("正在计算截至D的量价特征与历史相似日…")
        self.runner.submit(
            self.snapshot.revision,
            None,
            partial(
                self.service.predict,
                self.snapshot,
                self.asof.date().toPython(),
                self._config(),
                observed_at=self.observed_at,
                quote_at=self.quote_at,
            ),
        )

    def backtest(self):
        if not self.snapshot:
            return
        self._invalidate()
        self.status.setText("正在逐日验证A/B/C：先保存预测，再对照真实结果…")
        self.runner.submit(
            self.snapshot.revision,
            None,
            partial(
                self.service.backtest, self.snapshot, self.before.date().toPython(), self._config()
            ),
        )

    def _busy(self, busy):
        self.cancel_button.setEnabled(busy)

    def cancel(self):
        self.runner.cancel()
        self.status.setText("研究任务已取消，已保存的预测不会被覆盖")

    def _failed(self, error):
        self.status.setText("本次研究未完成：" + error)

    def _ready(self, run):
        if isinstance(run, dict) and "json" in run:
            self.status.setText("已导出JSON、Markdown及完整CSV对照表")
            return
        self.run = run
        self.export_button.setEnabled(True)
        text = ["研究诊断 · 公开校准概率未启用", ""]
        if run.features:
            features = dict(run.features.values)
            for n in (1, 2, 5, 20):
                value = features.get(f"volume_rv{n}")
                text.append(
                    f"较前{n}日均量：{exp(value):.2f}倍"
                    if value is not None
                    else f"较前{n}日均量：缺失"
                )
            for n in (20, 60):
                value = features.get(f"price_position_{n}")
                text.append(
                    f"此前{n}日位置：{value:.1%}" if value is not None else f"此前{n}日位置：缺失"
                )
            for p in run.predictions:
                name = {1: "上涨倾向", -1: "下跌倾向", 0: "不变倾向", None: "不预测"}[p.direction]
                text.extend(
                    [
                        "",
                        f"H{p.horizon} · {p.target or '日历未覆盖'}：{name}",
                        p.reason,
                        f"可用训练样本：{p.train_count}",
                    ]
                )
                if p.neighbor_counts:
                    down, flat, up = p.neighbor_counts
                    text.append(f"相似日实际分布：涨{up}／平{flat}／跌{down}（非未来概率）")
        else:
            for s in run.summaries:

                def fmt(x):
                    return "—" if x is None else f"{x:.1%}"

                text.extend(
                    [
                        f"{s['group']}组 / H{s['horizon']}：{s['scored']}条可评价方向",
                        f"命中 {fmt(s['accuracy'])}；同样本全猜涨 {fmt(s['always_up_accuracy'])}",
                        f"预测覆盖 {fmt(s['forecast_coverage'])}；共同样本命中 {fmt(s['common_accuracy'])}",
                        "",
                    ]
                )
            evaluations = {r.prediction_id: r for r in run.evaluations}
            selected = [p for p in run.predictions if p.group == "C" and p.model_id][-40:]
            self.table.setRowCount(len(selected))
            for i, p in enumerate(selected):
                e = evaluations[p.record_id]
                direction_names = {1: "涨", -1: "跌", 0: "平", None: "—"}
                values = [
                    p.as_of,
                    p.target or "—",
                    direction_names[p.direction],
                    direction_names[e.actual_direction],
                    "是" if e.correct is True else ("否" if e.correct is False else "不可评"),
                ]
                for j, value in enumerate(values):
                    self.table.setItem(i, j, QTableWidgetItem(value))
            self.table.resizeColumnsToContents()
        text.extend(["", *run.notes])
        self.output.setPlainText("\n".join(text))
        self.status.setText("研究完成 · 原始价格工程诊断；完整记录已归档")

    def export(self):
        if self.run is None:
            return
        path, _ = QFileDialog.getSaveFileName(
            self, "导出预测研究报告", "forecast_research", "报告前缀 (*)"
        )
        if path:
            # Composition supplies a writer, keeping UI independent of infrastructure.
            self.export_requested(path)

    def export_requested(self, path):
        if self.report_writer and self.run:
            self.status.setText("正在导出报告和完整对照表…")
            self.runner.submit(
                "export:" + self.run.experiment_id,
                None,
                partial(self.report_writer, self.run, path),
            )

    def shutdown(self):
        self.runner.shutdown()
