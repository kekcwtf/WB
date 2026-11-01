import os
os.environ.setdefault('QT_QPA_PLATFORM', 'offscreen')
from dataclasses import replace
from datetime import date
from decimal import Decimal as D
from pathlib import Path
import tempfile
import unittest
from PySide6.QtWidgets import QApplication, QPlainTextEdit
from wb_profit.finance import Operation, Kind
from wb_profit.storage import Ledger
from wb_profit.revision_history import revision_page
from wb_profit.revision_history_view import RevisionHistoryView


class RevisionHistoryTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.app = QApplication.instance() or QApplication([])

    def setUp(self):
        self.folder = tempfile.TemporaryDirectory()
        self.addCleanup(self.folder.cleanup)
        self.ledger = Ledger(Path(self.folder.name) / 'db.sqlite')
        self.addCleanup(self.ledger.close)
        self.start, self.end = date(2026, 1, 1), date(2026, 1, 31)
        self.op = Operation('s', 'wb_finance', '1:1:revenue', self.start, Kind.SALE, D('1050.123456'), '42:a', 1)

    def test_versions_compare_with_current_not_a_guessed_next_version(self):
        self.ledger.upsert([self.op])
        self.ledger.upsert([replace(self.op, amount=D('1000.123456'))])
        current = replace(self.op, amount=D('990.123456'))
        self.ledger.upsert([current])
        self.ledger.upsert([current])
        page = revision_page(self.ledger, 's', self.start, self.end)
        self.assertEqual(len(page.rows), 2)
        self.assertEqual([r.amount_difference for r in page.rows], [D('-10'), D('-60')])
        self.assertTrue(all(r.current == current for r in page.rows))
        self.assertFalse(page.has_more)
        self.assertEqual(self.ledger.operations('s', self.start, self.end), [current])

    def test_deleted_and_recreated_operation_is_not_assumed_to_have_continuous_history(self):
        self.ledger.replace_source('s', 'wb_finance', [self.op])
        self.ledger.replace_source('s', 'wb_finance', [])
        removed = revision_page(self.ledger, 's', self.start, self.end).rows[0]
        self.assertIsNone(removed.current)
        self.assertIsNone(removed.amount_difference)
        self.ledger.replace_source('s', 'wb_finance', [replace(self.op, amount=D('900'))])
        page = revision_page(self.ledger, 's', self.start, self.end)
        self.assertEqual(len(page.rows), 1)
        self.assertEqual(page.rows[0].amount_difference, D('-150.123456'))

    def test_period_matches_either_old_or_current_date_and_stores_are_isolated(self):
        moved_in = replace(self.op, identifier='in', occurred=date(2025, 12, 1))
        self.ledger.upsert([self.op, moved_in, replace(self.op, store='other')])
        self.ledger.upsert([replace(self.op, occurred=date(2026, 2, 1)),
                            replace(moved_in, occurred=self.start), replace(self.op, store='other', amount=D('1'))])
        page = revision_page(self.ledger, 's', self.start, self.end)
        self.assertEqual({r.previous.identifier for r in page.rows}, {'in', '1:1:revenue'})
        self.assertEqual(len(revision_page(self.ledger, 's', date(2027, 1, 1), date(2027, 1, 31)).rows), 0)

    def test_keyset_pages_have_no_duplicates_after_new_history_is_inserted(self):
        for i in range(6):
            self.ledger.upsert([replace(self.op, amount=D(1000+i))])
        first = revision_page(self.ledger, 's', self.start, self.end, limit=2)
        self.assertTrue(first.has_more)
        self.ledger.upsert([replace(self.op, amount=D('2000'))])
        second = revision_page(self.ledger, 's', self.start, self.end, limit=2, before_id=first.rows[-1].identifier)
        third = revision_page(self.ledger, 's', self.start, self.end, limit=2, before_id=second.rows[-1].identifier)
        ids = [r.identifier for page in (first, second, third) for r in page.rows]
        self.assertEqual(len(ids), len(set(ids)))
        self.assertEqual(len(ids), 5)
        self.assertFalse(third.has_more)

    def test_sqlite_utc_detection_time_converts_to_moscow_and_is_not_wb_change_time(self):
        self.ledger.upsert([self.op])
        self.ledger.upsert([replace(self.op, amount=D('1000'))])
        with self.ledger.db:
            self.ledger.db.execute("UPDATE revisions SET changed_at='2026-01-01 22:30:00'")
        row = revision_page(self.ledger, 's', self.start, self.end).rows[0]
        self.assertEqual(row.observed_at.isoformat(), '2026-01-02T01:30:00+03:00')
        self.assertTrue(row.observed_later)
        with self.ledger.db:
            self.ledger.db.execute("UPDATE revisions SET changed_at='unknown'")
        row = revision_page(self.ledger, 's', self.start, self.end).rows[0]
        self.assertIsNone(row.observed_at)
        self.assertFalse(row.observed_later)

    def test_invalid_page_arguments_are_rejected(self):
        for kwargs in [{'limit': 0}, {'limit': True}, {'limit': 501}, {'before_id': -1}, {'before_id': True}]:
            with self.assertRaises(ValueError):
                revision_page(self.ledger, 's', self.start, self.end, **kwargs)
        with self.assertRaises(ValueError):
            revision_page(self.ledger, 's', self.end, self.start)

    def test_view_pagination_sort_and_details_do_not_change_registry(self):
        operations = [replace(self.op, identifier=str(i)) for i in range(205)]
        self.ledger.upsert(operations)
        self.ledger.replace_source('s', 'wb_finance', [])
        view = RevisionHistoryView()
        self.addCleanup(view.close)
        view.set_context(self.ledger, 's', self.start, self.end)
        self.assertEqual(view.table.rowCount(), 200)
        self.assertTrue(view.next.isEnabled())
        view.table.sortItems(3)
        selected = view.table.item(0, 0).data(256)
        dialog = view.make_details(0)
        self.addCleanup(dialog.close)
        texts = [editor.toPlainText() for editor in dialog.findChildren(QPlainTextEdit)]
        self.assertIn(selected.previous.identifier, texts[0])
        self.assertIn('отсутствует', texts[1])
        view.older()
        self.assertEqual(view.table.rowCount(), 5)
        self.assertFalse(view.next.isEnabled())
        view.newer()
        self.assertEqual(view.table.rowCount(), 200)
        self.assertEqual(self.ledger.operations('s', self.start, self.end), [])
        view.clear()
        self.assertEqual(view.table.rowCount(), 0)
        self.assertFalse(view.previous.isEnabled())
        self.assertFalse(view.next.isEnabled())

    def test_backup_retains_history_and_does_not_invent_initial_versions(self):
        self.ledger.upsert([self.op])
        self.assertEqual(revision_page(self.ledger, 's', self.start, self.end).rows, ())
        self.ledger.upsert([replace(self.op, amount=D('1000'))])
        backup = self.ledger.backup(Path(self.folder.name) / 'snapshot.sqlite')
        ledger = Ledger(backup)
        try:
            self.assertEqual(revision_page(ledger, 's', self.start, self.end),
                             revision_page(self.ledger, 's', self.start, self.end))
        finally:
            ledger.close()
