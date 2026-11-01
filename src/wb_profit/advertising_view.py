from decimal import Decimal
from PySide6.QtCore import Qt
from PySide6.QtWidgets import QWidget,QVBoxLayout,QLabel,QScrollArea,QTableWidgetItem
from .ui_tables import NumericItem,read_only_table
from .ad_analytics import analyse_advertising,charge_day
from .data_views import records,advertising_rows
from .coverage import financial_period_reasons
from .finance import Kind,ZERO,money

class AdvertisingView(QScrollArea):
    def __init__(self):
        super().__init__()
        self.setWidgetResizable(True)
        page=QWidget();self.setWidget(page);layout=QVBoxLayout(page)
        title=QLabel('Реклама: затраты и источники оплаты')
        title.setStyleSheet('font-size: 22px; font-weight: 600');layout.addWidget(title)
        notice=QLabel('Пополнение счёта, расход кампании и удержание WB — разные операции. Эти экраны не списывают суммы повторно из прибыли. Связь с финансовыми удержаниями пока не подтверждена.')
        notice.setWordWrap(True);layout.addWidget(notice)
        self.summary=QLabel('Нет загруженных рекламных данных')
        self.summary.setTextFormat(Qt.TextFormat.PlainText);self.summary.setWordWrap(True);layout.addWidget(self.summary)
        self.issues=QLabel();self.issues.setTextFormat(Qt.TextFormat.PlainText);self.issues.setWordWrap(True);layout.addWidget(self.issues)
        self.campaigns=read_only_table(['Кампания','Расход по статистике','Валюта','Показы','Клики','Заказы по статистике','Сумма заказов по статистике','Примечание'])
        self.models=read_only_table(['Артикул WB','Расход по статистике','Валюта','Привязка'])
        self.charges=read_only_table(['Дата операции','Кампания','Название','Выставленные затраты (валюта профиля)','Источник оплаты'])
        self.payments=read_only_table(['ID пополнения','Дата','Сумма','Валюта','Источник','Состояние'])
        for heading,table in [('Кампании',self.campaigns),('Артикулы WB: размеры не распределяются',self.models),('Выставленные затраты',self.charges),('Пополнения: не являются расходом прибыли',self.payments)]:
            layout.addWidget(QLabel(heading));table.setMinimumHeight(160);table.setMaximumHeight(240);layout.addWidget(table)
        layout.addStretch()
        self.analysis=None

    def fill(self,table,rows):
        table.setSortingEnabled(False);table.setRowCount(len(rows))
        for row,values in enumerate(rows):
            for column,value in enumerate(values):
                item=NumericItem(Decimal(value) if value is not None else None) if isinstance(value,(Decimal,int)) or value is None else QTableWidgetItem(str(value))
                if isinstance(value,int):item.setText(str(value))
                table.setItem(row,column,item)
        table.setSortingEnabled(True)

    def invalid_period(self):
        self.analysis=None
        self.summary.setText('Дата начала периода должна быть не позже даты окончания')
        self.issues.clear()
        for table in (self.campaigns,self.models,self.charges,self.payments):table.setRowCount(0)

    def refresh(self,ledger,shop,start,end):
        report=analyse_advertising(ledger,shop,start,end);self.analysis=report
        account=records(ledger,shop,'ad_account');account=account[0] if account else None
        currency=report.currency or 'валюта не подтверждена'
        def amount(value,code=None):return '—' if value is None else f"{value:.2f} {code or currency}"
        account_code=account.get('currency') if account else None
        revenue=None
        if not financial_period_reasons(ledger,shop,start,end):
            ops=ledger.operations(shop,start,end)
            if not any(op.kind in {Kind.UNKNOWN,Kind.ADJUSTMENT,Kind.COMPENSATION} for op in ops):
                revenue=sum((op.amount for op in ops if op.kind in {Kind.SALE,Kind.RETURN}),ZERO)
        drr=report.drr(revenue)
        wallet=(f"Счёт продвижения: {amount(money(account['balance']),account_code)}\n"
                f"Лимит оплаты из будущих продаж: {amount(money(account['net']),account_code)}\n"
                f"Бонусы: {amount(money(account['bonus']),account_code)}\n") if account else 'Состояние рекламного счёта не загружено\n'
        state=ledger.db.execute("SELECT last_success FROM sync_state WHERE store=? AND source='ad_statistics'",(shop,)).fetchone()
        last=(state[0] if state and state[0] else 'не выполнялась')
        self.summary.setText('Последняя полная загрузка статистики: '+last+'\n'+wallet+f'Выставленные затраты за период: {amount(report.billed,account_code)}\n'
            +f'Потребление по статистике кампаний: {amount(report.consumed)}\n'
            +f'Успешные пополнения по загруженным записям: {amount(report.funding,account_code)}\n'
            +('ДРР: —\n' if drr is None else f'ДРР по статистике и подтверждённой выручке магазина: {drr:.2f}%\n')
            +'ROAS по фактическим продажам: нет подтверждённой привязки. Сумма заказов из рекламной статистики не заменяет выручку.')
        difference=report.invoice_statistics_difference
        comparison=(f'Разница выставленных затрат и потребления: {difference:.2f} {currency}. Совпадение сумм не подтверждает связь с удержаниями WB.' if difference is not None else 'Выставленные затраты и потребление нельзя полностью сопоставить за выбранный период.')
        issues=list(report.issues)
        for source,label in [('ad_statistics','Статистика'),('ad_account','Рекламный счёт'),('ad_payments','Пополнения')]:
            message=ledger.setting(shop,'source_message:'+source)
            if message and message!='Успешно':issues.append(label+': '+message)
        self.issues.setText(comparison+'\n'+'\n'.join(issues))
        campaigns=[(m.campaign_id,m.spend,m.currency,m.views,m.clicks,m.orders,m.order_amount,
                    'Заказы не зафиксированы в статистике' if m.orders==0 and m.spend>0 else 'Заказы не равны подтверждённым продажам') for m in report.campaigns]
        method='Прямая привязка WB к модели; размеры не распределены' if report.unallocated==0 else 'Привязка есть, суммы по артикулам не сверены'
        self.fill(self.campaigns,campaigns)
        self.fill(self.models,[(nm,amount,report.currency,method) for nm,amount in sorted(report.model_spend.items())])
        self.fill(self.charges,advertising_rows(ledger,shop,start,end))
        payments=[]
        for row in records(ledger,shop,'ad_payments'):
            when=charge_day(row.get('date'))
            if when is not None and not start<=when<=end:continue
            success=row.get('statusId')==1 and (row.get('type')!=3 or row.get('cardStatus') in {'success','succeeded'})
            status='Успешно' if success else 'Не подтверждено / ошибка'
            payments.append((str(row['id']),when.isoformat() if when else 'Дата/часовой пояс не указаны',money(row['sum']),row['currency'],{0:'Счёт',1:'Баланс',3:'Карта'}.get(row.get('type'),'Неизвестен'),status))
        self.fill(self.payments,payments)
