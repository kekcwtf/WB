import tempfile
import unittest
from datetime import date, datetime, timedelta
from decimal import Decimal as D
from pathlib import Path
from wb_profit.business_dates import MOSCOW
from wb_profit.inventory import inventory_report, statistics_day
from wb_profit.storage import Ledger
from wb_profit.sync import SyncStore, Page, Record
from wb_profit.finance import Kind, Operation

TODAY=date(2026,1,31)
NOW=datetime(2026,1,31,12,tzinfo=MOSCOW)
START=TODAY-timedelta(days=29)


class InventoryTests(unittest.TestCase):
    def setUp(self):
        self.folder=tempfile.TemporaryDirectory();self.addCleanup(self.folder.cleanup)
        self.path=Path(self.folder.name)/'db.sqlite'
        self.ledger=Ledger(self.path);self.addCleanup(self.ledger.close)
        self.cache=SyncStore(self.path);self.addCleanup(self.cache.close)
        self.snapshot('stocks',[{'nmId':42,'barcode':'00123','vendorCode':'shirt','techSize':'M',
                                  'warehouses':[{'warehouseName':'Всего находится на складах','quantity':10},
                                                {'warehouseName':'Коледино','quantity':10},
                                                {'warehouseName':'В пути до получателей','quantity':9}]}])
        self.sales(10)
        self.ledger.set_setting('s','source_coverage:sales',[[START.isoformat(),TODAY.isoformat()]])

    def snapshot(self, source, rows):
        self.cache.save_page('s',source,Page(tuple(Record(str(i),row) for i,row in enumerate(rows)),None,True,snapshot=True))
        with self.cache.db:
            self.cache.db.execute('UPDATE sync_state SET last_success=? WHERE store=? AND source=?',(NOW.isoformat(),'s',source))

    def sales(self,count):
        self.snapshot('sales',[{'saleID':'S'+str(i),'nmId':42,'barcode':'00123','warehouseType':'Склад WB',
                                'date':TODAY.isoformat()+'T10:00:00'} for i in range(count)])

    def report(self,**kwargs):
        return inventory_report(self.ledger,'s',TODAY,START,TODAY,now=NOW,**kwargs)

    def financial(self,storage='100', revenue='1000',extra=()):
        self.snapshot('finance_weekly',[{'reportId':1,'rrDate':TODAY.isoformat()}])
        self.snapshot('report_totals',[{'reportId':1,'dateTo':TODAY.isoformat()}])
        for name in ['finance_weekly','report_totals']:
            self.ledger.set_setting('s','source_coverage:'+name,[[START.isoformat(),TODAY.isoformat()]])
        self.ledger.set_setting('s','report_audits',[{'report_id':'1','verified':True,'difference':'0'}])
        self.ledger.upsert([Operation('s','report','sale',TODAY,Kind.SALE,D(revenue),'42:00123',1),
                            Operation('s','report','fee',TODAY,Kind.STORAGE,-D(storage),'42:00123'),*extra])

    def test_stock_total_is_not_added_to_warehouses_or_transit_and_average_is_exact(self):
        row=self.report().rows[0]
        self.assertEqual(row.stock,10)
        self.assertEqual(row.sales,10)
        self.assertEqual(row.daily_sales,D('10')/30)
        self.assertEqual(row.estimated_days,D('30'))
        self.assertFalse(row.no_sales)
        self.assertFalse(row.potential_illiquid)

    def test_one_successful_request_does_not_prove_thirty_day_history(self):
        self.ledger.set_setting('s','source_coverage:sales',[[TODAY.isoformat(),TODAY.isoformat()]])
        report=self.report();row=report.rows[0]
        self.assertFalse(report.sales_complete)
        self.assertIsNone(row.sales)
        self.assertIsNone(row.daily_sales)
        self.assertIsNone(row.no_sales)
        self.assertIsNone(row.potential_illiquid)

    def test_missing_day_or_in_progress_sync_suppresses_no_sales_signal(self):
        self.sales(0)
        self.ledger.set_setting('s','source_coverage:sales',[['2026-01-02','2026-01-15'],['2026-01-17','2026-01-31']])
        self.assertIsNone(self.report().rows[0].no_sales)
        self.ledger.set_setting('s','source_coverage:sales',[[START.isoformat(),TODAY.isoformat()]])
        self.cache.mark('s','sales','running')
        self.assertIsNone(self.report().rows[0].no_sales)

    def test_complete_empty_sales_and_large_stock_are_only_potential_illiquidity(self):
        self.sales(0)
        row=self.report().rows[0]
        self.assertEqual(row.sales,0)
        self.assertTrue(row.no_sales)
        self.assertTrue(row.potential_illiquid)
        self.assertIsNone(row.estimated_days) # не рисуем бесконечность или выдуманный срок

    def test_slow_stock_threshold_is_explicit_and_changeable(self):
        self.sales(1)
        self.assertEqual(self.report().rows[0].estimated_days,D('300'))
        self.assertTrue(self.report().rows[0].potential_illiquid)
        self.assertFalse(self.report(max_stock_days=365).rows[0].potential_illiquid)

    def test_stale_snapshot_keeps_loaded_quantity_but_has_no_current_stock_signals(self):
        with self.cache.db:
            self.cache.db.execute("UPDATE sync_state SET last_success=? WHERE source='stocks'",((NOW-timedelta(days=2)).isoformat(),))
        row=self.report().rows[0]
        self.assertEqual(row.stock,10)
        self.assertIsNone(row.daily_sales)
        self.assertIsNone(row.potential_illiquid)

    def test_fbw_sales_exclude_returns_fbs_and_old_events_and_use_moscow_dates(self):
        rows=[{'saleID':'S1','nmId':42,'barcode':'00123','warehouseType':'Склад WB','date':'2026-01-01T22:30:00Z'},
              {'saleID':'R2','nmId':42,'barcode':'00123','warehouseType':'Склад WB','date':'2026-01-10T10:00:00'},
              {'saleID':'S3','nmId':42,'barcode':'00123','warehouseType':'Склад продавца','date':'2026-01-10T10:00:00'},
              {'saleID':'S4','nmId':42,'barcode':'00123','warehouseType':'Склад WB','date':'2026-01-01T10:00:00'}]
        self.snapshot('sales',rows)
        self.assertEqual(self.report().rows[0].sales,1)
        self.assertEqual(statistics_day('2026-01-01T23:00:00'),date(2026,1,1))
        self.assertIsNone(statistics_day('invalid'))

    def test_malformed_date_or_unknown_warehouse_is_not_silently_zero_sales(self):
        for change in [{'date':'invalid'},{'warehouseType':'unknown'},{'saleID':'unknown'}]:
            row={'saleID':'S1','nmId':42,'barcode':'00123','warehouseType':'Склад WB','date':TODAY.isoformat()}
            self.snapshot('sales',[{**row,**change}])
            self.assertIsNone(self.report().rows[0].sales)

    def test_storage_alert_requires_verified_finance_and_uses_financial_revenue(self):
        self.financial(storage='100',revenue='1000')
        report=self.report();row=report.rows[0]
        self.assertTrue(report.storage_complete)
        self.assertEqual(row.storage_loaded,D('100'))
        self.assertEqual(row.storage_ratio,D('10'))
        self.assertTrue(row.high_storage)
        self.assertFalse(self.report(storage_warning_percent=D('11')).rows[0].high_storage)
        self.ledger.set_setting('s','report_audits',[])
        row=self.report().rows[0]
        self.assertEqual(row.storage_loaded,D('100'))
        self.assertIsNone(row.high_storage)
        self.assertIsNone(row.storage_ratio)

    def test_common_model_and_sold_out_storage_are_not_copied_into_each_size(self):
        extra=[Operation('s','report','common',TODAY,Kind.STORAGE,D('-50')),
               Operation('s','report','model',TODAY,Kind.STORAGE,D('-30'),'42'),
               Operation('s','report','soldout',TODAY,Kind.STORAGE,D('-20'),'99:other')]
        self.financial(storage='100',extra=extra)
        report=self.report()
        self.assertEqual(report.storage_loaded,D('200'))
        self.assertEqual(report.rows[0].storage_loaded,D('100'))
        self.assertEqual(report.storage_outside_rows,D('100'))

    def test_storage_reversal_is_net_amount_and_not_an_extra_expense(self):
        self.financial(storage='100',extra=[Operation('s','report','reversal',TODAY,Kind.STORAGE,D('40'),'42:00123')])
        row=self.report().rows[0]
        self.assertEqual(row.storage_loaded,D('60'))
        self.assertEqual(row.storage_ratio,D('6'))
        self.assertFalse(row.high_storage)

    def test_duplicate_stock_keys_and_duplicate_totals_do_not_multiply_storage(self):
        self.financial()
        row={'nmId':42,'barcode':'00123','warehouses':[{'warehouseName':'Всего находится на складах','quantity':10}]}
        self.snapshot('stocks',[row,{**row,'techSize':'other'}])
        report=self.report()
        self.assertTrue(all(item.stock is None for item in report.rows))
        self.assertEqual(sum(item.storage_loaded for item in report.rows),D('0'))
        self.assertEqual(report.storage_outside_rows,D('100'))
        self.snapshot('stocks',[{**row,'warehouses':row['warehouses']*2}])
        self.assertIsNone(self.report().rows[0].stock)

    def test_storage_at_zero_revenue_has_no_misleading_infinite_ratio(self):
        self.financial(revenue='0')
        row=self.report().rows[0]
        self.assertTrue(row.high_storage)
        self.assertIsNone(row.storage_ratio)

    def test_invalid_analytical_thresholds_are_rejected(self):
        for kwargs in [{'max_stock_days':0},{'max_stock_days':True},{'storage_warning_percent':D('0')},
                       {'storage_warning_percent':D('101')}]:
            with self.subTest(kwargs=kwargs),self.assertRaises(ValueError):self.report(**kwargs)
