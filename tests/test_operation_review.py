import os
os.environ.setdefault('QT_QPA_PLATFORM', 'offscreen')
from datetime import date
from decimal import Decimal as D
import json
from pathlib import Path
import tempfile
import unittest
from PySide6.QtCore import Qt
from PySide6.QtWidgets import QApplication, QPlainTextEdit
from wb_profit.finance import Operation, Kind
from wb_profit.storage import Ledger
from wb_profit.sync import SyncStore, Page, Record
from wb_profit.normalization import financial_row
from wb_profit.operation_review import review_operation
from wb_profit.operation_review_view import OperationReviewView
from test_wb_api import row


class OperationReviewTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.app = QApplication.instance() or QApplication([])

    def setUp(self):
        self.folder = tempfile.TemporaryDirectory()
        self.addCleanup(self.folder.cleanup)
        path = Path(self.folder.name) / 'db.sqlite'
        self.ledger = Ledger(path)
        self.addCleanup(self.ledger.close)
        self.cache = SyncStore(path)
        self.addCleanup(self.cache.close)
        self.view = OperationReviewView()
        self.addCleanup(self.view.close)

    def op(self, identifier, kind, amount='0', sku='42:123', shop='s', confirmed=True):
        return Operation(shop, 'wb_finance', identifier, date(2026, 1, 1), kind, D(amount), sku, confirmed=confirmed)

    def test_unknown_marker_explains_real_normalization_issue_without_mutating_ledger(self):
        data = row(forPay='940', newField='<b>данные WB</b>')
        self.cache.save_page('s', 'finance_weekly', Page((Record('1:1', data),), None, completed=True))
        normalized = financial_row('s', data)
        self.ledger.upsert(list(normalized.operations))
        before = self.ledger.operations('s', date.min, date.max)
        unknown = next(op for op in before if op.kind == Kind.UNKNOWN)
        report = review_operation(self.ledger, unknown)
        self.assertEqual(report.raw_identifier, '1:1')
        self.assertIn('Необъяснённая разница', ' '.join(report.reasons))
        self.assertIn('newField', ' '.join(report.reasons))
        self.assertIn('Нулевая сумма', ' '.join(report.reasons))
        self.assertEqual(json.loads(report.raw_json)['forPay'], '940')
        self.assertEqual(self.ledger.operations('s', date.min, date.max), before)

    def test_raw_lookup_is_exact_by_store_source_and_report_row_not_amount(self):
        data = row(retailAmount='9999999999999999.123456789')
        self.cache.save_page('other', 'finance_weekly', Page((Record('1:1', data),), None, completed=True))
        self.assertIsNone(review_operation(self.ledger, self.op('1:1:unclassified', Kind.UNKNOWN)).raw_json)
        self.cache.save_page('s', 'finance_weekly', Page((Record('1:1', data),), None, completed=True))
        report = review_operation(self.ledger, self.op('1:1:unclassified', Kind.UNKNOWN))
        self.assertIn('9999999999999999.123456789', report.raw_json)
        self.assertIsNone(review_operation(self.ledger, self.op('1:2:unclassified', Kind.UNKNOWN)).raw_json)

    def test_absent_raw_cache_and_mismatched_payload_do_not_invent_source(self):
        with tempfile.TemporaryDirectory() as folder:
            ledger = Ledger(Path(folder) / 'core.sqlite')
            try:
                self.assertIsNone(review_operation(ledger, self.op('1:1:unclassified', Kind.UNKNOWN)).raw_json)
            finally:
                ledger.close()
        self.cache.save_page('s', 'finance_weekly', Page((Record('1:1', row(reportId=99)),), None, completed=True))
        self.assertIsNone(review_operation(self.ledger, self.op('1:1:unclassified', Kind.UNKNOWN)).raw_json)

    def test_filters_counts_search_and_payout_exclusion(self):
        operations = [self.op('unknown', Kind.UNKNOWN), self.op('adjustment', Kind.ADJUSTMENT, '1'),
                      self.op('common', Kind.STORAGE, '-10', None), self.op('payout', Kind.PAYOUT, '100', None),
                      self.op('sale', Kind.SALE, '100'), self.op('preliminary', Kind.LOGISTICS, '-1', confirmed=False)]
        self.view.set_context(self.ledger, 's', operations + [operations[0]])
        self.assertEqual(self.view.table.model().rowCount(), 4)
        self.assertIn('Операций: 6', self.view.summary.text())
        self.assertIn('без SKU, кроме выплат: 1', self.view.summary.text())
        self.view.filter.setCurrentIndex(3)
        self.assertEqual(self.view.table.model().operation_at(0).identifier, 'common')
        self.view.filter.setCurrentIndex(5)
        self.assertEqual(self.view.table.model().rowCount(), 6)
        self.view.search.setText('НЕИЗВЕСТНЫЕ')
        self.assertEqual(self.view.table.model().operation_at(0).kind, Kind.UNKNOWN)
        self.view.clear()
        self.assertEqual(self.view.table.model().rowCount(), 0)
        self.assertIn('не подтверждает', self.view.summary.text())

    def test_empty_unverified_period_is_not_claimed_verified(self):
        self.view.set_context(self.ledger, 's', [])
        self.assertIn('пока не сверён', self.view.summary.text())
        self.assertEqual(self.view.table.model().rowCount(), 0)

    def test_details_after_sort_show_correct_exact_operation_and_literal_text(self):
        raw = row(newField='<script>это текст</script>')
        self.cache.save_page('s', 'finance_weekly', Page((Record('1:1', raw),), None, completed=True))
        operations = [self.op('1:1:unclassified', Kind.UNKNOWN), self.op('2:2:adjustment', Kind.ADJUSTMENT, '-10')]
        self.view.set_context(self.ledger, 's', operations)
        self.view.table.model().sort(5, Qt.SortOrder.DescendingOrder)
        dialog = self.view.make_details(0)
        self.addCleanup(dialog.close)
        texts = '\n'.join(editor.toPlainText() for editor in dialog.findChildren(QPlainTextEdit))
        self.assertIn('1:1:unclassified', texts)
        self.assertIn('<script>это текст</script>', texts)
        self.assertTrue(all(editor.isReadOnly() for editor in dialog.findChildren(QPlainTextEdit)))

    def test_shared_expense_is_not_unknown_and_does_not_require_reclassification(self):
        report = review_operation(self.ledger, self.op('shared', Kind.STORAGE, '-50', None))
        self.assertIn('общие расходы допустимы', ' '.join(report.reasons))
        self.assertNotIn('Неизвестная операция', ' '.join(report.reasons))

    def test_mixed_stores_are_rejected_and_old_rows_cleared(self):
        self.view.set_context(self.ledger, 's', [self.op('a', Kind.UNKNOWN)])
        with self.assertRaisesRegex(ValueError, 'отдельно по магазину'):
            self.view.set_context(self.ledger, 's', [self.op('b', Kind.UNKNOWN, shop='other')])
        self.assertEqual(self.view.table.model().rowCount(), 0)
