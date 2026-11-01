"""Проверка возвратов в разделе сверки; налоговые компоненты не проводятся в реестр."""
from decimal import Decimal
from PySide6.QtCore import Qt
from PySide6.QtGui import QColor
from PySide6.QtWidgets import QWidget,QVBoxLayout,QHBoxLayout,QLabel,QComboBox,QLineEdit,QTableWidgetItem
from .return_review import review_from_ledger
from .ui_tables import NumericItem,read_only_table


class ReturnReviewView(QWidget):
    def __init__(self,parent=None):
        super().__init__(parent)
        self.analysis=None
        layout=QVBoxLayout(self)
        title=QLabel('Возвраты: исходные продажи и ограничения налогового расчёта')
        title.setWordWrap(True);layout.addWidget(title)
        self.summary=QLabel()
        self.summary.setWordWrap(True);self.summary.setTextFormat(Qt.TextFormat.PlainText)
        layout.addWidget(self.summary)
        notice=QLabel('НДС в цене и расчёт УСН ниже — оценки по настройкам исходной продажи, не налоговый вычет. Они не уменьшают налоги или чистую прибыль автоматически. Даты операций WB не подтверждают налоговый момент признания дохода и возврата.')
        notice.setWordWrap(True);layout.addWidget(notice)
        bar=QHBoxLayout()
        self.search=QLineEdit();self.search.setPlaceholderText('SKU или ID возврата / исходной продажи')
        self.search.textChanged.connect(self.filter_rows);bar.addWidget(self.search)
        self.filter=QComboBox()
        self.filter.addItems(['Все возвраты','Нет однозначной связи','Проверить сумму или количество','Разные кварталы','Различаются налоговые настройки','Нет себестоимости'])
        self.filter.currentIndexChanged.connect(self.filter_rows);bar.addWidget(self.filter)
        layout.addLayout(bar)
        self.table=read_only_table(['Дата возврата','SKU','Возврат, ед.','Сумма возврата, ₽','Исходная продажа: ID','Дата продажи','Восстановленная себестоимость, ₽','НДС в цене: оценка, ₽','УСН с суммы: оценка, ₽','Проверка','ID возврата'])
        self.table.setMinimumHeight(220)
        layout.addWidget(self.table)

    def clear(self,message='Нет загруженных возвратов. Пустой список не подтверждает отсутствие возвратов.'):
        self.analysis=None;self.table.setRowCount(0);self.summary.setText(message)

    def set_context(self,ledger,shop,selected,costs,rules,*,financial_complete=False):
        report=review_from_ledger(ledger,shop,selected,costs,rules)
        self.analysis=report
        if not report.rows:
            self.table.setRowCount(0)
            self.summary.setText('В сверенном реестре нет возвратов за выбранный период.' if financial_complete else
                                 'Нет загруженных распознанных возвратов. Пустой список не подтверждает отсутствие возвратов.')
            return
        known=sum(row.restored_cost is not None for row in report.rows)
        cost=f'{report.known_restored_cost:.2f} ₽ (известная часть)' if known else '—'
        self.summary.setText(f'Строк: {len(report.rows)}; однозначно связаны: {report.linked_count}; '
                             f'проверить суммы/количество: {report.price_check_count}. Восстановленная себестоимость: {cost}. '+
                             ('Финансовый период сверён.' if financial_complete else 'Период загружен частично или финансовая сверка не завершена.'))
        self.table.setSortingEnabled(False);self.table.setRowCount(len(report.rows))
        for index,row in enumerate(report.rows):
            returned,sale=row.operation,row.sale
            status=[]
            if not row.linked:status.append('Нет однозначной связи')
            if 'amount' in row.flags:status.append('Проверить суммы / количество')
            if 'quarter' in row.flags:status.append('Разные кварталы')
            if 'rate' in row.flags:status.append('Разные налоговые настройки')
            if 'tax' in row.flags:status.append('Налоговые настройки не подтверждены')
            if 'cost' in row.flags:status.append('Нет себестоимости')
            values=[returned.occurred.isoformat(),returned.sku or 'Без SKU',
                    Decimal(row.returned_units) if row.returned_units is not None else None,row.returned_amount,
                    sale.identifier if sale else returned.original_sale_id or '—',sale.occurred.isoformat() if sale else '—',
                    row.restored_cost,row.vat_price_component,row.usn_price_component,
                    '; '.join(status) or 'Связь подтверждена; налоговый эффект требует проверки',returned.identifier]
            tooltip='\n'.join(row.issues)
            if sale:
                tooltip+=(f'\nИсходная продажа: {sale.units} ед.; {sale.amount:.2f} ₽. '
                          f'Все связанные подтверждённые возвраты в истории: {row.cumulative_units} ед.; {row.cumulative_amount:.2f} ₽. '
                          f'Исходные настройки: УСН {row.original_usn if row.original_usn is not None else "—"}%; '
                          f'НДС {row.original_vat if row.original_vat is not None else "—"}%.')
            for column,value in enumerate(values):
                item=NumericItem(value) if value is None or isinstance(value,Decimal) else QTableWidgetItem(value)
                item.setToolTip(tooltip)
                if row.flags:item.setForeground(QColor('#a66c00' if row.linked else '#777777'))
                if column==0:item.setData(Qt.ItemDataRole.UserRole,row)
                self.table.setItem(index,column,item)
        self.table.setSortingEnabled(True);self.filter_rows()

    def filter_rows(self,*_):
        flags={1:'unlinked',2:'amount',3:'quarter',4:'rate',5:'cost'}
        flag=flags.get(self.filter.currentIndex())
        query=self.search.text().strip().casefold()
        for index in range(self.table.rowCount()):
            row=self.table.item(index,0).data(Qt.ItemDataRole.UserRole)
            text=' '.join([row.operation.sku or '',row.operation.identifier,row.operation.original_sale_id or '']).casefold()
            self.table.setRowHidden(index,(bool(flag) and flag not in row.flags) or query not in text)
