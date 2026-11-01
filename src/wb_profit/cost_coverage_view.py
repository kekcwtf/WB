"""Помощь заполнению точного SKU/даты без назначения предполагаемой цены."""
from decimal import Decimal
from PySide6.QtCore import Qt, Signal
from PySide6.QtWidgets import (QDialog, QVBoxLayout, QHBoxLayout, QLabel, QComboBox,
    QLineEdit, QPushButton, QTableWidgetItem, QDialogButtonBox)
from .cost_coverage import cost_requirements
from .ui_tables import NumericItem, read_only_table


class CostCoverageDialog(QDialog):
    selected_cost = Signal(str, object)

    def __init__(self, operations, costs, metadata, parent=None):
        super().__init__(parent)
        self.report = cost_requirements(operations, costs)
        self.metadata = metadata
        self.setWindowTitle('Проверка себестоимости по SKU и размерам')
        self.resize(1050, 550)
        layout = QVBoxLayout(self)
        attention = sum(row.needs_attention for row in self.report.rows)
        summary = QLabel(f'SKU в подтверждённых продажах/возвратах: {len(self.report.rows)}; требуют внимания: {attention}; предварительных строк исключено: {self.report.preliminary_records}. '
                         'Проверены только загруженные записи: пустой список не подтверждает полноту истории или всех цен. '
                         'Единицы здесь — количество в строках продаж и возвратов, не остаток. Отсутствие цены не означает 0 ₽. Для возврата нужна цена на дату исходной продажи. Назначение цены не исправляет неизвестное количество или отсутствие связи возврата.')
        summary.setWordWrap(True)
        layout.addWidget(summary)
        bar = QHBoxLayout()
        self.search = QLineEdit();self.search.setPlaceholderText('Точный SKU, артикул продавца или название')
        self.filter = QComboBox();self.filter.addItems(['Требуют внимания', 'Нет цены', 'Нет связи возврата', 'Неизвестно количество', 'Все SKU'])
        bar.addWidget(self.search);bar.addWidget(self.filter);layout.addLayout(bar)
        self.table = read_only_table(['SKU / баркод', 'Артикул продавца', 'Название', 'Проверено, ед.', 'Без цены, ед.', 'Первая дата без цены', 'Возвратов без связи, строк', 'Количество неизвестно, строк'])
        layout.addWidget(self.table)
        self.status = QLabel('Один SKU — один размер/баркод. Цена не распространяется автоматически на другие размеры.')
        self.status.setWordWrap(True);self.status.setTextFormat(Qt.TextFormat.PlainText);layout.addWidget(self.status)
        self.fill = QPushButton('Заполнить SKU и дату в настройках')
        self.fill.clicked.connect(self.choose)
        layout.addWidget(self.fill)
        buttons = QDialogButtonBox(QDialogButtonBox.StandardButton.Close)
        buttons.rejected.connect(self.reject);layout.addWidget(buttons)
        self.search.textChanged.connect(self.refresh_rows)
        self.filter.currentIndexChanged.connect(self.refresh_rows)
        self.refresh_rows()

    def refresh_rows(self, *_):
        mode = self.filter.currentIndex()
        query = self.search.text().strip().casefold()
        rows = []
        for row in self.report.rows:
            matches = (row.needs_attention, row.missing_price_units > 0, row.unlinked_returns > 0, row.missing_quantity > 0, True)[mode]
            card = self.metadata.get(row.sku.split(':', 1)[0], {}) if row.sku else {}
            if not isinstance(card, dict):card = {}
            texts = (row.sku or 'Без SKU', str(card.get('vendorCode') or '—'), str(card.get('title') or '—'))
            if matches and (not query or query in ' '.join(texts).casefold()):
                rows.append((row, texts))
        self.table.setSortingEnabled(False);self.table.setRowCount(len(rows))
        for index, (row, texts) in enumerate(rows):
            values = [*texts, Decimal(row.checked_units), Decimal(row.missing_price_units),
                      row.earliest_missing_date.isoformat() if row.earliest_missing_date else '—',
                      Decimal(row.unlinked_returns), Decimal(row.missing_quantity)]
            for column, value in enumerate(values):
                item = NumericItem(value) if isinstance(value, Decimal) else QTableWidgetItem(value)
                item.setData(Qt.ItemDataRole.UserRole, row)
                self.table.setItem(index, column, item)
        self.table.setSortingEnabled(True)

    def choose(self):
        item = self.table.item(self.table.currentRow(), 0)
        if item is None:
            self.status.setText('Выберите SKU, для которого нужно назначить цену.')
            return
        row = item.data(Qt.ItemDataRole.UserRole)
        if not row.sku or row.earliest_missing_date is None:
            self.status.setText('Нет точного SKU и даты назначения цены. Сначала нужна проверка количества или связи возврата; дата и цена не предполагаются.')
            return
        self.selected_cost.emit(row.sku, row.earliest_missing_date)
        self.accept()
