"""Датированные налоговые настройки. Расчётная оценка, не налоговая декларация."""
from dataclasses import dataclass,replace
from datetime import date
from decimal import Decimal
from .finance import TaxSettings,Kind,ZERO,calculate,reconcile_versions

@dataclass(frozen=True)
class TaxRule:
    effective: date
    settings: TaxSettings

    def __post_init__(self):
        if type(self.effective) is not date or not isinstance(self.settings,TaxSettings):
            raise ValueError('Налоговая версия требует календарную дату и настройки ставок')


def load_rules(ledger,shop):
    return [TaxRule(date.fromisoformat(row['date']),TaxSettings(Decimal(row['usn']),Decimal(row['vat']),row['included'],row['confirmed']))
            for row in ledger.setting(shop,'tax_history',[])]


def rule_at(rules,day):
    indexed={}
    for rule in rules:
        if rule.effective in indexed and indexed[rule.effective]!=rule.settings:
            raise ValueError('Разные налоговые настройки на одну дату')
        indexed[rule.effective]=rule.settings
    eligible=[d for d in indexed if d<=day]
    return TaxRule(max(eligible),indexed[max(eligible)]) if eligible else None


def save_rule(ledger,shop,rule):
    from .tax_changes import change_rule
    return change_rule(ledger,shop,rule.effective,rule)


@dataclass(frozen=True)
class TaxPart:
    effective: date
    year: int
    quarter: int
    revenue: Decimal
    usn: Decimal
    vat: Decimal


def calculate_with_history(operations,costs,rules):
    operations=reconcile_versions(operations);rules=list(rules)
    result=calculate(operations,costs,None)
    issues=[];groups={};resolved={};missing_dates=set();unconfirmed=set()
    if any(op.kind in {Kind.UNKNOWN,Kind.ADJUSTMENT,Kind.COMPENSATION} for op in operations):
        issues.append('Налоговая классификация части операций не определена; итог налогов не рассчитан')
    if any(op.kind==Kind.RETURN for op in operations):
        issues.append('Налоговый эффект возврата требует отдельной периодизации; отрицательный налог не предполагается')
    for operation in operations:
        if operation.kind==Kind.RETURN:continue
        if operation.occurred not in resolved:resolved[operation.occurred]=rule_at(rules,operation.occurred)
        rule=resolved[operation.occurred]
        if rule is None:
            missing_dates.add(operation.occurred);continue
        if not rule.settings.vat_included and rule.settings.vat_percent>0:
            issues.append('Для финансовой суммы WB не подтверждена база цены без включённого НДС');continue
        if not rule.settings.confirmed_by_user:
            unconfirmed.add(rule.effective);continue
        if operation.kind!=Kind.SALE:continue
        key=(rule.effective,operation.occurred.year,(operation.occurred.month-1)//3+1)
        amount,settings=groups.get(key,(ZERO,rule.settings))
        groups[key]=(amount+operation.amount,settings)
    if missing_dates:
        issues.append(f'Нет налоговых настроек для {len(missing_dates)} дат операций: с {min(missing_dates).isoformat()} по {max(missing_dates).isoformat()}')
    if unconfirmed:
        issues.append(f'Не подтверждены налоговые настройки: {len(unconfirmed)} версий, начиная с {min(unconfirmed).isoformat()}')
    if not operations or not rules:issues.append('Назначьте налоговые настройки с датой начала действия')
    parts=[]
    for (effective,year,quarter),(revenue,settings) in sorted(groups.items()):
        usn,vat=settings.estimate(revenue)
        parts.append(TaxPart(effective,year,quarter,revenue,usn,vat))
    if issues:
        return replace(result,usn=None,vat=None,net_profit=None,
                       tax_limitations=(*result.tax_limitations,*dict.fromkeys(issues))),tuple(parts)
    usn=sum((part.usn for part in parts),ZERO);vat=sum((part.vat for part in parts),ZERO)
    eligible=bool(operations) and not result.missing_costs and not result.unknown_operations and len(result.tax_limitations)==1
    return replace(result,usn=usn,vat=vat,net_profit=result.result_before_tax-usn-vat if eligible else None),tuple(parts)
