from datetime import timedelta
import hashlib
import json
from ..sync import Page, Record, encode
from .transport import ApiError
from ..finance import money

class Advertising:
    name = 'advertising'
    category = 'promotion'
    def __init__(self, client, start, end):
        self.client, self.start, self.end = client, start, end
    async def pages(self, cursor):
        from datetime import date
        current = date.fromisoformat(cursor) if cursor else self.start.replace(day=1)
        while current <= self.end:
            import calendar
            end = min(self.end, current.replace(day=calendar.monthrange(current.year,current.month)[1]))
            scope=current.strftime('%Y-%m')
            rows = await self.client.request('GET', '/adv/v1/upd', params={'from': current.isoformat(), 'to': end.isoformat()})
            if not isinstance(rows, list):
                raise ApiError('Изменилась схема рекламных затрат')
            records = []
            occurrences = {}
            for row in sorted(rows,key=lambda r:json.dumps(r,sort_keys=True,default=encode)):
                if not isinstance(row, dict) or any(key not in row for key in ('advertId','updSum','updTime','paymentType','updNum')):
                    raise ApiError('Не хватает обязательных полей рекламной операции')
                try:
                    money(row['updSum'])
                    if not isinstance(row['paymentType'],str):raise ValueError()
                except (ValueError,TypeError,ArithmeticError):
                    raise ApiError('Неверная сумма или источник рекламных затрат') from None
                # updNum может быть 0, updTime — null: один updNum не является уникальным ключом.
                identity={key:row[key] for key in ('advertId','updTime','updNum','paymentType')}
                digest = hashlib.sha256(json.dumps(identity, sort_keys=True, default=encode).encode()).hexdigest()
                occurrences[digest] = occurrences.get(digest, 0) + 1
                records.append(Record(f'{scope}:{digest}:{occurrences[digest]}', row))
            current = end + timedelta(days=1)
            yield Page(tuple(records), current.isoformat(), current > self.end, snapshot_scope=scope)
