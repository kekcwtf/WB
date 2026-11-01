import os
os.environ.setdefault('QT_QPA_PLATFORM', 'offscreen')
import tempfile
import unittest
from pathlib import Path
from PySide6.QtWidgets import QApplication
from wb_profit.desktop import Window


class DesktopTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.app = QApplication.instance() or QApplication([])

    def test_empty_state_and_settings_survive_restart(self):
        with tempfile.TemporaryDirectory() as folder:
            path = Path(folder) / 'db.sqlite'
            window = Window(path)
            window.show()
            self.app.processEvents()
            self.assertIn('Нет подтверждённых', window.summary.text())
            window.stores = [{'id': 'shop', 'name': 'Мой магазин'}]
            window.ledger.set_setting('_app', 'stores', window.stores)
            window.store.addItem('Мой магазин', 'shop')
            window.usn.setText('4')
            window.vat.setText('5')
            window.tax_confirmed.setChecked(True)
            window.save_taxes()
            window.cost_sku.setText('SKU-42')
            window.cost_amount.setText('940,25')
            window.save_cost()
            window.period.setCurrentIndex(2)
            window.close()
            reopened = Window(path)
            self.assertEqual(reopened.usn.text(), '4')
            self.assertEqual(reopened.vat.text(), '5')
            self.assertTrue(reopened.tax_confirmed.isChecked())
            self.assertEqual(reopened.ledger.setting('shop', 'costs')[0]['amount'], '940.25')
            self.assertEqual(reopened.period.currentIndex(), 2)
            self.assertEqual(reopened.pages.count(), 8)
            reopened.close()

    def test_sku_detail_and_filter_use_persisted_operations(self):
        from datetime import date
        from decimal import Decimal
        from wb_profit.finance import Operation, Kind
        from PySide6.QtWidgets import QLabel
        with tempfile.TemporaryDirectory() as folder:
            window = Window(Path(folder) / 'db.sqlite')
            window.stores = [{'id': 'shop', 'name': 'Магазин'}]
            window.store.addItem('Магазин', 'shop')
            window.ledger.set_setting('shop', 'costs', [{'sku': 'a', 'date': date.today().isoformat(), 'amount': '400'}])
            window.ledger.upsert([
                Operation('shop', 'report', '1', date.today(), Kind.SALE, Decimal('1000'), 'a', 1),
                Operation('shop', 'report', '2', date.today(), Kind.ADVERTISING, Decimal('-800'), 'a'),
                Operation('shop', 'report', '3', date.today(), Kind.STORAGE, Decimal('-10'))])
            window.refresh()
            self.assertEqual(window.products.rowCount(), 1)
            self.assertEqual(window.products.item(0, 4).value, Decimal('-200'))
            self.assertEqual(window.products.item(0, 4).foreground().color().name(), '#c0392b')
            window.product_search.setText('missing')
            self.assertTrue(window.products.isRowHidden(0))
            window.product_search.setText('a')
            self.assertFalse(window.products.isRowHidden(0))
            dialog = window.make_sku_dialog('a')
            texts = '\n'.join(label.text() for label in dialog.findChildren(QLabel))
            self.assertIn('-200.00', texts)
            self.assertIn('реклама (800.00', texts)
            dialog.close()
            window.close()

    def test_external_only_data_does_not_claim_full_net_profit(self):
        from datetime import date
        from decimal import Decimal
        from wb_profit.expenses import save_external_expense
        with tempfile.TemporaryDirectory() as folder:
            window = Window(Path(folder) / 'db.sqlite')
            window.store.addItem('Магазин', 'shop')
            window.usn.setText('4')
            window.vat.setText('5')
            window.tax_confirmed.setChecked(True)
            window.save_taxes()
            save_external_expense(window.ledger, 'shop', date.today(), Decimal('100'), 'Упаковка')
            window.refresh()
            self.assertIn('Чистая прибыль: недостаточно данных', window.summary.text())
            self.assertEqual(window.operations.model().rowCount(), 1)
            window.close()

    def test_price_scenario_reacts_to_price_and_blocks_unknown_coverage(self):
        from datetime import date
        from decimal import Decimal as D
        from wb_profit.finance import Operation, Kind, Cost, TaxSettings
        from wb_profit.economics_view import EconomicsView
        day = date.today()
        operations = [Operation('s', 'report', '1', day, Kind.SALE, D('2100'), 'a', 2),
                      Operation('s', 'report', '2', day, Kind.COMMISSION, D('-210'), 'a'),
                      Operation('s', 'report', '3', day, Kind.LOGISTICS, D('-100'), 'a')]
        costs = [Cost('a', day, D('400'))]
        taxes = TaxSettings(D('4'), D('5'), confirmed_by_user=True)
        view = EconomicsView()
        view.set_context(operations, costs, taxes)
        self.assertFalse(view.price.isEnabled())
        self.assertIn('Полнота источников', view.output.text())
        view.set_context(operations, costs, taxes, coverage_confirmed=True)
        self.assertTrue(view.price.isEnabled())
        self.assertIn('405.00', view.output.text())
        view.price.setText('2100')
        self.assertIn('1260.00', view.output.text())
        view.price.setText('nan')
        self.assertIn('Введите положительную', view.output.text())
        view.set_context([], [], None)
        self.assertFalse(view.price.isEnabled())
        self.assertIn('Прогноз не рассчитан', view.output.text())
        view.close()

    def test_native_daily_chart_keeps_decimal_and_omits_incomplete_days(self):
        from datetime import date
        from decimal import Decimal as D
        from wb_profit.finance import Operation, Kind, Cost
        from wb_profit.charts import DailyChart
        chart=DailyChart()
        operations=[Operation('s','report','1',date(2026,1,1),Kind.SALE,D('1000.10'),'a',1),
                    Operation('s','report','2',date(2026,1,2),Kind.SALE,D('1000'),'b',1)]
        chart.set_operations(operations,[Cost('a',date(2025,1,1),D('400.03'))])
        self.assertEqual(chart.balances,{date(2026,1,1):D('600.07')})
        self.assertEqual(chart.chart().series()[0].count(),1)
        chart.set_operations([],[])
        self.assertEqual(chart.balances,{})
        chart.close()

    def test_background_sync_does_not_block_qt_event_loop(self):
        import asyncio
        import time
        from unittest.mock import patch
        from PySide6.QtCore import QEventLoop, QTimer
        from wb_profit.sync_service import SyncWorker
        ticks=[]
        async def slow_download(path,shop,token,notify):
            await asyncio.sleep(0.08)
            notify(shop,'orders','Проверено')
        with tempfile.TemporaryDirectory() as folder:
            with patch('wb_profit.sync_service.load_token',return_value='test-only'),patch('wb_profit.sync_service.synchronize_shop',slow_download):
                worker=SyncWorker(str(Path(folder)/'db.sqlite'),['shop'])
                loop=QEventLoop()
                timer=QTimer()
                timer.setInterval(10)
                timer.timeout.connect(lambda:ticks.append(time.monotonic()))
                worker.finished.connect(loop.quit)
                QTimer.singleShot(2000,loop.quit)
                timer.start();worker.start();loop.exec();timer.stop()
                self.assertTrue(worker.wait(2000))
                self.assertGreaterEqual(len(ticks),2)

    def test_product_photo_hosts_are_restricted_and_do_not_accept_userinfo(self):
        from wb_profit.images import allowed_image
        self.assertTrue(allowed_image('https://basket-01.wbbasket.ru/a.jpg'))
        for url in ['http://basket-01.wbbasket.ru/a.jpg','https://evil.test/a.jpg','https://wbbasket.ru.evil.test/a.jpg','https://secret@basket-01.wbbasket.ru/a.jpg']:
            self.assertFalse(allowed_image(url))

    def test_group_sizes_preserves_costs_and_combined_result(self):
        from datetime import date
        from decimal import Decimal as D
        from wb_profit.finance import Operation,Kind
        with tempfile.TemporaryDirectory() as folder:
            window=Window(Path(folder)/'db.sqlite')
            window.store.addItem('Магазин','shop')
            day=date.today()
            window.ledger.set_setting('shop','costs',[{'sku':'42:s','date':day.isoformat(),'amount':'400'},{'sku':'42:l','date':day.isoformat(),'amount':'500'}])
            window.ledger.upsert([Operation('shop','report','a',day,Kind.SALE,D('1000'),'42:s',1),Operation('shop','report','b',day,Kind.SALE,D('1200'),'42:l',1)])
            window.refresh()
            self.assertEqual(window.products.rowCount(),2)
            window.grouping.setCurrentIndex(1)
            self.assertEqual(window.products.rowCount(),1)
            self.assertEqual(window.products.item(0,4).value,D('1300'))
            self.assertEqual(window.products.item(0,3).value,D('900'))
            window.close()

    def test_return_detail_shows_original_sale_outside_current_period(self):
        from datetime import date
        from decimal import Decimal as D
        from PySide6.QtCore import QDate
        from PySide6.QtWidgets import QTableWidget
        from wb_profit.finance import Operation,Kind
        with tempfile.TemporaryDirectory() as folder:
            window=Window(Path(folder)/'db.sqlite')
            window.store.addItem('Магазин','shop')
            window.ledger.set_setting('shop','costs',[{'sku':'42:barcode','date':'2026-01-01','amount':'400'},
                                                      {'sku':'42:barcode','date':'2026-01-15','amount':'700'}])
            window.ledger.upsert([Operation('shop','wb_finance','1:2:revenue',date(2026,2,1),Kind.RETURN,D('-1000'),'42:barcode',-1,
                                           original_sale_id='1:1:revenue',cost_basis_date=date(2026,1,1))])
            window.period.setCurrentIndex(6)
            window.start.setDate(QDate(2026,2,1));window.end.setDate(QDate(2026,2,1))
            window.refresh()
            self.assertEqual(window.products.item(0,3).value,D('-400'))
            self.assertIn('Налоговая периодизация возвратов',window.summary.text())
            dialog=window.make_sku_dialog('42:barcode')
            history=next(table for table in dialog.findChildren(QTableWidget) if table.columnCount()==7)
            self.assertEqual(history.item(0,5).text(),'1:1:revenue')
            self.assertEqual(history.item(0,6).text(),'2026-01-01')
            dialog.close();window.close()

    def test_advertising_screen_does_not_turn_orders_or_topups_into_profit(self):
        from datetime import date
        from decimal import Decimal as D
        from PySide6.QtCore import QDate
        from wb_profit.sync import SyncStore,Page,Record
        from wb_profit.finance import Operation,Kind
        with tempfile.TemporaryDirectory() as folder:
            path=Path(folder)/'db.sqlite';window=Window(path);window.store.addItem('Магазин','shop')
            cache=SyncStore(path)
            def put(source,rows):cache.save_page('shop',source,Page(tuple(Record(str(i),r) for i,r in enumerate(rows)),None,True))
            put('ad_account',[{'balance':500,'net':1000,'bonus':50,'currency':'RUB'}])
            put('ad_payments',[{'id':1,'date':'2026-01-05T12:00:00+03:00','sum':1000,'currency':'RUB','statusId':1,'type':0}])
            put('advertising',[{'advertId':7,'updSum':200,'updTime':'2026-01-05T12:00:00+03:00','paymentType':'Баланс','updNum':1}])
            stats={'advertId':7,'currency':'RUB','sum':'200','sum_price':'10000','days':[{'date':'2026-01-05T00:00:00Z','sum':'200','sum_price':'10000','views':100,'clicks':10,'orders':2,
                   'apps':[{'sum':'200','nms':[{'nmId':42,'sum':'200'}]}]}]}
            put('ad_statistics',[{'request':{'advertId':7,'from':'2026-01-01','to':'2026-01-31'},'data':stats}])
            put('finance_weekly',[{'reportId':1,'rrDate':'2026-01-05'}]);put('report_totals',[{'reportId':1,'dateTo':'2026-01-31'}])
            for source in ['advertising','ad_statistics','finance_weekly','report_totals']:
                window.ledger.set_setting('shop','source_coverage:'+source,[['2026-01-01','2026-01-31']])
            window.ledger.set_setting('shop','report_audits',[{'report_id':'1','verified':True,'difference':'0'}])
            original=Operation('shop','wb_finance','sale',date(2026,1,5),Kind.SALE,D('2000'),'42:barcode',1)
            window.ledger.upsert([original])
            window.period.setCurrentIndex(6);window.start.setDate(QDate(2026,1,1));window.end.setDate(QDate(2026,1,31));window.refresh()
            view=window.advertising_view
            self.assertIn('ДРР по статистике и подтверждённой выручке магазина: 10.00%',view.summary.text())
            self.assertIn('нет подтверждённой привязки',view.summary.text())
            self.assertEqual(view.campaigns.item(0,6).value,D('10000'))
            self.assertEqual(view.payments.item(0,2).value,D('1000'))
            self.assertEqual(window.ledger.operations('shop',date.min,date.max),[original])
            self.assertIsNone(window.products.item(0,22).value)
            window.grouping.setCurrentIndex(1)
            self.assertEqual(window.products.item(0,22).value,D('200'))
            self.assertIn('Чистая прибыль: недостаточно данных',window.summary.text())
            window.start.setDate(QDate(2026,2,1))
            self.assertEqual(view.campaigns.rowCount(),0)
            self.assertEqual(view.charges.rowCount(),0)
            self.assertIn('Дата начала периода',view.summary.text())
            cache.close();window.close()

    def test_today_rolls_forward_when_application_remains_open_overnight(self):
        from datetime import date
        from unittest.mock import patch
        from PySide6.QtCore import QDate
        with tempfile.TemporaryDirectory() as folder:
            with patch('wb_profit.desktop.business_today',return_value=date(2026,1,4)):
                window=Window(Path(folder)/'db.sqlite');window.period.setCurrentIndex(0)
                self.assertEqual(window.start.date(),QDate(2026,1,4))
            with patch('wb_profit.desktop.business_today',return_value=date(2026,1,5)),patch.object(window,'start_sync'):
                window.sync_all()
            self.assertEqual(window.start.date(),QDate(2026,1,5))
            self.assertEqual(window.end.date(),QDate(2026,1,5))
            window.close()

    def test_dated_tax_settings_preserve_past_rates_and_future_rules(self):
        from datetime import date
        from decimal import Decimal as D
        from unittest.mock import patch
        from PySide6.QtCore import QDate
        from wb_profit.finance import Operation,Kind
        with tempfile.TemporaryDirectory() as folder,patch('wb_profit.desktop.business_today',return_value=date(2026,11,1)):
            path=Path(folder)/'db.sqlite';window=Window(path)
            window.stores=[{'id':'shop','name':'Магазин'}]
            window.ledger.set_setting('_app','stores',window.stores)
            window.store.addItem('Магазин','shop')
            for when,usn,vat in [('2026-01-01','4','5'),('2026-07-01','6','7'),('2027-01-01','10','20')]:
                window.tax_date.setDate(QDate.fromString(when,'yyyy-MM-dd'))
                window.usn.setText(usn);window.vat.setText(vat);window.tax_confirmed.setChecked(True);window.save_taxes()
            window.ledger.set_setting('shop','costs',[{'sku':'a','date':'2026-01-01','amount':'400'}])
            window.ledger.upsert([Operation('shop','report','a',date(2026,6,30),Kind.SALE,D('1050'),'a',1),
                                  Operation('shop','report','b',date(2026,7,1),Kind.SALE,D('1070'),'a',1)])
            window.period.setCurrentIndex(6);window.start.setDate(QDate(2026,6,30));window.end.setDate(QDate(2026,7,1));window.refresh()
            self.assertIn('Расчётный УСН: 100.00',window.summary.text())
            self.assertIn('Расчётный НДС: 120.00',window.summary.text())
            self.assertEqual(window.tax_breakdown.rowCount(),2)
            self.assertEqual(window.tax_history.rowCount(),3)
            window.close();window=Window(path)
            self.assertEqual(window.usn.text(),'6') # будущая ставка не отображается как текущая
            self.assertEqual(window.vat.text(),'7')
            self.assertEqual(window.tax_history.rowCount(),3)
            window.close()

    def test_verified_product_profit_and_explicit_common_allocation(self):
        from datetime import date
        from decimal import Decimal as D
        from PySide6.QtCore import QDate
        from wb_profit.finance import Operation,Kind
        from wb_profit.sync import SyncStore,Page,Record
        from wb_profit.taxes import TaxRule,save_rule
        from wb_profit.finance import TaxSettings
        with tempfile.TemporaryDirectory() as folder:
            path=Path(folder)/'db.sqlite';window=Window(path)
            window.stores=[{'id':'shop','name':'Магазин'}];window.ledger.set_setting('_app','stores',window.stores);window.store.addItem('Магазин','shop')
            day=date(2026,1,1)
            save_rule(window.ledger,'shop',TaxRule(day,TaxSettings(D('4'),D('5'),confirmed_by_user=True)))
            window.ledger.set_setting('shop','costs',[{'sku':'a','date':day.isoformat(),'amount':'400'},{'sku':'b','date':day.isoformat(),'amount':'800'}])
            window.ledger.upsert([Operation('shop','report','a',day,Kind.SALE,D('1050'),'a',1),Operation('shop','report','b',day,Kind.SALE,D('2100'),'b',1),Operation('shop','report','fee',day,Kind.STORAGE,D('-90'))])
            cache=SyncStore(path)
            cache.save_page('shop','finance_weekly',Page((Record('1:1',{'reportId':1,'rrDate':day.isoformat()}),),None,True))
            cache.save_page('shop','report_totals',Page((Record('1',{'reportId':1,'dateTo':'2026-01-07'}),),None,True))
            for source in ['finance_weekly','report_totals','advertising']:
                window.ledger.set_setting('shop','source_coverage:'+source,[['2026-01-01','2026-01-07']])
            window.ledger.set_setting('shop','report_audits',[{'report_id':'1','verified':True,'difference':'0'}])
            window.period.setCurrentIndex(6);window.start.setDate(QDate(2026,1,1));window.end.setDate(QDate(2026,1,7));window.refresh()
            def profits():return {window.products.item(row,0).text():window.products.item(row,5).value for row in range(window.products.rowCount())}
            self.assertEqual(profits(),{'a':D('560'),'b':D('1120')})
            self.assertIn('Чистая прибыль: 1590.00',window.summary.text())
            self.assertEqual(window.overview_view.analysis.metrics['net'].value,D('1590'))
            self.assertEqual(window.overview_view.analysis.metrics['gross'].value,D('1800'))
            self.assertEqual(window.overview_view.analysis.metrics['operating'].value,D('1710'))
            self.assertEqual(window.daily_chart.report.period_net,D('1590'))
            self.assertEqual(sum(window.daily_chart.balances.values()),D('1590'))
            self.assertIn('разница: 0.00',window.daily_note.text())
            self.assertEqual(window.daily_table.rowCount(),7)
            window.allocation_mode.setCurrentIndex(1)
            self.assertEqual(profits(),{'a':D('530'),'b':D('1060')})
            self.assertIn('Разница суммы SKU и результата магазина: 0.00',window.reconciliation.text())
            original=window.ledger.operations('shop',date.min,date.max)
            self.assertEqual(len(original),3) # распределение не создало новые расходы в реестре
            window.chart_mode.setCurrentIndex(1)
            cache.close();window.close();window=Window(path)
            self.assertEqual(window.chart_mode.currentData(),'before_tax')
            self.assertEqual(window.allocation_mode.currentIndex(),1)
            self.assertEqual(profits(),{'a':D('530'),'b':D('1060')})
            from PySide6.QtWidgets import QLabel,QTableWidget
            dialog=window.make_sku_dialog('a')
            self.assertIn('чистая прибыль 530.00',dialog.findChild(QLabel,'profitExplanation').text())
            self.assertNotIn('ещё не определена',dialog.findChild(QLabel,'profitExplanation').text())
            details=dialog.findChild(QTableWidget,'profitCategories')
            values={details.item(row,0).text():details.item(row,1).value for row in range(details.rowCount())}
            self.assertEqual(sum(values.values()),D('530'))
            self.assertEqual(values['Расчётный УСН (распределённый)'],D('-40'))
            self.assertEqual(values['Расчётный НДС (распределённый)'],D('-50'))
            self.assertEqual(values['Общие расходы (распределённый вклад)'],D('-30'))
            self.assertIn('разница с чистой прибылью: 0.00',dialog.findChild(QLabel,'profitCheck').text())
            dialog.close()
            window.ledger.set_setting('shop','costs',[{'sku':'a','date':day.isoformat(),'amount':'1000'},{'sku':'b','date':day.isoformat(),'amount':'800'}])
            window.refresh()
            self.assertEqual(profits()['a'],D('-70'))
            dialog=window.make_sku_dialog('a')
            self.assertIn('чистая прибыль -70.00',dialog.findChild(QLabel,'profitExplanation').text())
            self.assertIn('убыток возникает после их учёта',dialog.findChild(QLabel,'profitExplanation').text())
            dialog.close()
            window.ledger.set_setting('shop','source_coverage:advertising',[])
            window.refresh()
            self.assertIsNone(profits()['a'])
            self.assertIsNone(window.overview_view.analysis.metrics['net'].value)
            self.assertEqual(window.overview_view.values['net'].text(),'—')
            dialog=window.make_sku_dialog('a')
            self.assertIn('недостаточно данных',dialog.findChild(QLabel,'profitMetrics').text())
            self.assertNotIn('чистая прибыль -70.00',dialog.findChild(QLabel,'profitExplanation').text())
            dialog.close();window.close()
