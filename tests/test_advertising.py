import copy
import json
import tempfile
import unittest
from datetime import date
from decimal import Decimal as D
from pathlib import Path
from wb_profit.storage import Ledger
from wb_profit.sync import SyncStore,Page,Record
from wb_profit.ad_analytics import analyse_advertising,charge_day
from wb_profit.api.ad_statistics import AdvertisingStatistics
from wb_profit.api.ad_finances import AdCampaigns,AdAccount,AdPayments
from wb_profit.api.transport import ApiError

START=date(2026,1,1);END=date(2026,1,31)


def statistics(identifier=7,spend='120',when='2026-01-05',currency='RUB'):
    cost=D(spend)
    return {'advertId':identifier,'sum':cost,'sum_price':D('800'),'currency':currency,
            'days':[{'date':when+'T00:00:00Z','sum':cost,'sum_price':D('800'),'views':100,'clicks':10,'orders':2,
                     'apps':[{'appType':1,'sum':cost,'nms':[{'nmId':101,'sum':cost}]}]}]}


def cached(value):
    return {'request':{'advertId':value['advertId'],'from':'2026-01-01','to':'2026-01-31'},'data':value}


class Client:
    def __init__(self,responses):self.responses=iter(responses);self.calls=[]
    async def request(self,*args,**kwargs):self.calls.append((args,kwargs));return next(self.responses)


