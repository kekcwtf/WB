"""Аудит ручной себестоимости с обратимой правкой отдельного ключа SKU/дата."""
from datetime import date, datetime, timezone
from decimal import Decimal
import json
from uuid import uuid4
from .finance import Cost

PREFIX = 'cost_change:'


def _row(value):
    if not isinstance(value, dict) or set(value) != {'sku', 'date', 'amount'} or not isinstance(value['sku'], str) or not value['sku'].strip() or not isinstance(value['amount'], str):
        raise ValueError('Неверная запись себестоимости')
    Cost(value['sku'], date.fromisoformat(value['date']), Decimal(value['amount']))
    return value


def _key(row):
    return row['sku'], row['date']


def validate_change(store, name, value):
    if (not isinstance(value, dict) or set(value) != {'store', 'sequence', 'before', 'after', 'changed_at', 'undone_at'}
            or value['store'] != store or type(value['sequence']) is not int or value['sequence'] <= 0
            or not name.startswith(PREFIX) or len(name[len(PREFIX):]) != 32):
        raise ValueError('Неверный журнал изменений себестоимости')
    if value['before'] is None and value['after'] is None:
        raise ValueError('Нет изменения себестоимости')
    for row in (value['before'], value['after']):
        if row is not None:
            _row(row)
    if value['before'] is not None and value['after'] is not None and _key(value['before']) != _key(value['after']):
        raise ValueError('Изменение себестоимости относится к разным ключам')
    for field in ('changed_at', 'undone_at'):
        if field == 'undone_at' and value[field] is None:
            continue
        if datetime.fromisoformat(value[field]).tzinfo is None:
            raise ValueError('Неизвестен часовой пояс изменения себестоимости')
    return value


def _history(ledger, shop):
    history = ledger.setting(shop, 'costs', [])
    if not isinstance(history, list):
        raise ValueError('Неверная история себестоимости')
    keys = []
    for row in history:
        keys.append(_key(_row(row)))
    if len(keys) != len(set(keys)):
        raise ValueError('Повтор SKU и даты в истории себестоимости')
    return history


def _write(ledger, shop, name, value):
    ledger.db.execute('INSERT INTO settings VALUES(?,?,?) ON CONFLICT(store,name) DO UPDATE SET payload=excluded.payload',
                      (shop, name, json.dumps(value, ensure_ascii=False)))


def change_cost(ledger, shop, sku, effective, amount):
    if not shop or not isinstance(sku, str) or not sku.strip():
        raise ValueError('Укажите магазин и SKU себестоимости')
    if type(effective) is not date:
        raise ValueError('Нужна дата начала действия себестоимости')
    replacement = None
    if amount is not None:
        cost = Cost(sku.strip(), effective, amount)
        replacement = {'sku': cost.sku, 'date': cost.effective.isoformat(), 'amount': str(cost.per_unit)}
    key = (sku.strip(), effective.isoformat())
    if ledger.db.in_transaction:
        raise ValueError('Завершите текущую запись в базу перед изменением себестоимости')
    with ledger.db:
        ledger.db.execute('BEGIN IMMEDIATE')
        history = _history(ledger, shop)
        previous = next((row for row in history if _key(row) == key), None)
        if previous is None and replacement is None:
            return False
        if previous is not None and replacement is not None and Decimal(previous['amount']) == Decimal(replacement['amount']):
            return False
        sequence = ledger.setting(shop, 'cost_change_sequence', 0)
        if type(sequence) is not int or sequence < 0:
            raise ValueError('Неверная последовательность журнала себестоимости')
        value = {'store': shop, 'sequence': sequence+1, 'before': previous, 'after': replacement,
                 'changed_at': datetime.now(timezone.utc).isoformat(), 'undone_at': None}
        history = [row for row in history if _key(row) != key]
        if replacement is not None:
            history.append(replacement)
        _write(ledger, shop, 'costs', history)
        _write(ledger, shop, 'cost_change_sequence', sequence+1)
        _write(ledger, shop, PREFIX+uuid4().hex, value)
    return True


def changes(ledger, shop, limit=200):
    if type(limit) is not int or not 1 <= limit <= 1000:
        raise ValueError('Неверный размер истории изменений')
    result = []
    for name, payload in ledger.db.execute('SELECT name,payload FROM settings WHERE store=? AND substr(name,1,?)=? ORDER BY json_extract(payload,\'$.sequence\') DESC LIMIT ?',
                                          (shop, len(PREFIX), PREFIX, limit)):
        result.append((name, validate_change(shop, name, json.loads(payload))))
    return result


def undo_cost_change(ledger, shop):
    if ledger.db.in_transaction:
        raise ValueError('Завершите текущую запись в базу перед отменой себестоимости')
    with ledger.db:
        ledger.db.execute('BEGIN IMMEDIATE')
        row = ledger.db.execute("SELECT name,payload FROM settings WHERE store=? AND substr(name,1,?)=? AND json_extract(payload,'$.undone_at') IS NULL ORDER BY json_extract(payload,'$.sequence') DESC LIMIT 1",
                                (shop, len(PREFIX), PREFIX)).fetchone()
        if row is None:
            return False
        name, value = row[0], validate_change(shop, row[0], json.loads(row[1]))
        key = _key(value['after'] or value['before'])
        history = _history(ledger, shop)
        current = next((item for item in history if _key(item) == key), None)
        if current != value['after']:
            raise ValueError('Себестоимость изменена вне журнала. Автоматическая отмена остановлена; текущие цены не заменены.')
        history = [item for item in history if _key(item) != key]
        if value['before'] is not None:
            history.append(value['before'])
        value['undone_at'] = datetime.now(timezone.utc).isoformat()
        _write(ledger, shop, 'costs', history)
        _write(ledger, shop, name, value)
    return True
