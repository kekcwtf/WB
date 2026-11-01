import unittest
from wb_profit.sync import SyncStore, Synchronizer, Page, Record


class PaginationCycleTests(unittest.IsolatedAsyncioTestCase):
    async def test_repeated_earlier_cursor_stops_before_page_is_saved(self):
        for initial in (None, 'start'):
            with self.subTest(initial=initial):
                store = SyncStore(':memory:')
                try:
                    if initial:
                        store.save_page('s', 'source', Page((), initial))
                    class Source:
                        name = 'source'
                        async def pages(self, cursor):
                            yield Page((Record('first', {'value': 1}),), 'A')
                            yield Page((Record('second', {'value': 2}),), 'B')
                            yield Page((Record('bad', {'value': 999}),), initial or 'A')
                            raise AssertionError('После цикла нельзя продолжать загрузку')
                    with self.assertRaisesRegex(ValueError, 'прежний курсор'):
                        await Synchronizer(store).run('s', Source())
                    self.assertEqual(store.state('s', 'source')['cursor'], 'B')
                    self.assertEqual(store.state('s', 'source')['status'], 'failed')
                    self.assertIsNone(store.state('s', 'source')['last_success'])
                    self.assertEqual({row[0] for row in store.db.execute('SELECT identifier FROM source_records')}, {'first', 'second'})
                finally:
                    store.close()

    async def test_completed_page_can_keep_last_cursor_and_repeat_sync_has_no_revisions(self):
        store = SyncStore(':memory:')
        try:
            class Source:
                name = 'source'
                async def pages(self, cursor):
                    yield Page((Record('first', {'value': 1}),), 'A')
                    yield Page((), 'A', True)
            sync = Synchronizer(store)
            await sync.run('s', Source())
            await sync.run('s', Source())
            self.assertEqual(store.state('s', 'source')['status'], 'success')
            self.assertIsNotNone(store.state('s', 'source')['last_success'])
            self.assertEqual(store.db.execute('SELECT COUNT(*) FROM source_records').fetchone()[0], 1)
            self.assertEqual(store.db.execute('SELECT COUNT(*) FROM source_revisions').fetchone()[0], 0)
        finally:
            store.close()
