"""Несекретная сводка заявленных прав; не подтверждение подлинности JWT."""
from datetime import datetime, timezone
from .api.auth import Claims, CATEGORIES

SOURCE_CATEGORIES = {
    'report_totals': 'finance', 'finance_weekly': 'finance', 'orders': 'statistics',
    'sales': 'statistics', 'products': 'content', 'ad_account': 'promotion',
    'ad_payments': 'promotion', 'ad_campaigns': 'promotion', 'ad_statistics': 'promotion',
    'advertising': 'promotion', 'tariffs': None, 'stocks': 'analytics',
}


def validate_access(value):
    if not isinstance(value, dict) or set(value) != {'mask', 'account_type', 'expires_at', 'recorded_at'}:
        raise ValueError('Неверный формат сводки доступа API')
    for field in ('mask', 'account_type', 'expires_at'):
        if type(value[field]) is not int:
            raise ValueError('Неверный формат сводки доступа API')
    if value['mask'] < 0:
        raise ValueError('Неверная маска категорий API')
    recorded = datetime.fromisoformat(value['recorded_at'])
    if recorded.tzinfo is None:
        raise ValueError('Неизвестен часовой пояс сводки доступа API')
    # Исключаем непредставимую дату без обращения к секретному хранилищу.
    datetime.fromtimestamp(value['expires_at'], timezone.utc)
    return value


def save_access(ledger, shop, claims, *, now=None):
    now = now or datetime.now(timezone.utc)
    if now.tzinfo is None:
        raise ValueError('Нужен часовой пояс времени сохранения')
    value = {'mask': claims.mask, 'account_type': claims.account_type,
             'expires_at': claims.expires_at, 'recorded_at': now.isoformat()}
    validate_access(value)
    ledger.set_setting(shop, 'api_access', value)


def load_access(ledger, shop, *, now=None):
    value = ledger.setting(shop, 'api_access')
    if value is None:
        return None
    validate_access(value)
    now = now or datetime.now(timezone.utc)
    if now.tzinfo is None:
        raise ValueError('Нужен часовой пояс времени проверки')
    return Claims(value['mask'], value['account_type'], value['expires_at'] <= now.timestamp(), False, value['expires_at'])


def source_permission(claims, source):
    category = SOURCE_CATEGORIES[source]
    requirement = CATEGORIES[category][1] if category else 'Отдельная категория не требуется'
    if claims is None:
        return requirement, 'Нет сведений о правах; доступ не проверен'
    if claims.expired:
        return requirement, 'Срок действия токена истёк'
    if category and not claims.permits(category):
        return requirement, 'Не хватает категории «' + requirement + '»'
    if source == 'report_totals' and not claims.fast_limits:
        return requirement, 'Нужен персональный или сервисный токен для этого метода'
    return requirement, 'Заявлен доступ; требуется подтверждение API'
