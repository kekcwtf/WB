"""Загрузка официального API в фоновой Qt-нити; ни одного GUI-вызова из asyncio."""
import asyncio
import json
from datetime import datetime, timedelta, date, timezone
from pathlib import Path
from PySide6.QtCore import QThread, Signal
from .api.auth import inspect_token, CATEGORIES
from .access import save_access
from .api.transport import WbTransport, ApiError
from .api.financial_reports import FinancialReports
from .api.report_totals import ReportTotals
from .api.orders import Orders
from .api.sales import Sales
from .api.products import Products
from .api.advertising import Advertising
from .api.ad_finances import AdAccount,AdPayments,AdCampaigns,eligible_campaigns
from .api.ad_statistics import AdvertisingStatistics
from .api.tariffs import Tariffs
from .api.stocks import Stocks, ReportExpired
from .sync import SyncStore, Synchronizer, PaginationError
from .storage import Ledger
from .normalization import financial_row
from .secrets import load_token
from .retries import PREFIX,valid_plan,due,due_shops,save_failure,clear_shop

NORMALIZATION_VERSION=2

SOURCE_LABELS = {'report_totals':'Контрольные суммы отчётов','finance_weekly':'Финансовые отчёты','orders':'Заказы','sales':'Продажи и возвраты',
                 'products':'Карточки товаров','ad_account':'Счёт продвижения','ad_payments':'Пополнения продвижения','ad_campaigns':'Рекламные кампании','ad_statistics':'Статистика рекламы','advertising':'Рекламные затраты','tariffs':'Комиссии FBW','stocks':'Остатки'}


def rebuild_finance(path, shop):
    ledger = Ledger(path)
    operations, issues = [], []
    try:
        from .returns import resolve_returns
        from dataclasses import replace
        from .finance import Kind
        raw_rows=[json.loads(payload) for (payload,) in ledger.db.execute("SELECT payload FROM source_records WHERE store=? AND source='finance_weekly'",(shop,))]
        links=resolve_returns(raw_rows)
        for row in raw_rows:
            identifier=f"{row['reportId']}:{row['rrdId']}"
            try:
                normalized = financial_row(shop,row)
                for op in normalized.operations:
                    link=links.linked.get(identifier) if op.kind==Kind.RETURN else None
                    operations.append(replace(op,original_sale_id=link.sale_identifier,cost_basis_date=link.sale_date) if link else op)
                issues.extend(f'{identifier}: {issue}' for issue in normalized.issues)
            except (ValueError,KeyError,TypeError,ArithmeticError):
                # Не заменяем действующий реестр частично разобранным новым.
                raise ApiError('Нормализация остановлена: неизвестная схема, дата или валюта финансовой строки') from None
        ledger.replace_source(shop,'wb_finance',operations)
        ledger.set_setting(shop,'normalization_issues',issues)
        ledger.set_setting(shop,'return_link_issues',links.issues)
        ledger.set_setting(shop,'coverage_confirmed',False)
        ledger.set_setting(shop,'normalization_version',NORMALIZATION_VERSION)
    finally:
        ledger.close()


def ensure_cached_normalization(path,shop):
    """Миграция полного локального снимка без запросов WB, включая офлайн-запуск."""
    storage=SyncStore(path)
    ledger=Ledger(path)
    try:
        required=(ledger.setting(shop,'normalization_version',0)<NORMALIZATION_VERSION
                  and storage.state(shop,'finance_weekly')['completed']
                  and ledger.db.execute("SELECT 1 FROM source_records WHERE store=? AND source='finance_weekly' LIMIT 1",(shop,)).fetchone())
    finally:
        ledger.close();storage.close()
    if required:rebuild_finance(path,shop)


def rebuild_report_audits(path,shop):
    from .report_audit import audit_reports
    from .data_views import records
    from dataclasses import asdict
    from .sync import encode
    ledger=Ledger(path)
    try:
        audits=audit_reports(shop,records(ledger,shop,'finance_weekly'),records(ledger,shop,'report_totals'))
        ledger.set_setting(shop,'report_audits',json.loads(json.dumps([asdict(a) for a in audits],default=encode)))
        ledger.set_setting(shop,'audit_message','Сверка отчётов выполнена' if audits else 'Нет данных для независимой сверки')
    except (ValueError,KeyError,TypeError,ArithmeticError):
        ledger.set_setting(shop,'report_audits',[])
        ledger.set_setting(shop,'audit_message','Сверка остановлена: неизвестная схема или валюта')
    finally:ledger.close()


