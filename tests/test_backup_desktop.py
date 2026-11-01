import os
os.environ.setdefault('QT_QPA_PLATFORM', 'offscreen')
import tempfile
from contextlib import ExitStack
import unittest
import time
from datetime import date
from decimal import Decimal as D
from pathlib import Path
from unittest.mock import patch, Mock
from PySide6.QtCore import QDate, QTimer
from PySide6.QtWidgets import QApplication
from wb_profit.desktop import Window
from wb_profit.finance import Kind, Operation


class DesktopBackupTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.app = QApplication.instance() or QApplication([])

    def wait_for_maintenance(self, window):
        deadline = time.monotonic() + 10
        while time.monotonic() < deadline:
            self.app.processEvents()
            if window.maintenance is None:
                return
            time.sleep(0.005)  # Уступаем GIL потоку SQLite, не останавливая обработку Qt.
        self.fail('Фоновая операция не завершилась')

    def test_background_restore_reloads_stores_costs_taxes_theme_and_period(self):
        with tempfile.TemporaryDirectory() as folder, ExitStack() as resources:
            path = Path(folder) / 'live.sqlite'
            window = Window(path)
            resources.callback(window.close)
            window.stores = [{'id': 'saved', 'name': 'Сохранённый магазин'}]
            window.ledger.set_setting('_app', 'stores', window.stores)
            window.store.addItem('Сохранённый магазин', 'saved')
            window.tax_date.setDate(QDate(2026, 1, 1))
            window.usn.setText('4');window.vat.setText('5')
            window.tax_confirmed.setChecked(True);window.save_taxes()
            window.ledger.set_setting('saved', 'costs', [{'sku': 'sku', 'date': '2026-01-01', 'amount': '400'}])
            window.period.setCurrentIndex(6)
            window.start.setDate(QDate(2026, 1, 1));window.end.setDate(QDate(2026, 1, 7))
            window.dark_theme.setChecked(True)
            operation = Operation('saved', 'report', 'a', date(2026, 1, 1), Kind.SALE, D('1050'), 'sku', 1)
            window.ledger.upsert([operation])
            backup = Path(folder) / 'saved.sqlite'
            self.assertTrue(window.start_maintenance(backup))
            ticks=[]
            QTimer.singleShot(0,lambda:ticks.append('GUI жив'))
            self.wait_for_maintenance(window)
            self.assertTrue(ticks)
            self.assertTrue(backup.is_file())
            window.ledger.set_setting('_app', 'stores', [{'id': 'current', 'name': 'Другой магазин'}])
            window.ledger.set_setting('_app', 'dark', False)
            window.ledger.set_setting('saved', 'costs', [])
            window.reload_saved_state()
            self.assertEqual(window.current_store, 'current')
            with patch('wb_profit.desktop.save_token') as secret_write:
                self.assertTrue(window.start_maintenance(backup, restore=True))
                self.assertFalse(window.root.isEnabled())
                self.assertFalse(window.sync_timer.isActive())
                window.sync_all()
                self.assertIsNone(window.worker)
                self.wait_for_maintenance(window)
                secret_write.assert_not_called()
            self.assertEqual(window.current_store, 'saved')
            self.assertEqual(window.store.currentText(), 'Сохранённый магазин')
            self.assertEqual(window.usn.text(), '4')
            self.assertEqual(window.vat.text(), '5')
            self.assertTrue(window.dark_theme.isChecked())
            self.assertEqual(window.start.date(), QDate(2026, 1, 1))
            self.assertEqual(window.end.date(), QDate(2026, 1, 7))
            self.assertEqual(window.ledger.setting('saved', 'costs')[0]['amount'], '400')
            self.assertEqual(window.ledger.operations('saved', date.min, date.max), [operation])
            self.assertTrue(window.root.isEnabled())
            self.assertTrue(window.sync_timer.isActive())
            self.assertIn('Копия прежней базы:', window.sync_message.text())
            self.assertEqual(len(list((Path(folder) / 'backups').glob('*.sqlite'))), 1)
            window.close()
            reopened = Window(path)
            self.assertEqual(reopened.current_store, 'saved')
            self.assertEqual(reopened.products.rowCount(), 1)
            reopened.close()

    def test_invalid_restore_leaves_interface_and_data_available(self):
        with tempfile.TemporaryDirectory() as folder, ExitStack() as resources:
            window = Window(Path(folder) / 'live.sqlite')
            resources.callback(window.close)
            window.ledger.set_setting('_app', 'sentinel', 'untouched')
            bad = Path(folder) / 'bad.sqlite';bad.write_bytes(b'broken')
            with patch.object(window, 'error') as error:
                window.start_maintenance(bad, restore=True)
                self.wait_for_maintenance(window)
                error.assert_called_once()
            self.assertEqual(window.ledger.setting('_app', 'sentinel'), 'untouched')
            self.assertTrue(window.root.isEnabled())
            self.assertTrue(window.sync_timer.isActive())
            self.assertFalse((Path(folder) / 'backups').exists())

    def test_sync_and_maintenance_cannot_start_together(self):
        with tempfile.TemporaryDirectory() as folder, ExitStack() as resources:
            window = Window(Path(folder) / 'live.sqlite')
            resources.callback(window.close)
            window.worker = Mock()
            with patch.object(window, 'error') as error:
                self.assertFalse(window.start_maintenance(Path(folder) / 'copy.sqlite'))
                error.assert_called_once()
                self.assertIsNone(window.maintenance)
            window.worker = None

    def test_in_progress_disk_operation_prevents_window_destruction(self):
        with tempfile.TemporaryDirectory() as folder:
            window = Window(Path(folder) / 'live.sqlite')
            window.show()
            window.maintenance = Mock()
            self.assertFalse(window.close())
            self.assertTrue(window.isVisible())
            self.assertIn('Дождитесь завершения', window.sync_message.text())
            window.maintenance = None
            self.assertTrue(window.close())
