from datetime import date
import json
from .transport import ApiError
from ..sync import Page, Record

class StatisticsSource:
    category = 'statistics'
    kind = ''
    def __init__(self, client, start: date):
        self.client, self.start = client, start
    async def pages(self, cursor):
        since = cursor or self.start.isoformat()
        while True:
            rows = await self.client.request('GET', '/api/v1/supplier/' + self.kind,
                                             params={'dateFrom': since, 'flag': 0})
            if not isinstance(rows, list):
                raise ApiError('Изменилась схема оперативного отчёта')
            if not rows:
                yield Page((), since, True)
                return
            records = []
            for row in rows:
                if not isinstance(row, dict):
                    raise ApiError('Изменилась схема оперативной записи')
                identifier = row.get('srid') if self.kind == 'orders' else row.get('saleID')
                if not identifier or not isinstance(row.get('lastChangeDate'),str) or not row['lastChangeDate']:
                    raise ApiError('В оперативной записи нет стабильного ID или lastChangeDate')
                records.append(Record(str(identifier), row))
            changed = rows[-1]['lastChangeDate']
            if changed <= since:
                raise ApiError('Оперативный курсор не продвигается; требуется сверка граничных записей')
            since = changed
            yield Page(tuple(records), since)
