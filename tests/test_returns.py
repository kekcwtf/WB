import json
import tempfile
import unittest
from dataclasses import replace
from datetime import date
from decimal import Decimal as D
from pathlib import Path
from wb_profit.returns import resolve_returns
from wb_profit.finance import Cost, Kind, Operation, TaxSettings, calculate
from wb_profit.storage import Ledger
from wb_profit.sync import SyncStore, Page, Record
from wb_profit.sync_service import rebuild_finance

SALE=date(2026,1,1)
RETURN=date(2026,2,1)


def financial(identifier,reason='Продажа',day=SALE,quantity=1,**overrides):
    row={'reportId':1,'rrdId':identifier,'rrDate':day.isoformat(),'currency':'RUB',
         'nmId':42,'sku':'barcode','srid':'order-1','sellerOperName':reason,'docTypeName':reason,
         'quantity':quantity,'retailAmount':'1000','forPay':'900','vw':'80','vwNds':'16',
         'acquiringFee':'4','deliveryService':'0','paidStorage':'0','paidAcceptance':'0','penalty':'0',
         'deduction':'0','deliveryMethod':'FBW','additionalPayment':'0','rebillLogisticCost':'0',
         'installmentCofinancingAmount':'0','cashbackAmount':'0','cashbackDiscount':'0','cashbackCommissionChange':'0'}
    row.update(overrides)
    return row


