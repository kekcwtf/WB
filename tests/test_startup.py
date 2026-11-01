import json
import os
from pathlib import Path
import subprocess
import sys
import tempfile
import unittest
from unittest.mock import patch
from datetime import date
from decimal import Decimal as D
from wb_profit.storage import Ledger
from wb_profit.finance import Kind, Operation
from wb_profit.startup import SessionLock, StartupError, active_database, check_startup, recover_startup
from wb_profit.backups import BackupError


class StartupTests(unittest.TestCase):
    def setUp(self):
        self.directory = tempfile.TemporaryDirectory()
        self.addCleanup(self.directory.cleanup)
        self.root = Path(self.directory.name)

    def backup(self):
        source = self.root / 'saved.sqlite'
        ledger = Ledger(source)
        ledger.set_setting('_app', 'stores', [{'id': 'shop', 'name': 'Мой магазин'}])
        ledger.set_setting('shop', 'costs', [{'sku': 'sku', 'date': '2026-01-01', 'amount': '940.25'}])
        ledger.upsert([Operation('shop', 'report', '1', date(2026, 1, 1), Kind.SALE, D('123.456789'), 'sku', 1)])
        ledger.close()
        return source

    def test_first_start_does_not_create_a_database_before_window_opens(self):
        path = check_startup(self.root)
        self.assertEqual(path, self.root / 'wb-profit.sqlite')
        self.assertFalse(path.exists())

    def test_corrupted_primary_database_is_detected_without_replacing_it(self):
        path = self.root / 'wb-profit.sqlite'
        path.write_bytes(b'corrupted original')
        with self.assertRaises(StartupError):
            check_startup(self.root)
        self.assertEqual(path.read_bytes(), b'corrupted original')

    def test_recovery_keeps_original_and_journals_and_reopens_restored_data(self):
        original = self.root / 'wb-profit.sqlite'
        original.write_bytes(b'corrupted')
        for suffix in ('-wal', '-shm', '-journal'):
            Path(str(original) + suffix).write_bytes(b'original' + suffix.encode())
        source = self.backup()
        source_bytes = source.read_bytes()
        recovered = recover_startup(self.root, source)
        self.assertNotEqual(recovered, original)
        self.assertEqual(original.read_bytes(), b'corrupted')
        for suffix in ('-wal', '-shm', '-journal'):
            self.assertEqual(Path(str(original) + suffix).read_bytes(), b'original' + suffix.encode())
        self.assertEqual(source.read_bytes(), source_bytes)
        self.assertEqual(check_startup(self.root), recovered)
        self.assertEqual(active_database(self.root), recovered)
        ledger = Ledger(recovered)
        try:
            self.assertEqual(ledger.operations('shop', date.min, date.max)[0].amount, D('123.456789'))
            self.assertEqual(ledger.setting('shop', 'costs')[0]['amount'], '940.25')
            self.assertEqual(ledger.db.execute('SELECT count(*) FROM source_records').fetchone()[0], 0)
        finally:
            ledger.close()

    def test_invalid_or_missing_pointer_never_creates_an_empty_replacement(self):
        pointer = self.root / 'active-database.json'
        for payload in ['not json', '{}', json.dumps({'database': '../outside.sqlite'}),
                        json.dumps({'database': 'wb-profit-recovered-' + 'a' * 32 + '.sqlite'})]:
            with self.subTest(payload=payload):
                pointer.write_text(payload, encoding='utf-8')
                with self.assertRaises(StartupError):
                    check_startup(self.root)
                self.assertFalse((self.root / 'wb-profit.sqlite').exists())

    def test_failed_pointer_write_keeps_previous_selection_and_both_databases(self):
        source = self.backup()
        first = recover_startup(self.root, source)
        pointer = self.root / 'active-database.json'
        old_pointer = pointer.read_bytes()
        real_replace = os.replace

        def fail_pointer(src, dst):
            if Path(dst) == pointer:
                raise OSError('cannot save active database')
            return real_replace(src, dst)

        with patch('wb_profit.startup.os.replace', side_effect=fail_pointer), self.assertRaises(OSError):
            recover_startup(self.root, source)
        self.assertEqual(pointer.read_bytes(), old_pointer)
        self.assertEqual(check_startup(self.root), first)
        self.assertEqual(len(list(self.root.glob('wb-profit-recovered-*.sqlite'))), 2)
        self.assertEqual(list(self.root.glob('.wb-active-*')), [])

    def test_invalid_backup_preserves_original_and_does_not_publish_pointer(self):
        original = self.root / 'wb-profit.sqlite'
        original.write_bytes(b'original')
        source = self.root / 'bad.sqlite'
        source.write_bytes(b'not sqlite')
        with self.assertRaises(BackupError):
            recover_startup(self.root, source)
        self.assertEqual(original.read_bytes(), b'original')
        self.assertFalse((self.root / 'active-database.json').exists())
        self.assertEqual(list(self.root.glob('wb-profit-recovered-*.sqlite')), [])

    def test_other_process_cannot_acquire_live_lock_and_can_after_release(self):
        lock = SessionLock(self.root)
        self.assertTrue(lock.acquire())
        script = ('import sys; from PySide6.QtCore import QCoreApplication; '
                  'from wb_profit.startup import SessionLock; app=QCoreApplication([]); '
                  'lock=SessionLock(sys.argv[1]); ok=lock.acquire(); lock.release() if ok else None; '
                  'sys.exit(0 if ok else 2)')
        try:
            process = subprocess.run([sys.executable, '-c', script, str(self.root)], timeout=15,
                                     capture_output=True, text=True)
            self.assertEqual(process.returncode, 2, process.stderr)
        finally:
            lock.release()
        process = subprocess.run([sys.executable, '-c', script, str(self.root)], timeout=15,
                                 capture_output=True, text=True)
        self.assertEqual(process.returncode, 0, process.stderr)

    def test_crashed_process_lock_is_reclaimed_without_deleting_active_process_lock(self):
        script = ('import os,sys; from PySide6.QtCore import QCoreApplication; '
                  'from wb_profit.startup import SessionLock; app=QCoreApplication([]); '
                  'lock=SessionLock(sys.argv[1]); assert lock.acquire(); os._exit(0)')
        process = subprocess.run([sys.executable, '-c', script, str(self.root)], timeout=15,
                                 capture_output=True, text=True)
        self.assertEqual(process.returncode, 0, process.stderr)
        self.assertTrue((self.root / '.wb-profit.lock').exists())
        lock = SessionLock(self.root)
        try:
            self.assertTrue(lock.acquire())
        finally:
            lock.release()
