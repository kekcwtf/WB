"""Причины ограничений и точная исходная строка; финансовый реестр не изменяется."""
from dataclasses import dataclass
from decimal import Decimal
import json
from .finance import Kind
from .storage import operation_payload
from .normalization import financial_row


@dataclass(frozen=True)
class OperationReview:
    reasons: tuple[str, ...]
    normalized_json: str
    raw_json: str | None
    raw_identifier: str | None


def needs_attention(op):
    return (op.kind in {Kind.UNKNOWN, Kind.ADJUSTMENT} or not op.confirmed
            or (op.sku is None and op.kind != Kind.PAYOUT))


def review_operation(ledger, operation):
    reasons = []
    if operation.kind == Kind.UNKNOWN:
        reasons.append('Неизвестная операция блокирует полный расчёт. Нулевая сумма маркера не означает, что неизвестных расходов нет.')
    if operation.kind == Kind.ADJUSTMENT:
        reasons.append('Корректировка требует проверки основания и налогового учёта; её нельзя автоматически считать комиссией или повторно списывать.')
    if operation.sku is None and operation.kind != Kind.PAYOUT:
        reasons.append('Нет привязки к SKU. Сумма учитывается у магазина; общие расходы допустимы. Метод распределения задаётся отдельно.')
    if not operation.confirmed:
        reasons.append('Предварительная запись: подтверждённый финансовый отчёт ещё не получен.')
    raw, raw_identifier = None, None
    if operation.source == 'wb_finance':
        # Только документированный ключ адаптера: reportId:rrdId:suffix.
        parts = operation.identifier.split(':')
        cache_exists = ledger.db.execute("SELECT 1 FROM sqlite_master WHERE type='table' AND name='source_records'").fetchone()
        if cache_exists and len(parts) == 3 and all(part.isdecimal() for part in parts[:2]):
            candidate = ':'.join(parts[:2])
            row = ledger.db.execute("SELECT payload FROM source_records WHERE store=? AND source='finance_weekly' AND identifier=?",
                                    (operation.store, candidate)).fetchone()
            if row:
                data = json.loads(row[0], parse_float=Decimal)
                if f"{data.get('reportId')}:{data.get('rrdId')}" == candidate:
                    raw_identifier = candidate
                    raw = json.dumps(data, ensure_ascii=False, indent=2, default=str)
                    try:
                        reasons.extend(financial_row(operation.store, data).issues)
                    except (ValueError, TypeError, KeyError, ArithmeticError):
                        reasons.append('Исходная строка не проходит текущую проверку формата. Суммы не переинтерпретированы.')
        if raw is None:
            reasons.append('Исходная строка WB по точному ключу не найдена. Совпадения по сумме или соседней дате не используются.')
    if not reasons:
        reasons.append('В этой записи нет перечисленных признаков ограничения. Это не подтверждает полноту периода или прибыль магазина.')
    return OperationReview(tuple(dict.fromkeys(reasons)),
        json.dumps(operation_payload(operation), ensure_ascii=False, indent=2, default=str), raw, raw_identifier)
