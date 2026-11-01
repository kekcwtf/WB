from ..sync import Page, Record
from .transport import ApiError
from ..finance import money


def fbw_rate(row):
    if not isinstance(row, dict) or type(row.get('subjectID')) is not int or row['subjectID'] <= 0:
        raise ValueError('Неизвестная категория комиссии FBW')
    rate = money(row['paidStorageKgvp'])
    if not 0 <= rate <= 100:
        raise ValueError('Комиссия FBW вне поддерживаемого диапазона')
    return rate

class Tariffs:
    name = 'tariffs'
    category = None
    def __init__(self, client):
        self.client = client
    async def pages(self, cursor):
        response = await self.client.request('GET', '/api/v1/tariffs/commission', params={'locale':'ru'})
        if not isinstance(response, dict) or not isinstance(response.get('report'), list):
            raise ApiError('Изменилась схема комиссий WB')
        records=[]
        for row in response['report']:
            try:
                fbw_rate(row)
            except (ValueError, KeyError, TypeError, ArithmeticError):
                raise ApiError('Нет достоверной категории или комиссии FBW в тарифе') from None
            records.append(Record(str(row['subjectID']),row))
        yield Page(tuple(records), None, True, snapshot=True)
