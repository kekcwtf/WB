"""Для прогноза доступны только свежие завершённые снимки FBW и категорий карточек."""
from dataclasses import dataclass
from datetime import datetime, timedelta, timezone
from decimal import Decimal
from .api.tariffs import fbw_rate
from .data_views import records
from .business_dates import MOSCOW

MAX_AGE = timedelta(hours=24)


@dataclass(frozen=True)
class ForecastRates:
    by_sku: dict[str, Decimal]
    note: str
    valid_until: datetime | None = None


def _fresh(ledger, shop, source, now):
    row = ledger.db.execute('SELECT completed,status,last_success FROM sync_state WHERE store=? AND source=?',
                            (shop, source)).fetchone()
    if row is None or not row[0] or row[1] != 'success':
        return False, None, 'нет завершённого успешного обновления'
    try:
        stamp = datetime.fromisoformat(row[2])
        if stamp.tzinfo is None:
            raise ValueError()
        age = now - stamp
        if age < timedelta(0):
            return False, stamp, 'время загрузки находится в будущем; проверьте часы'
        if age >= MAX_AGE:
            return False, stamp, 'снимку 24 часа или больше'
        return True, stamp, ''
    except (ValueError, TypeError, OverflowError):
        return False, None, 'дата загрузки неизвестна'


def forecast_rates(ledger, shop, operations, metadata, *, now=None):
    now = now or datetime.now(timezone.utc)
    if now.tzinfo is None:
        raise ValueError('Для проверки возраста тарифа нужен часовой пояс')
    operations = list(operations)
    if any(operation.store != shop for operation in operations):
        raise ValueError('Тарифы прогноза выбираются отдельно по магазину')
    tariff_ok, stamp, reason = _fresh(ledger, shop, 'tariffs', now)
    suffix = ' Историческая комиссия остаётся оценкой; индивидуальные опции и изменение спроса не прогнозируются.'
    if not tariff_ok:
        return ForecastRates({}, 'Базовый тариф FBW не применён: ' + reason + '.' + suffix)
    cards_ok, cards_stamp, reason = _fresh(ledger, shop, 'products', now)
    if not cards_ok:
        return ForecastRates({}, 'Базовый тариф FBW не применён: категория товара не подтверждена свежими карточками — ' + reason + '.' + suffix)
    try:
        rates = {str(row['subjectID']): fbw_rate(row) for row in records(ledger, shop, 'tariffs')}
    except (ValueError, KeyError, TypeError, ArithmeticError):
        return ForecastRates({}, 'Сохранённый тариф FBW не прошёл проверку формата. Используется историческая комиссия.' + suffix)
    by_sku = {}
    for operation in operations:
        if not operation.sku:
            continue
        card = metadata.get(operation.sku.split(':', 1)[0], {})
        if not isinstance(card, dict):
            continue
        subject = card.get('subjectID')
        if type(subject) is int and str(subject) in rates:
            by_sku[operation.sku] = rates[str(subject)]
    observed = stamp.astimezone(MOSCOW).strftime('%d.%m.%Y %H:%M МСК')
    return ForecastRates(by_sku, f'Снимок базовой комиссии FBW получен {observed}; снимки тарифа и карточек моложе 24 часов. '
                         'Это правило свежести кеша, а не срок действия тарифа WB. Для SKU без достоверной категории используется историческая комиссия. '
                         'Индивидуальные опции конструктора тарифов не учтены; фактические комиссии берутся из финансового отчёта.',
                         min(stamp, cards_stamp) + MAX_AGE)
