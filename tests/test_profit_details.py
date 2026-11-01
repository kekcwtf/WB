import unittest
from datetime import date
from decimal import Decimal as D
from wb_profit.finance import Operation, Kind, Cost, TaxSettings
from wb_profit.taxes import TaxRule
from wb_profit.profit_details import product_profit_details

DAY = date(2026, 1, 1)


def op(identifier, kind, amount, sku='42:s', units=0):
    return Operation('shop', 'report', identifier, DAY, kind, D(amount), sku, units)


class ProfitDetailsTests(unittest.TestCase):
    def setUp(self):
        self.rules = [TaxRule(DAY, TaxSettings(D('4'), D('5'), confirmed_by_user=True))]
        self.ops = [op('sale', Kind.SALE, '1050', units=1), op('logistics', Kind.LOGISTICS, '-50'),
                    op('storage', Kind.STORAGE, '-100', None)]
        self.costs = [Cost('42:s', DAY, D('900'))]

    def details(self, **kwargs):
        return product_profit_details('42:s', self.ops, self.costs, self.rules,
                                      coverage_confirmed=True, **kwargs)

    def test_tax_and_common_expenses_explain_loss_after_positive_direct_result(self):
        result = self.details(method='revenue')
        # 1050 - 900 - 50 - 40 УСН - 50 НДС - 100 общие = -90.
        self.assertEqual(result.net_profit, D('-90'))
        self.assertEqual(result.analysis.result.result_before_tax, D('100'))
        self.assertEqual(result.categories['Расчётный УСН (распределённый)'], D('-40'))
        self.assertEqual(result.categories['Расчётный НДС (распределённый)'], D('-50'))
        self.assertEqual(result.categories['Общие расходы (распределённый вклад)'], D('-100'))
        self.assertEqual(sum(result.categories.values()), D('-90'))
        self.assertEqual(result.difference, D('0'))
        self.assertEqual(result.per_unit, D('-90'))
        self.assertEqual(result.roi, D('-10'))
        self.assertEqual(result.margin, D('-90') / D('1050') * 100)
        self.assertIn('убыток возникает после их учёта', result.explanation)
        self.assertIn('общих расходов', ' '.join(result.recommendations))

    def test_default_does_not_claim_unallocated_cost_is_inside_product_result(self):
        result = self.details()
        self.assertEqual(result.net_profit, D('10'))
        self.assertNotIn('Общие расходы (распределённый вклад)', result.categories)
        self.assertIn('остаются у магазина', result.allocation_note)
        self.assertNotIn('не определена', result.explanation)
        self.assertEqual(result.recommendations, ())

    def test_sizes_aggregate_each_cost_and_tax_once(self):
        operations = self.ops + [op('large', Kind.SALE, '2100', '42:l', 2)]
        costs = self.costs + [Cost('42:l', DAY, D('500'))]
        result = product_profit_details('42', operations, costs, self.rules, grouped=True,
                                       coverage_confirmed=True, method='revenue')
        # 3150 - 1900 - 50 - 100 - 120 УСН - 150 НДС = 830.
        self.assertEqual(result.net_profit, D('830'))
        self.assertEqual(sum(result.categories.values()), D('830'))
        self.assertEqual(result.per_unit, D('830') / 3)
        self.assertIn('Модель 42', result.explanation)
        self.assertEqual(result.categories['Расчётный УСН (распределённый)'], D('-120'))

    def test_repeated_operations_and_payout_do_not_inflate_detail(self):
        self.ops += [self.ops[0], op('payout', Kind.PAYOUT, '99999')]
        result = self.details()
        self.assertEqual(result.net_profit, D('10'))
        self.assertNotIn('Выплаты', result.categories)
        self.assertEqual(result.analysis.sold, 1)

    def test_unverified_or_missing_data_does_not_make_net_metrics(self):
        for covered, costs, rules in [(False, self.costs, self.rules), (True, [], self.rules),
                                      (True, self.costs, [])]:
            with self.subTest(covered=covered, costs=costs, rules=rules):
                result = product_profit_details('42:s', self.ops, costs, rules, coverage_confirmed=covered)
                self.assertIsNone(result.net_profit)
                self.assertIsNone(result.per_unit)
                self.assertIsNone(result.difference)
                self.assertNotIn('Расчётный УСН (распределённый)', result.categories)
                self.assertIn('не подставляются нулями', result.allocation_note)

    def test_unknown_product_is_not_reported_as_zero_profit(self):
        result = product_profit_details('missing', self.ops, self.costs, self.rules, coverage_confirmed=True)
        self.assertIsNone(result.net_profit)
        self.assertEqual(result.categories, {})

    def test_late_fee_correction_updates_loss_without_second_expense(self):
        result = self.details(method='revenue')
        self.assertEqual(result.net_profit, D('-90'))
        self.ops[-1] = op('storage', Kind.STORAGE, '-200', None)
        corrected = self.details(method='revenue')
        self.assertEqual(corrected.net_profit, D('-190'))
        self.assertEqual(corrected.categories['Общие расходы (распределённый вклад)'], D('-200'))
        self.assertEqual(corrected.difference, D('0'))

    def test_shared_expense_of_other_sku_is_not_reassigned(self):
        self.ops += [op('other', Kind.SALE, '1050', 'another', 1),
                     op('penalty', Kind.PENALTY, '-500', 'another')]
        self.costs += [Cost('another', DAY, D('400'))]
        result = self.details(method='revenue')
        self.assertEqual(result.net_profit, D('-40')) # 100 - 90 - half of common100
        self.assertNotIn('Штрафы', result.categories)
        self.assertEqual(result.categories['Общие расходы (распределённый вклад)'], D('-50'))
