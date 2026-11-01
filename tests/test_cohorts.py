from datetime import date,datetime,timedelta
from decimal import Decimal as D
import unittest
from wb_profit.cohorts import order_cohorts,cohort_rows

START=date(2026,1,1);END=date(2026,1,7);TODAY=date(2026,2,1)


def order(srid,barcode='123',**extra):
    return {'srid':srid,'nmId':42,'barcode':barcode,'warehouseType':'Склад WB',
            'date':'2026-01-02T10:00:00','isCancel':False,**extra}


def event(srid,identifier,barcode='123',**extra):
    return {'srid':srid,'saleID':identifier,'nmId':42,'barcode':barcode,'warehouseType':'Склад WB',
            'date':'2026-01-10T10:00:00',**extra}


class CohortTests(unittest.TestCase):
    def report(self,orders,sales,**kwargs):
        return order_cohorts(orders,sales,START,END,today=TODAY,**kwargs)

    def test_sales_after_selected_order_period_belong_to_same_cohort_and_unrelated_sales_do_not(self):
        report=self.report([order('bought'),order('cancelled',isCancel=True)],
                           [event('bought','S1'),event('other-order','S2')],history_complete=True)
        row=report.rows[0]
        self.assertEqual((row.orders,row.bought,row.cancelled,row.pending,row.ambiguous),(2,1,1,0,0))
        self.assertEqual(row.buyout_percent,D('50'))

    def test_open_order_keeps_rate_unknown_instead_of_dividing_unrelated_counts(self):
        row=self.report([order('bought'),order('open')],[event('bought','S1')],history_complete=True).rows[0]
        self.assertEqual(row.orders,2)
        self.assertEqual(row.pending,1)
        self.assertIsNone(row.buyout_percent)

    def test_registered_return_after_purchase_is_separate_from_original_buyout(self):
        row=self.report([order('a')],[event('a','S1'),event('a','R1',date='2026-01-20T10:00:00')],history_complete=True).rows[0]
        self.assertEqual(row.bought,1)
        self.assertEqual(row.returned,1)
        self.assertEqual(row.buyout_percent,D('100'))

    def test_cancelled_and_sold_or_return_without_sale_are_ambiguous(self):
        for orders,sales in [([order('a',isCancel=True)],[event('a','S1')]),
                             ([order('a')],[event('a','R1')])]:
            row=self.report(orders,sales,history_complete=True).rows[0]
            self.assertEqual(row.ambiguous,1)
            self.assertIsNone(row.buyout_percent)

    def test_exact_sku_and_warehouse_are_required(self):
        for change in [{'barcode':'other'},{'nmId':99},{'warehouseType':'Склад продавца'}]:
            row=self.report([order('a')],[event('a','S1',**change)],history_complete=True).rows[0]
            self.assertEqual(row.ambiguous,1)
            self.assertIsNone(row.buyout_percent)
        report=self.report([order('fbs',warehouseType='Склад продавца')],[event('fbs','S1')],history_complete=True)
        self.assertEqual(report.rows,())

    def test_duplicates_do_not_inflate_orders_or_sale_outcomes(self):
        orders=[order('a'),order('a')];sales=[event('a','S1'),event('a','S1')]
        row=self.report(orders,sales,history_complete=True).rows[0]
        self.assertEqual((row.orders,row.bought),(1,1))
        self.assertEqual(row.buyout_percent,D('100'))
        report=self.report([order('a'),order('a',barcode='other')],sales,history_complete=True)
        self.assertFalse(report.history_complete)
        self.assertEqual(report.rows,())

    def test_duplicate_distinct_sales_do_not_make_two_purchases_from_one_order(self):
        row=self.report([order('a')],[event('a','S1'),event('a','S2')],history_complete=True).rows[0]
        self.assertEqual(row.ambiguous,1)
        self.assertEqual(row.orders,1)
        self.assertIsNone(row.buyout_percent)

    def test_missing_history_or_unattributed_sales_prevents_final_rate(self):
        row=self.report([order('a')],[event('a','S1')]).rows[0]
        self.assertIsNone(row.buyout_percent)
        report=self.report([order('a',isCancel=True)],[event(None,'S1')],history_complete=True)
        self.assertFalse(report.history_complete)
        self.assertIsNone(report.rows[0].buyout_percent)

    def test_model_aggregation_uses_order_counts_not_average_of_size_percentages(self):
        orders=[order('small','s'),*[order('large'+str(i),'l',isCancel=True) for i in range(3)]]
        report=self.report(orders,[event('small','S1','s')],history_complete=True)
        sizes=cohort_rows(report)
        self.assertEqual(sizes['42:s'].buyout_percent,D('100'))
        self.assertEqual(sizes['42:l'].buyout_percent,D('0'))
        model=cohort_rows(report,grouped=True)['42']
        self.assertEqual(model.orders,4)
        self.assertEqual(model.buyout_percent,D('25'))

    def test_moscow_time_and_event_order_are_checked(self):
        report=self.report([order('a',date='2025-12-31T22:00:00Z')],[event('a','S1')],history_complete=True)
        self.assertEqual(report.rows[0].orders,1)
        row=self.report([order('a')],[event('a','S1',date='2026-01-02T09:00:00')],history_complete=True).rows[0]
        self.assertEqual(row.ambiguous,1)
        row=self.report([order('a')],[event('a','S1',date='2026-02-10T09:00:00')],history_complete=True).rows[0]
        self.assertEqual(row.ambiguous,1)

    def test_invalid_order_fields_do_not_become_zero_or_final_buyout(self):
        for bad in [order('bad',date=None),order('',date='2026-01-02T10:00:00'),None]:
            report=self.report([order('a'),bad],[event('a','S1')],history_complete=True)
            self.assertFalse(report.history_complete)
            self.assertIsNone(report.rows[0].buyout_percent)
        row=self.report([order('a',isCancel=None)],[event('a','S1')],history_complete=True).rows[0]
        self.assertEqual(row.ambiguous,1)

    def test_future_cohort_does_not_get_completed_rate(self):
        report=order_cohorts([order('a')],[event('a','S1')],START,date(2026,3,1),history_complete=True,today=TODAY)
        self.assertFalse(report.history_complete)
        self.assertIsNone(report.rows[0].buyout_percent)

    def test_every_recognized_order_has_exactly_one_outcome(self):
        orders=[order('bought'),order('cancelled',isCancel=True),order('open'),order('conflict',isCancel=True)]
        report=self.report(orders,[event('bought','S1'),event('conflict','S2')],history_complete=True)
        row=report.rows[0]
        self.assertEqual(row.orders,row.bought+row.cancelled+row.pending+row.ambiguous)
