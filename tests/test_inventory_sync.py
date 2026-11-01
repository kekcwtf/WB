import base64
from datetime import timedelta
import json
from pathlib import Path
import tempfile
import unittest
from wb_profit.api.transport import ApiError
from wb_profit.business_dates import business_today
from wb_profit.storage import Ledger
from wb_profit.sync import SyncStore,Page,Record
from wb_profit.sync_service import synchronize_shop


def stats_token():
    data=base64.urlsafe_b64encode(json.dumps({'s':1<<5,'acc':3,'exp':4102444800}).encode()).decode().rstrip('=')
    return 'e30.'+data+'.signature'


def client_for(handler):
    class Client:
        def __init__(self,*args,**kwargs):pass
        async def close(self):pass
        async def request(self,method,path,**kwargs):
            if path.endswith('/seller-info'):return {'sid':'seller','country':'ru'}
            if path.endswith('/commission'):return {'report':[]}
            if path.endswith('/orders'):return []
            if path.endswith('/sales'):return handler(kwargs['params']['dateFrom'])
            raise AssertionError(path)
    return Client


class InventorySyncTests(unittest.IsolatedAsyncioTestCase):
    async def test_partial_first_run_resumes_original_window_and_only_then_confirms_history(self):
        with tempfile.TemporaryDirectory() as folder:
            path=Path(folder)/'db.sqlite'
            today=business_today();initial=(today-timedelta(days=89)).isoformat()
            changed=(today-timedelta(days=10)).isoformat()+'T10:00:00'
            calls=[];fail=[True]
            row={'saleID':'S1','nmId':42,'barcode':'123','warehouseType':'Склад WB','date':changed,'lastChangeDate':changed}
            def handler(since):
                calls.append(since)
                if since==initial:return [row]
                if fail[0]:raise ApiError('Временный сбой')
                return []
            await synchronize_shop(path,'s',stats_token(),lambda *args:None,client_factory=client_for(handler),force=True)
            ledger=Ledger(path)
            self.assertIsNone(ledger.setting('s','source_coverage:sales'))
            self.assertEqual(ledger.setting('s','operational_window:sales'),initial)
            cache=SyncStore(path)
            self.assertEqual(cache.state('s','sales')['cursor'],changed)
            self.assertFalse(cache.state('s','sales')['completed'])
            fail[0]=False
            await synchronize_shop(path,'s',stats_token(),lambda *args:None,client_factory=client_for(handler),force=True)
            self.assertEqual(calls,[initial,changed,changed])
            self.assertEqual(ledger.setting('s','source_coverage:sales'),[[initial,today.isoformat()]])
            self.assertIsNone(ledger.setting('s','operational_window:sales'))
            self.assertEqual(cache.db.execute("SELECT count(*) FROM source_records WHERE source='sales'").fetchone()[0],1)
            cache.close();ledger.close()

    async def test_legacy_cursor_without_original_window_is_backfilled_instead_of_claiming_full_history(self):
        with tempfile.TemporaryDirectory() as folder:
            path=Path(folder)/'db.sqlite'
            ledger=Ledger(path);cache=SyncStore(path)
            today=business_today();initial=(today-timedelta(days=89)).isoformat()
            cursor=(today-timedelta(days=10)).isoformat()+'T10:00:00'
            cache.save_page('s','sales',Page((Record('S1',{'saleID':'S1','lastChangeDate':cursor}),),cursor,False))
            calls=[]
            def handler(since):calls.append(since);return []
            await synchronize_shop(path,'s',stats_token(),lambda *args:None,client_factory=client_for(handler),force=True)
            self.assertEqual(calls,[initial])
            self.assertEqual(ledger.setting('s','source_coverage:sales'),[[initial,today.isoformat()]])
            cache.close();ledger.close()

    async def test_existing_covered_history_uses_incremental_overlap_and_repeated_refresh_is_cached(self):
        with tempfile.TemporaryDirectory() as folder:
            path=Path(folder)/'db.sqlite'
            ledger=Ledger(path);cache=SyncStore(path)
            today=business_today();old=today-timedelta(days=2)
            ledger.set_setting('s','source_coverage:sales',[[(today-timedelta(days=40)).isoformat(),old.isoformat()]])
            cache.save_page('s','sales',Page((),None,True))
            with cache.db:
                cache.db.execute("UPDATE sync_state SET last_success=? WHERE source='sales'",(old.isoformat()+'T12:00:00+03:00',))
            calls=[]
            def handler(since):calls.append(since);return []
            await synchronize_shop(path,'s',stats_token(),lambda *args:None,client_factory=client_for(handler),force=True)
            self.assertEqual(calls,[(old-timedelta(days=7)).isoformat()])
            await synchronize_shop(path,'s',stats_token(),lambda *args:None,client_factory=client_for(handler))
            self.assertEqual(len(calls),1)
            cache.close();ledger.close()
