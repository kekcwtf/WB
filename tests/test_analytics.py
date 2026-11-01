import unittest
from datetime import date
from decimal import Decimal as D
from wb_profit.finance import Operation, Kind, Cost
from wb_profit.analytics import analyse_sku, stock_estimate
from wb_profit.expenses import save_external_expense
from wb_profit.storage import Ledger

DAY = date(2026, 1, 1)

class AnalyticsTests(unittest.TestCase):
    def test_loss_explanation_is_based_on_ledger(self):
        ops = [Operation('s', 'report', '1', DAY, Kind.SALE, D('1000'), 'a', 1),
               Operation('s', 'report', '2', DAY, Kind.ADVERTISING, D('-500'), 'a'),
               Operation('s', 'report', '3', DAY, Kind.LOGISTICS, D('-200'), 'a')]
        result = analyse_sku('a', ops, [Cost('a', DAY, D('400'))])
        self.assertEqual(result.result.result_before_tax, D('-100'))
        self.assertEqual(result.margin_before_tax, D('-10'))
        self.assertEqual(result.roi_before_tax, D('-25'))
        self.assertEqual(result.profit_per_unit_before_tax, D('-100'))
        self.assertIn('реклама (500.00', result.explanation)
        self.assertEqual(len(result.recommendations), 3)
        self.assertFalse(result.incomplete)

    def test_missing_cost_does_not_claim_profitability(self):
        op = Operation('s', 'report', '1', DAY, Kind.SALE, D('1000'), 'a', 1)
        result = analyse_sku('a', [op], [])
        self.assertTrue(result.incomplete)
        self.assertIsNone(result.margin_before_tax)
        self.assertIn('недостаточно', result.explanation)
        self.assertEqual(result.recommendations, ())

    def test_common_expenses_are_not_silently_allocated(self):
        ops = [Operation('s', 'report', '1', DAY, Kind.SALE, D('1000'), 'a', 1),
               Operation('s', 'report', '2', DAY, Kind.STORAGE, D('-900'), None)]
        result = analyse_sku('a', ops, [Cost('a', DAY, D('400'))])
        self.assertEqual(result.result.result_before_tax, D('600'))
        self.assertNotIn('Хранение', result.categories)

    def test_stock_estimate(self):
        result = stock_estimate(60, 30, 30)
        self.assertEqual(result.average_daily_sales, D('1'))
        self.assertEqual(result.estimated_days, D('60'))
        self.assertIsNone(stock_estimate(60, 0, 30).estimated_days)
        self.assertTrue(stock_estimate(60, 0, 30).no_sales)
        with self.assertRaises(ValueError):
            stock_estimate(1, 1, 0)

    def test_external_expense_edit_does_not_duplicate(self):
        ledger = Ledger(':memory:')
        identifier = save_external_expense(ledger, 's', DAY, D('123.45'), 'Упаковка')
        save_external_expense(ledger, 's', DAY, D('100'), 'Упаковка', identifier=identifier)
        operations = ledger.operations('s', date.min, date.max)
        self.assertEqual(len(operations), 1)
        self.assertEqual(operations[0].amount, D('-100'))
        self.assertIsNone(operations[0].sku)
        self.assertEqual(ledger.db.execute('SELECT COUNT(*) FROM revisions').fetchone()[0], 1)
        with self.assertRaises(ValueError):
            save_external_expense(ledger, 's', DAY, D('-10'), 'Расход')
        ledger.close()

    def test_model_groups_sizes_and_uses_each_sku_cost(self):
        from wb_profit.analytics import analyse_group
        operations=[Operation('s','report','a',DAY,Kind.SALE,D('1000'),'42:small',1),
                    Operation('s','report','b',DAY,Kind.SALE,D('1200'),'42:large',1)]
        costs=[Cost('42:small',DAY,D('400')),Cost('42:large',DAY,D('500'))]
        group=analyse_group('42',operations,costs)
        self.assertEqual(group.result.cost,D('900'))
        self.assertEqual(group.result.result_before_tax,D('1300'))
        self.assertEqual(group.sold,2)
