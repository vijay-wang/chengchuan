"""Read-only virtual table, sharing values rather than allocating per-cell widgets."""

from PySide6.QtCore import QAbstractTableModel, QModelIndex, Qt
from PySide6.QtGui import QColor

from chengchuan.ui import theme


class ValueTableModel(QAbstractTableModel):
    def __init__(
        self, headers, formats=None, color_columns=(), secondary=None, parent=None, header_tips=None
    ):
        super().__init__(parent)
        self.headers, self.formats = tuple(headers), formats or {}
        self.color_columns, self.secondary = frozenset(color_columns), secondary
        self.rows = ()
        self.source_rows = ()
        self.header_tips = header_tips or {}

    def set_rows(self, rows):
        self.beginResetModel()
        self.rows = tuple(rows)
        self.source_rows = self.rows
        self.endResetModel()

    def rowCount(self, parent=QModelIndex()):
        return 0 if parent.isValid() else len(self.rows)

    def columnCount(self, parent=QModelIndex()):
        return 0 if parent.isValid() else len(self.headers)

    def headerData(self, section, orientation, role=Qt.ItemDataRole.DisplayRole):
        if role == Qt.ItemDataRole.ToolTipRole and orientation == Qt.Orientation.Horizontal:
            return self.header_tips.get(section, self.headers[section])
        if role == Qt.ItemDataRole.DisplayRole and orientation == Qt.Orientation.Horizontal:
            return self.headers[section]

    def data(self, index, role=Qt.ItemDataRole.DisplayRole):
        if not index.isValid():
            return None
        value = self.rows[index.row()][index.column()]
        if role == Qt.ItemDataRole.UserRole:
            return value
        if role in (Qt.ItemDataRole.DisplayRole, Qt.ItemDataRole.ToolTipRole):
            return "—" if value is None else self.formats.get(index.column(), str)(value)
        if (
            role == Qt.ItemDataRole.ForegroundRole
            and index.column() in self.color_columns
            and value is not None
        ):
            return QColor(theme.UP if value >= 0 else theme.DOWN)

    def sort(self, column, order=Qt.SortOrder.AscendingOrder):
        if column < 0:
            self.layoutAboutToBeChanged.emit()
            self.rows = self.source_rows
            self.layoutChanged.emit()
            return
        if not self.rows:
            return
        descending = order == Qt.SortOrder.DescendingOrder
        known = [r for r in self.rows if r[column] is not None]
        missing = [r for r in self.rows if r[column] is None]
        if self.secondary is not None and column != self.secondary:
            known.sort(key=lambda r: (r[self.secondary] is None, -(r[self.secondary] or 0)))
        known.sort(key=lambda r: r[column], reverse=descending)
        self.layoutAboutToBeChanged.emit()
        self.rows = (*known, *missing)
        self.layoutChanged.emit()
