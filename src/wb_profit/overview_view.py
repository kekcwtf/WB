"""Основные финансовые цифры и разбивка с индивидуальной достоверностью."""
from decimal import Decimal
from PySide6.QtCore import Qt
from PySide6.QtWidgets import QWidget,QVBoxLayout,QGridLayout,QLabel,QFrame,QTableWidgetItem,QHeaderView
from .analytics import LABELS
from .overview import financial_overview
from .finance import Kind
from .ui_tables import read_only_table,NumericItem

CARDS = [('sales','Продажи'),('returns','Возвраты'),('revenue','Выручка после возвратов'),
         ('wb_cost','Расходы WB'),('cost','Себестоимость'),('net','Чистая прибыль · расчёт'),
         ('margin','Маржинальность'),('roi','ROI закупки'),('drr','ДРР · сверенная реклама')]
EXTRA = [('gross','Валовая прибыль · без НДС'),('operating','Операционный результат · до УСН'),
         ('before_tax','По финансовым проводкам · до НДС и УСН'),('usn','Расчётный УСН'),('vat','Расчётный НДС'),
         ('profitability','Рентабельность затрат'),('compensation','Компенсации · знаковый вклад'),
         ('adjustment','Корректировки · знаковый вклад'),('payout','Выплаты · только записи реестра')]


class OverviewView(QWidget):
    def __init__(self,parent=None):
        super().__init__(parent)
        self.analysis=None
        self.values={};self.statuses={};self.changes={}
        layout=QVBoxLayout(self)
        layout.setContentsMargins(0,0,0,0)
        grid=QGridLayout()
        for index,(key,title) in enumerate(CARDS):
            card=QFrame()
            card.setFrameShape(QFrame.Shape.StyledPanel)
            box=QVBoxLayout(card)
            heading=QLabel(title);heading.setWordWrap(True)
            value=QLabel('—');value.setStyleSheet('font-size: 24px; font-weight: 600')
            value.setTextFormat(Qt.TextFormat.PlainText)
            status=QLabel('нет данных');status.setWordWrap(True)
            box.addWidget(heading);box.addWidget(value);box.addWidget(status)
            change=QLabel('Сравнение: нет данных');change.setWordWrap(True)
            change.setTextFormat(Qt.TextFormat.PlainText)
            box.addWidget(change);self.changes[key]=change
            self.values[key]=value;self.statuses[key]=status
            grid.addWidget(card,index//3,index%3)
        layout.addLayout(grid)
        self.quality=QLabel('Нет данных для полного расчёта')
        self.quality.setWordWrap(True);self.quality.setTextFormat(Qt.TextFormat.PlainText)
        layout.addWidget(self.quality)
        self.details=read_only_table(['Показатель / статья','Сумма или процент','Единица','Достоверность'])
        self.details.horizontalHeader().setSectionResizeMode(0,QHeaderView.ResizeMode.Stretch)
        self.details.setMinimumHeight(240)
        self.details.setMaximumHeight(400)
        layout.addWidget(self.details)

    def clear(self,message='Нет подтверждённых финансовых операций'):
        self.analysis=None
        for key,label in self.values.items():
            label.setText('—');self.statuses[key].setText('нет данных')
            label.setToolTip('');label.setStyleSheet('font-size: 24px; font-weight: 600')
            self.changes[key].setText('Сравнение: нет данных');self.changes[key].setToolTip('')
        self.details.setRowCount(0);self.details.hide();self.quality.setText(message)

    def set_operations(self,operations,costs,rules,*,financial_complete=False,full_complete=False):
        for label in self.changes.values():
            label.setText('Сравнение: нет данных');label.setToolTip('')
        report=financial_overview(operations,costs,rules,financial_complete=financial_complete,full_complete=full_complete)
        self.analysis=report
        self.details.show()
        for key,label in self.values.items():
            metric=report.metrics[key]
            label.setText('—' if metric.value is None else f'{metric.value:,.2f} {metric.unit}'.replace(',',' '))
            label.setToolTip(metric.note)
            self.statuses[key].setText(metric.status if metric.value is not None else 'недостаточно данных')
            style='font-size: 24px; font-weight: 600'
            if key=='net' and metric.value is not None:style+='; color: '+('#c0392b' if metric.value<0 else '#218c4b')
            label.setStyleSheet(style)
        rows=[(label,report.metrics[key]) for key,label in EXTRA]
        rows.extend((LABELS[kind]+(' · внешние' if kind==Kind.EXTERNAL else ''),metric) for kind,metric in report.expenses.items())
        self.details.setSortingEnabled(False)
        self.details.setRowCount(len(rows))
        for index,(label,metric) in enumerate(rows):
            values=[label,metric.value,metric.unit,metric.status if metric.value is not None else 'недостаточно данных']
            for column,value in enumerate(values):
                item=NumericItem(value) if isinstance(value,Decimal) or value is None else QTableWidgetItem(value)
                item.setToolTip(metric.note)
                self.details.setItem(index,column,item)
        self.details.setSortingEnabled(True)
        control=(f'Контроль арифметики загруженных статей: разница {report.arithmetic_difference:.2f} ₽. '
                 if report.arithmetic_difference is not None else '')
        self.quality.setText(control+'НДС уже учтён в валовой и операционной прибыли; повторно не вычитается. Формулы — в подсказках показателей. '+
                             ('Период сверён; налоги расчётные.' if report.metrics['net'].value is not None else
                              'Полная чистая прибыль недоступна. '+ ' '.join(report.reasons)))

    def set_comparison(self, comparison):
        for key, label in self.changes.items():
            metric = comparison.metrics[key]
            period = f'Предыдущий равный период: {comparison.previous_start:%d.%m.%Y}–{comparison.previous_end:%d.%m.%Y}.'
            if metric.change is None:
                label.setText('Сравнение: недостаточно данных')
                label.setToolTip(period + ' ' + metric.unavailable_reason)
                continue
            relative = f' ({metric.relative_percent:+.2f}%)' if metric.relative_percent is not None else ''
            label.setText(f'К предыдущему периоду: {metric.change:+.2f} {metric.unit}{relative}')
            unit = '%' if metric.unit == 'п.п.' else metric.unit
            label.setToolTip(period + f' Было: {metric.previous:.2f} {unit}; сейчас: {metric.current:.2f} {unit}. '
                             'Сравнение двух равных по числу дней интервалов; расчётные налоги по настройкам каждого периода. '
                             + ('Изменение процентов — в процентных пунктах.' if unit == '%' else
                                'Процент изменения показан только при положительной предыдущей базе.'))
