import unittest
from datetime import date
from decimal import Decimal as D
from wb_profit.finance import Operation, Kind
from wb_profit.reconciliation import Controls, audit


class ReconciliationTests(unittest.TestCase):
    def test_independent_controls_and_payout(self):
        def operation(identifier, kind, amount):
            return Operation('shop', 'report', identifier, date(2026, 1, 1), kind, D(amount))
        sale = operation('1', Kind.SALE, '1000')
        operations = [sale, sale, operation('2', Kind.COMMISSION, '-100'),
                      operation('3', Kind.LOGISTICS, '-50'), operation('4', Kind.PAYOUT, '700'),
                      operation('5', Kind.EXTERNAL, '-20')]
        result = audit(operations, Controls(D('1000'), D('150'), D('850')))
        self.assertTrue(result.reconciled)
        self.assertEqual(result.payouts, D('700'))
        self.assertEqual(result.duplicate_count, 1)
        self.assertEqual(result.payable, D('850'))
        self.assertFalse(audit(operations, Controls(D('1000'), D('160'), D('840'))).reconciled)
        self.assertFalse(audit(operations, Controls()).reconciled)

    def test_empty_is_not_reconciled(self):
        self.assertFalse(audit([], Controls(D('0'), D('0'), D('0'))).reconciled)
