import os
os.environ.setdefault('QT_QPA_PLATFORM','offscreen')
from datetime import date
from decimal import Decimal as D
from pathlib import Path
import tempfile
import unittest
from unittest.mock import patch
from PySide6.QtCore import Qt
from PySide6.QtWidgets import QApplication
from wb_profit.cohort_view import CohortView
from wb_profit.desktop import Window
from wb_profit.finance import Operation,Kind
from wb_profit.storage import Ledger
from wb_profit.sync import SyncStore,Page,Record
from test_cohorts import order,event,START,END,TODAY
from test_cohorts_storage import NOW


class CohortViewTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):cls.app=QApplication.instance() or QApplication([])

    def setUp(self):
        self.folder=tempfile.TemporaryDirectory();self.addCleanup(self.folder.cleanup)
        self.path=Path(self.folder.name)/'db.sqlite'
        self.ledger=Ledger(self.path);self.addCleanup(self.ledger.close)
        self.cache=SyncStore(self.path);self.addCleanup(self.cache.close)
        orders=[order('small','s',supplierArticle='seller',subject='Рубашки'),
                *[order('large'+str(i),'l',isCancel=True,supplierArticle='seller',subject='Рубашки') for i in range(3)]]
        for source,rows in [('orders',orders),('sales',[event('small','S1','s')])]:
            self.cache.save_page('s',source,Page(tuple(Record(str(i),row) for i,row in enumerate(rows)),None,True,snapshot=True))
            with self.cache.db:self.cache.db.execute('UPDATE sync_state SET last_success=? WHERE source=?',(NOW.isoformat(),source))
            self.ledger.set_setting('s','source_coverage:'+source,[[START.isoformat(),TODAY.isoformat()]])
        self.view=CohortView();self.addCleanup(self.view.close)

    def test_sku_model_grouping_search_and_raw_order_metadata_fallback(self):
        self.view.refresh(self.ledger,'s',START,END,now=NOW)
        self.assertEqual(self.view.rows_by_sku['42:s'].buyout_percent,D('100'))
        self.assertEqual(self.view.categories,{'Рубашки'})
        self.assertEqual(self.view.table.item(0,1).text(),'seller')
        self.view.table.sortItems(10,Qt.SortOrder.DescendingOrder)
        self.view.filter_rows('42:l')
        visible=[self.view.table.item(row,0).text() for row in range(self.view.table.rowCount()) if not self.view.table.isRowHidden(row)]
        self.assertEqual(visible,['42:l'])
        self.view.refresh(self.ledger,'s',START,END,now=NOW,grouped=True)
        self.assertEqual(self.view.table.rowCount(),1)
        self.assertEqual(self.view.table.item(0,10).value,D('25'))
        self.assertNotIn('cancelled',self.view.table.item(0,10).toolTip())

    def test_missing_history_removes_rate_and_foreign_store_clears_previous_rows(self):
        self.view.refresh(self.ledger,'s',START,END,now=NOW)
        self.ledger.set_setting('s','source_coverage:sales',[])
        self.view.refresh(self.ledger,'s',START,END,now=NOW)
        self.assertTrue(all(self.view.table.item(row,10).value is None for row in range(self.view.table.rowCount())))
        self.assertIn('Покрытие неполное',self.view.summary.text())
        self.view.refresh(self.ledger,'other',START,END,now=NOW)
        self.assertEqual(self.view.table.rowCount(),0)

    def test_main_window_keeps_orders_separate_from_finances_and_persists_mode(self):
        self.ledger.set_setting('_app','stores',[{'id':'s','name':'Магазин'}])
        self.ledger.set_setting('s','period',{'index':6,'start':START.isoformat(),'end':END.isoformat()})
        original=CohortView.refresh
        def clocked(view,ledger,shop,start,end,**kwargs):
            kwargs['now']=NOW
            return original(view,ledger,shop,start,end,**kwargs)
        with patch.object(CohortView,'refresh',clocked):
            window=Window(self.path)
            self.assertEqual(window.products.rowCount(),0)
            self.assertEqual(window.cohort_view.table.rowCount(),2)
            self.assertEqual(window.overview_view.values['net'].text(),'—')
            window.product_mode.setCurrentIndex(1)
            self.assertEqual(window.product_tables.currentWidget(),window.cohort_view)
            self.assertTrue(window.profit_filter.isHidden())
            window.close();window=Window(self.path)
            self.assertEqual(window.product_mode.currentData(),'orders')
            self.ledger.upsert([Operation('s','report','financial',date(2026,1,5),Kind.SALE,D('1050'),'42:s',1),
                                Operation('s','report','earlier-order',date(2026,1,5),Kind.SALE,D('1050'),'99:other',1)])
            window.refresh()
            values={window.products.item(row,0).text():(window.products.item(row,25).value,window.products.item(row,26).value) for row in range(window.products.rowCount())}
            self.assertEqual(values['42:s'],(D('1'),D('100')))
            self.assertEqual(values['99:other'],(D('0'),None))
            self.assertEqual(window.pages.count(),8)
            window.grouping.setCurrentIndex(1)
            values={window.products.item(row,0).text():window.products.item(row,26).value for row in range(window.products.rowCount())}
            self.assertEqual(values['42'],D('25'))
            window.close()
