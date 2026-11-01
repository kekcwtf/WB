"""Диагностика только подтверждённых продаж/возвратов, без вымышленных количеств."""
from dataclasses import dataclass
from datetime import date
from .finance import Kind, reconcile_versions
from .purchase_costs import CostLookup


@dataclass(frozen=True)
class CostRequirement:
    sku: str | None
    checked_units: int
    missing_price_units: int
    earliest_missing_date: date | None
    unlinked_returns: int
    missing_quantity: int

    @property
    def needs_attention(self):
        return bool(self.missing_price_units or self.unlinked_returns or self.missing_quantity)


@dataclass(frozen=True)
class CostCoverage:
    rows: tuple[CostRequirement, ...]
    preliminary_records: int


def cost_requirements(operations, costs):
    operations = reconcile_versions(operations)
    if len({op.store for op in operations}) > 1:
        raise ValueError('Проверка себестоимости выполняется отдельно по магазину')
    lookup = CostLookup(costs)
    grouped = {}
    preliminary = 0
    for operation in operations:
        if operation.kind not in {Kind.SALE, Kind.RETURN}:
            continue
        if not operation.confirmed:
            preliminary += 1
            continue
        row = grouped.setdefault(operation.sku, {'checked': 0, 'missing': 0, 'earliest': None, 'returns': 0, 'quantity': 0})
        if not operation.units:
            if operation.amount:
                row['quantity'] += 1
            continue
        if operation.kind == Kind.RETURN and operation.cost_basis_date is None:
            row['returns'] += 1
            continue
        basis = operation.cost_basis_date or operation.occurred
        units = abs(operation.units)
        row['checked'] += units
        if lookup.resolve(operation.sku, basis) is None:
            row['missing'] += units
            row['earliest'] = min(row['earliest'], basis) if row['earliest'] is not None else basis
    rows = [CostRequirement(sku, value['checked'], value['missing'], value['earliest'],
                            value['returns'], value['quantity']) for sku, value in grouped.items()]
    return CostCoverage(tuple(sorted(rows, key=lambda row: (row.sku is None, row.sku or ''))), preliminary)
