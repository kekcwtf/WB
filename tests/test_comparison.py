import tempfile
import unittest
from datetime import date
from decimal import Decimal as D
from pathlib import Path
from wb_profit.storage import Ledger
from wb_profit.sync import SyncStore,Page,Record
from wb_profit.finance import Operation,Kind,Cost
from wb_profit.comparison import compare_periods,previous_period


class ComparisonTests(unittest.TestCase):
    def test_equal_inclusive_periods(self):
        self.assertEqual(previous_period(date(2026,1,8),date(2026,1,14)),(date(2026,1,1),date(2026,1,7)))
        self.assertEqual(previous_period(date(2026,1,1),date(2026,1,1)),(date(2025,12,31),date(2025,12,31)))

    def test_real_difference_and_missing_period_are_distinguished(self):
        with tempfile.TemporaryDirectory() as folder:
            path=Path(folder)/'db.sqlite';ledger=Ledger(path);storage=SyncStore(path)
            ledger.upsert([Operation('s','report','1',date(2026,1,1),Kind.SALE,D('500'),'a',1),
                           Operation('s','report','2',date(2026,1,8),Kind.SALE,D('1000'),'a',1)])
            for source in ['finance_weekly','report_totals','advertising']:
                ledger.set_setting('s','source_coverage:'+source,[['2026-01-01','2026-01-14']])
            storage.save_page('s','report_totals',Page((Record('1',{'reportId':1,'dateTo':'2026-01-07'}),Record('2',{'reportId':2,'dateTo':'2026-01-14'})),None,True))
            storage.save_page('s','finance_weekly',Page((Record('1:1',{'reportId':1,'rrDate':'2026-01-01'}),Record('2:1',{'reportId':2,'rrDate':'2026-01-08'})),None,True))
            ledger.set_setting('s','report_audits',[{'report_id':'1','verified':True},{'report_id':'2','verified':True}])
            result=compare_periods(ledger,'s',date(2026,1,8),date(2026,1,14),[Cost('a',date(2025,1,1),D('400'))])
            self.assertEqual((result.current_result,result.previous_result),(D('600'),D('100')))
            self.assertEqual(result.change,D('500'))
            self.assertEqual(result.change_percent,D('500'))
            ledger.set_setting('s','source_coverage:advertising',[['2026-01-08','2026-01-14']])
            incomplete=compare_periods(ledger,'s',date(2026,1,8),date(2026,1,14),[])
            self.assertIsNone(incomplete.change)
            self.assertIsNotNone(incomplete.unavailable_reason)
            ledger.close();storage.close()
