"""Экран сценария цены. Рабочие данные поступают только из локального реестра."""
from decimal import Decimal, InvalidOperation
from datetime import datetime, timezone
from PySide6.QtCore import QTimer
from PySide6.QtWidgets import QWidget, QVBoxLayout, QFormLayout, QComboBox, QLineEdit, QLabel, QTableWidgetItem
from .economics import historical_model
from .finance import Kind
from .ui_tables import read_only_table, NumericItem


EXPENSE_LABELS = {
    Kind.LOGISTICS: 'Логистика', Kind.REVERSE_LOGISTICS: 'Обратная логистика',
    Kind.STORAGE: 'Хранение', Kind.ACCEPTANCE: 'Платная приёмка',
    Kind.ACQUIRING: 'Эквайринг', Kind.ADVERTISING: 'Реклама',
    Kind.PENALTY: 'Штрафы', Kind.DEDUCTION: 'Удержания', Kind.EXTERNAL: 'Внешние расходы SKU',
}


class EconomicsView(QWidget):
    def __init__(self):
        super().__init__()
        layout = QVBoxLayout(self)
        heading = QLabel('Юнит-экономика: сценарий изменения цены')
        heading.setStyleSheet('font-size: 22px; font-weight: 600')
        layout.addWidget(heading)
        self.notice = QLabel('Сценарий использует проверенный снимок базового тарифа FBW либо историческую комиссию. Общие расходы магазина и изменение спроса не моделируются.')
        self.notice.setWordWrap(True)
        layout.addWidget(self.notice)
        form = QFormLayout()
        self.sku = QComboBox()
        self.price = QLineEdit()
        self.price.setPlaceholderText('Предполагаемая цена с НДС, ₽')
        form.addRow('SKU', self.sku)
        form.addRow('Новая цена, ₽', self.price)
        layout.addLayout(form)
        self.output = QLabel()
        self.output.setWordWrap(True)
        layout.addWidget(self.output)
        self.comparison = read_only_table(['Показатель на единицу', 'При исторической цене', 'При новой цене', 'Изменение'])
        self.comparison.setMinimumHeight(250)
        layout.addWidget(self.comparison, 1)
        self.operations, self.costs, self.taxes, self.coverage = [], [], None, False
        self.model = None
        self.fbw_rates = {}
        self.sku.currentTextChanged.connect(self.select_sku)
        self.price.textChanged.connect(self.project)
        self.tariff_timer = QTimer(self)
        self.tariff_timer.setInterval(60000)
        self.tariff_timer.timeout.connect(self.check_tariff_age)
        self.set_context([], [], None)

    def set_context(self, operations, costs, taxes, *, coverage_confirmed=False, fbw_rates=None, tariff_note=None, tariff_valid_until=None):
        self.operations, self.costs, self.taxes, self.coverage = list(operations), list(costs), taxes, coverage_confirmed
        self.fbw_rates=fbw_rates or {}
        self.tariff_valid_until = tariff_valid_until
        if tariff_valid_until is not None:self.tariff_timer.start()
        else:self.tariff_timer.stop()
        self.notice.setText(tariff_note or 'Свежесть снимка FBW не подтверждена в этом контексте. Сценарий — оценка; общие расходы магазина и изменение спроса не моделируются.')
        previous = self.sku.currentText()
        self.sku.blockSignals(True)
        self.sku.clear()
        self.sku.addItems(sorted({op.sku for op in self.operations if op.sku and op.kind == Kind.SALE}))
        index = self.sku.findText(previous)
        if index >= 0:
            self.sku.setCurrentIndex(index)
        self.sku.blockSignals(False)
        self.select_sku()

    def select_sku(self, *_):
        self.model = None
        self.comparison.setRowCount(0)
        self.price.setEnabled(False)
        self.price.clear()
        if not self.sku.currentText():
            self.output.setText('Нет подтверждённых продаж. Прогноз не рассчитан.')
            return
        try:
            self.model = historical_model(self.sku.currentText(), self.operations, self.costs, self.taxes,
                                          coverage_confirmed=self.coverage,fbw_commission_percent=self.fbw_rates.get(self.sku.currentText()))
        except ValueError as exc:
            self.output.setText(str(exc))
            return
        self.price.setEnabled(True)
        self.price.setText(str(self.model.average_price.quantize(Decimal('0.01'))))

    def project(self, *_):
        if self.model is None:
            return
        if self.tariff_valid_until is not None and datetime.now(timezone.utc) >= self.tariff_valid_until:
            self.fbw_rates = {}
            self.tariff_valid_until = None
            self.tariff_timer.stop()
            self.model = historical_model(self.sku.currentText(), self.operations, self.costs, self.taxes,
                                          coverage_confirmed=self.coverage)
            self.notice.setText('Снимок FBW устарел после открытия сценария. Комиссия переключена на историческую долю; цена сохранена. Общие расходы магазина и изменение спроса не моделируются.')
        try:
            result = self.model.forecast(Decimal(self.price.text().replace(',', '.')))
        except (ValueError, InvalidOperation):
            self.output.setText('Введите положительную конечную цену.')
            self.comparison.setRowCount(0)
            return
        baseline = self.model.forecast(self.model.average_price)
        rows = [('Цена с НДС, ₽', baseline.price, result.price),
                ('Комиссия WB, ₽', baseline.commission, result.commission)]
        rows.extend((EXPENSE_LABELS[kind] + ', ₽', amount, amount)
                    for kind, amount in self.model.expense_components)
        rows.extend([
            ('Остальные расходы — всего, ₽', baseline.other_expenses, result.other_expenses),
            ('Себестоимость, ₽', baseline.purchase_cost, result.purchase_cost),
            ('Расчётный УСН, ₽', baseline.usn, result.usn),
            ('Расчётный НДС, ₽', baseline.vat, result.vat),
            ('Условная прибыль, ₽', baseline.estimated_profit, result.estimated_profit),
            ('Маржинальность, %', baseline.margin_percent, result.margin_percent),
            ('ROI к себестоимости, %', baseline.roi_percent, result.roi_percent),
        ])
        self.comparison.setSortingEnabled(False)
        self.comparison.setRowCount(len(rows))
        for row, (label, old, new) in enumerate(rows):
            self.comparison.setItem(row, 0, QTableWidgetItem(label))
            for column, value in enumerate((old, new, new-old if old is not None and new is not None else None), 1):
                item = NumericItem(value)
                item.setToolTip('Изменение процентов показано в процентных пунктах.' if label.endswith('%')
                                else 'Расходы показаны положительными; возмещение услуги — отрицательным. Итог расходов не прибавляется повторно.')
                self.comparison.setItem(row, column, item)
        self.comparison.setSortingEnabled(True)
        break_even=self.model.break_even_price()
        threshold=f'{break_even:.2f} ₽' if break_even is not None else 'не определена'
        self.output.setText(
            f'Сценарий, ₽ на единицу\n'
            f'Цена: {result.price:.2f}\n'
            f'Комиссия ({self.model.commission_source}): {result.commission:.2f}\n'
            f'Остальные расходы по истории: {result.other_expenses:.2f}\n'
            f'Себестоимость по истории: {result.purchase_cost:.2f}\n'
            f'Расчётный УСН: {result.usn:.2f}\nРасчётный НДС: {result.vat:.2f}\n'
            f'Условная прибыль до общих расходов: {result.estimated_profit:.2f}\n'
            f'Условная маржинальность: {result.margin_percent:.2f}%\n'
            'Таблица ниже сравнивает два сценария: налоги пересчитаны на единицу, это не фактический итог периода.\n'
            f'Консервативная оценка точки безубыточности: {threshold}\n\n' + '\n'.join(self.model.limitations))

    def check_tariff_age(self):
        if self.tariff_valid_until is not None and datetime.now(timezone.utc) >= self.tariff_valid_until:
            self.project()
