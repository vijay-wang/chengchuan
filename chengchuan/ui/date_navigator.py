"""Calendar popup below charts; only a committed date click requests navigation."""

from PySide6.QtCore import QDate, QLocale, Qt, Signal
from PySide6.QtGui import QColor, QTextCharFormat
from PySide6.QtWidgets import (
    QCalendarWidget,
    QHBoxLayout,
    QLabel,
    QMenu,
    QPushButton,
    QWidget,
    QWidgetAction,
)


class DateNavigator(QWidget):
    requested = Signal(object)
    latest_requested = Signal()

    def __init__(self, parent=None):
        super().__init__(parent)
        layout = QHBoxLayout(self)
        layout.setContentsMargins(0, 6, 0, 0)
        layout.addWidget(QLabel("日期跳转"))
        self.button = QPushButton("选择日期 ▾")
        self.button.setMinimumWidth(138)
        self.button.setToolTip("点击日历选择日期；翻月不会请求行情")
        self.button.clicked.connect(self._open)
        layout.addWidget(self.button)
        self.latest = QPushButton("返回最新")
        self.latest.clicked.connect(self.latest_requested.emit)
        layout.addWidget(self.latest)
        self.hint = QLabel("选择日期查看历史")
        self.hint.setObjectName("muted")
        self.hint.setWordWrap(True)
        layout.addWidget(self.hint, 1)
        self.popup = QMenu(self)
        self.calendar = QCalendarWidget()
        self.calendar.setLocale(QLocale(QLocale.Language.Chinese, QLocale.Country.China))
        self.calendar.setFirstDayOfWeek(Qt.DayOfWeek.Monday)
        self.calendar.setVerticalHeaderFormat(QCalendarWidget.VerticalHeaderFormat.NoVerticalHeader)
        header = QTextCharFormat()
        header.setBackground(QColor("#1b3044"))
        header.setForeground(QColor("#a1b2c3"))
        self.calendar.setHeaderTextFormat(header)
        weekend = QTextCharFormat()
        weekend.setForeground(QColor("#f07b86"))
        for day in (Qt.DayOfWeek.Saturday, Qt.DayOfWeek.Sunday):
            self.calendar.setWeekdayTextFormat(day, weekend)
        self.calendar.setMaximumDate(QDate.currentDate())
        self.calendar.clicked.connect(self._picked)
        action = QWidgetAction(self.popup)
        action.setDefaultWidget(self.calendar)
        self.popup.addAction(action)
        self.set_context(False)

    def _open(self):
        self.calendar.setMaximumDate(QDate.currentDate())
        self.popup.popup(self.button.mapToGlobal(self.button.rect().bottomLeft()))

    def _picked(self, day):
        self.popup.hide()
        self.requested.emit(day.toPython())

    def set_context(self, enabled, day=None, historical=False, hint=None):
        self.button.setEnabled(enabled)
        self.latest.setEnabled(enabled and historical)
        if day is not None:
            selected = QDate(day.year, day.month, day.day)
            self.calendar.setSelectedDate(selected)
            self.calendar.setCurrentPage(day.year, day.month)
            self.button.setText(day.strftime("%Y-%m-%d") + " ▾")
        if hint is not None:
            self.hint.setText(hint)
