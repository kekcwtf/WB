from dataclasses import replace
from datetime import date
from decimal import Decimal as D
import unittest
from wb_profit.finance import Operation,Kind,Cost,TaxSettings
from wb_profit.taxes import TaxRule
from wb_profit.overview import financial_overview,WB_EXPENSES

DAY=date(2026,1,1)


def op(identifier,kind,amount,units=0,**kwargs):
    return Operation('shop','report',identifier,DAY,kind,D(amount),'a',units,**kwargs)


class OverviewTests(unittest.TestCase):
    def setUp(self):
        self.operations=[op('sale',Kind.SALE,'1050',1),op('commission',Kind.COMMISSION,'-100'),
                         op('logistics',Kind.LOGISTICS,'-50'),op('ads',Kind.ADVERTISING,'-20'),
                         op('external',Kind.EXTERNAL,'-30'),op('payout',Kind.PAYOUT,'2000')]
        self.costs=[Cost('a',DAY,D('400'))]
        self.rules=[TaxRule(DAY,TaxSettings(D('4'),D('5'),confirmed_by_user=True))]

    def report(self,**kwargs):
        return financial_overview(self.operations,self.costs,self.rules,financial_complete=True,full_complete=True,**kwargs)

    def test_independent_profit_bridge_does_not_deduct_vat_twice(self):
        report=self.report();m=report.metrics
        # 1050 sales - 170 WB - 30 external - 400 COGS - 50 VAT - 40 USN = 360.
        self.assertEqual(m['sales'].value,D('1050'))
        self.assertEqual(m['returns'].value,D('0'))
        self.assertEqual(m['revenue'].value,D('1050'))
        self.assertEqual(m['wb_cost'].value,D('170'))
        self.assertEqual(m['gross'].value,D('600')) # 1050 - VAT50 - COGS400
        self.assertEqual(m['before_tax'].value,D('450'))
        self.assertEqual(m['operating'].value,D('400')) # includes VAT exactly once
        self.assertEqual(m['usn'].value,D('40'))
        self.assertEqual(m['vat'].value,D('50'))
        self.assertEqual(m['net'].value,D('360'))
        self.assertEqual(m['payout'].value,D('2000'))
        self.assertEqual(report.arithmetic_difference,D('0'))

    def test_ratio_definitions_have_explicit_positive_denominators(self):
        m=self.report().metrics
        self.assertEqual(m['roi'].value,D('90'))
        self.assertEqual(m['profitability'].value,D('60'))
        self.assertEqual(m['margin'].value,D('360')/D('1050')*100)
        self.assertEqual(m['drr'].value,D('20')/D('1050')*100)

    def test_sales_and_returns_are_separate_and_return_tax_is_not_assumed(self):
        self.operations.append(op('return',Kind.RETURN,'-1050',-1,original_sale_id='sale',cost_basis_date=DAY))
        m=self.report().metrics
        self.assertEqual(m['sales'].value,D('1050'))
        self.assertEqual(m['returns'].value,D('1050'))
        self.assertEqual(m['revenue'].value,D('0'))
        self.assertEqual(m['cost'].value,D('0'))
        for key in ['gross','operating','net','roi','margin','drr']:
            self.assertIsNone(m[key].value)

    def test_absent_partial_data_is_not_presented_as_verified_zero(self):
        report=financial_overview([self.operations[0]],self.costs,self.rules)
        self.assertEqual(report.metrics['sales'].value,D('1050'))
        self.assertIsNone(report.metrics['returns'].value)
        self.assertIsNone(report.metrics['wb_cost'].value)
        self.assertIsNone(report.expenses[Kind.STORAGE].value)
        self.assertIsNone(report.metrics['net'].value)
        self.assertEqual(report.metrics['revenue'].status,'загружено')

    def test_missing_purchase_cost_does_not_make_zero_cost_or_net_profit(self):
        report=financial_overview(self.operations,[],self.rules,financial_complete=True,full_complete=True)
        for key in ['cost','gross','operating','before_tax','net','roi','margin','profitability']:
            self.assertIsNone(report.metrics[key].value,key)
        self.assertIn('Не назначена себестоимость',' '.join(report.reasons))

    def test_unmatched_advertising_blocks_complete_ratios_without_second_expense(self):
        report=financial_overview(self.operations,self.costs,self.rules,financial_complete=True,full_complete=False)
        self.assertEqual(report.expenses[Kind.ADVERTISING].value,D('20'))
        self.assertEqual(report.metrics['before_tax'].value,D('450'))
        self.assertEqual(report.metrics['gross'].value,D('600'))
        for key in ['net','operating','drr','roi','margin']:
            self.assertIsNone(report.metrics[key].value)

    def test_verified_drr_does_not_require_purchase_cost_or_tax_settings(self):
        report=financial_overview(self.operations,[],[],financial_complete=True,full_complete=True)
        self.assertIsNone(report.metrics['net'].value)
        self.assertEqual(report.metrics['drr'].value,D('20')/D('1050')*100)

    def test_payout_only_or_external_only_never_claims_zero_store_revenue(self):
        for operations in [[self.operations[-1]],[replace(self.operations[-2],source='external_expenses')],[]]:
            report=financial_overview(operations,self.costs,self.rules,financial_complete=True,full_complete=True)
            self.assertIsNone(report.metrics['revenue'].value)
            self.assertIsNone(report.metrics['net'].value)
            self.assertIsNone(report.metrics['vat'].value)

    def test_fee_only_partial_period_does_not_claim_zero_revenue_or_vat(self):
        report=financial_overview([self.operations[1]],self.costs,self.rules)
        self.assertEqual(report.expenses[Kind.COMMISSION].value,D('100'))
        self.assertIsNone(report.metrics['revenue'].value)
        self.assertIsNone(report.metrics['gross'].value)
        self.assertIsNone(report.metrics['vat'].value)

    def test_all_expense_categories_and_reversals_enter_bridge_once(self):
        operations=[self.operations[0],*[op(str(index),kind,'-10') for index,kind in enumerate(WB_EXPENSES)],
                    op('external',Kind.EXTERNAL,'-10'),op('reverse',Kind.STORAGE,'4')]
        report=financial_overview(operations,self.costs,self.rules,financial_complete=True,full_complete=True)
        self.assertEqual(report.metrics['wb_cost'].value,D('86'))
        self.assertEqual(report.expenses[Kind.STORAGE].value,D('6'))
        # 1050 - 400 - 86 - 10 - 50 - 40 = 464
        self.assertEqual(report.metrics['net'].value,D('464'))
        self.assertEqual(report.arithmetic_difference,D('0'))

    def test_confirmed_operation_replaces_preliminary_and_duplicates_are_not_summed(self):
        fact=replace(self.operations[0],economic_key='sale-a')
        estimate=replace(fact,source='operational',confirmed=False,amount=D('999'))
        report=financial_overview([fact,fact,estimate,*self.operations[1:]],self.costs,self.rules,
                                  financial_complete=True,full_complete=True)
        self.assertEqual(report.metrics['net'].value,D('360'))
        unknown_estimate=replace(estimate,economic_key=None)
        report=financial_overview([*self.operations,unknown_estimate],self.costs,self.rules,
                                  financial_complete=True,full_complete=True)
        self.assertEqual(report.metrics['sales'].value,D('1050'))
        self.assertIsNone(report.metrics['net'].value)

    def test_unknown_or_unclassified_income_does_not_receive_automatic_tax_rule(self):
        for kind in [Kind.UNKNOWN,Kind.COMPENSATION,Kind.ADJUSTMENT]:
            report=financial_overview([*self.operations,op('extra',kind,'100')],self.costs,self.rules,
                                      financial_complete=True,full_complete=True)
            self.assertIsNone(report.metrics['net'].value)
            self.assertIsNone(report.metrics['vat'].value)

    def test_late_correction_recalculates_and_negative_ad_refund_has_no_drr(self):
        self.operations[1]=replace(self.operations[1],amount=D('-150'))
        self.assertEqual(self.report().metrics['net'].value,D('310'))
        self.operations[3]=replace(self.operations[3],amount=D('10'))
        self.assertIsNone(self.report().metrics['drr'].value)

    def test_stores_cannot_be_merged_in_one_dashboard(self):
        with self.assertRaises(ValueError):
            financial_overview([*self.operations,replace(self.operations[0],store='other')],self.costs,self.rules)
