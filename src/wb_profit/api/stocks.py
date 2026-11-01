import asyncio
import json
from urllib.parse import quote
from ..sync import Page, Record
from .transport import ApiError

class ReportExpired(ApiError):
    pass

class Stocks:
    name = 'stocks'
    category = 'analytics'
    def __init__(self, client, sleep=asyncio.sleep):
        self.client, self.sleep = client, sleep
    async def pages(self, cursor):
        task = cursor
        if not task:
            created = await self.client.request('GET', '/api/v1/warehouse_remains', params={
                'groupByNm': 'true', 'groupByBarcode': 'true', 'groupBySize': 'true', 'groupBySa': 'true', 'groupBySubject': 'true'})
            task = created.get('data', {}).get('taskId') if isinstance(created, dict) else None
            if not isinstance(task, str) or not task:
                raise ApiError('В ответе создания отчёта остатков нет taskId')
            yield Page((), task)
        task_path = '/api/v1/warehouse_remains/tasks/' + quote(task, safe='')
        for _ in range(60):
            result = await self.client.request('GET', task_path + '/status')
            status = result.get('data', {}).get('status') if isinstance(result, dict) else None
            if status == 'done':
                break
            if status in {'purged','canceled'}:
                raise ReportExpired('Задание остатков истекло; при следующем обновлении будет создано новое')
            if status not in {'new', 'processing'}:
                raise ApiError('Задание остатков недоступно или отклонено; нужна новая генерация')
            await self.sleep(5)
        else:
            raise ApiError('Отчёт остатков ещё формируется; загрузка продолжится позже')
        rows = await self.client.request('GET', task_path + '/download')
        if not isinstance(rows, list):
            raise ApiError('Изменилась схема отчёта остатков')
        records=[]
        for row in rows:
            if not isinstance(row, dict) or 'nmId' not in row or 'barcode' not in row or not isinstance(row.get('warehouses'),list):
                raise ApiError('Не хватает полей в отчёте остатков')
            key = json.dumps([row['nmId'],row['barcode'],row.get('techSize')], ensure_ascii=False)
            records.append(Record(key,row))
        yield Page(tuple(records), task, True, snapshot=True)
