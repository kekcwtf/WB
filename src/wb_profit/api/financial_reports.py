from datetime import date
import json
from uuid import uuid4
from .transport import ApiError
from ..sync import Record, Page

class FinancialReports:
    name = 'finance_weekly'
    category = 'finance'
    def __init__(self, client, start: date, end: date):
        self.client, self.start, self.end = client, start, end

    async def pages(self, cursor):
        state = json.loads(cursor) if cursor else {'start': self.start.isoformat(), 'end': self.end.isoformat(), 'rrdId': 0}
        if not state.get('run_id'):
            state['run_id']=uuid4().hex
            state['rrdId']=0
        self.start,self.end=date.fromisoformat(state['start']),date.fromisoformat(state['end'])
        while True:
            rows = await self.client.request('POST', '/api/finance/v1/sales-reports/detailed', body={
                'dateFrom': state['start'], 'dateTo': state['end'], 'period': 'weekly',
                'limit': 100000, 'rrdId': state['rrdId']})
            if rows is None:
                yield Page((), json.dumps(state, sort_keys=True), True,run_id=state['run_id'],replace_window=('rrDate',state['start'],state['end']))
                return
            if not isinstance(rows, list) or not rows:
                raise ApiError('Финансовый API вернул пустой массив вместо документированного 204; курсор сохранён')
            records = []
            for row in rows:
                if not isinstance(row, dict) or not isinstance(row.get('rrdId'), int) or not row.get('reportId'):
                    raise ApiError('Изменилась схема финансового отчёта: нет reportId/rrdId')
                records.append(Record(f"{row['reportId']}:{row['rrdId']}", row))
            next_id = rows[-1]['rrdId']
            if next_id <= state['rrdId']:
                raise ApiError('Финансовый курсор не продвигается')
            state = {**state, 'rrdId': next_id}
            yield Page(tuple(records), json.dumps(state, sort_keys=True),run_id=state['run_id'])
