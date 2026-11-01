import json
import os
import sqlite3
import tempfile
import unittest
from contextlib import closing
from dataclasses import replace
from datetime import date
from decimal import Decimal as D
from pathlib import Path
from unittest.mock import patch
from wb_profit.backups import BackupError
from wb_profit.finance import Operation, Kind
from wb_profit.storage import Ledger
from wb_profit.sync import SyncStore, Page, Record


class BackupTests(unittest.TestCase):
    def setUp(self):
        self.folder = tempfile.TemporaryDirectory()
        self.addCleanup(self.folder.cleanup)
        self.root = Path(self.folder.name)
        self.path = self.root / 'working.sqlite'
        self.ledger = Ledger(self.path)
        self.addCleanup(self.ledger.close)
        self.operation = Operation('shop', 'report', '1', date(2026, 1, 1), Kind.SALE,
                                   D('123.456789'), 'sku', 1)
        self.ledger.set_setting('_app', 'stores', [{'id': 'shop', 'name': 'Магазин'}])
        self.ledger.upsert([self.operation])

    def operations(self, ledger=None):
        return (ledger or self.ledger).operations('shop', date.min, date.max)

    def test_full_restore_includes_raw_history_cursors_settings_and_safety_copy(self):
        cache = SyncStore(self.path)
        self.addCleanup(cache.close)
        self.ledger.set_setting('shop', 'costs', [{'sku': 'sku', 'date': '2026-01-01', 'amount': '940.25'}])
        self.ledger.set_setting('shop', 'tax_history', [{'date': '2026-01-01', 'usn': '4', 'vat': '5', 'included': True, 'confirmed': True}])
        cache.save_page('shop', 'orders', Page((Record('raw', {'amount': D('42.123456')}),), 'next', False))
        backup = self.ledger.backup(self.root / 'saved.sqlite')
        self.assertFalse(Path(str(backup) + '-wal').exists())
        with closing(sqlite3.connect(backup)) as saved:
            self.assertEqual(saved.execute('PRAGMA journal_mode').fetchone()[0], 'delete')
        changed = replace(self.operation, amount=D('99.999999'))
        self.ledger.upsert([changed])
        self.ledger.set_setting('shop', 'costs', [])
        cache.save_page('shop', 'orders', Page((Record('raw', {'amount': D('99')}),), None, True))
        safety = self.ledger.restore(backup)
        self.assertTrue(safety.is_file())
        self.assertEqual(safety.parent.name, 'backups')
        self.assertEqual(self.operations(), [self.operation])
        self.assertEqual(self.ledger.setting('shop', 'costs')[0]['amount'], '940.25')
        self.assertEqual(self.ledger.setting('shop', 'tax_history')[0]['usn'], '4')
        self.assertEqual(cache.state('shop', 'orders')['cursor'], 'next')
        self.assertFalse(cache.state('shop', 'orders')['completed'])
        payload = cache.db.execute("SELECT payload FROM source_records WHERE identifier='raw'").fetchone()[0]
        self.assertEqual(json.loads(payload)['amount'], '42.123456')
        previous = Ledger(safety)
        try:
            self.assertEqual(self.operations(previous), [changed])
            self.assertEqual(previous.db.execute('SELECT count(*) FROM revisions').fetchone()[0], 1)
            self.assertEqual(previous.setting('shop', 'costs'), [])
        finally:
            previous.close()
        self.assertEqual(self.ledger.upsert([self.operation]), (0, 0, 1))

    def test_backup_failure_preserves_previous_file_and_cleans_staging(self):
        backup = self.ledger.backup(self.root / 'saved.sqlite')
        original = backup.read_bytes()
        self.ledger.upsert([replace(self.operation, amount=D('1'))])
        with patch('wb_profit.backups.os.replace', side_effect=OSError('disk unavailable')):
            with self.assertRaises(OSError):
                self.ledger.backup(backup)
        self.assertEqual(backup.read_bytes(), original)
        self.assertEqual(list(self.root.glob('.wb-backup-*')), [])
        self.assertEqual(self.operations()[0].amount, D('1'))

    def test_live_path_symlink_and_hardlink_are_rejected(self):
        aliases = [self.path, self.path.parent / '.' / self.path.name]
        hardlink = self.root / 'hardlink.sqlite'
        os.link(self.path, hardlink)
        aliases.append(hardlink)
        symlink = self.root / 'symlink.sqlite'
        try:
            symlink.symlink_to(self.path)
        except OSError:
            pass  # Windows без привилегии создания symlink всё равно проверяет hardlink.
        else:
            aliases.append(symlink)
        for path in aliases:
            with self.subTest(path=path):
                with self.assertRaises(BackupError):
                    self.ledger.backup(path)
                with self.assertRaises(BackupError):
                    self.ledger.restore(path)
        self.assertEqual(self.operations(), [self.operation])

    def test_invalid_files_leave_current_data_untouched(self):
        for name, content in [('corrupted.sqlite', b'not sqlite'), ('empty.sqlite', b'')]:
            source = self.root / name
            source.write_bytes(content)
            with self.subTest(name=name), self.assertRaises(BackupError):
                self.ledger.restore(source)
            self.assertEqual(self.operations(), [self.operation])
        missing = self.root / 'missing.sqlite'
        with self.assertRaises(BackupError):
            self.ledger.restore(missing)
        self.assertFalse(missing.exists())
        self.assertFalse((self.root / 'backups').exists())

    def test_invalid_payload_and_conflicting_cost_history_are_rejected_before_safety_copy(self):
        source = self.ledger.backup(self.root / 'bad.sqlite')
        with closing(sqlite3.connect(source)) as db, db:
            db.execute("UPDATE operations SET payload='{}'")
        with self.assertRaises(BackupError):
            self.ledger.restore(source)
        self.assertEqual(self.operations(), [self.operation])
        source = self.ledger.backup(source)
        with closing(sqlite3.connect(source)) as db, db:
            costs = [{'sku': 'sku', 'date': '2026-01-01', 'amount': amount} for amount in ['100', '200']]
            db.execute('INSERT INTO settings VALUES(?,?,?)', ('shop', 'costs', json.dumps(costs)))
        with self.assertRaises(BackupError):
            self.ledger.restore(source)
        self.assertFalse((self.root / 'backups').exists())

    def test_triggers_and_incompatible_schema_are_not_imported(self):
        source = self.ledger.backup(self.root / 'foreign.sqlite')
        with closing(sqlite3.connect(source)) as db, db:
            db.execute('CREATE TRIGGER surprise AFTER INSERT ON operations BEGIN DELETE FROM settings; END')
        with self.assertRaises(BackupError):
            self.ledger.restore(source)
        self.assertEqual(self.operations(), [self.operation])
        with closing(sqlite3.connect(source)) as db, db:
            db.execute('DROP TRIGGER surprise')
            db.execute('ALTER TABLE settings ADD COLUMN future_version TEXT')
        with self.assertRaises(BackupError):
            self.ledger.restore(source)

    def test_cannot_overwrite_a_database_with_sqlite_sidecars(self):
        target = self.root / 'busy.sqlite'
        target.write_bytes(b'previous')
        Path(str(target) + '-wal').write_bytes(b'active')
        with self.assertRaises(BackupError):
            self.ledger.backup(target)
        self.assertEqual(target.read_bytes(), b'previous')

    def test_failed_restore_keeps_current_data_and_a_readable_safety_copy(self):
        source = self.ledger.backup(self.root / 'saved.sqlite')
        changed = replace(self.operation, amount=D('99'))
        self.ledger.upsert([changed])
        from wb_profit.backups import _copy

        interrupted=[]
        def fail_last(incoming, destination):
            if destination is self.ledger.db:
                def interrupt(status, remaining, total):
                    if remaining>0:
                        interrupted.append((remaining,total))
                        raise sqlite3.OperationalError('interrupted after first page')
                return incoming.backup(destination,pages=1,progress=interrupt)
            return _copy(incoming, destination)

        with patch('wb_profit.backups._copy', side_effect=fail_last), self.assertRaises(BackupError):
            self.ledger.restore(source)
        self.assertEqual(self.operations(), [changed])
        self.assertTrue(interrupted) # реально начатый SQLite backup был прерван до завершения
        saved = list((self.root / 'backups').glob('before-restore-*.sqlite'))
        self.assertEqual(len(saved), 1)
        previous = Ledger(saved[0])
        try:
            self.assertEqual(self.operations(previous), [changed])
        finally:
            previous.close()

    def test_no_uncommitted_backup_can_hang_or_publish_incomplete_data(self):
        self.ledger.db.execute("UPDATE operations SET payload=payload")
        with self.assertRaises(BackupError):
            self.ledger.backup(self.root / 'uncommitted.sqlite')
        self.ledger.db.rollback()
        self.assertFalse((self.root / 'uncommitted.sqlite').exists())

    def test_legacy_core_snapshot_prepares_cache_before_replacing_live_database(self):
        source = self.ledger.backup(self.root / 'legacy.sqlite')
        original_bytes = source.read_bytes()
        changed = replace(self.operation, amount=D('17'))
        self.ledger.upsert([changed])
        with patch('wb_profit.sync.SyncStore', side_effect=sqlite3.OperationalError('migration failed')):
            with self.assertRaises(BackupError):
                self.ledger.restore(source)
        self.assertEqual(self.operations(), [changed])
        self.assertFalse((self.root / 'backups').exists())
        self.ledger.restore(source)
        self.assertEqual(self.operations(), [self.operation])
        self.assertEqual(self.ledger.db.execute('SELECT count(*) FROM source_records').fetchone()[0], 0)
        self.assertEqual(source.read_bytes(), original_bytes)

    def test_destination_write_lock_fails_without_replacing_current_file(self):
        source = self.ledger.backup(self.root / 'source.sqlite')
        changed = replace(self.operation, amount=D('57'))
        self.ledger.upsert([changed])
        from wb_profit.backups import _copy
        lock = sqlite3.connect(self.path)

        def start_lock_then_restore(incoming, destination):
            if destination is self.ledger.db:
                lock.execute('BEGIN IMMEDIATE')
                # Не ждём реальные 30 секунд, но используем настоящий SQLITE_BUSY.
                with patch('wb_profit.backups.time.monotonic', side_effect=[100, 131]):
                    return _copy(incoming, destination)
            return _copy(incoming, destination)

        try:
            with patch('wb_profit.backups._copy', side_effect=start_lock_then_restore), self.assertRaises(BackupError):
                self.ledger.restore(source)
        finally:
            lock.rollback()
            lock.close()
        self.assertEqual(self.operations(), [changed])
