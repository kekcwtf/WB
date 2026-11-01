"""Транзакционное сохранение страниц и курсора, независимо от методов WB.

Адаптер обязан валидировать страницу целиком до сохранения и выдавать стабильный
ID записи. Неподтверждённые API-схемы в этом модуле не предполагаются.
"""
import asyncio
import json
import sqlite3
from dataclasses import dataclass
from datetime import datetime, timezone
from decimal import Decimal
from pathlib import Path
from typing import Protocol, AsyncIterator


@dataclass(frozen=True)
class Record:
    identifier: str
    payload: dict


@dataclass(frozen=True)
class Page:
    records: tuple[Record, ...]
    next_cursor: str | None
    completed: bool = False
    snapshot: bool = False
    snapshot_scope: str | None = None
    run_id: str | None = None
    replace_window: tuple[str,str,str] | None = None


class Source(Protocol):
    name: str

    def pages(self, cursor: str | None) -> AsyncIterator[Page]: ...


class PaginationError(ValueError):
    """Безопасное описание нарушения продвижения курсора, без его значения."""


def encode(value):
    # Числа JSON уже разобраны транспортом как Decimal. Храним их точные строки.
    if isinstance(value, Decimal):
        if not value.is_finite():
            raise ValueError('Неконечная сумма')
        return str(value)
    raise TypeError('Неподдерживаемый тип исходного поля')


class SyncStore:
    def __init__(self, path: str | Path):
        self.db = sqlite3.connect(str(path), timeout=30)
        self.db.executescript('''
        PRAGMA journal_mode=WAL;
        CREATE TABLE IF NOT EXISTS source_records(
          store TEXT NOT NULL, source TEXT NOT NULL, identifier TEXT NOT NULL,
          payload TEXT NOT NULL, PRIMARY KEY(store,source,identifier));
        CREATE INDEX IF NOT EXISTS source_records_date ON source_records(store,source,substr(json_extract(payload,'$.rrDate'),1,10));
        CREATE INDEX IF NOT EXISTS source_records_operational_date ON source_records(store,source,substr(json_extract(payload,'$.date'),1,10));
        CREATE INDEX IF NOT EXISTS source_records_srid ON source_records(store,source,json_extract(payload,'$.srid'));
        CREATE TABLE IF NOT EXISTS source_revisions(
          id INTEGER PRIMARY KEY, store TEXT NOT NULL, source TEXT NOT NULL,
          identifier TEXT NOT NULL, payload TEXT NOT NULL,
          changed_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP);
        CREATE TABLE IF NOT EXISTS sync_seen(store TEXT NOT NULL,source TEXT NOT NULL,run_id TEXT NOT NULL,identifier TEXT NOT NULL,PRIMARY KEY(store,source,run_id,identifier));
        CREATE TABLE IF NOT EXISTS sync_state(
          store TEXT NOT NULL, source TEXT NOT NULL, cursor TEXT,
          completed INTEGER NOT NULL DEFAULT 0, last_success TEXT,
          status TEXT NOT NULL DEFAULT 'not_started',
          PRIMARY KEY(store,source));
        ''')

    def state(self, store, source):
        row = self.db.execute('SELECT cursor,completed,last_success,status FROM sync_state WHERE store=? AND source=?', (store, source)).fetchone()
        return {'cursor': row[0], 'completed': bool(row[1]), 'last_success': row[2], 'status': row[3]} if row else {
            'cursor': None, 'completed': False, 'last_success': None, 'status': 'not_started'}

    def mark(self, store, source, status):
        if status not in {'running', 'failed', 'cancelled'}:
            raise ValueError('Неизвестный статус')
        with self.db:
            self.db.execute('INSERT INTO sync_state(store,source,status) VALUES(?,?,?) ON CONFLICT(store,source) DO UPDATE SET status=excluded.status', (store, source, status))

    def save_page(self, store: str, source: str, page: Page):
        if page.replace_window is not None and (not page.completed or not page.run_id or page.replace_window[0]!='rrDate'):
            raise ValueError('Неверное завершение финансового снимка')
        if page.snapshot and not page.completed:
            raise ValueError('Снимок должен содержать завершённый набор данных')
        if page.snapshot_scope is not None and (not page.snapshot_scope or ':' in page.snapshot_scope):
            raise ValueError('Некорректная область снимка')
        seen = set()
        encoded = []
        for record in page.records:
            if page.snapshot_scope is not None and not record.identifier.startswith(page.snapshot_scope+':'):
                raise ValueError('Запись не принадлежит области снимка')
            if not record.identifier or record.identifier in seen:
                raise ValueError('Пустой или повторяющийся ID внутри страницы')
            seen.add(record.identifier)
            encoded.append((record.identifier, json.dumps(record.payload, default=encode, ensure_ascii=False,
                                                         sort_keys=True, allow_nan=False)))
        now = datetime.now(timezone.utc).isoformat()
        with self.db:
            if page.snapshot or page.snapshot_scope is not None:
                previous = self.db.execute('SELECT identifier,payload FROM source_records WHERE store=? AND source=?',(store,source)).fetchall()
                for identifier,old in previous:
                    if identifier not in seen and (page.snapshot_scope is None or identifier.startswith(page.snapshot_scope+':')):
                        self.db.execute('INSERT INTO source_revisions(store,source,identifier,payload) VALUES(?,?,?,?)',(store,source,identifier,old))
                        self.db.execute('DELETE FROM source_records WHERE store=? AND source=? AND identifier=?',(store,source,identifier))
            for identifier, payload in encoded:
                if page.run_id:
                    self.db.execute('INSERT OR IGNORE INTO sync_seen VALUES(?,?,?,?)',(store,source,page.run_id,identifier))
                key = (store, source, identifier)
                old = self.db.execute('SELECT payload FROM source_records WHERE store=? AND source=? AND identifier=?', key).fetchone()
                if old and old[0] != payload:
                    self.db.execute('INSERT INTO source_revisions(store,source,identifier,payload) VALUES(?,?,?,?)', (*key, old[0]))
                self.db.execute('INSERT INTO source_records VALUES(?,?,?,?) ON CONFLICT(store,source,identifier) DO UPDATE SET payload=excluded.payload', (*key, payload))
            if page.replace_window is not None:
                _,left,right=page.replace_window
                stale=self.db.execute("SELECT identifier,payload FROM source_records WHERE store=? AND source=? AND substr(json_extract(payload,'$.rrDate'),1,10) BETWEEN ? AND ? AND identifier NOT IN (SELECT identifier FROM sync_seen WHERE store=? AND source=? AND run_id=?)",(store,source,left,right,store,source,page.run_id)).fetchall()
                for identifier,payload in stale:
                    self.db.execute('INSERT INTO source_revisions(store,source,identifier,payload) VALUES(?,?,?,?)',(store,source,identifier,payload))
                    self.db.execute('DELETE FROM source_records WHERE store=? AND source=? AND identifier=?',(store,source,identifier))
                self.db.execute('DELETE FROM sync_seen WHERE store=? AND source=? AND run_id=?',(store,source,page.run_id))
            self.db.execute('''INSERT INTO sync_state VALUES(?,?,?,?,?,?)
                ON CONFLICT(store,source) DO UPDATE SET cursor=excluded.cursor,
                completed=excluded.completed, last_success=CASE WHEN excluded.completed=1 THEN excluded.last_success ELSE sync_state.last_success END,
                status=excluded.status''', (store, source, page.next_cursor, int(page.completed),
                                           now if page.completed else None, 'success' if page.completed else 'running'))

    def close(self):
        self.db.close()


