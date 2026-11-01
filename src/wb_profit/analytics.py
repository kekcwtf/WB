"""Объяснения из реестра без предположений об атрибуции и причинности."""
from dataclasses import dataclass
from decimal import Decimal
from typing import Iterable
from .finance import Operation, Cost, Kind, Result, ZERO, calculate, reconcile_versions

LABELS = {
    Kind.SALE: 'Продажи', Kind.RETURN: 'Возвраты', Kind.COMMISSION: 'Комиссия WB',
    Kind.LOGISTICS: 'Логистика', Kind.REVERSE_LOGISTICS: 'Обратная логистика',
    Kind.STORAGE: 'Хранение', Kind.ACCEPTANCE: 'Приёмка', Kind.ACQUIRING: 'Эквайринг',
    Kind.ADVERTISING: 'Реклама', Kind.PENALTY: 'Штрафы', Kind.DEDUCTION: 'Удержания',
    Kind.COMPENSATION: 'Компенсации', Kind.ADJUSTMENT: 'Корректировки',
    Kind.EXTERNAL: 'Внешние расходы', Kind.PAYOUT: 'Выплаты', Kind.UNKNOWN: 'Неизвестные операции',
}


@dataclass(frozen=True)
class SkuAnalysis:
    sku: str
    result: Result
    sold: int
    returned: int
    categories: dict[str, Decimal]
    margin_before_tax: Decimal | None
    roi_before_tax: Decimal | None
    profit_per_unit_before_tax: Decimal | None
    explanation: str
    recommendations: tuple[str, ...]
    incomplete: bool


def analyse_sku(sku: str, operations: Iterable[Operation], costs: Iterable[Cost]) -> SkuAnalysis:
    # Общие расходы не распределяем автоматически: их влияние видно на уровне магазина.
    selected = [op for op in reconcile_versions(operations) if op.sku == sku]
    return _analyse_selected(sku,selected,costs)


def analyse_group(model: str, operations: Iterable[Operation], costs: Iterable[Cost]) -> SkuAnalysis:
    selected=[op for op in reconcile_versions(operations) if op.sku and op.sku.split(':',1)[0]==model]
    return _analyse_selected(model,selected,costs)


def _analyse_selected(sku,selected,costs):
    result = calculate(selected, costs, None)
    categories = {}
    for op in selected:
        if op.kind not in {Kind.PAYOUT, Kind.UNKNOWN}:
            label = LABELS[op.kind]
            categories[label] = categories.get(label, ZERO) + op.amount
    if result.cost:
        categories['Себестоимость'] = -result.cost
    sold = sum(op.units for op in selected if op.kind == Kind.SALE)
    returned = -sum(op.units for op in selected if op.kind == Kind.RETURN)
    missing_quantity = any(op.kind in {Kind.SALE, Kind.RETURN} and op.amount and not op.units for op in selected)
    incomplete = (not selected or bool(result.missing_costs or result.unknown_operations) or missing_quantity
                  or any(op.kind == Kind.RETURN and op.cost_basis_date is None for op in selected))
    units = sold - returned
    per_unit = result.result_before_tax / units if units > 0 and not incomplete else None
    margin = result.result_before_tax / result.revenue * Decimal('100') if result.revenue > ZERO and not incomplete else None
    roi = result.result_before_tax / result.cost * Decimal('100') if result.cost > ZERO and not incomplete else None
    expenses = sorted(((name, -amount) for name, amount in categories.items() if amount < ZERO),
                      key=lambda item: (-item[1], item[0]))
    recommendations = []
    if incomplete:
        explanation = f'SKU {sku}: данных недостаточно для достоверного вывода о прибыльности.'
    elif sold==0 and returned>0:
        explanation=(f'SKU {sku}: в периоде есть возвраты без новых продаж. Результат до налогов и общих расходов '
                     f'{result.result_before_tax:.2f} ₽. Это не доказывает убыточность товара: исходные продажи могли быть в другом периоде.')
    elif result.result_before_tax < ZERO:
        names = ', '.join(f'{name.lower()} ({amount:.2f} ₽)' for name, amount in expenses[:3])
        explanation = (f'SKU {sku}: результат до налога и общих расходов магазина составляет '
                       f'{result.result_before_tax:.2f} ₽. Наибольшие суммы затрат: {names}. '
                       'Это структура расходов, а не доказательство причины изменения продаж.')
        if categories.get('Реклама', ZERO) < ZERO:
            recommendations.append('Проверить расходы рекламных кампаний; связь рекламы с продажами требует отдельной атрибуции.')
        if categories.get('Логистика', ZERO) + categories.get('Обратная логистика', ZERO) < ZERO:
            recommendations.append('Проверить фактическую логистику и обратную логистику.')
        recommendations.append('Оценить изменение цены с учётом комиссии FBW, налогов и спроса.')
    else:
        explanation = (f'SKU {sku}: результат до налога и общих расходов магазина составляет '
                       f'{result.result_before_tax:.2f} ₽. Полная чистая прибыль по SKU ещё не определена.')
    return SkuAnalysis(sku, result, sold, returned, categories, margin, roi, per_unit,
                       explanation, tuple(recommendations), incomplete)


@dataclass(frozen=True)
class StockEstimate:
    average_daily_sales: Decimal
    estimated_days: Decimal | None
    no_sales: bool


def stock_estimate(stock: int, sold: int, observed_days: int) -> StockEstimate:
    """Оценка по фактическому наблюдаемому периоду, не гарантия наличия спроса."""
    if any(isinstance(value, bool) or not isinstance(value, int) for value in (stock, sold, observed_days)):
        raise TypeError('Остатки, продажи и дни должны быть целыми числами')
    if stock < 0 or sold < 0 or observed_days <= 0:
        raise ValueError('Нужен положительный период и неотрицательные продажи и остатки')
    daily = Decimal(sold) / Decimal(observed_days)
    return StockEstimate(daily, Decimal(stock) / daily if daily else None, sold == 0)
