from datetime import datetime,timedelta
from decimal import Decimal as D
from pathlib import Path
import tempfile
import unittest
from unittest.mock import patch
from wb_profit.auto_backups import automatic_backup,backup_due,backup_folder,options
from wb_profit.backups import BackupError,write_backup
from wb_profit.storage import Ledger
from wb_profit.finance import Operation,Kind
from wb_profit.business_dates import MOSCOW

NOW=datetime(2026,1,1,12,tzinfo=MOSCOW)


class AutomaticBackupTests(unittest.TestCase):
    def setUp(self):
        self.folder=tempfile.TemporaryDirectory();self.addCleanup(self.folder.cleanup)
        self.path=Path(self.folder.name)/'db.sqlite'
        self.ledger=Ledger(self.path);self.addCleanup(self.ledger.close)
        self.ledger.set_setting('_app','stores',[{'id':'s','name':'Магазин'}])
        self.op=Operation('s','report','sale',NOW.date(),Kind.SALE,D('123.456789'),'a',1)
        self.ledger.upsert([self.op])

    def save(self,now=NOW):return automatic_backup(self.ledger,self.path,now=now)

    def test_first_snapshot_is_readable_and_repeated_check_does_not_copy_again(self):
        result=self.save()
        self.assertTrue(result.created)
        backup=Ledger(result.path)
        try:
            self.assertEqual(backup.operations('s',NOW.date(),NOW.date()),[self.op])
            self.assertEqual(backup.setting('_app','stores')[0]['name'],'Магазин')
        finally:backup.close()
        original=result.path.read_bytes()
        second=self.save(NOW+timedelta(hours=1))
        self.assertFalse(second.created)
        self.assertEqual(result.path.read_bytes(),original)
        self.assertFalse(backup_due(self.ledger,self.path,NOW+timedelta(hours=1)))

    def test_interval_refresh_updates_same_day_and_new_day_creates_separate_file(self):
        first=self.save()
        second=self.save(NOW+timedelta(hours=4))
        self.assertTrue(second.created)
        self.assertEqual(first.path,second.path)
        third=self.save(NOW+timedelta(days=1))
        self.assertNotEqual(third.path,first.path)
        self.assertTrue(first.path.exists())

    def test_retention_removes_only_matching_daily_versions_after_new_verified_copy(self):
        self.ledger.set_setting('_app','auto_backup_options',{'enabled':True,'interval_hours':4,'keep':3})
        first=self.save()
        folder=backup_folder(self.path)
        manual=folder/'manual.sqlite';manual.write_bytes(b'manual')
        before_restore=folder/'before-restore.sqlite';before_restore.write_bytes(b'safety')
        foreign=folder/('auto-'+'b'*32+'-2025-12-01.sqlite');foreign.write_bytes(b'foreign')
        for day in range(1,5):result=self.save(NOW+timedelta(days=day))
        self.assertFalse(first.path.exists())
        self.assertEqual(len(list(folder.glob('auto-'+self.ledger.setting('_app','auto_backup_lineage')+'-*.sqlite'))),3)
        self.assertEqual(manual.read_bytes(),b'manual')
        self.assertEqual(before_restore.read_bytes(),b'safety')
        self.assertEqual(foreign.read_bytes(),b'foreign')
        self.assertTrue(result.path.exists())

    def test_failed_new_copy_does_not_prune_previous_versions_or_update_success(self):
        self.ledger.set_setting('_app','auto_backup_options',{'enabled':True,'interval_hours':4,'keep':3})
        for day in range(3):self.save(NOW+timedelta(days=day))
        previous=self.ledger.setting('_app','auto_backup_state')
        files=set(backup_folder(self.path).iterdir())
        with patch('wb_profit.auto_backups.write_backup',side_effect=OSError('disk full')):
            with self.assertRaises(OSError):self.save(NOW+timedelta(days=3))
        self.assertEqual(set(backup_folder(self.path).iterdir()),files)
        self.assertEqual(self.ledger.setting('_app','auto_backup_state'),previous)

    def test_missing_or_modified_file_is_recreated_despite_recent_success(self):
        result=self.save()
        result.path.unlink()
        self.assertTrue(backup_due(self.ledger,self.path,NOW+timedelta(minutes=1)))
        result=self.save(NOW+timedelta(minutes=1))
        result.path.write_bytes(b'corrupted')
        self.assertTrue(self.save(NOW+timedelta(minutes=2)).created)
        recovered=Ledger(result.path);recovered.close()

    def test_disabled_or_empty_store_does_not_create_files_or_lineage(self):
        self.ledger.set_setting('_app','auto_backup_options',{'enabled':False})
        self.assertFalse(self.save().created)
        self.assertFalse(backup_folder(self.path).exists())
        self.assertIsNone(self.ledger.setting('_app','auto_backup_lineage'))
        empty=Ledger(Path(self.folder.name)/'empty.sqlite')
        try:self.assertFalse(automatic_backup(empty,Path(self.folder.name)/'empty.sqlite',now=NOW).created)
        finally:empty.close()

    def test_future_dated_files_are_not_deleted_after_clock_moves_back(self):
        future=self.save(NOW+timedelta(days=30))
        result=self.save()
        self.assertTrue(result.created)
        self.assertTrue(future.path.exists())
        self.assertTrue(result.warnings)

    def test_cancellation_during_sqlite_copy_keeps_previous_file_and_cleans_staging(self):
        first=self.save()
        original=first.path.read_bytes();calls=[]
        def cancel():
            calls.append(True)
            if len(calls)>=2:raise InterruptedError('cancelled')
        with self.assertRaises(InterruptedError):
            write_backup(self.ledger.db,first.path,cancel_check=cancel)
        self.assertEqual(first.path.read_bytes(),original)
        self.assertEqual(list(first.path.parent.glob('.wb-backup-*')),[])

    def test_options_changes_apply_only_via_new_successful_snapshot(self):
        self.save()
        self.ledger.set_setting('_app','auto_backup_options',{'enabled':True,'interval_hours':2,'keep':5})
        self.assertTrue(backup_due(self.ledger,self.path,NOW+timedelta(minutes=1)))
        self.assertTrue(self.save(NOW+timedelta(minutes=1)).created)
        self.assertEqual(self.ledger.setting('_app','auto_backup_state')['options']['keep'],5)

    def test_invalid_options_and_lineage_cannot_select_unrelated_files_for_pruning(self):
        self.ledger.set_setting('_app','auto_backup_options',{'keep':1})
        with self.assertRaises(BackupError):options(self.ledger)
        self.ledger.set_setting('_app','auto_backup_options',{})
        self.ledger.set_setting('_app','auto_backup_lineage','../foreign')
        with self.assertRaises(BackupError):self.save()
        self.assertFalse(backup_folder(self.path).exists())
