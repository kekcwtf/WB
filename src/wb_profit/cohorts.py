"""Когорты заказов: исходы по srid + точному SKU, а не отношение разных периодов."""
from collections import defaultdict
from dataclasses import dataclass
from datetime import date,datetime,timedelta
from decimal import Decimal
import json
from .business_dates import MOSCOW,statistics_timestamp
from .inventory import stock_key,source_state,fresh


@dataclass(frozen=True)
class OrderOutcome:
    srid: str
    sku: str
    ordered: datetime
    outcome: str
    returned: bool
    issues: tuple[str,...]
    vendor: str = ''
    category: str = ''


@dataclass(frozen=True)
class CohortSku:
    sku: str
    orders: int
    bought: int
    cancelled: int
    pending: int
    ambiguous: int
    returned: int
    buyout_percent: Decimal | None


@dataclass(frozen=True)
class CohortReport:
    rows: tuple[CohortSku,...]
    outcomes: tuple[OrderOutcome,...]
    history_complete: bool
    reasons: tuple[str,...]


def summarize_cohort(sku,outcomes,complete):
    outcomes=list(outcomes)
    orders=len(outcomes)
    bought=sum(row.outcome=='bought' for row in outcomes)
    cancelled=sum(row.outcome=='cancelled' for row in outcomes)
    pending=sum(row.outcome=='pending' for row in outcomes)
    ambiguous=sum(row.outcome=='ambiguous' for row in outcomes)
    returned=sum(row.returned for row in outcomes)
    rate=Decimal(bought)/Decimal(orders)*100 if complete and orders and not pending and not ambiguous else None
    return CohortSku(sku,orders,bought,cancelled,pending,ambiguous,returned,rate)


def cohort_rows(report,*,grouped=False):
    groups=defaultdict(list)
    for row in report.outcomes:groups[row.sku.split(':',1)[0] if grouped else row.sku].append(row)
    return {sku:summarize_cohort(sku,rows,report.history_complete) for sku,rows in groups.items()}


def order_cohorts(orders,sales,start,end,*,history_complete=False,today=None):
    if start>end:raise ValueError('Неверный период когорты')
    today=today or datetime.now(MOSCOW).date()
    reasons=[];indexed={};conflicts=set();malformed=0
    for row in orders:
        if not isinstance(row,dict):malformed+=1;continue
        if row.get('warehouseType')=='Склад продавца':continue
        stamp=statistics_timestamp(row.get('date'))
        if stamp is None:
            malformed+=1;continue
        if not start<=stamp.date()<=end:continue
        key=stock_key(row);srid=row.get('srid')
        if row.get('warehouseType')!='Склад WB' or key is None or not isinstance(srid,str) or not srid.strip() or stamp.date()>today:
            malformed+=1;continue
        if srid in indexed and indexed[srid]!=row:conflicts.add(srid)
        else:indexed[srid]=row
    for srid in conflicts:indexed.pop(srid,None)
    if malformed:reasons.append(f'Не распознаны строки заказов: {malformed}; полнота когорты не подтверждена')
    if conflicts:reasons.append(f'Противоречивых srid в заказах: {len(conflicts)}; они не распределены между SKU')
    if end>today:reasons.append('Период когорты ещё не завершился')
    by_order=defaultdict(list);sale_ids={};bad_ids=set();orphan=0
    for row in sales:
        if not isinstance(row,dict):orphan+=1;continue
        stamp=statistics_timestamp(row.get('date'))
        srid=row.get('srid')
        if not isinstance(srid,str) or not srid.strip():
            if row.get('warehouseType')!='Склад продавца' and (stamp is None or stamp.date()>=start):orphan+=1
            continue
        if srid not in indexed:continue
        identifier=row.get('saleID')
        if isinstance(identifier,str):
            if identifier in sale_ids:
                if sale_ids[identifier]!=row:
                    bad_ids.add(srid);bad_ids.add(sale_ids[identifier].get('srid'))
                continue
            sale_ids[identifier]=row
        by_order[srid].append(row)
    if orphan:reasons.append(f'Есть продажи/возвраты без srid: {orphan}; их принадлежность к когорте не установлена')
    complete=bool(history_complete and not malformed and not conflicts and not orphan and end<=today)
    if not history_complete:reasons.append('Нет полного свежего покрытия заказов и исходов до текущего дня')
    outcomes=[]
    for srid,order in indexed.items():
        issues=[];key=stock_key(order);ordered=statistics_timestamp(order['date'])
        events=by_order[srid];bought=[];returned=[]
        if type(order.get('isCancel')) is not bool:issues.append('Неизвестен флаг отмены заказа')
        if srid in bad_ids:issues.append('Противоречивые версии saleID')
        for event in events:
            when=statistics_timestamp(event.get('date'));identifier=event.get('saleID')
            if (event.get('warehouseType')!='Склад WB' or stock_key(event)!=key or when is None or
                    when<ordered or when.date()>today or not isinstance(identifier,str) or len(identifier)<2 or identifier[0] not in 'SR'):
                issues.append('Дата, SKU, схема или тип исхода не подтверждены');continue
            (bought if identifier.startswith('S') else returned).append(when)
        if len(bought)>1 or len(returned)>1:issues.append('Несколько продаж/возвратов одного заказа: исход неоднозначен')
        if bought and order.get('isCancel'):issues.append('Одновременно отмечены продажа и отмена')
        if returned and (not bought or min(returned)<min(bought)):issues.append('Возврат не связан с единственной предшествующей продажей')
        outcome=('ambiguous' if issues else 'bought' if bought else 'cancelled' if order['isCancel'] else 'pending')
        outcomes.append(OrderOutcome(srid,':'.join(key),ordered,outcome,bool(returned and bought and not issues),tuple(dict.fromkeys(issues)),
                                     order.get('supplierArticle') if isinstance(order.get('supplierArticle'),str) else '',
                                     order.get('subject') if isinstance(order.get('subject'),str) else ''))
    groups=defaultdict(list)
    for row in outcomes:groups[row.sku].append(row)
    rows=tuple(summarize_cohort(sku,group,complete) for sku,group in sorted(groups.items()))
    return CohortReport(rows,tuple(outcomes),complete,tuple(reasons))


