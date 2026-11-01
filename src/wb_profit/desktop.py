"""Нативный интерфейс. До подключения проверенных источников не рисует показатели."""
import sys
import sqlite3
import uuid
from datetime import date, timedelta, datetime, timezone
from decimal import Decimal, InvalidOperation
from pathlib import Path

from PySide6.QtCore import QDate, Qt, QTimer, QSize, QLocale, QTranslator, QLibraryInfo, QUrl
from PySide6.QtGui import QColor, QIcon, QDesktopServices
from PySide6.QtWidgets import (
    QApplication, QMainWindow, QWidget, QHBoxLayout, QVBoxLayout, QListWidget,
    QStackedWidget, QLabel, QComboBox, QPushButton, QTableWidget, QTableWidgetItem,
    QFormLayout, QLineEdit, QCheckBox, QMessageBox, QDateEdit, QFileDialog,
    QInputDialog, QHeaderView, QAbstractItemView, QDialog, QDialogButtonBox, QScrollArea, QSpinBox,
)
from .finance import Cost, TaxSettings, calculate, Kind
from .storage import Ledger
from .backup_worker import BackupWorker,AutoBackupWorker
from .auto_backups import options as backup_options,backup_due,backup_folder
from .startup import SessionLock,StartupError,active_database
from .startup_dialog import StartupDialog
from .retries import plans,due_shops,network_recovered,clear_shop
from .secrets import save_token
from .analytics import analyse_sku, analyse_group, LABELS
from .operations_view import OperationsView
from .operation_review_view import OperationReviewView
from .revision_history_view import RevisionHistoryView
from .expenses import save_external_expense, cancel_external_expense
from .expense_archive_view import ExpenseArchiveDialog
from .cost_history import change_cost, undo_cost_change
from .cost_changes_view import CostChangesDialog
from .cost_coverage_view import CostCoverageDialog
from .tax_changes import change_rule, undo_change as undo_tax_change
from .tax_changes_view import TaxChangesDialog
from .economics_view import EconomicsView
from .sync_service import SyncWorker, SOURCE_LABELS
from .sync import SyncStore
from .api.auth import inspect_token, CATEGORIES
from .access import save_access
from .access_view import AccessView
from .source_coverage import source_coverage
from .tariff_cache import forecast_rates
from .data_views import advertising_rows, product_metadata, records
from .inventory_view import InventoryView
from .overview_view import OverviewView
from .return_review_view import ReturnReviewView
from .cohort_view import CohortView
from .charts import DailyChart
from .coverage import period_coverage,financial_period_reasons
from .comparison import compare_metrics
from .images import ProductImages


from .ui_tables import NumericItem,read_only_table
from .advertising_view import AdvertisingView


from .business_dates import business_today,MOSCOW
from .taxes import TaxRule,load_rules,save_rule,rule_at,calculate_with_history
from .profit_distribution import distribute_profit
from .profit_details import product_profit_details

def today_qdate():
    day=business_today()
    return QDate(day.year,day.month,day.day)


