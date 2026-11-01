import os
os.environ.setdefault('QT_QPA_PLATFORM', 'offscreen')
import unittest
from datetime import date
from decimal import Decimal as D
from PySide6.QtWidgets import QApplication
from wb_profit.economics_view import EconomicsView
from wb_profit.economics import historical_model
from wb_profit.finance import Operation, Kind, Cost, TaxSettings


class EconomicsBreakdownTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.app = QApplication.instance() or QApplication([])

    def setUp(self):
        self.day = date(2026, 1, 1)
        self.ops = [Operation('s', 'report', 'sale', self.day, Kind.SALE, D('2100'), 'a', 2)]
        for kind, amount in [(Kind.COMMISSION, '-210'), (Kind.LOGISTICS, '-100'),
                             (Kind.REVERSE_LOGISTICS, '-20'), (Kind.ADVERTISING, '-40'),
                             (Kind.STORAGE, '10')]:
            self.ops.append(Operation('s', 'report', kind.value, self.day, kind, D(amount), 'a'))
        self.costs = [Cost('a', self.day, D('400'))]
        self.taxes = TaxSettings(D('4'), D('5'), confirmed_by_user=True)
        self.view = EconomicsView()
        self.addCleanup(self.view.close)

    def context(self, **kwargs):
        self.view.set_context(self.ops, self.costs, self.taxes, coverage_confirmed=True, **kwargs)

    def rows(self):
        table = self.view.comparison
        return {table.item(row, 0).text(): [table.item(row, col).value for col in (1, 2, 3)]
                for row in range(table.rowCount())}

    def test_expense_breakdown_and_independent_price_comparison(self):
        self.context()
        self.view.price.setText('2100')
        rows = self.rows()
        # На единицу: 50 доставка +10 обратная +20 реклама -5 возврат хранения =75.
        # 1050 -105 -75 -400 -50 НДС -40 УСН =380.
        # 2100 -210 -75 -400 -100 НДС -80 УСН =1235.
        self.assertEqual(rows['Условная прибыль, ₽'], [D('380'), D('1235'), D('855')])
        self.assertEqual(rows['Остальные расходы — всего, ₽'], [D('75'), D('75'), D('0')])
        self.assertEqual(rows['Хранение, ₽'][0], D('-5'))
        self.assertEqual(rows['Обратная логистика, ₽'][0], D('10'))
        self.assertEqual(rows['Эквайринг, ₽'][0], D('0'))
        self.assertEqual(rows['ROI к себестоимости, %'][1], D('308.75'))
        self.assertIn('не фактический итог', self.view.output.text())

    def test_invalid_price_or_missing_coverage_clears_old_values(self):
        self.context()
        self.view.price.setText('NaN')
        self.assertEqual(self.view.comparison.rowCount(), 0)
        self.view.price.setText('1050')
        self.assertGreater(self.view.comparison.rowCount(), 0)
        self.view.set_context(self.ops, self.costs, self.taxes)
        self.assertEqual(self.view.comparison.rowCount(), 0)
        self.assertFalse(self.view.price.isEnabled())

    def test_fbw_tariff_applies_to_both_scenarios_and_zero_cost_roi_is_unknown(self):
        self.costs = [Cost('a', self.day, D('0'))]
        self.context(fbw_rates={'a': D('20')})
        self.view.price.setText('2100')
        rows = self.rows()
        self.assertEqual(rows['Комиссия WB, ₽'], [D('210'), D('420'), D('210')])
        self.assertEqual(rows['ROI к себестоимости, %'], [None, None, None])

    def test_duplicate_rows_shared_expenses_and_payouts_are_not_unit_costs(self):
        self.ops += [self.ops[1], Operation('s', 'report', 'shared', self.day, Kind.STORAGE, D('-999')),
                     Operation('s', 'report', 'pay', self.day, Kind.PAYOUT, D('999'), 'a')]
        model = historical_model('a', self.ops, self.costs, self.taxes, coverage_confirmed=True)
        self.assertEqual(sum((amount for _, amount in model.expense_components), D('0')), D('75'))
        self.assertEqual(model.forecast(D('1050')).estimated_profit, D('380'))

    def test_same_sku_in_two_stores_is_rejected(self):
        self.ops.append(Operation('other', 'report', 'sale', self.day, Kind.SALE, D('100'), 'a', 1))
        with self.assertRaisesRegex(ValueError, 'одному магазину'):
            historical_model('a', self.ops, self.costs, self.taxes, coverage_confirmed=True)
