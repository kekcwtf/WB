"""Обратимые изменения ручных налоговых настроек без автоматических льгот."""
from datetime import datetime, timezone
from decimal import Decimal
import json
from uuid import uuid4
from .finance import TaxSettings
from .taxes import TaxRule
from datetime import date

PREFIX = 'tax_change:'


def decode_row(row):
    if (not isinstance(row, dict) or set(row) != {'date', 'usn', 'vat', 'included', 'confirmed'}
            or not isinstance(row['usn'], str) or not isinstance(row['vat'], str)
            or type(row['included']) is not bool or type(row['confirmed']) is not bool):
        raise ValueError('Неверная версия налоговых настроек')
    return TaxRule(date.fromisoformat(row['date']), TaxSettings(Decimal(row['usn']), Decimal(row['vat']), row['included'], row['confirmed']))


def encode_rule(rule):
    row = {'date':rule.effective.isoformat(), 'usn':str(rule.settings.usn_percent), 'vat':str(rule.settings.vat_percent),
           'included':rule.settings.vat_included, 'confirmed':rule.settings.confirmed_by_user}
    decode_row(row)
    return row


def validate_change(shop, name, value):
    if (not isinstance(value, dict) or set(value) != {'store','sequence','before','after','changed_at','undone_at'}
            or value['store'] != shop or type(value['sequence']) is not int or value['sequence'] <= 0
            or not name.startswith(PREFIX) or len(name[len(PREFIX):]) != 32
            or value['before'] is None and value['after'] is None):
        raise ValueError('Неверный журнал налоговых настроек')
    for row in (value['before'], value['after']):
        if row is not None:decode_row(row)
    if value['before'] is not None and value['after'] is not None and value['before']['date'] != value['after']['date']:
        raise ValueError('Налоговая правка относится к разным датам')
    for field in ('changed_at','undone_at'):
        if field == 'undone_at' and value[field] is None:continue
        if datetime.fromisoformat(value[field]).tzinfo is None:
            raise ValueError('Неизвестен часовой пояс изменения налогов')
    return value


def _write(ledger, shop, name, value):
    ledger.db.execute('INSERT INTO settings VALUES(?,?,?) ON CONFLICT(store,name) DO UPDATE SET payload=excluded.payload',
                      (shop,name,json.dumps(value,ensure_ascii=False)))


def _rows(ledger, shop):
    values = ledger.setting(shop,'tax_history',[])
    if not isinstance(values,list):raise ValueError('Неверная налоговая история')
    rows = {}
    for row in values:
        rule = decode_row(row)
        if row['date'] in rows and decode_row(rows[row['date']]) != rule:
            raise ValueError('Разные налоговые настройки на одну дату')
        rows[row['date']] = row
    return list(rows.values())


def _save_state(ledger, shop, rows, previous):
    rows = sorted(rows,key=lambda row:row['date'])
    _write(ledger,shop,'tax_history',rows)
    revisions = ledger.setting(shop,'tax_history_revisions',[])
    if not isinstance(revisions,list):raise ValueError('Неверный архив прежних налоговых версий')
    if previous is not None:revisions.append(previous)
    _write(ledger,shop,'tax_history_revisions',revisions)
    if rows:
        _write(ledger,shop,'taxes',{key:value for key,value in rows[-1].items() if key != 'date'})
    else:
        ledger.db.execute("DELETE FROM settings WHERE store=? AND name='taxes'",(shop,))


def change_rule(ledger, shop, effective, rule, *, expected=None):
    if not shop or type(effective) is not date:
        raise ValueError('Выберите магазин и календарную дату налоговой версии')
    replacement = encode_rule(rule) if rule is not None else None
    if replacement is not None and rule.effective != effective:
        raise ValueError('Дата налоговой версии не соответствует выбранному ключу')
    if ledger.db.in_transaction:raise ValueError('Завершите текущую запись в базу перед правкой налогов')
    with ledger.db:
        ledger.db.execute('BEGIN IMMEDIATE')
        rows = _rows(ledger,shop)
        previous = next((row for row in rows if row['date'] == effective.isoformat()),None)
        if expected is not None and (previous is None or decode_row(previous) != expected):
            raise ValueError('Налоговая версия изменилась после открытия. Выберите её заново; настройки не удалены.')
        if previous is None and replacement is None:return False
        if previous is not None and replacement is not None and decode_row(previous) == decode_row(replacement):return False
        sequence = ledger.setting(shop,'tax_change_sequence',0)
        if type(sequence) is not int or sequence < 0:raise ValueError('Неверная последовательность налогового журнала')
        event = {'store':shop,'sequence':sequence+1,'before':previous,'after':replacement,
                 'changed_at':datetime.now(timezone.utc).isoformat(),'undone_at':None}
        rows = [row for row in rows if row['date'] != effective.isoformat()]
        if replacement is not None:rows.append(replacement)
        _save_state(ledger,shop,rows,previous)
        _write(ledger,shop,'tax_change_sequence',sequence+1)
        _write(ledger,shop,PREFIX+uuid4().hex,event)
    return True


def changes(ledger, shop, limit=200):
    if type(limit) is not int or not 1 <= limit <= 1000:raise ValueError('Неверный размер налогового журнала')
    return [(name,validate_change(shop,name,json.loads(payload))) for name,payload in ledger.db.execute(
        "SELECT name,payload FROM settings WHERE store=? AND substr(name,1,?)=? ORDER BY json_extract(payload,'$.sequence') DESC LIMIT ?",(shop,len(PREFIX),PREFIX,limit))]


def undo_change(ledger, shop):
    if not shop:raise ValueError('Сначала выберите магазин')
    if ledger.db.in_transaction:raise ValueError('Завершите текущую запись в базу перед отменой налоговой правки')
    with ledger.db:
        ledger.db.execute('BEGIN IMMEDIATE')
        row = ledger.db.execute("SELECT name,payload FROM settings WHERE store=? AND substr(name,1,?)=? AND json_extract(payload,'$.undone_at') IS NULL ORDER BY json_extract(payload,'$.sequence') DESC LIMIT 1",(shop,len(PREFIX),PREFIX)).fetchone()
        if row is None:return False
        name,value = row[0],validate_change(shop,row[0],json.loads(row[1]))
        effective = (value['after'] or value['before'])['date']
        rows = _rows(ledger,shop)
        current = next((row for row in rows if row['date'] == effective),None)
        if current != value['after']:
            raise ValueError('Налоговая версия изменена вне журнала. Текущие настройки не заменены.')
        rows = [row for row in rows if row['date'] != effective]
        if value['before'] is not None:rows.append(value['before'])
        _save_state(ledger,shop,rows,current)
        value['undone_at'] = datetime.now(timezone.utc).isoformat()
        _write(ledger,shop,name,value)
    return True
