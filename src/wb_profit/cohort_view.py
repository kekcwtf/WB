"""Предварительный выкуп одной когорты заказов; не финансовые продажи периода."""
from decimal import Decimal
from collections import defaultdict
from PySide6.QtCore import Qt
from PySide6.QtWidgets import QWidget,QVBoxLayout,QLabel,QTableWidgetItem
from .cohorts import cohorts_from_ledger,cohort_rows
from .data_views import product_metadata
from .inventory import source_state
from .ui_tables import NumericItem,read_only_table


class CohortView(QWidget):
    def __init__(self,parent=None):
        super().__init__(parent)
        self.analysis=None;self.rows_by_sku={};self.categories=set()
        layout=QVBoxLayout(self);layout.setContentsMargins(0,0,0,0)
        notice=QLabel('Когорта — заказы, созданные в выбранный период. Исходы ищутся по srid и точному SKU во всей доступной истории. Процент показан только при полном покрытии и отсутствии открытых или неоднозначных заказов. Возврат после покупки не отменяет факт выкупа и показан отдельно. Данные API предварительные, могут не включать неподтверждённые оплаты; это не финансовые продажи периода. Оперативная история WB ограничена 90 днями; более ранние когорты доступны, если сохранены локально.')
        notice.setWordWrap(True);layout.addWidget(notice)
        self.summary=QLabel();self.summary.setWordWrap(True);self.summary.setTextFormat(Qt.TextFormat.PlainText)
        layout.addWidget(self.summary)
        self.table=read_only_table(['SKU / модель','Артикул продавца','Название','Категория','Заказов распознано','Выкуплены по исходам','Отменены','Открыты / нет исхода','Неоднозначные исходы','Возвраты после покупки','Выкуп когорты, %','Достоверность'])
        layout.addWidget(self.table)

    def clear(self,message='Нет загруженной когорты заказов'):
        self.analysis=None;self.rows_by_sku={};self.categories=set();self.table.setRowCount(0);self.summary.setText(message)

    def refresh(self,ledger,shop,start,end,*,grouped=False,now=None):
        report=cohorts_from_ledger(ledger,shop,start,end,now=now)
        self.analysis=report;self.rows_by_sku=cohort_rows(report,grouped=grouped)
        metadata=product_metadata(ledger,shop)
        evidence=defaultdict(list)
        for outcome in report.outcomes:evidence[outcome.sku.split(':',1)[0] if grouped else outcome.sku].append(outcome)
        self.categories=set()
        orders_loaded=source_state(ledger,shop,'orders')[1] or '—'
        sales_loaded=source_state(ledger,shop,'sales')[1] or '—'
        self.summary.setText(f'Когорта по дате заказа: {start:%d.%m.%Y}–{end:%d.%m.%Y}. '
                             f'Последняя полная загрузка: заказы {orders_loaded}; продажи/возвраты {sales_loaded}.\n'+
                             ('Выгрузка покрыта; исходы всё равно предварительные. ' if report.history_complete else 'Покрытие неполное: числа — только загруженные наблюдения, не итог всей когорты. ')+
                             ' '.join(report.reasons))
        self.table.setSortingEnabled(False);self.table.setRowCount(len(self.rows_by_sku))
        for index,(sku,row) in enumerate(sorted(self.rows_by_sku.items())):
            card=metadata.get(sku.split(':',1)[0],{})
            vendors={entry.vendor for entry in evidence[sku] if entry.vendor}
            subjects={entry.category for entry in evidence[sku] if entry.category}
            vendor=str(card.get('vendorCode') or (next(iter(vendors)) if len(vendors)==1 else ''))
            category=str(card.get('subjectName') or (next(iter(subjects)) if len(subjects)==1 else ''))
            if category:self.categories.add(category)
            outcomes={'bought':'Покупка зарегистрирована','cancelled':'Отмена','pending':'Открыт / нет исхода','ambiguous':'Неоднозначный исход'}
            proof='Примеры исходов (до 10 заказов):\n'+'\n'.join(entry.srid+': '+('; '.join(entry.issues) or outcomes[entry.outcome]) for entry in evidence[sku][:10])
            status=('Когорта закрыта по оперативным данным' if row.buyout_percent is not None else
                    'Нет полного покрытия' if not report.history_complete else 'Есть открытые / неоднозначные заказы')
            values=[sku,vendor,str(card.get('title') or ''),category,
                    row.orders,row.bought,row.cancelled,row.pending,row.ambiguous,row.returned,row.buyout_percent,status]
            for column,value in enumerate(values):
                item=NumericItem(Decimal(value) if isinstance(value,int) else value) if value is None or isinstance(value,(int,Decimal)) else QTableWidgetItem(value)
                item.setToolTip('Выкуп = выкупленные заказы / все заказы одной завершённой когорты × 100. Данные предварительные. Неполная история и незавершённые исходы дают прочерк, а не 0%.\n'+proof)
                self.table.setItem(index,column,item)
        self.table.setSortingEnabled(True)

    def filter_rows(self,query='',category='Все категории'):
        query=query.strip().casefold()
        for row in range(self.table.rowCount()):
            text=' '.join(self.table.item(row,column).text() for column in range(4)).casefold()
            self.table.setRowHidden(row,query not in text or category!='Все категории' and self.table.item(row,3).text()!=category)
