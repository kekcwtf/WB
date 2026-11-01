"""Контроль реестра относительно независимых контрольных сумм отчёта.

Контрольные суммы должны поступать из официального отчёта, а не повторно
вычисляться из того же реестра. Отсутствующая сумма остаётся неизвестной.
"""
from dataclasses import dataclass
from decimal import Decimal
from .finance import Operation, Kind, ZERO, money, reconcile_versions


@dataclass(frozen=True)
class Controls:
    accrued: Decimal | None = None
    deducted: Decimal | None = None  # положительная величина удержаний
    payable: Decimal | None = None


@dataclass(frozen=True)
class Audit:
    accrued: Decimal
    deducted: Decimal
    payable: Decimal
    payouts: Decimal
    differences: dict[str, Decimal | None]
    unknown_ids: tuple[str, ...]
    unallocated_ids: tuple[str, ...]
    duplicate_count: int
    reconciled: bool


def audit(operations: list[Operation], controls: Controls) -> Audit:
    selected = reconcile_versions(operations)
    if len({op.store for op in selected}) > 1:
        raise ValueError('Сверка выполняется отдельно по магазину')
    if any(not op.confirmed for op in selected):
        raise ValueError('Сверка требует подтверждённые операции')
    # Внешние расходы не меняют сумму, подлежащую выплате WB.
    included = [op for op in selected if op.kind not in {Kind.PAYOUT, Kind.EXTERNAL, Kind.UNKNOWN}]
    accrued = sum((op.amount for op in included if op.amount > ZERO), ZERO)
    deducted = -sum((op.amount for op in included if op.amount < ZERO), ZERO)
    payable = accrued - deducted
    payouts = sum((op.amount for op in selected if op.kind == Kind.PAYOUT), ZERO)
    unknown = tuple(op.identifier for op in selected if op.kind == Kind.UNKNOWN)
    differences = {}
    for name, actual in [('accrued', accrued), ('deducted', deducted), ('payable', payable)]:
        control = getattr(controls, name)
        differences[name] = None if control is None else actual - money(control)
    return Audit(accrued, deducted, payable, payouts, differences, unknown,
                 tuple(op.identifier for op in included if op.sku is None),
                 len(operations) - len({(op.store, op.source, op.identifier) for op in operations}),
                 bool(selected) and not unknown and all(value == ZERO for value in differences.values()))