class ReturnTests(unittest.TestCase):
    def test_unambiguous_link_partial_returns_and_quantity_cap(self):
        sale=financial(1,quantity=2)
        returns=[financial(2,'Возврат',RETURN),financial(3,'Возврат',date(2026,3,1))]
        links=resolve_returns([sale,*returns])
        self.assertEqual(set(links.linked),{'1:2','1:3'})
        self.assertEqual(links.linked['1:2'].sale_identifier,'1:1:revenue')
        self.assertEqual(links.linked['1:2'].sale_date,SALE)
        self.assertEqual(links.issues,{})
        excessive=resolve_returns([sale,*returns,financial(4,'Возврат',date(2026,4,1))])
        self.assertFalse(excessive.linked)
        self.assertEqual(len(excessive.issues),3)

    def test_missing_or_ambiguous_origin_never_uses_nearest_sale(self):
        returned=financial(3,'Возврат',RETURN)
        for rows in [[returned],[financial(1),financial(2),returned],
                     [financial(1),{**returned,'sku':'another'}],
                     [financial(1),{**returned,'srid':''}],
                     [financial(1),{**returned,'deliveryMethod':'FBS'}],
                     [financial(1,day=date(2026,3,1)),returned]]:
            with self.subTest(rows=[r['rrdId'] for r in rows]):
                result=resolve_returns(rows)
                self.assertFalse(result.linked)
                self.assertTrue(result.issues)

    def test_unlinked_return_does_not_restore_current_cost(self):
        returned=Operation('s','wb_finance','return',RETURN,Kind.RETURN,D('-1000'),'42:barcode',-1)
        result=calculate([returned],[Cost('42:barcode',SALE,D('400')),Cost('42:barcode',date(2026,1,15),D('700'))],None)
        self.assertEqual(result.cost,D('0'))
        self.assertEqual(result.missing_costs,('42:barcode',))
        self.assertIsNone(result.net_profit)

    def test_original_cost_is_restored_even_outside_selected_period(self):
        returned=Operation('s','wb_finance','return',RETURN,Kind.RETURN,D('-1000'),'42:barcode',-1,
                           original_sale_id='1:1:revenue',cost_basis_date=SALE)
        costs=[Cost('42:barcode',SALE,D('400')),Cost('42:barcode',date(2026,1,15),D('700'))]
        result=calculate([returned],costs,TaxSettings(D('4'),D('5'),confirmed_by_user=True))
        self.assertEqual(result.cost,D('-400'))
        self.assertEqual(result.result_before_tax,D('-600'))
        self.assertEqual(result.missing_costs,())
        self.assertIsNone(result.net_profit) # налоговый вычет прошлого периода не придумываем
        changed=calculate([returned],[Cost('42:barcode',SALE,D('350')),*costs[1:]],None)
        self.assertEqual(changed.cost,D('-350'))
        with self.assertRaises(ValueError):replace(returned,cost_basis_date=date(2027,1,1))

    def test_database_round_trip_repeat_rebuild_and_removed_origin(self):
        with tempfile.TemporaryDirectory() as folder:
            path=Path(folder)/'db.sqlite'
            storage=SyncStore(path)
            sale,returned=financial(1),financial(2,'Возврат',RETURN)
            storage.save_page('shop','finance_weekly',Page((Record('1:1',sale),Record('1:2',returned)),None,True))
            rebuild_finance(path,'shop')
            ledger=Ledger(path)
            operations=ledger.operations('shop',RETURN,RETURN)
            linked=next(op for op in operations if op.kind==Kind.RETURN)
            self.assertEqual(linked.cost_basis_date,SALE)
            self.assertEqual(linked.original_sale_id,'1:1:revenue')
            before=ledger.db.execute('SELECT COUNT(*) FROM revisions').fetchone()[0]
            rebuild_finance(path,'shop')
            self.assertEqual(ledger.db.execute('SELECT COUNT(*) FROM revisions').fetchone()[0],before)
            self.assertEqual(ledger.operations('other',date.min,date.max),[])
            # Полный новый снимок удалил исходную продажу: старой ссылкой пользоваться нельзя.
            storage.save_page('shop','finance_weekly',Page((Record('1:2',returned),),None,True,snapshot=True))
            rebuild_finance(path,'shop')
            unlinked=next(op for op in ledger.operations('shop',RETURN,RETURN) if op.kind==Kind.RETURN)
            self.assertIsNone(unlinked.cost_basis_date)
            self.assertIn('1:2',ledger.setting('shop','return_link_issues'))
            ledger.close();storage.close()

    def test_sale_and_later_return_conserve_cost_despite_price_history(self):
        sale=Operation('s','wb_finance','sale',SALE,Kind.SALE,D('1000'),'a',1)
        returned=Operation('s','wb_finance','return',RETURN,Kind.RETURN,D('-1000'),'a',-1,
                           original_sale_id='sale',cost_basis_date=SALE)
        sale_fees=Operation('s','wb_finance','sale_fee',SALE,Kind.COMMISSION,D('-100'),'a')
        return_fees=Operation('s','wb_finance','return_fee',RETURN,Kind.COMMISSION,D('100'),'a')
        outbound=Operation('s','wb_finance','outbound',SALE,Kind.LOGISTICS,D('-50'),'a')
        inbound=Operation('s','wb_finance','inbound',RETURN,Kind.REVERSE_LOGISTICS,D('-30'),'a')
        costs=[Cost('a',SALE,D('400')),Cost('a',date(2026,1,15),D('900'))]
        first=calculate([sale,sale_fees,outbound],costs,None)
        second=calculate([returned,return_fees,inbound],costs,None)
        whole=calculate([sale,returned,sale_fees,return_fees,outbound,inbound],costs,None)
        self.assertEqual(first.result_before_tax,D('450'))
        self.assertEqual(second.result_before_tax,D('-530'))
        self.assertEqual(whole.cost,D('0'))
        self.assertEqual(whole.result_before_tax,D('-80'))
        self.assertEqual(first.result_before_tax+second.result_before_tax,whole.result_before_tax)

    def test_return_only_period_does_not_recommend_price_change(self):
        from wb_profit.analytics import analyse_sku
        returned=Operation('s','wb_finance','return',RETURN,Kind.RETURN,D('-1000'),'a',-1,
                           original_sale_id='sale',cost_basis_date=SALE)
        result=analyse_sku('a',[returned],[Cost('a',SALE,D('400'))])
        self.assertFalse(result.incomplete)
        self.assertIn('не доказывает убыточность',result.explanation)
        self.assertEqual(result.recommendations,())
        self.assertIsNone(result.profit_per_unit_before_tax)

    def test_upgrade_rebuilds_complete_cache_without_network(self):
        from wb_profit.sync_service import ensure_cached_normalization,NORMALIZATION_VERSION
        with tempfile.TemporaryDirectory() as folder:
            path=Path(folder)/'db.sqlite'
            storage=SyncStore(path)
            storage.save_page('s','finance_weekly',Page((Record('1:1',financial(1)),Record('1:2',financial(2,'Возврат',RETURN))),None,True))
            ledger=Ledger(path)
            self.assertEqual(ledger.operations('s',date.min,date.max),[])
            ensure_cached_normalization(path,'s')
            linked=next(op for op in ledger.operations('s',RETURN,RETURN) if op.kind==Kind.RETURN)
            self.assertEqual(linked.cost_basis_date,SALE)
            self.assertEqual(ledger.setting('s','normalization_version'),NORMALIZATION_VERSION)
            count=ledger.db.execute('SELECT COUNT(*) FROM revisions').fetchone()[0]
            ensure_cached_normalization(path,'s')
            self.assertEqual(ledger.db.execute('SELECT COUNT(*) FROM revisions').fetchone()[0],count)
            ledger.close();storage.close()

    def test_partial_cache_does_not_replace_previous_ledger_on_upgrade(self):
        from wb_profit.sync_service import ensure_cached_normalization
        with tempfile.TemporaryDirectory() as folder:
            path=Path(folder)/'db.sqlite';storage=SyncStore(path);ledger=Ledger(path)
            original=Operation('s','wb_finance','old',SALE,Kind.SALE,D('1000'),'a',1)
            ledger.upsert([original])
            storage.save_page('s','finance_weekly',Page((Record('1:2',financial(2,'Возврат',RETURN)),),'partial'))
            ensure_cached_normalization(path,'s')
            self.assertEqual(ledger.operations('s',date.min,date.max),[original])
            ledger.close();storage.close()
