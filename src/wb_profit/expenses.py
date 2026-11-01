"""Только внешние расходы продавца; операции WB здесь вводить нельзя."""
from datetime import date
from decimal import Decimal
from uuid import uuid4
from .finance import Operation, Kind, money
from .storage import Ledger


def save_external_expense(ledger: Ledger, store: str, occurred: date,
                          amount: Decimal, description: str, sku: str | None = None,
                          identifier: str | None = None) -> str:
    amount = money(amount)
    if not store or not description.strip():
        raise ValueError('Укажите магазин и назначение внешнего расхода')
    if amount <= 0 or amount != amount.quantize(Decimal('0.01')):
        raise ValueError('Укажите положительную сумму с точностью до копейки')
    identifier = identifier or str(uuid4())
    # Назначение не является источником финансовых WB-данных.
    operation = Operation(store, 'external_expenses', identifier, occurred,
                          Kind.EXTERNAL, -amount, sku.strip() if sku and sku.strip() else None)
    # Один transaction охватывает операцию и её назначение.
    from dataclasses import asdict
    import json
    payload = asdict(operation)
    payload['amount'] = str(operation.amount)
    payload['occurred'] = occurred.isoformat()
    encoded = json.dumps(payload, ensure_ascii=False, sort_keys=True)
    with ledger.db:
        cancelled = ledger.setting(store, CANCELLED_PREFIX + identifier)
        if cancelled is not None:
            validate_cancelled_expense(store, CANCELLED_PREFIX + identifier, cancelled)
        if cancelled is not None and cancelled.get('restored_at') is None:
            raise ValueError('Расход отменён. Восстановите его из списка отменённых перед редактированием.')
        key = (store, operation.source, identifier)
        old = ledger.db.execute('SELECT payload FROM operations WHERE store=? AND source=? AND identifier=?', key).fetchone()
        if old and old[0] != encoded:
            ledger.db.execute('INSERT INTO revisions(store,source,identifier,old_payload) VALUES(?,?,?,?)', (*key, old[0]))
        ledger.db.execute('INSERT INTO operations VALUES(?,?,?,?) ON CONFLICT(store,source,identifier) DO UPDATE SET payload=excluded.payload', (*key, encoded))
        ledger.db.execute('INSERT INTO settings VALUES(?,?,?) ON CONFLICT(store,name) DO UPDATE SET payload=excluded.payload',
                          (store, 'external_expense:' + identifier, json.dumps({'description': description.strip()}, ensure_ascii=False)))
    return identifier


CANCELLED_PREFIX = 'cancelled_external_expense:'


def validate_cancelled_expense(store, name, value):
    import json
    from datetime import datetime
    from .storage import decode_operation
    if (not isinstance(value, dict) or set(value) != {'operation', 'description', 'cancelled_at', 'restored_at'}
            or not isinstance(value['description'], str) or not value['description'].strip()):
        raise ValueError('Неверный формат отменённого внешнего расхода')
    operation = decode_operation(json.dumps(value['operation']))
    if (operation.store != store or operation.source != 'external_expenses' or operation.kind != Kind.EXTERNAL
            or name != CANCELLED_PREFIX + operation.identifier or operation.amount >= 0
            or operation.amount != operation.amount.quantize(Decimal('0.01'))
            or operation.confirmed is not True or operation.economic_key is not None
            or operation.sku is not None and (not isinstance(operation.sku, str) or not operation.sku.strip())):
        raise ValueError('Отменённая запись не является внешним расходом этого магазина')
    for field in ('cancelled_at', 'restored_at'):
        if value[field] is None and field == 'restored_at':
            continue
        stamp = datetime.fromisoformat(value[field])
        if stamp.tzinfo is None:
            raise ValueError('Неизвестен часовой пояс отмены расхода')
    return operation


def cancel_external_expense(ledger, store, identifier, *, expected=None):
    import json
    from datetime import datetime, timezone
    from .storage import decode_operation, operation_payload
    if ledger.db.in_transaction:
        raise ValueError('Завершите текущую запись в базу перед отменой расхода.')
    with ledger.db:
        ledger.db.execute('BEGIN IMMEDIATE')
        row = ledger.db.execute('SELECT payload FROM operations WHERE store=? AND source=? AND identifier=?',
                                (store, 'external_expenses', identifier)).fetchone()
        if row is None:
            return False
        operation = decode_operation(row[0])
        details = ledger.setting(store, 'external_expense:' + identifier, {})
        value = {'operation': operation_payload(operation), 'description': details.get('description', ''),
                 'cancelled_at': datetime.now(timezone.utc).isoformat(), 'restored_at': None}
        validate_cancelled_expense(store, CANCELLED_PREFIX + identifier, value)
        if expected is not None and operation != expected:
            raise ValueError('Расход изменился после открытия. Откройте его заново; данные не отменены.')
        ledger.db.execute('INSERT INTO revisions(store,source,identifier,old_payload) VALUES(?,?,?,?)',
                          (store, 'external_expenses', identifier, row[0]))
        ledger.db.execute('DELETE FROM operations WHERE store=? AND source=? AND identifier=?',
                          (store, 'external_expenses', identifier))
        ledger.db.execute('INSERT INTO settings VALUES(?,?,?) ON CONFLICT(store,name) DO UPDATE SET payload=excluded.payload',
                          (store, CANCELLED_PREFIX + identifier, json.dumps(value, ensure_ascii=False)))
    return True


def cancelled_expenses(ledger, store):
    import json
    result = []
    for name, payload in ledger.db.execute('SELECT name,payload FROM settings WHERE store=? AND substr(name,1,?)=?',
                                          (store, len(CANCELLED_PREFIX), CANCELLED_PREFIX)):
        value = json.loads(payload)
        operation = validate_cancelled_expense(store, name, value)
        if value['restored_at'] is None:
            result.append((operation, value))
    return sorted(result, key=lambda item: (item[1]['cancelled_at'], item[0].identifier), reverse=True)


def restore_external_expense(ledger, store, identifier):
    import json
    from datetime import datetime, timezone
    from .storage import operation_payload
    if ledger.db.in_transaction:
        raise ValueError('Завершите текущую запись в базу перед восстановлением расхода.')
    with ledger.db:
        ledger.db.execute('BEGIN IMMEDIATE')
        value = ledger.setting(store, CANCELLED_PREFIX + identifier)
        if value is None:
            return False
        operation = validate_cancelled_expense(store, CANCELLED_PREFIX + identifier, value)
        if value['restored_at'] is not None:
            return False
        key = (store, 'external_expenses', identifier)
        if ledger.db.execute('SELECT 1 FROM operations WHERE store=? AND source=? AND identifier=?', key).fetchone():
            raise ValueError('Расход с этим ID уже существует. Текущая запись не заменена.')
        ledger.db.execute('INSERT INTO operations VALUES(?,?,?,?)',
                          (*key, json.dumps(operation_payload(operation), ensure_ascii=False, sort_keys=True)))
        ledger.db.execute('INSERT INTO settings VALUES(?,?,?) ON CONFLICT(store,name) DO UPDATE SET payload=excluded.payload',
                          (store, 'external_expense:' + identifier, json.dumps({'description': value['description']}, ensure_ascii=False)))
        value['restored_at'] = datetime.now(timezone.utc).isoformat()
        ledger.db.execute('UPDATE settings SET payload=? WHERE store=? AND name=?',
                          (json.dumps(value, ensure_ascii=False), store, CANCELLED_PREFIX + identifier))
    return True
