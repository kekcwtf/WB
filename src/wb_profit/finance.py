"""Знаковый реестр. Выплаты не являются доходами; оценки не смешиваются с фактом."""
from dataclasses import dataclass
from datetime import date
from decimal import Decimal, ROUND_HALF_UP
from enum import StrEnum
from typing import Mapping, Iterable
from .purchase_costs import CostLookup
from fractions import Fraction

ZERO = Decimal('0')
CENT = Decimal('0.01')


def money(value: str | Decimal | int) -> Decimal:
    if isinstance(value, (float, bool)):
        raise TypeError('Денежное значение должно быть Decimal, строкой или целым')
    result = Decimal(value)
    if not result.is_finite():
        raise ValueError('Сумма должна быть конечной')
    return result


class Kind(StrEnum):
    SALE = 'sale'
    RETURN = 'return'
    COMMISSION = 'commission'
    LOGISTICS = 'logistics'
    REVERSE_LOGISTICS = 'reverse_logistics'
    STORAGE = 'storage'
    ACCEPTANCE = 'acceptance'
    ACQUIRING = 'acquiring'
    ADVERTISING = 'advertising'
    PENALTY = 'penalty'
    DEDUCTION = 'deduction'
    COMPENSATION = 'compensation'
    ADJUSTMENT = 'adjustment'
    EXTERNAL = 'external'
    PAYOUT = 'payout'
    UNKNOWN = 'unknown'


@dataclass(frozen=True)
class Operation:
    store: str
    source: str
    identifier: str
    occurred: date
    kind: Kind
    amount: Decimal
    sku: str | None = None
    units: int = 0  # продажа +, возврат −
    confirmed: bool = True
    economic_key: str | None = None
    original_sale_id: str | None = None
    cost_basis_date: date | None = None

    def __post_init__(self):
        object.__setattr__(self, 'amount', money(self.amount))
        if isinstance(self.units,bool) or not isinstance(self.units,int):
            raise TypeError('Количество должно быть целым числом')
        if (self.original_sale_id is None) != (self.cost_basis_date is None):
            raise ValueError('Связь возврата требует ID и дату исходной продажи')
        if self.cost_basis_date is not None:
            if (not isinstance(self.cost_basis_date,date) or not isinstance(self.original_sale_id,str)
                    or not self.original_sale_id.strip() or self.kind != Kind.RETURN or self.cost_basis_date > self.occurred):
                raise ValueError('Неверная связь возврата с исходной продажей')
        if not self.store or not self.source or not self.identifier:
            raise ValueError('Необходимы магазин, источник и ID')
        if self.kind == Kind.SALE and (self.amount < ZERO or self.units < 0):
            raise ValueError('Продажа должна иметь положительный знак')
        if self.kind == Kind.RETURN and (self.amount > ZERO or self.units > 0):
            raise ValueError('Возврат должен иметь отрицательный знак')
        if self.kind not in {Kind.SALE, Kind.RETURN} and self.units:
            raise ValueError('Количество допустимо только для продаж и возвратов')


@dataclass(frozen=True)
class Cost:
    sku: str
    effective: date
    per_unit: Decimal

    def __post_init__(self):
        object.__setattr__(self, 'per_unit', money(self.per_unit))
        if self.per_unit < ZERO:
            raise ValueError('Себестоимость не может быть отрицательной')


@dataclass(frozen=True)
class TaxSettings:
    usn_percent: Decimal
    vat_percent: Decimal
    vat_included: bool = True
    confirmed_by_user: bool = False

    def __post_init__(self):
        for name in ('usn_percent', 'vat_percent'):
            value = money(getattr(self, name))
            if not ZERO <= value <= Decimal('100'):
                raise ValueError('Ставка должна быть от 0 до 100')
            object.__setattr__(self, name, value)

    def estimate(self, revenue: Decimal) -> tuple[Decimal, Decimal]:
        """Расчётный налог с реализации; не подтверждённое обязательство.

        Компенсации, взносы, вычеты и налоговые убытки требуют отдельной проверки.
        Отрицательная база периода не создаёт автоматического возмещения.
        """
        base = max(ZERO, money(revenue))
        vat = base * self.vat_percent / (
            Decimal('100') + self.vat_percent if self.vat_included else Decimal('100'))
        usn_base = base - vat if self.vat_included else base
        return (usn_base * self.usn_percent / Decimal('100')).quantize(CENT, ROUND_HALF_UP), vat.quantize(CENT, ROUND_HALF_UP)


@dataclass(frozen=True)
class Result:
    revenue: Decimal
    wb_and_external: Decimal
    cost: Decimal
    usn: Decimal | None
    vat: Decimal | None
    result_before_tax: Decimal
    net_profit: Decimal | None
    missing_costs: tuple[str, ...]
    unknown_operations: tuple[str, ...]
    unallocated: Decimal
    tax_limitations: tuple[str, ...]


