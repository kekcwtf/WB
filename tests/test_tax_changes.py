import os
os.environ.setdefault('QT_QPA_PLATFORM','offscreen')
from datetime import date
from decimal import Decimal as D
from pathlib import Path
import tempfile
import sqlite3
import unittest
from PySide6.QtWidgets import QApplication
from wb_profit.storage import Ledger
from wb_profit.finance import Operation,Kind,Cost,TaxSettings
from wb_profit.taxes import TaxRule,save_rule,load_rules,calculate_with_history
from wb_profit.tax_changes import change_rule,undo_change,changes
from wb_profit.tax_changes_view import TaxChangesDialog
from wb_profit.desktop import Window
from wb_profit.backups import BackupError

JAN=date(2026,1,1)
JUL=date(2026,7,1)


class TaxChangeTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):cls.app=QApplication.instance() or QApplication([])

    def setUp(self):
        self.folder=tempfile.TemporaryDirectory();self.addCleanup(self.folder.cleanup)
        self.path=Path(self.folder.name)/'db.sqlite'
        self.ledger=Ledger(self.path);self.addCleanup(self.ledger.close)
        self.old=TaxRule(JAN,TaxSettings(D('4'),D('5'),confirmed_by_user=True))
        self.new=TaxRule(JUL,TaxSettings(D('6'),D('7'),confirmed_by_user=True))

    def result(self,day):
        return calculate_with_history([Operation('s','wb_finance','sale',day,Kind.SALE,D('1000'),'a',1)],
                                      [Cost('a',JAN,D('400'))],load_rules(self.ledger,'s'))[0]

    def test_independent_taxes_remove_and_undo_use_original_dates(self):
        save_rule(self.ledger,'s',self.old);save_rule(self.ledger,'s',self.new)
        # Цена1000, COGS400: 5%НДС=47.62, 4%УСН=38.10 =>514.28.
        self.assertEqual(self.result(JAN).net_profit,D('514.28'))
        # 7%НДС=65.42, 6%УСН=56.07 =>478.51.
        self.assertEqual(self.result(JUL).net_profit,D('478.51'))
        change_rule(self.ledger,'s',JUL,None)
        self.assertEqual(self.result(JUL).net_profit,D('514.28'))
        undo_change(self.ledger,'s')
        self.assertEqual(self.result(JUL).net_profit,D('478.51'))
        self.assertEqual(self.result(JAN).net_profit,D('514.28'))

    def test_removing_last_version_creates_unknown_tax_not_zero_and_backup_stays_valid(self):
        save_rule(self.ledger,'s',self.old)
        change_rule(self.ledger,'s',JAN,None)
        self.assertIsNone(self.result(JAN).usn)
        self.assertIsNone(self.result(JAN).net_profit)
        self.assertIsNone(self.ledger.setting('s','taxes'))
        snapshot=self.ledger.backup(Path(self.folder.name)/'empty-tax.sqlite')
        self.ledger.restore(snapshot)
        undo_change(self.ledger,'s')
        self.assertEqual(self.result(JAN).net_profit,D('514.28'))

    def test_equivalent_repeat_is_noop_and_undo_preserves_unconfirmed_flag(self):
        unconfirmed=TaxRule(JAN,TaxSettings(D('4'),D('5'),confirmed_by_user=False))
        save_rule(self.ledger,'s',unconfirmed)
        self.assertFalse(save_rule(self.ledger,'s',TaxRule(JAN,TaxSettings(D('4.00'),D('5.00')))))
        save_rule(self.ledger,'s',self.old)
        self.assertEqual(len(changes(self.ledger,'s')),2)
        undo_change(self.ledger,'s')
        self.assertFalse(load_rules(self.ledger,'s')[0].settings.confirmed_by_user)
        self.assertIsNone(self.result(JAN).net_profit)

    def test_stale_selection_and_external_edit_are_not_overwritten(self):
        save_rule(self.ledger,'s',self.old)
        newer=TaxRule(JAN,TaxSettings(D('6'),D('7'),confirmed_by_user=True))
        save_rule(self.ledger,'s',newer)
        with self.assertRaisesRegex(ValueError,'изменилась после открытия'):
            change_rule(self.ledger,'s',JAN,None,expected=self.old)
        values=self.ledger.setting('s','tax_history');values[0]['usn']='8'
        self.ledger.set_setting('s','tax_history',values)
        with self.assertRaisesRegex(ValueError,'вне журнала'):
            undo_change(self.ledger,'s')
        self.assertEqual(load_rules(self.ledger,'s')[0].settings.usn_percent,D('8'))

    def test_atomic_failure_preserves_rates_legacy_summary_and_audit(self):
        save_rule(self.ledger,'s',self.old)
        before=self.ledger.setting('s','taxes')
        self.ledger.db.execute("CREATE TRIGGER fail_tax BEFORE INSERT ON settings WHEN NEW.name LIKE 'tax_change:%' BEGIN SELECT RAISE(ABORT,'test'); END")
        with self.assertRaises(sqlite3.DatabaseError):
            save_rule(self.ledger,'s',self.new)
        self.assertEqual(load_rules(self.ledger,'s'),[self.old])
        self.assertEqual(self.ledger.setting('s','taxes'),before)
        self.assertEqual(len(changes(self.ledger,'s')),1)
        self.assertEqual(self.ledger.setting('s','tax_change_sequence'),1)

    def test_legacy_versions_have_no_invented_events_and_mixed_store_archive_is_rejected(self):
        self.ledger.set_setting('s','taxes',{'usn':'4','vat':'5','included':True,'confirmed':True})
        self.assertEqual(load_rules(self.ledger,'s'),[])
        self.assertEqual(changes(self.ledger,'s'),[])
        save_rule(self.ledger,'s',self.old)
        name,value=changes(self.ledger,'s')[0]
        self.assertEqual(changes(self.ledger,'other'),[])
        good=self.ledger.backup(Path(self.folder.name)/'good.sqlite')
        value['store']='other';self.ledger.set_setting('s',name,value)
        bad=self.ledger.backup(Path(self.folder.name)/'bad.sqlite')
        self.ledger.restore(good)
        with self.assertRaises(BackupError):self.ledger.restore(bad)
        self.assertEqual(load_rules(self.ledger,'s'),[self.old])

    def test_native_selection_removal_undo_and_journal_preserve_exact_rule(self):
        window=Window(self.path);self.addCleanup(window.close)
        window.store.addItem('Магазин','s')
        save_rule(window.ledger,'s',self.old);window.refresh()
        window.tax_history.selectRow(0);window.select_tax_rule(0)
        from PySide6.QtCore import QDate
        self.assertEqual(window.tax_date.date(),QDate(2026,1,1))
        self.assertEqual(window.usn.text(),'4')
        window.remove_tax_rule()
        self.assertEqual(window.tax_history.rowCount(),0)
        self.assertEqual(window.usn.text(),'')
        self.assertFalse(window.tax_confirmed.isChecked())
        window.undo_tax_rule()
        self.assertEqual(window.tax_history.rowCount(),1)
        self.assertTrue(window.tax_confirmed.isChecked())
        dialog=TaxChangesDialog(window.ledger,'s');self.addCleanup(dialog.close)
        self.assertEqual(dialog.table.rowCount(),2)
        self.assertTrue(any(dialog.table.item(row,6).text()=='Отменено' for row in range(2)))
        window.close()