async def synchronize_shop(path: str | Path, shop: str, token: str, notify, *, client_factory=WbTransport,stats_period=None,force=False,retry_only=False):
    if stats_period is not None and stats_period[0]>stats_period[1]:
        raise ValueError('Неверный период рекламной статистики')
    claims = inspect_token(token)
    if claims.expired:
        raise ApiError('Срок действия токена истёк')
    if claims.sandbox:
        raise ApiError('Тестовый токен не подходит для загрузки рабочего магазина')
    storage = SyncStore(path)
    ledger = Ledger(path)
    try:
        save_access(ledger,shop,claims)
    except BaseException:
        storage.close();ledger.close()
        raise
    sync = Synchronizer(storage)
    identity_plan=ledger.setting(shop,PREFIX+'identity')
    if (retry_only and not due_shops(ledger,[shop]) or
            valid_plan(identity_plan) and not due(identity_plan,force=force)):
        notify(shop,'token','Повтор проверки доступа отложен до разрешённого времени')
        storage.close();ledger.close();return
    clients = {}
    def reserve_rate(host,interval):
        import time
        key='api_next:'+host
        with ledger.db:
            ledger.db.execute('BEGIN IMMEDIATE')
            now=time.time()
            row=ledger.db.execute('SELECT payload FROM settings WHERE store=? AND name=?',(shop,key)).fetchone()
            next_at=json.loads(row[0]) if row else 0
            if next_at>now:
                return next_at-now
            ledger.db.execute('INSERT INTO settings VALUES(?,?,?) ON CONFLICT(store,name) DO UPDATE SET payload=excluded.payload',(shop,key,json.dumps(now+interval)))
        return 0
    def block_rate(host,delay):
        import time
        key='api_next:'+host
        with ledger.db:
            ledger.db.execute('BEGIN IMMEDIATE')
            row=ledger.db.execute('SELECT payload FROM settings WHERE store=? AND name=?',(shop,key)).fetchone()
            until=max(json.loads(row[0]) if row else 0,time.time()+delay)
            ledger.db.execute('INSERT INTO settings VALUES(?,?,?) ON CONFLICT(store,name) DO UPDATE SET payload=excluded.payload',(shop,key,json.dumps(until)))
    def client(host, interval, bucket=None):
        key=bucket or host
        if key not in clients:
            clients[key] = client_factory('https://'+host+'.wildberries.ru', lambda: token, interval=interval)
            if isinstance(clients[key],WbTransport):
                clients[key].rate_key=key
                clients[key].rate_reserver=lambda seconds:reserve_rate(key,seconds)
                clients[key].rate_blocker=lambda seconds:block_rate(key,seconds)
        return clients[key]
    # Привязываем локальный магазин к подтверждённому сервером sid.
    # Новый токен другого продавца не должен смешать его данные с существующим магазином.
    import hashlib
    fingerprint=hashlib.sha256(token.encode()).hexdigest()
    previous=ledger.setting(shop,'verified_api_identity')
    verified_at=datetime.fromisoformat(previous['verified_at']) if previous else None
    if not previous or previous.get('fingerprint')!=fingerprint or datetime.now(timezone.utc)-verified_at>=timedelta(days=1):
        try:
            identity=await client('common-api',60 if claims.fast_limits else 86400,bucket='seller-info').request('GET','/api/v1/seller-info')
            if not isinstance(identity,dict) or not isinstance(identity.get('sid'),str) or not identity['sid']:
                raise ApiError('Не удалось подтвердить ID продавца через официальный API')
            if previous and previous['seller_id']!=identity['sid']:
                raise ApiError('Этот токен принадлежит другому продавцу. Добавьте отдельный магазин; существующие данные сохранены.')
            ledger.set_setting(shop,'verified_api_identity',{'seller_id':identity['sid'],'name':identity.get('name',''),
                'country':identity.get('country'), 'fingerprint':fingerprint,'verified_at':datetime.now(timezone.utc).isoformat()})
        except BaseException as exc:
            if isinstance(exc,ApiError):
                save_failure(ledger,shop,'identity',exc,rate_until=ledger.setting(shop,'api_next:seller-info',0))
            await asyncio.gather(*(c.close() for c in clients.values()))
            storage.close();ledger.close()
            raise
    if identity_plan:ledger.set_setting(shop,PREFIX+'identity',None)
    today = datetime.now(timezone(timedelta(hours=3))).date()
    def start_for(name, first, overlap):
        last = storage.state(shop,name)['last_success']
        return max(first,datetime.fromisoformat(last).date()-timedelta(days=overlap)) if last else first
    full_at=ledger.setting(shop,'full_finance_at')
    full_due=not full_at or datetime.now(timezone.utc)-datetime.fromisoformat(full_at)>=timedelta(days=7)
    finance_start=date(2024,1,29) if full_due else start_for('finance_weekly',date(2024,1,29),35)
    finance = FinancialReports(client('finance-api',60 if claims.fast_limits else 43200),finance_start,today)
    stats = client('statistics-api',60 if claims.fast_limits else 10800)
    initial = today-timedelta(days=89)
    def advertising_start(name):
        last_full=ledger.setting(shop,'full_source_at:'+name)
        if not last_full or datetime.now(timezone.utc)-datetime.fromisoformat(last_full)>=timedelta(days=7):
            return date(2024,1,1)
        start=start_for(name,date(2024,1,1),31)
        if force and stats_period is not None:start=min(start,stats_period[0])
        return start
    def operational_start(name):
        ranges=ledger.setting(shop,'source_coverage:'+name,[])
        proposed=max(initial,start_for(name,initial,7))
        required=today-timedelta(days=29)
        # После нескольких дней офлайн перекрытие само закрывает новый разрыв;
        # полные 90 дней нужны только при недостаточной исходной истории.
        has_history=(proposed<=required or any(left<=required.isoformat() and
                     right>=(proposed-timedelta(days=1)).isoformat() for left,right in ranges))
        return proposed if has_history else initial

    promotion_client=client('advert-api',1 if claims.fast_limits else 3600)
    campaigns_source=AdCampaigns(promotion_client)
    sources = [finance, Orders(stats,operational_start('orders')), Sales(stats,operational_start('sales')),
               Products(client('content-api',0.6),initial_cursor=storage.state(shop,'products')['cursor'] if storage.state(shop,'products')['completed'] else None),
               AdAccount(promotion_client),
               AdPayments(promotion_client,advertising_start('ad_payments'),today),
               Advertising(promotion_client,
                           advertising_start('advertising'),today),
               Tariffs(client('common-api',60 if claims.fast_limits else 720)),
               Stocks(client('seller-analytics-api',60 if claims.fast_limits else 900))]
    if claims.fast_limits:
        sources.append(ReportTotals(clients['finance-api'],finance.start,finance.end))
    else:
        notify(shop,'report_totals','Список отчётов требует персональный или сервисный токен; чистая прибыль без сверки не подтверждается')
    refresh_seconds = {'ad_account':1800,'ad_payments':1800 if claims.fast_limits else 3600,'ad_campaigns':1800 if claims.fast_limits else 900,'ad_statistics':1800 if claims.fast_limits else 3600,'report_totals':1800,'finance_weekly':1800 if claims.fast_limits else 43200,
                       'orders':1800 if claims.fast_limits else 10800,'sales':1800 if claims.fast_limits else 7200,
                       'products':21600,'advertising':1800 if claims.fast_limits else 3600,'tariffs':86400,
                       'stocks':1800 if claims.fast_limits else 3600}
    async def run_source(source):
        name = source.name
        if source.category and not claims.permits(source.category):
            message='Нет заявленного права «'+CATEGORIES[source.category][1]+'»'
            ledger.set_setting(shop,PREFIX+name,None)
            ledger.set_setting(shop,'source_message:'+name,message)
            notify(shop,name,message)
            return
        state=storage.state(shop,name)
        last = ledger.setting(shop,'source_attempt:'+name)
        now=datetime.now(timezone.utc)
        plan=ledger.setting(shop,PREFIX+name)
        retrying=valid_plan(plan) and due(plan,now,force=force)
        if retry_only and not retrying:return
        if valid_plan(plan) and not retrying:
            notify(shop,name,'Повтор не раньше '+plan['next_at']);return
        minimum = refresh_seconds[name] if state['completed'] else (60 if claims.fast_limits else refresh_seconds[name])
        if retrying:minimum=0
        window_missing=False
        if name=='ad_statistics':
            ranges=ledger.setting(shop,'source_coverage:'+name,[])
            window_missing=(not any(left<=source.start.isoformat() and right>=source.end.isoformat() for left,right in ranges)
                            or ledger.setting(shop,'ad_statistics_campaigns')!=source.ids)
        basic_minimum={'finance_weekly':43200,'report_totals':43200,'orders':10800,'sales':7200,'products':0.6,'advertising':3600,'ad_payments':3600,'ad_account':1800,'ad_campaigns':900,'ad_statistics':3600,'tariffs':720,'stocks':900}
        if force and not claims.fast_limits and last and (now-datetime.fromisoformat(last)).total_seconds()<basic_minimum[name]:
            notify(shop,name,'Лимит базового токена WB ещё не позволяет повторный запрос');return
        if last and (now-datetime.fromisoformat(last)).total_seconds() < minimum and not window_missing and not force:
            notify(shop,name,'Кеш: повторный запрос пока не требуется или запрещён лимитом')
            return
        ledger.set_setting(shop,'source_attempt:'+name,now.isoformat())
        notify(shop,name,'Загрузка…')
        try:
            if name in {'orders','sales'}:
                pending=ledger.setting(shop,'operational_window:'+name)
                if pending and not state['completed']:
                    source.start=date.fromisoformat(pending)
                else:
                    if not state['completed'] and state['cursor'] is not None:
                        # Старый курсор не доказывает, откуда начиналась история.
                        with storage.db:
                            storage.db.execute('UPDATE sync_state SET cursor=NULL WHERE store=? AND source=?',(shop,name))
                    ledger.set_setting(shop,'operational_window:'+name,source.start.isoformat())
            await sync.run(shop,source)
            if name in {'finance_weekly','report_totals','advertising','ad_payments','ad_statistics','orders','sales'}:
                from .coverage import merge_ranges
                start=source.start.replace(day=1) if name in {'advertising','ad_payments','ad_statistics'} else source.start
                end=source.end if hasattr(source,'end') else today
                if name in {'orders','sales'}:start=max(start,today-timedelta(days=89))
                ledger.set_setting(shop,'source_coverage:'+name,merge_ranges(ledger.setting(shop,'source_coverage:'+name,[]),start,end))
                if name in {'orders','sales'}:ledger.set_setting(shop,'operational_window:'+name,None)
            if name in {'advertising','ad_payments'} and source.start==date(2024,1,1):
                ledger.set_setting(shop,'full_source_at:'+name,datetime.now(timezone.utc).isoformat())
            if name=='ad_statistics':
                ledger.set_setting(shop,'ad_statistics_campaigns',source.ids)
            if name == 'finance_weekly':
                rebuild_finance(path,shop)
                if source.start==date(2024,1,29):
                    ledger.set_setting(shop,'full_finance_at',datetime.now(timezone.utc).isoformat())
            if name in {'finance_weekly','report_totals'}:
                rebuild_report_audits(path,shop)
            if plan:ledger.set_setting(shop,PREFIX+name,None)
            notify(shop,name,'Обновлено '+datetime.now().strftime('%H:%M'))
            ledger.set_setting(shop,'source_message:'+name,'Успешно')
        except asyncio.CancelledError:
            raise
        except ApiError as exc:
            if isinstance(exc,ReportExpired):
                with storage.db:
                    storage.db.execute('UPDATE sync_state SET cursor=NULL,completed=0 WHERE store=? AND source=?',(shop,name))
            rate_key=getattr(source.client,'rate_key',None)
            rate_until=ledger.setting(shop,'api_next:'+rate_key,0) if rate_key else 0
            if not claims.fast_limits:
                rate_until=max(float(rate_until),datetime.fromisoformat(ledger.setting(shop,'source_attempt:'+name)).timestamp()+basic_minimum[name])
            scheduled=save_failure(ledger,shop,name,exc,rate_until=rate_until)
            message=str(exc)+(('; повтор не раньше '+scheduled['next_at']) if scheduled else '')
            ledger.set_setting(shop,'source_message:'+name,message)
            notify(shop,name,message)
        except PaginationError as exc:
            ledger.set_setting(shop,PREFIX+name,None)
            message=str(exc)+'. Последние сохранённые данные и позиция загрузки сохранены; завершённое покрытие не расширено.'
            ledger.set_setting(shop,'source_message:'+name,message)
            notify(shop,name,message)
        except Exception:
            ledger.set_setting(shop,PREFIX+name,None)
            message='Ошибка обработки источника; сохранённые данные не удалены'
            ledger.set_setting(shop,'source_message:'+name,message)
            notify(shop,name,message)
    async def campaign_statistics_chain():
        await run_source(campaigns_source)
        from .data_views import records
        snapshots=records(ledger,shop,'ad_campaigns')
        if not snapshots:
            notify(shop,'ad_statistics','Нет проверенного списка кампаний; статистика не запрашивается')
            return
        left,right=stats_period or (today-timedelta(days=29),today)
        ids=eligible_campaigns(snapshots[0])
        if not ids:
            message='Нет кампаний с доступной статистикой; ранее загруженные данные сохранены'
            ledger.set_setting(shop,'source_message:ad_statistics',message)
            notify(shop,'ad_statistics',message)
            return
        statistics=AdvertisingStatistics(client('advert-api',20 if claims.fast_limits else 3600,bucket='advert-stats'),
                                         ids,left,right)
        await run_source(statistics)
    try:
        await asyncio.gather(*(run_source(source) for source in sources),campaign_statistics_chain())
        rebuild_report_audits(path,shop)
    finally:
        await asyncio.gather(*(c.close() for c in clients.values()))
        storage.close()
        ledger.close()