def reconcile_versions(operations: Iterable[Operation]) -> list[Operation]:
    """Один экономический ключ должен присваиваться адаптером только при доказанной связи.

    Неоднозначные совпадения нельзя молча суммировать или удалять.
    """
    unique = {}
    for op in operations:
        key = (op.store, op.source, op.identifier)
        if key in unique and unique[key] != op:
            raise ValueError('Конфликт версий: сначала сохраните актуальную версию в реестр')
        unique[key] = op
    grouped = {}
    unmatched = []
    for op in unique.values():
        if op.economic_key is None:
            unmatched.append(op)
        else:
            grouped.setdefault((op.store, op.economic_key), []).append(op)
    for group in grouped.values():
        facts = [op for op in group if op.confirmed]
        candidates = facts or group
        if len(candidates) > 1:
            raise ValueError('Неоднозначное соответствие экономической операции')
        unmatched.extend(candidates)
    return unmatched


def calculate(operations: Iterable[Operation], costs: Iterable[Cost], taxes: TaxSettings | None) -> Result:
    ops = reconcile_versions(operations)
    if len({op.store for op in ops}) > 1:
        raise ValueError('Расчёт должен выполняться отдельно для каждого магазина')
    if any(not op.confirmed for op in ops):
        raise ValueError('Предварительные операции требуют отдельного расчёта')
    revenue = sum((op.amount for op in ops if op.kind in {Kind.SALE, Kind.RETURN}), ZERO)
    unknown = tuple(op.identifier for op in ops if op.kind == Kind.UNKNOWN)
    included = [op for op in ops if op.kind not in {Kind.PAYOUT, Kind.UNKNOWN}]
    expenses = sum((op.amount for op in included if op.kind not in {Kind.SALE, Kind.RETURN}), ZERO)
    unallocated = sum((op.amount for op in included if op.sku is None), ZERO)
    lookup = CostLookup(costs)
    cost = ZERO
    missing = set()
    for op in included:
        if op.kind not in {Kind.SALE, Kind.RETURN}:
            continue
        if not op.units:
            if op.amount:
                missing.add(op.sku or '(без SKU)')
            continue
        if op.kind==Kind.RETURN and op.cost_basis_date is None:
            missing.add(op.sku or '(без SKU)')
            continue
        basis_date=op.cost_basis_date or op.occurred
        purchase = lookup.resolve(op.sku,basis_date)
        if purchase is None:
            missing.add(op.sku or '(без SKU)')
            continue
        cost += purchase.per_unit * op.units
    before = revenue + expenses - cost
    usn, vat = taxes.estimate(revenue) if taxes else (None, None)
    limitations = ['Налоги расчётные; взносы, вычеты и налоговая классификация прочих доходов не подтверждены.']
    if any(op.kind==Kind.RETURN and op.cost_basis_date is None for op in ops):
        limitations.append('Себестоимость возвратов требует связи с исходной продажей; чистая прибыль не подтверждена.')
    if any(op.kind==Kind.RETURN for op in ops):
        limitations.append('Налоговая периодизация возвратов не подтверждена; расчёт не устанавливает право на возврат налога.')
    if any(op.kind in {Kind.COMPENSATION, Kind.ADJUSTMENT} for op in ops):
        limitations.append('Требуется определить налоговую базу компенсаций и корректировок.')
    eligible = bool(ops) and taxes is not None and taxes.confirmed_by_user and not missing and not unknown and len(limitations) == 1
    return Result(revenue, expenses, cost, usn, vat, before,
                  before - usn - vat if eligible else None,
                  tuple(sorted(missing)), unknown, unallocated, tuple(limitations))


def allocate(total: Decimal, weights: Mapping[str, Decimal]) -> dict[str, Decimal]:
    """Распределение методом наибольших остатков с сохранением суммы и знака."""
    total = money(total)
    if total != total.quantize(CENT):
        raise ValueError('Распределение требует сумму в копейках')
    checked = {key: money(value) for key, value in sorted(weights.items())}
    if not checked or any(w < ZERO for w in checked.values()) or sum(checked.values()) == ZERO:
        raise ValueError('Нужны неотрицательные веса с положительной суммой')
    # Распределяем целые копейки методом наибольших остатков.
    # Точные отношения весов не округляются и не используют float.
    weights_exact = {key: Fraction(value) for key, value in checked.items()}
    denominator = sum(weights_exact.values())
    cents = abs(int(total / CENT))
    shares = {key: cents * weight / denominator for key, weight in weights_exact.items()}
    units = {key: share.numerator // share.denominator for key, share in shares.items()}
    remaining = cents - sum(units.values())
    order = sorted((key for key, weight in weights_exact.items() if weight),
                   key=lambda key: (-(shares[key] - units[key]), -weights_exact[key], key))
    for key in order[:remaining]:units[key] += 1
    sign = -1 if total < ZERO else 1
    return {key: Decimal(value * sign) * CENT for key, value in units.items()}
