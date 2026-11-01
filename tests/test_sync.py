import unittest
from decimal import Decimal
from wb_profit.sync import Record, Page, SyncStore, Synchronizer


class SyncTests(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self):
        self.storage = SyncStore(':memory:')
        self.sync = Synchronizer(self.storage)

    async def asyncTearDown(self):
        self.storage.close()

    async def test_resume_after_failure_and_repeat_with_correction(self):
        starts = []
        class Source:
            name = 'test_source'
            failed = False
            corrected = False
            async def pages(self, cursor):
                starts.append(cursor)
                if cursor is None:
                    yield Page((Record('1', {'amount': Decimal('10.123') if not self.corrected else Decimal('20.123')}),), '1')
                if not self.failed:
                    self.failed = True
                    raise ConnectionError('test disconnect')
                yield Page((Record('2', {'amount': Decimal('30')}),), '2', True)
        source = Source()
        with self.assertRaises(ConnectionError):
            await self.sync.run('shop', source)
        self.assertEqual(self.storage.state('shop', source.name)['cursor'], '1')
        self.assertEqual(self.storage.state('shop', source.name)['status'], 'failed')
        self.assertIsNone(self.storage.state('shop', source.name)['last_success'])
        await self.sync.run('shop', source)
        self.assertEqual(starts, [None, '1'])
        self.assertIsNotNone(self.storage.state('shop', source.name)['last_success'])
        source.corrected = True
        await self.sync.run('shop', source)
        await self.sync.run('shop', source)
        self.assertEqual(self.storage.db.execute('SELECT COUNT(*) FROM source_records').fetchone()[0], 2)
        self.assertEqual(self.storage.db.execute('SELECT COUNT(*) FROM source_revisions').fetchone()[0], 1)

    async def test_malformed_page_does_not_advance_checkpoint(self):
        self.storage.save_page('shop', 'test', Page((Record('1', {'amount': '10'}),), '1'))
        with self.assertRaises(ValueError):
            self.storage.save_page('shop', 'test', Page((Record('2', {}), Record('2', {})), '2'))
        self.assertEqual(self.storage.state('shop', 'test')['cursor'], '1')
        self.assertEqual(self.storage.db.execute('SELECT COUNT(*) FROM source_records').fetchone()[0], 1)

    async def test_missing_final_page_is_not_success(self):
        class Source:
            name = 'empty'
            async def pages(self, cursor):
                if False:
                    yield
        with self.assertRaises(ValueError):
            await self.sync.run('shop', Source())
        self.assertEqual(self.storage.state('shop', 'empty')['status'], 'failed')

    async def test_empty_complete_page_is_valid(self):
        class Source:
            name = 'empty'
            async def pages(self, cursor):
                yield Page((), None, True)
        self.assertTrue(await self.sync.run('shop', Source()))
        self.assertEqual(self.storage.state('shop', 'empty')['status'], 'success')

    async def test_database_failure_rolls_back_records_and_cursor(self):
        self.storage.save_page('shop', 'test', Page((Record('1', {'amount': '10'}),), '1'))
        self.storage.db.execute("CREATE TRIGGER fail_checkpoint BEFORE UPDATE ON sync_state BEGIN SELECT RAISE(ABORT, 'test failure'); END")
        import sqlite3
        with self.assertRaises(sqlite3.IntegrityError):
            self.storage.save_page('shop', 'test', Page((Record('2', {'amount': '20'}),), '2'))
        self.assertEqual(self.storage.state('shop', 'test')['cursor'], '1')
        self.assertEqual(self.storage.db.execute('SELECT COUNT(*) FROM source_records').fetchone()[0], 1)

    async def test_parallel_run_is_coalesced_and_cancel_is_recorded(self):
        import asyncio
        entered = asyncio.Event()
        release = asyncio.Event()
        class Source:
            name = 'slow'
            calls = 0
            async def pages(self, cursor):
                self.calls += 1
                entered.set()
                await release.wait()
                yield Page((), None, True)
        source = Source()
        task = asyncio.create_task(self.sync.run('shop', source))
        await entered.wait()
        self.assertFalse(await self.sync.run('shop', source))
        self.assertEqual(source.calls, 1)
        task.cancel()
        with self.assertRaises(asyncio.CancelledError):
            await task
        self.assertEqual(self.storage.state('shop', source.name)['status'], 'cancelled')
        self.assertIsNone(self.storage.state('shop', source.name)['last_success'])

    async def test_snapshot_removes_old_stock_without_losing_other_source(self):
        self.storage.save_page('shop','stocks',Page((Record('old',{'stock':5}),),None,True,snapshot=True))
        self.storage.save_page('shop','sales',Page((Record('sale',{}),),None,True))
        self.storage.save_page('shop','stocks',Page((Record('new',{'stock':0}),),None,True,snapshot=True))
        self.assertEqual(self.storage.db.execute("SELECT identifier FROM source_records WHERE source='stocks'").fetchall(),[('new',)])
        self.assertEqual(self.storage.db.execute("SELECT COUNT(*) FROM source_records WHERE source='sales'").fetchone()[0],1)

    async def test_finance_window_deletion_waits_for_complete_run(self):
        self.storage.save_page('shop','finance_weekly',Page((Record('old',{'rrDate':'2026-01-01'}),Record('outside',{'rrDate':'2025-01-01'})),'x'))
        self.storage.save_page('shop','finance_weekly',Page((Record('new',{'rrDate':'2026-01-02'}),),'y',run_id='run'))
        self.assertEqual(self.storage.db.execute('SELECT COUNT(*) FROM source_records').fetchone()[0],3)
        self.storage.save_page('shop','finance_weekly',Page((),'z',True,run_id='run',replace_window=('rrDate','2026-01-01','2026-01-31')))
        ids={r[0] for r in self.storage.db.execute('SELECT identifier FROM source_records')}
        self.assertEqual(ids,{'outside','new'})
