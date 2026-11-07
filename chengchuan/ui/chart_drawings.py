"""Editable chart annotations; chart coordinates never become persisted indices."""

from dataclasses import replace

import pyqtgraph as pg
from PySide6.QtCore import QEvent, QObject, QPointF, Qt, QTimer, Signal
from PySide6.QtGui import QKeySequence, QShortcut
from PySide6.QtWidgets import QComboBox, QHBoxLayout, QLabel, QPushButton, QSizePolicy, QWidget

from chengchuan.application.drawings import DrawingBook
from chengchuan.application.errors import StorageError
from chengchuan.domain.drawings import TOOLS, Drawing, DrawingPoint, price_difference

TOOL_NAMES = (
    ("browse", "浏览 / 选择"),
    ("horizontal", "水平线"),
    ("vertical", "垂直线"),
    ("trend", "趋势线"),
    ("rectangle", "矩形"),
    ("price_range", "双线测距"),
)
COLORS = ("#e4bb66", "#67c7f2")


class DrawingVisual:
    def __init__(self, controller, record, preview=False):
        self.controller, self.record, self.preview = controller, record, preview
        self.items, self.lines = [], []
        self.roi = self.text = self.connector = None
        self.signature = None
        pen = pg.mkPen(
            COLORS[0], width=1.5, style=Qt.PenStyle.DashLine if preview else Qt.PenStyle.SolidLine
        )
        if record.tool in ("horizontal", "vertical", "price_range"):
            for i in range(len(record.points)):
                line = pg.InfiniteLine(
                    angle=90 if record.tool == "vertical" else 0,
                    movable=not preview,
                    pen=pg.mkPen(COLORS[i], width=1.5),
                    hoverPen=pg.mkPen("#eef6ff", width=2),
                    label=("A " if i == 0 else "B ") + "{value:.3f}"
                    if record.tool == "price_range"
                    else "{value:.3f}"
                    if record.tool == "horizontal"
                    else None,
                    labelOpts={"position": 0.08, "color": COLORS[i], "fill": (12, 23, 35, 165)},
                )
                line.sigClicked.connect(self._clicked)
                line.sigDragged.connect(self._moving)
                line.sigPositionChangeFinished.connect(self._finished)
                self.lines.append(line)
                self.items.append(line)
            if record.tool == "price_range":
                self.text = pg.TextItem(
                    color="#edf5ff", anchor=(1, 0.5), fill=(10, 20, 31, 190), border="#60738a"
                )
                self.connector = pg.PlotDataItem(
                    pen=pg.mkPen(COLORS[0], style=Qt.PenStyle.DashLine)
                )
                self.items += [self.text, self.connector]
        else:
            args = dict(pen=pen, hoverPen=pg.mkPen("#eef6ff", width=2), movable=not preview)
            self.roi = (
                pg.LineSegmentROI([[0, 1], [1, 1]], **args)
                if record.tool == "trend"
                else pg.RectROI([0, 1], [1, 1], **args)
            )
            self.roi.setAcceptedMouseButtons(
                Qt.MouseButton.NoButton if preview else Qt.MouseButton.LeftButton
            )
            self.roi.sigClicked.connect(self._clicked)
            self.roi.sigRegionChangeFinished.connect(self._finished)
            self.items.append(self.roi)
        for item in self.items:
            item.setZValue(65 if preview else 70)
            if not preview:
                item.drawing_id = record.id
            if preview or item in (self.text, self.connector):
                item.setAcceptedMouseButtons(Qt.MouseButton.NoButton)

    def _clicked(self, item, event):
        if not self.preview:
            self.controller.select(self.record.id)
            event.accept()

    def _moving(self, *args):
        if not self.controller.syncing:
            self.controller.select(self.record.id)
            self.position_label()

    def _finished(self, *args):
        if self.preview or self.controller.syncing:
            return
        c = self.controller
        if self.record.id not in c.book.items or not c.snapshot:
            return
        try:
            if self.lines:
                points = tuple(
                    DrawingPoint(p.timestamp, line.value())
                    if self.record.tool != "vertical"
                    else c.point(QPointF(line.value(), p.price))
                    for p, line in zip(self.record.points, self.lines)
                )
            elif self.record.tool == "trend":
                points = tuple(c.point(self.roi.mapToParent(p)) for p in self.roi.listPoints())
            else:
                pos, size = self.roi.pos(), self.roi.size()
                points = (c.point(pos), c.point(pos + size))
            c.book.put(replace(self.record, points=points))
            c.select(self.record.id)
            c.render()
            c.schedule_save()
        except (TypeError, ValueError) as exc:
            c.message.emit(str(exc))
            self.signature = None
            c.render()

    def sync(self, record):
        self.record = record
        c = self.controller
        if record.tool in ("horizontal", "price_range"):
            xs = ()
        else:
            xs = tuple(c.indices.get(p.timestamp) for p in record.points)
        visible = all(x is not None for x in xs)
        for item in self.items:
            if item.scene() is None:
                c.chart.price.addItem(item, ignoreBounds=True)
            item.setVisible(visible)
        signature = (record, xs, len(c.snapshot.series.bars) if record.tool == "vertical" else None)
        if visible and self.signature != signature:
            if self.lines:
                for i, (line, p) in enumerate(zip(self.lines, record.points)):
                    line.setBounds(
                        (-0.49, len(c.snapshot.series.bars) - 0.51)
                        if record.tool == "vertical"
                        else (1e-9, 1e12)
                    )
                    line.setValue(xs[i] if record.tool == "vertical" else p.price)
            elif record.tool == "trend":
                self.roi.setPos((0, 0), finish=False)
                for handle, x, p in zip(self.roi.getHandles(), xs, record.points):
                    self.roi.movePoint(handle, QPointF(x, p.price), finish=False)
            else:
                x0, x1 = sorted(xs)
                y0, y1 = sorted(p.price for p in record.points)
                self.roi.setPos((x0, y0), finish=False)
                self.roi.setSize((max(0.05, x1 - x0), max(1e-8, y1 - y0)), finish=False)
            self.signature = signature
        self.highlight(c.selected == record.id)
        self.position_label()

    def highlight(self, selected):
        if self.roi:
            self.roi.setPen(
                pg.mkPen("#edf5ff" if selected else COLORS[0], width=2 if selected else 1.5)
            )
            for handle in self.roi.getHandles():
                handle.setVisible(selected and not self.preview)
        for i, line in enumerate(self.lines):
            line.setPen(pg.mkPen(COLORS[i], width=2.5 if selected else 1.3))

    def position_label(self, *args):
        if self.text is None:
            return
        a, b = (line.value() for line in self.lines)
        if a <= 0 or b <= 0:
            return
        delta, percent = price_difference(a, b)
        left, right = self.controller.chart.price.getViewBox().viewRange()[0]
        x = right - (right - left) * 0.04
        self.text.setText(f"价差 B − A  {delta:+.3f}  /  {percent:+.2f}%\n以 A {a:.3f} 为基准")
        self.text.setPos(x, (a + b) / 2)
        self.connector.setData([x, x], [a, b])

    def remove(self):
        for item in self.items:
            if item.scene() is not None:
                self.controller.chart.price.removeItem(item)


