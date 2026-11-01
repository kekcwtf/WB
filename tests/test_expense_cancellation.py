import os
os.environ.setdefault('QT_QPA_PLATFORM', 'offscreen')
from dataclasses import replace
from datetime import date
from decimal import Decimal as D
from pathlib import Path
import sqlite3
import tempfile
import unittest
from PySide6.QtCore import Qt, QTimer
from PySide6.QtWidgets import QApplication, QDialog, QPushButton
from wb_profit.finance import Operation, Kind, Cost, TaxSettings, calculate
from wb_profit.storage import Ledger
from wb_profit.expenses import (save_external_expense, cancel_external_expense, cancelled_expenses,
                                restore_external_expense, CANCELLED_PREFIX)
from wb_profit.expense_archive_view import ExpenseArchiveDialog
from wb_profit.profit_distribution import distribute_profit
from wb_profit.taxes import TaxRule
from wb_profit.backups import BackupError
from wb_profit.desktop import Window


class ExpenseCancellationTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.app = QApplication.instance() or QApplication([])

    def setUp(self):
        self.folder = tempfile.TemporaryDirectory()
        self.addCleanup(self.folder.cleanup)
        self.path = Path(self.folder.name) / 'db.sqlite'
        self.ledger = Ledger(self.path)
        self.addCleanup(self.ledger.close)
        self.day = date(2026, 1, 1)
        self.identifier = save_external_expense(self.ledger, 's', self.day, D('100'), 'Упаковка')

    def operations(self, store='s'):
        return self.ledger.operations(store, date.min, date.max)

    def test_cancel_and_restore_change_profit_once_and_preserve_store_sku_conservation(self):
        self.ledger.upsert([Operation('s', 'wb_finance', 'sale', self.day, Kind.SALE, D('1000'), 'a', 1)])
        costs = [Cost('a', self.day, D('400'))]
        rules = [TaxRule(self.day, TaxSettings(D('0'), D('0'), confirmed_by_user=True))]
        def result():
            distribution = distribute_profit(self.operations(), costs, rules, coverage_confirmed=True)
            self.assertEqual(distribution.difference, D('0'))
            return distribution.store_net
        self.assertEqual(result(), D('500'))
        self.assertTrue(cancel_external_expense(self.ledger, 's', self.identifier))
        self.assertEqual(result(), D('600'))
        self.assertTrue(restore_external_expense(self.ledger, 's', self.identifier))
        self.assertEqual(result(), D('500'))

    def test_repeated_actions_are_idempotent_and_versions_are_preserved(self):
        self.assertTrue(cancel_external_expense(self.ledger, 's', self.identifier))
        self.assertFalse(cancel_external_expense(self.ledger, 's', self.identifier))
        self.assertEqual(len(cancelled_expenses(self.ledger, 's')), 1)
        self.assertEqual(self.ledger.db.execute('SELECT COUNT(*) FROM revisions').fetchone()[0], 1)
        self.assertTrue(restore_external_expense(self.ledger, 's', self.identifier))
        self.assertFalse(restore_external_expense(self.ledger, 's', self.identifier))
        self.assertEqual(len(self.operations()), 1)
        self.assertEqual(cancelled_expenses(self.ledger, 's'), [])
        self.assertEqual(self.ledger.setting('s', 'external_expense:'+self.identifier)['description'], 'Упаковка')
        self.assertTrue(cancel_external_expense(self.ledger, 's', self.identifier))
        self.assertEqual(self.ledger.db.execute('SELECT COUNT(*) FROM revisions').fetchone()[0], 2)

    def test_stale_editor_cannot_cancel_changed_expense_or_resurrect_cancelled_record(self):
        old = self.operations()[0]
        save_external_expense(self.ledger, 's', self.day, D('200'), 'Аренда', identifier=self.identifier)
        with self.assertRaisesRegex(ValueError, 'изменился'):
            cancel_external_expense(self.ledger, 's', self.identifier, expected=old)
        self.assertEqual(self.operations()[0].amount, D('-200'))
        cancel_external_expense(self.ledger, 's', self.identifier)
        with self.assertRaisesRegex(ValueError, 'отменён'):
            save_external_expense(self.ledger, 's', self.day, D('100'), 'Упаковка', identifier=self.identifier)
        self.assertEqual(self.operations(), [])

    def test_store_and_source_isolation_and_forged_archive_are_enforced(self):
        wb = Operation('s', 'wb_finance', self.identifier, self.day, Kind.COMMISSION, D('-20'))
        self.ledger.upsert([wb])
        self.assertFalse(cancel_external_expense(self.ledger, 'other', self.identifier))
        cancel_external_expense(self.ledger, 's', self.identifier)
        self.assertEqual(self.operations(), [wb])
        value = self.ledger.setting('s', CANCELLED_PREFIX+self.identifier)
        value['operation']['source'] = 'wb_finance'
        self.ledger.set_setting('s', CANCELLED_PREFIX+self.identifier, value)
        with self.assertRaises(ValueError):
            restore_external_expense(self.ledger, 's', self.identifier)
        self.assertEqual(self.operations(), [wb])

    def test_transaction_failure_rolls_back_operation_archive_and_revision(self):
        self.ledger.db.execute("CREATE TRIGGER fail_archive BEFORE INSERT ON settings WHEN NEW.name LIKE 'cancelled_external_expense:%' BEGIN SELECT RAISE(ABORT,'test'); END")
        with self.assertRaises(sqlite3.DatabaseError):
            cancel_external_expense(self.ledger, 's', self.identifier)
        self.assertEqual(len(self.operations()), 1)
        self.assertEqual(self.ledger.db.execute('SELECT COUNT(*) FROM revisions').fetchone()[0], 0)
        self.assertIsNone(self.ledger.setting('s', CANCELLED_PREFIX+self.identifier))
        self.ledger.db.execute('DROP TRIGGER fail_archive')
        cancel_external_expense(self.ledger, 's', self.identifier)
        self.ledger.db.execute("CREATE TRIGGER fail_restore BEFORE INSERT ON operations BEGIN SELECT RAISE(ABORT,'test'); END")
        with self.assertRaises(sqlite3.DatabaseError):
            restore_external_expense(self.ledger, 's', self.identifier)
        self.assertEqual(self.operations(), [])
        self.assertIsNone(self.ledger.setting('s', CANCELLED_PREFIX+self.identifier)['restored_at'])

    def test_backup_preserves_cancelled_expense_and_rejects_wrong_store_archive(self):
        cancel_external_expense(self.ledger, 's', self.identifier)
        good = self.ledger.backup(Path(self.folder.name)/'good.sqlite')
        self.ledger.restore(good)
        self.assertEqual(len(cancelled_expenses(self.ledger, 's')), 1)
        self.assertEqual(self.operations(), [])
        value = self.ledger.setting('s', CANCELLED_PREFIX+self.identifier)
        value['operation']['store'] = 'other'
        self.ledger.set_setting('s', CANCELLED_PREFIX+self.identifier, value)
        bad = self.ledger.backup(Path(self.folder.name)/'bad.sqlite')
        self.ledger.restore(good)
        with self.assertRaises(BackupError):
            self.ledger.restore(bad)
        self.assertEqual(len(cancelled_expenses(self.ledger, 's')), 1)

    def test_native_archive_restores_exact_selected_record_after_sort(self):
        other = save_external_expense(self.ledger, 's', self.day, D('50'), 'Доставка упаковки', 'a')
        for identifier in (self.identifier, other):
            cancel_external_expense(self.ledger, 's', identifier)
        dialog = ExpenseArchiveDialog(self.ledger, 's')
        self.addCleanup(dialog.close)
        changed = []
        dialog.changed.connect(lambda: changed.append(True))
        dialog.table.sortItems(4, Qt.SortOrder.AscendingOrder)
        dialog.table.selectRow(0)
        dialog.restore_selected()
        self.assertEqual([op.identifier for op in self.operations()], [other])
        self.assertEqual(dialog.table.rowCount(), 1)
        self.assertEqual(changed, [True])
        self.assertEqual(self.operations()[0].sku, 'a')

    def test_edit_dialog_can_cancel_expense_and_refresh_without_modifying_wb(self):
        window = Window(self.path)
        self.addCleanup(window.close)
        window.store.addItem('Магазин', 's')
        operation = self.operations()[0]
        clicked = []
        def click():
            for button in window.findChildren(QPushButton):
                if button.text() == 'Отменить этот внешний расход':
                    clicked.append(True)
                    button.click()
                    return
        guard = QTimer(window)
        guard.setSingleShot(True)
        guard.timeout.connect(lambda: [dialog.reject() for dialog in window.findChildren(QDialog)])
        guard.start(1000)
        QTimer.singleShot(0, click)
        window.external_expense_dialog(operation)
        guard.stop()
        self.assertEqual(clicked, [True])
        self.assertEqual(self.operations(), [])
        self.assertIn('отменён', window.sync_message.text())
        window.close()
