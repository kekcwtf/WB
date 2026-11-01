import json
import unittest
import base64
from datetime import date
from decimal import Decimal as D
from wb_profit.api.auth import inspect_token
from wb_profit.api.financial_reports import FinancialReports
from wb_profit.api.orders import Orders
from wb_profit.api.products import Products
from wb_profit.api.tariffs import Tariffs
from wb_profit.normalization import financial_row


def row(**overrides):
    result={'reportId':1,'rrdId':1,'rrDate':'2026-01-01','currency':'RUB','nmId':42,'sku':'123',
            'sellerOperName':'Продажа','docTypeName':'Продажа','quantity':1,'retailAmount':'1050',
            'forPay':'930','vw':'80','vwNds':'16','acquiringFee':'24','deliveryService':'50',
            'paidStorage':'0','paidAcceptance':'0','penalty':'0','deduction':'0','deliveryMethod':'FBW',
            'additionalPayment':'0','rebillLogisticCost':'0','installmentCofinancingAmount':'0',
            'cashbackAmount':'0','cashbackDiscount':'0','cashbackCommissionChange':'0'}
    result.update(overrides)
    return result


class NormalizeTests(unittest.TestCase):
    def test_embedded_fees_are_not_deducted_twice(self):
        normalized=financial_row('s',row())
        self.assertEqual(normalized.issues,())
        self.assertEqual(sum((op.amount for op in normalized.operations),D('0')),D('880'))
        self.assertEqual(len([op for op in normalized.operations if op.kind=='commission']),1)

    def test_residual_explained_without_hiding_it_in_commission(self):
        normalized=financial_row('s',row(forPay='940'))
        self.assertEqual(sum((op.amount for op in normalized.operations),D('0')),D('890'))
        self.assertTrue(normalized.issues)
        self.assertTrue(any(op.kind=='unknown' for op in normalized.operations))
        self.assertEqual(next(op.amount for op in normalized.operations if 'residual' in op.identifier),D('10'))

    def test_return_and_unknown_fields(self):
        normalized=financial_row('s',row(sellerOperName='Возврат',docTypeName='Возврат'))
        self.assertEqual(sum((op.amount for op in normalized.operations),D('0')),D('-980'))
        self.assertEqual(next(op.units for op in normalized.operations if op.kind=='return'),-1)
        data=row();del data['acquiringFee']
        self.assertTrue(financial_row('s',data).issues)
        with self.assertRaises(ValueError):
            financial_row('s',row(currency='USD'))

    def test_local_rights_are_only_claims(self):
        payload=base64.urlsafe_b64encode(json.dumps({'s':(1<<13)|(1<<5),'acc':3,'exp':4102444800,'t':False}).encode()).decode().rstrip('=')
        claims=inspect_token('e30.'+payload+'.signature')
        self.assertTrue(claims.permits('finance'))
        self.assertFalse(claims.permits('promotion'))
        self.assertTrue(claims.fast_limits)
        with self.assertRaises(ValueError):inspect_token('not-a-token')


class FakeClient:
    def __init__(self,responses):self.responses=iter(responses);self.calls=[]
    async def request(self,*args,**kwargs):self.calls.append((args,kwargs));return next(self.responses)


class AdapterTests(unittest.IsolatedAsyncioTestCase):
    async def test_new_finance_endpoint_and_pagination(self):
        client=FakeClient([[row()],None])
        pages=[p async for p in FinancialReports(client,date(2026,1,1),date(2026,1,7)).pages(None)]
        self.assertEqual(client.calls[0][0],('POST','/api/finance/v1/sales-reports/detailed'))
        self.assertEqual(client.calls[1][1]['body']['rrdId'],1)
        self.assertEqual(pages[0].records[0].identifier,'1:1')
        self.assertTrue(pages[-1].completed)

    async def test_operational_orders_cursor(self):
        client=FakeClient([[{'srid':'s1','lastChangeDate':'2026-01-01T10:00:00'}],[]])
        pages=[p async for p in Orders(client,date(2026,1,1)).pages(None)]
        self.assertEqual(client.calls[1][1]['params']['dateFrom'],'2026-01-01T10:00:00')
        self.assertTrue(pages[-1].completed)

    async def test_cards_and_fbw_tariff(self):
        client=FakeClient([{'cards':[{'nmID':42}],'cursor':{'total':1,'updatedAt':'2026-01-01','nmID':42}}])
        self.assertTrue([p async for p in Products(client).pages(None)][0].completed)
        client=FakeClient([{'report':[{'subjectID':1,'paidStorageKgvp':D('24'),'kgvpMarketplace':D('30')}]}])
        page=[p async for p in Tariffs(client).pages(None)][0]
        self.assertEqual(page.records[0].payload['paidStorageKgvp'],D('24'))

