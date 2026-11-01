import os
os.environ.setdefault('QT_QPA_PLATFORM','offscreen')
from datetime import date,timedelta
from decimal import Decimal as D
import unittest
from PySide6.QtWidgets import QApplication
from PySide6.QtCharts import QCategoryAxis
from wb_profit.charts import DailyChart
from wb_profit.finance import Operation,Kind,Cost,TaxSettings
from wb_profit.taxes import TaxRule

DAY=date(2026,1,1)


class DailyChartTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):cls.app=QApplication.instance() or QApplication([])

    def setUp(self):
        self.chart=DailyChart();self.addCleanup(self.chart.close)
        self.ops=[Operation('s','report',str(i),DAY+timedelta(days=i),Kind.SALE,D('1050'),'a',1) for i in (0,2)]
        self.costs=[Cost('a',DAY,D('400'))]
        self.rules=[TaxRule(DAY,TaxSettings(D('4'),D('5'),confirmed_by_user=True))]

    def test_incomplete_days_are_real_line_breaks_and_dates_are_business_calendar_dates(self):
        self.chart.set_operations(self.ops,self.costs,start=DAY,end=DAY+timedelta(days=2))
        self.assertEqual(self.chart.balances,{DAY:D('650'),DAY+timedelta(days=2):D('650')})
        self.assertEqual(self.chart.segments,((DAY,),(DAY+timedelta(days=2),)))
        self.assertEqual(len(self.chart.chart().series()),2)
        self.assertTrue(all(series.count()==1 for series in self.chart.chart().series()))
        axis=next(axis for axis in self.chart.chart().axes() if isinstance(axis,QCategoryAxis))
        self.assertEqual(axis.categoriesLabels(),['01.01.2026','02.01.2026','03.01.2026'])
        self.chart.set_operations(self.ops,self.costs,start=DAY-timedelta(days=1),end=DAY+timedelta(days=3))
        axis=next(axis for axis in self.chart.chart().axes() if isinstance(axis,QCategoryAxis))
        self.assertEqual(axis.categoriesLabels()[0],'31.12.2025')
        self.assertEqual(axis.categoriesLabels()[-1],'04.01.2026')
        self.assertEqual(len(self.chart.balances),2)

    def test_net_mode_does_not_silently_fallback_to_before_tax(self):
        self.chart.set_operations(self.ops,self.costs,self.rules,mode='net')
        self.assertEqual(self.chart.mode,'net')
        self.assertEqual(self.chart.balances,{})
        self.assertEqual(self.chart.chart().series(),[])
        self.assertIn('недоступна',self.chart.chart().title())

    def test_full_period_daily_net_sums_to_store_and_zero_days_are_explicit(self):
        self.chart.set_operations(self.ops,self.costs,self.rules,mode='net',coverage_confirmed=True,
                                  start=DAY,end=DAY+timedelta(days=2))
        self.assertEqual(self.chart.balances,{DAY:D('560'),DAY+timedelta(days=1):D('0'),DAY+timedelta(days=2):D('560')})
        self.assertEqual(sum(self.chart.balances.values()),self.chart.report.period_net)
        self.assertEqual(self.chart.report.difference,D('0'))
        self.assertEqual(len(self.chart.chart().series()),1)
        self.assertTrue(all(isinstance(value,D) for value in self.chart.balances.values()))
