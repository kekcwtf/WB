"""Представление прибыли SKU с сохранением контрольной суммы магазина.

Реестр не изменяется. Налоги распределяются внутри каждой версии ставок и
расчётного квартала; общие расходы — только по выбранному методу.
"""
from collections import defaultdict
from dataclasses import dataclass
from decimal import Decimal
from .finance import Kind,ZERO,allocate,calculate,reconcile_versions
from .taxes import calculate_with_history,rule_at

STORE='0:store'

def bucket(sku):return STORE if sku is None else '1:'+sku

def sku_from(key):return None if key==STORE else key[2:]

@dataclass(frozen=True)
class ProfitDistribution:
    net_by_sku: dict[str,Decimal]
    taxes_by_sku: dict[str,Decimal]
    usn_by_sku: dict[str,Decimal]
    vat_by_sku: dict[str,Decimal]
    common_by_sku: dict[str,Decimal]
    unallocated_net: Decimal
    store_net: Decimal
    difference: Decimal
    method: str
    warnings: tuple[str,...]


def distribute_profit(operations,costs,rules,*,coverage_confirmed=False,method='unallocated'):
    if method not in {'unallocated','revenue'}:raise ValueError('Неизвестный метод распределения')
    if not coverage_confirmed:return None
    operations=reconcile_versions(operations);costs=list(costs);rules=list(rules)
    whole,parts=calculate_with_history(operations,costs,rules)
    if whole.net_profit is None:return None
    grouped=defaultdict(list)
    revenue=defaultdict(lambda:ZERO)
    for op in operations:
        grouped[bucket(op.sku)].append(op)
        if op.kind==Kind.SALE:revenue[bucket(op.sku)]+=op.amount
    taxes=defaultdict(lambda:ZERO)
    usn=defaultdict(lambda:ZERO);vat=defaultdict(lambda:ZERO)
    version_weights=defaultdict(lambda:defaultdict(lambda:ZERO));resolved={}
    for op in operations:
        if op.kind!=Kind.SALE:continue
        if op.occurred not in resolved:resolved[op.occurred]=rule_at(rules,op.occurred)
        rule=resolved[op.occurred]
        if rule:
            version_weights[(rule.effective,op.occurred.year,(op.occurred.month-1)//3+1)][bucket(op.sku)]+=op.amount
    for part in parts:
        weights=version_weights[(part.effective,part.year,part.quarter)]
        if sum(weights.values(),ZERO)<=ZERO:
            if part.usn or part.vat:raise ValueError('Нет базы распределения расчётных налогов')
            continue
        for amount,component in ((part.usn,usn),(part.vat,vat)):
            for key,value in allocate(amount,weights).items():
                taxes[key]+=value;component[key]+=value
    cost_index=defaultdict(list)
    for cost in costs:cost_index[cost.sku].append(cost)
    net={key:calculate(group,[*cost_index.get(sku_from(key),[]),*cost_index.get('*',[])],None).result_before_tax-taxes[key] for key,group in grouped.items()}
    common=defaultdict(lambda:ZERO);warnings=[]
    if method=='revenue':
        # Не распределяем непривязанные продажи/их себестоимость на известные товары.
        pool=sum((op.amount for op in grouped.get(STORE,[]) if op.kind not in {Kind.SALE,Kind.RETURN,Kind.PAYOUT,Kind.UNKNOWN}),ZERO)
        weights={key:value for key,value in revenue.items() if key!=STORE and value>ZERO}
        if pool:
            if not weights:
                warnings.append('Нет положительной выручки SKU: общие расходы оставлены у магазина')
            elif pool!=pool.quantize(Decimal('0.01')):
                warnings.append('Общая сумма содержит доли копейки: она сохранена без распределения и округления')
            else:
                for key,amount in allocate(pool,weights).items():
                    common[key]=amount;net[key]=net.get(key,ZERO)+amount
                net[STORE]=net.get(STORE,ZERO)-pool
    unallocated=net.get(STORE,ZERO)
    named={sku_from(key):value for key,value in net.items() if key!=STORE}
    difference=sum(named.values(),ZERO)+unallocated-whole.net_profit
    if difference:raise ValueError('Сумма результатов SKU и нераспределённых операций не совпадает с магазином')
    def named_values(values):return {sku_from(k):v for k,v in values.items() if k!=STORE}
    return ProfitDistribution(net_by_sku=named,taxes_by_sku=named_values(taxes),
                              usn_by_sku=named_values(usn),vat_by_sku=named_values(vat),
                              common_by_sku=named_values(common),unallocated_net=unallocated,
                              store_net=whole.net_profit,difference=difference,method=method,warnings=tuple(warnings))