class AdvertisingTests(unittest.TestCase):
    def setUp(self):
        self.folder=tempfile.TemporaryDirectory();self.path=Path(self.folder.name)/'db.sqlite'
        self.storage=SyncStore(self.path);self.ledger=Ledger(self.path)
    def tearDown(self):self.storage.close();self.ledger.close();self.folder.cleanup()
    def put(self,source,rows):
        self.storage.save_page('shop',source,Page(tuple(Record(str(i),row) for i,row in enumerate(rows)),None,True,snapshot=True))
    def fixture(self):
        self.put('ad_account',[{'balance':D('2000'),'net':D('1500'),'bonus':D('50'),'currency':'RUB'}])
        self.put('advertising',[{'advertId':7,'updSum':200,'updTime':'2026-01-05T12:00:00+03:00','paymentType':'Баланс','updNum':4}])
        self.put('ad_payments',[{'id':1,'date':'2026-01-05T12:00:00Z','sum':1000,'currency':'RUB','statusId':1,'type':0},
                                {'id':2,'date':'2026-01-05T12:00:00Z','sum':500,'currency':'RUB','statusId':0,'type':0},
                                {'id':3,'date':'2026-01-05T12:00:00Z','sum':300,'currency':'RUB','statusId':1,'type':3,'cardStatus':'pending'}])
        first=statistics()
        first['days'][0]['apps']=[{'appType':1,'sum':D('70'),'nms':[{'nmId':101,'sum':D('70')}]},
                                  {'appType':32,'sum':D('50'),'nms':[{'nmId':102,'sum':D('50')}]}]
        second=statistics(8,'80','2026-01-06');second['days'][0]['orders']=0
        self.put('ad_statistics',[cached(first),cached(second)])
        for source in ['advertising','ad_statistics']:
            self.ledger.set_setting('shop','source_coverage:'+source,[['2026-01-01','2026-01-31']])
    def test_hierarchy_is_counted_once_and_funding_is_separate(self):
        self.fixture();report=analyse_advertising(self.ledger,'shop',START,END)
        self.assertEqual(report.billed,D('200'))
        self.assertEqual(report.consumed,D('200')) # не 400/600 от повторного суммирования иерархии
        self.assertEqual(report.funding,D('1000')) # ошибки и ожидание карты не включены
        self.assertEqual(report.model_spend,{'101':D('150'),'102':D('50')})
        self.assertEqual(report.unallocated,D('0'))
        self.assertEqual(report.drr(D('2000')),D('10'))
        self.assertIsNone(report.drr(D('0')))
    def test_empty_data_is_not_zero_spend(self):
        report=analyse_advertising(self.ledger,'shop',START,END)
        self.assertIsNone(report.billed);self.assertIsNone(report.consumed);self.assertIsNone(report.funding)
        self.assertFalse(report.statistics_complete)
    def test_undated_or_naive_charges_do_not_get_arbitrary_period(self):
        self.fixture()
        self.put('advertising',[{'advertId':7,'updSum':200,'updTime':'2026-01-05T12:00:00','paymentType':'Баланс','updNum':4}])
        report=analyse_advertising(self.ledger,'shop',START,END)
        self.assertIsNone(report.billed);self.assertEqual(report.undated_billed,D('200'))
        self.assertEqual(charge_day('2026-01-04T22:30:00Z'),date(2026,1,5))
        self.assertIsNone(charge_day('2026-01-05T12:00:00'))
    def test_currency_mismatch_blocks_ruble_ratios(self):
        self.fixture();self.put('ad_account',[{'balance':0,'net':0,'bonus':0,'currency':'EUR'}])
        report=analyse_advertising(self.ledger,'shop',START,END)
        self.assertIsNone(report.currency);self.assertIsNone(report.consumed);self.assertIsNone(report.drr(D('2000')))
    def test_unallocated_cost_is_not_invented_for_a_size(self):
        self.fixture()
        row=statistics();row['days'][0]['apps'][0]['nms'][0]['sum']=D('119')
        self.put('ad_statistics',[cached(row)])
        report=analyse_advertising(self.ledger,'shop',START,END)
        self.assertEqual(report.unallocated,D('1'))
        self.assertEqual(report.model_spend,{'101':D('119')})
        self.assertTrue(any('не сверены' in issue for issue in report.issues))
    def test_missing_campaign_is_not_a_zero_cost_campaign(self):
        self.fixture();self.put('ad_statistics',[{'request':{'advertId':7,'from':'2026-01-01','to':'2026-01-31'},'data':None}])
        report=analyse_advertising(self.ledger,'shop',START,END)
        self.assertIsNone(report.consumed);self.assertIsNone(report.drr(D('2000')))
        self.assertEqual(report.campaigns,())
    def test_matching_sum_never_creates_a_second_profit_deduction(self):
        from wb_profit.finance import Operation,Kind,calculate
        from wb_profit.coverage import period_coverage
        self.fixture()
        original=[Operation('shop','wb_finance','sale',date(2026,1,5),Kind.SALE,D('2000'),'a',1),
                  Operation('shop','wb_finance','deduction',date(2026,1,5),Kind.DEDUCTION,D('-200'))]
        self.ledger.upsert(original)
        before=calculate(original,[],None).result_before_tax
        analyse_advertising(self.ledger,'shop',START,END)
        after=self.ledger.operations('shop',START,END)
        self.assertEqual(calculate(after,[],None).result_before_tax,before)
        self.assertEqual(before,D('1800'))
        self.assertFalse(period_coverage(self.ledger,'shop',START,END)[0])
    def test_consumption_without_invoice_does_not_confirm_net_profit(self):
        from wb_profit.coverage import period_coverage
        self.fixture();self.put('advertising',[])
        covered,reasons=period_coverage(self.ledger,'shop',START,END)
        self.assertFalse(covered)
        self.assertTrue(any('Потребление рекламы' in reason for reason in reasons))


    def test_partial_billing_refresh_invalidates_old_full_coverage_claim(self):
        self.fixture()
        with self.storage.db:
            self.storage.db.execute("UPDATE sync_state SET completed=0,status='running' WHERE store='shop' AND source='advertising'")
        self.assertIsNone(analyse_advertising(self.ledger,'shop',START,END).billed)


