"""Читаемый журнал цен закупки; история начинается с реально сохранённых правок."""
from datetime import datetime
from decimal import Decimal
from PySide6.QtWidgets import QDialog, QVBoxLayout, QLabel, QTableWidgetItem, QDialogButtonBox
from .business_dates import MOSCOW
from .cost_history import changes
from .ui_tables import read_only_table, NumericItem


class CostChangesDialog(QDialog):
    def __init__(self, ledger, shop, parent=None):
        super().__init__(parent)
        self.setWindowTitle('Изменения себестоимости')
        self.resize(900, 450)
        layout = QVBoxLayout(self)
        notice = QLabel('До 200 последних изменений. Прежние цены из старых версий программы задним числом не придумываются. Удаление цены может включить предыдущую или явно назначенную общую цену; отсутствие цены не считается нулём.')
        notice.setWordWrap(True)
        layout.addWidget(notice)
        self.table = read_only_table(['№', 'Изменено, МСК', 'SKU', 'Действует с', 'Прежняя цена, ₽', 'Новая цена, ₽', 'Изменение', 'Отмена изменения'])
        layout.addWidget(self.table)
        rows = changes(ledger, shop)
        self.table.setSortingEnabled(False)
        self.table.setRowCount(len(rows))
        for index, (_name, event) in enumerate(rows):
            old, new = event['before'], event['after']
            target = new or old
            stamp = datetime.fromisoformat(event['changed_at']).astimezone(MOSCOW)
            action = 'Удаление' if new is None else 'Назначение' if old is None else 'Исправление'
            values = [Decimal(event['sequence']), stamp.strftime('%Y-%m-%d %H:%M:%S'),
                      'Общая (назначена)' if target['sku'] == '*' else target['sku'], target['date'],
                      Decimal(old['amount']) if old else None, Decimal(new['amount']) if new else None,
                      action, 'Отменено' if event['undone_at'] else 'Не отменено']
            for column, value in enumerate(values):
                self.table.setItem(index, column, NumericItem(value) if value is None or isinstance(value, Decimal) else QTableWidgetItem(value))
        self.table.setSortingEnabled(True)
        buttons = QDialogButtonBox(QDialogButtonBox.StandardButton.Close)
        buttons.rejected.connect(self.reject)
        layout.addWidget(buttons)
