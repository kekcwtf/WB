import os
os.environ.setdefault('QT_QPA_PLATFORM','offscreen')
from datetime import date,datetime,timedelta
from pathlib import Path
from decimal import Decimal as D
import tempfile
import unittest
from PySide6.QtCore import Qt
from PySide6.QtWidgets import QApplication
from wb_profit.business_dates import MOSCOW
from wb_profit.inventory_view import InventoryView
from wb_profit.storage import Ledger
from wb_profit.sync import SyncStore,Record,Page
from wb_profit.finance import Operation,Kind

TODAY=date(2026,1,31)
START=TODAY-timedelta(days=29)
NOW=datetime(2026,1,31,12,tzinfo=MOSCOW)


class InventoryViewTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):cls.app=QApplication.instance() or QApplication([])

    def setUp(self):
        self.folder=tempfile.TemporaryDirectory();self.addCleanup(self.folder.cleanup)
        path=Path(self.folder.name)/'db.sqlite'
        self.ledger=Ledger(path);self.addCleanup(self.ledger.close)
        self.cache=SyncStore(path);self.addCleanup(self.cache.close)
        for source,rows in [('stocks',[{'nmId':42,'barcode':'123','warehouses':[{'warehouseName':'Всего находится на складах','quantity':10}]},
                                       {'nmId':99,'barcode':'456','warehouses':[{'warehouseName':'Всего находится на складах','quantity':5}]}]),
                            ('sales',[{'saleID':'S1','nmId':42,'barcode':'123','warehouseType':'Склад WB','date':TODAY.isoformat()}]),
                            ('products',[{'nmID':42,'title':'Рубашка','subjectName':'Блузки'},
                                         {'nmID':99,'title':'Шарф','subjectName':'Аксессуары'}])]:
            self.cache.save_page('shop',source,Page(tuple(Record(str(i),row) for i,row in enumerate(rows)),None,True,snapshot=True))
        with self.cache.db:
            self.cache.db.execute('UPDATE sync_state SET last_success=?',(NOW.isoformat(),))
        self.ledger.set_setting('shop','source_coverage:sales',[[START.isoformat(),TODAY.isoformat()]])
        self.view=InventoryView();self.addCleanup(self.view.close)
        self.refresh()

    def refresh(self):self.view.refresh(self.ledger,'shop',TODAY,START,TODAY,NOW)

    def visible(self):
        return [self.view.table.item(row,0).text() for row in range(self.view.table.rowCount()) if not self.view.table.isRowHidden(row)]

    def test_filters_sorting_alphabetical_categories_and_options_persist(self):
        self.assertEqual([self.view.category.itemText(i) for i in range(self.view.category.count())],['Все категории','Аксессуары','Блузки'])
        self.view.filter.setCurrentIndex(2)
        self.assertCountEqual(self.visible(),['42','99'])
        self.view.days.setValue(365)
        self.assertEqual(self.visible(),['99'])
        self.assertEqual(self.ledger.setting('shop','inventory_options')['max_stock_days'],365)
        self.view.filter.setCurrentIndex(0)
        self.view.search.setText('Рубашка')
        self.assertEqual(self.visible(),['42'])
        self.view.search.clear()
        self.view.table.sortItems(4,Qt.SortOrder.DescendingOrder)
        self.view.filter.setCurrentIndex(1)
        self.assertEqual(self.visible(),['99'])
        reopened=InventoryView()
        reopened.refresh(self.ledger,'shop',TODAY,START,TODAY,NOW)
        self.assertEqual(reopened.days.value(),365)
        reopened.close()

    def test_partial_history_displays_unknown_instead_of_zero_sales_or_demand_forecast(self):
        self.ledger.set_setting('shop','source_coverage:sales',[])
        self.refresh()
        for row in range(self.view.table.rowCount()):
            self.assertIsNone(self.view.table.item(row,5).value)
            self.assertEqual(self.view.table.item(row,7).text(),'—')
            self.assertNotIn('Возможный неликвид',self.view.table.item(row,12).text())
        self.view.filter.setCurrentIndex(1)
        self.assertEqual(self.visible(),[])
        self.view.filter.setCurrentIndex(4)
        self.assertCountEqual(self.visible(),['42','99'])
        self.assertIn('Не полностью загружена история',self.view.summary.text())

    def test_invalid_period_clears_previous_analysis_and_store_switch_has_no_foreign_rows(self):
        self.view.invalid_period()
        self.assertIsNone(self.view.analysis)
        self.assertEqual(self.view.table.rowCount(),0)
        self.assertIn('Неверный период',self.view.summary.text())
        self.view.refresh(self.ledger,'other',TODAY,START,TODAY,NOW)
        self.assertEqual(self.view.table.rowCount(),0)
        self.assertIsNone(self.view.analysis.stocks_loaded)

    def test_high_storage_filter_requires_reconciled_financial_period(self):
        for source,payload in [('finance_weekly',{'reportId':1,'rrDate':TODAY.isoformat()}),
                               ('report_totals',{'reportId':1,'dateTo':TODAY.isoformat()})]:
            self.cache.save_page('shop',source,Page((Record('1',payload),),None,True))
            self.ledger.set_setting('shop','source_coverage:'+source,[[START.isoformat(),TODAY.isoformat()]])
        self.ledger.set_setting('shop','report_audits',[{'report_id':'1','verified':True,'difference':'0'}])
        self.ledger.upsert([Operation('shop','report','sale',TODAY,Kind.SALE,D('1000'),'42:123',1),
                            Operation('shop','report','storage',TODAY,Kind.STORAGE,D('-100'),'42:123'),
                            Operation('shop','report','common',TODAY,Kind.STORAGE,D('-50'))])
        self.refresh()
        self.view.filter.setCurrentIndex(3)
        self.assertEqual(self.visible(),['42'])
        self.assertIn('вне строк склада: 50.00',self.view.summary.text())
        self.view.percent.setValue(11)
        self.assertEqual(self.visible(),[])
        self.view.percent.setValue(10)
        self.assertEqual(self.visible(),['42'])
        self.ledger.set_setting('shop','report_audits',[])
        self.refresh()
        self.assertEqual(self.visible(),[])
        self.assertIn('неполные данные',self.view.summary.text())