class AdvertisingAdapterTests(unittest.IsolatedAsyncioTestCase):
    async def test_50_id_batch_limit_and_checkpoint_resume(self):
        first=Client([[statistics(i,'1') for i in range(1,51)]])
        source=AdvertisingStatistics(first,list(range(1,52)),START,END)
        generator=source.pages(None);page=await anext(generator);await generator.aclose()
        self.assertFalse(page.completed)
        self.assertEqual(len(first.calls[0][1]['params']['ids'].split(',')),50)
        second=Client([[statistics(51,'1')]])
        pages=[p async for p in AdvertisingStatistics(second,list(range(1,52)),START,END).pages(page.next_cursor)]
        self.assertEqual(second.calls[0][1]['params']['ids'],'51');self.assertTrue(pages[-1].completed)
    async def test_monthly_windows_and_unavailable_campaign(self):
        client=Client([[statistics()],[]])
        pages=[p async for p in AdvertisingStatistics(client,[7],START,date(2026,2,2)).pages(None)]
        self.assertEqual(client.calls[0][1]['params']['endDate'],'2026-01-31')
        self.assertEqual(client.calls[1][1]['params']['beginDate'],'2026-02-01')
        self.assertIsNone(pages[1].records[0].payload['data'])
    async def test_changed_and_removed_statistics_replaces_cached_value(self):
        with tempfile.TemporaryDirectory() as folder:
            storage=SyncStore(Path(folder)/'db.sqlite')
            for response in [[statistics(spend='10')],[statistics(spend='20')],[]]:
                for page in [p async for p in AdvertisingStatistics(Client([response]),[7],START,END).pages(None)]:
                    storage.save_page('shop','ad_statistics',page)
            rows=storage.db.execute("SELECT payload FROM source_records WHERE source='ad_statistics'").fetchall()
            self.assertEqual(len(rows),1);self.assertIsNone(json.loads(rows[0][0])['data'])
            storage.close()
    async def test_account_payments_and_campaigns_are_distinct_sources(self):
        account=[p async for p in AdAccount(Client([{'balance':1000,'net':200,'bonus':50,'currency':'RUB'}])).pages(None)]
        self.assertEqual(account[0].records[0].payload['balance'],1000)
        payments=[{'id':1,'date':'2026-01-05T00:00:00Z','sum':1000,'currency':'RUB','type':0,'statusId':1}]
        page=[p async for p in AdPayments(Client([payments]),START,END).pages(None)][0]
        self.assertEqual(page.records[0].identifier,'2026-01:1')
        campaigns=[p async for p in AdCampaigns(Client([{'adverts':None,'all':0}])).pages(None)]
        self.assertTrue(campaigns[0].snapshot)
    async def test_out_of_window_and_duplicate_campaign_data_are_rejected(self):
        for response in [[statistics(when='2026-02-01')],[statistics(),statistics()]]:
            with self.assertRaises(ApiError):
                _=[p async for p in AdvertisingStatistics(Client([response]),[7],START,END).pages(None)]

class AdvertisingCoverageRegressionTests(unittest.TestCase):
    def test_old_partial_campaign_cache_does_not_become_complete_from_other_campaigns(self):
        with tempfile.TemporaryDirectory() as folder:
            path=Path(folder)/'db.sqlite';storage=SyncStore(path);ledger=Ledger(path)
            row=cached(statistics());row['request']['to']='2026-01-15'
            storage.save_page('shop','ad_statistics',Page((Record('2026-01:7',row),),None,True))
            ledger.set_setting('shop','source_coverage:ad_statistics',[['2026-01-01','2026-01-31']])
            report=analyse_advertising(ledger,'shop',START,END)
            self.assertFalse(report.statistics_complete);self.assertIsNone(report.drr(D('2000')))
            storage.close();ledger.close()

class AdvertisingSchemaValidationTests(unittest.IsolatedAsyncioTestCase):
    async def test_unknown_currency_and_inconsistent_campaign_list_are_rejected(self):
        with self.assertRaises(ApiError):
            _=[p async for p in AdAccount(Client([{'balance':0,'net':0,'bonus':0,'currency':''}])).pages(None)]
        with self.assertRaises(ApiError):
            _=[p async for p in AdCampaigns(Client([{'all':2,'adverts':[]}])).pages(None)]

class BusinessDayTests(unittest.TestCase):
    def test_today_uses_moscow_day_independent_of_host_timezone(self):
        from datetime import datetime,timezone
        from wb_profit.business_dates import business_today
        self.assertEqual(business_today(datetime(2026,1,4,22,30,tzinfo=timezone.utc)),date(2026,1,5))
