from dataclasses import replace
from datetime import date
from decimal import Decimal as D
from pathlib import Path
import tempfile
import unittest
from wb_profit.finance import Operation,Kind,Cost,TaxSettings
from wb_profit.taxes import TaxRule,calculate_with_history
from wb_profit.return_review import review_returns,review_from_ledger
from wb_profit.storage import Ledger

JAN=date(2026,1,1);JUL=date(2026,7,1)


class ReturnReviewTests(unittest.TestCase):
    def setUp(self):
        self.sale=Operation('s','wb_finance','1:1:revenue',JAN,Kind.SALE,D('1050'),'42:123',1)
        self.returned=Operation('s','wb_finance','2:2:revenue',JUL,Kind.RETURN,D('-1050'),'42:123',-1,
                                original_sale_id=self.sale.identifier,cost_basis_date=JAN)
        self.costs=[Cost('42:123',JAN,D('400')),Cost('42:123',JUL,D('900'))]
        self.rules=[TaxRule(JAN,TaxSettings(D('4'),D('5'),confirmed_by_user=True)),
                    TaxRule(JUL,TaxSettings(D('6'),D('7'),confirmed_by_user=True))]

    def review(self,returned=None,sale=None,history=()):
        return review_returns([returned or self.returned],[sale or self.sale],history,self.costs,self.rules)

    def test_cost_uses_original_date_and_tax_price_components_use_original_rates(self):
        report=self.review();row=report.rows[0]
        self.assertTrue(row.linked)
        self.assertEqual(row.restored_cost,D('400'))
        self.assertEqual(row.returned_amount,D('1050'))
        self.assertEqual(row.usn_price_component,D('40'))
        self.assertEqual(row.vat_price_component,D('50'))
        self.assertIn('quarter',row.flags)
        self.assertIn('rate',row.flags)
        self.assertEqual(row.original_vat,D('5'))
        self.assertIn('не налоговый вычет',' '.join(row.issues))
        # Новый отчёт не меняет строгое финансовое ядро и не проводит вычеты.
        result,_=calculate_with_history([self.sale,self.returned],self.costs,self.rules)
        self.assertIsNone(result.net_profit)

    def test_partial_returns_include_history_outside_selected_period_once(self):
        sale=replace(self.sale,amount=D('2100'),units=2)
        earlier=replace(self.returned,identifier='early',occurred=date(2026,2,1))
        report=self.review(sale=sale,history=[earlier,self.returned,self.returned])
        row=report.rows[0]
        self.assertTrue(row.linked)
        self.assertEqual(row.cumulative_units,2)
        self.assertEqual(row.cumulative_amount,D('2100'))
        self.assertEqual(report.known_restored_cost,D('400')) # только выбранный возврат

    def test_over_returned_quantity_cannot_restore_cost_twice(self):
        extra=replace(self.returned,identifier='extra')
        row=self.review(history=[extra]).rows[0]
        self.assertFalse(row.linked)
        self.assertIn('amount',row.flags)
        self.assertIsNone(row.restored_cost)
        self.assertIsNone(row.vat_price_component)

    def test_price_mismatch_preserves_physical_cost_but_has_no_tax_price_guess(self):
        row=self.review(returned=replace(self.returned,amount=D('-1200'))).rows[0]
        self.assertTrue(row.linked)
        self.assertEqual(row.restored_cost,D('400'))
        self.assertIn('amount',row.flags)
        self.assertIsNone(row.usn_price_component)
        self.assertIsNone(row.vat_price_component)

    def test_rounding_mismatch_is_explicit_instead_of_assuming_unit_price(self):
        sale=replace(self.sale,amount=D('100'),units=3)
        row=self.review(sale=sale,returned=replace(self.returned,amount=D('-33.33'))).rows[0]
        self.assertIn('amount',row.flags)
        self.assertIsNone(row.vat_price_component)

    def test_missing_original_or_wrong_source_or_sku_cannot_be_silently_matched(self):
        for originals in [[],[replace(self.sale,source='other')],[replace(self.sale,sku='42:other')]]:
            row=review_returns([self.returned],originals,[],self.costs,self.rules).rows[0]
            self.assertFalse(row.linked)
            self.assertIsNone(row.restored_cost)
            self.assertIsNone(row.usn_price_component)

    def test_unknown_purchase_cost_does_not_hide_known_tax_price_component(self):
        row=review_returns([self.returned],[self.sale],[],[],self.rules).rows[0]
        self.assertIsNone(row.restored_cost)
        self.assertIn('cost',row.flags)
        self.assertEqual(row.vat_price_component,D('50'))

    def test_unconfirmed_origin_settings_or_vat_excluded_price_do_not_create_components(self):
        for settings in [TaxSettings(D('4'),D('5')),TaxSettings(D('4'),D('5'),vat_included=False,confirmed_by_user=True)]:
            row=review_returns([self.returned],[self.sale],[],self.costs,[TaxRule(JAN,settings)]).rows[0]
            self.assertIsNone(row.vat_price_component)
            self.assertIn('tax',row.flags)

    def test_same_calendar_quarter_is_not_declared_confirmed_tax_period(self):
        returned=replace(self.returned,occurred=date(2026,1,10))
        row=self.review(returned=returned).rows[0]
        self.assertNotIn('quarter',row.flags)
        self.assertNotIn('rate',row.flags)
        self.assertEqual(row.usn_price_component,D('40'))
        self.assertIn('не подтверждённое уменьшение',' '.join(row.issues))

    def test_preliminary_return_is_not_a_fact_and_mixed_stores_are_rejected(self):
        row=self.review(returned=replace(self.returned,confirmed=False)).rows[0]
        self.assertFalse(row.linked)
        self.assertIsNone(row.returned_amount)
        self.assertIsNone(row.restored_cost)
        with self.assertRaises(ValueError):
            review_returns([self.returned],[replace(self.sale,store='other')],[],self.costs,self.rules)

    def test_bulk_history_lookup_crosses_sqlite_batch_boundary_and_preserves_store_isolation(self):
        with tempfile.TemporaryDirectory() as folder:
            ledger=Ledger(Path(folder)/'db.sqlite')
            sales=[replace(self.sale,identifier='sale'+str(i)) for i in range(450)]
            returns=[replace(self.returned,identifier='return'+str(i),original_sale_id=sale.identifier) for i,sale in enumerate(sales)]
            ledger.upsert([*sales,*returns,replace(self.sale,store='other')])
            result=review_from_ledger(ledger,'s',returns,self.costs,self.rules)
            self.assertEqual(result.linked_count,450)
            self.assertEqual(result.known_restored_cost,D('180000'))
            self.assertTrue(all(row.operation.store=='s' and row.sale.store=='s' for row in result.rows))
            self.assertEqual(len(ledger.operations_by_keys('s',[('wb_finance','sale0')]*3)),1)
            ledger.close()
