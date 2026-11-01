import os
os.environ.setdefault('QT_QPA_PLATFORM','offscreen')
from dataclasses import replace
from datetime import date
from decimal import Decimal as D
from pathlib import Path
import tempfile
import unittest
from PySide6.QtCore import Qt,QDate
from PySide6.QtWidgets import QApplication
from wb_profit.finance import Operation,Kind,Cost,TaxSettings
from wb_profit.taxes import TaxRule,save_rule
from wb_profit.storage import Ledger
from wb_profit.desktop import Window
from wb_profit.return_review_view import ReturnReviewView

JAN=date(2026,1,1);JUL=date(2026,7,1)


class ReturnReviewViewTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):cls.app=QApplication.instance() or QApplication([])

    def setUp(self):
        self.folder=tempfile.TemporaryDirectory();self.addCleanup(self.folder.cleanup)
        self.path=Path(self.folder.name)/'db.sqlite'
        self.ledger=Ledger(self.path);self.addCleanup(self.ledger.close)
        self.sale=Operation('s','wb_finance','sale',JAN,Kind.SALE,D('1050'),'42:123',1)
        self.returned=Operation('s','wb_finance','return',JUL,Kind.RETURN,D('-1050'),'42:123',-1,
                                original_sale_id='sale',cost_basis_date=JAN)
        self.unlinked=Operation('s','wb_finance','unknown',JUL,Kind.RETURN,D('-900'),'43:other',-1)
        self.ledger.upsert([self.sale,self.returned,self.unlinked])
        self.costs=[Cost('42:123',JAN,D('400')),Cost('42:123',JUL,D('900'))]
        self.rules=[TaxRule(JAN,TaxSettings(D('4'),D('5'),confirmed_by_user=True)),
                    TaxRule(JUL,TaxSettings(D('6'),D('7'),confirmed_by_user=True))]
        self.view=ReturnReviewView();self.addCleanup(self.view.close)
        self.refresh()

    def refresh(self):self.view.set_context(self.ledger,'s',[self.returned,self.unlinked],self.costs,self.rules)

    def visible(self):
        return [self.view.table.item(row,10).text() for row in range(self.view.table.rowCount()) if not self.view.table.isRowHidden(row)]

    def test_filters_sorting_and_original_rate_estimates_are_present_without_ledger_mutation(self):
        self.assertCountEqual(self.visible(),['return','unknown'])
        self.view.filter.setCurrentIndex(1)
        self.assertEqual(self.visible(),['unknown'])
        self.view.filter.setCurrentIndex(4)
        self.assertEqual(self.visible(),['return'])
        self.view.search.setText('sale')
        self.assertEqual(self.visible(),['return'])
        self.view.table.sortItems(3,Qt.SortOrder.DescendingOrder)
        row=next(row for row in range(self.view.table.rowCount()) if self.view.table.item(row,10).text()=='return')
        self.assertEqual(self.view.table.item(row,6).value,D('400'))
        self.assertEqual(self.view.table.item(row,7).value,D('50'))
        self.assertEqual(self.view.table.item(row,8).value,D('40'))
        self.assertIn('не налоговый вычет',self.view.table.item(row,7).toolTip())
        self.assertEqual(len(self.ledger.operations('s',date.min,date.max)),3)

    def test_corrected_original_amount_clears_unproven_components_but_not_cost(self):
        self.ledger.upsert([replace(self.sale,amount=D('1100'))])
        self.refresh();self.view.filter.setCurrentIndex(2)
        self.assertEqual(self.visible(),['return'])
        matched=next(row for row in self.view.analysis.rows if row.operation.identifier=='return')
        self.assertIsNone(matched.vat_price_component)
        self.assertEqual(matched.restored_cost,D('400'))
        self.view.clear('Неверный период')
        self.assertIsNone(self.view.analysis)
        self.assertEqual(self.view.table.rowCount(),0)

    def test_main_window_loads_original_outside_selected_period_and_does_not_enable_net(self):
        self.ledger.set_setting('_app','stores',[{'id':'s','name':'Магазин'}])
        self.ledger.set_setting('s','period',{'index':6,'start':JUL.isoformat(),'end':JUL.isoformat()})
        self.ledger.set_setting('s','costs',[{'sku':cost.sku,'date':cost.effective.isoformat(),'amount':str(cost.per_unit)} for cost in self.costs])
        for rule in self.rules:save_rule(self.ledger,'s',rule)
        window=Window(self.path)
        self.assertEqual(window.return_review.analysis.linked_count,1)
        row=next(row for row in window.return_review.analysis.rows if row.linked)
        self.assertEqual(row.sale.occurred,JAN)
        self.assertEqual(row.restored_cost,D('400'))
        self.assertIsNone(window.overview_view.analysis.metrics['net'].value)
        self.assertEqual(window.pages.count(),8)
        window.start.setDate(QDate(2026,7,2))
        self.assertIsNone(window.return_review.analysis)
        self.assertEqual(window.return_review.table.rowCount(),0)
        window.close()
