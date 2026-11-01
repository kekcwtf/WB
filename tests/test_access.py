import os
os.environ.setdefault('QT_QPA_PLATFORM', 'offscreen')
import base64
import json
from datetime import datetime, timedelta, timezone
from pathlib import Path
import tempfile
import unittest
from unittest.mock import patch
from PySide6.QtWidgets import QApplication
from wb_profit.api.auth import inspect_token
from wb_profit.access import save_access, load_access, source_permission, SOURCE_CATEGORIES, validate_access
from wb_profit.access_view import AccessView
from wb_profit.sync_service import SOURCE_LABELS
from wb_profit.storage import Ledger
from wb_profit.desktop import Window


def token(mask=1 << 13, **overrides):
    data = {'s': mask, 'acc': 3, 'exp': 4102444800}
    data.update(overrides)
    payload = base64.urlsafe_b64encode(json.dumps(data).encode()).decode().rstrip('=')
    return 'e30.' + payload + '.synthetic-test-signature'


class AccessTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.app = QApplication.instance() or QApplication([])

    def setUp(self):
        self.folder = tempfile.TemporaryDirectory()
        self.addCleanup(self.folder.cleanup)
        self.path = Path(self.folder.name) / 'db.sqlite'
        self.ledger = Ledger(self.path)
        self.addCleanup(self.ledger.close)

    def test_only_allowlisted_metadata_is_saved_and_old_profile_needs_no_new_token(self):
        self.assertIsNone(load_access(self.ledger, 's'))
        value = token()
        save_access(self.ledger, 's', inspect_token(value))
        stored = self.ledger.setting('s', 'api_access')
        self.assertEqual(set(stored), {'mask', 'account_type', 'expires_at', 'recorded_at'})
        self.assertNotIn(value, json.dumps(stored))
        self.assertTrue(load_access(self.ledger, 's').permits('finance'))
        self.assertIsNone(load_access(self.ledger, 'other'))
        with self.assertRaises(ValueError):
            validate_access(dict(stored, unexpected_secret='forbidden'))

    def test_source_categories_and_basic_account_restriction_are_explicit(self):
        self.assertEqual(set(SOURCE_CATEGORIES), set(SOURCE_LABELS))
        claims = inspect_token(token())
        self.assertIn('подтверждение API', source_permission(claims, 'finance_weekly')[1])
        self.assertIn('Статистика', source_permission(claims, 'orders')[1])
        self.assertIn('Аналитика', source_permission(claims, 'stocks')[1])
        self.assertIn('Продвижение', source_permission(claims, 'advertising')[1])
        basic = inspect_token(token(acc=1))
        self.assertIn('персональный', source_permission(basic, 'report_totals')[1])
        self.assertIn('подтверждение API', source_permission(basic, 'tariffs')[1])

    def test_expiration_warning_is_recomputed_without_reading_vault_or_calling_api(self):
        now = datetime(2026, 1, 1, tzinfo=timezone.utc)
        value = token(exp=int((now + timedelta(days=6)).timestamp()))
        save_access(self.ledger, 's', inspect_token(value), now=now)
        view = AccessView(SOURCE_LABELS)
        self.addCleanup(view.close)
        with patch('wb_profit.secrets.backend', side_effect=AssertionError('Секрет читать нельзя')):
            view.refresh(self.ledger, 's', now=now)
            self.assertIn('ближайшие 7 дней', view.summary.text())
            self.assertIn('03:00', view.summary.text())
            view.refresh(self.ledger, 's', now=now+timedelta(days=7))
            self.assertIn('Срок истёк', view.summary.text())
            self.assertIn('#c0392b', view.summary.styleSheet())
            statuses = [view.table.item(i, 2).text() for i in range(view.table.rowCount())]
            self.assertTrue(all('истёк' in message for message in statuses))

    def test_missing_or_invalid_summary_never_claims_server_permission(self):
        view = AccessView(SOURCE_LABELS)
        self.addCleanup(view.close)
        view.refresh(self.ledger, 's')
        self.assertIn('повторно не требуется', view.summary.text())
        self.ledger.set_setting('s', 'api_access', {'mask': True})
        view.refresh(self.ledger, 's')
        self.assertTrue(all('не проверен' in view.table.item(i, 2).text() for i in range(view.table.rowCount())))

    def test_unrepresentable_expiry_and_bad_mask_fail_with_sanitized_error(self):
        for value in [token(exp=10**100), token(mask=True), token(mask=-1), 'private-secret-is-not-a-token']:
            with self.assertRaises(ValueError) as caught:
                inspect_token(value)
            self.assertNotIn(value, str(caught.exception))
            self.assertIn('Не удалось прочитать', str(caught.exception))

    def test_save_token_updates_metadata_and_never_stores_secret_in_database(self):
        window = Window(self.path)
        self.addCleanup(window.close)
        window.store.addItem('Магазин', 's')
        value = token()
        with patch('wb_profit.desktop.QInputDialog.getText', return_value=('  ' + value + '  ', True)), \
             patch('wb_profit.desktop.save_token') as save:
            window.store_token()
            save.assert_called_once_with('s', value)
        self.assertTrue(load_access(window.ledger, 's').permits('finance'))
        self.assertNotIn(value, '\n'.join(row[0] for row in window.ledger.db.execute('SELECT payload FROM settings')))
        self.assertIn('Заявленные категории', window.sync_message.text())
        self.assertEqual(window.access_view.table.rowCount(), len(SOURCE_LABELS))
        window.close()

    def test_invalid_expired_or_sandbox_token_does_not_replace_saved_access(self):
        window = Window(self.path)
        self.addCleanup(window.close)
        window.store.addItem('Магазин', 's')
        save_access(window.ledger, 's', inspect_token(token()))
        before = window.ledger.setting('s', 'api_access')
        for value in ['bad-secret', token(exp=1), token(t=True)]:
            with patch('wb_profit.desktop.QInputDialog.getText', return_value=(value, True)), \
                 patch('wb_profit.desktop.save_token') as save, patch.object(window, 'error') as error:
                window.store_token()
                save.assert_not_called()
                error.assert_called_once()
                self.assertNotIn(value, error.call_args.args[0])
        self.assertEqual(window.ledger.setting('s', 'api_access'), before)
        window.close()

    def test_rotation_during_sync_or_database_work_is_deferred_before_password_dialog(self):
        window = Window(self.path)
        self.addCleanup(window.close)
        window.store.addItem('Магазин', 's')
        for attribute in ('worker', 'maintenance', 'auto_backup_worker'):
            setattr(window, attribute, object())
            try:
                with patch('wb_profit.desktop.QInputDialog.getText') as ask, \
                     patch('wb_profit.desktop.save_token') as save, patch.object(window, 'error') as error:
                    window.store_token()
                    ask.assert_not_called()
                    save.assert_not_called()
                    self.assertIn('Дождитесь', error.call_args.args[0])
            finally:
                setattr(window, attribute, None)
        window.close()
