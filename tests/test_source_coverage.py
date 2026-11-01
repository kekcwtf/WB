import os
os.environ.setdefault('QT_QPA_PLATFORM', 'offscreen')
from datetime import date
from pathlib import Path
import tempfile
import unittest
from PySide6.QtCore import QDate
from PySide6.QtWidgets import QApplication
from wb_profit.source_coverage import missing_ranges, source_coverage
from wb_profit.storage import Ledger
from wb_profit.sync import SyncStore, Page
from wb_profit.desktop import Window


class SourceCoverageTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.app = QApplication.instance() or QApplication([])

    def setUp(self):
        self.folder = tempfile.TemporaryDirectory()
        self.addCleanup(self.folder.cleanup)
        self.path = Path(self.folder.name) / 'db.sqlite'
        self.ledger = Ledger(self.path)
        self.addCleanup(self.ledger.close)
        self.cache = SyncStore(self.path)
        self.addCleanup(self.cache.close)
        self.start, self.end = date(2026, 1, 1), date(2026, 1, 10)

    def test_gaps_clip_overlap_and_adjacent_ranges_without_inventing_coverage(self):
        ranges = [['2025-12-30', '2026-01-02'], ['2026-01-04', '2026-01-05'],
                  ['2026-01-05', '2026-01-06'], ['2026-01-07', '2026-01-08']]
        self.assertEqual(missing_ranges(ranges, self.start, self.end),
                         ((date(2026, 1, 3), date(2026, 1, 3)), (date(2026, 1, 9), self.end)))
        self.assertEqual(missing_ranges([['2025-12-01', '2026-02-01']], self.start, self.end), ())

    def test_empty_coverage_and_extreme_dates_do_not_overflow(self):
        self.assertEqual(missing_ranges([], self.start, self.end), ((self.start, self.end),))
        self.assertEqual(missing_ranges([['0001-01-01', '9999-12-31']], date.min, date.max), ())
        self.assertEqual(missing_ranges([], date.min, date.max), ((date.min, date.max),))

    def test_malformed_ranges_do_not_silently_turn_into_full_coverage(self):
        for ranges in [None, [['bad', '2026-01-01']], [['2026-01-10', '2026-01-01']], [['2026-01-01']]]:
            self.ledger.set_setting('s', 'source_coverage:sales', ranges)
            report = source_coverage(self.ledger, 's', 'sales', self.start, self.end)
            self.assertEqual(report.label, 'Покрытие не определено')
        self.assertEqual(source_coverage(self.ledger, 's', 'sales', self.end, self.start).label, 'Неверный период')

    def test_snapshots_and_campaign_date_coverage_do_not_claim_historical_completeness(self):
        snapshot = source_coverage(self.ledger, 's', 'stocks', self.start, self.end)
        self.assertIn('не история', snapshot.label)
        self.ledger.set_setting('s', 'source_coverage:ad_statistics', [['2026-01-01', '2026-01-10']])
        report = source_coverage(self.ledger, 's', 'ad_statistics', self.start, self.end)
        self.assertIn('не сверка', report.label)
        self.assertIn('кампаний', report.note)
        other = source_coverage(self.ledger, 'other', 'ad_statistics', self.start, self.end)
        self.assertEqual(other.gaps, ((self.start, self.end),))

    def test_preserved_coverage_explains_incomplete_refresh_without_hiding_gaps(self):
        self.ledger.set_setting('s', 'source_coverage:sales', [['2026-01-01', '2026-01-05']])
        self.cache.save_page('s', 'sales', Page((), 'pending'))
        report = source_coverage(self.ledger, 's', 'sales', self.start, self.end)
        self.assertEqual(report.gaps, ((date(2026, 1, 6), self.end),))
        self.assertIn('не завершена', report.note)
        self.assertIn('глубину истории', report.note)
        self.assertIn('06.01.2026', report.note)

    def test_overview_column_updates_with_selected_period_without_downloads(self):
        window = Window(self.path)
        self.addCleanup(window.close)
        window.store.addItem('Магазин', 's')
        window.ledger.set_setting('s', 'source_coverage:sales', [['2026-01-01', '2026-01-10']])
        window.period.setCurrentIndex(6)
        window.start.setDate(QDate(2026, 1, 1))
        window.end.setDate(QDate(2026, 1, 10))
        window.refresh_source_status()
        def item(label):
            return next(window.source_table.item(row, 3) for row in range(window.source_table.rowCount())
                        if window.source_table.item(row, 0).text() == label)
        self.assertIn('Есть покрытие', item('Продажи и возвраты').text())
        window.end.setDate(QDate(2026, 1, 11))
        window.refresh_source_status()
        self.assertIn('Пробелов', item('Продажи и возвраты').text())
        self.assertIn('11.01.2026', item('Продажи и возвраты').toolTip())
        self.assertIsNone(window.worker)
        window.close()
