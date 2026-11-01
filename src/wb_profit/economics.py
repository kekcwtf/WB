"""Условный прогноз по истории, не действующий тариф WB и не обещание спроса."""
from dataclasses import dataclass
from decimal import Decimal, ROUND_CEILING
from typing import Iterable
from .finance import Cost, Kind, Operation, TaxSettings, ZERO, money, reconcile_versions, calculate


@dataclass(frozen=True)
class PriceForecast:
    price: Decimal
    commission: Decimal
    other_expenses: Decimal
    purchase_cost: Decimal
    usn: Decimal
    vat: Decimal
    estimated_profit: Decimal
    margin_percent: Decimal
    roi_percent: Decimal | None


@dataclass(frozen=True)
class HistoricalModel:
    sku: str
    average_price: Decimal
    commission_fraction: Decimal
    expenses_per_unit: Decimal
    purchase_cost_per_unit: Decimal
    taxes: TaxSettings
    commission_source: str = 'историческая доля комиссии'
    limitations: tuple[str, ...] = (
        'Сценарий по фактической структуре расходов выбранного периода; действующие тарифы FBW не загружены.',
        'Налоги расчётные. Спрос, выкуп и рекламная эффективность при изменении цены не прогнозируются.',
        'Общие расходы магазина не распределены. Будущая прибыль не гарантируется.',
    )
    expense_components: tuple[tuple[Kind, Decimal], ...] = ()

    def break_even_price(self) -> Decimal | None:
        """Консервативная оценка с запасом на округление двух расчётных налогов.

        Это граница линейного сценария, не прогноз спроса и не гарантия прибыли.
        """
        vat=self.taxes.vat_percent/(Decimal('100')+self.taxes.vat_percent)
        usn=(Decimal('1')-vat)*self.taxes.usn_percent/Decimal('100')
        slope=Decimal('1')-self.commission_fraction-vat-usn
        if slope<=ZERO:return None
        try:
            price=((self.purchase_cost_per_unit+self.expenses_per_unit+Decimal('0.01'))/slope).quantize(Decimal('0.01'),rounding=ROUND_CEILING)
            return max(Decimal('0.01'),price)
        except ArithmeticError:return None

    def forecast(self, proposed_price: Decimal) -> PriceForecast:
        price = money(proposed_price)
        if price <= ZERO:
            raise ValueError('Предполагаемая цена должна быть положительной')
        commission = price * self.commission_fraction
        usn, vat = self.taxes.estimate(price)
        profit = price - commission - self.expenses_per_unit - self.purchase_cost_per_unit - usn - vat
        return PriceForecast(price, commission, self.expenses_per_unit, self.purchase_cost_per_unit,
                             usn, vat, profit, profit / price * Decimal('100'),
                             profit / self.purchase_cost_per_unit * Decimal('100') if self.purchase_cost_per_unit > ZERO else None)


def historical_model(sku: str, operations: Iterable[Operation], costs: Iterable[Cost], taxes: TaxSettings | None,
                     *, coverage_confirmed: bool = False, fbw_commission_percent: Decimal | None = None) -> HistoricalModel:
    """coverage_confirmed выставляет адаптер только для сверенного периода.

    Отсутствие строк отдельного расхода не означает нулевой расход без покрытия
    источниками. Эта отметка не является настройкой ручного ввода пользователя.
    """
    if not coverage_confirmed:
        raise ValueError('Полнота источников за период не подтверждена; прогноз недоступен')
    if taxes is None or not taxes.confirmed_by_user:
        raise ValueError('Подтвердите налоговую модель')
    if not taxes.vat_included:
        raise ValueError('Сценарий требует цену с включённым НДС; модель цены без НДС ещё не поддержана')
    selected = [op for op in reconcile_versions(operations) if op.sku == sku]
    if len({op.store for op in selected}) > 1:
        raise ValueError('Сценарий должен относиться только к одному магазину')
    if not selected or any(not op.confirmed for op in selected):
        raise ValueError('Нет подтверждённой истории SKU')
    if any(op.kind in {Kind.RETURN, Kind.UNKNOWN, Kind.COMPENSATION, Kind.ADJUSTMENT} for op in selected):
        raise ValueError('Есть возвраты, неизвестные операции или корректировки; модель требует отдельной сверки')
    result = calculate(selected, costs, taxes)
    if result.missing_costs:
        raise ValueError('Не хватает себестоимости или количества продаж')
    sold = sum(op.units for op in selected if op.kind == Kind.SALE)
    if sold <= 0 or result.revenue <= ZERO:
        raise ValueError('Недостаточно фактических продаж')
    commission = -sum((op.amount for op in selected if op.kind == Kind.COMMISSION), ZERO)
    other = -sum((op.amount for op in selected if op.kind not in {Kind.SALE, Kind.COMMISSION, Kind.PAYOUT}), ZERO)
    if commission < ZERO or commission >= result.revenue or other < ZERO:
        raise ValueError('Структура расходов не подходит для линейного сценария')
    units = Decimal(sold)
    model=HistoricalModel(sku, result.revenue / units, commission / result.revenue,
                           other / units, result.cost / units, taxes,
                           expense_components=tuple((kind, -sum((op.amount for op in selected if op.kind == kind), ZERO) / units)
                               for kind in Kind if kind not in {Kind.SALE, Kind.RETURN, Kind.COMMISSION,
                                   Kind.PAYOUT, Kind.UNKNOWN, Kind.COMPENSATION, Kind.ADJUSTMENT}))
    if fbw_commission_percent is not None:
        from dataclasses import replace
        rate=money(fbw_commission_percent)
        if not ZERO<=rate<=Decimal('100'):
            raise ValueError('Некорректная комиссия FBW')
        model=replace(model,commission_fraction=rate/Decimal('100'),commission_source='кеш тарифа FBW paidStorageKgvp',
                      limitations=('Комиссия FBW из сохранённого ответа API тарифов; остальные расходы по истории. Тарифы могут измениться.',*model.limitations[1:]))
    return model
