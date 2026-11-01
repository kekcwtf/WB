from datetime import datetime,timedelta,timezone
import tempfile
from pathlib import Path
import unittest
from wb_profit.storage import Ledger
from wb_profit.api.transport import ApiError
from wb_profit.retries import save_failure,due,due_shops,network_recovered,plans,clear_shop,PREFIX

NOW=datetime(2026,1,1,12,tzinfo=timezone.utc)


class RetryTests(unittest.TestCase):
    def setUp(self):
        self.ledger=Ledger(':memory:');self.addCleanup(self.ledger.close)

    def test_network_retry_increases_delay_and_is_bounded(self):
        error=ApiError('network',retryable=True,kind='network')
        seconds=[]
        for _ in range(10):
            plan=save_failure(self.ledger,'a','sales',error,now=NOW)
            seconds.append((datetime.fromisoformat(plan['next_at'])-NOW).total_seconds())
        self.assertEqual(seconds,[60,120,240,480,960,1800,1800,1800,1800,1800])
        self.assertFalse(due(plan,NOW))
        self.assertTrue(due(plan,NOW+timedelta(minutes=30)))

    def test_retry_after_and_reserved_api_slot_survive_restart(self):
        with tempfile.TemporaryDirectory() as folder:
            path=Path(folder)/'db.sqlite'
            ledger=Ledger(path)
            plan=save_failure(ledger,'a','sales',ApiError('limit',429,retry_after=600),now=NOW,
                              rate_until=(NOW+timedelta(hours=2)).timestamp())
            ledger.close();ledger=Ledger(path)
            try:
                reopened=ledger.setting('a',PREFIX+'sales')
                self.assertEqual(plan,reopened)
                self.assertFalse(due(reopened,NOW+timedelta(minutes=10),force=True))
                self.assertTrue(due(reopened,NOW+timedelta(hours=2)))
            finally:ledger.close()

    def test_network_recovery_shortens_only_local_network_backoff(self):
        network=ApiError('offline',retryable=True,kind='network')
        for _ in range(6):save_failure(self.ledger,'a','sales',network,now=NOW,rate_until=(NOW+timedelta(seconds=90)).timestamp())
        service=save_failure(self.ledger,'a','orders',ApiError('service',503,retry_after=300),now=NOW)
        network_recovered(self.ledger,['a'],NOW+timedelta(seconds=30))
        plan=self.ledger.setting('a',PREFIX+'sales')
        self.assertEqual(datetime.fromisoformat(plan['next_at']),NOW+timedelta(seconds=90))
        self.assertFalse(due(plan,NOW+timedelta(seconds=30),force=True))
        self.assertEqual(self.ledger.setting('a',PREFIX+'orders'),service)

    def test_permanent_error_clears_pending_retry_without_deleting_financial_settings(self):
        self.ledger.set_setting('a','costs',[{'sku':'sku','date':'2026-01-01','amount':'940'}])
        save_failure(self.ledger,'a','sales',ApiError('offline',retryable=True,kind='network'),now=NOW)
        self.assertIsNone(save_failure(self.ledger,'a','sales',ApiError('auth',401),now=NOW))
        self.assertEqual(plans(self.ledger,['a']),[])
        save_failure(self.ledger,'a','identity',ApiError('offline',retryable=True,kind='network'),now=NOW)
        clear_shop(self.ledger,'a')
        self.assertEqual(plans(self.ledger,['a']),[])
        self.assertEqual(self.ledger.setting('a','costs')[0]['amount'],'940')

    def test_only_due_stores_are_selected_and_foreign_stores_not_expedited(self):
        error=ApiError('offline',retryable=True,kind='network')
        for shop in ['a','b','foreign']:save_failure(self.ledger,shop,'sales',error,now=NOW)
        network_recovered(self.ledger,['a'],NOW)
        self.assertEqual(due_shops(self.ledger,['b','a'],NOW),['a'])
        self.assertEqual(due_shops(self.ledger,['a','b'],NOW+timedelta(minutes=1)),['a','b'])
        self.assertFalse(due(self.ledger.setting('foreign',PREFIX+'sales'),NOW))

    def test_invalid_metadata_is_not_an_unbounded_retry(self):
        for value in [None,{},{'attempt':True,'kind':'network','next_at':'bad','not_before':'bad'}]:
            self.ledger.set_setting('a',PREFIX+'sales',value)
            self.assertFalse(due(value,NOW))
            self.assertEqual(plans(self.ledger,['a']),[])
