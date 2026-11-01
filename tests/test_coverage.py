import unittest
from datetime import date
from wb_profit.coverage import merge_ranges, period_coverage
from wb_profit.storage import Ledger
from wb_profit.sync import SyncStore, Page, Record

class CoverageTests(unittest.TestCase):
    def test_ranges_do_not_hide_holes(self):
        ranges=merge_ranges([['2026-01-01','2026-01-05']],date(2026,1,10),date(2026,1,15))
        self.assertEqual(len(ranges),2)
        self.assertEqual(merge_ranges(ranges,date(2026,1,6),date(2026,1,9)),[['2026-01-01','2026-01-15']])

    def test_only_closed_reconciled_ad_free_period_is_covered(self):
        import tempfile
        from pathlib import Path
        with tempfile.TemporaryDirectory() as folder:
            path=Path(folder)/'db.sqlite'
            ledger=Ledger(path);storage=SyncStore(path)
            for source in ['finance_weekly','report_totals','advertising']:
                ledger.set_setting('s','source_coverage:'+source,[['2026-01-01','2026-01-31']])
            storage.save_page('s','report_totals',Page((Record('1',{'reportId':1,'dateTo':'2026-01-07'}),),None,True))
            storage.save_page('s','finance_weekly',Page((Record('1:1',{'reportId':1,'rrDate':'2026-01-01'}),),None,True))
            ledger.set_setting('s','report_audits',[{'report_id':'1','verified':True}])
            self.assertTrue(period_coverage(ledger,'s',date(2026,1,1),date(2026,1,7))[0])
            self.assertFalse(period_coverage(ledger,'s',date(2026,1,1),date(2026,1,8))[0])
            storage.save_page('s','advertising',Page((Record('ad',{'updSum':100,'updTime':'2026-01-01T12:00:00+03:00'}),),None,True))
            self.assertFalse(period_coverage(ledger,'s',date(2026,1,1),date(2026,1,7))[0])
            storage.close();ledger.close()

    def test_missing_close_date_does_not_crash_or_confirm_period(self):
        import tempfile
        from pathlib import Path
        from wb_profit.coverage import financial_period_reasons
        with tempfile.TemporaryDirectory() as folder:
            path=Path(folder)/'db.sqlite';ledger=Ledger(path);storage=SyncStore(path)
            storage.save_page('s','report_totals',Page((Record('1',{'reportId':1,'dateTo':None}),),None,True))
            reasons=financial_period_reasons(ledger,'s',date(2026,1,1),date(2026,1,7))
            self.assertTrue(any('дата закрытия' in reason for reason in reasons))
            storage.close();ledger.close()
