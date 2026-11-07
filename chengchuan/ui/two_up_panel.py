"""Replacement product entry: exactly-two-up, third-session direction only."""

from functools import partial

from chengchuan.application.two_up_presentation import prediction_text, summary_text
from chengchuan.domain.two_up import TwoUpConfig
from chengchuan.ui.nextday_panel import NextDayPanel


class TwoUpPanel(NextDayPanel):
    def __init__(self, service, parent=None, runner_factory=None, report_writer=None):
        super().__init__(service, parent, runner_factory, report_writer)
        self.notice.setText("恰好连涨两天 → 第三天涨跌\n目标命中率80%；允许拒判，同时报告覆盖率")
        form = self.form_scroll.widget().layout()
        form.setRowVisible(self.mode, False)
        form.setRowVisible(self.neighbors, False)
        self.mode.setEnabled(False)
        self._advanced_fields = (self.start, self.before, self.excluded)
        self.predict_button.setText("预测第三天")
        self.validate_button.setText("验证事件")
        self.advanced.setText("回测日期与排除项")
        self.table.setToolTip("仅展示已满足条件的最近40个事件；输入不足和拒判均保留")
        label = form.labelForField(self.asof)
        if label:
            label.setText("第二天收盘日")
        self.excluded.setToolTip(
            "可比昨收已处理价格可比口径；明确异常日期仍可排除，排除事件计入覆盖分母"
        )

    def _config(self):
        values = tuple(
            x.strip() for x in self.excluded.text().replace("，", ",").split(",") if x.strip()
        )
        return TwoUpConfig(excluded_dates=values)

    def set_context(self, snapshot, observed_at=None, quote_at=None):
        super().set_context(snapshot, observed_at, quote_at)
        if snapshot and snapshot.series.interval == "day" and self.run is None:
            self.status.setText("先确认前日不涨、随后两天收涨；第二天收盘后计算，不自动训练")

    def predict(self):
        if not self.snapshot:
            return
        self._invalidate()
        try:
            config = self._config()
        except ValueError as exc:
            self.status.setText(str(exc))
            return
        self.status.setText("正在检查恰好两连涨条件及历史80%信号门槛…")
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
            ),
        )

    def backtest(self):
        if not self.snapshot:
            return
        self._invalidate()
        try:
            config = self._config()
        except ValueError as exc:
            self.status.setText(str(exc))
            return
        self.status.setText("正在按历史时点验证；所有成立事件保留在覆盖率分母…")
        self.runner.submit(
            self.snapshot.revision,
            None,
            partial(
                self.service.backtest,
                self.snapshot,
                self.before.date().toPython(),
                config,
                start=self.start.date().toPython(),
            ),
        )

    def _ready(self, run):
        if isinstance(run, dict):
            self.status.setText("已导出JSON、Markdown和CSV事件对照表")
            return
        self.run = run
        self.export_button.setEnabled(self.report_writer is not None)
        self.table.setVisible(bool(run.evaluations))
        lines = []
        if run.evaluations:
            for summary in run.summaries:
                lines += summary_text(summary) + [""]
            self._show_events(run)
        else:
            for prediction in run.predictions:
                lines += prediction_text(prediction) + [""]
            if run.features:
                lines += list(run.features.evidence)
                lines += [f"{name}：{price:.3f}" for name, price in run.features.levels]
        lines += ["", *run.notes]
        self.output.setPlainText("\n".join(lines))
        self.status.setText("事件研究完成；80%为验收目标，不是默认已达到")

    def _show_events(self, run):
        from PySide6.QtWidgets import QTableWidgetItem

        outcomes = {e.prediction_id: e for e in run.evaluations}
        rows = [p for p in run.predictions if p.status not in ("not_event", "event_unknown")][-40:]
        self.table.setRowCount(len(rows))
        names = {1: "涨", -1: "跌", 0: "平", None: "未预测"}
        for i, p in enumerate(rows):
            e = outcomes[p.record_id]
            for j, text in enumerate(
                (
                    p.as_of,
                    p.target or "—",
                    names[p.direction],
                    names[e.actual_direction],
                    "是" if e.correct is True else "否" if e.correct is False else "—",
                )
            ):
                item = QTableWidgetItem(text)
                item.setToolTip(p.reason)
                self.table.setItem(i, j, item)
        self.table.resizeColumnsToContents()
