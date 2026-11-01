"""Показатели магазина: определения, полнота, без повторного вычитания НДС."""
from collections import defaultdict
from dataclasses import dataclass
from decimal import Decimal
from .finance import Kind, ZERO, reconcile_versions
from .taxes import calculate_with_history

WB_EXPENSES = (Kind.COMMISSION, Kind.LOGISTICS, Kind.REVERSE_LOGISTICS, Kind.STORAGE,
               Kind.ACCEPTANCE, Kind.ACQUIRING, Kind.ADVERTISING, Kind.PENALTY, Kind.DEDUCTION)


@dataclass(frozen=True)
class Metric:
    value: Decimal | None
    status: str
    note: str
    unit: str = '₽'


@dataclass(frozen=True)
class Overview:
    metrics: dict[str, Metric]
    expenses: dict[Kind, Metric]
    reasons: tuple[str, ...]
    arithmetic_difference: Decimal | None


def financial_overview(operations, costs, rules, *, financial_complete=False, full_complete=False):
    operations = reconcile_versions(operations)
    if len({op.store for op in operations}) > 1:
        raise ValueError('Обзор должен относиться к одному магазину')
    preliminary = [op for op in operations if not op.confirmed]
    facts = [op for op in operations if op.confirmed]
    result, _parts = calculate_with_history(facts, costs, rules)
    totals = defaultdict(lambda: ZERO)
    present = set()
    for op in facts:
        totals[op.kind] += op.amount
        present.add(op.kind)
    has_wb = any(op.source != 'external_expenses' and op.kind not in {Kind.PAYOUT, Kind.UNKNOWN} for op in facts)
    classified = not result.unknown_operations and not preliminary
    financial_complete = bool(financial_complete and has_wb and classified)
    full_complete = bool(full_complete and financial_complete)
    known_cost = not result.missing_costs
    reasons = list(result.tax_limitations)
    if preliminary:
        reasons.append(f'Предварительных операций: {len(preliminary)}; они не прибавлены к фактам')
    if result.unknown_operations:
        reasons.append(f'Не классифицировано операций: {len(result.unknown_operations)}')
    if result.missing_costs:
        reasons.append(f'Не назначена себестоимость: {len(result.missing_costs)} SKU; известная часть не является полной суммой')
    if not financial_complete:
        reasons.append('Финансовый период не полностью загружен, не закрыт или не сверён')
    if not full_complete:
        reasons.append('Полная прибыль требует сверки всех расходов, в том числе рекламы')

    def fact_value(kind, *, expense=False, complete=None):
        complete = financial_complete if complete is None else complete
        value = totals[kind] * (-1 if expense else 1) if kind in present or complete else None
        return Metric(value, 'сверено' if complete else 'загружено' if value is not None else 'нет данных',
                      'По финансовым операциям; сторно уменьшает расход' if expense else 'По финансовым операциям выбранного периода')

    expenses = {kind: fact_value(kind, expense=True, complete=full_complete if kind == Kind.ADVERTISING else financial_complete)
                for kind in WB_EXPENSES}
    expenses[Kind.EXTERNAL] = fact_value(Kind.EXTERNAL, expense=True)
    revenue = result.revenue if Kind.SALE in present or Kind.RETURN in present or financial_complete else None
    net = result.net_profit if full_complete else None
    before = result.result_before_tax if has_wb and known_cost and classified else None
    gross = result.revenue - result.vat - result.cost if revenue is not None and known_cost and classified and result.vat is not None else None
    operating = result.result_before_tax - result.vat if net is not None else None
    wb_cost = -sum((totals[kind] for kind in WB_EXPENSES), ZERO)
    all_cost = result.cost + wb_cost - totals[Kind.EXTERNAL]
    ad_cost = -totals[Kind.ADVERTISING]
    calculated = 'расчёт; период сверён' if full_complete else 'расчёт по загруженным данным'
    loaded = 'сверено' if financial_complete else 'загружено'
    metrics = {
        'sales': fact_value(Kind.SALE),
        'returns': fact_value(Kind.RETURN, expense=True),
        'revenue': Metric(revenue, loaded, 'Продажи минус возвраты; сумма WB включает НДС, если он применим'),
        'wb_cost': Metric(wb_cost if any(k in present for k in WB_EXPENSES) or full_complete else None,
                          'сверено' if full_complete else 'загружено', 'Сумма отдельных загруженных расходов WB; корректировки и компенсации показаны отдельно'),
        'cost': Metric(result.cost if known_cost and any(op.kind in {Kind.SALE, Kind.RETURN} for op in facts) else ZERO if known_cost and financial_complete else None,
                       loaded if known_cost else 'неполная себестоимость', 'Себестоимость по датам действия; для возврата — по связанной исходной продаже'),
        'gross': Metric(gross, calculated, 'Выручка WB − расчётный НДС − себестоимость'),
        'operating': Metric(operating, calculated, 'Финансовый результат после расходов и НДС, до УСН; полный расчёт требует сверки рекламы'),
        'before_tax': Metric(before, loaded, 'По загруженным финансовым проводкам до НДС и УСН; отсутствие несопоставленной рекламы не означает нулевой расход'),
        'usn': Metric(result.usn if revenue is not None else None, calculated, 'Расчётный УСН по датированной истории ставок; не подтверждённое обязательство'),
        'vat': Metric(result.vat if revenue is not None else None, calculated, 'Расчётный НДС; уже исключён из валовой и операционной прибыли'),
        'net': Metric(net, calculated, 'Операционный результат − расчётный УСН; НДС второй раз не вычитается'),
        'margin': Metric(net / result.revenue * 100 if net is not None and result.revenue > ZERO else None,
                         calculated, 'Чистая прибыль / выручка WB после возвратов, включая НДС × 100; та же база, что в таблице SKU', '%'),
        'roi': Metric(net / result.cost * 100 if net is not None and result.cost > ZERO else None,
                      calculated, 'ROI закупки = чистая прибыль / себестоимость реализованных товаров × 100; не годовая доходность', '%'),
        'profitability': Metric(net / all_cost * 100 if net is not None and all_cost > ZERO else None,
                                calculated, 'Чистая прибыль / (себестоимость + расходы WB + внешние расходы) × 100; знаменатель без расчётных налогов продавца', '%'),
        'drr': Metric(ad_cost / result.revenue * 100 if full_complete and ad_cost >= ZERO and result.revenue > ZERO else None,
                      calculated, 'Сверенная реклама из финансового реестра / выручка WB после возвратов × 100; расходы другого отчёта повторно не прибавляются', '%'),
        'compensation': fact_value(Kind.COMPENSATION),
        'adjustment': fact_value(Kind.ADJUSTMENT),
        'payout': Metric(totals[Kind.PAYOUT] if Kind.PAYOUT in present else None,'записи реестра',
                         'Не выручка и не прибыль. Отсутствие записи не подтверждает отсутствие банковского перевода'),
    }
    difference = None
    if before is not None:
        recomposed = (totals[Kind.SALE] + totals[Kind.RETURN] - wb_cost + totals[Kind.EXTERNAL]
                      + totals[Kind.COMPENSATION] + totals[Kind.ADJUSTMENT] - result.cost)
        difference = recomposed - before
        if difference:
            raise ValueError('Статьи обзора не сходятся с финансовым результатом')
    return Overview(metrics, expenses, tuple(dict.fromkeys(reasons)), difference)
