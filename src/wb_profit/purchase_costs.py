"""Одинаковый поиск датированной цены для финансового ядра и диагностики."""
from bisect import bisect_right


class CostLookup:
    def __init__(self, costs):
        grouped = {}
        for cost in costs:
            dated = grouped.setdefault(cost.sku, {})
            if cost.effective in dated and dated[cost.effective].per_unit != cost.per_unit:
                raise ValueError('Две разные себестоимости SKU на одну дату')
            dated[cost.effective] = cost
        self.values = {sku: [dated[day] for day in sorted(dated)] for sku, dated in grouped.items()}
        self.dates = {sku: [cost.effective for cost in rows] for sku, rows in self.values.items()}

    def resolve(self, sku, basis_date):
        for key in (sku, '*'):
            index = bisect_right(self.dates.get(key, []), basis_date) - 1
            if index >= 0:
                return self.values[key][index]
        return None
