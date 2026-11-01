"""Согласованные SQLite-снимки, проверка формата и восстановление с откатом SQLite."""
import json
import os
import sqlite3
import tempfile
import time
import uuid
from contextlib import closing
from datetime import date, datetime, timezone
from decimal import Decimal
from pathlib import Path
from .finance import Cost, Kind, Operation, TaxSettings, calculate


class BackupError(ValueError):
    pass


CORE = {
    'operations': ('store', 'source', 'identifier', 'payload'),
    'revisions': ('id', 'store', 'source', 'identifier', 'old_payload', 'changed_at'),
    'settings': ('store', 'name', 'payload'),
}
SYNC = {
    'source_records': ('store', 'source', 'identifier', 'payload'),
    'source_revisions': ('id', 'store', 'source', 'identifier', 'payload', 'changed_at'),
    'sync_seen': ('store', 'source', 'run_id', 'identifier'),
    'sync_state': ('store', 'source', 'cursor', 'completed', 'last_success', 'status'),
}
PRIMARY_KEYS = {
    'operations': ('store', 'source', 'identifier'), 'revisions': ('id',),
    'settings': ('store', 'name'), 'source_records': ('store', 'source', 'identifier'),
    'source_revisions': ('id',), 'sync_seen': ('store', 'source', 'run_id', 'identifier'),
    'sync_state': ('store', 'source'),
}


def _different_path(connection, path):
    live = next(row[2] for row in connection.execute('PRAGMA database_list') if row[1] == 'main')
    if live and (Path(live).resolve() == path.resolve() or
                 path.exists() and os.path.samefile(live, path)):
        raise BackupError('Выберите другой файл: это текущая рабочая база.')


def _copy(source, destination, cancel_check=None):
    busy_since = None

    def progress(status, remaining, total):
        nonlocal busy_since
        if cancel_check is not None:cancel_check()
        if status in {sqlite3.SQLITE_BUSY, sqlite3.SQLITE_LOCKED}:
            busy_since = busy_since or time.monotonic()
            if time.monotonic() - busy_since > 30:
                raise BackupError('База занята другой программой. Повторите позже.')
        else:
            busy_since = None

    if source.in_transaction or destination.in_transaction:
        raise BackupError('Дождитесь завершения записи в базу.')
    if cancel_check is not None:cancel_check()
    source.backup(destination, pages=256, progress=progress, sleep=0.05)


def _integrity(connection):
    if connection.execute('PRAGMA integrity_check').fetchall() != [('ok',)]:
        raise BackupError('Файл базы повреждён; рабочая база не заменена.')


def write_backup(connection, filename, *, cancel_check=None):
    """Пишет в отдельный временный файл; старую копию заменяет только после успеха."""
    target = Path(filename).absolute()
    _different_path(connection, target)
    if any(Path(str(target) + suffix).exists() for suffix in ('-wal', '-shm', '-journal')):
        raise BackupError('Выбранный файл используется SQLite. Сохраните копию под другим именем.')
    temporary = None
    try:
        handle, name = tempfile.mkstemp(prefix='.wb-backup-', suffix='.sqlite', dir=target.parent)
        os.close(handle)
        temporary = Path(name)
        with closing(sqlite3.connect(temporary)) as destination:
            if cancel_check is None:_copy(connection, destination)
            else:_copy(connection,destination,cancel_check)
            destination.execute('PRAGMA journal_mode=DELETE')
            _integrity(destination)
        with temporary.open('rb+') as stream:
            os.fsync(stream.fileno())
        if cancel_check is not None:cancel_check()
        os.replace(temporary, target)
        return target
    except sqlite3.Error as exc:
        raise BackupError('Не удалось создать целую резервную копию SQLite.') from exc
    finally:
        if temporary is not None:
            temporary.unlink(missing_ok=True)


def _json(payload):
    def invalid(_value):
        raise BackupError('Некорректное числовое значение в резервной копии.')
    return json.loads(payload, parse_constant=invalid)