class SyncWorker(QThread):
    progress = Signal(str,str,str)
    def __init__(self,path,shops,parent=None,*,stats_period=None,force=False,retry_only=False):
        super().__init__(parent)
        self.path,self.shops = path,shops
        self.stats_period=stats_period
        self.force=force
        self.retry_only=retry_only
        self.loop,self.task = None,None
    async def work(self):
        self.loop=asyncio.get_running_loop()
        self.task=asyncio.current_task()
        for shop in self.shops:
            if self.isInterruptionRequested():
                return
            try:
                ensure_cached_normalization(self.path,shop)
                token=load_token(shop)
                if not token:
                    self.pause_retries(shop)
                    self.progress.emit(shop,'token','Сначала сохраните токен магазина в настройках')
                    continue
                kwargs={'stats_period':self.stats_period} if self.stats_period is not None else {}
                if self.force:kwargs['force']=True
                if self.retry_only:kwargs['retry_only']=True
                await synchronize_shop(self.path,shop,token,self.progress.emit,**kwargs)
            except asyncio.CancelledError:
                return
            except (ApiError,ValueError) as exc:
                if not isinstance(exc,ApiError) or not exc.retryable:self.pause_retries(shop)
                self.progress.emit(shop,'token',str(exc))
            except Exception:
                self.pause_retries(shop)
                self.progress.emit(shop,'token','Не удалось прочитать Windows Credential Manager или обработать токен')
    def pause_retries(self,shop):
        ledger=Ledger(self.path)
        try:clear_shop(ledger,shop)
        finally:ledger.close()

    def run(self):
        try:
            asyncio.run(self.work())
        finally:
            self.loop,self.task = None,None
    def cancel(self):
        self.requestInterruption()
        if self.loop and self.task:
            try:
                self.loop.call_soon_threadsafe(self.task.cancel)
            except RuntimeError:
                pass