class ServiceTests(unittest.IsolatedAsyncioTestCase):
    async def test_end_to_end_download_normalization_cache_and_snapshot(self):
        import tempfile
        from pathlib import Path
        from wb_profit.sync_service import synchronize_shop
        from wb_profit.storage import Ledger
        from wb_profit.data_views import stock_rows
        payload=base64.urlsafe_b64encode(json.dumps({'s':sum(1<<b for b in [1,2,5,6,13]),'acc':3,'exp':4102444800}).encode()).decode().rstrip('=')
        token='e30.'+payload+'.signature'
        calls=[]
        seller=['seller-1']
        class Client:
            def __init__(self,host,provider,interval):self.host=host;self.finance_read=False
            async def close(self):pass
            async def request(self,method,path,**kwargs):
                calls.append((method,path,kwargs))
                if path=='/api/v1/seller-info':return {'sid':seller[0],'name':'Магазин','country':'ru'}
                if path.endswith('/sales-reports/detailed'):
                    if self.finance_read:return None
                    self.finance_read=True
                    return [row()]
                if path.endswith('/sales-reports/list'):return [{'reportId':1,'currency':'RUB','bankPaymentSum':'880'}]
                if path.endswith('/supplier/orders'):return []
                if path.endswith('/supplier/sales'):return []
                if path.endswith('/cards/list'):return {'cards':[{'nmID':42,'title':'Товар'}],'cursor':{'total':1,'updatedAt':'2026-01-01T00:00:00Z','nmID':42}}
                if path.endswith('/upd'):return []
                if path.endswith('/balance'):return {'currency':'RUB','balance':0,'net':0,'bonus':0}
                if path.endswith('/payments'):return []
                if path.endswith('/promotion/count'):return {'all':1,'adverts':[{'status':9,'type':9,'advert_list':[{'advertId':7}]}]}
                if path.endswith('/fullstats'):
                    return [{'advertId':7,'currency':'RUB','sum':'1','sum_price':'10','days':[{'date':kwargs['params']['beginDate']+'T00:00:00Z','sum':'1','sum_price':'10','views':10,'clicks':2,'orders':1,'apps':[{'sum':'1','nms':[{'nmId':42,'sum':'1'}]}]}]}]
                if path.endswith('/commission'):return {'report':[{'subjectID':1,'paidStorageKgvp':24}]}
                if path.endswith('/warehouse_remains'):return {'data':{'taskId':'task-1'}}
                if path.endswith('/status'):return {'data':{'status':'done'}}
                if path.endswith('/download'):return [{'nmId':42,'barcode':'123','warehouses':[
                    {'warehouseName':'Всего находится на складах','quantity':10},
                    {'warehouseName':'Коледино','quantity':10},
                    {'warehouseName':'В пути до получателей','quantity':2}]}]
                raise AssertionError(path)
        with tempfile.TemporaryDirectory() as folder:
            path=Path(folder)/'db.sqlite'
            messages=[]
            await synchronize_shop(path,'shop',token,lambda *args:messages.append(args),client_factory=Client)
            ledger=Ledger(path)
            ops=ledger.operations('shop',date.min,date.max)
            self.assertEqual(sum((op.amount for op in ops),D('0')),D('880'))
            self.assertEqual(ledger.setting('shop','normalization_issues'),[])
            self.assertFalse(ledger.setting('shop','coverage_confirmed'))
            self.assertTrue(ledger.setting('shop','report_audits')[0]['verified'])
            self.assertEqual(ledger.setting('shop','source_message:ad_statistics'),'Успешно')
            self.assertGreater(ledger.db.execute("SELECT COUNT(*) FROM source_records WHERE source='ad_statistics'").fetchone()[0],0)
            self.assertEqual(stock_rows(ledger,'shop',date(2026,1,1))[0][4],10)
            count=len(calls)
            await synchronize_shop(path,'shop',token,lambda *args:messages.append(args),client_factory=Client)
            self.assertEqual(len(calls),count)
            self.assertEqual(ledger.operations('shop',date.min,date.max),ops)
            await synchronize_shop(path,'shop',token,lambda *args:messages.append(args),client_factory=Client,force=True)
            self.assertGreater(len(calls),count)
            self.assertEqual(ledger.operations('shop',date.min,date.max),ops)
            seller[0]='seller-2'
            from wb_profit.api.transport import ApiError
            with self.assertRaisesRegex(ApiError,'другому продавцу'):
                await synchronize_shop(path,'shop',token+'different',lambda *args:None,client_factory=Client)
            self.assertEqual(ledger.operations('shop',date.min,date.max),ops)
            ledger.close()