def _validate_settings(store, name, value):
    if 'token' in name.lower() or 'secret' in name.lower():
        raise BackupError('Копия содержит поле секрета в SQLite. Такой формат не поддерживается.')
    if store == '_app' and name == 'stores':
        if not isinstance(value, list):
            raise BackupError('Неверный список магазинов в копии.')
        ids = set()
        for item in value:
            if (not isinstance(item, dict) or not isinstance(item.get('id'), str) or
                    not item['id'] or item['id'] in ids or not isinstance(item.get('name'), str) or not item['name'].strip()):
                raise BackupError('Неверные или повторяющиеся магазины в копии.')
            ids.add(item['id'])
    elif name == 'costs':
        if not isinstance(value, list):
            raise BackupError('Несовместимый формат истории себестоимости.')
        costs = [Cost(item['sku'], date.fromisoformat(item['date']), Decimal(item['amount'])) for item in value]
        calculate([], costs, None)  # в том числе конфликт цен на одну дату
    elif name in {'tax_history', 'taxes', 'tax_history_revisions'}:
        rows = [value] if name == 'taxes' else value
        if not isinstance(rows, list):
            raise BackupError('Несовместимый формат налоговых настроек.')
        for item in rows:
            TaxSettings(Decimal(item['usn']), Decimal(item['vat']), item['included'], item['confirmed'])
            if type(item['included']) is not bool or type(item['confirmed']) is not bool:
                raise BackupError('Неизвестное подтверждение налоговых настроек.')
            if name != 'taxes':
                date.fromisoformat(item['date'])
        if name == 'tax_history':
            from .taxes import TaxRule, rule_at
            rule_at([TaxRule(date.fromisoformat(item['date']),
                     TaxSettings(Decimal(item['usn']), Decimal(item['vat']), item['included'], item['confirmed']))
                     for item in rows], date.max)
    elif name.startswith('tax_change:'):
        from .tax_changes import validate_change
        validate_change(store, name, value)
    elif name == 'tax_change_sequence':
        if type(value) is not int or value < 0:
            raise BackupError('Неверная последовательность изменений налогов.')
    elif name.startswith('cost_change:'):
        from .cost_history import validate_change
        validate_change(store, name, value)
    elif name == 'cost_change_sequence':
        if type(value) is not int or value < 0:
            raise BackupError('Неверная последовательность изменений себестоимости.')
    elif name.startswith('cancelled_external_expense:'):
        from .expenses import validate_cancelled_expense
        validate_cancelled_expense(store, name, value)
    elif name == 'api_access':
        from .access import validate_access
        validate_access(value)
    elif name == 'period':
        if type(value['index']) is not int or not 0 <= value['index'] <= 6:
            raise BackupError('Неверный сохранённый период.')
        for field in ('start', 'end'):
            if field in value:
                date.fromisoformat(value[field])
    elif name == 'cost_allocation' and value not in {'revenue', 'unallocated'}:
        raise BackupError('Неизвестный метод распределения расходов.')
    elif store=='_app' and name=='auto_backup_options':
        from .auto_backups import validate_options
        validate_options(value)
    elif store=='_app' and name=='auto_backup_lineage' and value is not None:
        import re
        if not isinstance(value,str) or not re.fullmatch('[0-9a-f]{32}',value):
            raise BackupError('Неизвестная группа автоматических копий.')
    elif name == 'products_mode' and value not in {'profit','orders'}:
        raise BackupError('Неизвестный режим таблицы товаров.')
    elif name == 'daily_chart_mode' and value not in {'net','before_tax'}:
        raise BackupError('Неизвестный режим графика прибыли.')
    elif name == 'inventory_options':
        if not isinstance(value,dict):raise BackupError('Несовместимые настройки аналитики склада.')
        days=value.get('max_stock_days',90)
        percent=int(value.get('storage_warning_percent','10'))
        if type(days) is not int or not 1<=days<=3650 or not 1<=percent<=100:
            raise BackupError('Несовместимые пороги аналитики склада.')
    elif store == '_app' and name == 'dark' and type(value) is not bool:
        raise BackupError('Неизвестный формат настроек темы.')


