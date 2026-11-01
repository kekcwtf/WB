"""Локальное хранение исходных операций и истории исправлений без потери Decimal."""
import json
import sqlite3
from dataclasses import asdict
from datetime import date
from pathlib import Path
from .finance import Operation, Kind


def operation_payload(op: Operation):
    payload=asdict(op)
    payload['amount']=str(op.amount)
    payload['occurred']=op.occurred.isoformat()
    payload['cost_basis_date']=op.cost_basis_date.isoformat() if op.cost_basis_date else None
    return payload


def decode_operation(payload):
    data=json.loads(payload)
    data['occurred']=date.fromisoformat(data['occurred'])
    data['kind']=Kind(data['kind'])
    if data.get('cost_basis_date'):
        data['cost_basis_date']=date.fromisoformat(data['cost_basis_date'])
    return Operation(**data)


class Ledger:
    def __init__(self, path: str | Path):
        self.db = sqlite3.connect(str(path))
        self.db.execute('PRAGMA foreign_keys=ON')
        self.db.executescript('''
        CREATE TABLE IF NOT EXISTS operations (
          store TEXT NOT NULL, source TEXT NOT NULL, identifier TEXT NOT NULL,
          payload TEXT NOT NULL, PRIMARY KEY(store, source, identifier));
        CREATE INDEX IF NOT EXISTS operations_period ON operations(store,json_extract(payload,'$.occurred'));
        CREATE INDEX IF NOT EXISTS operations_return_origin ON operations(store,source,json_extract(payload,'$.original_sale_id'));
        CREATE TABLE IF NOT EXISTS revisions (
          id INTEGER PRIMARY KEY, store TEXT NOT NULL, source TEXT NOT NULL,
          identifier TEXT NOT NULL, old_payload TEXT NOT NULL,
          changed_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP);
        CREATE INDEX IF NOT EXISTS revisions_store_id ON revisions(store,id DESC);
        CREATE TABLE IF NOT EXISTS settings (
          store TEXT NOT NULL, name TEXT NOT NULL, payload TEXT NOT NULL,
          PRIMARY KEY(store, name));
        ''')

    def upsert(self, operations: list[Operation]) -> tuple[int, int, int]:
        added = updated = unchanged = 0
        with self.db:
            for op in operations:
                payload = operation_payload(op)
                encoded = json.dumps(payload, ensure_ascii=False, sort_keys=True)
                key = (op.store, op.source, op.identifier)
                row = self.db.execute('SELECT payload FROM operations WHERE store=? AND source=? AND identifier=?', key).fetchone()
                if row is None:
                    self.db.execute('INSERT INTO operations VALUES(?,?,?,?)', (*key, encoded))
                    added += 1
                elif row[0] != encoded:
                    self.db.execute('INSERT INTO revisions(store,source,identifier,old_payload) VALUES(?,?,?,?)', (*key, row[0]))
                    self.db.execute('UPDATE operations SET payload=? WHERE store=? AND source=? AND identifier=?', (encoded, *key))
                    updated += 1
                else:
                    unchanged += 1
        return added, updated, unchanged

    def operations(self, store: str, start: date, end: date) -> list[Operation]:
        result = []
        for row in self.db.execute("SELECT payload FROM operations WHERE store=? AND json_extract(payload,'$.occurred') BETWEEN ? AND ?", (store,start.isoformat(),end.isoformat())):
            op=decode_operation(row[0])
            if start <= op.occurred <= end:
                result.append(op)
        return result

    def operations_by_keys(self, store, keys, *, referring_returns=False):
        """Пакетный поиск исходных продаж или связанных возвратов по (source, ID)."""
        from collections import defaultdict
        groups=defaultdict(set)
        for source,identifier in keys:
            groups[source].add(identifier)
        expression="json_extract(payload,'$.original_sale_id')" if referring_returns else 'identifier'
        result=[]
        for source,identifiers in groups.items():
            identifiers=sorted(identifiers)
            for offset in range(0,len(identifiers),400):
                batch=identifiers[offset:offset+400]
                placeholders=','.join('?' for _ in batch)
                for row in self.db.execute(f'SELECT payload FROM operations WHERE store=? AND source=? AND {expression} IN ({placeholders})',
                                           (store,source,*batch)):
                    op=decode_operation(row[0])
                    if not referring_returns or op.kind==Kind.RETURN:result.append(op)
        return result

    def set_setting(self, store: str, name: str, value):
        if 'token' in name.lower() or 'secret' in name.lower():
            raise ValueError('Секреты нельзя сохранять в SQLite')
        with self.db:
            self.db.execute('INSERT INTO settings VALUES(?,?,?) ON CONFLICT(store,name) DO UPDATE SET payload=excluded.payload',
                            (store, name, json.dumps(value, ensure_ascii=False)))

    def setting(self, store: str, name: str, default=None):
        row = self.db.execute('SELECT payload FROM settings WHERE store=? AND name=?', (store, name)).fetchone()
        return json.loads(row[0]) if row else default

    def backup(self, path: str | Path):
        from .backups import write_backup
        return write_backup(self.db, path)

    def restore(self, path: str | Path):
        from .backups import restore_backup
        return restore_backup(self.db, path)

    def replace_source(self, store: str, source: str, operations: list[Operation]):
        """Атомарно заменяет только нормализованные операции заданного источника."""
        encoded = {}
        for op in operations:
            if op.store != store or op.source != source or op.identifier in encoded:
                raise ValueError('Неверная принадлежность или дубликат нормализованной операции')
            payload = operation_payload(op)
            encoded[op.identifier] = json.dumps(payload, ensure_ascii=False, sort_keys=True)
        existing = dict(self.db.execute('SELECT identifier,payload FROM operations WHERE store=? AND source=?',(store,source)))
        with self.db:
            for identifier, old in existing.items():
                if encoded.get(identifier) != old:
                    self.db.execute('INSERT INTO revisions(store,source,identifier,old_payload) VALUES(?,?,?,?)',(store,source,identifier,old))
                if identifier not in encoded:
                    self.db.execute('DELETE FROM operations WHERE store=? AND source=? AND identifier=?',(store,source,identifier))
            for identifier, payload in encoded.items():
                self.db.execute('INSERT INTO operations VALUES(?,?,?,?) ON CONFLICT(store,source,identifier) DO UPDATE SET payload=excluded.payload',(store,source,identifier,payload))

    def close(self):
        self.db.close()
