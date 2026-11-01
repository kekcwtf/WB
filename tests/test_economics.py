import unittest
from dataclasses import replace
from datetime import date
from decimal import Decimal as D
from wb_profit.finance import Operation, Kind, Cost, TaxSettings
from wb_profit.economics import historical_model

DAY = date(2026, 1, 1)

class EconomicsTests(unittest.TestCase):
    def setUp(self):
        self.ops = [Operation('s', 'report', 'sale', DAY, Kind.SALE, D('2100'), 'a', 2),
                    Operation('s', 'report', 'commission', DAY, Kind.COMMISSION, D('-210'), 'a'),
                    Operation('s', 'report', 'logistics', DAY, Kind.LOGISTICS, D('-100'), 'a')]
        self.costs = [Cost('a', DAY, D('400'))]
        self.taxes = TaxSettings(D('4'), D('5'), confirmed_by_user=True)

    def test_independently_calculated_price_scenarios(self):
        model = historical_model('a', self.ops, self.costs, self.taxes, coverage_confirmed=True)
        # Цена 1050; комиссия 105; доставка 50; закупка 400;
        # НДС 50; УСН 40. Итого 1050-105-50-400-50-40 = 405.
        forecast = model.forecast(D('1050'))
        self.assertEqual(forecast.estimated_profit, D('405'))
        self.assertEqual((forecast.vat, forecast.usn), (D('50'), D('40')))
        # При цене 2100: комиссия 210, НДС 100, УСН 80, доставка и закупка те же.
        self.assertEqual(model.forecast(D('2100')).estimated_profit, D('1260'))
        self.assertIn('не загружены', model.limitations[0])

    def test_missing_coverage_is_not_zero_cost(self):
        with self.assertRaisesRegex(ValueError, 'Полнота'):
            historical_model('a', self.ops, self.costs, self.taxes)

    def test_missing_purchase_cost_and_unconfirmed_taxes_block_forecast(self):
        with self.assertRaises(ValueError):
            historical_model('a', self.ops, [], self.taxes, coverage_confirmed=True)
        with self.assertRaises(ValueError):
            historical_model('a', self.ops, self.costs, replace(self.taxes, confirmed_by_user=False), coverage_confirmed=True)

    def test_returns_and_preliminary_operations_are_not_extrapolated(self):
        for extra in [Operation('s', 'report', 'r', DAY, Kind.RETURN, D('-100'), 'a', -1),
                      Operation('s', 'operational', 'p', DAY, Kind.LOGISTICS, D('-10'), 'a', confirmed=False)]:
            with self.assertRaises(ValueError):
                historical_model('a', self.ops + [extra], self.costs, self.taxes, coverage_confirmed=True)

    def test_nonpositive_and_float_prices_rejected(self):
        model = historical_model('a', self.ops, self.costs, self.taxes, coverage_confirmed=True)
        with self.assertRaises(ValueError):
            model.forecast(D('0'))
        with self.assertRaises(TypeError):
            model.forecast(100.1)

    def test_available_fbw_tariff_replaces_historical_fraction_only_in_forecast(self):
        model=historical_model('a',self.ops,self.costs,self.taxes,coverage_confirmed=True,fbw_commission_percent=D('20'))
        result=model.forecast(D('1050'))
        self.assertEqual(result.commission,D('210'))
        self.assertEqual(result.estimated_profit,D('300'))
        self.assertIn('paidStorageKgvp',model.commission_source)

    def test_conservative_break_even_includes_tax_rounding_bound(self):
        model=historical_model('a',self.ops,self.costs,self.taxes,coverage_confirmed=True)
        # После комиссии 10%, НДС 5/105 и УСН 4/105 остаётся 57/70 цены.
        # Фиксированные расходы 450, запас на два округления 0.01:
        # ceil_cent((450.01)*70/57) = 552.65.
        self.assertEqual(model.break_even_price(),D('552.65'))
        self.assertGreaterEqual(model.forecast(model.break_even_price()).estimated_profit,D('0'))
