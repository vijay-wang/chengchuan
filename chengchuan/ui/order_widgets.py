"""Shared order filter controls and stable amount-band colors."""

from PySide6.QtCore import Signal
from PySide6.QtWidgets import QCheckBox, QHBoxLayout, QWidget

from chengchuan.domain.microstructure import OrderFilter

COLORS = (
    "#4dc5b1",
    "#56b7d9",
    "#6b9be8",
    "#9186de",
    "#b382d4",
    "#d183b2",
    "#e18d86",
    "#e5b36b",
    "#c7cc75",
)


class OrderFilterControls(QWidget):
    changed = Signal(object)

    def __init__(self, verb="统计", include_cancelled=False, parent=None):
        super().__init__(parent)
        layout = QHBoxLayout(self)
        layout.setContentsMargins(0, 0, 0, 0)
        self.unfilled = QCheckBox(verb + "未成交")
        self.cancelled = QCheckBox(verb + "已撤销")
        self.unfilled.setToolTip("未成交指成交股数为0；部分成交由撤销开关独立控制。")
        self.cancelled.setToolTip("包括完全撤单，以及部分成交后撤销剩余的整张原单。")
        self.unfilled.setChecked(True)
        self.cancelled.setChecked(include_cancelled)
        for box in (self.unfilled, self.cancelled):
            layout.addWidget(box)
            box.toggled.connect(lambda unused: self.changed.emit(self.value()))
        layout.addStretch()

    def value(self):
        return OrderFilter(self.unfilled.isChecked(), self.cancelled.isChecked())

    def set_value(self, filters):
        for box, value in (
            (self.unfilled, filters.include_unfilled),
            (self.cancelled, filters.include_cancelled),
        ):
            box.blockSignals(True)
            box.setChecked(value)
            box.blockSignals(False)
