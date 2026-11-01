from datetime import date,timedelta
from decimal import Decimal as D
from dataclasses import replace
import unittest
from wb_profit.finance import Operation,Kind,Cost,TaxSettings
from wb_profit.taxes import TaxRule
from wb_profit.daily_finance import daily_finances

DAY=date(2026,1,1)


def op(identifier,kind,amount,units=0,day=DAY,sku='a',**kwargs):
    return Operation('s','report',identifier,day,kind,D(amount),sku,units,**kwargs)


class DailyFinanceTests(unittest.TestCase):
    def setUp(self):
        self.ops=[op('a',Kind.SALE,'1050',1),op('fee',Kind.LOGISTICS,'-50'),
                  op('b',Kind.SALE,'2100',2,day=DAY+timedelta(days=2)),
                  op('common',Kind.STORAGE,'-90',day=DAY+timedelta(days=3),sku=None)]
        self.costs=[Cost('a',DAY,D('400'))]
        self.rules=[TaxRule(DAY,TaxSettings(D('4'),D('5'),confirmed_by_user=True))]

    def report(self,**kwargs):
        return daily_finances(self.ops,self.costs,self.rules,start=DAY,end=DAY+timedelta(days=3),**kwargs)

    def test_daily_net_and_undistributed_store_expenses_conserve_selected_period(self):
        report=self.report(coverage_confirmed=True)
        # day1: 1050-400-50-40-50=510; day3:2100-800-80-100=1120; day4:-90.
        self.assertEqual([row.net for row in report.rows],[D('510'),D('0'),D('1120'),D('-90')])
        self.assertEqual(report.period_net,D('1540'))
        self.assertEqual(report.daily_sum,D('1540'))
        self.assertEqual(report.difference,D('0'))
        self.assertEqual(report.rows[3].usn,D('0'))
        self.assertEqual(report.rows[3].vat,D('0'))

    def test_period_tax_rounding_is_allocated_instead_of_rounding_each_day_independently(self):
        ops=[op(str(i),Kind.SALE,'0.05',1,day=DAY+timedelta(days=i)) for i in range(3)]
        report=daily_finances(ops,[Cost('a',DAY,D('0'))],
                              [TaxRule(DAY,TaxSettings(D('4'),D('0'),confirmed_by_user=True))],coverage_confirmed=True)
        # 0.15 * 4% = 0.006 -> 0.01. Отдельные дни ошибочно дали бы 0+0+0.
        self.assertEqual([row.usn for row in report.rows],[D('0.01'),D('0'),D('0')])
        self.assertEqual([row.net for row in report.rows],[D('0.04'),D('0.05'),D('0.05')])
        self.assertEqual(report.daily_sum,D('0.14'))
        self.assertEqual(report.difference,D('0'))

    def test_tax_versions_and_quarters_are_not_blended_between_days(self):
        ops=[op('a',Kind.SALE,'1050',1,day=date(2026,6,30)),op('b',Kind.SALE,'1070',1,day=date(2026,7,1))]
        rules=[*self.rules,TaxRule(date(2026,7,1),TaxSettings(D('6'),D('7'),confirmed_by_user=True))]
        report=daily_finances(ops,self.costs,rules,coverage_confirmed=True)
        self.assertEqual([row.usn for row in report.rows],[D('40'),D('60')])
        self.assertEqual([row.vat for row in report.rows],[D('50'),D('70')])
        self.assertEqual(report.period_net,D('1100'))

    def test_two_kopecks_tax_over_four_days_cannot_become_negative_on_any_day(self):
        operations=[op(str(i),Kind.SALE,'0.125',1,day=DAY+timedelta(days=i)) for i in range(4)]
        report=daily_finances(operations,[Cost('a',DAY,D('0'))],
                              [TaxRule(DAY,TaxSettings(D('4'),D('0'),confirmed_by_user=True))],coverage_confirmed=True)
        self.assertEqual([row.usn for row in report.rows],[D('0.01'),D('0.01'),D('0'),D('0')])
        self.assertTrue(all(row.usn>=0 for row in report.rows))
        self.assertEqual(report.period_net,D('0.48'))
        self.assertEqual(report.difference,D('0'))

    def test_partial_empty_days_are_unknown_and_do_not_become_zero(self):
        report=self.report()
        self.assertIsNone(report.rows[1].before_tax)
        self.assertIsNone(report.rows[1].net)
        self.assertIsNone(report.period_net)
        self.assertIsNone(report.daily_sum)
        self.assertIsNone(report.difference)
        self.assertEqual(report.rows[0].before_tax,D('600'))
        self.assertTrue(all(row.net is None for row in report.rows))

    def test_missing_cost_or_unknown_operation_hides_net_for_entire_period(self):
        for costs,ops in [([],self.ops),(self.costs,[*self.ops,op('unknown',Kind.UNKNOWN,'-1',day=DAY+timedelta(days=1))])]:
            report=daily_finances(ops,costs,self.rules,coverage_confirmed=True)
            self.assertIsNone(report.period_net)
            self.assertTrue(all(row.net is None for row in report.rows))

    def test_payout_does_not_enter_profit_and_is_not_a_known_zero_day_without_coverage(self):
        ops=[*self.ops,op('payout',Kind.PAYOUT,'100000',day=DAY+timedelta(days=1))]
        report=daily_finances(ops,self.costs,self.rules,coverage_confirmed=True)
        self.assertEqual(report.period_net,D('1540'))
        self.assertEqual(report.rows[1].net,D('0'))
        partial=daily_finances(ops,self.costs,self.rules)
        self.assertIsNone(partial.rows[1].before_tax)
        self.assertIn('Только выплаты',partial.rows[1].issues)

    def test_unlinked_preliminary_and_return_tax_effects_are_not_fabricated(self):
        for extra in [replace(self.ops[0],identifier='estimate',confirmed=False),
                      op('return',Kind.RETURN,'-1050',-1,original_sale_id='a',cost_basis_date=DAY)]:
            report=daily_finances([*self.ops,extra],self.costs,self.rules,coverage_confirmed=True)
            self.assertIsNone(report.period_net)
            self.assertTrue(all(row.usn is None and row.vat is None for row in report.rows))

    def test_confirmed_version_replaces_estimate_and_repeated_id_is_not_added(self):
        fact=replace(self.ops[0],economic_key='order')
        estimate=replace(fact,source='operational',amount=D('999'),confirmed=False)
        report=daily_finances([fact,fact,estimate,*self.ops[1:]],self.costs,self.rules,coverage_confirmed=True)
        self.assertEqual(report.period_net,D('1540'))

    def test_explicit_interval_selects_operations_and_late_correction_updates_same_day(self):
        report=daily_finances(self.ops,self.costs,self.rules,start=DAY,end=DAY,coverage_confirmed=True)
        self.assertEqual(report.period_net,D('510'))
        changed=[self.ops[0],replace(self.ops[1],amount=D('-100')),*self.ops[2:]]
        report=daily_finances(changed,self.costs,self.rules,coverage_confirmed=True)
        self.assertEqual(report.rows[0].net,D('460'))
        self.assertEqual(report.period_net,D('1490'))

    def test_zero_sale_no_tax_base_and_huge_interval_do_not_create_guessed_values(self):
        report=daily_finances([op('free',Kind.SALE,'0',1)],self.costs,self.rules,coverage_confirmed=True)
        self.assertEqual(report.period_net,D('-400'))
        self.assertEqual(report.rows[0].usn,D('0'))
        report=daily_finances(self.ops,self.costs,self.rules,start=date(1900,1,1),end=date(9999,1,1),coverage_confirmed=True)
        self.assertEqual(len(report.rows),3)
        self.assertEqual(report.period_net,D('1540'))
        self.assertTrue(report.reasons)

    def test_external_only_and_absent_data_do_not_claim_zero_store_net(self):
        for ops in [[],[replace(op('rent',Kind.EXTERNAL,'-10'),source='external_expenses')]]:
            report=daily_finances(ops,self.costs,self.rules,coverage_confirmed=True)
            self.assertIsNone(report.period_net)

    def test_invalid_period_and_cross_store_input_are_rejected(self):
        with self.assertRaises(ValueError):daily_finances(self.ops,self.costs,self.rules,start=DAY)
        with self.assertRaises(ValueError):daily_finances(self.ops,self.costs,self.rules,start=DAY+timedelta(days=1),end=DAY)
        with self.assertRaises(ValueError):daily_finances([*self.ops,replace(self.ops[0],store='other')],self.costs,self.rules)