class Window(QMainWindow):
    def __init__(self, database: str | Path | None = None):
        super().__init__()
        self.ledger=None
        try:
            self.initialize(database)
        except Exception:
            if self.ledger is not None:self.ledger.close()
            self.deleteLater()
            raise

    def initialize(self,database):
        self.setWindowTitle('WB Profit Desktop')
        self.setLocale(QLocale(QLocale.Language.Russian,QLocale.Country.Russia))
        self.resize(1240, 800)
        if database is None:
            from PySide6.QtCore import QStandardPaths
            folder = Path(QStandardPaths.writableLocation(QStandardPaths.AppLocalDataLocation))
            folder.mkdir(parents=True, exist_ok=True)
            database = active_database(folder)
        self.database_path = str(database)
        self.worker = None
        self.maintenance = None
        self.auto_backup_worker=None
        self.after_backup_sync=None
        self.auto_backup_error=''
        self.network_disconnected=False
        self.awaiting_network=False
        self.network_retry_pending=False
        self.deferred_sync=False
        self.closing = False
        self.ledger = Ledger(database)
        sync_storage = SyncStore(database)
        sync_storage.close()
        self.stores = self.ledger.setting('_app', 'stores', [])
        self.current_store = ''
        self.root = QWidget()
        self.setCentralWidget(self.root)
        main = QHBoxLayout(self.root)
        self.nav = QListWidget()
        self.nav.addItems(['Обзор', 'Товары', 'Прибыль и расходы', 'Реклама', 'Юнит-экономика', 'Склад', 'Сверка', 'Настройки'])
        self.nav.setMaximumWidth(200)
        main.addWidget(self.nav)
        content = QVBoxLayout()
        main.addLayout(content, 1)
        toolbar = QHBoxLayout()
        self.store = QComboBox()
        for item in self.stores:
            self.store.addItem(item['name'], item['id'])
        selected_store = self.ledger.setting('_app', 'selected_store')
        selected_index = self.store.findData(selected_store)
        if selected_index >= 0:
            self.store.setCurrentIndex(selected_index)
        self.store.currentIndexChanged.connect(self.change_store)
        toolbar.addWidget(QLabel('Магазин'))
        toolbar.addWidget(self.store)
        add = QPushButton('Добавить магазин')
        add.clicked.connect(self.add_store)
        toolbar.addWidget(add)
        self.period = QComboBox()
        self.period.addItems(['Сегодня', 'Вчера', 'Последние 7 дней', 'Последние 30 дней', 'Текущий месяц', 'Предыдущий месяц', 'Произвольный период'])
        self.period.currentIndexChanged.connect(self.change_period)
        toolbar.addWidget(self.period)
        self.start = QDateEdit(today_qdate())
        self.end = QDateEdit(today_qdate())
        for editor in [self.start, self.end]:
            editor.setCalendarPopup(True)
            editor.setDisplayFormat('dd.MM.yyyy')
            editor.dateChanged.connect(self.custom_period)
            toolbar.addWidget(editor)
        self.sync_button = QPushButton('Обновить сейчас')
        self.sync_button.clicked.connect(self.sync_current)
        toolbar.addWidget(self.sync_button)
        content.addLayout(toolbar)
        self.sync_message = QLabel('Синхронизация ещё не выполнялась')
        self.sync_message.setWordWrap(True)
        self.sync_message.setTextFormat(Qt.TextFormat.PlainText)
        content.addWidget(self.sync_message)
        self.connection_status=QLabel()
        self.connection_status.setTextFormat(Qt.TextFormat.PlainText)
        self.connection_status.setWordWrap(True)
        content.addWidget(self.connection_status)
        self.backup_warning=QLabel()
        self.backup_warning.setWordWrap(True);self.backup_warning.setTextFormat(Qt.TextFormat.PlainText)
        self.backup_warning.setStyleSheet('color: #a66c00')
        content.addWidget(self.backup_warning);self.backup_warning.hide()
        notice = QLabel('Загрузка выполняется только через официальный API WB. Полная чистая прибыль недоступна до сверки всех расходов, рекламы и налоговой базы. Отсутствие данных не означает нулевую прибыль.')
        self.notice = notice
        notice.setWordWrap(True)
        notice.setStyleSheet('background: #fff3cd; color: #624b00; padding: 12px; border-radius: 6px')
        content.addWidget(notice)
        self.pages = QStackedWidget()
        content.addWidget(self.pages, 1)
        overview = QWidget()
        overview_layout = QVBoxLayout(overview)
        title = QLabel('Прибыль магазина')
        title.setStyleSheet('font-size: 26px; font-weight: 600')
        overview_layout.addWidget(title)
        self.overview_view=OverviewView()
        overview_layout.addWidget(self.overview_view)
        self.summary = QLabel('Нет подтверждённых финансовых операций')
        self.summary.setWordWrap(True)
        self.summary.setTextFormat(Qt.TextFormat.PlainText)
        summary_toggle=QPushButton('Показать подробные ограничения расчёта')
        summary_toggle.setCheckable(True)
        summary_toggle.toggled.connect(self.summary.setVisible)
        overview_layout.addWidget(summary_toggle)
        overview_layout.addWidget(self.summary)
        self.summary.hide()
        self.comparison=QLabel()
        self.comparison.setWordWrap(True)
        self.comparison.setTextFormat(Qt.TextFormat.PlainText)
        overview_layout.addWidget(self.comparison)
        self.chart_mode=QComboBox()
        self.chart_mode.addItem('Чистая прибыль по дням · расчёт','net')
        self.chart_mode.addItem('До НДС и УСН · загруженные проводки','before_tax')
        self.chart_mode.currentIndexChanged.connect(self.save_chart_mode)
        overview_layout.addWidget(self.chart_mode)
        self.daily_chart = DailyChart()
        overview_layout.addWidget(self.daily_chart)
        self.daily_note=QLabel()
        self.daily_note.setWordWrap(True);self.daily_note.setTextFormat(Qt.TextFormat.PlainText)
        overview_layout.addWidget(self.daily_note)
        daily_toggle=QPushButton('Показать расчёт по дням')
        daily_toggle.setCheckable(True)
        self.daily_table=self.table(['Дата','Выручка, ₽','Расходы и доходы: вклад, ₽','Себестоимость, ₽','До НДС и УСН, ₽','УСН распределённый, ₽','НДС распределённый, ₽','Чистая прибыль, ₽','Достоверность'])
        self.daily_table.setMaximumHeight(240)
        daily_toggle.toggled.connect(self.daily_table.setVisible)
        overview_layout.addWidget(daily_toggle);overview_layout.addWidget(self.daily_table)
        self.daily_table.hide()
        self.source_table = self.table(['Источник', 'Последнее полное обновление', 'Состояние', 'Загрузка выбранного периода'])
        self.source_table.setMaximumHeight(180)
        overview_layout.addWidget(self.source_table)
        overview_layout.addStretch()
        overview_scroll=QScrollArea()
        overview_scroll.setWidgetResizable(True);overview_scroll.setWidget(overview)
        self.pages.addWidget(overview_scroll)
        self.products = self.table(['SKU', 'Выручка, ₽', 'Расходы и доходы, ₽', 'Себестоимость, ₽', 'Результат до налога, ₽', 'Расчётная чистая прибыль, ₽', 'Артикул продавца','Артикул WB','Название','Категория','Размер','Баркод','Продажи','Возвраты','Комиссия, ₽','Логистика, ₽','Обратная логистика, ₽','Хранение, ₽','Прибыль на единицу после налогов, ₽','Маржа после налогов, %','ROI после налогов, %','Фото','Реклама по модели, отдельный отчёт, ₽','Распределённые налоги, ₽','Распределённые общие расходы, ₽','Заказы · когорта, распознано','Выкуп завершённой когорты, %','Незавершённые / неоднозначные заказы'])
        for column in (25,26,27):
            self.products.horizontalHeaderItem(column).setToolTip('Заказы выбраны по дате создания, исходы — из доступной истории по srid + SKU. Это предварительная когорта, а не отношение финансовых продаж и заказов одного периода. Подробности в режиме «Заказы и выкуп».')
        self.products.setIconSize(QSize(48,48))
        self.products.verticalHeader().setDefaultSectionSize(52)
        self.photo_urls = {}
        self.images = ProductImages(Path(self.database_path).parent/'image-cache',self)
        self.images.ready.connect(self.photo_ready)
        product_page = QWidget()
        product_layout = QVBoxLayout(product_page)
        self.product_search = QLineEdit()
        self.product_search.setPlaceholderText('SKU, артикул или название')
        self.product_search.textChanged.connect(self.filter_products)
        product_layout.addWidget(self.product_search)
        self.product_note=QLabel('Двойной щелчок — расшифровка. Без полного расчёта цвет отражает результат до налогов; жёлтый — маржа ниже 10%.')
        self.product_note.setWordWrap(True)
        self.product_mode=QComboBox()
        self.product_mode.addItem('Прибыль по финансовым операциям','profit')
        self.product_mode.addItem('Заказы и выкуп · когорты, предварительно','orders')
        product_layout.addWidget(self.product_mode)
        product_layout.addWidget(self.product_note)
        cost_check = QPushButton('Проверить себестоимость за выбранный период')
        cost_check.clicked.connect(self.show_cost_coverage)
        product_layout.addWidget(cost_check)
        self.product_tables=QStackedWidget()
        self.product_tables.addWidget(self.products)
        self.cohort_view=CohortView()
        self.product_tables.addWidget(self.cohort_view)
        product_layout.addWidget(self.product_tables)
        self.grouping = QComboBox()
        self.grouping.addItems(['По SKU / размеру','По модели (артикул WB)'])
        self.grouping.currentIndexChanged.connect(self.refresh)
        product_layout.insertWidget(1,self.grouping)
        self.category_filter = QComboBox()
        self.category_filter.addItem('Все категории')
        self.category_filter.currentTextChanged.connect(self.filter_products)
        product_layout.insertWidget(1,self.category_filter)
        self.profit_filter=QComboBox()
        self.profit_filter.addItems(['Все результаты','Положительный результат','Отрицательный результат','Недостаточно данных для чистой прибыли'])
        self.profit_filter.currentIndexChanged.connect(self.filter_products)
        product_layout.insertWidget(2,self.profit_filter)
        self.product_mode.currentIndexChanged.connect(self.save_products_mode)
        self.products.cellDoubleClicked.connect(self.open_sku)
        self.pages.addWidget(product_page)
        self.operations = OperationsView()
        self.operations.doubleClicked.connect(lambda index: self.edit_external_expense(index.row(), index.column()))
        finance_page=QWidget()
        finance_layout=QVBoxLayout(finance_page)
        self.tax_note=QLabel('Налоговая оценка не рассчитана')
        self.tax_note.setWordWrap(True)
        finance_layout.addWidget(self.tax_note)
        self.tax_breakdown=self.table(['Настройки с','Год','Квартал','Выручка с НДС, ₽','Расчётный УСН, ₽','Расчётный НДС, ₽'])
        self.tax_breakdown.setMaximumHeight(190)
        finance_layout.addWidget(self.tax_breakdown)
        finance_layout.addWidget(self.operations)
        self.pages.addWidget(finance_page)
        self.advertising_view=AdvertisingView()
        self.ad_table=self.advertising_view.charges
        self.pages.addWidget(self.advertising_view)
        self.economics = EconomicsView()
        self.pages.addWidget(self.economics)
        self.inventory_view=InventoryView()
        self.stock_table=self.inventory_view.table
        self.pages.addWidget(self.inventory_view)
        self.reconciliation = QLabel()
        self.reconciliation.setWordWrap(True)
        self.reconciliation.setAlignment(Qt.AlignmentFlag.AlignTop)
        reconciliation_page=QWidget()
        reconciliation_layout=QVBoxLayout(reconciliation_page)
        reconciliation_layout.addWidget(self.reconciliation)
        self.operation_review = OperationReviewView()
        reconciliation_layout.addWidget(self.operation_review)
        self.revision_history = RevisionHistoryView()
        reconciliation_layout.addWidget(self.revision_history)
        self.return_review=ReturnReviewView()
        reconciliation_layout.addWidget(self.return_review)
        reconciliation_scroll=QScrollArea()
        reconciliation_scroll.setWidgetResizable(True);reconciliation_scroll.setWidget(reconciliation_page)
        self.pages.addWidget(reconciliation_scroll)
        self.build_settings()
        self.nav.currentRowChanged.connect(self.pages.setCurrentIndex)
        self.nav.setCurrentRow(0)
        self.change_store()
        self.api_refresh_timer=QTimer(self)
        self.api_refresh_timer.setSingleShot(True)
        self.api_refresh_timer.timeout.connect(self.refresh)
        self.sync_timer = QTimer(self)
        self.sync_timer.setInterval(30 * 60 * 1000)
        self.sync_timer.timeout.connect(self.sync_all)
        self.sync_timer.start()
        self.retry_timer=QTimer(self)
        self.retry_timer.setInterval(60*1000)
        self.retry_timer.timeout.connect(self.retry_pending)
        self.retry_timer.start()
        self.auto_backup_timer=QTimer(self)
        self.auto_backup_timer.setInterval(15*60*1000)
        self.auto_backup_timer.timeout.connect(self.maybe_auto_backup)
        self.auto_backup_timer.start()
        self.update_backup_status()
        if QApplication.platformName()!='offscreen':QTimer.singleShot(0,self.maybe_auto_backup)
        self.setup_connectivity()
        self.update_retry_status()
        if sys.platform == 'win32' and QApplication.platformName() != 'offscreen':
            QTimer.singleShot(500, self.sync_all)

    def update_backup_status(self):
        if self.closing:return
        config=backup_options(self.ledger)
        self.auto_backup_enabled.blockSignals(True);self.auto_backup_hours.blockSignals(True);self.auto_backup_keep.blockSignals(True)
        self.auto_backup_enabled.setChecked(config['enabled']);self.auto_backup_hours.setValue(config['interval_hours']);self.auto_backup_keep.setValue(config['keep'])
        self.auto_backup_enabled.blockSignals(False);self.auto_backup_hours.blockSignals(False);self.auto_backup_keep.blockSignals(False)
        state=self.ledger.setting('_app','auto_backup_state',{})
        stamp=state.get('success_at') if isinstance(state,dict) else None
        display=None
        if isinstance(stamp,str):
            try:
                parsed=datetime.fromisoformat(stamp)
                if parsed.tzinfo is not None:display=parsed.astimezone(MOSCOW).strftime('%d.%m.%Y %H:%M МСК')
            except ValueError:pass
        text=('Автокопии включены. ' if config['enabled'] else 'Автокопии отключены. ')+('Последняя успешная: '+display if display else 'Успешных автокопий пока нет.')
        if self.auto_backup_error:text+=' '+self.auto_backup_error
        self.auto_backup_status.setText(text)
        self.backup_warning.setText('Автокопии: '+self.auto_backup_error if self.auto_backup_error else '')
        self.backup_warning.setVisible(bool(self.auto_backup_error) and config['enabled'])

    def save_backup_options(self,*_):
        self.ledger.set_setting('_app','auto_backup_options',{'enabled':self.auto_backup_enabled.isChecked(),
                                'interval_hours':self.auto_backup_hours.value(),'keep':self.auto_backup_keep.value()})
        self.update_backup_status()
        QTimer.singleShot(0,self.maybe_auto_backup)

    def maybe_auto_backup(self):
        if self.closing or self.maintenance is not None or self.worker is not None or self.auto_backup_worker is not None:return
        try:
            if not backup_due(self.ledger,self.database_path):return
        except (OSError,ValueError):
            self.auto_backup_error='Не удалось проверить расписание автокопий.'
            self.auto_backup_status.setText(self.auto_backup_error);return
        self.auto_backup_status.setText('Создание автокопии в фоне…')
        self.auto_backup_worker=AutoBackupWorker(self.database_path,self)
        self.auto_backup_worker.finished.connect(self.automatic_backup_finished)
        self.auto_backup_worker.start()

    def automatic_backup_finished(self):
        worker=self.auto_backup_worker
        self.auto_backup_worker=None
        if not self.closing:
            self.auto_backup_error=worker.error_message or (' '.join(worker.result.warnings) if worker.result else '')
            self.update_backup_status()
            if worker.error_message:self.sync_message.setText(worker.error_message)
            if self.after_backup_sync:
                shops,force,retry_only=self.after_backup_sync;self.after_backup_sync=None
                self.start_sync(shops,force=force,retry_only=retry_only,backup_checked=True)
            else:QTimer.singleShot(0,self.retry_pending)
        worker.deleteLater()

    def open_backup_folder(self):
        folder=backup_folder(self.database_path).parent
        try:folder.mkdir(parents=True,exist_ok=True)
        except OSError:return self.error('Нет доступа к папке резервных копий.')
        if not QDesktopServices.openUrl(QUrl.fromLocalFile(str(folder))):self.error('Не удалось открыть папку резервных копий.')

    def setup_connectivity(self):
        self.network_info=None
        if QApplication.platformName()=='offscreen':return
        try:
            from PySide6.QtNetwork import QNetworkInformation
            QNetworkInformation.loadDefaultBackend()
            self.network_info=QNetworkInformation.instance()
            if self.network_info is not None:
                self.network_info.reachabilityChanged.connect(self.network_changed)
                self.network_changed(self.network_info.reachability())
        except (AttributeError,RuntimeError):
            self.network_info=None

    def network_changed(self,reachability):
        if self.closing:return
        from PySide6.QtNetwork import QNetworkInformation
        self.network_disconnected=reachability==QNetworkInformation.Reachability.Disconnected
        if self.network_disconnected:self.awaiting_network=True
        if reachability==QNetworkInformation.Reachability.Online and self.awaiting_network:
            self.awaiting_network=False
            self.network_retry_pending=True
            self.retry_pending()
        self.update_retry_status()

    def update_retry_status(self):
        if self.closing:return
        if self.maintenance is not None:
            self.connection_status.setText('Работа с базой: автоматические повторы приостановлены');return
        if self.network_disconnected:
            self.connection_status.setText('Сеть недоступна. Последние данные сохранены; обновление продолжится автоматически.');return
        pending=plans(self.ledger,[shop['id'] for shop in self.stores])
        if pending:
            next_at=min(datetime.fromisoformat(plan['next_at']) for _shop,_source,plan in pending).astimezone(MOSCOW)
            self.connection_status.setText(f'Автоматический повтор не раньше {next_at:%d.%m.%Y %H:%M} МСК. Последние данные доступны локально.')
        else:
            self.connection_status.setText('Данные хранятся локально. Плановое обновление — каждые 30 минут.')

    def retry_pending(self):
        if self.closing or self.maintenance is not None or self.network_disconnected or self.worker is not None or self.auto_backup_worker is not None:return
        shops=[shop['id'] for shop in self.stores]
        if self.network_retry_pending:
            network_recovered(self.ledger,shops)
            self.network_retry_pending=False
        self.update_retry_status()
        if self.deferred_sync:
            self.deferred_sync=False
            self.sync_all();return
        ready=due_shops(self.ledger,shops)
        if ready:
            if self.period.currentIndex()!=6:self.change_period()
            self.start_sync(ready,retry_only=True)

    def sync_current(self):
        if self.maintenance is not None:return
        if self.period.currentIndex()!=6:self.change_period()
        self.start_sync([self.current_store] if self.current_store else [],force=True)

    def sync_all(self):
        if self.maintenance is not None:return
        if self.period.currentIndex()!=6:self.change_period()
        self.start_sync([shop['id'] for shop in self.stores])

    def start_sync(self, shops,*,force=False,retry_only=False,backup_checked=False):
        if self.maintenance is not None:return
        if self.network_disconnected:
            self.deferred_sync=True
            self.update_retry_status();return
        if not shops:
            self.sync_message.setText('Сначала добавьте магазин и сохраните токен в настройках');return
        if self.worker is None and self.auto_backup_worker is None and not backup_checked:self.maybe_auto_backup()
        if self.auto_backup_worker is not None:
            previous=self.after_backup_sync
            if previous is None or force or (not previous[1] and not retry_only):self.after_backup_sync=(list(shops),force,retry_only)
            self.sync_message.setText('Обновление продолжится после завершения автокопии');return
        if self.worker is not None:
            self.sync_message.setText('Обновление уже выполняется')
            return
        if self.start.date()>self.end.date():
            self.sync_message.setText('Дата начала периода должна быть не позже даты окончания');return
        self.worker = SyncWorker(self.database_path, shops, self,stats_period=(self.start.date().toPython(),self.end.date().toPython()),force=force,retry_only=retry_only)
        self.worker.progress.connect(self.sync_progress)
        self.worker.finished.connect(self.sync_finished)
        self.sync_button.setEnabled(False)
        self.sync_message.setText('Загрузка в фоновом режиме…')
        self.worker.start()

    def sync_progress(self, shop, source, message):
        if not self.closing and shop == self.current_store:
            self.sync_message.setText(SOURCE_LABELS.get(source, 'Токен') + ': ' + message)
            self.refresh_source_status()
            if message.startswith('Обновлено'):
                self.api_refresh_timer.start(200)

    def sync_finished(self):
        self.api_refresh_timer.stop()
        self.sync_button.setEnabled(True)
        if self.worker is not None:
            self.worker.deleteLater()
            self.worker = None
        if not self.closing:
            self.refresh()
            self.update_retry_status()
            QTimer.singleShot(0,self.maybe_auto_backup)
            QTimer.singleShot(0,self.retry_pending)

    def refresh_source_status(self):
        if not hasattr(self, 'source_table'):
            return
        if hasattr(self, 'access_view'):
            self.access_view.refresh(self.ledger,self.current_store)
        self.source_table.setSortingEnabled(False)
        self.source_table.setRowCount(len(SOURCE_LABELS))
        for row,(source,label) in enumerate(SOURCE_LABELS.items()):
            status = self.ledger.db.execute('SELECT last_success,status FROM sync_state WHERE store=? AND source=?', (self.current_store,source)).fetchone()
            last,status_text = status if status else (None,'not_started')
            message = self.ledger.setting(self.current_store,'source_message:'+source)
            localized = {'not_started':'Не загружен','running':'Загрузка…','success':'Успешно','failed':'Ошибка','cancelled':'Отменено'}.get(status_text,status_text)
            coverage = source_coverage(self.ledger,self.current_store,source,self.start.date().toPython(),self.end.date().toPython())
            for column,text in enumerate([label,last or 'Не выполнялось',message or localized,coverage.label]):
                item = QTableWidgetItem(str(text))
                if column == 3:item.setToolTip(coverage.note)
                self.source_table.setItem(row,column,item)
        self.source_table.setSortingEnabled(True)

    def table(self, columns):
        return read_only_table(columns)

    def build_settings(self):
        page = QWidget()
        layout = QVBoxLayout(page)
        form = QFormLayout()
        layout.addLayout(form)
        self.usn = QLineEdit()
        self.vat = QLineEdit()
        self.usn.setPlaceholderText('Например, 4')
        self.vat.setPlaceholderText('Например, 5')
        self.vat_included = QCheckBox('НДС включён в цену')
        self.tax_confirmed = QCheckBox('Я проверил ставки и применимость этой расчётной модели')
        form.addRow('УСН «Доходы», %', self.usn)
        form.addRow('НДС, %', self.vat)
        self.tax_date=QDateEdit(today_qdate())
        self.tax_date.setCalendarPopup(True)
        self.tax_date.setDisplayFormat('dd.MM.yyyy')
        form.addRow('Налоговые настройки действуют с',self.tax_date)
        form.addRow(self.vat_included)
        form.addRow(self.tax_confirmed)
        self.tax_hint=QLabel('Налоги расчётные, без подтверждения обязательства, вычетов и взносов.')
        self.tax_hint.setWordWrap(True)
        layout.addWidget(self.tax_hint)
        save = QPushButton('Сохранить налоговые настройки')
        save.clicked.connect(self.save_taxes)
        layout.addWidget(save)
        self.tax_history=self.table(['Действуют с','УСН, %','НДС, %','НДС включён','Подтверждены пользователем'])
        self.tax_history.setMaximumHeight(170)
        layout.addWidget(self.tax_history)
        self.tax_history.cellDoubleClicked.connect(self.select_tax_rule)
        tax_tools = QHBoxLayout()
        for label, action in [('Удалить выбранную налоговую версию',self.remove_tax_rule),
                              ('Отменить последнее изменение налогов',self.undo_tax_rule),
                              ('Журнал налогов',self.show_tax_changes)]:
            button = QPushButton(label);button.clicked.connect(action);tax_tools.addWidget(button)
        layout.addLayout(tax_tools)
        tax_notice = QLabel('Двойной щелчок заполняет поля выбранной версии. Другая дата создаёт новую версию; ошибочную дату можно удалить с возможностью отмены. Без применимой подтверждённой версии налог неизвестен, а не равен нулю.')
        tax_notice.setWordWrap(True);layout.addWidget(tax_notice)
        self.allocation_mode=QComboBox()
        self.allocation_mode.addItems(['Общие расходы остаются у магазина','Распределять общие расходы по выручке SKU'])
        self.allocation_mode.currentIndexChanged.connect(self.save_allocation_mode)
        layout.addWidget(self.allocation_mode)
        self.cost_sku = QLineEdit()
        self.cost_sku.setPlaceholderText('Точный SKU, включая размер')
        self.cost_amount = QLineEdit()
        self.cost_amount.setPlaceholderText('Например, 940 — применяется только после сохранения')
        self.cost_date = QDateEdit(today_qdate())
        self.cost_date.setCalendarPopup(True)
        self.cost_date.setDisplayFormat('dd.MM.yyyy')
        form.addRow('SKU для себестоимости', self.cost_sku)
        form.addRow('Себестоимость единицы, ₽', self.cost_amount)
        form.addRow('Действует с', self.cost_date)
        cost_save = QPushButton('Назначить себестоимость SKU')
        cost_save.clicked.connect(self.save_cost)
        layout.addWidget(cost_save)
        default_cost=QPushButton('Назначить общую себестоимость SKU без индивидуальной')
        default_cost.clicked.connect(self.save_default_cost)
        layout.addWidget(default_cost)
        self.cost_history=self.table(['SKU / общая себестоимость','Действует с','Цена закупки, ₽'])
        self.cost_history.setMaximumHeight(180)
        layout.addWidget(self.cost_history)
        cost_check = QPushButton('SKU без себестоимости за выбранный период')
        cost_check.clicked.connect(self.show_cost_coverage)
        layout.addWidget(cost_check)
        self.cost_history.cellDoubleClicked.connect(self.select_cost)
        cost_tools = QHBoxLayout()
        for label, action in [('Удалить выбранную версию цены',self.remove_cost),
                              ('Отменить последнее изменение цены',self.undo_cost),
                              ('Журнал цен',self.show_cost_changes)]:
            button = QPushButton(label);button.clicked.connect(action);cost_tools.addWidget(button)
        layout.addLayout(cost_tools)
        cost_notice = QLabel('Двойной щелчок заполняет поля для исправления. Другая дата создаёт отдельную версию; ошибочную дату можно удалить. Отмена возвращает предыдущее сохранённое состояние цены, не нулевую себестоимость.')
        cost_notice.setWordWrap(True);layout.addWidget(cost_notice)
        external = QPushButton('Добавить внешний расход')
        external.clicked.connect(self.add_external_expense)
        layout.addWidget(external)
        cancelled = QPushButton('Отменённые внешние расходы')
        cancelled.clicked.connect(self.show_cancelled_expenses)
        layout.addWidget(cancelled)
        token_save = QPushButton('Сохранить токен в Windows Credential Manager')
        token_save.clicked.connect(self.store_token)
        layout.addWidget(token_save)
        self.access_view = AccessView(SOURCE_LABELS)
        layout.addWidget(self.access_view)
        check = QPushButton('Проверить доступ и загрузить данные')
        check.clicked.connect(self.sync_current)
        layout.addWidget(check)
        hint=QLabel('Для автоматического обновления раз в 30 минут и сверки отчётов нужен персональный или сервисный токен. Базовые токены имеют более строгие лимиты WB. Подлинность и права подтверждает сервер WB.')
        hint.setWordWrap(True)
        layout.addWidget(hint)
        config=backup_options(self.ledger)
        self.auto_backup_enabled=QCheckBox('Автоматические резервные копии')
        self.auto_backup_enabled.setChecked(config['enabled'])
        layout.addWidget(self.auto_backup_enabled)
        backup_form=QFormLayout()
        self.auto_backup_hours=QSpinBox();self.auto_backup_hours.setRange(1,24);self.auto_backup_hours.setValue(config['interval_hours'])
        self.auto_backup_keep=QSpinBox();self.auto_backup_keep.setRange(3,90);self.auto_backup_keep.setValue(config['keep'])
        backup_form.addRow('Обновлять текущую копию каждые, ч',self.auto_backup_hours)
        backup_form.addRow('Хранить ежедневных копий этой базы',self.auto_backup_keep)
        layout.addLayout(backup_form)
        self.auto_backup_enabled.toggled.connect(self.save_backup_options)
        self.auto_backup_hours.valueChanged.connect(self.save_backup_options)
        self.auto_backup_keep.valueChanged.connect(self.save_backup_options)
        self.auto_backup_status=QLabel();self.auto_backup_status.setWordWrap(True);self.auto_backup_status.setTextFormat(Qt.TextFormat.PlainText)
        layout.addWidget(self.auto_backup_status)
        auto_hint=QLabel('Копии создаются между обновлениями WB, пока программа открыта. На день хранится одна версия; текущая обновляется с выбранным интервалом. Ручные копии и копии перед восстановлением не удаляются. Папка находится на том же диске: для защиты от его поломки сохраняйте копию и на другом носителе.')
        auto_hint.setWordWrap(True);layout.addWidget(auto_hint)
        open_backups=QPushButton('Открыть папку резервных копий')
        open_backups.clicked.connect(self.open_backup_folder);layout.addWidget(open_backups)
        backup = QPushButton('Создать резервную копию базы')
        backup.clicked.connect(self.backup)
        layout.addWidget(backup)
        restore=QPushButton('Восстановить базу из резервной копии')
        restore.clicked.connect(self.restore_backup)
        layout.addWidget(restore)
        backup_hint=QLabel('Копия сохраняет все магазины, историю и настройки. Перед восстановлением текущая база автоматически сохраняется отдельно. Токены остаются в Windows Credential Manager; на другом компьютере их нужно сохранить заново.')
        backup_hint.setWordWrap(True)
        layout.addWidget(backup_hint)
        dark = QCheckBox('Тёмная тема')
        self.dark_theme=dark
        dark.setChecked(self.ledger.setting('_app', 'dark', False))
        dark.toggled.connect(self.theme)
        layout.addWidget(dark)
        self.theme(dark.isChecked())
        layout.addStretch()
        scroll = QScrollArea()
        scroll.setWidgetResizable(True)
        scroll.setWidget(page)
        self.pages.addWidget(scroll)

    def photo_ready(self,url,pixmap):
        for row in range(self.products.rowCount()):
            sku_item=self.products.item(row,0)
            if sku_item and self.photo_urls.get(sku_item.text())==url:
                item=self.products.item(row,21)
                if item is None:
                    item=QTableWidgetItem()
                    self.products.setItem(row,21,item)
                item.setIcon(QIcon(pixmap))

    def show_products_mode(self):
        index=self.product_mode.currentIndex()
        self.product_tables.setCurrentIndex(index)
        self.product_note.setVisible(index==0)
        self.profit_filter.setVisible(index==0)
        self.filter_products()

    def save_products_mode(self,*_):
        if self.current_store:self.ledger.set_setting(self.current_store,'products_mode',self.product_mode.currentData())
        self.show_products_mode()

    def filter_products(self, *_):
        query = self.product_search.text().strip().casefold()
        category = self.category_filter.currentText() if hasattr(self,'category_filter') else 'Все категории'
        for row in range(self.products.rowCount()):
            item = self.products.item(row, 0)
            texts = ' '.join(self.products.item(row,c).text() for c in (0,6,7,8,9,10,11) if self.products.item(row,c))
            category_item = self.products.item(row,9) if self.products.columnCount()>9 else None
            mode=self.profit_filter.currentIndex() if hasattr(self,'profit_filter') else 0
            net_item=self.products.item(row,5)
            net_amount=net_item.value if net_item else None
            amount=net_amount if net_amount is not None else self.products.item(row,4).value if self.products.item(row,4) else None
            unknown=item is None or bool(item.data(Qt.ItemDataRole.UserRole))
            hidden_by_profit=(mode==1 and (unknown or amount is None or amount<=0)) or (mode==2 and (unknown or amount is None or amount>=0)) or (mode==3 and net_amount is not None)
            self.products.setRowHidden(row, hidden_by_profit or (bool(query) and query not in texts.casefold()) or (category != 'Все категории' and (category_item is None or category_item.text()!=category)))

        if hasattr(self,'cohort_view'):self.cohort_view.filter_rows(query,category)

    def make_sku_dialog(self, sku):
        ops = self.ledger.operations(self.current_store, self.start.date().toPython(), self.end.date().toPython())
        costs = [Cost(c['sku'], date.fromisoformat(c['date']), Decimal(c['amount']))
                 for c in self.ledger.setting(self.current_store, 'costs', [])]
        covered,reasons=period_coverage(self.ledger,self.current_store,self.start.date().toPython(),self.end.date().toPython())
        profit=product_profit_details(sku,ops,costs,load_rules(self.ledger,self.current_store),
                                     grouped=self.grouping.currentIndex()==1,coverage_confirmed=covered,
                                     method=self.ledger.setting(self.current_store,'cost_allocation','unallocated'))
        dialog = QDialog(self)
        dialog.setWindowTitle(('Расшифровка модели ' if self.grouping.currentIndex()==1 else 'Расшифровка SKU ') + sku)
        dialog.resize(900, 650)
        outer=QVBoxLayout(dialog)
        body=QWidget()
        layout=QVBoxLayout(body)
        scroll=QScrollArea()
        scroll.setWidgetResizable(True);scroll.setWidget(body)
        outer.addWidget(scroll)
        summary = QLabel(profit.explanation)
        summary.setObjectName('profitExplanation')
        summary.setWordWrap(True)
        summary.setTextFormat(Qt.TextFormat.PlainText)
        layout.addWidget(summary)
        metrics=QLabel()
        metrics.setObjectName('profitMetrics')
        metrics.setTextFormat(Qt.TextFormat.PlainText)
        metrics.setWordWrap(True)
        if profit.net_profit is not None:
            def metric(value,suffix):return '—' if value is None else f'{value:.2f} {suffix}'
            metrics.setText('Прибыль на реализованную единицу: '+metric(profit.per_unit,'₽')+
                            '\nМаржинальность: '+metric(profit.margin,'%')+'; ROI: '+metric(profit.roi,'%'))
        else:
            metrics.setText('Чистая прибыль и показатели после налогов: недостаточно данных.\n'+'\n'.join(reasons))
        layout.addWidget(metrics)
        allocation=QLabel(profit.allocation_note)
        allocation.setObjectName('profitAllocation')
        allocation.setTextFormat(Qt.TextFormat.PlainText);allocation.setWordWrap(True)
        layout.addWidget(allocation)
        ad_report=self.advertising_view.analysis
        nm=sku.split(':',1)[0]
        if ad_report is not None and nm in ad_report.model_spend:
            label=QLabel(f'Затраты продвижения по отчёту модели {nm}: {ad_report.model_spend[nm]:.2f} {ad_report.currency}. Между размерами не распределены и повторно из финансового реестра не вычитаются.')
            label.setTextFormat(Qt.TextFormat.PlainText);label.setWordWrap(True);layout.addWidget(label)
        for recommendation in profit.recommendations:
            label = QLabel(recommendation)
            label.setWordWrap(True)
            layout.addWidget(label)
        details = self.table(['Статья', 'Вклад в результат, ₽'])
        details.setObjectName('profitCategories')
        details.setSortingEnabled(False)
        details.setRowCount(len(profit.categories))
        for row, (label, amount) in enumerate(sorted(profit.categories.items())):
            details.setItem(row, 0, QTableWidgetItem(label))
            details.setItem(row, 1, NumericItem(amount))
        details.setSortingEnabled(True)
        layout.addWidget(details)
        check=QLabel(f'Сумма статей: {sum(profit.categories.values(),Decimal("0")):.2f} ₽; разница с чистой прибылью: {profit.difference:.2f} ₽'
                    if profit.difference is not None else 'Статьи отражают известный результат до налогов и общих расходов.')
        check.setObjectName('profitCheck');check.setWordWrap(True)
        layout.addWidget(check)
        history = self.table(['Дата', 'Операция', 'Сумма, ₽', 'Источник', 'ID','Исходная продажа','Дата себестоимости'])
        history.setSortingEnabled(False)
        selected = [op for op in ops if op.sku and (op.sku.split(':',1)[0]==sku if self.grouping.currentIndex()==1 else op.sku==sku)]
        history.setRowCount(len(selected))
        for row, operation in enumerate(selected):
            for column, text in enumerate([operation.occurred.isoformat(), LABELS[operation.kind], '', operation.source, operation.identifier,operation.original_sale_id or '—',operation.cost_basis_date.isoformat() if operation.cost_basis_date else '—']):
                history.setItem(row, column, NumericItem(operation.amount) if column == 2 else QTableWidgetItem(text))
        history.setSortingEnabled(True)
        layout.addWidget(history)
        buttons = QDialogButtonBox(QDialogButtonBox.StandardButton.Close)
        buttons.button(QDialogButtonBox.StandardButton.Close).setText('Закрыть')
        buttons.rejected.connect(dialog.reject)
        outer.addWidget(buttons)
        return dialog

    def open_sku(self, row, _column):
        sku = self.products.item(row, 0)
        if sku:
            try:
                self.make_sku_dialog(sku.text()).exec()
            except ValueError as exc:
                self.error(str(exc))

    def edit_external_expense(self,row,_column):
        selected = self.operations.model().operation_at(row)
        if selected is None or selected.source != 'external_expenses':
            return
        # Перечитываем только выбранную запись: она могла обновиться после отрисовки.
        current = self.ledger.operations_by_keys(self.current_store, [(selected.source, selected.identifier)])
        if current:
            self.external_expense_dialog(current[0])

    def add_external_expense(self):
        self.external_expense_dialog()

    def show_cancelled_expenses(self):
        if not self.current_store:
            return self.error('Сначала добавьте магазин')
        dialog = ExpenseArchiveDialog(self.ledger,self.current_store,self)
        dialog.changed.connect(self.refresh)
        dialog.exec()
        dialog.deleteLater()

    def external_expense_dialog(self,operation=None):
        if not self.current_store:
            return self.error('Сначала добавьте магазин')
        if operation is not None and (operation.store != self.current_store or operation.source != 'external_expenses' or operation.kind != Kind.EXTERNAL):
            return self.error('Здесь можно изменять только внешние расходы выбранного магазина.')
        dialog = QDialog(self)
        dialog.setWindowTitle('Внешний расход продавца')
        layout = QVBoxLayout(dialog)
        notice = QLabel('Только расходы вне WB: например, упаковка или аренда. Комиссии, логистику и рекламу WB вручную вводить нельзя.')
        notice.setWordWrap(True)
        layout.addWidget(notice)
        form = QFormLayout()
        layout.addLayout(form)
        description, amount, sku = QLineEdit(), QLineEdit(), QLineEdit()
        occurred = QDateEdit(today_qdate())
        occurred.setCalendarPopup(True)
        occurred.setDisplayFormat('dd.MM.yyyy')
        sku.setPlaceholderText('Необязательно; пусто — расход всего магазина')
        if operation:
            details=self.ledger.setting(self.current_store,'external_expense:'+operation.identifier,{})
            description.setText(details.get('description',''))
            amount.setText(str(-operation.amount))
            sku.setText(operation.sku or '')
            occurred.setDate(QDate(operation.occurred.year,operation.occurred.month,operation.occurred.day))
        for label, editor in [('Назначение', description), ('Сумма, ₽', amount), ('Дата', occurred), ('SKU', sku)]:
            form.addRow(label, editor)
        buttons = QDialogButtonBox(QDialogButtonBox.StandardButton.Save | QDialogButtonBox.StandardButton.Cancel)
        buttons.button(QDialogButtonBox.StandardButton.Save).setText('Сохранить')
        buttons.button(QDialogButtonBox.StandardButton.Cancel).setText('Отмена')
        buttons.rejected.connect(dialog.reject)
        def save():
            try:
                save_external_expense(self.ledger, self.current_store, occurred.date().toPython(),
                                      Decimal(amount.text().replace(',', '.')), description.text(), sku.text(),identifier=operation.identifier if operation else None)
            except (ValueError, InvalidOperation) as exc:
                self.error(str(exc) if isinstance(exc, ValueError) else 'Укажите корректную сумму')
                return
            dialog.accept()
            self.refresh()
        buttons.accepted.connect(save)
        if operation:
            cancel = QPushButton('Отменить этот внешний расход')
            cancel.setToolTip('Расход исключается из прибыли; вернуть его можно в списке отменённых внешних расходов. История сохраняется.')
            def cancel_expense():
                try:
                    cancelled = cancel_external_expense(self.ledger,self.current_store,operation.identifier,expected=operation)
                except ValueError as exc:
                    return self.error(str(exc))
                except Exception:
                    return self.error('Не удалось отменить расход. Сохранённые данные не удалены.')
                dialog.accept()
                self.refresh()
                self.sync_message.setText('Внешний расход отменён. Восстановление доступно в настройках.' if cancelled else 'Расход уже отсутствует в текущем реестре.')
            cancel.clicked.connect(cancel_expense)
            layout.addWidget(cancel)
        layout.addWidget(buttons)
        dialog.exec()

    def theme(self, dark):
        self.ledger.set_setting('_app', 'dark', dark)
        app = QApplication.instance()
        if app.style().objectName().casefold() != 'fusion':
            app.setStyle('Fusion')
        self.setStyleSheet('QWidget { background: #20242c; color: #ededed; } QLineEdit,QTableWidget,QTableView,QComboBox,QListWidget { background: #2c323c; } QPushButton { padding: 7px; background: #3e4a60; }' if dark else 'QPushButton { padding: 7px; }')

    def add_store(self):
        name, accepted = QInputDialog.getText(self, 'Новый магазин', 'Название магазина')
        if not accepted or not name.strip():
            return
        item = {'id': str(uuid.uuid4()), 'name': name.strip()}
        self.stores.append(item)
        self.ledger.set_setting('_app', 'stores', self.stores)
        self.store.addItem(item['name'], item['id'])
        self.store.setCurrentIndex(self.store.count() - 1)

    def change_store(self, *_):
        self.current_store = self.store.currentData() or ''
        self.product_mode.blockSignals(True)
        mode_index=self.product_mode.findData(self.ledger.setting(self.current_store,'products_mode','profit'))
        self.product_mode.setCurrentIndex(max(0,mode_index))
        self.product_mode.blockSignals(False)
        self.show_products_mode()
        self.chart_mode.blockSignals(True)
        chart_index=self.chart_mode.findData(self.ledger.setting(self.current_store,'daily_chart_mode','net'))
        self.chart_mode.setCurrentIndex(max(0,chart_index))
        self.chart_mode.blockSignals(False)
        self.ledger.set_setting('_app', 'selected_store', self.current_store)
        rules=load_rules(self.ledger,self.current_store)
        active=rule_at(rules,business_today())
        if active:
            tax={'usn':str(active.settings.usn_percent),'vat':str(active.settings.vat_percent),'included':active.settings.vat_included,'confirmed':active.settings.confirmed_by_user}
        else:
            tax=self.ledger.setting(self.current_store,'taxes',{}) if not rules else {}
        self.usn.setText(tax.get('usn',''))
        self.vat.setText(tax.get('vat',''))
        self.vat_included.setChecked(tax.get('included',True))
        self.tax_confirmed.setChecked(tax.get('confirmed',False))
        self.tax_date.setDate(today_qdate())
        self.allocation_mode.blockSignals(True)
        self.allocation_mode.setCurrentIndex(1 if self.ledger.setting(self.current_store,'cost_allocation','unallocated')=='revenue' else 0)
        self.allocation_mode.blockSignals(False)
        self.tax_hint.setText('Сохранены ставки без даты: назначьте дату действия, чтобы применять их к истории.' if tax and not rules else 'Ставки применяются по дате начала действия. Будущая ставка не меняет прошлые периоды. Налоги остаются расчётными.')
        period = self.ledger.setting(self.current_store, 'period', {'index': 3})
        self.period.blockSignals(True)
        self.period.setCurrentIndex(period['index'])
        self.period.blockSignals(False)
        if period['index'] == 6 and 'start' in period:
            self.start.blockSignals(True)
            self.end.blockSignals(True)
            self.start.setDate(QDate.fromString(period['start'], 'yyyy-MM-dd'))
            self.end.setDate(QDate.fromString(period['end'], 'yyyy-MM-dd'))
            self.start.blockSignals(False)
            self.end.blockSignals(False)
            self.custom_period()
        else:
            self.change_period()

    def change_period(self, *_):
        today = business_today()
        index = self.period.currentIndex()
        first = today.replace(day=1)
        ranges = [(today, today), (today-timedelta(days=1), today-timedelta(days=1)),
                  (today-timedelta(days=6), today), (today-timedelta(days=29), today),
                  (first, today), ((first-timedelta(days=1)).replace(day=1), first-timedelta(days=1))]
        self.start.setEnabled(index == 6)
        self.end.setEnabled(index == 6)
        if index < 6:
            start, end = ranges[index]
            for editor, value in [(self.start, start), (self.end, end)]:
                editor.blockSignals(True)
                editor.setDate(QDate(value.year, value.month, value.day))
                editor.blockSignals(False)
        self.custom_period()

    def custom_period(self, *_):
        self.ledger.set_setting(self.current_store, 'period', {
            'index': self.period.currentIndex(), 'start': self.start.date().toString('yyyy-MM-dd'),
            'end': self.end.date().toString('yyyy-MM-dd')})
        self.refresh()

    def save_chart_mode(self,*_):
        if self.current_store:self.ledger.set_setting(self.current_store,'daily_chart_mode',self.chart_mode.currentData())
        self.refresh()

    def update_daily_chart(self,ops,costs,rules,start,end,covered):
        self.daily_chart.set_operations(ops,costs,rules,start=start,end=end,coverage_confirmed=covered,mode=self.chart_mode.currentData())
        report=self.daily_chart.report
        if report.period_net is not None:
            self.daily_note.setText(f'Сумма чистой прибыли по дням: {report.daily_sum:.2f} ₽; итог периода: {report.period_net:.2f} ₽; разница: {report.difference:.2f} ₽. '
                                   'Расчётные УСН и НДС распределены по выручке внутри версии ставок и квартала выбранного периода, с сохранением копеек. '
                                   'Это не ежедневное налоговое обязательство. Общие расходы остаются на дате финансовой операции. '
                                   'В сверенном периоде дни без операций имеют нулевой результат. '+ ' '.join(report.reasons))
        else:
            self.daily_note.setText('Чистая прибыль по дням недоступна. '+ ' '.join(report.reasons)+
                                   ' Неполные дни не заменяются нулём и не соединяются линией в графике загруженных проводок.')
        self.daily_table.setSortingEnabled(False)
        self.daily_table.setRowCount(len(report.rows))
        for index,row in enumerate(report.rows):
            values=[row.day.isoformat(),row.revenue,row.expenses_contribution,row.cost,row.before_tax,row.usn,row.vat,row.net,
                    '; '.join(row.issues) or ('Расчёт сверён с итогом периода' if row.net is not None else 'Загруженные проводки; чистая прибыль не подтверждена')]
            for column,value in enumerate(values):
                self.daily_table.setItem(index,column,NumericItem(value) if isinstance(value,Decimal) or value is None else QTableWidgetItem(value))
        self.daily_table.setSortingEnabled(True)

    def save_allocation_mode(self,*_):
        if self.current_store:
            self.ledger.set_setting(self.current_store,'cost_allocation','revenue' if self.allocation_mode.currentIndex()==1 else 'unallocated')
            self.refresh()

    def save_taxes(self):
        if not self.current_store:
            return self.error('Сначала добавьте магазин')
        try:
            tax = TaxSettings(Decimal(self.usn.text().replace(',', '.')), Decimal(self.vat.text().replace(',', '.')),
                              self.vat_included.isChecked(), self.tax_confirmed.isChecked())
            save_rule(self.ledger,self.current_store,TaxRule(self.tax_date.date().toPython(),tax))
            self.refresh()
        except (ValueError, InvalidOperation):
            self.error('Укажите конечные числовые ставки от 0 до 100')

    def select_tax_rule(self,row,_column=0):
        item = self.tax_history.item(row,0)
        if item is None:return
        self.fill_tax_rule(item.data(Qt.ItemDataRole.UserRole))

    def fill_tax_rule(self,rule):
        self.usn.setText(str(rule.settings.usn_percent) if rule else '')
        self.vat.setText(str(rule.settings.vat_percent) if rule else '')
        self.vat_included.setChecked(rule.settings.vat_included if rule else True)
        self.tax_confirmed.setChecked(rule.settings.confirmed_by_user if rule else False)
        effective = rule.effective if rule else business_today()
        self.tax_date.setDate(QDate(effective.year,effective.month,effective.day))

    def remove_tax_rule(self):
        item = self.tax_history.item(self.tax_history.currentRow(),0)
        if item is None:return self.error('Выберите налоговую версию в таблице.')
        rule = item.data(Qt.ItemDataRole.UserRole)
        try:
            change_rule(self.ledger,self.current_store,rule.effective,None,expected=rule)
            self.fill_tax_rule(rule_at(load_rules(self.ledger,self.current_store),business_today()))
            self.refresh()
            self.sync_message.setText('Налоговая версия удалена. Оставшиеся версии применяются по датам; без подходящей подтверждённой версии расчёт неполный. Удаление можно отменить.')
        except (ValueError,ArithmeticError) as exc:
            self.error(str(exc))

    def undo_tax_rule(self):
        if not self.current_store:return self.error('Сначала добавьте магазин')
        try:
            changed = undo_tax_change(self.ledger,self.current_store)
            self.fill_tax_rule(rule_at(load_rules(self.ledger,self.current_store),business_today()))
            self.refresh()
            self.sync_message.setText('Изменение налоговых настроек отменено; показатели пересчитаны.' if changed else 'Нет сохранённых налоговых изменений для отмены.')
        except (ValueError,ArithmeticError) as exc:
            self.error(str(exc))

    def show_tax_changes(self):
        if not self.current_store:return self.error('Сначала добавьте магазин')
        try:
            dialog = TaxChangesDialog(self.ledger,self.current_store,self)
        except (ValueError,TypeError,KeyError,ArithmeticError):
            return self.error('Журнал налогов не прошёл проверку. Настройки не изменены.')
        dialog.exec();dialog.deleteLater()

    def save_default_cost(self):
        if not self.current_store:
            return self.error('Сначала добавьте магазин')
        try:
            cost=Cost('*',self.cost_date.date().toPython(),Decimal(self.cost_amount.text().replace(',','.')))
            change_cost(self.ledger,self.current_store,cost.sku,cost.effective,cost.per_unit)
            self.refresh()
        except (ValueError,InvalidOperation):
            self.error('Укажите неотрицательную конечную себестоимость')

    def save_cost(self):
        if self.cost_sku.text().strip()=='*':
            return self.error('Для общей себестоимости используйте отдельную кнопку назначения')
        if not self.current_store or not self.cost_sku.text().strip():
            return self.error('Укажите магазин и SKU')
        try:
            cost = Cost(self.cost_sku.text().strip(), self.cost_date.date().toPython(), Decimal(self.cost_amount.text().replace(',', '.')))
            change_cost(self.ledger,self.current_store,cost.sku,cost.effective,cost.per_unit)
            self.refresh()
        except (ValueError, InvalidOperation):
            self.error('Себестоимость должна быть конечным неотрицательным числом')

    def make_cost_coverage_dialog(self):
        start,end = self.start.date().toPython(),self.end.date().toPython()
        if start > end:raise ValueError('Дата начала позже даты окончания')
        costs = [Cost(item['sku'],date.fromisoformat(item['date']),Decimal(item['amount']))
                 for item in self.ledger.setting(self.current_store,'costs',[])]
        dialog = CostCoverageDialog(self.ledger.operations(self.current_store,start,end),costs,
                                    product_metadata(self.ledger,self.current_store),self)
        dialog.selected_cost.connect(self.prefill_missing_cost)
        return dialog

    def show_cost_coverage(self):
        if not self.current_store:return self.error('Сначала добавьте магазин')
        try:
            dialog = self.make_cost_coverage_dialog()
        except (ValueError,TypeError,KeyError,ArithmeticError):
            return self.error('Не удалось проверить себестоимость: проверьте период и сохранённые цены.')
        dialog.exec();dialog.deleteLater()

    def prefill_missing_cost(self,sku,effective):
        self.cost_sku.setText(sku)
        self.cost_date.setDate(QDate(effective.year,effective.month,effective.day))
        self.cost_amount.clear()
        self.nav.setCurrentRow(7)
        self.pages.widget(7).ensureWidgetVisible(self.cost_amount)
        self.cost_amount.setFocus()
        self.sync_message.setText('SKU и первая дата без цены выбраны. Введите закупочную цену и сохраните её; цена не назначена автоматически.')

    def select_cost(self,row,_column=0):
        item = self.cost_history.item(row,0)
        if item is None:return
        sku, effective = item.data(Qt.ItemDataRole.UserRole)
        self.cost_sku.setText(sku)
        self.cost_date.setDate(QDate.fromString(effective,'yyyy-MM-dd'))
        self.cost_amount.setText(str(self.cost_history.item(row,2).value))

    def remove_cost(self):
        item = self.cost_history.item(self.cost_history.currentRow(),0)
        if item is None:return self.error('Выберите версию себестоимости в таблице.')
        sku, effective = item.data(Qt.ItemDataRole.UserRole)
        try:
            change_cost(self.ledger,self.current_store,sku,date.fromisoformat(effective),None)
            self.refresh()
            self.sync_message.setText('Версия себестоимости удалена. Действует предыдущая или назначенная общая цена; при отсутствии цены расчёт неполный. Удаление можно отменить.')
        except (ValueError,ArithmeticError) as exc:
            self.error(str(exc))

    def undo_cost(self):
        if not self.current_store:return self.error('Сначала добавьте магазин')
        try:
            changed = undo_cost_change(self.ledger,self.current_store)
            self.refresh()
            self.sync_message.setText('Последнее изменение себестоимости отменено; показатели пересчитаны.' if changed else 'Нет сохранённых изменений себестоимости для отмены.')
        except (ValueError,ArithmeticError) as exc:
            self.error(str(exc))

    def show_cost_changes(self):
        if not self.current_store:return self.error('Сначала добавьте магазин')
        try:
            dialog = CostChangesDialog(self.ledger,self.current_store,self)
        except (ValueError,TypeError,KeyError,ArithmeticError):
            return self.error('Журнал себестоимости не прошёл проверку. Текущие цены не изменены.')
        dialog.exec();dialog.deleteLater()

    def store_token(self):
        if not self.current_store:
            return self.error('Сначала добавьте магазин')
        if self.worker is not None or self.maintenance is not None or self.auto_backup_worker is not None:
            return self.error('Дождитесь завершения синхронизации или работы с базой перед заменой токена.')
        token, accepted = QInputDialog.getText(self, 'API Wildberries', 'Токен сохраняется только в Windows Credential Manager', QLineEdit.EchoMode.Password)
        if not accepted:
            return
        token = token.strip()
        try:
            claims = inspect_token(token)
        except ValueError as exc:
            return self.error(str(exc))
        if claims.expired or claims.sandbox:
            return self.error('Токен истёк или относится к песочнице. Нужен действующий рабочий токен.')
        try:
            save_token(self.current_store, token)
        except Exception:
            return self.error('Не удалось сохранить токен. Нужен доступный Windows Credential Manager. Секрет не записан в базу.')
        try:
            save_access(self.ledger,self.current_store,claims)
            clear_shop(self.ledger,self.current_store)
            self.update_retry_status()
            self.access_view.refresh(self.ledger,self.current_store)
            rights = ', '.join(label for category,(_,label) in CATEGORIES.items() if claims.permits(category))
            self.sync_message.setText('Токен сохранён. Заявленные категории: '+(rights or 'не найдены')+'. Доступ подтвердит ответ API.')
        except Exception:
            self.error('Токен сохранён в защищённом хранилище, но не удалось обновить сводку прав и настройки повторов. Проверьте доступ к базе; сохранённые данные не удалены.')

    def backup(self):
        if self.maintenance is not None or self.worker is not None or self.auto_backup_worker is not None:
            return self.error('Дождитесь завершения синхронизации или работы с базой.')
        filename, _ = QFileDialog.getSaveFileName(self, 'Резервная копия', 'wb-profit-backup.sqlite', 'SQLite (*.sqlite)')
        if filename:self.start_maintenance(filename)

    def restore_backup(self):
        if self.maintenance is not None or self.worker is not None or self.auto_backup_worker is not None:
            return self.error('Дождитесь завершения синхронизации или работы с базой.')
        filename,_=QFileDialog.getOpenFileName(self,'Восстановить базу',str(backup_folder(self.database_path).parent),'SQLite (*.sqlite *.db);;Все файлы (*)')
        if not filename:return
        confirmation=QMessageBox(self)
        confirmation.setWindowTitle('Восстановить базу')
        confirmation.setTextFormat(Qt.TextFormat.PlainText)
        confirmation.setText('Все магазины, операции и настройки будут заменены данными выбранной копии. '
                             'Перед заменой программа проверит файл и сохранит текущую базу в папку backups. '
                             'Токены Windows Credential Manager не изменятся.\n\nФайл: '+filename)
        confirmation.setStandardButtons(QMessageBox.StandardButton.Ok|QMessageBox.StandardButton.Cancel)
        confirmation.button(QMessageBox.StandardButton.Ok).setText('Восстановить')
        confirmation.button(QMessageBox.StandardButton.Cancel).setText('Отмена')
        confirmation.setDefaultButton(QMessageBox.StandardButton.Cancel)
        if confirmation.exec()==QMessageBox.StandardButton.Ok:self.start_maintenance(filename,restore=True)

    def start_maintenance(self,filename,*,restore=False):
        if self.maintenance is not None or self.worker is not None or self.auto_backup_worker is not None:
            self.error('Дождитесь завершения синхронизации или работы с базой.')
            return False
        self.api_refresh_timer.stop()
        self.sync_timer.stop()
        self.retry_timer.stop()
        self.auto_backup_timer.stop()
        self.root.setEnabled(False)
        self.sync_message.setText('Проверка копии и восстановление базы…' if restore else 'Создание резервной копии…')
        self.maintenance=BackupWorker(self.database_path,filename,restore=restore,parent=self)
        self.maintenance.finished.connect(self.maintenance_finished)
        self.maintenance.start()
        return True

    def maintenance_finished(self):
        worker=self.maintenance
        self.maintenance=None
        self.root.setEnabled(True)
        self.sync_timer.start()
        self.retry_timer.start()
        self.auto_backup_timer.start()
        if worker.error_message:
            self.sync_message.setText(worker.error_message)
            self.error(worker.error_message)
        elif worker.restore:
            self.reload_saved_state()
            self.sync_message.setText('База восстановлена. Копия прежней базы: '+str(worker.result))
        else:
            self.sync_message.setText('Резервная копия создана: '+str(worker.result))
        self.update_retry_status()
        self.update_backup_status()
        worker.deleteLater()

    def reload_saved_state(self):
        self.stores=self.ledger.setting('_app','stores',[])
        selected=self.ledger.setting('_app','selected_store','')
        self.store.blockSignals(True)
        self.store.clear()
        for item in self.stores:self.store.addItem(item['name'],item['id'])
        index=self.store.findData(selected)
        if index>=0:self.store.setCurrentIndex(index)
        self.store.blockSignals(False)
        self.dark_theme.blockSignals(True)
        self.dark_theme.setChecked(self.ledger.setting('_app','dark',False))
        self.dark_theme.blockSignals(False)
        self.theme(self.dark_theme.isChecked())
        self.product_search.clear()
        self.change_store()

    def error(self, message):
        QMessageBox.warning(self, 'WB Profit Desktop', message)

    def refresh_api_views(self,start,end):
        self.advertising_view.refresh(self.ledger,self.current_store,start,end)
        self.inventory_view.refresh(self.ledger,self.current_store,business_today(),start,end)
        self.cohort_view.refresh(self.ledger,self.current_store,start,end,grouped=self.grouping.currentIndex()==1)
        metadata=product_metadata(self.ledger,self.current_store)
        categories=sorted({card.get('subjectName','') for card in metadata.values() if card.get('subjectName')} | self.cohort_view.categories,key=lambda x:x.casefold().replace('ё','е'))
        selected=self.category_filter.currentText()
        self.category_filter.blockSignals(True)
        self.category_filter.clear()
        self.category_filter.addItems(['Все категории',*categories])
        if selected in categories:self.category_filter.setCurrentText(selected)
        self.category_filter.blockSignals(False)
        return metadata

    def refresh(self,*_):
        if not hasattr(self, 'summary'):
            return
        self.overview_view.clear()
        self.operations.set_operations([])
        self.operation_review.clear()
        self.revision_history.clear()
        self.return_review.clear()
        self.cohort_view.clear()
        self.refresh_source_status()
        self.tax_breakdown.setRowCount(0)
        self.tax_note.setText('Налоговая оценка за выбранный период ещё не рассчитана')
        tax_rules=load_rules(self.ledger,self.current_store)
        self.tax_hint.setText('Сохранены ставки без даты: назначьте дату действия, чтобы применять их к истории.' if not tax_rules and self.ledger.setting(self.current_store,'taxes') else 'Ставки применяются по дате начала действия. Будущая ставка не меняет прошлые периоды. Налоги остаются расчётными.')
        self.tax_history.setSortingEnabled(False)
        self.tax_history.setRowCount(len(tax_rules))
        for row,rule in enumerate(tax_rules):
            values=[rule.effective.isoformat(),rule.settings.usn_percent,rule.settings.vat_percent,'Да' if rule.settings.vat_included else 'Нет','Да' if rule.settings.confirmed_by_user else 'Нет']
            for column,value in enumerate(values):
                item = NumericItem(value) if isinstance(value,Decimal) else QTableWidgetItem(value)
                item.setData(Qt.ItemDataRole.UserRole,rule)
                self.tax_history.setItem(row,column,item)
        self.tax_history.setSortingEnabled(True)
        self.comparison.setText('Сравнение периодов: недостаточно подтверждённых данных')
        history=sorted(self.ledger.setting(self.current_store,'costs',[]),key=lambda c:(c['sku'],c['date']))
        self.cost_history.setSortingEnabled(False)
        self.cost_history.setRowCount(len(history))
        for row,c in enumerate(history):
            item=QTableWidgetItem('Общая (назначена)' if c['sku']=='*' else c['sku'])
            item.setData(Qt.ItemDataRole.UserRole,(c['sku'],c['date']))
            self.cost_history.setItem(row,0,item)
            self.cost_history.setItem(row,1,QTableWidgetItem(c['date']))
            self.cost_history.setItem(row,2,NumericItem(Decimal(c['amount'])))
        self.cost_history.setSortingEnabled(True)
        start, end = self.start.date().toPython(), self.end.date().toPython()
        self.economics.set_context([], [], None)
        self.daily_chart.set_operations([],[])
        self.daily_table.setRowCount(0)
        self.daily_note.setText('Нет данных для дневного расчёта')
        if start > end:
            self.advertising_view.invalid_period()
            self.inventory_view.invalid_period()
            self.cohort_view.clear('Неверный период заказов: дата начала позже даты окончания')
            self.reconciliation.setText('Неверный период: дата начала позже даты окончания')
            self.overview_view.clear('Дата начала должна быть не позже даты окончания')
            self.summary.setText('Дата начала должна быть не позже даты окончания')
            self.products.setRowCount(0)
            self.operations.set_operations([])
            return
        metadata = self.refresh_api_views(start,end)
        ops = self.ledger.operations(self.current_store, start, end)
        self.operations.set_operations(ops)
        costs=[Cost(c['sku'],date.fromisoformat(c['date']),Decimal(c['amount'])) for c in history]
        financial_complete=not financial_period_reasons(self.ledger,self.current_store,start,end)
        self.revision_history.set_context(self.ledger,self.current_store,start,end)
        try:
            self.operation_review.set_context(self.ledger,self.current_store,ops,financial_complete=financial_complete)
        except ValueError as exc:
            self.operation_review.clear('Проверка операций остановлена: '+str(exc))
        try:
            self.return_review.set_context(self.ledger,self.current_store,ops,costs,tax_rules,financial_complete=financial_complete)
        except ValueError as exc:
            self.return_review.clear('Проверка возвратов остановлена: '+str(exc))
        for table in [self.products]:
            table.setSortingEnabled(False)
            table.setRowCount(0)
        if not ops:
            self.summary.setText('Нет подтверждённых финансовых операций за выбранный период.\nВыручка: —\nЧистая прибыль: —\nПоследняя синхронизация: не выполнялась')
            self.product_note.setText('Нет финансовых операций. Загруженные заказы доступны в режиме «Заказы и выкуп» и не подменяют финансовую прибыль.')
            self.reconciliation.setText('Сверка не выполнялась: данные финансовых отчётов и контрольные суммы WB не загружены.')
        else:
            current_tax=rule_at(tax_rules,business_today())
            taxes=current_tax.settings if current_tax else None
            covered,coverage_reasons=period_coverage(self.ledger,self.current_store,start,end)
            tariff_context=forecast_rates(self.ledger,self.current_store,ops,metadata)
            self.economics.set_context(ops, costs, taxes,coverage_confirmed=covered,
                                      fbw_rates=tariff_context.by_sku,tariff_note=tariff_context.note,
                                      tariff_valid_until=tariff_context.valid_until)
            try:
                self.update_daily_chart(ops,costs,tax_rules,start,end,covered)
                self.overview_view.set_operations(ops,costs,tax_rules,
                                                 financial_complete=financial_complete,
                                                 full_complete=covered)
                result,tax_parts = calculate_with_history(ops,costs,tax_rules)
                self.tax_breakdown.setSortingEnabled(False)
                self.tax_breakdown.setRowCount(len(tax_parts))
                for row,part in enumerate(tax_parts):
                    values=[part.effective.isoformat(),str(part.year),str(part.quarter),part.revenue,part.usn,part.vat]
                    for column,value in enumerate(values):
                        self.tax_breakdown.setItem(row,column,NumericItem(value) if isinstance(value,Decimal) else QTableWidgetItem(value))
                self.tax_breakdown.setSortingEnabled(True)
                self.tax_note.setText('Известные части налогового расчёта: итог не определён' if result.usn is None else 'Расчётная оценка по датам финансовых операций WB за выбранный период; не декларация и не подтверждённое обязательство')
                comparison=compare_metrics(self.ledger,self.current_store,start,end,costs,tax_rules)
                self.overview_view.set_comparison(comparison)
                before_comparison=comparison.metrics['before_tax']
                if before_comparison.change is not None:
                    relative=f' ({before_comparison.relative_percent:+.2f}%)' if before_comparison.relative_percent is not None else ''
                    self.comparison.setText(f'Изменение результата до налогов: {before_comparison.change:+.2f} ₽{relative}. Предыдущий период: {comparison.previous_start:%d.%m.%Y}–{comparison.previous_end:%d.%m.%Y}')
                elif before_comparison.unavailable_reason:
                    self.comparison.setText('Сравнение периодов недоступно: '+before_comparison.unavailable_reason)
                has_wb = any(op.source != 'external_expenses' for op in ops)
                net = f'{result.net_profit:.2f} ₽ (расчёт)' if result.net_profit is not None and has_wb and covered else 'недостаточно данных'
                tax_text=('Расчётный УСН: —\nРасчётный НДС: —\n' if result.usn is None else f'Расчётный УСН: {result.usn:.2f} ₽\nРасчётный НДС: {result.vat:.2f} ₽\n')
                self.summary.setText(tax_text+f'Выручка: {result.revenue:.2f} ₽\nРезультат до налога: {result.result_before_tax:.2f} ₽\nЧистая прибыль: {net}\n' + '\n'.join([*result.tax_limitations,*coverage_reasons]))
                audits=self.ledger.setting(self.current_store,'report_audits',[])
                audit_text='\n'.join('Отчёт '+a['report_id']+': '+('Сверен' if a['verified'] else 'Расхождение / не классифицирован')+'; разница '+str(a['difference'])+' ₽' for a in audits)
                period_ids={op.identifier.rsplit(':',1)[0] for op in ops if op.source=='wb_finance'}
                return_issues={key:message for key,message in self.ledger.setting(self.current_store,'return_link_issues',{}).items() if key in period_ids}
                return_text='\nВозвраты без однозначной связи: '+str(len(return_issues))
                if return_issues:return_text+='\n'+'\n'.join(key+': '+message for key,message in list(return_issues.items())[:10])
                distribution=distribute_profit(ops,costs,tax_rules,coverage_confirmed=covered,method=self.ledger.setting(self.current_store,'cost_allocation','unallocated'))
                distribution_text=''
                if distribution is not None:
                    method_text='по выручке SKU' if distribution.method=='revenue' else 'остаются у магазина'
                    distribution_text=f'\nОбщие расходы: {method_text}\nОставшийся непривязанный результат после налогов: {distribution.unallocated_net:.2f} ₽\nРазница суммы SKU и результата магазина: {distribution.difference:.2f} ₽'
                    if distribution.warnings:distribution_text+='\n'+'\n'.join(distribution.warnings)
                    self.product_note.setText('Цвет отражает прибыль после налогов. Общие расходы '+method_text+'. Налоги распределены по выручке внутри каждой версии ставок; жёлтый — маржа ниже 10%.')
                else:
                    self.product_note.setText('Чистая прибыль пока не подтверждена. Цвет показывает известный результат до налогов; жёлтый — маржа ниже 10%.')
                self.reconciliation.setText(f'Операции без SKU в реестре: {result.unallocated:.2f} ₽\nНеизвестных операций: {len(result.unknown_operations)}\nSKU без себестоимости: {len(result.missing_costs)}\n'+(audit_text or self.ledger.setting(self.current_store,'audit_message','Независимые контрольные суммы ещё не загружены.'))+return_text+distribution_text)
                from collections import defaultdict
                grouped_ops=defaultdict(list)
                for op in ops:
                    if op.sku:grouped_ops[op.sku.split(':',1)[0] if self.grouping.currentIndex()==1 else op.sku].append(op)
                grouped_costs=defaultdict(list)
                default_costs=[cost for cost in costs if cost.sku=='*']
                for cost in costs:grouped_costs[cost.sku.split(':',1)[0] if self.grouping.currentIndex()==1 else cost.sku].append(cost)
                skus = sorted(grouped_ops)
                self.products.setRowCount(len(skus))
                self.photo_urls={}
                for row, sku in enumerate(skus):
                    sku_ops=grouped_ops[sku]
                    sku_costs=[*grouped_costs[sku],*default_costs]
                    item_result = calculate(sku_ops, sku_costs, None)
                    self.products.setItem(row, 0, QTableWidgetItem(sku))
                    analysis = analyse_group(sku,sku_ops,sku_costs) if self.grouping.currentIndex()==1 else analyse_sku(sku, sku_ops, sku_costs)
                    self.products.item(row,0).setData(Qt.ItemDataRole.UserRole,analysis.incomplete)
                    keys={op.sku for op in sku_ops if op.sku is not None}
                    product_net=sum((distribution.net_by_sku.get(key,Decimal('0')) for key in keys),Decimal('0')) if distribution is not None else None
                    product_tax=sum((distribution.taxes_by_sku.get(key,Decimal('0')) for key in keys),Decimal('0')) if distribution is not None else None
                    product_common=-sum((distribution.common_by_sku.get(key,Decimal('0')) for key in keys),Decimal('0')) if distribution is not None else None
                    display_margin=product_net/item_result.revenue*100 if product_net is not None and item_result.revenue>0 else analysis.margin_before_tax
                    display_result=product_net if product_net is not None else item_result.result_before_tax
                    for column, value in enumerate([item_result.revenue, item_result.wb_and_external, item_result.cost,
                                                   item_result.result_before_tax, product_net], 1):
                        item = NumericItem(value)
                        color = '#777777' if analysis.incomplete or value is None else ('#c0392b' if display_result < 0 else '#a66c00' if display_margin is not None and display_margin < 10 else '#218c4b')
                        item.setForeground(QColor(color))
                        self.products.setItem(row, column, item)
                    card=metadata.get(sku.split(':',1)[0],{})
                    size=next((size for size in card.get('sizes',[]) if sku.split(':',1)[-1] in [str(barcode) for barcode in size.get('skus',[])]),{})
                    extra=[card.get('vendorCode','—'),sku.split(':',1)[0],card.get('title','—'),card.get('subjectName','—'),size.get('techSize','—'),sku.split(':',1)[-1],
                           Decimal(analysis.sold),Decimal(analysis.returned),-sum((op.amount for op in sku_ops if op.kind==Kind.COMMISSION),Decimal('0')),
                           -sum((op.amount for op in sku_ops if op.kind==Kind.LOGISTICS),Decimal('0')),
                           -sum((op.amount for op in sku_ops if op.kind==Kind.REVERSE_LOGISTICS),Decimal('0')),
                           -sum((op.amount for op in sku_ops if op.kind==Kind.STORAGE),Decimal('0')),
                           product_net/(analysis.sold-analysis.returned) if product_net is not None and analysis.sold>analysis.returned else None,
                           product_net/item_result.revenue*100 if product_net is not None and item_result.revenue>0 else None,
                           product_net/item_result.cost*100 if product_net is not None and item_result.cost>0 else None]
                    ad_report=self.advertising_view.analysis
                    model_ad_cost=ad_report.model_spend.get(sku) if self.grouping.currentIndex()==1 and ad_report is not None and ad_report.currency=='RUB' else None
                    self.products.setItem(row,22,NumericItem(model_ad_cost))
                    self.products.setItem(row,23,NumericItem(product_tax))
                    self.products.setItem(row,24,NumericItem(product_common))
                    cohort=self.cohort_view.rows_by_sku.get(sku)
                    complete=self.cohort_view.analysis is not None and self.cohort_view.analysis.history_complete
                    order_count=Decimal(cohort.orders) if cohort else Decimal('0') if complete else None
                    buyout=cohort.buyout_percent if cohort else None
                    unresolved=Decimal(cohort.pending+cohort.ambiguous) if cohort else Decimal('0') if complete else None
                    for column,value in [(25,order_count),(26,buyout),(27,unresolved)]:
                        self.products.setItem(row,column,NumericItem(value))
                    if self.grouping.currentIndex()==1:
                        extra[4]='Все размеры'
                        extra[5]='Все баркоды'
                    photos=card.get('photos',[])
                    if photos and isinstance(photos[0],dict):
                        url=photos[0].get('square') or photos[0].get('tm')
                        if url:
                            self.photo_urls[sku]=url
                            self.images.request(url)
                    for column,value in enumerate(extra,6):
                        self.products.setItem(row,column,NumericItem(value) if isinstance(value,Decimal) or value is None else QTableWidgetItem(str(value)))
            except ValueError as exc:
                self.summary.setText(f'Расчёт остановлен: {exc}')
        for table in [self.products]:
            table.setSortingEnabled(True)
        self.filter_products()

    def closeEvent(self, event):
        if self.maintenance is not None:
            self.sync_message.setText('Дождитесь завершения работы с базой, затем закройте окно.')
            event.ignore()
            return
        self.closing = True
        self.api_refresh_timer.stop()
        self.sync_timer.stop()
        self.retry_timer.stop()
        self.auto_backup_timer.stop()
        active=[worker for worker in (self.worker,self.auto_backup_worker) if worker is not None and worker.isRunning()]
        for worker in active:worker.cancel()
        for worker in active:
            if not worker.wait(3000):
                self.sync_message.setText('Завершение фоновой работы… Закройте окно повторно через несколько секунд.')
                self.closing = False
                self.sync_timer.start();self.retry_timer.start();self.auto_backup_timer.start()
                event.ignore()
                return
        self.ledger.close()
        super().closeEvent(event)


