import os
os.environ.setdefault('QT_QPA_PLATFORM','offscreen')
from pathlib import Path
import tempfile
import unittest
from unittest.mock import patch,Mock
from datetime import datetime,timedelta,timezone
from PySide6.QtWidgets import QApplication
from PySide6.QtNetwork import QNetworkInformation
from wb_profit.desktop import Window
from wb_profit.retries import save_failure,PREFIX
from wb_profit.api.transport import ApiError


class RetryDesktopTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):cls.app=QApplication.instance() or QApplication([])

    def setUp(self):
        self.folder=tempfile.TemporaryDirectory();self.addCleanup(self.folder.cleanup)
        self.window=Window(Path(self.folder.name)/'db.sqlite');self.addCleanup(self.window.close)
        self.window.stores=[{'id':'shop','name':'Магазин'}]
        self.window.ledger.set_setting('_app','stores',self.window.stores)
        self.window.store.addItem('Магазин','shop')

    def test_disconnected_manual_refresh_is_deferred_and_online_resumes_once(self):
        self.window.network_changed(QNetworkInformation.Reachability.Disconnected)
        with patch('wb_profit.desktop.SyncWorker') as worker:
            self.window.sync_current()
            worker.assert_not_called()
            self.assertTrue(self.window.deferred_sync)
            self.assertIn('Сеть недоступна',self.window.connection_status.text())
        with patch.object(self.window,'start_sync') as start:
            self.window.network_changed(QNetworkInformation.Reachability.Online)
            self.assertFalse(self.window.deferred_sync)
            start.assert_called_once_with(['shop'])

    def test_due_retry_selects_only_failed_sources_and_maintenance_blocks_it(self):
        save_failure(self.window.ledger,'shop','sales',ApiError('offline',retryable=True,kind='network'),
                     now=datetime.now(timezone.utc)-timedelta(minutes=2))
        with patch.object(self.window,'start_sync') as start:
            self.window.retry_pending()
            start.assert_called_once_with(['shop'],retry_only=True)
            start.reset_mock();self.window.maintenance=Mock()
            self.window.retry_pending();start.assert_not_called()
            self.window.maintenance=None

    def test_network_signal_does_not_override_server_cooldown_or_query_during_worker(self):
        now=datetime.now(timezone.utc)
        plan=save_failure(self.window.ledger,'shop','sales',ApiError('offline',retryable=True,kind='network'),
                          now=now,rate_until=(now+timedelta(minutes=5)).timestamp())
        self.window.network_changed(QNetworkInformation.Reachability.Disconnected)
        with patch.object(self.window,'start_sync') as start:
            self.window.network_changed(QNetworkInformation.Reachability.Online)
            start.assert_not_called()
            self.assertEqual(self.window.ledger.setting('shop',PREFIX+'sales')['not_before'],plan['not_before'])
        self.window.worker=Mock()
        with patch('wb_profit.desktop.due_shops') as select:
            self.window.retry_pending();select.assert_not_called()
        self.window.worker=None

    def test_retry_timer_stops_on_close(self):
        self.assertEqual(self.window.retry_timer.interval(),60000)
        self.assertTrue(self.window.retry_timer.isActive())
        self.window.close()
        self.assertFalse(self.window.retry_timer.isActive())
