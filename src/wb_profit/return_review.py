"""Проверка сохранённых связей возвратов, без назначения налоговых вычетов."""
from collections import defaultdict
from dataclasses import dataclass
from decimal import Decimal
from .finance import Operation,Kind,ZERO,calculate,reconcile_versions
from .taxes import rule_at


@dataclass(frozen=True)
class ReturnReviewRow:
    operation: Operation
    sale: Operation | None
    linked: bool
    returned_units: int | None
    returned_amount: Decimal | None
    cumulative_units: int | None
    cumulative_amount: Decimal | None
    restored_cost: Decimal | None
    original_usn: Decimal | None
    original_vat: Decimal | None
    usn_price_component: Decimal | None
    vat_price_component: Decimal | None
    flags: tuple[str,...]
    issues: tuple[str,...]


@dataclass(frozen=True)
class ReturnReview:
    rows: tuple[ReturnReviewRow,...]
    linked_count: int
    missing_cost_count: int
    price_check_count: int
    known_restored_cost: Decimal


def review_returns(selected, originals, history, costs, rules):
    selected=reconcile_versions(selected)
    returns=[op for op in selected if op.kind==Kind.RETURN]
    originals=reconcile_versions(originals)
    history=reconcile_versions([*history,*returns])
    if len({op.store for op in [*selected,*originals,*history]})>1:
        raise ValueError('Проверка возвратов должна относиться к одному магазину')
    index={(op.source,op.identifier):op for op in originals}
    grouped=defaultdict(list)
    for op in history:
        if op.kind==Kind.RETURN and op.original_sale_id:grouped[(op.source,op.original_sale_id)].append(op)
    costs=list(costs);rules=list(rules)
    cost_index=defaultdict(list)
    for cost in costs:cost_index[cost.sku].append(cost)
    rows=[]
    for returned in sorted(returns,key=lambda op:(op.occurred,op.source,op.identifier)):
        issues=[];flags=[];sale=None
        quantity=amount=None
        if returned.confirmed:
            quantity=-returned.units if returned.units<0 else None;amount=-returned.amount
        else:
            flags.append('unlinked');issues.append('Возврат предварительный; не является финансовым фактом')
        if not returned.original_sale_id:
            flags.append('unlinked');issues.append('Нет однозначной связи с исходной продажей')
        else:
            sale=index.get((returned.source,returned.original_sale_id))
            if sale is None or sale.kind!=Kind.SALE or not sale.confirmed:
                flags.append('unlinked');issues.append('Исходная подтверждённая продажа не найдена в сохранённой истории того же источника')
                sale=None
            elif (not isinstance(sale.sku,str) or not sale.sku or returned.sku!=sale.sku or returned.cost_basis_date!=sale.occurred or returned.occurred<sale.occurred):
                flags.append('unlinked');issues.append('Не совпадают SKU или дата исходной продажи')
        related=grouped.get((returned.source,returned.original_sale_id),[])
        cumulative_qty=-sum(op.units for op in related if op.confirmed) if sale else None
        cumulative_amount=-sum((op.amount for op in related if op.confirmed),ZERO) if sale else None
        if sale and any(not op.confirmed for op in related):
            flags.append('amount');issues.append('Есть предварительные связанные возвраты; итог истории не подтверждён')
        if sale and (sale.units<=0 or returned.units>=0 or cumulative_qty>sale.units):
            flags.extend(['unlinked','amount']);issues.append('Количество возвратов не подтверждено или превышает исходную продажу')
        if sale and cumulative_amount>sale.amount:
            flags.append('amount');issues.append('Сумма связанных возвратов превышает исходную выручку; требуется проверка корректировок')
        if sale and -returned.amount*sale.units != sale.amount*(-returned.units):
            flags.append('amount');issues.append('Суммы за единицу не совпадают или требуют проверки округления')
        linked='unlinked' not in flags
        restored=None
        if linked:
            result=calculate([returned],[*cost_index.get(returned.sku,()),*cost_index.get('*',())],None)
            if not result.missing_costs:restored=-result.cost
            else:
                flags.append('cost');issues.append('Нет назначенной себестоимости на дату исходной продажи')
        origin_rule=rule_at(rules,sale.occurred) if sale and sale.kind==Kind.SALE else None
        return_rule=rule_at(rules,returned.occurred)
        if origin_rule is None or not origin_rule.settings.confirmed_by_user:
            flags.append('tax');issues.append('Нет подтверждённых налоговых настроек на дату исходной продажи')
        if return_rule is None or not return_rule.settings.confirmed_by_user:
            flags.append('tax');issues.append('Нет подтверждённых налоговых настроек на дату возврата')
        if sale and (sale.occurred.year,(sale.occurred.month-1)//3)!=(returned.occurred.year,(returned.occurred.month-1)//3):
            flags.append('quarter');issues.append('Продажа и возврат в разных календарных кварталах; налоговая периодизация не подтверждена')
        if origin_rule and return_rule and origin_rule.settings!=return_rule.settings:
            flags.append('rate');issues.append('Налоговые настройки продажи и возврата различаются; текущая ставка не подставляется вместо исходной')
        usn=vat=None
        if linked and 'amount' not in flags and origin_rule and origin_rule.settings.confirmed_by_user:
            tax=origin_rule.settings
            if tax.vat_included or not tax.vat_percent:usn,vat=tax.estimate(amount)
            else:
                flags.append('tax');issues.append('Не подтверждено включение НДС в исходную сумму WB')
        issues.append('НДС в цене и УСН с суммы — оценки по исходной ставке, не налоговый вычет и не подтверждённое уменьшение обязательства')
        rows.append(ReturnReviewRow(returned,sale,linked,quantity,amount,cumulative_qty,cumulative_amount,restored,
                                    origin_rule.settings.usn_percent if origin_rule else None,
                                    origin_rule.settings.vat_percent if origin_rule else None,
                                    usn,vat,tuple(dict.fromkeys(flags)),tuple(dict.fromkeys(issues))))
    return ReturnReview(tuple(rows),sum(row.linked for row in rows),sum('cost' in row.flags for row in rows),
                        sum('amount' in row.flags for row in rows),sum((row.restored_cost for row in rows if row.restored_cost is not None),ZERO))


def review_from_ledger(ledger,shop,selected,costs,rules):
    returns=[op for op in selected if op.kind==Kind.RETURN]
    keys={(op.source,op.original_sale_id) for op in returns if op.original_sale_id}
    originals=ledger.operations_by_keys(shop,keys)
    history=ledger.operations_by_keys(shop,keys,referring_returns=True)
    return review_returns(returns,originals,history,costs,rules)
