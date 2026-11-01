import unittest
from dataclasses import replace
from datetime import date
from decimal import Decimal as D
from wb_profit.finance import Operation, Kind, Cost, TaxSettings, calculate, allocate, money, reconcile_versions

DAY = date(2026, 1, 10)


def op(identifier, kind, amount, sku='a', units=0, **kwargs):
    return Operation('store', 'financial', identifier, DAY, kind, D(amount), sku, units, **kwargs)


class FinanceTests(unittest.TestCase):
    def test_small_allocation_never_flips_sign_or_uses_zero_weight(self):
        weights={'a':D('1'),'b':D('1'),'c':D('1'),'d':D('1'),'zero':D('0')}
        self.assertEqual(allocate(D('0.02'),weights),{'a':D('0.01'),'b':D('0.01'),'c':D('0'),'d':D('0'),'zero':D('0')})
        self.assertEqual(allocate(D('-0.02'),weights),{'a':D('-0.01'),'b':D('-0.01'),'c':D('0'),'d':D('0'),'zero':D('0')})
        for count in range(2,12):
            for cents in range(1,20):
                total=D(cents)/100
                positive=allocate(total,{str(i):D(i) for i in range(count)})
                negative=allocate(-total,{str(i):D(i) for i in range(count)})
                self.assertEqual(sum(positive.values()),total)
                self.assertEqual(sum(negative.values()),-total)
                self.assertTrue(all(value>=0 for value in positive.values()))
                self.assertTrue(all(value<=0 for value in negative.values()))
                self.assertEqual(positive['0'],D('0'))

    def test_independent_control_example(self):
        # 1050 sale − 100 commission − 50 delivery − 400 purchase
        # VAT included = 50; USN (1050−50)*4%=40; profit=410.
        result = calculate([op('1', Kind.SALE, '1050', units=1),
                            op('2', Kind.COMMISSION, '-100'), op('3', Kind.LOGISTICS, '-50')],
                           [Cost('a', DAY, D('400'))], TaxSettings(D('4'), D('5'), confirmed_by_user=True))
        self.assertEqual(result.net_profit, D('410.00'))
        self.assertEqual((result.usn, result.vat), (D('40'), D('50')))

    def test_returns_not_false_full_profit(self):
        result = calculate([op('r', Kind.RETURN, '-1050', units=-1,original_sale_id='sale-1',cost_basis_date=DAY)],
                           [Cost('a', DAY, D('400'))], TaxSettings(D('4'), D('5'), confirmed_by_user=True))
        self.assertEqual(result.revenue, D('-1050'))
        self.assertEqual(result.cost, D('-400'))
        self.assertIsNone(result.net_profit)

    def test_expense_categories_once(self):
        kinds = [Kind.COMMISSION, Kind.LOGISTICS, Kind.REVERSE_LOGISTICS, Kind.STORAGE,
                 Kind.ACCEPTANCE, Kind.ACQUIRING, Kind.ADVERTISING, Kind.PENALTY, Kind.DEDUCTION, Kind.EXTERNAL]
        result = calculate([op(str(i), kind, '-10') for i, kind in enumerate(kinds)], [], None)
        self.assertEqual(result.wb_and_external, D('-100'))

    def test_missing_cost_blocks_net(self):
        result = calculate([op('1', Kind.SALE, '100', units=1)], [], TaxSettings(D('0'), D('0'), confirmed_by_user=True))
        self.assertIsNone(result.net_profit)
        self.assertEqual(result.missing_costs, ('a',))

    def test_cost_history(self):
        result = calculate([op('1', Kind.SALE, '100', units=1)],
                           [Cost('a', date(2025, 1, 1), D('20')), Cost('a', date(2027, 1, 1), D('80'))], None)
        self.assertEqual(result.cost, D('20'))

    def test_payout_not_revenue(self):
        result = calculate([op('1', Kind.PAYOUT, '1000', sku=None)], [], None)
        self.assertEqual(result.revenue, D('0'))
        self.assertEqual(result.result_before_tax, D('0'))

    def test_unknown_blocks_net(self):
        result = calculate([op('1', Kind.UNKNOWN, '-10')], [], TaxSettings(D('0'), D('0'), confirmed_by_user=True))
        self.assertIsNone(result.net_profit)
        self.assertEqual(result.unknown_operations, ('1',))

    def test_duplicate_id(self):
        sale = op('1', Kind.SALE, '100', units=1)
        self.assertEqual(len(reconcile_versions([sale, sale])), 1)

    def test_confirmed_replaces_estimate(self):
        fact = op('1', Kind.ADVERTISING, '-100', economic_key='campaign:day')
        estimate = replace(fact, source='advertising', confirmed=False, amount=D('-90'))
        self.assertEqual(reconcile_versions([estimate, fact]), [fact])

    def test_ambiguous_links_rejected(self):
        fact = op('1', Kind.ADVERTISING, '-100', economic_key='campaign:day')
        with self.assertRaises(ValueError):
            reconcile_versions([fact, replace(fact, identifier='2')])

    def test_preliminary_not_mixed(self):
        with self.assertRaises(ValueError):
            calculate([op('1', Kind.SALE, '10', confirmed=False)], [], None)

    def test_conservation_sku_and_unallocated(self):
        operations = [op('1', Kind.SALE, '100', sku='a'), op('2', Kind.SALE, '200', sku='b'), op('3', Kind.STORAGE, '-50', sku=None)]
        whole = calculate(operations, [], None)
        pieces = [calculate([x for x in operations if x.sku == sku], [], None) for sku in ['a', 'b']]
        self.assertEqual(sum((p.result_before_tax for p in pieces), D('0')) + whole.unallocated, whole.result_before_tax)

    def test_allocation_exact(self):
        for amount in ['100.00', '-100.00', '0.01', '-0.01']:
            allocated = allocate(D(amount), {'a': D('1'), 'b': D('1'), 'c': D('1'), 'zero': D('0')})
            self.assertEqual(sum(allocated.values()), D(amount))
            self.assertEqual(allocated['zero'], D('0'))

    def test_rates_and_precision(self):
        self.assertEqual(TaxSettings(D('0'), D('7')).estimate(D('1070')), (D('0'), D('70')))
        with self.assertRaises(TypeError):
            money(0.1)
        with self.assertRaises(ValueError):
            money('NaN')
        with self.assertRaises(ValueError):
            TaxSettings(D('-1'), D('5'))

    def test_tax_settings_require_confirmation(self):
        self.assertIsNone(calculate([], [], TaxSettings(D('4'), D('5'))).net_profit)

    def test_stores_not_combined(self):
        one = op('1', Kind.STORAGE, '-10')
        with self.assertRaises(ValueError):
            calculate([one, replace(one, store='other')], [], None)

    def test_sale_without_quantity_cannot_hide_missing_cost(self):
        result = calculate([op('1', Kind.SALE, '1000')],
                           [Cost('a', DAY, D('400'))], TaxSettings(D('4'), D('5'), confirmed_by_user=True))
        self.assertIsNone(result.net_profit)
        self.assertEqual(result.missing_costs, ('a',))

    def test_conflicting_cost_history_is_not_silently_selected(self):
        with self.assertRaisesRegex(ValueError,'Две разные'):
            calculate([op('1',Kind.SALE,'1000',units=1)],[Cost('a',DAY,D('400')),Cost('a',DAY,D('500'))],None)

    def test_default_cost_requires_explicit_assignment_and_respects_individual_history(self):
        sale=op('1',Kind.SALE,'2000',units=1)
        self.assertEqual(calculate([sale],[],None).missing_costs,('a',))
        self.assertEqual(calculate([sale],[Cost('*',DAY,D('940'))],None).cost,D('940'))
        self.assertEqual(calculate([sale],[Cost('*',DAY,D('940')),Cost('a',DAY,D('800'))],None).cost,D('800'))
