import unittest
from datetime import date
from decimal import Decimal as D
from wb_profit.finance import Operation,Kind,Cost,TaxSettings
from wb_profit.taxes import TaxRule
from wb_profit.profit_distribution import distribute_profit

DAY=date(2026,1,1)

def operation(identifier,kind,amount,sku=None,units=0,day=DAY):return Operation('s','report',identifier,day,kind,D(amount),sku,units)

class ProfitDistributionTests(unittest.TestCase):
    def setUp(self):
        self.rules=[TaxRule(DAY,TaxSettings(D('4'),D('5'),confirmed_by_user=True))]
        self.ops=[operation('a',Kind.SALE,'1050','a',1),operation('b',Kind.SALE,'2100','b',1),operation('storage',Kind.STORAGE,'-90')]
        self.costs=[Cost('a',DAY,D('400')),Cost('b',DAY,D('800'))]
    def test_unallocated_pool_and_taxes_conserve_store_result(self):
        result=distribute_profit(self.ops,self.costs,self.rules,coverage_confirmed=True)
        # a: 1050-400-40-50=560; b: 2100-800-80-100=1120; магазин 1590.
        self.assertEqual(result.net_by_sku,{'a':D('560'),'b':D('1120')})
        self.assertEqual(result.unallocated_net,D('-90'))
        self.assertEqual(result.store_net,D('1590'))
        self.assertEqual(result.difference,D('0'))
    def test_common_cost_distribution_is_explicit_and_counted_once(self):
        result=distribute_profit(self.ops,self.costs,self.rules,coverage_confirmed=True,method='revenue')
        self.assertEqual(result.common_by_sku,{'a':D('-30'),'b':D('-60')})
        self.assertEqual(result.net_by_sku,{'a':D('530'),'b':D('1060')})
        self.assertEqual(result.unallocated_net,D('0'))
        self.assertEqual(result.difference,D('0'))
    def test_different_tax_versions_are_not_blended_between_products(self):
        rules=[*self.rules,TaxRule(date(2026,7,1),TaxSettings(D('6'),D('7'),confirmed_by_user=True))]
        ops=[self.ops[0],operation('b',Kind.SALE,'1070','b',1,day=date(2026,7,1))]
        result=distribute_profit(ops,self.costs,rules,coverage_confirmed=True)
        self.assertEqual(result.taxes_by_sku,{'a':D('90'),'b':D('130')})
    def test_unverified_period_does_not_get_product_net_profit(self):
        self.assertIsNone(distribute_profit(self.ops,self.costs,self.rules))
        self.assertIsNone(distribute_profit(self.ops,[],self.rules,coverage_confirmed=True))
    def test_fractional_kopeck_is_not_rounded_to_make_distribution_pass(self):
        ops=[*self.ops[:2],operation('storage',Kind.STORAGE,'-0.005')]
        result=distribute_profit(ops,self.costs,self.rules,coverage_confirmed=True,method='revenue')
        self.assertEqual(result.unallocated_net,D('-0.005'))
        self.assertTrue(result.warnings)
        self.assertEqual(result.difference,D('0'))

    def test_unassigned_sales_and_their_cost_are_not_spread_to_known_products(self):
        ops=[self.ops[0],operation('unassigned',Kind.SALE,'1050',None,1),self.ops[2]]
        result=distribute_profit(ops,[Cost('*',DAY,D('400'))],self.rules,coverage_confirmed=True,method='revenue')
        self.assertEqual(result.net_by_sku,{'a':D('470')})
        self.assertEqual(result.unallocated_net,D('560'))
        self.assertEqual(result.store_net,D('1030'))
        self.assertEqual(result.difference,D('0'))
