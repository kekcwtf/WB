from datetime import date,datetime,timedelta
from decimal import Decimal as D
from pathlib import Path
import tempfile
import unittest
from wb_profit.business_dates import MOSCOW
from wb_profit.cohorts import cohorts_from_ledger
from wb_profit.storage import Ledger
from wb_profit.sync import SyncStore,Page,Record
from test_cohorts import order,event,START,END,TODAY

NOW=datetime(2026,2,1,12,tzinfo=MOSCOW)


class CohortStorageTests(unittest.TestCase):
    def setUp(self):
        self.folder=tempfile.TemporaryDirectory();self.addCleanup(self.folder.cleanup)
        path=Path(self.folder.name)/'db.sqlite'
        self.ledger=Ledger(path);self.addCleanup(self.ledger.close)
        self.cache=SyncStore(path);self.addCleanup(self.cache.close)
        self.snapshot('orders',[order('bought'),order('cancelled',isCancel=True)])
        self.snapshot('sales',[event('bought','S1')])
        for source in ['orders','sales']:
            self.ledger.set_setting('s','source_coverage:'+source,[[START.isoformat(),TODAY.isoformat()]])

    def snapshot(self,source,rows,shop='s'):
        self.cache.save_page(shop,source,Page(tuple(Record(str(i),row) for i,row in enumerate(rows)),None,True,snapshot=True))
        with self.cache.db:
            self.cache.db.execute('UPDATE sync_state SET last_success=? WHERE store=? AND source=?',(NOW.isoformat(),shop,source))

    def report(self):return cohorts_from_ledger(self.ledger,'s',START,END,now=NOW)

    def test_sale_after_end_is_joined_and_other_store_cannot_supply_an_outcome(self):
        self.snapshot('sales',[event('cancelled','S2')],shop='other')
        report=self.report()
        self.assertTrue(report.history_complete)
        self.assertEqual(report.rows[0].buyout_percent,D('50'))
        self.assertEqual(self.ledger.db.execute('SELECT count(*) FROM operations').fetchone()[0],0)

    def test_history_must_cover_outcomes_through_observation_day_not_just_order_window(self):
        self.ledger.set_setting('s','source_coverage:sales',[[START.isoformat(),END.isoformat()]])
        report=self.report()
        self.assertFalse(report.history_complete)
        self.assertEqual(report.rows[0].bought,1)
        self.assertIsNone(report.rows[0].buyout_percent)

    def test_stale_or_inflight_sources_disable_rate(self):
        self.cache.mark('s','sales','running')
        self.assertIsNone(self.report().rows[0].buyout_percent)
        with self.cache.db:
            self.cache.db.execute("UPDATE sync_state SET status='success',last_success=? WHERE source='sales'",((NOW-timedelta(days=2)).isoformat(),))
        self.assertFalse(self.report().history_complete)

    def test_bad_order_date_is_not_silently_filtered_out_by_sql(self):
        self.snapshot('orders',[order('bought'),order('invalid',date='2026-02-31T10:00:00')])
        report=self.report()
        self.assertFalse(report.history_complete)
        self.assertTrue(any('Не распознаны' in issue for issue in report.reasons))

    def test_missing_sale_srid_is_detected_even_though_index_join_cannot_match_it(self):
        self.snapshot('sales',[event('bought','S1'),event(None,'S2')])
        report=self.report()
        self.assertFalse(report.history_complete)
        self.assertTrue(any('без srid' in issue for issue in report.reasons))

    def test_moscow_boundary_order_is_not_lost_by_iso_date_prefix_filter(self):
        self.snapshot('orders',[order('bought',date='2025-12-31T22:00:00Z')])
        report=self.report()
        self.assertEqual(report.rows[0].orders,1)
        self.assertEqual(report.rows[0].buyout_percent,D('100'))

    def test_srid_batch_boundary_does_not_drop_outcomes(self):
        self.snapshot('orders',[order('o'+str(i)) for i in range(450)])
        self.snapshot('sales',[event('o'+str(i),'S'+str(i)) for i in range(450)])
        report=self.report()
        self.assertEqual(report.rows[0].orders,450)
        self.assertEqual(report.rows[0].bought,450)
        self.assertEqual(report.rows[0].buyout_percent,D('100'))
