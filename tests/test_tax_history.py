import tempfile
import unittest
from datetime import date
from decimal import Decimal as D
from pathlib import Path
from wb_profit.finance import Operation,Kind,Cost,TaxSettings
from wb_profit.taxes import TaxRule,calculate_with_history,save_rule,load_rules,rule_at
from wb_profit.storage import Ledger

JAN=date(2026,1,1);JUL=date(2026,7,1)

def sale(identifier,day,amount):
    return Operation('shop','report',identifier,day,Kind.SALE,D(amount),'a',1)

class TaxHistoryTests(unittest.TestCase):
    def test_independent_two_rate_control(self):
        rules=[TaxRule(JAN,TaxSettings(D('4'),D('5'),confirmed_by_user=True)),
               TaxRule(JUL,TaxSettings(D('6'),D('7'),confirmed_by_user=True))]
        ops=[sale('old',date(2026,6,30),'1050'),sale('new',JUL,'1070')]
        result,parts=calculate_with_history(ops,[Cost('a',JAN,D('400'))],rules)
        # 1050 = 1000 + 50 НДС, УСН 40; 1070 = 1000 + 70 НДС, УСН 60.
        # 2120 - 800 себестоимость - 120 НДС - 100 УСН = 1100.
        self.assertEqual(result.vat,D('120'));self.assertEqual(result.usn,D('100'))
        self.assertEqual(result.net_profit,D('1100'))
        self.assertEqual([part.quarter for part in parts],[2,3])
    def test_future_rule_never_changes_past_period(self):
        original=TaxRule(JAN,TaxSettings(D('4'),D('5'),confirmed_by_user=True))
        future=TaxRule(date(2027,1,1),TaxSettings(D('20'),D('22'),confirmed_by_user=True))
        op=sale('1',JAN,'1050');costs=[Cost('a',JAN,D('400'))]
        self.assertEqual(calculate_with_history([op],costs,[original])[0],calculate_with_history([op],costs,[original,future])[0])
        self.assertEqual(rule_at([original,future],JUL),original)
    def test_missing_or_unconfirmed_dates_block_complete_profit(self):
        for rules in [[],[TaxRule(JUL,TaxSettings(D('4'),D('5'),confirmed_by_user=True))],
                      [TaxRule(JAN,TaxSettings(D('4'),D('5'),confirmed_by_user=False))]]:
            result,_=calculate_with_history([sale('1',JAN,'1050')],[Cost('a',JAN,D('400'))],rules)
            self.assertIsNone(result.net_profit);self.assertIsNone(result.usn);self.assertIsNone(result.vat)
    def test_return_does_not_create_unverified_tax_refund(self):
        returned=Operation('shop','report','return',JUL,Kind.RETURN,D('-1050'),'a',-1,original_sale_id='1',cost_basis_date=JAN)
        result,_=calculate_with_history([returned],[Cost('a',JAN,D('400'))],
                                       [TaxRule(JAN,TaxSettings(D('4'),D('5'),confirmed_by_user=True))])
        self.assertEqual(result.cost,D('-400'))
        self.assertIsNone(result.usn);self.assertIsNone(result.vat);self.assertIsNone(result.net_profit)
    def test_conflicting_dates_are_not_arbitrarily_selected(self):
        with self.assertRaises(ValueError):
            rule_at([TaxRule(JAN,TaxSettings(D('4'),D('5'))),TaxRule(JAN,TaxSettings(D('6'),D('7')))],JAN)
    def test_settings_preserve_other_dates_and_audit_replacements(self):
        with tempfile.TemporaryDirectory() as folder:
            path=Path(folder)/'db.sqlite';ledger=Ledger(path)
            old=TaxRule(JAN,TaxSettings(D('4'),D('5'),confirmed_by_user=True))
            future=TaxRule(JUL,TaxSettings(D('6'),D('7'),confirmed_by_user=True))
            save_rule(ledger,'shop',old);save_rule(ledger,'shop',future);save_rule(ledger,'shop',future)
            self.assertEqual(ledger.setting('shop','tax_history_revisions'),[])
            corrected=TaxRule(JUL,TaxSettings(D('5'),D('7'),confirmed_by_user=True))
            save_rule(ledger,'shop',corrected)
            self.assertEqual(len(ledger.setting('shop','tax_history_revisions')),1)
            ledger.close();ledger=Ledger(path)
            self.assertEqual(load_rules(ledger,'shop'),[old,corrected]);self.assertEqual(load_rules(ledger,'other'),[])
            ledger.close()
    def test_legacy_settings_have_no_invented_effective_date(self):
        ledger=Ledger(':memory:');ledger.set_setting('shop','taxes',{'usn':'4','vat':'5','included':True,'confirmed':True})
        self.assertEqual(load_rules(ledger,'shop'),[])
        ledger.close()

    def test_uncertain_income_classification_does_not_show_full_tax_total(self):
        uncertain=Operation('shop','report','comp',JAN,Kind.COMPENSATION,D('100'))
        result,parts=calculate_with_history([sale('1',JAN,'1050'),uncertain],[Cost('a',JAN,D('400'))],
                                          [TaxRule(JAN,TaxSettings(D('4'),D('5'),confirmed_by_user=True))])
        self.assertEqual(parts[0].usn,D('40'))
        self.assertIsNone(result.usn);self.assertIsNone(result.vat);self.assertIsNone(result.net_profit)
    def test_price_excluding_vat_is_not_assumed_for_wb_gross_amount(self):
        result,_=calculate_with_history([sale('1',JAN,'1050')],[Cost('a',JAN,D('400'))],
                                       [TaxRule(JAN,TaxSettings(D('4'),D('5'),vat_included=False,confirmed_by_user=True))])
        self.assertIsNone(result.net_profit);self.assertIsNone(result.vat)

    def test_long_unconfigured_history_reports_missing_dates_compactly(self):
        from datetime import timedelta
        operations=[sale(str(i),JAN+timedelta(days=i),'1050') for i in range(200)]
        result,_=calculate_with_history(operations,[Cost('a',JAN,D('400'))],[])
        self.assertIsNone(result.net_profit)
        self.assertLess(len(result.tax_limitations),10)
        self.assertTrue(any('200 дат' in issue for issue in result.tax_limitations))
