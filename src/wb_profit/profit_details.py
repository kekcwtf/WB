"""Расшифровка результата товара, согласованная с распределением магазина."""
from dataclasses import dataclass
from decimal import Decimal
from .analytics import SkuAnalysis, analyse_group, analyse_sku
from .finance import ZERO, reconcile_versions
from .profit_distribution import distribute_profit


@dataclass(frozen=True)
class ProfitDetails:
    analysis: SkuAnalysis
    categories: dict[str, Decimal]
    net_profit: Decimal | None
    per_unit: Decimal | None
    margin: Decimal | None
    roi: Decimal | None
    difference: Decimal | None
    explanation: str
    recommendations: tuple[str, ...]
    allocation_note: str


def product_profit_details(sku, operations, costs, rules, *, grouped=False,
                           coverage_confirmed=False, method='unallocated'):
    operations = reconcile_versions(operations)
    costs = list(costs)
    analysis = (analyse_group if grouped else analyse_sku)(sku, operations, costs)
    categories = dict(analysis.categories)
    distribution = distribute_profit(operations, costs, rules,
                                     coverage_confirmed=coverage_confirmed, method=method)
    keys = {op.sku for op in operations if op.sku is not None and
            (op.sku.split(':', 1)[0] == sku if grouped else op.sku == sku)}
    if distribution is None or analysis.incomplete or not keys:
        return ProfitDetails(analysis, categories, None, None, None, None, None,
                             analysis.explanation, analysis.recommendations,
                             'Полный расчёт по SKU недоступен: требуется полная сверка периода, '
                             'расходов и налогов магазина и известная себестоимость. '
                             'Налоги и общие расходы не подставляются нулями.')
    net = sum((distribution.net_by_sku[key] for key in keys), ZERO)
    usn = sum((distribution.usn_by_sku.get(key, ZERO) for key in keys), ZERO)
    vat = sum((distribution.vat_by_sku.get(key, ZERO) for key in keys), ZERO)
    common = sum((distribution.common_by_sku.get(key, ZERO) for key in keys), ZERO)
    categories['Расчётный УСН (распределённый)'] = -usn
    categories['Расчётный НДС (распределённый)'] = -vat
    if common:
        categories['Общие расходы (распределённый вклад)'] = common
    difference = sum(categories.values(), ZERO) - net
    if difference:
        raise ValueError('Расшифровка товара не совпадает с его расчётной чистой прибылью')
    units = analysis.sold - analysis.returned
    per_unit = net / units if units > 0 else None
    margin = net / analysis.result.revenue * 100 if analysis.result.revenue > ZERO else None
    roi = net / analysis.result.cost * 100 if analysis.result.cost > ZERO else None
    subject = 'Модель' if grouped else 'SKU'
    explanation = f'{subject} {sku}: расчётная чистая прибыль {net:.2f} ₽.'
    recommendations = []
    if net < ZERO:
        expenses = sorted(((name, -value) for name, value in categories.items() if value < ZERO),
                          key=lambda item: (-item[1], item[0]))
        explanation += ' Наибольшие суммы затрат: ' + ', '.join(
            f'{name.lower()} ({amount:.2f} ₽)' for name, amount in expenses[:3]) + '.'
        if analysis.result.result_before_tax >= ZERO:
            explanation += (' До налогов и распределения общих расходов результат не был отрицательным; '
                            'убыток возникает после их учёта.')
        if categories.get('Реклама', ZERO) < ZERO:
            recommendations.append('Проверить расходы рекламных кампаний; прибыль не доказывает рекламную атрибуцию.')
        if categories.get('Логистика', ZERO) + categories.get('Обратная логистика', ZERO) < ZERO:
            recommendations.append('Проверить фактическую логистику и обратную логистику.')
        if common < ZERO:
            recommendations.append('Проверить влияние общих расходов магазина и выбранный метод их распределения.')
        recommendations.append('Оценить изменение цены с учётом комиссии FBW, налогов и спроса.')
        explanation += ' Это структура результата, а не доказательство причин изменения спроса.'
    allocation_note = ('Общие расходы распределены по положительной выручке SKU.' if method == 'revenue'
                       else 'Общие расходы остаются у магазина и не входят в результат этого товара.')
    allocation_note += (' УСН и НДС — расчётные оценки, распределённые по выручке внутри версии ставок '
                        'и квартала. Это не индивидуальное подтверждённое налоговое обязательство SKU.')
    if distribution.warnings:
        allocation_note += ' ' + ' '.join(distribution.warnings)
    return ProfitDetails(analysis, categories, net, per_unit, margin, roi, difference,
                         explanation, tuple(recommendations), allocation_note)
