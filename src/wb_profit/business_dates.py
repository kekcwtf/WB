from datetime import datetime,timedelta,timezone

MOSCOW=timezone(timedelta(hours=3))


def statistics_timestamp(value):
    """OrdersItem/SalesItem: дата без зоны документирована как UTC+3."""
    if not isinstance(value,str):return None
    try:
        stamp=datetime.fromisoformat(value.replace('Z','+00:00'))
        return stamp.replace(tzinfo=MOSCOW) if stamp.tzinfo is None else stamp.astimezone(MOSCOW)
    except ValueError:return None


def statistics_day(value):
    stamp=statistics_timestamp(value)
    return stamp.date() if stamp is not None else None

def moscow_operation_day(value):
    """Без часового пояса неизвестно, к какому московскому дню относится операция."""
    if not isinstance(value,str):return None
    try:
        stamp=datetime.fromisoformat(value.replace('Z','+00:00'))
        return stamp.astimezone(MOSCOW).date() if stamp.tzinfo is not None else None
    except ValueError:return None


def business_today(now=None):
    stamp=now if now is not None else datetime.now(MOSCOW)
    if stamp.tzinfo is None:raise ValueError('Для определения московского дня нужен часовой пояс')
    return stamp.astimezone(MOSCOW).date()
