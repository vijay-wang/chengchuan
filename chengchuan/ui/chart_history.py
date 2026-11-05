"""Debounce chart-edge gestures; keep networking outside paint, hover and range fitting."""

from functools import partial

from PySide6.QtCore import QObject, QTimer, Signal

from chengchuan.application.chart_history import chart_offset, load_chart_page, merge_chart_frames
from chengchuan.ui.tasks import ProcessTaskRunner


class ChartHistoryController(QObject):
    status = Signal(str)

    def __init__(self, chart, apply, runner_factory=ProcessTaskRunner, parent=None):
        super().__init__(parent)
        self.chart, self.apply = chart, apply
        self.market = self.frame = self.desired = None
        self.expanded = False
        self.blocked = set()
        self.errors = set()
        self.active = None
        self.budget = 0
        self.runner = runner_factory(self)
        self.runner.timeout_ms = 120000
        self.runner.ready.connect(self._ready)
        self.runner.failed.connect(self._failed)
        self.timer = QTimer(self)
        self.timer.setSingleShot(True)
        self.timer.setInterval(180)
        self.timer.timeout.connect(self._next)
        chart.history_requested.connect(self.request)

    def reset(self):
        self.timer.stop()
        self.runner.cancel()
        self.frame = self.desired = self.active = None
        self.blocked.clear()
        self.errors.clear()
        self.expanded = False
        self.budget = 0

    def set_frame(self, market, frame):
        self.market, self.frame = market, frame

    def request(self, bounds):
        if self.frame is None or self.market is None:
            return
        if not getattr(self.market, "supports_history", False):
            self.status.emit("当前源仅能浏览已取得数据；相邻日期补数请切换猫爪数据。")
            return
        series = self.frame.snapshot.series
        if bounds[0] >= -0.51 and bounds[1] <= len(series.bars) - 0.49:
            self.timer.stop()
            self.runner.cancel()
            self.desired = self.active = None
            return
        self.blocked.difference_update(self.errors)
        self.errors.clear()
        self.desired = (series, tuple(bounds))
        self.budget = 4
        self.timer.start()

    def _bounds(self):
        old, bounds = self.desired
        shift = chart_offset(old, self.frame.snapshot.series)
        return bounds[0] + shift, bounds[1] + shift

    def _next(self):
        if self.active or not self.desired or not self.frame or self.budget <= 0:
            return
        left, right = self._bounds()
        count = len(self.frame.snapshot.series.bars)
        directions = []
        if left < -0.51:
            directions.append("older")
        if right > count - 0.49 and self.frame.as_of is not None:
            directions.append("newer")
        direction = next((d for d in directions if d not in self.blocked), None)
        if direction is None:
            self.chart.set_view_range(left, right)
            return
        self.active = direction
        self.budget -= 1
        self.status.emit("正在补充相邻交易日行情，已有图表保持显示…")
        self.runner.submit(
            direction, None, partial(load_chart_page, self.market, self.frame, direction)
        )

    def _ready(self, page):
        direction, self.active = self.active, None
        if not self.frame or not direction:
            return
        try:
            old = self.frame
            merged = merge_chart_frames(old, page, newer=direction == "newer") if page else old
            count = len(merged.snapshot.series.bars) - len(old.snapshot.series.bars)
            if count <= 0:
                self.blocked.add(direction)
                self.status.emit("已到当前可用数据边界；图表贴合已有数据。")
            else:
                self.expanded = True
                self.frame = merged
                self.apply(merged)
                self.status.emit(f"已补充{count}条相邻行情；继续缩小或拖动可加载更多。")
            if self.desired:
                self.chart.set_view_range(*self._bounds())
                self.timer.start()
        except (ValueError, RuntimeError) as exc:
            self._failed(str(exc), direction)

    def _failed(self, message, direction=None):
        direction = direction or self.active
        self.active = None
        if direction:
            self.blocked.add(direction)
            self.errors.add(direction)
        self.status.emit("相邻数据暂不可用，保留已有行情：" + message[:180])

    def shutdown(self):
        self.timer.stop()
        self.runner.shutdown()
