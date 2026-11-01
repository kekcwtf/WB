import os
os.environ.setdefault('QT_QPA_PLATFORM','offscreen')
from datetime import date
from decimal import Decimal as D
import unittest
from PySide6.QtWidgets import QApplication
from wb_profit.finance import Operation,Kind,Cost,TaxSettings
from wb_profit.taxes import TaxRule
from wb_profit.overview_view import OverviewView


class OverviewViewTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):cls.app=QApplication.instance() or QApplication([])

    def setUp(self):
        self.view=OverviewView();self.addCleanup(self.view.close)
        day=date(2026,1,1)
        self.ops=[Operation('s','report','sale',day,Kind.SALE,D('1050'),'a',1),
                  Operation('s','report','commission',day,Kind.COMMISSION,D('-100'),'a')]
        self.costs=[Cost('a',day,D('400'))]
        self.rules=[TaxRule(day,TaxSettings(D('4'),D('5'),confirmed_by_user=True))]

    def test_values_formulas_and_per_category_amounts_appear_in_native_widgets(self):
        self.view.set_operations(self.ops,self.costs,self.rules,financial_complete=True,full_complete=True)
        self.assertEqual(self.view.values['net'].text(),'460.00 ₽')
        self.assertEqual(self.view.values['roi'].text(),'115.00 %')
        self.assertIn('та же база',self.view.values['margin'].toolTip())
        self.assertIn('НДС уже учтён',self.view.quality.text())
        self.assertIn('разница 0.00',self.view.quality.text())
        values={self.view.details.item(row,0).text():self.view.details.item(row,1).value for row in range(self.view.details.rowCount())}
        self.assertEqual(values['Валовая прибыль · без НДС'],D('600'))
        self.assertEqual(values['Операционный результат · до УСН'],D('500'))
        self.assertEqual(values['Комиссия WB'],D('100'))

    def test_incomplete_period_and_clear_remove_previous_net_and_ratios(self):
        self.view.set_operations(self.ops,self.costs,self.rules,financial_complete=True,full_complete=True)
        self.view.set_operations(self.ops,self.costs,self.rules,financial_complete=True,full_complete=False)
        self.assertEqual(self.view.values['net'].text(),'—')
        self.assertEqual(self.view.values['roi'].text(),'—')
        self.assertEqual(self.view.statuses['net'].text(),'недостаточно данных')
        self.assertIn('Полная чистая прибыль недоступна',self.view.quality.text())
        self.view.clear('Неверный период')
        self.assertIsNone(self.view.analysis)
        self.assertTrue(all(label.text()=='—' for label in self.view.values.values()))
        self.assertEqual(self.view.details.rowCount(),0)

    def test_negative_net_is_highlighted_and_absent_unverified_expenses_are_unknown(self):
        costs=[Cost('a',date(2026,1,1),D('1000'))]
        self.view.set_operations(self.ops,costs,self.rules,financial_complete=True,full_complete=True)
        self.assertEqual(self.view.values['net'].text(),'-140.00 ₽')
        self.assertIn('#c0392b',self.view.values['net'].styleSheet())
        self.view.set_operations(self.ops,self.costs,self.rules)
        self.assertEqual(self.view.values['returns'].text(),'—')
        self.assertEqual(self.view.values['sales'].text(),'1 050.00 ₽')
