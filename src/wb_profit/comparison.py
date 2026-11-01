"""Сопоставимые равные интервалы: сравниваем только достаточно полные данные."""
from dataclasses import dataclass
from datetime import date,timedelta
from decimal import Decimal
from .coverage import period_coverage
from .finance import calculate


@dataclass(frozen=True)
class Comparison:
    previous_start: date
    previous_end: date
    current_result: Decimal | None
    previous_result: Decimal | None
    change: Decimal | None
    change_percent: Decimal | None
    unavailable_reason: str | None


def previous_period(start: date,end: date):
    if start>end:raise ValueError('Неверный интервал сравнения')
    length=(end-start).days+1
    return start-timedelta(days=length),start-timedelta(days=1)


def compare_periods(ledger,shop,start,end,costs):
    previous_start,previous_end=previous_period(start,end)
    costs=list(costs)
    results=[]
    for left,right in [(start,end),(previous_start,previous_end)]:
        covered,_=period_coverage(ledger,shop,left,right)
        operations=ledger.operations(shop,left,right)
        if not covered or not operations:
            return Comparison(previous_start,previous_end,None,None,None,None,'Один из периодов не закрыт, не загружен полностью или не сверён')
        result=calculate(operations,costs,None)
        if result.missing_costs or result.unknown_operations:
            return Comparison(previous_start,previous_end,None,None,None,None,'Не хватает себестоимости или классификации операций для сравнения')
        results.append(result.result_before_tax)
    current,previous=results
    delta=current-previous
    percentage=delta/previous*Decimal('100') if previous>0 else None
    return Comparison(previous_start,previous_end,current,previous,delta,percentage,None)


@dataclass(frozen=True)
class MetricChange:
    current: Decimal | None
    previous: Decimal | None
    change: Decimal | None
    relative_percent: Decimal | None
    unit: str
    unavailable_reason: str | None


@dataclass(frozen=True)
class MetricComparison:
    previous_start: date
    previous_end: date
    metrics: dict[str, MetricChange]


COMPARISON_KEYS = ('sales', 'returns', 'revenue', 'wb_cost', 'cost', 'net',
                   'margin', 'roi', 'drr', 'before_tax')
FINANCIAL_ONLY = {'sales', 'returns', 'revenue', 'cost'}


def compare_metrics(ledger, shop, start, end, costs, rules):
    """Каждый показатель сравнивается при достаточности обоих равных интервалов.

    Проценты в метриках отличаются в п.п.; относительный рост денежных значений
    показывается только при положительной предыдущей базе.
    """
    from .coverage import financial_period_reasons
    from .finance import Kind
    from .overview import financial_overview
    previous_start, previous_end = previous_period(start, end)
    costs, rules = list(costs), list(rules)
    reports, financial, full = [], [], []
    for left, right in ((start, end), (previous_start, previous_end)):
        operations = ledger.operations(shop, left, right)
        classified = bool(operations) and all(op.confirmed and op.kind != Kind.UNKNOWN for op in operations)
        has_wb = any(op.source != 'external_expenses' and op.kind not in {Kind.PAYOUT, Kind.UNKNOWN} for op in operations)
        financial_ready = not financial_period_reasons(ledger, shop, left, right) and classified and has_wb
        covered, _ = period_coverage(ledger, shop, left, right)
        full_ready = financial_ready and covered
        reports.append(financial_overview(operations, costs, rules,
                       financial_complete=financial_ready, full_complete=full_ready))
        financial.append(financial_ready)
        full.append(full_ready)
    metrics = {}
    for key in COMPARISON_KEYS:
        current, previous = (report.metrics[key] for report in reports)
        reason = None
        if not all(financial):
            reason = 'Один из финансовых периодов не полностью загружен, не закрыт или не сверён'
        elif key not in FINANCIAL_ONLY and not all(full):
            reason = 'Для сравнения нужны все расходы, включая сверенную рекламу, в обоих периодах'
        elif current.value is None or previous.value is None:
            reason = 'Не хватает себестоимости, налоговой модели или базы показателя в одном из периодов'
        if reason:
            metrics[key] = MetricChange(None, None, None, None,
                                       'п.п.' if current.unit == '%' else current.unit, reason)
            continue
        delta = current.value - previous.value
        relative = delta / previous.value * Decimal('100') if current.unit != '%' and previous.value > 0 else None
        metrics[key] = MetricChange(current.value, previous.value, delta, relative,
                                   'п.п.' if current.unit == '%' else current.unit, None)
    return MetricComparison(previous_start, previous_end, metrics)
