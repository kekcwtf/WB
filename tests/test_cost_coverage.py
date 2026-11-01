import os
os.environ.setdefault('QT_QPA_PLATFORM', 'offscreen')
from dataclasses import replace
from datetime import date
from decimal import Decimal as D
from pathlib import Path
import tempfile
import unittest
from PySide6.QtCore import QDate
from PySide6.QtWidgets import QApplication
from wb_profit.finance import Operation, Kind, Cost, calculate
from wb_profit.cost_coverage import cost_requirements
from wb_profit.cost_coverage_view import CostCoverageDialog
from wb_profit.desktop import Window


DAY = date(2026, 1, 1)


def sale(identifier='s', sku='42:a', day=DAY, units=1, confirmed=True):
    return Operation('shop', 'wb_finance', identifier, day, Kind.SALE, D('1000'), sku, units, confirmed)


class CostCoverageTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):cls.app = QApplication.instance() or QApplication([])

    def test_size_specific_dates_default_and_explicit_zero_match_financial_core(self):
        operations = [sale(), sale('b','42:b',date(2026,1,2),2), sale('late','42:b',date(2026,1,6))]
        costs = [Cost('42:a',DAY,D('0')), Cost('*',date(2026,1,5),D('400'))]
        rows = {row.sku:row for row in cost_requirements(operations,costs).rows}
        self.assertFalse(rows['42:a'].needs_attention)
        self.assertEqual(rows['42:b'].missing_price_units,2)
        self.assertEqual(rows['42:b'].checked_units,3)
        self.assertEqual(rows['42:b'].earliest_missing_date,date(2026,1,2))
        self.assertEqual(calculate(operations,costs,None).missing_costs,('42:b',))

    def test_return_uses_original_sale_date_and_unlinked_return_does_not_guess_date(self):
        original = date(2025,12,20)
        returned = Operation('shop','wb_finance','return',DAY,Kind.RETURN,D('-1000'),'42:a',-1,
                             original_sale_id='sale',cost_basis_date=original)
        costs = [Cost('42:a',original,D('400')),Cost('42:a',DAY,D('500'))]
        row = cost_requirements([returned],costs).rows[0]
        self.assertEqual(row.missing_price_units,0)
        self.assertEqual(calculate([returned],costs,None).cost,D('-400'))
        row = cost_requirements([returned],costs[1:]).rows[0]
        self.assertEqual(row.earliest_missing_date,original)
        unlinked = replace(returned,original_sale_id=None,cost_basis_date=None)
        row = cost_requirements([unlinked],costs).rows[0]
        self.assertEqual(row.unlinked_returns,1)
        self.assertIsNone(row.earliest_missing_date)
        self.assertEqual(row.missing_price_units,0)

    def test_missing_quantity_and_preliminary_records_are_not_invented_units(self):
        report = cost_requirements([sale(units=0),sale('preliminary',confirmed=False)],[])
        self.assertEqual(report.preliminary_records,1)
        self.assertEqual(report.rows[0].missing_quantity,1)
        self.assertEqual(report.rows[0].checked_units,0)
        self.assertIsNone(report.rows[0].earliest_missing_date)

    def test_duplicate_reconciliation_cost_conflict_and_store_isolation(self):
        operation = sale()
        row = cost_requirements([operation,operation],[]).rows[0]
        self.assertEqual(row.missing_price_units,1)
        costs = [Cost('42:a',DAY,D('400')),Cost('42:a',DAY,D('500'))]
        for calculate_cost in (lambda: cost_requirements([operation],costs), lambda: calculate([operation],costs,None)):
            with self.assertRaisesRegex(ValueError,'Две разные'):
                calculate_cost()
        with self.assertRaisesRegex(ValueError,'по магазину'):
            cost_requirements([operation,replace(operation,store='other')],[])

    def test_dialog_filters_exact_skus_and_does_not_offer_unknown_sku_or_return_date(self):
        unlinked = Operation('shop','wb_finance','return',DAY,Kind.RETURN,D('-1000'),'42:b',-1)
        dialog = CostCoverageDialog([sale(),sale('unknown',None),unlinked],[],{'42':{'vendorCode':'Артикул','title':'Товар'}})
        self.addCleanup(dialog.close)
        received=[]
        dialog.selected_cost.connect(lambda *args:received.append(args))
        dialog.search.setText('42:a')
        self.assertEqual(dialog.table.rowCount(),1)
        dialog.table.selectRow(0);dialog.choose()
        self.assertEqual(received,[('42:a',DAY)])
        dialog.search.clear();dialog.filter.setCurrentIndex(2)
        self.assertEqual(dialog.table.rowCount(),1)
        dialog.table.selectRow(0);dialog.choose()
        self.assertEqual(len(received),1)
        self.assertIn('не предполагаются',dialog.status.text())
        dialog.filter.setCurrentIndex(0);dialog.search.setText('Без SKU')
        dialog.table.selectRow(0);dialog.choose()
        self.assertEqual(len(received),1)

    def test_settings_prefill_preserves_exact_size_and_date_but_assigns_no_price(self):
        with tempfile.TemporaryDirectory() as folder:
            window = Window(Path(folder)/'db.sqlite')
            self.addCleanup(window.close)
            window.store.addItem('Магазин','shop')
            window.period.setCurrentIndex(6)
            window.start.setDate(QDate(2026,1,1));window.end.setDate(QDate(2026,1,31))
            window.ledger.upsert([sale()])
            window.cost_amount.setText('940')
            dialog = window.make_cost_coverage_dialog()
            self.addCleanup(dialog.close)
            dialog.table.selectRow(0);dialog.choose()
            self.assertEqual(window.cost_sku.text(),'42:a')
            self.assertEqual(window.cost_date.date(),QDate(2026,1,1))
            self.assertEqual(window.cost_amount.text(),'')
            self.assertEqual(window.nav.currentRow(),7)
            self.assertEqual(window.ledger.setting('shop','costs',[]),[])
            self.assertIsNone(window.worker)
            window.close()

    def test_assigning_one_size_removes_only_its_missing_price_row(self):
        with tempfile.TemporaryDirectory() as folder:
            window = Window(Path(folder)/'db.sqlite')
            self.addCleanup(window.close)
            window.store.addItem('Магазин','shop')
            window.period.setCurrentIndex(6)
            window.start.setDate(QDate(2026,1,1));window.end.setDate(QDate(2026,1,31))
            window.ledger.upsert([sale(),sale('b','42:b')])
            window.prefill_missing_cost('42:a',DAY)
            window.cost_amount.setText('400');window.save_cost()
            dialog = window.make_cost_coverage_dialog()
            self.addCleanup(dialog.close)
            self.assertEqual(dialog.table.rowCount(),1)
            self.assertEqual(dialog.table.item(0,0).text(),'42:b')
            self.assertEqual(window.ledger.setting('shop','costs')[0]['amount'],'400')
            window.close()
