"""Реклама: затраты, пополнения, потребление и удержания сохраняют разные смыслы."""
from collections import defaultdict
from dataclasses import dataclass
from datetime import datetime,date,timedelta,timezone
from decimal import Decimal
from .finance import ZERO,money
from .data_views import records

from .business_dates import moscow_operation_day as charge_day


def selected_statistics(ledger,shop,start,end):
    for entry in records(ledger,shop,'ad_statistics'):
        request=entry['request']
        if request['from']>end.isoformat() or request['to']<start.isoformat():continue
        yield entry


@dataclass(frozen=True)
class CampaignMetric:
    campaign_id: int
    spend: Decimal
    orders: int
    clicks: int
    views: int
    order_amount: Decimal
    currency: str


@dataclass(frozen=True)
class AdAnalysis:
    currency: str | None
    billed: Decimal | None
    undated_billed: Decimal
    funding: Decimal | None
    consumed: Decimal | None
    campaigns: tuple[CampaignMetric,...]
    model_spend: dict[str,Decimal]
    unallocated: Decimal | None
    issues: tuple[str,...]
    statistics_complete: bool

    @property
    def invoice_statistics_difference(self):
        if self.currency and self.billed is not None and self.consumed is not None:
            return self.billed-self.consumed
        return None

    def drr(self,revenue: Decimal | None):
        if (not self.statistics_complete or self.currency!='RUB' or self.consumed is None
                or revenue is None or money(revenue)<=0):return None
        return self.consumed/money(revenue)*Decimal('100')


def analyse_advertising(ledger,shop,start,end):
    issues=[]
    account=records(ledger,shop,'ad_account')
    account_currency=account[0].get('currency') if account else None
    billed=ZERO;undated=ZERO;has_undated=False
    for row in records(ledger,shop,'advertising'):
        amount=money(row['updSum']);day=charge_day(row.get('updTime'))
        if day is None:
            # Час/период неизвестны: не переносим сумму в выбранный день.
            undated+=amount
            has_undated=has_undated or bool(amount)
        elif start<=day<=end:billed+=amount
    ranges=ledger.setting(shop,'source_coverage:advertising',[])
    billing_loaded=any(left<=start.isoformat() and right>=end.isoformat() for left,right in ranges)
    billing_state=ledger.db.execute("SELECT completed FROM sync_state WHERE store=? AND source='advertising'",(shop,)).fetchone()
    if billing_state and not billing_state[0]:billing_loaded=False
    if not billing_loaded:issues.append('Реестр выставленных рекламных затрат за период загружен не полностью')
    if has_undated:issues.append('Есть выставленные затраты без достоверной даты; они не включены в итог выбранного периода')
    funding=ZERO;have_funding=False
    for row in records(ledger,shop,'ad_payments'):
        day=charge_day(row.get('date'))
        if day is None:
            if row.get('statusId')==1 and money(row['sum']):issues.append('Есть пополнение без достоверной даты')
            continue
        if not start<=day<=end:continue
        if row.get('statusId')!=1:continue
        if row.get('type')==3 and row.get('cardStatus') not in {'success','succeeded'}:
            issues.append('Операция картой не подтверждена как успешная');continue
        if row.get('currency')!=account_currency:
            issues.append('Валюта пополнения не подтверждена или отличается от валюты счёта');continue
        funding+=money(row['sum']);have_funding=True
    campaign_ranges=defaultdict(list)
    required_campaigns=set()
    for charge in records(ledger,shop,'advertising'):
        day=charge_day(charge.get('updTime'))
        if day is not None and start<=day<=end and money(charge['updSum']):
            required_campaigns.add(charge['advertId'])
    metrics={};model_spend=defaultdict(lambda:ZERO);unallocated=ZERO;currencies=set();have_stats=False;missing=False
    for entry in selected_statistics(ledger,shop,start,end):
        value=entry.get('data')
        if value is None:
            missing=True;issues.append('API не вернул статистику для части запрошенных кампаний');continue
        currency=value['currency'];currencies.add(currency)
        campaign_ranges[value['advertId']].append([entry['request']['from'],entry['request']['to']])
        required_campaigns.add(value['advertId'])
        # Суммы верхнего уровня — контроль, а не дополнительный расход.
        day_total=sum((money(day['sum']) for day in value['days']),ZERO)
        if day_total!=money(value['sum']):
            missing=True;issues.append('Итог кампании не совпадает с суммой дней')
        campaign_id=value['advertId']
        key=(campaign_id,currency)
        current=None
        for day in value['days']:
            when=date.fromisoformat(day['date'][:10]) # отчётный день API, не время отдельной операции
            if not start<=when<=end:continue
            have_stats=True
            current=metrics.setdefault(key,{'spend':ZERO,'orders':0,'clicks':0,'views':0,'order_amount':ZERO})
            current['spend']+=money(day['sum']);current['order_amount']+=money(day['sum_price'])
            for count in ('orders','clicks','views'):current[count]+=day[count]
            assigned=ZERO;app_total=ZERO
            for app in day['apps']:
                app_total+=money(app['sum'])
                for nm in app['nms']:
                    amount=money(nm['sum']);assigned+=amount
                    model_spend[str(nm['nmId'])]+=amount
            difference=money(day['sum'])-assigned
            unallocated+=difference
            if difference or app_total!=money(day['sum']):
                issues.append('Расходы по платформам/артикулам не сверены с итогом дня')
        if not value['days'] and money(value['sum'])==0:
            have_stats=True # явный нулевой итог API, а не отсутствие ответа
            metrics.setdefault(key,{'spend':ZERO,'orders':0,'clicks':0,'views':0,'order_amount':ZERO})
    currency=account_currency
    if not currency and len(currencies)==1:currency=next(iter(currencies))
    if len(currencies)>1 or (currencies and currency not in currencies):
        missing=True;issues.append('В статистике встречаются разные валюты; суммирование отключено')
        currency=None
    rows=tuple(CampaignMetric(identifier,**values,currency=code) for (identifier,code),values in sorted(metrics.items()))
    consumed=sum((m.spend for m in rows),ZERO) if have_stats and currency is not None else None
    from .coverage import merge_ranges
    for identifier in required_campaigns:
        spans=[]
        for left,right in campaign_ranges[identifier]:
            spans=merge_ranges(spans,date.fromisoformat(left),date.fromisoformat(right))
        if not any(left<=start.isoformat() and right>=end.isoformat() for left,right in spans):
            missing=True;issues.append('Не весь период статистики доступен для каждой кампании с затратами')
    ranges=ledger.setting(shop,'source_coverage:ad_statistics',[])
    complete=any(left<=start.isoformat() and right>=end.isoformat() for left,right in ranges) and have_stats and not missing and currency is not None
    state=ledger.db.execute("SELECT completed FROM sync_state WHERE store=? AND source='ad_statistics'",(shop,)).fetchone()
    if state and not state[0]:complete=False
    if not complete:issues.append('Рекламная статистика за период не подтверждена полностью; ДРР не рассчитан')
    if unallocated:issues.append('Не вся сумма имеет сверенную привязку к артикулу WB; размеры не распределяются')
    return AdAnalysis(currency,billed if billing_loaded and not has_undated and account_currency else None,undated,
                      funding if have_funding else None,consumed,rows,
                      dict(model_spend) if currency else {},unallocated if have_stats and currency else None,
                      tuple(dict.fromkeys(issues)),complete)
