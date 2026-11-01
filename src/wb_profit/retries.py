"""Сохранённое расписание повторов; не содержит токенов и текстов HTTP-ответов."""
from datetime import datetime,timedelta,timezone
import json

PREFIX='source_retry:'


def _date(value):
    stamp=datetime.fromisoformat(value)
    if stamp.tzinfo is None:raise ValueError('Нужен часовой пояс')
    return stamp


def valid_plan(value):
    try:
        if (not isinstance(value,dict) or type(value['attempt']) is not int or value['attempt']<1 or
                value['kind'] not in {'network','service','rate_limit'}):return False
        return _date(value['next_at'])>=_date(value['not_before'])
    except (KeyError,TypeError,ValueError):return False


def due(plan,now=None,*,force=False):
    now=now or datetime.now(timezone.utc)
    return valid_plan(plan) and now>=_date(plan['not_before']) and (force or now>=_date(plan['next_at']))


def save_failure(ledger,shop,source,error,*,now=None,rate_until=0):
    if not error.retryable:
        ledger.set_setting(shop,PREFIX+source,None)
        return None
    now=now or datetime.now(timezone.utc)
    previous=ledger.setting(shop,PREFIX+source)
    attempt=min(previous['attempt']+1,100) if valid_plan(previous) else 1
    delay=min(1800,60*2**min(attempt-1,5))
    try:
        gate=max(now+timedelta(seconds=max(0,float(error.retry_after or 0))),
                 datetime.fromtimestamp(float(rate_until or 0),timezone.utc))
    except (ValueError,TypeError,OverflowError,OSError):
        gate=datetime.max.replace(tzinfo=timezone.utc)
    plan={'attempt':attempt,'kind':error.kind,'not_before':gate.isoformat(),
          'next_at':max(gate,now+timedelta(seconds=delay)).isoformat()}
    ledger.set_setting(shop,PREFIX+source,plan)
    return plan


def plans(ledger,shops):
    shops=set(shops)
    result=[]
    for shop,name,payload in ledger.db.execute("SELECT store,name,payload FROM settings WHERE name GLOB 'source_retry:*'"):
        if shop not in shops:continue
        try:value=json.loads(payload)
        except (ValueError,TypeError):continue
        if valid_plan(value):result.append((shop,name[len(PREFIX):],value))
    return result


def due_shops(ledger,shops,now=None):
    ready={shop for shop,_source,plan in plans(ledger,shops) if due(plan,now)}
    return [shop for shop in shops if shop in ready]


def network_recovered(ledger,shops,now=None):
    now=now or datetime.now(timezone.utc)
    for shop,source,plan in plans(ledger,shops):
        if plan['kind']=='network':
            updated={**plan,'next_at':max(now,_date(plan['not_before'])).isoformat()}
            ledger.set_setting(shop,PREFIX+source,updated)


def clear_shop(ledger,shop):
    with ledger.db:
        ledger.db.execute("DELETE FROM settings WHERE store=? AND name GLOB 'source_retry:*'",(shop,))
