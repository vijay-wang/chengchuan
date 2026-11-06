"""Explicit next-session CPU research; no training on quote refresh."""

from datetime import datetime, time, timedelta
from functools import partial

from PySide6.QtCore import QDate
from PySide6.QtWidgets import (
    QComboBox,
    QDateEdit,
    QFormLayout,
    QFrame,
    QLineEdit,
    QScrollArea,
    QToolButton,
    QWidget,
)

from chengchuan.application.forecast_features import bar_date, history_at
from chengchuan.application.nextday_presentation import (
    DIRECTION_NAMES,
    prediction_text,
    summary_text,
)
from chengchuan.domain.models import SHANGHAI
from chengchuan.domain.nextday import NextDayConfig
from chengchuan.ui.forecast_panel import ForecastPanel
from chengchuan.ui.tasks import ProcessTaskRunner


class NextDayPanel(ForecastPanel):
    """Reuse cancellable task/export plumbing while keeping new targets and output separate."""

    def __init__(self, service, parent=None, runner_factory=None, report_writer=None):
        runner_factory = runner_factory or (
            lambda owner: ProcessTaskRunner(owner, timeout_ms=300000, task_label="次日预测研究")
        )
        super().__init__(service, parent, runner_factory, report_writer)
        self.notice.setText(
            "下一交易日 · CPU多因子研究\n量价、支撑压力、成交分布及行为代理；尚未经独立验证与校准"
        )
        self.mode = QComboBox()
        self.mode.addItem("盘后预测下一交易日", "close")
        self.mode.addItem("14:35预测下一交易日", "cut1435")
        self.start = QDateEdit(QDate.currentDate().addMonths(-6))
        self.start.setCalendarPopup(True)
        self.start.setDisplayFormat("yyyy-MM-dd")
        self.excluded = QLineEdit()
        self.excluded.setPlaceholderText("例如2026-06-12，多个日期用逗号分隔")
        self.excluded.setToolTip("原始价格未系统复权；可排除已知除权除息及异常日期，不自动填零")
        # Existing controls retain their familiar placement and keyboard behavior.
        form = next(
            self.layout().itemAt(i).layout()
            for i in range(self.layout().count())
            if isinstance(self.layout().itemAt(i).layout(), QFormLayout)
        )
        form.insertRow(0, "预测时点", self.mode)
        form.insertRow(2, "回测起始日D", self.start)
        form.addRow("排除日期", self.excluded)
        self.advanced = QToolButton()
        self.advanced.setText("展开回测与模型参数")
        self.advanced.setCheckable(True)
        self.advanced.setMinimumHeight(26)
        self.layout().insertWidget(2, self.advanced)
        self._advanced_fields = (self.start, self.before, self.neighbors, self.excluded)
        for widget in (self.mode, self.asof, *self._advanced_fields):
            widget.setMinimumHeight(max(32, widget.sizeHint().height()))
        for widget in self._advanced_fields:
            form.setRowVisible(widget, False)
        self.layout().removeItem(form)
        form_holder = QWidget()
        form_holder.setLayout(form)
        self.form_scroll = QScrollArea()
        self.form_scroll.setFrameShape(QFrame.Shape.NoFrame)
        self.form_scroll.setWidgetResizable(True)
        self.form_scroll.setWidget(form_holder)
        self.form_scroll.setFixedHeight(96)
        self.layout().insertWidget(1, self.form_scroll)

        def advanced_toggled(checked):
            for widget in self._advanced_fields:
                form.setRowVisible(widget, checked)
            self.form_scroll.setFixedHeight(220 if checked else 96)
            self.advanced.setText("收起回测与模型参数" if checked else "展开回测与模型参数")

        self.advanced.toggled.connect(advanced_toggled)
        self.mode.currentIndexChanged.connect(self._mode_changed)
        self.start.dateChanged.connect(self._invalidate)
        self.excluded.textChanged.connect(self._invalidate)
        self.predict_button.setText("预测下一交易日")
        self.validate_button.setText("历史逐日验证")
        self.table.setToolTip("多因子组最近40条记录，包含未给方向的记录；完整各组可导出")
        self.follow.setChecked(False)
        self.table.hide()

    def _config(self):
        dates = tuple(
            value.strip()
            for value in self.excluded.text().replace("，", ",").split(",")
            if value.strip()
        )
        return NextDayConfig(neighbors=self.neighbors.value(), excluded_dates=dates)

    def _mode_changed(self, *_):
        self._context_key = None
        self._invalidate()
        if self.snapshot:
            self.set_context(self.snapshot, self.observed_at, self.quote_at)

    def _latest(self, snapshot, now, observed_at, quote_at):
        if self.mode.currentData() == "close":
            return self.service.latest_date(snapshot, now, observed_at, quote_at)
        local = now.astimezone(SHANGHAI)
        eligible = [
            bar_date(bar)
            for bar in snapshot.series.bars
            if bar_date(bar) < local.date()
            or (bar_date(bar) == local.date() and local.time() >= time(14, 35))
        ]
        if not eligible:
            raise ValueError("尚无已到14:35的基准日")
        return eligible[-1]

    def set_context(self, snapshot, observed_at=None, quote_at=None):
        enabled = snapshot is not None and snapshot.series.interval == "day"
        key, day, error = None, None, None
        if enabled:
            try:
                day = self._latest(snapshot, datetime.now(SHANGHAI), observed_at, quote_at)
                cutoff_day = day if self.mode.currentData() == "close" else day - timedelta(days=1)
                prefix = history_at(snapshot, cutoff_day)
                key = (snapshot.series.symbol, self.mode.currentData(), day, prefix.revision)
            except ValueError as exc:
                error = str(exc)
                key = (snapshot.series.symbol, self.mode.currentData(), "unavailable")
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
            self.status.setText("切换到真实日K后使用下一交易日预测")
            return
        if day:
            self.asof.setDate(QDate(day.year, day.month, day.day))
        if error:
            self.status.setText(error)
            return
        suffix = (
            "14:35模式需对应历史分钟数据；缺失时明确不预测"
            if self.mode.currentData() == "cut1435"
            else "点击按钮计算，不随行情刷新反复训练"
        )
        self.status.setText(f"{snapshot.series.name} · 日线{len(snapshot.series.bars)}条；{suffix}")

    def predict(self):
        if not self.snapshot:
            return
        self._invalidate()
        try:
            config = self._config()
        except ValueError as exc:
            self.status.setText("配置无效：" + str(exc))
            return
        self.status.setText("正在计算可用历史特征与CPU模型…")
        self.runner.submit(
            self.snapshot.revision,
            None,
            partial(
                self.service.predict,
                self.snapshot,
                self.asof.date().toPython(),
                config,
                observed_at=self.observed_at,
                quote_at=self.quote_at,
                mode=self.mode.currentData(),
            ),
        )

    def backtest(self):
        if not self.snapshot:
            return
        if self.start.date() >= self.before.date():
            self.status.setText("回测起始日须早于目标上界")
            return
        self._invalidate()
        try:
            config = self._config()
        except ValueError as exc:
            self.status.setText("配置无效：" + str(exc))
            return
        self.status.setText("正在逐日验证价格、量价和多因子模型；先保存预测，再评价…")
        self.runner.submit(
            self.snapshot.revision,
            None,
            partial(
                self.service.backtest,
                self.snapshot,
                self.before.date().toPython(),
                config,
                start=self.start.date().toPython(),
                mode=self.mode.currentData(),
            ),
        )

    def _ready(self, run):
        if isinstance(run, dict) and "json" in run:
            self.status.setText("已导出JSON、Markdown及完整CSV对照表")
            return
        self.run = run
        self.table.setVisible(not bool(run.features))
        self.export_button.setEnabled(self.report_writer is not None)
        lines = ["下一交易日研究 · 历史频率与模型评分不是校准概率", ""]
        if run.features:
            for prediction in run.predictions:
                lines += prediction_text(prediction) + [""]
            feature = run.features
            lines += [f"输入覆盖到：{feature.coverage_at or '—'}", *feature.evidence]
            if feature.levels:
                lines += ["", "历史结构参考价（不是保证支撑／压力）："]
                lines += [f"  {name}：{value:.3f}" for name, value in feature.levels]
            lines += ["", *feature.notes]
        else:
            for summary in run.summaries:
                lines += summary_text(summary) + [""]
            self._show_history(run)
        lines += ["", *run.notes]
        self.output.setPlainText("\n".join(lines))
        self.status.setText("研究完成 · CPU计算；预测与对照记录可导出")

    def _show_history(self, run):
        from PySide6.QtWidgets import QTableWidgetItem

        evaluations = {row.prediction_id: row for row in run.evaluations}
        selected = [row for row in run.predictions if row.group == "full"][-40:]
        self.table.setRowCount(len(selected))
        for index, prediction in enumerate(selected):
            actual = evaluations.get(prediction.record_id)
            correct = actual.correct if actual else None
            values = [
                prediction.as_of,
                prediction.target or "—",
                DIRECTION_NAMES[prediction.direction],
                {1: "涨", -1: "跌", 0: "平", None: "—"}[
                    actual.actual_direction if actual else None
                ],
                "是" if correct is True else ("否" if correct is False else "不可评"),
            ]
            for column, value in enumerate(values):
                item = QTableWidgetItem(value)
                item.setToolTip(
                    prediction.reason if column == 2 else (actual.reason if actual else "")
                )
                self.table.setItem(index, column, item)
        self.table.resizeColumnsToContents()