class ReportAuditTests(unittest.TestCase):
    def test_independent_report_totals_detect_discrepancy_and_missing_reports(self):
        from wb_profit.report_audit import audit_reports
        self.assertTrue(audit_reports('s',[row()],[{'reportId':1,'currency':'RUB','bankPaymentSum':'880'}])[0].verified)
        wrong=audit_reports('s',[row()],[{'reportId':1,'currency':'RUB','bankPaymentSum':'900'}])[0]
        self.assertFalse(wrong.verified)
        self.assertEqual(wrong.difference,D('-20'))
        self.assertFalse(audit_reports('s',[row()],[])[0].verified)
        self.assertFalse(audit_reports('s',[],[{'reportId':1,'currency':'RUB','bankPaymentSum':'0'}])[0].verified)

class AdvertisingCorrectionTests(unittest.IsolatedAsyncioTestCase):
    async def test_changed_amount_and_removed_duplicate_do_not_accumulate(self):
        from wb_profit.api.advertising import Advertising
        from wb_profit.sync import SyncStore
        initial={'updNum':0,'updTime':None,'updSum':10,'advertId':7,'campName':'Кампания','paymentType':'Баланс'}
        source=Advertising(FakeClient([[initial,initial]]),date(2026,1,15),date(2026,1,31))
        storage=SyncStore(':memory:')
        for page in [p async for p in source.pages(None)]:storage.save_page('shop',source.name,page)
        self.assertEqual(storage.db.execute('SELECT COUNT(*) FROM source_records').fetchone()[0],2)
        corrected={**initial,'updSum':15}
        source=Advertising(FakeClient([[corrected]]),date(2026,1,20),date(2026,1,31))
        for page in [p async for p in source.pages(None)]:storage.save_page('shop',source.name,page)
        rows=storage.db.execute('SELECT payload FROM source_records').fetchall()
        self.assertEqual(len(rows),1)
        self.assertEqual(json.loads(rows[0][0])['updSum'],15)
        storage.close()

class SchemaChangeTests(unittest.TestCase):
    def test_new_financial_field_blocks_silent_profit(self):
        changed=financial_row('s',row(newUnknownFee='100'))
        self.assertTrue(any('поля' in issue for issue in changed.issues))
        self.assertTrue(any(op.kind=='unknown' for op in changed.operations))

class ProductIncrementalTests(unittest.IsolatedAsyncioTestCase):
    async def test_successful_cursor_is_reused_and_not_lost_on_empty_delta(self):
        cursor=json.dumps({'limit':100,'updatedAt':'2026-01-01T12:00:00Z','nmID':42})
        client=FakeClient([{'cards':[],'cursor':{'total':0}}])
        pages=[p async for p in Products(client,initial_cursor=cursor).pages(None)]
        self.assertEqual(client.calls[0][1]['body']['settings']['cursor']['nmID'],42)
        self.assertEqual(json.loads(pages[0].next_cursor)['updatedAt'],'2026-01-01T12:00:00Z')

    async def test_nonempty_cards_require_a_usable_next_update_cursor(self):
        from wb_profit.api.transport import ApiError
        client=FakeClient([{'cards':[{'nmID':42}],'cursor':{'total':1}}])
        with self.assertRaises(ApiError):
            _=[p async for p in Products(client).pages(None)]
