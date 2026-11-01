import tempfile
import unittest
from dataclasses import replace
from datetime import date
from decimal import Decimal
from pathlib import Path
from wb_profit.finance import Operation, Kind
from wb_profit.storage import Ledger


class StorageTests(unittest.TestCase):
    def test_repeat_sync_correction_backup_and_restart(self):
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / 'ledger.sqlite'
            ledger = Ledger(path)
            operation = Operation('shop', 'report', '1', date(2026, 1, 1), Kind.SALE, Decimal('123.456789'), 'sku', 1)
            self.assertEqual(ledger.upsert([operation]), (1, 0, 0))
            self.assertEqual(ledger.upsert([operation]), (0, 0, 1))
            corrected = replace(operation, amount=Decimal('120.123456'))
            self.assertEqual(ledger.upsert([corrected]), (0, 1, 0))
            self.assertEqual(ledger.db.execute('SELECT COUNT(*) FROM revisions').fetchone()[0], 1)
            ledger.set_setting('shop', 'costs', {'sku': '940.00'})
            ledger.backup(Path(directory) / 'backup.sqlite')
            ledger.close()
            for target in [path, Path(directory) / 'backup.sqlite']:
                reopened = Ledger(target)
                self.assertEqual(reopened.operations('shop', date(2025, 1, 1), date(2027, 1, 1)), [corrected])
                self.assertEqual(reopened.setting('shop', 'costs'), {'sku': '940.00'})
                self.assertEqual(reopened.operations('another', date.min, date.max), [])
                reopened.close()

    def test_tokens_not_stored_in_settings(self):
        ledger = Ledger(':memory:')
        with self.assertRaises(ValueError):
            ledger.set_setting('shop', 'api_token', 'test')
        ledger.close()
