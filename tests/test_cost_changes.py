import os
os.environ.setdefault('QT_QPA_PLATFORM', 'offscreen')
from datetime import date
from decimal import Decimal as D
from pathlib import Path
import tempfile
import sqlite3
import unittest
from PySide6.QtWidgets import QApplication
from wb_profit.cost_history import change_cost, undo_cost_change, changes
from wb_profit.cost_changes_view import CostChangesDialog
from wb_profit.storage import Ledger
from wb_profit.finance import Operation, Kind, Cost, calculate
from wb_profit.desktop import Window
from wb_profit.backups import BackupError


class CostChangeTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):cls.app = QApplication.instance() or QApplication([])

    def setUp(self):
        self.folder = tempfile.TemporaryDirectory()
        self.addCleanup(self.folder.cleanup)
        self.path = Path(self.folder.name)/'db.sqlite'
        self.ledger = Ledger(self.path)
        self.addCleanup(self.ledger.close)
        self.day = date(2026, 1, 1)
        self.operation = Operation('s','wb_finance','sale',self.day,Kind.SALE,D('1000'),'a',1)

    def result(self):
        costs = [Cost(row['sku'], date.fromisoformat(row['date']), D(row['amount']))
                 for row in self.ledger.setting('s','costs',[])]
        return calculate([self.operation],costs,None)

    def test_edit_remove_undo_recalculates_and_missing_cost_never_becomes_zero(self):
        change_cost(self.ledger,'s','a',self.day,D('400'))
        self.assertEqual(self.result().result_before_tax,D('600'))
        change_cost(self.ledger,'s','a',self.day,D('500'))
        self.assertEqual(self.result().result_before_tax,D('500'))
        change_cost(self.ledger,'s','a',self.day,None)
        self.assertEqual(self.result().missing_costs,('a',))
        self.assertTrue(undo_cost_change(self.ledger,'s'))
        self.assertEqual(self.result().result_before_tax,D('500'))
        undo_cost_change(self.ledger,'s')
        self.assertEqual(self.result().result_before_tax,D('600'))
        undo_cost_change(self.ledger,'s')
        self.assertEqual(self.result().missing_costs,('a',))
        self.assertFalse(undo_cost_change(self.ledger,'s'))

    def test_repeated_equal_value_and_missing_delete_do_not_make_fake_events(self):
        self.assertFalse(change_cost(self.ledger,'s','a',self.day,None))
        change_cost(self.ledger,'s','a',self.day,D('400'))
        self.assertFalse(change_cost(self.ledger,'s','a',self.day,D('400.00')))
        self.assertEqual(len(changes(self.ledger,'s')),1)
        self.assertEqual(changes(self.ledger,'other'),[])

    def test_removing_individual_price_reveals_explicit_default_and_undo_keeps_other_skus(self):
        change_cost(self.ledger,'s','*',self.day,D('300'))
        change_cost(self.ledger,'s','a',self.day,D('400'))
        change_cost(self.ledger,'s','b',self.day,D('700'))
        change_cost(self.ledger,'s','a',self.day,None)
        self.assertEqual(self.result().cost,D('300'))
        undo_cost_change(self.ledger,'s')
        self.assertEqual(self.result().cost,D('400'))
        self.assertEqual(next(row['amount'] for row in self.ledger.setting('s','costs') if row['sku']=='b'),'700')

    def test_outside_edit_blocks_undo_instead_of_overwriting_current_price(self):
        change_cost(self.ledger,'s','a',self.day,D('400'))
        self.ledger.set_setting('s','costs',[{'sku':'a','date':self.day.isoformat(),'amount':'900'}])
        with self.assertRaisesRegex(ValueError,'вне журнала'):
            undo_cost_change(self.ledger,'s')
        self.assertEqual(self.result().cost,D('900'))
        self.assertIsNone(changes(self.ledger,'s')[0][1]['undone_at'])

    def test_failure_rolls_back_price_sequence_and_audit_together(self):
        self.ledger.db.execute("CREATE TRIGGER fail_cost BEFORE INSERT ON settings WHEN NEW.name LIKE 'cost_change:%' BEGIN SELECT RAISE(ABORT,'test'); END")
        with self.assertRaises(sqlite3.DatabaseError):
            change_cost(self.ledger,'s','a',self.day,D('400'))
        self.assertEqual(self.ledger.setting('s','costs',[]),[])
        self.assertEqual(changes(self.ledger,'s'),[])
        self.assertIsNone(self.ledger.setting('s','cost_change_sequence'))

    def test_backup_retains_undo_and_rejects_mixed_store_audit(self):
        change_cost(self.ledger,'s','a',self.day,D('400'))
        good = self.ledger.backup(Path(self.folder.name)/'good.sqlite')
        change_cost(self.ledger,'s','a',self.day,D('500'))
        self.ledger.restore(good)
        self.assertEqual(self.result().cost,D('400'))
        name,value = changes(self.ledger,'s')[0]
        value['store']='other'
        self.ledger.set_setting('s',name,value)
        bad = self.ledger.backup(Path(self.folder.name)/'bad.sqlite')
        self.ledger.restore(good)
        with self.assertRaises(BackupError):self.ledger.restore(bad)
        undo_cost_change(self.ledger,'s')
        self.assertEqual(self.result().missing_costs,('a',))

    def test_legacy_prices_are_not_given_invented_history_and_bad_duplicates_are_rejected(self):
        old = {'sku':'a','date':self.day.isoformat(),'amount':'400'}
        self.ledger.set_setting('s','costs',[old])
        self.assertEqual(changes(self.ledger,'s'),[])
        change_cost(self.ledger,'s','a',self.day,D('500'))
        self.assertEqual(changes(self.ledger,'s')[0][1]['before'],old)
        self.ledger.set_setting('s','costs',[old,old])
        with self.assertRaisesRegex(ValueError,'Повтор'):
            change_cost(self.ledger,'s','a',self.day,D('600'))

    def test_native_table_selection_removal_undo_and_visible_audit(self):
        window = Window(self.path)
        self.addCleanup(window.close)
        window.store.addItem('Магазин','s')
        window.cost_sku.setText('a');window.cost_amount.setText('400')
        from PySide6.QtCore import QDate
        window.cost_date.setDate(QDate(2026,1,1))
        window.save_cost()
        window.cost_history.selectRow(0)
        window.select_cost(0)
        self.assertEqual(window.cost_sku.text(),'a')
        self.assertEqual(window.cost_amount.text(),'400')
        window.remove_cost()
        self.assertEqual(window.cost_history.rowCount(),0)
        window.undo_cost()
        self.assertEqual(window.cost_history.rowCount(),1)
        dialog = CostChangesDialog(window.ledger,'s')
        self.addCleanup(dialog.close)
        self.assertEqual(dialog.table.rowCount(),2)
        self.assertTrue(any(dialog.table.item(row,7).text()=='Отменено' for row in range(2)))
        window.close()
