"""Журнал без создания QWidgetItem для каждой финансовой проводки."""
from PySide6.QtCore import QAbstractTableModel, QModelIndex, Qt
from PySide6.QtWidgets import QTableView, QAbstractItemView, QHeaderView
from .analytics import LABELS


HEADERS = ('Дата', 'Источник', 'ID', 'SKU', 'Операция', 'Сумма, ₽', 'Статус')


class OperationsModel(QAbstractTableModel):
    def __init__(self, parent=None):
        super().__init__(parent)
        self._operations = []
        self._sort_column = -1
        self._sort_order = Qt.SortOrder.AscendingOrder

    def rowCount(self, parent=QModelIndex()):
        return 0 if parent.isValid() else len(self._operations)

    def columnCount(self, parent=QModelIndex()):
        return 0 if parent.isValid() else len(HEADERS)

    def operation_at(self, row):
        if 0 <= row < len(self._operations):
            return self._operations[row]
        return None

    @staticmethod
    def value(operation, column):
        return (operation.occurred, operation.source, operation.identifier,
                operation.sku or 'Без SKU', LABELS[operation.kind], operation.amount,
                'Подтверждён' if operation.confirmed else 'Предварительный')[column]

    def data(self, index, role=Qt.ItemDataRole.DisplayRole):
        if not index.isValid() or not (0 <= index.column() < len(HEADERS)):
            return None
        operation = self.operation_at(index.row())
        if operation is None:
            return None
        if role == Qt.ItemDataRole.DisplayRole:
            value = self.value(operation, index.column())
            if index.column() == 0:
                return value.strftime('%d.%m.%Y')
            if index.column() == 5:
                return f'{value:,.2f}'.replace(',', ' ')
            return value
        if role == Qt.ItemDataRole.TextAlignmentRole and index.column() == 5:
            return Qt.AlignmentFlag.AlignRight | Qt.AlignmentFlag.AlignVCenter
        if role == Qt.ItemDataRole.ToolTipRole:
            if index.column() == 5:
                return f'{operation.amount} ₽ · исходная точная сумма'
            if operation.source == 'external_expenses':
                return 'Двойной щелчок — изменить внешний расход.'
        return None

    def headerData(self, section, orientation, role=Qt.ItemDataRole.DisplayRole):
        if role != Qt.ItemDataRole.DisplayRole:
            return None
        if orientation == Qt.Orientation.Horizontal:
            return HEADERS[section] if 0 <= section < len(HEADERS) else None
        return str(section + 1)

    def set_operations(self, operations):
        values = list(operations)
        if self._sort_column >= 0:
            values.sort(key=lambda op: self.value(op, self._sort_column),
                        reverse=self._sort_order == Qt.SortOrder.DescendingOrder)
        self.beginResetModel()
        self._operations = values
        self.endResetModel()

    def sort(self, column, order=Qt.SortOrder.AscendingOrder):
        if not 0 <= column < len(HEADERS):
            return
        self._sort_column, self._sort_order = column, order
        # Сортируется весь набор, суммы остаются Decimal даже за пределами 2**53.
        permutation = sorted(range(len(self._operations)),
                             key=lambda row: self.value(self._operations[row], column),
                             reverse=order == Qt.SortOrder.DescendingOrder)
        self.layoutAboutToBeChanged.emit()
        old_indexes = self.persistentIndexList()
        positions = {old: new for new, old in enumerate(permutation)}
        self._operations = [self._operations[row] for row in permutation]
        self.changePersistentIndexList(old_indexes,
            [self.index(positions[index.row()], index.column()) for index in old_indexes])
        self.layoutChanged.emit()


class OperationsView(QTableView):
    def __init__(self, parent=None):
        super().__init__(parent)
        self.setModel(OperationsModel(self))
        self.setEditTriggers(QAbstractItemView.EditTrigger.NoEditTriggers)
        self.setSelectionBehavior(QAbstractItemView.SelectionBehavior.SelectRows)
        self.setAlternatingRowColors(True)
        self.setVerticalScrollMode(QAbstractItemView.ScrollMode.ScrollPerPixel)
        header = self.horizontalHeader()
        header.setSectionResizeMode(QHeaderView.ResizeMode.Interactive)
        header.setStretchLastSection(True)
        for column, width in enumerate((110, 155, 190, 170, 180, 155, 160)):
            self.setColumnWidth(column, width)
        # Автоподбор ширины по всей истории превратил бы ленивую таблицу в полный обход.
        self.setSortingEnabled(True)
        self.sortByColumn(0, Qt.SortOrder.AscendingOrder)

    def set_operations(self, operations):
        self.model().set_operations(operations)
