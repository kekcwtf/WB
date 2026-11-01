import os
os.environ.setdefault('QT_QPA_PLATFORM','offscreen')
from pathlib import Path
import tempfile
import unittest
from unittest.mock import patch,Mock
import time
from PySide6.QtCore import QTimer
from PySide6.QtTest import QTest
from PySide6.QtWidgets import QApplication
from wb_profit.desktop import Window


class AutomaticBackupDesktopTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):cls.app=QApplication.instance() or QApplication([])

    def setUp(self):
        self.folder=tempfile.TemporaryDirectory();self.addCleanup(self.folder.cleanup)
        self.path=Path(self.folder.name)/'db.sqlite'
        self.window=Window(self.path);self.addCleanup(self.window.close)
        self.window.stores=[{'id':'s','name':'Магазин'}]
        self.window.ledger.set_setting('_app','stores',self.window.stores)
        self.window.store.addItem('Магазин','s')

    def wait(self):
        for _ in range(500):
            self.app.processEvents()
            if self.window.auto_backup_worker is None:return
            QTest.qWait(5)
        self.fail('Автокопия не завершилась')

    def test_automatic_snapshot_keeps_gui_responsive_and_status_and_options_survive_restart(self):
        from wb_profit.auto_backups import write_backup
        ticks=[]
        def slow_copy(*args,**kwargs):time.sleep(0.06);return write_backup(*args,**kwargs)
        timer=QTimer();timer.setInterval(5);timer.timeout.connect(lambda:ticks.append(True))
        with patch('wb_profit.auto_backups.write_backup',slow_copy):
            timer.start();self.window.maybe_auto_backup();self.wait();timer.stop()
        self.assertGreater(len(ticks),2)
        self.assertTrue(self.window.root.isEnabled())
        self.assertIn('Последняя успешная',self.window.auto_backup_status.text())
        state=self.window.ledger.setting('_app','auto_backup_state')
        self.assertTrue((self.path.parent/'backups'/'automatic'/state['filename']).is_file())
        self.window.auto_backup_enabled.setChecked(False)
        self.window.auto_backup_keep.setValue(10)
        self.window.auto_backup_hours.setValue(6)
        self.app.processEvents()
        self.window.close();reopened=Window(self.path)
        self.assertFalse(reopened.auto_backup_enabled.isChecked())
        self.assertEqual(reopened.auto_backup_keep.value(),10)
        self.assertEqual(reopened.auto_backup_hours.value(),6)
        reopened.close()

    def test_sync_request_waits_for_snapshot_and_resumes_after_failure_without_copy_loop(self):
        with patch('wb_profit.auto_backups.write_backup',side_effect=OSError('disk full')),patch('wb_profit.desktop.SyncWorker') as sync:
            self.window.start_sync(['s'],force=True)
            sync.assert_not_called()
            self.assertIsNotNone(self.window.auto_backup_worker)
            self.wait()
            sync.assert_called_once()
            self.assertTrue(sync.call_args.kwargs['force'])
            self.assertIsNone(self.window.after_backup_sync)
            self.assertFalse(self.window.backup_warning.isHidden())
            self.assertIn('Автокопия не создана',self.window.backup_warning.text())
            self.assertIsNone(self.window.ledger.setting('_app','auto_backup_state'))
        self.window.worker=None

    def test_sync_and_restore_block_starting_automatic_copy_and_close_stops_timer(self):
        self.window.worker=Mock()
        self.window.maybe_auto_backup()
        self.assertIsNone(self.window.auto_backup_worker)
        self.window.worker=None;self.window.maintenance=Mock()
        self.window.maybe_auto_backup()
        self.assertIsNone(self.window.auto_backup_worker)
        self.window.maintenance=None
        self.window.close()
        self.assertFalse(self.window.auto_backup_timer.isActive())

    def test_close_cancels_active_snapshot_without_destroying_running_thread(self):
        from wb_profit.auto_backups import write_backup
        def slow_copy(*args,**kwargs):time.sleep(0.04);return write_backup(*args,**kwargs)
        with patch('wb_profit.auto_backups.write_backup',slow_copy):
            self.window.maybe_auto_backup()
            worker=self.window.auto_backup_worker
            self.assertTrue(self.window.close())
            self.assertTrue(worker.wait(1000))
            self.app.processEvents()
        self.assertFalse(self.window.auto_backup_timer.isActive())