def cohorts_from_ledger(ledger,shop,start,end,*,now=None):
    now=now or datetime.now(MOSCOW)
    if now.tzinfo is None:raise ValueError('Нужен часовой пояс наблюдения')
    left=(start-timedelta(days=1)).isoformat() if start>date.min else start.isoformat()
    right=(end+timedelta(days=1)).isoformat() if end<date.max else end.isoformat()
    # Запас в один день учитывает смещение ISO-дат с UTC/другими поясами в Москву.
    orders={identifier:json.loads(payload) for identifier,payload in ledger.db.execute(
        "SELECT identifier,payload FROM source_records WHERE store=? AND source='orders' AND substr(json_extract(payload,'$.date'),1,10) BETWEEN ? AND ?",(shop,left,right))}
    malformed=ledger.db.execute("""SELECT identifier,payload FROM source_records WHERE store=? AND source='orders' AND
        (json_type(payload,'$.date') IS NOT 'text' OR date(substr(json_extract(payload,'$.date'),1,10),'+0 days') IS NULL OR
         date(substr(json_extract(payload,'$.date'),1,10),'+0 days')!=substr(json_extract(payload,'$.date'),1,10))""",(shop,))
    for identifier,payload in malformed:orders[identifier]=json.loads(payload)
    srids=sorted({row['srid'] for row in orders.values() if isinstance(row,dict) and isinstance(row.get('srid'),str) and row['srid'].strip()})
    sales={}
    for offset in range(0,len(srids),400):
        batch=srids[offset:offset+400];placeholders=','.join('?' for _ in batch)
        for identifier,payload in ledger.db.execute(f"SELECT identifier,payload FROM source_records WHERE store=? AND source='sales' AND json_extract(payload,'$.srid') IN ({placeholders})",(shop,*batch)):
            sales[identifier]=json.loads(payload)
    for identifier,payload in ledger.db.execute("SELECT identifier,payload FROM source_records WHERE store=? AND source='sales' AND (json_type(payload,'$.srid') IS NOT 'text' OR trim(json_extract(payload,'$.srid'))='')",(shop,)):
        sales[identifier]=json.loads(payload)
    complete=True
    for source in ('orders','sales'):
        state=source_state(ledger,shop,source)
        ranges=ledger.setting(shop,'source_coverage:'+source,[])
        if (not state[0] or state[2]!='success' or not fresh(state[1],now) or
                not any(a<=start.isoformat() and b>=now.astimezone(MOSCOW).date().isoformat() for a,b in ranges)):
            complete=False
    return order_cohorts(list(orders.values()),list(sales.values()),start,end,history_complete=complete,today=now.astimezone(MOSCOW).date())
