"""Обратимая отмена только введённых продавцом внешних расходов."""
from datetime import datetime
from decimal import Decimal
from PySide6.QtCore import Qt, Signal
from PySide6.QtWidgets import (QDialog, QVBoxLayout, QLabel, QPushButton, QTableWidgetItem,
                               QDialogButtonBox, QAbstractItemView)
from .business_dates import MOSCOW
from .expenses import cancelled_expenses, restore_external_expense
from .ui_tables import NumericItem, read_only_table


class ExpenseArchiveDialog(QDialog):
    changed = Signal()

    def __init__(self, ledger, store, parent=None):
        super().__init__(parent)
        self.ledger, self.store = ledger, store
        self.setWindowTitle('Отменённые внешние расходы')
        self.resize(900, 500)
        layout = QVBoxLayout(self)
        notice = QLabel('Эти расходы исключены из прибыли. Восстановление вернёт расход с прежней датой, суммой и SKU. Операции WB здесь не отменяются и не восстанавливаются.')
        notice.setWordWrap(True)
        layout.addWidget(notice)
        self.status = QLabel()
        self.status.setTextFormat(Qt.TextFormat.PlainText)
        self.status.setWordWrap(True)
        layout.addWidget(self.status)
        self.table = read_only_table(['Отменён, МСК', 'Дата расхода', 'Назначение', 'SKU', 'Сумма, ₽', 'ID'])
        self.table.setSelectionBehavior(QAbstractItemView.SelectionBehavior.SelectRows)
        self.table.setSelectionMode(QAbstractItemView.SelectionMode.SingleSelection)
        layout.addWidget(self.table)
        self.restore = QPushButton('Восстановить выбранный расход')
        self.restore.clicked.connect(self.restore_selected)
        layout.addWidget(self.restore)
        buttons = QDialogButtonBox(QDialogButtonBox.StandardButton.Close)
        buttons.rejected.connect(self.reject)
        layout.addWidget(buttons)
        self.reload()

    def reload(self):
        try:
            rows = cancelled_expenses(self.ledger, self.store)
        except Exception:
            self.table.setRowCount(0)
            self.restore.setEnabled(False)
            self.status.setText('Не удалось прочитать отменённые расходы. Записи не изменены.')
            return
        self.table.setSortingEnabled(False)
        self.table.setRowCount(len(rows))
        for row, (operation, value) in enumerate(rows):
            stamp = datetime.fromisoformat(value['cancelled_at']).astimezone(MOSCOW)
            values = [stamp.strftime('%Y-%m-%d %H:%M:%S'), operation.occurred.isoformat(),
                      value['description'], operation.sku or 'Весь магазин', -operation.amount, operation.identifier]
            for column, value in enumerate(values):
                item = NumericItem(value) if isinstance(value, Decimal) else QTableWidgetItem(value)
                item.setData(Qt.ItemDataRole.UserRole, operation.identifier)
                self.table.setItem(row, column, item)
        self.table.setSortingEnabled(True)
        self.restore.setEnabled(bool(rows))
        self.status.setText(f'Отменённых расходов: {len(rows)}. В финансовой истории сохраняются прежние версии.')

    def restore_selected(self):
        row = self.table.currentRow()
        item = self.table.item(row, 0) if row >= 0 else None
        if item is None:
            self.status.setText('Выберите расход для восстановления.')
            return
        try:
            restored = restore_external_expense(self.ledger, self.store, item.data(Qt.ItemDataRole.UserRole))
        except ValueError as exc:
            self.status.setText(str(exc))
            return
        except Exception:
            self.status.setText('Не удалось восстановить расход. Текущие данные не заменены.')
            return
        self.reload()
        if restored:
            self.changed.emit()
            self.status.setText('Расход восстановлен с прежней датой и суммой; прибыль пересчитана.')