class ChartDrawings(QObject):
    message = Signal(str)

    def __init__(self, chart, book=None):
        super().__init__(chart)
        self.chart, self.book = chart, book or DrawingBook()
        self.snapshot = None
        self.indices = {}
        self.visuals = {}
        self.tool, self.first, self.preview, self.selected = "browse", None, None, None
        self.syncing = False
        self.consumed_button = Qt.MouseButton.NoButton
        self.controls = QWidget(chart)
        self.controls.hide()
        row = QHBoxLayout(self.controls)
        row.setContentsMargins(0, 0, 0, 0)
        row.setSpacing(7)
        row.addWidget(QLabel("画线"))
        self.combo = QComboBox()
        self.combo.setMinimumWidth(118)
        for key, name in TOOL_NAMES:
            self.combo.addItem(name, key)
        self.combo.currentIndexChanged.connect(lambda: self.set_tool(self.combo.currentData()))
        row.addWidget(self.combo)
        self.delete_button = QPushButton("删除选中")
        self.delete_button.setToolTip("选中画线后删除；也可在图表内按Delete")
        self.delete_button.clicked.connect(self.delete_selected)
        row.addWidget(self.delete_button)
        self.clear_button = QPushButton("清空画线")
        self.clear_button.setToolTip("仅清空当前股票、当前周期的画线")
        self.clear_button.clicked.connect(self.clear_current)
        row.addWidget(self.clear_button)
        self.hint = QLabel()
        self.hint.setObjectName("muted")
        self.hint.setSizePolicy(QSizePolicy.Policy.Ignored, QSizePolicy.Policy.Preferred)
        row.addWidget(self.hint, 1)
        self.timer = QTimer(self)
        self.timer.setSingleShot(True)
        self.timer.setInterval(400)
        self.timer.timeout.connect(self.flush)
        self.delete_shortcut = QShortcut(QKeySequence("Delete"), chart)
        self.delete_shortcut.setContext(Qt.ShortcutContext.WidgetWithChildrenShortcut)
        self.delete_shortcut.activated.connect(self.delete_selected)
        chart.snapshot_changed.connect(self.set_snapshot)
        chart.price.getViewBox().sigRangeChanged.connect(self.position_labels)
        self.set_snapshot(None)

    def set_snapshot(self, snapshot):
        previous = self.snapshot.series if self.snapshot else None
        same = (
            snapshot
            and previous
            and (previous.symbol, previous.interval)
            == (snapshot.series.symbol, snapshot.series.interval)
        )
        self.snapshot = snapshot
        self.indices = (
            {b.timestamp: i for i, b in enumerate(snapshot.series.bars)} if snapshot else {}
        )
        if not same:
            self.cancel()
            self.selected = None
        self.controls.setEnabled(snapshot is not None and not self.book.read_only)
        self.render()
        self.hint.setText(self.book.notice or "双击日K看当日分时")
        self.hint.setToolTip(self.hint.text())

    def set_tool(self, tool):
        if tool not in dict(TOOL_NAMES):
            raise ValueError("不支持的画线工具")
        self._clear_preview()
        self.first, self.tool = None, tool
        self.combo.blockSignals(True)
        self.combo.setCurrentIndex(self.combo.findData(tool))
        self.combo.blockSignals(False)
        self.chart.price.viewport().setCursor(
            Qt.CursorShape.ArrowCursor if tool == "browse" else Qt.CursorShape.CrossCursor
        )
        hint = (
            "点选画线可拖动；Delete删除"
            if tool == "browse"
            else (
                "依次点基准价A、目标价B"
                if tool == "price_range"
                else "点击放置"
                if TOOLS[tool] == 1
                else "依次点击两个端点"
            )
        )
        self.hint.setText(hint)
        self.hint.setToolTip(hint + "；Esc取消；Ctrl拖动仍可选区")

    def cancel(self):
        self.set_tool("browse")

    def point(self, point):
        bars = self.snapshot.series.bars
        index = min(len(bars) - 1, max(0, int(point.x() + 0.5)))
        return DrawingPoint(bars[index].timestamp, float(point.y()))

    def _record(self, first, second=None):
        series = self.snapshot.series
        return Drawing(
            "preview",
            series.symbol,
            series.interval,
            self.tool,
            (first,) if TOOLS[self.tool] == 1 else (first, second or first),
        )

    def _clear_preview(self):
        if self.preview:
            self.preview.remove()
            self.preview = None

    def handle_event(self, watched, event):
        if watched is not self.chart.price.viewport() or self.snapshot is None:
            return False
        kind = event.type()
        if kind not in (
            QEvent.Type.MouseButtonPress,
            QEvent.Type.MouseButtonRelease,
            QEvent.Type.MouseMove,
            QEvent.Type.MouseButtonDblClick,
        ):
            return False
        if kind == QEvent.Type.MouseButtonRelease and event.button() == self.consumed_button:
            self.consumed_button = Qt.MouseButton.NoButton
            return True
        scene = self.chart.price.mapToScene(event.position().toPoint())
        vb = self.chart.price.getViewBox()
        if event.modifiers() & Qt.KeyboardModifier.ControlModifier:
            return False
        if self.tool == "browse":
            identity = (
                self.hit(scene)
                if kind in (QEvent.Type.MouseButtonPress, QEvent.Type.MouseButtonDblClick)
                else None
            )
            if identity and event.button() == Qt.MouseButton.LeftButton:
                self.chart.price.setFocus(Qt.FocusReason.MouseFocusReason)
                self.select(identity)
                return kind == QEvent.Type.MouseButtonDblClick
            if kind == QEvent.Type.MouseButtonPress and event.button() == Qt.MouseButton.LeftButton:
                self.select(None)
            return False
        if kind == QEvent.Type.MouseButtonPress and event.button() == Qt.MouseButton.RightButton:
            self.consumed_button = event.button()
            self.cancel()
            return True
        if not vb.sceneBoundingRect().contains(scene):
            return kind == QEvent.Type.MouseButtonRelease
        pos = vb.mapSceneToView(scene)
        if pos.y() <= 0 or not -0.5 <= pos.x() <= len(self.snapshot.series.bars) - 0.5:
            return True
        if kind == QEvent.Type.MouseMove and self.first:
            record = self._record(self.first, self.point(pos))
            if self.preview is None:
                self.preview = DrawingVisual(self, record, preview=True)
            self.syncing = True
            try:
                self.preview.sync(record)
            finally:
                self.syncing = False
        if kind == QEvent.Type.MouseButtonPress and event.button() == Qt.MouseButton.LeftButton:
            self.consumed_button = event.button()
            self.chart.price.setFocus(Qt.FocusReason.MouseFocusReason)
            point = self.point(pos)
            if TOOLS[self.tool] == 2 and self.first is None:
                self.first = point
                self.hint.setText(
                    "再点第二条价格线B" if self.tool == "price_range" else "再点终点；Esc取消"
                )
            else:
                record = self._record(self.first or point, point)
                if self.tool in ("trend", "rectangle") and record.points[0] == record.points[1]:
                    return True
                if self.tool == "rectangle" and (
                    record.points[0].timestamp == point.timestamp
                    or record.points[0].price == point.price
                ):
                    self.message.emit("矩形需要不同的时间与价格")
                    return True
                try:
                    created = self.book.add(
                        record.symbol, record.interval, record.tool, record.points
                    )
                    self.cancel()
                    self.selected = created.id
                    self.render()
                    self.schedule_save()
                except ValueError as exc:
                    self.message.emit(str(exc))
        return True

    def hit(self, scene):
        for item in self.chart.price.scene().items(scene):
            while item is not None:
                identity = getattr(item, "drawing_id", None)
                if identity:
                    return identity
                item = item.parentItem()
        return None

    def select(self, identity):
        self.selected = identity
        for key, visual in self.visuals.items():
            visual.highlight(key == identity)
        self.delete_button.setEnabled(identity in self.book.items)

    def render(self):
        context = (
            (self.snapshot.series.symbol, self.snapshot.series.interval) if self.snapshot else None
        )
        records = {d.id: d for d in self.book.for_chart(context)}
        for key in tuple(self.visuals):
            if key not in records:
                self.visuals.pop(key).remove()
        self.syncing = True
        try:
            for key, record in records.items():
                if key not in self.visuals:
                    self.visuals[key] = DrawingVisual(self, record)
                self.visuals[key].sync(record)
        finally:
            self.syncing = False
        self.delete_button.setEnabled(self.selected in records)
        self.clear_button.setEnabled(bool(records))

    def position_labels(self, *args):
        for visual in self.visuals.values():
            visual.position_label()

    def delete_selected(self):
        if self.selected not in self.visuals:
            return
        self.book.remove((self.selected,))
        self.selected = None
        self.render()
        self.schedule_save()

    def clear_current(self):
        self.cancel()
        self.book.remove(tuple(self.visuals))
        self.selected = None
        self.render()
        self.schedule_save()

    def schedule_save(self):
        self.timer.start()

    def flush(self):
        self.timer.stop()
        try:
            self.book.flush()
        except (StorageError, OSError, ValueError) as exc:
            self.message.emit("画线尚未保存：" + str(exc))

    def shutdown(self):
        self.cancel()
        self.flush()
