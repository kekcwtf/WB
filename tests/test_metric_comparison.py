import os
os.environ.setdefault('QT_QPA_PLATFORM', 'offscreen')
from datetime import date
from decimal import Decimal as D
from pathlib import Path
import tempfile
import unittest
from PySide6.QtWidgets import QApplication
from wb_profit.comparison import compare_metrics
from wb_profit.finance import Operation, Kind, Cost, TaxSettings
from wb_profit.taxes import TaxRule
from wb_profit.storage import Ledger
from wb_profit.sync import SyncStore, Page, Record
from wb_profit.overview_view import OverviewView


class MetricComparisonTests(unittest.TestCase):
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
        self.start, self.end = date(2026, 1, 8), date(2026, 1, 14)
        self.ops = []
        for day, scale in [(date(2026, 1, 1), 1), (self.start, 2)]:
            for kind, value, units in [(Kind.SALE, '2100', 2), (Kind.COMMISSION, '-210', 0), (Kind.LOGISTICS, '-100', 0)]:
                self.ops.append(Operation('s', 'wb_finance', f'{day}:{kind}', day, kind, D(value)*scale, 'a', units))
        self.ledger.upsert(self.ops)
        self.costs = [Cost('a', date(2025, 1, 1), D('400'))]
        self.rules = [TaxRule(date(2025, 1, 1), TaxSettings(D('4'), D('5'), confirmed_by_user=True))]
        for source in ['finance_weekly', 'report_totals', 'advertising']:
            self.ledger.set_setting('s', 'source_coverage:'+source, [['2026-01-01', '2026-01-14']])
        self.cache.save_page('s', 'report_totals', Page((Record('1', {'reportId': 1, 'dateTo': '2026-01-07'}),
                                                      Record('2', {'reportId': 2, 'dateTo': '2026-01-14'})), None, True))
        self.cache.save_page('s', 'finance_weekly', Page((Record('1:1', {'reportId': 1, 'rrDate': '2026-01-01'}),
                                                       Record('2:1', {'reportId': 2, 'rrDate': '2026-01-08'})), None, True))
        self.ledger.set_setting('s', 'report_audits', [{'report_id': '1', 'verified': True}, {'report_id': '2', 'verified': True}])

    def compare(self):
        return compare_metrics(self.ledger, 's', self.start, self.end, self.costs, self.rules)

    def test_independent_net_cost_history_and_percentage_point_differences(self):
        self.costs.append(Cost('a', self.start, D('500')))
        report = self.compare()
        # Старый период: 2100-210-100-800-100НДС-80УСН=810.
        # Новый: 4200-420-200-1000-200НДС-160УСН=2220.
        net = report.metrics['net']
        self.assertEqual((net.current, net.previous, net.change), (D('2220'), D('810'), D('1410')))
        self.assertEqual(report.metrics['revenue'].relative_percent, D('100'))
        self.assertEqual(report.metrics['cost'].change, D('200'))
        roi = report.metrics['roi']
        self.assertEqual(roi.change, D('120.75'))
        self.assertEqual(roi.unit, 'п.п.')
        self.assertIsNone(roi.relative_percent)
        self.assertEqual(report.metrics['margin'].change, D('2220')/D('4200')*100-D('810')/D('2100')*100)

    def test_revenue_can_be_compared_when_advertising_prevents_full_profit(self):
        self.ledger.set_setting('s', 'source_coverage:advertising', [['2026-01-08', '2026-01-14']])
        report = self.compare()
        self.assertEqual(report.metrics['revenue'].change, D('2100'))
        self.assertIsNone(report.metrics['net'].change)
        self.assertIsNone(report.metrics['wb_cost'].change)
        self.assertIn('рекламу', report.metrics['net'].unavailable_reason)

    def test_missing_cost_does_not_erase_valid_revenue_or_invent_net(self):
        self.costs = []
        report = self.compare()
        self.assertEqual(report.metrics['revenue'].change, D('2100'))
        self.assertIsNone(report.metrics['cost'].change)
        self.assertIsNone(report.metrics['net'].change)

    def test_each_period_uses_its_effective_tax_settings(self):
        # Одинаковая экономика, разные ставки с 8 января.
        from dataclasses import replace
        current = [replace(op, amount=op.amount/2) for op in self.ops if op.occurred == self.start]
        self.ledger.upsert(current)
        self.rules.append(TaxRule(self.start, TaxSettings(D('6'), D('7'), confirmed_by_user=True)))
        net = self.compare().metrics['net']
        # 2100-210-100-800-137.38НДС-117.76УСН=734.86.
        self.assertEqual((net.current, net.previous, net.change), (D('734.86'), D('810'), D('-75.14')))

    def test_negative_or_zero_previous_profit_has_no_relative_growth_percent(self):
        for unit_cost in ['1000', '805']:
            self.costs = [Cost('a', date(2025, 1, 1), D(unit_cost))]
            net = self.compare().metrics['net']
            self.assertLessEqual(net.previous, D('0'))
            self.assertIsNotNone(net.change)
            self.assertIsNone(net.relative_percent)

    def test_unverified_unknown_or_preliminary_period_is_not_compared_as_complete(self):
        for kind, confirmed in [(Kind.UNKNOWN, True), (Kind.LOGISTICS, False)]:
            extra = Operation('s', 'wb_finance', 'extra', self.start, kind, D('0'), 'a', confirmed=confirmed)
            self.ledger.upsert([extra])
            self.assertTrue(all(metric.change is None for metric in self.compare().metrics.values()))
        self.ledger.db.execute("DELETE FROM operations WHERE identifier='extra'")
        self.ledger.db.commit()
        self.ledger.set_setting('s', 'report_audits', [])
        self.assertTrue(all(metric.change is None for metric in self.compare().metrics.values()))

    def test_native_cards_show_units_and_clear_previous_changes(self):
        view = OverviewView()
        self.addCleanup(view.close)
        report = self.compare()
        view.set_comparison(report)
        self.assertIn('+1610.00 ₽', view.changes['net'].text())
        self.assertIn('п.п.', view.changes['roi'].text())
        self.assertIn('01.01.2026–07.01.2026', view.changes['net'].toolTip())
        self.ledger.set_setting('s', 'source_coverage:advertising', [])
        view.set_comparison(self.compare())
        self.assertEqual(view.changes['net'].text(), 'Сравнение: недостаточно данных')
        self.assertIn('рекламу', view.changes['net'].toolTip())
        view.clear()
        self.assertTrue(all(label.text() == 'Сравнение: нет данных' for label in view.changes.values()))
