import os
os.environ.setdefault('QT_QPA_PLATFORM', 'offscreen')
from datetime import datetime, timedelta, timezone, date
from decimal import Decimal as D
from pathlib import Path
import tempfile
import unittest
from PySide6.QtWidgets import QApplication
from wb_profit.finance import Operation, Kind, Cost, TaxSettings
from wb_profit.storage import Ledger
from wb_profit.sync import SyncStore, Page, Record
from wb_profit.tariff_cache import forecast_rates
from wb_profit.api.tariffs import Tariffs
from wb_profit.api.transport import ApiError
from wb_profit.economics_view import EconomicsView
from test_wb_api import FakeClient


class TariffCacheTests(unittest.IsolatedAsyncioTestCase):
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
        self.now = datetime(2026, 1, 2, 12, tzinfo=timezone.utc)
        self.ops = [Operation('s', 'wb_finance', 'sale', date(2026, 1, 1), Kind.SALE, D('1050'), '42:a', 1),
                    Operation('s', 'wb_finance', 'commission', date(2026, 1, 1), Kind.COMMISSION, D('-105'), '42:a')]
        self.metadata = {'42': {'subjectID': 1}}
        self.cache.save_page('s', 'tariffs', Page((Record('1', {'subjectID': 1, 'paidStorageKgvp': D('20.125'), 'kgvpMarketplace': D('99')}),), None, True))
        self.cache.save_page('s', 'products', Page((Record('42', {'nmID': 42, 'subjectID': 1}),), None, True))
        self.stamp('tariffs', self.now-timedelta(hours=1))
        self.stamp('products', self.now-timedelta(hours=1))

    def stamp(self, source, value, completed=1, status='success'):
        with self.cache.db:
            self.cache.db.execute('UPDATE sync_state SET last_success=?,completed=?,status=? WHERE store=? AND source=?',
                                  (value.isoformat() if isinstance(value, datetime) else value, completed, status, 's', source))

    def rates(self):
        return forecast_rates(self.ledger, 's', self.ops, self.metadata, now=self.now)

    async def test_only_fbw_rate_is_selected_and_moscow_retrieval_date_is_visible(self):
        report = self.rates()
        self.assertEqual(report.by_sku, {'42:a': D('20.125')})
        self.assertIn('02.01.2026 14:00 МСК', report.note)
        self.assertIn('не срок действия', report.note)
        self.assertIn('Индивидуальные опции', report.note)

    async def test_stale_future_naive_and_missing_dates_do_not_supply_current_rate(self):
        for stamp in [self.now-timedelta(hours=24), self.now+timedelta(seconds=1), '2026-01-02T11:00:00', None, 'bad']:
            self.stamp('tariffs', stamp)
            self.assertEqual(self.rates().by_sku, {})
        self.stamp('tariffs', self.now-timedelta(hours=23, minutes=59))
        self.assertEqual(self.rates().by_sku['42:a'], D('20.125'))

    async def test_incomplete_failed_or_stale_cards_do_not_supply_category_based_rate(self):
        for source, completed, status in [('tariffs', 0, 'running'), ('tariffs', 1, 'failed'), ('products', 1, 'cancelled')]:
            self.stamp(source, self.now, completed=completed, status=status)
            self.assertEqual(self.rates().by_sku, {})
            self.stamp(source, self.now)
        self.stamp('products', self.now-timedelta(days=2))
        self.assertEqual(self.rates().by_sku, {})
        self.assertIn('категория товара', self.rates().note)

    async def test_invalid_and_fbs_only_api_rates_are_rejected_before_snapshot(self):
        for data in [{'subjectID': True, 'paidStorageKgvp': 10}, {'subjectID': 1, 'paidStorageKgvp': '-1'},
                     {'subjectID': 1, 'paidStorageKgvp': 'NaN'}, {'subjectID': 1, 'paidStorageKgvp': '101'},
                     {'subjectID': 1, 'kgvpMarketplace': 20}]:
            source = Tariffs(FakeClient([{'report': [data]}]))
            with self.assertRaises(ApiError):
                [page async for page in source.pages(None)]
        for rate in ['0', '100']:
            pages = [page async for page in Tariffs(FakeClient([{'report': [{'subjectID': 1, 'paidStorageKgvp': rate}]}])).pages(None)]
            self.assertTrue(pages[0].completed)

    async def test_missing_category_or_store_does_not_borrow_another_rate(self):
        self.metadata = {'42': {'subjectID': 99}}
        self.assertEqual(self.rates().by_sku, {})
        self.assertEqual(forecast_rates(self.ledger, 'other', [], {}, now=self.now).by_sku, {})
        with self.assertRaisesRegex(ValueError, 'по магазину'):
            forecast_rates(self.ledger, 'other', self.ops, self.metadata, now=self.now)

    async def test_fallback_changes_forecast_only_not_actual_operations(self):
        costs = [Cost('42:a', date(2025, 1, 1), D('400'))]
        taxes = TaxSettings(D('4'), D('5'), confirmed_by_user=True)
        self.ledger.upsert(self.ops)
        before = self.ledger.operations('s', date.min, date.max)
        view = EconomicsView()
        self.addCleanup(view.close)
        report = self.rates()
        view.set_context(self.ops, costs, taxes, coverage_confirmed=True, fbw_rates=report.by_sku, tariff_note=report.note)
        self.assertEqual(view.model.forecast(D('1050')).commission, D('211.3125'))
        self.stamp('tariffs', self.now-timedelta(days=2))
        report = self.rates()
        view.set_context(self.ops, costs, taxes, coverage_confirmed=True, fbw_rates=report.by_sku, tariff_note=report.note)
        self.assertEqual(view.model.forecast(D('1050')).commission, D('105'))
        self.assertIn('не применён', view.notice.text())
        self.assertIn('историческая', view.model.commission_source)
        self.assertEqual(self.ledger.operations('s', date.min, date.max), before)

    async def test_one_hundred_percent_fbw_tariff_has_no_finite_break_even_price(self):
        from wb_profit.economics import historical_model
        model = historical_model('42:a', self.ops, [Cost('42:a', date(2025, 1, 1), D('400'))],
                                 TaxSettings(D('0'), D('0'), confirmed_by_user=True),
                                 coverage_confirmed=True, fbw_commission_percent=D('100'))
        self.assertIsNone(model.break_even_price())
        self.assertEqual(model.forecast(D('1050')).estimated_profit, D('-400'))

    async def test_open_price_scenario_falls_back_at_expiry_without_resetting_input(self):
        from unittest.mock import patch
        view = EconomicsView()
        self.addCleanup(view.close)
        deadline = datetime.now(timezone.utc) + timedelta(hours=1)
        view.set_context(self.ops, [Cost('42:a', date(2025, 1, 1), D('400'))],
                         TaxSettings(D('4'), D('5'), confirmed_by_user=True),
                         coverage_confirmed=True, fbw_rates={'42:a': D('20')}, tariff_valid_until=deadline)
        view.price.setText('2100')
        self.assertEqual(view.model.forecast(D('2100')).commission, D('420'))
        with patch('wb_profit.economics_view.datetime') as clock:
            clock.now.return_value = deadline
            view.check_tariff_age()
        self.assertEqual(view.price.text(), '2100')
        self.assertEqual(view.model.forecast(D('2100')).commission, D('210'))
        self.assertIn('устарел после открытия', view.notice.text())
        self.assertFalse(view.tariff_timer.isActive())