class Synchronizer:
    def __init__(self, store: SyncStore):
        self.store = store
        self.locks: dict[tuple[str, str], asyncio.Lock] = {}

    async def run(self, shop: str, source: Source):
        lock = self.locks.setdefault((shop, source.name), asyncio.Lock())
        if lock.locked():
            return False  # таймер и кнопка не запускают параллельные запросы источника
        async with lock:
            state = self.store.state(shop, source.name)
            # Завершённый проход начинает новый интервал; адаптер выбирает перекрытие.
            cursor = None if state['completed'] else state['cursor']
            self.store.mark(shop, source.name, 'running')
            try:
                completed = False
                seen_cursors = {cursor}
                async for page in source.pages(cursor):
                    if completed:
                        raise ValueError('Источник вернул страницу после завершения')
                    if not page.completed and (page.next_cursor is None or page.next_cursor == cursor):
                        raise PaginationError('Курсор источника не продвигается')
                    if not page.completed and page.next_cursor in seen_cursors:
                        raise PaginationError('Источник повторил прежний курсор: загрузка остановлена без повторной записи страницы')
                    self.store.save_page(shop, source.name, page)
                    seen_cursors.add(page.next_cursor)
                    cursor = page.next_cursor
                    completed = page.completed
                if not completed:
                    raise ValueError('Источник завершился без подтверждения конца данных')
                return True
            except asyncio.CancelledError:
                self.store.mark(shop, source.name, 'cancelled')
                raise
            except Exception:
                self.store.mark(shop, source.name, 'failed')
                raise
