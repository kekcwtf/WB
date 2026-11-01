"""Сохранённые прежние версии сравниваются с текущей, не с предполагаемой следующей."""
from dataclasses import dataclass
from datetime import date, datetime, timezone
from decimal import Decimal
from .business_dates import MOSCOW
from .finance import Operation
from .storage import decode_operation


@dataclass(frozen=True)
class Revision:
    identifier: int
    observed_at: datetime | None
    previous: Operation
    current: Operation | None

    @property
    def amount_difference(self) -> Decimal | None:
        return self.current.amount - self.previous.amount if self.current is not None else None

    @property
    def observed_later(self):
        return self.observed_at is not None and self.observed_at.date() > self.previous.occurred


@dataclass(frozen=True)
class RevisionPage:
    rows: tuple[Revision, ...]
    has_more: bool


def revision_page(ledger, shop: str, start: date, end: date, *, before_id=None, limit=200):
    if start > end:
        raise ValueError('Дата начала позже даты окончания')
    if type(limit) is not int or not 1 <= limit <= 500:
        raise ValueError('Размер страницы должен быть от 1 до 500')
    if before_id is not None and (type(before_id) is not int or before_id <= 0):
        raise ValueError('Неверный указатель страницы истории')
    values = [shop, start.isoformat(), end.isoformat(), start.isoformat(), end.isoformat()]
    cursor_clause = ''
    if before_id is not None:
        cursor_clause = ' AND r.id < ?'
        values.append(before_id)
    values.append(limit + 1)
    records = ledger.db.execute('''SELECT r.id,r.changed_at,r.old_payload,o.payload,r.source,r.identifier
        FROM revisions r LEFT JOIN operations o
        ON o.store=r.store AND o.source=r.source AND o.identifier=r.identifier
        WHERE r.store=? AND
        (json_extract(r.old_payload,'$.occurred') BETWEEN ? AND ?
         OR json_extract(o.payload,'$.occurred') BETWEEN ? AND ?)'''+cursor_clause+
        ' ORDER BY r.id DESC LIMIT ?', values).fetchall()
    result = []
    for identifier, stamp, old, current, source, operation_id in records[:limit]:
        previous = decode_operation(old)
        current = decode_operation(current) if current is not None else None
        for operation in (previous, current):
            if operation is not None and (operation.store, operation.source, operation.identifier) != (shop, source, operation_id):
                raise ValueError('История не соответствует ключу операции')
        try:
            observed = datetime.fromisoformat(stamp)
            # CURRENT_TIMESTAMP в SQLite документирован в UTC.
            if observed.tzinfo is None:
                observed = observed.replace(tzinfo=timezone.utc)
            observed = observed.astimezone(MOSCOW)
        except (ValueError, TypeError, OverflowError):
            observed = None
        result.append(Revision(identifier, observed, previous, current))
    return RevisionPage(tuple(result), len(records) > limit)