def validate_backup(connection):
    """Не выполняет SQL из файла и не открывает его как рабочую базу приложения."""
    _integrity(connection)
    schema = connection.execute("SELECT name,type,sql FROM sqlite_master WHERE name NOT LIKE 'sqlite_%'").fetchall()
    tables = {name for name, kind, _sql in schema if kind == 'table'}
    if (not CORE.keys() <= tables or tables - (CORE.keys() | SYNC.keys()) or
            tables & SYNC.keys() and not SYNC.keys() <= tables or
            any(kind not in {'table', 'index'} or 'VIRTUAL TABLE' in (sql or '').upper() for _name, kind, sql in schema)):
        raise BackupError('Это не поддерживаемая резервная копия WB Profit Desktop.')
    for table, columns in (CORE | SYNC).items():
        if table in tables:
            info = connection.execute(f'PRAGMA table_info({table})').fetchall()
            actual = tuple(row[1] for row in info)
            primary = tuple(row[1] for row in sorted(info, key=lambda row: row[5]) if row[5])
            if actual != columns or primary != PRIMARY_KEYS[table]:
                raise BackupError('Структура копии несовместима с этой версией приложения.')
    try:
        stores = None
        for store, name, payload in connection.execute('SELECT store,name,payload FROM settings'):
            value = _json(payload)
            _validate_settings(store, name, value)
            if store == '_app' and name == 'stores':
                stores = {item['id'] for item in value}
        for store, source, identifier, payload in connection.execute('SELECT store,source,identifier,payload FROM operations'):
            value = _json(payload)
            value['occurred'] = date.fromisoformat(value['occurred'])
            value['kind'] = Kind(value['kind'])
            if value.get('cost_basis_date'):
                value['cost_basis_date'] = date.fromisoformat(value['cost_basis_date'])
            operation = Operation(**value)
            if (operation.store, operation.source, operation.identifier) != (store, source, identifier):
                raise BackupError('Операция не соответствует своему идентификатору.')
            if stores is None or store not in stores:
                raise BackupError('В копии есть операции магазина без сохранённой карточки магазина.')
        for table, column in [('revisions', 'old_payload'), ('source_records', 'payload'), ('source_revisions', 'payload')]:
            if table in tables:
                for (payload,) in connection.execute(f'SELECT {column} FROM {table}'):
                    if not isinstance(_json(payload), dict):
                        raise BackupError('Неверный формат исходных записей в копии.')
    except (KeyError, TypeError, ValueError, ArithmeticError) as exc:
        if isinstance(exc, BackupError):
            raise
        raise BackupError('Содержимое копии несовместимо с этой версией приложения.') from exc


def restore_backup(connection, filename):
    """Восстанавливает согласованный снимок. SQLite откатывает незавершённый backup."""
    source = Path(filename).resolve()
    if not source.is_file():
        raise BackupError('Файл резервной копии не найден.')
    _different_path(connection, source)
    live = next(row[2] for row in connection.execute('PRAGMA database_list') if row[1] == 'main')
    if not live:
        raise BackupError('Восстановление требует рабочую базу в файле.')
    try:
        with tempfile.TemporaryDirectory(prefix='.wb-restore-', dir=Path(live).parent) as folder:
            # Отдельный снимок защищает от изменения выбранной копии во время проверки.
            staged = Path(folder) / 'validated.sqlite'
            with closing(sqlite3.connect(source.as_uri() + '?mode=ro', uri=True)) as incoming:
                write_backup(incoming, staged)
            with closing(sqlite3.connect(staged)) as snapshot:
                validate_backup(snapshot)
            # Копии старого формата могут не иметь кеша. Готовим его до замены
            # рабочей базы: ошибка миграции не должна оставлять полувосстановление.
            from .sync import SyncStore
            cache = SyncStore(staged)
            cache.close()
            with closing(sqlite3.connect(staged)) as snapshot:
                validate_backup(snapshot)
                backups = Path(live).parent / 'backups'
                backups.mkdir(exist_ok=True)
                stamp = datetime.now(timezone.utc).strftime('%Y%m%d-%H%M%S')
                safety = write_backup(connection, backups / f'before-restore-{stamp}-{uuid.uuid4().hex[:8]}.sqlite')
                _copy(snapshot, connection)
                return safety
    except sqlite3.Error as exc:
        raise BackupError('Не удалось восстановить базу SQLite. Сохраните текущую базу и проверьте выбранную копию.') from exc
