import tempfile
from pathlib import Path
import unittest
from unittest.mock import patch
from wb_profit.storage import Ledger
from wb_profit.sync import SyncStore
from wb_profit.sync_service import synchronize_shop,SyncWorker
from wb_profit.retries import PREFIX,save_failure,plans
from wb_profit.api.transport import ApiError
from wb_profit.business_dates import business_today
from test_inventory_sync import stats_token


class SyncRecoveryTests(unittest.IsolatedAsyncioTestCase):
    async def test_missing_token_pauses_retries_without_prompt_or_api_calls(self):
        with tempfile.TemporaryDirectory() as folder:
            path=Path(folder)/'db.sqlite';ledger=Ledger(path)
            ledger.set_setting('s','costs',[{'sku':'a','date':'2026-01-01','amount':'940'}])
            save_failure(ledger,'s','sales',ApiError('offline',retryable=True,kind='network'))
            worker=SyncWorker(path,['s'],retry_only=True)
            with patch('wb_profit.sync_service.load_token',return_value=None),patch('wb_profit.sync_service.synchronize_shop') as download:
                await worker.work()
                download.assert_not_called()
            self.assertEqual(plans(ledger,['s']),[])
            self.assertEqual(ledger.setting('s','costs')[0]['amount'],'940')
            worker.loop=worker.task=None
            ledger.close()

    async def test_only_failed_source_resumes_cursor_after_network_returns_without_force(self):
        with tempfile.TemporaryDirectory() as folder:
            path=Path(folder)/'db.sqlite'
            changed=business_today().isoformat()+'T10:00:00'
            requests=[];offline=[True]
            class Client:
                def __init__(self,*args,**kwargs):pass
                async def close(self):pass
                async def request(self,method,url,**kwargs):
                    requests.append((url,kwargs.get('params',{}).get('dateFrom')))
                    if url.endswith('/seller-info'):return {'sid':'seller','country':'ru'}
                    if url.endswith('/commission'):return {'report':[]}
                    if url.endswith('/orders'):return []
                    if url.endswith('/sales'):
                        if kwargs['params']['dateFrom']!=changed:
                            return [{'saleID':'S1','lastChangeDate':changed,'date':changed,'nmId':42,'barcode':'123','warehouseType':'Склад WB'}]
                        if offline[0]:raise ApiError('offline',retryable=True,kind='network')
                        return []
                    raise AssertionError(url)
            await synchronize_shop(path,'s',stats_token(),lambda *args:None,client_factory=Client)
            ledger=Ledger(path);cache=SyncStore(path)
            self.assertEqual(cache.state('s','sales')['cursor'],changed)
            self.assertIsNone(ledger.setting('s','source_coverage:sales'))
            plan=ledger.setting('s',PREFIX+'sales')
            self.assertEqual(plan['kind'],'network')
            requests.clear()
            await synchronize_shop(path,'s',stats_token(),lambda *args:None,client_factory=Client,retry_only=True)
            self.assertEqual(requests,[]) # локальная задержка ещё действует
            plan.update(next_at='2000-01-01T00:00:00+00:00',not_before='2000-01-01T00:00:00+00:00')
            ledger.set_setting('s',PREFIX+'sales',plan)
            offline[0]=False
            await synchronize_shop(path,'s',stats_token(),lambda *args:None,client_factory=Client,retry_only=True)
            self.assertEqual(requests,[('/api/v1/supplier/sales',changed)])
            self.assertTrue(cache.state('s','sales')['completed'])
            self.assertIsNone(ledger.setting('s',PREFIX+'sales'))
            self.assertTrue(ledger.setting('s','source_coverage:sales'))
            self.assertEqual(cache.db.execute("SELECT count(*) FROM source_records WHERE source='sales'").fetchone()[0],1)
            cache.close();ledger.close()

    async def test_permanent_auth_error_stops_automatic_source_retry(self):
        with tempfile.TemporaryDirectory() as folder:
            path=Path(folder)/'db.sqlite'
            class Client:
                def __init__(self,*args,**kwargs):pass
                async def close(self):pass
                async def request(self,method,url,**kwargs):
                    if url.endswith('/seller-info'):return {'sid':'seller','country':'ru'}
                    if url.endswith('/commission'):return {'report':[]}
                    if url.endswith('/orders'):return []
                    if url.endswith('/sales'):raise ApiError('auth',401)
                    raise AssertionError(url)
            await synchronize_shop(path,'s',stats_token(),lambda *args:None,client_factory=Client)
            ledger=Ledger(path)
            self.assertIsNone(ledger.setting('s',PREFIX+'sales'))
            self.assertEqual(ledger.setting('s','source_message:sales'),'auth')
            ledger.close()

    async def test_identity_network_failure_is_scheduled_before_any_operational_requests(self):
        with tempfile.TemporaryDirectory() as folder:
            path=Path(folder)/'db.sqlite';calls=[]
            class Client:
                def __init__(self,*args,**kwargs):pass
                async def close(self):pass
                async def request(self,method,url,**kwargs):
                    calls.append(url)
                    raise ApiError('offline',retryable=True,kind='network')
            with self.assertRaises(ApiError):
                await synchronize_shop(path,'s',stats_token(),lambda *args:None,client_factory=Client)
            ledger=Ledger(path)
            self.assertEqual(ledger.setting('s',PREFIX+'identity')['kind'],'network')
            calls.clear()
            await synchronize_shop(path,'s',stats_token(),lambda *args:None,client_factory=Client,retry_only=True)
            self.assertEqual(calls,[])
            ledger.close()
