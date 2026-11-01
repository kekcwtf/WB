"""Один экран склада с явными условиями сигналов и качеством исходных данных."""
from decimal import Decimal
from PySide6.QtCore import Qt
from PySide6.QtGui import QColor
from PySide6.QtWidgets import QWidget, QVBoxLayout, QHBoxLayout, QLabel, QLineEdit, QComboBox, QSpinBox, QTableWidgetItem
from .inventory import inventory_report
from .ui_tables import NumericItem, read_only_table


class InventoryView(QWidget):
    def __init__(self, parent=None):
        super().__init__(parent)
        self.context = None
        self.analysis = None
        layout = QVBoxLayout(self)
        notice = QLabel('Остатки — последний загруженный снимок WB, без товаров в пути. Продажи — предварительные данные за 30 календарных дней, без вычитания возвратов; текущий день по последнему обновлению. Неподтверждённые оплаты могут отсутствовать. Дни запаса — оценка, а не гарантия спроса.')
        notice.setWordWrap(True)
        layout.addWidget(notice)
        self.summary = QLabel()
        self.summary.setWordWrap(True)
        self.summary.setTextFormat(Qt.TextFormat.PlainText)
        layout.addWidget(self.summary)
        bar = QHBoxLayout()
        self.search = QLineEdit()
        self.search.setPlaceholderText('Поиск по артикулу, названию или баркоду')
        self.search.textChanged.connect(self.filter_rows)
        bar.addWidget(self.search)
        self.filter = QComboBox()
        self.filter.addItems(['Все товары', 'Нет зарегистрированных продаж', 'Возможный неликвид', 'Высокие расходы хранения', 'Недостаточно данных о запасе'])
        self.filter.currentIndexChanged.connect(self.filter_rows)
        bar.addWidget(self.filter)
        self.category = QComboBox()
        self.category.addItem('Все категории')
        self.category.currentIndexChanged.connect(self.filter_rows)
        bar.addWidget(self.category)
        layout.addLayout(bar)
        options = QHBoxLayout()
        self.days = QSpinBox();self.days.setRange(1, 3650);self.days.setValue(90)
        self.percent = QSpinBox();self.percent.setRange(1, 100);self.percent.setValue(10)
        for label, control in [('Избыток запаса: более дней', self.days), ('Хранение: от % финансовой выручки', self.percent)]:
            options.addWidget(QLabel(label));options.addWidget(control)
            control.valueChanged.connect(self.change_options)
        layout.addLayout(options)
        self.table = read_only_table(['Артикул WB', 'Артикул продавца', 'Размер', 'Баркод', 'На складах', 'Продажи за 30 дней', 'В среднем в день', 'Оценка дней запаса', 'Название', 'Категория', 'Хранение: загружено, ₽', 'Хранение / выручка, %', 'Сигналы', 'Ограничения'])
        layout.addWidget(self.table)

    def refresh(self, ledger, shop, today, start, end, now=None):
        self.context = (ledger, shop, today, start, end, now)
        options = ledger.setting(shop, 'inventory_options', {})
        self.days.blockSignals(True);self.percent.blockSignals(True)
        self.days.setValue(options.get('max_stock_days', 90))
        self.percent.setValue(int(options.get('storage_warning_percent', '10')))
        self.days.blockSignals(False);self.percent.blockSignals(False)
        report = inventory_report(ledger, shop, today, start, end, now=now, max_stock_days=self.days.value(),
                                  storage_warning_percent=Decimal(self.percent.value()))
        self.analysis = report
        category = self.category.currentText()
        self.category.blockSignals(True)
        self.category.clear();self.category.addItem('Все категории')
        self.category.addItems(sorted({r.category for r in report.rows if r.category}, key=lambda s: s.casefold().replace('ё','е')))
        if self.category.findText(category) >= 0:self.category.setCurrentText(category)
        self.category.blockSignals(False)
        self.summary.setText(f'Остатки загружены: {report.stocks_loaded or "—"}; продажи: {report.sales_loaded or "—"}\n'
                             f'Продажи: {report.sales_start:%d.%m.%Y}–{report.sales_end:%d.%m.%Y}. '
                             f'Хранение: {start:%d.%m.%Y}–{end:%d.%m.%Y}; '+('финансовые отчёты сверены' if report.storage_complete else 'неполные данные')+
                             f'\nЗагруженные проводки хранения магазина: {report.storage_loaded:.2f} ₽; вне строк склада: {report.storage_outside_rows:.2f} ₽.\n'+
                             '«Возможный неликвид»: положительный остаток и нет зарегистрированных продаж либо дней запаса больше порога. '
                             'Высокое хранение: достигнут порог доли выручки либо есть хранение при неположительной выручке; только для сверенного периода.\n'+
                             '\n'.join(report.reasons))
        self.table.setSortingEnabled(False)
        self.table.setRowCount(len(report.rows))
        for index, row in enumerate(report.rows):
            signals = []
            if row.no_sales:signals.append('Нет зарегистрированных продаж')
            if row.potential_illiquid:signals.append('Возможный неликвид')
            if row.high_storage:signals.append('Высокие расходы хранения')
            if row.daily_sales is None:signals.append('Недостаточно данных о запасе')
            values = [row.nm_id,row.vendor,row.size,row.barcode,row.stock,row.sales,row.daily_sales,row.estimated_days,
                      row.title,row.category,row.storage_loaded,row.storage_ratio,'; '.join(signals) or 'Сигналов нет','; '.join(row.warnings)]
            for column, value in enumerate(values):
                item = NumericItem(Decimal(value) if isinstance(value, int) else value) if value is None or isinstance(value, (int, Decimal)) else QTableWidgetItem(value)
                if row.high_storage or row.potential_illiquid:item.setForeground(QColor('#a66c00'))
                elif row.daily_sales is None:item.setForeground(QColor('#777777'))
                if column == 0:item.setData(Qt.ItemDataRole.UserRole, row)
                self.table.setItem(index, column, item)
        self.table.setSortingEnabled(True)
        self.filter_rows()

    def invalid_period(self):
        self.context=None;self.analysis=None
        self.table.setRowCount(0)
        self.summary.setText('Неверный период расходов хранения: дата начала позже даты окончания')

    def change_options(self, *_):
        if self.context:
            ledger,shop,*_ = self.context
            ledger.set_setting(shop,'inventory_options',{'max_stock_days':self.days.value(),'storage_warning_percent':str(self.percent.value())})
            self.refresh(*self.context)

    def filter_rows(self, *_):
        mode = self.filter.currentIndex()
        query = self.search.text().strip().casefold()
        for index in range(self.table.rowCount()):
            row = self.table.item(index,0).data(Qt.ItemDataRole.UserRole)
            if row is None:continue
            match = (True,row.no_sales,row.potential_illiquid,row.high_storage,row.daily_sales is None)[mode]
            text = ' '.join([row.nm_id,row.vendor,row.barcode,row.title,row.category]).casefold()
            self.table.setRowHidden(index,not match or query not in text or
                                   (self.category.currentIndex()>0 and self.category.currentText()!=row.category))
