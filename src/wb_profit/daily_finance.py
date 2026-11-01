"""Период → дни: точные налоги с сохранением контрольной суммы выбранного периода."""
from collections import defaultdict
from dataclasses import dataclass
from datetime import date, timedelta
from decimal import Decimal
from .finance import Kind, ZERO, calculate, allocate, reconcile_versions
from .taxes import calculate_with_history, rule_at


@dataclass(frozen=True)
class DailyResult:
    day: date
    revenue: Decimal | None
    expenses_contribution: Decimal | None
    cost: Decimal | None
    before_tax: Decimal | None
    usn: Decimal | None
    vat: Decimal | None
    net: Decimal | None
    issues: tuple[str, ...]


@dataclass(frozen=True)
class DailyReport:
    rows: tuple[DailyResult, ...]
    period_net: Decimal | None
    daily_sum: Decimal | None
    difference: Decimal | None
    reasons: tuple[str, ...]


def daily_finances(operations, costs, rules=(), *, start=None, end=None, coverage_confirmed=False):
    operations = reconcile_versions(operations)
    if len({op.store for op in operations}) > 1:
        raise ValueError('Дневной расчёт должен относиться к одному магазину')
    if (start is None) != (end is None) or start is not None and start > end:
        raise ValueError('Задайте корректные начало и конец периода')
    if start is not None:
        operations = [op for op in operations if start <= op.occurred <= end]
    elif operations:
        start, end = min(op.occurred for op in operations), max(op.occurred for op in operations)
    costs, rules = list(costs), list(rules)
    facts = [op for op in operations if op.confirmed]
    preliminary = [op for op in operations if not op.confirmed]
    result, parts = calculate_with_history(facts, costs, rules)
    has_wb = any(op.source != 'external_expenses' and op.kind not in {Kind.PAYOUT, Kind.UNKNOWN} for op in facts)
    complete = bool(coverage_confirmed and has_wb and not preliminary and result.net_profit is not None)
    grouped = defaultdict(list)
    for op in operations:
        grouped[op.occurred].append(op)
    reasons = []
    if not complete:
        if not coverage_confirmed:reasons.append('Период не полностью загружен, не закрыт или не сверены все расходы')
        if not has_wb:reasons.append('Нет подтверждённых финансовых операций WB')
        if preliminary:reasons.append('Есть предварительные операции без подтверждённой замены')
        if result.missing_costs:reasons.append(f'Не назначена себестоимость: {len(result.missing_costs)} SKU')
        if result.unknown_operations:reasons.append(f'Неизвестных операций: {len(result.unknown_operations)}')
        reasons.extend(result.tax_limitations)
    taxes = {name: defaultdict(lambda: ZERO) for name in ('usn', 'vat')}
    if complete:
        weights = defaultdict(lambda: defaultdict(lambda: ZERO))
        resolved = {}
        for op in facts:
            if op.kind != Kind.SALE:continue
            if op.occurred not in resolved:resolved[op.occurred] = rule_at(rules, op.occurred)
            rule = resolved[op.occurred]
            key = (rule.effective, op.occurred.year, (op.occurred.month-1)//3+1)
            weights[key][op.occurred.isoformat()] += op.amount
        for part in parts:
            base = weights[(part.effective, part.year, part.quarter)]
            if sum(base.values(), ZERO) <= ZERO:
                if part.usn or part.vat:raise ValueError('Нет положительной базы распределения налогов по дням')
                continue
            for name in ('usn', 'vat'):
                for day, amount in allocate(getattr(part, name), base).items():
                    taxes[name][date.fromisoformat(day)] += amount
    # Не разворачиваем миллионы пустых дат при ошибочно выбранном огромном периоде.
    if start is not None and (end-start).days <= 3660:
        days = [start+timedelta(days=i) for i in range((end-start).days+1)]
    else:
        days = sorted(grouped)
        if start is not None:reasons.append('Длинный период: показаны только дни с операциями; нулевые дни не развёрнуты')
    cost_index = defaultdict(list)
    for cost in costs:cost_index[cost.sku].append(cost)
    rows = []
    for day in days:
        selected = grouped[day]
        confirmed = [op for op in selected if op.confirmed]
        financial = [op for op in confirmed if op.kind != Kind.PAYOUT]
        cost_keys = {op.sku for op in confirmed if op.kind in {Kind.SALE, Kind.RETURN}} | {'*'}
        day_costs = [cost for key in cost_keys for cost in cost_index.get(key, ())]
        current = calculate(confirmed, day_costs, None)
        issues = []
        if any(not op.confirmed for op in selected):issues.append('Есть предварительные операции')
        if current.missing_costs:issues.append('Не назначена себестоимость')
        if current.unknown_operations:issues.append('Неизвестные операции')
        if not financial and not complete:
            issues.append('Только выплаты' if confirmed else 'Нет подтверждённых операций дня')
        if any(op.kind == Kind.RETURN and op.cost_basis_date is None for op in confirmed):
            issues.append('Возврат без однозначной исходной продажи')
        before = current.result_before_tax if not issues else None
        usn = taxes['usn'][day] if complete else None
        vat = taxes['vat'][day] if complete else None
        net = before-usn-vat if complete and before is not None else None
        traded = any(op.kind in {Kind.SALE, Kind.RETURN} for op in financial)
        expenses = any(op.kind not in {Kind.SALE, Kind.RETURN, Kind.UNKNOWN} for op in financial)
        rows.append(DailyResult(day, current.revenue if traded or complete else None,
                                current.wb_and_external if expenses or complete else None,
                                current.cost if (traded or complete) and not current.missing_costs else None,
                                before, usn, vat, net, tuple(issues)))
    period_net = result.net_profit if complete else None
    daily_sum = sum((row.net for row in rows), ZERO) if complete else None
    difference = daily_sum-period_net if complete else None
    if difference:
        raise ValueError('Сумма прибыли по дням не совпадает с выбранным периодом')
    return DailyReport(tuple(rows), period_net, daily_sum, difference, tuple(dict.fromkeys(reasons)))