def run_desktop(app,folder,*,smoke=False):
    app.setQuitOnLastWindowClosed(False)
    try:
        session=SessionLock(folder)
        if not session.acquire():
            QMessageBox.information(None,'WB Profit Desktop','Программа уже открыта. Используйте существующее окно.')
            return 2
    except (OSError,StartupError):
        QMessageBox.warning(None,'WB Profit Desktop','Нет доступа к папке данных. Проверьте права доступа и свободное место.')
        return 1
    try:
        failure=None
        while True:
            startup=StartupDialog(folder,failure)
            accepted=startup.exec()==QDialog.DialogCode.Accepted
            database=startup.database
            startup.deleteLater()
            if not accepted:return 1
            try:
                window=Window(database)
                break
            except (sqlite3.Error,ValueError,KeyError,TypeError):
                failure='Не удалось загрузить базу или сохранённые настройки. Исходные файлы не удаляйте; выберите резервную копию.'
        window.show()
        app.setQuitOnLastWindowClosed(True)
        if smoke:QTimer.singleShot(100,window.close)
        return app.exec()
    finally:
        session.release()


def main():
    QLocale.setDefault(QLocale(QLocale.Language.Russian,QLocale.Country.Russia))
    app = QApplication(sys.argv)
    translator=QTranslator(app)
    if translator.load('qtbase_ru',QLibraryInfo.path(QLibraryInfo.LibraryPath.TranslationsPath)):
        app.installTranslator(translator)
    app.setApplicationName('WB Profit Desktop')
    app.setOrganizationName('WB Profit Desktop')
    if '--smoke-test' in sys.argv:
        import tempfile
        if sys.platform=='win32':
            from .secrets import backend,SERVICE
            backend().get_password(SERVICE,'smoke-read:'+uuid.uuid4().hex)
        with tempfile.TemporaryDirectory() as folder:
            return run_desktop(app,Path(folder),smoke=True)
    from PySide6.QtCore import QStandardPaths
    folder=Path(QStandardPaths.writableLocation(QStandardPaths.AppLocalDataLocation))
    return run_desktop(app,folder)


if __name__ == '__main__':
    raise SystemExit(main())
